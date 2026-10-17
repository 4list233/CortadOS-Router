# PURPOSE: Run unchanged maintained shared PTY exams against exact staged copies.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ09; no skipped delivery/FD suites.
# DEPENDS: Actual test bodies, StageCase's explicit known-owner fixture and staged origins.
# CEILING: No live writes, network, successful CLI spawn or production policy bypass.
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import unittest
from unittest.mock import patch

from acceptance.fresh_quota.stage_support import STAGE, StageCase, load

SHARED = Path('/Users/velox/cortados')
HOST = Path('/Users/velox/.cortados/hermes/hermes-agent')


def main(kind, output):
    if kind not in ('delivery', 'fd'):
        raise ValueError('Select delivery or fd')
    fixture = StageCase(methodName='runTest')
    fixture.setUp()
    try:
        tree = fixture.root
        shutil.rmtree(tree / 'plugins/cortados-router')
        for child in (tree / 'context').iterdir():
            if child.name not in ('__init__.py', 'env_compat.py'):
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
        destination = tree / 'plugins/model-providers/cortados-cli'
        shutil.copytree(STAGE / 'plugins/model-providers/cortados-cli', destination, dirs_exist_ok=True)
        os.environ.update(CORTADOS_DELIVERY_TEST_TREE=str(tree), CORTADOS_PTY_DIR=str(tree / 'plugins/cortados-pty'),
                          CORTADOS_LIVE_WRITE='0', WANGCAI_LIVE_WRITE='0')
        sys.path[:0] = [str(tree), str(tree / 'plugins/cortados-pty'), str(HOST)]
        # Host helper tests import the real inert classifier/recovery owners.
        sys.modules['agent'].__path__ = [str(STAGE / 'agent'), str(HOST / 'agent')]
        host_package = load('hermes_cli', HOST / 'hermes_cli/__init__.py')
        host_package.__path__ = [str(STAGE / 'hermes_cli'), str(HOST / 'hermes_cli')]
        load('hermes_constants', HOST / 'hermes_constants.py')
        config = load('hermes_cli.config', HOST / 'hermes_cli/config.py')
        fixture.enterContext(patch.object(config, 'load_config_readonly', return_value={'agent': {'model_router': None}}))
        sys.modules['hermes_cli'].config = config
        load('model_routing', tree / 'plugins/cortados-pty/model_routing.py')
        load('usage', tree / 'plugins/cortados-pty/usage.py')
        runner = load('runner', tree / 'plugins/cortados-pty/runner.py')
        source = SHARED / 'tests' / ('test_cli_delivery_isolated.py' if kind == 'delivery' else 'test_pty_spawn_fd_leak.py')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        with ExitStack() as guards:
            guards.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('Network disabled')))
            guards.enter_context(patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('Network disabled')))
            guards.enter_context(patch.object(os, 'system', side_effect=AssertionError('Shell disabled')))
            if kind == 'delivery':
                import subprocess
                guards.enter_context(patch.object(subprocess, 'Popen', side_effect=AssertionError('External CLI disabled')))
            module = load('preserved_' + kind + '_exams', source)
            assert Path(module.runner.__file__).resolve() == tree / 'plugins/cortados-pty/runner.py'
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(module))
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        report = {'kind': kind, 'test_source': str(source), 'test_sha256': before,
                  'staged_runner': str(STAGE / 'plugins/cortados-pty/runner.py'),
                  'staged_runner_sha256': hashlib.sha256((STAGE / 'plugins/cortados-pty/runner.py').read_bytes()).hexdigest(),
                  'tests': result.testsRun, 'failures': [str(t) for t, _ in result.failures],
                  'errors': [str(t) for t, _ in result.errors], 'skipped': result.skipped,
                  'successful_cli_launches': 0, 'network_calls': 0}
        Path(output).write_text(json.dumps(report, indent=2) + '\n')
        return 0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    raise SystemExit(main(*sys.argv[1:]))
