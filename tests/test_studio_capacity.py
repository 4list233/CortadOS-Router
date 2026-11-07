import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortados_router import studio_capacity as c


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
