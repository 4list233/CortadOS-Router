# Independent verification driver; records all lanes and fails on any failed/empty/skipped lane.
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
PRIVATE = Path('/Users/velox/.cortados-builds/quota-verification-repair')
HERMES = Path('/Users/velox/.cortados-builds/target-test-env/hermes-agent')
PYTHON = '/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python'
HPYTHON = '/Users/velox/.cortados-builds/target-test-env/venv/bin/python'
out = Path(sys.argv[1] if len(sys.argv) > 1 else '/Users/velox/.cortados-builds/training-teacher-verification/full-suite').resolve()
out.mkdir(parents=True, exist_ok=False)
report = {'revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
          'diff': subprocess.check_output(['git', 'diff', '--stat'], cwd=ROOT, text=True), 'lanes': []}
base = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TMPDIR') if k in os.environ}

def run(name, cmd, *, hermes=False, fixture=None, xml=None, benchmark=False, cwd=ROOT, installed=False):
    scratch = out / name
    scratch.mkdir()
    env = base.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT) + (os.pathsep + str(HERMES) if hermes else ''),
               HERMES_TEST_ROOT=str(fixture or HERMES), CORTADOS_REPO='/Users/velox/cortados',
               CORTADOS_HOME=str(scratch / 'state'), CORTADOS_HERMES_HOME=str(scratch / 'hermes'),
               HERMES_HOME=str(scratch / 'hermes'), HERMES_IGNORE_USER_CONFIG='1',
               HERMES_TELEMETRY_ENABLED='false', CORTADOS_LIVE_WRITE='0', WANGCAI_LIVE_WRITE='0',
               PYTHONHASHSEED='0', TZ='UTC', LANG='C.UTF-8',
               CORTADOS_MCP_ACTIVATION_ROOT='/Users/velox/.cortados-builds/mcp-learning-activation')
    env['CORTADOS_FRESH_QUOTA_STAGE'] = '/Users/velox/.cortados-builds/quota-sources-activation'
    env['CORTADOS_TRAINING_TEACHER_STAGE'] = '/Users/velox/.cortados-builds/training-teacher-activation'
    if installed:
        env.pop('PYTHONPATH', None)
    if name in {'test_required_model_routing', 'test_cron_dispatch_routing'}:
        env['PYTHONPATH'] += os.pathsep + str(PRIVATE)
    if name == 'shared-router-core':
        env['CORTADOS_REPO'] = env['CORTADOS_FRESH_QUOTA_STAGE']
    if name == 'acceptance-quota_evaluation':
        env['CORTADOS_QUOTA_CORE'] = '/Users/velox/.cortados-builds/quota-evaluation-activation/plugins/cortados-router/core.py'
        env['PYTHONPATH'] += os.pathsep + '/Users/velox/cortados'
    started = time.monotonic()
    with (scratch / 'output.log').open('w') as log:
        result = subprocess.run(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
    log = (scratch / 'output.log').read_text()
    counts = {'tests': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    if xml and xml.exists():
        tree = ET.parse(xml).getroot()
        suites = [tree] if tree.tag == 'testsuite' else list(tree.iter('testsuite'))
        for suite in suites:
            for key in counts:
                counts[key] += int(suite.get(key, 0))
    elif not benchmark:
        matches = re.findall(r'Ran (\d+) tests? in ', log)
        if matches:
            counts['tests'] = int(matches[-1])
        summary = re.findall(r'^(?:OK|FAILED)(?: \(([^\n]*)\))?$', log, re.M)
        for key in ('failures', 'errors', 'skipped'):
            match = re.search(key + r'=(\d+)', summary[-1] if summary else '')
            counts[key] = int(match[1]) if match else 0
    good = result.returncode == 0 and (benchmark or counts['tests'] > 0) and not any(counts[k] for k in ('failures', 'errors', 'skipped'))
    entry = {'name': name, 'command': cmd, 'env_overrides': {k: v for k, v in env.items() if k not in base},
             'exit_code': result.returncode, 'wall_s': time.monotonic() - started, 'counts': counts,
             'pass': good, 'benchmark': benchmark}
    report['lanes'].append(entry)
    (out / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({k: entry[k] for k in ('name', 'exit_code', 'wall_s', 'counts', 'pass')}), flush=True)

report['inventory'] = subprocess.check_output(['rg', '--files', '-g', 'test_*.py', '-g', '*_test.py'], cwd=ROOT, text=True).splitlines()
modules = ['tests.' + p.stem for p in sorted((ROOT / 'tests').glob('test_*.py'))]
run('standalone', [PYTHON, '-B', '-m', 'unittest', '-v', *modules])
for path in sorted((ROOT / 'tests/integration').glob('test_*.py')):
    xml = out / (path.stem + '.xml')
    fixture = (ROOT / 'acceptance/router_followup/hermes_pristine'
               if path.stem in {'test_auxiliary_model_routing', 'test_delegation_model_routing'} else None)
    options = ['-p', 'preserved_required_quota'] if path.stem in {'test_required_model_routing', 'test_cron_dispatch_routing'} else []
    run(path.stem, [HPYTHON, '-B', '-m', 'pytest', '-q', str(path), '-p', 'no:cacheprovider', *options, '--junitxml=' + str(xml)],
        hermes=True, fixture=fixture, xml=xml)
new_python = '/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python'
for path in sorted((ROOT / 'acceptance').iterdir()):
    if path.is_dir() and list(path.glob('test_*.py')):
        if path.name == 'mcp_learning':
            run('acceptance-mcp_learning', [new_python, '-B', str(ROOT / 'acceptance/training_teacher/evidence/run-preserved-learning.py')], installed=True)
        else:
            run('acceptance-' + path.name, [PYTHON, '-B', '-m', 'unittest', 'discover', '-s', str(path.relative_to(ROOT)), '-t', '.', '-v'])
run('benchmark', [PYTHON, '-B', 'acceptance/mcp_worker/benchmark.py', '--output', str(out / 'performance.json')], benchmark=True)
for pattern in ('test_router_nightly_runs.py', 'test_router_overnight.py'):
    run('shared-' + pattern[:-3], [PYTHON, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', pattern, '-v'], cwd='/Users/velox/cortados')
for name in ('delivery', 'fd'):
    run('preserved-pty-' + name, [HPYTHON, '-B', '-m', 'acceptance.fresh_quota.run_preserved_pty', name, str(out / ('preserved-pty-' + name + '.json'))], hermes=True)
run('shared-router-core', [PYTHON, '-B', '-m', 'unittest', 'discover', '-s', '/Users/velox/.cortados-builds/quota-sources-activation/tests', '-p', 'test_router.py', '-v'])
run('migrated-learning', [PYTHON, '-B', str(PRIVATE / 'run-migrated-learning.py'), str(out / 'migrated-learning.json')])
new_python = '/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python'
run('installed-teacher-adapter', [new_python, '-B', str(ROOT / 'acceptance/training_teacher/installed_driver.py')], installed=True)
run('installed-teacher-parity', [new_python, '-B', str(ROOT / 'acceptance/training_teacher/evidence/installed-parity.py'), str(out / 'installed-parity.json')], installed=True)
run('teacher-performance', [PYTHON, '-B', str(ROOT / 'acceptance/training_teacher/evidence/benchmark.py'), str(out / 'teacher-performance.json')], benchmark=True)
report['totals'] = {key: sum(lane['counts'][key] for lane in report['lanes']) for key in ('tests', 'failures', 'errors', 'skipped')}
report['pass'] = all(lane['pass'] for lane in report['lanes'])
(out / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'report': str(out / 'report.json'), 'totals': report['totals'], 'pass': report['pass']}), flush=True)
sys.exit(0 if report['pass'] else 1)
