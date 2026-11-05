"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools.

A write never breaks or slows a turn. The request path only calls record_async, which puts the row on a bounded
in-process queue and returns: a request thread never opens SQLite, takes a file lock or a lifecycle lock (a row it
cannot queue is counted under the queue mutex and goes on a drop list the supervisor logs). The writer starts with the provider
(start()), never from a request, and a supervisor thread restarts it whenever it is not running. It owns each row's
single disposition (committed or counted lost); shutdown stops it and counts, exactly once, what it did not write.
One background writer thread drains the queue in committed batches and retries while the db is locked or migrating, so the queue is the only buffer
while a migration (lane column, ts index) holds the db. A row is lost only when the queue is full, when the db keeps
failing with a non-busy error, or when the process exits with rows still queued; each loss is counted (`stats()`, shown
in /v1/cortados/usage under `ledger.lost`) and logged, never silent. A usage row is accounting for unitcost, not quota
enforcement: the floor reservations are separate and do not read this queue.

record() is the synchronous single-row writer (tests, fixtures, tools); it is never called on a request thread."""
from __future__ import annotations

import atexit
import contextlib
import fcntl
import logging
import queue
import sqlite3
import threading
import time
from collections import deque
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
# At exit the writer gets this long to finish (a clean stop should not lose the last turn's row); exit must not hang
# on a locked db, so what is still unwritten then is counted lost and logged.
SHUTDOWN_DRAIN_S = 2.0
# The supervisor's tick: how soon a dead (or never started) writer is restarted and request-path drops are logged.
SUPERVISE_S = 1.0
_GET_WAIT_S = 0.1        # the writer's wait for a row, so it sees the stop event promptly


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


def _write_rows(p: Path, rows: list[tuple], deadline: float, guard: Any = None) -> None:
    """_write for a batch: every row in one committed transaction, or none. `guard()`, when given, is a context
    manager entered around the commit (the async writer decides the batch's disposition there)."""
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
        if guard is None:
            c.commit()
        else:
            try:
                with guard():
                    c.commit()
            except BaseException:
                c.rollback()
                raise
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


class _Batch:
    """One path's rows the writer has claimed. Its single disposition (committed, or counted lost) is decided under
    Writer.lock: `done` once the writer committed or counted it, `revoked` once shutdown counted it lost instead."""
    __slots__ = ("path", "items", "done", "revoked")

    def __init__(self, path: Path, items: list):
        self.path, self.items, self.done, self.revoked = path, items, False, False


class _Revoked(Exception):
    """Shutdown took the batch away before its commit: rolled back, already counted lost by shutdown."""


class Writer:
    """The async ledger writer: a bounded queue, one writer thread, and a supervisor thread that (re)starts it.

    The request path takes only the queue mutex: admission, closure and rejected-row counts are serialized there.
    Lifecycle operations always take `lock` BEFORE the queue mutex, never the reverse. Dequeue/claim and terminal
    disposition/retirement are atomic under those locks; SQLite commit holds only `lock`. Requests never wait for
    SQLite or logging. Rejected rows are counted immediately; the supervisor only drains their deferred logs."""

    def __init__(self, maxsize: int = QUEUE_MAX):
        self.q: "queue.Queue[tuple[Path, tuple, str | None]]" = queue.Queue(maxsize=maxsize)
        self.drops: "deque[tuple[str, str | None, str]]" = deque()
        self.lock = threading.Lock()
        self.lost = 0                              # protected by q.mutex, including request-path rejections
        self.claimed: list[_Batch] = []
        self.closed = False
        self.stop = threading.Event()
        self.writer: threading.Thread | None = None
        self.supervisor: threading.Thread | None = None
        self._start_failures = 0

    # -- request path ------------------------------------------------------------------------------------------
    def put(self, item: tuple, lane: str, context: str | None) -> bool:
        # Queue's nonblocking put protocol, with the closed check in the SAME critical section. A producer that
        # waited for shutdown's drain cannot enqueue behind it. No Writer.lock (it may be held by a SQLite commit).
        with self.q.not_full:
            if self.closed:
                why = "the ledger writer is shut down"
            elif self.q.maxsize > 0 and self.q._qsize() >= self.q.maxsize:
                why = f"the ledger queue is full ({self.q.maxsize} rows waiting)"
            else:
                self.q._put(item)
                self.q.unfinished_tasks += 1
                self.q.not_empty.notify()
                return True
            self.lost += 1
            self.drops.append((lane, context, why))
            return False

    def reject(self, lane: str, context: str | None, why: str) -> None:
        """Bad input never reaches put; count it now, not after a potentially paused deferred logger."""
        with self.q.mutex:
            self.lost += 1
            self.drops.append((lane, context, why))

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def start(self) -> bool:
        """Start the supervisor (it starts the writer). Called at provider startup; idempotent. False if it could
        not start (logged; the next start() tries again)."""
        with self.lock:
            if self.closed or (self.supervisor is not None and self.supervisor.is_alive()):
                return not self.closed
            t = threading.Thread(target=self._supervise, name="cortados-usage-ledger-supervisor", daemon=True)
            try:
                t.start()
            except Exception as exc:
                err = exc
            else:
                self.supervisor = t
                return True
        logger.error("usage ledger: supervisor thread did not start (%s: %s); rows wait on the queue",
                     type(err).__name__, str(err)[:120])
        return False

    def _start_writer_thread(self) -> threading.Thread:
        t = threading.Thread(target=self._write_loop, name="cortados-usage-ledger-writer", daemon=True)
        t.start()
        return t

    def _supervise(self) -> None:
        while not self.stop.is_set():
            self.fold_drops()
            if self.writer is None or not self.writer.is_alive():
                try:
                    self.writer = self._start_writer_thread()
                    if self._start_failures:
                        logger.warning("usage ledger: writer thread started after %d failed start(s)",
                                       self._start_failures)
                    self._start_failures = 0
                except Exception as exc:
                    self._start_failures += 1
                    if self._start_failures == 1 or self._start_failures % 60 == 0:
                        logger.error("usage ledger: writer thread did not start (%s: %s); %d row(s) wait on the queue; "
                                     "retried every %.0fs", type(exc).__name__, str(exc)[:120], self.q.qsize(),
                                     SUPERVISE_S)
            self.stop.wait(SUPERVISE_S)
        self.fold_drops()

    def fold_drops(self) -> None:
        """Log already-counted request-path losses; detaching or pausing these logs cannot hide a disposition."""
        with self.q.mutex:
            got, self.drops = self.drops, deque()
            total = self.lost
        if got:
            self._log_lost(len(got), got[0][2], [(lane, ctx) for lane, ctx, _ in got if ctx], total)

    def _log_lost(self, n: int, why: str, contexts: list, total: int) -> None:
        for lane, context in contexts:
            logger.error("usage ledger row LOST for %s on lane %s (%s); %d lost since start", context, lane, why, total)
        if len(contexts) < n:
            logger.warning("usage ledger: %d row(s) LOST (%s); %d lost since start", n, why, total)

    # -- the writer --------------------------------------------------------------------------------------------
    def _after_take(self) -> None:
        """Test seam: after the atomic dequeue/claim, before any write."""

    def _take(self) -> list[_Batch]:
        # Wait without Writer.lock so an idle writer cannot delay shutdown. Recheck under BOTH locks: another
        # lifecycle operation may have drained/closed the queue between the notification and our claim.
        with self.q.not_empty:
            if not self.q._qsize() and not self.closed:
                self.q.not_empty.wait(_GET_WAIT_S)
        with self.lock:
            with self.q.mutex:
                if self.closed:
                    return []
                by_path: dict[Path, list] = {}
                for _ in range(min(BATCH_MAX, self.q._qsize())):
                    it = self.q._get()
                    by_path.setdefault(it[0], []).append(it)
                batches = [_Batch(p, its) for p, its in by_path.items()]
                self.claimed.extend(batches)
                self.q.not_full.notify_all()
                return batches

    def _write_loop(self) -> None:
        while not self.stop.is_set():
            batches = self._take()
            if not batches:
                continue
            try:
                self._after_take()
                self._handle(batches)
            except Exception:
                logger.exception("usage ledger writer: unexpected error")
            finally:
                # Includes paths not yet attempted if an earlier path raised. Already committed/revoked/retired
                # batches are not counted again, unlike losing the entire dequeued group on an unexpected error.
                for b in batches:
                    self._retire(b, "writer error")

    def _retire(self, b: _Batch, why: str) -> None:
        n = 0
        with self.lock:
            if b not in self.claimed:
                return
            with self.q.mutex:
                if not (b.done or b.revoked):
                    n = len(b.items)
                    self.lost += n
                    b.done = True
                total = self.lost
                self.claimed.remove(b)
                self._tasks_done(len(b.items))
        if n:
            self._log_lost(n, why, [(it[1][7], it[2]) for it in b.items if it[2]], total)

    def _tasks_done(self, n: int) -> None:
        """Queue mutex held: retire tasks with their disposition, even if the daemon never runs again."""
        self.q.unfinished_tasks -= n
        if not self.q.unfinished_tasks:
            self.q.all_tasks_done.notify_all()

    def _handle(self, batches: list[_Batch]) -> None:
        for b in batches:
            why = "writer error"
            try:
                why = self._write_batch(b) or why
            finally:
                self._retire(b, why)

    @contextlib.contextmanager
    def _commit_guard(self, b: _Batch):
        """Held around a batch's commit: the commit and its disposition happen together, so a shutdown either sees
        the batch committed or revokes it before the commit (which then rolls back), never both."""
        with self.lock:
            if b.revoked:
                raise _Revoked()
            yield
            b.done = True

    def _write_batch(self, b: _Batch) -> str | None:
        """Commit one path's rows, retrying while busy (locked, migrating) until stopped, and a non-busy error
        NON_BUSY_TRIES times. None when committed (or revoked by shutdown); else why the rows are lost."""
        rows = [it[1] for it in b.items]
        wait, tries = _WRITER_BACKOFF_S, 0
        while True:
            with self.lock:
                if b.revoked:
                    return None
            try:
                b.path.parent.mkdir(parents=True, exist_ok=True)
                _write_rows(b.path, rows, time.monotonic() + RETRY_S, lambda: self._commit_guard(b))
                _succeeded()
                return None
            except _Revoked:
                return None
            except Exception as exc:
                if not _busy(exc):
                    tries += 1
                    if tries >= NON_BUSY_TRIES:
                        _failed(exc, len(rows), [], "LOST")
                        return f"{type(exc).__name__}: {str(exc)[:120]}"
                elif wait == _WRITER_BACKOFF_S:     # the first busy retry of this batch: once, not per retry
                    _failed(exc, len(rows), [], "queued, retried until the db is free")
            if self.stop.wait(wait):
                return "the process exited while the db was still busy"
            wait = min(wait * 2, _WRITER_BACKOFF_MAX_S)

    # -- views -------------------------------------------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        self.fold_drops()
        with self.lock:
            with self.q.mutex:
                inflight = sum(len(b.items) for b in self.claimed if not (b.done or b.revoked))
                return {"queued": self.q._qsize() + inflight, "lost": self.lost}

    def flush(self, timeout: float) -> bool:
        until = time.monotonic() + timeout
        while self.q.unfinished_tasks:
            if time.monotonic() >= until:
                return False
            time.sleep(0.01)
        return True

    def shutdown(self, timeout: float | None = None) -> int:
        """Stop the writer, giving it `timeout` (SHUTDOWN_DRAIN_S) to finish what is queued; then count, exactly once,
        what it did not write: any batch it still holds (revoked, so its later commit rolls back and its result is
        ignored) and whatever is left on the queue. Returns the rows counted lost here. Every admitted row has a
        final disposition before return; subsequent rejected requests count as new losses, never queued work."""
        timeout = SHUTDOWN_DRAIN_S if timeout is None else timeout
        until = time.monotonic() + timeout
        self.flush(timeout)                         # the writer drains while it runs
        self.stop.set()
        w = self.writer
        if w is not None and w.is_alive():
            w.join(max(0.0, until - time.monotonic()))
        n, contexts = 0, []
        with self.lock:
            with self.q.mutex:
                self.closed = True
                for b in self.claimed:
                    if not (b.done or b.revoked):
                        b.revoked = True
                        n += len(b.items)
                        contexts += [(it[1][7], it[2]) for it in b.items if it[2]]
                    self._tasks_done(len(b.items))
                self.claimed = []
                while self.q._qsize():
                    it = self.q._get()
                    self._tasks_done(1)
                    n += 1
                    if it[2]:
                        contexts.append((it[1][7], it[2]))
                self.lost += n
                total = self.lost
                self.q.not_empty.notify_all()
                self.q.not_full.notify_all()
        if n:
            self._log_lost(n, "the process exited with rows still queued", contexts, total)
        self.fold_drops()
        return n


_W = Writer()


def start() -> bool:
    """Start the async writer (provider startup). Idempotent."""
    return _W.start()


def stats() -> dict[str, int]:
    """For /v1/cortados/usage: `queued` rows not yet committed and `lost` rows since this process started."""
    return _W.stats()


def flush(timeout: float) -> bool:
    """Wait up to `timeout` for every queued row to be committed or counted lost. True when drained."""
    return _W.flush(timeout)


def shutdown(timeout: float | None = None) -> int:
    return _W.shutdown(timeout)


def record_async(lane: str, model: str, usage: dict | None, *, context: str | None = None,
                 path: Path | None = None) -> bool:
    """Queue one turn for the writer and return at once: no SQLite, no lock beyond the queue's own, never raises.
    False when the row could not be queued (full queue, shut down, bad input): it is counted lost immediately and
    logged by the supervisor."""
    try:
        item = (path or db_path(), _row(lane, model, usage, None), context)
    except Exception as exc:
        _W.reject(lane, context, f"{type(exc).__name__}: {str(exc)[:120]}")
        return False
    return _W.put(item, lane, context)


def _at_exit() -> None:
    _W.shutdown()


atexit.register(_at_exit)
