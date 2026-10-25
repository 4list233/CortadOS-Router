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
_jobs: "queue.Queue[tuple[dict, queue.Queue]]" = queue.Queue()


MAX_RSS_GB = float(os.environ.get("CORTADOS_S1_MAX_RSS_GB", "6"))


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


def _rss_gb() -> float:
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**30


def worker() -> None:
    """The only thread that touches MLX (its streams are thread-local)."""
    n = 0
    while True:
        body, reply = _jobs.get()
        try:
            ensure_loaded()
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
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            reply: queue.Queue = queue.Queue()
            _jobs.put((body, reply))
            r = reply.get(timeout=60)
            self._send(500 if "error" in r else 200, {**r, "model": _state["model"]})
        except Exception as exc:
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    from cortados_router.s1_endpoint import bind_address
    host, port = bind_address()                                 # 127.0.0.1 unless CORTADOS_S1_BIND widens it
    threading.Thread(target=worker, daemon=True, name="mlx").start()
    _jobs.put(({"request": "warm up"}, queue.Queue()))          # load + first pass on the worker thread
    if host not in ("127.0.0.1", "localhost", "::1"):           # a non-loopback bind is a deliberate network exposure: say so
        print(f"CortadOS Router S1 scorer EXPOSED on {host}:{port} (reachable by other hosts; token-free, trust the network)", flush=True)
    else:
        print(f"CortadOS Router S1 scorer on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
