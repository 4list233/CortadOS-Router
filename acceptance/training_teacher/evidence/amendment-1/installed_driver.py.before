# PURPOSE: Exercise exact staged label.main with installed owners and private DATA in every mode.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T08; only broker/Claude external boundaries are substituted.
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from support import TeacherCase, COMMON, ANSWER, answers

RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017')
STAGE = Path(os.environ.get('CORTADOS_TRAINING_TEACHER_STAGE', '/Users/velox/.cortados-builds/training-teacher-activation'))
OLD_SITE = '/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages'


class InstalledLabel(TeacherCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(Path(sys.prefix), RELEASE / 'venv')
        self.assertEqual(Path(sys.executable), RELEASE / 'venv/bin/python')
        from cortados_router import teacher, teacher_attempts, learning_data
        site = RELEASE / 'venv/lib/python3.14/site-packages'
        for module in (teacher, teacher_attempts, learning_data):
            self.assertEqual(Path(module.__file__).resolve().parent, site / 'cortados_router')
        self.common_data = COMMON.DATA
        COMMON.DATA = self.data
        self.addCleanup(setattr, COMMON, 'DATA', self.common_data)
        state = self.data / '.cortados'; state.mkdir()
        (state / 'env').write_text('')
        (state / 'teacher-cwd').mkdir()
        config_home = self.data / '.config/cortados-workers'; config_home.mkdir(parents=True)
        (config_home / 'teacher.json').write_bytes(self.config_path.read_bytes())
        self.usage = []
        usage = types.ModuleType('usage'); usage.record = self.usage.append
        for context in (patch.object(Path, 'home', return_value=self.data),
                        patch.dict(os.environ, {'HOME': str(self.data), 'CORTADOS_MCP_LEARNING_SITE_PACKAGES': OLD_SITE}),
                        patch.dict(sys.modules, {'common': COMMON, 'usage': usage}),
                        patch('subprocess.run', side_effect=AssertionError('Unexpected native/model process'))):
            context.start(); self.addCleanup(context.stop)
        (self.data / 'requests.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.requests))
        spec = importlib.util.spec_from_file_location('installed_teacher_label_exam', STAGE / 'train/label.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def main(self, *extra):
        argv = [str(STAGE / 'train/label.py'), '--k', '1', '--batch', '12', '--workers', '1', *extra]
        with patch.object(sys, 'argv', argv):
            return self.module.main()

    def primary(self, model, batch, **kwargs):
        return answers(batch), {'usage': {}, 'session_id': 'synthetic-only'}

    def legacy_primary(self):
        # Both documented adapter/native boundaries are inert; implementation selection/writer stays real.
        boundary = patch.object(self.owner(), 'call_claude', side_effect=self.primary)
        boundary.start(); self.addCleanup(boundary.stop)
        if hasattr(self.module, 'call_teacher'):
            direct = patch.object(self.module, 'call_teacher', side_effect=self.primary)
            direct.start(); self.addCleanup(direct.stop)
        if hasattr(self.module, '_scarce'):
            scarcity = patch.object(self.module, '_scarce', return_value=0.1)
            scarcity.start(); self.addCleanup(scarcity.stop)

    def test_enabled_main_writes_final_train_only_and_no_claude_usage(self):
        self.transport()
        self.main('--split', 'train')
        expected = {rid for rid, part in self.snapshot['partitions'].items() if part == 'train'}
        self.assertEqual({row['id'] for row in self.labels()}, expected)
        self.assertTrue(self.broker.calls)
        self.assertEqual(self.usage, [])

    def test_flagoff_main_preserves_primary_and_never_submits_codex(self):
        self.transport(); self.legacy_primary()
        with patch.dict(os.environ, {'CORTADOS_TRAINING_TEACHER_V1': '0'}):
            self.main('--split', 'all')
        self.assertEqual(self.broker.calls, [])
        self.assertEqual({row['id'] for row in self.labels()}, {row['id'] for row in self.requests})
        self.assertTrue(all(row['teacher'].startswith('claude-') for row in self.labels()))

    def test_same_private_lock_refuses_every_main_mode_before_calls(self):
        from cortados_router.teacher_attempts import LabelAttempts
        self.transport(); self.legacy_primary()
        with LabelAttempts(self.data, validate_answers=COMMON.valid_answers, questions=COMMON.QUESTIONS):
            for enabled, only in [('1', ''), ('0', ''), ('1', 'outward')]:
                with self.subTest(enabled=enabled, only=only), patch.dict(os.environ, {'CORTADOS_TRAINING_TEACHER_V1': enabled}):
                    try:
                        value = self.main(*(['--only', only] if only else []))
                        self.assertNotEqual(value, 0)
                    except Exception as exc:
                        from integrations.mcp.schemas import WorkerError
                        self.assertIsInstance(exc, WorkerError)
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.labels(), [])
        self.assertEqual(self.usage, [])

    def test_override_retains_partial_representation_and_full_prefix(self):
        self.transport(); self.legacy_primary()
        labels = [{'id': row['id'], 'teacher': 'claude-opus-5' if row['split']=='holdout' else 'claude-sonnet-5',
                   'sample': 0, 'ts': 1, 'answers': ANSWER} for row in self.requests]
        prefix = ''.join(json.dumps(row) + '\n' for row in labels).encode()
        (self.data / 'labels.jsonl').write_bytes(prefix)
        self.main('--only', 'outward')
        self.assertEqual((self.data / 'labels.jsonl').read_bytes(), prefix)
        self.assertEqual(self.broker.calls, [])
        overrides = [json.loads(line) for line in (self.data / 'labels-override-outward.jsonl').read_text().splitlines()]
        self.assertEqual(len(overrides), len(labels))
        self.assertTrue(all(row['override'] is True and row['answers']=={'outward': False} for row in overrides))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstalledLabel)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
                      'skips':len(result.skipped),'pass':result.wasSuccessful() and not result.skipped}))
    sys.exit(0 if result.wasSuccessful() and not result.skipped else 1)
