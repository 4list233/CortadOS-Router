# Real macOS /bin/sh watchdog regressions. Run: python3 -I launcher_test.py MODE
# MODE: timeout, early, capture, identity, immediate, census-fail, ledger-fail, sweep, uuid-fail.
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
launcher = launcher.replace(r'\${', '${').replace(r'\\n', r'\n').replace(r'\\A', r'\A').replace(r'\\z', r'\z')
sweep = source.split('export const BG_SWEEP_SH = String.raw`', 1)[1].split('\n`', 1)[0]
mode = sys.argv[1]
assert mode in ('timeout', 'early', 'immediate', 'capture', 'identity', 'census-fail', 'ledger-fail', 'sweep', 'uuid-fail')
if mode == 'uuid-fail':
    import uuid
    # Each command must be checked, even when the other succeeds; validate successful but malformed output too.
    for fault in ('first', 'second', 'both', 'empty', 'malformed'):
        with tempfile.TemporaryDirectory(prefix='route-bg-uuid-') as tmp:
            base = pathlib.Path(tmp)
            bindir = base / 'bin'; bindir.mkdir()
            counter = base / 'count'
            fake = bindir / 'uuidgen'
            fake.write_text('#!/bin/sh\n'
                            'n=0; [ ! -f "$UUID_COUNT" ] || n=$(cat "$UUID_COUNT")\n'
                            'n=$((n + 1)); printf %s "$n" > "$UUID_COUNT"\n'
                            'case "$UUID_FAULT:$n" in first:1|second:2|both:*) exit 42;; '
                            'empty:*) exit 0;; malformed:*) printf invalid; exit 0;; esac\n'
                            + 'printf %s ' + shlex.quote(str(uuid.uuid4())) + '\n')
            fake.chmod(0o700)
            launched = base / 'launched'
            # Intercept supervisor launch itself so a broken launcher cannot leave a watchdog behind.
            nohup = bindir / 'nohup'
            nohup.write_text('#!/bin/sh\n: > "$LAUNCH_SENTINEL"\n')
            nohup.chmod(0o700)
            result = subprocess.run(['/bin/sh', '-c', launcher, 'sh', '1', 'true'],
                                    env={'PATH': str(bindir) + ':/usr/bin:/bin', 'TMPDIR': tmp,
                                         'UUID_COUNT': str(counter), 'UUID_FAULT': fault,
                                         'LAUNCH_SENTINEL': str(launched)},
                                    capture_output=True, text=True, timeout=5)
            assert result.returncode != 0, fault
            assert not result.stdout.strip(), fault
            assert not launched.exists(), fault
            assert not list(base.glob('cortados-bg.*/exit')), fault
    print('uuid-fail: first/second/both failures and empty/malformed output block launch')
    sys.exit(0)
if mode == 'sweep':
    import uuid
    with tempfile.TemporaryDirectory(prefix='route-bg-sweep-') as tmp:
        processes = []
        dirs = {}
        try:
            kinds = ('active', 'stale', 'unrelated', 'argv', 'marker', 'ledger', 'copied', 'embedded', 'collision',
                     'missing', 'legacy', 'noWindow')
            for kind in kinds:
                d = pathlib.Path(tmp) / ('cortados-bg.' + kind)
                d.mkdir(); dirs[kind] = d
                (d / 'owner-v1').touch()
                marker = str(uuid.uuid4()) + str(uuid.uuid4())
                (d / 'sweep-marker').write_text(marker + '\n')
                (d / 'run-start').write_text(str(int(time.time()) - 10))
                # Copied/embedded markers and the active-run collision start after this finished run's window.
                if kind in ('copied', 'embedded', 'collision'):
                    (d / 'run-finish').write_text(str(int(time.time()) - 5))
                if kind != 'active': (d / 'exit').write_text('0')
                env = {'PATH': '/usr/bin:/bin'}
                if kind in ('active', 'marker', 'copied', 'collision'): env['CORTADOS_BG_RUN'] = marker
                if kind == 'embedded': env['UNRELATED_LOG_TEXT'] = 'captured text CORTADOS_BG_RUN=' + marker + ' end'
                p = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)',
                                      'CORTADOS_BG_RUN=' + marker if kind == 'argv' else kind],
                                     env=env, start_new_session=True)
                processes.append((kind, p))
                start = subprocess.check_output(['ps', '-p', str(p.pid), '-o', 'lstart='], text=True).strip()
                if kind in ('ledger', 'stale', 'missing', 'legacy', 'noWindow'):
                    (d / 'processes').write_text(f'{p.pid}\t{p.pid}\t' + ('STALE' if kind == 'stale' else start) + '\n')
                if kind == 'missing': (d / 'sweep-marker').unlink()
                if kind == 'legacy': (d / 'sweep-marker').write_text('legacy-marker')
                if kind == 'noWindow': (d / 'run-start').unlink()
                if kind not in ('copied', 'embedded', 'collision'):
                    (d / 'run-finish').write_text(str(int(time.time()) + 1))
            # Result processing must not discard the identity evidence needed by a later sweep.
            subprocess.run(['/bin/sh', '-c', cleanup, 'sh', str(dirs['ledger'])], check=True)
            assert (dirs['ledger'] / 'processes').exists()
            bindir = pathlib.Path(tmp) / 'bin'; bindir.mkdir()
            reader = bindir / 'python3'
            reader.write_text('#!/bin/sh\nexit 42\n'); reader.chmod(0o700)
            failed = subprocess.run(['/bin/sh', '-c', sweep],
                                    env={'PATH': str(bindir) + ':/usr/bin:/bin', 'TMPDIR': tmp},
                                    capture_output=True, text=True, timeout=5)
            assert failed.returncode != 0, 'reader failure was swallowed'
            assert 'sweep marker-reader failed' in failed.stderr
            assert all(p.poll() is None for _, p in processes), 'failed reader caused a signal'
            subprocess.run(['/bin/sh', '-c', sweep], env={'PATH': '/usr/bin:/bin', 'TMPDIR': tmp},
                           check=True, timeout=30)
            for kind, p in processes:
                if kind in ('marker', 'ledger', 'missing', 'legacy', 'noWindow'):
                    p.wait(timeout=3); assert p.returncode < 0, kind
                    log = (dirs[kind] / 'sweep.log').read_text()
                    assert '\tTERM\t' in log
                    assert ('\tmarker-window\n' in log) == (kind == 'marker')
                else:
                    assert p.poll() is None, 'collateral kill: ' + kind
                    log = dirs[kind] / 'sweep.log'
                    assert not log.exists() or '\tTERM\t' not in log.read_text(), kind
        finally:
            for _, p in processes:
                if p.poll() is None: p.kill()
                p.wait()
    print('sweep: reader failure reported; marker and metadata-independent ledger recovery passed; unrelated processes spared')
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
    # A tracked leader that moves into an unrelated live group loses group-broadcast authority.
    with tempfile.TemporaryDirectory(prefix='route-bg-group-move-') as tmp:
        gate = pathlib.Path(tmp) / 'move'
        unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'], preexec_fn=os.setpgrp)
        mover_code = ('import os,sys,time,pathlib\ngate=pathlib.Path(sys.argv[1]); target=int(sys.argv[2])\n'
                      'while not gate.exists(): time.sleep(.01)\nos.setpgid(0,target)\ntime.sleep(60)')
        mover = subprocess.Popen([sys.executable, '-I', '-c', mover_code, str(gate), str(unrelated.pid)], preexec_fn=os.setpgrp)
        try:
            ledger = pathlib.Path(tmp) / 'processes'
            command = 'D=$1; c=$2\n' + tracker + '\ntrack scan; : > "$3"; sleep 1; track scan; track TERM'
            subprocess.run(['/bin/sh', '-c', command, 'sh', tmp, str(mover.pid), str(gate)], check=True)
            mover.wait(timeout=3)
            assert mover.returncode < 0, 'moved tracked process was not individually signalled'
            assert unrelated.poll() is None, 'ownership followed the tracked process into an unrelated group'
        finally:
            for p in (mover, unrelated):
                if p.poll() is None: p.kill()
                p.wait()
    print('stale identities ignored; moved leader signalled individually without killing unrelated group')
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
