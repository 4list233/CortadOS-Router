"""The S1 server's guards that matter once CORTADOS_S1_BIND exposes the port: token, a sane Content-Length, body-size
cap (admission); the atomic backlog cap (enqueue on a bounded queue); the caller-timeout propagation (client_wait);
and dropping a job whose deadline passed (_expired). Tested through pure helpers: no socket, worker or MLX."""
import queue
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
sys.path.insert(0, str(REPO))
import serve  # noqa: E402


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self._saved = (serve.TOKEN, serve.MAX_BODY_BYTES)
        serve.TOKEN, serve.MAX_BODY_BYTES = None, 1000

    def tearDown(self):
        serve.TOKEN, serve.MAX_BODY_BYTES = self._saved

    def test_admitted_within_limits(self):
        self.assertIsNone(serve.admission("", 100))

    def test_negative_length_is_400(self):
        self.assertEqual(serve.admission("", -1)[0], 400)     # would otherwise read until EOF, unbounded

    def test_body_over_cap_is_413(self):
        serve.MAX_BODY_BYTES = 50
        self.assertEqual(serve.admission("", 51)[0], 413)

    def test_token_required_when_set(self):
        serve.TOKEN = "s3cret"
        self.assertEqual(serve.admission("", 10)[0], 401)
        self.assertEqual(serve.admission("Bearer wrong", 10)[0], 401)
        self.assertIsNone(serve.admission("Bearer s3cret", 10))

    def test_token_checked_before_length(self):
        serve.TOKEN, serve.MAX_BODY_BYTES = "s3cret", 50
        self.assertEqual(serve.admission("", -5)[0], 401)     # unauthorized wins over a bad length


class EnqueueBacklogTests(unittest.TestCase):
    def setUp(self):
        self._jobs = serve._jobs

    def tearDown(self):
        serve._jobs = self._jobs

    def test_full_bounded_queue_sheds_with_503(self):
        serve._jobs = queue.Queue(maxsize=1)
        reply, full = serve.enqueue({"request": "a"}, serve.time.time() + 10)
        self.assertIsNotNone(reply); self.assertIsNone(full)   # first fits
        reply2, full2 = serve.enqueue({"request": "b"}, serve.time.time() + 10)
        self.assertIsNone(reply2); self.assertEqual(full2[0], 503)   # second overshoots the cap, atomically


class ClientWaitTests(unittest.TestCase):
    def test_caller_timeout_used_and_capped(self):
        self.assertEqual(serve.client_wait(None), serve.REPLY_TIMEOUT_S)
        self.assertEqual(serve.client_wait("15"), 15.0)
        self.assertEqual(serve.client_wait(str(serve.REPLY_TIMEOUT_S + 100)), serve.REPLY_TIMEOUT_S)  # capped
        self.assertEqual(serve.client_wait("garbage"), serve.REPLY_TIMEOUT_S)
        self.assertEqual(serve.client_wait("-3"), 0.0)


class ExpiryTests(unittest.TestCase):
    def test_expired_detects_passed_deadline(self):
        self.assertTrue(serve._expired(serve.time.time() - 1))
        self.assertFalse(serve._expired(serve.time.time() + 100))
        self.assertFalse(serve._expired(None))   # the warmup job carries no deadline


if __name__ == "__main__":
    unittest.main()
