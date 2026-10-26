"""The committed example fleet config (tables/resources.example.json) documents the home cluster — Studio, RTX 3060 PC,
Air, Pro — and must stay valid for CORTADOS_LOCAL_RESOURCES_CONFIG. The real tables/resources.json is git-ignored (it
holds SSH targets / tailnet addresses); this example ships placeholders only."""
import json
import sys
import tempfile
import unittest
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

    def test_sampler_rejects_duplicate_ids_before_any_network(self):
        bad = {"max_age_s": 120, "deployments": [],
               "nodes": [{"id": "x", "target": "local"}, {"id": "x", "target": "local"}]}
        path = Path(tempfile.mkdtemp()) / "bad.json"; path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):                              # guard fires before the SSH sampler runs
            local_resources.sample(str(path))


if __name__ == "__main__":
    unittest.main()
