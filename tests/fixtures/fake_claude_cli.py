"""Stand-in for `claude -p` in stream-json mode, for the provider bridge tests. Stdlib only.

Starts the `bridge` MCP server from --mcp-config like the real CLI, then for each user line on stdin:
  - "use two tools": one assistant message with two tool_use blocks for the first tool, called serially;
  - "use tool": one tool_use (input {"file_path": "f0.md"}), called through MCP with _meta.claudecode/toolUseId;
    either way it then answers "tool said: <result texts>";
  - otherwise it answers "echo: <text>".
Every turn ends with a result event. Writes its argv and relevant env to $FAKE_CLAUDE_ARGV when set.
With $FAKE_CLAUDE_STALL set it never reads stdin (a hung CLI).
"""
import json
import os
import subprocess
import sys
import time
import uuid

if os.environ.get("FAKE_CLAUDE_STALL"):
    time.sleep(120)

args = sys.argv[1:]
if os.environ.get("FAKE_CLAUDE_ARGV"):
    with open(os.environ["FAKE_CLAUDE_ARGV"], "w") as f:
        keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MCP_TOOL_TIMEOUT")
        json.dump({"argv": args, "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in keys}}, f)
mcp = None
if "--mcp-config" in args:
    cfg = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["bridge"]
    mcp = subprocess.Popen([cfg["command"]] + cfg["args"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                           env=dict(os.environ, **cfg.get("env", {})))
_id = [0]


def rpc(method, params):
    _id[0] += 1
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": _id[0], "method": method, "params": params}) + "\n")
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())["result"]


tools = []
if mcp:
    rpc("initialize", {"protocolVersion": "2025-06-18"})
    tools = [t["name"] for t in rpc("tools/list", {})["tools"]]


def out(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def assistant(blocks):
    out({"type": "assistant", "message": {"role": "assistant", "content": blocks,
                                          "usage": {"input_tokens": 10, "output_tokens": 5}}})


for line in sys.stdin:
    msg = json.loads(line)["message"]
    text = " ".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")
    n = 2 if "use two tools" in text else 1 if "use tool" in text else 0
    if n and tools:
        uses = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": "mcp__bridge__" + tools[0],
                 "input": {"file_path": f"f{i}.md"}} for i in range(n)]
        assistant([{"type": "text", "text": "Let me look."}] + uses)
        said = []
        for u in uses:
            r = rpc("tools/call", {"name": tools[0], "arguments": u["input"], "_meta": {"claudecode/toolUseId": u["id"]}})
            said.append(("ERR " if r.get("isError") else "") + " ".join(c.get("text", "") for c in r["content"]))
        assistant([{"type": "text", "text": "tool said: " + " | ".join(said)}])
    else:
        assistant([{"type": "text", "text": "echo: " + text}])
    out({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
