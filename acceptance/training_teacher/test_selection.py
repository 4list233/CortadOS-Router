# PURPOSE: Pin training-only fallback and conserved label planning budgets.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T01,T02,T03,T04,T07.
import copy
import json
from unittest.mock import patch
from .support import TeacherCase, ANSWER, answers


class TrainingSelection(TeacherCase):
    def test_available_claude_keeps_primary_and_never_submits_codex(self):
        def available(model, batch, **kwargs):
            return answers(batch), {'usage': {}, 'session_id': 'synthetic'}
        self.transport(claude=available)
        self.run_labels()
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(len(self.labels()), len(self.snapshot['rows']))
        for row in self.labels():
            expected = 'claude-opus-5' if self.snapshot['partitions'][row['id']] == 'holdout' else 'claude-sonnet-5'
            self.assertEqual(row['teacher'], expected)

    def test_unknown_primary_falls_back_only_for_final_training(self):
        self.transport()
        self.run_labels()
        expected = {rid for rid, part in self.snapshot['partitions'].items() if part == 'train'}
        self.assertTrue(expected)
        self.assertEqual({row['id'] for row in self.labels()}, expected)
        self.assertTrue(self.broker.calls)
        for row in self.labels():
            self.assertEqual(row['teacher'], 'gpt-6.1-sol')
            self.assertEqual(row['teacher_execution']['provider'], 'codex-cli')
            self.assertIsNone(row['teacher_execution']['reported_model'])
            self.assertNotIn('attempt_id', {key for key in row if key != 'teacher_execution'})
        for call in self.broker.calls:
            self.assertLessEqual(len(json.loads(call['context'])), 12)

    def test_final_validation_inside_raw_train_never_falls_back(self):
        valid = [row for row in self.snapshot['rows'] if self.snapshot['partitions'][row['id']] == 'valid']
        self.assertTrue(valid)
        self.assertTrue(all(row['split'] == 'train' for row in valid))
        self.snapshot['rows'] = valid
        self.snapshot['partitions'] = {row['id']: 'valid' for row in valid}
        self.transport()
        self.run_labels()
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.labels(), [])

    def test_no_fallback_after_arbitrary_or_started_failure(self):
        for error in (RuntimeError('quota 429 overloaded'), TimeoutError('quota'), ValueError('bad json')):
            with self.subTest(error=type(error).__name__):
                self.transport(claude=error)
                try:
                    self.run_labels(limit=1)
                except type(error):
                    pass
                self.assertEqual(self.broker.calls, [])
                self.assertEqual(self.labels(), [])

    def test_existing_teacher_closes_slot_and_prefix_is_immutable(self):
        row = next(row for row in self.snapshot['rows'] if self.snapshot['partitions'][row['id']] == 'train')
        existing = {'id': row['id'], 'sample': 0, 'teacher': 'claude-sonnet-5', 'ts': 123, 'answers': ANSWER}
        prefix = (json.dumps(existing, indent=None) + '\n').encode()
        (self.data / 'labels.jsonl').write_bytes(prefix)
        override = b'{"synthetic":"untouched"}\n'
        (self.data / 'labels-override-outward.jsonl').write_bytes(override)
        self.transport()
        self.run_labels()
        self.assertTrue((self.data / 'labels.jsonl').read_bytes().startswith(prefix))
        self.assertEqual((self.data / 'labels-override-outward.jsonl').read_bytes(), override)
        self.assertEqual(sum(r['id'] == row['id'] and r['sample'] == 0 for r in self.labels()), 1)

    def test_limit_counts_primary_refusal_and_fallback_as_one_batch(self):
        self.snapshot['rows'] = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train']
        self.transport()
        self.run_labels(limit=1, batch=4)
        self.assertEqual(self.broker.native_starts, 1)
        self.assertEqual(len(self.labels()), 4)

    def test_only_override_never_uses_fallback(self):
        self.transport()
        try:
            self.run_labels(only='outward')
        except (ValueError, RuntimeError):
            pass
        self.assertEqual(self.broker.calls, [])

    def test_invalid_options_cause_zero_calls(self):
        self.transport()
        for change in ({'workers': 0}, {'batch': 0}, {'k': True}, {'limit': -1}, {'split': 'invalid'}):
            with self.subTest(change=change):
                with self.assertRaises(Exception):
                    self.run_labels(**change)
        self.assertEqual(self.broker.calls, [])

    def test_changed_config_ceiling_refuses_fallback(self):
        self.transport()
        try:
            self.run_labels(max_scarcity=0.8)
        except Exception as exc:
            self.assertEqual(getattr(exc, 'code', None), 'invalid_request')
        self.assertEqual(self.broker.calls, [])

    def test_accepted_failed_job_never_gets_a_second_identity(self):
        self.transport()
        self.broker.state = 'failed'
        self.run_labels(split='train', limit=1)
        initial = copy.deepcopy(self.broker.calls)
        self.run_labels(split='train', limit=1)
        self.assertTrue(initial)
        first_ids = {r['request_id'] for r in initial}
        first_payloads = {r['context'] for r in initial}
        self.assertTrue(all(r['request_id'] in first_ids for r in self.broker.calls if r['context'] in first_payloads))
        self.assertEqual(self.labels(), [])

    def test_preacceptance_capacity_defers_same_identity_without_spin(self):
        self.transport()
        self.broker.refusal = 'capacity_exhausted'
        self.run_labels(split='train', limit=1)
        self.assertEqual(len(self.broker.calls), 1)
        previous = copy.deepcopy(self.broker.calls[0])
        self.broker.refusal = None
        self.run_labels(split='train', limit=1, batch=1)
        self.assertEqual(self.broker.calls[1], previous)
        self.assertEqual(self.broker.native_starts, 1)

    def test_lost_acceptance_response_replays_exact_original_batch(self):
        self.transport()
        self.broker.lost_once = True
        try:
            self.run_labels(split='train', limit=1)
        except Exception as exc:
            self.assertEqual(getattr(exc, 'code', None), 'storage_error')
        self.assertEqual(self.broker.native_starts, 1)
        previous = copy.deepcopy(self.broker.calls[0])
        self.snapshot['rows'].reverse()
        self.run_labels(split='train', limit=1, batch=1)
        replays = [call for call in self.broker.calls if call['request_id'] == previous['request_id']]
        self.assertGreaterEqual(len(replays), 2)
        self.assertTrue(all(call == previous for call in replays))
        slots = [(r['id'], r['sample']) for r in self.labels()]
        self.assertEqual(len(slots), len(set(slots)))

    def test_malformed_output_indexes_reject_whole_response(self):
        self.transport()
        self.snapshot['rows'] = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        for output in ([dict(i=0, **ANSWER), dict(i=0, **ANSWER)], [dict(i=True, **ANSWER)],
                       [dict(i=99, **ANSWER)], [dict(i=0, **ANSWER), 'bad']):
            with self.subTest(output=output):
                self.broker.output = output
                self.run_labels(limit=1)
                self.assertEqual(self.labels(), [])
                # A fresh sample tests another malformed response without retrying accepted work.
                for row in self.snapshot['rows']:
                    row['id'] += '-next'
                self.snapshot['partitions'] = {r['id']: 'train' for r in self.snapshot['rows']}

    def test_partial_unique_response_publishes_only_valid_present_answers(self):
        self.transport()
        self.snapshot['rows'] = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:3]
        self.broker.output = [dict(i=0, **ANSWER), {'i': 1, 'outward': 'invalid'}]
        self.run_labels(limit=1)
        self.assertEqual(len(self.labels()), 1)
        self.assertEqual(self.labels()[0]['answers'], ANSWER)

    def test_primary_call_runs_under_same_writer_lock(self):
        import importlib
        from .support import COMMON, QUESTIONS
        from integrations.mcp.schemas import WorkerError
        lock_checks = []
        def primary(model, batch, **kwargs):
            store = importlib.import_module('cortados_router.teacher_attempts').LabelAttempts
            with self.assertRaises(WorkerError):
                with store(self.data, validate_answers=COMMON.valid_answers, questions=QUESTIONS): pass
            lock_checks.append(True)
            return answers(batch), {'usage': {}}
        self.transport(claude=primary)
        self.run_labels(limit=1)
        self.assertEqual(lock_checks, [True])

    def test_lower_ceiling_config_must_match_request_and_never_mutates_running_config(self):
        self.transport()
        self.config['teacher_max_scarcity'] = 0.5
        self.config_path.write_text(json.dumps(self.config))
        before = self.config_path.read_bytes()
        try:
            self.run_labels(max_scarcity=0.7)
        except Exception as exc:
            self.assertEqual(getattr(exc, 'code', None), 'invalid_request')
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_enabled_split_train_never_writes_validation_or_holdout_even_primary_available(self):
        def primary(model, batch, **kwargs): return answers(batch), {'usage': {}}
        self.transport(claude=primary)
        self.run_labels(split='train')
        expected = {rid for rid, partition in self.snapshot['partitions'].items() if partition == 'train'}
        self.assertEqual({row['id'] for row in self.labels()}, expected)
        self.assertEqual(self.broker.calls, [])

    def test_changed_raw_config_refuses_replay_without_new_identity(self):
        self.transport(); self.broker.refusal = 'capacity_exhausted'
        self.run_labels(split='train', limit=1)
        self.assertEqual(len(self.broker.calls), 1)
        previous = copy.deepcopy(self.broker.calls)
        self.config_path.write_bytes(self.config_path.read_bytes() + b'\n')
        try:
            self.run_labels(split='train', limit=1)
        except Exception as exc:
            self.assertIn(getattr(exc, 'code', None), ('storage_error', 'invalid_request'))
        self.assertEqual(self.broker.calls, previous)
        self.assertEqual(self.broker.native_starts, 0)

    def test_malformed_existing_label_history_refuses_before_any_teacher(self):
        claude = self.transport()
        path = self.data / 'labels.jsonl'; path.write_bytes(b'{"incomplete":')
        with self.assertRaises(Exception): self.run_labels()
        self.assertEqual(path.read_bytes(), b'{"incomplete":')
        claude.assert_not_called()
        self.assertEqual(self.broker.calls, [])
