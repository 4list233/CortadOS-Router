"""Distribution drift: does this week's traffic look like the training set? (Forest 2026-09-24: retrain at frequent intervals as the task mix changes.)
Compares the work-kind and band mix of the last 7 days of routed turns with the mix of the labelled training set. Prints a score in [0,1]
(total variation distance over work kinds, plus the same over bands, halved) and exits 0 when it exceeds the threshold → nightly.sh retrains."""
from __future__ import annotations

import collections
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from common import DATA, read_labels, read_jsonl

THRESH = float(os.environ.get("CORTADOS_S1_DRIFT", "0.25"))


def mix(counter: collections.Counter) -> dict[str, float]:
    n = sum(counter.values()) or 1
    return {k: v / n for k, v in counter.items()}


def tvd(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))


def main() -> int:
    db = Path(os.environ.get("CORTADOS_HOME") or Path.home() / ".cortados") / "router.db"
    c = sqlite3.connect(db)
    week = c.execute("SELECT answers, band FROM routes WHERE ts > ? AND answers IS NOT NULL AND answers != 'null'", (time.time() - 7 * 86400,)).fetchall()
    if len(week) < 30:
        print(f"drift: only {len(week)} routed turns this week, not enough to judge"); return 1
    wk = collections.Counter(json.loads(a).get("work_kind") for a, _ in week); wb = collections.Counter(b for _, b in week)
    reqs = {r["id"] for r in read_jsonl(DATA / "requests.jsonl") if r["split"] == "train"}
    tk = collections.Counter(l["answers"]["work_kind"] for l in read_labels() if l["id"] in reqs)
    # bands of the training labels via the same table
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cortados_router"))
    import core
    core.lane_health = lambda: {}
    tb = collections.Counter(core.resolve({"answers": l["answers"], "degraded": False, "engine": "t", "engine_ms": 0})["band"] for l in read_labels() if l["id"] in reqs)
    score = 0.5 * (tvd(mix(wk), mix(tk)) + tvd(mix(wb), mix(tb)))
    print(f"drift {score:.2f} (threshold {THRESH}) · week kinds {dict(wk.most_common(4))} vs train {dict(tk.most_common(4))} · week bands {dict(wb)} vs train {dict(tb)}")
    return 0 if score >= THRESH else 1


if __name__ == "__main__":
    sys.exit(main())
