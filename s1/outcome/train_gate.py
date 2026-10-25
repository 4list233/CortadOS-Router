"""Outcome-training gate (RT-S1-OUTCOME-002): is there enough outcome data to warrant a weight train, and the one-time
arm that lets outcome-trained candidates auto-promote after Forest has reviewed the first.

  python3 -B -m outcome.train_gate            # report per-split counts + the fire/hold decision (reads only)
  python3 -B -m outcome.train_gate --arm      # after reviewing the first candidate's report.json: allow auto-promotion
  python3 -B -m outcome.train_gate --disarm   # require review again

The gate does NOT train or promote (one trainer, one promoter: s1/nightly.sh, which now builds SFT from outcomes and
grades on the outcome holdout). This entry answers only WHEN to run it, and holds the arm marker evaluate.py reads.
"""
from __future__ import annotations

import argparse
import json
import os

import common
from outcome import store

core = common.core
# Forest 2026-10-25: fire the first weight train at 150 labelled train-split outcomes — the same cadence as the
# historical nightly (+150 labelled requests). Lower trains on a thinner, noisier set; higher waits longer for GPU time.
MIN_TRAIN = int(os.environ.get("CORTADOS_OUTCOME_MIN_TRAIN", "150"))
ARM_MARKER = "outcome-train-armed"


def counts() -> dict[str, int]:
    """Valid labelled outcomes per split. An outcome without a well-formed 12-answer label is not training data."""
    out = {"train": 0, "holdout": 0, "unlabelled": 0}
    for o in store.read("outcomes"):
        if isinstance(o.get("labels"), dict) and common.valid_answers(o["labels"]):
            out[o.get("split", "train")] = out.get(o.get("split", "train"), 0) + 1
        else:
            out["unlabelled"] += 1
    return out


def armed() -> bool:
    return (store.home() / ARM_MARKER).exists()


def decision() -> dict:
    """Whether to run a weight-training pass now, with the reason and the hold-first arm state."""
    c = counts()
    fire = c["train"] >= MIN_TRAIN
    reason = (f"{c['train']} train-split outcomes >= {MIN_TRAIN}: ready to train"
              if fire else f"{c['train']}/{MIN_TRAIN} train-split outcomes: not yet")
    return {"counts": c, "threshold": MIN_TRAIN, "fire": fire, "armed": armed(),
            "promotion": "auto on no-regression" if armed() else "first candidate held for review", "reason": reason}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="store_true", help="allow outcome-trained candidates to auto-promote from here on")
    ap.add_argument("--disarm", action="store_true", help="require review of the next outcome-trained candidate again")
    a = ap.parse_args()
    marker = store.home() / ARM_MARKER
    if a.arm:
        d = decision()
        if not d["fire"]:                                # nothing to arm: outcomes are below the training threshold, so
            print(json.dumps({"armed": False, "refused": d["reason"]}))   # no outcome-trained candidate exists to review
            return
        marker.write_text("armed by Forest after reviewing the first outcome-trained candidate\n")
        print(json.dumps({"armed": True, "marker": str(marker)}))
        return
    if a.disarm:
        marker.unlink(missing_ok=True)
        print(json.dumps({"armed": False}))
        return
    print(json.dumps(decision(), indent=1))


if __name__ == "__main__":
    main()
