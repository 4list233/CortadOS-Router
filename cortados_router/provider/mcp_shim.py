"""Stdio MCP server that the headless CLI loads as `bridge`: it lists the client's tools and forwards each call.

Spawned by the CLI, one per bridge session. A `tools/call` is POSTed to the endpoint's /bridge/call, which blocks
until the client has run the tool and sent the result back. Stdlib only, no package imports: the CLI runs this
file directly.

Usage: mcp_shim.py <gateway url> <session id> <tools.json>; the endpoint token comes in CORTADOS_PROVIDER_TOKEN.

The invariant: the CLI must never generate its OWN synthetic tool error (its MCP tool timeout) and continue a paid turn
unattended. A real tool result written by this shim is never a violation, even one written close to the deadline.
(a) The shim's per-call deadline (CALL_TIMEOUT_S, passed in by the provider as CORTADOS_PROVIDER_CALL_TIMEOUT_S) is
    strictly earlier than the CLI's MCP_TOOL_TIMEOUT by a documented margin (timeouts.CLI_MARGIN_S, floor 30 s); both
    come from one constant in cortados_router/provider/timeouts.py. A write that passes the pre-write check (failed
    flag + deadline, under the dispatcher lock) is a one-line pipe write that completes long before the CLI could time
    out, so it is allowed to finish; stdout writes are deliberately not made cancellable.
(b) Once a deadline passes, the shim publishes nothing new: it SIGKILLs the owning CLI under the lock every writer
    checks, then asks the provider to close the backend. A flush that returns after the deadline also fails closed.
Cleanup survives owner death: on stdin EOF (the killed CLI closes our stdin) main() joins the abort for a bounded time
and reports an unconfirmed abort on stderr; independently, the provider removes a conversation whose CLI exited.
"""
import collections
import heapq
import queue
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
# Client tools include long test suites and permission prompts. Three hours covers the maximum task runtime;
# finite expiry still releases abandoned calls. The provider passes max(idle TTL, tool window) explicitly.
TOOL_WAIT_S = float(os.environ.get("CORTADOS_PROVIDER_TOOL_WAIT_S") or 10800)
if not 0 < TOOL_WAIT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_TOOL_WAIT_S must be positive and finite")
# A shim call can arrive while the backend is still producing the assistant reply. The server deliberately starts
# TOOL_WAIT_S only once that reply is complete, so the outer transports must cover the preceding turn budget.
TURN_TIMEOUT_S = float(os.environ.get("CORTADOS_PROVIDER_TURN_TIMEOUT_S") or 1800)
if not 0 < TURN_TIMEOUT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_TURN_TIMEOUT_S must be positive and finite")
# Slack lets the server return its timeout diagnostic and close the backend before the shim/CLI abandon the call.
CALL_SLACK_S = float(os.environ.get("CORTADOS_PROVIDER_CALL_SLACK_S") or 120)
if not 0 < CALL_SLACK_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_CALL_SLACK_S must be positive and finite")
CALL_TIMEOUT_S = float(os.environ.get("CORTADOS_PROVIDER_CALL_TIMEOUT_S") or (TURN_TIMEOUT_S + TOOL_WAIT_S + CALL_SLACK_S))
if not 0 < CALL_TIMEOUT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_CALL_TIMEOUT_S must be positive and finite")
# The provider resolves compatibility aliases and supplies all budgets explicitly to this standalone process.
ABORT_TIMEOUT_S = float(os.environ.get("CORTADOS_PROVIDER_ABORT_TIMEOUT_S") or 120)
if not 0 < ABORT_TIMEOUT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_ABORT_TIMEOUT_S must be positive and finite")

# Claude may issue sibling calls together. Never make a later call spend its CLI timeout queued behind an earlier
# HTTP request: every call's absolute deadline is anchored at the time the stdin reader read its line, before any parsing or queueing.
# Capacity, per shim process (one shim per bridge session; the provider admits at most 128 conversations, so at
# most 128 shims exist): 1 stdin reader + 1 main (parse/dispatch) + 1 deadline thread + MAX_IN_FLIGHT workers = 19 threads, fixed and
# independent of how many calls arrive. Across the 128-conversation cap that is at most 128 * 16 = 2048 concurrent
# /bridge/call requests, each of which the provider parks on one handler thread; abort traffic is sent from the
# deadline thread (overflow included), never from a worker or the stdin reader, so it never waits for worker capacity.
# 16 is well above the parallel tool fan-out Claude Code issues in one assistant turn.
MAX_IN_FLIGHT = 16
# Calls accepted but not yet answered (queued + running + being written). Queued calls cost one small dict, no
# thread. Above this the session is abnormal: fail closed (abort the backend) rather than grow without bound.
MAX_PENDING = 64
# Answered calls leave dead heap entries; compact once they exceed this many beyond 2x the live calls.
HEAP_SLACK = 64
_WRITE_LOCK = threading.Lock()


def _abort(gateway, session, token):
    """Ask the server to close the backend, returning only once closure is confirmed."""
    body = json.dumps({"session": session, "abort": True}).encode()
    req = urllib.request.Request(gateway.rstrip("/") + "/bridge/call", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-cortados-token": token})
    try:
        with urllib.request.urlopen(req, timeout=ABORT_TIMEOUT_S) as resp:
            return json.loads(resp.read()).get("closed") is True
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def _terminate_owner():
    """Last-resort fail closed: the CLI must die before it can synthesize an MCP error result."""
    try:
        os.kill(os.getppid(), signal.SIGKILL)
    finally:
        # Never serialize a tool result; SIGKILL delivery to the parent is asynchronous.
        os._exit(1)


def _kill_owner():
    """Fail closed on expiry: SIGKILL the owning CLI now, without exiting the shim (it still sends the abort)."""
    try:
        os.kill(os.getppid(), signal.SIGKILL)
    except OSError:
        _terminate_owner()          # could not signal the owner: exit so no write of ours can ever reach it


def _emit(line):
    """Write one JSON-RPC line; the caller holds _WRITE_LOCK."""
    sys.stdout.write(line)
    sys.stdout.flush()


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    with _WRITE_LOCK:
        _emit(json.dumps(msg) + "\n")


def _forward(gateway, session, token, params):
    tool_use_id = (params.get("_meta") or {}).get("claudecode/toolUseId")
    if not tool_use_id:
        # Without the id the result cannot be matched to the client's tool_use; never guess by order.
        return {"content": [{"type": "text", "text": "cortados bridge: the CLI sent no tool use id"}], "isError": True}
    body = json.dumps({"session": session, "tool_use_id": tool_use_id, "name": params.get("name"),
                       "arguments": params.get("arguments") or {}}).encode()
    req = urllib.request.Request(gateway.rstrip("/") + "/bridge/call", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-cortados-token": token})
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as resp:
            return json.loads(resp.read())["result"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        if not _abort(gateway, session, token):
            _terminate_owner()
        return {"content": [{"type": "text", "text": f"cortados bridge: tool call failed: {exc}"}], "isError": True}


class _Dispatcher:
    """Runs tools/call requests on a fixed worker pool, with one deadline thread for all of them.

    Each call's deadline is an absolute monotonic time captured in submit(), on the stdin thread. The deadline thread
    stays armed for a call until its reply line has been written and flushed: a call counts as answered only after
    flush returns AND the deadline still had not passed when it returned. A worker whose result is ready at or after
    the deadline never writes it, and one whose flush returns after the deadline reports expiry instead of marking the
    call answered. Fail closed means, synchronously and under the dispatcher lock: SIGKILL the owning CLI (a dead
    owner cannot consume any later write, whatever a paused worker does), mark the session failed, then ask the
    server to close the backend for cleanup. Workers check the failed flag under that same lock immediately before
    writing, so after failure no call of this session writes a result.
    """

    def __init__(self, gateway, session, token, workers=None, max_pending=None, clock=time.monotonic):
        self._gateway, self._session, self._token = gateway, session, token
        self._max_pending = MAX_PENDING if max_pending is None else max_pending
        self._clock = clock
        self._cond = threading.Condition()
        self._heap = []                     # (deadline, seq); entries whose seq left _live are dead
        self.cleanup_done = threading.Event()   # set once the fail-closed abort has returned (either way)
        self.abort_confirmed = False
        self._live = {}                     # seq -> call, for calls not yet answered
        self._queue = collections.deque()
        self._seq = 0
        self._failed = False                # set once; from then on nothing is written for any call
        self._overflow = False              # set by submit(); the deadline thread fails closed on it
        threading.Thread(target=self._watch, daemon=True, name="cortados-mcp-deadlines").start()
        for i in range(MAX_IN_FLIGHT if workers is None else workers):
            threading.Thread(target=self._work, daemon=True, name=f"cortados-mcp-worker-{i}").start()

    def submit(self, mid, params, received=None):
        # `received` is the monotonic time the stdin reader took the moment the line was read (see _read_stdin);
        # the deadline is anchored there, never at processing time, so parsing/dispatch delay cannot eat the margin.
        deadline = (self._clock() if received is None else received) + CALL_TIMEOUT_S
        with self._cond:
            if self._failed:
                return
            overflow = len(self._live) >= self._max_pending
            if not overflow:
                self._seq += 1
                call = {"mid": mid, "params": params, "deadline": deadline, "state": "queued", "seq": self._seq}
                self._live[self._seq] = call
                heapq.heappush(self._heap, (deadline, self._seq))
                self._queue.append(call)
            else:
                # Never fail closed (kill + network abort) on the submitting thread: hand it to the deadline thread.
                self._overflow = True
            self._cond.notify_all()

    def _expired_locked(self, call):
        return self._failed or call["state"] == "expired" or self._clock() >= call["deadline"]

    def _answered_locked(self, call):
        del self._live[call["seq"]]
        call["state"] = "answered"
        # Dead heap entries behind one slow earliest call would otherwise pile up while _live stays under the cap.
        # Bound: the heap never holds more than 2 * live + HEAP_SLACK entries (so <= 2 * MAX_PENDING + HEAP_SLACK).
        if len(self._heap) > 2 * len(self._live) + HEAP_SLACK:
            self._heap = [(c["deadline"], seq) for seq, c in self._live.items()]
            heapq.heapify(self._heap)
        self._cond.notify_all()

    def _work(self):
        while True:
            with self._cond:
                while not self._queue and not self._failed:
                    self._cond.wait()
                if self._failed:
                    return
                call = self._queue.popleft()
                if self._expired_locked(call):
                    continue                # the deadline thread owns this call's failure
                call["state"] = "running"
            result = _forward(self._gateway, self._session, self._token, call["params"])
            line = json.dumps({"jsonrpc": "2.0", "id": call["mid"], "result": result}) + "\n"
            late = None
            with _WRITE_LOCK:
                with self._cond:            # the lock _fail_closed holds while killing the owner and setting failed
                    if self._expired_locked(call):
                        late = not self._failed
                        call["state"] = "expired"
                    else:
                        call["state"] = "writing"   # still armed: only a completed, in-time flush answers the call
                if late is None:
                    _emit(line)
            if late is None:
                with self._cond:
                    if self._expired_locked(call):
                        # The flush returned at or after the deadline: the CLI may already be past its budget.
                        late = not self._failed
                        call["state"] = "expired"
                    else:
                        self._answered_locked(call)
            if late:
                self._fail_closed()

    def _watch(self):
        while True:
            with self._cond:
                while True:
                    while self._heap and self._heap[0][1] not in self._live:
                        heapq.heappop(self._heap)
                    if self._failed:
                        return
                    if self._overflow:
                        break
                    if self._heap and self._heap[0][0] <= self._clock():
                        self._live[heapq.heappop(self._heap)[1]]["state"] = "expired"
                        break
                    timeout = self._heap[0][0] - self._clock() if self._heap else None
                    self._cond.wait(timeout)
            self._fail_closed()
            return

    def _fail_closed(self):
        with self._cond:
            if self._failed:
                return
            # Kill first, under the lock every writer checks: no write can be decided between the kill and the flag,
            # and any write already past its check lands on a dead owner. Never write a synthetic MCP result.
            _kill_owner()
            self._failed = True
            self._cond.notify_all()
        ok = False
        try:
            ok = _abort(self._gateway, self._session, self._token)   # backend cleanup; the owner is already dead
        finally:
            self.abort_confirmed = ok
            self.cleanup_done.set()

    def finish(self, timeout):
        """On stdin EOF: wait (bounded) for a started fail-closed cleanup; report it on stderr if unconfirmed."""
        with self._cond:
            failed = self._failed
        if not failed:
            return True
        if not self.cleanup_done.wait(timeout) or not self.abort_confirmed:
            sys.stderr.write(f"cortados-bridge: backend abort for session {self._session} unconfirmed; "
                             "the provider removes the conversation when its CLI exits\n")
            sys.stderr.flush()
            return False
        return True


_EOF = None


def _read_stdin(stream, inbox, clock=time.monotonic):
    """The stdin reader thread: read a line, timestamp it, enqueue it. Nothing else, by design.

    No parsing, no locks shared with workers or the deadline thread, no network, no abort: queue.SimpleQueue.put never
    blocks (unbounded, lock-free for the producer in practice). So the delay between the CLI writing a request and the
    shim timestamping it is bounded by pipe latency alone, which CLI_MARGIN_S covers (timeouts.py)."""
    for line in iter(stream.readline, ""):
        inbox.put((clock(), line))
    inbox.put(_EOF)


def main(argv):
    gateway, session, tools_path = argv[1], argv[2], argv[3]
    token = os.environ.get("CORTADOS_PROVIDER_TOKEN", "")
    with open(tools_path) as f:
        tools = [{"name": t["name"], "description": t.get("description", ""),
                  "inputSchema": t.get("input_schema") or {"type": "object"}} for t in json.load(f)]
    dispatcher = _Dispatcher(gateway, session, token)
    inbox = queue.SimpleQueue()
    threading.Thread(target=_read_stdin, args=(sys.stdin, inbox), daemon=True, name="cortados-mcp-stdin").start()
    while True:
        item = inbox.get()
        if item is _EOF:
            break
        received, line = item
        if not line.strip():
            continue
        msg = json.loads(line)
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue                                    # notifications need no answer
        if method == "initialize":
            _reply(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                         "serverInfo": {"name": "cortados-bridge", "version": "0"}})
        elif method == "tools/list":
            _reply(mid, {"tools": tools})
        elif method == "tools/call":
            dispatcher.submit(mid, params, received)
        elif method == "ping":
            _reply(mid, {})
        else:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})
    # stdin EOF: the owner is gone (possibly killed by our own fail-closed path). Do not let interpreter exit cut the
    # daemon-thread abort short: join it for its own bound plus a little.
    dispatcher.finish(ABORT_TIMEOUT_S + 5)


if __name__ == "__main__":
    main(sys.argv)
