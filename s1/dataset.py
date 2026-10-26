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
from pathlib import Path

from common import DATA, clean_request, read_jsonl, split_of

SHEET = Path(__file__).resolve().parent / "sources/labeling-sheet.csv"
ROUTER_DB = Path.home() / ".cortados/router.db"


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


def _rows_from_db(db: Path) -> list:
    """Requests from one router db (read-only). Raises on an unreadable/locked/malformed db; the caller isolates that so
    one bad source never discards rows already collected from the healthy ones."""
    tag = _source_tag(db)
    c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        rows = []
        for tid, kind, plat, req in c.execute("SELECT turn_id, unit_kind, platform, request FROM routes ORDER BY ts"):
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
    if SHEET.exists():
        for r in csv.DictReader(SHEET.open()):
            new.append({"id": r["id"], "source": "1.0-sheet", "unit_kind": "interactive", "request": clean_request(r["request"])})
    for db in _router_dbs():
        try:
            if not db.exists():                             # a synced copy that has not arrived yet is simply skipped
                continue
            new.extend(_rows_from_db(db))
        except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
            print(f"dataset: skipping router db {db}: {exc}", file=sys.stderr)   # one bad/unreadable source never loses the others
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
