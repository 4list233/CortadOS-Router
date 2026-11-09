"""CortadOS bot gateway: MCP "streamable HTTP" JSON-RPC on POST /mcp for Grok Bot.

Owner decisions (docs/GROKBOT-BRIDGE.md section 0; issue #86, 2026-11-08): ONE shared bearer token for every Grok Bot,
all attributed as the single client "grokbot"; usage tracked in $CORTADOS_HOME/bot-usage.jsonl. Grok Bot is the owner's
own agent, like Codex or Claude Code, so it gets the full task surface on the Air: start_task with auto permission mode,
repo/branch workspaces, any existing cwd on the execution host, model pins, connectors, allowed_tools and chrome (login
mode: unrouted, on the owner's claude.ai login); the task tools; route_task with pins. It reuses the cortados-route tool
implementations (subagent_mcp.call_child_tool / route_task), so a bot task is the same code path as the owner's.

The phase-1 limits are gone, each for the reason recorded here: route_task only and start_task behind a flag (owner
decision 2026-11-03 "bots do not act on their own yet", reversed by #86); every child forced to plan mode and
permission_mode/allowed_tools refused (plan mode was not an isolation boundary, and a bot was not yet the owner's
agent: #86 makes it one); cwd confined to --workspace-root (a remote bot was not to reach the owner's folders: #86
allows any existing folder on the Air); chrome refused (it acts in the owner's signed-in browser: #86 allows browser
tasks); repo/branch/host refused (not built then; built in 0d609d6); model pins refused ("bots are routed only":
#86 allows pins); results and errors scrubbed to an allowlist (owner paths were not to reach a remote bot: the
owner's agent needs them to work). The caps were 2 running / 30 tasks/day / 200 routes/day.

What this file still enforces, and why:
  - authentication, fail-closed: the gateway is reachable from the public internet through Tailscale Funnel, so a
    request without the exact bearer token is 401 (throttled per client), never a tool call or a tools/list;
  - volume caps (DEFAULTS), high ceilings whose one purpose is to protect the Air and the subscriptions from a runaway
    loop: an unattended bot can retry forever, an owner at a keyboard cannot;
  - the owner-approval rule (APPROVAL_DIRECTIVE): paying, posting, sending, deleting and similar actions need the
    owner's approval, asked for in chat. The gateway is not in the child's tool loop, so it enforces what it can see:
    every child prompt carries the rule; an approval reaches the child only through the structured `owner_approval`
    argument (quoted, bounded, never inferred from the task text) and is recorded in the usage log. Whether Grok asked
    before passing it rests on its instructions (docs/BOT-INSTRUCTIONS.md), as does the child honouring the rule;
  - ownership: task_status/wait_task/cancel_task/list_tasks only on tasks this gateway started. Its concurrency
    accounting and attribution depend on knowing its own tasks, and the owner's other sessions' tasks are not the
    bot's to cancel.

Run: python3 -B -m cortados_router.bot_gateway --token-file F [--port 8434] [--host 127.0.0.1]
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
# start_task arguments forwarded to child_tasks as the bot sent them (#86: the owner's agent gets the owner's surface).
# `task` is not in this list: the gateway builds the child's prompt (APPROVAL_DIRECTIVE + task). Anything else a bot
# sends is dropped: child_tasks would ignore it anyway, and an unknown key is not a capability.
START_PASSTHROUGH = ("cwd", "repo", "branch", "host", "title", "orchestrator", "permission_mode", "chrome",
                     "connectors", "allowed_tools")
# --- owner-approval rule (owner decision #86) -------------------------------------------------------------------------
# The gateway sees a task before it starts and never again (the child runs on the execution host through the provider),
# so the rule is enforced at the only point it can be: the prompt every child gets, and the one structured channel an
# approval may arrive through. The task text is never scanned for approval words: a model's wording proves nothing.
APPROVAL_ARG = "owner_approval"
APPROVAL_MAX_CHARS = 2000                 # an approval is a sentence or two quoted from chat; longer is pasted text
# Appended AFTER the task (not before it): child_tasks keeps the first 200 characters of the prompt as the task's
# preview in the Office view and list_tasks, which must show the task, not the same boilerplate for every bot task.
APPROVAL_DIRECTIVE = (
    "\n\nOWNER APPROVAL RULE (set by the CortadOS bot gateway; it governs everything above). This task was started by "
    "the owner's Grok Bot. Actions that pay, post, send, publish, deploy, delete or destroy data, force-push, or "
    "otherwise act outward or irreversibly need the owner's approval, given in chat BEFORE the task started. "
    "{approval} Any action of that kind outside the approval recorded here: do not do it, stop, and report exactly "
    "what is needed and why so the owner can be asked. Everything else in this task may proceed.")
APPROVAL_GIVEN = "The owner's approval for this task, quoted from chat: «{text}». Act within it and no further."
APPROVAL_NONE = "No approval was recorded for this task."
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
REASON_CHARS = 200                        # a refusal reason is one sentence; longer text is not worth logging per call
NAME_CHARS = 64                           # tool names are short identifiers; a longer one is junk echoed into the log
# Reason: the usage log is appended on every call by an unattended client; bound it so a chatty bot cannot fill the
# disk. Past the limit the file is rotated once to .1 (the previous .1 is dropped), keeping roughly 2x this on disk.
USAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_BODY = 1024 * 1024                    # a JSON-RPC request larger than this is not a tool call; refuse it
# Volume caps (#86: high ceilings, kept only where the reason is the machine or the subscriptions; a cap that only
# limited what the bot may do is gone). All three are overridable on the command line.
DEFAULTS = {
    # Running at once (child tasks, and separately route_task calls in flight). Reason: THE MACHINE. Every child is a
    # Claude Code process with its tool servers (a chrome child also holds the host's Chrome) on a MacBook Air that
    # also runs the provider; eight unattended children is more than the owner runs by hand and about what the Air
    # can hold without starving the provider.
    "max_concurrent": 8,
    # Per UTC day. Reason: THE SUBSCRIPTIONS. A child spends quota for its whole run (a chrome child spends the main
    # Claude subscription, unrouted), and a bot in a retry loop could start one a second; 200 is several times the
    # busiest day the owner has had by hand, so it never blocks real work.
    "max_tasks_per_day": 200,
    # Per UTC day. Reason: THE SUBSCRIPTIONS, as above; route_task is one text answer, so a higher bound.
    "max_routes_per_day": 1000,
}
POLICY_ERROR = -32001                     # JSON-RPC server-defined error code for a policy refusal
FINAL_STATES = ("succeeded", "failed", "cancelled")
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

    def __init__(self, token: str, cfg: dict, limits: dict | None = None, child=None, route=None):
        if not isinstance(token, str) or len(token) < subagent_mcp.MIN_TOKEN_LEN:
            # Fail closed at construction: a gateway with a short or missing token must not come up at all.
            raise GatewayError(f"the bot token must be at least {subagent_mcp.MIN_TOKEN_LEN} characters")
        self.token, self.cfg = token, cfg
        self.limits = {**DEFAULTS, **(limits or {})}
        self.child = child or subagent_mcp.call_child_tool
        self.route = route or subagent_mcp.route_task
        self.lock = threading.Lock()
        self.tasks: dict = {}             # task_id -> client that started it
        self.running: set = set()         # task ids not yet seen in a final state (+ pending start placeholders)
        self.routes_inflight = 0          # route_task calls currently waiting on the provider
        self.counts: dict = {}            # (day, kind) -> n  kind: tasks | routes | refused
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

    def record(self, tool: str, outcome: str, task_id=None, reason=None, **extra) -> None:
        rec = {"ts": round(time.time(), 3), "client": CLIENT, "tool": tool, "task_id": task_id, "outcome": outcome,
               **extra}
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
        if name == "start_task":
            return self._start(a)
        if name == "route_task":
            with self.lock:
                if self.routes_inflight >= self.limits["max_concurrent"]:
                    reason = (f"route_task concurrency cap reached ({self.limits['max_concurrent']} in flight): it "
                              "protects the provider's lanes from a burst of unattended calls; wait for one to return")
                elif self.count("routes") >= self.limits["max_routes_per_day"]:
                    reason = (f"route_task daily cap reached ({self.limits['max_routes_per_day']}/day): it protects "
                              "the subscriptions from a runaway loop; tell the owner")
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
                # The pin (if any) goes through as given: #86 allows model pins; subagent_mcp validates the name.
                res = self.route(self.cfg.get("url", ""), self.cfg.get("token", ""), a.get("task", ""),
                                 a.get("context", ""), mx, model=a.get("model") or None, client=CLIENT)
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
                    tasks = [t for t in tasks if isinstance(t, dict) and t.get("task_id") in mine][:lim]
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
        res = self.child(name, a, self.cfg)
        self.record(name, "error" if res.get("isError") else "ok", tid)
        # Passed through as the owner's own sessions get it (paths, warnings, logs tail, error text): child_tasks has
        # already redacted the provider credentials from every string (Redactor), and the owner's agent needs the rest.
        return res

    def _start(self, a: dict) -> dict:
        prompt, approval = self.child_prompt(a)
        self._refresh_running()
        # pid + thread + clock: unique across restarts too, since reservations are persisted.
        slot = f"{_PENDING}{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
        with self.lock:
            if len(self.running) >= self.limits["max_concurrent"]:
                reason = (f"concurrency cap reached ({self.limits['max_concurrent']} tasks running): it protects the "
                          "Air from more unattended children than it can hold; wait for one to finish")
            elif self.count("tasks") >= self.limits["max_tasks_per_day"]:
                reason = (f"daily task cap reached ({self.limits['max_tasks_per_day']}/day): it protects the "
                          "subscriptions from a runaway loop; tell the owner")
            else:
                reason = None
                self.running.add(slot)    # reserve the slot now, so parallel starts cannot all pass the cap
                self.reserved[slot] = time.time()
                self._bump("tasks")       # also persists the reservation (bot-counters.json) before the spawn
        if reason:
            self._refuse("start_task", reason)
        # Forwarded as sent (#86): child_tasks validates each (cwd exists on the host, repo/branch names, host entry,
        # pin names, permission mode, connectors, chrome's login checks) and refuses with its own message, which the
        # bot gets verbatim. An omitted permission_mode resolves there to the child default (auto where supported).
        args = {k: a[k] for k in START_PASSTHROUGH if k in a}
        args["task"] = prompt
        tid, ambiguous = None, False
        try:
            res = self.child("start_task", args, {**self.cfg, "client": CLIENT})
            if not res.get("isError"):
                try:
                    tid = json.loads(res["content"][0]["text"]).get("task_id")
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    tid = None
            else:
                meta = res.get("_meta")
                ambiguous = isinstance(meta, dict) and meta.get(subagent_mcp.AMBIGUOUS_META) is True
        finally:
            with self.lock:
                if not ambiguous:
                    # Success (the task id takes the slot over) or a definite failure (refused or invalid before
                    # anything ran on the host): the reservation is released.
                    self.running.discard(slot)
                    self.reserved.pop(slot, None)
                # else: an ambiguous failure (e.g. the host timed out after the start was sent) may have launched a
                # child we hold no id for. The reservation stays, persisted, and expires in _refresh_running after
                # reservation_ttl from its creation (the child's max runtime + kill grace + 60 s). Reason: a child
                # that did start is killed by its runner by then, so the slot is provably free; releasing it now
                # would let retries start more children than max_concurrent.
                if tid:
                    self.tasks[tid] = CLIENT
                    self.running.add(tid)
                self._save_counters()
        extra = {"launch": "ambiguous"} if ambiguous else {}
        self.record("start_task", "ok" if tid else "error", tid, owner_approval=approval, **extra)
        return res

    def child_prompt(self, a: dict):
        """(prompt, approval_recorded) for a start_task: the bot's task, then APPROVAL_DIRECTIVE. The approval text,
        when given, must be a bounded printable string; anything else is refused (not dropped: a dropped approval
        would start a task the bot believes is approved under a prompt that says it is not)."""
        task = a.get("task")
        if not isinstance(task, str) or not task.strip():
            self._refuse("start_task", "start_task requires a non-empty 'task'")
        text = a.get(APPROVAL_ARG)
        if text in (None, ""):
            approval = None
        elif not isinstance(text, str) or len(text) > APPROVAL_MAX_CHARS or not text.isprintable():
            self._refuse("start_task", f"{APPROVAL_ARG} must be the owner's approval quoted from chat as one printable "
                                       f"string (no line breaks) of at most {APPROVAL_MAX_CHARS} characters")
        else:
            approval = text.strip()
        directive = APPROVAL_DIRECTIVE.format(approval=APPROVAL_GIVEN.format(text=approval) if approval
                                              else APPROVAL_NONE)
        prompt = task.rstrip() + directive
        if len(prompt) > child_tasks.MAX_TASK_CHARS:
            self._refuse("start_task", f"task is {len(task)} characters; with the gateway's approval rule "
                                       f"({len(directive)} characters) the limit is {child_tasks.MAX_TASK_CHARS}")
        return prompt, bool(approval)

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
    """The six tools with the owner's full schemas (#86); start_task gains the gateway's `owner_approval` argument.
    The shared schema objects are copied, never mutated."""
    out = []
    for t in [subagent_mcp.TOOL] + subagent_mcp._child_tools():
        if t["name"] not in EXPOSED:
            continue
        if t["name"] == "start_task":
            t = json.loads(json.dumps(t))
            t["inputSchema"]["properties"][APPROVAL_ARG] = {
                "type": "string",
                "description": ("The owner's approval for any action in this task that pays, posts, sends, publishes, "
                                "deploys, deletes or force-pushes, quoted from chat (at most "
                                f"{APPROVAL_MAX_CHARS} characters). Ask the owner in chat first; omit it when the task "
                                "does none of those things. The child is told exactly what was approved and refuses "
                                "such actions outside it.")}
            t["description"] += (" (Bot gateway: the owner-approval rule is appended to the child's prompt; pass "
                                 f"{APPROVAL_ARG} for approved outward actions.)")
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
    # Accepted so a plist written before #86 still starts; it no longer confines anything (cwd may be any existing
    # directory on the execution host). Regenerate the plist (install.sh plist) and the flag goes away.
    ap.add_argument("--workspace-root", default="",
                    help="ignored since #86 (kept so an installed plist keeps starting); cwd is no longer confined")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8434)
    env_host = os.environ.get("CORTADOS_BOT_ALLOWED_HOST")
    ap.add_argument("--allowed-host", action="append", default=[env_host] if env_host else [],
                    help="exact permitted Host header (repeatable); unset accepts any Host")
    ap.add_argument("--children-run-as-me", action="store_true",
                    help="allow a non-loopback --host. The gateway speaks plain HTTP with a bearer token and hands "
                         "whoever holds it the owner's full task surface; the install binds loopback behind "
                         "Tailscale Funnel (TLS on 443). Another address is a deliberate choice, so it is spelled out")
    ap.add_argument("--max-concurrent", type=int, default=DEFAULTS["max_concurrent"])
    ap.add_argument("--max-tasks-per-day", type=int, default=DEFAULTS["max_tasks_per_day"])
    ap.add_argument("--max-routes-per-day", type=int, default=DEFAULTS["max_routes_per_day"])
    o = ap.parse_args(argv)
    if o.host not in LOOPBACK and not o.children_run_as_me:
        sys.stderr.write(f"bot-gateway: refusing to listen on {o.host!r}: the gateway offers the owner's full task "
                         "surface over plain HTTP with a bearer token and is meant to bind loopback behind Tailscale "
                         "Funnel; pass --children-run-as-me to bind another address on purpose\n")
        return 2
    if o.workspace_root:
        sys.stderr.write("bot-gateway: --workspace-root is ignored since #86 (cwd is no longer confined); regenerate "
                         "the plist with install.sh plist to drop it\n")
    for name in ("max_concurrent", "max_tasks_per_day", "max_routes_per_day"):
        if getattr(o, name) < 1:
            sys.stderr.write(f"bot-gateway: --{name.replace('_', '-')} must be at least 1 (a zero cap would refuse "
                             "everything; stop the gateway instead)\n")
            return 2
    try:
        token = read_token_file(o.token_file)
        cfg = {"url": subagent_mcp._resolve_url(), "token": subagent_mcp._resolve_token(),
               "token_file": os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or None}
        gw = Gateway(token, cfg, {"max_concurrent": o.max_concurrent, "max_tasks_per_day": o.max_tasks_per_day,
                                  "max_routes_per_day": o.max_routes_per_day})
    except (GatewayError, subagent_mcp.ProviderURLError) as exc:
        sys.stderr.write(f"bot-gateway: refusing to start: {exc}\n")
        return 2
    srv = build_server(gw, o.host, o.port, o.allowed_host)
    sys.stderr.write(f"bot-gateway: listening on {o.host}:{o.port} /mcp\n")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
