"""Still-image checks for the Pro's vision lane (#125), before any image is decoded: the declared type, the encoded
size, the real file type from its bytes, and the dimensions from the file's own header (so a decompression bomb is
refused from its header, never by decoding it). Stdlib only.

Two users, one source: the local gate (local_governor.image_policy, before admission) and the vllm-mlx guard
(integrations/vllm-mlx/cortados_vllm_mlx_guard.py, before vllm-mlx queues or decodes anything). The guard runs in the
MLX venv, not the router package, so scripts/install_mlx_backend.sh copies this file next to it as
`cortados_image_check.py` (local_backend.install_paths "image_check"); the guard imports that copy, or this file from
the checkout when run from source.

Accepted: PNG, JPEG and WebP STILL images, carried inline as base64 (an Anthropic `base64` source, or a `data:` URL as
vllm-mlx's adapter writes it). Refused: any other type (GIF included: telling a still GIF from an animated one means
walking every block), a declared type that is not the real one, animated PNG (acTL) and WebP (VP8X animation flag,
ANIM/ANMF chunks), multi-picture JPEG (an APP2 MPF segment: PIL opens it as several frames), a JPEG with no or more
than one frame header before its scan, a zero dimension, more decoded pixels than the bound, or an aspect ratio the
Qwen processor refuses (over 200, mlx_vlm processing_qwen3_vl._smart_resize_image).

image_tokens() is the Qwen3-VL processor's own sizing (mlx-vlm 0.7.12, processing_qwen3_vl.py _smart_resize_image and
num_image_tokens, lines 241-264 and 459-489), ported so the gate can bound an image without the processor. The guard
uses the processor's own num_image_tokens instead (the same function the preprocessing uses).
"""
from __future__ import annotations

import base64
import binascii
import math
import struct

# The image types served, by the media type a client declares, and the extension a temp file gets.
MEDIA_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp"}
EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}
# The Qwen processor refuses a longer side over 200 times the shorter one (_smart_resize_image).
MAX_ASPECT = 200
# JPEG start-of-frame markers (every SOFn but DHT C4, JPG C8 and DAC CC).
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_STANDALONE = {0x01, *range(0xD0, 0xD8)}           # TEM and RSTn carry no length


class ImageRefused(ValueError):
    """An image the vision lane does not take; the message is safe to show (no image bytes in it)."""


def b64_max_len(max_bytes: int) -> int:
    """The longest base64 text that can decode to at most `max_bytes` (padded; whitespace is refused)."""
    return 4 * -(-int(max_bytes) // 3)


def decode_base64(data: object, max_bytes: int) -> bytes:
    """Strict base64 (the standard alphabet, padding, no whitespace) of at most max_bytes decoded bytes. The length is
    checked before decoding, so an oversized image costs nothing."""
    if not isinstance(data, str) or not data:
        raise ImageRefused("image data must be a non-empty base64 string")
    if len(data) > b64_max_len(max_bytes):
        raise ImageRefused(f"image over {max_bytes} bytes")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ImageRefused("image data is not valid base64") from None
    if len(raw) > max_bytes:
        raise ImageRefused(f"image over {max_bytes} bytes")
    return raw


def _png(b: bytes) -> tuple[int, int]:
    if len(b) < 33 or b[12:16] != b"IHDR" or struct.unpack(">I", b[8:12])[0] != 13:
        raise ImageRefused("PNG without a valid IHDR header")
    w, h = struct.unpack(">II", b[16:24])
    i = 33                                           # after IHDR (8 + 4 + 4 + 13 + 4 CRC)
    while i + 8 <= len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]
        kind = b[i + 4:i + 8]
        if kind == b"acTL":
            raise ImageRefused("animated PNG (APNG) is not served")
        if kind in (b"IDAT", b"IEND"):
            break
        i += 12 + n
    else:
        raise ImageRefused("PNG without image data")
    return w, h


def _jpeg(b: bytes) -> tuple[int, int]:
    i, frames, dims = 2, 0, None
    while True:
        while i < len(b) and b[i] == 0xFF and i + 1 < len(b) and b[i + 1] == 0xFF:
            i += 1                                   # fill bytes
        if i + 2 > len(b) or b[i] != 0xFF:
            raise ImageRefused("JPEG truncated or malformed before its scan")
        m = b[i + 1]
        if m in _STANDALONE:
            i += 2
            continue
        if m == 0xD9:
            raise ImageRefused("JPEG ends before its scan")
        if i + 4 > len(b):
            raise ImageRefused("JPEG truncated or malformed before its scan")
        n = struct.unpack(">H", b[i + 2:i + 4])[0]
        if n < 2 or i + 2 + n > len(b):
            raise ImageRefused("JPEG truncated or malformed before its scan")
        seg = b[i + 4:i + 2 + n]
        if m == 0xDA:                                # start of scan: the header is complete
            break
        if m == 0xE2 and seg[:4] == b"MPF\x00":
            raise ImageRefused("multi-picture JPEG (MPO) is not served")
        if m in _SOF:
            if len(seg) < 5:
                raise ImageRefused("JPEG frame header truncated")
            frames += 1
            h, w = struct.unpack(">HH", seg[1:5])
            dims = (w, h)
        i += 2 + n
    if frames != 1:
        raise ImageRefused("JPEG must have exactly one frame header")
    return dims


def _webp(b: bytes) -> tuple[int, int]:
    if len(b) < 30:
        raise ImageRefused("WebP truncated")
    kind, n = b[12:16], struct.unpack("<I", b[16:20])[0]
    d = b[20:20 + n]
    if kind == b"VP8 ":
        if len(d) < 10 or d[3:6] != b"\x9d\x01\x2a":
            raise ImageRefused("WebP (VP8) header malformed")
        w, h = struct.unpack("<HH", d[6:10])
        return w & 0x3FFF, h & 0x3FFF
    if kind == b"VP8L":
        if len(d) < 5 or d[0] != 0x2F:
            raise ImageRefused("WebP (VP8L) header malformed")
        bits = struct.unpack("<I", d[1:5])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8X":
        if len(d) < 10:
            raise ImageRefused("WebP (VP8X) header malformed")
        if d[0] & 0x02:
            raise ImageRefused("animated WebP is not served")
        i = 20 + n + (n & 1)
        while i + 8 <= len(b):                       # no animation chunk anywhere, whatever the flag says
            if b[i:i + 4] in (b"ANIM", b"ANMF"):
                raise ImageRefused("animated WebP is not served")
            m = struct.unpack("<I", b[i + 4:i + 8])[0]
            i += 8 + m + (m & 1)
        w = int.from_bytes(d[4:7], "little") + 1
        h = int.from_bytes(d[7:10], "little") + 1
        return w, h
    raise ImageRefused("WebP chunk type not recognized")


def sniff(raw: bytes) -> tuple[str, int, int]:
    """(kind, width, height) from the file's own bytes: kind "png" | "jpeg" | "webp". ImageRefused otherwise."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        kind, (w, h) = "png", _png(raw)
    elif raw[:3] == b"\xff\xd8\xff":
        kind, (w, h) = "jpeg", _jpeg(raw)
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind, (w, h) = "webp", _webp(raw)
    else:
        raise ImageRefused("not a PNG, JPEG or WebP image")
    return kind, w, h


def check_bytes(raw: bytes, media_type: object, *, max_pixels: int) -> dict:
    """The decoded image checked: {"kind", "width", "height", "bytes"}. ImageRefused names why not."""
    want = MEDIA_TYPES.get(media_type) if isinstance(media_type, str) else None
    if want is None:
        raise ImageRefused("image media_type must be one of " + ", ".join(sorted(MEDIA_TYPES)))
    kind, w, h = sniff(raw)
    if kind != want:
        raise ImageRefused(f"image declared {media_type} but its bytes are {kind}")
    if w < 1 or h < 1:
        raise ImageRefused("image has a zero dimension")
    if w * h > max_pixels:
        raise ImageRefused(f"image {w}x{h} decodes to more than {max_pixels} pixels")
    if max(w, h) > MAX_ASPECT * min(w, h):
        raise ImageRefused(f"image aspect ratio over {MAX_ASPECT}")
    return {"kind": kind, "width": w, "height": h, "bytes": len(raw)}


def check_base64(media_type: object, data: object, *, max_bytes: int, max_pixels: int) -> tuple[dict, bytes]:
    """An Anthropic base64 image source checked: (info, decoded bytes)."""
    raw = decode_base64(data, max_bytes)
    return check_bytes(raw, media_type, max_pixels=max_pixels), raw


def check_data_url(url: object, *, max_bytes: int, max_pixels: int) -> tuple[dict, bytes]:
    """A `data:<type>;base64,<data>` URL (what vllm-mlx's adapter makes of a base64 source) checked. Anything else,
    an http(s) URL above all, is refused: nothing is fetched."""
    if not isinstance(url, str) or not url.startswith("data:"):
        raise ImageRefused("only inline base64 images are served (no URLs)")
    head, sep, data = url[5:].partition(",")
    media_type, _, enc = head.partition(";")
    if not sep or enc != "base64":
        raise ImageRefused("image data URL must be data:<type>;base64,<data>")
    return check_base64(media_type, data, max_bytes=max_bytes, max_pixels=max_pixels)


def smart_resize(height: int, width: int, *, factor: int, min_pixels: int, max_pixels: int) -> tuple[int, int]:
    """mlx-vlm 0.7.12 processing_qwen3_vl._smart_resize_image, line for line (Python's round is the same there)."""
    if max(height, width) / min(height, width) > MAX_ASPECT:
        raise ImageRefused(f"image aspect ratio over {MAX_ASPECT}")
    h_bar = round(height / factor) * factor
    w_bar = round(width / factor) * factor
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
    return h_bar, w_bar


def image_tokens(height: int, width: int, *, patch_size: int = 16, merge_size: int = 2, min_pixels: int,
                 max_pixels: int) -> int:
    """The language-model tokens a still image becomes (Qwen3VLImageProcessor.num_image_tokens): resized h x w over
    (patch_size x merge_size)^2, 1,024 pixels per token for Qwen3.5's 16 x 2."""
    h, w = smart_resize(height, width, factor=patch_size * merge_size, min_pixels=min_pixels, max_pixels=max_pixels)
    return (h // patch_size) * (w // patch_size) // merge_size ** 2
