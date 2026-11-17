"""The vllm-mlx prompt guard (integrations/vllm-mlx/cortados_vllm_mlx_guard.py): it pins vllm-mlx 0.6.0, refuses a
prompt whose exact token count + max_tokens exceeds the context before vllm-mlx's scheduler queues it, and refuses the
multimodal route. Fakes only: a fake `vllm_mlx` package (its Scheduler.add_request mirrors 0.6.0's, line for line in
what the guard relies on) and a fake fastapi; no model, no MLX, no server."""
import hashlib
import importlib.util
import io
import contextlib
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from cortados_router import local_backend

ROOT = Path(__file__).resolve().parent.parent
GUARD_PATH = ROOT / "integrations" / "vllm-mlx" / "cortados_vllm_mlx_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("cortados_vllm_mlx_guard_under_test", GUARD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A fake vllm_mlx 0.6.0: the parts the guard touches. Scheduler.add_request keeps 0.6.0's shape: tokenize only when
# prompt_token_ids is None (setting num_prompt_tokens there), then queue.
FAKE = {
    "vllm_mlx/__init__.py": '__version__ = "0.6.0"\n',
    "vllm_mlx/request.py": textwrap.dedent('''
        class SamplingParams:
            def __init__(self, max_tokens=256):
                self.max_tokens = max_tokens

        class Request:
            def __init__(self, request_id, prompt, sampling_params):
                self.request_id, self.prompt, self.sampling_params = request_id, prompt, sampling_params
                self.prompt_token_ids = None
                self.num_prompt_tokens = 0
        '''),
    "vllm_mlx/scheduler.py": textwrap.dedent('''
        from collections import deque

        class ByteTokenizer:
            """The worst case for a bound: one token per UTF-8 byte (what `a!a!...` is on Qwen's tokenizer)."""
            def __init__(self):
                self.calls = 0
            def encode(self, text):
                self.calls += 1
                return list(text.encode("utf-8"))

        class Scheduler:
            def __init__(self, tokenizer=None):
                self.tokenizer = tokenizer or ByteTokenizer()
                self.requests, self.waiting = {}, deque()
            def add_request(self, request):
                if request.request_id in self.requests:
                    raise ValueError(f"Request {request.request_id} already exists")
                if request.prompt_token_ids is None:
                    if isinstance(request.prompt, str):
                        request.prompt_token_ids = self.tokenizer.encode(request.prompt)
                    else:
                        request.prompt_token_ids = list(request.prompt)
                    request.num_prompt_tokens = len(request.prompt_token_ids)
                request.remaining_tokens = request.prompt_token_ids
                self.requests[request.request_id] = request
                self.waiting.append(request)
            def _create_batch_generator(self, sampling_params=None):
                # 0.6.0 builds mlx-lm's BatchGenerator with --prefill-step-size / --prefill-batch-size (#126)
                return FakeBatchGenerator(FakeQwen35(), prefill_batch_size=8, prefill_step_size=2048)

        class FakeQwen35:
            pass
        FakeQwen35.__module__ = "mlx_lm.models.qwen3_5"

        class FakePromptBatch:
            def __init__(self, prefill_step_size):
                self.uids, self.prefill_step_size = [], prefill_step_size

        class FakeBatchGenerator:
            def __init__(self, model, prefill_batch_size, prefill_step_size):
                self.model, self.prefill_batch_size, self.prefill_step_size = model, prefill_batch_size, prefill_step_size
                self._prompt_batch = FakePromptBatch(prefill_step_size)
                self._generation_batch, self._unprocessed_sequences = [], deque()
            def _next(self):
                return [], []
        '''),
    "vllm_mlx/engine_core.py": "# engine_core\n",
    # The engines, as in 0.6.0: BatchedEngine's text route reaches Scheduler.add_request; SimpleEngine never does.
    "vllm_mlx/engine/__init__.py": "from .base import BaseEngine\n",
    "vllm_mlx/engine/base.py": textwrap.dedent('''
        from abc import ABC, abstractmethod

        class BaseEngine(ABC):
            @abstractmethod
            async def start(self): ...
            @abstractmethod
            async def generate(self, prompt, max_tokens=256): ...
            @abstractmethod
            async def stream_generate(self, prompt, max_tokens=256): ...
            @abstractmethod
            async def chat(self, messages, max_tokens=256): ...
            @abstractmethod
            async def stream_chat(self, messages, max_tokens=256): ...
        '''),
    "vllm_mlx/engine/batched.py": textwrap.dedent('''
        from vllm_mlx.engine.base import BaseEngine
        from vllm_mlx.request import Request, SamplingParams
        from vllm_mlx.scheduler import Scheduler

        def _resolve_metal_buffer_cache_limit(max_recommended, gpu_memory_utilization):
            return int(max_recommended * gpu_memory_utilization), "device-scaled"

        class BatchedEngine(BaseEngine):
            def __init__(self, model_name, scheduler_config=None):
                self.model_name, self.scheduler = model_name, Scheduler()
            async def start(self):
                return None
            async def generate(self, prompt, max_tokens=256):
                self.scheduler.add_request(Request("g", prompt, SamplingParams(max_tokens)))
                return "batched"
            async def stream_generate(self, prompt, max_tokens=256):
                yield await self.generate(prompt, max_tokens)
            async def chat(self, messages, max_tokens=256):
                return await self.generate(messages[-1]["content"], max_tokens)
            async def stream_chat(self, messages, max_tokens=256):
                yield await self.chat(messages, max_tokens)
        '''),
    "vllm_mlx/engine/simple.py": textwrap.dedent('''
        from vllm_mlx.engine.base import BaseEngine

        class SimpleEngine(BaseEngine):
            """Generates through mlx-lm directly: no scheduler, so no cap."""
            GENERATED = []
            def __init__(self, model_name, **kw):
                self.model_name = model_name
            async def start(self):
                return None
            async def generate(self, prompt, max_tokens=256):
                SimpleEngine.GENERATED.append(prompt)
                return "simple"
            async def stream_generate(self, prompt, max_tokens=256):
                yield await self.generate(prompt, max_tokens)
            async def chat(self, messages, max_tokens=256):
                return await self.generate(messages[-1]["content"], max_tokens)
            async def stream_chat(self, messages, max_tokens=256):
                yield await self.chat(messages, max_tokens)
        '''),
    "vllm_mlx/server.py": textwrap.dedent('''
        from vllm_mlx.engine.base import BaseEngine
        from vllm_mlx.engine.batched import BatchedEngine
        from vllm_mlx.engine.simple import SimpleEngine
        _engine = None
        def load_model(model_name, use_batching=False, scheduler_config=None):
            global _engine
            _engine = BatchedEngine(model_name, scheduler_config) if use_batching else SimpleEngine(model_name)
            return _engine
        '''),
    "vllm_mlx/model_registry.py": "# model_registry\n",
    "vllm_mlx/mllm_scheduler.py": textwrap.dedent('''
        class MLLMScheduler:
            def add_request(self, prompt, **kw):
                return "queued"
        '''),
    "vllm_mlx/api/__init__.py": "",
    "vllm_mlx/api/utils.py": textwrap.dedent('''
        class MllmRoute:
            def __init__(self, is_mllm, source):
                self.is_mllm, self.source = is_mllm, source
        MLLM = set()
        def resolve_mllm_route(model_name, resolved_path=None, *, force_mllm=False):
            return MllmRoute(force_mllm or model_name in MLLM, "config")
        '''),
    "vllm_mlx/cli.py": textwrap.dedent('''
        import sys
        CALLS = []
        def main():
            from vllm_mlx import server
            from vllm_mlx.api.utils import resolve_mllm_route
            CALLS.append(list(sys.argv))
            resolve_mllm_route(sys.argv[2], sys.argv[2])
            server.load_model(sys.argv[2], use_batching="--continuous-batching" in sys.argv)
        '''),
    "fastapi/__init__.py": textwrap.dedent('''
        class HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail)
                self.status_code, self.detail = status_code, detail
        '''),
}
PINNED_FILES = ("scheduler.py", "engine_core.py", "engine/batched.py", "cli.py", "server.py", "model_registry.py",
                "engine/__init__.py", "engine/base.py")
TEXT = ["--text-prefill-tokens", "256", "--buffer-cache-bytes", str(2 ** 30)]   # the text route's prefill bounds (#126)
SERVE = ["--context-tokens", "131072", *TEXT, "--", "serve", "/models/text", "--continuous-batching"]


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for rel, text in FAKE.items():
            p = Path(self.dir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.pins = {rel: hashlib.sha256((Path(self.dir) / "vllm_mlx" / rel).read_bytes()).hexdigest()
                     for rel in PINNED_FILES}
        sys.path.insert(0, self.dir)
        self.addCleanup(self._unimport)
        self.guard = load_guard()
        pins = patch.object(self.guard, "PINNED_SHA256", self.pins)
        pins.start()
        self.addCleanup(pins.stop)
        argv = patch.object(sys, "argv", list(sys.argv))
        argv.start()
        self.addCleanup(argv.stop)
        # #126: the text route's mlx-lm / mlx pins and config.json check have their own tests (test_text_prefill.py);
        # here they pass, and the fake vllm-mlx's engine.batched is the module the guard patches
        self.text_models = []

        def text_modules():
            from vllm_mlx.engine import batched
            return batched
        for name, fake in (("_text_modules", text_modules), ("check_text_model", self.text_models.append)):
            p = patch.object(self.guard, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def _unimport(self):
        sys.path.remove(self.dir)
        for name in list(sys.modules):
            if name.split(".")[0] in ("vllm_mlx", "fastapi"):
                del sys.modules[name]

    def repin(self):
        self.pins.update({rel: hashlib.sha256((Path(self.dir) / "vllm_mlx" / rel).read_bytes()).hexdigest()
                          for rel in PINNED_FILES})

    def run_main(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.guard.main(argv)
        return rc, err.getvalue()

    def installed(self, context=131072):
        import fastapi
        from vllm_mlx import mllm_scheduler, scheduler
        from vllm_mlx.api import utils
        from vllm_mlx.engine import base, batched, simple  # noqa: F401
        self.guard.install(context, scheduler.Scheduler, mllm_scheduler.MLLMScheduler, utils, fastapi.HTTPException,
                           base.BaseEngine, batched.BatchedEngine)
        return scheduler

    def request(self, prompt, max_tokens, rid="r1"):
        from vllm_mlx.request import Request, SamplingParams
        return Request(rid, prompt, SamplingParams(max_tokens))


class GuardCapTests(GuardBase):
    def test_round4_repro_a_200k_token_prompt_is_refused_before_it_is_queued(self):
        # The review's repro: user content "a!" * 100000 with max_tokens 1. The gate's reservation is capped at the
        # 131,072-token context, but the real prompt is 200,000+ tokens; vllm-mlx 0.6.0 would schedule it as is.
        import fastapi
        scheduler = self.installed()
        s = scheduler.Scheduler()
        prompt = "<|im_start|>user\n" + "a!" * 100000 + "<|im_end|>\n<|im_start|>assistant\n"
        for rid in ("r1", "r2"):                                       # two of them: neither reaches the queue
            with self.assertRaises(fastapi.HTTPException) as cm:
                s.add_request(self.request(prompt, 1, rid))
            self.assertEqual(cm.exception.status_code, 413)
            self.assertTrue(cm.exception.detail.startswith(self.guard.REFUSAL))
            self.assertIn(f"prompt {len(prompt.encode())} tokens + max_tokens 1", cm.exception.detail)
        self.assertEqual((len(s.waiting), s.requests), (0, {}))

    def test_the_count_is_the_prompt_that_is_prefilled_and_tokenized_once(self):
        scheduler = self.installed(context=1000)
        s = scheduler.Scheduler()
        r = self.request("x" * 900, 100)                               # 900 + 100 = the context exactly: admitted
        s.add_request(r)
        self.assertEqual(list(s.waiting), [r])
        self.assertEqual((r.num_prompt_tokens, len(r.prompt_token_ids)), (900, 900))
        self.assertIs(r.remaining_tokens, r.prompt_token_ids)          # the ids counted are the ids prefilled
        self.assertEqual(s.tokenizer.calls, 1)                         # the original did not tokenize again
        import fastapi
        with self.assertRaises(fastapi.HTTPException):
            s.add_request(self.request("x" * 900, 101, "r2"))         # one token over
        with self.assertRaises(fastapi.HTTPException):
            s.add_request(self.request(list(range(1001)), 0, "r3"))    # token-id prompts are counted too
        with self.assertRaises(fastapi.HTTPException):
            s.add_request(self.request("x", None, "r4"))               # an unknown max_tokens counts as the context
        self.assertEqual(len(s.waiting), 1)

    def test_a_processor_wrapping_the_tokenizer_is_used_as_vllm_mlx_does(self):
        class Wrapped:
            def __init__(self):
                from vllm_mlx.scheduler import ByteTokenizer
                self.tokenizer = ByteTokenizer()
        scheduler = self.installed(context=10)
        s = scheduler.Scheduler(tokenizer=Wrapped())
        s.add_request(self.request("abc", 5))
        self.assertEqual(s.waiting[0].num_prompt_tokens, 3)

    def test_the_multimodal_route_is_refused(self):
        import fastapi
        from vllm_mlx import mllm_scheduler
        from vllm_mlx.api import utils
        self.installed()
        with self.assertRaises(fastapi.HTTPException) as cm:
            mllm_scheduler.MLLMScheduler().add_request("hi", max_tokens=1)
        self.assertEqual(cm.exception.status_code, 501)
        utils.MLLM.add("/models/vl")
        with self.assertRaises(SystemExit):
            utils.resolve_mllm_route("/models/vl", "/models/vl")
        self.assertFalse(utils.resolve_mllm_route("/models/text", "/models/text").is_mllm)

    def test_the_refusal_prefix_is_the_one_the_gate_recognizes(self):
        self.assertEqual(self.guard.REFUSAL, local_backend.GUARD_REFUSAL)
        detail = self.guard.over_context(200_010, 1, 131072)
        self.assertEqual(local_backend.guard_refusal(413, ('{"detail": "%s"}' % detail).encode()), detail)
        self.assertIsNone(local_backend.guard_refusal(400, ('{"detail": "%s"}' % detail).encode()))
        self.assertIsNone(local_backend.guard_refusal(413, b'{"detail": "other"}'))


class GuardStartupTests(GuardBase):

    def test_serve_hands_over_to_vllm_mlx_with_the_guard_installed(self):
        rc, err = self.run_main(SERVE + ["--port", "8000"])
        from vllm_mlx import cli, scheduler, server
        from vllm_mlx.engine.batched import BatchedEngine
        self.assertEqual(rc, 0)
        self.assertEqual(cli.CALLS, [["vllm-mlx", "serve", "/models/text", "--continuous-batching", "--port", "8000"]])
        self.assertTrue(scheduler.Scheduler.add_request.__cortados_guard__)
        self.assertIs(type(server._engine), BatchedEngine)
        self.assertIn("capped at 131072", err)

    def test_another_version_or_changed_files_stop_startup(self):
        from vllm_mlx import scheduler
        with patch.object(sys.modules["vllm_mlx"], "__version__", "0.6.1"):
            rc, err = self.run_main(SERVE)
        self.assertEqual(rc, 2)
        self.assertIn("0.6.1 is not 0.6.0", err)
        self.assertFalse(hasattr(scheduler.Scheduler.add_request, "__cortados_guard__"))
        for rel in PINNED_FILES:
            with self.subTest(rel):
                path = Path(self.dir, "vllm_mlx", *rel.split("/"))
                text = path.read_text()
                path.write_text(text + "# patched\n")
                rc, err = self.run_main(SERVE)
                path.write_text(text)
                self.assertEqual(rc, 2)
                self.assertIn(f"{rel} differs", err)

    def test_the_real_pins_are_the_060_wheel(self):
        real = load_guard()
        self.assertEqual(set(real.PINNED_SHA256), set(PINNED_FILES))
        self.assertEqual(real.VLLM_MLX_VERSION, "0.6.0")
        self.assertTrue(all(len(v) == 64 for v in real.PINNED_SHA256.values()))

    def test_a_multimodal_model_stops_startup_and_check(self):
        from vllm_mlx.api import utils
        utils.MLLM.add("/models/vl")
        rc, err = self.run_main(["--check", "/models/vl"])            # what install_mlx_backend.sh runs first
        self.assertEqual(rc, 2)
        self.assertIn("MLLM route", err)
        self.assertEqual(self.run_main(["--check", "/models/text"])[0], 0)
        with self.assertRaises(SystemExit):                            # and serving it never starts
            self.run_main(["--context-tokens", "131072", *TEXT, "--", "serve", "/models/vl", "--continuous-batching"])

    def test_bad_command_lines_are_refused(self):
        for argv in ([], ["serve", "/m"], ["--context-tokens", "x", "--", "serve", "/m"],
                     ["--context-tokens", "131072", "--", "bench"], ["--check"],
                     ["--context-tokens", "0", "--", "serve", "/m", "--continuous-batching"],
                     ["--context-tokens", "131072", "--", "serve", "/m"]):
            with self.subTest(argv):
                self.assertEqual(self.run_main(argv)[0], 2)


class GuardEngineTests(GuardBase):
    """Round 5 review: only BatchedEngine reaches Scheduler.add_request, so any other engine is refused at runtime,
    whatever the dispatch files say."""

    def test_round5_repro_cli_selecting_simple_engine_stops_startup_even_with_matching_pins(self):
        # The review's mutation: cli.py's `use_batching=args.continuous_batching` becomes False. Re-pinned, so the
        # pins match and only the runtime refusal stands between it and an uncapped SimpleEngine.
        cli = Path(self.dir, "vllm_mlx", "cli.py")
        cli.write_text(FAKE["vllm_mlx/cli.py"].replace('use_batching="--continuous-batching" in sys.argv',
                                                       "use_batching=False"))
        self.repin()
        with self.assertRaises(self.guard.UnguardedEngine) as cm:
            self.run_main(SERVE)
        self.assertIn("refused SimpleEngine", str(cm.exception))
        from vllm_mlx import server
        self.assertIsNone(server._engine)

    def test_serve_without_continuous_batching_is_refused_before_vllm_mlx_starts(self):
        rc, err = self.run_main(["--context-tokens", "131072", *TEXT, "--", "serve", "/models/text"])
        self.assertEqual(rc, 2)
        self.assertIn("--continuous-batching", err)
        from vllm_mlx import cli
        self.assertEqual(cli.CALLS, [])

    def test_only_batched_engine_is_built_and_other_engines_never_generate(self):
        import asyncio
        self.installed(context=10)
        from vllm_mlx.engine.base import BaseEngine
        from vllm_mlx.engine.batched import BatchedEngine
        from vllm_mlx.engine.simple import SimpleEngine
        e = BatchedEngine("m")
        self.assertEqual(asyncio.run(e.generate("abc", 5)), "batched")      # the guarded route still serves
        import fastapi
        with self.assertRaises(fastapi.HTTPException):                       # and is capped
            asyncio.run(e.generate("abcdef", 5))
        with self.assertRaises(self.guard.UnguardedEngine):
            SimpleEngine("m")
        # An instance made without __new__ still cannot start or generate (each method raises when called).
        s = object.__new__(SimpleEngine)
        for name in self.guard.ENGINE_METHODS:
            with self.subTest(name), self.assertRaises(self.guard.UnguardedEngine):
                getattr(s, name)("x")
        self.assertEqual(SimpleEngine.GENERATED, [])
        with self.assertRaises(self.guard.UnguardedEngine):                  # a new engine class, or a subclass
            type("OtherEngine", (BaseEngine,), {})
        with self.assertRaises(self.guard.UnguardedEngine):
            type("TweakedBatched", (BatchedEngine,), {})
        with self.assertRaises(self.guard.GuardError):                        # installed once
            self.guard.refuse_other_engines(BaseEngine, BatchedEngine)

    def test_an_engine_with_its_own_new_stops_install(self):
        from vllm_mlx.engine import base
        from vllm_mlx.engine.batched import BatchedEngine
        sneaky = type("SneakyEngine", (base.BaseEngine,), {"__new__": lambda cls, *a, **k: object.__new__(cls)})
        self.addCleanup(lambda: sneaky)                                      # keep it alive (__subclasses__ is weak)
        with self.assertRaises(self.guard.GuardError):
            self.guard.refuse_other_engines(base.BaseEngine, BatchedEngine)
        rc, err = self.run_main(["--check", "/models/text"])                # install_mlx_backend.sh sees it first
        self.assertEqual(rc, 2)
        self.assertIn("SneakyEngine defines __new__", err)


class ServeArgvTests(unittest.TestCase):
    def test_the_launch_agent_runs_vllm_mlx_through_the_guard_with_the_governor_context(self):
        c = local_backend.load(str(ROOT / "tables" / "local-backend.example.json"))
        argv = local_backend.serve_argv(c)
        paths = local_backend.install_paths(c)
        gov = c["governor"]
        self.assertEqual(argv[:10], [paths["python"], paths["guard"], "--context-tokens", str(gov["context_tokens"]),
                                     # #126: the text route's prefill bounds, the sizes the gate's reservation uses
                                     "--text-prefill-tokens", str(gov["text_prefill_chunk_tokens"]),
                                     "--buffer-cache-bytes", str(gov["buffer_cache_bytes"]), "--", "serve"])
        self.assertTrue(paths["guard"].endswith("/bin/cortados-vllm-mlx-guard.py"))
        self.assertEqual(Path(paths["guard_source"]), GUARD_PATH)
        self.assertEqual(argv[10], paths["model"])
        # vllm-mlx is given the same prefill (the guard enforces it on every batch generator whatever the flags)
        i = argv.index("--prefill-step-size")
        self.assertEqual(argv[i:i + 4], ["--prefill-step-size", "256", "--prefill-batch-size", "1"])
        guard = load_guard()
        text, rest = guard.parse_text(argv[2:argv.index("--")])
        self.assertEqual((text.prefill_tokens, text.buffer_cache_bytes, rest),
                         (256, 2 ** 29, ["--context-tokens", "131072"]))


if __name__ == "__main__":
    unittest.main()
