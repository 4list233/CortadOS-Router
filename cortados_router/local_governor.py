"""Activity governor for a local model node (the Pro): how many local-model requests may run at once, from what the
owner is doing and how much memory the machine has left. Runs inside the local gate (local_gate.py), which enforces it
as an admission count on POST /v1/messages and reports it read-only on GET /api/tags and the switch status.

Inputs (sampled at most every `sample_s`, never per request):
  * owner idle seconds: IOHIDSystem's HIDIdleTime (`ioreg -r -c IOHIDSystem -d 1`, nanoseconds since the last keyboard
    or mouse event). Unreadable counts as ACTIVE: the safe side is the owner's own machine staying responsive.
  * memory pressure: `sysctl -n kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, 4 critical; the same level
    `memory_pressure` prints). Unreadable counts as `unknown_pressure` (default warn).

Policy (owner-approved, docs/LOCAL-RESOURCES.md "MLX backend"), as `policy()` computes it:

  owner        pressure    limit (requests running at once)
  active       normal      min(active_cap, idle limit)               default 2
  idle         normal      idle limit = min(idle_cap, memory cap)    default 8 (4-bit model)
  any          warn        min(the above, warn_cap)                  default 1
  any          critical    min(the above, critical_cap)              default 0: nothing new is admitted
  memory cap = floor((memory_budget_gib - weights_gib) / per_request_gib)        default floor((40-15)/2) = 12

The limit bounds ADMISSION only: a request already running is never stopped. When the owner comes back with 6 running,
the limit drops to 2 and nothing new is admitted until fewer than 2 run; the 6 finish on their own. Stdlib only.
"""
from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from typing import Any, Callable

# Every threshold, its default and the reason for it. The example config (tables/local-backend.example.json) repeats
# these reasons next to each value under governor._reasons; a test keeps the two in step.
DEFAULTS: dict[str, Any] = {
    # 10 minutes without keyboard or mouse: the owner has stepped away (a pause to read or think is shorter). Below it
    # the owner is "active" and the laptop must stay responsive.
    "active_idle_s": 600,
    # Owner active: 2 at once. Measured on the Pro (4-bit, ~8.5k-token prompts): 2 requests give 47.8 tok/s aggregate
    # at 23.9 tok/s each, near single-request speed, with ~4 GB more memory than one; 4 already halves per-request speed.
    "active_cap": 2,
    # Owner idle, pressure normal: the per-model idle cap (a model entry's idle_cap overrides it). 8 is the measured
    # aggregate peak for the 4-bit model (87.9 tok/s; 16 fell to 64.8) at a 31 GB peak footprint, no swap growth.
    "idle_cap": 8,
    # Memory pressure warn: macOS is compressing and about to swap; hold at 1 running so the node drains to one
    # request instead of piling KV cache on a machine already short of memory.
    "warn_cap": 1,
    # Memory pressure critical: admit nothing new; the running requests finish (killing them would waste their work
    # and free memory no sooner than letting them end).
    "critical_cap": 0,
    # The memory budget for the backend: 40 GB of the Pro's 48 GB unified memory, leaving ~8 GB for macOS and the
    # owner's apps. The idle cap is also bounded by (budget - weights) / per_request.
    "memory_budget_gib": 40.0,
    # The model's resident weights (a model entry's weights_gib overrides it): the 4-bit 27B is 15 GB on disk.
    "weights_gib": 15.0,
    # Memory per running request (a model entry's per_request_gib overrides it): measured 31 GB peak at 8 requests
    # of ~8.5k-token prompts with 15 GB weights, (31 - 15) / 8 = 2 GB each. Longer prompts take more KV cache; the
    # pressure signal covers what this average does not.
    "per_request_gib": 2.0,
    # Sampling period: the signals change on a scale of minutes (idle) or seconds (pressure); sampling every 5 s costs
    # two tiny subprocesses and never one per request.
    "sample_s": 5.0,
    # Retry-After on a refused (over-limit) request: the next sample is at most sample_s away, and a slot frees when a
    # request ends (seconds to minutes); 5 s lets a client retry soon without hammering the gate.
    "retry_after_s": 5,
    # An unreadable pressure level counts as warn: the node keeps serving, one at a time, rather than either stopping
    # (critical) or trusting an unknown memory state (normal).
    "unknown_pressure": "warn",
}
# The keys a model entry may override (per-model sizing: a 6-bit copy has heavier weights and a different cap).
MODEL_KEYS = ("idle_cap", "weights_gib", "per_request_gib")
INT_KEYS = ("active_idle_s", "active_cap", "idle_cap", "warn_cap", "critical_cap", "retry_after_s")
FLOAT_KEYS = ("memory_budget_gib", "weights_gib", "per_request_gib", "sample_s")
PRESSURES = ("normal", "warn", "critical")
PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}
SIGNAL_TIMEOUT_S = 2.0                     # each signal command; ioreg and sysctl answer in milliseconds


class GovernorConfigError(ValueError):
    pass


def check_config(raw: dict[str, Any] | None, model: dict[str, Any] | None = None) -> dict[str, Any]:
    """The governor settings: DEFAULTS, then the config's `governor` section, then the model entry's MODEL_KEYS.
    Unknown keys and bad values are refused (a threshold silently ignored would govern by a number nobody chose)."""
    raw = dict(raw or {})
    model = model or {}
    out = dict(DEFAULTS)
    for key, v in raw.items():
        if key.startswith("_"):
            continue
        if key not in DEFAULTS:
            raise GovernorConfigError(f"governor: unknown key {key!r}")
        out[key] = v
    for key in MODEL_KEYS:
        if key in model:
            out[key] = model[key]
    for key in INT_KEYS:
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise GovernorConfigError(f"governor: {key} must be a non-negative integer, got {v!r}")
    for key in FLOAT_KEYS:
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GovernorConfigError(f"governor: {key} must be a positive number, got {v!r}")
    if out["unknown_pressure"] not in PRESSURES:
        raise GovernorConfigError(f"governor: unknown_pressure must be one of {', '.join(PRESSURES)}")
    if out["idle_cap"] < 1:
        raise GovernorConfigError("governor: idle_cap must be at least 1")
    return out


def memory_cap(cfg: dict[str, Any]) -> int:
    """How many requests fit the memory budget beside the weights (never negative)."""
    return max(0, int((cfg["memory_budget_gib"] - cfg["weights_gib"]) // cfg["per_request_gib"]))


def policy(cfg: dict[str, Any], idle_s: float | None, pressure: str | None) -> dict[str, Any]:
    """The admission limit for one sample: {"limit", "owner", "pressure", "reason"}. Pure (module docstring table).
    idle_s None (unreadable) is an active owner; pressure None or unknown is cfg["unknown_pressure"]."""
    idle_limit = min(cfg["idle_cap"], memory_cap(cfg))
    owner = "idle" if idle_s is not None and idle_s >= cfg["active_idle_s"] else "active"
    if owner == "active":
        limit = min(cfg["active_cap"], idle_limit)
        reason = "owner active" if idle_s is not None else "owner activity unknown (counted active)"
    else:
        limit = idle_limit
        reason = "owner idle" + (" (memory budget caps it)" if memory_cap(cfg) < cfg["idle_cap"] else "")
    known = pressure in PRESSURES
    p = pressure if known else cfg["unknown_pressure"]
    if p == "critical":
        limit = min(limit, cfg["critical_cap"])
        reason = "memory pressure critical" if known else "memory pressure unknown (counted critical)"
    elif p == "warn":
        limit = min(limit, cfg["warn_cap"])
        reason = "memory pressure warn" if known else "memory pressure unknown (counted warn)"
    return {"limit": limit, "owner": owner, "pressure": pressure if known else "unknown", "reason": reason}


Runner = Callable[..., Any]


def _run(argv: list[str], run: Runner) -> str | None:
    try:
        r = run(argv, capture_output=True, text=True, timeout=SIGNAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if getattr(r, "returncode", 1) == 0 and isinstance(r.stdout, str) else None


def read_idle_s(run: Runner = subprocess.run) -> float | None:
    """Seconds since the last keyboard/mouse event (HIDIdleTime is nanoseconds), or None when unreadable."""
    out = _run(["/usr/sbin/ioreg", "-r", "-c", "IOHIDSystem", "-d", "1"], run)
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out or "")
    return int(m.group(1)) / 1e9 if m else None


def read_pressure(run: Runner = subprocess.run) -> str | None:
    """"normal" | "warn" | "critical" from kern.memorystatus_vm_pressure_level, or None when unreadable."""
    out = _run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], run)
    try:
        return PRESSURE_LEVELS.get(int((out or "").strip()))
    except ValueError:
        return None


class Governor:
    """The sampled policy: current() is the latest sample, re-read when it is older than sample_s. The readers and
    clock are seams for tests (no real ioreg/sysctl there)."""

    def __init__(self, cfg: dict[str, Any], *, idle_reader: Callable[[], float | None] = read_idle_s,
                 pressure_reader: Callable[[], str | None] = read_pressure,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self._idle, self._pressure, self._clock = idle_reader, pressure_reader, clock
        self._lock = threading.Lock()
        self._sample: dict[str, Any] | None = None
        self._at = 0.0

    def current(self) -> dict[str, Any]:
        with self._lock:                 # one sampler at a time; others wait milliseconds for its answer
            now = self._clock()
            if self._sample is None or now - self._at >= self.cfg["sample_s"]:
                self._sample = policy(self.cfg, self._idle(), self._pressure())
                self._at = now
            return dict(self._sample)


class Admission:
    """The gate's admission count: try_acquire() admits a request while fewer than the governor's limit run. No
    governor (None) is unlimited, today's behaviour for an Ollama node. A refused request is never queued here: the
    gate answers it at once with a retryable 529 and the provider routes elsewhere or retries."""

    def __init__(self, governor: Governor | None):
        self.governor = governor
        self._lock = threading.Lock()
        self.inflight = 0

    def snapshot(self) -> dict[str, Any]:
        """{"limit": int | None, "inflight": int, "owner", "pressure", "reason"}: what the gate reports."""
        g = self.governor.current() if self.governor else {"limit": None, "owner": None, "pressure": None,
                                                            "reason": "no governor"}
        with self._lock:
            return {**g, "inflight": self.inflight}

    def try_acquire(self) -> tuple[bool, dict[str, Any]]:
        g = self.governor.current() if self.governor else None
        with self._lock:
            limit = g["limit"] if g else None
            if limit is not None and self.inflight >= limit:
                return False, {**g, "inflight": self.inflight}
            self.inflight += 1
            return True, {**(g or {"limit": None, "owner": None, "pressure": None, "reason": "no governor"}),
                          "inflight": self.inflight}

    def release(self) -> None:
        with self._lock:
            self.inflight = max(0, self.inflight - 1)

    @property
    def retry_after_s(self) -> int:
        return int(self.governor.cfg["retry_after_s"]) if self.governor else DEFAULTS["retry_after_s"]
