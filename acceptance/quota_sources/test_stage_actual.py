# PURPOSE: Pin exact private source capture at staged physical transport boundaries.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS07; old routing assertions remain in 005.
# DEPENDS: Frozen staged source and additive fake installation, never real transports.
import importlib
import json
import unittest
from threading import Event
from unittest.mock import Mock, patch

from acceptance.fresh_quota.stage_support import StageCase, load


class ActualPtyBoundary(StageCase):
    def setUp(self):
        super().setUp()
        import sys
        pty = self.root / 'plugins/cortados-pty'
        sys.path.insert(0, str(pty))
        load('model_routing', pty / 'model_routing.py')
        load('usage', pty / 'usage.py')
        self.runner = load('quota_sources_staged_runner', pty / 'runner.py')
        self.spawn = self.enterContext(patch.object(self.runner.Job, 'spawn', autospec=True))
        self.enterContext(patch.object(self.runner.threading.Thread, 'start'))
        self.enterContext(patch.object(self.runner, 'build_argv', return_value=['fixture-native', '--model', 'auto']))
        self.enterContext(patch.object(self.runner, 'build_brain_argv', return_value=['fixture-native', '--model', 'auto']))

    def test_metadata_only_job_cannot_authorize_continuation(self):
        job = object.__new__(self.runner.Job)
        job.master_fd, job.done = 123, Event()
        job.meta = {'runtime': 'codex', 'model': 'gpt-6.1-sol', 'owner': 'exam',
                    'quota_stage': 'dispatch', 'quota_auth_scope': {'kind': 'cli_store', 'lane': 'codex', 'scope_id': 'b' * 64}}
        job.save = Mock()
        with patch.object(self.runner.os, 'write') as write:
            with self.assertRaisesRegex((ValueError, RuntimeError), 'quota_unknown'):
                job.send('fixture continuation')
            write.assert_not_called()

    def test_final_runner_gate_uses_new_owner_and_keeps_private_context_out_of_metadata(self):
        owner = importlib.import_module('cortados_router.quota_admission')
        with patch.object(owner, 'prepare_actual', wraps=owner.prepare_actual) as prepare, \
             patch.object(owner, 'check_actual', wraps=owner.check_actual) as check:
            job = self.runner.Runner().run('codex', 'fixture prompt', cwd=str(self.root), model='gpt-6.1-sol')
        prepare.assert_called_once()
        check.assert_called_once()
        self.spawn.assert_called_once()
        self.assertIn('cwd', prepare.call_args.kwargs)
        self.assertIn('environment', prepare.call_args.kwargs)
        # Runner return shape is not prescribed: inspect the physical Job receiver when necessary.
        actual = check.call_args.kwargs['actual']
        self.assertIsInstance(actual, owner.ActualSource)
        self.assertIs(job._quota_actual_source, actual)
        self.assertEqual(job.meta['quota_auth_scope'], actual.public_scope)
        self.assertNotIn('_quota_actual_source', json.dumps(job.meta))
        self.assertEqual(prepare.call_args.kwargs['argv'], tuple(self.spawn.call_args.args[1]))
        self.assertEqual(prepare.call_args.kwargs['environment'], self.spawn.call_args.args[3])
        for path in self.root.rglob('meta.json'):
            metadata = json.loads(path.read_text())
            self.assertNotIn('_quota_actual_source', metadata)
            self.assertNotIn('environment', metadata)

    def test_installed_owner_missing_new_api_never_falls_back_to_old_check(self):
        owner = importlib.import_module('cortados_router.quota_admission')
        with patch.object(owner, 'prepare_actual', None), patch.object(owner, 'check', wraps=owner.check) as old:
            with self.assertRaises((ValueError, RuntimeError, TypeError, AttributeError)):
                self.runner.Runner().run('codex', 'fixture prompt', cwd=str(self.root), model='gpt-6.1-sol')
            self.spawn.assert_not_called()
            old.assert_not_called()

    def test_continuation_keeps_original_private_context_when_global_environment_changes(self):
        owner = importlib.import_module('cortados_router.quota_admission')
        job = object.__new__(self.runner.Job)
        job.master_fd, job.done = 123, Event()
        job.meta = {'runtime': 'codex', 'model': 'gpt-6.1-sol', 'owner': 'exam', 'quota_stage': 'dispatch'}
        job.save = Mock()
        self.bind_job_context(job)
        original = job._quota_actual_source
        with patch.dict('os.environ', {'HOME': '/different/source', 'CODEX_AUTH_TOKEN': 'synthetic-new-token'}), \
             patch.object(owner, 'prepare_actual', wraps=owner.prepare_actual) as prepare, \
             patch.object(owner, 'check_actual', wraps=owner.check_actual) as check, \
             patch.object(self.runner.os, 'write') as write:
            job.send('fixture continuation')
        prepare.assert_not_called()
        check.assert_called_once()
        self.assertIs(check.call_args.kwargs['actual'], original)
        write.assert_called_once_with(123, b'fixture continuation\r')


class ActualTeacherBoundary(unittest.TestCase):
    def setUp(self):
        from acceptance.fresh_quota.test_teacher import TeacherDispatch
        self.teacher = TeacherDispatch(methodName='test_missing_teacher_executable_refuses_before_process')
        self.teacher.setUp()
        self.addCleanup(self.teacher.doCleanups)

    def test_both_learning_modes_bind_exact_teacher_transport_to_new_owner(self):
        import os
        from types import SimpleNamespace
        for enabled in (False, True):
            with self.subTest(learning=enabled):
                module = self.teacher.module(enabled)
                owner = importlib.import_module('cortados_router.quota_admission')
                with patch.object(owner, 'prepare_actual', wraps=owner.prepare_actual) as prepare, \
                     patch.object(owner, 'check_actual', wraps=owner.check_actual) as check, \
                     patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps({'result': '[]'}))) as run:
                    module.call_teacher('claude-sonnet-5', self.teacher.batch)
                prepare.assert_called_once()
                check.assert_called_once()
                run.assert_called_once()
                self.assertEqual(prepare.call_args.kwargs['argv'], tuple(run.call_args.args[0]))
                self.assertEqual(prepare.call_args.kwargs['environment'], run.call_args.kwargs['env'])
                self.assertEqual(prepare.call_args.kwargs['cwd'], run.call_args.kwargs.get('cwd', os.getcwd()))
                self.assertIsInstance(check.call_args.kwargs['actual'], owner.ActualSource)
