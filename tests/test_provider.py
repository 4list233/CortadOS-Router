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
import uuid
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
    def test_cache_metadata_is_not_application_data_and_request_is_not_mutated(self):
        block = {"type": "tool_use", "input": {"cache_control": "application data"},
                 "cache_control": {"type": "ephemeral"}}
        clean = wire.without_cache_control(block)
        self.assertNotIn("cache_control", clean)
        self.assertEqual(clean["input"], block["input"])
        self.assertIn("cache_control", block)
        tool = dict(READ_TOOL, cache_control={"type": "ephemeral"})
        turn = wire.parse_request({"tools": [tool], "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("cache_control", turn.tools[0])
        self.assertIn("cache_control", tool)


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

    def test_a_tool_name_with_a_lone_surrogate_is_a_wire_error(self):
        # json.loads accepts "\\ud800" and yields a str UTF-8 cannot encode; it must be refused here, not crash later.
        body = json.loads('{"tools": [{"name": "Re\\ud800d", "input_schema": {}}], "messages": [{"role": "user", "content": "x"}]}')
        with self.assertRaisesRegex(wire.WireError, r"tool name .*not valid Unicode"):
            wire.parse_request(body)
        # Non-ASCII beside the surrogate (seen in review: "é\ud800" crashed the old message formatting).
        mixed = json.loads('{"tools": [{"name": "\\u00e9\\ud800", "input_schema": {}}], "messages": [{"role": "user", "content": "x"}]}')
        with self.assertRaisesRegex(wire.WireError, r"tool name .*not valid Unicode"):
            wire.parse_request(mixed)

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


class EndpointHarness(unittest.TestCase):
    """The provider endpoint over the fake CLI: setUp/tearDown and request helpers only, no tests (so a subclass
    reuses the harness without re-running inherited tests)."""

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


class EndpointTests(EndpointHarness):
    def test_client_cache_markers_are_removed_on_every_claude_path(self):
        def blocks(text):
            return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral", "ttl": "5m"}},
                    {"type": "text", "text": "context", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        # Single-message one-shot, multi-message replay, first persistent turn, and continuation.
        for messages in ([{"role": "user", "content": blocks("hello")}],
                         [{"role": "user", "content": blocks("old")},
                          {"role": "assistant", "content": blocks("reply")},
                          {"role": "user", "content": blocks("new")}]):
            self.assertEqual(self.post({"messages": messages})["stop_reason"], "end_turn")
        history = [{"role": "user", "content": blocks("use tool")}]
        first = self.post({"tools": [READ_TOOL], "messages": history})
        use = first["content"][-1]
        history += [{"role": "assistant", "content": first["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        "content": blocks("result") + [{"type": "document",
                            "cache_control": {"type": "ephemeral"}, "content": blocks("nested")}]}]}]
        self.assertEqual(self.post({"tools": [READ_TOOL], "messages": history})["stop_reason"], "end_turn")


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

    def test_a_garbled_tool_name_is_skipped_not_fatal(self):
        history = [{"role": "user", "content": "garbled"}]
        with self.assertLogs("cortados_router.provider.bridge", "WARNING") as logs:
            r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertIn("mcp__bashbridge__Bash", "\n".join(logs.output))
        self.assertEqual(r1["stop_reason"], "end_turn")
        self.assertEqual(r1["content"], [{"type": "text", "text": "echo: garbled"}])
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "thanks"}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})     # the conversation lives on
        self.assertEqual(r2["content"], [{"type": "text", "text": "echo: thanks"}])

    def test_only_the_bridged_tool_use_after_a_garbled_one_is_forwarded(self):
        history = [{"role": "user", "content": "garbled, then use tool"}]
        with self.assertLogs("cortados_router.provider.bridge", "WARNING"):
            r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r1["stop_reason"], "tool_use")
        uses = [b for b in r1["content"] if b["type"] == "tool_use"]
        self.assertEqual([(u["name"], u["input"]) for u in uses], [("Read", {"file_path": "f0.md"})])
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "hi"}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hi"}])

    def test_a_wrong_tool_under_the_bridge_prefix_is_skipped_too(self):
        history = [{"role": "user", "content": "misnamed, then use tool"}]
        with self.assertLogs("cortados_router.provider.bridge", "WARNING") as logs:
            r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertIn("mcp__bridge__Bsh", "\n".join(logs.output))
        self.assertEqual(r1["stop_reason"], "tool_use")
        uses = [b for b in r1["content"] if b["type"] == "tool_use"]
        self.assertEqual([u["name"] for u in uses], ["Read"])
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "hi"}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual((r2["stop_reason"], r2["content"]), ("end_turn", [{"type": "text", "text": "tool said: hi"}]))

    def test_a_skipped_tool_use_the_cli_ran_itself_fails_the_conversation(self):
        body = {"tools": [READ_TOOL], "messages": [{"role": "user", "content": "garbled but ran locally"}]}
        with self.assertLogs("cortados_router.provider.bridge", "WARNING"):
            self.assertEqual(self.status_of(body), 502)

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


class RoutedHarness(EndpointHarness):
    """The endpoint with a stub router (every route is claude-cli/claude-test-model); harness only, no tests."""

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

    def test_a_lone_surrogate_tool_name_is_a_400_not_a_dropped_connection(self):
        body = {"model": "cortados", "tools": [{"name": "Re\ud800d", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "x"}]}
        self.assertEqual(self.status_of(body), 400)


class RoutedEndpointTests(RoutedHarness, EndpointTests):
    """The endpoint with a router: one route per conversation, the route's model on the CLI, execution logged once."""

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


class RunRecordTests(RoutedHarness):
    """x-cortados-run-id: each routed request of a client run is recorded; GET /v1/cortados/runs/<id> names the model."""

    def send(self, body, run_id, token=TOKEN):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}",
                                              "x-cortados-run-id": run_id})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]

    def lookup(self, run_id, token=TOKEN):
        req = urllib.request.Request(self.url + "/v1/cortados/runs/" + run_id,
                                     headers={"authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_malformed_run_id_is_a_400_before_routing(self):
        for bad in ("short", "x" * 65, "has space1", "semi;colon1", "dots.dots.dots"):
            code, err = self.send({"messages": [{"role": "user", "content": "hi"}]}, bad)
            self.assertEqual((code, err["type"]), (400, "invalid_request_error"), bad)
            self.assertEqual(err["message"], "cortados: x-cortados-run-id is not a run id")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.lookup("short")[0], 400)

    def test_a_routed_request_is_retrievable_by_run_id(self):
        code, _ = self.send({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool"}]},
                            "run_abc-123")
        self.assertEqual(code, 200)
        code, got = self.lookup("run_abc-123")
        self.assertEqual(code, 200)
        self.assertEqual(got, {"run_id": "run_abc-123", "model": "claude-test-model", "label": "claude-test-model",
                               "provider": "claude-cli", "models": ["claude-test-model"],
                               "builder_models": ["claude-test-model"]})

    def test_the_primary_is_the_first_request_with_tools(self):
        models = iter(["title-model", "worker-model", "other-model"])
        self.reg.router = lambda body, client: {"provider": "claude-cli", "model": next(models), "band": "R2",
                                                "route_id": "r"}
        self.send({"messages": [{"role": "user", "content": "a title"}]}, "run-primary")   # tool-less one-shot
        self.send({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool"}]}, "run-primary")
        self.send({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool again"}]},
                  "run-primary")
        code, got = self.lookup("run-primary")
        self.assertEqual(code, 200)
        self.assertEqual(got["model"], "worker-model")
        self.assertEqual(got["models"], ["title-model", "worker-model", "other-model"])
        # the tool-less title call wrote nothing: it is in `models` (compatibility) but not a builder
        self.assertEqual(got["builder_models"], ["worker-model", "other-model"])

    def test_a_run_of_only_toolless_calls_has_no_builder_models(self):
        self.send({"messages": [{"role": "user", "content": "a title"}]}, "run-titles")
        code, got = self.lookup("run-titles")
        self.assertEqual((code, got["models"], got["builder_models"]), (200, ["claude-test-model"], []))

    def test_a_continuation_under_a_second_run_id_adds_no_entry(self):
        history = [{"role": "user", "content": "please use tool"}]
        code, r1 = self.send({"tools": [READ_TOOL], "messages": history}, "run-opener")
        self.assertEqual((code, r1["stop_reason"]), (200, "tool_use"))
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r1["content"][-1]["id"], "content": "x"}]}]
        code, r2 = self.send({"tools": [READ_TOOL], "messages": history}, "run-continuer")
        self.assertEqual((code, r2["content"][0]["text"]), (200, "tool said: x"))
        self.assertEqual(self.lookup("run-continuer")[0], 404)        # routed when it opened, not by this request
        self.assertEqual(len(self.reg.runs._runs["run-opener"]), 1)

    def test_a_backend_start_failure_leaves_no_record(self):
        self.reg.claude_bin = [os.path.join(self.scratch, "no-such-claude")]
        code, err = self.send({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool"}]},
                              "run-nostart")
        self.assertEqual((code, err["type"]), (502, "api_error"))
        self.assertEqual(len(self.calls), 1)                          # it was routed, then the backend failed
        self.assertEqual(self.lookup("run-nostart")[0], 404)

    def test_the_api_lane_records_its_label(self):
        from types import SimpleNamespace
        from cortados_router import api_credits
        from cortados_router.provider.server import Handler, RunRecords
        rid = api_credits._org_id("sk") + "|2026-11:abc"     # resolves to the test key "sk" (as in test_api_lane)
        runs = RunRecords()

        def serve(headers, body):
            h = Handler.__new__(Handler)
            h.headers, h.server = headers, SimpleNamespace(registry=SimpleNamespace(runs=runs))
            h._error = lambda *a: self.fail(f"refused: {a}")
            with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk"}), \
                 mock.patch("cortados_router.api_credits.reserve", return_value=rid), \
                 mock.patch("cortados_router.api_credits.window_numbers", lambda now: (40.4, now + 1, 2.0)), \
                 mock.patch("cortados_router.provider.anthropic_proxy.serve", return_value=({}, "rejected")), \
                 mock.patch("cortados_router.api_credits.settle"):
                h._serve_api_lane(body, "claude-opus-5-5", None)

        serve({"x-cortados-run-id": "run-api-reveal", "x-cortados-reveal": "1"},
              {"model": "api/claude-opus-5-5", "tools": [READ_TOOL]})
        self.assertEqual(runs.summary("run-api-reveal"),
                         {"run_id": "run-api-reveal", "model": "claude-opus-5-5", "label": "claude-opus-5-5 · 60% left",
                          "provider": "anthropic-api", "models": ["claude-opus-5-5"],
                          "builder_models": ["claude-opus-5-5"]})
        serve({"x-cortados-run-id": "run-api-plain"}, {"model": "api/claude-opus-5-5"})   # no reveal, no tools
        got = runs.summary("run-api-plain")
        self.assertEqual((got["label"], got["provider"], got["builder_models"]),
                         ("claude-opus-5-5", "anthropic-api", []))
        serve({}, {"model": "api/claude-opus-5-5"})                                         # no run id: nothing kept
        self.assertEqual(len(runs._runs), 2)

    def test_a_full_run_still_keeps_a_later_tool_bearing_entry(self):
        from cortados_router.provider.server import RunRecords
        runs = RunRecords()
        for i in range(RunRecords.PER_RUN + 5):                    # tool-less side calls fill the run, then overflow
            runs.record("run-crowded", provider="p", model=f"side{i}", label="x", tools=False)
        runs.record("run-crowded", provider="p", model="builder", label="builder-label", tools=True)
        entries = runs._runs["run-crowded"]
        self.assertEqual(len(entries), RunRecords.PER_RUN)
        self.assertEqual([e["model"] for e in entries[:2]], ["side1", "side2"])   # the oldest tool-less one went
        got = runs.summary("run-crowded")
        self.assertEqual((got["model"], got["label"], got["builder_models"]), ("builder", "builder-label", ["builder"]))
        # further tool-less calls are dropped, never displacing the builder
        runs.record("run-crowded", provider="p", model="late-side", label="x", tools=False)
        self.assertNotIn("late-side", runs.summary("run-crowded")["models"])
        self.assertEqual(runs.summary("run-crowded")["builder_models"], ["builder"])
        # a run already full of tool-bearing entries keeps its first PER_RUN
        for i in range(RunRecords.PER_RUN + 3):
            runs.record("run-builders", provider="p", model=f"b{i}", label="x", tools=True)
        self.assertEqual(runs.summary("run-builders")["builder_models"], [f"b{i}" for i in range(RunRecords.PER_RUN)])

    def test_an_unknown_run_is_a_404(self):
        code, err = self.lookup("never-seen-run")
        self.assertEqual(code, 404)
        self.assertEqual(err["error"]["type"], "not_found_error")

    def test_the_run_lookup_needs_the_token(self):
        self.send({"messages": [{"role": "user", "content": "hi"}]}, "run-authed")
        self.assertEqual(self.lookup("run-authed", token="wrong")[0], 401)
        self.assertEqual(self.lookup("run-authed")[0], 200)

    def test_the_records_are_bounded(self):
        from cortados_router.provider.server import RunRecords
        runs = RunRecords()
        for i in range(RunRecords.MAX_RUNS + 10):
            runs.record(f"run-{i:05d}", provider="p", model="m", label="m", tools=False)
        self.assertIsNone(runs.summary("run-00000"))
        self.assertIsNotNone(runs.summary(f"run-{RunRecords.MAX_RUNS + 9:05d}"))
        self.assertEqual(len(runs._runs), RunRecords.MAX_RUNS)
        # a lookup refreshes recency: the oldest run, once looked up, survives the next evictions
        runs.summary("run-00010")
        for i in range(40):
            runs.record("run-many", provider="p", model=f"m{i}", label="x", tools=False)   # evicts run-00011
        self.assertEqual(len(runs._runs["run-many"]), RunRecords.PER_RUN)
        runs.record("run-newest", provider="p", model="m", label="m", tools=False)          # evicts run-00012
        self.assertIsNotNone(runs.summary("run-00010"))
        self.assertIsNone(runs.summary("run-00011"))
        self.assertIsNone(runs.summary("run-00012"))


class FailedSessionTombstoneTests(EndpointHarness):
    """A conversation dropped because its backend failed answers a retry of the failed request with the real reason
    (502 api_error), not the 409 of a conversation this endpoint never held."""

    def failed_conversation(self):
        """Open a conversation, fail its second turn in the backend, and return the failed request's body."""
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "backend failure"}]
        body = {"tools": [READ_TOOL], "messages": history}
        with self.assertLogs("cortados.provider", "WARNING"):
            self.assertEqual(self.status_of(body), 502)
        self.assertEqual(self.reg.sessions, {})                        # the failed conversation was dropped
        return body

    def error_of(self, body):
        try:
            self.post(body)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_retry_of_the_failed_request_gets_the_original_reason_not_a_409(self):
        body = self.failed_conversation()
        for _ in range(2):                                              # every retry, not only the first
            code, err = self.error_of(body)
            self.assertEqual((code, err["error"]["type"]), (502, "api_error"))
            msg = err["error"]["message"]
            self.assertIn("cache TTL ordering", msg)                   # the backend's own reason
            self.assertRegex(msg, r"^cortados: conversation [0-9a-f]{8} failed at \d\d:\d\d:\d\d: .*start a new conversation$")
            self.assertNotIn("no longer holds", msg)
        self.assertEqual(self.reg.sessions, {})                        # a tombstone never restarts a backend

    def test_a_streaming_retry_gets_the_reason_as_an_sse_error(self):
        body = self.failed_conversation()
        raw = self.post(dict(body, stream=True))
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["message_start", "error"])
        self.assertEqual(events[-1]["error"]["type"], "api_error")
        self.assertIn("cache TTL ordering", events[-1]["error"]["message"])

    def test_a_genuinely_unknown_history_is_still_a_409(self):
        self.failed_conversation()
        body = {"tools": [READ_TOOL], "messages": [{"role": "user", "content": "x"},
                                                   {"role": "assistant", "content": "an answer from elsewhere"},
                                                   {"role": "user", "content": "go on"}]}
        code, err = self.error_of(body)
        self.assertEqual(code, 409)
        self.assertIn("no longer holds", err["error"]["message"])

    def test_the_tombstone_expires_after_its_ttl(self):
        body = self.failed_conversation()
        later = time.time() + server.TOMBSTONE_TTL_S + 1
        with mock.patch.object(server.time, "time", return_value=later):
            self.assertEqual(self.error_of(body)[0], 409)
        self.assertEqual(self.reg.tombstones, {})                      # pruned, not only ignored

    def test_the_store_keeps_only_the_newest_entries(self):
        class Dead:
            def __init__(self, i):
                self.id, self.history = f"{i:08x}-dead", [f"fp-{i}"]
            def close(self):
                pass
        with mock.patch.object(server, "TOMBSTONE_MAX", 3):
            for i in range(5):
                self.reg.fail(Dead(i), "x" * 10_000 + TOKEN)
        self.assertEqual(list(self.reg.tombstones), [("fp-2",), ("fp-3",), ("fp-4",)])
        reason = self.reg.tombstones[("fp-4",)][0]
        self.assertLessEqual(len(reason), server.TOMBSTONE_REASON_CHARS)
        self.reg.fail(Dead(9), "boom " + TOKEN)
        self.assertNotIn(TOKEN, self.reg.tombstones[("fp-9",)][0])     # the endpoint token never lands in one

    def test_an_idle_closed_conversation_leaves_no_tombstone(self):
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        [s] = self.reg.sessions.values()
        s.last_used = time.time() - 10_000
        self.reg.reap(1)
        self.assertEqual((self.reg.sessions, dict(self.reg.tombstones)), ({}, {}))
        follow = history + [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "more"}]
        self.assertEqual(self.status_of({"tools": [READ_TOOL], "messages": follow}), 409)


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
# Blank EVERY login override scarcity knows (not a hand-copied subset): a test run from inside a routed Claude Code
# session inherits e.g. ANTHROPIC_CUSTOM_HEADERS (the x-cortados-reveal header), which unpins the login -> no % left.
PINNED_ENV = {"CLAUDE_CONFIG_DIR": DEFAULT_LOGIN_DIR,
              **{k: "" for k in core._scarcity_module()._LOGIN_OVERRIDES}}


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
        # any auth/transport override the mod blanks also unpins the login (a custom x-api-key header, an alt provider)
        for override in ("ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
                         "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_USE_ANTHROPIC_AWS"):
            self.assertIsNone(sc.claude_login_identity({"CLAUDE_CONFIG_DIR": "/tmp/x", override: "1"}))
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



class WholeSessionTests(EndpointHarness):
    """Whole-session mode on PRODUCTION routing: the router is server.serve's (routing.choose over the real core and
    tables), with only S1 (core.decide), lane health, the route-log state dir and the backend CLI faked. One Claude
    Code session sends its main loop as `sub/<fable>` and its native subagents as `cortados`."""

    def setUp(self):
        super().setUp()
        from cortados_router.provider import routing
        from cortados_router.session import ORCHESTRATORS
        self.fable = ORCHESTRATORS["fable"]
        keep = (core.STATE, core.lane_health, core.decide)
        self.addCleanup(lambda: (setattr(core, "STATE", keep[0]), setattr(core, "lane_health", keep[1]),
                                 setattr(core, "decide", keep[2])))
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-whole-session-"))   # never the live router.db
        core.lane_health = lambda: {}
        routing.execution_routing._scores.clear()
        self.scored, lock = [], threading.Lock()
        easy = {"steps": 0, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
                "outward": False, "needs_clarification": False, "diversity": False, "work_kind": "code",
                "urgency": "now", "parts": 0}

        def fake_s1(text, ctx=None):
            with lock:
                self.scored.append(text)
            return {"answers": dict(easy), "degraded": False, "engine": "s1-test", "engine_ms": 1}
        core.decide = fake_s1

        def router(body, client, exclude_model=None):     # as server.serve builds it; Codex off so routes stay Claude
            return routing.choose(core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin="python3",
                                  codex_bin="no-such-codex-cli", exclude_model=exclude_model)
        self.executed = []
        self.reg.router, self.reg.executed = router, self.executed.append
        self.tools_dir = os.path.join(self.scratch, "tools")
        os.makedirs(self.tools_dir)
        os.environ["FAKE_CLAUDE_TOOLS_DIR"] = self.tools_dir
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_TOOLS_DIR", None)

    def send(self, body):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              "user-agent": "claude-cli/2.1.316 (external, cli)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.headers.get("x-cortados-route"), json.loads(r.read())

    def test_main_loop_pin_and_concurrent_subagents_on_production_routing(self):
        mcp_tool = dict(READ_TOOL, name="mcp__srv__tool")
        # Main loop: sub/<fable> runs Fable and S1 is never asked.
        header, main = self.send({"model": self.fable, "tools": [READ_TOOL], "messages": [
            {"role": "user", "content": "plan the work"}]})
        self.assertTrue(header.startswith("claude-cli/claude-fable-5-1"), header)
        self.assertEqual(main["content"], [{"type": "text", "text": "echo: plan the work"}])
        self.assertEqual(self.scored, [])

        # Two native subagents (`cortados`), started together, each with its own first message and an MCP tool.
        firsts = [f"subtask {i}: please use tool" for i in range(2)]
        bodies = {i: {"model": "cortados", "tools": [mcp_tool, READ_TOOL],
                      "messages": [{"role": "user", "content": firsts[i]}]} for i in range(2)}
        first, second, errors = {}, {}, []
        gate = threading.Barrier(2)

        def subagent(i):
            try:
                gate.wait(10)
                first[i] = self.send(bodies[i])
                use = first[i][1]["content"][-1]
                history = bodies[i]["messages"] + [
                    {"role": "assistant", "content": first[i][1]["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                                  "content": f"result for {i}"}]}]
                gate.wait(10)                                   # both continuations in flight at once
                second[i] = self.send(dict(bodies[i], messages=history))
            except Exception as exc:                            # surfaced below, not lost in the thread
                errors.append(exc)
                gate.abort()

        threads = [threading.Thread(target=subagent, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(first), [0, 1])
        self.assertEqual(sorted(second), [0, 1])

        # S1 scored each subagent exactly once (the continuations are not new conversations), on its own text.
        self.assertEqual(len(self.scored), 2, self.scored)
        for f in firsts:
            self.assertEqual(sum(f in t for t in self.scored), 1, (f, self.scored))
        for i in range(2):
            header, r1 = first[i]
            self.assertTrue(header.startswith("claude-cli/"), header)
            self.assertFalse(header.startswith("claude-cli/claude-fable-5-1"), header)
            self.assertEqual(r1["stop_reason"], "tool_use")
            uses = [b for b in r1["content"] if b["type"] == "tool_use"]
            self.assertEqual([u["name"] for u in uses], ["mcp__srv__tool"])   # back to the client by its own name
            # The continuation reached its own conversation's tool call, not the other subagent's.
            self.assertEqual(second[i][1]["stop_reason"], "end_turn")
            self.assertEqual(second[i][1]["content"], [{"type": "text", "text": f"tool said: result for {i}"}])

        # Three conversations, three backends, three routes (one pinned, two S1-routed).
        self.assertEqual(len(self.reg.sessions), 3)
        self.assertEqual(len({s.route["route_id"] for s in self.reg.sessions.values()}), 3)
        self.assertEqual(sorted(bool(s.route.get("pinned")) for s in self.reg.sessions.values()), [False, False, True])
        # The MCP tool name reached each subagent backend unchanged (the bridge lists it as-is, first).
        seen = [json.loads(Path(self.tools_dir, f).read_text()) for f in sorted(os.listdir(self.tools_dir))]
        self.assertEqual(len(seen), 3)
        sub_tools = [x["tools"] for x in seen if "mcp__srv__tool" in x["tools"]]
        self.assertEqual(len(sub_tools), 2)
        for tools in sub_tools:
            self.assertEqual(tools[0], "mcp__srv__tool")
