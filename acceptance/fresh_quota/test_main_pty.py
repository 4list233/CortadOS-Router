# PURPOSE: Pin actual staged main and PTY send boundaries under both routing modes.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ05/06/10.
# DEPENDS: StageCase origin-checked fake installation; full staged source modules.
# CEILING: No real provider creation, PTY process or prompt bytes.
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from .stage_support import StageCase, load


class MainDispatch(StageCase):
    def setUp(self):
        super().setUp()
        self.cli, self.profiles = self.provider_plugin()
        self.plugin = load('fresh_quota_staged_plugin', self.root / 'plugins/cortados-router/__init__.py')
        self.plugin._pending['exam-turn'] = {'execution_route': self.receipt}

    def test_unknown_and_spent_stop_main_next_call_in_both_modes(self):
        for mode in (None, 'cortados-router'):
            for status in ('unknown', 'spent'):
                with self.subTest(mode=mode, status=status):
                    self.router_mode = mode
                    self.write_quota(status)
                    next_call = Mock(return_value='sent')
                    result = self.plugin._execution_admission({}, next_call, turn_id='exam-turn',
                        api_request_id='exam-request', provider='codex-cli', model='gpt-6.1-sol',
                        base_url='acp://cortados-codex', api_mode='chat_completions')
                    self.assertEqual(getattr(result, 'reason', None), 'quota_' + status)
                    next_call.assert_not_called()

    def test_known_quota_reaches_main_next_call_exactly_once_mode_off(self):
        next_call = Mock(return_value='sent')
        result = self.plugin._execution_admission({}, next_call, provider='codex-cli', model='gpt-6.1-sol',
                        base_url='acp://cortados-codex', api_mode='chat_completions')
        self.assertEqual(result, 'sent')
        next_call.assert_called_once()
        self.assertEqual([(r[0], r[1], r[2]) for r in self.calls()],
                         [('source_preflight', 'codex-cli', 'gpt-6.1-sol')])

    def test_matching_provider_label_without_registered_source_proof_refuses(self):
        self.profiles['codex-cli'] = NS(_runtime='codex', auth_type='external_process', api_mode='chat_completions')
        next_call = Mock()
        result = self.plugin._execution_admission({}, next_call, provider='codex-cli', model='gpt-6.1-sol',
                                                 base_url='acp://cortados-codex', api_mode='chat_completions')
        self.assertEqual(getattr(result, 'reason', None), 'quota_unknown')
        next_call.assert_not_called()

    def test_registered_cli_profile_with_wrong_actual_base_url_refuses(self):
        next_call = Mock()
        result = self.plugin._execution_admission({}, next_call, provider='codex-cli', model='gpt-6.1-sol',
                                                 base_url='https://other.invalid', api_mode='chat_completions')
        self.assertEqual(getattr(result, 'reason', None), 'quota_unknown')
        next_call.assert_not_called()


class PtyDispatch(StageCase):
    def setUp(self):
        super().setUp()
        pty = self.root / 'plugins/cortados-pty'
        sys.path.insert(0, str(pty))
        self.model_routing = load('model_routing', pty / 'model_routing.py')
        load('usage', pty / 'usage.py')
        self.runner = load('fresh_quota_staged_runner', pty / 'runner.py')
        self.spawn = self.enterContext(patch.object(self.runner.Job, 'spawn'))
        self.enterContext(patch.object(self.runner, 'build_argv', return_value=['fixture-native']))
        self.enterContext(patch.object(self.runner, 'build_brain_argv', return_value=['fixture-native']))
        self.enterContext(patch.object(self.runner.threading.Thread, 'start'))

    def test_unknown_and_spent_prevent_runner_spawn_router_off(self):
        for status in ('unknown', 'spent'):
            with self.subTest(status=status):
                self.write_quota(status)
                with self.assertRaisesRegex((ValueError, RuntimeError), 'quota_' + status):
                    self.runner.Runner().run('codex', 'fixture prompt', cwd=str(self.root), model='gpt-6.1-sol')
                self.spawn.assert_not_called()

    def test_reused_receipt_cannot_authorize_spent_brain_spawn(self):
        self.router_mode = 'cortados-router'
        self.write_quota('spent')
        with self.assertRaisesRegex((ValueError, RuntimeError), 'quota_spent'):
            self.runner.Runner().run_brain('codex', 'system', 'prompt', model='gpt-6.1-sol')
        self.spawn.assert_not_called()

    def test_quota_changed_after_route_still_prevents_physical_spawn(self):
        self.write_quota('available')
        def argv(*args, **kwargs):
            self.write_quota('spent')
            return ['fixture-native']
        with patch.object(self.runner, 'build_argv', side_effect=argv):
            with self.assertRaisesRegex((ValueError, RuntimeError), 'quota_spent'):
                self.runner.Runner().run('codex', 'fixture prompt', cwd=str(self.root), model='gpt-6.1-sol')
        self.spawn.assert_not_called()

    def test_unknown_and_spent_prevent_continuation_bytes_even_mode_off(self):
        job = object.__new__(self.runner.Job)
        job.master_fd, job.done = 123, Event()
        job.meta = {'runtime': 'codex', 'model': 'gpt-6.1-sol', 'owner': 'exam',
                    'quota_stage': 'dispatch', 'quota_auth_scope': {'kind': 'cli_store', 'lane': 'codex', 'scope_id': 'b' * 64}}
        job.save = Mock()
        self.bind_job_context(job)
        for status in ('unknown', 'spent'):
            with self.subTest(status=status):
                self.write_quota(status)
                with patch.object(self.runner.os, 'write') as write:
                    with self.assertRaisesRegex((ValueError, RuntimeError), 'quota_' + status):
                        job.send('fixture prompt')
                    write.assert_not_called()

    def test_available_continuation_checks_identity_and_writes_once(self):
        job = object.__new__(self.runner.Job)
        job.master_fd, job.done = 123, Event()
        job.meta = {'runtime': 'codex', 'model': 'gpt-6.1-sol', 'owner': 'exam',
                    'quota_stage': 'dispatch', 'quota_auth_scope': {'kind': 'cli_store', 'lane': 'codex', 'scope_id': 'b' * 64}}
        job.save = Mock()
        self.bind_job_context(job)
        with patch.object(self.runner.os, 'write') as write:
            job.send('fixture prompt')
        write.assert_called_once_with(123, b'fixture prompt\r')
        self.assertEqual([(r[0], r[1], r[2]) for r in self.calls()],
                         [('dispatch', 'codex-cli', 'gpt-6.1-sol')])
        self.assertEqual(self.calls()[0][3], job.meta['quota_auth_scope'])
