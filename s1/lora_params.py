"""The LoRA parameters (rank, scale, dropout) an S1 training segment runs at, decided from what it starts from, never from
mlx_lm's defaults. Stdlib only: nightly.sh runs it under the system python3 before every `mlx_lm lora` call.

Why (2026-11-13, #74): the live adapter became the 2026-11-08 blend, LoRA rank 16 (calibrate.py stacks the candidate's and
the live adapter's rank-8 factors: 8 + 8). nightly.sh warm-starts each new run from the promoted adapter, but called
`mlx_lm lora` with no config, so it trained at mlx_lm's built-in default (rank 8, scale 20.0). mlx_lm 0.31.3 resumes with
`model.load_weights(file, strict=False)`, which checks no shapes, so it would have trained rank-16 tensors and then
written a rank-8 adapter_config.json beside them: an adapter whose config lies about its weights.

  python3 lora_params.py plan --run <run dir> --num-layers 16 --base <model> [--start <weights>] [--config <adapter_config.json>]
      Decides the segment's lora_parameters and writes <run>/lora-train.yaml (JSON, which is YAML) for `mlx_lm lora -c`.
      With --start (the warm-start copy or a resume checkpoint) the rank is the rank of that file's TENSORS, and the
      config beside it (--config) must agree: same rank, same num_layers, fine_tune_type lora, same base. Scale, dropout
      (and keys, when present) are the config's. Without --start (a run from scratch) the parameters are the configured
      ones, CORTADOS_S1_LORA_RANK (default 8) and CORTADOS_S1_LORA_SCALE (default 20.0): mlx_lm's own defaults, which every
      run before the blend used, now written down instead of implied.
      CORTADOS_S1_LORA_RANK / _SCALE set explicitly AND a start file that disagrees: refused, never silently overridden.
      The run's first segment records its parameters in <run>/lora-params.json; a later segment of the same run that
      would train at other parameters is refused (a run never changes rank half-way).
      Exit 0 planned (one log line), 3 refused (one "LORA REFUSED: ..." line; nightly.sh skips training), 2 usage.
  python3 lora_params.py rank <weights>     prints the tensor rank of a safetensors adapter (exit 3 when unreadable/mixed)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import sys
from pathlib import Path

# mlx_lm 0.31.3 CONFIG_DEFAULTS["lora_parameters"]: what every from-scratch run trained at before this file existed.
DEFAULT_RANK = 8
DEFAULT_SCALE = 20.0
DEFAULT_DROPOUT = 0.0
# Warm starts above this rank are refused. Reason: each blend promotion ADDS the two ranks (calibrate.blend_tensors stacks
# the factors), and each nightly run then trains at the promoted rank, so blend after blend the rank doubles (16, 32,
# 64, ...) with the trainer's memory and step time growing with it, beside the 8B the Studio keeps resident. 64 leaves
# room for two more blend promotions past today's 16; past that, shrinking the adapter (or training fresh at a lower
# rank) is an owner decision, not something a night should drift into. Override: CORTADOS_S1_LORA_MAX_RANK.
DEFAULT_MAX_RANK = 64
# A safetensors header is a few hundred bytes per tensor (~100 KB for an adapter); anything near this is not an adapter
# file, and reading it would only load garbage into memory.
MAX_HEADER_BYTES = 64 * 1024 * 1024
LAYER_RE = re.compile(r"(?:^|\.)layers\.(\d+)\.")
EXIT_REFUSED = 3


class Refused(Exception):
    """The segment cannot start at well-defined LoRA parameters; the message says why and what to do."""


def safetensors_shapes(path: Path) -> dict[str, list[int]]:
    """Tensor name -> shape from a safetensors file's header (8-byte little-endian length + JSON); the data is not read."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if len(head) != 8:
                raise Refused(f"{path} is not a safetensors file (shorter than its 8-byte header length)")
            (n,) = struct.unpack("<Q", head)
            if n <= 0 or n > MAX_HEADER_BYTES:
                raise Refused(f"{path} is not a safetensors adapter (header length {n})")
            raw = f.read(n)
    except OSError as e:
        raise Refused(f"cannot read {path}: {e}") from e
    if len(raw) != n:
        raise Refused(f"{path} is truncated (header of {n} bytes, {len(raw)} present)")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise Refused(f"{path} has an unreadable safetensors header ({e})") from e
    if not isinstance(header, dict):
        raise Refused(f"{path} has an unreadable safetensors header (not an object)")
    out = {}
    for k, v in header.items():
        if k == "__metadata__":
            continue
        shape = v.get("shape") if isinstance(v, dict) else None
        if not isinstance(shape, list) or not all(isinstance(x, int) for x in shape):
            raise Refused(f"{path}: tensor {k!r} has no readable shape")
        out[k] = shape
    return out


def tensor_rank(path: Path) -> tuple[int, int | None]:
    """(LoRA rank, number of distinct transformer layers) of an mlx_lm adapter file, from its tensor shapes: lora_a is
    (in, r), lora_b is (r, out). Every pair must agree; a file with no LoRA tensors or with mixed ranks is refused. The
    layer count is None when the tensor names carry no `layers.<n>.` part."""
    shapes = safetensors_shapes(path)
    ranks: dict[int, list[str]] = {}
    layers: set[str] = set()
    for k, s in shapes.items():
        if k.endswith("lora_a") and len(s) == 2:
            ranks.setdefault(s[-1], []).append(k)
        elif k.endswith("lora_b") and len(s) == 2:
            ranks.setdefault(s[0], []).append(k)
        else:
            continue
        m = LAYER_RE.search(k)
        if m:
            layers.add(m.group(1))
    if not ranks:
        raise Refused(f"{path} holds no LoRA tensors (*.lora_a / *.lora_b)")
    if len(ranks) > 1:
        detail = ", ".join(f"rank {r}: {len(ks)} tensors, e.g. {ks[0]}" for r, ks in sorted(ranks.items()))
        raise Refused(f"{path} mixes LoRA ranks ({detail})")
    return next(iter(ranks)), (len(layers) or None)


def _configured(name: str, kind) -> float | int | None:
    """A positive whole number (kind int) or positive finite number (kind float) from the environment; unset/empty is
    None. A bad value is refused, not replaced: it names the LoRA shape, and a guessed shape is the bug this file fixes."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        v = kind(raw)
    except ValueError:
        raise Refused(f"{name}={raw!r} is not a {'whole number' if kind is int else 'number'}") from None
    if not math.isfinite(v) or v <= 0:
        raise Refused(f"{name}={raw!r} must be positive and finite")
    return v


def _read_config(path: Path | None) -> dict:
    if path is None or not path.is_file():
        raise Refused(f"no adapter_config.json beside the start weights ({path}): the rank and scale they were trained at "
                      "are unknown. Copy the source adapter's adapter_config.json there, or move the run aside")
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise Refused(f"{path} is unreadable ({e})") from e
    if not isinstance(cfg, dict):
        raise Refused(f"{path} is not a JSON object")
    return cfg


def plan(run: Path, num_layers: int, base: str, start: Path | None, config: Path | None) -> tuple[dict, str]:
    """(lora_parameters for the segment, one-line explanation). Raises Refused; writes nothing."""
    want_rank = _configured("CORTADOS_S1_LORA_RANK", int)
    want_scale = _configured("CORTADOS_S1_LORA_SCALE", float)
    max_rank = _configured("CORTADOS_S1_LORA_MAX_RANK", int) or DEFAULT_MAX_RANK
    if start is None:
        params = {"rank": want_rank or DEFAULT_RANK, "dropout": DEFAULT_DROPOUT,
                  "scale": float(want_scale if want_scale is not None else DEFAULT_SCALE)}
        why = (f"fresh run: LoRA rank {params['rank']}, scale {params['scale']:g} "
               f"({'configured' if want_rank or want_scale is not None else 'defaults'})")
    else:
        rank, layers = tensor_rank(start)
        cfg = _read_config(config)
        lp = cfg.get("lora_parameters")
        if not isinstance(lp, dict) or not lp.get("rank") or lp.get("scale") is None:
            raise Refused(f"{config} has no lora_parameters.rank/scale")
        if int(lp["rank"]) != rank:
            raise Refused(f"{config} says LoRA rank {lp['rank']} but the tensors of {start} are rank {rank}: the config "
                          "does not describe these weights (a run trained before lora_params.py existed can look like this). "
                          "Move the run aside rather than training on it")
        if (cfg.get("fine_tune_type") or "lora") != "lora":
            raise Refused(f"{config} is fine_tune_type {cfg.get('fine_tune_type')!r}; nightly.sh trains lora only")
        if cfg.get("num_layers") is not None and int(cfg["num_layers"]) != num_layers:
            raise Refused(f"{config} was trained on {cfg['num_layers']} layers; nightly.sh trains {num_layers}")
        if layers is not None and layers != num_layers:
            raise Refused(f"{start} has LoRA tensors on {layers} layers; nightly.sh trains {num_layers}")
        if cfg.get("model") and base and cfg["model"] != base:
            raise Refused(f"{config} was trained on base {cfg['model']!r}; tonight's base is {base!r}")
        if want_rank is not None and want_rank != rank:
            raise Refused(f"CORTADOS_S1_LORA_RANK={want_rank} but the start weights {start} are rank {rank}: a warm start "
                          "or resume keeps its own rank. Unset it, or start the run from scratch (no warm start)")
        if want_scale is not None and float(want_scale) != float(lp["scale"]):
            raise Refused(f"CORTADOS_S1_LORA_SCALE={want_scale:g} but {config} says scale {float(lp['scale']):g}: a warm "
                          "start or resume keeps its own scale. Unset it, or start the run from scratch")
        params = {k: v for k, v in lp.items() if k in ("rank", "dropout", "scale", "keys")}
        params["rank"], params["scale"] = rank, float(lp["scale"])
        params.setdefault("dropout", DEFAULT_DROPOUT)
        why = f"LoRA rank {rank}, scale {params['scale']:g} from {start.name} (tensors) and its {config.name}"
    if params["rank"] > max_rank:
        raise Refused(f"LoRA rank {params['rank']} is above CORTADOS_S1_LORA_MAX_RANK={max_rank} (each blend promotion adds "
                      "the ranks; past the cap, shrinking the adapter or training fresh is the owner's call)")
    rec_p = run / "lora-params.json"
    if rec_p.exists():
        try:
            rec = json.loads(rec_p.read_text())
        except (OSError, ValueError) as e:
            raise Refused(f"{rec_p} is unreadable ({e}); move it aside only if the run's checkpoints agree with it") from e
        if rec != params:
            raise Refused(f"{run} started at {rec} and this segment would train at {params}: a run never changes its LoRA "
                          "parameters half-way")
    return params, why


def write_plan(run: Path, params: dict, out: Path) -> None:
    """<run>/lora-params.json (the run's record, first segment only) and the -c file for mlx_lm, both atomically."""
    for path, obj in ((run / "lora-params.json", params), (out, {"lora_parameters": params})):
        if path.name == "lora-params.json" and path.exists():
            continue
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1) + "\n")
        os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--num-layers", required=True, type=int)
    p.add_argument("--base", default="")
    p.add_argument("--start", type=Path, default=None)
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None, help="default <run>/lora-train.yaml")
    r = sub.add_parser("rank")
    r.add_argument("weights", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "rank":
            rank, layers = tensor_rank(a.weights)
            print(f"{rank} {layers if layers is not None else '?'}")
            return 0
        params, why = plan(a.run, a.num_layers, a.base, a.start, a.config)
        out = a.out or a.run / "lora-train.yaml"
        write_plan(a.run, params, out)
        print(f"lora: {why} → {out}")
        return 0
    except Refused as e:
        print(f"LORA REFUSED: {e}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
