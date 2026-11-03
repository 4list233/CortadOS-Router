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
                        signaller=lambda pid, s: sig.append(s), budget_s=1.0, min_free_gb=8, interval_s=30, log=lambda m: None)
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

    def test_idle_vs_immediate_next_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = Path(tmp) / 'r'
            g = night.Guard(latency=lambda: 1.0, memory=lambda: 100.0, budget_s=1, min_free_gb=8, interval_s=0, log=lambda m: None)
            sleeps = []
            idle = [sys.executable, '-c', 'import os;open(os.environ["CORTADOS_S1_CYCLE_RESULT"],"w").write("idle")']
            night.run_continuous(idle, g, result_file=rf, idle_s=900, sleep=sleeps.append, cycles=2, poll_s=0)
            self.assertEqual(sleeps.count(900), 2)
            sleeps.clear()
            night.run_continuous([sys.executable, '-c', 'pass'], g, result_file=rf, idle_s=900, sleep=sleeps.append, cycles=2, poll_s=0)
            self.assertNotIn(900, sleeps)

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
        self.assertIn('--min-left-pct "${CORTADOS_S1_MIN_LEFT_PCT:-15}"', (root / 'nightly.sh').read_text())
