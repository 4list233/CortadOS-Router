# PURPOSE: Real native credential/config interpretation with only external Dashboard RPC substituted.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS01/02; explicit root-approved isolated Node preload instrumentation.
# CEILING: Private synthetic home; no network, provider main, login, credential refresh or settings writes.
import base64
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from integrations.mcp.native_process import ProcessTracker, process_identity

FIXTURES = Path(__file__).with_name('fixtures')
ROOT = Path(__file__).resolve().parents[2]
NATIVE = Path('/Users/velox/.local/share/cursor-agent/versions/2026.10.16-e3cb207')


class NativeReaderBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quota-native-reader-exam-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.cwd = self.home / 'workspace'
        self.cwd.mkdir()
        self.store = self.home / '.cursor'
        self.store.mkdir()
        for name in ('auth.json', 'cli-config.json'):
            shutil.copyfile(FIXTURES / ('synthetic-' + name), self.store / name)
        capture = json.loads((FIXTURES / 'cursor-native-rpc-response.json').read_text())
        (self.home / 'quota.json').write_text(json.dumps(capture['response']))
        self.observed_at = json.loads((FIXTURES / 'cursor-projection-reference.json').read_text())['observed_at']
        (self.home / 'observed-at.json').write_text(json.dumps(self.observed_at))
        self.config = {'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'native-reader-exam',
            'state_dir': str(self.home), 'codex_home': str(self.home),
            'codex_bin': str(ROOT / 'acceptance/mcp_worker/fake_codex.py'),
            'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
            'limits': {'max_concurrent_jobs': 2, 'max_timeout_s': 30, 'termination_grace_s': 1,
                       'startup_timeout_s': 10, 'max_handoff_bytes': 65536, 'max_output_bytes': 65536,
                       'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        self.request = {'provider': 'cursor-cli', 'environment': {'HOME': str(self.home),
            'PATH': '/usr/bin:/bin', 'AGENT_CLI_CREDENTIAL_STORE': 'file'},
            'executable': str(NATIVE / 'cursor-agent'), 'cwd': str(self.cwd),
            'argv': [str(NATIVE / 'cursor-agent'), '-p', '--output-format', 'stream-json', '--trust', '--model', 'auto', 'fixture']}

    def read(self):
        (self.home / 'config.json').write_text(json.dumps(self.config))
        (self.home / 'request.json').write_text(json.dumps(self.request))
        before = {p.relative_to(self.store):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        process = subprocess.Popen([sys.executable, '-B', str(FIXTURES / 'native_reader_driver.py'), str(ROOT), str(self.home)],
            cwd=self.cwd, env=dict(os.environ, HOME=str(self.home), CORTADOS_HOME=str(self.home), CORTADOS_REPO=str(self.home)),
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        identity = process_identity(process.pid)
        self.assertIsNotNone(identity)
        tracker = ProcessTracker(identity)
        try:
            # Test-only30s permits native module loading plus configured10s read and cleanup on slow hosts.
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                tracker.sample()
                time.sleep(.02)
            self.assertIsNotNone(process.poll(), 'Native reader exceeded bounded fixture deadline')
            stdout, stderr = process.communicate(timeout=1)
            self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
            self.assertNotIn(b'fixture-refresh-token', stdout + stderr)
            self.assertNotIn(b'PRIVATE_SENTINEL', stdout + stderr)
            tracker.sample()
            self.assertTrue(tracker.contained(), 'Native reader left an executable helper alive')
            result = json.loads((self.home / 'read-result.json').read_text())
            logfile = self.home / 'native-calls.jsonl'
            calls = [json.loads(line)['kind'] for line in logfile.read_text().splitlines()] if logfile.exists() else []
            self.assertNotIn('NETWORK_FORBIDDEN', calls)
            after = {p.relative_to(self.store):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
            self.assertEqual(before, after, 'Native read wrote credential/config/trust material')
            return result, calls
        finally:
            tracker.signal_groups(signal.SIGTERM)
            tracker.signal_groups(signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_native_credential_and_full_config_owners_reach_only_stubbed_dashboard(self):
        result, calls = self.read()
        self.assertTrue(result['ok'], result)
        self.assertEqual(calls, ['dashboard-quota'])
        self.assertEqual(result['valid_until_epoch'], 4102444800 - 300)
        windows = {row['pool']:row for row in result['record']['windows']}
        self.assertEqual(windows['other-models']['used_pct'], 33.22222222222222)
        self.assertEqual(windows['cursor-models']['reset_epoch'], 1794349955)

    def test_swallowed_native_config_repair_write_refuses_before_quota_call(self):
        (self.store / 'cli-config.json').write_text('{ PRIVATE_SENTINEL malformed synthetic config')
        result, calls = self.read()
        self.assertFalse(result['ok'], 'Native fallback swallowed a blocked config repair and authorized another configuration')
        self.assertEqual(calls, [], 'Blocked repair must refuse before reading quota using fallback defaults')

    def test_missing_settings_directory_cannot_be_created_by_native_reader(self):
        missing = self.home / 'absent-settings'
        self.request['environment']['CURSOR_CONFIG_DIR'] = str(missing)
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])
        self.assertFalse(missing.exists())

    def test_expiring_native_token_is_not_refreshed_and_does_not_reach_dashboard(self):
        path = self.store / 'auth.json'
        auth = json.loads(path.read_text())
        parts = auth['accessToken'].split('.')
        claim = {'sub': 'fixture-account', 'exp': int(self.observed_at) + 299}
        parts[1] = base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip('=')
        auth['accessToken'] = '.'.join(parts)
        path.write_text(json.dumps(auth))
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_stored_api_key_refuses_without_login_or_api_billing(self):
        path = self.store / 'auth.json'
        auth = json.loads(path.read_text())
        auth['apiKey'] = 'synthetic-api-key'
        path.write_text(json.dumps(auth))
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_project_override_is_unproven_not_silently_skipped(self):
        project = self.cwd / '.cursor'
        project.mkdir()
        (project / 'cli.json').write_text(json.dumps({'permissions': {'allow': [], 'deny': []}}))
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_active_direnv_is_not_disabled_only_for_probe(self):
        (self.cwd / '.envrc').write_text('export CURSOR_API_KEY=synthetic-switch')
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])

    def test_native_exact_300_seconds_retains_current_token_without_refresh(self):
        path = self.store / 'auth.json'
        auth = json.loads(path.read_text())
        parts = auth['accessToken'].split('.')
        claim = {'sub': 'fixture-account', 'exp': int(self.observed_at) + 300}
        parts[1] = base64.urlsafe_b64encode(json.dumps(claim).encode()).decode().rstrip('=')
        auth['accessToken'] = '.'.join(parts)
        path.write_text(json.dumps(auth))
        result, calls = self.read()
        self.assertTrue(result['ok'], result)
        self.assertEqual(calls, ['dashboard-quota'])
        self.assertEqual(result['valid_until_epoch'], int(self.observed_at))
        # read_source proves native semantics; final check still needs startup allowance and refuses.

    def test_malformed_native_expiry_is_unknown_without_refresh_or_quota(self):
        path = self.store / 'auth.json'
        auth = json.loads(path.read_text())
        parts = auth['accessToken'].split('.')
        parts[1] = base64.urlsafe_b64encode(json.dumps({'exp': '4102444800'}).encode()).decode().rstrip('=')
        auth['accessToken'] = '.'.join(parts)
        path.write_text(json.dumps(auth))
        result, calls = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(calls, [])
