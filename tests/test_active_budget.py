"""The smaller memory budget while the owner is active (owner decision 2026-11-22, "smaller while I work"; #126 Pro
capacity; docs/LOCAL-RESOURCES.md "Active budget"): the governor admits text turns against active_memory_budget_bytes -
weights_bytes (17.25 GiB beside the 4-bit weights: the corrected 128k reservation, 17.06 GiB, fits) while the owner is
active and memory_budget_bytes - weights_bytes (25 GiB) while the owner is idle; a turn over the budget in force alone
is too_large (413, sent elsewhere at once), the owner's return never revokes a running turn, and the capacity report is that of the budget in force.
Fakes only: FixedGovernor (no ioreg/sysctl), the in-thread fake vllm-mlx of test_local_mlx."""
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import local_gate, local_governor
from cortados_router.provider import local_lane
from tests.test_local_mlx import EXAMPLE, FakeVllm, FixedGovernor, LONG_MSG, MSG, MlxGateHarness, turns, wait_for

GIB = 2 ** 30
ACTIVE, IDLE = 30.0, 3600.0                     # owner idle seconds: below / above active_idle_s (600)
FULL, HALF = 131072, 65536                      # a full-context turn and a 64k one (input + max_tokens)
MEASURED_128K = int(11.93 * GIB)                # one 130,872-token turn alone, peak over idle (Pro, 2026-11-22)
SIX_BIT = int(22.4 * GIB)                       # the 6-bit copy's weights
SIX_BIT_ACTIVE_FIT = 72361                      # its fit_tokens while the owner works (9.85 GiB beside the weights)


class ActiveBudgetGovernorTests(unittest.TestCase):
    """Admission against the budget in force, on the default 4-bit text-only model (17.25 GiB active, 25 GiB idle)."""

    def admission(self, idle_s, **kw):
        g = FixedGovernor(idle_s=idle_s, **kw)
        return g, local_governor.Admission(g)

    def turn(self, a, tokens):
        return a.text_load(tokens - 1, 1)                                # input bound + max_tokens 1 = tokens

    def test_defaults_are_17_25_gib_active_and_25_gib_idle_beside_the_4bit_weights(self):
        c = local_governor.check_config({})
        self.assertEqual(local_governor.available_bytes(c, "active"), 17 * GIB + GIB // 4)
        self.assertEqual(local_governor.available_bytes(c, "idle"), 25 * GIB)
        self.assertEqual(local_governor.available_bytes(c), 25 * GIB)    # None: the most it can ever be
        self.assertEqual(local_governor.max_seqs(c), 8)                  # --max-num-seqs from the idle budget, as before

    def test_the_corrected_128k_reservation_is_above_the_measured_peak_and_within_the_active_budget(self):
        # measured 2026-11-22 on the Pro: one 130,872-token turn alone peaked 11.93 GiB over the idle footprint
        c = local_governor.check_config({})
        full = local_governor.text_set_bytes(c, [local_governor._text_load(c, FULL, FULL)])
        self.assertAlmostEqual(full["total"] / GIB, 17.06, places=2)
        self.assertGreaterEqual(full["total"] - MEASURED_128K, 5 * GIB)  # the stated margin: 5.13 GiB
        self.assertLessEqual(full["total"], local_governor.available_bytes(c, "active"))
        self.assertEqual(full["kv"], (FULL + 256) * 65536 * 17 // 16)     # 8 GiB + one layer's 0.5 GiB copy
        # two whole copies of the batch (the formula before the correction) could not have fitted the measurement
        self.assertGreater(2 * (FULL + 256) * c["kv_bytes_per_token"], MEASURED_128K)

    def test_per_token_kv_counts_only_the_full_attention_layers(self):
        # qwen3_5's layer_types as the Pro's config.json writes them: every 4th of 64 layers is full attention
        layer_types = ["full_attention" if (i + 1) % 4 == 0 else "linear_attention" for i in range(64)]
        full_layers = layer_types.count("full_attention")
        self.assertEqual(full_layers, 16)
        self.assertEqual(local_governor.TEXT_FULL_ATTENTION_LAYERS, full_layers)
        per_token = full_layers * 2 * 4 * 256 * 2                       # K and V x 4 KV heads x head_dim 256 x fp16
        self.assertEqual(local_governor.text_kv_bytes_per_token(), per_token)
        self.assertEqual(local_governor.check_config({})["kv_bytes_per_token"], per_token)
        self.assertEqual(json.loads(EXAMPLE.read_text())["governor"]["kv_bytes_per_token"], per_token)
        # all 64 layers (24 heads' worth would be more still) would have been 4x: 32 GiB at 128k
        self.assertEqual(64 * 2 * 4 * 256 * 2 * FULL, 32 * GIB)

    def test_active_admits_a_128k_turn_and_nothing_beside_it(self):
        _, a = self.admission(ACTIVE)
        t, rep = a.try_acquire(load=self.turn(a, FULL))
        self.assertIsNotNone(t, rep.get("reason"))
        self.assertAlmostEqual(a.reserved / GIB, 17.06, places=2)
        self.assertEqual((rep["budget_bytes"], rep["free_tokens"]), (17 * GIB + GIB // 4, 0))
        t2, rep = a.try_acquire(load=self.turn(a, 1024))
        self.assertEqual((t2, rep["refused"]), (None, "busy"))           # retryable: it fits once the long one ends
        t.release()
        self.assertEqual((a.inflight, a.reserved), (0, 0))

    def test_active_refuses_what_fits_only_the_idle_budget_as_too_large(self):
        # the 6-bit copy (22.4 GB of weights): 9.85 GiB beside them while the owner works, 17.6 GiB while away
        _, a = self.admission(ACTIVE, weights_bytes=SIX_BIT)
        t, rep = a.try_acquire(load=self.turn(a, FULL))
        self.assertIsNone(t)
        self.assertEqual(rep["refused"], "too_large")                    # not a 529: it goes elsewhere now
        self.assertIn("while the owner is active", rep["reason"])
        self.assertEqual((a.inflight, a.reserved), (0, 0))
        self.assertEqual(rep["fit_tokens"], SIX_BIT_ACTIVE_FIT)
        t, rep = a.try_acquire(load=self.turn(a, HALF))
        self.assertIsNotNone(t, rep.get("reason"))
        t.release()
        _, a = self.admission(IDLE, weights_bytes=SIX_BIT)
        t, rep = a.try_acquire(load=self.turn(a, FULL))
        self.assertIsNotNone(t, rep.get("reason"))
        t.release()

    def test_idle_admits_a_128k_turn(self):
        _, a = self.admission(IDLE)
        t, rep = a.try_acquire(load=self.turn(a, FULL))
        self.assertIsNotNone(t)
        self.assertAlmostEqual(a.reserved / GIB, 17.06, places=2)
        t.release()

    def test_unreadable_idle_time_counts_active_so_the_smaller_budget(self):
        _, a = self.admission(None, weights_bytes=SIX_BIT)
        self.assertEqual(a.try_acquire(load=self.turn(a, FULL))[1]["refused"], "too_large")
        _, a = self.admission(None)
        self.assertEqual(a.snapshot()["budget_bytes"], 17 * GIB + GIB // 4)

    def test_owner_returning_never_revokes_running_turns_and_blocks_new_ones_until_room_frees(self):
        # three 64k turns admitted while the owner is away reserve 18.17 GiB: over the 17.25 GiB active budget
        g, a = self.admission(IDLE, active_cap=8)                        # the count is not what holds them here
        running = [a.try_acquire(load=self.turn(a, HALF))[0] for _ in range(3)]
        self.assertTrue(all(running))
        held = a.reserved
        self.assertGreater(held, 17 * GIB + GIB // 4)
        g.set(idle_s=ACTIVE)                                             # the owner is back
        snap = a.snapshot()
        self.assertEqual((snap["owner"], snap["inflight"], snap["reserved_bytes"]), ("active", 3, held))
        self.assertEqual((snap["budget_bytes"], snap["free_bytes"], snap["free_tokens"]),
                         (17 * GIB + GIB // 4, 0, 0))
        self.assertFalse(snap["draining"])                               # over budget, not a stale width
        for tokens in (1024, HALF):                                      # nothing new, small or 64k: busy, retryable
            t, rep = a.try_acquire(load=self.turn(a, tokens))
            self.assertIsNone(t)
            self.assertEqual(rep["refused"], "busy")
            self.assertIn("memory budget: 0.0 GiB free", rep["reason"])
        self.assertEqual((a.inflight, a.reserved), (3, held))            # the running turns kept everything
        for t in running:                                                # they end on their own
            t.release()
        self.assertEqual((a.inflight, a.reserved), (0, 0))
        t, rep = a.try_acquire(load=self.turn(a, FULL))                  # room again, under the smaller budget
        self.assertIsNotNone(t, rep.get("reason"))
        t.release()

    def test_going_idle_with_turns_running_widens_the_room_at_once(self):
        g, a = self.admission(ACTIVE)
        t, _ = a.try_acquire(load=self.turn(a, HALF))
        before = a.snapshot()
        self.assertEqual(before["free_tokens"], 85012)
        g.set(idle_s=IDLE)
        after = a.snapshot()
        self.assertEqual(after["budget_bytes"], 25 * GIB)
        self.assertEqual(after["free_tokens"], 126287)
        self.assertIsNotNone(a.try_acquire(load=self.turn(a, 100_000))[0])

    def test_capacity_report_in_both_states(self):
        active_budget = 17 * GIB + GIB // 4
        g, a = self.admission(ACTIVE)
        snap = a.snapshot()
        self.assertEqual((snap["owner"], snap["budget_bytes"], snap["free_bytes"], snap["reserved_bytes"]),
                         ("active", active_budget, active_budget, 0))
        self.assertEqual((snap["fit_tokens"], snap["free_tokens"]), (FULL, FULL))   # 128k while the owner works
        g.set(idle_s=IDLE)
        snap = a.snapshot()
        self.assertEqual((snap["owner"], snap["budget_bytes"], snap["free_bytes"]), ("idle", 25 * GIB, 25 * GIB))
        self.assertEqual((snap["fit_tokens"], snap["free_tokens"]), (FULL, FULL))
        # beside one running 64k turn, each state's free bytes are its own budget minus the reservation
        t, _ = a.try_acquire(load=self.turn(a, HALF))
        idle = a.snapshot()
        g.set(idle_s=ACTIVE)
        active = a.snapshot()
        self.assertEqual(idle["free_bytes"], 25 * GIB - a.reserved)
        self.assertEqual(active["free_bytes"], active_budget - a.reserved)
        self.assertEqual(active["fit_tokens"], FULL)
        self.assertLess(active["free_tokens"], idle["free_tokens"])
        t.release()
        # the 6-bit copy's report: the active fit is its own
        _, six = self.admission(ACTIVE, weights_bytes=SIX_BIT)
        self.assertEqual(six.snapshot()["fit_tokens"], SIX_BIT_ACTIVE_FIT)

    def test_a_vision_models_per_request_bytes_use_the_budget_in_force_too(self):
        g, a = self.admission(ACTIVE, memory_budget_bytes=40 * GIB, active_memory_budget_bytes=20 * GIB)
        self.assertEqual(a.snapshot()["budget_bytes"], 5 * GIB)
        self.assertEqual(a.try_acquire(6 * GIB)[1]["refused"], "too_large")
        g.set(idle_s=IDLE)
        t, _ = a.try_acquire(6 * GIB)
        self.assertIsNotNone(t)
        t.release()


class ActiveBudgetConfigTests(unittest.TestCase):
    def test_active_above_idle_is_refused_and_an_unset_one_never_exceeds_the_idle_budget(self):
        with self.assertRaises(local_governor.GovernorConfigError):
            local_governor.check_config({"memory_budget_bytes": 30 * GIB, "active_memory_budget_bytes": 31 * GIB})
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(local_governor.GovernorConfigError):
                local_governor.check_config({"active_memory_budget_bytes": bad})
        c = local_governor.check_config({"memory_budget_bytes": 20 * GIB})      # an older config: one budget
        self.assertEqual(c["active_memory_budget_bytes"], 20 * GIB)
        c = local_governor.check_config({"active_memory_budget_bytes": 40 * GIB})  # equal: the distinction off
        self.assertEqual(local_governor.available_bytes(c, "active"), 25 * GIB)

    def test_example_states_the_active_budget_and_its_measurement(self):
        ex = json.loads(EXAMPLE.read_text())["governor"]
        self.assertEqual(ex["active_memory_budget_bytes"], 32 * GIB + GIB // 4)
        why = ex["_reasons"]["active_memory_budget_bytes"]
        for fact in ("2026-11-22", "38 GiB", "11.93 GiB", "17.06 GiB"):
            self.assertIn(fact, why)


class ActiveBudgetGateTests(MlxGateHarness):
    """Through the gate, on the 6-bit weights (a full-context turn fits only the idle budget): 413 while the owner
    works, 200 once away, and the owner's return holding new turns at a retryable 529 while the long one finishes."""
    GOVERNOR = {"idle_s": ACTIVE, "pressure": "normal", "active_cap": 2, "weights_bytes": SIX_BIT}

    def test_413_while_active_then_a_running_long_turn_survives_the_owners_return(self):
        code, raw, headers = self.call("/v1/messages", LONG_MSG)
        self.assertEqual(code, 413)
        self.assertEqual({k.lower(): v for k, v in headers.items()}.get("x-should-retry"), "false")
        data = json.loads(raw)
        self.assertTrue(data["error"]["message"].startswith(local_lane.GATE_TOO_LARGE))
        self.assertEqual((data["cortados_gate"]["owner"], data["cortados_gate"]["fit_tokens"]),
                         ("active", SIX_BIT_ACTIVE_FIT))
        self.assertEqual(data["cortados_gate"]["budget_bytes"], 32 * GIB + GIB // 4 - SIX_BIT)
        self.assertEqual(turns(), [])                                    # never sent
        self.governor.set(idle_s=IDLE)                                   # the owner steps away
        FakeVllm.hold = threading.Event()
        results = []
        t = threading.Thread(target=lambda: results.append(self.call("/v1/messages", LONG_MSG)), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.governor.set(idle_s=ACTIVE)                                 # and comes back mid-turn
        report = json.loads(self.call("/api/tags")[1])[local_gate.GATE_KEY]
        self.assertEqual((report["owner"], report["inflight"], report["free_tokens"]), ("active", 1, 0))
        code, raw, headers = self.call("/v1/messages", MSG)              # a short turn: busy now, retryable
        self.assertEqual(code, 529)
        self.assertIn("memory budget", json.loads(raw)["error"]["message"])
        FakeVllm.hold.set()
        t.join(5)
        self.assertEqual(results[0][0], 200)                             # the long turn was never cut
        self.assertTrue(wait_for(lambda: (self.gate.admission.inflight, self.gate.admission.reserved) == (0, 0)))
        self.assertEqual(self.call("/v1/messages", MSG)[0], 200)


class ActiveBudgetGate4BitTests(MlxGateHarness):
    """Through the gate on the default 4-bit weights: a full-context turn is served while the owner works."""
    GOVERNOR = {"idle_s": ACTIVE, "pressure": "normal"}

    def test_a_full_context_turn_is_served_while_the_owner_is_active(self):
        self.assertEqual(self.call("/v1/messages", LONG_MSG)[0], 200)
        self.assertEqual(len(turns()), 1)


class ActiveBudgetRoutingTests(unittest.TestCase):
    """The provider's skip check reads the budget in force: on the 6-bit weights a 100k turn is too_large while the
    owner is active (routed elsewhere before sending) and fits once the owner is idle; on the 4-bit it fits in both."""
    NODE = {"node": "pro-active-budget-test"}

    def test_gate_fit_follows_the_reported_budget(self):
        for weights, idle_s, want in ((SIX_BIT, ACTIVE, "too_large"), (SIX_BIT, IDLE, None),
                                      (15 * GIB, ACTIVE, None), (15 * GIB, IDLE, None)):
            a = local_governor.Admission(FixedGovernor(idle_s=idle_s, weights_bytes=weights))
            with patch.dict(local_lane._gate_reports, {}, clear=False):
                local_lane._note_gate(self.NODE, a.snapshot())
                got = local_lane.gate_fit(self.NODE, 100_000)
            self.assertEqual(got[0] if got else None, want, (weights, idle_s))


if __name__ == "__main__":
    unittest.main()
