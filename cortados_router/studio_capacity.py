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
import shlex
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


def parse_processes(text):
    """ps -axo pid=,stat=,command=. Never exclude T (SIGSTOP) processes.

    nightly.sh covers all preflight/MLX/Laya/evaluation phases, unlike the
    dashboard status file which records historical events, not a live lease.
    """
    if not text.strip():
        raise ValueError("empty ps output cannot prove S1 idle")
    training = evaluation = False
    for line in text.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            raise ValueError("invalid ps record")
        args = shlex.split(fields[2])
        names = {Path(arg).name for arg in args}
        # Only executable/script positions: overnight.py carries nightly.sh as
        # a future command even while sleeping between cycles.
        scripts = {Path(arg).name for arg in args[:2]}
        if "nightly.sh" in scripts or ("mlx_lm" in names and "lora" in names):
            training = True
        if "evaluate.py" in scripts:
            evaluation = True
        # Laya direct/manual runs, not just the nightly parent.
        if any("laya" in arg for arg in args) and names & {"train.sh", "train.py", "convert.py"}:
            training = True
        # Include model-loading preflight invoked by nightly.sh if orphaned.
        if "-c" in args and any("mlx_lm import load" in arg for arg in args):
            training = True
    return training, evaluation


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=5).stdout


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
    with opener.open(request, timeout=30) as response:
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

    def tick(self):
        now = self.clock()
        if (self.last_sample_ts is not None and
                not 0 <= now - self.last_sample_ts <= MAX_SAMPLE_GAP_SECONDS):
            self.eligible_since = None
        self.last_sample_ts = now
        result = dict(version=1, sampled_at=now, expires_at=now + STATUS_TTL_SECONDS,
                      model=self.model, context_tokens=CONTEXT, available=False,
                      model_loaded=False, action="unload", reason="unknown")
        try:
            inputs = self.collect()
            result["inputs"] = inputs
            loaded = resident(self.api, self.model)
            eligible = (not inputs["training_active"] and not inputs["eval_active"]
                        and inputs["free_reclaimable_bytes"] >= LOAD_BYTES
                        and inputs["swap_used_bytes"] < MAX_SWAP_BYTES)
            if not eligible:
                self.eligible_since = None
            elif self.eligible_since is None or now < self.eligible_since:
                self.eligible_since = now
            decision = decide(**{k: inputs[k] for k in (
                "training_active", "eval_active", "free_reclaimable_bytes", "swap_used_bytes")},
                model_loaded=loaded, last_change_ts=(now if self.eligible_since is None else self.eligible_since), now=now)
            result.update(action=decision.action, reason=decision.reason)
            if decision.action != "hold":
                payload = {"model": self.model, "stream": False,
                           "keep_alive": 0 if decision.action == "unload" else -1}
                if decision.action == "load":
                    payload["options"] = {"num_ctx": CONTEXT}
                self.api("/api/generate", payload)
                loaded = resident(self.api, self.model)
                if loaded != (decision.action == "load"):
                    raise RuntimeError("Ollama did not confirm residency transition")
            result["model_loaded"] = loaded
            result["available"] = (loaded and not inputs["training_active"]
                                   and not inputs["eval_active"]
                                   and inputs["free_reclaimable_bytes"] >= UNLOAD_BYTES
                                   and inputs["swap_used_bytes"] < MAX_SWAP_BYTES)
        except Exception as exc:
            self.eligible_since = None
            result.update(available=False, reason="fail closed: " + type(exc).__name__)
            # Even failed telemetry or residency queries must attempt shedding.
            try:
                self.api("/api/generate", {"model": self.model, "stream": False, "keep_alive": 0})
            except Exception as unload_error:
                result["unload_error"] = type(unload_error).__name__
        # Slow HTTP work must not extend the snapshot's validity.
        if self.clock() >= result["expires_at"]:
            result.update(available=False, reason="sample expired during control request")
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--status", type=Path, required=True)
    args = parser.parse_args()
    args.status.parent.mkdir(parents=True, exist_ok=True)
    # One residency owner; launchd restarts must not race another instance.
    with open(str(args.status) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = Runner(model=args.model)
        while True:
            write_status(args.status, runner.tick())
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
