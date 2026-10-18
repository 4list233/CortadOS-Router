"""Read-only installed-release parity and protected learning identity checks."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tomllib
import unittest

ROOT = Path('/Users/velox/.cortados-builds/cowork-mcp-verification/historical-007/source')
RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017')
SITE = RELEASE / 'venv/lib/python3.14/site-packages'
OLD_SITE = Path('/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages')
DETAILS = {}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstalledParity(unittest.TestCase):
    def test_installed_sources_match_reviewed_checkout(self):
        configuration = ROOT / 'pyproject.toml'
        self.assertEqual(digest(configuration), 'f7516262d17c7ddb3f19ba0b1e6e4ac3c99067244c95795a07e16a9b28e3fa7d')
        declaration = tomllib.loads(configuration.read_text())['tool']['setuptools']
        expected = set()
        for package in declaration['packages']:
            directory = ROOT / Path(*package.split('.'))
            expected.update(str(path.relative_to(ROOT)) for path in directory.glob('*.py') if path.is_file())
            for pattern in declaration.get('package-data', {}).get(package, []):
                expected.update(str(path.relative_to(ROOT)) for path in directory.glob(pattern) if path.is_file())
        roots = {package.split('.')[0] for package in declaration['packages']}
        actual = {str(path.relative_to(SITE)) for package in roots for path in (SITE / package).rglob('*')
                  if path.is_file() and '__pycache__' not in path.parts}
        self.assertEqual(actual, expected, 'Exact declared installed package file set')
        compared = {}
        for relative in sorted(expected):
            self.assertEqual(digest(ROOT / relative), digest(SITE / relative), relative)
            compared[relative] = digest(ROOT / relative)
        self.assertGreaterEqual(len(compared), 55)
        DETAILS['installed_source_sha256'] = compared
        DETAILS['package_declaration_sha256'] = digest(configuration)

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
        root = Path('/Users/velox/.cortados-builds/cowork-mcp-verification/historical-007/train')
        before = json.loads(Path('/Users/velox/.cortados-builds/cowork-mcp-verification/historical-007/data-before.json').read_text())
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
