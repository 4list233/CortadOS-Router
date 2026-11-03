"""This machine's hardware as a measured resource: memory, CPU, GPU and resident Ollama models.

`sample()` reads the host the way scarcity reads a subscription's "% left": one JSON snapshot
with a derived `headroom` block. It feeds the dashboard first and routing later; nothing in the
router reads it yet. Stdlib only. Every subprocess and the HTTP fetch is injectable (`run`,
`fetch`) so tests never depend on the host; every failure lands in `errors` and the field it
would have filled is marked unavailable with the reason. Missing readings are unknown, never 0.

Worst case one sample takes 5 x CMD_TIMEOUT + OLLAMA_TIMEOUT (about 20.5 s, every command hanging);
normally about 2 s (top's two 1 s samples). A timer that fires more often than the worst case can overlap.

CLI: python3 -m cortados_router.hardware sample [--out [PATH]]
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from cortados_router.paths import state_dir

GIB = 2 ** 30
CMD_TIMEOUT = 4  # seconds per command; top's two 1 s samples need ~2 s, so 4 s bounds a hang
OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
OLLAMA_TIMEOUT = 0.5  # loopback: if Ollama answers at all it answers fast; do not stall sampling
MAX_OLLAMA_BYTES = 65536  # /api/ps lists a handful of models; more means it is not Ollama (and bounds memory)
REASON_CHARS = 300  # a reason is for a human and a JSON field, not a stack dump
# Absolute paths: launchd and cron give a minimal PATH. ioreg -a prints a plist, which has no line width
# (the text form clips to the terminal width, and under launchd the GPU line would be cut).
SYSCTL, VM_STAT, TOP, IOREG = "/usr/sbin/sysctl", "/usr/bin/vm_stat", "/usr/bin/top", "/usr/sbin/ioreg"
IOREG_ARGV = (IOREG, "-a", "-r", "-c", "IOAccelerator", "-d", "1")


def default_run(argv, timeout=CMD_TIMEOUT):
    """Run argv, return stdout text. Raises on non-zero exit, missing binary or timeout."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    return result.stdout


def default_fetch(url, timeout=OLLAMA_TIMEOUT):
    """GET url. timeout is per socket operation; the size cap stops a dripping or huge body."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body = response.read(MAX_OLLAMA_BYTES + 1)
    if len(body) > MAX_OLLAMA_BYTES:
        raise ValueError(f"response over {MAX_OLLAMA_BYTES} bytes")
    return body.decode("utf-8")


def parse_vm_stat(text):
    """Available bytes = (free + inactive + speculative) pages x page size.

    Inactive pages are reclaimable, not guaranteed free; this matches local_resources.mac_memory."""
    page = re.search(r"page size of (\d+) bytes", text)
    if not page:
        raise ValueError("vm_stat did not report a page size")
    pages = {k.strip(): int(v) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)\.", text, re.M)}
    missing = [k for k in ("Pages free", "Pages inactive", "Pages speculative") if k not in pages]
    if missing:
        raise ValueError("vm_stat missing " + ", ".join(missing))
    return (pages["Pages free"] + pages["Pages inactive"] + pages["Pages speculative"]) * int(page[1])


def parse_top_cpu(text):
    """CPU busy % from the LAST 'CPU usage' line of `top -l 2 -n 0 -s 1`.

    Why top and not ps: ps %cpu is a decaying lifetime average per process, so a sum / ncpu lags
    and can exceed 100. top's first sample is also since-boot; the second covers the last second,
    which is the current load we want."""
    lines = re.findall(r"CPU usage:.*?([\d.]+)% idle", text)
    if not lines:
        raise ValueError("top printed no CPU usage line")
    idle = float(lines[-1])
    return round(100.0 - idle, 2)


def parse_ioreg_gpu(text):
    """Device Utilization % from `ioreg -a -r -c IOAccelerator -d 1` (a plist array of accelerators)."""
    devices = plistlib.loads(text.encode("utf-8") if isinstance(text, str) else text)
    if not isinstance(devices, list):
        raise ValueError("ioreg plist is not an array")
    values = []
    for device in devices:
        stats = device.get("PerformanceStatistics") if isinstance(device, dict) else None
        value = stats.get("Device Utilization %") if isinstance(stats, dict) else None
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    if not values:
        raise ValueError("IOAccelerator reports no Device Utilization %")
    return max(values)  # more than one accelerator: report the busiest


def parse_ollama_ps(text):
    """Resident models. A malformed entry fails the whole reading: a partial list would look complete."""
    data = json.loads(text)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise ValueError("ollama /api/ps has no models list")
    out = []
    for i, m in enumerate(models):
        name = (m.get("name") or m.get("model")) if isinstance(m, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError(f"ollama /api/ps entry {i} has no name")
        out.append({"name": name, "size": m.get("size"), "size_vram": m.get("size_vram")})
    return out


def unavailable(reason):
    return {"available": False, "reason": reason}


def sample(run=default_run, fetch=default_fetch, system=None, now=None, hostname=None):
    """One snapshot. sampled_at is epoch seconds when sampling started (readers need not use file mtime)."""
    system = system or platform.system()
    errors = []
    out = {"schema": 1, "sampled_at": round(now if now is not None else time.time(), 3),
           "host": hostname or socket.gethostname().split(".")[0], "os": system}

    def attempt(field, fn):
        try:
            return fn()
        # http.client.HTTPException is not an OSError (BadStatusLine, IncompleteRead); listed so a
        # non-Ollama service on the port is a reading failure, not a crash. No bare Exception: bugs surface.
        except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:REASON_CHARS]
            errors.append({"field": field, "error": reason})
            return unavailable(reason)

    try:
        load = os.getloadavg()
        out["load_avg"] = [round(x, 2) for x in load]
    except OSError as exc:
        errors.append({"field": "load_avg", "error": str(exc)})
        out["load_avg"] = unavailable(str(exc))
    out["ncpu"] = os.cpu_count()

    if system == "Darwin":
        out["chip"] = attempt("chip", lambda: run([SYSCTL, "-n", "machdep.cpu.brand_string"]).strip()
                              or _raise("empty chip name"))
        out["mem_total_bytes"] = attempt("mem_total_bytes", lambda: int(run([SYSCTL, "-n", "hw.memsize"]).strip()))
        out["mem_available_bytes"] = attempt("mem_available_bytes", lambda: parse_vm_stat(run([VM_STAT])))
        out["cpu_busy_pct"] = attempt("cpu_busy_pct", lambda: parse_top_cpu(run([TOP, "-l", "2", "-n", "0", "-s", "1"])))
        out["gpu_busy_pct"] = attempt("gpu_busy_pct", lambda: parse_ioreg_gpu(run(list(IOREG_ARGV))))
    else:
        reason = f"not implemented on {system}"
        for field in ("chip", "mem_total_bytes", "mem_available_bytes", "cpu_busy_pct", "gpu_busy_pct"):
            out[field] = unavailable(reason)
        errors.append({"field": "platform", "error": reason})

    out["ollama_models"] = attempt("ollama_models", lambda: parse_ollama_ps(fetch(OLLAMA_URL)))

    def num(key):
        value = out.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    mem, cpu, gpu = num("mem_available_bytes"), num("cpu_busy_pct"), num("gpu_busy_pct")
    out["headroom"] = {
        "free_mem_gb": round(mem / GIB, 2) if mem is not None else None,
        "cpu_idle_pct": round(100.0 - cpu, 2) if cpu is not None else None,
        "gpu_idle_pct": round(100.0 - gpu, 2) if gpu is not None else None,
    }
    out["errors"] = errors
    return out


def _raise(message):
    raise ValueError(message)


def default_out_path(host):
    return state_dir() / "hardware" / f"{host}.json"


def write_atomic(path, data, require_owned=False):
    """Write JSON via a temp file + rename: file 0600, readers never see half a file.

    Only directories this call creates are tightened to 0700 (every missing level, not just the leaf:
    mkdir's mode is masked by umask and Path.mkdir applies it to the leaf only). A directory that
    already exists keeps its mode: it is not ours to change (--out ./x.json, /tmp)."""
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
    if require_owned:  # our own state tree: refuse a planted symlink or someone else's directory
        info = os.lstat(path.parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"{path.parent} is not a directory owned by this user")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:  # adopt fd first so a failing fchmod still closes it
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # never mask the real error with a cleanup error
            os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cortados_router.hardware")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="sample this machine and print JSON")
    sp.add_argument("--out", nargs="?", const=True, default=None,
                    help="also write atomically; with no PATH: <state dir>/hardware/<host>.json")
    args = parser.parse_args(argv)
    if args.out == "":
        parser.error("--out PATH is empty; omit PATH for the default")
    data = sample()
    print(json.dumps(data, indent=2, sort_keys=True))  # printed even if the write below fails
    if args.out is not None:
        try:
            if args.out is True:
                write_atomic(default_out_path(data["host"]), data, require_owned=True)
            else:
                write_atomic(args.out, data)
        except OSError as exc:
            print(f"hardware: could not write sample: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
