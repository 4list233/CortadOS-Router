"""Local auto-route (#75): after S1 has chosen a route, a node's auto_route model takes the conversation only while the
node is available (on the Pro: AC power + manual switch + gate up, all answered by its /api/tags probe), the band is
listed, the client did not exclude it, the wire is /v1/messages and the request fits. Fakes only: no Ollama, no socket
(the end-to-end case reuses test_provider_local's in-thread fake Ollama on loopback)."""
import json
import os
import tempfile
import unittest
import uuid
import sys
from pathlib import Path

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core
from cortados_router.provider import local_lane, routing
from tests.test_provider_local import FakeOllama, LocalHarness, TAG, write_config

S1_ROUTE = {"router": "cortados", "provider": "codex-cli", "model": "gpt-5.6-terra", "account": None,
            "config_dir": None, "context_length": 200000, "route_id": "execution:provider:call:abc", "api_request_id": "",
            "degraded": False, "band": "R0", "projection": None,
            "decision": {"answers": {"D": 1}, "degraded": False, "engine": "s1", "engine_ms": 5},
            "route": {"band": "R0", "chosen": {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra"},
                      "answers": {"D": 1}, "subagents": 0, "verifier": False, "hard_rules": ["s1 rule"]}}
AUTO = {"model": "qwen-27b", "bands": ["R0", "R1"]}


def body(text="list the files", **extra):
    return {"model": "cortados", "max_tokens": 64, "messages": [{"role": "user", "content": text}], **extra}


class AutoLocalBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = os.environ.get(local_lane.CONFIG_ENV)
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", auto_route=AUTO)
        self.probes = []
        self.tags = {TAG, "phi:latest"}
        self.unavailable = None                  # a LocalUnavailable message: the probe raises it
        self.logged = []
        self.selected = dict(S1_ROUTE)
        self._keep = (local_lane.probe, routing.execution_routing.select, core.log_route, core.STATE,
                      core.lane_health, routing._now)
        self.clock = [1000.0]
        routing._now = lambda: self.clock[0]
        routing._auto_down.clear()

        def probe(node):
            self.probes.append(node["node"])
            if self.unavailable:
                raise local_lane.LocalUnavailable(self.unavailable)
            return set(self.tags)
        local_lane.probe = probe
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(self.selected))
        core.log_route = lambda turn_id, meta, decision, r: self.logged.append((turn_id, meta, decision, r)) or True
        core.STATE = Path(tempfile.mkdtemp())
        core.lane_health = lambda: {}

    def tearDown(self):
        (local_lane.probe, routing.execution_routing.select, core.log_route, core.STATE, core.lane_health,
         routing._now) = self._keep
        routing._auto_down.clear()
        if self._env is None:
            os.environ.pop(local_lane.CONFIG_ENV, None)
        else:
            os.environ[local_lane.CONFIG_ENV] = self._env
        os.environ.pop("TEST_LOCAL_PRO_URL", None)

    def choose(self, b=None, **kw):
        return routing.choose(core, b or body(), client="t", conversation_id="conv-1", claude_bin=sys.executable,
                              codex_bin=sys.executable, **kw)

    def assertS1Route(self, r):
        self.assertEqual((r["provider"], r["model"]), ("codex-cli", "gpt-5.6-terra"))
        self.assertNotIn("local", r)
        self.assertEqual(self.logged, [])        # S1's own row stands untouched


class AutoLocalTakenTests(AutoLocalBase):
    def test_available_pro_takes_a_listed_band(self):
        r = self.choose()
        self.assertEqual(r["provider"], "local-pro")
        self.assertEqual(r["model"], TAG)
        self.assertEqual(r["band"], "R0")
        self.assertEqual(r["route_id"], S1_ROUTE["route_id"])
        self.assertFalse(r.get("pinned"))
        self.assertEqual(r["local"]["model"]["id"], TAG)
        self.assertEqual(r["context_length"], 32768)
        self.assertEqual(self.probes, ["pro"])
        # The routes row is rewritten under the same id: what will run, with S1's own choice kept as runner_up.
        (turn_id, meta, decision, route), = self.logged
        self.assertEqual(turn_id, S1_ROUTE["route_id"])
        self.assertEqual(route["chosen"]["lane"], "local-pro")
        self.assertEqual(route["chosen"]["model"], TAG)
        self.assertEqual(route["runner_up"]["model"], "gpt-5.6-terra")
        self.assertIn("s1 rule", route["hard_rules"])
        self.assertTrue(any("local auto-route" in h for h in route["hard_rules"]))
        self.assertEqual(decision["engine"], "s1")
        self.assertEqual(meta["request"], "list the files")

    def test_the_client_floor_does_not_apply_to_the_local_lane(self):
        keep = routing.floor.reserve
        routing.floor.reserve = lambda *a, **k: self.fail("a local lane spends no subscription quota")
        try:
            r = self.choose(min_left_pct=50.0)
        finally:
            routing.floor.reserve = keep
        self.assertEqual(r["provider"], "local-pro")

    def test_a_recovered_node_is_probed_again_after_the_down_window(self):
        self.unavailable = "node pro: local gate: local disabled: on battery"
        self.assertS1Route(self.choose())
        self.unavailable = None
        self.clock[0] += routing.AUTO_DOWN_S + 1
        self.assertEqual(self.choose()["provider"], "local-pro")


class AutoLocalSkippedTests(AutoLocalBase):
    def test_on_battery_keeps_s1_route(self):
        self.unavailable = "node pro: local gate: local disabled: on battery"
        self.assertS1Route(self.choose())

    def test_switch_off_keeps_s1_route(self):
        self.unavailable = "node pro: local gate: local switch off"
        self.assertS1Route(self.choose())

    def test_gate_down_keeps_s1_route_and_is_not_probed_again_at_once(self):
        self.unavailable = "node pro did not answer /api/tags within 2s (asleep, offline or busy?): ConnectionRefused"
        self.assertS1Route(self.choose())
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, ["pro"])          # the down window spares the second opener the probe wait

    def test_band_not_listed_is_not_probed(self):
        self.selected = {**S1_ROUTE, "band": "R2"}
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, [])

    def test_degraded_scorer_never_auto_routes(self):
        self.selected = {**S1_ROUTE, "degraded": True}
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, [])

    def test_routing_error_is_left_alone(self):
        self.selected = {"router": "cortados", "error": "nothing fits", "degraded": False}
        r = self.choose()
        self.assertEqual(r["error"], "nothing fits")
        self.assertEqual(self.probes, [])

    def test_excluded_local_model_keeps_s1_route(self):
        for name in ("qwen-27b", TAG):
            routing._auto_down.clear()
            self.assertS1Route(self.choose(exclude_model=name))
        self.assertEqual(self.probes, [])

    def test_responses_wire_is_never_auto_routed(self):
        self.assertS1Route(self.choose(body(**{routing.NO_LOCAL_KEY: True})))
        self.assertEqual(self.probes, [])

    def test_model_not_pulled_keeps_s1_route(self):
        self.tags = {"phi:latest"}
        self.assertS1Route(self.choose())

    def test_request_larger_than_the_context_keeps_s1_route(self):
        self.assertS1Route(self.choose(body("x" * (4 * 40000))))

    def test_switch_off_in_config_never_probes(self):
        write_config(self.dir, "http://127.0.0.1:9", auto_route=False)
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, [])

    def test_broken_local_config_does_not_block_s1_routing(self):
        Path(os.environ[local_lane.CONFIG_ENV]).write_text("{not json")
        self.assertS1Route(self.choose())


class FakeResponse:
    def __init__(self, status, raw):
        self.status, self._raw, self.length = status, raw, len(raw)

    def read(self, amt=None):
        return self._raw[:amt]


class FakeConnection:
    def __init__(self, status, raw):
        self.response = FakeResponse(status, raw)

    def request(self, *a, **k):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


class GateProbeTests(unittest.TestCase):
    """The gate's own refusals (local_gate._error: `local gate: <reason>` on a 503) make the probe raise LocalUnavailable
    naming the reason: that is what auto_local reads as 'Pro not available'. A fake connection, no socket."""

    NODE = {"node": "pro", "base_url": "http://127.0.0.1:9", "probe_timeout_s": 1.0, "token": None}

    def probe_with(self, status, payload):
        keep = local_lane._connection
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        local_lane._connection = lambda base, timeout: FakeConnection(status, raw)
        try:
            return local_lane.probe(self.NODE)
        finally:
            local_lane._connection = keep

    def test_gate_refusals_are_unavailable(self):
        for reason in ("local switch off", "local disabled: on battery", "local disabled: unknown power source"):
            with self.assertRaises(local_lane.LocalUnavailable) as cm:
                self.probe_with(503, {"type": "error", "error": {"type": "overloaded_error",
                                                                  "message": f"local gate: {reason}"}})
            self.assertIn(reason, str(cm.exception))

    def test_available_gate_lists_tags(self):
        self.assertEqual(self.probe_with(200, {"models": [{"name": TAG}]}), {TAG})


class AutoRouteConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = os.environ.get(local_lane.CONFIG_ENV)

    def tearDown(self):
        if self._env is None:
            os.environ.pop(local_lane.CONFIG_ENV, None)
        else:
            os.environ[local_lane.CONFIG_ENV] = self._env
        os.environ.pop("TEST_LOCAL_PRO_URL", None)

    def refused(self, value, word):
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", auto_route=value)
        with self.assertRaises(local_lane.LocalConfigError, msg=repr(value)) as cm:
            local_lane.nodes()
        self.assertIn(word, str(cm.exception))

    def test_bare_true_is_refused_there_is_no_default_band(self):
        self.refused(True, "no default band")

    def test_r3_and_r4_are_never_allowed(self):
        for band in ("R3", "R4"):
            self.refused({"model": "qwen-27b", "bands": ["R0", band]}, "outward")

    def test_model_must_be_one_of_the_nodes(self):
        self.refused({"model": "llama-70b", "bands": ["R0"]}, "auto_route.model")

    def test_bands_must_be_named(self):
        for bands in (None, [], "R0", [0]):
            self.refused({"model": "qwen-27b", "bands": bands}, "auto_route.bands")

    def test_unknown_keys_are_refused(self):
        self.refused({"model": "qwen-27b", "bands": ["R0"], "fallback": True}, "unknown keys")

    def test_valid_config_by_alias_or_tag(self):
        for name in ("qwen-27b", TAG, "QWEN-27B"):
            os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9",
                                                             auto_route={"model": name, "bands": ["R1", "R0", "R1"]})
            (node, model, bands), = local_lane.auto_routes()
            self.assertEqual((node["node"], model["id"], bands), ("pro", TAG, ["R1", "R0"]))

    def test_off_means_no_auto_routes_but_pins_stay(self):
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9")
        self.assertEqual(local_lane.auto_routes(), [])
        self.assertTrue(local_lane.pins())

    def test_example_config_parses_and_offers_no_studio_lane(self):
        example = Path(__file__).resolve().parent.parent / "tables" / "resources.example.json"
        token = Path(self.dir) / "gate-token"
        token.write_text("t" * 40)
        token.chmod(0o600)
        os.environ["CORTADOS_LOCAL_PRO_OLLAMA_URL"] = "http://127.0.0.1:9"
        os.environ["CORTADOS_LOCAL_PRO_GATE_TOKEN_FILE"] = str(token)
        try:
            ns = local_lane.nodes(str(example))
        finally:
            os.environ.pop("CORTADOS_LOCAL_PRO_OLLAMA_URL", None)
            os.environ.pop("CORTADOS_LOCAL_PRO_GATE_TOKEN_FILE", None)
        self.assertEqual([n["node"] for n in ns], ["pro"])   # the Studio runs S1 only (owner, 2026-11-13)
        self.assertIsNone(ns[0]["auto_route"])               # off until the owner names the bands


class AutoLocalEndToEndTests(LocalHarness):
    """An S1-routed `cortados` conversation that auto_local moves to the Pro is served by the local relay (the in-thread
    fake Ollama) and keeps its model: a continuation pinning another model is the usual 409."""

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", auto_route=AUTO)
        self._select = routing.execution_routing.select
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
            {**S1_ROUTE, "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        routing._auto_down.clear()

    def tearDown(self):
        routing.execution_routing.select = self._select
        routing._auto_down.clear()
        super().tearDown()

    def test_routed_conversation_runs_on_the_pro(self):
        got = self.post(body("hi"))
        self.assertEqual(len(FakeOllama.seen), 1)
        path, sent = FakeOllama.seen[0]
        self.assertEqual((path, sent["model"]), ("/v1/messages", TAG))
        self.assertEqual(got["model"], "cortados")                   # echoes what the client asked for
        self.assertEqual(got["type"], "message")


if __name__ == "__main__":
    unittest.main()
