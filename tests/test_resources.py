"""The resource-pool snapshot (cortados_router/resources.py): model-fit estimate, the non-blocking local-telemetry
cache, and the aggregator. No network and no real state — core is a fake and the device sampler is injected."""
import os
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from cortados_router import resources  # noqa: E402

GIB = 2 ** 30
MODELS = [{"id": "a", "approx_gib": 2.5}, {"id": "b", "approx_gib": 8.5}, {"id": "c", "approx_gib": 40.0}]


class ModelFitTests(unittest.TestCase):
    def test_fit_by_headroom_biggest_first(self):
        self.assertEqual([m["id"] for m in resources.models_that_fit(10 * GIB, MODELS)], ["b", "a"])

    def test_none_headroom_fits_nothing(self):
        self.assertEqual(resources.models_that_fit(None, MODELS), [])

    def test_huge_headroom_fits_all(self):
        self.assertEqual([m["id"] for m in resources.models_that_fit(100 * GIB, MODELS)], ["c", "b", "a"])

    def test_reads_the_committed_table(self):
        self.assertTrue(any(m["id"] == "qwen3-4b-4bit" for m in resources.local_models()))


class LocalCacheTests(unittest.TestCase):
    def setUp(self):
        resources._local.update(at=0.0, data=None, refreshing=False)

    def test_a_sampler_failure_is_stored_as_error_not_raised(self):
        def boom(cfg):
            raise OSError("ssh down")
        self.assertIn("error", resources.refresh_local("cfg", sampler=boom))

    def test_a_good_sample_is_stored(self):
        data = resources.refresh_local("cfg", sampler=lambda cfg: {"nodes": [{"id": "studio"}]})
        self.assertEqual(data["nodes"][0]["id"], "studio")

    def test_no_config_returns_none(self):
        self.assertIsNone(resources.local_snapshot(None))


class FakeCore:
    def usage_summary(self, limit=12):
        return {"total_routes": 3, "by_band": {"R1": 3}, "degraded": {"count": 0, "of": 3}, "recent": []}

    def lane_health(self, *, refresh=True):
        return {"claude": {"ok": True, "scarcity": -0.3, "expiring_pct": 34,
                           "tightest": {"window": "week", "resets_at": "x"}, "why": None}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        # pre-seed a fresh device reading so local_snapshot returns it without sampling
        resources._local.update(at=time.time() + 1e6, refreshing=False,
                                data={"nodes": [{"id": "studio", "role": "coordinator", "status": "online",
                                                 "gpu_headroom_estimate_bytes": 10 * GIB, "gpu_utilization_pct": 12}],
                                      "known_pool_headroom_estimate_bytes": 10 * GIB, "note": "estimate"})

    def test_ties_usage_subscriptions_and_devices(self):
        snap = resources.snapshot(FakeCore(), local_config="dummy", now=time.time())
        self.assertEqual(snap["usage"]["total_routes"], 3)
        self.assertEqual(snap["subscriptions"]["claude"]["expiring_pct"], 34)
        self.assertEqual(snap["devices"]["status"], "ok")
        fits = snap["devices"]["nodes"][0]["fits"]
        self.assertIn("qwen3-8b-4bit", fits)                   # 10 GiB fits the small models
        self.assertNotIn("llama-3.3-70b-4bit", fits)           # but not a 70B

    def test_devices_pending_without_a_fleet_config(self):
        snap = resources.snapshot(FakeCore(), local_config=None)
        self.assertEqual(snap["devices"]["status"], "pending")
        self.assertIsNone(snap["devices"].get("error"))



class CacheOnlyTests(unittest.TestCase):
    def test_scarcity_current_refresh_false_never_samples(self):
        import cortados_router.scarcity as sc
        called = {"n": 0}
        keep_sample, keep_env = sc.sample, os.environ.get("CORTADOS_SCARCITY_REFRESH")
        sc.sample = lambda *a, **k: called.__setitem__("n", called["n"] + 1)
        os.environ["CORTADOS_SCARCITY_REFRESH"] = "on"           # prove refresh=False (not the env switch) blocks sampling
        try:
            sc.current(refresh=False)                            # an empty/stale cache would normally start a background sample
            self.assertEqual(called["n"], 0)
        finally:
            sc.sample = keep_sample
            if keep_env is None:
                os.environ.pop("CORTADOS_SCARCITY_REFRESH", None)
            else:
                os.environ["CORTADOS_SCARCITY_REFRESH"] = keep_env

    def test_lane_health_threads_refresh_false_to_scarcity(self):
        import cortados_router.core as core
        seen = {}
        keep = core._scarcity_module

        class FakeScModule:
            def lane_scarcity(self, *, refresh=True):
                seen["refresh"] = refresh
                return {}
        core._scarcity_module = lambda: FakeScModule()
        try:
            core.lane_health(refresh=False)
            self.assertFalse(seen["refresh"])                    # the dashboard's cache-only read reaches scarcity as refresh=False
        finally:
            core._scarcity_module = keep


class LocalRefreshBehaviorTests(unittest.TestCase):
    def setUp(self):
        resources._local.update(at=0.0, data=None, refreshing=False)

    def test_cold_call_returns_immediately_then_populates_in_background(self):
        gate = threading.Event()

        def sampler(cfg):
            gate.wait(5)
            return {"nodes": [{"id": "studio"}]}
        started = time.time()
        first = resources.local_snapshot("cfg", sampler=sampler)
        self.assertIsNone(first)                                 # sample pending: returns at once, does not join
        self.assertLess(time.time() - started, 1.0)              # and did not block on the slow sampler
        gate.set()
        for _ in range(500):
            if resources._local["data"] is not None:
                break
            time.sleep(0.01)
        self.assertEqual(resources._local["data"]["nodes"][0]["id"], "studio")

    def test_recovers_after_a_failed_refresh(self):
        def boom(cfg):
            raise OSError("ssh down")
        resources.refresh_local("cfg", sampler=boom)
        self.assertFalse(resources._local["refreshing"])         # flag cleared so a later GET retries, not wedged
        self.assertIn("error", resources._local["data"])


class UsageSummaryTests(unittest.TestCase):
    def setUp(self):
        import cortados_router.core as core
        import tempfile
        self.core = core
        self._keep = core.STATE
        self.tmp = Path(tempfile.mkdtemp())
        core.STATE = self.tmp

    def tearDown(self):
        self.core.STATE = self._keep

    def test_missing_db_is_a_clean_empty_summary(self):
        s = self.core.usage_summary()
        self.assertEqual(s["total_routes"], 0)
        self.assertNotIn("error", s)

    def test_db_without_a_routes_table_is_clean_empty_not_an_error(self):
        import sqlite3
        conn = sqlite3.connect(self.tmp / "router.db")
        conn.execute("CREATE TABLE other (x)"); conn.commit(); conn.close()
        s = self.core.usage_summary()
        self.assertEqual(s["total_routes"], 0)
        self.assertNotIn("error", s)                         # a missing routes table is no-data, not an error

    def test_counts_recent_and_bounded_latency_from_real_rows(self):
        import sqlite3
        conn = sqlite3.connect(self.tmp / "router.db")
        conn.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, platform TEXT, band TEXT, lane TEXT, model TEXT, "
                     "degraded INT, engine_ms INT, turn_ms INT, response_chars INT)")
        for i in range(5):
            conn.execute("INSERT INTO routes VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (f"t{i}", float(i), "p", "R1", "codex", "gpt", 0, 100 + i * 10, 200, 300))
        conn.commit(); conn.close()
        s = self.core.usage_summary(limit=3)
        self.assertEqual(s["total_routes"], 5)
        self.assertEqual(s["by_band"], {"R1": 5})
        self.assertEqual(s["by_lane"], {"codex/gpt": 5})
        self.assertEqual(len(s["recent"]), 3)                # bounded by limit
        self.assertEqual(s["s1_latency_ms"]["median"], 120)  # median of 100,110,120,130,140
        self.assertEqual(s["s1_latency_ms"]["window"], 5)

if __name__ == "__main__":
    unittest.main()
