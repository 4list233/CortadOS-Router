"""Operator replacement of an idle broker and only the Claude worker entry command."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import runpy
from unittest.mock import patch
from integrations.mcp import broker
from integrations.mcp.config import config_bytes, load_config
from integrations.mcp.native_process import process_identity

BASE = Path('/Users/velox/.cortados-builds/quota-verification-repair')
OUT = BASE / 'mcp-activation'
RELEASE = Path('/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017')
OLD = '/Users/velox/.local/share/cortados-workers/releases/fresh-quota-v1-20261017/venv/bin/python'
CONFIG = Path('/Users/velox/.config/cortados-workers/config.json')
CLAUDE = Path('/Users/velox/.claude.json')
approval = runpy.run_path(str(BASE / 'verify-reviewed-release.py'))['approval']
assert Path(sys.executable) == RELEASE / 'venv/bin/python'
assert Path(broker.__file__).is_relative_to(RELEASE)
assert not OUT.exists(), 'Preserve prior activation evidence'
raw = config_bytes(CONFIG)
config = load_config(CONFIG, raw=raw)
digest = hashlib.sha256(raw).hexdigest()
state = Path(config['state_dir'])
def records():
    with sqlite3.connect((state / 'jobs.db').as_uri() + '?mode=ro', uri=True) as db:
        assert db.execute('select count(*) from jobs where active<>0').fetchone()[0] == 0, 'Active jobs'
        return {'jobs': {r[0]: hashlib.sha256(r[1].encode()).hexdigest() for r in db.execute('select job_id,record from jobs')},
                'feedback': {str(r[0]): hashlib.sha256(r[1].encode()).hexdigest() for r in db.execute('select seq,payload from job_feedback')}}
before = records()
prior = json.loads(Path('/Users/velox/.cortados-builds/fresh-quota-verification/mcp-activation/broker-activation.json').read_text())
assert prior['new']['argv'] == [OLD, '-B', '-m', 'integrations.mcp', '--broker', '--config', str(CONFIG)]
identity = prior['new']['identity']
assert process_identity(identity['pid']) == identity, 'Recorded old broker identity changed'
# macOS renders the framework executable in ps rather than the venv launch path.
# Bind the already-recorded launch and native identity; do not weaken to a basename match.
old_command = subprocess.check_output(['ps', '-p', str(identity['pid']), '-o', 'command='], text=True).strip()
assert old_command == prior['new_ps'].split(None, 7)[7], 'Recorded old broker command changed'
old_claude = CLAUDE.read_bytes()
doc = json.loads(old_claude)
entry = doc['mcpServers']['cortados-workers']
assert entry['command'] == OLD
entry['command'] = str(RELEASE / 'venv/bin/python')
new_claude = (json.dumps(doc, indent=2) + '\n').encode()
OUT.mkdir(mode=0o700)
(OUT / 'claude-before.json').write_bytes(old_claude)
(OUT / 'claude-before.json').chmod(0o600)
with sqlite3.connect((state / 'jobs.db').as_uri() + '?mode=ro', uri=True) as source:
    with sqlite3.connect(OUT / 'jobs-before.db') as target:
        source.backup(target)
assert records() == before and process_identity(identity['pid']) == identity
os.kill(identity['pid'], signal.SIGTERM)
deadline = time.monotonic() + config['limits']['startup_timeout_s']
while process_identity(identity['pid']) is not None:
    assert time.monotonic() < deadline, 'Old broker failed to stop'
    time.sleep(config['limits']['process_poll_ms'] / 1000)
assert records() == before
os.environ['CORTADOS_HOME'] = str(state / 'router')
os.environ['CORTADOS_S1_URL'] = 'http://127.0.0.1:8431'
os.umask(0o077)
launches = []
real_popen = subprocess.Popen
def launch(argv, *args, **kwargs):
    process = real_popen(argv, *args, **kwargs)
    launches.append({'argv': argv, 'pid': process.pid, 'identity': process_identity(process.pid)})
    return process
with patch.object(subprocess, 'Popen', side_effect=launch):
    broker.ensure_broker(config, CONFIG, digest)
assert len(launches) == 1 and launches[0]['argv'][0] == str(RELEASE / 'venv/bin/python')
assert records() == before and config_bytes(CONFIG) == raw
assert broker.rpc(config, digest, '__hello', {})['ok']
assert CLAUDE.read_bytes() == old_claude, 'Concurrent Claude configuration change'
temporary = CLAUDE.with_name('.claude.json.quota006-activation')
assert not temporary.exists()
temporary.write_bytes(new_claude)
temporary.chmod(CLAUDE.stat().st_mode & 0o777)
assert CLAUDE.read_bytes() == old_claude
os.replace(temporary, CLAUDE)
verified = json.loads(CLAUDE.read_bytes())
verified['mcpServers']['cortados-workers']['command'] = OLD
assert verified == json.loads(old_claude), 'An unrelated Claude setting changed'
report = {'old_identity': identity, 'new': launches[0], 'prior_records_unchanged': True,
          'module_origin': broker.__file__, 'config_sha256': digest,
          'claude_change': 'Only cortados-workers command replaced', 'completed_at': time.time()}
(OUT / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
