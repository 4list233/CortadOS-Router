"""Exercise the nightly time boundary without loading any model."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import http.client
import os
import signal
import sys
import unittest
from unittest.mock import Mock, patch
import subprocess
import tempfile
import time as _time

spec = importlib.util.spec_from_file_location('overnight_test', Path(__file__).resolve().parents[1]/'s1/overnight.py')
night = importlib.util.module_from_spec(spec); spec.loader.exec_module(night)


class Overnight(unittest.TestCase):
    def test_launch_time_until_seven(self):
        self.assertEqual(night.seconds_left(datetime(2026,10,2,1,30,tzinfo=night.TORONTO)), 5.5*3600)

    def test_daytime_does_not_start_even_a_child(self):
        for hour in [0, 1, 7, 12, 23]:
            self.assertEqual(night.seconds_left(datetime(2026,10,1,hour,tzinfo=night.TORONTO)), 0)
        with patch.object(night.subprocess, 'Popen') as start:
            self.assertEqual(night.run_bounded(['never-run'], 0), 0)
            start.assert_not_called()

    def test_utc_boundary_is_not_offset_twice(self):
        self.assertEqual(night.seconds_left(datetime(2026,10,2,10,59,tzinfo=timezone.utc)), 60)
        self.assertEqual(night.seconds_left(datetime(2026,10,2,11,tzinfo=timezone.utc)), 0)

    def test_fall_clock_change_preserves_local_seven(self):
        self.assertEqual(night.seconds_left(datetime(2026,11,1,1,30,tzinfo=night.TORONTO,fold=0)), 6.5*3600)
        self.assertEqual(night.seconds_left(datetime(2026,11,1,1,30,tzinfo=night.TORONTO,fold=1)), 5.5*3600)

    def test_normal_child_exit_has_no_signal(self):
        with patch.object(night,'signal_group') as kill:
            self.assertEqual(night.run_bounded([sys.executable,'-c','raise SystemExit(3)'], 5),3)
            kill.assert_not_called()

    def test_real_owned_child_is_stopped_at_deadline(self):
        self.assertEqual(night.run_bounded([sys.executable,'-c','import time;time.sleep(30)'], .1, grace=1),124)

    def test_forced_stop_removes_only_its_own_pid_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile=Path(tmp)/'nightly.pid'
            script='import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)'
            self.assertEqual(night.run_bounded([sys.executable,'-c',script,str(pidfile)],.1,grace=1,pidfile=pidfile),124)
            self.assertFalse(pidfile.exists())
            pidfile.write_text('another-owner')
            self.assertEqual(night.run_bounded([sys.executable,'-c','pass'],5,pidfile=pidfile),0)
            self.assertEqual(pidfile.read_text(),'another-owner')

    def test_stubborn_child_gets_group_kill(self):
        # the group never drains (killpg(pgid, 0) keeps succeeding), so the grace runs out and the group is KILLed
        proc=Mock(pid=123);proc.wait.side_effect=[subprocess.TimeoutExpired('x',1),-9]
        sent=[]
        with patch.object(night.subprocess,'Popen',return_value=proc) as start, \
                patch.object(night.os,'killpg',lambda pgid,sig: sent.append((pgid,sig))):
            self.assertEqual(night.run_bounded(['owned'],1,grace=.1),124)
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertIn((123,signal.SIGTERM),sent)
        self.assertIn((123,signal.SIGKILL),sent)

    def test_resume_interrupted_before_first_save_does_not_double_count(self):
        source=(Path(__file__).resolve().parents[1]/'s1/nightly.sh').read_text()
        block=source[source.index('  resume=""'):source.index('  HF_HUB_OFFLINE=1 gated "$PY" -m mlx_lm')]
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);(run/'adapter').mkdir()
            (run/'adapter/0002400_adapters.safetensors').write_bytes(b'checkpoint-2400')
            (run/'iters-base').write_text('0\n');(run/'segment-target').write_text('3600\n')
            def plan():
                script='RUN="$1";iters=7200;MAX_ITERS=3600;while :; do\n'+block+'\nbreak;done\nprintf "PLAN %s %s\\n" "$done_iters" "$remaining"'
                return subprocess.check_output(['/bin/bash','-c',script,'test',str(run)],text=True)
            self.assertIn('PLAN 2400 1200',plan())
            self.assertIn('PLAN 2400 1200',plan())
            self.assertEqual((run/'adapter/resume_2400_adapters.safetensors').read_bytes(),b'checkpoint-2400')
            (run/'adapter/0000100_adapters.safetensors').write_bytes(b'checkpoint-2500')
            self.assertIn('PLAN 2500 1100',plan())


class ReviewFixesRound3(unittest.TestCase):
    """Scheme-less scorer URL never quoted, stale quota honoured, external TERM backs off, malformed URL is a config error."""

    def _guard(self, latency, logs, alerts):
        return night.Guard(latency=latency, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                           log=logs.append, alerter=lambda n, d: alerts.append(d))

    def test_scheme_less_scorer_url_never_in_exception_log_or_alert(self):
        for value, host in (('192.0.2.9:8431', '192.0.2.9'), ('studio.example.ts.net:8431', 'studio.example.ts.net')):
            with self.subTest(value=value), patch.dict(os.environ, {'CORTADOS_S1_URL': value}):
                with self.assertRaises(night.ScorerConfigError) as cm:
                    night.scorer_url()
                self.assertNotIn(host, str(cm.exception)); self.assertIn('CORTADOS_S1_URL', str(cm.exception))
                logs, alerts = [], []
                with patch('builtins.print') as pr:
                    g = self._guard(lambda: night.probe_scorer_latency(timeout=1), logs, alerts)
                    g.idle_probe()
                printed = '\n'.join(str(c.args[0]) for c in pr.call_args_list if c.args)
                text = '\n'.join(logs + alerts) + printed
                self.assertTrue(alerts, 'the config error is alerted')
                self.assertNotIn(host, text); self.assertNotIn('8431', text)

    def test_malformed_url_is_a_config_error_not_a_crash(self):
        logs, alerts = [], []
        with patch.dict(os.environ, {'CORTADOS_S1_URL': 'http://192.0.2.9:abc'}):
            with self.assertRaises(night.ScorerConfigError) as cm:
                night.probe_scorer_latency(url='http://192.0.2.9:abc/score', timeout=1)
            self.assertNotIn('192.0.2.9', str(cm.exception))
            g = self._guard(lambda: night.probe_scorer_latency(url='http://192.0.2.9:abc/score', timeout=1), logs, alerts)
            g.idle_probe()   # must not raise
        self.assertEqual(len(alerts), 1); self.assertNotIn('192.0.2.9', '\n'.join(logs + alerts))

    def _stale(self, marker):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; rf.write_text(marker + '\n')
            g = self._guard(lambda: 1.0, [], [])
            real_unlink = Path.unlink
            def unlink(self, *a, **k):
                if self == rf:
                    raise PermissionError(13, 'Permission denied')
                return real_unlink(self, *a, **k)
            with patch.object(Path, 'unlink', unlink), patch('builtins.print'):
                return night.run_cycle([sys.executable, '-c', 'pass'], g, result_file=rf, poll_s=0, sleep=lambda s: None)

    def test_stale_quota_marker_still_cools_down(self):
        self.assertEqual(self._stale('quota'), (0, 'quota'))
        self.assertEqual(self._stale('worked'), (0, 'idle'))

    def test_rc_130_without_stop_flag_backs_off_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / 'nightly.stop'
            def loop(outcomes):
                it = iter(outcomes); sleeps = []
                g = self._guard(lambda: 1.0, [], [])
                with patch.object(night, 'run_cycle', side_effect=lambda *a, **k: next(it)), patch('builtins.print') as pr:
                    rc = night.run_continuous(['x'], g, result_file=Path(tmp) / 'r', idle_s=900, sleep=sleeps.append,
                                              cycles=len(outcomes), stopflag=flag)
                return rc, sleeps, '\n'.join(str(c.args[0]) for c in pr.call_args_list if c.args)
            rc, sleeps, out = loop([(130, 'idle'), (0, 'worked')])
            self.assertEqual((rc, sleeps), (0, [900])); self.assertIn('external TERM', out)
            flag.touch()
            rc, sleeps, out = loop([(130, 'idle'), (0, 'worked')])
            # a flag present before the first cycle: the pre-cycle check returns 0 without running any cycle
            self.assertEqual((rc, sleeps), (0, [])); self.assertIn('stop flag', out); self.assertIn('not relaunching', out)
            flag.unlink()
            calls = iter([(130, 'idle'), (0, 'worked')])
            def cycle(*a, **k):
                flag.touch(); return next(calls)
            g = self._guard(lambda: 1.0, [], [])
            with patch.object(night, 'run_cycle', side_effect=cycle), patch('builtins.print'):
                rc = night.run_continuous(['x'], g, result_file=Path(tmp) / 'r2', idle_s=900, sleep=lambda s: None,
                                          cycles=2, stopflag=flag)
            self.assertEqual(rc, 130)



class Continuous(unittest.TestCase):
    def guard(self, lats, mems=None):
        sig = []; t = [0.0]
        lat_it = iter(lats); mem_it = iter(mems or [100.0] * len(lats))
        g = night.Guard(latency=lambda: next(lat_it), memory=lambda: next(mem_it), clock=lambda: t[0],
                        signaller=lambda pid, s: sig.append(s), budget_s=1.0, min_free_gb=8, interval_s=30, max_pause_s=1800,
                        log=lambda m: None, alerter=lambda n, d: None)
        return g, sig, t

    def test_latency_pause_and_hysteresis_resume(self):
        g, sig, t = self.guard([1.0, 2.5, 1.1, 1.0, 1.2])
        g.idle_probe()
        for _ in range(3):
            t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP]); self.assertTrue(g.paused)   # 2 good probes: not yet
        t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP, signal.SIGCONT]); self.assertFalse(g.paused)

    def test_probe_rate_limited(self):
        g, sig, t = self.guard([1.0, 9.0])
        g.idle_probe(); t[0] += 5; g.tick(1)
        self.assertEqual(sig, [])

    def test_memory_guard(self):
        g, sig, t = self.guard([1.0, 1.0], [4.0])
        g.idle_probe(); t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP])

    def _loop(self, outcomes, idle_s=900):
        """Drive run_continuous with scripted (rc, worked) cycles; returns the sleeps between cycles (poll sleeps excluded)."""
        it = iter(outcomes); sleeps = []
        g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                        log=lambda m: None, alerter=lambda n, d: None)
        with patch.object(night, 'run_cycle', side_effect=lambda *a, **k: next(it)):
            rc = night.run_continuous(['x'], g, result_file=Path('/nonexistent'), idle_s=idle_s, sleep=sleeps.append,
                                      cycles=len(outcomes))
        return rc, sleeps

    def test_worked_cycle_restarts_at_once(self):
        self.assertEqual(self._loop([(0, True), (0, True)])[1], [])

    def test_idle_cycle_sleeps_idle_interval(self):
        self.assertEqual(self._loop([(0, False), (0, False)])[1], [900, 900])

    def test_failures_and_quota_stop_back_off_exponentially_capped_and_reset(self):
        _, sleeps = self._loop([(1, False), (3, True), (2, False)] + [(3, False)] * 6 + [(0, True), (1, False)])
        self.assertEqual(sleeps, [900, 1800, 3600, 7200, 14400, 14400, 14400, 14400, 14400, 900])

    def test_stop_request_ends_the_loop(self):
        rc, sleeps = self._loop([(0, True), (130, False), (0, True)])
        self.assertEqual((rc, sleeps), (130, []))

    def test_run_cycle_reads_worked_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                        log=lambda m: None, alerter=lambda n, d: None)
            w = [sys.executable, '-c', 'import os;open(os.environ["CORTADOS_S1_CYCLE_RESULT"],"w").write("worked")']
            self.assertEqual(night.run_cycle(w, g, result_file=rf, poll_s=0, sleep=lambda s: None), (0, 'worked'))
            self.assertEqual(night.run_cycle([sys.executable, '-c', 'pass'], g, result_file=rf, poll_s=0,
                                             sleep=lambda s: None), (0, 'idle'))   # stale marker removed; absence = idle

    def test_scorer_plist_follows_env(self):
        with patch.dict(os.environ, {'CORTADOS_S1_SCORER_PLIST': '/tmp/x.plist'}):
            self.assertEqual(night.scorer_plist(), Path('/tmp/x.plist'))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CORTADOS_S1_SCORER_PLIST', None)
            self.assertTrue(str(night.scorer_plist()).endswith('com.cortados.s1-scorer.plist'))

    def test_term_while_paused_conts_first(self):
        proc = Mock(pid=7); proc.wait.return_value = 0
        sent = []   # every stop send goes through os.killpg (send_group_drained); stub it so pid 7 is never signalled
        with patch.object(night.os, 'killpg', lambda pgid, sig: sent.append(sig)):
            night.stop_tree(proc, grace=.1)
        self.assertEqual(sent[:2], [signal.SIGCONT, signal.SIGTERM])

    def test_real_stopped_child_terminates(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'], start_new_session=True)
        os.killpg(p.pid, signal.SIGSTOP)
        night.stop_tree(p, grace=5)
        self.assertEqual(p.returncode, -signal.SIGTERM)

    def test_min_left_default_15_and_env(self):
        root = Path(__file__).resolve().parents[1] / 's1'
        self.assertIn('MIN_LEFT_PCT_DEFAULT = 15.0', (root / 'label.py').read_text())
        text = (root / 'nightly.sh').read_text()   # validated in nightly.sh, then passed on (behaviour: test_s1_nightly min_left)
        self.assertIn('MIN_LEFT_PCT="${CORTADOS_S1_MIN_LEFT_PCT:-15}"', text)
        self.assertIn('--min-left-pct "$MIN_LEFT_PCT"', text)


import http.server
import threading


class ReviewFixes(unittest.TestCase):
    """s1-continuous review fixes: bounded pause, SIGCONT on exit, quota marker, env minimums, stop flag, pidfile, HTTP probe."""

    def mkguard(self, latency, *, max_pause_s=1800):
        sig, logs, alerts, t = [], [], [], [0.0]
        g = night.Guard(latency=latency, memory=lambda: 100.0, clock=lambda: t[0], signaller=lambda pid, s: sig.append(s),
                        budget_s=1.0, min_free_gb=8, interval_s=30, max_pause_s=max_pause_s, log=logs.append,
                        alerter=lambda n, d: alerts.append((n, d)))
        return g, sig, logs, alerts, t

    def test_unreachable_scorer_pause_released_by_unreadable_count(self):
        g, sig, logs, alerts, t = self.mkguard(lambda: None)
        for _ in range(night.Guard.MAX_UNREADABLE + 1):
            t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP, signal.SIGCONT]); self.assertFalse(g.paused)
        self.assertTrue(any('FORCED RESUME' in m for m in logs)); self.assertEqual(alerts[0][0], 'guard_forced_resume')
        for _ in range(5):
            t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP, signal.SIGCONT])   # blind after a forced release: no re-pause on no data

    def test_pause_released_after_max_pause(self):
        lats = iter([1.0] + [9.0] * 100)
        g, sig, logs, alerts, t = self.mkguard(lambda: next(lats), max_pause_s=120)
        g.idle_probe()
        for _ in range(5):
            t[0] += 30; g.tick(1)
        self.assertEqual(sig[:2], [signal.SIGSTOP, signal.SIGCONT])
        self.assertIn('CORTADOS_S1_MAX_PAUSE_S', alerts[0][1])

    def test_cycle_ending_while_paused_sends_sigcont(self):
        g, sig, logs, alerts, t = self.mkguard(lambda: None)
        g.paused = True; g.good = 2
        with tempfile.TemporaryDirectory() as tmp:
            rc, res = night.run_cycle([sys.executable, '-c', 'pass'], g, result_file=Path(tmp) / 'r', poll_s=0,
                                      sleep=lambda s: None)
        self.assertEqual((rc, res), (0, 'idle'))
        self.assertEqual(sig[-1], signal.SIGCONT); self.assertFalse(g.paused); self.assertEqual(g.good, 0)

    def test_quota_marker_cools_down(self):
        self.assertEqual(night.next_delay(0, 'quota', 900, 2, 18000), (18000, 0))
        self.assertEqual(night.next_delay(3, 'quota', 900, 0, 18000), (18000, 1))
        self.assertEqual(night.next_delay(0, 'worked', 900, 0, 18000), (0.0, 0))
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; rf.write_text('quota\n')
            self.assertEqual(night._read_result(rf), 'quota')
            rf.write_text('bogus'); self.assertEqual(night._read_result(rf), 'idle')

    def test_env_float_rejections(self):
        cases = [('CORTADOS_S1_CYCLE_IDLE_S', '30', dict(minimum=60)), ('CORTADOS_S1_PROBE_S', '0', dict(above=0)),
                 ('CORTADOS_S1_LATENCY_BUDGET_S', '-1', dict(minimum=0)), ('CORTADOS_S1_MIN_FREE_GB', '-2', dict(minimum=0)),
                 ('CORTADOS_S1_MAX_PAUSE_S', '10', dict(minimum=60)), ('CORTADOS_S1_QUOTA_COOLDOWN_S', '5', dict(minimum=60)),
                 ('CORTADOS_S1_PROBE_S', 'nan', dict(above=0)), ('CORTADOS_S1_LATENCY_BUDGET_S', 'inf', dict(minimum=0)),
                 ('CORTADOS_S1_CYCLE_IDLE_S', 'abc', dict(minimum=60))]
        for name, raw, kw in cases:
            with self.subTest(name=name, raw=raw), patch.dict(os.environ, {name: raw}), patch('builtins.print') as pr:
                self.assertEqual(night._env_float(name, 123.0, **kw), 123.0)
                self.assertIn(name, pr.call_args[0][0])
        with patch.dict(os.environ, {'CORTADOS_S1_PROBE_S': '5'}):
            self.assertEqual(night._env_float('CORTADOS_S1_PROBE_S', 30.0, above=0), 5.0)
        with patch.dict(os.environ, {'CORTADOS_S1_PROBE_S': '0'}), patch('builtins.print'):
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, log=lambda m: None, alerter=lambda n, d: None)
            self.assertEqual(g.interval, 30.0)

    def test_stop_flag_prevents_relaunch(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / 'nightly.stop'; flag.touch()
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0,
                            max_pause_s=1800, log=lambda m: None, alerter=lambda n, d: None)
            with patch.object(night, 'run_cycle') as rc, patch('builtins.print') as pr:
                self.assertEqual(night.run_continuous(['x'], g, result_file=Path(tmp) / 'r', idle_s=900, quota_s=18000,
                                                      stopflag=flag, cycles=3, sleep=lambda s: None), 0)
            rc.assert_not_called(); self.assertIn('stop flag', pr.call_args[0][0])

    def test_failure_count_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; sleeps = []
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0,
                            max_pause_s=1800, log=lambda m: None, alerter=lambda n, d: None)
            for _ in range(2):
                with patch.object(night, 'run_cycle', return_value=(1, 'idle')), patch('builtins.print'):
                    night.run_continuous(['x'], g, result_file=rf, idle_s=900, quota_s=18000, cycles=1, sleep=sleeps.append)
            self.assertEqual(sleeps, [900, 1800])

    def test_recover_pidfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / 'nightly.pid'; logs = []
            pf.write_text('4242\n'); sent = []
            night.recover_pidfile(pf, alive=lambda p: True, ps=lambda p: 'T', signaller=lambda p, s: sent.append((p, s)),
                                  log=logs.append)
            self.assertEqual(sent, [(4242, signal.SIGCONT)]); self.assertTrue(pf.exists())
            sent.clear()
            night.recover_pidfile(pf, alive=lambda p: True, ps=lambda p: 'S', signaller=lambda p, s: sent.append(s),
                                  log=logs.append)
            self.assertEqual(sent, [])
            night.recover_pidfile(pf, alive=lambda p: False, ps=lambda p: 'T', signaller=lambda p, s: sent.append(s),
                                  log=logs.append)
            self.assertFalse(pf.exists()); self.assertEqual(sent, [])
            self.assertTrue(any('stale pidfile' in m for m in logs))

    def test_run_cycle_clears_own_pidfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / 'nightly.pid'
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0,
                            max_pause_s=1800, log=lambda m: None, alerter=lambda n, d: None)
            w = [sys.executable, '-c', 'import os,sys;open(sys.argv[1],"w").write(str(os.getpid()))', str(pf)]
            night.run_cycle(w, g, result_file=Path(tmp) / 'r', poll_s=0, sleep=lambda s: None, pidfile=pf)
            self.assertFalse(pf.exists())

    def test_parse_vm_stat(self):
        full = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:      100000.\n"
                "Pages active:   5.\nPages inactive:  200000.\nPages speculative:  50000.\n")
        self.assertAlmostEqual(night.parse_vm_stat(full), 350000 * 16384 / 1e9)
        self.assertIsNone(night.parse_vm_stat(full.split("Pages inactive")[0]))   # truncated sample
        self.assertIsNone(night.parse_vm_stat(""))

    def test_alert_written_to_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = Path(tmp) / 'status.json'
            line = night.alert('guard_forced_resume', 'paused 1800s', status=st, log=lambda m: None)
            self.assertTrue(line.startswith('ALERT: guard_forced_resume'))
            import json
            self.assertIn('guard_forced_resume', json.loads(st.read_text())['alerts'])

    def test_probe_http_codes_and_url_resolution(self):
        codes = []
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                codes.append(self.headers.get('Authorization'))
                self.send_response(self.server.code); self.end_headers(); self.wfile.write(b'{}')
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            env = {'CORTADOS_S1_URL': f'http://127.0.0.1:{srv.server_port}', 'CORTADOS_S1_TOKEN': 'tok'}
            with patch.dict(os.environ, env):
                srv.code = 200
                self.assertIsInstance(night.probe_scorer_latency(timeout=5), float)
                self.assertEqual(codes[-1], 'Bearer tok')
                srv.code = 503
                with self.assertRaisesRegex(night.ScorerRefused, r'scorer refused \(HTTP 503\)'):
                    night.probe_scorer_latency(timeout=5)
                srv.code = 401
                with self.assertRaises(night.ScorerConfigError):
                    night.probe_scorer_latency(timeout=5)
        finally:
            srv.shutdown(); srv.server_close()

    def test_refused_is_violation_config_error_is_not(self):
        def refused():
            raise night.ScorerRefused('scorer refused (HTTP 503)')
        lats = iter([1.0])
        g, sig, logs, alerts, t = self.mkguard(lambda: next(lats))
        g.idle_probe(); g.latency = refused
        t[0] += 30; g.tick(1)
        self.assertEqual(sig, [signal.SIGSTOP]); self.assertTrue(any('HTTP 503' in m for m in logs))
        def bad():
            raise night.ScorerConfigError('HTTP 401')
        g2, sig2, logs2, alerts2, t2 = self.mkguard(lambda: 1.0)
        g2.idle_probe(); g2.latency = bad
        for _ in range(3):
            t2[0] += 30; g2.tick(1)
        self.assertEqual(sig2, []); self.assertEqual([a[0] for a in alerts2], ['scorer_probe_config'])   # once per cycle


class ReviewFixesRound2(unittest.TestCase):
    """Release hold-off, stale-marker idle, backoff clamp, no scorer URL in alerts/logs."""

    def test_persistent_503_still_runs_at_least_40_percent(self):
        def refused():
            raise night.ScorerRefused('scorer refused (HTTP 503)')
        sig, alerts, t = [], [], [0.0]
        g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, clock=lambda: t[0], signaller=lambda p, s: sig.append(s),
                        budget_s=1.0, min_free_gb=8, interval_s=30, max_pause_s=1800, log=lambda m: None,
                        alerter=lambda n, d: alerts.append(d))
        g.idle_probe(); g.latency = refused
        running = 0.0
        while t[0] < 7200:
            g.tick(1)
            if not g.paused:
                running += 1
            t[0] += 1
        self.assertGreaterEqual(running / 7200, 0.40, sig)
        self.assertGreaterEqual(len(alerts), 2)
        self.assertIn('forced release #1 in a row', alerts[0]); self.assertIn('forced release #2 in a row', alerts[1])
        self.assertIn('CORTADOS_S1_RELEASE_HOLD_S', alerts[0])

    def test_release_hold_env_default_and_minimum(self):
        mk = lambda: night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, max_pause_s=900, log=lambda m: None,
                                 alerter=lambda n, d: None)
        with patch.dict(os.environ):   # restored on exit: the key's absence must not leak into later tests
            os.environ.pop('CORTADOS_S1_RELEASE_HOLD_S', None)
            self.assertEqual(mk().release_hold, 900)
        with patch.dict(os.environ, {'CORTADOS_S1_RELEASE_HOLD_S': '30'}), patch('builtins.print'):
            self.assertEqual(mk().release_hold, 900)   # below the 60 s minimum: the default
        with patch.dict(os.environ, {'CORTADOS_S1_RELEASE_HOLD_S': '120'}):
            self.assertEqual(mk().release_hold, 120)

    def test_unremovable_stale_marker_reads_as_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; rf.write_text('worked\n')
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                            log=lambda m: None, alerter=lambda n, d: None)
            real_unlink = Path.unlink
            def unlink(self, *a, **k):
                if self == rf:
                    raise PermissionError(13, 'Permission denied')
                return real_unlink(self, *a, **k)
            with patch.object(Path, 'unlink', unlink), patch('builtins.print') as pr:
                self.assertEqual(night.run_cycle([sys.executable, '-c', 'pass'], g, result_file=rf, poll_s=0,
                                                 sleep=lambda s: None), (0, 'idle'))
            self.assertTrue(any('treated as idle' in str(c.args[0]) for c in pr.call_args_list))

    def test_huge_failure_count_does_not_overflow(self):
        delay, failures = night.next_delay(1, 'idle', 900, 2000)
        self.assertEqual(delay, night.BACKOFF_CAP_S); self.assertLessEqual(failures, night.MAX_FAILURES)
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; Path(tmp, 'r.failures').write_text('99999999999999999999\n')
            self.assertEqual(night._load_failures(rf), night.MAX_FAILURES)

    def test_scorer_url_never_in_alert_or_log(self):
        secret = 'http://192.0.2.9:8431'
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(403); self.end_headers()
            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            logs, alerts = [], []
            local = f'http://127.0.0.1:{srv.server_port}'
            with patch.dict(os.environ, {'CORTADOS_S1_URL': local}):
                g = night.Guard(latency=lambda: night.probe_scorer_latency(timeout=5), memory=lambda: 100.0, budget_s=1,
                                min_free_gb=8, interval_s=0, max_pause_s=1800, log=logs.append,
                                alerter=lambda n, d: alerts.append(d))
                g.idle_probe()
            text = '\n'.join(logs + alerts)
            self.assertNotIn(local, text); self.assertNotIn('127.0.0.1', text); self.assertIn('CORTADOS_S1_URL', text)
            # an unresolvable URL: the resolver quotes it, the error must not
            with patch.dict(os.environ, {'CORTADOS_S1_URL': secret.replace('http', 'ftp')}):
                with self.assertRaises(night.ScorerConfigError) as cm:
                    night.scorer_url()
            self.assertNotIn('192.0.2.9', str(cm.exception)); self.assertIn('CORTADOS_S1_URL', str(cm.exception))
            # unreachable: the OSError text is redacted too
            with patch.dict(os.environ, {'CORTADOS_S1_URL': secret}), patch('builtins.print') as pr, \
                    patch.object(night.urllib.request, 'urlopen', side_effect=OSError(f'cannot reach {secret}/score')):
                self.assertIsNone(night.probe_scorer_latency(timeout=1))
            self.assertNotIn('192.0.2.9', pr.call_args[0][0])
        finally:
            srv.shutdown(); srv.server_close()



class ReviewFixesRound4(unittest.TestCase):
    """Malformed URL / unexpected probe errors never kill the supervisor; resets are outages; no host text leaks."""

    def _guard(self, latency, logs, alerts):
        return night.Guard(latency=latency, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, max_pause_s=1800,
                           log=logs.append, alerter=lambda n, d: alerts.append((n, d)))

    def _survives(self, latency, host, name='scorer_probe_config'):
        logs, alerts = [], []
        with patch('builtins.print') as pr:
            g = self._guard(latency, logs, alerts)
            g.idle_probe(); g.idle_probe()   # must not raise; second probe in the same cycle does not re-alert
        printed = '\n'.join(str(c.args[0]) for c in pr.call_args_list if c.args)
        text = '\n'.join(logs + [d for _, d in alerts]) + printed
        self.assertEqual([n for n, _ in alerts], [name])
        if host:
            self.assertNotIn(host, text)
        return text

    def test_malformed_urls_are_config_errors_without_the_host(self):
        for value, host in (('http://[2001:db8::9:8431', '2001:db8'), ('http://studio-host:abc', 'studio-host'),
                            ('http://', None), ('http:///score', None), ('http://:8431/score', None)):
            with self.subTest(value=value), patch.dict(os.environ, {'CORTADOS_S1_URL': value}), \
                    patch.object(night.urllib.request, 'urlopen', wraps=night.urllib.request.urlopen) as urlopen:
                with self.assertRaises(night.ScorerConfigError) as cm:
                    night.probe_scorer_latency(timeout=1)
                if host:
                    self.assertNotIn(host, str(cm.exception))
                self.assertIn('CORTADOS_S1_URL', str(cm.exception))
                self.assertIsNone(cm.exception.__cause__); self.assertTrue(cm.exception.__suppress_context__)
                self._survives(lambda: night.probe_scorer_latency(timeout=1), host)
                if host is None:   # a hostless URL is refused before any connection attempt
                    urlopen.assert_not_called()

    def test_unexpected_probe_exception_is_a_config_error_by_type_name(self):
        def boom():
            raise RuntimeError('cannot talk to http://secret-host:8431/score')
        text = self._survives(boom, 'secret-host', name='scorer_probe_crashed')
        self.assertIn('RuntimeError', text)

    def test_unexpected_probe_exception_does_not_kill_a_running_cycle(self):
        logs, alerts = [], []
        def boom():
            raise RuntimeError('secret-host')
        g = self._guard(boom, logs, alerts)
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.print'):
            rc, _ = night.run_cycle([sys.executable, '-c', 'import time; time.sleep(0.2)'], g,
                                    result_file=Path(tmp) / 'r', poll_s=0.05)
        # (no baseline: the existing pause-to-measure / unreadable-release path may also alert; only the config alert is ours)
        self.assertEqual(rc, 0); self.assertEqual([n for n, _ in alerts].count('scorer_probe_crashed'), 1, alerts)
        self.assertNotIn('secret-host', repr(alerts) + '\n'.join(logs))

    def test_dropped_connection_is_an_outage_not_a_config_alert(self):
        import socket, threading
        srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(5)
        port = srv.getsockname()[1]
        def serve():
            for _ in range(2):
                try:
                    c, _ = srv.accept(); c.recv(65536); c.close()
                except OSError:
                    return
        th = threading.Thread(target=serve, daemon=True); th.start()
        try:
            logs, alerts = [], []
            url = f'http://127.0.0.1:{port}/score'
            with patch('builtins.print') as pr:
                self.assertIsNone(night.probe_scorer_latency(url=url, timeout=5))
                g = self._guard(lambda: night.probe_scorer_latency(url=url, timeout=5), logs, alerts)
                g.idle_probe()
            self.assertEqual(alerts, []); self.assertFalse(any('CONFIG' in m for m in logs))
            self.assertIn('latency unknown', pr.call_args_list[0][0][0])
        finally:
            srv.close(); th.join(2)

    def test_bare_host_in_os_error_is_not_printed(self):
        import ssl
        err = ssl.SSLCertVerificationError(1, "hostname 'studio.example.ts.net' doesn't match")
        for exc in (err, night.urllib.error.URLError(err), OSError(111, 'refused studio.example.ts.net:8431')):
            with self.subTest(exc=type(exc).__name__), patch('builtins.print') as pr, \
                    patch.object(night.urllib.request, 'urlopen', side_effect=exc):
                self.assertIsNone(night.probe_scorer_latency(url='http://127.0.0.1:9/score', timeout=1))
            self.assertNotIn('studio.example', pr.call_args[0][0])

    def test_resolver_errors_keep_a_value_free_class_and_a_working_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            ep = Path(tmp) / 'ep.json'; ep.write_text('{"endpoints": {"studio": "http://192.0.2.9:8431"}}')
            cases = (({'CORTADOS_S1_URL': '192.0.2.9:8431'}, 'not an http(s) URL'),
                     ({'CORTADOS_S1_ENDPOINTS': str(Path(tmp) / 'missing.json')}, 'does not exist'),
                     ({'CORTADOS_S1_ENDPOINTS': str(ep), 'CORTADOS_S1_ENDPOINT': 'stuido'}, 'not in the endpoints file'))
            for env, cls in cases:
                clean = {k: v for k, v in os.environ.items() if not k.startswith('CORTADOS_S1_')}
                with self.subTest(cls=cls), patch.dict(os.environ, {**clean, **env}, clear=True):
                    with self.assertRaises(night.ScorerConfigError) as cm:
                        night.scorer_url()
                    msg = str(cm.exception)
                    self.assertIn(cls, msg); self.assertNotIn('192.0.2.9', msg); self.assertNotIn('stuido', msg)
                    self.assertIn('s1_endpoint.resolve_url()', msg)
            # the hint is a command that works from the repo root
            out = subprocess.run([sys.executable, '-c', 'from cortados_router import s1_endpoint; print(s1_endpoint.resolve_url())'],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                                 env={**{k: v for k, v in os.environ.items() if not k.startswith('CORTADOS_S1_')},
                                      'CORTADOS_ROUTER_TABLES': tmp})   # no machine-local s1-endpoints.json decides it
            self.assertEqual(out.returncode, 0, out.stderr); self.assertTrue(out.stdout.startswith('http'))

    def test_unremovable_marker_alerts_once_per_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'; rf.write_text('worked\n')
            logs, alerts = [], []
            g = self._guard(lambda: 1.0, logs, alerts)
            real_unlink = Path.unlink
            def unlink(self, *a, **k):
                if self == rf:
                    raise PermissionError(13, 'Permission denied')
                return real_unlink(self, *a, **k)
            with patch.object(Path, 'unlink', unlink), patch('builtins.print') as pr:
                for _ in range(2):
                    self.assertEqual(night.run_cycle([sys.executable, '-c', 'pass'], g, result_file=rf, poll_s=0,
                                                     sleep=lambda s: None), (0, 'idle'))
            self.assertEqual([n for n, _ in alerts], ['cycle_result_stuck'])
            self.assertNotIn(tmp, alerts[0][1]); self.assertIn('cannot remove r ', alerts[0][1])   # name only, no path
            self.assertIn("'quota' result is still honoured", '\n'.join(str(c.args[0]) for c in pr.call_args_list))

    def test_stop_flag_removed_during_grace_is_still_a_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / 'nightly.stop'
            # the child creates the flag (as --stop would), then removes it (operator resumes) and exits 130
            code = (f'import pathlib,time; p=pathlib.Path({str(flag)!r}); p.touch(); time.sleep(0.3); p.unlink(); '
                    'raise SystemExit(130)')
            g = self._guard(lambda: 1.0, [], [])
            sleeps = []
            with patch('builtins.print'):
                rc = night.run_continuous([sys.executable, '-c', code], g, result_file=Path(tmp) / 'r', idle_s=900,
                                          sleep=lambda s: (sleeps.append(s) if s >= 60 else _time.sleep(s)), cycles=2,
                                          stopflag=flag, poll_s=0.05)
            self.assertEqual((rc, sleeps), (130, []))
            self.assertFalse((Path(tmp) / 'r.failures').exists() and (Path(tmp) / 'r.failures').read_text().strip() != '0')

    def test_torn_reply_is_an_outage_not_a_config_error(self):
        """IncompleteRead/BadStatusLine are HTTPExceptions and NOT OSErrors: a scorer mid-restart is an outage."""
        for exc in (http.client.IncompleteRead(b'partial'), http.client.BadStatusLine('garbage')):
            self.assertFalse(isinstance(exc, OSError))   # the OSError arm alone would not catch it
            with self.subTest(exc=type(exc).__name__), patch('builtins.print') as pr, \
                    patch.object(night.urllib.request, 'urlopen', side_effect=exc):
                self.assertIsNone(night.probe_scorer_latency(url='http://127.0.0.1:9/score', timeout=1))
            self.assertIn('latency unknown', pr.call_args[0][0])

    def test_stop_signals_from_the_probe_still_stop_the_supervisor(self):
        """main() turns SIGTERM into KeyboardInterrupt; the broad probe catch must be Exception, never BaseException."""
        for exc in (KeyboardInterrupt, SystemExit):
            def boom(exc=exc):
                raise exc
            g = self._guard(boom, [], [])
            with self.subTest(exc=exc.__name__), self.assertRaises(exc):
                g.idle_probe()



class NightWindowBudget(unittest.TestCase):
    """Forest 2026-11-07 option (a): a local-time night window with a looser latency budget; memory guard unchanged."""

    def _guard(self, now, lat, mem=100.0, window='01:00-07:00', logs=None, sig=None):
        logs = [] if logs is None else logs
        sig = [] if sig is None else sig
        lats = iter(lat)
        g = night.Guard(latency=lambda: next(lats), memory=lambda: mem, budget_s=1, min_free_gb=8, interval_s=0,
                        max_pause_s=1800, signaller=lambda pid, s: sig.append(s), log=logs.append,
                        alerter=lambda n, d: None, wall=lambda: now[0],
                        night_window=night.parse_night_window(window), night_budget_s=6.0)
        g.baseline_samples = [2.0]
        return g

    def test_inside_window_uses_night_budget(self):
        now, logs, sig = [datetime(2026, 11, 7, 3, 0)], [], []
        g = self._guard(now, [5.5, 8.5], logs=logs, sig=sig)
        g.tick(42)
        self.assertEqual(sig, [], '5.5 s is within 2.0 + 6.0 at night')
        g.tick(42)
        self.assertEqual(sig, [signal.SIGSTOP], '8.5 s is above the night ceiling: a stalled scorer still pauses')
        self.assertIn('night budget 6s (01:00-07:00 local', logs[-1])

    def test_outside_window_keeps_day_budget(self):
        now, logs, sig = [datetime(2026, 11, 7, 12, 0)], [], []
        g = self._guard(now, [3.5], logs=logs, sig=sig)
        g.tick(42)
        self.assertEqual(sig, [signal.SIGSTOP])
        self.assertIn('day budget 1s', logs[-1])
        self.assertEqual(g.current_budget()[0], 1)

    def test_window_edges_are_start_inclusive_end_exclusive(self):
        w = night.parse_night_window('01:00-07:00')
        self.assertTrue(night.in_night_window(w, datetime(2026, 11, 7, 1, 0)))
        self.assertTrue(night.in_night_window(w, datetime(2026, 11, 7, 6, 59)))
        self.assertFalse(night.in_night_window(w, datetime(2026, 11, 7, 7, 0)))
        self.assertFalse(night.in_night_window(w, datetime(2026, 11, 7, 0, 59)))
        self.assertFalse(night.in_night_window(None, datetime(2026, 11, 7, 3, 0)))

    def test_window_crossing_midnight(self):
        w = night.parse_night_window('23:00-05:00')
        for h, inside in ((22, False), (23, True), (0, True), (4, True), (5, False), (12, False)):
            with self.subTest(hour=h):
                self.assertEqual(night.in_night_window(w, datetime(2026, 11, 7, h, 30 if h == 22 else 0)), inside)

    def test_local_wall_time_follows_dst(self):
        # 2026-11-01 02:00 EDT -> 01:00 EST in Toronto: the same instant-based clock reads local wall time on both sides.
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/Toronto')
        w = night.parse_night_window('01:00-07:00')
        before = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).astimezone(tz)   # 01:30 EDT
        after = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc).astimezone(tz)   # 07:30 EST
        self.assertTrue(night.in_night_window(w, before))
        self.assertFalse(night.in_night_window(w, after))
        self.assertTrue(night.in_night_window(w, datetime(2026, 11, 1, 11, 30, tzinfo=timezone.utc).astimezone(tz)))  # 06:30 EST

    def test_invalid_window_rejected(self):
        for bad in ('', '1-7', '25:00-07:00', '01:60-07:00', '01:00-01:00', '01:00', 'night'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                night.parse_night_window(bad)
        self.assertIsNone(night.parse_night_window('off'))

    def test_invalid_env_window_reported_and_default_used(self):
        with patch.dict(os.environ, {'CORTADOS_S1_NIGHT_WINDOW': '25:00-07:00'}), patch('builtins.print') as pr:
            self.assertEqual(night._env_night_window(), night.parse_night_window('01:00-07:00'))
        self.assertIn('CORTADOS_S1_NIGHT_WINDOW', str(pr.call_args.args[0]))
        with patch.dict(os.environ, {'CORTADOS_S1_NIGHT_WINDOW': '22:30-06:00'}):
            self.assertEqual(night._env_night_window(), (night.time(22, 30), night.time(6, 0)))

    def test_night_budget_below_day_rejected(self):
        with self.assertRaises(ValueError):
            night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, night_budget_s=0.5,
                        night_window=night.parse_night_window('01:00-07:00'), log=lambda m: None, alerter=lambda n, d: None)
        with patch.dict(os.environ, {'CORTADOS_S1_NIGHT_LATENCY_BUDGET_S': '0.5'}), patch('builtins.print') as pr:
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, log=lambda m: None, alerter=lambda n, d: None)
        self.assertEqual(g.night_budget, 6.0)
        self.assertIn('below the day budget', str(pr.call_args.args[0]))

    def test_env_defaults(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('CORTADOS_S1_')}
        with patch.dict(os.environ, env, clear=True):
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, log=lambda m: None, alerter=lambda n, d: None)
        self.assertEqual((g.budget, g.night_budget, g.night_window), (1.0, 6.0, (night.time(1), night.time(7))))

    def test_memory_guard_still_pauses_at_night(self):
        now, logs, sig = [datetime(2026, 11, 7, 3, 0)], [], []
        g = self._guard(now, [2.1], mem=4.0, logs=logs, sig=sig)
        g.tick(42)
        self.assertEqual(sig, [signal.SIGSTOP])
        self.assertIn('free memory 4.0 GB < 8 GB', logs[-1])
        self.assertIn('night budget', logs[-1])

    def test_resume_logs_budget_and_switches_at_window_end(self):
        now, logs, sig = [datetime(2026, 11, 7, 6, 58)], [], []
        g = self._guard(now, [5.0, 5.0, 5.0, 5.0, 2.5, 2.5, 2.5], logs=logs, sig=sig)
        g.tick(42)
        self.assertEqual(sig, [])
        now[0] = datetime(2026, 11, 7, 7, 0)
        g.tick(42)
        self.assertEqual(sig, [signal.SIGSTOP], 'at 07:00 the day budget applies again')
        g.baseline_samples = [2.0]
        for _ in range(5):
            g.tick(42)
        self.assertEqual(sig, [signal.SIGSTOP, signal.SIGCONT])
        self.assertIn('RESUME', logs[-1]); self.assertIn('day budget 1s', logs[-1])

    def test_injected_window_validated_in_constructor(self):
        bad = ((night.time(1), night.time(1)), (night.time(1),), [night.time(1), night.time(7)], ('01:00', '07:00'),
               (1, 7), (night.time(1, tzinfo=timezone.utc), night.time(7)), '01:00-07:00')
        for w in bad:
            with self.subTest(window=w), self.assertRaises(ValueError):
                night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, night_budget_s=6.0, night_window=w,
                            log=lambda m: None, alerter=lambda n, d: None)
        for w in (None, (night.time(1), night.time(7)), (night.time(23), night.time(5))):
            with self.subTest(window=w):
                g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, night_budget_s=6.0, night_window=w,
                                log=lambda m: None, alerter=lambda n, d: None)
                self.assertEqual(g.night_window, w)


if __name__ == '__main__':
    unittest.main()


class TrainOnlyInWindow(unittest.TestCase):
    """Forest 2026-11-08: continuous mode trains (gated steps) only inside the night window; labeling runs all day."""

    def _guard(self, now):
        return night.Guard(latency=lambda: 2.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0,
                           max_pause_s=1800, signaller=lambda pid, s: None, log=lambda m: None,
                           alerter=lambda n, d: None, wall=now, night_window=night.parse_night_window('01:00-07:00'),
                           night_budget_s=6.0)

    def test_policy_outside_inside_switch_invalid_off(self):
        day, nite = datetime(2026, 11, 8, 14, 0), datetime(2026, 11, 8, 3, 0)
        ok, why, dl = night.train_policy(day, window_raw='01:00-07:00', switch_raw='1')
        self.assertFalse(ok)
        self.assertIn('training skipped: outside night window 01:00-07:00; next window starts 2026-11-09 01:00', why)
        self.assertIsNone(dl)
        ok, why, dl = night.train_policy(nite, window_raw='01:00-07:00', switch_raw='1')
        self.assertTrue(ok)
        self.assertEqual(dl, datetime(2026, 11, 8, 7, 0))
        self.assertEqual(night.train_policy(day, window_raw='01:00-07:00', switch_raw='0')[::2], (True, None))
        ok, why, _ = night.train_policy(nite, window_raw='25:00-07:00', switch_raw='1')
        self.assertFalse(ok)   # invalid window fails closed even at 03:00
        self.assertIn('invalid', why)
        self.assertFalse(night.train_policy(nite, window_raw='off', switch_raw='1')[0])
        # midnight-crossing window: deadline is tomorrow's end
        ok, _, dl = night.train_policy(datetime(2026, 11, 8, 23, 30), window_raw='23:00-05:00', switch_raw='1')
        self.assertEqual((ok, dl), (True, datetime(2026, 11, 9, 5, 0)))

    def _env_cmd(self, tmp):
        out = Path(tmp) / 'env'
        return out, [sys.executable, '-c', "import os,sys; open(sys.argv[1],'w').write("
                     "os.environ['CORTADOS_S1_TRAIN_ALLOWED']+'|'+os.environ['CORTADOS_S1_TRAIN_SKIP_REASON'])", str(out)]

    def test_outside_window_child_told_to_skip_and_loop_sleeps_idle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1',
                                                                          'CORTADOS_S1_NIGHT_WINDOW': '01:00-07:00'}):
            out, cmd = self._env_cmd(tmp)
            sleeps = []
            g = self._guard(lambda: datetime(2026, 11, 8, 14, 0))
            night.run_continuous(cmd, g, result_file=Path(tmp) / 'r', idle_s=900, sleep=sleeps.append, cycles=1, poll_s=0)
            allowed, reason = out.read_text().split('|', 1)
            self.assertEqual(allowed, '0')
            self.assertIn('outside night window', reason)
            self.assertEqual(sleeps[-1], 900)   # rc 0, no 'worked': idle interval, not a hot loop, not a failure backoff
            self.assertEqual(night._load_failures(Path(tmp) / 'r'), 0)

    def test_inside_window_and_switch_off_allow_training(self):
        for env, now in (({'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1'}, datetime(2026, 11, 8, 3, 0)),
                         ({'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '0'}, datetime(2026, 11, 8, 14, 0))):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**env, 'CORTADOS_S1_NIGHT_WINDOW': '01:00-07:00'}):
                out, cmd = self._env_cmd(tmp)
                rc, _ = night.run_cycle(cmd, self._guard(lambda: now), result_file=Path(tmp) / 'r', poll_s=0,
                                        sleep=lambda s: None)
                self.assertEqual((rc, out.read_text()), (0, '1|'))

    def test_invalid_window_means_no_training(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1',
                                                                          'CORTADOS_S1_NIGHT_WINDOW': 'nonsense'}):
            out, cmd = self._env_cmd(tmp)
            night.run_cycle(cmd, self._guard(lambda: datetime(2026, 11, 8, 3, 0)), result_file=Path(tmp) / 'r', poll_s=0,
                            sleep=lambda s: None)
            self.assertTrue(out.read_text().startswith('0|training skipped: CORTADOS_S1_NIGHT_WINDOW invalid'))

    def test_window_end_terminates_group_and_next_cycle_starts_at_once(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1',
                                                                          'CORTADOS_S1_NIGHT_WINDOW': '01:00-07:00'}):
            # a "trainer" grandchild in the same group that ignores nothing: TERM must reach it through the group
            marker = Path(tmp) / 'grandchild.pid'
            cmd = ['/bin/bash', '-c', f'{sys.executable} -c "import os,time; open(\'{marker}\',\'w\').write(str(os.getpid())); '
                   f'time.sleep(60)" & wait']
            clock = iter([datetime(2026, 11, 8, 6, 59)] + [datetime(2026, 11, 8, 6, 59, 30)] * 3
                         + [datetime(2026, 11, 8, 7, 0)] * 1000)
            ticks = []
            def sleep(s):
                ticks.append(s)
                _time.sleep(0.05)
            g = self._guard(lambda: next(clock))
            t0 = _time.monotonic()
            rc, res = night.run_cycle(cmd, g, result_file=Path(tmp) / 'r', poll_s=0, sleep=sleep, grace_end=5)
            self.assertEqual(res, 'window_end')
            self.assertLess(_time.monotonic() - t0, 30)
            pid = int(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                for _ in range(50):
                    os.kill(pid, 0)
                    _time.sleep(0.1)
            # the loop treats it as neither a failure nor a reason to wait
            sleeps = []
            clock2 = iter([datetime(2026, 11, 8, 6, 59)] + [datetime(2026, 11, 8, 7, 0)] * 1000)
            g2 = self._guard(lambda: next(clock2))
            night.run_continuous([sys.executable, '-c', 'import time; time.sleep(30)'], g2, result_file=Path(tmp) / 'r2',
                                 idle_s=900, sleep=lambda s: (sleeps.append(s), _time.sleep(0.01)), cycles=1, poll_s=0)
            self.assertNotIn(900, sleeps)
            self.assertEqual(night._load_failures(Path(tmp) / 'r2'), 0)


class WindowEndDrainAndDst(unittest.TestCase):
    """Review fixes: window end waits for the whole cycle group (P1); deadlines follow DST in a named zone (P2-a)."""

    def test_deadline_is_0700_wall_time_across_dst(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/Toronto')
        w = night.parse_night_window('01:00-07:00')
        for name, start in (('spring forward', datetime(2027, 3, 14, 1, 30, tzinfo=tz)),     # 01:30 EST, 07:00 is EDT
                            ('fall back', datetime(2026, 11, 1, 1, 30, tzinfo=tz))):         # 01:30 EDT, 07:00 is EST
            with self.subTest(name):
                # a fixed-offset clock (the old _local_now) must give the same deadline
                for now in (start, start.astimezone(start.tzinfo.utcoffset(start) and timezone(start.utcoffset()))):
                    ok, _, dl = night.train_policy(now, window_raw='01:00-07:00', switch_raw='1', zone=tz)
                    self.assertTrue(ok)
                    local = dl.astimezone(tz)
                    self.assertEqual((local.date(), local.hour, local.minute), (start.date(), 7, 0))
                    self.assertEqual(dl.timestamp(), datetime(start.year, start.month, start.day, 7, tzinfo=tz).timestamp())
                    self.assertLess(now, dl)
                end = datetime(start.year, start.month, start.day, 7, tzinfo=tz)
                self.assertTrue(night.in_night_window(w, end - night.timedelta(minutes=1), zone=tz))
                self.assertFalse(night.in_night_window(w, end, zone=tz))
                # the same instant read through UTC is still inside / outside
                self.assertTrue(night.in_night_window(w, (end - night.timedelta(minutes=1)).astimezone(timezone.utc), zone=tz))
                self.assertFalse(night.in_night_window(w, end.astimezone(timezone.utc), zone=tz))
        # spring forward: 01:30 EST -> 07:00 EDT is 4.5 real hours; fall back: 01:30 EDT -> 07:00 EST is 6.5
        spring = night.train_policy(datetime(2027, 3, 14, 1, 30, tzinfo=tz), window_raw='01:00-07:00', switch_raw='1', zone=tz)[2]
        self.assertEqual(spring.timestamp() - datetime(2027, 3, 14, 1, 30, tzinfo=tz).timestamp(), 4.5 * 3600)
        fall = night.train_policy(datetime(2026, 11, 1, 1, 30, tzinfo=tz), window_raw='01:00-07:00', switch_raw='1', zone=tz)[2]
        self.assertEqual(fall.timestamp() - datetime(2026, 11, 1, 1, 30, tzinfo=tz).timestamp(), 6.5 * 3600)

    def test_local_now_is_a_named_zone(self):
        self.assertIs(night._local_now().tzinfo, night.NIGHT_ZONE)

    # Hard bounds for the real-process fixtures: a slow machine FAILS the test, never hangs it or skips silently.
    READY_TIMEOUT_S = 30.0   # gate + trainer start (two Python interpreters) must be ready within this
    BACKSTOP_S = 30.0        # after the SIGTERM, the supervisor call must have returned within this (grace is 5 s)

    def _fixture_dir(self):
        """A temp dir that outlives the test body: cleanups (LIFO) kill the fixture groups first, then remove it."""
        import shutil
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(self._kill_fixture_groups, tmp)
        return tmp

    @staticmethod
    def _kill_fixture_groups(tmp, reap=True):
        """SIGKILL both fixture groups whatever the outcome: the cycle group (gate's pgid = the bash leader's pid) and the
        trainer's own session. Reaps the bash leader when it is still our unreaped child. A trainer that survives KILL fails."""
        for name in ('cycle.pgid', 'child.pid'):
            try:
                pgid = int((tmp / name).read_text())
            except FileNotFoundError:
                continue   # the fixture never got that far: there is no such group to kill
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue   # already drained: the normal outcome of a passing test
            except PermissionError:
                pass       # Darwin: zombie-only group; still reap below
            if name == 'cycle.pgid' and reap:
                try:
                    os.waitpid(pgid, 0)
                except ChildProcessError:
                    pass   # run_cycle/run_bounded's Popen already reaped the leader
            if name == 'child.pid':
                end = _time.monotonic() + 5
                while True:
                    try:
                        os.kill(pgid, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        break   # zombie awaiting launchd's reap: it can no longer hold the lock
                    if _time.monotonic() > end:
                        raise AssertionError(f'fixture trainer {pgid} survived SIGKILL')
                    _time.sleep(0.02)

    def _gate_cycle(self, tmp):
        """A cycle shaped like nightly.sh's gated(): bash leader -> gate (LOCK_EX) -> trainer in its own session holding the
        inherited lock fd, which takes ~1 s to stop after TERM. Returns (cmd, lock, ready, released, child_pid).
        The gate records the cycle group (cycle.pgid) and the trainer its own pid (child.pid) for _kill_fixture_groups."""
        lock, ready, released, child_pid = tmp / 'lock', tmp / 'ready', tmp / 'released', tmp / 'child.pid'
        # the "trainer": its own group, holds the inherited LOCK_EX fd, takes ~1 s to stop after TERM
        (tmp / 'trainer.py').write_text(
            "import os, signal, sys, time\n"
            "def term(*a):\n    time.sleep(1.0)\n    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\n"
            f"open({str(child_pid)!r}, 'w').write(str(os.getpid()))\n"
            "while True:\n    time.sleep(0.05)\n")
        # the "gate": takes LOCK_EX, starts the trainer in its own session with the fd, on TERM stops that group (3 s grace)
        (tmp / 'gate.py').write_text(
            "import fcntl, os, signal, subprocess, sys, time\n"
            f"open({str(tmp / 'cycle.pgid')!r}, 'w').write(str(os.getpgrp()))\n"
            f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            f"p = subprocess.Popen([sys.executable, {str(tmp / 'trainer.py')!r}], pass_fds=(fd,), start_new_session=True)\n"
            f"while not os.path.exists({str(child_pid)!r}):\n    time.sleep(0.02)\n"
            "def term(*a):\n"
            "    os.killpg(p.pid, signal.SIGTERM)\n"
            "    try:\n        p.wait(timeout=3)\n"
            "    except subprocess.TimeoutExpired:\n        os.killpg(p.pid, signal.SIGKILL); p.wait()\n"
            f"    open({str(released)!r}, 'w').write('x')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, term)\n"
            f"open({str(ready)!r}, 'w').write('x')\n"
            "while True:\n    time.sleep(0.05)\n")
        cmd = ['/bin/bash', '-c', f'{sys.executable} {tmp / "gate.py"}; echo done']
        return cmd, lock, ready, released, child_pid

    def _assert_gate_released(self, lock, released, child_pid):
        import fcntl
        self.assertTrue(released.exists(), 'the gate wrapper was killed before it stopped its trainer')
        fd = os.open(str(lock), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # raises if an orphaned trainer still holds the lock
        finally:
            os.close(fd)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid.read_text()), 0)

    def _sigterm_when(self, ready, tmp):
        """Deliver a real SIGTERM to this process once `ready` exists, with overnight.main's handler (-> KeyboardInterrupt).
        The SIGTERM must reach the main thread (that is where the handler raises), so the supervisor call stays on the main
        thread and this watchdog bounds it instead: no readiness within READY_TIMEOUT_S -> SIGTERM anyway and mark the test
        failed; call still running BACKSTOP_S later -> SIGKILL both fixture groups and mark it failed. Returns (state, done):
        the test sets `done` when the call returns and must assert state is clean."""
        import threading
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        old = signal.signal(signal.SIGTERM, interrupted)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        state = {'ready_timeout': False, 'backstop': False}
        done = threading.Event()
        def fire():
            end = _time.monotonic() + self.READY_TIMEOUT_S
            while not ready.exists():
                if done.is_set():
                    return   # the call returned before readiness; the test's own assertions report it
                if _time.monotonic() > end:
                    state['ready_timeout'] = True
                    break
                done.wait(0.02)
            if done.is_set():
                return
            os.kill(os.getpid(), signal.SIGTERM)
            if not done.wait(self.BACKSTOP_S):
                state['backstop'] = True
                self._kill_fixture_groups(tmp, reap=False)   # the main thread's Popen.wait reaps the leader
        t = threading.Thread(target=fire, daemon=True)
        t.start()
        self.addCleanup(t.join, self.BACKSTOP_S + self.READY_TIMEOUT_S + 5)
        self.addCleanup(done.set)
        return state, done

    def _assert_watchdog_clean(self, state):
        self.assertFalse(state['ready_timeout'], f'fixture not ready within {self.READY_TIMEOUT_S:g}s: test inconclusive, '
                         'failing rather than skipping')
        self.assertFalse(state['backstop'], f'supervisor call still running {self.BACKSTOP_S:g}s after SIGTERM')

    def test_sigterm_to_the_continuous_supervisor_drains_the_cycle_group(self):
        # launchd stop/restart on deploy: SIGTERM -> KeyboardInterrupt inside run_cycle's poll loop.
        tmp = self._fixture_dir()
        with patch.dict(os.environ, {'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1', 'CORTADOS_S1_NIGHT_WINDOW': '01:00-07:00'}):
            cmd, lock, ready, released, child_pid = self._gate_cycle(tmp)
            g = TrainOnlyInWindow._guard(self, lambda: datetime(2026, 11, 8, 3, 0))
            state, done = self._sigterm_when(ready, tmp)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    night.run_cycle(cmd, g, result_file=tmp / 'r', poll_s=0, sleep=lambda s: _time.sleep(0.05), grace=5)
            finally:
                done.set()
            self._assert_watchdog_clean(state)
            self._assert_gate_released(lock, released, child_pid)

    def test_sigterm_to_the_window_mode_supervisor_drains_the_group(self):
        tmp = self._fixture_dir()
        cmd, lock, ready, released, child_pid = self._gate_cycle(tmp)
        state, done = self._sigterm_when(ready, tmp)
        try:
            with self.assertRaises(KeyboardInterrupt):
                night.run_bounded(cmd, 120, grace=5)
        finally:
            done.set()
        self._assert_watchdog_clean(state)
        self._assert_gate_released(lock, released, child_pid)

    def test_window_mode_deadline_drains_the_group(self):
        tmp = self._fixture_dir()
        cmd, lock, ready, released, child_pid = self._gate_cycle(tmp)
        # The deadline must land after the fixture is up (gate handler installed, trainer started): 3 s covers two Python
        # starts; if it did not, the assertion below FAILS (a TERM before the gate's handler would kill it unreleased).
        self.assertEqual(night.run_bounded(cmd, 3.0, grace=5), 124)
        self.assertTrue(ready.exists(), 'fixture not ready before the 3 s deadline: test inconclusive, failing rather than '
                        'passing on a run that never exercised the gate')
        self._assert_gate_released(lock, released, child_pid)

    def test_window_end_waits_for_the_gate_wrapper_to_stop_its_own_group(self):
        tmp = self._fixture_dir()
        with patch.dict(os.environ, {'CORTADOS_S1_TRAIN_ONLY_IN_WINDOW': '1', 'CORTADOS_S1_NIGHT_WINDOW': '01:00-07:00'}):
            cmd, lock, ready, released, child_pid = self._gate_cycle(tmp)
            # in the window until the gate is ready, then past the deadline; a fixture that never gets ready fails the test
            t_ready = _time.monotonic() + self.READY_TIMEOUT_S
            def wall():
                if ready.exists():
                    return datetime(2026, 11, 8, 7, 0)
                if _time.monotonic() > t_ready:
                    raise AssertionError(f'fixture not ready within {self.READY_TIMEOUT_S:g}s')
                return datetime(2026, 11, 8, 6, 59)
            g = TrainOnlyInWindow._guard(self, wall)
            first = [True]
            def clock_wall():
                if first[0]:
                    first[0] = False
                    return datetime(2026, 11, 8, 6, 59)
                return wall()
            g.wall = clock_wall
            t0 = _time.monotonic()
            rc, res = night.run_cycle(cmd, g, result_file=tmp / 'r', poll_s=0, sleep=lambda s: _time.sleep(0.05), grace_end=5)
            self.assertEqual(res, 'window_end')
            self.assertLess(_time.monotonic() - t0, 20)
            self._assert_gate_released(lock, released, child_pid)

    def test_stop_on_an_already_zombie_only_group_completes(self):
        # P2-2-1: the leader has exited (unreaped zombie) before the stop starts. Darwin answers the initial CONT/TERM with
        # EPERM; the stop must end normally with the leader reaped. Real killpg first, then a stub forcing EPERM on every send.
        for stub in (False, True):
            with self.subTest(stubbed_eperm=stub):
                child = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
                self.addCleanup(lambda c=child: c.poll() is None and c.kill())
                end = _time.monotonic() + 10   # wait until it has exited but is still an unreaped zombie (no waitid on macOS)
                while subprocess.run(['ps', '-o', 'stat=', '-p', str(child.pid)], capture_output=True,
                                     text=True).stdout.strip()[:1] != 'Z':
                    self.assertLess(_time.monotonic(), end, 'fixture leader never became a zombie')
                    _time.sleep(0.02)
                sent = []
                def eperm(pgid, sig):
                    sent.append(sig)
                    raise PermissionError(1, 'Operation not permitted')
                with patch.object(night.os, 'killpg', eperm) if stub else patch.object(night, 'DRAIN_POLL_S', 0.05):
                    self.assertTrue(night.stop_group_drained(child, grace=2))
                self.assertIsNotNone(child.returncode)
                if stub:
                    self.assertEqual(sent, [signal.SIGCONT])   # the very first send already ends the stop

    def test_eperm_with_a_live_leader_is_a_genuine_error(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        def eperm(pgid, sig):
            raise PermissionError(1, 'Operation not permitted')
        with patch.object(night.os, 'killpg', eperm), self.assertRaises(PermissionError):
            night.stop_group_drained(child, grace=1)

    def test_deadline_compares_instants_in_the_repeated_fall_back_hour(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('America/Toronto')
        # 2026-11-01 01:00-02:00 happens twice: fold=0 is EDT (earlier), fold=1 is EST (an hour later)
        late_dl = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=1)    # 06:30 UTC
        early_dl = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0)   # 05:30 UTC
        self.assertFalse(night.deadline_reached(datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=0), late_dl))  # 05:45 UTC
        self.assertTrue(night.deadline_reached(datetime(2026, 11, 1, 1, 15, tzinfo=tz, fold=1), early_dl))  # 06:15 UTC
        self.assertTrue(night.deadline_reached(late_dl, late_dl))
        self.assertTrue(night.deadline_reached(datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=1), late_dl))
        self.assertFalse(night.deadline_reached(datetime(2026, 11, 1, 1, 15, tzinfo=tz, fold=0), early_dl))
        # wall-time >= would get the first two wrong (the bug this guards against)
        self.assertTrue(datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=0) >= late_dl)
        # and run_cycle uses it: a cycle at 01:45 EDT against a 01:30 EST deadline is not ended; 01:15 EST vs 01:30 EDT is
        for now, dl, want in ((datetime(2026, 11, 1, 1, 45, tzinfo=tz, fold=0), late_dl, 'idle'),
                              (datetime(2026, 11, 1, 1, 15, tzinfo=tz, fold=1), early_dl, 'window_end')):
            with self.subTest(now=now.isoformat(), deadline=dl.isoformat()):
                tmp = self._fixture_dir()
                g = TrainOnlyInWindow._guard(self, lambda n=now: n)
                with patch.object(night, 'train_policy', lambda w, dl=dl: (True, 'test window', dl)):
                    _, res = night.run_cycle([sys.executable, '-c', 'import time; time.sleep(0.3)'], g,
                                             result_file=tmp / 'r', poll_s=0, sleep=lambda s: _time.sleep(0.02), grace_end=2)
                self.assertEqual(res, want)

    def test_drain_kills_survivors_after_the_grace(self):
        child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                                  'time.sleep(60)'], start_new_session=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        _time.sleep(0.3)
        t0 = _time.monotonic()
        self.assertFalse(night.stop_group_drained(child, grace=0.5))
        self.assertLess(_time.monotonic() - t0, 5)
        self.assertIsNotNone(child.poll())
