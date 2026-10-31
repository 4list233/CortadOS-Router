"""The stdio route_task MCP server: protocol handshake, tool listing, and the delegation call against a fake provider.
No network — route_task takes a urlopen stand-in."""
import io
import json
import unittest

from cortados_router import subagent_mcp as m


class FakeResp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def ok_provider(obj):
    def _open(req, timeout=None):
        return FakeResp(obj)
    return _open


class RouteTaskTests(unittest.TestCase):
    def test_returns_the_routed_models_text(self):
        prov = ok_provider({"type": "message", "model": "gpt-6.1-sol",
                            "content": [{"type": "text", "text": "the answer"}]})
        r = m.route_task("http://x:8432", "tok-test-0001", "do a thing", urlopen=prov)
        self.assertNotIn("isError", r)
        self.assertEqual(r["content"][0]["text"], "the answer")
        self.assertEqual(r["_meta"]["cortados/routed_model"], "gpt-6.1-sol")

    def test_posts_cortados_model_and_token(self):
        seen = {}

        def _open(req, timeout=None):
            seen["url"] = req.full_url
            seen["token"] = req.headers.get("X-api-key")
            seen["body"] = json.loads(req.data)
            return FakeResp({"content": [{"type": "text", "text": "ok"}]})
        m.route_task("http://host:8432/", "secret-test-0001", "task text", context="extra", urlopen=_open)
        self.assertEqual(seen["url"], "http://host:8432/v1/messages")
        self.assertEqual(seen["token"], "secret-test-0001")
        self.assertEqual(seen["body"]["model"], "cortados")
        self.assertIn("task text", seen["body"]["messages"][0]["content"][0]["text"])
        self.assertIn("extra", seen["body"]["messages"][0]["content"][0]["text"])

    def test_empty_task_is_an_error(self):
        self.assertTrue(m.route_task("http://x", "t-test-0001", "   ", urlopen=ok_provider({}))["isError"])

    def test_unconfigured_is_an_error(self):
        self.assertTrue(m.route_task("", "", "task", urlopen=ok_provider({}))["isError"])

    def test_provider_error_payload_surfaces(self):
        prov = ok_provider({"type": "error", "error": {"message": "S1 unavailable; no Codex lane"}})
        r = m.route_task("http://x", "t-test-0001", "task", urlopen=prov)
        self.assertTrue(r["isError"])
        self.assertIn("S1 unavailable", r["content"][0]["text"])

    def test_unreachable_provider_is_an_error(self):
        def _boom(req, timeout=None):
            raise OSError("connection refused")
        r = m.route_task("http://x", "t-test-0001", "task", urlopen=_boom)
        self.assertTrue(r["isError"])
        self.assertIn("could not reach", r["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    CFG = {"url": "http://x:8432", "token": "t-test-0001"}

    def test_initialize_lists_capabilities(self):
        r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, self.CFG)
        self.assertEqual(r["result"]["serverInfo"]["name"], "cortados-route")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_tools_list_has_route_task(self):
        r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.CFG)
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names, ["route_task"])
        self.assertIn("task", r["result"]["tools"][0]["inputSchema"]["required"])

    def test_notification_gets_no_reply(self):
        self.assertIsNone(m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.CFG))

    def test_unknown_method_is_a_jsonrpc_error(self):
        r = m.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, self.CFG)
        self.assertEqual(r["error"]["code"], -32601)

    def test_tools_call_unknown_tool(self):
        r = m.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}}, self.CFG)
        self.assertTrue(r["result"]["isError"])


if __name__ == "__main__":
    unittest.main()


import http.client
import threading
import time


class RobustnessTests(unittest.TestCase):
    CFG = {"url": "http://x:8432", "token": "t-test-0001"}

    def test_truncated_response_is_an_error_not_a_crash(self):
        class Trunc:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): raise http.client.IncompleteRead(b"partial")
        r = m.route_task("http://x", "t-test-0001", "task", urlopen=lambda req, timeout=None: Trunc())
        self.assertTrue(r["isError"])
        self.assertIn("could not reach", r["content"][0]["text"])

    def test_non_dict_message_is_dropped(self):
        self.assertIsNone(m.handle(123, self.CFG))
        self.assertIsNone(m.handle("oops", self.CFG))

    def test_string_arguments_do_not_crash(self):
        r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "route_task", "arguments": "oops"}}, self.CFG)
        self.assertTrue(r["result"]["isError"])          # empty task → error, but no AttributeError

    def test_non_string_method_is_invalid_request(self):
        r = m.handle({"jsonrpc": "2.0", "id": 2, "method": 5}, self.CFG)
        self.assertEqual(r["error"]["code"], -32600)

    def test_initialize_rejects_unsupported_version(self):
        r = m.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}, self.CFG)
        self.assertEqual(r["result"]["protocolVersion"], m.PROTOCOL)   # not echoed; we don't implement batching
        r2 = m.handle({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, self.CFG)
        self.assertEqual(r2["result"]["protocolVersion"], "2025-06-18")


class ResponsivenessTests(unittest.TestCase):
    def test_ping_is_answered_while_a_slow_call_is_blocked(self):
        gate = threading.Event()
        orig = m.route_task
        m.route_task = lambda *a, **k: (gate.wait(5), {"content": [{"type": "text", "text": "done"}]})[1]
        out, lock = [], threading.Lock()

        def write(reply):
            with lock:
                out.append(reply)
        try:
            m._dispatch({"jsonrpc": "2.0", "id": "call", "method": "tools/call",
                         "params": {"name": "route_task", "arguments": {"task": "slow"}}}, {"url": "u", "token": "t-test-0001"}, write)
            time.sleep(0.05)
            m._dispatch({"jsonrpc": "2.0", "id": "p", "method": "ping"}, {"url": "u", "token": "t-test-0001"}, write)
            deadline = time.time() + 2
            while not any(r["id"] == "p" for r in out) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(any(r["id"] == "p" for r in out))            # ping answered...
            self.assertFalse(any(r["id"] == "call" for r in out))        # ...while the call is still blocked
            gate.set()
            deadline = time.time() + 2
            while not any(r["id"] == "call" for r in out) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(any(r["id"] == "call" for r in out))         # call completes after unblocking
        finally:
            m.route_task = orig
            gate.set()


import os
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "cortados_router" / "subagent_mcp.py"


class TokenSafetyTests(unittest.TestCase):
    def test_error_text_never_contains_the_token(self):
        tok = "synthetic-review-secret"

        def _boom(req, timeout=None):
            raise ValueError(f"Invalid header value {tok!r}")       # mimics urllib's header-validation error
        r = m.route_task("http://x", tok, "task", urlopen=_boom)
        self.assertTrue(r["isError"])
        self.assertNotIn(tok, r["content"][0]["text"])
        self.assertIn("***", r["content"][0]["text"])

    def test_a_short_token_is_refused_not_sent(self):
        # too short to be a provider token: refused before any request, so it can neither leak nor mangle the scrub
        sent = []
        r = m.route_task("http://x", "abc123", "task", urlopen=lambda req, timeout=None: sent.append(req))
        self.assertTrue(r["isError"])
        self.assertIn("shorter than", r["content"][0]["text"])
        self.assertNotIn("abc123", r["content"][0]["text"])
        self.assertEqual(sent, [])

    def test_a_structured_error_payload_is_never_formatted_into_the_result(self):
        # repr/str escaping (backslashes) would carry the token in a form the scrub no longer matches
        tok = "tok\\with\\backslashes-0001"
        for payload in ({"error": {"message": {"nested": tok}}}, {"type": "error", "error": [tok]}, [tok]):
            r = m.route_task("http://x", tok, "task", urlopen=ok_provider(payload))
            self.assertTrue(r["isError"])
            text = r["content"][0]["text"]
            self.assertIn("unexpected provider response", text)
            self.assertNotIn("backslashes", text)

    def test_a_structured_http_error_message_is_dropped_not_formatted(self):
        tok = "tok\\with\\backslash-0001"
        r = m.route_task("http://x", tok, "task", urlopen=self._http_error(["leak " + tok]))
        text = r["content"][0]["text"]
        self.assertIn("HTTP 401", text)
        self.assertNotIn("backslash", text)

    def test_the_success_path_scrubs_an_echoed_token(self):
        tok = "tok-success-echo-0001"
        r = m.route_task("http://x", tok, "task", urlopen=ok_provider(
            {"model": "m " + tok, "content": [{"type": "text", "text": "the key is " + tok}]}))
        self.assertNotIn(tok, r["content"][0]["text"])
        self.assertNotIn(tok, r["_meta"]["cortados/routed_model"])

    def test_a_non_string_model_is_not_repr_d_into_meta(self):
        tok = "tok\\back-slash-0001"
        r = m.route_task("http://x", tok, "task", urlopen=ok_provider({"model": [tok], "content": [{"type": "text", "text": "ok"}]}))
        self.assertEqual(r["_meta"]["cortados/routed_model"], "?")

    def test_a_non_ascii_token_is_refused_before_any_request(self):
        sent = []
        r = m.route_task("http://x", "tok-\u00e9-unicode-0001", "task", urlopen=lambda req, timeout=None: sent.append(req))
        self.assertIn("printable ASCII", r["content"][0]["text"])
        self.assertNotIn("unicode", r["content"][0]["text"])
        self.assertEqual(sent, [])

    def test_an_internal_error_reply_names_only_the_exception_type(self):
        out = []
        keep = m.handle
        m.handle = lambda msg, cfg: (_ for _ in ()).throw(RuntimeError("secret-config-detail"))
        try:
            m._dispatch({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {}, out.append)
            import time
            for _ in range(100):
                if out:
                    break
                time.sleep(0.01)
        finally:
            m.handle = keep
        self.assertEqual(out[0]["error"]["message"], "internal error (RuntimeError)")

    def test_token_whitespace_is_stripped_from_the_header(self):
        seen = {}

        def _open(req, timeout=None):
            seen["token"] = req.headers.get("X-api-key")
            return FakeResp({"content": [{"type": "text", "text": "ok"}]})
        m.route_task("http://x", "tok-test-0001\n", "task", urlopen=_open)
        self.assertEqual(seen["token"], "tok-test-0001")

    @staticmethod
    def _http_error(message):
        import urllib.error

        def _open(req, timeout=None):
            body = io.BytesIO(json.dumps({"type": "error", "error": {"message": message}}).encode())
            raise urllib.error.HTTPError("http://x/v1/messages", 401, "Unauthorized", {}, body)
        return _open

    def _assert_no_token_trace(self, text, tok):
        # Neither the whole token nor any 8+-char prefix of it may surface (a prefix is what a bad cut leaks).
        self.assertNotIn(tok, text)
        for k in range(8, len(tok) + 1):
            self.assertNotIn(tok[:k], text, f"token prefix of length {k} leaked")

    def test_http_error_body_with_the_token_is_scrubbed(self):
        tok = "synthetic-provider-secret-0123456789"
        r = m.route_task("http://x", tok, "task", urlopen=self._http_error(f"bad key {tok} rejected"))
        self.assertTrue(r["isError"])
        text = r["content"][0]["text"]
        self.assertIn("HTTP 401", text)
        self.assertIn("***", text)
        self._assert_no_token_trace(text, tok)

    def test_http_error_token_straddling_the_300_char_cut_is_not_leaked(self):
        tok = "synthetic-provider-secret-0123456789"
        msg = "x" * 290 + tok + " trailing"          # the token spans chars 290..325, across the 300-char cut
        r = m.route_task("http://x", tok, "task", urlopen=self._http_error(msg))
        self.assertTrue(r["isError"])
        self._assert_no_token_trace(r["content"][0]["text"], tok)

    def test_routing_error_message_with_the_token_is_scrubbed(self):
        tok = "synthetic-provider-secret-0123456789"
        prov = ok_provider({"type": "error", "error": {"message": f"lane refused key {tok}"}})
        r = m.route_task("http://x", tok, "task", urlopen=prov)
        self.assertTrue(r["isError"])
        text = r["content"][0]["text"]
        self.assertIn("routing error", text)
        self.assertIn("***", text)
        self._assert_no_token_trace(text, tok)

    def test_routing_error_payload_without_a_message_is_scrubbed(self):
        tok = "synthetic-provider-secret-0123456789"
        r = m.route_task("http://x", tok, "task", urlopen=ok_provider(["echo", tok]))   # non-dict payload, echoed raw
        self.assertTrue(r["isError"])
        self._assert_no_token_trace(r["content"][0]["text"], tok)


class DeepNestCrashTests(unittest.TestCase):
    def test_deeply_nested_line_does_not_crash_the_server(self):
        env = {**os.environ, "CORTADOS_PROVIDER_URL": "http://127.0.0.1:1", "CORTADOS_PROVIDER_TOKEN": "t-test-0001"}
        p = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             env=env, text=True, bufsize=1)
        got = {"msg": None}

        def reader():
            for line in p.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == "ping-x":
                    got["msg"] = msg
                    break
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            p.stdin.write("[" * 1500 + "]" * 1500 + "\n")           # pathologically nested → RecursionError in json.loads
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": "ping-x", "method": "ping"}) + "\n")
            p.stdin.flush()
            t.join(6)
            self.assertIsNotNone(got["msg"])                        # the server survived the nested line and still answered the ping
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass
            p.terminate()
            p.wait(timeout=5)


class EmbeddedNewlineTokenTests(unittest.TestCase):
    def test_token_with_embedded_newline_is_rejected_without_leak(self):
        tok = "synthetic-review-\nsecret-0123456789"
        called = {"n": 0}

        def _open(req, timeout=None):
            called["n"] += 1                                    # must never be reached
            return FakeResp({"content": [{"type": "text", "text": "ok"}]})
        r = m.route_task("http://x", tok, "task", urlopen=_open)
        self.assertTrue(r["isError"])
        self.assertEqual(called["n"], 0)                        # rejected before any request
        self.assertNotIn("secret-0123456789", r["content"][0]["text"])
        self.assertNotIn("synthetic-review", r["content"][0]["text"])

    def test_clean_token_still_reaches_the_provider(self):
        seen = {}

        def _open(req, timeout=None):
            seen["token"] = req.headers.get("X-api-key")
            return FakeResp({"content": [{"type": "text", "text": "ok"}]})
        m.route_task("http://x", "cortados-provider-token-0123456789", "task", urlopen=_open)
        self.assertEqual(seen["token"], "cortados-provider-token-0123456789")   # printable token passes the check


class ConfigResolutionTests(unittest.TestCase):
    """How main() resolves the provider URL and token: env first, then the token file; an unexpanded ${VAR} URL is
    treated as unconfigured (a clear 'not configured' error rather than 'unknown url type')."""
    import os as _os
    import tempfile as _tempfile
    from unittest import mock as _mock

    def test_url_drops_unexpanded_placeholder(self):
        with self._mock.patch.dict(self._os.environ, {"CORTADOS_PROVIDER_URL": "${CORTADOS_PROVIDER_URL}"}):
            self.assertEqual(m._resolve_url(), "")                     # literal placeholder -> unconfigured
        with self._mock.patch.dict(self._os.environ, {"CORTADOS_PROVIDER_URL": "  http://host:8432 "}):
            self.assertEqual(m._resolve_url(), "http://host:8432")     # stripped

    def test_token_env_takes_precedence(self):
        with self._mock.patch.dict(self._os.environ, {"CORTADOS_PROVIDER_TOKEN": "  env-token  "}):
            self.assertEqual(m._resolve_token(), "env-token")

    def test_token_falls_back_to_the_file(self):
        d = self._tempfile.mkdtemp()
        path = self._os.path.join(d, "provider-token")
        with open(path, "w") as f:
            f.write("file-token\n")
        env = {"CORTADOS_PROVIDER_TOKEN_FILE": path}
        env_clear = {k: v for k, v in self._os.environ.items() if k != "CORTADOS_PROVIDER_TOKEN"}
        env_clear.update(env)
        with self._mock.patch.dict(self._os.environ, env_clear, clear=True):
            self.assertEqual(m._resolve_token(), "file-token")

    def test_token_undecodable_file_is_empty_not_a_crash(self):
        d = self._tempfile.mkdtemp()
        path = self._os.path.join(d, "provider-token")
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00\xd8bad")                      # not valid UTF-8
        env_clear = {k: v for k, v in self._os.environ.items() if k != "CORTADOS_PROVIDER_TOKEN"}
        env_clear["CORTADOS_PROVIDER_TOKEN_FILE"] = path
        with self._mock.patch.dict(self._os.environ, env_clear, clear=True):
            self.assertEqual(m._resolve_token(), "")

    def test_token_missing_file_is_empty(self):
        env_clear = {k: v for k, v in self._os.environ.items() if k != "CORTADOS_PROVIDER_TOKEN"}
        env_clear["CORTADOS_PROVIDER_TOKEN_FILE"] = "/nonexistent/cortados/provider-token"
        with self._mock.patch.dict(self._os.environ, env_clear, clear=True):
            self.assertEqual(m._resolve_token(), "")


if __name__ == "__main__":
    unittest.main()
