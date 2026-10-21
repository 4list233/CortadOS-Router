"""Builder regressions for journal reload and immutable private execution mappings."""
import hashlib
import contextvars
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import uuid

from acceptance.training_teacher.support import TeacherCase, COMMON, QUESTIONS, SYSTEM, BrokerFixture
from integrations.mcp.schemas import WorkerError


class TeacherAttemptRegressions(TeacherCase):
    def store(self):
        return importlib.import_module('cortados_router.teacher_attempts').LabelAttempts(
            self.data, validate_answers=COMMON.valid_answers, questions=QUESTIONS)

    def reserve(self, store):
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        arguments = self.owner().codex_arguments(batch, sample=0, system_prompt=SYSTEM,
            request_id=str(uuid.uuid4()), cwd=str(self.state / 'teacher-cwd'))
        return store.reserve(batch, sample=0, system_prompt=SYSTEM, arguments=arguments,
            config_digest=hashlib.sha256(self.config_path.read_bytes()).hexdigest(), snapshot=self.snapshot)

    def test_reserved_manifest_reload_preserves_original_context_bytes(self):
        with self.store() as store: original = self.reserve(store)
        with self.store() as store:
            restored = store.pending()[0]
            self.assertEqual(restored, original)
            self.assertEqual(restored['arguments']['context'], original['arguments']['context'])

    def test_closed_nested_delegate_schema_rejects_journal_tampering(self):
        with self.store() as store: self.reserve(store)
        path = self.data / '.teacher-attempts/manifest.json'
        saved = json.loads(path.read_text())
        saved['attempts'][0]['arguments']['extra'] = True
        path.write_text(json.dumps(saved))
        with self.assertRaises(WorkerError):
            with self.store(): pass

    def _rebind_saved_identity(self, **changes):
        path = self.data / '.teacher-attempts/manifest.json'
        saved = json.loads(path.read_text())
        saved['data_identity'].update(changes)
        path.write_text(json.dumps(saved))
        return saved['data_identity']

    def assertRefused(self):
        with self.assertRaises(WorkerError):
            with self.store(): pass

    def test_reboot_device_change_on_the_same_volume_keeps_the_manifest(self):
        with self.store() as store: original = self.reserve(store)
        current = self.data.stat()
        self._rebind_saved_identity(device=current.st_dev + 1)
        with self.store() as store:
            self.assertEqual(store.pending()[0], original)
            self.assertEqual(store.manifest['data_identity']['device'], current.st_dev)

    def test_replacement_volume_with_the_same_path_and_inode_is_refused(self):
        with self.store() as store: self.reserve(store)
        self._rebind_saved_identity(device=self.data.stat().st_dev + 1,
                                    volume='00000000-0000-4000-8000-000000000000')
        self.assertRefused()

    def test_different_inode_is_refused(self):
        with self.store() as store: self.reserve(store)
        self._rebind_saved_identity(inode=self.data.stat().st_ino + 1)
        self.assertRefused()

    def test_legacy_manifest_opens_only_while_its_device_matches_and_then_gains_the_volume(self):
        with self.store() as store: original = self.reserve(store)
        path = self.data / '.teacher-attempts/manifest.json'
        saved = json.loads(path.read_text())
        volume = saved['data_identity'].pop('volume')
        path.write_text(json.dumps(saved))
        with self.store() as store:
            self.assertEqual(store.pending()[0], original)
            self.assertEqual(store.manifest['data_identity']['volume'], volume)
        saved['data_identity']['device'] += 1
        path.write_text(json.dumps(saved))
        self.assertRefused()

    def test_malformed_saved_identity_is_refused(self):
        with self.store() as store: self.reserve(store)
        self._rebind_saved_identity(device='16777234')
        self.assertRefused()

    def test_same_invocation_context_does_not_allow_a_second_flock(self):
        from cortados_router.teacher_attempts import _ACTIVE
        with self.store() as first:
            self.assertIs(_ACTIVE.get(), first)
            with self.assertRaises(WorkerError):
                with self.store(): pass
            self.assertIs(_ACTIVE.get(), first)
        self.assertIsNone(_ACTIVE.get())

    def test_copied_handoff_context_cannot_cross_threads_or_replay_a_consumed_nonce(self):
        with self.store() as store:
            with store._handoff():
                copied = contextvars.copy_context()
                def claim():
                    with self.assertRaises(WorkerError):
                        store._claim_handoff(self.data, COMMON.valid_answers, QUESTIONS)
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(copied.run, claim).result()
                store._claim_handoff(self.data, COMMON.valid_answers, QUESTIONS)
                with self.assertRaises(WorkerError):
                    copied.run(store._claim_handoff, self.data, COMMON.valid_answers, QUESTIONS)

    def test_manifest_duplicate_key_refuses_without_repair(self):
        with self.store() as store: self.reserve(store)
        path = self.data / '.teacher-attempts/manifest.json'
        original = path.read_text()
        changed = original.replace('"revision":', '"revision":999,"revision":', 1)
        path.write_text(changed)
        with self.assertRaises(WorkerError):
            with self.store(): pass
        self.assertEqual(path.read_text(), changed)

    def test_released_flock_cannot_publish_against_current_owner(self):
        import fcntl
        from acceptance.training_teacher.support import answers
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        with self.store() as stale:
            fcntl.flock(stale.lock.fileno(), fcntl.LOCK_UN)
            with self.store():
                with self.assertRaises(WorkerError):
                    stale.publish_claude(batch, model='claude-sonnet-5', sample=0,
                        answers=answers(batch), meta={}, snapshot=self.snapshot)
        self.assertEqual(self.labels(), [])

    def test_owner_appends_do_not_reparse_history_but_external_generation_does(self):
        from acceptance.training_teacher.support import answers, ANSWER
        from unittest.mock import patch
        rows = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:3]
        path = self.data / 'labels.jsonl'
        historical = {'id': 'old', 'sample': 0, 'teacher': 'claude-sonnet-5', 'ts': 0, 'answers': ANSWER}
        path.write_text(json.dumps(historical) + '\n')
        with self.store() as store:
            original = json.loads
            with patch('cortados_router.teacher_attempts.json.loads', wraps=original) as parse:
                for row in rows[:2]:
                    self.assertEqual(store.publish_claude([row], model='claude-sonnet-5', sample=0,
                        answers=answers([row]), meta={}, snapshot=self.snapshot), 1)
                self.assertEqual(parse.call_count, 0, 'Own append must retain derived historical cache')
                external = dict(historical, id=rows[2]['id'])
                prefix = path.read_bytes() + (json.dumps(external) + '\n').encode()
                path.write_bytes(prefix)
                self.assertEqual(store.publish_claude([rows[2]], model='claude-sonnet-5', sample=0,
                    answers=answers([rows[2]]), meta={}, snapshot=self.snapshot), 0)
                self.assertGreater(parse.call_count, 0, 'Changed file generation must be validated anew')
                self.assertEqual(path.read_bytes(), prefix)

    def test_external_malformed_replacement_cannot_be_hidden_by_append_cache(self):
        from acceptance.training_teacher.support import answers
        rows = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        path = self.data / 'labels.jsonl'
        with self.store() as store:
            store.publish_claude(rows[:1], model='claude-sonnet-5', sample=0,
                answers=answers(rows[:1]), meta={}, snapshot=self.snapshot)
            replacement = path.with_name('external')
            replacement.write_bytes(b'{"incomplete":')
            replacement.replace(path)
            with self.assertRaises(WorkerError):
                store.publish_claude(rows[1:], model='claude-sonnet-5', sample=0,
                    answers=answers(rows[1:]), meta={}, snapshot=self.snapshot)
        self.assertEqual(path.read_bytes(), b'{"incomplete":')

    def test_sequential_reuse_revalidates_unchanged_labels_once_per_invocation(self):
        from pathlib import Path
        from unittest.mock import patch
        from acceptance.training_teacher.support import answers
        row = next(r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train')
        path = self.data / 'labels.jsonl'
        store = self.store()
        with store:
            self.assertEqual(store.publish_claude([row], model='claude-sonnet-5', sample=0,
                answers=answers([row]), meta={}, snapshot=self.snapshot), 1)
        prefix = path.read_bytes()
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=Path.read_bytes) as read:
            for invocation in range(1, 3):
                with store:
                    self.assertTrue(store.occupied(row, 0, 'claude-sonnet-5', self.snapshot))
                    self.assertEqual(store.publish_claude([row], model='claude-sonnet-5', sample=0,
                        answers=answers([row]), meta={}, snapshot=self.snapshot), 0)
                    self.assertEqual(sum(call.args == (path,) for call in read.call_args_list), invocation)
        self.assertEqual(path.read_bytes(), prefix)

    def test_reentering_live_instance_preserves_held_descriptor_and_context(self):
        from cortados_router.teacher_attempts import _ACTIVE
        with self.store() as store:
            descriptor = store.lock.fileno()
            with self.assertRaises(WorkerError):
                store.__enter__()
            self.assertEqual(store.lock.fileno(), descriptor)
            self.assertIs(_ACTIVE.get(), store)
            with self.assertRaises(WorkerError):
                with self.store(): pass

    def test_context_exit_waits_for_inflight_writer_before_releasing_flock(self):
        import threading
        from unittest.mock import patch
        from acceptance.training_teacher.support import answers
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        started, release = threading.Event(), threading.Event()
        observed = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            store = self.store()
            original = store._atomic
            def atomic(*args):
                started.set()
                # Two seconds bounds a broken synchronization test, without racing normal local I/O.
                if not release.wait(2): raise AssertionError('Writer release was lost')
                return original(*args)
            def release_writer():
                threading.Event().wait(0.02)  # Let the main thread enter the blocked context exit.
                observed.append(store.lock is not None and not store.lock.closed)
                release.set()
            with store, patch.object(store, '_atomic', side_effect=atomic):
                publication = executor.submit(store.publish_claude, batch, model='claude-sonnet-5',
                    sample=0, answers=answers(batch), meta={}, snapshot=self.snapshot)
                self.assertTrue(started.wait(2))
                unblock = executor.submit(release_writer)
            unblock.result()
            self.assertEqual(publication.result(), 2)
        self.assertEqual(observed, [True])
        self.assertIsNone(store.lock)

    def test_external_replacement_during_atomic_append_never_seeds_trusted_cache(self):
        import os
        from unittest.mock import patch
        from acceptance.training_teacher.support import answers
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:1]
        path = self.data / 'labels.jsonl'
        external = self.data / 'external'
        external.write_bytes(b'{"incomplete":')
        original = os.replace
        def replace(source, target):
            original(source, target)
            if target == path: original(external, path)
        with self.store() as store, patch('os.replace', side_effect=replace):
            with self.assertRaises(WorkerError):
                store.publish_claude(batch, model='claude-sonnet-5', sample=0,
                    answers=answers(batch), meta={}, snapshot=self.snapshot)
        self.assertEqual(path.read_bytes(), b'{"incomplete":')
