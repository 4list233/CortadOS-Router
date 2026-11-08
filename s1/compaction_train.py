"""Train the compaction keep-head LoRA adapter (separate from the routing adapter) into runs/compaction-<stamp>/adapter.

Safety rails, each with its reason:
- Runs only when overnight.train_policy allows (the night window): training outside it takes the Studio GPU from the
  live routing scorer. Refused otherwise with the policy's reason, exit 75 (the gate's "did not run" code).
- The trainer runs through `python -m cortados_router.studio_capacity gate -- ...`, the same gate nightly.sh's gated()
  uses: it holds the node training lock, so the local model gate and other trainers yield.
- Never reads or writes current.json or the routing run dirs (runs/20*): the output dir is runs/compaction-<stamp>,
  which nightly.sh's runs/20*/ globs never match, and the script refuses any --out outside runs/compaction-*.
- The policy's deadline (the window end) bounds the WHOLE gated child: the wait for the gate's lock and the training.
  At the deadline the child's process group gets overnight.stop_group_drained (CONT+TERM, re-sent TERMs, drain for
  TRAIN_STOP_GRACE_S, then KILL) and is reaped; checkpointed adapter files are kept; exit 124 with the reason. Without
  it a run started near 07:00, or queued behind the gate across it, would train into the day holding the Studio lock.
- SIGTERM/SIGINT to this supervisor are latched by plain-flag handlers installed before the child is spawned; on a
  stop the group gets the same stop_group_drained + reap (further signals during cleanup are ignored), exit
  128+signum. Without them a launchd stop killed only the supervisor and orphaned the gate/trainer with the lock.
- --data / --out are resolved to absolute paths once, before validation: the child runs with cwd=s1/, so a relative
  path validated against the caller's cwd would name a different directory there.
- --dry-run prints the policy decision and the exact command, and trains nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
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
DEADLINE_RC = 124          # same code overnight.run_bounded returns for a window-end stop
POLL_S = 5.0               # deadline check interval: small against the 60 s stop grace, cheap


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
SIGNAL_POLL_S = 0.2        # how soon a latched stop signal is acted on


class StopLatch:
    """Plain-flag SIGTERM/SIGINT handlers, installed BEFORE the child is spawned. The handler only records the first
    signal: it raises nothing, so a second signal during cleanup cannot interrupt stop_group_drained half way and orphan
    the trainer with the Studio lock (the default SIGTERM action would kill this supervisor and leave the group running)."""

    def __init__(self):
        self.signum = None
        self._old = {}

    def _handler(self, signum, _frame):
        if self.signum is None:
            self.signum = signum

    def __enter__(self):
        for sig in STOP_SIGNALS:
            self._old[sig] = signal.signal(sig, self._handler)
        return self

    def __exit__(self, *exc):
        for sig, old in self._old.items():
            signal.signal(sig, old)
        return False


def supervise(cmd: list[str], deadline: datetime | None, out: Path, *, now_fn, env=None, cwd=None,
              popen=subprocess.Popen, stop=None, sleep=None, poll_s: float = POLL_S, grace: float | None = None) -> int:
    """Run the gated trainer in its own process group, bounded by `deadline` (None = unbounded, policy switch off) and
    by stop signals to this supervisor (exit 128+signum after the group is stopped and reaped)."""
    import time as _t
    import overnight
    stop = stop or overnight.stop_group_drained
    sleep = sleep or _t.sleep
    grace = overnight.TRAIN_STOP_GRACE_S if grace is None else grace
    if deadline is not None and overnight.deadline_reached(now_fn(), deadline):
        print(f"compaction_train refused: window ended at {deadline}; nothing started", file=sys.stderr)
        return REFUSED
    with StopLatch() as latch:
        if latch.signum is not None:
            return 128 + latch.signum
        child = popen(cmd, cwd=cwd, env=env, start_new_session=True)
        why = None
        try:
            while True:
                rc = child.poll()
                if rc is not None:
                    return rc
                if latch.signum is not None:
                    why = "signal"
                    break
                if deadline is not None and overnight.deadline_reached(now_fn(), deadline):
                    why = "deadline"
                    break
                sleep(min(poll_s, SIGNAL_POLL_S))
        finally:
            if child.poll() is None or why is not None:
                stop(child, grace=grace)   # also on an unexpected exception: never leave the group holding the lock
                child.wait()
        kept = sorted(p.name for p in (out / "adapter").glob("*.safetensors")) if (out / "adapter").is_dir() else []
        if why == "signal":
            print(f"compaction_train stopped: signal {latch.signum}; whole group stopped and reaped; "
                  f"checkpoints kept: {', '.join(kept) or 'none'}", file=sys.stderr)
            return 128 + latch.signum
        print(f"compaction_train stopped: night window ended at {deadline}; whole group stopped and reaped; "
              f"checkpoints kept: {', '.join(kept) or 'none'}", file=sys.stderr)
        return DEADLINE_RC


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


def main(argv=None, *, now: datetime | None = None, policy=None, now_fn=None, **supervise_kw) -> int:
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
        now_fn = now_fn or overnight._local_now
    now = now or datetime.now()
    now_fn = now_fn or (lambda: now)
    a.data = a.data.resolve()
    if a.out is not None:
        a.out = a.out.resolve()
    cfg = json.loads(a.config.read_text())
    out = a.out or S1 / "runs" / f"compaction-{now:%Y%m%d-%H%M}"
    base = os.environ.get("CORTADOS_S1_BASE", DEFAULT_BASE)
    py = os.environ.get("CORTADOS_S1_PY", str(REPO / ".venv-mlx/bin/python"))
    allowed, reason, deadline = policy(now)
    cmd = plan(a.data, out, cfg, base=base, py=py)
    bad = check_out(out)
    missing = [n for n in ("train.jsonl", "valid.jsonl") if not (a.data / n).is_file()]
    if a.dry_run:
        print(json.dumps({"policy": {"allowed": allowed, "reason": reason,
                                     "deadline": None if deadline is None else str(deadline)}, "out": str(out), "out_problem": bad,
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
    return supervise(cmd, deadline, out, now_fn=now_fn, env=env, cwd=str(S1), **supervise_kw)


if __name__ == "__main__":
    sys.exit(main())
