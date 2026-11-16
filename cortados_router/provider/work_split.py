"""Work split: how much of the owner's work his main (orchestrating) Claude Code sessions do themselves, and how much
they hand to CortadOS, measured in tokens (owner 2026-11-13: "a way to measure that").

Two sides, one unit:
  - routed        tokens the provider's backends reported per routed conversation (core.log_tokens: summed per backend
                  turn onto the conversation's `routes` row, with the x-cortados-session tag of the orchestrator whose
                  work it was). Child tasks, the Agent tool's routed subagents and route_task calls all carry that tag
                  (child_tasks passes it on), so they land on their orchestrator's session.
  - orchestrator  tokens the main sessions spent directly with Anthropic, read from their transcripts on the machine
                  that ran them (cortados_router/orchestrator_usage.py) and posted in that host's live heartbeat
                  (`orchestrator` block). Direct-model Agent-tool subagents count here (also as the `subagent`
                  sub-total); child-task transcripts, the provider's own headless CLIs and provider-served messages
                  are excluded there.
The join key is the Office session tag (sha256 of the machine's shared Office salt and the session id, 16 hex): both
sides compute the same one, and neither ever carries the raw session id.

Per-session attribution of routed work (#127), only on keys the provider observed on the requests themselves:
  - child task   the route row has an office_run (x-cortados-run-id). Its orchestrator is the row's office_origin
                 (x-cortados-origin, which the child runner sends: the task's origin, inherited by a nested task with no
                 orchestrator of its own); for a run whose requests carried none, the provider host's OWN task log
                 (origin_session, else the nearest ancestor's through parent_task_id: live._origin_of), passed in as
                 `run_origins`. Never the row's office_session: a child's x-cortados-session is the cortados-route
                 server's random per-process tag, not the orchestrator's.
  - route_task   no run id, office_via "route_task": the row's office_session (subagent_mcp.route_task_office_headers).
  - subagent     no run id, no via: the row's office_session (the cortados-routed-subagents mod's tag).
A heartbeat's fields are never a routed join key. A native row (heartbeat) matches a routed tag on its `session` or, when
the sender also had the mod's older store salt, its `session_alt` (both exact hashes; `matched_on` says which). A tag
found on one side only is published as such (`match`: "native only" / "routed only"), never paired by guess. Rows
written before #127 have no origin, run or via: their child-task work sits under the cortados-route server's tag, a
"routed only" row with no native side.

Definitions (also published in the view as `definition`, so the UI never has to restate them from memory):
  - fresh tokens = input + output + cache writes; `all` adds cache reads. The headline % is on fresh tokens: a long
    session re-reads its whole cached context on every turn, so cache reads would make the side with longer
    conversations look like all the work. Both are published.
  - % routed = routed ÷ (orchestrator + routed), per window, overall and per session.
  - A routed conversation counts in a window by the time it was ROUTED (routes.ts); its tokens are all its turns'. An
    orchestrator message counts by its own timestamp.
  - Not recorded, said as such: routed conversations from before this change, the local lane (Ollama reports no usage
    through its relay), the search lane (no route row) and conversations whose first turn never completed
    (`unrecorded` counts them). Main sessions on a host without a heartbeat orchestrator block are missing (`hosts`
    names the hosts that reported).

Reads router.db through the caller's reader connection (mode=rw + query_only, router_db #99). Metadata only.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any

from cortados_router import orchestrator_usage as ou
from cortados_router.provider import office

WINDOWS = dict(ou.WINDOWS)                         # the same names and spans the senders use
TOKEN_FIELDS = ou.TOKEN_FIELDS                     # input, output, cache_read, cache_write
_DB_COLS = {"input": "tok_input", "output": "tok_output", "cache_read": "tok_cache_read",
            "cache_write": "tok_cache_write"}
BY_SESSION_MAX = 100       # tags per window in the routed view: the owner's sessions in a week are far fewer
BY_MODEL_MAX = 24          # lane/model pairs per window: the pool has about a dozen routes
SESSION_ROUTES_MAX = 8     # lane/model pairs per session and window (the rest summed as "other")
PENDING_RUNS_MAX = 500     # child runs per window whose requests carried no origin, kept for the task-log lookup
VIAS = ("child_task", "subagent", "route_task")
_SPLIT_COLS = ("office_origin", "office_run", "office_via")
SESSIONS_MAX = 40          # joined session rows in the live section
ORCH_STALE_S = 15 * 60     # an orchestrator block older than this (sender scans every 5 min) is shown stale
DEFINITION = (
    "Tokens, per window (24h, 7d). Orchestrator = what the owner's main Claude Code sessions spent directly with "
    "Anthropic, read from their local transcripts and reported by each host's heartbeat; it includes their Agent-tool "
    "subagents that ran on a direct model (shown again as the 'subagent' sub-total) and excludes child-task transcripts, "
    "the provider's own headless CLIs and any message served by the provider. Routed = what the CortadOS provider's backends reported for routed conversations (child tasks, "
    "routed subagents, route_task), joined to the main session by its Office session tag. Fresh tokens = input + output "
    "+ cache writes; 'all' adds cache reads. % routed = routed fresh ÷ (orchestrator fresh + routed fresh); "
    "'% routed (all)' is the same on all tokens, cache reads included, which long native sessions dominate. Per "
    "session, routed work is attributed only on keys the provider observed: a child task by the orchestrator tag its "
    "requests carried (else the provider host's own task log, through parent tasks), a routed subagent or route_task "
    "call by its session tag; native and routed rows join on the session tag (or the sender's older-salt tag), "
    "and a tag seen on one side only is shown as such. A routed conversation counts in the window it was routed in; "
    "an orchestrator message by its own time. Local-lane and search-lane work, and routed conversations from before "
    "token recording, are not recorded and not estimated."
)


def _zero() -> dict[str, int]:
    return {k: 0 for k in TOKEN_FIELDS}


def _add(dst: dict[str, int], src: dict[str, Any]) -> None:
    for k in TOKEN_FIELDS:
        v = src.get(k)
        dst[k] += int(v) if type(v) in (int, float) and math.isfinite(v) and v >= 0 else 0


def _with_sums(t: dict[str, int]) -> dict[str, int]:
    return {**t, "fresh": ou.fresh_tokens(t), "all": ou.all_tokens(t)}


# Messages-shaped usage (both bridges report it; core.log_tokens reads the same keys) -> TOKEN_FIELDS
_USAGE_KEYS = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
               "cache_write": "cache_creation_input_tokens"}


def usage_tokens(usage: Any) -> dict[str, int]:
    """One backend turn's usage as {input, output, cache_read, cache_write}: counts only, a bad value is 0 (as
    core.log_tokens counts it). For the in-memory per-conversation and per-run totals of the orchestrator tree."""
    u = usage if isinstance(usage, dict) else {}
    out = {}
    for k, src in _USAGE_KEYS.items():
        v = u.get(src)
        out[k] = int(v) if type(v) in (int, float) and math.isfinite(v) and 0 <= v < 1e12 else 0
    return out


def add_tokens(dst: dict[str, int], src: dict[str, int]) -> None:
    """dst += src over TOKEN_FIELDS (dst may be empty)."""
    for k in TOKEN_FIELDS:
        dst[k] = int(dst.get(k, 0)) + int(src.get(k, 0))


def pub_tokens(t: Any) -> dict[str, int] | None:
    """A token total as published by the live view: the four counts and `fresh` (input + output + cache writes), or None
    when nothing was recorded (never an estimate)."""
    if not isinstance(t, dict) or not any(t.get(k) for k in TOKEN_FIELDS):
        return None
    clean = {k: int(t.get(k) or 0) for k in TOKEN_FIELDS}
    return {**clean, "fresh": ou.fresh_tokens(clean)}


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def has_token_columns(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(routes)")}
    return set(_DB_COLS.values()) | {"tok_turns", "office_session"} <= cols


def has_split_columns(conn: sqlite3.Connection) -> bool:
    """The per-session attribution columns (#127, core.TOKEN_COLUMNS) exist: a db the writer has opened since."""
    return set(_SPLIT_COLS) <= {r[1] for r in conn.execute("PRAGMA table_info(routes)")}


_TAG = office._HEADER_RULES[office.SESSION_HEADER]


def _attribute(session: Any, origin: Any, run: Any, via: Any) -> tuple[str, str | None, str | None]:
    """(via, tag, pending run id) of one route group (module doc). tag None: unattributed; a pending run id: a child
    run whose requests carried no origin, for the provider host's task log to resolve (section's run_origins)."""
    run = office.clean(run, office._RUN_ID_RE)
    if run:
        o = office.clean(origin, _TAG)
        # a named origin (the bot gateway's) is no session: unattributed, and not looked up in the task log either
        return "child_task", o, (None if o or office.clean(origin, office._ORIGIN_RE) else run)
    return ("route_task" if via == "route_task" else "subagent"), office.clean(session, _TAG), None


def _session_acc() -> dict[str, Any]:
    return {**_zero(), "conversations": 0, "routes": {}, "via": {}}


def _acc_add(acc: dict[str, Any], via: str, route: str, t: dict[str, int], n: int) -> None:
    _add(acc, t)
    acc["conversations"] += n
    r = acc["routes"].setdefault(route, {**_zero(), "conversations": 0})
    _add(r, t)
    r["conversations"] += n
    v = acc["via"].setdefault(via, {**_zero(), "conversations": 0})
    _add(v, t)
    v["conversations"] += n


def _acc_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    _add(dst, src)
    dst["conversations"] += src.get("conversations", 0)
    for key in ("routes", "via"):
        for k, v in (src.get(key) or {}).items():
            d = dst[key].setdefault(k, {**_zero(), "conversations": 0})
            _add(d, v)
            d["conversations"] += v.get("conversations", 0)


def _pub_session(acc: dict[str, Any]) -> dict[str, Any]:
    """One session's routed side as published: its totals, `by_route` (lane/model, SESSION_ROUTES_MAX, the rest as
    "other") and `by_via` (child_task, subagent, route_task), each with the four counts, fresh and all."""
    routes = sorted(acc["routes"].items(), key=lambda kv: (ou.fresh_tokens(kv[1]), ou.all_tokens(kv[1])), reverse=True)
    by_route = [{"route": k, **_with_sums({f: v[f] for f in TOKEN_FIELDS}), "conversations": v["conversations"]}
                for k, v in routes[:SESSION_ROUTES_MAX]]
    if len(routes) > SESSION_ROUTES_MAX:
        rest = {**_zero(), "conversations": 0}
        for _, v in routes[SESSION_ROUTES_MAX:]:
            _add(rest, v)
            rest["conversations"] += v["conversations"]
        by_route.append({"route": "other", **_with_sums({f: rest[f] for f in TOKEN_FIELDS}),
                         "conversations": rest["conversations"]})
    return {**_with_sums({f: acc[f] for f in TOKEN_FIELDS}), "conversations": acc["conversations"],
            "by_route": by_route,
            "by_via": {k: {**_with_sums({f: v[f] for f in TOKEN_FIELDS}), "conversations": v["conversations"]}
                       for k, v in sorted(acc["via"].items())}}


def routed_tokens(conn: sqlite3.Connection, now: float, windows: dict[str, int] | None = None) -> dict[str, Any]:
    """Per window: routed tokens overall, by orchestrator session tag (attributed by the module doc's rules, with each
    session's lane/model and via breakdown) and by lane/model, from router.db (one grouped query per window over the
    routes(ts) index). Child runs whose requests carried no origin are kept by run id in `pending_runs` for section()
    to resolve. A db not yet migrated (no token columns) says `not recorded`."""
    windows = dict(WINDOWS if windows is None else windows)
    if not has_token_columns(conn):
        return {"status": "not recorded", "windows": {w: int(s) for w, s in windows.items()}, "by_window": {}}
    split = has_split_columns(conn)
    cols = ", ".join(_SPLIT_COLS) if split else "NULL, NULL, NULL"
    out: dict[str, Any] = {}
    for w, span in windows.items():
        total, by_session, by_model, pending = _zero(), {}, {}, {}
        conversations = recorded = unattributed = 0
        rows = conn.execute(
            f"SELECT office_session, {cols}, lane, model, COUNT(*), SUM(tok_turns IS NOT NULL), "
            "SUM(COALESCE(tok_input,0)), SUM(COALESCE(tok_output,0)), SUM(COALESCE(tok_cache_read,0)), "
            "SUM(COALESCE(tok_cache_write,0)) FROM routes WHERE ts >= ? AND lane IS NOT NULL AND model IS NOT NULL "
            f"GROUP BY office_session, {', '.join(_SPLIT_COLS) + ', ' if split else ''}lane, model",
            (now - span,)).fetchall()
        for sess, origin, run, via_raw, lane, model, n, rec, *vals in rows:
            t = dict(zip(TOKEN_FIELDS, (int(v or 0) for v in vals)))
            n = int(n or 0)
            conversations += n
            recorded += int(rec or 0)
            _add(total, t)
            key = f"{office.clean(lane, office._LANE_RE) or '?'}/{office.clean(model, office._MODEL_RE) or '?'}"
            via, tag, run_id = _attribute(sess, origin, run, via_raw)
            if tag:
                _acc_add(by_session.setdefault(tag, _session_acc()), via, key, t, n)
            elif run_id and (run_id in pending or len(pending) < PENDING_RUNS_MAX):
                _acc_add(pending.setdefault(run_id, _session_acc()), via, key, t, n)
            else:
                unattributed += ou.fresh_tokens(t)
            m = by_model.setdefault(key, {**_zero(), "conversations": 0})
            _add(m, t)
            m["conversations"] += n
        sessions = sorted(by_session.items(), key=lambda kv: ou.fresh_tokens(kv[1]), reverse=True)
        models = sorted(by_model.items(), key=lambda kv: ou.fresh_tokens(kv[1]), reverse=True)
        out[w] = {"totals": _with_sums(total), "conversations": conversations, "recorded": recorded,
                  "unrecorded": conversations - recorded, "unattributed_fresh": unattributed,
                  "by_session": {k: _pub_session(v) for k, v in sessions[:BY_SESSION_MAX]},
                  "by_session_hidden": max(0, len(sessions) - BY_SESSION_MAX),
                  "by_model": [{"route": k, **_with_sums({f: v[f] for f in TOKEN_FIELDS}),
                                "conversations": v["conversations"]} for k, v in models[:BY_MODEL_MAX]],
                  # internal (section() resolves and drops it): child runs with no observed origin, by run id
                  "pending_runs": pending}
    return {"status": "recorded", "split_keys": split, "windows": {w: int(s) for w, s in windows.items()},
            "by_window": out}


def resolve_pending(routed: dict[str, Any] | None, run_origins: dict[str, str] | None) -> dict[str, Any] | None:
    """routed_tokens() with its pending child runs moved onto their orchestrator's session when the provider host's own
    task log names one (`run_origins`: run id -> origin tag), the rest counted as unattributed. Returns a copy without
    `pending_runs` (internal: run ids are not published here); None stays None."""
    if not isinstance(routed, dict):
        return routed
    out = {**routed, "by_window": {}}
    for w, rw in (routed.get("by_window") or {}).items():
        rw = dict(rw)
        pending = rw.pop("pending_runs", None) or {}
        by_session = {k: v for k, v in (rw.get("by_session") or {}).items()}
        merged: dict[str, dict[str, Any]] = {}
        resolved_fresh = 0
        for run_id, acc in pending.items():
            tag = office.clean((run_origins or {}).get(run_id), _TAG)
            if not tag:
                rw["unattributed_fresh"] = rw.get("unattributed_fresh", 0) + ou.fresh_tokens(acc)
                continue
            _acc_merge(merged.setdefault(tag, _session_acc()), acc)
            resolved_fresh += ou.fresh_tokens(acc)
        for tag, acc in merged.items():
            prev = by_session.get(tag)
            if prev:
                base = _session_acc()
                _add(base, prev)
                base["conversations"] = prev.get("conversations", 0)
                for r in prev.get("by_route") or []:
                    base["routes"][r["route"]] = {**{f: r[f] for f in TOKEN_FIELDS}, "conversations": r["conversations"]}
                for k, v in (prev.get("by_via") or {}).items():
                    base["via"][k] = {**{f: v[f] for f in TOKEN_FIELDS}, "conversations": v["conversations"]}
                _acc_merge(base, acc)
                acc = base
            by_session[tag] = _pub_session(acc)
        rw["by_session"] = dict(sorted(by_session.items(), key=lambda kv: kv[1].get("fresh", 0), reverse=True))
        rw["resolved_from_task_log_fresh"] = int(rw.get("resolved_from_task_log_fresh") or 0) + resolved_fresh
        out["by_window"][w] = rw
    return out


# ================================================================ the heartbeat block (provider side)
_HB_KEYS = {"version", "sampled_at", "salt", "salt_source", "windows", "totals", "subagent", "sessions", "sessions_hidden",
            "excluded"}
SALT_SOURCES = ("shared", "store")
# sidechain_entries: sent by senders from before subagents were counted (they skipped sidechain lines); still accepted
_EXCL_KEYS = ("child_task_files", "provider_bridge_files", "sidechain_entries", "routed_model_entries",
              "unreadable_files", "bad_lines")
_TAG_RE = office._HEADER_RULES[office.SESSION_HEADER]
_PROJECT_RE = office._HEADER_RULES[office.PROJECT_HEADER]


def _count(v: Any) -> bool:
    return type(v) is int and 0 <= v < 10 ** 13


def _tokens(v: Any) -> dict[str, int] | None:
    if not isinstance(v, dict) or set(v) - set(TOKEN_FIELDS) - {"sessions"} or not all(_count(v.get(k)) for k in TOKEN_FIELDS):
        return None
    return {k: v[k] for k in TOKEN_FIELDS}


def validate_block(b: Any, now: float, skew_s: float) -> tuple[dict[str, Any] | None, str | None]:
    """A heartbeat's `orchestrator` block (orchestrator_usage.scan's summary), or (None, ".<bad field>"). Only the
    known windows, counts, 16-64 hex tags, project names and model ids pass; field names only are ever echoed."""
    if not isinstance(b, dict) or set(b) - _HB_KEYS:
        return None, ""
    at = b.get("sampled_at")
    if not (type(at) in (int, float) and math.isfinite(at) and now - skew_s <= at <= now + 120):
        return None, ".sampled_at"
    wins = b.get("windows")
    if not isinstance(wins, dict) or set(wins) != set(WINDOWS) or any(wins[w] != WINDOWS[w] for w in WINDOWS):
        return None, ".windows"
    totals = b.get("totals")
    if not isinstance(totals, dict) or set(totals) != set(WINDOWS):
        return None, ".totals"
    out_totals = {}
    for w in WINDOWS:
        t = _tokens(totals[w])
        n = totals[w].get("sessions", 0) if isinstance(totals[w], dict) else None
        if t is None or not _count(n):
            return None, f".totals.{w}"
        out_totals[w] = {**t, "sessions": n}
    # the direct-model subagent sub-total of `totals`; None from a sender that predates it (not known, not zero)
    sub = b.get("subagent")
    out_sub = None
    if sub is not None:
        if not isinstance(sub, dict) or set(sub) != set(WINDOWS):
            return None, ".subagent"
        out_sub = {}
        for w in WINDOWS:
            t = _tokens(sub[w])
            if t is None or isinstance(sub[w], dict) and "sessions" in sub[w]:
                return None, f".subagent.{w}"
            out_sub[w] = t
    sessions = b.get("sessions", [])
    if not isinstance(sessions, list) or len(sessions) > ou.SESSIONS_MAX:
        return None, ".sessions"
    out_sessions = []
    for i, s in enumerate(sessions):
        where = f".sessions[{i}]"
        if not isinstance(s, dict) or set(s) - ({"session", "session_alt", "project", "model", "last_at", "models"}
                                                 | set(WINDOWS)):
            return None, where
        row: dict[str, Any] = {}
        for k, rule in (("session", _TAG_RE), ("session_alt", _TAG_RE), ("project", _PROJECT_RE),
                        ("model", office._MODEL_RE)):
            if s.get(k) is None:
                row[k] = None
            else:
                row[k] = office.clean(s[k], rule)
                if row[k] is None:
                    return None, f"{where}.{k}"
        la = s.get("last_at")
        if not (type(la) in (int, float) and math.isfinite(la) and now - skew_s <= la <= now + 600):
            return None, where + ".last_at"
        row["last_at"] = round(float(la), 3)
        for w in WINDOWS:
            t = _tokens(s.get(w))
            if t is None:
                return None, f"{where}.{w}"
            row[w] = t
        models, bad = _models(s.get("models"))
        if bad is not None:
            return None, f"{where}.models{bad}"
        row["models"] = models
        out_sessions.append(row)
    hidden = b.get("sessions_hidden", 0)
    if not _count(hidden):
        return None, ".sessions_hidden"
    ex = b.get("excluded", {})
    if not isinstance(ex, dict) or set(ex) - set(_EXCL_KEYS) or not all(_count(ex.get(k, 0)) for k in _EXCL_KEYS):
        return None, ".excluded"
    if b.get("salt") is not None and type(b["salt"]) is not bool:
        return None, ".salt"
    if b.get("salt_source") is not None and b["salt_source"] not in SALT_SOURCES:
        return None, ".salt_source"
    return {"sampled_at": round(float(at), 3), "salt": bool(b.get("salt")), "salt_source": b.get("salt_source"),
            "totals": out_totals, "subagent": out_sub,
            "sessions": out_sessions, "sessions_hidden": hidden,
            "excluded": {k: ex.get(k, 0) for k in _EXCL_KEYS}}, None


def _models(v: Any) -> tuple[dict[str, dict[str, dict[str, int]]] | None, str | None]:
    """A session row's `models` (orchestrator_usage._row_models): None when absent (an older sender: not known), else
    {model: {window: tokens}}; (None, ".<bad part>") when it breaks a rule. At most MODELS_PER_SESSION + 1 models, each
    a model id or "other", each window a list of four counts."""
    if v is None:
        return None, None
    if not isinstance(v, dict) or len(v) > ou.MODELS_PER_SESSION + 1:
        return None, ""
    out = {}
    for m, per in v.items():
        if not (m == ou.OTHER_MODEL or office.clean(m, office._MODEL_RE)):
            return None, ""
        if not isinstance(per, dict) or set(per) != set(WINDOWS):
            return None, f"[{len(out)}]"
        row = {}
        for w in WINDOWS:
            a = per[w]
            if not isinstance(a, list) or len(a) != len(TOKEN_FIELDS) or not all(_count(x) for x in a):
                return None, f"[{len(out)}].{w}"
            row[w] = dict(zip(TOKEN_FIELDS, a))
        out[m] = row
    return out, None


# ================================================================ the live section
def _native(s: dict[str, Any], w: str) -> dict[str, Any]:
    """A heartbeat session row's native side in window `w`: its four counts, fresh and all, and `models` (per model,
    largest first; None from a sender that does not break it down)."""
    models = None
    if isinstance(s.get("models"), dict):
        models = sorted(({"model": m, **_with_sums(per[w])} for m, per in s["models"].items() if any(per[w].values())),
                        key=lambda x: (x["model"] == ou.OTHER_MODEL, -x["fresh"], -x["all"]))
    return {**_with_sums({k: int(s[w].get(k, 0)) for k in TOKEN_FIELDS}), "models": models}


def _routed_of(by_window: dict[str, Any], w: str, tags: list[str]) -> dict[str, Any] | None:
    """The routed side of a session in window `w`, summed over its matched tags (`session` and `session_alt` can both
    carry routed work: the mod's routed subagents under the store salt, start_task and route_task under the shared one).
    None when no tag has routed work in the window."""
    found = [r for r in (((by_window.get(w) or {}).get("by_session") or {}).get(t) for t in tags) if r]
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    acc = _session_acc()
    for r in found:
        part = _session_acc()
        _add(part, r)
        part["conversations"] = r.get("conversations", 0)
        for x in r.get("by_route") or []:
            part["routes"][x["route"]] = {**{f: x[f] for f in TOKEN_FIELDS}, "conversations": x["conversations"]}
        for k, v in (r.get("by_via") or {}).items():
            part["via"][k] = {**{f: v[f] for f in TOKEN_FIELDS}, "conversations": v["conversations"]}
        _acc_merge(acc, part)
    return _pub_session(acc)


def _window_row(native: dict[str, Any] | None, routed: dict[str, Any] | None) -> dict[str, Any]:
    o_f = native["fresh"] if native else None
    o_a = native["all"] if native else None
    r_f, r_a = int((routed or {}).get("fresh") or 0), int((routed or {}).get("all") or 0)
    both = native is not None
    return {"orchestrator_fresh": o_f, "orchestrator_all": o_a, "routed_fresh": r_f, "routed_all": r_a,
            "routed_conversations": int((routed or {}).get("conversations") or 0),
            "routed_pct": _pct(r_f, o_f + r_f) if both else None,
            "routed_pct_all": _pct(r_a, o_a + r_a) if both else None,
            "native": native, "routed": routed}


def section(routed: dict[str, Any] | None, heartbeats: dict[str, dict[str, Any]], now: float,
            errors: list | None = None, run_origins: dict[str, str] | None = None) -> dict[str, Any]:
    """The live view's `work_split` section from routed_tokens() (via tasks.collect's `token_totals`) and the hosts'
    heartbeat `orchestrator` blocks. Each side's absence is an entry in `errors`, never a zero. `run_origins` (run id ->
    origin tag, from the provider host's own task log) resolves child runs whose requests carried no origin."""
    errors = list(errors or [])
    routed = resolve_pending(routed, run_origins)
    hosts, orch_rows = [], []
    orch_tot = {w: _zero() for w in WINDOWS}
    sub_tot: dict[str, dict[str, int] | None] = {w: _zero() for w in WINDOWS}
    for host, hb in sorted((heartbeats or {}).items()):
        o = hb.get("orchestrator") if isinstance(hb, dict) else None
        if not isinstance(o, dict):
            continue
        age = max(0.0, now - o["sampled_at"])
        stale = age > ORCH_STALE_S
        hosts.append({"host": host, "sampled_at": o["sampled_at"], "age_s": round(age, 1), "stale": stale,
                      "salt": o["salt"], "salt_source": o.get("salt_source"), "sessions_hidden": o["sessions_hidden"],
                      "excluded": o["excluded"]})
        for w in WINDOWS:
            _add(orch_tot[w], o["totals"][w])
            if o.get("subagent") is None:
                sub_tot[w] = None               # a host that does not split it out: the sum is not known
            elif sub_tot[w] is not None:
                _add(sub_tot[w], o["subagent"][w])
        for s in o["sessions"]:
            orch_rows.append({**s, "host": host, "_stale": stale, "_age": round(age, 1)})
    if not hosts:
        errors.append({"source": "orchestrator", "error": "no host has reported orchestrator tokens"})
    by_window = (routed or {}).get("by_window") or {}
    if routed is None:
        if not any(isinstance(e, dict) and e.get("source") == "router.db" for e in errors):
            errors.append({"source": "router.db", "error": "unavailable"})
    elif routed.get("status") != "recorded":
        errors.append({"source": "router.db", "error": "tokens not recorded (routes not migrated yet)"})
    overall: dict[str, Any] = {}
    for w in WINDOWS:
        rw = by_window.get(w) or {}
        rt = {k: int((rw.get("totals") or {}).get(k) or 0) for k in TOKEN_FIELDS}
        o, r = _with_sums(orch_tot[w]), _with_sums(rt)
        overall[w] = {"orchestrator": o, "routed": r,
                      "orchestrator_subagent": _with_sums(sub_tot[w]) if hosts and sub_tot[w] is not None else None,
                      "routed_pct": _pct(r["fresh"], o["fresh"] + r["fresh"]),
                      "routed_pct_all": _pct(r["all"], o["all"] + r["all"]),
                      "routed_by_model": list(rw.get("by_model") or []),
                      "routed_conversations": rw.get("conversations", 0), "routed_unrecorded": rw.get("unrecorded", 0),
                      "routed_unattributed_fresh": rw.get("unattributed_fresh", 0),
                      "routed_from_task_log_fresh": rw.get("resolved_from_task_log_fresh", 0)}
    widest = max(WINDOWS, key=WINDOWS.get)
    routed_tags = set()
    for w in WINDOWS:
        routed_tags |= set(((by_window.get(w) or {}).get("by_session") or {}))
    sessions, seen = [], set()
    orch_rows.sort(key=lambda s: s["last_at"], reverse=True)
    for s in orch_rows:
        tags = [t for t in (s.get("session"), s.get("session_alt")) if t]
        matched = [t for t in tags if t in routed_tags]
        row = {k: s.get(k) for k in ("session", "host", "project", "model", "last_at")}
        if s.get("session_alt"):
            row["session_alt"] = s["session_alt"]
        row["native_stale"] = s["_stale"]
        row["native_age_s"] = s["_age"]
        row["matched_on"] = ["session" if t == s.get("session") else "session_alt" for t in matched]
        row["match"] = "both" if matched else ("native only" if tags else "untagged")
        for w in WINDOWS:
            row[w] = _window_row(_native(s, w), _routed_of(by_window, w, matched))
        seen.update(tags)
        sessions.append(row)
    # Routed work whose orchestrator no heartbeat reported (a host without the sender, a session past the sender's cap,
    # a child run from before #127 under its MCP server's tag): its own rows, so routed tokens are never dropped.
    for tag in sorted(routed_tags - seen,
                      key=lambda t: -int((((by_window.get(widest) or {}).get("by_session") or {}).get(t) or {})
                                         .get("fresh") or 0)):
        row = {"session": tag, "host": None, "project": None, "model": None, "last_at": None, "native_stale": None,
               "native_age_s": None, "matched_on": [], "match": "routed only"}
        for w in WINDOWS:
            row[w] = _window_row(None, _routed_of(by_window, w, [tag]))
        sessions.append(row)
    has = bool(hosts) or bool(by_window)
    status = "ok" if not errors else ("partial" if has else "error")
    return {"as_of": round(now, 3), "status": status, "errors": errors, "definition": DEFINITION,
            "windows": {w: int(s) for w, s in WINDOWS.items()}, "overall": overall, "sessions": sessions[:SESSIONS_MAX],
            "sessions_hidden": max(0, len(sessions) - SESSIONS_MAX), "hosts": hosts,
            "split_keys": bool((routed or {}).get("split_keys"))}


SESSION_NOTES = {
    "no_native": "this session's own (native) tokens are not reported: its machine sends no orchestrator heartbeat, the "
                 "session is past the sender's 30-row cap, or the sender has no Office salt",
    "no_routed": "no routed work recorded for this session",
    "routed_not_recorded": "routed tokens are not recorded on the provider (router.db not migrated or unreadable)",
    "no_hosts": "no host has reported orchestrator tokens",
    "untagged": "the sender has no Office salt, so its sessions cannot be matched",
}


def session_split(ws: dict[str, Any], tag: str) -> dict[str, Any]:
    """The pulse pane's work split: ONLY the row of session `tag` (its `session`, or the sender's `session_alt`) from
    section()'s output, plus partial-data notes that name no other session. Nothing of another session is in it: no
    other row, no fleet totals, no host list (the row's own host, age and staleness only)."""
    rows = [r for r in ws.get("sessions") or [] if tag in (r.get("session"), r.get("session_alt"))]
    row = rows[0] if rows else None
    notes = []
    errs = ws.get("errors") or []
    if any(isinstance(e, dict) and e.get("source") == "router.db" for e in errs):
        notes.append(SESSION_NOTES["routed_not_recorded"])
    if not ws.get("hosts"):
        notes.append(SESSION_NOTES["no_hosts"])
    if row is None or row.get("match") == "routed only":
        notes.append(SESSION_NOTES["no_native"])
    elif row.get("native_stale"):
        notes.append(f"native tokens are {round((row.get('native_age_s') or 0) / 60)} min old: "
                     f"the {row.get('host') or 'host'} heartbeat is stale")
    if row is not None and row.get("host"):
        h = next((x for x in ws.get("hosts") or [] if x.get("host") == row["host"]), None)
        bad = int(((h or {}).get("excluded") or {}).get("unreadable_files") or 0)
        if bad:
            notes.append(f"{bad} transcript file{'s' if bad != 1 else ''} on {row['host']} could not be read")
    if row is None or not any(((row.get(w) or {}).get("routed")) for w in WINDOWS):
        if SESSION_NOTES["routed_not_recorded"] not in notes:
            notes.append(SESSION_NOTES["no_routed"])
    partial = [n for n in notes if n != SESSION_NOTES["no_routed"]]
    status = "none" if row is None else ("partial" if partial else "ok")
    return {"as_of": ws.get("as_of"), "status": status, "session": tag, "definition": DEFINITION,
            "windows": dict(ws.get("windows") or {}), "row": row, "notes": notes}
