"""Stdio MCP server that gives a Claude Code (or any MCP client) session ONE tool, `route_task`, for delegating a
self-contained subtask to the CortadOS router. The main session keeps its own model; when it wants to offload work it
calls this tool, which posts the subtask to the `cortados` provider endpoint (S1 scores it, the router picks the
cheapest capable lane in the resource pool, and the chosen model answers), and returns the answer as text. It does not
replace the session's model or touch its login.

It also serves the child-task tools (start_task, task_status, wait_task, list_tasks, cancel_task): whole tasks run as
separate headless Claude Code sessions on this machine (see cortados_router/child_tasks.py, imported lazily because it
needs the package; route_task does not).

Stdlib only; runs standalone (the MCP client launches it as a subprocess over stdio); route_task imports nothing from the
package. It forwards to the provider — it does NOT need to sit next to S1 or the provider; it only needs to reach the
provider URL.

Register with Claude Code, e.g. (the token is read from ~/.config/cortados/provider-token by default, so only the URL
need be set; CORTADOS_PROVIDER_TOKEN or CORTADOS_PROVIDER_TOKEN_FILE override it):
  claude mcp add cortados-route \\
    --env CORTADOS_PROVIDER_URL=http://<provider-ip>:8432 \\
    -- python3 /path/to/cortados_router/subagent_mcp.py
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = {"2025-06-18"}   # only versions whose required behaviour (no mandatory JSON-RPC batching) we implement
TIMEOUT_S = float(os.environ.get("CORTADOS_ROUTE_TIMEOUT_S", "300"))   # a routed subtask can take a while (cold lane, large output)
DEFAULT_MAX_TOKENS = 8000
MIN_TOKEN_LEN = 8                 # shorter is not a provider token (see route_task)

TOOL = {
    "name": "route_task",
    "description": ("Delegate ONE self-contained subtask to the CortadOS router and get the answer back as text. S1 "
                    "scores the subtask and routes it to the cheapest capable model in the resource pool; the chosen "
                    "model runs it and returns its answer. Use this to offload work (analysis, drafting, a focused "
                    "reasoning or coding question) when you want a result, not to run tools on this machine. The "
                    "delegate has NO access to this conversation or your files — put everything it needs in `task`."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The complete, self-contained subtask or question to delegate."},
            "context": {"type": "string", "description": "Optional extra input or background to include with the task."},
            "max_tokens": {"type": "integer", "description": f"Max tokens for the answer (default {DEFAULT_MAX_TOKENS})."},
            "model": {"type": "string", "description": "Optional. Omitted or 'cortados': S1 routes the subtask. Otherwise "
                      "a pinnable model or alias (fable, opus, sonnet, claude-*, gpt-*), sent as sub/<id> (no S1)."},
        },
        "required": ["task"],
    },
}


# Whole-task delegation: a separate headless Claude Code session on this machine (cortados_router/child_tasks.py).
_MODES = ["auto", "acceptEdits", "manual", "plan", "dontAsk"]   # mirrors child_tasks.PERMISSION_MODES (checked by a test)
CHILD_TOOLS = [
    {"name": "start_task",
     "description": ("Start a WHOLE task as a separate headless Claude Code session on this machine, routed through "
                     "CortadOS (S1 picks the model per turn unless an orchestrator model is pinned). The child has every "
                     "tool and MCP server the user's Claude Code has, works in `cwd`, and runs unattended; only its "
                     "final result comes back. Returns a task_id at once: poll with task_status or block with wait_task. "
                     "Put everything the child needs in `task`: it does not see this conversation."),
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "The complete, self-contained task prompt."},
         "cwd": {"type": "string", "description": "Absolute path of an existing directory the child works in."},
         "orchestrator": {"type": "string", "description": "'cortados' (default: S1 routes every turn) or a pinnable "
                          "model such as fable, opus, sonnet, gpt-6.1-sol: that pins the child's MAIN model; the "
                          "child's subagents are still routed by S1."},
         "permission_mode": {"type": "string", "enum": _MODES, "description": "auto (default where the model "
                             "supports it): a safety classifier approves or denies each action (haiku models other "
                             "than claude-haiku-5-5 have no auto mode: unset, they get acceptEdits; 'auto' is refused); acceptEdits: edits in cwd allowed, other approval-needing "
                             "tools denied; manual: approval-needing tools denied; dontAsk: only pre-approved tools "
                             "run; plan: read-only. bypassPermissions is deliberately not offered."},
         "chrome": {"type": "boolean", "description": "Opt-in (default false): LOGIN MODE. The child runs directly on "
                    "the user's claude.ai login (NOT routed by S1; spends the main Claude subscription) so Claude in "
                    "Chrome loads; it acts in the user's real signed-in Chrome in its own tab group. Model: cortados -> "
                    "claude-sonnet-5-5, a claude-* or sub/claude-* model as given; non-Claude models are refused. "
                    "osascript/open -a/Playwright/Puppeteer are blocked. connectors are refused (the login loads the "
                    "user's claude.ai connectors). Refused if Claude settings set an API key/token/base URL."},
         "connectors": {"type": "array", "items": {"type": "string"}, "description": "Optional: names of servers "
                        "from ~/.config/cortados/connectors.mcp.json the child needs (default none). Each server's "
                        "tool schemas are sent with every request; loading them all can exceed the model's context "
                        "(the provider then refuses with 413)."},
         "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Optional extra tool patterns "
                           "pre-approved for the child, e.g. ['Bash(npm test:*)', 'Bash(git diff:*)']."}},
         "required": ["task", "cwd"]}},
    {"name": "task_status",
     "description": "State of a child task (running/succeeded/failed/cancelled), elapsed time, models used, and the final "
                    "result text when done (bounded; the full log path is included).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "wait_task"},              # built at tools/list time by _wait_task_tool(): its cap comes from the env
    {"name": "list_tasks",
     "description": "Most recent child tasks, newest first, with state and a short preview.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "1-200, default 20"}}}},
    {"name": "cancel_task",
     "description": "Terminate a running child task (its own process group only).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
]
_CHILD_NAMES = {t["name"] for t in CHILD_TOOLS}


def _wait_task_tool() -> dict:
    """The wait_task entry with the cap in force now (CORTADOS_WAIT_MAX_S, or the 50 s default) interpolated. A bad
    CORTADOS_WAIT_MAX_S is named in the description; wait_task itself then fails with the same error."""
    try:
        ct = _child_module()
        default = f"{ct.WAIT_MAX_S:g}"
        try:
            cap, bad = f"{ct.wait_max_s():g}", ""
        except ct.TaskError as exc:
            cap, bad = default, f" Misconfigured: {exc}; wait_task returns this error until it is fixed."
    except ImportError:
        cap = default = "50"
        bad = ""
    dflt = default if float(default) <= float(cap) else cap
    return {"name": "wait_task",
            "description": f"Block until a child task ends or timeout_s passes (default {dflt} s, max {cap} s: the "
                           "Claude desktop app times MCP requests out at 60 s; CORTADOS_WAIT_MAX_S raises the cap for "
                           "other clients), then return what task_status returns. If the result says state "
                           "'running', the task is not done: call wait_task again to keep waiting." + bad,
            "inputSchema": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "timeout_s": {"type": "number", "description": f"0-{cap}, default {dflt}"}},
                "required": ["task_id"]}}


def _child_tools() -> list:
    return [_wait_task_tool() if t["name"] == "wait_task" else t for t in CHILD_TOOLS]


def _child_module():
    """Import cortados_router.child_tasks. This file is normally launched as a script, so its directory, not the repo
    root, is on sys.path; the root is added here, lazily, so route_task never depends on the package importing."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from cortados_router import child_tasks
    return child_tasks


def call_child_tool(name: str, a: dict, cfg: dict) -> dict:
    """Run one child-task tool. Errors come back as isError tool results; the provider token is scrubbed from every
    model-visible string (a child could echo it)."""
    token = (cfg.get("token") or "").strip()
    try:
        ct = _child_module()
    except ImportError as exc:
        return _err(f"child tasks unavailable: cannot import cortados_router.child_tasks ({exc})")
    # Redact BOTH secrets: the env token (what route_task uses) and the token file's (what the child launcher uses);
    # they can differ, and a child could echo the one it was given. A token shorter than MIN_TOKEN_LEN is refused
    # outright (route_task refuses it too): it could not be redacted without mangling ordinary words.
    tokens = [token]
    try:
        tokens.append(ct.session.read_token(cfg.get("token_file") or ct.session.DEFAULT_TOKEN_FILE))
    except ct.session.SessionError:
        pass                                    # no token file: nothing more to redact (start_task reports it if needed)
    try:
        red = ct.Redactor(tokens)
    except ct.TaskError as exc:
        return _err(f"{name}: {exc}")
    try:
        if name == "start_task":
            out = ct.start(a.get("task"), a.get("cwd"), a.get("orchestrator") or "cortados",
                           a.get("permission_mode") or None, a.get("allowed_tools"),
                           cfg.get("url", ""), cfg.get("token_file"), a.get("chrome", False), red,
                           a.get("connectors"))
        elif name == "task_status":
            out = ct.status(a.get("task_id"), red, cfg.get("url", ""), token)
        elif name == "wait_task":
            out = ct.wait(a.get("task_id"), a.get("timeout_s"), red, provider_url=cfg.get("url", ""),
                          token=token)              # wait() reads the cap via wait_max_s(); a bad one is a TaskError
        elif name == "list_tasks":
            out = {"tasks": ct.list_recent(a.get("limit", 20))}
        else:
            out = ct.cancel(a.get("task_id"))
    except ct.TaskError as exc:
        return _err(red.text(f"{name}: {exc}"))
    # Redact the structure's strings BEFORE serializing: json.dumps escapes quotes and backslashes, so a token holding
    # either would no longer match once serialized.
    return {"content": [{"type": "text", "text": json.dumps(red.tree(out), indent=2)}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def route_task(provider_url: str, token: str, task: str, context: str = "", max_tokens: int = DEFAULT_MAX_TOKENS,
               urlopen=urllib.request.urlopen, model=None) -> dict:
    """Post the subtask to the provider's /v1/messages and return the routed model's text as an MCP tool result."""
    provider_url, token = (provider_url or "").strip(), (token or "").strip()   # strip stray whitespace/newlines that break header validation
    # Never let the token reach model-visible text: every error below is scrubbed. A token too short to be a provider
    # token (a real one is a long random string) is refused instead of sent, so the scrub never has to choose between
    # leaking a short credential and mangling ordinary words that happen to contain it.
    scrub = lambda s: s.replace(token, "***") if token else s
    if not (provider_url and token):
        return _err("cortados route_task is not configured: set CORTADOS_PROVIDER_URL and CORTADOS_PROVIDER_TOKEN")
    if len(token) < MIN_TOKEN_LEN:
        return _err(f"cortados route_task: the provider token is shorter than {MIN_TOKEN_LEN} characters; it is not a "
                    f"provider token (check CORTADOS_PROVIDER_TOKEN or the token file)")
    if not (token.isascii() and token.isprintable()):
        # Reject a token that is not printable ASCII (control chars such as an embedded newline from a wrapped paste, or
        # non-ASCII) BEFORE building the request: urllib's header validator / latin-1 encoder would otherwise raise with
        # the token's escaped bytes or one of its characters, which the whole-token scrub misses.
        return _err("cortados route_task: CORTADOS_PROVIDER_TOKEN must be printable ASCII (no control characters such as "
                    "an embedded newline, no non-ASCII); fix it")
    if not (task and str(task).strip()):
        return _err("route_task requires a non-empty 'task'")
    requested = "cortados"
    if model not in (None, "", "cortados"):
        # A pin: validated by the same function start_task's orchestrator uses (child_tasks.pin_model), so both tools
        # accept exactly the same names. Only this path imports the package; plain route_task stays standalone.
        try:
            ct = _child_module()
        except ImportError as exc:
            return _err(f"route_task: model pins need cortados_router.child_tasks ({exc})")
        try:
            requested = ct.pin_model(model)
        except ct.TaskError as exc:
            return _err(f"route_task: {exc}")
    prompt = str(task) if not context else f"{task}\n\nContext:\n{context}"
    try:
        mx = int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        mx = DEFAULT_MAX_TOKENS
    body = json.dumps({"model": requested, "max_tokens": mx,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request(provider_url.rstrip("/") + "/v1/messages", data=body, method="POST",
                                 # reveal: the provider answers with the backend model (and the lane's % left)
                                 # instead of `cortados`, so the trailer below can name what really ran
                                 headers={"content-type": "application/json", "x-api-key": token,
                                          "x-cortados-reveal": "1"})
    try:
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            # Scrub the FULL message before truncating: cutting first could split the token at the 300-char boundary,
            # leaving a prefix of it that the whole-token scrub no longer matches (and so would leak).
            # Only a plain string message is passed on (as for routing errors below): str() of a list or dict is its
            # repr, whose escaping could carry the token past the scrub.
            msg = json.loads(exc.read()).get("error", {}).get("message")
            detail = (": " + scrub(msg)[:300]) if isinstance(msg, str) and msg else ""
        except Exception:
            pass
        return _err(scrub(f"cortados route_task: provider returned HTTP {exc.code}{detail}"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers a truncated body (IncompleteRead) after a dropped connection; ValueError covers
        # urllib's header validation, whose message would otherwise echo the token (scrubbed above).
        return _err(scrub(f"cortados route_task: could not reach the provider ({type(exc).__name__}: {exc})"))
    if not isinstance(data, dict) or data.get("type") == "error" or data.get("error"):
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        # The provider's error text is not ours to trust: it may echo the request's credential, so scrub it. Only a plain
        # string message is passed on; a raw payload is never formatted into the result, because repr/str escaping
        # (backslashes, quotes) would carry a token in a form the scrub no longer matches.
        if isinstance(msg, str) and msg:
            return _err("cortados route_task: routing error: " + scrub(msg)[:400])   # scrub, then truncate
        return _err(f"cortados route_task: routing error: unexpected provider response ({type(data).__name__})")
    blocks = [b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(b for b in blocks if b).strip()
    model = data.get("model")
    model = model if isinstance(model, str) and model else "?"    # a non-string would be repr'd past the scrub
    # The delegate's own text is model-visible too: scrub it in case the provider or the delegate echoes the credential.
    # One trailing line names the model that really ran, so the orchestrator can see and report it (MCP clients may not
    # show _meta to the model).
    shown = scrub(model)
    shown = f"S1 → {shown}" if requested == "cortados" else shown      # routed by S1, or the pin that ran
    body_text = (scrub(text) or "(the delegate returned no text)") + f"\n\n[cortados: {shown}]"
    return {"content": [{"type": "text", "text": body_text}],
            "_meta": {"cortados/routed_model": scrub(model), "cortados/requested_model": requested}}


def handle(msg: dict, cfg: dict) -> dict | None:
    """One JSON-RPC message in, one reply message out (None for a notification). Pure, for tests. Validates shapes so a
    malformed request yields a JSON-RPC error instead of crashing the session."""
    if not isinstance(msg, dict):
        return None                                     # a non-object line: no id to answer, drop it
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if mid is None:
        return None                                     # notifications need no answer
    base = {"jsonrpc": "2.0", "id": mid}
    if not isinstance(method, str):
        return {**base, "error": {"code": -32600, "message": "invalid request: 'method' must be a string"}}
    if method == "initialize":
        req = params.get("protocolVersion")
        version = req if req in SUPPORTED_PROTOCOLS else PROTOCOL   # only echo versions we actually implement
        return {**base, "result": {"protocolVersion": version, "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "cortados-route", "version": "0"}}}
    if method == "tools/list":
        return {**base, "result": {"tools": [TOOL] + _child_tools()}}
    if method == "tools/call":
        a = params.get("arguments")
        if not isinstance(a, dict):
            a = {}
        if params.get("name") in _CHILD_NAMES:
            return {**base, "result": call_child_tool(params["name"], a, cfg)}
        if params.get("name") != "route_task":
            return {**base, "result": _err(f"unknown tool {params.get('name')!r}")}
        return {**base, "result": route_task(cfg.get("url", ""), cfg.get("token", ""), a.get("task", ""),
                                              a.get("context", ""), a.get("max_tokens", DEFAULT_MAX_TOKENS),
                                              model=a.get("model"))}
    if method == "ping":
        return {**base, "result": {}}
    return {**base, "error": {"code": -32601, "message": f"method not found: {method}"}}


_out_lock = threading.Lock()


def _write(reply: dict) -> None:
    with _out_lock:                                     # serialize stdout across concurrent workers
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def _dispatch(msg, cfg, write=_write) -> None:
    """Run one message's handler on its own thread so a slow route_task never blocks the stdin reader (pings stay
    prompt). Replies are matched by id, so out-of-order writes are fine."""
    def run():
        try:
            reply = handle(msg, cfg)
        except Exception as exc:                        # never let one bad request kill the session; the reply names only
                                                        # the exception type, as its text is unscrubbed and may echo config
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error ({type(exc).__name__})"}} if mid is not None else None
        if reply is not None:
            write(reply)
    threading.Thread(target=run, daemon=True).start()


def _resolve_token() -> str:
    """The provider token: CORTADOS_PROVIDER_TOKEN if set, else the token file (CORTADOS_PROVIDER_TOKEN_FILE, default
    ~/.config/cortados/provider-token) — the same file the routed-subagents mod reads, so route_task needs only the URL
    configured, not the secret inlined in .mcp.json."""
    tok = (os.environ.get("CORTADOS_PROVIDER_TOKEN") or "").strip()
    if tok:
        return tok
    path = os.path.expanduser(os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or "~/.config/cortados/provider-token")
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):                       # unreadable, or undecodable (UnicodeDecodeError is a ValueError):
        return ""                                       # never crash the stdio server; route_task reports 'not configured'


def _resolve_url() -> str:
    """The provider URL, or "" when it is unset or still an unexpanded ${VAR} placeholder (a shell that did not export
    the var leaves the literal, which would otherwise become an 'unknown url type' error instead of a clear 'not
    configured' one)."""
    url = (os.environ.get("CORTADOS_PROVIDER_URL") or "").strip()
    return "" if url.startswith("${") else url


def main() -> None:
    cfg = {"url": _resolve_url(), "token": _resolve_token(),
           "token_file": os.environ.get("CORTADOS_PROVIDER_TOKEN_FILE") or None}   # the child launcher reads the file
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            continue                                    # a malformed or pathologically nested line is skipped, not fatal
        for one in (msg if isinstance(msg, list) else [msg]):   # tolerate a JSON-RPC batch by dispatching each element
            _dispatch(one, cfg)


if __name__ == "__main__":
    main()
