"""The band window (x-cortados-band / a `cortados@<window>` model; the whole-session launcher's --orchestrator-band):
S1's band clamped into [min, max] through core.resolve's existing cap and floor, under the hard rules. Fakes only: fake
model scores and lane health, a spy router behind the real endpoint, throwaway homes; no vendor CLI, no live state."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp(prefix="cortados-band-window-"))

from cortados_router import child_tasks, core, execution_routing, session
from cortados_router.provider import clients, routing, tasks
from cortados_router.provider.bridge import BridgeError
from tests.test_provider import TOKEN, EndpointHarness
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)
from tests import test_provider_codex, test_session
from cortados_router.provider import server as provider_server
from tests.test_routing_fixes import ACCOUNTS, MS, UCS_132, FakeSc, dec, health, week

R2 = ("R2", "R2")


def setattr_all(keep):
    core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts = keep


class ParseTests(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(core.parse_band_window("R2"), R2)
        self.assertEqual(core.parse_band_window(" r1-r2 "), ("R1", "R2"))
        self.assertIsNone(core.parse_band_window("any"))
        self.assertIsNone(core.parse_band_window("ANY"))
        for bad in ("", "R5", "R3-R1", "R1-", "-R2", "R1-R2-R3", "opus", None, 2):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.parse_band_window(bad)
        self.assertEqual(core.band_window_label("R2", "R2"), "R2")
        self.assertEqual(core.band_window_label("R1", "R2"), "R1-R2")


class ResolveTests(unittest.TestCase):
    """core.resolve(band_window=…): the clamp, and the hard rules over it. test_routing_fixes' catalogue and fake
    scarcity, patched onto cortados_router.core (that file loads its own copy of core)."""

    def setUp(self):
        keep = (core._sc_mod, core.model_scores, core.lane_health, core._vendor_accounts)
        self.addCleanup(lambda: setattr_all(keep))
        core.model_scores = lambda: MS
        core._vendor_accounts = lambda *a: [dict(x) for x in ACCOUNTS]
        self.fake({}, {}, claude=0.0, codex=0.0)

    def fake(self, lanes, ucs=UCS_132, **sc_by_lane):
        core._sc_mod = FakeSc(lanes, ucs)
        core.lane_health = lambda: health(**sc_by_lane)

    def the_132_state(self):
        import time
        n = time.time()
        self.fake({"claude": {"fetched_at": n, "windows": [week(60, 59, n)]},
                   "claude#b": {"fetched_at": n, "windows": [week(23, 59, n)]},
                   "codex": {"fetched_at": n, "windows": [week(5, 62.5, n, "week")]}},
                  **{"claude": 0.2, "claude#b": -0.94, "codex": -1.0})

    def test_clamp_up(self):
        low = dec(steps=1, ambiguity=0, novelty=0)                       # S1: R0
        self.assertEqual(core.resolve(low)["band"], "R0")
        r = core.resolve(low, band_window=R2)
        self.assertEqual(r["band"], "R2")
        self.assertIn("band window R2: band R0 raised to R2", r["hard_rules"])
        # the requirement is R2's, not R0's "anything qualifies"
        self.assertEqual(r["required_index"], core.required_index(core.table(), MS, "code", r["D"], "R2"))
        self.assertGreater(r["required_index"], 0)
        # a range floors at its min only
        self.assertEqual(core.resolve(low, band_window=("R1", "R2"))["band"], "R1")

    def test_clamp_down(self):
        hard = dec(steps=3, ambiguity=3, novelty=3)                      # D 9, blast radius 0: S1 says R3
        self.assertEqual(core.resolve(hard)["band"], "R3")
        r = core.resolve(hard, band_window=R2)
        self.assertEqual((r["band"], r["D"]), ("R2", core.band_max_D(core.table(), "R2")))
        self.assertIn("band window R2: band R3 lowered to R2", r["hard_rules"])
        self.assertTrue(any(x.startswith("band window: difficulty 9 counted as") for x in r["hard_rules"]))

    def test_within_the_window(self):
        r = core.resolve(dec(), band_window=("R1", "R3"))
        self.assertEqual(r["band"], core.resolve(dec())["band"])
        self.assertIn(f"band window R1-R3: band {r['band']} within the window", r["hard_rules"])

    def test_outward_beats_the_max(self):
        r = core.resolve(dec(steps=1, ambiguity=0, novelty=0, outward=True), band_window=R2)
        self.assertEqual((r["band"], r["features"]["blast_radius"]), ("R3", 3))
        self.assertIn("band window R2 overridden: blast radius 3 (outward effect) keeps R3", r["hard_rules"])
        self.assertFalse(any("counted as" in x for x in r["hard_rules"]))
        self.assertEqual(core.resolve(dec(blast=3), band_window=("R0", "R1"))["band"], "R3")

    def test_trusted_capability_floor_beats_the_max(self):
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), band_window=R2, minimum_band="R3")
        self.assertEqual((r["band"], r["D"]), ("R3", 9))                  # the requirement is uncapped too
        self.assertIn("band window R2 overridden: the trusted capability requirement floors band at R3",
                      r["hard_rules"])

    def test_with_the_batch_cap(self):
        # batch caps at R1, the window's min lifts it to R2; the difficulty stays capped (the window's max holds)
        r = core.resolve(dec(steps=3, ambiguity=3, novelty=3), band_window=R2, maximum_band="R1")
        self.assertEqual((r["band"], r["D"]), ("R2", core.band_max_D(core.table(), "R2")))
        # without a window, batch behaves exactly as before
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=3, novelty=3), maximum_band="R1")["band"], "R1")

    def test_degraded_scorer(self):
        degraded = {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "timeout"}
        r = core.resolve(degraded, "background", band_window=R2)            # background fallback band is R1
        self.assertEqual(r["band"], "R2")
        self.assertIn("engine degraded → fallback band", r["hard_rules"])
        self.assertIn("scorer unavailable → configured GPT Terra fallback", r["hard_rules"])
        self.assertNotEqual((r.get("chosen") or {}).get("provider", "").split("#")[0], "claude-cli")

    def test_degraded_scorer_through_the_provider_never_claude(self):
        degraded = {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "timeout"}
        allowed = [["claude-cli", "claude-opus-5-5"], ["openai-codex", "gpt-6.1-sol"], ["openai-codex", "gpt-6-astra"]]
        with mock.patch.object(core, "decide", return_value=degraded), \
                mock.patch.object(core, "log_route", return_value=True) as log:
            r = execution_routing.select(routing._NoClaudeWhenDegraded(core), prompt="plan it", platform="cli",
                                         constraints={"allowed_routes": allowed, "band_window": R2},
                                         credential_resolver=lambda **k: None)
        self.assertEqual(r["band"], "R2")
        self.assertNotEqual(r["provider"].split("#")[0], "claude-cli")
        self.assertEqual(log.call_args[0][1]["band_window"], "R2")

    def test_invalid_window_refused(self):
        for bad in (("R3", "R1"), ("R9", "R9"), "R2", ("R2",)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.resolve(dec(), band_window=bad)

    def test_select_routes_and_logs_the_window(self):
        self.the_132_state()
        allowed = [list(p) for p in (("claude-cli", "claude-opus-5-5"), ("claude-cli#b", "claude-opus-5-5"),
                                     ("openai-codex", "gpt-6.1-sol"), ("openai-codex", "gpt-6-astra"))]
        with mock.patch.object(core, "decide", return_value=dec(steps=3, ambiguity=3, novelty=3)), \
                mock.patch.object(core, "log_route", return_value=True) as log:
            r = execution_routing.select(core, prompt="rewrite the importer", platform="cli", session_id="bw",
                                         constraints={"allowed_routes": allowed, "band_window": R2},
                                         credential_resolver=lambda **k: None, supports_accounts=True)
        self.assertEqual((r["band"], r["band_window"]), ("R2", "R2"))
        self.assertEqual(r["route"]["required_index"],
                         core.required_index(core.table(), MS, "code", r["route"]["D"], "R2"))
        self.assertEqual(log.call_args[0][1]["band_window"], "R2")

    def test_routes_row_and_tasks_view(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(core, "STATE", Path(d)):
            core.log_route("t1", {"session_id": "s", "band_window": "R2"}, dec(), core.resolve(dec(), band_window=R2))
            core.log_route("t2", {"session_id": "s"}, dec(), core.resolve(dec()))
            out = core.resolve(dec(outward=True), band_window=R2)
            core.log_route("t3", {"session_id": "s", "band_window": "R2"}, dec(outward=True), out)
        c = sqlite3.connect(Path(d) / "router.db")
        self.assertEqual(c.execute("SELECT turn_id, band_window FROM routes ORDER BY turn_id").fetchall(),
                         [("t1", "R2"), ("t2", None), ("t3", "R2")])
        c.close()
        view = tasks.collect(Path(d))
        self.assertEqual(view["errors"], [])
        joined = json.dumps(view["tasks"])
        self.assertIn("band window R2", joined)
        self.assertIn("band window R2 overridden", joined)

    def test_rule_labels(self):
        self.assertEqual(tasks._rule_label("band window R2: band R1 raised to R2"), "band window R2")
        self.assertEqual(tasks._rule_label("band window R1-R2: band R2 within the window"), "band window R1-R2")
        self.assertEqual(tasks._rule_label("band window R2 overridden: blast radius 3 (outward effect) keeps R3"),
                         "band window R2 overridden")
        self.assertEqual(tasks._rule_label("band window: difficulty 9 counted as 6, the most R2 admits"),
                         "band window difficulty cap")
        self.assertIsNone(tasks._rule_label("band window <script>: x"))


class HeaderParseTests(unittest.TestCase):
    def test_header_and_model(self):
        self.assertEqual(routing.band_window(None, "cortados"), (None, None))            # a subagent: no window
        self.assertEqual(routing.band_window("", "sub/claude-haiku-4-5"), (None, None))  # a side call
        self.assertEqual(routing.band_window("r2", "cortados"), (R2, None))
        self.assertEqual(routing.band_window(None, "cortados@R1-R2"), (("R1", "R2"), None))
        self.assertEqual(routing.band_window("R2", "cortados@R2"), (R2, None))
        self.assertEqual(routing.band_window("any", "cortados"), (None, None))
        for h, m in (("R7", "cortados"), (None, "cortados@opus"), ("R1", "cortados@R2")):
            with self.subTest(h=h, m=m):
                w, err = routing.band_window(h, m)
                self.assertIsNone(w); self.assertTrue(err)

    def test_choose_passes_the_window_and_a_pin_ignores_it(self):
        seen = []

        def select(core_, **kw):
            seen.append(kw["constraints"])
            return {"error": "stop here"}
        with mock.patch.object(execution_routing, "select", select):
            routing.choose(core, {"model": "cortados@R2", "messages": [{"role": "user", "content": "hi"}]},
                           client="claude-cli", conversation_id="c1", band_window=R2)
            routing.choose(core, {"model": "cortados", "messages": [{"role": "user", "content": "hi"}]},
                           client="claude-cli", conversation_id="c2")
        self.assertEqual(seen[0]["band_window"], R2)
        self.assertNotIn("band_window", seen[1])                                   # a subagent: unwindowed
        with mock.patch.object(routing, "choose_pinned", return_value={"model": "claude-haiku-4-5"}) as pinned, \
                mock.patch.object(execution_routing, "select") as sel:
            routing.choose(core, {"model": "sub/claude-haiku-4-5", "messages": []}, client="claude-cli",
                           conversation_id="c3", band_window=R2)
        sel.assert_not_called(); pinned.assert_called_once()


def body(model="cortados", text="hi"):
    return {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": text}]}


class EndpointTests(EndpointHarness):
    """The real endpoint with a spy router: what reaches routing, and from whom."""
    OFFICE_TOKEN = "office-reader-token-0123456789"

    def setUp(self):
        super().setUp()
        self.seen = []
        self.reg.router = lambda b, client, **kw: (self.seen.append((b.get("model"), kw)), {"error": "stop here"})[1]
        path = Path(self.scratch) / "clients.json"
        path.write_text(json.dumps({"version": 1, "clients": [
            {"name": "reader", "token_sha256": clients.token_digest(self.OFFICE_TOKEN), "scopes": ["office:read"]}]}))
        self.reg.clients = clients.ClientTokens(path=path, owner_token=TOKEN)

    def error(self, b, **kw):
        try:
            self.post(b, **kw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]["message"]
        self.fail("expected an HTTP error")

    def test_header_reaches_the_router(self):
        self.assertEqual(self.status_of(body(), headers={"x-cortados-band": "r1-r2"}), 503)
        self.assertEqual(self.seen, [("cortados", {"band_window": ("R1", "R2")})])

    def test_main_loop_model_reaches_the_router_and_a_subagent_does_not(self):
        self.assertEqual(self.status_of(body("cortados@R2")), 503)               # the main loop
        self.assertEqual(self.status_of(body("cortados")), 503)                  # its subagent (forced model)
        self.assertEqual(self.seen, [("cortados@R2", {"band_window": R2}), ("cortados", {})])

    def test_invalid_or_conflicting_window_is_refused_before_routing(self):
        code, msg = self.error(body(), headers={"x-cortados-band": "R9"})
        self.assertEqual(code, 400); self.assertIn("not a band window", msg)
        code, msg = self.error(body("cortados@R2"), headers={"x-cortados-band": "R3"})
        self.assertEqual(code, 400); self.assertIn("different band windows", msg)
        self.assertEqual(self.seen, [])

    def test_untrusted_clients_never_reach_routing(self):
        # the model endpoints take the owner token only: a scoped client token is refused (403), a wrong one 401,
        # before the header is read, exactly as every other routing header
        self.assertEqual(self.status_of(body("cortados@R2"), token=self.OFFICE_TOKEN,
                                        headers={"x-cortados-band": "R2"}), 403)
        self.assertEqual(self.status_of(body("cortados@R2"), token="not-the-token",
                                        headers={"x-cortados-band": "R2"}), 401)
        self.assertEqual(self.seen, [])

    def test_responses_wire_takes_the_header(self):
        from tests.test_provider_responses import request, user
        try:
            self.post(request(user("hi"), tools=()), path="/v1/responses", headers={"x-cortados-band": "R2"})
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
        self.assertEqual(self.seen[0][1], {"band_window": R2})


_CODEX = test_provider_codex.CodexEndpointTests   # deleted below: a module-level TestCase would be collected here


class ContinuationTests(unittest.TestCase):
    """The window on continuations (round 3 of the band window review, owner 2026-11-24). An invalid one is refused on
    every request, before any lookup. A valid one is part of the conversation's identity (server._in_window): a request
    is matched only to a conversation opened in its own window; another window opens a new conversation from the
    history it carries, and the conversation it came from is untouched. The real endpoint over the fake codex
    app-server; the router is a stub returning a codex route."""

    IDENT = {"x-claude-code-session-id": "s-band-window"}

    # the codex endpoint harness, without inheriting (and so re-running) its tests
    tearDown, request, post, error_of, send_with, continuation = (
        _CODEX.tearDown, _CODEX.request, _CODEX.post, _CODEX.error_of, _CODEX.send_with, _CODEX.continuation)

    def setUp(self):
        test_provider_codex.CodexEndpointTests.setUp(self)
        self.routed = []

        def router(body, client, **kw):            # as routing.choose: a pin ignores the window, S1 routes in it
            self.routed.append(kw.get("band_window"))
            pin, w = routing.sub_pin(body), kw.get("band_window")
            if pin:
                return dict(test_provider_codex.CODEX_ROUTE, model=pin, band="pin", pinned=True)
            return dict(test_provider_codex.CODEX_ROUTE, **({"band_window": core.band_window_label(*w)} if w else {}))
        self.reg.router = router

    def ok(self, body, headers=None):
        code, got = self.send_with(body, dict(self.IDENT, **(headers or {})))
        self.assertEqual(code, 200, got)
        return got

    def opened(self, model):
        """A conversation opened in `model`'s window under the Claude Code session id, its first turn answered with a
        tool_use: (the next turn's body, answering it), the opening body."""
        first = {"model": model, "tools": [test_provider_codex.READ_TOOL],
                 "messages": [{"role": "user", "content": "please use tool"}]}
        r1 = self.ok(first)
        use = r1["content"][-1]
        nxt = dict(first, messages=first["messages"] + [
            {"role": "assistant", "content": r1["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use["id"], "content": "hello"}]}])
        return nxt, first

    def windows(self):
        return sorted((s.route or {}).get(provider_server.ASKED_WINDOW_KEY) or "none"
                      for s in self.reg.sessions.values())

    def by_window(self, label):
        [s] = [s for s in self.reg.sessions.values()
               if (s.route or {}).get(provider_server.ASKED_WINDOW_KEY) == label]
        return s

    def test_an_invalid_window_is_refused_on_opening(self):
        code, msg = self.error_of({"model": "cortados@NOT-A-BAND", "tools": [test_provider_codex.READ_TOOL],
                                   "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 400); self.assertIn("not a band window", msg)
        self.assertEqual((self.routed, len(self.reg.sessions)), ([], 0))

    def test_an_invalid_window_on_a_continuation_is_refused(self):
        body = self.continuation("cortados@R2", "cortados@NOT-A-BAND")
        code, msg = self.error_of(body)
        self.assertEqual(code, 400); self.assertIn("not a band window", msg)
        self.assertEqual(self.routed, [R2])                    # the opening turn only; the continuation never ran
        [session] = self.reg.sessions.values()                 # the conversation is kept: a valid retry continues it
        self.assertEqual(self.post(dict(body, model="cortados@R2"))["content"],
                         [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_an_invalid_header_window_on_a_continuation_is_refused(self):
        body, _ = self.opened("cortados@R2")
        code, err = self.send_with(body, dict(self.IDENT, **{routing.BAND_HEADER: "R9"}))
        self.assertEqual(code, 400); self.assertIn("not a band window", err["message"])
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_equivalent_spellings_are_one_window(self):
        body = self.continuation("cortados@R2", "cortados@r2-R2")   # another spelling of the same window
        self.assertEqual(self.post(body)["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_the_header_and_the_model_name_spell_one_window(self):
        body, _ = self.opened("cortados@R2-R2")
        self.ok(dict(body, model="cortados"), {routing.BAND_HEADER: "r2"})
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_a_same_window_continuation_with_a_reloaded_system_prompt_stays_in_its_conversation(self):
        # Reviewer P2 (3beeb7b, server.py:4293): the system prompt or tool set is no sign of a fork. A main loop that
        # reloads its system prompt, or loads a deferred tool, in the SAME window is its own continuation.
        body, _ = self.opened("cortados@R2")
        self.ok(dict(body, system="A reloaded system prompt."))
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_a_same_window_continuation_with_a_newly_loaded_tool_stays_in_its_conversation(self):
        body, _ = self.opened("cortados@R2")
        self.ok(dict(body, tools=body["tools"] + [dict(test_provider_codex.READ_TOOL, name="mcp__x__deferred")]))
        self.assertEqual((self.routed, len(self.reg.sessions)), ([R2], 1))

    def test_a_main_loop_window_change_opens_a_new_conversation_from_the_inherited_history(self):
        # Reviewer P2 (3beeb7b, server.py:4293): on 3beeb7b the same change was refused (terminal) or, with a reloaded
        # system prompt or a new tool, opened a fork. Now every shape of it is one thing: a new conversation.
        for extra in ({}, {"system": "A reloaded system prompt."},
                      {"tools": [test_provider_codex.READ_TOOL, dict(test_provider_codex.READ_TOOL, name="Grep")]}):
            with self.subTest(extra=sorted(extra)):
                for s in list(self.reg.sessions.values()):
                    self.reg.drop(s)
                self.routed.clear()
                body, _ = self.opened("cortados@R2")
                changed = dict(body, model="cortados@R3", **extra)
                self.ok(changed)                                         # 200: re-routed, never refused
                self.assertEqual(self.routed, [R2, ("R3", "R3")])
                self.assertEqual(self.windows(), ["R2", "R3"])
                parent, child = self.by_window("R2"), self.by_window("R3")
                self.assertEqual(len(parent.history), 1)                 # the parent is untouched
                self.assertFalse(getattr(child, "rebuilds", 0))          # its own conversation, not a parent rebuild

    def test_after_a_window_change_both_conversations_continue_independently(self):
        body, _ = self.opened("cortados@R2")
        r3 = self.ok(dict(body, model="cortados@R3"))                    # the R3 conversation answers
        self.assertEqual(self.ok(body)["content"], [{"type": "text", "text": "tool said: hello"}])   # R2 continues
        self.assertEqual(self.routed, [R2, ("R3", "R3")])
        r3_next = dict(body, model="cortados@R3", messages=body["messages"] + [
            {"role": "assistant", "content": r3["content"]},
            {"role": "user", "content": [{"type": "text", "text": "and then?"}]}])
        self.ok(r3_next)                                                 # R3's own continuation: not routed again
        self.assertEqual((self.routed, self.windows()), ([R2, ("R3", "R3")], ["R2", "R3"]))
        self.assertEqual((len(self.by_window("R2").history), len(self.by_window("R3").history)), (2, 3))

    def test_a_window_dropped_and_added_opens_new_conversations(self):
        for first, then, want in (("cortados@R2", "cortados", None), ("cortados", "cortados@R2", R2),
                                  ("cortados@R2", "cortados@R1-R2", ("R1", "R2"))):
            with self.subTest(first=first, then=then):
                for s in list(self.reg.sessions.values()):
                    self.reg.drop(s)
                self.routed.clear()
                body, _ = self.opened(first)
                self.ok(dict(body, model=then))
                self.assertEqual((len(self.routed), self.routed[-1], len(self.reg.sessions)), (2, want, 2))

    def test_a_pinned_conversation_ignores_the_window(self):
        body = self.continuation("sub/opus", "sub/opus")
        self.assertEqual(self.send_with(body, {routing.BAND_HEADER: "R1"})[0], 200)
        self.assertEqual(len(self.reg.sessions), 1)

    def test_a_pin_under_a_windowed_parent_opens_its_own_pinned_conversation(self):
        body, _ = self.opened("cortados@R2")
        self.ok(dict(body, model="sub/opus"))
        self.assertEqual(len(self.reg.sessions), 2)
        self.assertEqual(sorted(bool(s.route.get("pinned")) for s in self.reg.sessions.values()), [False, True])
        self.assertEqual(len(self.by_window("R2").history), 1)


    def test_concurrent_identical_window_changes_open_one_conversation(self):
        # Review round 4 (1ff9b69, server.py:3708), P2: two overlapping identical R2 -> R3 requests were both told Fork
        # and each opened a backend (windows R2, R3, R3); the next retry was a terminal 400 for two live conversations.
        # Now the second waits on the fork's join gate, looks up again and joins the first's conversation.
        body, _ = self.opened("cortados@R2")
        changed = dict(body, model="cortados@R3")
        real_new, entered, release, starts, out = self.reg._new, threading.Event(), threading.Event(), [], {}

        def slow_new(*a, **kw):                                          # the first R3 backend is starting
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
            deadline = time.time() + 10                                  # B on A's gate (fixed) or opening (1ff9b69)
            while not (any(g[1] == 2 for g in self.reg._rebuilding.values()) or len(starts) > 1):
                self.assertLess(time.time(), deadline, "the second request never reached the gate")
                time.sleep(0.01)
            release.set()
            a.join(30)
            b.join(30)
        self.assertEqual(len(starts), 1, out)                             # one R3 backend, not two
        self.assertEqual(self.windows(), ["R2", "R3"])
        self.assertEqual(self.routed, [R2, ("R3", "R3")])
        self.assertEqual(self.reg._rebuilding, {})
        served = next(r for code, r in out.values() if code == 200)
        # Both are served: one answered, the other joined its conversation (a stale 409 when it met the finished turn on
        # the turn lock, or the replay of that answer) ...
        for code, r in out.values():
            self.assertTrue((code, r) == (200, served) or code == 409, out)
        self.assertEqual(self.ok(changed), served)                         # ... and its retry is the replay, never a 400
        follow = dict(changed, messages=changed["messages"] + [
            {"role": "assistant", "content": served["content"]},
            {"role": "user", "content": [{"type": "text", "text": "and then?"}]}])
        self.ok(follow)                                                   # the R3 conversation continues unambiguously
        self.assertEqual(self.windows(), ["R2", "R3"])

    def test_another_windows_conversation_never_hides_a_failure_or_resurrects_it(self):
        # Review round 4 (1ff9b69, server.py:1691), P2: R3 advanced from the inherited history; R2 then failed and left
        # its tombstone. R2's retry had its failure suppressed by R3's live session (it extends the history), was forked
        # from R3 into a fresh R2 backend and answered 200. A failure is suppressed only by its own window's session.
        body, _ = self.opened("cortados@R2")
        self.ok(dict(body, model="cortados@R3"))                          # R3 advances from the inherited history
        r2 = self.by_window("R2")
        with mock.patch.object(r2, "run_turn", side_effect=BridgeError("codex backend: the turn failed")):
            with self.assertLogs("cortados.provider", "WARNING"):
                self.assertEqual(self.send_with(body, self.IDENT)[0], 502)   # R2 fails: dropped, tombstoned
        self.assertEqual(self.windows(), ["R3"])
        code, err = self.send_with(body, self.IDENT)                      # R2's retry
        self.assertEqual(code, 400, err)                                  # the failure, terminal
        self.assertIn("failed at", err["message"])
        self.assertEqual(self.windows(), ["R3"])                          # no resurrected R2 backend
        self.assertEqual(self.routed, [R2, ("R3", "R3")])                 # never routed again
        self.assertEqual(len(self.by_window("R3").history), 2)           # R3 untouched


class ForkTests(ContinuationTests):
    """Forked subagents of a windowed main loop: they inherit its history and session id and send plain `cortados`, so
    they are requests in another window: each opens its own conversation from the inherited history."""

    SUB = "You are a forked subagent."

    # ContinuationTests' own tests must not run twice through the subclass.
    locals().update({n: None for n in list(vars(ContinuationTests)) if n.startswith("test_")})

    def test_a_plain_cortados_fork_of_an_r2_parent_is_routed_on_its_own(self):
        body, _ = self.opened("cortados@R2")
        self.assertTrue(self.ok(dict(body, model="cortados", system=self.SUB))["content"])
        self.assertEqual(self.routed, [R2, None])                         # routed again, without a window (S1's band)
        self.assertEqual(self.windows(), ["R2", "none"])
        self.assertEqual(len(self.by_window("R2").history), 1)            # the parent is untouched

    def test_an_identity_less_fork_is_its_own_conversation_too(self):
        body = self.continuation("cortados@R2", "cortados")               # no session id: matched on tool-id proof
        self.assertTrue(self.post(dict(body, system=self.SUB))["content"])
        self.assertEqual((self.routed, self.windows()), ([R2, None], ["R2", "none"]))

    def test_a_fork_of_a_parent_that_already_answered_that_request_is_not_replayed_or_refused(self):
        # Reviewer P2 (3beeb7b, server.py:3599): the parent answered the inherited request, so find raised Replay
        # (its fingerprint excludes system and tools) before the fork was detected, and the fork got the parent's
        # changed-window 400. The replay is looked up only in the request's own window now.
        body, _ = self.opened("cortados@R2")
        answered = self.ok(body)                                          # the parent answers this exact request
        self.assertEqual(self.ok(body), answered)                         # its own retry: replayed, as before
        self.assertEqual(self.routed, [R2])
        fork = self.ok(dict(body, model="cortados", system=self.SUB))     # the fork: same history and user turn
        self.assertEqual(self.routed, [R2, None])                         # its own route, its own backend
        self.assertEqual(self.windows(), ["R2", "none"])
        self.assertTrue(fork["content"])
        self.assertEqual(len(self.by_window("R2").history), 2)            # the parent: as it was

    def test_a_fork_after_its_parent_moved_on_is_not_served_on_the_parents_route(self):
        # The parent answered again, so the fork's history is a branch of it (a rebuild for its own window): its own
        # route, and the parent's backend is kept, never rebuilt from.
        body, _ = self.opened("cortados@R2")
        self.ok(body)                                                     # the parent moves on
        msgs = body["messages"][:-1] + [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": body["messages"][-1]["content"][0]["tool_use_id"],
             "content": "forked"}]}]
        self.ok(dict(body, model="cortados", system=self.SUB, messages=msgs))
        self.assertEqual((self.routed, self.windows()), ([R2, None], ["R2", "none"]))
        parent, child = self.by_window("R2"), self.by_window(None)
        self.assertEqual(len(parent.history), 2)
        self.assertFalse(getattr(child, "rebuilds", 0))                    # its own conversation, not a parent rebuild

    def test_a_fork_continues_on_its_own_route_and_the_parent_on_its_own(self):
        body, _ = self.opened("cortados@R2")
        fork = dict(body, model="cortados", system=self.SUB)
        r = self.ok(fork)
        fork_next = dict(fork, messages=fork["messages"] + [
            {"role": "assistant", "content": r["content"]},
            {"role": "user", "content": [{"type": "text", "text": "again"}]}])
        self.ok(fork_next)                                                # the fork's own continuation
        self.assertEqual(self.routed, [R2, None])                         # not routed again, not refused
        self.assertEqual(self.ok(body)["content"], [{"type": "text", "text": "tool said: hello"}])   # the parent's
        self.assertEqual((self.routed, self.windows()), ([R2, None], ["R2", "none"]))
        self.assertEqual((len(self.by_window("R2").history), len(self.by_window(None).history)), (2, 3))

    def test_a_fork_of_a_fork(self):
        body, _ = self.opened("cortados@R2")
        fork = dict(body, model="cortados", system=self.SUB)
        r = self.ok(fork)
        grandchild = dict(fork, model="cortados@R3", system="A fork of the fork.", messages=fork["messages"] + [
            {"role": "assistant", "content": r["content"]},
            {"role": "user", "content": [{"type": "text", "text": "deeper"}]}])
        self.ok(grandchild)
        self.assertEqual(self.routed, [R2, None, ("R3", "R3")])
        self.assertEqual(self.windows(), ["R2", "R3", "none"])
        self.assertEqual((len(self.by_window("R2").history), len(self.by_window(None).history)), (1, 2))


class InWindowTests(unittest.TestCase):
    """server._in_window: a conversation is in its own window only; a pinned one in every window."""

    def test_membership(self):
        S = lambda w, **r: type("S", (), {"route": {provider_server.ASKED_WINDOW_KEY: w, **r}})()
        inw = provider_server._in_window
        self.assertTrue(inw(S("R2"), "R2"))
        self.assertFalse(inw(S("R2"), None))
        self.assertFalse(inw(S(None), "R2"))
        self.assertTrue(inw(S(None), None))
        self.assertTrue(inw(S("R2"), provider_server.ANY_WINDOW))
        self.assertTrue(inw(S(None, pinned=True), "R3"))
        self.assertTrue(inw(type("S", (), {"route": None})(), None))     # a fixed-model endpoint: no route, no window


del _CODEX


class LauncherTests(test_session.SessionTests):
    """session.py --orchestrator-band: only the main loop's --model carries the window."""

    def run_prepare(self, *argv):
        args, env, cmd = self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok, *argv])
        with open(cmd[cmd.index("--settings") + 1], encoding="utf-8") as f:
            settings = json.load(f)
        return env, cmd, settings

    def test_routed_orchestrator_defaults_to_r4(self):
        env, cmd, settings = self.run_prepare("--orchestrator", "cortados")
        self.assertEqual(cmd[1:3], ["--model", "cortados@R4"])   # owner 2026-11-24: the Opus 5.5 / Sol 6.1 band
        for e in (env, settings["env"]):
            self.assertEqual(e["CLAUDE_CODE_SUBAGENT_MODEL"], "cortados")            # subagents unwindowed
            self.assertEqual(e["CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL"], "cortados")
            self.assertEqual(e["ANTHROPIC_SMALL_FAST_MODEL"], session.SMALL_FAST_MODEL)
            self.assertNotIn("x-cortados-band", e["ANTHROPIC_CUSTOM_HEADERS"])       # would reach subagents too
        self.assertNotIn("cortados@", json.dumps(settings))

    def test_explicit_window_and_any(self):
        self.assertEqual(self.run_prepare("--orchestrator", "cortados", "--orchestrator-band", "r1-r3")[1][2],
                         "cortados@R1-R3")
        self.assertEqual(self.run_prepare("--orchestrator", "cortados", "--orchestrator-band", "any")[1][2],
                         "cortados")

    def test_pinned_orchestrator(self):
        self.assertEqual(self.run_prepare("--orchestrator", "opus")[1][2], "sub/claude-opus-5-5")
        self.assertEqual(self.run_prepare("--orchestrator", "opus", "--orchestrator-band", "any")[1][2],
                         "sub/claude-opus-5-5")
        with self.assertRaises(session.SessionError):
            self.run_prepare("--orchestrator", "opus", "--orchestrator-band", "R2")

    def test_invalid_window_refused(self):
        for bad in ("R5", "R3-R1", "opus"):
            with self.subTest(bad=bad), self.assertRaises(session.SessionError):
                self.run_prepare("--orchestrator", "cortados", "--orchestrator-band", bad)
        self.assertEqual(self.settings_files(), [], "nothing is written for a refused window")

    def test_print_env_shows_the_model(self):
        rc, out, err = self.run_main("--provider-url", "http://r.example", "--token-file", self.tok,
                                     "--orchestrator", "cortados", "--print-env")
        self.assertEqual(rc, 0, err)
        self.assertIn("--model cortados@R4", out)

    def test_child_tasks_keep_free_routing(self):
        import inspect
        self.assertIn('"--orchestrator-band", "any"', inspect.getsource(child_tasks._prepare))

    def test_launcher_model_is_what_the_provider_parses(self):
        _, cmd, _ = self.run_prepare("--orchestrator", "cortados", "--orchestrator-band", "R1-R2")
        self.assertEqual(routing.band_window(None, cmd[2]), (("R1", "R2"), None))


# SessionTests' own tests must not run twice through the subclass.
for _name in [n for n in vars(test_session.SessionTests) if n.startswith("test_")]:
    setattr(LauncherTests, _name, None)


if __name__ == "__main__":
    unittest.main()
