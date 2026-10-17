# Loads actual production PTY/reaper owners; only vendor transport is an inert Python terminal.
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

root, home, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root))
from cortados_router import scarcity

attempts = []
real_popen = subprocess.Popen

def guarded_popen(argv, *args, **kwargs):
    if Path(argv[0]).name in ('ps', 'lsof'):
        attempts.append(Path(argv[0]).name)
        raise OSError('Independent exam blocks privileged/external inventory before launch')
    return real_popen(argv, *args, **kwargs)

result, error = None, None
try:
    with patch.object(scarcity, 'PROBE_DIR', home / 'probes'), patch.object(subprocess, 'Popen', guarded_popen):
        result = scarcity.tui_capture(
            [sys.executable, '-B', str(Path(__file__).with_name('pty_source.py')), str(home / 'terminal.jsonl'), mode],
            '/usage', 'QUOTA_DONE', dict(os.environ, HOME=str(home)),
            boot_s=.5, run_s=.5, ready='READY')
except Exception as exc:
    error = type(exc).__name__  # No terminal material or exception message enters the report.
(home / 'result.json').write_text(json.dumps({'attempts': attempts, 'result': result, 'error': error}))
