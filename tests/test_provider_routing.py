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
        r = self.pin("sub/gpt-5.6-terra")
        self.assertTrue(r.get("client_error"))
        self.assertIn("not a Claude model", r["error"])

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

    def test_broken_accounts_config_refuses_claude(self):
        def broken(*a):
            raise core.AccountsConfigError("accounts.json unreadable")
        core._vendor_accounts = broken
        self.assertIn("unreadable", self.pin("sub/opus")["error"])


if __name__ == "__main__":
    unittest.main()
