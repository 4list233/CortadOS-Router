"""Operator-only guarded activation of the reviewed staged files; no source edits."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import runpy

BASE = Path('/Users/velox/.cortados-builds/quota-verification-repair')
STAGE = Path('/Users/velox/.cortados-builds/quota-sources-activation')
LIVE = Path('/Users/velox/cortados')
OUT = BASE / 'activation'
assert not OUT.exists(), 'Preserve an existing activation record'
approval = runpy.run_path(str(BASE / 'verify-reviewed-release.py'))['approval']
manifest_path = BASE / 'staged006-attempt2/candidate-manifest.json'
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
assert digest(manifest_path) == approval['stage_manifest_sha256']
manifest = json.loads(manifest_path.read_text())
if 'files' in manifest:
    manifest = manifest['files']
baseline = json.loads((BASE / 'activation-baseline.json').read_text())
for row in baseline['live_files'] + baseline['protected_files']:
    assert digest(Path(row['path'])) == row['sha256'], 'Live input changed: ' + row['path']
for name, row in manifest.items():
    assert digest(STAGE / name) == row['candidate_sha256'], 'Candidate changed: ' + name
    assert digest(LIVE / name) == row['before_sha256'], 'Before hash changed: ' + name
assert not (LIVE / 'train/nightly.pid').exists(), 'Nightly may be active'
processes = subprocess.check_output(['ps', '-axo', 'command='], text=True).splitlines()
assert not any(('train/nightly.sh' in p or 'train/student.py' in p) and not p.startswith('python3 -')
               for p in processes), 'Training may be active'
OUT.mkdir(mode=0o700)
(OUT / 'before').mkdir(mode=0o700)
ordered = sorted(manifest, key=lambda name: (name == 'train/nightly.sh', name.startswith('train/'), name))
report = {'approved': approval, 'started_at': time.time(), 'files': [], 'nightly_last': False}
for index, name in enumerate(ordered):
    source, target = STAGE / name, LIVE / name
    row = manifest[name]
    assert digest(target) == row['before_sha256'], 'Concurrent live change: ' + name
    assert digest(source) == row['candidate_sha256'], 'Concurrent candidate change: ' + name
    backup = OUT / 'before' / str(index)
    shutil.copy2(target, backup)
    temporary = target.with_name(target.name + '.quota006-activation')
    assert not temporary.exists(), 'Unexpected temporary path'
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, target.stat().st_mode & 0o777)
        assert digest(target) == row['before_sha256'], 'Concurrent live change before replace'
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    assert digest(target) == row['candidate_sha256']
    report['files'].append({'path': str(target), 'before_sha256': row['before_sha256'],
                            'after_sha256': row['candidate_sha256'], 'backup': str(backup),
                            'mode': oct(target.stat().st_mode & 0o777)})
    (OUT / 'report.json').write_text(json.dumps(report, indent=2))
report['nightly_last'] = ordered[-1] == 'train/nightly.sh'
for row in baseline['protected_files']:
    assert digest(Path(row['path'])) == row['sha256']
report['protected_files_unchanged'] = True
report['completed_at'] = time.time()
(OUT / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'files': len(report['files']), 'nightly_last': report['nightly_last'],
                  'protected_files_unchanged': True, 'report': str(OUT / 'report.json')}))
