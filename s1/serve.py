"""CortadOS Router S1 scorer service: the promoted adapter (s1/current.json) loaded once, answering POST /score in ~1 s on this Mac.
  POST /score {"request": str, "unit_kind": "interactive"|"background"} → {"answers", "probs", "confidence", "ms", "model"}
  GET  /health → {"ok", "model", "loaded_s"}
Reloads when current.json changes (a promotion). Never more than one request at a time (MLX is not thread-safe); the router's own timeout guards the turn.
Binds 127.0.0.1:8431 by default. Set CORTADOS_S1_BIND to a tailnet IP (or 0.0.0.0) to let other hosts reach it, and CORTADOS_S1_PORT for the port; clients resolve the matching URL through cortados_router.s1_endpoint."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))                 # repo root, so `import cortados_router` works from any launch cwd
os.environ.setdefault("HF_HUB_OFFLINE", "1")
from student import Scorer  # noqa: E402

CURRENT = HERE / "current.json"
_state: dict = {"scorer": None, "mtime": 0.0, "model": None, "loaded_s": None}

MAX_RSS_GB = float(os.environ.get("CORTADOS_S1_MAX_RSS_GB", "6"))
# Guards that matter once CORTADOS_S1_BIND exposes the port beyond loopback: one MLX worker serves everything, so an
# unbounded body or backlog from any reachable host is a resource-exhaustion lever. These bound it; the tailnet (or a
# token) is the access boundary.
MAX_BODY_BYTES = int(os.environ.get("CORTADOS_S1_MAX_BODY_BYTES", str(256 * 1024)))  # a score request is a few KB; refuse larger reads outright
MAX_PENDING = int(os.environ.get("CORTADOS_S1_MAX_PENDING", "32"))                   # cap the backlog: the bounded queue below refuses a flood (503)
REPLY_TIMEOUT_S = float(os.environ.get("CORTADOS_S1_REPLY_TIMEOUT_S", "60"))         # hard ceiling on the client wait; the client's own timeout usually shortens it
TOKEN = os.environ.get("CORTADOS_S1_TOKEN") or None                                  # when set, POST /score needs Authorization: Bearer <token>; loopback default leaves it unset

# Bounded, so a put_nowait that would exceed MAX_PENDING raises queue.Full and the handler returns 503 atomically -- a
# qsize() check before a separate put races under ThreadingHTTPServer and lets concurrent handlers overshoot.
_jobs: "queue.Queue[tuple[dict, queue.Queue, float]]" = queue.Queue(maxsize=MAX_PENDING)


def score_request(body, scorer, *, task_context_contract=None):
    """A promoted scorer must explicitly support the versioned task input."""
    from cortados_router.task_context import CONTRACT, validate_context, context_digest
    task = validate_context(body.get('task_context'))
    options = {}
    if task is not None:
        if task_context_contract != CONTRACT:
            return {'error': 'promoted scorer does not support durable task context'}
        if task['status'] != 'bound':
            return {'error': 'task context unavailable'}
        options['task_context'] = task
    result = scorer.score(str(body.get('request') or '')[:6000],
                          body.get('unit_kind') or 'interactive', **options)
    if task is not None:
        result = {**result, 'task_context_digest': context_digest(task)}
    return result


def admission(auth: str, length: int):
    """Why a POST /score is refused before its body is read, or None to admit it. Pure, so it is unit-tested without a
    socket: token (when set), a sane Content-Length, body-size cap. The backlog cap is enforced at enqueue, atomically."""
    if TOKEN is not None and auth != f"Bearer {TOKEN}":
        return 401, {"error": "unauthorized"}
    if length < 0:                                           # a negative Content-Length would make rfile.read(length) read until EOF, unbounded
        return 400, {"error": "negative Content-Length"}
    if length > MAX_BODY_BYTES:
        return 413, {"error": f"request body {length} exceeds {MAX_BODY_BYTES} bytes"}
    return None


def client_wait(header: str | None) -> float:
    """The server's wait for this request: the caller's own timeout (X-Request-Timeout-S) when sane, capped at
    REPLY_TIMEOUT_S. Propagating it lets the worker drop a job the caller has already abandoned."""
    if not header:
        return REPLY_TIMEOUT_S
    try:
        t = float(header)
    except ValueError:
        return REPLY_TIMEOUT_S
    return max(0.0, min(REPLY_TIMEOUT_S, t))


def enqueue(body: dict, deadline: float):
    """(reply_queue, None) when the job is queued, or (None, refusal) when the bounded backlog is full."""
    reply: queue.Queue = queue.Queue()
    try:
        _jobs.put_nowait((body, reply, deadline))
    except queue.Full:
        return None, (503, {"error": "scorer busy: pending queue full"})
    return reply, None


def _expired(deadline: float) -> bool:
    return deadline is not None and time.time() > deadline


def _rss_gb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30


def worker() -> None:
    """The only thread that touches MLX (its streams are thread-local)."""
    n = 0
    while True:
        body, reply, deadline = _jobs.get()
        if _expired(deadline):                                # the client already gave up; don't spend an MLX pass on a dead request
            reply.put({"error": "dropped: client deadline passed before scoring"})
            continue
        try:
            ensure_loaded()
            if _expired(deadline):                            # loading the scorer can take seconds; re-check right before spending the pass
                reply.put({"error": "dropped: client deadline passed during model load"})
                continue
            reply.put(score_request(body, _state['scorer'], task_context_contract=_state.get('task_context_contract')))
        except Exception as exc:
            reply.put({"error": f"{type(exc).__name__}: {exc}"})
        n += 1
        if n % 20 == 0 and _rss_gb() > MAX_RSS_GB:       # memory watchdog: exit and let launchd (KeepAlive) bring a clean process back
            print(f"rss {_rss_gb():.1f} GB > {MAX_RSS_GB} GB after {n} requests: restarting", flush=True)
            os._exit(3)


def ensure_loaded() -> None:
    m = CURRENT.stat().st_mtime
    if _state["scorer"] is None or m != _state["mtime"]:
        cfg = json.loads(CURRENT.read_text())
        t0 = time.time()
        _state["scorer"] = Scorer(cfg["base"], cfg["adapter"], cfg.get("temperatures"))
        _state.update(mtime=m, model=f"CortadOS Router S1 {cfg.get('promoted')} ({Path(cfg['adapter']).parent.name})", loaded_s=round(time.time() - t0, 1),
                      task_context_contract=cfg.get('task_context_contract'))
        print(f"loaded {_state['model']} in {_state['loaded_s']} s", flush=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": _state["scorer"] is not None, "model": _state["model"], "loaded_s": _state["loaded_s"]})

    def do_POST(self):
        try:
            self.connection.settimeout(REPLY_TIMEOUT_S)       # bound a stalled upload so it cannot hold a handler thread forever
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send(400, {"error": "bad Content-Length"}); return
            refusal = admission(self.headers.get("Authorization", ""), length)
            if refusal is not None:                           # token / bad length / oversized body: refuse before reading
                self._send(*refusal); return
            body = json.loads(self.rfile.read(length) or b"{}")   # length is now 0..MAX_BODY_BYTES
            wait = client_wait(self.headers.get("X-Request-Timeout-S"))
            reply, full = enqueue(body, time.time() + wait)
            if full is not None:                              # bounded backlog full: shed load
                self._send(*full); return
            r = reply.get(timeout=max(wait, 1.0))
            self._send(500 if "error" in r else 200, {**r, "model": _state["model"]})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


H.timeout = REPLY_TIMEOUT_S   # socketserver arms this on the connection at setup, before the request line/headers are read, so a stalled peer is dropped
MAX_CONNS = int(os.environ.get("CORTADOS_S1_MAX_CONNS", str(max(2 * MAX_PENDING, 64))))  # cap live connections: a slow-header flood can't spawn unbounded threads


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer spawns a thread per connection with no cap, and BaseHTTPRequestHandler reads the request
    line and headers before do_POST runs -- so a peer that withholds headers would stall a thread past every in-handler
    guard. A per-connection semaphore caps concurrency (excess connections are closed, not threaded), and H.timeout
    (set below) arms a socket timeout at setup so an incomplete request line or headers is dropped, not held."""
    daemon_threads = True

    def __init__(self, *a, max_conns: int, **k):
        self._conns = threading.BoundedSemaphore(max_conns)
        super().__init__(*a, **k)

    def _admit_connection(self) -> bool:
        return self._conns.acquire(blocking=False)

    def _release_connection(self) -> None:
        try:
            self._conns.release()
        except ValueError:
            pass

    def process_request(self, request, client_address):
        if not self._admit_connection():                        # at capacity: close the socket now, spawn no handler
            self.close_request(request)
            return
        super().process_request(request, client_address)       # admitted: the handler thread's shutdown_request releases

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._release_connection()


if __name__ == "__main__":
    from cortados_router.s1_endpoint import bind_address
    host, port = bind_address()                                 # 127.0.0.1 unless CORTADOS_S1_BIND widens it
    threading.Thread(target=worker, daemon=True, name="mlx").start()
    _jobs.put(({"request": "warm up"}, queue.Queue(), None))     # load + first pass on the worker thread (no deadline)
    if host not in ("127.0.0.1", "localhost", "::1"):           # a non-loopback bind is a deliberate network exposure: say what guards it
        guard = "token required" if TOKEN else "no token set (rely on the tailnet as the access boundary, or set CORTADOS_S1_TOKEN)"
        print(f"CortadOS Router S1 scorer EXPOSED on {host}:{port} ({guard}; body<={MAX_BODY_BYTES}B, backlog<={MAX_PENDING}, conns<={MAX_CONNS})", flush=True)
    else:
        print(f"CortadOS Router S1 scorer on {host}:{port}", flush=True)
    BoundedServer((host, port), H, max_conns=MAX_CONNS).serve_forever()
