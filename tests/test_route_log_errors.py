"""Issue #99: a failure to LOG a route never fails the routed request, is counted, and is visible in the usage and live
views; and the cause (same-process `mode=ro` readers of the rollback-journal router.db breaking the writer's locks with
SQLITE_IOERR_LOCK / EBADF) is gone from every in-process reader.

Throwaway state only: core.STATE points at a fresh temp dir; lane health and S1 are stubbed (no vendor CLI is opened).
"""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cortados_router import core, router_db
from cortados_router.provider import live, routing, tasks, usage

EASY = {"steps": 0, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
        "outward": False, "needs_clarification": False, "diversity": False, "work_kind": "code", "urgency": "now", "parts": 0}
DECISION = {"answers": None, "degraded": False, "engine": "s1-test", "engine_ms": 1}
ROUTE = {"band": "R1", "chosen": {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra"},
         "answers": None, "subagents": 0, "verifier": False, "hard_rules": []}


class _FailingConn:
    """A router.db connection whose writes fail like the Air's did: OperationalError('disk I/O error')."""

    def __init__(self, real):
        self.real = real

    def execute(self, sql, *a):
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE")):
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *a)

    def commit(self):
        self.real.commit()

    def close(self):
        self.real.close()


class Base(unittest.TestCase):
    def setUp(self):
        self._keep = (core.STATE, core.lane_health, core.decide, core._db, core.ROUTE_LOG_RETRY_S)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-route-log-"))   # never the live router.db
        core.lane_health = lambda: {}
        core.decide = lambda text, ctx=None: {"answers": dict(EASY), "degraded": False, "engine": "s1-test", "engine_ms": 1}
        core.ROUTE_LOG_RETRY_S = 0.001
        routing.execution_routing._scores.clear()
        router_db._reset()

    def tearDown(self):
        core.STATE, core.lane_health, core.decide, core._db, core.ROUTE_LOG_RETRY_S = self._keep
        router_db._reset()      # the live fixtures read router_db.stats(): leave a clean process behind

    def fail_writes(self, attempts):
        """The next `attempts` router.db opens hand out a connection whose writes fail; later ones are real."""
        real_db, left = core._db, [attempts]

        def db():
            c = real_db()
            if left[0] > 0:
                left[0] -= 1
                return _FailingConn(c)
            return c
        core._db = db

    def rows(self):
        with sqlite3.connect(core.STATE / "router.db") as c:
            return c.execute("SELECT COUNT(*) FROM routes").fetchone()[0]

    def choose(self, text="rename a variable"):
        body = {"model": "cortados", "messages": [{"role": "user", "content": text}]}
        return routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin="python3",
                              codex_bin="python3")


class RouteLogFailureTests(Base):
    def test_a_failing_route_log_does_not_fail_the_routed_request(self):
        core._db()                                   # create the schema first, then every write fails
        self.fail_writes(10)
        r = self.choose()
        self.assertNotIn("error", r)                 # the request is routed (no 503 "routing failed")
        self.assertTrue(r.get("model"))
        s = router_db.stats()
        self.assertEqual((s["count"], s["lost"], s["recovered"]), (2, 1, 0))   # the attempt and its one retry
        self.assertEqual((s["last_error_type"], s["last_op"]), ("OperationalError", "log_route"))
        self.assertIsInstance(s["last_error_at"], float)
        self.assertEqual(self.rows(), 0)

    def test_the_decision_is_the_same_with_and_without_a_failing_log(self):
        ok = self.choose()
        router_db._reset()
        self.fail_writes(10)
        failed = self.choose()
        for k in ("provider", "model", "band"):
            self.assertEqual(ok.get(k), failed.get(k), k)

    def test_pinned_route_survives_a_failing_log(self):
        core._db()
        self.fail_writes(10)
        body = {"model": "sub/opus", "messages": [{"role": "user", "content": "hi"}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin="python3")
        self.assertNotIn("error", r)
        self.assertEqual(router_db.stats()["last_op"], "log_route")

    def test_one_retry_recovers_a_transient_failure(self):
        core._db()
        self.fail_writes(1)
        self.assertTrue(core.log_route("t1", {"request": "x"}, DECISION, ROUTE))
        self.assertEqual(self.rows(), 1)
        s = router_db.stats()
        self.assertEqual((s["count"], s["lost"], s["recovered"]), (1, 0, 1))

    def test_log_route_returns_false_and_never_raises(self):
        core._db()
        self.fail_writes(2)
        self.assertFalse(core.log_route("t1", {"request": "x"}, DECISION, ROUTE))

    def test_log_execution_still_raises_after_its_retry_and_is_counted(self):
        # the provider retries an execution log on the conversation's next turn, so it keeps seeing the failure
        core.log_route("t1", {"request": "x"}, DECISION, ROUTE)
        self.fail_writes(2)
        with self.assertRaises(sqlite3.OperationalError):
            core.log_execution("t1", "codex-cli", "gpt-5.6-terra")
        s = router_db.stats()
        self.assertEqual((s["count"], s["lost"], s["last_op"]), (2, 1, "log_execution"))

    def test_the_warning_is_rate_limited(self):
        core._db()
        self.fail_writes(10)
        with self.assertLogs("cortados.router_db", "WARNING") as logs:
            for i in range(4):
                core.log_route(f"t{i}", {"request": "x"}, DECISION, ROUTE)
        self.assertEqual(len(logs.output), 1)        # 8 failed attempts, one line
        self.assertIn("OperationalError", logs.output[0])
        self.assertIn("disk I/O error", logs.output[0])
        self.assertEqual(router_db.stats()["count"], 8)


class ViewTests(Base):
    def failing(self):
        core._db()
        self.fail_writes(2)
        core.log_route("t1", {"request": "x"}, DECISION, ROUTE)

    def test_usage_view_publishes_the_counter(self):
        self.failing()
        out = usage.collect(state_dir=core.STATE, scarcity_reader=lambda: {}, refusals=[],
                            s1_probe=lambda url: {"available": True, "latency_ms": 3}, now=time.time())
        rl = out["routed"]["route_log_errors"]
        self.assertEqual((rl["count"], rl["lost"], rl["recovered"]), (2, 1, 0))
        self.assertEqual((rl["last_error_type"], rl["last_op"]), ("OperationalError", "log_route"))

    def test_usage_view_publishes_the_counter_without_a_db(self):
        out = usage.collect(state_dir=Path(tempfile.mkdtemp()), scarcity_reader=lambda: {}, refusals=[],
                            s1_probe=lambda url: {"available": True, "latency_ms": 3}, now=time.time())
        self.assertEqual(out["routed"]["route_log_errors"]["count"], 0)

    def test_live_view_publishes_the_counter(self):
        self.failing()
        now = time.time()
        snap = live.collect(now=now, child_events=lambda: [], office_view=lambda ev: {},
                            tasks_view=lambda: tasks.collect(core.STATE), usage_view=lambda: {},
                            heartbeats=lambda: {}, host_names=[])
        rl = snap["routed"]["route_log_errors"]
        self.assertEqual((rl["count"], rl["lost"], rl["last_error_type"], rl["last_op"]),
                         (2, 1, "OperationalError", "log_route"))
        self.assertEqual(snap["overview"]["route_log_errors"], 2)

    def test_live_view_revalidates_the_counter(self):
        bad = {"count": -3, "lost": "x", "recovered": True, "last_error_type": "/Users/someone/.cortados/router.db",
               "last_error_at": float("nan"), "last_op": "DROP TABLE"}
        self.assertEqual(live.route_log_errors(bad), {"count": 0, "lost": 0, "recovered": 0, "last_error_type": None,
                                                      "last_error_at": None, "last_op": None})
        snap = live.collect(now=time.time(), child_events=lambda: [], office_view=lambda ev: {},
                            tasks_view=lambda: {}, usage_view=lambda: {}, heartbeats=lambda: {}, host_names=[],
                            route_log=lambda: 1 / 0)
        self.assertIsNone(snap["routed"]["route_log_errors"])
        self.assertEqual(snap["routed"]["errors"][-1]["source"], "route log")


class RootCauseTests(Base):
    """The readers that share the provider process with core.log_route must not open router.db with mode=ro."""

    def seed(self, n=50):
        for i in range(n):
            core.log_route(f"seed{i}", {"request": "x" * 200, "session_id": "s"}, DECISION, ROUTE)

    def test_no_in_process_reader_opens_router_db_read_only(self):
        self.seed(3)
        seen, real = [], sqlite3.connect

        def spy(database, *a, **kw):
            seen.append(str(database))
            return real(database, *a, **kw)
        sqlite3.connect = spy
        try:
            tasks.collect(core.STATE)
            usage._routed(core.STATE / "router.db", time.time())
            core.usage_summary()
        finally:
            sqlite3.connect = real
        router_opens = [d for d in seen if "router.db" in d]
        self.assertEqual(len(router_opens), 3, seen)
        for d in router_opens:
            self.assertNotIn("mode=ro", d)
            self.assertIn("mode=rw", d)

    def test_reader_refuses_writes_and_never_creates_the_db(self):
        self.seed(1)
        c = router_db.reader(core.STATE / "router.db", 1)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("DELETE FROM routes")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM routes").fetchone()[0], 1)
        finally:
            c.close()
        missing = Path(tempfile.mkdtemp()) / "router.db"
        with self.assertRaises(sqlite3.OperationalError):
            router_db.reader(missing, 1)
        self.assertFalse(missing.exists())

    def test_route_logging_survives_concurrent_in_process_readers(self):
        # The #99 scenario: the dashboard / live polls read router.db in the provider process while it logs routes.
        # With mode=ro readers this produced SQLITE_IOERR_LOCK (EBADF) on most writes within a second on macOS.
        self.seed(50)
        stop = time.monotonic() + 1.5
        reader_errors = []

        def read():
            while time.monotonic() < stop:
                try:
                    v = tasks.collect(core.STATE)
                    reader_errors.extend(e["error"] for e in v["errors"] if e["error"] != "OperationalError")
                    usage._routed(core.STATE / "router.db", time.time())
                except sqlite3.OperationalError:
                    pass                             # a busy / interrupted read is the view's problem, not the writer's
        readers = [threading.Thread(target=read) for _ in range(3)]
        for t in readers:
            t.start()
        written = 0
        while time.monotonic() < stop:
            core.log_route(f"w{written}", {"request": "y" * 500, "session_id": "s"}, DECISION, ROUTE)
            written += 1
        for t in readers:
            t.join()
        self.assertGreater(written, 20)
        self.assertEqual(router_db.stats()["count"], 0, router_db.stats())
        self.assertEqual(reader_errors, [])


if __name__ == "__main__":
    unittest.main()
