"""Token gate in front of a node's Ollama: a small threaded reverse proxy run ON the node (the Pro).

Ollama has no authentication, so it listens on 127.0.0.1 only and this gate is the one door to it from the tailnet:

  * Only the two paths the provider's local lane uses are forwarded: POST /v1/messages and GET /api/tags. Everything
    else (model pulls, deletes, /api/generate, …) is a 404: a leaked token must not be able to manage the node.
  * Every request carries `Authorization: Bearer <token>` or `x-api-key: <token>`, compared in constant time
    (hmac.compare_digest) against a token read from a file that only its owner can read. The check runs before the
    body is read, and a refusal is a fixed 401 that echoes nothing back.
  * The token and request bodies are never logged; a log line is method, path and status.
  * The backend behind the gate is Ollama (default) or vllm-mlx (local_backend.py: a per-node config file); the client
    routes above stay the same and each backend receives only its fixed upstream routes (local_backend.UPSTREAM).
  * Admission (local_governor.py) bounds the POST /v1/messages running at once when the backend config has a
    `governor`: a COUNT that follows the owner's activity and memory pressure, and a BYTE budget each request reserves
    its KV cache from (input tokens + max_tokens; input tokens bounded from above by the request body's bytes as
    received plus a template allowance and what the backend re-serializes wider, local_governor.input_tokens; no
    backend call). The count slot and the bytes are taken in one step. A request over either, now, gets a retryable
    529 overloaded_error with Retry-After at once (never queued here); one that could not fit the budget even alone gets
    a non-retryable 413 request_too_large (TOO_LARGE_MESSAGE), which the provider routes elsewhere. On vllm-mlx a turn
    carrying content the served model cannot take is refused non-retryably before admission (UNSUPPORTED_MESSAGE): for a
    text-only model any non-text block; for a vision model (#125, its entry's `vision` block) anything outside its
    still-image policy (local_governor.image_policy). An admitted image turn also takes one of the model's image slots,
    in the same step as its count slot and bytes, and reserves its image tokens and the computed media-prefill bytes (local_governor.image_prefill_bytes). A body the gate cannot accept (another model, a chat_template_kwargs key other
    than enable_thinking, JSON nested too deeply) is a non-retryable 4xx (x-should-retry: false). The reservation is
    capped at the context because vllm-mlx runs under a prompt guard that refuses longer prompts; its refusal becomes
    the gate's never-fits 413 (local_backend.guard_refusal). Running requests are never
    stopped; a reservation is released however the request ends. The limit, in-flight count and reserved/free bytes
    are reported read-only as `cortados_gate` in GET /api/tags and in the switch status.
  * One more path is the gate's own, never forwarded: GET / POST /cortados/local-switch (SWITCH_PATH), the node's
    manual local-models switch, behind the same token. GET answers the switch, AC/battery, whether local models are
    allowed and why, and the models Ollama has loaded (names from /api/ps only); POST {"on": true|false} sets the switch
    through local_resources.set_local_switch, the toggle CLI's own code. It answers while the switch is off or on
    battery (it is how the owner sees and changes the state); inference keeps its refusal.

Run:   python3 -m cortados_router.local_gate --listen <tailscale-ip>
Check: python3 -m cortados_router.local_gate --listen <tailscale-ip> --print-config
Env equivalents: CORTADOS_LOCAL_GATE_LISTEN, CORTADOS_LOCAL_GATE_PORT, CORTADOS_LOCAL_GATE_UPSTREAM,
CORTADOS_LOCAL_GATE_TOKEN_FILE, CORTADOS_LOCAL_GATE_STUDIO_GATE_DIR, CORTADOS_LOCAL_BACKEND_CONFIG. Stdlib only.
While S1 training holds the node (LOCK_EX on <studio-gate dir>/node.lock), every forwarded request is a 503: each
request holds LOCK_SH on that file from before it reaches Ollama until its response is fully relayed.
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import ipaddress
import http.client
import io
import json
import logging
import math
import os
import secrets
import socket
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cortados_router import http_body, local_backend, local_governor, local_resources
from cortados_router.local_resources import pro_local_allowed
from cortados_router.studio_capacity import (gate_dir as default_studio_gate_dir, live_intents,
                                             open_node_lock, verify_node_lock)

logger = logging.getLogger("cortados.local_gate")

DEFAULT_PORT = 11435                      # next to Ollama's 11434, which stays on loopback
DEFAULT_UPSTREAM = "http://127.0.0.1:11434"
DEFAULT_TOKEN_FILE = "~/.config/cortados/local-gate-token"
# A guessable token would make the gate decoration. The floor is 32 hex characters (128 bits, what
# `openssl rand -hex 16` prints); the documented command, `openssl rand -hex 32`, prints 64 (256 bits).
MIN_TOKEN_CHARS = 32
# A Claude Code conversation is resent whole every turn, tool results included; several MB is normal, tens is not.
# The bound keeps one request from holding the laptop's memory hostage.
MAX_BODY_BYTES = 32 * 2 ** 20
# Concurrent connections: 0 = unlimited, the default (owner decision 2026-11-08). Several orchestrating sessions may
# each run many local-model subagents at once (3 x 10 = 30 is normal), and the gate is not where concurrency belongs:
# Ollama's OLLAMA_NUM_PARALLEL decides how many requests run at once and its OLLAMA_MAX_QUEUE how many wait, and a
# request beyond both gets Ollama's own error back. A gate cap below those would turn a queued, eventually-served
# request into a 503. Idle and trickling sockets are still bounded per connection by the timeouts and deadlines below.
# A positive --max-connections still caps (over the cap: an immediate 503), for an operator who wants a thread bound.
MAX_CONNECTIONS = 0
# Loopback connects in microseconds: a short connect wait tells "Ollama is not running" quickly. Reads wait long
# because a first model load (16 GB) can take a minute before the first byte, but are bounded so a hung Ollama never
# pins a gate thread forever.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 300.0
# A connection holds a gate thread (and, under a positive max_connections, one of its slots) from accept, before it
# has sent anything. Without a socket timeout, idle sockets (or Tailscale links dropped mid-request, which never send
# a FIN) would pile up threads forever, and under a cap hold every slot and lock the lane out. 30 s is far more than a provider needs to send its request line and body.
IDLE_TIMEOUT_S = 30.0
# The idle timeout only measures silence: a client trickling one byte every few seconds never trips it. So each phase
# of reading a request also has an absolute deadline, after which the connection is closed and its slot freed.
#   headers: the provider sends its request line and headers in one write; 10 s is generous even for a waking link.
HEADER_DEADLINE_S = 10.0
#   body: up to MAX_BODY_BYTES over Tailscale between home machines; 32 MiB at a slow 5 Mbit/s is ~55 s.
BODY_DEADLINE_S = 120.0
#   drain of a refused request: only a courtesy so the 401 arrives (see _drain); a client that cannot send its
#   body within 5 s is closed without the 401, since a well-behaved provider is never refused in the first place.
DRAIN_DEADLINE_S = 5.0
# The only routes the provider's local lane uses (local_lane.probe and local_lane.serve), whatever the backend; what
# each becomes upstream is local_backend.UPSTREAM's fixed table.
ROUTES = {("GET", "/api/tags"), ("POST", "/v1/messages")}
# The key of the gate's capacity report in a GET /api/tags answer and a busy (529) answer; the provider reads it
# (local_lane.GATE_KEY).
GATE_KEY = "cortados_gate"
BUSY_MESSAGE = "local gate: busy"           # the fixed prefix of an over-limit refusal (local_lane.GATE_BUSY)
# The fixed prefix of a refusal that no retry can change: the request's reservation exceeds the node's whole byte
# budget (local_lane.GATE_TOO_LARGE). Answered 413 request_too_large with x-should-retry: false.
TOO_LARGE_MESSAGE = "local gate: too large for this node's memory"
# The fixed prefix of the other refusal no retry can change: the turn carries content the node's model cannot take, on
# a vllm-mlx node: for a TEXT-ONLY model an image, a document, any non-text block (local_governor.non_text_content);
# for a vision model anything outside its still-image policy (local_governor.image_policy, #125). Answered 400 invalid_request_error with x-should-retry: false, before any
# admission; the provider routes the turn elsewhere (local_lane.GATE_UNSUPPORTED).
UNSUPPORTED_MESSAGE = "local gate: content this node's model cannot read"
# The capacity report's fields (GateServer.capacity, the 529 and 413 answers). Bytes and tokens are None without a
# governor: reserved/free bytes of the budget beside the weights, and the most tokens one request may hold alone
# (fit_tokens) and beside what runs now (free_tokens), which the provider's routing reads.
CAPACITY_KEYS = ("limit", "inflight", "retiring", "draining", "owner", "pressure", "reason", "reserved_bytes",
                 "free_bytes", "fit_tokens", "free_tokens", "image_inflight", "image_limit")
# Retirement (#126 review round 3, P1). On a vllm-mlx node serving a text-only model under a governor, a text turn's
# ticket is NOT released when forwarding ends: closing the upstream connection does not end the request in vllm-mlx (it
# defers the stream's close by 0.5 s and queues the scheduler abort until the executor's next step), so its rows may
# still be in the decode batch. Each forwarded turn carries a fresh ticket id (TICKET_HEADER:
# "<generation>.<seq>.<rand>", no request data: this gate process's random 128-bit generation, its counter that only
# goes up, 128 random bits; no wall clock, #126 review round 5); when forwarding ends the ticket turns "retiring" (it
# keeps its count slot, its bytes and its place in the text set) and the Retirer asks the prompt guard inside vllm-mlx
# (RETIRE_PATH on the upstream) which retiring ids it has seen leave the scheduler. Only that confirmation releases the
# ticket. The guard confirms an id it never saw by tombstoning it (refusing it should it still arrive) or because its
# generation is refused, so a backend restart (a new process: every row freed) confirms every old id once this gate has
# fenced it. No confirmation (backend hung or unreachable): the ticket stays reserved and the node reports busy
# ("retiring" in the capacity report): fail closed. A connect that failed sent nothing: released at once. This holds on
# every vllm-mlx node serving a text-only model, governed or not: its guard refuses a generating request without a fresh
# ticket (409), so the gate and the guard ship together (scripts/install_mlx_backend.sh copies both).
# integrations/vllm-mlx/cortados_vllm_mlx_guard.py (TicketLedger) is the other side; a test keeps the names equal.
# The fence (#126 review round 4, P1b). The gate's admissions and retiring tickets live in this process only, and the
# gate's LaunchAgent restarts it (KeepAlive) while vllm-mlx runs on: rows a previous gate's turns left in the batch are
# reserved by nobody. So the gate starts fenced: it admits no text turn (529, busy: retry, with the reason in the
# capacity report's "fence") until the guard answers FENCE_PATH {"generation": <this gate's>, "known": [every id this
# gate minted and has not released]} with "clear": no ticket of any other generation is admitted or bound any more, and
# no request bound to a ticket outside `known` is pending, queued or in the scheduler (atomic with admission in the
# guard: TicketLedger.fence). The gate fences itself again whenever it can no longer vouch for the backend's state: a
# retirement poll or fence query failed (lost contact), or the guard's boot id changed (a new vllm-mlx process: its
# predecessor's rows went with it: the fence lifts at once, and then every old id is retired).
# Chosen over reserving the backend's reported rows at full width: that needs the rows' widths or a worst case per
# row, and a second reservation path; the fence needs one query and admits nothing it cannot account for.
TICKET_HEADER = "x-cortados-ticket"
RETIRE_PATH = "/cortados/retire"
FENCE_PATH = "/cortados/fence"
FENCE_STARTING = "fenced: starting, waiting for the backend's guard to confirm no rows of a previous gate remain"
RETIRE_POLL_S = 0.2                         # between confirmation polls while tickets are retiring
RETIRE_BACKOFF_S = 2.0                      # the longest wait between polls after a failed one
RETIRE_TIMEOUT_S = 5.0                      # one poll, end to end (deadline_call)
RETIRE_BATCH = 256                          # ids per poll
RETIRE_MAX_BYTES = 1 << 20                  # the guard's answer is ids it was asked about only
# The served model's declared image capability in the report (#125): None for a text-only model, else these settings of
# its vision policy (local_governor.VISION_DEFAULTS), so the provider sees that the node reads still images and within
# which limits. Static config, no request data.
VISION_REPORT_KEYS = ("max_images", "image_tokens_max", "image_context_tokens", "image_concurrency",
                      "max_image_bytes", "max_image_pixels")
# The gate's own switch endpoint (module docstring). Not in ROUTES: it never reaches Ollama as a forwarded request.
SWITCH_PATH = "/cortados/local-switch"
# A switch request body is {"on": true} or {"on": false}; 1 KiB is ample, and a larger one is refused unread.
SWITCH_MAX_BODY = 1024
# The status asks Ollama which models are loaded (GET /api/ps on loopback). A loopback connect takes microseconds (1 s
# says "Ollama is not running") and the answer milliseconds; 2 s bounds a hung Ollama, so the whole status (pmset's own
# 2 s cap included) stays under the provider's 6 s wait (provider/local_switch.GATE_TIMEOUT_S) and still answers, with
# the models unknown. At most PS_MAX_MODELS names are reported. PS_TIMEOUT_S is end to end (connect, headers and body,
# deadline_call): a trickling Ollama cannot stretch it by sending a byte now and then.
PS_CONNECT_S = 1.0
PS_TIMEOUT_S = 2.0
PS_MAX_MODELS = 32
PS_MAX_BYTES = 1024 * 1024

class _DeadlineSocket:
    """A connected socket under an absolute monotonic deadline: every send and every receive waits at most the time
    left before `end`, so bytes arriving now and then cannot extend the call (a plain socket timeout is an inactivity
    timeout and resets on each byte). Past the deadline it raises TimeoutError (an OSError) before waiting at all.
    Stands in for HTTPConnection.sock: http.client only sends with sendall and reads through makefile("rb")."""

    def __init__(self, sock: socket.socket, end: float):
        self._sock, self._end = sock, end
        self._readers, self._closed = 0, False

    def _arm(self) -> None:
        left = self._end - time.monotonic()
        if left <= 0:
            raise TimeoutError("deadline passed")
        self._sock.settimeout(left)

    def sendall(self, data, *args):
        self._arm()
        return self._sock.sendall(data, *args)

    def recv_into(self, buf, *args):
        self._arm()
        return self._sock.recv_into(buf, *args)

    def makefile(self, mode: str = "rb", *args, **kwargs):
        if "w" in mode:
            raise ValueError("a deadline socket is read through makefile('rb') only")
        self._readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def close(self) -> None:
        # Like socket.socket: http.client closes the connection while a "Connection: close" response is still being
        # read, so the real socket closes only once no reader is open.
        self._closed = True
        if self._readers <= 0:
            self._sock.close()

    def _reader_closed(self) -> None:
        self._readers -= 1
        if self._closed and self._readers <= 0:
            self._sock.close()


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock: _DeadlineSocket):
        super().__init__()
        self._s = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        return self._s.recv_into(buf)

    def close(self) -> None:
        if not self.closed:
            self._s._reader_closed()
        super().close()


_new_socket = socket.socket                      # the one seam the multi-address test replaces


def _resolve_by(host: str, port: int, end: float) -> list:
    """getaddrinfo results for (host, port) as stream sockets, given up at the monotonic `end` (TimeoutError).
    A numeric IP is never resolved. getaddrinfo itself takes no timeout, so a name is resolved in a daemon thread that
    is left to finish on its own when the deadline passes (it holds nothing but its own result)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:                      # "fe80::1%en0" (a scope id) is not parsed here on 3.9 and is resolved below
        if ip.version == 4:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (host, port, 0, 0))]
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["infos"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except BaseException as exc:            # handed to the caller; nothing escapes the thread
            box["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="deadline-resolve", daemon=True).start()
    if not done.wait(max(0.0, end - time.monotonic())):
        raise TimeoutError("name resolution did not finish before the deadline")
    if "exc" in box:
        raise box["exc"]
    return box["infos"]


def _connect_by(address: tuple, connect_end: float, end: float, source_address=None) -> socket.socket:
    """socket.create_connection with one absolute deadline: name resolution and every connection attempt share the
    time left before `connect_end`, and no attempt starts once it has passed. The connected socket's timeout is the
    time left before `end` (a TLS handshake that follows waits no longer than the call has left)."""
    host, port = address[0], address[1]
    last: OSError | None = None
    for family, type_, proto, _, sockaddr in _resolve_by(host, port, connect_end):
        left = connect_end - time.monotonic()
        if left <= 0:
            break
        sock = _new_socket(family, type_, proto)
        try:
            sock.settimeout(left)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
        except OSError as exc:
            sock.close()
            last = exc
            continue
        left = end - time.monotonic()
        if left <= 0:
            sock.close()
            break
        sock.settimeout(left)
        return sock
    if last is not None and time.monotonic() < connect_end:
        raise last
    raise TimeoutError("connect did not finish before the deadline")


def deadline_call(conn: http.client.HTTPConnection, deadline_s: float, method: str, path: str, *,
                  body: bytes | None = None, headers: dict[str, str] | None = None, max_bytes: int,
                  connect_s: float | None = None, on_connect_error=None) -> tuple[int, bytes]:
    """One HTTP request on an unconnected `conn` within `deadline_s` seconds END TO END: the connect (name resolution
    and every address tried, together also capped at `connect_s`; a numeric IP is not resolved), the request, the status line and headers and the body all share one monotonic deadline, each wait
    given only the time left. The body is read incrementally, at most `max_bytes` (the rest is left unread).
    (status, body bytes); OSError (TimeoutError past the deadline) or http.client.HTTPException otherwise. A connect
    failure is passed to `on_connect_error(exc)` first when given, so a caller can tell "not reachable" from "did not
    answer". Stdlib only: the provider's switch view (provider/local_switch.py) uses it too.
    Not http_body.read_within: that is the SERVER side (a body a client sends us, on a socket http.server already
    accepted, after the request line and headers were read). This is the CLIENT side: the connect, our request, the
    status line and headers and then the body all share the one deadline, through _DeadlineSocket under http.client's
    own reader, which a body-only reader on a raw rfile cannot cover."""
    start = time.monotonic()
    end = start + deadline_s
    connect_end = start + min(deadline_s, connect_s if connect_s is not None else deadline_s)
    conn.timeout = max(0.001, connect_end - start)
    # http.client's connect (and HTTPSConnection's, which wraps the result in TLS) opens the socket through this
    # per-instance hook; socket.create_connection would resolve without any timeout and give EVERY resolved address a
    # full timeout, so a stalled resolver or a dead multi-address name could hold the call far past the deadline.
    conn._create_connection = lambda address, timeout=None, source_address=None: _connect_by(
        address, connect_end, end, source_address)
    try:
        conn.connect()
    except (OSError, http.client.HTTPException) as exc:
        if on_connect_error is not None:
            on_connect_error(exc)
        raise
    conn.sock = _DeadlineSocket(conn.sock, end)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    parts, left = [], max_bytes
    try:
        while left > 0:
            chunk = resp.read1(min(left, 65536))
            if not chunk:
                break
            parts.append(chunk)
            left -= len(chunk)
    finally:
        resp.close()                                    # an unread rest is dropped with the connection, never waited for
    return resp.status, b"".join(parts)


_UNAUTHORIZED = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                        "message": "local gate: bad or missing token"}}).encode()


class GateConfigError(ValueError):
    pass


def training_hold(gate_dir: str | os.PathLike | None, now: float | None = None) -> str | None:
    """Advisory fast path: why the node must not serve now because an S1 trainer announced an intent, or None.
    Read-only (never deletes or modifies an intent); the guarantee is the node lock in _handle_one.

    A pinned request loads a model; during training that would compete with the trainer for unified memory, so any
    live intent in the studio-gate handshake directory (studio_capacity.training_gate writes them) refuses the request.
    A missing directory means no trainer has ever run here (training_gate creates it): free. A directory that exists
    but cannot be read fails closed, because an intent we cannot see is still an intent. The returned reason names
    no path, pid or nonce: it goes back to the remote client."""
    if gate_dir is None:
        return None
    gate = Path(gate_dir)
    try:
        if not gate.exists():
            return None
        os.listdir(gate)                          # Path.glob swallows a permission error; listdir raises it
        live = live_intents(gate, time.time() if now is None else now, prune=False)  # read-only, advisory
    except OSError as exc:
        logger.warning("studio-gate dir unreadable (%s): refusing local requests", type(exc).__name__)
        return "S1 training state unreadable; local model refused"
    if live:
        return "S1 training in progress on this node; local model refused until it ends"
    return None


def read_token(path: str | os.PathLike) -> str:
    """The token in `path`, refusing a file that is missing, not a regular file, readable or writable by group or
    others (the token is the only thing between the tailnet and the node's models), or too short."""
    p = Path(os.path.expanduser(str(path)))
    try:
        st = p.stat()
    except OSError as exc:
        raise GateConfigError(f"token file {p} missing or unreadable ({exc.strerror}); create it with mode 0600") from exc
    if not stat.S_ISREG(st.st_mode):
        raise GateConfigError(f"token file {p} is not a regular file")
    if st.st_mode & 0o077:
        raise GateConfigError(f"token file {p} has mode {stat.S_IMODE(st.st_mode):04o}; group/other access must be "
                              "none (chmod 600)")
    try:
        token = p.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise GateConfigError(f"token file {p} unreadable ({type(exc).__name__})") from exc
    if len(token) < MIN_TOKEN_CHARS:
        raise GateConfigError(f"token in {p} is shorter than {MIN_TOKEN_CHARS} characters (128 bits); make one with "
                              "`openssl rand -hex 32`")
    return token


def check_listen(host: str, allow_any: bool) -> str:
    """The address to bind, validated: parsed as a numeric ASCII IP literal and bound in its canonical form (the node's
    Tailscale address).
    Hostnames, legacy spellings ("0", "0x0") and non-ASCII digits are refused, because the socket layer resolves or
    normalises them (fullwidth "０.０.０.０" binds as 0.0.0.0), so what was checked would not be what is bound.
    An all-interfaces address is refused unless asked: on a laptop that joins cafe Wi-Fi, 0.0.0.0 puts the gate on
    every network it meets, and the token would be the node's only defence there. Empty means all interfaces."""
    h = (host or "").strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        ip = ipaddress.ip_address("0.0.0.0")
    else:
        if not h.isascii():
            raise GateConfigError(f"listen address {host!r} is not ASCII; give the numeric IP literal")
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            raise GateConfigError(f"listen address {host!r} is not a numeric IP literal (hostnames and legacy forms "
                                  "are refused: the bound address must be the one checked)") from None
        if str(ip) != h.lower() and not (ip.version == 6 and ipaddress.ip_address(h.lower()) == ip):
            raise GateConfigError(f"listen address {host!r} is not in canonical form (use {ip})")
    if ip.is_unspecified and not allow_any:
        raise GateConfigError(f"refusing to listen on all interfaces ({host or 'empty'}): listen on the node's "
                              "Tailscale address, or pass --allow-any if every network this machine joins is trusted")
    return str(ip)


def check_max_connections(n) -> int:
    """max_connections: 0 is unlimited (MAX_CONNECTIONS explains why that is the default), a positive integer caps.
    Negative, non-integer and bool values are refused rather than read as "unlimited"."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise GateConfigError(f"max_connections must be 0 (unlimited) or a positive integer, got {n!r}")
    return n


def check_limits(**limits: float) -> None:
    """Every timeout, deadline and size limit must be a finite positive number: 0 makes a socket non-blocking,
    a negative one raises mid-request, NaN compares false everywhere and inf is no bound at all."""
    for name, v in limits.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GateConfigError(f"{name} must be a finite positive number, got {v!r}")


class GateServer(ThreadingHTTPServer):
    daemon_threads = True
    # The listen backlog. socketserver's default of 5 resets connects beyond it when a burst arrives faster than the
    # accept loop drains it; uncapped, a burst of many subagents (3 sessions x 10) is the expected load, and a reset is
    # a refusal the gate must not make. 128 is macOS's default somaxconn, so asking for more would gain nothing.
    request_queue_size = 128

    def __init__(self, addr, *, token: str, upstream: str, max_body: int = MAX_BODY_BYTES,
                 max_connections: int = MAX_CONNECTIONS, connect_timeout: float = CONNECT_TIMEOUT_S,
                 read_timeout: float = READ_TIMEOUT_S, idle_timeout: float = IDLE_TIMEOUT_S,
                 header_deadline: float = HEADER_DEADLINE_S, body_deadline: float = BODY_DEADLINE_S,
                 drain_deadline: float = DRAIN_DEADLINE_S, studio_gate_dir: str | os.PathLike | None = None,
                 backend: dict | None = None, admission: local_governor.Admission | None = None):
        check_max_connections(max_connections)
        check_limits(max_body=max_body, connect_timeout=connect_timeout,
                     read_timeout=read_timeout, idle_timeout=idle_timeout, header_deadline=header_deadline,
                     body_deadline=body_deadline, drain_deadline=drain_deadline)
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        u = urllib.parse.urlparse(upstream)
        if u.scheme != "http" or not u.hostname:
            raise GateConfigError(f"upstream must be http://host:port (Ollama on loopback), got {upstream!r}")
        self.token = token.encode()
        self.upstream_host, self.upstream_port = u.hostname, u.port or 80
        self.max_body, self.connect_timeout, self.read_timeout = max_body, connect_timeout, read_timeout
        self.idle_timeout = idle_timeout
        self.header_deadline, self.body_deadline, self.drain_deadline = header_deadline, body_deadline, drain_deadline
        # None when unlimited: no slot is taken at accept and none is released after the request.
        self.slots = threading.BoundedSemaphore(max_connections) if max_connections > 0 else None
        # The S1 training handshake dir (None: no training check, e.g. a node that never trains).
        self.studio_gate_dir = studio_gate_dir
        # The backend (local_backend.load) and the admission count; absent: Ollama, unlimited (the gate as before).
        self.backend = backend or {"backend": "ollama", "served_model": None, "governor": None}
        if self.backend["backend"] not in local_backend.BACKENDS:
            raise GateConfigError(f"unknown backend {self.backend['backend']!r}")
        self.admission = admission if admission is not None else local_governor.Admission(None)
        # Retirement (RETIRE_PATH): a vllm-mlx node serving a text-only model, whose guard serves the text route with
        # the ticket ledger installed and confirms each release (a vision model's guard has no ledger: #125 unchanged).
        gov = self.admission.governor
        vision = gov.cfg.get("vision") if gov is not None else (self.backend.get("model") or {}).get("vision")
        self.retirer = Retirer(self.upstream_host, self.upstream_port, connect_timeout=connect_timeout) \
            if self.backend["backend"] == "vllm-mlx" and not vision else None
        super().__init__(addr, GateHandler)

    def server_close(self):
        if self.retirer is not None:
            self.retirer.stop()
        super().server_close()

    def capacity(self) -> dict:
        """The read-only capacity report: backend, the admission limit (None: unlimited) and how many run now, and
        the governor's owner/pressure state and reason (coarse words only, no idle seconds)."""
        return self.report(self.admission.snapshot())

    def report(self, snap: dict) -> dict:
        vision = self.admission.vision
        return {"backend": self.backend["backend"], **{k: snap.get(k) for k in CAPACITY_KEYS},
                "vision": {k: vision[k] for k in VISION_REPORT_KEYS} if vision else None,
                "fence": self.retirer.fenced() if self.retirer is not None else None}

    def process_request(self, request, client_address):
        if self.slots is None:                      # unlimited: no slot to take or give back
            super().process_request(request, client_address)
            return
        if not self.slots.acquire(blocking=False):
            try:
                body = b'{"type":"error","error":{"type":"overloaded_error","message":"local gate: too many connections"}}'
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\ncontent-type: application/json\r\n"
                                b"content-length: " + str(len(body)).encode() + b"\r\nconnection: close\r\n\r\n" + body)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if self.slots is not None:
                self.slots.release()


def new_ticket_id(generation: str, seq: int) -> str:
    """A retirement id (TICKET_HEADER): "<generation: 32 hex>.<seq: 16 hex>.<rand: 32 hex>", the gate process's
    generation, its counter (strictly increasing within the generation) and 128 random bits. No clock in it."""
    if len(generation) != 32 or not 0 <= seq < 1 << 64:
        raise ValueError("a ticket needs a 32-hex generation and a 64-bit counter")
    return f"{generation}.{seq:016x}.{secrets.token_hex(16)}"


class Retirer:
    """The retiring tickets (TICKET_HEADER / RETIRE_PATH above), the fence (FENCE_PATH above) and the one thread that
    asks the guard about them. mint() gives a forwarded turn its id (known to the fence until released);
    hand_over(ticket, id) marks the ticket retiring and queues it; forget(id) drops an id whose turn sent nothing; a
    poll POSTs {"tickets": [ids]} to the upstream's RETIRE_PATH and releases exactly the ids the answer lists under
    "retired". A failed poll (unreachable, an error status, an unreadable answer) releases nothing, fences the gate and
    backs off up to RETIRE_BACKOFF_S: fail closed. `call` (tests) replaces the HTTP call: call(path, body) -> the parsed
    answer. `generation` is this gate's (random, 128 bits, drawn at start; tests may pass one)."""

    def __init__(self, host: str, port: int, *, connect_timeout: float = CONNECT_TIMEOUT_S,
                 interval: float = RETIRE_POLL_S, call=None, start: bool = True, generation: str | None = None):
        self.host, self.port, self.connect_timeout, self.interval = host, port, connect_timeout, interval
        self._call = call or self._http
        self._cv = threading.Condition()
        self._pending: dict[str, local_governor.Ticket] = {}
        self._minted: set[str] = set()       # ids minted and not yet handed over or forgotten
        self._stopped = False
        self._start = start
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.generation = secrets.token_hex(16) if generation is None else generation
        self._seq = 0                        # the next ticket's counter (under _cv)
        self._fence: str | None = FENCE_STARTING
        self._fence_lock = threading.Lock()  # one fence query at a time (the thread or a waiting request)
        self._fence_gen = 0                  # fence queries started
        self._fence_failed = float("-inf")   # time.monotonic() when the last one failed
        self.boot: str | None = None         # the guard process the fence was last lifted against
        self._kick()

    # ---- the fence
    def fenced(self) -> str | None:
        """Why no text turn is admitted now, or None."""
        with self._cv:
            return self._fence

    def _raise_fence(self, why: str) -> None:
        with self._cv:
            if self._fence is None:
                logger.warning("local gate %s", why)
            self._fence = why
            self._cv.notify_all()
        self._kick()

    def _seen_boot(self, boot: object) -> None:
        """A guard answer's boot id: a different one than the fence was lifted against is a new vllm-mlx process."""
        if not isinstance(boot, str):
            return
        with self._cv:
            changed = self.boot is not None and boot != self.boot
            if not changed:
                self.boot = boot
        if changed:
            self._raise_fence("fenced: the backend restarted (a new guard process): waiting for it to confirm the "
                              "fence")

    def fence_once(self, shared: bool = False) -> str | None:
        """One fence query unless the gate is not fenced (`shared`: nor when another query ran while this one waited
        for the lock): the fence's reason after it (None: lifted). Never raises: a failed query keeps the fence up,
        with the failure in its reason."""
        with self._cv:
            gen = self._fence_gen
        with self._fence_lock:
            with self._cv:
                if self._fence is None or (shared and self._fence_gen != gen):
                    return self._fence            # lifted, or a query ran while this one waited: its answer stands
                self._fence_gen += 1
                known = sorted(self._minted | set(self._pending))
            try:
                answer = self._call(FENCE_PATH, {"generation": self.generation, "known": known})
                if not isinstance(answer, dict) or not isinstance(answer.get("clear"), bool):
                    raise ValueError("the guard's fence answer has no clear flag")
            except Exception as exc:              # noqa: BLE001 (any failure keeps the fence up)
                with self._cv:
                    self._fence_failed = time.monotonic()
                    self._fence = f"fenced: the backend's guard did not confirm the fence ({type(exc).__name__})"
                    return self._fence
            with self._cv:
                if not answer["clear"] and answer.get("refused") is True:
                    self._fence = ("fenced: the backend's guard refuses this gate's generation (a newer gate fenced "
                                   "it): restart this gate")
                    return self._fence
                if not answer["clear"]:
                    n = answer.get("unknown")
                    self._fence = (f"fenced: the backend still holds {n if isinstance(n, int) else 'some'} "
                                   "request(s) of turns this gate does not know (a previous gate's)")
                    return self._fence
                self._fence = None
                self.boot = answer["boot"] if isinstance(answer.get("boot"), str) else None
                logger.info("local gate fence lifted")
                return None

    def lift_now(self) -> str | None:
        """A request found the gate fenced: try the fence now rather than wait for the thread's next try, unless one
        failed in the last RETIRE_BACKOFF_S (a burst shares a running query: at most two RETIRE_TIMEOUT_S waits).
        The fence's reason after it, or None."""
        with self._cv:
            if self._fence is None:
                return None
            if time.monotonic() - self._fence_failed < RETIRE_BACKOFF_S:
                return self._fence
        return self.fence_once(shared=True)

    # ---- the ids
    def mint(self) -> str:
        with self._cv:
            tid = new_ticket_id(self.generation, self._seq)
            self._seq += 1
            self._minted.add(tid)
        return tid

    def forget(self, ticket_id: str) -> None:
        with self._cv:
            self._minted.discard(ticket_id)

    def hand_over(self, ticket: local_governor.Ticket, ticket_id: str) -> None:
        ticket.mark_retiring()
        with self._cv:
            self._minted.discard(ticket_id)
            self._pending[ticket_id] = ticket
            self._cv.notify_all()
        self._kick()

    def _kick(self) -> None:
        with self._cv:
            if self._start and not self._stopped and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._run, name="gate-retirer", daemon=True)
                self._thread.start()

    def pending(self) -> list[str]:
        with self._cv:
            return list(self._pending)

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._cv.notify_all()

    def _http(self, path: str, body: dict) -> object:
        conn = http.client.HTTPConnection(self.host, self.port)
        try:
            status, raw = deadline_call(conn, RETIRE_TIMEOUT_S, "POST", path, body=json.dumps(body).encode(),
                                        headers={"content-type": "application/json"}, max_bytes=RETIRE_MAX_BYTES,
                                        connect_s=min(self.connect_timeout, RETIRE_TIMEOUT_S))
        finally:
            conn.close()
        if status != 200:
            raise ValueError(f"the guard answered {status}")
        return json.loads(raw)

    def poll_once(self) -> int:
        """Ask about up to RETIRE_BATCH retiring ids; release the confirmed ones. The number released; ValueError,
        OSError or http.client.HTTPException when the poll failed (nothing released)."""
        ids = self.pending()[:RETIRE_BATCH]
        if not ids:
            return 0
        answer = self._call(RETIRE_PATH, {"tickets": ids})
        retired = answer.get("retired") if isinstance(answer, dict) else None
        if not isinstance(retired, list):
            raise ValueError("the guard's answer has no retired list")
        self._seen_boot(answer.get("boot"))
        asked, done = set(ids), []
        with self._cv:
            for rid in retired:
                if isinstance(rid, str) and rid in asked and rid in self._pending:
                    done.append(self._pending.pop(rid))
        for ticket in done:
            ticket.release()
        return len(done)

    def _run(self) -> None:
        wait = self.interval
        while True:
            with self._cv:
                while not self._pending and self._fence is None and not self._stopped:
                    self._cv.wait()
                if self._stopped:
                    return
                fenced = self._fence is not None
            try:
                if fenced and self.fence_once() is not None:
                    raise ValueError(self.fenced() or "fenced")
                self.poll_once()
                self.last_error, wait = None, self.interval
            except Exception as exc:                 # noqa: BLE001 (the thread must outlive any one poll)
                # fail closed: the tickets stay reserved, the node reports them retiring, and the gate is fenced
                if not fenced:
                    if self.last_error is None:
                        logger.warning("retirement poll failed (%s): %d tickets stay reserved", type(exc).__name__,
                                       len(self.pending()))
                    self._raise_fence(f"fenced: lost contact with the backend's guard ({type(exc).__name__})")
                self.last_error, wait = type(exc).__name__, min(RETIRE_BACKOFF_S, wait * 2)
            with self._cv:
                if (self._pending or self._fence is not None) and not self._stopped:
                    self._cv.wait(wait)


class GateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: GateServer
    # The socket timeout for every read and write on the client connection (IDLE_TIMEOUT_S explains why it must
    # exist); the server's idle_timeout overrides it in setup().
    timeout = IDLE_TIMEOUT_S

    def setup(self):
        self.timeout = self.server.idle_timeout
        super().setup()
        # The header deadline (HEADER_DEADLINE_S): http.server reads the request line and headers itself, so a timer
        # shuts the socket if they have not all arrived in time; _handle cancels it once they have.
        self._expired = False
        self._header_timer = threading.Timer(self.server.header_deadline, self._expire)
        self._header_timer.daemon = True
        self._header_timer.start()

    def _expire(self) -> None:
        # Flag first: http.server may already hold a partial header block (the shutdown makes its readline return
        # early), and a request parsed from partial headers must never be forwarded or answered.
        self._expired = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self._header_timer.cancel()
        super().finish()

    def _read_body(self, n: int, deadline_s: float) -> bytes | None:
        """n bytes of body within an absolute deadline (BODY_DEADLINE_S / DRAIN_DEADLINE_S), or None when the
        deadline passes or the client goes away (http_body.read_within: a trickle cannot extend it)."""
        return http_body.read_within(self.connection, self.rfile, n, deadline_s, self.server.idle_timeout)

    def log_message(self, fmt, *args):          # http.server's line has the request line only: no token, no body
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, code: int, kind: str, message: str) -> None:
        self._send(code, json.dumps({"type": "error", "error": {"type": kind, "message": f"local gate: {message}"}}).encode())

    def _refuse(self, code: int, kind: str, message: str) -> None:
        """A refusal of the request itself (local_backend.Refusal: a bad body, another model, a disallowed template
        kwarg, a body nested too deeply): the same body always gets the same answer, so x-should-retry: false."""
        body = json.dumps({"type": "error", "error": {"type": kind, "message": f"local gate: {message}"}}).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("x-should-retry", "false")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authorized(self) -> bool:
        auth = self.headers.get("authorization") or ""
        given = auth[7:].strip() if auth[:7].lower() == "bearer " else (self.headers.get("x-api-key") or "").strip()
        return hmac.compare_digest(given.encode(), self.server.token)

    def _drain(self) -> bool:
        """Read and discard a refused request's body (up to max_body, within DRAIN_DEADLINE_S), so the client receives
        the 401 instead of a reset: closing with unread bytes in the socket makes the kernel send RST, which the client
        sees as EPIPE. False when the drain overran or the client left: the caller closes without the 401. A body over
        the limit, or with no length, is not read: the 401 is sent and the connection closes."""
        try:
            n = int(self.headers.get("content-length") or "0")
        except ValueError:
            return True
        if 0 < n <= self.server.max_body:
            return self._read_body(n, self.server.drain_deadline) is not None
        return True

    def _node_free(self) -> tuple[bool, str]:
        """pro_local_allowed (AC power + local switch; applies on every node running the gate, not only the Pro),
        then the S1 training hold."""
        allowed, reason = pro_local_allowed()
        if not allowed:
            return allowed, reason
        hold = training_hold(self.server.studio_gate_dir)
        return (False, hold) if hold else (True, "")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._handle_one(method)
        except OSError:                            # the client left, reset, stalled or was shut by a deadline
            self.close_connection = True           # nothing to tell it

    def _handle_one(self, method: str) -> None:
        self._header_timer.cancel()              # the headers are in
        self.close_connection = True             # one request per connection: no keep-alive slot holding
        if self._expired:                        # the header deadline fired: these headers may be partial
            return
        if not self._authorized():               # before the path or body is looked at
            if self._drain():
                self._send(401, _UNAUTHORIZED)
            return
        path = self.path.split("?", 1)[0]
        if path == SWITCH_PATH:                  # the gate's own endpoint: no admission check (see docstring)
            return self._switch(method)
        if (method, path) not in ROUTES:
            return self._error(404, "not_found_error", "not a forwarded path")
        allowed, reason = self._node_free()
        if not allowed:
            if self._drain():
                self._error(503, "overloaded_error", reason)
            return
        body = raw = plan = None
        if method == "POST":
            try:
                n = int(self.headers.get("content-length") or "-1")
            except ValueError:
                n = -1
            if n < 0:
                return self._error(411, "invalid_request_error", "content-length required")
            if n > self.server.max_body:
                return self._error(413, "request_too_large", f"body over {self.server.max_body} bytes")
            raw = self._read_body(n, self.server.body_deadline)
            if raw is None:                      # too slow or gone: close, freeing the slot
                return
            try:
                body = local_backend.adapt_messages_body(self.server.backend, raw)
                # Refused before any admission (adapt_messages_body checked the body parses): on a text-only model
                # anything but text; on a vision model (#125) anything outside its still-image policy.
                why, plan = self._content(json.loads(body))
            except local_backend.Refusal as exc:
                return self._refuse(exc.status, exc.kind, str(exc))
            except RecursionError:                 # the adapted body re-parsed deeper than the original
                return self._refuse(400, "invalid_request_error", local_backend.TOO_DEEP)
            if why is not None:
                return self._unsupported(why)
        # Admission: hold LOCK_SH on the node lock from here until the response is fully relayed. A trainer holds
        # LOCK_EX for its whole run (studio_capacity.training_gate), so the kernel guarantees no request reaches
        # Ollama while training runs, and the trainer cannot start while this request is in flight.
        lock_fd, ticket, ticket_id = None, None, None
        self._upstream_sent = False
        if self.server.studio_gate_dir is not None:
            try:
                # create=False: a request never recreates a missing/renamed-away gate dir (studio_capacity.open_node_lock);
                # the gate creates it once at startup (main) and the trainer creates it on its first run.
                lock_fd = open_node_lock(self.server.studio_gate_dir, create=False)
            except OSError as exc:
                logger.warning("studio-gate node lock unopenable (%s): refusing local request", type(exc).__name__)
                return self._error(503, "overloaded_error", "S1 training state unavailable; local model refused")
        try:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return self._error(503, "overloaded_error",
                                       "S1 training in progress on this node; local model refused until it ends")
                try:
                    verify_node_lock(self.server.studio_gate_dir, lock_fd)
                except OSError as exc:
                    logger.warning("studio-gate node lock moved while locking (%s): refusing local request",
                                   type(exc).__name__)
                    return self._error(503, "overloaded_error", "S1 training state unavailable; local model refused")
            allowed, reason = self._node_free()
            if not allowed:
                return self._error(503, "overloaded_error", reason)
            if method == "POST" and self.server.retirer is not None:
                fence = self.server.retirer.lift_now()
                if fence is not None:        # the fence (FENCE_PATH): nothing admitted it cannot account for
                    return self._busy({**self.server.admission.snapshot(), "reason": fence})
            if method == "POST":             # admission: only a model turn takes a slot, never the probe
                adm = self.server.admission
                images = len(plan) if plan is not None else 0
                # The bound comes from the body as the client sent it (raw), before the gate's own adaptation; the
                # validated image data is left out of the text bound and each image counted as image_tokens_max.
                need, load = 0, None
                if adm.governor is not None:
                    bound = local_governor.input_tokens(raw, adm.context_tokens, plan.data_chars if images else 0)
                    # #126: a text turn on a text-only model joins the text route's set reservation (the decode
                    # batch pads every row to the longest); anything else reserves its own bytes, as before
                    load = adm.text_load(bound, _max_tokens(body)) if not images else None
                    if load is None:
                        need = adm.request_bytes(bound, _max_tokens(body), images)
                # count slot, bytes and (an image turn) the image slot together, or none of them
                ticket, snap = adm.try_acquire(need, image=images > 0, load=load)
                if ticket is None:
                    if snap.get("refused") == "unsupported":
                        return self._unsupported(snap.get("reason") or "the model reads no images")
                    return self._too_large(snap) if snap.get("refused") == "too_large" else self._busy(snap)
                if self.server.retirer is not None and not images:
                    ticket_id = self.server.retirer.mint()   # retirement: the guard reports this id's release
            self._forward(method, path, body, ticket_id)
        finally:
            if ticket is not None:
                if ticket_id is not None and self._upstream_sent:
                    # A text turn on a guarded vllm-mlx: its rows may outlive this connection, so the ticket stays
                    # reserved (retiring) until the guard confirms the request left the scheduler (RETIRE_PATH).
                    self.server.retirer.hand_over(ticket, ticket_id)
                else:
                    # However the turn ended (relayed, an upstream error or timeout, the client gone, an exception on
                    # the way out): its slot and bytes are free. Nothing was sent upstream for a retiring turn whose
                    # connect failed. Ticket.release is idempotent.
                    if ticket_id is not None:
                        self.server.retirer.forget(ticket_id)
                    ticket.release()
            if lock_fd is not None:
                os.close(lock_fd)            # releases LOCK_SH

    def _busy(self, snap: dict) -> None:
        """The over-limit answer: 529 overloaded_error (Anthropic's "busy, retry"), Retry-After, and the capacity report
        so the provider marks the node busy without another probe. Nothing reached the backend."""
        cap = self.server.report(snap)
        body = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message":
                           f"{BUSY_MESSAGE} ({cap['inflight']} of {cap['limit']} running: {cap['reason']})"},
                           GATE_KEY: cap}).encode()
        self.send_response(529, "Overloaded")
        self.send_header("content-type", "application/json")
        self.send_header("retry-after", str(self.server.admission.retry_after_s))
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _too_large(self, snap: dict, guarded: str | None = None) -> None:
        """The never-fits answer: the request's reservation exceeds the whole budget beside the weights, so no retry
        can be admitted. 413 request_too_large, x-should-retry: false, no Retry-After, and the capacity report (its
        fit_tokens says how much would fit). Nothing reached the backend. `guarded`: the backend's prompt guard refused
        the prompt instead (its exact token count + max_tokens is over the context); nothing was scheduled there."""
        cap = self.server.report(snap)
        why = guarded if guarded is not None else \
            f"{cap['reason']}; at most {cap['fit_tokens']} tokens of input + max_tokens fit"
        body = json.dumps({"type": "error", "error": {"type": "request_too_large", "message":
                           f"{TOO_LARGE_MESSAGE} ({why})"}, GATE_KEY: cap}).encode()
        self.send_response(413)
        self.send_header("content-type", "application/json")
        self.send_header("x-should-retry", "false")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _content(self, body: dict):
        """(why the node's model cannot take this turn's content or None, the ImagePlan or None): an Ollama node is
        not checked; a vllm-mlx node serving a text-only model takes text only (non_text_content); one serving a
        vision model takes still images within its policy (image_policy, #125)."""
        if self.server.backend["backend"] != "vllm-mlx":
            return None, None
        vision = self.server.admission.vision
        if vision is None:
            why = local_governor.non_text_content(body)
            return (None if why is None else f"{why}; it serves a text-only model"), None
        why, plan = local_governor.image_policy(body, vision)
        return (None if why is None else f"{why}; it reads still images only, within its image policy"), plan

    def _unsupported(self, why: str) -> None:
        """The content refusal (UNSUPPORTED_MESSAGE): 400 invalid_request_error, x-should-retry: false, no
        Retry-After. Nothing was admitted or sent to the backend; the provider routes the turn elsewhere."""
        body = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                           f"{UNSUPPORTED_MESSAGE} ({why})"}}).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("x-should-retry", "false")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _switch(self, method: str) -> None:
        """GET / POST SWITCH_PATH (authorized by the caller). One implementation of the switch: local_resources."""
        if method == "POST":
            try:
                n = int(self.headers.get("content-length") or "-1")
            except ValueError:
                n = -1
            if n < 0:
                return self._error(411, "invalid_request_error", "content-length required")
            if n > SWITCH_MAX_BODY:
                return self._error(413, "request_too_large", f"a switch body is at most {SWITCH_MAX_BODY} bytes")
            raw = self._read_body(n, self.server.body_deadline)
            if raw is None:
                return
            try:
                body = json.loads(raw or b"null")
            except ValueError:
                body = None
            on = body.get("on") if isinstance(body, dict) else None
            if not isinstance(on, bool):
                return self._error(400, "invalid_request_error", 'body must be {"on": true} or {"on": false}')
            try:
                local_resources.set_local_switch(on)
            except OSError as exc:
                logger.warning("local switch change failed: %s", type(exc).__name__)
                return self._error(500, "api_error", f"cannot change the local switch ({type(exc).__name__})")
            logger.info("local switch set %s", "on" if on else "off")
        elif method != "GET":
            return self._error(405, "invalid_request_error", "the switch takes GET or POST")
        self._send(200, json.dumps(self.switch_status()).encode())

    def switch_status(self) -> dict:
        """The switch status: local_resources.local_switch_status, the S1 training hold (a yes/no, no detail), and the
        models Ollama has loaded."""
        out = local_resources.local_switch_status()
        out["training"] = training_hold(self.server.studio_gate_dir) is not None
        # "ollama" is the backend's reachability (the key predates the vllm-mlx backend; `backend` names which).
        out["models_loaded"], out["ollama"] = self._loaded_models()
        out["backend"] = self.server.backend["backend"]
        out["capacity"] = self.server.capacity()
        return out

    def _loaded_models(self) -> tuple[list[str] | None, str]:
        """(names, "up") from the backend on loopback (Ollama's GET /api/ps; vllm-mlx's GET /v1/models, its one
        resident model), or (None, "unreachable" | "unreadable"). Names only: sizes, digests and expiry times stay on
        the node."""
        backend = self.server.backend["backend"]
        conn = http.client.HTTPConnection(self.server.upstream_host, self.server.upstream_port)
        try:
            try:
                status, raw = deadline_call(conn, PS_TIMEOUT_S, "GET", local_backend.LOADED[backend],
                                            max_bytes=PS_MAX_BYTES,
                                            connect_s=min(self.server.connect_timeout, PS_CONNECT_S))
            except (OSError, http.client.HTTPException):
                return None, "unreachable"
            data = (local_backend.models_to_tags(raw) if backend == "vllm-mlx" else None) if status == 200 else None
            if data is None and status == 200 and backend == "ollama":
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
            models = data.get("models") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return None, "unreadable"
            names = [m.get("name") or m.get("model") for m in models if isinstance(m, dict)]
            names = [n for n in names if isinstance(n, str) and 0 < len(n) <= 200 and n.isprintable()]
            return names[:PS_MAX_MODELS], "up"
        finally:
            conn.close()

    def _forward(self, method: str, path: str, body: bytes | None, ticket_id: str | None = None) -> None:
        backend = self.server.backend["backend"]
        name = "Ollama" if backend == "ollama" else backend
        up_method, up_path = local_backend.UPSTREAM[backend][(method, path)]
        conn = http.client.HTTPConnection(self.server.upstream_host, self.server.upstream_port,
                                          timeout=self.server.connect_timeout)
        headers = {"content-type": self.headers.get("content-type") or "application/json"}
        if ticket_id is not None:
            headers[TICKET_HEADER] = ticket_id
        try:
            try:
                conn.connect()
                # connected: from here bytes may reach the backend, so a retiring ticket waits for its confirmation
                self._upstream_sent = True
                conn.sock.settimeout(self.server.read_timeout)
                conn.request(up_method, up_path, body=body, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                return self._error(502, "api_error", f"{name} unreachable on loopback ({type(exc).__name__})")
            ctype = resp.getheader("content-type") or "application/json"
            if "text/event-stream" not in ctype:
                try:
                    data = resp.read()
                except (OSError, http.client.HTTPException) as exc:
                    return self._error(502, "api_error", f"{name} response unreadable ({type(exc).__name__})")
                if path == "/api/tags" and resp.status == 200:
                    return self._tags(data, backend, ctype)
                if resp.status != 200 and backend != "ollama":
                    guarded = local_backend.guard_refusal(resp.status, data)
                    if guarded is not None:          # the backend's prompt guard: never fits, like the gate's own 413
                        return self._too_large(self.server.admission.snapshot(), guarded)
                    return self._send(resp.status, local_backend.anthropic_error(resp.status, data, backend))
                return self._send(resp.status, data, ctype)
            self.send_response(resp.status)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-cache")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:                          # line by line, flushed: SSE reaches the provider unbuffered
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass                                 # either side left; the provider reports an incomplete stream
        finally:
            conn.close()


    def _tags(self, data: bytes, backend: str, ctype: str) -> None:
        """A successful GET /api/tags: the backend's model list in Ollama's shape, with the capacity report added under
        GATE_KEY (Ollama clients ignore unknown keys). An answer that is not a JSON object is relayed unchanged."""
        if backend == "vllm-mlx":
            tags = local_backend.models_to_tags(data)
            if tags is None:
                return self._error(502, "api_error", f"{backend} answered /v1/models with an unreadable list")
        else:
            try:
                tags = json.loads(data)
            except ValueError:
                tags = None
            if not isinstance(tags, dict):
                return self._send(200, data, ctype)
        tags[GATE_KEY] = self.server.capacity()
        return self._send(200, json.dumps(tags).encode())


def _max_tokens(body: bytes | None):
    """The request's max_tokens for its reservation, or None (the governor then assumes default_max_tokens)."""
    try:
        data = json.loads(body) if body else None
    except (ValueError, RecursionError):
        return None
    return data.get("max_tokens") if isinstance(data, dict) else None


def _settings(argv=None) -> argparse.Namespace:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.local_gate", description=__doc__.split("\n")[0])
    ap.add_argument("--listen", default=env("CORTADOS_LOCAL_GATE_LISTEN", ""),
                    help="address to listen on: the node's Tailscale IP (never committed)")
    ap.add_argument("--port", type=int, default=int(env("CORTADOS_LOCAL_GATE_PORT") or DEFAULT_PORT))
    ap.add_argument("--upstream", default=env("CORTADOS_LOCAL_GATE_UPSTREAM") or DEFAULT_UPSTREAM)
    ap.add_argument("--token-file", default=env("CORTADOS_LOCAL_GATE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    ap.add_argument("--studio-gate-dir", default=env("CORTADOS_LOCAL_GATE_STUDIO_GATE_DIR") or str(default_studio_gate_dir()),
                    help="S1 training handshake dir; a live intent there refuses requests with 503 "
                         "(default $CORTADOS_HOME/studio-gate)")
    ap.add_argument("--backend-config", default=env(local_backend.CONFIG_ENV) or None,
                    help="the node's backend config (ollama | vllm-mlx, governor); default "
                         f"{local_backend.DEFAULT_CONFIG} when it exists, else Ollama with no governor")
    ap.add_argument("--allow-any", action="store_true", help="permit 0.0.0.0 / :: (only on a trusted-only machine)")
    ap.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    ap.add_argument("--max-connections", type=int,
                    default=env("CORTADOS_LOCAL_GATE_MAX_CONNECTIONS") or MAX_CONNECTIONS,   # argparse applies int to an env string
                    help="concurrent connections; 0 (default) is unlimited: Ollama's OLLAMA_NUM_PARALLEL / "
                         "OLLAMA_MAX_QUEUE are the real limits")
    ap.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT_S)
    ap.add_argument("--read-timeout", type=float, default=READ_TIMEOUT_S)
    ap.add_argument("--print-config", action="store_true", help="check the settings and token file, print them, exit")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = _settings(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        listen = check_listen(a.listen, a.allow_any)
        check_max_connections(a.max_connections)
        check_limits(max_body_bytes=a.max_body_bytes,
                     connect_timeout=a.connect_timeout, read_timeout=a.read_timeout)
        token = read_token(a.token_file)
        try:
            backend = local_backend.load(a.backend_config, explicit=a.backend_config is not None)
        except local_backend.BackendConfigError as exc:
            raise GateConfigError(str(exc)) from None
    except GateConfigError as exc:
        print(f"local_gate: {exc}", file=sys.stderr)
        return 2
    upstream = backend["upstream"] or a.upstream
    read_timeout = backend["read_timeout_s"] or a.read_timeout       # the backend config's, when it sets one
    governor = local_governor.Governor(backend["governor"]) if backend["governor"] else None
    if a.print_config:
        print(json.dumps({"listen": f"{listen}:{a.port}", "upstream": upstream, "token_file": a.token_file,
                          "backend": backend["backend"], "backend_config": backend["path"],
                          "served_model": backend["served_model"], "governor": backend["governor"],
                          "token": f"ok ({len(token)} chars, mode 0600)", "routes": sorted(f"{m} {p}" for m, p in ROUTES),
                          "switch_endpoint": f"GET/POST {SWITCH_PATH}",
                          "max_body_bytes": a.max_body_bytes, "max_connections": a.max_connections or "unlimited",
                          "connect_timeout_s": a.connect_timeout, "read_timeout_s": read_timeout,
                          "studio_gate_dir": a.studio_gate_dir}, indent=1))
        return 0
    try:
        srv = GateServer((listen, a.port), token=token, upstream=upstream, max_body=a.max_body_bytes,
                         max_connections=a.max_connections, connect_timeout=a.connect_timeout,
                         read_timeout=read_timeout, studio_gate_dir=a.studio_gate_dir, backend=backend,
                         admission=local_governor.Admission(governor))
    except (GateConfigError, OSError) as exc:
        print(f"local_gate: cannot start: {exc}", file=sys.stderr)
        return 2
    if a.studio_gate_dir:
        # Create/validate the gate dir and lock once here: requests open them with create=False.
        try:
            os.close(open_node_lock(a.studio_gate_dir))
        except OSError as exc:
            srv.server_close()
            print(f"local_gate: studio-gate node lock unusable: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    logger.info("local gate on %s:%s -> %s %s (%d forwarded routes, governor %s)", listen, a.port, backend["backend"],
                upstream, len(ROUTES), "on" if governor else "off")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
