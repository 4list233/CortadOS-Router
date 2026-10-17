# PURPOSE: Pin derived reservation indexes to one admitted writer invocation.
# CONTRACT: RT-TRAINING-TEACHER-007 sole writer and occupied-slot invariants.
# Reused instances may refuse entry; admitted instances must use the current manifest.
from . import test_attempts
from .support import TeacherCase, answers
from integrations.mcp.schemas import WorkerError

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
        try:
            first.__enter__()
        except WorkerError:
            self.assertEqual(self.labels(), [])
            return
        try:
            self.assertTrue(first.occupied(batch[0], 0, 'claude-sonnet-5', self.snapshot),
                            'Reentered writer lost the current reservation index')
            self.assertEqual(first.publish_claude(batch, model='claude-sonnet-5', sample=0,
                answers=answers(batch), meta={}, snapshot=self.snapshot), 0)
        finally:
            first.__exit__(None, None, None)
        self.assertEqual(self.labels(), [])
