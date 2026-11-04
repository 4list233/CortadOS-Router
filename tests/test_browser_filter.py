"""integrations/browser/mcp_filter.py (docs/BROWSER-TOOL.md, ticket B1b). The filter runs as a subprocess in front of a
stub MCP server (stdlib python over pipes) that records every message it receives, so each test checks both what the
client sees and that refused requests never reached the server. No node, npm or browser."""
import json
import os
import select
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
            # Chromium-like grandchild named by the task's browser dir (its --user-data-dir lives under TMPDIR), made an
            # orphan at once (double fork) so no descendant walk can ever see it; then the server dies abruptly.
            prof = open(sys.argv[2] + ".task").read() + "/browser/tmp/playwright_chromiumdev_profile-x"
            code = ("import subprocess, sys; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', "
                    "'--user-data-dir=' + sys.argv[1]], start_new_session=True, stdin=subprocess.DEVNULL, "
                    "stdout=subprocess.DEVNULL); open(sys.argv[2], 'w').write(str(g.pid))")
            subprocess.run([sys.executable, "-c", code, prof, sys.argv[2] + ".pid"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL)
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
            [sys.executable, "-I", str(FILTER), "--task", str(self.task), *af, *extra, "--",
             sys.executable, "-I", str(self.tmp / "stub.py"), str(self.tmp / "tools"), str(self.log)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.buf = b""

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
        # command-line ownership (the task's browser dir) can find it.
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

    def test_owned_by_task_matches_path_component_only(self):
        sys.path.insert(0, str(FILTER.parent))
        try:
            import mcp_filter
        finally:
            sys.path.remove(str(FILTER.parent))
        f = mcp_filter.Filter.__new__(mcp_filter.Filter)
        f.markers = ["/t/task1/browser"]
        uid = os.getuid()
        rows = [(10, 10, 1, uid, "chrome --user-data-dir=/t/task1/browser/tmp/p"),
                (11, 10, 10, uid, "x /t/task1/browser"),
                (12, 12, 1, uid, "chrome --user-data-dir=/t/task1/browserX/tmp"),
                (13, 13, 1, uid, "chrome --user-data-dir=/t/task10/browser/tmp"),
                (14, 14, 1, uid + 1, "chrome /t/task1/browser/tmp"),
                (os.getpid(), 1, 1, uid, "self /t/task1/browser/tmp")]
        self.assertEqual(f.owned_by_task(rows), {10: 10, 11: 10})

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
