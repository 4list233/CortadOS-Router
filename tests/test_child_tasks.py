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
                   "pythonpath": os.environ.get("PYTHONPATH"), "pid": os.getpid(),
                   "settings": (lambda p: p and {{"mode": os.stat(p).st_mode & 0o777, "body": json.load(open(p))}})(
                       sys.argv[sys.argv.index("--settings") + 1] if "--settings" in sys.argv else None),
                   "env": {{k: os.environ.get(k) for k in ("CORTADOS_PROVIDER_TOKEN", "CORTADOS_PROVIDER_URL",
                           "CORTADOS_PROVIDER_TOKEN_FILE", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_MODEL",
                           "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL",
                           "ANTHROPIC_AUTH_TOKEN", "HOME", "PATH")}}}}, f)
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
    if mode == "ok-result":
        # a successful stream-json run whose final result text is FAKE_RESULT (FAKE_RESULT_TOKEN appends the token)
        text = os.environ.get("FAKE_RESULT", "")
        if os.environ.get("FAKE_RESULT_TOKEN"):
            text += os.environ["ANTHROPIC_AUTH_TOKEN"]
        print(json.dumps({{"type": "system", "subtype": "init", "permissionMode": "acceptEdits"}}))
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text}}))
        sys.exit(0)
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
    if mode == "too-large":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True, "api_error_status": 413,
                          "result": "Request too large (max 32MB). Accumulated images..."}}))
        sys.exit(1)
    if mode == "stream-deep":
        # 1500-deep JSON: inside a tool result string (the decode in deep overflows) and as a raw line of its own
        # (json.loads overflows), each with the token in it
        tok = os.environ["ANTHROPIC_AUTH_TOKEN"]
        nested = "[" * 1500 + json.dumps(tok) + "]" * 1500
        print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1",
            "content": nested}}]}}}}))
        print(nested)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}}))
        sys.exit(0)
    if mode == "stream-deep-escaped":
        # the token escaped six times, inside 1500-deep JSON, both in a tool-result string and as a raw line
        esc = os.environ["ANTHROPIC_AUTH_TOKEN"]
        for _ in range(6):
            esc = json.dumps(esc)
        nested = "[" * 1500 + esc + "]" * 1500
        print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1",
            "content": nested}}]}}}}))
        print(nested)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}}))
        sys.exit(0)
    if mode == "legacy-pretty":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "pretty"}}, indent=2))
        sys.exit(0)
    if mode == "stream-u2028":
        print(json.dumps({{"type": "system", "subtype": "init", "permissionMode": "auto"}}))
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                          "result": "a\u2028b\u2029c\x85d"}}, ensure_ascii=False))
        sys.exit(0)
    if mode == "stream-manual":
        print(json.dumps({{"type": "system", "subtype": "init", "permissionMode": "default"}}))   # 2.1.316 for manual
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "ok"}}))
        sys.exit(0)
    if mode == "stream-json-in-tool":
        # a tool result whose text is JSON (one escaping layer more than the event), and JSON inside that JSON
        tok = os.environ["ANTHROPIC_AUTH_TOKEN"]
        inner = json.dumps({{"config": {{"token": tok}}}})
        print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "toolu_1",
            "name": "Bash", "input": {{"command": "cat cfg.json"}}}}]}}}}))
        print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "toolu_1",
            "content": [{{"type": "text", "text": inner}}, {{"type": "text", "text": json.dumps({{"wrapped": inner}})}}]}}]}}}}))
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "read " + inner}}))
        print("a non-JSON line with " + tok)
        sys.exit(0)
    if mode in ("stream-denied", "stream-outside"):
        # claude -p --output-format stream-json --verbose: one event per line, the result record last
        def ev(e):
            print(json.dumps(e)); sys.stdout.flush()
        ev({{"type": "system", "subtype": "init", "permissionMode": "auto", "cwd": os.getcwd()}})
        if mode == "stream-denied":
            ev({{"type": "system", "subtype": "status", "permissionMode": "default"}})   # Claude Code left auto
            ev({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "toolu_1", "name": "Write",
                "input": {{"file_path": os.path.join(os.getcwd(), "a.txt"), "content": "x"}}}}]}}}})
            ev({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "toolu_1",
                "is_error": True, "content": "Permission for this tool use was denied."}}]}}}})
            ev({{"type": "result", "subtype": "success", "is_error": False, "result": "I could not do it",
                "num_turns": 2, "permission_denials": [{{"tool_name": "Write", "tool_use_id": "toolu_1",
                                                         "tool_input": {{"file_path": "a.txt"}}}}]}})
        else:
            # confirmed writes (outside and inside cwd), a failed one and one that never got a result
            ev({{"type": "assistant", "message": {{"content": [
                {{"type": "tool_use", "id": "toolu_1", "name": "Write",
                  "input": {{"file_path": os.environ["FAKE_OUTSIDE"], "content": "child-ok"}}}},
                {{"type": "tool_use", "id": "toolu_2", "name": "Edit", "input": {{"file_path": "inside.txt"}}}},
                {{"type": "tool_use", "id": "toolu_3", "name": "Bash", "input": {{"command": "true"}}}},
                {{"type": "tool_use", "id": "toolu_4", "name": "Write", "input": {{"file_path": "failed.txt"}}}},
                {{"type": "tool_use", "id": "toolu_5", "name": "Write", "input": {{"file_path": "/tmp/unresolved.txt"}}}}]}}}})
            ev({{"type": "user", "message": {{"content": [
                {{"type": "tool_result", "tool_use_id": "toolu_1", "content": "File created successfully"}},
                {{"type": "tool_result", "tool_use_id": "toolu_2", "content": "edited"}},
                {{"type": "tool_result", "tool_use_id": "toolu_3", "content": ""}},
                {{"type": "tool_result", "tool_use_id": "toolu_4", "is_error": True, "content": "denied"}}]}}}})
            ev({{"type": "result", "subtype": "success", "is_error": False, "result": "done", "num_turns": 2,
                "permission_denials": []}})
        sys.exit(0)
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
        self.user_settings = os.path.join(self.tmp, "user-settings.json")
        us = mock.patch.object(ct, "USER_CLAUDE_SETTINGS", self.user_settings)
        us.start()
        self.addCleanup(us.stop)
        # the fake binary's control vars must survive the chrome child's env allowlist (tests only)
        ap = mock.patch.object(ct, "LOGIN_ENV_ALLOW_PREFIXES", ct.LOGIN_ENV_ALLOW_PREFIXES + ("FAKE_",))
        ap.start()
        self.addCleanup(ap.stop)
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
        self.assertNotIn("--mcp-config", a)                      # no connectors selected: no flag, a note in the log
        with open(os.path.join(w["dir"], "runner.log")) as f:
            self.assertIn("no connectors selected", f.read())
        self.assertTrue(w["settings_removed"])                   # the token-bearing settings file is gone
        self.assertFalse([n for n in os.listdir(w["dir"]) if n.startswith("session-")])
        self.assertIn("-p", a)
        self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", a)                 # -p only allows stream-json with --verbose
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
        _, only = self.call("list_tasks", ids=[tid, "t00000000000000-00000000"])   # bot gateway: owned ids only
        self.assertEqual([t["task_id"] for t in only["tasks"]], [tid])
        _, none = self.call("list_tasks", ids=[])
        self.assertEqual(none["tasks"], [])

    # ---- permission modes the child's model really supports (2026-11-02 E2E: haiku + auto denied every write)
    def test_auto_mode_support_follows_the_clis_rule(self):
        for model in ("cortados", "sub/claude-sonnet-5", "sub/claude-opus-5-5", "sub/claude-haiku-5-5",
                      "sub/gpt-6.1-sol", "sub/qwen-27b"):
            self.assertTrue(ct.auto_mode_supported(model), model)
        for model in ("sub/claude-haiku-4-5-20251001", "claude-haiku-4-5", "sub/CLAUDE-HAIKU-4-5"):
            self.assertFalse(ct.auto_mode_supported(model), model)

    def test_a_defaulted_mode_on_a_model_without_auto_is_accept_edits_and_says_why(self):
        r, out = self.call("start_task", task="hi", cwd=self.work, orchestrator="claude-haiku-4-5-20251001")
        self.assertFalse(r.get("isError"), r)
        self.assertEqual(out["permission_mode"], "acceptEdits")
        self.assertIn("has no auto mode", out["permission_mode_note"])
        _, w = self.call("wait_task", task_id=out["task_id"], timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(w["permission_mode"], "acceptEdits")
        self.assertIn("has no auto mode", w["permission_mode_note"])

    def test_explicit_auto_on_a_model_without_auto_is_refused(self):
        r, _ = self.call("start_task", task="hi", cwd=self.work, orchestrator="claude-haiku-4-5-20251001",
                         permission_mode="auto")
        self.assertTrue(r.get("isError"))
        self.assertIn("'auto' is not available", r["content"][0]["text"])
        self.assertIn("acceptEdits", r["content"][0]["text"])
        self.assertFalse(os.path.exists(os.path.join(self.work, "fake-argv.json")))     # nothing started
        tid = self.start(orchestrator="claude-haiku-4-5-20251001", permission_mode="acceptEdits")   # an explicit
        self.call("wait_task", task_id=tid, timeout_s=30)                                         # other mode runs

    def test_a_defaulted_mode_on_a_model_with_auto_is_auto(self):
        r, out = self.call("start_task", task="hi", cwd=self.work, orchestrator="sonnet")
        self.assertEqual(out["permission_mode"], "auto")
        self.assertNotIn("permission_mode_note", out)
        self.call("wait_task", task_id=out["task_id"], timeout_s=30)

    # ---- what the child really did, from its stream-json events
    def test_denials_and_a_left_mode_are_warnings_on_a_succeeded_child(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-denied"
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertEqual(w["state"], "succeeded")                 # the CLI says success ...
        self.assertEqual(w["permission_denials"], ["Write"])      # ... but it did not do the work
        self.assertEqual(w["tool_uses"], {"Write": 1})
        self.assertEqual(w["tool_errors"], 1)
        self.assertEqual(w["effective_permission_mode"], "default")
        self.assertNotIn("files_written", w)                      # the denied Write is not reported as written
        self.assertEqual(w["write_attempts_unconfirmed"], [{"path": os.path.join(self.work, "a.txt"),
                                                            "outcome": "failed"}])
        joined = " ".join(w["warnings"])
        self.assertIn("1 tool call(s) were denied (Write)", joined)
        self.assertIn("ran in permission mode 'default', not the requested 'auto'", joined)

    def test_writes_outside_cwd_are_reported(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-outside"
        outside = os.path.join(self.tmp, "cortados-bridge-x1", "work", "c.txt")
        os.environ["FAKE_OUTSIDE"] = outside
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertEqual(w["state"], "succeeded")
        self.assertEqual(w["tool_uses"], {"Write": 3, "Edit": 1, "Bash": 1})
        # only writes whose tool_result came back without an error count as written
        self.assertEqual(w["files_written"], [outside, os.path.join(self.work, "inside.txt")])
        self.assertEqual(w["writes_outside_cwd"], [outside])
        self.assertEqual(w["write_attempts_unconfirmed"], [
            {"path": os.path.join(self.work, "failed.txt"), "outcome": "failed"},
            {"path": os.path.realpath("/tmp/unresolved.txt"), "outcome": "unresolved"}])
        joined = " ".join(w["warnings"])
        self.assertIn("wrote 1 file(s) outside its cwd", joined)
        self.assertIn("2 file write(s) were attempted but not confirmed (1 failed, 1 without a result)", joined)
        self.assertNotIn("permission_denials", w)

    def test_a_stream_without_a_result_record_is_an_output_error(self):
        out, err = ct.parse_output('{"type": "system", "subtype": "init"}\n{"type": "assistant", "message": {}}')
        self.assertIsNone(out)
        self.assertIn("no result record", err)
        out, err = ct.parse_output('{"type": "system"}\n{"type": "result", "is_error": false, "result": "ok"}\n')
        self.assertEqual((out["result"], err), ("ok", None))

    def test_pinned_orchestrator_and_allowlist(self):
        tid = self.start(orchestrator="opus", permission_mode="plan", allowed_tools=["Bash(git diff:*)", "Read"])
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertTrue(a[a.index("--model") + 1].startswith("sub/"))
        self.assertEqual(a[a.index("--permission-mode") + 1], "plan")
        self.assertEqual(a[a.index("--allowedTools") + 1], "Bash(git diff:*),Read")

    def test_connectors_are_opt_in_and_filtered(self):
        conn = os.path.join(self.tmp, "connectors.mcp.json")
        servers = {"vercel": {"type": "http", "url": "https://vercel.example/mcp"},
                   "notion": {"type": "http", "url": "https://notion.example/mcp"},
                   "slack": {"type": "http", "url": "https://slack.example/mcp"}}
        with open(conn, "w") as f:
            json.dump({"mcpServers": servers}, f)
        os.environ["CORTADOS_CONNECTORS_MCP_CONFIG"] = conn
        tid = self.start(permission_mode="dontAsk")                 # default: none, even though the file exists
        self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertNotIn("--mcp-config", _load(os.path.join(self.work, "fake-argv.json"))["argv"])
        tid = self.start(connectors=["notion"])
        d = ct.task_path(tid)
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        path = a[a.index("--mcp-config") + 1]
        self.assertEqual(os.path.dirname(path), d)                 # a private per-task file, not the full config
        self.assertFalse(os.path.exists(path), "the selection (it may carry credentials) is removed at exit")
        self.assertEqual(_load(os.path.join(d, "meta.json"))["connectors"], ["notion"])
        r, _ = self.call("start_task", task="x", cwd=self.work, connectors=["nope"])
        self.assertIn("unknown connector(s) nope; available: notion, slack, vercel", r["content"][0]["text"])

    def test_selected_connectors_file_content(self):
        conn = os.path.join(self.tmp, "connectors.mcp.json")
        with open(conn, "w") as f:
            json.dump({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}, f)
        os.environ["CORTADOS_CONNECTORS_MCP_CONFIG"] = conn
        self.assertIsNone(ct.select_connectors(None))
        self.assertIsNone(ct.select_connectors([]))
        self.assertEqual(ct.select_connectors(["b"]), {"b": {"command": "y"}})
        for bad in ("a", [1], ["a"] * 40):
            with self.assertRaises(ct.TaskError):
                ct.select_connectors(bad)

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

    def test_client_attribution_on_both_lifecycle_events(self):
        # Review r2 P3-8 (bot gateway): the finished event is written from cmd.json, so it must carry the client too.
        self.cfg = {**self.cfg, "client": "grokbot"}
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        ev = [e for e in _events() if e.get("task_id") == tid]
        self.assertEqual([(e["event"], e.get("client")) for e in ev], [("started", "grokbot"), ("finished", "grokbot")])

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

    def _result_of(self, text, with_token=False):
        os.environ["FAKE_CLAUDE_MODE"] = "ok-result"
        os.environ["FAKE_RESULT"] = text
        if with_token:
            os.environ["FAKE_RESULT_TOKEN"] = "1"
        try:
            tid = self.start()
            _, w = self.call("wait_task", task_id=tid, timeout_s=30)
            _, st = self.call("task_status", task_id=tid)
        finally:
            for k in ("FAKE_RESULT", "FAKE_RESULT_TOKEN"):
                os.environ.pop(k, None)
        self.assertEqual(w["state"], "succeeded", w)
        return w, st

    def test_free_form_results_that_look_like_json_come_back_intact(self):
        for text in ("[report](report.md) is written", "[INFO] all done", "{summary} three files changed",
                     '"Done" \u2014 nothing else to do', '  [1, 2, oops', '{"a": 1}'):
            w, st = self._result_of(text)
            self.assertEqual(w["result"], text)
            self.assertEqual(st["result"], text)
            self.assertNotIn("redact_unparsed_lines", w)

    def test_a_result_holding_the_token_comes_back_masked(self):
        w, st = self._result_of('[INFO] key is "', with_token=True)
        for r in (w, st):
            self.assertEqual(r["result"], '[INFO] key is "***')
            self.assertNotIn(self.cfg["token"], json.dumps(r))
        with open(os.path.join(ct.task_path(w["task_id"]), "result.json")) as f:
            self.assertNotIn(self.cfg["token"], f.read())
        escaped = self.cfg["token"]                               # escaped several times, still masked as text
        for _ in range(ct.redact.ESCAPE_DEPTH):
            escaped = json.dumps(escaped)[1:-1]
        self.assertEqual(ct._text_result(ct.Redactor([self.cfg["token"]]), "x " + escaped), ("x ***", 0))

    def test_an_oversized_json_result_is_truncated_not_withheld(self):
        text = json.dumps([{"i": i, "note": "padding " * 3} for i in range(1200)])
        self.assertGreater(len(text), ct.RESULT_CHARS)
        w, st = self._result_of(text)
        for r in (w, st):
            self.assertEqual(r["result"], text[:ct.RESULT_CHARS])
            self.assertIn("result_truncated", r)
            self.assertNotIn("redact_unparsed_lines", r)

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
            # The child directive only: never the whole-session ORCHESTRATOR directive, for any orchestrator.
            self.assertEqual(prompt, ct.CHILD_DIRECTIVE, orch)
            self.assertNotIn("ORCHESTRATOR", prompt)
            self.assertNotIn("orchestrator_gate", json.dumps(_load(os.path.join(self.work, "fake-argv.json"))))
            settings = a[a.index("--settings") + 1]
            self.assertTrue(os.path.basename(settings).startswith("session-"))

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

    def test_token_in_json_inside_a_tool_result_survives_no_rotation(self):
        # stream-json: a tool result holding JSON text escapes the token twice (and JSON inside that, three times); a
        # raw pass with one escaped spelling missed it. The token has a quote and a backslash, and is rotated at once.
        tok = 'tok"QXZQ\\te-0005'                       # QXZQ: uppercase, so it never occurs by chance in a random lowercase file name
        with open(self.cfg["token_file"], "w") as f:
            f.write(tok)
        self.cfg["token"] = "env-token-unrelated-02"
        os.environ["FAKE_CLAUDE_MODE"] = "stream-json-in-tool"
        tid = self.start()
        with open(self.cfg["token_file"], "w") as f:            # rotated right after launch
            f.write("a-rotated-token-0006")
        r, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        self.assertEqual(w["redact_unparsed_lines"], 1)            # the non-JSON line was withheld, and is counted
        d = ct.task_path(tid)
        for name in ("stdout.log", "stderr.log", "result.json"):
            with open(os.path.join(d, name)) as f:
                data = f.read()
            self.assertNotIn("QXZQ", data, name)                    # no spelling of the token at any escaping depth
            self.assertNotIn("te-0005", data, name)
        self.assertNotIn("QXZQ", r["content"][0]["text"])
        with open(os.path.join(d, "stdout.log")) as f:             # still one JSON event per line, and decodable
            events = [json.loads(line) for line in f if line.startswith("{")]
        text = events[1]["message"]["content"][0]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"config": {"token": "***"}})

    def test_a_1500_deep_line_neither_kills_the_runner_nor_keeps_the_token(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-deep"
        tid = self.start()
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertIn(w["state"], ("succeeded", "failed"), w)
        self.assertTrue(w.get("redact_unparsed_lines", 0) >= 1 or "redact_error" in w, w)
        self.assertEqual(w.get("redact_unparsed_lines"), 2)       # the deep string and the deep line, both withheld
        d = ct.task_path(tid)
        for name in ("stdout.log", "stderr.log", "result.json"):
            with open(os.path.join(d, name)) as f:
                data = f.read()
            self.assertNotIn(self.cfg["token"], data, name)
        _, again = self.call("task_status", task_id=tid)            # status keeps answering
        self.assertEqual(again["state"], w["state"])

    def test_deep_cleans_what_text_leaks_beyond_the_raw_escape_depth(self):
        tok = 'ab"QQZZ\\ef-123456'                          # a quote and a backslash
        red = ct.Redactor([tok])
        levels = ct.redact.ESCAPE_DEPTH + 2                 # >= 6, and deeper than the raw pass matches
        value = {"token": tok}
        for _ in range(levels):                             # JSON text inside a string, `levels` down
            value = {"wrapped": json.dumps(value)}
        line = json.dumps(value)
        self.assertIn("QQZZ", red.text(line))                # the raw pass alone leaks it at this depth
        clean = json.dumps(red.deep(json.loads(line)))
        self.assertNotIn("QQZZ", clean)
        inner = json.loads(clean)
        for _ in range(levels):
            inner = json.loads(inner["wrapped"])
        self.assertEqual(inner, {"token": "***"})            # still the same structure, decodable

    def test_malformed_json_past_the_raw_escape_depth_is_withheld(self):
        tok = 'ab"QQZZ\\ef-123456'
        red = ct.Redactor([tok])
        value = '{"token": ' + json.dumps(tok) + ', broken'    # looks like JSON, does not decode
        for _ in range(ct.redact.ESCAPE_DEPTH + 2):
            value = json.dumps({"wrapped": value})
        line = json.dumps({"outer": value})
        self.assertIn("QQZZ", red.text(line))                 # the raw pass alone leaks it at this depth
        clean, withheld = red.deep_count(json.loads(line))
        self.assertNotIn("QQZZ", json.dumps(clean))
        self.assertEqual(withheld, 1)
        inner = clean["outer"]
        for _ in range(ct.redact.ESCAPE_DEPTH + 2):
            inner = json.loads(inner)["wrapped"]
        self.assertEqual(inner, ct.redact.WITHHELD)             # withheld wholesale, not text-redacted
        self.assertEqual(red.deep_count({"x": "[INFO] " + tok}), ({"x": ct.redact.WITHHELD}, 1))

    def test_a_chain_of_quoted_strings_is_unwound(self):
        tok = 'ab"QQZZ\\ef-123456'
        red = ct.Redactor([tok])
        levels = ct.redact.ESCAPE_DEPTH + 2
        value = "token=" + tok
        for _ in range(levels):                                 # a JSON string literal inside a string, again and again
            value = json.dumps(value)
        self.assertIn("QQZZ", red.text(value))
        clean, withheld = red.deep_count({"v": value})
        self.assertEqual(withheld, 0)
        self.assertNotIn("QQZZ", json.dumps(clean))
        inner = clean["v"]
        for _ in range(levels):
            inner = json.loads(inner)
        self.assertEqual(inner, "token=***")
        bad = json.dumps(json.dumps(tok))[:-2]                   # a quoted string cut short: does not decode
        self.assertEqual(red.deep_count([bad]), ([ct.redact.WITHHELD], 1))
        deep = "[" * 1500 + json.dumps(tok) + "]" * 1500         # too deep to decode
        self.assertEqual(red.deep_count([json.dumps(deep)]), ([json.dumps(ct.redact.WITHHELD)], 1))

    def test_keys_are_redacted_structurally_and_unsafe_keys_withhold_the_object(self):
        tok = 'ab"QQZZ\\ef-123456'
        red = ct.Redactor([tok])
        key = {"k": tok}
        for _ in range(ct.redact.ESCAPE_DEPTH + 2):              # an encoded key, deeper than the raw pass matches
            key = {"w": json.dumps(key)}
        enc = json.dumps(key)
        self.assertIn("QQZZ", red.text(enc))
        clean, withheld = red.deep_count({"outer": {enc: 1, "other": 2}})
        self.assertEqual(withheld, 0)
        self.assertNotIn("QQZZ", json.dumps(clean))
        self.assertEqual(clean["outer"]["other"], 2)
        (k,) = [k for k in clean["outer"] if k != "other"]
        inner = json.loads(k)
        for _ in range(ct.redact.ESCAPE_DEPTH + 2):
            inner = json.loads(inner["w"])
        self.assertEqual(inner, {"k": "***"})
        # a key that redacts to the name of another key: the whole object goes, not one silent value
        self.assertEqual(red.deep_count({"keep": {"a": 0, tok: 1, "***": 2}}), ({"keep": ct.redact.WITHHELD}, 1))
        # a key that cannot be processed safely (malformed JSON): the whole object goes, counted once
        self.assertEqual(red.deep_count([{"{" + tok: 1, "b": "{bad"}]), ([ct.redact.WITHHELD], 1))

    def test_redact_stream_file_counts_lines_it_could_not_parse(self):
        tok = "stream-token-000001"
        path = os.path.join(self.tmp, "s.log")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "a", "t": tok}) + "\n" + "plain " + tok + "\n" + "\n"
                    + "[" * 1500 + "]" * 1500 + "\n" + json.dumps({"type": "b"}) + "\n")
        self.assertEqual(ct.redact_stream_file(path, ct.Redactor([tok])), 2)   # the plain line and the deep one
        with open(path) as f:
            data = f.read()
        self.assertNotIn(tok, data)
        lines = data.split("\n")
        self.assertEqual(json.loads(lines[0]), {"type": "a", "t": "***"})
        self.assertEqual(lines[1], ct.WITHHELD_EVENT)            # replaced wholesale, not text-redacted
        self.assertEqual(lines[3], ct.WITHHELD_EVENT)

    def _assert_no_token_anywhere(self, d, fragment, extra=()):
        for name in ("stdout.log", "stderr.log", "result.json"):
            with open(os.path.join(d, name)) as f:
                data = f.read()
            self.assertNotIn(fragment, data, name)
        for text in extra:
            self.assertNotIn(fragment, text)

    def test_deep_nesting_with_six_escaping_layers_is_withheld_in_the_runner(self):
        tok = 'tok"QQZZ\\deep-0007'                       # a quote and a backslash
        with open(self.cfg["token_file"], "w") as f:
            f.write(tok)
        self.cfg["token"] = "env-token-unrelated-03"
        os.environ["FAKE_CLAUDE_MODE"] = "stream-deep-escaped"
        tid = self.start()
        r, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertGreaterEqual(w["redact_unparsed_lines"], 1, w)
        self._assert_no_token_anywhere(ct.task_path(tid), "QQZZ", [r["content"][0]["text"]])
        with open(os.path.join(ct.task_path(tid), "stdout.log")) as f:
            self.assertIn("could not be safely redacted", f.read())

    def test_deep_nesting_with_six_escaping_layers_is_withheld_in_recovery(self):
        def lines(tok):
            esc = tok
            for _ in range(6):
                esc = json.dumps(esc)
            nested = "[" * 1500 + esc + "]" * 1500
            return [json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                                        "content": nested}]}}), nested]
        d, _, meta, res = self._recovered(lambda tok: lines('rec"QQZZ\\deep-0008'))
        tok = 'rec"QQZZ\\deep-0008'
        meta["cred_fingerprints"] = ct.credential_fingerprints([tok])
        self.assertTrue(ct._release_recovered_logs(d, meta, res, ct.Redactor([tok])))
        self.assertGreaterEqual(res["redact_unparsed_lines"], 1)
        with open(os.path.join(d, "stdout.log")) as f:
            data = f.read()
        self.assertNotIn("QQZZ", data)
        self.assertNotIn("QQZZ", json.dumps(res))

    def test_a_result_with_line_separators_is_still_the_result(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-u2028"
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        self.assertEqual(w["result"], "a\u2028b\u2029c\x85d")
        with open(os.path.join(w["dir"], "stdout.log"), encoding="utf-8") as f:
            self.assertNotIn("\u2028", f.read())                 # re-serialized ASCII-only

    def test_a_pretty_printed_legacy_record_is_still_the_result(self):
        os.environ["FAKE_CLAUDE_MODE"] = "legacy-pretty"
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertEqual(w["state"], "succeeded", w)
        self.assertEqual(w["result"], "pretty")
        self.assertEqual(ct.parse_output(json.dumps({"type": "result", "is_error": False, "result": "x"}, indent=2)),
                         ({"type": "result", "is_error": False, "result": "x"}, None))

    def test_a_manual_child_is_not_warned_about_its_reported_default_mode(self):
        os.environ["FAKE_CLAUDE_MODE"] = "stream-manual"
        _, w = self.call("wait_task", task_id=self.start(permission_mode="manual"), timeout_s=30)
        self.assertEqual(w["effective_permission_mode"], "default")
        self.assertFalse([x for x in w.get("warnings", []) if "permission mode" in x], w)   # only the no-tools one
        self.assertEqual(len(w["warnings"]), 1)

    def test_a_child_with_no_tool_calls_is_warned_about_and_one_with_calls_is_not(self):
        # Found live (2026-11-02): a Codex child "succeeded" saying its filesystem was read-only, without one tool call.
        os.environ["FAKE_CLAUDE_MODE"] = "stream-manual"
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertEqual(w["state"], "succeeded")
        self.assertEqual(w["tool_uses"], {})
        self.assertTrue(any(x.startswith("no tool calls were made") for x in w["warnings"]), w)
        os.environ["FAKE_CLAUDE_MODE"] = "stream-outside"
        os.environ["FAKE_OUTSIDE"] = os.path.join(self.tmp, "elsewhere.txt")
        _, w = self.call("wait_task", task_id=self.start(), timeout_s=30)
        self.assertTrue(w["tool_uses"], w)
        self.assertFalse(any(x.startswith("no tool calls") for x in w.get("warnings", [])), w)

    def _recovered(self, lines):
        d = os.path.join(self.tmp, "recovered")
        os.makedirs(d, exist_ok=True)
        tok = "recover-token-00001"
        with open(os.path.join(d, "stdout.log"), "w") as f:
            f.write("\n".join(lines(tok)) + "\n")
        open(os.path.join(d, "stderr.log"), "w").close()
        meta = {"cwd": self.work, "cred_fingerprints": ct.credential_fingerprints([tok])}
        res = {"logs_unredacted": True}
        return d, tok, meta, res

    def test_recovered_logs_record_unparsed_lines_and_activity(self):
        d, tok, meta, res = self._recovered(lambda tok: [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "x.txt"}}]}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "w1",
                                                                 "content": tok}]}}),
            "not json " + tok])
        self.assertTrue(ct._release_recovered_logs(d, meta, res, ct.Redactor([tok])))
        self.assertEqual(res["redact_unparsed_lines"], 1)
        self.assertEqual(res["activity"]["files_written"], [os.path.join(self.work, "x.txt")])
        with open(os.path.join(d, "stdout.log")) as f:
            self.assertNotIn(tok, f.read())

    def test_a_structural_failure_in_recovery_withholds_logs_but_still_redacts_raw(self):
        d, tok, meta, res = self._recovered(lambda tok: [json.dumps({"type": "a", "t": tok})])
        with mock.patch.object(ct, "redact_stream_file", side_effect=RuntimeError("boom")):
            self.assertFalse(ct._release_recovered_logs(d, meta, res, ct.Redactor([tok])))
        self.assertIn("structurally", res["redact_error"])
        self.assertTrue(res["logs_unredacted"])                   # still withheld
        with open(os.path.join(d, "stdout.log")) as f:
            self.assertNotIn(tok, f.read())                        # the raw pass ran anyway

    def test_a_structural_failure_in_the_runner_fails_the_task_and_still_redacts_raw(self):
        os.environ["FAKE_CLAUDE_MODE"] = "echo-auth"
        tid, d = self._manual_task("echo-auth")
        old = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with mock.patch.object(ct, "redact_stream_file", side_effect=RecursionError("deep")):
            with mock.patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "runner-token-000001"}):
                ct._run(d)
        res = _load(os.path.join(d, "result.json"))
        self.assertIn("structurally", res["redact_error"])
        self.assertEqual(ct.classify(res), "failed")
        for name in ("stdout.log", "stderr.log"):
            with open(os.path.join(d, name)) as f:
                self.assertNotIn("runner-token-000001", f.read(), name)

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

    def test_chrome_login_mode_env_and_argv(self):
        os.environ.update({"ANTHROPIC_BASE_URL": "http://x.invalid", "ANTHROPIC_AUTH_TOKEN": "tok-test-000000001",
                           "ANTHROPIC_API_KEY": "sk-ant-test-" + "0" * 40, "ANTHROPIC_CUSTOM_HEADERS": "h: v", "ANTHROPIC_UNIX_SOCKET": "/s",
                           "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "cortados",
                           "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1", "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "cortados",
                           "CORTADOS_PROVIDER_TOKEN": "tok-test-000000002", "CORTADOS_PROVIDER_URL": "http://p.invalid",
                           "CORTADOS_PROVIDER_TOKEN_FILE": "/t", "ANTHROPIC_SMALL_FAST_MODEL": "m",
                           "ANTHROPIC_MODEL": "m", "ANTHROPIC_DEFAULT_OPUS_MODEL": "m", "CLAUDE_CONFIG_DIR": "/c"})
        r, out = self.call("start_task", task="hi", cwd=self.work, chrome=True)
        self.assertEqual(out["lane"], ct.LOGIN_LANE)
        self.assertEqual(out["model"], "claude-sonnet-5-5")
        d = ct.task_path(out["task_id"])
        _, st = self.call("wait_task", task_id=out["task_id"], timeout_s=30)
        self.assertEqual(st["lane"], ct.LOGIN_LANE)
        self.assertIn("login mode", st["router_run"]["note"])
        meta = _load(os.path.join(d, "meta.json"))
        self.assertEqual((meta["lane"], meta["model"]), (ct.LOGIN_LANE, "claude-sonnet-5-5"))
        seen = _load(os.path.join(self.work, "fake-argv.json"))
        a = seen["argv"]
        for k in ("CORTADOS_PROVIDER_TOKEN", "CORTADOS_PROVIDER_URL", "CORTADOS_PROVIDER_TOKEN_FILE",
                  "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CONFIG_DIR",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            self.assertIsNone(seen["env"][k], k)
        self.assertEqual(seen["env"]["HOME"], os.environ["HOME"])
        self.assertEqual(seen["nested"], "1")
        self.assertEqual(meta["cred_fingerprints"], [])
        sp = a[a.index("--settings") + 1]
        self.assertEqual(os.path.basename(sp), "session-login.json")
        self.assertEqual(seen["settings"], {"mode": 0o600, "body": {"enabledPlugins": {ct.session.PLUGIN_ID: False}}})
        self.assertFalse(os.path.exists(sp))   # removed with the child, like the routed child's settings file
        self.assertEqual(a[a.index("--model") + 1], "claude-sonnet-5-5")
        self.assertIn("--chrome", a)
        dis = a[a.index("--disallowedTools") + 1]
        for t in ("Bash(osascript:*)", "Bash(*osascript*)", "Bash(open -a *)", "Bash(*open -a*)", "Bash(*puppeteer*)",
                  "mcp__playwright", "mcp__puppeteer", "mcp__cortados-route"):
            self.assertIn(t, dis)
        self.assertIn("mcp__claude-in-chrome__", a[a.index("--append-system-prompt") + 1])
        # crash logs are released when the fingerprints are []: safe only while every launch-token key is forbidden
        self.assertLessEqual(set(ct.LAUNCH_TOKEN_KEYS), set(ct.LOGIN_FORBIDDEN_ENV))
        self.assertIn("SSH_AUTH_SOCK", ct.LOGIN_FORBIDDEN_ENV)
        self.assertIn("CLAUDECODE", ct.LOGIN_FORBIDDEN_ENV)
        self.assertNotIn("SSH_AUTH_SOCK", ct.LOGIN_ENV_ALLOW)
        self.assertEqual(ct.credential_fingerprints(ct.launch_tokens(ct.login_env(os.environ))), [])
        px = ct.login_env({"HTTPS_PROXY": "http://proxy:3128", "http_proxy": "http://u:p@proxy:3128",
                           "HTTP_PROXY": "u@proxy:1", "NO_PROXY": "localhost", "SSL_CERT_FILE": "/ca.pem",
                           "NODE_EXTRA_CA_CERTS": "/ca2.pem", "SSH_AUTH_SOCK": "/agent"})
        self.assertEqual(px, {"HTTPS_PROXY": "http://proxy:3128", "NO_PROXY": "localhost",
                              "SSL_CERT_FILE": "/ca.pem", "NODE_EXTRA_CA_CERTS": "/ca2.pem"})
        self.assertRaises(ct.TaskError, ct.wait_max_s, {"CORTADOS_WAIT_MAX_S": 30})
        env = ct.login_env(dict(os.environ, CLAUDECODE="1"))
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS",
                  "ANTHROPIC_UNIX_SOCKET", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_SUBAGENT_MODEL",
                  "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL", "CLAUDECODE",
                  "CORTADOS_PROVIDER_TOKEN", "CORTADOS_PROVIDER_URL", "CORTADOS_PROVIDER_TOKEN_FILE",
                  "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CONFIG_DIR"):
            self.assertNotIn(k, env)
        # even an allowlist that admitted them could not pass the forbidden names through
        with mock.patch.object(ct, "LOGIN_ENV_ALLOW", ct.LOGIN_ENV_ALLOW + ("CORTADOS_PROVIDER_TOKEN", "ANTHROPIC_MODEL")):
            self.assertNotIn("CORTADOS_PROVIDER_TOKEN", ct.login_env(os.environ))
            self.assertNotIn("ANTHROPIC_MODEL", ct.login_env(os.environ))
        with open(os.path.join(os.environ["CORTADOS_HOME"], "child-tasks.jsonl")) as f:
            self.assertIn(ct.LOGIN_LANE, f.read())

    def test_chrome_model_mapping_and_refusals(self):
        self.assertEqual(ct.login_model("cortados"), "claude-sonnet-5-5")
        self.assertEqual(ct.login_model("claude-opus-5-5"), "claude-opus-5-5")
        self.assertEqual(ct.login_model("sub/claude-haiku-5-5"), "claude-haiku-5-5")
        for bad in ("gpt-6.1-sol", "qwen-27b"):
            r, _ = self.call("start_task", task="x", cwd=self.work, chrome=True, orchestrator=bad)
            self.assertIn("only run Claude models", r["content"][0]["text"])
        r, _ = self.call("start_task", task="x", cwd=self.work, chrome=True, connectors=["a"])
        self.assertIn("connectors are not used with chrome", r["content"][0]["text"])
        r, out = self.call("start_task", task="x", cwd=self.work, chrome=True,
                           orchestrator="claude-haiku-4-5-20251001")
        self.assertEqual(out["permission_mode"], "acceptEdits")
        self.call("wait_task", task_id=out["task_id"], timeout_s=30)

    def test_chrome_refuses_api_settings(self):
        cases = [(self.user_settings, {"env": {"ANTHROPIC_API_KEY": "x"}}),
                 (os.path.join(self.work, ".claude", "settings.json"), {"apiKeyHelper": "x"}),
                 (os.path.join(self.work, ".claude", "settings.local.json"), {"env": {"ANTHROPIC_BASE_URL": "x"}})]
        os.makedirs(os.path.join(self.work, ".claude"))
        for path, cfg in cases:
            with open(path, "w") as f:
                json.dump(cfg, f)
            r, _ = self.call("start_task", task="x", cwd=self.work, chrome=True)
            self.assertIn("chrome task refused", r["content"][0]["text"])
            os.unlink(path)
        self.assertFalse(os.path.exists(os.path.join(self.work, "fake-argv.json")), "no child was spawned")

    def test_chrome_refuses_managed_policy(self):
        md = os.path.join(self.tmp, "managed")
        os.makedirs(os.path.join(md, "managed-settings.d"))
        with mock.patch.object(ct, "MANAGED_DIRS", (md,)):
            for path, cfg in ((os.path.join(md, "managed-settings.json"), {"env": {"ANTHROPIC_BASE_URL": "x"}}),
                              (os.path.join(md, "managed-settings.d", "a.json"), {"apiKeyHelper": "x"})):
                with open(path, "w") as f:
                    json.dump(cfg, f)
                r, _ = self.call("start_task", task="x", cwd=self.work, chrome=True)
                self.assertIn("chrome task refused", r["content"][0]["text"])
                os.unlink(path)
        self.assertFalse(os.path.exists(os.path.join(self.work, "fake-argv.json")), "no child was spawned")

    def test_login_logs_released_after_crash(self):
        # [] fingerprints (login mode: no launch credential) release; a missing field still withholds
        red = ct.Redactor()
        d = os.path.join(self.tmp, "crashed")
        os.mkdir(d)
        for n in ("stdout.log", "stderr.log"):
            with open(os.path.join(d, n), "w") as f:
                f.write("")
        self.assertFalse(ct._release_recovered_logs(d, {}, {}, red))
        self.assertTrue(ct._release_recovered_logs(d, {"cred_fingerprints": []}, {}, red))

    def test_routed_argv_unchanged_without_chrome(self):
        tid = self.start()
        self.call("wait_task", task_id=tid, timeout_s=30)
        a = _load(os.path.join(self.work, "fake-argv.json"))["argv"]
        self.assertIn("--settings", a)
        self.assertNotIn("--disallowedTools", a)
        self.assertNotIn("BROWSER RULES", a[a.index("--append-system-prompt") + 1])
        self.assertNotIn("lane", _load(os.path.join(ct.task_path(tid), "meta.json")))
        self.assertNotIn("mcp__cortados-route", " ".join(a))
        self.assertTrue(_load(os.path.join(ct.task_path(tid), "meta.json"))["cred_fingerprints"])
        self.assertTrue(os.path.basename(a[a.index("--settings") + 1]).startswith("session-"))

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

    def test_provider_refusal_surfaced_in_status(self):
        os.environ["FAKE_CLAUDE_MODE"] = "too-large"
        tid = self.start()
        self.runs[tid] = {"run_id": tid, "model": None, "label": None, "provider": None, "models": [],
                          "builder_models": [], "refusals": [{"status": 413, "type": "request_too_large",
                                                              "reason": "the request is larger than every eligible "
                                                                        "model's context"}]}
        _, w = self.call("wait_task", task_id=tid, timeout_s=30)
        self.assertEqual(w["state"], "failed")
        self.assertEqual(w["api_error_status"], 413)
        self.assertIn("HTTP 413 request_too_large", w["provider_refusal"])
        self.assertIn("larger than every eligible model's context", w["provider_refusal"])
        self.assertIn("connectors", w["hint"])

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

    def test_wait_cap_is_50s_for_the_desktop_app(self):
        # The Claude desktop app times MCP requests out at 60 s (observed 2026-11-03).
        os.environ.pop("CORTADOS_WAIT_MAX_S", None)
        self.assertEqual(ct.wait_max_s(), 50)
        r, _ = self.call("wait_task", task_id="t20261102000000-0000000a", timeout_s=60)
        self.assertTrue(r.get("isError"))
        self.assertIn("between 0 and 50", r["content"][0]["text"])

    def test_wait_cap_env_override(self):
        self.assertEqual(ct.wait_max_s({"CORTADOS_WAIT_MAX_S": "300"}), 300)
        self.assertEqual(ct.wait_max_s({"CORTADOS_WAIT_MAX_S": ""}), 50)
        self.assertEqual(ct.wait_max_s({}), 50)
        for bad in ("abc", "50s", "0", "-5", "5000", "99999", "nan"):
            with self.assertRaises(ct.TaskError, msg=bad):
                ct.wait_max_s({"CORTADOS_WAIT_MAX_S": bad})
        with self.assertRaises(ct.TaskError):
            ct.wait("t20261102000000-0000000a", 120, max_s=50)
        for bad in (float("nan"), 0, -1, "x"):
            with self.assertRaises(ct.TaskError, msg=repr(bad)):
                ct.wait("t20261102000000-0000000a", 1, max_s=bad)

    def test_wait_bad_env_cap_is_a_clear_tool_error(self):
        os.environ["CORTADOS_WAIT_MAX_S"] = "50s"
        r, _ = self.call("wait_task", task_id="t20261102000000-0000000a", timeout_s=1)
        self.assertTrue(r.get("isError"))
        self.assertIn("CORTADOS_WAIT_MAX_S='50s'", r["content"][0]["text"])

    def test_wait_default_honours_the_override(self):
        # default = min(WAIT_MAX_S, cap): a lower cap lowers the default, a higher cap does not raise it
        seen = []
        with mock.patch.object(ct, "_state", lambda d: ("running", {}, {})), \
                mock.patch.object(ct, "task_path", lambda tid: "/nonexistent"), \
                mock.patch.object(ct, "status", lambda *a, **k: {"state": "running"}), \
                mock.patch.object(ct.time, "sleep", lambda s: seen.append(s)):
            with mock.patch.dict(os.environ, {"CORTADOS_WAIT_MAX_S": "0.05"}):
                out = ct.wait("t20261102000000-0000000a", poll_s=0.01)
            self.assertIn("waited 0.05 s", out["wait_ended"])
            with mock.patch.dict(os.environ, {"CORTADOS_WAIT_MAX_S": "300"}):
                with mock.patch.object(ct, "WAIT_MAX_S", 0.02):
                    out = ct.wait("t20261102000000-0000000a", poll_s=0.01)
            self.assertIn("waited 0.02 s", out["wait_ended"])

    def test_tools_list_shows_the_wait_cap_in_force(self):
        def wait_entry():
            r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, self.cfg)
            return next(t for t in r["result"]["tools"] if t["name"] == "wait_task")
        os.environ.pop("CORTADOS_WAIT_MAX_S", None)
        self.assertIn("0-50", wait_entry()["inputSchema"]["properties"]["timeout_s"]["description"])
        os.environ["CORTADOS_WAIT_MAX_S"] = "300"
        w = wait_entry()
        self.assertIn("0-300, default 50", w["inputSchema"]["properties"]["timeout_s"]["description"])
        self.assertIn("max 300 s", w["description"])

    def test_wait_still_running_says_call_again(self):
        # a real sleeping fake child through the MCP call path
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        tid = self.start()
        r, out = self.call("wait_task", task_id=tid, timeout_s=0.1)
        self.assertFalse(r.get("isError"), r)
        self.assertEqual(out["state"], "running")
        self.assertIn("call wait_task again", out["wait_ended"])
        self.call("cancel_task", task_id=tid)
        self.call("wait_task", task_id=tid, timeout_s=30)

    def test_wait_reply_lands_inside_the_client_budget_when_the_fetch_hangs(self):
        # P1: a finished task's run-record fetch hanging must not push the reply past the client's 60 s timeout;
        # the fetch timeout shrinks to what is left of the deadline (min RUN_FETCH_MIN_S).
        tid = self.start()
        self.call("wait_task", task_id=tid, timeout_s=30)        # let it finish
        timeouts = []

        def hanging(req, timeout=None):
            timeouts.append(timeout)
            time.sleep(timeout)
            raise urllib.error.URLError("timed out")
        with mock.patch.object(ct, "_urlopen", hanging), mock.patch.object(ct, "POST_WAIT_RESERVE_S", 6.5):
            t0 = time.monotonic()
            r, out = self.call("wait_task", task_id=tid, timeout_s=0.5)
            elapsed = time.monotonic() - t0
        self.assertFalse(r.get("isError"), r)
        self.assertNotEqual(out["state"], "running")
        self.assertIn("error", out["router_run"])
        # budget: timeout_s 0.5 + reserve 6.5 = 7 s; the fetch gets end + reserve - recovery grace = ~2 s from now
        self.assertLessEqual(timeouts[0], 2.0 + 0.01)
        self.assertLess(elapsed, 0.5 + 6.5)
        self.assertLess(elapsed, 3.0)                              # the unbounded fetch would have taken 10 s

    def test_unconfigured_url(self):
        self.cfg["url"] = ""
        r, _ = self.call("start_task", task="x", cwd=self.work)
        self.assertIn("not configured", r["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
