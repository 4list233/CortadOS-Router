"""The S1 server's admission guards that matter once CORTADOS_S1_BIND exposes the port: token, body-size cap, backlog
cap. Tested through the pure `admission` function so no socket, worker or MLX is involved."""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
sys.path.insert(0, str(REPO))
import serve  # noqa: E402


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self._saved = (serve.TOKEN, serve.MAX_BODY_BYTES, serve.MAX_PENDING)
        serve.TOKEN, serve.MAX_BODY_BYTES, serve.MAX_PENDING = None, 1000, 8

    def tearDown(self):
        serve.TOKEN, serve.MAX_BODY_BYTES, serve.MAX_PENDING = self._saved

    def test_admitted_when_within_limits(self):
        self.assertIsNone(serve.admission("", 100, 0))

    def test_body_over_cap_is_413(self):
        serve.MAX_BODY_BYTES = 50
        self.assertEqual(serve.admission("", 51, 0)[0], 413)

    def test_full_backlog_is_503(self):
        serve.MAX_PENDING = 4
        self.assertEqual(serve.admission("", 10, 4)[0], 503)

    def test_token_required_when_set(self):
        serve.TOKEN = "s3cret"
        self.assertEqual(serve.admission("", 10, 0)[0], 401)
        self.assertEqual(serve.admission("Bearer wrong", 10, 0)[0], 401)
        self.assertIsNone(serve.admission("Bearer s3cret", 10, 0))

    def test_token_checked_before_size_or_backlog(self):
        serve.TOKEN, serve.MAX_BODY_BYTES, serve.MAX_PENDING = "s3cret", 50, 1
        self.assertEqual(serve.admission("", 9999, 99)[0], 401)   # unauthorized wins over 413/503


if __name__ == "__main__":
    unittest.main()
