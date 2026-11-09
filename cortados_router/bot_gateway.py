"""CortadOS bot gateway (phase 1): MCP "streamable HTTP" JSON-RPC on POST /mcp for Grok Bots.

Owner decisions (docs/GROKBOT-BRIDGE.md): ONE shared bearer token for every Grok Bot; all of them are attributed as the
single client "grokbot"; usage is tracked in $CORTADOS_HOME/bot-usage.jsonl; Chrome (login mode) is never allowed.

It reuses the cortados-route tool implementations (subagent_mcp.call_child_tool / route_task). Phase 1 offers ONLY
route_task (owner decision 2026-11-03: bots must not act on their own yet): start_task, and with it wait_task /
task_status / list_tasks / cancel_task, is disabled until per-bot filesystem/account isolation (phase 2, B5) exists.
The start_task path stays behind --enable-start-task (only together with --children-run-as-me), and even then every
child is forced to plan mode. Policy is enforced here BEFORE a tool runs: chrome refused, cwd confined to
--workspace-root, a client only sees the tasks it started, and caps (concurrency, tasks/day, route_task/day).

Run: python3 -B -m cortados_router.bot_gateway --token-file F --workspace-root DIR [--port 8434] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import collections
import hmac
import ipaddress
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cortados_router import child_tasks, paths, subagent_mcp

CLIENT = "grokbot"                       # fixed: one shared token means one attributable client (owner decision)
EXPOSED = ("start_task", "wait_task", "task_status", "list_tasks", "cancel_task", "route_task")
# Phase 1 surface. Reason: children run as the owner's user with the owner's files and MCP servers, and plan mode is
# not an isolation boundary (review r2 P1-2), so until phase 2 B5 a bot only gets text answers.
PHASE1 = ("route_task",)
START_DISABLED = ("start_task is disabled until per-bot filesystem/account isolation (phase 2, B5) exists; "
                  "use route_task")
# Reason: the only mode a bot child may ever get. Omitted/empty would let child_tasks pick its default (auto), which
# runs the owner's shell unattended (review r2 P1-1), so the gateway always sends it explicitly.
FORCED_PERMISSION_MODE = "plan"
# Successful results are allowlisted, not blocklisted: a field child_tasks adds later (a path, a note) must not reach a
# remote bot until someone decides it should. cwd is rewritten relative to the workspace root.
RESULT_FIELDS = ("task_id", "state", "elapsed_s", "exit_code", "timed_out", "result", "result_truncated", "models",
                 "cwd", "permission_mode", "cancelled", "ok", "waited_s")
# Fixed external error text: a child or provider error can carry owner paths (token file, task dir); the detail goes
# to the operator's stderr only, bounded to this many characters.
ERROR_DETAIL_CHARS = 500
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
# machine's layout. RESULT_FIELDS (an allowlist) keeps them out; this list names them for the tests that assert it.
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
MAX_INFLIGHT = 16                         # bounds connection threads at once (taken at accept); past it: 503
REQUEST_TIMEOUT_S = 30                    # a bot sends its JSON-RPC at once or is gone; a slow sender holds a thread
# The idle timeout only measures silence, so a client trickling a byte at a time would never trip it: the request line
# and headers, then the body, each get an absolute deadline (as local_gate does), after which the socket is shut.
HEADER_DEADLINE_S = 10.0                  # a bot sends its headers in one write; 10 s is generous for a slow link
BODY_DEADLINE_S = 30.0                    # MAX_BODY (1 MiB) in 30 s is ~280 kbit/s, slower than any real bot link
# Five guesses is enough to catch a broken configuration without making a typo immediately lock a client out.
AUTH_FAILURES = 5
# Ten minutes prevents slow password guessing while allowing stale mistakes to age out.
AUTH_WINDOW_S = 10 * 60
# One minute is a short first lockout; repeated bursts double up to the one-hour operationally recoverable ceiling.
AUTH_COOLDOWN_S, AUTH_COOLDOWN_MAX_S = 60, 60 * 60
# Sixty failed tokens/minute is response signalling, not an aggregate admission limit: a valid credential is always
# compared first so shared-egress attackers cannot lock it out. It makes widespread guessing visible/back off with 429.
GLOBAL_AUTH_FAILURES = 60
# 4096 addresses bounds memory under source-address churn while retaining far more than the expected bot population.
MAX_AUTH_CLIENTS = 4096
_PENDING = "__pending__"                  # placeholder prefix reserving a concurrency slot while a child starts


class GatewayError(Exception):
    pass


def client_address(peer: str, forwarded: str = "") -> str:
    """Use Funnel's appended last XFF hop only for a loopback TCP peer."""
    if peer in LOOPBACK:
        candidate = forwarded.rsplit(",", 1)[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return peer


class AuthThrottle:
    """Bounded failed-auth response limiter; valid credentials deliberately bypass all cooldowns."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.lock = clock, threading.Lock()
        self.clients = collections.OrderedDict()
        # At most the threshold plus one is needed to decide that the global cap has been crossed.
        self.global_failures = collections.deque(maxlen=GLOBAL_AUTH_FAILURES + 1)

    def blocked(self, address: str):
        now = self.clock()
        with self.lock:
            state = self.clients.get(address)
            if state is None:
                return None
            self.clients.move_to_end(address)
            if state["blocked_until"] > now:
                return max(1, int(state["blocked_until"] - now + .999))
            return None

    def failed(self, address: str):
        """Record an invalid token. The threshold failure stays 401; later attempts are blocked before token checking."""
        now = self.clock()
        with self.lock:
            while self.global_failures and now - self.global_failures[0] >= 60:
                self.global_failures.popleft()
            self.global_failures.append(now)
            global_block = len(self.global_failures) > GLOBAL_AUTH_FAILURES
            state = self.clients.get(address)
            if state is None:
                if len(self.clients) >= MAX_AUTH_CLIENTS:
                    # Source churn must not evict an active ban. Evict the oldest inactive entry; if every bounded
                    # slot is actively banned, leave the new source untracked and use only aggregate 429 signalling.
                    victim = next((key for key, value in self.clients.items()
                                   if value["blocked_until"] <= now), None)
                    if victim is None:
                        return 60
                    del self.clients[victim]
                state = {"failures": collections.deque(), "cooldown": 0, "blocked_until": 0.0}
                self.clients[address] = state
            self.clients.move_to_end(address)
            failures = state["failures"]
            while failures and now - failures[0] >= AUTH_WINDOW_S:
                failures.popleft()
            failures.append(now)
            if len(failures) >= AUTH_FAILURES:
                state["cooldown"] = min(AUTH_COOLDOWN_MAX_S, state["cooldown"] * 2 or AUTH_COOLDOWN_S)
                state["blocked_until"] = now + state["cooldown"]
                failures.clear()
            return 60 if global_block else None


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
                 child=None, route=None, enable_start_task: bool = False):
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
        self.offered = EXPOSED if enable_start_task else PHASE1
        # Launch reservations (slot -> wall time), persisted BEFORE a child is spawned: if the gateway dies between the
        # spawn and recording the task id, the reservation survives the restart and keeps the concurrency slot held.
        self.reserved: dict = {}
        try:
            # Reason: a reservation whose launch never reported back can only have produced a child that its runner
            # kills at max_s (+ the kill grace); after that the slot is provably free again.
            self.reservation_ttl = child_tasks._max_s() + child_tasks.KILL_GRACE_S + 60
        except child_tasks.TaskError as exc:
            raise GatewayError(str(exc)) from None
        self._load_owned()
        # After a restart, every owned task may still be running; _refresh_running prunes the finished ones.
        self.running.update(t for t, c in self.tasks.items() if c == CLIENT)
        self.running.update(self.reserved)

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
        now = time.time()
        for slot, ts in saved.get("reserved", {}).items():
            if now - ts < self.reservation_ttl:
                self.reserved[slot] = ts
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
        reserved = d.get("reserved") if isinstance(d.get("reserved"), dict) else {}
        return {"day": d.get("day"),
                "reserved": {k: v for k, v in reserved.items() if isinstance(k, str) and k.startswith(_PENDING)
                             and isinstance(v, (int, float)) and not isinstance(v, bool)},
                "counts": {k: v for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool)},
                "owned": [t for t in owned if isinstance(t, str) and t]}

    def _save_counters(self) -> None:
        """Called with self.lock held. Temp file + atomic rename, so a crash never leaves half a file."""
        today = _day()
        owned = sorted(t for t, c in self.tasks.items() if c == CLIENT)[-MAX_OWNED:]
        obj = {"day": today, "counts": {k: n for (d, k), n in self.counts.items() if d == today}, "owned": owned,
               "reserved": dict(self.reserved)}
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

    def _confined(self, cwd):
        """The cwd resolved ONCE, or None when it is not inside the root. The caller forwards exactly this value, so a
        symlink swapped after the check cannot redirect the child (review r2 P2-3)."""
        if not isinstance(cwd, str) or not os.path.isabs(cwd):
            return None
        real = os.path.realpath(cwd)
        return real if real == self.root or real.startswith(self.root + os.sep) else None

    def _refresh_running(self) -> None:
        """Drop finished tasks from self.running. Called WITHOUT self.lock held: task_status may take seconds."""
        with self.lock:
            ids = [t for t in self.running if not t.startswith(_PENDING)]
            # A reservation left by a launch that never reported back (the gateway died mid-start) expires here.
            now = time.time()
            stale = [s for s, ts in self.reserved.items() if now - ts >= self.reservation_ttl]
            for s in stale:
                self.reserved.pop(s, None)
                self.running.discard(s)
            if stale:
                self._save_counters()
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
        if name not in self.offered:
            self._refuse(name, START_DISABLED if name == "start_task" else
                         f"{name} has nothing to act on: {START_DISABLED}")
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
            return _external(name, res) if res.get("isError") else res
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
            return _external(name, res) if res.get("isError") else res
        # task_status / wait_task / cancel_task: only on tasks this client started
        tid = a.get("task_id")
        with self.lock:
            owner = self.tasks.get(tid) if isinstance(tid, str) else None
        if owner != CLIENT:
            self._refuse(name, "unknown task_id, or a task this client did not start", tid if isinstance(tid, str)
                         else None)
        res = self.child(name, a, self.cfg)
        self.record(name, "error" if res.get("isError") else "ok", tid)
        return _external(name, res) if res.get("isError") else self._scrub_result(res)

    def _scrub(self, d: dict) -> dict:
        d = {k: v for k, v in d.items() if k in RESULT_FIELDS}
        if isinstance(d.get("cwd"), str):
            real = os.path.realpath(d["cwd"])
            d["cwd"] = (os.path.relpath(real, self.root) if real == self.root or real.startswith(self.root + os.sep)
                        else "(outside the workspace root)")
        return d

    def _scrub_result(self, res: dict) -> dict:
        """Allowlist the fields of a successful JSON tool result. Anything else (not JSON, not an object) is not passed
        through: its text is unscrubbed, so the bot gets a fixed message and the operator the detail."""
        try:
            obj = json.loads(_text(res))
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, dict):
            return _external("result", subagent_mcp._err(_text(res)))
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
        if any(a.get(k) not in (None, "") for k in ("repo", "branch", "host")):
            # Refused, not dropped: the gateway builds the child's arguments itself (cwd confined below), so a silently
            # ignored repo/branch/host would run the task somewhere other than the bot asked. Full access is #86.
            self._refuse("start_task", "repo, branch and host are not offered through the bot gateway yet (#86): "
                                       "pass cwd inside the gateway's workspace root")
        cwd = self._confined(a.get("cwd"))
        if cwd is None:
            self._refuse("start_task", "cwd must be an absolute path inside the gateway's workspace root")
        self._refresh_running()
        # pid + thread + clock: unique across restarts too, since reservations are persisted.
        slot = f"{_PENDING}{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        with self.lock:
            if len(self.running) >= self.limits["max_concurrent"]:
                reason = (f"concurrency cap reached ({self.limits['max_concurrent']} running): it bounds unattended "
                          "subscription spend at once")
            elif self.count("tasks") >= self.limits["max_tasks_per_day"]:
                reason = f"daily task cap reached ({self.limits['max_tasks_per_day']}/day)"
            else:
                reason = None
                self.running.add(slot)    # reserve the slot now, so parallel starts cannot all pass the cap
                self.reserved[slot] = time.time()
                self._bump("tasks")       # also persists the reservation (bot-counters.json) before the spawn
        if reason:
            self._refuse("start_task", reason)
        args = {"task": a.get("task"), "cwd": cwd, "chrome": False, "permission_mode": FORCED_PERMISSION_MODE}
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
                self.reserved.pop(slot, None)
                if tid:
                    self.tasks[tid] = CLIENT
                    self.running.add(tid)
                self._save_counters()
        self.record("start_task", "ok" if tid else "error", tid)
        return self._scrub_result(res) if tid else _external("start_task", res)

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
            return {**base, "result": {"tools": tool_list(self.offered)}}
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


def _external(tool: str, res: dict) -> dict:
    """A fixed error for the bot; the unscrubbed detail (it may name owner paths) goes to the operator's stderr."""
    detail = _text(res).replace("\n", " ")[:ERROR_DETAIL_CHARS]
    sys.stderr.write(f"bot-gateway: {tool} error: {detail}\n")
    return subagent_mcp._err(f"{tool} failed inside the gateway; the operator's gateway log has the details")


def tool_list(offered=PHASE1) -> list:
    """The offered tools, with the refused arguments (chrome, connectors, pins, permission/tool grants) removed."""
    tools = [subagent_mcp.TOOL] + subagent_mcp._child_tools()
    out = []
    for t in tools:
        if t["name"] not in offered:
            continue
        if t["name"] == "start_task":
            t = json.loads(json.dumps(t))
            for k in ("chrome", "connectors", "orchestrator", "permission_mode", "allowed_tools", "repo", "branch", "host"):
                t["inputSchema"]["properties"].pop(k, None)
            t["inputSchema"]["required"] = ["task", "cwd"]
            t["description"] += (" (Bot gateway: cwd must be inside the gateway's workspace root; no chrome, no model "
                                 "pins, no pre-approved tools, no repo/branch/host.)")
        elif t["name"] == "route_task":
            t = json.loads(json.dumps(t))
            t["inputSchema"]["properties"].pop("model", None)
            t["description"] += " (Bot gateway: routed only, no model pins.)"
        out.append(t)
    return out


class GatewayServer(ThreadingHTTPServer):
    """Capacity is taken at accept, BEFORE a worker thread exists or a header is parsed (local_gate's pattern): the
    old in-handler semaphore let unauthenticated, half-sent headers each hold a thread (review r2 P2-4)."""
    daemon_threads = True

    def __init__(self, addr, handler, max_connections: int):
        self.slots = threading.BoundedSemaphore(max_connections) if max_connections > 0 else None
        super().__init__(addr, handler)

    def process_request(self, request, client_address):
        if self.slots is None or not self.slots.acquire(blocking=False):
            body = b'{"error": "busy: too many requests in flight"}'
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\n"
                                b"content-length: " + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)
            except OSError as exc:            # the client already left; nothing to tell it
                sys.stderr.write(f"bot-gateway: 503 not delivered ({type(exc).__name__})\n")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(gw: Gateway, allowed_hosts=(), clock=time.monotonic):
    throttle = AuthThrottle(clock)
    allowed_hosts = frozenset(allowed_hosts)

    class H(BaseHTTPRequestHandler):
        server_version = "cortados-bot-gateway"
        timeout = REQUEST_TIMEOUT_S

        def setup(self):
            super().setup()
            # http.server reads the request line and headers itself, so a timer shuts the socket if they are not all
            # in by HEADER_DEADLINE_S; do_POST/do_GET cancel it. The flag stops a request parsed from partial headers.
            self._expired = False
            self._header_timer = threading.Timer(HEADER_DEADLINE_S, self._expire)
            self._header_timer.daemon = True
            self._header_timer.start()

        def _expire(self):
            self._expired = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError as exc:            # already closed by the client: the deadline's goal is met
                sys.stderr.write(f"bot-gateway: header deadline shutdown ({type(exc).__name__})\n")

        def finish(self):
            self._header_timer.cancel()
            super().finish()

        def _read_body(self, n: int):
            """n bytes within BODY_DEADLINE_S, or None. Each wait is bounded by the time left, so a trickle cannot
            extend the deadline."""
            end = time.monotonic() + BODY_DEADLINE_S
            parts, left = [], n
            try:
                while left > 0:
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        return None
                    self.connection.settimeout(min(REQUEST_TIMEOUT_S, remaining))
                    chunk = self.rfile.read1(min(left, 65536))
                    if not chunk:
                        return None
                    parts.append(chunk)
                    left -= len(chunk)
            except OSError:
                return None                   # the client stalled or left; the caller closes the connection
            return b"".join(parts)

        def _client_addr(self):
            # Funnel is trusted only on loopback and appends the real client as the last XFF hop; earlier hops are
            # attacker-controlled. Invalid appended values fall back to the TCP peer rather than becoming log/state keys.
            headers = getattr(self, "headers", None)
            forwarded = headers.get("x-forwarded-for", "") if headers is not None else ""
            return client_address(self.client_address[0], forwarded)

        def log_message(self, fmt, *args):
            pass                                      # log_request below is the sole bounded, header-free access log

        def log_request(self, code="-", size="-"):
            command = getattr(self, "command", "-")
            path = getattr(self, "path", "-")
            sys.stderr.write("bot-gateway: %s %s %s %s\n" %
                             (command, path.split("?")[0][:100], code, self._client_addr()))

        def _send(self, code: int, obj, headers=()) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._header_timer.cancel()
            self.close_connection = True
            if self._expired:
                return
            if allowed_hosts and self.headers.get("host") not in allowed_hosts:
                return self._send(421, {"error": "misdirected request"})
            self._send(405, {"error": "use POST /mcp"})   # no SSE stream offered

        def do_POST(self):
            self._header_timer.cancel()
            self.close_connection = True          # one request per connection: no keep-alive slot holding
            if self._expired:                     # the header deadline fired: these headers may be partial
                return
            try:
                self._post()
            except OSError:                       # the client left, reset or stalled; nothing to tell it
                self.close_connection = True

        def _post(self):
            if allowed_hosts and self.headers.get("host") not in allowed_hosts:
                return self._send(421, {"error": "misdirected request"})
            if self.path.split("?")[0] != "/mcp":
                return self._send(404, {"error": "not found"})
            client = self._client_addr()
            # Compare first: clients sharing a Funnel egress address cannot lock a valid credential out. Cooldowns
            # and the aggregate counter therefore limit only invalid responses, not token-comparison admission.
            if not gw.authorized(self.headers.get("authorization")):
                retry = throttle.blocked(client)
                if retry is not None:
                    return self._send(429, {"error": "too many failed authentication attempts"},
                                      (("Retry-After", str(retry)),))
                retry = throttle.failed(client)
                if retry is not None:
                    return self._send(429, {"error": "too many failed authentication attempts"},
                                      (("Retry-After", str(retry)),))
                return self._send(401, {"error": "unauthorized"})
            try:
                n = int(self.headers.get("content-length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._send(413, {"error": "request body too large or invalid length"})
            raw = self._read_body(n)
            if raw is None:
                return                            # body deadline passed or the client left: close, no reply
            try:
                msg = json.loads(raw or b"null")
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


def build_server(gw: Gateway, host: str = "127.0.0.1", port: int = 8434, allowed_hosts=(),
                 clock=time.monotonic) -> ThreadingHTTPServer:
    return GatewayServer((host, port), make_handler(gw, allowed_hosts, clock), MAX_INFLIGHT)


def main(argv=None) -> int:
    # allow_abbrev=False: a prefix such as --enable-start-t must not expand to --enable-start-task; the Air launcher
    # (integrations/bot-gateway/launch.py) allowlists exact spellings, and this keeps the two parsers in agreement.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--token-file", default=os.environ.get("CORTADOS_BOT_TOKEN_FILE", ""))
    ap.add_argument("--workspace-root", default=os.environ.get("CORTADOS_BOT_WORKSPACE_ROOT", ""))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8434)
    env_host = os.environ.get("CORTADOS_BOT_ALLOWED_HOST")
    ap.add_argument("--allowed-host", action="append", default=[env_host] if env_host else [],
                    help="exact permitted Host header (repeatable); unset accepts any Host")
    ap.add_argument("--children-run-as-me", action="store_true",
                    help="allow a non-loopback --host although children run as the owner's user (no per-user "
                         "boundary until phase 2 B5)")
    ap.add_argument("--enable-start-task", action="store_true",
                    help="offer start_task (and the task tools) again; only with --children-run-as-me, and every "
                         "child is still forced to plan mode. Off in phase 1 (owner decision 2026-11-03)")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULTS["max_concurrent"])
    ap.add_argument("--max-tasks-per-day", type=int, default=DEFAULTS["max_tasks_per_day"])
    ap.add_argument("--max-routes-per-day", type=int, default=DEFAULTS["max_routes_per_day"])
    o = ap.parse_args(argv)
    if o.host not in LOOPBACK and not o.children_run_as_me:
        sys.stderr.write(f"bot-gateway: refusing to listen on {o.host!r}: children run as the owner's user and the "
                         "per-user boundary (phase 2 B5) is not built yet; pass --children-run-as-me to accept that\n")
        return 2
    if o.enable_start_task and not o.children_run_as_me:
        sys.stderr.write("bot-gateway: --enable-start-task needs --children-run-as-me: a child runs as the owner's "
                         "user with the owner's files until phase 2 B5\n")
        return 2
    try:
        token = read_token_file(o.token_file)
        cfg = {"url": subagent_mcp._resolve_url(), "token": subagent_mcp._resolve_token(),
               "token_file": os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or None}
        gw = Gateway(token, o.workspace_root, cfg, {"max_concurrent": o.max_concurrent,
                                                     "max_tasks_per_day": o.max_tasks_per_day,
                                                     "max_routes_per_day": o.max_routes_per_day},
                     enable_start_task=o.enable_start_task)
    except (GatewayError, subagent_mcp.ProviderURLError) as exc:
        sys.stderr.write(f"bot-gateway: refusing to start: {exc}\n")
        return 2
    srv = build_server(gw, o.host, o.port, o.allowed_host)
    sys.stderr.write(f"bot-gateway: listening on {o.host}:{o.port} /mcp\n")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
