"""Local transcripts of the provider's Codex-backed conversations (kept for Grounds and future S1 work).

Why the provider writes them: the Claude backend (bridge.py) runs a headless Claude CLI, and Claude Code itself keeps
each conversation's transcript under that account's config dir (<CLAUDE_CONFIG_DIR>/projects/<scratch path>/*.jsonl).
The Codex backend (codex_bridge.py) opens its threads with `"ephemeral": True`, so Codex keeps nothing. They stay
ephemeral on purpose: a persistent thread in CODEX_HOME could be picked up by Codex's own memories / resume features
and carry one conversation's context into another, and it would sit in a vendor-owned layout. So the provider writes
its own record of each Codex-backed turn from what it already holds: the client's new content and tool results, and
the backend's reply as delivered (text, tool calls), appended per turn.

Layout: $CORTADOS_HOME/transcripts/codex/<UTC day the conversation opened>/<conversation id>.jsonl. Directories 0700,
files 0600, append-only, opened through compaction/store.py's anchored primitives (no symlink, FIFO or hard link is
ever written through). Local files only: nothing here syncs or uploads them. The first line of each file is the
conversation header carrying the schema version (SCHEMA); then one line per turn. There is no deletion code.

Every text goes through the run export's redaction (child_tasks.redact_for_export: known provider credentials,
structurally and fail-closed, then well-known secret shapes scrubbed), on the writer thread, before it is written.

Never fails or slows the user's request: the request path only serializes the turn and enqueues it on a bounded
background writer (compaction/store.Store's queue and thread). Every failure (a full queue, an I/O error, a cap below)
is logged and counted in router_db's route-log error counter (op "transcript"), never raised and never silent.

Guards, each with its reason:
- FILE_MAX_MB (CORTADOS_TRANSCRIPT_FILE_MAX_MB): one conversation's file stops growing past this. A conversation stuck
  in a loop (the Grounds capture client once wrote ~52 TB in 11 days through one) stops here instead of filling the
  disk; one "stopped" line says so, later turns are counted, not written.
- MIN_FREE_GB (CORTADOS_TRANSCRIPT_MIN_FREE_GB): nothing is written while the volume has less free space than this,
  so transcripts are the first thing to stop, before router.db, the OS or the backends run out of room.
- A turn is written once: each conversation remembers the last turn index it queued (its history length), and the
  writer the last one it wrote per file, so a replayed or repeated request can never multiply writes.

Switch: CORTADOS_TRANSCRIPTS (default on); 0 / off / false / no turns it off.
"""
from __future__ import annotations

import atexit
import collections
import json
import logging
import os
import re
import threading
import time
from typing import Any

from cortados_router import child_tasks, router_db
from cortados_router.compaction import store as cstore
from cortados_router.compat import getenv

logger = logging.getLogger(__name__)

SCHEMA = "cortados.codex-transcript/1"
DIR_PARTS = ("transcripts", "codex")
ENV_SWITCH = "CORTADOS_TRANSCRIPTS"
OFF_VALUES = ("0", "off", "false", "no")
# Why 256 MiB: a long routed Codex conversation with large tool results is a few to a few tens of MB; ten times that
# still leaves room for any real one, while a looping conversation stops here (see the module docstring).
DEFAULT_FILE_MAX_MB = 256.0
# Why 5 GiB: the Air's other state (router.db, the backends' own files, swap) needs room to keep the provider up;
# below this the transcripts give way first.
DEFAULT_MIN_FREE_GB = 5.0
# Why 200,000 characters per text: one huge tool output (a whole file read twice) must not take the whole file cap in
# one record; real messages and results are far below it. The cut is marked in the text and counted per turn.
TEXT_LIMIT = 200_000
# Why 256 characters: inline image / file payloads (base64) are not useful as transcript text and would fill the file
# cap fast; their kind and size are kept instead.
MEDIA_INLINE_MAX = 256
# Why 4096: the writer's per-file "last turn written" memory must not grow with the provider's uptime; a conversation
# evicted from it is still covered by its own session's counter.
SEEN_FILES_MAX = 4096
# Header / turn fields holding free-form prose rather than structured data.
PROSE_KEYS = ("system", "note")
_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MEDIA_TYPES = ("image", "document", "input_image", "input_file", "inputImage")
_MEDIA_KEYS = ("data", "image_url", "imageUrl", "file_data", "url")


class TranscriptLimit(Exception):
    """A guard stopped a write (file cap, disk floor). Counted like any other failure."""


class TranscriptNotWritten(Exception):
    """What router_db's counter records for a turn that was not written (its message says why; never content)."""


def enabled(environ=None) -> bool:
    v = getenv(ENV_SWITCH, "", environ=environ)
    return (v or "").strip().lower() not in OFF_VALUES


def file_max_bytes() -> int:
    return int(cstore._float_env("CORTADOS_TRANSCRIPT_FILE_MAX_MB", DEFAULT_FILE_MAX_MB) * 1024 * 1024)


def min_free_bytes() -> int:
    return int(cstore._float_env("CORTADOS_TRANSCRIPT_MIN_FREE_GB", DEFAULT_MIN_FREE_GB) * 1024 ** 3)


def file_name(conversation_id: str) -> str:
    """<conversation id>.jsonl; an id that is not a plain token gets the store's escaped name (never a path)."""
    cid = str(conversation_id)
    return f"{cid}.jsonl" if _ID_OK.fullmatch(cid) else cstore.session_file_name(cid)


def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _iso(ts: float | None) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def strip_media(v: Any, in_media: bool = False) -> Any:
    """A copy of `v` with inline media payloads replaced by a marker naming their size."""
    if isinstance(v, list):
        return [strip_media(x, in_media) for x in v]
    if isinstance(v, dict):
        media = in_media or v.get("type") in _MEDIA_TYPES
        out = {}
        for k, x in v.items():
            if media and k in _MEDIA_KEYS and isinstance(x, str) and len(x) > MEDIA_INLINE_MAX:
                out[k] = f"(omitted by cortados: {len(x)} characters of inline media)"
            else:
                out[k] = strip_media(x, media)
        return out
    return v


def route_meta(route: Any) -> dict[str, Any]:
    """The route fields a transcript keeps: ids, provider, model, lane, band, account, pin. Never config paths."""
    r = route if isinstance(route, dict) else {}
    chosen = ((r.get("route") or {}).get("chosen") or {}) if isinstance(r.get("route"), dict) else {}
    lane = chosen.get("lane") if isinstance(chosen, dict) else None
    if not lane and str(r.get("provider") or "").startswith("codex-cli"):
        lane = "codex"
    out = {"route_id": r.get("route_id"), "provider": r.get("provider"), "model": r.get("model"), "lane": lane,
           "band": r.get("band"), "account": r.get("account"), "pinned": bool(r.get("pinned"))}
    return {k: v for k, v in out.items() if v is not None}


class Writer(cstore.Store):
    """The compaction store's bounded queue and single writer thread, for transcripts: every failure is counted in
    router_db's route-log error counter (and in this writer's own counters), its log line throttled there."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.written = 0                        # turn lines written
        self.duplicates = 0                     # turns refused as already queued or written (replays)
        self._seen: "collections.OrderedDict[str, int]" = collections.OrderedDict()   # writer thread only
        self._stopped: set[str] = set()         # files that got their "stopped" line (writer thread only)

    def _count(self, attr: str, why: str) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + 1)
            self.last_failure = why
        # One counter for every lost record write (router_db.note_error logs at most once a minute, with the total).
        router_db.note_error("transcript", TranscriptNotWritten(why), final=True)

    def stats(self) -> dict[str, Any]:
        out = super().stats()
        out.update(enabled=enabled(), written=self.written, duplicates=self.duplicates)
        return out

    def note_duplicate(self) -> None:
        with self._lock:
            self.duplicates += 1

    # -- the job (writer thread)
    def write_turn(self, day: str, name: str, n: int, payload: str) -> None:
        """Redact the queued turn and append it (with the header when the file is new). Raises on any failure; the
        Store counts it."""
        key = f"{day}/{name}"
        if self._seen.get(key, -1) >= n:
            self.note_duplicate()
            return
        job = json.loads(payload)
        red = child_tasks.provider_redactor(job.get("tokens") or ())
        withheld = truncated = 0
        scrubbed: dict[str, int] = {}

        def one(v, prose=False):
            nonlocal withheld, truncated
            out, w, s, t = child_tasks.redact_for_export(red, v, TEXT_LIMIT, prose=prose)
            withheld, truncated = withheld + w, truncated + t
            for k, c in s.items():
                scrubbed[k] = scrubbed.get(k, 0) + c
            return out

        def clean(v, key=None):
            # Message prose (a text block's text, the system prompt) may start with `[` or `{` and is redacted as
            # prose; everything else (tool inputs, ids, metadata) structurally, fail closed.
            if isinstance(v, list):
                return [clean(x) for x in v]
            if isinstance(v, dict):
                if v.get("type") in ("text", "input_text", "output_text") and isinstance(v.get("text"), str):
                    rest = one({k: x for k, x in v.items() if k != "text"})
                    if isinstance(rest, dict) and "text" not in rest:
                        rest["text"] = one(v["text"], prose=True)
                        return rest
                    return one(v)
                return {one(str(k), prose=True): clean(x, k) for k, x in v.items()}
            if isinstance(v, str) and key in PROSE_KEYS:
                return one(v, prose=True)
            return one(v)

        header, turn = clean(job["header"]), clean(job["turn"])
        if isinstance(turn, dict):
            turn["redaction"] = {"withheld": withheld, "scrubbed": scrubbed, "truncated": truncated}
        turn_line = json.dumps(turn, ensure_ascii=True) + "\n"
        dfd = cstore.open_store_dir(*DIR_PARTS, day, create=True)
        try:
            st = os.fstatvfs(dfd)
            free = st.f_bavail * st.f_frsize
            floor = min_free_bytes()
            fd = cstore.open_store_file(name, dfd, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            try:
                size = os.fstat(fd).st_size
                data = (json.dumps(header, ensure_ascii=True) + "\n" if size == 0 else "") + turn_line
                if free - len(data) < floor:
                    raise TranscriptLimit(f"limit: disk floor ({free} bytes free, floor {floor}); turn not written")
                cap = file_max_bytes()
                if size + len(data) > cap:
                    if key not in self._stopped:
                        self._stopped.add(key)
                        _write_all(fd, json.dumps({"kind": "stopped", "reason": "file_cap", "cap_bytes": cap,
                                                   "at": _iso(time.time()), "next_turn": n}) + "\n")
                    raise TranscriptLimit(f"limit: file cap ({cap} bytes) of {key}; turn {n} not written")
                _write_all(fd, data)
            finally:
                os.close(fd)
        finally:
            os.close(dfd)
        self._seen[key] = n
        self._seen.move_to_end(key)
        while len(self._seen) > SEEN_FILES_MAX:
            self._seen.popitem(last=False)
        with self._lock:
            self.written += 1


def _write_all(fd: int, data: str) -> None:
    buf = data.encode("utf-8")
    while buf:
        buf = buf[os.write(fd, buf):]


WRITER = Writer()
_session_lock = threading.Lock()


def record_turn(session: Any, *, tokens=(), user_content: Any, tool_results: Any, assistant_blocks: Any,
                stop: Any = None, usage: Any = None, started_at: float | None = None, meta: dict | None = None,
                rebuilt: bool = False, one_shot: bool = False, wire: str | None = None) -> bool:
    """Queue one Codex-backed turn of `session` for the writer. Call it after the reply is known and BEFORE the turn's
    reply fingerprint is appended to session.history (its length is the turn index). Never raises; True when queued.
    `tokens`: credentials to redact besides this process's own (the provider's owner token)."""
    n = last = None
    try:
        if not enabled():
            return False
        with _session_lock:
            last = getattr(session, "transcript_last", -1)
            if last >= len(getattr(session, "history", None) or ()):
                WRITER.note_duplicate()          # a replay or a repeat of a turn already queued: never written twice
                return False
            n = len(session.history or ())
            session.transcript_last = n
        cid = str(session.id)
        opened = getattr(session, "opened_at", None)
        opened = opened if isinstance(opened, (int, float)) else time.time()
        route = route_meta(getattr(session, "route", None))
        header: dict[str, Any] = {"schema": SCHEMA, "kind": "conversation", "conversation_id": cid,
                                  "backend": "codex", "opened_at": _iso(opened), "route": route,
                                  "one_shot": bool(one_shot), "wire": wire}
        parent = getattr(session, "transcript_parent", None)
        if parent:
            header["rebuilt_from"] = str(parent)  # the conversation this one was rebuilt from (its own file)
        if last < 0:                             # this session's first turn: what its backend was started with
            header["system"] = getattr(session, "system", None)
            header["tools"] = sorted(getattr(session, "tool_names", None) or ())
        if n > 0 and not rebuilt and last < 0:
            header["note"] = "earlier turns of this conversation were not recorded by this session"
        elif last >= 0:
            header["note"] = "header written by a later turn: this conversation's earlier turns were not written"
        now = time.time()
        turn = {"kind": "turn", "n": n, "started_at": _iso(started_at), "ended_at": _iso(now), "route": route,
                **{k: v for k, v in (meta or {}).items() if v is not None},
                "rebuilt": bool(rebuilt),
                "user": strip_media(user_content or []),
                "tool_results": [{"tool_use_id": k, **strip_media(v if isinstance(v, dict) else {"content": v})}
                                 for k, v in (tool_results or {}).items()],
                "assistant": strip_media(list(assistant_blocks or [])), "stop": stop,
                "usage": usage if isinstance(usage, dict) else None}
        payload = json.dumps({"header": header, "turn": turn, "tokens": [t for t in tokens if t]}, default=str)
        name = file_name(cid)
        day = day_of(opened)
        ok = WRITER.submit(f"transcript turn {n} of {cid[:8]}",
                           lambda: WRITER.write_turn(day, name, n, payload), nbytes=len(payload))
    except Exception as exc:                     # contained: a transcript never fails a turn
        WRITER._count("schedule_failures", f"transcript turn not queued: {type(exc).__name__}: {exc}")
        ok = False
    if not ok and n is not None:
        with _session_lock:                      # not queued: release the reservation, a later offer may write it
            if getattr(session, "transcript_last", None) == n:
                session.transcript_last = last
    return ok


def _at_exit() -> None:
    WRITER.shutdown()


atexit.register(_at_exit)
