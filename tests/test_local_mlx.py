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
LONG_CHARS = int(LONG_IN * 3.5)                             # the gate tests' long message (the fake counts it ~105k)


class ByteBudgetTests(unittest.TestCase):
    """The BYTE bound: each request reserves (input + max_tokens, capped) x 64 KiB + state + margin, and requests are
    admitted while the reservations fit in budget - weights (defaults: 40 GiB - 15 GiB = 25 GiB)."""

    def setUp(self):
        self.g = FixedGovernor(idle_s=3600)                           # owner idle, pressure normal: count limit 8
        self.a = local_governor.Admission(self.g)
        self.c = self.g.cfg

    def long(self):
        return self.a.request_bytes(LONG_IN, LONG_MAX)

    def short(self):
        return self.a.request_bytes(SHORT_IN, SHORT_MAX)

    def test_the_defaults_are_the_measured_model(self):
        c = self.c
        self.assertEqual((c["memory_budget_bytes"], c["weights_bytes"]), (40 * GIB, 15 * GIB))
        self.assertEqual(c["kv_bytes_per_token"], 16 * 2 * 4 * 256 * 2)            # 65,536 bytes per token
        self.assertEqual(131072 * c["kv_bytes_per_token"], 8 * GIB)               # a 128k context holds 8 GiB
        self.assertEqual(local_governor.request_tokens(c, LONG_IN, LONG_MAX), 131072)   # capped at the context
        self.assertEqual(self.long(), 8 * GIB + int(0.15 * GIB) + int(1.5 * GIB))
        self.assertEqual(local_governor.request_tokens(c, SHORT_IN, SHORT_MAX), 8512)
        # no max_tokens: the backend's default output is reserved
        self.assertEqual(local_governor.request_tokens(c, 2, None), 2 + 32768)
        self.assertEqual(local_governor.max_seqs(c), 8)

    def test_two_128k_requests_fit_the_4bit_budget_and_a_third_does_not(self):
        t1, t2 = self.a.try_acquire(self.long())[0], self.a.try_acquire(self.long())[0]
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        ticket, snap = self.a.try_acquire(self.long())
        self.assertIsNone(ticket)
        self.assertEqual(snap["refused"], "busy")                    # it fits alone: retryable, not too_large
        self.assertTrue(snap["reason"].startswith("memory budget: 5.7 GiB free, request needs 9.6 GiB"), snap)
        self.assertEqual((snap["inflight"], snap["limit"]), (2, 8))  # the count had room: the bytes refused it
        self.assertEqual(snap["reserved_bytes"], 2 * self.long())
        self.assertEqual(snap["free_bytes"], 25 * GIB - 2 * self.long())
        t1.release()
        self.assertIsNotNone(self.a.try_acquire(self.long())[0])     # a long one ended: room for another

    def test_eight_short_requests_fit_while_idle(self):
        held = [self.a.try_acquire(self.short())[0] for _ in range(8)]
        self.assertTrue(all(held))
        self.assertLessEqual(self.a.reserved, 25 * GIB)
        ticket, snap = self.a.try_acquire(self.short())
        self.assertIsNone(ticket)
        self.assertEqual(snap["reason"], "owner idle")               # the ninth is refused by the count, not bytes
        self.assertEqual(snap["free_tokens"], 0)                     # full: no room reported

    def test_a_mix_of_long_and_short(self):
        longs = [self.a.try_acquire(self.long())[0] for _ in range(2)]
        shorts = [self.a.try_acquire(self.short())[0] for _ in range(2)]
        self.assertTrue(all(longs + shorts))                         # 2 x 9.65 + 2 x 2.17 = 23.6 GiB of 25
        ticket, snap = self.a.try_acquire(self.short())
        self.assertIsNone(ticket)                                    # a third short does not fit beside two long
        self.assertIn("memory budget", snap["reason"])
        self.assertEqual(snap["inflight"], 4)
        self.assertLess(snap["free_tokens"], 8512)
        longs[0].release()
        more = [self.a.try_acquire(self.short())[0] for _ in range(4)]
        self.assertTrue(all(more))                                   # one long gone: four shorts in its place
        self.assertEqual(self.a.inflight, 7)
        # one long and several short: a second long no longer fits beside them
        self.assertIsNone(self.a.try_acquire(self.long())[0])

    def test_the_owner_and_pressure_caps_still_apply_when_bytes_fit(self):
        self.g.set(idle_s=5)                                         # owner active: 2, though 8 short would fit
        held = [self.a.try_acquire(self.short())[0] for _ in range(2)]
        self.assertTrue(all(held))
        self.assertEqual(self.a.try_acquire(self.short())[1]["reason"], "owner active")
        for t in held:
            t.release()
        self.g.set(idle_s=3600, pressure="warn")                     # the safety net: 1 at once whatever the bytes
        self.assertIsNotNone(self.a.try_acquire(self.short())[0])
        self.assertEqual(self.a.try_acquire(self.short())[1]["reason"], "memory pressure warn")
        self.g.set(pressure="critical")
        self.assertIsNone(self.a.try_acquire(self.short())[0])

    def test_a_single_request_that_fits_alone_is_admitted_when_nothing_runs(self):
        six = local_governor.Admission(FixedGovernor(idle_s=3600, idle_cap=6, weights_bytes=int(22.4 * GIB)))
        first, snap = six.try_acquire(self.long())                   # 17.6 GiB free: one 128k request (9.65)
        self.assertIsNotNone(first)
        ticket, snap = six.try_acquire(self.long())
        self.assertEqual((ticket, snap["refused"]), (None, "busy"))  # not two
        first.release()
        self.assertIsNotNone(six.try_acquire(self.long())[0])

    def test_a_request_too_large_to_ever_fit_is_refused_non_retryably(self):
        small = local_governor.Admission(FixedGovernor(idle_s=3600, memory_budget_bytes=20 * GIB))   # 5 GiB room
        ticket, snap = small.try_acquire(small.request_bytes(LONG_IN, LONG_MAX))
        self.assertIsNone(ticket)
        self.assertEqual(snap["refused"], "too_large")               # with nothing in flight: never
        self.assertIn("more than the 5.0 GiB budget beside the weights", snap["reason"])
        self.assertEqual(small.inflight, 0)
        self.assertEqual(snap["fit_tokens"], (5 * GIB - int(0.15 * GIB) - int(1.5 * GIB)) // 65536)
        self.assertIsNotNone(small.try_acquire(small.request_bytes(SHORT_IN, SHORT_MAX))[0])

    def test_release_gives_back_once(self):
        t = self.a.try_acquire(self.long())[0]
        t.release()
        t.release()                                                  # a second path calling it: no double free
        self.assertEqual((self.a.inflight, self.a.reserved), (0, 0))
        other = self.a.try_acquire(self.short())[0]
        t.release()
        self.assertEqual((self.a.inflight, self.a.reserved), (1, self.short()))
        other.release()

    def test_the_report_carries_bytes_and_token_room(self):
        t = self.a.try_acquire(self.long())[0]
        snap = self.a.snapshot()
        self.assertEqual(snap["reserved_bytes"], self.long())
        self.assertEqual(snap["free_bytes"], 25 * GIB - self.long())
        self.assertEqual(snap["fit_tokens"], 131072)                  # anything up to the context fits alone
        self.assertEqual(snap["free_tokens"], 131072)                 # and a second 128k fits beside it
        self.a.try_acquire(self.long())
        self.assertLess(self.a.snapshot()["free_tokens"], 131072)
        t.release()


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

    def test_model_path_is_config_and_a_six_bit_copy_is_a_config_change(self):
        self.example["vllm_mlx"]["model"] = "qwen-27b-6bit"
        self.example["vllm_mlx"]["models"]["qwen-27b-6bit"]["path"] = "/models/q6"
        c = local_backend.load(self.write(self.example))
        argv = local_backend.serve_argv(c)
        self.assertEqual(argv[1:3], ["serve", "/models/q6"])
        self.assertEqual(argv[argv.index("--max-num-seqs") + 1], "6")      # the 6-bit entry's own idle cap
        self.assertEqual(c["governor"]["weights_bytes"], int(22.4 * GIB))      # measured, no longer a placeholder
        self.assertEqual(c["governor"]["default_max_tokens"], 32768)          # vllm_mlx.serve.max_tokens

    def test_serve_argv_and_plist(self):
        c = local_backend.load(str(EXAMPLE))
        argv = local_backend.serve_argv(c)
        self.assertTrue(argv[0].endswith("/venvs/mlx-bench/bin/vllm-mlx"))
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
    count = None                                 # POST /v1/messages/count_tokens answers this; None: ~4 chars a token
    count_answer = None                          # (status, raw bytes) for count_tokens instead (a failing count)
    count_delay = 0.0                            # seconds count_tokens waits before answering (a slow count)

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
        FakeVllm.seen.append(("POST", self.path, body))
        if self.path == "/v1/messages/count_tokens":
            if FakeVllm.count_delay:
                time.sleep(FakeVllm.count_delay)
            if FakeVllm.count_answer is not None:
                status, raw = FakeVllm.count_answer
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            n = FakeVllm.count if FakeVllm.count is not None else len(json.dumps(body.get("messages"))) // 4
            return self._json(200, {"input_tokens": n})
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
    FakeVllm.count = None
    FakeVllm.count_answer = None
    FakeVllm.count_delay = 0.0


def turns():
    """The model turns that reached the fake backend (POST /v1/messages; the gate's token counts are not turns)."""
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
        self.assertEqual(data["cortados_gate"], {"backend": "vllm-mlx", "limit": None, "inflight": 0, "owner": None,
                                                 "pressure": None, "reason": "no governor", "reserved_bytes": None,
                                                 "free_bytes": None, "fit_tokens": None, "free_tokens": None})
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
        self.assertEqual(self.gate.admission.inflight, 0)                    # the slot was given back
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
        self.assertEqual(self.gate.admission.inflight, 0)


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

    def test_two_long_turns_run_and_a_third_is_a_retryable_529_naming_memory(self):
        a, _ = self.held(LONG_MSG)
        b, _ = self.held(LONG_MSG)
        code, raw, headers = self.call("/v1/messages", LONG_MSG)
        self.assertEqual(code, 529)
        self.assertEqual({k.lower(): v for k, v in headers.items()}["retry-after"], "5")
        data = json.loads(raw)
        self.assertTrue(data["error"]["message"].startswith(local_gate.BUSY_MESSAGE))
        self.assertIn("memory budget", data["error"]["message"])
        self.assertEqual(data["cortados_gate"]["inflight"], 2)
        self.assertEqual(len(turns()), 2)
        self.assertGreaterEqual(data["cortados_gate"]["free_tokens"], 8512)   # a short one still fits beside them
        FakeVllm.hold.set()
        a.join(5)
        b.join(5)
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
        self.assertEqual(turns(), [])                                   # counted, never sent
        self.assertEqual((self.gate.admission.inflight, self.gate.admission.reserved), (0, 0))
        self.assertEqual(self.call("/v1/messages", MSG)[0], 200)        # a short turn still runs


# ----------------------------------------------------------------------------- input tokens: exact count or bound


DENSE = "a!" * 50_000           # 100,000 bytes and, on Qwen's tokenizer, 100,000 tokens (1 byte a token)


def dense_msg(max_tokens=1024):
    return {"model": TAG, "max_tokens": max_tokens, "messages": [{"role": "user", "content": DENSE}]}


class InputTokensTests(unittest.TestCase):
    """local_governor.input_tokens: the backend's exact count plus the template allowance, else a hard byte bound."""

    def test_token_dense_text_is_bounded_by_its_bytes_not_estimated(self):
        body = dense_msg()
        tokens, how = local_governor.input_tokens(body)
        self.assertEqual(how, "bound")
        self.assertEqual(tokens, 100_000 + local_governor.template_tokens(body))
        self.assertGreaterEqual(tokens, 100_000)      # the tokenizer's real count; chars / 3.5 said 28,572

    def test_the_bound_counts_utf8_bytes_and_everything_the_model_sees(self):
        body = {"system": [{"type": "text", "text": "sys"}],
                "tools": [{"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "é" * 1000},
                             {"role": "assistant", "content": [
                                 {"type": "thinking", "thinking": "hmm"},
                                 {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "日本"}}]},
                             {"role": "user", "content": [
                                 {"type": "tool_result", "tool_use_id": "t1",
                                  "content": [{"type": "text", "text": "x" * 50}]},
                                 {"type": "mystery", "data": "z" * 40}]}]}
        tokens, how = local_governor.input_tokens(body)
        self.assertEqual(how, "bound")
        self.assertEqual(local_governor.template_tokens(body), 1024 + 3 * 16 + (1 + 2 + 2) * 32 + 64)
        visible = local_governor.input_bound_bytes(body)
        self.assertGreaterEqual(visible, 3 + 2000 + 3 + 4 + len(json.dumps({"path": "日本"})) + 50 + 40)
        self.assertEqual(tokens, visible + local_governor.template_tokens(body))

    def test_an_exact_count_is_used_with_the_template_allowance(self):
        body = dense_msg()
        self.assertEqual(local_governor.input_tokens(body, 100_000),
                         (100_000 + local_governor.template_tokens(body), "exact"))
        for junk in (None, -1, True, "5", 2.5):                         # no usable count: the bound
            self.assertEqual(local_governor.input_tokens(body, junk)[1], "bound")

    def test_an_image_is_never_counted_by_the_text_count(self):
        body = {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 9000}},
            {"type": "text", "text": "what is this"}]}]}
        self.assertFalse(local_governor.countable(body))
        tokens, how = local_governor.input_tokens(body, 3)
        self.assertEqual(how, "bound")
        self.assertGreater(tokens, 9000)

    def test_a_body_that_is_not_an_object_is_bounded_by_its_length(self):
        self.assertEqual(local_governor.input_tokens(b"not json"), (8, "bound"))
        self.assertEqual(local_governor.input_tokens(b"[1, 2]")[1], "bound")


class MlxGateCountTests(MlxGateHarness):
    """The gate asks vllm-mlx for the exact input count on loopback before admission, and takes the byte bound when it
    cannot get one. Owner idle: count 8, 25 GiB beside the weights."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}

    def held(self, body):
        if FakeVllm.hold is None:
            FakeVllm.hold = threading.Event()
        results = []
        t = threading.Thread(target=lambda: results.append(self.call("/v1/messages", body)), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        return t, results

    def expect(self, body, input_tokens):
        return self.gate.admission.request_bytes(input_tokens + local_governor.template_tokens(body),
                                                 body["max_tokens"])

    def counts(self):
        return [s for s in FakeVllm.seen if s[:2] == ("POST", "/v1/messages/count_tokens")]

    def test_token_dense_input_with_a_high_count_is_not_over_admitted(self):
        # the review's case: 100,000 chars of `a!` with a tokenizer that counts 100,000 tokens. The old chars / 3.5
        # estimate (29,103 tokens) admitted 7 at once; each really holds ~7.9 GiB, so 3 fit in 25 GiB and no more.
        FakeVllm.count = 100_000
        body = dense_msg()
        held = [self.held(body) for _ in range(3)]
        self.assertEqual(self.gate.admission.reserved, 3 * self.expect(body, 100_000))
        self.assertLessEqual(self.gate.admission.reserved, 25 * GIB)
        for _ in range(4):
            code, raw, _ = self.call("/v1/messages", body)
            self.assertEqual(code, 529)
            self.assertIn("memory budget", json.loads(raw)["error"]["message"])
        self.assertEqual((self.gate.admission.inflight, len(turns())), (3, 3))
        self.assertEqual(len(self.counts()), 7)
        self.assertEqual(self.counts()[0][2]["model"], SERVED)          # the adapted body is what is counted
        FakeVllm.hold.set()
        for t, _ in held:
            t.join(5)
        self.assertTrue(wait_for(lambda: (self.gate.admission.inflight, self.gate.admission.reserved) == (0, 0)))

    def test_the_exact_count_sets_the_reservation(self):
        FakeVllm.count = 40
        t, _ = self.held(MSG)
        self.assertEqual(self.gate.admission.reserved, self.expect(MSG, 40))
        FakeVllm.hold.set()
        t.join(5)

    def test_falls_back_to_the_byte_bound_when_the_count_fails(self):
        self.gate.count_deadline = 0.3
        body = dense_msg()
        bound = self.expect(body, 100_000)                               # 100,000 bytes of text
        for setup in (lambda: setattr(FakeVllm, "count_answer", (500, b'{"detail": "boom"}')),
                      lambda: setattr(FakeVllm, "count_answer", (404, b'{"detail": "Not Found"}')),
                      lambda: setattr(FakeVllm, "count_answer", (200, b"not json")),
                      lambda: setattr(FakeVllm, "count_answer", (200, b'{"input_tokens": -4}')),
                      lambda: setattr(FakeVllm, "count_delay", 1.0)):     # past the count deadline
            reset_fake()
            FakeVllm.count = 7                                           # what a working count would have said
            setup()
            with self.assertLogs(local_gate.logger, level="INFO") as cm:
                t, _ = self.held(body)
            self.assertEqual(self.gate.admission.reserved, bound)
            self.assertTrue(any("bounded by bytes" in m for m in cm.output))
            FakeVllm.hold.set()
            t.join(5)
            self.assertTrue(wait_for(lambda: self.gate.admission.reserved == 0))

    def test_an_unreachable_count_falls_back_too(self):
        # the count's connect is refused (the backend gone between two calls): the bound, and _count never raises
        refused = ConnectionRefusedError("refused")
        with patch.object(local_gate, "deadline_call", side_effect=refused):
            t, _ = self.held(dense_msg())
        self.assertEqual(self.gate.admission.reserved, self.expect(dense_msg(), 100_000))
        FakeVllm.hold.set()
        t.join(5)

    def test_a_request_with_an_image_is_bounded_without_asking_the_count(self):
        body = {**MSG, "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 4000}},
            {"type": "text", "text": "what"}]}]}
        t, _ = self.held(body)
        self.assertEqual(self.counts(), [])
        self.assertGreaterEqual(self.gate.admission.reserved, self.expect(body, 4000))
        FakeVllm.hold.set()
        t.join(5)

    def test_a_full_count_is_refused_before_asking_for_tokens(self):
        self.governor.set(idle_s=5)                                      # owner active: 2
        FakeVllm.hold = threading.Event()
        held = [self.held(MSG) for _ in range(2)]
        self.assertEqual(self.call("/v1/messages", MSG)[0], 529)
        self.assertEqual(len(self.counts()), 2)                          # the third was never counted
        FakeVllm.hold.set()
        for t, _ in held:
            t.join(5)

    def test_the_count_route_is_never_reachable_from_outside(self):
        route = local_backend.COUNT_TOKENS["vllm-mlx"]
        self.assertNotIn(route, local_gate.ROUTES)
        self.assertNotIn(route, set(local_backend.UPSTREAM["vllm-mlx"]))
        for upstream in local_backend.UPSTREAM.values():
            self.assertNotIn(route, set(upstream.values()))
        for path in ("/v1/messages/count_tokens", "/v1/messages/count_tokens?x=1", "/v1/messages/../messages/count_tokens"):
            code, raw, _ = self.call(path, MSG)
            self.assertEqual(code, 404, path)
        code, _, _ = self.call("/v1/messages/count_tokens", MSG, token="b" * 40)     # nor without the token
        self.assertEqual(code, 401)
        self.assertEqual(FakeVllm.seen, [])


class OllamaGovernedGateTests(MlxGateHarness):
    """An Ollama node with a governor has no count route: its turns reserve the byte bound, and nothing else is asked."""
    GOVERNOR = {"idle_s": 3600.0, "pressure": "normal"}

    def setUp(self):
        super().setUp()
        self.gate.backend = {"backend": "ollama", "served_model": None, "governor": None}

    def test_bound_without_a_count_call(self):
        if FakeVllm.hold is None:
            FakeVllm.hold = threading.Event()
        body = dense_msg()
        t = threading.Thread(target=lambda: self.call("/v1/messages", body), daemon=True)
        t.start()
        self.assertTrue(FakeVllm.entered.acquire(timeout=5))
        self.assertEqual(self.gate.admission.reserved, self.gate.admission.request_bytes(
            100_000 + local_governor.template_tokens(body), 1024))
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
        # the provider cannot count tokens: it estimates (routing only); the gate admits on the exact count or bound
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


if __name__ == "__main__":
    unittest.main()
