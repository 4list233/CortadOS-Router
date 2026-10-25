"""Phase A: two Claude accounts route as sibling lanes (Option A). The reserve account is spared while the other has
room, so work goes to account B and account A is kept in reserve; when B is spent, A takes over. No model calls."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from cortados_router import core

ROOT = Path(__file__).resolve().parents[1]


def dec(steps=0, ambiguity=0, blast=0, ctx=0, novelty=0, par=0, **kw):
    a = {"steps": steps, "ambiguity": ambiguity, "blast_radius": blast, "context_size": ctx, "novelty": novelty,
         "parallelizability": par, "outward": False, "work_kind": "conversation", "urgency": "now",
         "needs_clarification": False, "parts": 0, "diversity": False, **kw}
    return {"answers": a, "degraded": False, "engine": "test", "engine_ms": 1}


REF = {b: {"general": v, "code": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 80), ("R4", 95))}
MS = {"use_cases": {"code": ""}, "reference_index": REF,
      "models": [{"family": "mid", "ceiling": False, "proxy": False, "index": {"general": 55, "code": 52}, "price_1m_blended_usd": 4.0,
                  "reach": [{"lane": "claude", "provider": "claude-cli", "model": "mid"}]},
                 {"family": "mid2", "ceiling": False, "proxy": False, "index": {"general": 60, "code": 58}, "price_1m_blended_usd": 4.5,
                  "reach": [{"lane": "codex", "provider": "openai-codex", "model": "mid2"}]}]}

OK = {"ok": True, "why": None, "scarcity": 0.0, "expiring_pct": 0, "tightest": {}, "pools": {}}
SPENT = {"ok": False, "why": "week 100% used", "scarcity": 1.0, "expiring_pct": 0, "tightest": {}, "pools": {}}
SCARCE = {"ok": True, "why": None, "scarcity": 0.9, "expiring_pct": 0, "tightest": {}, "pools": {}}


class ReserveRoutingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA", "reserve": True},
            {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        self._env = (os.environ.get("CORTADOS_ROUTER_TABLES"), os.environ.get("CORTADOS_ROUTING_TABLE"))
        os.environ["CORTADOS_ROUTER_TABLES"] = self.dir                              # accounts.json here
        os.environ["CORTADOS_ROUTING_TABLE"] = str(ROOT / "tables/routing-table.json")  # real band table
        self._keep = (core.model_scores, core.lane_health, core._table, core._table_mtime)
        core.model_scores = lambda: MS
        core._table = None                                                           # force a reload against the overridden table path

    def tearDown(self):
        core.model_scores, core.lane_health, core._table, core._table_mtime = self._keep
        for k, v in zip(("CORTADOS_ROUTER_TABLES", "CORTADOS_ROUTING_TABLE"), self._env):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def _resolve(self):
        return core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))   # D=6 → needs 50; mid(52) and mid2(58) qualify

    def test_both_accounts_are_candidates(self):
        core.lane_health = lambda: {"claude": OK, "claude#b": OK, "codex": SCARCE}
        lanes = {c["lane"] for c in core.scored_candidates(core.table(), "R2", "code")}
        self.assertIn("claude", lanes)
        self.assertIn("claude#b", lanes)

    def test_reserve_account_spared_work_goes_to_B(self):
        core.lane_health = lambda: {"claude": OK, "claude#b": OK, "codex": SCARCE}
        r = self._resolve()
        self.assertEqual(r["chosen"]["lane"], "claude#b")                             # B used, A reserved
        self.assertTrue(any(s["lane"] == "claude" and "reserve" in (s.get("why") or "") for s in r["skipped"]))

    def test_reserve_account_takes_over_when_B_spent(self):
        core.lane_health = lambda: {"claude": OK, "claude#b": SPENT, "codex": SCARCE}
        r = self._resolve()
        self.assertEqual(r["chosen"]["lane"], "claude")                              # B spent → A (reserve) now serves

    def test_falls_off_claude_when_both_spent(self):
        core.lane_health = lambda: {"claude": SPENT, "claude#b": SPENT, "codex": OK}
        r = self._resolve()
        self.assertEqual(r["chosen"]["lane"], "codex")


if __name__ == "__main__":
    unittest.main()
