"""integrations/browser/mcp_filter.py (docs/BROWSER-TOOL.md, ticket B1b). The filter runs as a subprocess in front of a
stub MCP server (stdlib python over pipes) that records every message it receives, so each test checks both what the
client sees and that refused requests never reached the server. No node, npm or browser."""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILTER = ROOT / "integrations" / "browser" / "mcp_filter.py"
TOOLS = (ROOT / "integrations" / "browser" / "tools-0.0.84.txt").read_text().split()

STUB = r'''import json, sys
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
        self.log = self.tmp / "server.log"
        self.log.write_text("")
        self.p = None

    def tearDown(self):
        if self.p and self.p.poll() is None:
            self.p.kill()
            self.p.wait()
        if self.p:
            for f in (self.p.stdin, self.p.stdout, self.p.stderr):
                f.close()
        self._td.cleanup()

    def start(self, *extra):
        self.p = subprocess.Popen(
            [sys.executable, "-I", str(FILTER), "--task", str(self.task), *extra, "--",
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
                return None
            r, _, _ = select.select([self.p.stdout], [], [], left)
            if not r:
                return None
            chunk = os.read(self.p.stdout.fileno(), 65536)
            if not chunk:
                return None
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
        for i, bad in enumerate([["/etc/passwd"], [ok, "../../../x"], [str(self.task / "browser" / "in")], [5]]):
            self.assert_refused(self.call(10 + i, "browser_file_upload", paths=bad), 10 + i, -32004)
        self.assert_refused(self.call(20, "browser_drop", path="/etc/hosts"), 20, -32004)
        self.assertEqual(self.calls_seen(), ["browser_file_upload"])

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

    def test_client_eof_ends_session_cleanly(self):
        self.start()
        self.p.stdin.close()
        self.assertEqual(self.p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
