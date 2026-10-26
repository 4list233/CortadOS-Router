"""Phase A: two Claude accounts route as sibling lanes (Option A). The reserve account is spared while the other has
room, so work goes to account B and account A is kept in reserve; when B is spent, A takes over. No model calls."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from cortados_router import core, execution_routing
from cortados_router.provider import routing

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


REF2 = {b: {"general": v, "writing": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 80), ("R4", 95))}
# two Claude models: a weak Sonnet-class and a strong Fable (separately metered as the "fable" pool)
MS2 = {"use_cases": {"writing": ""}, "reference_index": REF2,
       "models": [{"family": "sonnet", "ceiling": False, "proxy": False, "index": {"general": 42, "writing": 42}, "price_1m_blended_usd": 4.0,
                   "reach": [{"lane": "claude", "provider": "claude-cli", "model": "claude-sonnet-5"}]},
                  {"family": "fable", "ceiling": False, "proxy": False, "index": {"general": 81, "writing": 81}, "price_1m_blended_usd": 10.0,
                   "reach": [{"lane": "claude", "provider": "claude-cli", "model": "claude-fable-5-1"}]}]}


class ReserveNeverOverridesCapabilityTests(unittest.TestCase):
    """Codex review finding 3: with the needed model spent on B but available on reserve A, the reserve preference must
    not pick an underqualified B model. A's qualifying Fable must win."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA", "reserve": True}, {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        self._env = (os.environ.get("CORTADOS_ROUTER_TABLES"), os.environ.get("CORTADOS_ROUTING_TABLE"))
        os.environ["CORTADOS_ROUTER_TABLES"] = self.dir
        os.environ["CORTADOS_ROUTING_TABLE"] = str(ROOT / "tables/routing-table.json")
        self._keep = (core.model_scores, core.lane_health, core._table)
        core.model_scores = lambda: MS2
        core._table = None

    def tearDown(self):
        core.model_scores, core.lane_health, core._table = self._keep
        for k, v in zip(("CORTADOS_ROUTER_TABLES", "CORTADOS_ROUTING_TABLE"), self._env):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_reserve_A_fable_wins_when_B_fable_is_spent(self):
        # B's Fable pool spent, everything else healthy; a hard writing task needs the Fable-class index
        fable_spent = {"ok": True, "why": None, "scarcity": 0.0, "expiring_pct": 0, "tightest": {},
                       "pools": {"fable": {"ok": False, "scarcity": 1.0, "expiring_pct": 0, "tightest": {}}}}
        core.lane_health = lambda: {"claude": OK, "claude#b": fable_spent}
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3, work_kind="writing"))     # D≈9 → needs the Fable-class index
        self.assertEqual(r["chosen"]["model"], "claude-fable-5-1")
        self.assertEqual(r["chosen"]["lane"], "claude")                                  # reserve A, because B's Fable is spent


if __name__ == "__main__":
    unittest.main()


class SelectorPropagatesAccountTests(unittest.TestCase):
    """Codex review findings 1 & 2: a sibling-account route survives execution_routing.select's allowlist, and the
    selected account is returned so the provider can run it on that login."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA", "reserve": True}, {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        self._env = {k: os.environ.get(k) for k in ("CORTADOS_ROUTER_TABLES", "CORTADOS_ROUTING_TABLE", "CORTADOS_HOME", "CORTADOS_SCARCITY_REFRESH")}
        os.environ.update(CORTADOS_ROUTER_TABLES=self.dir, CORTADOS_ROUTING_TABLE=str(ROOT / "tables/routing-table.json"),
                          CORTADOS_HOME=self.home, CORTADOS_SCARCITY_REFRESH="off")
        self._keep = (core.model_scores, core.lane_health, core._table)
        core.model_scores = lambda: MS
        core._table = None

    def tearDown(self):
        core.model_scores, core.lane_health, core._table = self._keep
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def _select(self):
        fit = {"known": True, "assembled": True, "input_tokens": 10, "input_bytes": 40, "source": "test"}
        return execution_routing.select(core, prompt="refactor the auth module", platform="cli", face="provider",
                                        entrypoint="provider", input_fit=fit,
                                        constraints={"allowed_routes": list(routing.served_routes(core))},
                                        credential_resolver=lambda *, requested, target_model: None)

    def test_sibling_survives_and_account_is_returned(self):
        core.lane_health = lambda: {"claude": OK, "claude#b": OK, "codex": SCARCE}
        r = self._select()
        self.assertEqual(r.get("provider"), "claude-cli#b")      # B survived the allowlist intersection and was chosen
        self.assertEqual(r.get("account"), "b")                  # account propagated to the result for the backend

    def test_reserve_A_takes_over_through_the_selector_when_B_spent(self):
        core.lane_health = lambda: {"claude": OK, "claude#b": SPENT, "codex": SCARCE}
        r = self._select()
        self.assertEqual(r.get("provider"), "claude-cli")        # base = account A
        self.assertEqual(r.get("account"), "a")


class ExpandAndVisionTests(unittest.TestCase):
    """Pass-2 findings 1 & 3: static/fallback candidates are account-expanded with config_dir, and the vision filter
    keeps sibling accounts."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA", "reserve": True}, {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        self._env = {k: os.environ.get(k) for k in ("CORTADOS_ROUTER_TABLES", "CORTADOS_ROUTING_TABLE", "CORTADOS_HOME", "CORTADOS_SCARCITY_REFRESH")}
        os.environ.update(CORTADOS_ROUTER_TABLES=self.dir, CORTADOS_ROUTING_TABLE=str(ROOT / "tables/routing-table.json"),
                          CORTADOS_HOME=self.home, CORTADOS_SCARCITY_REFRESH="off")
        self._keep = (core.model_scores, core.lane_health, core._table)
        core._table = None

    def tearDown(self):
        core.model_scores, core.lane_health, core._table = self._keep
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_expand_binds_config_dir_on_static_candidates(self):
        expanded = core._expand_claude_accounts([{"lane": "claude", "provider": "claude-cli", "model": "claude-x"},
                                                 {"lane": "codex", "provider": "openai-codex", "model": "gpt-x"}])
        claude = [c for c in expanded if core._base_lane(c["lane"]) == "claude"]
        self.assertEqual({c["account"] for c in claude}, {"a", "b"})
        self.assertTrue(all(c["config_dir"].endswith((".claude-acctA", ".claude-acctB")) for c in claude))
        self.assertEqual([c for c in expanded if c["lane"] == "codex"][0].get("account"), None)   # non-claude untouched

    def test_vision_keeps_a_sibling_account(self):
        core.model_scores = lambda: MS2
        core.lane_health = lambda: {"claude": SPENT, "claude#b": OK}         # base account A spent, sibling B available
        fit = {"known": True, "assembled": True, "input_tokens": 10, "input_bytes": 40, "source": "test"}
        r = execution_routing.select(core, prompt="describe this image", platform="cli", face="provider",
                                     entrypoint="provider", input_fit=fit,
                                     constraints={"allowed_routes": list(routing.served_routes(core)),
                                                  "required_capabilities": ["vision"]},
                                     credential_resolver=lambda *, requested, target_model: None)
        self.assertNotIn("error", r)                                         # not a no-route
        self.assertEqual(core._base_provider(r["provider"]), "claude-cli")   # a Claude account served it
        self.assertEqual(r["account"], "b")                                  # the sibling, since A is spent


class BrokenConfigAndStaticReserveTests(unittest.TestCase):
    """Pass-3 findings 2 & 3: a broken accounts.json refuses Claude (never the default login), and the static/fallback
    selection path honours the reserve policy."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self._env = {k: os.environ.get(k) for k in ("CORTADOS_ROUTER_TABLES", "CORTADOS_ROUTING_TABLE", "CORTADOS_HOME", "CORTADOS_SCARCITY_REFRESH")}
        os.environ.update(CORTADOS_ROUTER_TABLES=self.dir, CORTADOS_ROUTING_TABLE=str(ROOT / "tables/routing-table.json"),
                          CORTADOS_HOME=self.home, CORTADOS_SCARCITY_REFRESH="off")
        self._keep = (core.model_scores, core.lane_health, core._table)
        core._table = None

    def tearDown(self):
        core.model_scores, core.lane_health, core._table = self._keep
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_absent_config_is_default_login(self):
        self.assertEqual(core._vendor_accounts("claude", "claude", "claude-cli")[0]["account"], None)

    def test_broken_config_raises(self):
        (Path(self.dir) / "accounts.json").write_text("{ not valid json")
        with self.assertRaises(core.AccountsConfigError):
            core._vendor_accounts("claude", "claude", "claude-cli")

    def test_expand_drops_claude_on_broken_config(self):
        (Path(self.dir) / "accounts.json").write_text("{ not valid json")
        out = core._expand_claude_accounts([{"lane": "claude", "provider": "claude-cli", "model": "claude-x"},
                                            {"lane": "codex", "provider": "openai-codex", "model": "gpt-x"}])
        self.assertEqual([c["lane"] for c in out], ["codex"])           # Claude refused, not run on the default login

    def test_static_path_honours_reserve(self):
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA", "reserve": True}, {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        core.model_scores = lambda: None                                 # force the static band-list path
        core.lane_health = lambda: {"claude": OK, "claude#b": OK, "codex": OK, "cursor": OK}
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="writing"))   # R2 static, both accounts healthy
        self.assertNotEqual(r["chosen"]["lane"], "claude")               # reserve A not consumed
        self.assertTrue(any(s["lane"] == "claude" and "reserve" in (s.get("why") or "") for s in r["skipped"]))
