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
from unittest.mock import patch

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
        allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
        allowed.start()
        self.addCleanup(allowed.stop)
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

    def test_policy_rechecked_for_probe_and_inference_without_forwarding(self):
        from cortados_router import local_resources
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"CORTADOS_HOME": home}), \
             patch.object(local_resources, "_power_cache", None), \
             patch.object(local_resources, "command", return_value=b"Now drawing from 'AC Power'\n"), \
             patch.object(local_gate, "pro_local_allowed", side_effect=local_resources.pro_local_allowed):
            auth = {"x-api-key": TOKEN}
            code, raw = self.call("/api/tags", headers=auth)
            self.assertEqual(code, 503)
            self.assertIn("local switch off", raw)
            self.assertEqual(FakeOllama.seen, [])
            flag = Path(home) / "local-enabled"
            flag.touch()
            self.assertEqual(self.call("/api/tags", headers=auth)[0], 200)
            flag.unlink()
            code, raw = self.call("/v1/messages", self.MSG, headers=auth)
            self.assertEqual(code, 503)
            self.assertIn("local switch off", raw)
            self.assertEqual(FakeOllama.seen, [])
            flag.touch()
            with patch.object(local_resources, "_power_cache", None), \
                 patch.object(local_resources, "command", return_value=b"Now drawing from 'Battery Power'\n"):
                code, raw = self.call("/api/tags", headers=auth)
                self.assertEqual(code, 503)
                self.assertIn("on battery", raw)

    def test_policy_rechecked_after_upload(self):
        with patch.object(local_gate, "pro_local_allowed",
                          side_effect=[(True, "AC"), (False, "local switch off")]):
            code, raw = self.call("/v1/messages", self.MSG, headers={"x-api-key": TOKEN})
        self.assertEqual(code, 503)
        self.assertIn("local switch off", raw)
        self.assertEqual(FakeOllama.seen, [])

    def test_missing_or_wrong_token_is_401_without_echo(self):
        for headers in ({}, {"authorization": "Bearer nope"}, {"x-api-key": TOKEN[:-1]}, {"authorization": TOKEN}):
            code, raw = self.call("/v1/messages", {**self.MSG, "secret": "do-not-echo"}, headers=headers)
            self.assertEqual(code, 401)
            self.assertNotIn("do-not-echo", raw)
            self.assertNotIn("nope", raw)
        self.assertEqual(FakeOllama.seen, [])

    def test_a_refused_large_body_still_gets_the_401(self):
        import http.client
        body = json.dumps({**self.MSG, "pad": "x" * (2 * 2 ** 20)}).encode()     # 2 MiB, under max_body
        conn = http.client.HTTPConnection("127.0.0.1", self.gate.server_port, timeout=10)
        try:
            conn.request("POST", "/v1/messages", body=body,
                         headers={"content-type": "application/json", "authorization": "Bearer wrong"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 401)
            self.assertNotIn(b"pad", resp.read())
        finally:
            conn.close()

    def test_idle_connections_cannot_hold_every_slot(self):
        import socket
        import time
        self.gate.idle_timeout = 0.5
        idle = []
        for _ in range(local_gate.MAX_CONNECTIONS):       # every slot taken by a socket that sends nothing
            idle.append(socket.create_connection(("127.0.0.1", self.gate.server_port)))
        time.sleep(0.2)
        try:
            self.assertEqual(self.call("/api/tags", headers={"x-api-key": TOKEN})[0], 503)   # full right now
            deadline = time.time() + 5
            code = None
            while time.time() < deadline:
                time.sleep(0.2)
                code = self.call("/api/tags", headers={"x-api-key": TOKEN})[0]
                if code == 200:
                    break
            self.assertEqual(code, 200)                   # the idle ones timed out and gave their slots back
        finally:
            for sock in idle:
                sock.close()

    def _trickle(self, payload: bytes, interval: float, stop: threading.Event):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.gate.server_port))

        def run():
            try:
                for i in range(len(payload)):
                    if stop.is_set():
                        break
                    sock.sendall(payload[i:i + 1])
                    stop.wait(interval)
            except OSError:
                pass                                       # the gate closed it: what the deadline is for
        threading.Thread(target=run, daemon=True).start()
        return sock

    def _slots_freed_by_deadline(self, payload: bytes):
        import time
        self.gate.idle_timeout = 0.5                      # each trickled byte arrives well inside this
        stop = threading.Event()
        socks = [self._trickle(payload, 0.1, stop) for _ in range(local_gate.MAX_CONNECTIONS)]
        try:
            time.sleep(0.3)
            self.assertEqual(self.call("/api/tags", headers={"x-api-key": TOKEN})[0], 503)
            t0 = time.time()
            code = None
            while time.time() - t0 < 4:
                time.sleep(0.2)
                code = self.call("/api/tags", headers={"x-api-key": TOKEN})[0]
                if code == 200:
                    break
            self.assertEqual(code, 200)
            self.assertLess(time.time() - t0, 3)          # within the 1 s deadline plus slack, not the trickle's length
        finally:
            stop.set()
            for sock in socks:
                sock.close()

    def test_trickled_headers_hit_the_header_deadline(self):
        self.gate.header_deadline = 1.0
        self._slots_freed_by_deadline(b"GET /api/tags HTTP/1.1\r\nx-api-key: " + TOKEN.encode() + b"\r\n" + b"x" * 500)

    def test_trickled_body_hits_the_body_deadline(self):
        self.gate.body_deadline = 1.0
        head = (b"POST /v1/messages HTTP/1.1\r\nx-api-key: " + TOKEN.encode() +
                b"\r\ncontent-length: 1000\r\n\r\n")
        self.gate.header_deadline = 5.0
        self._slots_freed_by_deadline_after_head(head, b"x" * 1000)

    def test_trickled_refused_body_hits_the_drain_deadline_and_closes(self):
        import socket
        import time
        self.gate.drain_deadline = 1.0
        head = b"POST /v1/messages HTTP/1.1\r\nx-api-key: wrong\r\ncontent-length: 1000\r\n\r\n"
        self._slots_freed_by_deadline_after_head(head, b"x" * 1000)
        # and a drained-too-slowly request is closed without the 401
        self.gate.idle_timeout = 0.5
        sock = socket.create_connection(("127.0.0.1", self.gate.server_port))
        stop = threading.Event()
        try:
            sock.sendall(head)

            def run():
                try:
                    while not stop.is_set():
                        sock.sendall(b"x")
                        stop.wait(0.1)
                except OSError:
                    pass
            threading.Thread(target=run, daemon=True).start()
            sock.settimeout(4)
            t0 = time.time()
            got = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
            self.assertNotIn(b"401", got)
            self.assertLess(time.time() - t0, 3)
        finally:
            stop.set()
            sock.close()

    def _slots_freed_by_deadline_after_head(self, head: bytes, body: bytes):
        import socket
        import time
        self.gate.idle_timeout = 0.5
        stop = threading.Event()
        socks = []
        for _ in range(local_gate.MAX_CONNECTIONS):
            sock = socket.create_connection(("127.0.0.1", self.gate.server_port))
            sock.sendall(head)
            socks.append(sock)

            def run(sock=sock):
                try:
                    for i in range(len(body)):
                        if stop.is_set():
                            break
                        sock.sendall(body[i:i + 1])
                        stop.wait(0.1)
                except OSError:
                    pass
            threading.Thread(target=run, daemon=True).start()
        try:
            time.sleep(0.3)
            self.assertEqual(self.call("/api/tags", headers={"x-api-key": TOKEN})[0], 503)
            t0 = time.time()
            code = None
            while time.time() - t0 < 4:
                time.sleep(0.2)
                code = self.call("/api/tags", headers={"x-api-key": TOKEN})[0]
                if code == 200:
                    break
            self.assertEqual(code, 200)
            self.assertLess(time.time() - t0, 3)
        finally:
            stop.set()
            for sock in socks:
                sock.close()

    def test_expired_header_deadline_never_forwards_partial_headers(self):
        import socket
        import time
        self.gate.header_deadline = 0.5
        FakeOllama.gets = 0
        sock = socket.create_connection(("127.0.0.1", self.gate.server_port))
        try:
            # a whole, authorised request line and token header, but never the blank line that ends the headers
            sock.sendall(b"GET /api/tags HTTP/1.1\r\nx-api-key: " + TOKEN.encode() + b"\r\n")
            sock.settimeout(3)
            got = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
        finally:
            sock.close()
        time.sleep(0.2)
        self.assertEqual(FakeOllama.gets, 0)                # nothing was forwarded
        self.assertNotIn(b"HTTP/1.1 ", got)                 # and nothing answered: not even a 401
        self.assertEqual(self.call("/api/tags", headers={"x-api-key": TOKEN})[0], 200)

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


class TrainingHoldTests(GateHarness):
    """A live S1 training intent in the studio-gate dir refuses every forwarded request (temp dirs only)."""
    NONCE = "a" * 32

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "studio-gate"
        self.gate.studio_gate_dir = self.dir

    def write_intent(self, expires_in):
        import time
        self.dir.mkdir(exist_ok=True)
        now = time.time()
        (self.dir / f"intent-{os.getpid()}-{self.NONCE}.json").write_text(json.dumps(
            dict(version=1, pid=os.getpid(), nonce=self.NONCE, created_at=now - 1, expires_at=now + expires_in)))

    def test_no_dir_passes(self):
        code, _ = self.call("/api/tags", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 200)

    def test_live_intent_503_then_passes_after_expiry(self):
        import time
        self.write_intent(1.0)
        code, raw = self.call("/api/tags", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 503)
        self.assertIn("S1 training in progress", raw)
        self.assertNotIn(self.NONCE, raw)
        self.assertNotIn(str(self.dir), raw)
        n = len(FakeOllama.seen)
        code, _ = self.call("/v1/messages", GateTests.MSG, headers={"x-api-key": TOKEN})
        self.assertEqual(code, 503)
        self.assertEqual(len(FakeOllama.seen), n)          # nothing reached Ollama
        time.sleep(1.2)
        code, _ = self.call("/api/tags", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 200)

    @unittest.skipIf(os.geteuid() == 0, "root reads any directory")
    def test_unreadable_dir_fails_closed(self):
        self.dir.mkdir()
        os.chmod(self.dir, 0)
        self.addCleanup(os.chmod, self.dir, 0o700)
        code, raw = self.call("/api/tags", headers={"authorization": f"Bearer {TOKEN}"})
        self.assertEqual(code, 503)
        self.assertIn("unreadable", raw)
        self.assertNotIn(str(self.dir), raw)

    def test_unauthorized_still_401_not_hold(self):
        self.write_intent(60)
        code, _ = self.call("/api/tags", headers={"authorization": "Bearer wrong"})
        self.assertEqual(code, 401)


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
        for host in ("0.0.0.0", "::", "", "::0", "0:0:0:0:0:0:0:0", "[::]", " 0.0.0.0 "):
            with self.assertRaises(local_gate.GateConfigError):
                local_gate.check_listen(host, allow_any=False)
            self.assertIn(local_gate.check_listen(host, allow_any=True), ("0.0.0.0", "::"))
        # spellings the socket layer would resolve or normalise: refused even with --allow-any, because the address
        # bound must be the one checked (fullwidth digits bind as 0.0.0.0)
        for host in ("0", "0.0", "0x0", "\uff10.\uff10.\uff10.\uff10", "\uff11\uff12\uff17.0.0.1", "pro.example",
                     "localhost"):
            for allow in (False, True):
                with self.assertRaises(local_gate.GateConfigError, msg=host):
                    local_gate.check_listen(host, allow_any=allow)
        for host, bound in (("127.0.0.1", "127.0.0.1"), ("100.64.0.1", "100.64.0.1"), ("fd7a::1", "fd7a::1"),
                            ("[FD7A::1]", "fd7a::1")):
            self.assertEqual(local_gate.check_listen(host, allow_any=False), bound)
        self.assertEqual(local_gate.main(["--listen", "\uff10.\uff10.\uff10.\uff10", "--token-file", token_file(self.dir),
                                          "--print-config"]), 2)
        self.assertEqual(local_gate.main(["--listen", "0.0.0.0", "--token-file", token_file(self.dir)]), 2)

    def test_limits_must_be_finite_and_positive(self):
        tf = token_file(self.dir)
        for flag in ("--connect-timeout", "--read-timeout", "--max-body-bytes", "--max-connections"):
            for bad in ("-1", "0", "nan", "inf"):
                if flag.startswith("--max") and bad in ("nan", "inf"):
                    continue                              # int flags: argparse refuses those itself
                self.assertEqual(local_gate.main(["--listen", "127.0.0.1", "--token-file", tf, "--print-config",
                                                  flag, bad]), 2, (flag, bad))
        for key in ("connect_timeout", "read_timeout", "idle_timeout", "header_deadline", "body_deadline",
                    "drain_deadline"):
            for bad in (-1, 0, float("nan"), float("inf")):
                with self.assertRaises(local_gate.GateConfigError, msg=(key, bad)):
                    local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream="http://127.0.0.1:9", **{key: bad})

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
        allowed = patch.object(local_gate, "pro_local_allowed", return_value=(True, "test AC + on"))
        allowed.start()
        self.addCleanup(allowed.stop)
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

    def test_disabled_gate_reason_reaches_provider(self):
        with patch.object(local_gate, "pro_local_allowed", return_value=(False, "local disabled: on battery")):
            code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("local disabled: on battery", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_a_refusal_after_probe_rechecks_policy(self):
        with patch.object(local_lane, "probe", return_value={TAG}), \
             patch.object(local_gate, "pro_local_allowed", return_value=(False, "local switch off")):
            code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("local switch off", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_a_refused_token_is_a_clear_503(self):
        self.gate.token = b"b" * 40
        code, msg = self.error_of(self.BODY)
        self.assertEqual(code, 503)
        self.assertIn("local gate on node pro refused the provider token", msg)
        self.assertEqual(FakeOllama.seen, [])

    def test_outcome_is_logged_once_per_conversation(self):
        import time
        logged = []
        self.reg.log_outcome = lambda route, chars, ms: logged.append((route["route_id"], chars, ms))
        body = {**self.BODY, "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}
        got = self.post(body)
        follow = {**body, "messages": body["messages"] + [
            {"role": "assistant", "content": got["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": got["content"][0]["id"], "content": "ok"}]}]}
        self.post(follow)
        deadline = time.time() + 3
        while not logged and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        self.assertEqual(len(logged), 1)
        rid, chars, ms = logged[0]
        self.assertTrue(rid.startswith("execution:provider:pin:"))
        self.assertEqual(chars, 0)                       # a tool_use reply has no text
        self.assertGreaterEqual(ms, 0)

    def test_text_reply_chars_are_logged(self):
        import time
        logged = []
        self.reg.log_outcome = lambda route, chars, ms: logged.append(chars)
        orig = FakeOllama.do_POST

        def text_only(handler):                          # a tools conversation answered in text
            n = int(handler.headers["content-length"])
            b = json.loads(handler.rfile.read(n))
            b.pop("tools", None)
            raw = json.dumps(b).encode()
            handler.headers.replace_header("content-length", str(len(raw)))
            import io
            handler.rfile = io.BytesIO(raw)
            orig(handler)
        FakeOllama.do_POST = text_only
        try:
            self.post({**self.BODY, "tools": [{"name": "Read", "input_schema": {"type": "object"}}]})
        finally:
            FakeOllama.do_POST = orig
        deadline = time.time() + 3
        while not logged and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(logged, [len("hello from qwen")])

    def test_dropped_request_after_connect_is_not_called_asleep(self):
        import socket
        lsn = socket.socket()
        lsn.bind(("127.0.0.1", 0))
        lsn.listen(4)

        def accept_and_drop():
            for _ in range(2):
                try:
                    c, _ = lsn.accept()
                except OSError:
                    return
                c.close()
        threading.Thread(target=accept_and_drop, daemon=True).start()
        from cortados_router.provider import local_lane as ll
        node = {"node": "pro", "base_url": f"http://127.0.0.1:{lsn.getsockname()[1]}", "token": None,
                "connect_timeout_s": 1, "read_timeout_s": 2}

        class H:
            sent = []
            def send_response(self, code): self.code = code
            def send_header(self, *a): pass
            def end_headers(self): pass
            class wfile:
                buf = b""
                @classmethod
                def write(cls, b): cls.buf += b
        h = H()
        try:
            out = ll.serve(h, {"messages": []}, node, {"id": TAG, "alias": "qwen-27b"}, echo_model="sub/qwen-27b")
        finally:
            lsn.close()
        self.assertEqual((out, h.code), ("unreachable", 503))
        msg = json.loads(h.wfile.buf)["error"]["message"]
        self.assertIn("accepted the connection", msg)
        self.assertNotIn("asleep", msg)

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
        self.assertIn("local node pro is misconfigured on the provider (see provider log)", msg)
        self.assertNotIn(self.tok_dir, msg)                 # the provider's file paths never reach the client
        self.assertNotIn("chmod", msg)


if __name__ == "__main__":
    unittest.main()
