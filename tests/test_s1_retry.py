"""decide_s1 retries once on a transient connection error before declaring the scorer degraded. The two live degraded
causes seen in production were a connection refused (S1 not yet listening) and a RemoteDisconnected (S1 momentarily
overloaded and dropping the connection); a single quick retry rides both out (Forest 2026-10-26)."""
import http.client
import json
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
        res, n = self._run([urllib.error.URLError("[Errno 61] Connection refused"), GOOD])
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

    def test_retryable_http_error_is_retried_once(self):
        # a 503 is a transient server state: retry once, then degrade
        calls = {"n": 0}
        def fake(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        core.urllib.request.urlopen = fake
        res = core.decide_s1("do a thing")
        self.assertTrue(res["degraded"])
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
