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
        # The start_task path is tested with it switched on (--enable-start-task); PhaseOneTest covers the default.
        self.gw = bg.Gateway(TOKEN, self.root, {"url": "http://fake", "token": "prov-token-xyz123"},
                             child=self.child, route=route, enable_start_task=True)

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
        self.assertEqual(gw2.running, {"task-0001-abcdef"})   # still counted against the cap until seen final
        self.child.states["task-0001-abcdef"] = "succeeded"
        gw2._refresh_running()
        self.assertEqual(gw2.running, set())

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

    def test_model_pins_refused(self):
        for pin in ("sub/opus", "api/claude-opus", "gpt-6"):
            r = self.start(orchestrator=pin)
            self.assertIn("model pins", r["error"]["message"], pin)
            r = self.tcall("route_task", task="q", model=pin)
            self.assertIn("model pins", r["error"]["message"], pin)
        self.assertEqual(self.child.calls, [])
        self.assertEqual(self.routes, [])
        self.assertIn("result", self.start(orchestrator="cortados"))
        self.assertNotIn("orchestrator", self.child.calls[-1][1])
        self.assertIn("result", self.tcall("route_task", task="q", model="cortados"))
        tools = {t["name"]: t for t in bg.tool_list(bg.EXPOSED)}
        self.assertNotIn("orchestrator", tools["start_task"]["inputSchema"]["properties"])
        self.assertNotIn("model", tools["route_task"]["inputSchema"]["properties"])
        self.assertIn("model", subagent_mcp.TOOL["inputSchema"]["properties"])

    def test_permission_grants_refused(self):
        for kw in ({"permission_mode": "dontAsk"}, {"permission_mode": "bypassPermissions"},
                   {"allowed_tools": ["Bash"]}, {"allowed_tools": "Bash"}):
            r = self.start(**kw)
            self.assertIn("phase 2 B5", r["error"]["message"], kw)
        self.assertEqual(self.child.calls, [])
        self.assertIn("result", self.start(permission_mode="plan"))
        a = self.child.calls[-1][1]
        self.assertEqual(a.get("permission_mode"), "plan")
        self.assertNotIn("allowed_tools", a)
        props = {t["name"]: t for t in bg.tool_list(bg.EXPOSED)}["start_task"]["inputSchema"]["properties"]
        self.assertFalse({"permission_mode", "allowed_tools"} & set(props))

    def test_parallel_starts_respect_cap(self):
        self.start()                                  # 1 of 2 running
        gate, entered = threading.Event(), []
        inner = self.child

        def blocking(name, a, cfg):
            if name == "start_task":
                entered.append(1)
                gate.wait(5)
            return inner(name, a, cfg)
        self.gw.child = blocking
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.start())) for _ in range(3)]
        for t in ts:
            t.start()
        for _ in range(200):
            if len(out) >= 2:
                break
            threading.Event().wait(0.01)
        gate.set()
        for t in ts:
            t.join(5)
        self.assertEqual(len(entered), 1)
        self.assertEqual(sum("result" in r for r in out), 1)
        self.assertEqual(sum("concurrency cap" in r.get("error", {}).get("message", "") for r in out), 2)
        self.assertFalse(any(t.startswith(bg._PENDING) for t in self.gw.running))

    def test_failed_start_releases_slot(self):
        def boom(name, a, cfg):
            if name == "start_task":
                raise RuntimeError("child died")
            return self.child(name, a, cfg)
        self.gw.child = boom
        with redirect_stderr(io.StringIO()):
            r = self.start()
        # item 7: a non-TaskError is a JSON-RPC -32603 naming only the type, not a dropped connection
        self.assertEqual(r["error"]["code"], -32603)
        self.assertEqual(r["error"]["message"], "internal error (RuntimeError)")
        self.assertNotIn("child died", json.dumps(r))
        self.assertEqual(self.gw.running, set())

    # -- review round P3 -------------------------------------------------------------------------------------------
    def test_load_skips_bad_lines(self):
        self.start()
        with open(os.path.join(self.home, bg.USAGE_LOG), "a") as f:
            f.write('[1, 2]\n"str"\n{"ts": "noon", "tool": "start_task", "outcome": "ok"}\n{"ts": true}\nnot json\n'
                    + "[" * 100000 + "\n")
        err = io.StringIO()
        with redirect_stderr(err):
            gw2 = bg.Gateway(TOKEN, self.root, {}, child=self.child, route=lambda *a, **k: {})
        self.assertIn("skipped 6", err.getvalue())
        self.assertEqual(gw2.count("tasks"), 1)
        self.assertEqual(gw2.tasks.get("task-0001-abcdef"), "grokbot")

    def test_counters_survive_lost_usage_log(self):
        self.start(); self.tcall("route_task", task="q")
        os.remove(os.path.join(self.home, bg.USAGE_LOG))      # as if two rotations dropped today's records
        with open(os.path.join(self.home, bg.COUNTERS_FILE)) as f:
            saved = json.load(f)
        self.assertEqual(saved["owned"], ["task-0001-abcdef"])
        gw2 = bg.Gateway(TOKEN, self.root, {}, child=self.child, route=lambda *a, **k: {})
        self.assertEqual(gw2.count("tasks"), 1)
        self.assertEqual(gw2.count("routes"), 1)
        self.assertEqual(gw2.tasks.get("task-0001-abcdef"), "grokbot")

    def test_refresh_keeps_task_on_transient_error(self):
        self.start()
        flaky = {"content": [{"type": "text", "text": "task_status: could not read meta"}], "isError": True}
        gone = {"content": [{"type": "text", "text": "task_status: no such task task-0001-abcdef"}], "isError": True}
        self.gw.child = lambda n, a, c: flaky
        self.gw._refresh_running()
        self.assertEqual(self.gw.running, {"task-0001-abcdef"})
        self.gw.child = lambda n, a, c: gone
        self.gw._refresh_running()
        self.assertEqual(self.gw.running, set())

    # -- review round 2 --------------------------------------------------------------------------------------------
    def test_permission_mode_always_plan_through_real_resolution(self):
        # P1-1: omitted / null / empty must not reach child_tasks as "unset" (which resolves to auto).
        for kw in ({}, {"permission_mode": None}, {"permission_mode": ""}, {"permission_mode": "plan"}):
            self.assertIn("result", self.start(**kw), kw)
            fwd = self.child.calls[-1][1]
            mode, _ = ct.resolve_permission_mode(fwd.get("permission_mode"), "cortados")
            self.assertEqual(mode, "plan", kw)
            for t in self.child.states:        # finish it, freeing the concurrency slot for the next start
                self.child.states[t] = "succeeded"

    def test_cwd_resolved_once(self):
        # P2-3: the value checked is the value forwarded; a second realpath after the check would see a swapped link.
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        calls = []
        orig = os.path.realpath

        def counting(p, *a, **k):
            calls.append(p)
            return orig(p, *a, **k)
        with mock.patch.object(bg.os.path, "realpath", counting):
            self.start(cwd=real)
        self.assertEqual(calls.count(real), 1)
        self.assertEqual(self.child.calls[-1][1]["cwd"], orig(real))

    def test_reservation_persisted_before_spawn(self):
        # P2-5: if the gateway dies after spawning but before recording the task id, a restarted gateway still holds
        # the slot (the reservation was on disk before the child launched).
        seen = {}

        def crashing(name, a, cfg):
            if name == "start_task":
                seen["restart"] = bg.Gateway(TOKEN, self.root, {}, child=FakeChild(), enable_start_task=True)
                raise SystemExit("gateway killed mid-start")
            return self.child(name, a, cfg)
        self.gw.child = crashing
        with self.assertRaises(SystemExit):
            self.start()
        again = seen["restart"]
        self.assertEqual(len(again.reserved), 1)
        self.assertTrue(all(s.startswith(bg._PENDING) for s in again.running))
        with mock.patch.dict(again.limits, {"max_concurrent": 1}):
            self.assertIn("concurrency cap", again.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "start_task", "arguments": {"task": "t", "cwd": self.root}}})["error"]["message"])
        # Past the child's max runtime the reservation is provably dead and expires.
        again.reservation_ttl = 0
        again._refresh_running()
        self.assertEqual((again.reserved, again.running), ({}, set()))

    def test_errors_are_fixed_text(self):
        # P3-6: a failed start (or any child error) never shows the bot the raw text; stderr gets the detail.
        leak = {"content": [{"type": "text", "text": "cannot read /Users/o/.cortados/provider-token"}], "isError": True}
        self.gw.child = lambda n, a, c: leak
        err = io.StringIO()
        with redirect_stderr(err):
            r = self.start()
        txt = json.dumps(r)
        self.assertNotIn("/Users/o", txt)
        self.assertIn("gateway log", txt)
        self.assertIn("/Users/o/.cortados/provider-token", err.getvalue())

    def test_success_fields_allowlisted(self):
        self.start()
        self.gw.child = lambda n, a, c: {"content": [{"type": "text", "text": json.dumps(
            {"task_id": "task-0001-abcdef", "state": "running", "new_owner_field": "/Users/o/secret"})}]}
        st = json.loads(self.tcall("task_status", task_id="task-0001-abcdef")["result"]["content"][0]["text"])
        self.assertEqual(set(st), {"task_id", "state"})

    def test_start_forwards_realpath(self):
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        link = os.path.join(self.root, "link")
        os.symlink(real, link)
        self.start(cwd=link)
        self.assertEqual(self.child.calls[-1][1]["cwd"], os.path.realpath(real))

    def test_list_asks_for_owned_ids(self):
        self.start()
        self.tcall("list_tasks")
        name, a, _ = self.child.calls[-1]
        self.assertEqual((name, a["ids"]), ("list_tasks", ["task-0001-abcdef"]))

    def test_paths_stripped(self):
        self.start()
        sub = os.path.join(self.root, "proj")
        os.mkdir(sub)

        def child(name, a, cfg):
            row = {"task_id": "task-0001-abcdef", "state": "running", "dir": "/Users/o/.cortados/x",
                   "stdout_log": "/Users/o/x/stdout.log", "stderr_log": "/Users/o/x/stderr.log", "cwd": sub}
            if name == "list_tasks":
                row = {"tasks": [row]}
            return {"content": [{"type": "text", "text": json.dumps(row)}]}
        self.gw.child = child
        st = json.loads(self.tcall("task_status", task_id="task-0001-abcdef")["result"]["content"][0]["text"])
        lt = json.loads(self.tcall("list_tasks")["result"]["content"][0]["text"])["tasks"][0]
        for out in (st, lt):
            for k in bg.PATH_FIELDS:
                self.assertNotIn(k, out)
            self.assertEqual(out["cwd"], "proj")

    def test_route_max_tokens_clamped(self):
        seen = []
        self.gw.route = lambda url, tok, task, ctx, mx, client=None: seen.append(mx) or {"content": []}
        for v in (10 ** 9, "big", True, -5, 100):
            self.tcall("route_task", task="q", max_tokens=v)
        self.assertEqual(seen, [bg.MAX_ROUTE_TOKENS] * 4 + [100])

    def test_refresh_does_not_hold_lock(self):
        self.start()
        held = []

        def probe(name, a, cfg):
            held.append(self.gw.lock.locked())
            return self.child(name, a, cfg)
        self.gw.child = probe
        self.gw._refresh_running()
        self.assertEqual(held, [False])

    def test_route_concurrency_cap(self):
        gate, out = threading.Event(), []

        def slow(url, token, task, context="", max_tokens=0, model=None, client=None):
            gate.wait(5)
            return {"content": [{"type": "text", "text": "a"}]}
        self.gw.route = slow
        ts = [threading.Thread(target=lambda: out.append(self.tcall("route_task", task="q"))) for _ in range(2)]
        for t in ts:
            t.start()
        for _ in range(200):
            if self.gw.routes_inflight == 2:
                break
            threading.Event().wait(0.01)
        r = self.tcall("route_task", task="q3")
        self.assertIn("route_task concurrency cap", r["error"]["message"])
        gate.set()
        for t in ts:
            t.join(5)
        self.assertEqual(self.gw.routes_inflight, 0)
        self.assertIn("result", self.tcall("route_task", task="q4"))

    def test_main_refuses_non_loopback_host(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(bg.main(["--token-file", "/nonexistent", "--workspace-root", self.root,
                                      "--host", "0.0.0.0"]), 2)
        self.assertIn("--children-run-as-me", err.getvalue())
        self.assertIn("phase 2 B5", err.getvalue())

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


class PhaseOneTest(unittest.TestCase):
    """Default (no --enable-start-task): route_task only (owner decision 2026-11-03, review r2 P1-2)."""
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": tempfile.mkdtemp()})
        self.env.start()
        self.child = FakeChild()
        self.gw = bg.Gateway(TOKEN, tempfile.mkdtemp(), {}, child=self.child,
                             route=lambda *a, **k: {"content": [{"type": "text", "text": "answer"}]})

    def tearDown(self):
        self.env.stop()

    def call(self, name, **a):
        return self.gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": a}})

    def test_only_route_task_offered(self):
        r = self.gw.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([t["name"] for t in r["result"]["tools"]], ["route_task"])

    def test_start_and_task_tools_refused(self):
        r = self.call("start_task", task="x", cwd=self.gw.root)
        self.assertEqual(r["error"]["message"], "refused by bot gateway policy: " + bg.START_DISABLED)
        for name in ("wait_task", "task_status", "cancel_task", "list_tasks"):
            self.assertIn("phase 2, B5", self.call(name, task_id="t")["error"]["message"], name)
        self.assertEqual(self.child.calls, [])
        self.assertEqual(self.call("route_task", task="q")["result"]["content"][0]["text"], "answer")

    def test_enable_flag_needs_children_run_as_me(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(bg.main(["--enable-start-task", "--token-file", "x", "--workspace-root", "/"]), 2)
        self.assertIn("--children-run-as-me", err.getvalue())


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


class AuthThrottleTest(unittest.TestCase):
    def test_fake_clock_cooldown_doubles_and_caps(self):
        now = [1000.0]
        throttle = bg.AuthThrottle(lambda: now[0])
        for expected in (60, 120, 240, 480, 960, 1920, 3600, 3600):
            for _ in range(bg.AUTH_FAILURES):
                self.assertIsNone(throttle.failed("198.51.100.7"))
            self.assertEqual(throttle.blocked("198.51.100.7"), expected)
            now[0] += expected + .1

    def test_window_global_cap_and_bounded_state(self):
        now = [0.0]
        throttle = bg.AuthThrottle(lambda: now[0])
        with mock.patch.object(bg, "MAX_AUTH_CLIENTS", 2):
            throttle.failed("198.51.100.1")
            throttle.failed("198.51.100.2")
            throttle.failed("198.51.100.3")
        self.assertEqual(list(throttle.clients), ["198.51.100.2", "198.51.100.3"])
        now[0] += bg.AUTH_WINDOW_S + 1
        for i in range(bg.GLOBAL_AUTH_FAILURES):
            self.assertIsNone(throttle.failed(f"203.0.113.{i}"))
        self.assertEqual(throttle.failed("203.0.113.250"), 60)

    def test_source_churn_does_not_evict_active_bans(self):
        now = [0.0]
        throttle = bg.AuthThrottle(lambda: now[0])
        with mock.patch.object(bg, "MAX_AUTH_CLIENTS", 2):
            for address in ("198.51.100.1", "198.51.100.2"):
                for _ in range(bg.AUTH_FAILURES):
                    throttle.failed(address)
            self.assertEqual(throttle.failed("198.51.100.3"), 60)  # bounded overflow is signalled, not stored
        self.assertEqual(set(throttle.clients), {"198.51.100.1", "198.51.100.2"})
        self.assertIsNotNone(throttle.blocked("198.51.100.1"))

    def test_xff_only_from_loopback_and_last_hop(self):
        self.assertEqual(bg.client_address("127.0.0.1", "192.0.2.1, 198.51.100.2"), "198.51.100.2")
        self.assertEqual(bg.client_address("203.0.113.8", "198.51.100.2"), "203.0.113.8")
        self.assertEqual(bg.client_address("127.0.0.1", "not-an-address"), "127.0.0.1")


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

    def post(self, obj, token=TOKEN, headers=None):
        h = {"content-type": "application/json", **(headers or {})}
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
        self.assertEqual(len(json.loads(body)["result"]["tools"]), 1)    # phase 1: route_task only
        self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        self.assertNotIn(TOKEN, self.err.getvalue())
        self.assertIn("POST /mcp", self.err.getvalue())

    def test_per_address_throttle_and_valid_other_address(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        bad = {"X-Forwarded-For": "198.51.100.11"}
        for _ in range(bg.AUTH_FAILURES):
            self.assertEqual(self.post(msg, "wrong-token-0000000000", bad)[0], 401)
        # A bad actor sharing the public egress address cannot lock the legitimate bearer out.
        self.assertEqual(self.post(msg, TOKEN, bad)[0], 200)
        self.assertEqual(self.post(msg, "wrong-token-0000000000", bad)[0], 429)
        self.assertEqual(self.post(msg, TOKEN, {"X-Forwarded-For": "198.51.100.12"})[0], 200)
        log = self.err.getvalue()
        self.assertIn("POST /mcp 429 198.51.100.11", log)
        self.assertIn("POST /mcp 200 198.51.100.12", log)
        self.assertNotIn(TOKEN, log)

    def test_allowed_host_returns_421_before_auth(self):
        srv = bg.build_server(self.gw, "127.0.0.1", 0, ["gateway.example.test"])
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        old_url = self.url
        try:
            self.url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
            msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            self.assertEqual(self.post(msg, None)[0], 421)
            self.assertEqual(self.post(msg, TOKEN, {"Host": "gateway.example.test"})[0], 200)
        finally:
            self.url = old_url
            srv.shutdown()
            srv.server_close()

    def test_batch_cap_and_timeout(self):
        one = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        code, body = self.post([one] * bg.MAX_BATCH)
        self.assertEqual((code, len(json.loads(body))), (200, bg.MAX_BATCH))
        code, body = self.post([one] * (bg.MAX_BATCH + 1))
        self.assertEqual(json.loads(body)["error"]["code"], -32600)
        self.assertEqual(bg.make_handler(self.gw).timeout, bg.REQUEST_TIMEOUT_S)

    def test_inflight_cap_503(self):
        with mock.patch.object(bg, "MAX_INFLIGHT", 0):
            srv = bg.build_server(self.gw, "127.0.0.1", 0)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            self.url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"
            self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 503)
            # P2-4: refused at accept, before any header is parsed: a client that sends nothing still gets the 503.
            import socket
            with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5) as s:
                self.assertTrue(s.recv(100).startswith(b"HTTP/1.1 503"))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_header_deadline_closes_trickler(self):
        # P2-4: a client that never finishes its headers is shut at HEADER_DEADLINE_S, freeing its slot.
        import socket
        import time
        with mock.patch.object(bg, "HEADER_DEADLINE_S", 0.3):
            with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5) as s:
                s.sendall(b"POST /mcp HTTP/1.1\r\nhost: x\r\n")
                t0 = time.monotonic()
                self.assertEqual(s.recv(100), b"")
                self.assertLess(time.monotonic() - t0, 3)

    def test_malformed_and_too_many_headers_return_errors_without_traceback(self):
        import socket
        for request, expected in (
            (b"BOGUS\r\n\r\n", b"400"),
            (b"GET /mcp HTTP/1.1\r\n" + b"X: y\r\n" * 101 + b"\r\n", b"431"),
        ):
            with self.subTest(expected=expected):
                with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5) as s:
                    s.sendall(request)
                    self.assertIn(expected, s.recv(4096).split(b"\r\n", 1)[0])
        self.assertNotIn("Traceback", self.err.getvalue())
        self.assertNotIn("AttributeError", self.err.getvalue())

    def test_body_deadline_closes_trickler(self):
        import socket
        import time
        with mock.patch.object(bg, "BODY_DEADLINE_S", 0.3):
            with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5) as s:
                s.sendall(b"POST /mcp HTTP/1.1\r\nhost: x\r\nauthorization: Bearer " + TOKEN.encode() +
                          b"\r\ncontent-length: 100\r\n\r\n{")
                t0 = time.monotonic()
                self.assertEqual(s.recv(100), b"")
                self.assertLess(time.monotonic() - t0, 3)


if __name__ == "__main__":
    unittest.main()
