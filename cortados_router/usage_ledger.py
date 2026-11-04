"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools. A write never breaks or slows a turn: it runs off the request thread
(record_async), retries a busy/locked db within one end-to-end RETRY_S deadline instead of dropping the row, and failures are never raised. Consecutive failures are counted (`consecutive_failures()`) and
logged with their reason on the first and every LOG_EVERY-th one, so a ledger that keeps failing stays visible without flooding."""
from __future__ import annotations

import fcntl
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortados.usage_ledger")
TIMEOUT_S = 0.5          # one attempt's sqlite busy wait (capped by what is left of record()'s deadline)
# Reason: a busy db (a migration building the index, a view scan, a racing writer) is transient; dropping the row there
# lost turns, so record() retries. RETRY_S is ONE end-to-end deadline per record() call, from its start, over everything
# it does: the migrate lock wait, every sqlite busy wait and statement, and the retries. Reason it covers all of it:
# record_async's synchronous fallback runs on the request thread, and an unbounded step there (a blocking flock on
# usage.db.migrate held by another process) held a reply indefinitely. Past the deadline it is a real fault.
RETRY_S = 5.0
_RETRY_SLEEP_S = 0.05    # between retries and between non-blocking tries of the migrate lock
_PROGRESS_OPS = 10_000   # sqlite VM steps between deadline checks (as provider/usage.py's LEDGER_QUERY_S guard)
_SCHEMA = ("CREATE TABLE IF NOT EXISTS usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
           "cache_read_tokens INTEGER, cache_write_tokens INTEGER, lane TEXT)")
# Reason: the usage view (/v1/cortados/usage) scans this table by time window; with no index every scan read the whole
# table, and under the default rollback journal a long read held a SHARED lock that made record()'s commit time out (a
# 2M-row ledger lost writes). The index bounds the scan; WAL lets a reader and this writer run at once.
_TS_INDEX = "CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts)"
LOG_EVERY = 100          # a steady failure (full disk, read-only home) repeats every turn: one line per 100 turns stays readable
_fail_lock = threading.Lock()
_consecutive_failures = 0


def db_path() -> Path:
    from cortados_router import paths
    return paths.state_dir() / "usage.db"


def consecutive_failures() -> int:
    """Writes that failed in a row since the last success (0 when the ledger is healthy)."""
    with _fail_lock:
        return _consecutive_failures


def _ensure_lane_column(c: sqlite3.Connection) -> None:
    """Add `lane` to a ledger from the Hermes plugin (old rows stay lane-less). Two processes can migrate at once: the
    check and the ALTER run under BEGIN IMMEDIATE (one writer at a time), and a duplicate-column error from a racer that
    still got there first means the column exists, which is the goal."""
    if "lane" in {r[1] for r in c.execute("PRAGMA table_info(usage)")}:
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        if "lane" not in {r[1] for r in c.execute("PRAGMA table_info(usage)")}:
            try:
                c.execute("ALTER TABLE usage ADD COLUMN lane TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _ensure_wal(c: sqlite3.Connection) -> bool:
    """Switch the ledger to WAL (the mode is persistent in the file). The switch needs the file to itself; if a reader
    holds it right now the switch fails, which is logged and retried on the next record, and this row is still written
    in the old mode: losing a turn to a migration step is worse than one more try."""
    if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
        return True
    try:
        mode = c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.OperationalError as exc:
        logger.warning("usage ledger: WAL switch deferred (%s); retried on the next write", str(exc)[:120])
        return False
    if str(mode).lower() != "wal":
        logger.warning("usage ledger: journal_mode stayed %r (WAL refused); retried on the next write", mode)
        return False
    return True


def _migrated(c: sqlite3.Connection) -> bool:
    if str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        return False
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='usage_ts'").fetchone():
        return False
    return "lane" in {r[1] for r in c.execute("PRAGMA table_info(usage)")}


def _migrate_lock(lf, deadline: float) -> bool:
    """The exclusive migrate lock, tried without blocking every _RETRY_SLEEP_S until `deadline`. False: still held."""
    while True:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() + _RETRY_SLEEP_S > deadline:
                return False
            time.sleep(_RETRY_SLEEP_S)


def _migrate(c: sqlite3.Connection, p: Path, deadline: float) -> None:
    """One-time setup: WAL first, then the table, the ts index and the lane column. Order matters: building the index
    on a large rollback-journal ledger held the write lock for the whole build and concurrent records failed; in WAL
    they only queue behind it. Reason for the lock file: without it every writer that saw "not migrated" raced to
    switch the mode and build the index; under an exclusive flock on usage.db.migrate exactly one migrates and the
    others wait, then see it done (re-checked under the lock). The wait is bounded by record()'s deadline: a lock still
    held then skips the migration for this row, which is still written if the table already has the lane column (WAL
    and the index are optimisations; the lane is the schema), and fails otherwise."""
    if _migrated(c):
        return
    with open(str(p) + ".migrate", "a") as lf:
        if not _migrate_lock(lf, deadline):           # released on close
            if "lane" in {r[1] for r in c.execute("PRAGMA table_info(usage)")}:
                logger.warning("usage ledger: migration lock held elsewhere past the deadline; migration skipped for "
                               "this row, retried on the next write")
                return
            raise sqlite3.OperationalError("usage ledger: no lane column, and the migration lock is held elsewhere")
        if _migrated(c):
            return
        if _ensure_wal(c):
            # not built in the rollback journal: that build is what blocked the writers; the next record retries
            c.execute(_TS_INDEX)
            c.commit()
        _ensure_lane_column(c)


def _busy(exc: Exception) -> bool:
    m = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in m or "busy" in m)


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _write(p: Path, row: tuple, deadline: float) -> None:
    """One attempt, bounded by `deadline`: the busy wait is capped by the time left, and any statement still running at
    the deadline is interrupted (OperationalError "interrupted", not busy, so record() stops there)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise sqlite3.OperationalError("usage ledger: write deadline (RETRY_S) passed")
    c = sqlite3.connect(str(p), timeout=min(TIMEOUT_S, left))
    c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_OPS)   # non-zero: interrupt
    try:
        c.execute(_SCHEMA)        # IF NOT EXISTS: a fresh file needs the table before _migrated() can read it
        c.commit()
        _migrate(c, p, deadline)
        c.execute("INSERT INTO usage (ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane) "
                  "VALUES (?,?,?,?,?,?,?,?)", row)
        c.commit()
    finally:
        c.close()


def record(lane: str, model: str, usage: dict | None, *, ts: float | None = None, path: Path | None = None,
           context: str | None = None) -> bool:
    """Append one turn. `lane` is the scarcity lane (claude, claude#b, codex); its base is the runtime. Returns False on failure.
    `context` (e.g. "search lane, run <id>") marks a write whose loss must be seen every time: its failure is logged at error
    level with the lane and that context on EVERY occurrence, besides counting toward consecutive_failures(). Logged
    once, when the deadline (RETRY_S from the start of this call, covering every step) passes or on the first non-busy
    error."""
    global _consecutive_failures
    deadline = time.monotonic() + RETRY_S
    try:
        u = usage or {}
        runtime = (lane or "").split("#", 1)[0]
        row = (ts or time.time(), runtime, str(model or ""), _int(u.get("input_tokens")), _int(u.get("output_tokens")),
               _int(u.get("cache_read_input_tokens") or u.get("cache_read_tokens")),
               _int(u.get("cache_creation_input_tokens") or u.get("cache_write_tokens")), lane)
        p = path or db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                _write(p, row, deadline)
                break
            except sqlite3.OperationalError as exc:
                if not _busy(exc) or time.monotonic() + _RETRY_SLEEP_S > deadline:
                    raise
                time.sleep(_RETRY_SLEEP_S)
    except Exception as exc:                            # never raised: the turn already succeeded
        with _fail_lock:
            _consecutive_failures += 1
            n = _consecutive_failures
        if context:
            logger.error("usage ledger write failed for %s on lane %s (%s: %s); this spend is not recorded", context, lane,
                         type(exc).__name__, str(exc)[:120])
        elif n == 1 or n % LOG_EVERY == 0:
            logger.warning("usage ledger write failed (%s: %s); %d consecutive failures, logged on the first and every %d",
                           type(exc).__name__, str(exc)[:120], n, LOG_EVERY)
        return False
    with _fail_lock:
        if _consecutive_failures:
            logger.info("usage ledger writes recovered after %d failures", _consecutive_failures)
        _consecutive_failures = 0
    return True


def record_async(lane: str, model: str, usage: dict | None, *, context: str | None = None) -> threading.Thread | None:
    """Off the request thread, so even the sqlite timeout never delays a reply. Returns the started thread (tests join it),
    or None when the worker could not be started (RuntimeError "can't start new thread" under thread exhaustion) and the
    row was written synchronously instead. Reason for the fallback: by now the quota is already spent, so losing the row
    to a failed thread start is worse than holding this one reply for record()'s end-to-end deadline (RETRY_S). The
    synchronous write keeps the same retries, failure counting and `context` ERROR logging, and never raises."""
    try:
        t = threading.Thread(target=record, args=(lane, model, usage), kwargs={"context": context},
                             name="cortados-usage-ledger", daemon=True)
        t.start()
    except Exception as exc:
        logger.warning("usage ledger: worker thread did not start (%s: %s); writing the row for lane %s%s synchronously",
                       type(exc).__name__, str(exc)[:120], lane, f" ({context})" if context else "")
        record(lane, model, usage, context=context)
        return None
    return t
