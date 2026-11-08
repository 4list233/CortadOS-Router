"""Train the compaction keep-head LoRA adapter (separate from the routing adapter) into runs/compaction-<stamp>/adapter.

Safety rails, each with its reason:
- Runs only when overnight.train_policy allows (the night window): training outside it takes the Studio GPU from the
  live routing scorer. Refused otherwise with the policy's reason, exit 75 (the gate's "did not run" code).
- The trainer runs through `python -m cortados_router.studio_capacity gate -- ...`, the same gate nightly.sh's gated()
  uses: it holds the node training lock, so the local model gate and other trainers yield.
- Never reads or writes current.json or the routing run dirs (runs/20*): the output dir is runs/compaction-<stamp>,
  which nightly.sh's runs/20*/ globs never match, and the script refuses any --out outside runs/compaction-*.
- --dry-run prints the policy decision and the exact command, and trains nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

S1 = Path(__file__).resolve().parent
REPO = S1.parent
sys.path.insert(0, str(S1))
CONFIG = S1 / "compaction_train.json"
DEFAULT_BASE = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
REFUSED = 75


def plan(data: Path, out: Path, cfg: dict, *, base: str, py: str) -> list[str]:
    return [sys.executable, "-B", "-m", "cortados_router.studio_capacity", "gate", "--",
            py, "-m", "mlx_lm", "lora", "--model", base, "--train", "--data", str(data), "--fine-tune-type", "lora",
            "--num-layers", str(cfg["num_layers"]), "--batch-size", str(cfg["batch_size"]), "--iters", str(cfg["iters"]),
            "--learning-rate", str(cfg["learning_rate"]), "--mask-prompt", "--max-seq-length", str(cfg["max_seq_length"]),
            "--grad-checkpoint", "--steps-per-eval", str(cfg["steps_per_eval"]), "--save-every", str(cfg["save_every"]),
            "--adapter-path", str(out / "adapter")]


def check_out(out: Path) -> str | None:
    """Reason to refuse an output dir, or None."""
    r = out.resolve()
    if r.parent != (S1 / "runs").resolve() or not r.name.startswith("compaction-"):
        return f"--out must be s1/runs/compaction-<stamp> (got {out}); routing runs and current.json are off limits"
    if r.exists():
        return f"{out} already exists; pick a new stamp (an existing run is never overwritten)"
    return None


def main(argv=None, *, now: datetime | None = None, policy=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, required=True, help="dir with train.jsonl / valid.jsonl from compaction_dataset.py")
    ap.add_argument("--out", type=Path, default=None, help="default s1/runs/compaction-<YYYYmmdd-HHMM>")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if policy is None:
        import overnight
        policy = overnight.train_policy
        now = now or overnight._local_now()
    now = now or datetime.now()
    cfg = json.loads(a.config.read_text())
    out = a.out or S1 / "runs" / f"compaction-{now:%Y%m%d-%H%M}"
    base = os.environ.get("CORTADOS_S1_BASE", DEFAULT_BASE)
    py = os.environ.get("CORTADOS_S1_PY", str(REPO / ".venv-mlx/bin/python"))
    allowed, reason, _deadline = policy(now)
    cmd = plan(a.data, out, cfg, base=base, py=py)
    bad = check_out(out)
    missing = [n for n in ("train.jsonl", "valid.jsonl") if not (a.data / n).is_file()]
    if a.dry_run:
        print(json.dumps({"policy": {"allowed": allowed, "reason": reason}, "out": str(out), "out_problem": bad,
                          "missing_data": missing, "command": cmd}, indent=2))
        return 0
    if os.environ.get("CORTADOS_S1_TRAIN_ALLOWED") == "0":
        allowed, reason = False, os.environ.get("CORTADOS_S1_TRAIN_SKIP_REASON", "CORTADOS_S1_TRAIN_ALLOWED=0")
    if not allowed:
        print(f"compaction_train refused: {reason}", file=sys.stderr)
        return REFUSED
    if bad or missing:
        print(f"compaction_train refused: {bad or 'missing ' + ', '.join(missing)}", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    (out / "plan.json").write_text(json.dumps({"config": cfg, "command": cmd, "data": str(a.data)}, indent=2))
    env = {**os.environ, "HF_HUB_OFFLINE": "1",
           "PYTHONPATH": str(REPO) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
    return subprocess.call(cmd, cwd=str(S1), env=env)


if __name__ == "__main__":
    sys.exit(main())
