"""Unit tests for the admission-controlled API-credit meter (cortados_router/api_credits.py). Stdlib only; every
test uses an explicit temp meter path so the real ~/.cortados state is never touched."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import api_credits as ac

T = 1793000000.0


class TestPricing(unittest.TestCase):
    def test_price_for(self):
        self.assertEqual(ac.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(ac.price_for("claude-opus-5-5-20260401")["output"], 20.0)   # dated -> base
        self.assertEqual(ac.price_for("unknown-model"), ac.DEFAULT_PRICE)            # unknown -> most expensive

    def test_pricing_is_conservative(self):
        # cache WRITE is priced at the 1-hour rate (input x2), never the cheaper 5-minute rate
        self.assertEqual(ac.price_for("claude-opus-5-5")["cache_write"], 8.0)        # 4 x 2
        # Haiku uses its >100k tier so the guard never under-counts
        self.assertEqual(ac.price_for("claude-haiku-5-5")["input"], 0.50)

    def test_cost_from_usage_and_bad_counts(self):
        self.assertAlmostEqual(ac.cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000), 24.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=-5), 0.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=float("inf")), 0.0)
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 1_000_000}), 4.0)

    def test_estimate_max_cost_is_an_upper_bound(self):
        body = {"model": "api/claude-opus-5-5", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}
        est = ac.estimate_max_cost(body, "claude-opus-5-5")
        self.assertGreater(est, 0)
        # output ceiling alone (1000 tok @ $20/M) is already in the estimate
        self.assertGreaterEqual(est, 1000 * 20.0 / 1_000_000)

    def test_client_tool_named_like_a_server_tool_gets_no_allowance(self):
        # a CLIENT tool merely NAMED "web_search" must not receive the server web-search reservation (classify by type)
        self.assertEqual(ac._server_tool_allowance([{"name": "web_search", "input_schema": {"type": "object"}}]), 0.0)
        # the genuine server tool, identified by its `type`, still does
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305", "max_uses": 5}]), 0.05, places=4)

    def test_server_tool_charges_are_counted(self):
        # a completed turn that ran 10 web searches is billed $0.01 each on top of tokens
        usage = {"input_tokens": 1_000_000, "server_tool_use": {"web_search_requests": 10}}
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", usage), 4.0 + 0.10, places=4)
        self.assertEqual(ac.server_tool_cost({"server_tool_use": {"web_search_requests": 3}}), 0.03)
        self.assertEqual(ac.server_tool_cost({}), 0.0)

    def test_estimate_reserves_for_declared_web_search(self):
        # the per-tool allowance is exactly max_uses x $0.01; no cap -> a conservative default count
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305", "name": "web_search", "max_uses": 7}]), 0.07, places=4)
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305"}]),
                               ac.DEFAULT_SERVER_TOOL_USES * 0.01, places=4)
        self.assertEqual(ac._server_tool_allowance([{"type": "bash_20250124"}]), 0.0)   # not a billed server tool
        # and the allowance is actually folded into the reservation (tool body also lifts the token estimate)
        plain = {"model": "api/claude-opus-5-5", "max_tokens": 10, "messages": []}
        withtool = dict(plain, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 7}])
        self.assertGreaterEqual(ac.estimate_max_cost(withtool, "claude-opus-5-5"),
                                ac.estimate_max_cost(plain, "claude-opus-5-5") + 0.07)


class TestServerToolAdmission(unittest.TestCase):
    def test_unbounded_server_tools_flags_only_code_execution(self):
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]}),
                         ["code_execution_20250522"])
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": "web_search_20250305"}, {"type": "web_fetch_20250910"}]}), [])
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"name": "my_tool", "input_schema": {}}]}), [])  # a client tool
        self.assertEqual(ac.unbounded_server_tools({}), [])

    def test_enforce_server_tool_caps_injects_and_matches_the_reserve(self):
        body = {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}
        out = ac.enforce_server_tool_caps(body)
        self.assertEqual(out["tools"][0]["max_uses"], ac.DEFAULT_SERVER_TOOL_USES)
        self.assertIsNot(out, body)                               # copied; the original is not mutated
        self.assertNotIn("max_uses", body["tools"][0])
        # the enforced cap equals what the meter reserves for that tool, so admission is a real bound
        self.assertAlmostEqual(ac._server_tool_allowance(out["tools"]), ac.DEFAULT_SERVER_TOOL_USES * 0.01, places=4)
        declared = {"tools": [{"type": "web_search_20250305", "max_uses": 3}]}
        self.assertIs(ac.enforce_server_tool_caps(declared), declared)   # already capped -> unchanged


class TestPeriod(unittest.TestCase):
    def test_reset_day_shifts_the_period(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_RESET_DAY": "15"}):
            start, nxt = ac.period_bounds(T)
            self.assertTrue(nxt > start)
            self.assertTrue(28 <= round((nxt - start) / 86400) <= 31)   # a calendar month (DST may add an hour)

    def test_reset_day_31_clamps_to_short_months(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_RESET_DAY": "31"}):
            self.assertEqual(ac.reset_day(), 31)
            # Feb has no 31st, so the effective boundary is its last day; no crash, still a ~month window.
            feb = __import__("datetime").datetime(2026, 2, 10).astimezone().timestamp()
            start, nxt = ac.period_bounds(feb)
            self.assertTrue(28 <= round((nxt - start) / 86400) <= 31)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        self.env = mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_reserve_respects_budget_and_settle_reconciles(self):
        r1 = ac.reserve(6.0, T, self.meter)
        self.assertIsNotNone(r1)
        self.assertEqual(ac.committed(T, self.meter), 6.0)
        self.assertIsNone(ac.reserve(6.0, T, self.meter))            # would exceed $10 -> refused
        ac.settle(r1, 4.0, T, self.meter)                            # actual < reserved
        self.assertEqual(ac.spent(T, self.meter), 4.0)
        self.assertEqual(ac.committed(T, self.meter), 4.0)
        self.assertFalse(ac.over_budget(T, self.meter))

    def test_settle_records_the_actual_even_above_the_reservation(self):
        r = ac.reserve(3.0, T, self.meter)
        ac.settle(r, 5.0, T, self.meter)                            # actual exceeded the $3 reservation -> recorded in full, not clamped
        self.assertEqual(ac.spent(T, self.meter), 5.0)

    def test_fail_closed_on_a_non_finite_balance(self):
        self.meter.parent.mkdir(parents=True, exist_ok=True)
        self.meter.write_text('{"periods": {"2026-10": {"spent_usd": Infinity, "reserved": {}}}}')
        with self.assertRaises(ac.LedgerError):                     # a non-finite balance must not pass validation
            ac.reserve(1.0, T, self.meter)
        self.assertTrue(ac.over_budget(T, self.meter))             # read-only view of invalid state -> fully committed

    def test_over_budget_counts_outstanding_reservations(self):
        ac.reserve(10.0, T, self.meter)
        self.assertTrue(ac.over_budget(T, self.meter))              # fully committed even before settle

    def test_settle_is_idempotent(self):
        r = ac.reserve(6.0, T, self.meter)
        ac.settle(r, 4.0, T, self.meter)
        ac.settle(r, 4.0, T, self.meter)                            # a retried/duplicated settle must not double-charge
        self.assertEqual(ac.spent(T, self.meter), 4.0)

    def test_pruning_keeps_a_period_with_an_outstanding_reservation(self):
        old = ac.reserve(6.0, T, self.meter)                        # period A, left outstanding
        ac.reserve(1.0, T + 40 * 86400, self.meter)                # period B
        ac.reserve(1.0, T + 75 * 86400, self.meter)                # period C (A is now older than the 2 most recent)
        ac.settle(old, 4.0, T, self.meter)                         # A was not pruned (it had a reservation) -> charge lands
        self.assertEqual(ac.spent(T, self.meter), 4.0)

    def test_late_settle_lands_in_its_own_period(self):
        old = ac.reserve(5.0, T, self.meter)                        # period A
        later = T + 40 * 86400                                      # a later billing period
        ac.reserve(4.0, later, self.meter)
        ac.settle(old, 5.0, later, self.meter)                      # late settle of the old reservation
        self.assertEqual(ac.spent(later, self.meter), 0.0)         # the new period is untouched
        self.assertEqual(ac.spent(T, self.meter), 5.0)            # the old period got the charge

    def test_fail_closed_on_a_corrupt_ledger(self):
        self.meter.parent.mkdir(parents=True, exist_ok=True)
        self.meter.write_text("{ not json")
        with self.assertRaises(ac.LedgerError):
            ac.reserve(1.0, T, self.meter)
        # a read-only view of a corrupt ledger treats the period as fully committed (pacing fails safe)
        self.assertTrue(ac.over_budget(T, self.meter))

    def test_window_numbers_tracks_committed(self):
        ac.reserve(9.0, T, self.meter)
        used_pct, reset_at, window_s = ac.window_numbers(T, self.meter)
        self.assertEqual(used_pct, 90.0)
        self.assertGreater(reset_at, T)


class TestBudgetEnv(unittest.TestCase):
    def test_budget_default_override_and_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_API_CREDIT_BUDGET_USD", None)
            self.assertEqual(ac.budget_usd(), 200.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "nonsense"}):
            self.assertEqual(ac.budget_usd(), 200.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "400"}):
            self.assertEqual(ac.budget_usd(), 400.0)


if __name__ == "__main__":
    unittest.main()
