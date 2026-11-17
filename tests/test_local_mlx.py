"""The MLX backend for the local lane (docs/LOCAL-RESOURCES.md "MLX backend"): the activity governor's policy and
signals (local_governor.py), the backend config and request adaptation (local_backend.py), the gate's admission count
and its retryable 529 (local_gate.py), and the provider treating a full gate as a busy node (local_lane, routing).
Fakes only: an in-thread fake vllm-mlx on loopback, fake ioreg/sysctl runners; no model, no vendor CLI, no launchctl."""
import json
import os
import plistlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import http.client
import unicodedata
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import local_backend, local_gate, local_governor
from cortados_router.provider import local_lane, local_switch, routing, server
from tests.test_local_gate import TOKEN, token_file
from tests.test_provider import TOKEN as PROVIDER_TOKEN
from tests.test_provider_local import LocalHarness, TAG, write_config
from tests.test_provider_local import FakeOllama
from tests.test_provider_local_index import IndexBase, IndexEndToEndTests

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tables" / "local-backend.example.json"
SERVED = TAG                                    # the fake vllm-mlx serves the provider's configured id

# --------------------------------------------------------------------------------------------------- governor policy


GIB = 2 ** 30


def cfg(**kw):
    return local_governor.check_config(kw)


class PolicyTableTests(unittest.TestCase):
    """The owner-approved table (local_governor docstring), row by row, with the defaults (4-bit model)."""

    def limit(self, idle_s, pressure, **kw):
        return local_governor.policy(cfg(**kw), idle_s, pressure)

    def test_owner_active_pressure_normal_is_two(self):
        got = self.limit(30, "normal")
        self.assertEqual((got["limit"], got["owner"], got["reason"]), (2, "active", "owner active"))

    def test_owner_idle_ten_minutes_pressure_normal_is_the_idle_cap(self):
        self.assertEqual(self.limit(600, "normal")["limit"], 8)
        self.assertEqual(self.limit(599.9, "normal")["limit"], 2)          # the threshold is >= 10 min

    def test_pressure_warn_holds_at_one_whatever_the_owner(self):
        self.assertEqual(self.limit(30, "warn")["limit"], 1)
        got = self.limit(3600, "warn")
        self.assertEqual((got["limit"], got["reason"]), (1, "memory pressure warn"))

    def test_pressure_critical_admits_nothing_new(self):
        self.assertEqual(self.limit(30, "critical")["limit"], 0)
        self.assertEqual(self.limit(3600, "critical")["limit"], 0)

    def test_unreadable_idle_counts_as_active_and_unreadable_pressure_as_warn(self):
        self.assertEqual(self.limit(None, "normal")["limit"], 2)
        got = self.limit(3600, None)
        self.assertEqual((got["limit"], got["pressure"]), (1, "unknown"))
        self.assertEqual(self.limit(3600, None, unknown_pressure="critical")["limit"], 0)

    def test_the_count_is_no_longer_cut_by_a_fixed_per_request_size(self):
        # memory is the byte budget's job now (ByteBudgetTests): the count is the owner/pressure policy alone
        got = self.limit(3600, "normal", memory_budget_bytes=30 * GIB)
        self.assertEqual((got["limit"], got["reason"]), (8, "owner idle"))

    def test_per_model_overrides(self):
        six_bit = {"idle_cap": 6, "weights_bytes": int(22.4 * GIB)}
        c = local_governor.check_config({}, six_bit)
        self.assertEqual(local_governor.policy(c, 3600, "normal")["limit"], 6)
        self.assertEqual(local_governor.policy(c, 30, "normal")["limit"], 2)
        self.assertEqual(c["weights_bytes"], int(22.4 * GIB))

    def test_bad_thresholds_are_refused(self):
        for bad in ({"active_cap": -1}, {"active_cap": True}, {"sample_s": 0}, {"memory_budget_bytes": float("nan")},
                    {"memory_budget_bytes": 40.5}, {"kv_bytes_per_token": 0}, {"context_tokens": 0},
                    {"prefill_margin_bytes": -1}, {"unknown_pressure": "fine"}, {"idle_cap": 0}, {"bogus": 1}):
            with self.assertRaises(local_governor.GovernorConfigError, msg=bad):
                cfg(**bad)

    def test_the_replaced_count_only_keys_are_refused_by_name(self):
        for key in ("per_request_gib", "memory_budget_gib", "weights_gib"):
            with self.assertRaisesRegex(local_governor.GovernorConfigError, "was replaced by"):
                cfg(**{key: 2.0})
            with self.assertRaisesRegex(local_governor.GovernorConfigError, "model entry"):
                local_governor.check_config({}, {key: 2.0})


class SamplingTests(unittest.TestCase):
    def test_signals_are_read_at_most_every_sample_s(self):
        calls, clock = [], [0.0]
        g = local_governor.Governor(cfg(), idle_reader=lambda: calls.append("i") or 30.0,
                                    pressure_reader=lambda: "normal", clock=lambda: clock[0])
        self.assertEqual(g.current()["limit"], 2)
        clock[0] = 4.9
        g.current()
        self.assertEqual(calls, ["i"])
        clock[0] = 5.0
        g.current()
        self.assertEqual(calls, ["i", "i"])


class SignalReaderTests(unittest.TestCase):
    @staticmethod
    def runner(stdout, code=0):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    def test_hid_idle_time_is_nanoseconds(self):
        out = '+-o IOHIDSystem  <class IOHIDSystem>\n    {\n      "HIDIdleTime" = 615000000000\n    }\n'
        self.assertEqual(local_governor.read_idle_s(self.runner(out)), 615.0)

    def test_unreadable_idle_is_none(self):
        self.assertIsNone(local_governor.read_idle_s(self.runner("nothing here")))
        self.assertIsNone(local_governor.read_idle_s(self.runner('"HIDIdleTime" = 5', code=1)))

        def boom(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 2)
        self.assertIsNone(local_governor.read_idle_s(boom))

    def test_pressure_levels(self):
        for out, want in (("1\n", "normal"), ("2\n", "warn"), ("4\n", "critical"), ("3\n", None), ("x", None)):
            self.assertEqual(local_governor.read_pressure(self.runner(out)), want, out)

        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        self.assertIsNone(local_governor.read_pressure(missing))


class FixedGovernor(local_governor.Governor):
    """A governor whose sample the test sets directly (limit by owner/pressure through the real policy)."""

    def __init__(self, idle_s=30.0, pressure="normal", **kw):
        self.state = {"idle": idle_s, "pressure": pressure}
        super().__init__(cfg(**kw), idle_reader=lambda: self.state["idle"],
                         pressure_reader=lambda: self.state["pressure"], clock=lambda: 0.0)

    def set(self, idle_s=None, pressure=None):
        if idle_s is not None:
            self.state["idle"] = idle_s
        if pressure is not None:
            self.state["pressure"] = pressure
        self._sample = None                       # the next current() re-samples


class AdmissionTests(unittest.TestCase):
    """The COUNT bound (owner/pressure), with requests too small for the byte budget to matter."""

    def test_no_governor_is_unlimited(self):
        a = local_governor.Admission(None)
        for _ in range(50):
            self.assertIsNotNone(a.try_acquire(10 ** 15)[0])          # nothing is budgeted without a governor
        snap = a.snapshot()
        self.assertEqual((snap["limit"], snap["reserved_bytes"], snap["free_bytes"]), (None, None, None))

    def test_return_to_active_stops_admitting_beyond_two_and_never_stops_running_requests(self):
        g = FixedGovernor(idle_s=3600)
        a = local_governor.Admission(g)
        held = [a.try_acquire()[0] for _ in range(8)]                 # owner idle: 8 admitted
        self.assertTrue(all(held))
        ticket, snap = a.try_acquire()
        self.assertIsNone(ticket)
        self.assertEqual((snap["limit"], snap["inflight"], snap["refused"]), (8, 8, "busy"))
        g.set(idle_s=5)                                               # the owner is back
        ticket, snap = a.try_acquire()
        self.assertIsNone(ticket)
        self.assertEqual((snap["limit"], snap["inflight"], snap["owner"]), (2, 8, "active"))
        self.assertEqual(a.inflight, 8)                               # nothing running was stopped
        for left in (7, 6, 5, 4, 3, 2):                               # requests finish on their own
            held.pop().release()
            self.assertIsNone(a.try_acquire()[0], left)
        held.pop().release()                                          # 1 running: a second may start
        self.assertIsNotNone(a.try_acquire()[0])
        self.assertIsNone(a.try_acquire()[0])

    def test_warn_then_critical(self):
        g = FixedGovernor(idle_s=3600, pressure="warn")
        a = local_governor.Admission(g)
        first = a.try_acquire()[0]
        self.assertIsNotNone(first)
        self.assertIsNone(a.try_acquire()[0])
        g.set(pressure="critical")
        first.release()
        ticket, snap = a.try_acquire()
        self.assertIsNone(ticket)
        self.assertEqual((snap["limit"], snap["reason"]), (0, "memory pressure critical"))


# A Claude Code turn near the model's whole context, and a benchmark-sized one (~8.5k-token prompt, 512 out).
LONG_IN, LONG_MAX = 120_000, 32_000                         # 152k tokens asked: capped at the 131072-token context
SHORT_IN, SHORT_MAX = 8_000, 512                            # 8,512 tokens
LONG_CHARS = int(LONG_IN * 3.5)                             # the gate tests' long message (420 KB: capped at the context)


class ByteBudgetTests(unittest.TestCase):
    """The BYTE bound on the default (text-only) model, as the gate takes it (#126): each text turn joins the text
    route's set reservation (local_governor.text_set_bytes: the decode batch pads every row to the longest, one replaced
    KV copy, the guard's chunked prefill one sequence at a time, MLX's capped buffer cache), never less than the old
    per-request sum, and turns are admitted while the set fits in budget - weights (defaults: 40 - 15 = 25 GiB)."""

    def setUp(self):
        self.g = FixedGovernor(idle_s=3600)                           # owner idle, pressure normal: count limit 8
        self.a = local_governor.Admission(self.g)
        self.c = self.g.cfg

    def long(self, a=None):
        return (a or self.a).text_load(LONG_IN, LONG_MAX)

    def short(self, a=None):
        return (a or self.a).text_load(SHORT_IN, SHORT_MAX)

    def take(self, load, a=None):
        return (a or self.a).try_acquire(load=load)

    def alone(self, load):
        return local_governor.text_set_bytes(self.c, [load])["total"]

    def test_the_defaults_are_the_measured_model(self):
        c = self.c
        self.assertEqual((c["memory_budget_bytes"], c["weights_bytes"]), (40 * GIB, 15 * GIB))
        self.assertEqual(c["kv_bytes_per_token"], 16 * 2 * 4 * 256 * 2)            # 65,536 bytes per token
        self.assertEqual(131072 * c["kv_bytes_per_token"], 8 * GIB)               # a 128k context holds 8 GiB
        self.assertEqual(local_governor.request_tokens(c, LONG_IN, LONG_MAX), 131072)   # capped at the context
        # the old per-request reservation, now the floor: 8 GiB + state + the 1.5 GiB margin
        self.assertEqual(self.long().legacy, 8 * GIB + int(0.15 * GIB) + int(1.5 * GIB))
        # the computed peak governs a long turn: two KV copies of 128k (+ a cache step), its prefill chunk's scores
        self.assertEqual(self.a.request_bytes(LONG_IN, LONG_MAX), self.alone(self.long()))
        self.assertGreater(self.alone(self.long()), 22 * GIB)
        self.assertLess(self.alone(self.long()), 25 * GIB)                         # but it fits alone
        self.assertEqual(local_governor.request_tokens(c, SHORT_IN, SHORT_MAX), 8512)
        # no max_tokens: the backend's default output is reserved
        self.assertEqual(local_governor.request_tokens(c, 2, None), 2 + 32768)
        self.assertEqual(local_governor.max_seqs(c), 8)

    def test_one_128k_request_fits_the_4bit_budget_and_a_second_waits(self):
        t1 = self.take(self.long())[0]
        self.assertIsNotNone(t1)
        ticket, snap = self.take(self.long())
        self.assertIsNone(ticket)
        self.assertEqual(snap["refused"], "busy")                    # it fits alone: retryable, not too_large
        self.assertTrue(snap["reason"].startswith("memory budget: 1.1 GiB free, request needs"), snap)
        self.assertEqual((snap["inflight"], snap["limit"]), (1, 8))  # the count had room: the bytes refused it
        self.assertEqual(snap["reserved_bytes"], self.alone(self.long()))
        self.assertEqual(snap["free_bytes"], 25 * GIB - self.alone(self.long()))
        t1.release()
        self.assertIsNotNone(self.take(self.long())[0])              # the long one ended: room for another

    def test_eight_short_requests_fit_while_idle_and_reserve_exactly_what_they_did(self):
        loads = [self.short() for _ in range(8)]
        held = [self.take(x)[0] for x in loads]
        self.assertTrue(all(held))
        # the old per-request sum is at least the computed set peak here, so it stands byte for byte
        self.assertEqual(self.a.reserved, 8 * local_governor.tokens_bytes(self.c, 8512))
        self.assertLessEqual(self.a.reserved, 25 * GIB)
        ticket, snap = self.take(self.short())
        self.assertIsNone(ticket)
        self.assertEqual(snap["reason"], "owner idle")               # the ninth is refused by the count, not bytes
        self.assertEqual(snap["free_tokens"], 0)                     # full: no room reported

    def test_a_short_request_waits_beside_a_long_one(self):
        # the decode batch pads every row to the longest: a short turn beside a 128k one holds 128k of KV
        long = self.take(self.long())[0]
        ticket, snap = self.take(self.short())
        self.assertIsNone(ticket)
        self.assertIn("memory budget", snap["reason"])
        self.assertEqual(snap["refused"], "busy")
        long.release()
        shorts = [self.take(self.short())[0] for _ in range(4)]
        self.assertTrue(all(shorts))                                 # the long one gone: short ones run together
        self.assertIsNone(self.take(self.long())[0])                 # and a long one waits beside them
        self.assertEqual(self.a.inflight, 4)

    def test_the_owner_and_pressure_caps_still_apply_when_bytes_fit(self):
        self.g.set(idle_s=5)                                         # owner active: 2, though 8 short would fit
        held = [self.take(self.short())[0] for _ in range(2)]
        self.assertTrue(all(held))
        self.assertEqual(self.take(self.short())[1]["reason"], "owner active")
        for t in held:
            t.release()
        self.g.set(idle_s=3600, pressure="warn")                     # the safety net: 1 at once whatever the bytes
        self.assertIsNotNone(self.take(self.short())[0])
        self.assertEqual(self.take(self.short())[1]["reason"], "memory pressure warn")
        self.g.set(pressure="critical")
        self.assertIsNone(self.take(self.short())[0])

    def test_the_six_bit_copy_refuses_a_full_context_turn_non_retryably(self):
        six = local_governor.Admission(FixedGovernor(idle_s=3600, idle_cap=6, weights_bytes=int(22.4 * GIB)))
        ticket, snap = self.take(self.long(six), six)                # 17.6 GiB beside the weights: never fits
        self.assertEqual((ticket, snap["refused"]), (None, "too_large"))
        self.assertEqual(snap["fit_tokens"], local_governor.text_room(six.governor.cfg, [],
                                                                      int(40 * GIB - int(22.4 * GIB))))
        self.assertLess(snap["fit_tokens"], 131072)
        first = self.take(six.text_load(40_000, 32_000), six)[0]     # a 72k-token turn fits alone
        self.assertIsNotNone(first)

    def test_a_request_too_large_to_ever_fit_is_refused_non_retryably(self):
        small = local_governor.Admission(FixedGovernor(idle_s=3600, memory_budget_bytes=20 * GIB))   # 5 GiB room
        ticket, snap = self.take(self.long(small), small)
        self.assertIsNone(ticket)
        self.assertEqual(snap["refused"], "too_large")               # with nothing in flight: never
        self.assertIn("more than the 5.0 GiB budget beside the weights", snap["reason"])
        self.assertEqual(small.inflight, 0)
        self.assertEqual(snap["fit_tokens"], local_governor.text_room(small.governor.cfg, [], 5 * GIB))
        self.assertIsNotNone(self.take(self.short(small), small)[0])

    def test_release_gives_back_once(self):
        t = self.take(self.long())[0]
        t.release()
        t.release()                                                  # a second path calling it: no double free
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))
        other = self.take(self.short())[0]
        t.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (1, self.alone(self.short())))
        other.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))

    def test_the_set_keeps_its_peak_until_it_drains_then_starts_afresh(self):
        long = self.take(self.long())[0]
        long.release()                                               # drained: nothing of the long one is kept
        self.assertEqual((self.a._high_water, self.a._peak, self.a.reserved), (0, (0, 0), 0))
        held = [self.take(self.short())[0] for _ in range(3)]
        reserved = self.a.reserved
        held[0].release()     # recomputed over the two left at the set's peak (the backend's batch shrinks lazily)
        self.assertEqual(self.a.reserved, local_governor.text_set_bytes(
            self.c, [h.load for h in held[1:]], self.a._high_water, self.a._peak)["total"])
        self.assertLessEqual(self.a.reserved, reserved)
        for h in held[1:]:
            h.release()
        self.assertEqual(self.a.reserved, 0)

    def test_the_count_slot_and_the_bytes_are_taken_together(self):
        held = self.take(self.long())[0]
        ticket, snap = self.take(self.long())                        # busy on bytes: retryable
        self.assertEqual((ticket, snap["refused"]), (None, "busy"))
        self.assertEqual((self.a.inflight, self.a.reserved), (1, self.alone(self.long())))   # nothing taken
        ticket, snap = self.a.try_acquire(26 * GIB)                  # never fits: not retryable
        self.assertEqual((ticket, snap["refused"], snap["request_bytes"]), (None, "too_large", 26 * GIB))
        self.assertEqual((self.a.inflight, self.a.reserved), (1, self.alone(self.long())))
        held.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))

    def test_a_full_count_refuses_with_the_owner_reason_and_never_fits_wins_over_it(self):
        active = local_governor.Admission(FixedGovernor(idle_s=5))          # owner active: 2
        slots = [self.take(self.short(active), active)[0] for _ in range(2)]
        ticket, snap = self.take(self.short(active), active)
        self.assertEqual((ticket, snap["refused"], snap["reason"]), (None, "busy", "owner active"))
        ticket, snap = active.try_acquire(26 * GIB)                         # full count, but it could never fit
        self.assertEqual((ticket, snap["refused"]), (None, "too_large"))
        self.assertEqual((active.inflight, active.reserved), (2, 2 * local_governor.tokens_bytes(self.c, 8512)))
        for t in slots:
            t.release()
        self.assertEqual((active.inflight, active.reserved), (0, 0))
        free = local_governor.Admission(None)                               # no governor: unlimited, no bytes
        self.assertIsNone(free.text_load(LONG_IN, LONG_MAX))
        t, _ = free.try_acquire(10 ** 15)
        self.assertEqual((free.inflight, free.reserved), (1, 0))
        t.release()
        self.assertEqual(free.inflight, 0)

    def test_concurrent_acquires_never_overshoot_either_bound_and_release_exactly_once(self):
        # 40 threads at once, no counting anywhere: the bytes admit one 128k turn, the count (owner active) two short
        for admission, make, want in ((self.a, self.long, 1),
                                      (local_governor.Admission(FixedGovernor(idle_s=5)), self.short, 2)):
            go, got = threading.Barrier(40), []

            def one():
                go.wait(5)
                got.append(self.take(make(admission), admission)[0])
            threads = [threading.Thread(target=one) for _ in range(40)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
            admitted = [t for t in got if t is not None]
            self.assertEqual(len(admitted), want)
            self.assertEqual(admission.inflight, want)
            self.assertEqual(admission.reserved,
                             local_governor.text_set_bytes(self.c, [t.load for t in admitted])["total"])
            for t in admitted + admitted:                                   # every ticket released twice
                t.release()
            self.assertEqual((admission.inflight, admission.reserved), (0, 0))

    def test_the_report_carries_bytes_and_token_room(self):
        t = self.take(self.long())[0]
        snap = self.a.snapshot()
        self.assertEqual(snap["reserved_bytes"], self.alone(self.long()))
        self.assertEqual(snap["free_bytes"], 25 * GIB - self.alone(self.long()))
        self.assertEqual(snap["fit_tokens"], 131072)                  # anything up to the context fits alone
        self.assertEqual(snap["free_tokens"], 0)                      # nothing fits beside a 128k turn (padding)
        t.release()
        self.take(self.short())
        room = self.a.snapshot()["free_tokens"]
        self.assertGreater(room, 8512)
        self.assertLess(room, 131072)
        self.assertEqual(room, local_governor.text_room(self.c, list(self.a._loads), 25 * GIB))


# ----------------------------------------------------------------------------------------------------- backend config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.example = json.loads(EXAMPLE.read_text())

    def write(self, data):
        p = Path(self.dir) / "local-backend.json"
        p.write_text(json.dumps(data))
        return str(p)

    def test_example_config_loads_and_every_threshold_has_its_reason(self):
        c = local_backend.load(str(EXAMPLE))
        self.assertEqual((c["backend"], c["served_model"], c["upstream"]),
                         ("vllm-mlx", "Qwen27b_heretic:latest", "http://127.0.0.1:8000"))
        self.assertEqual(c["governor"]["idle_cap"], 8)
        reasons = self.example["governor"]["_reasons"]
        self.assertEqual(set(reasons), set(local_governor.DEFAULTS))
        self.assertTrue(all(isinstance(v, str) and len(v) > 20 for v in reasons.values()))
        # the example states the code's defaults, so the reasons describe the numbers that run
        for k, v in self.example["governor"].items():
            if not k.startswith("_"):
                self.assertEqual(v, local_governor.DEFAULTS[k], k)

    def test_no_file_is_ollama_with_no_governor_but_a_named_missing_file_is_an_error(self):
        with patch.dict(os.environ, {"HOME": self.dir}), patch.dict(os.environ, {local_backend.CONFIG_ENV: ""}):
            os.environ.pop(local_backend.CONFIG_ENV)
            c = local_backend.load()
        self.assertEqual((c["backend"], c["governor"], c["upstream"]), ("ollama", None, None))
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.load(str(Path(self.dir) / "missing.json"))

    def test_round5_server_default_template_kwargs_are_held_to_the_request_allowlist(self):
        # The round 5 review's repro: the server default reaches the same template call as a request's kwargs, so a
        # `chat_template` there rendered ~100k tokens for a minimal request the gate reserved ~1.1k for.
        for bad in ({"chat_template": "{{ 'a!' * 50000 }}"}, {"enable_thinking": False, "tools": []},
                    {"enable_thinking": "false"}, ["enable_thinking"], "x"):
            with self.subTest(bad):
                self.example["vllm_mlx"]["serve"]["default_chat_template_kwargs"] = bad
                with self.assertRaises(local_backend.BackendConfigError) as cm:
                    local_backend.load(self.write(self.example))
                self.assertIn("vllm_mlx.serve.default_chat_template_kwargs", str(cm.exception))
        # serve_argv re-checks a cfg that did not come through check(), and never emits the flag for it
        c = local_backend.load(str(EXAMPLE))
        argv = local_backend.serve_argv(c)
        self.assertEqual(argv[argv.index("--default-chat-template-kwargs") + 1], '{"enable_thinking": false}')
        c["serve"] = {**c["serve"], "default_chat_template_kwargs": {"chat_template": "{{ 'a!' * 50000 }}"}}
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.serve_argv(c)
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.launch_agent_plist(c, self.dir)
        for none in ({}, None):                                               # no default: no flag
            self.example["vllm_mlx"]["serve"]["default_chat_template_kwargs"] = none
            argv = local_backend.serve_argv(local_backend.load(self.write(self.example)))
            self.assertNotIn("--default-chat-template-kwargs", argv)

    def test_model_path_is_config_and_a_six_bit_copy_is_a_config_change(self):
        self.example["vllm_mlx"]["model"] = "qwen-27b-6bit"
        self.example["vllm_mlx"]["models"]["qwen-27b-6bit"]["path"] = "/models/q6"
        c = local_backend.load(self.write(self.example))
        argv = local_backend.serve_argv(c)
        self.assertEqual(argv[9:11], ["serve", "/models/q6"])                # after the guard's own arguments (#126: text flags)
        self.assertEqual(argv[argv.index("--max-num-seqs") + 1], "6")      # the 6-bit entry's own idle cap
        self.assertEqual(c["governor"]["weights_bytes"], int(22.4 * GIB))      # measured, no longer a placeholder
        self.assertEqual(c["governor"]["default_max_tokens"], 32768)          # vllm_mlx.serve.max_tokens

    def test_serve_argv_and_plist(self):
        c = local_backend.load(str(EXAMPLE))
        argv = local_backend.serve_argv(c)
        self.assertTrue(argv[0].endswith("/venvs/mlx-bench/bin/python"))
        self.assertTrue(argv[1].endswith("/venvs/mlx-bench/bin/cortados-vllm-mlx-guard.py"))    # the prompt guard
        for flag, value in (("--served-model-name", "Qwen27b_heretic:latest"), ("--host", "127.0.0.1"),
                            ("--port", "8000"), ("--max-num-seqs", "8"), ("--reasoning-parser", "qwen3")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertIn("--continuous-batching", argv)
        self.assertEqual(json.loads(argv[argv.index("--default-chat-template-kwargs") + 1]), {"enable_thinking": False})
        pl = plistlib.loads(local_backend.launch_agent_plist(c, "/tmp/logs"))
        self.assertEqual(pl["Label"], "com.cortados.vllm-mlx")
        self.assertEqual(pl["ProgramArguments"], argv)
        self.assertEqual(pl["StandardOutPath"], "/tmp/logs/vllm-mlx.log")

    def test_bad_configs_are_refused(self):
        def bad(mutate):
            d = json.loads(json.dumps(self.example))
            mutate(d)
            with self.assertRaises(local_backend.BackendConfigError):
                local_backend.load(self.write(d))
        bad(lambda d: d.update(backend="llama.cpp"))
        bad(lambda d: d["vllm_mlx"].update(model="nope"))
        bad(lambda d: d["vllm_mlx"]["serve"].update(host="0.0.0.0"))
        bad(lambda d: d["vllm_mlx"]["serve"].update(bogus=1))
        bad(lambda d: d.update(upstream="http://10.0.0.5:8000"))
        bad(lambda d: d["governor"].update(active_cap="two"))
        bad(lambda d: d["vllm_mlx"]["models"]["qwen-27b-4bit"].pop("path"))

    def test_gate_reads_the_backend_config(self):
        import contextlib
        import io
        tf = token_file(self.dir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = local_gate.main(["--listen", "127.0.0.1", "--token-file", tf, "--backend-config", str(EXAMPLE),
                                  "--print-config"])
        got = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual((got["backend"], got["upstream"], got["served_model"], got["read_timeout_s"]),
                         ("vllm-mlx", "http://127.0.0.1:8000", "Qwen27b_heretic:latest", 900.0))
        self.assertEqual(got["governor"]["active_cap"], 2)
        self.assertNotIn(TOKEN, out.getvalue())
        bad = self.write({**self.example, "backend": "nope"})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(local_gate.main(["--listen", "127.0.0.1", "--token-file", tf, "--backend-config", bad,
                                              "--print-config"]), 2)
        self.assertIn("backend must be one of", err.getvalue())

    def test_extra_args_may_only_add_allowlisted_flags(self):
        def with_extra(extra):
            d = json.loads(json.dumps(self.example))
            d["vllm_mlx"]["extra_args"] = extra
            return local_backend.load(self.write(d))
        # the review's case: overriding the loopback host and the backend's own sequence bound
        for extra in (["--host", "0.0.0.0", "--max-num-seqs", "64"], ["--host=0.0.0.0"], ["--max-num-seqs=64"],
                      ["--port", "9000"], ["--ho", "0.0.0.0"],                 # argparse would expand --ho to --host
                      ["--max-tokens", "999999"], ["--max-request-tokens=999999"], ["--timeout", "0"],
                      ["--served-model-name", "other"], ["--continuous-batching"], ["--api-key", "x"],
                      ["--mcp-config", "/tmp/m.json"], ["--kv-cache-quantization"], ["0.0.0.0"],
                      ["--default-temperature"], ["--default-temperature", "--host"], ["--default-temperature="],
                      ["--disable-prefix-cache=yes"], ["--default-top-k", "-h"]):
            with self.assertRaises(local_backend.BackendConfigError, msg=extra) as cm:
                with_extra(extra)
            self.assertIn("vllm_mlx.extra_args", str(cm.exception))
        with self.assertRaises(local_backend.BackendConfigError) as cm:
            with_extra(["--default-temperature", "0.6", "--host", "0.0.0.0"])
        self.assertIn("'--host' is not allowed", str(cm.exception))
        ok = with_extra(["--default-temperature", "0.6", "--default-top-p=0.95", "--default-presence-penalty", "-0.5",
                         "--disable-prefix-cache", "--chunked-prefill-tokens", "2048"])
        argv = local_backend.serve_argv(ok)
        self.assertEqual(argv[-8:], ["--default-temperature", "0.6", "--default-top-p=0.95",
                                     "--default-presence-penalty", "-0.5", "--disable-prefix-cache",
                                     "--chunked-prefill-tokens", "2048"])
        self.assertEqual([a for a in argv if a.startswith("--host")], ["--host"])     # set once, by the generator
        # a config dict built without check() is refused by the generator too
        with self.assertRaises(local_backend.BackendConfigError):
            local_backend.serve_argv({**ok, "extra_args": ["--host", "0.0.0.0"]})

    def test_install_script_is_valid_bash_and_never_run_here(self):
        script = ROOT / "scripts" / "install_mlx_backend.sh"
        self.assertTrue(os.access(script, os.X_OK))
        subprocess.run(["bash", "-n", str(script)], check=True)            # syntax only: the script is not run


# ------------------------------------------------------------------------------------------------ request adaptation

MLX = {"backend": "vllm-mlx", "served_model": SERVED}


def adapt(body):
    return json.loads(local_backend.adapt_messages_body(MLX, json.dumps(body).encode()))


class AdaptTests(unittest.TestCase):
    BODY = {"model": TAG, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}

    def test_thinking_maps_to_enable_thinking(self):
        self.assertIs(adapt({**self.BODY, "thinking": {"type": "disabled"}})["chat_template_kwargs"]["enable_thinking"],
                      False)
        got = adapt({**self.BODY, "max_tokens": 8192, "thinking": {"type": "enabled", "budget_tokens": 4096}})
        self.assertIs(got["chat_template_kwargs"]["enable_thinking"], True)
        self.assertNotIn("chat_template_kwargs", adapt(self.BODY))         # absent: the server's default
        own = adapt({**self.BODY, "thinking": {"type": "disabled"}, "chat_template_kwargs": {"enable_thinking": True}})
        self.assertIs(own["chat_template_kwargs"]["enable_thinking"], True)  # the client's own setting wins

    def test_the_lane_think_variant_reaches_vllm_mlx_as_thinking_on(self):
        node_body = local_lane._node_body({**self.BODY, "max_tokens": 8192},
                                          {"id": TAG, "variant_thinking": "on", "thinking": "off-unless-asked"})
        self.assertIs(adapt(node_body)["chat_template_kwargs"]["enable_thinking"], True)
        off = local_lane._node_body(self.BODY, {"id": TAG, "thinking": "off-unless-asked"})
        self.assertIs(adapt(off)["chat_template_kwargs"]["enable_thinking"], False)

    def test_model_must_be_the_served_one(self):
        latest = {"backend": "vllm-mlx", "served_model": "Qwen27b_heretic:latest"}
        for asked in ("Qwen27b_heretic", "Qwen27b_heretic:latest"):                 # name == name:latest
            got = json.loads(local_backend.adapt_messages_body(latest, json.dumps({**self.BODY, "model": asked}).encode()))
            self.assertEqual(got["model"], "Qwen27b_heretic:latest")
        with self.assertRaises(local_backend.Refusal) as cm:
            adapt({**self.BODY, "model": "phi:latest"})
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(local_backend.Refusal):
            local_backend.adapt_messages_body(MLX, b"not json")

    def test_round4_template_kwargs_are_allowlisted(self):
        # The review's repro: a replacement chat_template renders a 100k-token prompt from a 155-byte body.
        evil = {**self.BODY, "chat_template_kwargs": {"chat_template": "{{ 'a!' * 50000 }}"}}
        self.assertLess(len(json.dumps(evil)), 200)
        for kwargs in ({"chat_template": "{{ 'a!' * 50000 }}"}, {"tools": [{"name": "x" * 99}]},
                       {"enable_thinking": True, "add_generation_prompt": False}, {"enable_thinking": "yes"},
                       {"enable_thinking": 1}, ["enable_thinking"], "x"):
            with self.subTest(kwargs):
                with self.assertRaises(local_backend.Refusal) as cm:
                    adapt({**self.BODY, "chat_template_kwargs": kwargs})
                self.assertEqual((cm.exception.status, cm.exception.kind), (400, "invalid_request_error"))
        self.assertIn("chat_template is not allowed", str(self._refusal(evil)))
        self.assertEqual(adapt({**self.BODY, "chat_template_kwargs": {"enable_thinking": False}})
                         ["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(adapt({**self.BODY, "chat_template_kwargs": {}})["chat_template_kwargs"], {})

    def _refusal(self, body):
        try:
            adapt(body)
        except local_backend.Refusal as exc:
            return exc
        self.fail("not refused")

    def test_round4_deep_json_is_refused_not_raised(self):
        deep = b'{"model": "' + TAG.encode() + b'", "max_tokens": 1, "messages": ' + b"[" * 100_000 \
            + b"]" * 100_000 + b"}"
        with self.assertRaises(local_backend.Refusal) as cm:
            local_backend.adapt_messages_body(MLX, deep)
        self.assertEqual((cm.exception.status, str(cm.exception)), (400, local_backend.TOO_DEEP))

    def test_ollama_bodies_pass_unchanged(self):
        raw = b'{"model": "x", "thinking": {"type": "disabled"}}'
        self.assertIs(local_backend.adapt_messages_body({"backend": "ollama"}, raw), raw)

    def test_models_to_tags_and_error_shape(self):
        self.assertEqual(local_backend.models_to_tags(b'{"object":"list","data":[{"id":"m:latest"}]}'),
                         {"models": [{"name": "m:latest", "model": "m:latest"}]})
        self.assertIsNone(local_backend.models_to_tags(b'{"models": []}'))
        wrapped = json.loads(local_backend.anthropic_error(400, b'{"detail":"max_tokens exceeds server limit"}',
                                                           "vllm-mlx"))
        self.assertEqual(wrapped["error"], {"type": "invalid_request_error",
                                            "message": "vllm-mlx: max_tokens exceeds server limit"})
        already = b'{"type":"error","error":{"type":"api_error","message":"x"}}'
        self.assertEqual(local_backend.anthropic_error(500, already, "vllm-mlx"), already)


# ---------------------------------------------------------------------------------------- gate in front of vllm-mlx


class FakeVllm(BaseHTTPRequestHandler):
    """vllm-mlx's routes as the gate uses them: GET /v1/models, POST /v1/messages (Anthropic). Anything else 404."""
    seen: list = []
    hold = None                                  # set: POST /v1/messages waits on it (a long generation)
    entered: threading.Semaphore = threading.Semaphore(0)
    error = None                                 # (status, body) to answer POSTs with
    tickets: list = []                           # the ticket header of each POST (#126 round 3)
    retire_polls: list = []                      # the ids of each retirement poll

    def log_message(self, *a):
        pass

    def _json(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeVllm.seen.append(("GET", self.path, None))
        if self.path != "/v1/models":
            return self._json(404, {"detail": "Not Found"})
        self._json(200, {"object": "list", "data": [{"id": SERVED, "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == local_gate.RETIRE_PATH:     # the guard's retirement answer (#126 round 3): all ended at once
            FakeVllm.retire_polls.append(body["tickets"])
            return self._json(200, {"retired": body["tickets"], "boot": "fake"})
        FakeVllm.seen.append(("POST", self.path, body))
        FakeVllm.tickets.append(self.headers.get(local_gate.TICKET_HEADER))
        if self.path != "/v1/messages":
            return self._json(404, {"detail": "Not Found"})
        FakeVllm.entered.release()
        if FakeVllm.hold is not None:
            FakeVllm.hold.wait(10)
        if FakeVllm.error:
            return self._json(*FakeVllm.error)
        if body.get("model") != SERVED:
            return self._json(404, {"detail": f"The model `{body.get('model')}` does not exist."})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            events = [{"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                             "model": SERVED, "content": [], "usage": {}}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                      {"type": "content_block_stop", "index": 0},
                      {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
                      {"type": "message_stop"}]
            for ev in events:
                self.wfile.write(f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
            self.close_connection = True
            return
        self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": SERVED,
                         "content": [{"type": "text", "text": "hi from mlx"}], "stop_reason": "end_turn",
                         "usage": {"input_tokens": 3, "output_tokens": 4}})


def reset_fake():
    FakeVllm.seen = []
    FakeVllm.hold = None
    FakeVllm.entered = threading.Semaphore(0)
    FakeVllm.error = None
    FakeVllm.tickets = []
    FakeVllm.retire_polls = []


def turns():
    """The model turns that reached the fake backend (POST /v1/messages)."""
    return [s for s in FakeVllm.seen if s[:2] == ("POST", "/v1/messages")]


class MlxGateHarness(unittest.TestCase):
    GOVERNOR = None                                  # a FixedGovernor kwargs dict, or None (no governor)

    def setUp(self):
        reset_fake()
        allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
        allowed.start()
        self.addCleanup(allowed.stop)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeVllm)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.governor = FixedGovernor(**self.GOVERNOR) if self.GOVERNOR is not None else None
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.upstream.server_port}", read_timeout=5,
                                          backend={**MLX, "governor": None},
                                          admission=local_governor.Admission(self.governor))
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def tearDown(self):
        if FakeVllm.hold is not None:
            FakeVllm.hold.set()
        for s in (self.gate, self.upstream):
            s.shutdown()
            s.server_close()

    def call(self, path, body=None, method=None, token=TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)


MSG = {"model": TAG, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


class MlxGateRouteTests(MlxGateHarness):
    def test_tags_come_from_v1_models_in_ollama_shape_with_the_capacity_report(self):
        code, raw, _ = self.call("/api/tags")
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["models"], [{"name": SERVED, "model": SERVED}])
        self.assertEqual(data["cortados_gate"], {"backend": "vllm-mlx", "limit": None, "inflight": 0, "retiring": 0,
                                                 "draining": False, "owner": None,
                                                 "pressure": None, "reason": "no governor", "reserved_bytes": None,
                                                 "free_bytes": None, "fit_tokens": None, "free_tokens": None,
                                                 "image_inflight": None, "image_limit": None, "vision": None})
        self.assertEqual(FakeVllm.seen, [("GET", "/v1/models", None)])

    def test_messages_are_relayed_with_thinking_mapped(self):
        code, raw, _ = self.call("/v1/messages", {**MSG, "thinking": {"type": "disabled"}})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["content"][0]["text"], "hi from mlx")
        (_, path, body), = FakeVllm.seen
        self.assertEqual((path, body["chat_template_kwargs"]), ("/v1/messages", {"enable_thinking": False}))

    def test_streaming_is_relayed(self):
        code, raw, headers = self.call("/v1/messages", {**MSG, "stream": True})
        self.assertEqual(code, 200)
        self.assertIn("text/event-stream", headers.get("content-type", headers.get("Content-Type", "")))
        self.assertIn('"text_delta"', raw)
        self.assertTrue(raw.rstrip().endswith('{"type": "message_stop"}'))

    def test_only_the_backend_routes_are_used_and_others_stay_404(self):
        for method, path in (("GET", "/v1/models"), ("POST", "/v1/chat/completions"), ("POST", "/api/generate"),
                             ("GET", "/health"), ("POST", "/v1/requests/x/cancel"),
                             ("POST", "/v1/messages/count_tokens")):
            code, _, _ = self.call(path, {} if method == "POST" else None, method=method)
            self.assertEqual(code, 404, path)
        self.assertEqual(FakeVllm.seen, [])

    def test_another_model_is_refused_at_the_gate(self):
        code, raw, _ = self.call("/v1/messages", {**MSG, "model": "phi:latest"})
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(raw)["error"]["type"], "not_found_error")
        self.assertEqual(FakeVllm.seen, [])

    def test_backend_errors_reach_the_client_in_anthropic_shape(self):
        FakeVllm.error = (400, {"detail": "max_tokens exceeds server limit (65536)"})
        code, raw, _ = self.call("/v1/messages", MSG)
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(raw)["error"]["message"], "vllm-mlx: max_tokens exceeds server limit (65536)")

    def test_a_bad_token_still_reaches_nothing(self):
        code, _, _ = self.call("/v1/messages", MSG, token="b" * 40)
        self.assertEqual(code, 401)
        self.assertEqual(FakeVllm.seen, [])

    def test_switch_status_names_the_backend_and_its_capacity(self):
        with patch.object(local_gate.local_resources, "local_switch_status",
                          return_value={"switch": "on", "allowed": True, "power": "ac"}):
            code, raw, _ = self.call(local_gate.SWITCH_PATH)
        data = json.loads(raw)
        self.assertEqual((code, data["backend"], data["ollama"], data["models_loaded"]),
                         (200, "vllm-mlx", "up", [SERVED]))
        cleaned = local_switch._clean(data)
        self.assertEqual(cleaned["backend"], "vllm-mlx")
        self.assertEqual(cleaned["capacity"]["inflight"], 0)


class MlxGateAdmissionTests(MlxGateHarness):
    GOVERNOR = {"idle_s": 30.0, "pressure": "normal", "active_cap": 1}       # owner active, cap 1 for a short test

    def start_held(self, n=1):
        """n turns that reach the backend and stay there until FakeVllm.hold is set."""
        FakeVllm.hold = threading.Event()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.call("/v1/messages", MSG)), daemon=True)
                   for _ in range(n)]
        for t in threads:
            t.start()
        for _ in range(n):
            self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        return threads, results

    def test_over_the_limit_is_a_retryable_529_with_retry_after_and_nothing_reaches_the_backend(self):
        threads, results = self.start_held()
        code, raw, headers = self.call("/v1/messages", MSG)
        self.assertEqual(code, 529)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["retry-after"], "5")
        data = json.loads(raw)
        self.assertEqual(data["error"]["type"], "overloaded_error")
        self.assertTrue(data["error"]["message"].startswith(local_gate.BUSY_MESSAGE))
        self.assertEqual((data["cortados_gate"]["limit"], data["cortados_gate"]["inflight"]), (1, 1))
        self.assertEqual(len(turns()), 1)
        # the probe is never counted against the limit and reports the state
        code, raw, _ = self.call("/api/tags")
        self.assertEqual((code, json.loads(raw)["cortados_gate"]["inflight"]), (200, 1))
        FakeVllm.hold.set()
        for t in threads:
            t.join(5)
        self.assertEqual(results[0][0], 200)
        # the slot was given back once the guard confirmed the turn left vllm-mlx (the fake confirms at once, #126)
        self.assertTrue(wait_for(lambda: self.gate.admission.inflight == 0))
        self.assertEqual(self.call("/v1/messages", MSG)[0], 200)

    def test_critical_pressure_admits_nothing(self):
        self.governor.set(pressure="critical")
        code, raw, _ = self.call("/v1/messages", MSG)
        self.assertEqual(code, 529)
        self.assertEqual(json.loads(raw)["cortados_gate"]["reason"], "memory pressure critical")
        self.assertEqual(FakeVllm.seen, [])

    def test_a_failed_relay_frees_its_slot(self):
        FakeVllm.error = (500, {"detail": "boom"})
        self.assertEqual(self.call("/v1/messages", MSG)[0], 500)
        self.assertTrue(wait_for(lambda: self.gate.admission.inflight == 0))


# ------------------------------------------------------------------------------------- the provider and a busy gate


class ProviderBusyGateTests(LocalHarness):
    """The provider's lane through a governed MLX gate: a full gate is a 529 with Retry-After for the client (the
    server's BUSY_STATUS shape), and the report it carried marks the node busy for routing."""

    def setUp(self):
        super().setUp()
        reset_fake()
        allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
        allowed.start()
        self.addCleanup(allowed.stop)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeVllm)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.governor = FixedGovernor(idle_s=30.0, active_cap=1)
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.upstream.server_port}", read_timeout=5,
                                          backend={**MLX, "governor": None},
                                          admission=local_governor.Admission(self.governor))
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.tok_dir = tempfile.mkdtemp()
        os.environ[local_lane.CONFIG_ENV] = write_config(self.scratch, f"http://127.0.0.1:{self.gate.server_port}",
                                                         token_file=token_file(self.tok_dir))
        local_lane._gate_reports.clear()

    def tearDown(self):
        if FakeVllm.hold is not None:
            FakeVllm.hold.set()
        for s in (self.gate, self.upstream):
            s.shutdown()
            s.server_close()
        local_lane._gate_reports.clear()
        super().tearDown()

    BODY = {"model": "sub/qwen-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}

    def raw_post(self, body):
        req = urllib.request.Request(self.url + "/v1/messages", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json",
                                              "authorization": "Bearer " + PROVIDER_TOKEN, **self.HEADERS})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), dict(e.headers)

    def test_a_pinned_turn_through_the_mlx_gate_is_served(self):
        code, got, _ = self.raw_post(self.BODY)
        self.assertEqual((code, got["content"][0]["text"], got["model"]), (200, "hi from mlx", "sub/qwen-27b"))

    def test_a_full_gate_is_a_529_with_retry_after_and_marks_the_node_busy(self):
        self.gate.admission.inflight = 1                   # another provider's turn holds the only slot
        code, got, headers = self.raw_post(self.BODY)
        self.assertEqual(code, 529)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["retry-after"], "5")
        self.assertEqual(got["error"]["type"], "overloaded_error")
        self.assertIn("busy", got["error"]["message"])
        self.assertIn("1 of 1 running at the gate: owner active", got["error"]["message"])
        self.assertEqual(turns(), [])
        node = local_lane.nodes()[0]
        self.assertEqual(local_lane.gate_busy(node), "1 of 1 running at the gate: owner active")
        self.assertEqual(self.reg.sessions, {})             # the failed opener left no conversation behind
        self.gate.admission.inflight = 0                    # the slot frees: the retry is served
        self.assertEqual(self.raw_post(self.BODY)[0], 200)

    def test_the_refusal_fallback_path_treats_busy_as_unavailable(self):
        self.gate.admission.inflight = 1
        node = local_lane.nodes()[0]
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.complete({"model": "x", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
                                node, node["models"][0])
        self.assertIn("busy", str(cm.exception))

    def test_a_gate_out_of_memory_room_is_a_529_naming_the_memory_budget(self):
        self.gate.admission.reserved = 25 * GIB                # in-flight turns hold the whole budget, count has room
        code, got, headers = self.raw_post(self.BODY)
        self.assertEqual(code, 529)
        self.assertIn("is busy for sub/qwen-27b (memory budget: 0.0 GiB free", got["error"]["message"])
        self.assertEqual(turns(), [])
        self.gate.admission.reserved = 0

    def test_node_why_reads_the_gate_report_from_the_probe(self):
        node = local_lane.nodes()[0]
        entry = node["models"][0]
        self.assertEqual(routing._node_why(node, entry, self.BODY, lambda m: False, []), (None, False))
        self.gate.admission.inflight = 1
        why, _ = routing._node_why(node, entry, self.BODY, lambda m: False, [])
        self.assertEqual(why, "node busy (1 of 1 running at the gate: owner active)")


class GateReportTests(unittest.TestCase):
    NODE = {"node": "pro-test"}

    def tearDown(self):
        local_lane._gate_reports.pop("pro-test", None)

    def test_reports_are_validated_and_expire(self):
        local_lane._note_gate(self.NODE, {"limit": 2, "inflight": 2, "reason": "owner active", "owner": "active"})
        self.assertEqual(local_lane.gate_busy(self.NODE), "2 of 2 running at the gate: owner active")
        local_lane._note_gate(self.NODE, {"limit": 8, "inflight": 2, "reason": "owner idle"})
        self.assertIsNone(local_lane.gate_busy(self.NODE))
        local_lane._note_gate(self.NODE, {"limit": None, "inflight": 50})              # no governor: never full
        self.assertIsNone(local_lane.gate_busy(self.NODE))
        local_lane._note_gate(self.NODE, {"limit": 1, "inflight": 1})
        with patch.object(local_lane.time, "monotonic", return_value=time.monotonic() + 60):
            self.assertIsNone(local_lane.gate_busy(self.NODE))                          # stale: says nothing now
        local_lane._gate_reports.pop("pro-test")
        for junk in ("full", {"limit": "2", "inflight": 2}, {"limit": 2, "inflight": True}, {"limit": -1, "inflight": 0}):
            local_lane._note_gate(self.NODE, junk)
            self.assertIsNone(local_lane.gate_report(self.NODE), junk)


class IndexRouteBusyGateTests(IndexBase):
    """index_local: a node whose gate reports its limit reached is skipped after the probe, and S1's route stands."""

    def setUp(self):
        super().setUp()
        self.report = None
        inner = local_lane.probe

        def probe(node):
            tags = inner(node)
            if self.report is not None:
                local_lane._note_gate(node, self.report)
            return tags
        local_lane.probe = probe
        local_lane._gate_reports.clear()

    def tearDown(self):
        local_lane._gate_reports.clear()
        super().tearDown()

    def test_a_full_gate_keeps_s1s_route(self):
        self.report = {"limit": 2, "inflight": 2, "reason": "owner active"}
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertS1Route(self.route(14.0))
        self.assertTrue(any("node busy (2 of 2 running at the gate: owner active)" in m for m in cm.output))

    def test_a_gate_with_room_is_taken(self):
        self.report = {"limit": 8, "inflight": 3, "reason": "owner idle"}
        self.assertEqual(self.route(14.0)["provider"], "local-pro")


# ------------------------------------------------------------------------------- byte reservations through the gate


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


LONG_MSG = {"model": TAG, "max_tokens": LONG_MAX, "messages": [{"role": "user", "content": "x" * LONG_CHARS}]}


class MlxGateBytesTests(MlxGateHarness):
    """The gate reserves each turn's bytes, reports them, refuses a turn that does not fit now with a retryable 529
    and one that could never fit with a non-retryable 413, and gives the reservation back however the turn ends."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}              # owner idle: count 8, bytes 25 GiB

    def held(self, body):
        """One turn that reaches the backend and stays there until FakeVllm.hold is set: (thread, results)."""
        if FakeVllm.hold is None:
            FakeVllm.hold = threading.Event()
        results = []
        t = threading.Thread(target=lambda: results.append(self.call("/v1/messages", body)), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        return t, results

    def free(self):
        return wait_for(lambda: (self.gate.admission.inflight, self.gate.admission.reserved) == (0, 0))

    def test_a_turn_reserves_its_bytes_reports_them_and_gives_them_back_on_success(self):
        t, results = self.held(LONG_MSG)
        want = self.gate.admission.request_bytes(10 ** 6, LONG_MAX)       # capped at the 131072-token context
        self.assertEqual(self.gate.admission.reserved, want)
        code, raw, _ = self.call("/api/tags")
        report = json.loads(raw)["cortados_gate"]
        self.assertEqual((report["inflight"], report["reserved_bytes"], report["free_bytes"]),
                         (1, want, 25 * GIB - want))
        self.assertEqual(report["fit_tokens"], 131072)
        FakeVllm.hold.set()
        t.join(5)
        self.assertEqual(results[0][0], 200)
        self.assertTrue(self.free())

    def test_a_long_turn_runs_and_a_second_is_a_retryable_529_naming_memory(self):
        # #126: a 128k text turn reserves its computed peak (24.6 GiB of the 25), so a second one waits
        a, _ = self.held(LONG_MSG)
        code, raw, headers = self.call("/v1/messages", LONG_MSG)
        self.assertEqual(code, 529)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["retry-after"], "5")
        data = json.loads(raw)
        self.assertTrue(data["error"]["message"].startswith(local_gate.BUSY_MESSAGE))
        self.assertIn("memory budget", data["error"]["message"])
        self.assertEqual(data["cortados_gate"]["inflight"], 1)
        self.assertEqual(len(turns()), 1)
        self.assertEqual(data["cortados_gate"]["free_tokens"], 0)     # the decode batch would pad a short one to 128k
        FakeVllm.hold.set()
        a.join(5)
        self.assertTrue(self.free())

    def test_released_after_a_backend_error(self):
        FakeVllm.error = (500, {"detail": "boom"})
        self.assertEqual(self.call("/v1/messages", LONG_MSG)[0], 500)
        self.assertTrue(self.free())

    def test_released_after_an_upstream_timeout(self):
        self.gate.read_timeout = 0.3
        FakeVllm.hold = threading.Event()                              # the backend never answers in time
        code, raw, _ = self.call("/v1/messages", LONG_MSG)
        self.assertEqual(code, 502)
        self.assertIn("unreachable on loopback", json.loads(raw)["error"]["message"])   # timed out waiting
        self.assertTrue(self.free())

    def test_released_when_the_backend_is_unreachable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()
        self.gate.upstream_port = closed
        self.assertEqual(self.call("/v1/messages", LONG_MSG)[0], 502)
        self.assertTrue(self.free())

    def test_released_when_the_client_disconnects_mid_turn(self):
        FakeVllm.hold = threading.Event()
        raw = json.dumps({**LONG_MSG, "stream": True}).encode()
        c = socket.create_connection(("127.0.0.1", self.gate.server_port), timeout=5)
        c.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-type: application/json\r\n"
                  b"authorization: Bearer " + TOKEN.encode() + b"\r\ncontent-length: " + str(len(raw)).encode()
                  + b"\r\n\r\n" + raw)
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.assertGreater(self.gate.admission.reserved, 8 * GIB)
        c.close()                                                       # the client goes away mid-turn
        FakeVllm.hold.set()
        self.assertTrue(self.free())

    def test_released_when_the_relay_raises(self):
        with patch.object(local_gate.GateHandler, "_forward", side_effect=RuntimeError("bug")), \
                patch.object(self.gate, "handle_error", lambda *a: None):
            try:
                self.call("/v1/messages", LONG_MSG)
            except (OSError, http.client.HTTPException):
                pass                                                    # the connection dies with the handler
        self.assertTrue(self.free())


class MlxGateTooLargeTests(MlxGateHarness):
    """A budget of 5 GiB beside the weights: a 128k-token turn can never fit, alone or otherwise."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal", "memory_budget_bytes": 20 * GIB}

    def test_a_turn_too_large_to_ever_fit_is_a_non_retryable_413_and_reaches_nothing(self):
        code, raw, headers = self.call("/v1/messages", LONG_MSG)
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(code, 413)
        self.assertEqual(h.get("x-should-retry"), "false")
        self.assertNotIn("retry-after", h)
        data = json.loads(raw)
        self.assertEqual(data["error"]["type"], "request_too_large")
        self.assertTrue(data["error"]["message"].startswith(local_gate.TOO_LARGE_MESSAGE))
        self.assertTrue(data["error"]["message"].startswith(local_lane.GATE_TOO_LARGE))   # the provider's constant
        self.assertEqual(data["cortados_gate"]["inflight"], 0)
        self.assertLess(data["cortados_gate"]["fit_tokens"], 131072)
        self.assertEqual(turns(), [])                                   # never sent
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (0, 0))
        self.assertEqual(self.call("/v1/messages", MSG)[0], 200)        # a short turn still runs


# ------------------------------------------------------------------------------------------------- input tokens: the bound


DENSE = "a!" * 50_000           # 100,000 bytes and, on Qwen's tokenizer, 100,000 tokens (1 byte a token)
CONTEXT = 131072


def dense_msg(max_tokens=1024):
    return {"model": TAG, "max_tokens": max_tokens, "messages": [{"role": "user", "content": DENSE}]}


def compact(body):
    """The body as a Node client (Claude Code) sends it: compact separators, raw UTF-8. The smallest a body gets, so
    the hardest case for a bound on its bytes."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def tokens_of(body, cap=None):
    return local_governor.input_tokens(compact(body), cap)


def tojson(value):
    """The chat template's tojson as transformers defines it (ensure_ascii=False, default separators)."""
    return json.dumps(value, ensure_ascii=False)


def json_system_prompt_060(rf):
    """vllm-mlx 0.6.0's api/tool_calling.build_json_system_prompt for a json_schema response_format (non-thinking),
    line for line: the instruction text, name and description raw, the schema as json.dumps(indent=2)."""
    spec = rf["json_schema"]
    prompt = f"You must respond with a single valid JSON value matching the '{spec.get('name', 'response')}' schema."
    if spec.get("description"):
        prompt += f" {spec['description']}"
    prompt += f"\n\nJSON Schema:\n```json\n{json.dumps(spec.get('schema', {}), indent=2)}\n```\n\n"
    prompt += ("Output rules (STRICT):\n- Your first character MUST be `{` (or `[`).\n- Your last character MUST be "
               "`}` (or `]`).\n- Do NOT wrap the JSON in a markdown code block (no ``` fences).\n- Do NOT prepend any "
               "preamble such as \"Here is the JSON\" or \"Let me format this\".\n- Do NOT include comments, trailing "
               "explanations, or chain-of-thought.\n")
    return prompt


def openai_tool(t):
    """vllm-mlx 0.6.0's api/anthropic_adapter._convert_tool."""
    return {"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                             "parameters": t.get("input_schema") or {"type": "object",
                                                                                     "properties": {}}}}


def utf8(s):
    return len(s.encode("utf-8"))


SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}}
# Deep nesting: json.dumps(indent=2) writes one line per level, indented 2 spaces a level (quadratic in the depth).
DEEP = {}
for _ in range(300):
    DEEP = {"a": [DEEP]}


def reviewer_cases():
    """{name: (body, rendered bytes)}: each case where the rendered prompt outgrows the text the body carries, with the
    UTF-8 bytes of what vllm-mlx 0.6.0 renders from it (template markers aside)."""
    nested = {"model": TAG, "max_tokens": 1024, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [""] * 1_000_000}]}]}
    newline_tool = {"name": "T", "description": "\n" * 100_000, "input_schema": SCHEMA}
    newlines = {"model": TAG, "max_tokens": 1024, "tools": [newline_tool],
                "messages": [{"role": "user", "content": "hi"}]}
    rf = {"type": "json_schema", "json_schema": {"name": "r", "description": DENSE, "schema": SCHEMA}}
    response_format = {"model": TAG, "max_tokens": 1024, "response_format": rf,
                       "messages": [{"role": "user", "content": "hi"}]}
    deep_rf = {"type": "json_schema", "json_schema": {"name": "r", "schema": DEEP}}
    deep = {"model": TAG, "max_tokens": 1024, "response_format": deep_rf,
            "messages": [{"role": "user", "content": "hi"}]}
    accents = {"k": "é" * 50_000}                          # 100,000 bytes raw; json.dumps writes é: 300,000
    non_ascii_input = {"model": TAG, "max_tokens": 1024, "messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Edit", "input": accents}]}]}
    zeros_tool = {"name": "Z", "input_schema": {"enum": [0] * 50_000}}      # "0," 2 bytes; tojson "0, " 3
    zeros = {"model": TAG, "max_tokens": 1024, "tools": [zeros_tool], "messages": [{"role": "user", "content": "hi"}]}
    return {
        "1,000,000 empty tool_result items joined with newlines": (nested, 999_999),
        "a 100,000-newline tool description through tojson": (newlines, utf8(tojson(openai_tool(newline_tool)))),
        "a 100 KB response_format description": (response_format, utf8(json_system_prompt_060(rf)) + 2),
        "a 300-deep response_format schema under indent=2": (deep, utf8(json_system_prompt_060(deep_rf)) + 2),
        "non-ASCII tool_use input through json.dumps": (non_ascii_input, len(json.dumps(accents))),
        "a compact enum through tojson's separators": (zeros, utf8(tojson(openai_tool(zeros_tool)))),
    }


LONG_NAME = "mcp__" + "a!" * 2_000                 # a 4,005-byte tool name


def omitted_msg(max_tokens=1024):
    """An earlier review's case: a token-dense tool_result as string items (100,000 bytes) after a tool_use with a long
    name (vllm-mlx 0.6.0's own count saw none of it)."""
    return {"model": TAG, "max_tokens": max_tokens, "messages": [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": LONG_NAME, "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                      "content": [DENSE[:50_000], DENSE[50_000:]]}]}]}


class InputTokensTests(unittest.TestCase):
    """local_governor.input_tokens: the body's bytes as received + the template allowance + what vllm-mlx re-serializes
    wider, capped at the context. Rendered bytes >= tokens (a byte-level BPE token covers at least one byte), so each
    case checks bound >= rendered bytes."""

    def test_the_reviewer_cases_reserve_at_least_their_rendered_size(self):
        for name, (body, rendered) in reviewer_cases().items():
            with self.subTest(name):
                self.assertGreaterEqual(tokens_of(body), rendered)
                self.assertEqual(tokens_of(body, CONTEXT), min(CONTEXT, tokens_of(body)))
        _, (deep, rendered) = list(reviewer_cases().items())[3]
        self.assertGreater(rendered, 10 * len(compact(deep)))           # indent=2 really outgrows the body

    def test_token_dense_text_is_bounded_by_its_bytes_not_estimated(self):
        body = dense_msg()
        self.assertEqual(tokens_of(body), len(compact(body)) + local_governor.template_tokens(body))
        self.assertGreaterEqual(tokens_of(body), 100_000)                # the tokenizer's real count; chars / 3.5: 28,572
        self.assertGreaterEqual(tokens_of(omitted_msg()), 100_000 + len(LONG_NAME))

    def test_what_vllm_mlx_060_sends_besides_the_text_is_covered(self):
        # the parts vllm-mlx 0.6.0's own count left out, each at least the bytes the adapter sends for it
        cases = {
            "tool_use name and id": ({"messages": [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "i" * 300, "name": "n" * 5000, "input": {"a": 1}}]}]},
                300 + 5000 + len(json.dumps({"a": 1}))),
            "tool_result string items": ({"messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": ["s" * 7000, {"type": "text", "text": "x"}]}]}]},
                1 + 7000 + 1 + 1),
            "system text blocks": ({"system": [{"type": "text", "text": "y" * 6000}, {"type": "text", "text": "z"}],
                                    "messages": []}, 6000 + 1 + 1),
            "tool without a schema": ({"tools": [{"name": "T"}], "messages": []},
                                      utf8(tojson(openai_tool({"name": "T"})))),
            "forced tool_choice": ({"tools": [{"name": "Q" * 3000}], "tool_choice": {"type": "tool", "name": "Q" * 3000},
                                    "messages": []}, 3 * 3000 + 190 + len(tojson(openai_tool({"name": "Q" * 3000})))),
        }
        for name, (body, sent) in cases.items():
            with self.subTest(name):
                self.assertGreaterEqual(tokens_of(body), sent)

    def test_the_bound_reads_the_body_as_sent(self):
        body = dense_msg()
        pretty = json.dumps(body, indent=8).encode()                     # the same request, more bytes
        self.assertEqual(local_governor.input_tokens(pretty) - tokens_of(body), len(pretty) - len(compact(body)))
        self.assertEqual(local_governor.input_tokens(b'{"messages": []}'),
                         len(b'{"messages": []}') + local_governor.TEMPLATE_TOKENS_FIXED)

    def test_the_cap_stops_the_walk_and_a_too_deep_value_counts_as_over_it(self):
        self.assertEqual(tokens_of(reviewer_cases()["a 300-deep response_format schema under indent=2"][0], 5000), 5000)
        deep = []
        for _ in range(100_000):                                          # deeper than the encoder can go
            deep = [deep]
        self.assertEqual(local_governor._dumped_bytes(deep, 77), 78)
        self.assertLessEqual(local_governor._dumped_bytes(list(range(10 ** 6)), 100), 100 + 16)   # stopped early

    def test_round4_nfc_widening_is_bounded_by_the_normalized_bytes(self):
        # The review's case: U+FA6C (3 bytes) is U+242EE (4 bytes) after the Qwen tokenizer's NFC normalizer, so
        # 20,000 of them are 60,000 UTF-8 bytes in the body and 80,000 bytes (up to 80,000 tokens) in the prompt.
        text = "\ufa6c" * 20_000
        self.assertEqual((utf8(text), utf8(unicodedata.normalize("NFC", text))), (60_000, 80_000))
        for body in ({"model": TAG, "max_tokens": 1, "messages": [{"role": "user", "content": text}]},
                     {"model": TAG, "max_tokens": 1, "system": text, "messages": []},
                     {"model": TAG, "max_tokens": 1, "tools": [{"name": "T", "description": text}], "messages": []},
                     {"model": TAG, "max_tokens": 1, "messages": [{"role": "assistant", "content": [
                         {"type": "tool_use", "id": "t", "name": "E", "input": {text: text}}]}]}):
            with self.subTest(next(k for k in ("messages", "system", "tools") if body.get(k))):
                self.assertGreaterEqual(tokens_of(body), utf8(unicodedata.normalize("NFC", text)) * (
                    2 if "input" in json.dumps(body) else 1))
        # the widest single code point (U+1D160: 4 bytes, 12 after normalization), raw and \u-escaped in the body
        notes = "\U0001d160" * 10_000
        body = {"model": TAG, "max_tokens": 1, "messages": [{"role": "user", "content": notes}]}
        self.assertGreaterEqual(tokens_of(body), utf8(unicodedata.normalize("NFC", notes)))
        self.assertGreaterEqual(local_governor.input_tokens(json.dumps(body).encode()),
                                utf8(unicodedata.normalize("NFC", notes)))
        # a forced tool_choice writes the tool's name twice: both copies are counted
        name = "\ufa6c" * 5_000
        forced = {"model": TAG, "max_tokens": 1, "tools": [{"name": name}],
                  "tool_choice": {"type": "tool", "name": name}, "messages": []}
        self.assertGreaterEqual(local_governor.normalization_bytes(forced), 3 * 5_000)
        self.assertEqual(local_governor.normalization_bytes({"messages": [{"content": "ascii, 中文, →"}]}), 0)
        self.assertEqual(local_governor.normalization_bytes({"messages": [{"content": "é"}]}), 1)    # e + U+0301

    def test_nfd_is_never_shorter_than_nfc_over_the_whole_unicode_table(self):
        # The lemma the bound rests on: canonical composition replaces a starter and a combining character by one
        # code point no longer in UTF-8 than the two, so NFC(x) has at most NFD(x)'s bytes, for any x.
        checked = 0
        for cp in range(0x110000):
            d = unicodedata.decomposition(chr(cp))
            if not d or d.startswith("<"):
                continue
            parts = "".join(chr(int(h, 16)) for h in d.split())
            if len(parts) == 2:
                self.assertLessEqual(utf8(chr(cp)), utf8(parts), hex(cp))
                checked += 1
        self.assertGreater(checked, 1000)
        self.assertEqual(utf8("\uac01"), 3)              # Hangul LVT: 3 bytes from three 3-byte jamo
        for s in ("\ufa6c", "\U0001d160", "\u1f82", "e\u0301", "\u0344\u0301", "\uac00\u11a8", "\u0390"):
            self.assertLessEqual(utf8(unicodedata.normalize("NFC", s)), utf8(unicodedata.normalize("NFD", s)))
            self.assertGreaterEqual(local_governor._normalized_excess(s),
                                    utf8(unicodedata.normalize("NFC", s)) - utf8(s))

    def test_code_points_this_python_does_not_know_count_their_widest_decomposition(self):
        unknown = "\U000e0080"                            # unassigned in every Unicode version so far
        self.assertEqual(unicodedata.category(unknown), "Cn")
        self.assertEqual(local_governor._normalized_excess(unknown * 10), 10 * local_governor.UNASSIGNED_EXCESS)
        big = "\u00e9x" * 5_000_000                        # 10M characters: measured in chunks, stopped near the cap
        self.assertLessEqual(local_governor._normalized_excess(big, 1000), 1000 + 2 * local_governor.NORMALIZE_CHUNK)

    def test_a_body_that_is_not_an_object_is_bounded_by_its_length(self):
        fixed = local_governor.TEMPLATE_TOKENS_FIXED
        self.assertEqual(local_governor.input_tokens(b"not json"), 8 + fixed)
        self.assertEqual(local_governor.input_tokens(b"[1, 2]"), 6 + fixed)
        self.assertEqual(local_governor.input_tokens(b"x" * 10 ** 6, CONTEXT), CONTEXT)

    def test_non_text_content_is_named(self):
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 9000}}
        self.assertEqual(local_governor.non_text_content({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"}, img]}]}), "image block in message 1")
        self.assertEqual(local_governor.non_text_content({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "user", "content": [{"type": "document", "source": {"type": "base64", "data": "JVBE"}}]}]}),
            "document block in message 2")
        self.assertEqual(local_governor.non_text_content({"messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "ok"}, img]}]}]}),
            "non-text tool_result content in message 1")
        self.assertEqual(local_governor.non_text_content({"system": [{"type": "image"}], "messages": []}),
                         "non-text system block")
        self.assertEqual(local_governor.non_text_content({"messages": [{"role": "user", "content": [
            {"type": "<script>" * 9}]}]}), "non-text block in message 1")      # never echoes odd type names
        text_only = omitted_msg()
        text_only["messages"].append({"role": "assistant", "content": [{"type": "thinking", "thinking": "h"},
                                                                      {"type": "redacted_thinking", "data": "x"},
                                                                      {"type": "text", "text": "ok"}]})
        self.assertIsNone(local_governor.non_text_content(text_only))


class MlxGateBoundTests(MlxGateHarness):
    """The gate reserves each turn from the body it received, with no call to the backend before the turn itself.
    Owner idle: count 8, 25 GiB beside the weights."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}

    def call_raw(self, raw):
        req = urllib.request.Request(self.url + "/v1/messages", data=raw, method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def held(self, body, raw=None):
        if FakeVllm.hold is None:
            FakeVllm.hold = threading.Event()
        raw = raw if raw is not None else json.dumps(body).encode()
        results = []
        t = threading.Thread(target=lambda: results.append(self.call_raw(raw)), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=20))
        return t, results

    def expect(self, raw, max_tokens):
        return self.gate.admission.request_bytes(local_governor.input_tokens(raw, CONTEXT), max_tokens)

    def free(self):
        return wait_for(lambda: (self.gate.admission.inflight, self.gate.admission.reserved) == (0, 0))

    def test_token_dense_input_is_not_over_admitted(self):
        # an earlier review's case: 100,000 chars of `a!` are 100,000 tokens. The old chars / 3.5 estimate (29,103
        # tokens) admitted 7 at once. #126: each holds its 100k-token KV twice over plus its prefill chunk's scores
        # (~19 GiB computed), so one fits in 25 GiB and no more.
        body = dense_msg()
        held = [self.held(body)]
        self.assertEqual(self.gate.admission.reserved, self.expect(json.dumps(body).encode(), 1024))
        self.assertLessEqual(self.gate.admission.reserved, 25 * GIB)
        for _ in range(4):
            code, raw, _ = self.call("/v1/messages", body)
            self.assertEqual(code, 529)
            self.assertIn("memory budget", json.loads(raw)["error"]["message"])
        self.assertEqual((self.gate.admission.inflight, len(turns())), (1, 1))
        self.assertEqual(FakeVllm.seen, turns())                         # nothing but the turns reached the backend
        FakeVllm.hold.set()
        for t, _ in held:
            t.join(5)
        self.assertTrue(self.free())

    def test_the_reservation_is_the_body_as_received_not_as_adapted(self):
        body = {**MSG, "messages": [{"role": "user", "content": "word " * 200}]}
        pretty = json.dumps(body, indent=8).encode()
        t, _ = self.held(body, pretty)
        self.assertEqual(self.gate.admission.reserved, self.expect(pretty, 16))
        self.assertGreater(self.expect(pretty, 16), self.expect(compact(body), 16))
        self.assertEqual(turns()[0][2]["model"], SERVED)                 # the backend still gets the adapted body
        FakeVllm.hold.set()
        t.join(5)
        self.assertTrue(self.free())

    def test_the_reviewer_cases_reserve_at_least_their_rendered_size(self):
        for name, (body, rendered) in reviewer_cases().items():
            with self.subTest(name):
                reset_fake()
                t, _ = self.held(body, compact(body))
                floor = local_governor.tokens_bytes(self.governor.cfg, min(CONTEXT, rendered + 1024))
                self.assertGreaterEqual(self.gate.admission.reserved, floor)
                FakeVllm.hold.set()
                t.join(20)
                self.assertTrue(self.free())

    def burst(self, n, body=MSG):
        """n turns sent at once; returns (threads, results)."""
        results, go = [], threading.Event()

        def one():
            go.wait(5)
            results.append(self.call("/v1/messages", body))
        threads = [threading.Thread(target=one, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        go.set()
        return threads, results

    def test_ten_at_once_with_a_limit_of_two_run_two_and_release_every_slot(self):
        self.governor.set(idle_s=5)                                      # owner active: 2
        FakeVllm.hold = threading.Event()
        threads, results = self.burst(10)
        for _ in range(2):
            self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.assertTrue(wait_for(lambda: len(results) == 8))
        self.assertEqual(sorted(r[0] for r in results), [529] * 8)
        self.assertEqual((self.gate.admission.inflight, len(turns())), (2, 2))
        FakeVllm.hold.set()
        for t in threads:
            t.join(5)
        self.assertEqual(sorted(r[0] for r in results), [200] * 2 + [529] * 8)
        self.assertTrue(self.free())
        self.assertEqual([r[0] for r in [self.call("/v1/messages", MSG) for _ in range(3)]], [200] * 3)

    def test_eight_at_once_while_idle_all_run(self):
        FakeVllm.hold = threading.Event()
        threads, results = self.burst(8)
        for _ in range(8):
            self.assertTrue(FakeVllm.entered.acquire(timeout=10))
        self.assertEqual(self.gate.admission.inflight, 8)
        FakeVllm.hold.set()
        for t in threads:
            t.join(5)
        self.assertEqual([r[0] for r in results], [200] * 8)
        self.assertTrue(self.free())

    def test_a_refused_turn_takes_nothing(self):
        # busy on bytes (529) and never fits (413): neither the count slot nor any bytes are held afterwards
        body = dense_msg()
        held = [self.held(body)]                                         # ~19 GiB computed of the 25 GiB (#126)
        reserved = self.gate.admission.reserved
        code, raw, _ = self.call("/v1/messages", body)
        self.assertEqual(code, 529)
        self.assertIn("memory budget", json.loads(raw)["error"]["message"])
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (1, reserved))
        too_big = {**dense_msg(), "max_tokens": 10 ** 6}                 # ~1.1M tokens: more than the whole budget
        self.governor.cfg = {**self.governor.cfg, "context_tokens": 10 ** 6}
        code, raw, headers = self.call("/v1/messages", too_big)
        self.assertEqual(code, 413)
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (1, reserved))
        FakeVllm.hold.set()
        for t, _ in held:
            t.join(5)
        self.assertTrue(self.free())

    def test_a_request_with_an_image_or_document_is_refused_for_good_and_reaches_nothing(self):
        # the served 27B is text-only (vision tower excluded at load): an image would be dropped or fail, so the gate
        # refuses it non-retryably before admission, and the provider routes it elsewhere
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 4000}}
        for content, why in (([image, {"type": "text", "text": "what"}], "image block in message 1"),
                             ([{"type": "document", "source": {"type": "base64", "data": "JVBE"}}],
                              "document block in message 1"),
                             ([{"type": "tool_result", "tool_use_id": "t", "content": [image]}],
                              "non-text tool_result content in message 1")):
            code, raw, headers = self.call("/v1/messages", {**MSG, "messages": [{"role": "user", "content": content}]})
            h = {k.lower(): v for k, v in headers.items()}
            self.assertEqual(code, 400)
            self.assertEqual(h.get("x-should-retry"), "false")
            self.assertNotIn("retry-after", h)
            err = json.loads(raw)["error"]
            self.assertEqual(err["type"], "invalid_request_error")
            self.assertTrue(err["message"].startswith(local_gate.UNSUPPORTED_MESSAGE), err["message"])
            self.assertIn(why, err["message"])
        self.assertEqual(FakeVllm.seen, [])                              # never sent
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (0, 0))
        self.assertEqual(local_lane.GATE_UNSUPPORTED, local_gate.UNSUPPORTED_MESSAGE)

    def test_round4_a_prompt_over_the_context_reserves_the_whole_context_and_the_guard_refusal_never_fits(self):
        # The review's repro: "a!" * 100000 with max_tokens 1 is a 200k-token prompt. The gate can reserve no more
        # than the context (the backend holds no more), and the backend's prompt guard refuses the prompt before it is
        # scheduled; the gate answers that as its own never-fits 413 so the provider routes the turn elsewhere.
        body = {"model": TAG, "max_tokens": 1, "messages": [{"role": "user", "content": "a!" * 100000}]}
        self.assertEqual(local_governor.input_tokens(json.dumps(body).encode(), CONTEXT), CONTEXT)
        t, results = self.held(body)
        cfg = self.governor.cfg                                           # the whole context, as #126 computes it
        self.assertEqual(self.gate.admission.reserved,
                         local_governor.text_set_bytes(cfg, [local_governor.text_load(cfg, CONTEXT, 1)])["total"])
        self.assertEqual(self.gate.admission.reserved, self.gate.admission.request_bytes(CONTEXT, 1))
        FakeVllm.error = (413, {"detail": f"{local_backend.GUARD_REFUSAL} (prompt 200017 tokens + max_tokens 1 = "
                                          "200018, over the 131072-token context)"})
        FakeVllm.hold.set()
        t.join(10)
        code, raw, headers = results[0]
        h = {k.lower(): v for k, v in headers.items()}
        data = json.loads(raw)
        self.assertEqual((code, h.get("x-should-retry"), data["error"]["type"]), (413, "false", "request_too_large"))
        self.assertTrue(data["error"]["message"].startswith(local_lane.GATE_TOO_LARGE))
        self.assertIn("prompt 200017 tokens", data["error"]["message"])
        self.assertNotIn("retry-after", h)
        self.assertTrue(self.free())

    def test_round4_a_replacement_chat_template_is_refused_and_reaches_nothing(self):
        body = {**MSG, "chat_template_kwargs": {"chat_template": "{{ 'a!' * 50000 }}"}}
        code, raw, headers = self.call_raw(json.dumps(body).encode())
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual((code, h.get("x-should-retry")), (400, "false"))
        self.assertEqual(json.loads(raw)["error"]["type"], "invalid_request_error")
        self.assertIn("chat_template is not allowed", json.loads(raw)["error"]["message"])
        self.assertEqual(turns(), [])
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (0, 0))

    def test_round4_deep_json_is_a_structured_400_not_a_dropped_connection(self):
        deep = b'{"model": "' + TAG.encode() + b'", "max_tokens": 1, "messages": ' + b"[" * 100_000 \
            + b"]" * 100_000 + b"}"
        code, raw, headers = self.call_raw(deep)
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual((code, h.get("x-should-retry")), (400, "false"))
        self.assertEqual(json.loads(raw)["error"], {"type": "invalid_request_error",
                                                    "message": f"local gate: {local_backend.TOO_DEEP}"})
        self.assertEqual(turns(), [])
        self.assertEqual(self.call("/v1/messages", MSG)[0], 200)            # the gate still serves

    def test_an_ollama_node_does_not_refuse_images(self):
        self.gate.backend = {"backend": "ollama", "served_model": None, "governor": None}
        body = {**MSG, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 40}}]}]}
        self.assertEqual(self.call("/v1/messages", body)[0], 200)        # Ollama's own model decides


class OllamaGovernedGateTests(MlxGateHarness):
    """An Ollama node with a governor: its turns reserve the same body bound, and nothing else is asked."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}

    def setUp(self):
        super().setUp()
        self.gate.backend = {"backend": "ollama", "served_model": None, "governor": None}

    def test_the_body_bound_and_no_other_call(self):
        FakeVllm.hold = threading.Event()
        body = dense_msg()
        t = threading.Thread(target=lambda: self.call("/v1/messages", body), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.assertEqual(self.gate.admission.reserved, self.gate.admission.request_bytes(
            local_governor.input_tokens(json.dumps(body).encode(), CONTEXT), 1024))
        self.assertEqual([s[1] for s in FakeVllm.seen], ["/v1/messages"])
        FakeVllm.hold.set()
        t.join(5)


# ------------------------------------------------------------------------------ the provider and a too-large turn


class ProviderTooLargeGateTests(ProviderBusyGateTests):
    """The same harness with a gate whose budget holds no turn at all (1 GiB beside the weights, less than one
    sequence's state and margin): every turn is the gate's non-retryable too-large refusal."""

    def setUp(self):
        super().setUp()
        self.governor.cfg = local_governor.check_config({"memory_budget_bytes": 16 * GIB, "active_cap": 1})

    def test_a_pinned_turn_too_large_for_the_node_is_a_terminal_413_never_a_529_loop(self):
        code, got, headers = self.raw_post(self.BODY)
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(code, 413)
        self.assertEqual(h.get("x-should-retry"), "false")
        self.assertNotIn("retry-after", h)
        self.assertEqual(got["error"]["type"], "request_too_large")
        self.assertIn("too large for its memory budget", got["error"]["message"])
        self.assertEqual(turns(), [])
        self.assertEqual(self.reg.sessions, {})

    def test_the_refusal_fallback_path_treats_too_large_as_unavailable(self):
        node = local_lane.nodes()[0]
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.complete({"model": "x", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
                                node, node["models"][0])
        self.assertIn("too large", str(cm.exception))

    def test_node_why_reads_the_memory_room_from_the_probe(self):
        node = local_lane.nodes()[0]
        why, _ = routing._node_why(node, node["models"][0], self.BODY, lambda m: False, [])
        self.assertTrue(why.startswith("too large for the node (request ≈"), why)

    # the inherited busy-path tests run on this config too, but they are about the count: skip them here
    test_a_pinned_turn_through_the_mlx_gate_is_served = None
    test_a_full_gate_is_a_529_with_retry_after_and_marks_the_node_busy = None
    test_the_refusal_fallback_path_treats_busy_as_unavailable = None
    test_node_why_reads_the_gate_report_from_the_probe = None
    test_a_gate_out_of_memory_room_is_a_529_naming_the_memory_budget = None


class ProviderUnsupportedGateTests(ProviderBusyGateTests):
    """The same harness, a turn carrying an image: the MLX gate's text-only refusal, never a 529 loop."""
    IMAGE_BODY = {"model": "sub/qwen-27b", "max_tokens": 8, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBO"}},
        {"type": "text", "text": "what is this"}]}]}

    def test_a_pinned_turn_with_an_image_is_a_terminal_400(self):
        code, got, headers = self.raw_post(self.IMAGE_BODY)
        h = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(code, 400)
        self.assertEqual(h.get("x-should-retry"), "false")
        self.assertNotIn("retry-after", h)
        self.assertEqual(got["error"]["type"], "invalid_request_error")
        self.assertIn("text-only model", got["error"]["message"])
        self.assertEqual([x for x in FakeVllm.seen if x[0] == "POST"], [])   # neither counted nor sent
        self.assertEqual(self.reg.sessions, {})
        self.assertEqual(self.raw_post(self.BODY)[0], 200)                  # text still served

    def test_the_refusal_fallback_path_treats_it_as_unavailable(self):
        node = local_lane.nodes()[0]
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.complete({**self.IMAGE_BODY, "model": "x"}, node, node["models"][0])
        self.assertIn("text-only", str(cm.exception))

    test_a_pinned_turn_through_the_mlx_gate_is_served = None
    test_a_full_gate_is_a_529_with_retry_after_and_marks_the_node_busy = None
    test_the_refusal_fallback_path_treats_busy_as_unavailable = None
    test_node_why_reads_the_gate_report_from_the_probe = None
    test_a_gate_out_of_memory_room_is_a_529_naming_the_memory_budget = None


class GateFitTests(unittest.TestCase):
    NODE = {"node": "pro-fit"}

    def tearDown(self):
        local_lane._gate_reports.pop("pro-fit", None)

    def test_fit_verdicts(self):
        local_lane._note_gate(self.NODE, {"limit": 8, "inflight": 2, "reason": "owner idle", "reserved_bytes": 5,
                                          "free_bytes": 7, "fit_tokens": 131072, "free_tokens": 40000})
        self.assertIsNone(local_lane.gate_fit(self.NODE, 30000))
        kind, why = local_lane.gate_fit(self.NODE, 60000)
        self.assertEqual(kind, "busy")
        self.assertIn("memory: ≈40k tokens free beside 2 running", why)
        local_lane._note_gate(self.NODE, {"limit": 8, "inflight": 0, "fit_tokens": 50000, "free_tokens": 50000})
        self.assertEqual(local_lane.gate_fit(self.NODE, 60000)[0], "too_large")
        local_lane._note_gate(self.NODE, {"limit": None, "inflight": 3})          # no governor: no byte budget
        self.assertIsNone(local_lane.gate_fit(self.NODE, 10 ** 6))
        local_lane._note_gate(self.NODE, {"limit": 8, "inflight": 0, "fit_tokens": -1, "free_tokens": True})
        self.assertIsNone(local_lane.gate_fit(self.NODE, 10 ** 6))                # junk room is dropped

    def test_the_provider_estimates_by_characters(self):
        # the provider cannot count tokens: it estimates (routing only); the gate admits on the count or bound
        body = {"model": "x", "max_tokens": 512, "messages": [{"role": "user", "content": "y" * 7000}]}
        c = local_governor.check_config({})
        est = -(-len(json.dumps(body)) // 3.5)
        self.assertEqual(local_lane.request_tokens(body, {"context_length": 131072}),
                         local_governor.request_tokens(c, int(est), 512))
        self.assertEqual(local_lane.request_tokens(body, {"context_length": 1000}), 1000)     # capped


class IndexRouteMemoryTests(IndexRouteBusyGateTests):
    """index_local: a node whose gate says the request does not fit its memory (ever, or now) keeps S1's route."""

    def test_too_large_for_the_node_keeps_s1s_route(self):
        self.report = {"limit": 8, "inflight": 0, "reason": "owner idle", "fit_tokens": 10, "free_tokens": 10}
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertS1Route(self.route(14.0))
        self.assertTrue(any("too large for the node (request ≈" in m for m in cm.output), cm.output)

    def test_no_memory_room_now_keeps_s1s_route(self):
        self.report = {"limit": 8, "inflight": 3, "reason": "owner idle", "fit_tokens": 131072, "free_tokens": 10}
        with self.assertLogs(routing.logger, level="INFO") as cm:
            self.assertS1Route(self.route(14.0))
        self.assertTrue(any("node busy (memory: " in m for m in cm.output), cm.output)

    def test_room_enough_is_taken(self):
        self.report = {"limit": 8, "inflight": 3, "reason": "owner idle", "fit_tokens": 131072, "free_tokens": 90000}
        self.assertEqual(self.route(14.0)["provider"], "local-pro")


def _gate_too_large(handler):
    """FakeOllama's POST answered as the MLX gate refuses a turn that can never fit (a race the probe missed)."""
    length = int(handler.headers["content-length"])
    FakeOllama.seen.append((handler.path, json.loads(handler.rfile.read(length))))
    raw = json.dumps({"type": "error", "error": {"type": "request_too_large", "message":
                      local_lane.GATE_TOO_LARGE + " (request needs 9.6 GiB, more than the 5.0 GiB budget beside the "
                      "weights; at most 56000 tokens of input + max_tokens fit)"},
                      local_lane.GATE_KEY: {"limit": 8, "inflight": 0, "reason": "owner idle", "fit_tokens": 56000,
                                            "free_tokens": 56000}}).encode()
    handler.send_response(413)
    handler.send_header("content-type", "application/json")
    handler.send_header("x-should-retry", "false")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ProviderReroutesTooLargeTests(IndexEndToEndTests):
    """An S1-routed conversation the index route moved to the node, whose gate then refuses the opening turn as too
    large for its memory: the provider routes the conversation elsewhere (S1's cloud route) instead of answering the
    client with an error, and nothing is left bound to the node."""

    def setUp(self):
        super().setUp()
        local_lane._gate_reports.clear()
        p = patch.object(FakeOllama, "do_POST", _gate_too_large)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        local_lane._gate_reports.clear()
        super().tearDown()

    def test_think_variant_turn_reaches_the_node_with_thinking_enabled(self):
        pass                                                           # the parent's case: not this node's behaviour

    def test_the_opening_turn_is_routed_elsewhere(self):
        from tests.test_provider_local_auto import body
        routes = []
        real = self.reg.router

        def router(b, client, **kw):
            routes.append((kw.get("exclude_model"), real(b, client, **kw)))
            return routes[-1][1]
        self.reg.router = router
        with self.assertLogs(server.logger, level="INFO") as cm:
            got = self.post({**body("What is 6*7?"), "max_tokens": 8192})
        self.assertEqual(got.get("type"), "message")                   # answered, not an error
        self.assertEqual(len([x for x in FakeOllama.seen if x[0] == "/v1/messages"]), 1)   # tried once, never again
        self.assertTrue(any("cannot hold the opening turn; routing it elsewhere" in m for m in cm.output))
        self.assertEqual([r["provider"] for _, r in routes], ["local-pro", "claude-cli"])  # S1's route the 2nd time
        self.assertEqual(routes[1][0], TAG)                             # with the node's model excluded
        self.assertEqual(local_lane._inflight, {})
        self.assertEqual(local_lane.gate_report(local_lane.nodes()[0])["fit_tokens"], 56000)


def _gate_unsupported(handler):
    """FakeOllama's POST answered as the MLX gate refuses a turn its text-only model cannot read."""
    length = int(handler.headers["content-length"])
    FakeOllama.seen.append((handler.path, json.loads(handler.rfile.read(length))))
    raw = json.dumps({"type": "error", "error": {"type": "invalid_request_error", "message":
                      local_lane.GATE_UNSUPPORTED + " (image block in message 1; it serves a text-only model)"}}).encode()
    handler.send_response(400)
    handler.send_header("content-type", "application/json")
    handler.send_header("x-should-retry", "false")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class ProviderReroutesUnsupportedTests(ProviderReroutesTooLargeTests):
    """The same S1-routed opening turn, refused by the gate because its text-only model cannot read the content (an
    image): routed elsewhere with the node's model excluded, the client answered by that route."""

    def setUp(self):
        super().setUp()
        p = patch.object(FakeOllama, "do_POST", _gate_unsupported)
        p.start()
        self.addCleanup(p.stop)

    def test_the_opening_turn_is_routed_elsewhere(self):
        from tests.test_provider_local_auto import body
        routes = []
        real = self.reg.router

        def router(b, client, **kw):
            routes.append((kw.get("exclude_model"), real(b, client, **kw)))
            return routes[-1][1]
        self.reg.router = router
        with self.assertLogs(server.logger, level="INFO") as cm:
            got = self.post({**body("What is 6*7?"), "max_tokens": 8192})
        self.assertEqual(got.get("type"), "message")                   # answered, not an error
        self.assertEqual(len([x for x in FakeOllama.seen if x[0] == "/v1/messages"]), 1)
        self.assertTrue(any("cannot read the opening turn's content (text-only model); routing it elsewhere" in m
                            for m in cm.output), cm.output)
        self.assertEqual([r["provider"] for _, r in routes], ["local-pro", "claude-cli"])
        self.assertEqual(routes[1][0], TAG)
        self.assertEqual(local_lane._inflight, {})
        self.assertFalse(any(isinstance(v, server.LocalSession) for v in self.reg.sessions.values()))


if __name__ == "__main__":
    unittest.main()
