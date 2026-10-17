# PURPOSE: Synthetic training requests, broker receipts and canonical existing answer validator.
# CONTRACT: RT-TRAINING-TEACHER-007; no provider calls, live labels or credentials.
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('teacher_exam_common', ROOT / 's1/common.py')
COMMON = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(COMMON)
QUESTIONS = COMMON.QUESTIONS
ANSWER = {**{key: 1 for key in COMMON.core.FEATURES}, 'outward': False,
          'work_kind': COMMON.core.WORK_KINDS[0], 'urgency': COMMON.core.URGENCY[0],
          'needs_clarification': False, 'parts': 1, 'diversity': False}
SYSTEM = 'Synthetic teacher rubric. Return the twelve answers as JSON, with each input i.'


def rows(count=30):
    return [{'id': 'teacher-exam-' + str(i), 'unit_kind': 'interactive',
             'request': 'Synthetic independent topic number ' + str(i),
             'split': 'holdout' if i % 7 == 0 else 'train'} for i in range(count)]


def answers(batch):
    return [dict(i=i, **copy.deepcopy(ANSWER)) for i, _ in enumerate(batch)]


def options(**changes):
    return dict(split='all', k=1, batch=12, workers=3, limit=0, max_scarcity=0.7, only='', **changes)


class BrokerFixture:
    """Replay identity fixture at the existing typed broker transport boundary."""
    def __init__(self):
        self.calls, self.jobs = [], {}
        self.refusal = None
        self.lost_once = False
        self.output = None
        self.state = 'succeeded'
        self.native_starts = 0

    def submit(self, arguments):
        from integrations.mcp.schemas import WorkerError
        self.calls.append(copy.deepcopy(arguments))
        rid = arguments['request_id']
        if rid not in self.jobs:
            if self.refusal:
                raise WorkerError(self.refusal, 'Synthetic refusal')
            self.native_starts += 1
            jid = str(uuid.uuid4())
            batch = json.loads(arguments['context'])
            summary = json.dumps(self.output if self.output is not None else answers(batch))
            from integrations.mcp.codex import terminal_result
            result = terminal_result({'chosen': {'model': 'gpt-6.1-sol'}}, jid,
                {'events': 'events.jsonl', 'final_message': 'final.txt'},
                cli_version='0.163.0', summary=summary,
                error=None if self.state == 'succeeded' else WorkerError('native_failed', 'Synthetic failure'))
            self.jobs[rid] = {'arguments': copy.deepcopy(arguments), 'response': {'ok': True,
                'job': {'job_id': jid, 'route_id': str(uuid.uuid4()), 'state': self.state,
                    'cwd': arguments['cwd'], 'created_at_ms': 1000, 'started_at_ms': 1001,
                    'finished_at_ms': 1100, 'elapsed_ms': 99, 'progress': 'Synthetic terminal',
                    'reason': None if self.state == 'succeeded' else 'native_failed'}, 'result': result}}
            if self.lost_once:
                self.lost_once = False
                raise WorkerError('storage_error', 'Synthetic lost acceptance response')
        if self.jobs[rid]['arguments'] != arguments:
            raise WorkerError('idempotency_conflict', 'Synthetic identity conflict')
        value = copy.deepcopy(self.jobs[rid]['response'])
        value.pop('result')
        return value

    def status(self, job_id):
        value = self.result(job_id)
        value.pop('result')
        return value

    def result(self, job_id):
        return copy.deepcopy(next(row['response'] for row in self.jobs.values()
                                  if row['response']['job']['job_id'] == job_id))


class TeacherCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='teacher-exam-')
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name).resolve()
        self.native = self.data / 'native'
        self.native.mkdir()
        self.state = self.data / 'state'
        self.state.mkdir(mode=0o700)
        (self.state / 'teacher-cwd').mkdir(mode=0o700)
        self.config = {'schema': 'cortados.mcp-worker.config.v1', 'client_id': 's1-teacher',
            'state_dir': str(self.state), 'codex_bin': str(ROOT / 'acceptance/mcp_worker/fake_codex.py'),
            'codex_home': str(self.native), 'model_allowlist': ['gpt-6.1-sol'],
            'allow_network': False, 'connector_registry': {}, 'teacher_policy': 'training_labels_v1',
            'teacher_max_scarcity': 0.7, 'limits': {'max_concurrent_jobs': 1, 'max_timeout_s': 600,
                'termination_grace_s': 1, 'startup_timeout_s': 5, 'max_handoff_bytes': 65536,
                'max_output_bytes': 1048576, 'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        self.config_path = self.data / 'teacher.json'
        self.config_path.write_text(json.dumps(self.config))
        self.environment = {'HOME': str(self.data), 'PATH': os.environ.get('PATH', '')}
        env = patch.dict(os.environ, {'CORTADOS_TRAINING_TEACHER_V1': '1', 'CORTADOS_MCP_LEARNING_V1': '1'})
        env.start()
        self.addCleanup(env.stop)
        from cortados_router import learning_data
        self.learning = learning_data
        self.requests = rows()
        self.snapshot = self.learning.bind_snapshot(self.data, requests=self.requests)

    def owner(self):
        return importlib.import_module('cortados_router.teacher')

    def run_labels(self, **changes):
        opts = options()
        opts.update(changes)
        return self.owner().run_labels(data_dir=self.data, snapshot=self.snapshot, options=opts,
            system_prompt=SYSTEM, claude_environment=self.environment, claude_cwd=str(self.data),
            worker_config_path=self.config_path, validate_answers=COMMON.valid_answers, questions=QUESTIONS)

    def labels(self):
        path = self.data / 'labels.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def transport(self, *, claude=None):
        owner = self.owner()
        self.broker = BrokerFixture()
        from integrations.mcp import broker
        def rpc(config, digest, tool, arguments):
            from integrations.mcp.schemas import WorkerError
            try:
                if tool == 'delegate': return self.broker.submit(arguments)
                if tool == 'job_status': return self.broker.status(arguments['job_id'])
                if tool == 'job_result': return self.broker.result(arguments['job_id'])
                raise AssertionError('Unexpected broker operation: ' + tool)
            except WorkerError as exc:
                return exc.payload()
        a = patch.object(broker, 'rpc', side_effect=rpc)
        start = patch.object(broker, 'ensure_broker')
        start.start(); self.addCleanup(start.stop)
        b = patch.object(owner, 'call_claude', side_effect=claude or owner.PreStartRefusal('quota_unknown'))
        a.start(); mocked_claude = b.start()
        self.addCleanup(a.stop); self.addCleanup(b.stop)
        return mocked_claude
