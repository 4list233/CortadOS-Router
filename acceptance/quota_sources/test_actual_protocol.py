# PURPOSE: Exercise real private-context owner, cache and child protocol at an inert native boundary.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS02/04/06; native SourceCapture/read_source are fixture seams.
# CEILING: No real credential, settings, source query, model call or persisted private proof.
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROFILE = 'cursor-2026.10.16-e3cb207-file-v1'
ROOT = Path(__file__).resolve().parents[2]


class ActualSourceProtocol(unittest.TestCase):
    def setUp(self):
        from cortados_router import quota_sources, quota_admission, quota_observations
        self.native = quota_sources
        self.policy = importlib.reload(quota_admission)
        self.collector = importlib.reload(quota_observations)
        self.temp = tempfile.TemporaryDirectory(prefix='quota-actual-exam-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.control_path = self.home / 'control.json'
        self.log = self.home / 'calls.jsonl'
        self.credential = self.home / 'synthetic-credential'
        self.credential.write_text('synthetic-token-A')
        self.environment = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', ''),
                            'QUOTA_EXAM_CONTROL': str(self.control_path), 'QUOTA_EXAM_MARKER': 'original',
                            'SYNTHETIC_SECRET': 'PRIVATE_SENTINEL_NO_PUBLIC_COPY'}
        self.config = {
            'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'actual-exam',
            'state_dir': str(self.home), 'codex_home': str(self.home),
            'codex_bin': str(ROOT / 'acceptance/mcp_worker/fake_codex.py'),
            'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
            'limits': {'max_concurrent_jobs': 2, 'max_timeout_s': 30, 'termination_grace_s': 1,
                       'startup_timeout_s': 3, 'max_handoff_bytes': 65536, 'max_output_bytes': 65536,
                       'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        self.control = {'log': str(self.log)}
        self.write_control()
        self.argv = ['/fixture/cursor-agent', '-p', '--output-format', 'stream-json', '--trust', '--model', 'auto', 'synthetic prompt text']
        self.captures = []
        def capture(provider, *, environment, executable, argv, cwd, config):
            # Synthetic native-boundary fact; production owner must bind and compare it itself.
            self.captures.append(copy.deepcopy((provider, environment, executable, argv, cwd)))
            material = json.dumps([provider, environment, executable, str(cwd),
                                   self.credential.read_text()], sort_keys=True).encode()
            return self.native.SourceCapture(profile=PROFILE, lane='cursor',
                       source_argv=('--model', 'auto'), material=material, files=())
        self.capture = self.enterContext(patch.object(self.native, 'capture_source', side_effect=capture))
        real_popen = subprocess.Popen
        self.processes = []
        self.launches = []
        def launch(argv, *args, **kwargs):
            self.assertEqual(list(argv)[1:], ['-I', '-B', '-m', 'cortados_router.quota_observations', '--observe-actual'])
            self.assertEqual(kwargs.get('env'), self.environment)
            self.assertEqual(Path(kwargs['cwd']).resolve(), self.home)
            self.assertTrue(kwargs.get('start_new_session'))
            self.assertFalse(kwargs.get('shell', False))
            self.launches.append((list(argv), copy.deepcopy(kwargs['env'])))
            fixture = Path(__file__).with_name('fixtures') / 'actual_child.py'
            process = real_popen([sys.executable, '-I', '-B', str(fixture)], *args, **kwargs)
            self.processes.append(process)
            return process
        self.enterContext(patch.object(subprocess, 'Popen', side_effect=launch))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

    def write_control(self):
        self.control_path.write_text(json.dumps(self.control))

    def prepare(self, environment=None):
        return self.policy.prepare_actual('cursor-cli', environment=environment or self.environment,
             executable=self.argv[0], argv=tuple(self.argv), cwd=str(self.home), config=self.config)

    def check(self, actual, provider='cursor-cli'):
        value = self.policy.check_actual(provider, 'auto', actual=actual, config=self.config)
        self.assertEqual(set(value), {'status', 'kind', 'lane', 'pool', 'observed_at', 'age_s', 'source', 'reason'})
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(value))
        self.assertNotIn('synthetic-token', json.dumps(value))
        return value

    def rows(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_prepare_captures_without_permission_or_child_io(self):
        actual = self.prepare()
        self.assertIsNotNone(actual)
        self.assertEqual(self.launches, [])
        self.assertFalse(self.log.exists())

    def test_first_check_proves_without_registry_and_returns_closed_quota(self):
        self.assertEqual(self.check(self.prepare())['status'], 'available')
        self.assertEqual(len(self.launches), 1)
        row = self.rows()[0]
        self.assertEqual(set(row['request']), {'schema', 'nonce', 'profile', 'config', 'executable', 'source_argv', 'cwd'})
        self.assertEqual(row['request']['schema'], 'cortados.quota-source.request.v1')
        self.assertRegex(row['request']['nonce'], r'^[0-9a-f]{64}$')
        self.assertEqual(row['request']['profile'], PROFILE)
        self.assertEqual(row['request']['source_argv'], ['--model', 'auto'])
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(row))
        self.assertNotIn('synthetic prompt', json.dumps(row))
        self.assertNotIn('synthetic-token', json.dumps(row))

    def test_context_redacts_repr_and_refuses_pickle_or_json(self):
        actual = self.prepare()
        for secret in ('PRIVATE_SENTINEL', 'synthetic-token', 'synthetic prompt', str(self.home)):
            self.assertNotIn(secret, repr(actual))
        with self.assertRaises((TypeError, ValueError)):
            pickle.dumps(actual)
        with self.assertRaises(TypeError):
            json.dumps(actual)
        with self.assertRaises((AttributeError, TypeError)):
            actual.provider = 'claude-cli'

    def test_forged_context_and_public_scope_are_never_authority(self):
        for forged in ({'kind': 'native_subscription', 'lane': 'cursor', 'scope_id': 'a' * 64},
                       {'profile': PROFILE}, object(), None):
            self.assertEqual(self.check(forged)['status'], 'unknown')
        self.assertEqual(self.launches, [])

    def test_context_of_another_provider_refuses_before_collection(self):
        self.assertEqual(self.check(self.prepare(), provider='claude-cli')['status'], 'unknown')
        self.assertEqual(self.launches, [])

    def test_legacy_check_cannot_authorize_random_native_public_scope(self):
        scope = {'kind': 'native_subscription', 'lane': 'cursor', 'scope_id': 'a' * 64}
        value = self.policy.check('cursor-cli', 'auto', auth_scope=scope, config=self.config, observations=())
        self.assertEqual(value['status'], 'unknown')
        self.assertEqual(self.launches, [])

    def test_caller_environment_and_argv_mutation_cannot_rebind_captured_transport(self):
        actual = self.prepare()
        original = dict(self.environment)
        self.environment['QUOTA_EXAM_MARKER'] = 'changed'
        self.argv[-1] = 'changed prompt'
        # Launch assertion compares against original transport snapshot, not mutable caller input.
        mutated = self.environment
        self.environment = original
        try:
            self.assertEqual(self.check(actual)['status'], 'available')
            self.assertEqual(self.rows()[0]['marker'], 'original')
            self.assertTrue(all(row[1]['QUOTA_EXAM_MARKER'] == 'original' for row in self.captures))
        finally:
            self.environment = mutated

    def test_fresh_repeated_check_reuses_one_source_observation(self):
        actual = self.prepare()
        self.assertEqual(self.check(actual)['status'], 'available')
        self.assertEqual(self.check(actual)['status'], 'available')
        self.assertEqual(len(self.launches), 1)

    def test_concurrent_same_source_checks_share_one_inflight_observation(self):
        self.control['delay'] = .3
        self.write_control()
        first, second = self.prepare(), self.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.check, (first, second)))
        self.assertEqual([row['status'] for row in results], ['available', 'available'])
        self.assertEqual(len(self.launches), 1)

    def test_source_file_rotation_invalidates_original_and_new_context_gets_own_read(self):
        first = self.prepare()
        self.assertEqual(self.check(first)['status'], 'available')
        self.credential.write_text('synthetic-token-B')
        self.assertEqual(self.check(first)['status'], 'unknown')
        self.assertEqual(self.check(self.prepare())['status'], 'available')
        self.assertEqual(len(self.launches), 2)

    def test_rotation_during_read_refuses_late_success(self):
        self.control['delay'] = .4
        self.write_control()
        actual = self.prepare()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.check, actual)
            deadline = time.monotonic() + 2
            while not self.log.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(self.log.exists(), 'Fixture source never started')
            self.credential.write_text('synthetic-token-B')
            self.assertEqual(pending.result(timeout=4)['status'], 'unknown')

    def test_wrong_nonce_profile_source_extra_field_or_malformed_child_refuses(self):
        for mode in ('nonce', 'profile', 'source', 'extra', 'malformed'):
            with self.subTest(mode=mode):
                self.control['mode'] = mode
                self.write_control()
                self.assertEqual(self.check(self.prepare())['status'], 'unknown')
        self.assertEqual(len(self.launches), 5, 'Invalid child result must never seed cache')

    def test_expired_read_failure_does_not_restore_prior_admission(self):
        self.config['limits']['metadata_max_age_s'] = 1
        actual = self.prepare()
        self.assertEqual(self.check(actual)['status'], 'available')
        time.sleep(1.05)  # Cross configured one-second cache lifetime with scheduling margin.
        self.control['mode'] = 'failure'
        self.write_control()
        self.assertEqual(self.check(actual)['status'], 'unknown')
        self.assertEqual(len(self.launches), 2)

    def test_nonce_replay_after_expiry_refuses(self):
        self.config['limits']['metadata_max_age_s'] = 1
        self.control['mode'] = 'replay'
        self.write_control()
        actual = self.prepare()
        self.assertEqual(self.check(actual)['status'], 'available')
        time.sleep(1.05)
        self.assertEqual(self.check(actual)['status'], 'unknown')
        self.assertNotEqual(self.rows()[0]['request']['nonce'], self.rows()[1]['request']['nonce'])

    def test_process_generation_change_retires_context(self):
        actual = self.prepare()
        original = os.getpid()
        with patch.object(os, 'getpid', return_value=original + 1000000):
            self.assertEqual(self.check(actual)['status'], 'unknown')
        self.assertEqual(self.launches, [])

    def test_public_scope_is_random_sanitized_copy_and_not_permission(self):
        first, second = self.prepare(), self.prepare()
        scope = first.public_scope
        self.assertEqual(set(scope), {'kind', 'lane', 'scope_id'})
        self.assertEqual(scope['kind'], 'native_subscription')
        self.assertEqual(scope['lane'], 'cursor')
        self.assertRegex(scope['scope_id'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(scope, second.public_scope)
        scope['scope_id'] = '0' * 64
        self.assertNotEqual(first.public_scope['scope_id'], scope['scope_id'])
        self.assertEqual(self.launches, [])

    def test_missing_null_nonfinite_or_extra_private_proof_refuses(self):
        for mode in ('proof-missing', 'proof-null', 'proof-nan', 'proof-extra'):
            with self.subTest(mode=mode):
                self.control['mode'] = mode
                self.write_control()
                self.assertEqual(self.check(self.prepare())['status'], 'unknown')
        self.assertEqual(len(self.launches), 4)

    def test_cached_quota_cannot_outlive_native_validity_startup_allowance(self):
        self.control['valid_until_epoch'] = time.time() + 4.0
        self.write_control()
        actual = self.prepare()
        self.assertEqual(self.check(actual)['status'], 'available')
        time.sleep(1.1)  # Cross native proof deadline minus configured3s startup, still well below30s age.
        self.control['mode'] = 'failure'
        self.write_control()
        self.assertEqual(self.check(actual)['status'], 'unknown')

    def test_native_validity_shorter_than_startup_bound_refuses_initial_read(self):
        self.control['valid_until_epoch'] = time.time() + 2
        self.write_control()
        self.assertEqual(self.check(self.prepare())['status'], 'unknown')

    def test_private_proof_never_enters_public_result_or_scope(self):
        actual = self.prepare()
        value = self.check(actual)
        self.assertEqual(value['status'], 'available')
        exposed = json.dumps([value, actual.public_scope])
        self.assertNotIn('valid_until_epoch', exposed)
        self.assertNotIn('material', exposed)
        self.assertNotIn('PRIVATE_SENTINEL', exposed)

    def test_configured_preflight_uses_proven_native_source_but_public_scope_cannot_dispatch(self):
        with patch.object(self.collector, '_environment', return_value=(self.environment, self.argv[0], self.policy._unproven('cursor'))), \
             patch.object(os, 'getcwd', return_value=str(self.home)):
            value = self.policy.source_preflight('cursor-cli', 'auto', config=self.config)
        self.assertEqual(value['quota_stage'], 'source_preflight')
        self.assertEqual(value['quota']['status'], 'available')
        self.assertEqual(value['auth_scope']['kind'], 'native_subscription')
        self.assertEqual(len(self.launches), 1)
        refused = self.policy.check('cursor-cli', 'auto', auth_scope=value['auth_scope'], config=self.config, observations=())
        self.assertEqual(refused['status'], 'unknown')

    def test_selection_snapshot_is_frozen_when_new_source_becomes_spent(self):
        candidates = ({'provider': 'cursor-cli', 'model': 'auto', 'lane': 'cursor'},)
        with patch.object(self.collector, '_environment', return_value=(self.environment, self.argv[0], self.policy._unproven('cursor'))), \
             patch.object(os, 'getcwd', return_value=str(self.home)):
            original = self.policy.current_resources(candidates, config=self.config)
            health = original.health()
            self.credential.write_text('synthetic-token-B')
            self.control['used'] = 100
            self.write_control()
            changed = self.policy.current_resources(candidates, config=self.config)
        self.assertEqual(original.allowed_routes, (('cursor-cli', 'auto'),))
        self.assertEqual(changed.allowed_routes, ())
        self.assertEqual(original.health(), health)
        self.assertEqual(len(self.launches), 2)

    def test_uninitialized_owner_class_instance_is_not_valid_context(self):
        forged = object.__new__(self.policy.ActualSource)
        self.assertEqual(self.check(forged)['status'], 'unknown')
        self.assertEqual(self.launches, [])

    def test_source_capture_failure_does_not_echo_private_material(self):
        with patch.object(self.native, 'capture_source', side_effect=ValueError('PRIVATE_SENTINEL_NO_PUBLIC_COPY')):
            with self.assertRaises(ValueError) as error:
                self.prepare()
        self.assertNotIn('PRIVATE_SENTINEL', str(error.exception))
        self.assertEqual(self.launches, [])

    def test_cursor_status_reuses_actual_collector_with_exact_supplied_environment(self):
        from cortados_router import scarcity
        with patch.object(self.collector, '_environment', side_effect=AssertionError('supplied source must not be replaced')), \
             patch('shutil.which', return_value=self.argv[0]), patch.object(os, 'getcwd', return_value=str(self.home)), \
             patch.object(scarcity, '_cursor', side_effect=AssertionError('status recursion')), \
             patch.object(scarcity, 'sample', side_effect=AssertionError('status cannot write shared history')):
            result = self.collector.status_record('cursor', config=self.config, environment=self.environment)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(result['source'], 'cursor native getCurrentPeriodUsage')
        self.assertEqual({r['pool'] for r in result['windows']}, {'info', 'cursor-models', 'other-models'})
        self.assertEqual(self.rows()[0]['marker'], 'original')
        self.assertNotIn('proof', result)
        self.assertNotIn('valid_until_epoch', json.dumps(result))

    def test_failed_cursor_status_has_fixed_unknown_and_no_tui_fallback(self):
        from cortados_router import scarcity
        from integrations.mcp.schemas import WorkerError
        self.control['mode'] = 'failure'
        self.write_control()
        with patch.object(self.collector, '_environment', return_value=(self.environment, self.argv[0], self.policy._unproven('cursor'))), \
             patch.object(os, 'getcwd', return_value=str(self.home)), \
             patch.object(scarcity, 'tui_capture', side_effect=AssertionError('TUI fallback forbidden')):
            with self.assertRaises(WorkerError) as error:
                self.collector.status_record('cursor', config=self.config)
        self.assertEqual(error.exception.code, 'quota_unknown')
        self.assertEqual(str(error.exception), 'Quota source metadata is unavailable')
