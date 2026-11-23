"""#132 routing fixes (owner decisions 2026-11-22): account B's pool-size key, the prior for lower-bound pool sizes, the
switching margin, the reset-aware underuse ranking, the batch workload hint, the S1 degraded alert and the reporting
fields. Fakes only: a fake scarcity module, fake model scores and throwaway homes; no vendor CLI, no live state."""
from __future__ import annotations
import importlib.util
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HERMES_HOME", tempfile.mkdtemp(prefix="cortados-router-test-"))
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("core_rfix", str(ROOT / "cortados_router/core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
_real_sc = core._scarcity_module()                # the real module: pool_for and the history reader are tested as is

ACCOUNTS = [{"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": False, "config_dir": "/x/a"},
            {"lane": "claude#b", "provider": "claude-cli#b", "account": "b", "reserve": False, "config_dir": "/x/b"}]


def week(used: float, h_left: float, now: float, window: str = "week (all models)") -> dict:
    """A weekly window `used` % used that resets in `h_left` hours, as scarcity.window_view reports it."""
    return {"window": window, "pool": "all", "used_pct": used, "headroom_pct": 100 - used,
            "elapsed_pct": round(100 * (1 - h_left / 168), 1), "reset_epoch": now + h_left * 3600,
            "resets_in_h": h_left}


class FakeSc:
    """The scarcity module as core reads it: a cached reading, unit costs and recent burn rates."""
    def __init__(self, lanes: dict, ucs: dict, burns: dict | None = None):
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


# Fake catalogue around the 2026-11-22 R2/R3 rows: Opus 5.5 (ceiling, $8), Sol 6.1 ($4, code 66.2) and Astra
# (ceiling, $20), with R3's code reference 67.0 (so Sol misses R3 code, as in the diagnosis).
MS = {"use_cases": {"code": ""},
      "reference_index": {b: {"general": v, "code": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 67.0), ("R4", 95))},
      "models": [{"family": f, "ceiling": ceil, "proxy": False, "index": {"general": g, "code": c},
                  "price_1m_blended_usd": price, "reach": [{"lane": lane, "provider": prov, "model": model}]}
                 for f, model, g, c, lane, prov, ceil, price in (
                     ("claude-opus-5-5", "claude-opus-5-5", 99.8, 100.0, "claude", "claude-cli", True, 8.0),
                     ("gpt-6-1-sol", "gpt-6.1-sol", 90.1, 66.2, "codex", "openai-codex", False, 4.0),
                     ("gpt-6-astra", "gpt-6-astra", 100.0, 78.5, "codex", "openai-codex", True, 20.0))]}

# unit-cost.json of 2026-11-22 (diagnosis): every learned size a lower bound.
UCS_132 = {"claude:all": {"tokens_per_pct": 5_440_000, "remaining_tokens": 5_440_000 * 40, "lower_bound": True},
           "claude#b:all": {"tokens_per_pct": 4_950_000, "remaining_tokens": 4_950_000 * 77, "lower_bound": True},
           "codex:all": {"tokens_per_pct": 2_670_000, "remaining_tokens": 2_670_000 * 95, "lower_bound": True}}


def dec(steps=3, ambiguity=2, novelty=3, blast=0, outward=False, **kw):
    a = {"steps": steps, "ambiguity": ambiguity, "blast_radius": blast, "context_size": 0, "novelty": novelty,
         "parallelizability": 0, "outward": outward, "work_kind": "code", "urgency": "now", "needs_clarification": False,
         "parts": 0, "diversity": False, **kw}
    return {"answers": a, "degraded": False, "engine": "test", "engine_ms": 1}


class Base(unittest.TestCase):
    def setUp(self):
        self._keep = (core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts)
        core.model_scores = lambda: MS
        core._vendor_accounts = lambda *a: [dict(x) for x in ACCOUNTS]
        self.now = time.time()

    def tearDown(self):
        core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts = self._keep

    def fake(self, lanes, ucs=UCS_132, burns=None, **sc_by_lane):
        core._sc_mod = FakeSc(lanes, ucs, burns)
        core.lane_health = lambda: health(**sc_by_lane)

    def the_132_state(self, with_resets=True):
        """2026-11-22 ~20:00: A 40 % left (synthetic: A was not the router's pick), B 77 % left resetting Wed 07:00
        (59 h), Codex 95 % left resetting Tue 10:34 (62.5 h); scarcities as the routes logged them."""
        n = self.now
        lanes = {"claude": {"fetched_at": n, "windows": [week(60, 59, n)]},
                 "claude#b": {"fetched_at": n, "windows": [week(23, 59, n)]},
                 "codex": {"fetched_at": n, "windows": [week(5, 62.5, n, "week")]}}
        if not with_resets:
            for v in lanes.values():
                for w in v["windows"]:
                    w["reset_epoch"] = None
        self.fake(lanes, **{"claude": 0.2, "claude#b": -0.94, "codex": -1.0})


class PoolSizeKeyAndPrior(Base):
    """Items 1 and 2."""
    def test_account_b_uses_its_own_learned_pool_size(self):
        self.fake({"claude#b": {"windows": []}})
        b = {"lane": "claude#b", "model": "claude-opus-5-5", "family": "claude-opus-5-5", "config_dir": "/x/b"}
        self.assertEqual(core._pool_tokens_per_pct(b), 4_950_000)                   # claude#b:all, not A's claude:all
        self.assertEqual(core._pool_tokens_per_pct({**b, "lane": "claude"}), 5_440_000)
        # its remaining is its own learned record (77 % of B's size), not A's
        self.assertEqual(core._tokens_left_for(b), 4_950_000 * 77)

    def test_account_b_falls_back_to_the_vendor_size_while_unlearned(self):
        ucs = {k: v for k, v in UCS_132.items() if k != "claude#b:all"}
        self.fake({"claude#b": {"windows": [week(30, 50, self.now)]}}, ucs)
        b = {"lane": "claude#b", "model": "claude-opus-5-5", "config_dir": "/x/b"}
        self.assertEqual(core._pool_tokens_per_pct(b), 5_440_000)
        self.assertEqual(core._tokens_left_for(b), 5_440_000 * 70)                # B's own headroom × the shared size

    def test_lower_bound_size_never_undercuts_the_prior(self):
        self.fake({"codex": {"windows": []}})
        sol = {"lane": "codex", "model": "gpt-6.1-sol"}
        prior = core.table()["selection"]["pool_size_prior_tokens_per_pct"]["codex:all"]
        self.assertEqual(core._pool_tokens_per_pct(sol), prior)                     # 2.67 M lower bound → the prior
        self.assertAlmostEqual(core._tokens_left_for(sol), prior * 95, delta=1)    # same % left, the bigger pool
        # a learned size above the prior stands, and so does one that is not a lower bound
        self.fake({"codex": {"windows": []}}, {"codex:all": {"tokens_per_pct": 9_000_000, "lower_bound": True}})
        self.assertEqual(core._pool_tokens_per_pct(sol), 9_000_000)
        self.fake({"codex": {"windows": []}}, {"codex:all": {"tokens_per_pct": 2_000_000}})
        self.assertEqual(core._pool_tokens_per_pct(sol), 2_000_000)
        # no learned size: no prior is invented (price-only weight, as before)
        self.fake({"codex": {"windows": []}}, {})
        self.assertIsNone(core._pool_tokens_per_pct(sol))

    def test_prior_only_for_the_pools_the_table_names(self):
        self.fake({"claude": {"windows": []}}, {"claude:all": {"tokens_per_pct": 1_000, "lower_bound": True}})
        self.assertEqual(core._pool_tokens_per_pct({"lane": "claude", "model": "claude-opus-5-5"}), 1_000)


class ExpectedLoss(Base):
    """Item 4: the share expected to be lost at the reset."""
    def test_pace_projection(self):
        n = self.now
        # 23 % used over 109 h → 0.211 %/h; 59 h left → 12.4 % more; 77 − 12.4 = 64.6 % lost
        self.assertAlmostEqual(core.window_expected_loss_pct(week(23, 59, n), n, n), 64.6, delta=0.2)
        # a learned recent burn replaces the pace
        self.assertEqual(core.window_expected_loss_pct(week(23, 59, n), n, n, burn_pct_per_h=1.0), 18.0)
        self.assertEqual(core.window_expected_loss_pct(week(23, 59, n), n, n, burn_pct_per_h=2.0), 0.0)
        # no reset time, or the reset already passed: unknown
        self.assertIsNone(core.window_expected_loss_pct({**week(23, 59, n), "reset_epoch": None}, n, n))
        self.assertIsNone(core.window_expected_loss_pct(week(23, 59, n), n, n + 60 * 3600))

    def test_pool_resetting_tomorrow_outranks_one_in_four_days_only_when_the_loss_says_so(self):
        n = self.now
        lanes = {"claude#b": {"fetched_at": n, "windows": [week(81, 24, n)]},      # 19 % left, resets tomorrow
                 "codex": {"fetched_at": n, "windows": [week(21, 96, n, "week")]}}  # 79 % left, resets in 4 days
        tomorrow = {"lane": "claude#b", "model": "claude-opus-5-5", "config_dir": "/x/b"}
        four_days = {"lane": "codex", "model": "gpt-6-astra"}
        # at each window's own pace: 19 − 0.5625×24 = 5.5 % lost vs 79 − 0.2917×96 = 51 % → four days outranks
        self.fake(lanes, **{"claude#b": -0.5, "codex": -0.5})                     # both underused by the capped measure
        self.assertAlmostEqual(core._expected_loss_pct(tomorrow), 5.5, delta=0.2)
        self.assertAlmostEqual(core._expected_loss_pct(four_days), 51.0, delta=0.2)
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), accounts_aware=True)   # D=9: ceiling_underuse
        self.assertEqual((r["chosen"]["lane"], r["chosen"]["model"]), ("codex", "gpt-6-astra"))
        # learned burn: the tomorrow pool went idle (0.1 %/h), the four-day pool is burning 1 %/h →
        # 19 − 2.4 = 16.6 % lost vs 79 − 96 → 0 % lost: tomorrow now outranks
        self.fake(lanes, burns={("claude#b", "week (all models)"): 0.1, ("codex", "week"): 1.0},
                  **{"claude#b": -0.5, "codex": -0.5})
        self.assertAlmostEqual(core._expected_loss_pct(tomorrow), 16.6, delta=0.1)
        self.assertEqual(core._expected_loss_pct(four_days), 0.0)
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), accounts_aware=True)
        self.assertEqual((r["chosen"]["lane"], r["chosen"]["model"]), ("claude#b", "claude-opus-5-5"))
        self.assertTrue(any("would be lost at the reset" in x for x in r["hard_rules"]))

    def test_missing_reset_times_keep_todays_ranking(self):
        self.the_132_state(with_resets=False)
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), accounts_aware=True)
        self.assertIsNone(r["chosen"].get("expected_loss_pct"))
        rule = next(x for x in r["hard_rules"] if "paid-for frontier capacity" in x)
        self.assertIn("scarcity", rule)                                             # ranked by cost, as before
        # and the underuse credit is −scarcity × 80, as before (no expected loss on any route)
        r2 = core.resolve(dec(), accounts_aware=True)
        self.assertTrue(all(c.get("expected_loss_pct") is None for c in [r2["chosen"]] + r2["alternatives"]))

    def test_five_hour_windows_do_not_count_and_stale_ones_fall_back(self):
        n = self.now
        five = {"window": "5-hour session", "pool": "all", "used_pct": 99, "elapsed_pct": 50, "reset_epoch": n + 3600}
        self.fake({"codex": {"fetched_at": n, "windows": [week(5, 62.5, n, "week"), five]}})
        self.assertGreater(core._expected_loss_pct({"lane": "codex", "model": "gpt-6.1-sol"}), 90)
        self.fake({"codex": {"fetched_at": n, "windows": [{**week(5, 62.5, n, "week"), "stale": True}]}})
        self.assertIsNone(core._expected_loss_pct({"lane": "codex", "model": "gpt-6.1-sol"}))

    def test_recent_burn_rates_from_the_history(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        n = self.now
        rows = [{"ts": n - 20 * 3600, "lanes": {"codex": {"windows": [["week", 90.0, 99.0]]}}},   # before a reset
                {"ts": n - 18 * 3600, "lanes": {"codex": {"windows": [["week", 1.0, 1.0]]},
                                                "claude#b": {"windows": [["week (all models)", 10.0, 40.0]]}}},
                {"ts": n - 2 * 3600, "lanes": {"codex": {"windows": [["week", 9.0, 10.5]]},
                                               "claude#b": {"windows": [["week (all models)", 11.0, 49.0]]}}},
                {"ts": n - 1 * 3600, "lanes": {"claude#b": {"windows": [["week (all models)", 12.0, 50.0]]}}}]
        (d / "h.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        with mock.patch.object(_real_sc, "HISTORY", d / "h.jsonl"):
            _real_sc._burn_cache["key"] = None
            rates = _real_sc.recent_burn_rates(now=n)
        self.assertAlmostEqual(rates[("codex", "week")], 8.0 / 16, places=3)          # since the reset only
        self.assertAlmostEqual(rates[("claude#b", "week (all models)")], 2.0 / 17, places=3)


class R2Replay(Base):
    """The diagnosis's R2 case: Opus on B (weight 19.6, scarcity −0.94) beat Sol (20.0, −1.0) by 1–3 points because B
    was sized as A (5.44 M) and Codex at its 2.67 M lower bound. With B's own size, the Codex prior and the loss at
    reset, the corrected inputs decide it by a clear margin."""
    def test_corrected_inputs_decide_r2(self):
        self.the_132_state()
        r = core.resolve(dec(), accounts_aware=True)                    # D=8 code → R2, needs 61.3: Sol 66.2 clears it
        self.assertEqual(r["band"], "R2")
        every = {(c["lane"], c["model"]): c for c in [r["chosen"]] + (r["alternatives"] or [])}
        opus_b, sol = every[("claude#b", "claude-opus-5-5")], every[("codex", "gpt-6.1-sol")]
        # burns: Opus 8 / 4.95 = 1.616 (was 8 / 5.44 = 1.47 under A's key), Sol 4 / 5.63 = 0.710 (was 4 / 2.67 = 1.50),
        # Astra 20 / 5.63 = 3.55; the heaviest reachable route (Opus on A: 8 / 5.44 … no: Astra) is 100
        self.assertLess(sol["weight"], opus_b["weight"])
        # losses at reset: B 64.6 % (credit 51.7), Codex ≈ 92 % (credit ≈ 73.6) instead of both ≈ 76–80
        self.assertAlmostEqual(opus_b["expected_loss_pct"], 64.6, delta=0.3)
        self.assertGreater(sol["expected_loss_pct"], 90)
        self.assertEqual((r["chosen"]["lane"], r["chosen"]["model"]), ("codex", "gpt-6.1-sol"))
        self.assertGreater(opus_b["cost"] - sol["cost"], float(core.table()["selection"]["switch_margin_points"]))
        # no flap: a client whose previous R2 route was Opus on B moves (the gap is beyond the margin)
        r = core.resolve(dec(), accounts_aware=True, previous={"R2": {"lane": "claude#b", "model": "claude-opus-5-5"}})
        self.assertEqual(r["chosen"]["model"], "gpt-6.1-sol")


    def test_pre_fix_inputs_reproduce_the_diagnosis(self):
        """B sized under A's key, no prior, no reset times: Opus on B 19.6 / −0.94 beats Sol 20.0 / −1.0 by a few points,
        the rows the 2026-11-22 diagnosis read (−75.6 vs about −72)."""
        self.the_132_state(with_resets=False)
        ucs = {k: v for k, v in UCS_132.items() if k != "claude#b:all"}
        self.fake(core._sc_mod.lanes, ucs, **{"claude": 0.2, "claude#b": -0.94, "codex": -1.0})
        t = json.loads(json.dumps(core.table())); t["selection"]["pool_size_prior_tokens_per_pct"] = {}
        # without its own record B falls back to A's size and its remaining from its own headroom: the old lookup
        with mock.patch.object(core, "table", return_value=t):
            r = core.resolve(dec(), accounts_aware=True)
        every = {(c["lane"], c["model"]): c for c in [r["chosen"]] + (r["alternatives"] or [])}
        opus_b, sol = every[("claude#b", "claude-opus-5-5")], every[("codex", "gpt-6.1-sol")]
        self.assertEqual((r["chosen"]["lane"], r["chosen"]["model"]), ("claude#b", "claude-opus-5-5"))
        self.assertEqual((opus_b["weight"], sol["weight"]), (19.6, 20.0))
        self.assertEqual(opus_b["cost"], -75.6)
        self.assertLessEqual(sol["cost"] - opus_b["cost"], 4.0)


class SwitchingMargin(Base):
    """Item 3: one Claude login, no learned sizes (price-only weights), both pools at the capped −1.0."""
    def test_margin_holds_a_one_point_swing_and_releases_a_big_one(self):
        core._vendor_accounts = lambda *a: [dict(ACCOUNTS[0], account=None, config_dir=None)]
        self.fake({"claude": {"windows": []}, "codex": {"windows": []}}, {}, claude=-1.0, codex=-1.0)
        # price-only weights (no learned sizes): Opus 8 → 40, Sol 4 → 20 of the $20 non-ceiling-max… make them close:
        ms = json.loads(json.dumps(MS))
        ms["models"][1]["price_1m_blended_usd"] = 7.8                                # Sol a hair cheaper than Opus
        core.model_scores = lambda: ms
        r = core.resolve(dec())
        self.assertEqual(r["chosen"]["model"], "gpt-6.1-sol")
        c = {x["model"]: x["cost"] for x in [r["chosen"]] + r["alternatives"]}
        self.assertLessEqual(c["claude-opus-5-5"] - c["gpt-6.1-sol"], 3.0)         # a 1–3 point swing
        held = core.resolve(dec(), previous={"R2": {"lane": "claude", "model": "claude-opus-5-5"}})
        self.assertEqual(held["chosen"]["model"], "claude-opus-5-5")               # within 3 points: stays
        self.assertTrue(any("switching margin" in x for x in held["hard_rules"]))
        # a previous route of another band, or on another lane, is not held
        self.assertEqual(core.resolve(dec(), previous={"R3": {"lane": "claude", "model": "claude-opus-5-5"}})["chosen"]["model"],
                         "gpt-6.1-sol")
        self.assertEqual(core.resolve(dec(), previous={"R2": {"lane": "claude#b", "model": "claude-opus-5-5"}})["chosen"]["model"],
                         "gpt-6.1-sol")
        ms["models"][1]["price_1m_blended_usd"] = 4.0                                # a real gap
        moved = core.resolve(dec(), previous={"R2": {"lane": "claude", "model": "claude-opus-5-5"}})
        self.assertEqual(moved["chosen"]["model"], "gpt-6.1-sol")
        # an unavailable previous route is never held
        core.lane_health = lambda: {**health(codex=-1.0), "claude": {"ok": False, "why": "spent", "scarcity": 1.0,
                                                                     "expiring_pct": 0, "tightest": {}, "pools": {}}}
        ms["models"][1]["price_1m_blended_usd"] = 7.8
        self.assertEqual(core.resolve(dec(), previous={"R2": {"lane": "claude", "model": "claude-opus-5-5"}})["chosen"]["model"],
                         "gpt-6.1-sol")

    def test_margin_off_at_zero(self):
        core._vendor_accounts = lambda *a: [dict(ACCOUNTS[0], account=None, config_dir=None)]
        self.fake({"claude": {"windows": []}, "codex": {"windows": []}}, {}, claude=-1.0, codex=-1.0)
        ms = json.loads(json.dumps(MS)); ms["models"][1]["price_1m_blended_usd"] = 7.8
        core.model_scores = lambda: ms
        t = json.loads(json.dumps(core.table())); t["selection"]["switch_margin_points"] = 0
        with mock.patch.object(core, "table", return_value=t):
            r = core.resolve(dec(), previous={"R2": {"lane": "claude", "model": "claude-opus-5-5"}})
        self.assertEqual(r["chosen"]["model"], "gpt-6.1-sol")

    def test_provider_remembers_the_last_route_per_client_and_band(self):
        from cortados_router.provider import routing
        routing.forget_routes(); self.addCleanup(routing.forget_routes)
        self.assertIsNone(routing.previous_routes("claude-cli"))
        routing.remember_route("claude-cli", {"band": "R2", "route": {"chosen": {"lane": "claude#b", "model": "m"}}})
        routing.remember_route("python-httpx", {"band": "R1", "route": {"chosen": {"lane": "codex", "model": "x"}}})
        self.assertEqual(routing.previous_routes("claude-cli"), {"R2": {"lane": "claude#b", "model": "m"}})
        self.assertEqual(routing.previous_routes("python-httpx"), {"R1": {"lane": "codex", "model": "x"}})
        routing.remember_route("claude-cli", {"band": "R2", "route": {}})          # nothing chosen: unchanged
        self.assertEqual(routing.previous_routes("claude-cli")["R2"]["model"], "m")


class BatchHint(Base):
    """Item 5: x-cortados-workload: batch caps the band at R1 and is background work; the hard rules win."""
    def setUp(self):
        super().setUp()
        self.fake({}, {}, claude=0.0, codex=0.0)

    def test_cap_lowers_r2_and_r3(self):
        self.assertEqual(core.resolve(dec(), maximum_band="R1")["band"], "R1")
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), maximum_band="R1")
        self.assertEqual(r["band"], "R1"); self.assertIn("client workload hint caps band at R1", r["hard_rules"])
        self.assertEqual(core.resolve(dec(steps=0, ambiguity=0, novelty=0), maximum_band="R1")["band"], "R0")   # only lowers

    def test_outward_request_is_still_r3(self):
        r = core.resolve(dec(steps=1, ambiguity=0, novelty=0, outward=True), "background", maximum_band="R1")
        self.assertEqual(r["band"], "R3"); self.assertEqual(r["features"]["blast_radius"], 3)
        self.assertTrue(any("workload cap R1 not applied" in x for x in r["hard_rules"]))
        self.assertEqual(core.resolve(dec(blast=3), maximum_band="R1")["band"], "R3")

    def test_min_band_still_floors_after_the_cap(self):
        self.assertEqual(core.resolve(dec(), maximum_band="R1", minimum_band="R2")["band"], "R2")
        with self.assertRaises(ValueError):
            core.resolve(dec(), maximum_band="R9")

    def test_select_routes_batch_as_background_and_degraded_never_claude(self):
        from cortados_router import execution_routing
        from cortados_router.provider import routing
        seen = {}
        real = core.resolve

        def spy(decision, unit_kind="interactive", **kw):
            seen.update(unit=unit_kind, maximum_band=kw.get("maximum_band"))
            return real(decision, unit_kind, **kw)
        degraded = {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 30000, "error": "timeout"}
        allowed = (("claude-cli", "claude-opus-5-5"), ("openai-codex", "gpt-6.1-sol"), ("openai-codex", "gpt-6-astra"))
        with mock.patch.object(core, "resolve", spy), mock.patch.object(core, "decide", return_value=degraded), \
                mock.patch.object(core, "log_route", return_value=True) as log:
            r = execution_routing.select(routing._NoClaudeWhenDegraded(core), prompt="summarise", platform="cli",
                                         constraints={"workload": "batch", "allowed_routes": [list(p) for p in allowed]},
                                         credential_resolver=lambda **k: None)
        self.assertEqual((seen["unit"], seen["maximum_band"]), ("background", "R1"))
        self.assertEqual(r["band"], "R1")                                          # the background fallback band
        self.assertNotEqual(r["provider"].split("#")[0], "claude-cli")             # degraded: never Claude
        self.assertEqual(r["workload"], "batch")
        meta = log.call_args[0][1]
        self.assertEqual((meta["workload"], meta["unit_kind"]), ("batch", "background"))

    def test_header_values(self):
        from cortados_router.provider import routing
        self.assertEqual(routing.workload_hint("Batch "), "batch")
        self.assertIsNone(routing.workload_hint(None)); self.assertIsNone(routing.workload_hint(""))
        with self.assertLogs("cortados.provider.routing", logging.INFO) as cm:
            self.assertIsNone(routing.workload_hint("urgent"))
        self.assertIn("unknown value", cm.output[0])

    def test_route_row_records_the_hint(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(core, "STATE", Path(d)):
            r = core.resolve(dec(), maximum_band="R1")
            core.log_route("t1", {"session_id": "s", "workload": "batch", "unit_kind": "background"},
                           dec(), r)
            core.log_route("t2", {"session_id": "s"}, dec(), r)
        c = sqlite3.connect(Path(d) / "router.db")
        self.assertEqual(c.execute("SELECT turn_id, workload, unit_kind FROM routes ORDER BY turn_id").fetchall(),
                         [("t1", "batch", "background"), ("t2", None, None)])
        c.close()


class S1Alert(unittest.TestCase):
    """Item 6: the degraded share per 5 / 60 minutes from the route log, with a warning above the threshold."""
    def test_route_log_windows_and_warning(self):
        from cortados_router.provider import usage, live
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(core, "STATE", d):
            core._db().close()
        now = 1_800_000_000.0
        c = sqlite3.connect(d / "router.db")
        rows = [(f"a{i}", now - 60, 0, 4000 + 100 * i, "s1-model") for i in range(6)]      # 5 min: 6 answered …
        rows += [(f"d{i}", now - 120, 1, 30000, "s1") for i in range(4)]                   # … and 4 degraded (40 %)
        rows += [(f"o{i}", now - 1800, 0, 9000, "s1-model") for i in range(10)]            # earlier in the hour
        rows += [("pin", now - 30, 0, None, "pin"), ("old", now - 7200, 1, 30000, "s1")]   # a pin and an old row: out
        c.executemany("INSERT INTO routes (turn_id, ts, degraded, engine_ms, engine) VALUES (?,?,?,?,?)", rows)
        c.commit(); c.close()
        s = usage._s1_routes(d / "router.db", now)
        w5, w60 = s["windows"]
        self.assertEqual((w5["window"], w5["calls"], w5["degraded"], w5["degraded_pct"]), ("5m", 10, 4, 40.0))
        self.assertEqual((w60["calls"], w60["degraded"], w60["degraded_pct"]), (20, 4, 20.0))
        self.assertEqual((w5["s1_ms_p50"], w5["s1_ms_p90"], w5["degraded_wait_ms_p50"]), (4200.0, 4500.0, 30000.0))
        sec = live.s1_section({"s1": {"available": True, "status": 200, "latency_ms": 3}, "s1_routes": s}, now)
        self.assertEqual(sec["status"], "up")                                     # /health is fine …
        self.assertEqual(sec["routing"]["status"], "warn")                        # … but 40 % degraded in 5 min
        self.assertIn("40% of 10 calls degraded in 5m", sec["routing"]["warning"])
        self.assertNotIn("60m", sec["routing"]["warning"])                       # 20 % is not above 20 %
        quiet = live.s1_routing({"s1_routes": {**s, "windows": [{**w5, "degraded": 0, "degraded_pct": 0.0}, w60]}})
        self.assertEqual((quiet["status"], quiet["warning"]), ("ok", None))
        few = live.s1_routing({"s1_routes": {**s, "windows": [{**w5, "calls": 2, "degraded": 2, "degraded_pct": 100.0}]}})
        self.assertEqual(few["status"], "ok")                                     # below min_calls: no alarm
        self.assertEqual(live.s1_routing({})["status"], "unknown")
        # published through the usage view's schema (metadata only)
        pub = usage._publish({"generated_at": now, "s1_routes": s}, [])
        self.assertEqual(pub["s1_routes"]["windows"][0]["degraded_pct"], 40.0)


class Reporting(unittest.TestCase):
    """Item 7: elapsed null while unrecorded, an end-to-end time, provider and model as separate fields."""
    def test_fields(self):
        from cortados_router.provider import tasks
        from tests import test_tasks as tt
        d = tt.home(); self.addCleanup(shutil.rmtree, d, True)
        tt.insert(d, turn_id="done", ts=tt.NOW, engine_ms=800, turn_ms=1200, actual_model="claude-cli#b/claude-opus-5-5")
        tt.insert(d, turn_id="zero", ts=tt.NOW - 1, engine_ms=500, turn_ms=0, actual_model=None)
        tt.insert(d, turn_id="open", ts=tt.NOW - 2, engine_ms=500, turn_ms=None, actual_model=None)
        tt.pin(d, turn_id="pin", ts=tt.NOW - 3, turn_ms=300, actual_model="codex-cli/gpt-5")
        v = {t["task_id"]: t for t in tasks.collect(d)["tasks"]}
        self.assertEqual((v["done"]["elapsed_ms"], v["done"]["s1_ms"], v["done"]["end_to_end_ms"]), (1200, 800, 2000))
        self.assertEqual((v["done"]["executed_provider"], v["done"]["executed_model"], v["done"]["actual_model"]),
                         ("claude-cli#b", "claude-opus-5-5", "claude-cli#b/claude-opus-5-5"))
        for k in ("zero", "open"):
            self.assertIsNone(v[k]["elapsed_ms"]); self.assertIsNone(v[k]["end_to_end_ms"])   # null, never 0
            self.assertIsNone(v[k]["executed_model"])
        self.assertEqual((v["pin"]["s1_ms"], v["pin"]["end_to_end_ms"], v["pin"]["executed_model"]), (None, 300, "gpt-5"))


if __name__ == "__main__":
    unittest.main()
