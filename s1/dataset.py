"""Collect every real request we have into s1/data/requests.jsonl (idempotent; new rows are appended, old rows never change).
Sources: an optional private seed sheet (sources/labeling-sheet.csv: columns id, request; not published) + every turn the
shadow router has logged (~/.cortados/router.db). No request data ships with this repository."""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

from common import DATA, clean_request, read_jsonl, split_of

SHEET = Path(__file__).resolve().parent / "sources/labeling-sheet.csv"
ROUTER_DB = Path.home() / ".cortados/router.db"


def main() -> None:
    out = DATA / "requests.jsonl"
    have = {r["id"] for r in read_jsonl(out)}
    seen_text = {r["request"] for r in read_jsonl(out)}
    new = []
    if SHEET.exists():
        for r in csv.DictReader(SHEET.open()):
            new.append({"id": r["id"], "source": "1.0-sheet", "unit_kind": "interactive", "request": clean_request(r["request"])})
    if ROUTER_DB.exists():
        c = sqlite3.connect(ROUTER_DB.as_uri() + "?mode=ro", uri=True)
        for tid, kind, plat, req in c.execute("SELECT turn_id, unit_kind, platform, request FROM routes ORDER BY ts"):
            new.append({"id": "rt." + tid, "source": "hermes-" + (plat or "unknown"), "unit_kind": kind, "request": clean_request(req)})
        c.close()
    added = 0
    with out.open("a") as f:
        for r in new:
            if r["id"] in have or not r["request"] or len(r["request"]) < 2 or r["request"] in seen_text or r["request"] == "<url>":
                continue
            r["split"] = split_of(r["id"])
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            have.add(r["id"]); seen_text.add(r["request"]); added += 1
    rows = read_jsonl(out)
    print(f"requests.jsonl: +{added} new, {len(rows)} total, holdout {sum(r['split'] == 'holdout' for r in rows)}, by source "
          + json.dumps({s: sum(r['source'] == s for r in rows) for s in sorted({r['source'] for r in rows})}))


if __name__ == "__main__":
    sys.exit(main())
