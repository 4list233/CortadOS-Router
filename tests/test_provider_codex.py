"""The provider endpoint on a codex-cli route, end to end over HTTP, with tests/fixtures/fake_codex_app_server.py
standing in for `codex app-server` and tests/fixtures/fake_claude_cli.py for `claude -p`.

The router is a stub returning a codex-cli route; the fake app-server speaks the exact line shapes of the real stream
(brief §A), so these tests pin the bridge's protocol handling, its isolation command line and the factory map.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router.provider import codex_bridge
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
                  "FAKE_CODEX_MODEL", "FAKE_CODEX_SANDBOX"):
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

    def test_the_app_server_runs_isolated(self):
        os.environ["OPENAI_API_KEY"] = "sk-would-bill-an-api-account"
        try:
            self.post({"tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        finally:
            os.environ.pop("OPENAI_API_KEY")
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
                         "mcp_servers.node_repl.enabled=false", "mcp_servers.docs.enabled=false"):
            self.assertIn(override, overrides)
        self.assertEqual(len([o for o in overrides if o.startswith("mcp_servers.")]), 2)   # [mcp_servers.x.env] is not a server
        self.assertIsNone(seen["env"]["OPENAI_API_KEY"])
        self.assertEqual(seen["env"]["CODEX_HOME"], self.codex_home)
        self.assertEqual(os.listdir(seen["cwd"]), [])                   # empty scratch dir, never the project
        start = [m for m in self.inbound() if m.get("method") == "thread/start"][0]["params"]
        self.assertEqual((start["ephemeral"], start["sandbox"], start["approvalPolicy"], start["model"]),
                         (True, "read-only", "never", "gpt-test"))
        self.assertEqual(start["dynamicTools"], [{"type": "function", "name": "Read", "description": "Read a file",
                                                  "inputSchema": READ_TOOL["input_schema"]}])
        init = [m for m in self.inbound() if m.get("method") == "initialize"][0]["params"]
        self.assertTrue(init["capabilities"]["experimentalApi"])

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
            "[mcp_servers.commented] # the docs server\n[mcp_servers.'quoted.dot']\n[mcp_servers.plain.env]\nA = '1'\n")
        self.assertEqual(codex_bridge.mcp_server_names(home),
                         ["'quoted.dot'", "commented", "indented", "spaced", "trailing"])

    def test_mcp_servers_declared_any_other_way_are_refused_before_an_app_server_starts(self):
        for form in ('mcp_servers.foo.command = "x"\n', 'mcp_servers = { foo = { command = "x" } }\n',
                     '[mcp_servers]\nfoo = { command = "x" }\n', '[[mcp_servers.foo]]\ncommand = "x"\n'):
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

    def test_the_codex_version_pin_names_the_verified_release(self):
        self.assertEqual(codex_bridge.SUPPORTED_CLI_VERSIONS, ("0.163.0",))
        self.assertNotIn("code_mode_host", codex_bridge.DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
