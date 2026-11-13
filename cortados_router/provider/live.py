"""The live view: GET /v1/cortados/live, one metadata-only snapshot of everything CortadOS is running right now, for the
browser dashboard (GET /dashboard) and the cortados-pulse pane (issue #73).

It composes views that already exist rather than re-deriving them:
  - tasks          start_task children (running / queued / last 24 h) from $CORTADOS_HOME/child-tasks.jsonl, read once
                   through office.read_child_events (bounded tail), with the routed model/lane the Office board saw for
                   the same run id, and a read-only peek at a running task's dir on this machine (child_tasks.peek_local:
                   queued phase, workspace state). A remote task is never asked over ssh: a 5 s poll must stay local.
                   So the section's `scope` is always "provider host": a task running on another execution host logs
                   its events into THAT host's child-tasks.jsonl and is absent here. The one trace of such a task on
                   this machine, the remote stub a start_task from here leaves in the tasks dir, is listed as
                   `elsewhere` ({task_id, host, started_at}, the last 10 h): started from here on another host, its
                   state there unknown to this view.
  - conversations  every conversation the provider holds open right now (Registry.live_conversations: model, lane,
                   opened, last used, busy), joined with the Office board for its kind, title, session and tool; child
                   tasks' own conversations included (kind child_task, run id = task id); plus the floors' orchestrators
                   known from heartbeats and routed one-shot workers the Registry does not hold.
  - routed         the latest routing decisions, from tasks.collect (router.db, read-only, row-limited).
  - subscriptions  every lane's % left per window, from usage.collect_live (scarcity.json read strictly: cache only,
                   never a vendor CLI), and the lowest general window across the lanes routing can use (lane_enabled).
  - s1             the scorer's /health probe usage already memoises (reachable, HTTP status, latency).
  - hosts          the fleet (default air, studio, pro: CORTADOS_LIVE_HOSTS), each with its hardware sample (usage's
                   hardware cards) and its job heartbeat (POST /v1/cortados/live/heartbeat, below), or "none" said so.

Every section carries its own `as_of`, a `status` (ok / partial / error, s1: up / down / unknown) and `errors`
[{source, error}] naming what failed: a failed or absent source is never shown as an empty success. Metadata only:
never prompt or request text, tool arguments, file paths, commands, tokens or environment. Every string here comes
from an already-published view or is re-validated by this module (strict charsets, length caps, no addresses).

The heartbeat. A host that runs jobs the provider cannot see (the Mac Studio's S1 scorer and its continuous training
loop) POSTs its job states:

    POST /v1/cortados/live/heartbeat     (owner token, or a client token with the live:heartbeat scope)
    {"host": "studio", "ttl_s": 900,
     "jobs": [{"name": "s1-scorer", "state": "running", "summary": "serving v12"},
              {"name": "s1-continuous", "state": "running", "summary": "sweep 3/8", "progress_pct": 37.5,
               "started_at": 1762850000}]}

A heartbeat may also carry the host's `hardware` (cpu/gpu busy %, memory, load, disk free %: numbers and the chip
name only) and, on the S1 host, an `s1` block (health, the served model's name, the pipeline's hold flags by fixed
name, a pending candidate's reason slug); cortados_router/live_heartbeat.py is the sender (one per host, every 60 s).
A post replaces that host's previous one. It is shown fresh for ttl_s (default 900, 60..86400), then stale, and is
forgotten after 24 h. A live:heartbeat client token posts for its bound host only (provider/clients.py; 403 otherwise),
and the board's LRU never evicts a configured host (CORTADOS_LIVE_HOSTS / the defaults, exec-hosts.json), so posts for
made-up names can only push out other made-up names. A malformed heartbeat is refused with 400 naming the bad field (never echoing its value), a body
over HEARTBEAT_MAX_BYTES with 413. docs/PROVIDER-PLAN.md ("Live view") has the schema and the sender.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from cortados_router import router_db
from cortados_router.provider import office

VERSION = 1
LIVE_PATH = "/v1/cortados/live"
HEARTBEAT_PATH = "/v1/cortados/live/heartbeat"
# ---- the window and caps, each with its reason ----
# The dashboard's "recent" is the owner's day: what finished in the last 24 h.
CHILD_WINDOW_S = 24 * 3600.0
# A child is queued for the Chrome lock up to 6 h (child_tasks.CHROME_QUEUE_MAX_S) and then runs up to 3 h
# (CHILD_MAX_S_DEFAULT): a start with no end event after 10 h is not shown as running, it is "unknown" and said so.
STALE_RUNNING_S = office.CHILD_WINDOW_S   # 10 h: the Office keeps an open child exactly as long
RUNNING_MAX = 50            # children running at once: a handful; 50 keeps a runaway fan-out visible but bounded
RECENT_MAX = 40             # finished in 24 h, newest first: a day's worth at the owner's pace
PEEK_MAX = 64               # task-dir peeks per snapshot (a few stats each): bounds the poll's disk work
CONVERSATIONS_MAX = 64      # in-flight conversations listed; the rest are counted in `hidden`
ROUTED_MAX = 25             # latest routing decisions shown; tasks.collect reads at most its own ROW_LIMIT (200)
# ---- heartbeat rules ----
HEARTBEAT_MAX_BYTES = 8192  # 16 jobs x ~300 bytes is under 5 KiB; a larger body is not a heartbeat
HEARTBEAT_HOSTS_MAX = 16    # the fleet is three machines; LRU past this, configured hosts exempt (HeartbeatBoard)
HEARTBEAT_KEEP_S = 24 * 3600.0
TTL_DEFAULT_S = 900         # a job loop posting every 5 min misses two posts before it is shown stale
TTL_MIN_S, TTL_MAX_S = 60, 86400
JOBS_MAX = 16
JOB_STATES = ("running", "idle", "ok", "scheduled", "failed", "stopped")
TIME_SKEW_S = 30 * 86400.0  # job timestamps within a month of the provider's clock; anything else is not a time
SAMPLE_FUTURE_SKEW_S = 120.0  # a heartbeat hardware sampled_at may run ahead of the provider's clock by this much, no more
ELSEWHERE_MAX = 50          # remote stubs listed (child_tasks.remote_stubs reads at most its own cap of dirs)
SCOPE = "provider host"     # the tasks section sees this machine's child-task log only (see the module doc)
MEMO_S = 2.0                # GET /v1/cortados/live: N pollers (dashboard tabs, pulse panes) share one collect per 2 s
DEFAULT_HOSTS = ("air", "studio", "pro")
HOSTS_ENV = "CORTADOS_LIVE_HOSTS"
SELF_ENV = "CORTADOS_LIVE_SELF"   # the provider host's known name, when its hostname matches none (hosts_section)

_HOST_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")       # exec_hosts.HOST_NAME_RE: one slug per machine everywhere
_JOB_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
_SUMMARY_RE = re.compile(r"[\x20-\x7e]{1,80}")       # office's title rule: printable ASCII, 80 chars
_CLIENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_WS_STATE_RE = re.compile(r"[a-z_-]{1,24}")
_TASK_STATES = ("succeeded", "failed", "cancelled")   # child_tasks.TERMINAL_STATES
# The main session that started a task (child_tasks.clean_origin): its Office tag, or a named origin (the bot gateway)
_ORIGIN_SESSION_RE = re.compile(r"[0-9a-f]{16,64}|grokbot")
ORIGINS_MAX = 32            # origin groups listed (sessions with a task in the window); the rest flagged truncated
ORIGIN_IDS_MAX = 20         # task ids per origin group and per orchestrator item, newest first
_HB_KEYS = {"host", "jobs", "ttl_s", "hardware", "s1"}
# heartbeat `hardware`: percentages, byte counts, the 1-min load, the chip name and when it was sampled; nothing else
_HB_PCT = ("cpu_busy_pct", "gpu_busy_pct", "disk_free_pct")
_HB_BYTES = ("mem_available_bytes", "mem_total_bytes")
_HB_HW_KEYS = _HB_PCT + _HB_BYTES + ("load_1m", "chip", "sampled_at")
# heartbeat `s1`: the S1 pipeline's flags by their fixed names (s1/nightly.sh: data/no-promote, data/no-train-tonight,
# data/nightly.stop, data/sweep-request.json), never a path or a file's contents
S1_FLAGS = ("no-promote", "no-train-tonight", "nightly-stop", "sweep-requested")
S1_HEALTH = ("up", "down", "unknown")
_HB_S1_KEYS = {"health", "model", "flags", "pending", "loaded_s"}
_REASON_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,47}")   # a pending candidate's reason (evaluation_pending, promotion_held)
_JOB_KEYS = {"name", "state", "summary", "started_at", "finished_at", "next_at", "progress_pct"}


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _r(x: Any, nd: int = 1) -> float | None:
    return round(float(x), nd) if _num(x) else None


# ================================================================ heartbeats
def validate_heartbeat(body: Any, now: float) -> tuple[dict[str, Any] | None, str | None]:
    """(heartbeat, None) or (None, the bad field's name). Field names only are ever echoed, never a value."""
    if not isinstance(body, dict):
        return None, "body"
    if set(body) - _HB_KEYS:
        return None, "(unknown field)"
    host = office.clean(body.get("host"), _HOST_RE)
    if host is None:
        return None, "host"
    ttl = body.get("ttl_s", TTL_DEFAULT_S)
    if not _num(ttl) or not TTL_MIN_S <= ttl <= TTL_MAX_S:
        return None, "ttl_s"
    jobs = body.get("jobs", [])
    if not isinstance(jobs, list) or len(jobs) > JOBS_MAX:
        return None, "jobs"
    out_jobs, names = [], set()
    for i, j in enumerate(jobs):
        where = f"jobs[{i}]"
        if not isinstance(j, dict) or set(j) - _JOB_KEYS:
            return None, where
        name = office.clean(j.get("name"), _JOB_RE)
        if name is None or name in names:
            return None, where + ".name"
        names.add(name)
        if j.get("state") not in JOB_STATES:
            return None, where + ".state"
        job: dict[str, Any] = {"name": name, "state": j["state"], "summary": None}
        if j.get("summary") not in (None, ""):
            job["summary"] = office.clean(j["summary"], _SUMMARY_RE)
            if job["summary"] is None:
                return None, where + ".summary"
        for k in ("started_at", "finished_at", "next_at"):
            v = j.get(k)
            if v is None:
                job[k] = None
            elif _num(v) and abs(v - now) <= TIME_SKEW_S:
                job[k] = round(float(v), 3)
            else:
                return None, f"{where}.{k}"
        p = j.get("progress_pct")
        if p is None:
            job["progress_pct"] = None
        elif _num(p) and 0 <= p <= 100:
            job["progress_pct"] = round(float(p), 1)
        else:
            return None, where + ".progress_pct"
        out_jobs.append(job)
    out: dict[str, Any] = {"host": host, "ttl_s": int(ttl), "jobs": out_jobs}
    if body.get("hardware") is not None:
        hw, bad = _validate_hw(body["hardware"], now)
        if bad is not None:
            return None, "hardware" + bad
        out["hardware"] = hw
    if body.get("s1") is not None:
        s1, bad = _validate_s1(body["s1"])
        if bad is not None:
            return None, "s1" + bad
        out["s1"] = s1
    return out, None


def _validate_hw(hw: Any, now: float) -> tuple[dict[str, Any] | None, str | None]:
    """A heartbeat's hardware block, or (None, ".<bad field>")."""
    if not isinstance(hw, dict) or set(hw) - set(_HB_HW_KEYS):
        return None, ""
    out: dict[str, Any] = {}
    for k in _HB_HW_KEYS:
        v = hw.get(k)
        if v is None:
            out[k] = None
        elif k in _HB_PCT:
            if not (_num(v) and 0 <= v <= 100):
                return None, "." + k
            out[k] = round(float(v), 1)
        elif k in _HB_BYTES:
            if not (_num(v) and 0 <= v <= 2 ** 50):
                return None, "." + k
            out[k] = int(v)
        elif k == "load_1m":
            if not (_num(v) and 0 <= v <= 4096):
                return None, "." + k
            out[k] = round(float(v), 2)
        elif k == "chip":
            out[k] = office.clean(v, _SUMMARY_RE)
            if out[k] is None:
                return None, "." + k
        elif k == "sampled_at":
            # a sample is never from the future: a little clock skew is allowed, a later stamp would make it look
            # fresher than any real reading (and win the per-field merge in hosts_section)
            if not (_num(v) and now - TIME_SKEW_S <= v <= now + SAMPLE_FUTURE_SKEW_S):
                return None, "." + k
            out[k] = round(float(v), 3)
    return out, None


def _validate_s1(s1: Any) -> tuple[dict[str, Any] | None, str | None]:
    """A heartbeat's s1 block, or (None, ".<bad field>")."""
    if not isinstance(s1, dict) or set(s1) - _HB_S1_KEYS:
        return None, ""
    health = s1.get("health", "unknown")
    if health not in S1_HEALTH:
        return None, ".health"
    out: dict[str, Any] = {"health": health, "model": None, "flags": [], "pending": None, "loaded_s": None}
    if s1.get("model") not in (None, ""):
        out["model"] = office.clean(s1["model"], _SUMMARY_RE)
        if out["model"] is None:
            return None, ".model"
    flags = s1.get("flags", [])
    if not isinstance(flags, list) or len(flags) > len(S1_FLAGS) or any(f not in S1_FLAGS for f in flags) \
            or len(set(flags)) != len(flags):
        return None, ".flags"
    out["flags"] = [f for f in S1_FLAGS if f in flags]
    if s1.get("pending") not in (None, ""):
        out["pending"] = office.clean(s1["pending"], _REASON_RE)
        if out["pending"] is None:
            return None, ".pending"
    if s1.get("loaded_s") is not None:
        if not (_num(s1["loaded_s"]) and 0 <= s1["loaded_s"] <= 86400):
            return None, ".loaded_s"
        out["loaded_s"] = round(float(s1["loaded_s"]), 1)
    return out, None


class HeartbeatBoard:
    """In-memory, locked, bounded: host -> its latest validated heartbeat. A restart forgets them (the next post
    restores each), which the view shows honestly as "no heartbeat yet".

    `protected` (optional) names the configured hosts; it is asked only when the board is over HEARTBEAT_HOSTS_MAX, and
    eviction then drops the least recently posted host that is NOT configured. Configured hosts are the owner's own
    list, so the board holds at most HEARTBEAT_HOSTS_MAX + their number. A failing `protected` protects nothing (the
    plain LRU), never an unbounded board. `generation` changes with every post (the live view's memo key)."""

    def __init__(self, protected: Callable[[], Any] | None = None):
        self._lock = threading.Lock()
        self._hosts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._protected = protected
        self.generation = 0

    def post(self, hb: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            self._hosts.pop(hb["host"], None)
            self._hosts[hb["host"]] = {**hb, "received_at": round(now, 3)}
            if len(self._hosts) > HEARTBEAT_HOSTS_MAX:
                try:
                    keep = set(self._protected()) if self._protected is not None else set()
                except Exception:
                    keep = set()
                for h in [h for h in self._hosts if h not in keep][:len(self._hosts) - HEARTBEAT_HOSTS_MAX]:
                    del self._hosts[h]
            self.generation += 1
        return {"ok": True, "host": hb["host"], "jobs": len(hb["jobs"]), "fresh_for_s": hb["ttl_s"]}

    def snapshot(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            for h in [h for h, v in self._hosts.items() if now - v["received_at"] > HEARTBEAT_KEEP_S]:
                del self._hosts[h]
            return {h: {**v, "jobs": [dict(j) for j in v["jobs"]]} for h, v in self._hosts.items()}


# ================================================================ sections
def _section(now: float, status: str = "ok", errors: list | None = None, **fields: Any) -> dict[str, Any]:
    return {"as_of": round(now, 3), "status": status, "errors": list(errors or []), **fields}


def _status(errors: list, has_data: bool) -> str:
    return "ok" if not errors else ("partial" if has_data else "error")


def _err(source: str, exc: BaseException | str) -> dict[str, str]:
    return {"source": source, "error": exc if isinstance(exc, str) else type(exc).__name__}


def _children(events: list[dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    """task id -> what its lifecycle events say, every field re-validated (the log is a file, read as untrusted)."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = e.get("ts")
        if not _num(ts) or not now - CHILD_WINDOW_S <= ts <= now:
            continue
        tid = office.clean(e.get("task_id"), office._RUN_ID_RE)
        ev = e.get("event")
        if tid is None or ev not in ("started", "finished", "cancelled"):
            continue
        c = out.setdefault(tid, {"task_id": tid, "started_at": None, "ended_at": None, "final": None, "duration_s": None,
                                 "workspace": None, "chrome": False})
        if ev == "started":
            c["started_at"] = float(ts)
        else:
            c["ended_at"] = float(ts)
            st = e.get("status") if ev == "finished" else "cancelled"
            c["final"] = st if st in _TASK_STATES else "failed"   # a finished event always carries the classified state
            if _num(e.get("duration_s")) and e["duration_s"] >= 0:
                c["duration_s"] = float(e["duration_s"])
            ws = office.clean(e.get("workspace_state"), _WS_STATE_RE)
            if ws:
                c["workspace"] = ws
        c["chrome"] = c["chrome"] or e.get("chrome") is True
        for field, rule in (("title", office._HEADER_RULES[office.TITLE_HEADER]), ("host", _HOST_RE),
                            ("lane", office._LANE_RE), ("model", office._MODEL_RE), ("client", _CLIENT_RE),
                            ("session", office._HEADER_RULES[office.SESSION_HEADER]),
                            ("project", office._HEADER_RULES[office.PROJECT_HEADER]),
                            ("device", office._HEADER_RULES[office.DEVICE_HEADER]),
                            ("origin_session", _ORIGIN_SESSION_RE),
                            ("origin_project", office._HEADER_RULES[office.PROJECT_HEADER]),
                            ("origin_device", office._HEADER_RULES[office.DEVICE_HEADER])):
            v = office.clean(e.get(field), rule)
            if v is not None:
                c[field] = v
    return out


def _office_workers(view: dict[str, Any]) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    """(floor or None for the lobby, worker) for every worker the Office view lists."""
    out = []
    for f in view.get("floors") or []:
        for w in f.get("workers") or []:
            out.append((f, w))
    for w in (view.get("lobby") or {}).get("workers") or []:
        out.append((None, w))
    return out


def _elsewhere(stubs: list | None, known: set[str], now: float) -> list[dict[str, Any]]:
    """Remote stubs re-validated (the tasks dir is a file tree, read as untrusted), minus any task the local log already
    shows, newest first, within STALE_RUNNING_S (no child runs longer: see the constant)."""
    out = []
    for s in stubs or []:
        if not isinstance(s, dict):
            continue
        tid, host, at = office.clean(s.get("task_id"), office._RUN_ID_RE), office.clean(s.get("host"), _HOST_RE), \
            s.get("created_at")
        if tid is None or host is None or tid in known or not _num(at) or not now - STALE_RUNNING_S <= at <= now:
            continue
        out.append({"task_id": tid, "host": host, "started_at": round(float(at), 3)})
    out.sort(key=lambda x: -x["started_at"])
    return out[:ELSEWHERE_MAX]


def tasks_section(events: list[dict[str, Any]], office_view: dict[str, Any] | None,
                  peek: Callable[[str], dict[str, Any] | None] | None, now: float,
                  errors: list | None = None, stubs: list | None = None) -> dict[str, Any]:
    errors = list(errors or [])
    by_run = {w.get("run_id"): w for _, w in _office_workers(office_view or {}) if w.get("kind") == "child_task"}
    kids = _children(events, now)
    rows = []
    peeks = 0
    for tid, c in kids.items():
        w = by_run.get(tid) or {}
        state, phase, ws = c["final"], None, c["workspace"]
        if state is None:
            p = None
            if peek is not None and peeks < PEEK_MAX:
                peeks += 1
                try:
                    p = peek(tid)
                except Exception as exc:            # one bad dir must not erase the section
                    errors.append(_err("task_dir", exc))
            state = "running"
            if p:
                ws = office.clean(p.get("workspace"), _WS_STATE_RE) or ws
                if p.get("ended"):
                    state, phase = "unknown", "ended on its host; no end event logged"
                elif p.get("phase") == "queued":
                    state, phase = "queued", "waiting for the host's Chrome"
            if state != "unknown" and c["started_at"] is not None and now - c["started_at"] > STALE_RUNNING_S:
                state, phase = "unknown", "no end event after 10 h"
        started = c["started_at"]
        if started is not None:
            elapsed = (c["ended_at"] if c["final"] else now) - started
        else:                                       # started before the window: the finished event's own duration
            elapsed = c["duration_s"]
            if c["duration_s"] is not None and c["ended_at"] is not None:
                started = c["ended_at"] - c["duration_s"]
        live_tool = w.get("tool") if state == "running" and w.get("status") == "working" else None
        rows.append({
            "task_id": tid, "title": c.get("title") or w.get("title"), "state": state, "phase": phase,
            "host": c.get("host"), "client": c.get("client"),
            "provider": w.get("provider"), "lane": c.get("lane") or w.get("lane"), "model": w.get("model") or c.get("model"),
            "tool": live_tool, "started_at": _r(started, 3), "ended_at": _r(c["ended_at"], 3),
            "elapsed_s": _r(max(0.0, elapsed)) if _num(elapsed) else None, "workspace": ws, "browser": c["chrome"],
            # Office tags only for routed tasks: a browser (login-mode) task's historical tags are not a floor's
            "session": None if c["chrome"] else c.get("session"),
            "project": None if c["chrome"] else c.get("project"),
            "device": None if c["chrome"] else c.get("device"),
            # who started it (any task, browser ones too): the orchestrating session's tag, "grokbot", or None
            "origin_session": c.get("origin_session"), "origin_project": c.get("origin_project"),
            "origin_device": c.get("origin_device"),
        })
    counts = {k: 0 for k in ("running", "queued", "succeeded", "failed", "cancelled", "unknown")}
    for r in rows:
        counts[r["state"]] += 1
    running = sorted([r for r in rows if r["state"] == "running"], key=lambda r: -(r["started_at"] or 0))
    queued = sorted([r for r in rows if r["state"] == "queued"], key=lambda r: r["started_at"] or 0)
    recent = sorted([r for r in rows if r["state"] not in ("running", "queued")],
                    key=lambda r: -(r["ended_at"] or r["started_at"] or 0))
    truncated = len(running) > RUNNING_MAX or len(queued) > RUNNING_MAX or len(recent) > RECENT_MAX
    away = _elsewhere(stubs, set(kids), now)
    return _section(now, _status(errors, bool(rows) or bool(away)), errors, scope=SCOPE, window_s=int(CHILD_WINDOW_S),
                    counts=counts, running=running[:RUNNING_MAX], queued=queued[:RUNNING_MAX],
                    recent=recent[:RECENT_MAX], truncated=truncated, elsewhere=away, **origins_of(rows))


def origins_of(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The tasks grouped by the main session that started them, from every row in the window (not the capped lists):
    {origins: [{session, project, device, running, queued, finished, task_ids}], origins_truncated}. `session` None is
    the unattributed group (tasks from before origins were recorded, or a server that knows no session tag). Groups
    with a running or queued task first, the unattributed one last among equals, then by their newest task."""
    groups: dict[Any, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda r: -(r.get("started_at") or 0)):
        g = groups.setdefault(r.get("origin_session"), {
            "session": r.get("origin_session"), "project": None, "device": None, "running": 0, "queued": 0,
            "finished": 0, "task_ids": [], "_newest": r.get("started_at") or 0})
        g["project"] = g["project"] or r.get("origin_project")
        g["device"] = g["device"] or r.get("origin_device")
        st = r.get("state")
        g["running" if st == "running" else "queued" if st == "queued" else "finished"] += 1
        if len(g["task_ids"]) < ORIGIN_IDS_MAX:
            g["task_ids"].append(r["task_id"])
    out = sorted(groups.values(), key=lambda g: (not (g["running"] or g["queued"]), g["session"] is None, -g["_newest"]))
    for g in out:
        del g["_newest"]
    return {"origins": out[:ORIGINS_MAX], "origins_truncated": len(out) > ORIGINS_MAX}


def link_origins(conv: dict[str, Any], tasks: dict[str, Any]) -> None:
    """Each orchestrator item gets `tasks`: {running, queued, finished, task_ids} of the tasks its session started in
    the window (zeros when none; null when the tasks section could not be read)."""
    by = {g.get("session"): g for g in tasks.get("origins") or [] if g.get("session")}
    known = tasks.get("status") != "error"
    for it in conv.get("items") or []:
        if it.get("kind") != "orchestrator":
            continue
        g = (by.get(it.get("session")) if it.get("session") else None) or {}
        it["tasks"] = {"running": g.get("running", 0), "queued": g.get("queued", 0),
                       "finished": g.get("finished", 0), "task_ids": list(g.get("task_ids") or [])} if known else None


def for_session(snap: dict[str, Any], session: str) -> dict[str, Any]:
    """GET /v1/cortados/live?session=<tag>: the snapshot narrowed to one orchestrating session: its tasks (running,
    queued, recent, by origin_session), its origin group, and the conversations on its floor. Counts, overview and
    the other sections stay fleet-wide. `session` must already be valid (valid_session)."""
    t = dict(snap.get("tasks") or {})
    for k in ("running", "queued", "recent"):
        t[k] = [r for r in t.get(k) or [] if r.get("origin_session") == session]
    t["origins"] = [g for g in t.get("origins") or [] if g.get("session") == session]
    c = dict(snap.get("conversations") or {})
    c["items"] = [it for it in c.get("items") or [] if it.get("session") == session]
    return {**snap, "session_filter": session, "tasks": t, "conversations": c}


def valid_session(v: Any) -> str | None:
    """A ?session= value the filter accepts (an Office tag or a named origin), else None."""
    return v if isinstance(v, str) and _ORIGIN_SESSION_RE.fullmatch(v) else None


def _backend_items(backend: list, view: dict[str, Any], now: float) -> tuple[list[dict[str, Any]], set[str]]:
    """One item per conversation the provider holds open (Registry.live_conversations), joined with the Office board by
    its office key, and the office run ids / main-loop sessions those conversations cover. Every field is re-validated
    here: these records come from in-memory sessions whose models and lanes the routes chose."""
    workers = {w.get("run_id"): (f, w) for f, w in _office_workers(view) if w.get("run_id")}
    floors = {f.get("session"): f for f in view.get("floors") or [] if f.get("session")}
    items, covered = [], set()
    for c in backend or []:
        if not isinstance(c, dict):
            continue
        key = c.get("key") if isinstance(c.get("key"), str) else ""
        kind_of, _, ident = key.partition(":")
        floor, w = None, {}
        if kind_of in ("r", "c") and ident in workers:
            floor, w = workers[ident]
            covered.add(ident)
        elif kind_of == "main" and ident in floors:
            floor = floors[ident]
            covered.add("main:" + ident)
            o = floor.get("orchestrator") or {}
            w = {"tool": o.get("tool"), "status": o.get("status")}   # the main loop's tool, as the floor knows it
        if kind_of == "main":
            kind = "orchestrator"
        elif kind_of == "r":
            kind = w.get("kind") or "subagent"
        else:                                       # untagged, no run id: a plain conversation (README "Live view")
            kind = "conversation"
        busy, waiting = c.get("busy") is True, c.get("awaiting_tool") is True
        status = "working" if busy or waiting else "idle"
        opened = c.get("opened_at") if _num(c.get("opened_at")) else None
        items.append({
            "kind": kind, "run_id": office.clean(ident, office._RUN_ID_RE) if kind_of == "r" else
            office.clean(c.get("ref"), office._RUN_ID_RE),
            "session": (floor or {}).get("session"), "project": (floor or {}).get("project"),
            "device": (floor or {}).get("device"),
            "provider": c.get("provider") if c.get("provider") in office.PROVIDERS else w.get("provider"),
            "lane": office.clean(c.get("lane"), office._LANE_RE) or w.get("lane"),
            "model": office.clean(c.get("model"), office._MODEL_RE) or w.get("model"),
            "status": status, "tool": w.get("tool") if status == "working" and w.get("status") == "working" else None,
            "phase": "model turn" if busy else ("client running a tool" if waiting else "waiting for the client"),
            "source": "provider", "title": w.get("title"), "started_at": _r(opened, 3),
            "last_activity": _r(c.get("last_used"), 3)})
    return items, covered


def conversations_section(view: dict[str, Any], backend_sessions: int | None, now: float,
                          backend: list | None = None, errors: list | None = None) -> dict[str, Any]:
    """In-flight routed work. With `backend` (the Registry's open conversations), one item per open conversation,
    child tasks' included; then each floor's orchestrator known from a heartbeat (a main session that does not route
    itself), and Office workers that are working or idle but hold no open conversation (one-shot calls). Without it,
    the Office view alone (orchestrators and non-child workers), as before."""
    items, covered = _backend_items(backend, view, now) if backend is not None else ([], set())
    for f in view.get("floors") or []:
        o = f.get("orchestrator")
        if "main:" + str(f.get("session")) in covered:
            continue
        if isinstance(o, dict) and o.get("status") in ("working", "idle"):
            items.append({"kind": "orchestrator", "run_id": None, "session": f.get("session"),
                          "project": f.get("project"), "device": f.get("device"), "provider": None, "lane": None,
                          "model": o.get("model"), "status": o.get("status"), "tool": o.get("tool"), "phase": None,
                          "source": o.get("source"), "title": None, "started_at": f.get("since"),
                          "last_activity": o.get("last_seen")})
    for f, w in _office_workers(view):
        if w.get("run_id") in covered or w.get("status") not in ("working", "idle"):
            continue
        if w.get("kind") == "child_task":
            continue                                # a child holds a conversation while it runs; its row is the task's
        items.append({"kind": w.get("kind") or "subagent", "run_id": w.get("run_id"),
                      "session": (f or {}).get("session"), "project": (f or {}).get("project"),
                      "device": (f or {}).get("device"), "provider": w.get("provider"), "lane": w.get("lane"),
                      "model": w.get("model"), "status": w.get("status"), "tool": w.get("tool"), "phase": None,
                      "source": "routed", "title": w.get("title"), "started_at": w.get("started_at"),
                      "last_activity": None})
    for it in items:
        it["elapsed_s"] = _r(max(0.0, now - it["started_at"])) if _num(it.get("started_at")) else None
    items.sort(key=lambda it: (it["status"] != "working", -(it.get("started_at") or 0)))
    errors = [e for e in view.get("errors") or [] if isinstance(e, dict)] + list(errors or [])
    return _section(now, _status(errors, True), errors, backend_sessions=backend_sessions,
                    active=len(items), working=sum(1 for it in items if it["status"] == "working"),
                    floors=len(view.get("floors") or []), items=items[:CONVERSATIONS_MAX],
                    hidden=max(0, len(items) - CONVERSATIONS_MAX))


_CALL_KEYS = ("ts", "task_id", "session_id", "routing_kind", "band", "lane", "model", "actual_model", "elapsed_ms",
              "degraded", "s1_reason", "request_chars", "response_chars")


_LOG_OPS = ("log_route", "log_execution", "log_outcome")
_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,63}")


def route_log_errors(stats: Any) -> dict[str, Any] | None:
    """router_db.stats() re-validated: counts, an exception type name, a time and the op; nothing else (#99)."""
    if not isinstance(stats, dict):
        return None
    count = lambda k: stats.get(k) if isinstance(stats.get(k), int) and not isinstance(stats.get(k), bool) and stats.get(k) >= 0 else 0  # noqa: E731
    t = stats.get("last_error_type")
    return {"count": count("count"), "lost": count("lost"), "recovered": count("recovered"),
            "last_error_type": t if isinstance(t, str) and _TYPE_RE.fullmatch(t) else None,
            "last_error_at": stats.get("last_error_at") if _num(stats.get("last_error_at")) else None,
            "last_op": stats.get("last_op") if stats.get("last_op") in _LOG_OPS else None}


def routed_section(view: dict[str, Any], now: float, log_errors: dict[str, Any] | None = None) -> dict[str, Any]:
    calls = [{k: t.get(k) for k in _CALL_KEYS} for t in (view.get("tasks") or [])[:ROUTED_MAX]]
    errors = [e for e in view.get("errors") or [] if isinstance(e, dict)]
    return _section(now, _status(errors, bool(calls)), errors, calls=calls, totals=view.get("totals") or {},
                    row_limit=view.get("row_limit"), more=len(view.get("tasks") or []) > ROUTED_MAX,
                    route_log_errors=log_errors)


def _general(w: dict[str, Any]) -> bool:
    return w.get("pool") in (None, "all") and not w.get("stale") and _num(w.get("left_pct"))


def lane_enabled(lane: Any) -> bool:
    """Whether the router can send work to `lane` at all, by the checks routing itself makes: the anthropic-api credit
    lane only with a configured key (the provider refuses every api/ pin without one: server._serve_api_lane), any lane
    only with a context ceiling above 0 (core.resolve's fit gate refuses every turn on a 0 ceiling: antigravity's
    containment). A disabled lane's reading (no key: 0 % of a $0 budget) never stands for the fleet's floor."""
    from cortados_router import api_credits, core
    if not isinstance(lane, str):
        return False
    base = lane.split("#", 1)[0]
    if base == "anthropic-api":
        return bool(api_credits.configured_orgs())
    return core.lane_ceiling(lane) > 0


def subscriptions_section(u: dict[str, Any], now: float,
                          enabled: Callable[[Any], bool] | None = None) -> dict[str, Any]:
    """`enabled` (lane -> bool, e.g. lane_enabled) keeps disabled lanes out of `lowest`; they are still listed. One
    that raises is reported (source "lanes") and its lane is kept out too: an unknown lane is not the floor."""
    lanes = []
    errors: list = []
    for l in u.get("lanes") or []:
        wins = []
        for w in l.get("windows") or []:
            if not isinstance(w, dict):
                continue
            # hours to the reset from the provider's clock (reset_epoch), else the sampler's own figure; never the
            # local-time string, which reads differently in every time zone
            r = w.get("reset_epoch")
            hours = round((r - now) / 3600, 1) if _num(r) else w.get("resets_in_h")
            wins.append({"window": w.get("window"), "pool": w.get("pool"), "left_pct": w.get("left_pct"),
                         "resets_in_h": hours if _num(hours) else None, "stale": bool(w.get("stale"))})
        lanes.append({"lane": l.get("lane"), "label": l.get("label"), "account": l.get("account"),
                      "left_pct": l.get("left_pct"), "stale": bool(l.get("stale")), "error": l.get("error"),
                      "sample_age_s": l.get("sample_age_s"), "shared_with": l.get("shared_with") or [],
                      "pools": l.get("pools") or {}, "windows": wins})
    lowest = None
    for l in lanes:
        if l["stale"] or l["error"] or not _num(l["left_pct"]):
            continue                                # a stale or failed reading never stands for the fleet's floor
        if enabled is not None:
            try:
                on = enabled(l["lane"])
            except Exception as exc:
                errors.append(_err("lanes", exc))
                on = False
            if not on:
                continue
        if lowest is None or l["left_pct"] < lowest["left_pct"]:
            # the window the headline came from: the fresh one nearest it, a general (pool "all") one on a tie
            w = min((w for w in l["windows"] if not w.get("stale") and _num(w.get("left_pct"))),
                    key=lambda w: (abs(w["left_pct"] - l["left_pct"]), not _general(w)), default=None)
            lowest = {"lane": l["lane"], "label": l["label"], "left_pct": l["left_pct"],
                      "window": (w or {}).get("window")}
    errors = [e for e in u.get("errors") or [] if isinstance(e, dict) and str(e.get("source", "")).startswith("scarcity")] \
        + errors
    as_of = u.get("generated_at") if _num(u.get("generated_at")) else now
    return _section(as_of, _status(errors, bool(lanes)), errors, lanes=lanes, lowest=lowest)


def s1_section(u: dict[str, Any] | None, now: float, errors: list | None = None) -> dict[str, Any]:
    s = (u or {}).get("s1")
    if not isinstance(s, dict):
        return _section(now, "unknown", errors or [_err("s1", "not probed")], available=None, http_status=None,
                        latency_ms=None, reason=None)
    errs = [e for e in (u or {}).get("errors") or [] if isinstance(e, dict) and e.get("source") == "s1"]
    return _section(now, "up" if s.get("available") else "down", errs, available=bool(s.get("available")),
                    http_status=s.get("status"), latency_ms=s.get("latency_ms"), reason=s.get("reason"))


def _slug(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")[:32]
    return office.clean(s, _HOST_RE)


def known_hosts(env: dict | None = None) -> list[str]:
    raw = (os.environ if env is None else env).get(HOSTS_ENV)
    names = [_slug(x) for x in raw.split(",")] if raw else list(DEFAULT_HOSTS)
    return list(dict.fromkeys(n for n in names if n))


def _match(short: str | None, aliases: dict[str, set[str]]) -> str | None:
    """The host name a machine's short hostname belongs to: an exec host's `hostnames` alias, else a name equal to
    the hostname or one of its words (forests-mac-studio -> studio). None when no name or several match."""
    if not short:
        return None
    s = short.lower()
    for name, al in aliases.items():
        if s == name or s in al:
            return name
    words = set(re.split(r"[^a-z0-9]+", s))
    hits = [n for n in aliases if n in words]
    return hits[0] if len(hits) == 1 else None


_HW_PICK = ("cpu_busy_pct", "gpu_busy_pct", "mem_available_bytes", "mem_total_bytes", "chip")


def _merge_hardware(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """One host's hardware readings (sample cards, its heartbeat's block) as one card, field by field: each field from
    the freshest reading that has it, so a fresher but sparse heartbeat never hides what an older sample card knows.
    `age_s` and `source` are the freshest reading's; `filled_by` names the other source(s) that supplied a field."""
    ordered = sorted(readings, key=lambda r: r["age_s"] if _num(r.get("age_s")) else float("inf"))
    out = dict(ordered[0])
    fillers: list[str] = []
    for r in ordered[1:]:
        for k, v in r.items():
            if k in ("age_s", "source") or v is None or out.get(k) is not None:
                continue
            out[k] = v
            if r["source"] not in fillers:
                fillers.append(r["source"])
    out["filled_by"] = "+".join(fillers) or None
    return out


def hosts_section(u: dict[str, Any] | None, heartbeats: dict[str, dict[str, Any]], *, names: list[str],
                  exec_hosts: dict[str, list[str]] | None, exec_default: str | None, self_host: str | None,
                  now: float, errors: list | None = None, self_name: str | None = None) -> dict[str, Any]:
    """`self_name` (CORTADOS_LIVE_SELF) names the provider host outright when its hostname matches no known name; a
    hardware card whose hostname is the provider's own is then shown under that name. A host's hardware is the
    freshest of its sample card (hardware/<host>.json) and its fresh heartbeat's `hardware` block."""
    errors = list(errors or [])
    aliases: dict[str, set[str]] = {n: set() for n in names}
    for n, al in (exec_hosts or {}).items():
        aliases.setdefault(n, set()).update(a.lower() for a in al if isinstance(a, str))
    for h in heartbeats:
        aliases.setdefault(h, set())
    items: dict[str, dict[str, Any]] = {}

    def item(name: str) -> dict[str, Any]:
        return items.setdefault(name, {"name": name, "roles": [], "heartbeat_state": "none", "heartbeat": None,
                                       "hardware": None})
    for n in aliases:
        item(n)
    # Only a KNOWN name is published: the raw hostname slug can carry the owner's name (forests-macbook-air), and a
    # live:read reader is not meant to learn it. An unmatched provider host is null, said as such by the readers.
    me = _match(self_host, aliases)
    if me is None and self_name in aliases:
        me = self_name
    if me:
        item(me)["roles"].append("provider")
    for n in exec_hosts or {}:
        item(n)["roles"].append("default exec host" if n == exec_default else "exec host")
    unmatched = 0
    cands: dict[str, list[dict[str, Any]]] = {}     # every hardware reading per known host, merged below
    for card in (u or {}).get("hardware") or []:
        data = card.get("data") if isinstance(card, dict) else None
        if not isinstance(data, dict):
            continue
        host = data.get("host") if isinstance(data.get("host"), str) else None
        # A known name only, as for the provider host above: a card whose hostname matches none is dropped and counted
        # (below), never shown under its raw hostname (which can carry the owner's name).
        name = _match(host, aliases)
        if not name and host and self_host and host.lower() == self_host.lower():
            name = me                               # the provider's own sample, under the provider's known name
        if not name:
            unmatched += 1
            continue
        load = data.get("load_avg")
        hw = {k: data.get(k) for k in _HW_PICK if not isinstance(data.get(k), dict)}
        hw["load_1m"] = load[0] if isinstance(load, list) and load and _num(load[0]) else None
        hw["age_s"] = card.get("age_s")
        hw["disk_free_pct"] = None
        hw["source"] = "sample"
        cands.setdefault(name, []).append(hw)
    for h, hb in heartbeats.items():
        age = max(0.0, now - hb["received_at"])
        stale = age > hb["ttl_s"]
        it = item(h)
        it["heartbeat_state"] = "stale" if stale else "fresh"
        it["heartbeat"] = {"received_at": hb["received_at"], "age_s": round(age, 1), "ttl_s": hb["ttl_s"],
                           "stale": stale, "jobs": hb["jobs"], "s1": hb.get("s1")}
        hh = hb.get("hardware")
        if isinstance(hh, dict) and not stale:
            at = hh.get("sampled_at") if _num(hh.get("sampled_at")) else hb["received_at"]
            hw = {k: hh.get(k) for k in _HW_PICK if k != "chip"}
            hw.update(chip=hh.get("chip"), load_1m=hh.get("load_1m"), age_s=round(max(0.0, now - at), 1),
                      disk_free_pct=hh.get("disk_free_pct"), source="heartbeat")
            cands.setdefault(h, []).append(hw)
    for name, readings in cands.items():
        item(name)["hardware"] = _merge_hardware(readings)
    errors += [e for e in (u or {}).get("errors") or []
               if isinstance(e, dict) and str(e.get("source", "")).startswith("hardware") and e.get("error") != "absent"]
    if unmatched:
        # Counted, never named: add the machine's hostname to its exec-hosts.json `hostnames` (or CORTADOS_LIVE_HOSTS).
        errors.append(_err("hardware", f"{unmatched} hardware sample(s) match no known host name"))
    return _section(now, _status(errors, bool(items)), errors, provider_host=me, items=list(items.values()),
                    heartbeat_path=HEARTBEAT_PATH)


# ================================================================ the snapshot
def collect(*, now: float, child_events: Callable[[], list[dict[str, Any]]],
            office_view: Callable[[list[dict[str, Any]]], dict[str, Any]], tasks_view: Callable[[], dict[str, Any]],
            usage_view: Callable[[], dict[str, Any]], heartbeats: Callable[[], dict[str, dict[str, Any]]],
            backend_sessions: int | None = None, peek: Callable[[str], dict[str, Any] | None] | None = None,
            exec_hosts: Callable[[], tuple[dict[str, list[str]], str | None]] | None = None,
            self_host: str | None = None, host_names: list[str] | None = None,
            remote_stubs: Callable[[], list] | None = None,
            lane_enabled: Callable[[Any], bool] | None = None,
            conversations: Callable[[], list] | None = None, self_name: str | None = None,
            route_log: Callable[[], dict[str, Any]] = router_db.stats) -> dict[str, Any]:
    """The snapshot. Each input is a callable, run under its own guard: one that raises turns ITS section into
    status "error" with the exception's type (never its text: an OSError carries a path), the rest stand."""
    def guard(source: str, fn: Callable[[], Any], errs: list) -> Any:
        try:
            return fn()
        except Exception as exc:
            errs.append(_err(source, exc))
            return None

    child_errs: list = []
    events = guard("child-tasks.jsonl", child_events, child_errs)
    office_errs: list = []
    ov = guard("office", lambda: office_view(office.recent_child_events(events or [], now)), office_errs)
    stub_errs: list = []
    stubs = guard("task stubs", remote_stubs, stub_errs) if remote_stubs is not None else None
    tasks = tasks_section(events or [], ov, peek, now, child_errs + office_errs + stub_errs, stubs) \
        if events is not None \
        else _section(now, "error", child_errs + office_errs + stub_errs, scope=SCOPE, window_s=int(CHILD_WINDOW_S),
                      counts=None, running=[], queued=[], recent=[], truncated=False,
                      elsewhere=_elsewhere(stubs, set(), now), origins=[], origins_truncated=False)
    conv_errs: list = []
    backend = guard("conversations", conversations, conv_errs) if conversations is not None else None
    if isinstance(backend, list):
        backend_sessions = len(backend)             # the count from the same snapshot as the list it heads
    if ov is not None or backend is not None:    # the open conversations stand without the board (partial)
        conv = conversations_section(ov if ov is not None else {"errors": office_errs}, backend_sessions, now,
                                     backend, conv_errs)
    else:
        conv = _section(now, "error", office_errs + conv_errs, backend_sessions=backend_sessions, active=None,
                        working=None, floors=None, items=[], hidden=0)
    link_origins(conv, tasks)
    tv_errs: list = []
    tv = guard("router.db", tasks_view, tv_errs)
    # this process's failed router.db writes (#99): shown even when the read side failed, which is when they matter most
    rl_errs: list = []
    rl = route_log_errors(guard("route log", route_log, rl_errs))
    routed = routed_section(tv, now, rl) if tv is not None \
        else _section(now, "error", tv_errs, calls=[], totals={}, row_limit=None, more=False, route_log_errors=rl)
    routed["errors"] += rl_errs
    u_errs: list = []
    u = guard("usage", usage_view, u_errs)
    subs = subscriptions_section(u, now, lane_enabled) if u is not None else _section(now, "error", u_errs, lanes=[], lowest=None)
    s1 = s1_section(u, now, u_errs or None)
    hb_errs: list = []
    beats = guard("heartbeats", heartbeats, hb_errs) or {}
    ex_errs: list = []
    ex = guard("exec-hosts", exec_hosts, ex_errs) if exec_hosts is not None else None
    ex_hosts, ex_default = ex if isinstance(ex, tuple) else ({}, None)
    hosts = hosts_section(u, beats, names=host_names if host_names is not None else known_hosts(),
                          exec_hosts=ex_hosts, exec_default=ex_default, self_host=self_host, now=now,
                          errors=hb_errs + ex_errs + (u_errs if u is None else []),
                          self_name=self_name if self_name is not None else _slug(os.environ.get(SELF_ENV)))
    counts = tasks.get("counts") or {}
    overview = {
        "running_tasks": counts.get("running") if tasks["status"] != "error" else None,
        "queued_tasks": counts.get("queued") if tasks["status"] != "error" else None,
        "active_conversations": conv.get("active"), "working_conversations": conv.get("working"),
        "s1": s1["status"], "s1_latency_ms": s1.get("latency_ms"),
        "lowest_subscription": subs.get("lowest"),
        "route_log_errors": (rl or {}).get("count"),
        "hosts_with_heartbeat": sum(1 for h in hosts["items"] if h["heartbeat_state"] == "fresh"),
        "errors": sum(len(s["errors"]) for s in (tasks, conv, routed, subs, s1, hosts)),
    }
    return {"version": VERSION, "generated_at": round(now, 3), "overview": overview, "tasks": tasks,
            "conversations": conv, "routed": routed, "subscriptions": subs, "s1": s1, "hosts": hosts}


class Memo:
    """A MEMO_S memo with single flight for the encoded snapshot (as usage.collect has for its view): concurrent GETs
    wait for the one collect in progress and share its bytes. `key` names what must invalidate it early (the heartbeat
    board's generation, so a post shows on the next GET). A collect that raises is not memoised."""

    def __init__(self, ttl_s: float = MEMO_S, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._clock = ttl_s, clock
        self._lock = threading.Lock()               # guards _hit; never held across a collect
        self._flight = threading.Lock()             # held across a miss
        self._hit: tuple[float, Any, bytes] | None = None

    def _fresh(self, key: Any) -> bytes | None:
        with self._lock:
            h = self._hit
            return h[2] if h and h[1] == key and self._clock() - h[0] < self._ttl else None

    def get(self, key: Any, build: Callable[[], bytes]) -> bytes:
        body = self._fresh(key)
        if body is not None:
            return body
        with self._flight:
            body = self._fresh(key)
            if body is None:
                body = build()
                with self._lock:
                    self._hit = (self._clock(), key, body)
        return body
