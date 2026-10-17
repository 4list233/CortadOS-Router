# PURPOSE: Independent B1 amplification of sole-writer lifetime and preserved Claude behavior.
# CONTRACT: RT-TRAINING-TEACHER-007 sections 3-5; no provider or live-state calls.
import contextlib
import io
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from .support import TeacherCase, COMMON, QUESTIONS, ANSWER, answers
from integrations.mcp.schemas import WorkerError

class ReviewRegressions(TeacherCase):
    def store(self):
        from cortados_router.teacher_attempts import LabelAttempts
        return LabelAttempts(self.data, validate_answers=COMMON.valid_answers, questions=QUESTIONS)

    def batch(self):
        return [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]

    def publish(self, store, result=None):
        return store.publish_claude(self.batch(), model='claude-sonnet-5', sample=0,
            answers=answers(self.batch()) if result is None else result, meta={}, snapshot=self.snapshot)

    def test_closed_writer_cannot_publish_while_next_invocation_holds_lock(self):
        with self.store() as first:
            pass
        with self.store():
            with self.assertRaises(WorkerError):
                self.publish(first)
        self.assertEqual(self.labels(), [])

    def test_replaced_lock_inode_invalidates_old_writer(self):
        with self.store() as first:
            lock = self.data / '.teacher-attempts/lock'
            lock.rename(lock.with_name('old-lock'))
            lock.touch(mode=0o600)
            with self.store():
                with self.assertRaises(WorkerError):
                    self.publish(first)
        self.assertEqual(self.labels(), [])

    def test_wrong_process_context_cannot_publish(self):
        with self.store() as store:
            with patch('os.getpid', return_value=os.getpid() + 1):
                with self.assertRaises(WorkerError):
                    self.publish(store)
        self.assertEqual(self.labels(), [])

    def test_same_process_worker_thread_retains_writer_authority(self):
        with self.store() as store, ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(executor.submit(self.publish, store).result(), 2)
        self.assertEqual(len(self.labels()), 2)

    def test_claude_rows_skip_invalid_entries_without_discarding_valid_neighbors(self):
        result = [dict(i=0, **ANSWER), None, 'bad', {'i': 1}, dict(i=-1, **ANSWER),
                  dict(i=2, **ANSWER), dict(i=1, **ANSWER)]
        with self.store() as store:
            self.assertEqual(self.publish(store, result), 2)
        self.assertEqual({r['id'] for r in self.labels()}, {r['id'] for r in self.batch()})

    def test_claude_duplicate_indices_publish_each_slot_once(self):
        result = [dict(i=0, **ANSWER), dict(i=0, **ANSWER), dict(i=1, **ANSWER)]
        with self.store() as store:
            self.assertEqual(self.publish(store, result), 2)
        self.assertEqual(len(self.labels()), 2)

    def test_claude_bool_index_retains_legacy_int_semantics(self):
        with self.store() as store:
            self.assertEqual(self.publish(store, [dict(i=False, **ANSWER), dict(i=True, **ANSWER)]), 2)
        self.assertEqual(len(self.labels()), 2)

    def test_claude_override_skips_invalid_rows_and_preserves_bool_index(self):
        result = [dict(i=False, outward=True), None, dict(i=9, outward=True), dict(i=True, outward=False)]
        with self.store() as store:
            self.assertEqual(store.publish_override(self.batch(), model='claude-sonnet-5', sample=0,
                answers=result, only=['outward'], defaults=ANSWER, snapshot=self.snapshot), 2)
        self.assertEqual(self.labels(), [])

    def test_poststart_claude_nonquota_error_continues_without_codex_fallback(self):
        calls = []
        def primary(model, batch, **kwargs):
            calls.append(batch)
            if len(calls) == 1:
                raise ValueError('Synthetic malformed Claude JSON')
            return answers(batch), {'usage': {}}
        self.transport(claude=primary)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            summary = self.run_labels(split='train', batch=1, workers=1, limit=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(summary['published_labels'], 1)
        self.assertEqual(len(self.labels()), 1)
        self.assertTrue(output.getvalue() or summary['diagnostics'], 'Failure must remain observable')

    def test_poststart_claude_quota_error_stops_without_codex_fallback(self):
        for message in ('rate limit', '429', 'quota exhausted', 'overloaded'):
            with self.subTest(message=message):
                primary = self.transport(claude=RuntimeError(message))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    summary = self.run_labels(split='train', batch=1, workers=1, limit=2)
                self.assertEqual(primary.call_count, 1)
                self.assertEqual(self.broker.calls, [])
                self.assertEqual(summary['published_labels'], 0)
                self.assertTrue(output.getvalue() or summary['diagnostics'])
        self.assertEqual(self.labels(), [])
