"""Unit tests for the admission-controlled API-credit meter (cortados_router/api_credits.py). Stdlib only; every
test uses an explicit temp meter path so the real ~/.cortados state is never touched."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import api_credits as ac

T = 1793000000.0


class TestPricing(unittest.TestCase):
    def test_price_for(self):
        self.assertEqual(ac.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(ac.price_for("claude-opus-5-5-20260401")["output"], 20.0)   # dated -> base
        self.assertEqual(ac.price_for("unknown-model"), ac.DEFAULT_PRICE)            # unknown -> most expensive

    def test_pricing_is_conservative(self):
        # cache WRITE is priced at the 1-hour rate (input x2), never the cheaper 5-minute rate
        self.assertEqual(ac.price_for("claude-opus-5-5")["cache_write"], 8.0)        # 4 x 2
        # Haiku uses its >100k tier so the guard never under-counts
        self.assertEqual(ac.price_for("claude-haiku-5-5")["input"], 0.50)

    def test_cost_from_usage_and_bad_counts(self):
        self.assertAlmostEqual(ac.cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000), 24.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=-5), 0.0)
        self.assertEqual(ac.cost_usd("claude-haiku-5-5", input_tokens=float("inf")), 0.0)
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", {"input_tokens": 1_000_000}), 4.0)

    def test_estimate_max_cost_is_an_upper_bound(self):
        body = {"model": "api/claude-opus-5-5", "max_tokens": 1000, "messages": [{"role": "user", "content": "hi"}]}
        est = ac.estimate_max_cost(body, "claude-opus-5-5")
        self.assertGreater(est, 0)
        # output ceiling alone (1000 tok @ $20/M) is already in the estimate
        self.assertGreaterEqual(est, 1000 * 20.0 / 1_000_000)

    def test_client_tool_named_like_a_server_tool_gets_no_allowance(self):
        # a CLIENT tool merely NAMED "web_search" must not receive the server web-search reservation (classify by type)
        self.assertEqual(ac._server_tool_allowance([{"name": "web_search", "input_schema": {"type": "object"}}]), 0.0)
        # the genuine server tool, identified by its `type`, still does
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305", "max_uses": 5}]), 0.05, places=4)

    def test_server_tool_charges_are_counted(self):
        # a completed turn that ran 10 web searches is billed $0.01 each on top of tokens
        usage = {"input_tokens": 1_000_000, "server_tool_use": {"web_search_requests": 10}}
        self.assertAlmostEqual(ac.cost_from_usage("claude-opus-5-5", usage), 4.0 + 0.10, places=4)
        self.assertEqual(ac.server_tool_cost({"server_tool_use": {"web_search_requests": 3}}), 0.03)
        self.assertEqual(ac.server_tool_cost({}), 0.0)

    def test_estimate_reserves_for_declared_web_search(self):
        # the per-tool allowance is exactly max_uses x $0.01; no cap -> a conservative default count
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305", "name": "web_search", "max_uses": 7}]), 0.07, places=4)
        self.assertAlmostEqual(ac._server_tool_allowance([{"type": "web_search_20250305"}]),
                               ac.DEFAULT_SERVER_TOOL_USES * 0.01, places=4)
        self.assertEqual(ac._server_tool_allowance([{"type": "bash_20250124"}]), 0.0)   # not a billed server tool
        # and the allowance is actually folded into the reservation (tool body also lifts the token estimate)
        plain = {"model": "api/claude-opus-5-5", "max_tokens": 10, "messages": []}
        withtool = dict(plain, tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 7}])
        self.assertGreaterEqual(ac.estimate_max_cost(withtool, "claude-opus-5-5"),
                                ac.estimate_max_cost(plain, "claude-opus-5-5") + 0.07)


class TestServerToolAdmission(unittest.TestCase):
    def test_every_server_executed_tool_is_refused(self):
        # web_search and web_fetch both pull unbounded external content into input tokens; code_execution bills by
        # container-hour -> all three are refused on the credit lane. Only client tools pass.
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": "code_execution_20250522"}]}), ["code_execution_20250522"])
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": "web_fetch_20250910"}]}), ["web_fetch_20250910"])
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"type": "web_search_20250305"}]}), ["web_search_20250305"])
        self.assertEqual(ac.unbounded_server_tools({"tools": [{"name": "my_tool", "input_schema": {}}]}), [])  # a client tool
        self.assertEqual(ac.unbounded_server_tools({}), [])

    def test_enforce_server_tool_caps_injects_and_matches_the_reserve(self):
        body = {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}
        out = ac.enforce_server_tool_caps(body)
        self.assertEqual(out["tools"][0]["max_uses"], ac.DEFAULT_SERVER_TOOL_USES)
        self.assertIsNot(out, body)                               # copied; the original is not mutated
        self.assertNotIn("max_uses", body["tools"][0])
        # the enforced cap equals what the meter reserves for that tool, so admission is a real bound
        self.assertAlmostEqual(ac._server_tool_allowance(out["tools"]), ac.DEFAULT_SERVER_TOOL_USES * 0.01, places=4)
        declared = {"tools": [{"type": "web_search_20250305", "max_uses": 3}]}
        self.assertIs(ac.enforce_server_tool_caps(declared), declared)   # already capped -> unchanged


class TestPeriod(unittest.TestCase):
    def test_reset_day_shifts_the_period(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_RESET_DAY": "15"}):
            start, nxt = ac.period_bounds(T)
            self.assertTrue(nxt > start)
            self.assertTrue(28 <= round((nxt - start) / 86400) <= 31)   # a calendar month (DST may add an hour)

    def test_reset_day_31_clamps_to_short_months(self):
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_RESET_DAY": "31"}):
            self.assertEqual(ac.reset_day(), 31)
            # Feb has no 31st, so the effective boundary is its last day; no crash, still a ~month window.
            feb = __import__("datetime").datetime(2026, 2, 10).astimezone().timestamp()
            start, nxt = ac.period_bounds(feb)
            self.assertTrue(28 <= round((nxt - start) / 86400) <= 31)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        # One configured org ($10 window). A key must be configured for reserve() to have an org to charge.
        self.env = mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                                 "CORTADOS_ANTHROPIC_API_KEY": "sk-org-1"}, clear=False)
        self.env.start()
        os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)   # ensure a single org for this class

    def tearDown(self):
        self.env.stop()

    def test_reserve_respects_budget_and_settle_reconciles(self):
        r1 = ac.reserve(6.0, T, self.meter)
        self.assertIsNotNone(r1)
        self.assertEqual(ac.committed(T, self.meter), 6.0)
        self.assertIsNone(ac.reserve(6.0, T, self.meter))            # would exceed $10 -> refused
        ac.settle(r1, 4.0, T, self.meter)                            # actual < reserved
        self.assertEqual(ac.spent(T, self.meter), 4.0)
        self.assertEqual(ac.committed(T, self.meter), 4.0)
        self.assertFalse(ac.over_budget(T, self.meter))

    def test_settle_records_the_actual_even_above_the_reservation(self):
        r = ac.reserve(3.0, T, self.meter)
        ac.settle(r, 5.0, T, self.meter)                            # actual exceeded the $3 reservation -> recorded in full, not clamped
        self.assertEqual(ac.spent(T, self.meter), 5.0)

    def test_fail_closed_on_a_non_finite_balance(self):
        self.meter.parent.mkdir(parents=True, exist_ok=True)
        self.meter.write_text('{"periods": {"2026-10": {"spent_usd": Infinity, "reserved": {}}}}')
        with self.assertRaises(ac.LedgerError):                     # a non-finite balance must not pass validation
            ac.reserve(1.0, T, self.meter)
        self.assertTrue(ac.over_budget(T, self.meter))             # read-only view of invalid state -> fully committed

    def test_over_budget_counts_outstanding_reservations(self):
        ac.reserve(10.0, T, self.meter)
        self.assertTrue(ac.over_budget(T, self.meter))              # fully committed even before settle

    def test_settle_is_idempotent(self):
        r = ac.reserve(6.0, T, self.meter)
        ac.settle(r, 4.0, T, self.meter)
        ac.settle(r, 4.0, T, self.meter)                            # a retried/duplicated settle must not double-charge
        self.assertEqual(ac.spent(T, self.meter), 4.0)

    def test_pruning_keeps_a_period_with_an_outstanding_reservation(self):
        old = ac.reserve(6.0, T, self.meter)                        # period A, left outstanding
        ac.reserve(1.0, T + 40 * 86400, self.meter)                # period B
        ac.reserve(1.0, T + 75 * 86400, self.meter)                # period C (A is now older than the 2 most recent)
        ac.settle(old, 4.0, T, self.meter)                         # A was not pruned (it had a reservation) -> charge lands
        self.assertEqual(ac.spent(T, self.meter), 4.0)

    def test_reserve_into_an_older_resolved_period_persists(self):
        # a delayed caller (or clock rollback) reserves into an OLD period that newer periods have aged past and
        # that was already settled; the new reservation must not be lost to a detached, pruned ledger.
        aug = ac.reserve(6.0, T, self.meter)
        ac.reserve(1.0, T + 40 * 86400, self.meter)
        ac.reserve(1.0, T + 75 * 86400, self.meter)
        ac.settle(aug, 4.0, T, self.meter)                      # August resolved (no outstanding reservation)
        late = ac.reserve(2.0, T, self.meter)                   # reserve AGAIN into the old, now-resolved period
        self.assertIsNotNone(late)
        ac.settle(late, 2.0, T, self.meter)
        self.assertEqual(ac.spent(T, self.meter), 6.0)          # 4 + 2 both recorded; nothing vanished

    def test_reserve_into_a_pruned_historical_period_is_refused(self):
        p0 = ac.reserve(5.0, T, self.meter)
        ac.settle(p0, 5.0, T, self.meter)                                   # period 0, fully resolved
        for d in (40, 75, 110):                                             # three newer, resolved periods
            r = ac.reserve(1.0, T + d * 86400, self.meter)
            ac.settle(r, 1.0, T + d * 86400, self.meter)
        # period 0 has aged past the retained window and been pruned; a stale-timestamp reserve must NOT reopen it
        self.assertIsNone(ac.reserve(5.0, T, self.meter))
        self.assertEqual(ac.spent(T, self.meter), 0.0)                      # reads empty (pruned), not its old $5

    def test_late_settle_lands_in_its_own_period(self):
        old = ac.reserve(5.0, T, self.meter)                        # period A
        later = T + 40 * 86400                                      # a later billing period
        ac.reserve(4.0, later, self.meter)
        ac.settle(old, 5.0, later, self.meter)                      # late settle of the old reservation
        self.assertEqual(ac.spent(later, self.meter), 0.0)         # the new period is untouched
        self.assertEqual(ac.spent(T, self.meter), 5.0)            # the old period got the charge

    def test_fail_closed_on_a_corrupt_ledger(self):
        self.meter.parent.mkdir(parents=True, exist_ok=True)
        self.meter.write_text("{ not json")
        with self.assertRaises(ac.LedgerError):
            ac.reserve(1.0, T, self.meter)
        # a read-only view of a corrupt ledger treats the period as fully committed (pacing fails safe)
        self.assertTrue(ac.over_budget(T, self.meter))

    def test_window_numbers_tracks_committed(self):
        ac.reserve(9.0, T, self.meter)
        used_pct, reset_at, window_s = ac.window_numbers(T, self.meter)
        self.assertEqual(used_pct, 90.0)
        self.assertGreater(reset_at, T)


class TestTwoOrgs(unittest.TestCase):
    """Two credit orgs (two keys), each with its own $10 window — $20 total. Admission drains org 1 first."""
    def setUp(self):
        self.meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        self.env = mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                                 "CORTADOS_ANTHROPIC_API_KEY": "sk-org-1",
                                                 "CORTADOS_ANTHROPIC_API_KEY_2": "sk-org-2"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_configured_orgs_and_total_budget(self):
        self.assertEqual(ac.configured_orgs(),
                         [(ac._org_id("sk-org-1"), "sk-org-1"), (ac._org_id("sk-org-2"), "sk-org-2")])
        self.assertEqual(ac.budget_usd(), 10.0)          # per org
        self.assertEqual(ac.total_budget_usd(), 20.0)    # both orgs

    def test_reserve_drains_org1_then_overflows_to_org2(self):
        r1 = ac.reserve(8.0, T, self.meter)
        self.assertEqual(ac.key_for_reservation(r1), "sk-org-1")   # first org
        r2 = ac.reserve(8.0, T, self.meter)                        # org 1 would exceed $10 -> org 2
        self.assertEqual(ac.key_for_reservation(r2), "sk-org-2")
        self.assertEqual(ac.committed(T, self.meter), 16.0)        # summed across both orgs
        self.assertIsNone(ac.reserve(8.0, T, self.meter))          # both orgs full ($8 of $10 each) -> refused

    def test_key_for_reservation_routes_to_the_right_org(self):
        r1 = ac.reserve(8.0, T, self.meter)
        r2 = ac.reserve(8.0, T, self.meter)
        self.assertEqual(ac.key_for_reservation(r1), "sk-org-1")
        self.assertEqual(ac.key_for_reservation(r2), "sk-org-2")

    def test_settle_charges_only_its_own_org(self):
        r_org1 = ac.reserve(8.0, T, self.meter)          # first reserve -> org 1
        self.assertEqual(ac.key_for_reservation(r_org1), "sk-org-1")
        ac.reserve(9.0, T, self.meter)                   # org 1 full (8+9>10) -> org 2
        self.assertEqual(ac.committed(T, self.meter), 17.0)
        ac.settle(r_org1, 2.0, T, self.meter)            # settle org 1; org 2's reservation is untouched
        self.assertAlmostEqual(ac.spent(T, self.meter), 2.0, places=4)
        self.assertAlmostEqual(ac.committed(T, self.meter), 2.0 + 9.0, places=4)   # spent 2 (org1) + 9 reserved (org2)

    def test_deconfigured_org_has_no_key(self):
        r_org1 = ac.reserve(8.0, T, self.meter)          # org 1
        self.assertEqual(ac.key_for_reservation(r_org1), "sk-org-1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
            # an org-2 reservation now has no key (that org was removed from config)
            r_org2 = ac._org_id("sk-org-2") + "|" + ac.period_key(T) + ":deadbeef"
            self.assertIsNone(ac.key_for_reservation(r_org2))
            self.assertEqual(ac.key_for_reservation(r_org1), "sk-org-1")   # org 1 still configured

    def test_key_swap_between_slots_keeps_spend_with_its_key(self):
        # Codex P1 regression: fully spend org A, then swap the keys between slots. A second reservation must NOT
        # grant A a fresh window — spend stays bound to the key, not the slot.
        rA = ac.reserve(10.0, T, self.meter)             # org 1 == key A, fully reserved
        ac.settle(rA, 10.0, T, self.meter)               # A's $10 window is spent
        self.assertEqual(ac.key_for_reservation(rA), "sk-org-1")
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "sk-org-2",
                                          "CORTADOS_ANTHROPIC_API_KEY_2": "sk-org-1"}, clear=False):
            r2 = ac.reserve(10.0, T, self.meter)         # key A is exhausted wherever it sits -> lands on key B
            self.assertIsNotNone(r2)
            self.assertEqual(ac.key_for_reservation(r2), "sk-org-2")   # dispatched with B, not A
            self.assertEqual(ac.key_for_reservation(rA), "sk-org-1")   # A's old reservation still maps to A
            self.assertIsNone(ac.reserve(1.0, T, self.meter))          # now both keys spent -> refused ($20 total cap)

    def test_overrun_on_one_org_cannot_push_the_lane_over_total(self):
        # Codex round-3: a settled overrun on org A (actual > reserved, recorded un-clamped) eats the lane's
        # headroom; a later reservation on org B must be refused once the AGGREGATE would exceed the $20 total,
        # even though B's own $10 window still looks open.
        r = ac.reserve(6.0, T, self.meter)               # org A
        ac.settle(r, 11.0, T, self.meter)                # A overran to $11 (recorded in full)
        self.assertAlmostEqual(ac.committed(T, self.meter), 11.0, places=4)
        self.assertIsNone(ac.reserve(10.0, T, self.meter))   # would make the lane $21 > $20 -> refused
        self.assertLessEqual(ac.committed(T, self.meter), ac.total_budget_usd())
        self.assertIsNotNone(ac.reserve(9.0, T, self.meter))  # $11 + $9 = $20 exactly still fits (on org B)

    def test_same_key_in_two_slots_is_one_org(self):
        with mock.patch.dict(os.environ, {"CORTADOS_ANTHROPIC_API_KEY": "dup",
                                          "CORTADOS_ANTHROPIC_API_KEY_2": "dup"}, clear=False):
            self.assertEqual(ac.configured_orgs(), [(ac._org_id("dup"), "dup")])   # collapses to one org
            self.assertEqual(ac.total_budget_usd(), 10.0)                          # NOT $20 for one key
            r = ac.reserve(10.0, T, self.meter)
            self.assertIsNotNone(r)
            self.assertIsNone(ac.reserve(1.0, T, self.meter))                      # one $10 window, no phantom second


class TestLegacyMigration(unittest.TestCase):
    def test_legacy_single_org_file_is_read_as_org_1(self):
        meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        period = ac.period_key(T)
        meter.parent.mkdir(parents=True, exist_ok=True)
        meter.write_text(json.dumps({"periods": {period: {"spent_usd": 3.0, "reserved": {}}}}))
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                          "CORTADOS_ANTHROPIC_API_KEY": "sk-org-1"}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
            self.assertEqual(ac.spent(T, meter), 3.0)               # legacy spend read under the primary org
            r = ac.reserve(2.0, T, meter)                           # a new reservation migrates the file in place
            self.assertEqual(ac.key_for_reservation(r), "sk-org-1")
            ac.settle(r, 2.0, T, meter)
            self.assertEqual(ac.spent(T, meter), 5.0)               # 3 legacy + 2 new
            # a legacy reservation id (no org prefix) carries no org; settle finds its owner by searching
            self.assertEqual(ac._org_of("%s:abc123" % period), (None, period))
            data = json.loads(meter.read_text())
            self.assertIn("orgs", data)                             # persisted in the nested shape
            self.assertIn(ac._org_id("sk-org-1"), data["orgs"])     # keyed by the primary key's fingerprint

    def test_malformed_prune_cursor_fails_closed(self):
        # Round-1/2 regression: a prune cursor that is PRESENT but not a string (int OR explicit null) must RAISE,
        # never be dropped (which would let an already-pruned period reopen blank and re-admit its spent budget).
        for bad in (202610, None):                                 # int, and JSON null
            meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
            period = ac.period_key(T)
            meter.parent.mkdir(parents=True, exist_ok=True)
            meter.write_text(json.dumps({"periods": {period: {"spent_usd": 1.0, "reserved": {}}},
                                         "pruned_through": bad}))
            with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                              "CORTADOS_ANTHROPIC_API_KEY": "sk-org-1"}, clear=False):
                os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
                with self.assertRaises(ac.LedgerError):
                    ac.reserve(1.0, T, meter)
                self.assertTrue(ac.over_budget(T, meter))           # read-only view also fails safe (fully committed)

    def test_legacy_ledger_with_no_key_fails_closed(self):
        # Codex round-2: with NO key configured, a legacy ledger cannot be attributed to an org -> fail closed
        # rather than parking spend under a sentinel that a later-restored key would not see (a freed window).
        meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        period = ac.period_key(T)
        meter.parent.mkdir(parents=True, exist_ok=True)
        meter.write_text(json.dumps({"periods": {period: {"spent_usd": 5.0, "reserved": {}}}}))
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10"}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY", None)
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
            with self.assertRaises(ac.LedgerError):
                ac.reserve(1.0, T, meter)

    def test_legacy_file_with_multiple_keys_fails_closed(self):
        # Codex round-2 P1: a legacy (unattributable) ledger must NOT be migrated once >1 key is configured —
        # guessing the owner could free the real owner's spent window. Fail closed until it is upgraded single-key.
        meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        period = ac.period_key(T)
        meter.parent.mkdir(parents=True, exist_ok=True)
        meter.write_text(json.dumps({"periods": {period: {"spent_usd": 10.0, "reserved": {}}}}))
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                          "CORTADOS_ANTHROPIC_API_KEY": "sk-B",
                                          "CORTADOS_ANTHROPIC_API_KEY_2": "sk-A"}, clear=False):
            with self.assertRaises(ac.LedgerError):
                ac.reserve(10.0, T, meter)
            self.assertTrue(ac.over_budget(T, meter))              # read-only view fails safe too

    def test_legacy_reservation_settles_on_its_true_owner_after_swap(self):
        # Codex round-2 P2: a legacy reservation id (no org) must settle on whichever org actually holds it, found
        # by search, even after the primary key changes — not follow the current primary into the wrong org.
        meter = Path(tempfile.mkdtemp()) / "api-credit-meter.json"
        period = ac.period_key(T)
        legacy_rid = "%s:old-turn" % period
        meter.parent.mkdir(parents=True, exist_ok=True)
        meter.write_text(json.dumps({"periods": {period: {"spent_usd": 0.0, "reserved": {legacy_rid: 6.0}}}}))
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                          "CORTADOS_ANTHROPIC_API_KEY": "sk-A"}, clear=False):
            os.environ.pop("CORTADOS_ANTHROPIC_API_KEY_2", None)
            ac.reserve(1.0, T, meter)                              # persists the migration under A's fingerprint
        # now the keys change: A is demoted to slot 2, B becomes primary
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "10",
                                          "CORTADOS_ANTHROPIC_API_KEY": "sk-B",
                                          "CORTADOS_ANTHROPIC_API_KEY_2": "sk-A"}, clear=False):
            ac.settle(legacy_rid, 4.0, T, meter)                  # must find the reservation under A and settle it
            data = json.loads(meter.read_text())
            a_led = data["orgs"][ac._org_id("sk-A")]["periods"][period]
            self.assertNotIn(legacy_rid, a_led["reserved"])        # the stuck reservation was released
            self.assertAlmostEqual(a_led["spent_usd"], 4.0, places=4)   # A's actual $4 recorded on A, not lost


class TestBudgetEnv(unittest.TestCase):
    def test_budget_default_override_and_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORTADOS_API_CREDIT_BUDGET_USD", None)
            self.assertEqual(ac.budget_usd(), 200.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "nonsense"}):
            self.assertEqual(ac.budget_usd(), 200.0)
        with mock.patch.dict(os.environ, {"CORTADOS_API_CREDIT_BUDGET_USD": "400"}):
            self.assertEqual(ac.budget_usd(), 400.0)


if __name__ == "__main__":
    unittest.main()
