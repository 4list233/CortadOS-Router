# PURPOSE: Pin derived reservation indexes to one admitted writer invocation.
# CONTRACT: RT-TRAINING-TEACHER-007 sole writer and occupied-slot invariants.
# Revision2 preserves reusable instances, with fresh current-state validation after each acquisition.
from . import test_attempts
from .support import TeacherCase, answers

class InvocationCache(TeacherCase):
    store = test_attempts.DurableAttempts.store
    reserve = test_attempts.DurableAttempts.reserve

    def test_reused_writer_never_loses_another_invocations_reserved_slots(self):
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        first = self.store()
        with first:
            self.assertFalse(first.occupied(batch[0], 0, 'claude-sonnet-5', self.snapshot))
        with self.store() as second:
            attempt = self.reserve(second)
        self.assertEqual(attempt['row_ids'], [r['id'] for r in batch])
        with first:
            self.assertTrue(first.occupied(batch[0], 0, 'claude-sonnet-5', self.snapshot),
                            'Reentered writer lost the current reservation index')
            self.assertEqual(first.publish_claude(batch, model='claude-sonnet-5', sample=0,
                answers=answers(batch), meta={}, snapshot=self.snapshot), 0)
        self.assertEqual(self.labels(), [])

    def test_reused_populated_writer_reloads_reservations_without_changing_label_prefix(self):
        train = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train']
        batch = train[:2]
        first = self.store()
        with first:
            self.assertEqual(first.publish_claude([train[2]], model='claude-sonnet-5', sample=0,
                answers=answers([train[2]]), meta={}, snapshot=self.snapshot), 1)
            self.assertTrue(first.occupied(train[2], 0, 'claude-sonnet-5', self.snapshot))
            self.assertFalse(first.occupied(batch[0], 0, 'claude-sonnet-5', self.snapshot))
        prefix = (self.data / 'labels.jsonl').read_bytes()
        with self.store() as second:
            attempt = self.reserve(second)
        self.assertEqual(attempt['row_ids'], [r['id'] for r in batch])
        with first:
            self.assertTrue(first.occupied(train[2], 0, 'claude-sonnet-5', self.snapshot))
            self.assertTrue(first.occupied(batch[0], 0, 'claude-sonnet-5', self.snapshot))
            self.assertEqual(first.publish_claude(batch, model='claude-sonnet-5', sample=0,
                answers=answers(batch), meta={}, snapshot=self.snapshot), 0)
        self.assertEqual((self.data / 'labels.jsonl').read_bytes(), prefix)
