"""Stdio MCP server that the headless CLI loads as `bridge`: it lists the client's tools and forwards each call.

Spawned by the CLI, one per bridge session. A `tools/call` is POSTed to the endpoint's /bridge/call, which blocks
until the client has run the tool and sent the result back. Stdlib only, no package imports: the CLI runs this
file directly.

Usage: mcp_shim.py <gateway url> <session id> <tools.json>; the endpoint token comes in CORTADOS_PROVIDER_TOKEN.
"""
import json
import os
import signal
import sys
import threading
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
CALL_TIMEOUT_S = TURN_TIMEOUT_S + TOOL_WAIT_S + CALL_SLACK_S
# The provider resolves compatibility aliases and supplies all budgets explicitly to this standalone process.
ABORT_TIMEOUT_S = float(os.environ.get("CORTADOS_PROVIDER_ABORT_TIMEOUT_S") or 120)
if not 0 < ABORT_TIMEOUT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_ABORT_TIMEOUT_S must be positive and finite")

# Claude may issue sibling calls together. Never make a later call spend its CLI timeout queued behind an earlier
# HTTP request. The semaphore limits transport fan-out; each call's watchdog starts before that semaphore.
MAX_IN_FLIGHT = 16
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


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


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


def _dispatch_call(mid, gateway, session, token, params, slots):
    """Run independently; arm the fail-closed clock as soon as this stdin request is observed."""
    finished = threading.Event()
    state_lock = threading.Lock()
    state = {"expired": False}

    def expire():
        if finished.wait(CALL_TIMEOUT_S):
            return
        with state_lock:
            if finished.is_set():
                return
            state["expired"] = True
        # A confirmed abort closed the owning CLI. If it cannot be confirmed, kill that CLI here. Never write a
        # synthetic MCP result once this request's budget has expired.
        if not _abort(gateway, session, token):
            _terminate_owner()

    def run():
        with slots:
            with state_lock:
                if state["expired"]:
                    return
            result = _forward(gateway, session, token, params)
        with state_lock:
            if state["expired"]:
                return
            finished.set()
        _reply(mid, result)

    threading.Thread(target=expire, daemon=True, name=f"cortados-mcp-deadline-{mid}").start()
    threading.Thread(target=run, daemon=True, name=f"cortados-mcp-call-{mid}").start()


def main(argv):
    gateway, session, tools_path = argv[1], argv[2], argv[3]
    token = os.environ.get("CORTADOS_PROVIDER_TOKEN", "")
    with open(tools_path) as f:
        tools = [{"name": t["name"], "description": t.get("description", ""),
                  "inputSchema": t.get("input_schema") or {"type": "object"}} for t in json.load(f)]
    slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
    for line in sys.stdin:
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
            _dispatch_call(mid, gateway, session, token, params, slots)
        elif method == "ping":
            _reply(mid, {})
        else:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main(sys.argv)
