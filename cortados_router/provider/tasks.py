"""Read-only Tasks projection of router.db for GET /v1/cortados/tasks (the dashboard's Tasks section).

One task is one logged routing decision (a `routes` row), not necessarily one backend turn. No new
writer, no prompt access, no Office lock and no core._db_lock: the query runs on the dashboard's
read-only, deadline-limited SQLite connection (usage._ro_connect), bounded by ROW_LIMIT over the
`routes(ts)` index, so it never waits on the live routing path. Office tags and child-task events
have no durable join key to a route, so task titles and Office session identities are not guessed.

What is real here and what is not:
- model, actual_model, lane, band, degraded, elapsed_ms (turn_ms), request_chars, response_chars and
  the S1 inputs (answers, hard_rules) are recorded per route and published as metadata.
- the `request`, `context`, `task_context`, `engine_error` and `features` columns are never read: the
  SELECT names its columns, and the two JSON inputs are reduced to fixed labels before publishing.
- tokens are NOT recorded per route. usage.db holds tokens per completed backend turn (usage_ledger)
  but with no turn_id, so a task's tokens cannot be joined; every task says `tokens_status: not
  recorded` rather than carrying an estimate.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from cortados_router import core
from cortados_router.provider import office, usage

# 200 decisions gives a useful recent task table without shipping history on each 30 s dashboard
# poll. A smaller cap hides recent tasks; a larger one costs DB read time and DOM size. One
# look-ahead row discloses truncation, so no COUNT(*) scan runs on the live routing database.
ROW_LIMIT = 200
# The JSON inputs are cut in SQL before loading: answers is 12 short keys (far under 4 KiB); hard_rules
# is a list of up to ~20 sentences naming lanes and models (a few KiB). A cut that breaks the JSON is
# reported as "unreadable" by _reason, never silently read as "no rules".
ANSWERS_MAX = 4096
RULES_MAX = 16384
DEFINITION = (
    "% routed = 100 × routed decisions ÷ all logged decisions of that session, within the latest "
    f"{ROW_LIMIT} decisions. Routed = S1 (or its degraded fallback) selected the lane and model; pinned = the "
    "client named a sub/<model> pin, counted separately; unselected = no route was chosen. A decision-count "
    "share, not a token, time or whole-session share."
)
NOTES = [
    "One task is one router.db routing decision; continuation turns on a sticky session may not create rows. "
    "Elapsed is the recorded turn_ms of that decision's turn, not a whole task's duration.",
    "Session IDs are the router's recorded conversation IDs, not Office session tags; titles and Office/child-task "
    "links are not recorded with these decisions.",
    "Per-task tokens are not recorded: the usage ledger counts tokens per backend turn with no turn_id to join on, "
    "and no estimate is substituted. Request and response sizes are recorded character counts.",
    "S1 reason summarises the recorded band, typed answers and hard rules by fixed category, never scorer prose; "
    "rules that can embed free text (escalation reasons, fit failures) are named by category only.",
]
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_BANDS = ("R0", "R1", "R2", "R3", "R4", "pin")
# Hard rules leave only as these fixed labels, NEVER as the recorded strings: an escalation reason or a fit
# failure can embed user text or paths. The prefixes are core.resolve's and routing.choose_pinned's own
# wording; a rule matching none stays visible as a count. Order matters only for readability.
_RULES = (
    ("pinned by the client:", "client pin"),
    ("engine degraded → fallback band", "degraded scorer fallback"),
    ("scorer unavailable →", "scorer unavailable fallback"),
    ("outward effect → blast_radius floored at 3", "outward-effect blast-radius floor"),
    ("escalated (", "escalated difficulty floor"),
    ("trusted capability requirement floors band at ", "trusted capability floor"),
    ("deployment provider/model allowlist applied", "deployment allowlist"),
    ("needs index ≥ ", "index requirement"),
    ("turn ≈", "input-size exclusion"),
    ("sticky: session stays on ", "sticky session"),
    ("session moves off ", "session moved"),
    ("no available model reaches ", "strongest available taken"),
    ("no available candidate", "no candidate"),
    ("no router candidate fits input estimate", "no candidate fits input"),
    ("chosen ", "cheapest qualifying route"),
    ("needs clarification →", "needs clarification"),
    ("background unit on a scarce pool", "scarce pool: background deferred"),
)
_RULE_WORDS = {" demoted: ": "window pace demotion", " preferred: about ": "expiring window preferred",
               " takes it: its pool is underused": "underused pool preferred",
               " is scarce (": "scarce pool: sub-agents held"}


def _number(value: Any) -> int | float | None:
    return value if (type(value) in (int, float) and math.isfinite(value) and value >= 0) else None


def _json(value: Any, kind: type) -> tuple[Any, str]:
    """(parsed, state): state is `absent` (SQL NULL, or the JSON `null` log_route writes when S1 returned no answers),
    `unreadable` (not JSON of `kind`: a cut or foreign value) or `ok`."""
    if value is None:
        return None, "absent"
    try:
        parsed = json.loads(value) if isinstance(value, str) else ...
    except (ValueError, TypeError):
        parsed = ...
    if parsed is None:
        return None, "absent"
    return (parsed, "ok") if isinstance(parsed, kind) else (None, "unreadable")


def _rule_label(rule: Any) -> str | None:
    if not isinstance(rule, str):
        return None
    for prefix, label in _RULES:
        if rule.startswith(prefix):
            return label
    for word, label in _RULE_WORDS.items():
        if word in rule:
            return label
    return None


def _reason(row: dict, pinned: bool) -> str:
    """The S1 reason as fixed vocabulary: who decided, the band, the typed answers, the rule categories."""
    bits = ["client pin; S1 not asked" if pinned else
            "scorer degraded" if row["degraded"] else "S1 decision"]
    band = row["band"]
    bits.append("band " + band if band else "band not recorded")
    answers, state = _json(row.pop("answers"), dict)
    if not pinned:
        scores = []
        if answers:
            for key in core.FEATURES:
                v = answers.get(key)
                if type(v) is int and 0 <= v <= 3:
                    scores.append(f"{key}={v}")
            if answers.get("work_kind") in core.WORK_KINDS:
                scores.append("work_kind=" + answers["work_kind"])
            if answers.get("urgency") in core.URGENCY:
                scores.append("urgency=" + answers["urgency"])
            if type(answers.get("outward")) is bool:
                scores.append("outward=" + str(answers["outward"]).lower())
            if answers.get("needs_clarification") is True:
                scores.append("needs_clarification=true")
        if scores:
            bits.append(", ".join(scores))
        elif answers is not None:
            bits.append("typed answers recorded but none readable")
        else:
            bits.append("typed answers " + ("unreadable" if state == "unreadable" else "not recorded"))
    rules, state = _json(row.pop("hard_rules"), list)
    if rules:
        labels, withheld = set(), 0
        for rule in rules:
            label = _rule_label(rule)
            if label:
                labels.add(label)
            else:
                withheld += 1
        if labels:
            bits.append("rules: " + ", ".join(sorted(labels)))
        if withheld:
            bits.append(f"{withheld} more rule(s), details withheld")
    elif state == "unreadable":
        bits.append("hard rules unreadable")
    return "; ".join(bits)


def _kind(row: dict, pinned: bool) -> str:
    # The reason for each class is in DEFINITION. A degraded decision that still chose a lane and model is
    # routed: the router (not the client) made the choice, on its fallback band.
    return "pinned" if pinned else "routed" if row["lane"] and row["model"] else "unselected"


def _share(counts: dict) -> None:
    counts["routed_pct"] = round(100 * counts["routed"] / counts["decisions"], 1) if counts["decisions"] else None


def collect(state_dir: Path) -> dict[str, Any]:
    """Bounded metadata projection; absence and errors are explicit entries, never an empty success.

    Percentages use this same bounded snapshot (not unbounded session aggregates), so numerator and
    denominator come from one window. Decisions without a session ID are counted as `unassigned`, never
    folded into one invented session.
    """
    view: dict[str, Any] = {
        "tasks": [], "sessions": [], "totals": {"decisions": 0, "routed": 0, "pinned": 0, "unselected": 0},
        "row_limit": ROW_LIMIT, "truncated": False, "unassigned_decisions": 0,
        "tokens_status": "not recorded", "percentage_definition": DEFINITION, "notes": list(NOTES), "errors": [],
    }
    path = Path(state_dir) / "router.db"
    if not path.exists():
        view["errors"].append({"source": "router.db", "error": "absent"})
        _share(view["totals"])
        return view
    try:
        conn = usage._ro_connect(path, router=True)   # mode=rw + query_only: see router_db (#99)
        try:
            conn.row_factory = sqlite3.Row
            # Explicit projection: request, context, task_context, engine_error, features and every other
            # column stay unread. The two JSON inputs are cut in SQL before any row is loaded.
            rows = conn.execute(
                "SELECT turn_id, ts, session_id, lane, model, actual_model, band, engine, degraded, turn_ms, "
                "request_chars, response_chars, substr(answers,1,?) AS answers, substr(hard_rules,1,?) AS hard_rules "
                "FROM routes ORDER BY ts DESC LIMIT ?", (ANSWERS_MAX, RULES_MAX, ROW_LIMIT + 1),
            ).fetchall()
        finally:
            conn.close()  # release the SQLite reader before JSON parsing or rendering
    except sqlite3.Error as exc:
        # Exception messages can carry paths or SQL; publish the category only (as usage.collect does).
        view["errors"].append({"source": "router.db", "error": type(exc).__name__})
        _share(view["totals"])
        return view
    view["truncated"] = len(rows) > ROW_LIMIT
    sessions: dict[str, dict] = {}
    for raw in rows[:ROW_LIMIT]:
        row = dict(raw)
        row["task_id"] = office.clean(row.pop("turn_id"), _ID_RE)
        row["session_id"] = office.clean(row["session_id"], _ID_RE)
        for key in ("model", "actual_model"):
            row[key] = office.clean(row[key], office._MODEL_RE)
        row["lane"] = office.clean(row["lane"], office._LANE_RE)
        row["band"] = row["band"] if row["band"] in _BANDS else None
        pinned = row.pop("engine") == "pin" or row["band"] == "pin"
        row["degraded"] = bool(row["degraded"])
        row["s1_reason"] = _reason(row, pinned)          # consumes answers and hard_rules
        row["ts"] = _number(row["ts"])
        row["elapsed_ms"] = _number(row.pop("turn_ms"))
        row["request_chars"] = _number(row["request_chars"])
        row["response_chars"] = _number(row["response_chars"])
        row["tokens"] = None
        row["tokens_status"] = "not recorded"
        row["routing_kind"] = _kind(row, pinned)
        view["tasks"].append(row)
        view["totals"]["decisions"] += 1
        view["totals"][row["routing_kind"]] += 1
        sid = row["session_id"]
        if sid is None:
            view["unassigned_decisions"] += 1
            continue
        s = sessions.setdefault(sid, {"session_id": sid, "decisions": 0, "routed": 0, "pinned": 0, "unselected": 0,
                                      "last_ts": row["ts"]})
        s["decisions"] += 1
        s[row["routing_kind"]] += 1
    for session in sessions.values():
        _share(session)
    _share(view["totals"])
    view["sessions"] = list(sessions.values())            # most recent session first (rows arrive ts DESC)
    return view
