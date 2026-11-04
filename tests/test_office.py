"""The Office view (provider/office.py) and the scoped client tokens (provider/clients.py), over the real HTTP endpoint
with the fake Claude CLI (tests/fixtures/fake_claude_cli.py): no vendor CLI runs, state is a throwaway CORTADOS_HOME."""
import io
import json
import os
import sys
import tempfile
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from cortados_router import child_tasks
from cortados_router.provider import clients, office
from tests.test_provider import READ_TOOL, TOKEN, EndpointHarness

OFFICE_TOKEN = "office-token-for-tests-0001"
SESSION = "3f9a1c2e9b7d4a10"
TAGS = {"x-cortados-session": SESSION, "x-cortados-project": "CortadOS-Router", "x-cortados-device": "pro"}


def write_clients(path, entries):
    Path(path).write_text(json.dumps({"version": 1, "clients": entries}))


class OfficeHarness(EndpointHarness):
    """The endpoint with one scoped client (office:read) registered from a throwaway clients.json."""

    def setUp(self):
        super().setUp()
        self.clients_file = os.path.join(self.scratch, "clients.json")
        write_clients(self.clients_file, [{"name": "claw3d", "token_sha256": clients.token_digest(OFFICE_TOKEN),
                                           "scopes": ["office:read"]}])
        self.reg.clients = clients.ClientTokens(path=Path(self.clients_file), owner_token=TOKEN)
        home = mock.patch.dict(os.environ, {"CORTADOS_HOME": tempfile.mkdtemp()})
        home.start()
        self.addCleanup(home.stop)

    def request(self, method, path, token=None, body=None, headers=None, header="authorization"):
        h = {"content-type": "application/json", **(headers or {})}
        if token is not None:
            h[header] = f"Bearer {token}" if header == "authorization" else token
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def view(self, token=TOKEN):
        code, raw = self.request("GET", "/v1/cortados/office", token)
        self.assertEqual(code, 200, raw)
        return json.loads(raw), raw


class ScopeTests(OfficeHarness):
    GETS = ["/health", "/", "/dashboard", "/v1/models", "/resources", "/v1/cortados/usage",
            "/v1/cortados/runs/abcdefgh12", "/v1/cortados/office/heartbeat", "/v1/cortados/office/x", "/nope",
            "/v1/cortados/office?x=1/../usage"]
    POSTS = ["/v1/messages", "/v1/responses", "/v1/messages/count_tokens", "/bridge/call",
             "/v1/cortados/office/heartbeat", "/v1/cortados/office", "/nope"]

    def test_the_office_token_opens_only_get_office(self):
        for header in ("authorization", "x-api-key", "x-cortados-token"):
            code, raw = self.request("GET", "/v1/cortados/office", OFFICE_TOKEN, header=header)
            self.assertEqual(code, 200, (header, raw))
            self.assertEqual(json.loads(raw)["version"], 1)
        for p in self.GETS:
            if p.startswith("/v1/cortados/office?"):
                continue                          # the query is ignored: that one IS the office view
            code, raw = self.request("GET", p, OFFICE_TOKEN)
            self.assertEqual(code, 403, (p, raw))
            self.assertEqual(json.loads(raw)["error"]["type"], "permission_error")
        for p in self.POSTS:
            body = {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]}
            code, raw = self.request("POST", p, OFFICE_TOKEN, body=body)
            self.assertEqual(code, 403, (p, raw))
        for m in ("PUT", "DELETE", "PATCH"):     # not served at all: http.server's 501, never a 2xx
            code, _ = self.request(m, "/v1/cortados/office", OFFICE_TOKEN)
            self.assertNotEqual(code // 100, 2)
        self.assertEqual(self.reg.sessions, {})   # nothing was routed with the office token
        self.assertIsNone(self.reg.runs.summary("abcdefgh12"))

    def test_the_owner_token_is_unchanged(self):
        self.assertEqual(self.request("GET", "/v1/cortados/office", TOKEN)[0], 200)
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("GET", "/v1/models", TOKEN)[0], 200)
        self.assertEqual(self.request("GET", "/v1/cortados/runs/abcdefgh12", TOKEN)[0], 404)
        code, raw = self.request("POST", "/v1/messages", TOKEN,
                                 body={"model": "cortados", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(code, 200, raw)
        # bad and missing tokens: 401 everywhere they were 401, the office included
        for tok in (None, "wrong-token-000000000000"):
            for p in ("/v1/cortados/office", "/v1/models", "/v1/cortados/usage", "/resources"):
                self.assertEqual(self.request("GET", p, tok)[0], 401, (p, tok))
            self.assertEqual(self.request("POST", "/v1/messages", tok, body={})[0], 401)
            self.assertEqual(self.request("POST", "/v1/cortados/office/heartbeat", tok, body={})[0], 401)

    def test_a_bad_clients_file_fails_closed(self):
        for bad in ("not json", json.dumps({"version": 1, "clients": [{"name": "claw3d", "scopes": ["office:write"],
                                                                        "token_sha256": clients.token_digest(OFFICE_TOKEN)}]}),
                    json.dumps({"version": 1, "clients": [{"name": "x", "scopes": ["office:read"],
                                                           "token_sha256": clients.token_digest(TOKEN)}]})):
            Path(self.clients_file).write_text(bad)
            os.utime(self.clients_file, (time.time() + 5, time.time() + 5))   # a new mtime: re-read for sure
            with self.assertLogs("cortados_router.provider.clients", "ERROR"):
                code, _ = self.request("GET", "/v1/cortados/office", OFFICE_TOKEN)
            self.assertEqual(code, 401)
            self.assertIsNotNone(self.reg.clients.error)
            self.assertEqual(self.request("GET", "/v1/cortados/office", TOKEN)[0], 200)   # the owner is unaffected

    def test_revoking_a_client_needs_no_restart(self):
        self.assertEqual(self.request("GET", "/v1/cortados/office", OFFICE_TOKEN)[0], 200)
        write_clients(self.clients_file, [])
        os.utime(self.clients_file, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.request("GET", "/v1/cortados/office", OFFICE_TOKEN)[0], 401)


class ClientsCliTests(unittest.TestCase):
    def test_add_list_remove_never_prints_the_token(self):
        d = tempfile.mkdtemp()
        cf, tf = os.path.join(d, "clients.json"), os.path.join(d, "sub", "office-token")
        with mock.patch.dict(os.environ, {"CORTADOS_PROVIDER_CLIENTS": cf}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(clients.main(["add", "claw3d", "--scope", "office:read", "--token-file", tf]), 0)
                self.assertEqual(clients.main(["list"]), 0)
            token = Path(tf).read_text().strip()
            self.assertNotIn(token, out.getvalue())
            self.assertNotIn(token, Path(cf).read_text())                  # only its hash is stored
            self.assertEqual(os.stat(tf).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(cf).st_mode & 0o777, 0o600)
            c = clients.ClientTokens(path=Path(cf), owner_token="owner-token-xyz")
            self.assertEqual(c.match(token).name, "claw3d")
            self.assertTrue(c.match(token).allows("GET", "/v1/cortados/office"))
            self.assertFalse(c.match(token).allows("POST", "/v1/cortados/office"))
            self.assertIsNone(c.match("owner-token-xyz"))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(clients.main(["add", "other", "--scope", "office:read", "--token-file", tf]), 1)
                self.assertEqual(clients.main(["remove", "claw3d"]), 0)
            os.utime(cf, (time.time() + 5, time.time() + 5))
            self.assertIsNone(c.match(token))


class HeaderValidationTests(unittest.TestCase):
    def test_valid_tags_are_kept_and_invalid_ones_dropped_and_counted(self):
        b = office.OfficeBoard()
        good = b.read_tags({**TAGS, "x-cortados-task-title": "fix the tests (round 2): ok"})
        self.assertEqual(good, {"session": SESSION, "project": "CortadOS-Router", "device": "pro",
                                "title": "fix the tests (round 2): ok"})
        cases = {
            "x-cortados-session": ["ABCDEF0123456789", "3f9a", "3f9a1c2e9b7d4a10zz", "g" * 16, "a" * 65],
            "x-cortados-project": ["a/b", "../etc", "x" * 65, "p\tq", "proj;rm"],
            "x-cortados-device": ["pro air", "host.local/x", "192.0.2.5"],
            "x-cortados-task-title": ["t" * 81, "line\nbreak", "café", "ping 198.51.100.20", "   "],
        }
        for name, values in cases.items():
            for v in values:
                self.assertEqual(b.read_tags({name: v}), {}, (name, v))
        self.assertEqual(b.dropped["x-cortados-session"], 5)
        self.assertEqual(b.dropped["x-cortados-task-title"], 5)
        self.assertEqual(sum(b.dropped.values()), 18)


class FloorTests(OfficeHarness):
    def post_routed(self, content, run_id=None, headers=None, tools=True, history=None):
        h = {**(headers or {})}
        if run_id:
            h["x-cortados-run-id"] = run_id
        body = {"model": "cortados", "messages": history or [{"role": "user", "content": content}]}
        if tools:
            body["tools"] = [READ_TOOL]
        return self.post(body, headers=h)

    def test_tagged_runs_group_into_floors_and_untagged_into_the_lobby(self):
        r = self.post_routed("please use tool", "run-floor-01",
                             {**TAGS, "x-cortados-task-title": "read the docs"})
        self.assertEqual(r["stop_reason"], "tool_use")
        self.post_routed("hello there", "run-floor-02", {**TAGS, "x-cortados-task-title": "say hi"})
        other = {"x-cortados-session": "aaaaaaaaaaaaaaaa", "x-cortados-project": "Other", "x-cortados-device": "air"}
        self.post_routed("hello other", "run-other-01", other)
        self.post_routed("hello lobby", "run-lobby-01")              # no tag: the lobby
        self.post_routed("hello no id", None, tools=True)            # no tag, no run id: the lobby, by conversation
        # a malformed tag never refuses the request: it is dropped (and counted)
        r = self.post_routed("hello bad", "run-floor-03", {**TAGS, "x-cortados-project": "a/b"})
        self.assertEqual(r["stop_reason"], "end_turn")
        v, _ = self.view()
        floors = {f["session"]: f for f in v["floors"]}
        self.assertEqual(set(floors), {SESSION, "aaaaaaaaaaaaaaaa"})
        f = floors[SESSION]
        self.assertEqual((f["project"], f["device"]), ("CortadOS-Router", "pro"))
        ws = {w["run_id"]: w for w in f["workers"]}
        self.assertEqual(set(ws), {"run-floor-01", "run-floor-02", "run-floor-03"})
        self.assertEqual((ws["run-floor-01"]["status"], ws["run-floor-01"]["tool"]), ("working", "Read"))
        self.assertEqual(ws["run-floor-01"]["title"], "read the docs")
        self.assertEqual((ws["run-floor-02"]["status"], ws["run-floor-02"]["tool"]), ("idle", None))
        self.assertEqual(ws["run-floor-01"]["provider"], "claude")   # a fixed-model endpoint runs the Claude CLI
        self.assertEqual(ws["run-floor-01"]["kind"], "subagent")
        self.assertIsNone(f["orchestrator"])                          # no heartbeat yet, the main loop does not route
        self.assertEqual([w["run_id"] for w in floors["aaaaaaaaaaaaaaaa"]["workers"]], ["run-other-01"])
        lobby = {w["run_id"] for w in v["lobby"]["workers"]}
        self.assertIn("run-lobby-01", lobby)
        self.assertEqual(len(lobby), 2)
        self.assertTrue(any(x.startswith("c-") for x in lobby))
        self.assertEqual(v["dropped_headers"], {"x-cortados-project": 1})
        # the office token sees exactly the same view
        self.assertEqual(self.view(OFFICE_TOKEN)[0]["floors"][0]["session"], v["floors"][0]["session"])

    def test_the_tool_result_turn_moves_the_worker_on(self):
        history = [{"role": "user", "content": "please use tool"}]
        r1 = self.post_routed(None, "run-turns-01", TAGS, history=history)
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "x"}]}]
        self.post_routed(None, "run-turns-01", TAGS, history=history)
        w = self.view()[0]["floors"][0]["workers"][0]
        self.assertEqual((w["status"], w["tool"]), ("idle", None))

    def test_a_main_loop_that_routes_is_the_orchestrator(self):
        self.post_routed("hello main", None, TAGS)                    # tagged, no run id: the session's main loop
        f = self.view()[0]["floors"][0]
        self.assertEqual(f["workers"], [])
        self.assertEqual((f["orchestrator"]["source"], f["orchestrator"]["status"]), ("routed", "idle"))

    def test_no_prompt_tool_or_command_content_reaches_the_view(self):
        marker = "SECRETMARKER7731"
        history = [{"role": "user", "content": f"please use tool {marker} rm -rf /tmp/{marker}"}]
        r1 = self.post_routed(None, "run-marker-1", {**TAGS, "x-cortados-task-title": "marker run"}, history=history)
        use = r1["content"][-1]
        history += [{"role": "assistant", "content": r1["content"]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"],
                                                  "content": f"file says {marker} /Users/someone/{marker}"}]}]
        self.post_routed(None, "run-marker-1", TAGS, history=history)
        self.post(({"model": "cortados", "system": f"sys {marker}",
                    "messages": [{"role": "user", "content": f"{marker} lobby"}]}))
        self.request("POST", "/v1/cortados/office/heartbeat", TOKEN,
                     body={"session": SESSION, "status": "working", "tool": "Bash", "model": "claude-opus-5-5"})
        child_tasks.log_event("started", task_id="t20261104120000-0a1b2c3d", cwd=f"/Users/x/{marker}",
                              orchestrator="cortados", permission_mode="auto", chrome=False, title="child label",
                              session=SESSION, project="CortadOS-Router", task_preview=f"{marker} prompt")
        for tok in (TOKEN, OFFICE_TOKEN):
            v, raw = self.view(tok)
            self.assertNotIn(marker, raw)
            self.assertNotIn("/Users", raw)
            self.assertNotIn("f0.md", raw)                            # the tool call's argument
            self.assertNotIn(TOKEN, raw)
            self.assertNotIn(OFFICE_TOKEN, raw)
            self.assertIn("child label", raw)


class HeartbeatTests(OfficeHarness):
    def beat(self, body, token=TOKEN):
        return self.request("POST", "/v1/cortados/office/heartbeat", token, body=body)

    def test_heartbeat_validation_and_scope(self):
        ok = {"session": SESSION, "project": "CortadOS-Router", "device": "pro", "model": "claude-opus-5-5",
              "status": "working", "tool": "mcp__cortados-route__start_task"}
        self.assertEqual(self.beat(ok)[0], 200)
        for bad in ({**ok, "session": "nothex"}, {**ok, "status": "busy"}, {**ok, "tool": "Bash(rm -rf)"},
                    {**ok, "prompt": "hello"}, {**ok, "project": "a/b"}, [1]):
            self.assertEqual(self.beat(bad)[0], 400, bad)
        self.assertEqual(self.beat(ok, OFFICE_TOKEN)[0], 403)

    def test_a_heartbeat_shows_only_on_a_floor_that_routes(self):
        self.beat({"session": SESSION, "status": "working", "tool": "Agent", "model": "claude-opus-5-5"})
        self.assertEqual(self.view()[0]["floors"], [])               # held, not shown: the session has not routed
        self.post({"model": "cortados", "messages": [{"role": "user", "content": "hello"}]},
                  headers={**TAGS, "x-cortados-run-id": "run-hb-0001"})
        o = self.view()[0]["floors"][0]["orchestrator"]
        self.assertEqual((o["status"], o["tool"], o["model"], o["source"]),
                         ("working", "Agent", "claude-opus-5-5", "heartbeat"))
        self.beat({"session": SESSION, "status": "ended"})
        self.assertIsNone(self.view()[0]["floors"][0]["orchestrator"])

    def test_heartbeat_ttl_expiry(self):
        b = office.OfficeBoard()
        t0 = 1_000_000.0
        b.turn_start("r:run-ttl-001", {"session": SESSION}, run_id="run-ttl-001", provider="codex", lane="codex",
                     model="gpt-6.1-sol", now=t0)
        b.heartbeat({"session": SESSION, "status": "working", "tool": "Bash", "model": "m", "project": None,
                     "device": None}, now=t0)
        orch = lambda now: b.view([], now=now)["floors"][0]["orchestrator"]   # noqa: E731
        self.assertEqual(orch(t0 + office.HEARTBEAT_TTL_S - 1)["status"], "working")
        self.assertIsNone(orch(t0 + office.HEARTBEAT_TTL_S + 1))     # a working claim not refreshed: gone
        b.heartbeat({"session": SESSION, "status": "idle", "tool": None, "model": "m", "project": None,
                     "device": None}, now=t0 + 200)
        self.assertEqual(orch(t0 + 200 + office.HEARTBEAT_TTL_S + 1)["status"], "idle")   # idle lives longer
        # the floor itself goes FLOOR_TTL_S after its last activity (the idle heartbeat at t0+200 counted)
        self.assertEqual(b.view([], now=t0 + 200 + office.FLOOR_TTL_S + 1)["floors"], [])

    def test_worker_lifecycle_idle_done_dropped(self):
        b = office.OfficeBoard()
        t0 = 2_000_000.0
        b.turn_start("r:run-life-01", {"session": SESSION}, run_id="run-life-01", provider="local", lane="local-studio",
                     model="qwen3-32b", now=t0)
        b.turn_end("r:run-life-01", {"session": SESSION}, status="idle", now=t0 + 1)
        w = lambda now: b.view([], now=now)["floors"][0]["workers"]   # noqa: E731
        self.assertEqual(w(t0 + 2)[0]["status"], "idle")
        self.assertEqual(w(t0 + 1 + office.DONE_AFTER_S + 1)[0]["status"], "done")
        self.assertEqual(w(t0 + 1 + office.DONE_AFTER_S + office.WORKER_KEEP_S + 2), [])


class ChildTaskViewTests(OfficeHarness):
    def test_child_events_make_workers_and_floors(self):
        self.assertEqual(office.CHILD_EVENT_LOG, child_tasks.EVENT_LOG)
        tid, tid2 = "t20261104120000-0a1b2c3d", "t20261104120001-0a1b2c3e"
        child_tasks.log_event("started", task_id=tid, cwd="/x/CortadOS-Router", orchestrator="cortados",
                              permission_mode="auto", chrome=False, title="draft README section", session=SESSION,
                              project="CortadOS-Router", device="pro")
        child_tasks.log_event("started", task_id=tid2, cwd="/x/y", orchestrator="cortados", permission_mode="auto",
                              chrome=False, title="bot job", client="grokbot")
        v, _ = self.view()
        f = v["floors"][0]
        self.assertEqual((f["session"], f["project"]), (SESSION, "CortadOS-Router"))
        w = f["workers"][0]
        self.assertEqual((w["run_id"], w["kind"], w["title"], w["status"]), (tid, "child_task", "draft README section",
                                                                             "working"))
        self.assertEqual([x["run_id"] for x in v["lobby"]["workers"]], [tid2])
        # the child's own routed traffic (run id = task id) fills in provider and tool
        self.post({"model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool"}]},
                  headers={**TAGS, "x-cortados-run-id": tid})
        w = self.view()[0]["floors"][0]["workers"][0]
        self.assertEqual((w["kind"], w["provider"], w["tool"], w["title"]), ("child_task", "claude", "Read",
                                                                              "draft README section"))
        child_tasks.log_event("finished", task_id=tid, session=SESSION, status="failed")
        w = self.view()[0]["floors"][0]["workers"][0]
        self.assertEqual((w["status"], w["tool"]), ("error", None))
        self.assertIsNotNone(w["ended_at"])

    def test_an_unreadable_child_log_is_an_error_not_silence(self):
        d = Path(os.environ["CORTADOS_HOME"])
        d.mkdir(parents=True, exist_ok=True)
        (d / office.CHILD_EVENT_LOG).mkdir()                          # a directory: open() fails
        v, _ = self.view()
        self.assertEqual(v["errors"], [{"source": "child_tasks", "error": "IsADirectoryError"}])


if __name__ == "__main__":
    unittest.main()
