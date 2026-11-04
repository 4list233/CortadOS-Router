"""Collect every real request we have into s1/data/requests.jsonl (idempotent; new rows are appended, old rows never change).
Sources: an optional private seed sheet (sources/labeling-sheet.csv: columns id, request; not published) + every turn the
shadow router has logged (~/.cortados/router.db, plus any db named in CORTADOS_ROUTER_DB_EXTRA — e.g. one rsynced from
the provider host, so the whole fleet's daily usage feeds training). No request data ships with this repository."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from common import DATA, LABELER_MARKER, OUTCOME_MARKER, clean_request, read_jsonl, split_of

SHEET = Path(__file__).resolve().parent / "sources/labeling-sheet.csv"
ROUTER_DB = Path.home() / ".cortados/router.db"
# Router rows that are machine traffic, not requests a person or job made: training on them would feed the pipeline its own output.
#   labeler  — s1/label.py's teacher calls go through the provider and land in routes; each user message starts with this marker
#              (common.LABELER_MARKER; the provider keeps no run-id column, so the text is the durable mark). The text is a JSON array of
#              OTHER requests already in the dataset: a duplicate, and a mis-shaped one.
#   review   — cortados_router/reviews.py's templated review hand-offs ("[cortados-review:<ticket>:<role>] Review requested; …"): one fixed
#              template that differs only by ticket, so it would add hundreds of near-identical rows.
#   outcome  — s1/outcome's judge, generate and ladder-run sessions through the provider (common.OUTCOME_MARKER heads the first user
#              message): the loop's own prompts and synthesized tasks.
MACHINE_MARKERS = {"labeler": LABELER_MARKER, "review": "[cortados-review:", "outcome": OUTCOME_MARKER}


def _router_dbs() -> list[Path]:
    """The local shadow-router db plus any synced from other machines (CORTADOS_ROUTER_DB_EXTRA, os.pathsep-separated).
    A provider running on another host logs to its OWN ~/.cortados/router.db; rsync a copy here and name it in that
    variable so daily usage from the whole fleet feeds training, not only this machine's (Forest 2026-10-26)."""
    paths, seen = [], set()
    for db in [ROUTER_DB] + [Path(x).expanduser() for x in os.environ.get("CORTADOS_ROUTER_DB_EXTRA", "").split(os.pathsep) if x.strip()]:
        try:
            rp = db.resolve()                               # absolute + canonical: a relative path survives as_uri(), and one file named two ways dedupes once
        except (OSError, RuntimeError) as exc:              # a symlink loop (RuntimeError on 3.9) or bad path: skip this source, never abort the rest
            print(f"dataset: skipping router db {db}: {exc}", file=sys.stderr)
            continue
        if rp not in seen:
            seen.add(rp); paths.append(rp)
    return paths


def _source_tag(db: Path) -> str:
    """The local db keeps unqualified ids (`rt.<turn_id>`) so existing requests.jsonl ids are unchanged; every other db is
    tagged `<stem>-<hash of its resolved path>` — the stem alone collides across machines (`/air/router.db` and
    `/studio/router.db` share a stem), so a shared turn_id would silently drop one. The path hash makes each source a
    distinct, stable namespace (Codex review 2026-10-26)."""
    if db.resolve() == ROUTER_DB.resolve():
        return ""
    return db.stem + "-" + hashlib.sha1(str(db.resolve()).encode()).hexdigest()[:8]


def machine_kind(request) -> str | None:
    """Which MACHINE_MARKERS kind a raw router request STARTS with, or None for a real request. Head only: a person quoting or
    discussing a marker mid-message is still a real request."""
    head = (request or "").lstrip()
    for kind, marker in MACHINE_MARKERS.items():
        if head.startswith(marker):
            return kind
    return None


def _rows_from_db(db: Path, excluded: Counter | None = None) -> list:
    """Requests from one router db (read-only). Raises on an unreadable/locked/malformed db; the caller isolates that so
    one bad source never discards rows already collected from the healthy ones. Machine traffic (MACHINE_MARKERS) is
    skipped and counted into `excluded` by kind."""
    tag = _source_tag(db)
    c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        rows = []
        for tid, kind, plat, req in c.execute("SELECT turn_id, unit_kind, platform, request FROM routes ORDER BY ts"):
            machine = machine_kind(req)     # separate name: `kind` is the row's unit_kind (interactive/background) and must survive
            if machine:
                if excluded is not None:
                    excluded[machine] += 1
                continue
            rid = "rt." + tid if not tag else f"rt.{tag}.{tid}"
            rows.append({"id": rid, "source": "hermes-" + (plat or "unknown"), "unit_kind": kind, "request": clean_request(req)})
        return rows
    finally:
        c.close()


def main() -> None:
    out = DATA / "requests.jsonl"
    have = {r["id"] for r in read_jsonl(out)}
    seen_text = {r["request"] for r in read_jsonl(out)}
    new = []
    excluded: Counter = Counter()
    if SHEET.exists():
        for r in csv.DictReader(SHEET.open()):
            new.append({"id": r["id"], "source": "1.0-sheet", "unit_kind": "interactive", "request": clean_request(r["request"])})
    for db in _router_dbs():
        try:
            if not db.exists():                             # a synced copy that has not arrived yet is simply skipped
                continue
            new.extend(_rows_from_db(db, excluded))
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            print(f"dataset: skipping router db {db}: {exc}", file=sys.stderr)   # one bad/unreadable source never loses the others
    print(f"dataset: excluded machine router rows {json.dumps(dict(sorted(excluded.items())))} (labeler = s1/label.py teacher calls, "
          "review = templated review hand-offs)")
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
