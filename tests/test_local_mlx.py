"""The MLX backend for the local lane (docs/LOCAL-RESOURCES.md "MLX backend"): the activity governor's policy and
signals (local_governor.py), the backend config and request adaptation (local_backend.py), the gate's admission count
and its retryable 529 (local_gate.py), and the provider treating a full gate as a busy node (local_lane, routing).
Fakes only: an in-thread fake vllm-mlx on loopback, fake ioreg/sysctl runners; no model, no vendor CLI, no launchctl."""
import json
import os
import plistlib
import shutil
import subprocess
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

from cortados_router import local_backend, local_gate, local_governor
from cortados_router.provider import local_lane, local_switch, routing
from tests.test_local_gate import TOKEN, token_file
from tests.test_provider import TOKEN as PROVIDER_TOKEN
from tests.test_provider_local import LocalHarness, TAG, write_config
from tests.test_provider_local_index import IndexBase

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "tables" / "local-backend.example.json"
SERVED = TAG                                    # the fake vllm-mlx serves the provider's configured id

# --------------------------------------------------------------------------------------------------- governor policy


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

    def test_memory_budget_bounds_the_idle_cap(self):
        # (30 - 15) / 2 = 7 requests fit beside the weights: the idle cap of 8 is cut to 7
        got = self.limit(3600, "normal", memory_budget_gib=30)
        self.assertEqual(got["limit"], 7)
        self.assertIn("memory budget", got["reason"])
        # a budget that only fits the weights admits nothing, even with the owner active
        self.assertEqual(self.limit(30, "normal", memory_budget_gib=16)["limit"], 0)

    def test_per_model_overrides(self):
        six_bit = {"idle_cap": 6, "weights_gib": 22, "per_request_gib": 2.2}
        c = local_governor.check_config({}, six_bit)
        self.assertEqual(local_governor.policy(c, 3600, "normal")["limit"], 6)
        self.assertEqual(local_governor.policy(c, 30, "normal")["limit"], 2)

    def test_bad_thresholds_are_refused(self):
        for bad in ({"active_cap": -1}, {"active_cap": True}, {"sample_s": 0}, {"memory_budget_gib": float("nan")},
                    {"unknown_pressure": "fine"}, {"idle_cap": 0}, {"bogus": 1}):
            with self.assertRaises(local_governor.GovernorConfigError, msg=bad):
                cfg(**bad)


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
    def test_no_governor_is_unlimited(self):
        a = local_governor.Admission(None)
        for _ in range(50):
            self.assertTrue(a.try_acquire()[0])
        self.assertIsNone(a.snapshot()["limit"])

    def test_return_to_active_stops_admitting_beyond_two_and_never_stops_running_requests(self):
        g = FixedGovernor(idle_s=3600)
        a = local_governor.Admission(g)
        for _ in range(8):                                    # owner idle: 8 admitted
            self.assertTrue(a.try_acquire()[0])
        ok, snap = a.try_acquire()
        self.assertFalse(ok)
        self.assertEqual((snap["limit"], snap["inflight"]), (8, 8))
        g.set(idle_s=5)                                       # the owner is back
        ok, snap = a.try_acquire()
        self.assertFalse(ok)
        self.assertEqual((snap["limit"], snap["inflight"], snap["owner"]), (2, 8, "active"))
        self.assertEqual(a.inflight, 8)                       # nothing running was stopped
        for left in (7, 6, 5, 4, 3, 2):                       # requests finish on their own
            a.release()
            self.assertFalse(a.try_acquire()[0], left)
        a.release()                                           # 1 running: a second may start
        self.assertTrue(a.try_acquire()[0])
        self.assertFalse(a.try_acquire()[0])

    def test_warn_then_critical(self):
        g = FixedGovernor(idle_s=3600, pressure="warn")
        a = local_governor.Admission(g)
        self.assertTrue(a.try_acquire()[0])
        self.assertFalse(a.try_acquire()[0])
        g.set(pressure="critical")
        a.release()
        ok, snap = a.try_acquire()
        self.assertFalse(ok)
        self.assertEqual((snap["limit"], snap["reason"]), (0, "memory pressure critical"))


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
        self.assertEqual(c["governor"]["weights_gib"], 22)

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
                                                 "pressure": None, "reason": "no governor"})
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
                             ("GET", "/health"), ("POST", "/v1/requests/x/cancel")):
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
        self.assertEqual(len([s for s in FakeVllm.seen if s[0] == "POST"]), 1)
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
        self.assertEqual([s for s in FakeVllm.seen if s[0] == "POST"], [])
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


if __name__ == "__main__":
    unittest.main()
