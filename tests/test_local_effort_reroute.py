"""#122: the client's reasoning effort is held to what the local node accepts, and a node's pre-byte HTTP 5xx on a
cost-profile opening turn is routed elsewhere (never for a sensitive bind, a pin, or after bytes were sent).
Fakes only: test_provider_local's in-thread fake Ollama, the fake Claude CLI, a stubbed select and core.decide."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router.provider import effect_boundary, local_lane, routing, server
from tests.test_provider import TOKEN
from tests.test_provider_local import FakeOllama, TAG
from tests.test_provider_local_auto import FakeHandler, RelayConnection, body
from tests.test_provider_sensitive import SECRET_TERM, ContinuationRecheckBase, write_config
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)

# The node's answer observed on 2026-11-15 (a chat template refusing the effort level it was handed).
JINJA_500 = json.dumps({"type": "error", "error": {"type": "api_error", "message":
                        "Jinja Exception: Unexpected reasoning effort high. Supported types are xhigh (default), "
                        "medium, and low."}}).encode()
ORIGINAL_POST = FakeOllama.__dict__["do_POST"]       # the fake's normal answer, before any test patches it
CLAUDE_CODE_EFFORT = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}


class EffortMappingTests(unittest.TestCase):
    """local_lane.node_effort / _node_body: every client level, with and without a declared `efforts` list."""

    def test_no_declared_efforts_drops_every_level(self):
        for asked in local_lane.EFFORT_LEVELS + ("HIGH", "turbo", "", None, 3):
            self.assertIsNone(local_lane.node_effort(asked, None), asked)

    def test_xhigh_medium_low_template_declared_through_ollama(self):
        # Through Ollama `xhigh` arrives as `high`, so the observed template is declared ["medium", "low"].
        efforts = ["low", "medium"]
        want = {"low": "low", "medium": "medium", "high": "medium", "xhigh": "medium", "max": "medium"}
        for asked, level in want.items():
            self.assertEqual(local_lane.node_effort(asked, efforts), level, asked)
        self.assertEqual(local_lane.node_effort(" High ", efforts), "medium")
        self.assertIsNone(local_lane.node_effort("turbo", efforts))           # unknown: dropped, never guessed

    def test_never_more_than_asked_unless_nothing_is_lower(self):
        efforts = ["medium", "xhigh"]
        want = {"low": "medium", "medium": "medium", "high": "medium", "xhigh": "xhigh", "max": "xhigh"}
        for asked, level in want.items():
            self.assertEqual(local_lane.node_effort(asked, efforts), level, asked)

    def test_node_body_drops_the_effort_by_default_and_keeps_other_output_config(self):
        model = {"id": TAG, "alias": "qwen-27b", "thinking": "off-unless-asked", "efforts": None}
        sent = local_lane._node_body({**body("hi"), **CLAUDE_CODE_EFFORT}, model)
        self.assertNotIn("output_config", sent)
        self.assertEqual(sent["thinking"], {"type": "adaptive"})            # the client's thinking passes through
        kept = local_lane._node_body({**body("hi"), "output_config": {"effort": "high", "format": {"x": 1}}}, model)
        self.assertEqual(kept["output_config"], {"format": {"x": 1}})
        original = {**body("hi"), **CLAUDE_CODE_EFFORT}
        local_lane._node_body(original, model)
        self.assertEqual(original["output_config"], {"effort": "high"})     # the client's body is not mutated

    def test_node_body_maps_to_a_declared_level_on_every_variant(self):
        model = {"id": TAG, "alias": "qwen-27b", "thinking": "passthrough", "efforts": ["low", "medium"]}
        for extra in ({}, {"variant_thinking": "on"}, {"thinking_forced_off": True}):
            sent = local_lane._node_body({**body("hi"), **CLAUDE_CODE_EFFORT}, {**model, **extra})
            self.assertEqual(sent["output_config"], {"effort": "medium"}, extra)

    def test_config_validation(self):
        self.assertIsNone(local_lane._efforts({}, "pro", TAG))
        self.assertEqual(local_lane._efforts({"efforts": ["medium", "low"]}, "pro", TAG), ["low", "medium"])
        for bad in ([], "high", ["high", "high"], ["turbo"], [1], {"high": True}):
            with self.assertRaises(local_lane.LocalConfigError, msg=repr(bad)):
                local_lane._efforts({"efforts": bad}, "pro", TAG)


class NodeErrorRerouteRuleTests(unittest.TestCase):
    """local_lane.serve on a fake connection answering the observed 500: NODE_ERROR (nothing written) only for an
    opening turn (`reroute`) of an auto-routed cost-profile bind."""

    NODE = {"node": "pro", "base_url": "http://127.0.0.1:9", "connect_timeout_s": 1.0, "read_timeout_s": 1.0,
            "token": None}
    MODEL = {"id": TAG, "alias": "qwen-27b", "context_length": 32768, "efforts": None}

    def tearDown(self):
        local_lane._gate_reports.clear()            # a busy answer's gate report would mark the node busy for others

    def serve(self, status=500, raw=JINJA_500, **kw):
        h = FakeHandler()
        conn = RelayConnection(status, raw)
        conn.response.getheader = lambda name, default=None: default
        with patch.object(local_lane, "_connection", lambda base, timeout: conn):
            outcome = local_lane.serve(h, body(), self.NODE, self.MODEL, echo_model="cortados", **kw)
        return outcome, h.status

    def test_cost_profile_opening_turn_is_handed_back_unanswered(self):
        for reason in ("auto_route", "index", "prefer"):
            for status in (500, 502, 503, 504):
                self.assertEqual(self.serve(status, auto_routed=True, bind_reason=reason, reroute=True, cloud_ok=True),
                                 (local_lane.NODE_ERROR, None), (reason, status))

    def test_sensitive_profile_binds_keep_the_answered_error(self):
        for reason in ("sensitive_flag", "keyword", "refusal", None, "something-new"):
            self.assertEqual(self.serve(auto_routed=True, bind_reason=reason, reroute=True, cloud_ok=True),
                             ("rejected", 500), reason)

    def test_a_cost_bind_not_cleared_by_the_detectors_keeps_the_answered_error(self):
        # #122 round 2: the bind reason's name is never proof the request is not sensitive. Only an explicit True from
        # routing.request_sensitivity (local_lane.CLOUD_REROUTE_OK) clears it; absent or anything else fails closed.
        for reason in ("auto_route", "index", "prefer"):
            for cloud_ok in (False, None, "true", 1):
                self.assertEqual(self.serve(auto_routed=True, bind_reason=reason, reroute=True, cloud_ok=cloud_ok),
                                 ("rejected", 500), (reason, cloud_ok))
            self.assertEqual(self.serve(auto_routed=True, bind_reason=reason, reroute=True), ("rejected", 500), reason)

    def test_pins_continuations_and_client_errors_are_not_moved(self):
        self.assertEqual(self.serve(auto_routed=False, reroute=True, cloud_ok=True), ("rejected", 500))   # a pin
        self.assertEqual(self.serve(auto_routed=True, bind_reason="index", reroute=False, cloud_ok=True),
                         ("rejected", 500))
        bad = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}).encode()
        self.assertEqual(self.serve(400, bad, auto_routed=True, bind_reason="index", reroute=True, cloud_ok=True),
                         ("rejected", 400))

    def test_the_gates_busy_answer_keeps_its_retry(self):
        busy = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": local_lane.GATE_BUSY},
                           local_lane.GATE_KEY: {"limit": 1, "inflight": 1, "reason": "owner idle"}}).encode()
        self.assertEqual(self.serve(529, busy, auto_routed=True, bind_reason="index", reroute=True, cloud_ok=True),
                         ("rejected", 529))


class SensitivityPrecedenceTests(unittest.TestCase):
    """#122 round 2 guard: every sensitivity detector is asked before the cost preference, and the cloud re-route's
    clearance comes from the request itself (routing.request_sensitivity), never from the bind reason's name."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._env = os.environ.get(local_lane.CONFIG_ENV)
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", sensitive=True,
                                                         keywords=f"{SECRET_TERM}\n")
        self.addCleanup(self._restore)
        (_node, _entry, self.section), = local_lane.sensitive_routes()
        self.r = {"decision": {"answers": {"sensitive": True}}}

    def _restore(self):
        if self._env is None:
            os.environ.pop(local_lane.CONFIG_ENV, None)
        else:
            os.environ[local_lane.CONFIG_ENV] = self._env

    def secret(self):
        return body(f"explain the {SECRET_TERM}")

    def test_the_order_puts_every_detector_before_the_preference(self):
        self.assertEqual(routing.TRIGGER_PRECEDENCE[-1], "prefer")
        self.assertEqual(set(routing.TRIGGER_PRECEDENCE[:-1]), {"flag", "keyword", "s1"})
        for rule in routing.TRIGGER_PRECEDENCE[:-1]:                 # each detector binds a SENSITIVE profile
            self.assertEqual(effect_boundary.profile_for(routing.TRIGGER_REASONS.get(rule, rule)),
                             effect_boundary.SENSITIVE, rule)
        self.assertEqual(effect_boundary.profile_for(routing.TRIGGER_REASONS["prefer"]), effect_boundary.COST)

    def test_a_keyword_beats_prefer(self):
        self.assertEqual(routing.sensitive_trigger(self.secret(), {}, self.section, "prefer"), "keyword")
        self.assertEqual(routing.sensitive_trigger(body("hello"), {}, self.section, "prefer"), "prefer")

    def test_the_s1_signal_beats_prefer(self):
        with patch.object(routing, "S1_SENSITIVE_ANSWER", "sensitive"):
            self.assertEqual(routing.sensitive_trigger(body("hello"), self.r, self.section, "prefer"), "s1")
            self.assertEqual(routing.request_sensitivity(body("hello"), self.r, "prefer"), "s1")

    def test_the_flag_wins(self):
        self.assertEqual(routing.sensitive_trigger(self.secret(), {}, self.section, "sensitive"), "flag")
        self.assertEqual(routing.request_sensitivity(body("hello"), {}, "sensitive"), "flag")

    def test_request_sensitivity_ignores_the_preference(self):
        self.assertEqual(routing.request_sensitivity(self.secret(), {}, "prefer"), "keyword")
        self.assertEqual(routing.request_sensitivity(self.secret(), {}, None), "keyword")
        self.assertIsNone(routing.request_sensitivity(body("hello"), {}, "prefer"))

    def test_doubt_is_not_clearance(self):
        Path(self.dir, "keywords.txt").unlink()                    # an unreadable keyword list
        with self.assertLogs(routing.logger, level="WARNING"):
            self.assertEqual(routing.sensitive_trigger(self.secret(), {}, self.section, None), None)
        self.assertEqual(routing.request_sensitivity(self.secret(), {}, "prefer"), "unknown")
        Path(os.environ[local_lane.CONFIG_ENV]).write_text("{not json")   # an unreadable local config
        with self.assertLogs(routing.logger, level="WARNING"):
            self.assertEqual(routing.request_sensitivity(body("hello"), {}, None), "unknown")

    def test_stamp_clears_only_when_every_detector_cleared(self):
        moved = {"route_id": "x", "local": {}, "bind_reason": "auto_route"}
        self.assertIs(routing._stamp_sensitivity(moved, None)[local_lane.CLOUD_REROUTE_OK], True)
        for verdict in ("flag", "keyword", "s1", "unknown"):
            out = routing._stamp_sensitivity({**moved, local_lane.CLOUD_REROUTE_OK: True}, verdict)
            self.assertNotIn(local_lane.CLOUD_REROUTE_OK, out, verdict)

    def test_choose_binds_a_keyword_request_moved_by_a_cost_rule_as_sensitive(self):
        # keyword + auto_route / index: the sensitive move was not taken, a COST rule bound the request. It is still
        # sensitive by a detector: never cleared for the cloud re-route, and the effect boundary's profile is SENSITIVE.
        for reason in ("auto_route", "index"):
            got = self._choose(self.secret(), reason)
            self.assertNotIn(local_lane.CLOUD_REROUTE_OK, got, reason)
            self.assertEqual(got["bind_reason"], "keyword", reason)
            self.assertEqual(effect_boundary.profile_for(got["bind_reason"]), effect_boundary.SENSITIVE)
            clean = self._choose(body("hello"), reason)              # a request no detector marks keeps its cost bind
            self.assertIs(clean[local_lane.CLOUD_REROUTE_OK], True, reason)
            self.assertEqual(clean["bind_reason"], reason)

    def _choose(self, req, reason):
        s1 = {"route_id": "r1", "provider": "claude-cli", "model": "m", "band": "R0", "decision": {"answers": {}}}
        moved = {**s1, "provider": "local-pro", "local": {"node": {}, "model": {}}, "bind_reason": reason}
        with patch.object(routing.execution_routing, "select", lambda *a, **k: dict(s1)), \
                patch.object(routing, "served_routes", lambda core: []), \
                patch.object(routing, "sensitive_local", lambda core, b, r, **kw: r), \
                patch.object(routing, "auto_local", lambda core, b, r, **kw: dict(moved)):
            return routing.choose(object(), req, client="t", conversation_id="c")


def _node_500(handler):
    """FakeOllama's POST answered with the observed chat-template 500."""
    FakeOllama.seen.append((handler.path, json.loads(handler.rfile.read(int(handler.headers["content-length"])))))
    handler.send_response(500)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(JINJA_500)))
    handler.end_headers()
    handler.wfile.write(JINJA_500)


def _node_breaks_after_text(handler):
    """FakeOllama's POST: a 200 stream that writes text, then drops the connection before message_stop."""
    FakeOllama.seen.append((handler.path, json.loads(handler.rfile.read(int(handler.headers["content-length"])))))
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.end_headers()
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": TAG, "content": [], "stop_reason": None,
           "usage": {"input_tokens": 5, "output_tokens": 0}}
    for ev in ({"type": "message_start", "message": msg},
               {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
               {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial answer"}}):
        handler.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
        handler.wfile.flush()
    handler.close_connection = True


class RerouteEndToEndBase(ContinuationRecheckBase):
    """A routed `cortados` opening turn bound to the local lane, whose node answers the observed 500."""

    def setUp(self):
        super().setUp()
        p = patch.object(FakeOllama, "do_POST", _node_500)
        p.start()
        self.addCleanup(p.stop)
        self.routes = []
        real = self.reg.router

        def router(b, client, **kw):
            self.routes.append((kw.get("exclude_model"), real(b, client, **kw)))
            return self.routes[-1][1]
        self.reg.router = router

    def request(self, text="summarise the notes in this folder", stream=False):
        return {**body(text), "max_tokens": 256, "stream": stream, **CLAUDE_CODE_EFFORT}

    def send(self, req, headers=None):
        """(status, parsed JSON or raw text) of one POST, errors included."""
        r = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(req).encode(), method="POST",
                                   headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                            **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if resp.headers.get("content-type") == "application/json" \
                    else raw.decode()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def assertRerouted(self, headers=None, text="summarise the notes in this folder"):
        for stream in (False, True):
            FakeOllama.seen.clear()
            self.routes.clear()
            with self.assertLogs(server.logger, level="WARNING") as cm:
                code, got = self.send(self.request(text, stream), headers)
            self.assertEqual(code, 200, got)
            if stream:
                self.assertIn("message_stop", got)
            else:
                self.assertEqual(got.get("type"), "message")
            self.assertEqual(len(self.node_posts()), 1, stream)                   # tried once, never again
            self.assertNotIn("output_config", FakeOllama.seen[0][1])               # the effort never reached the node
            self.assertEqual([r.get("provider") for _, r in self.routes], ["local-pro", "claude-cli"])
            self.assertIn(TAG, self.routes[1][0].split(","))                      # the node's model excluded
            said = "\n".join(cm.output)
            self.assertIn("HTTP 5xx before any byte reached the client; routing it elsewhere", said)
            self.assertNotIn("Jinja", said)                                        # the node's text is not logged
            self.assertEqual(local_lane._inflight, {})

    def assertAnsweredLocally(self, headers=None, text="summarise the notes in this folder", streams=(False,)):
        for stream in streams:
            FakeOllama.seen.clear()
            self.routes.clear()
            code, got = self.send(self.request(text, stream), headers)
            self.assertEqual(code, 500, stream)
            self.assertIn("Unexpected reasoning effort", got["error"]["message"])
            self.assertEqual(len(self.node_posts()), 1, stream)
            self.assertEqual([r.get("provider") for _, r in self.routes], ["local-pro"], stream)   # never the cloud
            self.assertNotIn(local_lane.CLOUD_REROUTE_OK, self.routes[0][1])         # never cleared for the cloud
            self.assertEqual(self.reg.sessions, {})                                  # nothing left bound
            self.assertEqual(local_lane._inflight, {})


class AutoRouteNodeErrorTests(RerouteEndToEndBase):
    BAND = "R0"                                      # the R0 auto_route bind: cost profile

    def test_a_pre_byte_500_is_routed_to_the_cloud(self):
        self.assertRerouted()

    def test_a_break_after_bytes_is_not_routed_again(self):
        with patch.object(FakeOllama, "do_POST", _node_breaks_after_text):
            code, got = self.send(self.request(stream=True))
        self.assertEqual(code, 200)
        self.assertIn("partial answer", got)
        self.assertNotIn("message_stop", got)
        self.assertEqual([r.get("provider") for _, r in self.routes], ["local-pro"])
        self.assertEqual(len(self.node_posts()), 1)


class PreferNodeErrorTests(RerouteEndToEndBase):
    HEADERS_OPEN = {"x-cortados-local": "prefer"}    # prefer: local because it is cheaper (cost profile)

    def test_a_pre_byte_500_is_routed_to_the_cloud(self):
        self.assertRerouted(self.HEADERS_OPEN)


class PreferWithKeywordNodeErrorTests(RerouteEndToEndBase):
    """#122 round 2 (P1): a keyword-sensitive request that also carries `prefer` is sensitive, never a cost bind: the
    node's 500 is answered and no cloud backend is called, streaming or not. Before the fix sensitive_trigger returned
    "prefer" before it looked at the keywords, so the route log read [local-pro (prefer), claude-cli]."""
    HEADERS_OPEN = {"x-cortados-local": "prefer"}

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                     keywords=f"{SECRET_TERM}\n")

    def test_a_pre_byte_500_is_answered_and_never_leaves_the_machine(self):
        self.assertAnsweredLocally(self.HEADERS_OPEN, text=f"explain the {SECRET_TERM}", streams=(False, True))

    def test_the_bind_is_the_keyword_not_prefer(self):
        self.assertAnsweredLocally(self.HEADERS_OPEN, text=f"explain the {SECRET_TERM}")
        self.assertEqual(self.routes[0][1].get("bind_reason"), "keyword")

    def test_prefer_without_a_keyword_is_still_a_cost_bind_and_rerouted(self):
        self.assertRerouted(self.HEADERS_OPEN)


class SensitiveFlagNodeErrorTests(RerouteEndToEndBase):
    HEADERS_OPEN = {"x-cortados-local": "sensitive"}

    def test_a_pre_byte_500_is_answered_and_never_leaves_the_machine(self):
        self.assertAnsweredLocally(self.HEADERS_OPEN, streams=(False, True))


class KeywordAutoRouteNodeErrorTests(RerouteEndToEndBase):
    """A keyword-sensitive request the sensitive move did not take (its model, phi, excluded by the client) that the R0
    auto_route rule then bound: sensitive by a detector, so it takes the keyword bind and never reaches the cloud."""
    BAND = "R0"
    HEADERS_OPEN = {"x-cortados-exclude-model": "phi:latest"}

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}",
                     sensitive={"model": "phi:latest", "keywords_file": str(Path(self.scratch) / "keywords.txt"),
                                "refusal_fallback": False},
                     keywords=f"{SECRET_TERM}\n", auto_route={"model": "qwen-27b", "bands": ["R0", "R1"]})

    def test_a_pre_byte_500_is_answered_and_never_leaves_the_machine(self):
        self.assertAnsweredLocally(self.HEADERS_OPEN, text=f"explain the {SECRET_TERM}", streams=(False, True))
        self.assertEqual(self.routes[0][1]["local"]["model"]["id"], TAG)          # bound by auto_route's model
        self.assertEqual(self.routes[0][1].get("bind_reason"), "keyword")         # but as sensitive

    def test_without_the_keyword_auto_route_is_still_rerouted(self):
        self.assertRerouted(self.HEADERS_OPEN)


class KeywordNodeErrorTests(RerouteEndToEndBase):
    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                     keywords=f"{SECRET_TERM}\n")

    def test_a_pre_byte_500_is_answered_and_never_leaves_the_machine(self):
        self.assertAnsweredLocally(text=f"explain the {SECRET_TERM}", streams=(False, True))


class DeclaredEffortEndToEndTests(RerouteEndToEndBase):
    """A model entry that declares `efforts` gets the mapped level on the wire."""
    BAND = "R0"

    def setUp(self):
        super().setUp()
        p = Path(os.environ[local_lane.CONFIG_ENV])
        cfg = json.loads(p.read_text())
        cfg["nodes"][0]["ollama"]["models"][0]["efforts"] = ["medium", "low"]
        p.write_text(json.dumps(cfg))

    def test_high_reaches_the_node_as_medium(self):
        with patch.object(FakeOllama, "do_POST", ORIGINAL_POST):            # the normal fake answer
            code, got = self.send(self.request())
        self.assertEqual((code, got.get("type")), (200, "message"))
        self.assertEqual(FakeOllama.seen[0][1]["output_config"], {"effort": "medium"})


if __name__ == "__main__":
    unittest.main()
