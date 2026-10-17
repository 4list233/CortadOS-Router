# PURPOSE: Real Cursor profile capture against frozen installed native bytes and synthetic stores.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS01/02; capture is pure files/environment, no quota query.
# DEPENDS: Reviewed native fingerprint manifest and offline-native-validated synthetic store schema.
import copy
import hashlib
import json
import os
from pathlib import Path
import pickle
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from integrations.mcp.schemas import WorkerError

FIXTURES = Path(__file__).with_name('fixtures')
NATIVE = Path('/Users/velox/.local/share/cursor-agent/versions/2026.10.16-e3cb207')


class CursorNativeProfile(unittest.TestCase):
    def setUp(self):
        from cortados_router import quota_sources
        self.native = quota_sources
        self.temp = tempfile.TemporaryDirectory(prefix='cursor-native-profile-exam-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.cwd = self.home / 'workspace'
        self.cwd.mkdir()
        self.store = self.home / '.cursor'
        self.store.mkdir()
        for name in ('auth.json', 'cli-config.json'):
            shutil.copyfile(FIXTURES / ('synthetic-' + name), self.store / name)
        self.environment = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'AGENT_CLI_CREDENTIAL_STORE': 'file'}
        self.executable = str(NATIVE / 'cursor-agent')
        self.argv = (self.executable, '-p', '--output-format', 'stream-json', '--trust', '--model', 'auto', 'private prompt')
        self.config = {'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'native-profile-exam',
            'state_dir': str(self.home), 'codex_home': str(self.home),
            'codex_bin': str(Path(__file__).resolve().parents[2] / 'acceptance/mcp_worker/fake_codex.py'),
            'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
            'limits': {'max_concurrent_jobs': 2, 'max_timeout_s': 30, 'termination_grace_s': 1,
                       'startup_timeout_s': 3, 'max_handoff_bytes': 65536, 'max_output_bytes': 65536,
                       'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        self.enterContext(patch.object(subprocess, 'Popen', side_effect=AssertionError('pure capture cannot spawn')))
        self.enterContext(patch.object(socket, 'create_connection', side_effect=AssertionError('pure capture cannot query')))

    def capture(self, provider='cursor-cli'):
        return self.native.capture_source(provider, environment=self.environment, executable=self.executable,
             argv=self.argv, cwd=str(self.cwd), config=self.config)

    def refused(self):
        with self.assertRaises((ValueError, WorkerError)) as error:
            self.capture()
        self.assertNotIn('SYNTHETIC_SECRET_VALUE', str(error.exception))

    def test_known_native_chain_and_synthetic_file_source_capture_without_io_or_writes(self):
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = self.capture()
        self.assertEqual(result.profile, 'cursor-2026.10.16-e3cb207-file-v1')
        self.assertEqual(result.lane, 'cursor')
        self.assertIsInstance(result.material, bytes)
        self.assertIsInstance(result.files, tuple)
        manifest = json.loads((FIXTURES / 'cursor-native-manifest.json').read_text())
        files = {row[0]: row[-1] for row in result.files}
        for relative, expected in manifest['files'].items():
            self.assertEqual(files.get(str((NATIVE / relative).resolve())), expected['sha256'], relative)
        after = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_private_capture_redacts_and_rejects_serialization(self):
        result = self.capture()
        self.assertNotIn(str(self.home), repr(result))
        self.assertNotIn('fixture-refresh-token', repr(result))
        with self.assertRaises((TypeError, ValueError)):
            pickle.dumps(result)
        with self.assertRaises(TypeError):
            json.dumps(result)

    def test_prompt_resume_and_force_do_not_change_source_identity(self):
        first = self.capture()
        self.argv = (*self.argv[:-1], '--resume', 'private-resume-id', '--force', 'different private prompt')
        second = self.capture()
        self.assertEqual(first.material, second.material)
        self.assertEqual(first.source_argv, second.source_argv)
        self.assertNotIn('private', json.dumps(second.source_argv))

    def test_auth_endpoint_and_interpreter_overrides_refuse_without_logging_values(self):
        for key in ('CURSOR_API_KEY', 'CURSOR_AUTH_TOKEN', 'CURSOR_API_ENDPOINT', 'CURSOR_API_URL',
                    'NODE_OPTIONS', 'NODE_PATH', 'BASH_ENV', 'ENV', 'DYLD_INSERT_LIBRARIES'):
            with self.subTest(selector=key):
                self.environment[key] = 'SYNTHETIC_SECRET_VALUE'
                try:
                    self.refused()
                finally:
                    del self.environment[key]

    def test_unknown_duplicate_or_source_selecting_argv_refuses(self):
        original = self.argv
        for extra in (('--api-key', 'SYNTHETIC_SECRET_VALUE'), ('--endpoint', 'https://invalid.invalid'),
                      ('--header', 'x-fixture:secret'), ('--model', 'another-model'), ('--unknown',),
                      ('--output-format', 'json'), ('--auth-token', 'SYNTHETIC_SECRET_VALUE')):
            with self.subTest(flag=extra[0]):
                self.argv = (*original[:-1], *extra, original[-1])
                self.refused()
        self.argv = original

    def test_keychain_or_unknown_credential_backend_refuses(self):
        for value in ('keychain', 'auto', 'unreviewed'):
            with self.subTest(backend=value):
                self.environment['AGENT_CLI_CREDENTIAL_STORE'] = value
                self.refused()

    def test_token_rotation_changes_private_capture_and_never_public_repr(self):
        first = self.capture()
        path = self.store / 'auth.json'
        value = json.loads(path.read_text())
        value['accessToken'] += '-rotated'
        path.write_text(json.dumps(value))
        second = self.capture()
        self.assertNotEqual(first.material, second.material)
        self.assertNotEqual(first.files, second.files)
        self.assertNotIn('rotated', repr(second))

    def test_settings_rotation_changes_private_capture(self):
        first = self.capture()
        path = self.store / 'cli-config.json'
        value = json.loads(path.read_text())
        value['authInfo']['activeTeamId'] = 303
        path.write_text(json.dumps(value))
        second = self.capture()
        self.assertNotEqual(first.material, second.material)
        self.assertNotEqual(first.files, second.files)

    def test_unreviewed_executable_chain_refuses_without_system_node_fallback(self):
        binary = self.home / 'cursor-agent'
        binary.write_text('#!/bin/sh\nexit99\n')
        binary.chmod(0o700)
        self.executable = str(binary)
        self.argv = (self.executable, *self.argv[1:])
        self.refused()

    def test_new_project_config_or_envrc_is_not_invisible_to_capture(self):
        first = self.capture()
        for relative in ('.cursor/cli.json', '.envrc'):
            path = self.cwd / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text('{}' if relative.endswith('json') else 'export CURSOR_API_KEY=synthetic')
            try:
                try:
                    changed = self.capture()
                except (ValueError, WorkerError):
                    pass
                else:
                    self.assertNotEqual((first.material, first.files), (changed.material, changed.files))
            finally:
                path.unlink()
        restored = self.capture()
        # Directory generations may conservatively differ; the absent source must at least capture again.
        self.assertEqual(restored.profile, first.profile)

    def test_symlink_retarget_changes_source_generation_even_identical_content(self):
        original = self.store / 'auth.json'
        data = original.read_bytes()
        original.unlink()
        one, two = self.home / 'auth-one.json', self.home / 'auth-two.json'
        one.write_bytes(data)
        two.write_bytes(data)
        original.symlink_to(one)
        try:
            first = self.capture()
        except (ValueError, WorkerError):
            return  # A profile may conservatively refuse symlink credential authorities entirely.
        original.unlink()
        original.symlink_to(two)
        try:
            second = self.capture()
        except (ValueError, WorkerError):
            return
        self.assertNotEqual(first.files, second.files)

    def test_claude_and_antigravity_have_no_enabled_native_profile_in_this_slice(self):
        for provider in ('claude-cli', 'agy-cli'):
            with self.subTest(provider=provider):
                with self.assertRaises((ValueError, WorkerError)):
                    self.capture(provider)
