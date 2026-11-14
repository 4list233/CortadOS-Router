"""Activity governor for a local model node (the Pro): which local-model requests may run at once, from what the owner
is doing, how much memory the machine has left, and how much KV cache each request will hold. Runs inside the local
gate (local_gate.py), which enforces it on POST /v1/messages and reports it read-only on GET /api/tags and the switch
status.

Inputs (sampled at most every `sample_s`, never per request):
  * owner idle seconds: IOHIDSystem's HIDIdleTime (`ioreg -r -c IOHIDSystem -d 1`, nanoseconds since the last keyboard
    or mouse event). Unreadable counts as ACTIVE: the safe side is the owner's own machine staying responsive.
  * memory pressure: `sysctl -n kern.memorystatus_vm_pressure_level` (1 normal, 2 warn, 4 critical; the same level
    `memory_pressure` prints). Unreadable counts as `unknown_pressure` (default warn).

Two bounds, both must hold for a request to be admitted (docs/LOCAL-RESOURCES.md "MLX backend"):

COUNT (owner-approved policy, as `policy()` computes it): how many requests may run at once.
  owner        pressure    limit
  active       normal      min(active_cap, idle_cap)                 default 2
  idle         normal      idle_cap (per model)                      default 8 (4-bit model)
  any          warn        min(the above, warn_cap)                  default 1
  any          critical    min(the above, critical_cap)              default 0: nothing new is admitted

BYTES (token-aware, `request_bytes()`): each admitted request reserves the memory its context will hold,
  tokens   = min(input tokens + max_tokens, context_tokens)     input tokens ~ request body characters / 3.5
  reserved = tokens * kv_bytes_per_token + per_seq_state_bytes + prefill_margin_bytes
and a request is admitted only while the reservations in flight plus its own fit in
  memory_budget_bytes - weights_bytes.
Defaults (Qwen3.8 27B, 4-bit): 64 KiB per token (16 full-attention layers x K/V x 4 KV heads x head_dim 256 x fp16),
0.15 GiB recurrent state, 1.5 GiB prefill margin: a 128k-token request reserves 9.65 GiB, so two fit beside 15 GiB of
weights in a 40 GiB budget and a third does not, while eight ~9k-token requests (2.2 GiB each) fit.

A request that could not fit even alone (more than budget - weights with nothing else running) is refused for good
(`too_large`): retrying cannot help, so the gate answers it non-retryably and the provider routes it elsewhere. Pressure
stays the safety net: on warn or critical the count caps apply whatever the bytes say.

The bounds gate ADMISSION only: a request already running is never stopped. When the owner comes back with 6 running,
the limit drops to 2 and nothing new is admitted until fewer than 2 run; the 6 finish on their own. Stdlib only.
"""
from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from typing import Any, Callable

GIB = 2 ** 30
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
    # The memory budget for the backend: 40 GiB of the Pro's 48 GB unified memory, leaving ~8 GB for macOS and the
    # owner's apps. Requests are admitted while their reservations fit in (budget - weights).
    "memory_budget_bytes": 40 * GIB,
    # The model's resident weights (a model entry's weights_bytes overrides it): the 4-bit 27B is 15 GB on disk and
    # in memory.
    "weights_bytes": 15 * GIB,
    # KV cache per token of context (per model): Qwen3.8 27B is hybrid; only its 16 full-attention layers keep a KV
    # cache, and vllm-mlx keeps it unquantized: 16 layers x (K and V) x 4 KV heads x head_dim 256 x 2 bytes (fp16)
    # = 65,536 bytes per token, so a 128k-token context holds 8 GiB.
    "kv_bytes_per_token": 65536,
    # Per running sequence, whatever its length (per model): the recurrent (linear-attention) layers' state, about
    # 0.15 GiB per sequence for this model.
    "per_seq_state_bytes": int(0.15 * GIB),
    # Per running sequence (per model): prefill activations and allocator slack beyond the KV cache and state. The
    # 6-bit copy measured 24.7 GB at one ~9k-token request over 22.4 GB idle: 2.3 GB, of which KV + state explain
    # ~0.7 GiB; 1.5 GiB covers the rest. With it a 128k request reserves 9.65 GiB (two fit in 25 GiB, three do not).
    "prefill_margin_bytes": int(1.5 * GIB),
    # The longest context the model is served with (per model): the Pro block's context_length on the provider host
    # (tables/resources.json, 131072). A request's tokens are capped here: the backend holds no more than this.
    "context_tokens": 131072,
    # max_tokens assumed when a request names none: vllm-mlx's own default (vllm_mlx.serve.max_tokens, 32768); the
    # gate passes the configured serve value in.
    "default_max_tokens": 32768,
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
MODEL_KEYS = ("idle_cap", "weights_bytes", "kv_bytes_per_token", "per_seq_state_bytes", "prefill_margin_bytes",
              "context_tokens")
INT_KEYS = ("active_idle_s", "active_cap", "idle_cap", "warn_cap", "critical_cap", "retry_after_s",
            "memory_budget_bytes", "weights_bytes", "kv_bytes_per_token", "per_seq_state_bytes",
            "prefill_margin_bytes", "context_tokens", "default_max_tokens")
POSITIVE_KEYS = ("memory_budget_bytes", "weights_bytes", "kv_bytes_per_token", "context_tokens", "default_max_tokens")
FLOAT_KEYS = ("sample_s",)
# Keys of the count-only governor this replaced: refused by name, so an old config is not run on numbers that no
# longer mean anything (the fixed per-request size could not see a 128k-token turn).
REPLACED = {"per_request_gib": "token-aware admission (kv_bytes_per_token, per_seq_state_bytes, prefill_margin_bytes)",
            "memory_budget_gib": "memory_budget_bytes", "weights_gib": "weights_bytes"}
# Characters per token for the input estimate: English prose and code run ~3.5-4 characters per token on Qwen's
# tokenizer; the lower figure over-counts slightly, the safe side for a memory reservation.
CHARS_PER_TOKEN = 3.5
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
    for where, section in (("governor", raw), ("model entry", model)):
        for key in REPLACED:
            if key in section:
                raise GovernorConfigError(f"{where}: {key} was replaced by {REPLACED[key]} "
                                          "(docs/LOCAL-RESOURCES.md \"MLX backend\")")
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
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (key in POSITIVE_KEYS and v == 0):
            kind = "positive" if key in POSITIVE_KEYS else "non-negative"
            raise GovernorConfigError(f"governor: {key} must be a {kind} integer, got {v!r}")
    for key in FLOAT_KEYS:
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GovernorConfigError(f"governor: {key} must be a positive number, got {v!r}")
    if out["unknown_pressure"] not in PRESSURES:
        raise GovernorConfigError(f"governor: unknown_pressure must be one of {', '.join(PRESSURES)}")
    if out["idle_cap"] < 1:
        raise GovernorConfigError("governor: idle_cap must be at least 1")
    return out


def available_bytes(cfg: dict[str, Any]) -> int:
    """The memory requests may reserve: the budget beside the weights (never negative)."""
    return max(0, cfg["memory_budget_bytes"] - cfg["weights_bytes"])


def tokens_bytes(cfg: dict[str, Any], tokens: int) -> int:
    """The reservation for one request holding `tokens` of context (capped at context_tokens)."""
    tokens = max(0, min(int(tokens), cfg["context_tokens"]))
    return tokens * cfg["kv_bytes_per_token"] + cfg["per_seq_state_bytes"] + cfg["prefill_margin_bytes"]


def bytes_tokens(cfg: dict[str, Any], free: int) -> int:
    """The most tokens of context one more request may hold in `free` bytes (0 when not even the per-sequence part
    fits), capped at context_tokens."""
    room = free - cfg["per_seq_state_bytes"] - cfg["prefill_margin_bytes"]
    return 0 if room < 0 else min(cfg["context_tokens"], room // cfg["kv_bytes_per_token"])


def request_tokens(cfg: dict[str, Any], body_chars: int, max_tokens: Any) -> int:
    """The context a request will hold: its input (body characters / CHARS_PER_TOKEN, rounded up) plus the output it
    may write (max_tokens; default_max_tokens when absent or not a positive integer), capped at context_tokens."""
    out = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0 \
        else cfg["default_max_tokens"]
    return min(cfg["context_tokens"], math.ceil(max(0, body_chars) / CHARS_PER_TOKEN) + out)


def request_bytes(cfg: dict[str, Any], body_chars: int, max_tokens: Any) -> int:
    """request_tokens as a reservation in bytes (module docstring)."""
    return tokens_bytes(cfg, request_tokens(cfg, body_chars, max_tokens))


def max_seqs(cfg: dict[str, Any]) -> int:
    """The most requests that could ever run at once: the idle cap, bounded by how many of the smallest reservation
    (an empty context) fit the byte budget. At least 1 (the backend's own --max-num-seqs)."""
    return max(1, min(cfg["idle_cap"], available_bytes(cfg) // max(1, tokens_bytes(cfg, 0))))


def policy(cfg: dict[str, Any], idle_s: float | None, pressure: str | None) -> dict[str, Any]:
    """The COUNT limit for one sample: {"limit", "owner", "pressure", "reason"}. Pure (module docstring table).
    idle_s None (unreadable) is an active owner; pressure None or unknown is cfg["unknown_pressure"]."""
    owner = "idle" if idle_s is not None and idle_s >= cfg["active_idle_s"] else "active"
    if owner == "active":
        limit = min(cfg["active_cap"], cfg["idle_cap"])
        reason = "owner active" if idle_s is not None else "owner activity unknown (counted active)"
    else:
        limit = cfg["idle_cap"]
        reason = "owner idle"
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


def _gib(n: int) -> str:
    return f"{n / GIB:.1f}"


NO_GOVERNOR = {"limit": None, "owner": None, "pressure": None, "reason": "no governor"}


class Ticket:
    """One admitted request's hold on the admission: release() gives its slot and bytes back, once (a second call,
    from a later finally or error path, does nothing)."""

    def __init__(self, admission: "Admission", reserved: int):
        self._admission, self.reserved, self._done = admission, reserved, False

    def release(self) -> None:
        self._admission._release(self)


class Admission:
    """The gate's admission: try_acquire(reserve) admits a request while fewer than the governor's COUNT limit run AND
    its reservation fits the BYTES left (module docstring). No governor (None) is unlimited, today's behaviour for an
    Ollama node. A refused request is never queued here: the gate answers at once, a retryable 529 when the node is
    full now, a non-retryable refusal when the request could never fit (`too_large`)."""

    def __init__(self, governor: Governor | None):
        self.governor = governor
        self._lock = threading.Lock()
        self.inflight = 0
        self.reserved = 0                    # bytes held by the admitted requests in flight

    def _report(self, g: dict[str, Any]) -> dict[str, Any]:
        """The state report under the lock: the count sample, in flight, and (governed) the bytes and token room."""
        out = {**g, "inflight": self.inflight}
        if self.governor is None:
            return {**out, "reserved_bytes": None, "free_bytes": None, "fit_tokens": None, "free_tokens": None}
        cfg = self.governor.cfg
        free = max(0, available_bytes(cfg) - self.reserved)
        full = g["limit"] is not None and self.inflight >= g["limit"]
        return {**out, "reserved_bytes": self.reserved, "free_bytes": free,
                # the most tokens one request may hold: alone (ever), and beside what runs now (0 when the count is full)
                "fit_tokens": bytes_tokens(cfg, available_bytes(cfg)),
                "free_tokens": 0 if full else bytes_tokens(cfg, free)}

    def snapshot(self) -> dict[str, Any]:
        """{"limit", "inflight", "owner", "pressure", "reason", "reserved_bytes", "free_bytes", "fit_tokens",
        "free_tokens"}: what the gate reports (bytes and tokens None without a governor)."""
        g = self.governor.current() if self.governor else NO_GOVERNOR
        with self._lock:
            return self._report(g)

    def request_bytes(self, body_chars: int, max_tokens: Any) -> int:
        """The reservation a request asks for (0 without a governor: nothing is budgeted)."""
        return request_bytes(self.governor.cfg, body_chars, max_tokens) if self.governor else 0

    def try_acquire(self, reserve: int = 0) -> tuple[Ticket | None, dict[str, Any]]:
        """(ticket, report) when admitted; (None, report) when not, report["refused"] saying why: "busy" (the count
        limit or the bytes left; retryable) or "too_large" (more than the whole budget beside the weights: never)."""
        g = self.governor.current() if self.governor else NO_GOVERNOR
        with self._lock:
            if self.governor is not None:
                reserve = max(0, int(reserve))
                avail = available_bytes(self.governor.cfg)
                if reserve > avail:
                    return None, {**self._report(g), "refused": "too_large", "request_bytes": reserve,
                                  "reason": f"request needs {_gib(reserve)} GiB, more than the {_gib(avail)} GiB "
                                            "budget beside the weights"}
                limit = g["limit"]
                if limit is not None and self.inflight >= limit:
                    return None, {**self._report(g), "refused": "busy", "request_bytes": reserve}
                if self.reserved + reserve > avail:
                    return None, {**self._report(g), "refused": "busy", "request_bytes": reserve,
                                  "reason": f"memory budget: {_gib(avail - self.reserved)} GiB free, request needs "
                                            f"{_gib(reserve)} GiB"}
            else:
                reserve = 0
            self.inflight += 1
            self.reserved += reserve
            return Ticket(self, reserve), self._report(g)

    def _release(self, ticket: Ticket) -> None:
        with self._lock:
            if ticket._done:
                return
            ticket._done = True
            self.inflight = max(0, self.inflight - 1)
            self.reserved = max(0, self.reserved - ticket.reserved)

    @property
    def retry_after_s(self) -> int:
        return int(self.governor.cfg["retry_after_s"]) if self.governor else DEFAULTS["retry_after_s"]
