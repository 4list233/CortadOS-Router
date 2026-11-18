"""Pro vision (#125): still images on the local MLX lane, bounded before they cost memory.

  * image_check: the header-only checks (real type, dimensions, animation, bombs) and the Qwen resize/token count;
  * local_governor: the vision policy, image_policy over an Anthropic body, the image-aware reservation and the image
    slot in Admission's one atomic step;
  * local_gate: image turns relayed intact, refused before admission when outside the policy, released however they end;
  * the vllm-mlx guard's vision mode, against a fake vllm-mlx 0.6.0 MLLM stack and a fake mlx-vlm 0.7.12 that mirror
    the control flow the guard relies on (MLLMScheduler.add_request -> MLLMBatchGenerator._preprocess_request with its
    pixel-cache hit and miss branches, process_image_input, prepare_inputs expanding image placeholders);
  * local_backend / local_lane: the launch line and the provider's image-aware estimate.
Fakes and stubs only: no model, no MLX, no PIL, no network."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import struct
import tempfile
import textwrap
import threading
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from cortados_router import image_check, local_backend, local_gate, local_governor
from cortados_router.provider import local_lane
from tests.test_local_mlx import (GIB, MLX, SERVED, TAG, FakeVllm, MlxGateHarness, reset_fake, turns, wait_for)
from tests.test_local_gate import TOKEN
from tests.test_vllm_mlx_guard import GuardBase, ROOT

import base64

# --------------------------------------------------------------------------------------------------- image fixtures


def png(w, h, apng=False, idat=True):
    ihdr = struct.pack(">II", w, h) + bytes([8, 6, 0, 0, 0])
    out = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\0\0\0\0"
    if apng:
        out += struct.pack(">I", 8) + b"acTL" + b"\0" * 8 + b"\0\0\0\0"
    if idat:
        out += struct.pack(">I", 2) + b"IDAT" + b"xx" + b"\0\0\0\0"
    return out + struct.pack(">I", 0) + b"IEND" + b"\0\0\0\0"


def jpeg(w, h, sofs=1, mpo=False):
    out = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\x01\x01\0\0\x01\0\x01\0\0"
    if mpo:
        out += b"\xff\xe2" + struct.pack(">H", 10) + b"MPF\0" + b"\0\0\0\0"
    for _ in range(sofs):
        out += b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x22\0\x02\x11\x01\x03\x11\x01"
    return out + b"\xff\xda" + struct.pack(">H", 12) + b"\x03\x01\0\x02\x11\x03\x11\0\x3f\0" + b"\x12\x34" + b"\xff\xd9"


def riff(chunks):
    body = b"WEBP" + chunks
    return b"RIFF" + struct.pack("<I", len(body)) + body


def chunk(kind, data):
    return kind + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def webp_vp8l(w, h):
    return riff(chunk(b"VP8L", b"\x2f" + struct.pack("<I", (w - 1) | ((h - 1) << 14)) + b"\0" * 6))


def webp_vp8(w, h):
    return riff(chunk(b"VP8 ", b"\0\0\0\x9d\x01\x2a" + struct.pack("<HH", w, h) + b"\0" * 8))


def webp_vp8x(w, h, flag=False, anmf=False):
    head = bytes([0x02 if flag else 0]) + b"\0\0\0" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    out = chunk(b"VP8X", head)
    if anmf:
        out += chunk(b"ANMF", b"\0" * 16)
    return riff(out + chunk(b"VP8L", b"\x2f" + struct.pack("<I", (w - 1) | ((h - 1) << 14)) + b"\0" * 6))


def b64(raw):
    return base64.b64encode(raw).decode()


def data_url(raw, media="image/png"):
    return f"data:{media};base64,{b64(raw)}"


def image_block(raw=None, media="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64(raw or png(800, 600))}}


VISION = local_governor.check_vision(True)
POLICY = dict(max_bytes=VISION["max_image_bytes"], max_pixels=VISION["max_image_pixels"])


# --------------------------------------------------------------------------------------------------- image_check

class ImageCheckTests(unittest.TestCase):
    def test_png_jpeg_and_webp_stills_are_read_from_their_headers(self):
        for raw, media, dims in ((png(1920, 1080), "image/png", (1920, 1080)),
                                 (jpeg(640, 480), "image/jpeg", (640, 480)),
                                 (webp_vp8l(300, 200), "image/webp", (300, 200)),
                                 (webp_vp8(321, 123), "image/webp", (321, 123)),
                                 (webp_vp8x(4000, 3000), "image/webp", (4000, 3000))):
            with self.subTest(media=media, dims=dims):
                info, got = image_check.check_base64(media, b64(raw), **POLICY)
                self.assertEqual((info["width"], info["height"]), dims)
                self.assertEqual(got, raw)

    def test_animated_multipage_and_other_formats_are_refused(self):
        for raw, media, why in ((png(10, 10, apng=True), "image/png", "animated PNG"),
                                (jpeg(10, 10, mpo=True), "image/jpeg", "multi-picture"),
                                (jpeg(10, 10, sofs=2), "image/jpeg", "exactly one frame"),
                                (jpeg(10, 10, sofs=0), "image/jpeg", "exactly one frame"),
                                (webp_vp8x(10, 10, flag=True), "image/webp", "animated WebP"),
                                (webp_vp8x(10, 10, anmf=True), "image/webp", "animated WebP"),
                                (b"GIF89a" + b"\0" * 40, "image/gif", "media_type"),
                                (b"GIF89a" + b"\0" * 40, "image/png", "not a PNG"),
                                (png(10, 10, idat=False)[:-12], "image/png", "without image data")):
            with self.subTest(why), self.assertRaises(image_check.ImageRefused) as cm:
                image_check.check_base64(media, b64(raw), **POLICY)
            self.assertIn(why, str(cm.exception))

    def test_the_declared_type_must_be_the_real_one(self):
        with self.assertRaises(image_check.ImageRefused) as cm:
            image_check.check_base64("image/png", b64(jpeg(10, 10)), **POLICY)
        self.assertIn("declared image/png but its bytes are jpeg", str(cm.exception))

    def test_malformed_base64_and_truncated_files_are_refused(self):
        for media, data in (("image/png", "not base64!"), ("image/png", b64(png(5, 5)) + "\n"), ("image/png", ""),
                            ("image/png", None), ("image/jpeg", b64(jpeg(5, 5)[:20])), ("image/webp", b64(b"RIFF\0\0"))):
            with self.subTest(data=str(data)[:20]), self.assertRaises(image_check.ImageRefused):
                image_check.check_base64(media, data, **POLICY)

    def test_an_oversized_image_is_refused_before_it_is_decoded(self):
        big = "A" * (image_check.b64_max_len(1000) + 4)
        with patch.object(image_check.base64, "b64decode", side_effect=AssertionError("decoded")):
            with self.assertRaises(image_check.ImageRefused) as cm:
                image_check.check_base64("image/png", big, max_bytes=1000, max_pixels=10 ** 9)
        self.assertIn("over 1000 bytes", str(cm.exception))
        raw = png(10, 10) + b"\0" * 2000                           # decodes past the bound: refused too
        with self.assertRaises(image_check.ImageRefused):
            image_check.check_base64("image/png", b64(raw), max_bytes=len(raw) - 1, max_pixels=10 ** 9)

    def test_a_decompression_bomb_is_refused_from_its_header(self):
        # A ~70-byte PNG whose header says 50,000 x 50,000 (2.5 G pixels, 10 GB RGBA): never decoded.
        for raw, media in ((png(50000, 50000), "image/png"), (jpeg(65000, 65000), "image/jpeg"),
                           (webp_vp8x(16000, 16000), "image/webp")):
            with self.subTest(media), self.assertRaises(image_check.ImageRefused) as cm:
                image_check.check_base64(media, b64(raw), **POLICY)
            self.assertIn("pixels", str(cm.exception))
        info, _ = image_check.check_base64("image/png", b64(png(7680, 4320)), **POLICY)   # an 8K screenshot fits
        self.assertEqual(info["width"], 7680)

    def test_zero_dimensions_and_extreme_aspect_ratios_are_refused(self):
        for raw in (png(0, 10), png(10, 0), png(201 * 4, 4)):
            with self.subTest(len(raw)), self.assertRaises(image_check.ImageRefused):
                image_check.check_base64("image/png", b64(raw), **POLICY)

    def test_only_inline_data_urls_are_taken(self):
        for url in ("https://example.com/a.png", "http://169.254.169.254/latest", "file:///etc/passwd",
                    "data:image/png,rawdata", "data:image/png;base64", {"url": "data:image/png;base64,AA"}):
            with self.subTest(url=str(url)[:30]), self.assertRaises(image_check.ImageRefused):
                image_check.check_data_url(url, **POLICY)
        info, _ = image_check.check_data_url(data_url(png(64, 64)), **POLICY)
        self.assertEqual(info["kind"], "png")

    def test_image_tokens_follow_the_qwen_resize_and_never_pass_1024_at_one_megapixel(self):
        kw = dict(min_pixels=65536, max_pixels=1024 * 1024)
        self.assertEqual(image_check.image_tokens(1000, 1000, **kw), 961)      # 992 x 992 / 1,024
        self.assertEqual(image_check.image_tokens(1080, 1920, **kw), 1008)     # 768 x 1344, beta-scaled under 1 MP
        self.assertEqual(image_check.image_tokens(10, 10, **kw), 64)           # scaled up to min_pixels
        self.assertEqual(image_check.image_tokens(4320, 7680, **kw), image_check.image_tokens(7680, 4320, **kw))
        for h in range(1, 5000, 97):
            for w in (h, h * 3, max(1, h // 7), 199 * h):
                if max(h, w) <= 200 * min(h, w):
                    self.assertLessEqual(image_check.image_tokens(h, w, **kw), 1024, (h, w))
        # upstream's 16.7 MP default: 16,384 tokens for the same 8K screenshot class of image
        self.assertGreater(image_check.image_tokens(4320, 7680, min_pixels=65536, max_pixels=16777216), 16000)


# --------------------------------------------------------------------------------------------------- governor

def vision_cfg(**vision):
    return local_governor.check_config({}, {"vision": vision or True})


def body_with(*blocks, text="what is this?", **extra):
    return {"model": TAG, "max_tokens": extra.pop("max_tokens", 1000),
            "messages": [{"role": "user", "content": [*blocks, {"type": "text", "text": text}]}], **extra}


class VisionPolicyTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        v = local_governor.check_vision(True)
        self.assertEqual((v["max_images"], v["image_tokens_max"], v["image_context_tokens"], v["image_concurrency"],
                          v["prefill_chunk_tokens"], v["hidden_size"], v["vocab_size"]),
                         (5, 1024, 65536, 1, 256, 5120, 248320))
        with self.assertRaisesRegex(local_governor.GovernorConfigError, "vision_margin_bytes was replaced"):
            local_governor.check_vision({"vision_margin_bytes": 2 * GIB})   # the fixed margin is gone, by name
        for bad in ({"prefill_chunk_tokens": 70000}, {"act_bytes": 3}):
            with self.subTest(bad), self.assertRaises(local_governor.GovernorConfigError):
                local_governor.check_vision(bad)
        self.assertIsNone(local_governor.check_vision(None))
        self.assertIsNone(local_governor.check_config({})["vision"])         # text-only unless declared
        for bad in ({"max_images": 0}, {"image_tokens_max": True}, {"nope": 1}, {"image_context_tokens": 512}, "x"):
            with self.subTest(bad), self.assertRaises(local_governor.GovernorConfigError):
                local_governor.check_vision(bad)
        with self.assertRaises(local_governor.GovernorConfigError):          # over the model's context
            local_governor.check_config({"context_tokens": 32768}, {"vision": True})
        self.assertEqual(vision_cfg(image_concurrency=2)["vision"]["image_concurrency"], 2)

    def test_five_images_pass_and_a_sixth_is_refused(self):
        why, plan = local_governor.image_policy(body_with(*[image_block() for _ in range(5)]), VISION)
        self.assertIsNone(why)
        self.assertEqual(len(plan), 5)
        self.assertEqual(plan.data_chars, 5 * len(b64(png(800, 600))))
        why, plan = local_governor.image_policy(body_with(*[image_block() for _ in range(6)]), VISION)
        self.assertEqual(why, "more than 5 images")
        self.assertEqual(len(plan), 0)
        # counted across the whole conversation, not per message
        msgs = [{"role": "user", "content": [image_block() for _ in range(3)]},
                {"role": "assistant", "content": "ok"}, {"role": "user", "content": [image_block() for _ in range(3)]}]
        self.assertEqual(local_governor.image_policy({"messages": msgs}, VISION)[0], "more than 5 images")

    def test_url_sources_nested_images_documents_and_non_user_images_are_refused(self):
        url = {"type": "image", "source": {"type": "url", "url": "https://example.com/x.png"}}
        nested = {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "shot"},
                                                                           image_block()]}
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "AA=="}}
        cases = [(body_with(url), "not an inline base64 image"),
                 (body_with(nested), "images inside a tool result are not carried"),
                 (body_with(doc), "document block"),
                 ({"messages": [{"role": "assistant", "content": [image_block()]}]}, "non-user message"),
                 ({"system": [{"type": "text", "text": "s"}, image_block()], "messages": []}, "non-text system"),
                 (body_with(image_block(png(50000, 50000))), "pixels"),
                 (body_with(image_block(jpeg(10, 10), media="image/png")), "but its bytes are jpeg"),
                 (body_with({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": "%%%"}}), "not valid base64")]
        for body, why in cases:
            with self.subTest(why):
                got, plan = local_governor.image_policy(body, VISION)
                self.assertIn(why, got)
                self.assertEqual(len(plan), 0)

    def test_text_only_turns_pass_unchanged(self):
        for body in ({"messages": [{"role": "user", "content": "hi"}]},
                     {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                                                 "content": [{"type": "text", "text": "x"}]}]}]}):
            self.assertEqual(local_governor.image_policy(body, VISION)[0], None)


class VisionReservationTests(unittest.TestCase):
    def test_the_image_data_is_left_out_of_the_text_bound_and_each_image_counts_its_ceiling(self):
        cfg = vision_cfg()
        body = body_with(image_block(), image_block(jpeg(4000, 3000), "image/jpeg"), max_tokens=2000)
        raw = json.dumps(body).encode()
        _, plan = local_governor.image_policy(body, cfg["vision"])
        full = local_governor.input_tokens(raw, cfg["context_tokens"])
        text = local_governor.input_tokens(raw, cfg["context_tokens"], plan.data_chars)
        self.assertEqual(full - text, plan.data_chars)                    # exactly the validated data, nothing else
        stripped = json.loads(raw)
        for b in stripped["messages"][0]["content"][:2]:
            b["source"]["data"] = ""
        # still an upper bound: never below the bound of the same body with the image data emptied
        self.assertGreaterEqual(text, local_governor.input_tokens(json.dumps(stripped).encode(), None))
        tokens = local_governor.request_tokens(cfg, text, 2000, images=2)
        self.assertEqual(tokens, text + 2 * 1024 + 2000)
        self.assertEqual(local_governor.request_bytes(cfg, text, 2000, images=2),
                         tokens * 65536 + cfg["per_seq_state_bytes"]
                         + local_governor.image_prefill_bytes(cfg, tokens, 2)["total"])

    def test_an_image_request_is_capped_at_the_image_context_and_a_text_one_is_not(self):
        cfg = vision_cfg()
        self.assertEqual(local_governor.request_tokens(cfg, 100_000, 64_000, images=1), 65536)
        self.assertEqual(local_governor.request_tokens(cfg, 100_000, 64_000), 131072)
        self.assertEqual(local_governor.request_bytes(cfg, 100_000, 64_000, images=1),
                         65536 * 65536 + cfg["per_seq_state_bytes"]
                         + local_governor.image_prefill_bytes(cfg, 65536, 1)["total"])
        with self.assertRaises(ValueError):                                  # no images on a text-only model
            local_governor.request_tokens(local_governor.check_config({}), 10, 10, images=1)

    def test_an_image_slot_is_taken_with_the_count_and_bytes_in_one_step(self):
        gov = local_governor.Governor(vision_cfg(), idle_reader=lambda: 30.0, pressure_reader=lambda: "normal",
                                      clock=lambda: 0.0)                       # owner active: count 2
        a = local_governor.Admission(gov)
        img = a.request_bytes(5000, 4000, images=1)
        t1, snap = a.try_acquire(img, image=True)
        self.assertIsNotNone(t1)
        self.assertEqual((snap["image_inflight"], snap["image_limit"], snap["inflight"]), (1, 1, 1))
        t2, snap = a.try_acquire(img, image=True)                              # the image slot is full: busy
        self.assertIsNone(t2)
        self.assertEqual(snap["refused"], "busy")
        self.assertIn("image turns: 1 of 1", snap["reason"])
        self.assertEqual((a.inflight, a.reserved, a.image_inflight), (1, img, 1))   # nothing of it was taken
        t3, _ = a.try_acquire(a.request_bytes(5000, 4000))                     # a text turn still runs beside it
        self.assertIsNotNone(t3)
        t4, snap = a.try_acquire(a.request_bytes(10, 10))                      # owner active: the count holds at 2
        self.assertIsNone(t4)
        t1.release()
        t1.release()                                                           # idempotent
        self.assertEqual(a.image_inflight, 0)
        t5, _ = a.try_acquire(img, image=True)                                 # the slot is free again
        self.assertIsNotNone(t5)
        t3.release()
        t5.release()
        self.assertEqual((a.inflight, a.reserved, a.image_inflight), (0, 0, 0))

    def test_never_fits_and_unsupported(self):
        gov = local_governor.Governor(vision_cfg(), idle_reader=lambda: 3600.0, pressure_reader=lambda: "normal",
                                      clock=lambda: 0.0)
        a = local_governor.Admission(gov)
        t, snap = a.try_acquire(26 * GIB, image=True)
        self.assertEqual((t, snap["refused"]), (None, "too_large"))
        text_only = local_governor.Admission(local_governor.Governor(
            local_governor.check_config({}), idle_reader=lambda: 0.0, pressure_reader=lambda: "normal"))
        t, snap = text_only.try_acquire(10, image=True)
        self.assertEqual((t, snap["refused"], text_only.inflight), (None, "unsupported", 0))
        t, snap = local_governor.Admission(None).try_acquire(0, image=True)   # no governor: no images either
        self.assertEqual(snap["refused"], "unsupported")

    def test_concurrent_image_acquires_never_take_more_slots_than_allowed(self):
        gov = local_governor.Governor(vision_cfg(image_concurrency=2), idle_reader=lambda: 3600.0,
                                      pressure_reader=lambda: "normal", clock=lambda: 0.0)
        a = local_governor.Admission(gov)
        got, lock = [], threading.Lock()

        def one():
            t, _ = a.try_acquire(GIB, image=True)
            with lock:
                got.append(t)
        threads = [threading.Thread(target=one) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        held = [t for t in got if t is not None]
        self.assertEqual((len(held), a.image_inflight, a.inflight), (2, 2, 2))
        for t in held:
            t.release()
        self.assertEqual((a.image_inflight, a.inflight, a.reserved), (0, 0, 0))


class ImagePrefillReservationTests(unittest.TestCase):
    """#125 review round 2: the image reservation is the computed worst case of the guard's chunked media prefill."""
    V, H = 248320, 5120

    def test_the_reviewers_60k_tokens_and_a_tiny_image_reserve_their_computed_peak(self):
        cfg = vision_cfg()
        text, out = 60_000, 1000
        tokens = local_governor.request_tokens(cfg, text, out, images=1)
        self.assertEqual(tokens, 60_000 + 1024 + 1000)                       # under the 64k image context
        reserve = local_governor.request_bytes(cfg, text, out, images=1)
        # The peak, from first principles (not the formula): the KV cache and state, the whole prompt's embeddings,
        # one 256-token chunk's unfused attention scores (24 heads x 256 x P, bf16, three live copies), the kept
        # position's logits.
        peak = (tokens * 65536 + cfg["per_seq_state_bytes"] + tokens * self.H * 2 + 3 * 24 * 256 * tokens * 2
                + self.V * 2)
        self.assertGreaterEqual(reserve, peak)
        self.assertGreater(reserve, int(7.65 * GIB))                          # the old fixed margins under-reserved
        # The released forward's logits alone (every position projected, bf16) could never be reserved: no
        # reservation covers them, so the guard keeps one position instead.
        avail = local_governor.available_bytes(cfg)
        self.assertGreater(tokens * self.V * 2, avail)
        a = local_governor.Admission(local_governor.Governor(cfg, idle_reader=lambda: 3600.0,
                                                             pressure_reader=lambda: "normal", clock=lambda: 0.0))
        t, snap = a.try_acquire(reserve, image=True)                          # admitted, with that reservation
        self.assertIsNotNone(t, snap)
        self.assertEqual(a.reserved, reserve)
        t.release()

    def test_the_formula_by_term_at_the_largest_image_turn(self):
        cfg = vision_cfg()
        p, c = 65536, 256
        got = local_governor.image_prefill_bytes(cfg, p, 5)
        self.assertEqual(got["scores"], 4 * 24 * c * p * 2 + c * p)
        self.assertEqual(got["logits"], self.V * 10)
        self.assertEqual(got["sequence"], p * (3 * 2 * self.H + 2 * self.H + 64))
        self.assertEqual(got["kv_regrow"], p * 65536)
        self.assertEqual(got["chunk"], 2 * c * 2 * (3 * 17408 + max(3 * 24 * 256 + 2 * 4 * 256,
                                                                    4 * (16 * 128 + 48 * 128)) + 4 * self.H))
        # #125 round 4: the tower block at its padded width (16 heads x 80 = 1,280 for q, k, v and the fused output)
        # and the rotary embedding's float32 temporaries (3 x 4 bytes x 1,152 for each of q and k)
        self.assertEqual(got["media"], 5 * 1024 * self.H * 14
                         + 4 * 5 * 1024 * (1536 * 6 + 2 * (13 * 1152 + 4 * 1280 + 2 * 4304) + 24 * 1152))
        self.assertEqual(got["total"], sum(v for k, v in got.items() if k != "total"))
        largest = local_governor.request_bytes(cfg, 200_000, 32_000, images=5)
        self.assertEqual(largest, p * 65536 + cfg["per_seq_state_bytes"] + got["total"])
        self.assertAlmostEqual(largest / GIB, 15.91, places=2)                # docs/LOCAL-RESOURCES.md "Vision"
        avail = local_governor.available_bytes(cfg)
        self.assertLessEqual(largest, avail)                                  # fits alone ...
        text = local_governor.request_bytes(cfg, 200_000, 32_000)
        self.assertGreater(largest + text, avail)                             # ... but not beside a full 128k text turn
        self.assertLessEqual(local_governor.request_bytes(cfg, 2000, 1000, images=5) + text, avail)
        # a longer chunk is the score term growing with it
        bigger = local_governor.image_prefill_bytes(vision_cfg(prefill_chunk_tokens=1024), p, 5)
        self.assertEqual(bigger["scores"] - got["scores"], (1024 - c) * (4 * 24 * p * 2 + p))

    def test_an_image_turn_that_cannot_fit_is_refused_for_good(self):
        cfg = local_governor.check_config({"memory_budget_bytes": 26 * GIB}, {"vision": True})   # 11 GiB to share
        reserve = local_governor.request_bytes(cfg, 60_000, 1000, images=1)
        a = local_governor.Admission(local_governor.Governor(cfg, idle_reader=lambda: 3600.0,
                                                             pressure_reader=lambda: "normal", clock=lambda: 0.0))
        t, snap = a.try_acquire(reserve, image=True)
        self.assertEqual((t, snap["refused"]), (None, "too_large"))           # the gate's non-retryable 413
        t, _ = a.try_acquire(local_governor.request_bytes(cfg, 2000, 1000, images=1), image=True)
        self.assertIsNotNone(t)                                               # a short image turn still fits
        t.release()

    def test_text_only_reservations_are_unchanged(self):
        # On a vision model every text turn reserves exactly as before #125 (and #126 leaves that route alone). On a
        # text-only model #126's text route reservation applies: never below the old one, and equal to it where the
        # old one was already at least the computed peak (a short turn).
        for cfg in (vision_cfg(), local_governor.check_config({})):
            for text, out in ((10, 10), (9000, 4000), (200_000, 32_000)):
                with self.subTest(text=text, vision=bool(cfg["vision"])):
                    tokens = min(cfg["context_tokens"], text + out)
                    old = tokens * 65536 + cfg["per_seq_state_bytes"] + cfg["prefill_margin_bytes"]
                    got = local_governor.request_bytes(cfg, text, out)
                    if cfg["vision"]:
                        self.assertEqual(got, old)
                    else:
                        want = local_governor.text_set_bytes(cfg, [local_governor.text_load(cfg, text, out)])
                        self.assertEqual(got, want["total"])
                        self.assertEqual(got, max(old, want["computed"]))
                        if text == 10:
                            self.assertEqual(got, old)

    def test_the_guard_checks_the_dimensions_the_formula_uses(self):
        from tests.test_vllm_mlx_guard import load_guard
        guard = load_guard()
        self.assertEqual(guard.DIMS_KEYS, (*local_governor.VISION_DIM_KEYS, "act_bytes"))
        geo = guard.VISION_GEOMETRY
        self.assertEqual(local_governor.IMAGE_PATCH_VALUES, 3 * geo["temporal_patch_size"] * geo["patch_size"] ** 2)
        self.assertEqual(local_governor.PATCHES_PER_IMAGE_TOKEN, geo["merge_size"] ** 2)
        v = local_governor.VISION_DEFAULTS
        # the KV cache per token the governor reserves is these dimensions' (16 full-attention layers of 64)
        self.assertEqual(16 * 2 * v["num_key_value_heads"] * v["head_dim"] * v["act_bytes"],
                         local_governor.DEFAULTS["kv_bytes_per_token"])
        # one verified geometry, the same on both sides (#125 round 4)
        self.assertEqual(guard.VISION_DIMS_ALLOWED, {k: v[k] for k in local_governor.VISION_FIXED_KEYS})
        enc = guard.VISION_ENCODER_CONFIG
        self.assertEqual((enc["hidden_size"], enc["intermediate_size"], enc["num_heads"], enc["depth"]),
                         (v["vision_hidden_size"], v["vision_intermediate_size"], v["vision_num_heads"],
                          v["vision_depth"]))
        self.assertEqual(enc["out_hidden_size"], v["hidden_size"])
        self.assertEqual(guard.VISION_HEAD_DIM, local_governor.VISION_HEAD_DIM)


class EncoderAttentionReservationTests(unittest.TestCase):
    """#125 review rounds 3-4: the vision encoder's attention. One verified geometry (16 heads of 72, padded to 80 by
    ensure_fused_sdpa), every other refused; the reservation counts the padded width."""

    def test_the_round3_and_round4_repros_are_refused_at_config_load(self):
        # round 2: 8 heads (head_dim 144, unfused); round 3: 288 heads of 4, padded to 64 (4.18 GiB reserved for
        # 4.5 GiB of padded q/k/v + output at image_tokens_max 8192). Neither is the verified geometry.
        for repro in ({"vision_num_heads": 8, "image_tokens_max": 8192},
                      {"vision_num_heads": 288, "image_tokens_max": 8192, "image_context_tokens": 65536}):
            with self.subTest(repro), self.assertRaisesRegex(local_governor.GovernorConfigError,
                                                             "vision_num_heads is .* verified geometry"):
                vision_cfg(**repro)

    def test_every_single_field_geometry_deviation_is_refused(self):
        for key in local_governor.VISION_FIXED_KEYS:
            want = local_governor.VISION_DEFAULTS[key]
            for bad in (want + 1, max(1, want // 2) if want > 1 else 4):
                with self.subTest(key=key, value=bad), self.assertRaisesRegex(local_governor.GovernorConfigError,
                                                                              f"vision: {key} is"):
                    vision_cfg(**{key: bad})
        # repeating a verified value is not an override
        self.assertEqual(vision_cfg(vision_num_heads=16, act_bytes=2)["vision"]["vision_num_heads"], 16)

    def test_the_default_geometry_is_accepted_and_reserves_its_padded_peak(self):
        self.assertEqual((local_governor.VISION_HEAD_DIM, local_governor.VISION_PADDED_HEAD_DIM,
                          local_governor.VISION_PADDED_WIDTH), (72, 80, 1280))
        for itm in (1024, 8192):                                              # the default and a raised override
            cfg = vision_cfg(image_tokens_max=itm)
            n = itm * local_governor.PATCHES_PER_IMAGE_TOKEN                  # patches of one image
            terms = local_governor.image_prefill_bytes(cfg, 2 * itm, 1)
            self.assertNotIn("encoder_scores", terms)
            # first principles, one tower block per patch at bf16: q, k, v zero-padded to 80 and the fused kernel's
            # padded output (4 x 1,280), the unpadded widths (qkv, rotated q/k, norms, output copies, projection,
            # residuals: 13 x 1,152), the MLP (2 x 4,304); the rotary float32 temporaries; the pixel rows
            padded = 4 * n * 1280 * 2
            block = n * 2 * (13 * 1152 + 2 * 4304) + padded + n * 24 * 1152 + n * 1536 * 6
            self.assertGreaterEqual(terms["media"], block + itm * 5120 * 14)
            self.assertGreater(padded, 4 * n * 1152 * 2)                      # padding is counted, not the raw width
            reserve = local_governor.request_bytes(cfg, 0, 1, images=1)
            self.assertLessEqual(reserve, local_governor.available_bytes(cfg))

    def test_an_image_tokens_max_whose_padded_peak_cannot_fit_is_refused(self):
        with self.assertRaisesRegex(local_governor.GovernorConfigError, "one image turn reserves"):
            local_governor.check_config({"memory_budget_bytes": 20 * GIB},
                                        {"vision": {"image_tokens_max": 32768, "image_context_tokens": 65536}})
        # the same override fits a budget that holds its computed peak
        cfg = local_governor.check_config({"memory_budget_bytes": 64 * GIB},
                                          {"vision": {"image_tokens_max": 32768, "image_context_tokens": 65536}})
        self.assertLessEqual(local_governor.request_bytes(cfg, 0, 1, images=1), local_governor.available_bytes(cfg))

    def test_the_argv_carries_the_verified_geometry(self):
        cfg = vision_cfg(image_tokens_max=8192)
        self.assertEqual(local_backend.vision_argv({"backend": "vllm-mlx", "governor": cfg,
                                                    "model": {}})[-1].split(",")[-3:-1],
                         ["vision_num_heads=16", "vision_depth=27"])


# --------------------------------------------------------------------------------------------------- gate

class VisionGovernor(local_governor.Governor):
    def __init__(self, idle_s=3600.0, pressure="normal", vision=True, **kw):
        self.state = {"idle": idle_s, "pressure": pressure}
        super().__init__(local_governor.check_config(kw, {"vision": vision}),
                         idle_reader=lambda: self.state["idle"], pressure_reader=lambda: self.state["pressure"],
                         clock=lambda: 0.0)


class VisionGateHarness(MlxGateHarness):
    GOVERNOR = None

    def setUp(self):
        super().setUp()
        self.gate.admission = local_governor.Admission(VisionGovernor())

    def held(self, body):
        if FakeVllm.hold is None:
            FakeVllm.hold = threading.Event()
        results = []
        t = threading.Thread(target=lambda: results.append(self.call("/v1/messages", body)), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        return t, results

    def free(self):
        a = self.gate.admission
        return wait_for(lambda: (a.inflight, a.reserved, a.image_inflight) == (0, 0, 0))


class VisionGateTests(VisionGateHarness):
    def test_an_image_turn_reaches_the_backend_intact(self):
        body = body_with(image_block(), image_block(webp_vp8l(300, 200), "image/webp"))
        code, raw, _ = self.call("/v1/messages", body)
        self.assertEqual(code, 200)
        (_, path, sent), = FakeVllm.seen
        self.assertEqual(sent["messages"], body["messages"])                   # every image block, byte for byte
        self.assertTrue(self.free())

    def test_six_images_url_and_nested_images_are_refused_before_admission(self):
        url = {"type": "image", "source": {"type": "url", "url": "http://10.0.0.1/x.png"}}
        nested = {"type": "tool_result", "tool_use_id": "t", "content": [image_block()]}
        for body in (body_with(*[image_block() for _ in range(6)]), body_with(url), body_with(nested),
                     body_with(image_block(png(50000, 50000)))):
            with self.subTest(len(json.dumps(body))):
                code, raw, headers = self.call("/v1/messages", body)
                self.assertEqual(code, 400)
                self.assertEqual({k.lower(): v for k, v in headers.items()}.get("x-should-retry"), "false")
                self.assertTrue(json.loads(raw)["error"]["message"].startswith(local_gate.UNSUPPORTED_MESSAGE))
        self.assertEqual(turns(), [])
        self.assertEqual(self.gate.admission.inflight, 0)
        code, _, _ = self.call("/v1/messages", body_with(*[image_block() for _ in range(5)]))
        self.assertEqual(code, 200)

    def test_a_second_image_turn_is_a_retryable_529_while_a_text_turn_still_runs(self):
        t, _ = self.held(body_with(image_block()))
        code, raw, headers = self.call("/v1/messages", body_with(image_block()))
        self.assertEqual(code, 529)
        self.assertIn("image turns: 1 of 1", json.loads(raw)["error"]["message"])
        self.assertEqual(json.loads(raw)["cortados_gate"]["image_inflight"], 1)
        t2, _ = self.held({"model": TAG, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(self.gate.admission.inflight, 2)
        FakeVllm.hold.set()
        t.join(5)
        t2.join(5)
        self.assertTrue(self.free())

    def test_the_reservation_is_the_image_arithmetic(self):
        body = body_with(image_block(), max_tokens=3000)
        raw = json.dumps(body).encode()
        _, plan = local_governor.image_policy(body, self.gate.admission.vision)
        cfg = self.gate.admission.governor.cfg
        want = local_governor.request_bytes(cfg, local_governor.input_tokens(raw, 131072, plan.data_chars), 3000, 1)
        t, _ = self.held(body)
        self.assertEqual(self.gate.admission.reserved, want)
        FakeVllm.hold.set()
        t.join(5)
        self.assertTrue(self.free())

    def test_released_when_the_client_disconnects_mid_image_turn(self):
        FakeVllm.hold = threading.Event()
        raw = json.dumps({**body_with(image_block()), "stream": True}).encode()
        c = socket.create_connection(("127.0.0.1", self.gate.server_port), timeout=5)
        c.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\n"
                  b"authorization: Bearer " + TOKEN.encode() + b"\r\ncontent-length: " + str(len(raw)).encode()
                  + b"\r\n\r\n" + raw)
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.assertEqual(self.gate.admission.image_inflight, 1)
        c.close()
        FakeVllm.hold.set()
        self.assertTrue(self.free())

    def test_released_after_a_backend_error_or_guard_refusal(self):
        for err in ((500, {"detail": "boom"}),
                    (413, {"detail": "cortados guard: prompt too long for this node's context (prompt 70000 ...)"})):
            FakeVllm.error = err
            code, raw, _ = self.call("/v1/messages", body_with(image_block()))
            self.assertEqual(code, err[0])
            self.assertTrue(self.free())
        FakeVllm.error = None

    def test_text_only_turns_on_the_vision_model_reserve_as_before(self):
        body = {"model": TAG, "max_tokens": 500, "messages": [{"role": "user", "content": "hello"}]}
        cfg = self.gate.admission.governor.cfg
        want = local_governor.request_bytes(cfg, local_governor.input_tokens(json.dumps(body).encode(), 131072), 500)
        t, _ = self.held(body)
        self.assertEqual((self.gate.admission.reserved, self.gate.admission.image_inflight), (want, 0))
        FakeVllm.hold.set()
        t.join(5)
        self.assertTrue(self.free())

    def test_an_image_turn_over_the_budget_is_a_non_retryable_413(self):
        self.gate.admission = local_governor.Admission(VisionGovernor(memory_budget_bytes=24 * GIB))
        body = body_with(image_block(), text="x" * 40_000, max_tokens=20_000)
        code, raw, headers = self.call("/v1/messages", body)
        self.assertEqual(code, 413)
        self.assertEqual({k.lower(): v for k, v in headers.items()}.get("x-should-retry"), "false")
        self.assertEqual(turns(), [])
        self.assertTrue(self.free())

    def test_the_report_carries_the_image_capability(self):
        code, raw, _ = self.call("/api/tags")
        report = json.loads(raw)["cortados_gate"]
        self.assertEqual(report["vision"], {"max_images": 5, "image_tokens_max": 1024, "image_context_tokens": 65536,
                                            "image_concurrency": 1, "max_image_bytes": 5 * 2 ** 20,
                                            "max_image_pixels": 32 * 2 ** 20})
        self.assertEqual((report["image_inflight"], report["image_limit"]), (0, 1))


class TextOnlyGateStillRefusesImagesTests(MlxGateHarness):
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}

    def test_a_text_only_model_refuses_any_image(self):
        code, raw, _ = self.call("/v1/messages", body_with(image_block()))
        self.assertEqual(code, 400)
        self.assertIn("it serves a text-only model", json.loads(raw)["error"]["message"])
        self.assertEqual(turns(), [])


# --------------------------------------------------------------------------------------------------- guard (fakes)

IMG_PAD, VID_PAD, VIS_START, VIS_END = 151655, 151656, 151652, 151653
SPECIAL = {"<|image_pad|>": IMG_PAD, "<|video_pad|>": VID_PAD, "<|vision_start|>": VIS_START,
           "<|vision_end|>": VIS_END}

VISION_FAKE = {
    "mlx_vlm/__init__.py": "from .version import __version__\n",
    "mlx_vlm/version.py": '__version__ = "0.7.12"\n',
    "mlx_vlm/models/__init__.py": "",
    "mlx_vlm/models/base.py": "# base\n",
    "mlx_vlm/models/qwen3_5/__init__.py": "",
    "mlx_vlm/models/qwen3_5/vision.py": "# vision\n",
    "mlx_vlm/models/qwen3_5/language.py": textwrap.dedent('''
        VOCAB = 7

        class Output:
            def __init__(self, logits):
                self.logits = logits

        def row(prefix_sum, position):
            """The fake model's next-token logits at one position: a function of everything up to it."""
            return [(prefix_sum * (v + 3) + position * 31) % 9973 for v in range(VOCAB)]

        class LanguageModel:
            """qwen3_5's LanguageModel as far as the media prefill reaches it: writes every input position into the
            cache, projects only the trailing logits_to_keep positions (all when 0/None, base.py project_logits)."""
            def __init__(self):
                self.calls, self._rope_deltas = [], None
            def __call__(self, inputs, inputs_embeds=None, mask=None, cache=None, **kwargs):
                from mlx_vlm.utils import Arr
                ids = inputs.tolist()[0]
                keep = kwargs.get("logits_to_keep")
                pos = kwargs.get("position_ids")
                self.calls.append({"n": len(ids), "offset": cache.offset, "keep": keep,
                                   "embeds": inputs_embeds.shape if inputs_embeds is not None else None,
                                   "positions": pos.tolist() if pos is not None else None,
                                   "kwargs": sorted(k for k, v in kwargs.items() if v is not None)})
                if kwargs.get("pixel_values") is not None:
                    self._rope_deltas = None
                if kwargs.get("rope_deltas") is not None:
                    self._rope_deltas = kwargs["rope_deltas"]
                sums = []
                for t in ids:
                    cache.write(t)
                    sums.append((cache.sum, cache.offset - 1))
                kept = sums[-keep:] if keep else sums
                return Output(Arr([[row(s, p) for s, p in kept]], (1, len(kept), VOCAB)))
        '''),
    "mlx_vlm/models/qwen3_5/config.py": "# config\n",
    "mlx_vlm/models/qwen3_5/qwen3_5.py": textwrap.dedent('''
        from mlx_vlm.models.qwen3_5.language import LanguageModel

        class Features:
            """InputEmbeddingsFeatures.to_dict as base.py has it (attention_mask None overrides the request's)."""
            def __init__(self, inputs_embeds, position_ids, rope_deltas):
                self.inputs_embeds, self.position_ids, self.rope_deltas = inputs_embeds, position_ids, rope_deltas
            def to_dict(self):
                return {"inputs_embeds": self.inputs_embeds, "attention_mask_4d": None, "visual_pos_masks": None,
                        "attention_mask": None, "position_ids": self.position_ids, "rope_deltas": self.rope_deltas}

        class Attention:
            def __init__(self, heads, head_dim):
                self.num_heads, self.head_dim = heads, head_dim

        class Block:
            def __init__(self, heads, head_dim):
                self.attn = Attention(heads, head_dim)

        class PatchEmbed:
            patch_size, temporal_patch_size, in_channels = 16, 2, 3

        class VisionTower:
            """qwen3_vl/vision.py VisionModel as far as the guard reads it (TOWER: a test's overrides)."""
            def __init__(self, depth=27, heads=16, hidden=1152):
                self.blocks = [Block(heads, hidden // heads) for _ in range(depth)]
                self.patch_embed, self.spatial_merge_size = PatchEmbed(), 2

        TOWER = {}

        class Model:
            def __init__(self):
                self.language_model = LanguageModel()
                self.vision_tower = VisionTower(**TOWER)
                self.embedded = []
            def get_input_embeddings(self, input_ids, pixel_values=None, **kwargs):
                from mlx_vlm.utils import Arr
                ids = input_ids.tolist()[0]
                self.embedded.append((len(ids), getattr(pixel_values, "shape", None), sorted(kwargs)))
                pos = [[[p + 1000 * axis for p in range(len(ids))]] for axis in range(3)]
                return Features(Arr([[[t] for t in ids]], (1, len(ids), 1)), Arr(pos, (3, 1, len(ids))),
                                "rope-deltas")
            def __call__(self, input_ids, pixel_values=None, mask=None, cache=None, **kwargs):
                """qwen3_vl's Model.__call__ (qwen3_vl.py 215-236), which qwen3_5.Model inherits."""
                f = self.get_input_embeddings(input_ids, pixel_values, **kwargs)
                kwargs.update({"pixel_values": pixel_values, **f.to_dict()})
                return self.language_model(input_ids, mask=mask, cache=cache, **kwargs)

        class OtherModel:
            pass
        '''),
    "mlx_vlm/models/qwen3_vl/__init__.py": "",
    "mlx_vlm/models/qwen3_vl/qwen3_vl.py": "# qwen3_vl Model (the fake's __call__ lives on qwen3_5.Model)\n",
    "mlx_vlm/models/qwen3_vl/processing_qwen3_vl.py": textwrap.dedent('''
        from cortados_router import image_check

        SPECIAL = %r

        class Tokenizer:
            """Special tokens first (as HF splits added tokens), then one token per UTF-8 byte."""
            def __init__(self):
                self.calls = 0
            def encode(self, text, add_special_tokens=True):
                self.calls += 1
                out, i = [], 0
                while i < len(text):
                    for s, t in SPECIAL.items():
                        if text.startswith(s, i):
                            out.append(t)
                            i += len(s)
                            break
                    else:
                        out.extend(text[i].encode("utf-8"))
                        i += 1
                return out

        class Qwen3VLImageProcessor:
            def __init__(self):
                self.patch_size, self.merge_size, self.temporal_patch_size = 16, 2, 2
                self.min_pixels, self.max_pixels = 65536, 16777216
            def _resolved_size(self, h, w):
                f = self.patch_size * self.merge_size
                return image_check.smart_resize(h, w, factor=f, min_pixels=self.min_pixels,
                                                max_pixels=self.max_pixels)
            def num_image_tokens(self, h, w):
                rh, rw = self._resolved_size(h, w)
                return (rh // self.patch_size) * (rw // self.patch_size) // self.merge_size ** 2

        class Qwen3VLProcessor:
            def __init__(self):
                self.tokenizer = Tokenizer()
                self.image_processor = Qwen3VLImageProcessor()
                self.image_token_id, self.video_token_id = %d, %d
        ''' % (SPECIAL, IMG_PAD, VID_PAD)),
    "mlx_vlm/utils.py": textwrap.dedent('''
        from cortados_router import image_check

        TAMPER = None          # a test's hook: inputs -> inputs, to make prepare_inputs disagree with its bound
        CALLS = []

        class Arr:
            def __init__(self, value, shape=None):
                self.value = value
                self.shape = shape if shape is not None else (len(value),)
                self.size = len(value[0]) if value and isinstance(value[0], list) else len(value)
            @property
            def ndim(self):
                return len(self.shape)
            def tolist(self):
                return self.value
            def __getitem__(self, idx):
                """Basic indexing: slices and None (a new leading axis), as the media prefill uses it."""
                idx = idx if isinstance(idx, tuple) else (idx,)
                if idx and idx[0] is None:
                    return Arr([self.value], (1, *self.shape))[idx[1:]] if idx[1:] else Arr([self.value],
                                                                                          (1, *self.shape))
                def take(v, ix, shape):
                    if not ix:
                        return v, shape
                    r = range(*ix[0].indices(shape[0]))
                    parts = [take(v[j], ix[1:], shape[1:]) for j in r]
                    rest = parts[0][1] if parts else shape[1:]
                    return [p[0] for p in parts], (len(r), *rest)
                v, shape = take(self.value, idx, self.shape)
                return Arr(v, shape)

        def prepare_inputs(processor, images=None, audio=None, prompts=None, image_token_index=None, **kw):
            """mlx-vlm's prepare_inputs as the Qwen3-VL processor does it: each image placeholder expanded to
            grid_h x grid_w / merge^2 placeholders, pixel rows t x h x w, image_grid_thw one row per image."""
            CALLS.append(list(images or []))
            text = prompts
            grids, ip = [], processor.image_processor
            for path in images or []:
                with open(path, "rb") as f:
                    _, w, h = image_check.sniff(f.read())
                rh, rw = ip._resolved_size(h, w)
                grids.append([1, rh // ip.patch_size, rw // ip.patch_size])
            for t, gh, gw in grids:
                text = text.replace("<|image_pad|>", "<|placeholder|>" * (gh * gw // ip.merge_size ** 2), 1)
            text = text.replace("<|placeholder|>", "<|image_pad|>")
            ids = processor.tokenizer.encode(text, add_special_tokens=False)
            out = {"input_ids": Arr([ids], (1, len(ids))), "attention_mask": Arr([[1] * len(ids)], (1, len(ids)))}
            if grids:
                rows = sum(t * h * w for t, h, w in grids)
                out["pixel_values"] = Arr([], (rows, 1536))
                out["image_grid_thw"] = Arr(grids, (len(grids), 3))
            return TAMPER(out) if TAMPER else out
        '''),
    "vllm_mlx/vision_embedding_cache.py": textwrap.dedent('''
        import hashlib

        class Entry:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class VisionEmbeddingCache:
            def __init__(self, max_pixel_entries=100, max_encoding_entries=50, enabled=True):
                self.max_pixel_entries, self.enabled = max_pixel_entries, enabled
                self.entries = {}
            def _key(self, images, prompt):
                h = hashlib.sha256()
                for p in images:
                    with open(p, "rb") as f:
                        h.update(f.read())
                return h.hexdigest() + prompt
            def get_pixel_cache(self, images, prompt):
                if not self.enabled or not images:
                    return None
                return self.entries.get(self._key(images, prompt))
            def set_pixel_cache(self, images, prompt, **kw):
                if not self.enabled or not images:
                    return
                self.entries[self._key(images, prompt)] = Entry(**kw)
        '''),
    "vllm_mlx/models/__init__.py": "",
    "vllm_mlx/models/mllm.py": textwrap.dedent('''
        import tempfile
        DOWNLOADED, SAVED = [], {}

        def process_image_input(image):
            """0.6.0's: http(s) URLs are downloaded; base64 is saved to a temp file cached by hash, never removed."""
            if isinstance(image, dict):
                image = image.get("url", "")
            if image.startswith(("http://", "https://")):
                DOWNLOADED.append(image)
                return "/nonexistent/downloaded.png"
            import base64
            f = tempfile.NamedTemporaryFile(delete=False)
            f.write(base64.b64decode(image.split(",", 1)[1]))
            f.close()
            SAVED[image] = f.name
            return f.name
        '''),
    "vllm_mlx/mllm_scheduler.py": textwrap.dedent('''
        from collections import deque

        class MLLMSchedulerConfig:
            def __init__(self, specprefill_enabled=False):
                self.specprefill_enabled = specprefill_enabled

        class MLLMRequest:
            def __init__(self, request_id, prompt, images, videos, audio, max_tokens):
                self.request_id, self.prompt, self.images, self.videos, self.audio = (request_id, prompt, images,
                                                                                       videos, audio)
                self.max_tokens = max_tokens

        class MLLMScheduler:
            def __init__(self, model, processor, config=None, draft_model=None, draft_kind=None,
                         draft_block_size=None, specprefill_draft_model=None):
                self.model, self.processor = model, processor
                self.config = config or MLLMSchedulerConfig()
                self.waiting, self.requests, self.output_queues = deque(), {}, {}

            def add_request(self, prompt, images=None, videos=None, audio=None, max_tokens=256, temperature=0.7,
                            top_p=0.9, request_id=None, **kwargs):
                request_id = request_id or "r%d" % (len(self.requests) + 1)
                request = MLLMRequest(request_id, prompt, images, videos, audio, max_tokens)
                tok = self.processor.tokenizer
                request.num_prompt_tokens = len(tok.encode(prompt))       # text tokens only (0.6.0 line 486)
                self.requests[request_id] = request
                self.waiting.append(request)
                return request_id

            async def add_request_async(self, prompt, images=None, videos=None, audio=None, max_tokens=256,
                                        temperature=0.7, top_p=0.9, **kwargs):
                request_id = self.add_request(prompt=prompt, images=images, videos=videos, audio=audio,
                                              max_tokens=max_tokens, temperature=temperature, top_p=top_p, **kwargs)
                self.output_queues[request_id] = []
                return request_id
        '''),
    "vllm_mlx/mllm_batch_generator.py": textwrap.dedent('''
        from vllm_mlx.vision_embedding_cache import VisionEmbeddingCache

        EVALS = []

        class PrefillAbortedError(Exception):
            pass

        class _Mx:
            cleared = 0
            def clear_cache(self):
                _Mx.cleared += 1

        mx = _Mx()

        def _eval_prompt_cache(cache):
            EVALS.append(cache.offset)

        class PromptCache:
            """Every position the language model wrote (the KV cache and recurrent state, as one list)."""
            def __init__(self):
                self.tokens, self.sum = [], 0
            @property
            def offset(self):
                return len(self.tokens)
            def write(self, t):
                self.tokens.append(t)
                self.sum += t

        class MLLMBatchRequest:
            def __init__(self, request_id, prompt, images=None, videos=None, audio=None, max_tokens=256):
                self.request_id, self.prompt, self.images, self.videos, self.audio = (request_id, prompt, images,
                                                                                       videos, audio)
                self.max_tokens = max_tokens
                self.input_ids = self.pixel_values = self.attention_mask = self.image_grid_thw = None
                self.extra_kwargs = {}

        class MLLMBatchGenerator:
            def __init__(self, model, processor, mm_processor=None, max_tokens=256, enable_vision_cache=True,
                         vision_cache_size=100, **kw):
                self.model, self.processor = model, processor
                self.language_model = getattr(model, "language_model", model)
                self.vision_cache = VisionEmbeddingCache(max_pixel_entries=vision_cache_size,
                                                         max_encoding_entries=vision_cache_size // 2,
                                                         enabled=enable_vision_cache)
                self.encoded, self.errors, self.prefilled = [], [], {}
                self._aborted_request_ids, self._prefill_progress = set(), {}

            def _run_vision_encoding(self, request, cache=None):
                """0.6.0's (1615-1662): ONE forward of the whole prompt through the VLM, every position's logits."""
                kwargs = dict(request.extra_kwargs)
                if request.pixel_values is not None:
                    kwargs["pixel_values"] = request.pixel_values
                if request.attention_mask is not None:
                    kwargs["attention_mask"] = request.attention_mask
                if request.image_grid_thw is not None:
                    kwargs["image_grid_thw"] = request.image_grid_thw
                input_ids = request.input_ids
                if input_ids.ndim == 1:
                    input_ids = input_ids[None, :]
                output = self.model(input_ids, cache=cache, **kwargs)
                request.vision_encoded = True
                self._release_preprocessed_inputs(request)
                return output.logits if hasattr(output, "logits") else output

            @staticmethod
            def _release_preprocessed_inputs(request):
                request.pixel_values = None
                request.attention_mask = None
                request.image_grid_thw = None
                request.extra_kwargs.clear()

            def _preprocess_request(self, request):
                """0.6.0's control flow: the text-only early return, process_image_input per image, the pixel
                cache's hit branch (returns early) and its miss branch through prepare_inputs."""
                request.is_text_only = not bool(request.images or request.videos or request.audio)
                if request.input_ids is not None and not request.images and not request.videos and not request.audio:
                    return
                from mlx_vlm.utils import prepare_inputs
                all_images = []
                if request.images:
                    from .models.mllm import process_image_input
                    for img in request.images:
                        all_images.append(process_image_input(img))
                cached = self.vision_cache.get_pixel_cache(all_images, request.prompt)
                if cached is not None:
                    request.input_ids = cached.input_ids
                    request.pixel_values = cached.pixel_values
                    request.attention_mask = cached.attention_mask
                    request.image_grid_thw = cached.image_grid_thw
                    request.extra_kwargs = dict(cached.extra_kwargs)
                    return
                inputs = prepare_inputs(self.processor, images=all_images or None, prompts=request.prompt)
                request.input_ids = inputs.get("input_ids")
                request.pixel_values = inputs.get("pixel_values")
                request.attention_mask = inputs.get("attention_mask")
                request.extra_kwargs = {k: v for k, v in inputs.items()
                                        if k not in ["input_ids", "pixel_values", "attention_mask"]}
                request.image_grid_thw = request.extra_kwargs.pop("image_grid_thw", None)
                if all_images and request.pixel_values is not None:
                    self.vision_cache.set_pixel_cache(all_images, request.prompt, input_ids=request.input_ids,
                                                      pixel_values=request.pixel_values,
                                                      attention_mask=request.attention_mask,
                                                      image_grid_thw=request.image_grid_thw,
                                                      extra_kwargs=request.extra_kwargs)

            def _process_prompts(self, requests):
                """Per-request error handling as 0.6.0 (a failed preprocess fails only its request), then the
                vision encoder / prefill (recorded)."""
                ok = []
                for req in requests:
                    try:
                        self._preprocess_request(req)
                    except Exception as e:
                        self.errors.append((req.request_id, type(e).__name__, str(e)))
                        continue
                    ok.append(req)
                for req in ok:
                    if not req.is_text_only:                   # a cache miss: the media prefill (2233-2261)
                        cache = PromptCache()
                        try:
                            logits = self._run_vision_encoding(req, cache=cache)
                        except PrefillAbortedError:
                            self.errors.append((req.request_id, "PrefillAbortedError", ""))
                            continue
                        self.prefilled[req.request_id] = (cache, logits.tolist()[0][-1], logits.shape)
                    self.encoded.append(req.request_id)
                return ok
        '''),
    "vllm_mlx/api/anthropic_adapter.py": "# adapter\n",
    "vllm_mlx/memory_cache.py": "# memory cache\n",
    "vllm_mlx/multimodal_processor.py": "# mm processor\n",
    "vllm_mlx/patches/__init__.py": "",
    "vllm_mlx/patches/qwen3_5_mllm.py": "# patch\n",
    "PIL/__init__.py": "",
    # #125 round 3: the encoder, linear attention, caches and the rest of the import chain (pinned, contents faked)
    **{f"mlx_vlm/{rel}": f"# {rel}\n" for rel in (
        "models/qwen3_vl/vision.py", "models/qwen3_vl/config.py", "models/qwen3_vl/language.py",
        "models/qwen3_5/gated_delta.py", "models/qwen3_5/speculative_verifier.py", "models/cache.py", "turboquant.py",
        "kv_quant.py", "models/rope_utils.py", "models/activations.py", "models/fast_ops.py", "models/linear.py",
        "models/mlp.py", "models/switch_layers.py", "models/quantized_verifier.py",
        "models/exact_speculative_verify.py", "quantization/nvfp4.py", "speculative/cache_state.py",
        "speculative/ops/linear.py", "speculative/__init__.py", "speculative/ops/__init__.py",
        "quantization/__init__.py")},
    "PIL/Image.py": "MAX_IMAGE_PIXELS = 89478485\n\nclass DecompressionBombWarning(RuntimeWarning):\n    pass\n",
}
DIMS = {k: local_governor.VISION_DEFAULTS[k] for k in (*local_governor.VISION_DIM_KEYS, "act_bytes")}
DIMS_ARG = ",".join(f"{k}={v}" for k, v in DIMS.items())
VISION_ARGS = ["--vision", "--max-images", "5", "--image-tokens", "1024", "--image-context-tokens", "4096",
               "--max-image-bytes", str(5 * 2 ** 20), "--max-image-pixels", str(32 * 2 ** 20),
               "--image-prefill-tokens", "256", "--image-dims", DIMS_ARG]
TEXT_CONFIG = {k: DIMS[k] for k in DIMS if not k.startswith("vision_") and k != "act_bytes"}
QWEN_CONFIG = {"model_type": "qwen3_5", "architectures": ["Qwen3_5ForConditionalGeneration"],
               "text_config": {**TEXT_CONFIG, "dtype": "bfloat16", "num_hidden_layers": 64},
               "vision_config": {"depth": DIMS["vision_depth"], "hidden_size": DIMS["vision_hidden_size"],
                                 "intermediate_size": DIMS["vision_intermediate_size"],
                                 "num_heads": DIMS["vision_num_heads"], "patch_size": 16, "spatial_merge_size": 2,
                                 "temporal_patch_size": 2, "in_channels": 3, "out_hidden_size": 5120,
                                 "deepstack_visual_indexes": []}}


FAKE_MLX_TAG = "cp39-cp39-macosx_26_0_arm64"
FAKE_METAL_TAG = "py3-none-macosx_26_0_arm64"


def record_hash(data):
    """A RECORD line's hash of `data`: sha256= and the urlsafe base64 digest without padding."""
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def prompt_for(n_images, text="describe"):
    return ("<|im_start|>user\n" + "<|vision_start|><|image_pad|><|vision_end|>" * n_images + text
            + "<|im_end|>\n<|im_start|>assistant\n")


class VisionGuardBase(GuardBase):
    def setUp(self):
        super().setUp()
        for rel, text in VISION_FAKE.items():
            p = Path(self.dir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.vpins = {rel: self._sha("vllm_mlx", rel) for rel in self.guard.VISION_PINNED_SHA256}
        self.mpins = {rel: self._sha("mlx_vlm", rel) for rel in self.guard.MLX_VLM_PINNED_SHA256}
        self.mlx_pins, self.metal_pins = self.fake_mlx(Path(self.dir))
        self.mlx_py_pins = self.fake_mlx_python(Path(self.dir), self.guard.MLX_PY_SHA256)
        for name, value in (("VISION_PINNED_SHA256", self.vpins), ("MLX_VLM_PINNED_SHA256", self.mpins),
                            ("MLX_PINNED_RECORD", self.mlx_pins), ("MLX_METAL_PINNED_RECORD", self.metal_pins),
                            ("MLX_PY_PINNED_SHA256", {FAKE_MLX_TAG: self.mlx_py_pins}),
                            # #126 round 7: vision runs the text preflight too; mlx-lm faked here, mlx's checks real
                            ("_text_preflight", lambda: {"mlx_lm": None, "mlx": self.guard.verify_mlx_files()})):
            p = patch.object(self.guard, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.models = Path(self.dir) / "models"
        for name, cfg in (("vl", QWEN_CONFIG), ("other-vl", {**QWEN_CONFIG, "model_type": "gemma4"})):
            (self.models / name).mkdir(parents=True)
            (self.models / name / "config.json").write_text(json.dumps(cfg))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        filters = warnings.catch_warnings()
        filters.__enter__()
        self.addCleanup(filters.__exit__, None, None, None)

    @staticmethod
    def fake_mlx(root, version="0.32.4", tag=FAKE_MLX_TAG, metal_tag=FAKE_METAL_TAG):
        """Fake installed mlx and mlx-metal distributions under `root` (dist-info METADATA / WHEEL / RECORD and the
        files they name; mlx.core a plain module), and the pin tables that accept them."""
        # mlx is a namespace package, as in every released wheel (no mlx/__init__.py; #126 review round 2)
        files = {"mlx/core.py": "# the fake mlx.core extension\n",
                 "mlx/lib/libmlx.dylib": "fake libmlx\n", "mlx/lib/mlx.metallib": "fake metallib\n"}
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        h = {rel: record_hash((root / rel).read_bytes()) for rel in files}
        for dist, name, t, owned in (("mlx", "mlx", tag, ("mlx/core.py",)),
                                     ("mlx_metal", "mlx-metal", metal_tag,
                                      ("mlx/lib/libmlx.dylib", "mlx/lib/mlx.metallib"))):
            info = root / f"{dist}-{version}.dist-info"
            info.mkdir(exist_ok=True)
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
            (info / "WHEEL").write_text(f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {t}\n")
            (info / "RECORD").write_text("".join(f"{rel},{h[rel]},1\n" for rel in owned)
                                         + f"{dist}-{version}.dist-info/RECORD,,\n")
        return ({FAKE_MLX_TAG: ("mlx/core.py", h["mlx/core.py"])},
                {FAKE_METAL_TAG: {rel: h[rel] for rel in ("mlx/lib/libmlx.dylib", "mlx/lib/mlx.metallib")}})

    @staticmethod
    def fake_mlx_python(root, names):
        """mlx's pinned Python files (the names of MLX_PY_SHA256) as fakes under `root`/mlx, and their pins."""
        pins = {}
        for rel in names:
            p = root / "mlx" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# fake mlx/{rel}\n")
            pins[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        return pins

    def _sha(self, pkg, rel):
        return hashlib.sha256((Path(self.dir) / pkg / rel).read_bytes()).hexdigest()

    def _fresh(self):
        """Every fake module unimported (sys.path kept): the next import is unpatched."""
        import sys
        for name in list(sys.modules):
            if name.split(".")[0] in ("vllm_mlx", "fastapi", "mlx_vlm", "mlx", "PIL", "cortados_image_check"):
                del sys.modules[name]

    def _unimport(self):
        super()._unimport()
        import sys
        for name in list(sys.modules):
            if name.split(".")[0] in ("mlx_vlm", "mlx", "PIL", "cortados_image_check"):
                del sys.modules[name]

    def policy(self, **kw):
        args = dict(max_images=5, image_tokens=1024, image_context_tokens=4096, max_image_bytes=5 * 2 ** 20,
                    max_image_pixels=32 * 2 ** 20, prefill_tokens=64, dims=DIMS)
        args.update(kw)
        return self.guard.VisionPolicy(**args)

    def vision(self, context=8192, **kw):
        """The guard installed in vision mode on the fake classes; returns (scheduler, generator) built on the fake
        qwen3_5 model and Qwen3-VL processor."""
        import fastapi
        from vllm_mlx import mllm_batch_generator, mllm_scheduler, scheduler, vision_embedding_cache
        from vllm_mlx.api import utils
        from vllm_mlx.engine import base, batched, simple  # noqa: F401
        from vllm_mlx.models import mllm
        from mlx_vlm.models.qwen3_5.qwen3_5 import Model
        from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor
        self.pol = self.policy(**kw)
        self.guard.install(context, scheduler.Scheduler, mllm_scheduler.MLLMScheduler, utils, fastapi.HTTPException,
                           base.BaseEngine, batched.BatchedEngine, vision=self.pol)
        self.guard.install_vision(context, self.pol, mllm_scheduler_cls=mllm_scheduler.MLLMScheduler,
                                  batch_generator_cls=mllm_batch_generator.MLLMBatchGenerator,
                                  vision_cache_cls=vision_embedding_cache.VisionEmbeddingCache, mllm_models=mllm,
                                  api_utils=utils, http_exception=fastapi.HTTPException,
                                  image_check=self.guard.load_image_check(), tmp_dir=self.tmp)
        processor = Qwen3VLProcessor()
        s = mllm_scheduler.MLLMScheduler(model=Model(), processor=processor)
        g = mllm_batch_generator.MLLMBatchGenerator(Model(), processor)
        return s, g

    def batch(self, s, rid):
        from vllm_mlx.mllm_batch_generator import MLLMBatchRequest
        r = s.requests[rid]
        return MLLMBatchRequest(rid, r.prompt, r.images, r.videos, r.audio, r.max_tokens)

    def p_of(self, prompt, images):
        """The exact sequence length the fake prepare_inputs produces: what layer 1 must predict."""
        from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLImageProcessor, Tokenizer
        ip, ids = Qwen3VLImageProcessor(), Tokenizer().encode(prompt)
        ip.max_pixels = 1024 * 1024
        return len(ids) - len(images) + sum(ip.num_image_tokens(h, w) for w, h in images)


class GuardVisionAdmissionTests(VisionGuardBase):
    """Layer 1: MLLMScheduler.add_request, before anything is queued."""

    def test_the_exact_context_boundary_with_images_plus_and_minus_one(self):
        import fastapi
        s, g = self.vision(context=131072)
        prompt, raw = prompt_for(2), png(1000, 1000)
        p = self.p_of(prompt, [(1000, 1000), (1000, 1000)])
        rid = s.add_request(prompt, images=[data_url(raw)] * 2, max_tokens=4096 - p)   # exactly the image context
        self.assertIn(rid, s.requests)
        with self.assertRaises(fastapi.HTTPException) as cm:
            s.add_request(prompt, images=[data_url(raw)] * 2, max_tokens=4096 - p + 1)
        self.assertEqual(cm.exception.status_code, 413)
        self.assertTrue(cm.exception.detail.startswith(local_backend.GUARD_REFUSAL))   # the gate's never-fits 413
        self.assertIn(f"prompt {p} tokens + max_tokens {4096 - p + 1} = 4097, over the 4096-token context",
                      cm.exception.detail)
        self.assertEqual(len(s.waiting), 1)
        # layer 2 sees the same P the admission predicted
        req = self.batch(s, rid)
        g._process_prompts([req])
        self.assertEqual((g.encoded, g.errors), ([rid], []))
        self.assertEqual(req.input_ids.size, p)

    def test_a_bad_max_tokens_counts_as_the_whole_context(self):
        import fastapi
        s, _ = self.vision()
        for bad in (None, -1, "100", True, 1.5):
            with self.subTest(bad), self.assertRaises(fastapi.HTTPException) as cm:
                s.add_request(prompt_for(1), images=[data_url(png(64, 64))], max_tokens=bad)
            self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual((len(s.waiting), s.requests), (0, {}))

    def test_malformed_oversized_and_bomb_images_are_refused_and_nothing_is_queued(self):
        import fastapi
        s, _ = self.vision(max_image_bytes=4096)
        from vllm_mlx.models import mllm
        for url in (data_url(b"\x89PNG\r\n\x1a\n" + b"\0" * 20), "data:image/png;base64,@@@@",
                    data_url(png(10, 10) + b"\0" * 5000), data_url(png(50000, 50000)),
                    data_url(png(10, 10, apng=True)), data_url(jpeg(10, 10), "image/png"),
                    data_url(b"GIF89a" + b"\0" * 30, "image/gif")):
            with self.subTest(url[:40]), self.assertRaises(fastapi.HTTPException) as cm:
                s.add_request(prompt_for(1), images=[url], max_tokens=10)
            self.assertEqual(cm.exception.status_code, 400)
            self.assertTrue(cm.exception.detail.startswith(self.guard.IMAGE_REFUSAL))
        self.assertEqual((len(s.waiting), s.requests, mllm.SAVED, os.listdir(self.tmp)), (0, {}, {}, []))

    def test_six_images_are_refused_and_five_allowed(self):
        import fastapi
        s, g = self.vision(context=131072, image_context_tokens=65536)
        with self.assertRaises(fastapi.HTTPException) as cm:
            s.add_request(prompt_for(6), images=[data_url(png(640, 480))] * 6, max_tokens=100)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("more than 5 images", cm.exception.detail)
        rid = s.add_request(prompt_for(5), images=[data_url(png(640, 480))] * 5, max_tokens=100)
        g._process_prompts([self.batch(s, rid)])
        self.assertEqual((g.encoded, g.errors), ([rid], []))

    def test_url_images_are_refused_and_never_fetched(self):
        import fastapi
        s, g = self.vision()
        from vllm_mlx.models import mllm
        for url in ("https://example.com/cat.png", "http://169.254.169.254/latest/meta-data", {"url": "http://x/y"}):
            with self.subTest(str(url)), self.assertRaises(fastapi.HTTPException) as cm:
                s.add_request(prompt_for(1), images=[url], max_tokens=10)
            self.assertEqual(cm.exception.status_code, 400)
        # and if one reached preprocessing anyway (layer 1 bypassed), the guarded decoder refuses it there
        from vllm_mlx.mllm_batch_generator import MLLMBatchRequest
        req = MLLMBatchRequest("x", prompt_for(1), images=["https://example.com/cat.png"], max_tokens=10)
        g._process_prompts([req])
        self.assertEqual((g.encoded, mllm.DOWNLOADED), ([], []))
        self.assertIn("only inline base64", g.errors[0][2])
        self.assertIsNone(req.input_ids)

    def test_video_audio_and_placeholder_injection_are_refused(self):
        import fastapi
        s, _ = self.vision()
        cases = [dict(prompt=prompt_for(0), videos=["data:video/mp4;base64,AA=="]),
                 dict(prompt=prompt_for(0), audio=["data:audio/wav;base64,AA=="]),
                 dict(prompt=prompt_for(1) + "<|image_pad|>", images=[data_url(png(64, 64))]),   # an extra marker
                 dict(prompt=prompt_for(0) + "<|video_pad|>"),
                 dict(prompt=prompt_for(1)),                                                       # marker, no image
                 dict(prompt=prompt_for(0), images=[data_url(png(64, 64))])]                       # image, no marker
        for kw in cases:
            with self.subTest(kw.get("prompt")[-30:]), self.assertRaises(fastapi.HTTPException) as cm:
                s.add_request(max_tokens=10, **kw)
            self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(len(s.waiting), 0)

    def test_text_only_requests_on_the_vision_model_are_capped_as_the_text_route_does(self):
        import fastapi
        s, g = self.vision(context=1000)
        prompt = "x" * 900
        rid = s.add_request(prompt, max_tokens=100)                 # 900 + 100 = the full (text) context
        with self.assertRaises(fastapi.HTTPException) as cm:
            s.add_request(prompt, max_tokens=101, request_id="r9")
        self.assertEqual(cm.exception.status_code, 413)
        self.assertIn("over the 1000-token context", cm.exception.detail)  # not the image context
        req = self.batch(s, rid)
        g._process_prompts([req])
        self.assertEqual((g.encoded, req.input_ids.size, req.pixel_values), ([rid], 900, None))

    def test_the_text_scheduler_never_serves_in_vision_mode(self):
        import fastapi
        self.vision()
        from vllm_mlx import scheduler
        from vllm_mlx.request import Request, SamplingParams
        with self.assertRaises(fastapi.HTTPException) as cm:
            scheduler.Scheduler().add_request(Request("t", "hi", SamplingParams(1)))
        self.assertEqual(cm.exception.status_code, 501)

    def test_a_streamed_request_is_refused_before_anything_is_queued(self):
        # BatchedEngine.stream_generate's MLLM branch: add_request_async inside the response generator.
        import fastapi
        s, _ = self.vision()

        async def stream():
            rid = await s.add_request_async(prompt=prompt_for(1), images=[data_url(png(2000, 2000))],
                                            max_tokens=4000)
            yield rid

        async def consume():
            return [x async for x in stream()]
        with self.assertRaises(fastapi.HTTPException) as cm:
            asyncio.run(consume())
        self.assertEqual(cm.exception.status_code, 413)
        self.assertEqual((len(s.waiting), s.requests, s.output_queues), (0, {}, {}))

    def test_specprefill_and_drafting_are_refused(self):
        import fastapi
        s, _ = self.vision()
        for kw in ({"specprefill": True}, {"mllm_draft": True}):
            with self.subTest(kw), self.assertRaises(fastapi.HTTPException):
                s.add_request(prompt_for(0), max_tokens=10, **kw)


class GuardVisionPreparedTests(VisionGuardBase):
    """Layer 2: MLLMBatchGenerator._preprocess_request, after prepare_inputs and before the encoder or prefill."""

    def queued(self, s, n=1, max_tokens=100, raw=None):
        return s.add_request(prompt_for(n), images=[data_url(raw or png(640, 480))] * n, max_tokens=max_tokens)

    def test_a_cache_miss_that_disagrees_is_refused_released_and_fails_only_itself(self):
        from mlx_vlm import utils
        s, g = self.vision()
        good, bad = self.queued(s), self.queued(s)
        reqs = [self.batch(s, good), self.batch(s, bad)]

        def tamper(inputs):                     # the second request's processor writes a bigger grid than counted
            if len(utils.CALLS) == 2:
                inputs["image_grid_thw"] = utils.Arr([[1, 64, 64]], (1, 3))
            return inputs
        with patch.object(utils, "TAMPER", tamper):
            g._process_prompts(reqs)
        self.assertEqual(g.encoded, [good])
        (rid, kind, why), = g.errors
        self.assertEqual((rid, kind), (bad, "VisionInputRefused"))
        self.assertTrue(why.startswith(self.guard.PREPARED_REFUSAL))
        r = reqs[1]
        self.assertEqual((r.input_ids, r.pixel_values, r.image_grid_thw, r.attention_mask, r.extra_kwargs),
                         (None, None, None, None, {}))
        self.assertEqual(os.listdir(self.tmp), [])                          # temp files removed, refused or not

    def test_every_disagreement_is_caught(self):
        from mlx_vlm import utils
        s, g = self.vision()
        cases = {
            "placeholders": lambda i: {**i, "input_ids": utils.Arr([i["input_ids"].value[0] + [IMG_PAD]])},
            "pixel rows": lambda i: {**i, "pixel_values": utils.Arr([], (i["pixel_values"].shape[0] + 4, 1536))},
            "grid count": lambda i: {**i, "image_grid_thw": utils.Arr(i["image_grid_thw"].value * 2, (2, 3))},
            "not a still": lambda i: {**i, "image_grid_thw": utils.Arr([[2] + i["image_grid_thw"].value[0][1:]])},
            "video": lambda i: {**i, "video_grid_thw": utils.Arr([[1, 2, 2]])},
            "length": lambda i: {**i, "input_ids": utils.Arr([i["input_ids"].value[0] + [7] * 5000])},
            "batch": lambda i: {**i, "input_ids": utils.Arr(i["input_ids"].value * 2)},
            "over 1024": lambda i: {**i, "image_grid_thw": utils.Arr([[1, 66, 66]])},
        }
        for name, tamper in cases.items():
            with self.subTest(name), patch.object(utils, "TAMPER", tamper):
                g.errors = []
                req = self.batch(s, self.queued(s))
                g._process_prompts([req])
                self.assertEqual(len(g.errors), 1, name)
                self.assertIsNone(req.input_ids)
        self.assertEqual(g.encoded, [])

    def test_a_cache_hit_is_checked_too(self):
        from mlx_vlm import utils
        s, g = self.vision()
        self.assertFalse(g.vision_cache.enabled)                              # the guard disabled it
        self.assertEqual(g.vision_cache.max_pixel_entries, 0)
        g.vision_cache.enabled = True                                         # suppose an entry existed anyway
        raw = png(640, 480)
        first = self.batch(s, self.queued(s, raw=raw))
        g._process_prompts([first])                                           # a miss, stored
        self.assertEqual(len(g.vision_cache.entries), 1)
        calls = len(utils.CALLS)
        hit = self.batch(s, self.queued(s, raw=raw))
        g._process_prompts([hit])                                             # a consistent hit passes
        self.assertEqual((len(utils.CALLS), g.errors, len(g.encoded)), (calls, [], 2))
        entry, = g.vision_cache.entries.values()
        entry.image_grid_thw = utils.Arr([[1, 80, 80]])                       # a poisoned entry: 1,600 tokens
        bad = self.batch(s, self.queued(s, raw=raw))
        g._process_prompts([bad])
        self.assertEqual(len(utils.CALLS), calls)                             # the hit branch, not prepare_inputs
        self.assertEqual(g.errors[0][1], "VisionInputRefused")
        self.assertIsNone(bad.input_ids)

    def test_the_hit_branch_cannot_lengthen_a_request_past_its_context(self):
        from mlx_vlm import utils
        s, g = self.vision()
        g.vision_cache.enabled = True
        raw = png(640, 480)
        g._process_prompts([self.batch(s, self.queued(s, raw=raw))])
        entry, = g.vision_cache.entries.values()
        entry.input_ids = utils.Arr([entry.input_ids.value[0] + [5] * 4000])
        req = self.batch(s, self.queued(s, raw=raw, max_tokens=50))
        g._process_prompts([req])
        self.assertIn("over the 4096-token context", g.errors[0][2])

    def test_the_guarded_decoder_writes_private_temp_files_and_removes_them(self):
        s, g = self.vision()
        from vllm_mlx.models import mllm
        req = self.batch(s, self.queued(s, n=2))
        seen = []
        real = os.unlink
        with patch.object(os, "unlink", side_effect=lambda p: (seen.append(p), real(p))):
            g._process_prompts([req])
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(p.startswith(self.tmp) for p in seen))
        self.assertEqual((os.listdir(self.tmp), mllm.SAVED), ([], {}))        # never the unbounded upstream cache
        with self.assertRaises(ValueError):                                   # outside layer 2: refused
            mllm.process_image_input(data_url(png(10, 10)))

    def test_text_only_preprocessing_is_unchanged_and_checked(self):
        s, g = self.vision(context=1000)
        rid = s.add_request("hello", max_tokens=10)
        req = self.batch(s, rid)
        g._preprocess_request(req)                                            # the early (executor) path
        g._process_prompts([req])                                             # then the batch: idempotent
        self.assertEqual((g.encoded, req.input_ids.value, req.pixel_values), ([rid], [list(b"hello")], None))


class GuardMediaPrefillTests(VisionGuardBase):
    """The media prefill (MLLMBatchGenerator._run_vision_encoding): chunked, one position's logits kept."""

    def queued(self, s, text="describe", raw=None, max_tokens=100):
        return s.add_request(prompt_for(1, text), images=[data_url(raw or png(640, 480))], max_tokens=max_tokens)

    def atomic(self, g, s, rid):
        """The released forward on an identical request: the original _run_vision_encoding, unguarded."""
        from vllm_mlx.mllm_batch_generator import MLLMBatchGenerator, PromptCache
        req = self.batch(s, rid)
        g._preprocess_request(req)
        cache = PromptCache()
        logits = MLLMBatchGenerator._run_vision_encoding.__wrapped__(g, req, cache=cache)
        return cache, logits

    def test_logits_are_computed_only_for_the_kept_position_and_the_result_is_the_atomic_one(self):
        from vllm_mlx import mllm_batch_generator as mbg
        s, g = self.vision()                                                  # prefill_tokens 64
        rid = self.queued(s, text="x" * 200)
        req = self.batch(s, rid)
        lm = g.model.language_model
        evals, cleared = len(mbg.EVALS), mbg.mx.cleared
        g._process_prompts([req])
        self.assertEqual((g.errors, g.encoded), ([], [rid]))
        cache, last_row, shape = g.prefilled[rid]
        calls = list(lm.calls)
        P = cache.offset
        self.assertGreater(P, 300)                                            # several chunks
        self.assertEqual(shape, (1, 1, 7))                                    # one position's logits, never P
        self.assertTrue(all(c["keep"] == 1 for c in calls))                   # asked of the model on every chunk
        self.assertEqual([c["n"] for c in calls], [64] * (P // 64) + ([P % 64] if P % 64 else []))
        self.assertEqual([c["offset"] for c in calls], list(range(0, P, 64)))
        # the embeddings once over the whole prompt (vision tower + merge), with the request's pixels and grid
        (n, pixels, kw), = g.model.embedded
        self.assertEqual((n, pixels is not None, kw), (P, True, ["attention_mask", "image_grid_thw"]))
        # each chunk got its slice of the full prompt's mRoPE positions and the atomic call's other kwargs
        full = [[[p + 1000 * a for p in range(P)]] for a in range(3)]
        for c in calls:
            o, k = c["offset"], c["n"]
            self.assertEqual(c["positions"], [[row[0][o:o + k]] for row in full])
            self.assertEqual(c["embeds"], (1, k, 1))
            self.assertEqual(c["kwargs"], ["image_grid_thw", "logits_to_keep", "pixel_values", "position_ids",
                                          "rope_deltas"])
        self.assertEqual(lm._rope_deltas, "rope-deltas")                      # what _capture_dense_rope_delta reads
        self.assertEqual(len(mbg.EVALS) - evals, len(calls) - 1)              # the lazy graph cut between chunks
        self.assertEqual(mbg.mx.cleared - cleared, len(calls) - 1)
        self.assertEqual((req.pixel_values, req.image_grid_thw, req.extra_kwargs), (None, None, {}))
        self.assertTrue(req.vision_encoded)
        # the released forward on the same request: every position projected; the kept row and the cache are its
        lm.calls.clear()
        ref_cache, ref = self.atomic(g, s, self.queued(s, text="x" * 200))
        (atomic_call,) = lm.calls
        self.assertIsNone(atomic_call["keep"])
        self.assertEqual(ref.shape, (1, P, 7))
        self.assertEqual(ref.tolist()[0][-1], last_row)
        self.assertEqual(ref_cache.tokens, cache.tokens)                      # every position written either way

    def test_a_prompt_within_one_chunk_is_the_atomic_call_keeping_one_position(self):
        s, g = self.vision(prefill_tokens=4096)
        rid = self.queued(s)
        g._process_prompts([self.batch(s, rid)])
        (call,) = g.model.language_model.calls
        cache, _, shape = g.prefilled[rid]
        self.assertEqual((call["n"], call["offset"], call["keep"], shape), (cache.offset, 0, 1, (1, 1, 7)))

    def test_the_reviewers_60k_token_prompt_with_a_tiny_image(self):
        s, g = self.vision(context=131072, image_context_tokens=65536, prefill_tokens=256)
        rid = self.queued(s, text="a" * 60_000, raw=png(16, 16), max_tokens=1000)   # both layers admit it
        req = self.batch(s, rid)
        g._process_prompts([req])
        self.assertEqual(g.errors, [])
        cache, _, shape = g.prefilled[rid]
        calls = g.model.language_model.calls
        self.assertGreater(cache.offset, 60_000)
        self.assertEqual(shape, (1, 1, 7))
        self.assertTrue(all(c["keep"] == 1 and c["n"] <= 256 for c in calls))
        self.assertEqual(sum(c["n"] for c in calls), cache.offset)

    def test_logits_for_more_than_the_kept_position_are_refused(self):
        s, g = self.vision()
        lm = g.model.language_model
        real = type(lm).__call__

        def ignores_keep(self_, inputs, **kw):
            kw.pop("logits_to_keep", None)
            return real(self_, inputs, **kw)
        with patch.object(type(lm), "__call__", ignores_keep), self.assertRaisesRegex(RuntimeError, "not the one"):
            g._process_prompts([self.batch(s, self.queued(s))])

    def test_an_abort_stops_the_prefill_before_the_next_chunk(self):
        s, g = self.vision()
        rid = self.queued(s)
        g._aborted_request_ids.add(rid)
        g._process_prompts([self.batch(s, rid)])
        self.assertEqual((g.errors, g.model.language_model.calls, g._aborted_request_ids),
                         ([(rid, "PrefillAbortedError", "")], [], set()))

    def test_text_only_requests_do_not_take_the_media_prefill(self):
        s, g = self.vision()
        rid = s.add_request("hello", max_tokens=10)
        g._process_prompts([self.batch(s, rid)])
        self.assertEqual((g.encoded, g.prefilled, g.model.embedded, g.model.language_model.calls),
                         ([rid], {}, [], []))

    def test_a_drifted_prefill_seam_stops_startup(self):
        from vllm_mlx import mllm_batch_generator as mbg
        for name in ("_eval_prompt_cache", "PrefillAbortedError", "mx"):
            with self.subTest(name), patch.object(mbg, name, None), self.assertRaises(self.guard.GuardError):
                self.guard.media_prefill_seam(mbg.MLLMBatchGenerator)
        for attr in ("_run_vision_encoding", "_release_preprocessed_inputs"):
            with self.subTest(attr):
                cls = type("Drifted", (), {k: v for k, v in vars(mbg.MLLMBatchGenerator).items() if k != attr})
                cls.__module__ = mbg.__name__
                with self.assertRaises(self.guard.GuardError):
                    self.guard.media_prefill_seam(cls)
        with patch.object(mbg, "_eval_prompt_cache", None), self.assertRaises(self.guard.GuardError):
            self.vision()                                                     # install_vision refuses as a whole
        self.assertFalse(getattr(mbg.MLLMBatchGenerator._preprocess_request, "__cortados_guard__", False))

    def test_the_generator_refuses_a_model_that_is_not_qwen3_5_s(self):
        s, g = self.vision()
        from vllm_mlx.mllm_batch_generator import MLLMBatchGenerator
        from mlx_vlm.models.qwen3_5.qwen3_5 import Model, OtherModel
        from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor
        p = s.processor
        with self.assertRaises(self.guard.GuardError):
            MLLMBatchGenerator(OtherModel(), p)
        m = Model()
        m.language_model = object()
        with self.assertRaises(self.guard.GuardError):
            MLLMBatchGenerator(m, p)
        self.assertIsNotNone(Qwen3VLProcessor)


class GuardVisionStartupTests(VisionGuardBase):
    SERVE_VISION = ["--context-tokens", "131072", *VISION_ARGS, "--", "serve", None, "--continuous-batching"]

    def serve(self, model):
        return [model if a is None else a for a in self.SERVE_VISION]

    def test_serve_installs_the_vision_guard_and_sets_pil_s_bomb_limit(self):
        from vllm_mlx.api import utils
        model = str(self.models / "vl")
        utils.MLLM.add(model)
        rc, err = self.run_main(self.serve(model))
        self.assertEqual(rc, 0, err)
        from vllm_mlx import mllm_batch_generator, mllm_scheduler
        from PIL import Image
        self.assertTrue(mllm_scheduler.MLLMScheduler.add_request.__cortados_guard__)
        self.assertTrue(mllm_batch_generator.MLLMBatchGenerator._preprocess_request.__cortados_guard__)
        self.assertEqual(Image.MAX_IMAGE_PIXELS, 32 * 2 ** 20)
        with self.assertRaises(Image.DecompressionBombWarning):
            warnings.warn("bomb", Image.DecompressionBombWarning)
        self.assertIn("vision: 5 images of at most 1024 tokens", err)

    def test_vision_pin_drift_stops_startup(self):
        from vllm_mlx.api import utils
        model = str(self.models / "vl")
        utils.MLLM.add(model)
        for pkg, rel in [("vllm_mlx", r) for r in self.vpins] + [("mlx_vlm", r) for r in self.mpins]:
            with self.subTest(f"{pkg}/{rel}"):
                path = Path(self.dir, pkg, *rel.split("/"))
                text = path.read_text()
                path.write_text(text + "# drift\n")
                rc, err = self.run_main(self.serve(model))
                path.write_text(text)
                self.assertEqual(rc, 2)
                self.assertIn(f"{pkg}/{rel} differs", err)
        import sys
        import mlx_vlm                                    # the drifts above stopped startup before any import (P2b)
        with patch.object(sys.modules["mlx_vlm"], "__version__", "0.7.13"):
            rc, err = self.run_main(self.serve(model))
        self.assertEqual(rc, 2)
        self.assertIn("mlx-vlm 0.7.13 is not 0.7.12", err)

    ROUND3_PINS = ("models/qwen3_vl/vision.py", "models/qwen3_5/gated_delta.py", "models/cache.py",
                   "models/qwen3_vl/config.py", "models/qwen3_vl/__init__.py", "models/qwen3_vl/language.py",
                   "models/qwen3_5/speculative_verifier.py", "turboquant.py", "kv_quant.py", "models/rope_utils.py",
                   "models/activations.py", "models/fast_ops.py", "models/linear.py", "models/mlp.py",
                   "models/switch_layers.py", "models/quantized_verifier.py", "models/exact_speculative_verify.py",
                   "quantization/nvfp4.py", "speculative/cache_state.py", "speculative/ops/linear.py", "__init__.py",
                   "models/__init__.py", "speculative/__init__.py", "speculative/ops/__init__.py",
                   "quantization/__init__.py")

    def test_round3_each_new_pin_s_drift_stops_check_and_startup(self):
        """#125 review round 3 (P2): the encoder, the linear attention, the caches and the rest of the import chain
        are pinned; altering any one of them is refused (round 2 accepted these)."""
        from tests.test_vllm_mlx_guard import load_guard
        real = load_guard()
        self.assertEqual(sorted(set(self.ROUND3_PINS) - set(real.MLX_VLM_PINNED_SHA256)), [])
        from vllm_mlx.api import utils
        model = str(self.models / "vl")
        utils.MLLM.add(model)
        for rel in self.ROUND3_PINS:
            with self.subTest(rel):
                self.assertIn(rel, self.mpins)
                path = Path(self.dir, "mlx_vlm", *rel.split("/"))
                text = path.read_text()
                path.write_text(text + "# drift\n")
                try:
                    rc, err = self.run_main(["--check", model, *VISION_ARGS])
                    self.assertEqual(rc, 2)
                    self.assertIn(f"mlx_vlm/{rel} differs", err)
                    self._fresh()
                    from vllm_mlx.api import utils as fresh
                    fresh.MLLM.add(model)
                    rc, err = self.run_main(self.serve(model))
                    self.assertEqual(rc, 2)
                    self.assertIn(f"mlx_vlm/{rel} differs", err)
                finally:
                    path.write_text(text)
                    self._fresh()
                    from vllm_mlx.api import utils as fresh
                    fresh.MLLM.add(model)

    def test_round3_the_new_pins_are_the_released_wheel(self):
        from tests.test_vllm_mlx_guard import load_guard
        pins = load_guard().MLX_VLM_PINNED_SHA256
        self.assertEqual(len(pins), 35)
        for rel, want in (("models/qwen3_vl/vision.py", "9885d83cf85e037a88f8364817007094a82b5941d49e909d2d9fe11b5e03bf6c"),
                          ("models/qwen3_5/gated_delta.py",
                           "71da97ef25a013e185c4d19168b39c452b90f40d5462f4d08ff084a82e922d2f"),
                          ("models/cache.py", "d2e9900b3cd0fdf7e5410c904bbc320b8773c24aeda8f60858a0b58cf9b6b36b"),
                          ("models/qwen3_vl/config.py",
                           "8b529cfbd568c7d202140b4d5212be7fe740310c1a80374fa7dd4e59b9763017"),
                          ("models/qwen3_5/vision.py",
                           "a1db23800b564caaedcc79d6a27d76e3186b9315cb68b612d61afbfa69487600")):
            self.assertEqual(pins[rel], want, rel)
        self.assertTrue(all(len(v) == 64 and int(v, 16) >= 0 for v in pins.values()))

    def _model_dir(self, name, cfg):
        from vllm_mlx.api import utils
        d = self.models / name
        d.mkdir()
        (d / "config.json").write_text(json.dumps(cfg))
        utils.MLLM.add(str(d))
        return str(d)

    def test_round4_the_8_and_288_head_repros_are_refused_at_check_and_serve(self):
        """Round 2's 8 heads (head_dim 144, unfused) and round 3's 288 heads of 4 (padded to 64): refused whether
        config.json alone has them or the --image-dims passed match them."""
        vc = QWEN_CONFIG["vision_config"]
        for heads in (8, 288):
            with self.subTest(heads=heads):
                self._fresh()
                d = self._model_dir(f"heads{heads}", {**QWEN_CONFIG, "vision_config": {**vc, "num_heads": heads}})
                rc, err = self.run_main(["--check", d, *VISION_ARGS])
                self.assertEqual(rc, 2)
                self.assertIn(f"num_heads is {heads}", err)
                with self.assertRaises(SystemExit) as cm:
                    self.run_main(self.serve(d))
                self.assertIn("config.json", str(cm.exception))
                args = list(VISION_ARGS)
                args[-1] = DIMS_ARG.replace("vision_num_heads=16", f"vision_num_heads={heads}")
                rc, err = self.run_main(["--check", d, *args])
                self.assertEqual(rc, 2)
                self.assertIn("not the verified geometry", err)
                with self.assertRaisesRegex(self.guard.GuardError, "verified geometry"):
                    self.policy(dims={**DIMS, "vision_num_heads": heads})
        self.assertFalse(hasattr(self.guard, "vision_attention_problem"))     # no general head-count acceptance

    def test_round4_every_single_field_deviation_is_refused(self):
        vc = QWEN_CONFIG["vision_config"]
        self.assertEqual({k: vc[k] for k in self.guard.VISION_ENCODER_CONFIG}, self.guard.VISION_ENCODER_CONFIG)
        for key, want in self.guard.VISION_ENCODER_CONFIG.items():
            for name, bad in ((f"{key}+1", {**vc, key: want + 1}), (f"no-{key}", {k: v for k, v in vc.items()
                                                                                    if k != key})):
                with self.subTest(name):
                    self._fresh()
                    d = self._model_dir(name, {**QWEN_CONFIG, "vision_config": bad})
                    rc, err = self.run_main(["--check", d, *VISION_ARGS])
                    self.assertEqual(rc, 2, err)
                    self.assertIn(f"vision_config.{key}", err)
        for key, want in self.guard.VISION_DIMS_ALLOWED.items():                 # --image-dims, field by field
            with self.subTest(dims=key), self.assertRaisesRegex(self.guard.GuardError, f"{key}={want + 1} is not"):
                self.policy(dims={**DIMS, key: want + 1})
        self._fresh()
        d = self._model_dir("in-channels", {**QWEN_CONFIG, "vision_config": {**vc, "in_channels": 4}})
        self.assertEqual(self.run_main(["--check", d, *VISION_ARGS])[0], 2)
        self._fresh()
        d = self._model_dir("default", QWEN_CONFIG)
        rc, err = self.run_main(["--check", d, *VISION_ARGS])
        self.assertEqual(rc, 0, err)                                          # the verified geometry is accepted

    def test_round4_mlx_and_mlx_metal_are_pinned(self):
        self.assertEqual(self.guard.verify_mlx(), FAKE_MLX_TAG)               # the fakes, pinned: accepted
        info = Path(self.dir) / "mlx-0.32.4.dist-info"
        metal = Path(self.dir) / "mlx_metal-0.32.4.dist-info"
        core = Path(self.dir) / "mlx" / "core.py"
        lib = Path(self.dir) / "mlx" / "lib" / "mlx.metallib"
        cases = {
            "version": (info / "METADATA", lambda t: t.replace("Version: 0.32.4", "Version: 0.32.5"), "0.32.5 is not"),
            "metal-version": (metal / "METADATA", lambda t: t.replace("0.32.4", "0.31.0"), "mlx_metal 0.31.0"),
            "tag": (info / "WHEEL", lambda t: t.replace("macosx_26_0", "macosx_13_0"), "wheel tag"),
            "record": (info / "RECORD", lambda t: t.replace("sha256=", "sha256=A", 1), "RECORD's mlx/core.py"),
            "metal-record": (metal / "RECORD", lambda t: t.replace("sha256=", "sha256=A", 1), "RECORD's mlx/lib"),
            "core-bytes": (core, lambda t: t + "# patched: an unfused fallback\n", "mlx/mlx/core.py differs"),
            "metallib-bytes": (lib, lambda t: t + "x", "mlx.metallib differs"),
        }
        for name, (path, change, why) in cases.items():
            with self.subTest(name):
                text = path.read_text()
                path.write_text(change(text))
                try:
                    with self.assertRaisesRegex(self.guard.GuardError, why):
                        self.guard.verify_mlx()
                    self._fresh()
                    d = self._model_dir(f"mlx-{name}", QWEN_CONFIG)
                    rc, err = self.run_main(["--check", d, *VISION_ARGS])
                    self.assertEqual(rc, 2)
                    self.assertIn(why.split(" ")[0], err)
                finally:
                    path.write_text(text)
        # the platforms must agree, the imported mlx.core must be the pinned file, and both must be installed
        (metal / "WHEEL").write_text((metal / "WHEEL").read_text().replace("macosx_26_0", "macosx_15_0"))
        with patch.dict(self.guard.MLX_METAL_PINNED_RECORD,
                        {"py3-none-macosx_15_0_arm64": self.metal_pins[FAKE_METAL_TAG]}):
            with self.assertRaisesRegex(self.guard.GuardError, "different platforms"):
                self.guard.verify_mlx()
        (metal / "WHEEL").write_text((metal / "WHEEL").read_text().replace("macosx_15_0", "macosx_26_0"))
        other = Path(self.tmp) / "core.py"
        other.write_text(core.read_text())
        with self.assertRaisesRegex(self.guard.GuardError, "imported mlx.core"):
            self.guard.verify_mlx(core_file=str(other))

        def missing(name):
            from importlib import metadata
            raise metadata.PackageNotFoundError(name)
        with self.assertRaisesRegex(self.guard.GuardError, "mlx is not installed"):
            self.guard.verify_mlx(find=missing)

    def test_round4_the_real_mlx_pins_are_the_released_wheels(self):
        from tests.test_vllm_mlx_guard import load_guard
        real = load_guard()
        self.assertEqual(real.MLX_VERSION, "0.32.4")
        self.assertEqual(len(real.MLX_PINNED_RECORD), 27)                     # cp310-cp315 (+3.13t-3.15t) x macOS 14/15/26
        self.assertEqual(sorted(real.MLX_METAL_PINNED_RECORD),
                         [f"py3-none-macosx_{v}_0_arm64" for v in (14, 15, 26)])
        self.assertEqual(real.MLX_PINNED_RECORD["cp312-cp312-macosx_26_0_arm64"],
                         ("mlx/core.cpython-312-darwin.so", "sha256=AzYCMA_gZc7wlZEWkM5k9QuT7-JTg_5XXfEKudGBJg0"))
        self.assertEqual(real.MLX_METAL_PINNED_RECORD["py3-none-macosx_26_0_arm64"],
                         {"mlx/lib/libmlx.dylib": "sha256=2Mk5PVhOeBOTw-_8z8zYuoWtDHAmTtr5-JfcqYdTdcY",
                          "mlx/lib/mlx.metallib": "sha256=xtej-suDVo9uTLLPSoxzbuKTAlcaSDQ8uaOk0c0-OJM"})
        for tag, (rel, h) in real.MLX_PINNED_RECORD.items():
            self.assertTrue(tag.endswith("_arm64") and "macosx_" in tag and rel.startswith("mlx/core.cpython-"))
            self.assertRegex(h, r"^sha256=[A-Za-z0-9_-]{43}$")

    def test_round3_the_tower_s_geometry_and_depth_are_checked(self):
        vc = QWEN_CONFIG["vision_config"]
        cases = {"depth": {**vc, "depth": 32}, "patch14": {**vc, "patch_size": 14},
                 "no-patch-size": {k: v for k, v in vc.items() if k != "patch_size"},   # mlx-vlm's default: 14
                 "merge": {**vc, "spatial_merge_size": 4}, "temporal": {**vc, "temporal_patch_size": 1},
                 "deepstack": {**vc, "deepstack_visual_indexes": [8]}, "no-heads": {k: v for k, v in vc.items()
                                                                                     if k != "num_heads"}}
        for name, bad in cases.items():
            with self.subTest(name):
                self._fresh()
                d = self._model_dir(name, {**QWEN_CONFIG, "vision_config": bad})
                self.assertEqual(self.run_main(["--check", d, *VISION_ARGS])[0], 2)
        self._fresh()
        d = self._model_dir("window", {**QWEN_CONFIG, "vision_config": {**vc, "window_size": 64,
                                                                         "fullatt_block_indexes": [1]}})
        self.assertEqual(self.run_main(["--check", d, *VISION_ARGS])[0], 0)   # read by no block of this encoder

    def test_round3_the_loaded_tower_is_checked_when_the_scheduler_is_built(self):
        self.vision()                                                         # the default tower: accepted
        from vllm_mlx import mllm_scheduler
        from mlx_vlm.models.qwen3_5 import qwen3_5
        from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor
        for name, tower in (("heads", {"heads": 8}), ("heads288", {"heads": 288}), ("depth", {"depth": 26}),
                            ("hidden", {"hidden": 1280})):
            with self.subTest(name), patch.dict(qwen3_5.TOWER, tower):
                with self.assertRaisesRegex(self.guard.GuardError, "vision tower"):
                    mllm_scheduler.MLLMScheduler(model=qwen3_5.Model(), processor=Qwen3VLProcessor())
        m = qwen3_5.Model()
        m.vision_tower.patch_embed.patch_size = 14
        with self.assertRaisesRegex(self.guard.GuardError, "geometry"):
            mllm_scheduler.MLLMScheduler(model=m, processor=Qwen3VLProcessor())
        del m.vision_tower
        with self.assertRaisesRegex(self.guard.GuardError, "missing"):
            mllm_scheduler.MLLMScheduler(model=m, processor=Qwen3VLProcessor())

    def test_the_real_vision_pins_are_the_releases(self):
        from tests.test_vllm_mlx_guard import load_guard
        real = load_guard()
        self.assertEqual(real.MLX_VLM_VERSION, "0.7.12")
        for table in (real.VISION_PINNED_SHA256, real.MLX_VLM_PINNED_SHA256):
            self.assertTrue(table and all(len(v) == 64 and int(v, 16) >= 0 for v in table.values()))
        self.assertIn("mllm_batch_generator.py", real.VISION_PINNED_SHA256)
        self.assertIn("models/qwen3_vl/processing_qwen3_vl.py", real.MLX_VLM_PINNED_SHA256)
        # the media prefill's seam: Model.__call__ / masked_scatter, the language model, project_logits
        self.assertEqual(real.MLX_VLM_PINNED_SHA256["models/qwen3_vl/qwen3_vl.py"],
                         "12f0393421918eb1cbaa6daa447d93a405762e997f688676688e5c71d987d3ee")
        self.assertEqual(real.MLX_VLM_PINNED_SHA256["models/qwen3_5/language.py"],
                         "9cd5b798c183ac70d6c4ba0bdfabb04d1da126cd133e25089958d24d674cadac")
        self.assertEqual(real.MLX_VLM_PINNED_SHA256["models/base.py"],
                         "c75bf1f678270f33acae85a0ed7cbc6b656f51cac6664ecd8a4b23344f46a6cf")
        self.assertEqual(real.VISION_PINNED_SHA256["mllm_batch_generator.py"],
                         "592e1955cd00fd997ab52bf841b61cc5ee8c21871b0268b4545b8db0cd830e98")
        # the text route's pins are the same wheel: scheduler.py and engine/batched.py unchanged
        self.assertEqual(real.PINNED_SHA256["scheduler.py"],
                         "6fa94c0af2c4d653290a7cdfa8a1bf63c1d54a2017e2b300e7fa8cea3d24b6be")

    def test_an_mllm_that_is_not_qwen3_5_is_not_admitted(self):
        from vllm_mlx.api import utils
        utils.MLLM.add(str(self.models / "other-vl"))
        with self.assertRaises(SystemExit) as cm:
            self.run_main(self.serve(str(self.models / "other-vl")))
        self.assertIn("qwen3_5", str(cm.exception))

    def test_a_text_route_model_is_not_served_in_vision_mode(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(self.serve("/models/text"))
        self.assertIn("text route", str(cm.exception))

    def test_check_with_vision_flags(self):
        from vllm_mlx.api import utils
        utils.MLLM.update({str(self.models / "vl"), str(self.models / "other-vl")})
        self.assertEqual(self.run_main(["--check", str(self.models / "vl"), *VISION_ARGS])[0], 0)
        self.assertEqual(self.run_main(["--check", str(self.models / "other-vl"), *VISION_ARGS])[0], 2)
        self.assertEqual(self.run_main(["--check", "/models/text", *VISION_ARGS])[0], 2)
        self.assertEqual(self.run_main(["--check", str(self.models / "vl")])[0], 2)   # without --vision: refused

    def test_the_scheduler_refuses_an_unverified_model_or_processor_and_overrides_max_pixels(self):
        s, _ = self.vision()
        self.assertEqual(s.processor.image_processor.max_pixels, 1024 * 1024)  # upstream allowed 16.7 MP
        from vllm_mlx import mllm_scheduler
        from mlx_vlm.models.qwen3_5.qwen3_5 import Model, OtherModel
        from mlx_vlm.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor
        with self.assertRaises(self.guard.GuardError):
            mllm_scheduler.MLLMScheduler(model=OtherModel(), processor=Qwen3VLProcessor())
        p = Qwen3VLProcessor()
        p.image_processor.merge_size = 4
        with self.assertRaises(self.guard.GuardError):
            mllm_scheduler.MLLMScheduler(model=Model(), processor=p)
        with self.assertRaises(self.guard.GuardError):
            mllm_scheduler.MLLMScheduler(model=Model(), processor=Qwen3VLProcessor(), draft_model=object())

    def test_a_model_whose_dimensions_differ_from_the_reservation_s_is_not_served(self):
        from vllm_mlx.api import utils
        cases = {"wider": {**QWEN_CONFIG, "text_config": {**QWEN_CONFIG["text_config"], "intermediate_size": 25600}},
                 "tower": {**QWEN_CONFIG, "vision_config": {**QWEN_CONFIG["vision_config"], "hidden_size": 1280}},
                 "no-head-dim": {**QWEN_CONFIG, "text_config": {k: v for k, v in QWEN_CONFIG["text_config"].items()
                                                                if k != "head_dim"}},
                 "float32": {**QWEN_CONFIG, "text_config": {**QWEN_CONFIG["text_config"], "dtype": "float32"}},
                 "odd-dtype": {**QWEN_CONFIG, "torch_dtype": "float8"}}
        for name, cfg in cases.items():
            with self.subTest(name):
                self._fresh()                                                 # a fresh, unpatched vllm-mlx each time
                from vllm_mlx.api import utils
                d = self.models / name
                d.mkdir()
                (d / "config.json").write_text(json.dumps(cfg))
                utils.MLLM.add(str(d))
                self.assertEqual(self.run_main(["--check", str(d), *VISION_ARGS])[0], 2)
                with self.assertRaises(SystemExit) as cm:
                    self.run_main(self.serve(str(d)))
                self.assertIn("config.json", str(cm.exception))
        self._fresh()
        from vllm_mlx.api import utils
        d = self.models / "no-dtype"                                          # a config without a dtype is taken
        d.mkdir()
        cfg = {**QWEN_CONFIG, "text_config": {k: v for k, v in QWEN_CONFIG["text_config"].items() if k != "dtype"}}
        (d / "config.json").write_text(json.dumps(cfg))
        utils.MLLM.add(str(d))
        self.assertEqual(self.run_main(["--check", str(d), *VISION_ARGS])[0], 0)

    def test_bad_vision_command_lines_are_refused(self):
        for argv in (["--context-tokens", "131072", "--vision", "--", "serve", "/m", "--continuous-batching"],
                     ["--context-tokens", "131072", "--max-images", "5", "--", "serve", "/m",
                      "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-1], "x", "--", "serve", "/m",
                      "--continuous-batching"],
                     ["--context-tokens", "131072", "--vision", "--max-images", "5", "--image-tokens", "9000",
                      "--image-context-tokens", "4096", "--max-image-bytes", "1", "--max-image-pixels", "1", "--",
                      "serve", "/m", "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-2], "--", "serve", "/m", "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-1], DIMS_ARG.replace("hidden_size=5120,", ""),
                      "--", "serve", "/m", "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-1], DIMS_ARG + ",extra=1", "--", "serve", "/m",
                      "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-1], "hidden_size:5120", "--", "serve", "/m",
                      "--continuous-batching"],
                     ["--context-tokens", "131072", *VISION_ARGS[:-3], "9000", *VISION_ARGS[-2:], "--", "serve",
                      "/m", "--continuous-batching"]):
            with self.subTest(argv):
                self.assertEqual(self.run_main(argv)[0], 2)


# --------------------------------------------------------------------------------------------------- backend / lane

class VisionBackendConfigTests(unittest.TestCase):
    def config(self, vision=True, governor=True):
        raw = json.loads((ROOT / "tables" / "local-backend.example.json").read_text())
        raw["vllm_mlx"]["models"]["qwen-27b-4bit-vision"]["vision"] = vision
        raw["vllm_mlx"]["model"] = "qwen-27b-4bit-vision"
        if not governor:
            raw.pop("governor")
        return raw

    def test_a_vision_entry_launches_the_guard_in_vision_mode_and_a_text_entry_does_not(self):
        c = local_backend.check(self.config())
        argv = local_backend.serve_argv(c)
        cut = argv.index("--")
        self.assertEqual(argv[2:cut], ["--context-tokens", "131072", "--vision", "--max-images", "5",
                                       "--image-tokens", "1024", "--image-context-tokens", "65536",
                                       "--max-image-bytes", str(5 * 2 ** 20), "--max-image-pixels",
                                       str(32 * 2 ** 20), "--image-prefill-tokens", "256", "--image-dims", DIMS_ARG])
        paths = local_backend.install_paths(c)
        self.assertTrue(paths["image_check"].endswith("/bin/cortados_image_check.py"))
        self.assertEqual(Path(paths["image_check_source"]), ROOT / "cortados_router" / "image_check.py")
        self.assertEqual(paths["check_args"], argv[4:cut])
        text = local_backend.check(json.loads((ROOT / "tables" / "local-backend.example.json").read_text()))
        self.assertNotIn("--vision", local_backend.serve_argv(text))
        self.assertEqual(local_backend.install_paths(text)["check_args"], [])

    def test_vision_needs_a_governor_and_valid_settings(self):
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.check(self.config(governor=False))
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.check(self.config(vision={"max_images": -1}))

    def test_the_example_vision_entry_counts_the_tower_in_its_weights(self):
        raw = json.loads((ROOT / "tables" / "local-backend.example.json").read_text())
        text, vis = raw["vllm_mlx"]["models"]["qwen-27b-4bit"], raw["vllm_mlx"]["models"]["qwen-27b-4bit-vision"]
        self.assertGreater(vis["weights_bytes"], text["weights_bytes"])
        self.assertTrue(vis["path"].endswith("Qwen3.8-27B-heretic-ara-4bit-vision"))


class ProviderVisionEstimateTests(unittest.TestCase):
    def test_base64_is_not_counted_as_text_for_a_vision_model_and_is_for_others(self):
        body = body_with(image_block(png(3000, 2000) + b"\0" * 400_000))      # ~530k characters of base64
        text_model = {"id": TAG, "context_length": 131072, "vision": None}
        vision_model = {"id": TAG, "context_length": 131072, "vision": {"max_images": 5, "image_tokens": 1024}}
        plain = len(json.dumps(body)) // 4
        self.assertEqual(local_lane.input_tokens(body, text_model), plain)   # unchanged: request_fit's estimate
        self.assertGreater(plain, 131072)
        got = local_lane.input_tokens(body, vision_model)
        self.assertLess(got, 2000)
        self.assertEqual(got, (len(json.dumps(body)) - len(body["messages"][0]["content"][0]["source"]["data"])) // 4
                         + 1024)
        self.assertLess(local_lane.request_tokens(body, vision_model), local_lane.request_tokens(body, text_model))

    def test_the_node_config_declares_vision(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cfg = {"nodes": [{"id": "pro", "ollama": {"base_url": "http://127.0.0.1:1", "models": [
            {"id": "a:latest", "vision": True}, {"id": "b:latest", "vision": {"image_tokens": 512}},
            {"id": "c:latest"}]}}]}
        p = Path(d, "r.json")
        p.write_text(json.dumps(cfg))
        models = {m["id"]: m["vision"] for m in local_lane.nodes(str(p))[0]["models"]}
        self.assertEqual(models, {"a:latest": {"max_images": 5, "image_tokens": 1024},
                                  "b:latest": {"max_images": 5, "image_tokens": 512}, "c:latest": None})
        cfg["nodes"][0]["ollama"]["models"] = [{"id": "a:latest", "vision": {"image_tokens": 0}}]
        p.write_text(json.dumps(cfg))
        with self.assertRaises(local_lane.LocalConfigError):
            local_lane.nodes(str(p))


if __name__ == "__main__":
    unittest.main()
