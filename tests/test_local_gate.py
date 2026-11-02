"""The node-side token gate (local_gate.py) and the provider's local lane through it. In-thread fakes only."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from cortados_router import local_gate
from cortados_router.provider import local_lane
from tests.test_provider_local import FakeOllama, LocalHarness, TAG, write_config

TOKEN = "a" * 40


def token_file(directory, token=TOKEN, mode=0o600):
    p = Path(directory) / "gate-token"
    p.write_text(token + "\n")
    os.chmod(p, mode)
    return str(p)


class GateHarness(unittest.TestCase):
    def setUp(self):
        self.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        FakeOllama.seen = []
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.ollama.server_port}", read_timeout=5)
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def tearDown(self):
        for s in (self.gate, self.ollama):
            s.shutdown()
            s.server_close()

    def call(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"content-type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class GateTests(GateHarness):
    MSG = {"model": TAG, "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}

    def test_right_token_is_forwarded_by_bearer_or_api_key(self):
        code, raw = self.call("/api/tags", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 200)
        self.assertIn(TAG, raw)
        code, raw = self.call("/v1/messages", self.MSG, headers={"x-api-key": TOKEN})
        self.assertEqual((code, json.loads(raw)["stop_reason"]), (200, "end_turn"))

    def test_missing_or_wrong_token_is_401_without_echo(self):
        for headers in ({}, {"authorization": "Bearer nope"}, {"x-api-key": TOKEN[:-1]}, {"authorization": TOKEN}):
            code, raw = self.call("/v1/messages", {**self.MSG, "secret": "do-not-echo"}, headers=headers)
            self.assertEqual(code, 401)
            self.assertNotIn("do-not-echo", raw)
            self.assertNotIn("nope", raw)
        self.assertEqual(FakeOllama.seen, [])

    def test_other_paths_are_404(self):
        auth = {"authorization": f"Bearer {TOKEN}"}
        for method, path, body in (("POST", "/api/pull", {"name": "x"}), ("DELETE", "/api/delete", {"name": TAG}),
                                   ("POST", "/api/generate", {}), ("GET", "/v1/messages", None), ("POST", "/api/tags", {})):
            self.assertEqual(self.call(path, body, headers=auth, method=method)[0], 404, (method, path))
        self.assertEqual(FakeOllama.seen, [])

    def test_streams_sse_through(self):
        code, raw = self.call("/v1/messages", {**self.MSG, "stream": True}, headers={"x-api-key": TOKEN})
        self.assertEqual(code, 200)
        events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]
        self.assertEqual((events[0]["type"], events[-1]["type"]), ("message_start", "message_stop"))

    def test_oversized_body_is_413(self):
        self.gate.max_body = 100
        code, _ = self.call("/v1/messages", {**self.MSG, "pad": "x" * 200}, headers={"x-api-key": TOKEN})
        self.assertEqual(code, 413)
        self.assertEqual(FakeOllama.seen, [])

    def test_ollama_down_is_502(self):
        self.ollama.shutdown()
        self.ollama.server_close()
        self.assertEqual(self.call("/api/tags", headers={"x-api-key": TOKEN})[0], 502)


class GateConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_token_file_rules(self):
        self.assertEqual(local_gate.read_token(token_file(self.dir)), TOKEN)
        for mode in (0o640, 0o604, 0o644):
            with self.assertRaises(local_gate.GateConfigError):
                local_gate.read_token(token_file(self.dir, mode=mode))
        with self.assertRaises(local_gate.GateConfigError):
            local_gate.read_token(token_file(self.dir, token="short"))
        with self.assertRaises(local_gate.GateConfigError):
            local_gate.read_token(os.path.join(self.dir, "missing"))

    def test_all_interfaces_refused_without_the_flag(self):
        for host in ("0.0.0.0", "::", ""):
            with self.assertRaises(local_gate.GateConfigError):
                local_gate.check_listen(host, allow_any=False)
            local_gate.check_listen(host, allow_any=True)
        local_gate.check_listen("127.0.0.1", allow_any=False)
        self.assertEqual(local_gate.main(["--listen", "0.0.0.0", "--token-file", token_file(self.dir)]), 2)

    def test_print_config_checks_without_printing_the_token(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = local_gate.main(["--listen", "127.0.0.1", "--token-file", token_file(self.dir), "--print-config"])
        self.assertEqual(rc, 0)
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("ok (40 chars", out.getvalue())
        self.assertEqual(local_gate.main(["--listen", "127.0.0.1", "--print-config",
                                          "--token-file", token_file(self.dir, mode=0o644)]), 2)


class ProviderThroughGateTests(LocalHarness):
    """The provider's local lane sends the token on the probe and the turn; a refusal is a clear 503."""

    def setUp(self):
        super().setUp()
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.ollama.server_port}", read_timeout=5)
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.tok_dir = tempfile.mkdtemp()
        os.environ[local_lane.CONFIG_ENV] = write_config(self.scratch, f"http://127.0.0.1:{self.gate.server_port}",
                                                         token_file=token_file(self.tok_dir))

    def tearDown(self):
        self.gate.shutdown()
        self.gate.server_close()
        super().tearDown()

    BODY = {"model": "sub/qwen-27b", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}

    def test_provider_sends_the_token(self):
        self.assertEqual(self.post(self.BODY)["stop_reason"], "end_turn")
        raw = self.post({**self.BODY, "stream": True})
        self.assertIn('"qwen"', raw)
        self.assertTrue(raw.rstrip().endswith('{"type": "message_stop"}'))

    def test_a_refused_token_is_a_clear_503(self):
        self.gate.token = b"b" * 40
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("local gate on node pro refused the provider token", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_a_refusal_after_the_probe_is_also_a_503(self):
        real = local_lane.probe
        local_lane.probe = lambda node: {TAG, "phi:latest"}       # the gate rotates its token between probe and turn
        self.gate.token = b"b" * 40
        try:
            code, msg = self.error_of(self.BODY)
        finally:
            local_lane.probe = real
        self.assertEqual(code, 503)
        self.assertIn("refused the provider token", msg)

    def test_unsafe_provider_token_file_refuses_the_pin(self):
        os.chmod(os.path.join(self.tok_dir, "gate-token"), 0o644)
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("chmod 600", msg)


if __name__ == "__main__":
    unittest.main()
