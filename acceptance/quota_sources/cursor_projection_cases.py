# PURPOSE: Independent native Cursor projection cases bound by test_cursor_projection.
# CONTRACT: RT-QUOTA-SOURCES-006 draft; no source profile is enabled by these fixtures.
# DEPENDS: Frozen native response, existing scarcity pool/overflow owner.
# The adapter calls the production parser unchanged; these cases own no validation policy.
from copy import deepcopy
import json
from pathlib import Path

FIXTURES = Path(__file__).with_name('fixtures')


class CursorProjectionCases:
    """Final TestCase supplies project(payload, now) and assert_unknown(payload, now).

    Adapter must call the pinned production parser without changing data or policy.
    Mutations below are explicit synthetic boundary cases, not real captures.
    """

    def cursor_input(self):
        capture = json.loads((FIXTURES / 'cursor-native-rpc-response.json').read_text())
        reference = json.loads((FIXTURES / 'cursor-projection-reference.json').read_text())
        return deepcopy(capture['response']), reference

    def projected(self, payload=None, now=None):
        raw, reference = self.cursor_input()
        return self.project(raw if payload is None else payload,
                            reference['observed_at'] if now is None else now)

    def windows(self, record):
        rows = record['windows']
        self.assertEqual(len(rows), 3, 'Cursor native projection must retain three distinct pools')
        indexed = {row['pool']: row for row in rows}
        self.assertEqual(set(indexed), {'info', 'cursor-models', 'other-models'})
        return indexed

    def test_native_exact_cycle_and_percentage_fields(self):
        _, reference = self.cursor_input()
        windows = self.windows(self.projected())
        for pool, expected in reference['windows_by_pool'].items():
            with self.subTest(pool=pool):
                self.assertEqual(windows[pool]['used_pct'], expected['used_pct'])
                self.assertEqual(windows[pool]['reset_epoch'], 1794349955)
                self.assertEqual(windows[pool]['window_s'], 2678400)
                self.assertIsInstance(windows[pool]['window'], str)
                self.assertTrue(windows[pool]['window'])

    def test_native_epoch_milliseconds_not_local_end_of_day_or_30_4_days(self):
        for row in self.windows(self.projected()).values():
            self.assertEqual(row['reset_epoch'] - row['window_s'], 1791671555)
            self.assertNotEqual(row['window_s'], 30.4 * 86400)
            self.assertEqual(row['reset_epoch'] % 86400, 81155)

    def test_projection_does_not_mutate_native_payload(self):
        raw, reference = self.cursor_input()
        before = deepcopy(raw)
        self.project(raw, reference['observed_at'])
        self.assertEqual(raw, before)

    def test_display_and_money_do_not_override_native_pool_percentages(self):
        raw, _ = self.cursor_input()
        original = self.windows(self.projected(raw))
        raw['displayMessage'] = 'Unlimited usage available'
        raw['autoModelSelectedDisplayMessage'] = 'No usage'
        raw['namedModelSelectedDisplayMessage'] = 'Limit reached'
        for key in ('totalSpend', 'includedSpend', 'bonusSpend', 'limit'):
            raw['planUsage'][key] = 999999999
        altered = self.windows(self.projected(raw))
        for pool in original:
            for key in ('used_pct', 'reset_epoch', 'window_s'):
                self.assertEqual(altered[pool][key], original[pool][key])

    def test_native_exhausted_auto_preserves_existing_other_pool_overflow(self):
        from cortados_router import scarcity
        record = scarcity.finish('cursor', self.projected())
        self.assertFalse(record['pools']['cursor-models']['spent'])
        self.assertFalse(record['pools']['other-models']['spent'])
        self.assertIn('overflow', record['pools']['cursor-models']['note'])

    def test_both_exhausted_pools_remain_spent(self):
        from cortados_router import scarcity
        raw, _ = self.cursor_input()
        raw['planUsage']['apiPercentUsed'] = 100
        record = scarcity.finish('cursor', self.projected(raw))
        self.assertTrue(record['pools']['cursor-models']['spent'])
        self.assertTrue(record['pools']['other-models']['spent'])

    def test_zero_usage_retains_actual_cycle_not_fabricated_reset(self):
        raw, _ = self.cursor_input()
        for key in ('autoPercentUsed', 'apiPercentUsed', 'totalPercentUsed'):
            raw['planUsage'][key] = 0
        for row in self.windows(self.projected(raw)).values():
            self.assertEqual(row['used_pct'], 0)
            self.assertEqual(row['reset_epoch'], 1794349955)
            self.assertEqual(row['window_s'], 2678400)

    def test_missing_cycle_endpoint_refuses(self):
        for key in ('billingCycleStart', 'billingCycleEnd'):
            raw, reference = self.cursor_input()
            del raw[key]
            with self.subTest(key=key):
                self.assert_unknown(raw, reference['observed_at'])

    def test_invalid_cycle_endpoint_refuses(self):
        for key in ('billingCycleStart', 'billingCycleEnd'):
            for value in (None, True, 1791671555000.5, '', 'nan', 'inf', '1e12', '1791671555000.5', float('nan'), float('inf')):
                raw, reference = self.cursor_input()
                raw[key] = value
                with self.subTest(key=key, value=value):
                    self.assert_unknown(raw, reference['observed_at'])

    def test_reversed_or_empty_cycle_refuses(self):
        for end in ('1791671555000', '1791671554999'):
            raw, reference = self.cursor_input()
            raw['billingCycleEnd'] = end
            self.assert_unknown(raw, reference['observed_at'])

    def test_seconds_in_millisecond_fields_refuse(self):
        raw, reference = self.cursor_input()
        raw['billingCycleStart'] = '1791671555'
        raw['billingCycleEnd'] = '1794349955'
        self.assert_unknown(raw, reference['observed_at'])

    def test_observation_outside_native_cycle_refuses(self):
        raw, reference = self.cursor_input()
        for now in (reference['cycle_start_epoch'] - .001,
                    reference['reset_epoch'], reference['reset_epoch'] + .001):
            with self.subTest(now=now):
                self.assert_unknown(raw, now)

    def test_missing_or_invalid_required_pool_usage_refuses(self):
        for key in ('autoPercentUsed', 'apiPercentUsed', 'totalPercentUsed'):
            raw, reference = self.cursor_input()
            del raw['planUsage'][key]
            self.assert_unknown(raw, reference['observed_at'])
            for value in (None, True, '33', -1, 101, float('nan'), float('inf')):
                raw, reference = self.cursor_input()
                raw['planUsage'][key] = value
                with self.subTest(key=key, value=value):
                    self.assert_unknown(raw, reference['observed_at'])

    def test_native_enabled_is_diagnostic_and_never_overrides_allowance(self):
        raw, reference = self.cursor_input()
        original = self.windows(self.projected(raw))
        for value in (False, True, None):
            if value is None:
                raw.pop('enabled', None)
            else:
                raw['enabled'] = value
            with self.subTest(enabled=value):
                self.assertEqual(self.windows(self.projected(raw)), original)

    def test_integral_numeric_epoch_milliseconds_match_native_decimal_strings(self):
        raw, _ = self.cursor_input()
        expected = self.windows(self.projected(raw))
        for numeric in (int, float):
            for key in ('billingCycleStart', 'billingCycleEnd'):
                raw[key] = numeric(raw[key])
            self.assertEqual(self.windows(self.projected(raw)), expected)

    def test_nonfinite_or_non_numeric_observation_time_refuses(self):
        raw, _ = self.cursor_input()
        for now in (None, True, '1792267794.079', float('nan'), float('inf')):
            with self.subTest(now=now):
                self.assert_unknown(raw, now)

    def test_missing_or_non_object_plan_usage_refuses(self):
        for value in (None, [], 1, 'usage'):
            raw, reference = self.cursor_input()
            raw['planUsage'] = value
            self.assert_unknown(raw, reference['observed_at'])
        raw, reference = self.cursor_input()
        del raw['planUsage']
        self.assert_unknown(raw, reference['observed_at'])
