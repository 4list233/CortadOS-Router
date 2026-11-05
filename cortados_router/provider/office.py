"""The Office view: GET /v1/cortados/office, a metadata-only picture of who is working through CortadOS right now.

Each Claude Code session that routes through CortadOS is a FLOOR (keyed by its session tag), with its ORCHESTRATOR (the
main session, known from its heartbeats or, when the main loop itself routes, from its own requests) and its WORKERS
(routed subagents and child tasks). Routed work that carries no session tag (the outcome loop, the bot gateway, a plain
`claude --model cortados`) waits in the LOBBY.

Owner decisions (2026-11-04): floors only for sessions that route through CortadOS; metadata only. A floor carries
project name, device, model, lane, status, the current tool NAME and the routed task title (<= 80 chars). Never prompt
text, tool arguments or results, file contents, command text, paths, account names or tokens. Everything here is built
from fields this module validates itself (strict charsets and length caps); nothing from a request body is ever kept.

Inputs, all in memory except the child-task log:
  - routed requests (server.Handler._messages / _relay_local): turn start and end per worker, the route's provider,
    model and lane, and the NAMES of the tool_use blocks the backend hands the client;
  - $CORTADOS_HOME/child-tasks.jsonl (child_tasks.log_event): started / finished / cancelled, with title and tag;
  - heartbeats (POST /v1/cortados/office/heartbeat) from the cortados-routed-subagents mod, for the orchestrator.

The session tags and titles are client-asserted, inside the owner token's trust boundary (like x-cortados-run-id):
cosmetic, never authorizing anything. A malformed tag header is DROPPED and counted (dropped_headers in the view); a
routed request is never refused over one, because routing must not depend on a cosmetic header.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

from cortados_router.provider.usage import _has_address

VERSION = 1
CHILD_EVENT_LOG = "child-tasks.jsonl"   # child_tasks.EVENT_LOG (a test keeps the two equal)
SESSION_HEADER = "x-cortados-session"
PROJECT_HEADER = "x-cortados-project"
DEVICE_HEADER = "x-cortados-device"
TITLE_HEADER = "x-cortados-task-title"
# The header rules. The session tag is the mod's sha256(salt:session id) cut to 16 hex (64 allowed for a longer cut);
# project and device are a directory basename and a short hostname: plain names, no '/' (a path cannot pass); a title
# is the orchestrator-written task description, printable ASCII only (no control characters or escapes) and 80 chars,
# the owner's cap. Anything holding an address-looking string (an IP, a MAC) is dropped too: a title is free text.
_HEADER_RULES: dict[str, re.Pattern] = {
    SESSION_HEADER: re.compile(r"[0-9a-f]{16,64}"),
    PROJECT_HEADER: re.compile(r"[A-Za-z0-9._ -]{1,64}"),
    DEVICE_HEADER: re.compile(r"[A-Za-z0-9._-]{1,64}"),
    TITLE_HEADER: re.compile(r"[\x20-\x7e]{1,80}"),
}
TAG_FIELDS = {SESSION_HEADER: "session", PROJECT_HEADER: "project", DEVICE_HEADER: "device", TITLE_HEADER: "title"}
# Heartbeat and view field rules. Model ids as the exclude header accepts them (server._EXCLUDE_MODEL_RE), capped at
# 64; a lane key (claude#b, codex, local-studio); a tool name (Read, mcp__server__tool, mcp__claude-in-chrome__x).
_MODEL_RE = re.compile(r"[A-Za-z0-9._/\[\]:-]{1,64}")
_LANE_RE = re.compile(r"[A-Za-z0-9._#:/-]{1,64}")
_TOOL_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")       # server._RUN_ID_RE
ORCHESTRATOR_STATUSES = ("working", "idle", "ended")   # what a heartbeat may say; `ended` removes it
WORKER_STATUSES = ("working", "idle", "error", "done")
PROVIDERS = ("claude", "codex", "local")

# ---- lifetimes: each one says why it is that long ----
# A heartbeat saying `working` is refreshed on every tool call (throttled to one per 30 s when nothing changes), so one
# not refreshed for 120 s (four missed refreshes) is a session that died mid-turn: dropped rather than shown working.
HEARTBEAT_TTL_S = 120.0
# An `idle` heartbeat is not refreshed while the person is away (the mod sends on events only, it has no timer), and a
# session that ends sends `ended`; so an idle claim lives as long as a floor does without activity.
IDLE_HEARTBEAT_TTL_S = 900.0
# A floor with no routed request, child-task event or heartbeat for 15 min is a session that stopped routing.
FLOOR_TTL_S = 900.0
# A worker whose last turn ended (end_turn) and that sent nothing for 60 s has finished: a headless `claude -p` subagent
# exits after its final answer, and the provider never sees an exit, only the silence.
DONE_AFTER_S = 60.0
# Finished (done / error) workers stay visible for 10 min, so a glance at the office shows what just completed or failed.
WORKER_KEEP_S = 600.0
# Child tasks run up to CHILD_MAX_S_DEFAULT (3 h); the log is read 4 h back so a running one is still found.
CHILD_WINDOW_S = 4 * 3600.0
# ---- caps: memory is bounded because every key is client-chosen ----
MAX_FLOORS = 64             # sessions routing at once on one provider: a handful; 64 is far past any real fleet
MAX_WORKERS = 1024          # all workers across floors and lobby, LRU
MAX_WORKERS_PER_FLOOR = 32  # the default Claw3D floor has 8 desks; 32 keeps a fan-out visible without a huge body
MAX_LOBBY = 64              # untagged work (outcome loop, bot, plain clients): recent ones only
MAX_HEARTBEATS = 256        # one per live Claude Code session that runs the mod


def clean(value: Any, rule: re.Pattern) -> str | None:
    """`value` if it is a string fully matching `rule` and holding no address, else None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or not rule.fullmatch(v) or _has_address(v):
        return None
    return v


def provider_of(route_provider: str | None, local_prefix: str = "local-") -> str | None:
    """claude | codex | local for a route's provider (claude-cli#b -> claude). An unknown provider is None (shown as
    unknown), never guessed: the view must not say a lane is Claude's when it is not."""
    base = (route_provider or "").split("#", 1)[0]
    if base == "claude-cli":
        return "claude"
    if base == "codex-cli":
        return "codex"
    if base.startswith(local_prefix):
        return "local"
    return None


class OfficeBoard:
    """In-memory, locked, bounded state behind the Office view. Every method is bookkeeping and must never raise into a
    turn: the Handler calls them under a guard as well."""

    def __init__(self):
        self._lock = threading.Lock()
        self.floors: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.workers: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.mains: dict[str, dict[str, Any]] = {}     # session -> the routed main loop (whole-session mode)
        self.heartbeats: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.dropped: Counter = Counter()

    # ---------------------------------------------------------------- inputs
    def read_tags(self, headers) -> dict[str, str]:
        """The valid office tags of a request ({session, project, device, title}); an invalid header is dropped and
        counted, never an error."""
        out: dict[str, str] = {}
        bad: list[str] = []
        for name, field in TAG_FIELDS.items():
            raw = headers.get(name)
            if raw is None:
                continue
            v = clean(raw, _HEADER_RULES[name])
            if v is None:
                bad.append(name)
            else:
                out[field] = v
        if bad:
            with self._lock:
                self.dropped.update(bad)
        return out

    def _floor_locked(self, tags: dict[str, str], now: float) -> dict[str, Any] | None:
        s = tags.get("session")
        if not s:
            return None
        f = self.floors.pop(s, None) or {"session": s, "project": None, "device": None, "since": now}
        f["project"] = tags.get("project") or f["project"]
        f["device"] = tags.get("device") or f["device"]
        f["last"] = now
        self.floors[s] = f
        while len(self.floors) > MAX_FLOORS:
            gone, _ = self.floors.popitem(last=False)
            self.mains.pop(gone, None)
        return f

    def known(self, key: str) -> bool:
        with self._lock:
            if key.startswith("main:"):
                return key[5:] in self.mains
            return key in self.workers

    @staticmethod
    def worker_key(tags: dict[str, str], run_id: str | None, conversation: str | None) -> str | None:
        """Which worker a request belongs to: its run id; else, on a tagged session, that session's routed main loop
        ('main:<session>'); else its conversation ('c-<id>', lobby). None: nothing to show."""
        if run_id:
            return "r:" + run_id
        if tags.get("session"):
            return "main:" + tags["session"]
        if conversation:
            return "c:" + conversation
        return None

    def turn_start(self, key: str | None, tags: dict[str, str], *, run_id: str | None, provider: str | None,
                   lane: str | None, model: str | None, now: float | None = None) -> None:
        if not key:
            return
        now = time.time() if now is None else now
        with self._lock:
            self._floor_locked(tags, now)
            fields = {"provider": provider if provider in PROVIDERS else None, "lane": clean(lane, _LANE_RE),
                      "model": clean(model, _MODEL_RE), "status": "working", "tool": None, "updated": now,
                      "ended_at": None}
            if key.startswith("main:"):
                m = self.mains.get(tags["session"]) or {}
                m.update(fields)
                self.mains[tags["session"]] = m
                return
            w = self.workers.pop(key, None) or {"run_id": run_id, "kind": "subagent", "title": None,
                                                 "session": tags.get("session"), "started_at": now}
            w.update(fields)
            if tags.get("title"):
                w["title"] = tags["title"]
            if tags.get("session") and not w.get("session"):
                w["session"] = tags["session"]
            self.workers[key] = w
            while len(self.workers) > MAX_WORKERS:
                self.workers.popitem(last=False)

    def turn_end(self, key: str | None, tags: dict[str, str], *, status: str, tool: str | None = None,
                 now: float | None = None) -> None:
        """A turn ended: `idle` (the model answered), `working` with the tool it asked the client to run, or `error`."""
        if not key or status not in WORKER_STATUSES:
            return
        now = time.time() if now is None else now
        with self._lock:
            if tags.get("session") and tags["session"] in self.floors:
                self._floor_locked(tags, now)
            rec = self.mains.get(tags["session"]) if key.startswith("main:") else self.workers.get(key)
            if rec is None:
                return
            rec["status"], rec["tool"], rec["updated"] = status, clean(tool, _TOOL_RE), now
            if status == "error":
                rec["ended_at"] = now

    def heartbeat(self, hb: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """Store one validated heartbeat (validate_heartbeat). `ended` drops the session's heartbeat."""
        now = time.time() if now is None else now
        s = hb["session"]
        with self._lock:
            self.heartbeats.pop(s, None)
            if hb.get("status") == "ended":
                return {"ok": True, "stored": False}
            self.heartbeats[s] = {**hb, "seen": now}
            while len(self.heartbeats) > MAX_HEARTBEATS:
                self.heartbeats.popitem(last=False)
            f = self.floors.get(s)
            if f is not None:                      # a heartbeat keeps an existing floor alive; it never creates one
                f["last"] = now
                if hb.get("project"):
                    f["project"] = hb["project"]
                if hb.get("device"):
                    f["device"] = hb["device"]
        return {"ok": True, "stored": True}

    # ---------------------------------------------------------------- the view
    def view(self, child_events: list[dict[str, Any]], *, now: float | None = None,
             errors: list[dict[str, str]] | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        children = _children(child_events, now)
        with self._lock:
            self._expire_locked(now)
            floors = {s: dict(f) for s, f in self.floors.items()}
            workers = {k: dict(w) for k, w in self.workers.items()}
            mains = {s: dict(m) for s, m in self.mains.items() if s in self.floors}
            beats = {s: dict(h) for s, h in self.heartbeats.items()}
            dropped = dict(self.dropped)
        # Child tasks: merged into the worker their own traffic made (same run id), else a worker of their own. A child
        # with a tag opens (or keeps) its floor, as a routed request does.
        for tid, c in children.items():
            key = "r:" + tid
            w = workers.get(key) or {"run_id": tid, "provider": None, "lane": c.get("lane"), "model": c.get("model"),
                                     "status": "working", "tool": None, "updated": c["ts"], "ended_at": None,
                                     "started_at": c["started_at"]}
            w["kind"] = "child_task"
            w["title"] = c.get("title") or w.get("title")
            w["session"] = c.get("session") or w.get("session")
            w["started_at"] = c["started_at"] if c.get("started_at") is not None else w.get("started_at")
            if c.get("final"):
                w["status"], w["tool"], w["ended_at"] = c["final"], None, c["ts"]
            w["updated"] = max(w["updated"], c["ts"])
            workers[key] = w
            s = c.get("session")
            if s and (c.get("final") is None or now - c["ts"] <= FLOOR_TTL_S):
                f = floors.get(s) or {"session": s, "project": c.get("project"), "device": c.get("device"),
                                      "since": c["started_at"] or c["ts"], "last": c["ts"]}
                f["project"] = f.get("project") or c.get("project")
                f["device"] = f.get("device") or c.get("device")
                f["last"] = max(f.get("last") or 0, c["ts"])
                floors[s] = f
        # Global caps cover the merged log AND live state. Keep the most recently active floors/workers; the log
        # tail may hold thousands of unique keys even though live state was already bounded.
        recent_floors = sorted(floors, key=lambda s: (-floors[s]["last"], s))[:MAX_FLOORS]
        floors_hidden = len(floors) - len(recent_floors)
        hidden_sessions = set(floors) - set(recent_floors)
        floors = {s: floors[s] for s in recent_floors}
        eligible = []
        for w in workers.values():
            st = w.get("status")
            if st == "idle" and now - (w.get("updated") or now) > DONE_AFTER_S:
                w["status"] = "done"
                w["ended_at"] = w.get("ended_at") or w.get("updated")
            if w["status"] in ("done", "error") and now - (w.get("ended_at") or now) > WORKER_KEEP_S:
                continue
            eligible.append(w)
        visible = [w for w in eligible if w.get("session") not in hidden_sessions]
        visible.sort(key=lambda w: (-(w.get("updated") or 0), w.get("run_id") or ""))
        workers_hidden = len(eligible) - min(len(visible), MAX_WORKERS)
        by_floor: dict[str | None, list[dict[str, Any]]] = {}
        for w in visible[:MAX_WORKERS]:
            s = w.get("session") if w.get("session") in floors else None
            by_floor.setdefault(s, []).append(w)  # retain private activity until per-floor/lobby selection
        out_floors = []
        for s, f in sorted(floors.items(), key=lambda kv: -(kv[1].get("last") or 0)):
            ws = _cap(by_floor.get(s, []), MAX_WORKERS_PER_FLOOR)
            out_floors.append({"session": s, "project": f.get("project"), "device": f.get("device"),
                               "since": _ts(f.get("since")), "last_activity": _ts(f.get("last")),
                               "orchestrator": _orchestrator(beats.get(s), mains.get(s)),
                               "workers": ws["items"], "workers_hidden": ws["hidden"]})
        lobby = _cap(by_floor.get(None, []), MAX_LOBBY)
        return {"version": VERSION, "ts": _ts(now), "floors": out_floors,
                "floors_hidden": floors_hidden, "workers_hidden": workers_hidden,
                "lobby": {"workers": lobby["items"], "workers_hidden": lobby["hidden"]},
                "dropped_headers": dropped, "errors": list(errors or [])}

    def _expire_locked(self, now: float) -> None:
        for s in [s for s, f in self.floors.items() if now - f.get("last", now) > FLOOR_TTL_S]:
            del self.floors[s]
            self.mains.pop(s, None)
        for s in [s for s, h in self.heartbeats.items()
                  if now - h["seen"] > (HEARTBEAT_TTL_S if h.get("status") == "working" else IDLE_HEARTBEAT_TTL_S)]:
            del self.heartbeats[s]
        for k in [k for k, w in self.workers.items()
                  if now - (w.get("updated") or now) > DONE_AFTER_S + WORKER_KEEP_S]:
            del self.workers[k]


def validate_heartbeat(body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(heartbeat, None) or (None, the name of the bad field). Unlike the routed headers a heartbeat is refused when
    malformed: it is this endpoint's own request, and a 400 tells the mod its sender is broken."""
    if not isinstance(body, dict):
        return None, "body"
    unknown = sorted(set(body) - {"session", "project", "device", "model", "status", "tool"})
    if unknown:
        return None, "(unknown field)"           # the key itself is not echoed: it is client text
    out: dict[str, Any] = {}
    session = clean(body.get("session"), _HEADER_RULES[SESSION_HEADER])
    if session is None:
        return None, "session"
    out["session"] = session
    status = body.get("status")
    if status not in ORCHESTRATOR_STATUSES:
        return None, "status"
    out["status"] = status
    for field, rule in (("project", _HEADER_RULES[PROJECT_HEADER]), ("device", _HEADER_RULES[DEVICE_HEADER]),
                        ("model", _MODEL_RE), ("tool", _TOOL_RE)):
        raw = body.get(field)
        if raw is None or raw == "":
            out[field] = None
            continue
        v = clean(raw, rule)
        if v is None:
            return None, field
        out[field] = v
    return out, None


def read_child_events(path: Path, now: float) -> list[dict[str, Any]]:
    """The child-task log's recent records (dicts), oldest first. Raises OSError for an unreadable log (the caller
    reports it in errors[]); a missing log is no events."""
    from cortados_router.provider.usage import _tail_lines
    try:
        lines, _ = _tail_lines(path)
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue                                # counted by the usage view; the office shows only good records
        if isinstance(rec, dict) and _child_timestamp(rec.get("ts"), now):
            out.append(rec)
    return out


def _child_timestamp(ts: Any, now: float) -> bool:
    # The log is local: future events are not real activity. Bound both sides before converting even huge integers.
    return (type(ts) in (int, float) and now - CHILD_WINDOW_S <= ts <= now and math.isfinite(ts))


def _children(events: list[dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    """task id -> {ts, started_at, title, session, project, device, lane, model, final}, every field re-validated
    (the log is a file: it is read as untrusted)."""
    out: dict[str, dict[str, Any]] = {}
    for e in events:
        if not isinstance(e, dict) or not _child_timestamp(e.get("ts"), now) or e.get("chrome") is True:
            continue                              # also ignore historical login-mode records that carried Office tags
        tid = clean(e.get("task_id"), _RUN_ID_RE)
        ev = e.get("event")
        if tid is None or ev not in ("started", "finished", "cancelled"):
            continue
        c = out.setdefault(tid, {"started_at": None, "final": None})
        c["ts"] = float(e["ts"])
        if ev == "started":
            c["started_at"] = float(e["ts"])
        for field, rule in (("title", _HEADER_RULES[TITLE_HEADER]), ("session", _HEADER_RULES[SESSION_HEADER]),
                            ("project", _HEADER_RULES[PROJECT_HEADER]), ("device", _HEADER_RULES[DEVICE_HEADER]),
                            ("lane", _LANE_RE), ("model", _MODEL_RE)):
            v = clean(e.get(field), rule)
            if v is not None:
                c[field] = v
        if ev == "finished":
            c["final"] = "done" if e.get("status") == "succeeded" else "error"
        elif ev == "cancelled":
            c["final"] = "done"                     # stopped on purpose by its orchestrator: finished, not failed
    return out


def _orchestrator(hb: dict[str, Any] | None, main: dict[str, Any] | None) -> dict[str, Any] | None:
    """The floor's orchestrator: its heartbeat (status and tool as the mod saw them), else its own routed requests when
    the main loop routes; None when neither is known (a floor whose session runs no mod)."""
    if hb:
        return {"model": hb.get("model") or (main or {}).get("model"), "status": hb.get("status"),
                "tool": hb.get("tool"), "last_seen": _ts(hb.get("seen")), "source": "heartbeat"}
    if main:
        return {"model": main.get("model"), "status": "idle" if main.get("status") == "done" else main.get("status"),
                "tool": main.get("tool"), "last_seen": _ts(main.get("updated")), "source": "routed"}
    return None


def _pub_worker(w: dict[str, Any]) -> dict[str, Any]:
    return {"run_id": w.get("run_id"), "kind": w.get("kind") or "subagent", "title": w.get("title"),
            "provider": w.get("provider"), "lane": w.get("lane"), "model": w.get("model"), "status": w.get("status"),
            "tool": w.get("tool"), "started_at": _ts(w.get("started_at")), "ended_at": _ts(w.get("ended_at"))}


def _cap(items: list[dict[str, Any]], n: int) -> dict[str, Any]:
    """The n most relevant workers (working first, then the most recent), and how many were left out."""
    rank = {"working": 0, "idle": 1, "error": 2, "done": 3}
    items = sorted(items, key=lambda w: (rank.get(w["status"], 4), -(w.get("updated") or 0)))
    return {"items": [_pub_worker(w) for w in items[:n]], "hidden": max(0, len(items) - n)}


def _ts(x: Any) -> float | None:
    return round(float(x), 3) if isinstance(x, (int, float)) else None
