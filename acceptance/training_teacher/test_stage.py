# PURPOSE: Exercise reviewed launcher pre-import boundary and pin nightly phase isolation.
# CONTRACT: RT-TRAINING-TEACHER-007 AC-T08; no native/provider work.
import ast
import os
import re
from pathlib import Path
import sys
from unittest.mock import patch
from .support import TeacherCase, ROOT

RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017')
STAGE = Path(os.environ.get('CORTADOS_TRAINING_TEACHER_STAGE',
    '/Users/velox/.cortados-builds/training-teacher-activation'))


class ExecObserved(BaseException):
    pass


class StagedTeacher(TeacherCase):
    def bootstrap(self, path):
        """Run the actual top-level pre-import statements, excluding project imports."""
        tree = ast.parse(path.read_text())
        prefix = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and (
                    node.module == 'common' or node.module.startswith(('cortados_router', 's1'))):
                break
            if isinstance(node, ast.Import) and any(alias.name.startswith(('cortados_router', 'common')) for alias in node.names):
                break
            prefix.append(node)
        exec(compile(ast.Module(body=prefix, type_ignores=[]), str(path), 'exec'),
             {'__file__': str(path), '__name__': '__teacher_bootstrap_exam__'})

    def test_both_adapters_reexec_before_project_imports_with_exact_argv(self):
        for path in (ROOT / 's1/label.py', STAGE / 'train/label.py'):
            for enabled in ('0', '1'):
                with self.subTest(path=str(path), enabled=enabled), \
                     patch.dict(os.environ, {'CORTADOS_TRAINING_TEACHER_V1': enabled, 'CORTADOS_TEACHER_REEXEC_V1': ''}), \
                     patch.object(sys, 'executable', '/synthetic/other/python'), patch.object(sys, 'prefix', '/synthetic/other'), \
                     patch.object(sys, 'argv', [str(path), '--workers', '3', '--limit', '1']), \
                     patch('os.path.isfile', return_value=True), patch('os.path.exists', return_value=True), \
                     patch.object(Path, 'exists', return_value=True), \
                     patch.object(os, 'execve', side_effect=ExecObserved) as execute:
                    with self.assertRaises(ExecObserved): self.bootstrap(path)
                    command, argv, environment = execute.call_args.args
                    self.assertEqual(command, str(RELEASE / 'venv/bin/python'))
                    self.assertEqual(argv, [command, '-B', str(path.resolve()), '--workers', '3', '--limit', '1'])
                    self.assertEqual(environment['CORTADOS_TEACHER_REEXEC_V1'], '1')
                    self.assertEqual(os.environ['CORTADOS_TEACHER_REEXEC_V1'], '')

    def test_wrong_interpreter_with_marker_refuses_instead_of_reexec_loop(self):
        for path in (ROOT / 's1/label.py', STAGE / 'train/label.py'):
            with self.subTest(path=str(path)), patch.dict(os.environ, {'CORTADOS_TEACHER_REEXEC_V1': '1'}), \
                 patch.object(sys, 'executable', '/synthetic/other/python'), patch.object(sys, 'prefix', '/synthetic/other'), \
                 patch.object(os, 'execve') as execute:
                with self.assertRaisesRegex(RuntimeError, 'teacher_release_mismatch'): self.bootstrap(path)
                execute.assert_not_called()

    def test_nightly_uses_new_interpreter_only_for_label_phase(self):
        source = (STAGE / 'train/nightly.sh').read_text()
        self.assertIn('CORTADOS_TRAINING_TEACHER_V1=1', source)
        label_lines = [line for line in source.splitlines() if 'label.py' in line and not line.lstrip().startswith('#')]
        self.assertEqual(len(label_lines), 1)
        expanded = label_lines[0]
        for name, value in re.findall(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.+)$', source, re.M):
            expanded = expanded.replace('${'+name+'}', value.strip(chr(34)+chr(39))).replace('$'+name, value.strip(chr(34)+chr(39)))
        self.assertIn(str(RELEASE / 'venv/bin/python'), expanded)
        self.assertIn('--workers 3', label_lines[0])
        for line in source.splitlines():
            if any(name in line for name in ('evaluate.py', 'build_sft.py', 'current.py')):
                self.assertNotIn('training-teacher-v1', line)
