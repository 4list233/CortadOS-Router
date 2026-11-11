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


def events():
    """child-tasks.jsonl records as child_tasks.log_event writes them (cwd and an injected prompt field included: the
    view must drop both)."""
    base = {"cwd": "/Users/someone/secret-project", "orchestrator": "claude-app", "permission_mode": "acceptEdits",
            "chrome": False, "prompt": SECRET}
    return [
        {"ts": NOW - 30 * 3600, "event": "started", "task_id": OLD, **base, "title": "too old to show"},
        {"ts": NOW - 125, "event": "started", "task_id": RUNNING, **base, "host": "air", "lane": "claude#b",
         "model": "claude-sonnet-5", "title": "Refactor the scarcity reader", "session": SESSION,
         "project": "CortadOS-Router", "device": "pro"},
        {"ts": NOW - 600, "event": "started", "task_id": QUEUED, **base, "chrome": True, "host": "air",
         "title": "Check the release page", "session": SESSION},
        {"ts": NOW - 4000, "event": "started", "task_id": DONE, **base, "host": "air", "title": "Write the changelog"},
        {"ts": NOW - 3000, "event": "finished", "task_id": DONE, **base, "host": "air", "title": "Write the changelog",
         "status": "succeeded", "exit_code": 0, "duration_s": 1000.0, "workspace_state": "archived"},
        {"ts": NOW - 7200, "event": "started", "task_id": FAILED, **base, "title": "Bisect flaky test 10.0.0.7"},
        {"ts": NOW - 6900, "event": "finished", "task_id": FAILED, **base, "status": "failed", "exit_code": 1,
         "duration_s": 300.0, "workspace_state": "held"},
        {"ts": NOW - 5000, "event": "cancelled", "task_id": CANCELLED, **base, "client": "grokbot"},
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
            "row_limit": 200, "truncated": False, "errors": []}


def usage_live():
    from tests import test_usage as tu
    home = tu.fixture_home()
    hw = home / "hardware" / "studio.json"
    # HW_SAMPLE's load average is the test machine's own (os.getloadavg): pinned, so the fixture is the same everywhere
    hw.write_text(json.dumps({**json.loads(hw.read_text()), "load_avg": [1.5, 1.25, 1.0]}))
    os.utime(hw, (NOW - 30, NOW - 30))                                   # sampled 30 s before the fixture's clock
    return usage.collect_live(state_dir=home, scarcity_reader=lambda: tu.SCARCITY, now=NOW,
                              s1_probe=lambda url: {"available": True, "status": 200, "latency_ms": 23})


def heartbeats(at=NOW - 40):
    b = live.HeartbeatBoard()
    hb, bad = live.validate_heartbeat({"host": "studio", "ttl_s": 900, "jobs": [
        {"name": "s1-scorer", "state": "running", "summary": "serving v12"},
        {"name": "s1-continuous", "state": "running", "summary": "sweep 3/8", "progress_pct": 37.5,
         "started_at": NOW - 3600}]}, NOW)
    assert bad is None, bad
    b.post(hb, at)
    return b


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
                backend_sessions=2, peek=peek,
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

    def test_conversations_from_the_office_view(self):
        c = fixture_view()["conversations"]
        self.assertEqual((c["status"], c["backend_sessions"], c["active"], c["working"]), ("ok", 2, 3, 2))
        kinds = [(x["kind"], x["model"], x["status"]) for x in c["items"]]
        self.assertEqual(kinds, [("subagent", "gpt-6.1", "working"), ("orchestrator", "claude-opus-5", "working"),
                                 ("subagent", "qwen3-30b", "idle")])
        sub = c["items"][0]
        self.assertEqual((sub["tool"], sub["project"], sub["device"], sub["elapsed_s"], sub["title"]),
                         ("Read", "CortadOS-Router", "pro", 90.0, "Review the diff"))
        self.assertNotIn(RUNNING, json.dumps(c))              # child tasks live in the tasks section

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
        self.assertEqual(h["studio"]["hardware"]["age_s"], 30.0)   # tests/test_usage.HW_SAMPLE, host "studio"
        self.assertEqual((h["pro"]["heartbeat_state"], h["pro"]["heartbeat"]), ("none", None))
        self.assertEqual(v["overview"]["hosts_with_heartbeat"], 1)

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
        v = fixture_view(office_view=lambda ev: (_ for _ in ()).throw(RuntimeError("x")))
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

        def counted(reg):
            n.append(1)
            return real(reg)
        self.httpd.live_view = counted
        for _ in range(3):
            self.assertEqual(self.request("GET", live.LIVE_PATH, TOKEN)[0], 200)
        self.assertEqual(len(n), 1)                                  # one collect per live.MEMO_S
        self.assertEqual(self.request("POST", live.HEARTBEAT_PATH, BEAT_TOKEN, body={"host": "studio"})[0], 200)
        v = json.loads(self.request("GET", live.LIVE_PATH, LIVE_TOKEN)[1])
        self.assertEqual(len(n), 2)                                  # the post invalidated it
        self.assertEqual({x["name"]: x for x in v["hosts"]["items"]}["studio"]["heartbeat_state"], "fresh")

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


if __name__ == "__main__":
    unittest.main()
