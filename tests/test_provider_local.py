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
            content = [{"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"], "input": {"path": "x"}}]
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
            events += [{"type": "content_block_start", "index": i, "content_block": b},
                       {"type": "content_block_stop", "index": i}]
        events += [{"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 3}},
                   {"type": "message_stop"}]
        for ev in events:
            self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()


def write_config(directory, base_url, auto_route=False):
    cfg = {"nodes": [{"id": "pro", "role": "client", "target": "local", "pool_eligible": False,
                      "ollama": {"base_url_env": "TEST_LOCAL_PRO_URL", "auto_route": auto_route, "probe_timeout_s": 1,
                                 "connect_timeout_s": 1, "read_timeout_s": 5,
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
        self.assertIn("hello from qwen", raw)
        self.assertTrue(FakeOllama.seen[-1][1]["stream"])

    def test_tool_use_passthrough(self):
        body = {**self.BODY, "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
        got = self.post(body)
        self.assertEqual(got["stop_reason"], "tool_use")
        self.assertEqual(got["content"][0], {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "x"}})
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

    def test_missing_base_url_is_a_config_error(self):
        os.environ["TEST_LOCAL_PRO_URL"] = ""
        with self.assertRaises(local_lane.LocalConfigError):
            local_lane.nodes()


if __name__ == "__main__":
    unittest.main()
