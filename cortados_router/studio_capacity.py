"""Experimental Studio residency controller; no routing changes or S1 signals.

last_change_ts is the start of the current continuously load-eligible interval,
not merely the last Ollama transition. The runner resets it on every unsafe
sample and on restart. See README's Studio capacity section for the status ABI.
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

from .local_resources import mac_memory

GIB = 2 ** 30
# 8 GiB model allowance + 8 GiB remaining host/S1 safety margin.
LOAD_BYTES = 16 * GIB
# Keep the host/S1 safety margin after the model is resident.
UNLOAD_BYTES = 8 * GIB
# Two minutes of continuously safe observations avoids reload oscillation.
DWELL_SECONDS = 120
# Existing ~1.84 GiB swap is tolerated; 4 GiB indicates excessive pressure.
MAX_SWAP_BYTES = 4 * GIB
# Short observation interval, not a mutual-exclusion guarantee with S1.
POLL_SECONDS = 5
# A missed observation interval breaks proof of continuously safe headroom.
MAX_SAMPLE_GAP_SECONDS = 2 * POLL_SECONDS
# Consumers must fail closed after three missed observations.
STATUS_TTL_SECONDS = 15
# Survey recommendation: bounded 8K runtime, not existing 64K/128K tag defaults.
CONTEXT = 8192
MODEL = "studio-qwen3-8b:latest"
# Bounded residency lease: Ollama itself evicts the model this many seconds
# after the last renewal, so a crashed, killed or wedged controller cannot pin
# RAM for longer than this. Renewed only after a fresh, safe observation.
LEASE_SECONDS = 180
# An observation older than one poll interval no longer describes the host;
# it must never justify loading or renewing (training may have started).
MAX_OBSERVATION_AGE_SECONDS = POLL_SECONDS
# Each local probe (vm_stat, ps, sysctl) finishes in milliseconds; 2 s bounds a
# wedged probe so five probes cannot exceed STATUS_TTL_SECONDS on their own.
COMMAND_TIMEOUT_SECONDS = 2
# Loopback Ollama management calls (ps, keep_alive changes) are sub-second;
# a cold load of an 8B model from disk can take a few seconds. 10 s bounds a
# hung daemon; the stale-observation check covers whatever this delays.
HTTP_TIMEOUT_SECONDS = 10
# --- S1 training lease/ack handshake ---------------------------------------
# A trainer's intent expires unless refreshed, so a trainer that dies without
# its finally (SIGKILL, power loss) cannot keep the model shed forever; the pid
# liveness check frees it sooner on an ordinary crash.
INTENT_TTL_SECONDS = 300
# Refresh well inside the TTL: four missed refreshes before an intent lapses.
INTENT_REFRESH_SECONDS = 60
# The daemon acks within one poll after the unload is confirmed (5 s poll + a
# sub-second keep_alive=0 + /api/ps); 30 s is six polls, enough for a slow
# unload, short enough that a nightly run is never held up meaningfully.
ACK_WAIT_SECONDS = 30
ACK_POLL_SECONDS = 0.5
# Test-only overrides (defaults unchanged when unset): CORTADOS_STUDIO_ACK_WAIT
# shortens the ack wait so the nightly harness does not spend 30 s per gated
# step; CORTADOS_STUDIO_OLLAMA_PORT points the loopback management calls at
# another local port (a closed one stands for "Ollama not running"). The host
# stays 127.0.0.1 either way: residency control never leaves the machine.
ACK_WAIT_ENV = "CORTADOS_STUDIO_ACK_WAIT"
OLLAMA_PORT_ENV = "CORTADOS_STUDIO_OLLAMA_PORT"
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")  # uuid4().hex, as training_gate writes it
# Default status path is the one the LaunchAgent template passes via --status.
DEFAULT_STATUS = Path.home() / "Library" / "Application Support" / "CortadOS" / "studio-capacity.json"


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def decide(training_active, eval_active, free_reclaimable_bytes, swap_used_bytes,
           model_loaded, last_change_ts, now):
    """Pure policy. Unknown/invalid measurements shed residency, never admit."""
    numbers = (free_reclaimable_bytes, swap_used_bytes, last_change_ts, now)
    valid = all(isinstance(n, (int, float)) and not isinstance(n, bool)
                and math.isfinite(n) and n >= 0 for n in numbers)
    if (not valid or last_change_ts > now
            or type(training_active) is not bool or type(eval_active) is not bool
            or type(model_loaded) is not bool):
        return Decision("unload", "unknown or invalid input: fail closed")
    unsafe = None
    if training_active or eval_active:
        unsafe = "S1 heavy cycle/process owns priority (including stopped trainers)"
    elif free_reclaimable_bytes < UNLOAD_BYTES:
        unsafe = "headroom below 8 GiB host/S1 safety floor"
    elif swap_used_bytes >= MAX_SWAP_BYTES:
        unsafe = "swap at/above 4 GiB pressure ceiling"
    if unsafe:
        return Decision("unload" if model_loaded else "hold", unsafe)
    if model_loaded:
        return Decision("hold", "resident within keep thresholds")
    if free_reclaimable_bytes < LOAD_BYTES:
        return Decision("hold", "load requires 16 GiB estimated reclaimable headroom")
    if now - last_change_ts < DWELL_SECONDS:
        return Decision("hold", "load requires 120 seconds continuously eligible")
    return Decision("load", "idle with 16 GiB headroom sustained for 120 seconds")


def parse_vm_stat(text):
    page = re.search(r"page size of (\d+) bytes", text)
    pages = dict(re.findall(r"^(Pages (?:free|inactive|speculative)):\s+(\d+)\.", text, re.M))
    if not page or int(page[1]) <= 0 or len(pages) != 3:
        raise ValueError("incomplete vm_stat")
    # Do not add purgeable, wired, compressor or GPU allocations (overlap).
    return mac_memory(text)


def parse_memory_pressure(text):
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if not match or not 0 <= int(match[1]) <= 100:
        raise ValueError("invalid memory_pressure -Q")
    # Diagnostic percentage only: NOT an allocatable byte estimate.
    return int(match[1])


def parse_swap(text):
    match = re.search(r"\bused\s*=\s*([\d.]+)([KMGT])\b", text)
    if not match:
        raise ValueError("invalid vm.swapusage")
    return int(float(match[1]) * 1024 ** ("KMGT".index(match[2]) + 1))


_VALUE_FLAGS = {"-W", "-X", "-Q"}


def _script(tokens):
    """First positional argument after an interpreter, skipping its flags.

    ps prints the space-joined argv, not shell-quoted words, so tokens are a
    plain whitespace split; quoting can never be recovered and is not assumed.
    """
    rest = tokens[1:]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in ("-m", "-c"):
            return None
        if token in _VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return Path(token).name
    return None


def parse_processes(text):
    """ps -axo pid=,stat=,command=. Never exclude T (SIGSTOP) processes.

    nightly.sh covers all preflight/MLX/Laya/evaluation phases, unlike the
    dashboard status file which records historical events, not a live lease.
    Matching is deliberately conservative: a false positive only sheds the
    local model, a false negative lets it contend with S1.
    """
    if not text.strip():
        raise ValueError("empty ps output cannot prove S1 idle")
    training = evaluation = False
    for line in text.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            raise ValueError("invalid ps record")
        raw = fields[2]
        tokens = raw.split()
        names = {Path(arg).name for arg in tokens}
        # Only executable/script positions: overnight.py carries nightly.sh as
        # a future command even while sleeping between cycles.
        scripts = {Path(tokens[0]).name, _script(tokens)}
        if "nightly.sh" in scripts:
            training = True
        # Any mlx_lm use (python -m mlx_lm lora, mlx_lm.lora, mlx_lm in a -c
        # source such as the nightly preflight's load()) is S1 model work.
        if any("mlx_lm" in arg for arg in tokens) and (
                "lora" in raw or "-c" in tokens or "load" in raw):
            training = True
        if "evaluate.py" in scripts:
            evaluation = True
        # Laya direct/manual runs, not just the nightly parent.
        if "laya" in raw and names & {"train.sh", "train.py", "convert.py"}:
            training = True
    return training, evaluation


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=COMMAND_TIMEOUT_SECONDS).stdout


def gather(run=command):
    if sys.platform != "darwin":
        raise RuntimeError("Studio capacity runner requires macOS")
    free = parse_vm_stat(run(["/usr/bin/vm_stat"]))
    pressure_pct = parse_memory_pressure(run(["/usr/bin/memory_pressure", "-Q"]))
    pressure_level = int(run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    # Kernel level 1 is normal; warning/critical must shed immediately.
    if pressure_level != 1:
        free = 0
    swap = parse_swap(run(["/usr/sbin/sysctl", "vm.swapusage"]))
    training, evaluation = parse_processes(run(["/bin/ps", "-axo", "pid=,stat=,command="]))
    return dict(training_active=training, eval_active=evaluation,
                free_reclaimable_bytes=free, swap_used_bytes=swap,
                pressure_free_pct=pressure_pct)


def ollama(path, payload=None):
    """Fixed loopback management only; never inherit an HTTP proxy or pull."""
    data = None if payload is None else json.dumps(payload).encode()
    port = int(os.environ.get(OLLAMA_PORT_ENV) or 11434)
    request = urllib.request.Request("http://127.0.0.1:%d" % port + path, data=data,
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.load(response)


def resident(api, model):
    models = api("/api/ps")["models"]
    if not isinstance(models, list):
        raise ValueError("invalid Ollama residency")
    return any(item.get("name") == model or item.get("model") == model for item in models)


def write_status(path, status):
    """Atomic owner-only snapshot; readers must validate version + freshness."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(status, stream, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Runner:
    def __init__(self, model=MODEL, collect=gather, api=ollama, clock=time.time,
                 gate=None, alive=None):
        self.model, self.collect, self.api, self.clock = model, collect, api, clock
        # gate: handshake directory; None disables the handshake (tests/legacy).
        self.gate, self.alive = gate, alive or pid_alive
        self.eligible_since = None
        self.last_sample_ts = None
        # None = unknown. Shedding an unloaded model is skipped only when we
        # know it is absent: a keep_alive request may itself page weights in.
        self.maybe_loaded = None

    def shed(self, result=None):
        """Idempotent unload; Ollama keep_alive=0 releases residency now."""
        try:
            self.api("/api/generate", {"model": self.model, "stream": False, "keep_alive": 0})
        except Exception as unload_error:
            if result is not None:
                result["unload_error"] = type(unload_error).__name__
            return False
        return True

    def tick(self):
        now = self.clock()
        if (self.last_sample_ts is not None and
                not 0 <= now - self.last_sample_ts <= MAX_SAMPLE_GAP_SECONDS):
            self.eligible_since = None
        self.last_sample_ts = now
        result = dict(version=1, sampled_at=now, expires_at=now + STATUS_TTL_SECONDS,
                      model=self.model, context_tokens=CONTEXT, available=False,
                      model_loaded=False, action="unload", reason="unknown",
                      lease_seconds=LEASE_SECONDS)
        try:
            # Explicit trainer intents first: a live intent forces shed and
            # forbids load/renew. Reading them failing raises -> fail closed.
            intents = ([] if self.gate is None else
                       live_intents(self.gate, now, alive=self.alive))
            result["training_intents"] = len(intents)
            inputs = self.collect()
            result["inputs"] = inputs
            # ps-based detection stays as the second line of defence.
            heavy = inputs["training_active"] or bool(intents)
            safe = (not heavy and not inputs["eval_active"]
                    and inputs["free_reclaimable_bytes"] >= UNLOAD_BYTES
                    and inputs["swap_used_bytes"] < MAX_SWAP_BYTES)
            if not safe:
                # Mandatory shedding first: never wait on a residency query.
                self.eligible_since = None
                # Unknown residency (None) sheds like loaded: only a confirmed
                # absence may skip the unload.
                if self.maybe_loaded is not False and not self.shed(result):
                    raise RuntimeError("unload failed")
            loaded = resident(self.api, self.model)
            self.maybe_loaded = loaded
            eligible = safe and inputs["free_reclaimable_bytes"] >= LOAD_BYTES
            if not eligible:
                self.eligible_since = None
            elif self.eligible_since is None or now < self.eligible_since:
                self.eligible_since = now
            decision = decide(training_active=heavy, **{k: inputs[k] for k in (
                "eval_active", "free_reclaimable_bytes", "swap_used_bytes")},
                model_loaded=loaded, last_change_ts=(now if self.eligible_since is None else self.eligible_since), now=now)
            result.update(action=decision.action, reason=decision.reason)
            renew = decision.action == "load" or (decision.action == "hold" and loaded and safe)
            if renew and self.clock() - now > MAX_OBSERVATION_AGE_SECONDS:
                # Training may have started since this observation was taken.
                raise RuntimeError("stale observation")
            if decision.action == "unload":
                if not self.shed(result):
                    raise RuntimeError("unload failed")
            elif renew:
                # Every load and renewal pins num_ctx: a renewal without it
                # resolves Ollama's defaults and may reload at a larger context.
                payload = {"model": self.model, "stream": False, "keep_alive": LEASE_SECONDS,
                           "options": {"num_ctx": CONTEXT}}
                self.maybe_loaded = True
                self.api("/api/generate", payload)
            if decision.action != "hold":
                loaded = resident(self.api, self.model)
                self.maybe_loaded = loaded
                if loaded != (decision.action == "load"):
                    raise RuntimeError("Ollama did not confirm residency transition")
            result["model_loaded"] = loaded
            result["available"] = loaded and safe
            if intents:
                # Ack only after /api/ps confirmed the model is not resident.
                if loaded or resident(self.api, self.model):
                    raise RuntimeError("model still resident under training intent")
                for intent in intents:
                    write_ack(self.gate, intent, self.model, self.clock())
        except Exception as exc:
            self.eligible_since = None
            result.update(available=False, reason="fail closed: " + type(exc).__name__)
            # Even failed telemetry or residency queries must attempt shedding.
            if self.maybe_loaded is not False:
                self.shed(result)
        # Slow control work must not extend the observation's validity: revoke
        # status, restart the dwell, and shed rather than keep a stale lease.
        if self.clock() >= result["expires_at"]:
            self.eligible_since = None
            result.update(available=False, reason="sample expired during control request")
            if self.maybe_loaded is not False:
                self.shed(result)
        return result


def gate_dir(home=None):
    """Handshake directory: $CORTADOS_HOME/studio-gate (intents + acks)."""
    home = home or os.environ.get("CORTADOS_HOME") or str(Path.home() / ".cortados")
    return Path(home) / "studio-gate"


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def _read_json(path):
    with open(path) as stream:
        return json.load(stream)


def live_intents(gate, now, alive=pid_alive):
    """Unexpired intents whose trainer pid is alive. Expired or dead ones are
    removed so a crashed trainer cannot hold the model off forever."""
    gate = Path(gate)
    if not gate.is_dir():
        return []
    live = []
    for path in sorted(gate.glob("intent-*.json")):
        try:
            intent = _read_json(path)
            pid, nonce, expires = _valid_intent(intent, now)
        except FileNotFoundError:
            continue  # trainer removed it between glob and read
        except (ValueError, KeyError, TypeError, AttributeError):
            # A malformed intent fails closed (counts as live) until it is
            # older than one TTL, then it is discarded with a log line.
            try:
                age = now - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < INTENT_TTL_SECONDS:
                live.append(dict(pid=None, nonce=None, expires_at=None))
            else:
                print("studio_capacity: discarding malformed intent " + path.name, file=sys.stderr)
                path.unlink()
            continue
        if expires <= now or not alive(pid):
            print("studio_capacity: discarding %s intent pid %d" % (
                "expired" if expires <= now else "dead", pid), file=sys.stderr)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            with contextlib.suppress(FileNotFoundError):
                (gate / ("ack-" + nonce + ".json")).unlink()
            continue
        live.append(dict(pid=pid, nonce=nonce, expires_at=expires))
    return live


def _finite(value):
    # bool is an int subclass; json yields NaN/inf for NaN, Infinity and 1e999.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("non-finite or non-numeric timestamp")
    return float(value)


def _valid_intent(intent, now):
    """Strict v1 schema. Anything else raises and takes the malformed path,
    whose mtime bound guarantees cleanup (a NaN/inf expiry never would)."""
    if intent.get("version") != 1:
        raise ValueError("unsupported intent version")
    pid = intent["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid pid")
    nonce = intent["nonce"]
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        raise ValueError("invalid nonce")
    created, expires = _finite(intent["created_at"]), _finite(intent["expires_at"])
    # A lease longer than the TTL, or one reaching past now + TTL, is not one
    # training_gate writes; bounding it keeps every intent's hold finite.
    if not 0 < expires - created <= INTENT_TTL_SECONDS or expires > now + INTENT_TTL_SECONDS:
        raise ValueError("lease out of bounds")
    return pid, nonce, expires


def _connection_refused(exc):
    """Nothing listening on the Ollama port: no model can be resident."""
    reason = getattr(exc, "reason", None)
    return isinstance(exc, ConnectionRefusedError) or isinstance(reason, ConnectionRefusedError)


class GateRefused(RuntimeError):
    """Residency could not be confirmed absent: training must not start."""


def write_ack(gate, intent, model, now):
    if intent.get("nonce") is None:
        return  # malformed intent: shed, but there is nobody to ack
    write_status(Path(gate) / ("ack-" + intent["nonce"] + ".json"),
                 dict(version=1, nonce=intent["nonce"], pid=intent["pid"], model=model,
                      model_loaded=False, acked_at=now))


def _daemon_alive(status_path, now):
    """A fresh v1 snapshot from a running controller; else the daemon is down."""
    try:
        status = _read_json(status_path)
        return (status.get("version") == 1 and status.get("reason") != "controller stopped"
                and float(status["sampled_at"]) <= now < float(status["expires_at"]))
    except FileNotFoundError:
        return False
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


@contextlib.contextmanager
def training_gate(gate=None, status_path=None, model=MODEL, api=ollama, clock=time.time,
                  sleep=time.sleep, log=None, pid=None, wait_seconds=ACK_WAIT_SECONDS,
                  refresh_seconds=INTENT_REFRESH_SECONDS):
    """Hold an S1 training intent for the duration of the block.

    Writes an intent, waits (bounded) for the daemon's ack that the model is
    confirmed unloaded, and never proceeds silently: on timeout, whatever the
    daemon's status says, it queries /api/ps itself, unloads a resident model
    and confirms absence, raising GateRefused if that cannot be confirmed.
    Only a refused connection (Ollama not running) proceeds without a query
    result, with a log line."""
    log = log or (lambda message: print("studio_gate: " + message, file=sys.stderr))
    gate = Path(gate) if gate is not None else gate_dir()
    status_path = Path(status_path or os.environ.get("CORTADOS_STUDIO_STATUS") or DEFAULT_STATUS)
    gate.mkdir(parents=True, exist_ok=True)
    pid = os.getpid() if pid is None else pid
    nonce = uuid.uuid4().hex
    intent_path = gate / ("intent-%d-%s.json" % (pid, nonce))
    ack_path = gate / ("ack-" + nonce + ".json")

    def write_intent():
        now = clock()
        write_status(intent_path, dict(version=1, pid=pid, nonce=nonce, created_at=now,
                                       expires_at=now + INTENT_TTL_SECONDS))

    stop = threading.Event()

    def refresh():
        while not stop.wait(refresh_seconds):
            try:
                write_intent()
            except OSError as exc:
                log("intent refresh failed: " + type(exc).__name__)

    write_intent()
    refresher = threading.Thread(target=refresh, daemon=True)
    refresher.start()
    try:
        deadline = clock() + wait_seconds
        while not ack_path.exists():
            if clock() >= deadline:
                # Status freshness says nothing about residency (a daemon killed
                # right after a renewal leaves the model loaded for a whole lease;
                # a wrong status path looks "dead"): always ask Ollama directly.
                log("no ack in %ss (capacity daemon %s); checking %s residency directly" % (
                    wait_seconds, "live" if _daemon_alive(status_path, clock()) else
                    "not running: status %s stale or missing" % status_path, model))
                _confirm_unloaded(api, model, log)
                break
            sleep(ACK_POLL_SECONDS)
        else:
            log("daemon acked: %s unloaded" % model)
        yield intent_path
    finally:
        stop.set()
        refresher.join(timeout=5)
        for path in (intent_path, ack_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def _confirm_unloaded(api, model, log):
    try:
        loaded = resident(api, model)
    except Exception as exc:
        if _connection_refused(exc):
            log("Ollama not running (connection refused): nothing resident; proceeding")
            return
        raise GateRefused("studio_gate: residency query failed (%s); refusing to start" %
                          type(exc).__name__) from exc
    if loaded:
        log("%s resident; unloading (keep_alive=0)" % model)
        try:
            api("/api/generate", {"model": model, "stream": False, "keep_alive": 0})
            loaded = resident(api, model)
        except Exception as exc:
            raise GateRefused("studio_gate: unload of %s unconfirmed (%s); refusing to start" % (
                model, type(exc).__name__)) from exc
        if loaded:
            raise GateRefused("studio_gate: %s still resident after keep_alive=0" % model)
    log("confirmed %s not resident; proceeding" % model)


# Exit status of `gate` when it refuses (EX_TEMPFAIL): callers can tell a
# refused gate from a failing command.
GATE_REFUSED_EXIT = 75


def gate_main(argv):
    """`studio_capacity gate -- cmd ...`: run cmd under a training intent."""
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("usage: python -m cortados_router.studio_capacity gate -- <command> ...", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _terminate)
    try:
        wait = float(os.environ.get(ACK_WAIT_ENV) or ACK_WAIT_SECONDS)
        gate = training_gate(wait_seconds=wait)
        gate.__enter__()
    except GateRefused as exc:
        print(str(exc), file=sys.stderr)
        return GATE_REFUSED_EXIT
    try:
        return subprocess.call(argv)
    finally:
        gate.__exit__(None, None, None)


def serve(runner, status_path, sleep=time.sleep, polls=None):
    """Control loop. Any exit (SIGTERM, status-write failure, crash) unloads."""
    count = 0
    try:
        while polls is None or count < polls:
            write_status(status_path, runner.tick())
            count += 1
            sleep(POLL_SECONDS)
    finally:
        final = dict(version=1, model=runner.model, available=False, model_loaded=False,
                     action="unload", reason="controller stopped")
        if runner.maybe_loaded is not False and not runner.shed(final):
            # Residency may persist until the lease expires: say so, never
            # claim model_loaded=False after a failed unload.
            runner.maybe_loaded = None
            final.update(model_loaded=None, reason="controller stopped; unload failed")
            print("studio_capacity: shutdown unload failed: " + final.get("unload_error", "unknown"),
                  file=sys.stderr)
        try:
            final.update(sampled_at=runner.clock(), expires_at=runner.clock())
            write_status(status_path, final)
        except Exception as exc:
            print("studio_capacity: final status write failed: " + type(exc).__name__,
                  file=sys.stderr)


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def main():
    if sys.argv[1:2] == ["gate"]:
        sys.exit(gate_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--gate", type=Path, default=None,
                        help="training handshake directory (default $CORTADOS_HOME/studio-gate)")
    args = parser.parse_args()
    args.status.parent.mkdir(parents=True, exist_ok=True)
    # One residency owner; launchd restarts must not race another instance.
    with open(str(args.status) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # launchd stops jobs with SIGTERM; turn it into SystemExit so serve's
        # finally releases residency instead of leaving the lease to expire.
        signal.signal(signal.SIGTERM, _terminate)
        serve(Runner(model=args.model, gate=args.gate or gate_dir()), args.status)


if __name__ == "__main__":
    main()
