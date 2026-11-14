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
from cortados_router.provider import clients, live
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


def view(session=None, **over):
    b = board()
    args = dict(now=NOW, child_events=events, office_view=lambda ev: b.view(ev, now=NOW),
                tasks_view=lambda: tl.tasks_view(3), usage_view=tl.usage_live,
                heartbeats=lambda: tl.heartbeats().snapshot(NOW), peek=None, conversations=backend,
                ended_conversations=ended, run_tokens=lambda: {ROOT: {"input": 9000, "output": 2000,
                                                                      "cache_read": 50000, "cache_write": 3000}},
                remote_stubs=lambda: [{"task_id": AWAY, "host": "studio", "created_at": NOW - 30,
                                       "origin_session": MINE, "origin_project": "CortadOS-Router"}],
                exec_hosts=lambda: ({"air": ["forests-macbook-air"]}, "air"), self_host="forests-macbook-air",
                host_names=["air", "studio", "pro"], session=session)
    args.update(over)
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


def session_fixture():
    """GET /v1/cortados/live/session?session=MINE as the pane receives it (hooks/session.fixture.ts)."""
    return live.session_view(view(MINE), MINE)


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
                                  "hosts"})
        self.assertEqual(set(v["overview"]), {"s1", "s1_latency_ms", "lowest_subscription", "hosts_with_heartbeat"})
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
        # closing it moves it to the ended sub-sessions, still under its task
        for s in list(self.reg.sessions.values()):
            self.reg.drop(s)
        self.httpd._live_memo = live.Memo()
        raw = self.request("GET", live.SESSION_PATH + "?session=" + MINE, SESSION_TOKEN)[1]
        task = json.loads(raw)["orchestrators"]["items"][0]["tasks"][0]
        self.assertEqual([(s["role"], s["status"]) for s in task["sub_sessions"]], [("main", "done")])


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
