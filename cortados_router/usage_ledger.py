"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools. A write never breaks or slows a turn: it runs off the request thread
(record_async), retries a busy/locked db within one end-to-end RETRY_S deadline instead of dropping the row, and failures are never raised. Consecutive failures are counted (`consecutive_failures()`) and
logged with their reason on the first and every LOG_EVERY-th one, so a ledger that keeps failing stays visible without flooding.

No row is dropped because the db could not take it in time: a row record() cannot write by its deadline, for any reason
(SQLite admits one writer, so a long index build or any other writer can hold the db past every deadline), is appended
to SPOOL next to usage.db and replayed into the db later (see _spool / _replay). A row is lost only if that append fails
too, and that is logged as such."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
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
# Reason: on an oversized legacy ledger the index build can outlast any record() deadline; interrupted, sqlite rolls it
# back and the next call restarts it from zero, so a foreground-only build never finishes. A record() tries it for at
# most this long (or half of what is left of its deadline) and otherwise hands it to the background migration below.
INDEX_FOREGROUND_S = 1.0
# The background migration's retry backoff: first wait, doubling, capped (a held lock or a busy db is transient).
_BG_BACKOFF_S, _BG_BACKOFF_MAX_S = 1.0, 60.0
_bg_lock = threading.Lock()
_bg_started: set[str] = set()   # ledger paths whose background migration thread this process has started
SPOOL = "usage-spool.jsonl"
_SPOOL_MAX_LINE = 4096   # one os.write per row under PIPE_BUF-sized lines: an O_APPEND write this small is not interleaved
# Replay idempotency: the uids of spooled rows already inserted, so a crash between the insert and the spool's removal
# cannot count a row twice. A side table, not a column: main's usage schema stays as unitcost.learn and the usage view
# read it, and no index has to be built on the (possibly huge) usage table for it.
_APPLIED = "CREATE TABLE IF NOT EXISTS usage_spool_applied (uid TEXT PRIMARY KEY)"
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


def _index_present(c: sqlite3.Connection) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='usage_ts'").fetchone())


def _build_index(c: sqlite3.Connection) -> None:
    """The ts index build: the one migration step whose length grows with the ledger."""
    c.execute(_TS_INDEX)
    c.commit()


def _background_migrate(p: Path) -> None:
    """The full migration with no record() deadline: blocking migrate lock, no progress handler, retried with backoff
    until done. Runs on the one daemon thread _start_background_migration starts per ledger path."""
    wait = _BG_BACKOFF_S
    while True:
        try:
            c = sqlite3.connect(str(p), timeout=30.0)
            try:
                with open(str(p) + ".migrate", "a") as lf:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)   # off every request thread: blocking is fine here
                    if not _migrated(c):
                        if not _ensure_wal(c):
                            raise sqlite3.OperationalError("WAL switch deferred")
                        _ensure_lane_column(c)
                        _build_index(c)
                    if not _migrated(c):
                        raise sqlite3.OperationalError("still not migrated after a full pass")
                    # the rows spooled while the build held the writer lock, still under the migrate lock
                    n = _replay_locked(p, None)
                logger.info("usage ledger: background migration of %s complete; %d spooled row(s) replayed", p, n)
                return
            finally:
                c.close()
        except Exception as exc:
            logger.warning("usage ledger: background migration of %s failed (%s: %s); retried in %.0fs", p,
                           type(exc).__name__, str(exc)[:120], wait)
            time.sleep(wait)
            wait = min(wait * 2, _BG_BACKOFF_MAX_S)


def _start_background_migration(p: Path) -> None:
    """Start the background migration for `p` once per process. A thread that cannot start is logged and forgotten, so
    a later record() tries again."""
    key = str(p)
    with _bg_lock:
        if key in _bg_started:
            return
        _bg_started.add(key)
    try:
        threading.Thread(target=_background_migrate, args=(p,), name="cortados-usage-ledger-migrate", daemon=True).start()
        logger.warning("usage ledger: the ts index build does not fit a write's deadline; it continues in the background")
    except Exception as exc:
        with _bg_lock:
            _bg_started.discard(key)
        logger.warning("usage ledger: background migration thread did not start (%s: %s); retried on the next write",
                       type(exc).__name__, str(exc)[:120])


def _migrate(c: sqlite3.Connection, p: Path, deadline: float) -> bool:
    """One-time setup: WAL first, then the lane column, then the ts index. Order matters: building the index
    on a large rollback-journal ledger held the write lock for the whole build and concurrent records failed; in WAL
    they only queue behind it. The lane column (a fast ALTER, and the only step the row itself needs) comes before the
    index, and the index build gets at most INDEX_FOREGROUND_S here: one that does not fit is interrupted and rolled
    back, handed to the background migration, and this row is still written. Reason for the lock file: without it every writer that saw "not migrated" raced to
    switch the mode and build the index; under an exclusive flock on usage.db.migrate exactly one migrates and the
    others wait, then see it done (re-checked under the lock). The wait is bounded by record()'s deadline: a lock still
    held then skips the migration for this row, which is still written if the table already has the lane column (WAL
    and the index are optimisations; the lane is the schema), and fails otherwise. With the lane already there, the lock
    is tried once, not waited for: only optimisations remain, and the holder may be a long background build.
    Returns True when the index build must go to the background: the caller starts it only after its row is committed,
    so the background build (which holds the writer lock) cannot hold up the very row that triggered it."""
    if _migrated(c):
        return False
    has_lane = "lane" in {r[1] for r in c.execute("PRAGMA table_info(usage)")}
    with open(str(p) + ".migrate", "a") as lf:
        if not _migrate_lock(lf, time.monotonic() if has_lane else deadline):   # released on close
            if has_lane:
                # debug, not warning: while a long background build holds the lock this is every write, and expected
                logger.debug("usage ledger: migration lock held elsewhere; migration skipped for this row")
                return False
            raise sqlite3.OperationalError("usage ledger: no lane column, and the migration lock is held elsewhere")
        if _migrated(c):
            return False
        wal = _ensure_wal(c)
        _ensure_lane_column(c)
        with _bg_lock:
            in_background = str(p) in _bg_started  # this process already hands the build to its background thread
        if wal and not in_background and not _index_present(c):
            # not built in the rollback journal: that build is what blocked the writers; the next record retries
            stop = min(time.monotonic() + INDEX_FOREGROUND_S, time.monotonic() + max(0.0, deadline - time.monotonic()) / 2)
            c.set_progress_handler(lambda: 1 if time.monotonic() > stop else 0, _PROGRESS_OPS)
            try:
                _build_index(c)
            except sqlite3.OperationalError as exc:
                if "interrupt" not in str(exc).lower():
                    raise
                c.rollback()
                return True
            finally:
                c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_OPS)
    return False


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
        background = _migrate(c, p, deadline)
        c.execute(_INSERT, row)
        c.commit()
    finally:
        c.close()
    if background:                                  # after this row's commit, never before (Codex review r11)
        _start_background_migration(p)


_INSERT = ("INSERT INTO usage (ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane) "
           "VALUES (?,?,?,?,?,?,?,?)")


def _spool_path(p: Path) -> Path:
    return p.parent / SPOOL


def _spool(p: Path, row: tuple) -> None:
    """Append one row to the spool: one JSON line, O_APPEND, a single write, under the spool's flock. Raises on failure.
    The lock is held only for this write and for _replay's rename, never for sqlite work. After taking it the fd is
    checked to still be the spool's path: a replay may have renamed the file this fd was opened on, and a line written
    there after the replay read it would be lost."""
    line = json.dumps({"uid": uuid.uuid4().hex, "row": list(row)}, separators=(",", ":")) + "\n"
    if len(line.encode()) >= _SPOOL_MAX_LINE:       # only a pathological model name gets here: shortened, not dropped
        r = list(row)
        r[2] = str(r[2])[:200]
        line = json.dumps({"uid": uuid.uuid4().hex, "row": r}, separators=(",", ":")) + "\n"
    sp = _spool_path(p)
    for _ in range(10):
        fd = os.open(str(sp), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if os.fstat(fd).st_ino != os.stat(str(sp)).st_ino:
                    continue                        # renamed by a replay since open: reopen the current spool
            except FileNotFoundError:
                continue
            data = line.encode()
            if os.write(fd, data) != len(data):
                raise OSError("short write to the usage spool")
            return
        finally:
            os.close(fd)                            # releases the flock
    raise OSError("the usage spool kept being replaced under this write")


def _replay_locked(p: Path, deadline: float | None) -> int:
    """Move every spooled row into the db; the caller holds the migrate lock (one replayer at a time). The spool is
    renamed to a pending file first (under the spool's flock, so no append is lost), then each pending file's rows go
    in with INSERT OR IGNORE on their uid in usage_spool_applied inside one transaction, then the pending file is
    removed and its uids forgotten. A crash at any point leaves either the pending file (replayed again: the applied
    uids make that a no-op) or nothing. Bounded by `deadline` when given. Returns the rows inserted."""
    sp = _spool_path(p)
    if sp.exists():
        fd = os.open(str(sp), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size:
                os.rename(str(sp), str(sp) + f".{uuid.uuid4().hex}.replay")
        finally:
            os.close(fd)
    pending = sorted(p.parent.glob(SPOOL + ".*.replay"))
    if not pending:
        return 0
    left = None if deadline is None else deadline - time.monotonic()
    if left is not None and left <= 0:
        return 0
    c = sqlite3.connect(str(p), timeout=30.0 if left is None else min(TIMEOUT_S, left))
    if deadline is not None:
        c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_OPS)
    inserted = 0
    try:
        c.execute(_APPLIED)
        c.commit()
        for f in pending:
            entries = []
            for raw in f.read_text().splitlines():
                try:
                    e = json.loads(raw)
                except ValueError:
                    logger.warning("usage ledger: a torn spool line in %s was skipped", f.name)
                    continue
                if isinstance(e, dict) and isinstance(e.get("uid"), str) and isinstance(e.get("row"), list) \
                        and len(e["row"]) == 8:
                    entries.append(e)
            uids = [e["uid"] for e in entries]
            with c:                                 # one transaction per pending file
                for e in entries:
                    if c.execute("INSERT OR IGNORE INTO usage_spool_applied (uid) VALUES (?)", (e["uid"],)).rowcount:
                        c.execute(_INSERT, tuple(e["row"]))
                        inserted += 1
            f.unlink()
            with c:                                 # the file is gone, so these uids can never come back
                c.executemany("DELETE FROM usage_spool_applied WHERE uid = ?", ((u,) for u in uids))
    finally:
        c.close()
    if inserted:
        logger.info("usage ledger: %d spooled row(s) replayed into %s", inserted, p.name)
    return inserted


def _replay(p: Path, deadline: float) -> None:
    """After a successful write: replay the spool if there is one and the migrate lock is free right now (a held lock
    is a migration in progress, which replays when it ends). Never raises; a failed replay is logged and kept."""
    sp = _spool_path(p)
    if not sp.exists() and not any(p.parent.glob(SPOOL + ".*.replay")):
        return
    try:
        with open(str(p) + ".migrate", "a") as lf:
            if not _migrate_lock(lf, time.monotonic()):
                return
            _replay_locked(p, deadline)
    except Exception as exc:
        logger.warning("usage ledger: spool replay failed (%s: %s); kept for the next write", type(exc).__name__,
                       str(exc)[:120])


def record(lane: str, model: str, usage: dict | None, *, ts: float | None = None, path: Path | None = None,
           context: str | None = None) -> bool:
    """Append one turn. `lane` is the scarcity lane (claude, claude#b, codex); its base is the runtime. Returns False on failure.
    `context` (e.g. "search lane, run <id>") marks a write whose loss must be seen every time: its failure is logged at error
    level with the lane and that context on EVERY occurrence, besides counting toward consecutive_failures(). Logged
    once, when the deadline (RETRY_S from the start of this call, covering every step) passes or on the first non-busy
    error. A row that could not be written is spooled (returns False: not in the db yet, replayed later); the log says
    "spooled", or "LOST" when the spool append failed too."""
    global _consecutive_failures
    deadline = time.monotonic() + RETRY_S
    p = row = None
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
        fate = "LOST: it could not be spooled either"
        if p is not None and row is not None:
            try:
                _spool(p, row)
                fate = f"spooled to {SPOOL}, replayed into the db later"
            except Exception as sexc:
                fate = f"LOST: the spool append failed too ({type(sexc).__name__}: {str(sexc)[:120]})"
        if context:
            logger.error("usage ledger write failed for %s on lane %s (%s: %s); row %s", context, lane,
                         type(exc).__name__, str(exc)[:120], fate)
        elif fate.startswith("LOST") or n == 1 or n % LOG_EVERY == 0:
            logger.warning("usage ledger write failed (%s: %s); row %s; %d consecutive failures, logged on the first and "
                           "every %d", type(exc).__name__, str(exc)[:120], fate, n, LOG_EVERY)
        return False
    _replay(p, deadline)
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
