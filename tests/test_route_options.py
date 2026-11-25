"""Route picker step 4 (docs/ORCHESTRATOR-ROUTE-PICKER.md §2, §7): GET /v1/cortados/route-options, the models a pick
can name per band, from cached readings only. Fakes only: the route-pick tests' fake catalogue and lane health, a fake
scarcity module that fails if anything asks it to refresh, a fake local gate. No vendor CLI, no real provider state."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest import mock

from cortados_router import api_credits, core
from cortados_router.provider import local_lane, route_options, routing
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401
from tests.test_provider import TOKEN, EndpointHarness
from tests.test_route_pick import LOCAL_NODE, MS, OPUS, SOL, Catalogue, row

# A model whose only reach row is Cursor's: never a provider candidate (routing.SERVED_PROVIDERS).
CURSOR_ONLY = {"family": "claude-sonnet-5-5", "ceiling": False, "proxy": False, "index": {"general": 85.0, "code": 80.0},
               "reach": [{"lane": "cursor", "provider": "cursor-cli", "model": "claude-sonnet-5-5"}]}
MS_PLUS = {**MS, "models": MS["models"] + [CURSOR_ONLY],
           "local_models": [{"family": "qwen-27b", "thinking": "off", "index": {"general": 16.7},
                             "reach": [{"lane": "local", "provider": "ollama", "model": "qwen3.8-27b-heretic:q4"}]}]}


class NoRefreshSc:
    """The scarcity module: a refresh (which would open the vendor CLIs) fails the test."""

    def current(self, *a, refresh=True, **k):
        if refresh:
            raise AssertionError("route options asked scarcity for a fresh reading (would open a vendor CLI)")
        return {"lanes": {}}

    def pool_for(self, lane, model, pools):
        return None


class OptionsBase(Catalogue):

    def setUp(self):
        super().setUp()
        core.model_scores = lambda: MS_PLUS
        core._sc_mod = NoRefreshSc()
        self.health_calls = []

        def health(**k):
            self.health_calls.append(k)
            return self.health
        core.lane_health = health
        self.health["claude"] = {**row("claude"), "tightest": {"window": "week (all models)", "used_pct": 38.0,
                                                               "reset_epoch": 1796000000}}
        self.health["claude#b"] = row("claude#b", ok=False)
        self.health.pop("codex")                         # no cached reading for codex: unread, never a number
        self.api_state = {"on": False, "headroom_usd": 0.0}
        p = mock.patch.object(api_credits, "state", lambda now=None, **k: dict(self.api_state))
        p.start(); self.addCleanup(p.stop)

    def view(self, band, local_view=None):
        return route_options.collect(core, band, local_view=local_view, now=1795000000.0)


class CloudSectionTests(OptionsBase):

    def test_cached_readings_only(self):
        v = self.view("R3")
        self.assertEqual(v["errors"], [])
        self.assertTrue(self.health_calls)
        self.assertTrue(all(k == {"refresh": False} for k in self.health_calls), self.health_calls)

    def test_band_lists_models_clearing_its_reference(self):
        ids = [m["id"] for m in self.view("R3")["cloud"]["models"]]
        # R3 = 67 general in the fake catalogue: Astra 100, Opus 99.8, Nova 92, Sol 6.1 90.1, Sol 5.6 79.6
        self.assertEqual(ids, ["gpt-6-astra", OPUS, "gpt-7-nova", SOL, "gpt-5.6-sol"])
        r4 = [m["id"] for m in self.view("R4")["cloud"]["models"]]
        self.assertEqual(r4, ["gpt-6-astra", OPUS])
        below = [m["id"] for m in self.view("R4")["cloud"]["below_band"]]
        self.assertIn(SOL, below)                            # namable, but not the band's own list

    def test_cursor_only_models_absent_and_counted(self):
        c = self.view("R3")["cloud"]
        self.assertNotIn("claude-sonnet-5-5", [m["id"] for m in c["models"]] + [m["id"] for m in c["below_band"]])
        self.assertEqual(c["not_served"]["count"], 1)
        self.assertEqual(c["not_served"]["models"], ["claude-sonnet-5-5"])
        self.assertIn("Cursor", c["not_served"]["why"])

    def test_face_floor_rows_flagged(self):
        r0 = {m["id"]: m for m in self.view("R0")["cloud"]["models"]}
        self.assertTrue(r0["claude-haiku-4-5-20251001"]["below_face_floor"])
        self.assertFalse(r0[OPUS]["below_face_floor"])
        self.assertEqual(self.view("R0")["cloud"]["reference"]["general"], 0.0)

    def test_short_use_cases_and_ceiling(self):
        m = {x["id"]: x for x in self.view("R3")["cloud"]["models"]}
        self.assertEqual(m[SOL]["short_of"], ["code"])         # 66.2 < R3 code 67
        self.assertEqual(m[OPUS]["short_of"], [])
        self.assertTrue(m["gpt-6-astra"]["ceiling"])
        self.assertFalse(m[SOL]["ceiling"])
        self.assertEqual(m[OPUS]["name"], "Claude Opus 5.5")

    def test_per_account_state(self):
        m = {x["id"]: x for x in self.view("R3")["cloud"]["models"]}
        routes = {r["provider"]: r for r in m[OPUS]["routes"]}
        self.assertEqual(set(routes), {"claude-cli", "claude-cli#b"})
        a, b = routes["claude-cli"], routes["claude-cli#b"]
        self.assertEqual((a["account"], a["state"], a["left_pct"]), ("A", "ok", 62.0))
        self.assertEqual(a["resets_at"], "2026-11-30 00:53 UTC")
        self.assertEqual((b["account"], b["state"], b["left_pct"]), ("B", "spent", 0.0))
        codex = m[SOL]["routes"]
        self.assertEqual([(r["provider"], r["state"], r["left_pct"]) for r in codex], [("codex-cli", "unread", None)])

    def test_api_credit_lane_when_offered(self):
        self.api_state = {"on": True, "headroom_usd": 142.317}
        m = {x["id"]: x for x in self.view("R3")["cloud"]["models"]}
        api = [r for r in m[OPUS]["routes"] if r["provider"] == routing.API_PROVIDER]
        self.assertEqual([(r["state"], r["left_usd"]) for r in api], [("ok", 142.32)])
        self.assertFalse([r for r in m[SOL]["routes"] if r["provider"] == routing.API_PROVIDER])

    def test_metadata_only(self):
        self.api_state = {"on": True, "headroom_usd": 10.0, "reason": "/secret/path"}
        text = json.dumps(self.view("R3")) + json.dumps(self.view("R0"))
        for leak in ("/x/a", "/x/b", "config_dir", "/secret", "login", TOKEN):
            self.assertNotIn(leak, text)

    def test_unreadable_scores_are_an_error_not_an_empty_list(self):
        core.model_scores = lambda: {"models": [], "reference_index": {}}
        v = self.view("R3")
        self.assertIsNone(v["cloud"])
        self.assertEqual(v["errors"], [{"source": "cloud", "error": "ValueError"}])

    def test_band_parsing(self):
        self.assertEqual(route_options.parse_band({"band": ["r3"]}), "R3")
        self.assertEqual(route_options.parse_band({"band": ["LOCAL"]}), "local")
        for bad in ({}, {"band": ["R5"]}, {"band": ["R3", "R4"]}, {"band": ["any"]}):
            with self.subTest(bad=bad), self.assertRaises(route_options.BandError):
                route_options.parse_band(bad)


class LocalSectionTests(OptionsBase):

    def test_local_section_from_a_fake_gate(self):
        self.local_nodes = [LOCAL_NODE]
        gate = {"nodes": [{"node": "pro", "reachable": True, "switch": "off", "power": "ac", "allowed": False,
                           "models_loaded": ["qwen3.8-27b-heretic:q4"]}]}
        v = self.view("local", local_view=lambda: gate)
        self.assertNotIn("cloud", v)
        loc = v["local"]
        self.assertEqual(loc["status"], "ok")
        self.assertFalse(loc["enabled"])
        self.assertEqual(loc["note"], "local orchestrator: coming in the next step (plan-and-delegate profile)")
        self.assertEqual(loc["models"], [{"id": "qwen-27b", "tag": "qwen3.8-27b-heretic:q4", "name": "Qwen 27B",
                                          "node": "pro", "node_label": "Pro", "context_length": 131072,
                                          "index": {"off": 16.7}, "state": "switch off", "loaded": True}])
        gate["nodes"][0].update(switch="on", allowed=True)
        self.assertEqual(self.view("local", local_view=lambda: gate)["local"]["models"][0]["state"], "available")
        gate["nodes"][0].update(reachable=False)
        self.assertEqual(self.view("local", local_view=lambda: gate)["local"]["models"][0]["state"],
                         "node unreachable")

    def test_a_cloud_band_never_asks_the_gates(self):
        self.local_nodes = [LOCAL_NODE]
        v = self.view("R3", local_view=lambda: self.fail("a cloud band asked the local gates"))
        self.assertNotIn("local", v)

    def test_misconfigured_local_config_said_as_such(self):
        def broken(path=None):
            raise local_lane.LocalConfigError("bad block in /Users/someone/.config/local.json", "pro")
        with mock.patch.object(local_lane, "nodes", broken):
            loc = self.view("local")["local"]
        self.assertEqual(loc["status"], "misconfigured")
        self.assertIn("local node pro is misconfigured", loc["error"])
        self.assertNotIn("/Users", json.dumps(loc))

    def test_clashing_local_name_is_misconfigured(self):
        self.local_nodes = [{**LOCAL_NODE, "models": [{**LOCAL_NODE["models"][0], "alias": OPUS}]}]
        self.assertEqual(self.view("local")["local"]["status"], "misconfigured")


class EndpointTests(EndpointHarness):
    """The HTTP surface: owner token (or a dashboard session), 400 for a bad band, the server's own collect."""

    def get(self, path, token=TOKEN):
        req = urllib.request.Request(self.url + path, headers={"authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())

    def test_owner_token_band_and_errors(self):
        seen = []
        self.httpd.route_options_view = lambda band: seen.append(band) or {"band": band, "cloud": {"models": []}}
        self.assertEqual(self.get(route_options.PATH + "?band=r3"), (200, {"band": "R3", "cloud": {"models": []}}))
        self.assertEqual(seen, ["R3"])
        for path, code in ((route_options.PATH + "?band=R9", 400), (route_options.PATH, 400)):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(route_options.PATH + "?band=R3", token="wrong")
        self.assertEqual(cm.exception.code, 401)

    def test_a_failing_view_is_a_clean_500(self):
        def boom(band):
            raise RuntimeError("/secret/path")
        self.httpd.route_options_view = boom
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(route_options.PATH + "?band=R3")
        self.assertEqual(cm.exception.code, 500)
        self.assertNotIn("/secret", cm.exception.read().decode())

    def test_dashboard_session_scope_covers_it(self):
        from cortados_router.provider import dashboard_auth
        self.assertIn(("GET", route_options.PATH), dashboard_auth.SESSION_SCOPES["dashboard:read"])


if __name__ == "__main__":
    import unittest
    unittest.main()
