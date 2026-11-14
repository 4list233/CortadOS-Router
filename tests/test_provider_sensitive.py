"""Sensitive requests (docs/LOCAL-RESOURCES.md "Sensitive requests"): the x-cortados-local flag, the owner's keyword list
and the refusal fallback move a request to a node's sensitive model in any band, but only with no effect outside the
machine and through every local guard. Fakes only: S1's select, the node probe and the route log are stand-ins, the
end-to-end cases use the fake Claude CLI and test_provider_local's in-thread fake Ollama on loopback."""
import json
import logging
import os
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core, session as launcher, subagent_mcp
from cortados_router.provider import bridge, local_lane, routing, server
from tests.test_provider import READ_TOOL, TOKEN
from tests.test_provider_local import FakeOllama, LocalHarness, TAG
from tests.test_provider_local_auto import S1_ROUTE, AutoLocalBase, body

SECRET_TERM = "zebra protocol"          # a stand-in keyword: never a real term (the repository is public)
CLEAN = {"steps": 1, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
         "outward": False, "work_kind": "research", "urgency": "now", "needs_clarification": False, "parts": 0,
         "diversity": False}
R3_ROUTE = {**S1_ROUTE, "band": "R3", "provider": "claude-cli", "model": "claude-opus-test",
            "decision": {"answers": dict(CLEAN), "degraded": False, "engine": "s1", "engine_ms": 5},
            "route": {**S1_ROUTE["route"], "band": "R3",
                      "chosen": {"lane": "claude", "provider": "claude-cli", "model": "claude-opus-test"}}}


def write_config(directory, base_url, sensitive=None, auto_route=False, keywords=None):
    """test_provider_local's config (node `pro`, qwen-27b + phi), plus a `sensitive` section and a keyword file."""
    kw_file = Path(directory) / "keywords.txt"
    if keywords is not None:
        kw_file.write_text(keywords)
    if sensitive is True:
        sensitive = {"model": "qwen-27b", "keywords_file": str(kw_file), "refusal_fallback": True}
    cfg = {"nodes": [{"id": "pro", "ollama": {
        "base_url_env": "TEST_LOCAL_PRO_URL", "auto_route": auto_route, "probe_timeout_s": 1, "connect_timeout_s": 1,
        "read_timeout_s": 5, **({"sensitive": sensitive} if sensitive is not None else {}),
        "models": [{"id": TAG, "alias": "qwen-27b", "name": "Qwen 27B", "context_length": 32768},
                   {"id": "phi:latest"}]}}]}
    p = Path(directory) / "resources.json"
    p.write_text(json.dumps(cfg))
    os.environ["TEST_LOCAL_PRO_URL"] = base_url
    return str(p)


class SensitiveBase(AutoLocalBase):
    def setUp(self):
        super().setUp()
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", sensitive=True,
                                                         keywords=f"# comment\n\n{SECRET_TERM}\nred-team kit\n")
        self.selected = json.loads(json.dumps(R3_ROUTE))

    def answers(self, **changes):
        self.selected["decision"]["answers"].update(changes)

    def assertLocal(self, r, rule):
        self.assertEqual((r["provider"], r["model"]), ("local-pro", TAG))
        self.assertEqual(r["band"], "R3")                 # any band: the band is S1's, unchanged
        (turn_id, _, _, route), = self.logged
        self.assertEqual(turn_id, S1_ROUTE["route_id"])
        self.assertEqual(route["runner_up"]["model"], "claude-opus-test")
        self.assertTrue(any(h.startswith(rule + ":") for h in route["hard_rules"]), route["hard_rules"])

    def assertCloud(self, r):
        self.assertEqual((r["provider"], r["model"]), ("claude-cli", "claude-opus-test"))
        self.assertNotIn("local", r)
        self.assertEqual(self.logged, [])


class SensitiveTakenTests(SensitiveBase):
    def test_flag_moves_any_band(self):
        self.assertLocal(self.choose(local="sensitive"), "sensitive: flag")

    def test_prefer_moves_and_is_recorded_as_a_preference(self):
        self.assertLocal(self.choose(local="prefer"), "local: prefer")

    def test_keyword_moves_case_insensitively_and_whole_word(self):
        self.assertLocal(self.choose(body("please explain the ZEBRA   Protocol in detail")), "sensitive: keyword")
        self.logged.clear()
        self.assertCloud(self.choose(body("zebra protocols are not the term")))      # not inside a longer word
        self.assertCloud(self.choose(body("a zebraprotocol")))

    def test_blast_radius_one_is_still_local(self):
        self.answers(blast_radius=1)
        self.assertLocal(self.choose(local="sensitive"), "sensitive: flag")

    def test_keyword_in_system_prompt_or_reminder_or_tool_result_does_not_match(self):
        b = body(f"<system-reminder>{SECRET_TERM}</system-reminder> hello", system=f"about {SECRET_TERM}")
        self.assertCloud(self.choose(b))
        b = {"model": "cortados", "messages": [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": SECRET_TERM}]}]}
        self.assertCloud(self.choose(b))

    def test_keyword_match_and_request_text_are_never_logged(self):
        text = f"tell me about the {SECRET_TERM} for my lab"
        with self.assertLogs("cortados", level="DEBUG") as cm:
            r = self.choose(body(text))
        self.assertEqual(r["provider"], "local-pro")
        said = "\n".join(rec.getMessage() for rec in cm.records)
        self.assertIn("sensitive: keyword", said)
        for leak in (SECRET_TERM, "zebra", "for my lab"):
            self.assertNotIn(leak, said.lower())
        (_, _, _, route), = self.logged
        self.assertNotIn("zebra", json.dumps(route["hard_rules"]).lower())

    def test_keyword_list_is_reread_when_it_changes(self):
        self.assertCloud(self.choose(body("about the ostrich method")))
        kw = Path(self.dir) / "keywords.txt"
        kw.write_text("ostrich method\n")
        os.utime(kw, ns=(1, 1))                         # a new mtime even on a coarse clock
        self.assertEqual(self.choose(body("about the Ostrich Method"))["provider"], "local-pro")

    def test_planned_s1_signal_hook(self):
        keep = routing.S1_SENSITIVE_ANSWER
        routing.S1_SENSITIVE_ANSWER = "sensitive"
        try:
            self.answers(sensitive=True)
            self.assertLocal(self.choose(), "sensitive: s1")
        finally:
            routing.S1_SENSITIVE_ANSWER = keep


class SensitiveNotTakenTests(SensitiveBase):
    def test_outward_effect_is_never_local_flag_or_keyword(self):
        self.answers(outward=True)
        for kw in ({"local": "sensitive"}, {"local": "prefer"}, {}):
            with self.assertLogs(routing.logger, level="INFO") as cm:
                self.assertCloud(self.choose(body(f"send the {SECRET_TERM} email"), **kw))
            self.assertTrue(any("outward effect" in rec.getMessage() for rec in cm.records))
        self.assertEqual(self.probes, [])
        self.assertNotIn("refusal_fallback", self.choose(local="sensitive"))

    def test_blast_radius_two_or_three_is_never_local(self):
        for br in (2, 3):
            self.answers(blast_radius=br)
            self.assertCloud(self.choose(local="sensitive"))
        self.assertEqual(self.probes, [])

    def test_missing_answers_fail_closed(self):
        self.selected["decision"]["answers"] = {"D": 1}
        self.assertCloud(self.choose(local="sensitive"))

    def test_degraded_scorer_is_never_local(self):
        self.selected.update(degraded=True)
        self.selected["decision"]["degraded"] = True
        self.assertCloud(self.choose(local="sensitive"))
        self.assertEqual(self.probes, [])

    def test_node_unavailable_keeps_s1_route_with_reason(self):
        self.unavailable = "node pro: local gate: local switch off"
        with self.assertLogs(routing.logger, level="INFO") as cm:
            r = self.choose(local="sensitive")
        self.assertCloud(r)
        self.assertTrue(any("sensitive: flag" in m and "not taken" in m and "local switch off" in m
                            for m in (rec.getMessage() for rec in cm.records)))
        self.assertIn("refusal_fallback", r)            # the node's state is checked again at refusal time

    def test_model_not_listed_or_too_large_keeps_s1_route(self):
        self.tags = {"phi:latest"}
        self.assertCloud(self.choose(local="sensitive"))
        self.tags = {TAG}
        self.assertCloud(self.choose(body("x" * (4 * 40000)), local="sensitive"))

    def test_excluded_model_keeps_s1_route_and_no_refusal_fallback(self):
        r = self.choose(local="sensitive", exclude_model="qwen-27b")
        self.assertCloud(r)
        self.assertNotIn("refusal_fallback", r)

    def test_responses_wire_is_never_local(self):
        with self.assertLogs(routing.logger, level="INFO") as cm:
            r = self.choose(body(**{routing.NO_LOCAL_KEY: True}), local="sensitive")
        self.assertCloud(r)
        self.assertNotIn("refusal_fallback", r)
        self.assertTrue(any("/v1/responses" in rec.getMessage() for rec in cm.records))

    def test_no_sensitive_section_logs_the_flag_skip(self):
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9")
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertCloud(self.choose(local="sensitive"))
        self.assertTrue(any("no local node has a sensitive section" in rec.getMessage() for rec in cm.records))

    def test_bad_flag_value_is_a_client_error(self):
        r = self.choose(local="always")
        self.assertTrue(r["client_error"])
        self.assertIn("x-cortados-local", r["error"])

    def test_pin_ignores_the_flag(self):
        r = self.choose(body(model="sub/phi:latest"), local="sensitive")
        self.assertEqual(r["model"], "phi:latest")
        self.assertTrue(r["pinned"])

    def test_unreadable_keyword_list_warns_without_naming_terms_and_flag_still_works(self):
        (Path(self.dir) / "keywords.txt").unlink()
        with self.assertLogs(routing.logger, level="WARNING") as cm:
            self.assertCloud(self.choose(body(f"the {SECRET_TERM}")))
        self.assertIn("keyword list", cm.records[0].getMessage())
        self.assertEqual(self.choose(local="sensitive")["provider"], "local-pro")


class RefusalRouteTests(SensitiveBase):
    def test_marker_only_when_eligible_and_refusal_fallback_on(self):
        r = self.choose(body("hello"))
        self.assertEqual(r["refusal_fallback"]["targets"], [{"node": "pro", "model": TAG}])
        os.environ[local_lane.CONFIG_ENV] = write_config(self.dir, "http://127.0.0.1:9", sensitive={
            "model": "qwen-27b", "refusal_fallback": False})
        self.assertNotIn("refusal_fallback", self.choose(body("hello")))

    def test_refusal_route_moves_only_after_the_local_answer(self):
        r = self.choose(body("hello"))
        self.assertEqual(self.logged, [])
        self.assertIsNone(routing.refusal_route(core, body("hello"), r, lambda moved: False))
        self.assertEqual(self.logged, [])               # no local answer: S1's record stands
        moved = routing.refusal_route(core, body("hello"), r, lambda moved: True)
        self.assertEqual(moved["provider"], "local-pro")
        self.assertNotIn("refusal_fallback", moved)
        (_, _, _, route), = self.logged
        self.assertTrue(any(h.startswith("sensitive: refusal:") for h in route["hard_rules"]))

    def test_refusal_route_rechecks_the_node(self):
        r = self.choose(body("hello"))
        self.unavailable = "node pro: local gate: local disabled: on battery"
        routing._auto_down.clear()
        self.assertIsNone(routing.refusal_route(core, body("hello"), r, lambda moved: self.fail("must not run")))

    def test_no_marker_means_no_fallback(self):
        self.answers(outward=True)
        r = self.choose(body("hello"))
        self.assertIsNone(routing.refusal_route(core, body("hello"), r, lambda moved: self.fail("must not run")))


class SensitiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        os.environ.pop("TEST_LOCAL_PRO_URL", None)

    def refused(self, value, word):
        with self.assertRaises(local_lane.LocalConfigError, msg=repr(value)) as cm:
            local_lane.nodes(write_config(self.dir, "http://127.0.0.1:9", sensitive=value))
        self.assertIn(word, str(cm.exception))

    def test_malformed_sections_are_refused(self):
        self.refused(True and "yes", "must be an object")
        self.refused({"model": "llama"}, "sensitive.model")
        self.refused({"model": "qwen-27b", "bands": ["R0"]}, "unknown keys")
        self.refused({"model": "qwen-27b", "refusal_fallback": "yes"}, "refusal_fallback")
        self.refused({"model": "qwen-27b", "keywords_file": ""}, "keywords_file")

    def test_absent_is_off_and_defaults(self):
        self.assertEqual(local_lane.sensitive_routes(write_config(self.dir, "http://127.0.0.1:9")), [])
        (_, model, sec), = local_lane.sensitive_routes(write_config(self.dir, "http://127.0.0.1:9",
                                                                    sensitive={"model": TAG}))
        self.assertEqual((model["id"], sec["refusal_fallback"], sec["keywords_file"]), (TAG, False, None))

    def test_keywords_file_env_unset_is_an_error_at_match_time_not_for_pins(self):
        p = write_config(self.dir, "http://127.0.0.1:9", sensitive={"model": TAG, "keywords_file_env": "NO_SUCH_VAR_X"})
        (_, _, sec), = local_lane.sensitive_routes(p)
        self.assertTrue(local_lane.pins(p))
        with self.assertRaises(local_lane.KeywordListError):
            local_lane.keyword_match(sec, "anything")

    def test_phrase_matching(self):
        pat = local_lane.compile_keywords(["# x", "", "Red Team", "c2"])
        for text, hit in (("our red\tteam plan", True), ("RED TEAM", True), ("redteam", False), ("the C2 server", True),
                          ("ac2", False), ("red teams", False)):
            self.assertEqual(bool(pat.search(text.casefold())), hit, text)
        self.assertIsNone(local_lane.compile_keywords(["# only comments", "  "]))

    def test_example_files(self):
        root = Path(__file__).resolve().parent.parent
        pat = local_lane.compile_keywords((root / "tables" / "sensitive-keywords.example.txt").read_text().splitlines())
        self.assertTrue(pat.search("a placeholder term one here"))
        self.assertIn("tables/sensitive-keywords.txt", (root / ".gitignore").read_text())


class HeaderTests(LocalHarness):
    """x-cortados-local on both wires: read for a new conversation and handed to the router; a bad value is a 400."""

    def setUp(self):
        super().setUp()
        self.seen = []
        self.reg.router = lambda b, client, **kw: (self.seen.append(kw), {"error": "stop here"})[1]

    def test_messages_wire(self):
        code = self.status_of(body("hi"), headers={"x-cortados-local": "Sensitive"})
        self.assertEqual(code, 503)
        self.assertEqual(self.seen, [{"local": "sensitive"}])

    def test_responses_wire(self):
        from tests.test_provider_responses import request, user
        try:
            self.post(request(user("hi"), tools=()), path="/v1/responses", headers={"x-cortados-local": "prefer"})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
        self.assertEqual(self.seen, [{"local": "prefer"}])

    def test_absent_header_sends_nothing_and_bad_value_is_400(self):
        self.status_of(body("hi"))
        self.assertEqual(self.seen, [{}])
        self.assertEqual(self.status_of(body("hi"), headers={"x-cortados-local": "yes please"}), 400)
        self.assertEqual(len(self.seen), 1)              # refused before routing


class RefusalFallbackEndToEndTests(LocalHarness):
    """The Claude bridge reports the fake CLI's refusal; the server re-runs an eligible opening turn on the fake Ollama
    and binds the conversation there. A refusal after a tool ran, or of an ineligible request, reaches the client."""

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True, keywords="")
        self.route_log = []
        self._log_route = core.log_route
        core.log_route = lambda turn_id, meta, decision, r: self.route_log.append((turn_id, r)) or True
        self.marker = {"targets": [{"node": "pro", "model": TAG}], "conversation_id": "c", "client": "t"}
        self.reg.router = self.router
        self.reg.refusal_route = lambda b, route, run: routing.refusal_route(core, b, route, run)
        # A refusal-bound conversation's continuations are rescored (ContinuationRecheckBase covers the refusals).
        self.reg.local_recheck = lambda b, route: routing.continuation_admission(core, b, route)
        self.scored = []
        core.decide = fake_decide(self.scored)
        self.executed = []
        self.reg.executed = lambda route: self.executed.append((route["provider"], route["model"]))
        routing._auto_down.clear()
        routing._admitted.clear()

    def tearDown(self):
        core.log_route = self._log_route
        routing._auto_down.clear()
        routing._admitted.clear()
        super().tearDown()

    def router(self, b, client, **kw):
        r = json.loads(json.dumps({**R3_ROUTE, "provider": "claude-cli", "model": "claude-test",
                                   "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        if self.marker:
            r["refusal_fallback"] = self.marker
        return r

    def posts(self):
        return [p for p, _ in FakeOllama.seen if p == "/v1/messages"]

    def test_first_turn_refusal_is_answered_locally(self):
        got = self.post(body("please refuse"))
        self.assertEqual(got["content"], [{"type": "text", "text": "hello from qwen"}])
        self.assertEqual(got["stop_reason"], "end_turn")
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(FakeOllama.seen[0][1]["stream"])
        (_, route), = self.route_log
        self.assertEqual(route["chosen"]["lane"], "local-pro")
        self.assertEqual(route["runner_up"]["model"], "claude-opus-test")
        self.assertEqual(self.executed, [("local-pro", TAG)])

    def test_revealing_client_is_told_the_local_model(self):
        got = self.post(body("please refuse"), headers={"x-cortados-reveal": "1"})
        self.assertEqual(got["model"], TAG)

    def test_streamed_refusal_is_answered_locally(self):
        raw = self.post(body("please refuse", stream=True))
        self.assertIn("hello from qwen", raw)
        self.assertNotIn("refused", raw)

    def test_conversation_is_bound_to_the_local_lane(self):
        first = {**body("please refuse"), "tools": [READ_TOOL]}
        got = self.post(first)
        self.assertEqual(got["content"][0]["type"], "tool_use")          # the local model's own answer
        (s,) = self.reg.sessions.values()
        self.assertIsInstance(s, server.LocalSession)
        nxt = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": got["content"][0]["id"],
                                            "content": "ok"}]}
        self.post({**first, "messages": first["messages"] + [{"role": "assistant", "content": got["content"]}, nxt]})
        self.assertEqual(len(self.posts()), 2)                          # the continuation went to the node too
        self.assertEqual(self.scored, [scored("please refuse", "ok")])   # rescored: the latest words + the new result

    def test_refusal_after_tool_use_is_not_rerun(self):
        first = {**body("refuse after tool"), "tools": [READ_TOOL]}
        got = self.post(first)
        uses = [b for b in got["content"] if b["type"] == "tool_use"]
        nxt = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "ok"}]}
        with self.assertLogs(server.logger, level="INFO") as cm:
            got = self.post({**first, "messages": first["messages"] + [
                {"role": "assistant", "content": got["content"]}, nxt]})
        self.assertEqual(got["stop_reason"], "refusal")
        self.assertEqual(self.posts(), [])
        self.assertTrue(any("refusal not taken" in rec.getMessage() for rec in cm.records))
        self.assertNotIsInstance(next(iter(self.reg.sessions.values())), server.LocalSession)

    def test_ineligible_refusal_reaches_the_client(self):
        self.marker = None                              # e.g. outward: sensitive_local attached no marker
        got = self.post(body("please refuse"))
        self.assertEqual(got["stop_reason"], "refusal")
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.route_log, [])

    def test_node_down_at_refusal_time_keeps_the_refusal(self):
        self.ollama.shutdown()
        self.ollama.server_close()
        got = self.post(body("please refuse"))
        self.assertEqual(got["stop_reason"], "refusal")
        self.assertEqual(self.route_log, [])

    def test_normal_answers_are_untouched(self):
        got = self.post(body("hello there"))
        self.assertEqual(got["content"][0]["text"], "echo: hello there")
        self.assertEqual(self.posts(), [])


class BridgeRefusalTests(unittest.TestCase):
    def test_refusal_constant_matches_the_fake_cli(self):
        self.assertEqual(bridge.REFUSAL_EVENT, "model_refusal_no_fallback")
        self.assertIn(bridge.REFUSAL_EVENT, (Path(__file__).with_name("fixtures") / "fake_claude_cli.py").read_text())


class McpLocalOptionTests(unittest.TestCase):
    def test_modes_mirror_the_provider(self):
        self.assertEqual(subagent_mcp.LOCAL_MODES, routing.LOCAL_MODES)
        self.assertEqual(launcher.LOCAL_MODES, routing.LOCAL_MODES)
        self.assertEqual(subagent_mcp.LOCAL_HEADER, routing.LOCAL_HEADER)
        for t in [subagent_mcp.TOOL] + subagent_mcp.CHILD_TOOLS:
            if t["name"] in ("route_task", "start_task"):
                self.assertEqual(t["inputSchema"]["properties"]["local"]["enum"], list(routing.LOCAL_MODES))

    def test_route_task_sends_the_header(self):
        from tests.test_subagent_mcp import FakeResp
        seen = []

        def _open(req, timeout=None):
            seen.append(req.headers.get("X-cortados-local"))
            return FakeResp({"model": "x", "content": [{"type": "text", "text": "ok"}]})
        os.environ.pop(subagent_mcp.NESTED_ENV, None)
        subagent_mcp.route_task("http://h:8432", "secret-test-0001", "t", urlopen=_open, local="sensitive")
        subagent_mcp.route_task("http://h:8432", "secret-test-0001", "t", urlopen=_open)
        self.assertEqual(seen, ["sensitive", None])
        r = subagent_mcp.route_task("http://h:8432", "secret-test-0001", "t", urlopen=_open, local="maybe")
        self.assertTrue(r["isError"])
        self.assertEqual(len(seen), 2)

    def test_handle_passes_local_through_both_tools(self):
        from unittest import mock
        ct = subagent_mcp._child_module()
        with mock.patch.object(subagent_mcp, "route_task", return_value={"content": []}) as rt:
            subagent_mcp.handle({"id": 1, "method": "tools/call", "params": {
                "name": "route_task", "arguments": {"task": "t", "local": "sensitive"}}}, {"url": "u", "token": "secret-test-0001"})
        self.assertEqual(rt.call_args.kwargs["local"], "sensitive")
        with mock.patch.object(ct, "start", return_value={"task_id": "t"}) as start:
            subagent_mcp.handle({"id": 2, "method": "tools/call", "params": {
                "name": "start_task", "arguments": {"task": "t", "local": "sensitive"}}}, {"url": "u", "token": "secret-test-0001"})
        self.assertEqual(start.call_args.kwargs["local"], "sensitive")

    def test_child_session_carries_the_header(self):
        lines = launcher.custom_headers("t20261113185837-77ae1da7", local="sensitive").splitlines()
        self.assertIn("x-cortados-local: sensitive", lines)
        self.assertNotIn("x-cortados-local", launcher.custom_headers("t20261113185837-77ae1da7"))
        with self.assertRaises(launcher.SessionError):
            launcher.custom_headers(None, local="sensitive\nx-api-key: stolen")

    def test_start_task_refuses_bad_local_and_chrome(self):
        ct = subagent_mcp._child_module()
        os.environ.pop(ct.NESTED_ENV, None)
        with self.assertRaises(ct.TaskError) as cm:
            ct.start("a task", local="yes")
        self.assertIn("local must be one of", str(cm.exception))
        with self.assertRaises(ct.TaskError) as cm:
            ct.start("a task", chrome=True, local="sensitive")
        self.assertIn("chrome", str(cm.exception))


# ------------------------------------------------------------------------------------- review fixes (gpt-6.1-sol)
SAFE = dict(CLEAN)
OUTWARD_TEXT = "email the signed invoice to the client and mark it paid"


def scored(words, *results):
    """The text S1 scores for a continuation: the latest user words, then the new tool results (continuation_text)."""
    return words + (routing.TOOL_RESULTS_LABEL + "\n---\n".join(results) if results else "")


class LocalAdmissionTests(unittest.TestCase):
    """Finding 3: routing.local_admission, the one shared gate, takes only exact values and fails closed."""

    def admit(self, answers, band="R2", degraded=False):
        ok, why = routing.local_admission(answers, band, degraded)
        self.assertIsInstance(why, str)
        return ok

    def test_admits_exactly_zero_or_one_and_outward_false(self):
        for br in (0, 1):
            self.assertTrue(self.admit({**SAFE, "blast_radius": br}))

    def test_malformed_blast_radius_is_refused(self):
        for br in (-1, False, True, 1.9, 1.0, 0.0, "1", "0", None, 2, 3, [0]):
            self.assertFalse(self.admit({**SAFE, "blast_radius": br}), repr(br))

    def test_outward_must_be_exactly_false(self):
        for out in (True, 0, None, "false", "", []):
            self.assertFalse(self.admit({**SAFE, "outward": out}), repr(out))

    def test_missing_answers_degraded_and_band(self):
        self.assertFalse(self.admit({"D": 1}))
        self.assertFalse(self.admit({"blast_radius": 0}))
        self.assertFalse(self.admit({"outward": False}))
        self.assertFalse(self.admit(None))
        self.assertFalse(self.admit("garbled"))
        for deg in (True, None, 0, "no"):
            self.assertFalse(self.admit(SAFE, degraded=deg), repr(deg))
        for band in (None, "", 2):
            self.assertFalse(self.admit(SAFE, band=band), repr(band))

    def test_route_admission_reads_route_and_decision(self):
        r = {"band": "R1", "degraded": False, "decision": {"answers": dict(SAFE), "degraded": False}}
        self.assertTrue(routing.route_admission(r)[0])
        self.assertFalse(routing.route_admission({**r, "decision": {"answers": dict(SAFE), "degraded": True}})[0])
        self.assertFalse(routing.route_admission({**r, "decision": {"answers": dict(SAFE)}})[0])   # flag missing
        self.assertTrue(routing.route_admission({"band": "R1", "decision": {"answers": dict(SAFE), "degraded": False}})[0])
        self.assertFalse(routing.route_admission(None)[0])


class SharedGateTests(SensitiveBase):
    """Finding 2: auto_local passes the same gate as sensitive_local, so one rejecting no longer lets the other move
    the request. Both reviewer reproductions, plus the refusal fallback reading the gate again."""

    def setUp(self):
        super().setUp()
        os.environ[local_lane.CONFIG_ENV] = write_config(
            self.dir, "http://127.0.0.1:9", sensitive=True, keywords=f"{SECRET_TERM}\n",
            auto_route={"model": "qwen-27b", "bands": ["R0", "R1", "R2"]})

    def band(self, b):
        self.selected["band"] = b
        self.selected["route"]["band"] = b

    def test_r2_blast_two_flag_and_auto_route_stays_cloud(self):
        self.band("R2")
        self.answers(blast_radius=2)
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertCloud(self.choose(local="sensitive"))
        self.assertTrue(any("local auto-route not taken" in rec.getMessage() and "blast_radius 2" in rec.getMessage()
                            for rec in cm.records))
        self.assertEqual(self.probes, [])

    def test_r0_missing_outward_with_auto_route_stays_cloud(self):
        self.band("R0")
        del self.selected["decision"]["answers"]["outward"]
        self.assertCloud(self.choose())
        self.assertCloud(self.choose(local="sensitive"))
        self.assertEqual(self.probes, [])

    def test_r0_safe_still_auto_routes(self):
        self.band("R0")
        r = self.choose(body("hello"))
        self.assertEqual(r["provider"], "local-pro")
        self.assertTrue(any("local auto-route" in h for h in self.logged[0][3]["hard_rules"]))

    def test_reviewer_blast_radius_values_are_never_local(self):
        for band in ("R0", "R3"):
            for br in (-1, False, 1.9):
                with self.subTest(band=band, br=br):
                    self.band(band)
                    self.answers(blast_radius=br)
                    for kw in ({"local": "sensitive"}, {}):
                        self.assertCloud(self.choose(**kw))
        self.assertEqual(self.probes, [])

    def test_refusal_route_rechecks_the_gate_even_with_a_marker(self):
        r = self.choose(body("hello"))
        self.assertIn("refusal_fallback", r)
        r["decision"]["answers"]["outward"] = True                 # a marker on a route the gate refuses
        self.assertIsNone(routing.refusal_route(core, body("hello"), r, lambda moved: self.fail("must not run")))
        r["decision"]["answers"].update(outward=False, blast_radius=1.9)
        self.assertIsNone(routing.refusal_route(core, body("hello"), r, lambda moved: self.fail("must not run")))


def fake_decide(calls, *, outward_words=("email", "invoice", "deploy"), degraded=False, answers=None):
    """A stand-in for core.decide: outward for a text with one of `outward_words`, else CLEAN; records each text."""
    def decide(text, ctx=None):
        calls.append(text)
        if degraded:
            return {"answers": None, "degraded": True, "engine": "s1", "error": "scorer down"}
        if answers is not None:
            return {"answers": dict(answers), "degraded": False, "engine": "s1", "engine_ms": 1}
        out = any(w in text.lower() for w in outward_words)
        return {"answers": {**CLEAN, "outward": out, "blast_radius": 3 if out else 0}, "degraded": False,
                "engine": "s1", "engine_ms": 1}
    return decide


class ContinuationRecheckBase(LocalHarness):
    """Finding 1, end to end: a conversation an automatic rule bound to the local lane is rescored on every later turn
    before anything reaches the node. Fake Claude CLI, in-thread fake Ollama, a stubbed select and a fake core.decide."""

    BAND = "R3"
    HEADERS_OPEN: dict = {}

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True, keywords="",
                     auto_route={"model": "qwen-27b", "bands": ["R0", "R1"]})
        self._select = routing.execution_routing.select
        selected = {**R3_ROUTE, "band": self.BAND, "route": {**R3_ROUTE["route"], "band": self.BAND}}
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
            {**selected, "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        self._log_route = core.log_route
        core.log_route = lambda *a: True
        self.reg.local_recheck = lambda b, route: routing.continuation_admission(core, b, route)
        self.reg.refusal_route = lambda b, route, run: routing.refusal_route(core, b, route, run)
        self.scored = []
        core.decide = fake_decide(self.scored)
        routing._auto_down.clear()
        routing._admitted.clear()

    def tearDown(self):
        routing.execution_routing.select = self._select
        core.log_route = self._log_route
        routing._auto_down.clear()
        routing._admitted.clear()
        super().tearDown()

    def node_posts(self):
        return [p for p, _ in FakeOllama.seen if p == "/v1/messages"]

    def open(self, text="summarise the notes in this folder", model="cortados", **extra):
        first = {**body(text), "model": model, "tools": [READ_TOOL], **extra}
        got = self.post(first, headers=self.HEADERS_OPEN)
        (s,) = self.reg.sessions.values()
        self.assertIsInstance(s, server.LocalSession)
        self.assertEqual(len(self.node_posts()), 1)
        self.scored.clear()                         # the opening's own scoring is select's (stubbed) business
        return first, got

    def follow(self, first, got, text, stream=False):
        uses = [b for b in got["content"] if b["type"] == "tool_use"]
        content = [{"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "ok"}]
        if text is not None:
            content.append({"type": "text", "text": text})
        return {**first, "stream": stream, "messages": first["messages"] + [
            {"role": "assistant", "content": got["content"]}, {"role": "user", "content": content}]}

    def refused(self, req):
        req_ = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(req).encode(), method="POST",
                                      headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req_, timeout=30)
        e = cm.exception
        self.assertEqual(e.code, 400)
        self.assertEqual(e.headers.get("x-should-retry"), "false")
        err = json.loads(e.read())["error"]
        self.assertEqual(err["type"], "invalid_request_error")
        self.assertIn("bound to a local model", err["message"])
        self.assertIn("needs a cloud model", err["message"])
        return err["message"]

    def assertRefusedOutward(self, stream):
        first, got = self.open()
        with self.assertLogs(server.logger, level="INFO") as cm:
            msg = self.refused(self.follow(first, got, OUTWARD_TEXT, stream=stream))
        self.assertEqual(len(self.node_posts()), 1)          # the turn never reached the node
        self.assertEqual(self.scored, [scored(OUTWARD_TEXT, "ok")])
        said = "\n".join(rec.getMessage() for rec in cm.records)
        self.assertIn("local continuation refused", said)
        for leak in ("invoice", "email", "paid"):            # metadata only, in the log and the message
            self.assertNotIn(leak, said.lower())
            self.assertNotIn(leak, msg.lower())


class FlagBoundContinuationTests(ContinuationRecheckBase):
    HEADERS_OPEN = {"x-cortados-local": "sensitive"}

    def test_outward_continuation_is_refused_json(self):
        self.assertRefusedOutward(stream=False)

    def test_outward_continuation_is_refused_streaming(self):
        self.assertRefusedOutward(stream=True)

    def test_safe_continuation_is_rescored_and_reaches_the_node(self):
        first, got = self.open()
        got2 = self.post(self.follow(first, got, "now list the headings"))
        self.assertEqual(got2["content"][0]["type"], "tool_use")
        self.assertEqual(len(self.node_posts()), 2)
        self.assertEqual(self.scored, [scored("now list the headings", "ok")])   # this turn's words, not the opening's

    def test_safe_continuation_is_rescored_streaming(self):
        first, got = self.open()
        raw = self.post(self.follow(first, got, "now list the headings", stream=True))
        self.assertIn("tool_use", raw)
        self.assertEqual(self.scored, [scored("now list the headings", "ok")])

    def test_a_new_tool_result_is_rescored_not_served_from_the_cache(self):
        first, got = self.open()
        req = self.follow(first, got, "now list the headings")
        got2 = self.post(req)
        nxt = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": got2["content"][0]["id"],
                                            "content": "ok"}]}
        later = {**req, "messages": req["messages"] + [{"role": "assistant", "content": got2["content"]}, nxt]}
        self.post(later)
        self.assertEqual(len(self.node_posts()), 3)
        # Same words and the same result text, but a later turn (more messages): scored again, the cache missed.
        self.assertEqual(self.scored, [scored("now list the headings", "ok")] * 2)

    def test_an_identical_retry_reuses_the_admission_and_a_new_result_does_not(self):
        route = {"band": "R3", "route_id": "r-cache"}
        msgs = [{"role": "user", "content": "list the headings"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "# One"}]}]
        req = {"model": "cortados", "messages": msgs}
        self.assertTrue(routing.continuation_admission(core, req, route)[0])
        ok, why = routing.continuation_admission(core, json.loads(json.dumps(req)), route)   # the same request again
        self.assertTrue(ok)
        self.assertIn("admitted earlier", why)
        self.assertEqual(len(self.scored), 1)
        more = {"model": "cortados", "messages": msgs + [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "# Two"}]}]}
        self.assertTrue(routing.continuation_admission(core, more, route)[0])
        self.assertEqual(self.scored, [scored("list the headings", "# One"), scored("list the headings", "# Two")])
        same_text = {"model": "cortados", "messages": msgs + [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "# One"}]}]}
        self.assertTrue(routing.continuation_admission(core, same_text, route)[0])   # same text, more messages
        self.assertEqual(len(self.scored), 3)

    def test_refused_continuation_leaves_the_session_usable(self):
        first, got = self.open()
        self.refused(self.follow(first, got, OUTWARD_TEXT))
        (s,) = self.reg.sessions.values()
        self.assertIsInstance(s, server.LocalSession)
        got2 = self.post(self.follow(first, got, "then just count the files"))   # same history, a safe turn
        self.assertEqual(got2["content"][0]["type"], "tool_use")
        self.assertEqual(len(self.node_posts()), 2)
        self.assertIs(next(iter(self.reg.sessions.values())), s)

    def test_degraded_or_garbled_scorer_fails_closed(self):
        for decide in (fake_decide(self.scored, degraded=True),
                       fake_decide(self.scored, answers={"D": 1}),
                       fake_decide(self.scored, answers={**CLEAN, "blast_radius": 2}),
                       fake_decide(self.scored, answers={**CLEAN, "blast_radius": -1}),
                       fake_decide(self.scored, answers={**CLEAN, "outward": None})):
            routing._admitted.clear()
            FakeOllama.seen = []
            for s in list(self.reg.sessions.values()):
                self.reg.drop(s)
            core.decide = fake_decide(self.scored)
            first, got = self.open()
            core.decide = decide
            self.refused(self.follow(first, got, "now list the headings"))
            self.assertEqual(len(self.node_posts()), 1)

    def test_scorer_exception_fails_closed(self):
        first, got = self.open()

        def boom(text, ctx=None):
            raise RuntimeError("down")
        core.decide = boom
        self.refused(self.follow(first, got, "now list the headings"))

    def test_turn_without_any_user_text_fails_closed(self):
        req = {"model": "cortados", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}]}
        self.assertEqual(routing.continuation_admission(core, req, {"band": "R3", "route_id": "r"}),
                         (False, "no user text to score on this turn"))
        self.assertEqual(self.scored, [])
        req["messages"][0]["content"].append({"type": "text", "text": "list them"})   # earlier words are used
        self.assertTrue(routing.continuation_admission(core, req, {"band": "R3", "route_id": "r"})[0])
        self.assertEqual(self.scored, [scored("list them", "ok")])

    def test_no_checker_configured_fails_closed(self):
        first, got = self.open()
        self.reg.local_recheck = None
        self.refused(self.follow(first, got, "now list the headings"))
        self.assertEqual(len(self.node_posts()), 1)


class KeywordBoundContinuationTests(ContinuationRecheckBase):
    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                     keywords=f"{SECRET_TERM}\n")

    def test_outward_continuation_is_refused(self):
        first, got = self.open(text=f"explain the {SECRET_TERM}")
        self.refused(self.follow(first, got, OUTWARD_TEXT))
        self.assertEqual(len(self.node_posts()), 1)


class AutoRouteBoundContinuationTests(ContinuationRecheckBase):
    """The R0 auto_route bind (main's own case) is covered by the same continuation re-check."""
    BAND = "R0"

    def test_outward_continuation_is_refused_json(self):
        self.assertRefusedOutward(stream=False)

    def test_outward_continuation_is_refused_streaming(self):
        self.assertRefusedOutward(stream=True)

    def test_safe_continuation_continues(self):
        first, got = self.open()
        self.post(self.follow(first, got, "and the next file"))
        self.assertEqual(len(self.node_posts()), 2)
        self.assertEqual(self.scored, [scored("and the next file", "ok")])


class RefusalBoundContinuationTests(ContinuationRecheckBase):
    def test_outward_continuation_after_a_refusal_bind_is_refused(self):
        first, got = self.open(text="please refuse")
        self.refused(self.follow(first, got, OUTWARD_TEXT))
        self.assertEqual(len(self.node_posts()), 1)

    def test_outward_continuation_after_a_refusal_bind_is_refused_streaming(self):
        first, got = self.open(text="please refuse")
        self.refused(self.follow(first, got, OUTWARD_TEXT, stream=True))


class ExplicitPinNotRescoredTests(ContinuationRecheckBase):
    def test_local_pin_continuation_is_not_rescored(self):
        def no_s1(text, ctx=None):
            raise AssertionError("an explicit local pin must not be rescored")
        core.decide = no_s1
        for model in ("sub/qwen-27b", f"sub/{TAG}"):
            for s in list(self.reg.sessions.values()):
                self.reg.drop(s)
            FakeOllama.seen = []
            first, got = self.open(model=model)
            self.post(self.follow(first, got, OUTWARD_TEXT))
            self.assertEqual(len(self.node_posts()), 2)          # the client's choice: relayed


class RefusalFallbackUsableAnswerTests(LocalHarness):
    """Finding 4: a local fallback answer with no text or valid tool call is not an answer; the original refusal
    goes to the client and nothing is bound locally or recorded."""

    def setUp(self):
        super().setUp()
        write_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True, keywords="")
        self.route_log = []
        self._log_route = core.log_route
        core.log_route = lambda turn_id, meta, decision, r: self.route_log.append((turn_id, r)) or True
        marker = {"targets": [{"node": "pro", "model": TAG}], "conversation_id": "c", "client": "t"}
        self.reg.router = lambda b, client, **kw: json.loads(json.dumps(
            {**R3_ROUTE, "provider": "claude-cli", "model": "claude-test", "refusal_fallback": marker,
             "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        self.reg.refusal_route = lambda b, route, run: routing.refusal_route(core, b, route, run)
        self.executed = []
        self.reg.executed = lambda route: self.executed.append((route["provider"], route["model"]))
        routing._auto_down.clear()

    def tearDown(self):
        core.log_route = self._log_route
        routing._auto_down.clear()
        super().tearDown()

    def check(self, reply, stream=False, tools=False):
        FakeOllama.reply = reply
        req = {**body("please refuse", stream=stream), **({"tools": [READ_TOOL]} if tools else {})}
        got = self.post(req)
        if stream:
            self.assertIn("refusal", got)
            self.assertNotIn("hello from qwen", got)
        else:
            self.assertEqual(got["stop_reason"], "refusal")
        self.assertEqual(self.route_log, [])
        self.assertNotIn(("local-pro", TAG), self.executed)
        self.assertFalse(any(isinstance(s, server.LocalSession) for s in self.reg.sessions.values()))

    def test_thinking_only_max_tokens_keeps_the_refusal(self):
        self.check(([{"type": "thinking", "thinking": "The user asks"}], "max_tokens"))

    def test_empty_content_keeps_the_refusal_json_and_streaming(self):
        self.check(([], "max_tokens"))
        self.check(([], "max_tokens"), stream=True)

    def test_blank_text_or_invalid_tool_calls_keep_the_refusal(self):
        self.check(([{"type": "text", "text": "   "}], "end_turn"))
        self.check(([{"type": "tool_use", "id": "t1", "name": "NotDeclared", "input": {}}], "tool_use"), tools=True)
        self.check(([{"type": "tool_use", "id": "t1", "name": READ_TOOL["name"], "input": "x"}], "tool_use"), tools=True)
        self.check(([{"type": "tool_use", "id": "", "name": READ_TOOL["name"], "input": {}}], "tool_use"), tools=True)

    def test_usable_text_or_valid_tool_call_is_still_taken(self):
        FakeOllama.reply = ([{"type": "thinking", "thinking": "x"}, {"type": "text", "text": "ok"}], "end_turn")
        got = self.post(body("please refuse"))
        self.assertEqual(got["content"], [{"type": "text", "text": "ok"}])
        FakeOllama.reply = ([{"type": "tool_use", "id": "t1", "name": READ_TOOL["name"], "input": {"p": 1}}], "tool_use")
        got = self.post({**body("please refuse"), "tools": [READ_TOOL]})
        self.assertEqual(got["content"][0]["type"], "tool_use")


if __name__ == "__main__":
    unittest.main()
