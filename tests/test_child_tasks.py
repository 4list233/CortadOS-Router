"""Child tasks (start_task & co. on the cortados-route MCP server) against a FAKE claude binary: no real claude runs,
no provider is contacted, and all state lives in a throwaway tasks dir."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
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
                   "claudecode": os.environ.get("CLAUDECODE")}}, f)
    if mode == "sleep":
        time.sleep(60)
    if mode == "fail":
        sys.stderr.write("boom: something broke\\n")
        sys.exit(3)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "done: " + prompt,
                      "num_turns": 2, "modelUsage": {{"claude-fable-5": {{}}, "gpt-6.1-sol": {{}}}}}}))
''')


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
                                                "CLAUDECODE": "1"})
        self.env.start()
        os.environ.pop("CORTADOS_CHILD_TASK", None)
        self.addCleanup(self.env.stop)
        p = mock.patch.object(ct, "MANAGED_DIRS", (os.path.join(self.tmp, "no-managed"),))
        p.start()
        self.addCleanup(p.stop)
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
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
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
        for mode in ("bypassPermissions", "dangerously-skip-permissions", "auto"):
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
