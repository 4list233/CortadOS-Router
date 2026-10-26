"""Stdio MCP server that gives a Claude Code (or any MCP client) session ONE tool, `route_task`, for delegating a
self-contained subtask to the CortadOS router. The main session keeps its own model; when it wants to offload work it
calls this tool, which posts the subtask to the `cortados` provider endpoint (S1 scores it, the router picks the
cheapest capable lane in the resource pool, and the chosen model answers), and returns the answer as text. It does not
replace the session's model or touch its login.

Stdlib only; runs standalone (the MCP client launches it as a subprocess over stdio), so it imports nothing from the
package. It forwards to the provider — it does NOT need to sit next to S1 or the provider; it only needs to reach the
provider URL.

Register with Claude Code, e.g.:
  claude mcp add cortados-route \\
    --env CORTADOS_PROVIDER_URL=http://<provider-ip>:8432 \\
    --env CORTADOS_PROVIDER_TOKEN=<token> \\
    -- python3 /path/to/cortados_router/subagent_mcp.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"
TIMEOUT_S = float(os.environ.get("CORTADOS_ROUTE_TIMEOUT_S", "300"))   # a routed subtask can take a while (cold lane, large output)
DEFAULT_MAX_TOKENS = 8000

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
        },
        "required": ["task"],
    },
}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def route_task(provider_url: str, token: str, task: str, context: str = "", max_tokens: int = DEFAULT_MAX_TOKENS,
               urlopen=urllib.request.urlopen) -> dict:
    """Post the subtask to the provider's /v1/messages and return the routed model's text as an MCP tool result."""
    if not (provider_url and token):
        return _err("cortados route_task is not configured: set CORTADOS_PROVIDER_URL and CORTADOS_PROVIDER_TOKEN")
    if not (task and str(task).strip()):
        return _err("route_task requires a non-empty 'task'")
    prompt = str(task) if not context else f"{task}\n\nContext:\n{context}"
    try:
        mx = int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS
    except (TypeError, ValueError):
        mx = DEFAULT_MAX_TOKENS
    body = json.dumps({"model": "cortados", "max_tokens": mx,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request(provider_url.rstrip("/") + "/v1/messages", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-api-key": token})
    try:
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = ": " + (json.loads(exc.read()).get("error", {}).get("message") or "")[:300]
        except Exception:
            pass
        return _err(f"cortados route_task: provider returned HTTP {exc.code}{detail}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return _err(f"cortados route_task: could not reach the provider ({type(exc).__name__}: {exc})")
    if not isinstance(data, dict) or data.get("type") == "error" or data.get("error"):
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        return _err(f"cortados route_task: routing error: {msg or data}")
    blocks = [b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(b for b in blocks if b).strip()
    model = data.get("model") or "?"
    return {"content": [{"type": "text", "text": text or "(the delegate returned no text)"}],
            "_meta": {"cortados/routed_model": model}}


def handle(msg: dict, cfg: dict) -> dict | None:
    """One JSON-RPC message in, one reply message out (None for a notification). Pure, for tests."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None                                     # notifications need no answer
    base = {"jsonrpc": "2.0", "id": mid}
    if method == "initialize":
        return {**base, "result": {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                                    "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "cortados-route", "version": "0"}}}
    if method == "tools/list":
        return {**base, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        if params.get("name") != "route_task":
            return {**base, "result": _err(f"unknown tool {params.get('name')!r}")}
        a = params.get("arguments") or {}
        return {**base, "result": route_task(cfg.get("url", ""), cfg.get("token", ""), a.get("task", ""),
                                              a.get("context", ""), a.get("max_tokens", DEFAULT_MAX_TOKENS))}
    if method == "ping":
        return {**base, "result": {}}
    return {**base, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    cfg = {"url": os.environ.get("CORTADOS_PROVIDER_URL", ""), "token": os.environ.get("CORTADOS_PROVIDER_TOKEN", "")}
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line), cfg)
        except ValueError:
            continue                                    # a malformed line is skipped, not fatal
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
