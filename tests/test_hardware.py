import json
import os
import stat
import subprocess
import tempfile
import unittest

from cortados_router import hardware

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            500000.
Pages inactive:                           20000.
Pages speculative:                         2000.
"""
TOP = """Processes: 600 total
CPU usage: 3.0% user, 2.0% sys, 95.0% idle
Processes: 600 total
CPU usage: 20.5% user, 9.5% sys, 70.0% idle
"""
IOREG = '+-o AGXAcceleratorG16X  <class AGXAccelerator>\n  "PerformanceStatistics" = {"Device Utilization %"=37,"Renderer Utilization %"=30}\n'
OLLAMA = json.dumps({"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "size": 6000000000, "size_vram": 6000000000}]})

OUTPUTS = {
    ("sysctl", "-n", "machdep.cpu.brand_string"): "Apple M5 Max\n",
    ("sysctl", "-n", "hw.memsize"): str(64 * hardware.GIB) + "\n",
    ("vm_stat",): VM_STAT,
    ("top", "-l", "2", "-n", "0", "-s", "1"): TOP,
    ("ioreg", "-r", "-d", "1", "-c", "IOAccelerator"): IOREG,
}


def fake_run(overrides=None):
    table = dict(OUTPUTS)
    table.update(overrides or {})

    def run(argv, timeout=hardware.CMD_TIMEOUT):
        value = table[tuple(argv)]
        if isinstance(value, BaseException):
            raise value
        return value
    return run


def ok_fetch(url, timeout=None):
    return OLLAMA


def down_fetch(url, timeout=None):
    raise ConnectionRefusedError(61, "Connection refused")


class SampleTest(unittest.TestCase):
    def take(self, run=None, fetch=ok_fetch, system="Darwin"):
        return hardware.sample(run=run or fake_run(), fetch=fetch, system=system, now=1.0, hostname="box")

    def test_full_darwin_sample(self):
        s = self.take()
        self.assertEqual(s["chip"], "Apple M5 Max")
        self.assertEqual(s["mem_total_bytes"], 64 * hardware.GIB)
        self.assertEqual(s["mem_available_bytes"], 32000 * 16384)
        self.assertEqual(s["cpu_busy_pct"], 30.0)  # second sample, not the since-boot first
        self.assertEqual(s["gpu_busy_pct"], 37)
        self.assertEqual(s["ollama_models"], [{"name": "qwen3:8b", "size": 6000000000, "size_vram": 6000000000}])
        self.assertEqual(s["headroom"], {"free_mem_gb": round(32000 * 16384 / hardware.GIB, 2),
                                         "cpu_idle_pct": 70.0, "gpu_idle_pct": 63.0})
        self.assertEqual(s["errors"], [])
        self.assertEqual(s["host"], "box")

    def test_ollama_down_is_unavailable_not_empty(self):
        s = self.take(fetch=down_fetch)
        self.assertFalse(s["ollama_models"]["available"])
        self.assertIn("refused", s["ollama_models"]["reason"])
        self.assertEqual([e["field"] for e in s["errors"]], ["ollama_models"])

    def test_timeout_and_missing_gpu(self):
        run = fake_run({("top", "-l", "2", "-n", "0", "-s", "1"): subprocess.TimeoutExpired("top", 4),
                        ("ioreg", "-r", "-d", "1", "-c", "IOAccelerator"): "+-o nothing\n"})
        s = self.take(run=run)
        self.assertFalse(s["cpu_busy_pct"]["available"])
        self.assertIn("TimeoutExpired", s["cpu_busy_pct"]["reason"])
        self.assertFalse(s["gpu_busy_pct"]["available"])
        self.assertIsNone(s["headroom"]["cpu_idle_pct"])
        self.assertIsNone(s["headroom"]["gpu_idle_pct"])
        self.assertEqual({e["field"] for e in s["errors"]}, {"cpu_busy_pct", "gpu_busy_pct"})

    def test_missing_binary_and_bad_vm_stat(self):
        run = fake_run({("sysctl", "-n", "hw.memsize"): FileNotFoundError("sysctl"),
                        ("vm_stat",): "garbage\n"})
        s = self.take(run=run)
        self.assertFalse(s["mem_total_bytes"]["available"])
        self.assertFalse(s["mem_available_bytes"]["available"])
        self.assertIsNone(s["headroom"]["free_mem_gb"])

    def test_bad_ollama_json(self):
        s = self.take(fetch=lambda url, timeout=None: "not json")
        self.assertFalse(s["ollama_models"]["available"])

    def test_non_darwin_never_crashes(self):
        s = self.take(run=lambda argv, timeout=None: self.fail("no commands off macOS"), system="Linux")
        self.assertFalse(s["mem_total_bytes"]["available"])
        self.assertIn("Linux", s["gpu_busy_pct"]["reason"])
        self.assertIn("platform", [e["field"] for e in s["errors"]])
        json.dumps(s)


class ParserTest(unittest.TestCase):
    def test_vm_stat_needs_page_size(self):
        with self.assertRaises(ValueError):
            hardware.parse_vm_stat("Pages free: 1.\n")

    def test_ioreg_busiest_accelerator(self):
        self.assertEqual(hardware.parse_ioreg_gpu('"Device Utilization %"=5 "Device Utilization %"=80'), 80)


class WriteTest(unittest.TestCase):
    def test_atomic_write_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hardware", "box.json")
            hardware.write_atomic(path, {"a": 1})
            self.assertEqual(json.load(open(path)), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["box.json"])

    def test_default_path_uses_cortados_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("CORTADOS_HOME")
            os.environ["CORTADOS_HOME"] = tmp
            try:
                self.assertEqual(str(hardware.default_out_path("box")), os.path.join(tmp, "hardware", "box.json"))
            finally:
                if old is None:
                    del os.environ["CORTADOS_HOME"]
                else:
                    os.environ["CORTADOS_HOME"] = old


if __name__ == "__main__":
    unittest.main()
