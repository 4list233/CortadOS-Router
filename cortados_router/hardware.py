"""This machine's hardware as a measured resource: memory, CPU, GPU and resident Ollama models.

`sample()` reads the host the way scarcity reads a subscription's "% left": one JSON snapshot
with a derived `headroom` block. It feeds the dashboard first and routing later; nothing in the
router reads it yet. Stdlib only. Every subprocess and the HTTP fetch is injectable (`run`,
`fetch`) so tests never depend on the host; every failure lands in `errors` and the field it
would have filled is marked unavailable with the reason. Missing readings are unknown, never 0.

Worst case one sample takes 5 x CMD_TIMEOUT + FETCH_BUDGET + OLLAMA_TIMEOUT (about 22.5 s, every command
hanging; the last body read may block one more OLLAMA_TIMEOUT);
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
import xml.parsers.expat

from cortados_router.paths import state_dir

GIB = 2 ** 30
CMD_TIMEOUT = 4  # seconds per command; top's two 1 s samples need ~2 s, so 4 s bounds a hang
OLLAMA_URL = "http://127.0.0.1:11434/api/ps"
OLLAMA_TIMEOUT = 0.5  # loopback: if Ollama answers at all it answers fast; do not stall sampling
FETCH_BUDGET = 2.0  # total wall clock for one fetch: OLLAMA_TIMEOUT is per socket op and does not bound a dripping body
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # refuse: urllib then raises HTTPError for the 3xx


def default_fetch(url, timeout=OLLAMA_TIMEOUT):
    """GET url. timeout is per socket operation; FETCH_BUDGET bounds the whole body read (a peer
    dripping one byte per timeout would otherwise stall for hours); the size cap stops a huge body."""
    deadline = time.monotonic() + FETCH_BUDGET
    chunks, size = [], 0
    # No redirects: whatever holds the port could otherwise point the GET at any URL and have that
    # body land in the sample. A 3xx becomes an HTTPError (an OSError), so attempt() records it.
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(url, timeout=timeout) as response:
        while size <= MAX_OLLAMA_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError(f"body not complete within {FETCH_BUDGET}s")  # an OSError: attempt() records it
            chunk = response.read(min(8192, MAX_OLLAMA_BYTES + 1 - size))
            if not chunk:
                break
            if time.monotonic() > deadline:  # a read may block one more OLLAMA_TIMEOUT past the budget
                raise TimeoutError(f"body not complete within {FETCH_BUDGET}s")
            chunks.append(chunk)
            size += len(chunk)
    body = b"".join(chunks)
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
    try:
        devices = plistlib.loads(text.encode("utf-8") if isinstance(text, str) else text)
    except xml.parsers.expat.ExpatError as exc:  # plistlib normalises detection failures only, not malformed XML
        raise ValueError(f"ioreg plist is malformed: {exc}") from None
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
    except (OSError, AttributeError) as exc:  # AttributeError: os.getloadavg does not exist on Windows
        errors.append({"field": "load_avg", "error": str(exc)})
        out["load_avg"] = unavailable(str(exc))
    ncpu = os.cpu_count()
    if ncpu:
        out["ncpu"] = ncpu
    else:  # os.cpu_count() returns None when it cannot tell: unknown, with a reason, like every other field
        errors.append({"field": "ncpu", "error": "os.cpu_count() returned None"})
        out["ncpu"] = unavailable("os.cpu_count() returned None")

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


_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def safe_host(host):
    """host as a single safe file-name component: anything outside [A-Za-z0-9_-] becomes '_'.

    The host name is not validated by the OS for our purpose; '/' or '..' in it must not move the
    default write out of the state dir."""
    cleaned = _HOST_UNSAFE.sub("_", str(host or ""))
    return cleaned or "unknown"


def default_out_path(host):
    return state_dir() / "hardware" / f"{safe_host(host)}.json"


def write_atomic(path, data, require_owned=False):
    """Write JSON via a temp file + rename: file 0600, readers never see half a file.

    Only directories this call creates are tightened to 0700 (every missing level, not just the leaf:
    mkdir's mode is masked by umask and Path.mkdir applies it to the leaf only). A directory that
    already exists keeps its mode: it is not ours to change (--out ./x.json, /tmp)."""
    path = Path(path)
    owned_levels = _state_levels(path.parent) if require_owned else []
    _check_owned(owned_levels, existing_only=True)  # before creating anything under a planted link
    missing = []
    parent = path.parent
    while not os.path.lexists(parent):  # lexists: a dangling symlink is present, not missing
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:  # a concurrent sampler made it first: not ours, so its mode is left alone
            continue
        os.chmod(directory, 0o700)
    _check_owned(owned_levels, existing_only=False)  # every level now exists: re-check what was created
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


def _state_levels(directory):
    """The state dir and every level under it down to directory (require_owned is for our own tree only)."""
    if ".." in Path(directory).parts:  # relative_to is lexical: '..' would pass it and escape the tree
        raise PermissionError(f"{directory} leaves the state dir via '..'")
    # normpath, not resolve(): resolving would follow the symlinks _check_owned exists to refuse
    root = Path(os.path.normpath(state_dir()))
    directory = Path(os.path.normpath(directory))
    try:
        rel = directory.relative_to(root)
    except ValueError:
        raise PermissionError(f"{directory} is not under the state dir {root}") from None
    levels = [root]
    for part in rel.parts:
        levels.append(levels[-1] / part)
    return levels


def _check_owned(levels, existing_only):
    """lstat each level: a symlink, a non-directory, another user's directory, or one group/other can
    write (someone else could swap entries in it) anywhere in the tree is refused."""
    for level in levels:
        if existing_only and not os.path.lexists(level):
            continue
        info = os.lstat(level)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"{level} is not a directory owned by this user")
        if info.st_mode & 0o022:
            raise PermissionError(f"{level} is writable by group or others")


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
