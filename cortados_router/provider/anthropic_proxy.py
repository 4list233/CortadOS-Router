"""The anthropic-api lane executor: proxy a pinned turn to api.anthropic.com with the credit key and REPORT the
usage back (the server reserves/settles the credit — this module does no accounting itself).

Hardened after a Codex money-path review:
  * The upstream is pinned to the exact HTTPS Anthropic origin, and redirects are refused — so the credit key
    (x-api-key) is never sent to another host or re-sent across a 3xx redirect.
  * Streaming usage is parsed from each chunk BEFORE it is written to the client, so a disconnect mid-stream does
    not lose the usage already received. serve() returns the usage it captured (possibly partial); the caller
    settles the reservation with it, falling back to the full reservation when it is incomplete.

Stateless; the client sends full history each turn. Stdlib only.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API_BASE = os.environ.get("CORTADOS_ANTHROPIC_API_BASE", "https://api.anthropic.com")
API_VERSION = os.environ.get("CORTADOS_ANTHROPIC_API_VERSION", "2023-06-01")
UPSTREAM_TIMEOUT_S = float(os.environ.get("CORTADOS_ANTHROPIC_API_TIMEOUT_S", "1800"))
ALLOWED_HOST = "api.anthropic.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - never follow a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def api_key() -> str:
    return (os.environ.get("CORTADOS_ANTHROPIC_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def _require_anthropic_origin(url: str) -> None:
    """The credit key may only ever go to the exact HTTPS Anthropic origin."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() != ALLOWED_HOST:
        raise ValueError(f"anthropic-api lane refuses a non-Anthropic/insecure upstream: {u.scheme}://{u.hostname}")


def build_upstream(body: dict[str, Any], model: str, key: str, *, base: str = API_BASE, version: str = API_VERSION) -> tuple[str, bytes, dict[str, str]]:
    url = base.rstrip("/") + "/v1/messages"
    _require_anthropic_origin(url)
    out = dict(body)
    out["model"] = model
    data = json.dumps(out).encode()
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": version}
    beta = body.get("anthropic_beta") or body.get("betas")
    if isinstance(beta, list) and beta:
        headers["anthropic-beta"] = ",".join(str(b) for b in beta)
    elif isinstance(beta, str) and beta:
        headers["anthropic-beta"] = beta
    return url, data, headers


def extract_usage(message: Any) -> dict[str, Any]:
    u = message.get("usage") if isinstance(message, dict) else None
    return u if isinstance(u, dict) else {}


def _update_usage_from_sse(usage: dict[str, Any], text: str) -> bool:
    """Fold any usage carried by the SSE lines in `text` into `usage` (message_start = input/cache, delta = output).
    Returns True iff a PARSED terminal `message_stop` event was seen (a substring in generated text does not count)."""
    saw_stop = False
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
        t = ev.get("type")
        if t == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            if isinstance(u, dict):
                usage.update(u)
        elif t == "message_delta":
            u = ev.get("usage") or {}
            if isinstance(u, dict):
                usage.update({k: v for k, v in u.items() if v is not None})
        elif t == "message_stop":
            saw_stop = True
    return saw_stop


def usage_from_sse(text: str) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    _update_usage_from_sse(usage, text)
    return usage


def serve(handler: Any, body: dict[str, Any], model: str, now: float, *, key: str | None = None,
          urlopen: Callable[..., Any] | None = None) -> tuple[dict[str, Any], str]:
    """Proxy one turn to the real API and relay the response to the client. Returns (usage, outcome):
      * ("completed"): a terminal response was received — settle the actual cost from `usage`.
      * ("rejected"):  the API returned an HTTP error — it was not billed, settle zero.
      * ("unknown"):   the turn did not terminate for us (timeout, transport error, or a mid-stream disconnect) —
                       the spend is unknown, so the caller settles the FULL reservation (conservative).
    Usage is parsed before each client write, so a disconnect cannot lose it."""
    key = (key if key is not None else api_key()).strip()
    open_fn = urlopen or _OPENER.open
    url, data, headers = build_upstream(body, model, key)   # raises on a non-Anthropic origin
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    streaming = bool(body.get("stream"))
    try:
        resp = open_fn(req, timeout=UPSTREAM_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        handler.send_response(exc.code)
        handler.send_header("content-type", (exc.headers.get("content-type") if exc.headers else None) or "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return {}, "rejected"
    except (urllib.error.URLError, OSError) as exc:
        msg = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"cortados: upstream API unreachable: {exc}"}}).encode()
        handler.send_response(502)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(msg)))
        handler.end_headers()
        handler.wfile.write(msg)
        return {}, "unknown"   # dispatch may have reached the API; settle conservatively

    with resp:
        if not streaming:
            raw = resp.read()
            try:
                usage: dict[str, Any] = extract_usage(json.loads(raw))
            except ValueError:
                usage = {}
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return usage, "completed"
        # Streaming: parse usage (and watch for the terminal message_stop) from each chunk BEFORE writing it.
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.close_connection = True
        usage = {}
        completed = False
        disconnected = False
        for chunk in resp:
            text = chunk.decode("utf-8", "replace")
            if _update_usage_from_sse(usage, text):   # True only on a PARSED message_stop event
                completed = True
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except OSError:
                disconnected = True
                break  # client gone
        return usage, ("completed" if completed and not disconnected else "unknown")
