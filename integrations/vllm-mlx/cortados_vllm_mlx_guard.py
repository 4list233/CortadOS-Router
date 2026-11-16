#!/usr/bin/env python3
"""CortadOS prompt guard for vllm-mlx 0.6.0: makes the server refuse a prompt longer than the node's context.

Why: vllm-mlx 0.6.0 neither truncates nor refuses a prompt longer than the model's context (`--max-request-tokens`
bounds max_tokens only), and the MacBook Pro's GPU memory is wired, so a prompt bigger than the local gate reserved for
could freeze the laptop. The gate bounds each turn's input from the body's bytes (cortados_router/local_governor.py,
docs/LOCAL-RESOURCES.md "The input bound") and caps that reservation at the context; this guard makes the cap true.

How: vllm-mlx's text (LLM) route tokenizes a prompt for generation in exactly one place, `Scheduler.add_request`
(vllm_mlx/scheduler.py, 0.6.0 lines 2096-2125): `request.prompt_token_ids = self.tokenizer.encode(request.prompt)`
when they are not set yet, before the request is queued (`self.waiting.append`, line 2221, the only place a request
enters the queue; EngineCore.add_request and generate_batch_sync, engine_core.py 493/496/719, are its only callers).
The guard replaces that method with one that tokenizes the same way, sets the ids (so the count IS the prompt that is
prefilled, not a second opinion of it) and refuses, before calling the original, when

    prompt tokens + max_tokens > context_tokens

with fastapi's HTTPException(413, "cortados guard: prompt too long for this node's context (...)"). Nothing is queued
or prefilled. engine_core.add_request aborts and cleans up a request whose add_request raised (engine_core.py line
503, `except BaseException`).
The client sees the refusal: a non-streamed /v1/messages answers 413 {"detail": ...}, which the gate turns into its
never-fits 413 (the provider routes the turn elsewhere); a streamed one has already sent message_start, so it ends
with an Anthropic `error` event carrying the same text (server._anthropic_sse_error_frame).

Only the batched engine reaches that method. vllm-mlx 0.6.0 picks its engine from `--continuous-batching`
(cli.py `use_batching=args.continuous_batching` → server.load_model / server._build_engine / model_registry: True is
engine/batched.py BatchedEngine, whose text route is AsyncEngineCore → Scheduler; False is engine/simple.py
SimpleEngine, which generates through mlx-lm directly and never calls Scheduler.add_request). Pinning the files that
choose would not prove the choice, so the guard enforces it where it matters, at runtime:
  * `serve` without the exact `--continuous-batching` flag is refused before vllm-mlx starts;
  * BaseEngine.__new__ admits only BatchedEngine itself: constructing SimpleEngine, or any other engine, raises
    UnguardedEngine (at startup, the server exits; the LaunchAgent's restart then fails the same way);
  * every other BaseEngine subclass's start/generate/stream_generate/chat/stream_chat raises the same, in case an
    instance exists without __new__; and a BaseEngine subclass defined after the guard is installed is refused at
    class creation.

Streamed overflow is not refused before `message_start` (left as is, round 6): a pre-stream check would have to
render the chat template a second time outside BatchedEngine.stream_chat (server._stream_anthropic_messages calls it
lazily inside the StreamingResponse generator), and its ids could not be reused without also patching stream_chat,
stream_generate and AsyncEngineCore. The scheduler check above remains the one bound.

The multimodal route (MLLMScheduler) tokenizes for generation inside mlx_vlm's prepare_inputs, a path the text cap
cannot count, so without --vision it is not served: the guard stops startup if vllm-mlx resolves the model to the MLLM
route, and MLLMScheduler.add_request refuses every request in case one is built anyway.

Vision mode (--vision and its image flags, #125; docs/LOCAL-RESOURCES.md "Vision"). On an MLLM model EVERY request,
text-only included, goes BatchedEngine -> MLLMScheduler.add_request (mllm_scheduler.py 413-500, the only place a
request enters its queue: `self.waiting.append`, line 493) -> _schedule_waiting -> MLLMBatchGenerator.insert ->
_process_prompts / the chunked-prefill _chunked_next, which all reach the vision encoder and prefill only after
MLLMBatchGenerator._preprocess_request (mllm_batch_generator.py 999-1143: process_image_input, the pixel cache's hit
branch 1073-1090 and its miss branch, mlx_vlm.utils.prepare_inputs 1100-1106). add_request's own count (line 486) is
text tokens only; the real length P is known only after prepare_inputs expands each image placeholder into
grid_h x grid_w / 4 tokens (mlx-vlm 0.7.12 processing_qwen3_vl.py 892-904). So the guard bounds it twice:
  * layer 1, MLLMScheduler.add_request, before anything is queued: images checked from their bytes (count, inline
    data: URL only, PNG/JPEG/WebP still, encoded bytes, real type, header dimensions and pixels: image_check), then
    P = text tokens - placeholders + sum of the processor's own num_image_tokens(h, w) (processing_qwen3_vl.py
    459-488, sharing _resolved_size with the preprocessing), and P + max_tokens over the context (image_context_tokens
    with images) is the same 413 REFUSAL as the text route's;
  * layer 2, MLLMBatchGenerator._preprocess_request, after prepare_inputs (or a cache hit) and before the encoder or
    prefill: the actual P, and the placeholders, image_grid_thw rows and pixel rows must agree (the qwen3_5 merge
    raises inside prefill on a mismatch, which fails every request of that batch). A refusal releases the request's
    tensors and fails it alone (_process_prompts' per-request error path, line 1876-1903).
Also: the processor's max_pixels set to image_tokens x 1,024 (upstream allows 16.7 MP = 16,384 tokens per image);
process_image_input takes checked data: URLs only (no download, no unbounded base64 temp-file cache) and its temp
files are removed after preprocessing; VisionEmbeddingCache (count-only, 100/50 entries) is disabled; PIL's bomb limit
is the pixel bound; drafters and specprefill are refused; and only a model dir whose config.json is qwen3_5 /
Qwen3_5ForConditionalGeneration with a vision_config may serve. The text Scheduler refuses every request in this mode.
Pinned on top: the MLLM files of vllm-mlx 0.6.0 (VISION_PINNED_SHA256) and mlx-vlm 0.7.12 (MLX_VLM_PINNED_SHA256).

Pinned: vllm-mlx 0.6.0 (`vllm_mlx.__version__`) and the sha256 of the files the argument above reads
(PINNED_SHA256: scheduler.py, engine_core.py, engine/batched.py, and the engine dispatch cli.py, server.py,
model_registry.py, engine/__init__.py, engine/base.py, from the PyPI wheel
vllm_mlx-0.6.0-py3-none-any.whl, sha256 a5e5a52a...f062). Anything else stops startup: a new vllm-mlx needs this
argument re-read before the node serves it.

Usage (scripts/install_mlx_backend.sh copies this file to <venv>/bin/cortados-vllm-mlx-guard.py, and the LaunchAgent
runs it with the venv's python; cortados_router/local_backend.serve_argv builds the line):

    <venv>/bin/python <venv>/bin/cortados-vllm-mlx-guard.py --context-tokens 131072 -- serve <model> [vllm-mlx args]
    <venv>/bin/python <venv>/bin/cortados-vllm-mlx-guard.py --check <model dir>     (pins, engines and route; serves
                                                                                      nothing)

Runs in the MLX venv, not the router package (whose stdlib-only rule it does not share); it imports vllm_mlx and
fastapi from that venv. It loads no model itself: `serve` hands over to vllm-mlx's own CLI with the guard installed.
"""
from __future__ import annotations

import hashlib
import os
import sys
from typing import Any

VLLM_MLX_VERSION = "0.6.0"
PINNED_SHA256 = {
    "scheduler.py": "6fa94c0af2c4d653290a7cdfa8a1bf63c1d54a2017e2b300e7fa8cea3d24b6be",
    "engine_core.py": "f97949899bc26e996060decbb4e17e566edadaf132334e640d4077926beca67c",
    "engine/batched.py": "ec3a3d0e5e0257f1ab91acfe9dcb9b6eb0960097f80c76ceff7a33de37b5de9d",
    # The engine dispatch (which engine serves): the runtime refusal in install() is the proof, these the context.
    "cli.py": "7f2b7346982884582dccaa2f0c69592df275c502d1ff8bf5514902d937f8f6a0",
    "server.py": "d10d0c6f5f7eb7e3974de2bf41bd9a5342103742fd5c3dff4b1933cafc51bb63",
    "model_registry.py": "404b046b57d3a20a3930a031984b50f88c62dd1e1dde025b64c7a06b07d3e189",
    "engine/__init__.py": "acad2e2de72def8b7befe0f057dbc27b396cfc33636def15d609fec759164ae1",
    "engine/base.py": "6638d6c0265378d17d8753ac1a23c26016578d26b00c22cb5dc5d82c0360f46a",
}
# The refusal's fixed prefix (cortados_router.local_backend.GUARD_REFUSAL; a test keeps the two equal).
REFUSAL = "cortados guard: prompt too long for this node's context"
MLLM_REFUSAL = "cortados guard: the multimodal route is not served (its prompt length cannot be guarded)"
ENGINE_REFUSAL = ("cortados guard: only the guarded continuous-batching engine (BatchedEngine) may serve; "
                  "serve with --continuous-batching")
BATCHING_FLAG = "--continuous-batching"
# The engine methods that start or run generation (vllm_mlx/engine/base.py BaseEngine's abstract interface).
ENGINE_METHODS = ("start", "generate", "stream_generate", "chat", "stream_chat")
EXIT_REFUSED = 2


class GuardError(RuntimeError):
    pass


class UnguardedEngine(RuntimeError):
    """An engine other than BatchedEngine was about to be built or used (its prompts never reach the cap)."""


def verify(package_dir: str, version: str, pins: dict[str, str] | None = None) -> None:
    """GuardError unless this is the vllm-mlx the guard was written against: the version and every pinned file."""
    if version != VLLM_MLX_VERSION:
        raise GuardError(f"vllm-mlx {version} is not {VLLM_MLX_VERSION}: re-read the guard's argument for it first")
    for rel, want in (PINNED_SHA256 if pins is None else pins).items():
        path = os.path.join(package_dir, *rel.split("/"))
        try:
            with open(path, "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
        except OSError as exc:
            raise GuardError(f"vllm_mlx/{rel} unreadable ({type(exc).__name__})") from None
        if got != want:
            raise GuardError(f"vllm_mlx/{rel} differs from the 0.6.0 release the guard was written against")


def prompt_token_ids(tokenizer: Any, prompt: Any) -> list:
    """vllm-mlx 0.6.0 Scheduler.add_request's own tokenization, branch for branch (scheduler.py 2107-2125)."""
    if isinstance(prompt, str):
        if hasattr(tokenizer, "encode"):
            return tokenizer.encode(prompt)
        if hasattr(tokenizer, "tokenizer") and hasattr(tokenizer.tokenizer, "encode"):
            return tokenizer.tokenizer.encode(prompt)
        raise AttributeError(f"Tokenizer {type(tokenizer)} has no 'encode' method. "
                             "Continuous batching requires a tokenizer with encode support.")
    return list(prompt)


def over_context(prompt_tokens: int, max_tokens: Any, context_tokens: int) -> str | None:
    """The refusal text when prompt + max_tokens exceeds the context, else None. A max_tokens that is not a
    non-negative integer counts as the whole context (vllm-mlx always resolves one; this only fails closed)."""
    out = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens >= 0 \
        else context_tokens
    if prompt_tokens + out <= context_tokens:
        return None
    return (f"{REFUSAL} (prompt {prompt_tokens} tokens + max_tokens {out} = {prompt_tokens + out}, over the "
            f"{context_tokens}-token context)")


def _subclasses(cls: Any) -> list:
    out, todo = [], list(cls.__subclasses__())
    while todo:
        c = todo.pop()
        if c not in out:
            out.append(c)
            todo.extend(c.__subclasses__())
    return out


def check_engines(base_engine_cls: Any, batched_engine_cls: Any) -> None:
    """GuardError unless refuse_other_engines can hold on these classes (read only; --check runs it)."""
    if not (isinstance(batched_engine_cls, type) and issubclass(batched_engine_cls, base_engine_cls)):
        raise GuardError("BatchedEngine is not a BaseEngine: re-read the guard's argument for this vllm-mlx")
    if getattr(base_engine_cls.__dict__.get("__new__"), "__cortados_guard__", False):
        raise GuardError("the engine guard is already installed")
    for c in (base_engine_cls, *_subclasses(base_engine_cls)):
        # A __new__ of its own would bypass the one installed on BaseEngine (none in 0.6.0).
        if "__new__" in c.__dict__ or "__init_subclass__" in c.__dict__:
            raise GuardError(f"{c.__name__} defines __new__ or __init_subclass__: re-read the guard's argument")


def refuse_other_engines(base_engine_cls: Any, batched_engine_cls: Any) -> None:
    """Fail closed on every engine but BatchedEngine (module docstring): BaseEngine.__new__ admits only it, the other
    engines' generation methods raise, and a BaseEngine subclass defined from now on is refused at class creation."""
    check_engines(base_engine_cls, batched_engine_cls)
    for c in _subclasses(base_engine_cls):
        if c is batched_engine_cls:
            continue
        for name in ENGINE_METHODS:
            if callable(getattr(c, name, None)):
                setattr(c, name, _refusing(f"{c.__name__}.{name}"))

    def __new__(cls, *args, **kwargs):
        if cls is not batched_engine_cls:
            raise UnguardedEngine(f"{ENGINE_REFUSAL} (refused {cls.__name__})")
        return object.__new__(cls)

    __new__.__cortados_guard__ = True
    base_engine_cls.__new__ = staticmethod(__new__)

    def __init_subclass__(cls, **kwargs):
        raise UnguardedEngine(f"{ENGINE_REFUSAL} (a new engine class {cls.__name__} was defined after the guard)")

    __init_subclass__.__cortados_guard__ = True
    base_engine_cls.__init_subclass__ = classmethod(__init_subclass__)


def _refusing(what: str):
    def refused(*args, **kwargs):
        raise UnguardedEngine(f"{ENGINE_REFUSAL} ({what})")

    refused.__cortados_guard__ = True
    return refused


def install(context_tokens: int, scheduler_cls: Any, mllm_scheduler_cls: Any, api_utils: Any,
            http_exception: Any, base_engine_cls: Any, batched_engine_cls: Any, vision: Any = None) -> None:
    """Patch the classes in place (before any request exists): the prompt cap on Scheduler.add_request, the MLLM
    route refused at route resolution and at MLLMScheduler.add_request, every engine but BatchedEngine refused.
    `vision` (a VisionPolicy, "Vision mode"): the MLLM route is left to install_vision instead, and the text
    Scheduler refuses every request (a vision model must never be served by the text route, whose chat drops images)."""
    if isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or context_tokens <= 0:
        raise GuardError(f"--context-tokens must be a positive integer, got {context_tokens!r}")
    original = scheduler_cls.add_request
    if getattr(original, "__cortados_guard__", False):
        raise GuardError("the guard is already installed")
    refuse_other_engines(base_engine_cls, batched_engine_cls)

    def add_request(self, request):
        if vision is not None:
            raise http_exception(status_code=501, detail=TEXT_ROUTE_REFUSAL)
        if request.prompt_token_ids is None:
            ids = prompt_token_ids(self.tokenizer, request.prompt)
            # Set here, so the original skips its own tokenization and prefills exactly these ids.
            request.prompt_token_ids = ids
            request.num_prompt_tokens = len(ids)
        why = over_context(len(request.prompt_token_ids), getattr(request.sampling_params, "max_tokens", None),
                           context_tokens)
        if why is not None:
            raise http_exception(status_code=413, detail=why)
        return original(self, request)

    add_request.__cortados_guard__ = True
    add_request.__wrapped__ = original
    scheduler_cls.add_request = add_request
    if vision is not None:
        return                                   # the MLLM route is install_vision's

    def mllm_add_request(self, *args, **kwargs):
        raise http_exception(status_code=501, detail=MLLM_REFUSAL)

    mllm_add_request.__cortados_guard__ = True
    mllm_scheduler_cls.add_request = mllm_add_request

    resolve = api_utils.resolve_mllm_route

    def resolve_mllm_route(*args, **kwargs):
        route = resolve(*args, **kwargs)
        if getattr(route, "is_mllm", True):
            raise SystemExit(f"cortados-vllm-mlx-guard: {MLLM_REFUSAL}; vllm-mlx resolved this model to the MLLM "
                             f"route ({getattr(route, 'source', '?')}). Serve a text-only copy of the model.")
        return route

    resolve_mllm_route.__cortados_guard__ = True
    api_utils.resolve_mllm_route = resolve_mllm_route


# ---------------------------------------------------------------------------------------------------------------------
# Vision mode (#125): the qwen3_5 MLLM route, guarded. Module docstring "Vision mode" has the argument.
# ---------------------------------------------------------------------------------------------------------------------
MLX_VLM_VERSION = "0.7.12"
# vllm-mlx 0.6.0 files the vision argument reads (same wheel as PINNED_SHA256).
VISION_PINNED_SHA256 = {
    "mllm_scheduler.py": "8c21028e0268e5ee7b21816b8c85650acf01da10b6f3738c0f36ba76396de638",
    "mllm_batch_generator.py": "592e1955cd00fd997ab52bf841b61cc5ee8c21871b0268b4545b8db0cd830e98",
    "vision_embedding_cache.py": "106056ee12361617499a86649b33fa186aada8377bdb898bc69f8176bd901c21",
    "models/mllm.py": "ab988c496c452b8f0e25fdd5d7ba04523f56d03d7f54fd21164f68dbbb12d62e",
    "api/anthropic_adapter.py": "d7af75554e4c0f924a53a59e38be7f36b91c582ee9c9938fa13b7bfa2d53f7ba",
    "api/utils.py": "accef32773d5452d969f4e07816ede2d5610016b062fb80afba329a11bce8f8e",
    "memory_cache.py": "c45f46590723d4170ec1066d53ae487fed67f4c0544f91f9c4659aa0b940b6c6",
    "multimodal_processor.py": "3e1916fdfb5e2e50709ecb1e98f8f3ce49cd4b86f3182f4132e8cc5afc1d2d26",
    "patches/qwen3_5_mllm.py": "9aabccd085a906d5c14fbf2ff773d5cac68a045d72dd02a691b806ea5b460c49",
}
# mlx-vlm 0.7.12 (PyPI wheel mlx_vlm-0.7.12-py3-none-any.whl, sha256 0412a883...8e37): prepare_inputs and load_image,
# the Qwen3-VL processor qwen3_5 uses (install_auto_processor_patch), and the qwen3_5 model whose merge counts the
# placeholders against the features.
MLX_VLM_PINNED_SHA256 = {
    "version.py": "3e340d8ac40a2c58c145b8e0a039888859ccad415614fb1b3817e3fe29ad3f84",
    "utils.py": "d67f2f49bff354bad36cc4adebb375ad0bcee5fbca74976b5fcda29ef4e78c5e",
    "models/base.py": "c75bf1f678270f33acae85a0ed7cbc6b656f51cac6664ecd8a4b23344f46a6cf",
    "models/qwen3_vl/processing_qwen3_vl.py": "bbd3c89872bef5741865ba8397aad184466cdf5a36d84c0b0a48cc5bb5f5fb5d",
    "models/qwen3_5/__init__.py": "d5a1cf335ae0845d0033dcc77dfdd6174f801aa77375cd8d57d69bc0c26f3d2b",
    "models/qwen3_5/qwen3_5.py": "7c0e360a35e1f5d3819677cd6f7e4d7349f4f3dd2fad5fc8f1e374bd514e8f1b",
    "models/qwen3_5/vision.py": "a1db23800b564caaedcc79d6a27d76e3186b9315cb68b612d61afbfa69487600",
    "models/qwen3_5/language.py": "9cd5b798c183ac70d6c4ba0bdfabb04d1da126cd133e25089958d24d674cadac",
    "models/qwen3_5/config.py": "b4db2144b162bbc27c0d843833fdd969413b44260ed1f024866eeadf1832dc0f",
}
VISION_MODEL_TYPE = "qwen3_5"
VISION_ARCHITECTURES = ["Qwen3_5ForConditionalGeneration"]
# The runtime classes the argument was read against (module, class name).
VISION_MODEL_CLASS = ("mlx_vlm.models.qwen3_5.qwen3_5", "Model")
VISION_PROCESSOR_CLASS = ("mlx_vlm.models.qwen3_vl.processing_qwen3_vl", "Qwen3VLProcessor")
VISION_IMAGE_PROCESSOR_CLASS = ("mlx_vlm.models.qwen3_vl.processing_qwen3_vl", "Qwen3VLImageProcessor")
# Qwen3.5's vision geometry: 16-pixel patches merged 2 x 2 (1,024 pixels per token), stills duplicated over T = 2.
VISION_GEOMETRY = {"patch_size": 16, "merge_size": 2, "temporal_patch_size": 2}
# Refusals. IMAGE_REFUSAL: a 400 before anything is queued (the gate already refuses the same; this fails closed).
# PREPARED_REFUSAL: the prepared input disagrees with its bound, after prepare_inputs and before the vision encoder or
# prefill; it fails only its own request (MLLMBatchGenerator._process_prompts' per-request error path).
IMAGE_REFUSAL = "cortados guard: image input refused"
PREPARED_REFUSAL = "cortados guard: prepared multimodal input refused"
TEXT_ROUTE_REFUSAL = "cortados guard: vision mode serves through the MLLM scheduler only"
VISION_FLAGS = {"--max-images": "max_images", "--image-tokens": "image_tokens",
                "--image-context-tokens": "image_context_tokens", "--max-image-bytes": "max_image_bytes",
                "--max-image-pixels": "max_image_pixels"}


class VisionInputRefused(ValueError):
    """Layer 2: a prepared request that disagrees with its bound (raised inside _preprocess_request)."""


class VisionPolicy:
    """The still-image policy the guard enforces (local_backend.vision_argv passes the model entry's `vision`)."""

    def __init__(self, max_images: int, image_tokens: int, image_context_tokens: int, max_image_bytes: int,
                 max_image_pixels: int):
        self.max_images, self.image_tokens = max_images, image_tokens
        self.image_context_tokens = image_context_tokens
        self.max_image_bytes, self.max_image_pixels = max_image_bytes, max_image_pixels
        for k, v in vars(self).items():
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise GuardError(f"vision: {k} must be a positive integer, got {v!r}")
        if image_tokens >= image_context_tokens:
            raise GuardError("vision: --image-tokens must be below --image-context-tokens")

    @property
    def max_pixels(self) -> int:
        """The processor's max_pixels: image_tokens tokens of (patch x merge)^2 pixels each."""
        f = VISION_GEOMETRY["patch_size"] * VISION_GEOMETRY["merge_size"]
        return self.image_tokens * f * f


def parse_vision(args: list[str]) -> tuple["VisionPolicy | None", list[str]]:
    """(the policy or None, the arguments left) from `--vision` and VISION_FLAGS. Every flag is required with
    --vision and refused without it. GuardError on anything else."""
    rest, got, on = [], {}, False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--vision":
            on = True
        elif a in VISION_FLAGS:
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                raise GuardError(f"{a} needs a positive integer")
            got[VISION_FLAGS[a]] = int(args[i + 1])
            i += 1
        else:
            rest.append(a)
        i += 1
    if not on:
        if got:
            raise GuardError("image flags need --vision")
        return None, rest
    missing = [f for f, k in VISION_FLAGS.items() if k not in got]
    if missing:
        raise GuardError(f"--vision needs {', '.join(missing)}")
    return VisionPolicy(**got), rest


def check_vision_model(model_dir: Any) -> None:
    """GuardError unless the model directory's config.json is the verified route: model_type qwen3_5,
    architectures [Qwen3_5ForConditionalGeneration] and a vision_config (the tower)."""
    import json
    path = os.path.join(str(model_dir), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        raise GuardError(f"vision mode: {path} unreadable ({type(exc).__name__})") from None
    if not isinstance(cfg, dict) or cfg.get("model_type") != VISION_MODEL_TYPE \
            or cfg.get("architectures") != VISION_ARCHITECTURES or not isinstance(cfg.get("vision_config"), dict):
        raise GuardError(f"vision mode serves only {VISION_MODEL_TYPE} / {VISION_ARCHITECTURES[0]} with a "
                         "vision_config; this model is not that route")


def verify_vision(vllm_dir: str, vlm_dir: str, vlm_version: str, pins: dict | None = None,
                  vlm_pins: dict | None = None) -> None:
    """GuardError unless vllm-mlx's MLLM files and mlx-vlm are the releases the vision argument was read against."""
    if vlm_version != MLX_VLM_VERSION:
        raise GuardError(f"mlx-vlm {vlm_version} is not {MLX_VLM_VERSION}: re-read the guard's vision argument first")
    for base, name, table in ((vllm_dir, "vllm_mlx", VISION_PINNED_SHA256 if pins is None else pins),
                              (vlm_dir, "mlx_vlm", MLX_VLM_PINNED_SHA256 if vlm_pins is None else vlm_pins)):
        for rel, want in table.items():
            try:
                with open(os.path.join(base, *rel.split("/")), "rb") as f:
                    got = hashlib.sha256(f.read()).hexdigest()
            except OSError as exc:
                raise GuardError(f"{name}/{rel} unreadable ({type(exc).__name__})") from None
            if got != want:
                raise GuardError(f"{name}/{rel} differs from the release the guard's vision argument was read against")


def load_image_check() -> Any:
    """The still-image checker: the installed copy beside the guard (cortados_image_check.py), else this checkout's
    cortados_router/image_check.py (the guard run from source)."""
    try:
        import cortados_image_check as image_check            # noqa: F401  (installed beside the guard)
        return image_check
    except ImportError:
        pass
    import importlib.util
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "cortados_router",
                       "image_check.py")
    spec = importlib.util.spec_from_file_location("cortados_image_check", os.path.normpath(src))
    if spec is None or spec.loader is None or not os.path.exists(os.path.normpath(src)):
        raise GuardError("vision mode: cortados_image_check.py is not beside the guard (scripts/install_mlx_backend.sh "
                         "copies it)")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def harden_pil(image_mod: Any, warnings_mod: Any, max_pixels: int) -> None:
    """PIL's decompression-bomb limit at the policy's pixel bound, a warning above it made an error: the backstop for
    a file whose header lied to image_check (PIL raises DecompressionBombError only past twice the limit)."""
    image_mod.MAX_IMAGE_PIXELS = max_pixels
    warnings_mod.simplefilter("error", image_mod.DecompressionBombWarning)


def _flat(x: Any) -> list:
    """A token or grid array (mx.array, numpy, list) as a flat or row list: tolist() when it has one."""
    v = x.tolist() if hasattr(x, "tolist") else x
    return v if isinstance(v, list) else [v]


def _ids(x: Any) -> list:
    """input_ids (shape (P,) or (1, P)) as a flat list of ints; ValueError for a batch of more than one row."""
    v = _flat(x)
    if v and isinstance(v[0], list):
        if len(v) != 1:
            raise ValueError("a batch of prompts in one request")
        v = v[0]
    return v


def admission_refusal(policy: VisionPolicy, context_tokens: int, prompt_ids: list, image_token_id: int,
                      video_token_id: int, images: Any, videos: Any, audio: Any, max_tokens: Any,
                      count_tokens: Any, check_url: Any) -> tuple[int, str] | None:
    """Layer 1, before anything is queued: (status, detail) to refuse with, or None. Pure (the scheduler wrapper
    passes the tokenizer's ids for the prompt, the processor's num_image_tokens as `count_tokens(h, w)` and
    image_check.check_data_url bound to the policy as `check_url`).
      400 IMAGE_REFUSAL: video or audio; more than max_images; an image that is not a checked inline still; a prompt
          whose image placeholders are not exactly one per image (or holds a video placeholder);
      413 REFUSAL: the exact sequence length P = text tokens - placeholders + sum of each image's tokens, plus
          max_tokens, over the context (image_context_tokens when the request carries images)."""
    if videos or audio:
        return 400, f"{IMAGE_REFUSAL} (video and audio input are not served)"
    images = list(images or [])
    if len(images) > policy.max_images:
        return 400, f"{IMAGE_REFUSAL} (more than {policy.max_images} images)"
    sizes = []
    for n, url in enumerate(images, 1):
        try:
            info = check_url(url)
        except ValueError as exc:                       # image_check.ImageRefused (a ValueError): no image bytes in it
            return 400, f"{IMAGE_REFUSAL} (image {n}: {exc})"
        sizes.append((info["height"], info["width"]))
    pads = sum(1 for t in prompt_ids if t == image_token_id)
    if pads != len(images) or any(t == video_token_id for t in prompt_ids):
        return 400, (f"{IMAGE_REFUSAL} (the prompt holds {pads} image placeholders for {len(images)} images, or a "
                     "video placeholder)")
    tokens = []
    for n, (h, w) in enumerate(sizes, 1):
        try:
            t = int(count_tokens(h, w))
        except ValueError as exc:
            return 400, f"{IMAGE_REFUSAL} (image {n}: {exc})"
        if not 0 < t <= policy.image_tokens:
            return 400, f"{IMAGE_REFUSAL} (image {n} would be {t} tokens, over {policy.image_tokens})"
        tokens.append(t)
    p = len(prompt_ids) - pads + sum(tokens)
    ctx = min(context_tokens, policy.image_context_tokens) if images else context_tokens
    why = over_context(p, max_tokens, ctx)
    return (413, why) if why is not None else None


def prepared_problem(policy: VisionPolicy, context_tokens: int, n_media: int, input_ids: Any, image_grid_thw: Any,
                     pixel_values: Any, extra_kwargs: Any, max_tokens: Any, image_token_id: int, video_token_id: int,
                     merge_size: int) -> str | None:
    """Layer 2, on what prepare_inputs (or a pixel-cache hit) actually produced, before the vision encoder or prefill
    runs: why it is refused, or None. Every count must agree with the next:
      * input_ids is one prompt; its length P plus max_tokens fits the context (image_context_tokens with images);
      * a text-only request has no pixels, no grid and no image or video placeholder;
      * an image request has one grid row (t = 1, a still) per image, each at most image_tokens after merging; the
        placeholders in input_ids are exactly the merged grid total (no video placeholder); pixel_values has one row
        per patch (sum of t*h*w); no video tensors."""
    try:
        ids = _ids(input_ids)
    except ValueError as exc:
        return f"{PREPARED_REFUSAL} ({exc})"
    ctx = min(context_tokens, policy.image_context_tokens) if n_media else context_tokens
    why = over_context(len(ids), max_tokens, ctx)
    if why is not None:
        return f"{PREPARED_REFUSAL}: {why}"
    pads = sum(1 for t in ids if t == image_token_id)
    vpads = sum(1 for t in ids if t == video_token_id)
    extra = extra_kwargs if isinstance(extra_kwargs, dict) else {}
    if vpads or any(k in extra for k in ("pixel_values_videos", "video_grid_thw")):
        return f"{PREPARED_REFUSAL} (video input)"
    if not n_media:
        if pixel_values is not None or image_grid_thw is not None or pads:
            return f"{PREPARED_REFUSAL} (a text-only request carries pixels or image placeholders)"
        return None
    if image_grid_thw is None or pixel_values is None:
        return f"{PREPARED_REFUSAL} (an image request without pixels or grid)"
    rows = _flat(image_grid_thw)
    if len(rows) != n_media or not all(isinstance(r, list) and len(r) == 3 for r in rows):
        return f"{PREPARED_REFUSAL} ({len(rows)} image grids for {n_media} images)"
    patches = merged = 0
    for t, h, w in rows:
        if t != 1 or h <= 0 or w <= 0 or (h * w) % (merge_size * merge_size):
            return f"{PREPARED_REFUSAL} (image grid {t}x{h}x{w} is not a still)"
        tok = h * w // (merge_size * merge_size)
        if tok > policy.image_tokens:
            return f"{PREPARED_REFUSAL} (an image of {tok} tokens, over {policy.image_tokens})"
        patches += t * h * w
        merged += tok
    if pads != merged:
        return f"{PREPARED_REFUSAL} ({pads} image placeholders for {merged} image features)"
    shape = getattr(pixel_values, "shape", None)
    if not shape or shape[0] != patches:
        return f"{PREPARED_REFUSAL} (pixel rows do not match the image grids)"
    return None


def _same(obj: Any, where: tuple[str, str]) -> bool:
    return type(obj).__module__ == where[0] and type(obj).__name__ == where[1]


def install_vision(context_tokens: int, policy: VisionPolicy, *, mllm_scheduler_cls: Any, batch_generator_cls: Any,
                   vision_cache_cls: Any, mllm_models: Any, api_utils: Any, http_exception: Any, image_check: Any,
                   tmp_dir: str | None = None) -> None:
    """Vision mode's patches, in place and before any request exists (module docstring "Vision mode"):
      route        api_utils.resolve_mllm_route: the MLLM route only, and only for a verified qwen3_5 model dir;
      startup      MLLMScheduler.__init__: the model, processor and image processor are the verified classes and
                   geometry, no drafter or specprefill model; the processor's max_pixels set to the policy's;
      layer 1      MLLMScheduler.add_request (the one entry to the MLLM queue): admission_refusal, before queueing;
      layer 2      MLLMBatchGenerator._preprocess_request (every path to the encoder and prefill): prepared_problem
                   after the original, the request's tensors released and only it failed on a refusal; the request's
                   image temp files removed however it ends;
      decode       models.mllm.process_image_input: checked data: URLs only, inside layer 2 only, to a private temp
                   file per call (no URL download, no unbounded base64 file cache);
      caches       VisionEmbeddingCache built disabled (and MLLMBatchGenerator's instance disabled again)."""
    if getattr(mllm_scheduler_cls.add_request, "__cortados_guard__", False):
        raise GuardError("the vision guard is already installed")
    import tempfile
    import threading
    tmp = tmp_dir or tempfile.mkdtemp(prefix="cortados-vision-")     # 0700
    local = threading.local()

    def check_url(url):
        return image_check.check_data_url(url, max_bytes=policy.max_image_bytes,
                                          max_pixels=policy.max_image_pixels)[0]

    # route ---------------------------------------------------------------------------------------------------------
    resolve = api_utils.resolve_mllm_route

    def resolve_mllm_route(model_name, resolved_path=None, *args, **kwargs):
        route = resolve(model_name, resolved_path, *args, **kwargs)
        if not getattr(route, "is_mllm", False):
            raise SystemExit("cortados-vllm-mlx-guard: vision mode, but this model resolves to the text route; serve "
                             "the vision model folder or drop the model entry's `vision` block")
        try:
            check_vision_model(resolved_path or model_name)
        except GuardError as exc:
            raise SystemExit(f"cortados-vllm-mlx-guard: {exc}") from None
        return route

    resolve_mllm_route.__cortados_guard__ = True
    api_utils.resolve_mllm_route = resolve_mllm_route

    # startup -------------------------------------------------------------------------------------------------------
    sched_init = mllm_scheduler_cls.__init__

    def scheduler_init(self, model, processor, config=None, *args, **kwargs):
        if args:
            raise GuardError("vision mode: MLLMScheduler built with positional drafter arguments")
        if not _same(model, VISION_MODEL_CLASS):
            raise GuardError(f"vision mode: the model is {type(model).__module__}.{type(model).__name__}, not "
                             f"{'.'.join(VISION_MODEL_CLASS)}")
        ip = getattr(processor, "image_processor", None)
        if not _same(processor, VISION_PROCESSOR_CLASS) or not _same(ip, VISION_IMAGE_PROCESSOR_CLASS):
            raise GuardError("vision mode: the processor is not mlx-vlm's Qwen3VLProcessor / Qwen3VLImageProcessor")
        for k, v in VISION_GEOMETRY.items():
            if getattr(ip, k, None) != v:
                raise GuardError(f"vision mode: image processor {k} is {getattr(ip, k, None)!r}, not {v}")
        ids = (getattr(processor, "image_token_id", None), getattr(processor, "video_token_id", None))
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or ids[0] == ids[1]:
            raise GuardError("vision mode: the processor's image and video token ids are not two distinct integers")
        if kwargs.get("draft_model") is not None or kwargs.get("specprefill_draft_model") is not None:
            raise GuardError("vision mode: drafter and specprefill models are not served (their memory is unbounded)")
        if not isinstance(getattr(ip, "min_pixels", None), int) or ip.min_pixels > policy.max_pixels:
            raise GuardError("vision mode: the image processor's min_pixels is over the policy's max_pixels")
        ip.max_pixels = policy.max_pixels           # upstream's 16.7 MP would be 16,384 tokens and 1 GiB of KV
        sched_init(self, model, processor, config, *args, **kwargs)
        if getattr(self.config, "specprefill_enabled", False):
            raise GuardError("vision mode: specprefill is not served")
        tok = processor.tokenizer if hasattr(processor, "tokenizer") else processor
        self._cortados_vision = {"tokenizer": tok, "image_token_id": ids[0], "video_token_id": ids[1],
                                 "count": ip.num_image_tokens}

    scheduler_init.__cortados_guard__ = True
    mllm_scheduler_cls.__init__ = scheduler_init

    # layer 1 -------------------------------------------------------------------------------------------------------
    add_request = mllm_scheduler_cls.add_request

    def mllm_add_request(self, prompt, images=None, videos=None, audio=None, max_tokens=256, *args, **kwargs):
        v = getattr(self, "_cortados_vision", None)
        if v is None or not isinstance(prompt, str):
            raise http_exception(status_code=501, detail=f"{IMAGE_REFUSAL} (scheduler not set up by the guard)")
        if kwargs.get("specprefill") or kwargs.get("mllm_draft"):
            raise http_exception(status_code=400, detail=f"{IMAGE_REFUSAL} (specprefill and drafting are not served)")
        tok = v["tokenizer"]
        try:
            ids = tok.encode(prompt, add_special_tokens=False)     # as prepare_inputs tokenizes (no special tokens)
        except TypeError:
            ids = tok.encode(prompt)
        refused = admission_refusal(policy, context_tokens, list(ids), v["image_token_id"], v["video_token_id"],
                                    images, videos, audio, max_tokens, v["count"], check_url)
        if refused is not None:
            raise http_exception(status_code=refused[0], detail=refused[1])
        return add_request(self, prompt, images, videos, audio, max_tokens, *args, **kwargs)

    mllm_add_request.__cortados_guard__ = True
    mllm_add_request.__wrapped__ = add_request
    mllm_scheduler_cls.add_request = mllm_add_request

    # caches --------------------------------------------------------------------------------------------------------
    cache_init = vision_cache_cls.__init__

    def vision_cache_init(self, *args, **kwargs):
        cache_init(self, 0, 0, False)               # disabled: count-only entries of pixels and encodings, no bytes bound

    vision_cache_init.__cortados_guard__ = True
    vision_cache_cls.__init__ = vision_cache_init

    gen_init = batch_generator_cls.__init__

    def generator_init(self, *args, **kwargs):
        gen_init(self, *args, **kwargs)
        cache = getattr(self, "vision_cache", None)
        if cache is not None:
            cache.enabled = False
        ip = getattr(self.processor, "image_processor", None)
        if getattr(ip, "max_pixels", None) != policy.max_pixels:
            raise GuardError("vision mode: the batch generator's processor is not the guarded one")
        self._cortados_vision = {"image_token_id": self.processor.image_token_id,
                                 "video_token_id": self.processor.video_token_id, "merge_size": ip.merge_size}

    generator_init.__cortados_guard__ = True
    batch_generator_cls.__init__ = generator_init

    # layer 2 -------------------------------------------------------------------------------------------------------
    preprocess = batch_generator_cls._preprocess_request

    def release(request):
        request.input_ids = None
        request.pixel_values = None
        request.attention_mask = None
        request.image_grid_thw = None
        if isinstance(getattr(request, "extra_kwargs", None), dict):
            request.extra_kwargs.clear()

    def preprocess_request(self, request):
        v = getattr(self, "_cortados_vision", None)
        if v is None:
            raise VisionInputRefused(f"{PREPARED_REFUSAL} (batch generator not set up by the guard)")
        outer = getattr(local, "paths", None)
        local.paths = [] if outer is None else outer
        try:
            try:
                preprocess(self, request)
                n_media = len(request.images or []) + len(request.videos or []) + len(request.audio or [])
                why = prepared_problem(policy, context_tokens, n_media, request.input_ids, request.image_grid_thw,
                                       request.pixel_values, request.extra_kwargs, request.max_tokens,
                                       v["image_token_id"], v["video_token_id"], v["merge_size"])
            except BaseException:
                release(request)
                raise
            if why is not None:
                release(request)
                raise VisionInputRefused(why)
        finally:
            if outer is None:
                for path in local.paths:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                local.paths = None

    preprocess_request.__cortados_guard__ = True
    preprocess_request.__wrapped__ = preprocess
    batch_generator_cls._preprocess_request = preprocess_request

    # decode --------------------------------------------------------------------------------------------------------
    def process_image_input(image):
        paths = getattr(local, "paths", None)
        if paths is None:
            raise ValueError(f"{IMAGE_REFUSAL} (image input outside the guarded preprocessing)")
        if isinstance(image, dict):
            url = image.get("url", image.get("image_url", ""))
            image = url.get("url", "") if isinstance(url, dict) else url
        try:
            info, raw = image_check.check_data_url(image, max_bytes=policy.max_image_bytes,
                                                   max_pixels=policy.max_image_pixels)
        except image_check.ImageRefused as exc:
            raise ValueError(f"{IMAGE_REFUSAL} ({exc})") from None
        fd, path = tempfile.mkstemp(suffix=image_check.EXTENSIONS[info["kind"]], dir=tmp)
        paths.append(path)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return path

    process_image_input.__cortados_guard__ = True
    mllm_models.process_image_input = process_image_input


def _vision_modules():
    """vllm-mlx's MLLM modules, mlx-vlm, PIL and the image checker, verified against the vision pins."""
    import vllm_mlx
    import mlx_vlm
    verify_vision(os.path.dirname(os.path.abspath(vllm_mlx.__file__)), os.path.dirname(os.path.abspath(mlx_vlm.__file__)),
                  getattr(mlx_vlm, "__version__", "?"))
    from vllm_mlx import mllm_batch_generator, vision_embedding_cache
    from vllm_mlx.models import mllm
    return mllm_batch_generator, vision_embedding_cache, mllm, load_image_check()


def _modules():
    """vllm_mlx and fastapi from the venv, verified against the pins."""
    import vllm_mlx
    verify(os.path.dirname(os.path.abspath(vllm_mlx.__file__)), getattr(vllm_mlx, "__version__", "?"))
    from fastapi import HTTPException
    from vllm_mlx import mllm_scheduler, scheduler
    from vllm_mlx.api import utils as api_utils
    return vllm_mlx, scheduler, mllm_scheduler, api_utils, HTTPException


def _engines():
    """BaseEngine and BatchedEngine, with every engine module of 0.6.0 imported so its classes exist to be refused."""
    from vllm_mlx.engine import base, batched, simple  # noqa: F401  (simple: imported so SimpleEngine is patched)
    return base.BaseEngine, batched.BatchedEngine


def _usage(msg: str) -> int:
    print(f"cortados-vllm-mlx-guard: {msg}\nusage: cortados-vllm-mlx-guard.py --context-tokens N [vision flags] -- serve "
          "<model> [vllm-mlx args]\n       cortados-vllm-mlx-guard.py --check <model dir> [vision flags]\nvision flags: "
          "--vision " + " N ".join(VISION_FLAGS) + " N", file=sys.stderr)
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--check"]:
        if len(argv) < 2:
            return _usage("--check takes one model directory")
        try:
            policy, rest = parse_vision(argv[2:])
            if rest:
                return _usage("--check takes one model directory and the vision flags")
            _, _, _, api_utils, _ = _modules()
            check_engines(*_engines())            # the engine refusal would hold on this vllm-mlx (read only)
            if policy is not None:
                _vision_modules()                 # mlx-vlm's version and the vision pins
                check_vision_model(argv[1])
            route = api_utils.resolve_mllm_route(argv[1], argv[1])
        except GuardError as exc:
            print(f"cortados-vllm-mlx-guard: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except Exception as exc:                  # e.g. MllmRouteUndetermined: an unreadable config.json
            print(f"cortados-vllm-mlx-guard: the model route could not be resolved ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            return EXIT_REFUSED
        if policy is not None:
            if not getattr(route, "is_mllm", False):
                print(f"cortados-vllm-mlx-guard: vision mode, but {argv[1]} resolves to the text route",
                      file=sys.stderr)
                return EXIT_REFUSED
            print(f"cortados-vllm-mlx-guard: ok (vllm-mlx {VLLM_MLX_VERSION} and mlx-vlm {MLX_VLM_VERSION}, pinned "
                  f"files match, only BatchedEngine may serve, vision route {VISION_MODEL_TYPE})")
            return 0
        if getattr(route, "is_mllm", True):
            print(f"cortados-vllm-mlx-guard: {MLLM_REFUSAL}; {argv[1]} resolves to the MLLM route "
                  f"({getattr(route, 'source', '?')})", file=sys.stderr)
            return EXIT_REFUSED
        print(f"cortados-vllm-mlx-guard: ok (vllm-mlx {VLLM_MLX_VERSION}, pinned files match, only BatchedEngine "
              "may serve, text route)")
        return 0
    if "--" not in argv:
        return _usage("missing `--` before the vllm-mlx arguments")
    cut = argv.index("--")
    own, rest = argv[:cut], argv[cut + 1:]
    try:
        policy, own = parse_vision(own)
    except GuardError as exc:
        return _usage(str(exc))
    if len(own) != 2 or own[0] != "--context-tokens" or not own[1].isdigit():
        return _usage("expected --context-tokens N")
    if rest[:1] != ["serve"]:
        return _usage("only `serve` is run through the guard")
    if BATCHING_FLAG not in rest:
        return _usage(f"{ENGINE_REFUSAL} (`{BATCHING_FLAG}` missing from the serve arguments)")
    try:
        _, scheduler, mllm_scheduler, api_utils, http_exception = _modules()
        if policy is not None:
            batch_generator, vision_cache, mllm_models, image_check = _vision_modules()
        install(int(own[1]), scheduler.Scheduler, mllm_scheduler.MLLMScheduler, api_utils, http_exception,
                *_engines(), vision=policy)
        if policy is not None:
            import warnings
            from PIL import Image
            harden_pil(Image, warnings, policy.max_image_pixels)
            install_vision(int(own[1]), policy, mllm_scheduler_cls=mllm_scheduler.MLLMScheduler,
                           batch_generator_cls=batch_generator.MLLMBatchGenerator,
                           vision_cache_cls=vision_cache.VisionEmbeddingCache, mllm_models=mllm_models,
                           api_utils=api_utils, http_exception=http_exception, image_check=image_check)
    except GuardError as exc:
        print(f"cortados-vllm-mlx-guard: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    what = (f"; vision: {policy.max_images} images of at most {policy.image_tokens} tokens, image requests capped at "
            f"{policy.image_context_tokens}" if policy is not None else "")
    print(f"cortados-vllm-mlx-guard: prompt + max_tokens capped at {own[1]} tokens (vllm-mlx {VLLM_MLX_VERSION})"
          f"{what}", file=sys.stderr)
    from vllm_mlx import cli
    sys.argv = ["vllm-mlx", *rest]
    cli.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
