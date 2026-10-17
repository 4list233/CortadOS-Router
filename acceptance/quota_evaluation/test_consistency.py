# PURPOSE: Pin one quota observation per priced candidate per resolve calculation.
# CONTRACT: RT-QUOTA-EVALUATION-004; preserve pricing fallback and admission.
# DEPENDS: Actual standalone/frozen live core, deterministic observation sequences.
# CEILING: No teacher, GPU, network, promotion or live writes.
import os
from pathlib import Path
import unittest

from .support import ROOT, candidate, configure, resolve


class ConsistencyChecks:
    def test_positive_then_none_uses_first_observation_without_crash(self):
        result, calls = resolve(self, {'codex': [1000.0, None], 'claude': [4000.0, None]})
        self.assertEqual(calls, ['codex', 'claude'], 'quota-cost must read each priced candidate exactly once')
        self.assertEqual(result['chosen']['lane'], 'claude')
        self.assertEqual(result['chosen']['burn'], 1000.0)
        self.assertEqual(result['chosen']['weight'], 50.0)
        self.assertEqual(result['runner_up']['burn'], 2000.0)
        self.assertEqual(result['runner_up']['weight'], 100.0)

    def test_positive_then_changed_value_keeps_first_observation(self):
        result, calls = resolve(self, {'codex': [1000.0, 10000.0], 'claude': [4000.0, 400.0]})
        self.assertEqual(result['chosen']['lane'], 'claude', 'second read must not invert quota-cost ranking')
        self.assertEqual(result['chosen']['burn'], 1000.0)
        self.assertEqual(calls, ['codex', 'claude'])

    def test_unknown_estimate_retains_price_fallback_without_quota_evidence(self):
        result, calls = resolve(self, {'codex': [None, 1000.0], 'claude': [None, 1000.0]})
        self.assertEqual(calls, ['codex', 'claude'])
        self.assertEqual(result['chosen']['lane'], 'codex')
        self.assertIsNone(result['chosen']['burn'])
        self.assertEqual(result['chosen']['weight'], 50.0)
        self.assertIsNone(result['runner_up']['burn'])
        self.assertEqual(result['runner_up']['weight'], 100.0)
        self.assertNotIn('tokens_left', result['chosen'])
        self.assertEqual(result['chosen_window'], {})

    def test_zero_estimate_retains_existing_no_estimate_fallback(self):
        result, calls = resolve(self, {'codex': [0.0, 1000.0], 'claude': [0.0, 1000.0]})
        self.assertEqual(calls, ['codex', 'claude'])
        self.assertEqual(result['chosen']['lane'], 'codex')
        self.assertIsNone(result['chosen']['burn'])
        self.assertEqual(result['chosen']['weight'], 50.0)

    def test_unpriced_candidate_does_not_read_quota_estimate(self):
        self.candidates = [candidate('local', None)]
        result, calls = resolve(self, {})
        self.assertEqual(calls, [])
        self.assertIsNone(result['chosen']['burn'])
        self.assertEqual(result['chosen']['weight'], 0.0)

    def test_next_calculation_observes_new_quota_estimate(self):
        first, first_calls = resolve(self, {'codex': [1000.0, 1000.0], 'claude': [4000.0, 4000.0]})
        second, second_calls = resolve(self, {'codex': [10000.0, 10000.0], 'claude': [400.0, 400.0]})
        self.assertEqual(first['chosen']['lane'], 'claude')
        self.assertEqual(second['chosen']['lane'], 'codex')
        self.assertEqual(first_calls, ['codex', 'claude'])
        self.assertEqual(second_calls, ['codex', 'claude'])

    def test_spent_candidate_remains_excluded_with_unknown_estimate(self):
        self.health = {'codex': {'ok': False, 'scarcity': 1.0, 'why': 'fixture spent',
                                'tightest': {}, 'expiring_pct': 0}}
        result, calls = resolve(self, {'codex': [None], 'claude': [None]})
        self.assertEqual(calls, ['codex', 'claude'])
        self.assertEqual(result['chosen']['lane'], 'claude')
        self.assertTrue(any(row['why'] == 'fixture spent' for row in result['skipped']))


class StandaloneLegacy(ConsistencyChecks, unittest.TestCase):
    def setUp(self):
        configure(self, ROOT / 'cortados_router/core.py', False)


class StandaloneResources(ConsistencyChecks, unittest.TestCase):
    def setUp(self):
        configure(self, ROOT / 'cortados_router/core.py', True)


class FrozenLive(ConsistencyChecks, unittest.TestCase):
    def setUp(self):
        path = os.environ.get('CORTADOS_QUOTA_CORE')
        self.assertTrue(path, 'BLOCKED: CORTADOS_QUOTA_CORE must name a frozen live-core candidate')
        path = Path(path).resolve()
        self.assertTrue(path.is_file(), 'BLOCKED: frozen live-core source is missing')
        self.assertNotEqual(path, (ROOT / 'cortados_router/core.py').resolve(),
                            'standalone source cannot masquerade as copied live-core proof')
        configure(self, path, False)
