# PURPOSE: Pin collector containment and validation failures beyond protected exams.
# CONTRACT: RT-FRESH-QUOTA-005; private process fixtures, never provider calls.
# DEPENDS: Installed observation/admission owners and existing process identities.
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cortados_router import quota_admission as policy, quota_observations as collector
from integrations.mcp.native_process import process_identity


class ObservationContainment(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fresh-quota-builder-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'schema': 'cortados.mcp-worker.config.v1', 'client_id': 'builder',
                       'state_dir': str(self.root), 'codex_home': str(self.root), 'codex_bin': sys.executable,
                       'model_allowlist': ['gpt-6.1-sol'], 'allow_network': False, 'connector_registry': {},
                       'limits': {'startup_timeout_s': 1, 'termination_grace_s': 1, 'max_output_bytes': 65536,
                                  'metadata_max_age_s': 30, 'process_poll_ms': 20}}
        self.scope = {'kind': 'unproven', 'lane': 'codex', 'scope_id': None}

    def run_child(self, script, *, config=None):
        fixture = self.root / 'child.py'
        fixture.write_text(script)
        real_popen = subprocess.Popen
        def launch(argv, **kwargs):
            return real_popen([sys.executable, '-B', str(fixture)], **kwargs)
        start = time.monotonic()
        with patch.object(subprocess, 'Popen', side_effect=launch):
            result = collector._probe('codex', config or self.config, self.scope)
        # 1s source deadline + 1s configured grace + 3s scheduler allowance.
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result['error']['code'], 'quota_unknown')
        return result

    def test_timeout_contains_detached_session_descendant(self):
        pid_file = self.root / 'detached.pid'
        script = ('import subprocess, sys, time\n'
                  'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)\n'
                  f'open({str(pid_file)!r}, "w").write(str(child.pid))\n'
                  'time.sleep(60)\n')
        self.run_child(script)
        pid = int(pid_file.read_text())
        try:
            self.assertIsNone(process_identity(pid), 'quota collector retained detached runnable descendant')
        finally:
            if process_identity(pid) is not None:
                os.kill(pid, 9)

    def test_nonreading_child_cannot_block_config_stdin_past_deadline(self):
        config = copy.deepcopy(self.config)
        # Exceed a pipe's capacity without depending on its host-specific byte count.
        config['client_id'] = 'x' * (1024 * 1024)
        self.run_child('import time\ntime.sleep(60)\n', config=config)

    def test_failed_inventory_cleanup_stops_only_owned_root_with_bounded_wait(self):
        tracker = Mock()
        tracker.contained.return_value = False
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        start = time.monotonic()
        try:
            self.assertFalse(collector._reap(process, tracker, self.config['limits']['termination_grace_s']))
            # Configured grace plus scheduler allowance, with no unbounded wait.
            self.assertLess(time.monotonic() - start, 5)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=self.config['limits']['termination_grace_s'])

    def test_malformed_child_scope_is_refused(self):
        for scope in ({'kind': 'cli_store', 'lane': 'codex', 'scope_id': None},
                      {'kind': 'surprise', 'lane': 'codex', 'scope_id': '0' * 64}):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                collector._payload((json.dumps(dict(source='fixture', auth_scope=scope, record={}, error=None)) + '\n').encode())


class ExplicitSpentObservation(unittest.TestCase):
    def test_native_refusal_without_windows_stays_spent_without_inventing_geometry(self):
        scope = {'kind': 'cli_store', 'lane': 'codex', 'scope_id': '0' * 64}
        row = {'lane': 'codex', 'source': 'codex-app-server', 'auth_scope': scope,
               'observed_at': 100, 'observed_monotonic': 100, 'error': None,
               'record': {'source': 'codex-app-server', 'windows': [], 'refusing': True}}
        result = policy._evaluate({'provider': 'codex-cli', 'model': 'gpt-6.1-sol', 'auth_scope': scope},
                                  (row,), now=100, monotonic=100, max_age_s=30)
        self.assertEqual(result['status'], 'spent')
        self.assertEqual(row['record']['windows'], [])


class StatusParserFailures(unittest.TestCase):
    def test_codex_unsupported_clock_and_calendar_remain_unknown(self):
        from cortados_router.scarcity import parse_codex_status
        for reset in ('3:99 AM on 22 Oct', '3:22 AM on 31 Feb', '13:22 AM on 22 Oct', '3:22 AM on'):
            with self.subTest(reset=reset):
                row = parse_codex_status(f'Weekly limit: [bar] 64% left (resets {reset})')[0]
                self.assertIsNone(row['reset_epoch'])
                self.assertIsNone(row['resets_at'])


if __name__ == '__main__':
    unittest.main()
