"""Read-only join of two per-row grade files (evaluate.grade_row JSONL, e.g. a candidate's holdout-rows.jsonl and the live
bar's bar-<run>-<hkey>.rows.jsonl): which rows does the CANDIDATE under-route where LIVE is hit or over?

  python3 rows_report.py <candidate rows.jsonl> <live rows.jsonl>

Prints counts grouped by source, true band, the questions whose final answers differ, and a request-length bucket."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# Length buckets in approx tokens (chars/4): short one-liners, a paragraph, a page, longer. Coarse on purpose: the
# question is whether under-routing clusters by length, not a length model.
LENGTH_BUCKETS = [(50, "<50"), (200, "50-199"), (800, "200-799")]


def load(path: Path) -> dict[str, dict]:
    rows = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip():
            try:
                r = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{n}: not JSON ({exc})")
            rows[r["id"]] = r
    return rows


def bucket(tokens: int) -> str:
    return next((name for hi, name in LENGTH_BUCKETS if tokens < hi), ">=800")


def regressions(cand: dict[str, dict], live: dict[str, dict]) -> dict:
    """Rows the candidate under-routes and live does not, with the four groupings. Rows in only one file are counted, not joined."""
    shared = sorted(set(cand) & set(live))
    picked = [i for i in shared if cand[i]["verdict"] == "under" and live[i]["verdict"] in ("hit", "over")]
    diff_q = Counter()
    for i in picked:
        a, b = cand[i]["pred_answers"], live[i]["pred_answers"]
        diff_q.update(q for q in a if a.get(q) != b.get(q))
    return {"joined": len(shared), "only_candidate": len(set(cand) - set(live)), "only_live": len(set(live) - set(cand)),
            "candidate_under_live_ok": len(picked), "ids": picked,
            "by_source": dict(Counter(str(cand[i].get("source")) for i in picked)),
            "by_true_band": dict(Counter(cand[i]["true_band"] for i in picked)),
            "by_differing_question": dict(diff_q),
            "by_length": dict(Counter(bucket(int(cand[i].get("approx_tokens") or 0)) for i in picked))}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    rep = regressions(load(Path(argv[0])), load(Path(argv[1])))
    print(json.dumps({k: v for k, v in rep.items() if k != "ids"}, indent=1, sort_keys=True))
    print("ids:", " ".join(rep["ids"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
