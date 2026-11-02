"""Local model lanes (pin-only): sub/<local model> relays to a fake in-thread Ollama (Messages API + /api/tags)."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core
from cortados_router.provider import local_lane, routing, server
from tests.test_provider import EndpointHarness, TOKEN

TAG = "hf.co/example/Qwen-27B-GGUF:Q4_K_M"


class FakeOllama(BaseHTTPRequestHandler):
    tags = [TAG, "phi:latest"]
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/api/tags":
            self.send_error(404)
            return
        raw = json.dumps({"models": [{"name": t, "model": t} for t in self.tags]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeOllama.seen.append((self.path, body))
        if body["model"] not in self.tags:
            raw = json.dumps({"type": "error", "error": {"type": "not_found_error", "message": "model not found"}}).encode()
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if body.get("tools"):
            content = [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:8]}", "name": body["tools"][0]["name"],
                        "input": {"path": "x"}}]
            stop = "tool_use"
        else:
            content = [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "hello from qwen"}]
            stop = "end_turn"
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": content,
               "stop_reason": stop, "usage": {"input_tokens": 5, "output_tokens": 3}}
        if not body.get("stream"):
            raw = json.dumps(msg).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = [{"type": "message_start", "message": {**msg, "content": [], "stop_reason": None}}]
        for i, b in enumerate(content):
            if b["type"] == "text":                       # text streams as deltas, as Ollama sends it
                events += [{"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}]
                events += [{"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": w}}
                           for w in ("hello ", "from ", "qwen")]
            else:
                events += [{"type": "content_block_start", "index": i, "content_block": b}]
            events += [{"type": "content_block_stop", "index": i}]
        events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}},
                   {"type": "message_stop"}]
        for ev in events:
            self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()


def write_config(directory, base_url, auto_route=False, token_file=None):
    cfg = {"nodes": [{"id": "pro", "role": "client", "target": "local", "pool_eligible": False,
                      "ollama": {"base_url_env": "TEST_LOCAL_PRO_URL", "auto_route": auto_route, "probe_timeout_s": 1,
                                 "connect_timeout_s": 1, "read_timeout_s": 5,
                                 **({"token_file": token_file} if token_file else {}),
                                 "models": [{"id": TAG, "alias": "qwen-27b", "name": "Qwen 27B", "context_length": 32768},
                                            {"id": "phi:latest"}]}}]}
    p = Path(directory) / "resources.json"
    p.write_text(json.dumps(cfg))
    os.environ["TEST_LOCAL_PRO_URL"] = base_url
    return str(p)


class LocalHarness(EndpointHarness):
    def setUp(self):
        super().setUp()
        self.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        FakeOllama.seen = []
        self._env = os.environ.get(local_lane.CONFIG_ENV)
        os.environ[local_lane.CONFIG_ENV] = write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}")
        self._keep = (core.STATE, core.lane_health, core.decide)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-local-"))   # never the live router.db
        core.lane_health = lambda: {}

        def no_s1(text, ctx=None):
            raise AssertionError("a pinned request must not be scored")
        core.decide = no_s1
        self.reg.router = lambda body, client, **kw: routing.choose(
            core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=sys.executable,
            codex_bin=sys.executable, **kw)
        self.reg.executed = lambda route: core.log_execution(route["route_id"], route["provider"], route["model"])

    def tearDown(self):
        core.STATE, core.lane_health, core.decide = self._keep
        if self._env is None:
            os.environ.pop(local_lane.CONFIG_ENV, None)
        else:
            os.environ[local_lane.CONFIG_ENV] = self._env
        os.environ.pop("TEST_LOCAL_PRO_URL", None)
        self.ollama.shutdown()
        self.ollama.server_close()
        super().tearDown()

    def error_of(self, body):
        try:
            self.post(body)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]
        self.fail("expected an HTTP error")


class LocalPinTests(LocalHarness):
    BODY = {"model": "sub/qwen-27b", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}

    def test_pin_forwards_and_returns_and_is_logged(self):
        got = self.post(self.BODY)
        self.assertEqual(got["stop_reason"], "end_turn")
        self.assertEqual(got["model"], "sub/qwen-27b")                       # echoes what the client asked for
        self.assertEqual([b["type"] for b in got["content"]], ["thinking", "text"])
        self.assertEqual(FakeOllama.seen[-1][0], "/v1/messages")
        self.assertEqual(FakeOllama.seen[-1][1]["model"], TAG)               # the node's tag, not the alias
        for _ in range(50):                  # actual_model is recorded after the answer is delivered
            with sqlite3.connect(core.STATE / "router.db") as db:
                rows = db.execute("SELECT engine, band, lane, provider, model, actual_model FROM routes").fetchall()
            if rows and rows[0][-1]:
                break
            time.sleep(0.05)
        self.assertEqual(rows, [("pin", "pin", "local-pro", "local-pro", TAG, f"local-pro/{TAG}")])

    def test_continuation_turns_are_not_logged_again(self):
        self.post(self.BODY)
        follow = {**self.BODY, "messages": self.BODY["messages"] + [
            {"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}]}
        self.post(follow)
        with sqlite3.connect(core.STATE / "router.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM routes").fetchone()[0], 1)

    def test_streaming_passthrough(self):
        raw = self.post({**self.BODY, "stream": True})
        events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[0]["message"]["model"], "sub/qwen-27b")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual("".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta"),
                         "hello from qwen")
        self.assertTrue(FakeOllama.seen[-1][1]["stream"])

    def test_tool_use_passthrough(self):
        body = {**self.BODY, "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
        got = self.post(body)
        self.assertEqual(got["stop_reason"], "tool_use")
        self.assertEqual({k: v for k, v in got["content"][0].items() if k != "id"},
                         {"type": "tool_use", "name": "Read", "input": {"path": "x"}})
        self.assertTrue(got["content"][0]["id"].startswith("toolu_"))
        self.assertEqual(FakeOllama.seen[-1][1]["tools"], body["tools"])
        raw = self.post({**body, "stream": True})
        self.assertIn('"tool_use"', raw)

    def test_pin_by_tag_also_works(self):
        self.assertEqual(self.post({**self.BODY, "model": "sub/phi:latest"})["stop_reason"], "end_turn")

    def test_node_down_is_a_clear_503_and_nothing_else_runs(self):
        self.ollama.shutdown()
        self.ollama.server_close()
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("node pro did not answer /api/tags", msg)
        self.assertIn("no other model was tried", msg)
        self.assertEqual(self.reg.sessions, {})                              # no fallback session opened

    def test_model_not_listed_on_the_node_is_503(self):
        FakeOllama.tags = ["phi:latest"]
        try:
            code, msg = self.error_of(self.BODY)
        finally:
            FakeOllama.tags = [TAG, "phi:latest"]
        self.assertEqual(code, 503)
        self.assertIn("does not list", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_unknown_model_is_400_listing_pinnable_ids(self):
        code, msg = self.error_of({**self.BODY, "model": "sub/qwen-999"})
        self.assertEqual(code, 400)
        self.assertIn("qwen-27b", msg)
        self.assertIn("phi:latest", msg)
        self.assertIn("claude-opus-5-5", msg)

    def test_too_large_for_the_context_is_413(self):
        big = {**self.BODY, "messages": [{"role": "user", "content": "x" * (4 * 40000)}]}
        self.assertEqual(self.error_of(big)[0], 413)

    def test_models_lists_local_pins_with_node_labels(self):
        req = urllib.request.Request(self.url + "/v1/models", headers={"authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            names = {m["id"]: m["display_name"] for m in json.loads(r.read())["data"]}
        self.assertEqual(names["sub/qwen-27b"], "Qwen 27B · CortadOS (Pro)")
        self.assertEqual(names["sub/phi:latest"], "phi:latest · CortadOS (Pro)")
        self.assertIn("sub/claude-opus-5-5", names)

    def test_excluded_model_refuses_the_local_pin(self):
        self.assertEqual(self.status_of(self.BODY, headers={"x-cortados-exclude-model": "sub/qwen-27b"}), 400)
        self.assertEqual(FakeOllama.seen, [])


class LocalConversationPinTests(LocalHarness):
    """A local-pinned conversation keeps its model, exactly as a Claude or Codex pin does: the binding is recorded in
    the registry and every continuation is checked against it (the 409 "fixed per conversation")."""
    TOOLS = [{"name": "Read", "input_schema": {"type": "object"}}]

    def opener(self, model, stream=False):
        return {"model": model, "max_tokens": 64, "tools": self.TOOLS, "stream": stream,
                "messages": [{"role": "user", "content": "hi"}]}

    def follow(self, opener, reply_content, model):
        last = reply_content[-1] if reply_content else {}
        nxt = ({"role": "user", "content": [{"type": "tool_result", "tool_use_id": last["id"], "content": "ok"}]}
               if last.get("type") == "tool_use" else {"role": "user", "content": "more"})
        return {**opener, "model": model, "messages": opener["messages"] + [
            {"role": "assistant", "content": reply_content}, nxt]}

    def conflict(self, body):
        code, msg = self.error_of(body)
        self.assertEqual(code, 409)
        self.assertIn("fixed per conversation", msg)
        return msg

    def test_a_local_conversation_continues_on_its_model(self):
        first = self.opener("sub/qwen-27b")
        got = self.post(first)
        self.assertEqual(len(self.reg.sessions), 1)
        second = self.follow(first, got["content"], "sub/qwen-27b")
        got2 = self.post(second)
        self.assertEqual(got2["stop_reason"], "tool_use")
        self.assertEqual(len(FakeOllama.seen), 2)
        # the tag names the same pin, and `cortados` continues whatever the conversation runs
        self.post(self.follow(second, got2["content"], f"sub/{TAG}"))
        self.assertEqual(len(self.reg.sessions), 1)

    def test_a_streamed_local_conversation_continues(self):
        first = {**self.opener("sub/qwen-27b", stream=True), "tools": self.TOOLS}
        raw = self.post({**first, "tools": []})                 # tool-less: a one-shot, never registered
        self.assertEqual(self.reg.sessions, {})
        raw = self.post(first)
        blocks = [json.loads(l[5:])["content_block"] for l in raw.splitlines()
                  if l.startswith("data:") and '"content_block_start"' in l]
        self.assertEqual(self.post(self.follow({**first, "stream": False}, blocks, "sub/qwen-27b"))["stop_reason"], "tool_use")

    def test_claude_pinned_conversation_cannot_switch_to_a_local_pin(self):
        first = self.opener("sub/opus")
        got = self.post(first)
        msg = self.conflict(self.follow(first, got["content"], "sub/qwen-27b"))
        self.assertIn("pinned to sub/claude-opus-5-5", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_local_pinned_conversation_cannot_switch_to_a_claude_or_codex_pin(self):
        first = self.opener("sub/qwen-27b")
        got = self.post(first)
        for other in ("sub/opus", "sub/gpt-6.1-sol", "sub/phi:latest"):
            msg = self.conflict(self.follow(first, got["content"], other))
            self.assertIn("pinned to sub/qwen-27b", msg)
        self.assertEqual(len(FakeOllama.seen), 1)

    def test_s1_routed_conversation_cannot_switch_to_a_local_pin(self):
        pinned_router = self.reg.router

        def router(body, client, **kw):
            if body.get("model") == "cortados":         # stands in for S1: no scorer runs in this test
                return {"provider": "claude-cli", "model": "claude-sonnet-5", "route_id": "execution:test:s1",
                        "band": "R2", "degraded": False}
            return pinned_router(body, client, **kw)
        self.reg.router = router
        first = self.opener("cortados")
        got = self.post(first)
        msg = self.conflict(self.follow(first, got["content"], "sub/qwen-27b"))
        self.assertIn("routed by S1 to claude-sonnet-5", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_a_failed_opening_turn_leaves_no_conversation(self):
        self.ollama.shutdown()
        self.ollama.server_close()
        self.assertEqual(self.error_of(self.opener("sub/qwen-27b"))[0], 503)
        self.assertEqual(self.reg.sessions, {})


class LocalAutoRoutingOffTests(unittest.TestCase):
    """While the switch is off (the only state it can be in today), S1 routing never sees a local lane."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = os.environ.get(local_lane.CONFIG_ENV)
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9")

    def tearDown(self):
        if self._env is None:
            os.environ.pop(local_lane.CONFIG_ENV, None)
        else:
            os.environ[local_lane.CONFIG_ENV] = self._env
        os.environ.pop("TEST_LOCAL_PRO_URL", None)

    def test_served_routes_and_pinnable_models_never_include_a_local_lane(self):
        self.assertTrue(local_lane.pins())
        for p, _ in routing.served_routes(core):
            self.assertFalse(p.startswith(local_lane.LANE_PREFIX))
        for p, _ in routing.pinnable_models(core):
            self.assertFalse(p.startswith(local_lane.LANE_PREFIX))
        self.assertNotIn("local-pro", routing.SERVED_PROVIDERS)

    def test_s1_choice_is_restricted_to_served_providers(self):
        seen = {}
        keep = routing.execution_routing.select

        def fake_select(core_, **kw):
            seen.update(kw)
            return {"router": "x", "provider": "codex-cli", "model": "gpt-6.1-sol"}
        routing.execution_routing.select = fake_select
        try:
            routing.choose(core, {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]},
                           client="t", conversation_id="c")
        finally:
            routing.execution_routing.select = keep
        providers = {p for p, _ in seen["constraints"]["allowed_routes"]}
        self.assertFalse(any(p.startswith(local_lane.LANE_PREFIX) for p in providers))

    def test_turning_the_switch_on_is_refused_loudly(self):
        write_config(self.dir, "http://127.0.0.1:9", auto_route=True)
        with self.assertRaises(local_lane.LocalConfigError) as cm:
            local_lane.pins()
        self.assertIn("auto_route", str(cm.exception))

    def test_no_config_means_no_local_lanes(self):
        os.environ.pop(local_lane.CONFIG_ENV, None)
        self.assertEqual(local_lane.pins(), [])
        self.assertIsNone(local_lane.find("qwen-27b"))

    def test_non_positive_timeouts_are_config_errors(self):
        path = os.environ[local_lane.CONFIG_ENV]
        for key in ("read_timeout_s", "connect_timeout_s", "probe_timeout_s"):
            for bad in (0, -1, "x"):
                cfg = json.loads(Path(path).read_text())
                cfg["nodes"][0]["ollama"][key] = bad
                Path(path).write_text(json.dumps(cfg))
                with self.assertRaises(local_lane.LocalConfigError) as cm:
                    local_lane.nodes()
                self.assertIn(key, str(cm.exception))
            write_config(self.dir, "http://127.0.0.1:9")

    def test_missing_base_url_is_a_config_error(self):
        os.environ["TEST_LOCAL_PRO_URL"] = ""
        with self.assertRaises(local_lane.LocalConfigError):
            local_lane.nodes()


if __name__ == "__main__":
    unittest.main()
