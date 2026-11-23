"""S1 serve: admission (full queue only), dropping at dequeue work that cannot finish before the caller's deadline, and the per-request metadata log line.
Pure helpers and run_job with fake load/score functions: no MLX, no socket (except the end-to-end handler test, which
binds an ephemeral loopback port and uses a fake scorer)."""
import http.client
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
sys.path.insert(0, str(REPO))
import serve  # noqa: E402

SECRET = "PLEASE-DO-NOT-LOG delete prod bucket s3://acme-secret"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class ServiceTimesTests(unittest.TestCase):
    def test_floor_without_samples_and_moving_mean(self):
        st = serve.ServiceTimes(window=3, floor=0.5)
        self.assertEqual(st.expected(), 0.5)
        for x in (4.0, 4.0, 1.0):
            st.add(x)
        self.assertAlmostEqual(st.expected(), 3.0)
        st.add(1.0); st.add(1.0)                          # the window forgets the old slow samples
        self.assertAlmostEqual(st.expected(), 1.0)
        for _ in range(3):
            st.add(0.01)
        self.assertEqual(st.expected(), 0.5)              # never below the floor

    def test_a_slow_sample_ages_out(self):
        # review round 1, finding 1: one 16 s pass under contention must not set the estimate for good
        clock = Clock()
        st = serve.ServiceTimes(window=20, floor=0.3, max_age=300, clock=clock)
        st.add(16.0)
        self.assertAlmostEqual(st.expected(), 16.0)
        clock.t += 299
        self.assertAlmostEqual(st.expected(), 16.0)
        clock.t += 2
        self.assertEqual(st.expected(), 0.3)              # aged out: back to the floor with no new sample

    def test_reset_forgets_every_sample(self):
        st = serve.ServiceTimes(window=5, floor=0.2)
        st.add(30.0)
        st.reset()
        self.assertEqual(st.expected(), 0.2)


class ShouldDropTests(unittest.TestCase):
    def test_rules(self):
        now = 1000.0
        self.assertTrue(serve.should_drop(now + 3.0, 4.0, 1, now))        # others wait and it cannot finish: drop
        self.assertFalse(serve.should_drop(now + 5.0, 4.0, 1, now))       # it can finish
        self.assertFalse(serve.should_drop(now + 3.0, 4.0, 0, now))       # nothing waits: the recovery probe runs it
        self.assertTrue(serve.should_drop(now - 0.1, 0.1, 0, now))        # the caller is already gone
        self.assertFalse(serve.should_drop(None, 99.0, 9, now))           # the warm-up job has no deadline


class _ShedBase(unittest.TestCase):
    def setUp(self):
        self._saved = (serve._jobs, serve._service, dict(serve._state))
        serve._jobs = queue.Queue(maxsize=8)
        serve._service = serve.ServiceTimes(window=5, floor=0.1)

    def tearDown(self):
        serve._jobs, serve._service = self._saved[0], self._saved[1]
        serve._state.clear(); serve._state.update(self._saved[2])


class EnqueueShedTests(_ShedBase):
    """Review round 1, finding 2: no predictive shedding at admission; only an actually full queue is refused."""

    def test_only_a_full_queue_is_refused(self):
        serve._service.add(4.0)
        now = time.time()
        for _ in range(8):
            reply, refusal = serve.enqueue({"request": "a"}, now + 100, now=now)
            self.assertIsNone(refusal)
        reply, refusal = serve.enqueue({"request": "b"}, now + 100, now=now)
        self.assertIsNone(reply)
        self.assertEqual(refusal[0], 503)
        self.assertTrue(refusal[1]["busy"])

    def test_an_expired_backlog_does_not_cause_a_503(self):
        serve._service.add(4.0)
        now = time.time()
        for _ in range(5):                                # five queued jobs whose callers are long gone
            serve.enqueue({"request": "old"}, now - 30, now=now - 45)
        reply, refusal = serve.enqueue({"request": "new"}, now + 15, now=now)   # old rule: 6 x 4 s = 24 s > 15 s
        self.assertIsNone(refusal)
        scored = []
        while not serve._jobs.empty():                    # the worker drops the expired ones and scores the new one
            serve.run_job(serve._jobs.get_nowait(), load=lambda: None,
                          score=lambda b: scored.append(b["request"]) or {"answers": {}})
        self.assertEqual(scored, ["new"])
        self.assertEqual(reply.get_nowait()["answers"], {})

    def test_a_nearly_complete_active_job_does_not_cause_a_503(self):
        serve._service.add(10.0)
        serve._service.in_service = 1                     # a job in service, about to finish
        now = time.time()
        reply, refusal = serve.enqueue({"request": "a"}, now + 15, now=now)   # old rule: 2 x 10 s = 20 s > 15 s
        self.assertIsNone(refusal)
        self.assertIsNotNone(reply)


class RunJobDeadlineTests(_ShedBase):
    def _job(self, deadline, enqueued=None):
        reply = queue.Queue()
        return (({"request": SECRET}, reply, deadline, time.time() if enqueued is None else enqueued)), reply

    def test_a_job_that_cannot_finish_is_dropped_before_the_pass_when_others_wait(self):
        serve._service.add(4.0)
        scored = []
        job, reply = self._job(time.time() + 2.0)         # not expired yet, but 2 s < 4 s expected, one waiting behind
        serve.run_job(job, load=lambda: None, score=lambda b: scored.append(b) or {"answers": {}}, waiting=1)
        r = reply.get_nowait()
        self.assertEqual(scored, [])                      # no MLX pass spent
        self.assertTrue(r["busy"]); self.assertTrue(r["dropped"])
        self.assertIsNone(r["_meta"]["service_s"])

    def test_an_expired_job_is_dropped_even_with_an_empty_queue(self):
        job, reply = self._job(time.time() - 1.0)
        scored = []
        serve.run_job(job, load=lambda: None, score=lambda b: scored.append(b) or {}, waiting=0)
        self.assertEqual(scored, [])
        self.assertTrue(reply.get_nowait()["dropped"])

    def test_scoring_recovers_after_a_slow_sample(self):
        # review round 1, finding 1: the first pass took 16 s under contention; later default 15-s requests on an empty
        # queue still run (probe), and their service times bring the estimate back down
        serve._service.add(16.0)
        for _ in range(serve._service._times.maxlen):
            job, reply = self._job(time.time() + 15.0)
            serve.run_job(job, load=lambda: None, score=lambda b: {"answers": {"steps": 1}}, waiting=0)
            self.assertEqual(reply.get_nowait()["answers"], {"steps": 1})
        self.assertLess(serve._service.expected(), 1.0)   # the slow sample has left the window
        job, reply = self._job(time.time() + 15.0)        # and now even a job with others waiting is scored
        serve.run_job(job, load=lambda: None, score=lambda b: {"answers": {"steps": 2}}, waiting=3)
        self.assertEqual(reply.get_nowait()["answers"], {"steps": 2})

    def test_a_job_with_time_left_is_scored_and_its_service_time_recorded(self):
        job, reply = self._job(time.time() + 30.0, enqueued=time.time() - 0.5)
        serve.run_job(job, load=lambda: None, score=lambda b: (time.sleep(0.02), {"answers": {"steps": 1}})[1])
        r = reply.get_nowait()
        self.assertEqual(r["answers"], {"steps": 1})
        self.assertGreaterEqual(r["_meta"]["queue_wait_s"], 0.5)
        self.assertGreaterEqual(r["_meta"]["service_s"], 0.02)
        self.assertGreater(r["_meta"]["peak_rss_bytes"], 0)
        self.assertGreaterEqual(serve._service.expected(), 0.1)
        self.assertEqual(serve._service.in_service, 0)

    def test_a_slow_load_rechecks_the_deadline(self):
        serve._service.add(1.0)
        job, reply = self._job(time.time() + 1.5)
        scored = []
        serve.run_job(job, load=lambda: time.sleep(0.6), score=lambda b: scored.append(b) or {}, waiting=1)
        self.assertEqual(scored, [])
        self.assertTrue(reply.get_nowait()["dropped"])

    def test_the_warm_up_job_is_never_dropped(self):
        serve._service.add(100.0)
        job, reply = self._job(None)
        serve.run_job(job, load=lambda: None, score=lambda b: {"answers": {"x": 0}}, waiting=5)
        self.assertEqual(reply.get_nowait()["answers"], {"x": 0})


class ReloadResetTests(_ShedBase):
    """Review round 1, finding 1: a promotion (current.json changes) loads a new model and forgets the old estimate."""

    def test_a_promotion_resets_the_service_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            cur = Path(d) / "current.json"
            cur.write_text(json.dumps({"base": "b", "adapter": str(Path(d) / "run1" / "adapters"), "promoted": "x"}))
            saved = (serve.CURRENT, serve.Scorer)
            serve.CURRENT = cur
            serve.Scorer = lambda *a, **k: object()
            serve._state.update(scorer=None, mtime=0.0)
            try:
                serve._service.add(40.0)
                serve.ensure_loaded()
                self.assertEqual(serve._service.expected(), 0.1)
                serve._service.add(40.0)
                serve.ensure_loaded()                     # same model: the estimate stays
                self.assertEqual(serve._service.expected(), 40.0)
                os.utime(cur, (time.time() + 5, time.time() + 5))   # a promotion rewrites current.json
                serve.ensure_loaded()
                self.assertEqual(serve._service.expected(), 0.1)
            finally:
                serve.CURRENT, serve.Scorer = saved


class LogLineTests(unittest.TestCase):
    def test_format(self):
        line = serve.request_log_line(outcome="ok", code=200, queue_wait_s=1.2345, service_s=0.9, depth=3,
                                      body_bytes=812, prompt_tokens=640, prefix_tokens=520, expected_s=1.0,
                                      prefix_slots=1, prefix_bytes=75 * 2**20, peak_rss_bytes=5 * 2**30)
        self.assertRegex(line, r"^s1-request ts=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ outcome=ok code=200 queue_wait_ms=1234 "
                               r"service_ms=900 depth=3 bytes=812 tokens=640 prefix_tokens=520 expected_ms=1000 "
                               r"prefix_slots=1 prefix_mb=75 peak_rss_mb=5120$")

    def test_missing_fields_print_as_dash_and_unknown_outcomes_are_error(self):
        line = serve.request_log_line(outcome="timeout", code=504, queue_wait_s=None, service_s=None, depth=0,
                                      body_bytes=10)
        self.assertIn("outcome=timeout", line)
        self.assertIn("service_ms=- ", line)
        self.assertIn("tokens=- prefix_tokens=-", line)
        self.assertIn("outcome=error", serve.request_log_line(outcome=SECRET, code=200, queue_wait_s=None,
                                                              service_s=None, depth=0, body_bytes=0))

    def test_never_contains_text(self):
        # a field smuggling text fails to format as a number; the log never prints it
        with self.assertRaises((ValueError, TypeError)):
            serve.request_log_line(outcome="ok", code=200, queue_wait_s=None, service_s=None, depth=SECRET,
                                   body_bytes=0)
        captured = []

        class Cap(logging.Handler):
            def emit(self, rec):
                captured.append(rec.getMessage())
        h = Cap()
        serve._request_log.addHandler(h)
        try:
            serve.log_request(outcome="ok", code=200, queue_wait_s=None, service_s=None, depth=SECRET, body_bytes=0)
        finally:
            serve._request_log.removeHandler(h)
        self.assertEqual(captured, [])                    # dropped, never logged with the text

    def test_setup_writes_a_rotating_bounded_file(self):
        with tempfile.TemporaryDirectory() as d:
            saved = (serve.REQUEST_LOG_BYTES, serve.REQUEST_LOG_BACKUPS, list(serve._request_log.handlers))
            serve.REQUEST_LOG_BYTES, serve.REQUEST_LOG_BACKUPS = 400, 2
            try:
                where = serve.setup_request_log(str(Path(d) / "s1-requests.log"))
                for _ in range(50):
                    serve.log_request(outcome="ok", code=200, queue_wait_s=0.1, service_s=0.2, depth=1, body_bytes=5)
            finally:
                for h in serve._request_log.handlers[len(saved[2]):]:
                    h.close()
                serve._request_log.handlers[:] = saved[2]
                serve.REQUEST_LOG_BYTES, serve.REQUEST_LOG_BACKUPS = saved[0], saved[1]
            files = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(where, str(Path(d) / "s1-requests.log"))
            self.assertEqual(files, ["s1-requests.log", "s1-requests.log.1", "s1-requests.log.2"])
            self.assertTrue(all((Path(d) / f).stat().st_size <= 400 for f in files))


class HandlerEndToEndTests(_ShedBase):
    """The real handler on a loopback ephemeral port with a fake scorer: one log line per request, no request text in
    it, the busy answer as 503, and _meta never sent to the client."""

    def setUp(self):
        super().setUp()
        self.lines = []
        lines = self.lines

        class Cap(logging.Handler):
            def emit(self, rec):
                lines.append(rec.getMessage())
        self.cap = Cap()
        serve._request_log.addHandler(self.cap)

        class FakeScorer:
            last_stats = {"prompt_tokens": 42, "prefix_tokens": 30, "prefix_slots": 1, "prefix_bytes": 2**20}

        serve._state.update(scorer=FakeScorer(), model="fake")
        self._saved_load, self._saved_score = serve.ensure_loaded, serve.score_request
        serve.ensure_loaded = lambda: None
        serve.score_request = lambda body, scorer, **k: {"answers": {"steps": 1}}
        self.srv = serve.BoundedServer(("127.0.0.1", 0), serve.H, max_conns=8)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.stop = threading.Event()

        def work():
            while not self.stop.is_set():
                try:
                    job = serve._jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                serve.run_job(job)
        self.w = threading.Thread(target=work, daemon=True); self.w.start()

    def tearDown(self):
        self.stop.set(); self.w.join(2)
        self.srv.shutdown(); self.srv.server_close()
        serve._request_log.removeHandler(self.cap)
        serve.ensure_loaded, serve.score_request = self._saved_load, self._saved_score
        super().tearDown()

    def _post(self, timeout_hdr="15"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", "/score", body=json.dumps({"request": SECRET}),
                  headers={"Content-Type": "application/json", "X-Request-Timeout-S": timeout_hdr})
        r = c.getresponse()
        out = (r.status, json.loads(r.read()))
        c.close()
        return out

    def test_ok_probe_under_a_stale_estimate_then_busy_on_a_full_queue(self):
        code, body = self._post()
        self.assertEqual(code, 200)
        self.assertNotIn("_meta", body)
        serve._service.add(50.0)                          # a stale estimate longer than the 15 s deadline ...
        code, body = self._post()
        self.assertEqual(code, 200)                       # ... no 503 with an empty queue: the job runs (probe)
        self.stop.set(); self.w.join(2)                   # worker stopped; fill the bounded queue
        for _ in range(serve._jobs.maxsize):
            serve._jobs.put_nowait(({}, queue.Queue(), None, time.time()))
        code, body = self._post()
        self.assertEqual(code, 503)
        self.assertTrue(body["busy"])
        time.sleep(0.1)
        self.assertEqual(len(self.lines), 3)
        self.assertRegex(self.lines[0], r"outcome=ok code=200 .* tokens=42 prefix_tokens=30 .*prefix_slots=1 "
                                        r"prefix_mb=1 peak_rss_mb=\d+$")
        self.assertIn("outcome=ok code=200", self.lines[1])
        self.assertIn("outcome=busy code=503", self.lines[2])
        for line in self.lines:
            self.assertNotIn("PLEASE", line)
            self.assertNotIn("acme", line)

if __name__ == "__main__":
    unittest.main()
