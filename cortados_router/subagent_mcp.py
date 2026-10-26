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
    provider_url, token = (provider_url or "").strip(), (token or "").strip()   # strip stray whitespace/newlines that break header validation
    # Never let the token reach model-visible text. Only a token long enough to be a real secret is scrubbed; a short
    # token is not a credential and scrubbing it would mangle ordinary words (a real provider token is a long random string).
    scrub = (lambda s: s.replace(token, "***")) if (token and len(token) >= 8) else (lambda s: s)
    if not (provider_url and token):
        return _err("cortados route_task is not configured: set CORTADOS_PROVIDER_URL and CORTADOS_PROVIDER_TOKEN")
    if any(ord(c) < 32 or ord(c) == 127 for c in token):
        # Reject a token with control chars (e.g. an embedded newline from a wrapped paste) BEFORE building the request:
        # urllib's header validator would otherwise raise with the token's escaped bytes, which the raw-string scrub misses.
        return _err("cortados route_task: CORTADOS_PROVIDER_TOKEN contains control characters (e.g. an embedded newline); fix it")
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
        return _err(scrub(f"cortados route_task: provider returned HTTP {exc.code}{detail}"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # http.client.HTTPException covers a truncated body (IncompleteRead) after a dropped connection; ValueError covers
        # urllib's header validation, whose message would otherwise echo the token (scrubbed above).
        return _err(scrub(f"cortados route_task: could not reach the provider ({type(exc).__name__}: {exc})"))
    if not isinstance(data, dict) or data.get("type") == "error" or data.get("error"):
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        return _err(f"cortados route_task: routing error: {msg or data}")
    blocks = [b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(b for b in blocks if b).strip()
    model = data.get("model") or "?"
    return {"content": [{"type": "text", "text": text or "(the delegate returned no text)"}],
            "_meta": {"cortados/routed_model": model}}


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
        return {**base, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        if params.get("name") != "route_task":
            return {**base, "result": _err(f"unknown tool {params.get('name')!r}")}
        a = params.get("arguments")
        if not isinstance(a, dict):
            a = {}
        return {**base, "result": route_task(cfg.get("url", ""), cfg.get("token", ""), a.get("task", ""),
                                              a.get("context", ""), a.get("max_tokens", DEFAULT_MAX_TOKENS))}
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
        except Exception as exc:                        # never let one bad request kill the session
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"internal error: {exc}"}} if mid is not None else None
        if reply is not None:
            write(reply)
    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    cfg = {"url": os.environ.get("CORTADOS_PROVIDER_URL", ""), "token": os.environ.get("CORTADOS_PROVIDER_TOKEN", "")}
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
