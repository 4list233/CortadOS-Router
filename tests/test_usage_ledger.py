"""Usage ledger (cortados_router/usage_ledger.py), per-account unit-cost pools, and scarcity's visible learn failure."""
import importlib
import json
import os
import sqlite3
import tempfile
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
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}):
            t = usage_ledger.record_async("claude#b", "claude-sonnet-5", {"input_tokens": 4, "output_tokens": 1})
            t.join(5)
            self.assertFalse(t.is_alive())
            rows = sqlite3.connect(Path(d) / "usage.db").execute("SELECT runtime, input_tokens, lane FROM usage").fetchall()
            self.assertEqual(rows, [("claude", 4, "claude#b")])

    def test_record_async_writes_the_row_synchronously_when_the_thread_cannot_start(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}), \
                mock.patch.object(usage_ledger.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("cortados.usage_ledger", "WARNING"):
            self.assertIsNone(usage_ledger.record_async("claude#b", "m", {"input_tokens": 3}, context="search lane, run -"))
            rows = sqlite3.connect(Path(d) / "usage.db").execute("SELECT runtime, lane, input_tokens FROM usage").fetchall()
        self.assertEqual(rows, [("claude", "claude#b", 3)])

    def test_a_failed_synchronous_fallback_write_logs_error_with_lane_and_context(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}), \
                mock.patch.object(usage_ledger.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
            (Path(d) / "usage.db").mkdir()                  # a directory where the db must go: the fallback write fails
            self.assertIsNone(usage_ledger.record_async("claude#b", "m", {}, context="search lane, run search-run-0001"))
        errors = [r.getMessage() for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("lane claude#b", errors[0])
        self.assertIn("search lane, run search-run-0001", errors[0])

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

    def test_thread_start_failure_with_the_migrate_lock_held_returns_within_the_deadline_and_logs_error(self):
        # The reviewer's case: no worker thread (the fallback runs on the request thread) and the migrate lock held by
        # another process on a ledger that still needs its lane column. record must not block on the lock.
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CORTADOS_HOME": d}), \
                mock.patch.object(usage_ledger, "RETRY_S", 1.0):
            db = Path(d) / "usage.db"
            self._legacy(db)
            self._hold_migrate_lock(db)
            usage_ledger._consecutive_failures = 0
            t0 = time.monotonic()
            with mock.patch.object(usage_ledger.threading.Thread, "start", side_effect=RuntimeError("can't start new thread")), \
                    self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                self.assertIsNone(usage_ledger.record_async("claude#b", "m", {}, context="search lane, run search-run-0001"))
            took = time.monotonic() - t0
        self.assertLess(took, 1.0 + 0.75, "bounded by RETRY_S plus a small margin, not by the lock holder (60 s)")
        errors = [r.getMessage() for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("lane claude#b", errors[0])
        self.assertIn("search lane, run search-run-0001", errors[0])
        self.assertEqual(usage_ledger.consecutive_failures(), 1, "the failure is counted")

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

    def test_rows_written_while_a_long_background_build_holds_the_writer_lock_land_exactly_once(self):
        # Review r11: the background CREATE INDEX holds SQLite's one writer lock longer than RETRY_S, on a populated
        # ledger. Writes in that window cannot reach the db by their deadline: they are spooled, and the end of the
        # migration replays them. Every row lands exactly once.
        import threading as th
        real_build = usage_ledger._build_index
        building, release = th.Event(), th.Event()

        def build(c):
            if th.current_thread().name != "cortados-usage-ledger-migrate":
                c.execute("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT count(*) FROM r").fetchone()
                return                                          # never reached: interrupted at the foreground limit
            c.execute("BEGIN IMMEDIATE")                        # the build's writer lock, held past every deadline
            c.execute(usage_ledger._TS_INDEX)
            building.set()
            release.wait(10)
            c.commit()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(usage_ledger, "_build_index", build), \
                mock.patch.object(usage_ledger, "RETRY_S", 0.4), mock.patch.object(usage_ledger, "_BG_BACKOFF_S", 0.05), \
                mock.patch.object(usage_ledger, "_bg_started", set()):
            db = Path(d) / "usage.db"
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
            c.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?,?)",
                          ((float(i), "claude", "old", 1, 1, 0, 0) for i in range(50000)))
            c.commit()
            c.close()
            # the triggering row: committed before the background build starts
            self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": 1000}, ts=1e9, path=db))
            self.assertTrue(building.wait(10), "the background build started")
            with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                for i in range(1, 4):                           # each outlasts RETRY_S behind the build: spooled
                    self.assertFalse(usage_ledger.record("claude#b", "m", {"input_tokens": 1000 + i}, ts=1e9 + i, path=db,
                                                         context="search lane, run -"))
            self.assertEqual(sum("spooled" in m for m in cm.output), 3)
            self.assertFalse(any("LOST" in m for m in cm.output))
            release.set()
            until = time.monotonic() + 10
            got = []
            while time.monotonic() < until:
                c = sqlite3.connect(db)
                try:
                    got = c.execute("SELECT input_tokens FROM usage WHERE ts >= 1e9 ORDER BY ts").fetchall()
                    done = usage_ledger._migrated(c)
                finally:
                    c.close()
                if done and len(got) == 4:
                    break
                time.sleep(0.05)
            self.assertEqual(got, [(1000,), (1001,), (1002,), (1003,)], "every row exactly once")
            c = sqlite3.connect(db)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0], 50000 + 4)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM usage_spool_applied").fetchone()[0], 0)
            c.close()
            self.assertEqual(list(Path(d).glob("usage-spool.jsonl*.replay")), [])
            # a later write replays nothing again: still exactly once
            self.assertTrue(usage_ledger.record("claude#b", "m", {"input_tokens": 2000}, ts=2e9, path=db))
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage WHERE ts >= 1e9").fetchone()[0], 5)

    def test_a_replay_interrupted_after_its_insert_does_not_double_count(self):
        # crash between the insert transaction and the pending file's removal: the next replay must insert nothing
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "usage.db"
            self.assertTrue(usage_ledger.record("claude", "m", {}, ts=1.0, path=db))
            usage_ledger._spool(db, (5.0, "claude", "m", 7, 0, 0, 0, "claude#b"))
            real_unlink = Path.unlink
            with mock.patch.object(Path, "unlink", side_effect=OSError("crash")), self.assertRaises(OSError):
                usage_ledger._replay_locked(db, None)
            self.assertEqual(usage_ledger._replay_locked(db, None), 0, "the applied uid makes the second pass a no-op")
            self.assertEqual(sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage WHERE input_tokens = 7").fetchone()[0], 1)
            self.assertEqual(list(Path(d).glob("usage-spool.jsonl*")), [])
            del real_unlink

    def test_a_row_whose_spool_append_fails_too_is_logged_lost(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "dir-not-db"
            bad.mkdir()
            usage_ledger._consecutive_failures = 0
            with mock.patch.object(usage_ledger, "_spool", side_effect=OSError("disk full")), \
                    self.assertLogs("cortados.usage_ledger", "ERROR") as cm:
                self.assertFalse(usage_ledger.record("claude#b", "m", {}, path=bad, context="search lane, run r1"))
            self.assertIn("LOST", cm.output[0])
            self.assertIn("lane claude#b", cm.output[0])

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
