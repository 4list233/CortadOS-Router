"""Stdio allowlist filter between `claude` and the pinned playwright-mcp (docs/BROWSER-TOOL.md, ticket B1b).

    python3 -I mcp_filter.py --task <task_dir> [--grant browser_js] [--grant browser_upload] [--max-pages N]
        -- <server command...>          (normally: run-mcp.sh <task_dir> <prefix>)

The filter starts the server as a child over pipes and is the only thing Claude Code talks to. It is the server-side
tool boundary that holds even if Claude Code's --disallowedTools is misconfigured, so everything fails closed:
- tools/list answers are rewritten to the allowed names; a tools/call for any other name is refused, never forwarded;
- browser_navigate / browser_tabs URLs must be http:, https: or exactly about:blank (item 7c: javascript:/data:
  navigations run script exactly as the denied browser_evaluate would);
- pages opened through the filter (navigate, navigate_back, new tab) are counted and refused past the cap;
- only the listed client methods pass; batches, bad JSON, non-objects and oversize lines close the session;
- server->client requests (roots/list, sampling/*, elicitation/*, any other) are answered here with an error and never
  reach the client (item 7d: roots would widen the server's file boundary to the child's repo).
Stdlib only. Refusals are JSON-RPC errors with fixed reason text; nothing from a refused request is echoed back.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_FILE = os.path.join(HERE, "tools-0.0.84.txt")

# Default-off tools (items 5, 6): page JS, host code execution, local-file posting. Grants add some back.
DEFAULT_OFF = frozenset({"browser_evaluate", "browser_run_code_unsafe", "browser_file_upload", "browser_drop"})
GRANTS = {"browser_js": frozenset({"browser_evaluate"}),
          "browser_upload": frozenset({"browser_file_upload", "browser_drop"})}
# Host code execution (vm.runInContext in the server process): never grantable, whatever the configuration says.
NEVER = frozenset({"browser_run_code_unsafe"})
UPLOAD_TOOLS = frozenset({"browser_file_upload", "browser_drop"})
# Argument keys that carry local paths in the upload/drop tools; every one must resolve under <task>/browser/in.
PATH_KEYS = ("path", "paths", "filePath", "filePaths", "files")
PAGE_TOOLS = frozenset({"browser_navigate", "browser_navigate_back"})

CLIENT_METHODS = frozenset({"initialize", "notifications/initialized", "ping", "tools/list", "tools/call",
                            "notifications/cancelled"})
# Server notifications the client may see; everything else (resources/prompts changes, ...) is dropped.
SERVER_NOTIFICATIONS = frozenset({"notifications/progress", "notifications/message",
                                  "notifications/tools/list_changed"})

# Default page cap (docs "Caps": browser_max_pages default 50): bounds how much a child can browse per task.
DEFAULT_MAX_PAGES = 50
# Client lines are tool calls with small arguments (URLs, form text); 1 MiB is far above any real one and keeps a
# runaway client from making the filter buffer without bound.
CLIENT_LINE_MAX = 1 << 20
# Server lines carry base64 screenshots in tool results (a full-page PNG can be several MB); 64 MiB bounds memory while
# leaving room for those.
SERVER_LINE_MAX = 64 << 20
# Grace for the server to exit after the client closes stdin before it is killed: long enough for a clean browser
# shutdown, short enough that a hung server never outlives the child session for long.
EXIT_GRACE_S = 5
# The filter's own request ids (re-list after list_changed) carry this prefix; client ids with it are refused so the
# two can never collide.
OWN_ID_PREFIX = "cortados-filter-"

# Fixed refusal reasons (never echo request content back).
E_METHOD = (-32601, "method not allowed by the CortadOS browser filter")
E_PARAMS = (-32602, "invalid tools/call params refused by the CortadOS browser filter")
E_TOOL = (-32001, "tool not allowed by the CortadOS browser filter")
E_URL = (-32002, "URL refused by the CortadOS browser filter: only http:, https: and about:blank")
E_PAGES = (-32003, "page cap reached: the CortadOS browser filter refuses further navigation")
E_PATH = (-32004, "path refused by the CortadOS browser filter: upload paths must be under <task>/browser/in")
E_ID = (-32600, "invalid request id refused by the CortadOS browser filter")
E_SERVER_REQ = (-32601, "server-to-client requests are not supported by the CortadOS browser filter")
E_LIST = (-32603, "malformed tools/list answer from the browser server")


def pinned_tools(path=TOOLS_FILE):
    with open(path) as f:
        names = f.read().split()
    if not names:
        raise SystemExit(f"mcp_filter: {path} is empty; refusing to run without a pinned tool list")
    return frozenset(names)


def allowed_tools(pinned, grants):
    """Pinned list minus default-off, plus grants (only names the pinned server has), minus NEVER."""
    allowed = set(pinned) - DEFAULT_OFF
    for g in grants:
        if g not in GRANTS:
            raise SystemExit(f"mcp_filter: unknown grant {g!r}")
        allowed |= GRANTS[g] & pinned
    return frozenset(allowed - NEVER)


def url_ok(url):
    """Allowlist, not a deny list: exactly about:blank, or an http(s) URL with a host. The scheme must be lowercase and
    first (no leading whitespace or control characters: browsers strip those, so ' javascript:' or 'JavaScript:' would
    otherwise reach the page)."""
    if not isinstance(url, str):
        return False
    if url == "about:blank":
        return True
    if any(ord(c) <= 0x20 or ord(c) == 0x7f for c in url):
        return False
    if not (url.startswith("http://") or url.startswith("https://")):
        return False
    try:
        u = urlsplit(url)
        host = u.hostname
        u.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(host)


class Filter:
    def __init__(self, server_cmd, task_dir, allowed, max_pages, out=None):
        self.allowed = allowed
        self.max_pages = max_pages
        self.pages = 0
        self.in_dir = os.path.realpath(os.path.join(task_dir, "browser", "in"))
        self.out = out or sys.stdout.buffer
        self.out_lock = threading.Lock()
        self.srv_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.pending = {}          # forwarded client request id (json key) -> method
        self.own_seq = 0
        self.closing = False
        # New session: closing the filter kills the whole tree (wrapper, node, browser), not just the first process.
        self.proc = subprocess.Popen(server_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     start_new_session=True)

    # output helpers
    def to_client(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        with self.out_lock:
            self.out.write(data)
            self.out.flush()

    def to_server(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        with self.srv_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def error(self, mid, err, send=None):
        (send or self.to_client)({"jsonrpc": "2.0", "id": mid, "error": {"code": err[0], "message": err[1]}})

    def close(self, reason, code=1):
        with self.state_lock:
            if self.closing:
                return
            self.closing = True
        sys.stderr.write(f"mcp_filter: closing session: {reason}\n")
        sys.stderr.flush()
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the server already exited; nothing left to kill
        os._exit(code)

    # client -> server
    def on_client(self, msg):
        method = msg.get("method")
        if method is None:
            # A response from the client. The filter never forwards server requests, so there is nothing it may
            # answer; dropped.
            return
        is_request = "id" in msg
        mid = msg.get("id")
        if not isinstance(method, str) or method not in CLIENT_METHODS:
            if is_request:
                self.error(mid if self._id_ok(mid) else None, E_METHOD)
            return
        if not is_request:
            if method.startswith("notifications/"):
                self.to_server(msg)
            return
        if not self._id_ok(mid):
            self.error(None, E_ID)
            return
        if method == "tools/call":
            err = self.check_call(msg.get("params"), missing="params" not in msg)
            if err:
                self.error(mid, err)
                return
        elif method.startswith("notifications/"):
            self.error(mid, E_METHOD)  # a notification method sent with an id is not a request we know
            return
        with self.state_lock:
            self.pending[json.dumps(mid)] = method
        self.to_server(msg)

    @staticmethod
    def _id_ok(mid):
        if isinstance(mid, bool):
            return False
        if isinstance(mid, int):
            return True
        return isinstance(mid, str) and not mid.startswith(OWN_ID_PREFIX)

    def check_call(self, params, missing=False):
        if missing or not isinstance(params, dict):
            return E_PARAMS
        name = params.get("name")
        args = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            return E_PARAMS
        if name not in self.allowed:
            return E_TOOL
        opens_page = name in PAGE_TOOLS
        if name == "browser_navigate":
            if not url_ok(args.get("url")):
                return E_URL
        if name == "browser_tabs":
            if "url" in args and not url_ok(args["url"]):
                return E_URL
            opens_page = args.get("action") == "new"
        if name in UPLOAD_TOOLS and not self.paths_ok(args):
            return E_PATH
        if opens_page:
            with self.state_lock:
                if self.pages >= self.max_pages:
                    return E_PAGES
                self.pages += 1
        return None

    def paths_ok(self, args):
        for key in PATH_KEYS:
            if key not in args:
                continue
            vals = args[key] if isinstance(args[key], list) else [args[key]]
            for v in vals:
                if not isinstance(v, str) or not v:
                    return False
                real = os.path.realpath(v if os.path.isabs(v) else os.path.join(self.in_dir, v))
                if not real.startswith(self.in_dir + os.sep):
                    return False
        return True

    # server -> client
    def on_server(self, msg):
        method = msg.get("method")
        if method is not None:
            if "id" in msg:
                # roots/list, sampling/*, elicitation/*, anything else: answered here, never forwarded.
                self.error(msg.get("id"), E_SERVER_REQ, send=self.to_server)
                return
            if method not in SERVER_NOTIFICATIONS:
                return
            if method == "notifications/tools/list_changed":
                self.relist()
            self.to_client(msg)
            return
        key = json.dumps(msg.get("id"))
        if isinstance(msg.get("id"), str) and msg["id"].startswith(OWN_ID_PREFIX):
            self.on_relist(msg)
            return
        with self.state_lock:
            req = self.pending.pop(key, None)
        if req is None:
            return  # an answer to nothing the client asked: dropped
        if req == "tools/list" and "result" in msg:
            tools = msg["result"].get("tools") if isinstance(msg["result"], dict) else None
            if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
                self.error(msg.get("id"), E_LIST)
                return
            msg["result"]["tools"] = [t for t in tools if t.get("name") in self.allowed]
        self.to_client(msg)

    def relist(self):
        with self.state_lock:
            self.own_seq += 1
            mid = f"{OWN_ID_PREFIX}{self.own_seq}"
        self.to_server({"jsonrpc": "2.0", "id": mid, "method": "tools/list"})

    def on_relist(self, msg):
        # The allowlist is by name, so the client's own next tools/list is filtered as before; the re-list records
        # tools the server added that the pinned list does not know (a version drift the install gate should catch).
        tools = (msg.get("result") or {}).get("tools") if isinstance(msg.get("result"), dict) else None
        if not isinstance(tools, list):
            sys.stderr.write("mcp_filter: re-list after list_changed failed\n")
            return
        hidden = sorted(str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name") not in self.allowed)
        if hidden:
            sys.stderr.write(f"mcp_filter: hidden after list_changed: {', '.join(hidden)}\n")

    # loops
    def read_lines(self, stream, limit, side):
        while True:
            line = stream.readline(limit + 1)
            if not line:
                return
            if len(line) > limit:
                self.close(f"{side} line over {limit} bytes")
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.close(f"invalid JSON from {side}")
            if isinstance(msg, list):
                self.close(f"batch array from {side} (not supported)")
            if not isinstance(msg, dict):
                self.close(f"non-object message from {side}")
            yield msg

    def server_loop(self):
        try:
            for msg in self.read_lines(self.proc.stdout, SERVER_LINE_MAX, "server"):
                self.on_server(msg)
        except (BrokenPipeError, OSError) as e:
            self.close(f"pipe error: {e}")
        self.close("server closed its output", code=self.proc.poll() or 0)

    def run(self, stdin=None):
        threading.Thread(target=self.server_loop, daemon=True).start()
        try:
            for msg in self.read_lines(stdin or sys.stdin.buffer, CLIENT_LINE_MAX, "client"):
                self.on_client(msg)
        except (BrokenPipeError, OSError) as e:
            self.close(f"pipe error: {e}")
        # Client closed stdin: let the server finish, then make sure nothing of it survives.
        try:
            self.proc.stdin.close()
        except OSError as e:
            sys.stderr.write(f"mcp_filter: closing server stdin: {e}\n")
        try:
            rc = self.proc.wait(timeout=EXIT_GRACE_S)
        except subprocess.TimeoutExpired:
            rc = 0
        self.close("client closed its input", code=rc if rc and rc > 0 else 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="mcp_filter.py")
    p.add_argument("--task", required=True)
    p.add_argument("--grant", action="append", default=[], choices=sorted(GRANTS))
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument("--tools-file", default=TOOLS_FILE)
    p.add_argument("server", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    cmd = a.server[1:] if a.server[:1] == ["--"] else a.server
    if not cmd:
        p.error("server command required after --")
    if a.max_pages < 0:
        p.error("--max-pages must be >= 0")
    Filter(cmd, a.task, allowed_tools(pinned_tools(a.tools_file), a.grant), a.max_pages).run()


if __name__ == "__main__":
    main()
