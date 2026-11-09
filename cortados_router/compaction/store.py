"""Append-only local content store for the provider's conversations (phase 1, shadow only).

Off by default; on when CORTADOS_COMPACTION_STORE=1. The owner approved keeping conversation CONTENT on the provider
host (the Air) for a short time, so compaction labels can be harvested from what later turns actually reused. Nothing
here changes what is sent to a backend: the provider calls `record_turn` after a turn's reply is known, and only reads
from the request it already parsed.

Layout: $CORTADOS_HOME/compaction/sessions/<safe-id>-<sha12>.jsonl, directories 0700, files 0600, every open with
O_NOFOLLOW (a planted symlink is refused, not followed) and refused unless fstat shows a regular file with exactly one
hard link (a planted hard link to an outside file is refused, not written through). One JSON object per line:
  {"kind": "turn", "ts", "source", "meta": {run_id, session, ...}}      one per provider turn, first
  {"kind": "tool_result", "tool_use_id", "text", "chars", "truncated", "is_error"}
  {"kind": "message", "role": "user" | "assistant", "text"}
  {"kind": "tool_call", "tool_use_id", "name", "input"}
`read_session` turns a file back into the shared `events.Session`; it accepts only a path under sessions/.

Shadow decisions (prune.py) go to $CORTADOS_HOME/compaction/decisions/decisions-<UTC day>.<n>.jsonl, a new shard
per day and whenever the current one passes DECISIONS_SHARD_MAX_BYTES, so no single file grows without bound.

Retention covers both directories: files untouched for CORTADOS_COMPACTION_RETENTION_DAYS (default 14) are deleted,
then the oldest files go until the total is under CORTADOS_COMPACTION_MAX_MB (default 2048). `prune_expired` runs at
provider start and at most hourly after, on the store's writer thread.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import re
import stat
import sys
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

SESSIONS_DIR = ("compaction", "sessions")
DECISIONS_DIR = ("compaction", "decisions")
# Why 16 MiB: a decisions shard is the unit retention deletes and the harvest reads whole. 16 MiB holds a few hundred
# closed conversations, so one deletion never removes more than that and no single file grows without bound.
DECISIONS_SHARD_MAX_BYTES = 16 * 1024 * 1024

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


def decisions_dir() -> Path:
    return root() / "decisions"


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
# Every file operation is anchored to directory fds opened one component at a time from "/" down through
# $CORTADOS_HOME, each with O_NOFOLLOW + O_DIRECTORY and an fstat check, so a symlinked `compaction/` or `sessions/`,
# a symlinked home or a symlinked ANCESTOR of the home can never redirect a write, a read or a deletion outside the
# store (the only symlinks crossed are the OS aliases in _TRUSTED_ALIASES). Files are opened relative to those fds with
# O_NOFOLLOW | O_NONBLOCK (a FIFO planted at a session path cannot block the open) and refused unless fstat says
# they are regular files with exactly one hard link (a hard link IS a regular file, so S_ISREG alone let a session
# name linked to an outside file be appended to and chmod'ed).

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _home() -> str:
    return getenv("CORTADOS_HOME") or os.path.expanduser("~/.cortados")


def _open_dir(name: str, parent_fd: Optional[int], *, create: bool, own: bool) -> int:
    """Open one directory component without following a symlink; when `create`, a missing one is made 0700, and
    `own` dirs (ours) are forced 0700. mkdir runs only after the open found nothing: an existing ancestor on a
    read-only volume (macOS's sealed "/") would answer mkdir with EROFS, not EEXIST."""
    def _open() -> int:
        return os.open(name, _DIR_FLAGS) if parent_fd is None else os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        fd = _open()
    except FileNotFoundError:
        if not create:
            raise
        try:
            if parent_fd is None:
                os.mkdir(name, 0o700)
            else:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass                                # created concurrently: the open below still checks what it is
        fd = _open()
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


# Symlinks at the top of the filesystem that the OS itself installs, and nothing else. Why a fixed list: O_NOFOLLOW on
# an absolute path protects only its LAST component, so a symlinked ancestor of $CORTADOS_HOME (/x/link -> /elsewhere)
# used to redirect writes, reads and retention deletions outside the store. Every ancestor is now opened one component
# at a time with O_NOFOLLOW; these OS aliases are the only symlinks crossed, and only after checking that the link is
# still the root-owned one pointing where the OS points it (macOS: tempfile.gettempdir() lives under /var/folders).
# Any other symlinked ancestor is refused with OSError; there is no fallback that follows it.
_TRUSTED_ALIASES: dict[str, str] = ({"var": "private/var", "tmp": "private/tmp", "etc": "private/etc"}
                                    if sys.platform == "darwin" else {})


def _home_components() -> list[str]:
    """$CORTADOS_HOME as components below "/", lexically normalized, with a trusted OS alias at its top replaced by
    its target. Raises OSError when the top component is a symlink that is not the expected OS alias."""
    comps = [c for c in os.path.normpath(os.path.abspath(_home())).split(os.sep) if c]
    if comps and comps[0] in _TRUSTED_ALIASES:
        top, target = comps[0], _TRUSTED_ALIASES[comps[0]]
        try:
            st = os.lstat(os.sep + top)
            link = os.readlink(os.sep + top) if stat.S_ISLNK(st.st_mode) else None
        except FileNotFoundError:
            link, st = None, None
        if link is not None:
            if st.st_uid != 0 or link.rstrip(os.sep) not in (target, os.sep + target):
                raise OSError(f"/{top} is a symlink to {link!r} owned by uid {st.st_uid}, not the OS alias "
                              f"-> {target}; refusing to cross it")
            comps = target.split("/") + comps[1:]
    return comps


def open_store_dir(*parts: str, create: bool) -> int:
    """An fd on $CORTADOS_HOME/<parts...>. Every component from "/" down, the home's ancestors included, is opened
    relative to its parent's fd with O_NOFOLLOW + O_DIRECTORY, so a symlink anywhere on the path (an ancestor, the
    home, compaction/, sessions/) raises OSError; only the OS aliases in _TRUSTED_ALIASES are crossed. Missing
    ancestors and the home are created 0700 when `create`; otherwise FileNotFoundError. The caller closes it."""
    fd = _open_dir(os.sep, None, create=False, own=False)
    try:
        for c in _home_components():
            nfd = _open_dir(c, fd, create=create, own=False)
            os.close(fd)
            fd = nfd
        for p in parts:
            nfd = _open_dir(p, fd, create=create, own=True)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_file(name: str, dir_fd: int, flags: int, mode: int = 0o600) -> int:
    """Open a store file relative to an anchored dir fd and refuse anything that is not a regular file with ONE
    name. Why the link count: O_NOFOLLOW and S_ISREG do not see a hard link (it IS the regular file), so a session
    name hard-linked to a file outside the store was appended to and chmod'ed 0644 -> 0600 through it. The check
    runs before the chmod, the write or the read; a file this store created has exactly one name."""
    fd = os.open(name, flags | _FILE_FLAGS, mode, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{name} is not a regular file; refusing it")
        if st.st_nlink != 1:
            raise OSError(f"{name} has {st.st_nlink} hard links; a store file has one name, refusing it")
        if (flags & os.O_CREAT) and stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        if flags & (os.O_WRONLY | os.O_RDWR) == 0:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _append_at(dfd: int, name: str, data: str) -> None:
    fd = _open_file(name, dfd, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        buf = data.encode("utf-8")
        while buf:
            n = os.write(fd, buf)
            buf = buf[n:]
    finally:
        os.close(fd)


def append_in(parts: tuple, name: str, data: str) -> None:
    """Append `data` to $CORTADOS_HOME/<parts>/<name> (0600), never through a symlink, a FIFO or a non-file."""
    dfd = open_store_dir(*parts, create=True)
    try:
        _append_at(dfd, name, data)
    finally:
        os.close(dfd)


SHARD_NAME = re.compile(r"^decisions-(\d{4}-\d{2}-\d{2})\.(\d+)\.jsonl$")     # (UTC day, sequence)


def append_decisions(data: str, now: float | None = None) -> str:
    """Append decision rows to the current shard, decisions/decisions-<UTC day>.<n>.jsonl, and return its name. A
    new day starts shard 0; the shard rolls to n+1 once it is DECISIONS_SHARD_MAX_BYTES or more, so retention can
    delete decisions by age and size in bounded pieces. Runs on the writer thread (the listing and the append are
    serialized with retention)."""
    day = time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))
    dfd = open_store_dir(*DECISIONS_DIR, create=True)
    try:
        seqs = [int(m.group(2)) for m in map(SHARD_NAME.match, os.listdir(dfd)) if m and m.group(1) == day]
        seq = max(seqs, default=0)
        name = f"decisions-{day}.{seq}.jsonl"
        if seqs:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and st.st_size >= DECISIONS_SHARD_MAX_BYTES:
                name = f"decisions-{day}.{seq + 1}.jsonl"
        _append_at(dfd, name, data)
    finally:
        os.close(dfd)
    return name


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
# Why 64 MiB: the job count alone does not bound memory (1024 turns of several MB each would). The queue only fills
# when the disk is stuck, and then it must not grow toward the RAM the live conversations need; 64 MiB is about
# 3000 capped Claude Code results and drains in well under a second on a healthy disk. Each job is admitted with its
# serialized size, so the budget counts the bytes the queue really retains.
QUEUE_MAX_BYTES = 64 * 1024 * 1024
# Why 5 s: a turn append is sub-millisecond and one conversation's shadow decisions take tens of milliseconds, so
# 5 s drains a thousand queued jobs on a healthy disk; a stuck disk must not hold the provider's exit longer than
# that. What is still queued after it is counted lost, never silently discarded.
SHUTDOWN_DRAIN_S = 5.0

_STOP = object()        # queue sentinel: the writer exits its get() on it instead of blocking forever at shutdown


class Store:
    """All store I/O (session appends, shadow decisions, retention) runs on ONE background writer thread fed by a
    bounded queue, so it is serialized (retention can never race a write) and the request path only enqueues. Every
    failure (I/O, a full queue, an over-budget record, a thread that cannot start, a store that is shut down) is
    logged with its reason and counted, never raised: this is the one place a failure must not propagate, because
    the user's request has already been answered by the backend and a local logging store must never turn that into
    an error or stall turn finalization.

    Admission is bounded twice, by job count (QUEUE_MAX) and by retained bytes (QUEUE_MAX_BYTES): a turn is
    serialized on the request path, so its size is known and capped before it is queued, and the closure holds only
    the capped text, never the request's own content. Lifecycle: `shutdown` (provider exit, atexit) refuses new
    jobs, drains what was accepted for SHUTDOWN_DRAIN_S, stops the writer and counts the rest in `lost`.
    Locks: `_lock` guards counters, the throttle and the thread handle; the queue's own mutex guards admission,
    `_closed`, `_queued_bytes` and `_inflight`; the writer dequeues a job and sets `_inflight` in one hold of it
    (`_next_job`), so every accepted job is always either queued or in flight. `_lock` is taken before the queue
    mutex, never after it."""

    def __init__(self, queue_max: int = QUEUE_MAX, queue_max_bytes: int = QUEUE_MAX_BYTES):
        self._lock = threading.Lock()
        self._q: "queue.Queue" = queue.Queue(maxsize=queue_max)
        self._queue_max_bytes = int(queue_max_bytes)
        self._queued_bytes = 0                  # serialized bytes held by queued and in-flight jobs (queue mutex)
        self._inflight: Optional[str] = None    # the job the writer is executing (queue mutex)
        self._closed = False                    # set once by shutdown (queue mutex)
        self._thread: Optional[threading.Thread] = None
        self.write_failures = 0                 # I/O or job failures inside the writer
        self.dropped = 0                        # jobs refused at admission: full queue, byte budget, shut down
        self.schedule_failures = 0              # the writer thread could not be started, or a record could not be built
        self.lost = 0                           # jobs accepted but never run because the store shut down
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
        with self._q.mutex:
            queued, qbytes, closed = self._q._qsize(), self._queued_bytes, self._closed
        return {"enabled": enabled(), "write_failures": self.write_failures, "dropped": self.dropped,
                "schedule_failures": self.schedule_failures, "lost": self.lost, "last_failure": self.last_failure,
                "queued": queued, "queued_bytes": qbytes, "closed": closed}

    # -- the writer
    def _ensure_writer(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._run, daemon=True, name="cortados-compaction-writer")
            t.start()
            self._thread = t

    def _next_job(self):
        """Block until there is work, then take it AND register it as in flight in ONE hold of the queue mutex.
        Why one hold: Queue.get() releases the mutex between removing a job and returning it, so a writer paused
        there left a job that was neither queued nor in flight, and shutdown() counted it as neither (lost=0 for a
        job that never ran). Returns None when the store is shut down and nothing is left (the writer exits)."""
        with self._q.not_empty:
            while not self._q._qsize():
                if self._closed:
                    return None                 # shut down and nothing left: do not wait forever
                self._q.not_empty.wait()
            item = self._q._get()
            self._q.not_full.notify()
            if item is not _STOP:
                self._inflight = item[0]
            return item

    def _run(self) -> None:
        while True:
            item = self._next_job()
            if item is None:
                return
            if item is _STOP:
                self._q.task_done()
                return
            name, job, nbytes = item
            try:
                job()
            except Exception as exc:  # deliberately broad: see the class docstring
                self._count("write_failures", f"{name} failed: {type(exc).__name__}: {exc}")
            finally:
                with self._q.mutex:
                    self._queued_bytes -= nbytes
                    self._inflight = None
                self._q.task_done()

    def submit(self, name: str, job, nbytes: int = 0) -> bool:
        """Queue `job`, which retains `nbytes` of serialized content, for the writer; never blocks and never raises.
        False when it was not queued (full queue, byte budget, shut down, writer start failure), each counted."""
        try:
            with self._q.mutex:
                closed = self._closed
            if not closed:
                self._ensure_writer()
            # The queue's nonblocking put protocol with the closed and budget checks in the SAME critical section:
            # nothing can be admitted behind shutdown's drain, and the byte count is exact.
            with self._q.not_full:
                if self._closed:
                    why = "store shut down"
                elif self._q.maxsize > 0 and self._q._qsize() >= self._q.maxsize:
                    why = f"writer queue full ({self._q.maxsize} jobs)"
                elif self._queued_bytes + nbytes > self._queue_max_bytes:
                    why = (f"writer queue byte budget ({self._queue_max_bytes} bytes) would be exceeded: "
                           f"{self._queued_bytes} held, {nbytes} offered")
                else:
                    self._q._put((name, job, nbytes))
                    self._q.unfinished_tasks += 1
                    self._queued_bytes += nbytes
                    self._q.not_empty.notify()
                    return True
            self._count("dropped", f"{name} dropped: {why}")
        except Exception as exc:  # thread start failure and anything else: contained, counted
            self._count("schedule_failures", f"{name} not scheduled: {type(exc).__name__}: {exc}")
        return False

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until the queue is drained (tests, shutdown). True when drained."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self._q.unfinished_tasks

    def shutdown(self, timeout: Optional[float] = None) -> int:
        """Refuse new jobs, give the writer `timeout` (SHUTDOWN_DRAIN_S) to finish what was accepted, stop it, then
        count exactly once what did not run: whatever is still queued plus a job the writer is still stuck in.
        Returns that number (also added to `lost`). Idempotent; a second call only re-counts nothing."""
        timeout = SHUTDOWN_DRAIN_S if timeout is None else timeout
        until = time.monotonic() + timeout
        with self._lock:
            with self._q.mutex:
                self._closed = True             # from here on submit() drops; the queue holds only earlier work
                self._q.not_empty.notify_all()  # an idle writer waiting for work sees `closed` and exits
            t = self._thread
        self.flush(max(0.0, until - time.monotonic()))
        if t is not None and t.is_alive():
            with self._q.mutex:                 # the sentinel bypasses admission: the writer must see it
                self._q._put(_STOP)
                self._q.unfinished_tasks += 1
                self._q.not_empty.notify()
            t.join(max(0.0, until - time.monotonic()))
        n, names = 0, []
        with self._lock:
            with self._q.mutex:
                while self._q._qsize():
                    it = self._q._get()
                    self._q.unfinished_tasks -= 1
                    if it is not _STOP:
                        n += 1
                        names.append(it[0])
                        self._queued_bytes -= it[2]
                stuck = self._inflight if (t is not None and t.is_alive()) else None
                if stuck is not None:
                    n += 1
                    names.append(stuck + " (writer still in it)")
                    self._inflight = None       # counted once; a second shutdown() must not count it again
                if not self._q.unfinished_tasks:
                    self._q.all_tasks_done.notify_all()
                self.lost += n
                total = self.lost
                if n:
                    self.last_failure = f"{n} job(s) lost at shutdown"
        if n:
            logger.warning("compaction store: %d job(s) lost at shutdown after %.1fs (lost total: %d): %s", n,
                           timeout, total, "; ".join(names[:5]) + (" ..." if len(names) > 5 else ""))
        return n

    # -- jobs
    def record_turn(self, conversation_id: str, **kw) -> bool:
        """Queue one turn for the writer. Returns True when queued. The records are built and serialized HERE, on
        the request path, so the turn's size is known and capped before admission and the queued closure retains the
        capped text only, never the request's content; building is bounded work (every result and input is cut to
        the policy cap first). Never raises: a record that cannot be built is counted as a scheduling failure."""
        try:
            if not enabled():
                return False
            cid = str(conversation_id)
            lines = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in turn_records(**kw))
            name = session_file_name(cid)
            ok = self.submit(f"turn of conversation {cid[:8]}", lambda: append_in(SESSIONS_DIR, name, lines),
                             nbytes=len(lines.encode("utf-8")))
        except Exception as exc:  # contained like every scheduling failure
            self._count("schedule_failures", f"turn of conversation {str(conversation_id)[:8]} not built: "
                                             f"{type(exc).__name__}: {exc}")
            ok = False
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


RETAINED_DIRS = (SESSIONS_DIR, DECISIONS_DIR)


def prune_expired(now: float | None = None) -> list[str]:
    """Delete session files and decisions shards older than the retention, then the oldest of either until their
    total fits the size cap (one budget for the whole store: decisions must not grow outside it). Returns the
    deleted files as "<dir>/<name>". Runs on the writer thread (serialized with appends). Everything is relative to
    dir fds opened through verified, non-symlink components; only regular *.jsonl files are touched, and each
    candidate is re-stat'ed just before unlinking (same inode, still past its criterion) so a changed file is left
    alone."""
    now = time.time() if now is None else now
    dfds: dict[tuple, int] = {}
    removed: list[str] = []
    try:
        files = []
        for parts in RETAINED_DIRS:
            try:
                dfds[parts] = dfd = open_store_dir(*parts, create=False)
            except FileNotFoundError:
                continue
            for name in os.listdir(dfd):
                if not name.endswith(".jsonl"):
                    continue
                try:
                    est = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(est.st_mode):
                    files.append((est.st_mtime, est.st_size, parts, name, est.st_ino))
        files.sort()
        cutoff = now - retention_days() * 86400
        keep = []
        for mtime, size, parts, name, ino in files:
            if mtime < cutoff:
                _unlink_if(dfds[parts], parts, name, ino, lambda st: st.st_mtime < cutoff, removed)
            else:
                keep.append((mtime, size, parts, name, ino))
        total, cap = sum(k[1] for k in keep), max_bytes()
        for mtime, size, parts, name, ino in keep:
            if total <= cap:
                break
            if _unlink_if(dfds[parts], parts, name, ino, lambda st, m=mtime: st.st_mtime <= m, removed):
                total -= size
    finally:
        for dfd in dfds.values():
            os.close(dfd)
    if removed:
        logger.info("compaction store: pruned %d file(s)", len(removed))
    return removed


def _unlink_if(dfd: int, parts: tuple, name: str, ino: int, still, removed: list[str]) -> bool:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_ino != ino or not still(st):
            return False
        os.unlink(name, dir_fd=dfd)
        removed.append(f"{parts[-1]}/{name}")
        return True
    except FileNotFoundError:
        return False


def session_file_of(path: str | os.PathLike) -> str:
    """The session file name `path` denotes, or ValueError. Only a path that lexically names a file directly under
    sessions/ is accepted (both sides normalized the same way, no symlink followed to decide); the caller then opens
    THAT name through the anchored dir fds. There is no fallback for other paths: an alias such as
    sessions/../sessions/x.jsonl or a path outside the store would otherwise be opened directly and follow
    intermediate symlinks."""
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    want = os.path.normpath(os.path.abspath(os.fspath(sessions_dir())))
    head, name = os.path.split(p)
    if head != want or not name or name in (".", "..") or not name.endswith(".jsonl"):
        raise ValueError(f"compaction store: {os.fspath(path)!r} is not a session file under {want}; refusing it")
    return name


def read_session(path: str | os.PathLike) -> Session:
    """Read one session file into events.Session. Its source is the conversation's wire as the first turn header
    recorded it ("claude-code" / "codex"; "provider" when there is none), so token estimates use the right ratio.
    Calls without a result are interrupted. `path` must be a file under sessions/ (session_file_of); it is read
    through the anchored dir fds, never opened by path."""
    name = session_file_of(path)
    p = Path(name)
    sid = p.stem
    messages: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    turn_ts: Optional[str] = None
    source: Optional[str] = None
    text = read_in(SESSIONS_DIR, name)
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


def _at_exit() -> None:
    """Interpreter exit: daemon threads are frozen at teardown, so without this drain the turns and decisions
    accepted in the last moments (Registry.drop enqueues decisions right before exit) would vanish uncounted."""
    STORE.shutdown()


atexit.register(_at_exit)
