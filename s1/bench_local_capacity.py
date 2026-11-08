"""Local-model capacity bench for the Studio: how many concurrent Ollama sessions (OLLAMA_NUM_PARALLEL slots, each with
its own KV cache) fit while the S1 scorer keeps serving within budget and S1 training is NOT running.

For each step N it starts its OWN `ollama serve` on 127.0.0.1:<--port> (never the production :11434 instance), warms
the model, runs N concurrent streaming /api/chat sessions for --rounds rounds, probes S1 /score every --s1-interval s
and samples memory / swap / Ollama residency every second. A step FAILs on any request error, S1 failure, S1 p95 above
baseline p95 + budget, swap growth, or available memory below the reserve; the bench stops at the first FAIL and
recommends the largest PASS N. Measurement only: nothing in the router reads its output.

  python3 -B s1/bench_local_capacity.py --s1-url http://<studio>:8431 [--steps 1,2,4,6,8,10,12,16] [--dry-run]

S1 URL is required (--s1-url or CORTADOS_BENCH_S1_URL); there is no default host. CORTADOS_S1_TOKEN, when set, is sent
as the S1 bearer token. Stdlib only.
"""
import argparse
import datetime
import fcntl
import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `import cortados_router` works from any cwd
from cortados_router import studio_capacity as sc  # noqa: E402

GIB = 2 ** 30
MIB = 2 ** 20
OLLAMA_BIN = "/opt/homebrew/bin/ollama"
PRODUCTION_URL = "http://127.0.0.1:11434"
MODEL = "hf.co/mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF:Q4_K_M"
DEFAULT_STEPS = "1,2,4,6,8,10,12,16"
SAMPLE_SECONDS = 1.0          # 1 s sampling catches a swap / memory dip that a whole-step average would hide
READY_TIMEOUT_S = 60          # ollama serve answers /api/version within seconds; a minute means it is wedged
WARM_TIMEOUT_S = 300          # cold load of ~6.3 GB weights from disk plus KV allocation for N x ctx
SESSION_TIMEOUT_S = 900       # default --request-timeout, ABSOLUTE per session: 16 sessions x 6000-token prefill on an
                              # M1 Max can take minutes before the first byte; beyond 15 min the step has failed anyway
JOIN_SLACK_S = 5              # round join = request deadline + this: covers thread start and the watchdog's shutdown
STOP_POLL_S = 0.1             # how often a wait checks the stop flag: a Ctrl-C is acted on within this
PORT_PROBE_TIMEOUT_S = 1      # loopback connect answers in microseconds; a second means nothing usable is listening
LSOF_TIMEOUT_S = 10           # lsof on one port returns in well under a second; ten means it is wedged
S1_TIMEOUT_S = 30             # far above the ~1 s score time: a reply this late is counted as a failure anyway
STOP_WAIT_S = 15              # SIGTERM grace for ollama serve to release Metal buffers before SIGKILL
S1_BASELINE_PROBES = 10       # enough idle samples for a stable p95 without a long idle wait
S1_PROBE_REQUEST = "Rename the variable tmp to count in utils.py."  # fixed, short: latency, not content, is measured


class Refused(RuntimeError):
    """A precondition failed: the bench does not start (nothing was loaded)."""


class Stopped(Exception):
    """A stop was requested (SIGINT/SIGTERM set the flag); raised by our own code at a safe point, never by a handler."""


class DeadlineExceeded(TimeoutError):
    """A request ran past its ABSOLUTE deadline (a trickling reply resets an inactivity timeout, never this one)."""


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise Stopped()


# ---------- bounded HTTP ----------

def bounded_request(url, payload=None, limit_s=10, headers=None, on_line=None, clock=time.monotonic):
    """One HTTP request with an ABSOLUTE deadline of limit_s seconds. Reason: urllib's timeout bounds each recv, so a
    server that trickles a byte every few seconds keeps a request (and every join waiting on it) alive forever.

    The deadline is enforced twice: a watchdog timer shuts the socket down at the deadline (unblocking a recv in
    progress), and the read loop checks the clock after every read. Plain http only: loopback / tailnet, no proxy.
    Without on_line the whole body is parsed as JSON; with on_line each NDJSON line is passed to it and the first
    non-None return value ends the request and is returned (None if the stream ends first)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "http":
        raise ValueError("bench URLs must be http:// (loopback / tailnet): %s" % url)
    deadline = clock() + limit_s
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=limit_s)
    fired = []
    transport = {}  # the connected socket, saved before getresponse(): for a `Connection: close` reply http.client
                    # clears conn.sock while the response is still being read, so conn.sock cannot be shut down then

    def expire():
        fired.append(True)
        sock = transport.get("sock")
        if sock is None:
            return  # not connected yet: the fired check right after connect ends the request
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError as exc:  # socket closed between the check and the shutdown: the read already ended
            fired.append(exc)

    def check_deadline():
        if fired or clock() >= deadline:
            raise DeadlineExceeded("no complete reply within %.1fs" % limit_s)

    timer = threading.Timer(limit_s, expire)
    timer.daemon = True
    timer.start()
    try:
        body = None if payload is None else json.dumps(payload).encode()
        try:
            conn.connect()
            transport["sock"] = conn.sock
            check_deadline()  # the watchdog fired before the socket was saved: it shut nothing down, so stop here
            conn.request("GET" if body is None else "POST", (u.path or "/") + ("?" + u.query if u.query else ""),
                         body=body, headers={"Content-Type": "application/json", **(headers or {})})
            resp = conn.getresponse()
            check_deadline()
            buf = b""
            while True:
                chunk = resp.read1(65536)
                check_deadline()  # after every read: a late complete message is still a deadline miss
                if not chunk:
                    break
                buf += chunk
                if on_line is not None and resp.status < 400:
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        if line.strip():
                            result = on_line(json.loads(line))
                            if result is not None:
                                check_deadline()
                                return result
        except OSError as exc:
            if fired or clock() >= deadline:  # the watchdog's shutdown surfaces as a read error: report the cause
                raise DeadlineExceeded("no complete reply within %.1fs" % limit_s) from exc
            raise
        check_deadline()
        if resp.status >= 400:
            raise OSError("HTTP %d from %s: %s" % (resp.status, url, buf[:200].decode("utf-8", "replace")))
        if on_line is not None:
            if buf.strip():
                return on_line(json.loads(buf))
            return None
        return json.loads(buf)
    finally:
        timer.cancel()
        conn.close()
        sock = transport.get("sock")
        if sock is not None:
            sock.close()  # conn.close() skips it once a `Connection: close` reply detached it; closing twice is a no-op


def _http_json(url, payload=None, timeout=10, headers=None):
    return bounded_request(url, payload, limit_s=timeout, headers=headers)


def run_bounded(fn, limit_s, stop, clock=time.monotonic):
    """fn() in a daemon thread, joined in STOP_POLL_S slices up to limit_s; raises Stopped on a stop request and
    TimeoutError past the limit (fn's own deadline should end it first). Returns fn's result or re-raises its error."""
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised below in the calling thread
            box["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    deadline = clock() + limit_s
    while t.is_alive():
        check_stop(stop)
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("did not finish within %.1fs" % limit_s)
        t.join(min(STOP_POLL_S, remaining))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def check_production_idle(prod_url=PRODUCTION_URL, http=_http_json):
    """Refused unless the production Ollama has no model loaded. Reason: a second resident copy doubles weight memory
    and the measurement would describe a machine that never exists in service."""
    try:
        models = http(prod_url + "/api/ps").get("models")
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(getattr(exc, "reason", None), ConnectionRefusedError) or isinstance(exc, ConnectionRefusedError):
            return []  # production instance not running (LaunchAgent stopped): nothing is loaded
        raise Refused("cannot read production Ollama /api/ps: %s" % exc) from exc
    if not isinstance(models, list):
        raise Refused("production Ollama /api/ps returned no model list")
    if models:
        raise Refused("production Ollama has a model loaded (%s); unload it or wait for keep_alive"
                      % ", ".join(str(m.get("name") or m.get("model")) for m in models))
    return models


def probe_training_lock(gate):
    """Refused if training holds LOCK_EX: LOCK_SH|LOCK_NB on a separately opened fd, released at once."""
    fd = sc.open_node_lock(gate, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("S1 training holds the node lock") from exc
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def check_no_intents(gate, now=None):
    live = sc.live_intents(gate, time.time() if now is None else now, prune=False)
    if live:
        raise Refused("a live training intent exists (%d); training is about to start" % len(live))


def hold_shared_lock(gate):
    """LOCK_SH for the whole run, so the trainer's LOCK_EX cannot be taken mid-bench. Returns the fd."""
    fd = sc.open_node_lock(gate, create=True)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("S1 training took the node lock") from exc
        sc.verify_node_lock(gate, fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def preconditions(gate, prod_url=PRODUCTION_URL, http=_http_json):
    """All refusals before anything loads; returns the held LOCK_SH fd (caller closes it)."""
    probe_training_lock(gate)
    check_no_intents(gate)
    check_production_idle(prod_url, http)
    fd = hold_shared_lock(gate)
    try:
        check_no_intents(gate)  # re-check under the lock: an intent written between probe and hold still refuses
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---------- bench ollama server ----------

def port_in_use(port):
    """True if something already accepts on 127.0.0.1:port. Reason: /api/version would succeed against that other
    server and the bench would load models into, and measure, a process it does not own."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PORT_PROBE_TIMEOUT_S):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        raise Refused("cannot tell whether bench port %d is free: %s" % (port, exc)) from exc


def listener_pids(port):
    """PIDs listening on TCP port, plus each one's parent PID (so a child of our process also counts as ours)."""
    out = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"], capture_output=True,
                         text=True, timeout=LSOF_TIMEOUT_S)
    if out.returncode not in (0, 1):  # 1 = no match
        raise Refused("lsof failed on port %d: %s" % (port, out.stderr.strip()))
    pids = {int(x) for x in out.stdout.split() if x.strip().isdigit()}
    owners = set(pids)
    for pid in pids:
        ps = subprocess.run(["/bin/ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True,
                            timeout=LSOF_TIMEOUT_S)
        if ps.returncode == 0 and ps.stdout.strip().isdigit():
            owners.add(int(ps.stdout.strip()))
    return pids, owners


class OllamaServer:
    def __init__(self, port, ctx, n, binary=OLLAMA_BIN, popen=subprocess.Popen, http=_http_json, clock=time.monotonic,
                 sleep=time.sleep, in_use=port_in_use, listeners=listener_pids, stop=None):
        self.port = port
        self.in_use, self.listeners, self.stop_req = in_use, listeners, stop
        self.url = "http://127.0.0.1:%d" % port
        self.env = dict(os.environ, OLLAMA_HOST="127.0.0.1:%d" % port, OLLAMA_NUM_PARALLEL=str(n),
                        OLLAMA_CONTEXT_LENGTH=str(ctx), OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_KEEP_ALIVE="1m")
        self.binary, self.popen, self.http, self.clock, self.sleep = binary, popen, http, clock, sleep
        self.proc = None

    def start(self):
        if self.in_use(self.port):
            raise Refused("bench port %d already has a listener; refusing to load into a server we do not own"
                          % self.port)
        self.proc = self.popen([self.binary, "serve"], env=self.env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = self.clock() + READY_TIMEOUT_S
        last = None
        while self.clock() < deadline:
            check_stop(self.stop_req)
            if self.proc.poll() is not None:
                raise RuntimeError("bench ollama serve exited with %s" % self.proc.returncode)
            try:
                self.http(self.url + "/api/version", timeout=2)
            except (urllib.error.URLError, OSError) as exc:
                last = exc
                self.sleep(0.25)
                continue
            self.verify_owner()
            return
        raise RuntimeError("bench ollama serve not ready after %ds: %s" % (READY_TIMEOUT_S, last))

    def verify_owner(self):
        """The answering server must be our child: alive, and the port's listener is its pid (or its child)."""
        if self.proc.poll() is not None:
            raise Refused("bench ollama serve exited (%s) yet port %d answers: another server owns it"
                          % (self.proc.returncode, self.port))
        pids, owners = self.listeners(self.port)
        if self.proc.pid not in owners:
            raise Refused("port %d is served by pid(s) %s, not our ollama serve (pid %d); refusing"
                          % (self.port, sorted(pids) or "none", self.proc.pid))

    def stop(self):
        """SIGTERM, bounded wait, SIGKILL, reap. Called from teardown, which a signal never interrupts (the handler
        only sets a flag), so this always completes before the caller releases the node lock."""
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            print("bench: ollama serve ignored SIGTERM for %ss; sending SIGKILL" % STOP_WAIT_S, file=sys.stderr)
            proc.kill()
            proc.wait(timeout=STOP_WAIT_S)


# ---------- load ----------

def synthetic_prompt(index, words):
    """Distinct per session (index in the first and repeated words) so no prefix cache is shared between sessions."""
    head = "Session %d. Summarise the following log in three bullet points.\n" % index
    body = " ".join("event-%d-%d status ok latency %d" % (index, i, (i * 37 + index) % 997) for i in range(words // 5))
    return head + body


def run_session(url, model, prompt, gen_tokens, timeout=SESSION_TIMEOUT_S, clock=time.monotonic):
    """One streaming /api/chat with an ABSOLUTE deadline of timeout seconds; returns {ok, ttft_s, decode_tps, tokens,
    error}. Past the deadline the connection is closed and the session counts as an error."""
    payload = {"model": model, "stream": True, "messages": [{"role": "user", "content": prompt}],
               "options": {"num_predict": gen_tokens}}
    t0 = clock()
    state = {"ttft": None}

    def on_line(msg):
        if msg.get("error"):
            return dict(ok=False, error=str(msg["error"]))
        if state["ttft"] is None and (msg.get("message") or {}).get("content"):
            state["ttft"] = clock() - t0
        if msg.get("done"):
            count, dur = msg.get("eval_count") or 0, msg.get("eval_duration") or 0
            ttft = state["ttft"]
            return dict(ok=True, ttft_s=ttft if ttft is not None else clock() - t0, tokens=count,
                        decode_tps=(count / (dur / 1e9)) if dur else None, wall_s=clock() - t0)
        return None

    try:
        result = bounded_request(url + "/api/chat", payload, limit_s=timeout, on_line=on_line, clock=clock)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return dict(ok=False, error="%s: %s" % (type(exc).__name__, exc))
    return result if result is not None else dict(ok=False, error="stream ended without done")


def run_round(url, model, n, prompt_words, gen_tokens, round_index, session=run_session,
              request_timeout=SESSION_TIMEOUT_S, stop=None, clock=time.monotonic):
    """N concurrent sessions. Joins are bounded by the round deadline (request deadline + JOIN_SLACK_S) and poll the
    stop flag; a session still running at the round deadline is counted as an error (its daemon thread dies with
    the server at teardown)."""
    results = [None] * n
    crashed = []

    def work(i):
        try:
            results[i] = session(url, model, synthetic_prompt(round_index * 1000 + i, prompt_words), gen_tokens,
                                 timeout=request_timeout)
        except BaseException as exc:  # re-raised in the caller below so the step's finally stops the server
            crashed.append(exc)

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    t0 = clock()
    deadline = t0 + request_timeout + JOIN_SLACK_S
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            check_stop(stop)
            remaining = deadline - clock()
            if remaining <= 0:
                break
            t.join(min(STOP_POLL_S, remaining))
    if crashed:
        raise crashed[0]
    results = [r if r is not None else dict(ok=False, error="session still running at the round deadline (%.0fs)"
                                            % (request_timeout + JOIN_SLACK_S)) for r in results]
    return results, clock() - t0


# ---------- probes and sampling ----------

def s1_score(s1_url, token=None, timeout=S1_TIMEOUT_S, http=_http_json, clock=time.monotonic):
    """One fixed /score call; returns (ok, latency_s, error)."""
    headers = {"Authorization": "Bearer " + token} if token else {}
    t0 = clock()
    try:
        reply = http(s1_url.rstrip("/") + "/score", {"request": S1_PROBE_REQUEST, "unit_kind": "interactive"},
                     timeout=timeout, headers=headers)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, clock() - t0, "%s: %s" % (type(exc).__name__, exc)
    latency = clock() - t0
    if not isinstance(reply, dict) or reply.get("error") or "answers" not in reply:
        return False, latency, "bad reply: %s" % (reply.get("error") if isinstance(reply, dict) else type(reply))
    return True, latency, None


class Periodic(threading.Thread):
    """Calls fn() every interval seconds until stop(); collects results (exceptions are recorded, not dropped)."""

    def __init__(self, fn, interval):
        super().__init__(daemon=True)
        self.fn, self.interval, self.out = fn, interval, []
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self.out.append(self.fn())
            except Exception as exc:  # recorded as a sample error and counted by the caller
                self.out.append({"error": "%s: %s" % (type(exc).__name__, exc)})
            self._stop_evt.wait(self.interval)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=S1_TIMEOUT_S + 5)
        return self.out


def system_sample(bench_url, run=sc.command, http=_http_json):
    """available bytes (free+inactive+speculative), swap used, memory_pressure %, bench Ollama size / size_vram."""
    sample = {"t": time.time(), "available_bytes": sc.parse_vm_stat(run(["/usr/bin/vm_stat"])),
              "swap_bytes": sc.parse_swap(run(["/usr/sbin/sysctl", "vm.swapusage"]))}
    if os.path.exists("/usr/bin/memory_pressure"):
        sample["pressure_free_pct"] = sc.parse_memory_pressure(run(["/usr/bin/memory_pressure", "-Q"]))
    models = http(bench_url + "/api/ps", timeout=5).get("models") or []
    sample["ollama_size"] = sum(int(m.get("size") or 0) for m in models)
    sample["ollama_vram"] = sum(int(m.get("size_vram") or 0) for m in models)
    return sample


# ---------- summary and verdict ----------

def pct(values, q):
    """Nearest-rank percentile; None for no data."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    rank = max(1, min(len(vals), math.ceil(q * len(vals) / 100.0)))  # nearest rank: ceil(q/100 * n), 1-based
    return vals[rank - 1]


def summarize(n, sessions, walls, s1_probes, samples):
    ok = [s for s in sessions if s.get("ok")]
    sample_errors = [s for s in samples if "error" in s]
    good = [s for s in samples if "error" not in s]
    swaps = [s["swap_bytes"] for s in good]
    total_tokens = sum(s.get("tokens") or 0 for s in ok)
    return {
        "n": n,
        "requests": len(sessions),
        "errors": len(sessions) - len(ok),
        "error_samples": [s.get("error") for s in sessions if not s.get("ok")][:5],
        "ttft_p50_s": pct([s.get("ttft_s") for s in ok], 50),
        "ttft_p95_s": pct([s.get("ttft_s") for s in ok], 95),
        "decode_tps_p50": pct([s.get("decode_tps") for s in ok], 50),
        "aggregate_tps": (total_tokens / sum(walls)) if walls and sum(walls) > 0 else None,
        "min_available_gib": (min(s["available_bytes"] for s in good) / GIB) if good else None,
        "swap_growth_mib": ((max(swaps) - swaps[0]) / MIB) if swaps else None,
        "min_pressure_free_pct": min((s["pressure_free_pct"] for s in good if "pressure_free_pct" in s), default=None),
        "ollama_resident_gib": (max(s["ollama_size"] for s in good) / GIB) if good else None,
        "ollama_vram_gib": (max(s["ollama_vram"] for s in good) / GIB) if good else None,
        "sampler_errors": len(sample_errors),
        "s1_probes": len(s1_probes),
        "s1_failures": sum(1 for p in s1_probes if not p[0]),
        "s1_p50_s": pct([p[1] for p in s1_probes if p[0]], 50),
        "s1_p95_s": pct([p[1] for p in s1_probes if p[0]], 95),
    }


def verdict(rec, baseline_p95, s1_budget, swap_limit_mib, reserve_gib):
    """Returns (PASS|FAIL, reasons). Missing measurements fail closed: an unmeasured step is not a safe step."""
    reasons = []
    if rec["errors"]:
        reasons.append("%d request error(s)" % rec["errors"])
    if rec["s1_failures"]:
        reasons.append("%d S1 failure(s)" % rec["s1_failures"])
    if rec["s1_p95_s"] is None:
        reasons.append("no successful S1 probe")
    elif rec["s1_p95_s"] > baseline_p95 + s1_budget:  # S1's day budget: baseline + 1.0 s
        reasons.append("S1 p95 %.2fs > baseline %.2fs + %.2fs" % (rec["s1_p95_s"], baseline_p95, s1_budget))
    if rec["sampler_errors"] or rec["min_available_gib"] is None:
        reasons.append("memory sampling failed (%d error(s))" % rec["sampler_errors"])
    else:
        if rec["swap_growth_mib"] > swap_limit_mib:  # swap growth means the KV caches no longer fit in RAM
            reasons.append("swap grew %.0f MiB > %d" % (rec["swap_growth_mib"], swap_limit_mib))
        if rec["min_available_gib"] < reserve_gib:  # reserve keeps S1 + OS working set resident
            reasons.append("available memory %.2f GiB < reserve %.1f" % (rec["min_available_gib"], reserve_gib))
    return ("FAIL" if reasons else "PASS"), reasons


def recommended_cap(steps):
    passed = [s["n"] for s in steps if s["verdict"] == "PASS"]
    return max(passed) if passed else None


# ---------- orchestration ----------

class Deps:
    """Everything the bench touches outside Python, injectable for tests."""

    def __init__(self, popen=subprocess.Popen, http=_http_json, sampler=None, session=run_session, s1=None,
                 sleep=time.sleep, in_use=port_in_use, listeners=listener_pids, stop=None):
        self.popen, self.http, self.session, self.sleep = popen, http, session, sleep
        self.in_use, self.listeners = in_use, listeners
        self.stop = stop if stop is not None else sc.StopRequest()
        self.sampler = sampler or (lambda url: system_sample(url, http=http))
        self.s1 = s1 or (lambda url, token: s1_score(url, token, http=http))


def s1_baseline(args, deps):
    probes = []
    for _ in range(S1_BASELINE_PROBES):
        check_stop(deps.stop)
        probes.append(deps.s1(args.s1_url, args.s1_token))
        deps.sleep(args.s1_interval)
    fails = [p for p in probes if not p[0]]
    if fails:
        raise Refused("S1 baseline failed (%d/%d): %s" % (len(fails), len(probes), fails[0][2]))
    return {"p50_s": pct([p[1] for p in probes], 50), "p95_s": pct([p[1] for p in probes], 95), "probes": len(probes)}


def _sample_once(deps, url):
    try:
        return deps.sampler(url)
    except Exception as exc:  # counted as a sampler error by summarize (fails the step), like Periodic's samples
        return {"error": "%s: %s" % (type(exc).__name__, exc)}


def run_step(n, args, deps):
    server = OllamaServer(args.port, args.ctx, n, args.ollama_bin, popen=deps.popen, http=deps.http, sleep=deps.sleep,
                          in_use=deps.in_use, listeners=deps.listeners, stop=deps.stop)
    sampler = Periodic(lambda: deps.sampler(server.url), SAMPLE_SECONDS)
    prober = Periodic(lambda: deps.s1(args.s1_url, args.s1_token), args.s1_interval)
    sessions, walls, first, last = [], [], [], []
    try:
        server.start()
        # Baseline sample, sampling and S1 probing start BEFORE warm-up: model load and KV allocation are where swap
        # grows, memory bottoms out and S1 is disrupted, so the verdict must include them.
        first.append(_sample_once(deps, server.url))
        sampler.start()
        prober.start()
        run_bounded(lambda: deps.http(server.url + "/api/generate",
                                      {"model": args.model, "prompt": "", "keep_alive": "1m"}, timeout=WARM_TIMEOUT_S),
                    WARM_TIMEOUT_S + JOIN_SLACK_S, deps.stop)  # warm load: the weights land before the timed rounds
        for r in range(args.rounds):
            check_stop(deps.stop)
            results, wall = run_round(server.url, args.model, n, args.prompt_tokens, args.gen_tokens, r,
                                      session=deps.session, request_timeout=args.request_timeout, stop=deps.stop)
            sessions.extend(results)
            walls.append(wall)
        last.append(_sample_once(deps, server.url))  # closing sample: a step shorter than SAMPLE_SECONDS is still seen
    finally:
        # Teardown, in order and never interrupted (signal handlers only set a flag): probers (bounded joins), then
        # the owned server (SIGTERM, bounded wait, SIGKILL, reap). The caller releases the lock only after this.
        probes = prober.stop() if prober.is_alive() else prober.out
        samples = sampler.stop() if sampler.is_alive() else sampler.out
        server.stop()
    check_stop(deps.stop)
    probes = [p if isinstance(p, tuple) else (False, None, p.get("error")) for p in probes]
    return summarize(n, sessions, walls, probes, first + samples + last)


def release_lock(fd):
    os.close(fd)  # closing the only fd drops LOCK_SH


def run_bench(args, deps, gate):
    fd = preconditions(gate, args.production_url, deps.http)
    try:
        baseline = s1_baseline(args, deps)
        steps = []
        for n in args.steps:
            rec = run_step(n, args, deps)
            rec["verdict"], rec["reasons"] = verdict(rec, baseline["p95_s"], args.s1_budget, args.swap_limit_mib,
                                                     args.reserve_gib)
            steps.append(rec)
            if rec["verdict"] == "FAIL":
                # Stop at the first FAIL: a larger N only adds KV memory, and going further risks swapping the
                # machine that serves S1.
                break
    finally:
        release_lock(fd)  # only after run_step's teardown stopped the bench server: training may start again
    return {"model": args.model, "ctx": args.ctx, "prompt_tokens": args.prompt_tokens, "gen_tokens": args.gen_tokens,
            "rounds": args.rounds, "s1_baseline": baseline, "steps": steps, "recommended_cap": recommended_cap(steps),
            "thresholds": {"s1_budget_s": args.s1_budget, "swap_limit_mib": args.swap_limit_mib,
                           "reserve_gib": args.reserve_gib},
            "note": "the local gate is uncapped by default (local_gate max_connections 0 = unlimited); concurrency "
                    "through the gate is limited by Ollama OLLAMA_NUM_PARALLEL / OLLAMA_MAX_QUEUE, not by the gate. "
                    "Set OLLAMA_NUM_PARALLEL on the production instance to at most recommended_cap."}


def _f(v, fmt="%.2f"):
    return "-" if v is None else fmt % v


def markdown(report):
    lines = ["S1 idle baseline: p50 %ss, p95 %ss" % (_f(report["s1_baseline"]["p50_s"]),
                                                    _f(report["s1_baseline"]["p95_s"])), "",
             "| N | verdict | errors | TTFT p50/p95 s | decode tok/s p50 | agg tok/s | min avail GiB | swap +MiB "
             "| ollama GiB | S1 p50/p95 s | S1 fails | reasons |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in report["steps"]:
        lines.append("| %d | %s | %d | %s/%s | %s | %s | %s | %s | %s | %s/%s | %d | %s |" % (
            s["n"], s["verdict"], s["errors"], _f(s["ttft_p50_s"]), _f(s["ttft_p95_s"]), _f(s["decode_tps_p50"], "%.1f"),
            _f(s["aggregate_tps"], "%.1f"), _f(s["min_available_gib"]), _f(s["swap_growth_mib"], "%.0f"),
            _f(s["ollama_resident_gib"]), _f(s["s1_p50_s"]), _f(s["s1_p95_s"]), s["s1_failures"],
            "; ".join(s["reasons"]) or "-"))
    cap = report["recommended_cap"]
    lines += ["", "Recommended cap: %s" % ("none (N=%d already failed)" % report["steps"][0]["n"] if cap is None
                                           else "%d sessions" % cap), "Note: " + report["note"]]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--s1-url", default=os.environ.get("CORTADOS_BENCH_S1_URL"),
                   help="S1 scorer base URL (required; or CORTADOS_BENCH_S1_URL)")
    p.add_argument("--port", type=int, default=11436)
    p.add_argument("--ctx", type=int, default=32768)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--ollama-bin", default=OLLAMA_BIN)
    p.add_argument("--production-url", default=PRODUCTION_URL)
    p.add_argument("--steps", default=DEFAULT_STEPS)
    p.add_argument("--prompt-tokens", type=int, default=6000)
    p.add_argument("--gen-tokens", type=int, default=256)
    p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--request-timeout", type=float, default=SESSION_TIMEOUT_S)  # absolute per-session bound (see top)
    p.add_argument("--s1-interval", type=float, default=2.0)
    p.add_argument("--s1-budget", type=float, default=1.0)       # S1 day latency budget: baseline + 1.0 s
    p.add_argument("--swap-limit-mib", type=int, default=256)    # more than noise-level swap means RAM is exhausted
    p.add_argument("--reserve-gib", type=float, default=4.0)     # headroom for S1, macOS and the router on 32 GB
    p.add_argument("--out", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if not args.s1_url:
        p.error("--s1-url (or CORTADOS_BENCH_S1_URL) is required; there is no default host")
    try:
        args.steps = sorted({int(x) for x in args.steps.split(",") if x.strip()})
    except ValueError:
        p.error("--steps must be comma-separated integers")
    if not args.steps or args.steps[0] < 1:
        p.error("--steps must be positive integers")
    if args.port == 11434:
        p.error("--port 11434 is the production instance")
    args.s1_token = os.environ.get("CORTADOS_S1_TOKEN") or None
    args.out = args.out or "./bench-local-capacity-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return args


def main(argv=None, deps=None, gate=None):
    args = parse_args(argv)
    deps = deps or Deps()
    gate = gate or sc.gate_dir()
    if args.dry_run:
        print("plan: steps %s, ctx %d, prompt ~%d words, gen %d, rounds %d, bench port %d, S1 %s every %.1fs"
              % (args.steps, args.ctx, args.prompt_tokens, args.gen_tokens, args.rounds, args.port, args.s1_url,
                 args.s1_interval))
        try:
            fd = preconditions(gate, args.production_url, deps.http)
        except Refused as exc:
            print("check: REFUSED: %s" % exc)
            return 2
        os.close(fd)
        print("check: ok (no training lock or intent, production Ollama idle); nothing loaded (dry run)")
        return 0
    # SIGINT/SIGTERM only set a flag (no exception can land inside teardown); waits poll it and raise Stopped at a
    # safe point, so the bench server is always stopped before the node lock is released.
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    deps.stop.install()
    try:
        report = run_bench(args, deps, gate)
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 2
    except Stopped:
        print("stopped by signal %d; bench server stopped, lock released" % deps.stop.signum, file=sys.stderr)
        return deps.stop.exit_code()
    finally:
        for sig, handler in previous.items():  # restore, so an embedding caller (tests) keeps its own handlers
            signal.signal(sig, handler)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(markdown(report))
    print("JSON: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
