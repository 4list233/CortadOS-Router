"""GET /v1/cortados/live and POST /v1/cortados/live/heartbeat (provider/live.py): the snapshot's sections from fakes,
the heartbeat's validation and limits, auth and scopes over the real endpoint with the fake Claude CLI, and that no
prompt text, path, token or address ever reaches the snapshot. State is a throwaway CORTADOS_HOME / tasks dir; no
vendor CLI, ssh or S1 probe runs (the usage slice is collected from fixtures or replaced)."""
import io
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router import child_tasks
from cortados_router.provider import clients, live, office, usage

NOW = 1_800_000_000.0
SECRET = "SECRET-PROMPT-TEXT-0042"
RUNNING = "t20261111090000-aaaaaaaa"
QUEUED = "t20261111085000-bbbbbbbb"
DONE = "t20261111080000-cccccccc"
FAILED = "t20261111070000-dddddddd"
CANCELLED = "t20261111060000-eeeeeeee"
LOST = "t20261110060000-ffffffff"          # started 11 h ago, no end event: unknown, never "running"
OLD = "t20261109060000-99999999"           # outside the 24 h window
SESSION = "3f9a1c2e9b7d4a10"
ORIGIN = {"origin_session": SESSION, "origin_project": "CortadOS-Router", "origin_device": "pro"}   # the main session


def events():
    """child-tasks.jsonl records as child_tasks.log_event writes them (cwd and an injected prompt field included: the
    view must drop both)."""
    base = {"cwd": "/Users/someone/secret-project", "orchestrator": "claude-app", "permission_mode": "acceptEdits",
            "chrome": False, "prompt": SECRET}
    return [
        {"ts": NOW - 30 * 3600, "event": "started", "task_id": OLD, **base, "title": "too old to show"},
        {"ts": NOW - 125, "event": "started", "task_id": RUNNING, **base, "host": "air", "lane": "claude#b",
         "model": "claude-sonnet-5", "title": "Refactor the scarcity reader", "session": SESSION,
         "project": "CortadOS-Router", "device": "pro", **ORIGIN},
        {"ts": NOW - 600, "event": "started", "task_id": QUEUED, **base, "chrome": True, "host": "air",
         "title": "Check the release page", "session": SESSION},
        {"ts": NOW - 4000, "event": "started", "task_id": DONE, **base, "host": "air", "title": "Write the changelog",
         **ORIGIN},
        {"ts": NOW - 3000, "event": "finished", "task_id": DONE, **base, "host": "air", "title": "Write the changelog",
         "status": "succeeded", "exit_code": 0, "duration_s": 1000.0, "workspace_state": "archived"},
        {"ts": NOW - 7200, "event": "started", "task_id": FAILED, **base, "title": "Bisect flaky test 10.0.0.7",
         "origin_session": "/Users/someone", "origin_project": "<b>x</b>"},          # not an origin: unattributed
        {"ts": NOW - 6900, "event": "finished", "task_id": FAILED, **base, "status": "failed", "exit_code": 1,
         "duration_s": 300.0, "workspace_state": "held"},
        {"ts": NOW - 5000, "event": "cancelled", "task_id": CANCELLED, **base, "client": "grokbot",
         "origin_session": "grokbot"},
        {"ts": NOW - 11 * 3600, "event": "started", "task_id": LOST, **base, "host": "air"},
        {"ts": NOW - 10, "event": "task_exported", "task_id": DONE, "path_kind": "default"},
        {"ts": NOW - 5, "event": "started", "task_id": "../../etc/passwd", "title": SECRET},
        {"ts": NOW + 3600, "event": "started", "task_id": "t20261111100000-12345678"},   # future: not real activity
        "not a record",
    ]


def office_board():
    """An Office board with routed traffic: a main loop on a tagged floor, a routed subagent, the running child's own
    turn (model, lane, tool), a lobby conversation, and the orchestrator's heartbeat."""
    b = office.OfficeBoard()
    tags = {"session": SESSION, "project": "CortadOS-Router", "device": "pro"}
    b.turn_start("main:" + SESSION, tags, run_id=None, provider="claude", lane="claude#a", model="claude-opus-5",
                 now=NOW - 900)
    b.turn_start("r:sub-run-0001", {**tags, "title": "Review the diff"}, run_id="sub-run-0001", provider="codex",
                 lane="codex", model="gpt-6.1", now=NOW - 90)
    b.turn_end("r:sub-run-0001", tags, status="working", tool="Read", now=NOW - 80)
    b.turn_start("r:" + RUNNING, tags, run_id=RUNNING, provider="claude", lane="claude#b", model="claude-sonnet-5",
                 now=NOW - 100)
    b.turn_end("r:" + RUNNING, tags, status="working", tool="Bash", now=NOW - 20)
    b.turn_start("c:lobby-conv", {}, run_id=None, provider="local", lane="local-studio", model="qwen3-30b", now=NOW - 30)
    b.turn_end("c:lobby-conv", {}, status="idle", now=NOW - 25)
    b.heartbeat({"session": SESSION, "status": "working", "project": "CortadOS-Router", "device": "pro",
                 "model": "claude-opus-5", "tool": "Edit"}, now=NOW - 15)
    return b


def tasks_view(n=30):
    rows = [{"ts": NOW - 60 * i, "task_id": f"turn-{i}", "session_id": "session-aaaa", "routing_kind": "routed",
             "band": "R2", "lane": "claude#b", "model": "claude-sonnet-5", "actual_model": "claude-sonnet-5",
             "elapsed_ms": 1200 + i, "degraded": False, "s1_reason": "S1 decision; band R2; steps=1",
             "request_chars": 900, "response_chars": 300, "tokens": None, "tokens_status": "not recorded"}
            for i in range(n)]
    return {"tasks": rows, "totals": {"decisions": n, "routed": n, "pinned": 0, "unselected": 0, "routed_pct": 100.0},
            "row_limit": 200, "truncated": False, "errors": [], "token_totals": token_totals()}


def token_totals():
    """work_split.routed_tokens over a real `routes` table (core.DDL plus the columns core._db adds): two routed
    conversations of the main session SESSION, one routed subagent with no session tag, one row not yet recorded."""
    import sqlite3
    from cortados_router import core
    from cortados_router.provider import work_split
    c = sqlite3.connect(":memory:")
    c.executescript(core.DDL)
    for col, kind in (("scarcity", "TEXT"), ("context", "TEXT"), ("task_context", "TEXT"), *core.TOKEN_COLUMNS):
        c.execute(f"ALTER TABLE routes ADD COLUMN {col} {kind}")
    for tid, ts, lane, model, tag, tok in (
            ("r-1", NOW - 600, "claude#b", "claude-sonnet-5", SESSION, (1200, 800, 50000, 2000, 3)),
            ("r-2", NOW - 3 * 86400, "codex", "gpt-6.1", SESSION, (9000, 1000, 0, 0, 2)),
            ("r-3", NOW - 900, "codex", "gpt-6.1", None, (300, 200, 0, 0, 1)),
            ("r-4", NOW - 60, "claude#b", "claude-sonnet-5", None, (None, None, None, None, None))):
        c.execute("INSERT INTO routes (turn_id, ts, lane, model, office_session, tok_input, tok_output, tok_cache_read, "
                  "tok_cache_write, tok_turns) VALUES (?,?,?,?,?,?,?,?,?,?)", (tid, ts, lane, model, tag, *tok))
    try:
        return work_split.routed_tokens(c, NOW)
    finally:
        c.close()


# The Air runs the owner's main sessions: its heartbeat carries their own tokens (orchestrator_usage.scan's summary).
ORCH_BLOCK = {"version": 1, "sampled_at": NOW - 90, "salt": True, "windows": {"24h": 86400, "7d": 604800},
              "totals": {"24h": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000, "sessions": 1},
                         "7d": {"input": 9000, "output": 16000, "cache_read": 2500000, "cache_write": 75000, "sessions": 2}},
              "sessions": [{"session": SESSION, "project": "CortadOS-Router", "model": "claude-opus-5", "last_at": NOW - 120,
                            "24h": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000},
                            "7d": {"input": 5000, "output": 8000, "cache_read": 1500000, "cache_write": 45000}},
                           {"session": None, "project": "Grounds", "model": "claude-opus-5", "last_at": NOW - 4 * 86400,
                            "24h": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                            "7d": {"input": 4000, "output": 8000, "cache_read": 1000000, "cache_write": 30000}}],
              "sessions_hidden": 0, "excluded": {"child_task_files": 4, "sidechain_entries": 12, "routed_model_entries": 0,
                                                 "unreadable_files": 0, "bad_lines": 0}}


def usage_live():
    from tests import test_usage as tu
    home = tu.fixture_home()
    hw = home / "hardware" / "studio.json"
    # HW_SAMPLE's load average is the test machine's own (os.getloadavg): pinned, so the fixture is the same everywhere
    hw.write_text(json.dumps({**json.loads(hw.read_text()), "load_avg": [1.5, 1.25, 1.0]}))
    os.utime(hw, (NOW - 30, NOW - 30))                                   # sampled 30 s before the fixture's clock
    return usage.collect_live(state_dir=home, scarcity_reader=lambda: tu.SCARCITY, now=NOW,
                              s1_probe=lambda url: {"available": True, "status": 200, "latency_ms": 23})


S1_BLOCK = {"health": "up", "model": "CortadOS Router S1 2026-11-09 (run-20261109)", "flags": ["no-promote"],
            "pending": "promotion_held", "loaded_s": 41.2}


def heartbeats(at=NOW - 40):
    b = live.HeartbeatBoard()
    hb, bad = live.validate_heartbeat({"host": "studio", "ttl_s": 900, "jobs": [
        {"name": "s1-scorer", "state": "running", "summary": "serving v12"},
        {"name": "s1-continuous", "state": "running", "summary": "sweep 3/8", "progress_pct": 37.5,
         "started_at": NOW - 3600}], "s1": S1_BLOCK,
        "hardware": {"disk_free_pct": 61.5, "sampled_at": NOW - 100, "cpu_busy_pct": 80.0}}, NOW)
    assert bad is None, bad
    b.post(hb, at)
    # the Air (the provider host) has no hardware/<host>.json: its sender's heartbeat carries the sample
    hb, bad = live.validate_heartbeat({"host": "air", "ttl_s": 180, "jobs": [
        {"name": "com.cortados.provider", "state": "running", "summary": "pid up, 3 runs"}],
        "hardware": {"cpu_busy_pct": 12.5, "gpu_busy_pct": 3.0, "mem_available_bytes": 8 * 2 ** 30,
                     "mem_total_bytes": 24 * 2 ** 30, "load_1m": 2.1, "disk_free_pct": 40.0, "chip": "Apple M4",
                     "sampled_at": NOW - 20}, "orchestrator": ORCH_BLOCK}, NOW)
    assert bad is None, bad
    b.post(hb, at + 30)
    return b


def backend_conversations():
    """Registry.live_conversations as the server reports them: the floor's routed main loop (waiting for the client's
    tool), the running child's own conversation (a turn in progress), the routed subagent's, and one opened by a client
    that sends no office tags (no board key)."""
    return [
        {"key": "main:" + SESSION, "ref": "c-000000000001", "provider": "claude", "lane": "claude#a",
         "model": "claude-opus-5", "opened_at": NOW - 900, "last_used": NOW - 15, "busy": False, "awaiting_tool": True},
        {"key": "r:" + RUNNING, "ref": "c-000000000002", "provider": "claude", "lane": "claude#b",
         "model": "claude-sonnet-5", "opened_at": NOW - 100, "last_used": NOW - 20, "busy": True,
         "awaiting_tool": False},
        {"key": "r:sub-run-0001", "ref": "c-000000000003", "provider": "codex", "lane": "codex", "model": "gpt-6.1",
         "opened_at": NOW - 90, "last_used": NOW - 80, "busy": False, "awaiting_tool": True},
        {"key": None, "ref": "c-000000000004", "provider": "codex", "lane": "codex", "model": "gpt-6.1-mini",
         "opened_at": NOW - 400, "last_used": NOW - 300, "busy": False, "awaiting_tool": False},
    ]


def _n(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def peek(tid):
    if tid == QUEUED:
        return {"phase": "queued", "ended": False, "workspace": None}
    if tid == RUNNING:
        return {"phase": None, "ended": False, "workspace": "active"}
    return None


def fixture_view(**over):
    """The snapshot the dashboard and the pulse fixtures are built from (tests/test_dashboard.py, test_pulse_fixture)."""
    board, beats, tv, u = office_board(), heartbeats(), tasks_view(), usage_live()
    args = dict(now=NOW, child_events=events, office_view=lambda ev: board.view(ev, now=NOW),
                tasks_view=lambda: tv, usage_view=lambda: u, heartbeats=lambda: beats.snapshot(NOW),
                backend_sessions=4, peek=peek, conversations=backend_conversations,
                exec_hosts=lambda: ({"air": ["forests-macbook-air"], "studio": ["forests-mac-studio"]}, "air"),
                self_host="forests-macbook-air", host_names=["air", "studio", "pro"])
    args.update(over)
    return live.collect(**args)


class CollectTest(unittest.TestCase):
    def test_tasks_running_queued_recent(self):
        t = fixture_view()["tasks"]
        self.assertEqual(t["status"], "ok")
        self.assertEqual(t["counts"], {"running": 1, "queued": 1, "succeeded": 1, "failed": 1, "cancelled": 1,
                                       "unknown": 1})
        r = t["running"][0]
        self.assertEqual((r["task_id"], r["state"], r["host"], r["model"], r["lane"], r["provider"], r["tool"]),
                         (RUNNING, "running", "air", "claude-sonnet-5", "claude#b", "claude", "Bash"))
        self.assertEqual((r["elapsed_s"], r["started_at"], r["workspace"]), (125.0, NOW - 125, "active"))
        self.assertEqual((r["title"], r["project"], r["device"], r["session"]),
                         ("Refactor the scarcity reader", "CortadOS-Router", "pro", SESSION))
        q = t["queued"][0]
        self.assertEqual((q["task_id"], q["state"], q["phase"], q["browser"]),
                         (QUEUED, "queued", "waiting for the host's Chrome", True))
        self.assertIsNone(q["session"])                       # a browser task's tags never make it a floor's
        recent = {x["task_id"]: x for x in t["recent"]}
        self.assertEqual([x["task_id"] for x in t["recent"]], [DONE, CANCELLED, FAILED, LOST])   # newest end first
        self.assertEqual((recent[DONE]["state"], recent[DONE]["elapsed_s"], recent[DONE]["workspace"]),
                         ("succeeded", 1000.0, "archived"))
        self.assertEqual((recent[FAILED]["state"], recent[FAILED]["workspace"]), ("failed", "held"))
        self.assertIsNone(recent[FAILED]["title"])            # an address in a title drops the title, not the task
        self.assertEqual((recent[CANCELLED]["state"], recent[CANCELLED]["client"]), ("cancelled", "grokbot"))
        self.assertEqual((recent[LOST]["state"], recent[LOST]["phase"]), ("unknown", "no end event after 10 h"))
        self.assertNotIn(OLD, json.dumps(t))

    def test_task_ended_on_its_host_without_an_end_event_is_unknown(self):
        t = fixture_view(peek=lambda tid: {"phase": None, "ended": True, "workspace": None})["tasks"]
        self.assertEqual(t["counts"]["running"], 0)
        self.assertIn("no end event logged", json.dumps(t))

    def test_peek_failure_is_said_and_the_tasks_stay(self):
        def boom(tid):
            raise child_tasks.TaskError("record unreadable at /Users/someone/x")
        t = fixture_view(peek=boom)["tasks"]
        self.assertEqual(t["status"], "partial")
        self.assertIn({"source": "task_dir", "error": "TaskError"}, t["errors"])
        self.assertNotIn("/Users/", json.dumps(t))
        self.assertEqual(t["counts"]["running"], 2)          # without the peek the queued task reads as running

    def test_conversations_list_every_open_conversation(self):
        # Bug (2026-11-11): a running child talked to the provider the whole time and conversations said active 0:
        # the section was built from the Office board alone, which files child tasks under tasks, and the Registry's
        # open conversations were only counted (backend_sessions).
        c = fixture_view()["conversations"]
        self.assertEqual((c["status"], c["backend_sessions"], c["active"], c["working"]), ("ok", 4, 5, 3))
        rows = [(x["kind"], x["run_id"], x["model"], x["lane"], x["status"], x["source"]) for x in c["items"]]
        self.assertEqual(rows, [   # working first, newest first
            ("subagent", "sub-run-0001", "gpt-6.1", "codex", "working", "provider"),
            ("child_task", RUNNING, "claude-sonnet-5", "claude#b", "working", "provider"),
            ("orchestrator", "c-000000000001", "claude-opus-5", "claude#a", "working", "provider"),
            ("conversation", None, "qwen3-30b", "local-studio", "idle", "routed"),    # untagged one-shot: board only
            ("conversation", "c-000000000004", "gpt-6.1-mini", "codex", "idle", "provider")])
        by = {x["run_id"]: x for x in c["items"]}
        child = by[RUNNING]
        self.assertEqual((child["title"], child["tool"], child["elapsed_s"], child["phase"], child["session"]),
                         ("Refactor the scarcity reader", "Bash", 100.0, "model turn", SESSION))
        sub = by["sub-run-0001"]
        self.assertEqual((sub["tool"], sub["project"], sub["phase"], sub["elapsed_s"]),
                         ("Read", "CortadOS-Router", "client running a tool", 90.0))
        main = by["c-000000000001"]
        self.assertEqual((main["kind"], main["session"], main["elapsed_s"], main["tool"]),
                         ("orchestrator", SESSION, 900.0, "Edit"))
        # the floor's orchestrator is that routed main loop: listed once, not again from the board
        self.assertEqual(sum(1 for x in c["items"] if x["kind"] == "orchestrator"), 1)
        untagged = by["c-000000000004"]
        self.assertEqual((untagged["status"], untagged["phase"], untagged["session"]),
                         ("idle", "waiting for the client", None))

    def test_backend_sessions_counts_the_listed_snapshot(self):
        # review nit: a separate len(registry.sessions) could disagree with the list taken a moment later
        c = fixture_view(backend_sessions=99)["conversations"]
        self.assertEqual((c["backend_sessions"], len([x for x in c["items"] if x["source"] == "provider"])), (4, 4))
        c = fixture_view(backend_sessions=99, conversations=lambda: 1 / 0)["conversations"]
        self.assertEqual(c["backend_sessions"], 99)        # the list failed: the registry's own count stands

    def test_tasks_carry_their_origin_and_are_grouped_by_it(self):
        t = fixture_view()["tasks"]
        r = t["running"][0]
        self.assertEqual((r["origin_session"], r["origin_project"], r["origin_device"]),
                         (SESSION, "CortadOS-Router", "pro"))
        recent = {x["task_id"]: x for x in t["recent"]}
        self.assertEqual(recent[CANCELLED]["origin_session"], "grokbot")
        self.assertEqual((recent[FAILED]["origin_session"], recent[FAILED]["origin_project"]), (None, None))
        self.assertIsNone(t["queued"][0]["origin_session"])                  # logged before origins: unattributed
        groups = {g["session"]: g for g in t["origins"]}
        self.assertEqual(set(groups), {SESSION, "grokbot", None})
        mine = groups[SESSION]
        self.assertEqual((mine["project"], mine["device"], mine["running"], mine["queued"], mine["finished"],
                          mine["task_ids"]), ("CortadOS-Router", "pro", 1, 0, 1, [RUNNING, DONE]))
        self.assertEqual((groups[None]["running"], groups[None]["queued"], groups[None]["finished"]), (0, 1, 2))
        self.assertEqual((groups["grokbot"]["finished"], groups["grokbot"]["task_ids"]), (1, [CANCELLED]))
        # a group with running or queued work first; the unattributed group after a named one
        self.assertEqual([g["session"] for g in t["origins"]][:2], [SESSION, None])
        self.assertFalse(t["origins_truncated"])

    def test_orchestrator_items_count_their_sessions_tasks(self):
        c = fixture_view()["conversations"]
        main = next(x for x in c["items"] if x["kind"] == "orchestrator")
        self.assertEqual(main["tasks"], {"running": 1, "queued": 0, "finished": 1, "task_ids": [RUNNING, DONE]})
        self.assertNotIn("tasks", next(x for x in c["items"] if x["kind"] == "subagent"))
        # a floor known only from its heartbeat (no routed main loop) gets the same counts
        c = fixture_view(conversations=lambda: [])["conversations"]
        main = next(x for x in c["items"] if x["kind"] == "orchestrator")
        self.assertEqual((main["source"], main["tasks"]["running"]), ("heartbeat", 1))
        # the tasks could not be read: the count is unknown, never zero
        c = fixture_view(child_events=lambda: 1 / 0)["conversations"]
        self.assertIsNone(next(x for x in c["items"] if x["kind"] == "orchestrator")["tasks"])

    def test_origin_groups_are_capped(self):
        rows = [{"task_id": f"t2026111109{i:04d}-aaaaaaaa", "state": "succeeded", "started_at": NOW - i,
                 "origin_session": f"{i:016x}"} for i in range(live.ORIGINS_MAX + 3)]
        o = live.origins_of(rows)
        self.assertEqual((len(o["origins"]), o["origins_truncated"]), (live.ORIGINS_MAX, True))
        self.assertEqual(o["origins"][0]["session"], f"{0:016x}")             # newest first

    def test_for_session_narrows_tasks_and_conversations(self):
        v = live.for_session(fixture_view(), SESSION)
        self.assertEqual(v["session_filter"], SESSION)
        self.assertEqual([x["task_id"] for x in v["tasks"]["running"] + v["tasks"]["queued"] + v["tasks"]["recent"]],
                         [RUNNING, DONE])
        self.assertEqual([g["session"] for g in v["tasks"]["origins"]], [SESSION])
        self.assertTrue(v["conversations"]["items"])
        self.assertTrue(all(x["session"] == SESSION for x in v["conversations"]["items"]))
        self.assertEqual(v["tasks"]["counts"], fixture_view()["tasks"]["counts"])   # counts stay fleet-wide
        self.assertEqual(live.valid_session("grokbot"), "grokbot")
        for bad in ("", "ABCDEF0123456789", "short", "../x", None, 7):
            self.assertIsNone(live.valid_session(bad))

    def test_session_view_filters_before_the_fleet_caps(self):
        # Review 2026-11-13: tasks_section capped running[:50] / recent[:40] and only then was ?session= applied, so on a
        # busy fleet a session's older tasks vanished from its view while origins[].task_ids still listed them.
        other = "b" * 16
        tid = lambda i: f"t20261111{i:06d}-aaaaaaaa"
        ev = []
        for i in range(live.RUNNING_MAX + 5):                          # newer running tasks of another session
            ev.append({"ts": NOW - 100 - i, "event": "started", "task_id": tid(i), "origin_session": other})
        for i in range(100, 100 + live.RECENT_MAX + 5):                # newer finished tasks of another session
            ev += [{"ts": NOW - 2000 - i, "event": "started", "task_id": tid(i), "origin_session": other},
                   {"ts": NOW - 1000 - i, "event": "finished", "task_id": tid(i), "status": "succeeded"}]
        mine_run, mine_done = tid(900), tid(901)                      # the session's own: the oldest of each list
        ev += [{"ts": NOW - 3000, "event": "started", "task_id": mine_run, **ORIGIN},
               {"ts": NOW - 9000, "event": "started", "task_id": mine_done, **ORIGIN},
               {"ts": NOW - 8000, "event": "finished", "task_id": mine_done, "status": "failed"}]
        fleet = fixture_view(child_events=lambda: ev, peek=None)["tasks"]
        self.assertTrue(fleet["truncated"])
        listed = [x["task_id"] for x in fleet["running"] + fleet["recent"]]
        self.assertNotIn(mine_run, listed)                             # capped out of the fleet lists
        self.assertNotIn(mine_done, listed)
        self.assertEqual(next(g for g in fleet["origins"] if g["session"] == SESSION)["task_ids"], [mine_run, mine_done])
        for v in (fixture_view(child_events=lambda: ev, peek=None, session=SESSION),
                  fixture_view(child_events=lambda: ev, peek=None, session=SESSION)):
            t = v["tasks"]
            self.assertEqual(v["session_filter"], SESSION)
            self.assertEqual([x["task_id"] for x in t["running"]], [mine_run])
            self.assertEqual([x["task_id"] for x in t["recent"]], [mine_done])
            self.assertFalse(t["truncated"])
            self.assertEqual(t["counts"], fleet["counts"])             # counts stay fleet-wide
            self.assertEqual([g["session"] for g in t["origins"]], [SESSION])
        # the same through the old path (filtering the capped snapshot) loses them: what the fix is for
        stale = live.for_session(fixture_view(child_events=lambda: ev, peek=None), SESSION)["tasks"]
        self.assertEqual(stale["running"] + stale["recent"], [])

    def test_a_failed_conversation_list_keeps_the_board(self):
        c = fixture_view(conversations=lambda: 1 / 0)["conversations"]
        self.assertEqual(c["status"], "partial")
        self.assertIn({"source": "conversations", "error": "ZeroDivisionError"}, c["errors"])
        self.assertEqual(c["active"], 3)                     # the Office board's view, as without the list
        c = fixture_view(office_view=lambda ev: 1 / 0)["conversations"]
        self.assertEqual((c["status"], c["active"]), ("partial", 4))   # the open conversations stand without the board

    def test_conversations_from_the_office_view(self):
        c = fixture_view(conversations=None, backend_sessions=2)["conversations"]
        self.assertEqual((c["status"], c["backend_sessions"], c["active"], c["working"]), ("ok", 2, 3, 2))
        kinds = [(x["kind"], x["model"], x["status"]) for x in c["items"]]
        self.assertEqual(kinds, [("subagent", "gpt-6.1", "working"), ("orchestrator", "claude-opus-5", "working"),
                                 ("conversation", "qwen3-30b", "idle")])
        sub = c["items"][0]
        self.assertEqual((sub["tool"], sub["project"], sub["device"], sub["elapsed_s"], sub["title"]),
                         ("Read", "CortadOS-Router", "pro", 90.0, "Review the diff"))
        # child tasks live in the tasks section: no item of their own, only the ids its orchestrator started
        self.assertNotIn(RUNNING, json.dumps([{k: v for k, v in x.items() if k != "tasks"} for x in c["items"]]))

    def test_routed_calls_capped(self):
        r = fixture_view()["routed"]
        self.assertEqual(len(r["calls"]), live.ROUTED_MAX)
        self.assertTrue(r["more"])
        self.assertEqual(set(r["calls"][0]), set(live._CALL_KEYS))
        self.assertEqual(r["totals"]["routed_pct"], 100.0)

    def test_subscriptions_and_lowest(self):
        s = fixture_view()["subscriptions"]
        lanes = {l["lane"]: l for l in s["lanes"]}
        self.assertEqual(lanes["claude"]["left_pct"], 80.0)
        self.assertTrue(lanes["codex"]["stale"])
        # cursor (70 %) is the lowest fresh, error-free lane; its headline window is its Cursor-models pool
        self.assertEqual(s["lowest"]["lane"], "cursor")
        self.assertEqual(s["lowest"]["left_pct"], 70.0)
        self.assertTrue(s["lowest"]["window"].startswith("month (Cursor models"))
        self.assertEqual(fixture_view()["overview"]["lowest_subscription"], s["lowest"])

    def test_lowest_skips_lanes_routing_cannot_use(self):
        # Review P3: the anthropic-api lane with no key reads 0 % left (of a $0 budget) and was shown as the floor.
        def lane(name, left):
            return {"lane": name, "label": name, "left_pct": left, "stale": False, "error": None,
                    "windows": [{"window": "1mo", "pool": "all", "left_pct": left}]}
        u = {"lanes": [lane("anthropic-api", 0.0), lane("antigravity", 5.0), lane("claude", 60.0), lane("codex", 40.0)]}
        self.assertEqual(live.subscriptions_section(u, NOW)["lowest"]["lane"], "anthropic-api")   # no predicate: as before
        with mock.patch("cortados_router.api_credits.configured_orgs", return_value=[]):
            s = live.subscriptions_section(u, NOW, live.lane_enabled)
        self.assertEqual(s["lowest"]["lane"], "codex")        # antigravity: context ceiling 0, refused by the fit gate
        self.assertEqual(len(s["lanes"]), 4)                  # still listed, only never the floor
        with mock.patch("cortados_router.api_credits.configured_orgs", return_value=[("org", "key")]):
            self.assertEqual(live.subscriptions_section(u, NOW, live.lane_enabled)["lowest"]["lane"], "anthropic-api")
        s = live.subscriptions_section(u, NOW, lambda l: (_ for _ in ()).throw(KeyError()) if l == "codex" else True)
        self.assertEqual(s["lowest"]["lane"], "anthropic-api")
        s = live.subscriptions_section(u, NOW, lambda l: (_ for _ in ()).throw(KeyError()) if l != "claude" else True)
        self.assertEqual((s["lowest"]["lane"], s["status"]), ("claude", "partial"))
        self.assertIn({"source": "lanes", "error": "KeyError"}, s["errors"])
        v = fixture_view(lane_enabled=lambda l: l != "cursor")
        self.assertEqual(v["overview"]["lowest_subscription"]["lane"], v["subscriptions"]["lowest"]["lane"])
        self.assertNotEqual(v["subscriptions"]["lowest"]["lane"], "cursor")

    def test_hardware_matching_no_known_host_is_dropped_and_counted(self):
        u = usage_live()
        u["hardware"] = list(u.get("hardware") or []) + [
            {"file": "forests-imac.json", "age_s": 5.0, "data": {"host": "forests-imac", "cpu_busy_pct": 3.0}},
            {"file": "x.json", "age_s": 5.0, "data": {"host": None}}]
        v = fixture_view(usage_view=lambda: u)
        h = v["hosts"]
        self.assertEqual([x["name"] for x in h["items"]], ["air", "studio", "pro"])
        self.assertEqual(h["status"], "partial")
        self.assertIn({"source": "hardware", "error": "2 hardware sample(s) match no known host name"}, h["errors"])
        self.assertNotIn("imac", json.dumps(h))
        self.assertEqual({x["name"]: x for x in h["items"]}["studio"]["hardware"]["age_s"], 30.0)

    def test_s1_and_hosts(self):
        v = fixture_view()
        self.assertEqual((v["s1"]["status"], v["s1"]["latency_ms"], v["s1"]["http_status"]), ("up", 23, 200))
        h = {x["name"]: x for x in v["hosts"]["items"]}
        self.assertEqual(list(h), ["air", "studio", "pro"])
        self.assertEqual(v["hosts"]["provider_host"], "air")
        self.assertEqual(h["air"]["roles"], ["provider", "default exec host"])
        self.assertEqual((h["studio"]["heartbeat_state"], h["studio"]["heartbeat"]["age_s"]), ("fresh", 40.0))
        self.assertEqual([j["name"] for j in h["studio"]["heartbeat"]["jobs"]], ["s1-scorer", "s1-continuous"])
        # the studio's sample card (30 s old) is fresher than its heartbeat's hardware (100 s): the card stands, and the
        # heartbeat adds the disk figure the card does not have
        self.assertEqual(h["studio"]["hardware"]["age_s"], 30.0)   # tests/test_usage.HW_SAMPLE, host "studio"
        self.assertEqual((h["studio"]["hardware"]["source"], h["studio"]["hardware"]["disk_free_pct"],
                          h["studio"]["hardware"]["filled_by"]), ("sample", 61.5, "heartbeat"))
        self.assertEqual(h["studio"]["heartbeat"]["s1"], {**S1_BLOCK})
        # Bug (2026-11-11): the Air, the provider host itself, had no hardware: nothing writes hardware/<host>.json on
        # it. Its heartbeat sender's sample fills the card.
        self.assertEqual(h["air"]["hardware"], {"cpu_busy_pct": 12.5, "gpu_busy_pct": 3.0,
                                                "mem_available_bytes": 8 * 2 ** 30, "mem_total_bytes": 24 * 2 ** 30,
                                                "chip": "Apple M4", "load_1m": 2.1, "age_s": 20.0,
                                                "disk_free_pct": 40.0, "source": "heartbeat", "filled_by": None})
        self.assertEqual((h["pro"]["heartbeat_state"], h["pro"]["heartbeat"], h["pro"]["hardware"]), ("none", None, None))
        self.assertEqual(v["overview"]["hosts_with_heartbeat"], 2)

    def test_a_fresher_sparse_heartbeat_fills_in_without_hiding_the_card(self):
        # Review (2026-11-11): a fresher heartbeat with only disk_free_pct replaced the studio's richer sample card
        # wholesale. Now each field comes from the freshest reading that has it.
        b = heartbeats()
        hb, bad = live.validate_heartbeat({"host": "studio", "ttl_s": 180, "jobs": [],
                                           "hardware": {"disk_free_pct": 12.0, "cpu_busy_pct": 5.0,
                                                        "sampled_at": NOW - 2}}, NOW)
        self.assertIsNone(bad)
        b.post(hb, NOW - 1)
        hw = {x["name"]: x for x in fixture_view(heartbeats=lambda: b.snapshot(NOW))["hosts"]["items"]}["studio"]["hardware"]
        empty = live.HeartbeatBoard()
        card = {x["name"]: x for x in fixture_view(heartbeats=lambda: empty.snapshot(NOW))["hosts"]["items"]}[
            "studio"]["hardware"]                                                     # the sample card alone
        self.assertEqual((card["source"], card["filled_by"], card["disk_free_pct"]), ("sample", None, None))
        self.assertEqual((hw["source"], hw["age_s"], hw["filled_by"]), ("heartbeat", 2.0, "sample"))
        self.assertEqual((hw["cpu_busy_pct"], hw["disk_free_pct"]), (5.0, 12.0))     # the fresher values win
        kept = [k for k in ("chip", "gpu_busy_pct", "mem_available_bytes", "mem_total_bytes", "load_1m")
                if card.get(k) is not None]
        self.assertTrue(kept)
        for k in kept:
            self.assertEqual(hw[k], card[k], k)                                      # the card's, not hidden

    def test_a_stale_heartbeat_hardware_is_not_shown(self):
        b = heartbeats(at=NOW - 1000)
        h = {x["name"]: x for x in fixture_view(heartbeats=lambda: b.snapshot(NOW))["hosts"]["items"]}
        self.assertIsNone(h["air"]["hardware"])                   # its 180 s freshness is long past
        self.assertEqual(h["studio"]["hardware"]["source"], "sample")

    def test_the_provider_hosts_own_sample_maps_to_its_name(self):
        # a hostname that matches no known name by alias or word: the provider's own card is still shown, under the
        # name CORTADOS_LIVE_SELF gives it; a different unknown hostname stays dropped and counted
        u = usage_live()
        u["hardware"] = [{"file": "x.json", "age_s": 4.0, "data": {"host": "owners-laptop", "cpu_busy_pct": 7.0}},
                         {"file": "y.json", "age_s": 4.0, "data": {"host": "someone-else", "cpu_busy_pct": 9.0}}]
        b = live.HeartbeatBoard()
        v = fixture_view(usage_view=lambda: u, self_host="owners-laptop", exec_hosts=lambda: ({}, None),
                         heartbeats=lambda: b.snapshot(NOW), self_name="air")
        h = {x["name"]: x for x in v["hosts"]["items"]}
        self.assertEqual((v["hosts"]["provider_host"], h["air"]["hardware"]["cpu_busy_pct"]), ("air", 7.0))
        self.assertIn({"source": "hardware", "error": "1 hardware sample(s) match no known host name"},
                      v["hosts"]["errors"])
        self.assertNotIn("owners", json.dumps(v["hosts"]))
        with mock.patch.dict(os.environ, {"CORTADOS_LIVE_SELF": "Air"}):
            v = fixture_view(usage_view=lambda: u, self_host="owners-laptop", exec_hosts=lambda: ({}, None),
                             heartbeats=lambda: b.snapshot(NOW))
        self.assertEqual(v["hosts"]["provider_host"], "air")
        v = fixture_view(usage_view=lambda: u, self_host="owners-laptop", exec_hosts=lambda: ({}, None),
                         heartbeats=lambda: b.snapshot(NOW), self_name="nosuchhost")
        self.assertIsNone(v["hosts"]["provider_host"])           # only a known name, never a made-up one

    def test_a_stale_heartbeat_is_said_so(self):
        b = heartbeats(at=NOW - 1000)
        h = {x["name"]: x for x in fixture_view(heartbeats=lambda: b.snapshot(NOW))["hosts"]["items"]}
        self.assertEqual(h["studio"]["heartbeat_state"], "stale")
        self.assertTrue(h["studio"]["heartbeat"]["stale"])

    def test_host_matching(self):
        aliases = {"air": {"forests-macbook-air"}, "studio": set(), "pro": set()}
        self.assertEqual(live._match("forests-macbook-air", aliases), "air")
        self.assertEqual(live._match("Forests-Mac-Studio", aliases), "studio")
        self.assertIsNone(live._match("air-studio", aliases))    # two names: never a guess
        self.assertIsNone(live._match(None, aliases))
        self.assertEqual(live.known_hosts({"CORTADOS_LIVE_HOSTS": "Air, studio,,bad name!"}), ["air", "studio", "bad-name"])
        self.assertEqual(live.known_hosts({}), ["air", "studio", "pro"])

    def test_every_section_has_as_of_status_errors(self):
        v = fixture_view()
        for k in ("tasks", "conversations", "routed", "subscriptions", "s1", "hosts"):
            self.assertEqual(set(v[k]) >= {"as_of", "status", "errors"}, True, k)
        self.assertEqual(v["version"], live.VERSION)
        json.dumps(v, allow_nan=False)

    def test_a_failed_source_marks_only_its_section(self):
        def boom():
            raise OSError("/Users/someone/.cortados/router.db: permission denied")
        v = fixture_view(tasks_view=boom)
        self.assertEqual(v["routed"]["status"], "error")
        self.assertEqual(v["routed"]["errors"], [{"source": "router.db", "error": "OSError"}])
        self.assertEqual(v["tasks"]["status"], "ok")
        self.assertNotIn("/Users/", json.dumps(v))
        v = fixture_view(child_events=boom)
        self.assertEqual((v["tasks"]["status"], v["overview"]["running_tasks"]), ("error", None))
        self.assertEqual(v["conversations"]["status"], "ok")     # the office view stands without the log
        v = fixture_view(usage_view=boom)
        self.assertEqual((v["subscriptions"]["status"], v["s1"]["status"]), ("error", "unknown"))
        self.assertEqual(v["hosts"]["status"], "partial")
        self.assertIn({"source": "usage", "error": "OSError"}, v["hosts"]["errors"])
        v = fixture_view(office_view=lambda ev: (_ for _ in ()).throw(RuntimeError("x")), conversations=None)
        self.assertEqual((v["conversations"]["status"], v["conversations"]["active"]), ("error", None))
        self.assertEqual(v["tasks"]["status"], "partial")        # the tasks still come from the log
        v = fixture_view(exec_hosts=lambda: (_ for _ in ()).throw(ValueError("bad file")))
        self.assertIn({"source": "exec-hosts", "error": "ValueError"}, v["hosts"]["errors"])

    def test_s1_down_is_down(self):
        from tests import test_usage as tu
        u = usage.collect_live(state_dir=tu.fixture_home(), scarcity_reader=lambda: tu.SCARCITY, now=NOW,
                               s1_probe=lambda url: {"available": False, "reason": "URLError", "latency_ms": 1500})
        v = fixture_view(usage_view=lambda: u)
        self.assertEqual((v["s1"]["status"], v["s1"]["reason"], v["overview"]["s1"]), ("down", "URLError", "down"))
        self.assertEqual(v["s1"]["errors"], [{"source": "s1", "error": "URLError"}])

    def test_the_snapshot_holds_no_prompt_path_token_or_address(self):
        raw = json.dumps(fixture_view())
        for bad in (SECRET, "/Users/", "secret-project", "acceptEdits", "100.64.0.9", "10.0.0.7", "x@example.com",
                    "etc/passwd", "ssh_target", "config_dir", "cwd"):
            self.assertNotIn(bad, raw, bad)


class ScopeTest(unittest.TestCase):
    """#73 follow-ups: the tasks section says whose tasks it holds, lists remote stubs, publishes only known host names."""

    def test_tasks_scope_is_the_provider_host(self):
        t = fixture_view()["tasks"]
        self.assertEqual((t["scope"], t["elsewhere"]), ("provider host", []))
        t = fixture_view(child_events=lambda: (_ for _ in ()).throw(OSError("x")))["tasks"]
        self.assertEqual((t["status"], t["scope"]), ("error", "provider host"))

    def test_remote_stubs_are_listed_elsewhere(self):
        stubs = [{"task_id": "t20261111091000-11111111", "host": "air", "created_at": NOW - 60},
                 {"task_id": "t20261111092000-22222222", "host": "studio", "created_at": NOW - 30},
                 {"task_id": RUNNING, "host": "air", "created_at": NOW - 125},           # in the local log: not twice
                 {"task_id": "t20261110092000-33333333", "host": "air", "created_at": NOW - 11 * 3600},  # past 10 h
                 {"task_id": "t20261111093000-44444444", "host": "Forests-MBA", "created_at": NOW - 5},   # not a slug
                 {"task_id": "../x", "host": "air", "created_at": NOW - 5},
                 {"task_id": "t20261111094000-55555555", "host": "air", "created_at": "soon"}, "junk"]
        t = fixture_view(remote_stubs=lambda: stubs)["tasks"]
        self.assertEqual(t["elsewhere"], [
            {"task_id": "t20261111092000-22222222", "host": "studio", "started_at": NOW - 30},
            {"task_id": "t20261111091000-11111111", "host": "air", "started_at": NOW - 60}])
        self.assertEqual(t["counts"]["running"], 1)                  # never counted as running here
        self.assertEqual(t["status"], "ok")
        # stubs alone (nothing in the local log) are data: the section is not "error"
        t = live.tasks_section([], None, None, NOW, stubs=stubs)
        self.assertEqual((t["status"], len(t["elsewhere"])), ("ok", 3))

    def test_a_failed_stub_read_is_partial(self):
        def boom():
            raise PermissionError("/Users/someone/.cortados/tasks")
        t = fixture_view(remote_stubs=boom)["tasks"]
        self.assertEqual(t["status"], "partial")
        self.assertIn({"source": "task stubs", "error": "PermissionError"}, t["errors"])
        self.assertNotIn("/Users/", json.dumps(t))

    def test_provider_host_is_published_only_as_a_known_name(self):
        v = fixture_view(self_host="johns-imac-2")
        self.assertIsNone(v["hosts"]["provider_host"])
        self.assertNotIn("johns", json.dumps(v))
        self.assertEqual([h["name"] for h in v["hosts"]["items"]], ["air", "studio", "pro"])
        # a word of the hostname that IS a known name is that name (forests-mac-studio -> studio), never the slug
        v = fixture_view(self_host="forests-mac-studio", exec_hosts=lambda: ({}, None))
        self.assertEqual(v["hosts"]["provider_host"], "studio")
        self.assertNotIn("forests", json.dumps(v["hosts"]))
        v = fixture_view(self_host="forests-mac-studio", host_names=["lab"], exec_hosts=lambda: ({}, None),
                         heartbeats=lambda: {}, usage_view=lambda: {})
        self.assertIsNone(v["hosts"]["provider_host"])


class StubScanTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tempfile.mkdtemp(), "tasks")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.root), True)
        p = mock.patch.dict(os.environ, {"CORTADOS_TASKS_DIR": self.root})
        p.start()
        self.addCleanup(p.stop)

    def test_remote_stubs_read_only(self):
        self.assertEqual(child_tasks.remote_stubs(), [])
        self.assertFalse(os.path.exists(self.root))                  # never creates the tasks root
        os.makedirs(self.root)
        for tid, stub in (("t20261111090000-aaaaaaaa", {"host": "air", "created_at": NOW}),
                          ("t20261111091000-bbbbbbbb", None),                      # a local task
                          ("t20261111092000-cccccccc", "{not json"),
                          ("not-a-task", {"host": "air"})):
            os.makedirs(os.path.join(self.root, tid))
            if stub is not None:
                Path(self.root, tid, child_tasks.REMOTE_STUB).write_text(stub if isinstance(stub, str) else json.dumps(stub))
        before = sorted(os.listdir(self.root))
        self.assertEqual(child_tasks.remote_stubs(),
                         [{"task_id": "t20261111090000-aaaaaaaa", "host": "air", "created_at": NOW}])
        self.assertEqual(sorted(os.listdir(self.root)), before)


class MemoTest(unittest.TestCase):
    def test_ttl_key_and_single_flight(self):
        clock = [0.0]
        m = live.Memo(ttl_s=2.0, clock=lambda: clock[0])
        calls = []
        build = lambda: (calls.append(1), str(len(calls)).encode())[1]
        self.assertEqual((m.get("k", build), m.get("k", build)), (b"1", b"1"))
        self.assertEqual(m.get("k2", build), b"2")                   # another key (a heartbeat posted): rebuilt
        clock[0] = 2.5
        self.assertEqual(m.get("k2", build), b"3")                   # expired
        with self.assertRaises(RuntimeError):
            m.get("k3", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(m.get("k3", build), b"4")                   # a failure is not memoised
        gate, started, n = threading.Event(), threading.Event(), []

        def slow():
            n.append(1)
            started.set()
            gate.wait(5)
            return b"slow"
        m2 = live.Memo(ttl_s=2.0)
        out = []
        ts = [threading.Thread(target=lambda: out.append(m2.get("k", slow))) for _ in range(4)]
        ts[0].start()
        started.wait(5)
        for t in ts[1:]:
            t.start()
        time.sleep(0.05)
        gate.set()
        for t in ts:
            t.join(5)
        self.assertEqual((len(n), out), (1, [b"slow"] * 4))

    def test_several_views_are_memoised_side_by_side(self):
        # the fleet view and each pane's ?session= view are polled together: they must not evict each other
        clock = [0.0]
        m = live.Memo(ttl_s=2.0, clock=lambda: clock[0])
        calls = []
        build = lambda k: (lambda: (calls.append(k), k.encode())[1])
        for _ in range(3):
            for k in ("fleet", "s1", "s2"):
                self.assertEqual(m.get(k, build(k)), k.encode())
        self.assertEqual(calls, ["fleet", "s1", "s2"])
        for i in range(live.MEMO_KEYS + 4):
            m.get(f"x{i}", build(f"x{i}"))
        self.assertLessEqual(len(m._hits), live.MEMO_KEYS)
        clock[0] = 3.0
        m.get("late", build("late"))
        self.assertEqual(list(m._hits), ["late"])                     # expired entries are dropped, not kept


class ClientHostTest(unittest.TestCase):
    D = "a" * 64

    def entry(self, **kw):
        return json.dumps({"version": 1, "clients": [{"name": "studio", "token_sha256": self.D,
                                                      "scopes": ["live:heartbeat"], **kw}]})

    def test_heartbeat_clients_are_bound_to_one_host(self):
        self.assertEqual(clients.parse(self.entry())[0][1].host, "studio")          # the name, when it is a host name
        self.assertEqual(clients.parse(self.entry(name="studio-s1", host="studio"))[0][1].host, "studio")
        for bad in (dict(name="s1_job"), dict(host="Studio"), dict(host="10.0.0.1"), dict(host=7)):
            with self.assertRaises(clients.ClientsError, msg=bad):
                clients.parse(self.entry(**bad))
        with self.assertRaises(clients.ClientsError):                              # host means nothing for a reader
            clients.parse(self.entry(scopes=["live:read"], host="studio"))
        self.assertIsNone(clients.parse(self.entry(scopes=["live:read"]))[0][1].host)

    def test_cli_add_with_host(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cf, tf = os.path.join(d, "clients.json"), os.path.join(d, "beat-token")
        with mock.patch.dict(os.environ, {"CORTADOS_PROVIDER_CLIENTS": cf}):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(clients.main(["add", "studio-s1", "--scope", "live:heartbeat", "--host", "studio",
                                               "--token-file", tf]), 0)
                self.assertEqual(clients.main(["list"]), 0)
                # no host and a name that is not one: refused before any token file is made
                self.assertEqual(clients.main(["add", "s1_job", "--scope", "live:heartbeat", "--token-file",
                                               tf + "2"]), 1)
            self.assertIn("studio-s1: live:heartbeat (host studio)", out.getvalue())
            self.assertFalse(os.path.exists(tf + "2"))
            token = Path(tf).read_text().strip()
            self.assertEqual(clients.ClientTokens(path=Path(cf)).match(token).host, "studio")

    def test_load_logs_heartbeat_bindings_without_tokens(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        cf, tf, rf = (os.path.join(d, n) for n in ("clients.json", "beat-token", "read-token"))
        with mock.patch.dict(os.environ, {"CORTADOS_PROVIDER_CLIENTS": cf}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(clients.main(["add", "studio-s1", "--scope", "live:heartbeat", "--host", "studio",
                                           "--token-file", tf]), 0)
            self.assertEqual(clients.main(["add", "pulse", "--scope", "live:read", "--token-file", rf]), 0)
        token = Path(tf).read_text().strip()
        with self.assertLogs(clients.logger, "INFO") as cm:
            clients.ClientTokens(path=Path(cf)).match(token)
        logs = "\n".join(cm.output)
        self.assertIn("heartbeat bindings: studio-s1 -> studio", logs)
        self.assertNotIn("pulse ->", logs)
        self.assertNotIn(token, logs)


class HeartbeatTest(unittest.TestCase):
    GOOD = {"host": "studio", "jobs": [{"name": "s1-scorer", "state": "running"}]}

    def check(self, body, field):
        hb, bad = live.validate_heartbeat(body, NOW)
        self.assertIsNone(hb, body)
        self.assertEqual(bad, field)

    def test_good(self):
        hb, bad = live.validate_heartbeat(self.GOOD, NOW)
        self.assertIsNone(bad)
        self.assertEqual(hb, {"host": "studio", "ttl_s": 900, "jobs": [
            {"name": "s1-scorer", "state": "running", "summary": None, "started_at": None, "finished_at": None,
             "next_at": None, "progress_pct": None}]})
        self.assertEqual(live.validate_heartbeat({"host": "pro"}, NOW)[0]["jobs"], [])   # alive, no jobs

    def test_refused(self):
        job = self.GOOD["jobs"][0]
        self.check([], "body")
        self.check({**self.GOOD, "token": "x"}, "(unknown field)")
        for host in ("Studio", "10.0.0.1", "studio.local", "", None, 7, "a" * 40):
            self.check({**self.GOOD, "host": host}, "host")
        for ttl in (10, 10 ** 6, True, "900", float("nan")):
            self.check({**self.GOOD, "ttl_s": ttl}, "ttl_s")
        self.check({**self.GOOD, "jobs": [job] * 2}, "jobs[1].name")
        self.check({**self.GOOD, "jobs": [{"name": f"j{i}", "state": "ok"} for i in range(live.JOBS_MAX + 1)]}, "jobs")
        self.check({**self.GOOD, "jobs": "s1"}, "jobs")
        self.check({**self.GOOD, "jobs": [{**job, "cmd": "rm -rf /"}]}, "jobs[0]")
        self.check({**self.GOOD, "jobs": [{**job, "state": "exploded"}]}, "jobs[0].state")
        for s in ("line\nbreak", "at 192.168.1.4", "x" * 81, 5):
            self.check({**self.GOOD, "jobs": [{**job, "summary": s}]}, "jobs[0].summary")
        for k in ("started_at", "finished_at", "next_at"):
            for v in (NOW + 40 * 86400, float("inf"), "now", False):
                self.check({**self.GOOD, "jobs": [{**job, k: v}]}, f"jobs[0].{k}")
        for p in (-1, 101, "50", True):
            self.check({**self.GOOD, "jobs": [{**job, "progress_pct": p}]}, "jobs[0].progress_pct")

    def test_hardware_and_s1_blocks(self):
        hw = {"cpu_busy_pct": 12.345, "gpu_busy_pct": None, "mem_available_bytes": 2 ** 33, "mem_total_bytes": 2 ** 34,
              "load_1m": 1.234, "disk_free_pct": 55, "chip": "Apple M4 Max", "sampled_at": NOW - 5}
        s1 = {"health": "up", "model": "CortadOS Router S1 2026-11-09 (run-a)", "flags": ["nightly-stop", "no-promote"],
              "pending": "promotion_held", "loaded_s": 3}
        hb, bad = live.validate_heartbeat({**self.GOOD, "hardware": hw, "s1": s1}, NOW)
        self.assertIsNone(bad)
        self.assertEqual(hb["hardware"], {**hw, "cpu_busy_pct": 12.3, "load_1m": 1.23, "disk_free_pct": 55.0})
        self.assertEqual(hb["s1"], {**s1, "flags": ["no-promote", "nightly-stop"], "loaded_s": 3.0})
        self.assertEqual(live.validate_heartbeat({**self.GOOD, "s1": {}}, NOW)[0]["s1"],
                         {"health": "unknown", "model": None, "flags": [], "pending": None, "loaded_s": None})
        self.assertNotIn("hardware", live.validate_heartbeat(self.GOOD, NOW)[0])
        # a full sender body (16 jobs with 80-char summaries, both blocks) stays under the byte cap
        full = {"host": "studio", "ttl_s": 180, "hardware": hw, "s1": s1, "jobs": [
            {"name": f"com.cortados.job-{i:02d}", "state": "running", "summary": "x" * 80, "started_at": NOW,
             "finished_at": NOW, "progress_pct": 50.0} for i in range(live.JOBS_MAX)]}
        self.assertLess(len(json.dumps(full).encode()), live.HEARTBEAT_MAX_BYTES)
        self.assertIsNone(live.validate_heartbeat(full, NOW)[1])

    def test_hardware_and_s1_refused(self):
        self.check({**self.GOOD, "hardware": []}, "hardware")
        self.check({**self.GOOD, "hardware": {"path": "/Users/x"}}, "hardware")
        for k, v in (("cpu_busy_pct", 101), ("disk_free_pct", -1), ("gpu_busy_pct", "5"), ("mem_total_bytes", 2 ** 60),
                     ("load_1m", float("nan")), ("chip", "M4\nMax"), ("chip", "10.0.0.1"), ("sampled_at", NOW + 40 * 86400),
                     ("sampled_at", NOW + live.SAMPLE_FUTURE_SKEW_S + 1),
                     ("mem_available_bytes", True)):
            self.check({**self.GOOD, "hardware": {k: v}}, "hardware." + k)
        self.check({**self.GOOD, "s1": "up"}, "s1")
        self.check({**self.GOOD, "s1": {"token": "x"}}, "s1")
        self.check({**self.GOOD, "s1": {"health": "great"}}, "s1.health")
        for f in (["no-promote", "no-promote"], ["data/no-promote"], "no-promote", ["x"] * 5):
            self.check({**self.GOOD, "s1": {"flags": f}}, "s1.flags")
        for p in ("Promotion Held", "/Users/x/run", "a" * 49):
            self.check({**self.GOOD, "s1": {"pending": p}}, "s1.pending")
        self.check({**self.GOOD, "s1": {"model": "line\nbreak"}}, "s1.model")
        self.check({**self.GOOD, "s1": {"loaded_s": -1}}, "s1.loaded_s")

    def test_board_is_bounded_and_forgets(self):
        b = live.HeartbeatBoard()
        for i in range(live.HEARTBEAT_HOSTS_MAX + 3):
            b.post(live.validate_heartbeat({"host": f"h{i}"}, NOW)[0], NOW)
        snap = b.snapshot(NOW)
        self.assertEqual(len(snap), live.HEARTBEAT_HOSTS_MAX)
        self.assertNotIn("h0", snap)
        self.assertEqual(b.snapshot(NOW + live.HEARTBEAT_KEEP_S + 1), {})

    def test_eviction_never_drops_a_configured_host(self):
        b = live.HeartbeatBoard(protected=lambda: {"air", "studio"})
        gens = []
        for h in ["studio", "air"] + [f"x{i}" for i in range(live.HEARTBEAT_HOSTS_MAX + 5)]:
            b.post(live.validate_heartbeat({"host": h}, NOW)[0], NOW)
            gens.append(b.generation)
        snap = b.snapshot(NOW)
        self.assertEqual(len(snap), live.HEARTBEAT_HOSTS_MAX)
        self.assertIn("studio", snap)
        self.assertIn("air", snap)                                   # the oldest posts, kept: configured
        self.assertNotIn("x0", snap)
        self.assertEqual(gens, sorted(set(gens)))                    # every post moves the generation
        def broken():
            raise OSError("exec-hosts unreadable")
        b = live.HeartbeatBoard(protected=broken)                    # a failing list: the plain LRU, still bounded
        for i in range(live.HEARTBEAT_HOSTS_MAX + 2):
            b.post(live.validate_heartbeat({"host": f"h{i}"}, NOW)[0], NOW)
        self.assertEqual(len(b.snapshot(NOW)), live.HEARTBEAT_HOSTS_MAX)


class PeekTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, {"CORTADOS_TASKS_DIR": self.root})
        p.start()
        self.addCleanup(p.stop)

    def test_peek_is_read_only(self):
        self.assertIsNone(child_tasks.peek_local("../x"))
        self.assertIsNone(child_tasks.peek_local(RUNNING))         # no dir here
        d = os.path.join(self.root, RUNNING)
        os.makedirs(d)
        Path(d, child_tasks.QUEUED_MARKER).write_text("")
        Path(d, child_tasks.WORKSPACE_FILE).write_text(json.dumps({"state": "active", "path": "/Users/x/active/t"}))
        before = sorted(os.listdir(d))
        self.assertEqual(child_tasks.peek_local(RUNNING), {"phase": "queued", "ended": False, "workspace": "active"})
        self.assertEqual(sorted(os.listdir(d)), before)            # nothing written, reaped or recovered
        Path(d, child_tasks.WORKSPACE_FILE).write_text("{not json")
        self.assertEqual(child_tasks.peek_local(RUNNING)["workspace"], None)
        Path(d, child_tasks.REMOTE_STUB).write_text(json.dumps({"host": "air"}))
        self.assertIsNone(child_tasks.peek_local(RUNNING))         # a remote task is never peeked (nor asked over ssh)

    def test_event_fields_carry_the_host(self):
        self.assertEqual(child_tasks._event_fields({"task_id": RUNNING, "host": "air"})["host"], "air")
        self.assertNotIn("host", child_tasks._event_fields({"task_id": RUNNING}))


from tests.test_provider import TOKEN, EndpointHarness   # noqa: E402

LIVE_TOKEN = "live-reader-token-for-tests-01"
BEAT_TOKEN = "live-heartbeat-token-tests-01"
OFFICE_TOKEN = "office-token-for-tests-0001"


class EndpointTest(EndpointHarness):
    """The real endpoint: auth, scopes, the heartbeat's limits, and a routed prompt never reaching the snapshot."""

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        self.tasks = tempfile.mkdtemp()
        for d in (self.home, self.tasks):
            self.addCleanup(shutil.rmtree, d, True)
        env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_TASKS_DIR": self.tasks,
                                           "CORTADOS_EXEC_HOSTS_FILE": os.path.join(self.home, "absent.json")})
        env.start()
        self.addCleanup(env.stop)
        cf = Path(self.scratch) / "clients.json"
        cf.write_text(json.dumps({"version": 1, "clients": [
            {"name": "phone", "token_sha256": clients.token_digest(LIVE_TOKEN), "scopes": ["live:read"]},
            {"name": "studio", "token_sha256": clients.token_digest(BEAT_TOKEN), "scopes": ["live:heartbeat"]},
            {"name": "claw3d", "token_sha256": clients.token_digest(OFFICE_TOKEN), "scopes": ["office:read"]}]}))
        self.reg.clients = clients.ClientTokens(path=cf, owner_token=TOKEN)
        # the usage slice reads scarcity.json and probes S1: replaced, so no network call leaves the test
        self.httpd.live_usage_view = lambda reg, now: {"generated_at": now, "lanes": [], "hardware": [],
                                                       "s1": {"available": False, "reason": "no S1 URL configured"},
                                                       "errors": [{"source": "scarcity", "error": "FileNotFoundError"}]}

    def request(self, method, path, token=None, body=None, raw=None, headers=None):
        h = {"content-type": "application/json", **(headers or {})}
        if token is not None:
            h["authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else
                                            (b"{}" if method == "POST" else None))
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_session_filter(self):
        code, raw, _ = self.request("GET", live.LIVE_PATH + "?session=" + SESSION, TOKEN)
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["session_filter"], SESSION)
        for bad in ("?session=nothex", "?session=" + SESSION + "&session=" + SESSION, "?session="):
            code, raw, _ = self.request("GET", live.LIVE_PATH + bad, TOKEN)
            self.assertEqual(code, 400 if bad != "?session=" else 200, (bad, raw))
            self.assertNotIn("nothex", raw)
        self.assertEqual(self.request("GET", live.LIVE_PATH + "?session=" + SESSION, LIVE_TOKEN)[0], 200)
        self.assertEqual(self.request("GET", live.LIVE_PATH + "?session=" + SESSION)[0], 401)

    def test_paths_agree(self):
        self.assertEqual((clients.LIVE_PATH, clients.LIVE_HEARTBEAT_PATH), (live.LIVE_PATH, live.HEARTBEAT_PATH))

    def test_auth_and_scopes(self):
        for tok in (None, "wrong-token-000000000000"):
            self.assertEqual(self.request("GET", live.LIVE_PATH, tok)[0], 401)
            self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, tok, body={"host": "studio"})[0], 401)
        code, raw, headers = self.request("GET", live.LIVE_PATH, TOKEN)
        self.assertEqual(code, 200, raw)
        self.assertEqual(headers["cache-control"], "no-store")
        v = json.loads(raw)
        self.assertEqual(v["version"], 1)
        self.assertEqual(v["subscriptions"]["status"], "error")    # the missing scarcity cache is said, not hidden
        self.assertEqual(v["routed"]["errors"], [{"source": "router.db", "error": "absent"}])
        self.assertEqual(self.request("GET", live.LIVE_PATH, LIVE_TOKEN)[0], 200)
        for p in ("/v1/cortados/usage", "/v1/cortados/tasks", "/v1/cortados/office", "/resources"):
            self.assertEqual(self.request("GET", p, LIVE_TOKEN)[0], 403, p)
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, LIVE_TOKEN, body={"host": "studio"})[0], 403)
        self.assertEqual(self.request("GET", live.LIVE_PATH, OFFICE_TOKEN)[0], 403)
        self.assertEqual(self.request("GET", live.LIVE_PATH, BEAT_TOKEN)[0], 403)
        self.assertEqual(self.request("POST", "/v1/messages", BEAT_TOKEN,
                                      body={"model": "cortados", "messages": [{"role": "user", "content": "x"}]})[0], 403)
        self.assertEqual(self.reg.sessions, {})

    def test_heartbeat_post_limits_and_view(self):
        good = {"host": "studio", "jobs": [{"name": "s1-scorer", "state": "running", "summary": "serving v12"}]}
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, BEAT_TOKEN, body=good)
        self.assertEqual((code, json.loads(raw)), (200, {"ok": True, "host": "studio", "jobs": 1, "fresh_for_s": 900}))
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, TOKEN, body={"host": "pro"})[0], 200)  # owner: any
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, BEAT_TOKEN, body={"host": "air"})
        self.assertEqual(code, 403, raw)                             # the studio token speaks for studio only
        self.assertIn("for host studio only", raw)
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, TOKEN,
                                    body={"host": "studio", "jobs": [{"name": "x", "state": "running", "summary": SECRET + "\nmore"}]})
        self.assertEqual(code, 400)
        self.assertIn("jobs[0].summary", raw)
        self.assertNotIn(SECRET, raw)
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, TOKEN, raw=b"{not json")
        self.assertEqual(code, 400)
        big = json.dumps({"host": "studio", "pad": "x" * live.HEARTBEAT_MAX_BYTES}).encode()
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, TOKEN, raw=big)[0], 413)
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, TOKEN, raw=b"[]")[0], 400)
        v = json.loads(self.request("GET", live.LIVE_PATH, TOKEN)[1])
        h = {x["name"]: x for x in v["hosts"]["items"]}
        self.assertEqual(h["studio"]["heartbeat_state"], "fresh")
        self.assertEqual(h["studio"]["heartbeat"]["jobs"][0]["summary"], "serving v12")
        self.assertEqual((h["pro"]["heartbeat_state"], h["pro"]["heartbeat"]["jobs"]), ("fresh", []))
        self.assertEqual(h["air"]["heartbeat_state"] if "air" in h else "none", "none")

    def test_the_view_is_memoised_and_a_heartbeat_refreshes_it(self):
        n = []
        real = self.httpd.live_view

        def counted(reg, session=None):
            n.append(session)
            return real(reg, session)
        self.httpd.live_view = counted
        for _ in range(3):
            self.assertEqual(self.request("GET", live.LIVE_PATH, TOKEN)[0], 200)
        self.assertEqual(len(n), 1)                                  # one collect per live.MEMO_S
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, BEAT_TOKEN, body={"host": "studio"})[0], 200)
        v = json.loads(self.request("GET", live.LIVE_PATH, LIVE_TOKEN)[1])
        self.assertEqual(len(n), 2)                                  # the post invalidated it
        self.assertEqual({x["name"]: x for x in v["hosts"]["items"]}["studio"]["heartbeat_state"], "fresh")
        for _ in range(2):                                           # a session view: its own collect, memoised too
            self.assertEqual(self.request("GET", live.LIVE_PATH + "?session=" + SESSION, TOKEN)[0], 200)
            self.assertEqual(self.request("GET", live.LIVE_PATH, TOKEN)[0], 200)
        self.assertEqual(n[2:], [SESSION])

    def test_remote_stubs_reach_the_view(self):
        tid = "t20261111120000-abcdef01"
        os.makedirs(os.path.join(self.tasks, tid))
        Path(self.tasks, tid, child_tasks.REMOTE_STUB).write_text(json.dumps({"host": "air", "created_at": time.time() - 60}))
        v = json.loads(self.request("GET", live.LIVE_PATH, TOKEN)[1])
        self.assertEqual([(x["task_id"], x["host"]) for x in v["tasks"]["elsewhere"]], [(tid, "air")])
        self.assertEqual(v["tasks"]["scope"], "provider host")

    def test_routed_prompt_and_child_log_never_reach_the_snapshot(self):
        tags = {"x-cortados-session": SESSION, "x-cortados-project": "CortadOS-Router", "x-cortados-device": "pro",
                "x-cortados-task-title": "Summarise the notes", "x-cortados-run-id": "run-live-0001"}
        code, raw, _ = self.request("POST", "/v1/messages", TOKEN, headers=tags,
                                    body={"model": "cortados", "messages": [{"role": "user", "content": SECRET}]})
        self.assertEqual(code, 200, raw)
        Path(self.home, "child-tasks.jsonl").write_text("\n".join(json.dumps(e) for e in events()
                                                                  if isinstance(e, dict)) + "\n")
        code, raw, _ = self.request("GET", live.LIVE_PATH, TOKEN)
        self.assertEqual(code, 200, raw)
        for bad in (SECRET, TOKEN, "/Users/", "secret-project", "acceptEdits"):
            self.assertNotIn(bad, raw, bad)
        v = json.loads(raw)
        self.assertIn("Summarise the notes", raw)                 # the title the client asserted, as the office shows it
        self.assertEqual(v["conversations"]["backend_sessions"], len(self.reg.sessions))
        # the child log in this throwaway home is real input: events() are relative to NOW, so all are outside 24 h now
        self.assertEqual(v["tasks"]["status"], "ok")
        # a tool-less request is a one-shot (no open conversation): it is listed from the board, by its run id
        items = {x["run_id"]: x for x in v["conversations"]["items"]}
        self.assertEqual((items["run-live-0001"]["source"], items["run-live-0001"]["title"]),
                         ("routed", "Summarise the notes"))

    def test_the_sender_reaches_the_view(self):
        # cortados_router/live_heartbeat.py end to end: a fake launchctl and sample, the real endpoint and its
        # host-bound token; the Studio's S1 status and hardware show on its host card
        from cortados_router import live_heartbeat as hbs
        from tests.test_live_heartbeat import PRINT_RUNNING, fake_launchctl, fake_sample
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "data").mkdir()
        (d / "data" / "no-promote").touch()
        tok = d / "token"
        tok.write_text(BEAT_TOKEN)
        os.chmod(tok, 0o600)
        cfg = d / "heartbeat.json"
        cfg.write_text(json.dumps({"host": "studio", "provider_url": self.url, "token_file": str(tok), "ttl_s": 180,
                                   "jobs": [{"label": "com.cortados.s1-serve", "name": "s1-scorer"}],
                                   "s1": {"data_dir": str(d / "data"), "health_url": "http://s1.test"}}))
        with redirect_stderr(io.StringIO()):
            code = hbs.main(["--config", str(cfg)], run=fake_launchctl({"com.cortados.s1-serve": PRINT_RUNNING}),
                            sample=fake_sample, fetch=lambda u: b'{"ok": true, "model": "CortadOS Router S1 v12"}')
        self.assertEqual(code, 0)
        h = {x["name"]: x for x in json.loads(self.request("GET", live.LIVE_PATH, TOKEN)[1])["hosts"]["items"]}
        st = h["studio"]
        self.assertEqual((st["heartbeat_state"], st["heartbeat"]["jobs"][0]["state"]), ("fresh", "running"))
        self.assertEqual((st["heartbeat"]["s1"]["model"], st["heartbeat"]["s1"]["flags"]),
                         ("CortadOS Router S1 v12", ["no-promote"]))
        self.assertEqual((st["hardware"]["source"], st["hardware"]["chip"]), ("heartbeat", "Apple M2"))

    def test_open_conversations_reach_the_view(self):
        # Registry.live_conversations: the open conversation, joined with its board worker by the run id it carries
        tags = {"x-cortados-session": SESSION, "x-cortados-project": "CortadOS-Router",
                "x-cortados-run-id": "t20261111160000-0a0b0c0d"}
        from tests.test_provider import READ_TOOL
        code, raw, _ = self.request("POST", "/v1/messages", TOKEN, headers=tags, body={
            "model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "please use tool"}]})
        self.assertEqual(code, 200, raw)
        self.assertEqual(len(self.reg.sessions), 1)
        convs = self.reg.live_conversations()
        self.assertEqual(len(convs), 1)
        c = convs[0]
        self.assertEqual((c["key"], c["busy"], c["awaiting_tool"], c["provider"]),
                         ("r:t20261111160000-0a0b0c0d", False, True, "claude"))
        self.assertTrue(c["ref"].startswith("c-") and len(c["ref"]) == 14)
        self.assertNotIn(next(iter(self.reg.sessions)), json.dumps(convs))   # the session id itself is never published
        self.assertTrue(_n(c["opened_at"]) and _n(c["last_used"]))
        v = json.loads(self.request("GET", live.LIVE_PATH, TOKEN)[1])["conversations"]
        it = {x["run_id"]: x for x in v["items"]}["t20261111160000-0a0b0c0d"]
        self.assertEqual((it["source"], it["status"], it["phase"], it["tool"], it["session"]),
                         ("provider", "working", "client running a tool", "Read", SESSION))
        self.assertEqual((v["backend_sessions"], v["active"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
