# PURPOSE: Preserve ordinary MCP policy while enforcing teacher-only quota conservation.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T05,T06; synthetic native observations only.
import copy
import json
import math
import time
import uuid
from unittest.mock import Mock, patch
from .support import TeacherCase, ROOT, SYSTEM
from integrations.mcp.schemas import WorkerError, validate


class TeacherAdmission(TeacherCase):
    def load(self, config=None):
        from integrations.mcp.config import load_config
        return load_config(self.config_path, json.dumps(config or self.config).encode())

    def test_teacher_config_is_explicit_and_normalized(self):
        normalized = self.load()
        self.assertEqual(normalized['teacher_policy'], 'training_labels_v1')
        self.assertEqual(normalized['teacher_max_scarcity'], 0.7)

    def test_ordinary_config_gets_no_new_defaults(self):
        original = copy.deepcopy(self.config)
        original.pop('teacher_policy'); original.pop('teacher_max_scarcity')
        self.assertEqual(self.load(original), original)

    def test_policy_requires_closed_safe_teacher_config(self):
        bad = [('teacher_policy', 'unknown'), ('teacher_max_scarcity', True),
               ('teacher_max_scarcity', -0.1), ('teacher_max_scarcity', 0.71),
               ('teacher_max_scarcity', float('nan')), ('allow_network', True),
               ('model_allowlist', ['gpt-6-astra']), ('connector_registry', {'bad': {}})]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                candidate = copy.deepcopy(self.config); candidate[key] = value
                with self.assertRaises(WorkerError): self.load(candidate)
        for key, value in [('max_concurrent_jobs', 2), ('max_timeout_s', 601), ('metadata_max_age_s', 31)]:
            with self.subTest(limit=key):
                candidate = copy.deepcopy(self.config); candidate['limits'][key] = value
                with self.assertRaises(WorkerError): self.load(candidate)

    def test_policy_and_ceiling_must_appear_together(self):
        for missing in ('teacher_policy', 'teacher_max_scarcity'):
            candidate = copy.deepcopy(self.config); candidate.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(WorkerError): self.load(candidate)

    def arguments(self):
        return validate('delegate', {'task': SYSTEM, 'context': '[]',
            'selection': {'mode': 'explicit', 'model': 'gpt-6.1-sol'},
            'constraints': {'allowed_models': ['gpt-6.1-sol']},
            'permissions': {'network': False, 'mcp_servers': []}, 'account_ref': 'native-default',
            'request_id': str(uuid.uuid4()), 'cwd': str(self.state / 'teacher-cwd'), 'timeout_s': 600}, self.config)

    def native_snapshot(self):
        records = [json.loads(line)['result'] for line in (ROOT / 'acceptance/mcp_worker/fixtures/native-metadata-wire.jsonl').read_text().splitlines()]
        now = time.time()
        limits = records[2]
        for bucket in [limits['rateLimits'], *limits['rateLimitsByLimitId'].values()]:
            bucket['primary']['resetsAt'] = now + 302400
        return {'account': records[1], 'rate_limits': limits, 'models': ['gpt-6.1-sol'],
                'sampled_at_ms': now * 1000, 'sampled_monotonic': time.monotonic()}

    def test_same_admitted_resource_snapshot_owns_scarcity_ceiling(self):
        from cortados_router import worker_admission as owner
        for scarcity, code in ((0.6999, None), (0.7, 'permission_refused'), (0.9, 'permission_refused'),
                                (None, 'quota_unknown'), (float('nan'), 'quota_unknown')):
            resources = Mock(); resources.health.return_value = {'codex': {'scarcity': scarcity}}
            metadata = Mock(); metadata.snapshot.return_value = self.native_snapshot()
            with self.subTest(scarcity=scarcity), patch.object(owner.SelectionResources, 'from_quota', return_value=resources) as project:
                if code:
                    with self.assertRaises(WorkerError) as error: owner.admit(self.arguments(), self.config, metadata)
                    self.assertEqual(error.exception.code, code)
                else:
                    self.assertIs(owner.admit(self.arguments(), self.config, metadata)['resources'], resources)
                self.assertEqual(metadata.snapshot.call_count, 1)
                self.assertEqual(project.call_count, 1)

    def test_ordinary_workers_keep_prior_scarcity_policy(self):
        from cortados_router import worker_admission as owner
        config = copy.deepcopy(self.config); config.pop('teacher_policy'); config.pop('teacher_max_scarcity')
        resources = Mock(); resources.health.return_value = {'codex': {'scarcity': 0.99}}
        metadata = Mock(); metadata.snapshot.return_value = self.native_snapshot()
        with patch.object(owner.SelectionResources, 'from_quota', return_value=resources):
            self.assertIs(owner.admit(self.arguments(), config, metadata)['resources'], resources)

    def test_teacher_does_not_bypass_account_freshness_or_quota(self):
        from cortados_router import worker_admission as owner
        for kind, code in [('api', 'subscription_required'), ('wall', 'quota_unknown'),
                           ('monotonic', 'quota_unknown'), ('spent', 'quota_spent')]:
            snap = self.native_snapshot()
            if kind == 'api': snap['account']['account'] = {'type': 'apiKey'}
            if kind == 'wall': snap['sampled_at_ms'] -= 31000
            if kind == 'monotonic': snap['sampled_monotonic'] -= 31
            if kind == 'spent': snap['rate_limits']['ordinaryUsageAllowed'] = False
            metadata = Mock(); metadata.snapshot.return_value = snap
            with self.subTest(kind=kind), self.assertRaises(WorkerError) as error:
                owner.admit(self.arguments(), self.config, metadata)
            self.assertEqual(error.exception.code, code)

    def test_teacher_policy_does_not_change_native_argv(self):
        from integrations.mcp.codex import native_arguments
        ordinary = copy.deepcopy(self.config); ordinary.pop('teacher_policy'); ordinary.pop('teacher_max_scarcity')
        args = self.arguments(); route = {'chosen': {'model': 'gpt-6.1-sol'}}
        teacher = native_arguments(self.config, args, route, self.data / 'final')
        self.assertEqual(teacher, native_arguments(ordinary, args, route, self.data / 'final'))
        self.assertIn('workspace-write', teacher)
        self.assertIn('--strict-config', teacher)
        self.assertIn('mcp_servers={}', teacher)
        self.assertTrue(any('network_access=false' in arg.replace(' ', '').replace(chr(34), '') for arg in teacher))

    def test_prompt_and_closed_delegate_arguments_use_existing_payload(self):
        args = self.owner().codex_arguments(self.snapshot['rows'][:3], sample=0,
            system_prompt=SYSTEM, request_id=str(uuid.uuid4()), cwd=str(self.state / 'teacher-cwd'))
        self.assertEqual(args['task'], SYSTEM)
        self.assertEqual(args['context'], json.dumps(self.learning.teacher_payload(self.snapshot['rows'][:3]), ensure_ascii=False))
        self.assertEqual(args, validate('delegate', args, self.config))
        self.assertEqual(args['timeout_s'], 600)
        self.assertEqual(args['permissions'], {'network': False, 'mcp_servers': []})
