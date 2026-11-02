"""Child tasks (start_task & co. on the cortados-route MCP server) against a FAKE claude binary: no real claude runs,
no provider is contacted, and all state lives in a throwaway tasks dir."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import io
import time
import unittest
import urllib.error
from unittest import mock

from cortados_router import child_tasks as ct
from cortados_router import subagent_mcp as m

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, sys, time
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    with open(os.path.join(os.getcwd(), "fake-argv.json"), "w") as f:
        json.dump({{"argv": sys.argv[1:], "prompt": prompt, "nested": os.environ.get("CORTADOS_CHILD_TASK"),
                   "subagent": os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"),
                   "claudecode": os.environ.get("CLAUDECODE"),
                   "headers": os.environ.get("ANTHROPIC_CUSTOM_HEADERS")}}, f)
    if mode == "sleep":
        time.sleep(60)
    if mode == "fail":
        sys.stderr.write("boom: something broke\\n")
        sys.exit(3)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "done: " + prompt,
                      "num_turns": 2, "modelUsage": {{"claude-fable-5": {{}}, "gpt-6.1-sol": {{}}}}}}))
''')


class _Resp:
    def __init__(self, obj):
        self.b = json.dumps(obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.b


def _events():
    with open(os.path.join(os.environ["CORTADOS_HOME"], "child-tasks.jsonl")) as f:
        return [json.loads(line) for line in f]


def _load(path):
    with open(path) as f:
        return json.load(f)


class ChildTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())   # macOS /var is a symlink; the private-dir check refuses symlinks
        self.work = os.path.join(self.tmp, "work")
        os.mkdir(self.work)
        bin_ = os.path.join(self.tmp, "fake-claude")
        with open(bin_, "w") as f:
            f.write(FAKE.format(py=sys.executable))
        os.chmod(bin_, 0o755)
        tok = os.path.join(self.tmp, "token")
        with open(tok, "w") as f:
            f.write("tok-test-000000001")
        self.env = mock.patch.dict(os.environ, {"CORTADOS_TASKS_DIR": os.path.join(self.tmp, "tasks"),
                                                "CORTADOS_CHILD_CLAUDE_BIN": bin_, "FAKE_CLAUDE_MODE": "ok",
                                                "CLAUDECODE": "1",
                                                "CORTADOS_CONNECTORS_MCP_CONFIG": os.path.join(self.tmp, "no-conn.json"),
                                                "CORTADOS_HOME": os.path.join(self.tmp, "home"),
                                                "CORTADOS_SCARCITY_REFRESH": "off"})
        self.env.start()
        os.environ.pop("CORTADOS_CHILD_TASK", None)
        self.addCleanup(self.env.stop)
        p = mock.patch.object(ct, "MANAGED_DIRS", (os.path.join(self.tmp, "no-managed"),))
        p.start()
        self.addCleanup(p.stop)
        self.runs = {}

        def fake_urlopen(req, timeout=None):
            rid = req.full_url.rsplit("/", 1)[1]
            if rid not in self.runs:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return _Resp(self.runs[rid])
        u = mock.patch.object(ct, "_urlopen", fake_urlopen)
        u.start()
        self.addCleanup(u.stop)
        self.cfg = {"url": "http://provider.invalid:8432", "token": "tok-test-000000001", "token_file": tok}

    def tearDown(self):
        for tid, proc in list(ct._REAP.items()):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 15)
                except ProcessLookupError:
                    pass
                proc.wait()
        ct._REAP.clear()

    def call(self, name, **args):
        r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
                     self.cfg)["result"]
        return r, (None if r.get("isError") else json.loads(r["content"][0]["text"]))

    def start(self, **kw):
        r, out = self.call("start_task", task=kw.pop("task", "hello"), cwd=kw.pop("cwd", self.work), **kw)
        self.assertFalse(r.get("isError"), r)
        return out["task_id"]

    def test_tools_list_includes_child_tools(self):
        r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.cfg)
        names = [t["name"] for t in r["result"]["tools"]]
        self.assertEqual(names[0], "route_task")
        for n in ("start_task", "task_status", "wait_task", "list_tasks", "cancel_task"):
            self.assertIn(n, names)
        self.assertEqual(m._MODES, list(ct.PERMISSION_MODES))

    def test_start_status_wait_success(self):
        tid = self.start(task="build it")
        self.assertRegex(tid, ct.TASK_ID_RE)
        _, st = self.call("task_status", task_id=tid)
        self.assertIn(st["state"], ("running", "succeeded"))
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        self.assertEqual(w["result"], "done: build it")
        self.assertEqual(w["models"], ["claude-fable-5", "gpt-6.1-sol"])
        self.assertEqual(w["exit_code"], 0)
        seen = _load(os.path.join(self.work, "fake-argv.json"))
        self.assertEqual(seen["prompt"], "build it")          # prompt on stdin, not argv
        self.assertNotIn("build it", seen["argv"])
        a = seen["argv"]
        self.assertEqual(a[a.index("--model") + 1], "cortados")
        self.assertEqual(a[a.index("--permission-mode") + 1], "auto")
        self.assertNotIn("--mcp-config", a)                      # no connectors file: no flag, a note in the log
        with open(os.path.join(w["dir"], "runner.log")) as f:
            self.assertIn("connectors MCP config", f.read())
        self.assertTrue(w["settings_removed"])                   # the token-bearing settings file is gone
        self.assertFalse([n for n in os.listdir(w["dir"]) if n.startswith("session-")])
        self.assertIn("-p", a)
        self.assertEqual(a[a.index("--output-format") + 1], "json")
        self.assertNotIn("--append-system-prompt", a)          # cortados orchestrator: no directive
        self.assertNotIn("tok-test-000000001", " ".join(a))    # token never in argv
        self.assertEqual(seen["nested"], "1")
        self.assertEqual(seen["subagent"], "cortados")
        self.assertIsNone(seen["claudecode"])
        d = w["dir"]
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(ct.tasks_dir()).st_mode), 0o700)
        _, lst = self.call("list_tasks")
        self.assertEqual(lst["tasks"][0]["task_id"], tid)

    def test_pinned_orchestrator_and_allowlist(self):
        tid = self.start(orchestrator="opus", permission_mode="plan", allowed_tools=["Bash(git diff:*)", "Read"])
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertTrue(a[a.index("--model") + 1].startswith("sub/"))
        self.assertEqual(a[a.index("--permission-mode") + 1], "plan")
        self.assertEqual(a[a.index("--allowedTools") + 1], "Bash(git diff:*),Read")

    def test_connectors_config_passed_when_present(self):
        conn = os.path.join(self.tmp, "connectors.mcp.json")
        with open(conn, "w") as f:
            json.dump({"mcpServers": {}}, f)
        os.environ["CORTADOS_CONNECTORS_MCP_CONFIG"] = conn
        tid = self.start(permission_mode="dontAsk")
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertEqual(a[a.index("--mcp-config") + 1], conn)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")

    def test_settings_file_exists_while_running(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start()
        d = ct.task_path(tid)
        self.assertTrue([n for n in os.listdir(d) if n.startswith("session-")])
        self.call("cancel_task", task_id=tid)
        self.call("wait_task", task_id=tid, timeout_s=30)

    def test_prune_old_finished_tasks_on_start(self):
        root = ct._private_root()
        old, live = "t20200101000000-0000000a", "t20200101000000-0000000b"
        for tid in (old, live):
            os.mkdir(os.path.join(root, tid), 0o700)
        ct._write_json(os.path.join(root, old, "meta.json"), {"started_at": 1.0, "runner_pid": 0})
        ct._write_json(os.path.join(root, old, "result.json"), {"exit_code": 0, "ended_at": 2.0})
        # a "running" old task: its runner pid is alive (this test process), so it is kept
        ct._write_json(os.path.join(root, live, "meta.json"), {"started_at": 1.0, "runner_pid": os.getpid()})
        self.start()
        names = os.listdir(root)
        self.assertNotIn(old, names)
        self.assertIn(live, names)

    def test_chrome_opt_in_and_single_driver(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start(chrome=True)
        self.assertTrue(_load(os.path.join(ct.task_path(tid), "meta.json"))["chrome"])
        self.assertIn("--chrome", _load(os.path.join(ct.task_path(tid), "meta.json"))["argv"])
        r, _ = self.call("start_task", task="x", cwd=self.work, chrome=True)
        self.assertTrue(r.get("isError"))
        self.assertIn("already driving Chrome", r["content"][0]["text"])
        other = self.start()                                  # a non-chrome task may still run alongside
        self.assertNotIn("--chrome", _load(os.path.join(ct.task_path(other), "meta.json"))["argv"])
        for t in (tid, other):
            self.call("cancel_task", task_id=t)
            self.call("wait_task", task_id=t, timeout_s=30)

    def test_router_run_id_header_and_run_record(self):
        tid = self.start(task="secret-ish prompt text")
        self.runs[tid] = {"run_id": tid, "model": "gpt-6.1-sol", "label": "x", "provider": "codex-cli",
                          "models": ["gpt-6.1-sol"], "builder_models": ["gpt-6.1-sol"]}
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["router_run"]["models"], ["gpt-6.1-sol"])
        self.assertEqual(w["router_run"]["provider"], "codex-cli")
        seen = _load(os.path.join(self.work, "fake-argv.json"))
        self.assertIn("x-cortados-run-id: " + tid, seen["headers"])
        self.assertIn("x-cortados-reveal: 1", seen["headers"])
        ev = _events()
        self.assertEqual([e["event"] for e in ev], ["started", "finished"])
        self.assertEqual(ev[1]["status"], "succeeded")
        self.assertEqual(ev[1]["exit_code"], 0)
        self.assertIsInstance(ev[1]["duration_s"], float)
        self.assertEqual(ev[0]["task_id"], tid)
        with open(os.path.join(os.environ["CORTADOS_HOME"], "child-tasks.jsonl")) as f:
            raw = f.read()
        self.assertNotIn("secret-ish", raw)
        self.assertNotIn("tok-test", raw)

    def test_run_record_fetch_failure_is_a_field(self):
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded")
        self.assertIn("404", w["router_run"]["error"])

    def test_no_parallel_cap(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        ids = [self.start(cwd=self.work) for _ in range(6)]   # more than the old cap of 4
        for t in ids:
            _, st = self.call("task_status", task_id=t)
            self.assertEqual(st["state"], "running")
        for t in ids:
            self.call("cancel_task", task_id=t)
        for t in ids:
            _, w = self.call("wait_task", task_id=t, timeout_s=30)
            self.assertEqual(w["state"], "cancelled")
        kinds = [e["event"] for e in _events()]
        self.assertEqual(kinds.count("cancelled"), 6)
        self.assertEqual(kinds.count("started"), 6)

    def test_failure_exit(self):
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["exit_code"], 3)
        self.assertIn("boom", w["stderr_tail"])

    def test_cancel(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start()
        deadline = time.time() + 10
        while not os.path.exists(os.path.join(self.work, "fake-argv.json")) and time.time() < deadline:
            time.sleep(0.1)
        _, c = self.call("cancel_task", task_id=tid)
        self.assertTrue(c["cancelled"], c)
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "cancelled", w)
        self.assertTrue(w["settings_removed"])
        self.assertFalse([n for n in os.listdir(w["dir"]) if n.startswith("session-")])
        _, again = self.call("cancel_task", task_id=tid)
        self.assertFalse(again["cancelled"])

    def test_bad_task_id(self):
        for bad in ("../../etc", "t1", "", None, "t20261102000000-zzzzzzzz"):
            r, _ = self.call("task_status", task_id=bad)
            self.assertTrue(r.get("isError"), bad)
        r, _ = self.call("cancel_task", task_id="t20261102000000-0000000a")
        self.assertIn("no such task", r["content"][0]["text"])

    def test_bad_cwd(self):
        for cwd in ("relative/dir", os.path.join(self.tmp, "missing")):
            r, _ = self.call("start_task", task="x", cwd=cwd)
            self.assertTrue(r.get("isError"), cwd)

    def test_permission_mode_outside_allowed_set(self):
        for mode in ("bypassPermissions", "dangerously-skip-permissions", "yolo"):
            r, _ = self.call("start_task", task="x", cwd=self.work, permission_mode=mode)
            self.assertTrue(r.get("isError"))
            self.assertIn("not allowed", r["content"][0]["text"])

    def test_nested_child_refused(self):
        os.environ["CORTADOS_CHILD_TASK"] = "1"
        r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertIn("inside a child task", r["content"][0]["text"])

    def test_wait_timeout_capped(self):
        r, _ = self.call("wait_task", task_id="t20261102000000-0000000a", timeout_s=9999)
        self.assertTrue(r.get("isError"))

    def test_unconfigured_url(self):
        self.cfg["url"] = ""
        r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertIn("not configured", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
