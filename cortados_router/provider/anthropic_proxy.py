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
import math
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


def _update_usage_from_sse(usage: dict[str, Any], text: str) -> tuple[bool, bool]:
    """Fold any usage carried by the SSE lines in `text` into `usage` (message_start = input/cache + an INITIAL
    output estimate, message_delta = the FINAL output count). Returns (saw_message_stop, saw_valid_final_output):
      * saw_message_stop        -- a PARSED terminal message_stop event (a substring in generated text does not count)
      * saw_valid_final_output  -- a message_delta carried a valid (finite, non-negative) output_tokens, i.e. the
                                   real final count arrived. Without it, output_tokens is only the start estimate.
    A message_delta is applied AS-IS (not None-filtered), so a null/invalid final OVERWRITES the initial estimate
    and has_usable_usage() can then reject it, rather than the stale estimate being settled."""
    saw_stop = False
    saw_final_output = False
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
            u = ev.get("usage")
            if isinstance(u, dict):
                usage.update(u)   # the final counts, applied as-is so a null final overwrites the initial estimate
                if _valid_count(u.get("output_tokens")):
                    saw_final_output = True
        elif t == "message_stop":
            saw_stop = True
    return saw_stop, saw_final_output


def usage_from_sse(text: str) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    _update_usage_from_sse(usage, text)
    return usage


_REQUIRED_USAGE_FIELDS = ("input_tokens", "output_tokens")
_OPTIONAL_USAGE_FIELDS = ("cache_read_input_tokens", "cache_creation_input_tokens")


def _valid_count(v: Any) -> bool:
    """A usable token count: a finite, non-negative, non-bool number (a legitimate zero is fine)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def has_usable_usage(usage: Any) -> bool:
    """True iff `usage` can be settled accurately: BOTH input_tokens and output_tokens are present as finite
    non-negative numbers, and any present cache field is likewise valid. A real completed response always carries
    both counts, so this never downgrades a genuine completion; a null / negative / missing count (its true cost is
    unknown, not zero) keeps the turn 'unknown' and the conservative charge instead of settling a completed $0."""
    if not isinstance(usage, dict):
        return False
    if not all(_valid_count(usage.get(f)) for f in _REQUIRED_USAGE_FIELDS):
        return False
    return all(_valid_count(usage[f]) for f in _OPTIONAL_USAGE_FIELDS if f in usage)


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


def _with_model_suffix(message: Any, suffix: str) -> bool:
    """Append `suffix` to a message's `model` in place; False (left untouched) when it names none."""
    if isinstance(message, dict) and isinstance(message.get("model"), str):
        message["model"] += suffix
        return True
    return False


def _reveal_chunk(chunk: bytes, suffix: str) -> tuple[bytes, bool]:
    """The chunk with its message_start event's model suffixed, and whether it was. Any other chunk, or one that does
    not parse, is returned byte-for-byte: the reveal is cosmetic and never alters the relayed content."""
    if b"message_start" not in chunk:
        return chunk, False
    try:
        lines = chunk.split(b"\n")
        for i, line in enumerate(lines):
            if line.startswith(b"data: ") and b"message_start" in line:
                event = json.loads(line[6:])
                if event.get("type") == "message_start" and _with_model_suffix(event.get("message"), suffix):
                    lines[i] = b"data: " + json.dumps(event).encode()
                    return b"\n".join(lines), True
    except Exception:
        pass
    return chunk, False


def serve(handler: Any, body: dict[str, Any], model: str, now: float, *, key: str | None = None,
          urlopen: Callable[..., Any] | None = None, model_suffix: str | None = None) -> tuple[dict[str, Any], str]:
    """Proxy one turn to the real API and relay the response to the client. Returns (usage, outcome):
      * ("completed"): a terminal response was received — settle the actual cost from `usage`.
      * ("rejected"):  the API returned an HTTP error — it was not billed, settle zero.
      * ("unknown"):   the turn did not terminate for us (timeout, transport error, or a mid-stream disconnect) —
                       the spend is unknown, so the caller settles the FULL reservation (conservative).
    Usage is parsed before each client write, so a disconnect cannot lose it. `model_suffix` (a revealing client
    only) is appended to the success response's `model`; usage and billing always read the upstream bytes as sent."""
    key = (key if key is not None else api_key()).strip()
    open_fn = urlopen or _OPENER.open
    try:
        url, data, headers = build_upstream(body, model, key)
    except ValueError as exc:
        # A pre-dispatch construction/validation error (e.g. a misconfigured non-HTTPS/non-Anthropic upstream): the
        # request is NOT built and nothing is sent, so nothing is billed -> a certain rejection (settle zero). This
        # catch is scoped to build_upstream ONLY, so it never masks a post-dispatch opener/response error as rejected.
        msg = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                          "message": f"cortados: request not dispatched ({exc})"}}).encode()
        try:
            handler.send_response(400)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(msg)))
            handler.end_headers()
            handler.wfile.write(msg)
        except OSError:
            pass
        return {}, "rejected"
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
            if model_suffix and isinstance(parsed, dict) and parsed.get("type") == "message":
                try:
                    if _with_model_suffix(parsed, model_suffix):
                        raw = json.dumps(parsed).encode()
                except Exception:
                    pass
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
        final_output = False
        revealed = not model_suffix
        try:
            for chunk in resp:
                text = chunk.decode("utf-8", "replace")
                saw_stop, saw_final = _update_usage_from_sse(usage, text)
                completed = completed or saw_stop        # a PARSED message_stop event
                final_output = final_output or saw_final  # a message_delta supplied the real final output count
                if not revealed:
                    chunk, revealed = _reveal_chunk(chunk, model_suffix)
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except OSError:
                    break  # client gone: stop relaying; the billing outcome is decided by what we already parsed
        except (OSError, http.client.HTTPException):
            pass   # an upstream read failed mid-stream; keep whatever usage/terminal state we already captured
        # "completed" only when the terminal event was parsed, the real final output count arrived (not just the
        # message_start estimate), AND the merged usage is usable. Otherwise the cost is not reliably known, so the
        # turn stays "unknown" and keeps the conservative reservation. A parsed terminal event means the full turn
        # was received and billed regardless of a later client/upstream failure.
        return usage, ("completed" if completed and final_output and has_usable_usage(usage) else "unknown")
    finally:
        try:
            resp.close()
        except Exception:
            pass
