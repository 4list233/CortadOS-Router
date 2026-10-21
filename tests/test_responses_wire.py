"""The Responses wire alone: a Codex CLI 0.163.0 request in, a ClientTurn out, and the stream Codex's parser reads.

The first fixture is a real capture with its prose replaced (tests/fixtures/codex-responses-request-0.163.0.json).
FOLLOW_UP below is built from knowledge of the Responses history item shapes (assistant message, function_call,
function_call_output: serde names verified in the binary, never captured on this wire).
"""
import json
import unittest
from pathlib import Path

from cortados_router.provider import responses_wire as wire
from cortados_router.provider import routing
from cortados_router.provider.anthropic_wire import WireError, reply_fingerprint

FIXTURE = Path(__file__).with_name("fixtures") / "codex-responses-request-0.163.0.json"
READ_TOOL = {"type": "function", "name": "Read", "description": "Read a file", "strict": False,
             "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}


def capture() -> dict:
    return json.loads(FIXTURE.read_text())


def request(*items, tools=(READ_TOOL,), **extra) -> dict:
    body = {"model": "cortados", "stream": False, "input": list(items), "tools": list(tools), "tool_choice": "auto",
            "store": False}
    body.update(extra)
    return body


def user(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


# Built from knowledge, not a capture: the request Codex would send after the endpoint answered "I will read it" and a
# Read call, and Codex ran the tool. The reasoning item is what `include: reasoning.encrypted_content` makes Codex echo.
FOLLOW_UP = request(
    {"type": "message", "id": "msg_dev", "role": "developer", "content": [{"type": "input_text", "text": "<developer prompt>"}]},
    user("summarize README.md"),
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
    {"type": "message", "id": "msg_a1", "role": "assistant", "status": "completed",
     "content": [{"type": "output_text", "text": "I will read it ", "annotations": []}]},
    {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Read", "arguments": "{\"file_path\": \"README.md\"}",
     "status": "completed"},
    {"type": "function_call_output", "call_id": "call_1", "output": "# README\nhello"},
)


class ParseCaptureTests(unittest.TestCase):
    def test_the_real_request_parses_into_system_content_and_tools(self):
        turn = wire.parse_request(capture())
        self.assertEqual(turn.system, "<developer prompt>\n\n<skills instructions>\n\n<permissions instructions>")
        # Codex's injected user blocks keep their real markers in the fixture and reach the model as sent
        agents, env = (c["text"] for c in capture()["input"][2]["content"])
        self.assertTrue(agents.startswith("# AGENTS.md instructions\n\n<INSTRUCTIONS>") and agents.endswith("</INSTRUCTIONS>"))
        self.assertTrue(env.startswith("<environment_context>") and env.endswith("</environment_context>"))
        self.assertEqual(turn.content, [{"type": "text", "text": agents}, {"type": "text", "text": env},
                                        {"type": "text", "text": "say hi"}])
        self.assertEqual(turn.tool_results, {})
        self.assertEqual(turn.history, [])
        self.assertTrue(turn.stream)
        self.assertEqual(turn.model, "cortados")
        [tool] = turn.tools
        self.assertEqual(tool["name"], "request_user_input")
        self.assertEqual(tool["input_schema"], capture()["tools"][0]["parameters"])
        self.assertNotIn("strict", tool)

    def test_the_fixture_holds_no_prose_and_no_identity(self):
        raw = FIXTURE.read_text()
        for leak in ("@", "installation_id\":\"", "/Users/"):
            self.assertNotIn(leak, raw.replace('"x"', ""))
        self.assertTrue(all(v == "x" for v in capture()["client_metadata"].values()))


class HistoryTests(unittest.TestCase):
    def test_follow_up_history_matches_the_blocks_the_endpoint_emitted(self):
        turn = wire.parse_request(FOLLOW_UP)
        emitted = [{"type": "text", "text": "I will read it "},
                   {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "README.md"}}]
        self.assertEqual(turn.history, [reply_fingerprint(emitted)])
        self.assertEqual(turn.tool_results, {"call_1": {"content": [{"type": "text", "text": "# README\nhello"}], "isError": False}})
        self.assertEqual(turn.content, [])
        self.assertEqual(turn.system, "<developer prompt>")

    def test_each_run_of_assistant_items_is_one_reply(self):
        body = request(user("a"),
                       {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "one"}]},
                       user("b"),
                       {"type": "function_call", "call_id": "c2", "name": "Read", "arguments": "{}"},
                       {"type": "function_call", "call_id": "c3", "name": "Read", "arguments": "{}"},
                       {"type": "function_call_output", "call_id": "c2", "output": "x"},
                       {"type": "function_call_output", "call_id": "c3", "output": "y"})
        turn = wire.parse_request(body)
        self.assertEqual(turn.history, [
            reply_fingerprint([{"type": "text", "text": "one"}]),
            reply_fingerprint([{"type": "tool_use", "id": "c2"}, {"type": "tool_use", "id": "c3"}])])
        self.assertEqual(list(turn.tool_results), ["c2", "c3"])

    def test_output_as_a_list_maps_text_and_images_to_mcp_content(self):
        body = request(user("go"), {"type": "function_call", "call_id": "c", "name": "Read", "arguments": "{}"},
                       {"type": "function_call_output", "call_id": "c", "output": [
                           {"type": "input_text", "text": "shot:"},
                           {"type": "input_image", "image_url": "data:image/png;base64,AA=="}]})
        turn = wire.parse_request(body)
        self.assertEqual(turn.tool_results["c"]["content"],
                         [{"type": "text", "text": "shot:"}, {"type": "image", "data": "AA==", "mimeType": "image/png"}])
        self.assertFalse(turn.tool_results["c"]["isError"])   # Responses has no error flag on an output

    def test_user_text_beside_outputs_rides_on_the_last_result(self):
        body = request(user("go"), {"type": "function_call", "call_id": "c", "name": "Read", "arguments": "{}"},
                       {"type": "function_call_output", "call_id": "c", "output": "body"}, user("<reminder>"))
        turn = wire.parse_request(body)
        self.assertEqual(turn.content, [])
        self.assertEqual(turn.tool_results["c"]["content"],
                         [{"type": "text", "text": "body"}, {"type": "text", "text": "<reminder>"}])

    def test_a_developer_item_after_the_first_reply_joins_the_turn_as_operator_text(self):
        body = request(user("a"), {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "one"}]},
                       {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "env: x"}]},
                       user("b"))
        turn = wire.parse_request(body)
        self.assertEqual(turn.content, [{"type": "text", "text": "<system-message>\nenv: x\n</system-message>"},
                                        {"type": "text", "text": "b"}])

    def test_input_images_in_the_new_turn_become_anthropic_image_blocks(self):
        turn = wire.parse_request(request({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "look"}, {"type": "input_image", "image_url": "data:image/jpeg;base64,/9j=", "detail": "high"}]}))
        self.assertEqual(turn.content[1], {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "/9j="}})

    def test_an_input_ending_with_an_assistant_item_is_refused(self):
        with self.assertRaises(WireError):
            wire.parse_request(request(user("a"), {"type": "function_call", "call_id": "c", "name": "Read", "arguments": "{}"}))
        with self.assertRaises(WireError):
            wire.parse_request(request(user("a"), {"type": "message", "role": "assistant", "content": "y"}))

    def test_bad_arguments_and_unknown_items_are_refused_by_name(self):
        with self.assertRaisesRegex(WireError, "not JSON"):
            wire.parse_request(request(user("a"), {"type": "function_call", "call_id": "c", "name": "R", "arguments": "{oops"},
                                       {"type": "function_call_output", "call_id": "c", "output": "x"}))
        with self.assertRaisesRegex(WireError, "local_shell_call"):
            wire.parse_request(request(user("a"), {"type": "local_shell_call", "call_id": "c", "action": {}}, user("b")))
        with self.assertRaisesRegex(WireError, "input_audio"):
            wire.parse_request(request({"type": "message", "role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}))


class RefusalTests(unittest.TestCase):
    def test_previous_response_id_is_refused(self):
        with self.assertRaisesRegex(WireError, "previous_response_id"):
            wire.parse_request(request(user("hi"), previous_response_id="resp_1"))

    def test_only_auto_tool_choice_is_accepted(self):
        for choice in ("required", "none", {"type": "function", "name": "Read"}):
            with self.assertRaises(WireError):
                wire.parse_request(request(user("hi"), tool_choice=choice))
        wire.parse_request(request(user("hi"), tool_choice="auto"))
        wire.parse_request(request(user("hi")))                       # absent means auto

    def test_the_model_must_be_cortados(self):
        with self.assertRaisesRegex(WireError, "model"):
            wire.parse_request(request(user("hi"), model="gpt-5"))

    def test_stream_must_be_a_boolean(self):
        with self.assertRaises(WireError):
            wire.parse_request(request(user("hi"), stream="yes"))
        self.assertTrue(wire.parse_request(request(user("hi"), stream=True)).stream)
        self.assertFalse(wire.parse_request(request(user("hi"), stream=False)).stream)

    def test_web_search_and_other_hosted_tools_are_dropped_with_one_warning(self):
        body = request(user("hi"), tools=[READ_TOOL, {"type": "web_search", "external_web_access": False},
                                           {"type": "file_search", "vector_store_ids": []}])
        with self.assertLogs("cortados_router.provider.responses_wire", level="WARNING") as logs:
            turn = wire.parse_request(body)
        self.assertEqual([t["name"] for t in turn.tools], ["Read"])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("web_search", logs.output[0])

    def test_namespace_tools_are_refused_naming_the_apps_flag(self):
        # The function_call item carries `namespace` (binary serde field), but "<namespace>__<tool>" cannot be
        # split back without the tool list at emit time, so the whole request is refused up front.
        ns = {"type": "namespace", "name": "mcp__codex_apps__chatgpt_space", "description": "<tool description>",
              "tools": [{"type": "function", "name": "_create_page", "strict": False, "parameters": {"type": "object"}}]}
        with self.assertRaisesRegex(WireError, "--disable apps"):
            wire.parse_request(request(user("hi"), tools=[READ_TOOL, ns]))
        with self.assertRaisesRegex(WireError, "--disable apps"):
            wire.parse_request(request(user("hi"), {"type": "function_call", "call_id": "c", "name": "_create_page",
                                                    "namespace": "mcp__codex_apps__chatgpt_space", "arguments": "{}"},
                                       {"type": "function_call_output", "call_id": "c", "output": "x"}))

    def test_an_empty_turn_is_refused(self):
        with self.assertRaises(WireError):
            wire.parse_request(request({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "x"}]}))


class ScoringTests(unittest.TestCase):
    def test_score_messages_strips_developer_items_and_codex_injected_context_and_keeps_role_text(self):
        # The AGENTS.md / environment_context user item Codex puts before the prompt is the client's, not the request:
        # left in, routing's "first user message" would be the user's global agent instructions on every conversation.
        self.assertEqual(wire.score_messages(capture()), [{"role": "user", "content": [{"type": "text", "text": "say hi"}]}])
        self.assertEqual(routing.request_text(wire.score_messages(capture())), "say hi")
        self.assertEqual([m["role"] for m in wire.score_messages(FOLLOW_UP)], ["user", "assistant"])
        # with --ignore-user-config only the environment block precedes the prompt; a prompt that merely mentions
        # AGENTS.md is the request
        body = request(user("<environment_context>\n  <cwd>x</cwd>\n</environment_context>"), user("update AGENTS.md please"))
        self.assertEqual(routing.request_text(wire.score_messages(body)), "update AGENTS.md please")

    def test_function_tools_are_the_only_tools_routing_sees(self):
        body = request(user("hi"), tools=[{"type": "web_search", "external_web_access": False}, READ_TOOL, "junk"])
        self.assertEqual(wire.function_tools(body), [READ_TOOL])
        self.assertEqual(wire.function_tools(request(user("hi"), tools=[{"type": "web_search"}])), [])
        self.assertEqual(wire.function_tools({"input": []}), [])

    def test_one_shot_content_is_the_single_user_message_or_the_transcript(self):
        self.assertEqual(wire.one_shot_content(request(user("title this"), tools=())), [{"type": "text", "text": "title this"}])
        [block] = wire.one_shot_content(FOLLOW_UP)
        self.assertTrue(block["text"].startswith("Conversation so far:\n"))
        self.assertIn("assistant: [tool_use]", block["text"])
        self.assertIn("user: [tool_result]", block["text"])
        self.assertNotIn("<developer prompt>", block["text"])


def parse_sse(raw: bytes) -> list[tuple[str, dict]]:
    """A tiny SSE reader: (event, data) per event; comment lines are dropped as the spec says."""
    events = []
    for chunk in raw.decode().split("\n\n"):
        lines = [l for l in chunk.split("\n") if l and not l.startswith(":")]
        if not lines:
            continue
        kind = [l[len("event: "):] for l in lines if l.startswith("event: ")]
        data = [l[len("data: "):] for l in lines if l.startswith("data: ")]
        events.append((kind[0], json.loads(data[0])))
    return events


class StreamTests(unittest.TestCase):
    BLOCKS = [{"type": "text", "text": "reading"},
              {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "README.md"}}]

    def test_sse_body_round_trips_through_a_done_items_parser(self):
        msg_id = wire.new_message_id()
        self.assertTrue(msg_id.startswith("resp_cortados_"))
        raw = wire.sse_start(msg_id, "cortados") + wire.sse_ping() + b"".join(
            wire.sse_body(self.BLOCKS, "tool_use", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3}, msg_id=msg_id))
        events = parse_sse(raw)
        self.assertEqual([e for e, _ in events], ["response.created", "response.output_item.added", "response.output_item.done",
                                                  "response.output_item.added", "response.output_item.done", "response.completed"])
        self.assertEqual([d["sequence_number"] for _, d in events], [0, 1, 2, 3, 4, 5])
        self.assertEqual(events[0][1]["response"]["status"], "in_progress")
        done = [d["item"] for e, d in events if e == "response.output_item.done"]
        self.assertEqual(done[0]["type"], "message")
        self.assertEqual(done[0]["content"], [{"type": "output_text", "text": "reading", "annotations": []}])
        self.assertEqual((done[1]["type"], done[1]["call_id"], done[1]["name"], done[1]["status"]),
                         ("function_call", "toolu_1", "Read", "completed"))
        self.assertEqual(json.loads(done[1]["arguments"]), {"file_path": "README.md"})
        self.assertTrue(done[1]["id"].startswith("fc_"))
        completed = events[-1][1]["response"]
        self.assertEqual((completed["id"], completed["model"], completed["status"]), (msg_id, "cortados", "completed"))
        self.assertEqual(completed["output"], done)        # the same item ids as the done events
        self.assertEqual(completed["usage"], {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                              "input_tokens_details": {"cached_tokens": 3},
                                              "output_tokens_details": {"reasoning_tokens": 0}})

    def test_added_events_announce_the_item_in_progress(self):
        events = parse_sse(b"".join(wire.sse_body(self.BLOCKS, "tool_use", {})))
        added = [d["item"] for e, d in events if e == "response.output_item.added"]
        self.assertEqual((added[0]["status"], added[0]["content"]), ("in_progress", []))
        self.assertEqual(added[1]["status"], "in_progress")

    def test_message_json_is_the_completed_response_object(self):
        r = wire.message_json("resp_x", "cortados", self.BLOCKS, "tool_use", {"input_tokens": 1, "output_tokens": 2})
        self.assertEqual((r["id"], r["object"], r["status"], r["model"]), ("resp_x", "response", "completed", "cortados"))
        self.assertEqual([i["type"] for i in r["output"]], ["message", "function_call"])
        self.assertEqual(r["usage"]["total_tokens"], 3)

    def test_sse_error_is_a_failed_response_and_error_json_is_the_codex_shape(self):
        [(kind, data)] = parse_sse(wire.sse_error("boom", msg_id="resp_x"))
        self.assertEqual(kind, "response.failed")
        self.assertEqual((data["response"]["status"], data["response"]["error"]["message"]), ("failed", "boom"))
        self.assertEqual(wire.error_json("invalid_request_error", "bad"),
                         {"error": {"type": "invalid_request_error", "message": "bad"}})

    def test_ping_is_an_sse_comment(self):
        self.assertEqual(wire.sse_ping(), b": ping\n\n")
        self.assertEqual(parse_sse(wire.sse_ping()), [])


if __name__ == "__main__":
    unittest.main()
