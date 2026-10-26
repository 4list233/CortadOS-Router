"""The committed example fleet config (tables/resources.example.json) documents the home cluster — Studio, RTX 3060 PC,
Air, Pro — and must stay valid for CORTADOS_LOCAL_RESOURCES_CONFIG. The real tables/resources.json is git-ignored (it
holds SSH targets / tailnet addresses); this example ships placeholders only."""
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cortados_router import local_resources  # noqa: E402


class ResourcesExampleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "tables" / "resources.example.json").read_text())

    def test_shape(self):
        self.assertIsInstance(self.cfg["nodes"], list)
        self.assertIsInstance(self.cfg["deployments"], list)
        self.assertIsInstance(self.cfg["max_age_s"], int)

    def test_every_node_has_the_fields_the_sampler_reads(self):
        ids = [n["id"] for n in self.cfg["nodes"]]
        for n in self.cfg["nodes"]:
            self.assertIn("target", n)
            self.assertIsInstance(n["pool_eligible"], bool)
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_four_fleet_nodes_including_the_rtx_3060(self):
        byid = {n["id"]: n for n in self.cfg["nodes"]}
        self.assertEqual(set(byid), {"studio", "pc3060", "air", "pro"})
        self.assertEqual(byid["pc3060"]["inventory"]["vram_gib"], 12)    # the RTX 3060 worker
        self.assertEqual(byid["pro"]["target"], "local")                 # the machine running the sampler is 'local'

    def test_ships_placeholders_not_real_addresses(self):
        blob = json.dumps(self.cfg)
        self.assertNotIn("100.", blob)                                   # no tailnet CGNAT addresses committed
        self.assertIn(".local", blob)

    def test_duplicate_ids_are_rejected_before_any_node_is_sampled(self):
        bad = {"max_age_s": 120, "deployments": [],
               "nodes": [{"id": "x", "target": "local"}, {"id": "x", "target": "local"}]}
        path = Path(tempfile.mkdtemp()) / "bad.json"; path.write_text(json.dumps(bad))
        with mock.patch.object(local_resources, "sample_node") as sampled:
            with self.assertRaises(ValueError):
                local_resources.sample(str(path))
            sampled.assert_not_called()                                 # the guard runs BEFORE any node is contacted

    def test_the_committed_example_runs_through_the_sampler_with_mocked_telemetry(self):
        # a structural check alone would pass a null reserve or a wrong-typed target that then crashes the sampler;
        # run the real example through sample() with node telemetry faked, so the whole config path is exercised
        def fake_sample_node(node, source):
            return {"id": node["id"], "role": node.get("role"), "pool_eligible": node.get("pool_eligible", False),
                    "inventory": node.get("inventory", {}), "status": "online", "sampled_at": 0.0,
                    "gpu_headroom_estimate_bytes": 8 * 2**30, "gpu_budget_bytes": 12 * 2**30, "gpu_utilization_pct": 10}
        with mock.patch.object(local_resources, "sample_node", fake_sample_node):
            result = local_resources.sample(str(ROOT / "tables" / "resources.example.json"))
        self.assertEqual(len(result["nodes"]), 4)
        self.assertIn("known_pool_headroom_estimate_bytes", result)


if __name__ == "__main__":
    unittest.main()
