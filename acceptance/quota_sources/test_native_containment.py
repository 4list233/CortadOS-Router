# PURPOSE: Pin actual nested PTY cleanup without privileged inventory or trust acceptance.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS01/QS05; existing native identity owner remains unchanged.
# DEPENDS: Real scarcity.tui_capture/probe_reaper plus inert private terminal process.
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from integrations.mcp.native_process import ProcessTracker, process_identity

ROOT = Path(__file__).resolve().parents[2]


class NativeProbeContainment(unittest.TestCase):
    def run_terminal(self, mode):
        with tempfile.TemporaryDirectory(prefix='quota-source-native-exam-') as folder:
            home = Path(folder)
            process = subprocess.Popen(
                [sys.executable, '-B', str(Path(__file__).with_name('fixtures') / 'pty_driver.py'),
                 str(ROOT), str(home), mode], start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, HOME=str(home), CORTADOS_HOME=str(home), CORTADOS_REPO=str(home)))
            identity = process_identity(process.pid)
            self.assertIsNotNone(identity, 'Independent fixture root must have a native creation identity')
            tracker = ProcessTracker(identity)
            try:
                # Fixture-only allowance: two short terminal phases + existing cleanup + slow CI scheduling.
                deadline = time.monotonic() + 15
                while process.poll() is None and time.monotonic() < deadline:
                    tracker.sample()
                    time.sleep(.02)
                self.assertIsNotNone(process.poll(), 'Actual PTY/reaper path exceeded fixture deadline')
                stdout, stderr = process.communicate(timeout=1)
                self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
                tracker.sample()
                result = json.loads((home / 'result.json').read_text())
                rows = [json.loads(line) for line in (home / 'terminal.jsonl').read_text().splitlines()]
                return result, rows, tracker.contained()
            finally:
                # Strict native identity cleanup contains only this exam's owned descendants on red.
                tracker.signal_groups(signal.SIGTERM)
                tracker.signal_groups(signal.SIGKILL)
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                for pipe in (process.stdout, process.stderr):
                    pipe.close()

    def test_nested_pty_cleanup_uses_native_identity_without_ps_or_lsof(self):
        result, rows, contained = self.run_terminal('normal')
        self.assertEqual(result['attempts'], [], 'Admission PTY cleanup attempted external ps/lsof inventory')
        self.assertIsNone(result['error'])
        self.assertIn('QUOTA_DONE', result['result'])
        self.assertTrue(any(row.get('helper') for row in rows), 'Fixture did not create escaped helper')
        self.assertTrue(contained, 'Actual reaper left a PTY or escaped executable helper alive')

    def test_native_probe_never_accepts_workspace_trust_or_sends_usage_after_prompt(self):
        result, rows, contained = self.run_terminal('trust')
        self.assertFalse(any(row.get('input_hex') for row in rows),
                         'Read-only quota source sent input to a workspace trust prompt')
        self.assertTrue(contained, 'Refused trust prompt retained an owned executable')
        self.assertEqual(result['attempts'], [])
