"""decide_s1 retries once only when S1 was not reached: connection refused (S1 not yet listening) or reset /
RemoteDisconnected (closed before any reply, e.g. its connection cap). Since 2026-11-22 (owner-approved) a timeout or any
HTTP reply, 503 busy included, degrades at once: a retry after a timeout queued a second copy behind the first, which S1
still scored."""
import http.client
import json
import socket
import unittest
import urllib.error

from cortados_router import core

GOOD = {"answers": {"steps": 1, "ambiguity": 1, "blast_radius": 0, "context_size": 0, "novelty": 1,
                    "parallelizability": 0, "outward": False, "work_kind": "code", "urgency": "now",
                    "needs_clarification": False, "parts": 0, "diversity": False}, "model": "s1-test"}


class _Resp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._b


class S1RetryTests(unittest.TestCase):
    def setUp(self):
        self._keep = (core.urllib.request.urlopen, core.S1_RETRIES, core.S1_RETRY_BACKOFF_S)
        core.S1_RETRIES, core.S1_RETRY_BACKOFF_S = 1, 0.0   # deterministic: one retry, no sleep

    def tearDown(self):
        core.urllib.request.urlopen, core.S1_RETRIES, core.S1_RETRY_BACKOFF_S = self._keep

    def _run(self, effects):
        calls = {"n": 0}
        def fake(req, timeout=None):
            i = calls["n"]; calls["n"] += 1
            eff = effects[min(i, len(effects) - 1)]
            if isinstance(eff, Exception):
                raise eff
            return _Resp(eff)
        core.urllib.request.urlopen = fake
        return core.decide_s1("do a thing"), calls["n"]

    def test_transient_then_success_is_not_degraded(self):
        res, n = self._run([urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), GOOD])  # urlopen wraps the OSError
        self.assertFalse(res["degraded"])
        self.assertEqual(n, 2)                                   # one retry was used

    def test_remote_disconnected_twice_degrades_after_the_retry(self):
        res, n = self._run([http.client.RemoteDisconnected("closed"), http.client.RemoteDisconnected("closed")])
        self.assertTrue(res["degraded"])
        self.assertEqual(n, 2)                                   # tried, retried once, then gave up

    def test_a_malformed_body_is_not_retried(self):
        # a non-transient error (bad JSON) degrades immediately without burning the retry
        def fake(req, timeout=None):
            fake.calls += 1
            class Bad(_Resp):
                def read(self): return b"not json"
            return Bad({})
        fake.calls = 0
        core.urllib.request.urlopen = fake
        res = core.decide_s1("do a thing")
        self.assertTrue(res["degraded"])
        self.assertEqual(fake.calls, 1)


    def test_permanent_http_error_is_not_retried(self):
        # a 4xx (auth / body too large) is permanent: degrade immediately, no duplicate request, no backoff wait
        calls = {"n": 0}
        def fake(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        core.urllib.request.urlopen = fake
        res = core.decide_s1("do a thing")
        self.assertTrue(res["degraded"])
        self.assertEqual(calls["n"], 1)

    def test_busy_503_degrades_at_once_without_a_retry(self):
        # S1's busy answer (queue full, or it cannot finish before the deadline): degraded now, no second request
        calls = {"n": 0}
        def fake(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        core.urllib.request.urlopen = fake
        res = core.decide_s1("do a thing")
        self.assertTrue(res["degraded"])
        self.assertIsNone(res["answers"])
        self.assertTrue(res["error"].startswith("S1 busy"))
        self.assertEqual(calls["n"], 1)

    def test_no_http_status_is_retried(self):
        for code in (429, 500, 502, 504):
            res, n = self._run([urllib.error.HTTPError("http://s1/score", code, "x", {}, None), GOOD])
            self.assertTrue(res["degraded"], code)
            self.assertEqual(n, 1, code)

    def test_no_retry_on_timeout(self):
        # every shape a read or connect timeout takes through urlopen: the request may be queued and scored on S1
        for exc in (socket.timeout("timed out"), TimeoutError("timed out"),
                    urllib.error.URLError(socket.timeout("timed out")), urllib.error.URLError(TimeoutError())):
            res, n = self._run([exc, GOOD])
            self.assertTrue(res["degraded"], repr(exc))
            self.assertEqual(n, 1, repr(exc))                    # no second request

    def test_retry_on_refused_and_reset(self):
        for exc in (ConnectionRefusedError(61, "Connection refused"), urllib.error.URLError(ConnectionRefusedError(61, "refused")),
                    ConnectionResetError(54, "reset"), urllib.error.URLError(ConnectionResetError(54, "reset")),
                    http.client.RemoteDisconnected("closed")):
            res, n = self._run([exc, GOOD])
            self.assertFalse(res["degraded"], repr(exc))
            self.assertEqual(n, 2, repr(exc))                    # reached on the retry

    def test_other_connection_errors_are_not_retried(self):
        for exc in (urllib.error.URLError("[Errno 8] nodename nor servname provided"), OSError(64, "Host is down"),
                    http.client.IncompleteRead(b"")):
            res, n = self._run([exc, GOOD])
            self.assertTrue(res["degraded"], repr(exc))
            self.assertEqual(n, 1, repr(exc))


if __name__ == "__main__":
    unittest.main()
