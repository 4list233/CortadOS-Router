"""Stand-in for `claude -p` in stream-json mode, for the provider bridge tests. Stdlib only.

Starts the `bridge` MCP server from --mcp-config like the real CLI, then for each user line on stdin:
  - "use two tools": one assistant message with two tool_use blocks for the first tool, called serially;
  - "use tool": one tool_use (input {"file_path": "f0.md"}), called through MCP with _meta.claudecode/toolUseId;
    either way it then answers "tool said: <result texts>";
  - "garbled" first emits a tool_use of a tool that does not exist and answers it with the CLI's own tool error,
    then carries on as above; "misnamed" does the same with a wrong tool under the bridge's prefix
    (mcp__bridge__Bsh); "ran locally" answers the garbled call with a NON-error result, as if the CLI ran it;
  - otherwise it answers "echo: <text>".
Every turn ends with a result event. Writes its argv and relevant env to $FAKE_CLAUDE_ARGV when set, and its argv and
the bridge's tools/list names to $FAKE_CLAUDE_TOOLS_DIR/<pid>.json when that is set (one file per backend process).
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
if os.environ.get("FAKE_CLAUDE_TOOLS_DIR"):
    with open(os.path.join(os.environ["FAKE_CLAUDE_TOOLS_DIR"], f"{os.getpid()}.json"), "w") as f:
        json.dump({"argv": args, "tools": tools}, f)


def out(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def assistant(blocks):
    out({"type": "assistant", "message": {"role": "assistant", "content": blocks,
                                          "usage": {"input_tokens": 10, "output_tokens": 5}}})


def check_cache(value):
    """Reject leaked client cache metadata, including serialized fallback blocks."""
    if '"cache_control"' in json.dumps(value).replace('\\\"', '"'):
        out({"type": "result", "is_error": True, "result": "client cache_control reached backend"})
        sys.exit(0)


for line in sys.stdin:
    msg = json.loads(line)["message"]
    check_cache(msg)
    text = " ".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")
    if "backend failure" in text:
        out({"type": "result", "is_error": True, "result": "API Error: 400 cache TTL ordering"})
        continue
    n = 2 if "use two tools" in text else 1 if "use tool" in text else 0
    if "garbled" in text or "misnamed" in text or "ran locally" in text:
        # A model calls a tool that does not exist; the real CLI answers it with a tool error and the model goes on.
        bad = "toolu_" + uuid.uuid4().hex[:12]
        name = "mcp__bridge__Bsh" if "misnamed" in text else "mcp__bashbridge__Bash"
        assistant([{"type": "tool_use", "id": bad, "name": name, "input": {"command": "ls"}}])
        if "ran locally" in text:
            result = {"type": "tool_result", "tool_use_id": bad, "content": [{"type": "text", "text": "a.md b.md"}]}
        else:
            result = {"type": "tool_result", "tool_use_id": bad, "is_error": True,
                      "content": f"<tool_use_error>Error: No such tool available: {name}</tool_use_error>"}
        out({"type": "user", "message": {"role": "user", "content": [result]}})
    if n and tools:
        uses = [{"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:12], "name": "mcp__bridge__" + tools[0],
                 "input": {"file_path": f"f{i}.md"}} for i in range(n)]
        assistant([{"type": "text", "text": "Let me look."}] + uses)
        said = []
        for u in uses:
            r = rpc("tools/call", {"name": tools[0], "arguments": u["input"], "_meta": {"claudecode/toolUseId": u["id"]}})
            check_cache(r)
            said.append(("ERR " if r.get("isError") else "") + " ".join(c.get("text", "") for c in r["content"]))
        assistant([{"type": "text", "text": "tool said: " + " | ".join(said)}])
    else:
        assistant([{"type": "text", "text": "echo: " + text}])
    out({"type": "result", "subtype": "success", "is_error": False, "result": "done"})
