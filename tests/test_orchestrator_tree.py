"""The orchestrator tree (provider/live.py orchestrators_section, session_view, GET /v1/cortados/live/session):
orchestrating sessions -> their child tasks (nested through parent_task_id) -> each task's sub-sessions, the bot
gateway as an orchestrator, the unattributed group, and the per-session view that never holds another session's
metadata. Fakes only: a throwaway CORTADOS_HOME, no child is launched, no vendor CLI or S1 probe runs."""
import json
import os
import shutil
import tempfile
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import child_tasks
from cortados_router.provider import clients, live, work_split
from tests import test_live as tl

NOW = tl.NOW
SECRET = "SECRET-PROMPT-TEXT-0099"
MINE = "aaaaaaaaaaaaaaa1"                   # this pane's session (an Office tag)
OTHER = "bbbbbbbbbbbbbbb2"                  # another orchestrating session on the same provider
ROOT = "t20261111090000-a0000001"           # MINE's task, running, with sub-sessions
NESTED = "t20261111090500-a0000002"         # started with parent ROOT and no origin of its own: inherits MINE
NESTED2 = "t20261111090600-a0000003"        # nested under NESTED (depth 2)
FOLLOW = "t20261111091000-a0000004"         # parent ROOT, but OTHER's own: OTHER's tree, not nested under ROOT
THEIRS = "t20261111080000-b0000001"         # OTHER's, failed
BOT = "t20261111070000-c0000001"            # Grok Bot's
LEGACY = "t20261111060000-d0000001"         # no origin, no parent: unattributed
CYCLE_A = "t20261111050000-e0000001"        # a parent cycle with no origin: unattributed, never a hang
CYCLE_B = "t20261111050100-e0000002"
AWAY = "t20261111093000-a0000009"           # MINE's, started onto another host (a remote stub only)


def events():
    base = {"cwd": "/Users/someone/secret-project", "orchestrator": "claude-app", "chrome": False, "prompt": SECRET,
            "task_preview": SECRET}
    mine = {"origin_session": MINE, "origin_project": "CortadOS-Router", "origin_device": "pro"}
    other = {"origin_session": OTHER, "origin_project": "Grounds", "origin_device": "studio"}
    return [
        {"ts": NOW - 300, "event": "started", "task_id": ROOT, **base, **mine, "host": "air", "lane": "claude#b",
         "model": "claude-sonnet-5", "title": "Build the tree"},
        {"ts": NOW - 200, "event": "started", "task_id": NESTED, **base, "host": "air", "title": "Nested step",
         "parent_task_id": ROOT},
        {"ts": NOW - 150, "event": "started", "task_id": NESTED2, **base, "host": "air", "title": "Deeper step",
         "parent_task_id": NESTED},
        {"ts": NOW - 100, "event": "finished", "task_id": NESTED2, **base, "status": "succeeded", "duration_s": 50.0,
         "parent_task_id": NESTED},
        {"ts": NOW - 120, "event": "started", "task_id": FOLLOW, **base, **other, "title": "Follow-up by Grounds",
         "parent_task_id": ROOT},
        {"ts": NOW - 4000, "event": "started", "task_id": THEIRS, **base, **other, "title": "Their secret plan"},
        {"ts": NOW - 3000, "event": "finished", "task_id": THEIRS, **base, **other, "status": "failed",
         "duration_s": 1000.0},
        {"ts": NOW - 5000, "event": "started", "task_id": BOT, **base, "client": "grokbot", "origin_session": "grokbot",
         "title": "Bot errand"},
        {"ts": NOW - 4500, "event": "finished", "task_id": BOT, **base, "origin_session": "grokbot",
         "status": "succeeded", "duration_s": 500.0},
        {"ts": NOW - 6000, "event": "started", "task_id": LEGACY, **base, "title": "Legacy task"},
        {"ts": NOW - 5900, "event": "finished", "task_id": LEGACY, **base, "status": "succeeded", "duration_s": 100.0},
        {"ts": NOW - 7000, "event": "started", "task_id": CYCLE_A, **base, "parent_task_id": CYCLE_B},
        {"ts": NOW - 6990, "event": "started", "task_id": CYCLE_B, **base, "parent_task_id": CYCLE_A},
        {"ts": NOW - 6900, "event": "finished", "task_id": CYCLE_A, **base, "status": "succeeded", "duration_s": 100.0},
        {"ts": NOW - 6900, "event": "finished", "task_id": CYCLE_B, **base, "status": "succeeded", "duration_s": 90.0},
    ]


def backend():
    """Open provider conversations: ROOT's main loop (a turn running) and a subagent of it (running a tool), OTHER's
    main loop and its routed subagent, and MINE's own routed subagent (route_task)."""
    return [
        {"key": "r:" + ROOT, "ref": "c-00000000000a", "provider": "claude", "lane": "claude#b",
         "model": "claude-sonnet-5", "opened_at": NOW - 290, "last_used": NOW - 5, "busy": True, "awaiting_tool": False,
         "tokens": {"input": 3000, "output": 900, "cache_read": 40000, "cache_write": 1000}},
        {"key": "r:" + ROOT, "ref": "c-00000000000b", "provider": "codex", "lane": "codex", "model": "gpt-6.1",
         "opened_at": NOW - 60, "last_used": NOW - 10, "busy": False, "awaiting_tool": True},
        {"key": "r:" + NESTED, "ref": "c-00000000000c", "provider": "claude", "lane": "claude#a",
         "model": "claude-haiku-5", "opened_at": NOW - 190, "last_used": NOW - 30, "busy": False,
         "awaiting_tool": False},
        {"key": "main:" + OTHER, "ref": "c-00000000000d", "provider": "claude", "lane": "claude#a",
         "model": "claude-opus-5", "opened_at": NOW - 900, "last_used": NOW - 20, "busy": True, "awaiting_tool": False},
        {"key": "r:other-sub-0001", "ref": "c-00000000000e", "provider": "codex", "lane": "codex",
         "model": "gpt-6.1", "opened_at": NOW - 50, "last_used": NOW - 5, "busy": True, "awaiting_tool": False},
        {"key": "r:mine-sub-00001", "ref": "c-00000000000f", "provider": "codex", "lane": "codex",
         "model": "gpt-6.1-mini", "opened_at": NOW - 40, "last_used": NOW - 4, "busy": True, "awaiting_tool": False},
    ]


_TAGS = {ROOT: {"session": MINE}, NESTED: {"session": MINE}, OTHER: {"session": OTHER, "project": "Grounds"},
         "other-sub-0001": {"session": OTHER, "project": "Grounds", "title": "Their subagent title"},
         "mine-sub-00001": {"session": MINE, "project": "CortadOS-Router", "title": "Check the docs"}}


def tagged(convs):
    """Conversations with their own Office tags, as Registry._conversation_meta reports them (the request's tags)."""
    return [{**c, **_TAGS.get(c["key"].partition(":")[2], {})} for c in convs]


def ended():
    return [{"key": "r:" + ROOT, "ref": "c-000000000010", "provider": "local", "lane": "local-studio",
             "model": "qwen3-30b", "opened_at": NOW - 200, "last_used": NOW - 120, "closed_at": NOW - 110,
             "closed": "reaped", "tokens": {"input": 800, "output": 200, "cache_read": 0, "cache_write": 0}},
            {"key": "r:" + THEIRS, "ref": "c-000000000011", "provider": "claude", "opened_at": NOW - 3900,
             "closed_at": NOW - 3001, "closed": "failed"}]


def board():
    b = tl.office.OfficeBoard()
    mine = {"session": MINE, "project": "CortadOS-Router", "device": "pro"}
    other = {"session": OTHER, "project": "Grounds", "device": "studio"}
    b.turn_start("r:" + ROOT, mine, run_id=ROOT, provider="claude", lane="claude#b", model="claude-sonnet-5",
                 now=NOW - 290)
    b.turn_end("r:" + ROOT, mine, status="working", tool="Edit", now=NOW - 5)
    b.turn_start("main:" + OTHER, other, run_id=None, provider="claude", lane="claude#a", model="claude-opus-5",
                 now=NOW - 900)
    b.turn_start("r:other-sub-0001", {**other, "title": "Their subagent title"}, run_id="other-sub-0001",
                 provider="codex", lane="codex", model="gpt-6.1", now=NOW - 50)
    b.turn_start("r:mine-sub-00001", {**mine, "title": "Check the docs"}, run_id="mine-sub-00001", provider="codex",
                 lane="codex", model="gpt-6.1-mini", now=NOW - 40)
    b.heartbeat({"session": MINE, "status": "working", "project": "CortadOS-Router", "device": "pro",
                 "model": "claude-opus-5", "tool": "mcp__cortados-route__start_task"}, now=NOW - 3)
    return b


def own_stubs(stubs_fn):
    """session_stubs for collect: the origin index's answer (child_tasks.remote_stubs_for) simulated from a stub list,
    every stub of that origin, with no foreign stub and no fleet scan flag."""
    def by_origin(tag):
        return child_tasks.StubList(x for x in stubs_fn() if x.get("origin_session") == tag)
    return by_origin


def view(session=None, **over):
    b = board()
    args = dict(now=NOW, child_events=events, office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                tasks_view=lambda: tl.tasks_view(3), usage_view=tl.usage_live,
                heartbeats=lambda: tl.heartbeats().snapshot(NOW), peek=None, conversations=lambda: tagged(backend()),
                ended_conversations=ended, run_tokens=lambda: {ROOT: {"input": 9000, "output": 2000,
                                                                      "cache_read": 50000, "cache_write": 3000}},
                remote_stubs=lambda: [{"task_id": AWAY, "host": "studio", "created_at": NOW - 30,
                                       "origin_session": MINE, "origin_project": "CortadOS-Router"}],
                exec_hosts=lambda: ({"air": ["forests-macbook-air"]}, "air"), self_host="forests-macbook-air",
                host_names=["air", "studio", "pro"], session=session)
    args.update(over)
    if "session_stubs" not in over:                 # as the server: the session's own stubs from the origin index
        args["session_stubs"] = own_stubs(args["remote_stubs"] or (lambda: []))
    return live.collect(**args)


def groups(v):
    return {g["origin"]: g for g in v["orchestrators"]["items"]}


def ids(node):
    """Every task id in a node's subtree."""
    out = []
    for t in node.get("tasks") or []:
        out.append(t["task_id"])
        out.extend(ids(t))
    return out


# ---- the per-session work split (#127) over the same tree fixtures
CHILD_MCP_TAG = "c0ffeec0ffee0001"     # a child's own x-cortados-session: the cortados-route server's random tag


def split_token_totals():
    """work_split.routed_tokens over a real routes table: MINE's child task ROOT (origin observed on its requests), its
    nested NESTED (a run whose requests carried no origin: the provider host's task log names MINE through ROOT), a
    routed subagent and a route_task call of MINE's, and OTHER's follow-up FOLLOW."""
    import sqlite3
    from cortados_router import core
    from cortados_router.provider import work_split
    c = sqlite3.connect(":memory:")
    c.executescript(core.DDL)
    for col, kind in (("scarcity", "TEXT"), ("context", "TEXT"), ("task_context", "TEXT"), *core.TOKEN_COLUMNS):
        c.execute(f"ALTER TABLE routes ADD COLUMN {col} {kind}")
    for tid, ts, lane, model, sess, origin, run, via, tok in (
            ("w-1", NOW - 300, "claude#b", "claude-opus-5-5", CHILD_MCP_TAG, MINE, ROOT, None, (300000, 120000, 4000000, 80000)),
            ("w-2", NOW - 200, "codex", "gpt-6.1-sol", CHILD_MCP_TAG, None, NESTED, None, (600000, 200000, 0, 0)),
            ("w-3", NOW - 100, "codex", "gpt-6.1-mini", MINE, None, None, None, (40000, 10000, 0, 0)),
            ("w-4", NOW - 50, "claude#b", "claude-haiku-5", MINE, None, None, "route_task", (20000, 5000, 0, 0)),
            ("w-5", NOW - 120, "codex", "gpt-6.1", CHILD_MCP_TAG, OTHER, FOLLOW, None, (999, 999, 0, 0))):
        c.execute("INSERT INTO routes (turn_id, ts, lane, model, request, office_session, office_origin, office_run, "
                  "office_via, tok_input, tok_output, tok_cache_read, tok_cache_write, tok_turns) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)", (tid, ts, lane, model, SECRET, sess, origin, run, via, *tok))
    try:
        return work_split.routed_tokens(c, NOW)
    finally:
        c.close()


def split_tasks_view():
    return {**tl.tasks_view(3), "token_totals": split_token_totals()}


def split_orchestrator_block(at=NOW - 60):
    z = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    mine = {"input": 50000, "output": 250000, "cache_read": 30000000, "cache_write": 400000}
    return {"version": 2, "sampled_at": at, "salt": True, "salt_source": "shared", "windows": {"24h": 86400, "7d": 604800},
            "totals": {w: {"input": 50100, "output": 250100, "cache_read": 30000000, "cache_write": 400000, "sessions": 2}
                       for w in ("24h", "7d")},
            "sessions": [{"session": MINE, "project": "CortadOS-Router", "model": "claude-opus-5-5", "last_at": NOW - 70,
                          "24h": mine, "7d": mine,
                          "models": {"claude-opus-5-5": {w: [45000, 240000, 29000000, 380000] for w in ("24h", "7d")},
                                     "claude-haiku-5": {w: [5000, 10000, 1000000, 20000] for w in ("24h", "7d")}}},
                         {"session": OTHER, "project": "Grounds", "model": "claude-opus-5", "last_at": NOW - 90,
                          "24h": dict(z, input=100, output=100), "7d": dict(z, input=100, output=100)}],
            "sessions_hidden": 0, "excluded": {"child_task_files": 1, "routed_model_entries": 3, "unreadable_files": 0,
                                               "bad_lines": 0}}


def split_heartbeats(at=NOW - 60):
    b = tl.heartbeats()
    hb, bad = live.validate_heartbeat({"host": "pro", "ttl_s": 900, "jobs": [],
                                       "orchestrator": split_orchestrator_block(at)}, NOW)
    assert bad is None, bad
    b.post(hb, at)
    return b


def split_view(session=None, at=NOW - 60, **over):
    return view(session, tasks_view=split_tasks_view, heartbeats=lambda: split_heartbeats(at).snapshot(NOW), **over)


def session_fixture():
    """GET /v1/cortados/live/session?session=MINE as the pane receives it (hooks/session.fixture.ts), with MINE's work
    split: native on the Pro, routed through a child task, its nested child, a routed subagent and a route_task call."""
    return live.session_view(split_view(MINE), MINE)


FIXTURE = Path(__file__).resolve().parents[1] / "integrations/claude-mod/cortados-pulse/hooks/session.fixture.ts"


def write_session_fixture():
    """Regenerate hooks/session.fixture.ts after a deliberate change to the session view's shape."""
    FIXTURE.write_text("// Generated by tests.test_orchestrator_tree.session_fixture; checked against live.session_view."
                       "\nexport default " + json.dumps(session_fixture(), indent=2, ensure_ascii=False) + "\n")


class TreeTest(unittest.TestCase):
    def test_every_orchestrator_is_a_group(self):
        g = groups(view())
        self.assertEqual(set(g), {MINE, OTHER, "grokbot", None})
        self.assertEqual({k: v["kind"] for k, v in g.items()},
                         {MINE: "session", OTHER: "session", "grokbot": "bot", None: "unattributed"})
        self.assertEqual(g[MINE]["status"], "working")                             # its heartbeat
        self.assertEqual(g[MINE]["tool"], "mcp__cortados-route__start_task")
        self.assertEqual(g[OTHER]["status"], "working")                            # its routed main loop
        self.assertEqual(view()["overview"]["orchestrators"], 3)
        order = [x["origin"] for x in view()["orchestrators"]["items"]]
        self.assertEqual(order[-1], None)                                           # unattributed last

    def test_nested_tasks_inherit_their_roots_orchestrator(self):
        mine = groups(view())[MINE]
        self.assertEqual([t["task_id"] for t in mine["tasks"]], [ROOT, AWAY])
        root = mine["tasks"][0]
        self.assertEqual([t["task_id"] for t in root["tasks"]], [NESTED])
        nested = root["tasks"][0]
        self.assertEqual((nested["origin_via"], nested["parent_task_id"]), ("parent", ROOT))
        self.assertEqual([(t["task_id"], t["status"], t["origin_via"]) for t in nested["tasks"]],
                         [(NESTED2, "done", "parent")])
        self.assertEqual(mine["counts"], {"running": 2, "waiting": 0, "done": 1, "failed": 0, "unknown": 1})

    def test_a_follow_up_with_its_own_origin_stays_with_its_orchestrator(self):
        g = groups(view())
        self.assertNotIn(FOLLOW, ids(g[MINE]))
        follow = next(t for t in g[OTHER]["tasks"] if t["task_id"] == FOLLOW)
        self.assertEqual((follow["parent_task_id"], follow["parent_hidden"], follow["origin_via"]), (ROOT, False, None))
        # in OTHER's own view the parent is another orchestrator's task: said, never named
        follow = next(t for t in view(OTHER)["orchestrators"]["items"][0]["tasks"] if t["task_id"] == FOLLOW)
        self.assertEqual((follow["parent_task_id"], follow["parent_hidden"]), (None, True))

    def test_sub_sessions_under_their_task(self):
        root = groups(view())[MINE]["tasks"][0]
        subs = root["sub_sessions"]
        self.assertEqual([(s["ref"], s["role"], s["status"], s["phase"]) for s in subs], [
            ("c-00000000000a", "main", "running", "model turn"),
            ("c-00000000000b", "subagent", "running", "running a tool"),
            ("c-000000000010", "subagent", "done", "closed idle")])
        self.assertEqual(subs[0]["tokens"], {"input": 3000, "output": 900, "cache_read": 40000, "cache_write": 1000,
                                             "fresh": 4900})
        self.assertIsNone(subs[1]["tokens"])                                        # none recorded: never estimated
        self.assertEqual((subs[2]["elapsed_s"], subs[2]["model"]), (90.0, "qwen3-30b"))
        self.assertEqual(root["tokens"]["fresh"], 14000)                            # the run's own total
        self.assertEqual((root["tool"], root["model"], root["host"]), ("Edit", "claude-sonnet-5", "air"))
        nested = root["tasks"][0]
        self.assertEqual([(s["role"], s["status"]) for s in nested["sub_sessions"]], [("main", "waiting")])
        theirs = next(t for t in groups(view())[OTHER]["tasks"] if t["task_id"] == THEIRS)
        self.assertEqual([(s["status"], s["phase"]) for s in theirs["sub_sessions"]], [("failed", "failed")])

    def test_an_orchestrators_own_routed_subagents(self):
        g = groups(view())
        self.assertEqual([(r["ref"], r["title"], r["status"]) for r in g[MINE]["routed"]],
                         [("mine-sub-00001", "Check the docs", "running")])
        self.assertEqual([r["ref"] for r in g[OTHER]["routed"]], ["other-sub-0001"])

    def test_the_bot_and_the_unattributed_group(self):
        g = groups(view())
        self.assertEqual([(t["task_id"], t["status"]) for t in g["grokbot"]["tasks"]], [(BOT, "done")])
        self.assertEqual(set(ids(g[None])), {LEGACY, CYCLE_A, CYCLE_B})            # a parent cycle never hangs
        self.assertEqual((g[None]["status"], g["grokbot"]["status"]), ("n/a", "n/a"))

    def test_attention_counts_failed_and_unknown(self):
        g = groups(view())
        self.assertEqual(g[OTHER]["attention"], 1)                                  # THEIRS failed
        self.assertEqual(g[MINE]["attention"], 1)                                   # AWAY: state on another host
        away = next(t for t in g[MINE]["tasks"] if t["task_id"] == AWAY)
        self.assertEqual((away["state"], away["status"], away["host"]), ("elsewhere", "unknown", "studio"))

    def test_attention_counts_a_failed_sub_session_once(self):
        def failed(key, ref, at):
            return {"key": key, "ref": ref, "provider": "codex", "opened_at": at - 50, "closed_at": at, "closed": "failed"}
        more = [failed("r:" + ROOT, "c-000000000020", NOW - 20),           # ROOT runs on; one subagent failed
                failed("r:" + NESTED2, "c-000000000021", NOW - 101),       # a succeeded nested task, one failed sub
                failed("r:" + THEIRS, "c-000000000022", NOW - 3002)]       # THEIRS failed: a second failed sub
        g = groups(view(ended_conversations=lambda: ended() + more))
        self.assertEqual(g[MINE]["attention"], 3)                   # AWAY + ROOT's failed sub + NESTED2's failed sub
        self.assertEqual(g[OTHER]["attention"], 1)                  # THEIRS once, not once per failed sub-session
        root = g[MINE]["tasks"][0]
        self.assertEqual((root["status"], root["sub_sessions_failed"]), ("running", 1))
        self.assertIn(("c-000000000020", "failed"), [(s["ref"], s["status"]) for s in root["sub_sessions"]])
        theirs = next(t for t in g[OTHER]["tasks"] if t["task_id"] == THEIRS)
        self.assertEqual(theirs["sub_sessions_failed"], 2)
        self.assertEqual(g[MINE]["counts"], {"running": 2, "waiting": 0, "done": 1, "failed": 0, "unknown": 1})
        # the same in the session's own view, and a failed sub-session past the list cap still counts
        busy = [{"key": "r:" + ROOT, "ref": f"c-0000000003{i:02x}", "opened_at": NOW - 10, "busy": True}
                for i in range(live.SUBSESSIONS_MAX + 2)]
        mine = view(MINE, conversations=lambda: backend() + busy,
                    ended_conversations=lambda: ended() + more)["orchestrators"]["items"][0]
        self.assertEqual(mine["attention"], 3)
        self.assertNotIn("failed", [s["status"] for s in mine["tasks"][0]["sub_sessions"]])
        self.assertEqual(mine["tasks"][0]["sub_sessions_failed"], 1)

    def test_caps_are_said(self):
        many = [{"ts": NOW - 10 - i, "event": "started", "task_id": f"t20261111100000-{i:08x}",
                 "origin_session": MINE} for i in range(live.ORCH_TASKS_MAX + 5)]
        mine = groups(view(child_events=lambda: events() + many))[MINE]
        self.assertEqual((len(mine["tasks"]), mine["tasks_hidden"]), (live.ORCH_TASKS_MAX, 7))
        self.assertEqual(mine["counts"]["running"], live.ORCH_TASKS_MAX + 7)        # counts are over every task

    def test_a_failed_source_marks_the_tree(self):
        def boom():
            raise OSError("/Users/someone/x")
        o = view(ended_conversations=boom)["orchestrators"]
        self.assertEqual(o["status"], "partial")
        self.assertIn({"source": "ended conversations", "error": "OSError"}, o["errors"])
        self.assertNotIn("/Users/", json.dumps(o))
        o = view(child_events=boom)["orchestrators"]
        self.assertNotEqual(o["status"], "ok")                                      # never an empty success

    def test_metadata_only(self):
        raw = json.dumps(view())
        for bad in (SECRET, "/Users/", "secret-project", "claude-app"):
            self.assertNotIn(bad, raw, bad)
        for g in view()["orchestrators"]["items"]:
            self.assertFalse({"prompt", "task_preview", "cwd"} & set(json.dumps(g)), g["origin"])


class SessionFilterTest(unittest.TestCase):
    """The pane's view holds its own session's subtree and nothing that names or describes another session."""

    FOREIGN = (OTHER, THEIRS, FOLLOW, BOT, LEGACY, CYCLE_A, CYCLE_B, "grokbot", "Grounds", "Their secret plan",
               "Follow-up by Grounds", "Their subagent title", "other-sub-0001", "c-00000000000d", "c-00000000000e",
               "c-000000000011", "Bot errand", "Legacy task", "session-aaaa", "turn-0")

    def test_session_view_never_holds_another_origin(self):
        v = session_fixture()
        raw = json.dumps(v)
        for bad in self.FOREIGN + (SECRET, "/Users/"):
            self.assertNotIn(bad, raw, bad)
        self.assertEqual(set(v), {"version", "generated_at", "session_filter", "overview", "orchestrators", "s1",
                                  "hosts", "work_split"})
        self.assertEqual(set(v["overview"]), {"s1", "s1_latency_ms", "s1_guard", "lowest_subscription",
                                              "hosts_with_heartbeat"})
        self.assertEqual([g["origin"] for g in v["orchestrators"]["items"]], [MINE])
        self.assertEqual(v["orchestrators"]["session_filter"], MINE)
        self.assertEqual(ids(v["orchestrators"]["items"][0]), [ROOT, NESTED, NESTED2, AWAY])

    def test_hosts_carry_no_session(self):
        # the session view keeps the hosts section: it must hold no orchestrator block (other sessions' tags)
        raw = json.dumps(session_fixture()["hosts"])
        self.assertNotIn(tl.SESSION, raw)
        self.assertNotIn("orchestrator", raw)

    def test_every_origin_sees_only_itself(self):
        for origin in (MINE, OTHER, "grokbot"):
            items = view(origin)["orchestrators"]["items"]
            self.assertEqual([g["origin"] for g in items], [origin])
            others = set(ids(groups(view())[origin])) ^ set().union(*(ids(g) for g in view()["orchestrators"]["items"]))
            raw = json.dumps(live.session_view(view(origin), origin))
            for tid in others:
                self.assertNotIn(tid, raw, (origin, tid))

    def test_an_unknown_tag_sees_nothing(self):
        v = live.session_view(view("cccccccccccccccc"), "cccccccccccccccc")
        self.assertEqual(v["orchestrators"]["items"], [])
        for bad in (MINE, OTHER, ROOT, "grokbot"):
            self.assertNotIn(bad, json.dumps(v))

    def test_filtered_before_the_caps(self):
        many = [{"ts": NOW - 10 - i, "event": "started", "task_id": f"t20261111110000-{i:08x}",
                 "origin_session": "dddddddddddddddd"} for i in range(live.ORCH_MAX * live.ORCH_TASKS_MAX)]
        many += [{"ts": NOW - 20 - i, "event": "started", "task_id": f"t20261111120000-{i:08x}",
                  "origin_session": f"{i:016x}"} for i in range(live.ORCH_MAX + 4)]
        items = view(MINE, child_events=lambda: many + events())["orchestrators"]["items"]
        self.assertEqual(ids(items[0]), [ROOT, NESTED, NESTED2, AWAY])

    # ---- review 2026-11-14 (gpt-6.1-sol): the tree is built from uncapped inputs, filtered, then capped ----
    def busy_board(self, n):
        """board() plus `n` newer, working conversations of OTHER's: more than the conversations section lists."""
        b = board()
        other = {"session": OTHER, "project": "Grounds", "device": "studio"}
        extra = []
        for i in range(n):
            rid = f"other-busy-{i:05d}"
            b.turn_start("r:" + rid, other, run_id=rid, provider="codex", lane="codex", model="gpt-6.1", now=NOW - 2)
            extra.append({"key": "r:" + rid, "ref": f"c-1{i:011x}", "provider": "codex", "lane": "codex",
                          "model": "gpt-6.1", "opened_at": NOW - 2, "last_used": NOW - 1, "busy": True,
                          "awaiting_tool": False})
        return b, extra

    def test_foreign_conversations_do_not_hide_this_sessions_routed_subagent(self):
        n = live.CONVERSATIONS_MAX + 6
        b, extra = self.busy_board(n)
        over = dict(office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                    conversations=lambda: extra + tagged(backend()))
        fleet = view(**over)
        c = fleet["conversations"]                                  # the fleet list: capped as before
        self.assertEqual((len(c["items"]), c["hidden"]), (live.CONVERSATIONS_MAX, c["active"] - live.CONVERSATIONS_MAX))
        self.assertGreater(c["hidden"], 0)
        self.assertNotIn("mine-sub-00001", [it["run_id"] for it in fleet["conversations"]["items"]])
        for v in (fleet, view(MINE, **over), live.session_view(view(MINE, **over), MINE)):
            g = groups(v)[MINE]
            self.assertEqual([r["ref"] for r in g["routed"]], ["mine-sub-00001"])
            self.assertEqual(g["routed_hidden"], 0)
            self.assertEqual(v["orchestrators"]["inputs_truncated"], [])
        # OTHER's own count is complete too: every one of its n + 1 routed agents, not the board's 32 per floor
        theirs = groups(fleet)[OTHER]
        self.assertEqual(len(theirs["routed"]) + theirs["routed_hidden"], n + 1)
        self.assertEqual(len(theirs["routed"]), live.ORCH_ROUTED_MAX)

    def own_agents(self, n, *, open_conversations, tags_on_conversations=True):
        """board() plus `n` of MINE's routed agents on its one floor: open conversations (with or without their own
        tags, as the registry may know them) or one-shot workers the board alone knows."""
        b = board()
        mine = {"session": MINE, "project": "CortadOS-Router", "device": "pro"}
        convs = []
        for i in range(n):
            rid = f"mine-agent-{i:05d}"
            b.turn_start("r:" + rid, mine, run_id=rid, provider="codex", lane="codex", model="gpt-6.1",
                         now=NOW - 100 + i)
            if open_conversations:
                c = {"key": "r:" + rid, "ref": f"c-2{i:011x}", "provider": "codex", "lane": "codex",
                     "model": "gpt-6.1", "opened_at": NOW - 100 + i, "last_used": NOW - 1, "busy": True,
                     "awaiting_tool": False}
                convs.append({**c, "session": MINE} if tags_on_conversations else c)
        return dict(office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                    conversations=lambda: convs + tagged(backend()))

    def test_forty_own_routed_agents_on_one_floor_are_all_reported(self):
        n = tl.office.MAX_WORKERS_PER_FLOOR + 8                    # 40: more than the Office lists on one floor
        for kw in (dict(open_conversations=True), dict(open_conversations=True, tags_on_conversations=False),
                   dict(open_conversations=False)):
            over = self.own_agents(n, **kw)
            # the Office board itself still lists 32 on the floor: the cap the tree no longer joins through
            floor = next(f for f in tl.office.OfficeBoard.view(board(), [], now=NOW)["floors"] if f["session"] == MINE)
            self.assertLessEqual(len(floor["workers"]), tl.office.MAX_WORKERS_PER_FLOOR)
            for v in (view(**over), view(MINE, **over), live.session_view(view(MINE, **over), MINE)):
                g, o = groups(v)[MINE], v["orchestrators"]
                self.assertEqual(len(g["routed"]) + g["routed_hidden"], n + 1, kw)     # + mine-sub-00001
                self.assertEqual((o["inputs_truncated"], o["truncated"]), ([], False), kw)

    def test_a_worker_lost_to_the_boards_memory_is_said_for_its_session_only(self):
        b = tl.office.OfficeBoard()
        b.turn_start("r:mine-old-00001", {"session": MINE}, run_id="mine-old-00001", provider="codex", lane="codex",
                     model="gpt-6.1", now=NOW - 60)
        with mock.patch.object(tl.office, "MAX_WORKERS", 4):
            for i in range(4):                                      # OTHER's newer one-shots push MINE's out
                rid = f"other-new-{i:05d}"
                b.turn_start("r:" + rid, {"session": OTHER}, run_id=rid, provider="codex", lane="codex",
                             model="gpt-6.1", now=NOW - 10)
        over = dict(office_view=lambda ev: b.view(ev, now=NOW, uncapped=True))
        for v in (view(MINE, **over), live.session_view(view(MINE, **over), MINE)):
            self.assertEqual(v["orchestrators"]["inputs_truncated"], ["routed workers"])
            self.assertTrue(v["orchestrators"]["truncated"])
        for tag in ("cccccccccccccccc", OTHER):                     # not a flag for anyone else
            self.assertEqual(view(tag, **over)["orchestrators"]["inputs_truncated"], [], tag)
        self.assertEqual(view(**over)["orchestrators"]["inputs_truncated"], ["routed workers"])   # the fleet: said

    def test_the_fleet_view_is_unchanged_by_the_uncapped_join(self):
        # under the caps, the board's uncapped list changes nothing in the owner's view
        self.assertEqual(view(), view(office_view=lambda ev: board().view(ev, now=NOW)))
        # and a conversation's own tags only fill an owner the board did not know (NESTED's loop: no worker of its own)
        old, new = view(conversations=backend), view()
        for v in (old, new):
            for it in v["conversations"]["items"]:
                if it["run_id"] == NESTED:
                    it["session"] = MINE
        self.assertEqual(old, new)

    def test_foreign_remote_stubs_do_not_hide_this_sessions_remote_task(self):
        n = live.ELSEWHERE_MAX + 10
        stubs = [{"task_id": f"t20261111095000-f{i:07x}", "host": "studio", "created_at": NOW - 1 - i / 100,
                  "origin_session": OTHER} for i in range(n)]
        stubs.append({"task_id": AWAY, "host": "studio", "created_at": NOW - 30, "origin_session": MINE})
        fleet = view(remote_stubs=lambda: stubs)
        self.assertEqual(len(fleet["tasks"]["elsewhere"]), live.ELSEWHERE_MAX)      # the fleet list: capped as before
        self.assertNotIn(AWAY, [x["task_id"] for x in fleet["tasks"]["elsewhere"]])
        for v in (fleet, view(MINE, remote_stubs=lambda: stubs)):
            self.assertIn(AWAY, ids(groups(v)[MINE]))
        theirs = groups(fleet)[OTHER]
        self.assertEqual(theirs["counts"]["unknown"], n)                                # every one counted
        self.assertEqual(ids(view(MINE, remote_stubs=lambda: stubs)["orchestrators"]["items"][0]),
                         [ROOT, NESTED, NESTED2, AWAY])

    def test_the_hard_ceiling_is_said_and_applies_after_the_filter(self):
        stubs = [{"task_id": f"t20261111095000-f{i:07x}", "host": "studio", "created_at": NOW - 1 - i / 100,
                  "origin_session": OTHER} for i in range(12)]
        stubs.append({"task_id": AWAY, "host": "studio", "created_at": NOW - 30, "origin_session": MINE})
        with mock.patch.object(live, "TREE_INPUT_MAX", 5):
            mine = view(MINE, remote_stubs=lambda: stubs)["orchestrators"]
            self.assertIn(AWAY, ids(mine["items"][0]))                  # filtered first: OTHER's stubs never count
            self.assertEqual((mine["inputs_truncated"], mine["truncated"]), ([], False))
            o = view(remote_stubs=lambda: stubs)["orchestrators"]       # the fleet: 13 stubs, 5 kept, the cut said
            self.assertEqual((o["inputs_truncated"], o["truncated"]), (["conversations", "task stubs"], True))
        cut = child_tasks.StubList(stubs)
        cut.truncated = True                                            # the fleet's task-dir scan stopped at its cap
        self.assertEqual(view(remote_stubs=lambda: cut)["orchestrators"]["inputs_truncated"], ["task stubs"])
        for tag in (MINE, OTHER, "cccccccccccccccc"):                   # ... which is no session's own cut
            o = view(tag, remote_stubs=lambda: cut)["orchestrators"]
            self.assertEqual((o["inputs_truncated"], o["truncated"]), ([], False), tag)
        own = child_tasks.StubList([stubs[-1]])
        own.truncated = True                                            # THIS origin's index has more than it read
        o = view(MINE, remote_stubs=lambda: cut, session_stubs=lambda tag: own)["orchestrators"]
        self.assertEqual((o["inputs_truncated"], o["truncated"]), (["task stubs"], True))

    def test_the_session_tree_never_takes_the_fleet_scan(self):
        def fleet_scan():
            raise AssertionError("the fleet stubs were read")
        asked = []
        v = view(MINE, remote_stubs=None, session_stubs=lambda tag: asked.append(tag) or child_tasks.StubList(
            [{"task_id": AWAY, "host": "studio", "created_at": NOW - 30, "origin_session": MINE}]))
        self.assertEqual(asked, [MINE])
        self.assertIn(AWAY, ids(groups(v)[MINE]))
        self.assertEqual(view(MINE, remote_stubs=fleet_scan, session_stubs=lambda tag: [])["orchestrators"]["errors"],
                         [])                                            # the fleet scan's error is not the tree's

    def test_an_unknown_tag_gets_no_foreign_errors(self):
        seen = []

        def peek(tid):
            seen.append(tid)
            raise PermissionError("/Users/someone/tasks/" + tid)
        for tag in ("cccccccccccccccc", MINE):
            seen.clear()
            o = view(tag, peek=peek, peek_foreign=False)["orchestrators"]
            mine_open = {ROOT, NESTED} if tag == MINE else set()
            self.assertEqual(set(seen), mine_open)                       # no other session's dir is opened at all
            self.assertEqual(o["errors"], [{"source": "task_dir", "error": "PermissionError"}] * len(mine_open))
            self.assertEqual(o["status"], "partial" if mine_open else "ok")
        # the ?session= fleet variant still peeks every open task, but its tree keeps only its own tasks' errors
        seen.clear()
        v = view("cccccccccccccccc", peek=peek)
        self.assertEqual(set(seen), {ROOT, NESTED, FOLLOW})
        self.assertEqual((v["orchestrators"]["errors"], v["orchestrators"]["status"]), ([], "ok"))
        self.assertEqual(len(v["tasks"]["errors"]), 3)                  # the tasks section is fleet-wide, as before
        self.assertNotIn("/Users/", json.dumps(v))

    def test_this_sessions_tasks_are_peeked_first(self):
        many = [{"ts": NOW - 5 - i / 100, "event": "started", "task_id": f"t20261111130000-{i:08x}",
                 "origin_session": OTHER} for i in range(live.PEEK_MAX + 5)]
        seen = []
        view(MINE, child_events=lambda: many + events(), peek=lambda tid: seen.append(tid) or {"phase": "queued"})
        self.assertEqual(len(seen), live.PEEK_MAX)
        self.assertEqual(seen[:2], [ROOT, NESTED])                      # OTHER's tasks never use up the budget

    def test_the_fleet_tree_keeps_every_task_dir_error(self):
        def peek(tid):
            raise PermissionError("/x")
        o = view(peek=peek)["orchestrators"]
        self.assertEqual(o["errors"], [{"source": "task_dir", "error": "PermissionError"}] * 3)
        self.assertEqual(o["status"], "partial")

    def test_the_session_fixture_is_current(self):
        text = FIXTURE.read_text()
        self.assertEqual(json.loads(text.split("export default ", 1)[1]), session_fixture())


SESSION_TOKEN = "live-session-token-for-tests-01"


class SessionEndpointTest(tl.EndpointHarness):
    """GET /v1/cortados/live/session over the real endpoint: required tag, scopes, and a routed run's conversation
    appearing under its task."""

    def setUp(self):
        super().setUp()
        self.home, self.tasks = tempfile.mkdtemp(), tempfile.mkdtemp()
        for d in (self.home, self.tasks):
            self.addCleanup(shutil.rmtree, d, True)
        env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_TASKS_DIR": self.tasks,
                                           "CORTADOS_EXEC_HOSTS_FILE": os.path.join(self.home, "absent.json")})
        env.start()
        self.addCleanup(env.stop)
        # the registry was built before the home was patched: its bindings file goes in this test's own home
        self.reg.live = live.HeartbeatBoard(path=Path(self.home) / live.BINDINGS_FILE)
        cf = Path(self.scratch) / "clients.json"
        cf.write_text(json.dumps({"version": 1, "clients": [
            {"name": "pane", "token_sha256": clients.token_digest(SESSION_TOKEN), "scopes": ["live:session"]},
            {"name": "phone", "token_sha256": clients.token_digest(tl.LIVE_TOKEN), "scopes": ["live:read"]}]}))
        self.reg.clients = clients.ClientTokens(path=cf, owner_token=tl.TOKEN)
        self.httpd.live_usage_view = lambda reg, now: {"generated_at": now, "lanes": [], "hardware": [],
                                                       "s1": {"available": False, "reason": "no S1 URL configured"},
                                                       "errors": []}

    request = tl.EndpointTest.request

    def test_paths_agree(self):
        self.assertEqual(clients.LIVE_SESSION_PATH, live.SESSION_PATH)

    def test_newer_foreign_stubs_neither_hide_an_own_stub_nor_flag_another_tag(self):
        """Review 2026-11-14 (P2): 300 newer remote stubs of another session in the real tasks dir, past the fleet
        scan's REMOTE_STUBS_SCAN, and one older stub of MINE's, all written the way start_task writes them."""
        mine = "t20261111080000-00000001"
        child_tasks._write_stub(self.tasks, mine, "studio", {"origin_session": MINE})
        for i in range(300):
            child_tasks._write_stub(self.tasks, f"t20261111090000-{i:08x}", "studio", {"origin_session": OTHER})
        self.assertGreater(300, child_tasks.REMOTE_STUBS_SCAN)
        listed = []
        real = os.listdir

        def spy(path="."):
            listed.append(os.path.normpath(str(path)))
            return real(path)
        with mock.patch.object(os, "listdir", spy):
            code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)
        self.assertEqual(code, 200, raw)
        self.assertNotIn(os.path.normpath(self.tasks), listed)            # the task dirs were never listed
        o = json.loads(raw)["orchestrators"]
        self.assertEqual([t["task_id"] for t in o["items"][0]["tasks"]], [mine])
        self.assertEqual((o["inputs_truncated"], o["truncated"]), ([], False))
        self.assertNotIn(OTHER, raw)
        self.httpd._live_memo = live.Memo()
        code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=cccccccccccccccc", SESSION_TOKEN)
        o = json.loads(raw)["orchestrators"]
        self.assertEqual((o["items"], o["inputs_truncated"], o["truncated"]), ([], [], False))   # no foreign volume
        # the owner's fleet view: unchanged, its scan's cut said there
        fleet = json.loads(self.request("GET", live.LIVE_PATH, tl.TOKEN)[1])
        self.assertEqual(fleet["orchestrators"]["inputs_truncated"], ["task stubs"])
        self.assertNotIn(mine, [x["task_id"] for x in fleet["tasks"]["elsewhere"]])

    def test_tag_required_and_scopes(self):
        P = live.SESSION_PATH
        for q in ("", "?session=", "?session=nothex", f"?session={MINE}&session={MINE}"):
            code, raw, _ = self.request("GET", P + q, tl.TOKEN)
            self.assertEqual(code, 400, (q, raw))
            self.assertNotIn("nothex", raw)
        self.assertEqual(self.request("GET", P + "?session=" + MINE)[0], 401)
        for tok in (tl.TOKEN, tl.LIVE_TOKEN, SESSION_TOKEN):
            code, raw, headers = self.request("GET", P + "?session=" + MINE, tok)
            self.assertEqual(code, 200, raw)
            self.assertEqual(headers["cache-control"], "private, no-store")
            self.assertEqual(json.loads(raw)["session_filter"], MINE)
        # the narrow token opens nothing else: not the fleet view, not the routes
        for p in (live.LIVE_PATH, live.LIVE_PATH + "?session=" + MINE, "/v1/cortados/office", "/resources",
                  "/v1/cortados/usage"):
            self.assertEqual(self.request("GET", p, SESSION_TOKEN)[0], 403, p)
        self.assertEqual(self.request("POST", "/v1/messages", SESSION_TOKEN, body={
            "model": "cortados", "messages": [{"role": "user", "content": "x"}]})[0], 403)
        self.assertEqual(self.reg.sessions, {})

    def test_a_routed_run_is_a_sub_session_of_its_task(self):
        import time
        tid = "t20261111160000-0a0b0c0d"
        Path(self.home, "child-tasks.jsonl").write_text(json.dumps({
            "ts": time.time() - 30, "event": "started", "task_id": tid, "title": "Routed child", "host": "air",
            "origin_session": MINE, "prompt": SECRET}) + "\n" + json.dumps({
            "ts": time.time() - 20, "event": "started", "task_id": "t20261111160000-0f0f0f0f",
            "origin_session": OTHER, "title": "Not yours"}) + "\n")
        from tests.test_provider import READ_TOOL
        tags = {"x-cortados-session": MINE, "x-cortados-run-id": tid}
        code, raw, _ = self.request("POST", "/v1/messages", tl.TOKEN, headers=tags, body={
            "model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool " + SECRET}]})
        self.assertEqual(code, 200, raw)
        code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)
        self.assertEqual(code, 200, raw)
        for bad in (SECRET, OTHER, "Not yours", "t20261111160000-0f0f0f0f", tl.TOKEN):
            self.assertNotIn(bad, raw, bad)
        task = json.loads(raw)["orchestrators"]["items"][0]["tasks"][0]
        self.assertEqual((task["task_id"], task["title"], task["status"]), (tid, "Routed child", "running"))
        self.assertEqual([(s["role"], s["status"], s["phase"]) for s in task["sub_sessions"]],
                         [("main", "running", "running a tool")])
        # the registry knows its owner itself (the request's own tags), not through the board's capped floors
        self.assertEqual([(c["key"], c["session"]) for c in self.reg.live_conversations()], [("r:" + tid, MINE)])
        # closing it moves it to the ended sub-sessions, still under its task
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        self.httpd._live_memo = live.Memo()
        raw = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)[1]
        task = json.loads(raw)["orchestrators"]["items"][0]["tasks"][0]
        self.assertEqual([(s["role"], s["status"]) for s in task["sub_sessions"]], [("main", "done")])


    def test_the_session_endpoint_never_opens_another_sessions_task_dir(self):
        import time
        mine, theirs = "t20261111170000-0a0b0c0d", "t20261111170000-0f0f0f0f"
        Path(self.home, "child-tasks.jsonl").write_text("".join(json.dumps(e) + "\n" for e in (
            {"ts": time.time() - 30, "event": "started", "task_id": mine, "origin_session": MINE},
            {"ts": time.time() - 20, "event": "started", "task_id": theirs, "origin_session": OTHER})))
        seen = []

        def peek(tid):
            seen.append(tid)
            if tid == theirs:
                raise PermissionError("/private/tasks/" + tid)
            return None
        with mock.patch.object(child_tasks, "peek_local", peek):
            code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)
            self.assertEqual(code, 200, raw)
            self.assertEqual(seen, [mine])
            o = json.loads(raw)["orchestrators"]
            self.assertEqual((o["status"], o["errors"]), ("ok", []))
            seen.clear()
            code, raw, _ = self.request("GET", live.LIVE_PATH, tl.TOKEN)     # the fleet view peeks both, as before
            self.assertEqual(code, 200, raw)
            self.assertEqual(sorted(seen), sorted([mine, theirs]))
            self.assertIn({"source": "task_dir", "error": "PermissionError"}, json.loads(raw)["orchestrators"]["errors"])


    def test_a_posted_pro_heartbeat_puts_its_task_in_its_sessions_view_only(self):
        """#118 over the real endpoint: the Pro posts its host "here" tasks in its heartbeat; the Air's session view
        for MINE holds MINE's, OTHER's view holds OTHER's, and the owner's fleet tree holds both."""
        import time
        now = time.time()
        body = {"host": "pro", "ttl_s": 180, "jobs": [], "tasks": {"items": [
            {"task_id": PRO_RUN, "host": "pro", "state": "running", "title": "Pro side job", "started_at": now - 60,
             "origin_session": MINE, "origin_project": "CortadOS-Router", "origin_device": "pro"},
            {"task_id": PRO_THEIRS, "host": "pro", "state": "succeeded", "title": "Their pro job",
             "started_at": now - 900, "ended_at": now - 300, "origin_session": OTHER}]}}
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, tl.TOKEN, body=body)
        self.assertEqual(code, 200, raw)
        code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)
        self.assertEqual(code, 200, raw)
        o = json.loads(raw)["orchestrators"]
        self.assertEqual([(t["task_id"], t["host"], t["status"]) for t in o["items"][0]["tasks"]],
                         [(PRO_RUN, "pro", "running")])
        self.assertEqual((o["scope"], o["task_hosts"]), (live.TREE_SCOPE, ["pro"]))
        for bad in (PRO_THEIRS, "Their pro job", OTHER):
            self.assertNotIn(bad, raw, bad)
        self.httpd._live_memo = live.Memo()
        raw = self.request("GET", live.SESSION_PATH + "?session=" + OTHER, SESSION_TOKEN)[1]
        self.assertEqual([t["task_id"] for t in json.loads(raw)["orchestrators"]["items"][0]["tasks"]], [PRO_THEIRS])
        for bad in (PRO_RUN, "Pro side job", MINE):
            self.assertNotIn(bad, raw, bad)
        fleet = groups(json.loads(self.request("GET", live.LIVE_PATH, tl.TOKEN)[1]))
        self.assertEqual((ids(fleet[MINE]), ids(fleet[OTHER])), ([PRO_RUN], [PRO_THEIRS]))
        # a heartbeat cannot speak for another host's task
        bad = {**body, "host": "studio"}
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, tl.TOKEN, body=bad)[0], 400)

    def test_a_heartbeat_task_joins_its_run_only_through_the_origin_the_provider_saw(self):
        """Round 4 over the real endpoint: the Pro reports PRO_RUN under MINE; its run's routed request reaches the
        provider. With x-cortados-origin: MINE (as the child runner sends it) the conversation and its tokens join the
        task; a run that carried no origin, or another, joins nothing, whatever the heartbeat says."""
        import time
        from tests.test_provider import READ_TOOL
        now = time.time()
        runs = {PRO_RUN: MINE, PRO_THEIRS: None, PRO_NESTED: OTHER}
        body = {"host": "pro", "ttl_s": 180, "jobs": [], "tasks": {"items": [
            {"task_id": tid, "host": "pro", "state": "running", "title": "hb " + tid[-4:], "started_at": now - 60,
             "origin_session": MINE, "session": "cccccccccccccc0b"} for tid in runs]}}
        code, raw, _ = self.request("POST", live.HEARTBEAT_PATH, tl.TOKEN, body=body)
        self.assertEqual(code, 200, raw)
        for tid, origin in runs.items():
            h = {"x-cortados-run-id": tid, "x-cortados-session": "cccccccccccccc0b"}
            if origin:
                h["x-cortados-origin"] = origin
            code, raw, _ = self.request("POST", "/v1/messages", tl.TOKEN, headers=h, body={
                "model": "cortados", "tools": [READ_TOOL], "messages": [{"role": "user", "content": "use tool x"}]})
            self.assertEqual(code, 200, raw)
        self.assertEqual({k: v for k, v in self.reg.runs.run_origins().items() if k in runs},
                         {PRO_RUN: MINE, PRO_NESTED: OTHER})
        code, raw, _ = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)
        self.assertEqual(code, 200, raw)
        tasks = {t["task_id"]: t for t in json.loads(raw)["orchestrators"]["items"][0]["tasks"]}
        self.assertEqual(set(tasks), set(runs))
        self.assertEqual([s["role"] for s in tasks[PRO_RUN]["sub_sessions"]], ["main"])
        for tid in (PRO_THEIRS, PRO_NESTED):
            self.assertEqual((tasks[tid]["sub_sessions"], tasks[tid]["tokens"], tasks[tid]["provider"]), ([], None, None))
        for bad in ("x-cortados-origin", "_origin", "bind_origin", "cccccccccccccc0b"):
            self.assertNotIn(bad, raw, bad)
        # the conversation list itself never publishes the observed origin either (owner's fleet view)
        fleet = self.request("GET", live.LIVE_PATH, tl.TOKEN)[1]
        self.assertEqual([c.get("origin") for c in json.loads(fleet)["conversations"]["items"] if "origin" in c], [])


PRO_RUN = "t20261111094000-f0000001"        # MINE's, run with host "here" on the Pro: only in the Pro's heartbeat
PRO_THEIRS = "t20261111094100-f0000002"     # OTHER's, also on the Pro
PRO_NESTED = "t20261111094200-f0000003"     # on the Pro, parent ROOT (on the Air), no origin of its own


def pro_items():
    return [
        {"task_id": PRO_RUN, "state": "running", "title": "Pro side job", "started_at": NOW - 400,
         "model": "claude-haiku-5", "lane": "claude#a", "origin_session": MINE, "origin_project": "CortadOS-Router",
         "origin_device": "pro"},
        {"task_id": PRO_THEIRS, "state": "succeeded", "title": "Their pro job", "started_at": NOW - 2000,
         "ended_at": NOW - 1000, "origin_session": OTHER, "origin_project": "Grounds"},
        {"task_id": PRO_NESTED, "state": "running", "title": "Pro nested", "started_at": NOW - 100,
         "parent_task_id": ROOT},
        # the provider host's own log has ROOT: a heartbeat's copy of it never replaces or doubles it
        {"task_id": ROOT, "state": "succeeded", "title": "stale copy", "started_at": NOW - 300, "ended_at": NOW - 1}]


def beats_with(host="pro", items=None, at=NOW - 20, ttl=180):
    """tl.heartbeats() (the Studio's and the Air's) plus one host's heartbeat carrying a tasks block."""
    b = tl.heartbeats()
    hb, bad = live.validate_heartbeat({"host": host, "ttl_s": ttl, "jobs": [],
                                       "tasks": {"items": pro_items() if items is None else items}}, NOW)
    assert bad is None, bad
    b.post(hb, at)
    return lambda: b.snapshot(NOW)


class HostTasksTest(unittest.TestCase):
    """#118: tasks that ran on another execution host (host "here" on the Pro), known only from that host's own
    child-task log, reach the tree through its heartbeat's tasks block, attributed by their origin tag."""

    def test_a_pro_task_shows_in_its_sessions_view_and_never_in_anothers(self):
        v = live.session_view(view(MINE, heartbeats=beats_with()), MINE)
        mine = v["orchestrators"]["items"][0]
        self.assertIn(PRO_RUN, ids(mine))
        pro = next(t for t in mine["tasks"] if t["task_id"] == PRO_RUN)
        self.assertEqual((pro["host"], pro["status"], pro["title"], pro["model"], pro["lane"]),
                         ("pro", "running", "Pro side job", "claude-haiku-5", "claude#a"))
        self.assertEqual(pro["elapsed_s"], 400.0)
        raw = json.dumps(v)
        for bad in (PRO_THEIRS, "Their pro job", "stale copy") + SessionFilterTest.FOREIGN:
            self.assertNotIn(bad, raw, bad)
        theirs = json.dumps(live.session_view(view(OTHER, heartbeats=beats_with()), OTHER))
        self.assertIn(PRO_THEIRS, theirs)
        for bad in (PRO_RUN, "Pro side job", PRO_NESTED, "Pro nested", MINE):
            self.assertNotIn(bad, theirs, bad)
        nobody = json.dumps(live.session_view(view("cccccccccccccccc", heartbeats=beats_with()), "cccccccccccccccc"))
        for bad in (PRO_RUN, PRO_THEIRS, PRO_NESTED):
            self.assertNotIn(bad, nobody, bad)

    def test_the_fleet_tree_has_every_hosts_tasks(self):
        v = view(heartbeats=beats_with())
        g = groups(v)
        self.assertIn(PRO_RUN, ids(g[MINE]))
        self.assertIn(PRO_THEIRS, ids(g[OTHER]))
        self.assertEqual((v["orchestrators"]["scope"], v["orchestrators"]["task_hosts"]), (live.TREE_SCOPE, ["pro"]))
        self.assertEqual(v["tasks"]["scope"], live.SCOPE)            # the tasks section is still the provider host's
        self.assertNotIn(PRO_RUN, json.dumps(v["tasks"]))
        for tid in (PRO_RUN, PRO_THEIRS, PRO_NESTED):              # the hosts section carries no task entry
            self.assertNotIn(tid, json.dumps(v["hosts"]), tid)

    def test_a_task_nested_across_hosts_inherits_its_parents_orchestrator(self):
        root = groups(view(heartbeats=beats_with()))[MINE]["tasks"][0]
        self.assertEqual(root["task_id"], ROOT)
        nested = next(t for t in root["tasks"] if t["task_id"] == PRO_NESTED)
        self.assertEqual((nested["host"], nested["origin_via"]), ("pro", "parent"))

    def test_the_provider_hosts_own_log_wins_and_nothing_doubles(self):
        v = view(heartbeats=beats_with())
        everything = [t for g in v["orchestrators"]["items"] for t in ids(g)]
        self.assertEqual(everything.count(ROOT), 1)
        self.assertNotIn("stale copy", json.dumps(v))
        self.assertEqual(groups(v)[MINE]["tasks"][0]["status"], "running")

    def test_a_stale_heartbeat_never_shows_a_task_as_live(self):
        g = groups(view(heartbeats=beats_with(at=NOW - 1000, ttl=180)))[MINE]
        pro = next(t for t in g["tasks"] if t["task_id"] == PRO_RUN)
        self.assertEqual((pro["state"], pro["status"], pro["phase"]), ("unknown", "unknown", live.STALE_HB_PHASE))
        self.assertEqual(g["attention"], 3)                        # AWAY, PRO_RUN, and PRO_NESTED under ROOT

    def test_a_pro_tasks_routed_conversations_are_its_sub_sessions(self):
        # the provider saw the run's requests carry x-cortados-origin: MINE (the child runner sends it)
        conv = {"key": "r:" + PRO_RUN, "ref": "c-000000000030", "provider": "claude", "lane": "claude#a",
                "model": "claude-haiku-5", "opened_at": NOW - 390, "last_used": NOW - 2, "busy": True,
                "session": MINE, "origin": MINE}
        mine = view(MINE, heartbeats=beats_with(), conversations=lambda: tagged(backend()) + [conv])
        pro = next(t for t in mine["orchestrators"]["items"][0]["tasks"] if t["task_id"] == PRO_RUN)
        self.assertEqual([(s["ref"], s["role"], s["status"]) for s in pro["sub_sessions"]],
                         [("c-000000000030", "main", "running")])

    def test_a_hosts_report_replaces_a_remote_stub(self):
        # MINE started AWAY onto the Studio from this machine (a stub, state unknown here); the Studio's heartbeat
        # now reports it running
        items = [{"task_id": AWAY, "state": "running", "title": "On the studio", "started_at": NOW - 30,
                  "origin_session": MINE}]
        mine = groups(view(heartbeats=beats_with("studio", items)))[MINE]
        away = [t for t in mine["tasks"] if t["task_id"] == AWAY]
        self.assertEqual([(t["state"], t["host"], t["title"]) for t in away], [("running", "studio", "On the studio")])
        self.assertEqual(mine["attention"], 0)

    def test_a_task_started_from_the_pro_onto_the_air_needs_no_stub_on_the_air(self):
        """Round 2's remote-stub path for a start_task run on the Pro with host air: the stub and the origin index are
        written on the Pro, so the Air's session endpoint finds none. The task shows anyway: the Air's own log has it
        with the origin the Pro sent (test_child_tasks: test_remote_task_records_its_origin_on_the_host)."""
        tid = "t20261111095000-a000000a"
        log = events() + [{"ts": NOW - 50, "event": "started", "task_id": tid, "host": "air", "title": "Sent from pro",
                           "origin_session": MINE, "origin_device": "pro"}]
        v = live.session_view(view(MINE, child_events=lambda: log, remote_stubs=None,
                                   session_stubs=lambda tag: child_tasks.StubList()), MINE)
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == tid)
        self.assertEqual((t["status"], t["host"], t["title"]), ("running", "air", "Sent from pro"))

    def test_the_pane_knows_the_stale_heartbeat_phase(self):
        # the pane lets this machine's own log correct a task the view knows only from a stale heartbeat
        pane = (FIXTURE.parent / "register.ts").read_text()
        self.assertIn(f"export const STALE_HB_PHASE = {json.dumps(live.STALE_HB_PHASE)}", pane)

    def test_a_hosts_cut_list_is_said_in_the_fleet_view_only(self):
        b = tl.heartbeats()
        hb, bad = live.validate_heartbeat({"host": "pro", "jobs": [], "tasks": {"items": pro_items(), "truncated": True}},
                                          NOW)
        b.post(hb, NOW - 20)
        o = view(heartbeats=lambda: b.snapshot(NOW))["orchestrators"]
        self.assertEqual((o["inputs_truncated"], o["truncated"]), (["host tasks"], True))
        o = view(MINE, heartbeats=lambda: b.snapshot(NOW))["orchestrators"]
        self.assertEqual((o["inputs_truncated"], o["truncated"]), ([], False))
        self.assertIn(PRO_RUN, ids(o["items"][0]))

    def test_a_failed_heartbeat_read_marks_the_tree(self):
        def boom():
            raise RuntimeError("x")
        o = view(heartbeats=boom)["orchestrators"]
        self.assertEqual(o["status"], "partial")
        self.assertIn({"source": "heartbeats", "error": "RuntimeError"}, o["errors"])


VICTIM = "t20261111094500-f0000009"         # OTHER's task on the Pro, routed through this provider
VICTIM_CHILD = "cccccccccccccc0b"           # its child's own Office tag (one random tag per cortados-route server)
VICTIM_SECRETS = ("Victim private title", "VictimSecretTool", "c-0000000000bb", "claude-opus-9")


def victim_board(origin=OTHER):
    """board() plus VICTIM's routed run: a worker with its child's own tag, OTHER's private title and current tool, and
    the origin the provider saw on its requests (x-cortados-origin, sent by the child runner): OTHER."""
    b = board()
    tags = {"session": VICTIM_CHILD, "title": "Victim private title"}
    b.turn_start("r:" + VICTIM, tags, run_id=VICTIM, provider="claude", lane="claude#a", model="claude-opus-9",
                 now=NOW - 100, origin=origin)
    b.turn_end("r:" + VICTIM, tags, status="working", tool="VictimSecretTool", now=NOW - 5)
    return b


VICTIM_CONV = {"key": "r:" + VICTIM, "ref": "c-0000000000bb", "provider": "claude", "lane": "claude#a",
               "model": "claude-opus-9", "opened_at": NOW - 90, "last_used": NOW - 2, "busy": True,
               "session": VICTIM_CHILD, "title": "Victim private title", "origin": OTHER,
               "tokens": {"input": 5, "output": 5, "cache_read": 0, "cache_write": 0}}


def claim(board_, host, origin, at, **extra):
    """One heartbeat from `host` reporting VICTIM under `origin`; the board's answer."""
    hb, bad = live.validate_heartbeat({"host": host, "jobs": [], "tasks": {"items": [
        {"task_id": VICTIM, "state": "running", "started_at": NOW - 100, "origin_session": origin, **extra}]}}, NOW)
    assert bad is None, bad
    return board_.post(hb, at)


def victim_view(session, beats, origin=OTHER, **over):
    """The view with VICTIM's run as the provider recorded it: every record of it observed with `origin` (None: the
    run's requests carried no x-cortados-origin, a child runner from before round 4)."""
    b = victim_board(origin)
    args = dict(heartbeats=beats, office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                conversations=lambda: tagged(backend()) + [{**VICTIM_CONV, "origin": origin}], run_tokens=lambda: {
                    VICTIM: {"input": 777, "output": 1, "cache_read": 0, "cache_write": 0}},
                run_origins=lambda: {VICTIM: origin} if origin else {})
    args.update(over)
    v = view(session, **args)
    return live.session_view(v, session) if session else v


class HostBindingTest(unittest.TestCase):
    """Review round 3, P1: a task id's host and origin are bound once. A heartbeat from another host (or the same host
    with another origin) cannot take over a task id, and (round 4) the run's Office worker, sub-sessions and tokens
    join a heartbeat's task only when the provider itself saw the run's requests carry the task's bound origin."""

    def test_the_reviewers_repro_a_newer_heartbeat_cannot_take_another_hosts_task(self):
        hbs = live.HeartbeatBoard()
        self.assertNotIn("task_conflicts", claim(hbs, "pro", OTHER, NOW - 30, session=VICTIM_CHILD))
        self.assertEqual(claim(hbs, "studio", MINE, NOW - 10)["task_conflicts"], 1)
        beats = lambda: hbs.snapshot(NOW)
        raw = json.dumps(victim_view(MINE, beats))
        for bad in (VICTIM,) + VICTIM_SECRETS:
            self.assertNotIn(bad, raw, bad)
        # the owner keeps the task under OTHER, on the Pro, with its run joined (the worker carries the child's tag)
        theirs = victim_view(OTHER, beats)
        t = next(t for t in theirs["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["host"], t["title"], t["tool"], [s["ref"] for s in t["sub_sessions"]]),
                         ("pro", "Victim private title", "VictimSecretTool", ["c-0000000000bb"]))
        self.assertEqual(t["tokens"]["input"], 777)
        fleet = victim_view(None, beats)["orchestrators"]
        self.assertEqual(fleet["conflicts"], 1)
        self.assertEqual([g["origin"] for g in fleet["items"] if VICTIM in ids(g)], [OTHER])
        self.assertNotIn("conflicts", victim_view(MINE, beats)["orchestrators"])   # never in a session's view

    def test_the_same_host_cannot_change_a_tasks_origin_or_parent(self):
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 30, session=VICTIM_CHILD)
        self.assertEqual(claim(hbs, "pro", MINE, NOW - 20, session=VICTIM_CHILD)["task_conflicts"], 1)
        self.assertEqual(claim(hbs, "pro", OTHER, NOW - 15, session=VICTIM_CHILD,
                               parent_task_id=ROOT)["task_conflicts"], 1)
        raw = json.dumps(victim_view(MINE, lambda: hbs.snapshot(NOW)))
        for bad in (VICTIM,) + VICTIM_SECRETS:
            self.assertNotIn(bad, raw, bad)
        # the conflicting post dropped the entry: OTHER's task is gone from the board until the Pro reports it again
        self.assertNotIn("task_conflicts", claim(hbs, "pro", OTHER, NOW - 10, session=VICTIM_CHILD))
        self.assertIn(VICTIM, json.dumps(victim_view(OTHER, lambda: hbs.snapshot(NOW))))

    def test_a_first_claim_by_the_wrong_host_never_joins_the_real_run(self):
        """The forger reports first: it owns the id (first host wins), but the real run's worker, conversations and
        tokens carry the child's own tag, not the forger's: nothing of OTHER's run is published under MINE."""
        hbs = live.HeartbeatBoard()
        claim(hbs, "studio", MINE, NOW - 30)
        self.assertEqual(claim(hbs, "pro", OTHER, NOW - 10, session=VICTIM_CHILD)["task_conflicts"], 1)
        mine = victim_view(MINE, lambda: hbs.snapshot(NOW))
        t = next(t for t in mine["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["title"], t["tool"], t["provider"], t["model"], t["sub_sessions"], t["tokens"]),
                         (None, None, None, None, [], None))
        raw = json.dumps(mine)
        for bad in VICTIM_SECRETS + ("777",):
            self.assertNotIn(bad, raw, bad)
        # a forger that names a guessed tag gains nothing either
        hbs = live.HeartbeatBoard()
        claim(hbs, "studio", MINE, NOW - 30, session="dddddddddddddddd")
        for bad in VICTIM_SECRETS:
            self.assertNotIn(bad, json.dumps(victim_view(MINE, lambda: hbs.snapshot(NOW))), bad)

    def test_two_current_beats_after_a_restart_the_earlier_keeps_the_id(self):
        """A restart forgets the bindings; two boards' snapshots stand in for beats that were never compared."""
        pro, studio = live.HeartbeatBoard(), live.HeartbeatBoard()
        claim(pro, "pro", OTHER, NOW - 30, session=VICTIM_CHILD)
        claim(studio, "studio", MINE, NOW - 10)
        beats = lambda: {**pro.snapshot(NOW), **studio.snapshot(NOW)}
        for bad in (VICTIM,) + VICTIM_SECRETS:
            self.assertNotIn(bad, json.dumps(victim_view(MINE, beats)), bad)
        self.assertEqual(victim_view(None, beats)["orchestrators"]["conflicts"], 1)

    def test_the_provider_hosts_own_records_bind_their_ids(self):
        # ROOT is in this host's log (host air, origin MINE): the Studio cannot re-home it under OTHER
        hbs = live.HeartbeatBoard()
        hb, _ = live.validate_heartbeat({"host": "studio", "jobs": [], "tasks": {"items": [
            {"task_id": ROOT, "state": "running", "title": "hijack", "started_at": NOW - 50,
             "origin_session": OTHER}]}}, NOW)
        hbs.post(hb, NOW - 5)
        v = view(heartbeats=lambda: hbs.snapshot(NOW))
        self.assertEqual(v["orchestrators"]["conflicts"], 1)
        self.assertNotIn(ROOT, ids(groups(v)[OTHER]))
        self.assertNotIn("hijack", json.dumps(v))
        # AWAY is a stub this host wrote (onto the studio, MINE's): the Pro cannot claim it, nor the studio re-home it
        for host, origin in (("pro", MINE), ("studio", OTHER)):
            hbs = live.HeartbeatBoard()
            hb, _ = live.validate_heartbeat({"host": host, "jobs": [], "tasks": {"items": [
                {"task_id": AWAY, "state": "running", "title": "hijack", "started_at": NOW - 20,
                 "origin_session": origin}]}}, NOW)
            hbs.post(hb, NOW - 5)
            v = view(heartbeats=lambda: hbs.snapshot(NOW))
            self.assertEqual(v["orchestrators"]["conflicts"], 1, host)
            self.assertNotIn("hijack", json.dumps(v), host)
            mine = [t for t in groups(v)[MINE]["tasks"] if t["task_id"] == AWAY]
            self.assertEqual([(t["state"], t["host"]) for t in mine], [("elsewhere", "studio")], host)

    def test_a_conversation_of_the_run_with_another_observed_origin_is_not_a_sub_session(self):
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 10, session=VICTIM_CHILD)
        stray = {**VICTIM_CONV, "ref": "c-0000000000cc", "origin": MINE}
        bare = {**VICTIM_CONV, "ref": "c-0000000000dd", "origin": None}
        # a conversation with no x-cortados-origin is no observation: not joined, and it vetoes nothing
        v = victim_view(OTHER, lambda: hbs.snapshot(NOW),
                        conversations=lambda: tagged(backend()) + [VICTIM_CONV, bare])
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual([s["ref"] for s in t["sub_sessions"]], ["c-0000000000bb"])
        for bad in ('"_origin"', '"_session"', '"bind_origin"', VICTIM_CHILD):
            self.assertNotIn(bad, json.dumps(v), bad)            # compared, never published
        # one conversation of the run seen with another origin: the run was seen with two, and joins nothing at all
        # (round 5: the veto is run-wide, not per conversation)
        v = victim_view(OTHER, lambda: hbs.snapshot(NOW),
                        conversations=lambda: tagged(backend()) + [VICTIM_CONV, stray, bare])
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["sub_sessions"], t["tokens"], t["model"], t["tool"]), ([], None, None, None))

    def test_the_child_tag_is_no_part_of_the_claim(self):
        # round 4: a heartbeat's child tag is the reporting host's word: neither a key nor part of the binding
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 40)
        self.assertNotIn("task_conflicts", claim(hbs, "pro", OTHER, NOW - 30, session=VICTIM_CHILD))
        self.assertNotIn("task_conflicts", claim(hbs, "pro", OTHER, NOW - 20, session="dddddddddddddddd"))
        self.assertNotIn("session", hbs.snapshot(NOW)["pro"]["tasks"]["items"][0])   # checked, then dropped
        t = next(t for t in victim_view(OTHER, lambda: hbs.snapshot(NOW))["orchestrators"]["items"][0]["tasks"]
                 if t["task_id"] == VICTIM)
        self.assertEqual(t["tool"], "VictimSecretTool")


def post_ids(board_, host, ids_, at, origin=None):
    hb, bad = live.validate_heartbeat({"host": host, "jobs": [], "tasks": {"items": [
        {"task_id": t, "state": "running", "started_at": NOW - 100, **({"origin_session": origin} if origin else {})}
        for t in ids_]}}, NOW)
    assert bad is None, bad
    return board_.post(hb, at)


class ProviderObservedJoinTest(unittest.TestCase):
    """Review round 4: a join key is something the provider observed itself (the run's x-cortados-origin on its own
    requests), never something a heartbeat supplied; ownership is checked against every first-hand record before any
    session filtering; the bindings survive a restart and a full table refuses rather than evicts."""

    def test_the_reviewers_p1_repro_a_heartbeat_supplied_child_tag_joins_nothing(self):
        # claim(h, "studio", MINE, NOW-30, session=VICTIM_CHILD): the forger knows VICTIM's id AND its child tag
        hbs = live.HeartbeatBoard()
        claim(hbs, "studio", MINE, NOW - 30, session=VICTIM_CHILD)
        mine = victim_view(MINE, lambda: hbs.snapshot(NOW))
        t = next(t for t in mine["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["title"], t["tool"], t["provider"], t["model"], t["sub_sessions"], t["tokens"]),
                         (None, None, None, None, [], None))
        raw = json.dumps(mine)
        for bad in VICTIM_SECRETS + ("777",):
            self.assertNotIn(bad, raw, bad)

    def test_a_provider_observed_origin_joins_the_run(self):
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 30)                     # no child tag at all: the provider's record suffices
        t = next(t for t in victim_view(OTHER, lambda: hbs.snapshot(NOW))["orchestrators"]["items"][0]["tasks"]
                 if t["task_id"] == VICTIM)
        self.assertEqual((t["title"], t["tool"], t["provider"], t["model"], [s["ref"] for s in t["sub_sessions"]],
                          t["tokens"]["input"]),
                         ("Victim private title", "VictimSecretTool", "claude", "claude-opus-9", ["c-0000000000bb"], 777))

    def test_a_heartbeat_only_task_never_gets_joined_data(self):
        """The run's requests carried no x-cortados-origin (a runner from before round 4): the heartbeat's own fields
        only, however exactly the entry names the run's child tag and the right origin."""
        hbs = live.HeartbeatBoard()
        hb, _ = live.validate_heartbeat({"host": "pro", "jobs": [], "tasks": {"items": [
            {"task_id": VICTIM, "state": "running", "started_at": NOW - 100, "origin_session": OTHER,
             "session": VICTIM_CHILD, "title": "From the heartbeat", "model": "claude-haiku-5"}]}}, NOW)
        hbs.post(hb, NOW - 20)
        v = victim_view(OTHER, lambda: hbs.snapshot(NOW), origin=None)
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["title"], t["model"], t["tool"], t["provider"], t["sub_sessions"], t["tokens"]),
                         ("From the heartbeat", "claude-haiku-5", None, None, [], None))
        for bad in VICTIM_SECRETS:
            self.assertNotIn(bad, json.dumps(v), bad)
        # a task with no origin of its own (inherited through a parent the heartbeat names) joins nothing either
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", None, NOW - 20, parent_task_id=ROOT)
        t = next(t for t in ids_nodes(groups(victim_view(None, lambda: hbs.snapshot(NOW)))[MINE])
                 if t["task_id"] == VICTIM)
        self.assertEqual((t["title"], t["tool"], t["sub_sessions"], t["tokens"]), (None, None, [], None))

    def test_a_run_seen_with_two_origins_joins_nothing(self):
        self.assertEqual(live.office.note_origin(None, OTHER), OTHER)
        self.assertEqual(live.office.note_origin(OTHER, None), OTHER)
        self.assertEqual(live.office.note_origin(OTHER, MINE), live.office.ORIGIN_CONFLICT)
        self.assertEqual(live.office.note_origin(live.office.ORIGIN_CONFLICT, OTHER), live.office.ORIGIN_CONFLICT)
        b = victim_board()
        b.turn_start("r:" + VICTIM, {}, run_id=VICTIM, provider="claude", lane=None, model=None, now=NOW - 4,
                     origin=MINE)
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 20)
        for who in (OTHER, MINE):
            v = victim_view(who, lambda: hbs.snapshot(NOW), office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                            conversations=lambda: tagged(backend()) + [{**VICTIM_CONV, "origin": ""}],
                            run_origins=lambda: {VICTIM: ""})
            for bad in VICTIM_SECRETS + ("777",):
                self.assertNotIn(bad, json.dumps(v), (who, bad))

    def test_the_round5_repro_two_differently_tagged_conversations_join_nothing(self):
        """Review round 5, P2: one run, two conversations, one observed with OTHER and one with MINE; the worker's and
        the run tokens' origin are ORIGIN_CONFLICT; the task is reported under OTHER. Round 4 still gave OTHER's task
        the OTHER-tagged conversation's ref, model, phase and tokens."""
        b = victim_board()
        b.turn_start("r:" + VICTIM, {}, run_id=VICTIM, provider="claude", lane=None, model=None, now=NOW - 4,
                     origin=MINE)                       # the worker: OTHER, then MINE -> ORIGIN_CONFLICT
        self.assertEqual([w["origin"] for w in b.view([], now=NOW, uncapped=True)["workers_all"]
                          if w.get("run_id") == VICTIM], [live.office.ORIGIN_CONFLICT])
        theirs = {**VICTIM_CONV, "origin": OTHER,
                  "tokens": {"input": 4242, "output": 4243, "cache_read": 0, "cache_write": 0}}
        mine = {**VICTIM_CONV, "ref": "c-0000000000cc", "model": "gpt-6.1-secret", "origin": MINE,
                "tokens": {"input": 5151, "output": 5152, "cache_read": 0, "cache_write": 0}}
        hbs = live.HeartbeatBoard()
        claim(hbs, "pro", OTHER, NOW - 20)
        for who in (OTHER, MINE, None):
            v = victim_view(who, lambda: hbs.snapshot(NOW), office_view=lambda ev: b.view(ev, now=NOW, uncapped=True),
                            conversations=lambda: tagged(backend()) + [theirs, mine],
                            run_origins=lambda: {VICTIM: live.office.ORIGIN_CONFLICT})
            raw = json.dumps(v["orchestrators"])
            for bad in VICTIM_SECRETS + ("777", "c-0000000000cc", "gpt-6.1-secret", "4242", "5151"):
                self.assertNotIn(bad, raw, (who, bad))
            if who != MINE:
                t = next(t for g in v["orchestrators"]["items"] for t in g["tasks"] if t["task_id"] == VICTIM)
                self.assertEqual((t["sub_sessions"], t["tokens"], t["model"], t["tool"], t["provider"], t["title"]),
                                 ([], None, None, None, None, None))
        # the same with the worker and the run tokens agreeing on OTHER: the MINE conversation alone vetoes the run
        v = victim_view(OTHER, lambda: hbs.snapshot(NOW), conversations=lambda: tagged(backend()) + [theirs, mine])
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["sub_sessions"], t["tokens"], t["model"], t["tool"]), ([], None, None, None))
        # and an unreadable source of origin observations vetoes every heartbeat join
        def boom():
            raise RuntimeError("x")
        v = victim_view(OTHER, lambda: hbs.snapshot(NOW), ended_conversations=boom)
        t = next(t for t in v["orchestrators"]["items"][0]["tasks"] if t["task_id"] == VICTIM)
        self.assertEqual((t["sub_sessions"], t["tokens"], t["model"], t["tool"]), ([], None, None, None))
        # control: one origin everywhere still joins
        t = next(t for t in victim_view(OTHER, lambda: hbs.snapshot(NOW))["orchestrators"]["items"][0]["tasks"]
                 if t["task_id"] == VICTIM)
        self.assertEqual([s["ref"] for s in t["sub_sessions"]], ["c-0000000000bb"])

    def test_the_reviewers_p2_repro_another_sessions_stub_rejects_a_forgery_in_every_view(self):
        """AWAY is MINE's start onto the Studio (a stub on this machine). The Pro forges it under OTHER. The session
        endpoint reads no fleet scan and only OTHER's own index, so round 3 accepted it in OTHER's view."""
        stubs = [{"task_id": AWAY, "host": "studio", "created_at": NOW - 30, "origin_session": MINE,
                  "origin_project": "CortadOS-Router"}]
        hbs = live.HeartbeatBoard()
        hb, _ = live.validate_heartbeat({"host": "pro", "jobs": [], "tasks": {"items": [
            {"task_id": AWAY, "state": "running", "title": "forged", "started_at": NOW - 20,
             "origin_session": OTHER}]}}, NOW)
        hbs.post(hb, NOW - 5)
        lookups = []

        def by_id(ids_):
            lookups.append(list(ids_))
            return [x for x in stubs if x["task_id"] in ids_]
        for session in (OTHER, None):
            v = view(session, heartbeats=lambda: hbs.snapshot(NOW), remote_stubs=None if session else lambda: stubs,
                     session_stubs=own_stubs(lambda: stubs), stubs_by_id=by_id)
            self.assertNotIn("forged", json.dumps(v), session)
            if session:
                self.assertNotIn(AWAY, json.dumps(v), session)
            else:                                   # the fleet: MINE's own stub, never under OTHER
                self.assertNotIn(AWAY, ids(groups(v)[OTHER]))
                self.assertEqual([t["state"] for t in groups(v)[MINE]["tasks"] if t["task_id"] == AWAY], ["elsewhere"])
        self.assertEqual(lookups, [[AWAY]])         # the fleet view read the stub in its scan: no extra read
        self.assertEqual(view(heartbeats=lambda: hbs.snapshot(NOW), remote_stubs=lambda: stubs,
                              stubs_by_id=by_id)["orchestrators"]["conflicts"], 1)
        # the same over real stub files: child_tasks.remote_stubs_by_id reads exactly the claimed ids' stubs
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        child_tasks._write_stub(d, AWAY, "studio", {"origin_session": MINE})
        with mock.patch.object(child_tasks, "tasks_dir", lambda: d):
            self.assertEqual([(x["task_id"], x["host"], x["origin_session"])
                              for x in child_tasks.remote_stubs_by_id([AWAY, PRO_RUN, "../x"])],
                             [(AWAY, "studio", MINE)])
            v = view(OTHER, heartbeats=lambda: hbs.snapshot(NOW), remote_stubs=None,
                     session_stubs=lambda tag: child_tasks.StubList(), stubs_by_id=child_tasks.remote_stubs_by_id)
            self.assertNotIn(AWAY, json.dumps(v))
            # an unreadable stub: no report of that id is trusted, and the cause is not this session's error
            Path(d, AWAY, child_tasks.REMOTE_STUB).write_text("{not json")
            self.assertEqual(child_tasks.remote_stubs_by_id([AWAY]), [{"task_id": AWAY, "unreadable": True}])
            v = view(OTHER, heartbeats=lambda: hbs.snapshot(NOW), remote_stubs=None,
                     session_stubs=lambda tag: child_tasks.StubList(), stubs_by_id=child_tasks.remote_stubs_by_id)
            self.assertNotIn(AWAY, json.dumps(v))

    def test_a_failed_stub_lookup_trusts_no_claim_and_says_so(self):
        def boom(ids_):
            raise PermissionError("/private/tasks")
        o = view(OTHER, heartbeats=beats_with(), remote_stubs=None, stubs_by_id=boom)["orchestrators"]
        self.assertNotIn(PRO_THEIRS, json.dumps(o))
        self.assertIn({"source": "task stubs", "error": "PermissionError"}, o["errors"])
        self.assertEqual(o["status"], "partial")

    def test_the_reviewers_restart_repro(self):
        """VICTIM bound to Pro/OTHER, the provider restarts (a fresh board on the same file), the Studio posts first
        claiming it under MINE: refused."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = Path(d, live.BINDINGS_FILE)
        claim(live.HeartbeatBoard(path=path), "pro", OTHER, NOW - 60)
        with mock.patch.object(live.time, "time", return_value=NOW):
            fresh = live.HeartbeatBoard(path=path)               # the restart: loads the file
        self.assertEqual(claim(fresh, "studio", MINE, NOW - 10, session=VICTIM_CHILD)["task_conflicts"], 1)
        raw = json.dumps(victim_view(MINE, lambda: fresh.snapshot(NOW)))
        for bad in (VICTIM,) + VICTIM_SECRETS:
            self.assertNotIn(bad, raw, bad)
        self.assertNotIn("task_conflicts", claim(fresh, "pro", OTHER, NOW - 5))   # the owner keeps reporting it

    def test_bindings_persist_across_a_fresh_board(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = Path(d, "state", live.BINDINGS_FILE)
        old, live_ = "t20261111094500-f0000101", "t20261111094500-f0000102"
        b = live.HeartbeatBoard(path=path)
        post_ids(b, "pro", [old], NOW - live.HEARTBEAT_KEEP_S + 100, origin=OTHER)
        post_ids(b, "pro", [live_], NOW - 50, origin=MINE)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(sorted(os.listdir(path.parent)), [live.BINDINGS_FILE])   # atomic: no temp file left
        doc = json.loads(path.read_text())
        self.assertEqual(doc["version"], live.BINDINGS_VERSION)
        self.assertEqual({r[0]: r[1:4] for r in doc["bindings"]}, {old: ["pro", OTHER, None], live_: ["pro", MINE, None]})
        self.assertNotIn("title", path.read_text())              # ids, hosts, origins, parents and times only
        # a day later the first one has expired: a fresh board loads only the live one
        with mock.patch.object(live.time, "time", return_value=NOW + 200):
            fresh = live.HeartbeatBoard(path=path)
        self.assertEqual(set(fresh._bindings), {live_})
        self.assertEqual(fresh._bindings[live_]["bind"], ("pro", MINE, None))
        self.assertEqual(fresh.errors(), [])
        # an unreadable file: an empty table, said in the tree (never a failed start)
        path.write_text("{oops")
        broken = live.HeartbeatBoard(path=path)
        self.assertEqual((broken._bindings, broken.errors()), ({}, [{"source": "task bindings", "error": "JSONDecodeError"}]))
        o = view(binding_errors=broken.errors)["orchestrators"]
        self.assertIn({"source": "task bindings", "error": "JSONDecodeError"}, o["errors"])
        # entries that fail the rules are skipped, not trusted
        path.write_text(json.dumps({"version": 1, "bindings": [
            ["../etc", "pro", None, None, NOW], [live_, "Bad Host", None, None, NOW], [live_, "pro", "NOTHEX", None, NOW],
            [old, "pro", OTHER, None, NOW - 10]]}))
        with mock.patch.object(live.time, "time", return_value=NOW):
            self.assertEqual(set(live.HeartbeatBoard(path=path)._bindings), {old})
        # a save that fails keeps the table in memory and says so
        ro = live.HeartbeatBoard(path=Path(d, "file-not-dir", live.BINDINGS_FILE))
        Path(d, "file-not-dir").write_text("x")
        post_ids(ro, "pro", [live_], NOW - 5, origin=MINE)
        self.assertIn(live_, ro._bindings)
        self.assertEqual([e["source"] for e in ro.errors()], ["task bindings"])

    def test_a_corrupt_bindings_file_never_stops_the_start(self):
        """Review round 5, P2: `seen` = 10**400 made _num's math.isfinite raise OverflowError outside the loader's
        handler, and the Registry builds the board at provider start. Every bad entry is skipped and said; a garbage,
        oversized or malformed file is an empty table and said; nothing is raised."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = Path(d, live.BINDINGS_FILE)
        good, bad_id = "t20261111094500-f0000201", "t20261111094500-f0000202"
        ok_row = json.dumps([good, "pro", OTHER, None, NOW - 10])

        def load(text):
            path.write_bytes(text if isinstance(text, bytes) else text.encode())
            with mock.patch.object(live.time, "time", return_value=NOW):
                b = live.HeartbeatBoard(path=path)
            return set(b._bindings), [e["error"] for e in b.errors()]
        self.assertFalse(live._num(10 ** 400))
        self.assertEqual(live.validate_heartbeat({"host": "pro", "jobs": [], "ttl_s": 10 ** 400}, NOW), (None, "ttl_s"))
        # the reviewer's repro, written as the JSON a corrupt file would hold, beside a good entry
        for seen in ("1" + "0" * 400, "-1" + "0" * 400, "NaN", "Infinity", "-Infinity", "1e400", '"1800000000"',
                     "true", "null", "[1]", "{}"):
            row = f'["{bad_id}","pro",null,null,{seen}]'
            self.assertEqual(load(f'{{"version":1,"bindings":[{row},{ok_row}]}}'), ({good}, ["InvalidEntry"]), seen)
        # wrong types and shapes, entry by entry: skipped and said, the good one kept
        for row in ([bad_id, 5, None, None, NOW], [5, "pro", None, None, NOW], [bad_id, "pro", 5, None, NOW],
                    [bad_id, "pro", None, ["x"], NOW], [bad_id, "pro", None, None], {"task_id": bad_id}, "x", None,
                    [[bad_id], "pro", None, None, NOW]):
            self.assertEqual(load(json.dumps({"version": 1, "bindings": [row, json.loads(ok_row)]})),
                             ({good}, ["InvalidEntry"]), row)
        # an expired entry is no error
        self.assertEqual(load(json.dumps({"version": 1, "bindings": [
            [bad_id, "pro", None, None, NOW - live.HEARTBEAT_KEEP_S - 5], json.loads(ok_row)]})), ({good}, []))
        # whole files that cannot be used: an empty table, the type said, never raised
        for text, err in ((b"\xff\xfe\x00garbage", "UnicodeDecodeError"), ("garbage", "JSONDecodeError"),
                          ("[" * 100000 + "]" * 100000, "RecursionError"), ("[]", "ValueError"),
                          ('{"version":"1","bindings":[]}', "ValueError"), ('{"version":1,"bindings":{}}', "ValueError"),
                          ('{"version":1}', "ValueError"), ("1" + "0" * 400, "ValueError")):
            self.assertEqual(load(text), (set(), [err]), text[:20])
        # an oversized file is not read at all (a bounded read: never a parse of BINDINGS_MAX_BYTES + more)
        big = json.dumps({"version": 1, "bindings": [json.loads(ok_row)] * 20})
        with mock.patch.object(live, "BINDINGS_MAX_BYTES", len(big) - 1):
            self.assertEqual(load(big), (set(), ["BindingsTooLarge"]))
        with mock.patch.object(live, "BINDINGS_MAX_BYTES", len(big)):
            self.assertEqual(load(big), ({good}, []))
        self.assertEqual(live.BINDINGS_MAX_BYTES, 2 * 1024 * 1024)
        self.assertLess(len(json.dumps([bad_id, "abcdefghijklmnopqrstuvwxyz012345", "f" * 64, bad_id, NOW + 0.125]))
                        * live.TASK_BINDINGS_MAX, live.BINDINGS_MAX_BYTES)   # a full table fits under the cap
        # and the provider's own start, on a home holding the reviewer's file: the Registry builds; the board is empty
        from cortados_router.provider.server import Registry
        path.write_text(f'{{"version":1,"bindings":[["{bad_id}","pro",null,null,1{"0" * 400}]]}}')
        with mock.patch.dict(os.environ, {"CORTADOS_HOME": d}):
            reg = Registry(gateway_url="", token="t", claude_bin=["true"])
        self.assertEqual((reg.live._bindings, [e["error"] for e in reg.live.errors()]), ({}, ["InvalidEntry"]))

    def test_a_corrupt_or_oversized_remote_stub_is_skipped_never_raised(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        ids_ = [f"t20261111094500-f00003{i:02d}" for i in range(6)]
        child_tasks._write_stub(d, ids_[0], "studio", {"origin_session": MINE})
        for tid in ids_[1:]:
            os.mkdir(os.path.join(d, tid), 0o700)
        stub = lambda tid: Path(d, tid, child_tasks.REMOTE_STUB)
        stub(ids_[1]).write_text(json.dumps({"host": "studio", "pad": "x" * child_tasks.REMOTE_STUB_MAX_BYTES}))
        stub(ids_[2]).write_bytes(b"\xff\xfe garbage")
        stub(ids_[3]).write_text('{"host":"studio","created_at":1' + "0" * 400 + "}")
        stub(ids_[4]).write_text('{"host":"studio","created_at":NaN}')
        stub(ids_[5]).write_text('{"host":["studio"],"created_at":"x","origin":7}')
        with mock.patch.object(child_tasks, "tasks_dir", lambda: d):
            got = {x["task_id"]: x for x in child_tasks.remote_stubs_by_id(ids_)}
            self.assertEqual((got[ids_[1]], got[ids_[2]]), ({"task_id": ids_[1], "unreadable": True},
                                                             {"task_id": ids_[2], "unreadable": True}))
            self.assertEqual(got[ids_[0]]["host"], "studio")
            self.assertNotIn(ids_[5], got)                        # a host that is no string: no stub
            fleet = child_tasks.remote_stubs()
            self.assertEqual(sorted(x["task_id"] for x in fleet), sorted([ids_[0], ids_[3], ids_[4]]))
            # the live view re-validates them: the huge and NaN times are no times; nothing raises
            self.assertEqual([x["task_id"] for x in live._elsewhere(fleet, set(), NOW + 1e9)], [])
            with self.assertRaises(child_tasks.RecordError) as cm:
                child_tasks._stub(os.path.join(d, ids_[1]))
            self.assertEqual(cm.exception.kind, "RecordTooLarge")

    def test_a_full_table_refuses_new_ids_and_keeps_live_ones(self):
        ids_ = [f"t20261111094500-f00000{i:02d}" for i in range(5)]
        hbs = live.HeartbeatBoard()
        with mock.patch.object(live, "TASK_BINDINGS_MAX", 3):
            self.assertNotIn("task_conflicts", post_ids(hbs, "pro", ids_[:3], NOW - 50, origin=OTHER))
            out = post_ids(hbs, "studio", ids_[3:], NOW - 40, origin=MINE)
            self.assertEqual(out["task_conflicts"], 2)            # refused: no live binding was evicted
            self.assertEqual(set(hbs._bindings), set(ids_[:3]))
            self.assertEqual(hbs.snapshot(NOW)["studio"]["tasks"]["items"], [])
            # the bound ids keep being reported by their owner
            self.assertNotIn("task_conflicts", post_ids(hbs, "pro", ids_[:3], NOW - 30, origin=OTHER))
            # nobody can take one over through the full table either
            self.assertEqual(post_ids(hbs, "studio", ids_[:1], NOW - 20, origin=MINE)["task_conflicts"], 1)
            self.assertEqual(view(heartbeats=lambda: hbs.snapshot(NOW))["orchestrators"]["conflicts"], 1)  # latest beats
            # once expired (not reported for a day) they make room
            out = post_ids(hbs, "studio", ids_[3:], NOW - 30 + live.HEARTBEAT_KEEP_S + 1, origin=MINE)
            self.assertNotIn("task_conflicts", out)
            self.assertEqual(set(hbs._bindings), set(ids_[3:]))


def ids_nodes(node):
    """Every task node in a node's subtree."""
    out = []
    for t in node.get("tasks") or []:
        out.append(t)
        out.extend(ids_nodes(t))
    return out


class LinkageTest(unittest.TestCase):
    def test_event_fields_carry_parent_and_inheritance(self):
        f = child_tasks._event_fields({"task_id": ROOT, "parent_task_id": NESTED, "origin_via": "parent",
                                       "origin": {"origin_session": MINE}})
        self.assertEqual((f["parent_task_id"], f["origin_via"], f["origin_session"]), (NESTED, "parent", MINE))
        f = child_tasks._event_fields({"task_id": ROOT, "parent_task_id": "../etc", "origin_via": "parent"})
        self.assertNotIn("parent_task_id", f)
        self.assertNotIn("origin_via", f)                    # no origin: nothing was inherited

    def test_the_view_rejects_a_bad_parent(self):
        ev = [{"ts": NOW - 10, "event": "started", "task_id": ROOT, "parent_task_id": "../../etc/passwd"},
              {"ts": NOW - 10, "event": "started", "task_id": NESTED, "parent_task_id": NESTED}]   # itself
        rows = []
        live.tasks_section(ev, None, None, NOW, rows_out=rows)
        self.assertEqual({r["task_id"]: r["parent_task_id"] for r in rows}, {ROOT: None, NESTED: None})


if __name__ == "__main__":
    unittest.main()


class WorkSplitSessionTest(unittest.TestCase):
    """#127: the per-session work split in the session view, over the tree's own fixtures. Routed work is attributed on
    provider-observed keys only: the child's origin header, the provider host's own task log (nested runs), the session
    tag of a routed subagent or route_task call; never a child's own (MCP-server) tag or a heartbeat field."""

    def split(self, session=MINE, **kw):
        return live.session_view(split_view(session, **kw), session)["work_split"]

    def test_native_and_routed_join_for_one_session(self):
        ws = self.split()
        self.assertEqual(ws["status"], "ok")
        row = ws["row"]
        self.assertEqual((row["session"], row["host"], row["match"], row["matched_on"]), (MINE, "pro", "both", ["session"]))
        d = row["24h"]
        self.assertEqual(d["native"]["fresh"], 700000)
        self.assertEqual([m["model"] for m in d["native"]["models"]], ["claude-opus-5-5", "claude-haiku-5"])
        self.assertEqual(d["native"]["models"][0]["cache_read"], 29000000)       # the four kinds stay apart
        r = d["routed"]
        self.assertEqual(r["fresh"], 300000 + 120000 + 80000 + 800000 + 50000 + 25000)
        self.assertEqual(set(r["by_via"]), {"child_task", "subagent", "route_task"})
        self.assertEqual(r["by_via"]["child_task"]["conversations"], 2)          # ROOT and its nested NESTED
        self.assertEqual(r["by_via"]["route_task"]["fresh"], 25000)
        self.assertEqual(r["by_via"]["subagent"]["fresh"], 50000)
        self.assertEqual(r["by_route"][0]["route"], "codex/gpt-6.1-sol")

    def test_the_session_view_finds_its_row_past_the_fleet_cap(self):
        # review round 2, finding 2: collect(session=) selects the pane's row before the fleet table's display cap
        with mock.patch.object(work_split, "SESSIONS_MAX", 0):
            self.assertEqual(split_view()["work_split"]["sessions"], [])
            row = self.split()["row"]
        self.assertEqual((row["session"], row["match"]), (MINE, "both"))

    def test_the_session_view_survives_every_cap_at_its_smallest(self):
        # review round 3, finding 1: the routed read, the published cap and the fleet table at 0; the pane's row is whole
        whole = self.split()["row"]
        caps = {"BY_SESSION_MAX": 0, "SESSIONS_MAX": 0, "BY_MODEL_MAX": 0}
        with mock.patch.multiple(work_split, **caps):
            snap = split_view()
            self.assertEqual(snap["routed"]["totals"]["tokens"]["by_window"]["24h"]["by_session"], {})
            self.assertGreater(snap["routed"]["totals"]["tokens"]["by_window"]["24h"]["by_session_hidden"], 0)
            self.assertEqual(snap["work_split"]["sessions"], [])
            self.assertEqual(self.split()["row"], whole)

    def test_fresh_and_all_token_shares(self):
        d = self.split()["row"]["24h"]
        fresh_r, fresh_n = d["routed"]["fresh"], d["native"]["fresh"]
        all_r, all_n = d["routed"]["all"], d["native"]["all"]
        self.assertEqual(d["routed_pct"], round(100 * fresh_r / (fresh_r + fresh_n), 1))
        self.assertEqual(d["routed_pct_all"], round(100 * all_r / (all_r + all_n), 1))
        self.assertGreater(d["routed_pct"], 50)                  # 66.3: most fresh work went through the router
        self.assertLess(d["routed_pct_all"], 20)                 # 14.9: the native cache reads dominate all tokens

    def test_nested_child_resolves_through_the_task_log_only(self):
        # NESTED's requests carried no origin; the provider host's own log has NESTED -> parent ROOT -> origin MINE
        r = self.split()["row"]["24h"]["routed"]
        self.assertIn("codex/gpt-6.1-sol", [x["route"] for x in r["by_route"]])
        # without NESTED in the log there is no join: its tokens are unattributed, never guessed onto MINE
        no_nested = [e for e in events() if e.get("task_id") != NESTED]
        r = self.split(child_events=lambda: no_nested)["row"]["24h"]["routed"]
        self.assertNotIn("codex/gpt-6.1-sol", [x["route"] for x in r["by_route"]])
        fleet = split_view(child_events=lambda: no_nested)["work_split"]
        self.assertEqual(fleet["overall"]["24h"]["routed_unattributed_fresh"], 800000)
        # with it, the log resolved it, and the section says how much came through the log
        self.assertEqual(split_view()["work_split"]["overall"]["24h"]["routed_from_task_log_fresh"], 800000)

    def test_a_childs_own_session_tag_is_never_the_orchestrator(self):
        fleet = split_view()["work_split"]
        tags = [s["session"] for s in fleet["sessions"]]
        self.assertNotIn(CHILD_MCP_TAG, tags)
        self.assertIn(OTHER, tags)                               # FOLLOW's run, by its own observed origin

    def test_another_session_sees_nothing_of_mine(self):
        raw = json.dumps(self.split(OTHER))
        for bad in (MINE, "CortadOS-Router", "claude-opus-5-5", "gpt-6.1-sol", SECRET, CHILD_MCP_TAG, ROOT, NESTED):
            self.assertNotIn(bad, raw, bad)
        raw = json.dumps(self.split())
        for bad in (OTHER, "Grounds", SECRET, CHILD_MCP_TAG, ROOT, NESTED, FOLLOW, "pending_runs"):
            self.assertNotIn(bad, raw, bad)

    def test_an_unmatched_tag_is_said(self):
        ws = self.split("cccccccccccccccc")
        self.assertEqual((ws["status"], ws["row"]), ("none", None))
        self.assertTrue(any("native" in n for n in ws["notes"]))
        self.assertTrue(any("no routed work" in n for n in ws["notes"]))

    def test_a_stale_heartbeat_is_said(self):
        ws = self.split(at=NOW - 3600)
        self.assertEqual(ws["status"], "partial")
        self.assertTrue(ws["row"]["native_stale"])
        self.assertTrue(any("stale" in n and "pro" in n for n in ws["notes"]))

    def test_published_routed_totals_carry_no_run_ids(self):
        raw = json.dumps(split_view()["routed"])
        for bad in ("pending_runs", ROOT, NESTED, FOLLOW):
            self.assertNotIn(bad, raw, bad)
