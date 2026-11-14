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
  tokens   = min(input tokens + max_tokens, context_tokens)
  reserved = tokens * kv_bytes_per_token + per_seq_state_bytes + prefill_margin_bytes
input tokens (`input_tokens()`), never an average-case estimate, and never the backend's count alone:
  counted  the backend's own count (vllm-mlx POST /v1/messages/count_tokens, the gate asks it on loopback) plus the
           UTF-8 bytes of what vllm-mlx 0.6.0's count leaves out but its adapter sends (uncounted_bytes: tool_use names
           and ids, tool_result ids and string items, system string entries) plus template_tokens() for the chat
           template's markers and tool preamble; never more than the bound below;
  bound    when there is no count (Ollama, no endpoint, an error, a timeout): the UTF-8
           bytes of the text the model will see (input_bound_bytes) plus template_tokens(). A byte-level BPE token
           covers at least one byte, so bytes >= tokens whatever the text: token-dense input (`a!a!...`, 1 byte per
           token on Qwen's tokenizer) cannot be under-reserved. Prose and code run ~4 bytes per token, so the bound
           reserves ~4x what the count does and lowers concurrency, but only while the count is failing.
and a request is admitted only while the reservations in flight plus its own fit in
  memory_budget_bytes - weights_bytes.
Admission is two-phase (Admission.acquire_slot, then Ticket.reserve): a COUNT slot before the token count is asked,
the bytes after it, the slot given back if they do not fit. A request a text-only model cannot read (non_text_content:
an image, a document) is refused by the gate before either.
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

import json
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
# Template tokens no count or byte sum sees, added to the count and to the byte bound alike. vllm-mlx 0.6.0's
# count_tokens encodes the text, tool inputs, tool results and tool definitions one by one, without the chat template
# around them (server.count_anthropic_tokens). Qwen3's template adds <|im_start|>role / <|im_end|> per message (~5
# tokens), <tool_call> / <tool_response> wrappers and the tool's name per block (~10-20), the OpenAI-shaped
# {"type": "function", "function": ...} wrapper per tool definition (~20) and a fixed tool-use preamble (~300). Each
# allowance is about double that: 1024 + 16 per message + 32 per block + 64 per tool. At 64 KiB per token the fixed
# part reserves 64 MiB.
TEMPLATE_TOKENS_FIXED = 1024
TEMPLATE_TOKENS_PER_MESSAGE = 16
TEMPLATE_TOKENS_PER_BLOCK = 32
TEMPLATE_TOKENS_PER_TOOL = 64
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


def _utf8(s: Any) -> int:
    return len(s.encode("utf-8", "surrogatepass")) if isinstance(s, str) else 0


def _json_bytes(v: Any) -> int:
    """JSON as the backend renders a tool input or definition (json.dumps, ASCII-escaped: never shorter than the
    UTF-8 text it stands for)."""
    try:
        return len(json.dumps(v))
    except (TypeError, ValueError, RecursionError):
        return len(repr(v).encode("utf-8", "surrogatepass"))


def _content_bytes(content: Any) -> int:
    """The bytes of a system prompt, message content or tool_result content the model will see. Text, thinking,
    tool_use (name + input JSON) and tool_result (its text) by their text; an image by its source (base64 data or URL,
    far more bytes than the image's tokens); any other block by its whole JSON, so nothing is ever left out."""
    if content is None:
        return 0
    if isinstance(content, str):
        return _utf8(content)
    if not isinstance(content, list):
        return _json_bytes(content)
    total = 0
    for block in content:
        if isinstance(block, str):
            total += _utf8(block)
        elif not isinstance(block, dict):
            total += _json_bytes(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            total += _utf8(block["text"])
        elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            total += _utf8(block["thinking"])        # vllm-mlx 0.6.0 drops past thinking; counted all the same
        elif block.get("type") == "tool_use":
            total += _utf8(block.get("name")) + _utf8(block.get("id")) + _json_bytes(block.get("input") or {})
        elif block.get("type") == "tool_result":
            total += _utf8(block.get("tool_use_id")) + _content_bytes(block.get("content"))
        elif block.get("type") == "image":
            total += _json_bytes(block.get("source"))
        else:
            total += _json_bytes(block)
    return total


def _messages(body: dict[str, Any]) -> list:
    m = body.get("messages")
    return m if isinstance(m, list) else []


def _tools(body: dict[str, Any]) -> list:
    t = body.get("tools")
    return t if isinstance(t, list) else []


def template_tokens(body: dict[str, Any]) -> int:
    """The template allowance for a request (TEMPLATE_TOKENS_*): fixed, per message, per content block, per tool."""
    msgs = _messages(body)
    blocks = sum(len(m["content"]) for m in msgs if isinstance(m, dict) and isinstance(m.get("content"), list))
    system = body.get("system")
    blocks += len(system) if isinstance(system, list) else 0
    return (TEMPLATE_TOKENS_FIXED + TEMPLATE_TOKENS_PER_MESSAGE * len(msgs) + TEMPLATE_TOKENS_PER_BLOCK * blocks
            + TEMPLATE_TOKENS_PER_TOOL * len(_tools(body)))


def input_bound_bytes(body: dict[str, Any]) -> int:
    """The UTF-8 bytes of the text the model will see: system, every message's content (_content_bytes) and every tool
    definition's JSON. A hard upper bound on its tokens (a BPE token covers at least one byte)."""
    total = _content_bytes(body.get("system"))
    for m in _messages(body):
        total += _content_bytes(m.get("content")) if isinstance(m, dict) else _json_bytes(m)
    return total + sum(_json_bytes(t) for t in _tools(body))


# The content block types a text-only model can take (message content): text, past thinking (vllm-mlx 0.6.0 drops it)
# and the tool blocks. Inside a tool_result's content and in a system list: text blocks and plain strings only.
TEXT_BLOCKS = ("text", "thinking", "redacted_thinking", "tool_use", "tool_result")


def _text_only_list(items: Any) -> bool:
    """A system list or a tool_result's content list holds text only (text blocks or strings)."""
    return all(isinstance(i, str) or (isinstance(i, dict) and i.get("type") == "text") for i in items)


def non_text_content(body: dict[str, Any]) -> str | None:
    """What in the request a TEXT-ONLY model cannot see, or None. The Pro's vllm-mlx serves the 27B with its vision
    tower excluded: an image (top level or inside a tool_result), a document or any other non-text block would be
    dropped by vllm-mlx 0.6.0's adapter (or turned into image input the loaded model has no tower for), and the model
    would answer as if it had seen it. The gate refuses such a turn non-retryably (local_gate.UNSUPPORTED_MESSAGE) so
    the provider routes it elsewhere."""
    system = body.get("system")
    if isinstance(system, list) and not _text_only_list(system):
        return "non-text system block"
    for i, m in enumerate(_messages(body)):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            kind = b.get("type") if isinstance(b, dict) else None
            if kind not in TEXT_BLOCKS:
                name = kind if isinstance(kind, str) and kind.isidentifier() and len(kind) <= 40 else "non-text"
                return f"{name} block in message {i + 1}"
            if kind == "tool_result" and isinstance(b.get("content"), list) and not _text_only_list(b["content"]):
                return f"non-text tool_result content in message {i + 1}"
    return None


# What vllm-mlx's adapter (api/anthropic_adapter.anthropic_to_openai) sends for a tool defined without input_schema.
_DEFAULT_SCHEMA_BYTES = len(json.dumps({"type": "object", "properties": {}}))


def uncounted_bytes(body: dict[str, Any]) -> int:
    """The UTF-8 bytes of what the model will see that vllm-mlx 0.6.0's count leaves out. Read from its source
    (server.count_anthropic_tokens against api/anthropic_adapter): the count encodes a system string, every system
    block's "text", a message string, every content block's "text", a truthy "input" (as JSON), a tool_result's string
    content or its dict items' "text", and each tool's name, description and input_schema. The adapter sends, besides:

      * a tool_use block's name and id (the assistant's tool call) and its input as "{}" when empty;
      * a tool_result block's tool_use_id, and plain STRING items of its content list (joined into the tool message);
      * plain string entries of a system list;
      * the default {"type": "object", "properties": {}} of a tool without input_schema.

    A tool_result whose content is neither a string nor a list (vllm-mlx's model refuses it, but it is counted all the
    same) and any block the count does not know are added whole. Past thinking is neither counted nor sent (the adapter
    drops it). Bytes >= tokens (input_bound_bytes), so adding these bytes to the count can only over-reserve."""
    total = 0
    system = body.get("system")
    if isinstance(system, list):
        for s in system:
            if isinstance(s, str):
                total += _utf8(s)
            elif not isinstance(s, dict) or not (isinstance(s.get("text"), str) and s["text"]):
                total += _json_bytes(s)
    for m in _messages(body):
        if not isinstance(m, dict):
            total += _json_bytes(m)
            continue
        content = m.get("content")
        if isinstance(content, str) or content is None:
            continue
        if not isinstance(content, list):
            total += _json_bytes(content)
            continue
        for b in content:
            if not isinstance(b, dict):
                total += _utf8(b) if isinstance(b, str) else _json_bytes(b)
                continue
            kind = b.get("type")
            if kind == "text" or kind in ("thinking", "redacted_thinking"):
                continue                     # text counted; thinking dropped by the adapter
            if kind == "tool_use":
                total += _utf8(b.get("name")) + _utf8(b.get("id")) + (0 if b.get("input") else 2)
            elif kind == "tool_result":
                total += _utf8(b.get("tool_use_id"))
                sub = b.get("content")
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, str):
                            total += _utf8(item)
                        elif not (isinstance(item, dict) and isinstance(item.get("text"), str)):
                            total += _json_bytes(item)
                elif sub is not None and not isinstance(sub, str):
                    total += len(str(sub).encode("utf-8", "surrogatepass"))
            else:
                total += _json_bytes(b)      # an image, a document, anything unknown: the whole block
    for t in _tools(body):
        if isinstance(t, dict) and not t.get("input_schema"):
            total += _DEFAULT_SCHEMA_BYTES
    return total


def input_tokens(body: Any, counted: Any = None) -> tuple[int, str]:
    """(input tokens, "counted" | "bound") for a request (module docstring). The backend's count is never trusted
    alone: with `counted` a non-negative integer, the input is
        counted + uncounted_bytes(body) + template_tokens(body)
    (what the count saw, the bytes of what it left out, the template), never more than the byte bound (a hard upper
    bound by itself); without one, the byte bound plus the allowance. `body` is the parsed request; anything else (bytes
    that are not a JSON object) is bounded by its own length."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            return len(body), "bound"
        if not isinstance(body, dict):
            return len(json.dumps(body)), "bound"
    if not isinstance(body, dict):
        return _json_bytes(body), "bound"
    allowance = template_tokens(body)
    bound = input_bound_bytes(body) + allowance
    if isinstance(counted, int) and not isinstance(counted, bool) and counted >= 0:
        return min(bound, counted + uncounted_bytes(body) + allowance), "counted"
    return bound, "bound"


def request_tokens(cfg: dict[str, Any], input_tokens: int, max_tokens: Any) -> int:
    """The context a request will hold: its input tokens (input_tokens(): exact or a hard bound) plus the output it may
    write (max_tokens; default_max_tokens when absent or not a positive integer), capped at context_tokens."""
    out = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0 \
        else cfg["default_max_tokens"]
    return min(cfg["context_tokens"], max(0, int(input_tokens)) + out)


def request_bytes(cfg: dict[str, Any], input_tokens: int, max_tokens: Any) -> int:
    """request_tokens as a reservation in bytes (module docstring)."""
    return tokens_bytes(cfg, request_tokens(cfg, input_tokens, max_tokens))


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
    """One request's hold on the admission: a COUNT slot (acquire_slot) and the bytes it reserved (0 until reserve()).
    release() gives both back, once (a second call, from a later finally or error path, does nothing)."""

    def __init__(self, admission: "Admission", reserved: int):
        self._admission, self.reserved, self._done = admission, reserved, False

    def reserve(self, nbytes: int) -> tuple[bool, dict[str, Any]]:
        """Add the request's byte reservation to its slot: (True, report) when it fits; otherwise (False, report with
        "refused": "busy" | "too_large") and the slot is released here (Admission._reserve)."""
        return self._admission._reserve(self, nbytes)

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

    def request_bytes(self, input_tokens: int, max_tokens: Any) -> int:
        """The reservation a request asks for (0 without a governor: nothing is budgeted)."""
        return request_bytes(self.governor.cfg, input_tokens, max_tokens) if self.governor else 0

    def acquire_slot(self) -> tuple[Ticket | None, dict[str, Any]]:
        """Phase one: a COUNT slot holding no bytes yet, (ticket, report), or (None, report with "refused": "busy")
        when the count limit is reached. The gate takes it BEFORE asking the backend for the token count, so no more
        counts run than turns may (a count is synchronous tokenization on vllm-mlx's event loop), then calls
        ticket.reserve(bytes) with the counted size. No governor: always a slot."""
        g = self.governor.current() if self.governor else NO_GOVERNOR
        with self._lock:
            limit = g["limit"] if self.governor is not None else None
            if limit is not None and self.inflight >= limit:
                return None, {**self._report(g), "refused": "busy"}
            self.inflight += 1
            return Ticket(self, 0), self._report(g)

    def _reserve(self, ticket: Ticket, nbytes: int) -> tuple[bool, dict[str, Any]]:
        """Phase two (Ticket.reserve): the bytes beside the reservations in flight. Refused: "too_large" (more than
        the whole budget beside the weights: never) or "busy" (not now); either way the slot is given back here."""
        g = self.governor.current() if self.governor else NO_GOVERNOR
        with self._lock:
            if ticket._done:                 # released meanwhile (never in the gate): nothing to add to
                return False, {**self._report(g), "refused": "busy"}
            if self.governor is None:
                return True, self._report(g)
            nbytes = max(0, int(nbytes))
            avail = available_bytes(self.governor.cfg)
            refused = None
            if nbytes > avail:
                refused = {"refused": "too_large", "reason": f"request needs {_gib(nbytes)} GiB, more than the "
                                                             f"{_gib(avail)} GiB budget beside the weights"}
            elif self.reserved + nbytes > avail:
                refused = {"refused": "busy", "reason": f"memory budget: {_gib(avail - self.reserved)} GiB free, "
                                                        f"request needs {_gib(nbytes)} GiB"}
            if refused is not None:
                self._release_locked(ticket)
                return False, {**self._report(g), **refused, "request_bytes": nbytes}
            ticket.reserved += nbytes
            self.reserved += nbytes
            return True, self._report(g)

    def try_acquire(self, reserve: int = 0) -> tuple[Ticket | None, dict[str, Any]]:
        """Both phases at once: (ticket, report) when admitted; (None, report) when not, report["refused"] saying why:
        "busy" (the count limit or the bytes left; retryable) or "too_large" (more than the whole budget beside the
        weights: never). A request that could never fit is "too_large" even when the count is full."""
        if self.governor is not None and max(0, int(reserve)) > available_bytes(self.governor.cfg):
            g = self.governor.current()
            nbytes, avail = max(0, int(reserve)), available_bytes(self.governor.cfg)
            with self._lock:
                return None, {**self._report(g), "refused": "too_large", "request_bytes": nbytes,
                              "reason": f"request needs {_gib(nbytes)} GiB, more than the {_gib(avail)} GiB budget "
                                        "beside the weights"}
        ticket, report = self.acquire_slot()
        if ticket is None:
            return None, {**report, "request_bytes": max(0, int(reserve))}
        ok, report = ticket.reserve(reserve)
        return (ticket if ok else None), report

    def _release_locked(self, ticket: Ticket) -> None:
        if ticket._done:
            return
        ticket._done = True
        self.inflight = max(0, self.inflight - 1)
        self.reserved = max(0, self.reserved - ticket.reserved)

    def _release(self, ticket: Ticket) -> None:
        with self._lock:
            self._release_locked(ticket)

    @property
    def retry_after_s(self) -> int:
        return int(self.governor.cfg["retry_after_s"]) if self.governor else DEFAULTS["retry_after_s"]
