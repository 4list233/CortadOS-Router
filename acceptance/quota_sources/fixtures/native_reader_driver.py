# Test-only process isolates Node instrumentation from the rest of the independent suite.
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

root, home = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
from cortados_router import quota_sources
config = json.loads((home / 'config.json').read_text())
request = json.loads((home / 'request.json').read_text())
real_popen = subprocess.Popen

def launch(argv, *args, **kwargs):
    if Path(argv[0]).name != 'node':
        raise AssertionError('Native reader may only launch pinned Node in this fixture')
    argv = [argv[0], '--require', str(Path(__file__).with_name('native_dashboard_preload.cjs')), *argv[1:]]
    kwargs['env'] = dict(kwargs.get('env', os.environ), QUOTA_NATIVE_EXAM_HOME=str(home))
    return real_popen(argv, *args, **kwargs)

try:
    with patch.object(subprocess, 'Popen', side_effect=launch), patch('time.time', return_value=json.loads((home / 'observed-at.json').read_text())):
        capture = quota_sources.capture_source(**request, config=config)
        value = quota_sources.read_source(capture.profile, environment=request['environment'],
            executable=request['executable'], source_argv=capture.source_argv, cwd=request['cwd'], config=config)
    result = {'ok': True, 'record': value.record, 'valid_until_epoch': value.valid_until_epoch}
except Exception as exc:
    result = {'ok': False, 'error_type': type(exc).__name__, 'error_code': getattr(exc, 'code', None)}
(home / 'read-result.json').write_text(json.dumps(result))
