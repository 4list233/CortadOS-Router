"""labels.jsonl → mlx-lm chat-format SFT files. One training example PER TEACHER SAMPLE: a request whose three samples disagree appears with
each answer, so plain cross-entropy learns the teacher's distribution (soft labels in expectation) instead of a false certainty.
Splits (fixed by id hash): holdout 15% is never written here. Of the rest, 10% = valid (early stopping + temperature calibration)."""
from __future__ import annotations

import hashlib
import json
import random
import sys
from collections import Counter, defaultdict

from common import DATA, core, read_jsonl, read_labels, valid_answers
from student import messages


import os
REPS = {"R0": 1, "R1": 1, "R2": 3, "R3": 4}
UPWEIGHT_HIGH = int(os.environ.get("CORTADOS_S1_UPWEIGHT_HIGH", "2"))
for kv in (os.environ.get("CORTADOS_S1_REPS") or "").split(","):      # e.g. "R2:4,R3:6" — rare bands are the expensive mistakes
    if ":" in kv:
        REPS[kv.split(":")[0]] = int(kv.split(":")[1])


def band_of(a: dict) -> str:
    core.lane_health = lambda: {}
    return core.resolve({"answers": a, "degraded": False, "engine": "teacher", "engine_ms": 0})["band"]


# Outcome-loop training data (RT-S1-OUTCOME-002): each train-split outcome becomes one example built from the trace
# teacher's twelve labels — the same target shape as a teacher sample, but written after the task was actually run.
# unit_kind is fixed "interactive": these are coding tasks carried out interactively, and interactive is the
# conservative reading (it does not bias the scorer toward the cheaper background lane). The outcome band is the
# grading truth (evaluate.py), not a training target; the student still learns the twelve answers, band derived.
OUTCOME_UNIT_KIND = "interactive"


def outcome_examples() -> tuple[list, list, Counter, int]:
    """(train, valid, bands, labelled-outcome count) from the loop's outcomes store (empty when none yet).

    An outcome where S1 under-routed — its band below the band the task actually needed — is the expensive mistake,
    so it counts UPWEIGHT_HIGH times, the same asymmetry the teacher-sample path applies to the hardest reading."""
    from outcome import store as outcome_store                 # the loop's canonical store (CORTADOS_OUTCOME_HOME-aware)
    from outcome import train_gate
    order = core.BAND_ORDER
    train, valid, bands, n = [], [], Counter(), 0
    # The fire threshold gates INCLUSION, not just promotion: below it, outcomes are not training data yet, so a nightly
    # run trains on the teacher labels alone and no outcome-trained pass starts early (Codex review 2026-10-25).
    if not train_gate.decision()["fire"]:
        _write_provenance(0)
        return train, valid, bands, n
    for o in outcome_store.read("outcomes"):
        a = o.get("labels")
        if o.get("split") != "train" or not isinstance(a, dict) or not valid_answers(a):
            continue
        n += 1
        is_valid = int(hashlib.sha1(("v" + o["id"]).encode()).hexdigest(), 16) % 10 == 0
        b = band_of(a)
        bands[b] += 1
        reps = 1 if is_valid else REPS.get(b, 1)
        under = (o.get("s1_band") in order and o.get("outcome_band") in order
                 and order.index(o["s1_band"]) < order.index(o["outcome_band"]))
        k = reps * (UPWEIGHT_HIGH if (under and not is_valid) else 1)
        ex = {"messages": messages(o["request"], OUTCOME_UNIT_KIND, a)}
        (valid if is_valid else train).extend([ex] * k)
    _write_provenance(n)
    return train, valid, bands, n


def _provenance_path():
    from outcome import store as outcome_store
    return outcome_store.home() / "sft-provenance.json"


def _write_provenance(n: int) -> None:
    """Record how many outcomes THIS build included, so hold-first and --arm reflect what actually trained the
    candidate rather than a later live recount across the 149→150 boundary (Codex review 2026-10-25)."""
    import time
    _provenance_path().write_text(json.dumps({"outcomes_included": n, "when": time.strftime("%Y-%m-%d %H:%M")}))


def outcomes_trained() -> int:
    """Outcomes included in the current SFT build (0 if none / no build yet) — the candidate's fixed training provenance."""
    try:
        return int(json.loads(_provenance_path().read_text()).get("outcomes_included", 0))
    except (OSError, ValueError):
        return 0


def main() -> int:
    reqs = {r["id"]: r for r in read_jsonl(DATA / "requests.jsonl")}
    by_id = defaultdict(list)
    for l in read_labels():
        if l["id"] in reqs and reqs[l["id"]]["split"] == "train":
            by_id[l["id"]].append(l["answers"])
    train, valid, bands = [], [], Counter()
    for rid, samples in by_id.items():
        r = reqs[rid]
        is_valid = int(hashlib.sha1(("v" + rid).encode()).hexdigest(), 16) % 10 == 0
        b = Counter(band_of(a) for a in samples).most_common(1)[0][0]
        bands[b] += 1
        # rare bands are the expensive mistakes: repeat them (requests stay real; nothing synthetic is invented here)
        reps = 1 if is_valid else REPS.get(b, 1)
        d_of = lambda a: a["steps"] + a["ambiguity"] + a["blast_radius"] + a["novelty"]
        top = max(d_of(a) for a in samples)
        for a in samples:
            ex = {"messages": messages(r["request"], r["unit_kind"], a)}
            # Forest 2026-09-24: weight the worst (hardest-reading) answers more — the sample that read the task as hardest
            # counts UPWEIGHT_HIGH times when the samples disagree, so under-estimation is the costlier error in the loss
            k = reps * (UPWEIGHT_HIGH if (not is_valid and len(samples) > 1 and d_of(a) == top and any(d_of(b) < top for b in samples)) else 1)
            (valid if is_valid else train).extend([ex] * k)
    o_train, o_valid, o_bands, o_n = outcome_examples()         # merge the outcome loop's data with the teacher labels
    train += o_train
    valid += o_valid
    for b, c in o_bands.items():
        bands[b] += c
    random.Random(11).shuffle(train)
    out = DATA / "sft"
    out.mkdir(exist_ok=True)
    for name, rows in (("train", train), ("valid", valid)):
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
    print(f"labelled train-split requests {len(by_id)} · outcomes {o_n} · teacher+outcome bands {dict(bands)} · "
          f"sft train {len(train)} examples ({len(o_train)} from outcomes) · valid {len(valid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
