"""Experimental Studio residency controller; no routing changes or S1 signals.

last_change_ts is the start of the current continuously load-eligible interval,
not merely the last Ollama transition. The runner resets it on every unsafe
sample and on restart. See README's Studio capacity section for the status ABI.
"""
from __future__ import annotations

import argparse
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
import time
import urllib.request

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
    request = urllib.request.Request("http://127.0.0.1:11434" + path, data=data,
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
    def __init__(self, model=MODEL, collect=gather, api=ollama, clock=time.time):
        self.model, self.collect, self.api, self.clock = model, collect, api, clock
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
            inputs = self.collect()
            result["inputs"] = inputs
            safe = (not inputs["training_active"] and not inputs["eval_active"]
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
            decision = decide(**{k: inputs[k] for k in (
                "training_active", "eval_active", "free_reclaimable_bytes", "swap_used_bytes")},
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    args.status.parent.mkdir(parents=True, exist_ok=True)
    # One residency owner; launchd restarts must not race another instance.
    with open(str(args.status) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # launchd stops jobs with SIGTERM; turn it into SystemExit so serve's
        # finally releases residency instead of leaving the lease to expire.
        signal.signal(signal.SIGTERM, _terminate)
        serve(Runner(model=args.model), args.status)


if __name__ == "__main__":
    main()
