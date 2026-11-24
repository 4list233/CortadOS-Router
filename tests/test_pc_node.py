"""The PC node (#145, option A): the local gate on Windows through its platform seam (gate_platform.py), the `strata`
backend (local_backend.py), the serving policy (node_policy.py), the serving toggle and the provider's handling of a
second node that is down. Everything Windows-specific runs here on macOS through the seam with fakes: no ctypes call,
no msvcrt, no PowerShell, nothing on the PC. In-thread fake Strata on loopback only."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import core, gate_platform, local_backend, local_gate, local_governor, local_resources, node_policy
from cortados_router.provider import local_lane, local_switch, routing
from tests.provider_fakes import setUpModule, tearDownModule  # noqa: F401  (FAKE_* reach the fake CLIs, #106)
from tests.test_provider_local import TAG
from tests.test_provider_local_auto import AutoLocalBase, body
from tests.test_provider_local_index import scored_route

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "b" * 64
SERVED = "Qwen3.6-35B-A3B"
USER_SID = "S-1-5-21-1000-2000-3000-1001"
OWNER_SESSION = {"id": 1, "user": "PC\\owner", "logon": 134_000_000_000_000_000}
EXAMPLE = ROOT / "tables" / "local-backend.strata.example.json"
PC_EXAMPLE = ROOT / "tables" / "resources.pc-node.example.json"


def strata_cfg(upstream="http://127.0.0.1:8080", **policy):
    raw = json.loads(EXAMPLE.read_text())
    raw["upstream"] = upstream
    if policy.pop("no_policy", False):
        raw.pop("node_policy")
    else:
        raw["node_policy"].update(policy)
    return local_backend.check(raw, "test")


# ---------------------------------------------------------------- the Windows seam
class FakeMsvcrt:
    LK_NBLCK = 2

    def __init__(self, fail=0):
        self.fail, self.calls = fail, []

    def locking(self, fd, mode, n):
        self.calls.append((mode, n))
        if self.fail:
            self.fail -= 1
            raise OSError(36, "locked")


class FakeWinApi:
    """gate_platform.WinApi's methods, answered from fields. An Exception value is raised (the call failed)."""

    def __init__(self, sessions=None, ac=1, memory=50, owner=USER_SID, aces=None, idle=0.0, quns=5, current=None):
        self.sessions = [dict(OWNER_SESSION)] if sessions is None else sessions
        self.ac, self.memory, self.owner, self.idle, self.quns = ac, memory, owner, idle, quns
        self.current = dict(OWNER_SESSION) if current is None else current
        self.aces = [(0, 0, USER_SID), (0, 0, gate_platform.SID_SYSTEM),
                     (0, 0, gate_platform.SID_ADMINISTRATORS)] if aces is None else aces
        self.calls = []

    def _get(self, name, value):
        self.calls.append(name)
        if isinstance(value, BaseException):
            raise value
        return value

    def file_security(self, path):
        if isinstance(self.owner, BaseException):
            return self._get("file_security", self.owner)
        return self._get("file_security", (self.owner, self.aces))

    def current_user_sid(self):
        return self._get("current_user_sid", USER_SID)

    def ac_line_status(self):
        return self._get("ac_line_status", self.ac)

    def memory_load_pct(self):
        return self._get("memory_load_pct", self.memory)

    def active_sessions(self):
        return self._get("active_sessions", self.sessions)

    def current_session(self):
        return self._get("current_session", self.current)

    def input_idle_s(self):
        return self._get("input_idle_s", self.idle)

    def notification_state(self):
        return self._get("notification_state", self.quns)


class LockSeamTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        self.fd = fd
        self.addCleanup(os.close, fd)

    def test_windows_lock_retries_until_the_holder_lets_go(self):
        m, slept = FakeMsvcrt(fail=3), []
        gate_platform.lock_file(self.fd, windows=True, msvcrt_mod=m, sleep=slept.append)
        self.assertEqual(m.calls, [(m.LK_NBLCK, 1)] * 4)          # non-blocking, one byte, retried
        self.assertEqual(slept, [gate_platform.LOCK_RETRY_S] * 3)

    def test_windows_lock_gives_up_after_the_wait_and_says_so(self):
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        m = FakeMsvcrt(fail=10 ** 6)
        with self.assertRaises(gate_platform.LockTimeout):
            gate_platform.lock_file(self.fd, windows=True, msvcrt_mod=m, clock=lambda: clock[0], sleep=sleep)
        self.assertGreaterEqual(clock[0], gate_platform.LOCK_WAIT_S)

    def test_posix_lock_is_flock_as_before(self):
        with patch.object(gate_platform.fcntl, "flock") as flock:
            gate_platform.lock_file(self.fd, windows=False)
        flock.assert_called_once_with(self.fd, gate_platform.fcntl.LOCK_EX)

    def test_windows_skips_the_directory_fsync(self):
        with patch.object(gate_platform.os, "open", side_effect=AssertionError("no os.open on a directory")):
            gate_platform.fsync_dir(tempfile.gettempdir(), windows=True)

    def test_a_reparse_point_counts_as_a_link(self):
        st = os.stat_result((0o100600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        self.assertFalse(gate_platform.is_link(st))

        class St:
            st_mode = 0o100600
            st_file_attributes = gate_platform.FILE_ATTRIBUTE_REPARSE_POINT
        self.assertTrue(gate_platform.is_link(St()))


class AclTests(unittest.TestCase):
    OK = [(0, 0, USER_SID), (0, 0, gate_platform.SID_SYSTEM), (0, 0, gate_platform.SID_ADMINISTRATORS)]

    def test_owner_system_and_administrators_only_is_accepted(self):
        self.assertIsNone(gate_platform.acl_problem(USER_SID, self.OK, USER_SID))
        self.assertIsNone(gate_platform.acl_problem(gate_platform.SID_ADMINISTRATORS, self.OK, USER_SID))

    def test_everyone_users_and_authenticated_users_are_refused(self):
        for sid in ("S-1-1-0", "S-1-5-32-545", "S-1-5-11", "S-1-5-21-9-9-9-1002"):
            why = gate_platform.acl_problem(USER_SID, [*self.OK, (0, 0, sid)], USER_SID)
            self.assertIn(sid, why)

    def test_null_dacl_foreign_owner_and_unread_ace_types_are_refused(self):
        self.assertIn("no DACL", gate_platform.acl_problem(USER_SID, None, USER_SID))
        self.assertIn("owned by", gate_platform.acl_problem("S-1-5-21-9-9-9-1002", self.OK, USER_SID))
        self.assertIn("type 5", gate_platform.acl_problem(USER_SID, [*self.OK, (5, 0, "")], USER_SID))

    def test_deny_and_inherit_only_aces_do_not_grant(self):
        aces = [*self.OK, (1, 0, "S-1-1-0"), (0, gate_platform.INHERIT_ONLY_ACE, "S-1-1-0")]
        self.assertIsNone(gate_platform.acl_problem(USER_SID, aces, USER_SID))

    def test_read_token_on_windows_reads_the_acl_not_the_mode(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "tok"
        p.write_text(TOKEN)
        os.chmod(p, 0o644)                                   # a mode that macOS would refuse: means nothing on NTFS
        with patch.object(gate_platform, "WINDOWS", True), patch.object(gate_platform, "WinApi", FakeWinApi):
            self.assertEqual(local_gate.read_token(p), TOKEN)
        bad = lambda: FakeWinApi(aces=[*self.OK, (0, 0, "S-1-1-0")])   # Everyone may read it
        with patch.object(gate_platform, "WINDOWS", True), patch.object(gate_platform, "WinApi", bad):
            with self.assertRaises(local_gate.GateConfigError) as cm:
                local_gate.read_token(p)
        self.assertIn("S-1-1-0", str(cm.exception))
        self.assertIn("icacls", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        broken = lambda: FakeWinApi(owner=OSError(5, "denied"))
        with patch.object(gate_platform, "WINDOWS", True), patch.object(gate_platform, "WinApi", broken):
            with self.assertRaises(local_gate.GateConfigError) as cm:
                local_gate.read_token(p)
        self.assertIn("unreadable", str(cm.exception))

    def test_read_token_on_macos_is_unchanged(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "tok"
        p.write_text(TOKEN)
        os.chmod(p, 0o644)
        with self.assertRaises(local_gate.GateConfigError) as cm:
            local_gate.read_token(p)
        self.assertIn("has mode 0644; group/other access must be none (chmod 600)", str(cm.exception))


class WindowsEpochTests(unittest.TestCase):
    """next_epoch with the module's platform flag set to Windows: msvcrt lock, no O_NOFOLLOW, no directory fsync."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "epoch")
        self.msvcrt = FakeMsvcrt()
        for p in (patch.object(gate_platform, "WINDOWS", True), patch.object(gate_platform, "NOFOLLOW", 0),
                  patch.dict(sys.modules, {"msvcrt": self.msvcrt})):
            p.start()
            self.addCleanup(p.stop)

    def test_epochs_increase_under_the_msvcrt_lock(self):
        with patch.object(gate_platform.fcntl, "flock", side_effect=AssertionError("no flock on Windows")):
            self.assertEqual([local_gate.next_epoch(self.path) for _ in range(3)], [1, 2, 3])
        self.assertEqual(len(self.msvcrt.calls), 3)

    def test_a_linked_epoch_or_lock_file_refuses_to_start(self):
        target = os.path.join(self.dir, "elsewhere")
        Path(target).write_text("7\n")
        os.symlink(target, self.path)
        with self.assertRaises(local_gate.GateConfigError):
            local_gate.next_epoch(self.path)
        os.unlink(self.path)
        os.unlink(self.path + ".lock")                       # the first call made it
        os.symlink(target, self.path + ".lock")
        with self.assertRaises(local_gate.GateConfigError) as cm:
            local_gate.next_epoch(self.path)
        self.assertIn("is a link", str(cm.exception))


class WindowsImportTests(unittest.TestCase):
    def test_the_gate_imports_without_fcntl_and_without_the_training_lock(self):
        code = r"""
import sys
import argparse, base64, binascii, concurrent.futures, contextlib, csv, dataclasses, hmac, http.client, http.server
import io, ipaddress, json, logging, math, pathlib, platform, plistlib, re, secrets, shlex, shutil, signal, socket
import stat, struct, subprocess, tempfile, threading, typing, unicodedata, urllib.parse, urllib.request, uuid, zlib
sys.platform = "win32"          # only modules imported after this see Windows: ours
sys.modules["fcntl"] = None     # import fcntl raises ImportError, as on Windows
import cortados_router.local_gate as g
import cortados_router.gate_platform as p, cortados_router.node_policy, cortados_router.local_backend
assert p.WINDOWS and p.fcntl is None and g.fcntl is None
assert g.open_node_lock is None and g.default_studio_gate_dir is None
assert "cortados_router.studio_capacity" not in sys.modules
print(g._settings(["--no-studio-gate"]).studio_gate_dir == "")
"""
        r = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "True"), r.stderr)


class PlatformCheckTests(unittest.TestCase):
    def test_windows_needs_no_studio_gate_and_a_node_policy(self):
        cfg = strata_cfg()
        with self.assertRaises(local_gate.GateConfigError) as cm:
            local_gate.check_platform(cfg, no_studio_gate=False, windows=True)
        self.assertIn("--no-studio-gate", str(cm.exception))
        with self.assertRaises(local_gate.GateConfigError) as cm:
            local_gate.check_platform(strata_cfg(no_policy=True), no_studio_gate=True, windows=True)
        self.assertIn("node_policy", str(cm.exception))
        local_gate.check_platform(cfg, no_studio_gate=True, windows=True)

    def test_macos_refuses_a_windows_policy_and_is_unchanged_without_one(self):
        with self.assertRaises(local_gate.GateConfigError):
            local_gate.check_platform(strata_cfg(), no_studio_gate=True, windows=False)
        local_gate.check_platform(strata_cfg(no_policy=True), no_studio_gate=False, windows=False)
        local_gate.check_platform(local_backend.load(None), no_studio_gate=False, windows=False)   # the Pro

    def test_print_config_with_no_studio_gate(self):
        d = tempfile.mkdtemp()
        tok = Path(d) / "tok"
        tok.write_text(TOKEN)
        os.chmod(tok, 0o600)
        raw = json.loads(EXAMPLE.read_text())
        raw.pop("node_policy")
        cfgp = Path(d) / "backend.json"
        cfgp.write_text(json.dumps(raw))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = local_gate.main(["--listen", "127.0.0.1", "--token-file", str(tok), "--backend-config", str(cfgp),
                                  "--no-studio-gate", "--print-config"])
        self.assertEqual(rc, 0)
        shown = json.loads(out.getvalue())
        self.assertIsNone(shown["studio_gate_dir"])
        self.assertEqual((shown["backend"], shown["served_model"], shown["max_inflight"]), ("strata", SERVED, 1))
        self.assertEqual(shown["upstream"], "http://127.0.0.1:8080")
        self.assertNotIn(TOKEN, out.getvalue())


# ---------------------------------------------------------------- the strata backend
class StrataConfigTests(unittest.TestCase):
    def test_the_shipped_example_is_the_measured_default(self):
        c = local_backend.load(str(EXAMPLE))
        self.assertEqual((c["backend"], c["served_model"], c["max_inflight"]), ("strata", SERVED, 1))
        self.assertEqual(c["model"]["context_tokens"], 49152)               # 48K, measured
        self.assertEqual(c["model"]["quant"], "UD-Q4_K_XL")
        self.assertIsNone(c["governor"])
        self.assertEqual(c["node_policy"]["kind"], "windows-idle")
        self.assertIsNone(c["node_policy"]["idle_after_s"])                 # input alone never stops it (owner 2026-11-24)
        self.assertTrue(c["node_policy"]["fullscreen_blocks"])
        self.assertEqual(c["node_policy"]["gpu_busy_pct"], 40)
        self.assertNotIn("Coder", EXAMPLE.read_text())                       # the 125B is left out (owner 2026-11-24)

    def test_the_model_is_config_another_entry_is_selected_by_name(self):
        raw = json.loads(EXAMPLE.read_text())
        raw["strata"]["models"]["other"] = {"served_model_name": "Next-Model", "context_tokens": 32768}
        raw["strata"]["model"] = "other"
        c = local_backend.check(raw)
        self.assertEqual((c["served_model"], c["model"]["upstream_ids"]), ("Next-Model", ["Next-Model"]))

    def test_bad_sections_are_refused(self):
        def bad(mutate, word):
            raw = json.loads(EXAMPLE.read_text())
            mutate(raw)
            with self.assertRaises(local_backend.BackendConfigError) as cm:
                local_backend.check(raw)
            self.assertIn(word, str(cm.exception))
        bad(lambda r: r.update(governor={}), "governor")
        bad(lambda r: r["strata"]["models"]["qwen36-35b-a3b-q4"].pop("context_tokens"), "context_tokens")
        bad(lambda r: r["strata"]["models"]["qwen36-35b-a3b-q4"].update(vram=1), "unknown key")
        bad(lambda r: r["strata"].update(max_inflight=0), "max_inflight")
        bad(lambda r: r["strata"].update(model="missing"), "strata.model")
        bad(lambda r: r.update(upstream="http://10.0.0.5:8080"), "loopback")
        bad(lambda r: r["node_policy"].update(gpu_busy_pct=0), "gpu_busy_pct")
        bad(lambda r: r["node_policy"].update(idle_after_s=-1), "idle_after_s")
        bad(lambda r: r["node_policy"].update(own_gpu_grace_s=10), "unknown key")       # replaced in fix round 1
        bad(lambda r: r["node_policy"].update(own_gpu_skip_max_s=0), "own_gpu_skip_max_s")
        bad(lambda r: r["node_policy"].update(activity_stale_s=6), "activity_stale_s")
        bad(lambda r: r["node_policy"].update(kind="mac"), "kind")
        bad(lambda r: r["node_policy"].update(extra=1), "unknown key")


class StrataBodyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = strata_cfg()

    def adapt(self, **body):
        return json.loads(local_backend.adapt_messages_body(self.cfg, json.dumps(body).encode()))

    def test_the_served_name_is_enforced_and_sent(self):
        for asked in (SERVED, SERVED + ":latest"):
            self.assertEqual(self.adapt(model=asked, max_tokens=10, messages=[])["model"], SERVED)
        for other in ("qwen-27b", "", None, "claude-opus-5-5"):
            with self.assertRaises(local_backend.Refusal) as cm:
                self.adapt(model=other, max_tokens=10, messages=[])
            self.assertEqual(cm.exception.status, 404)

    def test_thinking_passes_through_untouched(self):
        out = self.adapt(model=SERVED, max_tokens=10, messages=[], thinking={"type": "enabled", "budget_tokens": 5})
        self.assertEqual(out["thinking"], {"type": "enabled", "budget_tokens": 5})
        self.assertNotIn("chat_template_kwargs", out)

    def test_max_tokens_that_leaves_no_prompt_room_never_fits(self):
        with self.assertRaises(local_backend.Refusal) as cm:
            self.adapt(model=SERVED, max_tokens=49152, messages=[])
        self.assertEqual((cm.exception.status, cm.exception.kind), (413, "request_too_large"))

    def test_model_list_names_only_the_served_model(self):
        listed = json.dumps({"data": [{"id": SERVED, "context_length": 49152}]}).encode()
        self.assertEqual(local_backend.strata_tags(self.cfg, listed),
                         {"models": [{"name": SERVED, "model": SERVED, "loaded": True}]})
        unloaded = json.dumps({"data": [{"id": SERVED, "unloaded": True}]}).encode()
        self.assertFalse(local_backend.strata_tags(self.cfg, unloaded)["models"][0]["loaded"])
        self.assertEqual(local_backend.strata_loaded(self.cfg, unloaded), [])
        other = json.dumps({"data": [{"id": "Some-Other.gguf"}]}).encode()
        self.assertEqual(local_backend.strata_tags(self.cfg, other), {"models": []})
        self.assertIsNone(local_backend.strata_tags(self.cfg, b"[]"))

    def test_an_openai_shaped_error_is_rewrapped(self):
        raw = json.dumps({"error": {"message": "context too long", "type": "invalid_request_error"}}).encode()
        out = json.loads(local_backend.anthropic_error(400, raw, "strata"))
        self.assertEqual(out["error"], {"type": "invalid_request_error", "message": "strata: context too long"})


class FakeStrata(BaseHTTPRequestHandler):
    models = [{"id": SERVED, "context_length": 49152}]
    status = 200                     # the POST answer's status
    seen: list = []
    paths: list = []
    hold = None                      # a threading.Event: the POST waits on it
    entered = None                   # a threading.Semaphore: released when a POST arrives

    @classmethod
    def reset(cls):
        cls.models = [{"id": SERVED, "context_length": 49152}]
        cls.status, cls.seen, cls.paths, cls.hold, cls.entered = 200, [], [], None, None

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
        FakeStrata.paths.append(("GET", self.path))
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": FakeStrata.models})
        self._json(404, {"detail": "no"})

    def do_POST(self):
        FakeStrata.paths.append(("POST", self.path))
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FakeStrata.seen.append(body)
        if FakeStrata.entered is not None:
            FakeStrata.entered.release()
        if FakeStrata.hold is not None:
            FakeStrata.hold.wait(10)
        if FakeStrata.status == 503:
            return self._json(503, {"error": {"message": "the GPU is in use by another program"}})
        if FakeStrata.status == 429:
            return self._json(429, {"error": {"message": "queue timeout"}})
        self._json(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
                         "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                         "usage": {"input_tokens": 3, "output_tokens": 1}})


class StrataGateHarness(unittest.TestCase):
    POLICY = None                    # a node_policy object, or None: pro_local_allowed patched to allowed

    def setUp(self):
        home = tempfile.mkdtemp()
        env = patch.dict(os.environ, {"CORTADOS_HOME": home})
        env.start()
        self.addCleanup(env.stop)
        if self.POLICY is None:
            allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
            allowed.start()
            self.addCleanup(allowed.stop)
        FakeStrata.reset()
        self.addCleanup(FakeStrata.reset)
        self.strata = ThreadingHTTPServer(("127.0.0.1", 0), FakeStrata)
        threading.Thread(target=self.strata.serve_forever, daemon=True).start()
        self.cfg = strata_cfg(f"http://127.0.0.1:{self.strata.server_port}")
        self.policy = self.make_policy()
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream=self.cfg["upstream"], read_timeout=5,
                                          backend=self.cfg, admission=local_gate.admission_for(self.cfg, None),
                                          policy=self.policy)
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def make_policy(self):
        return None

    def tearDown(self):
        if FakeStrata.hold is not None:
            FakeStrata.hold.set()
        for s in (self.gate, self.strata):
            s.shutdown()
            s.server_close()

    def call(self, path, body=None, method=None, token=TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), dict(e.headers)

    MSG = {"model": SERVED, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


class StrataGateTests(StrataGateHarness):
    def test_probe_answers_the_served_name_and_the_count_limit(self):
        code, data, _ = self.call("/api/tags")
        self.assertEqual(code, 200)
        self.assertEqual([m["name"] for m in data["models"]], [SERVED])
        cap = data[local_gate.GATE_KEY]
        self.assertEqual((cap["backend"], cap["limit"], cap["inflight"]), ("strata", 1, 0))
        self.assertEqual(FakeStrata.paths, [("GET", "/v1/models")])

    def test_a_turn_is_relayed_with_the_served_name(self):
        code, data, _ = self.call("/v1/messages", dict(self.MSG, model=SERVED + ":latest"))
        self.assertEqual((code, data["stop_reason"]), (200, "end_turn"))
        self.assertEqual(FakeStrata.seen[0]["model"], SERVED)
        self.assertEqual(FakeStrata.paths, [("POST", "/v1/messages")])

    def test_another_model_name_is_a_clean_404_and_never_reaches_strata(self):
        code, data, headers = self.call("/v1/messages", dict(self.MSG, model="qwen-27b"))
        self.assertEqual((code, data["error"]["type"]), (404, "not_found_error"))
        self.assertEqual(headers.get("x-should-retry"), "false")
        self.assertEqual(FakeStrata.paths, [])

    def test_strata_control_paths_are_not_forwarded(self):
        for method, path in (("POST", "/v1/unload"), ("POST", "/load"), ("GET", "/mcp"), ("POST", "/slots/0"),
                             ("POST", "/v1/vram"), ("GET", "/v1/models")):
            code, _, _ = self.call(path, {} if method == "POST" else None, method=method)
            self.assertEqual(code, 404, path)
        self.assertEqual(FakeStrata.paths, [])

    def test_strata_gpu_in_use_and_queue_timeout_become_the_retryable_529(self):
        for status, word in ((503, "GPU in use by another program"), (429, "queue timed out")):
            FakeStrata.status = status
            code, data, headers = self.call("/v1/messages", self.MSG)
            self.assertEqual(code, 529)
            self.assertTrue(data["error"]["message"].startswith(local_gate.BUSY_MESSAGE))
            self.assertIn(word, data["error"]["message"])
            self.assertIn("retry-after", {k.lower() for k in headers})
            self.assertIsNotNone(local_lane._gate_busy_answer(json.dumps(data).encode()))   # the provider reads it

    def test_one_turn_at_a_time_a_second_gets_busy(self):
        FakeStrata.hold, FakeStrata.entered = threading.Event(), threading.Semaphore(0)
        first = {}
        t = threading.Thread(target=lambda: first.update(r=self.call("/v1/messages", self.MSG)), daemon=True)
        t.start()
        self.assertTrue(FakeStrata.entered.acquire(timeout=5))
        code, data, _ = self.call("/v1/messages", self.MSG)
        self.assertEqual(code, 529)
        self.assertIn("1 of 1 running", data["error"]["message"])
        FakeStrata.hold.set()
        t.join(5)
        self.assertEqual(first["r"][0], 200)
        self.assertEqual(self.call("/v1/messages", self.MSG)[0], 200)            # the slot came back

    def test_switch_status_names_the_backend_and_loaded_model(self):
        code, data, _ = self.call(local_gate.SWITCH_PATH)
        self.assertEqual(code, 200)
        self.assertEqual((data["backend"], data["models_loaded"], data["ollama"]), ("strata", [SERVED], "up"))
        FakeStrata.models = [{"id": SERVED, "unloaded": True}]
        self.assertEqual(self.call(local_gate.SWITCH_PATH)[1]["models_loaded"], [])


# ---------------------------------------------------------------- the serving policy (windows-idle)
class PolicyBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.activity = os.path.join(self.dir, "activity.json")
        self.switch = [True]
        self.api = FakeWinApi()
        self.clock = [100.0]
        self.wall = [1_800_000_000.0]
        self.gpu = ["5\n"]
        self.smi_calls = []
        self.slept = []

    def run_smi(self, argv, **kw):
        self.smi_calls.append(argv)
        out = self.gpu[0]
        if isinstance(out, BaseException):
            raise out

        class R:
            returncode = 0 if out is not None else 9
            stdout = out
        return R()

    def sleep(self, s):
        self.slept.append(s)
        self.clock[0] += s

    def policy(self, **cfg):
        raw = {"kind": "windows-idle", "activity_file": self.activity, **cfg}
        return node_policy.WindowsIdlePolicy(node_policy.check_config(raw), api=self.api, run=self.run_smi,
                                             clock=lambda: self.clock[0], wall=lambda: self.wall[0],
                                             switch_on=lambda: self.switch[0], sleep=self.sleep)

    def write(self, idle_s=0.0, fullscreen="none", age=1.0, session=None, path_id=None):
        session = dict(OWNER_SESSION) if session is None else session
        path = node_policy.session_activity_path(self.activity, session["id"] if path_id is None else path_id)
        Path(path).write_text(json.dumps({"v": 2, "at": self.wall[0] - age, "session": session, "idle_s": idle_s,
                                          "fullscreen": fullscreen}))


class IdlePolicyTests(PolicyBase):
    def test_owner_signed_out_is_away_and_serves(self):
        self.api.sessions = []
        self.assertEqual(self.policy().allowed(), (True, node_policy.ALLOWED))

    def test_default_serves_while_the_owner_types_with_no_full_screen_app(self):
        # owner 2026-11-24 (fix round 1): input alone does not stop serving; only the toggle, full screen or the GPU
        self.write(idle_s=0.4)
        p = self.policy()
        self.assertEqual(p.allowed(), (True, node_policy.ALLOWED))
        self.assertEqual(p.status()["activity"], "present")

    def test_the_input_rule_is_an_option_off_by_default(self):
        self.write(idle_s=12)
        self.assertEqual(self.policy(idle_after_s=300).allowed(), (False, node_policy.OWNER_ACTIVE))
        self.write(idle_s=301)
        self.assertEqual(self.policy(idle_after_s=300).allowed(), (True, node_policy.ALLOWED))
        self.assertEqual(self.policy(idle_after_s=300).status()["activity"], "idle")

    def test_a_full_screen_game_refuses(self):
        for quns_word in ("busy", "d3d", "presentation"):
            self.write(idle_s=3600, fullscreen=quns_word)
            p = self.policy()
            self.assertEqual(p.allowed(), (False, node_policy.FULLSCREEN))
            self.assertEqual(p.status()["activity"], "fullscreen")
        self.write(idle_s=3600, fullscreen="unknown")
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))

    def test_missing_stale_or_garbled_activity_is_unknown_never_free(self):
        p = self.policy()
        self.assertEqual(p.allowed(), (False, node_policy.OWNER_UNKNOWN))               # no writer running
        self.write(age=21)
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))   # stale
        self.write(age=-60)
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))   # from the future
        Path(node_policy.session_activity_path(self.activity, 1)).write_text("{not json")
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))
        # a version-1 record (no session identity) is never accepted
        Path(node_policy.session_activity_path(self.activity, 1)).write_text(json.dumps(
            {"v": 1, "at": self.wall[0], "idle_s": 900, "fullscreen": "none"}))
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))
        self.api.sessions = OSError(5, "denied")
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))   # sessions unreadable
        self.api.sessions = [{"id": 1}]
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))   # malformed identity

    def test_switch_off_and_power(self):
        self.api.sessions = []
        self.switch[0] = False
        self.assertEqual(self.policy().allowed(), (False, node_policy.SWITCH_OFF))
        self.switch[0] = True
        for ac, why in ((0, node_policy.ON_BATTERY), (255, node_policy.POWER_UNKNOWN),
                        (OSError(1, "x"), node_policy.POWER_FAILED)):
            self.api.ac = ac
            self.assertEqual(self.policy().allowed(), (False, why))
        self.api.ac = 255
        self.assertTrue(self.policy(require_ac=False).allowed()[0])

    def test_the_switch_is_read_on_every_call_the_samples_are_cached(self):
        self.api.sessions = []
        p = self.policy()
        self.assertTrue(p.allowed()[0])
        self.assertTrue(p.allowed()[0])
        self.assertEqual(self.api.calls.count("active_sessions"), 1)                    # within sample_s
        self.switch[0] = False
        self.assertEqual(p.allowed(), (False, node_policy.SWITCH_OFF))                  # at once
        self.switch[0] = True
        self.api.sessions = [dict(OWNER_SESSION)]
        self.write(fullscreen="d3d")
        self.assertTrue(p.allowed()[0])                                                 # still the cached sample
        self.clock[0] += 3.0
        self.assertEqual(p.allowed(), (False, node_policy.FULLSCREEN))                  # re-sampled

    def test_another_program_using_the_gpu_refuses(self):
        self.api.sessions = []
        self.gpu[0] = "85\n"
        self.assertEqual(self.policy().allowed(), (False, node_policy.GPU_BUSY))
        self.assertEqual(self.smi_calls[0][1:3], ["-i", "0"])
        for broken in ("N/A\n", None, subprocess.TimeoutExpired("nvidia-smi", 2), "1\n2\n"):
            self.gpu[0] = broken
            self.assertEqual(self.policy().allowed(), (False, node_policy.GPU_UNKNOWN))
        self.gpu[0] = "85\n"
        self.assertTrue(self.policy(gpu_busy_pct=None).allowed()[0])                     # check turned off

    def test_memory_load_only_when_configured(self):
        self.api.sessions, self.api.memory = [], 99
        self.assertTrue(self.policy().allowed()[0])                                     # default off (docstring)
        self.assertEqual(self.policy(max_memory_load_pct=95).allowed(), (False, node_policy.MEMORY_HIGH))
        self.api.memory = OSError(1, "x")
        self.assertEqual(self.policy(max_memory_load_pct=95).allowed(), (False, node_policy.MEMORY_UNKNOWN))

    def test_status_carries_the_owner_word(self):
        self.write(idle_s=4000)
        st = self.policy().status()
        self.assertEqual(st, {"switch": "on", "allowed": True, "reason": node_policy.ALLOWED, "power": "ac",
                              "activity": "present"})
        self.assertLessEqual({"away", "present", "idle", "active", "fullscreen", "unknown"}, local_switch.ACTIVITY)

    def test_every_refusal_is_one_the_provider_may_repeat(self):
        self.assertTrue({"local gate: " + r for r in node_policy.REFUSALS} <= local_lane.PRO_LOCAL_REFUSALS)


class OwnGpuTests(PolicyBase):
    """Reviewer finding 1 (fix round 1): the GPU check could stay off forever, because every forwarded turn's end
    renewed a grace period. Now a reading is taken whenever the gate is quiet (own_gpu_settle_s after its last turn),
    and a turn of its own in flight leans on the last quiet reading for at most own_gpu_skip_max_s."""

    def setUp(self):
        super().setUp()
        self.api.sessions = []

    def test_back_to_back_short_turns_never_hide_another_programs_load(self):
        # the reviewer's repro: a conversation's turns arrive under 10 s apart, and a game takes the card meanwhile
        p = self.policy()
        admitted = 0
        for i in range(13):
            if p.allowed()[0]:
                admitted += 1
                p.forward_started()
                if i == 0:
                    self.gpu[0] = "99\n"                                                # the game starts mid-turn
                self.clock[0] += 3.0
                p.forward_ended()
            self.clock[0] += 4.0
        self.assertEqual(admitted, 1)                                                   # only the turn before the game
        self.assertEqual(p.allowed(), (False, node_policy.GPU_BUSY))
        p.forward_started()                                                             # a call during a turn
        self.clock[0] += 3.0
        self.assertEqual(p.allowed(), (False, node_policy.GPU_UNKNOWN))                 # the busy reading stands

    def test_failed_attempts_do_not_extend_the_skip(self):
        p = self.policy()
        self.assertTrue(p.allowed()[0])                                                 # quiet reading at t=100
        self.gpu[0] = "99\n"
        p.forward_started()                                                             # a long turn of its own
        for _ in range(30):                                                             # attempts end and restart
            p.forward_started()
            p.forward_ended()
            self.clock[0] += 3.0
        self.assertEqual(p.allowed(), (False, node_policy.GPU_UNKNOWN))                 # 90 s > own_gpu_skip_max_s

    def test_the_gates_own_decoding_is_not_another_program(self):
        p = self.policy()
        self.assertTrue(p.allowed()[0])                                                 # quiet reading, 5 %
        n = len(self.smi_calls)
        self.gpu[0] = "97\n"                                                            # Strata decoding
        p.forward_started()
        self.clock[0] += 20.0
        self.assertTrue(p.allowed()[0])                                                 # own turn: no reading taken
        self.assertEqual(len(self.smi_calls), n)
        self.clock[0] += 3.0                                                            # past sample_s
        p.forward_ended()
        self.gpu[0] = "3\n"                                                             # Strata done: card quiet
        self.clock[0] += 0.5
        self.assertTrue(p.allowed()[0])                                                 # next turn 0.5 s later
        self.assertEqual(len(self.smi_calls), n + 1)
        self.assertAlmostEqual(sum(self.slept), 1.0)                                    # waited out the settle time

    def test_a_reading_mixed_with_an_own_turn_start_is_not_trusted(self):
        p = self.policy()
        self.gpu[0] = "99\n"

        def smi(argv, **kw):                                                            # a turn starts mid-reading
            p.forward_started()
            return self.run_smi(argv, **kw)
        p._run = smi
        self.assertEqual(p.allowed(), (False, node_policy.GPU_UNKNOWN))                 # no quiet reading yet


class SessionBindingTests(PolicyBase):
    """Reviewer finding 2 (fix round 1): each active session needs its own fresh record, bound to its id, user and
    logon time."""

    def test_a_record_of_another_session_never_covers_an_active_one(self):
        other = {"id": 2, "user": "PC\\owner", "logon": OWNER_SESSION["logon"] + 1}
        self.write(session=other)                                                       # disconnected session's writer
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))
        self.write(session=other, path_id=1)                                            # even in session 1's file
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))
        self.write()
        self.assertTrue(self.policy().allowed()[0])

    def test_an_earlier_logon_that_reused_the_session_id_is_refused(self):
        self.write(session=dict(OWNER_SESSION, logon=OWNER_SESSION["logon"] - 10_000_000))
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))
        self.write(session=dict(OWNER_SESSION, user="PC\\guest"))
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))

    def test_every_active_session_must_be_covered(self):
        second = {"id": 3, "user": "PC\\guest", "logon": 7}
        self.api.sessions = [dict(OWNER_SESSION), second]
        self.write()
        self.assertEqual(self.policy().allowed(), (False, node_policy.OWNER_UNKNOWN))   # the guest has no writer
        self.write(session=second, fullscreen="d3d")
        self.assertEqual(self.policy().allowed(), (False, node_policy.FULLSCREEN))      # the guest is gaming
        self.write(session=second)
        self.assertTrue(self.policy().allowed()[0])


class ActivityWriterTests(PolicyBase):
    def test_one_record_is_written_and_read_back(self):
        api = FakeWinApi(idle=412.37, quns=3)
        rc = node_policy.run_writer(self.activity, api=api, once=True, wall=lambda: self.wall[0])
        self.assertEqual(rc, 0)
        path = node_policy.session_activity_path(self.activity, OWNER_SESSION["id"])
        self.assertTrue(path.endswith("activity.s1.json"))
        self.assertEqual(node_policy.read_activity(path, self.wall[0], 20, OWNER_SESSION),
                         {"idle_s": 412.4, "fullscreen": "d3d"})
        self.assertEqual(json.loads(Path(path).read_text())["session"], OWNER_SESSION)
        self.assertTrue(Path(path).read_bytes().isascii())
        self.assertFalse(os.path.exists(self.activity))                                 # never the shared base name

    def test_a_failed_idle_or_identity_read_writes_nothing_and_fails(self):
        for api in (FakeWinApi(idle=OSError(5, "x")), FakeWinApi(current=OSError(5, "x")),
                    FakeWinApi(current={"id": 1})):
            logged = []
            rc = node_policy.run_writer(self.activity, api=api, once=True, log=logged.append)
            self.assertEqual(rc, 1)
            self.assertEqual(os.listdir(self.dir), [])
            self.assertTrue(logged)

    def test_a_failed_fullscreen_read_is_written_as_unknown(self):
        node_policy.run_writer(self.activity, api=FakeWinApi(idle=900, quns=OSError(1, "x")), once=True,
                               wall=lambda: self.wall[0])
        path = node_policy.session_activity_path(self.activity, 1)
        self.assertEqual(node_policy.read_activity(path, self.wall[0], 20, OWNER_SESSION)["fullscreen"], "unknown")

    def test_the_writer_refuses_to_run_off_windows(self):
        self.assertEqual(node_policy.main(["activity", "--out", self.activity]), 2)


class IdleRefusalGateTests(StrataGateHarness):
    """The gate with the real windows-idle policy (fake Windows inputs): it refuses with the fixed reason while a
    full-screen app runs, the provider's probe repeats that reason, and a turn already running finishes."""
    POLICY = True

    def make_policy(self):
        self.pdir = tempfile.mkdtemp()
        self.activity = os.path.join(self.pdir, "activity.json")
        self.api = FakeWinApi()
        self.wall = [time.time()]
        self.clock = [0.0]
        local_resources.set_local_switch(True)
        raw = {"kind": "windows-idle", "activity_file": self.activity, "gpu_busy_pct": None}
        return node_policy.WindowsIdlePolicy(node_policy.check_config(raw), api=self.api, clock=lambda: self.clock[0],
                                             wall=lambda: self.wall[0])

    def owner(self, fullscreen):
        Path(node_policy.session_activity_path(self.activity, OWNER_SESSION["id"])).write_text(json.dumps(
            {"v": 2, "at": self.wall[0], "session": OWNER_SESSION, "idle_s": 0.2, "fullscreen": fullscreen}))
        self.clock[0] += 10.0                                    # past sample_s: the next call re-reads

    def test_a_full_screen_game_refuses_new_turns_and_the_probe(self):
        self.owner("d3d")
        code, data, _ = self.call("/v1/messages", self.MSG)
        self.assertEqual((code, data["error"]["message"]), (503, "local gate: " + node_policy.FULLSCREEN))
        self.assertEqual(self.call("/api/tags")[0], 503)
        self.assertEqual(FakeStrata.paths, [])
        node = {"node": "pc", "base_url": self.url, "token": TOKEN, "probe_timeout_s": 2}
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.probe(node)
        self.assertIn("full-screen app running", str(cm.exception))         # a fixed reason, repeated to the client
        self.owner("none")
        self.assertEqual(self.call("/v1/messages", self.MSG)[0], 200)

    def test_a_running_turn_finishes_when_a_game_starts(self):
        self.owner("none")
        FakeStrata.hold, FakeStrata.entered = threading.Event(), threading.Semaphore(0)
        first = {}
        t = threading.Thread(target=lambda: first.update(r=self.call("/v1/messages", self.MSG)), daemon=True)
        t.start()
        self.assertTrue(FakeStrata.entered.acquire(timeout=5))
        self.owner("d3d")                                          # a game starts mid-turn
        FakeStrata.hold.set()
        t.join(5)
        self.assertEqual(first["r"][0], 200)                       # finished, not cut off
        self.assertEqual(self.call("/v1/messages", self.MSG)[0], 503)   # nothing new admitted

    def test_the_toggle_turns_serving_off_and_on_through_the_gate(self):
        self.owner("none")
        code, st, _ = self.call(local_gate.SWITCH_PATH, {"on": False}, method="POST")
        self.assertEqual((code, st["switch"], st["allowed"], st["reason"]),
                         (200, "off", False, node_policy.SWITCH_OFF))
        self.assertEqual(st["activity"], "present")
        self.assertEqual(self.call("/v1/messages", self.MSG)[0], 503)
        self.assertFalse(local_resources.local_switch_path().exists())
        code, st, _ = self.call(local_gate.SWITCH_PATH, {"on": True}, method="POST")
        self.assertEqual((st["switch"], st["allowed"]), ("on", True))
        self.assertEqual(self.call("/v1/messages", self.MSG)[0], 200)

    def test_the_provider_local_switch_tool_flips_the_pc_by_node_name(self):
        self.owner("none")
        tok = Path(self.pdir) / "pc-token"
        tok.write_text(TOKEN)
        os.chmod(tok, 0o600)
        cfg = json.loads(PC_EXAMPLE.read_text())
        cfgp = Path(self.pdir) / "resources.json"
        cfgp.write_text(json.dumps(cfg))
        with patch.dict(os.environ, {"CORTADOS_LOCAL_PC_GATE_URL": self.url,
                                     "CORTADOS_LOCAL_PC_GATE_TOKEN_FILE": str(tok)}):
            row = local_switch.switch("PC", False, config_path=str(cfgp))
            self.assertEqual((row["node"], row["switch"], row["allowed"], row["backend"], row["activity"]),
                             ("pc", "off", False, "strata", "present"))
            self.assertEqual(row["reason"], node_policy.SWITCH_OFF)
            row = local_switch.switch("pc", True, config_path=str(cfgp))
            self.assertEqual((row["switch"], row["allowed"], row["models_loaded"]), ("on", True, [SERVED]))
            view = local_switch.view(str(cfgp))
        self.assertEqual([(n["node"], n["reachable"]) for n in view["nodes"]], [("pc", True)])
        self.assertNotIn(TOKEN, json.dumps(view))


# ---------------------------------------------------------------- the provider: config and a node that is down
class PcNodeConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tok = Path(self.dir) / "tok"
        self.tok.write_text(TOKEN)
        os.chmod(self.tok, 0o600)
        env = patch.dict(os.environ, {"CORTADOS_LOCAL_PC_GATE_URL": "http://127.0.0.1:9",
                                      "CORTADOS_LOCAL_PC_GATE_TOKEN_FILE": str(self.tok),
                                      "CORTADOS_LOCAL_PRO_OLLAMA_URL": "http://127.0.0.1:9",
                                      "CORTADOS_LOCAL_PRO_GATE_TOKEN_FILE": str(self.tok)})
        env.start()
        self.addCleanup(env.stop)

    def test_the_shipped_pc_entry_is_pin_only_one_at_a_time(self):
        node, = local_lane.nodes(str(PC_EXAMPLE))
        self.assertEqual((node["node"], node["lane"], node["max_inflight"]), ("pc", "local-pc", 1))
        self.assertIsNone(node["auto_route"])
        self.assertFalse(node["index_route"])                    # S1 never auto-picks it until the owner opts in
        m, = node["models"]
        self.assertEqual((m["id"], m["alias"], m["context_length"]), (SERVED, "qwen36-35b", 49152))
        backend = local_backend.load(str(EXAMPLE))
        self.assertEqual(m["id"], backend["served_model"])         # the two configs name the same model
        self.assertLessEqual(m["context_length"], backend["model"]["context_tokens"])
        self.assertNotIn(SERVED, json.dumps(json.loads((ROOT / "tables" / "model-scores.generated.json").read_text())
                                            .get("local_models")))   # no published proxy index yet

    def test_it_sits_beside_the_pro_with_unique_pin_names(self):
        fleet = json.loads((ROOT / "tables" / "resources.example.json").read_text())
        fleet["nodes"] += json.loads(PC_EXAMPLE.read_text())["nodes"]
        p = Path(self.dir) / "resources.json"
        p.write_text(json.dumps(fleet))
        self.assertEqual([n["node"] for n in local_lane.nodes(str(p))], ["pro", "pc"])
        self.assertEqual(local_lane.find("qwen36-35b", str(p))[0]["node"], "pc")

    def test_backend_lists_know_strata(self):
        from cortados_router.provider import office
        self.assertIn("strata", local_lane.NODE_BACKENDS)
        self.assertIn("strata", local_switch.BACKENDS)
        self.assertIn("strata", office.LOCAL_BACKENDS)

    def test_a_pc_that_is_asleep_fails_its_probe_quickly(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                                  # nothing listens: connection refused
        node = {"node": "pc", "base_url": f"http://127.0.0.1:{port}", "token": TOKEN, "probe_timeout_s": 2}
        t0 = time.monotonic()
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.probe(node)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertIn("asleep, offline or busy", str(cm.exception))


PC_VARIANT = {"family": "qwen36-35b", "local_id": SERVED, "thinking": "off", "proxy": True, "provenance": "test",
              "published": "qwen3-6-35b-a3b-non-reasoning", "index": {"general": 20.0, "code": 20.0}}
PRO_VARIANT = {"family": "qwen-27b", "local_id": TAG, "thinking": "off", "proxy": True, "provenance": "test",
               "published": "qwen3-8-27b-non-reasoning", "index": {"general": 30.0, "code": 30.0}}


class PcNodeDownRoutingTests(AutoLocalBase):
    """The provider marks a node down after a failed probe (routing.AUTO_DOWN_S) and routes around it. With two nodes,
    a PC that is asleep is skipped for AUTO_DOWN_S without another probe, the Pro still takes the work, and the PC is
    probed again after the window. (The test config turns the PC's index_route on to exercise the path; the shipped
    entry is pin-only.)"""

    def setUp(self):
        super().setUp()
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["auto_route"] = False
        cfg["nodes"].append({"id": "pc", "role": "worker", "target": None, "pool_eligible": False,
                             "ollama": {"base_url_env": "TEST_LOCAL_PC_URL", "index_route": True, "max_inflight": 1,
                                        "probe_timeout_s": 1, "connect_timeout_s": 1, "read_timeout_s": 5,
                                        "models": [{"id": SERVED, "alias": "qwen36-35b", "context_length": 49152}]}})
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        os.environ["TEST_LOCAL_PC_URL"] = "http://127.0.0.1:9"
        self.addCleanup(os.environ.pop, "TEST_LOCAL_PC_URL", None)
        self._scores = core.model_scores
        core.model_scores = lambda: {"models": [], "local_models": [PC_VARIANT, PRO_VARIANT]}
        self.addCleanup(setattr, core, "model_scores", self._scores)
        local_lane._inflight.clear()
        self.addCleanup(local_lane._inflight.clear)
        self.down = {"pc"}
        self.pro_up = True

        def probe(node):
            self.probes.append(node["node"])
            if node["node"] in self.down:
                raise local_lane.LocalUnavailable(f"node {node['node']} did not answer /api/tags within 1s (asleep, "
                                                  "offline or busy?): TimeoutError: timed out")
            return {SERVED} if node["node"] == "pc" else {TAG}
        local_lane.probe = probe

    def route(self, req=10.0):
        self.selected = scored_route(req)
        return self.choose(body())

    def test_an_asleep_pc_is_skipped_for_the_window_and_the_pro_takes_the_work(self):
        with self.assertLogs(routing.logger, level="INFO") as cm:
            r = self.route()
        self.assertEqual(r["provider"], "local-pro")                # the lighter-index PC was tried first, then Pro
        self.assertEqual(self.probes, ["pc", "pro"])
        self.assertTrue(any("local-pc" in m and "unavailable" in m for m in cm.output))
        self.assertGreater(routing._auto_down["pc"], self.clock[0])
        self.probes.clear()
        self.clock[0] += routing.AUTO_DOWN_S - 1
        self.assertEqual(self.route()["provider"], "local-pro")
        self.assertEqual(self.probes, ["pro"])                      # inside the window: the PC is not probed
        self.probes.clear()
        self.clock[0] += 2
        self.down = set()                                           # the PC woke up
        self.assertEqual(self.route()["provider"], "local-pc")
        self.assertEqual(self.probes, ["pc"])
        self.assertNotIn("pc", routing._auto_down)

    def test_both_down_keeps_s1s_cloud_route(self):
        self.down = {"pc", "pro"}
        self.assertS1Route(self.route())
        self.assertEqual(sorted(routing._auto_down), ["pc", "pro"])

    def test_a_busy_pc_is_not_offered_nor_probed(self):
        local_lane._inflight["pc"] = 1                              # max_inflight 1: Strata runs one turn
        self.down = set()
        self.assertEqual(self.route()["provider"], "local-pro")
        self.assertEqual(self.probes, ["pro"])

    def test_a_pin_to_an_asleep_pc_is_a_clear_503_naming_the_node(self):
        node, entry = local_lane.find("qwen36-35b")
        self.assertEqual(node["node"], "pc")
        with self.assertRaises(local_lane.LocalUnavailable) as cm:
            local_lane.probe(node)
        self.assertIn("node pc", str(cm.exception))


# ---------------------------------------------------------------- the Windows scripts (static: no PowerShell here)
class WindowsScriptTests(unittest.TestCase):
    SCRIPTS = sorted((ROOT / "scripts" / "windows").glob("*.ps1"))

    def test_scripts_are_ascii_for_powershell_5_1(self):
        self.assertEqual({p.name for p in self.SCRIPTS}, {"install_pc_node.ps1", "cortados-serving.ps1"})
        for p in self.SCRIPTS:
            self.assertTrue(p.read_bytes().isascii(), p.name)

    def test_installer_is_a_dry_run_unless_apply(self):
        text = (ROOT / "scripts" / "windows" / "install_pc_node.ps1").read_text()
        self.assertIn("if ($Apply) {", text)
        self.assertIn("[dry-run] would:", text)
        # every changing command sits inside a Step block (its scriptblock runs only with -Apply)
        body = text.split("function Q(", 1)[0] + text.split("$gateArgs", 1)[1]
        changing = ("Register-ScheduledTask ", "Unregister-ScheduledTask", "New-NetFirewallRule", "Remove-NetFirewallRule",
                    "icacls.exe", "Copy-Item", "Write-Ascii $", "New-Item", ".Save()")
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if any(c in line for c in changing) and not line.lstrip().startswith(("#", "function")):
                opener = next((lines[j] for j in range(i, -1, -1) if lines[j].lstrip().startswith("Step ")), "")
                self.assertTrue(opener, f"line {i}: {line.strip()} is not inside a Step block")

    def test_installer_refuses_a_non_ascii_profile_before_any_change(self):
        """Reviewer finding 3 (fix round 1): Write-Ascii stores a non-ASCII character as '?', so a profile path with one
        would be written as another path. The check covers %USERPROFILE% and runs before the first Step outside -Remove."""
        import re
        text = (ROOT / "scripts" / "windows" / "install_pc_node.ps1").read_text()
        lines = text.splitlines()
        checks = text.split("$PathChecks = @(", 1)[1].split("\n)\n", 1)[0]
        for name in ("$env:USERPROFILE", "$Repo", "$Python", "$PSScriptRoot"):
            self.assertIn(name, checks)
        check_at = next(i for i, l in enumerate(lines) if l.startswith("foreach ($pc in $PathChecks)"))
        self.assertIn("Test-Ascii $pc[1]", lines[check_at + 1])
        self.assertIn("throw", lines[check_at + 2])
        remove_end = next(i for i, l in enumerate(lines) if l.startswith("if ($Remove)"))
        remove_end = next(i for i in range(remove_end, len(lines)) if lines[i] == "}")
        first_step = next(i for i in range(remove_end + 1, len(lines)) if re.match(r"^\s*Step ", lines[i]))
        self.assertLess(check_at, first_step)
        # the same rule as Test-Ascii (printable ASCII only), simulated
        def test_ascii(t):
            return all(32 <= ord(c) <= 126 for c in t)
        self.assertIn("-gt 126 -or [int]$c -lt 32", text)
        self.assertTrue(test_ascii("C:\\Users\\owner"))
        self.assertFalse(test_ascii("C:\\Users\\Ren\u00e9"))

    def test_scripts_name_no_address_user_or_key(self):
        import re
        for p in self.SCRIPTS + [EXAMPLE, PC_EXAMPLE]:
            text = p.read_text()
            ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
            self.assertLessEqual(ips, {"127.0.0.1", "100.64.0.0", "0.0.0.0"}, p.name)
            self.assertNotRegex(text, r"(?i)C:\\Users\\(?!<)")
            self.assertNotIn("id_ed25519", text)

    def test_toggle_calls_the_gates_switch_and_never_prints_the_token(self):
        text = (ROOT / "scripts" / "windows" / "cortados-serving.ps1").read_text()
        self.assertIn(local_gate.SWITCH_PATH, text)
        self.assertNotRegex(text, r"Write-(Host|Output)[^\n]*\$token")
        self.assertIn("$token = $null", text)


if __name__ == "__main__":
    unittest.main()
