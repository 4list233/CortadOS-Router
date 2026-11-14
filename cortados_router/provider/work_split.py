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
The join key is the Office session tag (sha256 of the mod's salt and the session id, 16 hex): both sides compute the
same one, and neither ever carries the raw session id.

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
SESSIONS_MAX = 40          # joined session rows in the live section
ORCH_STALE_S = 15 * 60     # an orchestrator block older than this (sender scans every 5 min) is shown stale
DEFINITION = (
    "Tokens, per window (24h, 7d). Orchestrator = what the owner's main Claude Code sessions spent directly with "
    "Anthropic, read from their local transcripts and reported by each host's heartbeat; it includes their Agent-tool "
    "subagents that ran on a direct model (shown again as the 'subagent' sub-total) and excludes child-task transcripts, "
    "the provider's own headless CLIs and any message served by the provider. Routed = what the CortadOS provider's backends reported for routed conversations (child tasks, "
    "routed subagents, route_task), joined to the main session by its Office session tag. Fresh tokens = input + output "
    "+ cache writes; 'all' adds cache reads. % routed = routed fresh ÷ (orchestrator fresh + routed fresh). A routed "
    "conversation counts in the window it was routed in; an orchestrator message by its own time. Local-lane and "
    "search-lane work, and routed conversations from before token recording, are not recorded and not estimated."
)


def _zero() -> dict[str, int]:
    return {k: 0 for k in TOKEN_FIELDS}


def _add(dst: dict[str, int], src: dict[str, Any]) -> None:
    for k in TOKEN_FIELDS:
        v = src.get(k)
        dst[k] += int(v) if type(v) in (int, float) and math.isfinite(v) and v >= 0 else 0


def _with_sums(t: dict[str, int]) -> dict[str, int]:
    return {**t, "fresh": ou.fresh_tokens(t), "all": ou.all_tokens(t)}


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def has_token_columns(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(routes)")}
    return set(_DB_COLS.values()) | {"tok_turns", "office_session"} <= cols


def routed_tokens(conn: sqlite3.Connection, now: float, windows: dict[str, int] | None = None) -> dict[str, Any]:
    """Per window: routed tokens overall, by orchestrator session tag and by lane/model, from router.db (one grouped
    query per window over the routes(ts) index). A db not yet migrated (no token columns) says `not recorded`."""
    windows = dict(WINDOWS if windows is None else windows)
    if not has_token_columns(conn):
        return {"status": "not recorded", "windows": {w: int(s) for w, s in windows.items()}, "by_window": {}}
    out: dict[str, Any] = {}
    for w, span in windows.items():
        total, by_session, by_model = _zero(), {}, {}
        conversations = recorded = unattributed = 0
        rows = conn.execute(
            "SELECT office_session, lane, model, COUNT(*), SUM(tok_turns IS NOT NULL), "
            "SUM(COALESCE(tok_input,0)), SUM(COALESCE(tok_output,0)), SUM(COALESCE(tok_cache_read,0)), "
            "SUM(COALESCE(tok_cache_write,0)) FROM routes WHERE ts >= ? AND lane IS NOT NULL AND model IS NOT NULL "
            "GROUP BY office_session, lane, model", (now - span,)).fetchall()
        for tag, lane, model, n, rec, *vals in rows:
            t = dict(zip(TOKEN_FIELDS, (int(v or 0) for v in vals)))
            conversations += int(n or 0)
            recorded += int(rec or 0)
            _add(total, t)
            tag = office.clean(tag, office._HEADER_RULES[office.SESSION_HEADER])
            if tag:
                s = by_session.setdefault(tag, {**_zero(), "conversations": 0})
                _add(s, t)
                s["conversations"] += int(n or 0)
            else:
                unattributed += ou.fresh_tokens(t)
            key = f"{office.clean(lane, office._LANE_RE) or '?'}/{office.clean(model, office._MODEL_RE) or '?'}"
            m = by_model.setdefault(key, {**_zero(), "conversations": 0})
            _add(m, t)
            m["conversations"] += int(n or 0)
        sessions = sorted(by_session.items(), key=lambda kv: ou.fresh_tokens(kv[1]), reverse=True)
        models = sorted(by_model.items(), key=lambda kv: ou.fresh_tokens(kv[1]), reverse=True)
        out[w] = {"totals": _with_sums(total), "conversations": conversations, "recorded": recorded,
                  "unrecorded": conversations - recorded, "unattributed_fresh": unattributed,
                  "by_session": {k: _with_sums({f: v[f] for f in TOKEN_FIELDS}) | {"conversations": v["conversations"]}
                                 for k, v in sessions[:BY_SESSION_MAX]},
                  "by_session_hidden": max(0, len(sessions) - BY_SESSION_MAX),
                  "by_model": [{"route": k, **_with_sums({f: v[f] for f in TOKEN_FIELDS}),
                                "conversations": v["conversations"]} for k, v in models[:BY_MODEL_MAX]]}
    return {"status": "recorded", "windows": {w: int(s) for w, s in windows.items()}, "by_window": out}


# ================================================================ the heartbeat block (provider side)
_HB_KEYS = {"version", "sampled_at", "salt", "windows", "totals", "subagent", "sessions", "sessions_hidden", "excluded"}
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
        if not isinstance(s, dict) or set(s) - ({"session", "project", "model", "last_at"} | set(WINDOWS)):
            return None, where
        row: dict[str, Any] = {}
        for k, rule in (("session", _TAG_RE), ("project", _PROJECT_RE), ("model", office._MODEL_RE)):
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
        out_sessions.append(row)
    hidden = b.get("sessions_hidden", 0)
    if not _count(hidden):
        return None, ".sessions_hidden"
    ex = b.get("excluded", {})
    if not isinstance(ex, dict) or set(ex) - set(_EXCL_KEYS) or not all(_count(ex.get(k, 0)) for k in _EXCL_KEYS):
        return None, ".excluded"
    if b.get("salt") is not None and type(b["salt"]) is not bool:
        return None, ".salt"
    return {"sampled_at": round(float(at), 3), "salt": bool(b.get("salt")), "totals": out_totals, "subagent": out_sub,
            "sessions": out_sessions, "sessions_hidden": hidden,
            "excluded": {k: ex.get(k, 0) for k in _EXCL_KEYS}}, None


# ================================================================ the live section
def section(routed: dict[str, Any] | None, heartbeats: dict[str, dict[str, Any]], now: float,
            errors: list | None = None) -> dict[str, Any]:
    """The live view's `work_split` section from routed_tokens() (via tasks.collect's `token_totals`) and the hosts'
    heartbeat `orchestrator` blocks. Each side's absence is an entry in `errors`, never a zero."""
    errors = list(errors or [])
    hosts, orch_rows = [], []
    orch_tot = {w: _zero() for w in WINDOWS}
    sub_tot: dict[str, dict[str, int] | None] = {w: _zero() for w in WINDOWS}
    for host, hb in sorted((heartbeats or {}).items()):
        o = hb.get("orchestrator") if isinstance(hb, dict) else None
        if not isinstance(o, dict):
            continue
        age = max(0.0, now - o["sampled_at"])
        hosts.append({"host": host, "sampled_at": o["sampled_at"], "age_s": round(age, 1), "stale": age > ORCH_STALE_S,
                      "salt": o["salt"], "sessions_hidden": o["sessions_hidden"], "excluded": o["excluded"]})
        for w in WINDOWS:
            _add(orch_tot[w], o["totals"][w])
            if o.get("subagent") is None:
                sub_tot[w] = None               # a host that does not split it out: the sum is not known
            elif sub_tot[w] is not None:
                _add(sub_tot[w], o["subagent"][w])
        for s in o["sessions"]:
            orch_rows.append({**s, "host": host})
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
                      "routed_unattributed_fresh": rw.get("unattributed_fresh", 0)}
    sessions, seen = [], set()
    orch_rows.sort(key=lambda s: s["last_at"], reverse=True)
    for s in orch_rows:
        row = {k: s.get(k) for k in ("session", "host", "project", "model", "last_at")}
        for w in WINDOWS:
            o = ou.fresh_tokens(s[w])
            r = ((by_window.get(w) or {}).get("by_session") or {}).get(s["session"]) if s["session"] else None
            rf = int((r or {}).get("fresh") or 0)
            row[w] = {"orchestrator_fresh": o, "orchestrator_all": ou.all_tokens(s[w]), "routed_fresh": rf,
                      "routed_all": int((r or {}).get("all") or 0), "routed_conversations": int((r or {}).get("conversations") or 0),
                      "routed_pct": _pct(rf, o + rf)}
        if s["session"]:
            seen.add(s["session"])
        sessions.append(row)
    # Routed work whose orchestrator no heartbeat reported (a host without the sender, a session past the sender's cap):
    # shown as its own rows, so routed tokens are never dropped from the per-session view.
    widest = max(WINDOWS, key=WINDOWS.get)
    for tag, r in ((by_window.get(widest) or {}).get("by_session") or {}).items():
        if tag in seen:
            continue
        row = {"session": tag, "host": None, "project": None, "model": None, "last_at": None}
        for w in WINDOWS:
            rr = ((by_window.get(w) or {}).get("by_session") or {}).get(tag) or {}
            rf = int(rr.get("fresh") or 0)
            row[w] = {"orchestrator_fresh": None, "orchestrator_all": None, "routed_fresh": rf,
                      "routed_all": int(rr.get("all") or 0), "routed_conversations": int(rr.get("conversations") or 0),
                      "routed_pct": None}
        sessions.append(row)
    has = bool(hosts) or bool(by_window)
    status = "ok" if not errors else ("partial" if has else "error")
    return {"as_of": round(now, 3), "status": status, "errors": errors, "definition": DEFINITION,
            "windows": {w: int(s) for w, s in WINDOWS.items()}, "overall": overall, "sessions": sessions[:SESSIONS_MAX],
            "sessions_hidden": max(0, len(sessions) - SESSIONS_MAX), "hosts": hosts}
