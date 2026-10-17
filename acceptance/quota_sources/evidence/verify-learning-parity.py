"""Operator verification of immutable old/new learning and routing installations."""
import ast
import hashlib
import json
from pathlib import Path

BASE = Path('/Users/velox/.local/share/cortados-workers/releases')
OLD = BASE / 'fresh-quota-v1-20261017/venv/lib/python3.14/site-packages'
NEW = BASE / 'quota-sources-v1-20261017/venv/lib/python3.14/site-packages'
OUT = Path('/Users/velox/.cortados-builds/quota-verification-repair/learning-parity.json')
ALLOWED = {'cortados_router/' + name for name in (
    'quota_admission.py', 'quota_observations.py', 'quota_sources.py',
    'cursor_quota.cjs', 'scarcity.py', 'probe_reaper.py')}


def inventory(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ('cortados_router', 'integrations', 'tables')
            for p in (root / folder).rglob('*')
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}


assert NEW.is_dir() and OLD.is_dir(), 'Both noneditable releases must exist'
old, new = inventory(OLD), inventory(NEW)
changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)}
assert not old.keys() - new.keys(), 'Installed source unexpectedly removed'
assert changed <= ALLOWED, ('Unapproved release change', sorted(changed - ALLOWED))
required = [
    'cortados_router/learning_data.py', 'cortados_router/learning_source.py',
    'cortados_router/task_evidence.py', 'integrations/mcp/schemas.py',
    'integrations/mcp/native_profile.py',
] + [p for p in old if p.endswith('/__init__.py')]
assert all(old[p] == new[p] for p in required), 'Learning/initialization byte parity failed'


def clean_request(root):
    source = (root / 'cortados_router/core.py').read_text()
    function = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'clean_request')
    return {'ast': ast.dump(function, include_attributes=False),
            'source': ast.get_source_segment(source, function)}


assert clean_request(OLD) == clean_request(NEW), 'Learning input cleaner changed'
report = {'old': str(OLD), 'new': str(NEW), 'allowed_changes': sorted(changed),
          'preserved_files': {p: new[p] for p in sorted(set(required))},
          'all_old_source_hashes': old, 'all_new_source_hashes': new,
          'clean_request_identical_ast_and_source': True, 'pass': True}
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({'pass': True, 'changed': sorted(changed), 'report': str(OUT)}))
