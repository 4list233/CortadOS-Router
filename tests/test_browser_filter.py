"""integrations/browser/mcp_filter.py (docs/BROWSER-TOOL.md, ticket B1b). The filter runs as a subprocess in front of a
stub MCP server (stdlib python over pipes) that records every message it receives, so each test checks both what the
client sees and that refused requests never reached the server. No node, npm or browser."""
import contextlib
import signal
import io
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILTER = ROOT / "integrations" / "browser" / "mcp_filter.py"
TOOLS = (ROOT / "integrations" / "browser" / "tools-0.0.84.txt").read_text().split()
ARGS = json.loads((ROOT / "integrations" / "browser" / "args-0.0.84.json").read_text())
# Tools of the pinned schemas that take `filename` (a server-side write path); the filter refuses it on each.
FILENAME_TOOLS = ["browser_snapshot", "browser_console_messages", "browser_find", "browser_network_requests",
                  "browser_network_request", "browser_take_screenshot", "browser_evaluate"]

STUB = r'''import json, subprocess, sys
names, log = open(sys.argv[1]).read().split(), open(sys.argv[2], "a")
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    log.write(json.dumps(m) + "\n"); log.flush()
    if "method" not in m:
        continue
    if m.get("id") is None:
        continue
    meth = m["method"]
    if meth == "tools/list":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": n} for n in names]}})
        continue
    if meth == "tools/call":
        trig = m["params"].get("arguments", {}).get("trigger")
        if trig == "roots":
            send({"jsonrpc": "2.0", "id": 900, "method": "roots/list"})
        elif trig == "sampling":
            send({"jsonrpc": "2.0", "id": 901, "method": "sampling/createMessage", "params": {}})
        elif trig == "changed":
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        elif trig == "resources":
            send({"jsonrpc": "2.0", "method": "notifications/resources/list_changed"})
        elif trig == "stray":
            send({"jsonrpc": "2.0", "id": "never-asked", "result": {}})
        elif trig == "hold":
            continue                                   # never answered (until the client cancels)
        elif trig == "big":
            sys.stdout.write("{\"x\":\"" + "a" * 8192 + "\"}\n"); sys.stdout.flush()
        elif trig == "spawn":
            # Like Playwright's Chromium: detached into a session (and group) of its own, inheriting stderr.
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            open(sys.argv[2] + ".pid", "w").write(str(g.pid))
        elif trig == "exit":
            sys.exit(3)                                # the server dies on its own, no answer
        elif trig == "spawn_exit":
            # Chromium-like grandchild with its --user-data-dir under the task's TMPDIR, made an orphan at once (double
            # fork) so no descendant walk can ever see it; then the server dies abruptly. It executes <log>.exe (a
            # re-signed copy of bash, so the kernel's executable path is that file) with argv[0] from <log>.argv0, so a
            # test can run a real browser-named binary, one outside the browsers path, or a shell forging argv[0].
            prof = open(sys.argv[2] + ".task").read() + "/browser/tmp/playwright_chromiumdev_profile-x"
            exe = open(sys.argv[2] + ".exe").read()
            argv0 = open(sys.argv[2] + ".argv0").read()
            code = ("import subprocess, sys; g = subprocess.Popen([sys.argv[4], '-c', 'sleep 120; :', 'x', "
                    "'--user-data-dir=' + sys.argv[1]], executable=sys.argv[3], start_new_session=True, "
                    "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                    "open(sys.argv[2], 'w').write(str(g.pid))")
            subprocess.run([sys.executable, "-c", code, prof, sys.argv[2] + ".pid", exe, argv0],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            sys.exit(3)
    send({"jsonrpc": "2.0", "id": m["id"], "result": {"ok": meth}})
'''


class FilterTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.task = self.tmp / "task"
        (self.task / "browser" / "in").mkdir(parents=True)
        (self.task / "browser" / "in" / "a.txt").write_text("x")
        (self.tmp / "tools").write_text("\n".join(TOOLS) + "\n")
        (self.tmp / "stub.py").write_text(STUB)
        # The real per-tool keys, plus the stub's test-only `trigger` on browser_snapshot.
        args = dict(ARGS, browser_snapshot=ARGS["browser_snapshot"] + ["trigger"])
        (self.tmp / "args.json").write_text(json.dumps(args))
        self.log = self.tmp / "server.log"
        self.log.write_text("")
        Path(str(self.log) + ".task").write_text(str(self.task))
        self.browsers = self.tmp / "browsers"
        self.fake_exe = self.browsers / "chromium_headless_shell-1" / "chrome-mac" / "headless_shell"
        self.spawn_as(self.fake_exe)
        self.p = None

    def tearDown(self):
        if self.p and self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        if self.p:
            for f in (self.p.stdin, self.p.stdout, self.p.stderr):
                f.close()
        self._td.cleanup()

    def start(self, *extra, args_file=True):
        af = ["--args-file", str(self.tmp / "args.json")] if args_file else []
        self.p = subprocess.Popen(
            [sys.executable, "-I", str(FILTER), "--task", str(self.task), "--browsers-path", str(self.browsers),
             *af, *extra, "--",
             sys.executable, "-I", str(self.tmp / "stub.py"), str(self.tmp / "tools"), str(self.log)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buf = b""

    def spawn_as(self, exe, argv0=None):
        Path(str(self.log) + ".exe").write_text(str(exe))
        Path(str(self.log) + ".argv0").write_text(str(argv0 or exe))

    def fake_browser(self, path):
        """A real executable at path (bash copied and ad-hoc re-signed: a copied platform binary is killed by the
        kernel), so proc_pidpath reports that path. Skips the test where that is not possible (not macOS)."""
        if sys.platform != "darwin" or not os.path.exists("/usr/bin/codesign"):
            self.skipTest("kernel executable identity is macOS-only (libproc)")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile("/bin/bash", path)
        os.chmod(path, 0o755)
        r = subprocess.run(["/usr/bin/codesign", "-f", "-s", "-", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"codesign failed: {r.stderr.strip()}")
        return path

    def send_raw(self, data):
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def send(self, msg):
        self.send_raw(json.dumps(msg).encode() + b"\n")

    def recv(self, timeout=10):
        end = time.time() + timeout
        while b"\n" not in self.buf:
            left = end - time.time()
            if left <= 0:
                self.fail("no answer from the filter within timeout")
            r, _, _ = select.select([self.p.stdout], [], [], left)
            if not r:
                self.fail("no answer from the filter within timeout")
            chunk = os.read(self.p.stdout.fileno(), 65536)
            if not chunk:
                self.fail("filter closed its output without an answer")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, mid, name, **args):
        self.send({"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return self.recv()

    def server_saw(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def calls_seen(self):
        return [m["params"]["name"] for m in self.server_saw() if m.get("method") == "tools/call"]

    def assert_refused(self, resp, mid, code):
        self.assertEqual(resp["id"], mid)
        self.assertEqual(resp["error"]["code"], code, resp)

    def assert_closed(self):
        self.assertEqual(self.p.wait(timeout=10), 1)
        self.assertIn(b"closing session", self.p.stderr.read())

    # tools/list and tools/call allowlist
    def test_hidden_tools_absent_from_list(self):
        self.start()
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in self.recv()["result"]["tools"]}
        self.assertEqual(names, set(TOOLS) - {"browser_evaluate", "browser_run_code_unsafe", "browser_file_upload",
                                              "browser_drop"})

    def test_grants_add_back_but_never_run_code_unsafe(self):
        self.start("--grant", "browser_js", "--grant", "browser_upload")
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in self.recv()["result"]["tools"]}
        self.assertEqual(names, set(TOOLS) - {"browser_run_code_unsafe"})
        self.assert_refused(self.call(2, "browser_run_code_unsafe", code="1"), 2, -32001)
        self.assertEqual(self.call(3, "browser_evaluate", function="() => 1")["result"], {"ok": "tools/call"})
        self.assertEqual(self.calls_seen(), ["browser_evaluate"])

    def test_default_off_and_unknown_tools_refused(self):
        self.start()
        for i, name in enumerate(["browser_evaluate", "browser_file_upload", "browser_drop",
                                  "browser_run_code_unsafe", "browser_webmcp_list", "not_a_tool"]):
            self.assert_refused(self.call(i, name), i, -32001)
        self.assertEqual(self.calls_seen(), [])

    def test_upload_paths_must_be_under_task_in(self):
        self.start("--grant", "browser_upload")
        ok = str(self.task / "browser" / "in" / "a.txt")
        self.assertIn("result", self.call(1, "browser_file_upload", paths=[ok]))
        # Relative paths are refused outright: the server resolves them against <task>/browser, not in/, so
        # 'in/../config.json' is the proxy config and 'a.txt' is <task>/browser/a.txt.
        for i, bad in enumerate([["/etc/passwd"], [ok, "../../../x"], [str(self.task / "browser" / "in")], [5],
                                 ["in/../config.json"], ["a.txt"], [""], [str(self.task / "browser" / "config.json")]]):
            self.assert_refused(self.call(10 + i, "browser_file_upload", paths=bad), 10 + i, -32004)
            self.assert_refused(self.call(50 + i, "browser_drop", paths=bad), 50 + i, -32004)
        self.assertEqual(self.calls_seen(), ["browser_file_upload"])

    # argument keys
    def test_unknown_argument_keys_refused_on_every_tool(self):
        self.start("--grant", "browser_js", "--grant", "browser_upload")
        allowed = sorted(set(TOOLS) - {"browser_run_code_unsafe"})
        for i, name in enumerate(allowed):
            self.assert_refused(self.call(i, name, zz_unknown="x"), i, -32005)
        self.assert_refused(self.call(99, "browser_drop", path="/etc/hosts"), 99, -32005)
        self.assertEqual(self.calls_seen(), [])

    def test_default_args_file_is_the_pinned_schema(self):
        self.start(args_file=False)
        self.assert_refused(self.call(1, "browser_snapshot", trigger="roots"), 1, -32005)
        self.assertIn("result", self.call(2, "browser_snapshot", depth=2))
        self.assertEqual(self.calls_seen(), ["browser_snapshot"])

    def test_filename_refused_on_each_tool_that_takes_it(self):
        self.start("--grant", "browser_js")
        for name in FILENAME_TOOLS:
            self.assertIn("filename", ARGS[name], name)   # the pinned schema really has it
        for i, name in enumerate(FILENAME_TOOLS):
            self.assert_refused(self.call(i, name, filename="config.json"), i, -32004)
        self.assertEqual(self.calls_seen(), [])

    # URL rule
    def test_url_schemes_refused(self):
        self.start()
        bad = ["javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)", "\tjavascript:alert(1)",
               "data:text/html,<script>1</script>", "blob:https://x/1", "chrome://settings", "file:///etc/passwd",
               "view-source:http://x", "about:srcdoc", "HTTP://example.com", "http://", "http://[::1", "http://a:b/",
               "not a url", "", None, 5]
        for i, url in enumerate(bad):
            args = {} if url is None else {"url": url}
            self.assert_refused(self.call(i, "browser_navigate", **args), i, -32002)
            self.assert_refused(self.call(100 + i, "browser_tabs", action="new", url=url), 100 + i, -32002)
        self.assertEqual(self.calls_seen(), [])

    def test_url_schemes_admitted(self):
        self.start()
        for i, url in enumerate(["http://example.com/", "https://example.com/a?b#c", "about:blank"]):
            self.assertIn("result", self.call(i, "browser_navigate", url=url), url)
            self.assertIn("result", self.call(10 + i, "browser_tabs", action="new", url=url), url)
        self.assertEqual(len(self.calls_seen()), 6)

    # page cap
    def test_page_cap(self):
        self.start("--max-pages", "3")
        self.assertIn("result", self.call(1, "browser_navigate", url="https://a.example/"))
        self.assertIn("result", self.call(2, "browser_navigate_back"))
        self.assertIn("result", self.call(3, "browser_tabs", action="new"))
        self.assertIn("result", self.call(4, "browser_tabs", action="list"))       # not a page
        self.assert_refused(self.call(5, "browser_navigate", url="https://b.example/"), 5, -32003)
        self.assert_refused(self.call(6, "browser_navigate_back"), 6, -32003)
        self.assert_refused(self.call(7, "browser_tabs", action="new"), 7, -32003)
        self.assertIn("result", self.call(8, "browser_snapshot"))
        self.assertEqual(len(self.calls_seen()), 5)

    # protocol: methods and params
    def test_disallowed_method_refused_and_notification_dropped(self):
        self.start()
        self.send({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        self.assert_refused(self.recv(), 1, -32601)
        self.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
        self.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(self.recv()["id"], 2)
        self.assertEqual([m["method"] for m in self.server_saw()], ["ping"])

    def test_bad_tools_call_params_refused(self):
        self.start()
        cases = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": ["browser_snapshot"]},
                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": 7}},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"arguments": {}}},
                 {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "browser_snapshot", "arguments": "x"}}]
        for c in cases:
            self.send(c)
            self.assert_refused(self.recv(), c["id"], -32602)
        self.assertEqual(self.server_saw(), [])

    def test_bad_request_ids_refused(self):
        self.start()
        for mid in (True, 1.5, {"a": 1}, "cortados-filter-1"):
            self.send({"jsonrpc": "2.0", "id": mid, "method": "ping"})
            r = self.recv()
            self.assertIsNone(r["id"])
            self.assertEqual(r["error"]["code"], -32600)
        self.assertEqual(self.server_saw(), [])

    # protocol: session-closing input
    def test_batch_array_closes(self):
        self.start()
        self.send([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assert_closed()

    def test_bad_json_closes(self):
        self.start()
        self.send_raw(b"{not json\n")
        self.assert_closed()

    def test_non_object_closes(self):
        self.start()
        self.send_raw(b'"ping"\n')
        self.assert_closed()

    def test_oversize_line_closes(self):
        self.start()
        try:
            self.send_raw(b'{"x":"' + b"a" * (1 << 20) + b'"}\n')
        except BrokenPipeError:
            pass  # the filter may close before the whole line is written; the exit code below is the check
        self.assert_closed()
        self.assertEqual(self.server_saw(), [])

    # server -> client
    def test_server_requests_answered_and_not_forwarded(self):
        self.start()
        for i, trig in enumerate(["roots", "sampling"]):
            r = self.call(i, "browser_snapshot", trigger=trig)
            self.assertEqual(r["id"], i)           # the client sees only its own answer, never the server request
            self.assertIn("result", r)
        time.sleep(0.3)
        answers = {m["id"]: m for m in self.server_saw() if "method" not in m}
        self.assertEqual(answers[900]["error"]["code"], -32601)
        self.assertEqual(answers[901]["error"]["code"], -32601)

    def test_other_server_notifications_and_stray_answers_dropped(self):
        self.start()
        self.assertEqual(self.call(1, "browser_snapshot", trigger="resources")["id"], 1)
        self.assertEqual(self.call(2, "browser_snapshot", trigger="stray")["id"], 2)

    def test_list_changed_relists_and_is_forwarded(self):
        self.start()
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "browser_snapshot", "arguments": {"trigger": "changed"}}})
        first = self.recv()
        self.assertEqual(first["method"], "notifications/tools/list_changed")
        self.assertEqual(self.recv()["id"], 1)
        time.sleep(0.3)
        relists = [m for m in self.server_saw() if m.get("method") == "tools/list"]
        self.assertEqual(len(relists), 1)
        self.assertTrue(relists[0]["id"].startswith("cortados-filter-"))
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in self.recv()["result"]["tools"]}
        self.assertNotIn("browser_run_code_unsafe", names)

    def test_server_line_cap_is_a_parameter(self):
        self.start("--server-line-max", "4096")
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "browser_snapshot", "arguments": {"trigger": "big"}}})
        self.assertEqual(self.p.wait(timeout=20), 1)
        self.assertIn(b"server line over 4096 bytes", self.p.stderr.read())

    def test_client_line_cap_is_a_parameter(self):
        self.start("--client-line-max", "64")
        self.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "a" * 100}})
        self.assertEqual(self.p.wait(timeout=20), 1)
        self.assertIn(b"client line over 64 bytes", self.p.stderr.read())

    def test_nan_closes(self):
        self.start()
        self.send_raw(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n')
        self.assert_closed()
        self.assertEqual(self.server_saw(), [])

    def test_duplicate_in_flight_id_refused_and_cancel_frees_it(self):
        self.start()
        hold = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "browser_snapshot", "arguments": {"trigger": "hold"}}}
        self.send(hold)
        self.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        r = self.recv()
        self.assertIsNone(r["id"])
        self.assertEqual(r["error"]["code"], -32600)
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
        self.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        self.assertEqual(self.recv(), {"jsonrpc": "2.0", "id": 7, "result": {"ok": "ping"}})

    # close kills the whole tree, detached descendants included
    def _grandchild(self):
        self.assertEqual(self.call(1, "browser_snapshot", trigger="spawn")["id"], 1)
        pid = int(Path(str(self.log) + ".pid").read_text())
        os.kill(pid, 0)   # alive before close
        return pid

    def _assert_dead(self, pid):
        end = time.time() + 10
        while time.time() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(pid, 9)
        self.fail(f"detached grandchild {pid} survived close()")

    def test_close_kills_detached_grandchild(self):
        self.start()
        pid = self._grandchild()
        self.send_raw(b"{not json\n")
        self.assertEqual(self.p.wait(timeout=20), 1)
        self._assert_dead(pid)
        self.assertIn(b"closing session", self.p.stderr.read())   # EOF: nothing holds the stderr pipe any more

    def test_eof_kills_detached_grandchild(self):
        self.start()
        pid = self._grandchild()
        self.p.stdin.close()
        self.assertEqual(self.p.wait(timeout=20), 0)
        self._assert_dead(pid)

    def test_sigterm_kills_detached_grandchild(self):
        self.start()
        pid = self._grandchild()
        self.p.terminate()
        self.assertEqual(self.p.wait(timeout=20), 1)
        self._assert_dead(pid)
        self.assertIn(b"closing session: signal", self.p.stderr.read())

    def _stderr_eof(self, timeout=10):
        """Read the filter's stderr to EOF in a thread; fail if something (a surviving grandchild) holds it open."""
        out = []
        t = threading.Thread(target=lambda: out.append(self.p.stderr.read()), daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.fail("filter stderr still open: a descendant survived holding it")
        return out[0]

    def test_server_exit_kills_orphaned_task_browser_process(self):
        # P2-1: the server exits on its own after launching a detached child that is already an orphan; only the
        # command-line ownership (a real executable under the browsers path, the task's profile) can find it.
        self.fake_browser(self.fake_exe)
        self.start()
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "browser_snapshot", "arguments": {"trigger": "spawn_exit"}}})
        self.p.wait(timeout=20)
        pid = int(Path(str(self.log) + ".pid").read_text())
        self._assert_dead(pid)
        self.assertIn(b"closing session: server closed its output", self._stderr_eof())

    def test_server_exit_kills_descendant_seen_by_poll(self):
        # P2-1: a detached child that does NOT name the task dir, seen by the periodic walk before the server dies.
        self.start()
        pid = self._grandchild()
        time.sleep(3)   # > TREE_POLL_S: the poll has recorded it
        self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "browser_snapshot", "arguments": {"trigger": "exit"}}})
        self.p.wait(timeout=20)
        self._assert_dead(pid)
        self.assertIn(b"closing session: server closed its output", self._stderr_eof())

    def _bare_filter(self, browsers_path=True):
        sys.path.insert(0, str(FILTER.parent))
        try:
            import mcp_filter
        finally:
            sys.path.remove(str(FILTER.parent))
        f = mcp_filter.Filter.__new__(mcp_filter.Filter)
        f.browsers_root = os.path.realpath(self.browsers) if browsers_path else None
        f.task_tmp = os.path.realpath(self.task / "browser" / "tmp")
        f.proc_lock = threading.Lock()
        return mcp_filter, f

    def test_server_exit_spares_non_browser_with_task_profile_flag(self):
        # P2 round 3: an orphan whose --user-data-dir is the task's, but whose executable is not under the browsers
        # path, is not the task's browser and survives the close.
        self.spawn_as(self.fake_browser(self.tmp / "elsewhere" / "headless_shell"))
        self._assert_survives_spawn_exit()

    def test_server_exit_spares_shell_forging_browser_argv0(self):
        # P1 round 4: a shell whose argv[0] (all ps shows) is the browser path under the browsers dir, with the task's
        # profile flag: the kernel's executable is the shell, so it is not the task's browser and survives.
        self.fake_browser(self.tmp / "unused")   # skips off macOS like the positive test
        self.spawn_as("/bin/bash", argv0=self.fake_exe)
        self._assert_survives_spawn_exit()

    def _assert_survives_spawn_exit(self):
        self.start()
        self.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "browser_snapshot", "arguments": {"trigger": "spawn_exit"}}})
        self.p.wait(timeout=20)
        pid = int(Path(str(self.log) + ".pid").read_text())
        try:
            os.kill(pid, 0)   # still alive
        finally:
            try:
                os.killpg(pid, 9)
            except ProcessLookupError:
                pass

    def test_owned_by_task_needs_browser_exe_and_profile_token(self):
        # Decided on the kernel's (executable, argv) per pid (_identity, mocked here), never on the ps text.
        _, f = self._bare_filter()
        uid = os.getuid()
        B, T = str(self.browsers), str(self.task)
        prof = f"--user-data-dir={T}/browser/tmp/playwright_chromiumdev_profile-x"
        shell = f"{B}/chromium_headless_shell-1/chrome-mac/headless_shell"
        helper = (f"{B}/chromium-1/chrome-mac/Chromium.app/Contents/Frameworks/Chromium Framework.framework/Helpers/"
                  "Chromium Helper (Renderer).app/Contents/MacOS/Chromium Helper (Renderer)")
        st = "Tue Nov  3 10:00:00 2026"
        cases = [(10, 10, uid, shell, [shell, "--headless", prof]),                                 # yes
                 (11, 10, uid, helper, [helper, "--type=renderer", prof]),                          # yes (helper)
                 (20, 20, uid, "/bin/zsh", ["/bin/zsh", "-c", f"ls {T}/browser/tmp/p", prof]),       # a shell
                 (21, 21, uid, shell, [shell, "--user-data-dir=/x", f"https://e.test{T}/browser/tmp/p"]),  # URL only
                 (22, 22, uid, shell, [shell, f"--app=https://e.test/?{prof}"]),                    # flag in a URL
                 (23, 23, uid, shell, [shell, f"--user-data-dir=/backup{T}/browser/tmp/p"]),        # leading path
                 (24, 24, uid, f"{self.tmp}/elsewhere/headless_shell", ["headless_shell", prof]),   # not under B
                 (25, 25, uid, f"{B}/node-1/bin/node", ["node", prof]),                             # not a browser
                 (26, 26, uid, shell, [shell, f"--user-data-dir={T}/browser/tmpX/p"]),              # sibling dir
                 (27, 27, uid, shell, [shell, f"--user-data-dir={T}/browser/tmp/../../../etc"]),    # traversal
                 (28, 28, uid, shell, [shell, f"--user-data-dir={T}/browser/tmp"]),                 # tmp itself
                 (29, 29, uid, f"/backup{shell}", [f"/backup{shell}", prof]),                       # exe collision
                 (31, 31, uid, "/bin/bash", [shell, "-c", "sleep 120", prof]),                      # forged argv[0]
                 (32, 32, uid, None, [shell, prof]),                                                # identity unknown
                 (30, 30, uid + 1, shell, [shell, prof]),                                           # other user
                 (os.getpid(), 1, uid, shell, [shell, prof])]                                       # self
        rows = [(pid, pgid, 1, u, st, " ".join(argv)) for pid, pgid, u, _, argv in cases]
        ident = {pid: (exe, argv) if exe else None for pid, _, _, exe, argv in cases}
        f._identity = ident.get
        self.assertEqual(f.owned_by_task(rows), {10: (10, st), 11: (10, st)})
        _, off = self._bare_filter(browsers_path=False)
        off._identity = ident.get
        self.assertEqual(off.owned_by_task(rows), {})   # no --browsers-path: nothing is owned by command line

    def test_identity_is_the_kernel_executable_not_argv0(self):
        if sys.platform != "darwin":
            self.skipTest("macOS-only (libproc)")
        m, _ = self._bare_filter()
        p = subprocess.Popen(["/forged/headless_shell", "-c", "sleep 30; :", "x", "--user-data-dir=/a b"],
                             executable="/bin/bash")
        try:
            time.sleep(0.3)
            exe, argv = m.Filter._identity(p.pid)
            self.assertEqual(os.path.realpath(exe), os.path.realpath("/bin/bash"))
            self.assertEqual(argv, ["/forged/headless_shell", "-c", "sleep 30; :", "x", "--user-data-dir=/a b"])
        finally:
            p.kill()
            p.wait()
        self.assertIsNone(m.Filter._identity(p.pid))   # gone: unknown, never guessed

    class FakeProc:
        """Stateful stand-in for the server Popen: once it has exited, the first poll()/wait() reaps it (reaped flips),
        exactly like Popen; poll_delay makes the first poll() slow, to open a check-then-signal window."""
        def __init__(self, pid, exited=False, reaped=False, poll_delay=0):
            self.pid, self.args = pid, ["server"]
            self.exited, self.reaped, self.poll_delay = exited or reaped, reaped, poll_delay

        def poll(self):
            rc = None if not self.exited else 0
            if self.poll_delay:
                d, self.poll_delay = self.poll_delay, 0
                time.sleep(d)      # the answer above is already decided: a reap by another thread now goes unseen
            if rc is not None:
                self.reaped = True
            return rc

        def wait(self, timeout=None):
            self.exited = self.reaped = True
            return 0

    def _proc(self, pid, reaped):
        return self.FakeProc(pid, reaped=reaped)

    def _record_signals(self, m, f, action=None, killpg_delay=0):
        sent = []
        orig = (m.os.kill, m.os.killpg, m.EXIT_GRACE_S)

        def killpg(pid, sig):
            time.sleep(killpg_delay)   # a slow signal: a reap not serialised with it lands inside
            sent.append(("killpg", pid, sig, f.proc.reaped))
        m.os.kill = lambda pid, sig: sent.append(("kill", pid, sig))
        m.os.killpg = killpg
        m.EXIT_GRACE_S = 0.1   # the fake never exits on a (recorded, unsent) signal
        try:
            (action or f.kill_tree)()
        finally:
            m.os.kill, m.os.killpg, m.EXIT_GRACE_S = orig
        return sent

    def test_descendants_need_the_recorded_server_start(self):
        # P1 round 4: after the server is reaped its pid may be a new process; its children are never adopted.
        _, f = self._bare_filter()
        old, new = "Tue Nov  3 10:00:00 2026", "Tue Nov  3 11:00:00 2026"
        uid = os.getuid()
        f.proc = self._proc(400, reaped=True)
        f.server_start = old
        rows = [(400, 400, 1, uid, new, "x"), (401, 400, 400, uid, new, "y"), (402, 402, 401, uid, new, "z")]
        f._ps_full = lambda: rows
        self.assertEqual(f.descendants(), {})
        f.server_start = None                      # start unknown: walk disabled, never guessed
        self.assertEqual(f.descendants(), {})
        rows[0] = (400, 400, 1, uid, old, "x")     # the original server: its tree is adopted
        f.server_start = old
        self.assertEqual(f.descendants(), {401: new, 402: new})

    def test_reaped_server_replacement_group_not_signalled(self):
        # P1 round 4: the server is reaped and its pid now leads an unrelated group with a child. Nothing is sent to
        # that group or its members (no group signal after the reap; the walk refuses the recycled root).
        m, f = self._bare_filter()
        old, new = "Tue Nov  3 10:00:00 2026", "Tue Nov  3 11:00:00 2026"
        uid = os.getuid()
        f.state_lock = threading.Lock()
        f.known_tree = {}
        f.proc = self._proc(400, reaped=True)
        f.server_start = old
        f._ps_full = lambda: [(400, 400, 1, uid, new, "x"), (401, 400, 400, uid, new, "y")]
        f._start_of = lambda pid: new
        f.owned_by_task = lambda rows=None: {}
        self.assertEqual(self._record_signals(m, f), [])

    def test_unreaped_server_group_is_signalled(self):
        m, f = self._bare_filter()
        f.state_lock = threading.Lock()
        f.known_tree = {}
        f.proc = self._proc(400, reaped=False)
        f.descendants = lambda: {}
        f._ps_full = lambda: []
        f.owned_by_task = lambda rows=None: {}
        self.assertEqual(self._record_signals(m, f), [("killpg", 400, 15, False), ("killpg", 400, 9, False)])

    def test_concurrent_reap_never_lets_the_group_be_signalled(self):
        # P1 round 5: the server has exited; the closer's poll() says "unreaped" but is slow, and meanwhile another
        # thread reaps it (its pid is free for reuse). With every poll/wait serialised under proc_lock, the reap waits
        # for the whole check-and-signal step, so no group signal is ever sent after the reap.
        m, f = self._bare_filter()
        f.proc = self.FakeProc(400, poll_delay=0.3)
        f.proc.exited = False                     # unreaped and still running when the closer checks ...
        reaper = threading.Thread(target=lambda: (time.sleep(0.1), setattr(f.proc, "exited", True), f._server_poll()))

        def closer():
            reaper.start()
            f._kill_server_group(signal.SIGKILL)  # ... which takes 0.3 s; the reaper runs at 0.1 s
            reaper.join()
            f._kill_server_group(signal.SIGKILL)  # after the reap: nothing
        sent = self._record_signals(m, f, action=closer)
        self.assertTrue(f.proc.reaped)
        self.assertEqual(sent, [("killpg", 400, signal.SIGKILL, False)])

    def test_check_and_group_signal_are_one_locked_step(self):
        # P2 round 6: the delay is inside killpg, not poll, so only a lock held across poll() AND killpg() keeps the
        # reaper (at 0.1 s) out until the signal is sent; a lock around poll() alone would let it reap mid-signal.
        m, f = self._bare_filter()
        f.proc = self.FakeProc(400)
        reaper = threading.Thread(target=lambda: (time.sleep(0.1), setattr(f.proc, "exited", True), f._server_poll()))

        def closer():
            reaper.start()
            f._kill_server_group(signal.SIGKILL)
            reaper.join()
        sent = self._record_signals(m, f, action=closer, killpg_delay=0.3)
        self.assertTrue(f.proc.reaped)
        self.assertEqual(sent, [("killpg", 400, signal.SIGKILL, False)])

    def test_identity_failure_reported_once_gone_pid_silent(self):
        # P2 round 6: an identity lookup that fails for any reason but a gone pid is reported (once per process).
        if sys.platform != "darwin":
            self.skipTest("macOS-only (libproc)")
        m, _ = self._bare_filter()
        p = subprocess.Popen(["/bin/sleep", "0"])
        p.wait()
        orig = (m.LIBPROC, m._IDENTITY_REPORTED)
        err = io.StringIO()
        try:
            m._IDENTITY_REPORTED = False
            with contextlib.redirect_stderr(err):
                self.assertIsNone(m.Filter._identity(p.pid))           # gone: silent
                self.assertEqual(err.getvalue(), "")
                m.LIBPROC = str(self.tmp / "no-such-libproc.dylib")
                self.assertIsNone(m.Filter._identity(os.getpid()))     # library missing: reported ...
                self.assertIsNone(m.Filter._identity(os.getpid()))     # ... once
        finally:
            m.LIBPROC, m._IDENTITY_REPORTED = orig
        self.assertEqual(err.getvalue().count("kernel process identity unavailable"), 1, err.getvalue())
        self.assertIn("not killed by command line", err.getvalue())

    def test_procargs2_parser_keeps_exact_boundaries(self):
        # P1 round 5: argv is read at the aligned start after the exec path, exactly argc entries, never into env.
        m, _ = self._bare_filter()
        T = str(self.task)

        def buf(argv, env=(), path=b"/b/headless_shell", pad=None):
            n = len(path) + 1
            pad = b"\0" * (-(-n // 8) * 8 - n) if pad is None else pad
            return (len(argv).to_bytes(4, sys.byteorder) + path + b"\0" + pad
                    + b"".join(a.encode() + b"\0" for a in argv) + b"".join(e.encode() + b"\0" for e in env))
        ok = ["/b/headless_shell", "--headless", f"--user-data-dir={T}/browser/tmp/p"]
        self.assertEqual(m.parse_procargs2(buf(ok, env=["HOME=/h"])), ok)
        for plen in range(1, 18):   # every padding length
            self.assertEqual(m.parse_procargs2(buf(["a", "b"], path=b"/" + b"x" * plen)), ["a", "b"])
        # The reviewer's case: empty argv[0], the flag only as a substring of an argument, the real flag in the env.
        trick = buf(["", f"--x=a--user-data-dir={T}/browser/tmp/p"], env=[f"--user-data-dir={T}/browser/tmp/p"])
        self.assertIsNone(m.parse_procargs2(trick))
        self.assertEqual(m.parse_procargs2(buf(["a", "", "b"])), ["a", "", "b"])            # empty later entry kept
        self.assertIsNone(m.parse_procargs2(buf(["", "b"])))                                 # empty argv[0]
        self.assertIsNone(m.parse_procargs2(buf(["a", "b"])[:-1]))                           # unterminated
        self.assertIsNone(m.parse_procargs2(buf(["a"], pad=b"\0\0X\0\0\0")))                  # non-NUL padding
        self.assertIsNone(m.parse_procargs2(b"\0\0\0\0" + buf(["a"])[4:]))                  # argc 0
        self.assertIsNone(m.parse_procargs2((1 << 20).to_bytes(4, sys.byteorder) + buf(["a"])[4:]))  # argc huge
        self.assertIsNone(m.parse_procargs2(buf(["a", "b"])[:4] + buf(["a"])[4:]))           # argc > strings
        self.assertIsNone(m.parse_procargs2(b"\1\0\0\0" + b"\0" * 7 + b"a\0"))              # no exec path
        self.assertIsNone(m.parse_procargs2(b"\1\0"))                                        # truncated

    def test_identity_refuses_empty_argv0_with_flag_in_env(self):
        # The same regression against the real kernel buffer: empty argv[0], the flag only in the environment.
        if sys.platform != "darwin":
            self.skipTest("macOS-only (libproc)")
        m, _ = self._bare_filter()
        flag = f"--user-data-dir={self.task}/browser/tmp/p"
        p = subprocess.Popen(["", "-c", "sleep 30; :", f"--x=a{flag}"], executable="/bin/bash", env={"Z": flag})
        try:
            time.sleep(0.3)
            self.assertIsNone(m.Filter._identity(p.pid))
        finally:
            p.kill()
            p.wait()

    def test_start_of_reports_ps_failure(self):
        # P2 round 5: _start_of checks ps's exit: a failure is reported with stderr and means "do not signal".
        m, _ = self._bare_filter()
        fake = self.tmp / "ps"
        orig = m.PS
        m.PS = str(fake)
        try:
            for script, want, msg in [("echo 'ps: boom' >&2; exit 2", None, "ps: boom"),
                                      ("exit 1", None, ""),                           # pid gone: silent
                                      ("echo 'Tue Nov  3 10:00:00 2026'", "Tue Nov 3 10:00:00 2026", "")]:
                fake.write_text("#!/bin/sh\n" + script + "\n")
                fake.chmod(0o755)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(m.Filter._start_of(123), want)
                if msg:
                    self.assertIn("ps exited 2", err.getvalue())
                    self.assertIn(msg, err.getvalue())
                else:
                    self.assertEqual(err.getvalue(), "")
        finally:
            m.PS = orig

    def test_ps_failure_exit_is_reported(self):
        # P2 round 4: a nonzero ps exit is not an empty process table: it is reported with ps's stderr.
        m, _ = self._bare_filter()
        fake = self.tmp / "ps"
        fake.write_text("#!/bin/sh\necho 'ps: boom' >&2\nexit 2\n")
        fake.chmod(0o755)
        orig, m.PS = m.PS, str(fake)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertEqual(m.Filter._ps_full(), [])
        finally:
            m.PS = orig
        self.assertIn("ps exited 2", err.getvalue())
        self.assertIn("ps: boom", err.getvalue())

    def test_recycled_pid_not_signalled(self):
        # P2 round 3: a recorded pid whose start time changed belongs to another process now; neither it nor the
        # group it leads is signalled. Same for an owned-by-command-line pid that changed between ps and kill, and for
        # the server's group once the server is reaped with no member left.
        m, f = self._bare_filter()
        old, new = "Tue Nov  3 10:00:00 2026", "Tue Nov  3 11:00:00 2026"
        f.state_lock = threading.Lock()
        f.known_tree = {500: old, 501: old}

        f.proc = self._proc(400, reaped=True)
        f.descendants = lambda: {}
        f._ps_full = lambda: [(500, 500, 1, os.getuid(), new, "x"), (501, 777, 1, os.getuid(), old, "y")]
        f._start_of = lambda pid: {500: new, 501: old, 600: new}.get(pid)
        f.owned_by_task = lambda rows=None: {600: (600, old)}
        self.assertEqual(self._record_signals(m, f), [("kill", 501, 9)])

    def test_sigterm_while_main_thread_holds_state_lock(self):
        # P2-2: the signal lands while the main thread holds state_lock; the handler must not take it.
        driver = self.tmp / "driver.py"
        driver.write_text(
            "import os, signal, sys, time\n"
            f"sys.path.insert(0, {str(FILTER.parent)!r})\n"
            "import mcp_filter as m\n"
            "orig = m.Filter.on_client\n"
            "def on_client(self, msg):\n"
            "    if msg.get('method') == 'ping':\n"
            "        with self.state_lock:\n"
            "            os.kill(os.getpid(), signal.SIGTERM)\n"
            "            time.sleep(1)\n"
            "    return orig(self, msg)\n"
            "m.Filter.on_client = on_client\n"
            "m.main(sys.argv[1:])\n")
        self.p = subprocess.Popen(
            [sys.executable, "-I", str(driver), "--task", str(self.task), "--args-file", str(self.tmp / "args.json"),
             "--", sys.executable, "-I", str(self.tmp / "stub.py"), str(self.tmp / "tools"), str(self.log)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        try:
            rc = self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("filter deadlocked: SIGTERM while the main thread held state_lock")
        self.assertEqual(rc, 1)
        self.assertIn(b"closing session: signal 15", self._stderr_eof())

    def test_client_eof_ends_session_cleanly(self):
        self.start()
        self.p.stdin.close()
        self.assertEqual(self.p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
