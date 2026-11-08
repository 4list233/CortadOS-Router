"""Append-only local content store for the provider's conversations (phase 1, shadow only).

Off by default; on when CORTADOS_COMPACTION_STORE=1. The owner approved keeping conversation CONTENT on the provider
host (the Air) for a short time, so compaction labels can be harvested from what later turns actually reused. Nothing
here changes what is sent to a backend: the provider calls `record_turn` after a turn's reply is known, and only reads
from the request it already parsed.

Layout: $CORTADOS_HOME/compaction/sessions/<safe-id>-<sha12>.jsonl, directories 0700, files 0600, every open with
O_NOFOLLOW (a planted symlink is refused, not followed). One JSON object per line:
  {"kind": "turn", "ts", "source", "meta": {run_id, session, ...}}      one per provider turn, first
  {"kind": "tool_result", "tool_use_id", "text", "chars", "truncated", "is_error"}
  {"kind": "message", "role": "user" | "assistant", "text"}
  {"kind": "tool_call", "tool_use_id", "name", "input"}
`read_session` turns a file back into the shared `events.Session`.

Retention: files untouched for CORTADOS_COMPACTION_RETENTION_DAYS (default 14) are deleted, then the oldest sessions
go until the total is under CORTADOS_COMPACTION_MAX_MB (default 2048). `prune_expired` runs at provider start and at
most hourly after, in a background thread.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from cortados_router.compaction import policy
from cortados_router.compaction.events import Message, Session, ToolEvent
from cortados_router.compat import getenv

logger = logging.getLogger(__name__)

# The per-result cap is not a number of its own: it is tables/compaction.json's provider_tool_result_cap_tokens
# (8000) x chars_per_token for the conversation's source (2.5 Claude Code -> 20000 chars, 4 Codex -> 32000 chars),
# read through compaction/policy.py. Why: labels need the real content of a result to judge whether a later turn
# reused it, but one huge output must not fill the disk. The same cap applies to a tool call's INPUT (a giant Write
# content must not bypass it). The original length and a truncated flag are kept beside each.
# Why 14 days: long enough for the nightly label harvest plus a week of human review; content can hold secrets that
# tools printed, so it is kept no longer than that.
DEFAULT_RETENTION_DAYS = 14.0
# Why 2048 MB: a hard ceiling on the Air's disk whatever the retention says; oldest sessions go first.
DEFAULT_MAX_MB = 2048.0
# Why hourly: retention is measured in days, so pruning more often only costs directory scans.
PRUNE_INTERVAL_S = 3600.0
FORMAT_VERSION = 1

_SAFE = re.compile(r"[^A-Za-z0-9_-]")


_policy_cache: dict[str, Any] = {}


def load_policy() -> dict[str, Any]:
    """tables/compaction.json through policy.load, once per process (the one source of the shared thresholds)."""
    if "p" not in _policy_cache:
        _policy_cache["p"] = policy.load()
    return _policy_cache["p"]


def chars_per_token(source: str) -> float:
    cpt = load_policy()["chars_per_token"]
    return float(cpt.get(source, cpt["unknown"]))


def cap_chars(source: str) -> int:
    return int(load_policy()["provider_tool_result_cap_tokens"] * chars_per_token(source))


def enabled(environ=None) -> bool:
    return getenv("CORTADOS_COMPACTION_STORE", "", environ=environ) == "1"


def _float_env(name: str, default: float) -> float:
    raw = getenv(name)
    if raw in (None, ""):
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using the default %s", name, raw, default)
        return default
    if v <= 0:
        logger.warning("%s=%r must be positive; using the default %s", name, raw, default)
        return default
    return v


def retention_days() -> float:
    return _float_env("CORTADOS_COMPACTION_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def max_bytes() -> int:
    return int(_float_env("CORTADOS_COMPACTION_MAX_MB", DEFAULT_MAX_MB) * 1024 * 1024)


def root() -> Path:
    home = getenv("CORTADOS_HOME") or os.path.expanduser("~/.cortados")
    return Path(home) / "compaction"


def sessions_dir() -> Path:
    return root() / "sessions"


def session_file_name(conversation_id: str) -> str:
    """A file name that cannot escape the directory: a readable prefix plus a hash that keeps ids distinct."""
    cid = str(conversation_id)
    digest = hashlib.sha256(cid.encode("utf-8", "replace")).hexdigest()[:12]
    safe = _SAFE.sub("_", cid)[:40] or "conv"
    return f"{safe}-{digest}.jsonl"


def session_path(conversation_id: str) -> Path:
    return sessions_dir() / session_file_name(conversation_id)


# ------------------------------------------------------------------ safe file primitives (shared with prune.py)
#
# Every file operation is anchored to directory fds opened one component at a time from $CORTADOS_HOME down, each with
# O_NOFOLLOW + O_DIRECTORY and an fstat check, so a symlinked `compaction/` or `sessions/` (or a symlinked home) can
# never redirect a write, a read or a deletion outside the store. Files are opened relative to those fds with
# O_NOFOLLOW | O_NONBLOCK (a FIFO planted at a session path cannot block the open) and refused unless fstat says
# they are regular files.

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _home() -> str:
    return getenv("CORTADOS_HOME") or os.path.expanduser("~/.cortados")


def _open_dir(name: str, parent_fd: Optional[int], *, create: bool, own: bool) -> int:
    """Open one directory component without following a symlink; `own` dirs (ours) are created and forced 0700."""
    if create:
        try:
            if parent_fd is None:
                os.mkdir(name, 0o700)
            else:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, _DIR_FLAGS) if parent_fd is None else os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise OSError(f"{name} is not a directory; refusing it")
        if own and stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_store_dir(*parts: str, create: bool) -> int:
    """An fd on $CORTADOS_HOME/<parts...>, every component opened with O_NOFOLLOW (a symlinked home, compaction/ or
    sessions/ raises OSError). Raises FileNotFoundError when absent and not `create`. The caller closes it."""
    home = _home()
    if create:
        os.makedirs(os.path.dirname(os.path.abspath(home)), exist_ok=True)
    fd = _open_dir(os.path.abspath(home), None, create=create, own=False)
    try:
        for p in parts:
            nfd = _open_dir(p, fd, create=create, own=True)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_file(name: str, dir_fd: int, flags: int, mode: int = 0o600) -> int:
    fd = os.open(name, flags | _FILE_FLAGS, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{name} is not a regular file; refusing it")
        if (flags & os.O_CREAT) and stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        if flags & (os.O_WRONLY | os.O_RDWR) == 0:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def append_in(parts: tuple, name: str, data: str) -> None:
    """Append `data` to $CORTADOS_HOME/<parts>/<name> (0600), never through a symlink, a FIFO or a non-file."""
    dfd = open_store_dir(*parts, create=True)
    try:
        fd = _open_file(name, dfd, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            buf = data.encode("utf-8")
            while buf:
                n = os.write(fd, buf)
                buf = buf[n:]
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def read_in(parts: tuple, name: str) -> str:
    dfd = open_store_dir(*parts, create=False)
    try:
        fd = _open_file(name, dfd, os.O_RDONLY)
    finally:
        os.close(dfd)
    with os.fdopen(fd, encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------------ content extraction

def _text_of_blocks(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    parts = []
    for b in blocks or ():
        if isinstance(b, dict):
            if b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif b.get("type") in ("image", "input_image"):
                parts.append("[image]")
    return "\n".join(p for p in parts if p)


def _result_record(tool_use_id: str, result: dict[str, Any], cap: int) -> dict[str, Any]:
    content = result.get("content") if isinstance(result, dict) else result
    text = _text_of_blocks(content)
    return {"kind": "tool_result", "tool_use_id": str(tool_use_id), "text": text[:cap],
            "chars": len(text), "truncated": len(text) > cap,
            "is_error": bool(result.get("isError")) if isinstance(result, dict) else False}


def cap_input(inp: Any, cap: int) -> tuple[dict[str, Any], int, bool]:
    """(stored input, original JSON length, truncated). The cap holds on the FINAL serialized form: an input whose
    JSON fits is kept as is; over it, every value is cut to an equal share and the result re-measured; if it still
    does not fit (huge keys, deep structure), the input is stored as {"_truncated_json": <head of its JSON>} with the
    head shrunk until that dict's own JSON fits. A giant Write content can never bypass the cap."""
    inp = inp if isinstance(inp, dict) else {}
    raw = json.dumps(inp, ensure_ascii=False, default=str)
    if len(raw) <= cap:
        return inp, len(raw), False
    share = max(1, cap // max(1, 2 * len(inp)))
    out = {}
    for k, v in inp.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        out[k] = text[:share] if len(text) > share else v
    if len(json.dumps(out, ensure_ascii=False, default=str)) <= cap:
        return out, len(raw), True
    head = raw[:cap]
    while head and len(json.dumps({"_truncated_json": head}, ensure_ascii=False)) > cap:
        over = len(json.dumps({"_truncated_json": head}, ensure_ascii=False)) - cap
        head = head[:len(head) - max(1, over)]
    return {"_truncated_json": head}, len(raw), True


def turn_records(*, source: str, user_content: Any, tool_results: dict[str, Any] | None,
                 assistant_blocks: Iterable[dict[str, Any]], meta: dict[str, Any] | None = None,
                 now: float | None = None) -> list[dict[str, Any]]:
    """The records of one provider turn, in order: turn header, the results answering earlier calls, the new user
    text, the assistant text, the calls it issued."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    cap = cap_chars(source)
    out: list[dict[str, Any]] = [{"v": FORMAT_VERSION, "kind": "turn", "ts": ts, "source": source,
                                  "meta": {k: v for k, v in (meta or {}).items() if v}}]
    for tid, res in (tool_results or {}).items():
        out.append(_result_record(tid, res, cap))
    utext = _text_of_blocks(user_content)
    if utext:
        out.append({"kind": "message", "role": "user", "text": utext})
    blocks = [b for b in assistant_blocks or () if isinstance(b, dict)]
    out.append({"kind": "message", "role": "assistant",
                "text": "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")})
    for b in blocks:
        if b.get("type") == "tool_use":
            inp, n, cut = cap_input(b.get("input"), cap)
            out.append({"kind": "tool_call", "tool_use_id": str(b.get("id", "")), "name": str(b.get("name", "")),
                        "input": inp, "input_chars": n, "input_truncated": cut})
    return out


# ------------------------------------------------------------------ the store

# Why 1024: the writer drains a turn in well under a millisecond, so 1024 queued turns only happen when the disk is
# stuck; past that, records are dropped and counted rather than holding memory or ever blocking a request.
QUEUE_MAX = 1024


class Store:
    """All store I/O (session appends, shadow decisions, retention) runs on ONE background writer thread fed by a
    bounded queue, so it is serialized (retention can never race a write) and the request path only enqueues. Every
    failure (I/O, a full queue, a thread that cannot start) is logged with its reason and counted, never raised:
    this is the one place a failure must not propagate, because the user's request has already been answered by the
    backend and a local logging store must never turn that into an error or stall turn finalization."""

    def __init__(self, queue_max: int = QUEUE_MAX):
        self._lock = threading.Lock()           # guards counters, throttle state and the thread handle only
        self._q: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self._thread: Optional[threading.Thread] = None
        self.write_failures = 0                 # I/O or job failures inside the writer
        self.dropped = 0                        # jobs refused because the queue was full
        self.schedule_failures = 0              # the writer thread could not be started
        self.last_failure: Optional[str] = None
        self._last_prune = 0.0

    # -- counters
    def _count(self, attr: str, why: str) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + 1)
            self.last_failure = why
            n = getattr(self, attr)
        logger.warning("compaction store: %s (%s total: %d)", why, attr, n)

    def stats(self) -> dict[str, Any]:
        return {"enabled": enabled(), "write_failures": self.write_failures, "dropped": self.dropped,
                "schedule_failures": self.schedule_failures, "last_failure": self.last_failure,
                "queued": self._q.qsize()}

    # -- the writer
    def _ensure_writer(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._run, daemon=True, name="cortados-compaction-writer")
            t.start()
            self._thread = t

    def _run(self) -> None:
        while True:
            name, job = self._q.get()
            try:
                job()
            except Exception as exc:  # deliberately broad: see the class docstring
                self._count("write_failures", f"{name} failed: {type(exc).__name__}: {exc}")
            finally:
                self._q.task_done()

    def submit(self, name: str, job) -> bool:
        """Queue `job` for the writer; never blocks and never raises. False when it was not queued."""
        try:
            self._ensure_writer()
            self._q.put_nowait((name, job))
            return True
        except queue.Full:
            self._count("dropped", f"{name} dropped: writer queue full ({self._q.maxsize})")
        except Exception as exc:  # thread start failure and anything else: contained, counted
            self._count("schedule_failures", f"{name} not scheduled: {type(exc).__name__}: {exc}")
        return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is drained (tests, shutdown). True when drained."""
        deadline = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < deadline:
            time.sleep(0.005)
        return not self._q.unfinished_tasks

    # -- jobs
    def record_turn(self, conversation_id: str, **kw) -> bool:
        """Queue one turn for the writer. Returns True when queued; building and writing the records happen on the
        writer thread, off the request path."""
        if not enabled():
            return False
        cid = str(conversation_id)

        def job():
            lines = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in turn_records(**kw))
            append_in(("compaction", "sessions"), session_file_name(cid), lines)

        ok = self.submit(f"turn of conversation {cid[:8]}", job)
        self.maybe_prune_async()
        return ok

    def maybe_prune_async(self, now: float | None = None) -> bool:
        """Queue a retention pass when the last one is over PRUNE_INTERVAL_S old; never blocks, never raises. The
        throttle is reset when the pass could not be queued, so the next call retries."""
        try:
            if not enabled():
                return False
            now = time.time() if now is None else now
            with self._lock:
                if now - self._last_prune < PRUNE_INTERVAL_S:
                    return False
                prev, self._last_prune = self._last_prune, now
            queued = False
            try:
                queued = self.submit("retention", lambda: prune_expired(now))
            finally:
                if not queued:
                    with self._lock:
                        self._last_prune = prev
            return queued
        except Exception as exc:  # contained like every scheduling failure
            self._count("schedule_failures", f"retention not scheduled: {type(exc).__name__}: {exc}")
            return False


def prune_expired(now: float | None = None) -> list[str]:
    """Delete session files older than the retention, then the oldest until the total fits the size cap. Returns
    the deleted file names. Runs on the writer thread (serialized with appends). Everything is relative to a dir fd
    opened through verified, non-symlink components; only regular *.jsonl files are touched, and each candidate is
    re-stat'ed just before unlinking (same inode, still past its criterion) so a changed file is left alone."""
    now = time.time() if now is None else now
    try:
        dfd = open_store_dir("compaction", "sessions", create=False)
    except FileNotFoundError:
        return []
    try:
        files = []
        for name in os.listdir(dfd):
            if not name.endswith(".jsonl"):
                continue
            try:
                est = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(est.st_mode):
                files.append((est.st_mtime, est.st_size, name, est.st_ino))
        files.sort()
        removed: list[str] = []
        cutoff = now - retention_days() * 86400
        keep = []
        for mtime, size, name, ino in files:
            if mtime < cutoff:
                _unlink_if(dfd, name, ino, lambda st: st.st_mtime < cutoff, removed)
            else:
                keep.append((mtime, size, name, ino))
        total, cap = sum(k[1] for k in keep), max_bytes()
        for mtime, size, name, ino in keep:
            if total <= cap:
                break
            if _unlink_if(dfd, name, ino, lambda st, m=mtime: st.st_mtime <= m, removed):
                total -= size
    finally:
        os.close(dfd)
    if removed:
        logger.info("compaction store: pruned %d session file(s)", len(removed))
    return removed


def _unlink_if(dfd: int, name: str, ino: int, still, removed: list[str]) -> bool:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_ino != ino or not still(st):
            return False
        os.unlink(name, dir_fd=dfd)
        removed.append(name)
        return True
    except FileNotFoundError:
        return False


def read_session(path: str | os.PathLike) -> Session:
    """Read one session file into events.Session. Its source is the conversation's wire as the first turn header
    recorded it ("claude-code" / "codex"; "provider" when there is none), so token estimates use the right ratio.
    Calls without a result are interrupted."""
    p = Path(path)
    sid = p.stem
    messages: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    turn_ts: Optional[str] = None
    source: Optional[str] = None
    if p.parent == sessions_dir():
        text = read_in(("compaction", "sessions"), p.name)       # anchored to verified dir fds
    else:                                                         # an explicit path elsewhere: still no symlink/FIFO
        fd = os.open(p, os.O_RDONLY | _FILE_FLAGS)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{p} is not a regular file; refusing it")
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, encoding="utf-8") as f:
            text = f.read()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            logger.warning("compaction store: unreadable line in %s skipped", p.name)
            continue
        kind = r.get("kind")
        if kind == "turn":
            turn_ts = r.get("ts")
            source = source or r.get("source")
        elif kind == "message":
            messages.append({"role": r.get("role", ""), "text": r.get("text", ""), "ts": turn_ts, "events": []})
        elif kind == "tool_call":
            mi = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), 0)
            c = {"name": r.get("name", ""), "input": r.get("input") or {}, "message_index": mi, "ts": turn_ts,
                 "result": None, "index": len(calls)}
            calls.append(c)
            by_id[r.get("tool_use_id", "")] = c
            if messages:
                messages[mi]["events"].append(c["index"])
        elif kind == "tool_result":
            c = by_id.get(r.get("tool_use_id", ""))
            if c is not None:
                c["result"] = r
    msgs = tuple(Message(session=sid, index=i, role=m["role"], text=m["text"],
                         tool_event_indexes=tuple(m["events"]), ts=m["ts"]) for i, m in enumerate(messages))
    events = tuple(ToolEvent(session=sid, index=c["index"], message_index=c["message_index"], name=c["name"],
                             input=c["input"], result_text=(c["result"] or {}).get("text", ""),
                             result_chars=int((c["result"] or {}).get("chars", 0)),
                             is_error=bool((c["result"] or {}).get("is_error", False)),
                             interrupted=c["result"] is None, ts=c["ts"]) for c in calls)
    return Session(session=sid, source=source or "provider", messages=msgs, events=events)


STORE = Store()
