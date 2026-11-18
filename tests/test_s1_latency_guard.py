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
            g = guard(lambda: 9.0, clock=clock, state_path=path, baseline=(4.8, 6.2), release_hold_s=600)
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
         "timeouts": 1, "source": "scorer", "pauses_24h": 2}

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


if __name__ == "__main__":
    unittest.main()
