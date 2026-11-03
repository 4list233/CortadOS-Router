"""Bot gateway (cortados_router/bot_gateway.py): auth, the six-tool surface, policy, caps, usage log. Fakes only: the
child-task and route_task implementations are injected, so no claude/codex child and no provider is ever started."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from unittest import mock

from cortados_router import bot_gateway as bg
from cortados_router import child_tasks as ct
from cortados_router import subagent_mcp

TOKEN = "gw-test-token-0123456789abcdef"


class FakeChild:
    def __init__(self):
        self.calls, self.n, self.states = [], 0, {}

    def __call__(self, name, a, cfg):
        self.calls.append((name, dict(a), dict(cfg)))
        if name == "start_task":
            self.n += 1
            tid = f"task-{self.n:04d}-abcdef"
            self.states[tid] = "running"
            return {"content": [{"type": "text", "text": json.dumps({"task_id": tid, "state": "running"})}]}
        if name == "task_status":
            return {"content": [{"type": "text", "text": json.dumps({"task_id": a["task_id"],
                                                                     "state": self.states.get(a["task_id"])})}]}
        if name == "list_tasks":
            ts = [{"task_id": t, "state": s} for t, s in self.states.items()] + [{"task_id": "someone-elses"}]
            return {"content": [{"type": "text", "text": json.dumps({"tasks": ts})}]}
        return {"content": [{"type": "text", "text": json.dumps({"task_id": a.get("task_id"), "ok": True})}]}


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.root = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_SCARCITY_REFRESH": "off"})
        self.env.start()
        self.child = FakeChild()
        self.routes = []

        def route(url, token, task, context="", max_tokens=0, model=None, client=None):
            self.routes.append({"task": task, "client": client})
            return {"content": [{"type": "text", "text": "answer"}]}
        self.gw = bg.Gateway(TOKEN, self.root, {"url": "http://fake", "token": "prov-token-xyz123"},
                             child=self.child, route=route)

    def tearDown(self):
        self.env.stop()

    def tcall(self, name, **a):
        return self.gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": a}})

    def usage(self):
        p = os.path.join(self.home, bg.USAGE_LOG)
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l) for l in f]

    def start(self, **kw):
        a = {"task": "do it", "cwd": self.root, **kw}
        return self.tcall("start_task", **a)

    def test_tools_list_is_exactly_six(self):
        r = self.gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = sorted(t["name"] for t in r["result"]["tools"])
        self.assertEqual(names, sorted(bg.EXPOSED))
        st = [t for t in r["result"]["tools"] if t["name"] == "start_task"][0]
        self.assertNotIn("chrome", st["inputSchema"]["properties"])
        # the shared schema is not mutated
        self.assertIn("chrome", subagent_mcp.CHILD_TOOLS[0]["inputSchema"]["properties"])

    def test_initialize_and_notification(self):
        r = self.gw.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "cortados-bot-gateway")
        self.assertIsNone(self.gw.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_chrome_refused(self):
        r = self.start(chrome=True)
        self.assertEqual(r["error"]["code"], bg.POLICY_ERROR)
        self.assertIn("chrome", r["error"]["message"])
        self.assertEqual(self.child.calls, [])
        self.assertEqual(self.usage()[-1]["outcome"], "refused")

    def test_cwd_outside_root_refused(self):
        for cwd in ("/tmp", self.root + "/../", "relative/path", None):
            r = self.tcall("start_task", task="x", cwd=cwd)
            self.assertEqual(r["error"]["code"], bg.POLICY_ERROR, cwd)
        sub = os.path.join(self.root, "repo")
        os.mkdir(sub)
        self.assertIn("result", self.tcall("start_task", task="x", cwd=sub))

    def test_start_is_attributed_and_chrome_forced_false(self):
        r = self.start()
        self.assertIn("result", r)
        name, a, cfg = self.child.calls[-1]
        self.assertEqual((name, a["chrome"], cfg["client"]), ("start_task", False, "grokbot"))
        u = self.usage()[-1]
        self.assertEqual((u["client"], u["tool"], u["outcome"], u["task_id"]),
                         ("grokbot", "start_task", "ok", "task-0001-abcdef"))

    def test_unknown_or_foreign_task_refused(self):
        for tool in ("task_status", "wait_task", "cancel_task"):
            r = self.tcall(tool, task_id="someone-elses")
            self.assertEqual(r["error"]["code"], bg.POLICY_ERROR)
        self.start()
        self.assertIn("result", self.tcall("task_status", task_id="task-0001-abcdef"))
        listed = json.loads(self.tcall("list_tasks")["result"]["content"][0]["text"])["tasks"]
        self.assertEqual([t["task_id"] for t in listed], ["task-0001-abcdef"])

    def test_ownership_survives_restart(self):
        self.start()
        gw2 = bg.Gateway(TOKEN, self.root, {}, child=self.child, route=lambda *a, **k: {})
        self.assertEqual(gw2.tasks.get("task-0001-abcdef"), "grokbot")
        self.assertEqual(gw2.count("tasks"), 1)

    def test_concurrency_cap(self):
        self.start(); self.start()
        r = self.start()
        self.assertIn("concurrency cap", r["error"]["message"])
        self.child.states["task-0001-abcdef"] = "succeeded"
        self.assertIn("result", self.start())
        self.assertEqual(self.gw.count("refused"), 1)

    def test_daily_task_cap(self):
        self.gw.limits["max_tasks_per_day"] = 1
        self.start()
        self.child.states["task-0001-abcdef"] = "succeeded"
        self.assertIn("daily task cap", self.start()["error"]["message"])

    def test_route_task_cap_and_client(self):
        self.gw.limits["max_routes_per_day"] = 2
        self.assertIn("result", self.tcall("route_task", task="q1"))
        self.assertIn("result", self.tcall("route_task", task="q2"))
        r = self.tcall("route_task", task="q3")
        self.assertIn("route_task daily cap", r["error"]["message"])
        self.assertEqual([x["client"] for x in self.routes], ["grokbot", "grokbot"])
        self.assertEqual([u["outcome"] for u in self.usage() if u["tool"] == "route_task"], ["ok", "ok", "refused"])

    def test_other_tool_refused(self):
        r = self.tcall("Bash", command="ls")
        self.assertEqual(r["error"]["code"], bg.POLICY_ERROR)

    def test_usage_log_rotates(self):
        with mock.patch.object(bg, "USAGE_MAX_BYTES", 200):
            for _ in range(10):
                self.tcall("route_task", task="q")
        self.assertTrue(os.path.exists(os.path.join(self.home, bg.USAGE_LOG + ".1")))
        self.assertLess(os.path.getsize(os.path.join(self.home, bg.USAGE_LOG)), 400)

    def test_no_workspace_root_refuses(self):
        with self.assertRaises(bg.GatewayError):
            bg.Gateway(TOKEN, "", {})


class RouteTaskHeaderTest(unittest.TestCase):
    def test_route_task_sends_client_header_to_fake_provider(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return Resp(json.dumps({"model": "m", "content": [{"type": "text", "text": "hi"}]}).encode())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(subagent_mcp.NESTED_ENV, None)
            r = subagent_mcp.route_task("http://fake", "prov-token-xyz123", "q", urlopen=urlopen, client="grokbot")
        self.assertFalse(r.get("isError"))
        self.assertEqual(seen.get("x-cortados-client"), "grokbot")

    def test_child_event_fields_carry_client(self):
        self.assertEqual(ct._event_fields({"task_id": "t", "client": "grokbot"})["client"], "grokbot")
        self.assertNotIn("client", ct._event_fields({"task_id": "t"}))


class TokenFileTest(unittest.TestCase):
    def test_perms(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tok")
        with open(p, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(p, 0o644)
        with self.assertRaises(bg.GatewayError) as cm:
            bg.read_token_file(p)
        self.assertNotIn(TOKEN, str(cm.exception))
        os.chmod(p, 0o600)
        self.assertEqual(bg.read_token_file(p), TOKEN)
        os.symlink(p, p + ".link")
        with self.assertRaises(bg.GatewayError):
            bg.read_token_file(p + ".link")
        with self.assertRaises(bg.GatewayError):
            bg.read_token_file("")

    def test_main_refuses_bad_perms(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tok")
        with open(p, "w") as f:
            f.write(TOKEN)
        os.chmod(p, 0o640)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(bg.main(["--token-file", p, "--workspace-root", d]), 2)
        self.assertNotIn(TOKEN, err.getvalue())


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home})
        self.env.start()
        self.gw = bg.Gateway(TOKEN, tempfile.mkdtemp(), {}, child=FakeChild(), route=lambda *a, **k: {})
        self.err = io.StringIO()
        self.red = redirect_stderr(self.err)
        self.red.__enter__()
        self.srv = bg.build_server(self.gw, "127.0.0.1", 0)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/mcp"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.red.__exit__(None, None, None)
        self.env.stop()

    def post(self, obj, token=TOKEN):
        h = {"content-type": "application/json"}
        if token is not None:
            h["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, data=json.dumps(obj).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_auth(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(self.post(msg, None)[0], 401)
        self.assertEqual(self.post(msg, "wrong-token-0000000000")[0], 401)
        code, body = self.post(msg)
        self.assertEqual(code, 200)
        self.assertEqual(len(json.loads(body)["result"]["tools"]), 6)
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertNotIn(TOKEN, self.err.getvalue())
        self.assertIn("POST /mcp", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
