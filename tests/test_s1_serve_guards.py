"""The S1 server's guards that matter once CORTADOS_S1_BIND exposes the port: token, a sane Content-Length, body-size
cap (admission); the atomic backlog cap (enqueue on a bounded queue); the caller-timeout propagation (client_wait);
and dropping a job whose deadline passed (_expired). Tested through pure helpers: no socket, worker or MLX."""
import queue
import socket
import sys
import threading
import time
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


class ConnectionCapTests(unittest.TestCase):
    """A capped number of live connections, so a slow-header flood cannot spawn unbounded handler threads. Tested on an
    ephemeral port without real client sockets: the admit/release pair is the gate process_request uses."""

    def test_admit_refuses_past_capacity_and_recovers_on_release(self):
        srv = serve.BoundedServer(("127.0.0.1", 0), serve.H, max_conns=2)
        try:
            self.assertTrue(srv._admit_connection())
            self.assertTrue(srv._admit_connection())
            self.assertFalse(srv._admit_connection())   # at capacity
            srv._release_connection()
            self.assertTrue(srv._admit_connection())    # a freed slot admits again
        finally:
            srv.server_close()

    def test_handler_arms_a_connection_timeout(self):
        # socketserver applies H.timeout to the connection at setup, so a stalled request line/headers is dropped
        self.assertEqual(serve.H.timeout, serve.REPLY_TIMEOUT_S)
        self.assertGreater(serve.H.timeout, 0)


class SlowHeaderTests(unittest.TestCase):
    """A slow-drip client (bytes under the inactivity timeout) must not hold a slot indefinitely: the absolute header
    deadline cuts it, and the freed slot serves a normal client even while the attacker keeps transmitting."""

    def setUp(self):
        self._saved = serve.HEADER_DEADLINE_S
        serve.HEADER_DEADLINE_S = 0.3                         # short, so the test is quick
        self.srv = serve.BoundedServer(("127.0.0.1", 0), serve.H, max_conns=1)   # one slot: the drip must free it
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        serve.HEADER_DEADLINE_S = self._saved

    def test_trickle_frees_the_slot_for_a_real_request(self):
        drip = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        drip.sendall(b"GET / HTTP/1.0\r\n")                   # start, then never finish the headers
        stop = threading.Event()

        def keep_dripping():
            while not stop.is_set():
                try:
                    drip.sendall(b"X-Pad: y\r\n")            # a header line every 40 ms: under any inactivity timeout
                except OSError:
                    return
                time.sleep(0.04)
        dripper = threading.Thread(target=keep_dripping, daemon=True); dripper.start()
        try:
            time.sleep(serve.HEADER_DEADLINE_S + 0.3)         # let the deadline cut the drip and free the one slot
            real = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            real.sendall(b"GET / HTTP/1.0\r\n\r\n")
            real.settimeout(3)
            resp = real.recv(4096)
            self.assertIn(b"200", resp.split(b"\r\n", 1)[0])  # the slot recovered while the attacker kept transmitting
            real.close()
        finally:
            stop.set(); drip.close()


if __name__ == "__main__":
    unittest.main()
