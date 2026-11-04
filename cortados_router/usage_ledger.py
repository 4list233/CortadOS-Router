"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools.

A write never breaks or slows a turn. The request path only calls record_async, which puts the row on a bounded
in-process queue and returns: a request thread never opens SQLite or takes a file lock. One background writer thread
drains the queue in committed batches and retries while the db is locked or migrating, so the queue is the only buffer
while a migration (lane column, ts index) holds the db. A row is lost only when the queue is full, when the db keeps
failing with a non-busy error, or when the process exits with rows still queued; each loss is counted (`stats()`, shown
in /v1/cortados/usage under `ledger.lost`) and logged, never silent. A usage row is accounting for unitcost, not quota
enforcement: the floor reservations are separate and do not read this queue.

record() is the synchronous single-row writer (tests, fixtures, tools); it is never called on a request thread."""
from __future__ import annotations

import atexit
import fcntl
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortados.usage_ledger")
TIMEOUT_S = 0.5          # one attempt's sqlite busy wait (capped by what is left of record()'s deadline)
# Reason: a busy db (a migration building the index, a view scan, a racing writer) is transient; dropping the row there
# lost turns, so record() retries. RETRY_S is ONE end-to-end deadline per record() call (and per writer-thread batch
# attempt), over everything it does: the migrate lock wait, every sqlite busy wait and statement, and the retries, so
# one attempt never hangs the writer on a stuck lock; the writer then retries the batch.
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
LOG_EVERY = 100          # a steady failure (full disk, read-only home) repeats every turn: one line per 100 turns stays readable
_fail_lock = threading.Lock()
_consecutive_failures = 0
# Reason for the bound: a db that stays locked must not grow this process's memory without limit. A turn is one row
# (~100 bytes), and even a busy provider completes far fewer than 10k turns during any migration or lock, so a full
# queue means the writer is truly stuck; past it rows are counted lost and logged.
QUEUE_MAX = 10_000
BATCH_MAX = 500          # rows per committed batch: one short write transaction, never a long hold of the db
_WRITER_BACKOFF_S, _WRITER_BACKOFF_MAX_S = 0.05, 5.0   # between retries of a busy batch: first wait, doubling, capped
# A non-busy error (unopenable path, corrupt file, full disk) is not a lock to wait out: the batch is retried this many
# times and then counted lost, so one bad batch cannot stop the rows behind it forever.
NON_BUSY_TRIES = 3
# At exit the writer gets this long to commit what is queued (a clean stop should not lose the last turn's row); exit
# must not hang on a locked db, so what is still queued then is counted lost and logged.
SHUTDOWN_DRAIN_S = 2.0
_queue: "queue.Queue[tuple[Path, tuple, str | None]]" = queue.Queue(maxsize=QUEUE_MAX)
_state_lock = threading.Lock()
_lost = 0
_inflight = 0            # rows the writer has taken off the queue and not yet committed or counted lost
_writer: threading.Thread | None = None


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
                logger.info("usage ledger: background migration of %s complete", p)
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
            raise MigrationBusy("usage ledger: no lane column, and the migration lock is held elsewhere")
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


class MigrationBusy(sqlite3.OperationalError):
    """The row needs the lane column and another writer holds the migration: transient, retried like a lock."""


def _busy(exc: Exception) -> bool:
    m = str(exc).lower()
    return isinstance(exc, MigrationBusy) or (isinstance(exc, sqlite3.OperationalError) and ("locked" in m or "busy" in m))


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _write(p: Path, row: tuple, deadline: float) -> None:
    """One attempt, bounded by `deadline`: the busy wait is capped by the time left, and any statement still running at
    the deadline is interrupted (OperationalError "interrupted", not busy, so record() stops there)."""
    _write_rows(p, [row], deadline)


def _write_rows(p: Path, rows: list[tuple], deadline: float) -> None:
    """_write for a batch: every row in one committed transaction, or none."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise sqlite3.OperationalError("usage ledger: write deadline (RETRY_S) passed")
    c = sqlite3.connect(str(p), timeout=min(TIMEOUT_S, left))
    c.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_OPS)   # non-zero: interrupt
    try:
        c.execute(_SCHEMA)        # IF NOT EXISTS: a fresh file needs the table before _migrated() can read it
        c.commit()
        background = _migrate(c, p, deadline)
        c.executemany(_INSERT, rows)
        c.commit()
    finally:
        c.close()
    if background:                                  # after this row's commit, never before (Codex review r11)
        _start_background_migration(p)


_INSERT = ("INSERT INTO usage (ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane) "
           "VALUES (?,?,?,?,?,?,?,?)")


def _row(lane: str, model: str, usage: dict | None, ts: float | None) -> tuple:
    u = usage or {}
    runtime = (lane or "").split("#", 1)[0]
    return (ts or time.time(), runtime, str(model or ""), _int(u.get("input_tokens")), _int(u.get("output_tokens")),
            _int(u.get("cache_read_input_tokens") or u.get("cache_read_tokens")),
            _int(u.get("cache_creation_input_tokens") or u.get("cache_write_tokens")), lane)


def _failed(exc: Exception, n_rows: int, contexts: list[tuple[str, str]], fate: str) -> None:
    """Count one failed write and log it: every context row at error level every time, else the first and every
    LOG_EVERY-th failure (a steady failure repeats every turn)."""
    global _consecutive_failures
    with _fail_lock:
        _consecutive_failures += 1
        n = _consecutive_failures
    for lane, context in contexts:
        logger.error("usage ledger write failed for %s on lane %s (%s: %s); row %s", context, lane,
                     type(exc).__name__, str(exc)[:120], fate)
    if not contexts and (n == 1 or n % LOG_EVERY == 0 or fate.startswith("LOST")):
        logger.warning("usage ledger write failed (%s: %s); %d row(s) %s; %d consecutive failures, logged on the first "
                       "and every %d", type(exc).__name__, str(exc)[:120], n_rows, fate, n, LOG_EVERY)


def _succeeded() -> None:
    global _consecutive_failures
    with _fail_lock:
        if _consecutive_failures:
            logger.info("usage ledger writes recovered after %d failures", _consecutive_failures)
        _consecutive_failures = 0


def record(lane: str, model: str, usage: dict | None, *, ts: float | None = None, path: Path | None = None,
           context: str | None = None) -> bool:
    """Write one turn synchronously (tests, fixtures, tools; the provider uses record_async). `lane` is the scarcity
    lane (claude, claude#b, codex); its base is the runtime. Busy errors are retried until RETRY_S from the start of
    this call. Returns False on failure, never raises. `context` (e.g. "search lane, run <id>") marks a write whose
    failure is logged at error level with the lane and that context every time."""
    deadline = time.monotonic() + RETRY_S
    try:
        row = _row(lane, model, usage, ts)
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
        _failed(exc, 1, [(lane, context)] if context else [], "not written")
        return False
    _succeeded()
    return True


def stats() -> dict[str, int]:
    """The async writer's counters for /v1/cortados/usage: `queued` rows not yet committed (on the queue or in the
    batch being written) and `lost` rows since this process started."""
    with _state_lock:
        return {"queued": _queue.qsize() + _inflight, "lost": _lost}


def _count_lost(n: int, why: str, contexts: list[tuple[str, str]]) -> None:
    global _lost
    with _state_lock:
        _lost += n
        total = _lost
    for lane, context in contexts:
        logger.error("usage ledger row LOST for %s on lane %s (%s); %d lost since start", context, lane, why, total)
    if not contexts:
        logger.warning("usage ledger: %d row(s) LOST (%s); %d lost since start", n, why, total)


def _ensure_writer() -> None:
    """Start the writer thread if it is not running. A failed start is logged; the row stays queued and the next
    record_async tries again."""
    global _writer
    with _state_lock:
        if _writer is not None and _writer.is_alive():
            return
        t = threading.Thread(target=_writer_loop, name="cortados-usage-ledger-writer", daemon=True)
        try:
            t.start()
        except Exception as exc:
            logger.warning("usage ledger: writer thread did not start (%s: %s); %d row(s) wait on the queue",
                           type(exc).__name__, str(exc)[:120], _queue.qsize())
            return
        _writer = t


def record_async(lane: str, model: str, usage: dict | None, *, context: str | None = None,
                 path: Path | None = None) -> bool:
    """Queue one turn for the writer thread and return at once; never touches SQLite or a lock, never raises. Returns
    False when the row was not queued (queue full, or a bad row): it is then counted lost and logged."""
    try:
        item = (path or db_path(), _row(lane, model, usage, None), context)
        _queue.put_nowait(item)
    except queue.Full:
        _count_lost(1, f"the ledger queue is full ({QUEUE_MAX} rows waiting)", [(lane, context)] if context else [])
        return False
    except Exception as exc:
        _count_lost(1, f"{type(exc).__name__}: {str(exc)[:120]}", [(lane, context)] if context else [])
        return False
    _ensure_writer()
    return True


def _take_batch() -> list[tuple[Path, tuple, str | None]]:
    global _inflight
    batch = [_queue.get()]
    while len(batch) < BATCH_MAX:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    with _state_lock:
        _inflight += len(batch)
    return batch


def _done(n: int) -> None:
    global _inflight
    with _state_lock:
        _inflight -= n
    for _ in range(n):
        _queue.task_done()


def _write_batch(p: Path, items: list[tuple[Path, tuple, str | None]]) -> None:
    """Commit one path's rows, retrying while busy (locked, migrating) for as long as it takes, and a non-busy error
    NON_BUSY_TRIES times before counting the rows lost."""
    rows = [it[1] for it in items]
    contexts = [(it[1][7], it[2]) for it in items if it[2]]
    wait, tries = _WRITER_BACKOFF_S, 0
    while True:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_rows(p, rows, time.monotonic() + RETRY_S)
            _succeeded()
            return
        except Exception as exc:
            if not _busy(exc):
                tries += 1
                if tries >= NON_BUSY_TRIES:
                    _failed(exc, len(rows), [], "LOST")
                    _count_lost(len(rows), f"{type(exc).__name__}: {str(exc)[:120]}", contexts)
                    return
            elif wait == _WRITER_BACKOFF_S:         # the first busy retry of this batch: once, not per retry
                _failed(exc, len(rows), [], "queued, retried until the db is free")
        time.sleep(wait)
        wait = min(wait * 2, _WRITER_BACKOFF_MAX_S)


def _writer_loop() -> None:
    while True:
        batch = _take_batch()
        try:
            by_path: dict[Path, list] = {}
            for it in batch:
                by_path.setdefault(it[0], []).append(it)
            for p, items in by_path.items():
                _write_batch(p, items)
        except Exception:                           # a bug here must not kill the writer and strand the queue
            logger.exception("usage ledger writer: unexpected error; %d row(s) counted lost", len(batch))
            _count_lost(len(batch), "writer error", [])
        finally:
            _done(len(batch))


def flush(timeout: float) -> bool:
    """Wait up to `timeout` for every queued row to be committed or counted lost. True when the queue is drained."""
    until = time.monotonic() + timeout
    while _queue.unfinished_tasks:
        if time.monotonic() >= until:
            return False
        time.sleep(0.01)
    return True


def shutdown(timeout: float | None = None) -> int:
    """At process exit: give the writer SHUTDOWN_DRAIN_S to commit, then take what is still queued off the queue and
    count it lost (with the batch in flight), logged in one line. Returns the rows counted lost here."""
    flush(SHUTDOWN_DRAIN_S if timeout is None else timeout)
    left = 0
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
        _queue.task_done()
        left += 1
    with _state_lock:
        left += _inflight
    if left:
        _count_lost(left, "the process exited with rows still queued", [])
    return left


atexit.register(shutdown)
