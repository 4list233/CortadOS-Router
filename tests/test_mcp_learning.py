# PURPOSE: Regress MCP learning containment, snapshot lifetime and partition identities.
# CONTRACT: RT-MCP-LEARNING-003; builder regressions accumulate beside protected exams.
# DEPENDS: Standard unittest/tempfile/SQLite and the three installed learning owners.
# CEILING: Synthetic evidence only; no live state, model calls, training or activation.
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from cortados_router import learning_data as learning
from cortados_router import learning_source as source
from cortados_router import task_evidence as evidence
from integrations.mcp.schemas import WorkerError, canonical, validate


def packet(**updates):
    value = {'task': 'Inspect synthetic parser', 'selection': {'mode': 'explicit', 'model': 'gpt-6-luna'},
             'context': '', 'cwd': '/', 'timeout_s': 30, 'request_id': str(uuid.uuid4())}
    value.update(updates)
    return validate('delegate', value, {})


def task_evidence():
    return {'schema': 'cortados.mcp-task-evidence.v1', 'task_group_id': str(uuid.uuid4()),
            'components': [], 'dependencies': [], 'constraints': [], 'uncertainties': [],
            'verification': ['Inspect the synthetic output']}


def record(arguments, **updates):
    job_id, route_id = str(uuid.uuid4()), str(uuid.uuid4())
    value = {'job_id': job_id, 'route_id': route_id, 'arguments': arguments,
             'argument_hash': hashlib.sha256(canonical(arguments)).hexdigest(),
             'state': 'running', 'reason': None, 'elapsed_ms': 1, 'result': None,
             'route': {'chosen': {'model': 'gpt-6-luna'}}}
    value.update(updates)
    return value


class LearningRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mcp-learning-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700)
        self.state = self.root / 'state'
        (self.state / 'router').mkdir(parents=True)

    def seed(self, value, *, wal=False):
        args = value['arguments']
        self.jobs = sqlite3.connect(self.state / 'jobs.db')
        self.addCleanup(self.jobs.close)
        if wal:
            self.jobs.execute('PRAGMA journal_mode=WAL')
        self.jobs.execute('CREATE TABLE jobs(job_id,client_id,request_id,argument_hash,arguments,record)')
        self.jobs.execute('PRAGMA user_version=1')
        self.jobs.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
            (value['job_id'], 'synthetic-client', args['request_id'], value['argument_hash'],
             canonical(args), json.dumps(value)))
        self.jobs.commit()
        with closing(sqlite3.connect(self.state / 'router/router.db')) as db:
            db.execute('CREATE TABLE routes(turn_id,context)')
            db.execute('INSERT INTO routes VALUES (?,?)', (value['route_id'], json.dumps(evidence.provenance(args))))
            db.commit()

    def test_task_evidence_explicit_large_object_uses_worker_budget_only(self):
        supplied = task_evidence()
        supplied['references'] = [{'source': 'synthetic', 'text': 'λ' * 6001}]
        arguments = packet(task_evidence=supplied)
        limits = {'limits': {'max_handoff_bytes': 20000}}
        report = evidence.context_requirements(arguments, limits)['context_requirements']
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['scorer_chars'], 0)
        arguments['selection'] = {'mode': 's1'}
        self.assertEqual(evidence.context_requirements(arguments, limits)['context_requirements']['status'], 'over_budget')

    def test_task_evidence_duplicate_references_preserve_order_and_utf8_invalid_refuses(self):
        supplied = task_evidence()
        reference = {'source': 'synthetic', 'text': 'quote λ'}
        supplied['references'] = [reference, reference]
        self.assertEqual(evidence.validate_evidence(supplied)['references'], [reference, reference])
        supplied['references'][0] = {'source': 'synthetic', 'text': '\ud800'}
        with self.assertRaises(WorkerError):
            evidence.validate_evidence(supplied)

    def test_learning_source_closes_each_snapshot_connection_on_success_and_failure(self):
        self.seed(record(packet()))
        real_connect = sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch.object(sqlite3, 'connect', connect):
            source.read_observations(self.state)
            with self.assertRaises(source.SourceError):
                source._snapshot(self.state / 'jobs.db', ['SELECT absent_column FROM jobs'])
        self.assertEqual(len(connections), 3)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_learning_source_committed_wal_pending_progress_does_not_create_revision(self):
        value = record(packet())
        self.seed(value, wal=True)
        first = learning.collect_mcp(self.state, self.data)
        self.assertEqual(first['added'], 1)
        before = (self.data / 'requests.jsonl').read_bytes()
        value.update(state='cancelling', elapsed_ms=5000)
        self.jobs.execute('UPDATE jobs SET record=?', (json.dumps(value),))
        self.jobs.commit()
        self.assertEqual(learning.collect_mcp(self.state, self.data)['added'], 0)
        self.assertEqual((self.data / 'requests.jsonl').read_bytes(), before)
        current = learning.read_rows(self.data / 'mcp-observations.jsonl')[0]
        self.assertEqual(current['native_outcome'], 'pending')
        self.assertIsNone(current['elapsed_ms'])

    def test_learning_source_malformed_worker_is_degraded_and_preserves_published_evidence(self):
        value = record(packet())
        self.seed(value)
        self.assertEqual(learning.collect_mcp(self.state, self.data)['status'], 'ok')
        before = {path.name: path.read_bytes() for path in self.data.glob('*.jsonl')}
        value.update(state='succeeded', result={'worker': ['PRIVATE_BAD_WORKER'], 'usage': None})
        self.jobs.execute('UPDATE jobs SET record=?', (json.dumps(value),))
        self.jobs.commit()
        result = learning.collect_mcp(self.state, self.data)
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['diagnostics'][0]['code'], 'mcp_source_invalid')
        self.assertNotIn('PRIVATE_BAD_WORKER', json.dumps(result))
        self.assertEqual({path.name: path.read_bytes() for path in self.data.glob('*.jsonl')}, before)

    def test_learning_source_unknown_state_and_invalid_argument_type_are_contained(self):
        value = record(packet())
        self.seed(value)
        for malformed in (dict(value, state='answer_accepted'), dict(value, arguments=[])):
            self.jobs.execute('UPDATE jobs SET record=?', (json.dumps(malformed),))
            self.jobs.commit()
            result = learning.collect_mcp(self.state, self.data)
            self.assertEqual(result['status'], 'degraded')
            self.assertFalse((self.data / 'mcp-observations.jsonl').exists())

    def test_learning_eligibility_connects_transitive_group_fingerprint_conflicts(self):
        observations = []
        requests = []
        for index, (group, text) in enumerate((('group-a', 'one'), ('group-a', 'two'), ('group-b', 'two'))):
            rid, job_id = 'synthetic-' + str(index), str(uuid.uuid4())
            requests.append({'id': rid, 'source': 'mcp-worker', 'job_id': job_id, 'group_key': group,
                'task_fingerprint': evidence.digest(text), 'request': text, 'split': 'train' if index < 2 else 'holdout'})
            observations.append({'schema': 'cortados.mcp-learning.v1', 'job_id': job_id,
                                 'learning_id': rid, 'eligibility': True})
        original = copy.deepcopy(requests)
        selected = learning.eligible_rows(requests, observations)
        self.assertEqual(selected['rows'], [])
        self.assertEqual({row['reason'] for row in selected['excluded']}, {'split_conflict'})
        self.assertEqual(requests, original)

    def test_learning_evaluation_cache_hashes_content_not_adapter_names_or_row_counts(self):
        adapter, base = self.root / 'adapter', self.root / 'base'
        adapter.mkdir()
        base.mkdir()
        (adapter / 'weights.safetensors').write_bytes(b'weights-v1')
        (base / 'tokenizer.json').write_bytes(b'tokenizer-v1')
        (base / 'config.json').write_bytes(b'config-v1')
        row = {'id': 'unchanged', 'request': 'First λ request', 'split': 'holdout', 'unit_kind': 'interactive'}
        def identity():
            return learning.evaluation_identity([(row, {'answer': 1})], adapter=adapter, base=base,
                                                 fail_up=0.6, outward_at=0.5)
        first = identity()
        (adapter / 'weights.safetensors').write_bytes(b'weights-v2')
        second = identity()
        self.assertNotEqual(first, second)
        (base / 'tokenizer.json').write_bytes(b'tokenizer-v2')
        third = identity()
        self.assertNotEqual(second, third)
        row['request'] = 'Second λ request'
        self.assertNotEqual(third, identity())

    def test_learning_cache_invalidates_effective_prompt_questions_and_options(self):
        material = {'messages': [{'role': 'system', 'content': 'Original rubric'}],
                    'questions': ['first', 'second'], 'options': {'first': 4, 'second': 2}}
        def identity():
            return learning.evaluation_identity([], fail_up=0.6, outward_at=0.5,
                                                 scorer_material=material)
        first = identity()
        material['messages'][0]['content'] = 'Changed rubric'
        second = identity()
        self.assertNotEqual(first, second)
        material['questions'].reverse()
        third = identity()
        self.assertNotEqual(second, third)
        material['options']['second'] = 4
        self.assertNotEqual(third, identity())

    def test_evaluate_cache_uses_actual_student_prompt_rubric_dependency(self):
        import sys
        from acceptance.mcp_learning.pipeline_support import pipeline
        with pipeline('evaluate', self.data) as (evaluation, _):
            student = sys.modules['student']
            original = evaluation.cache_identity([], 0.6, {})
            with patch.object(student, 'SYSTEM', student.SYSTEM + '\nChanged synthetic anchor'):
                changed = evaluation.cache_identity([], 0.6, {})
            self.assertNotEqual(original, changed)
            with patch.object(evaluation.core, 'WORK_KINDS', list(reversed(evaluation.core.WORK_KINDS))):
                encoded_differently = evaluation.cache_identity([], 0.6, {})
            self.assertNotEqual(original, encoded_differently)


if __name__ == '__main__':
    unittest.main()
