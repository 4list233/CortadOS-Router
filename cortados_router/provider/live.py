"""The live view: GET /v1/cortados/live, one metadata-only snapshot of everything CortadOS is running right now, for the
browser dashboard (GET /dashboard) and the cortados-pulse pane (issue #73).

It composes views that already exist rather than re-deriving them:
  - tasks          start_task children (running / queued / last 24 h) from $CORTADOS_HOME/child-tasks.jsonl, read once
                   through office.read_child_events (bounded tail), with the routed model/lane the Office board saw for
                   the same run id, and a read-only peek at a running task's dir on this machine (child_tasks.peek_local:
                   queued phase, workspace state). A remote task is never asked over ssh: a 5 s poll must stay local.
  - conversations  in-flight routed work: the Office view's floors (orchestrators) and workers, minus child tasks.
  - routed         the latest routing decisions, from tasks.collect (router.db, read-only, row-limited).
  - subscriptions  every lane's % left per window, from usage.collect_live (scarcity.json read strictly: cache only,
                   never a vendor CLI), and the lowest general window across lanes.
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

A post replaces that host's previous one. It is shown fresh for ttl_s (default 900, 60..86400), then stale, and is
forgotten after 24 h. A malformed heartbeat is refused with 400 naming the bad field (never echoing its value), a body
over HEARTBEAT_MAX_BYTES with 413. docs/PROVIDER-PLAN.md ("Live view") shows a sender a Studio job could run.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from cortados_router.provider import office

VERSION = 1
LIVE_PATH = "/v1/cortados/live"
HEARTBEAT_PATH = "/v1/cortados/live/heartbeat"
# ---- the window and caps, each with its reason ----
# The dashboard's "recent" is the owner's day: what finished in the last 24 h.
CHILD_WINDOW_S = 24 * 3600.0
# A child is queued for the Chrome lock up to 6 h (child_tasks.CHROME_QUEUE_MAX_S) and then runs up to 3 h
# (CHILD_MAX_S_DEFAULT): a start with no end event after 10 h is not shown as running, it is "unknown" and said so.
STALE_RUNNING_S = 10 * 3600.0
RUNNING_MAX = 50            # children running at once: a handful; 50 keeps a runaway fan-out visible but bounded
RECENT_MAX = 40             # finished in 24 h, newest first: a day's worth at the owner's pace
PEEK_MAX = 64               # task-dir peeks per snapshot (a few stats each): bounds the poll's disk work
CONVERSATIONS_MAX = 64      # in-flight conversations listed; the rest are counted in `hidden`
ROUTED_MAX = 25             # latest routing decisions shown; tasks.collect reads at most its own ROW_LIMIT (200)
# ---- heartbeat rules ----
HEARTBEAT_MAX_BYTES = 8192  # 16 jobs x ~300 bytes is under 5 KiB; a larger body is not a heartbeat
HEARTBEAT_HOSTS_MAX = 16    # the fleet is three machines; LRU past this
HEARTBEAT_KEEP_S = 24 * 3600.0
TTL_DEFAULT_S = 900         # a job loop posting every 5 min misses two posts before it is shown stale
TTL_MIN_S, TTL_MAX_S = 60, 86400
JOBS_MAX = 16
JOB_STATES = ("running", "idle", "ok", "scheduled", "failed", "stopped")
TIME_SKEW_S = 30 * 86400.0  # job timestamps within a month of the provider's clock; anything else is not a time
DEFAULT_HOSTS = ("air", "studio", "pro")
HOSTS_ENV = "CORTADOS_LIVE_HOSTS"

_HOST_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")       # exec_hosts.HOST_NAME_RE: one slug per machine everywhere
_JOB_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}")
_SUMMARY_RE = re.compile(r"[\x20-\x7e]{1,80}")       # office's title rule: printable ASCII, 80 chars
_CLIENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_WS_STATE_RE = re.compile(r"[a-z_-]{1,24}")
_TASK_STATES = ("succeeded", "failed", "cancelled")   # child_tasks.TERMINAL_STATES
_HB_KEYS = {"host", "jobs", "ttl_s"}
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
    return {"host": host, "ttl_s": int(ttl), "jobs": out_jobs}, None


class HeartbeatBoard:
    """In-memory, locked, bounded: host -> its latest validated heartbeat. A restart forgets them (the next post
    restores each), which the view shows honestly as "no heartbeat yet"."""

    def __init__(self):
        self._lock = threading.Lock()
        self._hosts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()

    def post(self, hb: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            self._hosts.pop(hb["host"], None)
            self._hosts[hb["host"]] = {**hb, "received_at": round(now, 3)}
            while len(self._hosts) > HEARTBEAT_HOSTS_MAX:
                self._hosts.popitem(last=False)
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
                            ("device", office._HEADER_RULES[office.DEVICE_HEADER])):
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


def tasks_section(events: list[dict[str, Any]], office_view: dict[str, Any] | None,
                  peek: Callable[[str], dict[str, Any] | None] | None, now: float,
                  errors: list | None = None) -> dict[str, Any]:
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
        })
    counts = {k: 0 for k in ("running", "queued", "succeeded", "failed", "cancelled", "unknown")}
    for r in rows:
        counts[r["state"]] += 1
    running = sorted([r for r in rows if r["state"] == "running"], key=lambda r: -(r["started_at"] or 0))
    queued = sorted([r for r in rows if r["state"] == "queued"], key=lambda r: r["started_at"] or 0)
    recent = sorted([r for r in rows if r["state"] not in ("running", "queued")],
                    key=lambda r: -(r["ended_at"] or r["started_at"] or 0))
    truncated = len(running) > RUNNING_MAX or len(queued) > RUNNING_MAX or len(recent) > RECENT_MAX
    return _section(now, _status(errors, bool(rows)), errors, window_s=int(CHILD_WINDOW_S), counts=counts,
                    running=running[:RUNNING_MAX], queued=queued[:RUNNING_MAX], recent=recent[:RECENT_MAX],
                    truncated=truncated)


def conversations_section(view: dict[str, Any], backend_sessions: int | None, now: float) -> dict[str, Any]:
    """In-flight routed work from the Office view: each floor's orchestrator and every routed worker that is working or
    idle (child tasks are in the tasks section). Finished workers are left to the Office view."""
    items = []
    for f in view.get("floors") or []:
        o = f.get("orchestrator")
        if isinstance(o, dict) and o.get("status") in ("working", "idle"):
            items.append({"kind": "orchestrator", "run_id": None, "session": f.get("session"),
                          "project": f.get("project"), "device": f.get("device"), "provider": None, "lane": None,
                          "model": o.get("model"), "status": o.get("status"), "tool": o.get("tool"),
                          "source": o.get("source"), "started_at": f.get("since"), "last_activity": o.get("last_seen")})
    for f, w in _office_workers(view):
        if w.get("kind") == "child_task" or w.get("status") not in ("working", "idle"):
            continue
        items.append({"kind": w.get("kind") or "subagent", "run_id": w.get("run_id"),
                      "session": (f or {}).get("session"), "project": (f or {}).get("project"),
                      "device": (f or {}).get("device"), "provider": w.get("provider"), "lane": w.get("lane"),
                      "model": w.get("model"), "status": w.get("status"), "tool": w.get("tool"), "source": "routed",
                      "title": w.get("title"), "started_at": w.get("started_at"), "last_activity": None})
    for it in items:
        it["elapsed_s"] = _r(max(0.0, now - it["started_at"])) if _num(it.get("started_at")) else None
    items.sort(key=lambda it: (it["status"] != "working", -(it.get("started_at") or 0)))
    errors = [e for e in view.get("errors") or [] if isinstance(e, dict)]
    return _section(now, _status(errors, True), errors, backend_sessions=backend_sessions,
                    active=len(items), working=sum(1 for it in items if it["status"] == "working"),
                    floors=len(view.get("floors") or []), items=items[:CONVERSATIONS_MAX],
                    hidden=max(0, len(items) - CONVERSATIONS_MAX))


_CALL_KEYS = ("ts", "task_id", "session_id", "routing_kind", "band", "lane", "model", "actual_model", "elapsed_ms",
              "degraded", "s1_reason", "request_chars", "response_chars")


def routed_section(view: dict[str, Any], now: float) -> dict[str, Any]:
    calls = [{k: t.get(k) for k in _CALL_KEYS} for t in (view.get("tasks") or [])[:ROUTED_MAX]]
    errors = [e for e in view.get("errors") or [] if isinstance(e, dict)]
    return _section(now, _status(errors, bool(calls)), errors, calls=calls, totals=view.get("totals") or {},
                    row_limit=view.get("row_limit"), more=len(view.get("tasks") or []) > ROUTED_MAX)


def _general(w: dict[str, Any]) -> bool:
    return w.get("pool") in (None, "all") and not w.get("stale") and _num(w.get("left_pct"))


def subscriptions_section(u: dict[str, Any], now: float) -> dict[str, Any]:
    lanes = []
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
        if lowest is None or l["left_pct"] < lowest["left_pct"]:
            # the window the headline came from: the fresh one nearest it, a general (pool "all") one on a tie
            w = min((w for w in l["windows"] if not w.get("stale") and _num(w.get("left_pct"))),
                    key=lambda w: (abs(w["left_pct"] - l["left_pct"]), not _general(w)), default=None)
            lowest = {"lane": l["lane"], "label": l["label"], "left_pct": l["left_pct"],
                      "window": (w or {}).get("window")}
    errors = [e for e in u.get("errors") or [] if isinstance(e, dict) and str(e.get("source", "")).startswith("scarcity")]
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


def hosts_section(u: dict[str, Any] | None, heartbeats: dict[str, dict[str, Any]], *, names: list[str],
                  exec_hosts: dict[str, list[str]] | None, exec_default: str | None, self_host: str | None,
                  now: float, errors: list | None = None) -> dict[str, Any]:
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
    me = _match(self_host, aliases) or _slug(self_host)
    if me:
        item(me)["roles"].append("provider")
    for n in exec_hosts or {}:
        item(n)["roles"].append("default exec host" if n == exec_default else "exec host")
    for card in (u or {}).get("hardware") or []:
        data = card.get("data") if isinstance(card, dict) else None
        if not isinstance(data, dict):
            continue
        host = data.get("host") if isinstance(data.get("host"), str) else None
        name = _match(host, aliases) or _slug(host) or _slug(card.get("file"))
        if not name:
            continue
        load = data.get("load_avg")
        hw = {k: data.get(k) for k in _HW_PICK if not isinstance(data.get(k), dict)}
        hw["load_1m"] = load[0] if isinstance(load, list) and load and _num(load[0]) else None
        hw["age_s"] = card.get("age_s")
        it = item(name)
        if it["hardware"] is None or (_num(hw["age_s"]) and hw["age_s"] < (it["hardware"].get("age_s") or 1e18)):
            it["hardware"] = hw
    for h, hb in heartbeats.items():
        age = max(0.0, now - hb["received_at"])
        stale = age > hb["ttl_s"]
        it = item(h)
        it["heartbeat_state"] = "stale" if stale else "fresh"
        it["heartbeat"] = {"received_at": hb["received_at"], "age_s": round(age, 1), "ttl_s": hb["ttl_s"],
                           "stale": stale, "jobs": hb["jobs"]}
    errors += [e for e in (u or {}).get("errors") or []
               if isinstance(e, dict) and str(e.get("source", "")).startswith("hardware") and e.get("error") != "absent"]
    return _section(now, _status(errors, bool(items)), errors, provider_host=me, items=list(items.values()),
                    heartbeat_path=HEARTBEAT_PATH)


# ================================================================ the snapshot
def collect(*, now: float, child_events: Callable[[], list[dict[str, Any]]],
            office_view: Callable[[list[dict[str, Any]]], dict[str, Any]], tasks_view: Callable[[], dict[str, Any]],
            usage_view: Callable[[], dict[str, Any]], heartbeats: Callable[[], dict[str, dict[str, Any]]],
            backend_sessions: int | None = None, peek: Callable[[str], dict[str, Any] | None] | None = None,
            exec_hosts: Callable[[], tuple[dict[str, list[str]], str | None]] | None = None,
            self_host: str | None = None, host_names: list[str] | None = None) -> dict[str, Any]:
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
    tasks = tasks_section(events or [], ov, peek, now, child_errs + office_errs) if events is not None \
        else _section(now, "error", child_errs + office_errs, window_s=int(CHILD_WINDOW_S), counts=None, running=[],
                      queued=[], recent=[], truncated=False)
    conv = conversations_section(ov, backend_sessions, now) if ov is not None \
        else _section(now, "error", office_errs, backend_sessions=backend_sessions, active=None, working=None,
                      floors=None, items=[], hidden=0)
    tv_errs: list = []
    tv = guard("router.db", tasks_view, tv_errs)
    routed = routed_section(tv, now) if tv is not None \
        else _section(now, "error", tv_errs, calls=[], totals={}, row_limit=None, more=False)
    u_errs: list = []
    u = guard("usage", usage_view, u_errs)
    subs = subscriptions_section(u, now) if u is not None else _section(now, "error", u_errs, lanes=[], lowest=None)
    s1 = s1_section(u, now, u_errs or None)
    hb_errs: list = []
    beats = guard("heartbeats", heartbeats, hb_errs) or {}
    ex_errs: list = []
    ex = guard("exec-hosts", exec_hosts, ex_errs) if exec_hosts is not None else None
    ex_hosts, ex_default = ex if isinstance(ex, tuple) else ({}, None)
    hosts = hosts_section(u, beats, names=host_names if host_names is not None else known_hosts(),
                          exec_hosts=ex_hosts, exec_default=ex_default, self_host=self_host, now=now,
                          errors=hb_errs + ex_errs + (u_errs if u is None else []))
    counts = tasks.get("counts") or {}
    overview = {
        "running_tasks": counts.get("running") if tasks["status"] != "error" else None,
        "queued_tasks": counts.get("queued") if tasks["status"] != "error" else None,
        "active_conversations": conv.get("active"), "working_conversations": conv.get("working"),
        "s1": s1["status"], "s1_latency_ms": s1.get("latency_ms"),
        "lowest_subscription": subs.get("lowest"),
        "hosts_with_heartbeat": sum(1 for h in hosts["items"] if h["heartbeat_state"] == "fresh"),
        "errors": sum(len(s["errors"]) for s in (tasks, conv, routed, subs, s1, hosts)),
    }
    return {"version": VERSION, "generated_at": round(now, 3), "overview": overview, "tasks": tasks,
            "conversations": conv, "routed": routed, "subscriptions": subs, "s1": s1, "hosts": hosts}
