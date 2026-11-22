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
                   `elsewhere` ({task_id, host, started_at} and the origin_* fields its stub recorded, the last
                   10 h): started from here on another host, its state there unknown to this view.
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
  - orchestrators  the tree the pulse pane and the dashboard draw (orchestrators_section): each orchestrating main
                   session (Office tag), the bot gateway and an unattributed group -> the child tasks it started
                   (nested through parent_task_id; a task without an origin takes its root's) -> each task's
                   sub-sessions (the run's open and recently closed provider conversations, x-cortados-run-id), with
                   status, timing, model and in-memory token totals. GET /v1/cortados/live/session?session=<tag>
                   (session_view) is the pane's view: that one subtree and session-free fleet health, nothing else.
  - work_split     orchestrator tokens (the main sessions' own, from the heartbeats' `orchestrator` blocks) against
                   routed tokens (router.db, via tasks.collect's token_totals), overall and per main session
                   (provider/work_split.py has the definitions).

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

A heartbeat may also carry an `orchestrator` block (cortados_router/orchestrator_usage.py: the host's main Claude Code
sessions' tokens per window, by Office session tag; validated by work_split.validate_block), the host's `hardware` (cpu/gpu busy %, memory, load, disk free %: numbers and the chip
name only) and, on the S1 host, an `s1` block (health, the served model's name, the pipeline's hold flags by fixed
name, a pending candidate's reason slug), and a `tasks` block (#118: the host's own start_task children of the last 24 h,
from its own child-task log, metadata only; validate_tasks, merged into the orchestrator tree by heartbeat_rows);
cortados_router/live_heartbeat.py is the sender (one per host, every 60 s).
A post replaces that host's previous one. It is shown fresh for ttl_s (default 900, 60..86400), then stale, and is
forgotten after 24 h. A live:heartbeat client token posts for its bound host only (provider/clients.py; 403 otherwise),
and the board's LRU never evicts a configured host (CORTADOS_LIVE_HOSTS / the defaults, exec-hosts.json), so posts for
made-up names can only push out other made-up names. A malformed heartbeat is refused with 400 naming the bad field (never echoing its value), a body
over HEARTBEAT_MAX_BYTES with 413. docs/PROVIDER-PLAN.md ("Live view") has the schema and the sender.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from cortados_router import child_tasks, router_db
from cortados_router.provider import office, work_split

logger = logging.getLogger("cortados.provider.live")

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
# 16 jobs x ~300 bytes is under 5 KiB; the orchestrator block adds at most 30 sessions x ~350 bytes (~10 KiB) and the
# tasks block at most HB_TASKS_MAX entries x ~550 bytes (~22 KiB, all fields at their longest). Larger is not a
# heartbeat. The sender trims its tasks block (then drops the orchestrator block) to fit, never the jobs.
HEARTBEAT_MAX_BYTES = 49152
# ---- a heartbeat's `tasks` block (#118): the host's own recent start_task children, metadata only ----
# A host that runs children (an exec host: `host: "here"` on the Pro, the Studio) logs them in ITS child-task log,
# which the provider cannot read. Its heartbeat carries them so the tree shows tasks from every host. 40 is the
# live view's RECENT_MAX: a day's worth at the owner's pace; running ones are sent first, then the newest finished.
HB_TASKS_MAX = 40
HB_TASK_STATES = ("running", "queued", "succeeded", "failed", "cancelled", "unknown")
# the phases tasks_section itself writes, the only ones a heartbeat may carry (a fixed vocabulary, never free text)
HB_TASK_PHASES = ("waiting for the host's Chrome", "ended on its host; no end event logged", "no end event after 10 h")
_HB_TASK_KEYS = {"task_id", "title", "state", "phase", "host", "lane", "model", "started_at", "ended_at", "workspace",
                 "browser", "origin_session", "origin_project", "origin_device", "origin_machine", "origin_via",
                 "parent_task_id",
                 "session"}
# A task id's host and origin are bound once (HeartbeatBoard._bindings, review rounds 3 and 4): the first host that
# reports it owns it, with its origin and parent; a later claim that differs is dropped and counted. The bindings last
# HEARTBEAT_KEEP_S (24 h, the heartbeat window) since the id was last reported and are kept in BINDINGS_FILE under
# $CORTADOS_HOME, so a provider restart does not reopen them. At most TASK_BINDINGS_MAX: only expired ones are ever
# evicted; a full table refuses a NEW id (counted as a conflict) rather than drop a live binding.
TASK_BINDINGS_MAX = 4096
BINDINGS_FILE = "live-task-bindings.json"
BINDINGS_VERSION = 1
# The most of BINDINGS_FILE that is ever read. One entry is under 200 bytes (two task ids, a host, a 64-hex origin, a
# time), so TASK_BINDINGS_MAX entries are under 1 MiB; 2 MiB leaves room and keeps a corrupt or planted file from
# making the provider's start parse an unbounded document. A larger file is not read at all (said in errors()).
BINDINGS_MAX_BYTES = 2 * 1024 * 1024
BINDINGS_SAVE_SEEN_S = 3600.0   # a re-reported id's `seen` reaches the file at most hourly: the TTL may run 1 h short
STALE_HB_PHASE = "its host's heartbeat is stale; state as last reported"
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
# The orchestrator tree also sees what the hosts' heartbeats carry (#118): `task_hosts` names them.
TREE_SCOPE = "provider host and heartbeats"
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
_ORIGIN_SESSION_RE = child_tasks.ORIGIN_SESSION_RE
_ORIGIN_RULES = (("origin_session", _ORIGIN_SESSION_RE), ("origin_project", office._HEADER_RULES[office.PROJECT_HEADER]),
                 ("origin_device", office._HEADER_RULES[office.DEVICE_HEADER]),
                 ("origin_machine", child_tasks.MACHINE_TAG_RE))
# origin_machine (child_tasks.ORIGIN_KEYS) is carried internally to pick a machine's untagged tasks; it is a
# capability, so no section ever publishes it (_pub strips it from every published task row).
_PRIVATE_KEYS = ("origin_machine",)
MACHINE_FILTER_RULE = "one machine tag (16-64 lowercase hex, the pulse pane's machineTag)"
SESSION_FILTER_RULE = "one Office session tag (16-64 lowercase hex) or " + " or ".join(child_tasks.ORIGIN_NAMED)
# ---- the orchestrator tree (orchestrators_section) ----
ORCH_MAX = 32               # orchestrator groups (sessions, the bot, unattributed); the rest flagged truncated
ORCH_TASKS_MAX = 20         # top-level tasks per orchestrator (nested ones ride with their parent), active first
NEST_MAX = 4                # nesting depth drawn (a parent chain longer than this is flattened at the cut)
SUBSESSIONS_MAX = 8         # sub-sessions listed per task (main loop first, then working, then newest)
ORCH_ROUTED_MAX = 8         # an orchestrator's own routed subagents (route_task, the routed-subagents mod) listed
# The tree is built from the UNCAPPED conversation items and remote stubs (the fleet lists' own caps, 64 and 50, made
# one session's items vanish behind another's: review 2026-11-14), filtered by origin first. Ownership comes from each
# conversation's own tags and the board's uncapped workers, a session's stubs from its origin index
# (child_tasks.remote_stubs_for), so every cut a session view names is its own (docs/PULSE-PER-SESSION.md §5 has each
# input's bound and cost). This hard ceiling only bounds the work on a runaway fleet; when it cuts, the section names
# the input in `inputs_truncated`.
TREE_INPUT_MAX = 2000
# Statuses in the owner's words (docs/PULSE-PER-SESSION.md): running, waiting (queued, or idle between turns), done,
# failed, unknown (no end event after 10 h, ended without one, or on another host).
_TASK_STATUS = {"running": "running", "queued": "waiting", "succeeded": "done", "cancelled": "done",
                "failed": "failed", "unknown": "unknown", "elsewhere": "unknown"}
MEMO_KEYS = 16              # live.Memo: the fleet view plus this many-1 session views memoised side by side
ORIGINS_MAX = 32            # origin groups listed (sessions with a task in the window); the rest flagged truncated
ORIGIN_IDS_MAX = 20         # task ids per origin group and per orchestrator item, newest first
_HB_KEYS = {"host", "jobs", "ttl_s", "hardware", "s1", "orchestrator", "tasks"}
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
    """A finite real number. Never raises: math.isfinite raises OverflowError for an int past a float's range (a JSON
    `1e400` is inf, but `10**400` written out is an int), which a persisted file or a body may hold (review round 5)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except (OverflowError, ValueError):
        return False


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
    if body.get("orchestrator") is not None:
        orch, bad = work_split.validate_block(body["orchestrator"], now, TIME_SKEW_S)
        if bad is not None:
            return None, "orchestrator" + bad
        out["orchestrator"] = orch
    if body.get("tasks") is not None:
        tasks, bad = validate_tasks(body["tasks"], host, now)
        if bad is not None:
            return None, "tasks" + bad
        out["tasks"] = tasks
    return out, None


def validate_tasks(block: Any, host: str, now: float) -> tuple[dict[str, Any] | None, str | None]:
    """A heartbeat's `tasks` block, {"items": [entry], "truncated": bool}, or (None, ".<bad field>"). Each entry is one
    start_task child that ran on THIS host (`host` must be the heartbeat's own: a host speaks for its own tasks only),
    every field re-checked by the rule the live view applies to its own log (tasks_section, _children). An entry
    whose newest time is outside the 24 h window is dropped, not refused: the sender filters by the same window, so
    only clock skew puts one there. `session` (the child's own Office tag, sent by senders since round 3) is checked and
    then dropped: a heartbeat-supplied tag is never a join key (review round 4; heartbeat_rows joins on what the
    provider itself observed)."""
    if not isinstance(block, dict) or set(block) - {"items", "truncated"}:
        return None, ""
    items = block.get("items", [])
    if not isinstance(items, list) or len(items) > HB_TASKS_MAX:
        return None, ".items"
    if not isinstance(block.get("truncated", False), bool):
        return None, ".truncated"
    out, seen = [], set()
    title_rule = office._HEADER_RULES[office.TITLE_HEADER]
    for i, t in enumerate(items):
        where = f".items[{i}]"
        if not isinstance(t, dict) or set(t) - _HB_TASK_KEYS:
            return None, where
        tid = t.get("task_id")
        if not (isinstance(tid, str) and child_tasks.TASK_ID_RE.fullmatch(tid)) or tid in seen:
            return None, where + ".task_id"
        seen.add(tid)
        if t.get("host", host) != host:
            return None, where + ".host"
        if t.get("state") not in HB_TASK_STATES:
            return None, where + ".state"
        e: dict[str, Any] = {"task_id": tid, "host": host, "state": t["state"]}
        if t.get("phase") is not None:
            if t["phase"] not in HB_TASK_PHASES:
                return None, where + ".phase"
            e["phase"] = t["phase"]
        if t.get("session") is not None and office.clean(t["session"], office._HEADER_RULES[office.SESSION_HEADER]) is None:
            return None, where + ".session"
        for k, rule in (("title", title_rule), ("lane", office._LANE_RE), ("model", office._MODEL_RE),
                        ("workspace", _WS_STATE_RE), *_ORIGIN_RULES):
            if t.get(k) is not None:
                e[k] = office.clean(t[k], rule)
                if e[k] is None:
                    return None, f"{where}.{k}"
        if t.get("origin_via") not in (None, "parent"):
            return None, where + ".origin_via"
        if t.get("origin_via") == "parent":
            e["origin_via"] = "parent"
        parent = t.get("parent_task_id")
        if parent is not None:
            if not (isinstance(parent, str) and child_tasks.TASK_ID_RE.fullmatch(parent)) or parent == tid:
                return None, where + ".parent_task_id"
            e["parent_task_id"] = parent
        if t.get("browser") is not None:
            if not isinstance(t["browser"], bool):
                return None, where + ".browser"
            e["browser"] = t["browser"]
        for k in ("started_at", "ended_at"):
            v = t.get(k)
            if v is None:
                continue
            # never from the future (a little skew allowed): a later stamp would order it above real activity
            if not (_num(v) and now - TIME_SKEW_S <= v <= now + SAMPLE_FUTURE_SKEW_S):
                return None, f"{where}.{k}"
            e[k] = round(float(v), 3)
        if e.get("started_at") is None and e.get("ended_at") is None:
            return None, where + ".started_at"
        if e.get("ended_at") is not None and e.get("started_at") is not None and e["ended_at"] < e["started_at"]:
            return None, where + ".ended_at"
        final = t["state"] in _TASK_STATES
        if final != (e.get("ended_at") is not None):
            return None, where + ".ended_at"        # a finished task has its end time, an open one has none
        if max(e.get("ended_at") or 0, e.get("started_at") or 0) < now - CHILD_WINDOW_S:
            continue                                # outside the window: dropped (see above)
        out.append(e)
    return {"items": out, "truncated": block.get("truncated", False) is True}, None


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


class BindingsTooLarge(ValueError):
    """BINDINGS_FILE is over BINDINGS_MAX_BYTES: not read (its type name is the error the tree shows)."""


class HeartbeatBoard:
    """In-memory, locked, bounded: host -> its latest validated heartbeat. A restart forgets them (the next post
    restores each), which the view shows honestly as "no heartbeat yet". The task-id bindings are the exception: with
    `path` (the server passes $CORTADOS_HOME/BINDINGS_FILE) they are loaded at start and written atomically when they
    change, so a restart cannot reopen an id to whichever host reports it first (review round 4, P2). Without `path`
    (tests) they live in memory only.

    `protected` (optional) names the configured hosts; it is asked only when the board is over HEARTBEAT_HOSTS_MAX, and
    eviction then drops the least recently posted host that is NOT configured. Configured hosts are the owner's own
    list, so the board holds at most HEARTBEAT_HOSTS_MAX + their number. A failing `protected` protects nothing (the
    plain LRU), never an unbounded board. `generation` changes with every post (the live view's memo key)."""

    def __init__(self, protected: Callable[[], Any] | None = None, path: str | os.PathLike | None = None):
        self._lock = threading.Lock()
        self._hosts: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._protected = protected
        self.generation = 0
        # task id -> {"bind": (host, origin_session, parent_task_id), "seen": t, "saved": the seen last written}
        self._bindings: dict[str, dict[str, Any]] = {}
        self._path = Path(path) if path is not None else None
        self._load_error: str | None = None
        self._save_error: str | None = None
        if self._path is not None:
            self._load(time.time())

    @staticmethod
    def binding(e: dict[str, Any]) -> tuple:
        """What a task id is bound to: the host it ran on and who it belongs to (its origin, its parent). Any of these
        changing is a different claim on the id, not an update. (The child's own Office tag is no part of it since
        round 4: it was the reporting host's word, and nothing joins on it any more.)"""
        return (e.get("host"), e.get("origin_session"), e.get("parent_task_id"))

    @staticmethod
    def same_claim(bound: tuple, want: tuple) -> bool:
        """Whether a report `want` is the bound claim again: host, origin and parent all equal."""
        return tuple(bound) == tuple(want)

    # ---- persistence (BINDINGS_FILE)
    def _load(self, now: float) -> None:
        """Read the bindings file: valid, unexpired entries only, the newest TASK_BINDINGS_MAX. A missing file is an
        empty table. An unreadable, oversized (BINDINGS_MAX_BYTES) or malformed file is an empty table too, and an
        invalid entry is skipped; either is said in errors() (the tree's `task bindings` error, the type only) and
        logged. Nothing raised while reading or validating ever leaves here: the Registry builds the board at
        provider start, and a corrupt file must never stop the provider (review round 5, P2)."""
        try:
            ok, bad = self._read_bindings(now)
        except Exception as exc:                    # unreadable, oversized, malformed: the whole file is skipped
            self._load_error = type(exc).__name__
            logger.warning("live: task bindings unreadable, starting empty: %s", type(exc).__name__)
            return
        if bad:
            self._load_error = "InvalidEntry"
            logger.warning("live: task bindings: %d invalid entries skipped", bad)
        ok.sort(key=lambda x: x[0], reverse=True)
        for seen, tid, bind in ok[:TASK_BINDINGS_MAX]:
            self._bindings.setdefault(tid, {"bind": bind, "seen": seen, "saved": seen})

    def _read_bindings(self, now: float) -> tuple[list[tuple[float, str, tuple]], int]:
        """(valid unexpired entries, the number of invalid ones). Raises for a file that cannot be used at all (a
        missing file is ([], 0)); one bad entry is counted and skipped, never the file."""
        try:
            with open(self._path, "rb") as f:
                data = f.read(BINDINGS_MAX_BYTES + 1)
        except FileNotFoundError:
            return [], 0
        if len(data) > BINDINGS_MAX_BYTES:
            raise BindingsTooLarge()
        doc = json.loads(data.decode("utf-8"))
        rows = doc.get("bindings") if isinstance(doc, dict) and doc.get("version") == BINDINGS_VERSION else None
        if not isinstance(rows, list):
            raise ValueError("malformed")
        ok: list[tuple[float, str, tuple]] = []
        bad = 0
        for r in rows:
            try:
                entry = self._valid_entry(r, now)
            except Exception:                       # whatever a planted value makes a check raise: that entry only
                entry = False
            if entry is False:
                bad += 1
            elif entry is not None:
                ok.append(entry)
        return ok, bad

    @staticmethod
    def _valid_entry(r: Any, now: float) -> Any:
        """(seen, task id, binding) for a valid unexpired entry; None for a valid expired one; False for an invalid
        one: not a 5-list, a field of the wrong type or shape, a `seen` that is no finite number (NaN, Infinity, an
        int past a float's range) or later than the clock allows."""
        if not (isinstance(r, list) and len(r) == 5):
            return False
        tid, host, origin, parent, seen = r
        if not (isinstance(tid, str) and child_tasks.TASK_ID_RE.fullmatch(tid)
                and isinstance(host, str) and _HOST_RE.fullmatch(host)
                and (origin is None or (isinstance(origin, str) and _ORIGIN_SESSION_RE.fullmatch(origin)))
                and (parent is None or (isinstance(parent, str) and child_tasks.TASK_ID_RE.fullmatch(parent)))
                and _num(seen) and seen <= now + SAMPLE_FUTURE_SKEW_S):
            return False
        if seen < now - HEARTBEAT_KEEP_S:
            return None                             # expired: dropped as the board would, not an error
        return float(seen), tid, (host, origin, parent)

    def _save_locked(self) -> None:
        """Write the bindings atomically (a private temp file in the same directory, then os.replace), mode 0600. A
        failure is logged and said in errors(); the in-memory table stands."""
        tmp = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            doc = {"version": BINDINGS_VERSION,
                   "bindings": [[t, *b["bind"], round(b["seen"], 3)] for t, b in self._bindings.items()]}
            tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}-{threading.get_ident()}")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            tmp = None
            for b in self._bindings.values():
                b["saved"] = b["seen"]
            self._save_error = None
        except Exception as exc:
            self._save_error = type(exc).__name__
            logger.warning("live: task bindings not saved: %s", type(exc).__name__)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def errors(self) -> list[dict[str, str]]:
        """The bindings file's trouble ({source: "task bindings", error: <type>}), for the tree's errors: a provider
        that could not load or save them says so instead of looking like a clean table."""
        with self._lock:
            return [_err("task bindings", e) for e in (self._load_error, self._save_error) if e]

    def _bind_locked(self, hb: dict[str, Any], now: float) -> tuple[dict[str, Any], bool]:
        """(heartbeat, changed): the heartbeat with every task entry whose id is already bound to another host, origin or parent dropped,
        and their number in tasks.conflicts. The first report of an id binds it (review round 3, P1: a newer heartbeat
        from another host must not take over a task id and re-publish its run under a chosen origin). Expired bindings
        (not reported for HEARTBEAT_KEEP_S) are the only ones removed; with TASK_BINDINGS_MAX live ones a new id is
        refused and counted, never bound by evicting another (review round 4, P2). `changed`: the table must be saved."""
        changed = False
        for t in [t for t, b in self._bindings.items() if now - b["seen"] > HEARTBEAT_KEEP_S]:
            del self._bindings[t]
            changed = True
        block = hb.get("tasks")
        if not isinstance(block, dict):
            return hb, changed
        kept, conflicts = [], 0
        for e in block.get("items") or []:
            want = self.binding(e)
            b = self._bindings.get(e["task_id"])
            if b is not None:
                if not self.same_claim(b["bind"], want):
                    conflicts += 1                  # dropped: the id stays with its first host and origin
                    continue
                b["seen"] = max(b["seen"], now)
                changed = changed or b["saved"] is None or b["seen"] - b["saved"] > BINDINGS_SAVE_SEEN_S
            elif len(self._bindings) >= TASK_BINDINGS_MAX:
                conflicts += 1                      # full of live bindings: refused, none evicted
                continue
            else:
                self._bindings[e["task_id"]] = {"bind": want, "seen": now, "saved": None}
                changed = True
            kept.append(e)
        return {**hb, "tasks": {**block, "items": kept, "conflicts": conflicts}}, changed

    def post(self, hb: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            hb, changed = self._bind_locked(hb, now)
            if changed and self._path is not None:
                self._save_locked()
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
        out = {"ok": True, "host": hb["host"], "jobs": len(hb["jobs"]), "fresh_for_s": hb["ttl_s"]}
        if isinstance(hb.get("tasks"), dict) and hb["tasks"]["conflicts"]:
            out["task_conflicts"] = hb["tasks"]["conflicts"]   # the sender's own entries: no other host's data
        return out

    def snapshot(self, now: float | None = None) -> dict[str, dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            for h in [h for h, v in self._hosts.items() if now - v["received_at"] > HEARTBEAT_KEEP_S]:
                del self._hosts[h]
            return {h: {**v, "jobs": [dict(j) for j in v["jobs"]],
                        **({"tasks": {**v["tasks"], "items": [dict(t) for t in v["tasks"]["items"]]}}
                           if isinstance(v.get("tasks"), dict) else {})}
                    for h, v in self._hosts.items()}


def heartbeat_rows(beats: dict[str, dict[str, Any]], now: float, known: dict[str, dict[str, Any]],
                   office_view: dict[str, Any] | None = None,
                   conflicted: Any = frozenset()) -> tuple[list[dict[str, Any]], list[str], bool, int]:
    """(rows, hosts, cut, conflicts): the heartbeats' task entries as tasks_section rows, for the orchestrator tree,
    the hosts whose heartbeat carried a tasks block, whether any of those blocks was cut by its sender (`truncated`:
    the host had more than HB_TASKS_MAX tasks in its window), and how many claims on a task id were dropped.

    A task id's host and origin are authoritative and set once (review round 3, P1). `known` (task id -> row: the
    provider host's own log and its remote stubs) is first-hand: an entry that names another host or another origin
    for such an id is a conflict; otherwise a log row wins, and the host's own report replaces a stub (`stub` true: a
    start sent from here onto that host). Between heartbeats the board bound each id to the first host that reported it
    (HeartbeatBoard._bind_locked: later differing claims were already dropped and counted there); should two current
    beats still disagree (bindings lost with an unreadable bindings file), the earlier-received one keeps the id and
    the other counts. A `known` entry marked `unverified` (its stub could not be read) drops the claim uncounted: an
    id this machine may own is never handed to a heartbeat on a guess.
    A heartbeat past its TTL still lists its tasks, but a running or queued one is then `unknown` ("its host's
    heartbeat is stale"). A running task with no end after STALE_RUNNING_S is unknown, as tasks_section says.

    Joins (review round 4: a join key is something the provider observed itself, never something a heartbeat
    supplied). The Office board's worker for the run (x-cortados-run-id: its title, model, lane, provider, current
    tool) joins only when the provider saw the run's own requests carry x-cortados-origin equal to the task's bound
    origin (the worker's `origin`, office.note_origin); a run id and every tag in the entry are the reporting host's
    word. Each row carries its bound origin in `bind_origin` (never published) so orchestrators_section applies the
    same rule to the run's sub-sessions (each conversation's observed origin) and tokens (the run's, RunRecords). A task
    with no origin, or whose run the provider never saw with it, shows its heartbeat's own fields only. Any worker with
    the run id is considered (a remote child's routed traffic makes a `subagent` worker here; `child_task` ones come
    from this host's own log), a `child_task` one first.
    `conflicted` (collect passes conflicted_runs over every record of every run, review round 5, P2): run ids the
    provider saw with two origins, or ORIGIN_CONFLICT, anywhere. Such a run's row joins nothing at all: no worker
    field here, and its `bind_origin` is None, so orchestrators_section joins none of its sub-sessions or tokens."""
    workers = _all_workers(office_view or {})
    conflicted = _UnionRuns(conflicted, conflicted_runs([w for _, w in workers], None, None))
    by_run: dict[Any, dict[str, Any]] = {}
    for _, w in workers:
        if w.get("run_id") and (w["run_id"] not in by_run or w.get("kind") == "child_task"):
            by_run[w["run_id"]] = w
    rows: dict[str, dict[str, Any]] = {}
    hosts: list[str] = []
    cut = False
    conflicts = 0
    for h, hb in sorted(beats.items(), key=lambda kv: (kv[1].get("received_at") or 0, kv[0])):
        block = hb.get("tasks")
        if not isinstance(block, dict):
            continue
        hosts.append(h)
        cut = cut or block.get("truncated") is True
        if _num(block.get("conflicts")):
            conflicts += int(block["conflicts"])
        stale = now - (hb.get("received_at") or 0) > (hb.get("ttl_s") or TTL_DEFAULT_S)
        for e in block.get("items") or []:
            tid = e.get("task_id")
            if e.get("host") != h:
                continue
            k = known.get(tid)
            if k is not None:
                if k.get("unverified"):
                    continue                        # this machine's own record could not be read: no claim on a guess
                if (k.get("host") and k["host"] != h) or \
                        (k.get("origin_session") and e.get("origin_session") != k["origin_session"]) or \
                        (k.get("stub") and e.get("origin_machine") and e["origin_machine"] != k.get("origin_machine")):
                    # the provider host's own record names another host or origin; or its stub (written here, at the
                    # start) another machine tag than the report claims: a host never moves a task into a machine's
                    # untagged group (a report without the tag, an older host, is no claim: the stub's is kept below)
                    conflicts += 1
                    continue
                if not k.get("stub"):
                    continue                        # the provider host's own log has it: first-hand, it wins
                # a remote stub (started from here onto host h, same origin): h's own report replaces it
            if tid in rows:
                if not HeartbeatBoard.same_claim(rows[tid]["_bind"], HeartbeatBoard.binding(e)):
                    conflicts += 1
                continue
            start, end = e.get("started_at"), e.get("ended_at")
            if max(end or 0, start or 0) < now - CHILD_WINDOW_S:
                continue                            # a day-old heartbeat's finished tasks age out as the log's do
            state, phase = e["state"], e.get("phase")
            if state in ("running", "queued"):
                if stale:
                    state, phase = "unknown", STALE_HB_PHASE
                elif start is not None and now - start > STALE_RUNNING_S:
                    state, phase = "unknown", "no end event after 10 h"
            origin = e.get("origin_session") if tid not in conflicted else None   # the run-wide veto
            # the machine tag: the provider host's own stub's when it has one (first-hand), else the host's report
            machine = k.get("origin_machine") if k is not None and k.get("stub") else e.get("origin_machine")
            w = by_run.get(tid) or {}
            if not origin or w.get("origin") != origin:
                w = {}                              # the provider never saw this run carry the task's origin
            elapsed = ((end if end is not None else now) - start) if start is not None else None
            rows[tid] = {
                "task_id": tid, "title": e.get("title") or w.get("title"), "state": state, "phase": phase, "host": h,
                "client": None, "provider": w.get("provider"), "lane": e.get("lane") or w.get("lane"),
                "model": w.get("model") or e.get("model"),
                "tool": w.get("tool") if state == "running" and w.get("status") == "working" else None,
                "started_at": start, "ended_at": end,
                "elapsed_s": _r(max(0.0, elapsed)) if _num(elapsed) else None, "workspace": e.get("workspace"),
                "browser": e.get("browser") is True, "session": None, "project": None, "device": None,
                "origin_session": e.get("origin_session"), "origin_project": e.get("origin_project"),
                "origin_device": e.get("origin_device"), "origin_machine": machine,
                "origin_via": e.get("origin_via"),
                "parent_task_id": e.get("parent_task_id"), "bind_origin": origin, "_bind": HeartbeatBoard.binding(e)}
    for r in rows.values():
        del r["_bind"]
    return list(rows.values()), sorted(hosts), cut, conflicts


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
                            ("origin_device", office._HEADER_RULES[office.DEVICE_HEADER]),
                            ("origin_machine", child_tasks.MACHINE_TAG_RE)):
            v = office.clean(e.get(field), rule)
            if v is not None:
                c[field] = v
        parent = e.get("parent_task_id")
        if isinstance(parent, str) and child_tasks.TASK_ID_RE.fullmatch(parent) and parent != tid:
            c["parent_task_id"] = parent
        if e.get("origin_via") == "parent":
            c["origin_via"] = "parent"
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


def _all_workers(view: dict[str, Any]) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    """(floor-like {session, project, device} or None, worker) for EVERY worker on the board, before the Office's
    per-floor (32), lobby and overall caps, when the view carries them (OfficeBoard.view(uncapped=True): `workers_all`,
    each worker with its own session tag); else the capped lists (_office_workers). The orchestrator tree joins
    through this, so one session's workers never depend on how busy the other floors are (review 2026-11-14)."""
    if not isinstance(view.get("workers_all"), list):
        return _office_workers(view)
    out = []
    for w in view["workers_all"]:
        if isinstance(w, dict):
            out.append(({"session": w["session"], "project": w.get("project"), "device": w.get("device")}
                        if w.get("session") else None, w))
    return out


def _elsewhere(stubs: list | None, known: set[str], now: float, cap: int | None = ELSEWHERE_MAX) -> list[dict[str, Any]]:
    """Remote stubs re-validated (the tasks dir is a file tree, read as untrusted), minus any task the local log already
    shows, newest first, within STALE_RUNNING_S (no child runs longer: see the constant). `cap` None: every one (the
    tree filters by origin before it caps)."""
    out = []
    for s in stubs or []:
        if not isinstance(s, dict):
            continue
        tid, host, at = office.clean(s.get("task_id"), office._RUN_ID_RE), office.clean(s.get("host"), _HOST_RE), \
            s.get("created_at")
        if tid is None or host is None or tid in known or not _num(at) or not now - STALE_RUNNING_S <= at <= now:
            continue
        org = {k: office.clean(s.get(k), rule) for k, rule in _ORIGIN_RULES}
        out.append({"task_id": tid, "host": host, "started_at": round(float(at), 3),
                    **{k: v for k, v in org.items() if v}})   # who started it, when the stub recorded it
    out.sort(key=lambda x: -x["started_at"])
    return out if cap is None else out[:cap]


def tasks_section(events: list[dict[str, Any]], office_view: dict[str, Any] | None,
                  peek: Callable[[str], dict[str, Any] | None] | None, now: float,
                  errors: list | None = None, stubs: list | None = None, session: str | None = None,
                  rows_out: list | None = None, peek_foreign: bool = True, task_errors_out: list | None = None,
                  elsewhere_out: list | None = None) -> dict[str, Any]:
    """The tasks section. With `session` (a valid ?session= value) running, queued and recent hold only that session's
    tasks, filtered BEFORE the caps: filtering the capped fleet lists lost a busy fleet's older tasks from the session's
    view while its origin group still listed them (review 2026-11-13). Counts and origins stay fleet-wide. `rows_out`
    (a list) receives every row of the window, uncapped, for the orchestrator tree (orchestrators_section);
    `elsewhere_out` every remote stub, uncapped, for the same; `task_errors_out` a (task id, error) pair for each
    task-dir error, so the tree publishes only the errors of tasks it shows.

    With `session`, that session's open tasks are peeked first (PEEK_MAX is shared: other sessions' tasks must not use
    it up), and with `peek_foreign` False (the session endpoint) no other task's directory is opened at all."""
    errors = list(errors or [])
    by_run = {w.get("run_id"): w for _, w in _all_workers(office_view or {}) if w.get("kind") == "child_task"}
    kids = _children(events, now)
    own: set[str] | None = None
    if session is not None:
        lite = {tid: {"task_id": tid, "origin_session": c.get("origin_session"),
                      "parent_task_id": c.get("parent_task_id")} for tid, c in kids.items()}
        own = {tid for tid, r in lite.items() if _origin_of(lite, r)[0] == session}
    peeked: dict[str, Any] = {}
    if peek is not None:
        open_ids = [tid for tid, c in kids.items() if c["final"] is None]
        if own is not None:                         # stable: the fleet view peeks in log order, as before
            open_ids.sort(key=lambda t: t not in own)
            if not peek_foreign:
                open_ids = [t for t in open_ids if t in own]
        for tid in open_ids[:PEEK_MAX]:
            try:
                peeked[tid] = peek(tid)
            except Exception as exc:                # one bad dir must not erase the section
                e = _err("task_dir", exc)
                errors.append(e)
                if task_errors_out is not None:
                    task_errors_out.append((tid, e))
    rows = []
    for tid, c in kids.items():
        w = by_run.get(tid) or {}
        state, phase, ws = c["final"], None, c["workspace"]
        if state is None:
            p = peeked.get(tid)
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
            "origin_device": c.get("origin_device"), "origin_via": c.get("origin_via"),
            "parent_task_id": c.get("parent_task_id"),
            "origin_machine": c.get("origin_machine"),     # internal (_PRIVATE_KEYS): stripped from what is published
        })
    if rows_out is not None:
        rows_out.extend(rows)
    counts = {k: 0 for k in ("running", "queued", "succeeded", "failed", "cancelled", "unknown")}
    for r in rows:
        counts[r["state"]] += 1
    listed = rows if session is None else [r for r in rows if r.get("origin_session") == session]
    running = sorted([r for r in listed if r["state"] == "running"], key=lambda r: -(r["started_at"] or 0))
    queued = sorted([r for r in listed if r["state"] == "queued"], key=lambda r: r["started_at"] or 0)
    recent = sorted([r for r in listed if r["state"] not in ("running", "queued")],
                    key=lambda r: -(r["ended_at"] or r["started_at"] or 0))
    truncated = len(running) > RUNNING_MAX or len(queued) > RUNNING_MAX or len(recent) > RECENT_MAX
    away = _elsewhere(stubs, set(kids), now, cap=None)
    if elsewhere_out is not None:
        elsewhere_out.extend(away)
    return _section(now, _status(errors, bool(rows) or bool(away)), errors, scope=SCOPE, window_s=int(CHILD_WINDOW_S),
                    counts=counts, running=_pub(running[:RUNNING_MAX]), queued=_pub(queued[:RUNNING_MAX]),
                    recent=_pub(recent[:RECENT_MAX]), truncated=truncated, elsewhere=_pub(away[:ELSEWHERE_MAX]),
                    **origins_of(rows))


def _pub(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of task rows as published: without the internal capability fields (_PRIVATE_KEYS)."""
    return [{k: v for k, v in r.items() if k not in _PRIVATE_KEYS} for r in rows]


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


# ================================================================ the orchestrator tree
def _origin_of(rows_by_id: dict[str, dict[str, Any]], r: dict[str, Any]) -> tuple[str | None, str | None]:
    """(origin, via): a task's own origin_session, else the nearest ancestor's through parent_task_id (via "parent"):
    a nested task with no orchestrator of its own belongs to its root's. (None, None): unattributed."""
    if r.get("origin_session"):
        return r["origin_session"], r.get("origin_via")
    seen, cur = {r.get("task_id")}, r
    while cur.get("parent_task_id") and cur["parent_task_id"] not in seen and len(seen) <= 16:
        seen.add(cur["parent_task_id"])
        cur = rows_by_id.get(cur["parent_task_id"])
        if cur is None:
            break
        if cur.get("origin_session"):
            return cur["origin_session"], "parent"
    return None, None


class _AllRuns:
    """Every run id: what conflicted_runs gives when a source of origin observations could not be read."""
    def __contains__(self, rid: Any) -> bool:
        return True


class _UnionRuns:
    """Membership in any of `parts` (sets, or _AllRuns)."""
    def __init__(self, *parts: Any):
        self._parts = parts

    def __contains__(self, rid: Any) -> bool:
        return any(rid in p for p in self._parts)


def conflicted_runs(workers: list | None, conversations: list | None, run_origins: Any,
                    unknown: bool = False) -> Any:
    """The run ids a heartbeat row may join nothing of (review round 5, P2): every origin the provider observed for a
    run, on any record (an Office worker's `origin`, each conversation's of key "r:<run>" in `conversations`, a list
    of lists, and RunRecords' run origin), taken together. A run seen with ORIGIN_CONFLICT anywhere, with two
    different origins across its records, or with a value that is no origin at all is conflicted: one record that
    agrees with a heartbeat cannot outvote another that does not. `unknown` (a source of observations could not be
    read this time): every run, so a missed observation never lets a join through."""
    if unknown:
        return _AllRuns()
    seen: dict[str, set] = {}

    def note(rid: Any, o: Any) -> None:
        if not isinstance(rid, str) or o is None:
            return                                  # no x-cortados-origin on that record: no observation
        ok = office.clean(o, office._ORIGIN_RE) if o != office.ORIGIN_CONFLICT else None
        seen.setdefault(rid, set()).add(ok if ok is not None else office.ORIGIN_CONFLICT)
    for w in workers or []:
        if isinstance(w, dict):
            note(w.get("run_id"), w.get("origin"))
    for src in conversations or []:
        for c in src if isinstance(src, list) else []:
            if isinstance(c, dict) and isinstance(c.get("key"), str) and c["key"].startswith("r:"):
                note(c["key"][2:], c.get("origin"))
    for rid, o in (run_origins.items() if isinstance(run_origins, dict) else ()):
        note(rid, o)
    return {rid for rid, s in seen.items() if office.ORIGIN_CONFLICT in s or len(s) > 1}


def _sub_node(c: dict[str, Any], now: float) -> dict[str, Any] | None:
    """One sub-session (a Registry conversation of a run, open or recently closed) as a tree node, re-validated."""
    if not isinstance(c, dict):
        return None
    opened = c.get("opened_at") if _num(c.get("opened_at")) else None
    closed = c.get("closed_at") if _num(c.get("closed_at")) else None
    busy, waiting = c.get("busy") is True, c.get("awaiting_tool") is True
    if closed is not None:
        status = "failed" if c.get("closed") == "failed" else "done"
        phase = {"failed": "failed", "exited": "backend exited", "reaped": "closed idle",
                 "rebuilt": "rebuilt"}.get(c.get("closed"), "closed")
    else:
        status = "running" if busy or waiting else "waiting"
        phase = "model turn" if busy else ("running a tool" if waiting else "idle between turns")
    ref = office.clean(c.get("ref"), office._RUN_ID_RE)
    return {"ref": ref, "role": "subagent", "status": status, "phase": phase,
            "provider": c.get("provider") if c.get("provider") in office.PROVIDERS else None,
            "lane": office.clean(c.get("lane"), office._LANE_RE), "model": office.clean(c.get("model"), office._MODEL_RE),
            "started_at": _r(opened, 3), "ended_at": _r(closed, 3), "last_activity": _r(c.get("last_used"), 3),
            "elapsed_s": _r(max(0.0, (closed if closed is not None else now) - opened)) if opened is not None else None,
            "tokens": work_split.pub_tokens(c.get("tokens"))}


def _subs_by_run(backend: list | None, ended: list | None, now: float) -> dict[str, list[dict[str, Any]]]:
    """run id -> its sub-session nodes: the open conversations whose office key is "r:<run id>" and the recently closed
    ones (Registry.ended, within the task window). The first-opened is the run's main loop (role "main"): Claude Code
    opens a child's main conversation before any subagent's. Ordered main, running, waiting, then newest."""
    out: dict[str, list[dict[str, Any]]] = {}
    for src in (backend or [], ended or []):
        for c in src:
            if not isinstance(c, dict) or not isinstance(c.get("key"), str) or not c["key"].startswith("r:"):
                continue
            rid = office.clean(c["key"][2:], office._RUN_ID_RE)
            if rid is None:
                continue
            if _num(c.get("closed_at")) and now - c["closed_at"] > CHILD_WINDOW_S:
                continue
            n = _sub_node(c, now)
            if n is not None:
                # the origin the provider saw on this conversation's requests (x-cortados-origin), compared (never
                # published) for a heartbeat's task: _task_subs
                n["_origin"] = office.clean(c.get("origin"), office._ORIGIN_RE)
                out.setdefault(rid, []).append(n)
    rank = {"running": 0, "waiting": 1, "failed": 2, "done": 3}
    for rid, subs in out.items():
        first = min(subs, key=lambda n: (n["started_at"] if n["started_at"] is not None else math.inf))
        first["role"] = "main"
        subs.sort(key=lambda n: (n["role"] != "main", rank.get(n["status"], 4), -(n["started_at"] or 0)))
    return out


def _task_subs(r: dict[str, Any], subs: list[dict[str, Any]], tokens: Any,
               run_origin: Any = None, conflicted: bool = False) -> tuple[list[dict[str, Any]], Any]:
    """A task's sub-sessions and run tokens, as published. For a row from another host's heartbeat (`bind_origin`)
    only the run's conversations whose requests the provider saw carry the task's bound origin join, and its run
    tokens only when the run's own observed origin (`run_origin`, RunRecords.run_origins) is that origin: the run id
    and the entry's tags are the other host's word (heartbeat_rows). The provider host's own rows (its log, its stubs)
    join by run id, as before: first-hand. `conflicted` (the run is in conflicted_runs: seen with two origins
    anywhere) vetoes every join of a heartbeat row, whatever each conversation's own origin (review round 5, P2)."""
    if "bind_origin" in r:
        o = r["bind_origin"] if not conflicted else None
        subs = [x for x in subs if o and x.get("_origin") == o]
        if not (o and run_origin == o):
            tokens = None
    return [{k: v for k, v in x.items() if k != "_origin"} for x in subs], tokens


def _task_node(r: dict[str, Any], subs: list[dict[str, Any]], tokens: Any, via: str | None,
               run_origin: Any = None, conflicted: bool = False) -> dict[str, Any]:
    subs, tokens = _task_subs(r, subs, tokens, run_origin, conflicted)
    st = r.get("state")
    sub = subs[:SUBSESSIONS_MAX]
    main = next((x for x in subs if x.get("role") == "main"), None) or {}   # a task with no model of its own: its loop's
    return {"task_id": r.get("task_id"), "title": r.get("title"), "state": st, "status": _TASK_STATUS.get(st, "unknown"),
            "phase": r.get("phase"), "tool": r.get("tool"), "host": r.get("host"), "provider": r.get("provider"),
            "lane": r.get("lane") or main.get("lane"), "model": r.get("model") or main.get("model"),
            "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"), "elapsed_s": r.get("elapsed_s"), "workspace": r.get("workspace"),
            "browser": bool(r.get("browser")), "parent_task_id": r.get("parent_task_id"), "parent_hidden": False,
            "origin_via": via,
            "tokens": work_split.pub_tokens(tokens), "sub_sessions": sub,
            "sub_sessions_hidden": max(0, len(subs) - SUBSESSIONS_MAX),
            "sub_sessions_failed": sum(1 for x in subs if x.get("status") == "failed"),   # all, listed or not
            "tasks": []}


def _rank_task(n: dict[str, Any]) -> tuple:
    order = {"running": 0, "waiting": 1, "failed": 2, "unknown": 3, "done": 4}
    return (order.get(n["status"], 5), -(n.get("ended_at") or n.get("started_at") or 0))


def orchestrators_section(rows: list[dict[str, Any]], elsewhere: list[dict[str, Any]] | None,
                          conv: dict[str, Any] | None, backend: list | None, ended: list | None,
                          run_tokens: dict[str, Any] | None, now: float, errors: list | None = None,
                          session: str | None = None, inputs_truncated: list | None = None,
                          task_hosts: list | None = None, conflicts: int | None = None,
                          run_origins: dict[str, Any] | None = None,
                          conflicted: Any = frozenset(), untagged_machine: str | None = None) -> dict[str, Any]:
    """The orchestrator tree: each orchestrating main session (its Office tag), the bot gateway ("grokbot") and an
    unattributed group, each with the child tasks it started in the window (nested under their parent task through
    parent_task_id) and, under each task, its sub-sessions (the run's routed conversations, x-cortados-run-id).

    Every node is metadata only, built from already-validated rows and conversation records. With `session` only that
    origin's group is built, BEFORE any cap, and the unattributed group is never included: a session's view can hold
    nothing of another session (docs/PULSE-PER-SESSION.md). `rows`, `elsewhere` and `conv` items must be the uncapped
    inputs (collect bounds them by TREE_INPUT_MAX and names a cut input in `inputs_truncated`).

    `errors`: a source's error (a dict) applies to every view; a (task id, error) pair belongs to that task and is
    published only when the task is in this tree (a foreign task's PermissionError is not this session's failure).
    `attention` counts failed and unknown tasks, plus each failed sub-session of a task that is neither (a failed task
    is counted once, however many of its sub-sessions failed).

    `task_hosts` (collect passes it): the hosts whose heartbeat carried task entries, already merged into `rows`
    (heartbeat_rows). The section's scope is then TREE_SCOPE and names them; None keeps the provider host's SCOPE.
    `conflicts` (the fleet view only): heartbeat claims on a task id that were dropped because the id is bound to
    another host or origin; published as a count, never with the ids or what was claimed.
    `run_origins` (run id -> the origin the provider observed on the run's requests): a heartbeat row's run tokens
    join only through it (_task_subs). `conflicted`: run ids that join nothing (conflicted_runs; collect's covers the
    Office workers too); the runs this function's own inputs show conflicted are added to it.
    `untagged_machine` (with `session` only, valid_machine): the tasks that carry NO origin tag at all (own or through
    a parent) and whose OWN recorded origin_machine is exactly this tag are built as one more group, kind "untagged",
    origin None: tasks started through a cortados-route server that knew no session (the Claude desktop app's own
    server). The tag is a keyed hash of the starting machine's 0600 Office salt (subagent_mcp.machine_tag), so only a
    caller on that machine can name it; the hostname (origin_device) is display only and opens nothing. A task with any
    origin tag, the bot's included, or with no origin_machine (older tasks, a server without the salt) never enters
    it (docs/PULSE-PER-SESSION.md §11)."""
    run_tokens = run_tokens if isinstance(run_tokens, dict) else {}
    run_origins = run_origins if isinstance(run_origins, dict) else {}
    conflicted = _UnionRuns(conflicted, conflicted_runs(None, [backend, ended], run_origins))
    subs = _subs_by_run(backend, ended, now)
    by_id = {r["task_id"]: r for r in rows if r.get("task_id")}
    away = [{"task_id": x["task_id"], "title": None, "state": "elsewhere", "phase": "runs on " + x["host"] +
             "; its state is on that host", "host": x["host"], "started_at": x["started_at"], "ended_at": None,
             "elapsed_s": None, "origin_session": x.get("origin_session"), "origin_project": x.get("origin_project"),
             "origin_device": x.get("origin_device"), "origin_machine": x.get("origin_machine")}
            for x in elsewhere or [] if x.get("task_id") not in by_id]
    for x in away:
        by_id[x["task_id"]] = x
    groups: dict[Any, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    origin_of: dict[str, Any] = {}
    for r in list(by_id.values()):
        origin, via = _origin_of(by_id, r)
        if session is not None and origin != session and not (
                untagged_machine is not None and origin is None and r.get("origin_machine") == untagged_machine):
            continue                                # filtered before anything is built or capped
        origin_of[r["task_id"]] = origin
        nodes[r["task_id"]] = _task_node(r, subs.get(r["task_id"], []), run_tokens.get(r["task_id"]), via,
                                         run_origins.get(r["task_id"]), r["task_id"] in conflicted)
        g = groups.setdefault(origin, {"project": None, "device": None})
        g["project"] = g["project"] or r.get("origin_project")
        g["device"] = g["device"] or r.get("origin_device")
    errors = [e[1] if isinstance(e, tuple) else e for e in errors or []
              if not isinstance(e, tuple) or e[0] in nodes]

    def nests(tid: str) -> bool:
        """Whether the task hangs under its parent: the parent is in the same group, and the chain of same-group
        parents above it ends at a top-level task within NEST_MAX steps. A cycle (a parent chain that comes back) or a
        longer chain is drawn flat: every task of it stays visible at the top, none is lost inside the loop."""
        seen, cur = [tid], tid
        while True:
            parent = nodes[cur].get("parent_task_id")
            if parent not in nodes or origin_of.get(parent) != origin_of[tid]:
                return len(seen) > 1                # reached a top-level task: nested iff tid is not itself the top
            if parent in seen or len(seen) > NEST_MAX:
                return False
            seen.append(parent)
            cur = parent

    flat = {tid for tid in nodes if not nests(tid)}
    tops: dict[Any, list[dict[str, Any]]] = {}
    for tid, n in nodes.items():
        parent = n.get("parent_task_id")
        if tid not in flat:                         # its parent is in the group and its chain ends at a top task
            nodes[parent]["tasks"].append(n)
        else:
            tops.setdefault(origin_of[tid], []).append(n)
        if parent is not None and parent not in nodes and session is not None:
            # a parent outside this session's tree (another orchestrator's task): never named in a session's view
            n["parent_task_id"], n["parent_hidden"] = None, True

    def finish(n: dict[str, Any], counts: dict[str, int]) -> None:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
        # a failed or unknown task once; otherwise each of its failed sub-sessions (a running task whose subagent
        # failed needs a look too)
        counts["_attention"] = counts.get("_attention", 0) + (
            1 if n["status"] in ("failed", "unknown") else n["sub_sessions_failed"])
        n["tasks"].sort(key=_rank_task)
        for k in n["tasks"]:
            finish(k, counts)

    # Live orchestrators (conversations items of kind orchestrator: a heartbeat or the routed main loop) and their own
    # routed subagents, by Office session tag.
    live_orch: dict[str, dict[str, Any]] = {}
    routed: dict[str, list[dict[str, Any]]] = {}
    for it in (conv or {}).get("items") or []:
        sess = it.get("session")
        if not sess or (session is not None and sess != session):
            continue
        if it.get("kind") == "orchestrator":
            live_orch.setdefault(sess, it)
        elif it.get("kind") == "subagent" and it.get("run_id") not in by_id:   # a task's own run is its sub-sessions
            routed.setdefault(sess, []).append({
                "ref": it.get("run_id"), "role": "subagent", "title": it.get("title"),
                "status": "running" if it.get("status") == "working" else "waiting",
                "phase": it.get("phase"), "tool": it.get("tool"), "provider": it.get("provider"), "lane": it.get("lane"),
                "model": it.get("model"), "started_at": it.get("started_at"), "elapsed_s": it.get("elapsed_s"),
                "tokens": it.get("tokens")})
    out = []
    for origin in set(tops) | set(live_orch) | set(routed):
        top = sorted(tops.get(origin, []), key=_rank_task)
        counts: dict[str, int] = {}
        for n in top:
            finish(n, counts)
        o = live_orch.get(origin) if origin else None
        g = groups.get(origin) or {}
        # with a session filter the only origin-less group is this machine's untagged tasks (see untagged_machine)
        kind = ("untagged" if session is not None else "unattributed") if origin is None \
            else "bot" if origin in child_tasks.ORIGIN_NAMED else "session"
        rs = routed.get(origin, []) if origin else []
        newest = max([n.get("ended_at") or n.get("started_at") or 0 for n in top] +
                     [(o or {}).get("last_activity") or 0], default=0)
        out.append({
            "origin": origin, "kind": kind,
            "project": g.get("project") or (o or {}).get("project"), "device": g.get("device") or (o or {}).get("device"),
            "status": (o or {}).get("status") or ("n/a" if kind != "session" else "not live"),
            "model": (o or {}).get("model"), "tool": (o or {}).get("tool"),
            "last_activity": _r(newest, 3) if newest else None,
            "counts": {k: counts.get(k, 0) for k in ("running", "waiting", "done", "failed", "unknown")},
            "attention": counts.get("_attention", 0),
            "tasks": top[:ORCH_TASKS_MAX], "tasks_hidden": max(0, len(top) - ORCH_TASKS_MAX),
            "routed": rs[:ORCH_ROUTED_MAX], "routed_hidden": max(0, len(rs) - ORCH_ROUTED_MAX)})
    # active first (a running or waiting task, a working main session), the unattributed group last among equals,
    # then by the newest activity
    out.sort(key=lambda x: (not (x["counts"]["running"] or x["counts"]["waiting"] or x["status"] == "working"),
                            x["kind"] in ("unattributed", "untagged"), -(x["last_activity"] or 0)))
    cut = sorted(set(inputs_truncated or []))
    fields = {"scope": SCOPE if task_hosts is None else TREE_SCOPE, "window_s": int(CHILD_WINDOW_S),
              "items": out[:ORCH_MAX], "truncated": len(out) > ORCH_MAX or bool(cut), "inputs_truncated": cut}
    if task_hosts is not None:
        fields["task_hosts"] = sorted(task_hosts)   # host names only: fleet health, no session in it
    if conflicts is not None:
        fields["conflicts"] = conflicts
    if session is not None:
        fields["session_filter"] = session
    return _section(now, _status(errors, bool(out)), errors, **fields)


SESSION_PATH = "/v1/cortados/live/session"


def session_view(snap: dict[str, Any], session: str) -> dict[str, Any]:
    """GET /v1/cortados/live/session?session=<tag>: the pulse pane's view. ONLY that session's orchestrator subtree
    (built filtered, see orchestrators_section) plus fleet health that names no session (S1, the hosts' job
    heartbeats and hardware, the lowest subscription window). No task, conversation, routed call or count of any
    other session is in it. `snap` must come from collect(session=session). With `&machine=<tag>` (collect's
    untagged_machine) the orchestrators also hold the untagged group of the machine whose salt that tag comes from
    (kind "untagged": tasks that carry no origin tag at all), never another session's or another machine's."""
    o = snap.get("overview") or {}
    return {"version": VERSION, "generated_at": snap.get("generated_at"), "session_filter": session,
            "overview": {"s1": o.get("s1"), "s1_latency_ms": o.get("s1_latency_ms"),
                         "lowest_subscription": o.get("lowest_subscription"),
                         "hosts_with_heartbeat": o.get("hosts_with_heartbeat")},
            "orchestrators": snap.get("orchestrators"), "s1": snap.get("s1"), "hosts": snap.get("hosts"),
            # this session's own work split row only (work_split.session_split): no other session, no fleet totals
            "work_split": work_split.session_split(snap.get("work_split") or {}, session)}


def for_session(snap: dict[str, Any], session: str) -> dict[str, Any]:
    """GET /v1/cortados/live?session=<tag>: the snapshot narrowed to one orchestrating session: its tasks (running,
    queued, recent, by origin_session), its origin group, and the conversations on its floor. Counts, overview and
    the other sections stay fleet-wide. `session` must already be valid (valid_session). collect(session=) calls it
    on a snapshot whose task lists were filtered before their caps; here the lists are only narrowed again."""
    t = dict(snap.get("tasks") or {})
    for k in ("running", "queued", "recent"):
        t[k] = [r for r in t.get(k) or [] if r.get("origin_session") == session]
    t["origins"] = [g for g in t.get("origins") or [] if g.get("session") == session]
    c = dict(snap.get("conversations") or {})
    c["items"] = [it for it in c.get("items") or [] if it.get("session") == session]
    o = dict(snap.get("orchestrators") or {})
    # the session's own group, and the machine's untagged group when it was asked for (orchestrators_section)
    o["items"] = [g for g in o.get("items") or [] if g.get("origin") == session or g.get("kind") == "untagged"]
    o["session_filter"] = session
    return {**snap, "session_filter": session, "tasks": t, "conversations": c, "orchestrators": o}


def valid_machine(v: Any) -> str | None:
    """A ?machine= value (child_tasks.MACHINE_TAG_RE: the tag cortados-route records as origin_machine), else None."""
    return v if isinstance(v, str) and child_tasks.MACHINE_TAG_RE.fullmatch(v) else None


def valid_session(v: Any) -> str | None:
    """A ?session= value the filter accepts (an Office tag or a named origin), else None."""
    return v if isinstance(v, str) and _ORIGIN_SESSION_RE.fullmatch(v) else None


_TAG_RULES = (("session", office._HEADER_RULES[office.SESSION_HEADER]),
              ("project", office._HEADER_RULES[office.PROJECT_HEADER]),
              ("device", office._HEADER_RULES[office.DEVICE_HEADER]))


def _backend_items(backend: list, view: dict[str, Any], now: float) -> tuple[list[dict[str, Any]], set[str]]:
    """One item per conversation the provider holds open (Registry.live_conversations), joined with the Office board by
    its office key, and the office run ids / main-loop sessions those conversations cover. Every field is re-validated
    here: these records come from in-memory sessions whose models and lanes the routes chose.

    Ownership (session, project, device) is the conversation's OWN Office tags, which the registry keeps per
    conversation, uncapped; the board (its uncapped workers when the view has them) only fills what a conversation
    does not carry. Joining through the board's capped floors lost a session's 33rd routed agent (review 2026-11-14)."""
    workers = {w.get("run_id"): (f, w) for f, w in _all_workers(view) if w.get("run_id")}
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
        own = {k: office.clean(c.get(k), rule) for k, rule in _TAG_RULES}
        if kind_of == "main" and own["session"] is None:
            own["session"] = office.clean(ident, _TAG_RULES[0][1])    # the main loop's key is its session tag
        tag = {k: own[k] or (floor or {}).get(k) for k in own}
        items.append({
            "kind": kind, "run_id": office.clean(ident, office._RUN_ID_RE) if kind_of == "r" else
            office.clean(c.get("ref"), office._RUN_ID_RE),
            "session": tag["session"], "project": tag["project"], "device": tag["device"],
            "provider": c.get("provider") if c.get("provider") in office.PROVIDERS else w.get("provider"),
            "lane": office.clean(c.get("lane"), office._LANE_RE) or w.get("lane"),
            "model": office.clean(c.get("model"), office._MODEL_RE) or w.get("model"),
            "status": status, "tool": w.get("tool") if status == "working" and w.get("status") == "working" else None,
            "phase": "model turn" if busy else ("client running a tool" if waiting else "waiting for the client"),
            "source": "provider",
            "title": w.get("title") or office.clean(c.get("title"), office._HEADER_RULES[office.TITLE_HEADER]),
            "started_at": _r(opened, 3),
            "last_activity": _r(c.get("last_used"), 3), "tokens": work_split.pub_tokens(c.get("tokens"))})
    return items, covered


def conversations_section(view: dict[str, Any], backend_sessions: int | None, now: float,
                          backend: list | None = None, errors: list | None = None,
                          items_out: list | None = None) -> dict[str, Any]:
    """In-flight routed work. With `backend` (the Registry's open conversations), one item per open conversation,
    child tasks' included; then each floor's orchestrator known from a heartbeat (a main session that does not route
    itself), and Office workers that are working or idle but hold no open conversation (one-shot calls). Without it,
    the Office view alone (orchestrators and non-child workers), as before. `items_out` (a list) receives every item,
    before the CONVERSATIONS_MAX cap and from the board's uncapped workers (_all_workers), for the orchestrator tree."""
    items, covered = _backend_items(backend, view, now) if backend is not None else ([], set())
    items += _floor_orchestrators(view, covered)
    tree = items + _worker_items(_all_workers(view), covered) if items_out is not None else None
    items += _worker_items(_office_workers(view), covered)
    for lst in (items, tree or []):
        for it in lst:
            it["elapsed_s"] = _r(max(0.0, now - it["started_at"])) if _num(it.get("started_at")) else None
        lst.sort(key=lambda it: (it["status"] != "working", -(it.get("started_at") or 0)))
    if items_out is not None:
        items_out.extend(tree)
    errors = [e for e in view.get("errors") or [] if isinstance(e, dict)] + list(errors or [])
    return _section(now, _status(errors, True), errors, backend_sessions=backend_sessions,
                    active=len(items), working=sum(1 for it in items if it["status"] == "working"),
                    floors=len(view.get("floors") or []), items=items[:CONVERSATIONS_MAX],
                    hidden=max(0, len(items) - CONVERSATIONS_MAX))


def _floor_orchestrators(view: dict[str, Any], covered: set[str]) -> list[dict[str, Any]]:
    """Each floor's orchestrator known from a heartbeat whose main loop holds no open conversation here."""
    items = []
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
    return items


def _worker_items(pairs: list, covered: set[str]) -> list[dict[str, Any]]:
    """Office workers that are working or idle but hold no open conversation (one-shot calls), from `pairs`
    (_office_workers for the fleet list, _all_workers for the tree)."""
    items = []
    for f, w in pairs:
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
    return items


_CALL_KEYS = ("ts", "task_id", "session_id", "routing_kind", "band", "lane", "model", "actual_model", "elapsed_ms",
              "degraded", "s1_reason", "request_chars", "response_chars", "sensitive_cloud")


_LOG_OPS = ("log_route", "log_execution", "log_outcome", "log_tokens", "log_sensitive_cloud")
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
    totals = dict(view.get("totals") or {})
    if view.get("token_totals") is not None:
        totals["tokens"] = view["token_totals"]     # per window: overall, by orchestrator session tag, by lane/model
    return _section(now, _status(errors, bool(calls)), errors, calls=calls, totals=totals,
                    row_limit=view.get("row_limit"), more=len(view.get("tasks") or []) > ROUTED_MAX,
                    route_log_errors=log_errors)


def _general(w: dict[str, Any]) -> bool:
    return w.get("pool") in (None, "all") and not w.get("stale") and _num(w.get("left_pct"))


def lane_enabled(lane: Any) -> bool:
    """Whether the router can send work to `lane` at all, by the checks routing itself makes: the anthropic-api credit
    lane only while it is on (key, grants, ledger, stop line: api_credits.state; the provider refuses every API turn
    otherwise: server._api_turn), any lane
    only with a context ceiling above 0 (core.resolve's fit gate refuses every turn on a 0 ceiling: antigravity's
    containment). A disabled lane's reading (no key: 0 % of a $0 budget) never stands for the fleet's floor."""
    from cortados_router import api_credits, core
    if not isinstance(lane, str):
        return False
    base = lane.split("#", 1)[0]
    if base == "anthropic-api":
        return bool(api_credits.state(time.time()).get("on"))
    return core.lane_ceiling(lane) > 0


def api_credit_section(st: dict[str, Any] | None, now: float, errors: list | None = None) -> dict[str, Any]:
    """The API credit lane (#114) from api_credits.state: on / off and why, dollars used / left under the 95 % stop line,
    the binding budget's % used, the alert level (50 / 80 / 95), the soonest counting grant's expiry and whether the
    expiry preference is active. Metadata only: never the key, the config path or a request."""
    errs = list(errors or [])
    if not isinstance(st, dict):
        return _section(now, "error", errs or [_err("api credit", "unavailable")], on=False, reason=None,
                        alert_level=0)
    grant = st.get("grant") if isinstance(st.get("grant"), dict) else None
    month = st.get("month") if isinstance(st.get("month"), dict) else None
    exp = grant.get("expires_at") if grant else None
    return _section(now, "ok", errs, on=bool(st.get("on")), reason=st.get("reason"),
                    alert_level=st.get("alert_level") or 0, pct_used=_r(st.get("pct_used")),
                    headroom_usd=_r(st.get("headroom_usd"), 2), reserved_usd=_r(st.get("reserved_usd"), 2),
                    open_reservations=st.get("open_reservations"),
                    grant=({"amount_usd": _r(grant.get("amount_usd"), 2), "used_usd": _r(grant.get("used_usd"), 2),
                            "left_usd": _r(grant.get("left_usd"), 2), "stop_line_usd": _r(grant.get("stop_line_usd"), 2),
                            "expires_at": exp, "expires_in_h": round((exp - now) / 3600, 1) if _num(exp) else None}
                           if grant else None),
                    month=({"limit_usd": _r(month.get("limit_usd"), 2), "used_usd": _r(month.get("used_usd"), 2),
                            "stop_line_usd": _r(month.get("stop_line_usd"), 2), "resets_at": month.get("resets_at")}
                           if month else None),
                    grants=[{"name": g.get("name"), "amount_usd": _r(g.get("amount_usd"), 2),
                             "expires_at": g.get("expires_at"), "counting": bool(g.get("counting"))}
                            for g in (st.get("grants") or []) if isinstance(g, dict)],
                    expiring_soon=bool(st.get("expiring_soon")), prefer_within_days=st.get("prefer_within_days"))


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


def heartbeat_stale(hb: dict[str, Any], now: float) -> bool:
    """A stored heartbeat (HeartbeatBoard.snapshot) is stale past its own ttl_s since it was received."""
    return now - hb["received_at"] > hb["ttl_s"]


def fresh_hardware(heartbeats: dict[str, dict[str, Any]], now: float) -> list[tuple[str, dict[str, Any], float]]:
    """(host, hardware block, age_s) for each fresh heartbeat that carries hardware, by host name. The block is the one
    validate_heartbeat already checked (_HB_HW_KEYS only); age_s runs from its sampled_at, else from receipt. The one
    reader of the board's hardware: the hosts section here and the Usage view (usage._collect) both use it."""
    out = []
    for h, hb in sorted(heartbeats.items()):
        hh = hb.get("hardware")
        if not isinstance(hh, dict) or heartbeat_stale(hb, now):
            continue
        at = hh.get("sampled_at") if _num(hh.get("sampled_at")) else hb["received_at"]
        out.append((h, dict(hh), round(max(0.0, now - at), 1)))
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
        stale = heartbeat_stale(hb, now)
        it = item(h)
        it["heartbeat_state"] = "stale" if stale else "fresh"
        it["heartbeat"] = {"received_at": hb["received_at"], "age_s": round(age, 1), "ttl_s": hb["ttl_s"],
                           "stale": stale, "jobs": hb["jobs"], "s1": hb.get("s1")}
    for h, hh, hw_age in fresh_hardware(heartbeats, now):
        hw = {k: hh.get(k) for k in _HW_PICK if k != "chip"}
        hw.update(chip=hh.get("chip"), load_1m=hh.get("load_1m"), age_s=hw_age,
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
            session_stubs: Callable[[str], list] | None = None,
            lane_enabled: Callable[[Any], bool] | None = None,
            conversations: Callable[[], list] | None = None, self_name: str | None = None,
            route_log: Callable[[], dict[str, Any]] = router_db.stats,
            ended_conversations: Callable[[], list] | None = None,
            run_tokens: Callable[[], dict[str, Any]] | None = None,
            run_origins: Callable[[], dict[str, Any]] | None = None,
            stubs_by_id: Callable[[list[str]], list] | None = None,
            binding_errors: Callable[[], list] | None = None,
            api_credit: Callable[[], dict[str, Any]] | None = None,
            session: str | None = None, peek_foreign: bool = True,
            untagged_machine: str | None = None,
            machine_stubs: Callable[[str], list] | None = None) -> dict[str, Any]:
    """The snapshot. Each input is a callable, run under its own guard: one that raises turns ITS section into
    status "error" with the exception's type (never its text: an OSError carries a path), the rest stand.
    `session` (already valid, valid_session): the ?session= view (for_session), its tasks filtered before the caps.
    `peek_foreign` False (GET /v1/cortados/live/session): no other session's task directory is opened.
    `remote_stubs`: the fleet's stubs (child_tasks.remote_stubs, a scan of the newest task dirs) for the tasks section
    and the fleet tree; None skips the scan (the session endpoint). `session_stubs(origin)`: with `session`, that
    origin's own stubs (child_tasks.remote_stubs_for, the origin index), which then feed the tree instead of the fleet
    scan: neither other sessions' stubs nor the fleet scan's cut can change or flag this session's tree.
    `stubs_by_id(ids)` (child_tasks.remote_stubs_by_id): this machine's stubs of exactly the ids the heartbeats report,
    read before any session filtering, so ownership is checked against every stub here in every view (review round 4,
    P2); the records found are compared, never published. `run_origins`: run id -> the origin the provider saw on the
    run's requests (RunRecords.run_origins), the only key a heartbeat task's tokens join on. `binding_errors`: the
    heartbeat board's bindings-file trouble (HeartbeatBoard.errors), shown in the tree's errors. `untagged_machine`
    (with `session`, already valid, valid_machine): the tree also holds the tasks that carry no origin tag and that
    machine tag (orchestrators_section); `machine_stubs` (child_tasks.remote_stubs_for_machine) gives its remote stubs
    from the machine index, as `session_stubs` gives the session's."""
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
    rows: list = []
    away: list = []                                 # every remote stub, uncapped (the tasks section lists 50)
    task_errs: list = []                            # (task id, error) for each task-dir error
    if events is not None:
        tasks = tasks_section(events, ov, peek, now, child_errs + office_errs + stub_errs, stubs, session, rows,
                              peek_foreign=peek_foreign, task_errors_out=task_errs, elsewhere_out=away)
    else:
        away = _elsewhere(stubs, set(), now, cap=None)
        tasks = _section(now, "error", child_errs + office_errs + stub_errs, scope=SCOPE, window_s=int(CHILD_WINDOW_S),
                         counts=None, running=[], queued=[], recent=[], truncated=False,
                         elsewhere=_pub(away[:ELSEWHERE_MAX]), origins=[], origins_truncated=False)
    conv_errs: list = []
    backend = guard("conversations", conversations, conv_errs) if conversations is not None else None
    if isinstance(backend, list):
        backend_sessions = len(backend)             # the count from the same snapshot as the list it heads
    conv_items: list = []                           # every conversation item, uncapped (the section lists 64)
    if ov is not None or backend is not None:    # the open conversations stand without the board (partial)
        conv = conversations_section(ov if ov is not None else {"errors": office_errs}, backend_sessions, now,
                                     backend, conv_errs, items_out=conv_items)
    else:
        conv = _section(now, "error", office_errs + conv_errs, backend_sessions=backend_sessions, active=None,
                        working=None, floors=None, items=[], hidden=0)
    link_origins(conv, tasks)
    tree_errs: list = []
    # The hosts' heartbeats, read once: the tree takes their task entries (#118), the hosts section their jobs.
    hb_errs: list = []
    beats = guard("heartbeats", heartbeats, hb_errs) or {}
    ended = guard("ended conversations", ended_conversations, tree_errs) if ended_conversations is not None else None
    tok = guard("run tokens", run_tokens, tree_errs) if run_tokens is not None else None
    r_orig = guard("run tokens", run_origins, tree_errs) if run_origins is not None else None
    if binding_errors is not None:
        tree_errs += guard("task bindings", binding_errors, tree_errs) or []
    # The tree's inputs: uncapped, narrowed to the session's own origin FIRST, then bounded by TREE_INPUT_MAX with the
    # cut named, so no other session's activity can push this session's items out (review 2026-11-14). A session's
    # stubs come from its own origin index when the caller gives one: then every flag in `inputs_truncated` is about
    # this session's own data only.
    fleet_away = list(away)                         # the fleet scan's stubs (none in the session endpoint)
    own_index = session is not None and session_stubs is not None
    if own_index:
        own = guard("task stubs", lambda: session_stubs(session), tree_errs)
        away = _elsewhere(own, set(), now, cap=None)
        cut: list = ["task stubs"] if getattr(own, "truncated", False) is True else []
        if untagged_machine is not None and machine_stubs is not None:
            mine = guard("task stubs", lambda: machine_stubs(untagged_machine), tree_errs)
            seen = {x["task_id"] for x in away}
            away += [x for x in _elsewhere(mine, seen, now, cap=None)]
            if getattr(mine, "truncated", False) is True and "task stubs" not in cut:
                cut.append("task stubs")
    else:
        cut = ["task stubs"] if getattr(stubs, "truncated", False) is True else []
    # The board's memory (MAX_WORKERS LRU, fleet-wide) may have dropped a session's one-shot worker: said, per session.
    evicted = (ov or {}).get("workers_evicted")
    if isinstance(evicted, list) and (session in evicted if session is not None else evicted):
        cut.append("routed workers")
    # Tasks that ran on another host (its own log, which this machine cannot read) arrive in its heartbeat. The
    # provider host's own records are first-hand and bind their ids: its log rows (every row, before the session
    # filter), and EVERY remote stub it wrote for a start it sent elsewhere: the ones this view read, plus a read of
    # the stub of each id a heartbeat names (stubs_by_id), so another session's stub rejects a forged report in this
    # session's view as it does in the fleet's (review round 4, P2). Compared only: no foreign record is published.
    first_hand: dict[str, dict[str, Any]] = {x["task_id"]: {**x, "stub": True} for x in fleet_away + away}
    first_hand.update({r["task_id"]: r for r in rows})
    if stubs_by_id is not None:
        claimed = list(dict.fromkeys(
            e.get("task_id") for hb in beats.values() if isinstance(hb, dict) and isinstance(hb.get("tasks"), dict)
            for e in hb["tasks"].get("items") or [] if isinstance(e, dict) and isinstance(e.get("task_id"), str)))
        missing = [t for t in claimed if t not in first_hand]
        if missing:
            lookup_errs: list = []
            found = guard("task stubs", lambda: stubs_by_id(missing), lookup_errs)
            tree_errs += lookup_errs
            if found is None:                       # could not check: no heartbeat claim on these ids this time
                first_hand.update({t: {"task_id": t, "unverified": True} for t in missing})
            for x in found or []:
                if isinstance(x, dict) and x.get("task_id") in missing:
                    first_hand[x["task_id"]] = ({"task_id": x["task_id"], "unverified": True} if x.get("unreadable")
                                                else {"task_id": x["task_id"], "host": x.get("host"), "stub": True,
                                                      "origin_session": x.get("origin_session"),
                                                      "origin_machine": x.get("origin_machine")})
    # Run-wide origin conflicts over every record the provider holds (workers, open and ended conversations, run
    # origins), before any join: a source given but unreadable vetoes every heartbeat join (review round 5, P2).
    conflicted = conflicted_runs(
        [w for _, w in _all_workers(ov)] if isinstance(ov, dict) else None,
        [x for x in (backend, ended) if isinstance(x, list)], r_orig,
        unknown=not isinstance(ov, dict) or (conversations is not None and not isinstance(backend, list))
        or (ended_conversations is not None and not isinstance(ended, list))
        or (run_origins is not None and not isinstance(r_orig, dict)))
    hb_rows, task_hosts, hb_cut, hb_conflicts = heartbeat_rows(beats, now, first_hand, ov, conflicted)
    # A host that had more tasks than its heartbeat carries: said in the fleet view. Not in a session's view: the cut
    # is over every session's tasks on that host, and a session's flags are about its own data only (§5).
    if hb_cut and session is None:
        cut.append("host tasks")
    if session is not None:
        away = [x for x in away if x.get("origin_session") == session or (
            untagged_machine is not None and not x.get("origin_session")
            and x.get("origin_machine") == untagged_machine)]
        conv_items = [it for it in conv_items if it.get("session") == session]
    for name, lst in (("task stubs", away), ("conversations", conv_items)):
        if len(lst) > TREE_INPUT_MAX:
            del lst[TREE_INPUT_MAX:]                # both are newest / working first
            cut.append(name)
    # A task-dir error belongs to its task (the tree drops it with a task it does not show); the sources' errors
    # (the log, the board, the stubs, the conversations) apply to every view.
    tid_of = {id(e): tid for tid, e in task_errs}
    task_side = [(tid_of[id(e)], e) if id(e) in tid_of else e for e in tasks["errors"]
                 # the fleet scan's error is not the tree's when the session's own index was read instead
                 if not (own_index and isinstance(e, dict) and e.get("source") == "task stubs")] \
        if tasks["status"] != "ok" else []
    orch = orchestrators_section(rows + hb_rows, away, {"items": conv_items},
                                 backend if isinstance(backend, list) else None,
                                 ended if isinstance(ended, list) else None, tok, now,
                                 task_side + (conv["errors"] if conv["status"] != "ok" else []) + tree_errs + hb_errs,
                                 session, inputs_truncated=cut, task_hosts=task_hosts,
                                 conflicts=hb_conflicts if session is None else None,
                                 run_origins=r_orig if isinstance(r_orig, dict) else None, conflicted=conflicted,
                                 untagged_machine=untagged_machine if session is not None else None)
    tv_errs: list = []
    tv = guard("router.db", tasks_view, tv_errs)
    # The work split's child runs whose requests carried no origin are attributed through the provider host's OWN task
    # log (every row of the window, before any session filter; the parent chain as the tree walks it), never a
    # heartbeat's report (#127). The published token totals carry no run ids.
    by_id = {r["task_id"]: r for r in rows if isinstance(r, dict) and r.get("task_id")}
    run_orig = {tid: o for tid, o in ((tid, _origin_of(by_id, r)[0]) for tid, r in by_id.items()) if o}
    # Every session tag stays in `split_totals` for the work split's join (the pane's session survives every display
    # cap, review round 3); the routed section publishes the BY_SESSION_MAX-capped copy.
    split_totals = None
    if isinstance(tv, dict) and isinstance(tv.get("token_totals"), dict):
        split_totals = work_split.resolve_pending(tv["token_totals"], run_orig, cap=False)
        tv = {**tv, "token_totals": work_split.cap_sessions(split_totals)}
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
    ex_errs: list = []
    ex = guard("exec-hosts", exec_hosts, ex_errs) if exec_hosts is not None else None
    ex_hosts, ex_default = ex if isinstance(ex, tuple) else ({}, None)
    hosts = hosts_section(u, beats, names=host_names if host_names is not None else known_hosts(),
                          exec_hosts=ex_hosts, exec_default=ex_default, self_host=self_host, now=now,
                          errors=hb_errs + ex_errs + (u_errs if u is None else []),
                          self_name=self_name if self_name is not None else _slug(os.environ.get(SELF_ENV)))
    if split_totals is None and isinstance(tv, dict):
        split_totals = tv.get("token_totals")
    ws = work_split.section(split_totals, beats, now, tv_errs if tv is None else None, session=session)
    ac_errs: list = []
    ac = api_credit_section(guard("api credit", api_credit, ac_errs), now, ac_errs) if api_credit is not None else None
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
        "orchestrators": sum(1 for g in orch["items"] if g["kind"] != "unattributed"),
        "routed_token_pct_24h": ((ws.get("overall") or {}).get("24h") or {}).get("routed_pct"),
    }
    if ac is not None:                              # #114: only where the section is published (not the session view)
        overview["api_credit_alert"] = ac.get("alert_level")
    snap = {"version": VERSION, "generated_at": round(now, 3), "overview": overview, "tasks": tasks,
            "conversations": conv, "orchestrators": orch, "routed": routed, "subscriptions": subs, "s1": s1,
            "hosts": hosts, "work_split": ws}
    if ac is not None:
        snap["api_credit"] = ac
    return for_session(snap, session) if session is not None else snap


class Memo:
    """A MEMO_S memo with single flight for the encoded snapshot (as usage.collect has for its view): concurrent GETs
    wait for the one collect in progress and share its bytes. `key` names what must invalidate it early (the heartbeat
    board's generation, so a post shows on the next GET). A collect that raises is not memoised. Up to MEMO_KEYS keys
    are held at once: the fleet view and each pane's ?session= view are polled side by side, and one slot would make
    them evict each other on every poll."""

    def __init__(self, ttl_s: float = MEMO_S, clock: Callable[[], float] = time.monotonic):
        self._ttl, self._clock = ttl_s, clock
        self._lock = threading.Lock()               # guards _hits; never held across a collect
        self._flight = threading.Lock()             # held across a miss
        self._hits: OrderedDict[Any, tuple[float, bytes]] = OrderedDict()

    def _fresh(self, key: Any) -> bytes | None:
        with self._lock:
            h = self._hits.get(key)
            return h[1] if h and self._clock() - h[0] < self._ttl else None

    def get(self, key: Any, build: Callable[[], bytes]) -> bytes:
        body = self._fresh(key)
        if body is not None:
            return body
        with self._flight:
            body = self._fresh(key)
            if body is None:
                body = build()
                with self._lock:
                    at = self._clock()
                    self._hits.pop(key, None)
                    self._hits[key] = (at, body)
                    for k in [k for k, (t, _) in self._hits.items() if at - t >= self._ttl]:
                        del self._hits[k]                   # expired: never served again (keys only move forward)
                    while len(self._hits) > MEMO_KEYS:
                        self._hits.popitem(last=False)
        return body
