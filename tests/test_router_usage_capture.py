"""PTY failure diagnostics with tiny fake CLIs; no account/model calls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('router_usage_capture_test', ROOT/'cortados_router/scarcity.py')
scarcity = importlib.util.module_from_spec(spec); spec.loader.exec_module(scarcity)


class Capture(unittest.TestCase):
    def test_startup_exit_retains_metadata_without_boot_text(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scarcity, 'PROBE_DIR', Path(tmp)):
            diagnostic = {}
            result = scarcity.tui_capture([sys.executable, '-c', 'print("private startup error", flush=True)'],
                                          '/status', 'limit:', dict(os.environ), boot_s=2,
                                          ready='model:', diagnostics=diagnostic)
        self.assertEqual(result, '')
        self.assertEqual(diagnostic['stage'], 'boot')
        self.assertTrue(diagnostic['eof'])
        self.assertGreater(diagnostic['startup_chars'], 0)
        self.assertEqual(diagnostic['response_chars'], 0)
        self.assertNotIn('private', str(diagnostic))

    def test_empty_codex_error_reports_capture_state(self):
        def capture(*args, diagnostics, **kwargs):
            diagnostics.update(stage='command', eof=False, startup_chars=800, response_chars=0, ready_seen=True)
            return ''
        with patch.object(scarcity, '_cli_env', return_value={}), patch.object(scarcity, 'tui_capture', side_effect=capture):
            with self.assertRaisesRegex(RuntimeError, 'stage=command, eof=False, startup_chars=800'):
                scarcity._codex()

    def test_status_parser_still_uses_only_captured_panel(self):
        with patch.object(scarcity, '_cli_env', return_value={}), patch.object(scarcity, 'tui_capture', return_value='Weekly limit: [####] 63% left'):
            result = scarcity._codex()
        self.assertEqual(result['windows'][0]['used_pct'], 37)

    def test_boot_exit_recovers_once_and_retains_diagnostic(self):
        calls = []
        def capture(*args, diagnostics, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                diagnostics.update(stage='boot', eof=True, startup_chars=724, response_chars=0, ready_seen=False, boot_failure='unknown')
                return ''
            return 'Weekly limit: [####] 61% left'
        with patch.object(scarcity, '_cli_env', return_value={}), patch.object(scarcity, 'tui_capture', side_effect=capture):
            result = scarcity._codex()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['windows'][0]['used_pct'], 39)
        self.assertEqual(result['startup_retries'][0]['startup_chars'], 724)

    def test_persistent_boot_exit_is_bounded(self):
        def capture(*args, diagnostics, **kwargs):
            diagnostics.update(stage='boot', eof=True, boot_failure='unknown')
            return ''
        with patch.object(scarcity, '_cli_env', return_value={}), patch.object(scarcity, 'tui_capture', side_effect=capture) as call:
            with self.assertRaisesRegex(RuntimeError, 'attempts=2'):
                scarcity._codex()
        self.assertEqual(call.call_count, 2)

    def test_config_auth_and_exec_failures_are_not_retried(self):
        for kind in ('config', 'auth', 'exec'):
            def capture(*args, diagnostics, **kwargs):
                diagnostics.update(stage='boot', eof=True, boot_failure=kind)
                return ''
            with self.subTest(kind=kind), patch.object(scarcity, '_cli_env', return_value={}), patch.object(scarcity, 'tui_capture', side_effect=capture) as call:
                with self.assertRaisesRegex(RuntimeError, 'attempts=1'):
                    scarcity._codex()
                self.assertEqual(call.call_count, 1)

    def test_startup_classification_never_copies_private_text(self):
        self.assertEqual(scarcity._boot_failure_kind('Error: The cursor position could not be read within a normal duration'), 'cursor_position')
        self.assertEqual(scarcity._boot_failure_kind('Error loading config: private-token'), 'config')
        self.assertEqual(scarcity._boot_failure_kind('private-token'), 'unknown')

    def test_codex_probe_survives_a_dead_configured_binary(self):
        # 2026-10-13: ChatGPT.app dropped Resources/codex while 13 profiles still pointed CORTADOS_CODEX_BIN at it.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'codex'
            fake.write_text('#!/bin/sh\n'); fake.chmod(0o755)
            env = {'CORTADOS_CODEX_BIN': '/nonexistent/codex', 'PATH': tmp}
            with patch.object(scarcity, '_cli_env', return_value=env), \
                 patch.object(scarcity, 'tui_capture', return_value='Weekly limit: [####] 63% left') as call:
                result = scarcity._codex()
        self.assertEqual(call.call_args.args[0], [str(fake)],
                         'scarcity._codex: a missing configured binary must fall back to codex on PATH')
        self.assertEqual(result['configured_binary_missing'], '/nonexistent/codex',
                         'scarcity._codex: the fallback must stay visible in the reading')

    def test_codex_probe_keeps_a_working_configured_binary(self):
        with patch.object(scarcity, '_cli_env', return_value={'CORTADOS_CODEX_BIN': sys.executable, 'PATH': ''}), \
             patch.object(scarcity, 'tui_capture', return_value='Weekly limit: [####] 63% left') as call:
            result = scarcity._codex()
        self.assertEqual(call.call_args.args[0], [sys.executable])
        self.assertNotIn('configured_binary_missing', result)

    def test_failed_exec_never_runs_a_forked_copy_of_the_caller(self):
        # Regression: after pty.fork, a failed execvpe raised in the child, which then kept running the caller's code.
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "import importlib.util, os, sys\n"
                "from pathlib import Path\n"
                f"spec = importlib.util.spec_from_file_location('scarcity_exec_test', {str(ROOT / 'cortados_router/scarcity.py')!r})\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                f"m.PROBE_DIR = Path({tmp!r})\n"
                "try:\n"
                "    m.tui_capture(['/nonexistent/vendor-cli'], '/usage', 'never', dict(os.environ), boot_s=2)\n"
                "finally:\n"
                f"    open({str(Path(tmp) / 'callers.txt')!r}, 'a').write(str(os.getpid()) + '\\n')\n")
            subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60)
            # The forked child's stdout is the PTY, so the caller marks a file instead of printing.
            callers = (Path(tmp) / 'callers.txt').read_text().split()
        self.assertEqual(len(callers), 1, 'scarcity.tui_capture: a failed exec must exit the forked child, not run the '
                                          f'caller a second time; callers seen: {callers}')


if __name__ == '__main__':
    unittest.main()
