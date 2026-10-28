"""Unit tests for the anthropic-api lane executor (cortados_router/provider/anthropic_proxy.py). No live API and no
real ~/.cortados state: the upstream is a fake urlopen and the meter is redirected to a temp CORTADOS_HOME."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from cortados_router import api_credits
from cortados_router.provider import anthropic_proxy as ap


class FakeResp:
    """Context-manager stand-in for a urlopen response: read() for non-stream, iteration for stream."""
    def __init__(self, *, body=b"", chunks=None):
        self._body, self._chunks = body, chunks
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._body
    def __iter__(self):
        return iter(self._chunks or [])


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()
        self.close_connection = False
        self.ended = False
    def send_response(self, code):
        self.status = code
    def send_header(self, k, v):
        self.headers[k] = v
    def end_headers(self):
        self.ended = True


class TestPureHelpers(unittest.TestCase):
    def test_build_upstream_swaps_model_and_sets_headers(self):
        url, data, headers = ap.build_upstream({"model": "cortados", "max_tokens": 10, "betas": ["x-1", "y-2"]},
                                               "claude-opus-5-5", "sk-test")
        self.assertTrue(url.endswith("/v1/messages"))
        sent = json.loads(data)
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertEqual(sent["max_tokens"], 10)
        self.assertEqual(headers["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", headers)
        self.assertEqual(headers["anthropic-beta"], "x-1,y-2")

    def test_extract_usage(self):
        self.assertEqual(ap.extract_usage({"usage": {"input_tokens": 3}}), {"input_tokens": 3})
        self.assertIsNone(ap.extract_usage("not a dict"))

    def test_usage_from_sse_accumulates(self):
        sse = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"usage":{"input_tokens":100,"cache_read_input_tokens":20}}}\n\n'
            'event: message_delta\n'
            'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
            'data: [DONE]\n\n'
        )
        u = ap.usage_from_sse(sse)
        self.assertEqual(u["input_tokens"], 100)
        self.assertEqual(u["cache_read_input_tokens"], 20)
        self.assertEqual(u["output_tokens"], 7)


class TestServe(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_API_CREDIT_BUDGET_USD": "200"})
        self._env.start()
        self.now = 1793000000.0
    def tearDown(self):
        self._env.stop()

    def test_nonstream_relays_body_and_meters(self):
        msg = {"id": "m1", "type": "message", "role": "assistant", "content": [{"type": "text", "text": "hi"}],
               "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}  # opus-5-5 input = $4
        handler = FakeHandler()
        ap.serve(handler, {"model": "cortados", "stream": False}, "claude-opus-5-5", self.now,
                 key="sk-x", urlopen=lambda req, timeout=0: FakeResp(body=json.dumps(msg).encode()))
        self.assertEqual(handler.status, 200)
        self.assertEqual(json.loads(handler.wfile.getvalue())["id"], "m1")
        self.assertAlmostEqual(api_credits.spent(self.now, api_credits.meter_path()), 4.0, places=6)

    def test_stream_relays_chunks_and_meters(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":0}}\n\n',
            b'data: [DONE]\n\n',
        ]
        handler = FakeHandler()
        ap.serve(handler, {"model": "cortados", "stream": True}, "claude-opus-5-5", self.now,
                 key="sk-x", urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.headers["content-type"], "text/event-stream")
        self.assertIn(b"message_start", handler.wfile.getvalue())
        self.assertAlmostEqual(api_credits.spent(self.now, api_credits.meter_path()), 4.0, places=6)  # 1M input @ $4

    def test_http_error_is_relayed(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {"content-type": "application/json"}, io.BytesIO(b'{"error":"nope"}'))
        handler = FakeHandler()
        ap.serve(handler, {"model": "cortados"}, "claude-opus-5-5", self.now,
                 key="sk-x", urlopen=mock.Mock(side_effect=err))
        self.assertEqual(handler.status, 400)
        self.assertIn(b"nope", handler.wfile.getvalue())
        self.assertEqual(api_credits.spent(self.now, api_credits.meter_path()), 0.0)  # a failed call costs nothing


if __name__ == "__main__":
    unittest.main()
