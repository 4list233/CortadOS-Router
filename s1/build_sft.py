"""labels.jsonl → mlx-lm chat-format SFT files. One training example PER TEACHER SAMPLE: a request whose three samples disagree appears with
each answer, so plain cross-entropy learns the teacher's distribution (soft labels in expectation) instead of a false certainty.
Splits (fixed by id hash): holdout 15% is never written here. Of the rest, 10% = valid (early stopping + temperature calibration)."""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict

from common import DATA, core, read_jsonl, read_labels, valid_answers
from cortados_router.learning_data import bind_snapshot, training_tokenizer, training_context
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


def main() -> int:
    snapshot = bind_snapshot(DATA)
    reqs = {r["id"]: r for r in snapshot["rows"]}
    tokenizer = None
    if any(row.get("source") == "mcp-worker" for row in reqs.values()):
        try:
            tokenizer = training_tokenizer()
        except Exception:
            tokenizer = None
    by_id = defaultdict(list)
    for l in read_labels():
        if l["id"] in reqs and reqs[l["id"]]["split"] == "train" and valid_answers(l["answers"]):
            by_id[l["id"]].append(l["answers"])
    train, valid, bands = [], [], Counter()
    for rid, samples in by_id.items():
        r = reqs[rid]
        is_valid = snapshot["partitions"][rid] == "valid"
        b = Counter(band_of(a) for a in samples).most_common(1)[0][0]
        bands[b] += 1
        # rare bands are the expensive mistakes: repeat them (requests stay real; nothing synthetic is invented here)
        reps = 1 if is_valid else REPS.get(b, 1)
        d_of = lambda a: a["steps"] + a["ambiguity"] + a["blast_radius"] + a["novelty"]
        top = max(d_of(a) for a in samples)
        for a in samples:
            ex = {"messages": messages(r["request"], r["unit_kind"], a)}
            refusal = training_context(r, ex["messages"], tokenizer)
            if refusal:
                print(json.dumps(refusal, sort_keys=True))
                continue
            # Forest 2026-09-24: weight the worst (hardest-reading) answers more — the sample that read the task as hardest
            # counts UPWEIGHT_HIGH times when the samples disagree, so under-estimation is the costlier error in the loss
            k = reps * (UPWEIGHT_HIGH if (not is_valid and len(samples) > 1 and d_of(a) == top and any(d_of(b) < top for b in samples)) else 1)
            (valid if is_valid else train).extend([ex] * k)
    random.Random(11).shuffle(train)
    out = DATA / "sft"
    out.mkdir(exist_ok=True)
    for name, rows in (("train", train), ("valid", valid)):
        (out / f"{name}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n")
    print(f"labelled train-split requests {len(by_id)} · teacher-majority bands {dict(bands)} · sft train {len(train)} examples · valid {len(valid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
