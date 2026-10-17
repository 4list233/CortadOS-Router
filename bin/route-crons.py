#!/usr/bin/env python3
"""Collect completed cron outcomes using the donor sampler's occurrence evidence.

PURPOSE: Export CortadOS cron outcome collection for standalone direct-file callers.
CONTRACT: RT-MCP-FOLLOWUP-001; header evidence owns attribution and occurrence keys
make rescans idempotent. Saved job pins remain inputs because dispatch owns routing.
DEPENDS: core._db owns the router database; stdlib reads retained Hermes outputs.
CEILING: Write outcome projections and failed-route hints only; never steer providers.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE = Path(os.environ.get("CORTADOS_HOME") or Path.home() / ".cortados")
HERMES = Path(os.environ.get("HERMES_HOME") or Path.home() / ".cortados/hermes")
sys.path.insert(0, str(REPO / "cortados_router"))
import core  # noqa: E402

BAD_ROUTES = STATE / "cron-bad-routes.json"
LANE_PROVIDER = {
    "claude": "claude-cli", "codex": "openai-codex", "cursor": "cursor-cli",
    "antigravity": "agy-cli", "local": "custom",
}
FAIL_RE = re.compile(
    r"(?i)^(error|traceback|\[error\]|api error|not logged in|rate limit|quota|exhausted|usage limit|i (?:can't|cannot|am unable))"
)
# Retain the donor's six output files per job per scan: fewer miss recent retained
# occurrences; more increase repeat scan I/O. Older unrecorded files stay outside recovery.
OUTPUT_SCAN_LIMIT = 6
# Retain the donor's three-character minimum response: lower accepts empty-like
# output; higher rejects terse successful replies. Units: response characters.
MIN_RESPONSE_CHARS = 3
# Retain the donor's minute-width timestamp match: fewer than 16 characters can
# confuse occurrences; more can miss saved timestamps with different seconds formats.
STATUS_TIMESTAMP_CHARS = 16
# Retain the donor's 300-character diagnostic head: fewer hide failure evidence;
# more increase database size and retained response content. Units: characters.
RESPONSE_HEAD_CHARS = 300
# Retain the donor's two-failure window: fewer blacklist on a single transient;
# more delay avoidance of repeatedly failing routes. Units: recorded occurrences.
FAILED_ROUTE_OCCURRENCES = 2
# Retain the donor's one-day avoidance: fewer than 86400 seconds retry bad routes
# sooner; more exclude recovered capacity longer. Units: seconds from observation.
BAD_ROUTE_TTL_S = 86400


def profiles() -> list[tuple[str, Path, str]]:
    out = [("default", HERMES / "cron/jobs.json", "hermes")]
    for f in glob.glob(str(HERMES / "profiles/*/cron/jobs.json")):
        name = Path(f).parents[1].name
        cli = name if (Path.home() / ".local/bin" / name).exists() else f"hermes -p {name}"
        out.append((name, Path(f), cli))
    return [(n, p, c) for n, p, c in out if p.exists()]


def db() -> sqlite3.Connection:
    c = core._db()
    c.execute("""CREATE TABLE IF NOT EXISTS cron_outcomes (
        key TEXT PRIMARY KEY, ts REAL, profile TEXT, job_id TEXT, job_name TEXT,
        provider TEXT, model TEXT, status TEXT, response_chars INTEGER,
        verdict TEXT, response_head TEXT, turn_id TEXT)""")
    return c


def record_outcomes(c: sqlite3.Connection) -> None:
    bad = json.loads(BAD_ROUTES.read_text()) if BAD_ROUTES.exists() else {}
    for profile, path, _ in profiles():
        base = path.parent / "output"
        with path.open() as jobs_file:
            jobs = json.load(jobs_file)["jobs"]
        for j in jobs:
            for f in sorted(glob.glob(str(base / j["id"] / "*.md")))[-OUTPUT_SCAN_LIMIT:]:
                key = f"{j['id']}:{Path(f).stem}"
                if c.execute("SELECT 1 FROM cron_outcomes WHERE key=?", (key,)).fetchone():
                    continue
                text = Path(f).read_text(errors="replace")
                resp = text.split("## Response", 1)[1].strip() if "## Response" in text else ""
                header = text.split("## Prompt", 1)[0]
                m = re.search(r"\*\*Model:\*\*\s*(\S+)", header)
                model = m.group(1) if m else None
                p = re.search(r"\*\*Provider:\*\*\s*(\S+)", header)
                provider = p.group(1) if p else None
                session = re.search(r"\*\*Routing Session:\*\*\s*(\S+)", header)
                turn_id = f"cron-dispatch:{profile}:{session.group(1)}" if session else None
                stamp = Path(f).stem.replace("_", " ")[:STATUS_TIMESTAMP_CHARS]
                status = j.get("last_status") if stamp in (j.get("last_run_at") or "").replace("T", " ") else "ok?"
                verdict = "FAILED" if (
                    not resp or len(resp) < MIN_RESPONSE_CHARS or FAIL_RE.match(resp) or status == "error"
                ) else "CONFIRMED"
                c.execute("INSERT OR REPLACE INTO cron_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (key, os.path.getmtime(f), profile, j["id"], j["name"], provider, model,
                           status, len(resp), verdict, resp[:RESPONSE_HEAD_CHARS], turn_id))
                if turn_id and provider and model and verdict == "CONFIRMED":
                    c.execute("UPDATE routes SET actual_model=?, response_chars=? WHERE turn_id=?",
                              (f"{provider}/{model}", len(resp), turn_id))
                route = f"{provider}/{model}"
                if verdict == "FAILED" and provider and model:
                    fails = c.execute(
                        "SELECT verdict FROM cron_outcomes WHERE job_id=? AND provider||'/'||model=? "
                        "ORDER BY ts DESC LIMIT ?", (j["id"], route, FAILED_ROUTE_OCCURRENCES)
                    ).fetchall()
                    if len(fails) == FAILED_ROUTE_OCCURRENCES and all(v[0] == "FAILED" for v in fails):
                        lane = next((k for k, v in LANE_PROVIDER.items() if v == provider), provider)
                        bad.setdefault(j["id"], {})[f"{lane}/{model}"] = time.time() + BAD_ROUTE_TTL_S
                print(f"outcome {profile}/{j['name']} {Path(f).stem}: {route} → {verdict} ({len(resp)} chars)")
    c.commit()
    BAD_ROUTES.write_text(json.dumps(bad, indent=1))


if __name__ == "__main__":
    con = db()
    try:
        record_outcomes(con)
    finally:
        con.close()
