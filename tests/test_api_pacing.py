"""Scarcity v2 §9 step 4 (owner 2026-11-24): the API credit grant ranked like any other pool, by its percent used against
its pace to expiry (core.api_pace, selection.api_pacing, routing table v22). Fakes only: the fake scarcity module and
model scores of tests/test_routing_underuse_143.py, throwaway homes; no vendor CLI, no network, no live state."""
from __future__ import annotations

import collections
import copy
import json
import unittest
from unittest import mock

from tests.test_routing_underuse_143 import (Base, SERVED, WITH_API, core, dec)

_TABLE = core.table()


def table_with(pacing):
    """The checked-in table with selection.api_pacing on (True), off (False) or absent (None: table v21's selection)."""
    t = copy.deepcopy(_TABLE)
    if pacing is None:
        t["selection"].pop("api_pacing", None)       # (the version number is left as is: it is echoed in each decision)
    else:
        t["selection"]["api_pacing"] = {**t["selection"]["api_pacing"], "enabled": pacing}
    return t


T_ON, T_OFF, T_V21 = table_with(True), table_with(False), table_with(None)
LIGHT = dec(steps=2, ambiguity=1, novelty=1, blast=1)                  # D=5 code, R1: the API lane may take it


class PacingBase(Base):
    TABLE = T_ON

    def use(self, t):
        core.table = lambda: t

    def lanes(self, d=LIGHT, n=600, prefix="k", allowed=WITH_API, **kw):
        return collections.Counter(self.resolve(d, allowed=allowed, split_key=f"{prefix}{i}", **kw)["chosen"]["lane"]
                                   for i in range(n))


class Table(unittest.TestCase):
    def test_v22_ships_the_flag_on(self):
        # v22 introduced the flag; later tables (v23: #144 PR A) keep it, so pin the floor, not the exact number
        self.assertGreaterEqual(_TABLE["version"], 22)
        self.assertIs(_TABLE["selection"]["api_pacing"]["enabled"], True)
        core.validate_table(_TABLE)

    def test_the_flag_is_the_only_selection_change(self):
        # everything v21 had is still there, unchanged: the flag off is v21
        self.assertEqual({k: v for k, v in T_OFF["selection"].items() if k != "api_pacing"}, T_V21["selection"])


class Pace(PacingBase):
    def test_the_standardized_percent_used_and_its_pace(self):
        # $95 spent of the $190 stop line, 100 h to the last counting hour, absorbing 0.25 %/h
        self.audit_state(api_used=50.0, api_h_left=100.0, burns={(core.API_LANE, "grant"): 0.25})
        p = core.api_pace(self.now)
        self.assertEqual(p["used_pct"], 50.0)
        self.assertAlmostEqual(p["needed_pct_h"], 0.5, places=3)               # (100 − 50) / 100 h
        self.assertAlmostEqual(p["actual_pct_h"], 0.25, places=3)
        self.assertAlmostEqual(p["forecast_pct"], 75.0, places=1)              # 50 + 0.25 × 100
        self.assertAlmostEqual(p["share"], 0.5 / 0.75, places=3)

    def test_expiry_pressure_is_one_over_the_hours_left(self):
        self.audit_state(api_used=50.0, api_h_left=100.0, burns={(core.API_LANE, "grant"): 0.25})
        far = core.api_pace(self.now)
        self.audit_state(api_used=50.0, api_h_left=25.0, burns={(core.API_LANE, "grant"): 0.25})
        near = core.api_pace(self.now)
        self.assertAlmostEqual(near["needed_pct_h"], 4 * far["needed_pct_h"], places=3)
        self.assertGreater(near["share"], far["share"])

    def test_no_learned_burn_falls_back_to_the_windows_own_pace(self):
        self.audit_state(api_used=0.0, api_h_left=211.0)                       # nothing spent, no history
        p = core.api_pace(self.now)
        self.assertEqual(p["actual_pct_h"], 0.0)
        self.assertEqual(p["share"], 1.0)                                       # absorbing nothing: it takes the split

    def test_a_stale_or_failed_reading_gives_no_pace(self):
        self.audit_state()
        self.assertIsNotNone(core.api_pace(self.now))
        core._sc_mod.reading_freshness = lambda lane, v: (False, False)
        self.assertIsNone(core.api_pace(self.now))
        r = self.resolve(LIGHT, split_key="s")
        self.assertIn("api pacing: no fresh grant reading; the grant takes no paced share", r["hard_rules"])
        core._sc_mod.reading_freshness = lambda lane, v: (True, True)
        self.assertIsNone(core.api_pace(self.now))

    def test_a_failure_is_named_in_the_decision_never_silent(self):
        self.audit_state()
        core._sc_mod.recent_burn_rates = lambda *a, **k: 1 / 0
        self.assertEqual(core.api_pace(self.now), {"error": "ZeroDivisionError"})
        r = self.resolve(LIGHT, split_key="s")
        self.assertIn("api pacing: failed (ZeroDivisionError); the grant takes no paced share", r["hard_rules"])


class Pacing(PacingBase):
    def api_count(self, used, h_left, burn, n=600):
        self.audit_state(api_used=used, api_h_left=h_left, burns={(core.API_LANE, "grant"): burn})
        return self.lanes(n=n)[core.API_LANE]

    def test_lagging_its_pace_pulls_more_work_and_ahead_less(self):
        # 40 % used, 100 h left: needs 0.6 %/h
        far_behind = self.api_count(40.0, 100.0, 0.1)
        behind = self.api_count(40.0, 100.0, 0.4)
        ahead = self.api_count(40.0, 100.0, 0.7)                                # forecast 110 %: reaches its stop line
        self.assertGreater(far_behind, behind)
        self.assertGreater(behind, 0)
        self.assertLess(ahead, behind)
        self.assertEqual(ahead, 0)

    def test_it_takes_more_than_v21_gave_it_while_it_lags(self):
        self.audit_state()                                                     # the audit's state: $0 spent, 211 h
        on = self.lanes(n=1000)[core.API_LANE]
        self.use(T_OFF)
        off = self.lanes(n=1000)[core.API_LANE]
        self.assertGreater(on, 2 * max(off, 1))

    def test_the_subscriptions_keep_their_own_proportions(self):
        self.audit_state(api_used=40.0, api_h_left=100.0, burns={(core.API_LANE, "grant"): 0.4})
        on = self.lanes(n=1500)
        self.use(T_OFF)
        off = self.lanes(n=1500, allowed=SERVED)
        share = lambda c: c["codex"] / (c["codex"] + c["claude#b"])
        self.assertAlmostEqual(share(on), share(off), delta=0.06)
        self.assertGreater(on["codex"], 0); self.assertGreater(on["claude#b"], 0)

    def test_no_special_weight_the_v21_discount_is_gone(self):
        self.audit_state()
        api_and_sol = tuple(sorted({(core.API_LANE, m) for m in ("claude-opus-5-5", "claude-sonnet-5")}
                                   | {("codex-cli", "gpt-6.1-sol"), ("codex-cli", "gpt-6-astra")}))
        on = self.every(self.resolve(dec(), allowed=api_and_sol))[(core.API_LANE, "claude-opus-5-5")]
        self.use(T_OFF)
        off = self.every(self.resolve(dec(), allowed=api_and_sol))[(core.API_LANE, "claude-opus-5-5")]
        self.assertLessEqual(off["weight"], 1.0)                               # v21: "free until the grant lapses"
        self.assertGreater(on["weight"], 50)                                   # paced: its burn, as any pool

    def test_its_scarcity_is_its_pace_to_expiry(self):
        # 70 % used with 80 % of its life gone: window_view calls that over pace (> 0), but it lags its stop line
        self.audit_state(api_used=70.0, api_h_left=60.0, burns={(core.API_LANE, "grant"): 0.2},
                         **{core.API_LANE: 0.1})
        r = self.resolve(LIGHT, split_key="s1")
        self.assertTrue(any(x.startswith("api pacing:") and "forecast 82% at expiry" in x for x in r["hard_rules"]),
                        r["hard_rules"])
        self.assertGreater(self.lanes(n=300)[core.API_LANE], 0)
        self.use(T_OFF)
        self.assertEqual(self.lanes(n=300)[core.API_LANE], 0)                  # v21: over pace, no share at all

    def test_it_takes_part_until_its_stop_line_not_five_points_before(self):
        # forecast 97 %: a subscription this close is out of the split (min_loss_pct 5); the grant's 100 % is its stop
        self.audit_state(api_used=93.0, api_h_left=10.0, burns={(core.API_LANE, "grant"): 0.4})
        self.assertGreater(self.lanes(n=400)[core.API_LANE], 0)

    def test_hard_work_on_spare_frontier_pools_is_paced_too(self):
        self.audit_state(api_used=40.0, api_h_left=100.0, burns={(core.API_LANE, "grant"): 0.1})
        d = dec(steps=3, ambiguity=2, novelty=2, blast=1)                      # D 8: ceiling_underuse
        r = self.resolve(d, split_key="h1")
        self.assertTrue(any("api pacing" in x for x in r["hard_rules"]))
        self.assertGreater(self.lanes(d, n=300, prefix="h")[core.API_LANE], 0)


class MoneyRules(PacingBase):
    def test_no_api_work_past_the_stop_line(self):
        self.audit_state(api_used=100.0, api_h_left=100.0, **{core.API_LANE: 1.0})   # spent: the reading refuses
        self.assertEqual(self.lanes(n=300)[core.API_LANE], 0)
        # even a reading whose health still says ok: pacing refuses a grant at its stop line
        self.audit_state(api_used=100.0, api_h_left=100.0)
        self.assertEqual(self.lanes(n=300)[core.API_LANE], 0)
        r = self.resolve(LIGHT, split_key="x")
        self.assertTrue(any("past its stop line" in (s.get("why") or "") for s in r["skipped"]
                            if s["lane"] == core.API_LANE))

    def test_no_api_work_past_its_last_counting_hour(self):
        self.audit_state(api_used=10.0, api_h_left=-0.5)
        self.assertEqual(self.lanes(n=300)[core.API_LANE], 0)
        self.assertEqual(core.api_pace(self.now)["share"], 0.0)

    def test_sensitive_outward_wide_blast_and_degraded_still_refused(self):
        self.audit_state()
        for d in (dec(outward=True), dec(blast=2), dec(degraded=True), dec(steps=2, ambiguity=1, outward=True)):
            for i in range(60):
                r = self.resolve(d, split_key=f"o{i}")
                self.assertNotEqual((r["chosen"] or {}).get("lane"), core.API_LANE)
        # sensitive and local-only work never reaches core with the lane (the provider's api_lane_pairs): unchanged
        from cortados_router.provider import routing
        on = {"on": True, "expiring_soon": False, "headroom_usd": 150.0}
        body = {"model": "cortados", "max_tokens": 1000, "messages": [{"role": "user", "content": "rename a variable"}]}
        with mock.patch("cortados_router.api_credits.state", return_value=on):
            self.assertTrue(routing.api_lane_pairs(core, body, (("claude-cli", "claude-sonnet-5"),), local=None))
            self.assertEqual(routing.api_lane_pairs(core, body, (("claude-cli", "claude-sonnet-5"),),
                                                    local="sensitive"), [])
            with mock.patch.object(routing, "request_sensitivity", return_value="keyword"):
                self.assertEqual(routing.api_lane_pairs(core, body, (("claude-cli", "claude-sonnet-5"),),
                                                        local=None), [])


class FlagOffIsV21(PacingBase):
    """The flag off decides exactly what table v21 decided, field for field, on the route tests' states and more."""

    STATES = [dict(), dict(api_used=40.0, api_h_left=100.0, burns={(core.API_LANE, "grant"): 0.1}),
              dict(api_used=70.0, api_h_left=60.0, burns={(core.API_LANE, "grant"): 0.2}),
              dict(api_used=100.0, api_h_left=10.0), dict(api_used=10.0, api_h_left=-1.0),
              dict(burns={("codex", "week"): 5.0})]
    DECISIONS = [LIGHT, dec(), dec(outward=True), dec(blast=2), dec(degraded=True),
                 dec(steps=3, ambiguity=2, novelty=2, blast=1), dec(steps=3, ambiguity=2, novelty=2, blast=0,
                                                                     work_kind="planning")]

    def test_byte_identical_decisions(self):
        n = 0
        for st in self.STATES:
            for d in self.DECISIONS:
                for allowed in (WITH_API, SERVED):
                    for key in (None, "a1", "a2", "a3", "b7"):
                        out = {}
                        for name, t in (("v21", T_V21), ("off", T_OFF)):
                            self.use(t)
                            self.audit_state(**st)
                            out[name] = json.dumps(self.resolve(copy.deepcopy(d), allowed=allowed,
                                                                **({"split_key": key} if key else {})),
                                                   sort_keys=True, default=str)
                        self.assertEqual(out["v21"], out["off"], (st, d["answers"], key))
                        n += 1
        self.assertGreater(n, 400)


class ThreeDayRuleRetired(unittest.TestCase):
    ON = {"on": True, "expiring_soon": True, "headroom_usd": 100.0, "prefer_within_days": 3.0,
          "grant": {"expires_at": 0}}

    def test_the_expiry_preference_no_longer_moves_work(self):
        from cortados_router.provider import routing
        from tests.test_api_credit_lane import BODY, FakeCore, local_route

        class PacedCore(FakeCore):
            def api_pacing_cfg(self):
                return {"enabled": True}
        r = local_route()
        with mock.patch("cortados_router.api_credits.state", return_value=self.ON):
            self.assertIs(routing.prefer_api_credit(PacedCore(), BODY, r, client="t", conversation_id="c"), r)
            moved = routing.prefer_api_credit(FakeCore(), BODY, r, client="t", conversation_id="c")
        self.assertEqual(moved["provider"], routing.API_PROVIDER)               # the flag off: v21's rule

    def test_the_checked_in_table_retires_it(self):
        from cortados_router import core as real_core
        from cortados_router.provider import routing
        self.assertTrue(routing.api_pacing_on(real_core))

        class Broken:
            def api_pacing_cfg(self):
                raise ValueError("unreadable")
        with self.assertLogs(routing.logger, "WARNING"):
            self.assertFalse(routing.api_pacing_on(Broken()))


if __name__ == "__main__":
    unittest.main()
