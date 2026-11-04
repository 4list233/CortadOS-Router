"""The provider endpoint end to end over HTTP, with tests/fixtures/fake_claude_cli.py standing in for `claude -p`.

The fake runs the real mcp_shim, so every bridged call crosses the same process and HTTP boundaries as in production.
"""
import json
import os
import sqlite3
import subprocess
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
from cortados_router.provider import search_lane, server
from cortados_router.provider.server import ProviderServer, Registry

FAKE = Path(__file__).with_name("fixtures") / "fake_claude_cli.py"
TOKEN = "test-token"
RUNS_PATH_FOR_TESTS = "/v1/cortados/runs/"
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


class ConversationDigestRobustnessTests(unittest.TestCase):
    """The tombstone digest reads earlier messages the wire never used to parse; it must not refuse or crash on them."""

    def test_malformed_earlier_user_content_is_still_served(self):
        def turn(content):
            return wire.parse_request({"messages": [
                {"role": "user", "content": content}, {"role": "assistant", "content": "a"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": content}]},
                {"role": "assistant", "content": "b"}, {"role": "user", "content": "next"}]})
        clean = turn("hi")
        digests = {turn(bad).prefix_digest for bad in (None, 42, {"x": 1})}
        self.assertEqual(len(digests), 3)
        self.assertNotIn(clean.prefix_digest, digests)
        self.assertEqual(turn(42).history, clean.history)
        with self.assertRaises(wire.WireError):                 # the new turn itself is still checked
            wire.parse_request({"messages": [{"role": "user", "content": 42}]})

    def nested(self, depth, leaf="x"):
        content: object = leaf
        for i in range(depth):
            content = [{"type": "tool_result", "tool_use_id": f"t{i}", "content": content}]
        return content

    def test_reminder_stripping_is_linear_and_matches_the_span_rule(self):
        import random, re, time
        # Quadratic before (a regex rescanning to the end for each unclosed opening): 32 000 unclosed openings in
        # ~750 KB took 55 s. The scan must stay fast and keep the old rule's result.
        poisoned = ("<system-reminder> " + "y" * 6) * 32000
        t0 = time.time()
        wire.conversation_digest([{"role": "user", "content": poisoned}])
        self.assertLess(time.time() - t0, 2.0)
        old = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
        parts = ["<system-reminder>", "</system-reminder>", "<system-", "reminder>", " ", "\n", "a", "b"]
        rng = random.Random(7)
        for _ in range(5000):
            text = "".join(rng.choice(parts) for _ in range(rng.randint(0, 12)))
            self.assertEqual(wire.canonical_text(text), " ".join(old.sub(" ", text).split()), text)

    def test_deep_nesting_never_raises_recursion_error(self):
        # The deepest tool_result nesting json.loads accepts here (about 490 levels, ~980 JSON levels): the deepest
        # body the server can ever hand to parse_request. Built as text, since json.dumps has the same limit.
        def text(depth):
            head = "".join('[{"type": "tool_result", "tool_use_id": "t", "content": ' for _ in range(depth))
            return ('{"messages": [{"role": "user", "content": ' + head + '"x"' + "}]" * depth + '}, '
                    '{"role": "assistant", "content": "a"}, {"role": "user", "content": "next"}]}')
        depth, body = 495, None
        while body is None:
            try:
                body = json.loads(text(depth))
            except RecursionError:
                depth -= 5
        self.assertGreater(depth, 400)

        def from_deeper_stack(frames, b):           # a handler thread calls parse_request below its own frames
            return from_deeper_stack(frames - 1, b) if frames else wire.parse_request(b)

        for b, frames in ((body, 0), ({"messages": body["messages"][:1]}, 0), (body, 300)):
            try:
                from_deeper_stack(frames, b)
            except wire.WireError:
                pass
            except RecursionError:
                self.fail("parse_request raised RecursionError")

    def test_content_past_the_depth_limit_still_changes_the_digest(self):
        deep = wire.DIGEST_MAX_DEPTH + 5
        a = wire.conversation_digest([{"role": "user", "content": self.nested(deep, "x")}])
        b = wire.conversation_digest([{"role": "user", "content": self.nested(deep, "y")}])
        self.assertNotEqual(a, b)


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

    HEADERS: dict = {}                       # sent on every request of the helpers below (a subclass sets them)

    def post(self, body, token=TOKEN, path="/v1/messages", headers=None):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}",
                                              **self.HEADERS, **(headers or {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if r.headers.get("content-type") == "application/json" else raw

    def status_of(self, body, token=TOKEN, headers=None):
        try:
            self.post(body, token=token, headers=headers)
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

    def test_a_tool_call_naming_the_backend_scratch_dir_is_refused_not_forwarded(self):
        # The backend CLI's own environment block names its scratch work dir (2026-11-02: a child's Write landed in
        # <scratch>/work/c.txt on the client). Such a call never reaches the client; the model gets the refusal.
        body = {"tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool scratch path"}]}
        with self.assertLogs("cortados_router.provider.bridge", "WARNING") as logs:
            r = self.post(body)
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertFalse([b for b in r["content"] if b["type"] == "tool_use"])
        self.assertIn("ERR cortados: not run", r["content"][-1]["text"])
        self.assertTrue(any("scratch dir" in line for line in logs.output), logs.output)
        seen = json.loads(Path(self.argv_file).read_text())
        prompt = seen["argv"][seen["argv"].index("--system-prompt") + 1]
        self.assertIn("[cortados relay]", prompt)
        self.assertIn(os.path.basename(os.path.dirname(seen["cwd"])), prompt)   # names the scratch dir not to use

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
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": self.url,
                                          "ANTHROPIC_CUSTOM_HEADERS": "x-cortados-run-id: r1"}):
            self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        seen = json.loads(Path(self.argv_file).read_text())
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__bridge__Read")
        self.assertIsNone(seen["env"]["ANTHROPIC_BASE_URL"])        # would loop back into this endpoint
        self.assertEqual(os.listdir(seen["cwd"]), [])                # empty scratch dir, never the project
        self.assertFalse([k for k in server.claude_env(None) if k.startswith("ANTHROPIC_")])

    def test_claude_env_strips_every_anthropic_variable_by_prefix(self):
        from cortados_router.provider import bridge
        ambient = {k: "" for k in os.environ if k.startswith("ANTHROPIC_")}   # the developer's shell, cleared
        with mock.patch.dict(os.environ, {**ambient, "ANTHROPIC_CUSTOM_HEADERS": "x-cortados-run-id: r1",
                                          "ANTHROPIC_MODEL": "m", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "KEEP_ME": "1"}):
            env = bridge.claude_env(None)
            self.assertFalse([k for k in env if k.startswith("ANTHROPIC_")])
            self.assertEqual((env["KEEP_ME"], env["CLAUDE_CODE_OAUTH_TOKEN"]), ("1", "tok"))
            env = bridge.claude_env("/cfg/b")
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/cfg/b")
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

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

    def _two_tool_turns(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        history += [{"role": "assistant", "content": r2["content"]}]
        return history

    def test_cleared_old_tool_result_content_still_continues_the_conversation(self):
        # Claude Code's time-based microcompaction replaces old tool_result content with a placeholder. The match key
        # (Registry.find) is the assistant reply fingerprints only, so this must keep continuing, not 409.
        history = self._two_tool_turns()
        history[2]["content"][0]["content"] = "[Old tool result content cleared]"
        r = self.post({"tools": [READ_TOOL], "messages": history + [{"role": "user", "content": "thanks"}]})
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(len(self.reg.sessions), 1)

    def test_a_different_earlier_tool_use_id_is_refused_not_continued(self):
        history = self._two_tool_turns()
        history[1] = {"role": "assistant", "content": [dict(history[1]["content"][-1], id="toolu_someone_else")]}
        history[2]["content"][0]["tool_use_id"] = "toolu_someone_else"
        body = {"tools": [READ_TOOL], "messages": history + [{"role": "user", "content": "thanks"}]}
        self.assertEqual(self.status_of(body), 409)

    def test_edited_earlier_assistant_text_is_refused_not_continued(self):
        history = self._two_tool_turns()
        history[3] = {"role": "assistant", "content": [{"type": "text", "text": "an answer this endpoint never wrote"}]}
        body = {"tools": [READ_TOOL], "messages": history + [{"role": "user", "content": "thanks"}]}
        self.assertEqual(self.status_of(body), 409)

    def test_a_compacted_history_with_no_assistant_message_opens_a_new_conversation(self):
        self._two_tool_turns()
        summary = [{"role": "user", "content": "This session is being continued from a previous conversation. "
                                               "Summary: read f0.md, it said hello."}]
        self.assertEqual(self.post({"tools": [READ_TOOL], "messages": summary})["stop_reason"], "end_turn")
        self.assertEqual(len(self.reg.sessions), 2)

    def test_a_retry_of_the_answered_turn_replays_the_stored_response_without_the_backend(self):
        # A lost/timed-out response: the session already holds turn 2's reply, the client re-sends turn 2.
        body, r2 = self._answered_tool_turn()
        history = body["messages"]
        (session,) = self.reg.sessions.values()
        before = list(session.history)
        with mock.patch.object(session, "run_turn", side_effect=AssertionError("backend called")), \
                mock.patch.object(session, "deliver", side_effect=AssertionError("backend called")), \
                self.assertLogs("cortados.provider", "INFO") as logs:
            again = self.post(body, headers=self.REPLAY_ID)
        self.assertEqual(again, r2)
        self.assertEqual(session.history, before)
        self.assertTrue(any("replaying its last response" in l for l in logs.output))
        # The conversation still continues from the replayed reply.
        r3 = self.post({"tools": [READ_TOOL], "messages": history + [{"role": "assistant", "content": r2["content"]},
                                                                     {"role": "user", "content": "thanks"}]},
                       headers=self.REPLAY_ID)
        self.assertEqual(r3["stop_reason"], "end_turn")

    def test_a_retry_with_a_different_user_turn_still_409s_and_says_why(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history}, headers=self.REPLAY_ID)
        history += [{"role": "assistant", "content": r1["content"]}]
        use = r1["content"][-1]["id"]
        self.post({"tools": [READ_TOOL], "messages": history + [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use, "content": "hello"}]}]},
                  headers=self.REPLAY_ID)
        other = {"tools": [READ_TOOL], "messages": history + [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use, "content": "something else"}]}]}
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            self.assertEqual(self.status_of(other, headers=self.REPLAY_ID), 409)
        self.assertTrue(any("class=retry_of_answered_turn" in l for l in logs.output), logs.output)

    REPLAY_ID = {"x-claude-code-session-id": "conversation-replay"}   # replay needs a conversation identity

    def _answered_tool_turn(self, headers=None):
        """One conversation at two replies (a tool_use, then its answer): the retry body of turn 2 and its reply.
        Sent under REPLAY_ID unless `headers` says otherwise: only a request with an identity is ever replayed."""
        headers = self.REPLAY_ID if headers is None else headers
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history}, headers=headers)
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": r1["content"][-1]["id"],
                                                  "content": "hello"}]}]
        body = {"tools": [READ_TOOL], "messages": history}
        return body, self.post(body, headers=headers)

    def test_the_stored_reply_belongs_to_the_current_history_length(self):
        body, _ = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        self.assertEqual(session.last_reply[0], len(session.history))

    def test_a_streaming_retry_replays_the_stored_response_as_sse(self):
        body, r2 = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        with mock.patch.object(session, "run_turn", side_effect=AssertionError("backend called")), \
                mock.patch.object(session, "deliver", side_effect=AssertionError("backend called")):
            raw = self.post(dict(body, stream=True), headers=self.REPLAY_ID)
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[0]["message"]["id"], r2["id"])
        self.assertEqual(events[-1]["type"], "message_stop")
        text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
        self.assertEqual(text, r2["content"][0]["text"])
        self.assertEqual([e for e in events if e["type"] == "message_delta"][0]["delta"]["stop_reason"],
                         r2["stop_reason"])

    def test_a_replay_counts_as_use_so_the_reaper_keeps_the_conversation(self):
        body, r2 = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        session.last_used = time.time() - 3600                         # idle for an hour before the retry
        self.assertEqual(self.post(body, headers=self.REPLAY_ID), r2)
        self.reg.reap(600)
        self.assertEqual(list(self.reg.sessions.values()), [session])

    def test_a_retry_from_another_scope_is_not_replayed(self):
        body, r2 = self._answered_tool_turn(headers={"x-claude-code-session-id": "conversation-A"})
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            self.assertEqual(self.status_of(body, headers={"x-claude-code-session-id": "conversation-B"}), 409)
        self.assertTrue(any("class=retry_of_answered_turn" in l for l in logs.output), logs.output)
        self.assertEqual(self.post(body, headers={"x-claude-code-session-id": "conversation-A"}), r2)

    def test_an_identity_less_retry_is_not_replayed_but_409s(self):
        # P2-1: without a conversation identity both scopes are None; None == None must never replay a stored reply
        # to whoever re-sends the same history (another User-Agent, another wire). It gets the ordinary 409.
        body, r2 = self._answered_tool_turn(headers={})
        (session,) = self.reg.sessions.values()
        self.assertIsNone(session.scope)
        self.assertIsNotNone(session.last_reply)
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            self.assertEqual(self.status_of(body, headers={"user-agent": "someone-else/1.0"}), 409)
        self.assertTrue(any("class=retry_of_answered_turn" in l for l in logs.output), logs.output)
        self.assertEqual(self._409_class(body), "retry_of_answered_turn")   # the same client without an id too

    def test_a_retry_in_the_same_scope_on_another_wire_is_not_replayed(self):
        # P2-1: the session remembers the wire it was opened on; a stored /v1/messages reply is never replayed to a
        # request on another wire, even one whose scope matches.
        body, r2 = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        self.assertEqual(session.wire_path, "/v1/messages")
        turn = wire.parse_request(body)
        with self.reg._lock:
            self.assertEqual(len(self.reg._replays(turn, session.scope, "/v1/messages")), 1)
            self.assertEqual(self.reg._replays(turn, session.scope, "/v1/responses"), [])
            self.assertEqual(self.reg._replays(turn, None, "/v1/messages"), [])
        session.wire_path = "/v1/responses"                            # as if opened on the other wire
        self.assertEqual(self._409_class(body, headers=self.REPLAY_ID), "retry_of_answered_turn")
        session.wire_path = "/v1/messages"
        self.assertEqual(self.post(body, headers=self.REPLAY_ID), r2)

    def test_a_reply_recorded_for_another_history_length_is_not_replayed(self):
        # P2-2: the torn state the last_reply[0] == len(history) guard exists for: the stored reply's fingerprint and
        # the retry's history both fit, but the reply was recorded for an older turn index than the history it sits
        # on (last_reply recorded, then history advanced, or the other way round). It must 409, never replay.
        body, r2 = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        n = len(session.history)
        session.last_reply = (n - 1,) + tuple(session.last_reply[1:])  # recorded for an older turn index
        self.assertEqual(self._409_class(body, headers=self.REPLAY_ID), "retry_of_answered_turn")
        session.last_reply = (n,) + tuple(session.last_reply[1:])      # restored: the same retry is replayed
        self.assertEqual(self.post(body, headers=self.REPLAY_ID), r2)

    def test_a_retry_that_changes_the_pinned_model_is_refused_not_replayed(self):
        body, r2 = self._answered_tool_turn()
        try:
            self.post(dict(body, model="sub/opus"), headers=self.REPLAY_ID)
            self.fail("a retry naming another model was served")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            err = json.loads(exc.read())
        self.assertIn("the model is fixed per conversation", err["error"]["message"])
        self.assertNotIn(r2["id"], json.dumps(err))

    def test_a_reply_over_the_replay_cap_is_not_kept_and_its_retry_409s(self):
        with mock.patch.object(server, "REPLAY_MAX_BYTES", 16), \
                self.assertLogs("cortados.provider", "INFO") as logs:
            body, _ = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        self.assertIsNone(session.last_reply)
        self.assertTrue(any("not kept for a retry replay" in l and "over REPLAY_MAX_BYTES" in l
                            for l in logs.output), logs.output)
        self.assertEqual(self._409_class(body, headers=self.REPLAY_ID), "retry_of_answered_turn")

    def test_an_unserialisable_reply_is_not_kept_and_says_why(self):
        session = type("S", (), {"id": "abcdef0123", "history": ["x"], "last_reply": ("old",)})()
        turn = wire.parse_request({"messages": [{"role": "user", "content": "hi"}]})
        loop: list = []
        loop.append(loop)                                              # json.dumps refuses a circular reference
        with self.assertLogs("cortados.provider", "INFO") as logs:
            server._remember_reply(session, turn, "msg_1", "cortados", ([{"type": "text", "text": "a"}], "end_turn",
                                                                         {"x": loop}))
        self.assertIsNone(session.last_reply)
        self.assertTrue(any("does not serialize (ValueError)" in l for l in logs.output), logs.output)

    def test_a_retry_two_turns_behind_with_the_same_user_text_409s_as_client_behind(self):
        # Turn N and turn N+1 carry the same user text, so turn N's retry has the stored reply's turn fingerprint;
        # only the history-length guard in _replays tells it is two replies behind, not a retry of the last turn.
        body, r2 = self._answered_tool_turn()
        history = body["messages"] + [{"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}]
        turn_n = {"tools": [READ_TOOL], "messages": history}
        r3 = self.post(turn_n, headers=self.REPLAY_ID)
        self.post({"tools": [READ_TOOL], "messages": history + [{"role": "assistant", "content": r3["content"]},
                                                                {"role": "user", "content": "thanks"}]},
                  headers=self.REPLAY_ID)
        (session,) = self.reg.sessions.values()
        self.assertEqual(len(session.history), 4)
        self.assertEqual(session.last_reply[1], wire.turn_fingerprint(wire.parse_request(turn_n)))
        self.assertEqual(self._409_class(turn_n, headers=self.REPLAY_ID), "client_behind=2")

    def test_two_sessions_the_same_retry_could_replay_are_refused_not_guessed(self):
        opening = [{"role": "user", "content": "hi"}]
        a = self.post({"tools": [READ_TOOL], "messages": opening}, headers=self.REPLAY_ID)
        self.post({"tools": [READ_TOOL], "messages": opening}, headers=self.REPLAY_ID)   # same opening, same reply
        follow = {"tools": [READ_TOOL], "messages": opening + [{"role": "assistant", "content": a["content"]},
                                                               {"role": "user", "content": "more"}]}
        sa, sb = list(self.reg.sessions.values())
        replies = []
        for live, aside in ((sa, sb), (sb, sa)):                        # advance each alone: together it is ambiguous
            with self.reg._lock:
                self.reg.sessions.pop(aside.id)
            try:
                replies.append(self.post(follow, headers=self.REPLAY_ID))
            finally:
                with self.reg._lock:
                    self.reg.sessions[aside.id] = aside
        self.assertNotEqual(replies[0]["id"], replies[1]["id"])
        self.assertEqual(sa.history, sb.history)
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            try:
                self.post(follow, headers=self.REPLAY_ID)
                self.fail("an ambiguous retry was replayed")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 409)
                raw = exc.read().decode()
        for r in replies:
            self.assertNotIn(r["id"], raw)
        line = next(l for l in logs.output if "409 lost conversation" in l)
        self.assertIn("candidates=2", line)

    def test_a_stale_continuation_whose_conversation_closed_while_waiting_is_classified(self):
        body, r2 = self._answered_tool_turn()
        (session,) = self.reg.sessions.values()
        follow = {"tools": [READ_TOOL], "messages": body["messages"] + [
            {"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}]}
        codes = []

        def continuation():
            codes.append(self.status_of(follow))
        session.lock.acquire()
        try:
            with self.assertLogs("cortados.provider", "WARNING") as logs:
                t = threading.Thread(target=continuation)
                t.start()
                time.sleep(1.0)                                         # it has found the session and waits on the lock
                with self.reg._lock:
                    self.reg.sessions.pop(session.id)                   # closed meanwhile (a failure elsewhere, a reap)
                session.lock.release()
                t.join(30)
        finally:
            if session.lock.locked():
                session.lock.release()
            session.close()
        self.assertEqual(codes, [409])
        self.assertTrue(any("409 lost conversation: class=closed_while_waiting" in l for l in logs.output),
                        logs.output)

    def _409_class(self, body, headers=None):
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            self.assertEqual(self.status_of(body, headers=headers), 409)
        line = next(l for l in logs.output if "409 lost conversation" in l)
        return line.split("class=")[1].split()[0]

    def test_the_409_log_classifies_client_ahead_diverged_no_candidate_and_reaped(self):
        history = self._two_tool_turns()
        tail = [{"role": "user", "content": "thanks"}]
        ahead = history + tail + [{"role": "assistant", "content": [{"type": "text", "text": "never written"}]}]
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": ahead + tail}), "client_ahead")
        edited = list(history)
        edited[3] = {"role": "assistant", "content": [{"type": "text", "text": "an answer this endpoint never wrote"}]}
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": edited + tail}), "diverged_at=1")
        # Retried two replies behind the live session (P2-1): named client_behind, not a divergence.
        (session,) = self.reg.sessions.values()
        self.post({"tools": [READ_TOOL], "messages": history + tail})     # the session is now at 3 replies
        self.assertEqual(len(session.history), 3)
        behind = history[:2] + [{"role": "user", "content": "a different turn"}]   # 1 reply, then a new turn
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": behind}), "client_behind=2")
        stranger = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "x"}]}]
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": stranger + tail}), "no_candidate")
        self.reg.reap(-1)                               # every idle session is past a negative idle limit
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": history + tail}), "reaped_recently")

    def test_an_ambiguous_history_is_refused_not_guessed(self):
        opening = [{"role": "user", "content": "hi"}]
        a = self.post({"tools": [READ_TOOL], "messages": opening})
        self.post({"tools": [READ_TOOL], "messages": opening})          # same opening, same text reply
        follow = opening + [{"role": "assistant", "content": a["content"]}, {"role": "user", "content": "more"}]
        self.assertEqual(self._409_class({"tools": [READ_TOOL], "messages": follow}), "ambiguous")

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
        self.assertIn("/v1/cortados/usage", body)            # it fetches the usage endpoint for its data


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


class FixedModelFloorTests(EndpointHarness):
    """A fixed-model endpoint (no router) cannot choose or reserve an account: a floor fails closed on the opening turn."""

    def test_opening_turn_with_a_floor_is_refused_and_without_it_runs(self):
        body = {"model": "cortados", "messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual(self.status_of(body, headers={"x-cortados-min-left-pct": "5"}), 429)
        self.assertEqual(self.status_of(body), 200)


class SubscriptionPinEndpointTests(EndpointHarness):
    """sub/ pins through the endpoint: GET /v1/models offers every pinnable model, and a Codex pin opens the codex-cli
    backend with the pinned model (the backend is a recording stand-in over the fake Claude CLI: no vendor CLI runs)."""

    def setUp(self):
        super().setUp()
        self._keep = (core.STATE, core.lane_health, core.decide)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-pin-endpoint-"))   # never the live router.db
        core.lane_health = lambda: {}

        def no_s1(text, ctx=None):
            raise AssertionError("a pinned request must not be scored")
        core.decide = no_s1
        self.opened = []

        def recording(provider):
            def factory(reg, **kw):
                self.opened.append((provider, kw["model"]))
                return server._claude_session(reg, **kw)
            return factory
        self.reg._factories = {"claude-cli": recording("claude-cli"), "codex-cli": recording("codex-cli")}
        from cortados_router.provider import routing
        self.reg.router = lambda body, client, **kw: routing.choose(
            core, body, client=client, conversation_id=uuid.uuid4().hex, claude_bin=sys.executable,
            codex_bin=sys.executable, **kw)

    def tearDown(self):
        core.STATE, core.lane_health, core.decide = self._keep
        super().tearDown()

    def models(self):
        req = urllib.request.Request(self.url + "/v1/models", headers={"authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_models_lists_cortados_and_every_pinnable_model_with_labels(self):
        from cortados_router.provider import routing
        got = self.models()
        names = {m["id"]: m["display_name"] for m in got["data"]}
        self.assertEqual(got["data"][0]["id"], "cortados")
        self.assertEqual(names["cortados"], "CortadOS")
        self.assertEqual(names["sub/claude-fable-5-1"], "Claude Fable 5.1 · CortadOS")
        self.assertEqual(names["sub/gpt-6.1-sol"], "GPT-6.1 Sol · CortadOS")
        self.assertEqual(set(names) - {"cortados"}, {f"sub/{m}" for _, m in routing.pinnable_models(core)})
        self.assertEqual((got["first_id"], got["last_id"]), ("cortados", got["data"][-1]["id"]))

    def test_models_without_routing_lists_only_cortados(self):
        self.reg.router = None                  # a --fixed-model endpoint does not honour pins
        self.assertEqual([m["id"] for m in self.models()["data"]], ["cortados"])

    def test_codex_pin_opens_the_codex_backend_with_the_pinned_model(self):
        body = {"model": "sub/gpt-6.1-sol", "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual(self.post(body)["stop_reason"], "end_turn")
        self.assertEqual(self.opened, [("codex-cli", "gpt-6.1-sol")])
        self.assertEqual(self.status_of({**body, "model": "sub/gpt-9-imaginary"}), 400)

    def test_a_codex_pinned_conversation_keeps_its_model(self):
        session = type("S", (), {"route": {"pinned": True, "model": "gpt-6.1-sol", "provider": "codex-cli"}})()
        self.assertIsNone(server.Handler._model_change(session, {"model": "sub/gpt-6.1-sol"}))
        self.assertIsNone(server.Handler._model_change(session, {"model": "cortados"}))
        why = server.Handler._model_change(session, {"model": "sub/opus"})
        self.assertIn("pinned to sub/gpt-6.1-sol", why)
        self.assertIn("start a new conversation", why)


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

    def test_refusals_are_logged_and_recorded_under_the_run_id(self):
        def too_big(body, client):
            return {"error": "sub/x: the request (≈293k tokens) does not fit any Claude account's input ceiling",
                    "fit_blocked": True}
        self.reg.router = too_big
        with self.assertLogs("cortados.provider", level="WARNING") as logs:
            code, err = self.send({"messages": [{"role": "user", "content": "hi"}]}, "t20261102182559-5ca0fe09")
        self.assertEqual((code, err["type"]), (413, "request_too_large"))
        self.assertTrue(any("413" in line and "larger than every eligible" in line for line in logs.output), logs.output)
        self.assertFalse(any("does not fit" in line for line in logs.output), logs.output)   # no backend text
        code, got = self.lookup("t20261102182559-5ca0fe09")
        self.assertEqual(code, 200)                              # no longer "no routed request recorded"
        self.assertEqual(got["models"], [])
        last = got["refusals"][-1]
        self.assertEqual((last["status"], last["type"]), (413, "request_too_large"))
        self.assertEqual(last["reason"], "the request is larger than every eligible model's context")
        self.assertEqual(set(last), {"status", "type", "reason", "at"})   # no raw or backend text is stored

    # ---- P1: records and refusal log lines hold status, type and a fixed reason only: no backend text, no token
    def refused_with(self, error, run_id, **router_extra):
        self.reg.router = lambda body, client: {"error": error, **router_extra}
        with self.assertLogs("cortados.provider", level="WARNING") as logs:
            self.send({"messages": [{"role": "user", "content": "hi"}]}, run_id)
        return self.lookup(run_id)[1]["refusals"][-1], "\n".join(logs.output)

    def assert_no_fragment(self, text, secret, k=4):
        for i in range(len(secret) - k + 1):
            self.assertNotIn(secret[i:i + k], text)

    def assert_clean(self, last, logs, *secrets):
        self.assertEqual(set(last), {"status", "type", "reason", "at"})
        record = json.dumps(last)
        for secret in secrets:
            self.assert_no_fragment(record, secret)
            self.assert_no_fragment(logs, secret)

    def test_a_full_token_in_backend_text_reaches_neither_record_nor_log(self):
        last, logs = self.refused_with(f"backend said Authorization: Bearer {TOKEN} and quit", "run-redact-full",
                                       fit_blocked=True)
        self.assert_clean(last, logs, TOKEN, "backend said")

    def test_a_token_across_a_truncation_boundary_reaches_neither_record_nor_log(self):
        # The review's "x"*490+token case: a token straddling any cut a slice might make. Every offset around the
        # old 200 and 300 cuts and the reviewer's 500 is tried; nothing of the token (or the padding) survives.
        for pad in [*range(185, 205), *range(285, 305), 490, 495]:
            last, logs = self.refused_with("x" * pad + TOKEN + "tail", f"run-cut-{pad:04d}", fit_blocked=True)
            self.assert_clean(last, logs, TOKEN, "xxxxxxxx", "tail")

    def test_a_json_escaped_credential_reaches_neither_record_nor_log(self):
        key = 'sk-ant-api"quoted\\back-0123456789'
        escaped = json.dumps(key)[1:-1]
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": key}):
            last, logs = self.refused_with(f'upstream body {{"key": "{escaped}"}} and raw {key}', "run-escaped",
                                           fit_blocked=True)
        self.assert_clean(last, logs, key, escaped, "quoted", "upstream body")

    # ---- P2: refusal log lines name an allowlisted endpoint, never the raw path
    def test_refusal_logs_never_carry_the_request_path(self):
        secret = "pathsecret0123456789"
        cases = [("GET", RUNS_PATH_FOR_TESTS + secret + ".bad", TOKEN, 400, "runs"),       # malformed run id
                 ("GET", RUNS_PATH_FOR_TESTS + secret, "wrong-token-0123", 401, "runs"),   # unauthenticated
                 ("GET", "/nope/" + secret, TOKEN, 404, "other"),                          # unknown path
                 ("POST", "/nope/" + secret + "?k=" + secret, TOKEN, 404, "other")]
        for method, path, token, want, where in cases:
            req = urllib.request.Request(self.url + path, method=method,
                                         data=b"{}" if method == "POST" else None,
                                         headers={"authorization": f"Bearer {token}",
                                                  "content-type": "application/json"})
            with self.assertLogs("cortados.provider", level="WARNING") as logs:
                try:
                    urllib.request.urlopen(req, timeout=30).close()
                    self.fail("expected an HTTP error")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, want, path)
            out = "\n".join(logs.output)
            self.assertNotIn(secret, out)
            self.assertIn(f"{method} {where} ", out)

    def test_request_derived_text_is_never_stored(self):
        secret_pin = "sub/please-dont-store-this-0123"
        last, logs = self.refused_with(f"{secret_pin}: not a served model", "run-pin-text", client_error=True)
        self.assertEqual(last["reason"], "the pin or exclusion names no model this endpoint can serve")
        self.assertNotIn("detail", last)
        self.assertNotIn(secret_pin, json.dumps(last))
        self.assertNotIn(secret_pin, logs)

    # ---- P2: only an authenticated request creates, changes or evicts a run record
    def test_an_unauthenticated_request_cannot_create_modify_or_evict_a_record(self):
        self.reg.runs.MAX_RUNS = 2
        self.reg.router = lambda body, client: {"error": "too big", "fit_blocked": True}
        self.send({"messages": [{"role": "user", "content": "hi"}]}, "run-owner-1")
        before = self.lookup("run-owner-1")[1]
        with self.assertLogs("cortados.provider", level="WARNING") as logs:
            for token in ("wrong-token-0123456789", ""):
                for rid in ("run-owner-1", "run-intruder-1", "run-intruder-2", "run-intruder-3"):
                    code, err = self.send({"messages": [{"role": "user", "content": "hi"}]}, rid, token=token)
                    self.assertEqual((code, err["type"]), (401, "authentication_error"))
        self.assertTrue(all("unauthenticated" in line for line in logs.output if "401" in line), logs.output)
        self.assertEqual(self.lookup("run-owner-1"), (200, before))      # neither modified nor evicted
        for rid in ("run-intruder-1", "run-intruder-2", "run-intruder-3"):
            self.assertEqual(self.lookup(rid)[0], 404)                   # never created
        self.assertEqual(list(self.reg.runs._refusals), ["run-owner-1"])

    # ---- P2: every failure path records through the one helper
    def test_a_streaming_bridge_error_is_recorded(self):
        history = [{"role": "user", "content": "hi"}]
        code, r1 = self.send({"tools": [READ_TOOL], "messages": history}, "run-stream-fail")
        self.assertEqual(code, 200)
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "backend failure"}]
        req = urllib.request.Request(self.url + "/v1/messages", method="POST",
                                     data=json.dumps({"stream": True, "tools": [READ_TOOL], "messages": history}).encode(),
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              "x-cortados-run-id": "run-stream-fail"})
        with self.assertLogs("cortados.provider", level="WARNING"):
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["type"], "error")                     # HTTP 200 + an SSE error event
        last = self.lookup("run-stream-fail")[1]["refusals"][-1]
        self.assertEqual((last["status"], last["type"]), (502, "api_error"))
        self.assertTrue(last["reason"].startswith("backend_reported_error"), last)
        self.assertEqual(set(last), {"status", "type", "reason", "at"})

    def test_a_400_refusal_is_recorded_too(self):
        def bad_pin(body, client):
            return {"error": "sub/nope: not a served model", "client_error": True}
        self.reg.router = bad_pin
        code, _ = self.send({"messages": [{"role": "user", "content": "hi"}]}, "run_bad-pin-1")
        self.assertEqual(code, 400)
        last = self.lookup("run_bad-pin-1")[1]["refusals"][-1]
        self.assertEqual(last["status"], 400)
        self.assertNotIn("sub/nope", json.dumps(last))           # the client's own pin is not echoed into the record

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
        code, got = self.lookup("run-nostart")
        self.assertEqual(code, 200)                                   # no route entry: only the refusal is recorded
        self.assertEqual((got["models"], got["model"]), ([], None))
        self.assertEqual(got["refusals"][-1]["status"], 502)

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
    """A conversation dropped because its backend failed answers the next request on it (a retry of the failed request,
    or a new message on the same history) with a terminal error naming the failure's category, not the 409 of a
    conversation this endpoint never held; the tombstone belongs to that conversation and client only. Requests carry
    Claude Code's X-Claude-Code-Session-Id (the conversation identity a tombstone needs) unless a test says otherwise."""

    SID = "55fdfd5e-174a-4af2-acab-f4bc918051ab"
    HEADERS = {"X-Claude-Code-Session-Id": SID}

    def failed_conversation(self, first="hi", headers=None):
        """Open a conversation, fail its second turn in the backend, and return the failed request's body."""
        history = [{"role": "user", "content": first}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history}, headers=headers)
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "backend failure"}]
        body = {"tools": [READ_TOOL], "messages": history}
        with self.assertLogs("cortados.provider", "WARNING"):
            self.assertEqual(self.status_of(body, headers=headers), 502)
        self.assertEqual(self.reg.sessions, {})                        # the failed conversation was dropped
        return body

    def send(self, body, headers=None):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              **self.HEADERS, **(headers or {})})
        return urllib.request.urlopen(req, timeout=30)

    def lose_a_reply(self, headers):
        """Conversation B: opens with "hi" (A's prefix, A's reply), advances its live session one turn, then retries as
        a client that lost that turn's response would: the history no longer matches B's live session."""
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history}, headers=headers)
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "next step"}]
        retry = {"tools": [READ_TOOL], "messages": history}
        self.post(retry, headers=headers)                              # B's session advances; the reply is "lost"
        self.assertEqual(len(self.reg.sessions), 1)                    # B is healthy and live
        return retry

    def test_without_an_identity_an_independent_conversation_never_gets_another_ones_failure(self):
        # The round-3 reproduction: A fails after "hi -> OK"; independent B (same prefix, same UA, no identity)
        # advances, loses a response and retries. B must get the ordinary 409, never A's terminal failure.
        none = {"X-Claude-Code-Session-Id": ""}
        server._unkeyed_logged = False
        with mock.patch.object(server.logger, "info") as info:
            body = self.failed_conversation(headers=none)
        lines = [c.args[0] % c.args[1:] for c in info.call_args_list]
        self.assertEqual(sum("no conversation identity" in line for line in lines), 1, lines)
        self.assertEqual(len(self.reg.tombstones), 0)                  # no identity: nothing published
        self.assertEqual(self.error_of(body, none)[0], 409)            # A's own retry: the ordinary 409 too
        # B's retry of its answered turn: without an identity it is never replayed (another client could share the
        # history), and never A's failure either: the ordinary 409.
        self.assertEqual(self.error_of(self.lose_a_reply(none), none)[0], 409)

    def test_the_missing_identity_is_logged_once_per_process(self):
        none = {"X-Claude-Code-Session-Id": ""}
        server._unkeyed_logged = False
        with mock.patch.object(server.logger, "info") as info:
            self.failed_conversation(headers=none)
            self.failed_conversation("hello", headers=none)
        lines = [c.args[0] % c.args[1:] for c in info.call_args_list]
        self.assertEqual(sum("no conversation identity" in line for line in lines), 1, lines)

    def test_identity_values_are_never_logged(self):
        with mock.patch.object(server.logger, "info") as info:          # INFO: the level the brief forbids
            body = self.failed_conversation()
            self.error_of(body)
        logged = [c.args[0] % c.args[1:] for c in info.call_args_list]
        self.assertTrue(logged)
        self.assertFalse([line for line in logged if self.SID in line])

    def test_distinct_identities_never_share_a_tombstone(self):
        self.failed_conversation(headers={"X-Claude-Code-Session-Id": "session-a"})
        b = {"X-Claude-Code-Session-Id": "session-b"}
        # B's retry of its answered turn is now replayed from B's live session (Replay): never A's failure.
        self.assertIn("content", self.post(self.lose_a_reply(b), headers=b))

    def test_a_sibling_sharing_the_session_id_retrying_a_lost_reply_gets_the_409_not_the_failure(self):
        # Round-4 reproduction: Claude Code's main loop and subagents share one session id. Sibling A fails after
        # "hi -> OK" (tombstone); sibling B, same id, UA and history, advances, loses that response and retries its
        # previous history. B's live session strictly extends it: B gets the ordinary 409, never A's terminal error.
        body = self.failed_conversation()
        self.assertEqual(len(self.reg.tombstones), 1)
        # B's retry of its answered turn is now replayed from B's live session (Replay): never A's failure.
        self.assertIn("content", self.post(self.lose_a_reply(self.HEADERS), headers=self.HEADERS))
        self.assertEqual(len(self.reg.sessions), 1)                    # B is untouched and still live
        self.assertEqual(self.error_of(body)[0], 409)                  # A's retry is indistinguishable from B's

    def test_a_live_extension_under_another_identity_never_hides_the_failure(self):
        # Control: the live session that extends the history belongs to another session id; A's retry keeps its
        # terminal error.
        body = self.failed_conversation()
        self.lose_a_reply({"X-Claude-Code-Session-Id": "session-b"})
        code, err, _ = self.error_of(body)
        self.assertEqual(code, 400)
        self.assertIn("failed at", err["error"]["message"])

    def test_the_same_identity_and_history_gets_the_terminal_error(self):
        body = self.failed_conversation()
        self.assertEqual(len(self.reg.tombstones), 1)
        code, err, _ = self.error_of(body)
        self.assertEqual((code, err["error"]["type"]), (400, "invalid_request_error"))
        self.assertIn("failed at", err["error"]["message"])
        self.assertEqual(self.error_of(body, {"X-Claude-Code-Session-Id": ""})[0], 409)   # identity dropped: no match

    def test_a_run_id_alone_is_an_identity(self):
        run = {"X-Claude-Code-Session-Id": "", server.RUN_ID_HEADER: "run-00077"}
        body = self.failed_conversation(headers=run)
        self.assertEqual(self.error_of(body, run)[0], 400)

    def error_of(self, body, headers=None):
        try:
            self.send(body, headers).close()
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_a_retry_of_the_failed_request_gets_a_terminal_error_not_a_409(self):
        body = self.failed_conversation()
        for _ in range(2):                                              # every retry, not only the first
            code, err, headers = self.error_of(body)
            # 400 invalid_request_error + x-should-retry: false: Claude Code / the SDK retry api_error and 5xx.
            self.assertEqual((code, err["error"]["type"]), (400, "invalid_request_error"))
            self.assertEqual(headers.get("x-should-retry"), "false")
            msg = err["error"]["message"]
            self.assertIn("backend_reported_error: the Claude backend reported an error (API status 400)", msg)
            self.assertNotIn("cache TTL ordering", msg)                # backend text is never replayed
            self.assertRegex(msg, r"^cortados: conversation [0-9a-f]{8} failed at \d\d:\d\d:\d\d .*start a new conversation$")
            self.assertNotIn("no longer holds", msg)
        self.assertEqual(self.reg.sessions, {})                        # a tombstone never restarts a backend

    def test_a_request_on_a_failed_conversation_is_recorded_under_its_run_id(self):
        run = {"x-cortados-run-id": "run-after-fail"}      # part of the conversation identity: sent on every turn
        body = self.failed_conversation(headers=run)
        code, _, _ = self.error_of(body, headers=run)
        self.assertEqual(code, 400)
        req = urllib.request.Request(self.url + "/v1/cortados/runs/run-after-fail",
                                     headers={"authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            last = json.loads(r.read())["refusals"][-1]
        self.assertEqual((last["status"], last["type"]), (400, "invalid_request_error"))
        self.assertEqual(last["reason"], "the conversation already failed (terminal): backend_reported_error: the "
                                         "Claude backend reported an error (API status 400)")
        self.assertNotIn("cache TTL ordering", json.dumps(last))       # backend text is never replayed here either
        self.assertEqual([r["status"] for r in json.loads(urllib.request.urlopen(req, timeout=30).read())["refusals"]],
                         [502, 400])                                     # the failed turn itself, then the retry

    def test_a_new_message_on_the_failed_history_also_gets_the_terminal_error(self):
        body = self.failed_conversation()
        follow = dict(body, messages=body["messages"] + [{"role": "user", "content": "are you there?"}])
        self.assertEqual(self.error_of(follow)[0], 400)

    def test_a_streaming_retry_gets_a_terminal_sse_error(self):
        body = self.failed_conversation()
        raw = self.post(dict(body, stream=True))
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["message_start", "error"])
        self.assertEqual(events[-1]["error"]["type"], "invalid_request_error")
        self.assertIn("backend_reported_error", events[-1]["error"]["message"])

    def test_another_conversation_with_the_same_reply_never_gets_the_tombstone(self):
        body = self.failed_conversation()
        # Same assistant reply (same fingerprints), different earlier user message: another conversation.
        other = dict(body, messages=[{"role": "user", "content": "a different question"}] + body["messages"][1:])
        code, err, _ = self.error_of(other)
        self.assertEqual(code, 409)
        self.assertIn("no longer holds", err["error"]["message"])

    def test_client_churn_in_the_history_still_finds_the_tombstone(self):
        # What a client re-renders between a request and its retry is not the conversation's identity: the system
        # prompt, earlier <system-reminder> spans, thinking blocks, cache markers, mid-conversation system messages
        # and whitespace. Each transformation alone must still meet the terminal error, never a retryable 409.
        def first(reminder):
            return [{"type": "text", "text": f"<system-reminder>\n{reminder}\n</system-reminder>"},
                    {"type": "text", "text": "hi"}]
        thinking = {"type": "thinking", "thinking": "let me see", "signature": "sig-1"}
        body = self.failed_conversation(first("todo list: empty"))

        def variant(edit):
            msgs = json.loads(json.dumps(body["messages"]))
            edit(msgs)
            return dict(body, messages=msgs)

        variants = {
            "reminder changed": variant(lambda m: m[0].update(content=first("todo list: 3 items, date 2026-11-03"))),
            "reminder removed": variant(lambda m: m[0].update(content=[{"type": "text", "text": "hi"}])),
            "system changed": dict(body, system=[{"type": "text", "text": "a regenerated system prompt",
                                                  "cache_control": {"type": "ephemeral"}}]),
            "thinking added": variant(lambda m: m[1].update(content=[thinking] + m[1]["content"])),
            "redacted thinking added": variant(lambda m: m[1].update(
                content=[{"type": "redacted_thinking", "data": "opaque"}] + m[1]["content"])),
            "cache_control changed": variant(lambda m: m[0]["content"][1].update(cache_control={"type": "ephemeral",
                                                                                                "ttl": "1h"})),
            "system message inserted": variant(lambda m: m.insert(1, {"role": "system", "content": "env: changed"})),
            "whitespace changed": variant(lambda m: m[0]["content"][1].update(text="  hi \n")),
        }
        for name, retry in variants.items():
            with self.subTest(name):
                code, err, _ = self.error_of(retry)
                self.assertEqual((code, err["error"]["type"]), (400, "invalid_request_error"))
                self.assertIn("backend_reported_error", err["error"]["message"])

    def test_thinking_removed_on_the_retry_still_finds_the_tombstone(self):
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        with_thinking = [{"type": "thinking", "thinking": "hmm", "signature": "s"}] + r1["content"]
        body = {"tools": [READ_TOOL], "messages": history + [{"role": "assistant", "content": with_thinking},
                                                              {"role": "user", "content": "backend failure"}]}
        with self.assertLogs("cortados.provider", "WARNING"):
            self.assertEqual(self.status_of(body), 502)
        retry = dict(body, messages=history + [{"role": "assistant", "content": r1["content"]},
                                               {"role": "user", "content": "backend failure"}])
        self.assertEqual(self.error_of(retry)[0], 400)

    def test_conversations_differing_only_in_real_user_text_do_not_collide(self):
        body = self.failed_conversation([{"type": "text", "text": "<system-reminder>r</system-reminder>"},
                                         {"type": "text", "text": "hi"}])
        other = json.loads(json.dumps(body))
        other["messages"][0]["content"][1]["text"] = "hi there"       # same reminder and reply, other user text
        code, err, _ = self.error_of(other)
        self.assertEqual(code, 409)
        self.assertIn("no longer holds", err["error"]["message"])

    def test_a_busy_timeout_never_closes_or_tombstones_the_owners_session(self):
        # Deterministic overlap: request A owns the session lock; request B (same history) times out waiting for it.
        # B's timeout is request-local: a retryable 529 overloaded_error, the session stays live and unclosed, no
        # tombstone. When A's backend then really fails, A's own diagnostic is the one recorded, not backend_timeout.
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        [s] = self.reg.sessions.values()
        body = {"tools": [READ_TOOL], "messages": history + [{"role": "assistant", "content": r1["content"]},
                                                              {"role": "user", "content": "overlap"}]}
        in_turn, release = threading.Event(), threading.Event()
        closes, real_close = [], s.close

        def send_user(content, timeout):
            in_turn.set()
            self.assertTrue(release.wait(30))
            raise BridgeError("the headless CLI exited with code 9; see /secret/dir")

        def close():
            closes.append(time.time())
            real_close()
        s.send_user, s.close = send_user, close
        results = {}

        def call(name):
            results[name] = self.error_of(body)

        with mock.patch.object(server, "TURN_TIMEOUT_S", 0.5), self.assertLogs("cortados.provider", "WARNING"):
            a = threading.Thread(target=call, args=("a",))
            a.start()
            self.assertTrue(in_turn.wait(10))
            call("b")                                                   # waits 0.5 s for A's lock, then gives up
            self.assertEqual(results["b"][0], server.BUSY_STATUS)
            self.assertEqual(results["b"][1]["error"]["type"], "overloaded_error")
            self.assertIn("conversation busy", results["b"][1]["error"]["message"])
            self.assertNotEqual(results["b"][2].get("x-should-retry"), "false")
            self.assertIs(self.reg.get(s.id), s)                       # the owner's session survives B's timeout
            self.assertEqual((closes, len(self.reg.tombstones)), ([], 0))
            release.set()
            a.join(10)
        self.assertEqual(results["a"][0], 502)                         # A's real failure
        self.assertEqual((self.reg.sessions, len(closes)), ({}, 1))
        code, err, _ = self.error_of(body)                             # the retry sees A's diagnostic
        self.assertEqual(code, 400)
        self.assertIn("backend_exited", err["error"]["message"])
        self.assertNotIn("backend_timeout", err["error"]["message"])

    def test_another_client_or_run_with_the_same_history_never_gets_the_tombstone(self):
        body = self.failed_conversation()
        self.assertEqual(self.error_of(body)[0], 400)                  # the default client (Python-urllib)
        self.assertEqual(self.error_of(body, {"user-agent": "claude-cli/2.1.0 (external, cli)"})[0], 409)
        self.assertEqual(self.error_of(body, {server.RUN_ID_HEADER: "run-00042"})[0], 409)

    def test_a_genuinely_unknown_history_is_still_a_409(self):
        self.failed_conversation()
        body = {"tools": [READ_TOOL], "messages": [{"role": "user", "content": "x"},
                                                   {"role": "assistant", "content": "an answer from elsewhere"},
                                                   {"role": "user", "content": "go on"}]}
        code, err, _ = self.error_of(body)
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
                self.id = f"{i:08x}-dead"
            def close(self):
                pass
        with mock.patch.object(server, "TOMBSTONE_MAX", 3):
            for i in range(5):
                self.reg.fail(Dead(i), BridgeError("boom"), f"key-{i}")
        self.assertEqual(list(self.reg.tombstones), ["key-2", "key-3", "key-4"])
        self.reg.fail(Dead(9), BridgeError("boom"), None)              # an opening request: no tombstone
        self.assertEqual(len(self.reg.tombstones), 3)

    def test_the_replayed_reason_never_carries_tool_arguments_or_backend_text(self):
        secret = "sk-live-SECRET123 /Users/someone/private.txt"
        cases = {
            "codex app-server sent a malformed item/tool/call: "
            + json.dumps({"arguments": {"command": f"cat {secret}"}})[:300]: "backend_protocol_error",
            f"the headless CLI reported an error: API Error: 400 {secret}": "backend_reported_error",
            f"the Codex turn failed: {secret} (usageLimitExceeded)": "codex_turn_failed",
            f"the Codex turn failed: boom ({secret})": "codex_turn_failed",
            f"codex app-server refused client tool {secret}: {secret}": "backend_protocol_error",
            f"the headless CLI exited with code 1; see {secret}": "backend_exited",
            f"something nobody anticipated: {secret} {TOKEN}": "backend_error",
        }
        for text, kind in cases.items():
            diag = server.failure_diagnostic(BridgeError(text))
            self.assertTrue(diag.startswith(kind + ": "), (text, diag))
            for leak in ("SECRET", "private", "someone", TOKEN, "cat "):
                self.assertNotIn(leak, diag)
        self.assertTrue(server.failure_diagnostic(BridgeError("the Codex turn failed: x (usageLimitExceeded)"))
                        .endswith("(usageLimitExceeded)"))           # an allowlisted enum value is kept

    def test_every_bridge_error_template_has_a_category(self):
        # Drift guard: a new or reworded BridgeError in either bridge must land in a named category, not the generic
        # backend_error. f-string placeholders are rendered as "1" (a count, a code, seconds, a status).
        import ast

        def render(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(v.value if isinstance(v, ast.Constant) else "1" for v in node.values)
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left, right = render(node.left), render(node.right)
                return None if left is None or right is None else left + right
            return None

        provider_dir = Path(server.__file__).parent
        seen = 0
        for name in ("bridge.py", "codex_bridge.py"):
            for node in ast.walk(ast.parse((provider_dir / name).read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "BridgeError" and node.args:
                    text = render(node.args[0])
                    self.assertIsNotNone(text, f"{name}:{node.lineno}: BridgeError message is not a literal template")
                    diag = server.failure_diagnostic(BridgeError(text))
                    self.assertFalse(diag.startswith("backend_error:"), f"{name}:{node.lineno}: {text!r} -> {diag}")
                    seen += 1
        self.assertGreater(seen, 30)

    def test_an_unknown_codex_turn_status_keeps_its_detail(self):
        diag = server.failure_diagnostic(BridgeError("the Codex turn cancelledByServer: quota (usageLimitExceeded)"))
        self.assertEqual(diag, "codex_turn_failed: the Codex backend failed the turn (usageLimitExceeded)")
        self.assertTrue(server.failure_diagnostic(BridgeError("the Codex turn completed with 1 tool call(s) unanswered"))
                        .startswith("tool_result_mismatch: "))

    def test_an_uncategorised_failure_is_logged_without_its_text(self):
        with self.assertLogs("cortados.provider", level="INFO") as logs:
            diag = server.failure_diagnostic(BridgeError("something new: sk-SECRET"))
        self.assertEqual(diag, "backend_error: the backend failed")
        self.assertIn("add a pattern to _FAILURE_CATEGORIES", logs.output[0])
        self.assertNotIn("SECRET", "".join(logs.output))

    def test_an_idle_closed_conversation_leaves_no_tombstone(self):
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        [s] = self.reg.sessions.values()
        s.last_used = time.time() - 10_000
        self.reg.reap(1)
        self.assertEqual((self.reg.sessions, dict(self.reg.tombstones)), ({}, {}))
        follow = history + [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "more"}]
        self.assertEqual(self.status_of({"tools": [READ_TOOL], "messages": follow}), 409)

    def test_a_request_waiting_on_a_failing_turn_never_reaches_the_dead_backend(self):
        # Deterministic overlap: request A holds the session lock and its backend fails; request B (a retry with the
        # same history) found the session before A failed and waits for the lock. The session must be out of the
        # registry and the tombstone in place at the same instant, before B gets the lock; B is then told the
        # failure, terminally, and never drives the closed backend.
        history = [{"role": "user", "content": "hi"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        [s] = self.reg.sessions.values()
        body = {"tools": [READ_TOOL], "messages": history + [{"role": "assistant", "content": r1["content"]},
                                                              {"role": "user", "content": "overlap"}]}
        in_turn, release = threading.Event(), threading.Event()
        seen, b_waiting, sends = [], threading.Event(), []
        real_acquire = s.lock.acquire

        def send_user(content, timeout):
            sends.append(content)
            if len(sends) == 1:
                in_turn.set()
                release.wait(10)
                with self.reg._lock:                                    # what any other request could observe now
                    seen.append((s.id in self.reg.sessions, len(self.reg.tombstones)))
                raise BridgeError("the headless CLI exited with code 9; see /secret/dir")
            raise AssertionError("a request reached the failed backend")

        class Lock:                                                     # signals when B starts waiting for the lock
            def acquire(self_, timeout=-1):
                if in_turn.is_set():
                    b_waiting.set()
                return real_acquire(timeout=timeout)
            def release(self_):
                s.lock_real.release()
            def locked(self_):
                return s.lock_real.locked()
        s.lock_real, s.send_user = s.lock, send_user
        s.lock = Lock()
        results = {}

        def call(name):
            results[name] = self.error_of(body)

        a = threading.Thread(target=call, args=("a",))
        with self.assertLogs("cortados.provider", "WARNING"):
            a.start()
            self.assertTrue(in_turn.wait(10))
            b = threading.Thread(target=call, args=("b",))
            b.start()
            self.assertTrue(b_waiting.wait(10))
            release.set()
            a.join(10)
            b.join(10)
        self.assertEqual(seen, [(True, 0)])                           # while A's turn ran: live, no tombstone
        self.assertEqual(len(sends), 1)                                # B never sent anything to the backend
        self.assertEqual(results["a"][0], 502)                         # A gets its own failure
        code, err, _ = results["b"]
        self.assertEqual((code, err["error"]["type"]), (400, "invalid_request_error"))
        self.assertIn("backend_exited", err["error"]["message"])
        self.assertNotIn("secret", err["error"]["message"])
        self.assertEqual((self.reg.sessions, len(self.reg.tombstones)), ({}, 1))


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


FAKE_SEARCH = """import json, os, sys, time
open(os.environ["FAKE_SEARCH_LOG"], "a").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
    "env": {k: v for k, v in os.environ.items() if k.startswith(("ANTHROPIC_", "CLAUDE_CONFIG_DIR"))}}) + "\\n")
mode = os.environ.get("FAKE_SEARCH_MODE", "ok")
if mode == "fail":
    sys.stderr.write("boom"); sys.exit(3)
if mode == "sleep":
    time.sleep(float(os.environ.get("FAKE_SEARCH_SLEEP", "5")))
def emit(e): print(json.dumps(e))
emit({"type": "system", "subtype": "init"})
if mode != "nosearch":
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ws1", "name": "WebSearch",
                                                        "input": {"query": "q"}}]}})
    if mode == "toolerror":
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "ws1", "is_error": True,
                                                       "content": "<tool_use_error>search failed</tool_use_error>"}]}})
    else:
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "ws1",
                                                       "content": "Web search results for query: q"}]}})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}})
emit({"type": "result", "subtype": "success", "is_error": False,
      "result": "1. Cortados - https://example.com/c - a router\\nAnswer: it routes.",
      "usage": {"input_tokens": 11, "output_tokens": 7, "server_tool_use": {"web_search_requests": 0}}})
"""


# The stream events of a WebSearch that ran (the shape a live CLI run produced, 2026-11-03).
SEARCH_EVIDENCE = [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ws1", "name": "WebSearch", "input": {}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "ws1", "content": "results"}]}}]


class SearchFakeMixin:
    """A fake `claude -p` for the search lane, writing each call's argv and ANTHROPIC_* env to FAKE_SEARCH_LOG."""

    def install_fake_search(self, mode="ok"):
        path = os.path.join(self.scratch, "fake_search.py")
        Path(path).write_text(FAKE_SEARCH)
        self.search_log = os.path.join(self.scratch, "search.jsonl")
        for k, v in (("FAKE_SEARCH_LOG", self.search_log), ("FAKE_SEARCH_MODE", mode)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        self.reg.search_bin = [sys.executable, path]

    def search_calls(self):
        if not os.path.exists(self.search_log):
            return []
        return [json.loads(line) for line in Path(self.search_log).read_text().splitlines()]


class ServerToolTests(SearchFakeMixin, EndpointHarness):
    """Anthropic server tools (web_search_*) cannot run on the bridged backends: incident 2026-11-03, where Claude Code's
    WebSearch got an empty success with no links. When the search lane fails, the model must read that the search did
    not happen (these tests run with a failing search CLI; SearchLaneTests covers the working lane)."""
    WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

    def setUp(self):
        super().setUp()
        self.install_fake_search("fail")
    SEARCH = {"model": "cortados", "tools": [WEB_SEARCH],
              "messages": [{"role": "user", "content": "Perform a web search for the query: cortados"}]}

    def test_server_tools_never_become_bridged_client_tools(self):
        turn = wire.parse_request({"tools": [self.WEB_SEARCH, READ_TOOL, {"type": "web_fetch_20250910", "name": "web_fetch"}],
                                   "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual([t["name"] for t in turn.tools], ["Read"])
        self.assertEqual(wire.server_tool_names({"tools": [self.WEB_SEARCH, READ_TOOL]}), ["web_search"])
        self.assertEqual(wire.server_tool_names({"tools": "bad"}), [])
        # Client-executed Anthropic-defined tools are the client's to run, so they stay.
        self.assertFalse(wire.is_server_tool({"type": "bash_20250124", "name": "bash"}))

    def test_a_web_search_only_request_gets_an_explicit_unavailable_reply_and_no_backend(self):
        r = self.post(self.SEARCH)
        self.assertEqual(r["stop_reason"], "end_turn")
        text = r["content"][0]["text"]
        self.assertIn("web_search is unavailable on this CortadOS lane", text)
        self.assertIn("WebFetch", text)
        self.assertFalse(os.path.exists(self.argv_file), "no CLI may be started for an unservable search")
        self.assertEqual(self.reg.sessions, {})

    def test_the_streamed_reply_says_the_same(self):
        raw = self.post(dict(self.SEARCH, stream=True))
        self.assertIn("event: message_start", raw)
        self.assertIn("unavailable on this CortadOS lane", raw)
        self.assertIn('"stop_reason": "end_turn"', raw)
        self.assertFalse(os.path.exists(self.argv_file))

    # The real Claude Code 2.1.317 WebSearch shape: the tool is forced, which the bridge's auto-only check refused
    # with 400 before the unavailable reply could be reached (review P1).
    REAL = {"model": "claude-sonnet-4-6", "max_tokens": 32000, "tools": [WEB_SEARCH],
            "tool_choice": {"type": "tool", "name": "web_search"},
            "messages": [{"role": "user", "content": "Perform a web search for the query: cortados router"}]}

    def test_the_real_forced_web_search_request_gets_the_unavailable_reply(self):
        r = self.post(self.REAL)
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertIn("web_search is unavailable on this CortadOS lane", r["content"][0]["text"])
        self.assertFalse(os.path.exists(self.argv_file), "no CLI may be started for an unservable search")
        self.assertEqual(self.reg.sessions, {})

    def test_the_real_forced_web_search_request_streamed(self):
        raw = self.post(dict(self.REAL, stream=True))
        self.assertIn("event: message_start", raw)
        self.assertIn("unavailable on this CortadOS lane", raw)
        self.assertIn('"stop_reason": "end_turn"', raw)
        self.assertFalse(os.path.exists(self.argv_file))

    def test_any_choice_over_server_tools_only_also_gets_the_reply(self):
        r = self.post(dict(self.REAL, tool_choice={"type": "any"}))
        self.assertIn("unavailable on this CortadOS lane", r["content"][0]["text"])

    def test_forcing_a_client_tool_is_still_refused(self):
        msgs = [{"role": "user", "content": "x"}]
        for tools, choice in (([READ_TOOL], {"type": "tool", "name": "Read"}),
                              ([self.WEB_SEARCH, READ_TOOL], {"type": "tool", "name": "Read"}),
                              ([self.WEB_SEARCH, READ_TOOL], {"type": "tool", "name": "web_search"}),
                              ([self.WEB_SEARCH, READ_TOOL], {"type": "any"}),
                              ([self.WEB_SEARCH], {"type": "tool", "name": "Read"}),
                              ([self.WEB_SEARCH], {"type": "none"}),
                              ([], {"type": "any"})):
            with self.subTest(tools=tools, choice=choice), self.assertRaises(wire.WireError):
                wire.parse_request({"tools": tools, "tool_choice": choice, "messages": msgs})
        self.assertEqual(self.status_of(dict(self.REAL, tools=[self.WEB_SEARCH, READ_TOOL])), 400)
        self.assertFalse(os.path.exists(self.argv_file))

    def test_mixed_with_client_tools_the_client_tools_are_still_served(self):
        r = self.post({"model": "cortados", "tools": [self.WEB_SEARCH, READ_TOOL],
                       "messages": [{"role": "user", "content": "please use tool"}]})
        self.assertEqual(r["stop_reason"], "tool_use")
        self.assertEqual(r["content"][-1]["name"], "Read")


class SearchLaneTests(SearchFakeMixin, EndpointHarness):
    """The search lane (search_lane.py): a web-search-only request runs a headless CLI with only WebSearch."""
    SEARCH = ServerToolTests.REAL

    def setUp(self):
        super().setUp()
        self.install_fake_search("ok")
        # A successful search writes a usage-ledger row; capture it rather than touch any real usage.db.
        self.ledger = []
        p = mock.patch("cortados_router.usage_ledger.record_async",
                       side_effect=lambda lane, model, usage: self.ledger.append((lane, model, usage)))
        p.start()
        self.addCleanup(p.stop)

    def test_query_extraction(self):
        q = search_lane.extract_query
        self.assertEqual(q(self.SEARCH), "cortados router")
        self.assertEqual(q({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "Perform a web search for the query: a b"}]}]}), "a b")
        self.assertEqual(q({"messages": [{"role": "user", "content": "plain words"}]}), "plain words")
        self.assertEqual(q({"messages": []}), "")
        self.assertEqual(q({}), "")

    def test_json_reply_carries_the_result_and_usage(self):
        r = self.post(self.SEARCH)
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertIn("https://example.com/c", r["content"][0]["text"])
        self.assertEqual(r["usage"]["output_tokens"], 7)
        self.assertFalse(os.path.exists(self.argv_file), "the bridge CLI is never started for a search")
        self.assertEqual(self.reg.sessions, {})

    def test_sse_reply_carries_the_result(self):
        raw = self.post(dict(self.SEARCH, stream=True))
        self.assertIn("event: message_start", raw)
        self.assertIn("https://example.com/c", raw)
        self.assertIn('"stop_reason": "end_turn"', raw)

    def test_argv_has_only_websearch_and_no_bridge_and_env_is_stripped(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "t",
                                          "ANTHROPIC_API_KEY": "k",
                                          "ANTHROPIC_CUSTOM_HEADERS": "x-cortados-run-id: r1"}):
            self.post(self.SEARCH)
        [call] = self.search_calls()
        a = call["argv"]
        self.assertEqual(a[a.index("--setting-sources") + 1], "")
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(json.loads(a[a.index("--settings") + 1]), {"disableAllHooks": True})
        self.assertEqual(a[a.index("--tools") + 1], "WebSearch")
        self.assertEqual(a[a.index("--allowedTools") + 1], "WebSearch")
        self.assertEqual(a[a.index("--model") + 1], search_lane.SEARCH_MODEL)
        self.assertEqual(json.loads(a[a.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertIn("--strict-mcp-config", a)
        self.assertIn("cortados router", a[a.index("-p") + 1])
        self.assertFalse(any("bridge" in x or "mcp_shim" in x for x in a))
        self.assertEqual(call["env"], {})
        self.assertIn("cortados-search-", call["cwd"])

    def test_named_account_runs_on_its_login_and_an_unknown_one_falls_back(self):
        with mock.patch.dict(os.environ, {search_lane.ACCOUNT_ENV: "b"}), \
                mock.patch.object(self.reg, "claude_config_dir", lambda acct: "/cfg/b" if acct == "b" else None):
            self.post(self.SEARCH)
        self.assertEqual(self.search_calls()[0]["env"], {"CLAUDE_CONFIG_DIR": "/cfg/b"})
        with mock.patch.dict(os.environ, {search_lane.ACCOUNT_ENV: "zz"}), \
                mock.patch.object(self.reg, "claude_config_dir", lambda acct: None):
            r = self.post(self.SEARCH)
        self.assertIn("unavailable", r["content"][0]["text"])
        self.assertIn("no_claude_lane", r["content"][0]["text"])

    def test_nonzero_exit_falls_back_to_the_unavailable_text(self):
        os.environ["FAKE_SEARCH_MODE"] = "fail"
        r = self.post(self.SEARCH)
        self.assertIn("web_search is unavailable on this CortadOS lane", r["content"][0]["text"])
        self.assertIn("exit_3", r["content"][0]["text"])

    def test_timeout_falls_back_to_the_unavailable_text(self):
        os.environ["FAKE_SEARCH_MODE"] = "sleep"
        with mock.patch.object(search_lane, "SEARCH_TIMEOUT_S", 0.5):
            r = self.post(self.SEARCH)
        self.assertIn("unavailable", r["content"][0]["text"])
        self.assertIn("timeout", r["content"][0]["text"])

    def test_past_the_cap_the_reply_says_search_busy(self):
        with mock.patch.object(search_lane, "_slots", threading.BoundedSemaphore(1)):
            search_lane._slots.acquire()
            try:
                r = self.post(self.SEARCH)
            finally:
                search_lane._slots.release()
        self.assertIn("search busy", r["content"][0]["text"])
        self.assertEqual(self.search_calls(), [])

    def test_concurrent_searches_are_capped(self):
        os.environ["FAKE_SEARCH_MODE"], os.environ["FAKE_SEARCH_SLEEP"] = "sleep", "1"
        self.addCleanup(os.environ.pop, "FAKE_SEARCH_SLEEP", None)
        out = []
        with mock.patch.object(search_lane, "_slots", threading.BoundedSemaphore(2)):
            ts = [threading.Thread(target=lambda: out.append(self.post(self.SEARCH)["content"][0]["text"]))
                  for _ in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(sum("search busy" in t for t in out), 2)
        self.assertEqual(sum("example.com" in t for t in out), 2)

    def test_a_scratch_dir_failure_is_an_unavailable_reply_and_leaks_no_slot(self):
        before = search_lane._slots._value
        with mock.patch.object(search_lane.tempfile, "mkdtemp", side_effect=OSError("disk full")):
            r = self.post(self.SEARCH)
        self.assertIn("web_search is unavailable on this CortadOS lane", r["content"][0]["text"])
        self.assertIn("start_failed", r["content"][0]["text"])
        self.assertEqual(search_lane._slots._value, before)
        self.assertEqual(self.search_calls(), [])

    def test_an_unexpected_lane_error_is_an_unavailable_reply_not_a_dropped_connection(self):
        with mock.patch.object(search_lane, "run", side_effect=RuntimeError("bug")), \
                self.assertLogs("cortados.provider", "ERROR"):
            r = self.post(self.SEARCH)
        self.assertIn("unavailable", r["content"][0]["text"])
        self.assertIn("the search lane failed: internal", r["content"][0]["text"])

    def test_an_overlong_query_is_refused_before_any_cli(self):
        body = {"model": "cortados", "tools": [ServerToolTests.WEB_SEARCH],
                "messages": [{"role": "user", "content": "q" * (search_lane.MAX_QUERY_CHARS + 1)}]}
        r = self.post(body)
        self.assertIn("query_too_long", r["content"][0]["text"])
        self.assertEqual(self.search_calls(), [])

    def test_domain_filters_reach_the_prompt(self):
        tool = dict(ServerToolTests.WEB_SEARCH, allowed_domains=["docs.python.org", ""],
                    blocked_domains=["spam.example"])
        self.post({"model": "cortados", "tools": [tool],
                   "messages": [{"role": "user", "content": "asyncio timeout"}]})
        p = self.search_calls()[0]["argv"]
        p = p[p.index("-p") + 1]
        self.assertIn("Only use results from these domains: docs.python.org.", p)
        self.assertIn("Never use results from these domains: spam.example.", p)
        self.assertEqual(search_lane.prompt("x"), search_lane.PROMPT.format(query="x"))

    def test_non_finite_usage_is_dropped(self):
        self.assertIsNone(search_lane._usage_int(float("nan")))
        self.assertIsNone(search_lane._usage_int(float("inf")))
        self.assertIsNone(search_lane._usage_int(True))
        self.assertEqual(search_lane._usage_int(7.0), 7)
        out = {"type": "result", "is_error": False, "result": "r",
               "usage": {"input_tokens": float("nan"), "output_tokens": 3}}
        done = subprocess.CompletedProcess([], 0, "\n".join(json.dumps(e) for e in SEARCH_EVIDENCE + [out]), "")
        with mock.patch.object(search_lane.subprocess, "run", return_value=done):
            res = search_lane.run("claude", "q", None)
        self.assertEqual(res.usage, {"output_tokens": 3})

    def test_a_search_is_recorded_in_the_usage_ledger_on_its_claude_lane(self):
        self.post(self.SEARCH)
        self.assertEqual(self.ledger, [("claude", search_lane.SEARCH_MODEL, {"input_tokens": 11, "output_tokens": 7})])
        self.ledger.clear()
        accts = [{"lane": "claude", "account": "a"}, {"lane": "claude#b", "account": "b"}]
        with mock.patch.dict(os.environ, {search_lane.ACCOUNT_ENV: "b"}), \
                mock.patch.object(self.reg, "claude_config_dir", lambda acct: "/cfg/b"), \
                mock.patch("cortados_router.core._vendor_accounts", return_value=accts):
            self.post(self.SEARCH)
        self.assertEqual([row[0] for row in self.ledger], ["claude#b"])
        self.ledger.clear()
        os.environ["FAKE_SEARCH_MODE"] = "fail"
        self.post(self.SEARCH)
        self.assertEqual(self.ledger, [], "a failed search spent nothing worth learning from and is not recorded")

    def test_argv_streams_events(self):
        a = search_lane.argv("claude", "q")
        self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", a)

    def test_a_reply_without_a_search_is_unavailable_and_unrecorded(self):
        os.environ["FAKE_SEARCH_MODE"] = "nosearch"
        r = self.post(self.SEARCH)
        text = r["content"][0]["text"]
        self.assertIn("web_search is unavailable on this CortadOS lane", text)
        self.assertIn("no search was performed", text)
        self.assertNotIn("example.com", text, "an answer from memory must not reach the client")
        self.assertEqual(self.ledger, [])

    def test_a_failed_websearch_tool_call_is_unavailable_and_unrecorded(self):
        os.environ["FAKE_SEARCH_MODE"] = "toolerror"
        r = self.post(self.SEARCH)
        self.assertIn("no search was performed", r["content"][0]["text"])
        self.assertEqual(self.ledger, [])

    def test_search_evidence(self):
        ev = search_lane.search_evidence
        self.assertTrue(ev(SEARCH_EVIDENCE))
        self.assertFalse(ev([]))
        self.assertFalse(ev(SEARCH_EVIDENCE[:1]), "a tool_use with no result is not a search")
        self.assertFalse(ev(SEARCH_EVIDENCE[1:]), "a result for no WebSearch call is not a search")
        other = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "ws1", "name": "Bash"}]}},
                 SEARCH_EVIDENCE[1]]
        self.assertFalse(ev(other))
        empty = [SEARCH_EVIDENCE[0], {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "ws1", "content": [{"type": "text", "text": " "}]}]}}]
        self.assertFalse(ev(empty))
        # a server-side usage count of 0 (what the live CLI reports even after a search) does not veto stream evidence
        done = SEARCH_EVIDENCE + [{"type": "result", "is_error": False, "result": "first"},
                                  {"type": "result", "is_error": False, "result": "last",
                                   "usage": {"server_tool_use": {"web_search_requests": 0}}}]
        with mock.patch.object(search_lane.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "noise\n" + "\n".join(json.dumps(e) for e in done), "")):
            self.assertEqual(search_lane.run("claude", "q", None).text, "last", "the last result line is the answer")
        with mock.patch.object(search_lane.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "\n".join(json.dumps(e) for e in SEARCH_EVIDENCE), "")):
            with self.assertRaises(search_lane.SearchFailed) as cm:
                search_lane.run("claude", "q", None)
        self.assertEqual(cm.exception.kind, "bad_output")

    def _accounts(self, rows):
        from cortados_router import core
        return mock.patch.object(core, "_vendor_accounts", return_value=rows)

    ACCTS = [{"lane": "claude", "account": "a", "config_dir": "/cfg/a"},
             {"lane": "claude#b", "account": "b", "config_dir": "/cfg/b"}]

    def test_configured_account_runs_and_is_charged_on_that_account(self):
        with mock.patch.dict(os.environ, {search_lane.ACCOUNT_ENV: "b", "CLAUDE_CONFIG_DIR": "/cfg/a"}), \
                mock.patch.object(self.reg, "claude_config_dir", lambda acct: "/cfg/b"), self._accounts(self.ACCTS):
            self.post(self.SEARCH)
        self.assertEqual(self.search_calls()[0]["env"], {"CLAUDE_CONFIG_DIR": "/cfg/b"})
        self.assertEqual([row[0] for row in self.ledger], ["claude#b"])

    def test_default_with_accounts_runs_on_the_first_account_explicitly_despite_an_inherited_dir(self):
        # The provider runs with account b's dir in its env; the default search must not inherit it while charging
        # `claude`: it runs on the first account (the bare claude lane) explicitly.
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/cfg/b"}), self._accounts(self.ACCTS):
            os.environ.pop(search_lane.ACCOUNT_ENV, None)
            self.post(self.SEARCH)
        self.assertEqual(self.search_calls()[0]["env"], {"CLAUDE_CONFIG_DIR": "/cfg/a"})
        self.assertEqual([row[0] for row in self.ledger], ["claude"])

    def test_an_inherited_dir_matching_no_account_is_not_charged_to_claude(self):
        default = [{"lane": "claude", "provider": "claude-cli", "account": None, "config_dir": None}]
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/else"}), self._accounts(default), \
                self.assertLogs("cortados_router.provider.search_lane", "WARNING") as logs:
            os.environ.pop(search_lane.ACCOUNT_ENV, None)
            r = self.post(self.SEARCH)
        self.assertIn("https://example.com/c", r["content"][0]["text"], "the search still runs")
        self.assertEqual(self.search_calls()[0]["env"], {"CLAUDE_CONFIG_DIR": "/somewhere/else"})
        self.assertEqual(self.ledger, [], "an unmatched login writes no ledger row")
        self.assertTrue(any("matches no configured account" in m for m in logs.output))

    def test_default_without_accounts_or_inherited_dir_is_charged_to_claude(self):
        default = [{"lane": "claude", "provider": "claude-cli", "account": None, "config_dir": None}]
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", search_lane.ACCOUNT_ENV)}
        with mock.patch.dict(os.environ, env, clear=True), self._accounts(default):
            self.post(self.SEARCH)
        self.assertEqual(self.search_calls()[0]["env"], {})
        self.assertEqual([row[0] for row in self.ledger], ["claude"])

    def test_unreadable_accounts_fail_the_search(self):
        from cortados_router import core
        with mock.patch.object(core, "_vendor_accounts", side_effect=core.AccountsConfigError("bad")):
            r = self.post(self.SEARCH)
        self.assertIn("no_claude_lane", r["content"][0]["text"])
        self.assertEqual(self.search_calls(), [])

    def test_forced_client_tool_choice_is_still_400(self):
        self.assertEqual(self.status_of(dict(self.SEARCH, tools=[ServerToolTests.WEB_SEARCH, READ_TOOL])), 400)
        self.assertEqual(self.search_calls(), [])

    def test_a_non_search_server_tool_is_not_sent_to_the_search_lane(self):
        r = self.post({"model": "cortados", "tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}],
                       "messages": [{"role": "user", "content": "x"}]})
        self.assertIn("unavailable", r["content"][0]["text"])
        self.assertEqual(self.search_calls(), [])


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
