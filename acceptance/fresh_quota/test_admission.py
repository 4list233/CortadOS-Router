# PURPOSE: Pin temporal, identity, raw-window and no-estimate admission rules.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ01/02/03.
# DEPENDS: Public check/resources APIs with isolated observations and clocks.
# CEILING: No provider probes, training, or live state.
from copy import deepcopy
import math
from unittest.mock import patch

from .support import EPOCH, MONOTONIC, PolicyCase, candidate, observation, scope, unavailable


class Admission(PolicyCase):
    def test_exact_thirty_seconds_is_available_without_refresh(self):
        value = self.check([observation(age=30)])
        self.assertEqual(value['status'], 'available')
        self.assertEqual(value['age_s'], 30)
        self.collect.assert_not_called()

    def test_over_thirty_seconds_cannot_authorize_after_failed_refresh(self):
        value = self.check([observation(age=30.001)])
        self.assertEqual(value['status'], 'unknown')
        self.collect.assert_called_once()

    def test_configured_lower_age_limit_remains_effective(self):
        self.config['limits']['metadata_max_age_s'] = 1
        self.assertEqual(self.check([observation(age=1.001)])['status'], 'unknown')
        self.collect.assert_called_once()

    def test_configured_larger_age_cannot_relax_thirty_seconds(self):
        self.config['limits']['metadata_max_age_s'] = 3600
        self.assertEqual(self.check([observation(age=31)])['status'], 'unknown')
        self.collect.assert_called_once()

    def test_expired_observation_can_be_replaced_by_actual_fresh_success(self):
        self.collect.return_value = (observation(),)
        self.assertEqual(self.check([observation(age=31)])['status'], 'available')
        self.collect.assert_called_once()

    def test_absent_observation_refresh_failure_is_unknown(self):
        self.assertEqual(self.check([])['status'], 'unknown')
        self.collect.assert_called_once()

    def test_future_epoch_observation_is_unknown(self):
        row = observation()
        row['observed_at'] = EPOCH + 1
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_future_monotonic_observation_is_unknown(self):
        row = observation()
        row['observed_monotonic'] = MONOTONIC + 1
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_old_monotonic_record_cannot_hide_behind_fresh_epoch(self):
        row = observation()
        row['observed_monotonic'] = MONOTONIC - 31
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_fresh_envelope_cannot_hide_expired_window(self):
        row = observation()
        row['record']['windows'][0]['reset_epoch'] = EPOCH - 1
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_reset_beyond_original_window_is_unknown(self):
        row = observation(age=10)
        row['record']['windows'][0]['reset_epoch'] = EPOCH + 604800
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_missing_raw_reset_is_unknown_despite_display_string(self):
        row = observation()
        row['record']['windows'][0]['reset_epoch'] = None
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_missing_subscription_windows_is_unknown(self):
        row = observation()
        row['record']['windows'] = []
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_invalid_used_percentage_is_unknown(self):
        for value in (-1, 101, math.inf, math.nan, True):
            with self.subTest(used=value):
                row = observation()
                row['record']['windows'][0]['used_pct'] = value
                self.assertEqual(self.check([row])['status'], 'unknown')

    def test_invalid_duration_is_unknown(self):
        for value in (0, -1, math.inf, math.nan, True):
            with self.subTest(duration=value):
                row = observation()
                row['record']['windows'][0]['window_s'] = value
                self.assertEqual(self.check([row])['status'], 'unknown')

    def test_existing_spent_threshold_is_refusal(self):
        self.assertEqual(self.check([observation(used=99)])['status'], 'spent')

    def test_source_failure_cannot_authorize_even_with_old_record(self):
        row = observation()
        row['error'] = unavailable()['error']
        self.assertEqual(self.check([row])['status'], 'unknown')

    def test_familiar_model_does_not_infer_unknown_provider(self):
        self.assertEqual(self.check([observation()], provider='paid-api')['status'], 'unknown')
        self.collect.assert_not_called()

    def test_unrelated_pool_does_not_authorize_required_model_pool(self):
        row = observation('antigravity', pool='claude-gpt')
        value = self.check([row], provider='agy-cli', model='gemini-3-pro')
        self.assertEqual(value['status'], 'unknown')

    def test_observed_required_pool_authorizes_exact_provider(self):
        row = observation('antigravity', pool='gemini')
        value = self.check([row], provider='agy-cli', model='gemini-3-pro')
        self.assertEqual(value['status'], 'available')
        self.assertEqual(value['pool'], 'gemini')

    def test_declared_local_resource_has_no_actual_transport_proof(self):
        from cortados_router import core
        row = observation('local')
        row['record'] = {'source': 'exam-local', 'windows': [], 'scarcity': 0.2, 'refusing': False}
        local = candidate('local', 'fixture-local', 'ollama')
        with patch.object(core, 'table', return_value={'candidates': {'R1': [local]}}):
            result = self.check([row], provider='ollama', model='fixture-local')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['kind'], 'local_resource')

    def test_local_name_without_declared_candidate_is_unknown(self):
        from cortados_router import core
        row = observation('local')
        row['record'] = {'source': 'exam-local', 'windows': [], 'scarcity': 0.2}
        with patch.object(core, 'table', return_value={'candidates': {}}):
            result = self.check([row], provider='ollama', model='fixture-local')
        self.assertEqual(result['status'], 'unknown')

    def test_scope_mismatch_cannot_authorize_matching_provider_and_model(self):
        other = scope()
        other['scope_id'] = '0' * 64
        self.assertEqual(self.check([observation()], auth_scope=other)['status'], 'unknown')

    def test_unproven_scope_cannot_authorize_known_quota(self):
        unproven = {'kind': 'unproven', 'lane': 'codex', 'scope_id': None}
        self.assertEqual(self.check([observation()], auth_scope=unproven)['status'], 'unknown')

    def test_native_oauth_provider_cannot_borrow_cli_store_observation(self):
        self.assertEqual(self.check([observation()], provider='openai-codex')['status'], 'unknown')

    def test_duplicate_lane_observations_are_integrity_failure(self):
        self.assertEqual(self.check([observation(), observation()])['status'], 'unknown')

    def test_info_pool_is_not_subscription_admission(self):
        self.assertEqual(self.check([observation(pool='info')])['status'], 'unknown')

    def test_scope_shape_cannot_contain_unrecognized_fields(self):
        proof = dict(scope(), extra='not canonical')
        self.assertEqual(self.check([observation()], auth_scope=proof)['status'], 'unknown')


class ResourceSnapshot(PolicyCase):
    def test_unknown_learned_cost_preserves_known_quota(self):
        from cortados_router import core
        c = candidate()
        resources = self.resources([c], (observation(),), None)
        self.assertTrue(core.candidate_health(c, resources.health())['ok'])
        self.assertIsNone(resources.tokens_per_pct(c))
        self.assertIsNone(resources.tokens_left(c))

    def test_missing_lane_is_explicitly_unavailable(self):
        from cortados_router import core
        c = candidate('claude', 'claude-fable-5', 'claude-cli')
        resources = self.resources([c], (observation(),), None)
        self.assertFalse(core.candidate_health(c, resources.health())['ok'])

    def test_mutating_observation_or_cost_map_cannot_change_snapshot(self):
        c, row = candidate(), observation()
        costs = {'codex:all': {'tokens_per_pct': 2000, 'remaining_tokens': 128000}}
        resources = self.resources([c], (row,), costs)
        health = deepcopy(resources.health())
        row['record']['windows'][0]['used_pct'] = 100
        row['record']['refusing'] = True
        costs['codex:all']['tokens_per_pct'] = 1
        costs['codex:all']['remaining_tokens'] = 0
        self.assertEqual(resources.health(), health)
        self.assertEqual(resources.tokens_per_pct(c), 2000)
        self.assertEqual(resources.tokens_left(c), 128000)

    def test_resources_and_supplied_empty_costs_never_read_live_files(self):
        from cortados_router import core, scarcity
        c, row = candidate(), observation()
        with patch.object(core, '_scarcity_module', return_value=scarcity), \
                patch.object(scarcity, 'current', side_effect=AssertionError('live quota read forbidden')), \
                patch.object(scarcity, 'unit_costs', side_effect=AssertionError('live cost read forbidden')):
            resources = self.resources([c], (row,), {})
            resources.health()
            self.assertIsNone(resources.tokens_per_pct(c))
            self.assertIsNone(resources.tokens_left(c))
            self.assertIsNone(core._pool_tokens_per_pct(c, lanes={}, costs={}))
            self.assertIsNone(core._tokens_left_for(c, lanes={}, costs={}))

    def test_current_resources_reads_costs_once_and_collects_requested_lanes(self):
        from cortados_router import scarcity
        self.collect.return_value = (observation('claude'), observation())
        candidates = [candidate(), candidate('claude', 'claude-fable-5', 'claude-cli'), candidate()]
        with patch.object(scarcity, 'unit_costs', return_value={}) as costs:
            resources = self.policy.current_resources(candidates, config=self.config)
            resources.health()
            resources.tokens_per_pct(candidates[0])
            resources.tokens_left(candidates[0])
        costs.assert_called_once()
        self.collect.assert_called_once()
        self.assertEqual(set(self.collect.call_args.args[0]), {'codex', 'claude'})

    def test_resources_frozen_instant_checks_both_lanes_independently(self):
        from cortados_router import core
        a, b = candidate(), candidate('claude', 'claude-fable-5', 'claude-cli')
        rows = (observation(age=30), observation('claude', age=30.001))
        resources = self.resources([a, b], rows)
        self.assertTrue(core.candidate_health(a, resources.health())['ok'])
        self.assertFalse(core.candidate_health(b, resources.health())['ok'])

    def test_allowed_routes_excludes_mismatched_scope_before_resolution(self):
        good = candidate()
        bad = candidate(provider='openai-codex')
        resources = self.resources([good, bad], (observation(),))
        self.assertEqual(resources.allowed_routes, (('codex-cli', 'gpt-6.1-sol'),))
