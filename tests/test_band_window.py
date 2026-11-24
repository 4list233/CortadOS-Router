"""The band window (x-cortados-band / a `cortados@<window>` model; the whole-session launcher's --orchestrator-band):
S1's band clamped into [min, max] through core.resolve's existing cap and floor, under the hard rules. Fakes only: fake
model scores and lane health, a spy router behind the real endpoint, throwaway homes; no vendor CLI, no live state."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp(prefix="cortados-band-window-"))

from cortados_router import child_tasks, core, execution_routing, session
from cortados_router.provider import clients, routing, tasks
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
    """Review of b1d3075 (P2): the window is read on every request. An invalid one is refused on a continuation as on
    an opening turn; a changed one is refused (terminal), never served on the old route (server Handler._window_change).
    The real endpoint over the fake codex app-server; the router is a stub returning a codex route."""

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

    def test_an_invalid_window_on_a_continuation_is_refused(self):
        body = self.continuation("cortados@R2", "cortados@NOT-A-BAND")
        code, msg = self.error_of(body)
        self.assertEqual(code, 400); self.assertIn("not a band window", msg)
        self.assertEqual(self.routed, [R2])                    # the opening turn only; the continuation never ran
        [session] = self.reg.sessions.values()                 # the conversation is kept: a valid retry continues it
        self.assertEqual(self.post(dict(body, model="cortados@R2"))["content"],
                         [{"type": "text", "text": "tool said: hello"}])

    def test_the_same_window_continues(self):
        body = self.continuation("cortados@R2", "cortados@r2-R2")   # another spelling of the same window
        self.assertEqual(self.post(body)["content"], [{"type": "text", "text": "tool said: hello"}])
        self.assertEqual(self.routed, [R2])

    def test_a_changed_window_is_refused_not_rerouted(self):
        for first, then in (("cortados@R2", "cortados@R1-R2"), ("cortados@R2", "cortados"), ("cortados", "cortados@R2")):
            with self.subTest(first=first, then=then):
                self.routed.clear()
                for s in list(self.reg.sessions.values()):
                    self.reg.drop(s)
                code, msg = self.error_of(self.continuation(first, then))
                self.assertEqual(code, provider_server.TERMINAL_STATUS)   # terminal: the retry asks the same
                self.assertIn("band window is fixed per conversation", msg)
                self.assertEqual(len(self.routed), 1)                    # never re-routed

    def test_a_changed_header_window_is_refused_too(self):
        body = self.continuation("cortados@R2", "cortados")
        code, err = self.send_with(body, {routing.BAND_HEADER: "R3"})
        self.assertEqual(code, provider_server.TERMINAL_STATUS)
        self.assertIn("this request asks for band window R3", err["message"])
        self.assertEqual(self.send_with(body, {routing.BAND_HEADER: "R2"})[0], 200)   # the header naming the same one

    def test_a_pinned_conversation_ignores_the_window(self):
        body = self.continuation("sub/opus", "sub/opus")
        self.assertEqual(self.send_with(body, {routing.BAND_HEADER: "R1"})[0], 200)


del _CODEX


class LauncherTests(test_session.SessionTests):
    """session.py --orchestrator-band: only the main loop's --model carries the window."""

    def run_prepare(self, *argv):
        args, env, cmd = self.prepare(["--provider-url", "http://r.example", "--token-file", self.tok, *argv])
        with open(cmd[cmd.index("--settings") + 1], encoding="utf-8") as f:
            settings = json.load(f)
        return env, cmd, settings

    def test_routed_orchestrator_defaults_to_r2(self):
        env, cmd, settings = self.run_prepare("--orchestrator", "cortados")
        self.assertEqual(cmd[1:3], ["--model", "cortados@R2"])
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
        self.assertIn("--model cortados@R2", out)

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
