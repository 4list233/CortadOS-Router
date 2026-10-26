"""s1/dataset.py ingests the local shadow-router db plus any synced from other machines via CORTADOS_ROUTER_DB_EXTRA, so a
provider on another host (which logs to its own ~/.cortados/router.db) feeds training once its db is rsynced here. Paths
are resolved (a relative extra path must not crash as_uri()), rows from a non-local db are source-tagged so a shared
turn_id does not silently drop one, and an unreadable source is skipped rather than aborting the collection."""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
import dataset  # noqa: E402


def _make_db(path: Path, turn_id="abc", request="do the thing", platform="provider:claude-cli"):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, unit_kind TEXT, platform TEXT, request TEXT)")
    c.execute("INSERT INTO routes VALUES (?,?,?,?,?)", (turn_id, 1.0, "interactive", platform, request))
    c.commit(); c.close()


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
        self.assertEqual(dataset._router_dbs(), [dataset.ROUTER_DB.resolve()])

    def test_extra_dbs_are_appended_resolved_and_absolute(self):
        os.environ["CORTADOS_ROUTER_DB_EXTRA"] = "~/.cortados/router-air.db" + os.pathsep + "/tmp/router-studio.db"
        dbs = dataset._router_dbs()
        self.assertEqual(dbs[0], dataset.ROUTER_DB.resolve())                     # local always first
        self.assertIn((Path.home() / ".cortados" / "router-air.db").resolve(), dbs)
        self.assertIn(Path("/tmp/router-studio.db").resolve(), dbs)
        self.assertTrue(all(p.is_absolute() for p in dbs))

    def test_a_relative_extra_path_is_resolved_not_crashing(self):
        os.environ["CORTADOS_ROUTER_DB_EXTRA"] = "synced/router.db"
        dbs = dataset._router_dbs()
        self.assertTrue(dbs[-1].is_absolute())                                   # a relative path would break as_uri() unresolved

    def test_a_duplicate_path_is_ingested_once(self):
        os.environ["CORTADOS_ROUTER_DB_EXTRA"] = str(dataset.ROUTER_DB) + os.pathsep + str(dataset.ROUTER_DB)
        self.assertEqual(dataset._router_dbs().count(dataset.ROUTER_DB.resolve()), 1)


class IngestionTests(unittest.TestCase):
    def test_rows_from_an_extra_db_are_read_and_source_tagged(self):
        d = Path(tempfile.mkdtemp()); db = d / "router-air.db"
        _make_db(db, turn_id="t1", request="refactor the parser")
        rows = dataset._rows_from_db(db)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["id"].startswith("rt.router-air-"))              # tagged <stem>-<pathhash>, distinct from a local rt.t1
        self.assertTrue(rows[0]["id"].endswith(".t1"))
        self.assertIn("refactor the parser", rows[0]["request"])

    def test_same_stem_dbs_on_different_paths_get_distinct_ids(self):
        # /air/router.db and /studio/router.db share a stem; a shared turn_id must still produce two distinct rows
        a = Path(tempfile.mkdtemp()) / "router.db"; b = Path(tempfile.mkdtemp()) / "router.db"
        _make_db(a, turn_id="dup", request="from air"); _make_db(b, turn_id="dup", request="from studio")
        ida = dataset._rows_from_db(a)[0]["id"]; idb = dataset._rows_from_db(b)[0]["id"]
        self.assertNotEqual(ida, idb)                                            # no silent collision
        self.assertTrue(ida.endswith(".dup") and idb.endswith(".dup"))

    def test_an_unresolvable_path_is_skipped_not_fatal(self):
        d = Path(tempfile.mkdtemp())
        loop_a, loop_b = d / "a", d / "b"
        import os as _os
        _os.symlink(loop_b, loop_a); _os.symlink(loop_a, loop_b)                 # a symlink loop: resolve() raises
        _os.environ["CORTADOS_ROUTER_DB_EXTRA"] = str(loop_a / "router.db")
        dbs = dataset._router_dbs()                                              # must not raise; the loop source is skipped
        self.assertEqual(dbs, [dataset.ROUTER_DB.resolve()])

    def test_local_db_rows_stay_unqualified(self):
        d = Path(tempfile.mkdtemp()); db = d / "router.db"
        _make_db(db, turn_id="t2", request="hi there")
        keep = dataset.ROUTER_DB
        dataset.ROUTER_DB = db                                                   # make this db the local one
        try:
            self.assertEqual(dataset._rows_from_db(db)[0]["id"], "rt.t2")        # local → unqualified, unchanged scheme
        finally:
            dataset.ROUTER_DB = keep

    def test_a_malformed_db_raises_so_main_skips_it(self):
        d = Path(tempfile.mkdtemp()); bad = d / "bad.db"; bad.write_text("not a sqlite database")
        with self.assertRaises((sqlite3.Error, OSError, ValueError)):
            dataset._rows_from_db(bad)


if __name__ == "__main__":
    unittest.main()
