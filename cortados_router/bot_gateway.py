"""CortadOS bot gateway (phase 1): MCP "streamable HTTP" JSON-RPC on POST /mcp for Grok Bots.

Owner decisions (docs/GROKBOT-BRIDGE.md): ONE shared bearer token for every Grok Bot; all of them are attributed as the
single client "grokbot"; usage is tracked in $CORTADOS_HOME/bot-usage.jsonl; Chrome (login mode) is never allowed.

It reuses the cortados-route tool implementations (subagent_mcp.call_child_tool / route_task) and exposes only six
tools. Policy is enforced here BEFORE a tool runs: chrome refused, cwd confined to --workspace-root, a client only sees
the tasks it started, and caps (concurrency, tasks/day, route_task/day). Stdlib only.

Run: python3 -B -m cortados_router.bot_gateway --token-file F --workspace-root DIR [--port 8434] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cortados_router import paths, subagent_mcp

CLIENT = "grokbot"                       # fixed: one shared token means one attributable client (owner decision)
EXPOSED = ("start_task", "wait_task", "task_status", "list_tasks", "cancel_task", "route_task")
USAGE_LOG = "bot-usage.jsonl"
# Reason: the usage log rotates (below), so two rotations in one day can drop today's records; the caps and ownership
# live in this small file instead (rewritten atomically on every change), and the usage log stays the audit trail.
COUNTERS_FILE = "bot-counters.json"
# Reason: the owned-id list grows by at most max_tasks_per_day; 10000 is about a year at the default cap, and a task
# older than that is past any status interest. The oldest ids (they sort by start time) are dropped first.
MAX_OWNED = 10000
# Reason: a bot gets answers no longer than the owner's own route_task default; a larger max_tokens would let one
# unattended call spend more subscription quota than any interactive one.
MAX_ROUTE_TOKENS = subagent_mcp.DEFAULT_MAX_TOKENS
# Reason: absolute paths in the owner's home (task dir, log files) tell a remote bot nothing it can use and leak the
# machine's layout; they are stripped from every child result, and cwd is shown relative to the workspace root.
PATH_FIELDS = ("dir", "stdout_log", "stderr_log")
REASON_CHARS = 200                        # a refusal reason is one sentence; longer text is not worth logging per call
NAME_CHARS = 64                           # tool names are short identifiers; a longer one is junk echoed into the log
# Reason: the usage log is appended on every call by an unattended client; bound it so a chatty bot cannot fill the
# disk. Past the limit the file is rotated once to .1 (the previous .1 is dropped), keeping roughly 2x this on disk.
USAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_BODY = 1024 * 1024                    # a JSON-RPC request larger than this is not a tool call; refuse it
DEFAULTS = {"max_concurrent": 2,          # bounds unattended subscription spend running at once (tasks and routes each)
            "max_tasks_per_day": 30,      # bounds child sessions per UTC day
            "max_routes_per_day": 200}    # route_task is cheaper (one text answer), so a higher daily bound
POLICY_ERROR = -32001                     # JSON-RPC server-defined error code for a policy refusal
FINAL_STATES = ("succeeded", "failed", "cancelled")
ROUTED = (None, "", "cortados")           # GROKBOT-BRIDGE.md 3.3: bots are routed only, never sub/ or api/ pins
# Reason: children run as the owner's user until the per-user boundary (phase 2 B5) ships, so a bot may only get a
# read-only planning child; any other mode (or a pre-approved tool list) would hand a remote bot the owner's shell.
ALLOWED_PERMISSION_MODES = (None, "", "plan")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
MAX_BATCH = 20                            # MCP batches are a handful of calls; a longer list is not a real client
MAX_INFLIGHT = 16                         # bounds handler threads doing work at once; past it the server answers 503
REQUEST_TIMEOUT_S = 30                    # a bot sends its JSON-RPC at once or is gone; a slow sender holds a thread
_PENDING = "__pending__"                  # placeholder prefix reserving a concurrency slot while a child starts


class GatewayError(Exception):
    pass


def read_token_file(path: str) -> str:
    """The shared token. The file must be a regular file owned by us with mode 0600 (no group/other bits): a readable
    token file would let any local user act as every bot."""
    if not path:
        raise GatewayError("no token file: pass --token-file or set CORTADOS_BOT_TOKEN_FILE")
    # Open first, then check the open descriptor (fstat): a stat-then-open check could be raced by swapping the file.
    # O_NOFOLLOW refuses a symlink; O_NONBLOCK keeps a FIFO from blocking the open before the regular-file check.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise GatewayError(f"cannot open the token file ({type(exc).__name__}; a symlink is refused)") from None
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise GatewayError("the token file must be a regular file (not a symlink)")
        if st.st_mode & 0o077:
            raise GatewayError(f"the token file mode is {oct(st.st_mode & 0o777)}; it must be 0600")
        if st.st_uid != os.getuid():
            raise GatewayError("the token file must be owned by the user running the gateway")
        tok = f.read().strip()
    if len(tok) < subagent_mcp.MIN_TOKEN_LEN or not (tok.isascii() and tok.isprintable()):
        raise GatewayError(f"the token must be printable ASCII of at least {subagent_mcp.MIN_TOKEN_LEN} characters")
    return tok


def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class Gateway:
    """Policy + accounting. One instance per server; thread-safe."""

    def __init__(self, token: str, workspace_root: str, cfg: dict, limits: dict | None = None,
                 child=None, route=None):
        if not workspace_root:
            raise GatewayError("no workspace root: pass --workspace-root (the gateway refuses to run tasks anywhere)")
        root = os.path.realpath(workspace_root)
        if not os.path.isdir(root):
            raise GatewayError("the workspace root is not an existing directory")
        self.token, self.root, self.cfg = token, root, cfg
        self.limits = {**DEFAULTS, **(limits or {})}
        self.child = child or subagent_mcp.call_child_tool
        self.route = route or subagent_mcp.route_task
        self.lock = threading.Lock()
        self.tasks: dict = {}             # task_id -> client that started it
        self.running: set = set()         # task ids not yet seen in a final state (+ pending start placeholders)
        self.routes_inflight = 0          # route_task calls currently waiting on the provider
        self.counts: dict = {}            # (day, kind) -> n  kind: tasks | routes | refused
        self._load_owned()
        # After a restart, every owned task may still be running; _refresh_running prunes the finished ones.
        self.running.update(t for t, c in self.tasks.items() if c == CLIENT)

    # -- auth ------------------------------------------------------------------------------------------------------
    def authorized(self, header) -> bool:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.token.encode())

    # -- accounting ------------------------------------------------------------------------------------------------
    def usage_path(self) -> str:
        return str(paths.state_dir() / USAGE_LOG)

    def record(self, tool: str, outcome: str, task_id=None, reason=None) -> None:
        rec = {"ts": round(time.time(), 3), "client": CLIENT, "tool": tool, "task_id": task_id, "outcome": outcome}
        if reason:
            rec["reason"] = reason[:REASON_CHARS]
        p = self.usage_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            try:
                if os.path.getsize(p) > USAGE_MAX_BYTES:
                    os.replace(p, p + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError as exc:
            # Not silent: a usage-log failure is reported on stderr (no token in it), and the call still answers.
            sys.stderr.write(f"bot-gateway: could not append the usage log ({type(exc).__name__})\n")

    def counters_path(self) -> str:
        return str(paths.state_dir() / COUNTERS_FILE)

    def _load_owned(self) -> None:
        """Rebuild ownership and today's counters from bot-counters.json and the usage log (whichever saw more), so a
        restart neither orphans a bot's tasks nor resets its daily caps, even after the usage log has rotated."""
        today = _day()
        skipped = 0
        log_counts: dict = {}
        for p in (self.usage_path() + ".1", self.usage_path()):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except (ValueError, RecursionError):
                            r = None
                        ts = r.get("ts") if isinstance(r, dict) else None
                        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                            skipped += 1      # not a record this gateway wrote (non-object, or no numeric ts)
                            continue
                        tid = r.get("task_id")
                        if r.get("tool") == "start_task" and r.get("outcome") == "ok" and isinstance(tid, str) and tid:
                            self.tasks[tid] = r.get("client", CLIENT)
                        try:
                            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
                        except (OverflowError, OSError, ValueError):
                            skipped += 1      # a ts outside the platform's time range
                            continue
                        if day == today:
                            kind = {"start_task": "tasks", "route_task": "routes"}.get(r.get("tool"))
                            if r.get("outcome") == "ok" and kind:
                                log_counts[kind] = log_counts.get(kind, 0) + 1
                            elif r.get("outcome") == "refused":
                                log_counts["refused"] = log_counts.get("refused", 0) + 1
            except FileNotFoundError:
                continue
        if skipped:
            sys.stderr.write(f"bot-gateway: skipped {skipped} unreadable line(s) in the usage log\n")
        saved = self._read_counters()
        for t in saved.get("owned", []):
            self.tasks.setdefault(t, CLIENT)
        file_counts = saved.get("counts", {}) if saved.get("day") == today else {}
        for kind in ("tasks", "routes", "refused"):
            n = max(log_counts.get(kind, 0), file_counts.get(kind, 0))
            if n:
                self.counts[(today, kind)] = n

    def _read_counters(self) -> dict:
        p = self.counters_path()
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, RecursionError) as exc:
            # Not silent: the usage log still rebuilds what it holds; the operator is told the file was unreadable.
            sys.stderr.write(f"bot-gateway: ignoring unreadable {COUNTERS_FILE} ({type(exc).__name__})\n")
            return {}
        if not isinstance(d, dict):
            sys.stderr.write(f"bot-gateway: ignoring {COUNTERS_FILE}: not a JSON object\n")
            return {}
        counts = d.get("counts") if isinstance(d.get("counts"), dict) else {}
        owned = d.get("owned") if isinstance(d.get("owned"), list) else []
        return {"day": d.get("day"),
                "counts": {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)},
                "owned": [t for t in owned if isinstance(t, str) and t]}

    def _save_counters(self) -> None:
        """Called with self.lock held. Temp file + atomic rename, so a crash never leaves half a file."""
        today = _day()
        owned = sorted(t for t, c in self.tasks.items() if c == CLIENT)[-MAX_OWNED:]
        obj = {"day": today, "counts": {k: n for (d, k), n in self.counts.items() if d == today}, "owned": owned}
        p = self.counters_path()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="." + COUNTERS_FILE + ".", suffix=".tmp", dir=os.path.dirname(p))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(obj, f, sort_keys=True)
                os.replace(tmp, p)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError as exc:
            sys.stderr.write(f"bot-gateway: could not write {COUNTERS_FILE} ({type(exc).__name__})\n")

    def _bump(self, kind: str) -> int:
        """Called with self.lock held."""
        k = (_day(), kind)
        self.counts[k] = self.counts.get(k, 0) + 1
        self._save_counters()
        return self.counts[k]

    def count(self, kind: str) -> int:
        return self.counts.get((_day(), kind), 0)

    # -- policy ----------------------------------------------------------------------------------------------------
    def _refuse(self, tool: str, reason: str, task_id=None):
        with self.lock:
            self._bump("refused")
        self.record(tool, "refused", task_id, reason)
        raise PolicyRefusal(reason)

    def _inside_root(self, cwd) -> bool:
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return False
        real = os.path.realpath(cwd)
        return real == self.root or real.startswith(self.root + os.sep)

    def _refresh_running(self) -> None:
        """Drop finished tasks from self.running. Called WITHOUT self.lock held: task_status may take seconds."""
        with self.lock:
            ids = [t for t in self.running if not t.startswith(_PENDING)]
        done = []
        for tid in ids:
            res = self.child("task_status", {"task_id": tid}, self.cfg)
            if res.get("isError"):
                # Only a task the store no longer has is gone; any other error (a transient read failure) keeps it
                # counted, so a flaky status probe cannot open a concurrency slot the task still holds.
                if "no such task" in _text(res):
                    done.append(tid)
                continue
            try:
                state = json.loads(_text(res)).get("state")
            except (ValueError, AttributeError, RecursionError):
                state = None
            if state in FINAL_STATES:
                done.append(tid)
        with self.lock:
            self.running.difference_update(done)

    def call(self, name: str, a: dict) -> dict:
        if name not in EXPOSED:
            self._refuse(str(name)[:NAME_CHARS], f"tool {str(name)[:NAME_CHARS]!r} is not offered by the bot gateway")
        if name == "start_task":
            return self._start(a)
        if name == "route_task":
            if a.get("model") not in ROUTED:
                self._refuse(name, "model pins are not allowed through the bot gateway: bots are routed only "
                                   "(cortados), never sub/ or api/ pins")
            with self.lock:
                if self.routes_inflight >= self.limits["max_concurrent"]:
                    reason = (f"route_task concurrency cap reached ({self.limits['max_concurrent']} in flight): it "
                              "bounds unattended subscription spend at once")
                elif self.count("routes") >= self.limits["max_routes_per_day"]:
                    reason = (f"route_task daily cap reached ({self.limits['max_routes_per_day']}/day): "
                              "it bounds unattended subscription spend")
                else:
                    reason = None
                    self._bump("routes")
                    self.routes_inflight += 1
            if reason:
                self._refuse(name, reason)
            mx = a.get("max_tokens")
            mx = (min(mx, MAX_ROUTE_TOKENS) if isinstance(mx, int) and not isinstance(mx, bool) and mx > 0
                  else MAX_ROUTE_TOKENS)
            try:
                res = self.route(self.cfg.get("url", ""), self.cfg.get("token", ""), a.get("task", ""),
                                 a.get("context", ""), mx, client=CLIENT)
            finally:
                with self.lock:
                    self.routes_inflight -= 1
            self.record(name, "error" if res.get("isError") else "ok")
            return res
        if name == "list_tasks":
            with self.lock:
                mine = {t for t, c in self.tasks.items() if c == CLIENT}
            lim = a.get("limit", 20)
            lim = lim if isinstance(lim, int) and 1 <= lim <= 200 else 20    # 200: list_recent's own bound
            # Ask the store for exactly the ids this gateway started (not the owner's newest N, which bot tasks
            # would fall out of once the owner has started enough newer ones).
            res = self.child("list_tasks", {"limit": lim, "ids": sorted(mine)}, self.cfg)
            if not res.get("isError"):
                try:
                    tasks = json.loads(_text(res)).get("tasks", [])
                    tasks = [self._scrub(t) for t in tasks if isinstance(t, dict) and t.get("task_id") in mine][:lim]
                    res = {"content": [{"type": "text", "text": json.dumps({"tasks": tasks}, indent=2)}]}
                except (ValueError, TypeError, AttributeError, RecursionError):
                    res = subagent_mcp._err("list_tasks: unexpected result from the task store")
            self.record(name, "error" if res.get("isError") else "ok")
            return res
        # task_status / wait_task / cancel_task: only on tasks this client started
        tid = a.get("task_id")
        with self.lock:
            owner = self.tasks.get(tid) if isinstance(tid, str) else None
        if owner != CLIENT:
            self._refuse(name, "unknown task_id, or a task this client did not start", tid if isinstance(tid, str)
                         else None)
        res = self._scrub_result(self.child(name, a, self.cfg))
        self.record(name, "error" if res.get("isError") else "ok", tid)
        return res

    def _scrub(self, d: dict) -> dict:
        d = {k: v for k, v in d.items() if k not in PATH_FIELDS}
        if isinstance(d.get("cwd"), str):
            real = os.path.realpath(d["cwd"])
            d["cwd"] = (os.path.relpath(real, self.root) if real == self.root or real.startswith(self.root + os.sep)
                        else "(outside the workspace root)")
        return d

    def _scrub_result(self, res: dict) -> dict:
        """Strip owner paths from a JSON tool result; a non-JSON (error) text is passed through unchanged."""
        try:
            obj = json.loads(_text(res))
        except (ValueError, RecursionError):
            return res
        if not isinstance(obj, dict):
            return res
        out = dict(res)
        out["content"] = [{"type": "text", "text": json.dumps(self._scrub(obj), indent=2)}]
        return out

    def _start(self, a: dict) -> dict:
        if a.get("chrome") not in (None, False):
            self._refuse("start_task", "chrome is never allowed through the bot gateway (it would act in the "
                                       "owner's signed-in browser on the main subscription)")
        if a.get("orchestrator") not in ROUTED:
            self._refuse("start_task", "model pins are not allowed through the bot gateway: bots are routed only "
                                       "(cortados), never sub/ or api/ pins")
        if a.get("permission_mode") not in ALLOWED_PERMISSION_MODES or a.get("allowed_tools") not in (None, [], ""):
            self._refuse("start_task", "permission_mode other than 'plan' and allowed_tools are refused until the "
                                       "per-user boundary (phase 2 B5) ships: children run as the owner's user")
        if not self._inside_root(a.get("cwd")):
            self._refuse("start_task", "cwd must be an absolute path inside the gateway's workspace root")
        self._refresh_running()
        slot = f"{_PENDING}{threading.get_ident()}-{time.monotonic_ns()}"
        with self.lock:
            if len(self.running) >= self.limits["max_concurrent"]:
                reason = (f"concurrency cap reached ({self.limits['max_concurrent']} running): it bounds unattended "
                          "subscription spend at once")
            elif self.count("tasks") >= self.limits["max_tasks_per_day"]:
                reason = f"daily task cap reached ({self.limits['max_tasks_per_day']}/day)"
            else:
                reason = None
                self._bump("tasks")
                self.running.add(slot)    # reserve the slot now, so parallel starts cannot all pass the cap
        if reason:
            self._refuse("start_task", reason)
        args = {k: a[k] for k in ("task", "permission_mode") if a.get(k)}
        args["cwd"] = os.path.realpath(a["cwd"])   # forward the checked path: a symlink swapped after the check is moot
        args["chrome"] = False
        tid = None
        try:
            res = self.child("start_task", args, {**self.cfg, "client": CLIENT})
            if not res.get("isError"):
                try:
                    tid = json.loads(res["content"][0]["text"]).get("task_id")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    tid = None
        finally:
            with self.lock:
                self.running.discard(slot)
                if tid:
                    self.tasks[tid] = CLIENT
                    self.running.add(tid)
                    self._save_counters()
        self.record("start_task", "ok" if tid else "error", tid)
        return self._scrub_result(res) if tid else res

    # -- JSON-RPC --------------------------------------------------------------------------------------------------
    def handle(self, msg) -> dict | None:
        """Like subagent_mcp._dispatch: an unexpected exception never drops the connection with a traceback; the
        reply names only the exception type (its text is unscrubbed and may echo config or paths)."""
        try:
            return self._handle(msg)
        except Exception as exc:
            mid = msg.get("id") if isinstance(msg, dict) else None
            sys.stderr.write(f"bot-gateway: internal error ({type(exc).__name__})\n")
            if mid is None:
                return None
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"internal error ({type(exc).__name__})"}}

    def _handle(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if mid is None:
            return None                                   # notifications (e.g. notifications/initialized)
        base = {"jsonrpc": "2.0", "id": mid}
        if method == "initialize":
            req = params.get("protocolVersion")
            ver = req if req in subagent_mcp.SUPPORTED_PROTOCOLS else subagent_mcp.PROTOCOL
            return {**base, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                        "serverInfo": {"name": "cortados-bot-gateway", "version": "1"}}}
        if method == "ping":
            return {**base, "result": {}}
        if method == "tools/list":
            return {**base, "result": {"tools": tool_list()}}
        if method == "tools/call":
            a = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                return {**base, "result": self.call(params.get("name"), a)}
            except PolicyRefusal as exc:
                return {**base, "error": {"code": POLICY_ERROR, "message": f"refused by bot gateway policy: {exc}"}}
        return {**base, "error": {"code": -32601, "message": f"method not found: {str(method)[:NAME_CHARS]}"}}


class PolicyRefusal(Exception):
    pass


def _text(res) -> str:
    try:
        t = res["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return ""
    return t if isinstance(t, str) else ""


def tool_list() -> list:
    """The six tools, with the refused arguments (chrome, connectors, pins, permission/tool grants) removed."""
    tools = [subagent_mcp.TOOL] + subagent_mcp._child_tools()
    out = []
    for t in tools:
        if t["name"] not in EXPOSED:
            continue
        if t["name"] == "start_task":
            t = json.loads(json.dumps(t))
            for k in ("chrome", "connectors", "orchestrator", "permission_mode", "allowed_tools"):
                t["inputSchema"]["properties"].pop(k, None)
            t["description"] += (" (Bot gateway: cwd must be inside the gateway's workspace root; no chrome, no model "
                                 "pins, no pre-approved tools.)")
        elif t["name"] == "route_task":
            t = json.loads(json.dumps(t))
            t["inputSchema"]["properties"].pop("model", None)
            t["description"] += " (Bot gateway: routed only, no model pins.)"
        out.append(t)
    return out


def make_handler(gw: Gateway):
    inflight = threading.BoundedSemaphore(MAX_INFLIGHT)

    class H(BaseHTTPRequestHandler):
        server_version = "cortados-bot-gateway"
        timeout = REQUEST_TIMEOUT_S

        def log_message(self, fmt, *args):          # path + status only; headers (the token) are never logged
            sys.stderr.write("bot-gateway: %s %s\n" % (self.command, self.path.split("?")[0][:100]))

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(405, {"error": "use POST /mcp"})   # no SSE stream offered

        def do_POST(self):
            if not inflight.acquire(blocking=False):
                return self._send(503, {"error": "busy: too many requests in flight"})
            try:
                self._post()
            finally:
                inflight.release()

        def _post(self):
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            if not gw.authorized(self.headers.get("authorization")):
                return self._send(401, {"error": "unauthorized"})
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "request body too large or invalid length"})
            try:
                msg = json.loads(self.rfile.read(n) or b"null")
            except (ValueError, RecursionError):          # RecursionError: deeply nested JSON
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "parse error"}})
            if isinstance(msg, list):
                if len(msg) > MAX_BATCH:
                    return self._send(200, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32600, "message": f"batch longer than {MAX_BATCH}"}})
                replies = [r for r in (gw.handle(m) for m in msg) if r is not None]
                return self._send(200, replies) if replies else self._send(202, {})
            reply = gw.handle(msg)
            if reply is None:
                self.send_response(202)
                self.send_header("content-length", "0")
                self.end_headers()
                return
            self._send(200, reply)
    return H


def build_server(gw: Gateway, host: str = "127.0.0.1", port: int = 8434) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(gw))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--token-file", default=os.environ.get("CORTADOS_BOT_TOKEN_FILE", ""))
    ap.add_argument("--workspace-root", default=os.environ.get("CORTADOS_BOT_WORKSPACE_ROOT", ""))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8434)
    ap.add_argument("--children-run-as-me", action="store_true",
                    help="allow a non-loopback --host although children run as the owner's user (no per-user "
                         "boundary until phase 2 B5)")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULTS["max_concurrent"])
    ap.add_argument("--max-tasks-per-day", type=int, default=DEFAULTS["max_tasks_per_day"])
    ap.add_argument("--max-routes-per-day", type=int, default=DEFAULTS["max_routes_per_day"])
    o = ap.parse_args(argv)
    if o.host not in LOOPBACK and not o.children_run_as_me:
        sys.stderr.write(f"bot-gateway: refusing to listen on {o.host!r}: children run as the owner's user and the "
                         "per-user boundary (phase 2 B5) is not built yet; pass --children-run-as-me to accept that\n")
        return 2
    try:
        token = read_token_file(o.token_file)
        cfg = {"url": subagent_mcp._resolve_url(), "token": subagent_mcp._resolve_token(),
               "token_file": os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or None}
        gw = Gateway(token, o.workspace_root, cfg, {"max_concurrent": o.max_concurrent,
                                                     "max_tasks_per_day": o.max_tasks_per_day,
                                                     "max_routes_per_day": o.max_routes_per_day})
    except GatewayError as exc:
        sys.stderr.write(f"bot-gateway: refusing to start: {exc}\n")
        return 2
    srv = build_server(gw, o.host, o.port)
    sys.stderr.write(f"bot-gateway: listening on {o.host}:{o.port} /mcp\n")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
