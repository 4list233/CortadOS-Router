# Real macOS /bin/sh watchdog regressions. Run: python3 -I launcher_test.py MODE
# MODE: timeout, early, capture, identity, immediate, census-fail, ledger-fail, sweep.
# Only stdlib, fake children and throwaway state; never launches a provider.

import json, os, pathlib, signal, subprocess, sys, tempfile, time
import re, shlex
source = pathlib.Path(__file__).with_name('register.ts').read_text()
cleanup = source.split('export const BG_CLEANUP_SH = `', 1)[1].split('\n`\n', 1)[0]
def expand(text):
    text = text.replace('${shQuote(BG_CLEANUP_SH)}', shlex.quote(cleanup))
    text = re.sub(r'\$\{shQuote\(`([\s\S]*?)`\)\}', lambda m: shlex.quote(m[1].replace(r'\\', '\\')), text)
    return text.replace('${BG_CAPTURE_BYTES}', '1048576').replace(r'\${', '${')
supervisor = expand(source.split('const BG_SUPERVISE_SH = `', 1)[1].split('\n`\n', 1)[0])
reaper = expand(source.split('export const BG_REAP_SH = `', 1)[1].split('\n`\n', 1)[0])
launcher = source.split('export const BG_LAUNCH_SH = `', 1)[1].split('`\n', 1)[0]
launcher = launcher.replace('${BG_REAP_SH}', reaper).replace('${shQuote(BG_SUPERVISE_SH)}', shlex.quote(supervisor))
launcher = launcher.replace(r'\${', '${').replace(r'\\n', r'\n')
sweep = source.split('export const BG_SWEEP_SH = String.raw`', 1)[1].split('\n`', 1)[0]
mode = sys.argv[1]
assert mode in ('timeout', 'early', 'immediate', 'capture', 'identity', 'census-fail', 'ledger-fail', 'sweep')
if mode == 'sweep':
    import uuid
    with tempfile.TemporaryDirectory(prefix='route-bg-sweep-') as tmp:
        processes = []
        dirs = {}
        try:
            for kind in ('active', 'stale', 'unrelated', 'argv', 'marker', 'ledger'):
                d = pathlib.Path(tmp) / ('cortados-bg.' + kind)
                d.mkdir()
                dirs[kind] = d
                (d / 'owner-v1').touch()
                marker = str(uuid.uuid4())
                (d / 'sweep-marker').write_text(marker + '\n')
                if kind != 'active': (d / 'exit').write_text('0')
                env = {'PATH': '/usr/bin:/bin'}
                if kind in ('active', 'marker'): env['CORTADOS_BG_RUN'] = marker
                p = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)',
                                      'CORTADOS_BG_RUN=' + marker if kind == 'argv' else kind],
                                     env=env, start_new_session=True)
                processes.append((kind, p))
                start = subprocess.check_output(['ps', '-p', str(p.pid), '-o', 'lstart='], text=True).strip()
                if kind in ('ledger', 'stale'):
                    (d / 'processes').write_text(f'{p.pid}\t{p.pid}\t' + (start if kind == 'ledger' else 'STALE') + '\n')
            # Result processing must not discard the identity evidence needed by a later sweep.
            subprocess.run(['/bin/sh', '-c', cleanup, 'sh', str(dirs['ledger'])], check=True)
            assert (dirs['ledger'] / 'processes').exists()
            subprocess.run(['/bin/sh', '-c', sweep], env={'PATH': '/usr/bin:/bin', 'TMPDIR': tmp},
                           check=True, timeout=30)
            for kind, p in processes:
                if kind in ('marker', 'ledger'):
                    p.wait(timeout=3)
                    assert p.returncode < 0, kind
                    assert '\tTERM\t' in (dirs[kind] / 'sweep.log').read_text()
                else:
                    assert p.poll() is None, 'collateral kill: ' + kind
                    assert not (dirs[kind] / 'sweep.log').exists(), kind
        finally:
            for _, p in processes:
                if p.poll() is None: p.kill()
                p.wait()
    print('sweep: active, stale, unrelated and argv-only spared; marker and ledger members terminated')
    sys.exit(0)
if mode == 'identity':
    # Exercise the actual watchdog helper against a live, unrelated process in its own group.
    # A ledger entry for its pid with a different start time must not adopt or signal it.
    tracker = 'track() {' + supervisor.split('track() {', 1)[1].split('\n}\nfailed=', 1)[0] + '\n}\n'
    with tempfile.TemporaryDirectory(prefix='route-bg-identity-') as tmp:
        sleeper = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        try:
            ledger = pathlib.Path(tmp) / 'processes'
            ledger.write_text(str(sleeper.pid) + '\t' + str(sleeper.pid) + '\tSTALE IDENTITY\n')
            command = 'D=$1; c=$2\n' + tracker + '\ntrack scan && track TERM && track KILL'
            subprocess.run(['/bin/sh', '-c', command, 'sh', tmp, str(sleeper.pid)], check=True)
            assert sleeper.poll() is None, 'stale pid identity was signaled'
            assert 'STALE IDENTITY' in ledger.read_text(), 'stale pid was readopted'
            # With a matching identity, the same helper must signal it.
            start = subprocess.check_output(['ps', '-p', str(sleeper.pid), '-o', 'lstart='], text=True).strip()
            ledger.write_text(str(sleeper.pid) + '\t' + str(sleeper.pid) + '\t' + start + '\n')
            subprocess.run(['/bin/sh', '-c', command, 'sh', tmp, str(sleeper.pid)], check=True)
            sleeper.wait(timeout=3)
            assert sleeper.returncode < 0
        finally:
            if sleeper.poll() is None: sleeper.kill()
            sleeper.wait()
    print('stale identity ignored; matching identity terminated')
    sys.exit(0)
if mode == 'capture':
    with tempfile.TemporaryDirectory(prefix='route-bg-capture-') as tmp:
        env = {'PATH': '/usr/bin:/bin', 'TMPDIR': tmp}
        # Shorten only the raw-retention timer, not the watchdog or byte limit.
        quick = launcher.replace('+ 3600', '+ 2').replace('sleep 60;', 'sleep 1;')
        d = pathlib.Path(subprocess.check_output(
            ['/bin/sh', '-c', quick, 'sh', '30', sys.executable, '-I', '-c',
             'import os; os.write(1,b"x"*2097152); os.write(2,b"y"*2097152)'],
            env=env, text=True, timeout=3).strip())
        try:
            deadline = time.monotonic() + 22
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
subprocess.Popen([sys.executable, '-I', '-c', sys.argv[3], sys.argv[1]], start_new_session=True)
while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)
if sys.argv[2] == 'immediate':
    finish()
if sys.argv[2] == 'early':
    # Let the periodic census observe the detached tool before deliberately orphaning it.
    ledger = pathlib.Path(os.environ['D']) / 'processes'
    pid = pathlib.Path(sys.argv[1]).read_text()
    while not ledger.exists() or not any(line.split()[0] == pid for line in ledger.read_text().splitlines()):
        time.sleep(.01)
    finish()
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
        run_launcher = launcher
        if mode == 'census-fail':
            # Allow the fake workload to start before failing every census, including teardown.
            run_launcher = launcher.replace('close $ps or die "ps failed";', 'close $ps or die "ps failed"; sleep 1; die "injected census failure";')
        elif mode == 'ledger-fail':
            run_launcher = launcher.replace('open my $out, ">", "$file.tmp"', 'sleep 1; mkdir "$file.tmp"; open my $out, ">", "$file.tmp"')
        d = pathlib.Path(subprocess.check_output(
            ['/bin/sh', '-c', run_launcher, 'sh', '2' if mode in ('timeout', 'census-fail') else '30',
             sys.executable, '-I', '-c', child, str(pidfile), mode, grandchild],
            env=env, text=True, timeout=3).strip())
        deadline = time.monotonic() + 17
        while not (d / 'exit').exists() and time.monotonic() < deadline: time.sleep(.05)
        assert (d / 'exit').exists(), 'launcher never published exit'
        assert pidfile.exists(), 'fake grandchild never started'
        pid = int(pidfile.read_text())
        assert pid != int(groupfile.read_text()), 'probe did not detach from child group'
        if mode not in ('census-fail', 'ledger-fail', 'immediate'):
            assert any(line.split()[0] == str(pid) for line in (d / 'processes').read_text().splitlines()), 'detached tool was not tracked'
        elif mode in ('census-fail', 'ledger-fail'):
            assert (d / 'supervisor-failed').exists(), 'census failure was not published'
        if mode in ('immediate', 'census-fail', 'ledger-fail'):
            assert not gone(pid), 'fixture did not reproduce watchdog escape'
            subprocess.run(['/bin/sh', '-c', sweep], env=env, check=True, timeout=30)
            assert '\tKILL\t' + str(pid) + '\t' in (d / 'sweep.log').read_text()
            deadline = time.monotonic() + 3
        while not gone(pid) and time.monotonic() < deadline: time.sleep(.05)
        assert gone(pid), 'TERM-resistant detached grandchild survived descendant cleanup'
        assert (d / 'timedout').exists() == (mode == 'timeout')
        expected_exit = '125' if mode in ('census-fail', 'ledger-fail') else '0'
        assert (d / 'exit').read_text() == expected_exit, (d / 'exit').read_text()
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
