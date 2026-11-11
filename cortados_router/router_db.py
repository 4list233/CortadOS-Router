"""router.db access shared by the writer (core.log_route & co.) and the in-process readers (usage, tasks, live, core).

Two things live here, both stdlib and import-free so the provider views can use them without loading core's tables:

1. reader(): how a process that also WRITES router.db must open it for reading. Not `?mode=ro`. Issue #99: the
   provider on the Air answered `503 routing failed` because core.log_route raised `sqlite3.OperationalError: disk I/O
   error`. SQLite's extended code was SQLITE_IOERR_LOCK (3850) with errno EBADF: router.db is a rollback-journal
   (journal_mode=delete) database, SQLite's unix VFS keeps ONE lock state per file per process (POSIX locks are
   per-process), and once the same process also holds `mode=ro` connections (opened O_RDONLY) the writer's lock
   transition can fail with EBADF. A stress run on a throwaway db (1 writer, 3 readers, same process, 4 s) gave
   hundreds of writer failures with `mode=ro` readers and none with readers opened `mode=rw` + `PRAGMA query_only`,
   with readers in another process, or with a WAL database. The dashboard / live / pulse polls (usage.collect,
   tasks.collect, live) made such readers frequent in the provider process. `mode=rw` never creates the file (a
   missing db is still an error) and query_only refuses every write on the connection.

2. The route-log error counter: a failure to LOG a route must never fail the routed request, but it must not be
   silent either. core counts every failed write attempt here; the usage and live views publish stats().
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortados.router_db")

LOG_EVERY_S = 60.0          # at most one warning line per minute; the suppressed count rides on the next one


def reader(path: Path | str, timeout: float) -> sqlite3.Connection:
    """A read-only connection to an EXISTING router.db that is safe next to this process's own writer (see above).
    Raises sqlite3.OperationalError when the file is missing (mode=rw never creates it) or unreadable."""
    # Path.as_uri() percent-encodes '#', '?' and '%' (a string-built 'file:' URI would read them as a fragment / query)
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=timeout)
    try:
        c.execute("PRAGMA query_only=ON")
    except BaseException:
        c.close()
        raise
    return c


_lock = threading.Lock()
_stats: dict[str, Any] = {}
_last_log = [0.0]
_suppressed = [0]


def _reset() -> None:
    """Tests: back to a process that has seen no route-log error."""
    with _lock:
        _stats.clear()
        _stats.update(count=0, lost=0, recovered=0, last_error_type=None, last_error_at=None, last_op=None)
        _last_log[0] = 0.0
        _suppressed[0] = 0


_reset()


def note_error(op: str, exc: BaseException, *, final: bool) -> None:
    """One failed write attempt of `op` (log_route, log_execution, log_outcome). final: no retry follows (the write is
    lost for log_route; re-raised to its caller for the others). Logs at most once per LOG_EVERY_S, type and message only
    (sqlite messages carry no path or row data)."""
    now = time.time()
    with _lock:
        _stats["count"] += 1
        _stats["lost"] += 1 if final else 0
        _stats["last_error_type"] = type(exc).__name__
        _stats["last_error_at"] = round(now, 3)
        _stats["last_op"] = op
        if now - _last_log[0] < LOG_EVERY_S:
            _suppressed[0] += 1
            return
        _last_log[0], suppressed, _suppressed[0] = now, _suppressed[0], 0
        total = _stats["count"]
    logger.warning("router.db %s failed (%s: %s; %s)%s; %d route-log errors since start", op, type(exc).__name__,
                   str(exc)[:120], "given up" if final else "retrying once",
                   f"; {suppressed} more not logged" if suppressed else "", total)


def note_recovered(op: str) -> None:
    """The retry of `op` after a failed attempt succeeded."""
    with _lock:
        _stats["recovered"] += 1


def stats() -> dict[str, Any]:
    """{count: failed attempts, lost: writes given up, recovered: retries that succeeded, last_error_type, last_error_at
    (epoch s), last_op} for this process since start."""
    with _lock:
        return dict(_stats)
