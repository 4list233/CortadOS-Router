"""Usage-probe cleanup: a probe CLI never outlives its probe (cortados_router/probe_reaper.py).

Regression for 2026-10-13: sampler daemon threads killed at interpreter exit skipped tui_capture's cleanup, leaving 47
orphaned vendor CLIs holding PTYs until every usage probe failed with "out of pty devices". The fake CLI is node,
one of the command names the reaper matches; no vendor CLI or account is touched.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "cortados_router"
NODE = shutil.which("node")
# Like the real agy/claude orphans (2026-10-13): survives the PTY hangup (SIGHUP) and SIGTERM, so only an explicit
# reap kills it. A CLI that dies on SIGHUP would make the exit test pass even without the fix.
IDLE_CLI = [NODE, "-e", "process.on('SIGHUP', () => {}); process.on('SIGTERM', () => {}); setInterval(() => {}, 1000)"] if NODE else None


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, str(PLUGIN / file))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


reaper = _load("router_probe_reaper_test", "probe_reaper.py")


def _wait_gone(proc, timeout=8.0):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


@unittest.skipUnless(NODE, "probe_reaper tests need node as the fake vendor CLI; install node (it is a vendor-CLI dependency)")
class ProbeReaper(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-reaper-"))
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill(); proc.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cli_in(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(IDLE_CLI, cwd=folder, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        deadline = time.time() + 5
        while proc.pid not in reaper.probe_cwd_pids(self.tmp, folder) and time.time() < deadline:
            time.sleep(0.2)
        return proc

    def _age(self, folder, seconds):
        past = time.time() - seconds
        os.utime(folder, (past, past))

    def test_exit_mid_probe_leaves_no_cli_behind(self):
        script = textwrap.dedent(f"""
            import importlib.util, os, sys, threading, time
            from pathlib import Path
            spec = importlib.util.spec_from_file_location("scarcity_exit_test", {str(PLUGIN / 'scarcity.py')!r})
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            m.PROBE_DIR = Path({str(self.tmp)!r})
            threading.Thread(target=m.tui_capture, args=({IDLE_CLI!r}, "/usage", "never", dict(os.environ)),
                             kwargs={{"boot_s": 60}}, daemon=True).start()
            deadline = time.time() + 10
            while not any(m.PROBE_DIR.glob("p*")) and time.time() < deadline:
                time.sleep(0.1)
            time.sleep(1.5)
            print("probe-started" if any(m.PROBE_DIR.glob("p*")) else "probe-missing", flush=True)
            sys.exit(0)
        """)
        done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, timeout=60, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("probe-started", done.stdout, "test setup: the probe CLI never started, so exit cleanup was not exercised")
        deadline, left = time.time() + 8, None
        while time.time() < deadline:
            left = reaper.probe_cwd_pids(self.tmp)
            if not left:
                break
            time.sleep(0.5)
        self.assertEqual(left, [], "probe_reaper: a process exiting mid-probe must reap its probe CLI (atexit), "
                                   f"orphans still in {self.tmp}: {left}")

    def test_sweep_reclaims_cli_in_stale_folder(self):
        folder = self.tmp / "p11111-22222"
        proc = self._cli_in(folder)
        self._age(folder, reaper.STALE_PROBE_AGE_S + 60)
        counts = reaper.sweep_stale(self.tmp)
        self.assertTrue(_wait_gone(proc), "probe_reaper: sweep_stale must kill a CLI left in a folder older than STALE_PROBE_AGE_S")
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(counts["killed"], 1)
        deadline = time.time() + 5
        while folder.exists() and time.time() < deadline:
            reaper.sweep_stale(self.tmp)
            time.sleep(0.2)
        self.assertFalse(folder.exists(), "probe_reaper: sweep_stale must remove a stale probe folder once its CLI is gone")

    def test_sweep_spares_fresh_and_tracked_folders(self):
        fresh = self.tmp / "p33333-44444"
        fresh_proc = self._cli_in(fresh)
        tracked = self.tmp / "p55555-66666"
        tracked_proc = self._cli_in(tracked)
        self._age(tracked, reaper.STALE_PROBE_AGE_S + 60)
        reaper.track(tracked_proc.pid, -1, tracked, self.tmp)
        try:
            counts = reaper.sweep_stale(self.tmp)
        finally:
            reaper.untrack(tracked_proc.pid)
        self.assertEqual(counts, {"stale": 0, "killed": 0, "removed": 0},
                         "probe_reaper: sweep_stale must never touch a probe that may still be running")
        self.assertIsNone(fresh_proc.poll(), "probe_reaper: a folder younger than STALE_PROBE_AGE_S belongs to a live probe")
        self.assertIsNone(tracked_proc.poll(), "probe_reaper: a folder tracked by this process belongs to a live probe")

    def test_staleness_ignores_owner_pid_in_folder_name(self):
        folder = self.tmp / f"p{os.getpid()}-77777"     # owner pid alive (this test), but the folder is old: still stale
        folder.mkdir()
        self._age(folder, reaper.STALE_PROBE_AGE_S + 60)
        counts = reaper.sweep_stale(self.tmp)
        self.assertEqual(counts["removed"], 1, "probe_reaper: staleness is by folder age, never by the (reusable) owner pid")


if __name__ == "__main__":
    unittest.main()
