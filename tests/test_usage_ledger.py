"""Usage ledger (cortados_router/usage_ledger.py), per-account unit-cost pools, and scarcity's visible learn failure."""
import importlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import usage_ledger


def _unitcost(home: str):
    with mock.patch.dict(os.environ, {"CORTADOS_HOME": home}):
        import cortados_router.unitcost as uc
        return importlib.reload(uc)


class LedgerWrite(unittest.TestCase):
    def test_fake_turn_writes_one_row_with_lane(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7}
            self.assertTrue(usage_ledger.record("claude#b", "claude-sonnet-5", usage, ts=10.0, path=p))
            rows = sqlite3.connect(p).execute("SELECT ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane FROM usage").fetchall()
            self.assertEqual(rows, [(10.0, "claude", "claude-sonnet-5", 100, 20, 5, 7, "claude#b")])

    def test_old_ledger_gets_lane_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
            c.commit(); c.close()
            self.assertTrue(usage_ledger.record("codex", "gpt-6", {"input_tokens": 1}, path=p))
            self.assertIn("lane", {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(usage)")})

    def test_old_rollback_ledger_migrates_to_wal_with_ts_index(self):
        # the usage view scans by ts while turns are recorded: WAL + an index on ts keep the writer unblocked
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
            c.commit()
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            c.close()
            for _ in range(2):                                  # idempotent: the second write migrates nothing
                self.assertTrue(usage_ledger.record("codex", "gpt-6", {"input_tokens": 1}, path=p))
            c = sqlite3.connect(p)
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual([r[1] for r in c.execute("PRAGMA index_list(usage)")], ["usage_ts"])
            plan = " ".join(str(r) for r in c.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM usage WHERE ts >= 0"))
            self.assertIn("usage_ts", plan)
            c.close()

    def test_legacy_ledger_migration_with_concurrent_writers_loses_no_row(self):
        # Review P2: the first CREATE INDEX on a large rollback-journal ledger held the write lock and concurrent
        # record() calls returned False. WAL first, one migrator under the lock file, and busy retries: every row lands.
        import threading
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
            c.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
                          ((float(i % 99991), "claude", f"m{i % 40}", 1, 1, 0, 0) for i in range(200000)))
            c.commit()
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            c.close()
            usage_ledger._consecutive_failures = 0
            per, results, go = 25, [], threading.Event()

            def writer(k):
                go.wait()
                for j in range(per):
                    results.append(usage_ledger.record("codex", "gpt", {"input_tokens": 1}, ts=1e9 + k * 1000 + j, path=p))
            ts = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
            # one attempt's busy wait shorter than the index build, as on a multi-million-row ledger with the real
            # 0.5 s timeout: the old writer then dropped rows; the retry budget (RETRY_S) must still land every one
            with mock.patch.object(usage_ledger, "TIMEOUT_S", 0.005):
                for t in ts:
                    t.start()
                go.set()
                for t in ts:
                    t.join()
            self.assertEqual(results.count(False), 0, f"{results.count(False)} of {len(results)} writes failed")
            c = sqlite3.connect(p)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM usage WHERE ts >= 1e9").fetchone()[0], 4 * per)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 200000 + 4 * per)
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertIn("usage_ts", [r[1] for r in c.execute("PRAGMA index_list(usage)")])
            c.close()

    def test_busy_db_is_retried_not_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            self.assertTrue(usage_ledger.record("codex", "gpt", {}, path=p))
            real, calls = usage_ledger._write, {"n": 0}

            def flaky(path, row, *rest):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise sqlite3.OperationalError("database is locked")
                real(path, row, *rest)
            with mock.patch.object(usage_ledger, "_write", flaky):
                self.assertTrue(usage_ledger.record("codex", "gpt", {}, path=p))
            self.assertEqual(calls["n"], 3)
            self.assertEqual(sqlite3.connect(p).execute("SELECT COUNT(*) FROM usage").fetchone()[0], 2)

    def test_a_context_write_that_stays_busy_is_retried_then_logged_once_at_error(self):
        # The search lane's rows carry a context: a busy db is still retried for RETRY_S (main's logic), and only the
        # final failure is logged, at ERROR, with the lane and the context.
        with tempfile.TemporaryDirectory() as d:
            calls = {"n": 0}

            def busy(path, row, *rest):
                calls["n"] += 1
                raise sqlite3.OperationalError("database is locked")
            with mock.patch.object(usage_ledger, "_write", busy), mock.patch.object(usage_ledger, "RETRY_S", 0.3), \
                    self.assertLogs("cortados.usage_ledger", "ERROR") as cm:
                self.assertFalse(usage_ledger.record("claude#b", "m", {}, path=Path(d) / "usage.db",
                                                     context="search lane, run search-run-0001"))
            self.assertGreater(calls["n"], 1, "retried before giving up")
            self.assertEqual(len(cm.output), 1)
            self.assertIn("lane claude#b", cm.output[0])
            self.assertIn("search lane, run search-run-0001", cm.output[0])

    def test_a_context_row_uses_the_lane_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": 2}, path=p, context="search lane, run -"))
            self.assertEqual(sqlite3.connect(p).execute("SELECT runtime, lane, input_tokens FROM usage").fetchall(),
                             [("claude", "claude#b", 2)])

    def test_failure_does_not_raise_and_logs_type(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "dir-not-db"
            bad.mkdir()
            usage_ledger._consecutive_failures = 0
            with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                self.assertFalse(usage_ledger.record("claude", "m", {"input_tokens": 1}, path=bad))
            self.assertIn("OperationalError", cm.output[0])

    def test_consecutive_failures_counted_rate_limited_and_reset(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "dir-not-db"
            bad.mkdir()
            usage_ledger._consecutive_failures = 0
            with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                for _ in range(usage_ledger.LOG_EVERY):
                    usage_ledger.record("claude", "m", {}, path=bad)
            self.assertEqual(usage_ledger.consecutive_failures(), usage_ledger.LOG_EVERY)
            self.assertEqual(len(cm.output), 2)                 # the first and the LOG_EVERY-th
            self.assertTrue(usage_ledger.record("claude", "m", {}, path=Path(d) / "usage.db"))
            self.assertEqual(usage_ledger.consecutive_failures(), 0)

    def test_db_path_uses_paths_state_dir(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}):
            self.assertEqual(usage_ledger.db_path(), Path(d) / "usage.db")

    def test_migration_race_duplicate_column_is_success(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.db"
            c = sqlite3.connect(p)
            c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
            c.commit(); c.close()
            # A racer adds the column between our first check and the ALTER: simulate by reporting no lane on the first
            # PRAGMA only, so the ALTER hits "duplicate column" for real.
            racer = sqlite3.connect(p); racer.execute("ALTER TABLE usage ADD COLUMN lane TEXT"); racer.commit(); racer.close()
            real = usage_ledger._ensure_lane_column
            calls = {"n": 0}

            class Conn:
                def __init__(self, inner): self.inner = inner
                def execute(self, sql, *a):
                    if sql.startswith("PRAGMA table_info") and calls["n"] < 2:
                        calls["n"] += 1
                        return iter([])
                    return self.inner.execute(sql, *a)
            inner = sqlite3.connect(p)
            real(Conn(inner))                                     # must not raise
            inner.close()
            self.assertEqual(calls["n"], 2)
            self.assertTrue(usage_ledger.record("codex", "gpt-6", {"input_tokens": 1}, path=p))

    def test_record_async_writes_row(self):
        w = usage_ledger.Writer()
        self.addCleanup(w.shutdown, 0.5)
        with mock.patch.object(usage_ledger, "_W", w), tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}):
            self.assertTrue(w.start())
            self.assertTrue(usage_ledger.record_async("claude#b", "claude-sonnet-5", {"input_tokens": 4, "output_tokens": 1}))
            self.assertTrue(usage_ledger.flush(5))
            rows = sqlite3.connect(Path(d) / "usage.db").execute("SELECT runtime, input_tokens, lane FROM usage").fetchall()
            self.assertEqual(rows, [("claude", 4, "claude#b")])

    def _hold_migrate_lock(self, db):
        """Another process holding usage.db.migrate (as the reviewer did); returns it, killed at cleanup."""
        import subprocess
        import sys
        ready = Path(str(db) + ".held")
        code = ("import fcntl, sys, time, pathlib; f = open(sys.argv[1] + '.migrate', 'a'); "
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX); pathlib.Path(sys.argv[1] + '.held').touch(); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, str(db)])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        until = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "the lock holder started")
        return proc

    def _legacy(self, db):
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
        c.commit()
        c.close()

    def test_migrate_lock_held_but_schema_has_the_lane_the_row_is_still_written(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "RETRY_S", 0.5):
            db = Path(d) / "usage.db"
            c = sqlite3.connect(db)
            c.execute(usage_ledger._SCHEMA)                     # current schema, still rollback journal: not migrated
            c.commit()
            c.close()
            self._hold_migrate_lock(db)
            t0 = time.monotonic()
            with self.assertLogs("cortados.usage_ledger", "DEBUG") as cm:
                self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": 5}, path=db, context="search lane, run -"))
            self.assertLess(time.monotonic() - t0, 0.5 + 0.75)
            self.assertFalse(any(r.levelname == "ERROR" for r in cm.records))
            self.assertEqual(sqlite3.connect(db).execute("SELECT lane, input_tokens FROM usage").fetchall(), [("claude#b", 5)])

    def test_a_statement_past_the_deadline_is_interrupted(self):
        # sqlite work is bounded too: a statement that would run forever (an endless recursive query standing in for
        # a huge index build) is interrupted at the deadline by the progress handler
        def endless(c, p, deadline):
            c.execute("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) FROM r").fetchone()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_migrate", endless):
            db = Path(d) / "usage.db"
            t0 = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as cm:
                usage_ledger._write(db, (1.0, "claude", "m", 0, 0, 0, 0, "claude"), time.monotonic() + 0.3)
            self.assertIn("interrupted", str(cm.exception))
            self.assertLess(time.monotonic() - t0, 1.5)
            with self.assertRaises(sqlite3.OperationalError):     # and a spent deadline never starts an attempt
                usage_ledger._write(db, (1.0, "claude", "m", 0, 0, 0, 0, "claude"), time.monotonic() - 1)

    def test_an_index_build_longer_than_every_deadline_still_lands_rows_and_completes_in_the_background(self):
        # Review r10: on an oversized legacy ledger every foreground CREATE INDEX outlasts the deadline and is rolled
        # back. The lane column comes first, the row is written anyway, and one background thread (no record
        # deadline) finishes the build.
        import threading as th
        attempts = {"fg": 0, "bg": 0}
        real_build = usage_ledger._build_index

        def build(c):
            if th.current_thread().name == "cortados-usage-ledger-migrate":
                attempts["bg"] += 1
                return real_build(c)
            attempts["fg"] += 1                                 # stands in for a build longer than any deadline
            c.execute("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) FROM r").fetchone()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_build_index", build), \
                mock.patch.object(usage_ledger, "RETRY_S", 0.5), mock.patch.object(usage_ledger, "_BG_BACKOFF_S", 0.05), \
                mock.patch.object(usage_ledger, "_bg_started", set()):
            db = Path(d) / "usage.db"
            self._legacy(db)
            usage_ledger._consecutive_failures = 0
            t0 = time.monotonic()
            for i in range(3):
                self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": i}, path=db, context="search lane, run -"))
            self.assertLess(time.monotonic() - t0, 3 * 0.5 + 1.0, "each write stays inside its own deadline")
            self.assertGreaterEqual(attempts["fg"], 1, "the foreground tried, and was interrupted")
            until = time.monotonic() + 10
            while time.monotonic() < until:
                c = sqlite3.connect(db)
                try:
                    done = usage_ledger._migrated(c)
                finally:
                    c.close()
                if done:
                    break
                time.sleep(0.05)
            self.assertTrue(done, "the background migration completed")
            self.assertGreaterEqual(attempts["bg"], 1)
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("SELECT lane, input_tokens FROM usage ORDER BY input_tokens").fetchall(),
                             [("claude#b", 0), ("claude#b", 1), ("claude#b", 2)])
            self.assertIn("usage_ts", [r[1] for r in c.execute("PRAGMA index_list(usage)")])
            self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            c.close()
            self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": 9}, path=db))

    def _lost(self):
        return usage_ledger.stats()["lost"]

    def _fresh(self, maxsize=usage_ledger.QUEUE_MAX, start=True):
        """A new Writer in place of the module's, with real threads; shut down at cleanup."""
        w = usage_ledger.Writer(maxsize)
        p = mock.patch.object(usage_ledger, "_W", w)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(w.shutdown, 0.5)
        if start:
            self.assertTrue(w.start())
        return w

    def _join_writer(self, w):
        # A writer can reach a test barrier before _start_writer_thread returns/publishes w.writer. Stop and join
        # the supervisor first, so cleanup never races that publication (and never relies on a scheduling sleep).
        w.stop.set()
        if w.supervisor is not None:
            w.supervisor.join(5)
            self.assertFalse(w.supervisor.is_alive())
        if w.writer is not None:
            w.writer.join(5)
            self.assertFalse(w.writer.is_alive())

    def test_recovery_logging_exception_never_replays_a_committed_batch(self):
        # r15-1: recovery logging used to sit inside the retry block, duplicating a committed row.
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            w = self._fresh(start=False)
            with mock.patch.object(usage_ledger, "_consecutive_failures", 1), \
                    mock.patch.object(usage_ledger.logger, "info", side_effect=[RuntimeError("post-commit log"), None]), \
                    mock.patch.object(usage_ledger.logger, "exception") as diagnostic, \
                    mock.patch.object(usage_ledger, "_write_rows", wraps=usage_ledger._write_rows) as write:
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
                self.assertTrue(w.start())
                try:
                    self.assertTrue(w.flush(5))
                finally:
                    self._join_writer(w)
                self.assertEqual(write.call_count, 1)
                diagnostic.assert_called_once()
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
            self.assertEqual(w.stats(), {"queued": 0, "lost": 0})
            self.assertEqual(w.q.unfinished_tasks, 0)

    def test_write_cleanup_exception_never_replays_a_committed_batch(self):
        # The write helper can itself raise after commit (close/background migration), including a busy error.
        for error in (RuntimeError("post-commit cleanup"), sqlite3.OperationalError("database is locked")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as d:
                db = Path(d) / "usage.db"
                w = self._fresh(start=False)
                real_write = usage_ledger._write_rows

                def committed_then_failed(*args):
                    real_write(*args)
                    raise error

                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
                batches = w._take()
                with mock.patch.object(usage_ledger, "_write_rows", side_effect=committed_then_failed) as write:
                    with self.assertRaises(type(error)):
                        w._handle(batches)
                    self.assertEqual(write.call_count, 1)
                    # A repeated dispatch of an already committed batch is also a no-op.
                    self.assertIsNone(w._write_batch(batches[0]))
                    self.assertEqual(write.call_count, 1)
                with sqlite3.connect(db) as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
                self.assertEqual(w.stats(), {"queued": 0, "lost": 0})
                self.assertEqual(w.q.unfinished_tasks, 0)

    def test_rejection_details_are_bounded_with_exact_concurrent_loss_counts(self):
        # r15-2: a stalled supervisor must not turn 100,000 rejections into 100,000 retained records.
        w = self._fresh(maxsize=1, start=False)
        item = (Path("/nonexistent/usage.db"), usage_ledger._row("codex", "gpt", {}, None), None)
        self.assertTrue(w.put(item, "codex", None))

        def reject_many():
            for i in range(25000):
                if i % 2:
                    w.reject("codex", f"bad-{i}", "bad input")
                else:
                    w.put(item, "codex", f"full-{i}")

        producers = [threading.Thread(target=reject_many) for _ in range(4)]
        for producer in producers:
            producer.start()
        for producer in producers:
            producer.join(5)
            self.assertFalse(producer.is_alive())
        self.assertEqual(w.lost, 100000)
        self.assertEqual(len(w.drops), usage_ledger.DROP_DETAIL_MAX)
        self.assertEqual(w.drops_omitted, 100000 - usage_ledger.DROP_DETAIL_MAX)
        with self.assertLogs("cortados.usage_ledger", "WARNING") as logs:
            self.assertEqual(w.stats(), {"queued": 1, "lost": 100000})
        self.assertTrue(any("100000 row(s) LOST" in msg for msg in logs.output))
        self.assertTrue(any(f"{100000 - usage_ledger.DROP_DETAIL_MAX} rejected-row diagnostic detail(s) omitted"
                            in msg for msg in logs.output))
        self.assertEqual(len(w.drops), 0)
        self.assertEqual(w.drops_omitted, 0)
        # Detaching a diagnostic batch must not discard the bound or carry its omission count into the next batch.
        w.reject("codex", "next", "bad input")
        with mock.patch.object(w, "_log_lost") as log, mock.patch.object(usage_ledger.logger, "warning") as warning:
            w.fold_drops()
        log.assert_called_once_with(1, "bad input", [("codex", "next")], 100001)
        warning.assert_not_called()
        self.assertEqual(w.drops.maxlen, usage_ledger.DROP_DETAIL_MAX)
        with mock.patch.object(w, "_log_lost"):
            self.assertEqual(w.shutdown(0), 1)
        self.assertEqual(w.lost, 100002)

    def test_rejections_during_a_paused_drain_keep_separate_omission_counts(self):
        with mock.patch.object(usage_ledger, "DROP_DETAIL_MAX", 3):
            w = self._fresh(start=False)
            entered, release = threading.Event(), threading.Event()
            drained = []

            def pause_log(n, why, contexts, total):
                drained.append((n, total))
                entered.set()
                release.wait(5)

            for _ in range(5):
                w.reject("codex", None, "bad input")
            with mock.patch.object(w, "_log_lost", pause_log), \
                    self.assertLogs("cortados.usage_ledger", "WARNING") as logs:
                drainer = threading.Thread(target=w.fold_drops)
                drainer.start()
                try:
                    self.assertTrue(entered.wait(5))
                    for _ in range(7):
                        w.reject("codex", None, "bad input")
                    self.assertEqual((len(w.drops), w.drops_omitted, w.lost), (3, 4, 12))
                finally:
                    release.set()
                    drainer.join(5)
                self.assertFalse(drainer.is_alive())
                w.fold_drops()
            self.assertEqual(drained, [(5, 5), (7, 12)])
            self.assertTrue(any("2 rejected-row diagnostic detail(s) omitted" in msg for msg in logs.output))
            self.assertTrue(any("4 rejected-row diagnostic detail(s) omitted" in msg for msg in logs.output))
            self.assertEqual(w.stats(), {"queued": 0, "lost": 12})
            self.assertEqual(w.drops_omitted, 0)

    def test_closed_writer_rejection_details_remain_bounded(self):
        w = self._fresh(start=False)
        self.assertEqual(w.shutdown(0), 0)
        item = (Path("/nonexistent/usage.db"), usage_ledger._row("codex", "gpt", {}, None), None)
        for _ in range(usage_ledger.DROP_DETAIL_MAX + 5):
            self.assertFalse(w.put(item, "codex", None))
        self.assertEqual(len(w.drops), usage_ledger.DROP_DETAIL_MAX)
        self.assertEqual(w.drops_omitted, 5)
        with self.assertLogs("cortados.usage_ledger", "WARNING") as logs:
            self.assertEqual(w.stats(), {"queued": 0, "lost": usage_ledger.DROP_DETAIL_MAX + 5})
        self.assertTrue(any("5 rejected-row diagnostic detail(s) omitted" in msg for msg in logs.output))

    def _provider_startup_failure(self, message):
        from cortados_router.provider.server import Handler, ProviderServer, ThreadingHTTPServer
        servers = []
        activate = ThreadingHTTPServer.server_activate

        def listening(server):
            servers.append(server)
            activate(server)                       # real bind/listen, no serving thread or requests

        error = None
        try:
            with mock.patch.object(ThreadingHTTPServer, "server_activate", listening):
                try:
                    ProviderServer(("127.0.0.1", 0), None)
                except RuntimeError as exc:
                    error = exc
            # Keep the traceback (and thus the partially constructed server) alive. assertRaises clears it and
            # would allow garbage collection to hide the leaked listening socket.
            self.assertIsInstance(error, RuntimeError)
            self.assertRegex(str(error), message)
            self.assertIsNotNone(error.__traceback__)
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0].socket.fileno(), -1)
            # Reuse the exact port immediately, without GC, sleeps, or a mocked socket constructor.
            with ThreadingHTTPServer(servers[0].server_address, Handler) as retry:
                self.assertEqual(retry.server_address, servers[0].server_address)
            return error
        finally:
            for server in servers:
                server.server_close()              # also clean up if the regression fails

    def test_supervisor_start_failure_aborts_provider_startup(self):
        w = self._fresh(start=False)
        failure = RuntimeError("can't start new thread")
        with mock.patch.object(threading.Thread, "start", side_effect=failure):
            error = self._provider_startup_failure("supervisor thread did not start")
        self.assertIs(error.__cause__, failure)
        self.assertIsNone(w.supervisor)
        self.assertIsNone(w.writer)
        # A caller may explicitly retry startup; the failed attempt did not publish a phantom supervisor.
        self.assertTrue(w.start())
        self._join_writer(w)

    def test_closed_ledger_aborts_provider_startup_and_releases_socket(self):
        w = self._fresh(start=False)
        self.assertEqual(usage_ledger.shutdown(0), 0)
        self.assertTrue(w.closed)
        with mock.patch.object(threading.Thread, "start") as start_thread:
            self._provider_startup_failure("ledger writer is shut down")
        start_thread.assert_not_called()
        self.assertIsNone(w.supervisor)
        self.assertIsNone(w.writer)
        self.assertEqual(w.stats(), {"queued": 0, "lost": 0})

    def test_a_locked_db_never_blocks_the_request_path_and_the_rows_land_once_it_frees(self):
        self._fresh()
        # Review r12: the request thread only queues. Another connection holds SQLite's writer lock (as a long index
        # build does); record_async still returns at once, the writer retries, and every row lands when it is released.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "TIMEOUT_S", 0.05):
            db = Path(d) / "usage.db"
            self.assertTrue(usage_ledger.record("codex", "gpt", {}, ts=1.0, path=db))     # migrated, WAL
            holder = sqlite3.connect(db)
            holder.execute("BEGIN IMMEDIATE")
            lost0 = self._lost()
            t0 = time.monotonic()
            for i in range(50):
                self.assertTrue(usage_ledger.record_async("claude#b", "m", {"input_tokens": i}, path=db,
                                                          context="search lane, run -" if i == 0 else None))
            self.assertLess(time.monotonic() - t0, 0.2, "queuing never waits on the db")
            self.assertFalse(usage_ledger.flush(0.5), "the writer is waiting on the lock")
            self.assertGreater(usage_ledger.stats()["queued"], 0)
            holder.rollback()
            holder.close()
            self.assertTrue(usage_ledger.flush(10))
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*), SUM(input_tokens) FROM usage WHERE lane = 'claude#b'").fetchone(),
                             (50, sum(range(50))))
            c.close()
            self.assertEqual(self._lost(), lost0)
            self.assertEqual(usage_ledger.stats()["queued"], 0)

    def test_a_migrating_ledger_never_blocks_the_request_path(self):
        self._fresh()
        # a legacy ledger (no lane column) whose migrate lock another process holds: the writer retries (MigrationBusy
        # is busy), the request path is untouched, and the rows land once the lock is released
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "RETRY_S", 0.2):
            db = Path(d) / "usage.db"
            self._legacy(db)
            proc = self._hold_migrate_lock(db)
            lost0 = self._lost()
            t0 = time.monotonic()
            for i in range(5):
                self.assertTrue(usage_ledger.record_async("claude#b", "m", {"input_tokens": i}, path=db))
            self.assertLess(time.monotonic() - t0, 0.2)
            self.assertFalse(usage_ledger.flush(0.6))
            proc.kill()
            proc.wait()
            self.assertTrue(usage_ledger.flush(15))
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage WHERE lane = 'claude#b'").fetchone()[0], 5)
            self.assertEqual(self._lost(), lost0)

    def test_the_batched_drain_writes_every_row(self):
        self._fresh()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "BATCH_MAX", 7):
            db = Path(d) / "usage.db"
            batches = []
            real = usage_ledger._write_rows

            def spy(p, rows, deadline, guard=None):
                batches.append(len(rows))
                return real(p, rows, deadline, guard)
            with mock.patch.object(usage_ledger, "_write_rows", spy):
                for i in range(100):
                    usage_ledger.record_async("codex", "gpt", {"input_tokens": 1}, path=db)
                self.assertTrue(usage_ledger.flush(10))
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage").fetchone()[0], 100)
            self.assertLessEqual(max(batches), 7)
            self.assertEqual(sum(batches), 100)

    def test_a_full_queue_counts_the_lost_rows_and_the_usage_api_reports_them(self):
        from cortados_router.provider import usage as usage_view
        w = self._fresh(maxsize=2, start=False)                         # no writer: the queue stays full
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}):
            with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                results = [usage_ledger.record_async("codex", "gpt", {}) for _ in range(5)]
                self.assertEqual(results, [True, True, False, False, False])
                out = usage_view.collect(state_dir=Path(d), scarcity_reader=lambda: {}, refusals=[],
                                         s1_probe=lambda u: {"available": False}, now=time.time())
            self.assertEqual(w.lost, 3)
            self.assertTrue(any("LOST" in m and "queue is full" in m for m in cm.output))
            self.assertEqual(out["ledger"]["lost"], 3)
            self.assertEqual(out["ledger"]["queued"], 2)
            while not w.q.empty():                                      # not counted by the cleanup shutdown
                w.q.get_nowait()
                w.q.task_done()

    def test_a_full_queue_context_row_is_logged_at_error_with_lane_and_context(self):
        w = self._fresh(maxsize=1, start=False)
        w.q.put_nowait(None)
        self.assertFalse(usage_ledger.record_async("claude#b", "m", {}, context="search lane, run r1"))
        with self.assertLogs("cortados.usage_ledger", "ERROR") as cm:
            w.fold_drops()
        self.assertIn("lane claude#b", cm.output[0])
        self.assertIn("search lane, run r1", cm.output[0])
        w.q.get_nowait()
        w.q.task_done()

    def test_a_start_failure_until_the_queue_fills_still_recovers(self):
        # Review r13 P2-1: the writer cannot start until the queue is full; the supervisor (not a request) keeps
        # retrying, and once a start succeeds the stranded rows are written. Rows refused while full are counted lost.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "SUPERVISE_S", 0.05):
            db = Path(d) / "usage.db"
            w = self._fresh(maxsize=3, start=False)
            real, allow = w._start_writer_thread, {"ok": False}

            def start_writer():
                if not allow["ok"]:
                    raise RuntimeError("can't start new thread")
                return real()
            w._start_writer_thread = start_writer
            with self.assertLogs("cortados.usage_ledger", "WARNING"):
                self.assertTrue(w.start())
                results = [usage_ledger.record_async("codex", "gpt", {"input_tokens": i}, path=db) for i in range(5)]
                self.assertEqual(results, [True, True, True, False, False])
                time.sleep(0.2)                                          # several failed starts
                self.assertFalse(db.exists())
                allow["ok"] = True
                self.assertTrue(w.flush(10))
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage").fetchone()[0], 3)
            self.assertEqual(w.lost, 2)
            self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db), "accepting rows again")
            self.assertTrue(w.flush(5))

    def test_record_async_never_waits_on_the_lifecycle_lock(self):
        # Review r13 P2-2: the request path takes no lifecycle lock; with Writer.lock held elsewhere it still returns
        w = self._fresh(maxsize=1, start=False)
        done = []
        with w.lock:
            t = threading.Thread(target=lambda: done.append(
                [usage_ledger.record_async("codex", "gpt", {}, path=Path("/nonexistent/u.db")) for _ in range(3)]))
            t.start()
            t.join(1.0)
            self.assertFalse(t.is_alive(), "record_async blocked on the writer's lock")
        self.assertEqual(done, [[True, False, False]])
        w.q.get_nowait()
        w.q.task_done()
        w.fold_drops()

    def test_shutdown_during_a_batch_write_commits_it_or_counts_it_lost_exactly_once(self):
        # Review r13 P2-3: shutdown while the writer is inside a batch's write. Either the commit lands (lost 0) or
        # shutdown revokes the batch, its commit rolls back and its result is ignored (lost 1, no row) - never both.
        for release_first in (False, True):
            with self.subTest(release_first=release_first), tempfile.TemporaryDirectory() as d:
                db = Path(d) / "usage.db"
                w = self._fresh()
                entered, release = threading.Event(), threading.Event()
                real = usage_ledger._write_rows

                def slow(p, rows, deadline, guard=None):
                    entered.set()
                    release.wait(5)
                    return real(p, rows, deadline, guard)
                with mock.patch.object(usage_ledger, "_write_rows", slow):
                    usage_ledger.record_async("codex", "gpt", {"input_tokens": 1}, path=db)
                    self.assertTrue(entered.wait(5))
                    if release_first:
                        release.set()
                        self.assertTrue(w.flush(5))
                    try:
                        n = w.shutdown(0)
                        # The daemon may never resume at process exit: disposition must already be final.
                        self.assertEqual(w.stats(), {"queued": 0, "lost": 0 if release_first else 1})
                        self.assertEqual(n, 0 if release_first else 1)
                        self.assertEqual(w.q.unfinished_tasks, 0)
                    finally:
                        release.set()
                        self._join_writer(w)
                rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage").fetchone()[0] if db.exists() else 0
                self.assertEqual(rows + w.lost, 1, f"rows={rows} lost={w.lost}: exactly one disposition")
                self.assertEqual(n, w.lost)
                self.assertEqual(rows, 1 if release_first else 0)

    def test_shutdown_of_a_failing_batch_counts_it_once(self):
        # the reproduced lost=2: a batch that keeps failing (non-busy) while shutdown runs is counted once
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_WRITER_BACKOFF_S", 0.2):
            bad = Path(d) / "usage.db"
            bad.mkdir()
            w = self._fresh()
            with self.assertLogs("cortados.usage_ledger", "WARNING"):
                usage_ledger.record_async("claude#b", "m", {}, path=bad)
                time.sleep(0.1)                                          # inside its retries
                w.shutdown(0.05)
                self._join_writer(w)
            self.assertEqual(w.lost, 1)

    def test_shutdown_between_taking_rows_and_writing_counts_every_path_before_resume(self):
        # r14-2: the old test expected zero until resuming the daemon, masking the process-exit loss.
        # Two paths are claimed together; a third row remains queued. All three must be disposed before resume.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "BATCH_MAX", 2):
            paths = [Path(d) / f"usage-{i}.db" for i in range(3)]
            w = self._fresh(start=False)
            taken, release = threading.Event(), threading.Event()

            def pause():
                taken.set()
                release.wait(5)
            w._after_take = pause
            for db in paths:
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
            self.assertTrue(w.start())
            try:
                self.assertTrue(taken.wait(5))
                self.assertEqual(w.stats(), {"queued": 3, "lost": 0})
                with self.assertLogs("cortados.usage_ledger", "WARNING"):
                    self.assertEqual(w.shutdown(0), 3)
                self.assertEqual(w.stats(), {"queued": 0, "lost": 3})
                self.assertEqual(w.q.unfinished_tasks, 0)
                self.assertEqual(w.shutdown(0), 0, "a second shutdown cannot recount revoked work")
                self.assertFalse(any(db.exists() for db in paths))
            finally:
                release.set()
                self._join_writer(w)
            self.assertFalse(w.writer.is_alive())
            self.assertEqual(w.stats(), {"queued": 0, "lost": 3})
            self.assertFalse(any(db.exists() for db in paths))

    def test_put_waiting_for_queue_admission_cannot_enqueue_after_shutdown(self):
        # r14-1: pause exactly before taking the admission mutex. The old code had already checked closed here.
        w = self._fresh(start=False)
        item = (Path("/nonexistent/usage.db"), usage_ledger._row("codex", "gpt", {}, None), None)
        self.assertTrue(w.put(item, "codex", None))
        entered, release = threading.Event(), threading.Event()
        condition = w.q.not_full

        class AdmissionGate:
            def __enter__(self):
                entered.set()
                release.wait(5)
                return condition.__enter__()

            def __exit__(self, *args):
                return condition.__exit__(*args)

            def __getattr__(self, name):
                return getattr(condition, name)

        results = []
        with mock.patch.object(w.q, "not_full", AdmissionGate()):
            producer = threading.Thread(target=lambda: results.append(w.put(item, "codex", None)))
            producer.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertLogs("cortados.usage_ledger", "WARNING"):
                    self.assertEqual(w.shutdown(0), 1)
                self.assertEqual(w.stats(), {"queued": 0, "lost": 1})
                self.assertEqual(w.q.unfinished_tasks, 0)
            finally:
                release.set()
                producer.join(5)
        self.assertFalse(producer.is_alive())
        self.assertEqual(results, [False])
        # The post-closure attempt is rejected and counted immediately, not orphaned on a stopped supervisor.
        self.assertEqual(w.lost, 2)
        self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
        self.assertEqual(w.shutdown(0), 0)

    def test_failed_batch_is_counted_before_its_retirement_log_can_pause(self):
        # r14-3: no claimed/queued work remains when logging starts. Loss must already be counted at that point.
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "NON_BUSY_TRIES", 1):
            w = self._fresh(start=False)
            entered, release = threading.Event(), threading.Event()
            real_log = w._log_lost

            def pause_log(*args):
                entered.set()
                release.wait(5)
                real_log(*args)

            w._log_lost = pause_log
            with mock.patch.object(usage_ledger, "_write_rows", side_effect=OSError("synthetic disk failure")), \
                    self.assertLogs("cortados.usage_ledger", "WARNING"):
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=Path(d) / "usage.db"))
                self.assertTrue(w.start())
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(w.shutdown(0), 0, "the failed batch was already counted")
                    self.assertEqual(w.lost, 1)
                    self.assertEqual(w.stats(), {"queued": 0, "lost": 1})
                    self.assertEqual(w.claimed, [])
                    self.assertEqual(w.q.unfinished_tasks, 0)
                finally:
                    release.set()
                    self._join_writer(w)
            self.assertFalse(w.writer.is_alive())
            self.assertEqual(w.stats(), {"queued": 0, "lost": 1})

    def test_shutdown_claims_failed_batch_paused_before_retirement(self):
        # The other side of r14-3: shutdown wins BEFORE retirement, so the writer must not count it again.
        w = self._fresh(start=False)
        entered, release = threading.Event(), threading.Event()

        def failed(b):
            entered.set()
            release.wait(5)
            return "synthetic disk failure"

        with mock.patch.object(w, "_write_batch", failed):
            self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=Path("/nonexistent/usage.db")))
            self.assertTrue(w.start())
            try:
                self.assertTrue(entered.wait(5))
                with self.assertLogs("cortados.usage_ledger", "WARNING"):
                    self.assertEqual(w.shutdown(0), 1)
                self.assertEqual(w.stats(), {"queued": 0, "lost": 1})
                self.assertEqual(w.q.unfinished_tasks, 0)
            finally:
                release.set()
                self._join_writer(w)
        self.assertFalse(w.writer.is_alive())
        self.assertEqual(w.stats(), {"queued": 0, "lost": 1})

    def test_deferred_rejection_log_cannot_hide_losses_from_shutdown(self):
        w = self._fresh(maxsize=1, start=False)
        item = (Path("/nonexistent/usage.db"), usage_ledger._row("codex", "gpt", {}, None), None)
        self.assertTrue(w.put(item, "codex", None))
        self.assertFalse(w.put(item, "codex", None))
        self.assertEqual(w.lost, 1, "a rejected row is counted on the request, without Writer.lock")
        entered, release = threading.Event(), threading.Event()
        real_log = w._log_lost

        def pause_log(n, why, contexts, total):
            if "queue is full" in why:
                entered.set()
                release.wait(5)
            real_log(n, why, contexts, total)

        with mock.patch.object(w, "_log_lost", pause_log), self.assertLogs("cortados.usage_ledger", "WARNING"):
            logger = threading.Thread(target=w.fold_drops)
            logger.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(w.shutdown(0), 1)
                self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
                self.assertEqual(w.q.unfinished_tasks, 0)
            finally:
                release.set()
                logger.join(5)
        self.assertFalse(logger.is_alive())
        self.assertEqual(w.stats(), {"queued": 0, "lost": 2})

    def test_shutdown_counts_what_is_left_in_the_queue(self):
        w = self._fresh(maxsize=10, start=False)
        for _ in range(4):
            self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=Path("/nonexistent/usage.db")))
        with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
            self.assertEqual(w.shutdown(timeout=0.05), 4)
        self.assertEqual(w.lost, 4)
        self.assertIn("exited with rows still queued", cm.output[0])
        self.assertEqual(w.stats()["queued"], 0)
        self.assertFalse(usage_ledger.record_async("codex", "gpt", {}), "a shut-down writer accepts nothing")

    def test_a_non_busy_failure_counts_the_batch_lost_after_its_tries(self):
        w = self._fresh()
        logged = threading.Event()
        real_log = w._log_lost

        def log(*args):
            real_log(*args)
            logged.set()

        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_WRITER_BACKOFF_S", 0.01), \
                mock.patch.object(w, "_log_lost", log):
            bad = Path(d) / "usage.db"
            bad.mkdir()                                     # a directory where the db must go
            lost0 = self._lost()
            with self.assertLogs("cortados.usage_ledger", "ERROR") as cm:
                usage_ledger.record_async("claude#b", "m", {}, path=bad, context="search lane, run r2")
                self.assertTrue(usage_ledger.flush(5))
                self.assertTrue(logged.wait(5), "flush guarantees disposition; logging happens afterward")
            self.assertEqual(self._lost() - lost0, 1)
            self.assertTrue(any("LOST" in m and "search lane, run r2" in m for m in cm.output))

    def test_unexpected_error_only_loses_uncommitted_paths(self):
        # A multi-path drain may already have committed some rows when handling a later path fails.
        with tempfile.TemporaryDirectory() as d:
            w = self._fresh(start=False)
            paths = [Path(d) / f"usage-{i}.db" for i in range(3)]
            real_write = w._write_batch
            entered, release = threading.Event(), threading.Event()

            def write(b):
                if b.path == paths[1]:
                    entered.set()
                    release.wait(5)
                    raise RuntimeError("synthetic writer error")
                return real_write(b)

            for db in paths:
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
            with mock.patch.object(w, "_write_batch", write), self.assertLogs("cortados.usage_ledger", "WARNING"):
                self.assertTrue(w.start())
                try:
                    self.assertTrue(entered.wait(5))
                    with sqlite3.connect(paths[0]) as c:
                        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
                    self.assertEqual(w.stats(), {"queued": 2, "lost": 0})
                    self.assertEqual(w.shutdown(0), 2)
                    self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
                    self.assertEqual(w.q.unfinished_tasks, 0)
                finally:
                    release.set()
                    self._join_writer(w)
            self.assertFalse(w.writer.is_alive())
            self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
            self.assertFalse(any(db.exists() for db in paths[1:]))

    def test_shutdown_after_commit_before_retirement_does_not_count_a_loss(self):
        # A committed batch can still be claimed. Shutdown must retire its task, not revoke/count it.
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            w = self._fresh(start=False)
            entered, release = threading.Event(), threading.Event()
            real_write = w._write_batch

            def committed(b):
                result = real_write(b)
                entered.set()
                release.wait(5)
                return result

            with mock.patch.object(w, "_write_batch", committed):
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
                self.assertTrue(w.start())
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(len(w.claimed), 1)
                    self.assertTrue(w.claimed[0].done)
                    self.assertEqual(w.q.unfinished_tasks, 1)
                    self.assertEqual(w.shutdown(0), 0)
                    self.assertEqual(w.stats(), {"queued": 0, "lost": 0})
                    self.assertEqual(w.q.unfinished_tasks, 0)
                    with sqlite3.connect(db) as c:
                        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
                finally:
                    release.set()
                    self._join_writer(w)
            self.assertEqual(w.stats(), {"queued": 0, "lost": 0})
            self.assertEqual(w.q.unfinished_tasks, 0)

    def test_writer_error_retires_only_uncommitted_paths_before_shutdown(self):
        # Unlike the shutdown-wins test above, let error cleanup dispose of both remaining paths itself.
        with tempfile.TemporaryDirectory() as d:
            w = self._fresh(start=False)
            paths = [Path(d) / f"usage-{i}.db" for i in range(3)]
            real_write, real_log = w._write_batch, w._log_lost
            entered, release = threading.Event(), threading.Event()

            def write(b):
                if b.path == paths[1]:
                    raise RuntimeError("synthetic writer error")
                return real_write(b)

            def log(n, why, contexts, total):
                if total == 2:
                    entered.set()
                    release.wait(5)
                real_log(n, why, contexts, total)

            for db in paths:
                self.assertTrue(usage_ledger.record_async("codex", "gpt", {}, path=db))
            with mock.patch.object(w, "_write_batch", write), mock.patch.object(w, "_log_lost", log), \
                    self.assertLogs("cortados.usage_ledger", "WARNING"):
                self.assertTrue(w.start())
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(w.shutdown(0), 0, "both losses were counted before their logs")
                    self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
                    self.assertEqual(w.q.unfinished_tasks, 0)
                    self.assertEqual(w.claimed, [])
                    with sqlite3.connect(paths[0]) as c:
                        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 1)
                    self.assertFalse(any(db.exists() for db in paths[1:]))
                finally:
                    release.set()
                    self._join_writer(w)
            self.assertEqual(w.stats(), {"queued": 0, "lost": 2})
            self.assertEqual(w.q.unfinished_tasks, 0)

    def test_background_migration_retries_with_backoff_until_it_succeeds(self):
        calls = {"n": 0}
        real = usage_ledger._ensure_lane_column

        def flaky(c):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return real(c)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_ensure_lane_column", flaky), \
                mock.patch.object(usage_ledger, "_BG_BACKOFF_S", 0.01), \
                self.assertLogs("cortados.usage_ledger", "INFO") as cm:
            db = Path(d) / "usage.db"
            self._legacy(db)
            usage_ledger._background_migrate(db)                # returns only once migrated
            c = sqlite3.connect(db)
            self.assertTrue(usage_ledger._migrated(c))
            c.close()
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sum("retried in" in m for m in cm.output), 2)
        self.assertTrue(any("background migration" in m and "complete" in m for m in cm.output))

    def test_server_hook_skips_unrouted_and_writes_routed(self):
        from cortados_router.provider import server
        with mock.patch.object(usage_ledger, "record_async") as rec:
            server._ledger_turn(mock.Mock(route=None), {"input_tokens": 1})
            rec.assert_not_called()
            server._ledger_turn(mock.Mock(route={"provider": "claude-cli#b", "model": "claude-sonnet-5"}), {"input_tokens": 3})
            rec.assert_called_once_with("claude#b", "claude-sonnet-5", {"input_tokens": 3})


from tests.test_provider import EndpointHarness, READ_TOOL  # noqa: E402  (after the ledger import, as the harness needs it)


class LedgerNeverBreaksATurn(EndpointHarness):
    def test_a_raising_ledger_keeps_history_and_the_next_continuation_succeeds(self):
        from cortados_router.provider import server
        server._ledger_hook_warned.clear()
        with mock.patch.object(server, "_ledger_turn", side_effect=RuntimeError("boom /secret/path")), \
             self.assertLogs("cortados.provider", "WARNING") as cm:
            history = [{"role": "user", "content": "please use tool"}]
            r1 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
            self.assertEqual(r1["stop_reason"], "tool_use")
            use = r1["content"][-1]
            history += [{"role": "assistant", "content": r1["content"]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
            r2 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
            self.assertEqual(r2["stop_reason"], "end_turn")
        ledger_lines = [l for l in cm.output if "usage ledger hook failed" in l]
        self.assertEqual(len(ledger_lines), 1)                     # once per type, not once per turn
        self.assertIn("RuntimeError", ledger_lines[0])
        self.assertNotIn("/secret/path", ledger_lines[0])          # the type only, never the message
        self.assertEqual(len(self.reg.sessions), 1)


class Pools(unittest.TestCase):
    def tearDown(self):
        import cortados_router.unitcost as uc
        importlib.reload(uc)

    def test_per_account_pools(self):
        with tempfile.TemporaryDirectory() as d:
            uc = _unitcost(d)
            self.assertEqual(uc.pool_of("claude", "claude-sonnet-5"), "claude:all")
            self.assertEqual(uc.pool_of("claude", "claude-sonnet-5", "claude"), "claude:all")
            self.assertEqual(uc.pool_of("claude", "claude-sonnet-5", "claude#b"), "claude#b:all")
            self.assertEqual(uc.pool_of("claude", "fable-1", "claude#b"), "claude#b:fable")
            self.assertEqual(uc.window_pool("claude#b", "week (all models)"), "claude#b:all")
            self.assertEqual(uc.window_pool("codex", "week"), "codex:all")

    def test_learn_tokens_per_pct_from_history_and_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            uc = _unitcost(d)
            t0 = time.time() - 3 * 3600
            hist = []
            for i in range(6):        # account a: +2 %/hour; account b: +1 %/hour
                hist.append({"ts": t0 + i * 1800, "lanes": {"claude": {"windows": [["week (all models)", 10 + 2 * i, 0]]},
                                                             "claude#b": {"windows": [["week (all models)", 50 + i, 0]]}}})
            (Path(d) / "scarcity-history.jsonl").write_text("\n".join(json.dumps(h) for h in hist) + "\n")
            db = Path(d) / "usage.db"
            for i in range(1, 6):
                usage_ledger.record("claude", "claude-sonnet-5", {"input_tokens": 1_000_000}, ts=t0 + i * 1800 - 60, path=db)
                usage_ledger.record("claude#b", "claude-sonnet-5", {"input_tokens": 300_000}, ts=t0 + i * 1800 - 60, path=db)
            out = uc.learn()
            self.assertEqual(out["claude:all"]["tokens_per_pct"], 500_000)
            self.assertEqual(out["claude#b:all"]["tokens_per_pct"], 300_000)
            self.assertEqual(out["claude:all"]["remaining_tokens"], (100 - 20) * 500_000)
            self.assertTrue(out["claude:all"]["lower_bound"])
            self.assertNotEqual(out["claude:all"]["confidence"], "good")
            self.assertTrue((Path(d) / "unit-cost.json").exists())


class ScarcityLearnError(unittest.TestCase):
    def tearDown(self):            # reload under the suite's own env so later tests see the module they expect
        import cortados_router.scarcity as sc
        importlib.reload(sc)

    def test_learn_failure_is_logged_and_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CORTADOS_HOME": d, "CORTADOS_SCARCITY_REFRESH": "off"}):
                import cortados_router.scarcity as sc
                sc = importlib.reload(sc)
                self.assertTrue(str(sc.CACHE).startswith(d))
                with mock.patch.object(sc, "SAMPLERS", {"fake": lambda: {"windows": []}}), \
                     mock.patch.object(sc, "claude_lanes", return_value=[]), \
                     mock.patch.object(sc, "_plans", return_value={}), \
                     mock.patch.object(sc, "finish", side_effect=lambda lane, rec: rec), \
                     mock.patch.object(sc._reaper, "sweep_stale", return_value=None), \
                     mock.patch.object(sc, "_learn_unit_costs", side_effect=FileNotFoundError("usage.db")):
                    with self.assertLogs("cortados.scarcity", "WARNING") as cm:
                        out = sc._sample_locked(force=True)
                self.assertIn("FileNotFoundError", cm.output[0])
                self.assertIn("FileNotFoundError", out["learn_error"])
                self.assertIn("FileNotFoundError", json.loads(sc.CACHE.read_text())["learn_error"])


    def test_learn_success_clears_the_error_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"CORTADOS_HOME": d, "CORTADOS_SCARCITY_REFRESH": "off"}):
                import cortados_router.scarcity as sc
                sc = importlib.reload(sc)
                sc.CACHE.parent.mkdir(parents=True, exist_ok=True)
                sc.CACHE.write_text(json.dumps({"fetched_at": 0, "lanes": {}, "learn_error": "OperationalError: old", "learned_at": 0}))
                with mock.patch.object(sc, "SAMPLERS", {"fake": lambda: {"windows": []}}), \
                     mock.patch.object(sc, "claude_lanes", return_value=[]), \
                     mock.patch.object(sc, "_plans", return_value={}), \
                     mock.patch.object(sc, "finish", side_effect=lambda lane, rec: rec), \
                     mock.patch.object(sc._reaper, "sweep_stale", return_value=None), \
                     mock.patch.object(sc, "_learn_unit_costs", return_value=None):
                    out = sc._sample_locked(force=True)
                disk = json.loads(sc.CACHE.read_text())
                self.assertIsNone(out["learn_error"])
                self.assertIsNone(disk["learn_error"])
                self.assertGreater(disk["learned_at"], 0)          # persisted, so the 6-hour gate holds
                self.assertIn("fake", disk["lanes"])


class NoLedgerYet(unittest.TestCase):
    def test_learn_with_no_usage_db_or_no_usage_table_is_no_data(self):
        for make in (False, True):
            with self.subTest(empty_db=make), tempfile.TemporaryDirectory() as d:
                if make:
                    sqlite3.connect(Path(d) / "usage.db").close()    # a file with no `usage` table
                uc = _unitcost(d)
                uc.learn()                                           # must not raise
                self.assertEqual(make, (Path(d) / "usage.db").exists())   # learn never creates the ledger


if __name__ == "__main__":
    unittest.main()
