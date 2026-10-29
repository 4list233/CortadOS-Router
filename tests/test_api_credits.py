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


class TestPeriod(unittest.TestCase):
    def test_reset_day_shifts_the_period(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_RESET_DAY": "15"}):
            start, nxt = ac.period_bounds(T)
            self.assertTrue(nxt > start)
            self.assertTrue(28 * 86400 <= (nxt - start) <= 31 * 86400)


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

    def test_settle_is_clamped_to_the_reservation(self):
        r = ac.reserve(3.0, T, self.meter)
        ac.settle(r, 999.0, T, self.meter)                          # actual over-reported -> clamped to 3
        self.assertEqual(ac.spent(T, self.meter), 3.0)

    def test_over_budget_counts_outstanding_reservations(self):
        ac.reserve(10.0, T, self.meter)
        self.assertTrue(ac.over_budget(T, self.meter))              # fully committed even before settle

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
