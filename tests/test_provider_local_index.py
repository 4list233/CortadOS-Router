"""Local models in selection by a published proxy index (local-published-index, owner 2026-11-13): a node model whose
published base model's index (−10 %) clears S1's required index takes the conversation like a cloud model would, the
non-thinking variant first, the thinking variant only when max_tokens leaves room, never outward / blast-radius-3 /
R3+ work, never with a degraded scorer, never on a busy or unavailable node. Fakes only: no Ollama, no socket except
the in-thread fake Ollama on loopback the end-to-end cases reuse from test_provider_local."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core
from cortados_router.provider import local_lane, routing
from tests.test_provider_local import FakeOllama, LocalHarness, TAG, write_config
from tests.test_provider_local_auto import AutoLocalBase, S1_ROUTE, body
from tests.test_provider_sensitive import OUTWARD_TEXT, SECRET_TERM, ContinuationRecheckBase, SensitiveBase, scored
from tests.test_provider_sensitive import write_config as write_sensitive_config
from cortados_router.provider import server
from tests.test_provider import TOKEN

ROOT = Path(__file__).resolve().parent.parent
PROV = "proxy: published base model, −10%"
NON = {"family": "qwen-27b", "local_id": TAG, "thinking": "off", "proxy": True, "provenance": PROV,
       "published": "qwen3-8-27b-non-reasoning", "index": {"general": 16.7, "code": 15.1, "writing": 17.6}}
THINK = {"family": "qwen-27b-think", "local_id": TAG, "thinking": "on", "proxy": True, "provenance": PROV,
         "published": "qwen3-8-27b", "index": {"general": 38.8, "code": 39.6, "writing": 46.3}}
SCORES = {"models": [], "local_models": [NON, THINK]}


def scored_route(req, *, band="R1", use_case="code", outward=False, blast=1, cost=12.5):
    """S1's execution route as select returns it: core.resolve's output under `route`, with the required index, and
    S1's raw answers under `decision` (what the shared gate, routing.route_admission, reads)."""
    answers = {"outward": outward, "work_kind": use_case}
    chosen = {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra", "use_case": use_case, "cost": cost}
    decision = {**S1_ROUTE["decision"], "answers": {**S1_ROUTE["decision"]["answers"], "outward": outward,
                                                    "blast_radius": blast, "work_kind": use_case}}
    return {**S1_ROUTE, "band": band, "decision": decision,
            "route": {**S1_ROUTE["route"], "band": band, "chosen": chosen, "required_index": req, "answers": answers,
                      "features": {"blast_radius": blast}}}


class IndexBase(AutoLocalBase):
    """The auto-route harness with the manual auto_route OFF: only the index route can move a conversation."""

    def setUp(self):
        super().setUp()
        write_config(self.dir, "http://127.0.0.1:9", auto_route=False)
        self._scores = core.model_scores
        core.model_scores = lambda: json.loads(json.dumps(SCORES))
        local_lane._inflight.clear()

    def tearDown(self):
        core.model_scores = self._scores
        local_lane._inflight.clear()
        super().tearDown()

    def route(self, req, max_tokens=64, **kw):
        self.selected = scored_route(req, **{k: v for k, v in kw.items() if k != "extra"})
        return self.choose(body(max_tokens=max_tokens, **(kw.get("extra") or {})))


class VariantChoiceTests(IndexBase):
    def test_non_thinking_variant_takes_what_it_qualifies_for(self):
        r = self.route(14.0)
        self.assertEqual((r["provider"], r["model"]), ("local-pro", TAG))
        self.assertEqual(r["local"]["model"]["variant"], "qwen-27b")
        self.assertEqual(r["local"]["model"]["variant_thinking"], "off")
        (_, _, _, route), = self.logged
        self.assertEqual(route["chosen"]["variant"], "qwen-27b")
        self.assertEqual(route["chosen"]["index"], 15.1)
        self.assertEqual(route["chosen"]["provenance"], PROV)
        self.assertEqual(route["runner_up"]["model"], "gpt-5.6-terra")          # S1's choice, what runs without the node
        self.assertTrue(any(h.startswith("local index route: qwen-27b code index 15.1") for h in route["hard_rules"]))

    def test_lighter_variant_wins_when_both_qualify(self):
        r = self.route(10.0, max_tokens=32000)
        self.assertEqual(r["local"]["model"]["variant"], "qwen-27b")

    def test_think_variant_when_only_it_qualifies_and_max_tokens_leaves_room(self):
        r = self.route(30.0, max_tokens=8192)
        self.assertEqual(r["local"]["model"]["variant"], "qwen-27b-think")
        self.assertEqual(r["local"]["model"]["variant_thinking"], "on")

    def test_think_variant_threshold_is_inclusive_and_below_it_is_cloud(self):
        self.assertEqual(self.route(30.0, max_tokens=local_lane.THINK_MIN_MAX_TOKENS)["local"]["model"]["variant"],
                         "qwen-27b-think")
        self.logged.clear()
        for small in (local_lane.THINK_MIN_MAX_TOKENS - 1, 1024, 20):
            self.assertS1Route(self.route(30.0, max_tokens=small))
        self.assertS1Route(self.choose({k: v for k, v in body().items() if k != "max_tokens"}))

    def test_requirement_above_both_variants_keeps_s1_route_without_a_probe(self):
        self.assertS1Route(self.route(45.0, max_tokens=32000))
        self.assertEqual(self.probes, [])

    def test_qualifies_per_use_case(self):
        self.assertEqual(self.route(45.0, max_tokens=32000, use_case="writing")["local"]["model"]["variant"],
                         "qwen-27b-think")

    def test_client_that_asked_for_thinking_gets_only_the_think_variant(self):
        asked = {"thinking": {"type": "enabled", "budget_tokens": 2000}}
        self.assertEqual(self.route(10.0, max_tokens=8192, extra=asked)["local"]["model"]["variant"], "qwen-27b-think")
        self.logged.clear()
        self.assertS1Route(self.route(10.0, max_tokens=1024, extra=asked))

    def test_reviewers_repro_on_the_index_route_falls_back_to_s1_cloud_route(self):
        # Thinking asked with max_tokens 2048: neither variant qualifies, so the ordinary index route keeps S1's
        # cloud route (no forced-off local serve: that is for sensitive routes only).
        repro = {"thinking": {"type": "enabled", "budget_tokens": 1900}}
        for req in (0.0, 10.0, 30.0):
            self.assertS1Route(self.route(req, max_tokens=2048, band="R0" if req == 0.0 else "R1", extra=repro))
        self.assertEqual(self.probes, [])

    def test_r0_requirement_zero_takes_the_non_thinking_variant(self):
        self.assertEqual(self.route(0.0, band="R0")["local"]["model"]["variant"], "qwen-27b")

    def test_live_tag_with_latest_matches_the_proxy_id(self):
        live = [dict(NON, local_id="Qwen27b_heretic"), dict(THINK, local_id="Qwen27b_heretic")]
        core.model_scores = lambda: {"models": [], "local_models": live}
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["models"][0]["id"] = "Qwen27b_heretic:latest"
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        self.tags = {"Qwen27b_heretic:latest"}
        r = self.route(14.0)
        self.assertEqual((r["model"], r["local"]["model"]["variant"]), ("Qwen27b_heretic:latest", "qwen-27b"))


class HardRuleTests(IndexBase):
    def test_outward_work_never_runs_local(self):
        self.assertS1Route(self.route(0.0, outward=True))
        self.assertEqual(self.probes, [])

    def test_blast_radius_three_never_runs_local(self):
        self.assertS1Route(self.route(0.0, blast=3))
        self.assertEqual(self.probes, [])

    def test_r3_and_r4_never_run_local(self):
        for band in ("R3", "R4"):
            self.assertS1Route(self.route(0.0, band=band))
        self.assertEqual(self.probes, [])

    def test_degraded_scorer_never_runs_local(self):
        self.selected = {**scored_route(0.0), "degraded": True}
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, [])

    def test_unscored_route_has_no_index_to_clear(self):
        self.selected = scored_route(None)
        self.assertS1Route(self.choose())
        self.assertEqual(self.probes, [])

    def test_garbled_or_blast_two_answers_never_run_local(self):
        # The shared gate (routing.local_admission) takes only outward exactly False and blast_radius exactly 0 or 1.
        for outward, blast in ((None, 1), ("false", 1), (False, 2), (False, True), (False, "1"), (False, 1.0)):
            self.assertS1Route(self.route(0.0, outward=outward, blast=blast))
        self.assertEqual(self.probes, [])

    def test_gate_is_applied_at_the_move_itself(self):
        # _move_to_local runs route_admission again: a caller that skipped index_local's early check still cannot move
        # an outward decision.
        node = local_lane.nodes()[0]
        entry = node["models"][0]
        r = scored_route(0.0, outward=True)
        got = routing._move_to_local(core, body(), r, node, entry, client="test", conversation_id="c", rule="test")
        self.assertIs(got, r)
        ok = scored_route(0.0)
        self.assertEqual(routing._move_to_local(core, body(), ok, node, entry, client="test", conversation_id="c",
                                                rule="test")["provider"], "local-pro")

    def test_responses_wire_and_exclusion_keep_s1_route(self):
        self.selected = scored_route(0.0)
        self.assertS1Route(self.choose(body(**{routing.NO_LOCAL_KEY: True})))
        for name in ("qwen-27b", TAG):
            self.assertS1Route(self.choose(exclude_model=name))
        self.assertEqual(self.probes, [])


class AvailabilityTests(IndexBase):
    def test_unavailable_node_falls_back_to_s1_route_and_logs_the_skip(self):
        self.unavailable = "node pro: local gate: local disabled: on battery"
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertS1Route(self.route(14.0))
        self.assertTrue(any("local index route" in m and "unavailable" in m and "on battery" in m for m in cm.output))
        self.assertFalse(any("list the files" in m for m in cm.output))        # metadata only, never the request

    def test_busy_node_is_not_offered_nor_probed(self):
        local_lane._inflight["pro"] = 1
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertS1Route(self.route(14.0))
        self.assertTrue(any("node busy" in m for m in cm.output))
        self.assertEqual(self.probes, [])

    def test_max_inflight_raises_the_busy_bar(self):
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["max_inflight"] = 2
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        local_lane._inflight["pro"] = 1
        self.assertEqual(self.route(14.0)["provider"], "local-pro")

    def test_max_inflight_three_allows_three_concurrent_relays_and_the_fourth_goes_cloud(self):
        # max_inflight is the node's config value (default 1; a benchmark measures the real limit): with 3, the node
        # takes a new conversation while 0, 1 or 2 turns are relayed through local_lane.serve, and not with 3.
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["max_inflight"] = 3
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        node = local_lane.nodes()[0]
        self.assertEqual(node["max_inflight"], 3)
        release, entered = threading.Event(), threading.Semaphore(0)
        real_serve = local_lane._serve

        def held(*a, **k):                                  # a relayed turn that stays in flight until released
            entered.release()
            release.wait(10)
            return "completed"

        local_lane._serve = held
        threads = []
        try:
            for n in range(3):
                self.assertEqual(local_lane.inflight(node), n)
                self.assertEqual(self.route(14.0)["provider"], "local-pro", f"{n} in flight of 3")
                t = threading.Thread(target=local_lane.serve, args=(None, body(), node, node["models"][0]),
                                     kwargs={"echo_model": "cortados"}, daemon=True)
                t.start()
                threads.append(t)
                self.assertTrue(entered.acquire(timeout=5))
            self.assertEqual(local_lane.inflight(node), 3)
            self.logged.clear()                             # the three moves' rows; the fourth must write none
            with self.assertLogs(routing.logger, level="INFO") as cm:
                self.assertS1Route(self.route(14.0))        # the fourth: cloud
            self.assertTrue(any("node busy (3 turn(s) in flight, max_inflight 3)" in m for m in cm.output))
        finally:
            release.set()
            for t in threads:
                t.join(5)
            local_lane._serve = real_serve
        self.assertEqual(local_lane.inflight(node), 0)
        self.assertEqual(self.route(14.0)["provider"], "local-pro")

    def test_model_not_listed_or_too_large_keeps_s1_route(self):
        self.tags = {"phi:latest"}
        self.assertS1Route(self.route(14.0))
        self.tags = {TAG}
        self.selected = scored_route(14.0)
        self.assertS1Route(self.choose(body("x" * (4 * 40000))))

    def test_index_route_off_in_config(self):
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["index_route"] = False
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        self.assertS1Route(self.route(0.0))
        self.assertEqual(self.probes, [])

    def test_passthrough_model_cannot_take_a_variant(self):
        write_config(self.dir, "http://127.0.0.1:9", auto_route=False, thinking="passthrough")
        self.assertS1Route(self.route(0.0, max_tokens=32000))

    def test_no_local_models_in_the_scores_keeps_s1_route(self):
        core.model_scores = lambda: {"models": []}
        self.assertS1Route(self.route(0.0))
        self.assertEqual(self.probes, [])

    def test_bad_config_values_are_refused(self):
        for key, value, word in (("index_route", "yes", "index_route"), ("max_inflight", 0, "max_inflight"),
                                 ("max_inflight", True, "max_inflight")):
            cfg = json.loads(Path(write_config(self.dir, "http://127.0.0.1:9")).read_text())
            cfg["nodes"][0]["ollama"][key] = value
            Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
            with self.assertRaises(local_lane.LocalConfigError) as cm:
                local_lane.nodes()
            self.assertIn(word, str(cm.exception))
            self.selected = scored_route(0.0)
            self.assertS1Route(self.choose())                   # a broken config never blocks S1 routing


class ManualRuleStillWorksTests(IndexBase):
    def test_manual_r0_rule_takes_first_and_index_route_covers_the_rest(self):
        write_config(self.dir, "http://127.0.0.1:9", auto_route={"model": "qwen-27b", "bands": ["R0"]})
        r = self.route(0.0, band="R0")
        self.assertEqual(r["provider"], "local-pro")
        self.assertNotIn("variant", r["local"]["model"])               # the manual rule's entry, as before
        (_, _, _, route), = self.logged
        self.assertTrue(any(h.startswith("local auto-route: band R0") for h in route["hard_rules"]))
        self.logged.clear()
        r = self.route(14.0, band="R1")
        self.assertEqual(r["local"]["model"]["variant"], "qwen-27b")   # R1 is not in its bands: the index route

    def test_manual_rule_without_scores_behaves_as_before(self):
        core.model_scores = lambda: None
        write_config(self.dir, "http://127.0.0.1:9", auto_route={"model": "qwen-27b", "bands": ["R0"]})
        self.selected = dict(S1_ROUTE)
        self.assertEqual(self.choose()["provider"], "local-pro")


class SensitiveVariantTests(SensitiveBase):
    """main's sensitive routes (#109) that pick the local lane run a variant chosen per request by this branch's
    published-index rules (routing.choose_variant): any band, so the index need not clear, but the lighter variant
    that clears it is preferred, else the strongest eligible; the think variant needs room in max_tokens; a client that
    asked for thinking gets only the think variant. The choice reaches the node (local_lane._node_body)."""

    def setUp(self):
        super().setUp()
        self._scores = core.model_scores
        core.model_scores = lambda: json.loads(json.dumps(SCORES))

    def tearDown(self):
        core.model_scores = self._scores
        super().tearDown()

    def scored(self, req, use_case="code"):
        self.selected["route"]["required_index"] = req
        self.selected["route"]["chosen"] = {**self.selected["route"]["chosen"], "use_case": use_case}

    def variant(self, r):
        (_, _, _, route), = self.logged
        self.assertEqual(route["chosen"]["variant"], r["local"]["model"]["variant"])     # recorded as it runs
        self.assertTrue(route["chosen"]["proxy"])
        return r["local"]["model"]["variant"], r["local"]["model"]["variant_thinking"]

    def test_flag_takes_the_lighter_variant_that_clears(self):
        self.scored(10.0)
        r = self.choose(body("hello", max_tokens=32000), local="sensitive")
        self.assertLocal(r, "sensitive: flag")
        self.assertEqual(self.variant(r), ("qwen-27b", "off"))
        self.assertEqual(local_lane._node_body(body("hello", max_tokens=32000), r["local"]["model"])["thinking"],
                         {"type": "disabled"})

    def test_flag_takes_the_think_variant_when_only_it_clears_with_room(self):
        self.scored(30.0)
        r = self.choose(body("hello", max_tokens=8192), local="sensitive")
        self.assertLocal(r, "sensitive: flag")
        self.assertEqual(self.variant(r), ("qwen-27b-think", "on"))
        self.assertEqual(local_lane._node_body(body("hello", max_tokens=8192), r["local"]["model"])["thinking"],
                         {"type": "enabled", "budget_tokens": 4096})

    def test_any_band_runs_local_on_the_strongest_eligible_when_none_clears(self):
        self.scored(60.0)                                                  # R3-level: above both variants
        r = self.choose(body("hello", max_tokens=8192), local="sensitive")
        self.assertLocal(r, "sensitive: flag")
        self.assertEqual(self.variant(r), ("qwen-27b-think", "on"))
        self.logged.clear()
        r = self.choose(body("hello", max_tokens=1024), local="sensitive")  # no room to think: the non-thinking one
        self.assertEqual(self.variant(r), ("qwen-27b", "off"))

    def test_client_that_asked_for_thinking_gets_the_think_variant(self):
        self.scored(10.0)
        asked = body("hello", max_tokens=8192, thinking={"type": "enabled", "budget_tokens": 2000})
        r = self.choose(asked, local="sensitive")
        self.assertEqual(self.variant(r), ("qwen-27b-think", "on"))
        self.assertEqual(local_lane._node_body(asked, r["local"]["model"])["thinking"],
                         {"type": "enabled", "budget_tokens": 2000})         # the client's own thinking passes

    def test_keyword_route_chooses_too(self):
        self.scored(30.0)
        r = self.choose(body(f"about the {SECRET_TERM}", max_tokens=8192))
        self.assertLocal(r, "sensitive: keyword")
        self.assertEqual(self.variant(r), ("qwen-27b-think", "on"))

    def test_refusal_fallback_chooses_and_its_whole_answer_gets_the_variant_thinking(self):
        self.scored(30.0)
        b = body("hello", max_tokens=8192)
        r = self.choose(b)
        sent = []
        moved = routing.refusal_route(core, b, r, lambda m: sent.append(local_lane._node_body(b, m["local"]["model"]))
                                      or True)
        self.assertEqual(moved["bind_reason"], "refusal")
        self.assertEqual(self.variant(moved), ("qwen-27b-think", "on"))
        self.assertEqual(sent[0]["thinking"], {"type": "enabled", "budget_tokens": 4096})

    # Review P2: thinking asked with max_tokens < 4096 rejects both variants; the reviewer's repro went out with the
    # client's thinking unchanged (1900 of 2048 tokens, 148 left for the answer).
    REPRO = {"max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1900}}
    FORCED = "thinking requested but max_tokens < 4096: served without thinking"

    def assertForcedOff(self, r, b, sent=None):
        self.assertEqual(self.variant(r), ("qwen-27b", "off"))
        self.assertTrue(r["local"]["model"]["thinking_forced_off"])
        (_, _, _, route), = self.logged
        self.assertEqual(route["chosen"]["variant_why"], self.FORCED)
        self.assertTrue(route["chosen"]["thinking_forced_off"])
        out = sent if sent is not None else local_lane._node_body(b, r["local"]["model"])
        self.assertEqual(out["thinking"], {"type": "disabled"})
        self.assertEqual(out["max_tokens"], 2048)

    def test_flag_with_thinking_asked_and_no_room_is_served_without_thinking(self):
        self.scored(10.0)
        b = body("hello", **self.REPRO)
        r = self.choose(b, local="sensitive")
        self.assertLocal(r, "sensitive: flag")
        self.assertForcedOff(r, b)

    def test_refusal_with_thinking_asked_and_no_room_is_served_without_thinking(self):
        self.scored(10.0)
        b = body("hello", **self.REPRO)
        r = self.choose(b)
        sent = []
        moved = routing.refusal_route(core, b, r, lambda m: sent.append(local_lane._node_body(b, m["local"]["model"]))
                                      or True)
        self.assertEqual(moved["bind_reason"], "refusal")
        self.assertForcedOff(moved, b, sent[0])

    # Review P2, round 2: the safeguard must not depend on the published scores. Each case below used to send the
    # node {"max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1900}} with the #108 guard off.
    def unvaried_forced_off(self, case, refusal):
        """The REPRO request on a sensitive flag (or a refusal fallback) runs with thinking forced off, no variant
        named, the reason naming `case`; returns the node body."""
        b = body("hello", **self.REPRO)
        if refusal:
            sent = []
            r = routing.refusal_route(core, b, self.choose(b),
                                      lambda m: sent.append(local_lane._node_body(b, m["local"]["model"])) or True)
            self.assertEqual(r["bind_reason"], "refusal")
        else:
            r = self.choose(b, local="sensitive")
            self.assertLocal(r, "sensitive: flag")
        self.assertNotIn("variant", r["local"]["model"])
        self.assertTrue(r["local"]["model"]["thinking_forced_off"])
        (_, _, _, route), = self.logged
        self.assertEqual(route["chosen"]["variant_why"], f"{self.FORCED} ({case})")
        self.assertTrue(route["chosen"]["thinking_forced_off"])
        self.assertNotIn("variant", route["chosen"])
        out = sent[0] if refusal else local_lane._node_body(b, r["local"]["model"])
        self.assertEqual((out["thinking"], out["max_tokens"]), ({"type": "disabled"}, 2048))
        return out

    def passthrough(self):
        write_sensitive_config(self.dir, "http://127.0.0.1:9", sensitive=True, thinking="passthrough")

    def no_variants(self):
        core.model_scores = lambda: {"models": [], "local_models": []}

    def unreadable(self):
        # Unreadable for choose_variant's own read only: routing.choose's earlier reads (served_routes) are another
        # path, and this case is about the safeguard once the move to the local lane is under way.
        def broken():
            if sys._getframe(1).f_code.co_name == "choose_variant":
                raise json.JSONDecodeError("bad", "", 0)
            return json.loads(json.dumps(SCORES))
        core.model_scores = broken

    CASES = {"passthrough": (passthrough, "its node entry is thinking: passthrough, overridden so the answer is not "
                                          "starved"),
             "no variants": (no_variants, "no published variant for this model"),
             "unreadable": (unreadable, "the published scores are unreadable")}

    def test_each_case_on_the_flag_route_goes_out_without_thinking(self):
        for name, (setup, case) in self.CASES.items():
            with self.subTest(name):
                self.scored(10.0)
                self.logged.clear()
                core.model_scores = lambda: json.loads(json.dumps(SCORES))
                setup(self)
                self.unvaried_forced_off(case, refusal=False)
                write_sensitive_config(self.dir, "http://127.0.0.1:9", sensitive=True)

    def test_each_case_on_the_refusal_route_goes_out_without_thinking(self):
        for name, (setup, case) in self.CASES.items():
            with self.subTest(name):
                self.scored(10.0)
                self.logged.clear()
                core.model_scores = lambda: json.loads(json.dumps(SCORES))
                setup(self)
                self.unvaried_forced_off(case, refusal=True)
                write_sensitive_config(self.dir, "http://127.0.0.1:9", sensitive=True)

    def test_passthrough_is_kept_when_there_is_room_or_no_thinking_was_asked(self):
        self.passthrough()
        for b in (body("hello", max_tokens=8192, thinking={"type": "enabled", "budget_tokens": 2000}),
                  body("hello", max_tokens=2048)):
            self.logged.clear()
            r = self.choose(b, local="sensitive")
            self.assertNotIn("thinking_forced_off", r["local"]["model"])
            self.assertEqual(local_lane._node_body(b, r["local"]["model"]).get("thinking"), b.get("thinking"))

    def test_thinking_asked_and_no_room_with_only_a_think_variant_still_goes_out_without_thinking(self):
        self.scored(10.0)
        core.model_scores = lambda: {"models": [], "local_models": [THINK]}
        self.unvaried_forced_off("no published non-thinking variant for this model", refusal=False)

    def test_passthrough_entry_or_no_variants_keeps_the_model_unvaried(self):
        self.scored(10.0)
        core.model_scores = lambda: {"models": [], "local_models": []}
        r = self.choose(body("hello"), local="sensitive")
        self.assertLocal(r, "sensitive: flag")
        self.assertNotIn("variant", r["local"]["model"])
        (_, _, _, route), = self.logged
        self.assertNotIn("variant", route["chosen"])


class NodeBodyTests(unittest.TestCase):
    MODEL = {"id": TAG, "alias": "qwen-27b", "thinking": "off-unless-asked"}

    def test_think_variant_sends_thinking_enabled_with_half_of_max_tokens(self):
        sent = local_lane._node_body({"max_tokens": 32000}, {**self.MODEL, "variant_thinking": "on"})
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 16000})
        sent = local_lane._node_body({"max_tokens": 4096}, {**self.MODEL, "variant_thinking": "on"})
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 2048})

    def test_think_variant_turn_without_room_is_sent_thinking_disabled(self):
        for mt in (4095, 20, None, "8192", True):
            sent = local_lane._node_body({"max_tokens": mt}, {**self.MODEL, "variant_thinking": "on"})
            self.assertEqual(sent["thinking"], {"type": "disabled"}, mt)

    def test_non_thinking_variant_is_sent_thinking_disabled(self):
        sent = local_lane._node_body({"max_tokens": 32000}, {**self.MODEL, "variant_thinking": "off"})
        self.assertEqual(sent["thinking"], {"type": "disabled"})

    def test_a_clients_own_thinking_passes_through(self):
        own = {"type": "enabled", "budget_tokens": 1500}
        for v in ("on", "off"):
            sent = local_lane._node_body({"max_tokens": 32000, "thinking": own}, {**self.MODEL, "variant_thinking": v})
            self.assertEqual(sent["thinking"], own)

    def test_pins_are_unchanged(self):
        self.assertEqual(local_lane._node_body({"max_tokens": 64}, self.MODEL)["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", local_lane._node_body({"max_tokens": 64},
                                                           {**self.MODEL, "thinking": "passthrough"}))


class IndexEndToEndTests(LocalHarness):
    """An S1-routed conversation the index route moves to the think variant is served by the fake Ollama with thinking
    enabled and a budget that fits; the in-flight count is released after the turn."""

    def setUp(self):
        super().setUp()
        self._select, self._scores = routing.execution_routing.select, core.model_scores
        core.model_scores = lambda: json.loads(json.dumps(SCORES))
        # S1's route names the harness's FAKE claude, never codex-cli: were the index route not taken, the endpoint
        # would start that route's backend, and only the claude one is faked here (the cloud fallback itself is
        # covered without a backend above).
        s1 = scored_route(30.0)
        s1.update(provider="claude-cli", model="claude-sonnet-5")
        s1["route"]["chosen"].update(lane="claude", provider="claude-cli", model="claude-sonnet-5")
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
            {**s1, "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        routing._auto_down.clear()
        local_lane._inflight.clear()

    def tearDown(self):
        routing.execution_routing.select, core.model_scores = self._select, self._scores
        routing._auto_down.clear()
        local_lane._inflight.clear()
        super().tearDown()

    def test_think_variant_turn_reaches_the_node_with_thinking_enabled(self):
        FakeOllama.thinker = "ollama"
        got = self.post({**body("What is 6*7?"), "max_tokens": 8192})
        path, sent = FakeOllama.seen[-1]
        self.assertEqual((path, sent["model"]), ("/v1/messages", TAG))
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 4096})
        self.assertEqual(got["content"][-1], {"type": "text", "text": "42"})
        self.assertEqual(local_lane._inflight, {})


class ForcedOffEndToEndTests(ContinuationRecheckBase):
    """Review P2, round 2, end to end: a sensitive turn that asked for thinking with max_tokens < 4096 reaches the fake
    Ollama with thinking disabled whatever the published scores say, and when the node still spends the whole
    max_tokens without an answer, the client gets #108's error (local_lane.thinking_starved), not an empty reply."""

    HEADERS_OPEN = {"x-cortados-local": "sensitive"}
    REPRO = {"max_tokens": 2048, "thinking": {"type": "enabled", "budget_tokens": 1900}}

    def setUp(self):
        super().setUp()
        self._scores = core.model_scores
        core.model_scores = lambda: json.loads(json.dumps(SCORES))

    def tearDown(self):
        core.model_scores = self._scores
        super().tearDown()

    def starved(self, stream=False):
        FakeOllama.reply = ([{"type": "thinking", "thinking": "The user asks"}], "max_tokens")
        req = urllib.request.Request(self.url + "/v1/messages", method="POST",
                                     data=json.dumps({**body("hello", stream=stream), **self.REPRO}).encode(),
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                                              **self.HEADERS_OPEN})
        path, sent = None, None
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
            self.assertTrue(stream, raw)
            events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]
            self.assertEqual(events[-1]["type"], "error")
            msg = events[-1]["error"]["message"]
        except urllib.error.HTTPError as e:
            self.assertFalse(stream)
            self.assertEqual(e.code, 502)
            msg = json.loads(e.read())["error"]["message"]
        path, sent = FakeOllama.seen[-1]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual((sent["thinking"], sent["max_tokens"]), ({"type": "disabled"}, 2048))
        self.assertIn("used the whole max_tokens (2048) without writing an answer (thinking only)", msg)
        self.assertNotIn("The user asks", msg)
        self.assertEqual(self.reg.sessions, {})

    def test_forced_off_turn_with_no_answer_is_the_108_error(self):
        self.starved()
        self.starved(stream=True)

    def test_passthrough_entry_forced_off_turn_with_no_answer_is_the_108_error(self):
        write_sensitive_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", sensitive=True,
                               keywords="", thinking="passthrough")
        self.starved()


class IndexBoundContinuationTests(ContinuationRecheckBase):
    """A conversation the index route moved to the local lane is an AUTOMATIC bind (its route is not `pinned`), so every
    later turn takes local-sensitive's re-check (server.Handler._local_continuation_refused → continuation_admission):
    an outward continuation is refused with the terminal 400 before it reaches the node; a safe one is relayed."""

    BAND = "R1"

    def setUp(self):
        super().setUp()
        # No manual auto_route and no sensitive section: only the index route can bind the conversation locally.
        write_sensitive_config(self.scratch, f"http://127.0.0.1:{self.ollama.server_port}", auto_route=False)
        self._scores = core.model_scores
        core.model_scores = lambda: json.loads(json.dumps(SCORES))
        selected = scored_route(14.0)                     # qwen-27b's code index 15.1 clears it at max_tokens 64
        routing.execution_routing.select = lambda core_, **kw: json.loads(json.dumps(
            {**selected, "route_id": "execution:provider:call:" + uuid.uuid4().hex[:8]}))
        local_lane._inflight.clear()

    def tearDown(self):
        core.model_scores = self._scores
        local_lane._inflight.clear()
        super().tearDown()

    def assertIndexBound(self):
        (s,) = self.reg.sessions.values()
        self.assertIsInstance(s, server.LocalSession)
        route = s.route
        self.assertFalse(route.get("pinned"))
        self.assertEqual(route["local"]["model"]["variant"], "qwen-27b")
        self.assertTrue(route["route"]["chosen"]["proxy"])
        self.assertTrue(any(h.startswith("local index route:") for h in route["route"]["hard_rules"]))

    def test_outward_continuation_of_an_index_bound_conversation_is_refused_json(self):
        self.assertRefusedOutward(stream=False)
        self.assertIndexBound()

    def test_outward_continuation_of_an_index_bound_conversation_is_refused_streaming(self):
        self.assertRefusedOutward(stream=True)
        self.assertIndexBound()

    def test_safe_continuation_of_an_index_bound_conversation_reaches_the_node(self):
        first, got = self.open()
        self.assertIndexBound()
        got2 = self.post(self.follow(first, got, "now list the headings"))
        self.assertEqual(got2["content"][0]["type"], "tool_use")
        self.assertEqual(len(self.node_posts()), 2)
        self.assertEqual(self.scored, [scored("now list the headings", "ok")])   # main: the words + new tool results


class BuilderTests(unittest.TestCase):
    """tables/benchmarks/build_tiers.py: the proxy index is the published base family's index −deduct, with its
    provenance; a base the data does not know is never given a made-up index."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("build_tiers_t", str(ROOT / "tables/benchmarks/build_tiers.py"))
        cls.bt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.bt)

    def proxies(self, variants, overall=None, n_obs=None):
        keep = self.bt.ROUTER
        d = Path(tempfile.mkdtemp())
        (d / "local-models.json").write_text(json.dumps({"models": [], "index_proxies": {"variants": variants}}))
        self.bt.ROUTER = d
        try:
            overall = {"qwen3-8-27b": 1.0, "qwen3-8-27b-non": 0.5} if overall is None else overall
            n_obs = {"qwen3-8-27b": 32, "qwen3-8-27b-non": 3} if n_obs is None else n_obs
            base = {"general": {"qwen3-8-27b": 43.1, "qwen3-8-27b-non": 18.6},
                    "code": {"qwen3-8-27b": 44.0, "qwen3-8-27b-non": 16.8}}
            return self.bt.local_proxies(overall, n_obs, lambda u, f: base[u][f], ["general", "code"], 3)
        finally:
            self.bt.ROUTER = keep

    def test_index_is_base_minus_the_deduction_with_provenance(self):
        ranked, unranked = self.proxies([
            {"variant": "qwen-27b", "id": "Qwen27b_heretic", "thinking": "off", "published": "qwen3-8-27b-non-reasoning",
             "deduct": 0.1},
            {"variant": "qwen-27b-think", "id": "Qwen27b_heretic", "thinking": "on", "published": "qwen3-8-27b",
             "deduct": 0.1}])
        self.assertEqual(unranked, [])
        non, think = ranked
        self.assertEqual((non["published_family"], non["base_index"], non["index"]),
                         ("qwen3-8-27b-non", {"general": 18.6, "code": 16.8}, {"general": 16.7, "code": 15.1}))
        self.assertEqual(think["index"], {"general": 38.8, "code": 39.6})
        for m in ranked:
            self.assertEqual((m["proxy"], m["provenance"], m["ceiling"]), (True, PROV, False))
            self.assertEqual(m["reach"], [{"lane": "local", "provider": "ollama", "model": "Qwen27b_heretic"}])

    def test_unknown_or_thin_base_and_bad_entries_stay_unranked(self):
        cases = [({"published": "qwen3-8-9b"}, "no benchmark scores"),
                 ({"published": "qwen3-8-27b-non-reasoning"}, "benchmarks (< 3)"),
                 ({"deduct": 1.0}, "deduct"), ({"deduct": "10%"}, "deduct"), ({"thinking": "maybe"}, "thinking"),
                 ({"id": ""}, "needs")]
        for change, word in cases:
            v = {"variant": "x", "id": "X", "thinking": "off", "published": "qwen3-8-27b", "deduct": 0.1, **change}
            ranked, unranked = self.proxies([v], n_obs={"qwen3-8-27b": 32, "qwen3-8-27b-non": 2})
            self.assertEqual(ranked, [], change)
            self.assertIn(word, unranked[0]["why"], change)

    def test_family_names_of_the_published_bases(self):
        self.assertEqual(self.bt.family("qwen3-8-27b-non-reasoning"), "qwen3-8-27b-non")
        self.assertEqual(self.bt.family("qwen3.8-27b"), "qwen3-8-27b")

    def test_committed_scores_carry_the_owners_two_variants(self):
        ms = json.loads((ROOT / "tables/model-scores.generated.json").read_text())
        by = {m["family"]: m for m in ms["local_models"]}
        self.assertEqual(set(by), {"qwen-27b", "qwen-27b-think"})
        self.assertEqual((by["qwen-27b"]["published"], by["qwen-27b-think"]["published"]),
                         ("qwen3-8-27b-non-reasoning", "qwen3-8-27b"))
        for m in by.values():
            self.assertEqual((m["local_id"], m["deduct"], m["provenance"]), ("Qwen27b_heretic", 0.1, PROV))
            for u, x in m["base_index"].items():
                self.assertEqual(m["index"][u], round(x * 0.9, 1))
        self.assertNotIn("qwen-27b", {m["family"] for m in ms["models"]})   # no cloud consumer sees a local variant


if __name__ == "__main__":
    unittest.main()
