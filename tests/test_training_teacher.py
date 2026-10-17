"""Builder regressions: conserved batch identity, durable empty slots and bounded workers."""
import copy
import json
import threading
import time
from unittest.mock import patch

from acceptance.training_teacher.support import TeacherCase, answers, SYSTEM
from integrations.mcp.schemas import WorkerError


class TrainingTeacherRegressions(TeacherCase):
    def test_training_capacity_refusal_replays_original_batch_despite_changed_batch_size(self):
        self.transport(); self.broker.refusal = 'capacity_exhausted'
        first = self.run_labels(split='train', limit=1)
        original = copy.deepcopy(self.broker.calls[0])
        self.assertEqual(first['planned_batches'], 1)
        self.broker.refusal = None
        second = self.run_labels(split='train', limit=1, batch=1)
        self.assertEqual(self.broker.calls, [original, original])
        self.assertEqual(second['planned_batches'], 1)
        self.assertEqual(len(self.labels()), len(json.loads(original['context'])))

    def test_training_lost_acceptance_retains_uuid_and_publishes_exactly_once(self):
        self.transport(); self.broker.lost_once = True
        first = self.run_labels(split='train', limit=1)
        self.assertEqual(first['diagnostics'][0]['code'], 'storage_error')
        original = copy.deepcopy(self.broker.calls[0])
        self.run_labels(split='train', limit=1, batch=1)
        replay = [a for a in self.broker.calls if a['request_id'] == original['request_id']]
        self.assertEqual(replay, [original, original])
        slots = [(row['id'], row['sample']) for row in self.labels()]
        self.assertEqual(len(slots), len(set(slots)))

    def test_training_failed_native_slots_never_gain_a_second_request_identity(self):
        self.transport(); self.broker.state = 'failed'
        self.run_labels(split='train', limit=1)
        first = self.broker.calls[0]
        self.run_labels(split='train', limit=1)
        repeats = [a for a in self.broker.calls if a['context'] == first['context']]
        self.assertEqual(repeats, [first])
        self.assertEqual(self.labels(), [])

    def test_primary_worker_concurrency_respects_requested_bound(self):
        mutex = threading.Lock()
        active = peak = 0
        def primary(model, batch, **kwargs):
            nonlocal active, peak
            with mutex:
                active += 1; peak = max(peak, active)
            time.sleep(0.01)
            with mutex: active -= 1
            return answers(batch), {'usage': {}}
        self.transport(claude=primary)
        self.run_labels(batch=1, workers=3)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 3)
        self.assertEqual(len(self.labels()), len(self.snapshot['rows']))

    def test_unknown_primary_check_does_not_refresh_scarcity_or_start_subprocess(self):
        from cortados_router import quota_admission, scarcity
        owner = self.owner()
        with patch('shutil.which', return_value=self.config['codex_bin']), \
             patch.object(quota_admission, 'prepare_actual', return_value=object()), \
             patch.object(quota_admission, 'check_actual', return_value={'status': 'unknown'}), \
             patch.object(scarcity, 'current') as refresh, patch('subprocess.run') as run:
            with self.assertRaises(owner.PreStartRefusal):
                owner.call_claude('claude-sonnet-5', self.snapshot['rows'][:2], system_prompt=SYSTEM,
                    environment=self.environment, cwd=str(self.data), config=self.config, max_scarcity=0.7)
        refresh.assert_not_called(); run.assert_not_called()

    def test_claude_quota_view_preserves_existing_closed_owner_values(self):
        from cortados_router import quota_admission
        from cortados_router.quota_observations import _configuration
        owner = self.owner()
        seen = []
        def unproven(provider, **kwargs):
            seen.append(kwargs['config'])
            _configuration(kwargs['config'])
            raise ValueError('Actual source is unavailable')
        with patch('shutil.which', return_value=self.config['codex_bin']), \
             patch.object(quota_admission, 'prepare_actual', side_effect=unproven):
            with self.assertRaises(owner.PreStartRefusal):
                owner.call_claude('claude-sonnet-5', self.snapshot['rows'][:2], system_prompt=SYSTEM,
                    environment=self.environment, cwd=str(self.data), config=self.config, max_scarcity=0.7)
        expected = {k: v for k, v in self.config.items() if k not in ('teacher_policy', 'teacher_max_scarcity')}
        self.assertEqual(seen, [expected])
