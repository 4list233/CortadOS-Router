"""Offline tests for the router core: band resolution, hard rules, table validation, sub-agent counts. No model calls."""
import importlib.util, json, os, sys, tempfile, unittest
from pathlib import Path
os.environ.setdefault("CORTADOS_HERMES_HOME", tempfile.mkdtemp(prefix="cortados-router-test-"))
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("core", str(ROOT / "cortados_router/core.py")); core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
core.lane_health = lambda: {}  # tests are about the table, not live lane state
_scored = core.scored_candidates
core.scored_candidates = lambda t, band, kind, accounts_aware=True: None  # …and about the hand-written fallback candidates; scored selection has its own tests below

def dec(steps=0, ambiguity=0, blast=0, ctx=0, novelty=0, par=0, **kw):
    a = {"steps": steps, "ambiguity": ambiguity, "blast_radius": blast, "context_size": ctx, "novelty": novelty, "parallelizability": par,
         "outward": False, "work_kind": "conversation", "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False, **kw}
    return {"answers": a, "degraded": False, "engine": "test", "engine_ms": 1}

class Bands(unittest.TestCase):
    def test_r0_r1(self):
        self.assertEqual(core.resolve(dec())["band"], "R0")
        r = core.resolve(dec(steps=2, ambiguity=2, blast=1)); self.assertEqual((r["band"], r["D"], r["subagents"]), ("R1", 5, 0))
    def test_r2_decomposes_only_when_parts_or_parallel(self):
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2))["subagents"], 0)          # D=7, serial, one part → orchestrated, no fan-out
        r = core.resolve(dec(steps=2, ambiguity=1, blast=1, par=2, parts=2)); self.assertEqual((r["band"], r["subagents"]), ("R2", 3))
    def test_r3_by_difficulty_and_by_blast(self):
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=3, novelty=3))["band"], "R3")
        r = core.resolve(dec(blast=3)); self.assertEqual(r["band"], "R3"); self.assertTrue(r["verifier"]); self.assertGreaterEqual(r["subagents"], 3)
    def test_outward_floors_blast_radius(self):
        r = core.resolve(dec(steps=1, outward=True)); self.assertEqual((r["band"], r["features"]["blast_radius"]), ("R3", 3)); self.assertTrue(r["hard_rules"])
    def test_r4_never_by_score(self):
        self.assertNotEqual(core.resolve(dec(3, 3, 3, 3, 3, 3))["band"], "R4")
    def test_degraded_uses_fallback(self):
        d = {"answers": None, "degraded": True, "engine": "test", "engine_ms": 1}
        self.assertEqual(core.resolve(d, "interactive")["band"], "R2"); self.assertEqual(core.resolve(d, "background")["band"], "R1")
    def test_work_kind_reorders_lanes(self):
        self.assertEqual(core.resolve(dec(steps=2, ambiguity=1, blast=1, par=2, work_kind="code"))["chosen"]["lane"], "codex")

class TableValidation(unittest.TestCase):
    def test_rejects_ceiling_outside_r4_and_non_grok_cursor(self):
        t = json.loads((ROOT / "tables/routing-table.json").read_text())
        bad = json.loads(json.dumps(t)); bad["candidates"]["R2"].append({"lane": "claude", "provider": "claude-cli", "model": "claude-fable-5-1", "tier": "W-ceiling"})
        bad.setdefault("selection", {})["ceiling_by_score"] = False
        with self.assertRaises(ValueError): core.validate_table(bad)
        bad = json.loads(json.dumps(t)); bad["candidates"]["R1"][0]["model"] = "claude-sonnet-5"
        bad.setdefault("selection", {})["ceiling_by_score"] = False
        with self.assertRaises(ValueError): core.validate_table(bad)
        core.validate_table(t)

class Scarcity(unittest.TestCase):
    """Subscription scarcity re-ranks lanes inside the band; it never changes the band."""
    def setUp(self):
        self._lh = core.lane_health
    def tearDown(self):
        core.lane_health = self._lh
    def _lanes(self, **kw):
        core.lane_health = lambda: {l: {"ok": v.get("s", 0) < 1.0, "why": "spent", "scarcity": v.get("s", 0), "expiring_pct": v.get("exp", 0),
                                        "tightest": {"window": "week", "used_pct": 80, "elapsed_pct": 40, "projected_pct_at_reset": 200, "resets_at": "x"}} for l, v in kw.items()}
    def test_spent_lane_is_skipped(self):
        self._lanes(cursor={"s": 1.0})
        r = core.resolve(dec(steps=2, ambiguity=2, blast=1))
        self.assertEqual(r["band"], "R1"); self.assertNotEqual(r["chosen"]["lane"], "cursor")
        self.assertTrue(any(s['lane'] == 'cursor' for s in r['skipped']))
    def test_fast_burning_lane_goes_behind(self):
        first = core.resolve(dec(steps=2, ambiguity=2, blast=1))["chosen"]["lane"]
        self._lanes(**{first: {"s": 0.8}})
        r = core.resolve(dec(steps=2, ambiguity=2, blast=1))
        self.assertEqual(r["band"], "R1"); self.assertNotEqual(r["chosen"]["lane"], first); self.assertTrue(any("demoted" in x for x in r["hard_rules"]))
    def test_expiring_headroom_goes_first(self):
        cands = core.table()["candidates"]["R2"]
        last = cands[-1]["lane"]
        self._lanes(**{last: {"s": 0.0, "exp": 30}})
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=2))
        self.assertEqual(r["band"], "R2"); self.assertEqual(r["chosen"]["lane"], last)
    def test_scarce_lane_holds_subagents_to_minimum(self):
        self._lanes(claude={"s": 0.8}, codex={"s": 0.85}, cursor={"s": 0.9})   # every lane of the band is scarce
        r = core.resolve(dec(steps=3, ambiguity=2, blast=1, novelty=2, par=3, parts=3))
        row = next(b for b in core.table()["bands"] if b["band"] == r["band"])
        self.assertEqual(r["subagents"], (row.get("subagents") or {}).get("min", 0))

    def test_model_pool_is_scored_separately(self):
        # Fable's own week is spent while the all-models week has room: Fable is skipped, other Claude models are not
        week = {"window": "week (fable)", "used_pct": 100, "elapsed_pct": 90, "projected_pct_at_reset": 111, "resets_at": "x"}
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": 0.1, "expiring_pct": 0, "tightest": {},
                                               "pools": {"fable": {"ok": False, "why": "week (fable) 100% used", "scarcity": 1.0, "expiring_pct": 0, "tightest": week}}}}
        self.assertFalse(core.candidate_health({"lane": "claude", "model": "claude-fable-5-1"}, core.lane_health())["ok"])
        self.assertTrue(core.candidate_health({"lane": "claude", "model": "claude-opus-5"}, core.lane_health())["ok"])



class ScoredSelection(unittest.TestCase):
    """No band lists: every model has an index per use case, every task a required index."""
    MS = {"use_cases": {"code": "", "writing": ""}, "reference_index": {b: {"general": v, "code": v, "writing": v} for b, v in (("R1", 10), ("R2", 50), ("R3", 80), ("R4", 95))},
          "models": [{"family": f, "ceiling": ceil, "proxy": False, "index": {"general": g, "code": c, "writing": w}, "price_1m_blended_usd": price, "reach": [{"lane": lane, "provider": prov, "model": f}]}
                     for f, g, c, w, lane, prov, ceil, price in (("small", 12, 12, 30, "cursor", "cursor-cli", False, 1.0), ("mid", 55, 52, 70, "claude", "claude-cli", False, 4.0), ("mid2", 60, 58, 40, "codex", "openai-codex", False, 4.5),
                                                                 ("big", 85, 88, 60, "claude", "claude-cli", False, 10.0), ("top", 99, 100, 90, "claude", "claude-cli", True, 20.0),
                                                                 ("loc", 0, 0, 0, "local", "custom", False, None))]}
    def setUp(self):
        self._keep = (core.scored_candidates, core.model_scores, core.lane_health)
        core.scored_candidates, core.model_scores, core.lane_health = _scored, (lambda: self.MS), (lambda: {})
    def tearDown(self):
        core.scored_candidates, core.model_scores, core.lane_health = self._keep
    def test_capacity_credit_needs_a_fresh_reading(self):
        # second adversarial review 2026-10-21: a failed Codex sample (scarcity None, read as 0) still earned the tokens-left credit and beat Claude
        keep = core._tokens_left_for
        core._tokens_left_for = lambda c: 500_000_000 if c["lane"] == "codex" else None
        try:
            fresh = {"codex": {"ok": True, "why": None, "scarcity": 0.0, "expiring_pct": 0, "tightest": {}, "pools": {}}}
            core.lane_health = lambda: fresh
            self.assertEqual(core.resolve(dec(steps=2, ambiguity=1, work_kind="code"))["chosen"]["lane"], "codex")      # fresh pool with the most room: credited
            core.lane_health = lambda: {}                                                                                   # unknown or failed: no reading at all
            r = core.resolve(dec(steps=2, ambiguity=1, work_kind="code"))
            self.assertEqual(r["chosen"]["lane"], "claude"); self.assertNotIn("tokens_left", json.dumps(r["chosen"]))
        finally:
            core._tokens_left_for = keep
    def test_requirement_is_continuous_in_difficulty(self):
        t = core.table()
        r = [core.required_index(t, self.MS, "code", D, "R2") for D in (6, 7, 8)]
        self.assertEqual(r[0], 50); self.assertTrue(r[0] < r[1] < r[2] < 80)
    def test_least_overqualified_model_that_clears_the_bar(self):
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))            # D=6 → needs 50
        self.assertEqual((r["chosen"]["model"], r["required_index"]), ("mid", 50.0))
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=3, work_kind="code"))            # D=8 → needs 70: mid and mid2 no longer clear it
        self.assertEqual(r["chosen"]["model"], "big")
    def test_use_case_changes_the_winner_and_ceiling_never_by_score(self):
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2, work_kind="writing"))["chosen"]["model"], "mid")    # D=7 needs 60: mid writes at 70
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2, work_kind="code"))["chosen"]["model"], "big")       # …but codes at 52
        r = core.resolve(dec(3, 3, 3, 3, 3, 3, work_kind="code"))            # D=12: the ceiling model wins by score (Forest 2026-09-23)
        self.assertEqual(r["chosen"]["model"], "top")
        self.assertNotEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2, work_kind="code"))["chosen"]["model"], "top")   # D=7: never on a medium task
    def test_scarcity_moves_the_choice(self):
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": 0.9, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))["chosen"]["model"], "mid2")      # 0.9 × 30 points outweighs 6 of surplus

    def test_expiring_window_beats_a_far_under_pace_sibling(self):
        # claude's weekly window is near reset with credit unspent (expiring_pct=34); codex is more under-pace overall
        # (scarcity -1.0) but its reset is far (expiring_pct=0). Reserve-by-reset spends the near-reset credit first.
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": -0.39, "expiring_pct": 34, "tightest": {"window": "week", "resets_at": "x"}, "pools": {}},
                                    "codex": {"ok": True, "why": None, "scarcity": -1.0, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))["chosen"]["lane"], "claude")

    def test_without_the_expiring_signal_the_under_pace_sibling_wins(self):
        # control: with neither lane near reset, the most under-pace lane still wins — so the flip above is the expiring term's doing
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": -0.39, "expiring_pct": 0, "tightest": {}, "pools": {}},
                                    "codex": {"ok": True, "why": None, "scarcity": -1.0, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))["chosen"]["lane"], "codex")

    def test_a_scarce_now_window_suppresses_the_expiring_discount(self):
        # claude's weekly credit is expiring (expiring_pct=33) but its tightest window is scarce NOW (scarcity 0.8, e.g. the
        # 5-hour session nearly spent). The deadline discount must not route work onto a lane with no immediate room; codex,
        # which has room now, wins. (Codex review 2026-10-26)
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": 0.8, "expiring_pct": 33, "tightest": {"window": "5-hour session", "resets_at": "x"}, "pools": {}},
                                    "codex": {"ok": True, "why": None, "scarcity": 0.0, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"))["chosen"]["lane"], "codex")
    def test_secondary_source_only_when_native_spent(self):
        ms = {"use_cases": {"code": ""}, "reference_index": ScoredSelection.MS["reference_index"],
              "models": [{"family": "claude-opus-5", "ceiling": False, "proxy": False, "index": {"general": 80, "code": 85},
                          "reach": [{"lane": "claude", "provider": "claude-cli", "model": "claude-opus-5"}, {"lane": "cursor", "provider": "cursor-cli", "model": "claude-opus-5-thinking-high", "pool": "other-models"}]},
                         {"family": "kimi-k3", "ceiling": False, "proxy": False, "index": {"general": 64, "code": 68}, "reach": [{"lane": "cursor", "provider": "cursor-cli", "model": "kimi-k3-high", "pool": "other-models"}]}]}
        core.model_scores = lambda: ms
        self._keep = (core.scored_candidates, core.model_scores, core.lane_health)
        healthy = {"claude": {"ok": True, "why": None, "scarcity": 0.9, "expiring_pct": 0, "tightest": {}, "pools": {}}}      # scarce but NOT spent
        core.lane_health = lambda: healthy
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=3, work_kind="code"))                                            # D=8 needs 70: only Opus qualifies
        self.assertEqual((r["chosen"]["lane"], r["chosen"]["model"]), ("claude", "claude-opus-5"))                            # native wins even at 0.9
        spent = {"claude": {"ok": False, "why": "week 100% used", "scarcity": 1.0, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        core.lane_health = lambda: spent
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=3, work_kind="code"))
        self.assertEqual(r["chosen"]["lane"], "cursor")                                                                       # secondary source takes over
        self.assertEqual(core.native_lane_for("kimi-k3"), None); self.assertEqual(core.native_lane_for("gpt-oss-120b"), None); self.assertEqual(core.native_lane_for("cursor-grok-4.6"), None)
    def test_context_policy_and_stickiness(self):
        big = 200 * 4 * 1000                                                      # 200k tokens of history
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"), history_chars=big)
        self.assertTrue(all("ceiling" in s.get("why", "") for s in r["skipped"]) or r["chosen"] is None or r["chosen"]["lane"] == "claude")
        self.assertEqual(core.resolve(dec())["chosen"]["lane"], "local")                      # fresh reflex: local
        r = core.resolve(dec(), history_chars=40000)                              # 10k of history: local excluded for reflex work
        self.assertNotEqual((r["chosen"] or {}).get("lane"), "local")
        st = {"lane": "codex", "model": "mid2", "band": "R2", "pinned": False}
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="code"), sticky=st)     # mid2 (58) clears 50: stays
        self.assertEqual(r["chosen"]["model"], "mid2")
        r = core.resolve(dec(steps=3, ambiguity=2, novelty=3, work_kind="code"), sticky=st)     # needs 70: outgrown, moves
        self.assertNotEqual(r["chosen"]["model"], "mid2")


    def test_insight_work_goes_to_the_frontier(self):
        ms = json.loads(json.dumps(ScoredSelection.MS)); ms["use_cases"]["planning"] = ""
        for m in ms["models"]:
            m["index"]["planning"] = m["index"]["general"]
        ms["reference_index"] = {b: {**v, "planning": v["general"]} for b, v in ms["reference_index"].items()}
        core.model_scores = lambda: ms
        # Trigger is selection.ceiling_work.min_D in tables/routing-table.json. It was D>=5 (Forest 2026-09-25, CortadOS 2fbf4ff9) and is
        # D>=7 since CortadOS 634c97bb (Forest 2026-09-29: "ceiling_work min_D 5→7 (outward floor made ordinary planning-tagged replies
        # frontier work)"); ROUTER-DESIGN §9 records that this test still asserted D=5. Same intent, at the documented boundary.
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2, work_kind="planning"))["chosen"]["model"], "top")    # D=7 planning → frontier
        self.assertNotEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=1, work_kind="planning"))["chosen"]["model"], "top")  # D=6 planning → not (below min_D 7)
        self.assertNotEqual(core.resolve(dec(steps=1, ambiguity=1, work_kind="planning"))["chosen"]["model"], "top")            # D=2 planning → not
        self.assertNotEqual(core.resolve(dec(steps=3, ambiguity=2, novelty=2, work_kind="code"))["chosen"]["model"], "top")     # D=7 code → price ladder
    def test_underused_frontier_pool_takes_hard_work(self):
        # Trigger is selection.ceiling_underuse.min_D in tables/routing-table.json. It was D>=6 (Forest 2026-09-25, CortadOS 2fbf4ff9) and is
        # D>=9 since CortadOS dc029601 ("router: Astra only for D>=9 when Codex is underused (was 6) ... simple turns were staying on Astra");
        # ROUTER-DESIGN §9 records that this test still asserted D=6. Same intent, at the documented boundary.
        self.assertNotEqual(core.resolve(dec(steps=3, ambiguity=3, novelty=3, work_kind="code"))["chosen"]["model"], "top")    # D=9 code, no underuse → price ladder
        core.lane_health = lambda: {"claude": {"ok": True, "why": None, "scarcity": -0.6, "expiring_pct": 0, "tightest": {}, "pools": {}}}
        self.assertEqual(core.resolve(dec(steps=3, ambiguity=3, novelty=3, work_kind="code"))["chosen"]["model"], "top")       # D=9 code, claude pool underused → top
        self.assertNotEqual(core.resolve(dec(steps=3, ambiguity=3, novelty=2, work_kind="code"))["chosen"]["model"], "top")    # D=8, underused → not (below min_D 9)

    def test_face_turns_have_a_floor_but_background_does_not(self):
        r = core.resolve(dec(), "interactive")                                    # R0 face turn: "small" (12) is below the floor → not chosen
        self.assertNotEqual(r["chosen"]["model"], "small")
        self.assertEqual(core.resolve(dec(), "background")["chosen"]["lane"], "local")   # background reflex: cheapest still wins


class CleanRequest(unittest.TestCase):
    def test_discord_shapes(self):
        c = core.clean_request
        self.assertEqual(c("[Triggering message id: `1` — use as `message_id` for reply/react/pin via the discord tools.]\n\ngive me a confirmation when you see it"), "give me a confirmation when you see it")
        self.assertEqual(c("[Triggering message id: `1` — x.]\n\n[Recent channel messages]\n[Donna [bot]] hi\n\n[New message]\ndo the thing\n\n-# router (shadow, nothing changed): R0"), "do the thing")
        self.assertIsNone(c("[Triggering message id: `1` — x.]\n\n[Recent channel messages]\n[Donna [bot]] cut off he"))
        self.assertEqual(c('[Replying to: "Which faces?"]\n\n[Triggering message id: `1` — x.]\n\nall of them'), '(in reply to: "Which faces?")\nall of them')
        self.assertEqual(c("plain question"), "plain question")




def _load_sc():
    spec = importlib.util.spec_from_file_location("sc", str(ROOT / "cortados_router/scarcity.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class UsageScreens(unittest.TestCase):
    """Parsers run on the text the four vendor screens really printed on 2026-09-21."""
    sc = _load_sc()
    def test_window_view_is_pct_vs_time(self):
        import time
        w = self.sc.window_view("week", 70, time.time() + 0.1 * 7 * 86400, 7 * 86400)
        self.assertEqual(w["elapsed_pct"], 90.0); self.assertEqual(w["projected_pct_at_reset"], 78); self.assertLess(w["scarcity"], 0.2); self.assertEqual(w["expiring_pct"], 22)
        hot = self.sc.window_view("week", 60, time.time() + 0.7 * 7 * 86400, 7 * 86400)
        self.assertEqual(hot["projected_pct_at_reset"], 200); self.assertEqual(hot["scarcity"], 0.9)
        self.assertEqual(self.sc.window_view("week", 100, time.time() + 3600, 7 * 86400)["scarcity"], 1.0)
    def test_claude(self):
        w = self.sc.parse_claude_usage("Current session\r ███ \n20%\nused\r Resets 12pm (America/Toronto)\r Current week (all models)\r ████ \n70%\nused\r Resets Sep 21 at 3pm (America/Toronto)")
        self.assertEqual([(x["window"], x["used_pct"]) for x in w], [("5-hour session", 20.0), ("week (all models)", 70.0)]); self.assertTrue(all(x["resets_at"] for x in w))
    FROZEN = __import__("datetime").datetime(2026, 10, 21, 12, 0).timestamp()   # the day of the 0.163.0 capture: the fixtures never age
    def test_codex(self):
        text = (ROOT / "tests/fixtures/codex-status-0.163.0.txt").read_text()                     # the real screen, redacted
        w = self.sc.parse_codex_status(text, now=self.FROZEN)
        self.assertEqual([(x["window"], x["pool"], x["used_pct"]) for x in w], [("week", "all", 0.0)])   # the session uuid above the limit line is not a pool
        self.assertIn("-10-27 12:01", w[0]["resets_at"]); self.assertTrue(143 < w[0]["resets_in_h"] < 145)   # "12:01 PM on 27 Oct" read as a date, not as under a day away

    def test_codex_0_164(self):
        text = (ROOT / "tests/fixtures/codex-status-0.164.0.txt").read_text()                     # real 0.164.0 screen, redacted
        now = __import__("datetime").datetime(2026, 10, 25, 16, 30).timestamp()                   # the day of the 0.164.0 capture
        w = self.sc.parse_codex_status(text, now=now)
        self.assertEqual([(x["window"], x["pool"], x["used_pct"]) for x in w], [("week", "all", 0.0)])   # 100% left = 0% used
        self.assertIn("-11-01 14:55", w[0]["resets_at"]); self.assertTrue(166 < w[0]["resets_in_h"] < 168)   # "2:55 PM on 1 Nov" within the week
    def test_codex_screen_is_never_guessed(self):
        head = ">_ OpenAI Codex (v0.163.0)\n"
        for screen, why in ((head + " 01a10000-0000-7000-8000-000000000000 Weekly limit: [█]\n100%\nleft", "unknown limit label"),
                            (head + " Weekly limit: [█]\n40%\nleft (resets 12:01 PM on 27 Nov)", "outside its"),
                            (head + " Weekly limit: [█]\n40%\nleft (resets 12:01 PM on 27 Oct, maybe)", "unreadable reset"),
                            (head + " Weekly limit: [█]\n40%\nleft", "no reset time"),
                            (">_ OpenAI Codex (v0.170.0)\n Weekly limit: [█]\n100%\nleft", "not one this parser"),
                            (" Weekly limit: [█]\n100%\nleft", "no 'OpenAI Codex"),
                            # adversarial review 2026-10-21: an incomplete entry must not borrow the next entry's figures …
                            (head + " Weekly limit: [█]\n GPT-5.3-Codex-Spark Weekly limit: [█]\n100%\nleft", "'Weekly limit' entry unreadable"),
                            # … a truncated or misplaced reset is not "no reset" …
                            (head + " Weekly limit: [█]\n100%\nleft (resets 12:01 PM on 27 Oct", "entry unreadable"),
                            (head + " Weekly limit: [█]\n50%\nleft (resets 12:01 PM) on 27 Oct", "entry unreadable"),
                            # … a quota line the grammar cannot read must not vanish, and an impossible clock is not normalised
                            (head + " 5h limit: [█]\n100%\nleft\n Weekly limit: 0% left (resets 12:01 PM on 27 Oct)", "unreadable limit line"),
                            (head + " 5h limit: [█]\n50%\nleft (resets 25:00 PM)", "impossible clock time"),
                            (head + " Weekly limit: [█]\n100%\nleft (resets )", "entry unreadable")):          # an empty reset is not "no reset"
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                self.sc.parse_codex_status(screen, now=self.FROZEN)
        w = self.sc.parse_codex_status(head + " Weekly limit: [█]\n100%\nleft", now=self.FROZEN)          # untouched: no reset clock is running
        self.assertEqual((w[0]["used_pct"], w[0]["resets_at"]), (0.0, None))
        w = self.sc.parse_codex_status(head + " Weekly limit: [█]\n100%\nleft\n 5h limit: [█]\n100%\nleft", now=self.FROZEN)   # a digit-leading entry ends the one above
        self.assertEqual([x["window"] for x in w], ["week", "5-hour"])
    def test_codex_time_only_reset_beside_the_clock(self):
        # adversarial review 2026-10-21 (third pass): the screen shows minutes, so a reset read during its displayed minute is now, not tomorrow
        import datetime
        head = ">_ OpenAI Codex (v0.163.0)\n"
        day = datetime.datetime(2026, 10, 21)
        for clock, when, expect in (((12, 0, 30), "12:00 PM", day.replace(hour=12)),                 # the displayed minute
                                    ((23, 59, 30), "12:00 AM", day + datetime.timedelta(days=1)),     # midnight, half a minute ahead
                                    ((0, 0, 30), "12:00 AM", day),                                     # midnight, half a minute behind
                                    ((12, 30, 0), "12:00 PM", day.replace(hour=12))):                  # clocks half an hour apart: the reset just passed, as the date form reads it
            with self.subTest(when=when, clock=clock):
                now = day.replace(hour=clock[0], minute=clock[1], second=clock[2]).timestamp()
                w = self.sc.parse_codex_status(head + f" 5h limit: [█]\n0%\nleft (resets {when})", now=now)
                self.assertEqual(w[0]["resets_at"], expect.strftime("%Y-%m-%d %H:%M"))
        with self.assertRaisesRegex(ValueError, "outside its 5-hour window"):                           # two hours past is tomorrow's time, and tomorrow is outside a 5-hour window
            self.sc.parse_codex_status(head + " 5h limit: [█]\n0%\nleft (resets 12:00 PM)", now=day.replace(hour=14, second=1).timestamp())
    def test_codex_reset_across_calendar_boundaries(self):
        # adversarial review 2026-10-21 (fourth pass): a reset inside the past hour may sit on yesterday's date or in last year, and the window
        # is measured in elapsed seconds, so a DST change neither refuses a reset 45 minutes past nor admits one seven hours ahead
        import datetime, os, time
        if not hasattr(time, "tzset"):
            self.skipTest("needs time.tzset to pin the zone")
        head = ">_ OpenAI Codex (v0.163.0)\n"
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/Toronto"; time.tzset()
        try:
            at = lambda *ymdhms: datetime.datetime(*ymdhms).timestamp()
            for clock, entry, expect in (((2026, 10, 22, 0, 0, 1), " 5h limit: [█]\n50%\nleft (resets 11:59 PM)", "2026-10-21 23:59"),              # yesterday's, 61 s past
                                         ((2027, 1, 1, 0, 0, 1), " Weekly limit: [█]\n50%\nleft (resets 11:59 PM on 31 Dec)", "2026-12-31 23:59"),  # last year's
                                         ((2026, 3, 8, 3, 15, 0), " 5h limit: [█]\n50%\nleft (resets 1:30 AM)", "2026-03-08 01:30"),             # spring forward: 45 min past
                                         ((2026, 11, 1, 0, 30, 0), " 5h limit: [█]\n50%\nleft (resets 5:30 AM on 1 Nov)", "2026-11-01 05:30")):  # fall back: six elapsed hours
                with self.subTest(expect=expect):
                    w = self.sc.parse_codex_status(head + entry, now=at(*clock))
                    self.assertEqual(w[0]["resets_at"], expect)
            with self.assertRaisesRegex(ValueError, "outside its 5-hour window"):           # fall back: "6:30 AM" is seven elapsed hours from 00:30, not six
                self.sc.parse_codex_status(head + " 5h limit: [█]\n50%\nleft (resets 6:30 AM on 1 Nov)", now=at(2026, 11, 1, 0, 30, 0))
            # fifth pass: the hour DST repeats names two moments and the screen does not say which; a time DST skips is not on the calendar
            with self.assertRaisesRegex(ValueError, "two moments"):
                self.sc.parse_codex_status(head + " 5h limit: [█]\n20%\nleft (resets 1:30 AM)", now=at(2026, 11, 1, 1, 45, 0))
            with self.assertRaisesRegex(ValueError, "no reading on the calendar|outside its"):
                self.sc.parse_codex_status(head + " Weekly limit: [█]\n50%\nleft (resets 2:30 AM on 8 Mar)", now=at(2026, 3, 8, 3, 15, 0))
            with self.assertRaisesRegex(ValueError, "outside its 5-hour window"):           # the skipped 2:30 is not today's; tomorrow's is 23 h out
                self.sc.parse_codex_status(head + " 5h limit: [█]\n50%\nleft (resets 2:30 AM)", now=at(2026, 3, 8, 3, 15, 0))
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
    def test_cursor(self):
        w = self.sc.parse_cursor_usage("Usage • Pro Resets Oct 10 Monthly plan Category Current Usage Included 17% used ███ Auto 1% used █ API 33% used ███ On-Demand Disabled")
        self.assertEqual([(x["used_pct"], x["pool"]) for x in w], [(17.0, "info"), (1.0, "cursor-models"), (33.0, "other-models")])
        rec = self.sc.finish("cursor", {"windows": w})
        self.assertEqual(sorted(rec["pools"]), ["cursor-models", "other-models"]); self.assertEqual(rec["scarcity"], rec["pools"]["cursor-models"]["scarcity"])
        self.assertEqual(self.sc.pool_for("cursor", "cursor-grok-4.6-medium-fast", []), "cursor-models"); self.assertEqual(self.sc.pool_for("cursor", "claude-opus-5", []), "other-models")
    def test_claude_fable_week_is_its_own_pool(self):
        import datetime
        soon = (datetime.datetime.now() + datetime.timedelta(hours=3)).strftime("%b %-d at %-I%p").replace("AM", "am").replace("PM", "pm")   # the test must not age
        w = self.sc.parse_claude_usage(f"Current session 3% used Resets 5pm (America/Toronto) Current week (all models) 73% used Resets {soon} (America/Toronto) "
                                       f"Current week (Fable) 95% used Resets {soon} (America/Toronto)")
        self.assertEqual([x["pool"] for x in w], ["all", "all", "fable"])
        rec = self.sc.finish("claude", {"windows": w})
        self.assertGreaterEqual(rec["pools"]["fable"]["scarcity"], 0.75); self.assertLess(rec["scarcity"], 0.3)   # 0.8 minus the small expiring credit
        self.assertEqual(self.sc.pool_for("claude", "claude-fable-5-1", ["fable"]), "fable"); self.assertIsNone(self.sc.pool_for("claude", "claude-haiku-4-5", ["fable"]))
    def test_codex_named_limit(self):
        # the 0.163 layout: one entry per line, "% left" wrapped under it, a model-specific week beside the general one, the screen painted twice
        entries = (" 5h limit: [████]\n80%\nleft (resets 2:05 PM)\n Weekly limit: [░]\n0%\nleft (resets 12:01 PM on 27 Oct)\n"
                   " GPT-5.3-Codex-Spark   Weekly limit: [███]\n90%\nleft (resets 12:01 PM on 27 Oct)\n Credits: 5396\n")
        screen = (">_ OpenAI Codex (v0.163.0)\n Account: user@example.com\n(Pro\n200)\n Session: 01a10000-0000-7000-8000-000000000000\n" + entries) * 2
        w = self.sc.parse_codex_status(screen, now=self.FROZEN)
        self.assertEqual([(x["window"], x["pool"], x["used_pct"]) for x in w], [("5-hour", "all", 20.0), ("week", "all", 100.0), ("week (GPT-5.3-Codex-Spark)", "gpt-5-3-codex-spark", 10.0)])
        self.assertEqual(w[1]["scarcity"], 1.0); self.assertIn("-10-21 14:05", w[0]["resets_at"]); self.assertIn("-10-27 12:01", w[2]["resets_at"])
    def test_antigravity(self):
        g = self.sc.parse_agy_usage("GEMINI MODELS Models within this group: Gemini Flash Weekly Limit Remaining [ ███ ] 99.03% Refreshes in 35h 32m Five Hour Limit Remaining [ ██ ] 98.02% Refreshes in 4h 25m "
                                    "CLAUDE AND GPT MODELS Models within this group: Claude Opus Weekly Limit Remaining [ ███ ] 100.00% Quota available Five Hour Limit Remaining [ ███ ] 100.00% Quota available")
        self.assertEqual(sorted(g), ["claude and gpt models", "gemini models"]); self.assertEqual(g["gemini models"][0]["used_pct"], 1.0)
        self.assertEqual(self.sc.pool_for("antigravity", "Gemini 3.7 Flash (Medium)", []), "gemini"); self.assertEqual(self.sc.pool_for("antigravity", "claude-opus-5", []), "claude-gpt"); self.assertEqual(g["gemini models"][0]["resets_in_h"], 35.5)


if __name__ == "__main__":
    unittest.main(verbosity=1)
