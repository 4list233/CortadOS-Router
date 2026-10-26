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
        r = m.route_task("http://x:8432", "tok", "do a thing", urlopen=prov)
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
        m.route_task("http://host:8432/", "secret", "task text", context="extra", urlopen=_open)
        self.assertEqual(seen["url"], "http://host:8432/v1/messages")
        self.assertEqual(seen["token"], "secret")
        self.assertEqual(seen["body"]["model"], "cortados")
        self.assertIn("task text", seen["body"]["messages"][0]["content"][0]["text"])
        self.assertIn("extra", seen["body"]["messages"][0]["content"][0]["text"])

    def test_empty_task_is_an_error(self):
        self.assertTrue(m.route_task("http://x", "t", "   ", urlopen=ok_provider({}))["isError"])

    def test_unconfigured_is_an_error(self):
        self.assertTrue(m.route_task("", "", "task", urlopen=ok_provider({}))["isError"])

    def test_provider_error_payload_surfaces(self):
        prov = ok_provider({"type": "error", "error": {"message": "S1 unavailable; no Codex lane"}})
        r = m.route_task("http://x", "t", "task", urlopen=prov)
        self.assertTrue(r["isError"])
        self.assertIn("S1 unavailable", r["content"][0]["text"])

    def test_unreachable_provider_is_an_error(self):
        def _boom(req, timeout=None):
            raise OSError("connection refused")
        r = m.route_task("http://x", "t", "task", urlopen=_boom)
        self.assertTrue(r["isError"])
        self.assertIn("could not reach", r["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    CFG = {"url": "http://x:8432", "token": "t"}

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
    CFG = {"url": "http://x:8432", "token": "t"}

    def test_truncated_response_is_an_error_not_a_crash(self):
        class Trunc:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): raise http.client.IncompleteRead(b"partial")
        r = m.route_task("http://x", "t", "task", urlopen=lambda req, timeout=None: Trunc())
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
                         "params": {"name": "route_task", "arguments": {"task": "slow"}}}, {"url": "u", "token": "t"}, write)
            time.sleep(0.05)
            m._dispatch({"jsonrpc": "2.0", "id": "p", "method": "ping"}, {"url": "u", "token": "t"}, write)
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
