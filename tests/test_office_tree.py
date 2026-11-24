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
FIXTURE = Path(__file__).resolve().parents[1] / "integrations/office-claw3d/fixtures/sample-tree.json"


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
    out = ot.backend()
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
    """Regenerate the adapter's fixture after a deliberate change to the tree's shape."""
    FIXTURE.write_text(json.dumps(sample_tree(), indent=1, ensure_ascii=False) + "\n")


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
        self.assertEqual(set(t), {"version", "generated_at", "orchestrators"})
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

    def test_a_failed_source_stays_visible(self):
        t = live.office_tree({"orchestrators": {"status": "partial", "items": [],
                                                "errors": [{"source": "child_tasks", "error": "PermissionError",
                                                            "extra": ot.SECRET}]}})
        self.assertEqual(t["orchestrators"]["status"], "partial")
        self.assertEqual(t["orchestrators"]["errors"], [{"source": "child_tasks", "error": "PermissionError"}])
        self.assertEqual(live.office_tree({})["orchestrators"]["status"], "error")   # no section is not "ok"


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

    def test_office_read_opens_the_tree_and_not_the_live_view(self):
        code, raw = self.request(live.OFFICE_TREE_PATH, OFFICE_TOKEN)
        self.assertEqual(code, 200, raw)
        t = json.loads(raw)
        self.assertEqual(set(t), {"version", "generated_at", "orchestrators"})
        self.assertNotIn("tokens", keys_everywhere(t))
        self.assertEqual(self.request(live.OFFICE_TREE_PATH, TOKEN)[0], 200)            # the owner
        self.assertEqual(self.request("/v1/cortados/office", OFFICE_TOKEN)[0], 200)     # unchanged
        for p in (live.LIVE_PATH, live.SESSION_PATH + "?session=" + ot.MINE, "/v1/cortados/usage",
                  "/v1/cortados/tasks", "/v1/cortados/office/tree/x", "/resources"):
            self.assertEqual(self.request(p, OFFICE_TOKEN)[0], 403, p)
        self.assertEqual(self.request(live.OFFICE_TREE_PATH, LIVE_TOKEN)[0], 403)      # live:read is not office:read
        for tok in (None, "wrong-token-000000000000"):
            self.assertEqual(self.request(live.OFFICE_TREE_PATH, tok)[0], 401)

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
