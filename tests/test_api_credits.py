"""Unit tests for the Anthropic API-credit lane meter (cortados_router/api_credits.py). Stdlib only; every test
uses an explicit temp meter path so the real ~/.cortados state is never touched."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import api_credits as ac

T = 1793000000.0          # a fixed 2026 timestamp
T_NEXT_MONTH = T + 45 * 86400  # 45 days later -> a different calendar month, whatever the local tz


class TestPricing(unittest.TestCase):
    def test_price_for_exact_prefix_and_default(self):
        self.assertEqual(ac.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(ac.price_for("CLAUDE-OPUS-5-5")["input"], 4.0)          # case-insensitive
        self.assertEqual(ac.price_for("claude-opus-5-5-20260401")["output"], 20.0)  # dated suffix -> base
        self.assertEqual(ac.price_for("gpt-6-astra"), ac.DEFAULT_PRICE)          # unknown -> most expensive default

    def test_cost_usd_math(self):
        self.assertEqual(ac.cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000), 24.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=1_000_000), 0.10)
        # unknown model priced as the most expensive tier (never under-counts toward the cap)
        self.assertEqual(ac.cost_usd("some-unknown-model", input_tokens=1_000_000), 10.0)

    def test_cost_usd_ignores_bad_counts(self):
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=-5), 0.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=float("inf")), 0.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=float("nan")), 0.0)

    def test_cost_from_usage_reads_all_fields(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
        # opus-5-5: 1M input @4 + 1M cache_read @0.20 = 4.20
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", usage), 4.20, places=6)
        self.assertEqual(ac.cost_from_usage("claude-opus-5-5", None), 0.0)


class TestMeter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.meter = Path(self.dir) / "api-credit-meter.json"

    def test_record_accumulates_within_a_month(self):
        self.assertEqual(ac.spent(T, self.meter), 0.0)                       # no file yet -> 0
        ac.record("claude-opus-5-5", {"input_tokens": 1_000_000}, T, self.meter)  # $4
        ac.record("claude-haiku-5-5", {"output_tokens": 1_000_000}, T, self.meter)  # +$0.50
        self.assertAlmostEqual(ac.spent(T, self.meter), 4.50, places=6)

    def test_month_rollover_resets(self):
        ac.record("claude-opus-5-5", {"input_tokens": 1_000_000}, T, self.meter)
        self.assertAlmostEqual(ac.spent(T, self.meter), 4.0, places=6)
        # reading in a later month sees 0 (the stored file is from the prior month)
        self.assertEqual(ac.spent(T_NEXT_MONTH, self.meter), 0.0)
        # recording in the new month resets the meter to just the new cost
        ac.record("claude-haiku-5-5", {"output_tokens": 1_000_000}, T_NEXT_MONTH, self.meter)
        self.assertAlmostEqual(ac.spent(T_NEXT_MONTH, self.meter), 0.50, places=6)
        self.assertEqual(ac.spent(T, self.meter), 0.0)                       # the old month is gone

    def test_over_budget_and_remaining(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "200"}):
            self.assertFalse(ac.over_budget(T, self.meter))
            self.assertAlmostEqual(ac.remaining(T, self.meter), 200.0, places=6)
            # 10M output on opus-5-5 = $200 -> at the cap
            ac.record("claude-opus-5-5", {"output_tokens": 10_000_000}, T, self.meter)
            self.assertTrue(ac.over_budget(T, self.meter))
            self.assertEqual(ac.remaining(T, self.meter), 0.0)

    def test_window_numbers(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "200"}):
            ac.record("claude-opus-5-5", {"output_tokens": 1_000_000}, T, self.meter)  # $20 of $200 = 10%
            used_pct, reset_at, window_s = ac.window_numbers(T, self.meter)
            self.assertAlmostEqual(used_pct, 10.0, places=4)
            self.assertGreater(reset_at, T)                                  # resets in the future
            self.assertTrue(28 * 86400 <= window_s <= 31 * 86400)            # a calendar month

    def test_window_used_pct_caps_at_100(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10"}):
            ac.record("claude-opus-5-5", {"output_tokens": 10_000_000}, T, self.meter)  # $200 >> $10 budget
            used_pct, _, _ = ac.window_numbers(T, self.meter)
            self.assertEqual(used_pct, 100.0)


class TestBudgetEnv(unittest.TestCase):
    def test_budget_default_and_override_and_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_API_CREDIT_BUDGET_USD", None)
            self.assertEqual(ac.budget_usd(), 200.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "500"}):
            self.assertEqual(ac.budget_usd(), 500.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "nonsense"}):
            self.assertEqual(ac.budget_usd(), 200.0)                         # bad value -> default, never 0
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "-5"}):
            self.assertEqual(ac.budget_usd(), 200.0)                         # non-positive -> default


if __name__ == "__main__":
    unittest.main()
