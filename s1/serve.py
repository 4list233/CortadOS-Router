"""CortadOS Router S1 scorer service: the promoted adapter (s1/current.json) loaded once, answering POST /score in ~1 s on this Mac.
  POST /score {"request": str, "unit_kind": "interactive"|"background"} → {"answers", "probs", "confidence", "ms", "model"}
  GET  /health → {"ok", "model", "loaded_s"}
Reloads when current.json changes (a promotion). Never more than one request at a time (MLX is not thread-safe); the router's own timeout guards the turn.
A job whose remaining deadline is shorter than the expected service time (recent service times, floored) is answered at
once with 503 {"busy": true} instead of being scored for a caller that will be gone; the router treats that as degraded.
One metadata-only line per request goes to the request log (request_log_line; never the request text).
Binds 127.0.0.1:8431 by default. Set CORTADOS_S1_BIND to a tailnet IP (or 0.0.0.0) to let other hosts reach it, and CORTADOS_S1_PORT for the port; clients resolve the matching URL through cortados_router.s1_endpoint."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import queue
from collections import deque
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
RECV_DEADLINE_S = float(os.environ.get("CORTADOS_S1_RECV_DEADLINE_S", "10"))         # total time allowed to receive the whole request (line + headers + body); a trickle past this is cut
SERVICE_FLOOR_S = float(os.environ.get("CORTADOS_S1_SERVICE_FLOOR_S", "0.3"))       # the expected service time never drops below this (no samples yet, or a run of tiny prompts)
SERVICE_WINDOW = int(os.environ.get("CORTADOS_S1_SERVICE_WINDOW", "20"))            # how many recent service times the expectation averages
REQUEST_LOG = os.environ.get("CORTADOS_S1_REQUEST_LOG") or None                     # per-request log file; unset: next to stdout's log file when stdout is one, else stdout
REQUEST_LOG_BYTES = int(os.environ.get("CORTADOS_S1_REQUEST_LOG_BYTES", str(5 * 1024 * 1024)))
REQUEST_LOG_BACKUPS = int(os.environ.get("CORTADOS_S1_REQUEST_LOG_BACKUPS", "3"))  # bounded: at most (1 + backups) x bytes on disk
TOKEN = os.environ.get("CORTADOS_S1_TOKEN") or None                                  # when set, POST /score needs Authorization: Bearer <token>; loopback default leaves it unset

# Bounded, so a put_nowait that would exceed MAX_PENDING raises queue.Full and the handler returns 503 atomically -- a
# qsize() check before a separate put races under ThreadingHTTPServer and lets concurrent handlers overshoot.
_jobs: "queue.Queue[tuple[dict, queue.Queue, float | None, float]]" = queue.Queue(maxsize=MAX_PENDING)


class ServiceTimes:
    """The expected time one score takes: the mean of the last `window` service times (the scoring pass only, not a
    model load), never below `floor`. Written by the one worker, read by handler threads: guarded by a lock."""

    def __init__(self, window: int = SERVICE_WINDOW, floor: float = SERVICE_FLOOR_S):
        self._times: deque = deque(maxlen=max(1, window))
        self._floor = floor
        self._lock = threading.Lock()
        self.in_service = 0                               # 1 while the worker is scoring a job, else 0

    def add(self, seconds: float) -> None:
        with self._lock:
            self._times.append(max(0.0, seconds))

    def expected(self) -> float:
        with self._lock:
            mean = sum(self._times) / len(self._times) if self._times else 0.0
        return max(self._floor, mean)


_service = ServiceTimes()
BUSY = {"error": "busy: the scorer cannot answer before the caller's deadline", "busy": True}


def too_late(deadline: float | None, ahead: int, expected: float, now: float | None = None) -> bool:
    """True when a job with `ahead` jobs before it cannot finish before its deadline: remaining < (ahead + 1) x the
    expected service time. A job without a deadline (the warm-up) is never too late."""
    if deadline is None:
        return False
    remaining = deadline - (time.time() if now is None else now)
    return remaining < (ahead + 1) * expected


def request_log_line(*, outcome: str, code: int, queue_wait_s, service_s, depth: int, body_bytes: int,
                     prompt_tokens=None, prefix_tokens=None, expected_s=None) -> str:
    """One request as metadata only: every field is a number or a fixed outcome word, so no request text can reach the
    log whatever the body held. None prints as '-'."""
    if outcome not in ("ok", "timeout", "dropped", "busy", "error", "refused"):
        outcome = "error"

    def ms(x):
        return "-" if x is None else str(int(round(float(x) * 1000)))

    def num(x):
        return "-" if x is None else str(int(x))

    return (f"s1-request ts={time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}Z outcome={outcome} code={int(code)}"
            f" queue_wait_ms={ms(queue_wait_s)} service_ms={ms(service_s)} depth={num(depth)}"
            f" bytes={num(body_bytes)} tokens={num(prompt_tokens)} prefix_tokens={num(prefix_tokens)}"
            f" expected_ms={ms(expected_s)}")


_request_log = logging.getLogger("cortados.s1.requests")
_request_log.propagate = False                            # never into the root logger (or a test runner's handlers)
_request_log.addHandler(logging.NullHandler())            # silent until setup_request_log() runs in the server process
_request_log.setLevel(logging.INFO)


def _stdout_log_dir() -> Path | None:
    """The directory of the file stdout writes to (launchd's StandardOutPath, the scorer's existing log), if any."""
    try:
        import fcntl
        import stat
        if not stat.S_ISREG(os.fstat(1).st_mode):
            return None
        if hasattr(fcntl, "F_GETPATH"):                   # macOS
            return Path(fcntl.fcntl(1, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()).parent
        return Path(os.readlink("/proc/self/fd/1")).parent  # Linux
    except Exception:
        return None


def setup_request_log(path: str | None = REQUEST_LOG) -> str:
    """Attach the bounded request-log handler: a rotating file (REQUEST_LOG_BYTES x (1 + REQUEST_LOG_BACKUPS)) at
    `path`, else `s1-requests.log` beside stdout's log file, else stdout itself (an interactive run). Returns where."""
    target = Path(path) if path else None
    if target is None:
        d = _stdout_log_dir()
        target = d / "s1-requests.log" if d is not None else None
    if target is None:
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
        where = "stdout"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(str(target), maxBytes=REQUEST_LOG_BYTES,
                                                       backupCount=REQUEST_LOG_BACKUPS)
        where = str(target)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _request_log.addHandler(handler)
    return where


def log_request(**fields) -> None:
    try:
        _request_log.info(request_log_line(**fields))
    except Exception:                                     # the log never fails a request
        pass


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


def enqueue(body: dict, deadline: float | None, *, now: float | None = None):
    """(reply_queue, None) when the job is queued, or (None, refusal) when it is shed: the bounded backlog is full, or
    the jobs ahead of it (queued + in service) and its own pass would outlast its deadline at the expected service
    time, so it is answered busy now rather than after a wait that ends the same way."""
    reply: queue.Queue = queue.Queue()
    ahead = _jobs.qsize() + _service.in_service
    if too_late(deadline, ahead, _service.expected(), now):
        return None, (503, dict(BUSY))
    try:
        _jobs.put_nowait((body, reply, deadline, time.time() if now is None else now))
    except queue.Full:
        return None, (503, {"error": "scorer busy: pending queue full", "busy": True})
    return reply, None


def _expired(deadline: float) -> bool:
    return deadline is not None and time.time() > deadline


def _rss_gb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30


def run_job(job, *, load=None, score=None) -> None:
    """One job on the worker thread. Its reply carries the result and a `_meta` dict (queue wait, service time, token
    counts) that the handler logs and strips. A job that can no longer finish in time is dropped with a busy reply:
    before the pass when the remaining deadline is shorter than the expected service time, and again after a model load."""
    body, reply, deadline, enqueued = job
    load = load or ensure_loaded
    score = score or (lambda b: score_request(b, _state['scorer'], task_context_contract=_state.get('task_context_contract')))
    started = time.time()
    meta = {"queue_wait_s": started - enqueued, "service_s": None, "expected_s": _service.expected()}
    if too_late(deadline, 0, meta["expected_s"], started):   # the caller would be gone before the pass ends: don't spend it
        reply.put({**BUSY, "dropped": True, "_meta": meta})
        return
    _service.in_service = 1
    try:
        load()
        if too_late(deadline, 0, _service.expected()):        # loading the scorer can take seconds; re-check before the pass
            reply.put({**BUSY, "dropped": True, "_meta": meta})
            return
        t0 = time.time()
        try:
            result = score(body)
        finally:
            meta["service_s"] = time.time() - t0
            _service.add(meta["service_s"])
        stats = getattr(_state.get("scorer"), "last_stats", None) or {}
        meta.update(prompt_tokens=stats.get("prompt_tokens"), prefix_tokens=stats.get("prefix_tokens"))
        reply.put({**result, "_meta": meta})
    except Exception as exc:
        reply.put({"error": f"{type(exc).__name__}: {exc}", "_meta": meta})
    finally:
        _service.in_service = 0


def worker() -> None:
    """The only thread that touches MLX (its streams are thread-local)."""
    n = 0
    while True:
        run_job(_jobs.get())
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

    # Absolute deadline covering the whole inbound request: request line, headers AND body. The socket timeout bounds
    # inactivity per recv, which a trickle (a byte every few ms) evades; this timer shuts the connection once the total
    # receive time is up, whether or not bytes keep arriving. Cancelled once the request is fully read (do_GET has no
    # body; do_POST cancels after reading the body), so scoring time is never bounded by it.
    def setup(self):
        super().setup()
        self._recv_timer = threading.Timer(RECV_DEADLINE_S, self._kill_slow_recv)
        self._recv_timer.daemon = True
        self._recv_timer.start()

    def _kill_slow_recv(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)   # unblocks a stalled read; the handler then ends and the slot frees
        except OSError:
            pass

    def _recv_done(self):
        t = getattr(self, "_recv_timer", None)
        if t is not None:
            t.cancel()

    def finish(self):
        self._recv_done()                                # belt-and-braces: never leave a timer armed past the connection
        super().finish()

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._recv_done()
        self._send(200, {"ok": _state["scorer"] is not None, "model": _state["model"], "loaded_s": _state["loaded_s"]})

    def do_POST(self):
        log = None
        try:
            self.connection.settimeout(REPLY_TIMEOUT_S)       # per-recv inactivity bound; the absolute RECV deadline (armed in setup) bounds total receive time
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                log_request(outcome="refused", code=400, queue_wait_s=None, service_s=None, depth=None, body_bytes=None)
                self._send(400, {"error": "bad Content-Length"}); return
            refusal = admission(self.headers.get("Authorization", ""), length)
            if refusal is not None:                           # token / bad length / oversized body: refuse before reading
                log_request(outcome="refused", code=refusal[0], queue_wait_s=None, service_s=None, depth=None,
                            body_bytes=length)
                self._send(*refusal); return
            raw = self.rfile.read(length)                     # length is 0..MAX_BODY_BYTES; the RECV timer cuts a trickled body
            self._recv_done()                                 # whole request received; stop the deadline before scoring
            log = {"body_bytes": len(raw), "depth": _jobs.qsize() + _service.in_service, "queue_wait_s": None,
                   "service_s": None, "expected_s": _service.expected()}
            body = json.loads(raw or b"{}")
            wait = client_wait(self.headers.get("X-Request-Timeout-S"))
            reply, full = enqueue(body, time.time() + wait)
            if full is not None:                              # backlog full, or it could not finish in time: shed load now
                log_request(outcome="busy", code=full[0], **log); log = None
                self._send(*full); return
            try:
                r = reply.get(timeout=max(wait, 1.0))
            except queue.Empty:
                log_request(outcome="timeout", code=504, **log); log = None
                self._send(504, {"error": "timeout: no score before the caller's deadline"}); return
            meta = r.pop("_meta", None) or {}
            log.update({k: meta.get(k) for k in ("queue_wait_s", "service_s", "expected_s", "prompt_tokens", "prefix_tokens")
                        if k in meta})
            code = 503 if r.get("busy") else 500 if "error" in r else 200
            log_request(outcome="dropped" if r.get("dropped") else "ok" if code == 200 else "error", code=code, **log); log = None
            self._send(code, {**r, "model": _state["model"]})
        except Exception as exc:
            if log is not None:                               # e.g. a malformed JSON body: still one line for the request
                log_request(outcome="error", code=500, **log)
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

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)):
            return                                              # client went away or was cut mid-exchange: normal, not a server fault
        super().handle_error(request, client_address)


if __name__ == "__main__":
    from cortados_router.s1_endpoint import bind_address
    host, port = bind_address()                                 # 127.0.0.1 unless CORTADOS_S1_BIND widens it
    threading.Thread(target=worker, daemon=True, name="mlx").start()
    print(f"request log: {setup_request_log()}", flush=True)
    _jobs.put(({"request": "warm up"}, queue.Queue(), None, time.time()))   # load + first pass on the worker thread (no deadline)
    if host not in ("127.0.0.1", "localhost", "::1"):           # a non-loopback bind is a deliberate network exposure: say what guards it
        guard = "token required" if TOKEN else "no token set (rely on the tailnet as the access boundary, or set CORTADOS_S1_TOKEN)"
        print(f"CortadOS Router S1 scorer EXPOSED on {host}:{port} ({guard}; body<={MAX_BODY_BYTES}B, backlog<={MAX_PENDING}, conns<={MAX_CONNS})", flush=True)
    else:
        print(f"CortadOS Router S1 scorer on {host}:{port}", flush=True)
    BoundedServer((host, port), H, max_conns=MAX_CONNS).serve_forever()
