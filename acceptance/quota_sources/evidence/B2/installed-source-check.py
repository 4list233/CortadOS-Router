"""Bounded native metadata check through installed owner; never launches a worker."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import time
from cortados_router import quota_admission, quota_observations
from integrations.mcp.config import load_config

BASE = Path('/Users/velox/.cortados-builds/quota-verification-repair')
STAGE = Path('/Users/velox/.cortados-builds/quota-sources-activation')
RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017')
assert Path(quota_admission.__file__).is_relative_to(RELEASE)
out = BASE / 'installed-source-check.json'
assert not out.exists(), 'Preserve the first installed source check'
config = load_config('/Users/velox/.config/cortados-workers/config.json')
sys.path.insert(0, '/Users/velox/cortados')
sys.path.insert(0, str(STAGE / 'plugins/cortados-pty'))
spec = importlib.util.spec_from_file_location('quota006_operator_runner', STAGE / 'plugins/cortados-pty/runner.py')
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
report = {'started_at': time.time(), 'module_origin': quota_admission.__file__, 'worker_turns': 0,
          'caller_origin': runner.__file__, 'lanes': {}}
for runtime, provider, model in [('cursor', 'cursor-cli', 'auto'), ('claude', 'claude-cli', 'sonnet'),
                                  ('agy', 'agy-cli', 'gemini-2.5-pro')]:
    env = runner._child_env(runtime, None)
    argv = runner.build_brain_argv(runtime, model=model, system_prompt='Quota metadata check only; never dispatched.')
    binary = shutil.which(argv[0], path=env.get('PATH'))
    if binary:
        argv[0] = str(Path(binary).resolve())
    try:
        actual = quota_admission.prepare_actual(provider, environment=env, executable=argv[0],
                                                argv=tuple(argv), cwd='/Users/velox/.cortados/brain-cwd', config=config)
        result = quota_admission.check_actual(provider, model, actual=actual, config=config)
        report['lanes'][runtime] = result
        if runtime == 'cursor' and result['status'] in ('available', 'spent'):
            # Same source context/cache, no second native request and no secret-bearing fields.
            entry = quota_admission._same_actual(actual, config)
            row = quota_observations.collect_actual(entry, config=config)
            report['cursor_windows'] = row['record'].get('windows', [])
        actual = None
    except Exception as exc:
        report['lanes'][runtime] = {'status': 'unknown', 'failure_class': type(exc).__name__,
                                    'reason': 'Actual source could not be proven; worker not started'}
report['codex'] = quota_admission.source_preflight('codex-cli', 'gpt-6-luna', config=config)['quota']
report['completed_at'] = time.time()
out.write_text(json.dumps(report, indent=2))
out.chmod(0o600)
print(json.dumps(report, indent=2))
