"""POST /v1/responses end to end: the Codex CLI's Responses requests served by the same sessions as /v1/messages,
with tests/fixtures/fake_claude_cli.py standing in for `claude -p` and tests/fixtures/fake_codex_app_server.py for
`codex app-server`.

The opening request is the redacted real capture (tests/fixtures/codex-responses-request-0.163.0.json). The
follow-up items (assistant message, function_call, function_call_output) are the output items the endpoint itself
returned, sent back as Codex re-sends its history (store=false), plus the function_call_output built from knowledge
of the Responses shapes (never captured on this wire). Every test runs on both backends: ResponsesEndpointTests
routes to claude-cli, ResponsesOnCodexTests to codex-cli.
"""
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router.provider import codex_bridge, routing, server
from cortados_router.provider.server import ProviderServer, Registry

FIXTURES = Path(__file__).with_name("fixtures")
FAKE_CODEX = FIXTURES / "fake_codex_app_server.py"
FAKE_CLAUDE = FIXTURES / "fake_claude_cli.py"
CAPTURE = FIXTURES / "codex-responses-request-0.163.0.json"
TOKEN = "test-token"
READ_TOOL = {"type": "function", "name": "Read", "description": "Read a file", "strict": False,
             "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
CLAUDE_ROUTE = {"provider": "claude-cli", "model": "claude-test-model", "band": "R1", "route_id": "r1"}
CODEX_ROUTE = {"provider": "codex-cli", "model": "gpt-test", "band": "R2", "route_id": "r1"}
# The headers Codex 0.163.0 sends to a custom provider (brief §C); only authorization and the User-Agent matter here.
CODEX_HEADERS = {"content-type": "application/json", "authorization": f"Bearer {TOKEN}", "originator": "codex_exec",
                 "user-agent": "codex_exec/0.163.0 (Mac OS 27.0.1; arm64)", "accept": "text/event-stream",
                 "session-id": "00000000-0000-0000-0000-000000000000", "x-codex-turn-metadata": "{}"}


def user(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def request(*items, tools=(READ_TOOL,), stream=False):
    """A request in the capture's shape (same top-level keys), with its own input and tools."""
    body = json.loads(CAPTURE.read_text())
    body.update({"stream": stream, "input": list(items), "tools": list(tools)})
    return body


def capture_user_text():
    """The user text of the capture as the fake CLIs echo it: every user block, in order, joined by one space."""
    return " ".join(c["text"] for it in json.loads(CAPTURE.read_text())["input"] if it.get("role") == "user"
                    for c in it["content"])


def parse_sse(raw: bytes):
    """(event, data) per event, plus the number of comment lines (the keepalive) the stream carried."""
    events, comments = [], 0
    for chunk in raw.decode().split("\n\n"):
        lines = [l for l in chunk.split("\n") if l]
        comments += sum(1 for l in lines if l.startswith(":"))
        lines = [l for l in lines if not l.startswith(":")]
        if lines:
            events.append(([l[7:] for l in lines if l.startswith("event: ")][0],
                           json.loads([l[6:] for l in lines if l.startswith("data: ")][0])))
    return events, comments


class ResponsesEndpointTests(unittest.TestCase):
    ROUTE = CLAUDE_ROUTE

    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.codex_argv_file = os.path.join(self.scratch, "codex-argv.json")
        self.claude_argv_file = os.path.join(self.scratch, "claude-argv.json")
        codex_home = os.path.join(self.scratch, "codex-home")          # a dedicated home, never the user's ~/.codex
        os.mkdir(codex_home)
        Path(codex_home, "config.toml").write_text('model = "gpt-test"\n')
        os.environ["FAKE_CODEX_ARGV"] = self.codex_argv_file
        os.environ["FAKE_CLAUDE_ARGV"] = self.claude_argv_file
        # The fake app-server dispatches sibling calls at once; the production settle window (for slow siblings, pinned
        # in test_provider_codex) would only add waiting here.
        self.settle = codex_bridge.CODEX_SETTLE_S
        codex_bridge.CODEX_SETTLE_S = 0.3
        self.route, self.clients, self.executed = dict(self.ROUTE), [], []

        def router(body, client):
            self.clients.append(client)
            return dict(self.route)

        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=[sys.executable, str(FAKE_CLAUDE)],
                            codex_bin=[sys.executable, str(FAKE_CODEX)], codex_home=codex_home,
                            router=router, executed=self.executed.append)
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.reg.gateway_url = self.url
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        codex_bridge.CODEX_SETTLE_S = self.settle
        for k in ("FAKE_CODEX_ARGV", "FAKE_CLAUDE_ARGV"):
            os.environ.pop(k, None)

    # ------------------------------------------------------------------ helpers
    def send(self, body, headers=CODEX_HEADERS, path="/v1/responses"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=data, method="POST", headers=headers),
                                      timeout=60)

    def post(self, body):
        with self.send(body) as r:
            self.assertEqual(r.headers.get("content-type"), "application/json")
            return r.headers.get("x-cortados-route"), json.loads(r.read())

    def stream(self, body):
        with self.send(dict(body, stream=True)) as r:
            self.assertEqual(r.headers.get("content-type"), "text/event-stream")
            events, comments = parse_sse(r.read())
            return r.headers.get("x-cortados-route"), events, comments

    def error_of(self, body, headers=CODEX_HEADERS):
        try:
            self.send(body, headers=headers)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def no_backend_started(self):
        self.assertEqual(self.reg.sessions, {})
        self.assertFalse(Path(self.codex_argv_file).exists())
        self.assertFalse(Path(self.claude_argv_file).exists())

    def expected_header(self):
        r = self.route
        return f"{r['provider']}/{r['model']} band={r['band']} id={r['route_id']}"

    # ------------------------------------------------------------------ tests
    def test_the_captured_codex_request_streams_a_completed_response(self):
        header, events, _ = self.stream(json.loads(CAPTURE.read_text()))
        self.assertEqual(header, self.expected_header())
        self.assertEqual(self.clients, ["codex_exec"])                 # the route log's platform: provider:codex_exec
        kinds = [k for k, _ in events]
        self.assertEqual(kinds, ["response.created", "response.output_item.added", "response.output_item.done",
                                 "response.completed"])
        self.assertEqual([d["sequence_number"] for _, d in events], [0, 1, 2, 3])
        created, completed = events[0][1]["response"], events[-1][1]["response"]
        self.assertEqual((created["status"], created["model"]), ("in_progress", "cortados"))
        self.assertTrue(created["id"].startswith("resp_cortados_"))
        self.assertEqual((completed["id"], completed["status"], completed["model"]), (created["id"], "completed", "cortados"))
        [item] = completed["output"]
        self.assertEqual(item["type"], "message")
        # the developer items became the system prompt; the user items (Codex's AGENTS.md and environment blocks
        # included: the model is meant to see them) reached the backend in order
        self.assertEqual(item["content"], [{"type": "output_text", "annotations": [], "text": "echo: " + capture_user_text()}])
        self.assertTrue(capture_user_text().startswith("# AGENTS.md instructions") and capture_user_text().endswith(" say hi"))
        self.assertEqual(completed["output"], [events[2][1]["item"]])  # the same item as output_item.done
        self.assertEqual(len(self.reg.sessions), 1)                    # a request with tools keeps its session
        self.assertEqual([r["route_id"] for r in self.executed], ["r1"])

    def test_a_function_call_round_trips_by_call_id_across_two_requests(self):
        items = [user("please use tool")]
        _, r1 = self.post(request(*items))
        [call] = [i for i in r1["output"] if i["type"] == "function_call"]
        self.assertEqual((call["name"], json.loads(call["arguments"]), call["status"]), ("Read", {"file_path": "f0.md"}, "completed"))
        self.assertTrue(call["id"].startswith("fc_") and call["call_id"])
        # Codex re-sends the output items as history, then the tool's result keyed by call_id
        items += r1["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": "hello"}]
        header, r2 = self.post(request(*items))
        self.assertEqual(header, self.expected_header())
        self.assertEqual([i["type"] for i in r2["output"]], ["message"])
        self.assertEqual(r2["output"][0]["content"][0]["text"], "tool said: hello")
        # a plain follow-up continues the same session
        items += r2["output"] + [user("thanks")]
        _, r3 = self.post(request(*items))
        self.assertEqual(r3["output"][0]["content"][0]["text"], "echo: thanks")
        self.assertEqual(len(self.reg.sessions), 1)
        self.assertEqual(self.clients, ["codex_exec"])                 # routed once, for the whole conversation
        self.assertEqual([r["route_id"] for r in self.executed], ["r1"])

    def test_a_non_streaming_request_returns_the_response_object(self):
        header, r = self.post(request(user("hi")))
        self.assertEqual(header, self.expected_header())
        self.assertEqual((r["object"], r["status"], r["model"]), ("response", "completed", "cortados"))
        self.assertTrue(r["id"].startswith("resp_cortados_"))
        self.assertEqual(r["output"][0]["content"], [{"type": "output_text", "text": "echo: hi", "annotations": []}])
        u = r["usage"]
        self.assertEqual(u["total_tokens"], u["input_tokens"] + u["output_tokens"])
        self.assertEqual(sorted(u), ["input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"])
        self.assertIn("cached_tokens", u["input_tokens_details"])
        self.assertIn("reasoning_tokens", u["output_tokens_details"])

    def test_a_toolless_request_is_a_one_shot_and_keeps_no_session(self):
        _, r = self.post(request(user("title this"), tools=()))
        self.assertEqual(r["output"][0]["content"][0]["text"], "echo: title this")
        self.assertEqual(self.reg.sessions, {})

    def test_a_continuation_the_endpoint_no_longer_holds_is_a_409_in_the_responses_shape(self):
        code, body = self.error_of(request(
            user("x"), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "from before a restart"}]},
            user("go on")))
        self.assertEqual(code, 409)
        self.assertEqual(sorted(body), ["error"])                      # {"error": {...}}, not Anthropic's {"type": "error", ...}
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("no longer holds", body["error"]["message"])
        # a tool result for a call this endpoint never issued is the same lost conversation
        code, body = self.error_of(request(
            user("x"), {"type": "function_call", "call_id": "call_gone", "name": "Read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_gone", "output": "late"}))
        self.assertEqual(code, 409)
        self.no_backend_started()

    def test_a_failing_router_is_a_503_in_the_responses_shape_and_starts_nothing(self):
        def broken(body, client):
            raise sqlite3.OperationalError("database is locked")
        self.reg.router = broken
        code, body = self.error_of(request(user("x")))
        self.assertEqual((code, sorted(body), body["error"]["type"]), (503, ["error"], "overloaded_error"))
        self.assertIn("routing failed", body["error"]["message"])
        self.reg.router = lambda body, client: {"error": "No authenticated, available model fits"}
        code, body = self.error_of(request(user("x")))
        self.assertEqual(code, 503)
        self.assertIn("No authenticated", body["error"]["message"])
        self.no_backend_started()

    def test_a_bad_token_or_bad_request_answers_in_the_responses_shape(self):
        code, body = self.error_of(request(user("x")), headers=dict(CODEX_HEADERS, authorization="Bearer nope"))
        self.assertEqual((code, sorted(body), body["error"]["type"]), (401, ["error"], "authentication_error"))
        code, body = self.error_of(dict(request(user("x")), model="gpt-6-astra"))
        self.assertEqual((code, body["error"]["type"]), (400, "invalid_request_error"))
        self.assertIn("cortados", body["error"]["message"])
        code, body = self.error_of(b"not json")
        self.assertEqual((code, sorted(body)), (400, ["error"]))
        self.no_backend_started()

    def test_a_json_body_that_is_not_an_object_is_a_400_not_a_dropped_connection(self):
        for raw in (b"[]", b"1", b'"cortados"', b"null"):
            code, body = self.error_of(raw)
            self.assertEqual((code, body["error"]["type"]), (400, "invalid_request_error"), raw)
            self.assertIn("JSON object", body["error"]["message"])
        code, body = self.error_of(b"[]", headers=dict(CODEX_HEADERS, **{"user-agent": "claude-cli/2.1.304"}))
        self.assertEqual(code, 400)                                    # the Messages path answers too
        self.no_backend_started()

    def test_a_bad_history_item_in_a_one_shot_is_a_400_not_a_dropped_connection(self):
        # parse_request validates only the new turn; the one-shot transcript re-reads the history, so a shape error
        # in an older user item surfaces there and must still answer in the wire's error shape.
        code, body = self.error_of(request(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}, {"type": "input_audio", "input_audio": {}}]},
            {"type": "function_call", "call_id": "c1", "name": "Read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "x"},
            user("b"), tools=()))
        self.assertEqual((code, body["error"]["type"]), (400, "invalid_request_error"))
        self.assertIn("input_audio", body["error"]["message"])
        self.no_backend_started()

    def test_the_keepalive_is_an_sse_comment_the_stream_survives(self):
        ping_s = server.PING_S
        # A tool call holds the turn for the sibling-call window (bridge.SETTLE_S, or codex_bridge.CODEX_SETTLE_S as
        # setUp pins it: 0.3 s) before it is returned, so a ping interval far below that puts comment lines between
        # response.created and the items.
        server.PING_S = 0.02
        try:
            _, events, comments = self.stream(request(user("use tool")))
        finally:
            server.PING_S = ping_s
        self.assertGreater(comments, 0)
        self.assertEqual([k for k, _ in events][0], "response.created")
        self.assertEqual([k for k, _ in events][-1], "response.completed")
        self.assertEqual([i["type"] for i in events[-1][1]["response"]["output"]][-1], "function_call")

    def test_the_messages_wire_is_unchanged_beside_it(self):
        headers = dict(CODEX_HEADERS, **{"user-agent": "claude-cli/2.1.304 (external, cli)"})
        with self.send({"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}, headers=headers,
                       path="/v1/messages") as r:
            body = json.loads(r.read())
        self.assertEqual((body["type"], body["content"][0]["text"]), ("message", "echo: hi"))
        code, err = self.error_of(request(user("x")), headers=dict(headers, authorization="Bearer nope"))
        self.assertEqual(code, 401)                                    # the Responses path still answers in its shape
        self.assertEqual(sorted(err), ["error"])


class RoutingBodyTests(unittest.TestCase):
    """What routing.choose is handed for a Responses request: the scored messages and the function tools only."""

    def test_only_function_tools_reach_routing_so_its_standalone_test_matches_the_servers_one_shot(self):
        body = request(user("first"), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"}]},
                       user("second"), tools=({"type": "web_search", "external_web_access": False},))
        routed = server.RESPONSES.routing_body(body)
        self.assertEqual(routed["tools"], [])                          # served as a one-shot, scored as one too
        self.assertEqual(routing.request_text(routed["messages"], standalone=not routed["tools"]), "second")
        routed = server.RESPONSES.routing_body(request(user("first"), tools=(READ_TOOL, {"type": "web_search"})))
        self.assertEqual(routed["tools"], [READ_TOOL])
        self.assertEqual(routed["model"], "cortados")                  # the rest of the body is kept for sizing

    def test_codex_injected_context_is_not_what_s1_scores(self):
        routed = server.RESPONSES.routing_body(json.loads(CAPTURE.read_text()))
        self.assertEqual(routing.request_text(routed["messages"], standalone=not routed["tools"]), "say hi")


class SlowSession:
    """A session whose turn outlives the client: run_turn sleeps, close() is recorded."""
    closed = []

    def __init__(self, reg, *, system, tools, model):
        self.id, self.lock, self.last_used = "slow", threading.Lock(), time.time()
        self.history, self.emitted, self.route, self.route_logged = [], set(), None, False

    def send_user(self, content, timeout):
        pass

    def deliver(self, results):
        pass

    def run_turn(self, timeout):
        time.sleep(0.6)
        return [{"type": "text", "text": "late"}], "end_turn", {"input_tokens": 1, "output_tokens": 1}

    def close(self):
        SlowSession.closed.append(self.id)


class ClientGoneTests(unittest.TestCase):
    def test_a_client_hanging_up_mid_turn_still_closes_a_one_shot_session(self):
        # A one-shot session is not in the registry, so nothing but _messages' own close() reaps its CLI; a ping that
        # hits the closed socket must not skip it.
        SlowSession.closed.clear()
        reg = Registry(gateway_url="", token=TOKEN, session_factories={"claude-cli": SlowSession})
        httpd = ProviderServer(("127.0.0.1", 0), reg)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        ping_s = server.PING_S
        server.PING_S = 0.02
        try:
            body = json.dumps(dict(request(user("title this"), tools=()), stream=True)).encode()
            with socket.create_connection(("127.0.0.1", httpd.server_port), timeout=5) as sock:
                sock.sendall(b"POST /v1/responses HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode() +
                             b"\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() +
                             b"\r\n\r\n" + body)
                self.assertTrue(sock.recv(4096).startswith(b"HTTP/1.1 200"))      # the stream opened
            deadline = time.time() + 5                                             # then the client hung up
            while not SlowSession.closed and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(SlowSession.closed, ["slow"])
        finally:
            server.PING_S = ping_s
            httpd.shutdown()
            httpd.server_close()


class ResponsesOnCodexTests(ResponsesEndpointTests):
    """The same wire on the Codex backend: the Codex callId is the call_id Codex sends back."""
    ROUTE = CODEX_ROUTE

    def test_the_call_id_is_the_app_server_call_id(self):
        _, r1 = self.post(request(user("use tool")))
        [call] = [i for i in r1["output"] if i["type"] == "function_call"]
        self.assertTrue(call["call_id"].startswith("exec-"))
        argv = json.loads(Path(self.codex_argv_file).read_text())["argv"]
        self.assertEqual(argv[0], "app-server")
        self.assertFalse(Path(self.claude_argv_file).exists())

    def test_a_failed_codex_turn_is_response_failed_or_a_502(self):
        _, events, _ = self.stream(request(user("fail")))
        self.assertEqual([k for k, _ in events], ["response.created", "response.failed"])
        failed = events[-1][1]["response"]
        self.assertEqual(failed["status"], "failed")
        self.assertIn("usageLimitExceeded", failed["error"]["message"])
        code, body = self.error_of(request(user("fail")))
        self.assertEqual((code, body["error"]["type"]), (502, "api_error"))
        self.assertIn("usageLimitExceeded", body["error"]["message"])
        self.assertEqual(self.reg.sessions, {})                        # a failed conversation is dropped


if __name__ == "__main__":
    unittest.main()
