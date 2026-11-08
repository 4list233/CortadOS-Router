"""s1/bench_local_capacity.py with fakes only: fake HTTP servers for Ollama and S1, an injected popen and sampler, a
temp CORTADOS_HOME. A real ollama is never started and no real S1 is called."""
import fcntl
import http.server
import io
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "s1"))
import bench_local_capacity as b  # noqa: E402
from cortados_router import studio_capacity as sc  # noqa: E402

GIB = 2 ** 30


class FakeHandler(http.server.BaseHTTPRequestHandler):
    """Ollama + S1 in one: behaviour comes from server.state."""

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st = self.server.state
        if self.path == "/api/ps":
            self._json(200, {"models": st.get("ps", [])})
        elif self.path == "/api/version":
            self._json(200, {"version": "fake"})
        else:
            self._json(404, {})

    def do_POST(self):
        st = self.server.state
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        st.setdefault("posts", []).append((self.path, body))
        if self.path == "/score":
            if st.get("s1_fail"):
                self._json(503, {"error": "busy"})
            else:
                self._json(200, {"answers": [1], "ms": 5})
        elif self.path == "/api/generate":
            self._json(200, {"done": True})
        elif self.path == "/api/chat":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(json.dumps({"message": {"content": "hi"}, "done": False}).encode() + b"\n")
            self.wfile.write(json.dumps({"done": True, "eval_count": 10, "eval_duration": int(1e9)}).encode() + b"\n")
        else:
            self._json(404, {})


def fake_server(state):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    srv.state = state
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


class FakeProc:
    _next = [50000]

    def __init__(self):
        self.signals, self.returncode = [], None
        self.pid = FakeProc._next[0]
        FakeProc._next[0] += 1
        self.on_signal = None

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if self.on_signal:
            self.on_signal()
        self.returncode = -sig

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.chmod(self.home, 0o700)
        self.gate = sc.gate_dir(self.home)
        self.prod_state = {}
        self.prod, self.prod_url = fake_server(self.prod_state)
        self.addCleanup(self.prod.shutdown)
        self.procs = []

    def popen(self, argv, env=None, **kw):
        self.assertNotEqual(env["OLLAMA_HOST"], "127.0.0.1:11434")
        self.envs = getattr(self, "envs", []) + [env]
        p = FakeProc()
        self.procs.append(p)
        return p

    def args(self, extra=()):
        a = b.parse_args(["--s1-url", self.prod_url, "--production-url", self.prod_url, "--steps", "1,2,4",
                          "--rounds", "1", "--s1-interval", "0.01", "--port", str(self.prod.server_address[1]),
                          "--out", os.path.join(self.home, "out.json")] + list(extra))
        return a

    def deps(self, sampler=None, session=None, **kw):
        sampler = sampler or (lambda url: {"available_bytes": 20 * GIB, "swap_bytes": 0, "ollama_size": 7 * GIB,
                                           "ollama_vram": 7 * GIB})
        # The fake Ollama lives on the bench port, so the occupied-port check and the listener-owner check are
        # injected here; OccupiedPort below exercises the real ones.
        kw.setdefault("in_use", lambda port: False)
        kw.setdefault("listeners", lambda port: ({self.procs[-1].pid}, {self.procs[-1].pid}))
        return b.Deps(popen=self.popen, sampler=sampler, session=session or b.run_session, sleep=lambda s: None, **kw)


class Preconditions(Base):
    def test_refuses_when_training_holds_lock_ex(self):
        fd = sc.open_node_lock(self.gate)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(b.Refused, "training holds"):
            b.preconditions(self.gate, self.prod_url)
        self.assertEqual(self.procs, [])

    def test_refuses_on_live_intent(self):
        sc.ensure_gate_dir(self.gate)
        (Path(self.gate) / "intent-x.json").write_text("garbage")  # malformed + fresh counts as live (fail closed)
        with self.assertRaisesRegex(b.Refused, "intent"):
            b.preconditions(self.gate, self.prod_url)

    def test_refuses_when_production_has_model_loaded(self):
        self.prod_state["ps"] = [{"name": "some-model", "size": 1}]
        with self.assertRaisesRegex(b.Refused, "production Ollama has a model loaded"):
            b.preconditions(self.gate, self.prod_url)

    def test_holds_shared_lock_so_training_cannot_start(self):
        fd = b.preconditions(self.gate, self.prod_url)
        try:
            other = sc.open_node_lock(self.gate)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        finally:
            os.close(fd)

    def test_dry_run_refuses_and_loads_nothing(self):
        self.prod_state["ps"] = [{"name": "m"}]
        out = io.StringIO()
        with redirect_stdout(out):
            rc = b.main(["--s1-url", self.prod_url, "--production-url", self.prod_url, "--dry-run"],
                        deps=self.deps(), gate=self.gate)
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", out.getvalue())
        self.assertEqual(self.procs, [])

    def test_s1_url_required(self):
        os.environ.pop("CORTADOS_BENCH_S1_URL", None)
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                unittest.mock.patch("sys.stderr", io.StringIO()):
            b.parse_args([])


REC = dict(n=4, errors=0, s1_failures=0, s1_p95_s=1.2, sampler_errors=0, min_available_gib=10.0, swap_growth_mib=0.0)


class Verdict(unittest.TestCase):
    def v(self, **kw):
        return b.verdict(dict(REC, **kw), 1.0, 1.0, 256, 4.0)

    def test_pass(self):
        self.assertEqual(self.v(), ("PASS", []))

    def test_each_fail_reason(self):
        cases = {"request error": dict(errors=1), "S1 failure": dict(s1_failures=2),
                 "S1 p95": dict(s1_p95_s=2.5), "swap grew": dict(swap_growth_mib=300.0),
                 "reserve": dict(min_available_gib=3.5), "no successful S1": dict(s1_p95_s=None),
                 "sampling failed": dict(sampler_errors=1)}
        for text, kw in cases.items():
            verdict, reasons = self.v(**kw)
            self.assertEqual(verdict, "FAIL", text)
            self.assertIn(text, "; ".join(reasons))

    def test_recommended_cap(self):
        steps = [dict(n=1, verdict="PASS"), dict(n=2, verdict="PASS"), dict(n=4, verdict="FAIL")]
        self.assertEqual(b.recommended_cap(steps), 2)
        self.assertIsNone(b.recommended_cap([dict(n=1, verdict="FAIL")]))

    def test_summarize_measures(self):
        samples = [{"available_bytes": 9 * GIB, "swap_bytes": 0, "ollama_size": GIB, "ollama_vram": GIB},
                   {"available_bytes": 5 * GIB, "swap_bytes": 100 * 2 ** 20, "ollama_size": 2 * GIB, "ollama_vram": GIB}]
        sessions = [dict(ok=True, ttft_s=0.5, tokens=10, decode_tps=20.0), dict(ok=False, error="x")]
        rec = b.summarize(2, sessions, [2.0], [(True, 0.3, None), (False, None, "e")], samples)
        self.assertEqual((rec["errors"], rec["s1_failures"]), (1, 1))
        self.assertAlmostEqual(rec["min_available_gib"], 5.0)
        self.assertAlmostEqual(rec["swap_growth_mib"], 100.0)
        self.assertAlmostEqual(rec["ollama_resident_gib"], 2.0)
        self.assertAlmostEqual(rec["aggregate_tps"], 5.0)


class Bench(Base):
    def test_full_run_with_fake_servers(self):
        args = self.args()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = b.main(["--s1-url", self.prod_url, "--production-url", self.prod_url, "--steps", "1,2",
                         "--rounds", "1", "--s1-interval", "0.01", "--port", str(self.prod.server_address[1]),
                         "--out", args.out], deps=self.deps(), gate=self.gate)
        self.assertEqual(rc, 0)
        report = json.load(open(args.out))
        self.assertEqual([s["verdict"] for s in report["steps"]], ["PASS", "PASS"])
        self.assertEqual(report["recommended_cap"], 2)
        self.assertIn("NUM_PARALLEL", out.getvalue())
        self.assertIn("uncapped", out.getvalue())
        self.assertEqual([e["OLLAMA_NUM_PARALLEL"] for e in self.envs], ["1", "2"])
        self.assertTrue(all(p.signals for p in self.procs))  # every step's server stopped

    def test_stops_at_first_fail(self):
        calls = []

        def sampler(url):
            calls.append(url)
            n = len(self.procs)
            return {"available_bytes": (20 if n < 2 else 2) * GIB, "swap_bytes": 0, "ollama_size": 0, "ollama_vram": 0}

        report = b.run_bench(self.args(), self.deps(sampler=sampler), self.gate)
        self.assertEqual([(s["n"], s["verdict"]) for s in report["steps"]], [(1, "PASS"), (2, "FAIL")])
        self.assertEqual(report["recommended_cap"], 1)
        self.assertEqual(len(self.procs), 2)  # step 4 never started

    def test_server_stopped_and_lock_released_on_exception(self):
        def boom(*a, **kw):
            raise RuntimeError("session exploded")

        with self.assertRaisesRegex(RuntimeError, "exploded"):
            b.run_bench(self.args(), self.deps(session=boom), self.gate)
        self.assertEqual(len(self.procs), 1)
        self.assertTrue(self.procs[0].signals)
        fd = sc.open_node_lock(self.gate)  # LOCK_SH released: training could take LOCK_EX now
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_s1_failure_during_step_fails(self):
        args = self.args()
        deps = self.deps()
        real = deps.s1
        state = {"n": 0}

        def s1(url, token):
            state["n"] += 1
            return real(url, token) if state["n"] <= b.S1_BASELINE_PROBES else (False, 0.1, "down")

        deps.s1 = s1
        report = b.run_bench(args, deps, self.gate)
        self.assertEqual(report["steps"][0]["verdict"], "FAIL")
        self.assertIn("S1 failure", "; ".join(report["steps"][0]["reasons"]))


class Percentile(unittest.TestCase):
    def test_exact_integer_ranks(self):
        self.assertEqual(b.pct(range(1, 21), 95), 19)
        self.assertEqual(b.pct(range(1, 11), 50), 5)
        self.assertEqual(b.pct([7], 95), 7)
        self.assertEqual(b.pct([7], 50), 7)
        self.assertEqual(b.pct(range(1, 21), 100), 20)
        self.assertIsNone(b.pct([], 50))


class TrickleHandler(http.server.BaseHTTPRequestHandler):
    """Streams a non-final NDJSON line every 50 ms forever: never idle long enough for an inactivity timeout."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            for _ in range(400):  # 20 s ceiling so the fake thread itself ends
                self.wfile.write(json.dumps({"message": {"content": "x"}, "done": False}).encode() + b"\n")
                self.wfile.flush()
                time.sleep(0.05)
        except OSError as exc:  # the client closed at its deadline: expected, record it
            self.server.closed.append(exc)


class DelayedCloseHandler(http.server.BaseHTTPRequestHandler):
    """Waits header_delay, sends headers with Connection: close, waits body_delay, then one complete `done` line."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        time.sleep(self.server.header_delay)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            time.sleep(self.server.body_delay)
            self.wfile.write(json.dumps({"message": {"content": "x"}, "done": True, "eval_count": 1,
                                         "eval_duration": 1000}).encode() + b"\n")
            self.wfile.flush()
        except OSError as exc:  # the client closed at its deadline: expected, record it
            self.server.finished.append(exc)
        self.close_connection = True


class Deadlines(unittest.TestCase):
    def test_trickling_response_stops_at_deadline_as_error(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
        srv.closed = []
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        t0 = time.monotonic()
        res = b.run_session(url, "m", "p", 1, timeout=0.5)
        took = time.monotonic() - t0
        self.assertFalse(res["ok"])
        self.assertIn("DeadlineExceeded", res["error"])
        self.assertLess(took, 3.0)

    def test_round_join_bounded_and_counts_error(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*a, **kw):
            release.wait(30)
            return dict(ok=True)

        t0 = time.monotonic()
        results, _ = b.run_round("http://x", "m", 2, 10, 1, 0, session=hang, request_timeout=0.2,
                                 stop=sc.StopRequest())
        self.assertLess(time.monotonic() - t0, b.JOIN_SLACK_S + 2)
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertIn("round deadline", results[0]["error"])

    def delayed_server(self, header_delay, body_delay):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DelayedCloseHandler)
        srv.daemon_threads = True
        srv.header_delay, srv.body_delay = header_delay, body_delay
        srv.finished = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, "http://127.0.0.1:%d" % srv.server_address[1]

    def test_connection_close_reply_past_deadline_is_error(self):
        # headers at 0.15 s and a complete `done` line at 0.30 s, Connection: close; the deadline is 0.2 s
        _, url = self.delayed_server(0.15, 0.15)
        t0 = time.monotonic()
        res = b.run_session(url, "m", "p", 1, timeout=0.2)
        took = time.monotonic() - t0
        self.assertFalse(res["ok"], res)
        self.assertIn("DeadlineExceeded", res["error"])
        self.assertLess(took, 2.0)

    def test_connection_close_reply_within_deadline_is_ok(self):
        _, url = self.delayed_server(0.0, 0.0)
        res = b.run_session(url, "m", "p", 1, timeout=2.0)
        self.assertTrue(res["ok"], res)

    def test_run_round_passes_request_timeout_to_sessions(self):
        # the server holds headers 3 s: without timeout=request_timeout the real session keeps the 900 s default and
        # is abandoned, still running, only at the round deadline (0.2 + JOIN_SLACK_S)
        _, url = self.delayed_server(3.0, 0.0)
        t0 = time.monotonic()
        results, _ = b.run_round(url, "m", 2, 10, 1, 0, request_timeout=0.2, stop=sc.StopRequest())
        took = time.monotonic() - t0
        self.assertEqual([r["ok"] for r in results], [False, False])
        for r in results:  # ended by the session's own deadline (thread finished), not abandoned at the round deadline
            self.assertIn("DeadlineExceeded", r["error"])
        self.assertLess(took, 1.5)


class OccupiedPort(Base):
    def test_refuses_when_bench_port_has_listener(self):
        other_state = {}
        other, _ = fake_server(other_state)
        self.addCleanup(other.shutdown)
        args = self.args(["--port", str(other.server_address[1])])
        deps = self.deps(in_use=b.port_in_use)
        with self.assertRaisesRegex(b.Refused, "already has a listener"):
            b.run_bench(args, deps, self.gate)
        self.assertEqual(self.procs, [])
        paths = [p for p, _ in other_state.get("posts", [])]
        self.assertNotIn("/api/generate", paths)
        self.assertNotIn("/api/chat", paths)
        self.assertLockFree()

    def test_refuses_when_listener_is_not_our_child(self):
        deps = self.deps(listeners=lambda port: ({4242}, {4242, 1}))
        with self.assertRaisesRegex(b.Refused, "not our ollama serve"):
            b.run_bench(self.args(), deps, self.gate)
        self.assertTrue(self.procs[0].signals)  # the spawned child was stopped
        self.assertNotIn("/api/generate", [p for p, _ in self.prod_state.get("posts", [])])

    def assertLockFree(self):
        fd = sc.open_node_lock(self.gate)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


class WarmupInVerdict(Base):
    def test_swap_growth_during_warmup_fails(self):
        def sampler(url):
            warmed = any(p == "/api/generate" for p, _ in self.prod_state.get("posts", []))
            return {"available_bytes": 20 * GIB, "swap_bytes": (1 * GIB if warmed else 0), "ollama_size": 0,
                    "ollama_vram": 0}

        report = b.run_bench(self.args(), self.deps(sampler=sampler), self.gate)
        self.assertEqual(report["steps"][0]["verdict"], "FAIL")
        self.assertIn("swap grew", "; ".join(report["steps"][0]["reasons"]))


class Signals(Base):
    def test_sigint_during_teardown_stops_server_before_lock_release(self):
        events = []
        real_release = b.release_lock

        def release(fd):
            events.append(("release", [p.returncode for p in self.procs]))
            real_release(fd)

        def session(*a, **kw):
            signal.raise_signal(signal.SIGINT)  # Ctrl-C mid-round: only sets the flag
            return dict(ok=True, ttft_s=0.1, tokens=1, decode_tps=1.0)

        deps = self.deps(session=session)

        def popen(argv, env=None, **kw):
            p = Base.popen(self, argv, env=env, **kw)
            p.on_signal = lambda: (signal.raise_signal(signal.SIGINT), events.append(("server-stop", p.pid)))
            return p

        deps.popen = popen
        old = signal.getsignal(signal.SIGINT)
        with unittest.mock.patch.object(b, "release_lock", release), \
                unittest.mock.patch("sys.stderr", io.StringIO()), redirect_stdout(io.StringIO()):
            rc = b.main(["--s1-url", self.prod_url, "--production-url", self.prod_url, "--steps", "1,2",
                         "--rounds", "2", "--s1-interval", "0.01", "--port", str(self.prod.server_address[1]),
                         "--out", os.path.join(self.home, "o.json")], deps=deps, gate=self.gate)
        self.assertIs(signal.getsignal(signal.SIGINT), old)
        self.assertEqual(rc, 130)
        self.assertEqual([e[0] for e in events], ["server-stop", "release"])
        self.assertTrue(all(code is not None for code in events[-1][1]))  # every server reaped before the release
        self.assertEqual(len(self.procs), 1)  # step 2 never started
        self.assertFalse(os.path.exists(os.path.join(self.home, "o.json")))

    def test_sigterm_exit_code(self):
        stop = sc.StopRequest()
        stop.handle(signal.SIGTERM, None)
        self.assertEqual(stop.exit_code(), 143)
        with self.assertRaises(b.Stopped):
            b.run_bench(self.args(), self.deps(stop=stop), self.gate)
        self.assertEqual(self.procs, [])


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
