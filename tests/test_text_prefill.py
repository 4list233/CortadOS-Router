"""#126: the text route's computed reservation on the Pro's MLX lane (local_governor.text_set_bytes) and the guard's text
prefill bounds (cortados_vllm_mlx_guard.py "Text prefill"). Fakes only: no MLX, no model, no vllm-mlx, no network."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cortados_router import local_backend, local_governor
from tests.test_pro_vision import DIMS_ARG, FAKE_MLX_TAG, VISION_ARGS, VisionGuardBase  # noqa: F401
from tests.test_vllm_mlx_guard import GuardBase, TEXT, load_guard

GIB = 2 ** 30
ROOT = Path(__file__).resolve().parent.parent

# The Pro's Qwen3.8 27B (text_config), written out here rather than read from the module under test.
H, I, HQ, HKV, D = 5120, 17408, 24, 4, 256
KH, VH, KD, VD, V, A = 16, 48, 128, 128, 248320, 2
KV_PER_TOKEN = 16 * 2 * HKV * D * A                          # 65,536: 16 full-attention layers of 64
STATE = int(0.15 * GIB)


def width() -> int:
    """One prompt token's activations in one layer, bytes (the larger of full attention and linear attention)."""
    conv = 2 * KH * KD + VH * VD
    attention = A * (6 * HQ * D + 4 * HKV * D)
    linear = A * (3 * conv + 4 * VH * VD + 2 * KH * KD) + 4 * (2 * VH * VD + 4 * VH)
    return A * (3 * I + 4 * H) + max(attention, linear)


def prefill_terms(p: int, chunk: int = 256) -> dict:
    c = min(chunk, p)
    return {"scores": 4 * HQ * c * p * A, "mask": 2 * c * p, "chunk": 2 * c * width(), "ids": 8 * p}


def set_terms(tokens: list, prompts: list, high_water: int = 0) -> dict:
    n, m, p = len(tokens), max(max(tokens), high_water) + 256, max(prompts)
    out = {"kv": 2 * n * m * KV_PER_TOKEN, "decode": 4 * HQ * n * m * A + n * m,
           "fixed": n * (2 * STATE + V * (A + 8)),
           "prefill": max(sum(prefill_terms(p).values()) for p in prompts), "buffer_cache": GIB // 2,
           # one largest buffer over MLX's cache limit: a chunk's scores, or one layer's K of the padded batch
           "cache_overshoot": max(HQ * min(256, p) * p * A, n * m * HKV * D * A)}
    computed = sum(out.values())
    legacy = sum(t * KV_PER_TOKEN + STATE + int(1.5 * GIB) for t in tokens)
    return {**out, "computed": computed, "legacy": legacy, "total": max(computed, legacy), "cells": n * m}


class FixedGovernor(local_governor.Governor):
    def __init__(self, idle_s=3600.0, pressure="normal", model=None, **kw):
        super().__init__(local_governor.check_config(kw, model), idle_reader=lambda: idle_s,
                         pressure_reader=lambda: pressure, clock=lambda: 0.0)


class TextReservationByTermTests(unittest.TestCase):
    """The reservation by term at 1k, 32k, 64k and 128k tokens (prompt as long as the context: the worst case)."""

    def setUp(self):
        self.cfg = local_governor.check_config({})

    def test_the_width_is_the_linear_attention_layer(self):
        self.assertEqual(width(), 314_112)
        self.assertEqual(local_governor._text_token_width(), width())

    def test_each_term_at_1k_32k_64k_and_128k(self):
        for p in (1024, 32768, 65536, 131072):
            with self.subTest(tokens=p):
                got = local_governor.text_prefill_bytes(self.cfg, p)
                want = prefill_terms(p)
                self.assertEqual({k: got[k] for k in want}, want)
                self.assertEqual(got["total"], sum(want.values()))
                load = local_governor._text_load(self.cfg, p, p)
                self.assertEqual(local_governor.text_set_bytes(self.cfg, [load]), set_terms([p], [p]))

    def test_the_numbers_the_docs_give(self):
        gib = {p: round(local_governor.text_set_bytes(self.cfg, [local_governor._text_load(self.cfg, p, p)])["total"]
                        / GIB, 2) for p in (1024, 32768, 65536, 131072)}
        self.assertEqual(gib, {1024: 1.71, 32768: 6.88, 65536: 12.78, 131072: 24.57})
        scores = {p: local_governor.text_prefill_bytes(self.cfg, p)["scores"] / GIB for p in (32768, 131072)}
        self.assertEqual(scores, {32768: 1.5, 131072: 6.0})        # 4 buffers of 24 x 256 x P x 2 bytes
        # upstream's 2,048-token step would be 8 times that: 48 GiB of scores at 128k
        self.assertEqual(local_governor.text_prefill_bytes({**self.cfg, "text_prefill_chunk_tokens": 2048},
                                                           131072)["scores"], 48 * GIB)

    def test_no_logits_term_for_the_prompt_and_one_decode_row_per_request(self):
        terms = local_governor.text_prefill_bytes(self.cfg, 131072)
        self.assertNotIn("logits", terms)                    # prefill evaluates only the cache state
        load = local_governor._text_load(self.cfg, 131072, 131072)
        self.assertEqual(load.fixed, 2 * STATE + V * (A + 8))

    def test_the_prompt_is_the_input_bound_and_the_context_is_input_plus_max_tokens(self):
        load = local_governor.text_load(self.cfg, 40_000, 32_000)
        self.assertEqual((load.tokens, load.prompt), (72_000, 40_000))
        load = local_governor.text_load(self.cfg, 500_000, 32_000)   # both capped at the context
        self.assertEqual((load.tokens, load.prompt), (131072, 131072))
        load = local_governor.text_load(self.cfg, 10, None)          # no max_tokens: the backend's default
        self.assertEqual(load.tokens, 10 + 32768)

    def test_short_turns_reserve_exactly_what_they_did(self):
        for i, out in ((10, 10), (500, 256), (2000, 512), (8000, 512)):
            with self.subTest(input=i, max_tokens=out):
                old = local_governor.tokens_bytes(self.cfg, i + out)
                got = local_governor.text_set_bytes(self.cfg, [local_governor.text_load(self.cfg, i, out)])
                self.assertEqual(got["total"], max(old, got["computed"]))
        # alone, the per-node buffer cache makes a tiny turn's computed peak exceed the old margin only above ~1k
        self.assertEqual(local_governor.request_bytes(self.cfg, 10, 10), local_governor.tokens_bytes(self.cfg, 20))
        # eight together: the old sum stands byte for byte (the buffer cache and one prefill are shared)
        loads = [local_governor.text_load(self.cfg, 8000, 512) for _ in range(8)]
        got = local_governor.text_set_bytes(self.cfg, loads)
        self.assertGreater(got["legacy"], got["computed"])
        self.assertEqual(got["total"], 8 * local_governor.tokens_bytes(self.cfg, 8512))

    def test_the_set_pads_every_row_to_the_longest(self):
        long, short = (local_governor._text_load(self.cfg, 131072, 131072),
                       local_governor._text_load(self.cfg, 2048, 2048))
        both = local_governor.text_set_bytes(self.cfg, [long, short])
        self.assertEqual(both["kv"], 2 * 2 * (131072 + 256) * KV_PER_TOKEN)   # the short row is 128k wide too
        self.assertEqual(both["prefill"], long.prefill)                       # one prefill at a time
        self.assertEqual(both["buffer_cache"], GIB // 2)                      # once per node
        self.assertEqual(both["cache_overshoot"], HQ * 256 * 131072 * A)      # the long prompt's score buffer
        self.assertEqual(local_governor.text_set_bytes(self.cfg, []), dict.fromkeys(both, 0))
        self.assertEqual(local_governor.text_set_bytes(self.cfg, [short], 131072)["kv"],
                         2 * (131072 + 256) * KV_PER_TOKEN)                # a departed long row's width

    def test_config_keys(self):
        for bad in ({"kv_copies": 1}, {"kv_copies": 0}, {"text_score_buffers": 0}, {"buffer_cache_bytes": 0},
                    {"text_prefill_chunk_tokens": 0}, {"text_prefill_chunk_tokens": 200_000},
                    {"text_prefill_chunk_tokens": True}):
            with self.subTest(bad), self.assertRaises(local_governor.GovernorConfigError):
                local_governor.check_config(bad)
        c = local_governor.check_config({"text_prefill_chunk_tokens": 512, "kv_copies": 3})
        self.assertEqual(local_governor.text_prefill_bytes(c, 131072)["scores"], 12 * GIB)

    def test_text_room_is_the_largest_context_that_fits(self):
        cfg, budget = self.cfg, local_governor.available_bytes(self.cfg)
        self.assertEqual(local_governor.text_room(cfg, [], budget), 131072)
        loads = [local_governor.text_load(cfg, 8000, 512)]
        room = local_governor.text_room(cfg, loads, budget)
        fits = lambda t: local_governor.text_set_bytes(cfg, [*loads, local_governor._text_load(cfg, t, t)])["total"]
        self.assertLessEqual(fits(room), budget)
        self.assertGreater(fits(room + 1), budget)
        self.assertEqual(local_governor.text_room(cfg, [], 1), 0)


class TextAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.a = local_governor.Admission(FixedGovernor())             # owner idle: count 8, 25 GiB beside the weights

    def test_eight_short_requests_are_admitted_at_once(self):
        loads = [self.a.text_load(2000, 512) for _ in range(8)]
        tickets = [self.a.try_acquire(load=x) for x in loads]
        self.assertTrue(all(t for t, _ in tickets), [s.get("reason") for _, s in tickets])
        self.assertEqual(self.a.inflight, 8)
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(self.a.governor.cfg, loads)["total"])
        self.assertEqual(self.a.reserved, 8 * local_governor.tokens_bytes(self.a.governor.cfg, 2512))   # as before
        ticket, snap = self.a.try_acquire(load=self.a.text_load(2000, 512))
        self.assertEqual((ticket, snap["refused"], snap["reason"]), (None, "busy", "owner idle"))   # the count's 8
        for t, _ in tickets:
            t.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))

    def test_a_full_context_prompt_is_reserved_soundly_alone_on_the_4bit(self):
        load = self.a.text_load(131072, 1)
        ticket, snap = self.a.try_acquire(load=load)
        self.assertIsNotNone(ticket)
        terms = local_governor.text_set_bytes(self.a.governor.cfg, [load])
        self.assertEqual(snap["reserved_bytes"], terms["computed"])
        self.assertLessEqual(terms["computed"], 25 * GIB)
        self.assertEqual(snap["free_tokens"], 0)                       # nothing fits beside it
        ticket2, snap2 = self.a.try_acquire(load=self.a.text_load(100, 16))
        self.assertEqual((ticket2, snap2["refused"]), (None, "busy"))  # retryable 529: it fits once the long one ends
        ticket.release()
        self.assertIsNotNone(self.a.try_acquire(load=self.a.text_load(100, 16))[0])

    def test_a_full_context_prompt_is_refused_413_where_it_cannot_fit(self):
        six = local_governor.Admission(FixedGovernor(model={"weights_bytes": int(22.4 * GIB), "idle_cap": 6}))
        ticket, snap = six.try_acquire(load=six.text_load(131072, 1))
        self.assertEqual((ticket, snap["refused"]), (None, "too_large"))   # never fits: non-retryable at the gate
        self.assertEqual(six.inflight, 0)
        self.assertEqual(snap["fit_tokens"], 92337)                   # the most the 6-bit copy admits, alone
        self.assertIsNotNone(six.try_acquire(load=six.text_load(92337, 1))[0])   # 92,337 + 1 out
        six = local_governor.Admission(FixedGovernor(model={"weights_bytes": int(22.4 * GIB), "idle_cap": 6}))
        self.assertEqual(six.try_acquire(load=six.text_load(92338, 1))[1]["refused"], "too_large")

    def test_one_load_admitted_twice_is_released_once_each(self):
        load = self.a.text_load(2000, 512)
        t1, t2 = self.a.try_acquire(load=load)[0], self.a.try_acquire(load=load)[0]
        t1.release()
        self.assertEqual((self.a.inflight, self.a.reserved),
                         (1, local_governor.text_set_bytes(self.a.governor.cfg, [load])["total"]))
        t2.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))

    def test_a_departed_long_row_keeps_its_width_until_the_set_drains(self):
        # #126 review: BatchKVCache.filter gathers the survivors at the old width and slices the padding off (a view),
        # so a short turn that outlives a 64k one is still backed by 64k-wide KV until the batch grows or empties
        cfg = self.a.governor.cfg
        long, short = self.a.text_load(60_000, 4096), self.a.text_load(4000, 512)
        t_long, t_short = self.a.try_acquire(load=long)[0], self.a.try_acquire(load=short)[0]
        self.assertIsNotNone(t_short)
        t_long.release()
        self.assertEqual(self.a._high_water, long.tokens)
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(cfg, [short], long.tokens, self.a._peak)["total"])
        self.assertGreaterEqual(self.a.reserved, local_governor.text_set_bytes(cfg, [short], long.tokens)["total"])
        self.assertGreater(self.a.reserved, local_governor.text_set_bytes(cfg, [short])["total"])
        # a second short one would be reserved as if 64k wide too: whether it is admitted beside the kept width or
        # the set drains first (#126 round 3: when the kept width withholds room), the width stays until the set ends
        t2, snap = self.a.try_acquire(load=self.a.text_load(4000, 512))
        if t2 is not None:
            self.assertEqual(snap["reserved_bytes"],
                             local_governor.text_set_bytes(cfg, [short, t2.load], long.tokens, self.a._peak)["total"])
            t2.release()
        self.assertEqual(self.a._high_water, long.tokens)
        t_short.release()                                              # drained: the width is gone
        self.assertEqual((self.a._high_water, self.a.reserved), (0, 0))
        t3 = self.a.try_acquire(load=self.a.text_load(4000, 512))[0]
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(cfg, [t3.load])["total"])

    def test_the_set_keeps_its_peak_rows_until_it_drains(self):
        # #126 review round 2: filter's gather is lazy, so seven finished rows' batch can outlive their tickets; the
        # set's KV never shrinks below its peak (rows x width) until no text turn runs
        cfg = self.a.governor.cfg
        loads = [self.a.text_load(8000, 512) for _ in range(8)]
        tickets = [self.a.try_acquire(load=x)[0] for x in loads]
        peak = local_governor.text_set_bytes(cfg, loads)
        for t in tickets[1:]:
            t.release()
        left = local_governor.text_set_bytes(cfg, [loads[0]], loads[0].tokens, (peak["cells"], peak["cache_overshoot"]))
        self.assertEqual(left["kv"], peak["kv"])                       # still eight rows' worth
        self.assertEqual(self.a.reserved, left["total"])
        self.assertGreater(self.a.reserved, local_governor.text_set_bytes(cfg, [loads[0]])["total"])
        tickets[0].release()
        self.assertEqual((self.a._peak, self.a._high_water, self.a.reserved), ((0, 0), 0, 0))

    def test_the_overshoot_of_a_departed_prompt_is_kept_until_the_set_drains(self):
        cfg = self.a.governor.cfg
        long, short = self.a.text_load(40_000, 1024), self.a.text_load(2000, 256)
        t_long, t_short = self.a.try_acquire(load=long)[0], self.a.try_acquire(load=short)[0]
        self.assertIsNotNone(t_short)
        over = HQ * 256 * 40_000 * A                                   # the long prompt's score buffer
        self.assertEqual(self.a._peak[1], max(over, self.a._peak[0] * HKV * D * A))
        t_long.release()
        self.assertEqual(local_governor.text_set_bytes(cfg, [short], self.a._high_water,
                                                       self.a._peak)["cache_overshoot"], self.a._peak[1])
        self.assertGreaterEqual(self.a._peak[1], over)
        t_short.release()
        self.assertEqual(self.a._peak, (0, 0))

    def test_a_load_is_refused_on_a_vision_model(self):
        v = local_governor.Admission(FixedGovernor(model={"vision": True}))
        self.assertIsNone(v.text_load(100, 16))
        with self.assertRaises(ValueError):
            v.try_acquire(load=self.a.text_load(100, 16))


class VisionUnchangedTests(unittest.TestCase):
    """#126 leaves the vision route as #125 built it: image reservations, text turns on a vision model, its argv."""

    def setUp(self):
        self.cfg = local_governor.check_config({}, {"vision": True})

    def test_image_and_text_reservations_on_a_vision_model(self):
        for text, out, images in ((100, 512, 1), (20_000, 4096, 5), (9000, 4000, 0)):
            with self.subTest(text=text, images=images):
                got = local_governor.request_bytes(self.cfg, text, out, images)
                tokens = local_governor.request_tokens(self.cfg, text, out, images)
                if images:
                    want = tokens * KV_PER_TOKEN + STATE \
                        + local_governor.image_prefill_bytes(self.cfg, tokens, images)["total"]
                else:
                    want = tokens * KV_PER_TOKEN + STATE + int(1.5 * GIB)
                self.assertEqual(got, want)
        self.assertFalse(local_governor.text_route(self.cfg))

    def test_vision_admission_still_sums_and_reports_as_before(self):
        a = local_governor.Admission(FixedGovernor(model={"vision": True}))
        need = a.request_bytes(9000, 4000)
        t1, _ = a.try_acquire(need)
        t2, snap = a.try_acquire(need)
        self.assertEqual(a.reserved, 2 * need)
        self.assertEqual(snap["fit_tokens"], local_governor.bytes_tokens(a.governor.cfg, 25 * GIB))
        t1.release()
        t2.release()
        self.assertEqual(a.reserved, 0)

    def test_the_vision_entry_gets_no_text_flags(self):
        raw = json.loads((ROOT / "tables" / "local-backend.example.json").read_text())
        raw["vllm_mlx"]["model"] = "qwen-27b-4bit-vision"
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "b.json")
        Path(path).write_text(json.dumps(raw))
        vc = local_backend.load(path)
        argv = local_backend.serve_argv(vc)
        self.assertEqual(local_backend.text_argv(vc), ([], []))
        for flag in ("--text-prefill-tokens", "--buffer-cache-bytes", "--prefill-step-size", "--prefill-batch-size"):
            self.assertNotIn(flag, argv)
        self.assertIn("--vision", argv)


def write_lm(base: str, files: dict) -> dict:
    """A fake mlx_lm package with the pinned files; returns its pins."""
    pins = {}
    for rel in files:
        p = Path(base, "mlx_lm", *rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\n")
        pins[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return pins


class MlxLmPinTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pins = write_lm(self.dir, self.guard.MLX_LM_PINNED_SHA256)
        self.lm = os.path.join(self.dir, "mlx_lm")

    def test_the_real_pins_are_the_0313_wheel(self):
        self.assertEqual(self.guard.MLX_LM_VERSION, "0.31.3")
        self.assertEqual(set(self.guard.MLX_LM_PINNED_SHA256), {
            "__init__.py", "_version.py", "generate.py", "sample_utils.py", "models/__init__.py", "models/cache.py",
            "models/base.py", "models/qwen3_5.py", "models/qwen3_next.py", "models/gated_delta.py",
            "models/activations.py", "models/rope_utils.py", "models/switch_layers.py"})
        self.assertTrue(all(len(v) == 64 and int(v, 16) >= 0 for v in self.guard.MLX_LM_PINNED_SHA256.values()))
        # the empty models/__init__.py is the sha256 of no bytes
        self.assertEqual(self.guard.MLX_LM_PINNED_SHA256["models/__init__.py"], hashlib.sha256(b"").hexdigest())
        # the vllm-mlx files the text argument reads are in the text route's pins already
        for rel in ("scheduler.py", "engine/batched.py", "engine_core.py"):
            self.assertIn(rel, self.guard.PINNED_SHA256)

    def test_matching_files_pass(self):
        self.guard.verify_text(self.lm, "0.31.3", self.pins)

    def test_another_version_is_refused(self):
        with self.assertRaisesRegex(self.guard.GuardError, "0.31.4 is not 0.31.3"):
            self.guard.verify_text(self.lm, "0.31.4", self.pins)

    def test_a_drift_in_any_pinned_file_is_refused(self):
        for rel in self.guard.MLX_LM_PINNED_SHA256:
            with self.subTest(rel):
                path = Path(self.lm, *rel.split("/"))
                text = path.read_text()
                path.write_text(text + "# patched\n")
                with self.assertRaisesRegex(self.guard.GuardError, f"mlx_lm/{rel} differs"):
                    self.guard.verify_text(self.lm, "0.31.3", self.pins)
                path.write_text(text)
                path.unlink()
                with self.assertRaisesRegex(self.guard.GuardError, f"mlx_lm/{rel} unreadable"):
                    self.guard.verify_text(self.lm, "0.31.3", self.pins)
                path.write_text(text)

    def test_the_text_checks_run_on_disk_first_and_then_check_what_was_imported(self):
        # #126 review round 3 (P2b): the preflight reads files only; the imports come after it, each checked
        calls = []
        with patch.object(self.guard, "_locate", lambda name: calls.append(("locate", name)) or self.lm), \
                patch.object(self.guard, "verify_text", lambda d, v, *a: calls.append(("text", v))), \
                patch.object(self.guard, "verify_mlx_files", lambda *a, **k: calls.append(("mlx files",))
                             or ("tag", "core", "lib")):
            found = self.guard._text_preflight()
        self.assertEqual(calls, [("locate", "mlx_lm"), ("text", None), ("mlx files",)])
        self.assertEqual(found, {"mlx_lm": self.lm, "mlx": ("tag", "core", "lib")})
        calls.clear()
        fake = type(sys)("mlx_lm")
        fake.__file__ = os.path.join(self.lm, "__init__.py")
        fake.__version__ = "0.31.3"
        batched = type(sys)("vllm_mlx.engine.batched")
        engine = type(sys)("vllm_mlx.engine")
        engine.batched = batched
        mods = {"mlx_lm": fake, "vllm_mlx.engine": engine, "vllm_mlx.engine.batched": batched}
        with patch.dict(sys.modules, mods), \
                patch.object(self.guard, "verify_text", lambda d, v, *a: calls.append(("text", v))), \
                patch.object(self.guard, "verify_mlx_core", lambda *a: calls.append(("core",) + a)):
            self.assertIs(self.guard._text_modules(found), batched)
            self.assertEqual(calls, [("text", "0.31.3"), ("core", "core", "lib")])
            fake.__file__ = os.path.join(self.dir, "elsewhere", "mlx_lm", "__init__.py")
            with self.assertRaisesRegex(self.guard.GuardError, "mlx_lm was imported from"):
                self.guard._text_modules(found)


def text_config(**over) -> dict:
    cfg = {"model_type": "qwen3_5", "hidden_size": 5120, "intermediate_size": 17408, "num_attention_heads": 24, "num_key_value_heads": 4,
           "head_dim": 256, "linear_num_key_heads": 16, "linear_num_value_heads": 48, "linear_key_head_dim": 128,
           "linear_value_head_dim": 128, "vocab_size": 248320, "num_hidden_layers": 64,
           "full_attention_interval": 4, "linear_conv_kernel_dim": 4, "torch_dtype": "bfloat16"}
    cfg.update(over)
    return {k: v for k, v in cfg.items() if v is not None}


class TextModelCheckTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, cfg) -> str:
        Path(self.dir, "config.json").write_text(json.dumps(cfg))
        return self.dir

    def test_the_dims_are_the_governors(self):
        want = {k: v for k, v in local_governor.TEXT_DIMS.items() if k != "act_bytes"}
        self.assertEqual(self.guard.TEXT_DIMS, want)
        self.assertEqual(self.guard.TEXT_ACT_BYTES, local_governor.TEXT_DIMS["act_bytes"])
        # the KV cache per token the governor reserves is these dimensions' (16 full-attention layers of 64)
        d = local_governor.TEXT_DIMS
        full = d["num_hidden_layers"] // d["full_attention_interval"]
        self.assertEqual(full * 2 * d["num_key_value_heads"] * d["head_dim"] * d["act_bytes"],
                         local_governor.DEFAULTS["kv_bytes_per_token"])
        # and its recurrent state is under per_seq_state_bytes: float32 gated-delta state + the conv state
        linear = d["num_hidden_layers"] - full
        conv = 2 * d["linear_num_key_heads"] * d["linear_key_head_dim"] \
            + d["linear_num_value_heads"] * d["linear_value_head_dim"]
        state = linear * (d["linear_num_value_heads"] * d["linear_value_head_dim"] * d["linear_key_head_dim"] * 4
                          + (d["linear_conv_kernel_dim"] - 1) * conv * d["act_bytes"])
        self.assertLessEqual(state, local_governor.DEFAULTS["per_seq_state_bytes"])

    def test_the_verified_model_passes_at_the_top_level_or_in_text_config(self):
        self.guard.check_text_model(self.write(text_config()))
        self.guard.check_text_model(self.write({"model_type": "qwen3_5", "text_config": text_config(model_type=None)}))
        # mlx-lm's own defaults stand for keys config.json leaves out when they are the verified values
        self.guard.check_text_model(self.write(text_config(full_attention_interval=None,
                                                           linear_conv_kernel_dim=None)))

    def test_any_other_geometry_is_refused(self):
        for over in ({"hidden_size": 4096}, {"head_dim": None}, {"head_dim": 128}, {"num_key_value_heads": 8},
                     {"num_hidden_layers": 48}, {"full_attention_interval": 2}, {"vocab_size": 151936},
                     {"linear_num_value_heads": 64}, {"num_experts": 8}, {"torch_dtype": "float32"},
                     {"torch_dtype": "complex64"}, {"hidden_size": True}, {"model_type": "qwen3_next"},
                     {"model_type": None}, {"text_config": "x"}):
            with self.subTest(over), self.assertRaises(self.guard.GuardError):
                self.guard.check_text_model(self.write(text_config(**over)))
        Path(self.dir, "config.json").write_text("{not json")
        with self.assertRaisesRegex(self.guard.GuardError, "unreadable"):
            self.guard.check_text_model(self.dir)
        with self.assertRaisesRegex(self.guard.GuardError, "unreadable"):
            self.guard.check_text_model(os.path.join(self.dir, "missing"))


class FakeLM:
    pass


FakeLM.__module__ = "mlx_lm.models.qwen3_5"


class FakePB:
    def __init__(self, step):
        self.uids, self.prefill_step_size = [], step


class FakeBG:
    def __init__(self, model=None, batch=8, step=2048):
        self.model = model if model is not None else FakeLM()
        self.prefill_batch_size, self.prefill_step_size = batch, step
        self._prompt_batch, self._generation_batch, self._unprocessed_sequences = FakePB(step), [], []

    def _next(self):
        return [], []


class EnforceTextPrefillTests(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()

    def test_one_sequence_at_a_time_in_chunks_of_at_most_the_policy(self):
        bg = self.guard.enforce_text_prefill(FakeBG(), 256)
        self.assertEqual((bg.prefill_batch_size, bg.prefill_step_size, bg._prompt_batch.prefill_step_size),
                         (1, 256, 256))
        # a smaller step (e.g. --chunked-prefill-tokens 128 on the native API) is kept: it only lowers the peak
        bg = self.guard.enforce_text_prefill(FakeBG(step=128), 256)
        self.assertEqual((bg.prefill_step_size, bg._prompt_batch.prefill_step_size), (128, 128))

    def test_a_generator_the_argument_did_not_read_fails_the_request(self):
        other = type("Llama", (), {})
        cases = {"no seam": FakeBG(), "busy prompt batch": FakeBG(), "other model": FakeBG(model=other()),
                 "bad step": FakeBG(step=0), "bool batch": FakeBG(batch=True)}
        del cases["no seam"]._prompt_batch
        cases["busy prompt batch"]._prompt_batch.uids.append(1)
        for name, bg in cases.items():
            with self.subTest(name), self.assertRaisesRegex(RuntimeError, self.guard.TEXT_PREFILL_REFUSAL):
                self.guard.enforce_text_prefill(bg, 256)

    def test_parse_text(self):
        policy, rest = self.guard.parse_text(["--context-tokens", "9", *TEXT])
        self.assertEqual((policy.prefill_tokens, policy.buffer_cache_bytes, rest),
                         (256, GIB, ["--context-tokens", "9"]))
        self.assertEqual(self.guard.parse_text(["--context-tokens", "9"]), (None, ["--context-tokens", "9"]))
        for bad in (["--text-prefill-tokens", "256"], ["--text-prefill-tokens", "x", "--buffer-cache-bytes", "1"],
                    ["--text-prefill-tokens", "0", "--buffer-cache-bytes", "1"], [*TEXT, "--text-prefill-tokens", "1"],
                    ["--buffer-cache-bytes"]):
            with self.subTest(bad), self.assertRaises(self.guard.GuardError):
                self.guard.parse_text(bad)


class InstallTextTests(GuardBase):
    """The guard on the fake vllm-mlx (tests/test_vllm_mlx_guard.py): every batch generator the Scheduler builds is
    held to the policy, and the buffer cache limit vllm-mlx sets at engine start is the policy's."""

    def test_serve_installs_the_text_prefill_bounds(self):
        rc, err = self.run_main(SERVE_TEXT)
        self.assertEqual(rc, 0, err)
        from vllm_mlx import scheduler
        from vllm_mlx.engine import batched
        bg = scheduler.Scheduler(config=scheduler.SchedulerConfig(enable_prefix_cache=False))._create_batch_generator()
        self.assertEqual((bg.prefill_batch_size, bg.prefill_step_size, bg._prompt_batch.prefill_step_size),
                         (1, 256, 256))
        self.assertEqual(batched._resolve_metal_buffer_cache_limit(48 * GIB, 0.9),
                         (GIB, "cortados guard (--buffer-cache-bytes)"))
        self.assertEqual(self.text_models, ["/models/text"])           # config.json checked before serving
        self.assertIn("text prefill one sequence at a time in chunks of at most 256", err)

    def test_the_text_route_needs_its_flags_and_vision_mode_refuses_them(self):
        rc, err = self.run_main(["--context-tokens", "131072", "--", "serve", "/models/text", "--continuous-batching"])
        self.assertEqual(rc, 2)
        self.assertIn("--text-prefill-tokens", err)
        rc, err = self.run_main(["--context-tokens", "131072", *TEXT, "--vision", "--", "serve", "/m",
                                 "--continuous-batching"])
        self.assertEqual(rc, 2)
        from vllm_mlx import cli
        self.assertEqual(cli.CALLS, [])

    def test_a_model_the_reservation_was_not_computed_for_stops_startup(self):
        def refuse(path):
            raise self.guard.GuardError("text route: config.json hidden_size is 4096")
        with patch.object(self.guard, "check_text_model", refuse):
            rc, err = self.run_main(SERVE_TEXT)
        self.assertEqual(rc, 2)
        self.assertIn("hidden_size is 4096", err)
        from vllm_mlx import cli, scheduler
        self.assertEqual(cli.CALLS, [])
        self.assertFalse(hasattr(scheduler.Scheduler._create_batch_generator, "__cortados_guard__"))

    def test_installed_once_and_a_missing_seam_stops_install(self):
        from vllm_mlx import scheduler
        from vllm_mlx.engine import batched
        policy = self.guard.TextPolicy(256, GIB)
        self.guard.install_text(policy, scheduler.Scheduler, batched)
        with self.assertRaisesRegex(self.guard.GuardError, "already installed"):
            self.guard.install_text(policy, scheduler.Scheduler, batched)
        with self.assertRaisesRegex(self.guard.GuardError, "missing"):
            self.guard.install_text(policy, type("S", (), {}), batched)

    def test_check_runs_the_text_checks(self):
        rc, err = self.run_main(["--check", "/models/text"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.text_models, ["/models/text"])


SERVE_TEXT = ["--context-tokens", "131072", *TEXT, "--", "serve", "/models/text", "--continuous-batching",
              "--disable-prefix-cache"]



class TextSetDrainTests(unittest.TestCase):
    """#126 review rounds 2 and 3 (P2a): the departed turns' width and rows stay reserved while any text turn runs (the
    lazy-free protection), and the set drains as soon as they withhold room the live turns would leave as a fresh set:
    from the governor's own state (a departure, an admission, a snapshot), not only from a refused POST, because the
    provider reads the capacity report first and never sends a turn the report has no room for."""

    def setUp(self):
        self.a = local_governor.Admission(FixedGovernor())             # owner idle: count 8, 25 GiB beside the weights
        self.cfg = self.a.governor.cfg
        from cortados_router.provider import local_lane
        self.lane = local_lane
        self.node = {"node": "test-drain-node"}
        self.addCleanup(local_lane._gate_reports.pop, self.node["node"], None)

    def short(self):
        return self.a.try_acquire(load=self.a.text_load(100, 1))

    def preflight(self, tokens):
        """What the provider decides before sending (routing: local_lane.gate_fit on the gate's latest report)."""
        self.lane._note_gate(self.node, {"backend": "vllm-mlx", **self.a.snapshot()})
        return self.lane.gate_fit(self.node, tokens)

    def fresh_room(self, loads):
        return local_governor.text_room(self.cfg, loads, local_governor.available_bytes(self.cfg))

    def test_round3_repro_the_preflight_cannot_keep_the_stale_width_forever(self):
        # the reviewer's repro: a departed 64k width kept by two short turns, 100 rounds of "the provider's preflight
        # skips a third, one short turn finishes, its replacement is admitted". Before: never draining, the
        # high-water 64,001, 17.6 GiB held, free_tokens 0 forever. Now the governor drains on its own state
        tokens = local_governor.request_tokens(self.cfg, 100, 1)
        t_long, _ = self.a.try_acquire(load=self.a.text_load(64000, 1))
        live = [self.short()[0]]
        t_long.release()
        # one short turn beside the kept width withholds no room yet (a second row at 64k fits either way)
        self.assertFalse(self.a.snapshot()["draining"])
        live.append(self.short()[0])
        self.assertTrue(all(live))
        # two do: a third row at the kept 64k width fits nowhere, while a fresh set has room. The governor drains on
        # its own state, without any refused POST
        self.assertTrue(self.a._draining)
        recovered = None
        for step in range(100):
            fit = self.preflight(tokens)
            if fit is None:                                            # the provider sends the turn
                t, snap = self.short()
                self.assertIsNotNone(t, snap.get("reason"))
                live.append(t)
                if not self.a._draining and self.a._high_water < 64000:
                    recovered = step
                    break
            else:
                self.assertEqual(fit[0], "busy")                       # retryable, never "too_large"
                self.assertIn("draining", self.lane.gate_report(self.node)["reason"])
            if live:
                live.pop(0).release()                                  # one short turn finishes
            t, snap = self.short()                                      # its replacement
            if t is not None:
                live.append(t)
            else:
                self.assertEqual(snap["refused"], "busy")
        self.assertIsNotNone(recovered, "the stale width was never reclaimed")
        self.assertLessEqual(recovered, 3)                             # bounded by the two turns of the old set
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(self.cfg, [t.load for t in live])["total"])
        self.assertLess(self.a.reserved, 6 * GIB)
        self.assertEqual(self.a.snapshot()["free_tokens"], self.fresh_room([t.load for t in live]))

    def test_round2_repro_a_refused_turn_still_drains_the_set(self):
        # the direct-admission path (a turn the preflight let through, refused for the stale bytes alone)
        t_long, _ = self.a.try_acquire(load=self.a.text_load(64000, 1))
        live, _ = self.short()
        t_long.release()
        second, _ = self.short()
        self.assertIsNotNone(second)
        self.a._draining = False                                       # as if the governor had not looked yet
        third, snap = self.short()
        self.assertEqual((third, snap["refused"], self.a._high_water), (None, "busy", 64001))
        self.assertIn("draining", snap["reason"])
        self.assertTrue(self.a._draining)
        self.assertEqual(snap["free_tokens"], 0)                       # the provider sees no room while draining
        for _ in range(3):
            t, snap = self.short()
            self.assertEqual((t, snap["refused"]), (None, "busy"))
            self.assertIn("text set draining", snap["reason"])
        live.release()
        self.assertTrue(self.a._draining)                              # one turn of the old set still runs
        second.release()
        self.assertEqual((self.a._draining, self.a._high_water, self.a._peak, self.a.reserved), (False, 0, (0, 0), 0))
        tickets = [self.short()[0] for _ in range(3)]                  # the third tiny turn now runs, at the fresh size
        self.assertTrue(all(tickets))
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(self.cfg, [t.load for t in tickets])["total"])

    def test_the_departed_width_stays_reserved_while_its_set_runs(self):
        # the lazy-free protection: no byte of the departed 64k row is given back while a turn that may share its
        # batch runs, drain or no drain
        long, short = self.a.text_load(64000, 1), self.a.text_load(100, 1)
        t_long, t_short = self.a.try_acquire(load=long)[0], self.a.try_acquire(load=short)[0]
        t_long.release()
        t2 = self.short()[0]
        self.assertIsNotNone(t2)
        self.assertTrue(self.a._draining)
        stale = local_governor.text_set_bytes(self.cfg, [short, t2.load], long.tokens, self.a._peak)["total"]
        self.assertEqual(self.a.reserved, stale)
        self.assertGreater(stale, local_governor.text_set_bytes(self.cfg, [short, t2.load])["total"])
        refused, snap = self.short()
        self.assertIsNone(refused)
        self.assertEqual(self.a.reserved, stale)                       # a refusal changes nothing
        t_short.release()
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(self.cfg, [t2.load], long.tokens,
                                                                        self.a._peak)["total"])
        t2.release()
        self.assertEqual(self.a.reserved, 0)

    def test_no_drain_while_the_kept_terms_withhold_no_room(self):
        # overlapping turns of similar size: the departed one's rows stay reserved (bytes), but the room the report
        # advertises is the fresh set's, so no request size is held back and admission goes on without a drain
        a, b = self.a.text_load(20000, 8000), self.a.text_load(20000, 8000)
        ta, tb = self.a.try_acquire(load=a)[0], self.a.try_acquire(load=b)[0]
        ta.release()
        self.assertGreater(self.a.reserved, local_governor.text_set_bytes(self.cfg, [b])["total"])   # bytes kept
        snap = self.a.snapshot()
        self.assertFalse(snap["draining"])
        self.assertEqual(snap["free_tokens"], self.fresh_room([b]))
        for _ in range(20):                                            # steady overlap: never a drain
            nxt, snap = self.a.try_acquire(load=self.a.text_load(20000, 8000))
            self.assertIsNotNone(nxt, snap.get("reason"))
            tb.release()
            tb = nxt
            self.assertFalse(self.a.snapshot()["draining"])
        # a turn too big for the live set even at its fresh size is plain busy: draining would not admit it
        big, snap = self.a.try_acquire(load=self.a.text_load(120_000, 4096))
        self.assertEqual((big, snap["refused"]), (None, "busy"))
        self.assertNotIn("draining", snap["reason"])
        self.assertFalse(self.a._draining)
        # the count limit is the governor's sample, never a drain
        busy = local_governor.Admission(FixedGovernor(idle_s=0.0))      # owner active: the active cap
        while True:
            t, snap = busy.try_acquire(load=busy.text_load(100, 1))
            if t is None:
                break
        self.assertFalse(busy._draining)
        self.assertNotIn("draining", snap["reason"])


class PrefixCacheOffTests(GuardBase):
    """#126 review round 2 (P1): vllm-mlx's prefix cache pool (up to 20% of free RAM) and its insertion snapshots are
    outside the text reservation, so the text route serves with it off, and the guard refuses to start otherwise."""

    def test_the_launch_agent_turns_the_prefix_cache_off_on_the_text_route(self):
        c = local_backend.load(str(ROOT / "tables" / "local-backend.example.json"))
        argv = local_backend.serve_argv(c)
        self.assertEqual(local_backend.PREFIX_CACHE_OFF, "--disable-prefix-cache")
        self.assertEqual(local_backend.PREFIX_CACHE_OFF, self.guard.PREFIX_CACHE_FLAG)
        self.assertIn("--disable-prefix-cache", argv[argv.index("--") + 1:])     # a vllm-mlx serve argument
        self.assertNotIn("--enable-prefix-cache", argv)
        self.assertIn("--disable-prefix-cache", local_backend.text_argv(c)[1])
        plist = local_backend.launch_agent_plist(c, "/tmp/logs")
        self.assertIn(b"<string>--disable-prefix-cache</string>", plist)

    def test_the_guard_refuses_a_cache_enabled_start(self):
        rc, err = self.run_main(SERVE_TEXT[:-1])                       # serve without --disable-prefix-cache
        self.assertEqual(rc, 2)
        self.assertIn(self.guard.PREFIX_CACHE_REFUSAL, err)
        from vllm_mlx import cli
        self.assertEqual(cli.CALLS, [])
        # a different spelling (argparse would expand `--disable-prefix`) is not the flag the argument read
        rc, err = self.run_main([*SERVE_TEXT[:-1], "--disable-prefix"])
        self.assertEqual(rc, 2)
        self.assertIn(self.guard.PREFIX_CACHE_FLAG, err)

    def test_the_runtime_refuses_a_scheduler_with_a_prefix_cache(self):
        # the flag reached the guard, but vllm-mlx's cli did not apply it (here: a mutated, re-pinned cli.py):
        # the Scheduler the engine builds is refused before it holds a pool
        cli = Path(self.dir, "vllm_mlx", "cli.py")
        cli.write_text(cli.read_text().replace('enable_prefix_cache="--disable-prefix-cache" not in sys.argv',
                                               "enable_prefix_cache=True"))
        self.repin()
        with self.assertRaisesRegex(self.guard.GuardError, "enable_prefix_cache is True"):
            self.run_main(SERVE_TEXT)
        from vllm_mlx import scheduler, server
        self.assertIsNone(server._engine)
        with self.assertRaisesRegex(self.guard.GuardError, "no scheduler config"):
            scheduler.Scheduler()                                      # 0.6.0's default config enables the cache
        # a Scheduler that builds a cache although its config says off is refused after its __init__
        original = scheduler.Scheduler.__init__.__wrapped__

        def leaky(self, *a, **k):
            original(self, *a, **k)
            self.block_aware_cache = object()
        with patch.object(scheduler.Scheduler, "__init__", self._rewrap(scheduler.Scheduler, leaky)):
            with self.assertRaisesRegex(self.guard.GuardError, "built block_aware_cache"):
                scheduler.Scheduler(None, None, scheduler.SchedulerConfig(enable_prefix_cache=False))

    def _rewrap(self, cls, init):
        """install_text's wrapper around `init` (a fresh install on a copy of the class)."""
        from vllm_mlx.engine import batched
        copy = type("Scheduler", (), {"__init__": init, "_create_batch_generator": lambda self: None})
        fake_batched = type(sys)("batched")
        fake_batched._resolve_metal_buffer_cache_limit = batched._resolve_metal_buffer_cache_limit.__wrapped__
        self.guard.install_text(self.guard.TextPolicy(256, GIB), copy, fake_batched)
        return copy.__init__

    def test_round2_repro_cache_occupancy_cannot_change_the_bound(self):
        # the reviewer's numbers: a full-context turn reserves 24.57 of 25 GiB, and a 4 GiB prefix pool beside it
        # would make 28.57. With the cache off there is no pool: the served engine's scheduler holds none, so what
        # the process holds beyond the weights is the reservation, whatever earlier turns were cached
        rc, err = self.run_main(SERVE_TEXT)
        self.assertEqual(rc, 0, err)
        self.assertIn("prefix cache off", err)
        from vllm_mlx import server
        sched = server._engine.scheduler
        self.assertIs(sched.config.enable_prefix_cache, False)
        for attr in self.guard.PREFIX_CACHE_ATTRS:
            self.assertIsNone(getattr(sched, attr), attr)
        cfg = local_governor.check_config({})
        full = local_governor.text_set_bytes(cfg, [local_governor.text_load(cfg, 131071, 1)])["total"]
        self.assertAlmostEqual(full / GIB, 24.57, places=2)
        pool = 0                                                       # no prefix cache: nothing to occupy
        self.assertLessEqual(full + pool, local_governor.available_bytes(cfg))
        self.assertGreater(full + 4 * GIB, local_governor.available_bytes(cfg))   # why the pool had to go


class MlxPythonPinTests(VisionGuardBase):
    """#126 review round 2 (P2b): mlx's Python files on the models' import chain are pinned to the released 0.32.4
    wheels, so a drifted mlx.nn (e.g. silu promoting to float32) stops startup."""

    NAMES = {"__array_api_info.py", "_reprlib_fix.py", "utils.py", "nn/__init__.py", "nn/init.py", "nn/losses.py",
             "nn/utils.py", "nn/layers/__init__.py", "nn/layers/activations.py", "nn/layers/base.py",
             "nn/layers/containers.py", "nn/layers/convolution.py", "nn/layers/convolution_transpose.py",
             "nn/layers/distributed.py", "nn/layers/dropout.py", "nn/layers/embedding.py", "nn/layers/linear.py",
             "nn/layers/normalization.py", "nn/layers/pooling.py", "nn/layers/positional_encoding.py",
             "nn/layers/quantized.py", "nn/layers/recurrent.py", "nn/layers/transformer.py", "nn/layers/upsample.py"}

    def test_the_real_python_pins_are_the_released_wheels(self):
        real = load_guard()
        self.assertEqual(set(real.MLX_PY_SHA256), self.NAMES)
        self.assertEqual(set(real.MLX_PY_PINNED_SHA256), set(real.MLX_PINNED_RECORD))      # every pinned wheel tag
        for tag, table in real.MLX_PY_PINNED_SHA256.items():
            self.assertEqual(table, real.MLX_PY_SHA256, tag)            # byte-identical in all 27 macOS wheels
        for rel, want in (("nn/layers/activations.py",
                           "244a4acb0f77fac196e3e355dcae0a5195691b7f9f12d6c5aeb243c6cb021ae1"),
                          ("nn/layers/quantized.py", "8ca4574a212f14cbec4c446826474583ef0bf1e2cb1630574fd730c77af03b33"),
                          ("nn/layers/normalization.py",
                           "d038fced58d7bf972e80e5ea508c9285e81bd9d523773deb50c5a935b25af9af"),
                          ("utils.py", "e55d7c63e7b706a0d31728343760f3f98d90e44d34054be4cf076586fdbe1616")):
            self.assertEqual(real.MLX_PY_SHA256[rel], want, rel)
        self.assertTrue(all(len(v) == 64 and int(v, 16) >= 0 for v in real.MLX_PY_SHA256.values()))
        self.assertEqual(set(real.MLX_PY_MODULES), {"nn", "utils", "_reprlib_fix", "__array_api_info"})

    def test_matching_files_pass(self):
        self.assertEqual(self.guard.verify_mlx(), FAKE_MLX_TAG)

    def test_a_drift_in_any_pinned_python_file_is_refused(self):
        for rel in sorted(self.mlx_py_pins):
            with self.subTest(rel):
                path = Path(self.dir, "mlx", *rel.split("/"))
                text = path.read_text()
                path.write_text(text + "# patched\n")
                try:
                    with self.assertRaisesRegex(self.guard.GuardError, f"mlx/{rel} differs"):
                        self.guard.verify_mlx()
                    path.unlink()
                    with self.assertRaisesRegex(self.guard.GuardError, f"mlx/{rel} unreadable"):
                        self.guard.verify_mlx()
                finally:
                    path.write_text(text)
        self.guard.verify_mlx()

    def test_round2_repro_silu_promoting_to_float32_is_refused(self):
        act = Path(self.dir, "mlx", "nn", "layers", "activations.py")
        released = ("import mlx.core as mx\n\n\ndef silu(x):\n    return x * mx.sigmoid(x)\n")
        act.write_text(released)
        self.mlx_py_pins["nn/layers/activations.py"] = hashlib.sha256(released.encode()).hexdigest()
        self.assertEqual(self.guard.verify_mlx(), FAKE_MLX_TAG)
        act.write_text(released.replace("return x * mx.sigmoid(x)",
                                        "x = x.astype(mx.float32)\n    return x * mx.sigmoid(x)"))
        with self.assertRaisesRegex(self.guard.GuardError, "mlx/nn/layers/activations.py differs"):
            self.guard.verify_mlx()
        # and startup stops: --check runs verify_mlx (vision mode here; the text route through _text_modules)
        rc, err = self.run_main(["--check", str(self.models / "vl"), *VISION_ARGS])
        self.assertEqual(rc, 2)
        self.assertIn("activations.py differs", err)

    def test_a_planted_init_a_shadowed_module_or_an_unpinned_tag_is_refused(self):
        init = Path(self.dir, "mlx", "__init__.py")
        init.write_text("")
        try:
            with self.assertRaisesRegex(self.guard.GuardError, "mlx/__init__.py exists"):
                self.guard.verify_mlx()
        finally:
            init.unlink()
        other = Path(self.tmp, "mlx", "nn", "__init__.py")
        other.parent.mkdir(parents=True)
        other.write_text("# a shadowing mlx.nn\n")
        spec = type("Spec", (), {"origin": str(other)})
        with self.assertRaisesRegex(self.guard.GuardError, "mlx.nn resolves to"):
            self.guard.verify_mlx(find_spec=lambda name: spec if name == "mlx.nn" else
                                  type("S", (), {"origin": str(Path(self.dir, "mlx", name[4:] + ".py"))}))
        with self.assertRaisesRegex(self.guard.GuardError, "no Python file pins"):
            self.guard.verify_mlx(py_pins={})


if __name__ == "__main__":
    unittest.main()
