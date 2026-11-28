"""The Office's rooms (#77): GET /v1/cortados/office/tree (live.office_tree) under the office:read scope, its allowlisted
metadata-only shape, the orchestrator family sources (bot origin, routed main loop's wire, heartbeat `client`), and the
adapter fixture integrations/office-claw3d/fixtures/sample-tree.json kept equal to what the provider builds.
Fakes only: a throwaway CORTADOS_HOME, no child is launched, no vendor CLI or S1 probe runs."""
import json
import os
import shutil
import tempfile
os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())
os.environ.setdefault("CORTADOS_SCARCITY_REFRESH", "off")
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router.provider import clients, live, office, server
from tests import test_live as tl
from tests import test_orchestrator_tree as ot
from tests.test_provider import TOKEN, EndpointHarness

NOW = ot.NOW
OFFICE_TOKEN = "office-token-for-tree-tests-01"
LIVE_TOKEN = "live-reader-token-tree-tests-1"
BOT_RUNNING = "t20261111095000-c0000002"     # the bot's running task: its room has someone in it
LOOSE = "t20261111095500-d0000002"           # no origin, running: the unattributed room
LOBBY_ASK = "c-000000000012"                 # E9: a one-off routed question, no orchestrator, no task
LOBBY_LOOP = "outcome-loop-0001"             # E9: the S1 outcome loop's routed call, no task
FIXTURE = Path(__file__).resolve().parents[1] / "integrations/office-claw3d/fixtures/sample-tree.json"
SERVERS_FIXTURE = FIXTURE.with_name("sample-servers.json")   # the server room (office round 2), ServerRoom.snap()


def events():
    base = {"cwd": "/Users/someone/secret-project", "prompt": ot.SECRET, "chrome": False}
    return ot.events() + [
        {"ts": NOW - 90, "event": "started", "task_id": BOT_RUNNING, **base, "origin_session": "grokbot",
         "title": "Bot lookup", "host": "air", "lane": "codex", "model": "gpt-6.1"},
        {"ts": NOW - 80, "event": "started", "task_id": LOOSE, **base, "title": "Loose task", "host": "air",
         "lane": "claude#a", "model": "claude-haiku-5"}]


def backend(wire_other="/v1/responses"):
    """ot.backend() with the wire each conversation was opened on: OTHER's routed main loop came in on /v1/responses
    (a Codex harness running a Claude model: the suit follows the harness, E7)."""
    out = ot.backend() + [
        # E9's lobby: a one-off routed question (no session tag, no run id) and the outcome loop's call (a run id that
        # is no task); and LOOSE's own conversation (untagged, but its run is a task of the tree: never the lobby)
        {"key": "c:" + LOBBY_ASK, "ref": LOBBY_ASK, "provider": "claude", "lane": "claude#a", "model": "claude-haiku-5",
         "opened_at": NOW - 20, "last_used": NOW - 2, "busy": True, "awaiting_tool": False, "title": "Quick question"},
        {"key": "r:" + LOBBY_LOOP, "ref": "c-000000000013", "provider": "codex", "lane": "codex",
         "model": "gpt-6.1-mini", "opened_at": NOW - 15, "last_used": NOW - 3, "busy": False, "awaiting_tool": False},
        {"key": "r:" + LOOSE, "ref": "c-000000000014", "provider": "claude", "lane": "claude#a",
         "model": "claude-haiku-5", "opened_at": NOW - 70, "last_used": NOW - 1, "busy": True, "awaiting_tool": False}]
    for c in out:
        c["wire"] = wire_other if c["key"] == "main:" + ot.OTHER else "/v1/messages"
    return out


def tree_view(**over):
    args = dict(child_events=events, conversations=lambda: ot.tagged(backend()))
    args.update(over)
    return ot.view(**args)


def sample_tree():
    return live.office_tree(tree_view())


def write_fixture():
    """Regenerate the adapter's fixtures after a deliberate change to the tree's or the server room's shape."""
    FIXTURE.write_text(json.dumps(sample_tree(), indent=1, ensure_ascii=False) + "\n")
    SERVERS_FIXTURE.write_text(json.dumps(live.office_servers(ServerRoom().snap()), indent=1, ensure_ascii=False) + "\n")


def keys_everywhere(x, out=None):
    out = set() if out is None else out
    if isinstance(x, dict):
        out.update(x)
        for v in x.values():
            keys_everywhere(v, out)
    elif isinstance(x, list):
        for v in x:
            keys_everywhere(v, out)
    return out


class TreeShape(unittest.TestCase):
    def test_the_adapter_fixture_is_what_the_provider_builds(self):
        self.assertEqual(json.loads(FIXTURE.read_text()), sample_tree(),
                         "regenerate with: python3 -B -c 'from tests.test_office_tree import write_fixture; "
                         "write_fixture()'")

    def test_metadata_only_allowlist(self):
        t = sample_tree()
        raw = json.dumps(t)
        self.assertEqual(set(t), {"version", "generated_at", "orchestrators", "lobby"})
        self.assertEqual(set(t["lobby"]), {"as_of", "status", "errors", "truncated", "items"})
        for r in t["lobby"]["items"]:
            self.assertEqual(set(r), set(live._TREE_LOBBY_KEYS) | {"ref", "status"})
        for banned in ("tokens", "workspace", "browser", "parent_hidden", "session_filter", "conflicts",
                       "task_hosts", "inputs_truncated", "subscriptions", "hosts", "routed_calls", "work_split",
                       "origin_machine", "_origin", "cwd", "prompt"):
            self.assertNotIn(banned, keys_everywhere(t), banned)
        for secret in (ot.SECRET, "/Users/", "secret-project", "40000", "cache_read"):
            self.assertNotIn(secret, raw)
        # every node's keys are exactly the allowlist: a field added to the live tree never leaks unreviewed
        for g in t["orchestrators"]["items"]:
            self.assertEqual(set(g), set(live._TREE_GROUP_KEYS) | {"tasks", "routed"})
            for r in g["routed"]:
                self.assertEqual(set(r), set(live._TREE_ROUTED_KEYS))
            stack = list(g["tasks"])
            while stack:
                n = stack.pop()
                self.assertEqual(set(n), set(live._TREE_TASK_KEYS) | {"sub_sessions", "tasks"})
                for s in n["sub_sessions"]:
                    self.assertEqual(set(s), set(live._TREE_SUB_KEYS))
                stack.extend(n["tasks"])

    def test_tree_groups_nesting_and_sub_sessions(self):
        g = {x["origin"]: x for x in sample_tree()["orchestrators"]["items"]}
        self.assertEqual(set(g), {ot.MINE, ot.OTHER, "grokbot", None})
        self.assertEqual((g["grokbot"]["kind"], g[None]["kind"]), ("bot", "unattributed"))
        root = next(n for n in g[ot.MINE]["tasks"] if n["task_id"] == ot.ROOT)
        self.assertEqual([s["role"] for s in root["sub_sessions"]], ["main", "subagent", "subagent"])
        nested = root["tasks"][0]
        self.assertEqual((nested["task_id"], nested["parent_task_id"]), (ot.NESTED, ot.ROOT))
        self.assertEqual(nested["tasks"][0]["task_id"], ot.NESTED2)
        self.assertIn(ot.FOLLOW, [n["task_id"] for n in g[ot.OTHER]["tasks"]])   # its own origin: OTHER's room
        self.assertEqual([r["ref"] for r in g[ot.MINE]["routed"]], ["mine-sub-00001"])
        self.assertIn(LOOSE, [n["task_id"] for n in g[None]["tasks"]])

    def test_the_lobby_holds_work_with_no_orchestrator_and_no_task(self):
        """#77 E9: untagged routed work that is no task's (a one-off question, the outcome loop) is the lobby; a task's
        own untagged conversation is its sub-session, never the lobby; a tagged session's routed helper is its room."""
        lobby = sample_tree()["lobby"]
        self.assertEqual(lobby["status"], "ok")
        self.assertEqual(sorted(r["ref"] for r in lobby["items"]), sorted([LOBBY_ASK, LOBBY_LOOP]))
        r = {x["ref"]: x for x in lobby["items"]}
        self.assertEqual((r[LOBBY_ASK]["kind"], r[LOBBY_ASK]["status"], r[LOBBY_ASK]["title"]),
                         ("conversation", "running", "Quick question"))
        self.assertEqual((r[LOBBY_LOOP]["kind"], r[LOBBY_LOOP]["status"]), ("subagent", "waiting"))
        # a session view never has a lobby: the tree endpoint is the fleet's only
        self.assertEqual(live._tree_lobby({"status": "ok", "items": [
            {"kind": "subagent", "session": ot.MINE, "run_id": "mine-sub-00001", "status": "working"}]}, [])["items"], [])
        # a failed conversations source is the lobby's error, said, never "nobody"
        failed = live.office_tree({"conversations": {"status": "error", "items": [],
                                                     "errors": [{"source": "conversations", "error": "OSError"}]}})
        self.assertEqual((failed["lobby"]["status"], failed["lobby"]["errors"]),
                         ("error", [{"source": "conversations", "error": "OSError"}]))

    def test_a_failed_source_stays_visible(self):
        t = live.office_tree({"orchestrators": {"status": "partial", "items": [],
                                                "errors": [{"source": "child_tasks", "error": "PermissionError",
                                                            "extra": ot.SECRET}]}})
        self.assertEqual(t["orchestrators"]["status"], "partial")
        self.assertEqual(t["orchestrators"]["errors"], [{"source": "child_tasks", "error": "PermissionError"}])
        self.assertEqual(live.office_tree({})["orchestrators"]["status"], "error")   # no section is not "ok"


class HostBadge(unittest.TestCase):
    """The Office's host badge (#77 brand): each orchestrator group carries `host`, the KNOWN host name its session's
    device belongs to (exec-hosts.json names and `hostnames`), never the raw device hostname; a task's `host` is its
    exec host, as before."""

    def test_the_tree_carries_each_rooms_host(self):
        g = {x["origin"]: x for x in sample_tree()["orchestrators"]["items"]}
        self.assertEqual((g[ot.MINE]["host"], g[ot.OTHER]["host"]), ("pro", "studio"))
        self.assertIsNone(g["grokbot"]["host"])                 # no device: no badge, never a guess
        self.assertIsNone(g[None]["host"])
        self.assertEqual(next(n for n in g["grokbot"]["tasks"] if n["task_id"] == BOT_RUNNING)["host"], "air")

    def test_only_a_known_name_is_published(self):
        aliases = live.host_aliases(["air", "pro", "studio"], {"air": ["forests-macbook-air"], "lab": ["box-7"]})
        items = [{"device": "forests-macbook-air"}, {"device": "forests-mac-studio"}, {"device": "box-7"},
                 {"device": "johns-imac-2"}, {"device": None}, {"device": ["pro"]}, {}]
        live.orchestrator_hosts(items, aliases)
        self.assertEqual([i["host"] for i in items], ["air", "studio", "lab", None, None, None, None])
        # the provider's own hostname matching no name is the provider host's known name, when there is one
        mine = [{"device": "owners-laptop"}]
        live.orchestrator_hosts(mine, aliases, self_host="owners-laptop", me="pro")
        self.assertEqual(mine[0]["host"], "pro")
        live.orchestrator_hosts(mine, aliases, self_host="owners-laptop", me=None)
        self.assertIsNone(mine[0]["host"])

    def test_collect_sets_host_from_exec_hosts_aliases(self):
        # "pro" is now only a hostname alias of an exec host named laptop; studio stays known through its heartbeat
        t = live.office_tree(tree_view(host_names=["laptop"],
                                       exec_hosts=lambda: ({"laptop": ["pro"], "air": ["forests-macbook-air"]}, "air")))
        hosts = {x["origin"]: x["host"] for x in t["orchestrators"]["items"]}
        self.assertEqual(hosts[ot.MINE], "laptop")
        self.assertEqual(hosts[ot.OTHER], "studio")


class FamilySources(unittest.TestCase):
    """#77 E7: first source that applies, published as client_source: bot, wire, heartbeat; else none (the adapter
    guesses from the model and says so)."""

    def groups(self, **over):
        return {x["origin"]: x for x in live.office_tree(tree_view(**over))["orchestrators"]["items"]}

    def test_each_source(self):
        g = self.groups()
        self.assertEqual((g["grokbot"]["client"], g["grokbot"]["client_source"]), ("grok", "bot"))
        self.assertEqual((g[ot.OTHER]["client"], g[ot.OTHER]["client_source"]), ("codex", "wire"))
        self.assertEqual((g[ot.MINE]["client"], g[ot.MINE]["client_source"]), (None, None))   # heartbeat, no client
        self.assertEqual((g[None]["client"], g[None]["client_source"]), (None, None))
        g = self.groups(conversations=lambda: ot.tagged(backend("/v1/messages")))
        self.assertEqual((g[ot.OTHER]["client"], g[ot.OTHER]["client_source"]), ("claude-code", "wire"))

    def test_a_heartbeat_client_and_the_wire_outranking_it(self):
        plain = ot.board

        def board():
            b = plain()
            b.heartbeat({"session": ot.MINE, "status": "working", "project": "CortadOS-Router", "device": "pro",
                         "model": "claude-opus-5", "tool": None, "client": "codex"}, now=NOW - 2)
            b.heartbeat({"session": ot.OTHER, "status": "working", "project": "Grounds", "device": "studio",
                         "model": "claude-opus-5", "tool": None, "client": "claude-code"}, now=NOW - 2)
            return b
        with mock.patch.object(ot, "board", board):
            g = self.groups()
        self.assertEqual((g[ot.MINE]["client"], g[ot.MINE]["client_source"]), ("codex", "heartbeat"))
        self.assertEqual((g[ot.OTHER]["client"], g[ot.OTHER]["client_source"]), ("codex", "wire"))

    def test_heartbeat_client_vocabulary(self):
        base = {"session": ot.MINE, "status": "working"}
        for c in office.CLIENTS:
            hb, bad = office.validate_heartbeat({**base, "client": c})
            self.assertEqual((hb["client"], bad), (c, None))
        for c in (None, ""):
            self.assertEqual(office.validate_heartbeat({**base, "client": c})[0]["client"], None)
        self.assertIsNone(office.validate_heartbeat(base)[0]["client"])
        for c in ("cursor", "Claude-Code", 7, ["codex"], ot.SECRET):
            self.assertEqual(office.validate_heartbeat({**base, "client": c}), (None, "client"))

    def test_conversation_meta_carries_the_wire_and_never_another_path(self):
        class S:
            id, route, model, opened_at, last_used = "x", {"provider": "claude-cli#b", "model": "m"}, "m", 1.0, 2.0
        for wire, want in (("/v1/messages", "/v1/messages"), ("/v1/responses", "/v1/responses"),
                           ("/v1/other", None), (None, None)):
            s = S()
            s.wire_path = wire
            self.assertEqual(server.Registry._conversation_meta(s)["wire"], want)
        s = S()
        s.route = {"provider": "local-studio", "model": "qwen3:27b"}
        with mock.patch.object(server.local_lane, "node_backend", lambda node: "ollama" if node == "studio" else None):
            self.assertEqual(server.Registry._conversation_meta(s)["local_backend"], "ollama")


class SessionName(unittest.TestCase):
    """Office round 2 (owner 2026-11-27): an orchestrator is named as Claude Code names its session. The mod sends the
    name in its heartbeat (`name`), the provider validates it (office.SESSION_NAME_RE) and publishes it as the group's
    `name`; a session that never sent one, the bot and the unattributed group have none."""

    def named_board(self, name, now=NOW - 2):
        plain = ot.board

        def board():
            b = plain()
            b.heartbeat({"session": ot.MINE, "status": "working", "project": "CortadOS-Router", "device": "pro",
                         "model": "claude-opus-5", "tool": None, "client": "claude-code", "name": name}, now=now)
            return b
        return board

    def test_the_heartbeat_name_is_the_rooms_name(self):
        hb, bad = office.validate_heartbeat({"session": ot.MINE, "status": "idle", "name": "Office round 2"})
        self.assertEqual((hb["name"], bad), ("Office round 2", None))
        with mock.patch.object(ot, "board", self.named_board("Office round 2")):
            t = live.office_tree(tree_view())
        g = {x["origin"]: x for x in t["orchestrators"]["items"]}
        self.assertEqual(g[ot.MINE]["name"], "Office round 2")
        self.assertIsNone(g[ot.OTHER]["name"])                    # its session sent no name
        self.assertIsNone(g["grokbot"]["name"])
        self.assertIsNone(g[None]["name"])

    def test_a_routed_main_loop_keeps_its_heartbeat_name(self):
        """OTHER's main loop routes (a `main:` conversation): the name still comes from its own heartbeat."""
        plain = ot.board

        def board():
            b = plain()
            b.heartbeat({"session": ot.OTHER, "status": "working", "project": "Grounds", "device": "studio",
                         "model": "claude-opus-5", "tool": None, "client": None, "name": "Grounds capture"},
                        now=NOW - 2)
            return b
        with mock.patch.object(ot, "board", board):
            g = {x["origin"]: x for x in live.office_tree(tree_view())["orchestrators"]["items"]}
        self.assertEqual(g[ot.OTHER]["name"], "Grounds capture")

    def test_name_rule(self):
        base = {"session": ot.MINE, "status": "working"}
        for ok in ("x", "Fix the router's S1 guard", "a" * 64):
            self.assertEqual(office.validate_heartbeat({**base, "name": ok})[0]["name"], ok)
        for none in (None, ""):
            self.assertIsNone(office.validate_heartbeat({**base, "name": none})[0]["name"])
        self.assertIsNone(office.validate_heartbeat(base)[0]["name"])
        # too long, control characters, non-ASCII, an address, not a string: the heartbeat is refused, never cut here
        for bad in ("a" * 65, "line\nbreak", "café", "ssh 10.0.0.7", 7, ["x"]):
            self.assertEqual(office.validate_heartbeat({**base, "name": bad}), (None, "name"), bad)


class ServerRoom(unittest.TestCase):
    """GET /v1/cortados/office/servers (office round 2): racks per host and the S1 console, numbers and fixed words."""

    def snap(self, **over):
        snap = {"generated_at": NOW,
                "hosts": {"as_of": NOW, "status": "ok", "errors": [], "provider_host": "air",
                          "items": [
                              {"name": "studio", "roles": ["exec host"], "heartbeat_state": "fresh",
                               "heartbeat": {"received_at": NOW - 8, "age_s": 8, "ttl_s": 900, "stale": False,
                                             "jobs": [{"name": "s1-scorer", "summary": ot.SECRET}],
                                             "s1": {"health": "up", "model": "s1-qwen3-4b-sft-v7", "flags": ["no-promote"],
                                                    "pending": "evaluation_pending", "loaded_s": 40.0}},
                               "hardware": {"cpu_busy_pct": 42.0, "mem_total_bytes": 1, "chip": "Apple M3 Ultra"}},
                              {"name": "air", "roles": ["provider", "default exec host"], "heartbeat_state": "none",
                               "heartbeat": None, "hardware": None},
                              {"name": "pc", "roles": [], "heartbeat_state": "stale",
                               "heartbeat": {"received_at": NOW - 1800, "age_s": 1800, "ttl_s": 900, "stale": True,
                                             "jobs": [], "s1": None}, "hardware": None},
                              {"name": "pro", "roles": [], "heartbeat_state": "none", "heartbeat": None,
                               "hardware": None},
                              {"name": "Forests-MacBook", "heartbeat_state": "fresh"}]},
                "s1": {"as_of": NOW, "status": "up", "errors": [], "available": True, "http_status": 200,
                       "latency_ms": 23.4, "reason": ot.SECRET,
                       "routing": {"status": "ok", "warn_above_pct": 20.0, "warning": None, "windows": [
                           {"window": "5m", "calls": 4, "degraded": 0, "degraded_pct": 0.0, "s1_ms_p50": 150.0,
                            "s1_ms_p90": 300.0, "degraded_wait_ms_p50": None},
                           {"window": "60m", "calls": 96, "degraded": 6, "degraded_pct": 6.2, "s1_ms_p50": 181.2,
                            "s1_ms_p90": 420.0, "degraded_wait_ms_p50": 15000.0}]}},
                "subscriptions": {"items": [{"account": "owner@example.com"}]}}
        snap.update(over)
        return snap

    def test_exact_fields(self):
        v = live.office_servers(self.snap())
        self.assertEqual(set(v), {"version", "generated_at", "hosts", "s1"})
        self.assertEqual(set(v["hosts"]), {"as_of", "status", "errors", "truncated", "items"})
        for h in v["hosts"]["items"]:
            self.assertEqual(set(h), set(live._SERVER_HOST_KEYS))
        self.assertEqual(set(v["s1"]), set(live._SERVER_S1_KEYS) | {"errors"})
        raw = json.dumps(v)
        for secret in (ot.SECRET, "owner@example.com", "cpu_busy_pct", "Apple", "no-promote", "evaluation_pending",
                       "s1-scorer", "Forests", "roles", "jobs"):
            self.assertNotIn(secret, raw, secret)

    def test_hosts_up_down_unknown(self):
        hosts = live.office_servers(self.snap())["hosts"]["items"]
        self.assertEqual([(h["name"], h["state"], h["as_of"], h["provider"]) for h in hosts],
                         [("air", "up", NOW, True), ("studio", "up", NOW - 8, False),
                          ("pro", "unknown", None, False), ("pc", "down", NOW - 1800, False)])

    def test_s1_numbers(self):
        s1 = live.office_servers(self.snap())["s1"]
        self.assertEqual(s1, {"status": "up", "health": "up", "model": "s1-qwen3-4b-sft-v7", "as_of": NOW,
                              "probe_ms": 23.4, "p50_ms": 181.2, "p90_ms": 420.0, "calls_1h": 96, "scored_1h": 90,
                              "degraded_pct_1h": 6.2, "degraded": False, "errors": []})
        warn = self.snap()
        warn["s1"]["routing"]["status"] = "warn"
        self.assertTrue(live.office_servers(warn)["s1"]["degraded"])

    def test_missing_sections_are_said_not_invented(self):
        v = live.office_servers({"generated_at": NOW})
        self.assertEqual((v["hosts"]["status"], v["hosts"]["items"]), ("error", []))
        self.assertEqual(v["s1"], {"status": "unknown", "health": None, "model": None, "as_of": None, "probe_ms": None,
                                   "p50_ms": None, "p90_ms": None, "calls_1h": None, "scored_1h": None,
                                   "degraded_pct_1h": None, "degraded": False, "errors": []})
        bad = self.snap(s1={"status": "up; rm", "latency_ms": -1, "routing": {"windows": [
            {"window": "60m", "calls": "96", "s1_ms_p50": float("nan")}]}})
        s1 = live.office_servers(bad)["s1"]
        self.assertEqual((s1["status"], s1["probe_ms"], s1["calls_1h"], s1["p50_ms"]), ("unknown", None, None, None))

    def test_racks_capped(self):
        items = [{"name": f"host{i}", "heartbeat_state": "none"} for i in range(12)]
        v = live.office_servers(self.snap(hosts={"status": "ok", "items": items}))
        self.assertEqual((len(v["hosts"]["items"]), v["hosts"]["truncated"]), (live.SERVER_RACKS_MAX, True))

    def test_the_adapter_fixture_is_what_the_provider_builds(self):
        self.assertEqual(json.loads(SERVERS_FIXTURE.read_text()), live.office_servers(self.snap()),
                         "regenerate with: python3 -B -c 'from tests.test_office_tree import write_fixture; "
                         "write_fixture()'")

    def test_collect_feeds_it(self):
        v = live.office_servers(tree_view())
        self.assertEqual(v["version"], 1)
        self.assertIn("studio", [h["name"] for h in v["hosts"]["items"]])


class TreeEndpoint(EndpointHarness):
    """The real endpoint: the office:read token reads the tree and nothing of the live view."""

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
            {"name": "claw3d", "token_sha256": clients.token_digest(OFFICE_TOKEN), "scopes": ["office:read"]}]}))
        self.reg.clients = clients.ClientTokens(path=cf, owner_token=TOKEN)
        self.httpd.live_usage_view = lambda reg, now: {"generated_at": now, "lanes": [], "hardware": [],
                                                       "s1": {"available": False, "reason": "no S1 URL configured"},
                                                       "errors": []}

    def request(self, path, token=None):
        h = {} if token is None else {"authorization": f"Bearer {token}"}
        req = urllib.request.Request(self.url + path, method="GET", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_paths_agree(self):
        self.assertEqual(clients.OFFICE_TREE_PATH, live.OFFICE_TREE_PATH)
        self.assertEqual(server.endpoint_id(live.OFFICE_TREE_PATH), "office_tree")
        self.assertEqual(clients.OFFICE_SERVERS_PATH, live.OFFICE_SERVERS_PATH)
        self.assertEqual(server.endpoint_id(live.OFFICE_SERVERS_PATH), "office_servers")
        self.assertEqual(clients.SCOPES["office:read"], frozenset({
            ("GET", clients.OFFICE_PATH), ("GET", clients.OFFICE_TREE_PATH), ("GET", clients.OFFICE_SERVERS_PATH)}))

    def test_office_read_opens_the_server_room_and_nothing_more(self):
        code, raw = self.request(live.OFFICE_SERVERS_PATH, OFFICE_TOKEN)
        self.assertEqual(code, 200, raw)
        v = json.loads(raw)
        self.assertEqual(set(v), {"version", "generated_at", "hosts", "s1"})
        self.assertEqual(set(v["s1"]), set(live._SERVER_S1_KEYS) | {"errors"})
        self.assertEqual(self.request(live.OFFICE_SERVERS_PATH, TOKEN)[0], 200)          # the owner
        self.assertEqual(self.request(live.OFFICE_SERVERS_PATH, LIVE_TOKEN)[0], 403)     # live:read is not office:read
        for tok in (None, "wrong-token-000000000000"):
            self.assertEqual(self.request(live.OFFICE_SERVERS_PATH, tok)[0], 401)
        for q in ("?x=1", "?"):
            self.assertEqual(self.request(live.OFFICE_SERVERS_PATH + q, OFFICE_TOKEN)[0], 400, q)
        self.assertEqual(self.request(live.OFFICE_SERVERS_PATH + "/x", OFFICE_TOKEN)[0], 403)
        # only GET: a POST with the office token is refused by the scope
        req = urllib.request.Request(self.url + live.OFFICE_SERVERS_PATH, data=b"{}", method="POST",
                                     headers={"authorization": f"Bearer {OFFICE_TOKEN}"})
        try:
            urllib.request.urlopen(req, timeout=30)
            self.fail("POST accepted")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_office_read_opens_the_tree_and_not_the_live_view(self):
        code, raw = self.request(live.OFFICE_TREE_PATH, OFFICE_TOKEN)
        self.assertEqual(code, 200, raw)
        t = json.loads(raw)
        self.assertEqual(set(t), {"version", "generated_at", "orchestrators", "lobby"})
        self.assertEqual(set(t["lobby"]), {"as_of", "status", "errors", "truncated", "items"})
        for r in t["lobby"]["items"]:
            self.assertEqual(set(r), set(live._TREE_LOBBY_KEYS) | {"ref", "status"})
        self.assertNotIn("tokens", keys_everywhere(t))
        self.assertEqual(self.request(live.OFFICE_TREE_PATH, TOKEN)[0], 200)            # the owner
        self.assertEqual(self.request("/v1/cortados/office", OFFICE_TOKEN)[0], 200)     # unchanged
        for p in (live.LIVE_PATH, live.SESSION_PATH + "?session=" + ot.MINE, "/v1/cortados/usage",
                  "/v1/cortados/tasks", "/v1/cortados/office/tree/x", "/resources"):
            self.assertEqual(self.request(p, OFFICE_TOKEN)[0], 403, p)
        self.assertEqual(self.request(live.OFFICE_TREE_PATH, LIVE_TOKEN)[0], 403)      # live:read is not office:read
        for tok in (None, "wrong-token-000000000000"):
            self.assertEqual(self.request(live.OFFICE_TREE_PATH, tok)[0], 401)

    def test_a_failed_server_room_is_a_500_without_the_error_text(self):
        # first request of this server: nothing memoised yet (live.Memo, MEMO_S)
        with mock.patch.object(self.httpd, "live_view", side_effect=RuntimeError(ot.SECRET)):
            code, raw = self.request(live.OFFICE_SERVERS_PATH, TOKEN)
        self.assertEqual(code, 500)
        self.assertNotIn(ot.SECRET, raw)

    def test_no_query_and_a_failure_is_a_500_not_an_empty_tree(self):
        for q in ("?session=" + ot.MINE, "?x=1", "?"):
            code, raw = self.request(live.OFFICE_TREE_PATH + q, OFFICE_TOKEN)
            self.assertEqual(code, 400, (q, raw))
            self.assertNotIn(ot.MINE, raw)
        with mock.patch.object(self.httpd, "live_view", side_effect=RuntimeError(ot.SECRET)):
            code, raw = self.request(live.OFFICE_TREE_PATH, TOKEN)
        self.assertEqual(code, 500)
        self.assertNotIn(ot.SECRET, raw)

    def test_a_routed_prompt_never_reaches_the_tree(self):
        # the tree reads the same collect as the live view (whose tests drive a real routed turn); a prompt in it
        # never reaches this body
        with mock.patch.object(self.httpd, "live_view", lambda reg, *a, **k: tree_view()):
            code, raw = self.request(live.OFFICE_TREE_PATH, OFFICE_TOKEN)
        self.assertEqual(code, 200, raw)
        self.assertNotIn(ot.SECRET, raw)
        self.assertEqual(json.loads(raw), sample_tree())


if __name__ == "__main__":
    unittest.main()
