"""Stdio MCP server that the headless CLI loads as `bridge`: it lists the client's tools and forwards each call.

Spawned by the CLI, one per bridge session. A `tools/call` is POSTed to the endpoint's /bridge/call, which blocks
until the client has run the tool and sent the result back. Stdlib only, no package imports: the CLI runs this
file directly.

Usage: mcp_shim.py <gateway url> <session id> <tools.json>; the endpoint token comes in CORTADOS_PROVIDER_TOKEN.
"""
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
# Client tools include long test suites and permission prompts. Three hours covers the maximum task runtime;
# finite expiry still releases abandoned calls. This value is also used by the registry and passed to the CLI.
TOOL_WAIT_S = float(os.environ.get("CORTADOS_PROVIDER_TOOL_WAIT_S") or 10800)
if not 0 < TOOL_WAIT_S < float("inf"):
    raise ValueError("CORTADOS_PROVIDER_TOOL_WAIT_S must be positive and finite")
# Transport slack lets the server return its timeout diagnostic before the shim/CLI abandon the call.
CALL_TIMEOUT_S = TOOL_WAIT_S + 120


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
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
        return {"content": [{"type": "text", "text": f"cortados bridge: tool call failed: {exc}"}], "isError": True}


def main(argv):
    gateway, session, tools_path = argv[1], argv[2], argv[3]
    token = os.environ.get("CORTADOS_PROVIDER_TOKEN", "")
    with open(tools_path) as f:
        tools = [{"name": t["name"], "description": t.get("description", ""),
                  "inputSchema": t.get("input_schema") or {"type": "object"}} for t in json.load(f)]
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
            _reply(mid, _forward(gateway, session, token, params))
        elif method == "ping":
            _reply(mid, {})
        else:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main(sys.argv)
