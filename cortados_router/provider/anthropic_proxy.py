"""The anthropic-api lane executor: proxy a routed turn to api.anthropic.com with the credit key, meter the spend.

Unlike the CLI bridges (bridge.py / codex_bridge.py), this lane is STATELESS. The Anthropic wire is stateless — the
client sends the full /v1/messages history on every turn — and the client here is a genuine Anthropic client that
expects genuine Anthropic responses, so the provider forwards the request to the real API and relays the real
response straight back. Only two things change: the virtual "cortados" model id becomes the routed Claude model id,
and the response's token usage is costed into the api_credits meter (the lane's $200/mo window).

The pure helpers (build_upstream / extract_usage / usage_from_sse) are unit-tested; serve() does the socket I/O.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from cortados_router import api_credits

API_BASE = os.environ.get("CORTADOS_ANTHROPIC_API_BASE", "https://api.anthropic.com")
API_VERSION = os.environ.get("CORTADOS_ANTHROPIC_API_VERSION", "2023-06-01")
UPSTREAM_TIMEOUT_S = float(os.environ.get("CORTADOS_ANTHROPIC_API_TIMEOUT_S", "1800"))


def api_key() -> str:
    return (os.environ.get("CORTADOS_ANTHROPIC_API_KEY") or "").strip()


def configured() -> bool:
    """The lane is usable only with a credit key set."""
    return bool(api_key())


def build_upstream(body: dict[str, Any], model: str, key: str, *, base: str = API_BASE, version: str = API_VERSION) -> tuple[str, bytes, dict[str, str]]:
    """(url, data, headers) for the upstream /v1/messages call: the client body with the routed model id, the credit
    key, and an anthropic-beta header carried through when the client set one."""
    out = dict(body)
    out["model"] = model
    data = json.dumps(out).encode()
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": version}
    beta = body.get("anthropic_beta") or body.get("betas")
    if isinstance(beta, list) and beta:
        headers["anthropic-beta"] = ",".join(str(b) for b in beta)
    elif isinstance(beta, str) and beta:
        headers["anthropic-beta"] = beta
    return base.rstrip("/") + "/v1/messages", data, headers


def extract_usage(message: Any) -> dict[str, Any] | None:
    """The `usage` block of a non-streaming /v1/messages response."""
    return message.get("usage") if isinstance(message, dict) else None


def usage_from_sse(text: str) -> dict[str, Any]:
    """Accumulate usage from an SSE stream: `message_start` carries input (and cache) tokens, `message_delta` the
    output tokens. Later values win, so the final delta's output_tokens is what is metered."""
    usage: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except ValueError:
            continue
        if ev.get("type") == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            if isinstance(u, dict):
                usage.update(u)
        elif ev.get("type") == "message_delta":
            u = ev.get("usage") or {}
            if isinstance(u, dict):
                usage.update({k: v for k, v in u.items() if v is not None})
    return usage


def _meter(model: str, usage: dict[str, Any] | None, now: float) -> None:
    try:
        if usage:
            api_credits.record(model, usage, now)
    except Exception:
        # Metering must never fail the client's turn; a lost sample only risks slight over-use of the credit, and the
        # cap check before the next call still holds.
        pass


def serve(handler: Any, body: dict[str, Any], model: str, now: float, *, key: str | None = None,
          urlopen: Callable[..., Any] = urllib.request.urlopen) -> None:
    """Proxy one turn to the real API and relay the response to the client, metering the spend. `handler` is the
    BaseHTTPRequestHandler serving the client request."""
    key = (key if key is not None else api_key()).strip()
    url, data, headers = build_upstream(body, model, key)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    streaming = bool(body.get("stream"))
    try:
        resp = urlopen(req, timeout=UPSTREAM_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        handler.send_response(exc.code)
        handler.send_header("content-type", exc.headers.get("content-type", "application/json") if exc.headers else "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return
    except (urllib.error.URLError, OSError) as exc:
        msg = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"cortados: upstream API unreachable: {exc}"}}).encode()
        handler.send_response(502)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(msg)))
        handler.end_headers()
        handler.wfile.write(msg)
        return

    with resp:
        if not streaming:
            raw = resp.read()
            try:
                _meter(model, extract_usage(json.loads(raw)), now)
            except ValueError:
                pass
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        # Streaming: relay the real API's SSE bytes straight through, sniffing usage as it passes.
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.close_connection = True
        seen: list[str] = []
        try:
            for chunk in resp:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                seen.append(chunk.decode("utf-8", "replace"))
        finally:
            _meter(model, usage_from_sse("".join(seen)), now)
