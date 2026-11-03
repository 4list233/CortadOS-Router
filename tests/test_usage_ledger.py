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

    def test_failure_does_not_raise_and_logs_type(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "dir-not-db"
            bad.mkdir()
            usage_ledger._warned.clear()
            with self.assertLogs("cortados.usage_ledger", "WARNING") as cm:
                self.assertFalse(usage_ledger.record("claude", "m", {"input_tokens": 1}, path=bad))
            self.assertIn("OperationalError", cm.output[0])

    def test_server_hook_skips_unrouted_and_writes_routed(self):
        from cortados_router.provider import server
        with mock.patch.object(usage_ledger, "record_async") as rec:
            server._ledger_turn(mock.Mock(route=None), {"input_tokens": 1})
            rec.assert_not_called()
            server._ledger_turn(mock.Mock(route={"provider": "claude-cli#b", "model": "claude-sonnet-5"}), {"input_tokens": 3})
            rec.assert_called_once_with("claude#b", "claude-sonnet-5", {"input_tokens": 3})


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


if __name__ == "__main__":
    unittest.main()
