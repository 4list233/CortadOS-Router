"""Exercise the nightly time boundary without loading any model."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
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
