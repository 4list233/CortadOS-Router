# PURPOSE: Separate typed zero-start refusal from uncertain or completed native attempts.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T01,T05; subprocess is always replaced.
import json
from types import SimpleNamespace
from unittest.mock import patch
from .support import TeacherCase, SYSTEM, answers


class ClaudeBoundary(TeacherCase):
    def call(self):
        return self.owner().call_claude('claude-sonnet-5', self.snapshot['rows'][:2],
            system_prompt=SYSTEM, environment=self.environment, cwd=str(self.data),
            config=self.config, max_scarcity=0.7)

    def test_real_unproven_claude_source_refuses_before_subprocess(self):
        owner = self.owner()
        with patch('shutil.which', return_value=self.config['codex_bin']), patch('subprocess.run') as run:
            with self.assertRaises(owner.PreStartRefusal) as exc: self.call()
        self.assertEqual(exc.exception.reason, 'quota_unknown')
        run.assert_not_called()

    def test_known_unknown_and_spent_checks_never_start_native(self):
        from cortados_router import quota_admission
        owner = self.owner()
        for status, reason in [('unknown', 'quota_unknown'), ('spent', 'quota_spent')]:
            with self.subTest(status=status), patch('shutil.which', return_value=self.config['codex_bin']), \
                 patch.object(quota_admission, 'prepare_actual', return_value=object()), \
                 patch.object(quota_admission, 'check_actual', return_value={'status': status}), patch('subprocess.run') as run:
                with self.assertRaises(owner.PreStartRefusal) as exc: self.call()
                self.assertEqual(exc.exception.reason, reason)
                run.assert_not_called()

    def test_permission_error_is_not_reclassified_as_quota_refusal(self):
        from cortados_router import quota_admission
        with patch('shutil.which', return_value=self.config['codex_bin']), \
             patch.object(quota_admission, 'prepare_actual', side_effect=PermissionError('Synthetic permission failure')), \
             patch('subprocess.run') as run:
            with self.assertRaises(PermissionError): self.call()
            run.assert_not_called()

    def test_started_native_failure_never_becomes_prestart_refusal(self):
        from cortados_router import quota_admission, scarcity
        owner = self.owner()
        for error in (TimeoutError('quota 429'), RuntimeError('quota'), ValueError('bad response')):
            with self.subTest(error=type(error).__name__), patch('shutil.which', return_value=self.config['codex_bin']), \
                 patch.object(quota_admission, 'prepare_actual', return_value=object()), \
                 patch.object(quota_admission, 'check_actual', return_value={'status': 'available'}), \
                 patch.object(scarcity, 'current', return_value={'lanes': {'claude': {'scarcity': 0.1}}}), \
                 patch('subprocess.run', side_effect=error) as run:
                with self.assertRaises(type(error)) as exc: self.call()
                self.assertNotIsInstance(exc.exception, owner.PreStartRefusal)
                run.assert_called_once()
