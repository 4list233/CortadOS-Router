"""Independent in-memory mutation proof; never edits a production source file."""
import hashlib, json, os, signal, subprocess, unittest
from pathlib import Path
from unittest.mock import patch
from acceptance.fresh_quota.test_collection import Collection


def group_only(process, tracker, grace):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    return True


def blocking_input(process, config, deadline, tracker):
    process.stdin.write((json.dumps(config) + '\n').encode())
    process.stdin.close()
    raise ValueError('mutation fixture never reaches an admissible response')


class GroupMutant(Collection):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.collector, '_reap', group_only))


class WriteMutant(Collection):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(self.collector, '_exchange', blocking_input))


report = []
for cls, method in ((GroupMutant, 'test_deadline_contains_pty_session_and_nested_setsid_helper'),
                    (WriteMutant, 'test_child_that_never_reads_config_cannot_block_deadline')):
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([cls(method)]))
    report.append({'mutant': cls.__name__, 'test': method, 'tests': result.testsRun,
                   'failures': [{'test': str(t), 'traceback': tb} for t,tb in result.failures],
                   'errors': [{'test': str(t), 'traceback': tb} for t,tb in result.errors],
                   'skips': result.skipped, 'detected': bool(result.failures) and not result.errors})
source=Path('cortados_router/quota_observations.py')
Path('/Users/velox/.cortados-builds/fresh-quota-verification/containment-mutants.json').write_text(json.dumps(
    {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'results': report}, indent=2)+'\n')
assert all(r['detected'] for r in report)
