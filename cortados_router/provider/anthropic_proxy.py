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

import http.client
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
        if not isinstance(ev, dict):
            continue   # a well-formed JSON non-object (e.g. `data: []`) carries no usage and must not raise here
        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message")
            u = msg.get("usage") if isinstance(msg, dict) else None
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


_USAGE_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def has_usable_usage(usage: Any) -> bool:
    """True iff `usage` carries at least one token count as a real (finite, non-bool) number. A usage block whose
    counts are all null/missing/non-numeric is NOT usable: its cost cannot be settled, so the turn must stay
    'unknown' rather than settle as a completed $0. Legitimate zero counts are accepted."""
    if not isinstance(usage, dict):
        return False
    for f in _USAGE_TOKEN_FIELDS:
        v = usage.get(f)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                if v == v and v not in (float("inf"), float("-inf")):   # finite, not NaN
                    return True
            except TypeError:
                continue
    return False


def invalid_header(headers: dict[str, str]) -> str | None:
    """The NAME of the first outbound header whose value the HTTP layer would reject (contains CR/LF/NUL, or is not
    latin-1 encodable), else None. Checked BEFORE dispatch so a malformed header (a bad beta, or a mis-configured
    credit key) is a certain pre-dispatch rejection. Returns the name only — never the value — so the credit key is
    never echoed back to the client."""
    for k, v in headers.items():
        s = v if isinstance(v, str) else str(v)
        if any(c in s for c in ("\r", "\n", "\x00")):
            return k
        try:
            s.encode("latin-1")
        except UnicodeEncodeError:
            return k
    return None


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
    bad_header = invalid_header(headers)
    if bad_header is not None:
        # A malformed outbound header (a client-supplied beta, or a mis-configured credit key) means nothing is sent,
        # so nothing is billed -> a certain pre-dispatch rejection. Relay a 400 naming the header (never its value).
        msg = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                          "message": f"cortados: outbound header {bad_header!r} is malformed; request not dispatched"}}).encode()
        try:
            handler.send_response(400)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(msg)))
            handler.end_headers()
            handler.wfile.write(msg)
        except OSError:
            pass
        return {}, "rejected"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    streaming = bool(body.get("stream"))
    try:
        resp = open_fn(req, timeout=UPSTREAM_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        # The API REJECTED the turn: nothing is billed, regardless of whether we can read or relay the error body.
        try:
            payload = exc.read()
        except (OSError, http.client.HTTPException):
            payload = b'{"type":"error","error":{"type":"api_error","message":"cortados: upstream rejected the request"}}'
        try:
            handler.send_response(exc.code)
            handler.send_header("content-type", (exc.headers.get("content-type") if exc.headers else None) or "application/json")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
        except OSError:
            pass
        return {}, "rejected"
    except (urllib.error.URLError, OSError) as exc:
        msg = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"cortados: upstream API unreachable: {exc}"}}).encode()
        try:
            handler.send_response(502)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(msg)))
            handler.end_headers()
            handler.wfile.write(msg)
        except OSError:
            pass
        return {}, "unknown"   # dispatch may have reached the API; settle conservatively
    except Exception as exc:
        # Headers were validated above, so this is NOT a pre-dispatch rejection: the POST may well have been sent
        # (e.g. the stdlib raised parsing the response headers). The spend is uncertain -> "unknown", never a $0
        # rejection. Best-effort 502 to the client.
        msg = json.dumps({"type": "error", "error": {"type": "api_error", "message": "cortados: upstream response could not be read"}}).encode()
        try:
            handler.send_response(502)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(msg)))
            handler.end_headers()
            handler.wfile.write(msg)
        except OSError:
            pass
        return {}, "unknown"

    try:
        if not streaming:
            try:
                raw = resp.read()
            except (OSError, http.client.HTTPException):
                return {}, "unknown"   # the turn may have been billed, but we have no usable response -> conservative
            try:
                parsed: Any = json.loads(raw)
            except ValueError:
                parsed = None
            # "completed" ONLY for a terminal message carrying usable usage. A truncated or malformed body (its cost
            # is real but unreadable) stays "unknown", so it keeps the conservative reservation instead of settling 0.
            if isinstance(parsed, dict) and parsed.get("type") == "message" and has_usable_usage(parsed.get("usage")):
                usage, outcome = parsed["usage"], "completed"
            else:
                usage, outcome = {}, "unknown"   # malformed body, or usage with no usable numeric counts -> conservative
            try:
                handler.send_response(200)
                handler.send_header("content-type", "application/json")
                handler.send_header("content-length", str(len(raw)))
                handler.end_headers()
                handler.wfile.write(raw)
            except OSError:
                pass   # a full response was received; a client write failure does not change the billing outcome
            return usage, outcome
        # Streaming: parse usage (and watch for the terminal message_stop) from each chunk BEFORE writing it.
        try:
            handler.send_response(200)
            handler.send_header("content-type", "text/event-stream")
            handler.send_header("cache-control", "no-cache")
            handler.send_header("connection", "close")
            handler.end_headers()
        except OSError:
            return {}, "unknown"   # client gone before the stream even started; the turn may still have been billed
        handler.close_connection = True
        usage = {}
        completed = False
        try:
            for chunk in resp:
                text = chunk.decode("utf-8", "replace")
                if _update_usage_from_sse(usage, text):   # True only on a PARSED message_stop event
                    completed = True
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except OSError:
                    break  # client gone: stop relaying; the billing outcome is decided by what we already parsed
        except (OSError, http.client.HTTPException):
            pass   # an upstream read failed mid-stream; keep whatever usage/terminal state we already captured
        # "completed" only when the terminal event was parsed AND the captured usage is usable (a null/absent-usage
        # stream cannot be settled, so it keeps the conservative "unknown" charge). A parsed terminal event means the
        # full turn was received and billed regardless of a later client/upstream failure.
        return usage, ("completed" if completed and has_usable_usage(usage) else "unknown")
    finally:
        try:
            resp.close()
        except Exception:
            pass
