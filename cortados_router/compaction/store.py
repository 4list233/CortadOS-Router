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

def ensure_dir(path: Path) -> None:
    """Create `path` (and parents) 0700; refuse it if it is a symlink or not a directory."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a plain directory (symlink or file); refusing to write there")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def append_line(path: Path, line: str) -> None:
    """Append one line to `path`, created 0600, never through a symlink (O_NOFOLLOW raises ELOOP on one)."""
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file; refusing to write")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        data = line.encode("utf-8")
        while data:
            n = os.write(fd, data)
            data = data[n:]
    finally:
        os.close(fd)


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
    """(stored input, original JSON length, truncated). Over the cap, every value is cut to an equal share of it
    (non-strings as their JSON text), so the dict keeps its keys and a giant Write content cannot bypass the cap."""
    inp = inp if isinstance(inp, dict) else {}
    raw = json.dumps(inp, ensure_ascii=False, default=str)
    if len(raw) <= cap:
        return inp, len(raw), False
    share = max(1, cap // max(1, len(inp)))
    out = {}
    for k, v in inp.items():
        text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        out[k] = text[:share] if len(text) > share else v
    return out, len(raw), True


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

class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.write_failures = 0                  # exposed: a failing store is visible, never silent
        self.last_failure: Optional[str] = None
        self._last_prune = 0.0
        self._prune_running = False

    def record_turn(self, conversation_id: str, **kw) -> bool:
        """Append one turn. Returns True when written. A failure is logged with its reason and counted, and NOT
        raised: this is the one place a failure must not propagate, because the user's request has already been
        answered by the backend and a local logging store must never turn that into an error."""
        if not enabled():
            return False
        try:
            lines = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in turn_records(**kw))
            with self._lock:
                ensure_dir(root())
                ensure_dir(sessions_dir())
                append_line(session_path(conversation_id), lines)
            return True
        except Exception as exc:  # deliberately broad: see the docstring
            with self._lock:
                self.write_failures += 1
                self.last_failure = f"{type(exc).__name__}: {exc}"
            logger.warning("compaction store: turn of conversation %s not recorded (%s); %d failure(s) so far",
                           str(conversation_id)[:8], self.last_failure, self.write_failures)
            return False
        finally:
            self.maybe_prune_async()

    def stats(self) -> dict[str, Any]:
        return {"enabled": enabled(), "write_failures": self.write_failures, "last_failure": self.last_failure}

    def maybe_prune_async(self, now: float | None = None) -> bool:
        """Start a background prune when the last one is over PRUNE_INTERVAL_S old; never blocks the caller."""
        if not enabled():
            return False
        now = time.time() if now is None else now
        with self._lock:
            if self._prune_running or now - self._last_prune < PRUNE_INTERVAL_S:
                return False
            self._prune_running, self._last_prune = True, now

        def run():
            try:
                prune_expired(now)
            except Exception:
                logger.exception("compaction store: prune failed")
            finally:
                with self._lock:
                    self._prune_running = False

        threading.Thread(target=run, daemon=True, name="cortados-compaction-prune").start()
        return True


def prune_expired(now: float | None = None) -> list[str]:
    """Delete session files older than the retention, then the oldest until the total fits the size cap. Returns
    the deleted file names. Only plain files ending in .jsonl directly under sessions/ are touched."""
    now = time.time() if now is None else now
    d = sessions_dir()
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        logger.warning("compaction store: %s is not a plain directory; not pruning", d)
        return []
    files = []
    for entry in os.scandir(d):
        if not entry.name.endswith(".jsonl"):
            continue
        est = entry.stat(follow_symlinks=False)
        if stat.S_ISREG(est.st_mode):
            files.append((est.st_mtime, est.st_size, entry.name))
    files.sort()
    removed: list[str] = []
    cutoff = now - retention_days() * 86400
    keep = []
    for mtime, size, name in files:
        if mtime < cutoff:
            _unlink(d / name, removed)
        else:
            keep.append((mtime, size, name))
    total, cap = sum(s for _, s, _ in keep), max_bytes()
    for mtime, size, name in keep:
        if total <= cap:
            break
        _unlink(d / name, removed)
        total -= size
    if removed:
        logger.info("compaction store: pruned %d session file(s)", len(removed))
    return removed


def _unlink(path: Path, removed: list[str]) -> None:
    try:
        os.unlink(path)
        removed.append(path.name)
    except FileNotFoundError:
        pass


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
    with open(p, encoding="utf-8") as f:
        for line in f:
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
