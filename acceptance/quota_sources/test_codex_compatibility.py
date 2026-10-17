# PURPOSE: Keep existing strict Codex source and collection policy through new actual-context API.
# CONTRACT: RT-QUOTA-SOURCES-006 Codex-v005 compatibility; no new credential reader.
from pathlib import Path
from unittest.mock import patch

from acceptance.fresh_quota.support import PolicyCase, observation


class CodexActualCompatibility(PolicyCase):
    def setUp(self):
        super().setUp()
        self.environment = {'HOME': self.temp.name, 'CODEX_HOME': self.config['codex_home']}
        self.argv = (self.config['codex_bin'], 'exec', '--strict-config', '--ignore-user-config', 'fixture prompt')

    def prepare(self):
        return self.policy.prepare_actual('codex-cli', environment=self.environment,
            executable=self.config['codex_bin'], argv=self.argv, cwd=self.temp.name, config=self.config)

    def test_codex_actual_scope_and_existing_collection_remain_compatible(self):
        actual = self.prepare()
        expected = self.policy.auth_scope('codex-cli', environment=self.environment,
            executable=self.config['codex_bin'], argv=self.argv, config=self.config)
        self.assertEqual(actual.public_scope, expected)
        row = observation()
        row['auth_scope'] = expected
        self.collect.return_value = (row,)
        with patch.object(self.policy, 'check', wraps=self.policy.check) as existing:
            value = self.policy.check_actual('codex-cli', 'gpt-6.1-sol', actual=actual, config=self.config)
        self.assertEqual(value['status'], 'available')
        existing.assert_called_once()
        self.collect.assert_called_once()

    def test_codex_actual_known_spent_still_refuses(self):
        actual = self.prepare()
        row = observation(used=100)
        row['auth_scope'] = actual.public_scope
        self.collect.return_value = (row,)
        value = self.policy.check_actual('codex-cli', 'gpt-6.1-sol', actual=actual, config=self.config)
        self.assertEqual(value['status'], 'spent')

    def test_codex_actual_nonstrict_argv_refuses_before_collection(self):
        self.argv = (self.config['codex_bin'], 'exec', 'fixture prompt')
        with self.assertRaises(ValueError):
            self.prepare()
        self.collect.assert_not_called()

    def test_codex_actual_alternate_auth_selector_does_not_gain_native_profile(self):
        self.environment['OPENAI_API_KEY'] = 'SYNTHETIC_PRIVATE_KEY'
        with self.assertRaises(ValueError) as error:
            self.prepare()
        self.assertNotIn('SYNTHETIC_PRIVATE_KEY', str(error.exception))
        self.collect.assert_not_called()
