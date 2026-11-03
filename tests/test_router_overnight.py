"""Exercise the nightly time boundary without loading any model."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import os
import signal
import sys
import unittest
from unittest.mock import Mock, patch
import subprocess
import tempfile

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
        proc=Mock(pid=123);proc.wait.side_effect=[subprocess.TimeoutExpired('x',1),subprocess.TimeoutExpired('x',1),-9]
        with patch.object(night.subprocess,'Popen',return_value=proc) as start,patch.object(night,'signal_group') as kill:
            self.assertEqual(night.run_bounded(['owned'],1,grace=.1),124)
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertIn(((123,signal.SIGTERM),),[(c.args,) for c in kill.call_args_list])
        self.assertIn(((123,signal.SIGKILL),),[(c.args,) for c in kill.call_args_list])

    def test_resume_interrupted_before_first_save_does_not_double_count(self):
        source=(Path(__file__).resolve().parents[1]/'s1/nightly.sh').read_text()
        block=source[source.index('  resume=""'):source.index('  HF_HUB_OFFLINE=1 $PY -m mlx_lm')]
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


if __name__ == '__main__':unittest.main()


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
        with patch.object(night, 'signal_group') as kill:
            night.stop_tree(proc, grace=.1)
        self.assertEqual([c.args[1] for c in kill.call_args_list][:2], [signal.SIGCONT, signal.SIGTERM])

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
