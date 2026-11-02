"""Token gate in front of a node's Ollama: a small threaded reverse proxy run ON the node (the Pro).

Ollama has no authentication, so it listens on 127.0.0.1 only and this gate is the one door to it from the tailnet:

  * Only the two paths the provider's local lane uses are forwarded: POST /v1/messages and GET /api/tags. Everything
    else (model pulls, deletes, /api/generate, …) is a 404: a leaked token must not be able to manage the node.
  * Every request carries `Authorization: Bearer <token>` or `x-api-key: <token>`, compared in constant time
    (hmac.compare_digest) against a token read from a file that only its owner can read. The check runs before the
    body is read, and a refusal is a fixed 401 that echoes nothing back.
  * The token and request bodies are never logged; a log line is method, path and status.

Run:   python3 -m cortados_router.local_gate --listen <tailscale-ip>
Check: python3 -m cortados_router.local_gate --listen <tailscale-ip> --print-config
Env equivalents: CORTADOS_LOCAL_GATE_LISTEN, CORTADOS_LOCAL_GATE_PORT, CORTADOS_LOCAL_GATE_UPSTREAM,
CORTADOS_LOCAL_GATE_TOKEN_FILE. Stdlib only.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import http.client
import json
import logging
import math
import os
import socket
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
# The node runs one model and Ollama queues requests anyway; more parallel connections than this only add threads
# a sleeping-then-woken laptop has to carry. A connection over the limit gets an immediate 503.
MAX_CONNECTIONS = 8
# Loopback connects in microseconds: a short connect wait tells "Ollama is not running" quickly. Reads wait long
# because a first model load (16 GB) can take a minute before the first byte, but are bounded so a hung Ollama never
# pins a gate thread forever.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 300.0
# A connection holds one of the MAX_CONNECTIONS slots from accept, before it has sent anything. Without a socket
# timeout, a few idle sockets (or Tailscale links dropped mid-request, which never send a FIN) would hold every slot
# forever and lock the lane out. 30 s is far more than a provider needs to send its request line and body.
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
# The only routes the provider's local lane uses (local_lane.probe and local_lane.serve).
ROUTES = {("GET", "/api/tags"), ("POST", "/v1/messages")}

_UNAUTHORIZED = json.dumps({"type": "error", "error": {"type": "authentication_error",
                                                        "message": "local gate: bad or missing token"}}).encode()


class GateConfigError(ValueError):
    pass


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


def check_limits(**limits: float) -> None:
    """Every timeout, deadline and size limit must be a finite positive number: 0 makes a socket non-blocking,
    a negative one raises mid-request, NaN compares false everywhere and inf is no bound at all."""
    for name, v in limits.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GateConfigError(f"{name} must be a finite positive number, got {v!r}")


class GateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, *, token: str, upstream: str, max_body: int = MAX_BODY_BYTES,
                 max_connections: int = MAX_CONNECTIONS, connect_timeout: float = CONNECT_TIMEOUT_S,
                 read_timeout: float = READ_TIMEOUT_S, idle_timeout: float = IDLE_TIMEOUT_S,
                 header_deadline: float = HEADER_DEADLINE_S, body_deadline: float = BODY_DEADLINE_S,
                 drain_deadline: float = DRAIN_DEADLINE_S):
        check_limits(max_body=max_body, max_connections=max_connections, connect_timeout=connect_timeout,
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
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(addr, GateHandler)

    def process_request(self, request, client_address):
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
            self.slots.release()


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
        deadline passes or the client goes away. Each wait is bounded by the time left, so a trickle cannot extend it."""
        end = time.monotonic() + deadline_s
        parts, left = [], n
        try:
            while left > 0:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                self.connection.settimeout(min(self.server.idle_timeout, remaining))
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    return None
                parts.append(chunk)
                left -= len(chunk)
        except OSError:
            return None
        finally:
            try:
                self.connection.settimeout(self.server.idle_timeout)
            except OSError:
                pass
        return b"".join(parts)

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
        if (method, path) not in ROUTES:
            return self._error(404, "not_found_error", "not a forwarded path")
        body = None
        if method == "POST":
            try:
                n = int(self.headers.get("content-length") or "-1")
            except ValueError:
                n = -1
            if n < 0:
                return self._error(411, "invalid_request_error", "content-length required")
            if n > self.server.max_body:
                return self._error(413, "request_too_large", f"body over {self.server.max_body} bytes")
            body = self._read_body(n, self.server.body_deadline)
            if body is None:                     # too slow or gone: close, freeing the slot
                return
        conn = http.client.HTTPConnection(self.server.upstream_host, self.server.upstream_port,
                                          timeout=self.server.connect_timeout)
        try:
            try:
                conn.connect()
                conn.sock.settimeout(self.server.read_timeout)
                conn.request(method, path, body=body,
                             headers={"content-type": self.headers.get("content-type") or "application/json"})
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as exc:
                return self._error(502, "api_error", f"Ollama unreachable on loopback ({type(exc).__name__})")
            ctype = resp.getheader("content-type") or "application/json"
            if "text/event-stream" not in ctype:
                try:
                    data = resp.read()
                except (OSError, http.client.HTTPException) as exc:
                    return self._error(502, "api_error", f"Ollama response unreadable ({type(exc).__name__})")
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


def _settings(argv=None) -> argparse.Namespace:
    env = os.environ.get
    ap = argparse.ArgumentParser(prog="python3 -m cortados_router.local_gate", description=__doc__.split("\n")[0])
    ap.add_argument("--listen", default=env("CORTADOS_LOCAL_GATE_LISTEN", ""),
                    help="address to listen on: the node's Tailscale IP (never committed)")
    ap.add_argument("--port", type=int, default=int(env("CORTADOS_LOCAL_GATE_PORT") or DEFAULT_PORT))
    ap.add_argument("--upstream", default=env("CORTADOS_LOCAL_GATE_UPSTREAM") or DEFAULT_UPSTREAM)
    ap.add_argument("--token-file", default=env("CORTADOS_LOCAL_GATE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    ap.add_argument("--allow-any", action="store_true", help="permit 0.0.0.0 / :: (only on a trusted-only machine)")
    ap.add_argument("--max-body-bytes", type=int, default=MAX_BODY_BYTES)
    ap.add_argument("--max-connections", type=int, default=MAX_CONNECTIONS)
    ap.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT_S)
    ap.add_argument("--read-timeout", type=float, default=READ_TIMEOUT_S)
    ap.add_argument("--print-config", action="store_true", help="check the settings and token file, print them, exit")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = _settings(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        listen = check_listen(a.listen, a.allow_any)
        check_limits(max_body_bytes=a.max_body_bytes, max_connections=a.max_connections,
                     connect_timeout=a.connect_timeout, read_timeout=a.read_timeout)
        token = read_token(a.token_file)
    except GateConfigError as exc:
        print(f"local_gate: {exc}", file=sys.stderr)
        return 2
    if a.print_config:
        print(json.dumps({"listen": f"{listen}:{a.port}", "upstream": a.upstream, "token_file": a.token_file,
                          "token": f"ok ({len(token)} chars, mode 0600)", "routes": sorted(f"{m} {p}" for m, p in ROUTES),
                          "max_body_bytes": a.max_body_bytes, "max_connections": a.max_connections,
                          "connect_timeout_s": a.connect_timeout, "read_timeout_s": a.read_timeout}, indent=1))
        return 0
    try:
        srv = GateServer((listen, a.port), token=token, upstream=a.upstream, max_body=a.max_body_bytes,
                         max_connections=a.max_connections, connect_timeout=a.connect_timeout,
                         read_timeout=a.read_timeout)
    except (GateConfigError, OSError) as exc:
        print(f"local_gate: cannot start: {exc}", file=sys.stderr)
        return 2
    logger.info("local gate on %s:%s -> %s (%d forwarded routes)", listen, a.port, a.upstream, len(ROUTES))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
