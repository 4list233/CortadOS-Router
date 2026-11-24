"""#143 routing changes (owner decisions 2026-11-23): the API credit lane as a scored candidate, the frontier made
reachable for hard work (knots, ceiling_underuse from D 8, ceiling_work requiring a frontier model, Fable's pool size,
Sol 6.1 as an R3 reference), and the split of work across underused pools. Fakes only: a fake scarcity module, fake
model scores, throwaway homes; no vendor CLI, no network, no live state."""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HERMES_HOME", tempfile.mkdtemp(prefix="cortados-router-test-"))
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("core_u143", str(ROOT / "cortados_router/core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
_real_sc = core._scarcity_module()

ACCOUNTS = [{"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": False, "config_dir": "/x/a"},
            {"lane": "claude#b", "provider": "claude-cli#b", "account": "b", "reserve": False, "config_dir": "/x/b"}]


def week(used, h_left, now, window="week (all models)", pool="all"):
    return {"window": window, "pool": pool, "used_pct": used, "headroom_pct": 100 - used,
            "elapsed_pct": round(100 * (1 - h_left / 168), 1), "reset_epoch": now + h_left * 3600}


def grant(used, h_left, now, life_h=30 * 24):
    """The API credit lane's one window, as scarcity._anthropic_api reports it (no `pool`: the general window)."""
    return {"window": "grant", "used_pct": used, "headroom_pct": 100 - used,
            "elapsed_pct": round(100 * (1 - h_left / life_h), 1), "reset_epoch": now + h_left * 3600}


class FakeSc:
    def __init__(self, lanes, ucs, burns=None):
        self.lanes, self.ucs, self.burns = lanes, ucs, burns or {}

    def current(self, *a, **k):
        return {"lanes": self.lanes}

    def unit_costs(self):
        return self.ucs

    def pool_for(self, lane, model, pools):
        return _real_sc.pool_for(lane, model, pools)

    def recent_burn_rates(self, *a, **k):
        return dict(self.burns)


def health(**lanes):
    return {l: {"ok": True, "why": None, "scarcity": s, "expiring_pct": 0, "tightest": {}, "pools": {}}
            for l, s in lanes.items()}


def model(family, mid, g, idx, lane, prov, ceil, price):
    return {"family": family, "ceiling": ceil, "proxy": False, "index": {"general": g, **idx},
            "price_1m_blended_usd": price, "reach": [{"lane": lane, "provider": prov, "model": mid}]}


# The audit week's catalogue, trimmed: R3 code reference 66.2 (Sol 6.1 is an R3 reference since #143).
MS = {"use_cases": {"code": "", "planning": ""},
      "reference_index": {"R1": {"general": 13.3, "code": 14.2, "planning": 6.1},
                          "R2": {"general": 52.8, "code": 47.9, "planning": 50.8},
                          "R3": {"general": 79.6, "code": 66.2, "planning": 76.1},
                          "R4": {"general": 91.2, "code": 78.5, "planning": 87.4}},
      "models": [model("claude-opus-5-5", "claude-opus-5-5", 99.8, {"code": 100.0, "planning": 97.7}, "claude", "claude-cli", True, 8.0),
                 model("claude-fable-5-1", "claude-fable-5-1", 91.2, {"code": 84.0, "planning": 87.4}, "claude", "claude-cli", True, 20.0),
                 model("claude-sonnet-5", "claude-sonnet-5", 52.8, {"code": 47.9, "planning": 50.8}, "claude", "claude-cli", False, 4.0),
                 model("gpt-6-1-sol", "gpt-6.1-sol", 90.1, {"code": 66.2, "planning": 88.8}, "codex", "openai-codex", False, 4.0),
                 model("gpt-6-astra", "gpt-6-astra", 100.0, {"code": 78.5, "planning": 100.0}, "codex", "openai-codex", True, 20.0),
                 model("gpt-5-6-luna", "gpt-5.6-luna", 50.2, {"code": 49.9, "planning": 44.8}, "codex", "openai-codex", False, 0.45)]}

# unit-cost.json of 2026-11-23 (the audit's copy): every learned size a lower bound, no Fable record.
UCS = {"claude#b:all": {"tokens_per_pct": 5_610_803, "remaining_tokens": 364_702_169, "lower_bound": True},
       "claude:all": {"tokens_per_pct": 7_377_367, "remaining_tokens": 575_434_588, "lower_bound": True},
       "codex:all": {"tokens_per_pct": 3_882_299, "remaining_tokens": 322_230_781, "lower_bound": True}}

CLAUDE_MODELS = ("claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5")
CODEX_MODELS = ("gpt-6.1-sol", "gpt-6-astra", "gpt-5.6-luna")
SERVED = tuple(sorted({(p, m) for p in ("claude-cli", "claude-cli#b") for m in CLAUDE_MODELS}
                      | {("codex-cli", m) for m in CODEX_MODELS}))
WITH_API = tuple(sorted(set(SERVED) | {(core.API_LANE, m) for m in CLAUDE_MODELS}))


def dec(steps=3, ambiguity=1, novelty=1, blast=1, outward=False, work_kind="code", degraded=False, **kw):
    a = {"steps": steps, "ambiguity": ambiguity, "blast_radius": blast, "context_size": 0, "novelty": novelty,
         "parallelizability": 0, "outward": outward, "work_kind": work_kind, "urgency": "now",
         "needs_clarification": False, "parts": 0, "diversity": False, **kw}
    return {"answers": a, "degraded": degraded, "engine": "test", "engine_ms": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self._keep = (core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts)
        core.model_scores = lambda: MS
        core._vendor_accounts = lambda *a: [dict(x) for x in ACCOUNTS]
        self.now = time.time()

    def tearDown(self):
        core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts = self._keep

    def audit_state(self, api_used=0.0, api_h_left=211.0, ucs=UCS, burns=None, **override):
        """2026-11-23 23:37: A scarce (0.9, 25 % used of a fresh week), B 36 % used resetting in 31.7 h, Codex 18 % used
        resetting in 36.4 h, the API grant 0 % used with 211 h to its stop-counting point."""
        n = self.now
        lanes = {"claude": {"fetched_at": n, "windows": [week(25, 158.7, n), week(6, 158.7, n, "week (Fable)", "fable")]},
                 "claude#b": {"fetched_at": n, "windows": [week(36, 31.7, n), week(4, 31.7, n, "week (Fable)", "fable")]},
                 "codex": {"fetched_at": n, "windows": [week(18, 36.4, n, "week")]},
                 core.API_LANE: {"fetched_at": n, "budget_usd": 200.0, "stop_line_usd": 190.0,
                                 "windows": [grant(api_used, api_h_left, n)]}}
        sc = {"claude": 0.9, "claude#b": -0.84, "codex": -1.0, core.API_LANE: -1.0, **override}
        core._sc_mod = FakeSc(lanes, ucs, burns)
        core.lane_health = lambda: health(**sc)

    def resolve(self, d, allowed=WITH_API, **kw):
        return core.resolve(d, "interactive", input_fit={"input_tokens": 40000}, allowed_routes=allowed,
                            execution=True, accounts_aware=True, **kw)

    @staticmethod
    def every(r):
        return {(c["lane"], c["model"]): c for c in [r["chosen"]] + (r["alternatives"] or [])}


# ---------------------------------------------------------------------------------------------------- 1. API lane
class ApiLaneCandidate(Base):
    def test_offered_only_when_the_host_lists_its_pairs(self):
        self.audit_state()
        r = self.resolve(dec(), allowed=SERVED)
        self.assertFalse(any(c["lane"] == core.API_LANE for c in [r["chosen"]] + r["alternatives"]))
        r = self.resolve(dec())
        lanes = {c["lane"] for c in [r["chosen"]] + r["alternatives"]}
        self.assertIn(core.API_LANE, lanes)
        # never offered by a host that names no pairs at all (core.decide, Hermes)
        r = core.resolve(dec(), "interactive", input_fit={"input_tokens": 40000}, accounts_aware=True)
        self.assertNotIn(core.API_LANE, json.dumps(r["chosen"]) + json.dumps(r["alternatives"]))

    def test_pool_size_is_the_stop_lines_dollars_at_the_models_price(self):
        self.audit_state()
        opus = {"lane": core.API_LANE, "model": "claude-opus-5-5", "price": 8.0}
        # $190 / 100 = $1.90 per %, at $8 per 1M → 237,500 tokens per %
        self.assertAlmostEqual(core._pool_tokens_per_pct(opus), 237_500, delta=1)
        self.assertAlmostEqual(core._tokens_left_for(opus), 237_500 * 100, delta=100)   # 100 % headroom
        # a reading written before stop_line_usd: the grant total at the default 95 % stop
        del core._sc_mod.lanes[core.API_LANE]["stop_line_usd"]
        self.assertAlmostEqual(core._pool_tokens_per_pct(opus), 237_500, delta=1)

    API_AND_SOL = tuple(sorted({(core.API_LANE, m) for m in CLAUDE_MODELS} | {("codex-cli", "gpt-6.1-sol"),
                                                                             ("codex-cli", "gpt-6-astra")}))

    def test_free_until_the_grant_lapses(self):
        self.audit_state()                                  # nothing spent, 211 h left: the whole grant would lapse
        r = self.resolve(dec(), allowed=self.API_AND_SOL)
        api = self.every(r)[(core.API_LANE, "claude-opus-5-5")]
        self.assertGreater(api["expected_loss_pct"], 99)
        self.assertLessEqual(api["weight"], 1.0)            # its dollars cost (almost) nothing
        # spent at a pace that uses most of it before expiry: the discount shrinks, the weight rises (capped at 100)
        self.audit_state(api_used=60.0, api_h_left=48.0, burns={(core.API_LANE, "grant"): 0.8})
        r2 = self.resolve(dec(), allowed=self.API_AND_SOL)
        api2 = self.every(r2)[(core.API_LANE, "claude-opus-5-5")]
        self.assertLess(api2["expected_loss_pct"], 5)
        self.assertGreater(api2["weight"], 50)
        self.assertLessEqual(api2["weight"], 100)
        # the cost falls again as the expiry nears with credit left (same spend, same burn, fewer hours)
        self.audit_state(api_used=60.0, api_h_left=10.0, burns={(core.API_LANE, "grant"): 0.8})
        api3 = self.every(self.resolve(dec(), allowed=self.API_AND_SOL))[(core.API_LANE, "claude-opus-5-5")]
        self.assertLess(api3["cost"], api2["cost"])

    def test_never_the_weights_normalising_reference(self):
        self.audit_state()
        without = self.every(self.resolve(dec(), allowed=SERVED))
        with_api = self.every(self.resolve(dec()))
        for key in [("codex", "gpt-6.1-sol"), ("claude#b", "claude-opus-5-5")]:
            if key in without and key in with_api:
                self.assertEqual(without[key]["weight"], with_api[key]["weight"])

    def test_never_outward_wide_blast_or_degraded(self):
        self.audit_state()
        for d in (dec(outward=True), dec(blast=2), dec(degraded=True)):
            r = self.resolve(d)
            self.assertNotEqual((r["chosen"] or {}).get("lane"), core.API_LANE)
            self.assertFalse(any(c["lane"] == core.API_LANE for c in (r["alternatives"] or [])))
        self.assertIn("outward", core.api_lane_refusal(dec(outward=True), {}))
        self.assertIn("blast radius", core.api_lane_refusal(dec(blast=2), {"max_blast_radius": 1}))
        self.assertIn("degraded", core.api_lane_refusal({"answers": None, "degraded": True}, {}))
        self.assertIn("blast radius", core.api_lane_refusal(dec(blast=True), {}))       # malformed: refused
        self.assertIsNone(core.api_lane_refusal(dec(blast=0), {}))

    def test_anthropics_own_api_is_never_a_secondary_source(self):
        self.audit_state(**{"claude#b": -0.84})
        r = self.resolve(dec())
        api = [c for c in [r["chosen"]] + r["alternatives"] if c["lane"] == core.API_LANE]
        self.assertTrue(api)
        self.assertFalse(any(c.get("secondary") for c in api))


# --------------------------------------------------------------------------------------- 2. the frontier for hard work
class FrontierReachable(Base):
    def test_table_knots_and_thresholds(self):
        sel = core.table()["selection"]
        self.assertEqual([(k["D"], k["ref"]) for k in sel["knots"]][-3:], [(8, "R3"), (9, "R4"), (10, "best")])
        self.assertEqual(sel["ceiling_underuse"]["min_D"], 8)
        cw = sel["ceiling_work"]
        self.assertTrue(cw["require_frontier"])
        self.assertTrue(core.ceiling_work_applies(cw, "planning", 7))
        self.assertFalse(core.ceiling_work_applies(cw, "code", 8))                     # D 8 code: routine outward edits
        self.assertTrue(core.ceiling_work_applies(cw, "code", 9))
        self.assertFalse(core.ceiling_work_applies(cw, "writing", 10))

    def test_d9_requires_the_r4_reference_and_d10_the_best(self):
        t = core.table()
        self.assertEqual(core.required_index(t, MS, "code", 9, "R3"), 78.5)
        self.assertEqual(core.required_index(t, MS, "code", 10, "R3"), 100.0)
        self.assertEqual(core.required_index(t, MS, "code", 8, "R2"), 66.2)            # the R3 reference: Sol 6.1 clears it

    def test_hard_planning_needs_a_frontier_model_while_one_is_available(self):
        self.audit_state()
        d = dec(steps=3, ambiguity=2, novelty=2, blast=0, work_kind="planning")       # D=7 planning
        r = self.resolve(d, allowed=SERVED)
        self.assertTrue(r["chosen"]["ceiling"], r["chosen"])
        self.assertTrue(any("needs a frontier (ceiling) model" in x for x in r["hard_rules"]))
        # Sol clears the R4 planning reference (88.8 >= 87.4) but is held off; with every frontier route refused by
        # the host (its quota floor), the index rule alone applies again and Sol may take it
        r = self.resolve(d, allowed=SERVED, hold_ok=lambda c: not c.get("ceiling"))
        self.assertFalse(r["chosen"]["ceiling"])
        self.assertTrue(any("no frontier model available" in x for x in r["hard_rules"]))

    def test_routine_d8_code_is_not_forced_onto_the_frontier(self):
        # steps 3, ambiguity 1, novelty 1, blast 3 (outward floor) = D 8: no frontier requirement; Sol 6.1 clears R3
        self.audit_state(**{"codex": 0.1, "claude#b": 0.1})                            # no spare frontier pool
        r = self.resolve(dec(outward=True), allowed=SERVED)
        self.assertEqual((r["band"], r["D"], r["required_index"]), ("R3", 8, 66.2))
        self.assertEqual(r["chosen"]["model"], "gpt-6.1-sol")
        self.assertFalse(any("ceiling_work" in x for x in r["hard_rules"]))

    def test_ceiling_underuse_from_d8_and_never_a_route_the_host_refuses(self):
        self.audit_state()
        r = self.resolve(dec(outward=True), allowed=SERVED)                             # D=8, spare frontier pools
        self.assertTrue(r["chosen"]["ceiling"])
        self.assertTrue(any("paid-for frontier capacity" in x for x in r["hard_rules"]))
        r = self.resolve(dec(steps=3, ambiguity=1, novelty=1, blast=0), allowed=SERVED)   # D=5: not
        self.assertFalse(any("paid-for frontier capacity" in x for x in r["hard_rules"]))
        r = self.resolve(dec(outward=True), allowed=SERVED, hold_ok=lambda c: c["lane"] != "codex")
        self.assertNotEqual(r["chosen"]["lane"], "codex")

    def test_r3_code_reference_is_sol_61_in_the_generated_scores(self):
        t = core.table()
        self.assertIn("gpt-6.1-sol", t["tiering"]["band_references"]["R3"])
        ms = json.loads((ROOT / "tables/model-scores.generated.json").read_text())
        sol = next(m for m in ms["models"] if m["family"] == "gpt-6-1-sol")
        self.assertEqual(ms["reference_index"]["R3"]["code"], sol["index"]["code"])
        self.assertLessEqual(ms["reference_index"]["R3"]["code"], 66.2)


class FablePoolSize(Base):
    def fable(self, lane="claude#b"):
        return {"lane": lane, "model": "claude-fable-5-1", "family": "claude-fable-5-1", "price": 20.0,
                "config_dir": "/x/b" if lane == "claude#b" else "/x/a"}

    def test_fable_is_sized_by_the_window_that_binds_it(self):
        self.audit_state()                                   # B: all-models 64 % left, Fable 96 % left → all binds
        ps = core._pool_size(self.fable())
        self.assertEqual(ps["bound_by"], "all")
        self.assertEqual(ps["tpp"], UCS["claude#b:all"]["tokens_per_pct"])
        self.assertEqual(core._tokens_left_for(self.fable()), UCS["claude#b:all"]["remaining_tokens"])
        self.assertEqual(core._quota_group(self.fable()), core._quota_group(
            {**self.fable(), "model": "claude-opus-5-5", "family": "claude-opus-5-5", "price": 8.0}))

    def test_its_own_week_binding_uses_the_estimate_or_the_learned_size(self):
        n = self.now
        self.audit_state()
        core._sc_mod.lanes["claude#b"]["windows"] = [week(10, 31.7, n), week(80, 31.7, n, "week (Fable)", "fable")]
        ps = core._pool_size(self.fable())
        self.assertNotIn("bound_by", ps)
        ratio = core.table()["selection"]["pool_size_from_pool"]["claude:fable"]["ratio"]
        self.assertAlmostEqual(ps["tpp"], ratio * UCS["claude#b:all"]["tokens_per_pct"], delta=1)
        self.assertAlmostEqual(core._tokens_left_for(self.fable()), ps["tpp"] * 20, delta=1)   # 20 % left in its week
        # a learned size that is not a lower bound stands; a lower-bound one below the estimate is raised
        core._sc_mod.ucs = {**UCS, "claude#b:fable": {"tokens_per_pct": 1_000_000}}
        self.assertEqual(core._pool_tokens_per_pct(self.fable()), 1_000_000)
        core._sc_mod.ucs = {**UCS, "claude#b:fable": {"tokens_per_pct": 1_000_000, "lower_bound": True}}
        self.assertAlmostEqual(core._pool_tokens_per_pct(self.fable()), ratio * UCS["claude#b:all"]["tokens_per_pct"], delta=1)

    def test_fable_is_no_longer_priced_out(self):
        # The audit: no Fable size → price-only weight 177.8 and no capacity credit. Sized by B's all-models week it
        # weighs what Astra weighs ($20 on a ~5.6 M tokens/% pool), and earns the capacity credit.
        self.audit_state()
        allowed = (("claude-cli#b", "claude-fable-5-1"), ("codex-cli", "gpt-6-astra"), ("codex-cli", "gpt-6.1-sol"))
        r = self.resolve(dec(steps=3, ambiguity=2, novelty=2, blast=0, work_kind="planning"), allowed=allowed)
        every = self.every(r)
        fable_b, astra = every[("claude#b", "claude-fable-5-1")], every[("codex", "gpt-6-astra")]
        self.assertLessEqual(fable_b["weight"], 100.0)
        self.assertAlmostEqual(fable_b["weight"], astra["weight"], delta=2.0)
        self.assertIsNotNone(core._tokens_left_for(self.fable()))

    def test_unitcost_learns_the_fable_week(self):
        from cortados_router import unitcost
        self.assertEqual(unitcost.window_pool("claude#b", "week (Fable)"), "claude#b:fable")
        self.assertEqual(unitcost.window_pool("claude", "week (Fable)"), "claude:fable")
        self.assertEqual(unitcost.window_pool("claude", "week (all models)"), "claude:all")
        self.assertIsNone(unitcost.window_pool("claude", "5-hour session"))


# ------------------------------------------------------------------------------------------------ 3. underuse split
class SplitPick(unittest.TestCase):
    def test_deterministic_and_proportional(self):
        w = [("codex:all", 60.0), ("claude#b:all", 40.0)]
        self.assertEqual(core.split_pick("conv-1", w), core.split_pick("conv-1", list(reversed(w))))
        picks = collections.Counter(core.split_pick(f"conv-{i}", w) for i in range(20000))
        self.assertAlmostEqual(picks["codex:all"] / 20000, 0.6, delta=0.02)
        self.assertIsNone(core.split_pick("x", [("a", 0.0)]))
        self.assertEqual(core.split_pick("x", [("a", 0.0), ("b", 1.0)]), "b")

    def test_a_small_weight_change_moves_few_keys(self):
        a = [core.split_pick(f"k{i}", [("x", 60.0), ("y", 40.0)]) for i in range(5000)]
        b = [core.split_pick(f"k{i}", [("x", 58.0), ("y", 42.0)]) for i in range(5000)]
        self.assertLess(sum(p != q for p, q in zip(a, b)) / 5000, 0.03)


class UnderuseSplit(Base):
    def test_underused_pools_share_r2_by_the_quota_each_would_lose(self):
        self.audit_state()
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)                               # D=5 code, R1
        lanes = collections.Counter(self.resolve(d, allowed=SERVED, split_key=f"c{i}")["chosen"]["lane"]
                                    for i in range(400))
        self.assertGreater(lanes["codex"], 0)
        self.assertGreater(lanes["claude#b"], 0)
        self.assertEqual(lanes["claude"], 0)                                            # A is scarce: never in the split
        r = self.resolve(d, allowed=SERVED, split_key="c1")
        rule = next(x for x in r["hard_rules"] if x.startswith("underuse split"))
        self.assertIn("codex:all", rule); self.assertIn("claude#b:all", rule)
        # the conversation key decides, and decides the same way every time
        self.assertEqual(r["chosen"]["lane"], self.resolve(d, allowed=SERVED, split_key="c1")["chosen"]["lane"])
        # the shares follow expected lost tokens: Codex ≈ 386 M (74 % × 5.63 M prior), B ≈ 290 M
        share = lanes["codex"] / 400
        self.assertTrue(0.45 < share < 0.75, share)

    def test_no_key_keeps_the_cheapest(self):
        self.audit_state()
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)
        r = self.resolve(d, allowed=SERVED)
        self.assertFalse(any(x.startswith("underuse split") for x in r["hard_rules"]))
        self.assertEqual(r["chosen"]["lane"], "codex")

    def test_no_split_when_the_cheapest_pool_loses_nothing(self):
        self.audit_state(burns={("codex", "week"): 5.0})                                # Codex will use all of its week
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)
        rs = [self.resolve(d, allowed=SERVED, split_key=f"c{i}") for i in range(30)]
        firsts = {r["chosen"]["lane"] for r in rs}
        self.assertEqual(len(firsts), 1)                                                 # one route, whatever the key

    def test_a_pool_the_host_refuses_takes_no_part(self):
        self.audit_state()
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)
        lanes = {self.resolve(d, allowed=SERVED, split_key=f"c{i}", hold_ok=lambda c: c["lane"] != "claude#b")
                 ["chosen"]["lane"] for i in range(60)}
        self.assertNotIn("claude#b", lanes)

    def test_the_switching_margin_does_not_undo_a_split(self):
        self.audit_state()
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)
        key = next(f"c{i}" for i in range(200)
                   if self.resolve(d, allowed=SERVED, split_key=f"c{i}")["chosen"]["lane"] == "claude#b")
        r = self.resolve(d, allowed=SERVED, split_key=key, previous={"R1": {"lane": "codex", "model": "gpt-6.1-sol"}})
        self.assertEqual(r["chosen"]["lane"], "claude#b")
        self.assertFalse(any(x.startswith("switching margin: stays") for x in r["hard_rules"]))

    def test_spare_frontier_pools_share_hard_work(self):
        self.audit_state()
        lanes = collections.Counter(self.resolve(dec(outward=True), allowed=SERVED, split_key=f"h{i}")["chosen"]["lane"]
                                    for i in range(300))
        self.assertGreater(lanes["codex"], 0); self.assertGreater(lanes["claude#b"], 0)
        r = self.resolve(dec(outward=True), allowed=SERVED, split_key="h1")
        self.assertTrue(r["chosen"]["ceiling"])
        self.assertTrue(any("spare frontier pools share it" in x for x in r["hard_rules"]))

    def test_the_api_lane_takes_its_share(self):
        self.audit_state()
        d = dec(steps=2, ambiguity=1, novelty=1, blast=1)
        lanes = collections.Counter(self.resolve(d, split_key=f"a{i}")["chosen"]["lane"] for i in range(1000))
        self.assertGreater(lanes[core.API_LANE], 0)
        self.assertLess(lanes[core.API_LANE], 150)                                      # a $190 grant is a small pool


# ---------------------------------------------------------------------------------- provider: the lane's admission
class ProviderApiLane(unittest.TestCase):
    ON = {"on": True, "expiring_soon": False, "headroom_usd": 150.0}
    BODY = {"model": "cortados", "max_tokens": 1000, "messages": [{"role": "user", "content": "rename a variable"}]}
    SERVED = (("claude-cli", "claude-opus-5-5"), ("claude-cli#b", "claude-opus-5-5"), ("claude-cli", "claude-sonnet-5"),
              ("codex-cli", "gpt-6.1-sol"))

    def pairs(self, body=None, state=None, local=None, sensitive=None):
        from cortados_router.provider import routing
        with mock.patch("cortados_router.api_credits.state", return_value=state or self.ON), \
                mock.patch.object(routing, "request_sensitivity", return_value=sensitive):
            return routing.api_lane_pairs(None, body or self.BODY, self.SERVED, local=local)

    def test_offered_for_a_cleared_request_while_the_lane_is_on(self):
        from cortados_router.provider import routing
        self.assertEqual(self.pairs(), [(routing.API_PROVIDER, "claude-opus-5-5"), (routing.API_PROVIDER, "claude-sonnet-5")])

    def test_never_for_sensitive_local_only_or_unbounded_work(self):
        from cortados_router.provider import routing
        self.assertEqual(self.pairs(local="sensitive"), [])
        self.assertEqual(self.pairs(local="prefer"), [])
        self.assertEqual(self.pairs(sensitive="keyword"), [])
        self.assertEqual(self.pairs(sensitive="unknown"), [])                          # doubt counts
        self.assertEqual(self.pairs(body={**self.BODY, routing.NO_LOCAL_KEY: True}), [])
        self.assertEqual(self.pairs(body={**self.BODY, "model": "sub/claude-opus-5-5"}), [])
        self.assertEqual(self.pairs(body={**self.BODY, "fallbacks": [{"model": "claude-opus-5"}]}), [])
        self.assertEqual(self.pairs(state={**self.ON, "on": False}), [])

    def test_a_model_whose_upper_bound_cost_does_not_fit_is_left_out(self):
        from cortados_router import api_credits
        from cortados_router.provider import routing
        sonnet = api_credits.estimate_max_cost(self.BODY, "claude-sonnet-5")
        opus = api_credits.estimate_max_cost(self.BODY, "claude-opus-5-5")
        self.assertLess(sonnet, opus)
        got = self.pairs(state={**self.ON, "headroom_usd": (sonnet + opus) / 2})
        self.assertEqual(got, [(routing.API_PROVIDER, "claude-sonnet-5")])

    def test_a_degraded_scorer_never_sees_the_lane(self):
        from cortados_router.provider import routing
        seen = {}

        class C:
            def resolve(self, decision, *a, allowed_routes=None, **kw):
                seen["allowed"] = allowed_routes
                return {}
        routing._NoClaudeWhenDegraded(C()).resolve({"degraded": True, "answers": None},
                                                   allowed_routes=(("anthropic-api", "claude-opus-5-5"),
                                                                   ("codex-cli", "gpt-6.1-sol")))
        self.assertEqual(seen["allowed"], (("codex-cli", "gpt-6.1-sol"),))

    def test_the_lane_needs_its_key(self):
        from cortados_router import execution_routing
        from cortados_router.provider import routing
        check = routing._credentials("claude", "codex")
        with mock.patch("cortados_router.api_credits.key_problem", return_value="no key"):
            with self.assertRaises(execution_routing.CredentialsUnavailable):
                check(requested=routing.API_PROVIDER, target_model="claude-opus-5-5")
        with mock.patch("cortados_router.api_credits.key_problem", return_value=None):
            check(requested=routing.API_PROVIDER, target_model="claude-opus-5-5")

    def test_the_selector_offers_the_lane_only_to_a_host_listing_it(self):
        from cortados_router import execution_routing
        seen = []

        class C:
            API_LANE = core.API_LANE
            S1_CURRENT = Path(tempfile.mkdtemp()) / "none"

            def table(self):
                return {"candidates": {}, "workloads": {}}

            def model_scores(self):
                return {"models": [{"reach": [{"provider": "claude-cli", "model": "claude-opus-5-5"},
                                              {"provider": "openai-codex", "model": "gpt-6.1-sol"}]}]}

            def decide(self, *a, **k):
                return dict(dec())

            def resolve(self, decision, unit, *, allowed_routes, **kw):
                seen.append(allowed_routes)
                return {"chosen": None}
        execution_routing.select(C(), prompt="x", session_id="s1", credential_resolver=lambda **k: None)
        self.assertFalse(any(p[0] == core.API_LANE for p in seen[-1]))
        execution_routing.select(C(), prompt="x", session_id="s2", credential_resolver=lambda **k: None,
                                 constraints={"allowed_routes": [["claude-cli", "claude-opus-5-5"],
                                                                 [core.API_LANE, "claude-opus-5-5"]]})
        self.assertIn((core.API_LANE, "claude-opus-5-5"), seen[-1])

    def test_select_passes_the_conversation_as_the_split_key_and_the_floor_check(self):
        from cortados_router import execution_routing
        got = {}

        class C:
            S1_CURRENT = Path(tempfile.mkdtemp()) / "none"

            def table(self):
                return {"candidates": {}, "workloads": {}}

            def model_scores(self):
                return {"models": [{"reach": [{"provider": "openai-codex", "model": "gpt-6.1-sol"}]}]}

            def decide(self, *a, **k):
                return dict(dec())

            def resolve(self, decision, unit, **kw):
                got.update(kw)
                return {"chosen": None}
        ok = lambda c: True
        execution_routing.select(C(), prompt="x", session_id="conv-9", credential_resolver=lambda **k: None, hold_ok=ok)
        self.assertEqual(got.get("split_key"), "conv-9")
        self.assertIs(got.get("hold_ok"), ok)                                            # even without a previous route


class ProviderChooseApi(unittest.TestCase):
    BODY = {"model": "cortados", "max_tokens": 1000, "messages": [{"role": "user", "content": "rename a variable"}]}

    def run_choose(self, sensitivity_after=None):
        from cortados_router.provider import routing
        routes = []

        def select(core_, **kw):
            allowed = kw["constraints"]["allowed_routes"]
            routes.append(list(allowed))
            api = any(p[0] == routing.API_PROVIDER for p in allowed)
            provider, model_ = (routing.API_PROVIDER, "claude-opus-5-5") if api else ("codex-cli", "gpt-6.1-sol")
            return {"router": "t", "provider": provider, "model": model_, "route_id": kw.get("route_id") or "rid-1",
                    "band": "R2", "degraded": False, "decision": dict(dec()),
                    "route": {"chosen": {"lane": provider.replace("-cli", "") if provider != routing.API_PROVIDER
                                         else provider, "provider": provider, "model": model_}}}
        calls = iter([None, sensitivity_after, sensitivity_after])
        with mock.patch.object(routing.execution_routing, "select", side_effect=select), \
                mock.patch.object(routing, "served_routes", return_value=(("claude-cli", "claude-opus-5-5"),
                                                                          ("codex-cli", "gpt-6.1-sol"))), \
                mock.patch("cortados_router.api_credits.state", return_value={"on": True, "headroom_usd": 150.0}), \
                mock.patch.object(routing, "request_sensitivity", side_effect=lambda *a, **k: next(calls)), \
                mock.patch.object(routing, "sensitive_local", side_effect=lambda core_, body, r, **kw: r), \
                mock.patch.object(routing, "auto_local", side_effect=lambda core_, body, r, **kw: r):
            routing.forget_routes()
            out = routing.choose(object(), self.BODY, client="t", conversation_id="c1")
            routing.forget_routes()
        return out, routes

    def test_a_win_on_the_lane_is_bound_as_a_scored_conversation(self):
        from cortados_router.provider import routing
        out, routes = self.run_choose()
        self.assertEqual((out["provider"], out["bind_reason"]), (routing.API_PROVIDER, routing.API_SCORED_REASON))
        self.assertEqual(len(routes), 1)

    def test_a_request_marked_after_routing_is_routed_again_without_the_lane(self):
        from cortados_router.provider import routing
        out, routes = self.run_choose(sensitivity_after="s1")
        self.assertEqual(len(routes), 2)
        self.assertFalse(any(p[0] == routing.API_PROVIDER for p in routes[1]))
        self.assertEqual(out["provider"], "codex-cli")
        self.assertEqual(out["route_id"], "rid-1")                                      # the same routes row


class ContinuationGate(unittest.TestCase):
    def gate(self, bind, state, recheck=(True, "ok")):
        from cortados_router.provider import routing
        with mock.patch("cortados_router.api_credits.state", return_value=state), \
                mock.patch.object(routing, "request_sensitivity", return_value=None):
            return routing.api_continuation_admission({"messages": []}, {"route_id": "r", "bind_reason": bind},
                                                      local=None, recheck=lambda b, r: recheck)

    def test_a_scored_conversation_needs_the_lane_on_not_an_expiry_window(self):
        from cortados_router.provider import routing
        self.assertTrue(self.gate(routing.API_SCORED_REASON, {"on": True, "expiring_soon": False})[0])
        self.assertFalse(self.gate(routing.API_SCORED_REASON, {"on": False, "reason": "hard stop"})[0])
        ok, why = self.gate(routing.API_SCORED_REASON, {"on": True}, recheck=(False, "outward effect"))
        self.assertFalse(ok); self.assertIn("outward", why)                              # the per-turn re-check stays

    def test_the_expiry_preference_keeps_its_window_and_other_binds_are_refused(self):
        from cortados_router.provider import routing
        self.assertFalse(self.gate(routing.API_BIND_REASON, {"on": True, "expiring_soon": False})[0])
        self.assertTrue(self.gate(routing.API_BIND_REASON, {"on": True, "expiring_soon": True})[0])
        self.assertFalse(self.gate("auto_route", {"on": True, "expiring_soon": True})[0])
        self.assertFalse(self.gate(None, {"on": True, "expiring_soon": True})[0])


if __name__ == "__main__":
    unittest.main()
