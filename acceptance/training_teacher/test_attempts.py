# PURPOSE: Pin the sole label writer's reservation, privacy, lock and crash invariants.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T02,T03,T04,T07.
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import uuid
from unittest.mock import patch
from .support import TeacherCase, COMMON, QUESTIONS, SYSTEM, ANSWER, BrokerFixture, answers
from integrations.mcp.schemas import WorkerError


class DurableAttempts(TeacherCase):
    def store(self):
        owner = importlib.import_module('cortados_router.teacher_attempts')
        return owner.LabelAttempts(self.data, validate_answers=COMMON.valid_answers, questions=QUESTIONS)

    def reserve(self, store):
        batch = [r for r in self.snapshot['rows'] if self.snapshot['partitions'][r['id']] == 'train'][:2]
        args = self.owner().codex_arguments(batch, sample=0, system_prompt=SYSTEM,
            request_id=str(uuid.uuid4()), cwd=str(self.state / 'teacher-cwd'))
        return store.reserve(batch, sample=0, system_prompt=SYSTEM, arguments=args, snapshot=self.snapshot,
            config_digest=hashlib.sha256(self.config_path.read_bytes()).hexdigest())

    def terminal(self, store, attempt):
        store.submitted(attempt['attempt_id'])
        broker = BrokerFixture()
        response = broker.submit(attempt['arguments'])
        store.bind_job(attempt['attempt_id'], response['job']['job_id'])
        terminal = broker.result(response['job']['job_id'])
        store.terminal(attempt['attempt_id'], terminal)
        return terminal

    def test_reservation_persists_uuid_arguments_and_private_modes(self):
        with self.store() as store:
            attempt = self.reserve(store)
            manifest = self.data / '.teacher-attempts/manifest.json'
            saved = json.loads(manifest.read_text())
            self.assertEqual(saved['attempts'], [attempt])
            self.assertEqual(uuid.UUID(attempt['request_id']).version, 4)
            self.assertEqual(uuid.UUID(attempt['attempt_id']).version, 4)
            self.assertEqual(attempt['request_id'], attempt['arguments']['request_id'])
            self.assertEqual(attempt['config_digest'], hashlib.sha256(self.config_path.read_bytes()).hexdigest())
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(manifest.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((manifest.parent / 'lock').stat().st_mode), 0o600)

    def test_reserved_slot_cannot_be_reserved_twice_or_mutated_through_returned_dict(self):
        with self.store() as store:
            attempt = self.reserve(store)
            before = (self.data / '.teacher-attempts/manifest.json').read_bytes()
            attempt['row_ids'].append('caller-mutation')
            self.assertEqual((self.data / '.teacher-attempts/manifest.json').read_bytes(), before)
            with self.assertRaises(WorkerError): self.reserve(store)
            self.assertEqual((self.data / '.teacher-attempts/manifest.json').read_bytes(), before)

    def test_second_writer_refuses_while_first_context_owns_lock(self):
        with self.store():
            with self.assertRaises(WorkerError):
                with self.store(): pass
        with self.store(): pass

    def test_private_journal_symlink_and_public_mode_refuse_without_repair(self):
        target = self.data / 'other'; target.mkdir()
        private = self.data / '.teacher-attempts'
        private.symlink_to(target, target_is_directory=True)
        with self.assertRaises(WorkerError):
            with self.store(): pass
        self.assertTrue(private.is_symlink())
        private.unlink(); private.mkdir(mode=0o755)
        with self.assertRaises(WorkerError):
            with self.store(): pass
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o755)

    def test_corrupt_manifest_never_resets_or_loses_identity(self):
        with self.store() as store: self.reserve(store)
        path = self.data / '.teacher-attempts/manifest.json'
        path.write_bytes(b'{"schema":')
        with self.assertRaises(WorkerError):
            with self.store(): pass
        self.assertEqual(path.read_bytes(), b'{"schema":')

    def test_manifest_unknown_field_refuses(self):
        with self.store() as store: self.reserve(store)
        path = self.data / '.teacher-attempts/manifest.json'
        saved = json.loads(path.read_text()); saved['unknown'] = True
        path.write_text(json.dumps(saved))
        with self.assertRaises(WorkerError):
            with self.store(): pass

    def test_labels_symlink_and_truncated_history_never_get_repaired(self):
        labels = self.data / 'labels.jsonl'; target = self.data / 'target'
        target.write_text('do not change')
        labels.symlink_to(target)
        with self.assertRaises(WorkerError):
            with self.store() as store: self.reserve(store)
        self.assertEqual(target.read_text(), 'do not change')
        labels.unlink(); labels.write_bytes(b'{"id":')
        with self.assertRaises(WorkerError):
            with self.store() as store: self.reserve(store)
        self.assertEqual(labels.read_bytes(), b'{"id":')

    def test_published_lines_preserve_prefix_mode_and_truthful_receipt(self):
        labels = self.data / 'labels.jsonl'
        prefix = (json.dumps({'id': 'historical', 'teacher': 'claude-sonnet-5', 'sample': 0, 'ts': 0, 'answers': ANSWER}) + '\n').encode()
        labels.write_bytes(prefix); labels.chmod(0o640)
        with self.store() as store:
            attempt = self.reserve(store); terminal = self.terminal(store, attempt)
            count = store.publish(attempt['attempt_id'], terminal=terminal,
                answers=json.loads(terminal['result']['summary']), snapshot=self.snapshot)
            self.assertEqual(count, 2)
            self.assertEqual(store.publish(attempt['attempt_id'], terminal=terminal,
                answers=json.loads(terminal['result']['summary']), snapshot=self.snapshot), 0)
        self.assertTrue(labels.read_bytes().startswith(prefix))
        self.assertEqual(stat.S_IMODE(labels.stat().st_mode), 0o640)
        for row in self.labels()[1:]:
            receipt = row['teacher_execution']
            self.assertEqual(receipt['attempt_id'], attempt['attempt_id'])
            self.assertEqual(receipt['request_id'], attempt['request_id'])
            self.assertEqual(receipt['job_id'], terminal['job']['job_id'])
            self.assertIsNone(receipt['reported_model'])
            self.assertEqual(receipt['cli_version'], '0.163.0')

    def test_final_partition_or_revision_change_refuses_publication(self):
        with self.store() as store:
            attempt = self.reserve(store); terminal = self.terminal(store, attempt)
            changed = copy.deepcopy(self.snapshot)
            changed['partitions'][attempt['row_ids'][0]] = 'valid'
            with self.assertRaises(WorkerError):
                store.publish(attempt['attempt_id'], terminal=terminal,
                    answers=json.loads(terminal['result']['summary']), snapshot=changed)
        self.assertEqual(self.labels(), [])

    def test_conflicting_terminal_identity_refuses_publication(self):
        with self.store() as store:
            attempt = self.reserve(store); terminal = self.terminal(store, attempt)
            changed = copy.deepcopy(terminal); changed['job']['job_id'] = str(uuid.uuid4())
            with self.assertRaises(WorkerError):
                store.publish(attempt['attempt_id'], terminal=changed,
                    answers=json.loads(terminal['result']['summary']), snapshot=self.snapshot)
        self.assertEqual(self.labels(), [])

    def test_crash_after_labels_replace_reconciles_without_duplicate_lines(self):
        original_replace = os.replace
        crashed = False
        with self.store() as store:
            attempt = self.reserve(store); terminal = self.terminal(store, attempt)
            def replace(source, destination, *args, **kwargs):
                nonlocal crashed
                original_replace(source, destination, *args, **kwargs)
                if Path(destination).name == 'labels.jsonl' and not crashed:
                    crashed = True
                    raise OSError('Synthetic crash after atomic labels replace')
            with patch.object(os, 'replace', side_effect=replace):
                with self.assertRaises((OSError, WorkerError)):
                    store.publish(attempt['attempt_id'], terminal=terminal,
                        answers=json.loads(terminal['result']['summary']), snapshot=self.snapshot)
        self.assertTrue(crashed)
        before = (self.data / 'labels.jsonl').read_bytes()
        with self.store() as store:
            store.publish(attempt['attempt_id'], terminal=terminal,
                answers=json.loads(terminal['result']['summary']), snapshot=self.snapshot)
        self.assertEqual((self.data / 'labels.jsonl').read_bytes(), before)
        self.assertEqual(len(self.labels()), 2)

    def test_reservation_replace_failure_does_not_expose_sendable_attempt(self):
        with self.store() as store:
            with patch.object(os, 'replace', side_effect=OSError('Synthetic storage failure')):
                with self.assertRaises((OSError, WorkerError)): self.reserve(store)
        with self.store() as store:
            self.assertEqual(store.pending(), ())

    def test_failed_terminal_cannot_publish_supplied_answers(self):
        with self.store() as store:
            attempt = self.reserve(store)
            store.submitted(attempt['attempt_id'])
            broker = BrokerFixture(); broker.state = 'failed'
            receipt = broker.submit(attempt['arguments'])
            store.bind_job(attempt['attempt_id'], receipt['job']['job_id'])
            terminal = broker.result(receipt['job']['job_id'])
            store.terminal(attempt['attempt_id'], terminal)
            result = store.publish(attempt['attempt_id'], terminal=terminal,
                answers=[dict(i=0, **ANSWER)], snapshot=self.snapshot)
            self.assertEqual(result, 0)
        self.assertEqual(self.labels(), [])

    def test_data_directory_identity_change_refuses_reconciliation(self):
        with self.store() as store:
            self.reserve(store)
        journal = json.loads((self.data / '.teacher-attempts/manifest.json').read_text())
        journal['data_identity']['inode'] += 1
        (self.data / '.teacher-attempts/manifest.json').write_text(json.dumps(journal))
        with self.assertRaises(WorkerError):
            with self.store(): pass
