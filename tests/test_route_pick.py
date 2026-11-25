"""The orchestrator route picker, steps 1-3 (docs/ORCHESTRATOR-ROUTE-PICKER.md §7; owner decisions §8, 2026-11-24):
the parser (routing.route_pick), the selection (routing.choose's model set, E3 escalation, E2 refusals) and the
identity (the pick label in the conversation identity: matching, forks, the fork gate, failures and replays).
Fakes only: a fake model catalogue, fake lane health and S1 decisions, the real endpoint over the fake codex
app-server, throwaway homes. No vendor CLI is opened, no provider is started, nothing reads ~/.cortados."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp(prefix="cortados-route-pick-"))

from cortados_router import core, execution_routing
from cortados_router.provider import anthropic_wire, local_lane, routing, tasks
from cortados_router.provider import server as provider_server
from cortados_router.provider.bridge import BridgeError
from tests import test_provider_codex
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)
from tests.test_provider import EndpointHarness
from tests.test_routing_fixes import ACCOUNTS, FakeSc, dec

BIN = sys.executable                     # a binary that exists: _credentials checks PATH only

# A catalogue around table v23's R3/R4 rows. References: R3 67 / R4 95 for every use case (so Sol 6.1's code 66.2
# misses R3 code and every turn at D 9 needs R4's 95). Nova is a fake non-frontier model clearing R4 code, so the
# frontier rule (not the index) is what it misses. Sonnet and Haiku are below R3; Haiku below the face floor.
_ROWS = (
    # family,           model,                         general, code, lane,    provider,       ceiling, price
    ("claude-opus-5-5", "claude-opus-5-5",              99.8, 100.0, "claude", "claude-cli",   True,   8.0),
    ("claude-sonnet-5", "claude-sonnet-5",              52.8, 47.9, "claude", "claude-cli",    False,  3.0),
    ("claude-haiku-4-5", "claude-haiku-4-5-20251001",   13.3, 14.2, "claude", "claude-cli",    False,  1.0),
    ("gpt-6-1-sol", "gpt-6.1-sol",                      90.1, 66.2, "codex", "openai-codex",   False,  4.0),
    ("gpt-5-6-sol", "gpt-5.6-sol",                      79.6, 67.0, "codex", "openai-codex",   False,  3.0),
    ("gpt-6-astra", "gpt-6-astra",                      100.0, 78.5, "codex", "openai-codex",  True,   20.0),
    ("gpt-7-nova", "gpt-7-nova",                        92.0, 96.0, "codex", "openai-codex",   False,  5.0),
)
MS = {"use_cases": {"code": ""},
      "reference_index": {b: {"general": v, "code": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 67.0), ("R4", 95))},
      "models": [{"family": f, "ceiling": ceil, "proxy": False, "index": {"general": g, "code": c},
                  "price_1m_blended_usd": price, "reach": [{"lane": lane, "provider": prov, "model": model}]}
                 for f, model, g, c, lane, prov, ceil, price in _ROWS]}
OPUS, SOL, ASTRA, NOVA, SONNET = "claude-opus-5-5", "gpt-6.1-sol", "gpt-6-astra", "gpt-7-nova", "claude-sonnet-5"
LOCAL_NODE = {"node": "pro", "lane": "local-pro", "label": "Pro", "base_url": "http://127.0.0.1:9",
              "models": [{"id": "qwen3.8-27b-heretic:q4", "alias": "qwen-27b", "name": "Qwen 27B",
                          "context_length": 131072}]}


def row(lane, ok=True, scarcity=0.0, why=None, reset_epoch=None):
    tight = {"window": "week (all models)", "used_pct": 100 if not ok else 40, "resets_at": "soon",
             **({"reset_epoch": reset_epoch} if reset_epoch else {})}
    return {"ok": ok, "why": why if why else (None if ok else f"week (all models) 100% used, resets soon"),
            "scarcity": 1.0 if not ok else scarcity, "tightest": tight, "expiring_pct": 0, "pools": {}}


class Catalogue(unittest.TestCase):
    """The fake catalogue on cortados_router.core (the module routing and the server use), no local config, no API
    lane unless a test offers it, a clean switching-margin memory."""

    def setUp(self):
        keep = {k: getattr(core, k) for k in ("_sc_mod", "model_scores", "lane_health", "_vendor_accounts",
                                              "decide", "log_route")}
        self.addCleanup(lambda: [setattr(core, k, v) for k, v in keep.items()])
        core.model_scores = lambda: MS
        core._vendor_accounts = lambda *a: [dict(x) for x in ACCOUNTS]
        core._sc_mod = FakeSc({}, {})
        self.health = {"claude": row("claude"), "claude#b": row("claude#b"), "codex": row("codex")}
        core.lane_health = lambda **k: self.health
        self.logged = []
        core.log_route = lambda rid, meta, decision, route: self.logged.append((rid, meta, route)) or True
        self.local_nodes = []
        p = mock.patch.object(local_lane, "nodes", lambda path=None: [dict(n) for n in self.local_nodes])
        p.start(); self.addCleanup(p.stop)
        self.api = []
        p = mock.patch.object(routing, "api_lane_pairs", lambda core_, body, served, **k: list(self.api))
        p.start(); self.addCleanup(p.stop)
        routing.forget_routes()
        self.addCleanup(routing.forget_routes)

    def pick(self, text):
        return routing.resolve_pick(core, text)


# ----------------------------------------------------------------------------------------------------- step 1: parser
class ParserTests(Catalogue):

    def test_window_only_picks_are_todays_window(self):
        for text, label, window in (("R3", "R3", ("R3", "R3")), ("r3", "R3", ("R3", "R3")), ("R3-R3", "R3", ("R3", "R3")),
                                    (" r1-r2 ", "R1-R2", ("R1", "R2"))):
            with self.subTest(text=text):
                p = self.pick(text)
                self.assertEqual((p.label, p.window, p.restricts), (label, window, False))
        self.assertIsNone(self.pick("any"))
        self.assertIsNone(self.pick("ANY"))

    def test_every_grammar_case(self):
        cases = {
            "R3:*": ("R3:*", ("R3", "R3"), (), True, False),
            "r4:*": ("R4:*", ("R4", "R4"), (), True, False),
            "R2-R3:*": ("R2-R3:*", ("R2", "R3"), (), True, False),
            f"R3:{OPUS}": (f"R3:{OPUS}", ("R3", "R3"), (OPUS,), False, False),
            f"R3:{OPUS}+{SOL}": (f"R3:{OPUS}+{SOL}", ("R3", "R3"), (OPUS, SOL), False, False),
            f"any:{SOL}": (f"any:{SOL}", None, (SOL,), False, False),
            "local": ("local", None, (), False, True),
            "local:qwen-27b": ("local:qwen-27b", None, ("qwen-27b",), False, True),
        }
        self.local_nodes = [LOCAL_NODE]
        for text, (label, window, models, star, local) in cases.items():
            with self.subTest(text=text):
                p = self.pick(text)
                self.assertEqual((p.label, p.window, p.models, p.star, p.local), (label, window, models, star, local))
                self.assertTrue(p.restricts)

    def test_case_order_duplicates_and_friendly_names_give_one_label(self):
        want = f"R3:{OPUS}+{SOL}"
        for text in (f"R3:{OPUS}+{SOL}", f"r3:{SOL}+{OPUS}", f"R3:CLAUDE-OPUS-5-5+GPT-6.1-SOL", f"R3:opus+gpt-6.1",
                     f"R3:{SOL}+opus+{OPUS}", f"R3-r3:Opus+{SOL}+{SOL}"):
            with self.subTest(text=text):
                self.assertEqual(self.pick(text).label, want)
        self.assertEqual(self.pick(f"R3:{SOL}+opus").models, (SOL, OPUS))         # the order as given is kept
        # release date optional; the label carries the served id
        self.assertEqual(self.pick("R2:claude-haiku-4-5").label, "R2:claude-haiku-4-5-20251001")
        self.assertEqual(self.pick("R2:haiku").label, "R2:claude-haiku-4-5-20251001")

    def test_refusals(self):
        self.local_nodes = [LOCAL_NODE]
        bad = {
            "R5:opus": "not a band window",
            "R3-R1:opus": "not a band window",
            ":opus": "not a band window",
            "R3:": "not a model name",
            "R3:+": "not a model name",
            f"R3:{OPUS}++{SOL}": "not a model name",
            "R3:op us": "not a model name",
            "R3:opus;rm": "not a model name",
            "any:*": "plain `cortados`",
            "local:*": "name the local models",
            "R3:" + "+".join(["opus"] * 9): "at most 8 models",
            "R3:" + "a" * 130: "at most 128",
            "R3:sol": "ambiguous",
            "R3:nonesuch-9": "not a model this endpoint serves",
            "R3:qwen-27b": "local model",
            "local:opus": "cloud model",
            "local:nonesuch": "not a local model",
            "R3:" + "a" * 65: "not a model name",
        }
        for text, why in bad.items():
            with self.subTest(text=text), self.assertRaises(routing.PickError) as ctx:
                self.pick(text)
            self.assertIn(why, str(ctx.exception))

    def test_a_broken_local_config_is_said_as_such(self):
        def broken(path=None):
            raise local_lane.LocalConfigError("bad json at /secret/path", "pro")
        with mock.patch.object(local_lane, "nodes", broken), self.assertRaises(routing.PickError) as ctx:
            self.pick("local:qwen-27b")
        self.assertIn("misconfigured", str(ctx.exception))
        self.assertNotIn("/secret/path", str(ctx.exception))

    def test_sources_header_band_and_model(self):
        rp = lambda h, b, m: routing.route_pick(core, h, b, m)
        self.assertEqual(rp(None, None, "cortados"), (None, None))                           # a subagent
        self.assertEqual(rp(None, None, "sub/claude-haiku-4-5"), (None, None))              # a side call
        self.assertEqual(rp(None, None, f"cortados@R3:{SOL}+opus")[0].label, f"R3:{OPUS}+{SOL}")
        self.assertEqual(rp(f"R3:opus+{SOL}", None, "cortados")[0].label, f"R3:{OPUS}+{SOL}")   # the header alone
        self.assertEqual(rp(f"R3:opus+{SOL}", None, f"cortados@r3:{SOL}+{OPUS}")[0].label, f"R3:{OPUS}+{SOL}")
        self.assertEqual(rp(None, "R3", f"cortados@R3:{SOL}")[0].label, f"R3:{SOL}")        # the band agrees
        self.assertEqual(rp(None, "r2", "cortados")[0].label, "R2")                          # today's header
        self.assertEqual(rp(None, None, "cortados@R1-R2")[0].label, "R1-R2")                 # today's model name
        for h, b, m, why in ((f"R3:{SOL}", None, f"cortados@R3:{OPUS}", "different route picks"),
                             (None, "R2", f"cortados@R3:{SOL}", "different band windows"),
                             (None, "any", f"cortados@R3:{SOL}", "different band windows"),
                             (None, "R2", f"cortados@any:{SOL}", "different band windows"),
                             (None, "R2", "cortados@R3", "different band windows"),
                             (None, "R9", "cortados", "not a band window"),
                             ("R3:nonesuch-9", None, "cortados", "x-cortados-pick:"),
                             (None, None, "cortados@NOT-A-BAND", "not a band window")):
            with self.subTest(h=h, b=b, m=m):
                pick, err = rp(h, b, m)
                self.assertIsNone(pick)
                self.assertIn(why, err)

    def test_existing_band_window_reading_is_unchanged(self):
        # routing.band_window (kept for its callers) and route_pick read a window-only request the same way
        for h, m in (("r2", "cortados"), (None, "cortados@R1-R2"), ("R2", "cortados@R2"), ("any", "cortados")):
            with self.subTest(h=h, m=m):
                w, err = routing.band_window(h, m)
                p, perr = routing.route_pick(core, None, h, m)
                self.assertEqual((w, err, perr), (p.window if p else None, None, None))


# -------------------------------------------------------------------------------------------------- step 2: selection
class SelectionTests(Catalogue):

    def choose(self, pick_text, d=None, **kw):
        core.decide = lambda *a, **k: dict(d or dec())
        pick = self.pick(pick_text) if pick_text else None
        return routing.choose(core, {"model": f"cortados@{pick_text}", "messages": [{"role": "user", "content":
                              f"task {uuid.uuid4().hex}"}]}, client="claude-cli",
                              conversation_id=uuid.uuid4().hex, claude_bin=BIN, codex_bin=BIN,
                              **({"band_window": pick.window} if pick and pick.window else {}),
                              **({"route_pick": pick} if pick else {}), **kw)

    def spy_select(self):
        seen, real = [], execution_routing.select

        def spy(core_, **kw):
            seen.append({**kw["constraints"], "allowed_routes": sorted(tuple(p) for p in
                                                                         kw["constraints"]["allowed_routes"])})
            return real(core_, **kw)
        p = mock.patch.object(execution_routing, "select", spy)
        p.start(); self.addCleanup(p.stop)
        return seen

    def rules(self):
        return self.logged[-1][2]["hard_rules"]

    def test_a_selected_model_is_offered_on_every_account_and_lane(self):
        self.api = [("anthropic-api", OPUS), ("anthropic-api", SONNET)]
        seen = []
        with mock.patch.object(execution_routing, "select",
                               lambda core_, **kw: seen.append(kw["constraints"]) or {"error": "stop here"}):
            self.choose(f"R3:{OPUS}")
        self.assertEqual(sorted(tuple(p) for p in seen[0]["allowed_routes"]),
                         [("anthropic-api", OPUS), ("claude-cli", OPUS), ("claude-cli#b", OPUS)])
        self.assertEqual(seen[0]["band_window"], ("R3", "R3"))

    def test_scarcity_picks_account_b_when_a_is_hot(self):
        self.health["claude"] = row("claude", scarcity=0.9)
        self.health["claude#b"] = row("claude#b", scarcity=-0.5)
        r = self.choose(f"R3:{OPUS}")
        self.assertEqual((r["provider"], r["model"]), ("claude-cli#b", OPUS))
        self.assertIn(f"route pick R3:{OPUS}: 1 model, 2 routes", self.rules())
        self.assertIn("route pick: local cost routes not taken", self.rules())
        self.assertNotIn(routing.PICK_ESCALATED_KEY, r)

    def test_scarcity_ranks_across_the_selected_models(self):
        r = self.choose(f"R3:{OPUS}+{ASTRA}")
        self.assertIn(r["model"], (OPUS, ASTRA))
        self.assertNotIn(r["model"], (SOL, SONNET, NOVA))

    def test_every_selected_route_spent_refuses_naming_each_and_does_not_escalate(self):
        n = time.time()
        self.health["claude"] = row("claude", ok=False, reset_epoch=n + 7200)
        self.health["claude#b"] = row("claude#b", ok=False, reset_epoch=n + 3600)
        seen = self.spy_select()
        r = self.choose(f"R3:{OPUS}")
        self.assertTrue(r["quota_blocked"])
        self.assertTrue(r["pick_refused"])
        self.assertFalse(r.get("client_error"))
        for route in (f"claude-cli/{OPUS}", f"claude-cli#b/{OPUS}"):
            self.assertIn(route, r["error"])
        self.assertIn("Earliest reset: " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(n + 3600)), r["error"])
        self.assertIn("`/model cortados@R3:*`", r["error"])
        self.assertIn("Nothing was run on a model outside the pick", r["error"])
        self.assertEqual(len(seen), 1)                                  # never re-routed without the pick
        self.assertNotIn(routing.PICK_ESCALATED_KEY, r)
        self.assertEqual(self.logged, [])                               # nothing chosen, no routes row

    def test_an_exhausted_selection_refuses_even_when_unselected_models_are_free(self):
        # E2 is not E3: the selected model clears the requirement, so its exhaustion refuses; Astra (free) is not used
        self.health["claude"] = row("claude", ok=False)
        self.health["claude#b"] = row("claude#b", ok=False)
        r = self.choose(f"R3:{OPUS}", d=dec(steps=3, ambiguity=3, novelty=3))      # D 9: needs 95, Opus has 100
        self.assertTrue(r["quota_blocked"])
        self.assertNotIn("provider", r)

    def test_every_route_too_large_is_a_fit_refusal(self):
        big = {"model": f"cortados@R3:{OPUS}", "messages": [{"role": "user", "content": "x" * 1_200_000}]}
        core.decide = lambda *a, **k: dec()
        r = routing.choose(core, big, client="claude-cli", conversation_id="c-fit", claude_bin=BIN, codex_bin=BIN,
                           band_window=("R3", "R3"), route_pick=self.pick(f"R3:{OPUS}"))
        self.assertTrue(r.get("fit_blocked"), r)
        self.assertNotIn(routing.PICK_ESCALATED_KEY, r)
        self.assertIn("exceeds", r["error"])

    def test_a_missing_binary_is_a_dependency_failure(self):
        self.health["codex"] = row("codex")
        core.decide = lambda *a, **k: dec()
        r = routing.choose(core, {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, client="claude-cli",
                           conversation_id="c-bin", claude_bin=BIN, codex_bin="/nonexistent/codex",
                           band_window=("R3", "R3"), route_pick=self.pick(f"R3:{NOVA}"))   # Nova clears R3
        self.assertTrue(r["pick_refused"], r)
        self.assertFalse(r.get("quota_blocked") or r.get("fit_blocked") or r.get("client_error"))   # → 503
        self.assertIn("not on PATH", r["error"])

    def test_degraded_scorer_and_a_claude_only_pick_refuses_naming_the_rule(self):
        degraded = {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "timeout"}
        r = self.choose(f"R3:{OPUS}", d=degraded)
        self.assertTrue(r["degraded"])
        self.assertIn("a degraded scorer never defaults to Claude", r["error"])
        self.assertIn("every selected model is a Claude model", r["error"])
        self.assertFalse(r.get("quota_blocked") or r.get("fit_blocked") or r.get("client_error"))   # → 503

    def test_degraded_scorer_and_a_mixed_pick_runs_on_the_codex_member(self):
        degraded = {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "timeout"}
        r = self.choose(f"R3:{OPUS}+{SOL}", d=degraded)
        self.assertEqual((r["provider"], r["model"]), ("codex-cli", SOL))

    def test_an_outward_turn_keeps_its_r3_floor_within_the_set(self):
        r = self.choose(f"R2:{OPUS}+{SONNET}", d=dec(steps=1, ambiguity=0, novelty=0, outward=True))
        self.assertEqual(r["band"], "R3")
        self.assertIn(r["model"], (OPUS, SONNET))
        self.assertIn("band window R2 overridden: blast radius 3 (outward effect) keeps R3", self.rules())

    def test_star_never_yields_a_model_below_the_band_reference(self):
        self.assertEqual(routing.pick_star_models(core, ("R4", "R4")), (OPUS, ASTRA))
        self.assertEqual(routing.pick_star_models(core, ("R3", "R3")), tuple(sorted((OPUS, SOL, ASTRA, NOVA, "gpt-5.6-sol"))))
        self.assertEqual(len(routing.pick_star_models(core, ("R0", "R0"))), len(_ROWS))
        for d in (dec(steps=1, ambiguity=0, novelty=0), dec(), dec(steps=3, ambiguity=3, novelty=3)):
            with self.subTest(d=d["answers"]):
                r = self.choose("R4:*", d=d)
                self.assertIn(r["model"], (OPUS, ASTRA))
        self.assertTrue(any(x.startswith(f"route pick R4:*: 2 models, 3 routes ({OPUS}, {ASTRA})")
                            for x in self.rules()))

    def test_star_with_unreadable_scores_is_a_dependency_failure(self):
        core.model_scores = lambda: None
        r = routing.choose(core, {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, client="c",
                           conversation_id="c-star", claude_bin=BIN, codex_bin=BIN, band_window=("R4", "R4"),
                           route_pick=routing.Pick(("R4", "R4"), star=True, label="R4:*"))
        self.assertIn("cannot be resolved", r["error"])
        self.assertFalse(r.get("client_error"))

    def test_an_exclusion_that_empties_the_set_is_a_400(self):
        r = self.choose(f"R3:{OPUS}", exclude_model=OPUS)
        self.assertTrue(r["client_error"])
        self.assertIn("no route serves", r["error"])
        self.assertIn("x-cortados-exclude-model", r["error"])
        # an exclusion that leaves a selected model keeps the pick on it
        r = self.choose(f"R3:{OPUS}+{ASTRA}", exclude_model=OPUS)
        self.assertEqual(r["model"], ASTRA)

    def test_local_picks_are_refused_until_step_8(self):
        self.local_nodes = [LOCAL_NODE]
        for text in ("local", "local:qwen-27b"):
            with self.subTest(text=text):
                r = self.choose(text)
                self.assertTrue(r["client_error"])
                self.assertIn("not enabled yet", r["error"])

    def test_cost_moves_to_local_are_skipped_and_the_sensitive_rule_still_wins(self):
        with mock.patch.object(routing, "auto_local", side_effect=AssertionError("auto_local under a pick")):
            r = self.choose(f"R3:{SOL}+{OPUS}")
        self.assertIn(r["model"], (SOL, OPUS))

        def moved(core_, body, r, **kw):
            out = {**r, "local": {"node": LOCAL_NODE, "model": LOCAL_NODE["models"][0]},
                   "route": {**r["route"], "hard_rules": list(r["route"]["hard_rules"]) + ["sensitive: moved"]}}
            return out
        with mock.patch.object(routing, "sensitive_local", moved):
            r = self.choose(f"R3:{SOL}")
        self.assertTrue(r.get("local"))
        self.assertIn(f"route pick R3:{SOL} overridden: sensitive", self.rules())

    def test_a_pin_ignores_the_pick(self):
        with mock.patch.object(routing, "choose_pinned", return_value={"model": OPUS}) as pinned, \
                mock.patch.object(execution_routing, "select") as sel:
            routing.choose(core, {"model": "sub/opus", "messages": []}, client="claude-cli", conversation_id="p",
                           route_pick=self.pick(f"R3:{SOL}"), band_window=("R3", "R3"))
        sel.assert_not_called(); pinned.assert_called_once()

    def test_a_pick_route_is_not_remembered_for_the_switching_margin(self):
        self.choose(f"R3:{OPUS}")
        self.assertIsNone(routing.previous_routes("claude-cli"))


# --------------------------------------------------------------------------------------------- E3: escalation
class EscalationTests(SelectionTests):
    """E3 (owner 2026-11-24): a turn whose requirement exceeds every selected model escalates to the route normal
    routing chooses without the model set; it is recorded and the client is told."""

    locals().update({n: None for n in list(vars(SelectionTests)) if n.startswith("test_")})

    def test_a_high_turn_under_r3_sol_escalates_and_is_recorded(self):
        seen = self.spy_select()
        r = self.choose(f"R3:{SOL}", d=dec(steps=3, ambiguity=3, novelty=3))   # code D 9: needs 95, Sol has 66.2
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["allowed_routes"], [("codex-cli", SOL)])
        self.assertIn(("claude-cli", OPUS), seen[1]["allowed_routes"])         # every served model
        self.assertNotIn(r["model"], (SOL,))
        self.assertGreaterEqual(dict((m[1], m[3]) for m in _ROWS)[r["model"]], 95)
        esc = r[routing.PICK_ESCALATED_KEY]
        self.assertEqual((esc["label"], esc["reason"], esc["required"]), (f"R3:{SOL}", "index", 95.0))
        self.assertTrue(any(x.startswith("route pick escalated: no selected model reaches 95") for x in self.rules()))
        self.assertEqual(len({rid for rid, *_ in self.logged}), 1)             # one routes row (the escalated one)
        self.assertEqual(r["band_window"], "R3")                               # the window stays; the set is dropped

    def test_an_outward_turn_whose_requirement_outgrows_the_pick_escalates(self):
        # an outward effect floors blast radius at 3; with high novelty that is a D the selection cannot reach
        r = self.choose(f"R2:{SONNET}", d=dec(steps=2, ambiguity=1, novelty=2, outward=True))
        route = self.logged[-1][2]
        self.assertEqual(r["band"], "R3")
        self.assertGreater(route["required_index"], 47.9)
        self.assertEqual(r[routing.PICK_ESCALATED_KEY]["reason"], "index")
        self.assertNotEqual(r["model"], SONNET)

    def test_the_frontier_rule_escalates_a_pick_with_no_frontier_model(self):
        r = self.choose(f"R3:{NOVA}", d=dec(steps=3, ambiguity=3, novelty=3))  # Nova clears 95 on code, not frontier
        self.assertEqual(r[routing.PICK_ESCALATED_KEY]["reason"], "frontier")
        self.assertIn(r["model"], (OPUS, ASTRA))
        self.assertTrue(any(x.startswith("route pick escalated: no selected model reaches the frontier requirement")
                            for x in self.rules()))

    def test_escalation_is_on_capability_not_availability(self):
        # the selected model clears the turn: it is spent, so E2 refuses (above); here a capable selection runs as is
        r = self.choose(f"R3:{OPUS}+{SOL}", d=dec(steps=3, ambiguity=3, novelty=3))
        self.assertEqual(r["model"], OPUS)
        self.assertNotIn(routing.PICK_ESCALATED_KEY, r)

    def test_an_escalation_whose_selection_is_also_spent_still_escalates(self):
        # capability first: the selection could not have served the turn even with room, so it escalates
        self.health["codex"] = row("codex", ok=False)
        r = self.choose(f"R3:{SOL}", d=dec(steps=3, ambiguity=3, novelty=3))
        self.assertEqual(r["model"], OPUS)
        self.assertEqual(r[routing.PICK_ESCALATED_KEY]["reason"], "index")

    def test_escalation_leaves_the_api_lane_out(self):
        self.api = [("anthropic-api", OPUS)]
        seen = self.spy_select()
        self.choose(f"R3:{SOL}", d=dec(steps=3, ambiguity=3, novelty=3))
        self.assertNotIn(("anthropic-api", OPUS), seen[1]["allowed_routes"])

    def test_the_notice(self):
        n = routing.pick_notice({"label": f"R3:{SOL}", "reason": "index", "required": 95.0}, OPUS)
        self.assertEqual(n, {"type": "text", "text": f"⚠ CortadOS: route pick R3:{SOL} escalated: the turn needs a "
                                                     f"higher index than any selected model has, so this conversation "
                                                     f"runs on {OPUS}."})
        self.assertIn("frontier model", routing.pick_notice({"label": "R4:*", "reason": "frontier"}, OPUS)["text"])
        for bad in (None, {}, {"label": "R3:<b>", "reason": "index"}, {"label": "R3:x", "reason": "free text"},
                    {"label": "R3\nforged", "reason": "index"}):
            with self.subTest(bad=bad):
                self.assertIsNone(routing.pick_notice(bad, OPUS))
        self.assertIn("a stronger model", routing.pick_notice({"label": "R3:x", "reason": "index"}, "Opus\n!")["text"])

    def test_tasks_view_labels(self):
        self.assertEqual(tasks._rule_label("route pick escalated: no selected model reaches 95 (code; …)"),
                         "route pick escalated")
        # Step 5: without table ids (no `ids`) a label's models are withheld; with them the label is shown
        self.assertEqual(tasks._rule_label(f"route pick R3:{OPUS}+{SOL}: 2 models, 4 routes"), "route pick (2 models)")
        self.assertEqual(tasks._rule_label(f"route pick R3:{OPUS}+{SOL}: 2 models, 4 routes", frozenset({OPUS, SOL})),
                         f"route pick R3:{OPUS}+{SOL} (2 models)")
        self.assertEqual(tasks._rule_label("route pick R4:*: 2 models, 3 routes (a, b)"), "route pick R4:* (2 models)")
        self.assertEqual(tasks._rule_label(f"route pick R3:{SOL} overridden: sensitive"), "route pick overridden")
        self.assertEqual(tasks._rule_label("route pick: local cost routes not taken"),
                         "route pick: local cost route not taken")
        self.assertIsNone(tasks._rule_label("route pick <script>: x"))


# --------------------------------------------------------------------------------------------- step 3: identity
_CODEX = test_provider_codex.CodexEndpointTests


class IdentityTests(unittest.TestCase):
    """The pick label is the conversation identity the band window's label was (server._in_window, the failure key, the
    fork gate, replays). The real endpoint over the fake codex app-server; the router is a stub returning a codex route
    and recording the pick it was given."""

    IDENT = {"x-claude-code-session-id": "s-route-pick"}
    tearDown, request, post, error_of, send_with, continuation = (
        _CODEX.tearDown, _CODEX.request, _CODEX.post, _CODEX.error_of, _CODEX.send_with, _CODEX.continuation)

    def setUp(self):
        test_provider_codex.CodexEndpointTests.setUp(self)
        keep = (core.model_scores, core._vendor_accounts)
        self.addCleanup(lambda: (setattr(core, "model_scores", keep[0]), setattr(core, "_vendor_accounts", keep[1])))
        core.model_scores = lambda: MS
        core._vendor_accounts = lambda *a: [dict(x) for x in ACCOUNTS]
        p = mock.patch.object(local_lane, "nodes", lambda path=None: [])
        p.start(); self.addCleanup(p.stop)
        self.routed, self.escalate = [], False

        def router(body, client, **kw):
            pick, w = kw.get("route_pick"), kw.get("band_window")
            self.routed.append(pick.label if pick else (core.band_window_label(*w) if w else None))
            if routing.sub_pin(body):
                return dict(test_provider_codex.CODEX_ROUTE, model=routing.sub_pin(body), band="pin", pinned=True)
            out = dict(test_provider_codex.CODEX_ROUTE)
            if self.escalate and pick is not None:
                out[routing.PICK_ESCALATED_KEY] = {"label": pick.label, "reason": "index", "required": 95.0}
            return out
        self.reg.router = router

    def ok(self, body, headers=None):
        code, got = self.send_with(body, dict(self.IDENT, **(headers or {})))
        self.assertEqual(code, 200, got)
        return got

    def opened(self, model):
        first = {"model": model, "tools": [test_provider_codex.READ_TOOL],
                 "messages": [{"role": "user", "content": "please use tool"}]}
        r1 = self.ok(first)
        use = r1["content"][-1]
        nxt = dict(first, messages=first["messages"] + [
            {"role": "assistant", "content": r1["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}])
        return nxt, r1

    def labels(self):
        return sorted((s.route or {}).get(provider_server.ASKED_WINDOW_KEY) or "none"
                      for s in self.reg.sessions.values())

    def by_label(self, label):
        [s] = [s for s in self.reg.sessions.values()
               if (s.route or {}).get(provider_server.ASKED_WINDOW_KEY) == label]
        return s

    def test_the_label_is_stored_and_the_router_gets_the_pick(self):
        self.opened(f"cortados@R3:{SOL}+opus")
        self.assertEqual(self.routed, [f"R3:{OPUS}+{SOL}"])
        self.assertEqual(self.labels(), [f"R3:{OPUS}+{SOL}"])

    def test_spellings_of_one_pick_continue_one_conversation(self):
        body, _ = self.opened(f"cortados@R3:{OPUS}+{SOL}")
        self.ok(dict(body, model=f"cortados@r3:{SOL}+{OPUS}"))
        self.ok(dict(body, model="cortados"), {routing.PICK_HEADER: f"R3:opus+{SOL}"})
        self.assertEqual((self.routed, len(self.reg.sessions)), ([f"R3:{OPUS}+{SOL}"], 1))

    def test_a_pick_change_forks_with_the_inherited_history(self):
        body, _ = self.opened(f"cortados@R3:{OPUS}")
        self.ok(dict(body, model=f"cortados@R3:{SOL}"))
        self.assertEqual(self.routed, [f"R3:{OPUS}", f"R3:{SOL}"])
        self.assertEqual(self.labels(), [f"R3:{OPUS}", f"R3:{SOL}"])
        parent, child = self.by_label(f"R3:{OPUS}"), self.by_label(f"R3:{SOL}")
        self.assertEqual(len(parent.history), 1)                         # the parent is untouched
        self.assertEqual(len(child.history), 2)                          # the child holds the inherited history
        self.assertFalse(getattr(child, "rebuilds", 0))

    def test_window_and_pick_are_distinct_identities(self):
        for first, then in (("cortados@R3", f"cortados@R3:{SOL}"), (f"cortados@R3:{SOL}", "cortados@R3"),
                            ("cortados@R3:*", "cortados@R3"), (f"cortados@R3:{SOL}", f"cortados@any:{SOL}")):
            with self.subTest(first=first, then=then):
                for s in list(self.reg.sessions.values()):
                    self.reg.drop(s)
                self.routed.clear()
                body, _ = self.opened(first)
                self.ok(dict(body, model=then))
                self.assertEqual((len(self.routed), len(self.reg.sessions)), (2, 2))

    def test_r3_conversations_keep_their_identity(self):
        # a conversation opened under today's window carries the label `R3`; the same window as a model name or as a
        # pick header continues it
        body, _ = self.opened("cortados@R3")
        self.assertEqual(self.labels(), ["R3"])
        self.ok(dict(body, model="cortados@r3-R3"))
        self.ok(dict(body, model="cortados"), {routing.PICK_HEADER: "R3"})
        self.ok(dict(body, model="cortados"), {routing.BAND_HEADER: "R3"})
        self.assertEqual((self.routed, len(self.reg.sessions)), (["R3"], 1))

    def test_a_pin_is_in_every_pick(self):
        body = self.continuation("sub/opus", "sub/opus")
        self.assertEqual(self.send_with(body, {routing.PICK_HEADER: f"R3:{SOL}"})[0], 200)
        self.assertEqual(len(self.reg.sessions), 1)

    def test_an_invalid_pick_on_a_continuation_is_refused_before_lookup(self):
        body, _ = self.opened(f"cortados@R3:{SOL}")
        code, err = self.send_with(dict(body, model="cortados@R3:nonesuch-9"), self.IDENT)
        self.assertEqual(code, 400); self.assertIn("not a model this endpoint serves", err["message"])
        code, err = self.send_with(dict(body, model=f"cortados@R3:{SOL}"), dict(self.IDENT, **{routing.BAND_HEADER: "R2"}))
        self.assertEqual(code, 400); self.assertIn("different band windows", err["message"])
        self.assertEqual((self.routed, len(self.reg.sessions)), ([f"R3:{SOL}"], 1))

    def test_concurrent_identical_pick_changes_open_one_conversation(self):
        body, _ = self.opened(f"cortados@R3:{OPUS}")
        changed = dict(body, model=f"cortados@R3:{SOL}")
        real_new, entered, release, starts, out = self.reg._new, threading.Event(), threading.Event(), [], {}

        def slow_new(*a, **kw):
            starts.append(1)
            if len(starts) == 1:
                entered.set()
                release.wait(10)
            return real_new(*a, **kw)

        with mock.patch.object(self.reg, "_new", side_effect=slow_new):
            a = threading.Thread(target=lambda: out.update(a=self.send_with(changed, self.IDENT)))
            a.start()
            self.assertTrue(entered.wait(10))
            b = threading.Thread(target=lambda: out.update(b=self.send_with(changed, self.IDENT)))
            b.start()
            deadline = time.time() + 10
            while not (any(g[1] == 2 for g in self.reg._rebuilding.values()) or len(starts) > 1):
                self.assertLess(time.time(), deadline, "the second request never reached the gate")
                time.sleep(0.01)
            release.set()
            a.join(30)
            b.join(30)
        self.assertEqual(len(starts), 1, out)
        self.assertEqual(self.labels(), [f"R3:{OPUS}", f"R3:{SOL}"])
        self.assertEqual(self.routed, [f"R3:{OPUS}", f"R3:{SOL}"])
        served = next(r for code, r in out.values() if code == 200)
        for code, r in out.values():
            self.assertTrue((code, r) == (200, served) or code == 409, out)
        self.assertEqual(self.ok(changed), served)                       # its retry is the replay

    def test_a_failure_belongs_to_its_pick(self):
        body, _ = self.opened(f"cortados@R3:{OPUS}")
        self.ok(dict(body, model=f"cortados@R3:{SOL}"))                   # the other pick advances
        first = self.by_label(f"R3:{OPUS}")
        with mock.patch.object(first, "run_turn", side_effect=BridgeError("codex backend: the turn failed")):
            with self.assertLogs("cortados.provider", "WARNING"):
                self.assertEqual(self.send_with(body, self.IDENT)[0], 502)
        code, err = self.send_with(body, self.IDENT)                     # its retry: the failure, never a new fork
        self.assertEqual(code, 400, err)
        self.assertIn("failed at", err["message"])
        self.assertEqual(self.labels(), [f"R3:{SOL}"])
        self.assertEqual(self.routed, [f"R3:{OPUS}", f"R3:{SOL}"])

    def test_a_reply_is_never_replayed_across_picks(self):
        body, _ = self.opened(f"cortados@R3:{OPUS}")
        answered = self.ok(body)
        self.assertEqual(self.ok(body), answered)                         # its own retry: replayed
        other = self.ok(dict(body, model=f"cortados@R3:{SOL}"))           # same history, another pick: its own route
        self.assertEqual(self.routed, [f"R3:{OPUS}", f"R3:{SOL}"])
        self.assertTrue(other["content"])
        self.assertEqual(len(self.by_label(f"R3:{OPUS}").history), 2)

    def test_an_escalated_conversation_leads_its_opening_reply_with_the_notice(self):
        self.escalate = True
        body, r1 = self.opened(f"cortados@R3:{SOL}")
        notice = r1["content"][0]
        self.assertEqual(notice["type"], "text")
        self.assertTrue(notice["text"].startswith(f"⚠ CortadOS: route pick R3:{SOL} escalated: "), notice)
        self.assertEqual(r1["content"][-1]["type"], "tool_use")           # the model's own blocks follow, untouched
        r2 = self.ok(body)                                                # the continuation matches as delivered
        self.assertFalse(any(b.get("text", "").startswith("⚠ CortadOS: route pick") for b in r2["content"]))
        self.assertEqual((len(self.routed), len(self.reg.sessions)), (1, 1))


    def test_a_table_change_never_breaks_a_running_conversation(self):
        # Fix round 1, finding 4 (gpt-6.1-sol on d2b45da): the pick is resolved when the conversation opens; a selected
        # model leaving the served catalogue later does not refuse its continuations, only new openings and forks.
        body, _ = self.opened(f"cortados@R3:{OPUS}+{SOL}")
        real = routing.pinnable_models
        with mock.patch.object(routing, "pinnable_models", lambda c: [p for p in real(c) if p[1] != OPUS]):
            self.assertEqual(routing.route_pick(core, None, None, f"cortados@R3:{OPUS}+{SOL}")[0], None)  # unresolvable
            self.ok(body)                                                    # the continuation runs on its route
            self.assertEqual((self.routed, len(self.reg.sessions)), ([f"R3:{OPUS}+{SOL}"], 1))
            fresh = {"model": f"cortados@R3:{OPUS}+{SOL}", "tools": [test_provider_codex.READ_TOOL],
                     "messages": [{"role": "user", "content": "a new conversation"}]}
            code, err = self.send_with(fresh, dict(self.IDENT, **{"x-claude-code-session-id": "s-other"}))
            self.assertEqual(code, 400, err)                                # an opening is validated now
            self.assertIn("not a model this endpoint serves", err["message"])
            code, err = self.send_with(dict(body, model=f"cortados@R3:{OPUS}"), self.IDENT)
            self.assertEqual(code, 400, err)                                # never opened under it: refused as before
        self.assertEqual((self.routed, len(self.reg.sessions)), ([f"R3:{OPUS}+{SOL}"], 1))

    # Fix round 2 (gpt-6.1-sol on 7879224): the kept pick is looked up only among the conversations the request could
    # continue (its scope, wire and history), never across the whole registry.
    OTHER = {"x-claude-code-session-id": "s-route-pick-other"}

    def opened_in(self, model, headers):
        first = {"model": model, "tools": [test_provider_codex.READ_TOOL],
                 "messages": [{"role": "user", "content": "please use tool"}]}
        r1 = self.ok(first, headers)
        use = r1["content"][-1]
        return dict(first, messages=first["messages"] + [
            {"role": "assistant", "content": r1["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}])

    def remapped_then_gone(self):
        """`opus` meant Opus, then Sonnet (a catalogue change), then nothing (both left the served catalogue)."""
        real = routing.pinnable_models
        return (mock.patch.dict(routing.SUB_ALIASES, {"opus": SONNET}),
                mock.patch.object(routing, "pinnable_models",
                                  lambda c: [p for p in real(c) if p[1] not in (OPUS, SONNET)]))

    def test_another_scope_under_the_same_alias_never_makes_a_continuation_ambiguous(self):
        # The reviewer's reproduction: scope A opened `opus+sol` as Opus, scope B as Sonnet; once `opus` resolves to
        # nothing, A's continuation was a 400 (two labels registry-wide) until B's conversation was dropped.
        model = f"cortados@R3:opus+{SOL}"
        a = self.opened_in(model, {})
        remap, gone = self.remapped_then_gone()
        with remap:
            b = self.opened_in(model, self.OTHER)
        self.assertEqual(self.labels(), [f"R3:{OPUS}+{SOL}", f"R3:{SONNET}+{SOL}"])
        with remap, gone:
            self.assertEqual(routing.route_pick(core, None, None, model)[0], None)     # unresolvable now
            self.ok(a)                                                                # A continues its own
            self.ok(b, self.OTHER)                                                    # and B its own
        self.assertEqual(len(self.reg.sessions), 2)
        self.assertEqual(len(self.routed), 2)                                         # nothing re-routed or forked
        self.assertEqual([len(s.history) for s in self.reg.sessions.values()], [2, 2])

    def test_two_labels_within_one_scope_are_still_ambiguous(self):
        # Within the caller's own scope the history is held by two conversations opened under one spelling with
        # different labels (the alias changed, the continuation forked): which one it continues cannot be told.
        model = f"cortados@R3:opus+{SOL}"
        body = self.opened_in(model, {})
        remap, gone = self.remapped_then_gone()
        with remap:
            self.ok(body)                                                             # a fork under the new label
        self.assertEqual(self.labels(), [f"R3:{OPUS}+{SOL}", f"R3:{SONNET}+{SOL}"])
        with remap, gone:
            code, err = self.send_with(body, self.IDENT)
        self.assertEqual(code, 400, err)
        self.assertIn("not a model this endpoint serves", err["message"])
        self.assertEqual(len(self.reg.sessions), 2)

    def test_a_kept_pick_is_never_borrowed_from_another_scope(self):
        # Only scope B opened under the spelling; a request in scope A carrying B's very history gets the 400, and the
        # lookup itself finds nothing for A.
        model = f"cortados@R3:{OPUS}+{SOL}"
        b = self.opened_in(model, self.OTHER)
        real = routing.pinnable_models
        with mock.patch.object(routing, "pinnable_models", lambda c: [p for p in real(c) if p[1] != OPUS]):
            code, err = self.send_with(b, self.IDENT)
            self.assertEqual(code, 400, err)
            self.assertIn("not a model this endpoint serves", err["message"])
            turn = anthropic_wire.parse_request(b)
            key = routing.pick_request_key(None, None, model)
            [opened] = self.reg.sessions.values()
            mine = self.reg.conversation_scope(wire_path=opened.wire_path, client=opened.client,
                                               identity="x-claude-code-session-id=s-route-pick")
            self.assertNotEqual(mine, opened.scope)
            self.assertIsNone(self.reg.pick_as_opened(key, turn, mine, opened.wire_path, client=opened.client))
            self.assertEqual(self.reg.pick_as_opened(key, turn, opened.scope, opened.wire_path,
                                                     client=opened.client).label, f"R3:{OPUS}+{SOL}")
            self.ok(b, self.OTHER)                                                    # B's own continuation runs
        self.assertEqual((len(self.routed), len(self.reg.sessions)), (1, 1))


del _CODEX


# ------------------------------------------------------------------------------- fix round 1 (gpt-6.1-sol on d2b45da)
TAG = LOCAL_NODE["models"][0]["id"]


class FixRound1Tests(SelectionTests):
    """Findings 1-3 of the adversarial review of d2b45da, on the real routing.choose and sensitive_local with a fake
    local node (probe and gate faked; nothing is contacted)."""

    locals().update({n: None for n in list(vars(SelectionTests)) if n.startswith("test_")})

    def setUp(self):
        super().setUp()
        self.local_nodes = [{**LOCAL_NODE, "sensitive": {"model": LOCAL_NODE["models"][0], "refusal_fallback": False}}]
        for name, fake in (("probe", lambda node: {TAG}), ("gate_busy", lambda node: None)):
            p = mock.patch.object(local_lane, name, fake)
            p.start(); self.addCleanup(p.stop)
        routing._auto_down.clear()
        self.addCleanup(routing._auto_down.clear)

    def test_a_local_preference_does_not_move_a_pick_off_its_models(self):
        # finding 1: `prefer` is a cost preference, not sensitivity: under a pick it moves nothing
        r = self.choose(f"R3:{OPUS}", local="prefer")
        self.assertNotIn("local", r)
        self.assertEqual(r["model"], OPUS)
        self.assertNotIn(routing.PICK_ESCALATED_KEY, r)
        self.assertIn("route pick: x-cortados-local: prefer not taken (a cost preference)", self.rules())
        self.assertFalse(any("overridden: sensitive" in x for x in self.rules()))
        r = self.choose(f"R3:{OPUS}", local="sensitive")                 # genuine sensitivity still wins (E5)
        self.assertEqual((r["provider"], r["model"]), ("local-pro", TAG))
        self.assertIn(f"route pick R3:{OPUS} overridden: sensitive", self.rules())

    def test_one_selected_model_must_meet_both_the_index_and_the_frontier_rule(self):
        # finding 2: Astra is frontier but 78.5 on code, Nova 96 but not frontier; a code turn at D 9 needs a frontier
        # model of 95: neither is one, so the pick escalates (to Opus, the only such model)
        r = self.choose(f"R3:{ASTRA}+{NOVA}", d=dec(steps=3, ambiguity=3, novelty=3))
        esc = r.get(routing.PICK_ESCALATED_KEY)
        self.assertIsNotNone(esc, r.get("model"))
        self.assertEqual((esc["reason"], esc["required"]), ("frontier", 95.0))
        self.assertEqual(r["model"], OPUS)
        self.assertTrue(any(x.startswith("route pick escalated: no selected model reaches the frontier requirement")
                            and "strongest selected frontier model has 78.5" in x for x in self.rules()))

    def test_the_sensitive_rule_wins_over_an_exhausted_selection(self):
        # finding 3: every selected route spent; a sensitive-marked turn goes local (E5) rather than an E2 refusal
        self.health["claude"] = row("claude", ok=False)
        self.health["claude#b"] = row("claude#b", ok=False)
        r = self.choose(f"R3:{OPUS}", local="sensitive")
        self.assertNotIn("error", r)
        self.assertEqual((r["provider"], r["model"]), ("local-pro", TAG))
        self.assertIn(f"route pick R3:{OPUS} overridden: sensitive", self.rules())
        self.assertIn("route pick: no selected route usable now (E2); the sensitive rule asked first (E5)",
                      self.rules())
        self.assertEqual(len({rid for rid, *_ in self.logged}), 1)
        self.logged.clear()
        r = self.choose(f"R3:{OPUS}")                                     # not sensitive: E2 refuses as before
        self.assertTrue(r["pick_refused"] and r["quota_blocked"])
        self.assertEqual(self.logged, [])


class EndpointStatusTests(Catalogue):
    """E2's statuses through the real endpoint: 429 for quota, never a 503 the client would retry until it timed out."""

    def test_a_spent_selection_is_a_429(self):
        class H(EndpointHarness):
            def runTest(self):
                pass
        h = H()
        h.setUp()
        self.addCleanup(h.tearDown)
        self.health["claude"] = row("claude", ok=False)
        self.health["claude#b"] = row("claude#b", ok=False)
        core.decide = lambda *a, **k: dec()
        h.reg.router = lambda b, client, **kw: routing.choose(core, b, client=client, conversation_id=uuid.uuid4().hex,
                                                              claude_bin=BIN, codex_bin=BIN, **kw)
        try:
            h.post({"model": f"cortados@R3:{OPUS}", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})
        except Exception as e:                                      # urllib.error.HTTPError
            self.assertEqual(e.code, 429)
            msg = json.loads(e.read())["error"]["message"]
            self.assertIn(f"route pick R3:{OPUS}: no selected route can take this conversation now", msg)
        else:
            self.fail("expected a 429")


if __name__ == "__main__":
    unittest.main()
