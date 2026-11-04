"""integrations/browser shell scripts (docs/BROWSER-TOOL.md, ticket B1). A stub playwright-mcp in a throwaway prefix
stands in for the real server: it records its env, argv and cwd and answers tools/list, so these tests check the
boundary claims (scrubbed env, fixed flags, proxy only in a 0600 config, fail-closed exits) without npm, node or a
browser. Every test runs on a throwaway CORTADOS_HOME."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER = ROOT / "integrations" / "browser"
INSTALL = BROWSER / "install.sh"
RUN = BROWSER / "run-mcp.sh"
TOOLS = (BROWSER / "tools-0.0.84.txt").read_text()
TOKEN = "abcdefghijklmnop0123"
PROXY = f"http://{TOKEN}@127.0.0.1:40123"

# The stub is /bin/sh so the env it records is exactly what the wrapper passed (the macOS /usr/bin/python3 shim adds
# SDKROOT and friends); the MCP answering is handed to python after recording.
STUB = r'''#!/bin/sh
d="$(dirname "$0")"
[ "$(cat "$d/mode")" = fail ] && { echo "stub: boom" >&2; exit 3; }
env > "$PWD/stub.env"; printf '%s\n' "$@" > "$PWD/stub.argv"; pwd -P > "$PWD/stub.cwd"
exec /usr/bin/python3 "$d/stub.py" "$d/tools"
'''
STUB_PY = r'''import json, os, sys
names = open(sys.argv[1]).read().split()
if open(os.path.join(os.path.dirname(sys.argv[1]), "mode")).read() == "notjson":
    print("NOT JSON", flush=True)     # protocol garbage before an otherwise valid answer
for line in sys.stdin:
    m = json.loads(line)
    if m.get("id") is not None:
        mode = open(os.path.join(os.path.dirname(sys.argv[1]), "mode")).read()
        # mode "schema:<json>": every tool carries that inputSchema value (to test malformed schemas).
        extra = {"inputSchema": json.loads(mode[len("schema:"):])} if mode.startswith("schema:") else {}
        res = ({"tools": [dict({"name": n}, **extra) for n in names]} if m["method"] == "tools/list" else {})
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
'''


class BrowserScriptsTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.home = self.tmp / "home"
        self.prefix = self.home / "browser" / "mcp-0.0.84"
        self.bindir = self.prefix / "node_modules" / ".bin"
        self.task = self.tmp / "task"
        fake = self.tmp / "fakebin"        # a fake node: run-mcp.sh only needs `command -v node`
        fake.mkdir()
        (fake / "node").write_text("#!/bin/sh\nexit 0\n")
        (fake / "node").chmod(0o755)
        # argv recorders: every external command install.sh / run-mcp.sh run through PATH appends its argv to argv.log
        # (NUL-separated, one record per line), then execs the real binary. The proxy token must never appear there.
        self.argv_log = self.tmp / "argv.log"
        for name in ("python3", "grep", "mkdir", "cat", "dirname", "env", "rm", "mktemp"):
            real = shutil.which(name, path="/usr/bin:/bin")
            (fake / name).write_text(f"#!/bin/sh\nprintf '%s\\0' \"$0\" \"$@\" >> '{self.argv_log}'\n"
                                     f"printf '\\n' >> '{self.argv_log}'\nexec '{real}' \"$@\"\n")
            (fake / name).chmod(0o755)
        self.env = {"PATH": f"{fake}:/usr/bin:/bin", "CORTADOS_HOME": str(self.home), "HOME": str(self.tmp)}

    def tearDown(self):
        self._td.cleanup()

    def stub(self, mode="ok", tools=TOOLS):
        self.bindir.mkdir(parents=True, exist_ok=True)
        b = self.bindir / "playwright-mcp"
        b.write_text(STUB)
        b.chmod(0o755)
        (self.bindir / "stub.py").write_text(STUB_PY)
        (self.bindir / "mode").write_text(mode)
        (self.bindir / "tools").write_text(tools)

    def sh(self, script, *args, env=None, input=""):
        return subprocess.run(["sh", str(script), *map(str, args)], env=env or self.env, capture_output=True,
                              text=True, timeout=60, input=input)

    def config(self, proxy=PROXY):
        # The proxy URL goes on stdin: it carries the token, and argv is visible to every local user through ps.
        return self.sh(INSTALL, "config", self.task, input=proxy + "\n" if proxy else "")

    # install.sh config
    def test_config_refuses_without_proxy_or_binary(self):
        self.assertEqual(self.config(proxy="").returncode, 2)          # no proxy, stub absent
        self.assertEqual(self.config().returncode, 2)                  # proxy, no binary
        self.assertIn("install --browsers", self.config().stderr)
        self.stub()
        self.assertEqual(self.config(proxy="").returncode, 2)

    def test_config_refuses_proxy_of_wrong_shape(self):
        self.stub()
        for bad in ["http://127.0.0.1:40123", f"https://{TOKEN}@127.0.0.1:1", f"http://{TOKEN}@10.0.0.1:1",
                    f"http://{TOKEN}@localhost:1", f"http://{TOKEN}:pw@127.0.0.1:1", f"http://{TOKEN}@127.0.0.1:1/x",
                    "http://short@127.0.0.1:1", f"http://{TOKEN}@127.0.0.1:1;bypass"]:
            self.assertEqual(self.config(proxy=bad).returncode, 2, bad)
        self.assertFalse((self.task / "browser" / "config.json").exists())

    def test_config_writes_exact_files_0600_and_token_only_in_config(self):
        self.stub()
        r = self.config()
        self.assertEqual(r.returncode, 0, r.stderr)
        px = {"server": "http://127.0.0.1:40123", "username": TOKEN, "password": ""}
        cfg = self.task / "browser" / "config.json"
        mcp = self.task / "browser-mcp.json"
        self.assertEqual(json.loads(cfg.read_text()), {"browser": {"browserName": "chromium", "isolated": True,
            "launchOptions": {"headless": True, "chromiumSandbox": True, "proxy": px,
                "args": ["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]},
            "contextOptions": {"proxy": px}}})
        server = json.loads(mcp.read_text())["mcpServers"]["browser"]
        self.assertTrue(os.path.isabs(server["command"]) and os.access(server["command"], os.X_OK), server)
        self.assertEqual(json.loads(mcp.read_text()), {"mcpServers": {"browser": {
            "command": server["command"], "args": ["-I", str(BROWSER / "mcp_filter.py"), "--task", str(self.task),
                                           "--browsers-path", f"{self.prefix}/browsers", "--",
                                           str(RUN), str(self.task), str(self.prefix)]}}})
        for f in (cfg, mcp):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f)
        self.assertNotIn(TOKEN, mcp.read_text())

    def test_config_takes_proxy_on_stdin_never_argv(self):
        self.stub()
        r = self.config()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((self.task / "browser" / "config.json").read_text())
                         ["browser"]["launchOptions"]["proxy"]["username"], TOKEN)
        log = self.argv_log.read_text()
        self.assertIn("python3", log)              # the recorders saw the config writer's python
        self.assertNotIn(TOKEN, log)
        # The old argv form is refused, not silently accepted or ignored.
        r = self.sh(INSTALL, "config", self.task, PROXY)
        self.assertEqual(r.returncode, 2)
        self.assertIn("stdin", r.stderr)

    # run-mcp.sh
    def test_wrapper_scrubs_env_and_passes_fixed_flags(self):
        self.stub()
        self.assertEqual(self.config().returncode, 0)
        poisoned = dict(self.env, PLAYWRIGHT_MCP_PROXY_SERVER="http://evil:1", PLAYWRIGHT_MCP_PROXY_BYPASS="*",
                        PLAYWRIGHT_MCP_CDP_ENDPOINT="ws://x", PLAYWRIGHT_MCP_PORT="9", PLAYWRIGHT_MCP_EXTENSION="1",
                        PLAYWRIGHT_MCP_ALLOW_UNRESTRICTED_FILE_ACCESS="1", NODE_OPTIONS="--require /x",
                        PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK="1", HTTP_PROXY="http://x:1",
                        NO_PROXY="*")
        r = subprocess.run(["sh", str(RUN), str(self.task), str(self.prefix)], env=poisoned, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        b = self.task / "browser"
        env = dict(l.split("=", 1) for l in (b / "stub.env").read_text().splitlines())
        seen = {"argv": (b / "stub.argv").read_text().splitlines(), "cwd": (b / "stub.cwd").read_text().strip()}
        # PWD, SHLVL, _ and OLDPWD are set by the stub's own /bin/sh, not inherited.
        self.assertEqual({k: v for k, v in env.items() if k not in ("PWD", "SHLVL", "_", "OLDPWD")},
                         {"PATH": f"{self.tmp / 'fakebin'}:/usr/bin:/bin", "HOME": f"{b}/home", "TMPDIR": f"{b}/tmp",
                          "PLAYWRIGHT_BROWSERS_PATH": f"{self.prefix}/browsers"})
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(b))
        blocked = ("http://localhost:*;https://localhost:*;http://127.0.0.1:*;https://127.0.0.1:*;http://[::1]:*;"
                   "https://[::1]:*;http://0.0.0.0:*;https://0.0.0.0:*")
        self.assertEqual(seen["argv"], ["--config", f"{b}/config.json", "--headless", "--isolated", "--sandbox",
                                        "--no-webmcp", "--ignore-roots", "--browser", "chromium",
                                        "--blocked-origins", blocked, "--output-dir", f"{b}/out",
                                        "--block-service-workers", "--timeout-navigation", "30000",
                                        "--timeout-action", "10000"])
        self.assertNotIn(TOKEN, " ".join(seen["argv"]))
        self.assertNotIn(TOKEN, self.argv_log.read_text())

    def test_wrapper_fails_closed(self):
        run = lambda: subprocess.run(["sh", str(RUN), str(self.task), str(self.prefix)], env=self.env,
                                     stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual(run().returncode, 2)                          # no binary
        self.stub()
        self.assertEqual(run().returncode, 2)                          # no config
        self.assertEqual(self.config().returncode, 0)
        cfg = self.task / "browser" / "config.json"
        good = json.loads(cfg.read_text())
        for mutate in (lambda c: c["browser"]["launchOptions"].pop("proxy"),
                       lambda c: c["browser"].pop("contextOptions"),
                       lambda c: c["browser"]["contextOptions"]["proxy"].update(bypass="*"),
                       lambda c: c["browser"]["launchOptions"]["proxy"].update(server="http://10.0.0.1:1")):
            c = json.loads(json.dumps(good))
            mutate(c)
            cfg.write_text(json.dumps(c))
            r = run()
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertFalse((self.task / "browser" / "stub.argv").exists())

    # install.sh tools (the install gate's tool listing)
    def test_tool_listing_goes_through_the_wrapper(self):
        self.stub()
        r = self.sh(INSTALL, "tools")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "\n".join(sorted(TOOLS.split())) + "\n")

    def test_args_listing_is_per_tool_schema_keys(self):
        # The stub's tools carry no inputSchema, so every key set is empty; the format is args-0.0.84.json's.
        self.stub()
        r = self.sh(INSTALL, "args")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {n: [] for n in TOOLS.split()})
        pinned = json.loads((BROWSER / "args-0.0.84.json").read_text())
        self.assertEqual(sorted(pinned), sorted(TOOLS.split()))   # the committed allowlist covers every pinned tool

    def test_tool_listing_failure_shows_server_stderr(self):
        self.stub(mode="fail")
        r = self.sh(INSTALL, "tools")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stub: boom", r.stderr)

    def test_tool_listing_fails_on_malformed_protocol_output(self):
        self.stub(mode="notjson")
        r = self.sh(INSTALL, "tools")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIn("malformed MCP protocol output", r.stderr)
        self.assertIn("NOT JSON", r.stderr)

    def test_tool_listing_fails_on_non_object_input_schema(self):
        # A present inputSchema / properties that is not an object is reported as malformed output at once: no
        # truthiness coercion, so falsy values ([], "", false, null) never pass as "no arguments". (A non-object used
        # to kill the reader thread with an uncaught AttributeError: a 30 s wait blaming a missing answer.)
        bad = ['"x"', "[]", '""', "false", "null", "0", "[1]",
               '{"properties": []}', '{"properties": ""}', '{"properties": false}', '{"properties": null}',
               '{"properties": "x"}']
        for schema in bad:
            with self.subTest(schema=schema):
                self.stub(mode="schema:" + schema)
                t0 = time.monotonic()
                r = self.sh(INSTALL, "args")
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertEqual(r.stdout, "")
                self.assertIn("malformed MCP protocol output", r.stderr)
                self.assertIn("inputSchema of", r.stderr)
                self.assertNotIn("within 30 s", r.stderr)
                self.assertLess(time.monotonic() - t0, 25)

    def test_args_listing_defaults_only_absent_fields(self):
        # Absent inputSchema (the default stub) or absent / empty-object properties: no arguments, exit 0.
        for schema in ['{}', '{"type": "object"}', '{"properties": {}}']:
            with self.subTest(schema=schema):
                self.stub(mode="schema:" + schema)
                r = self.sh(INSTALL, "args")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), {n: [] for n in TOOLS.split()})
        self.stub(mode='schema:{"properties": {"url": {}, "a": {}}}')
        r = self.sh(INSTALL, "args")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {n: ["a", "url"] for n in TOOLS.split()})


if __name__ == "__main__":
    unittest.main()
