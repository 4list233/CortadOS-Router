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
            if payload and payload["keep_alive"] == -1:
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
            if payload:
                self.now += 20
            return original(path, payload)
        self.runner.api = slow
        self.runner.tick()
        self.assertFalse(self.advance(120)["available"])

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
