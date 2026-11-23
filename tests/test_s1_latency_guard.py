"""#116: the S1 latency guard. The scorer's own timing window (serve.py record_latency / GET /latency), the guard's
reading of it (overnight.ScorerLatency), a probe past the router's S1 timeout counted as a violation, the p50/p90 rule,
every process group of the cycle's tree stopped and continued (the studio gate's trainer leads its own group), the state
file, and its way into the live view (live_heartbeat.s1_guard, live.validate_s1_guard, live.s1_guard_view).

Fakes only: a fake /latency fetch, fake probes, fake clocks, fake signallers and a fake ps; the HTTP tests run serve.py's
handler and a stand-in scorer on loopback in this process (no MLX, no worker thread). The one real-ps test walks a tree
of this test's own sleeping processes. No real S1, Studio job or ssh is touched."""
import http.server
import importlib.util
import io
import json
import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
sys.path.insert(0, str(REPO))
import serve  # noqa: E402
from cortados_router import live_heartbeat as hbs  # noqa: E402
from cortados_router.provider import live  # noqa: E402

spec = importlib.util.spec_from_file_location("overnight_guard_test", REPO / "s1/overnight.py")
night = importlib.util.module_from_spec(spec); spec.loader.exec_module(night)

INF = math.inf


def sample(age, s, outcome="ok", probe=False):
    return {"age_s": age, "s": s, "outcome": outcome, "probe": probe}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def guard(latency, *, clock=None, logs=None, sig=None, groups=None, state_path=None, baseline=None, **kw):
    """A Guard on fakes: day budget 1 s, no night window, interval 0, plenty of memory."""
    logs = [] if logs is None else logs
    sig = [] if sig is None else sig
    g = night.Guard(latency=latency, memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0, max_pause_s=1800,
                    clock=clock or Clock(), signaller=lambda pid, s: sig.append((pid, s)), log=logs.append,
                    alerter=lambda n, d: None, night_window=None, groups=groups, state_path=state_path,
                    epoch=lambda: 1_800_000_000.0, **kw)
    if baseline is not None:
        g.baseline_samples, g.baseline_p90_samples = [baseline[0]], [baseline[1]]
    return g


class ScorerTimingWindow(unittest.TestCase):
    """serve.py times every /score request itself (#116)."""

    def setUp(self):
        self._saved = (serve._latency.copy(), serve.TOKEN, serve._jobs)
        serve._latency.clear()

    def tearDown(self):
        serve._latency.clear(); serve._latency.extend(self._saved[0])
        serve.TOKEN, serve._jobs = self._saved[1], self._saved[2]

    def test_samples_are_reported_by_age_and_outcome_within_the_window(self):
        t = [100.0]
        serve.record_latency(4.8, "ok", clock=lambda: t[0])
        t[0] = 101.0
        serve.record_latency(15.0, "timeout", probe=True, clock=lambda: t[0])
        serve.record_latency(0.0, "bogus", clock=lambda: t[0])        # an unknown outcome is kept as an error
        rep = serve.latency_report(clock=lambda: 103.0)
        self.assertEqual(rep["samples"], [sample(3.0, 4.8), sample(2.0, 15.0, "timeout", True), sample(2.0, 0.0, "error")])
        rep = serve.latency_report(clock=lambda: 100.0 + serve.LATENCY_WINDOW_S + 0.5)   # the first one aged out
        self.assertEqual([s["s"] for s in rep["samples"]], [15.0, 0.0])

    def test_the_window_is_bounded(self):
        for _ in range(serve.LATENCY_MAX_SAMPLES + 10):
            serve.record_latency(1.0, "ok")
        self.assertEqual(len(serve.latency_report()["samples"]), serve.LATENCY_MAX_SAMPLES)

    def _server(self):
        srv = serve.BoundedServer(("127.0.0.1", 0), serve.H, max_conns=8)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}"

    def _post(self, url, headers):
        req = urllib.request.Request(url + "/score", data=b'{"request": "x"}', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_a_request_the_handler_gives_up_on_is_a_timeout_and_a_full_backlog_is_busy(self):
        url = self._server()
        serve._jobs = queue.Queue(maxsize=1)           # no worker: a queued job is never answered
        self.assertEqual(self._post(url, {"X-Request-Timeout-S": "0.2", "X-CortadOS-Probe": "1"}), 500)
        self.assertEqual(self._post(url, {}), 503)     # the unanswered job still fills the one slot
        rows = serve.latency_report()["samples"]
        self.assertEqual([(r["outcome"], r["probe"]) for r in rows], [("timeout", True), ("busy", False)])
        self.assertGreaterEqual(rows[0]["s"], 0.9)     # the handler waits at least 1 s (client_wait floor)

    def test_a_job_the_worker_dropped_past_its_deadline_is_a_timeout(self):
        url = self._server()
        serve._jobs = queue.Queue(maxsize=4)

        def worker():                                  # answers as serve.worker does for an expired job
            body, reply, deadline = serve._jobs.get()
            reply.put({"error": "dropped: client deadline passed before scoring"})
        threading.Thread(target=worker, daemon=True).start()
        self.assertEqual(self._post(url, {"X-Request-Timeout-S": "5"}), 500)
        self.assertEqual([r["outcome"] for r in serve.latency_report()["samples"]], ["timeout"])

    def test_latency_endpoint_needs_the_token_once_one_is_set(self):
        url = self._server()
        serve.record_latency(2.0, "ok")
        with urllib.request.urlopen(url + "/latency", timeout=10) as r:
            self.assertEqual(json.loads(r.read())["samples"][0]["s"], 2.0)
        serve.TOKEN = "s3cret"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(url + "/latency", timeout=10)
        self.assertEqual(cm.exception.code, 401)
        req = urllib.request.Request(url + "/latency", headers={"Authorization": "Bearer s3cret"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(len(json.loads(r.read())["samples"]), 1)
        with urllib.request.urlopen(url + "/health", timeout=10) as r:   # /health is unchanged and needs no token
            self.assertIn("ok", json.loads(r.read()))


class ProbeTimeout(unittest.TestCase):
    """2026-11-14: a probe past its timeout read as "no reading", so a scorer timing routed calls out never paused."""

    def test_a_probe_past_the_router_timeout_raises_timed_out_and_says_so(self):
        seen = {}

        class Slow(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["probe"] = self.headers.get("X-CortadOS-Probe")
                seen["timeout"] = self.headers.get("X-Request-Timeout-S")
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                time.sleep(1.0)
                try:
                    self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        with self.assertRaises(night.ScorerTimedOut) as cm:
            night.probe_scorer_latency(f"http://127.0.0.1:{srv.server_port}/score", timeout=0.2)
        self.assertIn("timed out after 0.2s", str(cm.exception))
        self.assertEqual((seen.get("timeout"), seen.get("probe")), ("0.2", "1"))

    def test_the_default_probe_timeout_is_the_routers_s1_timeout(self):
        with patch.dict(os.environ, {"CORTADOS_S1_TIMEOUT_S": "15"}):
            self.assertEqual(night.router_s1_timeout(), 15.0)
        with patch.dict(os.environ, {"CORTADOS_S1_TIMEOUT_S": "nan"}), patch("builtins.print"):
            self.assertEqual(night.router_s1_timeout(), 15.0)

    def test_a_timed_out_probe_pauses_the_tree(self):
        def timed_out():
            raise night.ScorerTimedOut("scorer probe timed out after 15s (the router's S1 timeout)")
        logs, sig = [], []
        g = guard(timed_out, logs=logs, sig=sig, baseline=(4.8, 6.2))
        g.tick(42)
        self.assertEqual(sig, [(42, signal.SIGSTOP)])
        self.assertTrue(g.paused)
        self.assertEqual(g.reason, "timeout")
        self.assertIn("timed out", logs[-1])

    def test_a_timeout_while_paused_is_not_unreadable_and_never_a_baseline(self):
        def timed_out():
            raise night.ScorerTimedOut("t")
        g = guard(timed_out, baseline=(4.8, 6.2))
        g.tick(42)
        for _ in range(night.Guard.MAX_UNREADABLE + 2):
            g.tick(42)
        self.assertTrue(g.paused)                       # held: no forced release on "unreadable" probes
        self.assertEqual(g.baseline_samples, [4.8])     # the timed-out readings did not move the baseline


class ScorerLatencyReading(unittest.TestCase):
    def reader(self, samples, probe=lambda: 9.0, now=1000.0, **kw):
        body = None if samples is None else {"window_s": 900, "samples": samples}
        calls = []

        def fetch(url):
            calls.append(url)
            return body
        probes = []

        def p():
            probes.append(1)
            return probe() if callable(probe) else probe
        r = night.ScorerLatency(url="http://scorer.test:8431/score", fetch=fetch, probe=p, clock=lambda: now,
                                window_s=180, **kw)
        return r, calls, probes

    def test_routed_samples_give_p50_and_p90_without_a_probe(self):
        r, calls, probes = self.reader([sample(10, 4.0), sample(20, 5.0), sample(30, 6.0), sample(40, 7.0)])
        got = r.read(None)
        self.assertEqual(calls, ["http://scorer.test:8431/latency"])
        self.assertEqual(probes, [])
        self.assertEqual(got, night.Reading(5.0, 7.0, 4, 0, "scorer"))

    def test_samples_that_began_before_since_or_left_the_window_are_skipped(self):
        # now 1000: a sample ending 10 s ago that took 5 s began at 985
        r, _, probes = self.reader([sample(10, 5.0), sample(10, 30.0), sample(200, 1.0), sample(5, 2.0), sample(1, 3.0)])
        got = r.read(since=980.0)        # kept: 5.0 (began 985), 2.0 (993), 3.0 (996); 30.0 began at 960, 1.0 aged out
        self.assertEqual((got.n, got.p50, got.p90), (3, 3.0, 5.0))
        self.assertEqual(probes, [])

    def test_timeouts_and_busy_are_infinite(self):
        r, _, _ = self.reader([sample(5, 4.0), sample(6, 15.0, "timeout"), sample(7, 0.0, "busy")])
        got = r.read(None)
        self.assertEqual((got.timeouts, got.p90), (2, INF))

    def test_a_quiet_scorer_is_topped_up_by_one_probe(self):
        r, _, probes = self.reader([sample(5, 4.0)])
        got = r.read(None)
        self.assertEqual(probes, [1])
        self.assertEqual((got.n, got.p90, got.source), (2, 9.0, "scorer"))

    def test_a_probe_that_times_out_or_is_refused_is_an_infinite_sample(self):
        for exc in (night.ScorerTimedOut("t"), night.ScorerRefused("r")):
            def boom(exc=exc):
                raise exc
            r, _, _ = self.reader([], probe=boom)
            self.assertEqual(r.read(None), night.Reading(INF, INF, 1, 1, "scorer"))

    def test_an_older_scorer_without_latency_falls_back_to_the_probe(self):
        r, _, probes = self.reader(None, probe=lambda: 4.5)
        self.assertEqual(r.read(None), night.Reading(4.5, 4.5, 1, 0, "probe"))
        r, _, _ = self.reader(None, probe=lambda: None)          # and nothing at all: no reading
        self.assertIsNone(r.read(None))

    def test_a_config_error_still_reaches_the_guard(self):
        def bad():
            raise night.ScorerConfigError("HTTP 401")
        r, _, _ = self.reader([], probe=bad)
        with self.assertRaises(night.ScorerConfigError):
            r.read(None)

    def test_malformed_rows_are_skipped(self):
        r, _, _ = self.reader([{"age_s": "x"}, {"s": 1}, sample(-1, 1.0), sample(1, float("nan")), sample(2, 3.0),
                               sample(3, 3.5), sample(4, 4.0)])
        self.assertEqual(r.read(None).n, 3)

    def test_the_guard_passes_its_state_change_time_as_since(self):
        class Src:
            def __init__(self):
                self.since = []

            def read(self, since):
                self.since.append(since)
                return night.Reading(9.0, 9.0, 3, 0, "scorer")
        clock = Clock(500.0)
        src = Src()
        g = guard(src, clock=clock, baseline=(4.8, 6.2))
        clock.t = 530.0
        g.tick(42)                                      # over budget: paused at 530
        clock.t = 560.0
        g.tick(42)
        self.assertEqual(src.since, [500.0, 530.0])


class PercentileRule(unittest.TestCase):
    """Over budget when p50 > baseline p50 + budget OR p90 > baseline p90 + budget; resume after 3 good readings."""

    def run_readings(self, readings, baseline=(4.8, 6.2)):
        it = iter(readings)

        class Src:
            def read(self, since):
                return next(it)
        logs, sig = [], []
        g = guard(Src(), logs=logs, sig=sig, baseline=baseline)
        states = []
        for _ in readings:
            g.tick(42)
            states.append(g.paused)
        return g, states, sig, logs

    def R(self, p50, p90, n=5):
        return night.Reading(p50, p90, n, 0, "scorer")

    def test_a_p90_over_its_budget_pauses_even_with_a_good_p50(self):
        g, states, sig, logs = self.run_readings([self.R(5.0, 7.0), self.R(5.0, 7.4)])
        self.assertEqual(states, [False, True])
        self.assertEqual(g.reason, "latency")
        self.assertIn("p90 7.40s", logs[-1])

    def test_a_p50_over_its_budget_pauses(self):
        _, states, _, _ = self.run_readings([self.R(5.9, 6.0)])
        self.assertEqual(states, [True])

    def test_resume_needs_three_good_readings_in_a_row(self):
        bad, good = self.R(9.0, 12.0), self.R(4.9, 6.3)
        g, states, sig, logs = self.run_readings([bad, good, good, bad, good, good, good])
        self.assertEqual(states, [True, True, True, True, True, True, False])
        self.assertEqual([s for _, s in sig], [signal.SIGSTOP, signal.SIGCONT])
        self.assertIn("RESUME", logs[-1])

    def test_a_reading_without_a_baseline_pauses_once_to_measure(self):
        g, states, _, logs = self.run_readings([self.R(9.0, 12.0)], baseline=None)
        g.baseline_samples, g.baseline_p90_samples = [], []
        self.assertEqual(states, [True])
        self.assertEqual(g.reason, "no_baseline")

    def test_paused_readings_become_the_baseline(self):
        g, _, _, _ = self.run_readings([self.R(9.0, 12.0), self.R(4.0, 5.0)], baseline=(4.8, 6.2))
        self.assertEqual((g.baseline_samples, g.baseline_p90_samples), ([4.8, 4.0], [6.2, 5.0]))


class ProcessGroups(unittest.TestCase):
    PS = """  100     1   100
  101   100   100
  200   101   200
  201   200   200
  202   200   202
  300     1   300
  bad line
"""

    def test_every_group_in_the_cycle_tree_is_found(self):
        self.assertEqual(night.work_groups(100, ps=lambda: self.PS), [100, 200, 202])

    def test_this_supervisors_own_group_is_never_included(self):
        with patch.object(night.os, "getpgrp", return_value=202):
            self.assertEqual(night.work_groups(100, ps=lambda: self.PS), [100, 200])

    def test_a_ps_failure_answers_the_cycle_group_alone(self):
        def boom():
            raise subprocess.CalledProcessError(1, "ps")
        logs = []
        self.assertEqual(night.work_groups(100, ps=boom, log=logs.append), [100])
        self.assertIn("ps failed", logs[0])

    def test_a_pause_stops_the_gated_trainers_own_group_and_resume_continues_it(self):
        it = iter([night.Reading(9.0, 12.0, 3, 0, "scorer")] + [night.Reading(4.8, 6.2, 3, 0, "scorer")] * 3)

        class Src:
            def read(self, since):
                return next(it)
        sig, logs = [], []
        g = guard(Src(), sig=sig, logs=logs, groups=lambda pg: [pg, 200, 202], baseline=(4.8, 6.2))
        g.tick(100)
        self.assertEqual(sig, [(100, signal.SIGSTOP), (200, signal.SIGSTOP), (202, signal.SIGSTOP)])
        self.assertIn("+2 gated step group(s) stopped", logs[-1])
        sig.clear()
        for _ in range(3):
            g.tick(100)
        self.assertEqual(sig, [(100, signal.SIGCONT), (200, signal.SIGCONT), (202, signal.SIGCONT)])

    def test_continue_all_reaches_a_group_that_left_the_tree(self):
        # The gate wrapper was killed: its trainer (pgid 200) is no longer under the cycle, but the pause stopped it.
        trees = iter([[100, 200], [100]])
        sig = []
        g = guard(lambda: 9.0, sig=sig, groups=lambda pg: next(trees), baseline=(4.8, 6.2))
        g.tick(100)
        sig.clear()
        g.continue_all(100)
        self.assertEqual(sig, [(200, signal.SIGCONT)])

    def test_a_gone_group_is_skipped(self):
        def signaller(pid, s):
            if pid == 200:
                raise ProcessLookupError
        g = night.Guard(latency=lambda: 9.0, memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0,
                        max_pause_s=1800, signaller=signaller, groups=lambda pg: [pg, 200], log=lambda m: None,
                        alerter=lambda n, d: None, night_window=None)
        g.baseline_samples = [4.8]
        g.tick(100)                                     # no raise
        self.assertTrue(g.paused)

    def test_a_cycle_leader_gone_at_the_pause_is_not_fatal(self):
        def signaller(pid, s):
            raise PermissionError(1, "zombie-only group")
        g = night.Guard(latency=lambda: 9.0, memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0,
                        max_pause_s=1800, signaller=signaller, log=lambda m: None, alerter=lambda n, d: None,
                        night_window=None)
        g.baseline_samples = [4.8]
        g.tick(100)
        self.assertTrue(g.paused)

    def test_run_cycle_continues_every_stopped_group_when_the_cycle_ends(self):
        sig = []
        g = guard(lambda: 9.0, sig=sig, groups=lambda pg: [pg, 999], baseline=(4.8, 6.2))
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            night.run_cycle([sys.executable, "-c", "import time; time.sleep(0.5)"], g, result_file=Path(tmp) / "r",
                            poll_s=0.05, sleep=time.sleep)
        self.assertIn((999, signal.SIGSTOP), sig)
        self.assertIn((999, signal.SIGCONT), sig)
        self.assertLess(sig.index((999, signal.SIGSTOP)), max(i for i, x in enumerate(sig) if x == (999, signal.SIGCONT)))
        self.assertFalse(g.paused)

    def test_real_ps_finds_a_grandchild_in_its_own_session(self):
        # This test's own processes only: a child that starts a sleeper with start_new_session (as the studio gate does).
        code = ("import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
                "print(p.pid, flush=True)\n"
                "time.sleep(30)\n")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            grandchild = int(child.stdout.readline())
            try:
                groups = night.work_groups(child.pid)
                self.assertEqual(groups[0], child.pid)
                self.assertIn(grandchild, groups)
            finally:
                os.kill(grandchild, signal.SIGKILL)
        finally:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            child.stdout.close()


class GuardStateFile(unittest.TestCase):
    def test_the_state_file_is_what_the_heartbeat_sends_and_the_provider_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            g = guard(lambda: 9.0, state_path=path, baseline=(4.8, 6.2))
            g.in_cycle = True
            g.tick(42)
            st = json.loads(path.read_text())
            self.assertEqual((st["state"], st["reason"], st["budget"], st["p50_s"], st["baseline_p90_s"]),
                             ("paused", "latency", "day", 9.0, 6.2))
            self.assertEqual(st["pauses_24h"], 1)
            now = 1_800_000_000.0
            sent = hbs.s1_guard(Path(tmp), now=now)
            self.assertEqual(sent["state"], "paused")
            self.assertNotIn("version", sent)
            hb, bad = live.validate_heartbeat({"host": "studio", "jobs": [], "s1": {"health": "up", "guard": sent}}, now)
            self.assertIsNone(bad)
            self.assertEqual(hb["s1"]["guard"]["p50_s"], 9.0)

    def test_a_timed_out_reading_is_written_as_null_seconds(self):
        def timed_out():
            raise night.ScorerTimedOut("t")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            g = guard(timed_out, state_path=path, baseline=(4.8, 6.2))
            g.tick(42)
            st = json.loads(path.read_text())
            self.assertEqual((st["reason"], st["p50_s"], st["p90_s"], st["timeouts"]), ("timeout", None, None, 1))
            self.assertIsNone(live.validate_s1_guard({k: v for k, v in st.items() if k != "version"}, 1_800_000_000.0)[1])

    def test_an_unwritable_state_file_is_logged_once_and_never_fatal(self):
        logs = []
        g = guard(lambda: 4.8, logs=logs, state_path=Path("/nonexistent-dir/latency-guard.json"), baseline=(4.8, 6.2))
        g.tick(42); g.tick(42)
        self.assertEqual(sum("cannot write guard state" in m for m in logs), 1)
        self.assertNotIn("/nonexistent-dir", "".join(logs))

    def test_the_state_is_written_during_the_release_hold_off_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            clock = Clock(0.0)
            # Low memory, not scoring: at the pause limit that is a forced release (scoring over budget stops for the day).
            g = guard(lambda: 4.8, clock=clock, state_path=path, baseline=(4.8, 6.2), release_hold_s=600)
            g.memory = lambda: 1.0
            g.max_pause = 60
            g.tick(42)                                  # paused at 0
            clock.t = 61.0
            g.tick(42)                                  # forced release: hold-off until 661
            self.assertEqual(json.loads(path.read_text())["reason"], "forced_release")
            path.unlink()
            clock.t = 120.0
            g.tick(42)                                  # inside the hold-off: no judgement, but the state is fresh
            st = json.loads(path.read_text())
            self.assertEqual((st["state"], st["reason"]), ("idle", "forced_release"))
            self.assertFalse(g.paused)

    def test_idle_between_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            g = guard(lambda: 4.0, state_path=path)
            g.idle_probe()
            st = json.loads(path.read_text())
            self.assertEqual((st["state"], st["baseline_p50_s"], st["source"]), ("idle", 4.0, "probe"))


class LiveView(unittest.TestCase):
    NOW = 1_800_000_000.0
    G = {"state": "paused", "reason": "latency", "updated_at": NOW - 10, "paused_s": 60.0, "budget": "day",
         "budget_s": 1.0, "baseline_p50_s": 4.8, "baseline_p90_s": 6.2, "p50_s": 9.0, "p90_s": None, "samples": 4,
         "timeouts": 1, "source": "scorer", "pauses_24h": 2, "pause": "complete", "until": None, "until_rule": None,
         "unresolved_groups": 0}

    def test_a_valid_state_passes(self):
        g, bad = live.validate_s1_guard(self.G, self.NOW)
        self.assertIsNone(bad)
        self.assertEqual(g, self.G)
        g, bad = live.validate_s1_guard({"state": "unknown"}, self.NOW)    # the sender's "unreadable" form
        self.assertIsNone(bad)
        self.assertEqual((g["state"], g["samples"], g["p50_s"]), ("unknown", 0, None))

    def test_anything_else_is_refused_by_field_name(self):
        cases = {".state": {"state": "stopped"}, ".reason": {"reason": "because"}, ".budget": {"budget": "noon"},
                 ".source": {"source": "/tmp/x"}, ".p50_s": {"p50_s": -1}, ".budget_s": {"budget_s": 1e9},
                 ".samples": {"samples": True}, ".timeouts": {"timeouts": 1.5}, ".updated_at": {"updated_at": 1.0},
                 ".paused_s": {"paused_s": float("inf")}, "": {"url": "http://studio:8431"}}
        for bad_field, over in cases.items():
            self.assertEqual(live.validate_s1_guard({**self.G, **over}, self.NOW), (None, bad_field), over)
        _, bad = live.validate_heartbeat({"host": "studio", "jobs": [], "s1": {"guard": {"state": "x"}}}, self.NOW)
        self.assertEqual(bad, "s1.guard.state")

    def test_the_s1_section_shows_the_freshest_guard_and_marks_a_silent_one_stale(self):
        beats = {"studio": {"received_at": self.NOW - 30, "ttl_s": 180, "s1": {"guard": self.G}},
                 "old": {"received_at": self.NOW - 999, "ttl_s": 180, "s1": {"guard": {**self.G, "updated_at": self.NOW}}},
                 "air": {"received_at": self.NOW - 30, "ttl_s": 180, "s1": {"health": "up"}}}
        v = live.s1_guard_view(beats, self.NOW)
        self.assertEqual((v["host"], v["age_s"], v["stale"], v["state"]), ("studio", 10.0, False, "paused"))
        beats["studio"]["s1"]["guard"] = {**self.G, "updated_at": self.NOW - 400}
        self.assertTrue(live.s1_guard_view(beats, self.NOW)["stale"])
        beats["studio"]["s1"]["guard"] = {**self.G, "state": "idle", "updated_at": self.NOW - 4000}
        self.assertFalse(live.s1_guard_view(beats, self.NOW)["stale"])   # idle writes once per cycle: age means nothing
        self.assertIsNone(live.s1_guard_view({"air": beats["air"]}, self.NOW))

    def test_the_heartbeat_reads_the_file_and_reports_a_bad_one_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.assertIsNone(hbs.s1_guard(data, now=self.NOW))                  # no supervisor here: no field
            (data / hbs.GUARD_FILE).write_text(json.dumps({"version": 1, **self.G}))
            self.assertEqual(hbs.s1_guard(data, now=self.NOW), self.G)
            (data / hbs.GUARD_FILE).write_text(json.dumps({**self.G, "url": "http://studio.ts.net:8431"}))
            self.assertEqual(hbs.s1_guard(data, now=self.NOW), {"state": "unknown"})
            (data / hbs.GUARD_FILE).write_text("{not json")
            self.assertEqual(hbs.s1_guard(data, now=self.NOW), {"state": "unknown"})
            (data / hbs.GUARD_FILE).write_text("[" * 4000)                       # nested past the JSON recursion limit
            self.assertEqual(hbs.s1_guard(data, now=self.NOW), {"state": "unknown"})
            (data / hbs.GUARD_FILE).write_text(" " * (hbs.GUARD_MAX_BYTES + 1))
            self.assertEqual(hbs.s1_guard(data, now=self.NOW), {"state": "unknown"})

    def test_an_older_provider_gets_the_s1_block_without_the_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            data = d / "data"; data.mkdir()
            (data / hbs.GUARD_FILE).write_text(json.dumps({**self.G, "updated_at": time.time()}))
            token = d / "token"; token.write_text("t" * 32 + "\n"); os.chmod(token, 0o600)
            cfg = d / "hb.json"
            cfg.write_text(json.dumps({"version": 1, "host": "studio", "provider_url": "http://provider.test:8432",
                                       "token_file": str(token), "jobs": [], "hardware": False, "disk": False,
                                       "tasks": False, "s1": {"data_dir": str(data), "health_url": "http://x.test:1"}}))
            seen = []

            class Resp(io.BytesIO):
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def opener(req, timeout):
                body = json.loads(req.data)
                seen.append(body)
                if "guard" in (body.get("s1") or {}):
                    raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(
                        b'{"error": {"message": "cortados: heartbeat field s1 is invalid"}}'))
                return Resp(b"{}")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = hbs.main(["--config", str(cfg)], run=lambda *a, **k: None, sample=lambda: {},
                                fetch=lambda url: b'{"ok": true}', opener=opener, disk_usage=lambda p: None,
                                task_events=lambda: [], peek=None)
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("refused the s1 guard field", err.getvalue())
            self.assertEqual(["guard" in b["s1"] for b in seen], [True, False])
            self.assertEqual(seen[-1]["s1"]["health"], "up")


START = "Sun Nov 22 14:57:48 2026"


def discovery(groups, complete=True, starts=None):
    return night.Discovery(list(groups), complete, dict(starts or {}))


class IncompleteDiscovery(unittest.TestCase):
    """A2 (#116 review round 1): a failed ps is not a complete pause; it is retried and takes no baseline meanwhile."""

    def test_a_ps_failure_is_reported_as_incomplete(self):
        def boom():
            raise subprocess.TimeoutExpired("ps", 10)
        d = night.discover_groups(100, ps=boom, log=lambda m: None)
        self.assertEqual((d.groups, d.complete), ([100], False))

    def test_discovery_keeps_each_group_leaders_start_time(self):
        ps = ("  100     1   100 Sun Nov 22 14:57:48 2026\n"
              "  101   100   100 Sun Nov 22 14:57:49 2026\n"
              "  200   101   200 Sun Nov  2 09:01:02 2026\n")
        d = night.discover_groups(100, ps=lambda: ps)
        self.assertEqual((d.groups, d.complete), ([100, 200], True))
        self.assertEqual(d.starts, {100: START, 200: "Sun Nov 2 09:01:02 2026"})

    def test_an_incomplete_pause_is_reported_retried_and_never_a_baseline(self):
        trees = iter([discovery([100], False), discovery([100], False), discovery([100, 200])]
                     + [discovery([100, 200])] * 10)
        it = iter([9.0, 5.0, 5.0, 5.0, 5.0])
        sig = []
        g = guard(lambda: next(it), sig=sig, groups=lambda pg: next(trees), baseline=(4.8, 6.2))
        g.tick(100)
        self.assertTrue(g.paused)
        self.assertFalse(g.pause_complete)
        self.assertEqual(g.state()["pause"], "incomplete")
        g.tick(100)                                      # retry: ps still failing
        self.assertFalse(g.pause_complete)
        self.assertEqual(g.baseline_samples, [4.8])      # the 5.0 reading was taken with the trainer maybe running
        self.assertNotIn((200, signal.SIGSTOP), sig)
        g.tick(100)                                      # retry: ps works, the trainer group is found and stopped
        self.assertTrue(g.pause_complete)
        self.assertIn((200, signal.SIGSTOP), sig)
        self.assertEqual(g.state()["pause"], "complete")
        self.assertEqual(g.baseline_samples, [4.8, 5.0])  # from now on the readings are the scorer alone

    def test_no_resume_without_a_baseline_while_the_pause_is_incomplete(self):
        sig = []
        g = guard(lambda: 2.0, sig=sig, groups=lambda pg: discovery([pg], False))
        for _ in range(6):
            g.tick(100)
        self.assertTrue(g.paused)
        self.assertEqual(g.baseline_samples, [])
        self.assertNotIn((100, signal.SIGCONT), sig)

    def test_a_sigstop_that_did_not_land_keeps_the_pause_incomplete(self):
        sent = []
        def signaller(pid, s):
            if pid == 200 and s == signal.SIGSTOP:
                raise PermissionError(1, "not permitted")
            sent.append((pid, s))
        g = night.Guard(latency=lambda: 9.0, memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0,
                        max_pause_s=1800, signaller=signaller, groups=lambda pg: [pg, 200], log=lambda m: None,
                        alerter=lambda n, d: None, night_window=None)
        g.baseline_samples = [4.8]
        g.tick(100)
        self.assertTrue(g.paused)
        self.assertFalse(g.pause_complete)


class CrashRecovery(unittest.TestCase):
    """A1 (#116 review round 1): the stopped groups are recorded before each SIGSTOP and a restarted supervisor continues
    every recorded group that is still the one recorded, then every group in the tree, before the cycle's root."""

    def test_every_group_is_recorded_before_it_is_signalled_and_cleared_once_continued(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped = Path(tmp) / night.STOPPED_GROUPS_NAME
            seen = []
            def signaller(pid, s):
                recs, ok = night.read_stopped_groups(stopped)
                seen.append((pid, s, [r["pgid"] for r in recs]))
            it = iter([night.Reading(9.0, 12.0, 3, 0, "scorer")] + [night.Reading(4.8, 6.2, 3, 0, "scorer")] * 3)

            class Src:
                def read(self, since):
                    return next(it)
            g = night.Guard(latency=Src(), memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0,
                            max_pause_s=1800, signaller=signaller, log=lambda m: None, alerter=lambda n, d: None,
                            night_window=None, stopped_path=stopped, leader_start=lambda pid: START,
                            groups=lambda pg: discovery([pg, 200], starts={pg: START, 200: "Mon Nov 23 01:00:00 2026"}))
            g.baseline_samples, g.baseline_p90_samples = [4.8], [6.2]
            g.tick(100)
            stops = [x for x in seen if x[1] == signal.SIGSTOP]
            self.assertEqual([x[0] for x in stops], [100, 200])
            for pid, _, recorded in stops:
                self.assertIn(pid, recorded)              # on disk before the signal
            recs, _ = night.read_stopped_groups(stopped)
            self.assertEqual(recs, [{"pgid": 100, "start": START}, {"pgid": 200, "start": "Mon Nov 23 01:00:00 2026"}])
            for _ in range(3):
                g.tick(100)
            self.assertFalse(g.paused)
            self.assertFalse(stopped.exists())

    def test_a_restarted_supervisor_continues_every_stopped_group_before_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped, trainer, pidfile = d / night.STOPPED_GROUPS_NAME, d / night.TRAINER_GROUP_NAME, d / "nightly.pid"
            # The first supervisor pauses (cycle 100, gate 150, trainer 200) and is killed: no SIGCONT ever sent.
            g = guard(lambda: 9.0, groups=lambda pg: discovery([pg, 150, 200], starts={pg: "a", 150: "b", 200: "c"}),
                      baseline=(4.8, 6.2), stopped_path=stopped, leader_start=lambda pid: {100: "a"}.get(pid))
            g.tick(100)
            del g
            # The gate wrapper (150) crashed too: the trainer (200) left the tree, which now shows another gate (300).
            trainer.write_text(json.dumps({"version": 1, "pgid": 200, "start": "c", "gate_pid": 150}))
            pidfile.write_text("100\n")
            sent = []
            starts = {100: "a", 150: None, 200: "c", 300: "d"}
            cont = night.recover_stopped_groups(stopped, trainer, pidfile, signaller=lambda p, s: sent.append((p, s)),
                                                leader_start=lambda pid: starts.get(pid), alive=lambda pid: True,
                                                discover=lambda root: discovery([root, 300]), log=lambda m: None)
            night.recover_pidfile(pidfile, alive=lambda p: True, ps=lambda p: "T",
                                  signaller=lambda p, s: sent.append((p, s)), log=lambda m: None, stopped_path=stopped)
            self.assertEqual(sorted(cont), [150, 200, 300])
            self.assertEqual(sent[-1], (100, signal.SIGCONT))   # the root last
            self.assertEqual(sorted(sent[:-1]), [(150, signal.SIGCONT), (200, signal.SIGCONT), (300, signal.SIGCONT)])
            self.assertFalse(stopped.exists())

    def test_recovery_signals_only_groups_that_are_still_the_recorded_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped = d / night.STOPPED_GROUPS_NAME
            night._write_json_durable(stopped, {"version": 1, "groups": [
                {"pgid": 201, "start": "old"},       # pgid reused by another process: never signalled
                {"pgid": 202, "start": "same"},      # still ours
                {"pgid": 203, "start": "x"},         # leader gone: the group, if any, is still ours
                {"pgid": 204, "start": None},        # alive, no start recorded: undecidable
                {"pgid": 205, "start": "y"},         # ps failed: undecidable
                {"pgid": os.getpgrp(), "start": "z"},   # this supervisor's own group: never
                {"pgid": 1, "start": "launchd"}]})
            def leader_start(pid):
                if pid == 205:
                    raise subprocess.TimeoutExpired("ps", 10)
                return {201: "new", 202: "same", 204: "whatever", os.getpgrp(): "z", 1: "launchd"}.get(pid)
            sent, logs = [], []
            night.recover_stopped_groups(stopped, None, d / "no.pid", signaller=lambda p, s: sent.append(p),
                                         leader_start=leader_start, log=logs.append)
            self.assertEqual(sent, [202, 203])
            self.assertTrue(any("201 now belongs to another process" in m for m in logs))

    def test_group_identity_rule(self):
        self.assertTrue(night.group_is_ours({"pgid": 7, "start": "a"}, leader_start=lambda p: "a"))
        self.assertTrue(night.group_is_ours({"pgid": 7, "start": "a"}, leader_start=lambda p: None))
        self.assertFalse(night.group_is_ours({"pgid": 7, "start": "a"}, leader_start=lambda p: "b"))
        self.assertIsNone(night.group_is_ours({"pgid": 7, "start": None}, leader_start=lambda p: "b"))

    def test_real_ps_start_time_matches_the_gates_own(self):
        # This test's own child only: the guard's identity check and the gate's record agree on the same process.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            start = night.proc_start(child.pid)
            self.assertTrue(start)
            self.assertEqual(start, cap._proc_start(child.pid))
            self.assertTrue(night.group_is_ours({"pgid": child.pid, "start": start}))
        finally:
            child.kill()
            child.wait()
        self.assertIsNone(night.proc_start(child.pid))


from cortados_router import studio_capacity as cap  # noqa: E402


class TrainerGroupRecord(unittest.TestCase):
    """C (#116, owner 2026-11-18): the studio gate records its trainer's group; the guard stops and continues it after
    checking it is still the same group, even when it has left the cycle's tree."""

    def mk(self, trainer_file, starts, sig, **kw):
        return guard(lambda: 9.0, sig=sig, groups=lambda pg: [pg], baseline=(4.8, 6.2), trainer_file=trainer_file,
                     leader_start=lambda pid: starts.get(pid), **kw)

    def test_the_recorded_trainer_group_is_paused_and_continued_outside_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / night.TRAINER_GROUP_NAME
            tf.write_text(json.dumps({"version": 1, "pgid": 500, "start": START, "gate_pid": 499}))
            sig = []
            g = self.mk(tf, {500: START}, sig)
            g.tick(100)
            self.assertEqual(sig, [(100, signal.SIGSTOP), (500, signal.SIGSTOP)])
            self.assertTrue(g.pause_complete)
            sig.clear()
            g.continue_all(100)
            self.assertEqual(sig, [(500, signal.SIGCONT)])

    def test_a_record_naming_a_reused_pgid_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / night.TRAINER_GROUP_NAME
            tf.write_text(json.dumps({"version": 1, "pgid": 500, "start": START}))
            sig = []
            g = self.mk(tf, {500: "Mon Nov 23 03:00:00 2026"}, sig)
            g.tick(100)
            self.assertEqual(sig, [(100, signal.SIGSTOP)])
            self.assertTrue(g.pause_complete)

    def test_a_record_for_the_supervisors_own_group_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / night.TRAINER_GROUP_NAME
            tf.write_text(json.dumps({"pgid": os.getpgrp(), "start": START}))
            sig = []
            g = self.mk(tf, {os.getpgrp(): START}, sig)
            g.tick(100)
            self.assertEqual(sig, [(100, signal.SIGSTOP)])

    def test_an_unreadable_record_keeps_the_pause_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / night.TRAINER_GROUP_NAME
            tf.write_text("{not json")
            g = self.mk(tf, {}, [])
            g.tick(100)
            self.assertTrue(g.paused)
            self.assertFalse(g.pause_complete)

    def test_run_cycle_tells_the_cycle_where_the_gate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            out = d / "env"
            g = guard(lambda: 4.8, baseline=(4.8, 6.2), trainer_file=d / night.TRAINER_GROUP_NAME)
            code = "import os,sys; open(sys.argv[1],'w').write(os.environ.get('CORTADOS_S1_TRAINER_GROUP_FILE',''))"
            with patch("builtins.print"):
                night.run_cycle([sys.executable, "-c", code, str(out)], g, result_file=d / "r", poll_s=0.05,
                                sleep=time.sleep)
            self.assertEqual(out.read_text(), str(d / night.TRAINER_GROUP_NAME))

    def test_the_gate_records_its_trainer_group_and_removes_it_when_the_group_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "trainer-group.json"
            seen = []
            with open(Path(tmp) / "lock", "w") as lock, patch.dict(os.environ, {cap.TRAINER_GROUP_ENV: str(rec)}), \
                    patch.object(cap, "_proc_start", lambda pid: START):
                rc = cap.run_holding_lock([sys.executable, "-c", "import time; time.sleep(0.3)"], lock.fileno(),
                                          after_spawn=lambda child: seen.append((child.pid, json.loads(rec.read_text()))))
            self.assertEqual(rc, 0)
            pid, body = seen[0]
            self.assertEqual((body["pgid"], body["start"], body["gate_pid"]), (pid, START, os.getpid()))
            self.assertFalse(rec.exists())

    def test_a_group_that_survived_the_stop_keeps_its_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "trainer-group.json"
            with open(Path(tmp) / "lock", "w") as lock, patch.dict(os.environ, {cap.TRAINER_GROUP_ENV: str(rec)}), \
                    patch.object(cap, "_proc_start", lambda pid: START), \
                    patch.object(cap, "_stop_group", lambda child, pgid, grace: (child.wait(), False)[1]):
                cap.run_holding_lock([sys.executable, "-c", "pass"], lock.fileno())
            self.assertTrue(rec.exists())

    def test_the_gate_never_removes_another_gates_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "trainer-group.json"
            rec.write_text(json.dumps({"pgid": 777, "start": START}))
            with redirect_stderr(io.StringIO()):
                cap._clear_trainer_group(str(rec), 778)
            self.assertTrue(rec.exists())

    def test_without_the_variable_the_gate_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != cap.TRAINER_GROUP_ENV}
            with open(Path(tmp) / "lock", "w") as lock, patch.dict(os.environ, env, clear=True):
                cap.run_holding_lock([sys.executable, "-c", "pass"], lock.fileno())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["lock"])


class TrainerStopAtGracefulStop(unittest.TestCase):
    """Item 4 (#116, owner 2026-11-22): the guard must always be able to stop the trainer. A trainer whose gate crashed is
    outside the cycle's group; every graceful stop TERMs the recorded group (identity checked), gives it the gate's
    checkpoint grace, KILLs it if still alive, and clears the record only once the group is gone. Fakes: signaller,
    group_alive, leader_start, clock and sleep; one test runs this test's own processes."""

    def world(self, tmp, *, pgid=500, start=START, honours=("TERM",), starts=None):
        tf = Path(tmp) / night.TRAINER_GROUP_NAME
        tf.write_text(json.dumps({"version": 1, "pgid": pgid, "start": start, "gate_pid": 499}))
        alive, sig, alerts, clock = {pgid}, [], [], Clock(0.0)
        def signaller(g, s):
            sig.append((g, s))
            if (s == signal.SIGTERM and "TERM" in honours) or (s == signal.SIGKILL and "KILL" in honours):
                alive.discard(g)
        g = guard(lambda: 4.8, sig=sig, trainer_file=tf,
                  leader_start=(lambda p: (starts if starts is not None else {pgid: start}).get(p)))
        g.signaller = signaller
        g.group_alive = lambda grp: grp in alive
        g.alerter = lambda n, d: alerts.append(n)
        def sleep(s):
            clock.t += s
        kw = dict(clock=clock, sleep=sleep)
        return g, tf, sig, alerts, kw, clock

    def test_the_grace_is_the_gates_own_checkpoint_grace(self):
        self.assertEqual(night.TRAINER_STOP_GRACE_S, cap.TRAINING_STOP_GRACE_SECONDS)

    def test_a_trainer_that_outlived_its_gate_is_termed_and_its_record_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp)
            self.assertTrue(g.stop_trainer_group(**kw))
            self.assertEqual(sig, [(500, signal.SIGCONT), (500, signal.SIGTERM)])
            self.assertFalse(tf.exists())
            self.assertEqual(alerts, [])

    def test_a_trainer_ignoring_term_is_killed_after_the_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, clock = self.world(tmp, honours=("KILL",))
            self.assertTrue(g.stop_trainer_group(**kw))
            self.assertEqual([s for _, s in sig], [signal.SIGCONT, signal.SIGTERM, signal.SIGKILL])
            self.assertGreaterEqual(clock.t, night.TRAINER_STOP_GRACE_S)   # the whole checkpoint grace first
            self.assertFalse(tf.exists())

    def test_a_group_surviving_kill_keeps_its_record_and_alerts(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp, honours=())
            self.assertFalse(g.stop_trainer_group(**kw))
            self.assertTrue(tf.exists())
            self.assertEqual(alerts, ["guard_trainer_survived"])

    def test_a_reused_pgid_is_never_signalled_and_its_stale_record_is_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp, starts={500: "Mon Nov 23 03:00:00 2026"})
            self.assertTrue(g.stop_trainer_group(**kw))
            self.assertEqual(sig, [])
            self.assertFalse(tf.exists())

    def test_an_unverifiable_record_is_not_signalled_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp)
            def ps_fails(pid):
                raise subprocess.TimeoutExpired("ps", 10)
            g.leader_start = ps_fails
            self.assertFalse(g.stop_trainer_group(**kw))
            self.assertEqual(sig, [])
            self.assertTrue(tf.exists())
            self.assertEqual(alerts, ["guard_trainer_unknown"])

    def test_group_0_1_and_the_supervisors_own_are_never_signalled(self):
        for bad in (0, 1, os.getpgrp()):
            with tempfile.TemporaryDirectory() as tmp:
                g, tf, sig, alerts, kw, _ = self.world(tmp, pgid=bad)
                self.assertFalse(g.stop_trainer_group(**kw))
                self.assertEqual(sig, [], bad)
                self.assertTrue(tf.exists())

    def test_no_record_or_a_gone_group_signals_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp)
            g.group_alive = lambda grp: False
            self.assertTrue(g.stop_trainer_group(**kw))
            self.assertFalse(tf.exists())
            self.assertTrue(g.stop_trainer_group(**kw))   # absent now
            self.assertEqual(sig, [])

    def test_a_record_rewritten_by_another_gate_is_not_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            g, tf, sig, alerts, kw, _ = self.world(tmp)
            orig = g.signaller
            def signaller(grp, s):
                orig(grp, s)
                if s == signal.SIGTERM:
                    tf.write_text(json.dumps({"pgid": 600, "start": START}))
            g.signaller = signaller
            self.assertTrue(g.stop_trainer_group(**kw))
            self.assertEqual(json.loads(tf.read_text())["pgid"], 600)

    def _orphan(self, d):
        """A 'trainer' in its own session, as the gate starts it, whose gate is gone: saves a checkpoint on TERM."""
        ckpt = d / "checkpoint"
        code = ("import signal, sys, time\n"
                "def stop(*a):\n"
                "    open(sys.argv[1], 'w').write('saved'); sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", code, str(ckpt)], start_new_session=True)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        # An orphaned trainer is reaped by launchd once it exits; here this test is its parent, so a thread reaps it (an
        # unreaped zombie would keep its group present to kill(-pgid, 0)).
        threading.Thread(target=p.wait, daemon=True).start()
        deadline = time.monotonic() + 10
        start = None
        while start is None and time.monotonic() < deadline:
            start = night.proc_start(p.pid)
        tf = d / night.TRAINER_GROUP_NAME
        tf.write_text(json.dumps({"version": 1, "pgid": p.pid, "start": start, "gate_pid": 1}))
        time.sleep(0.3)   # let it install its TERM handler
        return p, ckpt, tf

    def _real_guard(self, d, tf, clock):
        g = guard(lambda: 9.0, clock=clock, baseline=(4.8, 6.2), wall=lambda: at(2026, 11, 22, 14), trainer_file=tf,
                  groups=lambda pg: [pg])
        g.signaller = night.signal_group   # real signals, to this test's own processes only
        g.epoch = lambda: at(2026, 11, 22, 14).timestamp()
        g.max_pause = 60
        return g

    def test_stop_for_day_terms_a_trainer_whose_gate_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            p, ckpt, tf = self._orphan(d)
            clock = Clock(0.0)
            g = self._real_guard(d, tf, clock)
            def sleep(s):
                clock.t += 31.0
                time.sleep(0.02)
            with patch("builtins.print"), patch.dict(os.environ, {"CORTADOS_S1_TRAIN_ONLY_IN_WINDOW": "0"}):
                rc, result = night.run_cycle([sys.executable, "-c", "import time; time.sleep(30)"], g,
                                             result_file=d / "r", poll_s=0.05, sleep=sleep, grace_end=10, trainer_grace=10)
            self.assertEqual(result, "stopped_for_day")
            self.assertEqual(p.wait(timeout=10), 0)
            self.assertEqual(ckpt.read_text(), "saved")
            self.assertFalse(tf.exists())

    def test_the_window_end_terms_a_trainer_whose_gate_crashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            p, ckpt, tf = self._orphan(d)
            g = self._real_guard(d, tf, Clock(0.0))
            g.wall = lambda: at(2026, 11, 22, 7, 1)   # the window (01:00-07:00) has ended by the first poll
            with patch("builtins.print"), patch.object(night, "train_policy",
                                                       lambda now: (True, "inside", at(2026, 11, 22, 7))):
                rc, result = night.run_cycle([sys.executable, "-c", "import time; time.sleep(30)"], g,
                                             result_file=d / "r", poll_s=0.05, sleep=time.sleep, grace_end=10,
                                             trainer_grace=10)
            self.assertEqual(result, "window_end")
            self.assertEqual(p.wait(timeout=10), 0)
            self.assertEqual(ckpt.read_text(), "saved")
            self.assertFalse(tf.exists())


TORONTO = night.TORONTO


def at(y, mo, d, h, mi=0):
    return night.datetime(y, mo, d, h, mi, tzinfo=TORONTO)


class StopForDay(unittest.TestCase):
    """B (#116, owner 2026-11-18): still over budget when the longest pause ends -> the trainer is stopped the ordinary
    graceful way (checkpoint kept) and no cycle starts before the next training-window start."""

    def test_the_resume_rule_is_the_next_training_window_start(self):
        cases = [(at(2026, 11, 22, 14), "01:00-07:00", at(2026, 11, 23, 1), "window_start"),
                 (at(2026, 11, 22, 0, 30), "01:00-07:00", at(2026, 11, 22, 1), "window_start"),
                 (at(2026, 11, 22, 2), "01:00-07:00", at(2026, 11, 22, 3), "retry_1h"),   # inside: 1 h (owner 2026-11-22)
                 (at(2026, 11, 22, 6, 30), "01:00-07:00", at(2026, 11, 22, 7, 30), "retry_1h"),
                 (at(2026, 11, 22, 23), "22:00-06:00", at(2026, 11, 23, 0), "retry_1h"),   # a window across midnight
                 (at(2026, 11, 22, 14), "", at(2026, 11, 23, 1), "window_start"),              # unset: the default
                 (at(2026, 11, 22, 14), "off", at(2026, 11, 23, 0), "midnight"),
                 (at(2026, 11, 22, 14), "nonsense", at(2026, 11, 23, 0), "midnight")]
        for now, raw, want, slug in cases:
            until, rule, _ = night.training_resume_at(now, window_raw=raw)
            self.assertEqual((until.timestamp(), rule), (want.timestamp(), slug), (now, raw))

    def test_inside_the_window_the_stop_lasts_one_real_hour_across_a_dst_change(self):
        # 2026-11-01 01:30 EDT (the first of the two 01:30s): one hour later is 01:30 EST, not 02:30 wall time.
        now = night.datetime(2026, 11, 1, 1, 30, tzinfo=TORONTO, fold=0)
        until, rule, words = night.training_resume_at(now, window_raw="01:00-07:00")
        self.assertEqual((until.timestamp() - now.timestamp(), rule), (3600.0, "retry_1h"))
        self.assertIn("inside the training window", words)
        naive = night.datetime(2026, 11, 22, 2)
        self.assertEqual(night.training_resume_at(naive, window_raw="01:00-07:00")[0], night.datetime(2026, 11, 22, 3))

    def test_a_stop_inside_the_window_records_retry_1h_and_the_live_view_accepts_it(self):
        clock = Clock(0.0)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}):
            path = Path(tmp) / night.GUARD_STATE_NAME
            g, alerts = self.mkday(lambda: 9.0, clock, state_path=path)
            g.wall = lambda: at(2026, 11, 22, 3)
            g.epoch = lambda: at(2026, 11, 22, 3).timestamp()
            g.tick(100); clock.t = 61.0; g.tick(100)
            st = g.state()
            self.assertEqual((st["state"], st["until_rule"], st["until"]),
                             ("stopped_for_day", "retry_1h", at(2026, 11, 22, 4).timestamp()))
            self.assertIsNone(live.validate_s1_guard({k: v for k, v in st.items() if k != "version"}, st["updated_at"])[1])
            g2 = guard(lambda: 4.8, state_path=path)
            g2.epoch = lambda: at(2026, 11, 22, 3, 30).timestamp()
            g2.load_day_stop()
            self.assertEqual((g2.day_stop_active(), g2.day_stop_rule), (True, "retry_1h"))
            g2.epoch = lambda: at(2026, 11, 22, 4, 1).timestamp()
            self.assertTrue(g2.day_stop_expired())   # after the hour, cycles may train again

    def mkday(self, latency, clock, **kw):
        alerts = []
        g = guard(latency, clock=clock, baseline=(4.8, 6.2), wall=lambda: at(2026, 11, 22, 14), **kw)
        g.alerter = lambda n, d: alerts.append(n)
        g.epoch = lambda: at(2026, 11, 22, 14).timestamp()
        g.max_pause = 60
        return g, alerts

    def test_over_budget_at_the_pause_limit_stops_for_the_day(self):
        sig = []
        clock = Clock(0.0)
        with patch.dict(os.environ, {"CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}):
            g, alerts = self.mkday(lambda: 9.0, clock, sig=sig)
            g.tick(100)
            clock.t = 61.0
            g.tick(100)
        self.assertTrue(g.stop_requested)
        self.assertEqual(alerts, ["guard_stopped_for_day"])
        self.assertEqual(sig, [(100, signal.SIGSTOP)])            # no release: run_cycle stops the cycle gracefully
        st = g.state()
        self.assertEqual((st["state"], st["reason"], st["until_rule"], st["until"]),
                         ("stopped_for_day", "latency", "window_start", at(2026, 11, 23, 1).timestamp()))
        self.assertIsNone(live.validate_s1_guard({k: v for k, v in st.items() if k != "version"}, st["updated_at"])[1])
        clock.t = 200.0
        g.tick(100)                                                # stopped: no further pause, release or reading
        self.assertEqual(sig, [(100, signal.SIGSTOP)])

    def test_within_budget_at_the_pause_limit_is_still_the_forced_release(self):
        it = iter([9.0] + [None] * 10)
        sig = []
        clock = Clock(0.0)
        g, alerts = self.mkday(lambda: next(it), clock, sig=sig)
        g.tick(100)
        clock.t = 61.0
        g.tick(100)                                                # unreadable at the limit: not evidence of over budget
        self.assertFalse(g.stop_requested)
        self.assertEqual(alerts, ["guard_forced_resume"])

    def test_run_cycle_ends_the_cycle_with_the_graceful_stop_so_the_trainer_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            ckpt = d / "checkpoint"
            code = ("import signal, sys, time\n"
                    "def stop(*a):\n"
                    "    open(sys.argv[1], 'w').write('saved'); sys.exit(0)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    "time.sleep(30)\n")
            clock = Clock(0.0)
            g, alerts = self.mkday(lambda: 9.0, clock)
            def sleep(s):
                clock.t += 31.0
                time.sleep(0.02)
            # A cycle that may train (at 14:00 only with training allowed any time): a labelling-only one is released.
            with patch("builtins.print"), patch.dict(os.environ, {"CORTADOS_S1_TRAIN_ONLY_IN_WINDOW": "0"}):
                rc, result = night.run_cycle([sys.executable, "-c", code, str(ckpt)], g, result_file=d / "r",
                                             poll_s=0.05, sleep=sleep, grace_end=10)
            self.assertEqual((rc, result), (0, "stopped_for_day"))
            self.assertEqual(ckpt.read_text(), "saved")
            self.assertTrue(g.day_stop_active())
            self.assertFalse(g.paused)

    def test_during_a_day_stop_cycles_keep_running_labelling_only_and_the_state_stays_fresh(self):
        # Owner 2026-11-22 (item 2): only training is held; cycles start as usual and the sleeps between them keep the
        # guard's state fresh; the day stop is cleared once its time has passed.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            now = [1_800_000_000.0]
            g = guard(lambda: 4.8, state_path=d / night.GUARD_STATE_NAME)
            g.epoch = lambda: now[0]
            g.interval = 30
            g.clock = lambda: now[0]
            g.day_stop_until, g.day_stop_reason, g.day_stop_rule = now[0] + 3600, "latency", "window_start"
            started, states = [], []
            def sleep(s):
                states.append(json.loads((d / night.GUARD_STATE_NAME).read_text())["state"])
                now[0] += s
            def fake_cycle(*a, **k):
                started.append((now[0], g.day_stop_active()))
                return 0, "idle"
            with patch.object(night, "run_cycle", fake_cycle), patch("builtins.print"):
                night.run_continuous(["true"], g, result_file=d / "r", idle_s=900, sleep=sleep, cycles=6)
            self.assertEqual(started[0], (1_800_000_000.0, True))   # no wait: the first cycle starts at once
            self.assertEqual([a for _, a in started], [True] * 4 + [False] * 2)
            self.assertEqual(set(states[:4 * 30]), {"stopped_for_day"})   # one reading per 30 s between cycles
            self.assertIsNone(g.day_stop_until)

    def test_run_cycle_during_a_day_stop_is_labelling_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            out = d / "env"
            g = guard(lambda: 4.8, baseline=(4.8, 6.2))
            g.epoch = lambda: at(2026, 11, 22, 14).timestamp()
            g.wall = lambda: at(2026, 11, 22, 14)
            g.day_stop_until, g.day_stop_reason, g.day_stop_rule = at(2026, 11, 23, 1).timestamp(), "latency", "window_start"
            code = ("import os,sys; open(sys.argv[1],'w').write(os.environ['CORTADOS_S1_TRAIN_ALLOWED'] + '|' + "
                    "os.environ['CORTADOS_S1_TRAIN_SKIP_REASON'] + '|' + os.environ['CORTADOS_S1_TRAIN_UNTIL'])")
            seen = []
            orig_tick = g.tick
            g.tick = lambda pgid: (seen.append(g.cycle_trains), orig_tick(pgid))
            with patch.dict(os.environ, {"CORTADOS_S1_TRAIN_ONLY_IN_WINDOW": "0"}), patch("builtins.print"):
                night.run_cycle([sys.executable, "-c", code + "; import time; time.sleep(0.2)", str(out)], g,
                                result_file=d / "r", poll_s=0.02, sleep=time.sleep)
            allowed, reason, until = out.read_text().split("|")
            self.assertEqual((allowed, until), ("0", ""))   # training allowed any time, yet held by the day stop
            self.assertIn("stopped for the day by the S1 latency guard", reason)
            self.assertIn("labelling only", reason)
            self.assertIn(False, seen)
            self.assertTrue(g.cycle_trains)   # reset after the cycle

    def test_a_labelling_only_cycle_over_budget_at_the_limit_is_released_not_stopped(self):
        sig, clock = [], Clock(0.0)
        g, alerts = self.mkday(lambda: 9.0, clock, sig=sig)
        g.cycle_trains = False
        g.tick(100)
        clock.t = 61.0
        g.tick(100)
        self.assertFalse(g.stop_requested)
        self.assertIsNone(g.day_stop_until)
        self.assertEqual(alerts, ["guard_forced_resume"])
        self.assertEqual(sig, [(100, signal.SIGSTOP), (100, signal.SIGCONT)])

    def test_a_paused_labelling_cycle_during_a_day_stop_shows_paused_and_keeps_the_stop_across_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            sig, clock = [], Clock(0.0)
            g, _ = self.mkday(lambda: 9.0, clock, sig=sig, state_path=path)
            g.day_stop_until, g.day_stop_reason, g.day_stop_rule = at(2026, 11, 23, 1).timestamp(), "timeout", "window_start"
            g.cycle_trains = False
            g.in_cycle = True
            g.tick(100)
            st = json.loads(path.read_text())
            self.assertEqual((st["state"], st["reason"], st["pause"], st["until_rule"]),
                             ("paused", "latency", "complete", "window_start"))
            self.assertIsNone(live.validate_s1_guard({k: v for k, v in st.items() if k != "version"}, st["updated_at"])[1])
            g2 = guard(lambda: 4.8, state_path=path)
            g2.epoch = lambda: at(2026, 11, 22, 15).timestamp()
            g2.load_day_stop()
            self.assertTrue(g2.day_stop_active())

    def test_no_sweep_shortcut_during_a_day_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            req = d / "sweep-request.json"
            req.write_text("{}")
            now = [at(2026, 11, 22, 3).timestamp()]
            g = guard(lambda: 4.8)
            g.epoch = lambda: now[0]
            g.wall = lambda: at(2026, 11, 22, 3)
            g.interval = 600
            g.day_stop_until, g.day_stop_rule = now[0] + 3600, "retry_1h"
            slept = []
            def sleep(s):
                slept.append(s); now[0] += s
            with patch.object(night, "run_cycle", lambda *a, **k: (0, "quota")), patch("builtins.print"), \
                    patch.dict(os.environ, {"CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}):
                night.run_continuous(["true"], g, result_file=d / "r", idle_s=60, quota_s=7200, sleep=sleep, cycles=1,
                                     sweep_request=req)
            self.assertEqual(sum(slept), 7200)   # the quota cooldown stands: the next cycle labels, it cannot sweep

    def test_the_stop_flag_still_ends_the_loop_during_a_day_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            flag = d / "nightly.stop"
            g = guard(lambda: 4.8)
            g.epoch = lambda: 1_800_000_000.0
            g.day_stop_until = 1_800_000_000.0 + 3600
            flag.write_text("")
            with patch.object(night, "run_cycle", lambda *a, **k: self.fail("no cycle")), patch("builtins.print"):
                self.assertEqual(night.run_continuous(["true"], g, result_file=d / "r", idle_s=60, stopflag=flag), 0)

    def test_a_restarted_supervisor_keeps_the_day_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / night.GUARD_STATE_NAME
            clock = Clock(0.0)
            g, _ = self.mkday(lambda: 9.0, clock, state_path=path)
            with patch.dict(os.environ, {"CORTADOS_S1_NIGHT_WINDOW": "01:00-07:00"}):
                g.tick(100); clock.t = 61.0; g.tick(100)
            g2 = guard(lambda: 4.8, state_path=path)
            g2.epoch = lambda: at(2026, 11, 22, 15).timestamp()
            g2.load_day_stop()
            self.assertTrue(g2.day_stop_active())
            self.assertEqual((g2.day_stop_reason, g2.day_stop_rule), ("latency", "window_start"))
            # A value further ahead than any real day stop (a hand-edited or corrupt file) is not honoured.
            st = json.loads(path.read_text())
            path.write_text(json.dumps({**st, "until": st["until"] + 10 * 86400}))
            g3 = guard(lambda: 4.8, state_path=path)
            g3.epoch = g2.epoch
            g3.load_day_stop()
            self.assertFalse(g3.day_stop_active())

    def test_the_live_view_accepts_and_shows_the_state(self):
        now = 1_800_000_000.0
        st = {"state": "stopped_for_day", "reason": "timeout", "updated_at": now - 5, "until": now + 11 * 3600,
              "until_rule": "window_start", "budget": "day", "budget_s": 1.0, "samples": 1, "timeouts": 1, "pauses_24h": 1}
        g, bad = live.validate_s1_guard(st, now)
        self.assertIsNone(bad)
        self.assertEqual((g["state"], g["until"], g["until_rule"]), ("stopped_for_day", now + 11 * 3600, "window_start"))
        for over, field in (({"until": now + 10 * 86400}, ".until"), ({"until_rule": "tomorrow"}, ".until_rule"),
                            ({"pause": "half"}, ".pause"), ({"until": "soon"}, ".until")):
            self.assertEqual(live.validate_s1_guard({**st, **over}, now), (None, field))
        beats = {"studio": {"received_at": now - 30, "ttl_s": 180, "s1": {"guard": {**g, "updated_at": now - 400}}}}
        self.assertTrue(live.s1_guard_view(beats, now)["stale"])   # a silent supervisor during a day stop is stale
        html = (REPO / "cortados_router/provider/dashboard.html").read_text()
        self.assertIn('"stopped_for_day"', html)
        self.assertIn("training stopped for the day", html)
        self.assertIn('"retry_1h"', html)
        self.assertIn("labelling continues", html)
        self.assertEqual(live.S1_GUARD_UNTIL_RULES, night.DAY_STOP_RULES)



def eperm_signaller(sig, refuse):
    """A fake signaller recording every signal; (pgid, sig) pairs in `refuse` (a set the test may change) raise EPERM."""
    def signaller(pid, s):
        if (pid, s) in refuse or (pid, None) in refuse:
            raise PermissionError(1, "Operation not permitted")
        sig.append((pid, s))
    return signaller


class UnresolvedContinuation(unittest.TestCase):
    """#116 review round 2 (gpt-6.1-sol). Finding 1: a stopped group not confirmed continued (identity undecidable, or EPERM
    from a live member) is never forgotten: kept in the stopped-groups record, reported (state file + alert) and retried
    at every reading, at startup recovery and in continue_all alike. Finding 2: continue_all checks a remembered group's
    identity before SIGCONT. Finding 3: the root's SIGCONT takes the zombie-aware EPERM rule, never a crash. And #131
    item 3: EPERM is handled on every pause / continue / stop path, the production signal_group included."""

    def paused(self, tmp, sig, refuse, *, live=lambda g: [g], starts=None, trees=None, alerts=None, **kw):
        stopped = Path(tmp) / night.STOPPED_GROUPS_NAME
        starts = {100: "a", 200: "c"} if starts is None else starts
        trees = trees or (lambda pg: discovery([pg, 200], starts={pg: "a", 200: "c"}))
        alerts = [] if alerts is None else alerts
        g = guard(lambda: 9.0, sig=sig, groups=trees, baseline=(4.8, 6.2), stopped_path=stopped,
                  leader_start=lambda pid: starts.get(pid), live_members=live, **kw)
        g.signaller = eperm_signaller(sig, refuse)
        g.alerter = lambda n, d: alerts.append(n)
        g.tick(100)
        self.assertTrue(g.paused)
        return g, stopped

    # Finding 1: continue_all and the guard's retry
    def test_continue_all_keeps_a_group_that_refuses_sigcont_and_retries_it_every_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig, refuse, alerts, logs = [], set(), [], []
            g, stopped = self.paused(tmp, sig, refuse, alerts=alerts, logs=logs)
            refuse.add((200, signal.SIGCONT))                      # a live member refuses: EPERM, not zombie-only
            g.continue_all(100)
            self.assertEqual(g.unresolved, {200: "c"})
            self.assertEqual([r["pgid"] for r in night.read_stopped_groups(stopped)[0]], [100, 200])
            g.continue_root(100)
            self.assertEqual([r["pgid"] for r in night.read_stopped_groups(stopped)[0]], [200])   # kept durably
            self.assertEqual(alerts, ["guard_continue_incomplete"])
            g.new_cycle()                                           # a new cycle does not forget it
            self.assertEqual(g.state()["unresolved_groups"], 1)
            g.latency, g.groups = (lambda: 4.8), (lambda pg: [pg])   # the next cycle runs in budget, without group 200
            g.tick(300)                                             # retried: still refused, alerted only once
            self.assertEqual(g.unresolved, {200: "c"})
            self.assertEqual(alerts, ["guard_continue_incomplete"])
            refuse.clear()
            sig.clear()
            g.last = None
            g.idle_probe()                                          # between cycles too
            self.assertIn((200, signal.SIGCONT), sig)
            self.assertEqual(g.unresolved, {})
            self.assertFalse(stopped.exists())
            self.assertEqual(g.state()["unresolved_groups"], 0)
            self.assertIn("now confirmed continued or gone", logs[-1])

    def test_an_eperm_from_a_zombie_only_group_counts_as_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig, refuse = [], set()
            g, stopped = self.paused(tmp, sig, refuse, live=lambda grp: [])
            refuse.add((200, signal.SIGCONT))
            g._continue(100)
            self.assertEqual(g.unresolved, {})
            self.assertFalse(stopped.exists())

    def test_sleep_between_cycles_retries_an_unresolved_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig, refuse = [], {(200, signal.SIGCONT)}
            g, stopped = self.paused(tmp, sig, refuse)
            g._continue(100)
            self.assertEqual(set(g.unresolved), {200})
            slept = []
            def sleep(s):
                slept.append(s)
                refuse.clear()                                       # the member became signallable meanwhile
            g.interval = 30
            night.sleep_between_cycles(g, 120, sleep=sleep)
            self.assertEqual(g.unresolved, {})
            self.assertEqual(slept, [30, 30, 30, 30])
            self.assertFalse(stopped.exists())

    # Finding 1: startup recovery
    def test_recovery_keeps_an_unverifiable_group_reports_it_and_the_guard_retries_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped = d / night.STOPPED_GROUPS_NAME
            night._write_json_durable(stopped, {"version": 1, "groups": [{"pgid": 202, "start": "same"},
                                                                          {"pgid": 205, "start": "y"}]})
            ps_ok = {"v": False}
            def leader_start(pid):
                if pid == 205 and not ps_ok["v"]:
                    raise subprocess.TimeoutExpired("ps", 10)
                return {202: "same", 205: "y"}.get(pid)
            sent, alerts, logs = [], [], []
            done = night.recover_stopped_groups(stopped, None, d / "no.pid", signaller=lambda p, s: sent.append(p),
                                                leader_start=leader_start, log=logs.append,
                                                alerter=lambda n, det: alerts.append(n))
            self.assertEqual((done, sent), ([202], [202]))
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 205, "start": "y"}])   # not discarded
            self.assertEqual(alerts, ["guard_recovery_incomplete"])
            self.assertTrue(any("RECOVERY INCOMPLETE" in m for m in logs))
            sig = []
            g = guard(lambda: 4.8, sig=sig, stopped_path=stopped, leader_start=leader_start, baseline=(4.8, 6.2))
            g.load_unresolved()
            self.assertEqual(g.state()["unresolved_groups"], 1)
            g.tick(100)
            self.assertEqual(sig, [])                               # still undecidable: not signalled on a guess
            ps_ok["v"] = True
            g.tick(100)
            self.assertEqual(sig, [(205, signal.SIGCONT)])
            self.assertFalse(stopped.exists())

    def test_recovery_keeps_a_group_that_refuses_sigcont(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped = d / night.STOPPED_GROUPS_NAME
            night._write_json_durable(stopped, {"version": 1, "groups": [{"pgid": 202, "start": "s"},
                                                                          {"pgid": 203, "start": "t"}]})
            def signaller(p, s):
                raise PermissionError(1, "Operation not permitted")
            alerts = []
            done = night.recover_stopped_groups(stopped, None, d / "no.pid", signaller=signaller,
                                                leader_start=lambda pid: {202: "s", 203: "t"}[pid],
                                                live_members=lambda g: [g] if g == 202 else [], log=lambda m: None,
                                                alerter=lambda n, det: alerts.append(n))
            self.assertEqual(done, [203])                           # 203 was zombie-only: drained
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 202, "start": "s"}])
            self.assertEqual(alerts, ["guard_recovery_incomplete"])

    def test_a_clean_recovery_removes_the_record_and_alerts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped = d / night.STOPPED_GROUPS_NAME
            night._write_json_durable(stopped, {"version": 1, "groups": [{"pgid": 202, "start": "s"}]})
            alerts = []
            night.recover_stopped_groups(stopped, None, d / "no.pid", signaller=lambda p, s: None,
                                         leader_start=lambda pid: "s", log=lambda m: None,
                                         alerter=lambda n, det: alerts.append(n))
            self.assertFalse(stopped.exists())
            self.assertEqual(alerts, [])

    # Finding 2: identity before SIGCONT
    def test_continue_all_never_signals_a_remembered_group_whose_id_was_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig, starts, logs = [], {100: "a", 200: "c"}, []
            trees = iter([discovery([100, 200], starts={100: "a", 200: "c"})] + [discovery([100])] * 10)
            g, stopped = self.paused(tmp, sig, set(), starts=starts, trees=lambda pg: next(trees), logs=logs)
            starts[200] = "someone else"                            # the trainer exited; its id now leads another group
            sig.clear()
            g._continue(100)
            self.assertEqual(sig, [(100, signal.SIGCONT)])
            self.assertEqual(g.unresolved, {})                      # confirmed stale: dropped, not retried
            self.assertFalse(stopped.exists())
            self.assertTrue(any("200 is gone and its id now belongs to another process" in m for m in logs))

    def test_continue_all_keeps_a_remembered_group_it_cannot_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig = []
            trees = iter([discovery([100, 200], starts={100: "a", 200: "c"})] + [discovery([100])] * 10)
            g, stopped = self.paused(tmp, sig, set(), trees=lambda pg: next(trees))
            def ps_fails(pid):
                raise subprocess.TimeoutExpired("ps", 10)
            g.leader_start = ps_fails
            sig.clear()
            g._continue(100)
            self.assertNotIn((200, signal.SIGCONT), sig)
            self.assertEqual(g.unresolved, {200: "c"})
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 200, "start": "c"}])

    def test_a_remembered_group_still_in_the_tree_needs_no_ps(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig = []
            g, stopped = self.paused(tmp, sig, set())
            def ps_fails(pid):
                raise subprocess.TimeoutExpired("ps", 10)
            g.leader_start = ps_fails                               # the fresh tree walk is the identity proof
            sig.clear()
            g._continue(100)
            self.assertEqual(sig, [(100, signal.SIGCONT), (200, signal.SIGCONT)])
            self.assertFalse(stopped.exists())

    # Finding 3: the root's SIGCONT
    def test_a_zombie_only_root_answering_eperm_resumes_without_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig, refuse = [], set()
            g, stopped = self.paused(tmp, sig, refuse, live=lambda grp: [])
            refuse.add((100, signal.SIGCONT))
            g._continue(100)                                        # raised PermissionError before
            self.assertEqual(g.unresolved, {})
            self.assertIn((200, signal.SIGCONT), sig)
            self.assertFalse(stopped.exists())

    def test_a_live_root_refusing_sigcont_is_unresolved_not_a_crash_nor_a_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            it = iter([4.8] * 20)
            sig, refuse, alerts = [], set(), []
            g, stopped = self.paused(tmp, sig, refuse, alerts=alerts)
            g.latency = lambda: next(it)
            refuse.add((100, signal.SIGCONT))
            for _ in range(3):
                g.tick(100)                                         # three good readings: the resume
            self.assertFalse(g.paused)
            self.assertEqual(g.unresolved, {100: "a"})
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 100, "start": "a"}])
            self.assertEqual(g.state()["unresolved_groups"], 1)
            self.assertEqual(alerts, ["guard_continue_incomplete"])
            refuse.clear()
            g.tick(100)                                             # the running cycle's root: retried, now continued
            self.assertEqual(g.unresolved, {})
            self.assertFalse(stopped.exists())

    def test_run_cycle_ends_cleanly_when_the_roots_final_sigcont_is_refused(self):
        sig, alerts = [], []
        g = guard(lambda: 4.8, sig=sig, baseline=(4.8, 6.2), live_members=lambda grp: [grp])
        g.alerter = lambda n, d: alerts.append(n)
        g.signaller = lambda pid, s: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted"))
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.print"):
            rc, _ = night.run_cycle([sys.executable, "-c", "pass"], g, result_file=Path(tmp) / "r", poll_s=0.05,
                                    sleep=time.sleep)
        self.assertEqual(rc, 0)
        self.assertEqual(len(g.unresolved), 1)
        self.assertEqual(alerts, ["guard_continue_incomplete"])

    # #131 item 3: EPERM on every pause / continue / stop path
    def test_the_production_signaller_eperm_never_crashes_pause_or_resume(self):
        # signal_group itself handles only ESRCH: this is the call that crashed the Studio's supervisor on 2026-11-22.
        it = iter([9.0] + [4.8] * 20)
        alerts = []
        g = night.Guard(latency=lambda: next(it), memory=lambda: 100.0, budget_s=1.0, min_free_gb=8, interval_s=0,
                        max_pause_s=1800, signaller=night.signal_group, groups=lambda pg: [pg, 200],
                        leader_start=lambda pid: None, live_members=lambda grp: [grp], log=lambda m: None,
                        alerter=lambda n, d: alerts.append(n), night_window=None, clock=Clock())
        g.baseline_samples, g.baseline_p90_samples = [4.8], [6.2]
        with patch.object(night.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")):
            g.tick(100)                                             # _pause -> signal_group: EPERM
            self.assertTrue(g.paused)
            self.assertFalse(g.pause_complete)
            g.tick(100)                                             # _complete_pause retry: EPERM again
            g.pause_complete = True                                 # (as if it then landed) so the resume can run
            for _ in range(3):
                g.tick(100)                                         # _continue -> continue_root + continue_all: EPERM
        self.assertFalse(g.paused)
        self.assertEqual(set(g.unresolved), {100, 200})
        self.assertEqual(alerts, ["guard_continue_incomplete"])

    def test_stop_trainer_group_survives_eperm_on_every_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tf = Path(tmp) / night.TRAINER_GROUP_NAME
            tf.write_text(json.dumps({"version": 1, "pgid": 500, "start": START, "gate_pid": 499}))
            clock, alerts = Clock(0.0), []
            g = guard(lambda: 4.8, trainer_file=tf, leader_start=lambda p: START)
            g.signaller = lambda pid, s: (_ for _ in ()).throw(PermissionError(1, "Operation not permitted"))
            g.group_alive = lambda grp: True
            g.alerter = lambda n, d: alerts.append(n)
            def sleep(s):
                clock.t += s
            self.assertFalse(g.stop_trainer_group(clock=clock, sleep=sleep))
            self.assertTrue(tf.exists())                            # record kept: the guard still finds it
            self.assertEqual(alerts, ["guard_trainer_survived"])

    def test_the_cycle_stop_survives_a_live_member_refusing_the_signal(self):
        class Child:
            pid = 4242
            returncode = None
            def wait(self, timeout=None):
                return 0
            def poll(self):
                return None
        alerts = []
        g = guard(lambda: 4.8)
        g.alerter = lambda n, d: alerts.append(n)
        with patch.object(night.os, "killpg", side_effect=PermissionError(1, "Operation not permitted")), \
                patch.object(night, "_live_group_members", return_value=[4243]), patch("builtins.print"):
            night.stop_cycle_group(Child(), g, grace=1.0)           # stop_group_drained re-raised EPERM before
        self.assertEqual(alerts, ["guard_stop_incomplete"])

    def test_recover_pidfile_survives_eperm_and_records_the_root_as_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            pidfile, stopped = d / "nightly.pid", d / night.STOPPED_GROUPS_NAME
            pidfile.write_text("4242\n")
            alerts = []
            def signaller(p, s):
                raise PermissionError(1, "Operation not permitted")
            night.recover_pidfile(pidfile, alive=lambda p: True, ps=lambda p: "T", signaller=signaller,
                                  log=lambda m: None, stopped_path=stopped, leader_start=lambda p: START,
                                  live_members=lambda g: [g], alerter=lambda n, det: alerts.append(n))
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 4242, "start": START}])
            self.assertEqual(alerts, ["guard_recovery_incomplete"])
            g = guard(lambda: 4.8, stopped_path=stopped)
            g.load_unresolved()
            self.assertEqual(g.unresolved, {4242: START})


class StartupRootRecovery(unittest.TestCase):
    """#116 review round 3: recover_stopped_groups deleted the live root's stopped record and left it to recover_pidfile;
    a failed or timed-out status lookup there then forgot a root that may still be SIGSTOPped, blocking every later cycle."""

    def _paused_then_killed(self, d):
        stopped, pidfile = d / night.STOPPED_GROUPS_NAME, d / "nightly.pid"
        g = guard(lambda: 9.0, groups=lambda pg: discovery([pg, 200], starts={pg: "a", 200: "c"}),
                  baseline=(4.8, 6.2), stopped_path=stopped, leader_start=lambda pid: {100: "a"}.get(pid))
        g.tick(100)   # the supervisor pauses cycle 100 (and its trainer 200), then dies: no SIGCONT ever sent
        self.assertTrue(g.paused)
        pidfile.write_text("100\n")
        return stopped, pidfile

    def _recover(self, d, stopped, pidfile, ps, sent, alerts):
        starts = {100: "a", 200: "c"}
        night.recover_stopped_groups(stopped, None, pidfile, signaller=lambda p, s: sent.append((p, s)),
                                     leader_start=lambda pid: starts.get(pid), alive=lambda pid: True,
                                     discover=lambda root: discovery([root, 200]), log=lambda m: None,
                                     alerter=lambda n, det: alerts.append(n))
        night.recover_pidfile(pidfile, alive=lambda p: True, ps=ps, signaller=lambda p, s: sent.append((p, s)),
                              log=lambda m: None, stopped_path=stopped, leader_start=lambda pid: starts.get(pid),
                              alerter=lambda n, det: alerts.append(n))

    def _assert_root_kept_and_retried(self, ps):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped, pidfile = self._paused_then_killed(d)
            sent, alerts = [], []
            self._recover(d, stopped, pidfile, ps, sent, alerts)
            self.assertEqual(sent, [(200, signal.SIGCONT)])   # the trainer continued; the root's state is unknown
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 100, "start": "a"}])
            self.assertEqual(alerts, ["guard_recovery_incomplete"])
            sig = []
            g = guard(lambda: 4.8, sig=sig, stopped_path=stopped, leader_start=lambda pid: {100: "a"}.get(pid))
            g.load_unresolved()
            self.assertEqual(g.unresolved, {100: "a"})
            g.retry_unresolved()   # the next guard reading continues the old root
            self.assertEqual(sig, [(100, signal.SIGCONT)])
            self.assertEqual(g.unresolved, {})
            self.assertFalse(stopped.exists())

    def test_a_timed_out_root_status_lookup_keeps_the_root_unresolved(self):
        def ps(pid):
            raise subprocess.TimeoutExpired("ps", 10)
        self._assert_root_kept_and_retried(ps)

    def test_a_failed_root_status_lookup_keeps_the_root_unresolved(self):
        def ps(pid):
            raise OSError(5, "Input/output error")
        self._assert_root_kept_and_retried(ps)

    def test_a_root_with_no_record_is_recorded_when_its_status_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped, pidfile = d / night.STOPPED_GROUPS_NAME, d / "nightly.pid"
            pidfile.write_text("100\n")
            alerts = []
            def ps(pid):
                raise subprocess.TimeoutExpired("ps", 10)
            self._recover(d, stopped, pidfile, ps, [], alerts)
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 100, "start": "a"}])
            self.assertEqual(alerts, ["guard_recovery_incomplete"])

    def test_the_root_record_is_dropped_once_continued_running_or_gone(self):
        for ps, alive in (("T", True), ("S", True), ("T", False)):
            with self.subTest(ps=ps, alive=alive), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                stopped, pidfile = self._paused_then_killed(d)
                sent, alerts = [], []
                starts = {100: "a", 200: "c"}
                night.recover_stopped_groups(stopped, None, pidfile, signaller=lambda p, s: sent.append((p, s)),
                                             leader_start=lambda pid: starts.get(pid), alive=lambda pid: alive,
                                             discover=lambda root: discovery([root, 200]), log=lambda m: None,
                                             alerter=lambda n, det: alerts.append(n))
                self.assertEqual(alerts, [])   # the root's own record is recover_pidfile's, not an incomplete recovery
                night.recover_pidfile(pidfile, alive=lambda p: alive, ps=lambda p: ps,
                                      signaller=lambda p, s: sent.append((p, s)), log=lambda m: None,
                                      stopped_path=stopped, alerter=lambda n, det: alerts.append(n))
                self.assertFalse(stopped.exists())
                self.assertEqual(alerts, [])

    def test_the_root_record_survives_a_crash_between_the_two_recoveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped, pidfile = self._paused_then_killed(d)
            starts = {100: "a", 200: "c"}
            night.recover_stopped_groups(stopped, None, pidfile, signaller=lambda p, s: None,
                                         leader_start=lambda pid: starts.get(pid), alive=lambda pid: True,
                                         discover=lambda root: discovery([root, 200]), log=lambda m: None)
            # recover_pidfile never ran (the supervisor died again): the root is still on record for the next start.
            self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 100, "start": "a"}])



class RootIdentityFromPidfile(unittest.TestCase):
    """#116 review round 6 (gpt-6.1-sol): a stopped root with no identity record whose status and start-time probes both
    failed at startup was recorded with start null, and group_is_ours then answered unknown for that live leader forever:
    never continued, its pidfile blocking every later cycle. The pidfile's write time is now recorded as the identity
    evidence: a leader that started no later than the write is the process the pidfile named; one that started after it
    is a reused pid and is never signalled."""

    WRITTEN = float(int(time.time()) - 3600)

    def lstart(self, epoch):
        return time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(epoch))

    def _recover_with_ps_down(self, d):
        stopped, pidfile = d / night.STOPPED_GROUPS_NAME, d / "nightly.pid"
        pidfile.write_text("100\n")
        os.utime(pidfile, (self.WRITTEN, self.WRITTEN))
        def down(pid):
            raise subprocess.TimeoutExpired("ps", 10)
        sent, alerts = [], []
        night.recover_stopped_groups(stopped, None, pidfile, signaller=lambda p, s: sent.append((p, s)),
                                     leader_start=down, alive=lambda pid: True,
                                     discover=lambda root: night.Discovery([root], False, {}), log=lambda m: None,
                                     alerter=lambda n, det: alerts.append(n))
        night.recover_pidfile(pidfile, alive=lambda p: True, ps=down, signaller=lambda p, s: sent.append((p, s)),
                              log=lambda m: None, stopped_path=stopped, leader_start=down,
                              alerter=lambda n, det: alerts.append(n))
        self.assertEqual(sent, [])
        self.assertEqual(alerts, ["guard_recovery_incomplete"])
        self.assertEqual(night.read_stopped_groups(stopped)[0], [{"pgid": 100, "start": None, "written": self.WRITTEN}])
        return stopped

    def _guard_after_restart(self, stopped, starts):
        sig, state = [], {"ps": "down"}
        def leader_start(pid):
            if state["ps"] == "down":
                raise OSError(5, "Input/output error")
            return starts.get(pid)
        g = guard(lambda: 4.8, sig=sig, stopped_path=stopped, leader_start=leader_start)
        g.load_unresolved()
        return g, sig, state

    def test_transient_ps_failure_then_recovery_continues_the_root_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped = self._recover_with_ps_down(Path(tmp))
            g, sig, state = self._guard_after_restart(stopped, {100: self.lstart(self.WRITTEN - 120)})
            g.retry_unresolved()                       # ps still down: kept, not signalled on a guess
            self.assertEqual(sig, [])
            self.assertEqual(g.unresolved, {100: None})
            self.assertEqual(night.read_stopped_groups(stopped)[0][0].get("written"), self.WRITTEN)   # evidence kept
            state["ps"] = "up"
            g.retry_unresolved()                       # ps works again: started before the pidfile write -> ours
            g.retry_unresolved()
            self.assertEqual(sig, [(100, signal.SIGCONT)])
            self.assertEqual(g.unresolved, {})
            self.assertFalse(stopped.exists())

    def test_a_leader_started_in_the_same_second_as_the_pidfile_write_is_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped = self._recover_with_ps_down(Path(tmp))
            g, sig, state = self._guard_after_restart(stopped, {100: self.lstart(self.WRITTEN)})
            state["ps"] = "up"
            g.retry_unresolved()
            self.assertEqual(sig, [(100, signal.SIGCONT)])

    def test_a_reused_pid_started_after_the_pidfile_write_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped = self._recover_with_ps_down(Path(tmp))
            g, sig, state = self._guard_after_restart(stopped, {100: self.lstart(self.WRITTEN + 5)})
            state["ps"] = "up"
            for _ in range(3):
                g.retry_unresolved()
            self.assertEqual(sig, [])
            self.assertEqual(g.unresolved, {})         # another process's now: dropped, never signalled
            self.assertFalse(stopped.exists())

    def test_the_evidence_survives_a_restart_between_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            stopped = self._recover_with_ps_down(Path(tmp))
            g, sig, _ = self._guard_after_restart(stopped, {})
            g.retry_unresolved()
            g2, sig2, state = self._guard_after_restart(stopped, {100: self.lstart(self.WRITTEN - 1)})
            self.assertEqual(g2.unresolved_written, {100: self.WRITTEN})
            state["ps"] = "up"
            g2.retry_unresolved()
            self.assertEqual(sig + sig2, [(100, signal.SIGCONT)])

    def test_an_existing_null_record_gets_the_write_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stopped = d / night.STOPPED_GROUPS_NAME
            stopped.write_text(json.dumps({"version": 1, "groups": [{"pgid": 100, "start": None}]}))
            self._recover_with_ps_down(d)   # asserts the root record now carries the pidfile's write time

    def test_group_is_ours_without_evidence_or_with_an_unparseable_start_stays_unknown(self):
        st = self.lstart(self.WRITTEN - 10)
        self.assertIsNone(night.group_is_ours({"pgid": 100, "start": None}, leader_start=lambda p: st))
        self.assertIsNone(night.group_is_ours({"pgid": 100, "start": None, "written": self.WRITTEN},
                                              leader_start=lambda p: "not a date"))
        self.assertIsNone(night.group_is_ours({"pgid": 100, "start": None, "written": True}, leader_start=lambda p: st))
        self.assertTrue(night.group_is_ours({"pgid": 100, "start": None, "written": self.WRITTEN}, leader_start=lambda p: st))
        self.assertTrue(night.group_is_ours({"pgid": 100, "start": None, "written": self.WRITTEN},
                                            leader_start=lambda p: None))   # leader gone: as before

FAKE_PS = """#!/bin/sh
printf '%s' "$FAKE_PS_OUT"
exit "${FAKE_PS_RC:-0}"
"""


class RealPsWrapperFailures(unittest.TestCase):
    """#116 review round 5: the status wrapper recover_pidfile runs by default ignored ps's exit status, so a ps that failed
    with empty stdout while the root was still stopped read as "running" and the durable record was deleted without a
    SIGCONT. These run the real subprocess wrappers against a fake `ps` executable first on PATH."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        bindir = Path(self._tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "ps").write_text(FAKE_PS)
        (bindir / "ps").chmod(0o755)
        self.path = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"

    def _fake_ps(self, rc, out=""):
        return patch.dict(os.environ, {"PATH": self.path, "FAKE_PS_RC": str(rc), "FAKE_PS_OUT": out})

    def test_a_failed_or_unrecognized_root_status_keeps_the_stopped_root_unresolved(self):
        for rc, out in ((1, ""), (2, ""), (0, ""), (0, "   \n"), (0, "?"), (0, "garbage"), (1, "T")):
            with self.subTest(rc=rc, out=out):
                with self._fake_ps(rc, out):
                    StartupRootRecovery()._assert_root_kept_and_retried(None)   # None: the real default wrapper

    def test_a_readable_root_status_still_resolves(self):
        for out, sent_root in (("T\n", True), ("Ss\n", False), ("R+\n", False)):
            with self.subTest(out=out), tempfile.TemporaryDirectory() as tmp:
                d = Path(tmp)
                case = StartupRootRecovery()
                stopped, pidfile = case._paused_then_killed(d)
                sent, alerts = [], []
                with self._fake_ps(0, out):
                    case._recover(d, stopped, pidfile, None, sent, alerts)
                self.assertEqual((100, signal.SIGCONT) in sent, sent_root)
                self.assertFalse(stopped.exists())
                self.assertEqual(alerts, [])

    def test_proc_start_treats_a_failed_or_empty_ps_as_unknown_never_gone(self):
        for rc, out in ((0, ""), (2, ""), (0, "  \n")):
            with self.subTest(rc=rc, out=out), self._fake_ps(rc, out):
                with self.assertRaises(subprocess.SubprocessError):
                    night.proc_start(4242)
                self.assertIsNone(night.group_is_ours({"pgid": 4242, "start": "a"}))
        with self._fake_ps(1, ""):
            self.assertIsNone(night.proc_start(4242))          # BSD ps: exit 1, no row = no such process
        with self._fake_ps(0, "Sun Nov 22 14:57:48 2026\n"):
            self.assertEqual(night.proc_start(4242), "Sun Nov 22 14:57:48 2026")

    def test_the_gate_never_records_a_failed_ps_answer_as_the_trainer_start(self):
        for rc, out in ((1, "Sun Nov 22 14:57:48 2026\n"), (2, "junk"), (0, ""), (0, " \n")):
            with self.subTest(rc=rc, out=out), self._fake_ps(rc, out):
                self.assertIsNone(cap._proc_start(4242))
        with self._fake_ps(0, "Sun Nov 22  4:57:48 2026\n"):
            self.assertEqual(cap._proc_start(4242), "Sun Nov 22 4:57:48 2026")

    def test_an_empty_or_failed_process_listing_is_never_a_complete_discovery(self):
        for rc, out in ((0, ""), (1, ""), (0, "garbage\n")):
            with self.subTest(rc=rc, out=out), self._fake_ps(rc, out):
                d = night.discover_groups(100, log=lambda m: None)
                self.assertFalse(d.complete)
                self.assertEqual(d.groups, [100])

    def test_an_empty_or_failed_member_listing_is_never_drained(self):
        for rc, out in ((0, ""), (1, ""), (0, "garbage\n")):
            with self.subTest(rc=rc, out=out), self._fake_ps(rc, out):
                with self.assertRaises(subprocess.SubprocessError):
                    night._live_group_members(4242)


if __name__ == "__main__":
    unittest.main()
