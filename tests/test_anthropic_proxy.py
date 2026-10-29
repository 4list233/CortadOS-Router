"""Unit tests for the anthropic-api lane executor (cortados_router/provider/anthropic_proxy.py). No live API: the
upstream is a fake opener. serve() does no accounting now — it returns the captured usage for the caller to settle."""
import io
import json
import unittest
import urllib.error
from unittest import mock

from cortados_router.provider import anthropic_proxy as ap


class FakeResp:
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
    def __init__(self, fail_write_after=None):
        self.status = None
        self.headers = {}
        self.wfile = _Wfile(fail_write_after)
        self.close_connection = False
    def send_response(self, code):
        self.status = code
    def send_header(self, k, v):
        self.headers[k] = v
    def end_headers(self):
        pass


class _Wfile:
    def __init__(self, fail_after=None):
        self.buf = io.BytesIO()
        self._fail_after = fail_after
        self._writes = 0
    def write(self, b):
        self._writes += 1
        if self._fail_after is not None and self._writes > self._fail_after:
            raise OSError("client gone")
        self.buf.write(b)
    def flush(self):
        pass


class TestOrigin(unittest.TestCase):
    def test_build_upstream_rejects_non_anthropic_or_insecure(self):
        for bad in ("http://api.anthropic.com", "https://evil.example.com", "https://api.anthropic.com.evil.com"):
            with self.assertRaises(ValueError):
                ap.build_upstream({"model": "x"}, "claude-opus-5-5", "sk", base=bad)

    def test_build_upstream_accepts_the_anthropic_origin(self):
        url, data, headers = ap.build_upstream({"model": "cortados", "betas": ["b1"]}, "claude-opus-5-5", "sk",
                                               base="https://api.anthropic.com")
        self.assertTrue(url.startswith("https://api.anthropic.com/v1/messages"))
        self.assertEqual(json.loads(data)["model"], "claude-opus-5-5")
        self.assertEqual(headers["x-api-key"], "sk")
        self.assertEqual(headers["anthropic-beta"], "b1")

    def test_opener_refuses_redirects(self):
        # the redirect handler returns None, so urllib does not re-send the credentialed request to a new host
        self.assertIsNone(ap._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example.com"))


class TestServe(unittest.TestCase):
    def test_nonstream_returns_completed_usage_and_relays_body(self):
        msg = {"id": "m1", "type": "message", "usage": {"input_tokens": 1000, "output_tokens": 7}}
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=json.dumps(msg).encode()))
        self.assertEqual(h.status, 200)
        self.assertEqual(json.loads(h.wfile.buf.getvalue())["id"], "m1")
        self.assertEqual(usage["output_tokens"], 7)
        self.assertEqual(outcome, "completed")

    def test_stream_completed_when_message_stop_seen(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(usage.get("output_tokens"), 5)
        self.assertEqual(outcome, "completed")

    def test_stream_message_stop_substring_in_text_is_not_completion(self):
        # a text delta that merely CONTAINS the string "message_stop", then the upstream ends with no terminal
        # event -> must stay "unknown" (the old substring check wrongly settled this as completed)
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"the event is message_stop"}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(usage.get("output_tokens"), 5)
        self.assertEqual(outcome, "unknown")   # no parsed message_stop event -> conservative

    def test_stream_disconnect_is_unknown_with_captured_usage(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
        ]
        # the write fails after the FIRST chunk; usage is captured but the turn did not terminate -> unknown
        h = FakeHandler(fail_write_after=1)
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(usage.get("input_tokens"), 1000)
        self.assertEqual(outcome, "unknown")

    def test_stream_completed_survives_a_late_client_write_failure(self):
        # writes 1-2 succeed; the write of the message_stop chunk fails. message_stop was parsed first, so the full
        # turn was received and billed -> "completed", not downgraded to "unknown" by the client disconnect.
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        h = FakeHandler(fail_write_after=2)
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(outcome, "completed")
        self.assertEqual(usage.get("output_tokens"), 5)

    def test_nonstream_completed_survives_a_client_write_failure(self):
        msg = {"id": "m1", "type": "message", "usage": {"input_tokens": 1000, "output_tokens": 7}}
        h = FakeHandler(fail_write_after=0)   # the body write fails outright
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=json.dumps(msg).encode()))
        self.assertEqual(outcome, "completed")   # a full response was received and billed regardless
        self.assertEqual(usage.get("output_tokens"), 7)

    def test_http_error_stays_rejected_even_if_relay_write_fails(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {"content-type": "application/json"}, io.BytesIO(b'{"error":"x"}'))
        h = FakeHandler(fail_write_after=0)
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=mock.Mock(side_effect=err))
        self.assertEqual((usage, outcome), ({}, "rejected"))   # the API rejected it; a failed relay does not bill it

    def test_http_error_is_relayed_and_rejected(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {"content-type": "application/json"}, io.BytesIO(b'{"error":"x"}'))
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=mock.Mock(side_effect=err))
        self.assertEqual(h.status, 400)
        self.assertEqual((usage, outcome), ({}, "rejected"))   # the API rejected it -> nothing to settle


if __name__ == "__main__":
    unittest.main()
