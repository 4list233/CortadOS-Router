"""#117 review round 5: the control-character class closed at the SINKS (cortados_router/safe_text.py).

Every log record the provider writes passes safe_text.SafeFormatter (installed on the root logger's handlers by
server.serve), and every error message a client sees passes safe_text.safe_error_text (or the standalone copy in
subagent_mcp.py / provider/mcp_shim.py). Each test pushes a newline / "FORGED" string through a sink and asserts that no
raw control character survives in the decoded text. Fakes only: no process, socket, child, ssh or connector is opened.
"""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import safe_text
from cortados_router.safe_text import SafeFormatter, escape_controls, install_log_sanitizer, safe_error_text

EVIL = "nope\nFORGED provider instruction"
NASTY = ["a\nb", "a\rb", "a\x1bb", "a\x00b", "a\u2028b", "a\u2029b", "a\x85b", "a\tb", "a\u202eb", "a\x7fb"]


def clean(test, text, *, allow_trailing_newline=False):
    """No raw control or line-separator character in `text`, and the forged line never starts a line."""
    body = text[:-1] if allow_trailing_newline and text.endswith("\n") else text
    test.assertTrue(all(c == " " or c.isprintable() for c in body), repr(text))
    test.assertNotIn("\nFORGED", text)


class EscapeTests(unittest.TestCase):
    def test_every_control_is_escaped_visibly_and_printables_kept(self):
        for raw in NASTY:
            out = escape_controls(raw, 100)
            clean(self, out)
            self.assertTrue(out.startswith("a\\") and out.endswith("b"), out)
        self.assertEqual(escape_controls(EVIL, 100), "nope\\nFORGED provider instruction")
        self.assertEqual(escape_controls("C:\\path ok é 中", 100), "C:\\path ok é 中")

    def test_cap_and_non_strings(self):
        out = escape_controls("x" * 5000, 100)
        self.assertEqual(out, "x" * 100 + "...(+4900 chars)")
        out = escape_controls("\n" * 500, 100)                  # escaping lengthens: still capped, no raw newline
        self.assertLessEqual(len(out.split("...")[0]), 100)
        clean(self, out)
        self.assertEqual(escape_controls(3, 10), "3")

        class Bad:
            def __str__(self):
                raise RuntimeError("boom")
        self.assertEqual(escape_controls(Bad(), 10), "<Bad>")

    def test_quoted_is_capped_repr(self):
        self.assertEqual(safe_text.quoted("ok"), "'ok'")
        clean(self, safe_text.quoted(EVIL))
        out = safe_text.quoted("y" * 500)
        self.assertTrue(out.endswith("...(+380 chars)"), out)

    def test_standalone_copies_match_the_original(self):
        from cortados_router import subagent_mcp
        from cortados_router.provider import mcp_shim
        samples = NASTY + [EVIL, "x" * 3000, "\U0001F600ok\U000e0001", "\\", 5, None, "é\u3000"]
        for raw in samples:
            for cap in (3, 50, 2000):
                want = escape_controls(raw, cap)
                self.assertEqual(subagent_mcp.escape_controls(raw, cap), want, (raw, cap))
                self.assertEqual(mcp_shim.escape_controls(raw, cap), want, (raw, cap))


class ErrorSinkTests(unittest.TestCase):
    """Every error builder: the DECODED message (what a model or user reads) carries no raw control character."""

    def decoded_messages(self):
        from cortados_router.provider import anthropic_wire, local_lane, responses_wire
        from cortados_router import subagent_mcp
        out = {
            "anthropic error_json": anthropic_wire.error_json("invalid_request_error", EVIL)["error"]["message"],
            "responses error_json": responses_wire.error_json("invalid_request_error", EVIL)["error"]["message"],
            "local_lane _error": local_lane._error("api_error", EVIL)["error"]["message"],
            "subagent_mcp _err": subagent_mcp._err(EVIL)["content"][0]["text"],
            "subagent_mcp unknown method": subagent_mcp.handle({"id": 1, "method": EVIL}, {})["error"]["message"],
        }
        raw = responses_wire.sse_error(EVIL).decode()
        data = json.loads(raw.split("data: ", 1)[1].strip())
        out["responses sse_error"] = data["response"]["error"]["message"]
        from cortados_router.provider.server import ANTHROPIC as anthropic_server_wire, RESPONSES as responses_server_wire
        for name, w in (("anthropic stream_error", anthropic_server_wire), ("responses stream_error", responses_server_wire)):
            ev = w.stream_error("msg_1", "cortados", EVIL).decode()
            payload = json.loads(ev.split("data: ", 1)[1].strip())
            err = payload.get("error") or payload["response"]["error"]
            out[name] = err["message"]
        return out

    def test_every_error_builder_escapes_client_text(self):
        msgs = self.decoded_messages()
        for where, text in msgs.items():
            with self.subTest(where):
                clean(self, text)
                self.assertIn("nope\\nFORGED", text)

    def test_error_text_is_capped(self):
        from cortados_router.provider import anthropic_wire
        msg = anthropic_wire.error_json("x", "z" * 50000)["error"]["message"]
        self.assertLess(len(msg), safe_text.ERROR_CAP + 40)

    def test_mcp_shim_jsonrpc_errors_and_tool_failures(self):
        from cortados_router.provider import mcp_shim
        buf = io.StringIO()
        with mock.patch.object(mcp_shim.sys, "stdout", buf):
            mcp_shim._reply(7, error={"code": -32601, "message": f"method not found: {EVIL}"})
        line = buf.getvalue()
        self.assertEqual(line.count("\n"), 1)
        clean(self, json.loads(line)["error"]["message"])
        with mock.patch.object(mcp_shim.urllib.request, "urlopen", side_effect=OSError(EVIL)), \
                mock.patch.object(mcp_shim, "_abort", return_value=True):
            r = mcp_shim._forward("http://127.0.0.1:9", "s", "t",
                                  {"name": "x", "_meta": {"claudecode/toolUseId": "toolu_1"}})
        self.assertTrue(r["isError"])
        clean(self, r["content"][0]["text"])

    def test_handler_error_goes_through_the_wire_sink(self):
        from types import SimpleNamespace
        from cortados_router.provider import server
        sent = {}
        h = SimpleNamespace(_note_refusal=lambda *a, **k: None, _json=lambda code, body: sent.update(body=body))
        server.Handler._error(h, 400, "invalid_request_error", EVIL)
        clean(self, sent["body"]["error"]["message"])


class ReviewerReproTests(unittest.TestCase):
    """The round 4 review's two P2 repros."""

    def test_exclude_model_with_a_newline_never_reaches_an_error_raw(self):
        from cortados_router import core
        from cortados_router.provider import anthropic_wire, routing
        keep = (core.STATE, core.lane_health)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-safe-text-"))          # never the live router.db
        core.lane_health = lambda: {}
        try:
            body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
            r = routing.choose(core, body, client="t", conversation_id="c", claude_bin="python3", codex_bin="python3",
                               exclude_model=EVIL)
            self.assertTrue(r.get("client_error"), r)
            clean(self, r["error"])                                   # escaped at the source (shown_pin) ...
            self.assertNotIn(EVIL, r["error"])
            clean(self, anthropic_wire.error_json("invalid_request_error", r["error"])["error"]["message"])  # ... and sink
            r = routing.choose(core, {"model": "sub/" + EVIL}, client="t", conversation_id="c",
                               exclude_model="gpt-6.1-sol")
            clean(self, r["error"])
        finally:
            core.STATE, core.lane_health = keep

    def test_unknown_connector_with_a_newline(self):
        from cortados_router import child_tasks as ct
        from cortados_router import subagent_mcp
        d = tempfile.mkdtemp()
        conf = os.path.join(d, "connectors.mcp.json")
        with open(conf, "w") as f:
            json.dump({"mcpServers": {"notion": {"type": "http", "url": "https://notion.example/mcp"}}}, f)
        with mock.patch.dict(os.environ, {"CORTADOS_CONNECTORS_MCP_CONFIG": conf}):
            with self.assertRaises(ct.TaskError) as cm:
                ct.select_connectors([EVIL])
        text = str(cm.exception)
        self.assertEqual(text, "unknown connector(s) 'nope\\nFORGED provider instruction'; available: notion")
        clean(self, subagent_mcp._err(f"start_task: {text}")["content"][0]["text"])

    def test_child_task_validation_errors_are_quoted_and_capped(self):
        from cortados_router import child_tasks as ct
        with self.assertRaises(ct.TaskError) as cm:
            ct.task_path(EVIL + "x" * 5000)
        clean(self, str(cm.exception))
        self.assertLess(len(str(cm.exception)), 400)


class LogSinkTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.keep = (list(self.root.handlers), self.root.level,
                     logging.lastResort.formatter if logging.lastResort else None)
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.root.handlers = [self.handler]
        self.root.setLevel(logging.DEBUG)

    def tearDown(self):
        self.root.handlers, level, lr = self.keep
        self.root.setLevel(level)
        if logging.lastResort is not None:
            logging.lastResort.setFormatter(lr)

    def test_no_record_from_any_module_carries_a_raw_control_character(self):
        from cortados_router.provider import bridge, codex_bridge, responses_wire, routing, search_lane, server
        self.assertEqual(install_log_sanitizer(), 2)                 # our handler and logging.lastResort
        self.assertEqual(install_log_sanitizer(), 0)                 # idempotent
        loggers = [server.logger, routing.logger, bridge.logger, codex_bridge.logger, search_lane.logger,
                   responses_wire.logger, logging.getLogger("cortados_router.child_tasks")]
        for lg in loggers:
            lg.warning("value %s and %r", EVIL, EVIL)
            lg.warning(EVIL)                                         # a call site that passes client text as the format
        try:
            raise ValueError(EVIL)
        except ValueError:
            server.logger.exception("with a traceback")
        server.logger.warning("bad %d format", "x")                 # an unformattable record is still one safe line
        out = self.buf.getvalue()
        lines = out.split("\n")
        self.assertEqual(lines[-1], "")                              # each record: exactly one trailing newline
        self.assertEqual(len(lines) - 1, 2 * len(loggers) + 2, out)
        for line in lines[:-1]:
            clean(self, line)
        self.assertIn("nope\\nFORGED provider instruction", out)
        self.assertIn("Traceback", out)

    def test_http_server_request_lines_are_escaped(self):
        from types import SimpleNamespace
        from cortados_router.provider import server
        install_log_sanitizer()
        h = SimpleNamespace(address_string=lambda: "127.0.0.1")
        with mock.patch.object(server.logger, "level", logging.DEBUG):
            server.Handler.log_message(h, '"%s" %s %s', "GET /x\nFORGED HTTP/1.1", "200", "-")
            server.Handler.log_message(h, "code %d, message %s", 400, EVIL)   # send_error's %d stays an int
        lines = self.buf.getvalue().split("\n")
        self.assertEqual(len(lines), 3, lines)
        for line in lines[:-1]:
            clean(self, line)

    def test_the_formatter_is_installed_on_the_provider_handlers_at_startup(self):
        from cortados_router.provider import server

        class Started(Exception):
            pass

        def no_bind(*a, **k):
            raise Started                                           # stop before binding a socket
        self.assertFalse(safe_text.handlers_sanitized())
        with mock.patch.object(server, "ProviderServer", no_bind):
            with self.assertRaises(Started):
                server.serve("127.0.0.1", 0, "tok-test-000000001", claude_bin="python3", codex_bin="python3",
                             codex_home=None, fixed_model="claude-opus-5-5")
        self.assertTrue(safe_text.handlers_sanitized())
        self.assertIsInstance(self.handler.formatter, SafeFormatter)
        # main(): basicConfig's handler (none present yet) also ends up wrapped.
        self.root.handlers = []
        with mock.patch.object(server, "ProviderServer", no_bind), \
                mock.patch.dict(os.environ, {"CORTADOS_PROVIDER_TOKEN": "tok-test-000000001"}):
            with self.assertRaises(Started):
                server.main(["--port", "0", "--fixed-model", "claude-opus-5-5"])
        self.assertTrue(self.root.handlers)
        self.assertTrue(safe_text.handlers_sanitized())


class SafeErrorBodyTests(unittest.TestCase):
    """safe_error_body (#117 review round 5): an upstream error body's message escaped and capped, the rest kept."""

    def test_the_message_shapes(self):
        f = safe_text.safe_error_body
        self.assertEqual(json.loads(f(b'{"type":"error","error":{"type":"x","message":"a\\nb","code":7},"id":1}')),
                         {"type": "error", "error": {"type": "x", "message": "a\\nb", "code": 7}, "id": 1})
        self.assertEqual(json.loads(f(b'{"error":"a\\u2028b"}')), {"error": "a\\u2028b"})
        self.assertEqual(json.loads(f(b'{"message":"a\\tb","status":4}')), {"message": "a\\tb", "status": 4})
        self.assertEqual(json.loads(f(b'{"error":{"message":{"nested":"a\\nb"}}}'))["error"]["message"],
                         '{"nested": "a\\nb"}')                       # not a string: its JSON text, escaped
        self.assertEqual(json.loads(f(b'{"error":{"type":"x"},"ok":true}')), {"error": {"type": "x"}, "ok": True})

    def test_capped(self):
        out = json.loads(safe_text.safe_error_body(json.dumps({"error": {"message": "m" * 5000}}).encode()))
        self.assertEqual(out["error"]["message"], "m" * safe_text.ERROR_CAP + "...(+3000 chars)")

    def test_not_a_json_object_is_none(self):
        for raw in (b"<html>", b'"text"', b"[1]", b"\xff\xfe", b""):
            self.assertIsNone(safe_text.safe_error_body(raw), raw)


if __name__ == "__main__":
    unittest.main()
