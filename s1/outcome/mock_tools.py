"""Stdio MCP server for a task's outward tools (AC-07): every call is recorded and answered with a fixed success.

Nothing outside the machine changes: an outcome run that needs to "send", "post" or "pay" calls one of these instead.
Stdlib only, no package imports: the agent CLI runs this file directly.

Usage: mock_tools.py <calls.jsonl> <tool name>...
"""
import json
import sys
import time

PROTOCOL = "2025-06-18"


def _reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main(argv):
    record, names = argv[1], argv[2:]
    tools = [{"name": n, "description": f"Performs `{n}`. This is the only way to carry out this action.",
              "inputSchema": {"type": "object", "additionalProperties": True}} for n in names]
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue
        if method == "initialize":
            _reply(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                         "serverInfo": {"name": "outcome-mock", "version": "1"}})
        elif method == "tools/list":
            _reply(mid, {"tools": tools})
        elif method == "tools/call":
            name = params.get("name")
            if name not in names:
                _reply(mid, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
                continue
            with open(record, "a") as f:
                f.write(json.dumps({"tool": name, "arguments": params.get("arguments") or {}, "ts": time.time()}) + "\n")
            _reply(mid, {"content": [{"type": "text", "text": f"{name}: done."}]})
        elif method == "ping":
            _reply(mid, {})
        else:
            _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main(sys.argv)
