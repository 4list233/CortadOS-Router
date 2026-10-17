# PURPOSE: Pin the observed Codex status reset and session-label parsing defects.
# CONTRACT: RT-FRESH-QUOTA-005; expected quota values are source observations.
# DEPENDS: Actual scarcity.parse_codex_status and sanitized captured status fragment.
# CEILING: Parser fixtures do not establish provider freshness or execution permission.
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name('fixtures')


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 17, 12, 0, 0, tzinfo=tz)


class CodexStatus(unittest.TestCase):
    source = ROOT / 'cortados_router/scarcity.py'

    def setUp(self):
        self.private = tempfile.TemporaryDirectory(prefix='fresh-quota-parser-')
        self.addCleanup(self.private.cleanup)
        with patch.dict('os.environ', {'CORTADOS_HOME': self.private.name}):
            spec = importlib.util.spec_from_file_location('fresh_quota_scarcity', self.source)
            self.scarcity = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.scarcity)
        self.clock = FrozenDateTime.now().timestamp()

    def parse(self, value):
        with patch.object(self.scarcity, 'datetime', FrozenDateTime), \
                patch.object(self.scarcity.time, 'time', return_value=self.clock):
            return self.scarcity.parse_codex_status(value)

    def test_captured_weekly_reset_preserves_day_month_and_am(self):
        capture = json.loads((FIXTURES / 'codex-status.json').read_text())
        rows = self.parse(capture['quota_fragments'][0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['resets_at'], '2026-10-22 03:22',
                         'Codex reset must retain its reported calendar day after AM/PM')
        self.assertEqual(rows[0]['headroom_pct'], 64.0)
        self.assertEqual(rows[0]['used_pct'], 36.0)
        self.assertEqual(rows[0]['pool'], 'all')

    def test_session_identifier_does_not_become_weekly_model_pool(self):
        capture = json.loads((FIXTURES / 'codex-status.json').read_text())
        # Synthetic session header surrounds the real captured quota fragment.
        text = 'Session: 11111111-1111-4111-8111-111111111111\n' + capture['quota_fragments'][0]
        rows = self.parse(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['window'], 'week', 'session identity is not a metered model')
        self.assertEqual(rows[0]['pool'], 'all')

    def assert_clock(self, clock, expected):
        rows = self.parse(f'Weekly limit: [bar] 64% left (resets {clock} on 22 Oct)')
        self.assertEqual(rows[0]['resets_at'], '2026-10-22 ' + expected)

    def test_midnight_reset_is_hour_zero(self):
        self.assert_clock('12:05 AM', '00:05')

    def test_noon_reset_is_hour_twelve(self):
        self.assert_clock('12:05 PM', '12:05')

    def test_afternoon_reset_uses_pm(self):
        self.assert_clock('3:22 PM', '15:22')

    def test_existing_24_hour_and_named_model_limits_remain_distinct(self):
        rows = self.parse('Weekly limit: [bar] 64% left (resets 15:22 on 22 Oct)\n'
                          'gpt-6-astra Weekly limit: [bar] 20% left (resets 16:00 on 22 Oct)')
        self.assertEqual([(row['window'], row['pool']) for row in rows],
                         [('week', 'all'), ('week (gpt-6-astra)', 'gpt-6-astra')])
        self.assertEqual(rows[0]['resets_at'], '2026-10-22 15:22')
        self.assertEqual(rows[1]['resets_at'], '2026-10-22 16:00')

    def test_absent_reset_is_not_fabricated(self):
        rows = self.parse('Weekly limit: [bar] 64% left')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['resets_at'])
        self.assertIsNone(rows[0]['resets_in_h'])

    def test_malformed_calendar_or_clock_keeps_reset_unknown(self):
        for when in ('3:99 AM on 22 Oct', '3:22 AM on 31 Feb', '13:22 AM on 22 Oct',
                     '00:22 PM on 22 Oct', '24:22 on 22 Oct', '3:22 AM on 22 Xxx'):
            with self.subTest(when=when):
                rows = self.parse(f'Weekly limit: [bar] 64% left (resets {when})')
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]['reset_epoch'])
                self.assertIsNone(rows[0]['resets_at'])


class FrozenLiveCodexStatus(CodexStatus):
    source = Path(os.environ.get('CORTADOS_FRESH_QUOTA_STAGE',
                  '/Users/velox/.cortados-builds/fresh-quota-activation')) / 'plugins/cortados-router/scarcity.py'
