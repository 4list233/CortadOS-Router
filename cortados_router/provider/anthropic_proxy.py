"""The anthropic-api lane executor: proxy a pinned turn to api.anthropic.com with the credit key and REPORT the
usage back (the server reserves/settles the credit — this module does no accounting itself).

Hardened after a Codex money-path review:
  * The upstream is pinned to the exact HTTPS Anthropic origin, and redirects are never followed — so the credit key
    (x-api-key) is never sent to another host or re-sent across a 3xx redirect.
  * Streaming usage is parsed from each chunk BEFORE it is written to the client, so a disconnect mid-stream does
    not lose the usage already received. serve() returns the usage it captured (possibly partial); the caller
    settles the reservation with it, falling back to the full reservation when it is incomplete.

  * The request is written only through a per-turn _Gate (#114 review round 2): the lane's own HTTPS transport
    (_dispatch) bounds DNS, connect and the TLS handshake by the time left, checks the gate under its lock right before
    every write, and the deadline shuts the gate and aborts the socket, so a late worker can never send the request.
    A turn whose gate shut before any byte was written is a certain non-dispatch (settled at zero). Error bodies are
    read for at most ERROR_BODY_MAX bytes and never past the deadline.

Stateless; the client sends full history each turn. Stdlib only.
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from cortados_router.provider.anthropic_wire import error_json
from cortados_router.safe_text import safe_error_body

API_BASE = os.environ.get("CORTADOS_ANTHROPIC_API_BASE", "https://api.anthropic.com")
API_VERSION = os.environ.get("CORTADOS_ANTHROPIC_API_VERSION", "2023-06-01")
UPSTREAM_TIMEOUT_S = float(os.environ.get("CORTADOS_ANTHROPIC_API_TIMEOUT_S", "1800"))
ALLOWED_HOST = "api.anthropic.com"
ABORTED = "aborted"     # serve outcome: the turn hit its hard wall-clock cap and was cut off (settled in full)
logger = logging.getLogger("cortados.provider.anthropic_proxy")


def turn_cap_s() -> float:
    """The hard WALL-CLOCK cap of one upstream turn (#114 review round 1): the configured timeout, never above
    api_credits.MAX_TURN_S (which is below the grants' expiry margin), never non-positive. A socket timeout bounds each
    socket operation only; serve() enforces this one on the whole turn."""
    from cortados_router import api_credits
    t = UPSTREAM_TIMEOUT_S
    if not math.isfinite(t) or t <= 0:
        t = api_credits.MAX_TURN_S
    return min(t, api_credits.MAX_TURN_S)


def _socket_of(resp: Any) -> Any:
    """The socket under a urllib/http.client response, or None (a fake response in tests has none)."""
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


class _Cancelled(OSError):
    """The turn's gate is shut (its deadline passed, or it was cancelled): the request was not written from here on."""


def _abort_socket(sock: Any) -> None:
    """Abort a connection now: linger 0 (its close resets the connection and discards any unsent bytes instead of
    flushing them) and a shutdown of the raw socket, which wakes a thread blocked in connect/recv/send on it. The base
    socket method is used for a TLS socket too, so its TLS object is left alone (a later write fails on the dead socket
    instead of falling back to plaintext)."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    except (OSError, ValueError):
        pass
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass


class _Gate:
    """Whether the turn's request may still be written (#114 review round 2, finding 1). One per turn, shared by the
    thread that dispatches and every canceller (the caller giving up at the deadline, the cutoff timer):
      * may_write() is called right before EVERY write of request bytes; under the lock it checks that the gate is open
        and the deadline has not passed, and records that a request byte may leave (`sent`);
      * cancel() shuts the gate under the same lock, then aborts the attached socket, so a write that passed the check
        just before fails on the dead socket, and none can pass it afterwards;
      * attach() registers each socket as it is made, refused once the gate is shut.
    So once the deadline passes or the turn is cancelled, no worker — however late its DNS, connect or TLS handshake
    returns — can send the request. `sent` False after the turn means nothing was written: it cannot have been billed."""

    def __init__(self, end: float, clock: Callable[[], float]):
        self._lock = threading.Lock()
        self._end, self._clock = end, clock
        self._open = True
        self._sock: Any = None
        self.sent = False

    def left(self) -> float:
        return self._end - self._clock()

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def attach(self, sock: Any) -> bool:
        with self._lock:
            if not self._open:
                return False
            self._sock = sock
            return True

    def may_write(self) -> bool:
        with self._lock:
            if self._open and self._clock() < self._end:
                self.sent = True
                return True
            self._open = False
            return False

    def cancel(self) -> None:
        with self._lock:
            self._open = False
            sock = self._sock
        if sock is not None:
            _abort_socket(sock)


class _Cutoff:
    """Aborts a response at the turn's deadline even while a read is blocked: a timer that shuts the gate (aborting its
    socket, which wakes a blocked recv) and, for a response with no gated socket, shuts its own socket down or closes
    it. `fired` says it did."""

    def __init__(self, resp: Any, delay_s: float, gate: "_Gate | None" = None):
        self.resp, self.gate, self.fired = resp, gate, False
        self._timer = threading.Timer(max(0.0, delay_s), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        self.fired = True
        if self.gate is not None:
            self.gate.cancel()
        sock = _socket_of(self.resp)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)   # wakes a blocked read with EOF; the reader's thread closes resp
            except OSError:
                pass
            return
        try:
            self.resp.close()                     # no socket to shut down (not a urllib response): close it instead
        except Exception:
            pass

    def cancel(self) -> None:
        self._timer.cancel()


def _resolve(host: str, port: int) -> list:
    """getaddrinfo for the upstream (a test seam). It has no timeout of its own: it runs on the dispatch thread, the
    caller stops waiting at the deadline, and the gate refuses everything after it returns late."""
    return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)


_TLS_CONTEXT: Any = None


def _tls_wrap(sock: socket.socket, host: str) -> Any:
    """The TLS socket over a connected `sock`, NOT yet handshaken (the caller attaches it to the gate first, so the
    handshake can be cancelled). Certificate and hostname verified against the system trust store. A test seam."""
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        _TLS_CONTEXT = ssl.create_default_context()
    return _TLS_CONTEXT.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)


def _proxy_for(host: str) -> str | None:
    """The HTTPS proxy the environment / system configuration would use for `host`, or None. The credit lane only
    connects directly (its transport must own the socket to cancel the dispatch), so it refuses to run behind one
    rather than send the credit key around a configured proxy."""
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("all")
    if proxy and not urllib.request.proxy_bypass(host):
        return proxy
    return None


class _GatedConnection(http.client.HTTPConnection):
    """An HTTPS connection whose every step is bounded by the turn's deadline and whose request is written only
    through the gate: DNS (bounded by the caller's wait), connect and TLS handshake each with a socket timeout of at
    most the time left and on a socket the gate can abort, and every send checked by `gate.may_write()`. It never
    follows a redirect (http.client does not) and never uses a proxy."""

    default_port = 443

    def __init__(self, host: str, port: int, gate: _Gate, per_op: float):
        super().__init__(host, port, timeout=per_op)
        self._gate = gate

    def _bounded(self) -> float:
        left = self._gate.left()
        if left <= 0 or not self._gate.is_open():
            raise _Cancelled("the API credit lane's turn deadline passed before the request was sent")
        return min(float(self.timeout), left)

    def connect(self) -> None:
        last: BaseException | None = None
        infos = _resolve(self.host, self.port)
        sock: Any = None
        for family, kind, proto, _name, addr in infos:
            timeout = self._bounded()
            s = socket.socket(family, kind, proto)
            if not self._gate.attach(s):
                s.close()
                raise _Cancelled("the API credit lane's turn was cancelled before it connected")
            s.settimeout(timeout)
            try:
                s.connect(addr)
            except OSError as exc:
                s.close()
                last = exc
                continue
            sock = s
            break
        if sock is None:
            raise last if last is not None else OSError(f"no address for {self.host}")
        try:
            sock.settimeout(self._bounded())
            tls = _tls_wrap(sock, ALLOWED_HOST)
            if not self._gate.attach(tls):
                raise _Cancelled("the API credit lane's turn was cancelled during its TLS setup")
            if tls is not sock:
                sock = tls
            handshake = getattr(sock, "do_handshake", None)
            if handshake is not None:
                sock.settimeout(self._bounded())
                handshake()
            sock.settimeout(self._bounded())   # each later operation; the cutoff timer enforces the whole turn
        except BaseException:
            sock.close()
            raise
        self.sock = sock

    def send(self, data: Any) -> None:
        if self.sock is None:
            raise _Cancelled("the API credit lane's connection is not open; nothing sent")
        if not self._gate.may_write():          # under the gate's lock: never a request byte after the deadline
            raise _Cancelled("the API credit lane's turn deadline passed before the request was sent")
        super().send(data)


def _dispatch(url: str, data: bytes, headers: dict[str, str], gate: _Gate, per_op: float) -> Any:
    """POST the request to the pinned origin through the gate and return the http.client response (any status). Runs
    on the dispatch thread. The socket is handed to the response and closes with it."""
    u = urllib.parse.urlparse(url)
    conn = _GatedConnection(u.hostname or ALLOWED_HOST, u.port or 443, gate, per_op)
    try:
        conn.connect()
        conn.request("POST", u.path or "/", body=data, headers={**headers, "connection": "close"})
        resp = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    # Hand the socket to the response (as http.client does for a closing response): conn.close() would also close a
    # kept-alive response, so drop conn's reference and close it here; it is really closed when the response is.
    sock, conn.sock = conn.sock, None
    if sock is not None:
        sock.close()
    return resp


def _open_within(open_fn: Callable[[], Any], gate: _Gate, remaining: float) -> tuple[Any, bool]:
    """open_fn() on a helper thread, waited for at most `remaining` seconds of wall clock: (response, False), or
    (None, True) when the deadline passed first. Then the gate is cancelled before returning, so the abandoned call
    can no longer write the request whatever it is blocked in (DNS, connect, TLS, send); a response it still returns
    later is closed on its thread. An exception from open_fn is raised here as it was."""
    box: dict[str, Any] = {}
    done = threading.Event()
    abandoned = threading.Event()

    def run() -> None:
        try:
            box["resp"] = open_fn()
        except BaseException as exc:      # noqa: BLE001 - re-raised on the caller's thread
            box["exc"] = exc
        done.set()
        if abandoned.is_set() and "resp" in box:
            try:
                box["resp"].close()
            except Exception:
                pass

    t = threading.Thread(target=run, name="cortados-api-open", daemon=True)
    t.start()
    if not done.wait(max(0.0, remaining)):
        abandoned.set()
        gate.cancel()                         # from here on nothing more of the request can be written
        if done.is_set() and "resp" in box:   # it finished in between: close it, the turn is over all the same
            try:
                box["resp"].close()
            except Exception:
                pass
        return None, True
    if "exc" in box:
        raise box["exc"]
    return box["resp"], False


ERROR_BODY_MAX = 64 * 1024     # an upstream error body is read up to this many bytes; the rest is never read
ERROR_BODY_WAIT_S = 30.0       # and for at most this long (never past the turn's deadline)


def _read_error_body(src: Any, end: float, clock: Callable[[], float], gate: _Gate) -> bytes | None:
    """At most ERROR_BODY_MAX bytes of an upstream error response, read on a helper thread that is waited for until the
    turn's deadline (and ERROR_BODY_WAIT_S at most) (#114 review round 2, finding 2): a stalled or dribbling error body
    cannot hold the turn past its cap. None when it could not be read in time or failed. `src` is always closed; a late
    read is woken by aborting the gate's socket."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["body"] = src.read(ERROR_BODY_MAX + 1)
        except BaseException as exc:      # noqa: BLE001 - an unreadable body is just not relayed
            box["exc"] = exc
        done.set()

    threading.Thread(target=run, name="cortados-api-error-body", daemon=True).start()
    in_time = done.wait(max(0.0, min(end - clock(), ERROR_BODY_WAIT_S)))
    if not in_time:
        gate.cancel()
    try:
        src.close()
    except Exception:
        pass
    if not in_time or "exc" in box or not isinstance(box.get("body"), (bytes, bytearray)):
        return None
    return bytes(box["body"][:ERROR_BODY_MAX])


def api_key() -> str:
    """The credit key (api_credits.api_key: the provider's CORTADOS_ANTHROPIC_API_KEY). Never logged or relayed."""
    from cortados_router import api_credits
    return api_credits.api_key()


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


def _fold(reply: list, ev: dict[str, Any]) -> None:
    """Rebuild the streamed answer's content blocks (text and tool_use ids are all a reply fingerprint reads)."""
    t = ev.get("type")
    if t == "content_block_start" and isinstance(ev.get("content_block"), dict):
        reply.append(dict(ev["content_block"]))
    elif t == "content_block_delta" and reply and isinstance(ev.get("delta"), dict):
        d = ev["delta"]
        if d.get("type") == "text_delta" and reply[-1].get("type") == "text":
            reply[-1]["text"] = reply[-1].get("text", "") + str(d.get("text", ""))


def _update_usage_from_sse(usage: dict[str, Any], text: str, reply: list | None = None) -> tuple[bool, bool]:
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
        if reply is not None:
            _fold(reply, ev)
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


def _named(message: Any, suffix: str | None, echo: str | None) -> bool:
    """Rename a message's `model` in place: to `echo` (the name the client asked for, on a routed conversation) and/or
    with `suffix` appended (a revealing client). False (left untouched) when it names none."""
    if not isinstance(message, dict) or not isinstance(message.get("model"), str):
        return False
    if echo:
        message["model"] = echo
    if suffix:
        message["model"] += suffix
    return True


def _reveal_chunk(chunk: bytes, suffix: str | None, echo: str | None = None) -> tuple[bytes, bool]:
    """The chunk with its message_start event's model renamed (_named), and whether it was. Any other chunk, or one
    that does not parse, is returned byte-for-byte: the rename is cosmetic and never alters the relayed content."""
    if b"message_start" not in chunk:
        return chunk, False
    try:
        lines = chunk.split(b"\n")
        for i, line in enumerate(lines):
            if line.startswith(b"data: ") and b"message_start" in line:
                event = json.loads(line[6:])
                if event.get("type") == "message_start" and _named(event.get("message"), suffix, echo):
                    lines[i] = b"data: " + json.dumps(event).encode()
                    return b"\n".join(lines), True
    except Exception:
        pass
    return chunk, False


def _send_error(handler: Any, code: int, kind: str, message: str) -> None:
    """A cortados-generated error, built by anthropic_wire.error_json (its message escaped and capped, #117 review
    round 5), sent best-effort: a client that is gone changes nothing."""
    _send_raw_error(handler, code, "application/json", json.dumps(error_json(kind, message)).encode())


def _send_raw_error(handler: Any, code: int, content_type: str, payload: bytes) -> None:
    try:
        handler.send_response(code)
        handler.send_header("content-type", content_type)
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    except OSError:
        pass


def upstream_error_payload(code: int, payload: bytes) -> bytes:
    """The body relayed for an upstream HTTP error: a JSON object with its error message escaped and capped and every
    other field as the API sent it (safe_error_body); anything else (HTML from a proxy, a truncated body) becomes a
    cortados error quoting its start, escaped the same way."""
    safe = safe_error_body(payload)
    if safe is not None:
        return safe
    return json.dumps(error_json("api_error", f"cortados: upstream rejected the request (HTTP {code}): "
                                 f"{payload[:300].decode('utf-8', 'replace')}")).encode()


def serve(handler: Any, body: dict[str, Any], model: str, now: float, *, key: str | None = None,
          urlopen: Callable[..., Any] | None = None, model_suffix: str | None = None, echo_model: str | None = None,
          reply: list | None = None, commit: Callable[[list], None] | None = None,
          deadline: float | None = None, clock: Callable[[], float] = time.monotonic) -> tuple[dict[str, Any], str]:
    """Proxy one turn to the real API and relay the response to the client. Returns (usage, outcome):
      * ("completed"): a terminal response was received — settle the actual cost from `usage`.
      * ("rejected"):  the API returned an HTTP error, or the request was never written (refused before dispatch,
                       a failed DNS/connect/TLS, or the gate shut at the deadline before the first byte) — it was
                       not billed, settle zero.
      * ("unknown"):   the turn did not terminate for us (timeout, transport error, or a mid-stream disconnect) —
                       the spend is unknown, so the caller settles the FULL reservation (conservative).
      * ("aborted"):   the turn reached its hard wall-clock cap and was cut off (ABORTED). Closing the connection is no
                       proof the API stopped generating or billing, so the caller settles the FULL reservation too.
    The cap: `deadline` (on `clock`, time.monotonic by default; the caller passes the one its reservation was admitted
    for), else turn_cap_s() from the send. It is enforced on the whole turn: the dispatch (DNS, connect, TLS, send, the
    wait for the response headers) runs on a helper thread joined until the deadline, after which the turn's gate is
    shut so the request can no longer be written; every socket operation is bounded by the time left, a timer aborts
    the socket at the deadline (a stalled read wakes), the stream is checked against `clock` after every chunk, and an
    error body is read only until the deadline. `urlopen` is a test seam: a fake opener called (through the gate) in
    place of the real transport.
    Usage is parsed before each client write, so a disconnect cannot lose it. `model_suffix` (a revealing client
    only) is appended to the success response's `model`; usage and billing always read the upstream bytes as sent.
    A routed conversation (server._relay_api) also passes `echo_model` (the model name the client asked for, which the
    response then names), `reply` (filled with the answer's content blocks) and `commit` (called with them just before
    the terminal bytes are written, so the conversation's history is recorded before the client can continue it)."""
    key = (key if key is not None else api_key()).strip()
    try:
        url, data, headers = build_upstream(body, model, key)
    except ValueError as exc:
        # A pre-dispatch construction/validation error (e.g. a misconfigured non-HTTPS/non-Anthropic upstream): the
        # request is NOT built and nothing is sent, so nothing is billed -> a certain rejection (settle zero). This
        # catch is scoped to build_upstream ONLY, so it never masks a post-dispatch opener/response error as rejected.
        _send_error(handler, 400, "invalid_request_error", f"cortados: request not dispatched ({exc})")
        return {}, "rejected"
    bad_header = invalid_header(headers)
    if bad_header is not None:
        # A malformed outbound header (a client-supplied beta, or a mis-configured credit key) means nothing is sent,
        # so nothing is billed -> a certain pre-dispatch rejection. Relay a 400 naming the header (never its value).
        _send_error(handler, 400, "invalid_request_error",
                    f"cortados: outbound header {bad_header!r} is malformed; request not dispatched")
        return {}, "rejected"
    if urlopen is None:
        proxy = _proxy_for(ALLOWED_HOST)
        if proxy is not None:
            # Nothing is sent (settle zero): the lane's transport owns its socket so it can cancel the dispatch, and it
            # never hands the credit key to a proxy it cannot cancel through.
            _send_error(handler, 502, "api_error", "cortados: the anthropic-api credit lane connects to the API only "
                        "directly and an HTTPS proxy is configured; request not dispatched")
            return {}, "rejected"
    streaming = bool(body.get("stream"))
    cap = turn_cap_s()
    start = clock()
    end = start + cap if deadline is None else min(float(deadline), start + cap)
    left = end - start
    if left <= 0:
        _send_error(handler, 504, "api_error", "cortados: the API credit lane's time cap for this turn had passed; "
                    "request not dispatched")
        return {}, "rejected"            # nothing was sent: nothing billed
    gate = _Gate(end, clock)
    per_op = min(cap, left)
    if urlopen is None:
        def open_fn() -> Any:
            return _dispatch(url, data, headers, gate, per_op)
    else:
        # A test's fake opener: calling it counts as writing the request, so it is called only through the gate.
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")

        def open_fn() -> Any:
            if not gate.may_write():
                raise _Cancelled("the API credit lane's turn deadline passed before the request was sent")
            return urlopen(req, timeout=per_op)
    error: tuple[int, Any] | None = None
    try:
        resp, late = _open_within(open_fn, gate, left)
        if late:
            if not gate.sent:
                # The gate was shut before any request byte was written, and no late worker can write one now.
                logger.warning("anthropic-api turn reached its %.0f s cap before the request was sent", left)
                _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap before the request "
                            "could be sent; request not dispatched")
                return {}, "rejected"
            logger.warning("anthropic-api turn cut off at its %.0f s cap before the response began", left)
            _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap before it answered; "
                        "it was cut off (its reservation is kept in full)")
            return {}, ABORTED
        status = getattr(resp, "status", 200)
        if isinstance(status, int) and not 200 <= status < 300:
            error = (status, resp)           # the gated transport returns every status; urllib raised HTTPError
    except urllib.error.HTTPError as exc:
        error = (exc.code, exc)
    except (urllib.error.URLError, OSError) as exc:
        if not gate.sent:
            # Failed before any request byte was written (DNS, connect, TLS, or the gate shut): nothing billed.
            if clock() >= end:
                _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap before the "
                            "request could be sent; request not dispatched")
            else:
                _send_error(handler, 502, "api_error", f"cortados: upstream API unreachable ({type(exc).__name__}); "
                            "request not dispatched")
            return {}, "rejected"
        if clock() >= end:                 # the per-operation timeout is the time left: this IS the cap
            _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap before it answered; "
                        "it was cut off (its reservation is kept in full)")
            return {}, ABORTED
        _send_error(handler, 502, "api_error", f"cortados: upstream API unreachable: {exc}")
        return {}, "unknown"   # dispatch may have reached the API; settle conservatively
    except Exception as exc:
        # Headers were validated above, so this is NOT a pre-dispatch rejection: the POST may well have been sent
        # (e.g. the stdlib raised parsing the response headers). The spend is uncertain -> "unknown", never a $0
        # rejection. Best-effort 502 to the client.
        _send_error(handler, 502, "api_error", "cortados: upstream response could not be read")
        return {}, "unknown"
    if error is not None:
        # The API REJECTED the turn (or answered a redirect, which is never followed): nothing is billed, whether or
        # not its body can be read. The body is read only up to ERROR_BODY_MAX bytes and only until the turn's deadline
        # (#114 review round 2, finding 2), the response is always closed, and the body is relayed with its error
        # message escaped and capped, the other fields as sent (#117 review round 5).
        code, src = error
        raw_err = _read_error_body(src, end, clock, gate)
        if raw_err is None:
            payload = json.dumps(error_json("api_error", "cortados: upstream rejected the request")).encode()
        else:
            payload = upstream_error_payload(code, raw_err)
        _send_raw_error(handler, code, "application/json", payload)
        return {}, "rejected"

    cutoff = _Cutoff(resp, end - clock(), gate)
    try:
        if not streaming:
            try:
                raw = resp.read()
            except (OSError, http.client.HTTPException, ValueError, AttributeError):
                if cutoff.fired or clock() >= end:      # the cutoff, or the read timeout set to the time left
                    _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap and was cut "
                                "off (its reservation is kept in full)")
                    return {}, ABORTED
                return {}, "unknown"   # the turn may have been billed, but we have no usable response -> conservative
            if cutoff.fired or clock() > end:
                # Read past the cap: what arrived may be cut short by the shutdown; never relayed as an answer.
                _send_error(handler, 504, "api_error", "cortados: the API turn reached its time cap and was cut off "
                            "(its reservation is kept in full)")
                return {}, ABORTED
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
            if (model_suffix or echo_model) and isinstance(parsed, dict) and parsed.get("type") == "message":
                try:
                    if _named(parsed, model_suffix, echo_model):
                        raw = json.dumps(parsed).encode()
                except Exception:
                    pass
            if outcome == "completed" and reply is not None:
                reply.extend(b for b in (parsed.get("content") or []) if isinstance(b, dict))
                if commit is not None:
                    commit(reply)
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
        revealed = not (model_suffix or echo_model)
        committed = False
        aborted = False
        try:
            for chunk in resp:
                if cutoff.fired or clock() > end:
                    aborted = True                        # the chunk arrived past the cap: not relayed, not counted
                    break
                text = chunk.decode("utf-8", "replace")
                saw_stop, saw_final = _update_usage_from_sse(usage, text, reply)
                completed = completed or saw_stop        # a PARSED message_stop event
                final_output = final_output or saw_final  # a message_delta supplied the real final output count
                if not revealed:
                    chunk, revealed = _reveal_chunk(chunk, model_suffix, echo_model)
                if saw_stop and commit is not None and reply is not None and not committed:
                    committed = True
                    commit(reply)                         # before the terminal event reaches the client
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except OSError:
                    break  # client gone: stop relaying; the billing outcome is decided by what we already parsed
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            pass   # an upstream read failed mid-stream (or the cutoff closed it under the read); keep whatever usage/terminal state we already captured
        if (aborted or cutoff.fired or clock() >= end) and not completed:
            logger.warning("anthropic-api stream cut off at its %.0f s cap", left)
            err = {"type": "error", "error": {"type": "api_error", "message": "cortados: the API turn reached its time "
                                              "cap and was cut off"}}
            try:
                handler.wfile.write(f"event: error\ndata: {json.dumps(err)}\n\n".encode())
                handler.wfile.flush()
            except OSError:
                pass
            return usage, ABORTED
        # "completed" only when the terminal event was parsed, the real final output count arrived (not just the
        # message_start estimate), AND the merged usage is usable. Otherwise the cost is not reliably known, so the
        # turn stays "unknown" and keeps the conservative reservation. A parsed terminal event means the full turn
        # was received and billed regardless of a later client/upstream failure.
        return usage, ("completed" if completed and final_output and has_usable_usage(usage) else "unknown")
    finally:
        cutoff.cancel()
        try:
            resp.close()
        except Exception:
            pass
