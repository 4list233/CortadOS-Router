"""Operator-only activation of an independently accepted immutable candidate."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile

BASE = Path('/Users/velox/.cortados-builds/training-teacher-verification')
ROOT = Path('/Users/velox/.cortados-builds/cortados-router-repo')
STAGE = Path('/Users/velox/.cortados-builds/training-teacher-activation')
LIVE = Path('/Users/velox/cortados')
RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017')
SITE = RELEASE / 'venv/lib/python3.14/site-packages'
CONFIG = Path('/Users/velox/.config/cortados-workers/teacher.json')
STATE = Path('/Users/velox/.local/state/cortados-teacher-workers')
DRAFT = Path('/private/tmp/training-teacher-config-007.json')
OUT = BASE / 'activation'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

approval = json.loads((BASE / 'activation-approved.json').read_text())
assert approval['independent_B'] == 0 and approval['contract'] == 'RT-TRAINING-TEACHER-007'
for entry in approval['evidence']:
    assert digest(Path(entry['path'])) == entry['sha256'], 'Acceptance evidence changed'
suite = json.loads(Path(approval['full_suite']).read_text())
assert any(entry['path'] == approval['full_suite'] and entry['sha256'] == digest(Path(approval['full_suite']))
           for entry in approval['evidence']), 'Accepted full suite must be pinned'
assert type(approval['accepted_test_count']) is int and approval['accepted_test_count'] > 0
assert suite['pass'] and suite['totals']['tests'] == approval['accepted_test_count']
assert all(suite['totals'][k] == 0 for k in ('failures', 'errors', 'skipped'))
assert all(lane['pass'] for lane in suite['lanes'])
assert digest(DRAFT) == approval['teacher_config_sha256']
assert digest(Path('/Users/velox/.config/cortados-workers/config.json')) == approval['ordinary_config_sha256']
assert not any(p.exists() or p.is_symlink() for p in (OUT, CONFIG, STATE))
release = json.loads((BASE / 'release.json').read_text())
assert digest(Path(release['wheel'])) == release['wheel_sha256'] == approval['wheel_sha256']
for name, expected in release['source_sha256'].items():
    assert digest(ROOT / name) == expected, 'Reviewed source changed: ' + name
with zipfile.ZipFile(release['wheel']) as wheel:
    members = {name for name in wheel.namelist()
               if not name.endswith('/') and name.startswith(('cortados_router/', 'integrations/', 'tables/'))}
    installed = {str(path.relative_to(SITE)) for package in ('cortados_router', 'integrations', 'tables')
                 for path in (SITE / package).rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts}
    assert installed == members, 'Installed package file set differs from accepted wheel'
    for name in sorted(members):
        expected = hashlib.sha256(wheel.read(name)).hexdigest()
        assert expected == release['source_sha256'][name], 'Wheel source identity changed: ' + name
        assert digest(SITE / name) == expected, 'Installed source changed: ' + name
baseline = json.loads((STAGE / 'source-baseline.json').read_text())
for name, expected in baseline.items():
    assert digest(LIVE / name) == expected, 'Live baseline changed: ' + name
for name, expected in approval['staged_files'].items():
    assert digest(STAGE / name) == expected, 'Reviewed stage changed: ' + name
assert set(approval['staged_files']) == {'train/label.py', 'train/nightly.sh'}
before = json.loads((BASE / 'data-before.json').read_text())
for name, expected in before['immutable_files'].items():
    assert digest(LIVE / 'train' / name) == expected, 'Protected input changed: ' + name
assert digest(LIVE / 'train/data/labels.jsonl') == before['label_sha256']
processes = subprocess.check_output(['ps', '-axo', 'command='], text=True).splitlines()
pattern = re.compile(r'(?:^|\s)(?:\S*/)?(?:label\.py|nightly\.sh|overnight\.py|student\.py)(?:\s|$)')
assert not any(pattern.search(p) for p in processes), 'A label or training invocation is active'
pidfile = LIVE / 'train/data/nightly.pid'
if pidfile.exists():
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError('Nightly PID is active')

OUT.mkdir(mode=0o700)
(OUT / 'before').mkdir(mode=0o700)
report = {'started_at': time.time(), 'files': [], 'release': str(RELEASE), 'teacher_config_sha256': digest(DRAFT)}
def record():
    (OUT / 'report.json').write_text(json.dumps(report, indent=2) + '\n')

STATE.mkdir(mode=0o700)
(STATE / 'teacher-cwd').mkdir(mode=0o700)
fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as stream:
    stream.write(DRAFT.read_bytes())
    stream.flush()
    os.fsync(stream.fileno())
assert digest(CONFIG) == digest(DRAFT)
subprocess.run([str(RELEASE / 'venv/bin/python'), '-B', '-c',
    'import sys; from integrations.mcp.config import load_config; load_config(sys.argv[1])', str(CONFIG)],
    check=True, cwd='/private/tmp', env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
report['config_installed'] = True
record()
for name in ('train/label.py', 'train/nightly.sh'):
    target, source = LIVE / name, STAGE / name
    assert digest(target) == baseline[name] and digest(source) == approval['staged_files'][name]
    backup = OUT / 'before' / target.name
    shutil.copy2(target, backup)
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, temp = tempfile.mkstemp(prefix='.teacher007-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        assert digest(target) == baseline[name], 'Concurrent live change before replacement'
        os.replace(temp, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    assert digest(target) == approval['staged_files'][name]
    report['files'].append({'path': str(target), 'before_sha256': baseline[name],
                            'after_sha256': digest(target), 'backup': str(backup), 'mode': oct(mode)})
    record()
report.update(completed_at=time.time(), nightly_last=True)
record()
print(json.dumps({'activated': True, 'files': 2, 'report': str(OUT / 'report.json')}))
