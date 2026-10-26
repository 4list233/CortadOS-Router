"""The resource-pool snapshot (cortados_router/resources.py): model-fit estimate, the non-blocking local-telemetry
cache, and the aggregator. No network and no real state — core is a fake and the device sampler is injected."""
import sys
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

    def lane_health(self):
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


if __name__ == "__main__":
    unittest.main()
