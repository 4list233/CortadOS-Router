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

from cortados_router.provider import codex_bridge, codex_isolation, routing
from cortados_router.provider import server as provider_server
from cortados_router.provider.bridge import BridgeError
from cortados_router.provider.server import ProviderServer, Registry

FAKE_CODEX = Path(__file__).with_name("fixtures") / "fake_codex_app_server.py"
FAKE_CLAUDE = Path(__file__).with_name("fixtures") / "fake_claude_cli.py"
TOKEN = "test-token"
READ_TOOL = {"name": "Read", "description": "Read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}
CODEX_ROUTE = {"provider": "codex-cli", "model": "gpt-test", "band": "R2", "route_id": "r1"}


def late_state(s):
    """The late-result state CodexSession.__init__ sets, for sessions the unit tests build without a process."""
    s._call_tools, s._turn_done, s._late, s._followups, s._turn_id = {}, False, [], 0, None


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

    def test_a_route_flagged_client_error_answers_400_not_503(self):
        # a sub/ pin naming a model this endpoint does not serve is the client's to fix (routing.choose_pinned)
        self.route = {"error": "sub/x is not a Claude model this endpoint serves", "client_error": True}
        code, msg = self.error_of({"model": "sub/x", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 400)
        self.assertIn("not a Claude model", msg)
        self.route = {"error": "every Claude subscription account is spent"}
        code, _ = self.error_of({"model": "sub/opus", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 503)
        self.route = {"error": "sub/opus: the request does not fit any Claude account's input ceiling", "fit_blocked": True}
        code, msg = self.error_of({"model": "sub/opus", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 413)                          # deterministic: a retry of the same input cannot succeed
        self.assertIn("does not fit", msg)

    def test_the_exclude_header_reaches_the_router_and_refuses_an_api_pin_on_that_model(self):
        seen = {}

        def router(body, client, exclude_model=None):
            seen["exclude_model"] = exclude_model
            return dict(self.route)
        self.reg.router = router
        headers = {"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                   provider_server.EXCLUDE_MODEL_HEADER: "claude-opus-5-5[1m]"}

        def send(body):
            req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())["error"]["message"]
        code, _ = send({"model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 200)
        self.assertEqual(seen["exclude_model"], "claude-opus-5-5[1m]")
        seen.clear()
        code, msg = send({"model": "api/claude-opus-5-5", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 400)
        self.assertIn("api/claude-opus-5-5 is the excluded model", msg)
        self.assertEqual(seen, {})                           # refused before any routing or spend

    def send_with(self, body, headers):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]

    def test_an_exclude_header_that_is_not_a_model_id_answers_400(self):
        calls = []
        self.reg.router = lambda body, client, exclude_model=None: calls.append(exclude_model) or dict(self.route)
        h = provider_server.EXCLUDE_MODEL_HEADER
        for bad in ("x" * 129, "claude opus", "opus;rm", "op%us", "a,b c", "<opus>", ",".join(["m"] * 9)):
            for model in ("cortados", "api/claude-sonnet-5"):
                code, err = self.send_with({"model": model, "tools": [READ_TOOL],
                                            "messages": [{"role": "user", "content": "hi"}]}, {h: bad})
                self.assertEqual((code, err["type"]), (400, "invalid_request_error"), (bad, model))
                self.assertIn("x-cortados-exclude-model is not a model id", err["message"])
        self.assertEqual(calls, [])                          # refused before routing (or any api/ spend)
        code, _ = self.send_with({"model": "cortados", "tools": [READ_TOOL],
                                  "messages": [{"role": "user", "content": "hi"}]}, {h: "x" * 128})
        self.assertEqual(code, 200)                          # 128 characters is the limit, not over it
        self.assertEqual(calls, ["x" * 128])
        calls.clear()
        code, _ = self.send_with({"model": "cortados", "tools": [READ_TOOL],
                                  "messages": [{"role": "user", "content": "hi"}]}, {h: " a , ,b," + ",m" * 6})
        self.assertEqual(code, 200)                          # 8 items (empty ones ignored) is the limit
        self.assertEqual(calls, ["a,b" + ",m" * 6])

    def test_an_api_pin_on_any_listed_excluded_model_answers_400(self):
        self.reg.router = lambda body, client, exclude_model=None: self.fail("an api/ pin is never routed")
        code, err = self.send_with({"model": "api/claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
                                   {provider_server.EXCLUDE_MODEL_HEADER: "claude-opus-5-5,sonnet"})
        self.assertEqual(code, 400)
        self.assertIn("api/claude-sonnet-5 is the excluded model", err["message"])

    def test_the_exclude_header_on_a_continuing_conversation_is_not_passed_to_the_router(self):
        calls = []
        self.reg.router = lambda body, client, exclude_model=None: calls.append(exclude_model) or dict(self.route)
        body = self.continuation("cortados", "cortados")     # opens the conversation without the header
        self.assertEqual(calls, [None])
        code, r2 = self.send_with(body, {provider_server.EXCLUDE_MODEL_HEADER: "claude-opus-5-5"})
        self.assertEqual(code, 200)
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual(calls, [None])                      # the route was chosen once; the header did not re-route

    def test_an_exclusion_client_error_answers_400(self):
        self.reg.router = lambda body, client, exclude_model=None: {
            "error": "No authenticated, available model fits. (the client excluded gpt-x via x-cortados-exclude-model)",
            "client_error": True}
        code, err = self.send_with({"model": "cortados", "tools": [READ_TOOL],
                                    "messages": [{"role": "user", "content": "hi"}]},
                                   {provider_server.EXCLUDE_MODEL_HEADER: "gpt-x"})
        self.assertEqual((code, err["type"]), (400, "invalid_request_error"))

    def pin_router(self):
        # a stand-in for routing.choose: a sub/ pin runs the pinned model, anything else S1's route
        def route(body, client):
            p = routing.sub_pin(body)
            return dict(CODEX_ROUTE, model=p, band="pin", pinned=True) if p else dict(CODEX_ROUTE)
        self.reg.router = route

    def continuation(self, first_model, next_model):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"model": first_model, "tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        return {"model": next_model, "tools": [READ_TOOL], "messages": history}

    def test_a_pinned_conversation_continues_under_the_same_pin_or_cortados(self):
        self.pin_router()
        body = self.continuation("sub/opus", "sub/claude-opus-5-5")     # alias and full name are the same pin
        r2 = self.post(body)
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        [session] = self.reg.sessions.values()
        self.assertEqual(session.route["model"], "claude-opus-5-5")
        # `cortados` on a pinned conversation continues: the pinned model keeps running
        body = dict(body, model="cortados", messages=body["messages"] + [
            {"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}])
        self.assertEqual(self.post(body)["content"], [{"type": "text", "text": "echo: thanks"}])
        self.assertEqual(session.route["model"], "claude-opus-5-5")

    def test_switching_the_pinned_model_mid_conversation_is_refused(self):
        self.pin_router()
        code, msg = self.error_of(self.continuation("sub/opus", "sub/sonnet"))
        self.assertEqual(code, 409)
        self.assertIn("fixed per conversation", msg)
        self.assertIn("sub/claude-opus-5-5", msg)
        self.assertIn("sub/claude-sonnet-5", msg)
        [session] = self.reg.sessions.values()                          # the conversation itself is kept
        self.assertEqual(session.route["model"], "claude-opus-5-5")

    def test_pinning_an_s1_routed_conversation_is_refused(self):
        self.pin_router()
        code, msg = self.error_of(self.continuation("cortados", "sub/opus"))
        self.assertEqual(code, 409)
        self.assertIn("routed by S1 to gpt-test", msg)
        self.assertIn("new conversation", msg)

    def test_a_plain_turn_echoes_through_the_app_server(self):
        header, r = self.raw({"model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(header, "codex-cli/gpt-test band=R2 id=r1")
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(r["content"], [{"type": "text", "text": "echo: hi"}])
        self.assertEqual(r["usage"], {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        self.assertEqual(len(self.reg.sessions), 1)
        self.assertIsInstance(list(self.reg.sessions.values())[0], codex_bridge.CodexSession)

    def test_a_tool_call_round_trips_with_the_codex_call_id_and_usage_is_a_delta(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r1["stop_reason"], "tool_use")
        use = r1["content"][-1]
        self.assertEqual((use["type"], use["name"], use["input"]), ("tool_use", "Read", {"file_path": "f0.md"}))
        self.assertTrue(use["id"].startswith("exec-"))                  # the Codex callId, as issued
        self.assertEqual((r1["usage"]["input_tokens"], r1["usage"]["output_tokens"]), (0, 0))   # nothing reported yet by the server
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        r2 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r2["stop_reason"], "end_turn")
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual((r2["usage"]["input_tokens"], r2["usage"]["output_tokens"]), (200, 20))  # both model requests of this turn
        reply = [m for m in self.inbound() if m.get("id") == 0 and "result" in m][0]
        self.assertEqual(reply["result"], {"success": True, "contentItems": [{"type": "inputText", "text": "hello"}]})
        history += [{"role": "assistant", "content": r2["content"]}, {"role": "user", "content": "thanks"}]
        r3 = self.post({"tools": [READ_TOOL], "messages": history})
        self.assertEqual(r3["content"], [{"type": "text", "text": "echo: thanks"}])
        self.assertEqual((r3["usage"]["input_tokens"], r3["usage"]["output_tokens"]), (100, 10))  # a delta, not the thread total
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
                   "openai_base_url": {"openai_base_url": "http://127.0.0.1:11434/api/codex/v1"},   # `ollama launch codex`
                   "model_catalog_json": {"model_catalog_json": "/x/ollama-launch-models.json"},
                   "features": {"features": {"shell_tool": True}},
                   "code_mode_host": {"features": {"code_mode_host": False}},
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
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--enable"], ["code_mode_host"])
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
        for override in ("notify=[]", 'web_search="disabled"', "include_environment_context=false", "project_doc_max_bytes=0",
                         'model_provider="openai"', "mcp_servers.node_repl.enabled=false", "mcp_servers.docs.enabled=false",
                         f"code_mode.default_exec_yield_time_ms={int(provider_server.TURN_TIMEOUT_S * 1000)}"):
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

    def test_mcp_and_odd_client_tool_names_reach_codex_encoded_and_come_back_original(self):
        # Codex refuses mcp__ names ("dynamic tool name is reserved", -32600) and anything outside ^[a-zA-Z0-9_-]{1,64}$;
        # the fake enforces the same rule, so this thread only starts because the names were encoded.
        tools = [{"name": "mcp__cortados-route__route_task", "description": "route", "input_schema": {"type": "object"}},
                 {"name": "fs.read", "input_schema": {"type": "object"}}, READ_TOOL]
        r = self.post({"tools": tools, "messages": [{"role": "user", "content": "use tool"}]})
        start = [m for m in self.inbound() if m.get("method") == "thread/start"][0]["params"]
        declared = [t["name"] for t in start["dynamicTools"]]
        self.assertEqual(declared[0], "cx_mcp_cortados-route__route_task")
        self.assertTrue(declared[1].startswith("fs_read_"))
        self.assertEqual(declared[2], "Read")
        [call] = [b for b in r["content"] if b["type"] == "tool_use"]
        self.assertEqual(call["name"], "mcp__cortados-route__route_task")   # the client's own name, not the wire name

    def test_a_tool_name_codex_still_refuses_is_named_in_the_error(self):
        tools = [{"name": "mcp__srv__do_it", "input_schema": {"type": "object"}}, READ_TOOL]
        os.environ["FAKE_CODEX_RESERVED"] = "cx_mcp_srv__do_it"
        try:
            with self.assertRaises(BridgeError) as cm:
                codex_bridge.CodexSession(system="s", tools=tools, codex_bin=[sys.executable, str(FAKE_CODEX)],
                                          codex_home=self.codex_home, scratch_root=self.scratch)
        finally:
            del os.environ["FAKE_CODEX_RESERVED"]
        msg = str(cm.exception)
        self.assertIn("refused client tool 'mcp__srv__do_it'", msg)
        self.assertIn("cx_mcp_srv__do_it", msg)
        self.assertIn("-32600", msg)

    def test_a_decoy_tool_named_like_a_word_of_the_refusal_is_not_blamed(self):
        # "dynamic tool name is reserved: ..." contains the words tool, name and is: a client tool of one of those
        # names must not be reported as the refused one; the name Codex actually spelled out is.
        tools = [{"name": "tool", "input_schema": {"type": "object"}}, {"name": "is", "input_schema": {"type": "object"}},
                 {"name": "mcp__srv__do_it", "input_schema": {"type": "object"}}, READ_TOOL]
        os.environ["FAKE_CODEX_RESERVED"] = "cx_mcp_srv__do_it"
        try:
            with self.assertRaises(BridgeError) as cm:
                codex_bridge.CodexSession(system="s", tools=tools, codex_bin=[sys.executable, str(FAKE_CODEX)],
                                          codex_home=self.codex_home, scratch_root=self.scratch)
        finally:
            del os.environ["FAKE_CODEX_RESERVED"]
        msg = str(cm.exception)
        self.assertIn("refused client tool 'mcp__srv__do_it'", msg)
        self.assertNotIn("refused client tool 'tool'", msg)
        self.assertNotIn("refused client tool 'is'", msg)

    def test_a_refusal_naming_no_declared_tool_blames_none(self):
        s = object.__new__(codex_bridge.CodexSession)
        s._aliases = {}
        # "toolname" is inside "zzz_toolname" but not a whole token of it: no declared tool is named by this message.
        declared = [{"name": "toolname"}, {"name": "zzz"}, {"name": "Read"}]
        err = s._tool_refusal(BridgeError("-32600: dynamic tool name is reserved: zzz_toolname"), declared)
        msg = str(err)
        self.assertIn("none of them is known to be wrong", msg)
        self.assertNotIn("refused client tool", msg)
        self.assertIn("zzz_toolname", msg)
        # A declared name that is a whole token of the message still wins, the longest first.
        err = s._tool_refusal(BridgeError("-32600: dynamic tool name is reserved: Read"), declared)
        self.assertIn("refused client tool 'Read'", str(err))

    def test_a_tool_named_like_a_refusal_word_is_not_blamed_over_the_refused_one(self):
        s = object.__new__(codex_bridge.CodexSession)
        s._aliases = {}
        # "reserved" is a whole word of the message and longer than "foo"; only the name Codex spelled out counts.
        declared = [{"name": "reserved"}, {"name": "foo"}]
        err = s._tool_refusal(BridgeError("codex app-server refused thread/start: dynamic tool name is reserved: foo "
                                          "(code -32600)"), declared)
        self.assertIn("refused client tool 'foo'", str(err))
        self.assertNotIn("'reserved'", str(err))

    def test_an_undeclared_refused_name_blames_no_tool_even_a_boilerplate_one(self):
        s = object.__new__(codex_bridge.CodexSession)
        s._aliases = {}
        for msg in ("codex app-server refused thread/start: dynamic tool name is reserved: other (code -32600)",
                    "codex app-server refused thread/start: dynamic tool namespace is reserved: tool (code -32600)",
                    "codex app-server refused thread/start: some new dynamic tool rule about tool (code -32600)"):
            err = str(s._tool_refusal(BridgeError(msg), [{"name": "tool"}]))
            self.assertIn("none of them is known to be wrong", err, msg)
            self.assertNotIn("refused client tool", err, msg)
            self.assertIn(msg, err)

    def test_every_known_refusal_format_yields_the_refused_name(self):
        cases = {
            "dynamic tool name is reserved: mcp__x": "mcp__x",
            "duplicate dynamic tool name: Read": "Read",
            "dynamic tool name has leading/trailing whitespace:  Read ": " Read ",
            "dynamic tool name must match ^[a-zA-Z0-9_-]+$ to match Responses API: fs.read": "fs.read",
            "dynamic tool name must be at most 64 characters to match Responses API: " + "a" * 65: "a" * 65,
        }
        for raw, name in cases.items():
            for wrapped in (raw, f"codex app-server refused thread/start: {raw} (code -32600)"):
                self.assertEqual(codex_bridge.refused_tool_name(wrapped), name, wrapped)
        for other in ("dynamic tool namespace is reserved: mcp", "thread/start.dynamicTools requires experimentalApi "
                      "capability", "dynamic tool name must not be empty", ""):
            self.assertIsNone(codex_bridge.refused_tool_name(other), other)

    def test_the_fake_refusal_formats_round_trip_through_the_bridge(self):
        # Driven through the fake app-server, so the formats the fake emits are the ones the parser reads.
        tools = [{"name": "reserved", "input_schema": {"type": "object"}}, {"name": "foo", "input_schema": {"type": "object"}}]
        os.environ["FAKE_CODEX_RESERVED"] = "foo"
        try:
            with self.assertRaises(BridgeError) as cm:
                codex_bridge.CodexSession(system="s", tools=tools, codex_bin=[sys.executable, str(FAKE_CODEX)],
                                          codex_home=self.codex_home, scratch_root=self.scratch)
        finally:
            del os.environ["FAKE_CODEX_RESERVED"]
        self.assertIn("refused client tool 'foo'", str(cm.exception))

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

    def test_a_changed_tool_set_on_a_continuation_is_warned_about_once_and_served_on_the_original_thread(self):
        # find() continues by history; the thread declared its tools on thread/start, so a tool added mid-conversation
        # is never declared to Codex. The limit is said once, names only; the conversation is served as before.
        edit_tool = {"name": "Edit", "description": "edit", "input_schema": {"type": "object"}}
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"system": "A", "tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        with self.assertLogs("cortados.provider", level="WARNING") as logs:
            r2 = self.post({"system": "A", "tools": [READ_TOOL, edit_tool], "messages": history})
        self.assertEqual(r2["content"], [{"type": "text", "text": "tool said: hello"}])
        lines = [l for l in logs.output if "tool set changed mid-conversation" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("added ['Edit'], dropped []", lines[0])
        [session] = self.reg.sessions.values()
        self.assertTrue(session.tools_warned)
        start = [m for m in self.inbound() if m.get("method") == "thread/start"]
        self.assertEqual([[t["name"] for t in m["params"]["dynamicTools"]] for m in start], [["Read"]])   # one thread

    def test_two_overlapping_continuations_serve_one_and_refuse_the_other_without_losing_the_session(self):
        # Both pass Registry.find (same history) before either holds the session lock: the test holds the lock so
        # both are waiting, then releases it. One is served; the other finds the conversation moved on and gets a
        # 409; the session survives and the next real continuation works.
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post({"tools": [READ_TOOL], "messages": history})
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}]
        [session] = self.reg.sessions.values()
        codes = []

        def continuation():
            try:
                self.post({"tools": [READ_TOOL], "messages": history})
                codes.append(200)
            except urllib.error.HTTPError as exc:
                codes.append(exc.code)
        session.lock.acquire()
        threads = [threading.Thread(target=continuation) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(1.0)                                                  # both have found the session and wait on the lock
        session.lock.release()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(codes), [200, 409])
        self.assertEqual(list(self.reg.sessions.values()), [session])  # the healthy conversation was kept
        history += [{"role": "assistant", "content": [{"type": "text", "text": "tool said: hello"}]}, {"role": "user", "content": "thanks"}]
        self.assertEqual(self.post({"tools": [READ_TOOL], "messages": history})["content"], [{"type": "text", "text": "echo: thanks"}])

    def test_a_finished_one_shot_backend_is_closed_before_the_reply_is_written(self):
        # A client that stops reading blocks the reply write for as long as it likes; the throwaway app-server must
        # be gone by then (it is in no registry). A 3 MB echo and a socket that never reads make the write block.
        import socket
        closed, orig_close = [], codex_bridge.CodexSession.close

        def close(s):
            closed.append(s.id)
            orig_close(s)
        codex_bridge.CodexSession.close = close
        body = json.dumps({"messages": [{"role": "user", "content": "x" * (3 * 1024 * 1024)}]}).encode()
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=30)
        try:
            sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Authorization: Bearer " + TOKEN.encode() + b"\r\nUser-Agent: t/1\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            deadline = time.time() + 20
            while not closed and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(closed), 1, "the one-shot backend was not closed while the reply write was pending")
            self.assertEqual(self.reg.sessions, {})
        finally:
            codex_bridge.CodexSession.close = orig_close
            sock.close()

    def test_a_blocked_keepalive_does_not_keep_a_finished_one_shot_backend_alive(self):
        # Streaming, tool-less, and the client never reads: 1 MB keepalives every 10 ms fill the socket so the ping
        # write blocks the handler thread. The one-shot's close must not wait for that thread.
        import socket
        closed, orig_close, orig_ping = [], codex_bridge.CodexSession.close, provider_server.ANTHROPIC.stream_ping
        ping_s = provider_server.PING_S

        def close(s):
            closed.append(s.id)
            orig_close(s)
        codex_bridge.CodexSession.close = close
        provider_server.ANTHROPIC.stream_ping = lambda: b": " + b"x" * (1 << 20) + b"\n\n"
        provider_server.PING_S = 0.01
        body = json.dumps({"stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=30)
        try:
            sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Authorization: Bearer " + TOKEN.encode() + b"\r\nUser-Agent: t/1\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            deadline = time.time() + 20
            while not closed and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(closed), 1, "the one-shot backend was not closed while a keepalive write was blocked")
            self.assertEqual(self.reg.sessions, {})
        finally:
            codex_bridge.CodexSession.close, provider_server.ANTHROPIC.stream_ping = orig_close, orig_ping
            provider_server.PING_S = ping_s
            sock.close()

    def test_an_opening_request_whose_client_leaves_mid_turn_does_not_leave_its_session_behind(self):
        # The client (a Ctrl-C) hangs up after message_start while the turn runs: the reply can never reach it, and
        # its retry starts over with an empty history, so the just-opened session must be dropped, not reaped later.
        import socket
        closed, orig_close, ping_s = [], codex_bridge.CodexSession.close, provider_server.PING_S

        def close(s):
            closed.append(s.id)
            orig_close(s)
        codex_bridge.CodexSession.close, provider_server.PING_S = close, 0.05
        body = json.dumps({"stream": True, "tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool"}]}).encode()
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=30)
        try:
            sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                         b"Authorization: Bearer " + TOKEN.encode() + b"\r\nUser-Agent: t/1\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            head = b""
            while b"message_start" not in head:
                head += sock.recv(4096)
        finally:
            sock.close()                                                 # the client is gone mid-turn
        try:
            deadline = time.time() + 20
            while (not closed or self.reg.sessions) and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(len(closed), 1, "the just-opened session was not closed after its client left")
            self.assertEqual(self.reg.sessions, {})
        finally:
            codex_bridge.CodexSession.close, provider_server.PING_S = orig_close, ping_s

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
        self.assertEqual(codex_isolation.mcp_server_names(home), ['"b c"', "a"])
        self.assertEqual(codex_isolation.mcp_server_names(tempfile.mkdtemp()), [])

    def test_mcp_table_headers_in_every_toml_spelling_name_the_server_exactly(self):
        # Indented (how nested [mcp_servers.x.env] tables are usually written), spaces inside the brackets, a trailing
        # space, a trailing comment and a single-quoted key: all valid TOML Codex starts a server from. The captured
        # name carries no whitespace, because it becomes the `-c mcp_servers.<name>.enabled=false` key.
        home = tempfile.mkdtemp()
        Path(home, "config.toml").write_text(
            "  [mcp_servers.indented]\ncommand = 'x'\n[ mcp_servers.spaced ]\n[mcp_servers.trailing ]\n"
            "[mcp_servers.commented] # the docs server\n[mcp_servers.'quoted.dot']\n[mcp_servers.plain.env]\nA = '1'\n"
            '["mcp_servers".rootquoted]\ncommand = "x"\n')
        self.assertEqual(codex_isolation.mcp_server_names(home),
                         ["'quoted.dot'", "commented", "indented", "rootquoted", "spaced", "trailing"])

    def test_mcp_servers_declared_any_other_way_are_refused_before_an_app_server_starts(self):
        for form in ('mcp_servers.foo.command = "x"\n', 'mcp_servers = { foo = { command = "x" } }\n',
                     '[mcp_servers]\nfoo = { command = "x" }\n', '[[mcp_servers.foo]]\ncommand = "x"\n',
                     '["mcp_servers"]\nfoo = { command = "x" }\n', '"mcp_servers".foo.command = "x"\n'):
            home = tempfile.mkdtemp()
            Path(home, "config.toml").write_text('model = "m"\n' + form)
            with self.assertRaisesRegex(BridgeError, r"\[mcp_servers\.<name>\] tables"):
                codex_isolation.mcp_server_names(home)
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

    def test_an_inherited_relative_codex_home_reaches_the_child_as_the_same_absolute_path(self):
        # CODEX_HOME=./dedicated in the server's environment and no --codex-home: the config is read relative to this
        # process's cwd, so the child (cwd: the scratch work dir) must be told the same directory, made absolute.
        os.environ["FAKE_CODEX_ARGV"] = os.path.join(self.scratch, "argv.json")
        Path(self.codex_home, "config.toml").write_text('[mcp_servers.node_repl]\ncommand = "x"\n')
        relative, keep = os.path.relpath(self.codex_home), os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = relative
        try:
            s = self.session(codex_home=None)
            try:
                seen = json.loads(Path(self.scratch, "argv.json").read_text())
            finally:
                s.close()
        finally:
            os.environ.pop("CODEX_HOME", None)
            if keep is not None:
                os.environ["CODEX_HOME"] = keep
        self.assertEqual(seen["env"]["CODEX_HOME"], os.path.abspath(self.codex_home))
        self.assertIn("mcp_servers.node_repl.enabled=false", seen["argv"])   # the config of that same home was read

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

    def turn_starts(self, log):
        return [json.loads(l)["params"] for l in Path(log).read_text().splitlines()
                if json.loads(l).get("method") == "turn/start"]

    def late_session(self):
        log = os.path.join(self.scratch, "log.jsonl")
        os.environ["FAKE_CODEX_LOG"] = log
        return self.session(), log

    def assert_followup_carries(self, log, call, text):
        starts = self.turn_starts(log)
        self.assertEqual(len(starts), 2)                                 # the client's turn, then one follow-up
        said = " ".join(i.get("text", "") for i in starts[1]["input"] if i.get("type") == "text")
        self.assertIn(f"call {call['id']} (tool Read, status ok)", said)
        self.assertIn(text, said)

    def test_a_turn_that_completes_around_a_delivered_result_continues_in_a_follow_up_turn(self):
        codex_bridge.CODEX_SETTLE_S = 0.05                               # return the call before the fake completes
        s, log = self.late_session()
        try:
            s.send_user([{"type": "text", "text": "complete around call"}], timeout=10)
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "tool_use")
            [call] = blocks
            time.sleep(1.0)                                              # the fake's turn/completed is queued by now
            s.deliver({call["id"]: {"content": [{"type": "text", "text": "late-content"}], "isError": False}})
            with self.assertLogs("cortados_router.provider.codex_bridge", "WARNING") as logs:
                blocks, stop, _ = s.run_turn(10)                         # no BridgeError: the result is handed over
            self.assertEqual(stop, "end_turn")
            self.assertIn("late-content", blocks[-1]["text"])            # the fake echoes the follow-up's input
            self.assertTrue(any("follow-up turn 1/" in m for m in logs.output))
            self.assert_followup_carries(log, call, "late-content")
        finally:
            s.close()
            os.environ.pop("FAKE_CODEX_LOG", None)

    def test_a_turn_that_ends_before_its_call_is_returned_still_hands_the_call_to_the_client(self):
        s, log = self.late_session()                                     # default settle: turn/completed lands inside it
        try:
            s.send_user([{"type": "text", "text": "complete around call"}], timeout=10)
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "tool_use")
            [call] = blocks
            s.deliver({call["id"]: {"content": [{"type": "text", "text": "after-the-end"}], "isError": False}})
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "end_turn")
            self.assert_followup_carries(log, call, "after-the-end")
        finally:
            s.close()
            os.environ.pop("FAKE_CODEX_LOG", None)

    def test_a_late_sibling_followed_by_turn_completed_still_reaches_the_client(self):
        codex_bridge.CODEX_SETTLE_S = 0.05
        s, log = self.late_session()
        try:
            s.send_user([{"type": "text", "text": "late sibling"}], timeout=10)
            [first], stop, _ = s.run_turn(10)
            self.assertEqual(stop, "tool_use")
            s.deliver({first["id"]: {"content": [{"type": "text", "text": "one"}], "isError": False}})
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "tool_use")
            [second] = [b for b in blocks if b["type"] == "tool_use"]
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(second["name"], "Read")
            s.deliver({second["id"]: {"content": [{"type": "text", "text": "two"}], "isError": False}})
            blocks, stop, _ = s.run_turn(10)
            self.assertEqual(stop, "end_turn")
            self.assert_followup_carries(log, second, "two")
        finally:
            s.close()
            os.environ.pop("FAKE_CODEX_LOG", None)

    def test_follow_up_turns_are_bounded_per_client_request(self):
        codex_bridge.CODEX_SETTLE_S = 0.05
        s, _ = self.late_session()
        try:
            s.send_user([{"type": "text", "text": "complete around call"}], timeout=10)
            [call], _, _ = s.run_turn(10)
            time.sleep(1.0)
            s.deliver({call["id"]: {"content": [{"type": "text", "text": "x"}], "isError": False}})
            s._followups = codex_bridge.MAX_FOLLOWUPS                    # as if this request had used them all
            with self.assertRaisesRegex(BridgeError, "refusing another follow-up turn.*" + call["id"]):
                s.run_turn(10)
        finally:
            s.close()
            os.environ.pop("FAKE_CODEX_LOG", None)

    def test_the_exec_yield_override_is_on_the_command_line_and_its_absence_is_refused(self):
        args = codex_isolation.command_line(["codex"], self.codex_home, 123000)
        self.assertIn("code_mode.default_exec_yield_time_ms=123000", [b for a, b in zip(args, args[1:]) if a == "-c"])
        codex_isolation.check_command_line(args, 123000)
        stripped = [a for a in args if a != "code_mode.default_exec_yield_time_ms=123000"]
        with self.assertRaisesRegex(BridgeError, "lacks `-c code_mode.default_exec_yield_time_ms=123000`"):
            codex_isolation.check_command_line(stripped, 123000)
        with self.assertRaisesRegex(BridgeError, "lacks"):
            codex_isolation.check_command_line(args, 999)               # another budget than the one asked for
        for bad in (0, -1, 1.5, True, None):
            with self.assertRaisesRegex(BridgeError, "positive integer"):
                codex_isolation.exec_yield_override(bad)

    def test_an_effective_config_whose_exec_yield_differs_or_is_missing_is_refused(self):
        os.environ["FAKE_CODEX_ARGV"] = os.path.join(self.scratch, "argv.json")
        s = self.session(exec_yield_ms=45000)
        try:
            argv = json.loads(Path(self.scratch, "argv.json").read_text())["argv"]
            self.assertIn("code_mode.default_exec_yield_time_ms=45000", argv)
        finally:
            s.close()
        for patch in ({"code_mode": {"default_exec_yield_time_ms": 30000}}, {"code_mode": None}):
            os.environ["FAKE_CODEX_CONFIG_PATCH"] = json.dumps(patch)
            try:
                with self.assertRaisesRegex(BridgeError, "code_mode.default_exec_yield_time_ms=.*expected 45000"):
                    self.session(exec_yield_ms=45000)
            finally:
                del os.environ["FAKE_CODEX_CONFIG_PATCH"]

    def test_the_effective_config_check_accepts_the_real_isolated_config_and_names_every_violation(self):
        # The shape config/read answered on Codex CLI 0.163.0 under the bridge's own command line (P2 probe).
        real = {"model_provider": "openai", "model_providers": {}, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
                "openai_base_url": None, "model_catalog_json": None,
                "web_search": "disabled", "project_doc_max_bytes": 0, "notify": [], "include_environment_context": False,
                "mcp_servers": {"node_repl": {"enabled": False}, "computer-use": {"enabled": False},
                                "openaiDeveloperDocs": {"url": "https://developers.openai.com/mcp", "enabled": False}},
                "features": {**{f: False for f in codex_isolation.DISABLED_FEATURES}, "code_mode_host": True, "mentions_v2": True},
                "code_mode": {"default_exec_yield_time_ms": codex_isolation.DEFAULT_EXEC_YIELD_MS}}
        codex_isolation.check_effective_config(real)
        broken = dict(real, model_provider="mirror", chatgpt_base_url="https://mirror.invalid/", web_search="live",
                      model_providers={"openai": {"base_url": "https://mirror.invalid/v1"}},
                      mcp_servers={**real["mcp_servers"], "rogue": {"command": "x", "enabled": True}, "quiet": {"command": "y"}},
                      features={**real["features"], "shell_tool": True, "apps": None})
        with self.assertRaises(BridgeError) as cm:
            codex_isolation.check_effective_config(broken)
        text = str(cm.exception)
        for needle in ('model_provider="mirror"', "chatgpt_base_url", 'web_search="live"', "model_providers.openai",
                       "['quiet', 'rogue']", "['apps', 'shell_tool']", "--codex-home"):
            self.assertIn(needle, text)
        # The config `ollama launch codex` writes (seen live on the build Mac): a ChatGPT login whose turns go to Ollama.
        ollama = dict(real, openai_base_url="http://127.0.0.1:11434/api/codex/v1", model_catalog_json="/x/ollama-launch-models.json")
        with self.assertRaisesRegex(BridgeError, "openai_base_url.*model_catalog_json"):
            codex_isolation.check_effective_config(ollama)
        with self.assertRaisesRegex(BridgeError, "no login"):
            codex_isolation.check_account({"account": None, "requiresOpenaiAuth": True})
        with self.assertRaisesRegex(BridgeError, "not a ChatGPT subscription"):
            codex_isolation.check_account({"account": {"type": "apiKey"}})
        codex_isolation.check_account({"account": {"type": "chatgpt", "planType": "pro"}})

    def test_run_turn_keeps_its_deadline_while_notifications_keep_arriving(self):
        # A server that streams notifications without end re-arms the settle window on every one; the absolute
        # deadline must still end the turn: with calls collected they are returned, with none it is a failure.
        s = object.__new__(codex_bridge.CodexSession)
        late_state(s)
        s.id, s._fatal, s._usage_total, s._usage_mark = "unit", None, {}, {}
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {}, set(), {}, {}
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

    def test_a_lone_surrogate_name_hashes_without_raising(self):
        # The wires refuse such names; the encoder must still never raise if one gets past them.
        aliases = codex_isolation.tool_aliases([{"name": "Re\ud800d"}, {"name": "Read"}])
        [(wire_name, client)] = aliases.items()
        self.assertEqual(client, "Re\ud800d")
        self.assertTrue(codex_isolation.codex_safe(wire_name), wire_name)

    def test_tool_aliases_cover_the_codex_client_tool_set_and_hash_around_a_taken_alias(self):
        codex_client = [{"name": n} for n in ("exec_command", "write_stdin", "request_user_input", "view_image",
                                              "get_goal", "create_goal", "update_goal", "apply_patch", "update_plan")]
        aliases = codex_isolation.tool_aliases(codex_client + [{"name": "Read"}])
        self.assertEqual(aliases, {"client_" + t["name"]: t["name"] for t in codex_client})
        self.assertEqual(codex_isolation.tool_aliases([{"name": "Read"}, {"name": "Edit"}]), {})
        # A client tool already named like an alias keeps its name; the Codex-named one gets a hashed alias instead.
        taken = codex_isolation.tool_aliases([{"name": "sleep"}, {"name": "client_sleep"}])
        [(alias, orig)] = taken.items()
        self.assertEqual(orig, "sleep")
        self.assertNotEqual(alias, "client_sleep")
        self.assertTrue(alias.startswith("client_sleep_"))

    def assert_codex_safe_round_trip(self, names):
        aliases = codex_isolation.tool_aliases([{"name": n} for n in names])
        wire = [next((a for a, o in aliases.items() if o == n), n) for n in names]
        self.assertEqual(len(set(wire)), len(names), wire)                 # collision-free
        for w in wire:
            self.assertRegex(w, r"^[A-Za-z0-9_-]{1,64}$")
            self.assertFalse(w.lower().startswith("mcp__"), w)
        self.assertEqual([aliases.get(w, w) for w in wire], names)       # reversible
        self.assertEqual(codex_isolation.tool_aliases([{"name": n} for n in names]), aliases)   # deterministic
        return dict(zip(names, wire))

    def test_mcp_tool_names_are_encoded_for_codex_and_mapped_back(self):
        wire = self.assert_codex_safe_round_trip(["mcp__cortados-route__route_task", "mcp__github__create_issue"])
        self.assertEqual(wire["mcp__cortados-route__route_task"], "cx_mcp_cortados-route__route_task")
        self.assertEqual(wire["mcp__github__create_issue"], "cx_mcp_github__create_issue")

    def test_names_with_dots_and_odd_characters_are_encoded(self):
        wire = self.assert_codex_safe_round_trip(["fs.read", "my tool", "émoji✓", "a/b:c", " padded ", ""])
        self.assertTrue(wire["fs.read"].startswith("fs_read_"))

    def test_very_long_names_are_cut_to_64_with_a_distinguishing_hash(self):
        a, b = "mcp__server__" + "x" * 100 + "a", "mcp__server__" + "x" * 100 + "b"
        wire = self.assert_codex_safe_round_trip([a, b, "y" * 65])
        self.assertTrue(all(len(w) == 64 for w in wire.values()))

    def test_names_that_would_collide_after_encoding_get_distinct_names(self):
        wire = self.assert_codex_safe_round_trip(["fs.read", "fs_read", "fs:read", "mcp__a__b", "cx_mcp_a__b"])
        self.assertEqual(wire["fs_read"], "fs_read")                     # the safe name keeps its own name
        self.assertEqual(wire["cx_mcp_a__b"], "cx_mcp_a__b")
        self.assertNotEqual(wire["mcp__a__b"], "cx_mcp_a__b")

    def test_safe_names_pass_through_unchanged(self):
        names = ["Read", "Edit", "Bash", "web-fetch", "Task_2", "z" * 64]
        self.assertEqual(codex_isolation.tool_aliases([{"name": n} for n in names]), {})

    def test_the_bare_mcp_name_is_encoded_in_any_case_and_round_trips(self):
        # codex_safe rejects "mcp" (the root of the namespace Codex reserves), so the wire name must differ from it.
        self.assertFalse(codex_isolation.codex_safe("mcp"))
        self.assertFalse(codex_isolation.codex_safe("MCP"))
        wire = self.assert_codex_safe_round_trip(["mcp", "MCP", "Mcp", "mcp..x", "cx_mcp"])
        self.assertTrue(wire["mcp"].startswith("cx_mcp_"), wire["mcp"])  # cx_mcp is a client tool here: hashed around
        self.assertEqual(wire["cx_mcp"], "cx_mcp")
        self.assertEqual(wire["MCP"], "cx_MCP")
        self.assertEqual(wire["Mcp"], "cx_Mcp")
        for n in ("mcp", "MCP", "Mcp", "mcp..x"):
            self.assertNotEqual(wire[n], n)
            self.assertNotEqual(wire[n].lower(), "mcp")
        self.assertTrue(wire["mcp..x"].startswith("cx_mcp__x_"))         # mcp..x -> mcp__x after substitution -> cx_
        self.assertEqual(codex_isolation.tool_aliases([{"name": "mcp"}]), {"cx_mcp": "mcp"})

    def test_the_hash_suffix_never_pushes_a_name_past_64_even_when_every_shorter_suffix_is_taken(self):
        taken, seen = set(), []
        with self.assertRaises(BridgeError):                              # exhausts the 8..62 digit suffixes, then refuses
            for _ in range(200):
                out = codex_isolation._encode("mcp__server__" + "x" * 100, taken)
                self.assertLessEqual(len(out), codex_isolation.MAX_TOOL_NAME, out)
                self.assertRegex(out, r"^[A-Za-z0-9_-]{1,64}$")
                self.assertNotIn(out, taken)
                taken.add(out)
                seen.append(out)
        self.assertEqual(len(seen), codex_isolation.MAX_TOOL_NAME - 2 - 8 + 1)   # every suffix length 8..62 was used
        self.assertEqual(len(set(seen)), len(seen))

    def test_abandoning_a_turn_keeps_its_deadline_while_events_keep_arriving(self):
        # The drain after turn/interrupt must end at its deadline even when the queue never runs dry.
        s = object.__new__(codex_bridge.CodexSession)
        late_state(s)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.thread_id, s._turn_id = "unit", None, {}, {}, "t1", "turn-1"
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {"exec-1": 0}, {"exec-1"}, {}, {}
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
        late_state(s)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.thread_id, s._turn_id, s.proc = "unit", None, {}, {}, "t1", "turn-1", Proc()
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {f"exec-{i}": i for i in range(4)}, set(), {}, {}
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
        late_state(s)
        s._delivered, s._usage_mark = {}, {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 5}
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
        late_state(s)
        s.id, s._fatal, s._usage_total, s._usage_mark, s.proc, s.dir = "unit", None, {}, {}, Proc(), Path(self.scratch)
        s._events, s._pending, s.emitted, s._delivered, s._aliases = queue.Queue(), {}, set(), {}, {}
        s._events.put(("request", {"method": "item/tool/requestUserInput", "id": 7, "params": {"questions": []}}))
        t0 = time.time()
        with self.assertRaises(BridgeError):
            s.run_turn(0.025)
        self.assertLess(time.time() - t0, 0.3)                           # not a fresh REPLY_WRITE_S for the answer

    def test_the_messages_wire_reports_the_cache_counts_the_bridges_deliver(self):
        from cortados_router.provider import anthropic_wire
        usage = {"input_tokens": 8341, "cache_read_input_tokens": 3968, "cache_creation_input_tokens": 10, "output_tokens": 110}
        self.assertEqual(anthropic_wire.message_json("m", "cortados", [], "end_turn", usage)["usage"],
                         {"input_tokens": 8341, "output_tokens": 110, "cache_read_input_tokens": 3968, "cache_creation_input_tokens": 10})
        raw = b"".join(anthropic_wire.sse_body([{"type": "text", "text": "hi"}], "end_turn", usage)).decode()
        delta = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: ") and '"message_delta"' in l][0]
        self.assertEqual(delta["usage"], {"input_tokens": 8341, "output_tokens": 110, "cache_read_input_tokens": 3968,
                                          "cache_creation_input_tokens": 10})

    def test_the_codex_version_pin_names_the_verified_release(self):
        self.assertEqual(codex_bridge.SUPPORTED_CLI_VERSIONS, ("0.163.0",))
        self.assertNotIn("code_mode_host", codex_isolation.DISABLED_FEATURES)


if __name__ == "__main__":
    unittest.main()
