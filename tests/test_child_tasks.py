"""Child tasks (start_task & co. on the cortados-route MCP server) against a FAKE claude binary: no real claude runs,
no provider is contacted, and all state lives in a throwaway tasks dir."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import io
import signal
import time
import unittest
import urllib.error
from unittest import mock

from cortados_router import child_tasks as ct
from cortados_router import subagent_mcp as m

FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, signal, subprocess, sys, time
    prompt = sys.stdin.read()
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    with open(os.path.join(os.getcwd(), "fake-argv.json"), "w") as f:
        json.dump({{"argv": sys.argv[1:], "prompt": prompt, "nested": os.environ.get("CORTADOS_CHILD_TASK"),
                   "subagent": os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL"),
                   "claudecode": os.environ.get("CLAUDECODE"),
                   "headers": os.environ.get("ANTHROPIC_CUSTOM_HEADERS"),
                   "pythonpath": os.environ.get("PYTHONPATH"), "pid": os.getpid()}}, f)
    if mode == "sleep":
        time.sleep(60)
    if mode == "stubborn":
        # ignores SIGTERM and leaves a grandchild that ignores it too: only a group SIGKILL stops them
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        g = subprocess.Popen([sys.executable, "-c",
                              "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
        with open(os.path.join(os.getcwd(), "grandchild.pid"), "w") as f:
            f.write(str(g.pid))
        time.sleep(120)
    if mode == "termok":
        # exits 0 with a success result when it gets SIGTERM (as at a timeout)
        def ok(*_):
            print(json.dumps({{"type": "result", "is_error": False, "result": "fine"}}))
            sys.stdout.flush()
            os._exit(0)
        signal.signal(signal.SIGTERM, ok)
        time.sleep(120)
    if mode == "echo-env":
        sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
        print(json.dumps({{"type": "result", "is_error": True, "result": os.environ.get("FAKE_RESULT", "")}}))
        sys.exit(1)
    if mode == "crash-orphan":
        g = subprocess.Popen([sys.executable, "-c",
                              "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
        with open(os.path.join(os.getcwd(), "grandchild.pid"), "w") as f:
            f.write(str(g.pid))
        time.sleep(0.3)
        sys.exit(3)
    if mode == "echo-sleep":
        sys.stderr.write("stderr has " + os.environ["ANTHROPIC_AUTH_TOKEN"] + "\\n")
        sys.stderr.flush()
        open(os.path.join(os.getcwd(), "echoed"), "w").close()
        time.sleep(60)
    if mode == "badjson":
        print("this is not json")
        sys.exit(0)
    if mode == "empty":
        sys.exit(0)
    if mode == "scalar":
        print("42")
        sys.exit(0)
    if mode == "wrongshape":
        print(json.dumps({{"foo": 1}}))
        sys.exit(0)
    if mode == "echo-auth":
        tok = os.environ["ANTHROPIC_AUTH_TOKEN"]
        time.sleep(0.5)
        sys.stderr.write("stderr has " + tok + "\\n")
        print(json.dumps({{"type": "result", "is_error": False, "result": "result has " + tok}}))
        sys.exit(0)
    if mode == "leak":
        tok = open(os.environ["FAKE_LEAK_FILE"]).read().strip()
        print(json.dumps({{"type": "result", "is_error": False, "result": "the token is " + tok}}))
        sys.exit(0)
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


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_file(path, timeout=10):
    end = time.time() + timeout
    while not os.path.exists(path) and time.time() < end:
        time.sleep(0.05)
    return os.path.exists(path)


def _load(path):
    with open(path) as f:
        return json.load(f)


class InstalledClaudeTests(unittest.TestCase):
    """The allowed modes must be choices of the installed claude (reads `claude --help` only; no session, no API call)."""

    def test_permission_modes_are_claude_choices(self):
        import re
        import shutil
        import subprocess
        exe = shutil.which("claude")
        if not exe:
            self.skipTest("no claude on PATH")
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"claude --help did not run: {exc}")
        m_ = re.search(r"--permission-mode <mode>.*?\(choices:(.*?)\)", out, re.S)
        if not m_:
            self.skipTest("claude --help lists no --permission-mode choices")
        choices = set(re.findall(r'"([A-Za-z]+)"', m_.group(1)))
        self.assertTrue(set(ct.PERMISSION_MODES) <= choices, (ct.PERMISSION_MODES, choices))
        self.assertIn("--permission-prompts", out)


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
        g = mock.patch.object(ct, "KILL_GRACE_S", 1)
        g.start()
        self.addCleanup(g.stop)
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
        self.assertEqual(a[a.index("--append-system-prompt") + 1], ct.CHILD_DIRECTIVE)   # cortados: child directive only
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
        # a "running" old task: something holds its runner lock (this test, standing in for a live runner): kept
        ct._write_json(os.path.join(root, live, "meta.json"), {"started_at": 1.0, "runner_pid": os.getpid()})
        import fcntl
        fd = os.open(os.path.join(root, live, "runner.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.start()
        names = os.listdir(root)
        self.assertNotIn(old, names)
        self.assertIn(live, names)

    def test_chrome_opt_in_and_parallel(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start(chrome=True)
        tid2 = self.start(chrome=True)                        # two chrome tasks run together: no concurrency limit
        for t in (tid, tid2):
            meta = _load(os.path.join(ct.task_path(t), "meta.json"))
            self.assertTrue(meta["chrome"])
            self.assertIn("--chrome", meta["argv"])
            _, st = self.call("task_status", task_id=t)
            self.assertEqual(st["state"], "running")
        other = self.start()
        self.assertNotIn("--chrome", _load(os.path.join(ct.task_path(other), "meta.json"))["argv"])
        r, _ = self.call("start_task", task="x", cwd=self.work, chrome="yes")
        self.assertIn("chrome must be true or false", r["content"][0]["text"])
        for t in (tid, tid2, other):
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

    def test_permission_prompts_none_and_no_pythonpath_leak(self):
        tid = self.start()
        self.call("wait_task", task_id=tid, timeout_s=30)
        seen = _load(os.path.join(self.work, "fake-argv.json"))
        a = seen["argv"]
        self.assertEqual(a[a.index("--permission-prompts") + 1], "none")
        self.assertNotIn(str(ct.REPO_ROOT), seen["pythonpath"] or "")

    def test_reused_pid_is_not_signalled(self):
        # A task whose recorded runner pid now belongs to an unrelated live process (this test process) and whose
        # lock nobody holds: not running, and cancel must refuse rather than signal that pid.
        root = ct._private_root()
        tid = "t20261102000000-0000abcd"
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time() - 3600,
                                                      "runner_pid": os.getpid()})
        open(os.path.join(d, "runner.lock"), "w").close()
        with open(os.path.join(d, "session-x.json"), "w") as f:
            f.write("{}")
        with mock.patch.object(ct.os, "kill") as kill, mock.patch.object(ct.os, "killpg") as killpg:
            _, c = self.call("cancel_task", task_id=tid)
            kill.assert_not_called()
            killpg.assert_not_called()
        self.assertFalse(c["cancelled"])
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        self.assertFalse(os.path.exists(os.path.join(d, "session-x.json")))

    def test_cancel_refuses_live_lock_with_foreign_pid(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start()
        d = ct.task_path(tid)
        meta = _load(os.path.join(d, "meta.json"))
        real = meta["runner_pid"]
        meta["runner_pid"] = os.getpid()                       # lock held by the real runner, pid is someone else
        ct._write_json(os.path.join(d, "meta.json"), meta)
        _, c = self.call("cancel_task", task_id=tid)
        self.assertFalse(c["cancelled"])
        meta["runner_pid"] = real
        ct._write_json(os.path.join(d, "meta.json"), meta)
        _wait_file(os.path.join(self.work, "fake-argv.json"))
        _, c = self.call("cancel_task", task_id=tid)
        self.assertTrue(c["cancelled"])
        self.call("wait_task", task_id=tid, timeout_s=30)

    def test_cancel_kills_stubborn_child_and_grandchild(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stubborn"
        tid = self.start()
        gpath = os.path.join(self.work, "grandchild.pid")
        self.assertTrue(_wait_file(gpath))
        time.sleep(0.2)
        with open(gpath) as f:
            gpid = int(f.read())
        _, c = self.call("cancel_task", task_id=tid)
        self.assertTrue(c["cancelled"])
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "cancelled", w)
        end = time.time() + 5
        while _pid_alive(gpid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_pid_alive(gpid), "grandchild survived the group SIGKILL")

    def test_timeout_kills_group(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stubborn"
        os.environ["CORTADOS_CHILD_MAX_S"] = "1"
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "failed")
        self.assertTrue(w.get("timed_out"))
        with open(os.path.join(self.work, "grandchild.pid")) as f:
            gpid = int(f.read())
        end = time.time() + 5
        while _pid_alive(gpid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_pid_alive(gpid))

    def test_bad_max_s_names_the_variable(self):
        os.environ["CORTADOS_CHILD_MAX_S"] = "soon"
        r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertIn("CORTADOS_CHILD_MAX_S", r["content"][0]["text"])
        self.assertEqual(os.listdir(ct.tasks_dir()) if os.path.isdir(ct.tasks_dir()) else [], [])

    def test_failed_start_leaves_no_dir(self):
        with mock.patch.object(ct.session, "prepare", side_effect=ct.session.SessionError("nope")):
            r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertTrue(r.get("isError"))
        self.assertEqual([n for n in os.listdir(ct.tasks_dir()) if ct.TASK_ID_RE.fullmatch(n)], [])

    def test_metaless_old_dir_is_failed(self):
        root = ct._private_root()
        tid = "t20261102000000-0000beef"
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "running")             # fresh: start() may be mid-way
        os.utime(d, (time.time() - 120, time.time() - 120))
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")

    def test_token_file_token_is_scrubbed(self):
        os.environ["FAKE_CLAUDE_MODE"] = "leak"
        os.environ["FAKE_LEAK_FILE"] = self.cfg["token_file"]
        self.cfg["token"] = "env-token-different-0001"           # the env token differs from the file's
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertNotIn("tok-test-000000001", json.dumps(w))

    # --- review round 2 ---------------------------------------------------------------------------------------------

    def _manual_task(self, mode="sleep"):
        """A task dir prepared by hand, for driving ct._run in-process."""
        root = ct._private_root()
        tid = ct._new_id()
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        with open(os.path.join(d, "prompt.txt"), "w") as f:
            f.write("p")
        env_bin = os.environ["CORTADOS_CHILD_CLAUDE_BIN"]
        ct._write_json(os.path.join(d, "cmd.json"), {"argv": [env_bin], "cwd": self.work, "max_s": 60, "grace_s": 1,
                                                    "event": {"task_id": tid}, "started_at": time.time()})
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time(), "runner_pid": None})
        os.environ["FAKE_CLAUDE_MODE"] = mode
        return tid, d

    def test_cancel_during_popen_is_not_dropped(self):
        tid, d = self._manual_task("sleep")
        real = ct.subprocess.Popen

        def popen_with_sigterm(*a, **kw):
            os.kill(os.getpid(), signal.SIGTERM)   # lands while Popen runs: the handler sees no proc yet
            return real(*a, **kw)
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct.subprocess, "Popen", popen_with_sigterm):
            t0 = time.time()
            ct._run(d)
        self.assertLess(time.time() - t0, 20, "the latched cancel stopped the 60 s child")
        res = _load(os.path.join(d, "result.json"))
        self.assertTrue(res["cancelled"], res)
        self.assertEqual(ct.classify(res), "cancelled")

    def test_cancel_marker_before_spawn_skips_the_child(self):
        tid, d = self._manual_task("ok")
        open(os.path.join(d, "cancelled"), "w").close()
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        ct._run(d)
        res = _load(os.path.join(d, "result.json"))
        self.assertTrue(res["cancelled"])
        self.assertFalse(os.path.exists(os.path.join(self.work, "fake-argv.json")), "no child was spawned")

    def test_status_in_the_window_before_spawn_does_not_recover(self):
        # Poll exactly between meta.json being published and the runner being spawned: the lock is already held by
        # start(), so the task is running and its settings file is untouched.
        seen = {}
        real = ct._prepare

        def prepare_then_poll(d, *a, **kw):
            out = real(d, *a, **kw)
            seen["state"] = ct._state(d)[0]
            seen["settings"] = [n for n in os.listdir(d) if n.startswith("session-")]
            seen["result"] = os.path.exists(os.path.join(d, "result.json"))
            return out
        with mock.patch.object(ct, "_prepare", prepare_then_poll):
            tid = self.start()
        self.assertEqual(seen["state"], "running")
        self.assertTrue(seen["settings"])
        self.assertFalse(seen["result"])
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)

    def test_timeout_with_exit_zero_is_failed(self):
        os.environ["FAKE_CLAUDE_MODE"] = "termok"
        os.environ["CORTADOS_CHILD_MAX_S"] = "1"
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertTrue(w.get("timed_out"), w)
        self.assertEqual(w["exit_code"], 0)
        self.assertEqual(w["state"], "failed")
        self.assertEqual(ct.classify({"exit_code": 0, "timed_out": True, "output": {"is_error": False}}), "failed")
        self.assertEqual(ct.classify({"exit_code": 0, "error": "x"}), "failed")
        self.assertEqual(ct.classify({"exit_code": 0, "cancelled": True}), "cancelled")
        self.assertEqual([e["status"] for e in _events() if e["event"] == "finished"], ["failed"])

    def test_simultaneous_recovery(self):
        import threading
        root = ct._private_root()
        tid = "t20261102000000-00000ccc"
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time() - 3600,
                                                      "runner_pid": None})
        open(os.path.join(d, "runner.lock"), "w").close()
        with open(os.path.join(d, "session-1.json"), "w") as f:
            f.write("{}")
        barrier, states = threading.Barrier(8), []

        def probe():
            barrier.wait()
            states.append(ct._state(d)[0])
        ts = [threading.Thread(target=probe) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertTrue(set(states) <= {"failed", "running"}, states)
        self.assertIn("failed", states)
        res = _load(os.path.join(d, "result.json"))
        self.assertEqual(res["settings_removed"], ["session-1.json"])   # exactly one recoverer did the work
        self.assertEqual(ct._state(d)[0], "failed")
        self.assertFalse([n for n in os.listdir(d) if n.endswith(".tmp")])

    def test_failure_at_final_meta_write_stops_the_runner(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        real = ct._write_json

        def failing(path, obj):
            if path.endswith("meta.json") and obj.get("runner_pid"):
                raise OSError("disk full")
            return real(path, obj)
        with mock.patch.object(ct, "_write_json", failing):
            r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertTrue(r.get("isError"))
        self.assertIn("stopped", r["content"][0]["text"])
        self.assertEqual([n for n in os.listdir(ct.tasks_dir()) if ct.TASK_ID_RE.fullmatch(n)], [])
        if os.path.exists(os.path.join(self.work, "fake-argv.json")):
            pid = _load(os.path.join(self.work, "fake-argv.json"))["pid"]
            end = time.time() + 5
            while _pid_alive(pid) and time.time() < end:
                time.sleep(0.1)
            self.assertFalse(_pid_alive(pid), "the child kept running after the aborted start")

    def test_short_tokens_refused(self):
        for env_tok, file_tok in (("short", "tok-test-000000001"), ("tok-test-000000001", "abc")):
            with open(self.cfg["token_file"], "w") as f:
                f.write(file_tok)
            self.cfg["token"] = env_tok
            r, _ = self.call("list_tasks")
            self.assertTrue(r.get("isError"), (env_tok, file_tok))
            self.assertIn("shorter than 8", r["content"][0]["text"])

    def test_tokens_with_quotes_and_at_cuts_are_redacted(self):
        env_tok = 'env"tok\\en-0001'                 # a quote and a backslash: json.dumps would escape both
        file_tok = "file-token-000000000002"
        with open(self.cfg["token_file"], "w") as f:
            f.write(file_tok)
        self.cfg["token"] = env_tok
        os.environ["FAKE_CLAUDE_MODE"] = "echo-env"
        # the file token straddles the result cut; the env token sits where the stderr tail read begins
        os.environ["FAKE_RESULT"] = "x" * (ct.RESULT_CHARS - 5) + file_tok + " " + env_tok
        # the stderr tail's read starts 10 characters before the end of the file token (mid-token)
        os.environ["FAKE_STDERR"] = env_tok + "\n" + file_tok + "z" * (ct.LOG_TAIL_CHARS - 10)
        tid = self.start()
        r, w = self.call("wait_task", task_id=tid, timeout_s=30)
        text = r["content"][0]["text"]
        for tok in (env_tok, file_tok):
            for frag in (tok, tok[:6], tok[-6:], json.dumps(tok)[1:-1]):
                self.assertNotIn(frag, text, frag)
        self.assertIn("***", w["result"])               # redacted first, then cut (not cut mid-token)
        self.assertEqual(len(w["result"]), ct.RESULT_CHARS)
        self.assertTrue(w["stderr_tail"].startswith("***"), w["stderr_tail"][:20])

    # --- review round 3 ---------------------------------------------------------------------------------------------

    def test_runner_error_after_spawn_stops_child_and_grandchild(self):
        tid, d = self._manual_task("stubborn")
        gpath = os.path.join(self.work, "grandchild.pid")

        def fail_after_child_is_up(d_, pgid):
            self.assertTrue(_wait_file(gpath))
            time.sleep(0.3)                      # the fake now ignores SIGTERM and its grandchild exists
            raise OSError("cannot write child.pid")
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct, "_note_child", fail_after_child_is_up):
            ct._run(d)
        with open(gpath) as f:
            gpid = int(f.read())
        cpid = _load(os.path.join(self.work, "fake-argv.json"))["pid"]
        end = time.time() + 5
        while (_pid_alive(gpid) or _pid_alive(cpid)) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_pid_alive(cpid), "claude survived a runner error")
        self.assertFalse(_pid_alive(gpid), "the grandchild survived a runner error")
        res = _load(os.path.join(d, "result.json"))
        self.assertIn("runner error after the child started", res["error"])
        self.assertEqual(ct.classify(res), "failed")

    def test_malformed_output_is_failed(self):
        for mode in ("badjson", "empty", "scalar", "wrongshape"):
            os.environ["FAKE_CLAUDE_MODE"] = mode
            tid = self.start()
            _, w = self.call("wait_task", task_id=tid, timeout_s=30)
            self.assertEqual(w["exit_code"], 0, mode)
            self.assertEqual(w["state"], "failed", (mode, w))
            self.assertIn("output_error", w, mode)              # the diagnostic is reported for every malformed shape
        for res in ({"exit_code": 0}, {"exit_code": 0, "output": {}}, {"exit_code": 0, "output_error": "x"},
                    {"exit_code": 0, "output": {"type": "result", "result": "r"}},          # is_error missing
                    {"exit_code": 0, "output": {"type": "result", "is_error": False}}):    # result missing
            self.assertEqual(ct.classify(res), "failed", res)
        self.assertEqual(ct.classify({"exit_code": 0, "output": {"type": "result", "is_error": False, "result": ""}}),
                         "succeeded")

    def test_preview_redacted_before_slicing(self):
        file_tok = "tok-test-000000001"
        tid = self.start(task="a" * 195 + file_tok + " tail")    # the token straddles the 200-char preview cut
        self.call("wait_task", task_id=tid, timeout_s=30)
        r, lst = self.call("list_tasks")
        preview = lst["tasks"][0]["task_preview"]
        self.assertNotIn(file_tok[:5], preview)
        self.assertIn("***", preview)
        self.assertNotIn(file_tok[:5], _load(os.path.join(ct.task_path(tid), "meta.json"))["task_preview"])

    def _launch_token_case(self, after_launch):
        file_tok = "tok-test-000000001"                  # the launch token (the child sees it as ANTHROPIC_AUTH_TOKEN)
        os.environ["FAKE_CLAUDE_MODE"] = "echo-auth"
        tid = self.start()
        after_launch()
        r, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertFalse(r.get("isError"), r)
        text = r["content"][0]["text"]
        self.assertNotIn(file_tok, text)
        self.assertIn("result has ***", w["result"])
        d = ct.task_path(tid)
        for name in ("stdout.log", "stderr.log", "result.json"):
            with open(os.path.join(d, name)) as f:
                self.assertNotIn(file_tok, f.read(), name)

    def test_token_file_replaced_after_launch(self):
        def replace():
            with open(self.cfg["token_file"], "w") as f:
                f.write("a-brand-new-token-0002")
        self._launch_token_case(replace)

    def test_token_file_removed_after_launch(self):
        self._launch_token_case(lambda: os.unlink(self.cfg["token_file"]))

    def test_redact_file_streams_across_chunks(self):
        path = os.path.join(self.tmp, "big.log")
        tok = "tok-test-000000001"
        with open(path, "w") as f:
            f.write("x" * 10 + tok + "y" * 7 + tok)
        ct.redact_file(path, ct.Redactor([tok]), chunk=13)   # chunk boundaries fall inside both tokens
        with open(path) as f:
            self.assertEqual(f.read(), "x" * 10 + "***" + "y" * 7 + "***")

    def test_child_directive_wording(self):
        self.assertIn("may use your own subagents", ct.CHILD_DIRECTIVE)
        self.assertIn("do not call start_task", ct.CHILD_DIRECTIVE)
        self.assertNotIn("do not delegate it again", ct.CHILD_DIRECTIVE)

    def test_child_directive_in_argv(self):
        for orch in ("cortados", "opus"):
            tid = self.start(orchestrator=orch)
            self.call("wait_task", task_id=tid, timeout_s=30)
            a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
            self.assertEqual(a.count("--append-system-prompt"), 1, orch)
            prompt = a[a.index("--append-system-prompt") + 1]
            self.assertTrue(prompt.endswith(ct.CHILD_DIRECTIVE), orch)       # last, so it wins
            if orch == "opus":
                self.assertGreater(len(prompt), len(ct.CHILD_DIRECTIVE))      # the launcher's directive comes first

    # --- review round 4 ---------------------------------------------------------------------------------------------

    def test_group_survivor_reaped_after_crash(self):
        os.environ["FAKE_CLAUDE_MODE"] = "crash-orphan"
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["exit_code"], 3)
        with open(os.path.join(self.work, "grandchild.pid")) as f:
            gpid = int(f.read())
        end = time.time() + 5
        while _pid_alive(gpid) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_pid_alive(gpid), "a tool process left in claude's group survived claude's exit")

    def test_overlapping_credentials_across_chunk_boundaries(self):
        short, long_ = "abcdefgh", "abcdefgh-LONGER-1234"
        red = ct.Redactor([short, long_])
        self.assertEqual(red.text("x" + long_ + "y" + short), "x***y***")
        body = "x" * 11 + long_ + "z" * 5 + short + "q"
        path = os.path.join(self.tmp, "multi.log")
        for chunk in range(1, len(body) + 2):
            with open(path, "w") as f:
                f.write(body)
            ct.redact_file(path, red, chunk=chunk)
            with open(path) as f:
                got = f.read()
            self.assertEqual(got, "x" * 11 + "***" + "z" * 5 + "***" + "q", chunk)

    def test_json_escaped_token_scrubbed_from_stored_logs(self):
        tok = 'tok"quo\\te-0001'                       # a quote and a backslash
        with open(self.cfg["token_file"], "w") as f:
            f.write(tok)
        self.cfg["token"] = "env-token-unrelated-01"
        os.environ["FAKE_CLAUDE_MODE"] = "echo-auth"
        tid = self.start()
        with open(self.cfg["token_file"], "w") as f:            # rotated right after launch
            f.write("a-rotated-token-0003")
        r, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        d = ct.task_path(tid)
        for name in ("stdout.log", "stderr.log", "result.json"):
            with open(os.path.join(d, name)) as f:
                data = f.read()
            self.assertNotIn(tok, data, name)
            self.assertNotIn(json.dumps(tok)[1:-1], data, name)
        self.assertNotIn(json.dumps(tok)[1:-1], r["content"][0]["text"])

    def test_redaction_failure_fails_the_task_and_withholds_logs(self):
        for mode in ("ok", "fail"):
            tid, d = self._manual_task(mode)
            old = signal.getsignal(signal.SIGTERM)
            self.addCleanup(signal.signal, signal.SIGTERM, old)
            with mock.patch.object(ct, "redact_file", side_effect=OSError("disk gone")):
                ct._run(d)
            res = _load(os.path.join(d, "result.json"))
            self.assertIn("redact_error", res)
            self.assertEqual(ct.classify(res), "failed", mode)
            _, st = self.call("task_status", task_id=tid)
            self.assertEqual(st["state"], "failed")
            self.assertIn("redact_error", st)
            self.assertEqual(st["stderr_tail"], ct.REDACT_FAILED_PLACEHOLDER)

    # --- review round 5 ---------------------------------------------------------------------------------------------

    def _kill_runner(self, tid):
        pid = _load(os.path.join(ct.task_path(tid), "meta.json"))["runner_pid"]
        os.kill(pid, signal.SIGKILL)
        ct._REAP[tid].wait()

    def test_crash_then_token_rotation_withholds_logs(self):
        os.environ["FAKE_CLAUDE_MODE"] = "echo-sleep"
        tid = self.start()
        self.assertTrue(_wait_file(os.path.join(self.work, "echoed")))
        self._kill_runner(tid)
        with open(self.cfg["token_file"], "w") as f:              # rotated after the crash
            f.write("a-rotated-token-0004")
        self.cfg["token"] = "a-rotated-token-0004"               # neither current credential is the launch one
        r, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["stderr_tail"], ct.LOGS_WITHHELD_NOTE)
        self.assertIn("logs_withheld", st)
        self.assertNotIn("tok-test-000000001", r["content"][0]["text"])
        self.assertNotIn("tok-test-000000001", json.dumps(st))
        self.assertNotIn("stdout_log", st)                         # no pointer to the raw, credential-bearing logs
        self.assertNotIn("stderr_log", st)
        self.assertNotIn("dir", st)                                # nor the dir that holds them
        self.assertIn("must not be read", st["logs_withheld"])

    def test_crash_with_same_credentials_releases_redacted_logs(self):
        os.environ["FAKE_CLAUDE_MODE"] = "echo-sleep"
        tid = self.start()
        self.assertTrue(_wait_file(os.path.join(self.work, "echoed")))
        self._kill_runner(tid)
        r, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        self.assertNotIn("logs_withheld", st)
        self.assertIn("stderr has ***", st["stderr_tail"])
        with open(os.path.join(ct.task_path(tid), "stderr.log")) as f:
            self.assertNotIn("tok-test-000000001", f.read())        # redacted on disk too

    def test_runner_death_stops_child_group(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stubborn"
        tid = self.start()
        gpath = os.path.join(self.work, "grandchild.pid")
        self.assertTrue(_wait_file(gpath))
        time.sleep(0.3)
        with open(gpath) as f:
            gpid = int(f.read())
        cpid = _load(os.path.join(self.work, "fake-argv.json"))["pid"]
        self._kill_runner(tid)
        self.assertTrue(_pid_alive(cpid))                          # orphaned until recovery
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        end = time.time() + 5
        while (_pid_alive(cpid) or _pid_alive(gpid)) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(_pid_alive(cpid), "claude survived its runner's death")
        self.assertFalse(_pid_alive(gpid), "the grandchild survived its runner's death")
        self.assertEqual(_load(os.path.join(ct.task_path(tid), "result.json"))["child_cleanup"], "child group stopped")

    def test_recovery_never_signals_a_reused_pid(self):
        import subprocess
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        root = ct._private_root()
        tid = "t20261102000000-00000ddd"
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time() - 3600,
                                                      "runner_pid": None})
        open(os.path.join(d, "runner.lock"), "w").close()
        # the recorded child is "this pgid", but with another start time: the pid now names an unrelated process
        ct._write_json(os.path.join(d, "child.json"), {"pgid": other.pid, "start": "Thu Jan  1 00:00:00 1970"})
        _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        time.sleep(0.3)
        self.assertIsNone(other.poll(), "an unrelated process group was signalled")
        self.assertIn("not signalled", _load(os.path.join(d, "result.json"))["child_cleanup"])

    def test_chrome_flag_enforced_both_ways(self):
        tid = self.start()
        self.call("wait_task", task_id=tid, timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertIn("--no-chrome", a)
        self.assertNotIn("--chrome", a)
        tid = self.start(chrome=True)
        self.call("wait_task", task_id=tid, timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertIn("--chrome", a)
        self.assertNotIn("--no-chrome", a)

    def test_installed_claude_accepts_chrome_flags(self):
        import shutil
        import subprocess
        exe = shutil.which("claude")
        if not exe:
            self.skipTest("no claude on PATH")
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=30).stdout
        self.assertIn("--no-chrome", out)
        self.assertIn("--chrome", out)

    # --- review round 6 ---------------------------------------------------------------------------------------------

    def _dead_task_with_child(self, child_rec):
        """A crashed task whose recorded child group is a live, unrelated session leader started here."""
        import subprocess
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        root = ct._private_root()
        tid = ct._new_id()
        d = os.path.join(root, tid)
        os.mkdir(d, 0o700)
        ct._write_json(os.path.join(d, "meta.json"), {"task_id": tid, "started_at": time.time() - 3600,
                                                      "runner_pid": None})
        open(os.path.join(d, "runner.lock"), "w").close()
        rec = dict(child_rec, pgid=other.pid)
        if rec.get("start") == "LIVE":
            rec["start"] = ct._proc_start(other.pid)
        ct._write_json(os.path.join(d, "child.json"), rec)
        return tid, d, other

    def _only_probes(self, calls):
        self.assertTrue(all(sig == 0 for _, sig in calls), calls)

    def test_unknown_start_time_is_never_signalled(self):
        tid, d, other = self._dead_task_with_child({"start": "LIVE"})
        calls = []
        real = os.killpg

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real(pgid, sig)
        with mock.patch.object(ct, "_proc_start", return_value=ct.UNKNOWN), mock.patch.object(ct.os, "killpg", spy):
            _, st = self.call("task_status", task_id=tid)
        self.assertEqual(st["state"], "failed")
        self._only_probes(calls)
        self.assertIsNone(other.poll())
        self.assertIn("start time unknown", _load(os.path.join(d, "result.json"))["child_cleanup"])

    def test_unrecorded_start_with_live_leader_is_not_signalled(self):
        tid, d, other = self._dead_task_with_child({"start": None})   # died between the two child.json writes
        self.call("task_status", task_id=tid)
        time.sleep(0.2)
        self.assertIsNone(other.poll())
        note = _load(os.path.join(d, "result.json"))["child_cleanup"]
        self.assertIn("not signalled", note)
        self.assertIn("may still be running", note)

    def test_eperm_group_is_foreign_not_gone(self):
        tid, d, other = self._dead_task_with_child({"start": "LIVE"})
        calls = []

        def eperm(pgid, sig):
            calls.append((pgid, sig))
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(ct.os, "killpg", eperm):
            self.call("task_status", task_id=tid)
        self._only_probes(calls)
        self.assertEqual(_load(os.path.join(d, "result.json"))["child_cleanup"],
                         "pgid reused by another user's process; not signalled")

    def test_ps_by_absolute_path(self):
        self.assertTrue(os.path.isabs(ct.PS))
        self.assertIsInstance(ct._proc_start(os.getpid()), str)
        with mock.patch.object(ct, "PS", "/nonexistent/ps"):
            self.assertIs(ct._proc_start(os.getpid()), ct.UNKNOWN)   # not "no such process"

    def test_recovery_grace_is_short(self):
        self.assertLessEqual(ct.RECOVERY_GRACE_S, 5)
        os.environ["FAKE_CLAUDE_MODE"] = "stubborn"
        with mock.patch.object(ct, "KILL_GRACE_S", 30):
            tid = self.start()
            self.assertTrue(_wait_file(os.path.join(self.work, "grandchild.pid")))
            time.sleep(0.3)
            self._kill_runner(tid)
            t0 = time.time()
            self.call("task_status", task_id=tid)
            self.assertLess(time.time() - t0, 15, "recovery waited the full 30 s kill grace")

    def test_note_child_writes_pgid_before_start(self):
        tid, d = self._manual_task("ok")
        writes = []
        real = ct._write_json

        def spy(path, obj):
            if path.endswith("child.json"):
                writes.append(dict(obj))
            return real(path, obj)
        with mock.patch.object(ct, "_write_json", spy):
            ct._note_child(d, os.getpid())
        self.assertEqual(writes[0], {"pgid": os.getpid(), "start": None})
        self.assertIsInstance(writes[-1]["start"], str)

    # --- round-7 P3s ------------------------------------------------------------------------------------------------

    def test_failed_ps_is_unknown_and_never_signalled(self):
        import subprocess
        fake = subprocess.CompletedProcess(["ps"], 1, stdout="", stderr="ps: boom")
        with mock.patch.object(ct.subprocess, "run", return_value=fake):
            self.assertIs(ct._proc_start(os.getpid()), ct.UNKNOWN)
        for rc, err in ((-9, ""), (2, "")):
            with mock.patch.object(ct.subprocess, "run",
                                   return_value=subprocess.CompletedProcess(["ps"], rc, stdout="", stderr=err)):
                self.assertIs(ct._proc_start(os.getpid()), ct.UNKNOWN, rc)
        # a real exited pid: exit 1, nothing printed -> "no such process"
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertIsNone(ct._proc_start(p.pid))
        tid, d, other = self._dead_task_with_child({"start": "LIVE"})
        calls = []
        real = os.killpg

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real(pgid, sig)
        with mock.patch.object(ct.subprocess, "run", return_value=fake), mock.patch.object(ct.os, "killpg", spy):
            self.call("task_status", task_id=tid)
        self._only_probes(calls)
        self.assertIsNone(other.poll())
        self.assertIn("start time unknown", _load(os.path.join(d, "result.json"))["child_cleanup"])

    def test_child_record_written_from_preexec(self):
        tid, d = self._manual_task("ok")
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct, "_note_child", lambda *a: None):     # only the forked child writes the record
            ct._run(d)
        rec = _load(os.path.join(d, "child.json"))
        self.assertEqual(rec["pgid"], _load(os.path.join(self.work, "fake-argv.json"))["pid"])
        self.assertFalse([n for n in os.listdir(d) if n.endswith(".tmp")])

    def test_subprocess_error_at_spawn_is_a_recorded_failure(self):
        import subprocess
        tid, d = self._manual_task("ok")
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct.subprocess, "Popen", side_effect=subprocess.SubprocessError("preexec failed")):
            ct._run(d)
        res = _load(os.path.join(d, "result.json"))
        self.assertIn("could not start the child: SubprocessError", res["error"])
        self.assertEqual(ct.classify(res), "failed")

    def test_prune_failure_in_one_dir_does_not_block_start(self):
        root = ct._private_root()
        bad, good = "t20200101000000-000000c1", "t20200101000000-000000c2"
        for tid in (bad, good):
            os.mkdir(os.path.join(root, tid), 0o700)
            ct._write_json(os.path.join(root, tid, "meta.json"), {"started_at": 1.0, "runner_pid": None})
            ct._write_json(os.path.join(root, tid, "result.json"), {"exit_code": 0, "ended_at": 2.0})
        real = ct.shutil.rmtree

        def rmtree(path, *a, **kw):
            if os.path.basename(path) == bad:
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **kw)
        with mock.patch.object(ct.shutil, "rmtree", rmtree):
            r, out = self.call("start_task", task="x", cwd=self.work)
        self.assertFalse(r.get("isError"), r)
        self.assertTrue(any(e.startswith(bad) for e in out["prune_errors"]), out)
        names = os.listdir(root)
        self.assertIn(bad, names)
        self.assertNotIn(good, names)
        self.call("wait_task", task_id=out["task_id"], timeout_s=30)

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
        for bad in ("../../etc", "t1", "", None, "t20261102000000-zzzzzzzz", "t20261102000000-0000000a\n"):
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
