"""The provider endpoint end to end over HTTP, with tests/fixtures/fake_claude_cli.py standing in for `claude -p`.

The fake runs the real mcp_shim, so every bridged call crosses the same process and HTTP boundaries as in production.
"""
import json
import os
import sqlite3
import sys
import tempfile
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router.provider import anthropic_wire as wire
from cortados_router.provider.bridge import BridgeError, BridgeSession
from cortados_router import core, scarcity
from cortados_router.provider import server
from cortados_router.provider.server import ProviderServer, Registry

FAKE = Path(__file__).with_name("fixtures") / "fake_claude_cli.py"
TOKEN = "test-token"
READ_TOOL = {"name": "Read", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}


class WireTests(unittest.TestCase):
    def test_tool_results_are_split_from_new_content_and_reminders_ride_on_the_last_result(self):
        turn = wire.parse_request({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "body"},
                                         {"type": "text", "text": "<reminder>"}]}]})
        self.assertEqual(turn.content, [])
        self.assertEqual(turn.tool_results["t1"]["content"],
                         [{"type": "text", "text": "body"}, {"type": "text", "text": "<reminder>"}])
        self.assertEqual(len(turn.history), 1)

    def test_history_is_the_fingerprint_of_each_assistant_message(self):
        turn = wire.parse_request({"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y "},
                                                {"role": "user", "content": "z"}]})
        self.assertEqual(turn.history, [wire.reply_fingerprint([{"type": "text", "text": "y"}])])

    def test_only_auto_tool_choice_is_accepted(self):
        for choice in ({"type": "any"}, {"type": "tool", "name": "Read"}, {"type": "none"}):
            with self.assertRaises(wire.WireError):
                wire.parse_request({"tool_choice": choice, "messages": [{"role": "user", "content": "x"}]})
        wire.parse_request({"tool_choice": {"type": "auto"}, "messages": [{"role": "user", "content": "x"}]})

    def test_mid_conversation_system_messages_join_the_new_turn(self):
        turn = wire.parse_request({"messages": [{"role": "user", "content": "do it"},
                                                {"role": "system", "content": [{"type": "text", "text": "env: x"}]}]})
        self.assertEqual(turn.content, [{"type": "text", "text": "do it"},
                                        {"type": "text", "text": "<system-message>\nenv: x\n</system-message>"}])

    def test_assistant_prefill_is_refused(self):
        with self.assertRaises(wire.WireError):
            wire.parse_request({"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]})

    def test_error_results_and_images_map_to_mcp(self):
        turn = wire.parse_request({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
            "is_error": True, "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}]}]})
        self.assertEqual(turn.tool_results["t"], {"content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}], "isError": True})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.argv_file = os.path.join(self.scratch, "argv.json")
        os.environ["FAKE_CLAUDE_ARGV"] = self.argv_file
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=[sys.executable, str(FAKE)])
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.reg.gateway_url = self.url
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        os.environ.pop("FAKE_CLAUDE_ARGV", None)

    def post(self, body, token=TOKEN, path="/v1/messages"):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if r.headers.get("content-type") == "application/json" else raw

    def status_of(self, body, token=TOKEN):
        try:
            self.post(body, token=token)
            return 200
        except urllib.error.HTTPError as e:
            return e.code

    def test_a_bridged_tool_call_round_trips_through_the_client(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
        self.assertEqual(r1["stop_reason"], "tool_use")
        use = r1["content"][-1]
        self.assertEqual((use["type"], use["name"], use["input"]), ("tool_use", "Read", {"file_path": "f0.md"}))
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        r2 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["stop_reason"], "end_turn")
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        # a plain follow-up continues the same CLI session
        history += [{"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}]
        r3 = self.post({"model": "cortados", "tools": [READ_TOOL], "messages": history})
        self.assertEqual(r3["content"], [{"type": "text", "text": "echo: thanks"}])
        self.assertEqual(len(self.reg.sessions), 1)

    def test_parallel_tool_uses_are_returned_together_and_answered_in_one_request(self):
        history = [{"role": "user", "content": "use two tools"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        uses = [b for b in r1["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(uses), 2)
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "a"},
            {"type": "tool_result", "tool_use_id": uses[1]["id"], "content": "b", "is_error": True}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: a | ERR b"}])

    def test_streaming_response_is_valid_sse(self):
        raw = self.post({"stream": True, "tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool"}]})
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        starts = [e["content_block"] for e in events if e["type"] == "content_block_start"]
        self.assertEqual([b["type"] for b in starts], ["text", "tool_use"])
        deltas = [e["delta"] for e in events if e["type"] == "content_block_delta"]
        self.assertEqual(json.loads(deltas[1]["partial_json"]), {"file_path": "f0.md"})
        self.assertEqual([e for e in events if e["type"] == "message_delta"][0]["delta"]["stop_reason"], "tool_use")

    def test_the_headless_cli_runs_isolated(self):
        os.environ["ANTHROPIC_BASE_URL"] = self.url
        try:
            self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        finally:
            os.environ.pop("ANTHROPIC_BASE_URL")
        seen = json.loads(Path(self.argv_file).read_text())
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__bridge__Read")
        self.assertIsNone(seen["env"]["ANTHROPIC_BASE_URL"])        # would loop back into this endpoint
        self.assertEqual(os.listdir(seen["cwd"]), [])                # empty scratch dir, never the project

    def test_toolless_requests_are_one_shot_and_keep_no_session(self):
        r = self.post({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                    {"role": "user", "content": "title?"}]})
        self.assertTrue(r["content"][0]["text"].startswith("echo: Conversation so far:\nuser: a\nassistant: b"))
        self.assertEqual(self.reg.sessions, {})

    def test_conversations_with_identical_openings_stay_separate(self):
        opening = [{"role": "user", "content": "please use tool"}]
        a = self.post({"tools": [READ_TOOL], "messages": opening})
        b = self.post({"tools": [READ_TOOL], "messages": opening})
        ua, ub = a["content"][-1], b["content"][-1]
        self.assertNotEqual(ua["id"], ub["id"])
        ra = self.post({"tools": [READ_TOOL], "messages": opening + [{"role": "assistant", "content": a["content"]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ua["id"], "content": "from A"}]}]})
        rb = self.post({"tools": [READ_TOOL], "messages": opening + [{"role": "assistant", "content": b["content"]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ub["id"], "content": "from B"}]}]})
        self.assertEqual(ra["content"][0]["text"], "tool said: from A")
        self.assertEqual(rb["content"][0]["text"], "tool said: from B")

    def test_an_ambiguous_history_is_refused_not_guessed(self):
        opening = [{"role": "user", "content": "hi"}]
        a = self.post({"tools": [READ_TOOL], "messages": opening})
        self.post({"tools": [READ_TOOL], "messages": opening})          # same opening, same text reply
        follow = opening + [{"role": "assistant", "content": a["content"]}, {"role": "user", "content": "more"}]
        self.assertEqual(self.status_of({"tools": [READ_TOOL], "messages": follow}), 409)

    def test_a_toolless_call_never_blocks_a_session_waiting_for_a_tool_result(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        history += [{"role": "assistant", "content": r1["content"]}]
        title = self.post({"messages": history + [{"role": "user", "content": "title?"}]})   # while the session waits
        self.assertTrue(title["content"][0]["text"].startswith("echo: Conversation so far:"))
        r2 = self.post({"tools": [READ_TOOL], "messages": history + [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r1["content"][-1]["id"], "content": "ok"}]}]})
        self.assertEqual(r2["content"][0]["text"], "tool said: ok")

    def test_a_continuation_of_an_unknown_conversation_is_a_409_not_a_fresh_start(self):
        body = {"tools": [READ_TOOL], "messages": [{"role": "user", "content": "x"},
                                                   {"role": "assistant", "content": "an answer from before a restart"},
                                                   {"role": "user", "content": "go on"}]}
        self.assertEqual(self.status_of(body), 409)
        self.assertEqual(self.reg.sessions, {})

    def test_results_for_an_unknown_conversation_are_a_409(self):
        body = {"tools": [READ_TOOL], "messages": [
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_gone", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_gone", "content": "x"}]}]}
        self.assertEqual(self.status_of(body), 409)

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(self.status_of({"messages": [{"role": "user", "content": "x"}]}, token="nope"), 401)

    def test_resources_endpoint_returns_a_snapshot(self):
        req = urllib.request.Request(self.url + "/resources", headers={"authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            snap = json.loads(r.read())
        for key in ("usage", "subscriptions", "devices", "generated_at"):
            self.assertIn(key, snap)
        self.assertNotIn("error", snap["usage"])                 # a real summary, not an error section
        self.assertIsInstance(snap["usage"]["total_routes"], int)   # read-only roll-up (count varies with the shared temp home)
        self.assertIn("by_band", snap["usage"])
        self.assertEqual(snap["devices"]["status"], "pending")   # no fleet config here: device telemetry not sampled
        self.assertIsInstance(snap["subscriptions"], dict)

    def test_resources_endpoint_needs_a_token(self):
        req = urllib.request.Request(self.url + "/resources")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(cm.exception.code, 401)

    def test_dashboard_page_is_served_without_a_token(self):
        req = urllib.request.Request(self.url + "/dashboard")
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers.get("content-type", ""))
            body = r.read().decode()
        self.assertIn("CortadOS Router", body)               # the page title/heading
        self.assertIn("/resources", body)                    # it fetches the resources endpoint for its data


class RoutedEndpointTests(EndpointTests):
    """The endpoint with a router: one route per conversation, the route's model on the CLI, execution logged once."""

    def setUp(self):
        super().setUp()
        self.calls, self.executed = [], []

        def router(body, client):
            self.calls.append(client)
            return {"provider": "claude-cli", "model": "claude-test-model", "band": "R2", "route_id": f"r{len(self.calls)}"}

        self.reg.router, self.reg.executed = router, self.executed.append

    def raw(self, body):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              "user-agent": "claude-cli/2.1.304 (external, cli)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers.get("x-cortados-route"), json.loads(r.read())

    def test_a_conversation_is_routed_once_and_runs_on_the_chosen_model(self):
        history = [{"role": "user", "content": "please use tool"}]
        header, r1 = self.raw({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(header, "claude-cli/claude-test-model band=R2 id=r1")
        argv = json.loads(Path(self.argv_file).read_text())["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test-model")
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r1["content"][-1]["id"], "content": "x"}]}]
        self.raw({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(self.calls, ["claude-cli"])                 # not re-routed on the continuation
        self.assertEqual([r["route_id"] for r in self.executed], ["r1"])

    def test_the_turn_outcome_logs_the_delivered_text_size(self):
        logged = threading.Event()
        outcomes = []
        def record(route, chars, turn_ms):
            outcomes.append((route["route_id"], chars, turn_ms)); logged.set()
        self.reg.log_outcome = record
        _, r1 = self.raw({"messages": [{"role": "user", "content": "hello there"}]})   # a plain turn: the fake answers "echo: hello there"
        self.assertTrue(logged.wait(10), "the outcome was never logged (it runs off the critical path)")
        rid, chars, ms = outcomes[0]
        self.assertEqual(rid, "r1")
        expected = sum(len(b["text"]) for b in r1["content"] if b.get("type") == "text")
        self.assertGreater(expected, 0)                              # the reply really had text
        self.assertEqual(chars, expected)                            # the metric is the delivered text size, not a constant
        self.assertIsInstance(ms, int); self.assertGreaterEqual(ms, 0)

    def test_a_failed_execution_log_is_retried_on_the_next_turn(self):
        attempts = []

        def flaky(route):
            attempts.append(route["route_id"])
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
        self.reg.executed = flaky
        history = [{"role": "user", "content": "please use tool"}]
        _, r1 = self.raw({"tools": [READ_TOOL], "messages": history})          # answered despite the failed log
        self.assertEqual(r1["stop_reason"], "tool_use")
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r1["content"][-1]["id"], "content": "x"}]}]
        self.raw({"tools": [READ_TOOL], "messages": history})
        history += [{"role": "assistant", "content": [{"type": "text", "text": "tool said: x"}]}, {"role": "user", "content": "again"}]
        self.raw({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(attempts, ["r1", "r1"])                               # retried once, then never again

    def test_a_router_exception_is_a_503_and_starts_no_cli(self):
        def broken(body, client):
            raise sqlite3.OperationalError("database is locked")
        self.reg.router = broken
        self.assertEqual(self.status_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "x"}]}), 503)
        self.assertEqual(self.reg.sessions, {})
        self.assertFalse(Path(self.argv_file).exists())

    def test_no_available_route_is_a_503_and_starts_no_cli(self):
        self.reg.router = lambda body, client: {"error": "No authenticated, available model fits"}
        self.assertEqual(self.status_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "x"}]}), 503)
        self.assertEqual(self.reg.sessions, {})
        self.assertFalse(Path(self.argv_file).exists())


class StartFailureTests(unittest.TestCase):
    def test_a_cli_that_cannot_start_is_a_bridge_error_and_leaks_no_scratch_dir(self):
        # Registry.new and _messages map BridgeError to 502; an OSError out of popen would instead drop the client's
        # connection without a response and leave the scratch dir and the stderr handle behind.
        scratch = tempfile.mkdtemp()
        with self.assertRaisesRegex(BridgeError, "cannot start .*no-such-claude"):
            BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9", token="t",
                          claude_bin=os.path.join(scratch, "no-such-claude"), scratch_root=scratch)
        self.assertEqual(os.listdir(scratch), [])


class StalledCliTests(unittest.TestCase):
    def test_a_cli_that_stops_reading_input_is_killed_at_the_deadline(self):
        os.environ["FAKE_CLAUDE_STALL"] = "1"
        try:
            s = BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9", token="t",
                              claude_bin=[sys.executable, str(FAKE)])
        finally:
            os.environ.pop("FAKE_CLAUDE_STALL")
        try:
            started = time.time()
            with self.assertRaises(BridgeError):
                s.send_user([{"type": "text", "text": "x" * 1_000_000}], timeout=1)   # far beyond the pipe buffer
            self.assertLess(time.time() - started, 5)
            self.assertIsNotNone(s.proc.wait(timeout=5))
        finally:
            s.close()


# The default login, pinned for the reveal tests: the env a bridged CLI (and the sampler) would see, so the effective login
# a reading is stamped with is predictable whatever the developer's own env holds.
DEFAULT_LOGIN_DIR = "/tmp/cortados-test-default-login"
PINNED_ENV = {"CLAUDE_CONFIG_DIR": DEFAULT_LOGIN_DIR, "CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_API_KEY": "",
              "ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_BASE_URL": "", "CLAUDE_CODE_USE_BEDROCK": "", "CLAUDE_CODE_USE_VERTEX": ""}


def login_of(config_dir=None):
    """The effective login a session on `config_dir` (None: the default login) runs on, as reveal states it."""
    return core.claude_login_identity(server.claude_env(config_dir))


def reading(windows, config_dir=None, age_s=60.0, **kw):
    """A fresh, successful Claude reading sampled on `config_dir`'s effective login (overridable through kw)."""
    now = time.time()
    return {"config_dir": config_dir, "login": login_of(config_dir), "fetched_at": now - age_s, "tried_at": now - age_s,
            "error": None, "windows": windows, **kw}


class RevealTests(unittest.TestCase):
    """A routed subagent sends x-cortados-reveal and is told the real routed model and its lane's remaining window in
    `model`; every other client keeps seeing `cortados`. The scarcity cache is faked: no real ~/.cortados is read."""

    post = EndpointTests.post

    def setUp(self):
        env = mock.patch.dict(os.environ, PINNED_ENV)
        env.start()
        self.addCleanup(env.stop)
        EndpointTests.setUp(self)

    def tearDown(self):
        EndpointTests.tearDown(self)

    def route(self, **kw):
        def router(body, client):
            return {"provider": "claude-cli", "model": "claude-sonnet-5", "band": "R2", "route_id": "r1", **kw}
        self.reg.router, self.reg.executed = router, lambda route: None

    def fake_lanes(self, lanes):     # core reads its own (path-loaded) scarcity module: fake that one
        return mock.patch.object(core._scarcity_module(), "current", lambda *a, **kw: {"fetched_at": time.time(), "lanes": lanes})

    def send(self, body, reveal=True):
        headers = {"content-type": "application/json", "authorization": f"Bearer {TOKEN}"}
        if reveal:
            headers["x-cortados-reveal"] = "1"
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
        if body.get("stream"):
            events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
            self.assertEqual(events[0]["type"], "message_start")
            return events[0]["message"]["model"]
        return json.loads(raw)["model"]

    def claude_lane(self, used_5h=10.0, used_week=42.0):
        now = time.time()
        return {"claude": reading([scarcity.window_view("5-hour", used_5h, now + 3600, 5 * 3600, now),
                                   scarcity.window_view("week", used_week, now + 3 * 86400, 7 * 86400, now)])}

    def test_a_stale_reading_reveals_the_model_alone(self):
        self.route()
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
        lanes = self.claude_lane()
        lanes["claude"].update(fetched_at=time.time() - 7 * 3600, tried_at=time.time() - 7 * 3600)
        with self.fake_lanes(lanes):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.assertEqual(self.send(dict(body, stream=stream)), "claude-sonnet-5")

    def test_reveal_names_the_routed_model_and_its_lanes_remaining_percent(self):
        self.route()
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
        with self.fake_lanes(self.claude_lane()):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.assertEqual(self.send(dict(body, stream=stream)), "claude-sonnet-5 · 58% left")

    def test_without_the_header_the_model_is_exactly_cortados(self):
        self.route()
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
        with self.fake_lanes(self.claude_lane()):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.assertEqual(self.send(dict(body, stream=stream), reveal=False), "cortados")

    def test_reveal_falls_back_to_the_model_alone_when_the_percent_is_unknown(self):
        self.route()
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
        with self.fake_lanes({}):                                     # no reading for the lane
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.assertEqual(self.send(dict(body, stream=stream)), "claude-sonnet-5")
        with mock.patch.object(core._scarcity_module(), "current", side_effect=RuntimeError("cache broken")):
            self.assertEqual(self.send(body), "claude-sonnet-5")

    def test_a_degraded_or_unrouted_turn_keeps_cortados(self):
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
        self.assertEqual(self.send(body), "cortados")                # no router: no route to reveal
        self.route(degraded=True)
        with self.fake_lanes(self.claude_lane()):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    self.assertEqual(self.send(dict(body, stream=stream)), "cortados")

    def test_a_crashing_reveal_never_fails_the_turn(self):
        self.route()
        with mock.patch.object(server, "reveal_model", side_effect=RuntimeError("boom")):
            self.assertEqual(self.send({"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}), "cortados")


class RevealModelTests(unittest.TestCase):
    """reveal_model's lane mapping against a faked scarcity cache (core.headroom_pct_for reads it)."""

    def setUp(self):
        env = mock.patch.dict(os.environ, PINNED_ENV)
        env.start()
        self.addCleanup(env.stop)

    def lanes(self, lanes):
        return mock.patch.object(core._scarcity_module(), "current", lambda *a, **kw: {"lanes": lanes})

    def win(self, name, used, pool=None, reset_in_s=3600, window_s=7 * 86400):
        now = time.time()
        w = scarcity.window_view(name, used, now + reset_in_s, window_s, now)
        return {**w, "pool": pool} if pool else w

    def test_a_per_account_route_reads_its_own_lane(self):
        with self.lanes({"claude": reading([self.win("week", 90)]), "claude#b": reading([self.win("week", 20)], "/b")}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli#b", "model": "claude-sonnet-5", "account": "b",
                                                  "config_dir": "/b"}), "claude-sonnet-5 · 80% left")
            chosen = {"provider": "claude-cli#b", "model": "claude-sonnet-5", "account": "b", "config_dir": "/b",
                      "route": {"chosen": {"lane": "claude#b"}}}
            self.assertEqual(server.reveal_model(chosen), "claude-sonnet-5 · 80% left")

    def test_a_codex_route_names_the_model_only_its_login_cannot_be_confirmed(self):
        # The Codex reading carries no login identity, and the backend may run on a dedicated --codex-home: never show
        # the sampled (possibly the default) login's % for it.
        with self.lanes({"codex": reading([self.win("week", 33.4)])}):
            self.assertEqual(server.reveal_model({"provider": "codex-cli", "model": "gpt-5.5"}), "gpt-5.5")
            chosen = {"provider": "codex-cli", "model": "gpt-5.5", "route": {"chosen": {"lane": "codex"}}}
            self.assertEqual(server.reveal_model(chosen), "gpt-5.5")

    def test_the_percent_is_the_least_remaining_window_not_the_pacing_tightest(self):
        # The week is early and on a hot pace (scarcity 0.9, what _roll binds on) but 50 % left; the 5-hour window is
        # nearly reset at a calm pace yet only 15 % left. "% left" is the minimum: 15 %.
        week = self.win("week", 50, reset_in_s=int(6.5 * 86400))
        five = self.win("5-hour", 85, reset_in_s=60, window_s=5 * 3600)
        self.assertIs(scarcity._roll([week, five])["tightest"], week)
        with self.lanes({"claude": reading([week, five])}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-sonnet-5"}),
                             "claude-sonnet-5 · 15% left")

    def test_a_separately_metered_pool_binds_when_it_is_tighter(self):
        with self.lanes({"claude": reading([self.win("week", 20), self.win("week-fable", 95, pool="fable")])}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-fable-5"}),
                             "claude-fable-5 · 5% left")
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-sonnet-5"}),
                             "claude-sonnet-5 · 80% left")      # a model off the pool is not bound by it

    def test_a_reading_for_another_login_or_a_failed_one_gives_no_percent(self):
        b = {"provider": "claude-cli", "model": "m", "account": "b", "config_dir": "/b"}
        with self.lanes({"claude": reading([self.win("week", 20)], "/a")}):
            self.assertEqual(server.reveal_model(b), "m")
        with self.lanes({"claude": reading([self.win("week", 20)], error="timeout")}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
        failed_since = reading([self.win("week", 20)], age_s=600, error="Timeout", tried_at=time.time() - 30)
        with self.lanes({"claude": failed_since}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")

    def test_a_stale_reading_gives_no_percent(self):
        # Older than the routing view's 6-hour freshness window (scarcity.reading_freshness, as lane_scarcity): unknown
        # to routing, so no % either. Just inside it the % is shown.
        with self.lanes({"claude": reading([self.win("week", 20)], age_s=6 * 3600 + 60)}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
            self.assertIsNone(core._scarcity_module().lane_scarcity(refresh=False)["claude"]["scarcity"])
        with self.lanes({"claude": reading([self.win("week", 20)], age_s=6 * 3600 - 60)}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m · 80% left")
        undated = reading([self.win("week", 20)])
        del undated["fetched_at"]
        with self.lanes({"claude": undated}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")

    def test_an_unconfirmed_effective_login_gives_no_percent(self):
        # Both explicit config_dirs are None, but the sampler ran on another CLAUDE_CONFIG_DIR than the provider's
        # default login would: the None==None match must not show that login's %.
        other = reading([self.win("week", 20)], login=os.path.realpath("/tmp/some-other-login"))
        with self.lanes({"claude": other}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
        # a legacy reading with no recorded login, or one whose login could not be pinned (an ambient token)
        legacy = reading([self.win("week", 20)])
        del legacy["login"]
        for r in (legacy, reading([self.win("week", 20)], login=None)):
            with self.lanes({"claude": r}):
                self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
        # the provider's own env cannot be pinned to a stored login (an ambient OAuth token): no %
        with self.lanes({"claude": reading([self.win("week", 20)])}), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
        # the provider's CLAUDE_CONFIG_DIR changed since the sample: no longer the sampled login
        with self.lanes({"claude": reading([self.win("week", 20)])}), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/cortados-test-elsewhere"}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")

    def test_the_sampler_stamps_the_login_it_effectively_ran_on(self):
        sc = core._scarcity_module()
        self.assertEqual(sc.claude_login_identity({"CLAUDE_CONFIG_DIR": "/tmp/x"}), os.path.realpath("/tmp/x"))
        self.assertEqual(sc.claude_login_identity({"HOME": "/tmp/h"}), os.path.realpath("/tmp/h/.claude"))
        self.assertIsNone(sc.claude_login_identity({"CLAUDE_CONFIG_DIR": "/tmp/x", "ANTHROPIC_API_KEY": "k"}))
        # a RELATIVE config dir resolves against each process's cwd (sampler vs bridge differ) -> no pinned login
        self.assertIsNone(sc.claude_login_identity({"CLAUDE_CONFIG_DIR": "../../login"}))
        self.assertIsNone(sc.claude_login_identity({"CLAUDE_CONFIG_DIR": "rel/login"}))
        seen = {}

        def capture(argv, command, done, env, **kw):
            seen["env"] = dict(env)
            return "Current session 10% used\nCurrent week (all models) 20% used"
        with mock.patch.object(sc, "tui_capture", capture), \
                mock.patch.object(sc, "parse_claude_usage", lambda t: [{**self.win("week", 20), "pool": "all"}]):
            self.assertEqual(sc._claude(None, None)["login"], sc.claude_login_identity(seen["env"]))
            self.assertEqual(sc._claude(None, "/tmp/acct-b")["login"], os.path.realpath("/tmp/acct-b"))

    def test_a_missing_separate_pool_reading_gives_no_percent(self):
        # Fable is metered on its own pool; with its per-model panel rate-limited (no fable window, or only one carried
        # forward from an earlier read) the general windows alone cannot bound it: the model name alone.
        fable = {"provider": "claude-cli", "model": "claude-fable-5"}
        with self.lanes({"claude": reading([self.win("week", 20)])}):
            self.assertEqual(server.reveal_model(fable), "claude-fable-5")
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-sonnet-5"}),
                             "claude-sonnet-5 · 80% left")
        carried = reading([self.win("week", 20), {**self.win("week-fable", 30, pool="fable"), "stale": True}],
                          per_model="not shown this read (vendor rate limit on the per-model panel)")
        with self.lanes({"claude": carried}):
            self.assertEqual(server.reveal_model(fable), "claude-fable-5")
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-sonnet-5"}),
                             "claude-sonnet-5 · 80% left")      # off the pool: the fresh general windows still count
        chosen = {"provider": "claude-cli", "model": "claude-opus-5", "route": {"chosen": {"lane": "claude", "pool": "opus"}}}
        with self.lanes({"claude": reading([self.win("week", 20)])}):
            self.assertEqual(server.reveal_model(chosen), "claude-opus-5")
        # a reading with ONLY a per-model (fable) window and NO general window: shared session/weekly capacity is
        # unknown, so even the fable model gets its name alone, never a % from the pool window in isolation (finding 3)
        with self.lanes({"claude": reading([self.win("week-fable", 10, pool="fable")])}):
            self.assertEqual(server.reveal_model(fable), "claude-fable-5")

    def test_a_dropped_per_model_pool_is_carried_forward_when_another_pool_shows(self):
        # finding 2: one per-model panel (Fable) shows this read but another (Opus) is rate-limited. The sampler must
        # carry the dropped Opus pool forward (stale), so that model is still known to HAVE a separate pool and its %
        # is withheld — while a general-pool model (Sonnet) still shows its %.
        sc = core._scarcity_module()
        prev = {"config_dir": None, "windows": [self.win("week", 20), self.win("week-opus", 99, pool="opus")]}
        with mock.patch.object(sc, "tui_capture", lambda *a, **k: "Current week (all models) 20% used\nCurrent week (Fable) 10% used"), \
                mock.patch.object(sc, "parse_claude_usage", lambda _t: [self.win("week", 20), self.win("week-fable", 10, pool="fable")]):
            read = sc._claude(prev, None)
        opus = next((w for w in read["windows"] if w.get("pool") == "opus"), None)
        self.assertIsNotNone(opus)                       # the dropped Opus pool is carried forward, not forgotten
        self.assertTrue(opus.get("stale"))               # marked stale: known to exist, but not this read's number
        self.assertIn("per_model", read)
        # stamp the freshness/login fields the cache layer adds around the sampler, then check reveal on the result
        read.update({"fetched_at": time.time(), "tried_at": time.time(), "error": None, "login": login_of(None)})
        with self.lanes({"claude": read}):
            opus_route = {"provider": "claude-cli", "model": "claude-opus-5", "route": {"chosen": {"lane": "claude", "pool": "opus"}}}
            self.assertEqual(server.reveal_model(opus_route), "claude-opus-5")                 # withheld: stale Opus pool
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "claude-sonnet-5"}),
                             "claude-sonnet-5 · 80% left")                                     # general pool: still shown

    def test_the_default_login_is_an_identity_of_its_own(self):
        # A default-login route (no account) must not show a reading sampled for a named account, nor a named route the
        # default login's reading; only a matching identity is trusted. A route config_dir without an account runs on
        # the default login (Registry.new), so it is the default login's reading that applies.
        a = {"provider": "claude-cli", "model": "m", "account": "a", "config_dir": "/a"}
        with self.lanes({"claude": reading([self.win("week", 20)], "/a")}):
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m")
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m", "config_dir": "/a"}), "m")
            self.assertEqual(server.reveal_model(a), "m · 80% left")
        with self.lanes({"claude": reading([self.win("week", 20)])}):
            self.assertEqual(server.reveal_model(a), "m")
            self.assertEqual(server.reveal_model({"provider": "claude-cli", "model": "m"}), "m · 80% left")

    def test_the_api_lane_suffix_reads_its_dollar_meter(self):
        from cortados_router import api_credits
        with mock.patch.object(api_credits, "window_numbers", lambda now: (12.6, now + 1, 2.0)):
            self.assertEqual(server.api_reveal_suffix(), " · 87% left")
        with mock.patch.object(api_credits, "window_numbers", side_effect=api_credits.LedgerError("down")):
            self.assertIsNone(server.api_reveal_suffix())
        # a route never draws on the api lane through S1: no lane, so the model alone
        self.assertEqual(server.reveal_model({"provider": "anthropic-api", "model": "claude-opus-5-5"}), "claude-opus-5-5")

    def test_no_route_or_a_degraded_one_reveals_nothing(self):
        for route in (None, {}, {"provider": "claude-cli", "model": "m", "degraded": True},
                      {"error": "no route"}):
            self.assertIsNone(server.reveal_model(route))


if __name__ == "__main__":
    unittest.main()


class MessagesWireFoldTests(unittest.TestCase):
    def test_text_and_images_beside_tool_results_ride_on_the_last_result(self):
        # Claude Code can send a pasted screenshot in the same message as the tool results; the CLI only receives
        # that message through the pending MCP calls, so every block rides on the last result, images included.
        from cortados_router.provider import anthropic_wire
        tool = {"name": "Read", "description": "r", "input_schema": {"type": "object"}}
        body = {"model": "cortados", "tools": [tool], "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "body"},
                                         {"type": "text", "text": "<reminder>"},
                                         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]}]}
        turn = anthropic_wire.parse_request(body)
        self.assertEqual(turn.content, [])
        self.assertEqual(turn.tool_results["t1"]["content"], [{"type": "text", "text": "body"}, {"type": "text", "text": "<reminder>"},
                                                              {"type": "image", "data": "QUJD", "mimeType": "image/png"}])

