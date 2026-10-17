# PURPOSE: Preserve actual native weekly geometry and separate status from admission.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ02/08.
# DEPENDS: Native project/observe public seams; captured sanitized API response.
# CEILING: No native binary, credential refresh or model call.
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from cortados_router import scarcity_codex_native
from integrations.mcp.schemas import WorkerError


class NativeProjection(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((Path(__file__).with_name('fixtures') / 'native-quota.json').read_text())
        self.now = self.raw['sampled_at_ms'] / 1000

    def test_weekly_primary_is_one_weekly_window_without_invented_five_hour(self):
        record = scarcity_codex_native.project(self.raw, now=self.now)
        self.assertEqual(len(record['windows']), 1)
        window = record['windows'][0]
        self.assertEqual(window['window'], 'week')
        self.assertEqual(window['window_s'], 604800)
        self.assertEqual(window['reset_epoch'], 1792653737)
        self.assertEqual(window['used_pct'], 36)
        self.assertEqual(window['headroom_pct'], 64)
        self.assertEqual(window['pool'], 'all')

    def test_default_admission_keeps_rejecting_spent(self):
        self.raw['ordinaryUsageAllowed'] = False
        with self.assertRaises(WorkerError) as raised:
            scarcity_codex_native.project(self.raw, now=self.now)
        self.assertEqual(raised.exception.code, 'quota_spent')

    def test_status_can_display_valid_spent_windows_without_authorizing(self):
        self.raw['ordinaryUsageAllowed'] = False
        record = scarcity_codex_native.project(self.raw, now=self.now, admission=False)
        self.assertTrue(record['refusing'])
        self.assertEqual(record['windows'][0]['reset_epoch'], 1792653737)

    def test_status_does_not_invent_windows_for_explicit_spent(self):
        self.raw['ordinaryUsageAllowed'] = False
        self.raw['rateLimitsByLimitId']['codex']['primary'] = None
        self.raw['rateLimits']['primary'] = None
        record = scarcity_codex_native.project(self.raw, now=self.now, admission=False)
        self.assertTrue(record['refusing'])
        self.assertEqual(record['windows'], [])

    def test_invalid_window_stays_unknown_in_status_mode(self):
        self.raw['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = None
        with self.assertRaises(WorkerError) as raised:
            scarcity_codex_native.project(self.raw, now=self.now, admission=False)
        self.assertEqual(raised.exception.code, 'quota_unknown')

    def test_window_view_retains_fractional_raw_duration_and_unknown_reset(self):
        from cortados_router import scarcity
        row = scarcity.window_view('subscription', 12, None, 3600.5, now=self.now)
        self.assertEqual(row['window_s'], 3600.5)
        self.assertIsNone(row['reset_epoch'])
        self.assertIsNone(row['resets_at'])


class NativeObservation(unittest.TestCase):
    def setUp(self):
        from tests import test_mcp_route_task as existing
        existing.RouteContract.setUp(self)
        from integrations.mcp.native_metadata import NativeMetadata
        self.reader = NativeMetadata(self.config)
        self.reader.version = '0.163.0'
        self.sample = existing.snapshot()

    def test_observe_is_uncached_and_does_not_admit_spent(self):
        self.sample['rate_limits']['ordinaryUsageAllowed'] = False
        with patch.object(self.reader, '_collect', return_value=deepcopy(self.sample)) as collect:
            first = self.reader.observe()
            second = self.reader.observe()
            self.assertFalse(first['rate_limits']['ordinaryUsageAllowed'])
            self.assertFalse(second['rate_limits']['ordinaryUsageAllowed'])
            self.assertEqual(collect.call_count, 2)
            self.assertIsNone(self.reader._cached)
            with self.assertRaises(WorkerError) as failure:
                self.reader.snapshot()
            self.assertEqual(failure.exception.code, 'quota_spent')
            self.assertIsNone(self.reader._cached)

    def test_snapshot_cache_cannot_exceed_thirty_seconds(self):
        self.config['limits']['metadata_max_age_s'] = 3600
        self.reader._cached = deepcopy(self.sample)
        self.reader._cached['sampled_monotonic'] -= 31
        with patch.object(self.reader, '_collect', return_value=deepcopy(self.sample)) as collect:
            self.reader.snapshot()
        collect.assert_called_once()

    def test_uncached_native_observation_uses_read_only_protocol(self):
        from integrations.mcp.native_metadata import NativeMetadata
        reader = NativeMetadata(self.config)
        reader.observe()
        calls = [json.loads(line) for line in (self.path / 'fixture-calls.jsonl').read_text().splitlines()]
        rpc = [row for row in calls if row.get('kind') == 'metadata']
        self.assertTrue(rpc)
        methods = {row['method'] for row in rpc}
        self.assertIn('account/rateLimits/read', methods)
        self.assertFalse(methods & {'turn/start', 'account/login/start', 'account/logout', 'account/updated'})
        account = [row for row in rpc if row['method'] == 'account/read']
        self.assertTrue(account)
        self.assertTrue(all(row['params']['refreshToken'] is False for row in account))
