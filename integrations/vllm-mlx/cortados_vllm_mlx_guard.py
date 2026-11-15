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

The multimodal route (MLLMScheduler) tokenizes for generation inside mlx_vlm's prepare_inputs, a path this guard
cannot count the same way, so it is not served: the guard stops startup if vllm-mlx resolves the model to the MLLM
route, and MLLMScheduler.add_request refuses every request in case one is built anyway.

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
            http_exception: Any, base_engine_cls: Any, batched_engine_cls: Any) -> None:
    """Patch the classes in place (before any request exists): the prompt cap on Scheduler.add_request, the MLLM
    route refused at route resolution and at MLLMScheduler.add_request, every engine but BatchedEngine refused."""
    if isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or context_tokens <= 0:
        raise GuardError(f"--context-tokens must be a positive integer, got {context_tokens!r}")
    original = scheduler_cls.add_request
    if getattr(original, "__cortados_guard__", False):
        raise GuardError("the guard is already installed")
    refuse_other_engines(base_engine_cls, batched_engine_cls)

    def add_request(self, request):
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
    print(f"cortados-vllm-mlx-guard: {msg}\nusage: cortados-vllm-mlx-guard.py --context-tokens N -- serve <model> "
          "[vllm-mlx args]\n       cortados-vllm-mlx-guard.py --check <model dir>", file=sys.stderr)
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--check"]:
        if len(argv) != 2:
            return _usage("--check takes one model directory")
        try:
            _, _, _, api_utils, _ = _modules()
            check_engines(*_engines())            # the engine refusal would hold on this vllm-mlx (read only)
            route = api_utils.resolve_mllm_route(argv[1], argv[1])
        except GuardError as exc:
            print(f"cortados-vllm-mlx-guard: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except Exception as exc:                  # e.g. MllmRouteUndetermined: an unreadable config.json
            print(f"cortados-vllm-mlx-guard: the model route could not be resolved ({type(exc).__name__}: {exc})",
                  file=sys.stderr)
            return EXIT_REFUSED
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
    if len(own) != 2 or own[0] != "--context-tokens" or not own[1].isdigit():
        return _usage("expected --context-tokens N")
    if rest[:1] != ["serve"]:
        return _usage("only `serve` is run through the guard")
    if BATCHING_FLAG not in rest:
        return _usage(f"{ENGINE_REFUSAL} (`{BATCHING_FLAG}` missing from the serve arguments)")
    try:
        _, scheduler, mllm_scheduler, api_utils, http_exception = _modules()
        install(int(own[1]), scheduler.Scheduler, mllm_scheduler.MLLMScheduler, api_utils, http_exception,
                *_engines())
    except GuardError as exc:
        print(f"cortados-vllm-mlx-guard: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"cortados-vllm-mlx-guard: prompt + max_tokens capped at {own[1]} tokens (vllm-mlx {VLLM_MLX_VERSION})",
          file=sys.stderr)
    from vllm_mlx import cli
    sys.argv = ["vllm-mlx", *rest]
    cli.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
