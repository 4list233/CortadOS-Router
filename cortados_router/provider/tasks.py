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
- sensitive_cloud (#124): a request a sensitivity detector marked, answered in the cloud because the Pro could not
  take it. Published as its fixed categories only (detector, reason, notice shown or opted out), re-validated against
  provider/sensitive_notice's vocabulary; anything else is dropped. Counted per window in totals.sensitive_cloud.
- tokens: recorded per routed conversation since the work-split change (core.log_tokens sums each backend turn's
  input / output / cache read / cache write onto the route's row). A row with no recorded turn (older rows, the local
  lane, a turn that never completed) says `tokens_status: not recorded` and carries no estimate. `token_totals` sums
  them per window, overall, by orchestrator session tag and by lane/model (provider/work_split.routed_tokens).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from cortados_router import core
from cortados_router.provider import office, routing, sensitive_notice, usage, work_split

# 200 decisions gives a useful recent task table without shipping history on each 30 s dashboard
# poll. A smaller cap hides recent tasks; a larger one costs DB read time and DOM size. One
# look-ahead row discloses truncation, so no COUNT(*) scan runs on the live routing database.
ROW_LIMIT = 200
# The JSON inputs are cut in SQL before loading: answers is 12 short keys (far under 4 KiB); hard_rules
# is a list of up to ~20 sentences naming lanes and models (a few KiB). A cut that breaks the JSON is
# reported as "unreadable" by _reason, never silently read as "no rules".
ANSWERS_MAX = 4096
RULES_MAX = 16384
SENS_MAX = 512                  # sensitive_cloud: three short category strings as JSON
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
    "Tokens are the backends' reported usage summed over a routed conversation's turns; rows from before token "
    "recording, local-lane rows and conversations with no completed turn show 'not recorded', never an estimate. "
    "Request and response sizes are recorded character counts.",
    "Sensitive in cloud: a request a sensitivity detector (flag, keyword, S1, cloud refusal) marked, answered by a "
    "cloud model because the Pro could not take it; shown as the detector and reason categories only.",
    "S1 reason summarises the recorded band, typed answers and hard rules by fixed category, never scorer prose; "
    "rules that can embed free text (escalation reasons, fit failures) are named by category only.",
]
_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_BANDS = ("R0", "R1", "R2", "R3", "R4", "pin")
# core.log_execution's "provider/model": office's model rule plus '#' for an account's provider (claude-cli#b)
_EXEC_RE = re.compile(r"[A-Za-z0-9._/\[\]:#-]{1,128}")
# Hard rules leave only as these fixed labels, NEVER as the recorded strings: an escalation reason or a fit
# failure can embed user text or paths. The prefixes are core.resolve's and routing.choose_pinned's own
# wording; a rule matching none stays visible as a count. Order matters only for readability.
_RULES = (
    ("pinned by the client:", "client pin"),
    # routing.choose_local_pick: the effect boundary profile every turn of a local route pick runs under (step 8)
    ("boundary profile: orchestrator-sensitive", "boundary profile orchestrator-sensitive"),
    ("boundary profile: orchestrator", "boundary profile orchestrator"),
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


# core.resolve's band window rules ("band window R2: band R1 raised to R2", "band window R2 overridden: blast radius 3
# …"): labelled with the window itself, which the pattern admits only as band names (never free text), so the Tasks
# view shows "band window R2" / "band window R2 overridden". The difficulty step ("band window: difficulty …") has its
# own fixed label.
_WINDOW_RULE = re.compile(r"band window (R[0-4](?:-R[0-4])?)( overridden)?[: ]")
# A route pick rule names the pick's label (band names and table ids, routing.Pick) before its colon; routing.choose
# writes `route pick <label>: <n> model(s), <m> route(s)` and `route pick <label> overridden: sensitive`.
_PICK_RULE = re.compile(r"route pick ((?:any|local|R[0-4](?:-R[0-4])?)(?::(?:\*|[a-z0-9._+-]{1,600}))?)"
                        r"( overridden)?: (?:(\d{1,2}) models?, )?")


# routing.choose_local_pick's rule: `route pick local[:<pin ids>] (orchestrator profile): <pin> on node <node>`.
_LOCAL_PICK_RULE = re.compile(r"route pick local(?::[A-Za-z0-9._+-]{1,600})? \(orchestrator profile\): ")
# its other rules: `route pick local…: marked sensitive by <detector> (E5): …` and `… <pin> on node <n> skipped: …`
_LOCAL_PICK_NOTE_RULE = re.compile(r"route pick local(?::[A-Za-z0-9._+-]{1,600})?: (?:marked sensitive by |"
                                   r"[A-Za-z0-9._:-]{1,128} on node [A-Za-z0-9._?-]{1,64} skipped: )")


def _rule_label(rule: Any, ids: "frozenset[str] | None" = None) -> str | None:
    if not isinstance(rule, str):
        return None
    m = _WINDOW_RULE.match(rule)
    if m:
        return "band window " + m.group(1) + (" overridden" if m.group(2) else "")
    if rule.startswith("band window: difficulty "):
        return "band window difficulty cap"
    if rule.startswith("route pick"):
        # routing.choose's route pick rules (docs/ORCHESTRATOR-ROUTE-PICKER.md): fixed labels only; the pick's own
        # label and the reasons stay on the row.
        if rule.startswith("route pick escalated: "):
            return "route pick escalated"
        if rule.startswith("route pick: local cost routes not taken"):
            return "route pick: local cost route not taken"
        if _LOCAL_PICK_RULE.match(rule):
            # routing.choose_local_pick (step 8): a local orchestrator, plan and delegate only. Fixed words: the pin
            # ids (provider config, not table ids) stay on the row.
            return "route pick local (orchestrator profile)"
        if _LOCAL_PICK_NOTE_RULE.match(rule):
            return ("route pick local: sensitive, read and plan only" if " marked sensitive by " in rule else
                    "route pick local: model skipped")
        m = _PICK_RULE.match(rule)
        if m:
            # The label is shown only when it is one (routing.view_pick_label) and its every id is a table id (`ids`,
            # routing.table_model_ids): never text a request could have put there. Else the fixed word alone.
            label = routing.view_pick_label(m.group(1), ids if ids is not None else frozenset())
            shown = "route pick" + (" " + label if label else "")
            if m.group(2):
                return shown + " overridden"
            return shown + (f" ({int(m.group(3))} model{'s' if int(m.group(3)) != 1 else ''})" if m.group(3) else "")
        return None
    for prefix, label in _RULES:
        if rule.startswith(prefix):
            return label
    for word, label in _RULE_WORDS.items():
        if word in rule:
            return label
    return None


def _reason(row: dict, pinned: bool, ids: "frozenset[str] | None" = None) -> str:
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
            label = _rule_label(rule, ids)
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


def collect(state_dir: Path, now: float | None = None) -> dict[str, Any]:
    """Bounded metadata projection; absence and errors are explicit entries, never an empty success.

    Percentages use this same bounded snapshot (not unbounded session aggregates), so numerator and
    denominator come from one window. Decisions without a session ID are counted as `unassigned`, never
    folded into one invented session.
    """
    view: dict[str, Any] = {
        "tasks": [], "sessions": [], "totals": {"decisions": 0, "routed": 0, "pinned": 0, "unselected": 0,
                                                "sensitive_cloud": 0},
        "row_limit": ROW_LIMIT, "truncated": False, "unassigned_decisions": 0,
        "tokens_status": "not recorded", "percentage_definition": DEFINITION, "notes": list(NOTES), "errors": [],
        "token_totals": None,
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
            # The token columns exist once the writer has opened the db after the work-split change (core._db);
            # until then they are read as NULL, i.e. not recorded.
            has_tok = work_split.has_token_columns(conn)
            tok_sql = ("tok_input, tok_output, tok_cache_read, tok_cache_write, tok_turns" if has_tok else
                       "NULL AS tok_input, NULL AS tok_output, NULL AS tok_cache_read, NULL AS tok_cache_write, "
                       "NULL AS tok_turns")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(routes)")}
            has_sens = "sensitive_cloud" in cols
            sens_sql = "substr(sensitive_cloud,1,?) AS sensitive_cloud" if has_sens else "? AS sensitive_cloud"
            # route_pick: added by core._db on the writer's next open of an older db; NULL (no pick) until then
            pick_sql = "substr(route_pick,1,?) AS route_pick" if "route_pick" in cols else "? AS route_pick"
            rows = conn.execute(
                "SELECT turn_id, ts, session_id, lane, model, actual_model, band, engine, degraded, turn_ms, engine_ms, "
                "request_chars, response_chars, substr(answers,1,?) AS answers, substr(hard_rules,1,?) AS hard_rules, "
                f"{tok_sql}, {sens_sql}, {pick_sql} FROM routes ORDER BY ts DESC LIMIT ?",
                (ANSWERS_MAX, RULES_MAX, SENS_MAX if has_sens else None,
                 core.ROUTE_PICK_MAX + 1 if "route_pick" in cols else None, ROW_LIMIT + 1),
            ).fetchall()
            view["token_totals"] = work_split.routed_tokens(conn, now if now is not None else time.time())
        finally:
            conn.close()  # release the SQLite reader before JSON parsing or rendering
    except sqlite3.Error as exc:
        # Exception messages can carry paths or SQL; publish the category only (as usage.collect does).
        view["errors"].append({"source": "router.db", "error": type(exc).__name__})
        _share(view["totals"])
        return view
    view["truncated"] = len(rows) > ROW_LIMIT
    if (view["token_totals"] or {}).get("status") == "recorded":
        view["tokens_status"] = "recorded per routed conversation"
    sessions: dict[str, dict] = {}
    try:
        ids = routing.table_model_ids(core)       # a pick label's ids are shown only when they are table ids
    except Exception as exc:                     # the tables cannot say: no label shows its ids (fixed words only)
        view["errors"].append({"source": "tables", "error": type(exc).__name__})
        ids = frozenset()
    for raw in rows[:ROW_LIMIT]:
        row = dict(raw)
        row["task_id"] = office.clean(row.pop("turn_id"), _ID_RE)
        row["session_id"] = office.clean(row["session_id"], _ID_RE)
        row["model"] = office.clean(row["model"], office._MODEL_RE)
        # the execution record names the account's provider (claude-cli#b/…): '#' allowed here, or every account-B row
        # read as "not executed" (#132)
        row["actual_model"] = office.clean(row["actual_model"], _EXEC_RE)
        row["lane"] = office.clean(row["lane"], office._LANE_RE)
        row["band"] = row["band"] if row["band"] in _BANDS else None
        pinned = row.pop("engine") == "pin" or row["band"] == "pin"
        row["degraded"] = bool(row["degraded"])
        row["s1_reason"] = _reason(row, pinned, ids)     # consumes answers and hard_rules
        # the route pick the conversation was opened under (routing.choose → core.log_route_pick), re-checked
        row["pick"] = routing.view_pick_label(row.pop("route_pick"), ids)
        row["ts"] = _number(row["ts"])
        # #132: elapsed_ms is the first turn's time (log_outcome), null — never 0 — until it is recorded; a stored 0
        # can only be a missing measurement. end_to_end_ms adds S1's scoring time (engine_ms: null for a pin, which
        # S1 never sees, so a pin's end-to-end time is its turn alone).
        turn = _number(row.pop("turn_ms"))
        row["elapsed_ms"] = turn if turn else None
        s1_ms = _number(row.pop("engine_ms"))
        row["s1_ms"] = None if pinned else s1_ms
        row["end_to_end_ms"] = (turn + (0 if pinned else (s1_ms or 0))) if turn and (pinned or s1_ms is not None) else None
        # actual_model is the execution record "provider/model" (core.log_execution), kept as is; its two parts are
        # also given separately so a report compares the model with `model` directly (#132).
        prov, _, name = (row["actual_model"] or "").partition("/")
        row["executed_provider"] = prov if row["actual_model"] and name else None
        row["executed_model"] = name if row["actual_model"] and name else None
        row["request_chars"] = _number(row["request_chars"])
        row["response_chars"] = _number(row["response_chars"])
        tok = {k: _number(row.pop("tok_" + k)) for k in work_split.TOKEN_FIELDS}
        if _number(row.pop("tok_turns")):
            row["tokens"] = {k: int(v or 0) for k, v in tok.items()}
            row["tokens_status"] = "recorded"
        else:
            row["tokens"] = None
            row["tokens_status"] = "not recorded"
        sens, _ = _json(row.pop("sensitive_cloud"), dict)
        row["sensitive_cloud"] = sensitive_notice.valid_record(sens)
        if row["sensitive_cloud"]:
            view["totals"]["sensitive_cloud"] += 1
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
