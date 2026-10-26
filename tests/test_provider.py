"""The provider endpoint end to end over HTTP, with tests/fixtures/fake_claude_cli.py standing in for `claude -p`.

The fake runs the real mcp_shim, so every bridged call crosses the same process and HTTP boundaries as in production.
"""
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
from pathlib import Path

from cortados_router.provider import anthropic_wire as wire
from cortados_router.provider.bridge import BridgeError, BridgeSession
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

