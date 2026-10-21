"""The provider endpoint on a codex-cli route, end to end over HTTP, with tests/fixtures/fake_codex_app_server.py
standing in for `codex app-server` and tests/fixtures/fake_claude_cli.py for `claude -p`.

The router is a stub returning a codex-cli route; the fake app-server speaks the exact line shapes of the real stream
(brief §A), so these tests pin the bridge's protocol handling, its isolation command line and the factory map.
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router.provider import codex_bridge
from cortados_router.provider import server as provider_server
from cortados_router.provider.bridge import BridgeError
from cortados_router.provider.server import ProviderServer, Registry

FAKE_CODEX = Path(__file__).with_name("fixtures") / "fake_codex_app_server.py"
FAKE_CLAUDE = Path(__file__).with_name("fixtures") / "fake_claude_cli.py"
TOKEN = "test-token"
READ_TOOL = {"name": "Read", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
CODEX_ROUTE = {"provider": "codex-cli", "model": "gpt-test", "band": "R2", "route_id": "r1"}


class CodexEndpointTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.argv_file = os.path.join(self.scratch, "codex-argv.json")
        self.claude_argv_file = os.path.join(self.scratch, "claude-argv.json")
        self.log_file = os.path.join(self.scratch, "codex-inbound.jsonl")
        self.codex_home = os.path.join(self.scratch, "codex-home")     # a dedicated home, never the user's ~/.codex
        os.mkdir(self.codex_home)
        Path(self.codex_home, "config.toml").write_text(
            'model = "gpt-test"\n[mcp_servers.node_repl]\ncommand = "x"\n[mcp_servers.node_repl.env]\nA = "1"\n'
            '[mcp_servers.docs]\nurl = "https://example.invalid"\n')
        os.environ["FAKE_CODEX_ARGV"] = self.argv_file
        os.environ["FAKE_CODEX_LOG"] = self.log_file
        os.environ["FAKE_CLAUDE_ARGV"] = self.claude_argv_file
        # The production settle window (2 s) exists for siblings that stream in slowly; the fake dispatches its
        # siblings at once, so the window only adds waiting here. The slow-sibling test restores the real value.
        self.settle = codex_bridge.CODEX_SETTLE_S
        codex_bridge.CODEX_SETTLE_S = 0.3
        self.route = dict(CODEX_ROUTE)
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=[sys.executable, str(FAKE_CLAUDE)],
                            codex_bin=[sys.executable, str(FAKE_CODEX)], codex_home=self.codex_home,
                            router=lambda body, client: dict(self.route), executed=lambda route: None)
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
        for k in ("FAKE_CODEX_ARGV", "FAKE_CODEX_LOG", "FAKE_CLAUDE_ARGV", "FAKE_CODEX_VERSION", "FAKE_CODEX_LEAK_MCP",
                  "FAKE_CODEX_MODEL", "FAKE_CODEX_SANDBOX", "FAKE_CODEX_ACCOUNT", "FAKE_CODEX_CONFIG_PATCH"):
            os.environ.pop(k, None)

    def request(self, body):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              "user-agent": "claude-cli/2.1.304 (external, cli)"})
        return urllib.request.urlopen(req, timeout=30)

    def post(self, body):
        with self.request(body) as r:
            return json.loads(r.read())

    def raw(self, body):
        with self.request(body) as r:
            return r.headers.get("x-cortados-route"), json.loads(r.read())

    def error_of(self, body):
        try:
            self.post(body)
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]

    def inbound(self):
        return [json.loads(l) for l in Path(self.log_file).read_text().splitlines() if l.strip()]

    def test_a_plain_turn_echoes_through_the_app_server(self):
        header, r = self.raw({"model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(header, "codex-cli/gpt-test band=R2 id=r1")
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(r["content"], [{"type": "text", "text": "echo: hi"}])
        self.assertEqual(r["usage"], {"input_tokens": 100, "output_tokens": 10})
        self.assertEqual(len(self.reg.sessions), 1)
        self.assertIsInstance(list(self.reg.sessions.values())[0], codex_bridge.CodexSession)

    def test_a_tool_call_round_trips_with_the_codex_call_id_and_usage_is_a_delta(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r1["stop_reason"], "tool_use")
        use = r1["content"][-1]
        self.assertEqual((use["type"], use["name"], use["input"]), ("tool_use", "Read", {"file_path": "f0.md"}))
        self.assertTrue(use["id"].startswith("exec-"))                  # the Codex callId, as issued
        self.assertEqual(r1["usage"], {"input_tokens": 0, "output_tokens": 0})   # nothing reported yet by the server
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["stop_reason"], "end_turn")
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual(r2["usage"], {"input_tokens": 200, "output_tokens": 20})  # both model requests of this turn
        reply = [m for m in self.inbound() if m.get("id") == 0 and "result" in m][0]
        self.assertEqual(reply["result"], {"success": True, "contentItems": [{"type": "inputText", "text": "hello"}]})
        history += [{"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}]
        r3 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r3["content"], [{"type": "text", "text": "echo: thanks"}])
        self.assertEqual(r3["usage"], {"input_tokens": 100, "output_tokens": 10})  # a delta, not the thread total
        self.assertEqual(len(self.reg.sessions), 1)
        self.assertEqual(len([m for m in self.inbound() if m.get("method") == "turn/start"]), 2)

    def test_two_outstanding_calls_are_returned_together_and_answered_by_their_own_ids(self):
        history = [{"role": "user", "content": "use two tools"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        uses = [b for b in r1["content"] if b["type"] == "tool_use"]
        self.assertEqual([u["input"] for u in uses], [{"file_path": "f0.md"}, {"file_path": "f1.md"}])
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "a"},
            {"type": "tool_result", "tool_use_id": uses[1]["id"], "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                {"type": "text", "text": "b"}], "is_error": True}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: a | ERR b"}])
        replies = {m["id"]: m["result"] for m in self.inbound() if m.get("id") in (0, 1) and "result" in m}
        self.assertEqual(replies[1]["success"], False)
        self.assertEqual(replies[1]["contentItems"][0], {"type": "inputImage", "imageUrl": "data:image/png;base64,AA=="})

    def test_sibling_calls_that_stream_in_slowly_are_returned_together(self):
        codex_bridge.CODEX_SETTLE_S = self.settle                        # the production window, under test here
        self.assertGreater(self.settle, 0.6)                             # the fake's second call comes 0.6 s later
        r1 = self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "use two tools slowly"}]})
        self.assertEqual(r1["stop_reason"], "tool_use")
        self.assertEqual([b["input"] for b in r1["content"] if b["type"] == "tool_use"],
                         [{"file_path": "f0.md"}, {"file_path": "f1.md"}])

    def test_a_thread_started_on_another_model_is_refused_with_a_502(self):
        os.environ["FAKE_CODEX_MODEL"] = "gpt-other"
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 502)
        self.assertIn("'gpt-other'", message)
        self.assertIn("'gpt-test'", message)
        self.assertEqual(self.reg.sessions, {})
        self.assertEqual([m.get("method") for m in self.inbound() if m.get("method") == "turn/start"], [])  # never ran

    def test_a_thread_that_is_not_read_only_is_refused_with_a_502(self):
        os.environ["FAKE_CODEX_SANDBOX"] = "workspaceWrite"
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 502)
        self.assertIn("workspaceWrite", message)
        self.assertEqual(self.reg.sessions, {})

    def test_a_turn_rerouted_to_another_model_is_a_502(self):
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "reroute"}]})
        self.assertEqual(code, 502)
        self.assertIn("rerouted", message)
        self.assertIn("gpt-other", message)
        self.assertEqual(self.reg.sessions, {})

    def test_a_backend_that_cannot_start_is_a_502_not_a_dropped_connection(self):
        self.reg.codex_bin = os.path.join(self.scratch, "no-such-codex")
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 502)
        self.assertIn("cannot start", message)
        self.assertIn("no-such-codex", message)
        self.assertEqual(self.reg.sessions, {})

    def test_a_failed_turn_is_a_502_naming_the_codex_error(self):
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "fail"}]})
        self.assertEqual(code, 502)
        self.assertIn("usageLimitExceeded", message)
        self.assertIn("usage limit", message)
        self.assertEqual(self.reg.sessions, {})                         # the conversation is dropped

    def test_a_leaked_builtin_tool_is_a_502(self):
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "leak builtin"}]})
        self.assertEqual(code, 502)
        self.assertIn("commandExecution", message)
        self.assertEqual(self.reg.sessions, {})

    def test_an_mcp_server_starting_is_an_isolation_failure(self):
        os.environ["FAKE_CODEX_LEAK_MCP"] = "1"
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 502)
        self.assertIn("isolation failed", message)
        self.assertEqual(self.reg.sessions, {})

    def refused(self, body, code=502):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post(body)
        self.assertEqual(cm.exception.code, code)
        return cm.exception.read().decode()

    def test_an_mcp_server_the_effective_config_still_enables_is_refused_before_the_thread_starts(self):
        # Declared in a spelling the config.toml reader cannot see (a quoted root key, a managed layer): the effective
        # config is the authority, and the refusal comes before thread/start, which is when Codex would start it.
        os.environ["FAKE_CODEX_CONFIG_PATCH"] = json.dumps({"mcp_servers": {"rogue": {"command": "/usr/bin/true", "enabled": True}}})
        text = self.refused({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertIn("rogue", text)
        self.assertIn("would not isolate", text)
        methods = [m.get("method") for m in self.inbound()]
        self.assertEqual([m for m in methods if m in ("account/read", "config/read", "thread/start")], ["account/read", "config/read"])
        self.assertEqual(self.reg.sessions, {})

    def test_a_login_that_is_not_a_chatgpt_subscription_is_refused(self):
        for account, expect in (('{"type": "apiKey"}', "not a ChatGPT subscription"), ("null", "no login")):
            with self.subTest(account=account):
                os.environ["FAKE_CODEX_ACCOUNT"] = account
                text = self.refused({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
                self.assertIn(expect, text)
        self.assertNotIn("thread/start", [m.get("method") for m in self.inbound()])

    def test_inherited_config_that_would_redirect_inference_or_re_enable_a_tool_is_refused(self):
        patches = {"model_provider": {"model_provider": "mirror"},
                   "model_providers.openai": {"model_providers": {"openai": {"base_url": "https://mirror.invalid/v1"}}},
                   "chatgpt_base_url": {"chatgpt_base_url": "https://mirror.invalid/backend-api/"},
                   "features": {"features": {"shell_tool": True}},
                   "web_search": {"web_search": "live"},
                   "project_doc_max_bytes": {"project_doc_max_bytes": 32768}}
        for name, patch in patches.items():
            with self.subTest(name):
                os.environ["FAKE_CODEX_CONFIG_PATCH"] = json.dumps(patch)
                text = self.refused({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
                self.assertIn("would not isolate", text)
                self.assertIn(name.split(".")[0], text)
        self.assertNotIn("thread/start", [m.get("method") for m in self.inbound()])

    def test_the_app_server_runs_isolated(self):
        os.environ["OPENAI_API_KEY"] = "sk-would-bill-an-api-account"
        os.environ["OPENAI_BASE_URL"] = "https://mirror.invalid/v1"
        try:
            self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        finally:
            os.environ.pop("OPENAI_API_KEY")
            os.environ.pop("OPENAI_BASE_URL")
        seen = json.loads(Path(self.argv_file).read_text())
        argv = seen["argv"]
        self.assertEqual(argv[0], "app-server")
        disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
        for feature in ("shell_tool", "unified_exec", "unified_exec_tty", "view_image", "image_generation", "apps",
                        "plugins", "hooks", "multi_agent", "sleep_tool", "skill_search", "tool_suggest", "goals",
                        "memories", "browser_use", "computer_use", "collaboration_modes"):
            self.assertIn(feature, disabled)
        self.assertNotIn("code_mode_host", disabled)                    # dynamic tools are served through it
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        for override in ("notify=[]", 'web_search="disabled"', "include_environment_context=false", "project_doc_max_bytes=0",
                         'model_provider="openai"', "mcp_servers.node_repl.enabled=false", "mcp_servers.docs.enabled=false"):
            self.assertIn(override, overrides)
        self.assertEqual(len([o for o in overrides if o.startswith("mcp_servers.")]), 2)   # [mcp_servers.x.env] is not a server
        self.assertIsNone(seen["env"]["OPENAI_API_KEY"])
        self.assertIsNone(seen["env"]["OPENAI_BASE_URL"])
        methods = [m.get("method") for m in self.inbound()]
        # The login and the effective config are checked before the thread (and with it any MCP server) starts.
        self.assertLess(methods.index("account/read"), methods.index("thread/start"))
        self.assertLess(methods.index("config/read"), methods.index("thread/start"))
        account = [m for m in self.inbound() if m.get("method") == "account/read"][0]["params"]
        self.assertIs(account["refreshToken"], False)                   # a refresh would spend the single-use token
        self.assertEqual(seen["env"]["CODEX_HOME"], self.codex_home)
        self.assertEqual(os.listdir(seen["cwd"]), [])                   # empty scratch dir, never the project
        start = [m for m in self.inbound() if m.get("method") == "thread/start"][0]["params"]
        self.assertEqual((start["ephemeral"], start["sandbox"], start["approvalPolicy"], start["model"]),
                         (True, "read-only", "never", "gpt-test"))
        self.assertEqual(start["dynamicTools"], [{"type": "function", "name": "Read", "description": "Read a file",
                                                  "inputSchema": READ_TOOL["input_schema"]}])
        init = [m for m in self.inbound() if m.get("method") == "initialize"][0]["params"]
        self.assertTrue(init["capabilities"]["experimentalApi"])

    def test_client_tools_named_like_codex_tools_are_aliased_on_the_wire_and_mapped_back(self):
        # Codex as a client sends exec_command, write_stdin, request_user_input, view_image, ...: Codex as the backend
        # would never offer a dynamic tool of those names to the model (verified live), so they go in under client_
        # and the tool_use the client receives carries its own name again.
        exec_tool = {"name": "exec_command", "description": "run", "input_schema": {"type": "object"}}
        r = self.post({"tools": [exec_tool, READ_TOOL], "messages": [{"role": "user", "content": "use tool"}]})
        start = [m for m in self.inbound() if m.get("method") == "thread/start"][0]["params"]
        self.assertEqual([t["name"] for t in start["dynamicTools"]], ["client_exec_command", "Read"])
        [call] = [b for b in r["content"] if b["type"] == "tool_use"]
        self.assertEqual(call["name"], "exec_command")                 # the fake calls its first dynamic tool
        self.assertEqual(r["stop_reason"], "tool_use")

    def test_a_client_gone_before_the_first_event_still_closes_the_one_shot_backend(self):
        # A tool-less streaming request whose client disconnects while the backend starts: the first SSE write fails
        # before the worker's cleanup exists. The one-shot's app-server is in no registry, so it must be closed here.
        closed, orig_close, orig_start = [], codex_bridge.CodexSession.close, provider_server.ANTHROPIC.stream_start

        def close(s):
            closed.append(s.id)
            orig_close(s)

        def gone(*a):
            raise BrokenPipeError("client hung up")
        codex_bridge.CodexSession.close, provider_server.ANTHROPIC.stream_start = close, gone
        try:
            for body in ({"stream": True, "messages": [{"role": "user", "content": "hi"}]},             # one-shot
                         {"stream": True, "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]}):  # opening a kept session
                try:
                    self.post(body)
                except Exception:
                    pass                                                 # the connection ends without a body
        finally:
            codex_bridge.CodexSession.close, provider_server.ANTHROPIC.stream_start = orig_close, orig_start
        self.assertEqual(len(closed), 2)
        self.assertEqual(self.reg.sessions, {})                          # the just-opened session is not left for the reaper
        self.assertEqual([d for d in os.listdir(self.scratch) if d.startswith("cortados-codex-")], [])

    def test_a_changed_system_prompt_on_a_continuation_is_served_under_the_original_with_a_warning(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"system": "A", "tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        with self.assertLogs("cortados.provider", level="WARNING") as logs:
            r2 = self.post({"system": "B", "tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertTrue(any("system prompt changed mid-conversation" in line for line in logs.output))
        start = [m for m in self.inbound() if m.get("method") == "thread/start"]
        self.assertEqual([m["params"]["baseInstructions"] for m in start], ["A"])   # one thread, the original prompt

    def test_the_system_prompt_is_the_thread_base_instructions(self):
        self.post({"system": "Be terse.", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        start = [m for m in self.inbound() if m.get("method") == "thread/start"][0]["params"]
        self.assertEqual(start["baseInstructions"], "Be terse.")

    def test_an_unsupported_cli_version_is_refused_with_a_502(self):
        os.environ["FAKE_CODEX_VERSION"] = "0.1.0"
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 502)
        self.assertIn("0.1.0", message)
        self.assertEqual(self.reg.sessions, {})

    def test_a_new_user_message_while_a_call_is_pending_fails_the_call_and_starts_a_new_turn(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r1["stop_reason"], "tool_use")
        history += [{"role": "assistant", "content": r1["content"]}, {"role": "user", "content": "never mind, hello"}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "echo: never mind, hello"}])
        inbound = self.inbound()
        failed = [m for m in inbound if m.get("id") == 0 and "result" in m][0]["result"]
        self.assertEqual(failed["success"], False)
        self.assertEqual(failed["contentItems"][0]["text"], codex_bridge.MOVED_ON)
        methods = [m.get("method") for m in inbound if m.get("method") in ("turn/start", "turn/interrupt")]
        self.assertEqual(methods, ["turn/start", "turn/interrupt", "turn/start"])
        self.assertEqual(len(self.reg.sessions), 1)
        # a late result for the abandoned call belongs to a history the endpoint no longer holds: 409, never replayed
        stale = history[:2] + [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": r1["content"][-1]["id"], "content": "late"}]}]
        self.assertEqual(self.error_of({"tools": [READ_TOOL], "messages": stale})[0], 409)

    def test_a_user_input_request_is_answered_empty_and_the_turn_continues(self):
        r = self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "ask user"}]})
        self.assertEqual(r["content"], [{"type": "text", "text": "echo: ask user"}])
        answer = [m for m in self.inbound() if m.get("id") == 0 and "result" in m][0]["result"]
        self.assertEqual(answer, {"answers": {}})

    def test_a_toolless_request_gets_its_own_throwaway_app_server(self):
        r = self.post({"messages": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                                    {"role": "user", "content": "title?"}]})
        self.assertTrue(r["content"][0]["text"].startswith("echo: Conversation so far:\nuser: a\nassistant: b"))
        self.assertEqual(self.reg.sessions, {})

    def test_a_claude_cli_route_still_runs_the_claude_cli(self):
        self.route = {"provider": "claude-cli", "model": "claude-test-model", "band": "R1", "route_id": "r9"}
        header, r = self.raw({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(header, "claude-cli/claude-test-model band=R1 id=r9")
        self.assertEqual(r["content"], [{"type": "text", "text": "echo: hi"}])
        argv = json.loads(Path(self.claude_argv_file).read_text())["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-test-model")
        self.assertFalse(Path(self.argv_file).exists())                 # no app-server started

    def test_an_unserved_provider_is_a_503_and_starts_nothing(self):
        self.route = {"provider": "agy-cli", "model": "gemini-x", "band": "R1", "route_id": "r9"}
        code, message = self.error_of({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 503)
        self.assertIn("agy-cli", message)
        self.assertEqual(self.reg.sessions, {})
        self.assertFalse(Path(self.argv_file).exists())
        self.assertFalse(Path(self.claude_argv_file).exists())

    def test_streaming_on_codex_is_valid_sse(self):
        with self.request({"stream": True, "tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool"}]}) as r:
            raw = r.read().decode()
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertEqual([e["content_block"]["type"] for e in events if e["type"] == "content_block_start"], ["tool_use"])


class CodexSessionUnitTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.codex_home = os.path.join(self.scratch, "codex-home")
        os.mkdir(self.codex_home)
        Path(self.codex_home, "config.toml").write_text('model = "gpt-test"\n')
        self.settle = codex_bridge.CODEX_SETTLE_S

    def tearDown(self):
        codex_bridge.CODEX_SETTLE_S = self.settle
        os.environ.pop("FAKE_CODEX_ARGV", None)

    def session(self, **kw):
        kw.setdefault("codex_bin", [sys.executable, str(FAKE_CODEX)])
        kw.setdefault("codex_home", self.codex_home)
        return codex_bridge.CodexSession(system="", tools=[READ_TOOL], model="gpt-test", scratch_root=self.scratch, **kw)

    def test_mcp_server_names_come_from_the_table_headers_only(self):
        home = tempfile.mkdtemp()
        Path(home, "config.toml").write_text('[mcp_servers.a]\nx=1\n[mcp_servers.a.env]\ny=2\n[mcp_servers."b c"]\nz=3\n[other]\n')
        self.assertEqual(codex_bridge.mcp_server_names(home), ['"b c"', "a"])
        self.assertEqual(codex_bridge.mcp_server_names(tempfile.mkdtemp()), [])

    def test_mcp_table_headers_in_every_toml_spelling_name_the_server_exactly(self):
        # Indented (how nested [mcp_servers.x.env] tables are usually written), spaces inside the brackets, a trailing
        # space, a trailing comment and a single-quoted key: all valid TOML Codex starts a server from. The captured
        # name carries no whitespace, because it becomes the `-c mcp_servers.<name>.enabled=false` key.
        home = tempfile.mkdtemp()
        Path(home, "config.toml").write_text(
            "  [mcp_servers.indented]\ncommand = 'x'\n[ mcp_servers.spaced ]\n[mcp_servers.trailing ]\n"
            "[mcp_servers.commented] # the docs server\n[mcp_servers.'quoted.dot']\n[mcp_servers.plain.env]\nA = '1'\n"
            '["mcp_servers".rootquoted]\ncommand = "x"\n')
        self.assertEqual(codex_bridge.mcp_server_names(home),
                         ["'quoted.dot'", "commented", "indented", "rootquoted", "spaced", "trailing"])

    def test_mcp_servers_declared_any_other_way_are_refused_before_an_app_server_starts(self):
        for form in ('mcp_servers.foo.command = "x"\n', 'mcp_servers = { foo = { command = "x" } }\n',
                     '[mcp_servers]\nfoo = { command = "x" }\n', '[[mcp_servers.foo]]\ncommand = "x"\n',
                     '["mcp_servers"]\nfoo = { command = "x" }\n', '"mcp_servers".foo.command = "x"\n'):
            home = tempfile.mkdtemp()
            Path(home, "config.toml").write_text('model = "m"\n' + form)
            with self.assertRaisesRegex(BridgeError, r"\[mcp_servers\.<name>\] tables"):
                codex_bridge.mcp_server_names(home)
            with self.assertRaises(BridgeError):
                self.session(codex_home=home)
            self.assertEqual(os.listdir(self.scratch), ["codex-home"])  # the scratch dir of the refused session is gone

    def test_leaked_items_include_the_sleep_and_image_generation_tools(self):
        s = object.__new__(codex_bridge.CodexSession)                    # _note_notification needs no process
        s.id, s._usage_total, s._fatal = "unit", {}, None
        for itype in ("sleep", "imageGeneration", "commandExecution"):
            with self.assertRaisesRegex(BridgeError, itype):
                s._note_notification({"method": "item/started", "params": {"item": {"type": itype, "id": "i1"}}})
        s._note_notification({"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "m1"}}})
        # Codex's own MCP resource tools (server "codex") run inside the app-server against servers the config check
        # proved disabled: not a leak. Any other mcpToolCall is.
        s._note_notification({"method": "item/started", "params": {"item": {"type": "mcpToolCall", "id": "r1", "server": "codex",
                                                                          "tool": "list_mcp_resources", "arguments": {}}}})
        with self.assertRaisesRegex(BridgeError, "mcpToolCall"):
            s._note_notification({"method": "item/started", "params": {"item": {"type": "mcpToolCall", "id": "r2", "server": "docs",
                                                                              "tool": "list_mcp_resources"}}})
        with self.assertRaisesRegex(BridgeError, "mcpToolCall"):
            s._note_notification({"method": "item/started", "params": {"item": {"type": "mcpToolCall", "id": "r3", "server": "codex",
                                                                              "tool": "search"}}})

    def test_a_binary_that_cannot_start_is_a_bridge_error_and_leaks_no_scratch_dir(self):
        with self.assertRaisesRegex(BridgeError, "cannot start .*no-such-codex"):
            self.session(codex_bin=os.path.join(self.scratch, "no-such-codex"))
        self.assertEqual(os.listdir(self.scratch), ["codex-home"])

    def test_a_relative_codex_home_reaches_the_child_as_an_absolute_path(self):
        os.environ["FAKE_CODEX_ARGV"] = os.path.join(self.scratch, "argv.json")
        relative = os.path.relpath(self.codex_home)                      # relative to this process's cwd
        self.assertFalse(os.path.isabs(relative))
        s = self.session(codex_home=relative)
        try:
            seen = json.loads(Path(self.scratch, "argv.json").read_text())
        finally:
            s.close()
        self.assertEqual(seen["env"]["CODEX_HOME"], os.path.abspath(self.codex_home))
        self.assertNotEqual(seen["cwd"], os.getcwd())                   # the child's cwd is the scratch work dir

    def test_a_turn_that_completes_around_a_delivered_result_is_refused_not_an_empty_reply(self):
        codex_bridge.CODEX_SETTLE_S = 0.05                               # return the call before the fake completes
        s = self.session()
        try:
            s.send_user([{"type": "text", "text": "complete around call"}], timeout=10)
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "tool_use")
            [call] = blocks
            time.sleep(1.0)                                              # the fake's turn/completed is queued by now
            s.deliver({call["id"]: {"content": [{"type": "text", "text": "late"}], "isError": False}})
            with self.assertRaisesRegex(BridgeError, "without consuming the delivered tool results"):
                s.run_turn(10)
        finally:
            s.close()

    def test_the_effective_config_check_accepts_the_real_isolated_config_and_names_every_violation(self):
        # The shape config/read answered on Codex CLI 0.163.0 under the bridge's own command line (P2 probe).
        real = {"model_provider": "openai", "model_providers": {}, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                "web_search": "disabled", "project_doc_max_bytes": 0, "notify": [], "include_environment_context": False,
                "mcp_servers": {"node_repl": {"enabled": False}, "computer-use": {"enabled": False},
                                "openaiDeveloperDocs": {"url": "https://developers.openai.com/mcp", "enabled": False}},
                "features": {**{f: False for f in codex_bridge.DISABLED_FEATURES}, "code_mode_host": True, "mentions_v2": True}}
        codex_bridge.check_effective_config(real)
        broken = dict(real, model_provider="mirror", chatgpt_base_url="https://mirror.invalid/", web_search="live",
                      model_providers={"openai": {"base_url": "https://mirror.invalid/v1"}},
                      mcp_servers={**real["mcp_servers"], "rogue": {"command": "x", "enabled": True}, "quiet": {"command": "y"}},
                      features={**real["features"], "shell_tool": True, "apps": None})
        with self.assertRaises(BridgeError) as cm:
            codex_bridge.check_effective_config(broken)
        text = str(cm.exception)
        for needle in ('model_provider="mirror"', "chatgpt_base_url", 'web_search="live"', "model_providers.openai",
                       "['quiet', 'rogue']", "['apps', 'shell_tool']"):
            self.assertIn(needle, text)
        with self.assertRaisesRegex(BridgeError, "no login"):
            codex_bridge.check_account({"account": None, "requiresOpenaiAuth": True})
        with self.assertRaisesRegex(BridgeError, "not a ChatGPT subscription"):
            codex_bridge.check_account({"account": {"type": "apiKey"}})
        codex_bridge.check_account({"account": {"type": "chatgpt", "planType": "pro"}})

    def test_run_turn_keeps_its_deadline_while_notifications_keep_arriving(self):
        # A server that streams notifications without end re-arms the settle window on every one; the absolute
        # deadline must still end the turn: with calls collected they are returned, with none it is a failure.
        s = object.__new__(codex_bridge.CodexSession)
        s.id, s._fatal, s._usage_total, s._usage_mark = "unit", None, {}, {}
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {}, set(), set(), {}
        stop, settle = threading.Event(), codex_bridge.CODEX_SETTLE_S

        def chatter():
            while not stop.is_set():
                s._events.put(("notify", {"method": "item/agentMessage/delta", "params": {"delta": "x"}}))
                time.sleep(0.02)
        threading.Thread(target=chatter, daemon=True).start()
        try:
            codex_bridge.CODEX_SETTLE_S = 5.0
            t0 = time.time()
            with self.assertRaisesRegex(BridgeError, "did not finish the turn within"):
                s.run_turn(0.3)
            self.assertLess(time.time() - t0, 2.0)
            s._events.put(("request", {"method": "item/tool/call", "id": 0,
                                       "params": {"callId": "exec-1", "tool": "Read", "arguments": {"file_path": "a"}}}))
            t0 = time.time()
            blocks, stop_reason, _ = s.run_turn(0.3)
            self.assertEqual((stop_reason, blocks[0]["id"]), ("tool_use", "exec-1"))
            self.assertLess(time.time() - t0, 2.0)
        finally:
            stop.set()
            codex_bridge.CODEX_SETTLE_S = settle

    def test_tool_aliases_cover_the_codex_client_tool_set_and_refuse_a_taken_alias(self):
        codex_client = [{"name": n} for n in ("exec_command", "write_stdin", "request_user_input", "view_image",
                                              "get_goal", "create_goal", "update_goal", "apply_patch", "update_plan")]
        aliases = codex_bridge.tool_aliases(codex_client + [{"name": "Read"}])
        self.assertEqual(aliases, {"client_" + t["name"]: t["name"] for t in codex_client})
        self.assertEqual(codex_bridge.tool_aliases([{"name": "Read"}, {"name": "Edit"}]), {})
        with self.assertRaisesRegex(BridgeError, "client_sleep"):
            codex_bridge.tool_aliases([{"name": "sleep"}, {"name": "client_sleep"}])

    def test_abandoning_a_turn_keeps_its_deadline_while_events_keep_arriving(self):
        # The drain after turn/interrupt must end at its deadline even when the queue never runs dry.
        s = object.__new__(codex_bridge.CodexSession)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.thread_id, s._turn_id = "unit", None, {}, {}, "t1", "turn-1"
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {"exec-1": 0}, {"exec-1"}, set(), {}
        s._reply, s._call = (lambda *a, **k: None), (lambda *a, **k: {})
        stop, result = threading.Event(), {}

        def chatter():
            while not stop.is_set():
                s._events.put(("notify", {"method": "error", "params": {"error": {"message": "x"}, "willRetry": True}}))
                time.sleep(0.005)

        def abandon():
            try:
                s._abandon_turn(time.time() + 0.2)
            except BridgeError as exc:
                result["error"] = exc
        threading.Thread(target=chatter, daemon=True).start()
        worker = threading.Thread(target=abandon, daemon=True)
        worker.start()
        worker.join(3)
        stop.set()
        self.assertFalse(worker.is_alive(), "the drain outlived its deadline")
        self.assertIn("did not complete the interrupted turn", str(result.get("error")))
        self.assertEqual(s._pending, {})

    def test_abandonment_writes_share_the_request_deadline_and_an_expired_request_starts_no_turn(self):
        # Four pending calls, a server whose stdin takes 120 ms per line, 25 ms left: one write at most, then the
        # deadline, never four fresh REPLY_WRITE_S budgets. And with nothing pending, an expired deadline must not
        # reach turn/start.
        class SlowStdin:
            def write(self, line):
                time.sleep(0.12)

            def flush(self):
                pass

        class Proc:
            stdin, killed = SlowStdin(), False

            def kill(self):
                self.killed = True
        s = object.__new__(codex_bridge.CodexSession)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.thread_id, s._turn_id, s.proc = "unit", None, {}, {}, "t1", "turn-1", Proc()
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {f"exec-{i}": i for i in range(4)}, set(), set(), {}
        s.route, s.last_used, s.dir = None, 0.0, Path(self.scratch)
        t0 = time.time()
        with self.assertRaises(BridgeError):
            s._abandon_turn(time.time() + 0.025)
        self.assertLess(time.time() - t0, 0.3)
        s._pending = {f"exec-{i}": i for i in range(4)}
        s.emitted = set(s._pending)
        t0 = time.time()
        with self.assertRaises(BridgeError):                            # the write is cut at the deadline, or refused after it
            s.deliver({c: {"content": [{"type": "text", "text": "r"}], "isError": False} for c in s._pending}, timeout=0.025)
        self.assertLess(time.time() - t0, 0.3)                           # not four fresh REPLY_WRITE_S budgets
        calls = []
        s._pending, s._call = {}, lambda method, *a, **k: calls.append(method) or {"turn": {"id": "x"}}
        with self.assertRaisesRegex(BridgeError, "deadline passed before the turn could start"):
            s.send_user([{"type": "text", "text": "late"}], timeout=-1)
        self.assertEqual(calls, [])

    def test_usage_is_reported_in_the_messages_shape_with_the_cached_part_taken_out_of_codex_input(self):
        s = object.__new__(codex_bridge.CodexSession)
        s._delivered, s._usage_mark = set(), {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 5}
        s._usage_total = {"totalTokens": 12419, "inputTokens": 12309, "cachedInputTokens": 3968, "cacheWriteInputTokens": 10,
                          "outputTokens": 110, "reasoningOutputTokens": 42}            # the live spike's second reading
        _, _, usage = s._finish([], "end_turn")
        self.assertEqual(usage, {"input_tokens": (12309 - 100) - (3968 - 40), "output_tokens": 105, "cache_read_input_tokens": 3928,
                                 "cache_creation_input_tokens": 10, "reasoning_output_tokens": 42})
        self.assertEqual(s._usage_mark, s._usage_total)

    def test_a_defensive_reply_inside_a_turn_is_bounded_by_the_request_deadline(self):
        class SlowStdin:
            def write(self, line):
                time.sleep(0.12)

            def flush(self):
                pass

        class Proc:
            stdin, killed = SlowStdin(), False

            def kill(self):
                self.killed = True
        s = object.__new__(codex_bridge.CodexSession)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.proc, s.dir = "unit", None, {}, {}, Proc(), Path(self.scratch)
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {}, set(), set(), {}
        s._events.put(("request", {"method": "item/tool/requestUserInput", "id": 7, "params": {"questions": []}}))
        t0 = time.time()
        with self.assertRaises(BridgeError):
            s.run_turn(0.025)
        self.assertLess(time.time() - t0, 0.3)                           # not a fresh REPLY_WRITE_S for the answer

    def test_the_codex_version_pin_names_the_verified_release(self):
        self.assertEqual(codex_bridge.SUPPORTED_CLI_VERSIONS, ("0.163.0",))
        self.assertNotIn("code_mode_host", codex_bridge.DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
