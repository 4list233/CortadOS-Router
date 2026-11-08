import fcntl
import json
import os
import signal
import sys
import stat
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
        self.assertEqual(self.leftovers(), [])

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

    def leftovers(self):
        """Handshake files left in the gate dir; node.lock is permanent by design."""
        return sorted(p.name for p in self.gate.iterdir() if p.name != c.NODE_LOCK_NAME) if self.gate.exists() else []

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
        self.assertTrue(any("daemon acked" in line for line in self.logs))
        self.assertIn("confirmed no model resident", self.logs[-1])
        self.assertLess(clock[0], c.ACK_WAIT_SECONDS)
        self.assertEqual(self.leftovers(), [])  # intent + ack removed

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
        self.assertEqual(self.leftovers(), [])

    def test_daemon_dead_unload_unconfirmed_refuses(self):
        def stuck(path, payload=None):
            return {"models": [{"name": c.MODEL}]} if path == "/api/ps" else {}
        ctx, _ = self.gate_ctx(api=stuck)
        with self.assertRaises(c.GateRefused):
            with ctx:
                self.fail("training must not start")
        self.assertEqual(self.leftovers(), [])

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
        self.assertEqual(self.leftovers(), [])

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
        self.assertEqual(self.leftovers(), [])

    def test_fallback_unloads_every_loaded_model(self):
        resident = {c.MODEL, "huihui-other:latest", "third:q4"}
        def api(path, payload=None):
            self.calls.append((path, payload))
            if path == "/api/ps":
                return {"models": [{"name": n} for n in sorted(resident)]}
            if payload["keep_alive"] == 0:
                resident.discard(payload["model"])
            return {}
        ctx, _ = self.gate_ctx(api=api)
        with ctx:
            self.assertEqual(resident, set())
        unloaded = {p["model"] for path, p in self.calls if path == "/api/generate"}
        self.assertEqual(unloaded, {c.MODEL, "huihui-other:latest", "third:q4"})
        self.assertIn("confirmed", self.logs[-1])

    def test_fallback_refuses_if_any_model_remains(self):
        resident = {c.MODEL, "stuck:latest"}
        def api(path, payload=None):
            if path == "/api/ps":
                return {"models": [{"name": n} for n in sorted(resident)]}
            if payload["model"] != "stuck:latest":
                resident.discard(payload["model"])
            return {}
        ctx, _ = self.gate_ctx(api=api)
        with self.assertRaises(c.GateRefused) as cm:
            with ctx:
                self.fail("training must not start")
        self.assertIn("stuck:latest", str(cm.exception))
        self.assertEqual(self.leftovers(), [])

    def ack_on_sleep(self):
        """Fake daemon: acks every live intent at the first poll, unloading only its own model."""
        def tick():
            for intent in c.live_intents(self.gate, 0.0, alive=lambda p: True):
                c.write_ack(self.gate, intent, c.MODEL, 0.0)
        return tick

    def multi_api(self, resident, stuck=()):
        def api(path, payload=None):
            self.calls.append((path, payload))
            if path == "/api/ps":
                return {"models": [{"name": n} for n in sorted(resident)]}
            if payload["keep_alive"] == 0 and payload["model"] not in stuck:
                resident.discard(payload["model"])
            return {}
        return api

    def test_after_ack_every_other_model_is_unloaded(self):
        # The daemon only unloads its own model; another pinned model must not stay beside training.
        resident = {"huihui-9b:latest", "third:q4"}
        ctx, _ = self.gate_ctx(api=self.multi_api(resident), on_sleep=self.ack_on_sleep())
        with ctx:
            self.assertEqual(resident, set())
        self.assertTrue(any("daemon acked" in line for line in self.logs))
        self.assertEqual({p["model"] for path, p in self.calls if path == "/api/generate"},
                         {"huihui-9b:latest", "third:q4"})

    def test_after_ack_survivor_refuses(self):
        resident = {"huihui-9b:latest", "stuck:latest"}
        ctx, _ = self.gate_ctx(api=self.multi_api(resident, stuck={"stuck:latest"}), on_sleep=self.ack_on_sleep())
        with self.assertRaises(c.GateRefused) as cm:
            with ctx:
                self.fail("training must not start")
        self.assertIn("stuck:latest", str(cm.exception))
        self.assertEqual(self.leftovers(), [])

    def test_malformed_ps_reply_refuses(self):
        for reply in ({"models": {}}, {"models": ""}, {}, {"models": [{"size": 1}]}, {"models": [{"name": ""}]},
                      {"models": ["x"]}, []):
            with self.subTest(reply=reply):
                ctx, _ = self.gate_ctx(api=lambda path, payload=None, r=reply: r, wait_seconds=0)
                with self.assertRaises(c.GateRefused):
                    with ctx:
                        self.fail("training must not start")
                self.assertEqual(self.leftovers(), [])

    # Node lock (local_gate <-> training_gate), deterministic: injected clock and sleep.

    def holder(self, kind=fcntl.LOCK_SH):
        fd = c.open_node_lock(self.gate)
        fcntl.flock(fd, kind | fcntl.LOCK_NB)
        return fd

    def exclusive_free(self):
        fd = c.open_node_lock(self.gate)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def test_node_lock_path_shared_and_private(self):
        self.assertEqual(c.node_lock_path(self.gate), self.gate / "node.lock")
        os.close(c.open_node_lock(self.gate))
        self.assertEqual(stat.S_IMODE((self.gate / "node.lock").stat().st_mode), 0o600)

    def test_trainer_waits_for_in_flight_request_then_proceeds(self):
        self.loaded = False
        fd = self.holder()
        def on_sleep():  # gate_ctx runs this at the first poll only: the request ends while the trainer waits
            os.close(fd)
        ctx, clock = self.gate_ctx(on_sleep=on_sleep, wait_seconds=0)
        with ctx as intent_path:
            self.assertTrue(intent_path.exists())
            self.assertFalse(self.exclusive_free())  # LOCK_EX held for the whole block
            with self.assertRaises(BlockingIOError):
                self.holder()                         # a gate request cannot take LOCK_SH
        self.assertEqual(clock[0], c.TRAINER_LOCK_POLL_SECONDS)  # waited exactly one poll
        self.assertIn("confirmed no model resident", self.logs[-1])
        self.assertTrue(self.exclusive_free())

    def test_trainer_times_out_on_in_flight_request(self):
        fd = self.holder()
        self.addCleanup(os.close, fd)
        ctx, clock = self.gate_ctx(lock_wait_seconds=5)
        with self.assertRaises(c.GateRefused) as cm:
            with ctx:
                self.fail("training must not start")
        self.assertIn("in flight", str(cm.exception))
        self.assertGreaterEqual(clock[0], 5)
        self.assertEqual(self.leftovers(), [])        # no intent written
        self.assertEqual(self.calls, [])               # no residency query, no unload attempted

    def test_lock_released_when_block_raises_or_gate_refuses(self):
        ctx, _ = self.gate_ctx()
        with self.assertRaises(ValueError):
            with ctx:
                raise ValueError("trainer crashed")
        self.assertTrue(self.exclusive_free())
        ctx, _ = self.gate_ctx(api=lambda path, payload=None: {"bad": 1}, wait_seconds=0)
        with self.assertRaises(c.GateRefused):
            with ctx:
                self.fail("training must not start")
        self.assertTrue(self.exclusive_free())

    def test_crash_in_block_removes_intent(self):
        ctx, _ = self.gate_ctx()
        with self.assertRaises(ValueError):
            with ctx:
                raise ValueError("trainer crashed")
        self.assertEqual(self.leftovers(), [])

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
                        patch.object(c, "training_gate") as gate, patch.object(c.subprocess, "Popen") as call:
                    self.assertEqual(c.gate_main(["--", "true"]), c.GATE_REFUSED_EXIT)
                gate.assert_not_called()
                call.assert_not_called()


class NodeLockSafetyTests(unittest.TestCase):
    """open_node_lock refuses anything but a private, real, owned dir and lock file (temp dirs only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gate = self.root / "studio-gate"

    def test_symlinked_lock_file_refused(self):
        os.close(c.open_node_lock(self.gate))
        (self.gate / "node.lock").unlink()
        os.symlink(self.root / "elsewhere", self.gate / "node.lock")
        with self.assertRaises(OSError):
            os.close(c.open_node_lock(self.gate))
        with self.assertRaises(c.GateRefused):
            os.close(c.acquire_trainer_lock(self.gate, sleep=lambda s: None, wait_seconds=0))
        self.assertFalse((self.root / "elsewhere").exists())

    def test_symlinked_dir_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        os.symlink(real, self.gate)
        with self.assertRaises(c.NodeLockUnsafe):
            c.open_node_lock(self.gate)
        with self.assertRaises(c.GateRefused):
            c.acquire_trainer_lock(self.gate, sleep=lambda s: None, wait_seconds=0)
        self.assertFalse((real / "node.lock").exists())

    def test_group_writable_dir_refused(self):
        self.gate.mkdir()
        os.chmod(self.gate, 0o770)
        with self.assertRaises(c.NodeLockUnsafe):
            c.open_node_lock(self.gate)
        with self.assertRaises(c.GateRefused):
            c.acquire_trainer_lock(self.gate, sleep=lambda s: None, wait_seconds=0)

    def test_trainer_created_dir_is_0700_under_umask_0002(self):
        old = os.umask(0o002)
        try:
            fd = c.acquire_trainer_lock(self.gate, sleep=lambda s: None, wait_seconds=0)
        finally:
            os.umask(old)
        os.close(fd)
        self.assertEqual(stat.S_IMODE(os.lstat(self.gate).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.gate / "node.lock").st_mode), 0o600)


# Subprocess harness: runs the real gate_main with training_gate bound to a temp gate dir and a fake, empty Ollama
# (never the real one: the real check would unload models on this machine).
HARNESS = r'''
import functools, sys
sys.path.insert(0, sys.argv[1])
from cortados_router import studio_capacity as c
c.training_gate = functools.partial(c.training_gate, gate=sys.argv[2], api=lambda path, payload=None: {"models": []},
                                    log=lambda m: None)
sys.exit(c.gate_main(["--"] + sys.argv[3:]))
'''


class GateMainProcessTests(unittest.TestCase):
    """Real processes: the training command keeps the node lock past its wrapper, and its group is stopped."""

    ROOT = str(Path(__file__).resolve().parents[1])

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gate = self.root / "studio-gate"
        self.marker = self.root / "started"

    def start(self, *cmd):
        env = dict(os.environ, CORTADOS_HOME=str(self.root), **{c.ACK_WAIT_ENV: "0"})
        proc = self.subprocess.Popen([sys.executable, "-c", HARNESS, self.ROOT, str(self.gate)] + list(cmd), env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def wait_for(self, cond, seconds=10):
        deadline = time.monotonic() + seconds
        while not cond():
            if time.monotonic() > deadline:
                self.fail("condition not reached in %ss" % seconds)
            time.sleep(0.02)

    def shared_free(self):
        fd = c.open_node_lock(self.gate)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def pid_from(self, path):
        self.wait_for(lambda: path.exists() and path.read_text().strip())
        return int(path.read_text())

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_sigkilled_wrapper_lock_held_until_child_exits(self):
        script = ("import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
                  "time.sleep(0.8)")
        proc = self.start(sys.executable, "-c", script, str(self.marker))
        child = self.pid_from(self.marker)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(5)
        self.assertTrue(self.alive(child))
        self.assertFalse(self.shared_free())          # the training child still holds LOCK_EX
        self.wait_for(lambda: not self.alive(child))  # orphaned child, reaped by init
        self.wait_for(self.shared_free)

    def test_sigterm_stops_whole_group_then_releases(self):
        grand = self.root / "grand"
        grand_script = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)"
        shell = '"%s" -c "%s" "%s" & echo $$ > "%s"; wait' % (sys.executable, grand_script, grand, self.marker)
        proc = self.start("/bin/sh", "-c", shell)
        sh_pid = self.pid_from(self.marker)
        grand_pid = self.pid_from(grand)
        self.assertFalse(self.shared_free())
        os.kill(proc.pid, signal.SIGTERM)
        self.assertEqual(proc.wait(10), 128 + signal.SIGTERM)
        self.assertFalse(self.alive(sh_pid))
        self.wait_for(lambda: not self.alive(grand_pid), 5)
        self.assertTrue(self.shared_free())

    def test_normal_completion_releases(self):
        proc = self.start(sys.executable, "-c", "import sys; sys.exit(3)")
        self.assertEqual(proc.wait(10), 3)
        self.assertTrue(self.shared_free())
