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
  * the prefill, MLLMBatchGenerator._run_vision_encoding (the one forward of an image-bearing prompt, upstream atomic
    and projecting every position to the vocabulary): replaced by media_prefill, chunked (--image-prefill-tokens) with
    logits_to_keep=1, so its memory is the computed bound the gate reserves (local_governor.image_prefill_bytes; the
    argument is at media_prefill below). --image-dims carries the model dimensions that bound was computed from, and
    startup stops unless config.json has exactly those.
Also: the processor's max_pixels set to image_tokens x 1,024 (upstream allows 16.7 MP = 16,384 tokens per image);
process_image_input takes checked data: URLs only (no download, no unbounded base64 temp-file cache) and its temp
files are removed after preprocessing; VisionEmbeddingCache (count-only, 100/50 entries) is disabled; PIL's bomb limit
is the pixel bound; drafters and specprefill are refused; and only a model dir whose config.json is qwen3_5 /
Qwen3_5ForConditionalGeneration with a vision_config (and the --image-dims dimensions) may serve. The text Scheduler
refuses every request in this mode.
The vision encoder's own attention (#125 rounds 3-4) is counted as fused with its padded width, which holds for ONE
verified geometry only: --image-dims must be exactly VISION_DIMS_ALLOWED (refused at --check and at serve, before
anything loads), config.json's vision_config exactly VISION_ENCODER_CONFIG (check_vision_model, at --check and at
route resolution) and the loaded tower the same (vision_tower_problem, when MLLMScheduler is built). And the fused
kernel's implementation is pinned: mlx and mlx-metal 0.32.4, their installed dist-info RECORD entries and the files
themselves (the mlx.core extension, libmlx.dylib, mlx.metallib) against the released macOS arm64 wheels' RECORDs
(MLX_PINNED_RECORD / MLX_METAL_PINNED_RECORD, verify_mlx), and the mlx.core actually imported must be that file.
Pinned on top: the MLLM files of vllm-mlx 0.6.0 (VISION_PINNED_SHA256) and mlx-vlm 0.7.12 (MLX_VLM_PINNED_SHA256: the
processor, the model, the vision encoder, the linear attention, the caches and the rest of their import chain;
docs/LOCAL-RESOURCES.md "Vision" lists them).

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
    # Model.__call__ (qwen3_vl.py 215-236), which qwen3_5.Model inherits: get_input_embeddings, then the language
    # model with **kwargs (so logits_to_keep reaches it); and masked_scatter (14-32), the merge the media prefill runs.
    "models/qwen3_vl/qwen3_vl.py": "12f0393421918eb1cbaa6daa447d93a405762e997f688676688e5c71d987d3ee",
    "models/qwen3_5/__init__.py": "d5a1cf335ae0845d0033dcc77dfdd6174f801aa77375cd8d57d69bc0c26f3d2b",
    "models/qwen3_5/qwen3_5.py": "7c0e360a35e1f5d3819677cd6f7e4d7349f4f3dd2fad5fc8f1e374bd514e8f1b",
    "models/qwen3_5/vision.py": "a1db23800b564caaedcc79d6a27d76e3186b9315cb68b612d61afbfa69487600",
    "models/qwen3_5/language.py": "9cd5b798c183ac70d6c4ba0bdfabb04d1da126cd133e25089958d24d674cadac",
    "models/qwen3_5/config.py": "b4db2144b162bbc27c0d843833fdd969413b44260ed1f024866eeadf1832dc0f",
    # #125 review round 3 (P2): every file the reservation and the chunk-equivalence argument read, beyond the above
    # (the qwen3_5 import chain, read from the same wheel; docs/LOCAL-RESOURCES.md "Vision" lists them all).
    # The vision encoder qwen3_5/vision.py only inherits: Attention (one image per split, ensure_fused_sdpa, no
    # windowed blocks), the blocks, PatchEmbed, the merger; and the VisionConfig defaults qwen3_5's inherits.
    "models/qwen3_vl/vision.py": "9885d83cf85e037a88f8364817007094a82b5941d49e909d2d9fe11b5e03bf6c",
    "models/qwen3_vl/config.py": "8b529cfbd568c7d202140b4d5212be7fe740310c1a80374fa7dd4e59b9763017",
    "models/qwen3_vl/__init__.py": "da4897a091e4ddce3d19e8dd01b7b52cc0ed5af484da0a4aa9b65804c2b8d808",
    "models/qwen3_vl/language.py": "f01e7b266a4083ffd64b180474e2a64c397077ebd31dd133b0eef890be1f2c71",
    # The linear (recurrent) attention: gated_delta_update, whose state carries across chunks.
    "models/qwen3_5/gated_delta.py": "71da97ef25a013e185c4d19168b39c452b90f40d5462f4d08ff084a82e922d2f",
    "models/qwen3_5/speculative_verifier.py": "32b632f3ade3bcdfdab61d280ac3c219edc3b7b54881a2fba8b06415396302e9",
    # The caches (KVCache's 256-step growth, ArraysCache) and the attention dispatch's cache types.
    "models/cache.py": "d2e9900b3cd0fdf7e5410c904bbc320b8773c24aeda8f60858a0b58cf9b6b36b",
    "turboquant.py": "7ea10cdb828b44f52dc53dff61a93ae86e6e88e4df145b39a2e3b10b39ccf85c",
    "kv_quant.py": "2936878096435dd2540e7a029b986259e5b7101c972a5be7168495a58e7fbfa3",
    # The rest of the chain: rotary embeddings, activations, the layers and kernels the model's modules use.
    "models/rope_utils.py": "6944f69a03c41cbb9613afbd2f1b54b71c504840aaeb0eade39c6109dbfdd25b",
    "models/activations.py": "2ce04422f5670d8c3507f723d33253632e14121f45f2aa8070b08544ea92dfc9",
    "models/fast_ops.py": "d4ebf917ded3911959e7dd37aeb78cd631b7e6ab29a57cfa1daad1e7ee606286",
    "models/linear.py": "148a56193feaf170097ff1c5edccca8165c1b5df878f1605778c4d3642a4d2fa",
    "models/mlp.py": "e5413aa08a61ee26ab3bf70fac9aeef2ac9c13342e2a4507c2e7cc908a3273e7",
    "models/switch_layers.py": "a8b688e0c0ed37ffe88804efe287fc19998eec9686b76f981b2e2ff635f76751",
    "models/quantized_verifier.py": "b01182801e18ca63ff1e0cb8be3146131c7204c91dfae656ab3c9ddb51256e13",
    "models/exact_speculative_verify.py": "170d189df3cf4be1d0044ea497e863f04160c6d9f11719462e43f201124b2b96",
    "quantization/nvfp4.py": "a1ef4a7e8d1d21fe32490273873ba182d6ae3618fb671147eb3363976ad7de5c",
    "speculative/cache_state.py": "bbc2af0ca2123b4c550133e142223575ef6b798b3475839d058ba806d1f1d8fa",
    "speculative/ops/linear.py": "6a42ec72dd65e9f304b65058d85945a36c9d7d8eab4bb70bcb96a080a219c5c9",
    # The package __init__ files importing the chain runs.
    "__init__.py": "609662121e39f1b1a945000d5cca0de94059b882c584dbf21cffeac3c5e02e7f",
    "models/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "speculative/__init__.py": "1f98ff6655dc4629b8d8c5001a32d5a95d232e156722be5b1adac848ff6f0559",
    "speculative/ops/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "quantization/__init__.py": "7dac848bf14e9db1a3da4bb3a8c30093c3a5fdfeb53ef8137af9234831b3fe1a",
}
# #125 review round 4 (P2): the fused kernel's implementation. ensure_fused_sdpa only pads and calls
# mlx.core.fast.scaled_dot_product_attention; which kernel that dispatches to (fused, or an unfused fallback that holds
# heads x N x N scores) is decided in the compiled mlx.core extension, libmlx.dylib and mlx.metallib. So mlx and
# mlx-metal are pinned to 0.32.4 (the Pro's installed versions) by their installed dist-info RECORD entries AND the
# files' own sha256, against the RECORDs of the released PyPI wheels (fetched 2026-11-16; each RECORD hash was checked
# against the file bytes in its wheel). Pinned: every macOS arm64 wheel of 0.32.4 (CPython 3.10-3.15, free-threaded
# builds included, macosx 14.0 / 15.0 / 26.0: the Pro's Python and macOS tag were not readable from the build machine);
# the installed WHEEL tag picks the row, and mlx's and mlx-metal's macOS tags must agree. Any other platform, version or byte fails closed (verify_mlx).
MLX_VERSION = "0.32.4"
# mlx (one row per wheel tag): the mlx.core extension, as RECORD names it and its RECORD hash.
MLX_PINNED_RECORD = {
    "cp310-cp310-macosx_14_0_arm64": ("mlx/core.cpython-310-darwin.so", "sha256=ozADsGv_W8ufM3WQaucH0w3iI8AigNLjt3KlMDXNWE4"),
    "cp310-cp310-macosx_15_0_arm64": ("mlx/core.cpython-310-darwin.so", "sha256=vk_Y_xORAxATEsDbekJQrgI35XveqdsLartsd7SokKU"),
    "cp310-cp310-macosx_26_0_arm64": ("mlx/core.cpython-310-darwin.so", "sha256=ubmrEwEjeXwlLSExQvV6FMRnf-y6WZBB_H10hYBDdb4"),
    "cp311-cp311-macosx_14_0_arm64": ("mlx/core.cpython-311-darwin.so", "sha256=6eHpkzhXoWWNXnQshFoZb4XpSaDgD7Jh0NvPL1Co8Yg"),
    "cp311-cp311-macosx_15_0_arm64": ("mlx/core.cpython-311-darwin.so", "sha256=O8eTCprg14wD9DN3VZDmtniWwFdqU-ukrlzcnp76nIw"),
    "cp311-cp311-macosx_26_0_arm64": ("mlx/core.cpython-311-darwin.so", "sha256=njvJ2z6Qrk7_U772V0Ad8pbmi466FaHlI-L3jI_CG7I"),
    "cp312-cp312-macosx_14_0_arm64": ("mlx/core.cpython-312-darwin.so", "sha256=ikDhN76IMe8run8XsKJR7IfkDomgqhaIf3ENpf7IdQQ"),
    "cp312-cp312-macosx_15_0_arm64": ("mlx/core.cpython-312-darwin.so", "sha256=Jf86zDz7li2aIcAY4OAPRLWTWIj3aTYWPgDCFZYwnmY"),
    "cp312-cp312-macosx_26_0_arm64": ("mlx/core.cpython-312-darwin.so", "sha256=AzYCMA_gZc7wlZEWkM5k9QuT7-JTg_5XXfEKudGBJg0"),
    "cp313-cp313-macosx_14_0_arm64": ("mlx/core.cpython-313-darwin.so", "sha256=7yA1QHijOOQ34BiN9qcU-5FJCQXTafsSNQDdEN8FPLo"),
    "cp313-cp313-macosx_15_0_arm64": ("mlx/core.cpython-313-darwin.so", "sha256=kgEm6ULPdHpdkYLEb5lROCbDiQgVKrhGwDEBPnXrs5Q"),
    "cp313-cp313-macosx_26_0_arm64": ("mlx/core.cpython-313-darwin.so", "sha256=LgmHIqREybUahLHOKG5OLBaAwUbIiRY82QqCGcPUTRA"),
    "cp313-cp313t-macosx_14_0_arm64": ("mlx/core.cpython-313t-darwin.so", "sha256=hwQKl1ND205Lt6bOUH9fhRgCIlOk7O2ptDd9vG1rJvU"),
    "cp313-cp313t-macosx_15_0_arm64": ("mlx/core.cpython-313t-darwin.so", "sha256=0HRHD3MMLD4i9YTqOOMNeGT43MJ_dJwZaZoQuunC0wk"),
    "cp313-cp313t-macosx_26_0_arm64": ("mlx/core.cpython-313t-darwin.so", "sha256=Rjjgq01INCeW-wI2ymeXNYf6zOg09HgDY0PP2jXivqQ"),
    "cp314-cp314-macosx_14_0_arm64": ("mlx/core.cpython-314-darwin.so", "sha256=l2tRufhC83Dp-0HMvd9TtPwjxQ8KV9Y45CYqh3v8sRQ"),
    "cp314-cp314-macosx_15_0_arm64": ("mlx/core.cpython-314-darwin.so", "sha256=Wd6Gx-mbRTpok_2EFUVZU7vwk9kn2fygitAXUGY7NdI"),
    "cp314-cp314-macosx_26_0_arm64": ("mlx/core.cpython-314-darwin.so", "sha256=V6xBxzY-SULesBzq-ny1U7Akxx_bx3xZStDciCSZQLE"),
    "cp314-cp314t-macosx_14_0_arm64": ("mlx/core.cpython-314t-darwin.so", "sha256=0iryGq6fv5Vmgt2HREbAWXgWAycR6EaMt5QPrtrsj0o"),
    "cp314-cp314t-macosx_15_0_arm64": ("mlx/core.cpython-314t-darwin.so", "sha256=97JD7RkVjuLybapNFXF5g9cIQITWgyqGoP8gSsnLejs"),
    "cp314-cp314t-macosx_26_0_arm64": ("mlx/core.cpython-314t-darwin.so", "sha256=WYIimOQwHm4pAzBsgBTyo3UvMwMHCPK2KNcgHs4BwdY"),
    "cp315-cp315-macosx_14_0_arm64": ("mlx/core.cpython-315-darwin.so", "sha256=QIs1iKFWnuAQyWxohmtfb2KYxKNqN86vHXq7uczYEgE"),
    "cp315-cp315-macosx_15_0_arm64": ("mlx/core.cpython-315-darwin.so", "sha256=QDdjJat5nNvtOVQs21FYbchcnooy9TBGTYfYZAjGmYQ"),
    "cp315-cp315-macosx_26_0_arm64": ("mlx/core.cpython-315-darwin.so", "sha256=GNmOuRVPyTle0sc34ZdJLcZ8zVWaIimJdj4xWZf0dFE"),
    "cp315-cp315t-macosx_14_0_arm64": ("mlx/core.cpython-315t-darwin.so", "sha256=-54upqKAxh6QNsYYbodgGFhPgE8HkUmtB1w3VCYVAwQ"),
    "cp315-cp315t-macosx_15_0_arm64": ("mlx/core.cpython-315t-darwin.so", "sha256=uUaHPMnk3xa3s3_oLchztUN0fwYhrHvvMLCPw5d21Ug"),
    "cp315-cp315t-macosx_26_0_arm64": ("mlx/core.cpython-315t-darwin.so", "sha256=KInAQpRORsuXfkhIXmJFpUoxbD3Z3xVv86Up4LDA3P0"),
}
# mlx-metal (one row per wheel tag): the C++ library and the Metal kernels.
MLX_METAL_PINNED_RECORD = {
    "py3-none-macosx_14_0_arm64": {
        "mlx/lib/libmlx.dylib": "sha256=dc71Z7mLHGM0KjV9q3vhzuX6ysjyAdnto_NEQvlB-mI",
        "mlx/lib/mlx.metallib": "sha256=1wqyE9TPqfJnPk2ywqRHTIatnMdnRWS-xN1KyUCdpnU",
    },
    "py3-none-macosx_15_0_arm64": {
        "mlx/lib/libmlx.dylib": "sha256=u-TuWrCiO7SpXGxe6d3HUgq7-91kYV7p-7OtIY1EeGs",
        "mlx/lib/mlx.metallib": "sha256=82sBxE3V8tkCDesaxq1T3Ahd6Brxk5ZpTFlj7zoSE38",
    },
    "py3-none-macosx_26_0_arm64": {
        "mlx/lib/libmlx.dylib": "sha256=2Mk5PVhOeBOTw-_8z8zYuoWtDHAmTtr5-JfcqYdTdcY",
        "mlx/lib/mlx.metallib": "sha256=xtej-suDVo9uTLLPSoxzbuKTAlcaSDQ8uaOk0c0-OJM",
    },
}

VISION_MODEL_TYPE = "qwen3_5"
VISION_ARCHITECTURES = ["Qwen3_5ForConditionalGeneration"]
# The runtime classes the argument was read against (module, class name).
VISION_MODEL_CLASS = ("mlx_vlm.models.qwen3_5.qwen3_5", "Model")
VISION_LANGUAGE_CLASS = ("mlx_vlm.models.qwen3_5.language", "LanguageModel")
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
                "--max-image-pixels": "max_image_pixels", "--image-prefill-tokens": "prefill_tokens"}
# --image-dims k=v,...: the model dimensions the gate's reservation was computed from (local_governor.VISION_DIM_KEYS
# and act_bytes, image_prefill_bytes); checked against the model's config.json at startup (check_vision_model).
DIMS_FLAG = "--image-dims"
# Each name, where config.json holds it: (sub-config, key).
IMAGE_DIMS = {
    "hidden_size": ("text_config", "hidden_size"),
    "intermediate_size": ("text_config", "intermediate_size"),
    "num_attention_heads": ("text_config", "num_attention_heads"),
    "num_key_value_heads": ("text_config", "num_key_value_heads"),
    "head_dim": ("text_config", "head_dim"),
    "linear_num_key_heads": ("text_config", "linear_num_key_heads"),
    "linear_num_value_heads": ("text_config", "linear_num_value_heads"),
    "linear_key_head_dim": ("text_config", "linear_key_head_dim"),
    "linear_value_head_dim": ("text_config", "linear_value_head_dim"),
    "vocab_size": ("text_config", "vocab_size"),
    "vision_hidden_size": ("vision_config", "hidden_size"),
    "vision_intermediate_size": ("vision_config", "intermediate_size"),
    "vision_num_heads": ("vision_config", "num_heads"),
    "vision_depth": ("vision_config", "depth"),
}
DIMS_KEYS = (*IMAGE_DIMS, "act_bytes")
# #125 review round 4: the ONE geometry vision mode serves (the Pro's Qwen3.8 27B, config.json checked 2026-11-16;
# local_governor.VISION_DEFAULTS / VISION_FIXED_KEYS hold the same values). The reservation's encoder term is argued
# for it only: head_dim 1,152 / 16 = 72, which mlx-vlm 0.7.12's ensure_fused_sdpa (models/base.py, pinned) pads to 80
# for MLX's fused kernel, counted at that padded width. Rounds 2 and 3 each found another geometry the general formula
# did not bound (8 heads, unfused; 288 heads of 4, padded to 64), so there is no general acceptance: a future model
# is added here explicitly, with its own reservation argument.
VISION_DIMS_ALLOWED = {
    "hidden_size": 5120, "intermediate_size": 17408, "num_attention_heads": 24, "num_key_value_heads": 4,
    "head_dim": 256, "linear_num_key_heads": 16, "linear_num_value_heads": 48, "linear_key_head_dim": 128,
    "linear_value_head_dim": 128, "vocab_size": 248320, "vision_hidden_size": 1152, "vision_intermediate_size": 4304,
    "vision_num_heads": 16, "vision_depth": 27, "act_bytes": 2,
}
# config.json's vision_config, exactly (every key present: mlx-vlm's VisionConfig defaults differ, e.g. depth 32 and
# patch_size 14), plus in_channels 3 (its default when absent). The vision encoder (qwen3_vl/vision.py Attention,
# pinned) runs every block as full attention over one image at a time (cu_seqlens splits; window_size and
# fullatt_block_indexes are read into its config and used by no block).
VISION_ENCODER_CONFIG = {"depth": 27, "num_heads": 16, "hidden_size": 1152, "intermediate_size": 4304,
                         "patch_size": 16, "spatial_merge_size": 2, "temporal_patch_size": 2, "out_hidden_size": 5120}
VISION_TOWER_GEOMETRY = {"patch_size": 16, "spatial_merge_size": 2, "temporal_patch_size": 2, "in_channels": 3}
VISION_HEAD_DIM = VISION_ENCODER_CONFIG["hidden_size"] // VISION_ENCODER_CONFIG["num_heads"]          # 72
# Bytes per element of the dtypes a config.json may name (the activations are computed in it).
DTYPE_BYTES = {"bfloat16": 2, "float16": 2, "float32": 4}


class VisionInputRefused(ValueError):
    """Layer 2: a prepared request that disagrees with its bound (raised inside _preprocess_request)."""


class VisionPolicy:
    """The still-image policy the guard enforces (local_backend.vision_argv passes the model entry's `vision`)."""

    def __init__(self, max_images: int, image_tokens: int, image_context_tokens: int, max_image_bytes: int,
                 max_image_pixels: int, prefill_tokens: int, dims: dict | None = None):
        self.max_images, self.image_tokens = max_images, image_tokens
        self.image_context_tokens = image_context_tokens
        self.max_image_bytes, self.max_image_pixels = max_image_bytes, max_image_pixels
        self.prefill_tokens = prefill_tokens
        for k, v in vars(self).items():
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise GuardError(f"vision: {k} must be a positive integer, got {v!r}")
        if image_tokens >= image_context_tokens:
            raise GuardError("vision: --image-tokens must be below --image-context-tokens")
        if prefill_tokens > image_context_tokens:
            raise GuardError("vision: --image-prefill-tokens must not exceed --image-context-tokens")
        dims = dict(dims or {})
        if sorted(dims) != sorted(DIMS_KEYS) or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
                                                    for v in dims.values()):
            raise GuardError(f"vision: {DIMS_FLAG} needs exactly {', '.join(DIMS_KEYS)} as positive integers")
        for k, want in VISION_DIMS_ALLOWED.items():
            if dims[k] != want:
                raise GuardError(f"vision: {DIMS_FLAG} {k}={dims[k]} is not the verified geometry ({k}={want}); "
                                 "vision mode serves that one geometry only (VISION_DIMS_ALLOWED)")
        self.dims = dims

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
        elif a == DIMS_FLAG:
            got["dims"] = parse_dims(args[i + 1] if i + 1 < len(args) else "")
            i += 1
        else:
            rest.append(a)
        i += 1
    if not on:
        if got:
            raise GuardError("image flags need --vision")
        return None, rest
    missing = [f for f, k in [*VISION_FLAGS.items(), (DIMS_FLAG, "dims")] if k not in got]
    if missing:
        raise GuardError(f"--vision needs {', '.join(missing)}")
    return VisionPolicy(**got), rest


def parse_dims(text: str) -> dict:
    """--image-dims' value, `name=N,name=N,...`, as a dict (VisionPolicy checks the names). GuardError if malformed."""
    out = {}
    for part in text.split(","):
        name, eq, value = part.partition("=")
        if not eq or not value.isdigit() or name in out:
            raise GuardError(f"{DIMS_FLAG} takes name=N,name=N,... (got {text!r})")
        out[name] = int(value)
    return out


def check_vision_model(model_dir: Any, dims: dict | None = None) -> None:
    """GuardError unless the model directory's config.json is the verified route: model_type qwen3_5,
    architectures [Qwen3_5ForConditionalGeneration] and a vision_config (the tower). With `dims` (the policy's
    --image-dims): every dimension the gate's image reservation was computed from equals config.json's, and the
    config's dtype (when it names one) is no wider than the act_bytes counted; a different model needs its
    reservation recomputed, so vision mode does not start on it."""
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
    if dims is None:
        return
    for name, (section, key) in IMAGE_DIMS.items():
        sub = cfg.get(section)
        got = sub.get(key) if isinstance(sub, dict) else None
        if got != dims[name]:
            raise GuardError(f"vision mode: config.json {section}.{key} is {got!r}, but the gate's image reservation "
                             f"was computed for {dims[name]} ({DIMS_FLAG} {name}); recompute it for this model")
    vc = cfg["vision_config"]
    for key, want in (*VISION_ENCODER_CONFIG.items(), ("in_channels", 3)):
        got = vc.get(key, 3 if key == "in_channels" else None)
        if isinstance(got, bool) or got != want:
            raise GuardError(f"vision mode: config.json vision_config.{key} is {got!r}, not {want}: vision mode serves "
                             "the one verified encoder geometry (VISION_ENCODER_CONFIG)")
    if vc.get("deepstack_visual_indexes"):
        raise GuardError("vision mode: config.json vision_config.deepstack_visual_indexes is set (qwen3_5 has none)")
    text = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else {}
    for where in (text, cfg):
        dtype = where.get("dtype", where.get("torch_dtype"))
        if dtype is None:
            continue
        if DTYPE_BYTES.get(dtype, 0) == 0 or DTYPE_BYTES[dtype] > dims["act_bytes"]:
            raise GuardError(f"vision mode: config.json's dtype {dtype!r} is not covered by act_bytes "
                             f"{dims['act_bytes']}")


def vision_tower_problem(tower: Any, dims: dict) -> str | None:
    """The loaded vision tower against the verified geometry, or None when it agrees: the model as built
    (qwen3_vl/vision.py VisionModel), not only its config.json. VISION_ENCODER_CONFIG's depth blocks, every block's
    attention 16 heads of 72, and the patch geometry the reservation's. `dims` is the policy's (VISION_DIMS_ALLOWED)."""
    blocks = getattr(tower, "blocks", None)
    if tower is None or not isinstance(blocks, (list, tuple)):
        return "is missing or has no blocks"
    if dims != VISION_DIMS_ALLOWED:
        return "was checked against dimensions other than the verified geometry"
    if len(blocks) != VISION_ENCODER_CONFIG["depth"]:
        return f"has {len(blocks)} blocks, not {VISION_ENCODER_CONFIG['depth']}"
    want_h = VISION_ENCODER_CONFIG["num_heads"]
    for n, blk in enumerate(blocks):
        attn = getattr(blk, "attn", None)
        heads, d = getattr(attn, "num_heads", None), getattr(attn, "head_dim", None)
        if isinstance(heads, bool) or isinstance(d, bool) or heads != want_h or d != VISION_HEAD_DIM:
            return f"block {n} attention has {heads!r} heads of {d!r}, not {want_h} of {VISION_HEAD_DIM}"
    pe = getattr(tower, "patch_embed", None)
    got = {"patch_size": getattr(pe, "patch_size", None), "temporal_patch_size": getattr(pe, "temporal_patch_size", None),
           "spatial_merge_size": getattr(tower, "spatial_merge_size", None),
           "in_channels": getattr(pe, "in_channels", None)}
    if got != VISION_TOWER_GEOMETRY:
        return f"geometry is {got}, not {VISION_TOWER_GEOMETRY}"
    return None


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


def _record_hash(path: str) -> str:
    """A file's hash in a RECORD's form: sha256= and the urlsafe base64 digest without padding."""
    import base64
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256=" + base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode()


def verify_mlx(find: Any = None, core_file: Any = None, pins: dict | None = None,
               metal_pins: dict | None = None) -> str:
    """GuardError unless the installed mlx and mlx-metal are the released 0.32.4 wheels for this platform (module
    docstring; MLX_PINNED_RECORD): the version, one WHEEL tag that has a pinned row, the RECORD entries of the files
    that implement fast.scaled_dot_product_attention's dispatch equal to the release's, those files' bytes equal to
    them, and the mlx.core imported (`core_file`, else mlx.core.__file__) that very file, beside that libmlx.dylib.
    `find` is importlib.metadata.distribution (tests pass a fake). Returns the mlx wheel tag."""
    if find is None:
        from importlib import metadata
        find = metadata.distribution
    found = {}
    for name, table in (("mlx", MLX_PINNED_RECORD if pins is None else pins),
                        ("mlx_metal", MLX_METAL_PINNED_RECORD if metal_pins is None else metal_pins)):
        try:
            dist = find(name)
        except Exception as exc:                  # importlib.metadata.PackageNotFoundError
            raise GuardError(f"{name} is not installed ({type(exc).__name__}): vision mode needs {name} "
                             f"{MLX_VERSION}") from None
        version = dist.version
        if version != MLX_VERSION:
            raise GuardError(f"{name} {version} is not {MLX_VERSION}: re-read the guard's vision argument first")
        tags = [ln.split(":", 1)[1].strip() for ln in (dist.read_text("WHEEL") or "").splitlines()
                if ln.startswith("Tag:")]
        if len(tags) != 1 or tags[0] not in table:
            raise GuardError(f"{name} {MLX_VERSION}: wheel tag {tags!r} is not one of the pinned releases")
        want = table[tags[0]]
        if name == "mlx":
            want = {want[0]: want[1]}
        record = {}
        for ln in (dist.read_text("RECORD") or "").splitlines():
            parts = ln.rsplit(",", 2)
            if len(parts) == 3:
                record[parts[0]] = parts[1]
        paths = {}
        for rel, h in want.items():
            if record.get(rel) != h:
                raise GuardError(f"{name} {MLX_VERSION}: RECORD's {rel} is not the released wheel's ({tags[0]})")
            path = os.path.realpath(str(dist.locate_file(rel)))
            try:
                got = _record_hash(path)
            except OSError as exc:
                raise GuardError(f"{name}/{rel} unreadable ({type(exc).__name__})") from None
            if got != h:
                raise GuardError(f"{name}/{rel} differs from the released {MLX_VERSION} wheel ({tags[0]})")
            paths[rel] = path
        found[name] = (tags[0], paths)
    (mlx_tag, mlx_paths), (metal_tag, metal_paths) = found["mlx"], found["mlx_metal"]
    if mlx_tag.split("-")[-1] != metal_tag.split("-")[-1]:
        raise GuardError(f"mlx ({mlx_tag}) and mlx-metal ({metal_tag}) are built for different platforms")
    if core_file is None:
        import mlx.core
        core_file = getattr(mlx.core, "__file__", None)
    core = os.path.realpath(str(core_file)) if core_file else None
    if core != next(iter(mlx_paths.values())):
        raise GuardError(f"the imported mlx.core ({core_file}) is not the pinned mlx {MLX_VERSION} extension")
    lib = os.path.realpath(os.path.join(os.path.dirname(core), "lib", "libmlx.dylib"))
    if lib != metal_paths["mlx/lib/libmlx.dylib"]:
        raise GuardError("the imported mlx.core does not sit beside the pinned mlx-metal libmlx.dylib")
    return mlx_tag


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
      caches       VisionEmbeddingCache built disabled (and MLLMBatchGenerator's instance disabled again);
      prefill      MLLMBatchGenerator._run_vision_encoding (the one media prefill): chunked, and only the last
                   position projected to the vocabulary (media_prefill and the "Media prefill" comment above it)."""
    if getattr(mllm_scheduler_cls.add_request, "__cortados_guard__", False):
        raise GuardError("the vision guard is already installed")
    seam = media_prefill_seam(batch_generator_cls)          # before anything is patched: a drifted seam stops here
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
            check_vision_model(resolved_path or model_name, policy.dims)
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
        why = vision_tower_problem(getattr(model, "vision_tower", None), policy.dims)
        if why is not None:
            raise GuardError(f"vision mode: the loaded vision tower {why}")
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
        if not _same(self.model, VISION_MODEL_CLASS) \
                or not _same(getattr(self.model, "language_model", None), VISION_LANGUAGE_CLASS):
            raise GuardError("vision mode: the batch generator's model is not mlx-vlm's qwen3_5 Model / LanguageModel")
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

    # prefill -------------------------------------------------------------------------------------------------------
    def run_vision_encoding(self, request, cache=None):
        if getattr(self, "_cortados_vision", None) is None:
            raise VisionInputRefused(f"{PREPARED_REFUSAL} (batch generator not set up by the guard)")
        return media_prefill(self, request, cache, policy.prefill_tokens, seam)

    run_vision_encoding.__cortados_guard__ = True
    run_vision_encoding.__wrapped__ = seam["original"]
    batch_generator_cls._run_vision_encoding = run_vision_encoding


# ---------------------------------------------------------------------------------------------------------------------
# Media prefill (#125 review round 2). The released route prefills an image-bearing prompt in ONE forward:
# _process_prompts (mllm_batch_generator.py 2233-2261) sends every media request on a cache miss to
# _run_vision_encoding (1615-1662), which calls self.model(input_ids, cache=cache, **kwargs) over the whole prompt
# with no logits_to_keep, so qwen3_5's LanguageModel (language.py 1910-1920) projects EVERY position to the 248,320-
# word vocabulary (base.py project_logits 64-80 keeps all when logits_to_keep is 0/None) before the scheduler keeps
# the last row: 27.8 GiB of bfloat16 logits at 60k tokens. Unchunked, its attention (head_dim 256, outside MLX's fused
# kernel: base.py ensure_fused_sdpa) would also hold heads x P x P scores.
# The guard replaces _run_vision_encoding with media_prefill, which is mlx-vlm 0.7.12's own chunked VLM prefill
# (generate/ar.py 422-517, enabled for qwen3_5 by LanguageModel.chunked_prefill_policy, 1487-1507, when there is no
# drafter, which vision mode refuses) on vllm-mlx's cache and bookkeeping:
#   1. the embeddings once, exactly as Model.__call__ (qwen3_vl.py 224-233) builds them: get_input_embeddings runs the
#      vision tower and merges the features (masked_scatter), and its output (inputs_embeds, position_ids,
#      rope_deltas; attention_mask None) overrides the request's kwargs;
#   2. the language model over chunks of `step` tokens: input_ids and inputs_embeds sliced, position_ids sliced to the
#      chunk (the full prompt's mRoPE positions, as vllm-mlx's own chunked text prefill passes them,
#      _language_model_kwargs 1376-1394), every other kwarg as the atomic call had it, and logits_to_keep=1;
#      between chunks the cache is evaluated (_eval_prompt_cache, as _run_chunked_text_prefill does, so the lazy
#      graph never spans the prompt) and MLX's buffer cache cleared (the grown scores and replaced KV buffers);
#   3. then what the original does after its forward: vision_encoded, the inputs released, the logits returned.
# Why it is the same result: logits_to_keep slices only the hidden states handed to the vocabulary projection, after
# the model has run every position and written every position's K/V and recurrent state into the cache (project_logits
# docstring: "never to caches"); the projection is row-wise, so the kept row is the computation the scheduler's
# logits[:, -1, :] read from the full matrix (on Metal a one-row matmul may take another kernel than a many-row one,
# the same rounding-level difference decode has). Chunking is causal attention against the cached keys plus the
# gated-delta recurrence carried in the cache, the path vllm-mlx's text prefill and mlx-vlm's generate take for this
# model. A short prompt (one chunk) is the atomic call with logits_to_keep=1. A one-token chunk takes the ordinary
# forward too: the LanguageModel's one-token shortcut needs a _batch_invariant_decode hook, which neither pinned release
# sets on qwen3_5 (language.py 1872-1892; only qwen4_exp defines one).
# The seam is pinned: the files above (mllm_batch_generator.py, qwen3_vl.py, qwen3_5 language.py, base.py) by sha256;
# media_prefill_seam refuses a generator class without what media_prefill calls; MLLMBatchGenerator.__init__ checks the
# model is qwen3_5's Model with its LanguageModel; media_prefill refuses logits that are not one position.
def media_prefill_seam(batch_generator_cls: Any) -> dict:
    """What media_prefill needs from vllm-mlx's batch generator module, or GuardError (read only)."""
    original = getattr(batch_generator_cls, "_run_vision_encoding", None)
    if not callable(original) or getattr(original, "__cortados_guard__", False):
        raise GuardError("vision mode: MLLMBatchGenerator._run_vision_encoding is missing or already patched: "
                         "re-read the guard's media prefill argument")
    if not callable(getattr(batch_generator_cls, "_release_preprocessed_inputs", None)):
        raise GuardError("vision mode: MLLMBatchGenerator._release_preprocessed_inputs is missing")
    mod = sys.modules.get(batch_generator_cls.__module__)
    eval_cache = getattr(mod, "_eval_prompt_cache", None)
    aborted = getattr(mod, "PrefillAbortedError", None)
    clear = getattr(getattr(mod, "mx", None), "clear_cache", None)
    if not callable(eval_cache) or not callable(clear) or not (isinstance(aborted, type)
                                                               and issubclass(aborted, BaseException)):
        raise GuardError("vision mode: vllm_mlx.mllm_batch_generator lacks _eval_prompt_cache, PrefillAbortedError or "
                         "mx.clear_cache: re-read the guard's media prefill argument")
    return {"original": original, "eval_cache": eval_cache, "aborted": aborted, "clear_cache": clear}


def _positions(x: Any, start: int, end: int) -> Any:
    """Position ids for tokens start..end: the sequence is the LAST axis, (B, P) or mRoPE's (3, B, P) (as
    _language_model_kwargs slices them, mllm_batch_generator.py 1388-1393)."""
    return x[:, :, start:end] if x.ndim == 3 else x[:, start:end]


def media_prefill(gen: Any, request: Any, cache: Any, step: int, seam: dict) -> Any:
    """The guarded _run_vision_encoding (comment above): the prompt's embeddings once, then the language model in
    chunks of `step` tokens with logits_to_keep=1. Returns the (1, 1, vocab) logits of the last position."""
    if cache is None:
        raise RuntimeError(f"{PREPARED_REFUSAL} (a media prefill without a prompt cache)")
    kwargs = dict(request.extra_kwargs)                    # as the original builds them (1633-1640)
    if request.attention_mask is not None:
        kwargs["attention_mask"] = request.attention_mask
    if request.image_grid_thw is not None:
        kwargs["image_grid_thw"] = request.image_grid_thw
    pixel_values = request.pixel_values
    input_ids = request.input_ids
    if input_ids.ndim == 1:
        input_ids = input_ids[None, :]
    model = gen.model
    feats = model.get_input_embeddings(input_ids, pixel_values, **kwargs)         # qwen3_vl.py 224
    lm_kwargs = {**kwargs, "pixel_values": pixel_values, **feats.to_dict()}       # qwen3_vl.py 228-233
    embeds = lm_kwargs.pop("inputs_embeds")
    positions = lm_kwargs.pop("position_ids")
    if embeds is None or positions is None:
        raise RuntimeError(f"{PREPARED_REFUSAL} (the embeddings carry no inputs_embeds or position_ids)")
    lm_kwargs["logits_to_keep"] = 1
    lm = model.language_model
    total = input_ids.shape[1]
    start, out = 0, None
    while start < total:
        if request.request_id in gen._aborted_request_ids:   # a disconnect between chunks (as the text prefill)
            gen._aborted_request_ids.discard(request.request_id)
            raise seam["aborted"](request.request_id)
        end = min(start + step, total)
        # input_ids (B, P) and inputs_embeds (B, P, hidden): the sequence is axis 1 (generate/ar.py 499-500)
        out = lm(input_ids[:, start:end], inputs_embeds=embeds[:, start:end], cache=cache,
                 position_ids=_positions(positions, start, end), **lm_kwargs)
        start = end
        gen._prefill_progress[request.request_id] = (start, total)
        if start < total:
            seam["eval_cache"](cache)
            seam["clear_cache"]()
    del embeds, positions, feats, lm_kwargs
    request.vision_encoded = True
    gen._release_preprocessed_inputs(request)             # as the original, after its forward (1653-1656)
    logits = out.logits if hasattr(out, "logits") else out
    if logits.ndim != 3 or logits.shape[1] != 1:
        raise RuntimeError(f"{PREPARED_REFUSAL} (the media prefill returned logits for {logits.shape} positions, "
                           "not the one kept)")
    return logits


# ---------------------------------------------------------------------------------------------------------------------
# Text prefill (#126; docs/LOCAL-RESOURCES.md "Text prefill"). On the text route, vllm-mlx 0.6.0's Scheduler
# (scheduler.py 1518-1600, _create_batch_generator) builds mlx-lm 0.31.3's BatchGenerator with vllm-mlx's
# --prefill-step-size (2,048) and --prefill-batch-size (8). mlx-lm prefills (generate.py 1124-1170,
# PromptProcessingBatch.prompt) up to prefill_batch_size sequences together, right-padded, against one BatchKVCache, in
# steps of prefill_step_size tokens, evaluating only the cache state (no prompt position is projected to the
# vocabulary). Its full attention at head_dim 256 runs MLX 0.32.4's unfused fallback under 1,024 query tokens
# (scaled_dot_product_attention.cpp use_fallback; fast.cpp's fallback: bf16 scores, a masked copy, the softmax), so one
# step holds sequences x heads x step x context scores: 8 x 24 x 2,048 x 131,072 x 2 bytes = 96 GiB a buffer at worst.
# The guard bounds it where the generator is built:
#   * every BatchGenerator the Scheduler creates prefills ONE sequence at a time (prefill_batch_size 1) in chunks of
#     at most --text-prefill-tokens (prefill_step_size, and the empty PromptProcessingBatch's own step, which is the
#     one PromptProcessingBatch.prompt chunks by, since extend() keeps it); a generator without that seam, or a model
#     other than mlx-lm's qwen3_5, fails the request (fail closed);
#   * MLX's buffer cache limit is --buffer-cache-bytes (engine/batched.py _resolve_metal_buffer_cache_limit, which
#     vllm-mlx calls through its module global at engine start; upstream 90% of the GPU's working set): freed KV
#     copies and score buffers are released rather than kept resident until the next clear. The limit is a recycling
#     threshold (MLX allocator: free() caches a whole buffer while the cache is under the limit, malloc trims it back),
#     so the cache holds at most the limit plus one buffer; the gate counts both.
# Same result: a chunk boundary only splits the prompt into causal attention against the cached keys plus the
# gated-delta and conv state carried in the cache, the path upstream already takes at 2,048; batching prefills is a
# scheduling choice (each row's attention is masked to its own tokens). Floating-point results may differ at the
# rounding level, as between any two chunk sizes (GEMM tiling depends on the chunk's rows); no token is dropped.
# The gate reserves the result (local_governor.text_set_bytes). Pinned: mlx-lm 0.31.3 (MLX_LM_PINNED_SHA256), the
# vllm-mlx files above (PINNED_SHA256: scheduler.py, engine/batched.py) and mlx / mlx-metal 0.32.4 (verify_mlx: the
# fallback and the allocator are in the compiled library).
MLX_LM_VERSION = "0.31.3"
# mlx-lm 0.31.3 (PyPI wheel mlx_lm-0.31.3-py3-none-any.whl, sha256 758cfddf...83b8): the batch generator, the caches
# and mask, the qwen3_5 model and its import chain, and the sampler the decode rows run.
MLX_LM_PINNED_SHA256 = {
    "__init__.py": "f9ffa88772d26e537a98aa39ab16488a7a0d13cc1fac5d665376132c94b49608",
    "_version.py": "f0da9bc5c5c1bf21d576f7aa67b4eda887f1c7f0666746187b493e6831c4af6c",
    # BatchGenerator, PromptProcessingBatch (prefill steps, cache-state-only eval), GenerationBatch (extend / filter)
    "generate.py": "270778ad53eaca55a8533d82e6752660fe5d2605c4aa0879b48a50a91f69345f",
    "sample_utils.py": "c0ce439f8dbf0d4e6d0f37f728a324f3f72878e6a9123df41be520804c596d67",
    "models/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    # KVCache / BatchKVCache (256-step growth, extend pads to the longest row), ArraysCache (the recurrent state)
    "models/cache.py": "819ed95dcbf755652363cfdb15a639890447abb534a06dcefd52c7fff5055750",
    # create_causal_mask, scaled_dot_product_attention -> mx.fast.scaled_dot_product_attention
    "models/base.py": "61330e1c065739cd712bfeb09d673f33797cde7e613e95bf6d9ebbee9006f373",
    "models/qwen3_5.py": "f0daa30bba5cb521c8bdfa7093101a544c6a37bbba09bca582288219cb04ae3a",
    "models/qwen3_next.py": "3c572fe3fbb36721efab4d80d1bb6af11beb4ad1caae18deefc9fc84cbcd9b79",
    "models/gated_delta.py": "79c8376a51c694b03e54d2f996ced6ea6c8c42868b8571529f97334db165a3e1",
    "models/activations.py": "dbca5bae41ba0a8380a53903c3e98da37c1e15b46383bc2edb5806ba94fafe72",
    "models/rope_utils.py": "9f68c938c040fa111d13f2ed95c70e8261515fb3b54f8a0a474c096baf4e087a",
    "models/switch_layers.py": "073a6a808d5c90bb699a2ecca0e559b06727ae96dbc1f0253e4c7e77e4ee1ef2",
}
TEXT_FLAGS = {"--text-prefill-tokens": "prefill_tokens", "--buffer-cache-bytes": "buffer_cache_bytes"}
# The one verified text geometry (local_governor.TEXT_DIMS, the same values): the Pro's Qwen3.8 27B text_config. A key
# config.json leaves out takes mlx-lm 0.31.3's TextModelArgs default (models/qwen3_5.py 25-70) when that default is the
# verified value (full_attention_interval 4, linear_conv_kernel_dim 4), and is refused otherwise.
TEXT_DIMS = {
    "hidden_size": 5120, "intermediate_size": 17408, "num_attention_heads": 24, "num_key_value_heads": 4,
    "head_dim": 256, "linear_num_key_heads": 16, "linear_num_value_heads": 48, "linear_key_head_dim": 128,
    "linear_value_head_dim": 128, "vocab_size": 248320, "num_hidden_layers": 64, "full_attention_interval": 4,
    "linear_conv_kernel_dim": 4,
}
TEXT_DIMS_DEFAULTS = {"full_attention_interval": 4, "linear_conv_kernel_dim": 4}
TEXT_ACT_BYTES = 2
TEXT_MODEL_TYPE = "qwen3_5"
TEXT_MODEL_MODULES = ("mlx_lm.models.qwen3_5",)
TEXT_PREFILL_REFUSAL = "cortados guard: the text prefill could not be bounded"


class TextPolicy:
    """The text route's prefill bounds (--text-prefill-tokens, --buffer-cache-bytes), positive integers."""

    def __init__(self, prefill_tokens: int, buffer_cache_bytes: int):
        for name, v in (("--text-prefill-tokens", prefill_tokens), ("--buffer-cache-bytes", buffer_cache_bytes)):
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise GuardError(f"{name} must be a positive integer, got {v!r}")
        self.prefill_tokens, self.buffer_cache_bytes = prefill_tokens, buffer_cache_bytes


def parse_text(args: list[str]) -> tuple["TextPolicy | None", list[str]]:
    """(TextPolicy or None, the other arguments): the text flags, each `--flag N` once; both or neither."""
    got: dict[str, int] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in TEXT_FLAGS:
            if a in got or i + 1 >= len(args) or not args[i + 1].isdigit():
                raise GuardError(f"{a} takes one positive integer, once")
            got[a] = int(args[i + 1])
            i += 2
            continue
        rest.append(a)
        i += 1
    if not got:
        return None, rest
    if len(got) != len(TEXT_FLAGS):
        raise GuardError(f"the text flags go together: {' N '.join(TEXT_FLAGS)} N")
    return TextPolicy(got["--text-prefill-tokens"], got["--buffer-cache-bytes"]), rest


def check_text_model(model_dir: Any) -> None:
    """GuardError unless config.json (its text_config when it has one, else the top level) is the verified text
    geometry TEXT_DIMS, dense (no experts), in a dtype no wider than TEXT_ACT_BYTES: the gate's text reservation was
    computed for it (local_governor.text_set_bytes), so the text route does not start on another model."""
    import json
    path = os.path.join(str(model_dir), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        raise GuardError(f"text route: {path} unreadable ({type(exc).__name__})") from None
    if not isinstance(cfg, dict):
        raise GuardError(f"text route: {path} is not an object")
    # mlx-lm 0.31.3 loads mlx_lm.models.<model_type> (utils._get_classes; no remapping for qwen3_5), whose ModelArgs
    # reads text_config when there is one and the top level otherwise (models/qwen3_5.py 360-364): the module the
    # runtime check (TEXT_MODEL_MODULES) expects, caught here at install time rather than on the first request
    if cfg.get("model_type") != TEXT_MODEL_TYPE:
        raise GuardError(f"text route: config.json model_type is {cfg.get('model_type')!r}, not {TEXT_MODEL_TYPE!r} "
                         "(mlx-lm's qwen3_5, the model the text reservation was computed for)")
    text = cfg["text_config"] if "text_config" in cfg else cfg
    if not isinstance(text, dict):
        raise GuardError("text route: config.json text_config is not an object")
    for key, want in TEXT_DIMS.items():
        got = text.get(key, TEXT_DIMS_DEFAULTS.get(key))
        if isinstance(got, bool) or got != want:
            raise GuardError(f"text route: config.json {key} is {got!r}, but the gate's text reservation was computed "
                             f"for {want} (TEXT_DIMS); a different model needs its own reservation")
    if text.get("num_experts", 0):
        raise GuardError("text route: config.json names experts; the text reservation is for the dense model")
    for where in (text, cfg):
        dtype = where.get("dtype", where.get("torch_dtype"))
        if dtype is not None and (DTYPE_BYTES.get(dtype, 0) == 0 or DTYPE_BYTES[dtype] > TEXT_ACT_BYTES):
            raise GuardError(f"text route: config.json's dtype {dtype!r} is wider than the {TEXT_ACT_BYTES} bytes "
                             "the text reservation counts")


def verify_text(lm_dir: str, lm_version: str, pins: dict | None = None) -> None:
    """GuardError unless mlx-lm is the 0.31.3 release the text prefill argument was read from, file for file."""
    if lm_version != MLX_LM_VERSION:
        raise GuardError(f"mlx-lm {lm_version} is not {MLX_LM_VERSION}: re-read the guard's text prefill argument")
    for rel, want in (MLX_LM_PINNED_SHA256 if pins is None else pins).items():
        path = os.path.join(lm_dir, *rel.split("/"))
        try:
            with open(path, "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
        except OSError as exc:
            raise GuardError(f"mlx_lm/{rel} unreadable ({type(exc).__name__})") from None
        if got != want:
            raise GuardError(f"mlx_lm/{rel} differs from the {MLX_LM_VERSION} release the guard was written against")


def text_prefill_problem(bg: Any) -> str | None:
    """Why this BatchGenerator lacks the seam enforce_text_prefill sets, or None (mlx-lm 0.31.3's native layout)."""
    for attr in ("prefill_batch_size", "prefill_step_size", "_prompt_batch", "_generation_batch",
                 "_unprocessed_sequences", "_next", "model"):
        if not hasattr(bg, attr):
            return f"the batch generator has no {attr}"
    pb = bg._prompt_batch
    if not hasattr(pb, "prefill_step_size") or not hasattr(pb, "uids") or len(pb.uids) != 0:
        return "the batch generator's prompt batch is not mlx-lm 0.31.3's empty PromptProcessingBatch"
    module = type(bg.model).__module__
    if module not in TEXT_MODEL_MODULES:
        return f"the served model is {module}.{type(bg.model).__name__}, not mlx-lm's qwen3_5 (TEXT_DIMS)"
    for name in ("prefill_batch_size", "prefill_step_size"):
        v = getattr(bg, name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            return f"the batch generator's {name} is {v!r}"
    return None


def enforce_text_prefill(bg: Any, chunk: int) -> Any:
    """One sequence prefills at a time, in chunks of at most `chunk` tokens (module comment above); RuntimeError (the
    request fails, nothing is prefilled) when the generator is not the layout the argument read."""
    why = text_prefill_problem(bg)
    if why is not None:
        raise RuntimeError(f"{TEXT_PREFILL_REFUSAL} ({why})")
    bg.prefill_batch_size = 1
    bg.prefill_step_size = min(bg.prefill_step_size, chunk)
    bg._prompt_batch.prefill_step_size = min(int(bg._prompt_batch.prefill_step_size), chunk)
    return bg


def install_text(policy: TextPolicy, scheduler_cls: Any, batched_module: Any) -> None:
    """Patch the text route (before any request exists): every BatchGenerator the Scheduler builds is held to the
    policy's prefill (enforce_text_prefill), and MLX's buffer cache limit vllm-mlx sets at engine start is the
    policy's."""
    original = getattr(scheduler_cls, "_create_batch_generator", None)
    resolve = getattr(batched_module, "_resolve_metal_buffer_cache_limit", None)
    if not callable(original) or not callable(resolve):
        raise GuardError("text route: Scheduler._create_batch_generator or engine.batched."
                         "_resolve_metal_buffer_cache_limit is missing: re-read the guard's text prefill argument")
    if getattr(original, "__cortados_guard__", False) or getattr(resolve, "__cortados_guard__", False):
        raise GuardError("the text prefill guard is already installed")

    def create_batch_generator(self, *args, **kwargs):
        return enforce_text_prefill(original(self, *args, **kwargs), policy.prefill_tokens)

    create_batch_generator.__cortados_guard__ = True
    create_batch_generator.__wrapped__ = original
    scheduler_cls._create_batch_generator = create_batch_generator

    def resolve_metal_buffer_cache_limit(*args, **kwargs):
        return policy.buffer_cache_bytes, "cortados guard (--buffer-cache-bytes)"

    resolve_metal_buffer_cache_limit.__cortados_guard__ = True
    resolve_metal_buffer_cache_limit.__wrapped__ = resolve
    batched_module._resolve_metal_buffer_cache_limit = resolve_metal_buffer_cache_limit


def _text_modules():
    """mlx-lm verified against its pins, mlx / mlx-metal against theirs, and vllm-mlx's engine.batched module."""
    import mlx_lm
    verify_text(os.path.dirname(os.path.abspath(mlx_lm.__file__)), getattr(mlx_lm, "__version__", "?"))
    verify_mlx()                                  # the attention fallback and the allocator (mlx / mlx-metal 0.32.4)
    from vllm_mlx.engine import batched
    return batched


def _vision_modules():
    """vllm-mlx's MLLM modules, mlx-vlm, PIL and the image checker, verified against the vision pins (and mlx /
    mlx-metal against theirs)."""
    import vllm_mlx
    import mlx_vlm
    verify_vision(os.path.dirname(os.path.abspath(vllm_mlx.__file__)), os.path.dirname(os.path.abspath(mlx_vlm.__file__)),
                  getattr(mlx_vlm, "__version__", "?"))
    verify_mlx()                                  # the fused kernel's implementation (mlx / mlx-metal 0.32.4)
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
    print(f"cortados-vllm-mlx-guard: {msg}\nusage: cortados-vllm-mlx-guard.py --context-tokens N "
          "(text flags | vision flags) -- serve <model> [vllm-mlx args]\n"
          "       cortados-vllm-mlx-guard.py --check <model dir> [vision flags]\n"
          "text flags: " + " N ".join(TEXT_FLAGS) + " N\nvision flags: --vision " + " N ".join(VISION_FLAGS)
          + f" N {DIMS_FLAG} name=N,...", file=sys.stderr)
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
                _vision_modules()                 # mlx-vlm's and mlx's versions and the vision pins
                check_vision_model(argv[1], policy.dims)
            else:
                _text_modules()                   # mlx-lm's and mlx's versions and the text prefill pins (#126)
                check_text_model(argv[1])
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
            print(f"cortados-vllm-mlx-guard: ok (vllm-mlx {VLLM_MLX_VERSION}, mlx-vlm {MLX_VLM_VERSION} and mlx / "
                  f"mlx-metal {MLX_VERSION}, pinned "
                  f"files match, only BatchedEngine may serve, vision route {VISION_MODEL_TYPE})")
            return 0
        if getattr(route, "is_mllm", True):
            print(f"cortados-vllm-mlx-guard: {MLLM_REFUSAL}; {argv[1]} resolves to the MLLM route "
                  f"({getattr(route, 'source', '?')})", file=sys.stderr)
            return EXIT_REFUSED
        print(f"cortados-vllm-mlx-guard: ok (vllm-mlx {VLLM_MLX_VERSION}, mlx-lm {MLX_LM_VERSION} and mlx / mlx-metal "
              f"{MLX_VERSION}, pinned files match, only BatchedEngine may serve, text route)")
        return 0
    if "--" not in argv:
        return _usage("missing `--` before the vllm-mlx arguments")
    cut = argv.index("--")
    own, rest = argv[:cut], argv[cut + 1:]
    try:
        policy, own = parse_vision(own)
        text, own = parse_text(own)
    except GuardError as exc:
        return _usage(str(exc))
    if len(own) != 2 or own[0] != "--context-tokens" or not own[1].isdigit():
        return _usage("expected --context-tokens N")
    if rest[:1] != ["serve"] or len(rest) < 2:
        return _usage("only `serve <model>` is run through the guard")
    if policy is None and text is None:
        return _usage("the text route needs " + " N ".join(TEXT_FLAGS) + " N (#126: its prefill is bounded)")
    if policy is not None and text is not None:
        return _usage("the text flags are for the text route; vision mode bounds its own prefill")
    if BATCHING_FLAG not in rest:
        return _usage(f"{ENGINE_REFUSAL} (`{BATCHING_FLAG}` missing from the serve arguments)")
    try:
        _, scheduler, mllm_scheduler, api_utils, http_exception = _modules()
        if policy is not None:
            batch_generator, vision_cache, mllm_models, image_check = _vision_modules()
        if text is not None:
            batched = _text_modules()
            check_text_model(rest[1])
        install(int(own[1]), scheduler.Scheduler, mllm_scheduler.MLLMScheduler, api_utils, http_exception,
                *_engines(), vision=policy)
        if text is not None:
            install_text(text, scheduler.Scheduler, batched)
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
            f"{policy.image_context_tokens}, media prefill in chunks of {policy.prefill_tokens} keeping one position's "
            "logits" if policy is not None else f"; text prefill one sequence at a time in chunks of at most "
            f"{text.prefill_tokens}, MLX buffer cache {text.buffer_cache_bytes} bytes")
    print(f"cortados-vllm-mlx-guard: prompt + max_tokens capped at {own[1]} tokens (vllm-mlx {VLLM_MLX_VERSION})"
          f"{what}", file=sys.stderr)
    from vllm_mlx import cli
    sys.argv = ["vllm-mlx", *rest]
    cli.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
