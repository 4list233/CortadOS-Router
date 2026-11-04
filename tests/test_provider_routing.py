"""Provider routing through the router's real execution policy, on throwaway state with live lane health stubbed.

core.lane_health is replaced (it would sample the vendor CLIs) and core.decide is replaced by fixed S1 answers, so
these tests pin what the router does with a scored request, not S1 itself.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cortados_router import core
from cortados_router.provider import routing

EASY = {"steps": 0, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0,
        "outward": False, "needs_clarification": False, "diversity": False, "work_kind": "code", "urgency": "now", "parts": 0}
HARD = dict(EASY, steps=3, ambiguity=3, blast_radius=2, novelty=3, context_size=1)


def scored(answers):
    return lambda text, ctx=None: {"answers": dict(answers), "degraded": False, "engine": "s1-test", "engine_ms": 1}


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self._keep = (core.STATE, core.lane_health, core.decide)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-routing-"))   # never the live router.db
        core.lane_health = lambda: {}
        routing.execution_routing._scores.clear()

    def tearDown(self):
        core.STATE, core.lane_health, core.decide = self._keep

    def body(self, text):
        return {"model": "cortados", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\nrepo context the client injected\n</system-reminder>"},
            {"type": "text", "text": text}]}]}

    def choose(self, answers, text):
        core.decide = scored(answers)
        return routing.choose(core, self.body(text), client="claude-cli", conversation_id=text[:8], claude_bin="python3",
                              codex_bin="python3")

    def test_the_exclusion_note_is_in_the_logged_route(self):
        # the real select (no fake): the note is in the hard_rules core.log_route records
        logged = []
        keep = core.log_route
        core.log_route = lambda rid, meta, decision, route: logged.append(list(route["hard_rules"]))
        try:
            core.decide = scored(EASY)
            r = routing.choose(core, self.body("rename a variable"), client="claude-cli", conversation_id="x",
                               claude_bin="python3", codex_bin="python3", exclude_model="claude-opus-5-5")
        finally:
            core.log_route = keep
        self.assertNotIn("error", r)
        self.assertNotEqual(r["model"], "claude-opus-5-5")
        self.assertEqual(len(logged), 1)
        self.assertIn("excluded by client: claude-opus-5-5", logged[0])

    def test_the_exclusion_displaces_the_model_the_request_would_have_routed_to(self):
        # the real select: the same request, routed once freely and once with that route's model excluded
        core.decide = scored(EASY)
        free = routing.choose(core, self.body("rename a variable"), client="claude-cli", conversation_id="free",
                              claude_bin="python3", codex_bin="python3")
        self.assertNotIn("error", free)
        routing.execution_routing._scores.clear()
        kept_off = routing.choose(core, self.body("rename a variable"), client="claude-cli", conversation_id="kept",
                                  claude_bin="python3", codex_bin="python3", exclude_model=free["model"])
        self.assertNotIn("error", kept_off)
        self.assertNotEqual(routing.model_key(kept_off["model"]), routing.model_key(free["model"]))

    def test_only_served_providers_are_offered(self):
        routes = routing.served_routes(core)
        self.assertTrue(routes)
        self.assertEqual({p for p, _ in routes}, {"claude-cli", "codex-cli"})

    def test_codex_rows_are_served_as_codex_cli_never_as_the_native_provider(self):
        routes = routing.served_routes(core)
        self.assertIn(("codex-cli", "gpt-5.6-terra"), routes)
        self.assertNotIn("openai-codex", {p for p, _ in routes})

    def test_harder_requests_get_a_stronger_model(self):
        easy = self.choose(EASY, "rename a variable in utils.py")
        hard = self.choose(HARD, "redesign the storage layer across services")
        self.assertNotIn("error", easy)
        self.assertNotIn("error", hard)
        self.assertTrue({easy["provider"], hard["provider"]} <= set(routing.SERVED_PROVIDERS))
        self.assertLess(core.BAND_ORDER.index(easy["band"]), core.BAND_ORDER.index(hard["band"]))
        idx = {c["model"]: c["index"] for c in [easy["route"]["chosen"], hard["route"]["chosen"]]}
        self.assertLessEqual(idx[easy["model"]], idx[hard["model"]])

    def test_every_route_is_logged_for_training_with_the_client(self):
        r = self.choose(EASY, "rename a variable in utils.py")
        with sqlite3.connect(core.STATE / "router.db") as db:
            row = db.execute("SELECT platform, request, provider, model FROM routes WHERE turn_id=?", (r["route_id"],)).fetchone()
        self.assertEqual(row, ("provider:claude-cli", "rename a variable in utils.py", r["provider"], r["model"]))
        self.assertIn(r["provider"], routing.SERVED_PROVIDERS)

    def test_injected_client_context_is_not_scored(self):
        self.assertEqual(routing.request_text(self.body("fix the bug")["messages"]), "fix the bug")

    def test_a_degraded_scorer_routes_to_codex_never_to_claude(self):
        core.decide = lambda text, ctx=None: {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "down"}
        r = routing.choose(core, self.body("rename a variable"), client="claude-cli", conversation_id="c", claude_bin="python3",
                           codex_bin="python3")
        self.assertNotIn("error", r)
        self.assertEqual(r["provider"], "codex-cli")
        self.assertTrue(r["degraded"])
        self.assertNotIn("claude-cli", json.dumps(r["route"]["chosen"]))

    def test_a_degraded_scorer_with_no_codex_lane_is_refused_not_sent_to_claude(self):
        core.decide = lambda text, ctx=None: {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1, "error": "down"}
        r = routing.choose(core, self.body("rename a variable"), client="claude-cli", conversation_id="c", claude_bin="python3",
                           codex_bin="no-such-codex-anywhere")
        self.assertIn("error", r)
        self.assertIn("never defaults to Claude", r["error"])
        self.assertIn("no Codex lane survived", r["error"])
        self.assertFalse((core.STATE / "router.db").exists() and sqlite3.connect(core.STATE / "router.db").execute(
            "SELECT count(*) FROM routes").fetchone()[0])

    def test_a_standalone_title_call_is_scored_and_logged_as_itself(self):
        core.decide = scored(EASY)
        body = {"messages": [{"role": "user", "content": "deploy the payments service to production"},
                             {"role": "assistant", "content": "Deployed."},
                             {"role": "user", "content": "Return only a three word title for this conversation"}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="t", claude_bin="python3", codex_bin="python3")
        with sqlite3.connect(core.STATE / "router.db") as db:
            row = db.execute("SELECT request FROM routes WHERE turn_id=?", (r["route_id"],)).fetchone()
        self.assertEqual(row, ("Return only a three word title for this conversation",))

    def test_a_missing_cli_refuses_rather_than_guessing(self):
        core.decide = scored(EASY)
        r = routing.choose(core, self.body("x"), client="t", conversation_id="c", claude_bin="no-such-cli-anywhere",
                           codex_bin="no-such-codex-anywhere")
        self.assertIn("error", r)
        self.assertIn("credentials unavailable", json.dumps(r))

    def test_a_missing_codex_cli_only_removes_the_codex_lane(self):
        core.decide = scored(EASY)
        r = routing.choose(core, self.body("rename a variable in utils.py"), client="t", conversation_id="c",
                           claude_bin="python3", codex_bin="no-such-codex-anywhere")
        self.assertNotIn("error", r)
        self.assertEqual(r["provider"], "claude-cli")


class SubscriptionPinTests(unittest.TestCase):
    """`sub/<claude model>`: the client chose the model, so S1 is never asked, but the account, capacity and logging rules hold."""

    def setUp(self):
        self._keep = (core.STATE, core.lane_health, core.decide, core._vendor_accounts)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-pin-"))   # never the live router.db
        core.lane_health = lambda: {}

        def no_s1(text, ctx=None):
            raise AssertionError("a pinned request must not be scored")
        core.decide = no_s1

    def tearDown(self):
        core.STATE, core.lane_health, core.decide, core._vendor_accounts = self._keep

    def pin(self, model, claude_bin="python3"):
        body = {"model": model, "tools": [{"name": "Read"}], "messages": [{"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\ninjected\n</system-reminder>"}, {"type": "text", "text": "plan the work"}]}]}
        return routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin=claude_bin, codex_bin="python3")

    def two_accounts(self):
        core._vendor_accounts = lambda *a: [
            {"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": False, "config_dir": "/x/a"},
            {"lane": "claude#b", "provider": "claude-cli#b", "account": "b", "reserve": False, "config_dir": "/x/b"}]

    def test_pin_parsing(self):
        self.assertEqual(routing.sub_pin({"model": "sub"}), routing.SUB_DEFAULT)
        self.assertEqual(routing.sub_pin({"model": "sub/opus"}), "claude-opus-5-5")
        self.assertEqual(routing.sub_pin({"model": "sub/claude-sonnet-5"}), "claude-sonnet-5")
        self.assertEqual(routing.sub_pin({"model": " sub/ "}), routing.SUB_DEFAULT)
        for m in ("cortados", "api/claude-opus-5-5", "subway", None):
            self.assertIsNone(routing.sub_pin({"model": m}))

    def test_every_alias_names_a_served_claude_model(self):
        served = {m for p, m in routing.served_routes(core) if p.split("#")[0] == "claude-cli"}
        self.assertLessEqual(set(routing.SUB_ALIASES.values()) | {routing.SUB_DEFAULT}, served)

    def test_pin_runs_the_named_model_without_s1_and_is_logged(self):
        r = self.pin("sub/opus")
        self.assertNotIn("error", r)
        self.assertEqual((r["provider"], r["model"], r["band"], r["pinned"]), ("claude-cli", "claude-opus-5-5", "pin", True))
        with sqlite3.connect(core.STATE / "router.db") as db:
            row = db.execute("SELECT platform, request, engine, provider, model, answers FROM routes WHERE turn_id=?",
                             (r["route_id"],)).fetchone()
        self.assertEqual(row, ("provider:claude-cli", "plan the work", "pin", "claude-cli", "claude-opus-5-5", "null"))

    def test_pin_below_the_band_still_runs(self):
        r = self.pin("sub/haiku")
        self.assertEqual(r.get("model"), "claude-haiku-4-5-20251001")

    def test_unknown_model_is_a_client_error(self):
        r = self.pin("sub/gpt-9-imaginary")
        self.assertTrue(r.get("client_error"))
        self.assertIn("sub/gpt-9-imaginary is not a model this endpoint serves", r["error"])
        self.assertIn("gpt-6.1-sol", r["error"])            # the refusal names what can be pinned, Codex included
        self.assertIn("claude-opus-5-5", r["error"])
        self.assertFalse((core.STATE / "router.db").exists())

    def codex_served(self):
        return sorted(m for p, m in routing.served_routes(core) if p == "codex-cli")

    def test_pinnable_models_are_the_served_routes_of_both_backends(self):
        pins = routing.pinnable_models(core)
        self.assertEqual([m for p, m in pins if p == "codex-cli"], self.codex_served())
        self.assertIn(("claude-cli", "claude-opus-5-5"), pins)
        self.assertTrue(self.codex_served())                # the tables do serve Codex models
        self.assertEqual(pins[0][0], "claude-cli")          # Claude first, then Codex

    def test_codex_pin_runs_the_named_model_on_the_codex_backend_without_s1(self):
        for model in self.codex_served():
            r = self.pin(f"sub/{model}")
            self.assertNotIn("error", r, model)
            self.assertEqual((r["provider"], r["model"], r["band"], r["pinned"], r["account"]),
                             ("codex-cli", model, "pin", True, None))
            self.assertEqual(r["route"]["chosen"]["lane"], "codex")
            self.assertEqual(r["context_length"],
                             int(core._context_cfg().get("provider_ceiling_tokens", {}).get("codex-cli", 200000)))
        with sqlite3.connect(core.STATE / "router.db") as db:
            row = db.execute("SELECT engine, provider, model FROM routes WHERE turn_id=?", (r["route_id"],)).fetchone()
        self.assertEqual(row, ("pin", "codex-cli", self.codex_served()[-1]))

    def test_codex_pin_needs_the_codex_binary(self):
        body = {"model": "sub/gpt-6.1-sol", "messages": [{"role": "user", "content": "x"}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="c", claude_bin="python3",
                           codex_bin="no-such-codex-anywhere")
        self.assertIn("no-such-codex-anywhere is not on PATH", r["error"])
        self.assertFalse(r.get("client_error"))

    def test_spent_codex_lane_refuses_without_an_api_hint(self):
        core.lane_health = lambda: {"codex": {"ok": False, "why": "week 100% used", "pools": {}}}
        r = self.pin("sub/gpt-6.1-sol")
        self.assertIn("no Codex subscription account can take this request", r["error"])
        self.assertIn("week 100% used", r["error"])
        self.assertNotIn("api/", r["error"])                # the API-credit lane is Anthropic's only

    def test_oversized_codex_pin_is_refused_on_the_codex_cli_ceiling(self):
        cap = int(core._context_cfg().get("provider_ceiling_tokens", {}).get("codex-cli", 200000))
        body = {"model": "sub/gpt-6.1-sol", "messages": [{"role": "user", "content": "x" * ((cap + 10) * 4)}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="c", claude_bin="python3", codex_bin="python3")
        self.assertTrue(r.get("fit_blocked"))
        self.assertIn("any Codex account", r["error"])

    def test_excluded_codex_pin_is_refused(self):
        body = {"model": "sub/gpt-6.1-sol", "messages": [{"role": "user", "content": "x"}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="c", claude_bin="python3",
                           codex_bin="python3", exclude_model="claude-opus-5-5, GPT-6.1-Sol")
        self.assertTrue(r.get("client_error"))
        self.assertIn("sub/gpt-6.1-sol is the excluded model", r["error"])
        r = routing.choose(core, body, client="claude-cli", conversation_id="c", claude_bin="python3",
                           codex_bin="python3", exclude_model="gpt-6-sol")
        self.assertEqual(r.get("model"), "gpt-6.1-sol")     # another model excluded: the pin runs

    def test_broken_claude_accounts_refuses_a_claude_pin_but_not_a_codex_one(self):
        def broken(*a):
            raise core.AccountsConfigError("accounts.json is present but unreadable")
        core._vendor_accounts = broken
        self.assertIn("unreadable", self.pin("sub/opus")["error"])
        self.assertEqual(self.pin("sub/gpt-6.1-sol").get("provider"), "codex-cli")

    def test_readable_model_names(self):
        for model, want in (("claude-fable-5-1", "Claude Fable 5.1"), ("gpt-6.1-sol", "GPT-6.1 Sol"),
                            ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"), ("gpt-5.5", "GPT-5.5"),
                            ("claude-opus-5", "Claude Opus 5"), ("gpt-6-astra", "GPT-6 Astra"),
                            ("muse-spark-1.3", "muse-spark-1.3")):
            self.assertEqual(routing.readable_model_name(model), want)

    def test_missing_cli_refuses(self):
        r = self.pin("sub/opus", claude_bin="no-such-cli-anywhere")
        self.assertIn("not on PATH", r["error"])

    def test_spent_account_is_skipped(self):
        self.two_accounts()
        core.lane_health = lambda: {"claude": {"ok": False, "why": "week 100% used", "pools": {}, "config_dir": "/x/a"},
                                    "claude#b": {"ok": True, "scarcity": 0.4, "pools": {}, "config_dir": "/x/b"}}
        r = self.pin("sub/opus")
        self.assertEqual((r["provider"], r["account"], r["config_dir"]), ("claude-cli#b", "b", "/x/b"))

    def test_least_scarce_account_wins(self):
        self.two_accounts()
        core.lane_health = lambda: {"claude": {"ok": True, "scarcity": 0.9, "pools": {}, "config_dir": "/x/a"},
                                    "claude#b": {"ok": True, "scarcity": 0.2, "pools": {}, "config_dir": "/x/b"}}
        self.assertEqual(self.pin("sub/opus")["provider"], "claude-cli#b")

    def test_every_account_spent_points_at_the_api_pin(self):
        self.two_accounts()
        core.lane_health = lambda: {lane: {"ok": False, "why": "spent", "pools": {}, "config_dir": d}
                                    for lane, d in (("claude", "/x/a"), ("claude#b", "/x/b"))}
        r = self.pin("sub/opus")
        self.assertIn("api/claude-opus-5-5", r["error"])
        self.assertFalse(r.get("client_error"))

    def test_oversized_pinned_request_is_refused_and_a_normal_one_passes(self):
        # the same assembled chars/4 estimate core.resolve gates on in execution mode; one over the ceiling is refused
        cap = core.lane_ceiling("claude", "claude-opus-5-5")
        body = {"model": "sub/opus", "tools": [{"name": "Read"}], "messages": [{"role": "user", "content": "x"}]}
        body["messages"][0]["content"] = "x" * ((cap + 25) * 4 - len(json.dumps(body)) + 1)
        self.assertEqual(routing.request_fit(body)["input_tokens"], cap + 25)
        r = routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin="python3")
        self.assertIn("does not fit", r.get("error", ""))
        self.assertFalse(r.get("client_error"))             # not a 400: nothing in the model name is wrong
        self.assertTrue(r.get("fit_blocked"))                 # the server answers 413: retrying unchanged cannot succeed
        self.assertNotIn("route_id", r)                       # refused before a routes row is written
        self.assertFalse((core.STATE / "router.db").exists())
        self.assertNotIn("error", self.pin("sub/opus"))

    def test_a_configured_byte_ceiling_refuses_a_pin_as_it_refuses_resolve(self):
        # the pin and core.resolve share core.lane_byte_ceilings: a byte cap on the claude lane gates both
        keep = core._context_cfg
        core._context_cfg = lambda: {**keep(), "ceiling_bytes": {"claude": 500}}
        try:
            body = {"model": "sub/opus", "tools": [{"name": "Read"}], "messages": [{"role": "user", "content": "x" * 600}]}
            r = routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin="python3")
            self.assertTrue(r.get("fit_blocked"))
            self.assertIn("byte ceiling 500", r["error"])
            self.assertEqual(core.lane_byte_ceilings()["claude"], 500)
        finally:
            core._context_cfg = keep

    def health(self, a, b):
        core.lane_health = lambda: {"claude": {"ok": True, "pools": {}, "config_dir": "/x/a", **a},
                                    "claude#b": {"ok": True, "pools": {}, "config_dir": "/x/b", **b}}

    def test_expiring_credit_beats_lower_scarcity_as_in_resolve(self):
        self.two_accounts()
        self.health({"scarcity": -0.4, "expiring_pct": 0}, {"scarcity": -0.05, "expiring_pct": 60})
        self.assertEqual(self.pin("sub/sonnet")["provider"], "claude-cli#b")
        # resolve's static path ranks the same two accounts the same way (one canonical ranking, core.static_scarcity_rank)
        keep = core.scored_candidates
        core.scored_candidates = lambda *a, **k: None
        try:
            r = core.resolve({"answers": None, "degraded": True, "engine": "s1", "engine_ms": 0}, execution=True,
                             accounts_aware=True, allowed_routes=(("claude-cli", "claude-sonnet-5"), ("claude-cli#b", "claude-sonnet-5")))
        finally:
            core.scored_candidates = keep
        self.assertEqual(r["chosen"]["provider"], "claude-cli#b")

    def test_reserve_account_is_used_only_when_the_non_reserve_one_is_spent(self):
        core._vendor_accounts = lambda *a: [
            {"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": True, "config_dir": "/x/a"},
            {"lane": "claude#b", "provider": "claude-cli#b", "account": "b", "reserve": False, "config_dir": "/x/b"}]
        self.health({"scarcity": -0.4, "expiring_pct": 60}, {"scarcity": 0.3})
        self.assertEqual(self.pin("sub/opus")["provider"], "claude-cli#b")
        self.health({"scarcity": -0.4}, {"ok": False, "why": "week 100% used"})
        self.assertEqual(self.pin("sub/opus")["provider"], "claude-cli")

    def test_toolless_pinned_call_logs_its_own_last_user_message(self):
        # a title/summary call: no tools, so what is on record is the call itself, not the conversation's first task
        body = {"model": "sub/haiku", "messages": [{"role": "user", "content": "refactor the billing module"},
                                                   {"role": "assistant", "content": "done"},
                                                   {"role": "user", "content": "write a 5-word title"}]}
        r = routing.choose(core, body, client="claude-cli", conversation_id="conv", claude_bin="python3")
        with sqlite3.connect(core.STATE / "router.db") as db:
            row = db.execute("SELECT request FROM routes WHERE turn_id=?", (r["route_id"],)).fetchone()
        self.assertEqual(row, ("write a 5-word title",))

    def test_pin_rows_stay_out_of_s1_health_metrics(self):
        for _ in range(3):
            self.assertNotIn("error", self.pin("sub/opus"))
        with sqlite3.connect(core.STATE / "router.db") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM routes WHERE engine='pin' AND engine_ms IS NULL").fetchone()[0], 3)
        scored = {"band": "R2", "chosen": {"lane": "codex", "provider": "codex-cli", "model": "gpt-5.6-terra"},
                  "answers": None, "subagents": 0, "verifier": False, "hard_rules": []}
        core.log_route("scored-1", {"session_id": "s", "platform": "provider:claude-cli", "request": "x"},
                       {"answers": None, "degraded": True, "engine": "s1", "engine_ms": 1000}, scored)
        s = core.usage_summary()
        self.assertEqual(s["total_routes"], 4)                                  # pins stay in the route totals
        self.assertEqual(s["by_band"], {"pin": 3, "R2": 1})
        self.assertEqual(s["by_lane"], {"claude/claude-opus-5-5": 3, "codex/gpt-5.6-terra": 1})
        self.assertEqual(s["degraded"], {"count": 1, "of": 1})                  # only the scored row
        self.assertEqual((s["s1_latency_ms"]["median"], s["s1_latency_ms"]["window"]), (1000, 1))

    def test_account_with_a_cross_login_reading_is_skipped(self):
        # the reading cached under lane `claude` was sampled from another login (config_dir differs): not trusted
        self.two_accounts()
        core.lane_health = lambda: {"claude": {"ok": True, "scarcity": -0.9, "pools": {}, "config_dir": "/x/other"},
                                    "claude#b": {"ok": True, "scarcity": 0.4, "pools": {}, "config_dir": "/x/b"}}
        r = self.pin("sub/opus")
        self.assertEqual((r["provider"], r["account"]), ("claude-cli#b", "b"))
        [s] = r["route"]["skipped"]
        self.assertEqual((s["lane"], s["model"]), ("claude", "claude-opus-5-5"))
        self.assertIn("stale cross-account reading", s["why"])

    def test_fable_pin_refuses_an_account_whose_fable_pool_is_spent(self):
        self.two_accounts()
        fable_spent = {"ok": True, "scarcity": 0.1, "config_dir": "/x/a",
                       "pools": {"fable": {"ok": False, "scarcity": 1.0, "why": "fable pool 100% used"}}}
        core.lane_health = lambda: {"claude": fable_spent,
                                    "claude#b": {"ok": True, "scarcity": 0.6, "config_dir": "/x/b",
                                                 "pools": {"fable": {"ok": True, "scarcity": 0.6}}}}
        r = self.pin("sub/fable")
        self.assertEqual((r["provider"], r["model"]), ("claude-cli#b", "claude-fable-5-1"))
        self.assertEqual(r["route"]["skipped"][0]["pool"], "fable")
        # alone, the account with general-window room but a spent Fable pool is refused, not used
        core._vendor_accounts = lambda *a: [
            {"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": False, "config_dir": "/x/a"}]
        core.lane_health = lambda: {"claude": fable_spent}
        r = self.pin("sub/fable")
        self.assertIn("fable pool 100% used", r["error"])
        self.assertIn("api/claude-fable-5-1", r["error"])

    def test_refusal_names_stale_readings_and_does_not_suggest_the_api_pin_for_them(self):
        self.two_accounts()
        core.lane_health = lambda: {"claude": {"ok": True, "pools": {}, "config_dir": "/x/other"},
                                    "claude#b": {"ok": True, "pools": {}, "config_dir": "/x/other"}}
        r = self.pin("sub/opus")
        self.assertIn("different login", r["error"])
        self.assertNotIn("spent", r["error"])
        self.assertNotIn("api/", r["error"])

    def test_refusal_for_fit_plus_spent_names_both_and_suggests_api_for_the_spent_one(self):
        self.two_accounts()
        keep = core.lane_ceiling
        core.lane_ceiling = lambda lane, model="", transport=None: 10 if lane == "claude" else keep(lane, model, transport)
        try:
            core.lane_health = lambda: {"claude": {"ok": True, "pools": {}, "config_dir": "/x/a"},
                                        "claude#b": {"ok": False, "why": "week 100% used", "pools": {}, "config_dir": "/x/b"}}
            r = self.pin("sub/opus")
            self.assertIn("too large", r["error"])
            self.assertIn("no capacity left", r["error"])
            self.assertIn("week 100% used", r["error"])
            self.assertIn("pin api/claude-opus-5-5", r["error"])
            # fit + stale only: nothing was spent, so api/ is not offered
            core.lane_health = lambda: {"claude": {"ok": True, "pools": {}, "config_dir": "/x/a"},
                                        "claude#b": {"ok": True, "pools": {}, "config_dir": "/x/other"}}
            r = self.pin("sub/opus")
            self.assertIn("too large", r["error"])
            self.assertIn("different login", r["error"])
            self.assertNotIn("api/", r["error"])
        finally:
            core.lane_ceiling = keep

    def test_pin_route_skipped_has_resolves_shape_and_renders_in_the_footer(self):
        self.two_accounts()
        core.lane_health = lambda: {"claude": {"ok": False, "why": "week 100% used", "pools": {}, "config_dir": "/x/a"},
                                    "claude#b": {"ok": True, "scarcity": 0.4, "pools": {}, "config_dir": "/x/b"}}
        r = self.pin("sub/opus")
        self.assertEqual(r["route"]["skipped"], [{"lane": "claude", "model": "claude-opus-5-5", "pool": None,
                                                  "why": "week 100% used"}])
        line = core.footer(r["route"], r["decision"])
        self.assertIn("skipped claude/claude-opus-5-5 (week 100% used)", line)
        with sqlite3.connect(core.STATE / "router.db") as db:
            stored = json.loads(db.execute("SELECT skipped FROM routes WHERE turn_id=?", (r["route_id"],)).fetchone()[0])
        self.assertEqual(stored, r["route"]["skipped"])

    def test_usage_summary_clamps_a_negative_limit(self):
        for _ in range(3):
            self.assertNotIn("error", self.pin("sub/opus"))
        self.assertEqual(core.usage_summary(limit=-1)["recent"], [])     # SQLite LIMIT -1 would be unlimited
        self.assertEqual(core.usage_summary(limit=0)["recent"], [])
        self.assertEqual(len(core.usage_summary(limit=2)["recent"]), 2)

    def test_usage_summary_on_a_routes_table_without_an_engine_column(self):
        core.STATE.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(core.STATE / "router.db") as db:
            db.execute("CREATE TABLE routes (ts REAL, platform TEXT, band TEXT, lane TEXT, model TEXT, degraded INTEGER, "
                       "engine_ms INTEGER, turn_ms INTEGER, response_chars INTEGER)")
            db.executemany("INSERT INTO routes VALUES (?,?,?,?,?,?,?,?,?)",
                           [(1.0, "cli", "R1", "codex", "m", 0, 100, None, None),
                            (2.0, "cli", "R2", "codex", "m", 1, 300, None, None),
                            (3.0, "cli", "R2", "codex", "m", 0, None, None, None)])
        s = core.usage_summary()
        self.assertNotIn("error", s)
        self.assertEqual(s["total_routes"], 3)
        self.assertEqual(s["degraded"], {"count": 1, "of": 3})
        self.assertEqual((s["s1_latency_ms"]["median"], s["s1_latency_ms"]["window"]), (200, 2))
        self.assertEqual(len(s["recent"]), 3)

    def test_broken_accounts_config_refuses_claude(self):
        def broken(*a):
            raise core.AccountsConfigError("accounts.json unreadable")
        core._vendor_accounts = broken
        self.assertIn("unreadable", self.pin("sub/opus")["error"])


class MinLeftFloorTests(unittest.TestCase):
    """x-cortados-min-left-pct: the provider is the single enforcer (provider/floor.py). Accounts below the floor are
    excluded before ranking, the turn is reserved on the account that executes it, absent header = unchanged."""

    def setUp(self):
        import time as _t
        self.now = _t.time()
        self._keep = (core.STATE, core.lane_health, core.decide, core._vendor_accounts, core.subscription_readings,
                      core.claude_login_identity, core.codex_login_identity)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-floor-"))
        # The effective login of the env the bridge would start the CLI with: its CLAUDE_CONFIG_DIR, else ~/.claude.
        core.claude_login_identity = lambda env: env.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        core.lane_health = lambda: {}
        core.decide = lambda text, ctx=None: (_ for _ in ()).throw(AssertionError("pin must not be scored"))
        core._vendor_accounts = lambda *a: [
            {"lane": "claude", "provider": "claude-cli", "account": "a", "reserve": False, "config_dir": "/x/a"},
            {"lane": "claude#b", "provider": "claude-cli#b", "account": "b", "reserve": True, "config_dir": "/x/b"}]

    def tearDown(self):
        (core.STATE, core.lane_health, core.decide, core._vendor_accounts, core.subscription_readings,
         core.claude_login_identity, core.codex_login_identity) = self._keep

    def win(self, used, pool="all", stale=False, reset=None, name=None):
        return {"window": name or ("week" if pool == "all" else f"week ({pool})"), "pool": pool, "used_pct": used,
                "headroom_pct": None, "stale": stale,
                "reset_epoch": None if reset == "unknown" else (self.now + 86400 if reset is None else reset)}

    def readings(self, a, b, **extra):
        rd = {"claude": {"fetched_at": self.now - 60, "failed": False, "config_dir": "/x/a", "login": "/x/a", "windows": a},
              "claude#b": {"fetched_at": self.now - 60, "failed": False, "config_dir": "/x/b", "login": "/x/b", "windows": b}}
        rd.update(extra)
        core.subscription_readings = lambda: rd
        return rd

    def pin(self, model, floor=None, codex_home=None):
        body = {"model": model, "messages": [{"role": "user", "content": "x"}]}
        return routing.choose(core, body, client="claude-cli", conversation_id="c", claude_bin="python3",
                              codex_bin="python3", **({"min_left_pct": floor} if floor is not None else {}),
                              **({"codex_home": codex_home} if codex_home else {}))

    def ledger(self):
        p = core.STATE / "floor-reservations.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_absent_header_keeps_todays_choice_and_reserves_nothing(self):
        core.subscription_readings = lambda: (_ for _ in ()).throw(AssertionError("no floor: readings not consulted"))
        r = self.pin("sub/opus")
        self.assertEqual(r["provider"], "claude-cli")                       # reserve spared, as before
        self.assertNotIn("min_left_pct", r)
        self.assertEqual(self.ledger(), [])

    def test_reserves_on_the_account_that_executes(self):
        self.readings([self.win(40)], [self.win(10)])
        lanes = []
        for _ in range(3):
            r = self.pin("sub/opus", 5)
            lanes.append((r["provider"], r["route"]["chosen"]["lane"]))
            self.assertEqual(r["min_left_pct"], 5)
        self.assertEqual([row["lane"] for row in self.ledger()], [lane for _, lane in lanes])
        self.assertEqual(set(lanes), {("claude-cli", "claude")})

    def test_low_account_excluded_before_ranking_and_reservations_count(self):
        # claude at 3% left is out; claude#b (a reserve) at 8% left takes three 1% turns, then everything is refused
        self.readings([self.win(97)], [self.win(92)])
        got = [self.pin("sub/opus", 5) for _ in range(4)]
        self.assertEqual([r.get("provider") for r in got[:3]], ["claude-cli#b"] * 3)
        self.assertTrue(got[3].get("quota_blocked"))
        self.assertIn("outstanding", got[3]["error"])
        self.assertEqual([row["lane"] for row in self.ledger()], ["claude#b"] * 3)

    def test_fable_pool_costs_twice_per_turn(self):
        self.readings([self.win(10), self.win(93, "fable")], [self.win(10), self.win(93.5, "fable")])
        r = self.pin("sub/fable", 5)                                       # claude: 7% - 2% = 5% left: admitted
        self.assertEqual(r.get("provider"), "claude-cli")
        r = self.pin("sub/fable", 5)                                       # claude: 7% - 4% < 5%; claude#b: 6.5% - 2% < 5%
        self.assertTrue(r.get("quota_blocked"), r)

    def test_carried_forward_low_or_figureless_pool_holds(self):
        self.readings([self.win(10), self.win(98, "fable", stale=True)], [self.win(10), self.win(None, "fable", stale=True)])
        r = self.pin("sub/fable", 5)
        self.assertTrue(r.get("quota_blocked"))
        self.assertIn("carried-forward", r["error"])
        self.readings([self.win(10)], [self.win(10)])                     # no pool reading at all: general bounds it
        self.assertEqual(self.pin("sub/fable", 5).get("provider"), "claude-cli")

    def test_stale_reading_unknown_reset_near_reset_and_other_login_hold(self):
        rd = self.readings([self.win(10, reset=self.now + 1800)], [self.win(10, reset="unknown")])
        r = self.pin("sub/opus", 5)
        self.assertTrue(r.get("quota_blocked"))
        self.assertIn("resets within", r["error"]); self.assertIn("reset time unknown", r["error"])
        rd["claude"] = {"fetched_at": self.now - 4000, "failed": False, "login": "/x/a", "windows": [self.win(10)]}
        rd["claude#b"] = {"fetched_at": self.now - 60, "failed": False, "login": "/x/other", "windows": [self.win(10)]}
        r = self.pin("sub/opus", 5)
        self.assertIn("older than", r["error"]); self.assertIn("not sampled from the login", r["error"])
        del rd["claude#b"]["login"]                                       # no identity stamp: unproven, held
        self.assertTrue(self.pin("sub/opus", 5).get("quota_blocked"))

    def test_default_login_is_proven_by_login_not_config_dir(self):
        import os
        from unittest import mock
        core._vendor_accounts = lambda *a: [
            {"lane": "claude", "provider": "claude-cli", "account": None, "reserve": False, "config_dir": None}]
        rd = {"claude": {"fetched_at": self.now - 60, "failed": False, "config_dir": None, "login": "/a",
                         "windows": [self.win(10)]}}
        core.subscription_readings = lambda: rd
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/b"}):       # the provider inherited another login
            r = self.pin("sub/opus", 5)
            self.assertTrue(r.get("quota_blocked"), r)
            self.assertIn("not sampled from the login", r["error"])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/a"}):       # same login as the sampler: admitted
            self.assertEqual(self.pin("sub/opus", 5).get("provider"), "claude-cli")

    def test_one_conversation_is_charged_once_not_per_turn(self):
        self.readings([self.win(92)], [self.win(10)])                     # 8% left: three runs fit
        route = self.pin("sub/opus", 5)
        for _ in range(20):                                               # 20 continuations of the same run
            self.assertIsNone(routing.floor_turn(core, route, 5))
        self.assertEqual(len(self.ledger()), 1)
        # The reservation ages out (a reading sampled more than IN_FLIGHT_S after it now shows the work): the next turn
        # re-charges once, and only once.
        from cortados_router.provider import floor as floor_mod
        p = core.STATE / "floor-reservations.jsonl"
        rows = self.ledger()
        rows[0]["ts"] = self.now - 60 - floor_mod.IN_FLIGHT_S - 120
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertIsNone(routing.floor_turn(core, route, 5))
        self.assertIsNone(routing.floor_turn(core, route, 5))
        self.assertEqual([r["conv"] for r in self.ledger()], [route["route_id"]] * 2)
        p.write_text(json.dumps(rows[0]) + "\n")                       # back to one aged-out row: three new runs fit
        self.assertEqual([self.pin("sub/opus", 5).get("provider") for _ in range(3)], ["claude-cli"] * 3)
        self.assertNotEqual(self.pin("sub/opus", 5).get("provider"), "claude-cli")   # 4th RUN does not fit

    def test_fable_runs_admitted_without_a_pool_window_count_when_it_returns(self):
        self.readings([self.win(10)], [self.win(10)])                     # Fable panel missing
        for _ in range(2):
            self.assertEqual(self.pin("sub/fable", 5).get("provider"), "claude-cli")
        self.assertTrue(all("claude|pool|fable" in row["keys"] for row in self.ledger()))
        self.readings([self.win(10), self.win(90, "fable")], [self.win(10), self.win(99, "fable")])
        r = self.pin("sub/fable", 5)                                      # 10% - (2 outstanding + 1) x 2% = 4% < 5%
        self.assertTrue(r.get("quota_blocked"), r)
        self.assertIn("2 run(s) outstanding", r["error"])

    def test_codex_pin_under_a_floor_needs_the_reading_of_the_executing_home(self):
        core.codex_login_identity = lambda env: env.get("CODEX_HOME")    # the home codex_isolation.child_env sets
        codex = {"fetched_at": self.now - 60, "failed": False, "windows": [self.win(10)]}
        self.readings([self.win(10)], [self.win(10)], codex=codex)
        r = self.pin("sub/gpt-6.1-sol", 5, codex_home="/homes/provider")
        self.assertTrue(r.get("quota_blocked"))                          # a reading sampled before the stamp existed
        self.assertIn("not sampled from the login", r["error"])
        codex["login"] = "/homes/sampler"                                # sampled on another home: held
        self.assertTrue(self.pin("sub/gpt-6.1-sol", 5, codex_home="/homes/provider").get("quota_blocked"))
        codex["login"] = "/homes/provider"                               # the provider's own --codex-home: admitted
        r = self.pin("sub/gpt-6.1-sol", 5, codex_home="/homes/provider")
        self.assertEqual(r.get("provider"), "codex-cli", r)
        self.assertEqual(self.ledger()[-1]["lane"], "codex")
        self.assertEqual(r["codex_home"], "/homes/provider")               # the backend starts on the checked home
        # a continuation is checked against the session's bound home, whatever the provider's home resolves to now
        self.assertIsNone(routing.floor_turn(core, r, 5, "/homes/elsewhere"))
        codex["login"] = "/homes/elsewhere"
        self.assertIn("not sampled from the login", routing.floor_turn(core, r, 5, "/homes/elsewhere"))

    def test_codex_home_symlink_rotation_never_moves_the_checked_identity(self):
        import os
        root = Path(tempfile.mkdtemp(prefix="codex-homes-"))
        (root / "A").mkdir(); (root / "B").mkdir()
        link = root / "current"
        link.symlink_to(root / "A")
        a, b = os.path.realpath(root / "A"), os.path.realpath(root / "B")
        codex = {"fetched_at": self.now - 60, "failed": False, "login": a, "windows": [self.win(10)]}
        self.readings([self.win(10)], [self.win(10)], codex=codex)
        r = self.pin("sub/gpt-6.1-sol", 5, codex_home=str(link))           # admitted on A, through the link
        self.assertEqual((r.get("provider"), r.get("codex_home")), ("codex-cli", a))
        link.unlink(); link.symlink_to(root / "B")                         # rotated after admission, before startup
        self.assertEqual(r["codex_home"], a)                               # the backend still starts on A
        codex["login"] = b                                                 # the sampler now follows the link to B
        self.assertIn("not sampled from the login", routing.floor_turn(core, r, 5, str(link)))   # between turns
        codex["login"] = a
        self.assertIsNone(routing.floor_turn(core, r, 5, str(link)))

    def test_floor_only_tightens_a_spent_account_stays_out(self):
        self.readings([self.win(10)], [self.win(10)])
        core.lane_health = lambda: {"claude": {"ok": False, "why": "spent", "pools": {}, "config_dir": "/x/a"}}
        self.assertEqual(self.pin("sub/opus", 0)["provider"], "claude-cli#b")

    def test_continuation_turn_rechecks_the_sessions_account(self):
        rd = self.readings([self.win(92)], [self.win(10)])
        route = self.pin("sub/opus", 5)
        self.assertEqual(route["provider"], "claude-cli")                  # 8% left: this opening turn reserved
        self.assertIsNone(routing.floor_turn(core, route, 5))
        rd["claude"]["windows"] = [self.win(96)]                           # the account drops below the floor mid-run
        why = routing.floor_turn(core, route, 5)                           # refused, never moved to the healthy sibling
        self.assertIn("would pass", why)
        self.assertEqual({row["lane"] for row in self.ledger()}, {"claude"})
        self.assertIn("unknown", routing.floor_turn(core, None, 5))
        for lane in ("local", "local-studio"):                             # routed and pinned spellings of a local node
            local = {"model": "m", "route": {"chosen": {"lane": lane}}}
            self.assertIsNone(routing.floor_turn(core, local, 5))          # a local node has no subscription floor


class ExcludeModelTests(unittest.TestCase):
    """x-cortados-exclude-model: a new conversation stays off one model, so a review never runs on its builder's model."""

    def setUp(self):
        self._keep = (core.STATE, routing.execution_routing.select, core.lane_health)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-provider-exclude-"))   # never the live router.db
        core.lane_health = lambda: {}                        # never sample the vendor CLIs
        self.seen = {}

        def fake_select(core_, **kw):
            self.seen.update(kw)
            return {"provider": "codex-cli", "model": "gpt-x", "route": {"hard_rules": []}}
        routing.execution_routing.select = fake_select

    def tearDown(self):
        core.STATE, routing.execution_routing.select, core.lane_health = self._keep

    def body(self, model="cortados"):
        return {"model": model, "messages": [{"role": "user", "content": "review the diff"}]}

    def test_normalize_model(self):
        for raw, want in (("claude-opus-5-5[1m]", "claude-opus-5-5"), ("sub/opus", "claude-opus-5-5"),
                          ("opus", "claude-opus-5-5"), ("cortados", None), ("", None), (None, None),
                          ("sub", routing.SUB_DEFAULT), ("api", ""), ("API", ""), ("sub/", ""), ("api/", ""),
                          ("api/ ", ""), ("api/claude-sonnet-5", "claude-sonnet-5"),
                          ("Claude-Opus-5-5[1M]", "claude-opus-5-5"), ("SUB/Opus", "claude-opus-5-5"), ("CORTADOS", None),
                          ("opusplan", "claude-opus-5-5"), ("OpusPlan", "claude-opus-5-5"),
                          ("default", routing.SUB_DEFAULT), ("Default", routing.SUB_DEFAULT)):
            self.assertEqual(routing.normalize_model(raw), want, raw)
        self.assertEqual(routing.model_key("Claude-Haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_case_and_claude_code_menu_names_match_served_routes(self):
        for header, want in (("CLAUDE-OPUS-5-5", "claude-opus-5-5"), ("opusplan", "claude-opus-5-5"),
                             ("default", routing.SUB_DEFAULT), ("Claude-Haiku-4-5", "claude-haiku-4-5-20251001"),
                             ("GPT-5.6-Terra", "gpt-5.6-terra")):
            self.seen.clear()
            r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c", exclude_model=header)
            self.assertNotIn("error", r, header)
            self.assertEqual({m for _, m in self.seen["constraints"]["excluded_routes"]}, {want}, header)

    def test_choose_excludes_every_served_route_of_the_model(self):
        r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c", exclude_model="claude-opus-5-5[1m]")
        excluded = {tuple(p) for p in self.seen["constraints"]["excluded_routes"]}
        served = set(routing.served_routes(core))
        self.assertTrue(excluded)
        self.assertEqual(excluded, {(p, m) for p, m in served if m == "claude-opus-5-5"})
        self.assertFalse({m for _, m in excluded} - {"claude-opus-5-5"})
        self.assertEqual(self.seen["constraints"]["notes"], ["excluded by client: claude-opus-5-5"])

    def test_dated_and_undated_ids_match_both_ways(self):
        self.assertEqual(routing.model_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        served = {m for _, m in routing.served_routes(core)}
        self.assertIn("claude-haiku-4-5-20251001", served)
        for header in ("claude-haiku-4-5", "claude-haiku-4-5-20251001"):
            routing.choose(core, self.body(), client="claude-cli", conversation_id="c", exclude_model=header)
            excluded = {m for _, m in self.seen["constraints"]["excluded_routes"]}
            self.assertEqual(excluded, {"claude-haiku-4-5-20251001"}, header)
        # the other way round: a dated header against an undated served id
        r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c",
                           exclude_model="claude-opus-5-5-20260101")
        self.assertEqual({m for _, m in self.seen["constraints"]["excluded_routes"]}, {"claude-opus-5-5"})
        self.assertNotIn("error", r)
        self.seen.clear()
        r = routing.choose(core, self.body("sub/opus"), client="claude-cli", conversation_id="c",
                           exclude_model="claude-opus-5-5-20260101")
        self.assertTrue(r["client_error"])

    def test_an_unmatched_exclusion_is_a_client_error(self):
        # loud beats silent: routing normally could put the review on its builder's (mistyped) model
        r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c", exclude_model="no-such-model")
        self.assertTrue(r["client_error"])
        self.assertEqual(r["error"], "x-cortados-exclude-model: no-such-model names no model this endpoint serves; "
                                     "the review could not be kept off it")
        self.assertEqual(self.seen, {})                      # select (and its route log) never ran

    def choose(self, exclude, **kw):
        return routing.choose(core, self.body(), client="claude-cli", conversation_id="c", claude_bin="python3",
                              codex_bin="python3", exclude_model=exclude, **kw)

    def test_an_emptied_pool_says_the_client_excluded_the_model(self):
        # the excluded model's only route was healthy: the exclusion caused the refusal
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = self.choose("opus")
        self.assertTrue(r["error"].endswith("(the client excluded claude-opus-5-5 via x-cortados-exclude-model)"))
        self.assertTrue(r["client_error"])                   # 400, not a 503 the client CLI would retry until timeout

    def test_a_degraded_failure_with_an_exclusion_stays_a_server_error(self):
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits.",
                                                                "degraded": True}
        r = self.choose("opus")
        self.assertNotIn("client_error", r)
        self.assertIn("S1 is unavailable", r["error"])
        self.assertNotIn("the client excluded", r["error"])

    def test_excluding_a_spent_model_is_not_blamed_on_the_client(self):
        # every lane spent: the excluded model could not have served it either, so the failure is the server's (503)
        core.lane_health = lambda: {lane: {"ok": False, "why": "spent", "pools": {}}
                                    for lane in ("claude", "codex", "cursor", "antigravity")}
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = self.choose("opus")
        self.assertNotIn("client_error", r)
        self.assertNotIn("the client excluded", r["error"])

    def test_excluding_a_model_whose_binary_is_missing_is_not_blamed_on_the_client(self):
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c",
                           claude_bin="no-such-binary-cortados", codex_bin="python3", exclude_model="opus")
        self.assertNotIn("client_error", r)

    def test_excluding_the_only_healthy_route_is_a_client_error(self):
        # Claude healthy, Codex spent: excluding the Claude model is what emptied the pool
        core.lane_health = lambda: {"codex": {"ok": False, "why": "spent", "pools": {}}}
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = self.choose("opus")
        self.assertTrue(r["client_error"])

    def test_an_unknown_health_falls_back_to_not_degraded(self):
        def broken():
            raise RuntimeError("sampler down")
        core.lane_health = broken
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        self.assertTrue(self.choose("opus")["client_error"])

    def test_a_lane_prefix_without_a_model_is_a_client_error(self):
        for header in ("api", "API", "sub/", "api/"):
            self.seen.clear()
            r = self.choose(header)
            self.assertTrue(r["client_error"], header)
            self.assertEqual(r["error"], f"x-cortados-exclude-model: {header} names no model this endpoint serves; "
                                         "the review could not be kept off it")
            self.assertEqual(self.seen, {}, header)         # select never ran
        # bare `sub` still names the default subscription model
        r = self.choose("sub")
        self.assertNotIn("error", r)
        self.assertEqual({m for _, m in self.seen["constraints"]["excluded_routes"]}, {routing.SUB_DEFAULT})

    def test_a_route_onto_the_excluded_model_is_refused_not_served(self):
        # the guarantee holds even if select ignored excluded_routes
        routing.execution_routing.select = lambda core_, **kw: {"router": "x", "provider": "claude-cli",
                                                                "model": "claude-opus-5-5", "route": {"hard_rules": []}}
        r = self.choose("Claude-Opus-5-5-20260101")
        self.assertTrue(r["error"].startswith("internal: routed to the excluded model claude-opus-5-5"))
        self.assertNotIn("client_error", r)                  # a server fault: 503
        self.assertNotIn("provider", r)

    def test_a_routing_error_without_the_header_stays_a_server_error(self):
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = routing.choose(core, self.body(), client="claude-cli", conversation_id="c")
        self.assertNotIn("client_error", r)

    def test_no_exclusion_without_the_header(self):
        routing.choose(core, self.body(), client="claude-cli", conversation_id="c")
        self.assertNotIn("excluded_routes", self.seen["constraints"])

    def test_a_comma_list_excludes_every_listed_model(self):
        r = self.choose("claude-opus-5-5, ,sonnet")
        self.assertNotIn("error", r)
        excluded = {m for _, m in self.seen["constraints"]["excluded_routes"]}
        self.assertEqual(excluded, {"claude-opus-5-5", "claude-sonnet-5"})
        self.assertEqual(self.seen["constraints"]["notes"], ["excluded by client: claude-opus-5-5, claude-sonnet-5"])

    def test_an_unmatched_listed_model_is_a_client_error(self):
        r = self.choose("claude-opus-5-5,no-such-model")
        self.assertTrue(r["client_error"])
        self.assertEqual(r["error"], "x-cortados-exclude-model: no-such-model names no model this endpoint serves; "
                                     "the review could not be kept off it")
        self.assertEqual(self.seen, {})

    def test_a_route_onto_any_listed_model_is_refused(self):
        routing.execution_routing.select = lambda core_, **kw: {"router": "x", "provider": "claude-cli",
                                                                "model": "claude-sonnet-5", "route": {"hard_rules": []}}
        r = self.choose("claude-opus-5-5,claude-sonnet-5")
        self.assertTrue(r["error"].startswith("internal: routed to the excluded model claude-sonnet-5"))

    def test_an_emptied_pool_names_every_listed_model(self):
        routing.execution_routing.select = lambda core_, **kw: {"error": "No authenticated, available model fits."}
        r = self.choose("opus,sonnet")
        self.assertTrue(r["client_error"])
        self.assertTrue(r["error"].endswith(
            "(the client excluded claude-opus-5-5, claude-sonnet-5 via x-cortados-exclude-model)"))

    def test_pin_on_the_second_listed_model_is_a_client_error(self):
        r = routing.choose(core, self.body("sub/opus"), client="claude-cli", conversation_id="c",
                           exclude_model="claude-sonnet-5,claude-opus-5-5")
        self.assertTrue(r["client_error"])
        self.assertEqual(r["error"], "sub/claude-opus-5-5 is the excluded model (x-cortados-exclude-model); "
                                     "pin another model or use cortados")
        self.assertEqual(self.seen, {})

    def test_pin_on_the_excluded_model_is_a_client_error(self):
        r = routing.choose(core, self.body("sub/opus"), client="claude-cli", conversation_id="c",
                           exclude_model="claude-opus-5-5")
        self.assertTrue(r["client_error"])
        self.assertIn("excluded model", r["error"])
        self.assertEqual(self.seen, {})


if __name__ == "__main__":
    unittest.main()
