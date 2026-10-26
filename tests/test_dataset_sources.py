"""s1/dataset.py ingests the local shadow-router db plus any synced from other machines via CORTADOS_ROUTER_DB_EXTRA, so
a provider running on another host (which logs to its own ~/.cortados/router.db) can feed training once its db is rsynced
here (Forest 2026-10-26)."""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
import dataset  # noqa: E402


class RouterDbSourcesTests(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("CORTADOS_ROUTER_DB_EXTRA")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CORTADOS_ROUTER_DB_EXTRA", None)
        else:
            os.environ["CORTADOS_ROUTER_DB_EXTRA"] = self._old

    def test_local_db_only_by_default(self):
        os.environ.pop("CORTADOS_ROUTER_DB_EXTRA", None)
        self.assertEqual(dataset._router_dbs(), [dataset.ROUTER_DB])

    def test_extra_dbs_are_appended_and_expanded(self):
        os.environ["CORTADOS_ROUTER_DB_EXTRA"] = "~/.cortados/router-air.db" + os.pathsep + "/tmp/router-studio.db"
        dbs = dataset._router_dbs()
        self.assertEqual(dbs[0], dataset.ROUTER_DB)                       # local always first
        self.assertIn(Path.home() / ".cortados" / "router-air.db", dbs)   # ~ expanded
        self.assertIn(Path("/tmp/router-studio.db"), dbs)

    def test_a_duplicate_path_is_ingested_once(self):
        os.environ["CORTADOS_ROUTER_DB_EXTRA"] = str(dataset.ROUTER_DB) + os.pathsep + str(dataset.ROUTER_DB)
        self.assertEqual(dataset._router_dbs().count(dataset.ROUTER_DB), 1)


if __name__ == "__main__":
    unittest.main()
