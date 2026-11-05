# Real macOS /bin/sh watchdog regression. Run: python3 -I launcher_test.py timeout (or early).
# Only stdlib, fake children and throwaway state; never launches a provider.

import json, os, pathlib, signal, subprocess, sys, tempfile, time
import re, shlex
source = pathlib.Path(__file__).with_name('register.ts').read_text()
supervisor = source.split('const BG_SUPERVISE_SH = `', 1)[1].split('\n`\n', 1)[0]
supervisor = re.sub(r'\$\{shQuote\(`([\s\S]*?)`\)\}', lambda m: shlex.quote(m[1]), supervisor)
launcher = source.split('export const BG_LAUNCH_SH = `', 1)[1].split('`\n', 1)[0]
launcher = launcher.replace('${shQuote(BG_SUPERVISE_SH)}', shlex.quote(supervisor))
launcher = launcher.replace(r'\${', '${').replace(r'\\n', r'\n')
mode = sys.argv[1]
assert mode in ('timeout', 'early')
grandchild = """
import os, pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
while True: time.sleep(1)
"""
child = """
import os, pathlib, signal, subprocess, sys, time
def finish(*args):
    print('{"result":"partial result"}', flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, finish)
pathlib.Path(sys.argv[1] + '.group').write_text(str(os.getpgrp()))
subprocess.Popen([sys.executable, '-I', '-c', sys.argv[3], sys.argv[1]])
while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)
if sys.argv[2] == 'early': finish()
while True: time.sleep(1)
"""
def gone(pid):
    try: os.kill(pid, 0); return False
    except ProcessLookupError: return True
with tempfile.TemporaryDirectory(prefix='route-bg-test-') as tmp:
    pidfile = pathlib.Path(tmp) / 'grandchild'
    groupfile = pathlib.Path(str(pidfile) + '.group')
    try:
        env = {'PATH': '/usr/bin:/bin', 'TMPDIR': tmp}
        d = pathlib.Path(subprocess.check_output(
            ['/bin/sh', '-c', launcher, 'sh', '2' if mode == 'timeout' else '30',
             sys.executable, '-I', '-c', child, str(pidfile), mode, grandchild],
            env=env, text=True, timeout=3).strip())
        deadline = time.monotonic() + 17
        while not (d / 'exit').exists() and time.monotonic() < deadline: time.sleep(.05)
        assert (d / 'exit').exists(), 'launcher never published exit'
        assert pidfile.exists(), 'fake grandchild never started'
        pid = int(pidfile.read_text())
        while not gone(pid) and time.monotonic() < deadline: time.sleep(.05)
        assert gone(pid), 'TERM-resistant grandchild survived group cleanup'
        assert (d / 'timedout').exists() == (mode == 'timeout')
        assert (d / 'exit').read_text() == '0', (d / 'exit').read_text()
        assert json.loads((d / 'stdout').read_text())['result'] == 'partial result'
    finally:
        if groupfile.exists():
            try: os.killpg(int(groupfile.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass
        if pidfile.exists():
            try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass
print('launcher cleanup verified')
