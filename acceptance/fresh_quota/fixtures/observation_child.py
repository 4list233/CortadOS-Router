# PURPOSE: Real bounded-process fixture for quota collection, never a provider.
import json
import os
import pty
from pathlib import Path
import signal
import subprocess
import sys
import time

lane, control_path = sys.argv[1:]
control = json.loads(Path(control_path).read_text())
mode = control.get('mode', 'normal')
if mode == 'no-read-stdin':
    with open(control['log'], 'a') as target:
        target.write(json.dumps({'lane': lane, 'pid': os.getpid(), 'pgid': os.getpgrp(),
                                 'started': time.time(), 'monotonic': time.monotonic()}) + '\n')
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
config = json.load(sys.stdin)
with open(control['log'], 'a') as target:
    target.write(json.dumps({'lane': lane, 'pid': os.getpid(), 'pgid': os.getpgrp(), 'started': time.time(),
                             'monotonic': time.monotonic(), 'config': config}) + '\n')
if mode == 'escaped-hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child_pid, master = pty.fork()
    if child_pid == 0:
        with open(control['log'], 'a') as target:
            target.write(json.dumps({'escaped_pid': os.getpid(), 'pgid': os.getpgrp()}) + '\n')
        helper = subprocess.Popen([sys.executable, '-c',
            'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],
            start_new_session=True)
        with open(control['log'], 'a') as target:
            target.write(json.dumps({'escaped_pid': helper.pid, 'pgid': helper.pid}) + '\n')
        time.sleep(60)
        os._exit(0)
    time.sleep(60)
if mode == 'hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    with open(control['log'], 'a') as target:
        target.write(json.dumps({'child_pid': child.pid}) + '\n')
    time.sleep(60)
if mode == 'overflow':
    sys.stdout.write('x' * 2097152)
    sys.stdout.flush()
    time.sleep(60)
if mode == 'stderr-overflow':
    sys.stderr.write('x' * 2097152)
    sys.stderr.flush()
    time.sleep(60)
if mode == 'invalid':
    print('{not json}')
    sys.exit(0)
if mode == 'exit-error':
    sys.exit(7)
time.sleep(control.get('delays', {}).get(lane, 0))
value = control['outputs'][lane]
print(json.dumps(value), flush=True)
if mode == 'extra-json':
    print(json.dumps(value), flush=True)
