"""Read-only installed-release parity and protected learning identity checks."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017')
SITE = RELEASE / 'venv/lib/python3.14/site-packages'
OLD_SITE = Path('/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages')
DETAILS = {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstalledParity(unittest.TestCase):
    def test_installed_sources_match_reviewed_checkout(self):
        compared = {}
        for package in ('cortados_router', 'integrations'):
            expected = {str(p.relative_to(ROOT)) for p in (ROOT / package).rglob('*') if p.suffix in ('.py', '.cjs')}
            actual = {str(p.relative_to(SITE)) for p in (SITE / package).rglob('*') if p.suffix in ('.py', '.cjs')}
            self.assertEqual(actual, expected, package + ' installed source file set')
            for source in sorted((ROOT / package).rglob('*')):
                if source.suffix not in ('.py', '.cjs'):
                    continue
                relative = source.relative_to(ROOT)
                self.assertTrue((SITE / relative).is_file(), str(relative))
                self.assertEqual(digest(source), digest(SITE / relative), str(relative))
                compared[str(relative)] = digest(source)
        self.assertGreaterEqual(len(compared), 55)
        DETAILS['installed_source_sha256'] = compared

    def test_runtime_imports_are_from_fixed_installed_release(self):
        self.assertEqual(Path(sys.executable), RELEASE / 'venv/bin/python')
        self.assertEqual(Path(sys.prefix), RELEASE / 'venv')
        origins = {}
        for name in ('cortados_router.teacher', 'cortados_router.teacher_attempts',
                     'cortados_router.learning_data', 'integrations.mcp.broker', 'integrations.mcp.config'):
            module = importlib.import_module(name)
            self.assertEqual(Path(module.__file__).resolve(), SITE / (name.replace('.', '/') + '.py'))
            origins[name] = str(Path(module.__file__).resolve())
        DETAILS['origins'] = origins

    def test_learning_and_native_execution_owners_are_unchanged(self):
        paths = ['cortados_router/' + name + '.py' for name in
                 ('learning_data', 'learning_source', 'task_evidence', 'task_context', 'core')]
        paths += ['integrations/mcp/' + name + '.py' for name in
                  ('codex', 'worker', 'job_store', 'broker', 'native_metadata', 'native_process', 'schemas')]
        for name in paths:
            self.assertEqual(digest(SITE / name), digest(OLD_SITE / name), name)
        DETAILS['unchanged_owners'] = paths

    def test_existing_exam_and_model_settings_are_unchanged(self):
        root = Path('/Users/velox/cortados/train')
        before = json.loads(Path('/Users/velox/.cortados-builds/training-teacher-verification/data-before.json').read_text())
        for name, expected in before['immutable_files'].items():
            self.assertEqual(digest(root / name), expected, name)
        self.assertEqual(digest(root / 'data/labels.jsonl'), before['label_sha256'])
        DETAILS['immutable_files'] = before['immutable_files']


result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(InstalledParity))
DETAILS['counts'] = {'tests': result.testsRun, 'failures': len(result.failures),
                     'errors': len(result.errors), 'skipped': len(result.skipped)}
DETAILS['pass'] = result.wasSuccessful() and not result.skipped
Path(sys.argv[1]).write_text(json.dumps(DETAILS, indent=2) + '\n')
sys.exit(0 if DETAILS['pass'] else 1)
