"""Local auto-route (#75): after S1 has chosen a route, a node's auto_route model takes the conversation only while the
node is available (on the Pro: AC power + manual switch + gate up, all answered by its /api/tags probe), the band is
listed, the client did not exclude it, the wire is /v1/messages and the request fits. Fakes only: no Ollama, no socket
(the end-to-end case reuses test_provider_local's in-thread fake Ollama on loopback)."""
import json
import os
import sqlite3
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

    def test_s1_runner_up_is_kept_not_overwritten(self):
        s1_second = {"lane": "claude", "provider": "claude-cli", "model": "claude-sonnet-5"}
        self.selected = {**S1_ROUTE, "route": {**S1_ROUTE["route"], "runner_up": s1_second}}
        self.choose()
        (_, _, _, route), = self.logged
        self.assertEqual(route["runner_up"]["model"], "gpt-5.6-terra")        # S1's choice: what runs without the node
        self.assertEqual(route["s1_runner_up"], s1_second)
        self.assertEqual([s["model"] for s in route["skipped"]], ["claude-sonnet-5"])

    def test_rewrite_replaces_s1_row_under_the_same_route_id(self):
        core.log_route = self._keep[2]                          # the real writer, on this test's throwaway STATE
        core.log_route(S1_ROUTE["route_id"], {"request": "list the files"}, S1_ROUTE["decision"],
                       {**S1_ROUTE["route"], "subagents": 0, "verifier": False})
        self.assertEqual(self.choose()["provider"], "local-pro")
        with sqlite3.connect(core.STATE / "router.db") as db:
            rows = db.execute("SELECT turn_id, lane, model, runner_up FROM routes").fetchall()
        self.assertEqual([(t, l, m, json.loads(u)["model"]) for t, l, m, u in rows],
                         [(S1_ROUTE["route_id"], "local-pro", TAG, "gpt-5.6-terra")])

    def test_a_gate_401_is_a_warning_battery_is_info(self):
        for message, level in ((local_lane.GATE_REFUSED.format(node="pro"), "WARNING"),
                               ("node pro: local gate: local disabled: on battery", "INFO")):
            routing._auto_down.clear()
            self.unavailable = message
            with self.assertLogs(routing.logger, level="INFO") as cm:
                self.assertS1Route(self.choose())
            self.assertEqual([r.levelname for r in cm.records if "not taken" in r.getMessage()], [level])

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
    fake Ollama) and keeps its model: a continuation pinning another model is the usual terminal refusal."""

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

    TOOLS = [{"name": "Read", "input_schema": {"type": "object"}}]

    def converse_then_lose_the_node(self, model):
        """Open a tool conversation on `model`, then stop the node: the continuation's error message."""
        first = {**body("hi"), "model": model, "tools": self.TOOLS}
        got = self.post(first)
        self.assertEqual(len(self.reg.sessions), 1)
        nxt = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": got["content"][-1]["id"],
                                            "content": "ok"}]}
        self.ollama.shutdown()
        self.ollama.server_close()
        code, msg = self.error_of({**first, "messages": first["messages"] + [
            {"role": "assistant", "content": got["content"]}, nxt]})
        self.assertEqual(code, 503)
        self.assertEqual(len(self.reg.sessions), 1)          # the conversation stays bound to its node
        return msg

    def test_auto_routed_conversation_losing_its_node_is_not_called_a_pin(self):
        msg = self.converse_then_lose_the_node("cortados")
        self.assertIn(f"auto-routed by S1 to {TAG} on pro; the node is unreachable", msg)
        self.assertIn("start a new conversation (it is routed again)", msg)
        self.assertIn("x-cortados-exclude-model: qwen-27b", msg)
        self.assertNotIn("sub/", msg)

    def test_pinned_conversation_losing_its_node_keeps_the_pin_wording(self):
        msg = self.converse_then_lose_the_node("sub/qwen-27b")
        self.assertIn("local node pro unreachable for sub/qwen-27b", msg)
        self.assertIn("No other model was tried", msg)
        self.assertNotIn("auto-routed", msg)


class AutoLocalThinkingTests(AutoLocalEndToEndTests):
    """#108 on the auto-routed path: R0 reflex requests with small max_tokens reach a thinking model. The node is asked
    not to think; if it still spends the whole budget thinking, the client gets an error naming the cause, not an
    empty answer, and no conversation is kept."""

    SMALL = {**body("What is 6*7?"), "max_tokens": 20}

    def setUp(self):
        super().setUp()
        FakeOllama.thinker = "ollama"

    def test_small_max_tokens_is_answered_with_thinking_off(self):
        got = self.post(self.SMALL)
        self.assertEqual(FakeOllama.seen[-1][1]["thinking"], {"type": "disabled"})
        self.assertEqual((got["stop_reason"], got["content"]), ("end_turn", [{"type": "text", "text": "42"}]))

    def test_a_thinking_only_reply_is_an_error_not_an_empty_answer(self):
        FakeOllama.thinker = "ignores-disabled"
        code, msg = self.error_of(self.SMALL)
        self.assertEqual(code, 502)
        self.assertIn(f"auto-routed by S1 to {TAG} on pro, which used the whole max_tokens (20) without writing an "
                      "answer (thinking only)", msg)
        self.assertIn("x-cortados-exclude-model: qwen-27b", msg)
        self.assertNotIn("The user asks", msg)                       # never the model's text
        self.assertEqual(self.reg.sessions, {})

    def test_a_streamed_thinking_only_reply_ends_in_an_error_event(self):
        FakeOllama.thinker = "ignores-disabled"
        raw = self.post({**self.SMALL, "stream": True})
        events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("without writing an answer (thinking only)", events[-1]["error"]["message"])
        self.assertNotIn("message_stop", [e["type"] for e in events])
        self.assertNotIn("message_delta", [e["type"] for e in events])
        self.assertIn("event: error\n", raw)

    def test_client_thinking_is_passed_through_and_its_reply_relayed(self):
        asked = {"type": "enabled", "budget_tokens": 1024}
        got = self.post({**self.SMALL, "thinking": asked})
        self.assertEqual(FakeOllama.seen[-1][1]["thinking"], asked)
        self.assertEqual((got["stop_reason"], [b["type"] for b in got["content"]]), ("max_tokens", ["thinking"]))

    def test_what_counts_as_an_answer(self):
        keep = local_lane._answered
        self.assertTrue(keep([{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "4"}]))
        self.assertTrue(keep([{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]))
        self.assertFalse(keep([{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "  "}]))
        self.assertFalse(keep([]))


class FakeHandler:
    def __init__(self):
        import io
        self.status, self.wfile = None, io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def message(self):
        return json.loads(self.wfile.getvalue())["error"]["message"]


class RelayConnection(FakeConnection):
    """A connection for local_lane.serve: connects (or fails to), then answers one status."""

    def __init__(self, status=None, raw=b"", connect_error=None):
        super().__init__(status, raw)
        self.connect_error = connect_error
        self.sock = type("Sock", (), {"settimeout": lambda self, t: None})()

    def connect(self):
        if self.connect_error:
            raise self.connect_error


class RelayUnavailableWordingTests(unittest.TestCase):
    """local_lane.serve's message when the node is unavailable mid-conversation: an auto-routed conversation is told
    S1 routed it there (never a `sub/` pin it did not make); a pinned one keeps the pin wording. No socket."""

    NODE = {"node": "pro", "base_url": "http://127.0.0.1:9", "connect_timeout_s": 1.0, "read_timeout_s": 1.0,
            "token": None}
    MODEL = {"id": TAG, "alias": "qwen-27b", "context_length": 32768}

    def serve_with(self, conn, auto_routed):
        keep = local_lane._connection
        local_lane._connection = lambda base, timeout: conn
        h = FakeHandler()
        try:
            outcome = local_lane.serve(h, body(), self.NODE, self.MODEL, echo_model="cortados",
                                       auto_routed=auto_routed)
        finally:
            local_lane._connection = keep
        return outcome, h.status, h.message()

    CASES = {
        "unreachable": lambda: RelayConnection(connect_error=ConnectionRefusedError("refused")),
        "gate token": lambda: RelayConnection(401, b"{}"),
        "on battery": lambda: RelayConnection(503, json.dumps({"type": "error", "error": {
            "type": "overloaded_error", "message": "local gate: local disabled: on battery"}}).encode()),
    }

    def test_auto_routed_wording(self):
        for name, conn in self.CASES.items():
            outcome, status, msg = self.serve_with(conn(), auto_routed=True)
            self.assertEqual(status, 503, name)
            self.assertTrue(msg.startswith(f"cortados: this conversation was auto-routed by S1 to {TAG} on pro; "
                                           "the node is "), (name, msg))
            self.assertTrue(msg.endswith("; start a new conversation (it is routed again) or send "
                                         "x-cortados-exclude-model: qwen-27b"), (name, msg))
            self.assertNotIn("sub/", msg, name)
        self.assertIn("local disabled: on battery", self.serve_with(self.CASES["on battery"](), True)[2])

    def test_pinned_wording_is_unchanged(self):
        _, status, msg = self.serve_with(self.CASES["unreachable"](), auto_routed=False)
        self.assertEqual(status, 503)
        self.assertIn("local node pro unreachable for sub/qwen-27b", msg)
        _, _, msg = self.serve_with(self.CASES["gate token"](), auto_routed=False)
        self.assertEqual(msg, "cortados: " + local_lane.GATE_REFUSED.format(node="pro") + "; no other model was tried")
        outcome, status, msg = self.serve_with(self.CASES["on battery"](), auto_routed=False)
        self.assertEqual((outcome, status, msg), ("rejected", 503, "local gate: local disabled: on battery"))

    def test_an_unknown_503_body_is_never_repeated_as_a_policy_reason(self):
        raw = json.dumps({"type": "error", "error": {"type": "x", "message": "secret upstream text"}}).encode()
        outcome, status, msg = self.serve_with(RelayConnection(503, raw), auto_routed=True)
        self.assertEqual((outcome, status), ("rejected", 503))
        self.assertNotIn("auto-routed", msg)                 # relayed as any node error is, not dressed as policy


if __name__ == "__main__":
    unittest.main()
