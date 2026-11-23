"""Unit tests for the API credit lane's accounting (cortados_router/api_credits.py, #114). Stdlib only; every test uses a
throwaway ledger and config (never ~/.cortados, never ~/.config) and a fake key. No network."""
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock

from cortados_router import api_credits as ac

REPO = Path(__file__).resolve().parents[1]


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


# Today's grant (owner, 2026-11-22): $200, granted 2026-10-30, expires 2026-12-02 19:00 America/Toronto (EST).
GRANT = {"name": "API credit (Ultra)", "amount_usd": 200, "granted": "2026-10-30T00:00:00-04:00",
         "expires": "2026-12-02T19:00:00-05:00", "remaining_usd": 200, "remaining_as_of": "2026-11-22T00:00:00-05:00"}
CONFIG = {"enabled": True, "org_monthly_limit_usd": 200, "prefer_within_days": 3, "grants": [GRANT]}
NOW = ts("2026-11-22T15:00:00-05:00")


def png(w: int, h: int) -> str:
    """A minimal valid PNG header of w x h (image_check reads the IHDR only) as base64."""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00")) + chunk(b"IEND", b"")
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + raw).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = self.dir / "home" / "api-credit-ledger.json"
        self.cfg = self.dir / "api-credit.json"
        self.write_config(CONFIG)
        self.env = mock.patch.dict(os.environ, {"CORTADOS_HOME": str(self.dir / "home"),
                                                "CORTADOS_API_CREDIT_CONFIG": str(self.cfg),
                                                "CORTADOS_ANTHROPIC_API_KEY": "sk-fake-test"})
        self.env.start()
        os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
        self.addCleanup(self.env.stop)

    def write_config(self, cfg):
        self.cfg.write_text(json.dumps(cfg))

    def state(self, now=NOW):
        return ac.state(now, path=self.ledger)

    def reserve(self, cost, now=NOW):
        return ac.reserve(cost, now, self.ledger)


class TestPricing(unittest.TestCase):
    def test_price_for(self):
        self.assertEqual(ac.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(ac.price_for("claude-opus-5-5-20260401")["output"], 20.0)   # dated -> base
        self.assertEqual(ac.price_for("claude-opus-5")["input"], 5.0)                # not confused with opus-5-5
        self.assertEqual(ac.price_for("claude-haiku-4-5-20251001")["input"], 1.0)
        self.assertEqual(ac.price_for("unknown-model"), ac.DEFAULT_PRICE)            # unknown -> most expensive

    def test_pricing_is_conservative(self):
        self.assertEqual(ac.price_for("claude-opus-5-5")["cache_write"], 8.0)        # the 1-hour write rate, 4 x 2
        self.assertEqual(ac.price_for("claude-haiku-5-5")["input"], 0.50)            # Haiku 5.5's >100k tier

    def test_cost_from_usage_prices_every_premium(self):
        # under the long-context line: plain prices
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 100_000, "output_tokens": 1000}),
                               (100_000 * 4 + 1000 * 20) / 1e6)
        # over it: input x2, output x1.5
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 300_000, "output_tokens": 1000}),
                               (300_000 * 8 + 1000 * 30) / 1e6)
        # fast mode x2 and data residency x1.1 on top
        fast = ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 1000, "speed": "fast"})
        self.assertAlmostEqual(fast, (1000 * 8 + 1000 * 40) / 1e6)
        geo = ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 0, "inference_geo": "us"})
        self.assertAlmostEqual(geo, 1000 * 4 * 1.1 / 1e6)
        # cache writes at the 1-hour rate, cache reads at a tenth
        cw = ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 0, "output_tokens": 0,
                                                    "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 1000})
        self.assertAlmostEqual(cw, (1000 * 8 + 1000 * 0.4) / 1e6)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=-5), 0.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=float("inf")), 0.0)

    def test_iterations_are_settled_each_at_its_own_model_and_never_below_the_top_level(self):
        # #114 review round 1: a multi-attempt turn reports usage.iterations; each billed attempt is priced at its own
        # model (never below the requested one) and speed, and the charge is the larger of their sum and the top level.
        top = {"input_tokens": 1000, "output_tokens": 100}
        its = [{"model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 50},
               {"model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 100, "speed": "fast"}]
        got = ac.cost_from_usage("claude-sonnet-5", {**top, "iterations": its})
        want = (1000 * 2 + 50 * 10) / 1e6 + (1000 * 5 * 2 + 100 * 25 * 2) / 1e6
        self.assertAlmostEqual(got, want)
        # a cheaper model named by an iteration is priced at the requested (dearer) one
        cheap = [{"model": "claude-haiku-4-5", "input_tokens": 1000, "output_tokens": 100}]
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5", {**top, "iterations": cheap}),
                               (1000 * 5 + 100 * 25) / 1e6)
        # an iteration naming no model (unexpected: fallbacks are refused) is priced at the dearest tier
        anon = [{"input_tokens": 1000, "output_tokens": 100}]
        self.assertAlmostEqual(ac.cost_from_usage("claude-sonnet-5", {**top, "iterations": anon}),
                               (1000 * 10 + 100 * 50) / 1e6)
        # iterations summing below the top level never lower the charge
        small = [{"model": "claude-sonnet-5", "input_tokens": 1, "output_tokens": 1}]
        self.assertAlmostEqual(ac.cost_from_usage("claude-sonnet-5", {**top, "iterations": small}),
                               ac.cost_from_usage("claude-sonnet-5", top))

    def test_server_tool_charges_are_counted(self):
        usage = {"input_tokens": 1_000, "output_tokens": 0, "server_tool_use": {"web_search_requests": 10}}
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", usage), 0.004 + 0.10, places=6)


class TestEstimateIsAnUpperBound(unittest.TestCase):
    def body(self, **kw):
        return {"model": "cortados", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}], **kw}

    def test_every_request_byte_is_a_token_at_the_cache_write_rate_plus_max_tokens_as_output(self):
        b = self.body()
        n = len(json.dumps(b).encode()) + ac.TEMPLATE_TOKENS
        self.assertAlmostEqual(ac.estimate_max_cost(b, "claude-opus-5-5"), (n * 8.0 + 1000 * 20.0) / 1e6)

    def test_the_estimate_covers_the_real_bill_of_any_completion(self):
        # whatever the API reports for a turn of this request (input <= bytes, output <= max_tokens, any split between
        # input, cache writes and cache reads), the settled cost never exceeds the reserved estimate
        b = self.body(messages=[{"role": "user", "content": "x" * 5000}], max_tokens=2000)
        est = ac.estimate_max_cost(b, "claude-sonnet-5-5")
        n = len(json.dumps(b).encode())
        for split in ((n, 0, 0), (0, n, 0), (0, 0, n), (n // 3, n // 3, n // 3)):
            usage = {"input_tokens": split[0], "cache_creation_input_tokens": split[1],
                     "cache_read_input_tokens": split[2], "output_tokens": 2000}
            self.assertLessEqual(ac.cost_from_usage("claude-sonnet-5-5", usage), est)

    def test_no_max_tokens_means_the_largest_output(self):
        b = {"model": "cortados", "messages": []}
        self.assertGreaterEqual(ac.estimate_max_cost(b, "claude-opus-5-5"), ac.MAX_OUTPUT_TOKENS * 20.0 / 1e6)

    def test_premiums_are_priced_in(self):
        base = ac.estimate_max_cost(self.body(), "claude-opus-5-5")
        self.assertGreaterEqual(ac.estimate_max_cost(self.body(speed="fast"), "claude-opus-5-5"), 2.0 * base)
        self.assertGreater(ac.estimate_max_cost(self.body(inference_geo="us"), "claude-opus-5-5"), base * 1.09)
        big = self.body(messages=[{"role": "user", "content": "x" * 250_000}])
        n = len(json.dumps(big).encode()) + ac.TEMPLATE_TOKENS
        self.assertAlmostEqual(ac.estimate_max_cost(big, "claude-opus-5-5"), (n * 16.0 + 1000 * 30.0) / 1e6)

    def test_a_request_carrying_fallbacks_is_refused_whatever_its_value(self):
        # #114 review round 1, the review's repro: Fable 5.2 at max_tokens=1 with an Opus 5 fallback allowing 128k fast
        # output reserved $0.067656 although the fallback alone permits $6.40. Any `fallbacks` is now refused outright.
        repro = self.body(max_tokens=1, fallbacks=[{"model": "claude-opus-5", "max_tokens": 128_000, "speed": "fast"}])
        for fb in (repro, self.body(fallbacks="default"), self.body(fallbacks=[]), self.body(fallbacks=None)):
            with self.assertRaises(ac.Unbounded) as cm:
                ac.estimate_max_cost(fb, "claude-fable-5-2")
            self.assertIn("fallbacks", str(cm.exception))

    def test_images_are_bounded_by_their_pixels_not_their_base64(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png(1000, 1000)}}
        b = self.body(messages=[{"role": "user", "content": [img, {"type": "text", "text": "what is it"}]}])
        est = ac.estimate_max_cost(b, "claude-opus-5-5")
        tokens = int(1000 * 1000 / 750 * 1.5 + 0.9999) + 200
        self.assertGreaterEqual(est, (tokens * 8.0 + 1000 * 20.0) / 1e6)
        # inside a tool result too
        tr = {"type": "tool_result", "tool_use_id": "t", "content": [img]}
        self.assertGreater(ac.estimate_max_cost(self.body(messages=[{"role": "user", "content": [tr]}]),
                                                "claude-opus-5-5"), tokens * 8.0 / 1e6)

    def test_unbounded_content_is_refused(self):
        cases = [
            {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="}},
            {"type": "document", "source": {"type": "file", "file_id": "file_1"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "bm90IGEgcG5n"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": png(10, 10)}},
            {"type": "container_upload", "file_id": "f"},
        ]
        for block in cases:
            with self.assertRaises(ac.Unbounded, msg=block):
                ac.estimate_max_cost(self.body(messages=[{"role": "user", "content": [block]}]), "claude-opus-5-5")
        for field in ("mcp_servers", "container", "thread"):
            with self.assertRaises(ac.Unbounded):
                ac.estimate_max_cost(self.body(**{field: [{"x": 1}]}), "claude-opus-5-5")
        # a text document is bounded by its bytes
        doc = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "hello"}}
        ac.estimate_max_cost(self.body(messages=[{"role": "user", "content": [doc]}]), "claude-opus-5-5")

    def test_server_executed_tools_are_refused_and_client_tools_pass(self):
        for t in ("web_search_20250305", "web_fetch_20250910", "code_execution_20250825", "advisor_20260301",
                  "mcp_toolset"):
            self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": t, "name": "x"}]}), [t])
            with self.assertRaises(ac.Unbounded):
                ac.estimate_max_cost(self.body(tools=[{"type": t, "name": "x"}]), "claude-opus-5-5")
        for t in ({"name": "Read", "input_schema": {}}, {"type": "custom", "name": "x", "input_schema": {}},
                  {"type": "bash_20250124", "name": "bash"}, {"type": "text_editor_20250728", "name": "e"}):
            self.assertEqual(ac.unbounded_server_tools({"tools": [t]}), [])
        tools = [{"name": "Read", "input_schema": {}}] * 3
        b = self.body(tools=tools)
        n = len(json.dumps(b).encode()) + ac.TEMPLATE_TOKENS + 3 * ac.PER_TOOL_TOKENS
        self.assertAlmostEqual(ac.estimate_max_cost(b, "claude-opus-5-5"), (n * 8.0 + 1000 * 20.0) / 1e6)


class TestConfig(Base):
    def test_todays_grant_worked_example(self):
        st = self.state()
        self.assertTrue(st["on"], st["reason"])
        self.assertEqual(st["grant"]["amount_usd"], 200.0)
        self.assertEqual(st["grant"]["stop_line_usd"], 190.0)          # 95% of $200
        self.assertEqual(st["month"]["stop_line_usd"], 190.0)          # 95% of the $200 org limit
        self.assertEqual(st["headroom_usd"], 190.0)
        self.assertEqual(st["grant"]["expires_at"], ts("2026-12-03T00:00:00+00:00"))   # 19:00 EST = 00:00 UTC
        self.assertFalse(st["expiring_soon"])
        self.assertTrue(self.state(ts("2026-11-29T19:00:00-05:00"))["expiring_soon"])   # 3 days before expiry
        self.assertFalse(self.state(ts("2026-11-29T18:59:59-05:00"))["expiring_soon"])

    def test_the_lane_turns_off_an_hour_before_the_last_grant_expires_and_stays_off(self):
        self.assertTrue(self.state(ts("2026-12-02T17:59:59-05:00"))["on"])
        for t in ("2026-12-02T18:00:00-05:00", "2026-12-02T19:00:00-05:00", "2027-01-15T12:00:00-05:00"):
            st = self.state(ts(t))
            self.assertFalse(st["on"], t)
            self.assertIn("no counting grant", st["reason"])
            self.assertIsNone(self.reserve(0.01, ts(t)))
        # a new grant from the owner turns it on again; no refill is ever assumed
        self.write_config({**CONFIG, "grants": [GRANT, {"name": "next", "amount_usd": 50,
                                                        "granted": "2027-01-10T00:00:00-05:00",
                                                        "expires": "2027-02-10T00:00:00-05:00"}]})
        st = self.state(ts("2027-01-15T12:00:00-05:00"))
        self.assertTrue(st["on"], st["reason"])
        self.assertEqual(st["grant"]["stop_line_usd"], 47.5)

    def test_a_turn_is_admitted_only_if_its_time_cap_ends_before_the_grant_expires(self):
        # #114 review round 1: the hard turn cap is below the expiry margin, and reserve() refuses a turn whose cap
        # would end inside the slack before the earliest counting grant expires.
        self.assertLess(ac.MAX_TURN_S, ac.EXPIRY_MARGIN_S)
        expires = ts("2026-12-02T19:00:00-05:00")
        last = expires - ac.EXPIRY_MARGIN_S - 1          # the grant still counts, with the default cap it fits exactly
        self.assertIsNotNone(ac.reserve(0.01, last, self.ledger))
        self.assertIsNone(ac.reserve(0.01, last, self.ledger, duration_s=ac.MAX_TURN_S + 2))   # would end too late
        self.assertIsNone(ac.reserve(0.01, expires - ac.EXPIRY_MARGIN_S + 1, self.ledger))      # stopped counting
        for bad in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ac.LedgerError):
                ac.reserve(0.01, NOW, self.ledger, duration_s=bad)

    def test_a_grant_not_yet_granted_does_not_count(self):
        self.write_config({**CONFIG, "grants": [{**GRANT, "granted": "2026-11-25T00:00:00-05:00",
                                                 "remaining_as_of": "2026-11-25T00:00:00-05:00"}]})
        self.assertFalse(self.state()["on"])

    def test_remaining_below_amount_counts_as_used(self):
        self.write_config({**CONFIG, "grants": [{**GRANT, "remaining_usd": 150}]})
        st = self.state()
        self.assertEqual(st["grant"]["used_usd"], 50.0)
        self.assertEqual(st["headroom_usd"], 140.0)                    # 190 - 50
        self.assertEqual(st["pct_used"], 25.0)

    def test_the_month_limit_binds_when_it_is_lower(self):
        self.write_config({**CONFIG, "org_monthly_limit_usd": 20})
        self.assertEqual(self.state()["headroom_usd"], 19.0)
        self.assertIsNone(self.reserve(19.5))
        self.assertIsNotNone(self.reserve(19.0))
        self.assertFalse(self.state()["on"])                           # 95% of the month limit: hard stop

    def test_the_month_resets_on_the_utc_first_but_the_grant_keeps_counting(self):
        self.write_config({**CONFIG, "org_monthly_limit_usd": 20})
        late = ts("2026-11-29T12:00:00+00:00")
        rid = self.reserve(10.0, late)
        ac.settle(rid, 10.0, late, self.ledger)
        self.assertEqual(self.state(late)["month"]["used_usd"], 10.0)
        dec = ts("2026-12-01T12:00:00+00:00")
        st = self.state(dec)
        self.assertEqual(st["month"]["used_usd"], 0.0)
        self.assertEqual(st["grant"]["used_usd"], 10.0)
        # the last UTC day of the previous month still counts against the new one (a turn crossing midnight)
        eve = ts("2026-11-30T23:59:00+00:00")
        rid = self.reserve(5.0, eve)
        ac.settle(rid, 5.0, eve, self.ledger)
        self.assertEqual(self.state(dec)["month"]["used_usd"], 5.0)

    def test_bad_configs_turn_the_lane_off(self):
        bad = [
            "not json", [], {"grants": [GRANT]},                                    # no monthly limit
            {**CONFIG, "stop_fraction": 0.99},                                      # never over 95%
            {**CONFIG, "grants": [{**GRANT, "expires": "2026-12-02T19:00:00"}]},    # no UTC offset
            {**CONFIG, "grants": [{**GRANT, "amount_usd": -1}]},
            {**CONFIG, "grants": [{**GRANT, "remaining_usd": 300}]},
            {**CONFIG, "grants": [{k: v for k, v in GRANT.items() if k != "remaining_as_of"}]},
            {**CONFIG, "grants": [{**GRANT, "expires": GRANT["granted"]}]},
            {**CONFIG, "budget": 1},                                                # unknown field
            {**CONFIG, "enabled": "yes"},
        ]
        for cfg in bad:
            self.cfg.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
            st = self.state()
            self.assertFalse(st["on"], cfg)
            with self.assertRaises(ac.ConfigError, msg=cfg):
                self.reserve(0.01)
        self.cfg.unlink()
        self.assertIn("no API credit config", self.state()["reason"])
        with self.assertRaises(ac.LedgerError):
            self.reserve(0.01)

    def test_disabled_and_keyless_and_two_key_states_are_off(self):
        self.write_config({**CONFIG, "enabled": False})
        self.assertFalse(self.state()["on"])
        self.assertIsNone(self.reserve(0.01))
        self.write_config(CONFIG)
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": ""}):
            self.assertIn("no API key", self.state()["reason"])
            self.assertIsNone(self.reserve(0.01))
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY_2": "sk-other"}):
            self.assertIn("second credit key", self.state()["reason"])
            self.assertIsNone(self.reserve(0.01))
            self.assertEqual(ac.configured_orgs(), [])

    def test_state_never_contains_the_key(self):
        st = self.state()
        self.assertNotIn("sk-fake-test", json.dumps(st))
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY_2": "sk-other"}):
            self.assertNotIn("sk-other", json.dumps(self.state()))


class TestStopLine(Base):
    def test_reservations_stop_at_95_percent(self):
        rids = [self.reserve(10.0) for _ in range(19)]
        self.assertTrue(all(rids))
        self.assertIsNone(self.reserve(0.01))                          # $190 committed: the stop line
        st = self.state()
        self.assertFalse(st["on"])
        self.assertIn("hard stop", st["reason"])
        self.assertEqual(st["alert_level"], 95)

    def test_a_request_that_does_not_fit_is_refused_but_a_smaller_one_fits(self):
        self.assertIsNotNone(self.reserve(185.0))
        self.assertIsNone(self.reserve(5.01))
        self.assertIsNotNone(self.reserve(5.0))

    def test_settle_frees_the_unused_reservation_and_records_overruns(self):
        rid = self.reserve(100.0)
        ac.settle(rid, 1.25, NOW, self.ledger)
        self.assertEqual(self.state()["grant"]["used_usd"], 1.25)
        ac.settle(rid, 50.0, NOW, self.ledger)                          # a duplicate settle is a no-op
        self.assertEqual(self.state()["grant"]["used_usd"], 1.25)
        rid = self.reserve(1.0)
        ac.settle(rid, 3.0, NOW, self.ledger)                           # an overrun is recorded in full, never clamped
        self.assertEqual(self.state()["grant"]["used_usd"], 4.25)

    def test_alert_levels(self):
        self.assertEqual(ac.alert_level(None), 0)
        self.assertEqual(ac.alert_level(49.99), 0)
        self.assertEqual(ac.alert_level(50), 50)
        self.assertEqual(ac.alert_level(80), 80)
        self.assertEqual(ac.alert_level(94.9), 80)
        self.assertEqual(ac.alert_level(95), 95)
        rid = self.reserve(160.0)
        ac.settle(rid, 160.0, NOW, self.ledger)
        self.assertEqual(self.state()["alert_level"], 80)
        self.assertTrue(self.state()["on"])

    def test_concurrent_threads_never_pass_the_stop_line(self):
        # 32 threads race to reserve $7.31 each (and settle some at a random real cost below it): the committed total
        # can never pass $190, and exactly as many reservations succeed as fit
        cost = 7.31
        got: list = []
        barrier = threading.Barrier(32)

        def worker(i):
            barrier.wait()
            for _ in range(5):
                rid = self.reserve(cost)
                if rid is None:
                    return
                got.append(rid)
                if i % 2:
                    ac.settle(rid, cost * 0.5, NOW, self.ledger)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = json.loads(self.ledger.read_text())
        committed = sum(d["spent_usd"] + sum(d["reserved"].values()) for d in data["days"].values())
        self.assertLessEqual(committed, 190.0 + 1e-6)
        self.assertGreater(committed, 190.0 - cost)                     # and the credit was actually usable up to it
        self.assertFalse(self.state()["on"] and self.state()["headroom_usd"] >= cost)

    def test_concurrent_processes_never_pass_the_stop_line(self):
        # 6 separate processes (the provider's threads share one lock file with any other process on the machine)
        script = ("import sys; from cortados_router import api_credits as ac; from pathlib import Path\n"
                  "p = Path(sys.argv[1]); now = float(sys.argv[2]); n = 0\n"
                  "while True:\n"
                  "    rid = ac.reserve(3.33, now, p)\n"
                  "    if rid is None: break\n"
                  "    n += 1\n"
                  "    if n % 3 == 0: ac.settle(rid, 3.33, now, p)\n"
                  "print(n)\n")
        procs = [subprocess.Popen([sys.executable, "-B", "-c", script, str(self.ledger), repr(NOW)], cwd=str(REPO),
                                  env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(6)]
        counts = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            counts.append(int(out.strip()))
        data = json.loads(self.ledger.read_text())
        committed = sum(d["spent_usd"] + sum(d["reserved"].values()) for d in data["days"].values())
        self.assertLessEqual(committed, 190.0 + 1e-6)
        self.assertEqual(sum(counts), int(190.0 // 3.33))                # exactly what fits, across all processes


class TestFailClosed(Base):
    def test_a_corrupt_ledger_turns_the_lane_off(self):
        self.reserve(1.0)
        for text in ("{not json", "[]", json.dumps({"version": 1, "days": {}, "pruned_through": ""}),
                     json.dumps({"version": 2, "days": {"2026-11-22": {"spent_usd": -1, "reserved": {}}},
                                 "pruned_through": ""}),
                     json.dumps({"version": 2, "days": {"2026-11-22": {"spent_usd": 0, "reserved": {"x": 1}}},
                                 "pruned_through": ""}),
                     json.dumps({"version": 2, "days": {"nope": {"spent_usd": 0, "reserved": {}}},
                                 "pruned_through": ""}),
                     json.dumps({"version": 2, "days": {}, "pruned_through": 5}),
                     json.dumps({"version": 2, "days": {"2026-11-22": {"spent_usd": float("nan"), "reserved": {}}},
                                 "pruned_through": ""})):
            self.ledger.write_text(text)
            st = self.state()
            self.assertFalse(st["on"], text)
            self.assertTrue(st["reason"].startswith("ledger:"), st["reason"])
            with self.assertRaises(ac.LedgerError):
                self.reserve(0.01)
            self.assertEqual(self.ledger.read_text(), text)              # never "repaired" into a blank ledger

    def test_an_unreadable_ledger_turns_the_lane_off(self):
        self.reserve(1.0)
        self.ledger.chmod(0o000)
        self.addCleanup(self.ledger.chmod, 0o600)
        if os.access(self.ledger, os.R_OK):                              # running as root: permissions do not bite
            self.skipTest("root can read a 000 file")
        self.assertFalse(self.state()["on"])
        with self.assertRaises(ac.LedgerError):
            self.reserve(0.01)

    def test_an_unwritable_ledger_turns_the_lane_off(self):
        self.reserve(1.0)
        home = self.ledger.parent
        home.chmod(0o500)
        self.addCleanup(home.chmod, 0o700)
        if os.access(home, os.W_OK):
            self.skipTest("root can write a read-only directory")
        st = self.state()
        self.assertFalse(st["on"])
        self.assertIn("not writable", st["reason"])
        with self.assertRaises(ac.LedgerError):
            self.reserve(0.01)
        self.assertEqual(self.state()["open_reservations"], 1)          # nothing was lost

    def test_a_failed_write_leaves_the_ledger_as_it_was(self):
        rid = self.reserve(1.0)
        before = self.ledger.read_text()
        with mock.patch("cortados_router.api_credits.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ac.LedgerError):
                self.reserve(1.0)
            with self.assertRaises(ac.LedgerError):
                ac.settle(rid, 0.5, NOW, self.ledger)
        self.assertEqual(self.ledger.read_text(), before)                # the reservation still counts at full amount
        self.assertEqual(list(self.ledger.parent.glob(".*.tmp")), [])

    def test_a_pruned_window_or_a_clock_rollback_refuses(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps({"version": 2, "days": {}, "pruned_through": "2026-11-25"}))
        self.assertFalse(self.state()["on"])
        with self.assertRaises(ac.LedgerError):
            self.reserve(0.01)

    def test_bad_amounts_are_refused(self):
        for bad in (float("nan"), float("inf"), -1.0):
            with self.assertRaises(ac.LedgerError):
                self.reserve(bad)
        rid = self.reserve(1.0)
        with self.assertRaises(ac.LedgerError):
            ac.settle(rid, float("nan"), NOW, self.ledger)


class TestWindowNumbers(Base):
    def test_window_reads_the_stop_line_as_full(self):
        used, reset, window = ac.window_numbers(NOW, self.ledger)
        self.assertEqual(used, 0.0)
        self.assertEqual(reset, ts("2026-12-02T18:00:00-05:00"))         # stops counting an hour before expiry
        rid = self.reserve(95.0)
        ac.settle(rid, 95.0, NOW, self.ledger)
        self.assertAlmostEqual(ac.window_numbers(NOW, self.ledger)[0], 50.0)   # $95 of the $190 stop line

    def test_window_raises_on_a_broken_ledger(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{")
        with self.assertRaises(ac.LedgerError):
            ac.window_numbers(NOW, self.ledger)

    def test_status_cli_prints_metadata_only(self):
        out = subprocess.run([sys.executable, "-B", "-m", "cortados_router.api_credits", "status"], cwd=str(REPO),
                             env=dict(os.environ), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"lane": "anthropic-api"', out.stdout)
        self.assertNotIn("sk-fake-test", out.stdout + out.stderr)


if __name__ == "__main__":
    unittest.main()
