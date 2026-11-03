"""This machine's hardware as a measured resource: memory, CPU, GPU and resident Ollama models.

`sample()` reads the host the way scarcity reads a subscription's "% left": one JSON snapshot
with a derived `headroom` block. It feeds the dashboard first and routing later; nothing in the
router reads it yet. Stdlib only. Every subprocess and the HTTP fetch is injectable (`run`,
`fetch`) so tests never depend on the host; every failure lands in `errors` and the field it
would have filled is marked unavailable with the reason. Missing readings are unknown, never 0.

CLI: python3 -m cortados_router.hardware sample [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

GIB = 2 ** 30
CMD_TIMEOUT = 4  # seconds per command; top's two 1 s samples need ~2 s, so 4 s bounds a hang
OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
OLLAMA_TIMEOUT = 0.5  # loopback: if Ollama answers at all it answers fast; do not stall sampling


def default_run(argv, timeout=CMD_TIMEOUT):
    """Run argv, return stdout text. Raises on non-zero exit, missing binary or timeout."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    return result.stdout


def default_fetch(url, timeout=OLLAMA_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


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
    values = re.findall(r'"Device Utilization %"\s*=\s*(\d+)', text)
    if not values:
        raise ValueError("IOAccelerator reports no Device Utilization %")
    return max(int(v) for v in values)  # more than one accelerator: report the busiest


def parse_ollama_ps(text):
    data = json.loads(text)
    models = data.get("models")
    if not isinstance(models, list):
        raise ValueError("ollama /api/ps has no models list")
    return [{"name": m.get("name") or m.get("model"), "size": m.get("size"),
             "size_vram": m.get("size_vram")} for m in models if isinstance(m, dict)]


def unavailable(reason):
    return {"available": False, "reason": reason}


def sample(run=default_run, fetch=default_fetch, system=None, now=None, hostname=None):
    system = system or platform.system()
    errors = []
    out = {"schema": 1, "sampled_at": round(now if now is not None else time.time(), 3),
           "host": hostname or socket.gethostname().split(".")[0], "os": system}

    def attempt(field, fn):
        try:
            return fn()
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            reason = f"{type(exc).__name__}: {exc}"[:300]
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
        out["chip"] = attempt("chip", lambda: run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
                              or _raise("empty chip name"))
        out["mem_total_bytes"] = attempt("mem_total_bytes", lambda: int(run(["sysctl", "-n", "hw.memsize"]).strip()))
        out["mem_available_bytes"] = attempt("mem_available_bytes", lambda: parse_vm_stat(run(["vm_stat"])))
        out["cpu_busy_pct"] = attempt("cpu_busy_pct", lambda: parse_top_cpu(run(["top", "-l", "2", "-n", "0", "-s", "1"])))
        out["gpu_busy_pct"] = attempt("gpu_busy_pct", lambda: parse_ioreg_gpu(run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"])))
    else:
        reason = f"not implemented on {system}"
        for field in ("chip", "mem_total_bytes", "mem_available_bytes", "cpu_busy_pct", "gpu_busy_pct"):
            out[field] = unavailable(reason)
        errors.append({"field": "platform", "error": reason})
        out["chip"] = platform.processor() or unavailable(reason)

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
    home = Path(os.environ.get("CORTADOS_HOME") or Path.home() / ".cortados")
    return home / "hardware" / f"{host}.json"


def write_atomic(path, data):
    """Write JSON via a temp file + rename: directory 0700, file 0600, readers never see half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cortados_router.hardware")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sample", help="sample this machine and print JSON")
    sp.add_argument("--out", nargs="?", const="", default=None,
                    help="also write atomically; with no PATH: $CORTADOS_HOME/hardware/<host>.json")
    args = parser.parse_args(argv)
    data = sample()
    if args.out is not None:
        write_atomic(args.out or default_out_path(data["host"]), data)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
