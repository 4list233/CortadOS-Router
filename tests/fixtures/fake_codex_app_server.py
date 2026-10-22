"""Stand-in for `codex app-server` (Codex CLI 0.163.0) over stdio, for the provider Codex bridge tests. Stdlib only.

Speaks the line shapes of the real stream captured in the P2 spike: one JSON object per line, server request ids are
integers from 0, replies echo the client's string ids. Triggers on the text of turn/start:
  "use two tools"  two item/tool/call requests outstanding at once for the first dynamic tool, then an agentMessage
                   "tool said: <reply texts joined by ' | ', failed replies prefixed 'ERR '>";
                   with "slowly" in the text the second call follows the first after SLOW_SIBLING_S (a long
                   function_call payload still streaming);
  "use tool"       one such call ({"file_path": "f0.md"}) and the same answer;
  "complete around call"  one call, then after 0.5 s a turn/completed with status completed while the call is still
                   outstanding (the client's result is read and dropped afterwards): a turn that ends around a call;
  "fail"           turn/completed with status failed and codexErrorInfo usageLimitExceeded;
  "leak builtin"   an item/started carrying a commandExecution item (a built-in tool ran), then a normal completion;
  "reroute"        a model/rerouted notification (Codex moved the turn to another model), then "echo: <text>";
  "ask user"       an item/tool/requestUserInput server request, then "echo: <text>";
  otherwise        "echo: <text>".
A turn/interrupt that arrives while a turn waits on the client ends that turn with status interrupted.
Env: FAKE_CODEX_ARGV dumps {"argv","cwd","env"}; FAKE_CODEX_LOG appends every inbound line; FAKE_CODEX_VERSION is
the reported thread.cliVersion (default 0.163.0); FAKE_CODEX_MODEL makes thread/start answer that model instead of
the requested one; FAKE_CODEX_SANDBOX makes it answer that sandbox type instead of readOnly; FAKE_CODEX_LEAK_MCP
emits mcpServer/startupStatus/updated before the thread/start reply, like a configured MCP server starting;
FAKE_CODEX_ACCOUNT is the JSON `account` object account/read answers (default a chatgpt pro login; "null" = no login);
FAKE_CODEX_CONFIG_PATCH is JSON deep-merged over the effective config that config/read answers, which is otherwise
built from this process's own `-c` overrides and `--disable` flags exactly as the real server reflects them.
thread/start with dynamicTools and no experimentalApi
capability is refused with the real -32600 error. Token usage: every model request adds 100 input / 10 output tokens
to the thread total, reported as the real server does, after the tool results and after the final message.
"""
import itertools
import json
import os
import select
import sys
import time
import uuid

VERSION = os.environ.get("FAKE_CODEX_VERSION", "0.163.0")
LOG = os.environ.get("FAKE_CODEX_LOG")
SLOW_SIBLING_S = 0.6                        # longer than bridge.SETTLE_S (0.3), shorter than codex_bridge.CODEX_SETTLE_S
ENV_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CODEX_HOME")
# Every tool feature the real registry knows; `--disable` turns one off, the rest keep their stable default (on).
FEATURES = ("shell_tool", "unified_exec", "unified_exec_tty", "view_image", "image_generation", "apps", "plugins", "hooks",
            "multi_agent", "sleep_tool", "skill_search", "tool_suggest", "goals", "memories", "browser_use", "computer_use",
            "collaboration_modes", "code_mode_host")


def toml_value(raw):
    """The value of a `-c key=value` override: TOML scalars and the empty array, which JSON reads the same way."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw.strip('"')


def effective_config(argv):
    """What config/read answers: the defaults of a real 0.163.0 home with every -c override and --disable applied."""
    cfg = {"model_provider": None, "model_providers": {}, "chatgpt_base_url": "https://chatgpt.com/backend-api/",
           "openai_base_url": None, "model_catalog_json": None,
           "mcp_servers": {}, "notify": None, "web_search": None, "include_environment_context": True,
           "project_doc_max_bytes": 32768, "features": {f: True for f in FEATURES}}
    for i, a in enumerate(argv):
        if a == "--disable" and i + 1 < len(argv):
            cfg["features"][argv[i + 1]] = False
        if a == "--enable" and i + 1 < len(argv):
            cfg["features"][argv[i + 1]] = True
        if a == "-c" and i + 1 < len(argv) and "=" in argv[i + 1]:
            key, _, raw = argv[i + 1].partition("=")
            parts, node = key.split("."), cfg
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = toml_value(raw)
    patch = os.environ.get("FAKE_CODEX_CONFIG_PATCH")
    if patch:
        def merge(into, over):
            for k, v in over.items():
                into[k] = merge(into.get(k) or {}, v) if isinstance(v, dict) and isinstance(into.get(k), dict) else v
            return into
        merge(cfg, json.loads(patch))
    return cfg
if os.environ.get("FAKE_CODEX_ARGV"):
    with open(os.environ["FAKE_CODEX_ARGV"], "w") as f:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "env": {k: os.environ.get(k) for k in ENV_KEYS}}, f)

stdin = os.fdopen(0, "rb", buffering=0)     # unbuffered, so select() sees exactly what readline() has not consumed
server_ids = itertools.count()
state = {"experimental": False, "tools": [], "thread": None, "total": {"inputTokens": 0, "outputTokens": 0,
                                                                        "cachedInputTokens": 0, "totalTokens": 0}}


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def notify(method, params):
    out({"method": method, "params": params, "emittedAtMs": int(time.time() * 1000)})


def read(timeout=None):
    """The next inbound message; None at EOF or when nothing arrives within `timeout` seconds."""
    if timeout is not None and not select.select([stdin], [], [], timeout)[0]:
        return None
    line = stdin.readline()
    if not line:
        return None
    if LOG:
        with open(LOG, "a") as f:
            f.write(line.decode())
    return json.loads(line)


def usage(turn_id):
    t = state["total"]
    last = {"inputTokens": 100, "outputTokens": 10, "cachedInputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 110}
    for k, v in last.items():
        t[k] = t.get(k, 0) + v
    notify("thread/tokenUsage/updated", {"threadId": state["thread"], "turnId": turn_id,
                                         "tokenUsage": {"total": dict(t), "last": last, "modelContextWindow": 258400}})


def item(turn_id, method, it):
    notify(method, {"item": it, "threadId": state["thread"], "turnId": turn_id, "completedAtMs": int(time.time() * 1000)})


def completed(turn_id, status, items=(), error=None):
    notify("turn/completed", {"threadId": state["thread"], "turn": {"id": turn_id, "rootTurnId": turn_id, "items": list(items),
                                                                     "itemsView": "summary", "status": status, "error": error}})


def interrupted(msg, turn_id):
    """Handle a request that arrives while a turn is active. True if the turn is over (interrupt or unsubscribe)."""
    mid, method = msg.get("id"), msg.get("method")
    if method == "turn/interrupt":
        out({"id": mid, "result": {}})
        completed(turn_id, "interrupted")
        return True
    if method == "thread/unsubscribe":
        out({"id": mid, "result": {"status": "unsubscribed"}})
        return True
    if mid is not None:
        out({"id": mid, "error": {"code": -32600, "message": f"{method} is not accepted while a turn is active"}})
    return False


def wait_replies(rids, turn_id):
    """Block until every server request in `rids` has its reply. None when the turn was ended instead."""
    replies = {}
    while len(replies) < len(rids):
        msg = read()
        if msg is None:
            sys.exit(0)                                 # stdin closed
        if "method" not in msg:
            if msg.get("id") in rids:
                replies[msg["id"]] = msg.get("result") or {"success": False, "contentItems": [
                    {"type": "inputText", "text": "error reply: " + json.dumps(msg.get("error"))}]}
            continue
        if interrupted(msg, turn_id):
            return None
    return replies


def agent_message(turn_id, text):
    it = {"type": "agentMessage", "id": "msg_" + uuid.uuid4().hex[:12], "text": "", "phase": "final_answer"}
    item(turn_id, "item/started", it)
    notify("item/agentMessage/delta", {"threadId": state["thread"], "turnId": turn_id, "itemId": it["id"], "delta": text})
    it = dict(it, text=text)
    item(turn_id, "item/completed", it)
    return it


def run_turn(mid, params):
    turn_id = str(uuid.uuid4())
    text = " ".join(i.get("text", "") for i in params.get("input") or [] if i.get("type") == "text")
    turn = {"id": turn_id, "rootTurnId": turn_id, "items": [], "itemsView": "notLoaded", "status": "inProgress", "error": None}
    out({"id": mid, "result": {"turn": turn}})
    notify("turn/started", {"threadId": state["thread"], "turn": turn})
    user = {"type": "userMessage", "id": str(uuid.uuid4()), "clientId": None, "content": [{"type": "text", "text": text}]}
    item(turn_id, "item/started", user)
    item(turn_id, "item/completed", user)
    if "fail" in text:
        completed(turn_id, "failed", error={"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"})
        return
    if "leak builtin" in text:
        item(turn_id, "item/started", {"type": "commandExecution", "id": "cmd-1", "command": "ls", "status": "inProgress"})
        completed(turn_id, "completed")
        return
    if "reroute" in text:
        notify("model/rerouted", {"threadId": state["thread"], "turnId": turn_id, "fromModel": "gpt-test",
                                  "toModel": "gpt-other", "reason": "modelNotFound"})
    if "complete around call" in text and state["tools"]:
        call_id, rid, args = "exec-" + str(uuid.uuid4()), next(server_ids), {"file_path": "f0.md"}
        item(turn_id, "item/started", {"type": "dynamicToolCall", "id": call_id, "namespace": None, "tool": state["tools"][0],
                                       "arguments": args, "status": "inProgress", "contentItems": None, "success": None})
        out({"method": "item/tool/call", "id": rid, "params": {"threadId": state["thread"], "turnId": turn_id,
                                                               "callId": call_id, "namespace": None,
                                                               "tool": state["tools"][0], "arguments": args}})
        time.sleep(0.5)
        completed(turn_id, "completed")
        wait_replies({rid}, turn_id)                    # the late result is read so the pipe never fills, and dropped
        return
    if "ask user" in text:
        rid = next(server_ids)
        out({"method": "item/tool/requestUserInput", "id": rid, "params": {
            "threadId": state["thread"], "turnId": turn_id, "itemId": "ask-1", "isBlocking": True,
            "questions": [{"id": "q1", "header": "Which?", "question": "Which file?"}]}})
        if wait_replies({rid}, turn_id) is None:
            return
    n = 2 if "use two tools" in text else 1 if "use tool" in text else 0
    if n and state["tools"]:
        calls = []
        for i in range(n):
            if i and "slowly" in text:
                time.sleep(SLOW_SIBLING_S)
            call_id, rid, args = "exec-" + str(uuid.uuid4()), next(server_ids), {"file_path": f"f{i}.md"}
            item(turn_id, "item/started", {"type": "dynamicToolCall", "id": call_id, "namespace": None, "tool": state["tools"][0],
                                           "arguments": args, "status": "inProgress", "contentItems": None, "success": None})
            out({"method": "item/tool/call", "id": rid, "params": {"threadId": state["thread"], "turnId": turn_id,
                                                                   "callId": call_id, "namespace": None,
                                                                   "tool": state["tools"][0], "arguments": args}})
            calls.append((rid, call_id, args))
        replies = wait_replies({rid for rid, _, _ in calls}, turn_id)
        if replies is None:
            return
        said = []
        for rid, call_id, args in calls:
            r = replies[rid]
            said.append(("" if r.get("success") else "ERR ") + " ".join(
                c.get("text", "") for c in r.get("contentItems") or [] if c.get("type") == "inputText"))
            item(turn_id, "item/completed", {"type": "dynamicToolCall", "id": call_id, "namespace": None, "tool": state["tools"][0],
                                             "arguments": args, "status": "completed" if r.get("success") else "failed",
                                             "contentItems": r.get("contentItems"), "success": r.get("success"), "durationMs": 0})
        usage(turn_id)
        # The model's next request takes time; an interrupt arriving now ends the turn, as on the real server.
        msg = read(timeout=0.2)
        if msg is not None and interrupted(msg, turn_id):
            return
        final = "tool said: " + " | ".join(said)
    else:
        final = "echo: " + text
    it = agent_message(turn_id, final)
    usage(turn_id)
    completed(turn_id, "completed", items=[it])


def main():
    while True:
        msg = read()
        if msg is None:
            return
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            state["experimental"] = bool((params.get("capabilities") or {}).get("experimentalApi"))
            out({"id": mid, "result": {"userAgent": "fake-codex/0.163.0", "codexHome": os.environ.get("CODEX_HOME", "~/.codex"),
                                       "platformFamily": "unix", "platformOs": "macos"}})
        elif method == "initialized":
            notify("remoteControl/status/changed", {"status": "disabled", "serverName": "fake", "installationId": "x"})
            notify("account/updated", {"authMode": "chatgpt", "planType": "pro"})
        elif method == "account/read":
            account = json.loads(os.environ.get("FAKE_CODEX_ACCOUNT") or '{"type": "chatgpt", "email": "x@example.invalid", "planType": "pro"}')
            out({"id": mid, "result": {"account": account, "requiresOpenaiAuth": True}})
        elif method == "config/read":
            out({"id": mid, "result": {"config": effective_config(sys.argv[1:]), "origins": {}, "layers": []}})
        elif method == "thread/start":
            if params.get("dynamicTools") is not None and not state["experimental"]:
                out({"id": mid, "error": {"code": -32600, "message": "thread/start.dynamicTools requires experimentalApi capability"}})
                continue
            state["tools"] = [t["name"] for t in params.get("dynamicTools") or []]
            state["thread"] = str(uuid.uuid4())
            if os.environ.get("FAKE_CODEX_LEAK_MCP"):
                notify("mcpServer/startupStatus/updated", {"name": "leaky-server", "status": {"type": "starting"}})
            model = os.environ.get("FAKE_CODEX_MODEL") or params.get("model")
            thread = {"id": state["thread"], "sessionId": state["thread"], "preview": "", "ephemeral": params.get("ephemeral"),
                      "model": model, "modelProvider": "openai", "createdAt": int(time.time()),
                      "updatedAt": int(time.time()), "status": {"type": "idle"}, "cwd": params.get("cwd"),
                      "cliVersion": VERSION, "originator": "cortados-provider", "source": "vscode", "turns": []}
            out({"id": mid, "result": {"thread": thread, "model": model, "modelProvider": "openai",
                                       "cwd": params.get("cwd"), "instructionSources": [],
                                       "approvalPolicy": params.get("approvalPolicy"),
                                       "sandbox": {"type": os.environ.get("FAKE_CODEX_SANDBOX") or "readOnly", "networkAccess": False}}})
            notify("thread/started", {"thread": thread})
        elif method == "turn/start":
            run_turn(mid, params)
        elif method == "turn/interrupt":
            out({"id": mid, "error": {"code": -32600, "message": "no active turn to interrupt"}})
        elif method == "thread/unsubscribe":
            out({"id": mid, "result": {"status": "unsubscribed"}})
        elif mid is not None:
            out({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
