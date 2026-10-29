"""Unit tests for the anthropic-api lane executor (cortados_router/provider/anthropic_proxy.py). No live API: the
upstream is a fake opener. serve() does no accounting now — it returns the captured usage for the caller to settle."""
import http.client
import io
import json
import unittest
import urllib.error
from unittest import mock

from cortados_router.provider import anthropic_proxy as ap


class FakeResp:
    def __init__(self, *, body=b"", chunks=None, read_exc=None, raise_after_chunks=None, close_exc=None):
        self._body, self._chunks = body, chunks
        self._read_exc, self._raise_after_chunks, self._close_exc = read_exc, raise_after_chunks, close_exc
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        if self._read_exc is not None:
            raise self._read_exc
        return self._body
    def __iter__(self):
        for c in (self._chunks or []):
            yield c
        if self._raise_after_chunks is not None:
            raise self._raise_after_chunks   # upstream read fault AFTER the chunks were delivered
    def close(self):
        if self._close_exc is not None:
            raise self._close_exc


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

    def test_stream_upstream_read_fault_after_message_stop_stays_completed(self):
        # all SSE events (incl. message_stop) arrive, THEN the upstream read raises -> the parsed terminal state is
        # kept, so the turn settles as completed with its real usage, not the conservative full reservation.
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks, raise_after_chunks=ConnectionResetError("reset")))
        self.assertEqual(outcome, "completed")
        self.assertEqual(usage.get("output_tokens"), 5)

    def test_stream_upstream_read_fault_before_completion_is_unknown(self):
        # usage arrives but no terminal event; an IncompleteRead then ends the stream -> unknown, usage preserved
        chunks = [b'data: {"type":"message_start","message":{"usage":{"input_tokens":100000}}}\n\n']
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks, raise_after_chunks=http.client.IncompleteRead(b"")))
        self.assertEqual(outcome, "unknown")
        self.assertEqual(usage.get("input_tokens"), 100000)

    def test_nonstream_malformed_body_is_unknown_not_completed_zero(self):
        # a truncated 200 body cannot be parsed -> the turn's real cost is unreadable, so keep it "unknown"
        # (conservative full reservation) rather than settling completed-for-zero.
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=b'{"type":"message","usage":{"input_tokens":1000'))
        self.assertEqual((usage, outcome), ({}, "unknown"))

    def test_nonstream_message_without_usage_is_unknown(self):
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=json.dumps({"type": "message", "id": "m"}).encode()))
        self.assertEqual((usage, outcome), ({}, "unknown"))

    def test_nonstream_upstream_read_fault_is_unknown(self):
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(read_exc=http.client.IncompleteRead(b"")))
        self.assertEqual((usage, outcome), ({}, "unknown"))

    def test_http_error_body_read_fault_stays_rejected(self):
        # the error body declares more bytes than it supplies; reading it raises, but the turn was still REJECTED
        err = urllib.error.HTTPError("u", 400, "Bad", {"content-type": "application/json"},
                                     io.BufferedReader(io.BytesIO(b'{"error"')))
        err.read = mock.Mock(side_effect=http.client.IncompleteRead(b'{"error"'))
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=mock.Mock(side_effect=err))
        self.assertEqual((usage, outcome), ({}, "rejected"))

    def test_response_close_fault_does_not_mask_the_outcome(self):
        msg = {"id": "m1", "type": "message", "usage": {"input_tokens": 1000, "output_tokens": 7}}
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=json.dumps(msg).encode(), close_exc=OSError("close")))
        self.assertEqual(outcome, "completed")
        self.assertEqual(usage.get("output_tokens"), 7)

    def test_invalid_outbound_header_is_rejected_before_dispatch(self):
        # a client-supplied beta with CRLF is caught by pre-validation; nothing is dispatched and the injected value
        # is not echoed back to the client
        called = []
        def must_not_dispatch(req, timeout=0):
            called.append(1)
            raise AssertionError("must not dispatch")
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "betas": ["bad\r\nx-injected: true"]}, "claude-opus-5-5", 0.0,
                                  key="sk", urlopen=must_not_dispatch)
        self.assertEqual((usage, outcome), ({}, "rejected"))
        self.assertEqual(h.status, 400)
        self.assertEqual(called, [])                                   # never sent
        self.assertNotIn(b"x-injected", h.wfile.buf.getvalue())        # injected value not reflected

    def test_malformed_credit_key_is_not_echoed_to_the_client(self):
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0,
                                  key="sk-ant-SECRET\r\nwrapped", urlopen=lambda req, timeout=0: None)
        self.assertEqual(outcome, "rejected")
        body = h.wfile.buf.getvalue()
        self.assertNotIn(b"SECRET", body)                              # the credit key value is never disclosed
        self.assertIn(b"x-api-key", body)                             # only the header NAME is reported

    def test_post_dispatch_opener_error_is_unknown_not_rejected(self):
        # a failure raised by the opener AFTER headers validated (e.g. response-header parse) -> dispatch uncertain,
        # so "unknown" (conservative full reservation), never a $0 rejection
        def opener_raises(req, timeout=0):
            raise UnicodeError("bad response header")   # a ValueError subclass, as the stdlib raises
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk", urlopen=opener_raises)
        self.assertEqual((usage, outcome), ({}, "unknown"))

    def test_stream_null_usage_is_unknown_not_completed_zero(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":null,"output_tokens":null}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":null}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(outcome, "unknown")   # parsed message_stop but no usable usage -> conservative

    def test_nonstream_null_usage_counts_is_unknown(self):
        body = json.dumps({"type": "message", "usage": {"input_tokens": None, "output_tokens": None}}).encode()
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=body))
        self.assertEqual((usage, outcome), ({}, "unknown"))   # non-empty usage but no usable counts -> conservative

    def test_nonstream_zero_usage_counts_is_completed(self):
        body = json.dumps({"type": "message", "usage": {"input_tokens": 0, "output_tokens": 0}}).encode()
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=body))
        self.assertEqual(outcome, "completed")   # legitimate zero counts are usable

    def test_stream_malformed_event_after_message_stop_keeps_completed(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":1000}}}\n\n',
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            b'data: []\n\n',   # a well-formed JSON non-object must not raise and discard the known completion
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(outcome, "completed")
        self.assertEqual(usage.get("output_tokens"), 5)

    def test_has_usable_usage(self):
        self.assertTrue(ap.has_usable_usage({"input_tokens": 0, "output_tokens": 0}))               # legit zeros
        self.assertTrue(ap.has_usable_usage({"input_tokens": 10, "output_tokens": 7, "cache_read_input_tokens": 3}))
        self.assertFalse(ap.has_usable_usage({"input_tokens": 10}))                                 # output missing
        self.assertFalse(ap.has_usable_usage({"input_tokens": None, "output_tokens": 0}))           # input null, cost unknown
        self.assertFalse(ap.has_usable_usage({"input_tokens": -1, "output_tokens": -1}))            # negative
        self.assertFalse(ap.has_usable_usage({"input_tokens": 10, "output_tokens": 7, "cache_read_input_tokens": None}))  # bad optional
        self.assertFalse(ap.has_usable_usage({"input_tokens": True, "output_tokens": 1}))           # bool is not a count
        self.assertFalse(ap.has_usable_usage({"speed": "standard"}))
        self.assertFalse(ap.has_usable_usage({}))
        self.assertFalse(ap.has_usable_usage(None))

    def test_nonstream_partial_usage_is_unknown(self):
        body = json.dumps({"type": "message", "usage": {"input_tokens": None, "output_tokens": 0}}).encode()
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(body=body))
        self.assertEqual((usage, outcome), ({}, "unknown"))   # input cost unknown -> not a completed $0

    def test_stream_partial_usage_is_unknown(self):
        chunks = [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":null,"output_tokens":0}}}\n\n',
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados", "stream": True}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=lambda req, timeout=0: FakeResp(chunks=chunks))
        self.assertEqual(outcome, "unknown")

    def test_prebuild_origin_error_is_a_zero_charge_rejection(self):
        # a misconfigured upstream makes build_upstream raise BEFORE dispatch -> rejected (settle 0), never "unknown"
        h = FakeHandler()
        with mock.patch("cortados_router.provider.anthropic_proxy.build_upstream",
                        side_effect=ValueError("anthropic-api lane refuses a non-Anthropic/insecure upstream")):
            usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                      urlopen=lambda req, timeout=0: FakeResp(body=b"{}"))
        self.assertEqual((usage, outcome), ({}, "rejected"))
        self.assertEqual(h.status, 400)

    def test_http_error_is_relayed_and_rejected(self):
        err = urllib.error.HTTPError("u", 400, "Bad", {"content-type": "application/json"}, io.BytesIO(b'{"error":"x"}'))
        h = FakeHandler()
        usage, outcome = ap.serve(h, {"model": "cortados"}, "claude-opus-5-5", 0.0, key="sk",
                                  urlopen=mock.Mock(side_effect=err))
        self.assertEqual(h.status, 400)
        self.assertEqual((usage, outcome), ({}, "rejected"))   # the API rejected it -> nothing to settle


if __name__ == "__main__":
    unittest.main()
