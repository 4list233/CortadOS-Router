import json
import os
import time
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from cortados_router import studio_capacity as c


N1 = "0000000000000000000000000000000a"  # canonical nonce (uuid4().hex shape)


class PolicyTests(unittest.TestCase):
    def decision(self, **kw):
        inputs = dict(training_active=False, eval_active=False,
                      free_reclaimable_bytes=16*c.GIB, swap_used_bytes=2*c.GIB,
                      model_loaded=False, last_change_ts=100, now=220)
        inputs.update(kw)
        return c.decide(**inputs).action

    def test_load_boundary(self):
        self.assertEqual(self.decision(), "load")

    def test_dwell(self):
        self.assertEqual(self.decision(now=219), "hold")

    def test_below_load(self):
        self.assertEqual(self.decision(free_reclaimable_bytes=16*c.GIB-1), "hold")

    def test_keep_boundary(self):
        self.assertEqual(self.decision(model_loaded=True, free_reclaimable_bytes=8*c.GIB), "hold")

    def test_shed_low(self):
        self.assertEqual(self.decision(model_loaded=True, free_reclaimable_bytes=8*c.GIB-1), "unload")

    def test_heavy_priority(self):
        for key in ("training_active", "eval_active"):
            self.assertEqual(self.decision(model_loaded=True, **{key: True}), "unload")
            self.assertEqual(self.decision(**{key: True}), "hold")

    def test_swap_ceiling(self):
        self.assertEqual(self.decision(model_loaded=True, swap_used_bytes=4*c.GIB), "unload")

    def test_invalid(self):
        for kw in ({"free_reclaimable_bytes": None}, {"swap_used_bytes": -1},
                   {"now": float("nan")}, {"last_change_ts": 221},
                   {"training_active": None}):
            self.assertEqual(self.decision(**kw), "unload")


class ParsingTests(unittest.TestCase):
    def test_captured_vm_stat(self):
        root = Path(__file__).parent / "fixtures" / "studio-capacity"
        for name, expected in (("vm-stat-16k.txt", 14811693056),
                               ("vm-stat-4k.txt", 3702923264),
                               ("vm-stat-local-captured.txt", (148978+1088698+1991)*16384)):
            self.assertEqual(c.parse_vm_stat((root/name).read_text()), expected)

    def test_vm_stat_missing(self):
        with self.assertRaises(ValueError):
            c.parse_vm_stat("page size of 16384 bytes\nPages free: 1.")

    def test_pressure_not_budget(self):
        self.assertEqual(c.parse_memory_pressure("System-wide memory free percentage: 70%"), 70)
        with self.assertRaises(ValueError):
            c.parse_memory_pressure("unavailable")

    def test_swap(self):
        self.assertEqual(c.parse_swap("vm.swapusage: total = 3072.00M used = 1883.94M free = 1188.06M"),
                         int(1883.94*1024**2))
        self.assertEqual(c.parse_swap("used = 2.00G"), 2*c.GIB)

    def test_processes(self):
        self.assertEqual(c.parse_processes("35794 T /x/python -m mlx_lm lora --train"), (True, False))
        self.assertEqual(c.parse_processes("34422 S /bin/bash /x/s1/nightly.sh"), (True, False))
        self.assertEqual(c.parse_processes("123 S python evaluate.py --baseline"), (False, True))
        self.assertEqual(c.parse_processes("74980 S python /x/s1/serve.py\n75539 S python overnight.py --continuous -- /bin/bash /x/s1/nightly.sh"), (False, False))
        self.assertEqual(c.parse_processes("74980 S python /x/s1/serve.py"), (False, False))

    def test_bad_process_record(self):
        for text in ("garbled", ""):
            with self.assertRaises(ValueError):
                c.parse_processes(text)

    def test_preflight_and_laya(self):
        self.assertTrue(c.parse_processes("123 S python -c \"from mlx_lm import load; load(123)\"")[0])
        # Real ps output: argv joined by spaces, no quotes around -c source.
        self.assertTrue(c.parse_processes("123 S /x/python3 -c from mlx_lm import load; load('/x/model')")[0])
        self.assertTrue(c.parse_processes("123 S /x/python3 -u -m mlx_lm.lora --train")[0])
        self.assertEqual(c.parse_processes("123 S /x/python3 -u -B /x/s1/evaluate.py --x"), (False, True))
        self.assertEqual(c.parse_processes("123 S /x/python3 -W ignore evaluate.py"), (False, True))
        self.assertEqual(c.parse_processes("9 S /bin/bash -x /x/s1/nightly.sh"), (True, False))
        self.assertTrue(c.parse_processes("123 T /x/laya/python /x/laya/train.py")[0])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.loaded = False
        self.calls = []
        self.inputs = dict(training_active=False, eval_active=False,
                           free_reclaimable_bytes=16*c.GIB, swap_used_bytes=0)
        def api(path, payload=None):
            self.calls.append((path, payload))
            if path == "/api/ps":
                return {"models": [{"name": c.MODEL}] if self.loaded else []}
            self.loaded = payload["keep_alive"] != 0
            return {}
        self.runner = c.Runner(collect=lambda: self.inputs.copy(), api=api, clock=lambda: self.now)

    def advance(self, seconds):
        for _ in range(seconds // 5):
            self.now += 5
            result = self.runner.tick()
        return result

    def test_load_then_training_unload(self):
        self.assertFalse(self.runner.tick()["available"])
        self.assertTrue(self.advance(120)["available"])
        payload = [p for _, p in self.calls if p][-1]
        self.assertEqual(payload["options"], {"num_ctx": 8192})
        self.inputs["training_active"] = True
        self.assertFalse(self.runner.tick()["available"])
        self.assertFalse(self.loaded)

    def test_dwell_resets_on_dip(self):
        self.runner.tick()
        self.now += 119
        self.inputs["free_reclaimable_bytes"] -= 1
        self.runner.tick()
        self.inputs["free_reclaimable_bytes"] += 1
        self.now += 1
        self.assertFalse(self.runner.tick()["available"])
        self.assertTrue(self.advance(120)["available"])

    def test_collection_failure_unloads(self):
        self.loaded = True
        def fail():
            raise ValueError("bad telemetry")
        self.runner.collect = fail
        self.assertFalse(self.runner.tick()["available"])
        self.assertFalse(self.loaded)

    def test_sampling_gap_resets_dwell(self):
        self.runner.tick()
        self.now += 120
        self.assertFalse(self.runner.tick()["available"])
        self.assertTrue(self.advance(120)["available"])

    def test_failed_load_never_available(self):
        original = self.runner.api
        def refuse(path, payload=None):
            if payload and payload["keep_alive"] == c.LEASE_SECONDS:
                raise OSError("load failed")
            return original(path, payload)
        self.runner.api = refuse
        self.runner.tick()
        result = self.advance(120)
        self.assertFalse(result["available"])
        self.assertIn("OSError", result["reason"])

    def test_unload_failure_reported(self):
        def fail(*args):
            raise OSError("Ollama down")
        self.runner.api = fail
        result = self.runner.tick()
        self.assertFalse(result["available"])
        self.assertEqual(result["unload_error"], "OSError")

    def test_residency_not_confirmed(self):
        self.runner.api = lambda path, payload=None: {"models": []}
        self.runner.tick()
        result = self.advance(120)
        self.assertFalse(result["available"])
        self.assertIn("RuntimeError", result["reason"])

    def test_slow_control_expires_status(self):
        original = self.runner.api
        def slow(path, payload=None):
            if payload and payload["keep_alive"]:
                self.now += 20
            return original(path, payload)
        self.runner.api = slow
        self.runner.tick()
        self.assertFalse(self.advance(120)["available"])
        # Residency itself is revoked, not just the status flag.
        self.assertFalse(self.loaded)
        self.assertEqual(self.calls[-1][1]["keep_alive"], 0)

    def test_stale_observation_never_loads(self):
        collect = self.runner.collect
        def slow_collect():
            inputs = collect()
            self.now += c.MAX_OBSERVATION_AGE_SECONDS + 1
            return inputs
        self.runner.tick()
        self.advance(120)
        self.runner.collect = slow_collect
        self.now += 5
        result = self.runner.tick()
        self.assertFalse(result["available"])
        self.assertFalse(self.loaded)
        self.assertFalse(any(p and p["keep_alive"] for _, p in self.calls[-3:]))

    def test_lease_bounded_and_renewed(self):
        self.runner.tick()
        self.advance(120)
        leases = [p["keep_alive"] for _, p in self.calls if p]
        self.assertTrue(leases)
        self.assertTrue(all(k == c.LEASE_SECONDS for k in leases))
        before = len(leases)
        self.advance(5)
        self.assertEqual(len([p for _, p in self.calls if p]), before + 1)
        self.assertNotIn(-1, leases)
        # Renewals keep the 8K context, not just the first load.
        renewals = [p for _, p in self.calls if p and p["keep_alive"]]
        self.assertGreater(len(renewals), 1)
        for payload in renewals:
            self.assertEqual(payload["options"], {"num_ctx": c.CONTEXT})

    def test_startup_with_training_sheds_before_residency_query(self):
        # Unknown residency at startup must shed like loaded.
        self.loaded = True
        self.inputs["training_active"] = True
        self.runner.tick()
        self.assertEqual(self.calls[0], ("/api/generate",
                         {"model": c.MODEL, "stream": False, "keep_alive": 0}))
        self.assertEqual(self.calls[1][0], "/api/ps")
        self.assertFalse(self.loaded)

    def test_training_sheds_before_residency_query(self):
        self.runner.tick()
        self.advance(120)
        self.inputs["training_active"] = True
        start = len(self.calls)
        self.now += 5
        self.runner.tick()
        self.assertEqual(self.calls[start], ("/api/generate",
                         {"model": c.MODEL, "stream": False, "keep_alive": 0}))
        self.assertFalse(self.loaded)

    def test_termination_unloads(self):
        self.runner.tick()
        self.advance(120)
        self.assertTrue(self.loaded)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"capacity.json"
            def sigterm(_):
                raise SystemExit(143)
            with self.assertRaises(SystemExit):
                c.serve(self.runner, path, sleep=sigterm)
            self.assertFalse(self.loaded)
            self.assertFalse(json.loads(path.read_text())["available"])

    def test_shutdown_unload_failure_reported(self):
        self.runner.tick()
        self.advance(120)
        self.assertTrue(self.loaded)
        def down(path, payload=None):
            raise OSError("Ollama down")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"capacity.json"
            def sigterm(_):
                self.runner.api = down
                raise SystemExit(143)
            with patch("sys.stderr") as err:
                with self.assertRaises(SystemExit):
                    c.serve(self.runner, path, sleep=sigterm)
            logged = "".join(call.args[0] for call in err.write.call_args_list)
            self.assertIn("shutdown unload failed: OSError", logged)
            final = json.loads(path.read_text())
            self.assertFalse(final["available"])
            self.assertIsNone(final["model_loaded"])
            self.assertEqual(final["unload_error"], "OSError")
            self.assertIsNone(self.runner.maybe_loaded)

    def test_status_write_failure_unloads(self):
        self.runner.tick()
        self.advance(120)
        self.assertTrue(self.loaded)
        with patch.object(c, "write_status", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                c.serve(self.runner, Path("/nonexistent/x.json"), sleep=lambda _: None)
        self.assertFalse(self.loaded)

    def test_clock_rollback_resets(self):
        self.runner.tick()
        self.advance(60)
        self.now = 50
        self.assertFalse(self.runner.tick()["available"])

    def test_status_atomic_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"capacity.json"
            c.write_status(path, self.runner.tick())
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_gather_injected(self):
        samples = {"vm_stat": (Path(__file__).parent/"fixtures/studio-capacity/vm-stat-16k.txt").read_text(),
                   "memory_pressure": "System-wide memory free percentage: 70%",
                   "kern.memorystatus_vm_pressure_level": "1",
                   "vm.swapusage": "used = 1883.94M", "ps": "123 T python evaluate.py"}
        def run(argv):
            return samples[Path(argv[0]).name if Path(argv[0]).name != "sysctl" else argv[-1]]
        with patch.object(c.sys, "platform", "darwin"):
            self.assertTrue(c.gather(run)["eval_active"])
            samples["kern.memorystatus_vm_pressure_level"] = "2"
            self.assertEqual(c.gather(run)["free_reclaimable_bytes"], 0)


if __name__ == "__main__":
    unittest.main()


class HandshakeTests(unittest.TestCase):
    """Training lease/ack handshake; temp dirs and fake Ollama only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = Path(self.tmp.name) / "studio-gate"
        self.status = Path(self.tmp.name) / "status.json"
        self.now = 1000.0
        self.loaded = True
        self.calls = []
        self.dead = set()
        self.inputs = dict(training_active=False, eval_active=False,
                           free_reclaimable_bytes=32*c.GIB, swap_used_bytes=0)
        self.logs = []
        self.runner = c.Runner(collect=lambda: self.inputs.copy(), api=self.api,
                               clock=lambda: self.now, gate=self.gate,
                               alive=lambda pid: pid not in self.dead)

    def api(self, path, payload=None):
        self.calls.append((path, payload))
        if path == "/api/ps":
            return {"models": [{"name": c.MODEL}] if self.loaded else []}
        self.loaded = payload["keep_alive"] != 0
        return {}

    def intent(self, pid=4242, nonce=N1, expires=None):
        self.gate.mkdir(parents=True, exist_ok=True)
        c.write_status(self.gate / ("intent-%d-%s.json" % (pid, nonce)),
                       dict(version=1, pid=pid, nonce=nonce, created_at=(self.now if expires is None else expires - 300),
                            expires_at=self.now + 300 if expires is None else expires))

    def test_intent_sheds_and_acks(self):
        self.runner.maybe_loaded = True
        self.intent()
        result = self.runner.tick()
        self.assertFalse(self.loaded)
        # Shed precedes the residency query, so the decision itself is hold.
        self.assertIn(("/api/generate", {"model": c.MODEL, "stream": False, "keep_alive": 0}), self.calls)
        self.assertEqual(result["training_intents"], 1)
        ack = json.loads((self.gate / ("ack-%s.json" % N1)).read_text())
        self.assertEqual((ack["nonce"], ack["model_loaded"]), (N1, False))

    def test_no_load_or_renew_while_intent_live(self):
        self.loaded = False
        self.intent()
        for _ in range(60):  # 5 minutes of otherwise eligible samples
            self.now += 5
            self.intent()  # trainer keeps refreshing
            self.runner.tick()
        self.assertFalse(self.loaded)
        self.assertFalse([p for p in self.calls if p[0] == "/api/generate" and p[1]["keep_alive"] != 0])

    def test_expired_intent_ignored_and_removed(self):
        self.loaded = False
        self.intent(expires=self.now - 1)
        self.assertEqual(c.live_intents(self.gate, self.now), [])
        self.assertFalse(list(self.gate.glob("intent-*")))

    def test_dead_trainer_intent_cleaned(self):
        self.dead.add(4242)
        self.intent()
        (self.gate / ("ack-%s.json" % N1)).write_text("{}")
        self.assertEqual(c.live_intents(self.gate, self.now, alive=lambda p: p not in self.dead), [])
        self.assertFalse(list(self.gate.iterdir()))

    def test_invalid_intent_schema_takes_malformed_path(self):
        good = dict(version=1, pid=4242, nonce=N1, created_at=self.now, expires_at=self.now + 300)
        bad = [dict(expires_at=float("nan")), dict(expires_at=float("inf")), dict(created_at=float("-inf")),
               dict(pid=0), dict(pid=-5), dict(pid=True), dict(pid="4242"), dict(nonce="n1"),
               dict(nonce=N1.upper()), dict(version=2), dict(expires_at=self.now + 10**6),
               dict(created_at=self.now - 10**6), dict(expires_at="1e999")]
        for i, override in enumerate(bad):
            with self.subTest(override=override):
                self.gate.mkdir(parents=True, exist_ok=True)
                path = self.gate / ("intent-%d.json" % i)
                path.write_text(json.dumps({**good, **override}))  # allow_nan: writes NaN/Infinity
                self.assertEqual(c.live_intents(self.gate, self.now, alive=lambda p: True),
                                 [dict(pid=None, nonce=None, expires_at=None)])
                # bounded: discarded once older than one TTL by mtime
                self.assertEqual(c.live_intents(self.gate, time.time() + c.INTENT_TTL_SECONDS + 1,
                                                alive=lambda p: True), [])
                self.assertFalse(path.exists())
        # raw JSON spellings the decoder turns into non-finite floats
        for i, raw in enumerate(('NaN', 'Infinity', '1e999')):
            with self.subTest(raw=raw):
                path = self.gate / ("intent-raw-%d.json" % i)
                path.write_text('{"version": 1, "pid": 4242, "nonce": "%s", "created_at": %s, "expires_at": %s}'
                                % (N1, self.now, raw))
                self.assertEqual(len(c.live_intents(self.gate, self.now, alive=lambda p: True)), 1)
                c.live_intents(self.gate, time.time() + c.INTENT_TTL_SECONDS + 1, alive=lambda p: True)
                self.assertFalse(path.exists())

    def test_huge_integer_timestamp_is_malformed_and_eventually_deleted(self):
        self.gate.mkdir(parents=True, exist_ok=True)
        for field in ("created_at", "expires_at"):
            with self.subTest(field=field):
                path = self.gate / ("intent-huge-%s.json" % field)
                good = dict(version=1, pid=4242, nonce=N1, created_at=self.now, expires_at=self.now + 300)
                path.write_text(json.dumps({**good, field: 10**400}))
                with self.assertRaises(ValueError):
                    c._valid_intent(json.loads(path.read_text()), self.now)
                self.assertEqual(c.live_intents(self.gate, self.now, alive=lambda p: True),
                                 [dict(pid=None, nonce=None, expires_at=None)])
                self.assertEqual(c.live_intents(self.gate, time.time() + c.INTENT_TTL_SECONDS + 1,
                                                alive=lambda p: True), [])
                self.assertFalse(path.exists())

    def test_malformed_intent_fails_closed_until_ttl(self):
        self.gate.mkdir(parents=True)
        (self.gate / "intent-1-x.json").write_text("{bad")
        self.assertEqual(len(c.live_intents(self.gate, time.time())), 1)
        self.assertEqual(c.live_intents(self.gate, time.time() + c.INTENT_TTL_SECONDS + 1), [])

    def gate_ctx(self, **kw):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            kw.pop("on_sleep", lambda: None)()
        self.runner.clock = lambda: clock[0]  # daemon and trainer share one clock
        args = dict(gate=self.gate, status_path=self.status, api=self.api,
                    clock=lambda: clock[0], sleep=sleep, log=self.logs.append, pid=os.getpid())
        args.update({k: v for k, v in kw.items() if k != "on_sleep"})
        return c.training_gate(**args), clock

    def test_trainer_waits_for_daemon_ack(self):
        def daemon_tick():
            self.runner.maybe_loaded = None
            self.runner.alive = lambda pid: True
            self.runner.tick()
        ctx, clock = self.gate_ctx(on_sleep=daemon_tick)
        with ctx as intent_path:
            self.assertFalse(self.loaded)
            self.assertTrue(intent_path.exists())
        self.assertIn("daemon acked", self.logs[-1])
        self.assertLess(clock[0], c.ACK_WAIT_SECONDS)
        self.assertFalse(list(self.gate.iterdir()))  # intent + ack removed

    def test_daemon_dead_model_resident_trainer_unloads(self):
        # A daemon killed right after a renewal leaves the model resident for a
        # lease: stale status must not let training start beside it.
        self.loaded = True
        ctx, clock = self.gate_ctx()
        with ctx:
            self.assertFalse(self.loaded)
        self.assertGreaterEqual(clock[0], c.ACK_WAIT_SECONDS)
        self.assertTrue(any("not running" in line for line in self.logs))
        self.assertIn("confirmed", self.logs[-1])
        self.assertFalse(list(self.gate.iterdir()))

    def test_daemon_dead_unload_unconfirmed_refuses(self):
        def stuck(path, payload=None):
            return {"models": [{"name": c.MODEL}]} if path == "/api/ps" else {}
        ctx, _ = self.gate_ctx(api=stuck)
        with self.assertRaises(c.GateRefused):
            with ctx:
                self.fail("training must not start")
        self.assertFalse(list(self.gate.iterdir()))

    def test_ollama_not_running_proceeds_with_log(self):
        def refused(path, payload=None):
            raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
        ctx, _ = self.gate_ctx(api=refused)
        with ctx:
            pass
        self.assertIn("connection refused", self.logs[-1])

    def test_ollama_error_refuses(self):
        def broken(path, payload=None):
            raise urllib.error.URLError(TimeoutError("timed out"))
        ctx, _ = self.gate_ctx(api=broken)
        with self.assertRaises(c.GateRefused):
            with ctx:
                self.fail("training must not start")
        self.assertFalse(list(self.gate.iterdir()))

    def test_live_daemon_without_ack_trainer_unloads_itself(self):
        c.write_status(self.status, dict(version=1, sampled_at=0, expires_at=10**9, reason="hold"))
        ctx, _ = self.gate_ctx()
        with ctx:
            self.assertFalse(self.loaded)
        self.assertIn(("/api/generate", {"model": c.MODEL, "stream": False, "keep_alive": 0}), self.calls)
        self.assertIn("confirmed", self.logs[-1])

    def test_live_daemon_unload_unconfirmed_refuses(self):
        c.write_status(self.status, dict(version=1, sampled_at=0, expires_at=10**9, reason="hold"))
        def stuck(path, payload=None):
            return {"models": [{"name": c.MODEL}]} if path == "/api/ps" else {}
        ctx, _ = self.gate_ctx(api=stuck)
        with self.assertRaises(RuntimeError):
            with ctx:
                self.fail("training must not start")
        self.assertFalse(list(self.gate.iterdir()))

    def test_crash_in_block_removes_intent(self):
        ctx, _ = self.gate_ctx()
        with self.assertRaises(ValueError):
            with ctx:
                raise ValueError("trainer crashed")
        self.assertFalse(list(self.gate.iterdir()))

    def test_intent_refreshed_while_held(self):
        ctx, _ = self.gate_ctx(refresh_seconds=0.01)
        with ctx as intent_path:
            first = intent_path.stat().st_mtime_ns
            time.sleep(0.1)
            self.assertTrue(intent_path.exists())
        self.assertFalse(intent_path.exists())


class OllamaEndpointTests(unittest.TestCase):
    def test_no_env_var_redirects_the_residency_check(self):
        seen = []

        class Opener:
            def open(self, request, timeout):
                seen.append(request.full_url)
                raise urllib.error.URLError(ConnectionRefusedError())

        env = {"CORTADOS_STUDIO_OLLAMA_PORT": "1", "OLLAMA_HOST": "127.0.0.1:1", "OLLAMA_PORT": "1",
               "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
               "CORTADOS_STUDIO_OLLAMA_URL": "http://127.0.0.1:1"}
        with patch.dict(os.environ, env), patch.object(c.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(urllib.error.URLError):
                c.ollama("/api/ps")
        self.assertEqual(seen, ["http://127.0.0.1:11434/api/ps"])
        self.assertFalse(hasattr(c, "OLLAMA_PORT_ENV"))


class AckWaitConfigTests(unittest.TestCase):
    def test_valid_values(self):
        self.assertEqual(c.ack_wait_from_env(None), c.ACK_WAIT_SECONDS)
        self.assertEqual(c.ack_wait_from_env(""), c.ACK_WAIT_SECONDS)
        self.assertEqual(c.ack_wait_from_env("0"), 0.0)
        self.assertEqual(c.ack_wait_from_env(str(c.ACK_WAIT_SECONDS)), c.ACK_WAIT_SECONDS)

    def test_invalid_values_refuse_before_running(self):
        for raw in ("nan", "NaN", "inf", "-inf", "Infinity", "-1", "-0.5", str(c.ACK_WAIT_SECONDS + 1), "1e9",
                    "abc", "5s", " "):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    c.ack_wait_from_env(raw)
                with patch.dict(os.environ, {c.ACK_WAIT_ENV: raw}), \
                        patch.object(c, "training_gate") as gate, patch.object(c.subprocess, "call") as call:
                    self.assertEqual(c.gate_main(["--", "true"]), c.GATE_REFUSED_EXIT)
                gate.assert_not_called()
                call.assert_not_called()
