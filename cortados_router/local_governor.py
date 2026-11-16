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
input tokens (`input_tokens()`) are an upper bound, never an estimate and never a backend count:
  bytes of the request body exactly as received + template_tokens() + reserialized_bytes() + normalization_bytes(),
  capped at context_tokens. Every character the model sees from the body costs at least one byte there and a
  byte-level BPE token covers at least one byte of the NFC-normalized text, so the bound holds whatever the text
  (token-dense `a!a!...` included); the template allowance and reserialized_bytes cover the only text that is not a
  copy from the body, normalization_bytes what the tokenizer's NFC normalizer writes wider (docs/LOCAL-RESOURCES.md
  "The input bound"). Code-heavy turns run 3.5-4.5 body bytes per token, so long turns reserve the whole context.
  The cap is enforced by the backend itself: vllm-mlx runs under integrations/vllm-mlx/cortados_vllm_mlx_guard.py,
  which refuses a prompt whose exact token count + max_tokens exceeds context_tokens before it is scheduled.
and a request is admitted only while the reservations in flight plus its own fit in
  memory_budget_bytes - weights_bytes.
The count slot and the bytes are taken together (Admission.try_acquire). A request a text-only model cannot read
(non_text_content: an image, a document) is refused by the gate before admission.

IMAGES (#125, a model entry with a `vision` block: check_vision): a vision model takes STILL images within its policy
(image_policy: count, inline base64 only, type, encoded bytes, real file type, header dimensions and pixels, user
messages only, never inside a tool_result, which vllm-mlx 0.6.0's adapter would drop). An image-bearing request reserves
  tokens   = min(text input tokens + images x image_tokens_max + max_tokens, image_context_tokens)
  reserved = tokens * kv_bytes_per_token + per_seq_state_bytes + image_prefill_bytes(tokens, images)
where the text input bound is input_tokens() with the validated image data's bytes left out (the base64 is not text the
model reads; every other byte of the body still counts), and image_tokens_max bounds each image's tokens from above (the
guard sets the processor's max_pixels to it). image_prefill_bytes is the computed worst case of the guard's chunked media
prefill (its docstring has the formula and every input; docs/LOCAL-RESOURCES.md "Vision" the reasoning), not a fixed
margin. It also takes one of image_concurrency image slots, in the same atomic step as the count slot and the bytes.
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
import sys
import threading
import time
import unicodedata
from typing import Any, Callable

from cortados_router import image_check

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
# Template tokens: the only prompt text that does not come from the request body. Qwen3's chat template adds
# <|im_start|>role / <|im_end|> per message (~5 tokens), <tool_call> / <tool_response> wrappers and the tool's name per
# block (~10-20), the OpenAI-shaped {"type": "function", "function": ...} wrapper per tool definition (~20) and a fixed
# tool-use preamble (~300). Each allowance is about double that: 1024 + 16 per message + 32 per block + 64 per tool. At
# 64 KiB per token the fixed part reserves 64 MiB. Counted as tokens, the allowance is also at least as many BYTES as the
# template text, so it stays an upper bound under "a token covers at least one byte".
TEMPLATE_TOKENS_FIXED = 1024
TEMPLATE_TOKENS_PER_MESSAGE = 16
TEMPLATE_TOKENS_PER_BLOCK = 32
TEMPLATE_TOKENS_PER_TOOL = 64
# Instruction text vllm-mlx 0.6.0 writes into the system prompt from outside the body: the response_format instruction
# (api/tool_calling.build_json_system_prompt, ~480 bytes at its longest) and the forced tool_choice one (server, ~190
# bytes besides the tool's name). Rounded up like the template allowance.
INJECTED_TEXT_BYTES = 1024
# Unicode normalization. The deployed tokenizer's family (the public Qwen/Qwen3.5-27B tokenizer.json, and Qwen3's)
# normalizes with NFC before its byte-level BPE, and NFC can widen text: U+FA6C (3 bytes) becomes U+242EE (4 bytes),
# U+1D160 (4 bytes) three code points (12 bytes). The bound measures each body string's NFD form instead, which is
# never shorter than NFC in UTF-8 (a canonical composition replaces two code points by one no longer than the two:
# checked over the whole Unicode table by the tests) and, unlike NFC, is per character, so strings measured apart add
# up to the prompt they are rendered into. A code point this Python's Unicode table does not know (unassigned here,
# perhaps assigned in the tokenizer's newer table) counts UNASSIGNED_EXCESS more bytes: at most 4 code points of 4
# bytes after decomposition.
UNASSIGNED_EXCESS = 16
NORMALIZE_CHUNK = 65536                    # characters measured at a time, so a 32 MiB string stops near the cap
_UNASSIGNED: re.Pattern | None = None
PRESSURES = ("normal", "warn", "critical")
PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}
SIGNAL_TIMEOUT_S = 2.0                     # each signal command; ioreg and sysctl answer in milliseconds


class GovernorConfigError(ValueError):
    pass


# A vision model's still-image policy (#125, owner decisions 2026-11-15/16), each a model entry's `vision` key. Initial
# values until measured; tables/local-backend.example.json repeats each reason next to its value.
VISION_DEFAULTS: dict[str, int] = {
    # Up to 5 still images per request.
    "max_images": 5,
    # Each image at most 1,024 language-model tokens: Qwen3.5's 16-pixel patches merged 2x2 make 1,024 pixels per
    # token, so ~1 megapixel after the processor's resize (the guard sets the processor's max_pixels to
    # image_tokens_max x 1,024; upstream allows 16.7 MP = 16,384 tokens and 1 GiB of KV cache per image).
    "image_tokens_max": 1024,
    # An image-bearing request is capped at a 64k combined context (input + max_tokens) until measured; the guard
    # refuses a longer one exactly, after counting its image tokens.
    "image_context_tokens": 65536,
    # One image-bearing request at a time until measured (vision encoding is the unmeasured peak).
    "image_concurrency": 1,
    # Encoded bytes per image (decoded from base64): the Anthropic API's own 5 MB per image, so a turn the cloud takes
    # fits here too.
    "max_image_bytes": 5 * 2 ** 20,
    # Decoded pixels per image, from the file header before anything is decoded (a decompression bomb is refused from
    # its header): 32 Mi pixels holds an 8K screenshot (7680 x 4320); the guard sets PIL's bomb limit to the same.
    "max_image_pixels": 32 * 2 ** 20,
    # ---- image_prefill_bytes(): the computed worst case of the guard's chunked media prefill (#125 review round 2).
    # The guard prefills an image-bearing prompt in chunks of this many tokens, projecting only the last position to
    # the vocabulary (cortados_vllm_mlx_guard.py "Media prefill"); the guard gets the same number
    # (--image-prefill-tokens). The full-attention head_dim 256 is outside MLX's fused attention kernel (mlx-vlm
    # 0.7.12 models/base.py ensure_fused_sdpa: 64/80/128), so each chunk materializes heads x chunk x context scores:
    # 256 keeps them at 3 GiB at the 64k image context (1,024 would be 12 GiB).
    "prefill_chunk_tokens": 256,
    # Score-sized buffers counted per chunk: the q.k^T product, the causal-masked copy, the softmax output, and one
    # more for the allocator holding the previous layer's buffer until it is reused.
    "score_buffers": 4,
    # Bytes per activation element: the model computes in bfloat16 (config.json's dtype; the guard refuses a wider one).
    "act_bytes": 2,
    # The model's dimensions (config.json; the guard refuses to start vision mode when they differ, --image-dims):
    # Qwen3.8 27B (qwen3_5) text_config, and its vision_config's widths.
    "hidden_size": 5120,
    "intermediate_size": 17408,
    "num_attention_heads": 24,
    "num_key_value_heads": 4,
    "head_dim": 256,
    "linear_num_key_heads": 16,
    "linear_num_value_heads": 48,
    "linear_key_head_dim": 128,
    "linear_value_head_dim": 128,
    "vocab_size": 248320,
    "vision_hidden_size": 1152,
    "vision_intermediate_size": 4304,
}
# The keys of VISION_DEFAULTS the guard checks against the model's config.json (--image-dims, in this order).
VISION_DIM_KEYS = ("hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads", "head_dim",
                   "linear_num_key_heads", "linear_num_value_heads", "linear_key_head_dim", "linear_value_head_dim",
                   "vocab_size", "vision_hidden_size", "vision_intermediate_size")
# Keys a vision block may no longer carry, refused by name (the fixed margins the computed reservation replaced).
VISION_REPLACED = {"vision_margin_bytes": "image_prefill_bytes(), computed from the model's dimensions "
                                          "(docs/LOCAL-RESOURCES.md \"Vision\")"}
# Qwen3.5's vision geometry (the guard's VISION_GEOMETRY, checked at startup): 16-pixel patches, stills duplicated
# over a temporal patch of 2, 3 channels, merged 2 x 2 into one language-model token.
IMAGE_PATCH_VALUES = 3 * 2 * 16 * 16
PATCHES_PER_IMAGE_TOKEN = 2 * 2


def check_vision(raw: Any, cfg: dict[str, Any] | None = None) -> dict[str, int] | None:
    """A model entry's `vision` block, validated (None: a text-only model). `true` is every default. Positive integers
    only; image_context_tokens no more than the model's context_tokens (cfg), and room for one image in it."""
    if raw is None or raw is False:
        return None
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise GovernorConfigError("vision must be an object, true, or absent")
    out = dict(VISION_DEFAULTS)
    for key, v in raw.items():
        if key.startswith("_"):
            continue
        if key in VISION_REPLACED:
            raise GovernorConfigError(f"vision: {key} was replaced by {VISION_REPLACED[key]}")
        if key not in VISION_DEFAULTS:
            raise GovernorConfigError(f"vision: unknown key {key!r}")
        out[key] = v
    for key, v in out.items():
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise GovernorConfigError(f"vision: {key} must be a positive integer, got {v!r}")
    if cfg is not None and out["image_context_tokens"] > cfg["context_tokens"]:
        raise GovernorConfigError("vision: image_context_tokens must not exceed context_tokens")
    if out["image_tokens_max"] >= out["image_context_tokens"]:
        raise GovernorConfigError("vision: image_tokens_max must be below image_context_tokens")
    if out["prefill_chunk_tokens"] > out["image_context_tokens"]:
        raise GovernorConfigError("vision: prefill_chunk_tokens must not exceed image_context_tokens")
    if out["act_bytes"] not in (2, 4):
        raise GovernorConfigError("vision: act_bytes must be 2 (bfloat16 / float16) or 4 (float32)")
    return out


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
    # The model's declared image capability (None: text-only), checked against this context.
    out["vision"] = check_vision(model.get("vision"), out)
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


class ImagePlan:
    """What image_policy found in a request a vision model may serve: the images (each {"kind", "width", "height",
    "bytes"}) and `data_chars`, the characters of their validated base64 data, which the text bound leaves out."""

    def __init__(self, images: list[dict[str, Any]], data_chars: int):
        self.images, self.data_chars = images, data_chars

    def __len__(self) -> int:
        return len(self.images)


def image_policy(body: dict[str, Any], vision: dict[str, Any]) -> tuple[str | None, ImagePlan]:
    """(why refused or None, ImagePlan) for a VISION model (#125; module docstring "IMAGES"). Text-only content follows
    non_text_content's rules. On top of them an image block is taken only:
      * in a user message's own content (not the system prompt, not an assistant turn, never inside a tool_result:
        vllm-mlx 0.6.0's adapter keeps only the text of a tool_result, so an image there would be dropped silently);
      * as an inline base64 source (a url source would make the node fetch it: refused, nothing is fetched);
      * within image_check: PNG / JPEG / WebP still, real type equal to the declared media_type, at most
        max_image_bytes decoded, header dimensions at most max_image_pixels, aspect ratio at most 200;
      * at most max_images of them in the request.
    Every other non-text block (a document, ...) is refused as before. The first problem is reported; nothing of the
    image is in the message."""
    images: list[dict[str, Any]] = []
    chars = 0
    system = body.get("system")
    if isinstance(system, list) and not _text_only_list(system):
        return "non-text system block", ImagePlan([], 0)
    for i, m in enumerate(_messages(body)):
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for b in content:
            kind = b.get("type") if isinstance(b, dict) else None
            if kind == "image":
                if m.get("role") != "user":
                    return f"image in a non-user message {i + 1}", ImagePlan([], 0)
                src = b.get("source")
                if not isinstance(src, dict) or src.get("type") != "base64":
                    return (f"image in message {i + 1} is not an inline base64 image (url and other sources are "
                            "not fetched)"), ImagePlan([], 0)
                if len(images) >= vision["max_images"]:
                    return f"more than {vision['max_images']} images", ImagePlan([], 0)
                try:
                    info, _ = image_check.check_base64(src.get("media_type"), src.get("data"),
                                                       max_bytes=vision["max_image_bytes"],
                                                       max_pixels=vision["max_image_pixels"])
                except image_check.ImageRefused as exc:
                    return f"image {len(images) + 1} (message {i + 1}): {exc}", ImagePlan([], 0)
                images.append(info)
                chars += len(src["data"])
                continue
            if kind not in TEXT_BLOCKS:
                name = kind if isinstance(kind, str) and kind.isidentifier() and len(kind) <= 40 else "non-text"
                return f"{name} block in message {i + 1}", ImagePlan([], 0)
            if kind == "tool_result" and isinstance(b.get("content"), list) and not _text_only_list(b["content"]):
                return (f"non-text tool_result content in message {i + 1} (images inside a tool result are not "
                        "carried to the model)"), ImagePlan([], 0)
    return None, ImagePlan(images, chars)


# What vllm-mlx's adapter (api/anthropic_adapter._convert_tool) sends for a tool defined without input_schema.
_DEFAULT_SCHEMA = {"type": "object", "properties": {}}


def _dumped_bytes(value: Any, left: int, indent: int | None = None) -> int:
    """len(json.dumps(value, indent=indent)): ASCII-escaped, so its characters are its bytes, and never shorter than the
    same value as vllm-mlx renders it (the adapter's json.dumps, the template's tojson with ensure_ascii=False, both with
    the ", " / ": " separators). Counted chunk by chunk (iterencode) and stopped once past `left`, so a 32 MiB body or a
    deep schema under indent is never built in memory. A value too deep to encode counts as `left + 1` (over the cap)."""
    n = 0
    try:
        for chunk in json.JSONEncoder(indent=indent).iterencode(value):
            n += len(chunk)
            if n > left:
                break
    except (RecursionError, ValueError, TypeError):
        return left + 1
    return n


def reserialized_bytes(body: dict[str, Any], left: int = sys.maxsize) -> int:
    """The bytes of what vllm-mlx 0.6.0 RE-SERIALIZES instead of copying, each as long as its widest rendering, plus
    the instruction text it adds from outside the body (INJECTED_TEXT_BYTES). Re-serialization can be longer than the
    body bytes it came from (compact JSON gains ", " / ": " separators, non-ASCII text becomes \\uXXXX, indent=2 adds
    a line and its indentation per value), so these are added IN FULL on top of the body bytes (docs/LOCAL-RESOURCES.md
    "The input bound"):

      * every tool definition, as the OpenAI-shaped tool the template renders with tojson (wrapper and the default
        schema of a tool without input_schema included);
      * every tool_use block's input (the adapter's json.dumps arguments; the template's tojson of them);
      * response_format, as json.dumps(..., indent=2) (build_json_system_prompt dumps its schema that way), plus
        its instruction text;
      * a forced tool_choice ("any" or "tool"): its instruction text and the tool_choice itself (the name is written
        twice).

    Stops counting once past `left` (input_tokens' cap)."""
    total = 0

    def add(value: Any, indent: int | None = None) -> None:
        nonlocal total
        if total <= left:
            total += _dumped_bytes(value, left - total, indent)

    for t in _tools(body):
        add({"type": "function", "function": {"name": t.get("name"), "description": t.get("description") or "",
                                              "parameters": t.get("input_schema") or _DEFAULT_SCHEMA}}
            if isinstance(t, dict) else t)
    for m in _messages(body):
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    add(b.get("input") or {})
    if body.get("response_format"):
        total += INJECTED_TEXT_BYTES
        add(body["response_format"], indent=2)       # a superset of its schema under the same indent
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") in ("any", "tool"):
        total += INJECTED_TEXT_BYTES
        add(choice)
    return total


def _unassigned() -> re.Pattern:
    """A pattern matching the non-ASCII code points this Python's Unicode table leaves unassigned (built once)."""
    global _UNASSIGNED
    if _UNASSIGNED is None:
        ranges, start = [], None
        for cp in range(0x80, 0x110001):
            gap = cp <= 0x10FFFF and unicodedata.category(chr(cp)) == "Cn"
            if gap and start is None:
                start = cp
            elif not gap and start is not None:
                ranges.append(f"{re.escape(chr(start))}-{re.escape(chr(cp - 1))}")
                start = None
        _UNASSIGNED = re.compile("[" + "".join(ranges) + "]")
    return _UNASSIGNED


def _utf8(s: str) -> int:
    return len(s.encode("utf-8", "surrogatepass"))      # a lone surrogate (a JSON \ud800 escape) is 3 bytes


def _normalized_excess(s: str, left: int = sys.maxsize) -> int:
    """How many more UTF-8 bytes the tokenizer's normalizer can make of `s` than `s` has: NFD's bytes over the raw
    bytes (never negative), plus UNASSIGNED_EXCESS per code point this Python cannot decompose. ASCII is 0. Measured
    in chunks (NFD is per character, so chunks add up) and stopped once past `left`."""
    if s.isascii():
        return 0
    n = 0
    for i in range(0, len(s), NORMALIZE_CHUNK):
        part = s[i:i + NORMALIZE_CHUNK]
        if part.isascii():
            continue
        if not unicodedata.is_normalized("NFD", part):
            n += max(0, _utf8(unicodedata.normalize("NFD", part)) - _utf8(part))
        n += UNASSIGNED_EXCESS * len(_unassigned().findall(part))
        if n > left:
            break
    return n


def normalization_bytes(body: dict[str, Any], left: int = sys.maxsize) -> int:
    """The bytes the tokenizer's Unicode normalization (NFC) can add to the prompt rendered from `body`, bounded from
    above (docs/LOCAL-RESOURCES.md "The input bound"): every string in the body, keys included, counted once for each
    time vllm-mlx 0.6.0 can write it raw into the prompt. That is once, except a forced tool_choice, whose tool name its
    instruction line writes twice, so tool_choice is counted again. ASCII adds nothing, and so does text vllm-mlx
    writes ASCII-escaped (json.dumps). Walked without recursion; stops once past `left`."""
    total = 0
    stack: list = [body, body.get("tool_choice")]
    while stack and total <= left:
        v = stack.pop()
        if isinstance(v, str):
            total += _normalized_excess(v, left - total)
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return total


def input_tokens(raw: bytes, cap: int | None = None, exclude_chars: int = 0) -> int:
    """An upper bound on a turn's input tokens, never an estimate (docs/LOCAL-RESOURCES.md "The input bound"):
        len(raw) + template_tokens(body) + reserialized_bytes(body) + normalization_bytes(body)
    capped at `cap` (the model's context; None: uncapped). `raw` is the request body EXACTLY as the client sent it:
    every character the model sees from the body costs at least one byte there, a byte-level BPE token covers at least
    one byte of the normalized text, the template allowance covers the template's own text, reserialized_bytes what
    vllm-mlx writes wider than it came and normalization_bytes what NFC writes wider. A body that is not a JSON object
    is bounded by its length plus the fixed allowance (the gate refuses it before this is asked).
    `exclude_chars` (#125): characters of validated image data (ImagePlan.data_chars) the model never reads as text;
    each was at least one byte of `raw` (base64 is ASCII; JSON may only escape it longer), so leaving them out keeps
    the bound an upper bound. The images' own tokens are added by request_tokens."""
    limit = sys.maxsize if cap is None else max(0, int(cap))
    raw = bytes(raw)
    excluded = max(0, min(int(exclude_chars), len(raw)))
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        return min(limit, len(raw) + TEMPLATE_TOKENS_FIXED)
    total = len(raw) - excluded + template_tokens(body)
    if total < limit:
        total += reserialized_bytes(body, limit - total)
    if total < limit:
        total += normalization_bytes(body, limit - total)
    return min(limit, total)


def request_tokens(cfg: dict[str, Any], input_tokens: int, max_tokens: Any, images: int = 0) -> int:
    """The context a request will hold: its input tokens (input_tokens(): an upper bound) plus the output it may
    write (max_tokens; default_max_tokens when absent or not a positive integer), capped at context_tokens. With
    `images` (#125, a vision model): plus image_tokens_max per image, capped at image_context_tokens instead (the
    guard refuses a longer image-bearing prompt exactly)."""
    out = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0 \
        else cfg["default_max_tokens"]
    vision = cfg.get("vision") if images else None
    if images and vision is None:
        raise ValueError("an image-bearing request needs a vision model's settings")
    if vision:
        return min(vision["image_context_tokens"],
                   max(0, int(input_tokens)) + images * vision["image_tokens_max"] + out)
    return min(cfg["context_tokens"], max(0, int(input_tokens)) + out)


def image_prefill_bytes(cfg: dict[str, Any], tokens: int, images: int) -> dict[str, int]:
    """The computed worst case, in bytes, of what an image-bearing request holds beside its KV cache and recurrent
    state while the guard prefills it (cortados_vllm_mlx_guard.py "Media prefill"), by term; "total" is their sum.
    P = tokens (the request's whole context: prompt + max_tokens, a bound on the prompt), C = min(chunk, P),
    T = images x image_tokens_max image tokens, N = 4 T vision patches; a = act_bytes; H, I = hidden and intermediate
    size; Hq, Hkv, D = attention heads, KV heads, head_dim; Kh, Vh, Kd, Vd the linear-attention heads and head dims;
    V = vocab; Hv, Iv the vision tower's widths. Each term holds at once with the others (a sum, not a maximum):
      sequence  P x (3aH + 2H + 64)       the whole prompt's embeddings, live for the whole prefill: embed_tokens'
                                          output, the merged copy masked_scatter writes, one more copy (a), the image
                                          mask broadcast to P x H and its numpy copy (2H bool), ids / position ids /
                                          attention mask (64)
      media     T x H x (a + 12)          the merged image features plus masked_scatter's int64 and uint32 indices
              + N x (1536 x (4 + a)       the pixel rows (3 x 2 x 16 x 16 float32) and their activation-dtype copy
                 + a x (12 Hv + 2 Iv))    one vision block's activations per patch (norms, qkv, rotated and padded
                                          q/k/v, attention output and projection; MLP in and out)
      kv_regrow P x kv_bytes_per_token    the KV cache's growth copy: KVCache grows in 256-token steps by
                                          concatenation, and the replaced buffers wait in MLX's allocator cache until
                                          the guard clears it after each chunk
      scores    score_buffers x Hq x C x P x a + C x P
                                          one chunk's attention against the whole context, unfused (head_dim 256), and
                                          the causal mask
      chunk     2 x C x a x (3I + max(3 Hq D + 2 Hkv D, 4 (Kh Kd + Vh Vd)) + 4H)
                                          one layer's activations for a chunk (MLP gate/up/product, attention or
                                          linear-attention projections, residual and norms), doubled for the allocator
      logits    V x (a + 8)               the one kept position's logits and its float32 log-probabilities
    What the old atomic forward computed beside these (every position projected to the vocabulary: P x V x a, 27.8 GiB
    at 60k tokens) is gone: the guard keeps one position."""
    v = cfg["vision"]
    p = max(0, int(tokens))
    c = min(v["prefill_chunk_tokens"], p)
    a, h, i = v["act_bytes"], v["hidden_size"], v["intermediate_size"]
    hq, hkv, d = v["num_attention_heads"], v["num_key_value_heads"], v["head_dim"]
    img = max(0, int(images)) * v["image_tokens_max"]
    patches = img * PATCHES_PER_IMAGE_TOKEN
    width = 3 * i + max(3 * hq * d + 2 * hkv * d,
                        4 * (v["linear_num_key_heads"] * v["linear_key_head_dim"]
                             + v["linear_num_value_heads"] * v["linear_value_head_dim"])) + 4 * h
    out = {
        "sequence": p * (3 * a * h + 2 * h + 64),
        "media": img * h * (a + 12) + patches * (IMAGE_PATCH_VALUES * (4 + a)
                                                 + a * (12 * v["vision_hidden_size"]
                                                        + 2 * v["vision_intermediate_size"])),
        "kv_regrow": p * cfg["kv_bytes_per_token"],
        "scores": v["score_buffers"] * hq * c * p * a + c * p,
        "chunk": 2 * c * a * width,
        "logits": v["vocab_size"] * (a + 8),
    }
    out["total"] = sum(out.values())
    return out


def request_bytes(cfg: dict[str, Any], input_tokens: int, max_tokens: Any, images: int = 0) -> int:
    """request_tokens as a reservation in bytes (module docstring). An image-bearing request reserves its KV cache and
    state plus image_prefill_bytes (no fixed margins); a text-only one tokens_bytes, unchanged."""
    tokens = request_tokens(cfg, input_tokens, max_tokens, images)
    if images and cfg.get("vision"):
        return tokens * cfg["kv_bytes_per_token"] + cfg["per_seq_state_bytes"] \
            + image_prefill_bytes(cfg, tokens, images)["total"]
    return tokens_bytes(cfg, tokens)


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
    """One admitted request's hold: a COUNT slot and the bytes it reserved. release() gives both back, once (a second
    call, from a later finally or error path, does nothing)."""

    def __init__(self, admission: "Admission", reserved: int, image: bool = False):
        self._admission, self.reserved, self._done, self.image = admission, reserved, False, image

    def release(self) -> None:
        self._admission._release(self)


class Admission:
    """The gate's admission: try_acquire(reserve) admits a request while fewer than the governor's COUNT limit run AND
    its reservation fits the BYTES left (module docstring), both checked and taken in one step under one lock. No
    governor (None) is unlimited, today's behaviour for an Ollama node. A refused request is never queued here: the
    gate answers at once, a retryable 529 when the node is full now, a non-retryable refusal when the request could
    never fit (`too_large`)."""

    def __init__(self, governor: Governor | None):
        self.governor = governor
        self._lock = threading.Lock()
        self.inflight = 0
        self.reserved = 0                    # bytes held by the admitted requests in flight
        self.image_inflight = 0              # image-bearing requests in flight (#125)

    def _report(self, g: dict[str, Any]) -> dict[str, Any]:
        """The state report under the lock: the count sample, in flight, and (governed) the bytes and token room."""
        out = {**g, "inflight": self.inflight}
        if self.governor is None:
            return {**out, "reserved_bytes": None, "free_bytes": None, "fit_tokens": None, "free_tokens": None,
                    "image_inflight": None, "image_limit": None}
        cfg = self.governor.cfg
        vision = cfg.get("vision")
        out.update(image_inflight=self.image_inflight if vision else None,
                   image_limit=vision["image_concurrency"] if vision else None)
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

    @property
    def context_tokens(self) -> int | None:
        """The served model's context (input_tokens' cap), None without a governor."""
        return self.governor.cfg["context_tokens"] if self.governor else None

    @property
    def vision(self) -> dict[str, Any] | None:
        """The served model's image policy (check_vision), None for a text-only model or without a governor."""
        return self.governor.cfg.get("vision") if self.governor else None

    def request_bytes(self, input_tokens: int, max_tokens: Any, images: int = 0) -> int:
        """The reservation a request asks for (0 without a governor: nothing is budgeted)."""
        return request_bytes(self.governor.cfg, input_tokens, max_tokens, images) if self.governor else 0

    def try_acquire(self, reserve: int = 0, image: bool = False) -> tuple[Ticket | None, dict[str, Any]]:
        """(ticket, report) when admitted; (None, report) when not, report["refused"] saying why: "too_large" (more
        than the whole budget beside the weights: never, even when the count is full too) or "busy" (the count limit,
        the bytes left now, or for an `image`-bearing request every image slot taken; retryable). The count slot, the
        bytes and the image slot are taken together, atomically. An image-bearing request on a model without a vision
        policy (or without a governor) is "unsupported": the gate checks that first, this only fails closed."""
        g = self.governor.current() if self.governor else NO_GOVERNOR
        with self._lock:
            vision = self.vision
            if image and vision is None:
                return None, {**self._report(g), "refused": "unsupported", "reason": "the model reads no images"}
            if self.governor is None:
                self.inflight += 1
                return Ticket(self, 0), self._report(g)
            nbytes = max(0, int(reserve))
            avail = available_bytes(self.governor.cfg)
            refused = None
            if nbytes > avail:
                refused = {"refused": "too_large", "reason": f"request needs {_gib(nbytes)} GiB, more than the "
                                                             f"{_gib(avail)} GiB budget beside the weights"}
            elif g["limit"] is not None and self.inflight >= g["limit"]:
                refused = {"refused": "busy"}                       # the sample's reason: owner / pressure
            elif image and self.image_inflight >= vision["image_concurrency"]:
                refused = {"refused": "busy", "reason": f"image turns: {self.image_inflight} of "
                                                        f"{vision['image_concurrency']} running"}
            elif self.reserved + nbytes > avail:
                refused = {"refused": "busy", "reason": f"memory budget: {_gib(avail - self.reserved)} GiB free, "
                                                        f"request needs {_gib(nbytes)} GiB"}
            if refused is not None:
                return None, {**self._report(g), **refused, "request_bytes": nbytes}
            self.inflight += 1
            self.reserved += nbytes
            if image:
                self.image_inflight += 1
            return Ticket(self, nbytes, image), self._report(g)

    def _release_locked(self, ticket: Ticket) -> None:
        if ticket._done:
            return
        ticket._done = True
        self.inflight = max(0, self.inflight - 1)
        self.reserved = max(0, self.reserved - ticket.reserved)
        if ticket.image:
            self.image_inflight = max(0, self.image_inflight - 1)

    def _release(self, ticket: Ticket) -> None:
        with self._lock:
            self._release_locked(ticket)

    @property
    def retry_after_s(self) -> int:
        return int(self.governor.cfg["retry_after_s"]) if self.governor else DEFAULTS["retry_after_s"]
