# Real macOS /bin/sh watchdog regression. Run: python3 -I launcher_test.py timeout (or early).
# Only stdlib, fake children and throwaway state; never launches a provider.

import json, os, pathlib, signal, subprocess, sys, tempfile, time
import re, shlex
source = pathlib.Path(__file__).with_name('register.ts').read_text()
cleanup = source.split('export const BG_CLEANUP_SH = `', 1)[1].split('\n`\n', 1)[0]
def expand(text):
    text = text.replace('${shQuote(BG_CLEANUP_SH)}', shlex.quote(cleanup))
    text = re.sub(r'\$\{shQuote\(`([\s\S]*?)`\)\}', lambda m: shlex.quote(m[1]), text)
    return text.replace('${BG_CAPTURE_BYTES}', '1048576').replace(r'\${', '${')
supervisor = expand(source.split('const BG_SUPERVISE_SH = `', 1)[1].split('\n`\n', 1)[0])
reaper = expand(source.split('export const BG_REAP_SH = `', 1)[1].split('\n`\n', 1)[0])
launcher = source.split('export const BG_LAUNCH_SH = `', 1)[1].split('`\n', 1)[0]
launcher = launcher.replace('${BG_REAP_SH}', reaper).replace('${shQuote(BG_SUPERVISE_SH)}', shlex.quote(supervisor))
launcher = launcher.replace(r'\${', '${').replace(r'\\n', r'\n')
mode = sys.argv[1]
assert mode in ('timeout', 'early', 'capture')
if mode == 'capture':
    with tempfile.TemporaryDirectory(prefix='route-bg-capture-') as tmp:
        env = {'PATH': '/usr/bin:/bin', 'TMPDIR': tmp}
        # Shorten only the raw-retention timer, not the watchdog or byte limit.
        quick = launcher.replace('+ 3600', '+ 1').replace('sleep 60;', 'sleep 1;')
        d = pathlib.Path(subprocess.check_output(
            ['/bin/sh', '-c', quick, 'sh', '30', sys.executable, '-I', '-c',
             'import os; os.write(1,b"x"*2097152); os.write(2,b"y"*2097152)'],
            env=env, text=True, timeout=3).strip())
        try:
            deadline = time.monotonic() + 18
            while not (d / 'exit').exists() and time.monotonic() < deadline: time.sleep(.02)
            assert (d / 'exit').exists(), 'overflow run never finished'
            assert (d / 'overflow').exists(), 'overflow was not reported'
            assert (d / 'stdout').stat().st_size == 1048576
            assert (d / 'stderr').stat().st_size == 1048576
            (d / 'output.md').write_text('sanitized result')
            while (d / 'stdout').exists() and time.monotonic() < deadline: time.sleep(.05)
            assert not (d / 'stdout').exists(), 'raw retention expired without cleanup'
            assert not (d / 'stderr').exists()
            assert (d / 'output.md').read_text() == 'sanitized result'
            # Recovery removes stale raw captures, never the sanitized result; ignores unmarked dirs.
            for marked in (True, False):
                stale = pathlib.Path(tmp) / ('cortados-bg.marked' if marked else 'cortados-bg.unmarked')
                stale.mkdir()
                (stale / 'stdout').write_text('raw')
                (stale / 'output.md').write_text('saved')
                if marked: (stale / 'owner-v1').touch()
                os.utime(stale, (time.time() - 18000,) * 2)
            subprocess.run(['/bin/sh', '-c', reaper], env=env, check=True)
            assert not (pathlib.Path(tmp) / 'cortados-bg.marked/stdout').exists()
            assert (pathlib.Path(tmp) / 'cortados-bg.marked/output.md').read_text() == 'saved'
            assert (pathlib.Path(tmp) / 'cortados-bg.unmarked/stdout').exists()
        finally:
            if (d / 'supervisor').exists():
                try: os.kill(int((d / 'supervisor').read_text()), signal.SIGTERM)
                except ProcessLookupError: pass
    print('capture bounds, overflow, expiry and recovery verified')
    sys.exit(0)
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
        if 'd' in locals() and (d / 'supervisor').exists():
            try: os.kill(int((d / 'supervisor').read_text()), signal.SIGTERM)
            except ProcessLookupError: pass
        if groupfile.exists():
            try: os.killpg(int(groupfile.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass
        if pidfile.exists():
            try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass
print('launcher cleanup verified')
