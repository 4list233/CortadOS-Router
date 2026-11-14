"""The local-models switch from anywhere: local_resources (one implementation), the gate's GET/POST
/cortados/local-switch, the provider's GET /v1/cortados/local and POST /v1/cortados/local/switch, the `local_switch`
MCP tool, and the `name` / `name:latest` normalisation of local_lane. Fakes only: an in-thread fake Ollama (with
/api/ps), a fake pmset (local_resources.command patched), a throwaway CORTADOS_HOME; never the real switch file."""
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cortados_router import local_gate, local_resources, subagent_mcp
from cortados_router.provider import clients, local_lane, local_switch, server
from tests.test_local_gate import TOKEN, token_file
from tests.test_provider import EndpointHarness, TOKEN as OWNER
from tests.test_provider_local import FakeOllama, LocalHarness, TAG, write_config

AC = b"Now drawing from 'AC Power'\n"
BATTERY = b"Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; AC attached; not charging\n"


class FakeOllamaPs(FakeOllama):
    """FakeOllama plus GET /api/ps (the loaded models): `loaded` names, or `ps_status` to answer an error."""
    loaded = [TAG]
    ps_status = 200
    ps_gets = 0

    def do_GET(self):
        if self.path != "/api/ps":
            return super().do_GET()
        FakeOllamaPs.ps_gets += 1
        raw = json.dumps({"models": [{"name": n, "model": n, "size": 123, "digest": "d" * 64}
                                     for n in self.loaded]}).encode()
        self.send_response(self.ps_status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Home(unittest.TestCase):
    """A throwaway CORTADOS_HOME and a fake pmset (AC unless a test sets self.power)."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        for p in (patch.dict(os.environ, {"CORTADOS_HOME": self.home.name}),
                  patch.object(local_resources, "_power_cache", None),
                  patch.object(local_resources, "POWER_CACHE_S", 0.0),
                  patch.object(local_resources, "command", side_effect=lambda *a, **k: self.power)):
            p.start()
            self.addCleanup(p.stop)
        self.power = AC
        self.flag = Path(self.home.name) / "local-enabled"
        self.assertNotEqual(local_resources.local_switch_path(), Path("~/.cortados/local-enabled").expanduser())


class SwitchImplementationTests(Home):
    def test_on_off_is_atomic_and_idempotent(self):
        local_resources.set_local_switch(True)
        self.assertTrue(self.flag.is_file())
        self.assertEqual(oct(self.flag.stat().st_mode & 0o777), "0o600")
        local_resources.set_local_switch(True)                 # already on: unchanged
        self.assertEqual([p.name for p in Path(self.home.name).iterdir()], ["local-enabled"])   # no temp file left
        local_resources.set_local_switch(False)
        local_resources.set_local_switch(False)                # already off: fine
        self.assertFalse(self.flag.exists())

    def test_on_creates_a_missing_home(self):
        nested = Path(self.home.name) / "new" / "home"
        with patch.dict(os.environ, {"CORTADOS_HOME": str(nested)}):
            local_resources.set_local_switch(True)
            self.assertTrue((nested / "local-enabled").is_file())

    def test_status_reports_power_and_works_while_off_or_on_battery(self):
        self.assertEqual(local_resources.local_switch_status(),
                         {"switch": "off", "allowed": False, "reason": "local switch off", "power": "ac"})
        self.flag.touch()
        self.power = BATTERY
        self.assertEqual(local_resources.local_switch_status(),
                         {"switch": "on", "allowed": False, "reason": "local disabled: on battery", "power": "battery"})
        self.power = b"garbage"
        self.assertEqual(local_resources.local_switch_status()["power"], "unknown")

    def test_cli_uses_the_same_code(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(local_resources, "set_local_switch",
                                                           wraps=local_resources.set_local_switch) as setter:
            local_resources.main(["toggle", "on"])
        setter.assert_called_once_with(True)
        self.assertEqual(json.loads(out.getvalue())["power"], "ac")


class GateSwitchTests(Home):
    def setUp(self):
        super().setUp()
        self.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaPs)
        threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        FakeOllama.seen, FakeOllamaPs.loaded, FakeOllamaPs.ps_status, FakeOllamaPs.ps_gets = [], [TAG], 200, 0
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.ollama.server_port}", read_timeout=5)
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.gate.server_port}"

    def tearDown(self):
        for s in (self.gate, self.ollama):
            if s is not None:
                s.shutdown()
                s.server_close()

    def call(self, method, body=None, token=TOKEN, path=local_gate.SWITCH_PATH, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})}
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_auth_required_before_anything(self):
        for method, body in (("GET", None), ("POST", {"on": True})):
            for token in (None, "nope", TOKEN[:-1]):
                code, data = self.call(method, body, token=token)
                self.assertEqual(code, 401)
                self.assertNotIn(TOKEN, json.dumps(data))
        self.assertFalse(self.flag.exists())
        self.assertEqual(FakeOllamaPs.ps_gets, 0)

    def test_status_answers_while_off_and_on_battery(self):
        self.power = BATTERY
        code, data = self.call("GET")
        self.assertEqual(code, 200)
        self.assertEqual(data, {"switch": "off", "allowed": False, "reason": "local switch off", "power": "battery",
                                "training": False, "models_loaded": [TAG], "ollama": "up"})
        self.flag.touch()
        code, data = self.call("GET")
        self.assertEqual((data["switch"], data["allowed"], data["reason"]), ("on", False, "local disabled: on battery"))
        # inference keeps its refusal
        code, _ = self.call("GET", path="/api/tags")
        self.assertEqual(code, 503)
        self.assertEqual(FakeOllama.seen, [])

    def test_post_on_then_off(self):
        code, data = self.call("POST", {"on": True})
        self.assertEqual((code, data["switch"], data["allowed"], data["power"]), (200, "on", True, "ac"))
        self.assertTrue(self.flag.is_file())
        self.assertEqual(self.call("GET", path="/api/tags")[0], 200)          # now admitted
        code, data = self.call("POST", {"on": False})
        self.assertEqual((code, data["switch"], data["allowed"], data["reason"]), (200, "off", False, "local switch off"))
        self.assertFalse(self.flag.exists())
        self.assertEqual(self.call("GET", path="/api/tags")[0], 503)

    def test_bad_bodies_change_nothing(self):
        for raw in (b'{"on": "yes"}', b'{"on": 1}', b"[]", b"not json", b"{}"):
            code, data = self.call("POST", raw=raw)
            self.assertEqual(code, 400, raw)
            self.assertIn('{"on": true}', data["error"]["message"])
        code, _ = self.call("POST", raw=b'{"on": true, "pad": "' + b"x" * 2000 + b'"}')
        self.assertEqual(code, 413)
        self.assertFalse(self.flag.exists())
        self.assertEqual(self.call("PUT", {"on": True})[0], 405)
        self.assertFalse(self.flag.exists())

    def test_models_unknown_when_ollama_is_down_or_odd(self):
        FakeOllamaPs.ps_status = 500
        self.assertEqual(self.call("GET")[1]["ollama"], "unreadable")
        self.ollama.shutdown()
        self.ollama.server_close()
        self.ollama = None
        code, data = self.call("GET")
        self.assertEqual((code, data["models_loaded"], data["ollama"]), (200, None, "unreachable"))

    def test_only_names_leave_the_node(self):
        data = self.call("GET")[1]
        self.assertNotIn("digest", json.dumps(data))
        self.assertNotIn("size", json.dumps(data))

    def test_api_ps_is_not_a_forwarded_route(self):
        self.flag.touch()
        self.assertEqual(self.call("GET", path="/api/ps")[0], 404)

    def test_switch_failure_is_a_500_without_detail(self):
        with patch.object(local_resources, "set_local_switch", side_effect=PermissionError("/secret/path")):
            code, data = self.call("POST", {"on": True})
        self.assertEqual(code, 500)
        self.assertNotIn("/secret/path", json.dumps(data))

    def test_training_hold_is_shown(self):
        with patch.object(local_gate, "training_hold", return_value="S1 training in progress"):
            self.assertTrue(self.call("GET")[1]["training"])


class ProviderSwitchTests(EndpointHarness):
    """GET /v1/cortados/local and POST /v1/cortados/local/switch through a real gate on loopback."""

    def setUp(self):
        super().setUp()
        self.home = Home("setUp")
        self.home.setUp()
        self.addCleanup(self.home.doCleanups)
        self.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaPs)
        threading.Thread(target=self.ollama.serve_forever, daemon=True).start()
        FakeOllamaPs.loaded, FakeOllamaPs.ps_status = [TAG], 200
        self.gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN,
                                          upstream=f"http://127.0.0.1:{self.ollama.server_port}", read_timeout=5)
        threading.Thread(target=self.gate.serve_forever, daemon=True).start()
        self.tok_dir = tempfile.mkdtemp()
        env = patch.dict(os.environ, {local_lane.CONFIG_ENV: write_config(
            self.scratch, f"http://127.0.0.1:{self.gate.server_port}", token_file=token_file(self.tok_dir),
            auto_route={"model": "qwen-27b", "bands": ["R0"]})})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(os.environ.pop, "TEST_LOCAL_PRO_URL", None)

    def tearDown(self):
        for s in (self.gate, self.ollama):
            if s is not None:
                s.shutdown()
                s.server_close()
        super().tearDown()

    def get(self, path=local_switch.VIEW_PATH, token=OWNER):
        req = urllib.request.Request(self.url + path, headers={"authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def switch(self, body, token=OWNER):
        try:
            return 200, self.post(body, token=token, path=local_switch.SWITCH_PATH)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_owner_token_required(self):
        self.assertEqual(self.get(token=None)[0], 401)
        self.assertEqual(self.get(token="wrong")[0], 401)
        self.assertEqual(self.switch({"node": "pro", "on": True}, token="wrong")[0], 401)
        self.assertFalse(self.home.flag.exists())

    def test_a_scoped_client_token_is_refused(self):
        reader = clients.Client(name="pulse", scopes=frozenset({"live:read"}))
        with patch.object(self.reg.clients, "match", return_value=reader):
            self.assertEqual(self.get(token="x" * 20)[0], 403)
            self.assertEqual(self.switch({"node": "pro", "on": True}, token="x" * 20)[0], 403)
        self.assertFalse(self.home.flag.exists())

    def test_status_while_off_and_on_battery(self):
        self.home.power = BATTERY
        code, data = self.get()
        self.assertEqual(code, 200)
        self.assertEqual(data["nodes"], [{"node": "pro", "label": "Pro",
                                          "auto_route": {"model": "qwen-27b", "bands": ["R0"]},
                                          "models": ["qwen-27b", "phi:latest"], "reachable": True, "error": None,
                                          "switch": "off", "allowed": False, "reason": "local switch off",
                                          "power": "battery", "training": False, "ollama": "up",
                                          "models_loaded": [TAG]}])
        raw = json.dumps(data)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("127.0.0.1", raw)                      # never the node's address
        self.assertNotIn(self.tok_dir, raw)

    def test_on_off_through_the_gate(self):
        code, row = self.switch({"node": "pro", "on": True})
        self.assertEqual((code, row["switch"], row["allowed"]), (200, "on", True))
        self.assertTrue(self.home.flag.is_file())
        code, row = self.switch({"node": "PRO", "on": False})   # node ids are matched case-insensitively
        self.assertEqual((code, row["switch"], row["allowed"]), (200, "off", False))
        self.assertFalse(self.home.flag.exists())

    def test_unknown_node_and_bad_bodies(self):
        code, data = self.switch({"node": "studio", "on": True})
        self.assertEqual(code, 404)
        self.assertIn("unknown local node 'studio' (configured: pro)", data["error"]["message"])
        for body in ({"on": True}, {"node": "pro"}, {"node": "pro", "on": "true"}, {"node": 3, "on": True}):
            self.assertEqual(self.switch(body)[0], 400, body)
        self.assertFalse(self.home.flag.exists())

    def test_node_unreachable(self):
        self.gate.shutdown()
        self.gate.server_close()
        self.gate = None
        with socket.socket() as s:                              # a port with nothing listening
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        os.environ["TEST_LOCAL_PRO_URL"] = f"http://127.0.0.1:{port}"
        code, data = self.get()
        self.assertEqual(code, 200)
        row = data["nodes"][0]
        self.assertEqual((row["reachable"], row["switch"], row["allowed"]), (False, "unknown", None))
        self.assertIn("local node pro unreachable (ConnectionRefusedError)", row["error"])
        self.assertNotIn(str(port), json.dumps(data))
        code, data = self.switch({"node": "pro", "on": True})
        self.assertEqual(code, 503)
        self.assertIn("may be asleep or offline", data["error"]["message"])

    def test_gate_token_mismatch_and_old_gate(self):
        self.gate.token = b"b" * 40
        code, data = self.switch({"node": "pro", "on": True})
        self.assertEqual(code, 502)
        self.assertIn("refused the provider's token", data["error"]["message"])
        self.gate.token = TOKEN.encode()
        old_gate = lambda handler, method: handler._error(404, "not_found_error", "not a forwarded path")
        with patch.object(local_gate.GateHandler, "_switch", old_gate):   # a gate from before the endpoint
            code, data = self.switch({"node": "pro", "on": True})
        self.assertEqual(code, 502)
        self.assertIn("older local_gate.py", data["error"]["message"])
        self.assertFalse(self.home.flag.exists())

    def test_no_gate_configured(self):
        os.environ[local_lane.CONFIG_ENV] = write_config(self.scratch, f"http://127.0.0.1:{self.gate.server_port}")
        code, data = self.switch({"node": "pro", "on": True})
        self.assertEqual(code, 409)
        self.assertIn("no local gate configured", data["error"]["message"])
        self.assertFalse(self.get()[1]["nodes"][0]["reachable"])

    def test_misconfigured_node_names_the_node_only(self):
        os.environ["TEST_LOCAL_PRO_URL"] = "not a url"
        code, data = self.get()
        self.assertEqual(code, 500)
        self.assertIn("local node pro is misconfigured", data["error"]["message"])
        self.assertNotIn(self.scratch, data["error"]["message"])

    def test_no_local_config(self):
        os.environ.pop(local_lane.CONFIG_ENV)
        code, data = self.get()
        self.assertEqual((code, data["nodes"]), (200, []))

    def test_gate_answer_is_cleaned_field_by_field(self):
        evil = {"switch": "maybe", "allowed": "yes", "reason": "x" * 500, "power": "nuclear", "training": 1,
                "ollama": "<b>", "models_loaded": ["ok:latest", "bad\nname", 7], "token": TOKEN}
        self.assertEqual(local_switch._clean(evil),
                         {"switch": "unknown", "allowed": None, "reason": None, "power": "unknown", "training": None,
                          "ollama": "unknown", "models_loaded": ["ok:latest"]})

    def raw_post(self, head: bytes, body: bytes = b"", trickle=None):
        """POST SWITCH_PATH with hand-written headers on a raw socket: (status, json or None, seconds)."""
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=20) as s:
            s.sendall(b"POST " + local_switch.SWITCH_PATH.encode() + b" HTTP/1.1\r\nhost: x\r\n"
                      b"authorization: Bearer " + OWNER.encode() + b"\r\ncontent-type: application/json\r\n"
                      + head + b"\r\n")
            try:
                if trickle is None:
                    s.sendall(body)
                else:
                    for i in range(len(body)):
                        s.sendall(body[i:i + 1])
                        time.sleep(trickle)
            except OSError:                             # the server answered and closed before the body was sent
                pass
            raw = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                raw += chunk
                if b"\r\n\r\n" in raw:
                    hdr, _, rest = raw.partition(b"\r\n\r\n")
                    n = next((int(l.split(b":")[1]) for l in hdr.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
                    if len(rest) >= n:
                        break
        hdr, _, rest = raw.partition(b"\r\n\r\n")
        return int(hdr.split(b" ")[1]), json.loads(rest) if rest else None, time.monotonic() - start

    def test_switch_body_is_bounded(self):
        ok = json.dumps({"node": "pro", "on": True}).encode()
        code, data, _ = self.raw_post(b"", ok)                          # no content-length
        self.assertEqual(code, 411)
        for cl in (b"-5", b"abc", b"1e3", b""):
            code, data, _ = self.raw_post(b"content-length: " + cl + b"\r\n", ok)
            self.assertEqual(code, 400, cl)                              # present but not a byte count
        big = b'{"node": "pro", "on": true, "pad": "' + b"a" * (1 << 20) + b'"}'
        code, data, elapsed = self.raw_post(b"content-length: %d\r\n" % len(big), big)
        self.assertEqual(code, 413)
        self.assertIn("at most 1024 bytes", data["error"]["message"])
        code, data, _ = self.raw_post(b"content-length: 1025\r\n", b"")   # refused before a byte is read
        self.assertEqual(code, 413)
        code, data, _ = self.raw_post(b"content-length: 9\r\n", b"not json!")
        self.assertEqual(code, 400)
        code, data, _ = self.raw_post(b"content-length: 2\r\n", b"[]")
        self.assertEqual(code, 400)
        self.assertFalse(self.home.flag.exists())
        code, data, _ = self.raw_post(b"content-length: %d\r\n" % len(ok), ok)
        self.assertEqual((code, data["switch"]), (200, "on"))

    def test_switch_body_has_an_absolute_deadline(self):
        ok = json.dumps({"node": "pro", "on": True}).encode()
        with patch.object(local_switch, "SWITCH_BODY_DEADLINE_S", 0.3):
            code, data, elapsed = self.raw_post(b"content-length: %d\r\n" % len(ok), ok, trickle=0.05)
            self.assertEqual(code, 408)
            self.assertLess(elapsed, 1.5)                                # trickled whole, it would take ~1.4 s
            code, data, elapsed = self.raw_post(b"content-length: 500\r\n", b"{")   # stalls after one byte
            self.assertEqual(code, 408)
            self.assertLess(elapsed, 1.5)
        self.assertFalse(self.home.flag.exists())

    def test_endpoint_ids(self):
        self.assertEqual(server.endpoint_id(local_switch.VIEW_PATH), "local")
        self.assertEqual(server.endpoint_id(local_switch.SWITCH_PATH + "?x"), "local_switch")


class Trickle:
    """A fake HTTP peer on loopback that reads the request, then answers one byte every `gap` seconds forever: a
    status line and headers when `headers` is False starts it trickling there, else a 200 header with a long body.
    Each byte resets a plain socket timeout, so only an end-to-end deadline stops a client."""

    def __init__(self, gap=0.05, in_headers=False):
        self.gap, self.in_headers = gap, in_headers
        self.stop = threading.Event()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        with c:
            try:
                c.settimeout(5)
                c.recv(65536)
                if not self.in_headers:
                    c.sendall(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 100000\r\n\r\n")
                    data = b"{" + b" " * 99999
                else:
                    data = b"HTTP/1.1 200 OK\r\n" + b"x-pad: " + b"a" * 100000
                for i in range(len(data)):
                    if self.stop.is_set():
                        return
                    c.sendall(data[i:i + 1])
                    time.sleep(self.gap)
            except OSError:
                return

    def close(self):
        self.stop.set()
        self.sock.close()


class DeadlineCallTests(unittest.TestCase):
    """local_gate.deadline_call: one monotonic deadline for connect, headers and body (review fix: the socket timeout
    alone reset on every byte, so a trickling gate exceeded a 0.1 s budget)."""

    def trickle(self, **kw):
        t = Trickle(**kw)
        self.addCleanup(t.close)
        return t

    def test_trickling_body_and_headers_stop_at_the_deadline(self):
        import http.client
        for in_headers in (False, True):
            t = self.trickle(in_headers=in_headers)
            conn = http.client.HTTPConnection("127.0.0.1", t.port)
            start = time.monotonic()
            with self.assertRaises((OSError, http.client.HTTPException)):
                local_gate.deadline_call(conn, 0.3, "GET", "/x", max_bytes=1 << 20)
            conn.close()
            self.assertLess(time.monotonic() - start, 1.0, in_headers)

    def test_answers_within_the_deadline_and_caps_the_body(self):
        import http.client
        ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllamaPs)
        threading.Thread(target=ollama.serve_forever, daemon=True).start()
        self.addCleanup(ollama.server_close)
        self.addCleanup(ollama.shutdown)
        FakeOllamaPs.loaded, FakeOllamaPs.ps_status = [TAG], 200
        conn = http.client.HTTPConnection("127.0.0.1", ollama.server_port)
        status, raw = local_gate.deadline_call(conn, 5, "GET", "/api/ps", max_bytes=1 << 20)
        self.assertEqual((status, json.loads(raw)["models"][0]["name"]), (200, TAG))
        conn = http.client.HTTPConnection("127.0.0.1", ollama.server_port)
        status, raw = local_gate.deadline_call(conn, 5, "GET", "/api/ps", max_bytes=10)
        self.assertEqual((status, len(raw)), (200, 10))

    def test_connect_failure_is_reported_apart(self):
        import http.client
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        seen = []
        with self.assertRaises(OSError):
            local_gate.deadline_call(http.client.HTTPConnection("127.0.0.1", port), 1, "GET", "/", max_bytes=10,
                                     on_connect_error=seen.append)
        self.assertEqual(len(seen), 1)


class TrickleTimeoutTests(Home):
    """The gate's /api/ps read and the provider's gate call keep their whole-call bounds against a trickling peer."""

    def test_gate_status_with_a_trickling_ollama(self):
        t = Trickle()
        self.addCleanup(t.close)
        gate = local_gate.GateServer(("127.0.0.1", 0), token=TOKEN, upstream=f"http://127.0.0.1:{t.port}",
                                     read_timeout=5)
        threading.Thread(target=gate.serve_forever, daemon=True).start()
        self.addCleanup(gate.server_close)
        self.addCleanup(gate.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{gate.server_port}{local_gate.SWITCH_PATH}",
                                     headers={"authorization": f"Bearer {TOKEN}"})
        with patch.object(local_gate, "PS_TIMEOUT_S", 0.3):
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5)
        self.assertEqual((data["ollama"], data["models_loaded"]), ("unreachable", None))

    def test_provider_gate_call_with_a_trickling_gate(self):
        for in_headers in (False, True):
            t = Trickle(in_headers=in_headers)
            self.addCleanup(t.close)
            node = {"node": "pro", "base_url": f"http://127.0.0.1:{t.port}", "connect_timeout_s": 2.0,
                    "token": TOKEN}
            with patch.object(local_switch, "GATE_TIMEOUT_S", 0.3):
                start = time.monotonic()
                with self.assertRaises(local_switch.SwitchError) as cm:
                    local_switch._gate_call(node, "GET")
                elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0, in_headers)
            self.assertEqual(cm.exception.status, 503)
            self.assertIn("did not answer within 0.3s", str(cm.exception))


class McpLocalSwitchTests(unittest.TestCase):
    URL, TOK = "http://provider.test:8432", "owner-token-123456"

    def fake(self, answer, status=200):
        seen = []

        class Resp:
            def __init__(self, raw):
                self.raw = raw

            def read(self):
                return self.raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            seen.append((req.get_method(), req.full_url, req.data, dict(req.header_items()), timeout))
            raw = json.dumps(answer).encode()
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
            return Resp(raw)
        return urlopen, seen

    ROW = {"node": "pro", "label": "Pro", "switch": "on", "allowed": True}

    def test_listed(self):
        tools = subagent_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {})["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "local_switch")
        self.assertEqual(tool["inputSchema"]["properties"]["state"]["enum"], ["on", "off", "status"])

    def test_status_on_off(self):
        urlopen, seen = self.fake({"nodes": [self.ROW, {"node": "studio"}]})
        r = subagent_mcp.local_switch(self.URL, self.TOK, "status", urlopen=urlopen)
        self.assertEqual(seen[0][:2], ("GET", self.URL + "/v1/cortados/local"))
        self.assertEqual(seen[0][3]["X-api-key"], self.TOK)
        self.assertEqual(len(json.loads(r["content"][0]["text"])["nodes"]), 2)      # no node: every node
        r = subagent_mcp.local_switch(self.URL, self.TOK, "status", "pro", urlopen=urlopen)
        self.assertEqual(json.loads(r["content"][0]["text"])["nodes"], [self.ROW])
        r = subagent_mcp.local_switch(self.URL, self.TOK, "status", "air", urlopen=urlopen)
        self.assertTrue(r["isError"])
        urlopen, seen = self.fake(self.ROW)
        r = subagent_mcp.local_switch(self.URL, self.TOK, "off", urlopen=urlopen)
        self.assertEqual(seen[0][:3], ("POST", self.URL + "/v1/cortados/local/switch", b'{"node": "pro", "on": false}'))
        self.assertNotIn("isError", r)
        subagent_mcp.local_switch(self.URL, self.TOK, "on", "studio", urlopen=urlopen)
        self.assertEqual(json.loads(seen[1][2]), {"node": "studio", "on": True})

    def test_errors_are_results_and_scrubbed(self):
        urlopen, _ = self.fake({"error": {"message": f"unknown local node 'x' {self.TOK}"}}, status=404)
        r = subagent_mcp.local_switch(self.URL, self.TOK, "on", "x", urlopen=urlopen)
        self.assertTrue(r["isError"])
        self.assertIn("HTTP 404: unknown local node 'x' ***", r["content"][0]["text"])
        self.assertNotIn(self.TOK, r["content"][0]["text"])
        self.assertTrue(subagent_mcp.local_switch(self.URL, self.TOK, "maybe")["isError"])
        self.assertTrue(subagent_mcp.local_switch("", self.TOK, "status")["isError"])
        self.assertTrue(subagent_mcp.local_switch(self.URL, "short", "status")["isError"])

        def down(req, timeout=None):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        r = subagent_mcp.local_switch(self.URL, self.TOK, "status", urlopen=down)
        self.assertIn("could not reach the provider", r["content"][0]["text"])

    def test_allowed_inside_a_child_task_and_via_handle(self):
        urlopen, seen = self.fake({"nodes": [self.ROW]})
        with patch.dict(os.environ, {subagent_mcp.NESTED_ENV: "1"}), \
             patch.object(subagent_mcp.urllib.request, "urlopen", urlopen):
            msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "local_switch", "arguments": {"state": "status"}}}
            r = subagent_mcp.handle(msg, {"url": self.URL, "token": self.TOK})["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(len(seen), 1)
        r = subagent_mcp.handle(msg, {"url_error": "provider URL file x: not found"})["result"]
        self.assertTrue(r["isError"])


class TagNormalisationTests(unittest.TestCase):
    def test_normal_tag(self):
        for given, want in (("Qwen27b_heretic", "Qwen27b_heretic:latest"), ("Phi_4:latest", "Phi_4:latest"),
                            ("hf.co/x/Qwen-GGUF:Q4_K_M", "hf.co/x/Qwen-GGUF:Q4_K_M"),
                            ("registry:5000/ns/model", "registry:5000/ns/model:latest"), ("", "")):
            self.assertEqual(local_lane.normal_tag(given), want)

    def test_lists(self):
        tags = {"Qwen27b_heretic:latest", "hf.co/x/Q:Q4_K_M"}
        self.assertTrue(local_lane.lists(tags, "Qwen27b_heretic"))
        self.assertTrue(local_lane.lists(tags, "Qwen27b_heretic:latest"))
        self.assertTrue(local_lane.lists({"Phi_4"}, "Phi_4:latest"))
        self.assertFalse(local_lane.lists(tags, "Qwen27b_heretic:q8"))
        self.assertFalse(local_lane.lists(tags, "hf.co/x/Q"))
        self.assertFalse(local_lane.lists(tags, ""))

    def test_example_config_carries_explicit_tags(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "tables" / "resources.example.json").read_text())
        ids = [m["id"] for n in cfg["nodes"] for m in (n.get("ollama") or {}).get("models", [])]
        self.assertEqual(ids, ["Qwen27b_heretic:latest", "Huihui_Qwen_9b:latest", "Huihui_Qwen3.8:latest",
                               "Phi_4:latest"])


class UntaggedPinTests(LocalHarness):
    """A config id without its tag (`phi`) is served when the node lists `phi:latest`: the 503 "node pro does not list
    Qwen27b_heretic" of 2026-11-13 came from comparing the two literally."""

    def test_untagged_config_id_is_served(self):
        cfg = json.loads(Path(os.environ[local_lane.CONFIG_ENV]).read_text())
        cfg["nodes"][0]["ollama"]["models"] = [{"id": "phi", "alias": "phi-4"}]
        Path(os.environ[local_lane.CONFIG_ENV]).write_text(json.dumps(cfg))
        FakeOllama.tags = [TAG, "phi:latest", "phi"]           # Ollama serves `phi` as phi:latest
        self.addCleanup(setattr, FakeOllama, "tags", [TAG, "phi:latest"])
        out = self.post({"model": "sub/phi-4", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(out["stop_reason"], "end_turn")
        FakeOllama.tags = [TAG, "phi:q8"]                       # another tag is not the same model
        code, msg = self.error_of({"model": "sub/phi-4", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(code, 503)
        self.assertIn("does not list phi", msg)


if __name__ == "__main__":
    unittest.main()
