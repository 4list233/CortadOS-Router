import contextlib
import http.client
import io
import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from cortados_router import hardware

# Real capture (M5 Max, 2026-11-03), trimmed of the tag-storage tail; includes the quoted key.
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    10000.
Pages active:                                1203204.
Pages inactive:                                20000.
Pages speculative:                              2000.
Pages throttled:                                   0.
Pages wired down:                             285683.
Pages purgeable:                               75106.
"Translation faults":                     1349400432.
Pages copy-on-write:                       123363742.
Pages zero filled:                        1098692836.
Pages reactivated:                          13612137.
Pages purged:                                4914075.
File-backed pages:                            875735.
Anonymous pages:                             1532644.
Pages stored in compressor:                   687935.
Pages occupied by compressor:                 341083.
Decompressions:                              8297807.
Compressions:                               12560345.
Pageins:                                    31864759.
Pageouts:                                      40532.
Swapins:                                      171787.
Swapouts:                                     494031.
"""
TOP = """Processes: 600 total
CPU usage: 3.0% user, 2.0% sys, 95.0% idle
Processes: 600 total
CPU usage: 20.5% user, 9.5% sys, 70.0% idle
"""


def ioreg_plist(*utilizations):
    """`ioreg -a -r -c IOAccelerator -d 1` shape: the real M5 Max key set (2026-11-03), one entry per accelerator."""
    devices = []
    for pct in utilizations:
        devices.append({
            "IOClass": "AGXAcceleratorG16X", "IOObjectClass": "AGXAcceleratorG16X",
            "IORegistryEntryName": "AGXAcceleratorG16X", "IORegistryEntryID": 4294968371,
            "CFBundleIdentifier": "com.apple.AGXG16X", "MetalPluginName": "AGXMetalG16X",
            "IOReportLegend": [{"IOReportGroupName": "GPU Stats", "IOReportChannels": [[1, 2, "x" * 400]]}],
            "AGCInfo": {"fLastSubmissionPID": 512, "fSubmissionsSinceLastCheck": 0},
            "PerformanceStatistics": {
                "Alloc system memory": 6213484544, "Allocated PB Size": 274726912,
                "Device Utilization %": pct, "In use system memory": 1379794944,
                "In use system memory (driver)": 0, "Renderer Utilization %": pct, "SplitSceneCount": 0,
                "TiledSceneBytes": 557056, "Tiler Utilization %": pct, "lastRecoveryTime": 0, "recoveryCount": 0},
        })
    return plistlib.dumps(devices).decode()


IOREG = ioreg_plist(37)
OLLAMA = json.dumps({"models": [{"name": "qwen3:8b", "model": "qwen3:8b", "size": 6000000000, "size_vram": 6000000000}]})

OUTPUTS = {
    (hardware.SYSCTL, "-n", "machdep.cpu.brand_string"): "Apple M5 Max\n",
    (hardware.SYSCTL, "-n", "hw.memsize"): str(64 * hardware.GIB) + "\n",
    (hardware.VM_STAT,): VM_STAT,
    (hardware.TOP, "-l", "2", "-n", "0", "-s", "1"): TOP,
    hardware.IOREG_ARGV: IOREG,
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
        self.assertEqual(s["sampled_at"], 1.0)

    def test_http_protocol_error_is_a_reading_failure(self):
        def junk(url, timeout=None):
            raise http.client.BadStatusLine("junk")
        s = self.take(fetch=junk)
        self.assertFalse(s["ollama_models"]["available"])
        self.assertIn("BadStatusLine", s["ollama_models"]["reason"])

    def test_ollama_entry_without_name_fails_reading(self):
        s = self.take(fetch=lambda url, timeout=None: json.dumps({"models": [{"size": 1}, "junk"]}))
        self.assertFalse(s["ollama_models"]["available"])
        self.assertIn("no name", s["ollama_models"]["reason"])

    def test_reason_is_capped(self):
        s = self.take(fetch=lambda url, timeout=None: (_ for _ in ()).throw(ValueError("x" * 1000)))
        self.assertEqual(len(s["ollama_models"]["reason"]), hardware.REASON_CHARS)

    def test_ollama_down_is_unavailable_not_empty(self):
        s = self.take(fetch=down_fetch)
        self.assertFalse(s["ollama_models"]["available"])
        self.assertIn("refused", s["ollama_models"]["reason"])
        self.assertEqual([e["field"] for e in s["errors"]], ["ollama_models"])

    def test_timeout_and_missing_gpu(self):
        run = fake_run({(hardware.TOP, "-l", "2", "-n", "0", "-s", "1"): subprocess.TimeoutExpired("top", 4),
                        hardware.IOREG_ARGV: plistlib.dumps([{"IOClass": "x"}]).decode()})
        s = self.take(run=run)
        self.assertFalse(s["cpu_busy_pct"]["available"])
        self.assertIn("TimeoutExpired", s["cpu_busy_pct"]["reason"])
        self.assertFalse(s["gpu_busy_pct"]["available"])
        self.assertIsNone(s["headroom"]["cpu_idle_pct"])
        self.assertIsNone(s["headroom"]["gpu_idle_pct"])
        self.assertEqual({e["field"] for e in s["errors"]}, {"cpu_busy_pct", "gpu_busy_pct"})

    def test_missing_binary_and_bad_vm_stat(self):
        run = fake_run({(hardware.SYSCTL, "-n", "hw.memsize"): FileNotFoundError("sysctl"),
                        (hardware.VM_STAT,): "garbage\n"})
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
        self.assertFalse(s["chip"]["available"])  # never the host's own processor string
        json.dumps(s)


class ParserTest(unittest.TestCase):
    def test_vm_stat_needs_page_size(self):
        with self.assertRaises(ValueError):
            hardware.parse_vm_stat("Pages free: 1.\n")

    def test_ioreg_busiest_accelerator(self):
        self.assertEqual(hardware.parse_ioreg_gpu(ioreg_plist(5, 80)), 80)

    def test_ioreg_long_plist_parses(self):
        self.assertGreater(len(IOREG), 1000)  # wider than any terminal: width never matters for -a
        self.assertEqual(hardware.parse_ioreg_gpu(IOREG), 37)

    def test_ioreg_malformed_plist_is_a_reading_failure(self):
        bad = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><array><dict>'
               '<key>IORegistryEntryName</key><string>AGX\x01</string></dict></array></plist>')
        with self.assertRaises(ValueError):
            hardware.parse_ioreg_gpu(bad)
        base = fake_run()

        def run(argv, timeout=None):
            return bad if argv[0] == hardware.IOREG else base(argv)
        data = hardware.sample(run=run, fetch=ok_fetch, system="Darwin", now=1.0, hostname="box")
        self.assertIs(data["gpu_busy_pct"]["available"], False)
        self.assertIn("gpu_busy_pct", [e["field"] for e in data["errors"]])

    def test_ncpu_none_is_unavailable_with_reason(self):
        with mock.patch.object(hardware.os, "cpu_count", return_value=None):
            data = hardware.sample(run=fake_run(), fetch=ok_fetch, system="Darwin", now=1.0, hostname="box")
        self.assertIs(data["ncpu"]["available"], False)
        self.assertIn("ncpu", [e["field"] for e in data["errors"]])

    def test_ioreg_text_form_is_rejected(self):
        with self.assertRaises(ValueError):
            hardware.parse_ioreg_gpu('+-o AGX\n  "PerformanceStatistics" = {"Device Utilization %"=37}\n')


class RunnerTest(unittest.TestCase):
    def test_default_run_passes_timeout_and_check(self):
        with mock.patch.object(hardware.subprocess, "run") as run:
            run.return_value.stdout = "ok"
            self.assertEqual(hardware.default_run(["x"]), "ok")
        self.assertEqual(run.call_args.kwargs["timeout"], hardware.CMD_TIMEOUT)
        self.assertTrue(run.call_args.kwargs["check"])

    def test_default_run_times_out(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            hardware.default_run(["/bin/sh", "-c", "sleep 5"], timeout=0.2)

    def test_default_fetch_timeout_and_size_cap(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with opener_returning(Resp(b"{}")) as build:
            self.assertEqual(hardware.default_fetch("http://x"), "{}")
        self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], hardware.OLLAMA_TIMEOUT)
        self.assertIs(build.call_args.args[0], hardware._NoRedirect)
        big = Resp(b"a" * (hardware.MAX_OLLAMA_BYTES + 10))
        with opener_returning(big):
            with self.assertRaises(ValueError):
                hardware.default_fetch("http://x")

    def test_default_fetch_wall_clock_budget(self):
        class Drip:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"a"  # one byte per read, forever: only the deadline stops it
        clock = iter(range(0, 1000))
        with opener_returning(Drip()), \
                mock.patch.object(hardware.time, "monotonic", side_effect=lambda: float(next(clock))):
            with self.assertRaises(TimeoutError):
                hardware.default_fetch("http://x")

    def test_redirect_is_refused(self):
        handler = hardware._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere/"))

    def test_default_fetch_checks_budget_after_a_blocking_read(self):
        class Slow:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b"a"  # the clock below jumps past the budget during this one read
        clock = iter([0.0, 0.0, hardware.FETCH_BUDGET + 1])
        with opener_returning(Slow()), mock.patch.object(hardware.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(TimeoutError):
                hardware.default_fetch("http://x")


def opener_returning(response):
    opener = mock.Mock()
    opener.open.return_value = response
    return mock.patch.object(hardware.urllib.request, "build_opener", return_value=opener)


@contextlib.contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class WriteTest(unittest.TestCase):
    def test_atomic_write_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hardware", "box.json")
            hardware.write_atomic(path, {"a": 1})
            with open(path) as handle:
                self.assertEqual(json.load(handle), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["box.json"])

    def test_existing_parent_keeps_its_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, "shared")
            os.mkdir(shared)
            os.chmod(shared, 0o755)
            hardware.write_atomic(os.path.join(shared, "hw.json"), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(shared).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(shared, "hw.json")).st_mode), 0o600)

    def test_every_created_level_is_0700(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "fresh-home")
            before = stat.S_IMODE(os.stat(tmp).st_mode)
            hardware.write_atomic(os.path.join(home, "hardware", "box.json"), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(tmp).st_mode), before)

    def test_concurrent_mkdir_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "hardware")
            real_mkdir = os.mkdir

            def racing_mkdir(path, mode=0o777):
                real_mkdir(path, 0o755)  # another sampler wins the race
                raise FileExistsError(path)
            with mock.patch.object(hardware.os, "mkdir", racing_mkdir):
                hardware.write_atomic(os.path.join(target, "box.json"), {"a": 1})
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o755)  # not ours: mode untouched

    def test_owned_check_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp, CORTADOS_HERMES_HOME=None):
            real = os.path.join(tmp, "real")
            os.mkdir(real)
            os.symlink(real, os.path.join(tmp, "hardware"))
            with self.assertRaises(PermissionError):
                hardware.write_atomic(os.path.join(tmp, "hardware", "box.json"), {}, require_owned=True)

    def test_owned_check_refuses_symlinked_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = os.path.join(tmp, "elsewhere")
            os.mkdir(elsewhere)
            home = os.path.join(tmp, "home")
            os.symlink(elsewhere, home)
            with env(CORTADOS_HOME=home, CORTADOS_HERMES_HOME=None):
                with self.assertRaises(PermissionError):
                    hardware.write_atomic(hardware.default_out_path("box"), {}, require_owned=True)
            self.assertEqual(os.listdir(elsewhere), [])  # nothing created inside the link target

    def test_owned_check_refuses_foreign_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp, CORTADOS_HERMES_HOME=None):
            with mock.patch.object(hardware.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(PermissionError):
                    hardware.write_atomic(hardware.default_out_path("box"), {}, require_owned=True)
            self.assertFalse(os.path.exists(os.path.join(tmp, "hardware")))

    def test_owned_check_refuses_path_outside_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=os.path.join(tmp, "home"), CORTADOS_HERMES_HOME=None):
            with self.assertRaises(PermissionError):
                hardware.write_atomic(os.path.join(tmp, "other", "box.json"), {}, require_owned=True)

    def test_owned_check_refuses_dotdot_escape(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=os.path.join(tmp, "home"), CORTADOS_HERMES_HOME=None):
            target = os.path.join(tmp, "home", "hardware", "..", "..", "escaped.json")
            with self.assertRaises(PermissionError):
                hardware.write_atomic(target, {}, require_owned=True)
            self.assertFalse(os.path.lexists(os.path.join(tmp, "escaped.json")))
            self.assertFalse(os.path.lexists(os.path.join(tmp, "home")))

    def test_owned_check_refuses_group_writable_level(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp, CORTADOS_HERMES_HOME=None):
            os.chmod(tmp, 0o770)
            with self.assertRaises(PermissionError):
                hardware.write_atomic(os.path.join(tmp, "hardware", "box.json"), {}, require_owned=True)
            self.assertFalse(os.path.lexists(os.path.join(tmp, "hardware")))

    def test_hostile_host_stays_in_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp, CORTADOS_HERMES_HOME=None):
            for host in ("../../../../tmp/x", "..", ".hidden", "a/b", "", None):
                path = hardware.default_out_path(host)
                self.assertEqual(str(path.parent), os.path.join(tmp, "hardware"), host)
                self.assertRegex(path.name, r"^[A-Za-z0-9_-]+\.json$")
            path = hardware.default_out_path("../../x")
            hardware.write_atomic(path, {}, require_owned=True)
            self.assertTrue(path.exists())

    def test_hostname_fqdn_is_trimmed(self):
        with mock.patch.object(hardware.socket, "gethostname", return_value="box.tail1234.ts.net"):
            out = hardware.sample(run=fake_run(), fetch=ok_fetch, system="Darwin", now=1.0)
        self.assertEqual(out["host"], "box")

    def test_missing_getloadavg_is_unavailable(self):
        with mock.patch.object(hardware.os, "getloadavg", side_effect=AttributeError("no getloadavg")):
            out = hardware.sample(run=fake_run(), fetch=ok_fetch, system="Windows", now=1.0, hostname="box")
        self.assertFalse(out["load_avg"]["available"])

    def test_default_path_uses_cortados_home(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp, CORTADOS_HERMES_HOME=None):
            self.assertEqual(str(hardware.default_out_path("box")), os.path.join(tmp, "hardware", "box.json"))

    def test_default_path_hermes_home_only(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=None, CORTADOS_HERMES_HOME=tmp,
                                                       WANGCAI_HOME=None, WANGCAI_HERMES_HOME=None):
            self.assertEqual(str(hardware.default_out_path("box")), os.path.join(tmp, "hardware", "box.json"))


class MainTest(unittest.TestCase):
    def run_main(self, argv):
        real = hardware.sample
        fake = lambda: real(run=fake_run(), fetch=ok_fetch, system="Darwin", now=1.0, hostname="box")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(hardware, "sample", fake), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hardware.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_out_without_path_writes_default(self):
        with tempfile.TemporaryDirectory() as tmp, env(CORTADOS_HOME=tmp):
            code, out, _ = self.run_main(["sample", "--out"])
            path = os.path.join(tmp, "hardware", "box.json")
            self.assertEqual(code, 0)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with open(path) as handle:
                self.assertEqual(json.load(handle), json.loads(out))

    def test_write_failure_still_prints_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            open(blocker, "w").close()
            code, out, err = self.run_main(["sample", "--out", os.path.join(blocker, "x.json")])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["host"], "box")
            self.assertIn("could not write", err)

    def test_empty_out_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.run_main(["sample", "--out", ""])

if __name__ == "__main__":
    unittest.main()
