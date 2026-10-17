# PURPOSE: Pin nonsecret source provenance and refusal of unproved auth alternatives.
# CONTRACT: RT-FRESH-QUOTA-005 auth-scope amendment; identity is never inferred from a model.
# DEPENDS: Sole public auth_scope canonicalizer; private paths and inert argv.
# CEILING: No token content, credentials, source-home files or actual native calls.
import hashlib
import json
import os
from pathlib import Path

from .support import PolicyCase


class AuthScope(PolicyCase):
    def source_scope(self, provider='codex-cli', *, env=None, argv=None, executable=None):
        environment = {'HOME': self.temp.name, 'CODEX_HOME': self.config['codex_home']}
        environment.update(env or {})
        if argv is None:
            argv = (self.config['codex_bin'], 'exec', '--strict-config', '--ignore-user-config')
        result = self.policy.auth_scope(provider, environment=environment,
            executable=executable or self.config['codex_bin'], argv=argv, config=self.config)
        self.assertEqual(set(result), {'kind', 'lane', 'scope_id'})
        return result

    def test_identical_cli_source_is_stable_and_contains_no_raw_paths(self):
        a, b = self.source_scope(), self.source_scope()
        self.assertEqual(a, b)
        self.assertEqual(a['kind'], 'cli_store')
        self.assertEqual(a['lane'], 'codex')
        self.assertRegex(a['scope_id'], r'^[0-9a-f]{64}$')
        self.assertNotIn(self.temp.name, str(a))

    def test_effective_home_or_codex_home_changes_scope(self):
        initial = self.source_scope()
        for field in ('HOME', 'CODEX_HOME', 'XDG_CONFIG_HOME'):
            with self.subTest(field=field):
                other = self.source_scope(env={field: str(Path(self.temp.name) / ('other-' + field))})
                self.assertNotEqual(initial, other)

    def test_resolved_binary_identity_is_bound(self):
        binary = Path(self.temp.name) / 'other-codex'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        self.assertNotEqual(self.source_scope(), self.source_scope(executable=str(binary)))

    def test_nonstrict_codex_config_is_unproven(self):
        result = self.source_scope(argv=())
        self.assertEqual(result['kind'], 'unproven')
        self.assertIsNone(result['scope_id'])

    def test_native_oauth_never_borrows_native_cli_scope(self):
        result = self.source_scope(provider='openai-codex')
        self.assertEqual(result['kind'], 'unproven')
        self.assertIsNone(result['scope_id'])

    def test_codex_alternate_auth_or_base_url_is_unproven_without_secret_echo(self):
        for variable in ('OPENAI_API_KEY', 'OPENAI_KEY', 'OPENAI_BASE_URL', 'CODEX_API_KEY', 'CODEX_AUTH_TOKEN'):
            with self.subTest(variable=variable):
                result = self.source_scope(env={variable: 'DO-NOT-COPY-FIXTURE'})
                self.assertEqual(result['kind'], 'unproven')
                self.assertIsNone(result['scope_id'])
                self.assertNotIn('DO-NOT-COPY-FIXTURE', str(result))

    def test_claude_injected_token_or_key_cannot_claim_file_store_scope(self):
        for variable in ('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL'):
            with self.subTest(variable=variable):
                result = self.source_scope(provider='claude-cli', env={variable: 'DO-NOT-COPY-FIXTURE'})
                self.assertEqual(result['kind'], 'unproven')
                self.assertIsNone(result['scope_id'])

    def test_cursor_override_store_cannot_match_observed_file_store(self):
        file_scope = self.source_scope(provider='cursor-cli', env={'AGENT_CLI_CREDENTIAL_STORE': 'file'})
        other = self.source_scope(provider='cursor-cli', env={'AGENT_CLI_CREDENTIAL_STORE': 'keychain'})
        self.assertNotEqual(file_scope, other)

    def test_scope_hash_uses_exact_nonsecret_descriptor(self):
        home = str(Path(self.temp.name).resolve())
        descriptor = {'lane': 'codex', 'executable': str(Path(self.config['codex_bin']).resolve()),
                      'home': home, 'config_home': str(Path(home) / '.config'),
                      'store': str(Path(self.config['codex_home']).resolve()), 'store_mode': 'default',
                      'uid': os.getuid()}
        expected = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(',', ':'),
                                            ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(self.source_scope()['scope_id'], expected)

    def test_local_table_name_cannot_create_transport_scope(self):
        for provider in ('custom', 'ollama'):
            self.assertEqual(self.source_scope(provider)['kind'], 'unproven')

    def test_metadata_app_server_argv_is_not_public_dispatch_proof(self):
        result = self.source_scope(argv=(self.config['codex_bin'], 'app-server', '--listen', 'stdio://'))
        self.assertEqual(result['kind'], 'unproven')
