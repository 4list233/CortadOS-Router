# PURPOSE: Pin exact-route filtering and quota refresh independent of scorer caching.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ03/04; aliases precede resource eligibility.
# DEPENDS: Full production execution_routing.select and injectable quota_config owner seam.
# CEILING: Scorer, credentials and resolver are inert; no model or disk quota reads.
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from .support import PolicyCase, scope


class Selection(PolicyCase):
    def setUp(self):
        super().setUp()
        from cortados_router import execution_routing
        self.selector = execution_routing
        self.selector._scores.clear()
        self.native = {'lane': 'codex', 'provider': 'openai-codex', 'model': 'gpt-6.1-sol'}
        self.choice = dict(self.native, provider='codex-cli')
        self.timeline = []
        self.allowed = (('codex-cli', 'gpt-6.1-sol'),)
        self.core = NS(S1_CURRENT=Path(self.temp.name) / 'absent-current',
            table=lambda: {'candidates': {'R1': [deepcopy(self.native)]}},
            model_scores=lambda: {'models': []}, log_route=Mock(), lane_ceiling=lambda *a: 100000,
            _context_cfg=lambda: {}, candidate_health=lambda *a: {'ok': True}, lane_health=lambda: {})
        self.core.decide = Mock(side_effect=self.score)
        self.core.resolve = Mock(side_effect=self.resolve)
        self.resources_mock = self.enterContext(patch.object(self.policy, 'current_resources', side_effect=self.resources_for))
        self.preflight = self.enterContext(patch.object(self.policy, 'source_preflight', return_value={
            'quota_stage': 'source_preflight', 'auth_scope': scope(), 'quota': {
                'status': 'available', 'kind': 'subscription', 'lane': 'codex', 'pool': 'all',
                'observed_at': 2000000000, 'age_s': 0, 'source': 'exam', 'reason': 'known'}}))

    def score(self, *args, **kwargs):
        self.timeline.append('score')
        return {'degraded': False, 'answers': {}, 'engine': 'exam', 'engine_ms': 0}

    def resources_for(self, candidates, *, config):
        self.timeline.append('resources')
        self.assertIn(('codex-cli', 'gpt-6.1-sol'), [(c['provider'], c['model']) for c in candidates],
                      'existing CLI alias must exist before exact-route quota filtering')
        self.assertEqual(config, self.config)
        return NS(allowed_routes=self.allowed, health=lambda: {'codex': {'ok': True}},
                  tokens_per_pct=lambda c: None, tokens_left=lambda c: None)

    def resolve(self, *args, **kwargs):
        self.timeline.append('resolve')
        self.assertEqual(kwargs['allowed_routes'], self.allowed)
        self.assertIsNotNone(kwargs.get('resources'), 'resolver must consume the same frozen resource snapshot')
        return {'chosen': deepcopy(self.choice), 'band': 'R1', 'hard_rules': [], 'skipped': []}

    def select(self):
        return self.selector.select(self.core, prompt='exam request', session_id='same', turn_id='same',
            quota_config=self.config, credential_resolver=lambda **kwargs: {},
            input_fit={'assembled': True, 'known': True, 'input_tokens': 10})

    def test_cli_alias_survives_filter_and_native_oauth_does_not(self):
        result = self.select()
        self.assertEqual((result['provider'], result['model']), self.allowed[0])
        self.assertEqual(result['quota_stage'], 'source_preflight')
        self.assertLess(self.timeline.index('resources'), self.timeline.index('score'))
        self.core.resolve.assert_called_once()

    def test_cached_scorer_answer_does_not_cache_available_quota(self):
        self.select()
        self.allowed = ()
        self.core.resolve.reset_mock()
        result = self.select()
        self.assertIn('error', result)
        self.core.resolve.assert_not_called()
        self.core.decide.assert_called_once()
        self.assertGreaterEqual(self.resources_mock.call_count, 2)
