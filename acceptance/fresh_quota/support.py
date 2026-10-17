# PURPOSE: Private deterministic observations for fresh-quota policy exams.
# CONTRACT: RT-FRESH-QUOTA-005; no live provider, learner, or credential access.
# DEPENDS: Existing scarcity shape; normalized worker config contract.
# CEILING: Fixtures supply observations, never implementation policy decisions.
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
EPOCH = 2_000_000_000.0
MONOTONIC = 10_000.0


def scope(lane='codex'):
    return {'kind': 'local_resource' if lane == 'local' else 'cli_store', 'lane': lane,
            'scope_id': hashlib.sha256(('exam-scope:' + lane).encode()).hexdigest()}


def observation(lane='codex', *, age=0, used=36, pool='all', duration=604800):
    from cortados_router import scarcity
    start = EPOCH - age
    reset = start + duration / 2
    window = scarcity.window_view('week', used, reset, duration, now=start)
    window.update(pool=pool, window_s=duration, reset_epoch=reset)
    record = scarcity.finish(lane, {'source': 'exam-source', 'windows': [window]})
    return {'lane': lane, 'auth_scope': scope(lane), 'source': 'exam-source', 'observed_at': start,
            'observed_monotonic': MONOTONIC - age, 'record': record, 'error': None}


def unavailable(lane='codex'):
    return {'lane': lane, 'auth_scope': scope(lane), 'source': 'exam-source', 'observed_at': EPOCH,
            'observed_monotonic': MONOTONIC, 'record': None,
            'error': {'code': 'quota_unknown', 'reason': 'fixture observation unavailable'}}


def candidate(lane='codex', model='gpt-6.1-sol', provider='codex-cli', **extra):
    return {'lane': lane, 'model': model, 'provider': provider, 'auth_scope': scope(lane), **extra}


class PolicyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fresh-quota-exam-')
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        for name in ('state', 'native'):
            (home / name).mkdir()
        self.config = {
            'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'fresh-quota-exam',
            'state_dir': str(home / 'state'), 'codex_home': str(home / 'native'),
            'codex_bin': str(ROOT / 'acceptance/mcp_worker/fake_codex.py'),
            'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
            'limits': {'max_concurrent_jobs': 2, 'max_timeout_s': 30, 'termination_grace_s': 1,
                       'startup_timeout_s': 5, 'max_handoff_bytes': 65536,
                       'max_output_bytes': 1048576, 'metadata_max_age_s': 30,
                       'process_poll_ms': 20},
        }
        for context in (patch.dict('os.environ', {'CORTADOS_HOME': str(home), 'CORTADOS_REPO': str(home)}),
                        patch.object(time, 'time', return_value=EPOCH),
                        patch.object(time, 'monotonic', return_value=MONOTONIC)):
            context.start()
            self.addCleanup(context.stop)
        from cortados_router import quota_admission, quota_observations
        self.policy, self.collector = quota_admission, quota_observations
        self.collect = patch.object(quota_observations, 'collect', return_value=(unavailable(),)).start()
        self.addCleanup(patch.stopall)

    def check(self, obs, provider='codex-cli', model='gpt-6.1-sol', auth_scope=None):
        auth_scope = auth_scope if auth_scope is not None else (obs[0]['auth_scope'] if obs else scope())
        result = self.policy.check(provider, model, auth_scope=auth_scope, config=deepcopy(self.config), observations=tuple(obs))
        self.assertEqual(set(result), {'status', 'kind', 'lane', 'pool', 'observed_at', 'age_s', 'source', 'reason'})
        self.assertIsInstance(result['reason'], str)
        return result

    def resources(self, candidates, observations, costs=None):
        return self.policy.resources(candidates, observations, costs,
                                     now=EPOCH, monotonic=MONOTONIC, max_age_s=30)
