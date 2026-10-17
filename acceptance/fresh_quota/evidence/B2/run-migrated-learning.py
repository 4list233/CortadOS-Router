"""Run unchanged activation assertions against new installed learning modules.

Teacher transport stays the existing inert subprocess double. The additional
quota check gets explicit known synthetic input; its real refusal logic is
examined independently by the 005 teacher tests in both learning modes.
"""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
STAGE = Path('/Users/velox/.cortados-builds/fresh-quota-activation')
sys.path.insert(0, str(ROOT))
os.environ['CORTADOS_MCP_ACTIVATION_ROOT'] = str(STAGE)
from acceptance.mcp_learning import pipeline_support, test_activation

protected = [ROOT / 'acceptance/mcp_learning' / name
             for name in ('test_activation.py', 'pipeline_support.py')]
before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
original = pipeline_support.pipeline
origins = []


@contextmanager
def migrated_pipeline(name, data, **kwargs):
    previous = dict(sys.modules)
    previous_path = list(sys.path)
    for key in list(sys.modules):
        if key.split('.')[0] in {'cortados_router', 'integrations'}:
            del sys.modules[key]
    try:
        with original(name, data, **kwargs) as (module, trace):
            if name != 'label':
                yield module, trace
                return
            module.REPO = STAGE
            binary = data / 'inert-bin/claude'
            binary.parent.mkdir(exist_ok=True)
            binary.write_text('#!/bin/sh\nexit 99\n')
            binary.chmod(0o700)
            environment = {'HOME': str(data), 'PATH': str(binary.parent)}
            module._env = lambda: environment
            owner = importlib.import_module('cortados_router.quota_admission')
            config = importlib.import_module('integrations.mcp.config')
            site = Path(os.environ['CORTADOS_MCP_LEARNING_SITE_PACKAGES']).resolve()
            for imported in (owner, config):
                assert Path(imported.__file__).resolve().is_relative_to(site)
                origins.append(str(Path(imported.__file__).resolve()))
            quota = {'status': 'available', 'kind': 'subscription', 'lane': 'claude',
                     'pool': 'all', 'observed_at': 2000000000, 'age_s': 0,
                     'source': 'migrated-learning-fixture', 'reason': 'Synthetic component input'}
            with patch.object(config, 'load_config', return_value={}), \
                    patch.object(owner, 'check', return_value=quota):
                yield module, trace
    finally:
        sys.path[:] = previous_path
        for key in set(sys.modules) - set(previous):
            del sys.modules[key]
        sys.modules.update(previous)


with patch.object(test_activation, 'pipeline', migrated_pipeline):
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(test_activation))
after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
assert before == after, 'Protected activation test bytes changed'
report = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
          'skipped': len(result.skipped), 'protected_before': before, 'protected_after': after,
          'installed_origins': sorted(set(origins)),
          'fixture_substitutions': ['fresh stage REPO', 'inert PATH/HOME', 'known quota check',
                                    'normalized config fixture', 'isolated package namespaces']}
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2))
sys.exit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)
