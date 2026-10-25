"""Outcome-training readiness (RT-S1-OUTCOME-002): is there enough outcome data to include outcomes in a weight train?

  python3 -B -m outcome.train_gate            # report per-split outcome counts + the fire decision (reads only)

This only answers WHEN there is enough data for build_sft to include outcomes; it does not train or promote. The one
trainer/promoter is s1/nightly.sh: build_sft merges outcomes once the threshold is met, evaluate grades on the outcome
holdout, and a candidate auto-promotes only if it does not regress against the live model (no hold-first/arm ceremony —
the no-regression bar alone guards every promotion, so the live scorer can never get worse).
"""
from __future__ import annotations

import argparse
import json
import os

import common
from outcome import store

core = common.core
# Forest 2026-10-25: include outcomes in training at 150 labelled train-split outcomes — the same cadence as the
# historical nightly (+150 labelled requests). Lower trains on a thinner, noisier set; higher waits longer for GPU time.
MIN_TRAIN = int(os.environ.get("CORTADOS_OUTCOME_MIN_TRAIN", "150"))


def counts() -> dict[str, int]:
    """Valid labelled outcomes per split. An outcome without a well-formed 12-answer label is not training data."""
    out = {"train": 0, "holdout": 0, "unlabelled": 0}
    for o in store.read("outcomes"):
        if isinstance(o.get("labels"), dict) and common.valid_answers(o["labels"]):
            out[o.get("split", "train")] = out.get(o.get("split", "train"), 0) + 1
        else:
            out["unlabelled"] += 1
    return out


def decision() -> dict:
    """Whether a build should include outcomes now (fire), with the per-split counts and the reason."""
    c = counts()
    fire = c["train"] >= MIN_TRAIN
    reason = (f"{c['train']} train-split outcomes >= {MIN_TRAIN}: included in training"
              if fire else f"{c['train']}/{MIN_TRAIN} train-split outcomes: teacher labels only until the threshold")
    return {"counts": c, "threshold": MIN_TRAIN, "fire": fire, "reason": reason}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(decision(), indent=1))


if __name__ == "__main__":
    main()
