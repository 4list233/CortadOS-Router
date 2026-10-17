"""One operator owner for the exact installed-artifact activation guard."""
import hashlib
import json
from pathlib import Path

BASE = Path('/Users/velox/.cortados-builds/quota-verification-repair')
SITE = Path('/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages')
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
approval = json.loads((BASE / 'activation-approved.json').read_text())
assert approval['B'] == 0 and approval['full_suite_pass'] is True
for name in ('installed-candidate-parity.json', 'learning-parity.json'):
    assert digest(BASE / name) == approval['evidence_sha256'][name], 'Approval evidence changed: ' + name
parity = json.loads((BASE / 'installed-candidate-parity.json').read_text())
learning = json.loads((BASE / 'learning-parity.json').read_text())
assert parity['source_installed_wheel_byte_parity'] and parity['dependencies_identical'] and learning['pass']
actual = {str(p.relative_to(SITE)): digest(p)
          for folder in ('cortados_router', 'integrations', 'tables')
          for p in (SITE / folder).rglob('*')
          if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
assert actual == parity['files'] == learning['all_new_source_hashes'], 'Installed artifact changed'
release = json.loads((BASE / 'release.json').read_text())
assert digest(Path(release['wheel'])) == parity['wheel_sha256'] == approval['wheel_sha256']
