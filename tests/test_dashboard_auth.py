"""Dashboard password login (provider/dashboard_auth.py and its routes in provider/server.py).

Fakes only: a throwaway CORTADOS_HOME per test, a Registry with no backend, no vendor CLI, no real password anywhere
but this file's own test strings. Covers the CLI (0600 file, nothing secret printed), login success and failure, the
per-address lockout and the global cap, session expiry, persistence across a server restart, cookie flags, the session's
narrow scope (never /v1/messages, /v1/responses, heartbeats), CSRF refusals, logout and the unchanged owner token."""
import contextlib
import email.message
import http.client
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from cortados_router.provider import dashboard_auth as da
from cortados_router.provider import server
from cortados_router.provider.server import ProviderServer, Registry

TOKEN = "t-owner-token-dashboard-auth"
PASSWORD = "correct horse battery"          # a test string, never a real password
WRONG = "wrong horse battery!"


def run_cli(*argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = da.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"CORTADOS_HOME": self.home, "CORTADOS_SCARCITY_REFRESH": "off"})
        env.start()
        self.addCleanup(env.stop)


class CliTest(HomeCase):
    def test_set_status_clear(self):
        code, out, err = run_cli("set-password", "--stdin", stdin=PASSWORD + "\n")
        self.assertEqual(code, 0, err)
        p = da.auth_path()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        raw = p.read_text()
        doc = json.loads(raw)
        self.assertNotIn(PASSWORD, raw)
        if da.scrypt_available():
            self.assertEqual((doc["kdf"], doc["n"], doc["r"], doc["p"], doc["dklen"]), ("scrypt", 2 ** 15, 8, 1, 32))
        else:   # Apple's LibreSSL Python 3.9 (the Air's provider): the recorded fallback, said in the output
            self.assertEqual((doc["kdf"], doc["iterations"], doc["dklen"]), ("pbkdf2_sha256", 600_000, 32))
            self.assertIn("no hashlib.scrypt", out)
        self.assertEqual(len(bytes.fromhex(doc["salt"])), 16)
        self.assertTrue(da.verify_password(PASSWORD, doc))
        self.assertFalse(da.verify_password(WRONG, doc))
        code, sout, serr = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("set", sout)
        for text in (out, err, sout, serr):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(doc["hash"], text)
            self.assertNotIn(doc["salt"], text)
        # no stray temp file is left beside it, and the sessions file is 0600 too
        self.assertEqual(sorted(n for n in os.listdir(self.home) if not n.endswith(".lock")),
                         ["dashboard-auth.json", "dashboard-sessions.json"])
        self.assertEqual(stat.S_IMODE(os.stat(da.sessions_path()).st_mode), 0o600)
        code, out, _ = run_cli("clear-password")
        self.assertEqual(code, 0)
        self.assertFalse(p.exists())
        self.assertIn("not set", run_cli("status")[1])

    def test_refuses_without_tty_or_short(self):
        code, _, err = run_cli("set-password", stdin=PASSWORD + "\n")   # a StringIO stdin is not a TTY
        self.assertEqual(code, 1)
        self.assertIn("terminal", err)
        self.assertFalse(da.auth_path().exists())
        code, _, err = run_cli("set-password", "--stdin", stdin="short\n")
        self.assertEqual(code, 1)
        self.assertIn("12", err)
        self.assertNotIn("short", err.replace("shorter", ""))
        self.assertFalse(da.auth_path().exists())

    def test_tty_prompts_twice_and_must_match(self):
        with mock.patch("sys.stdin") as si, mock.patch("sys.stdout") as so:
            si.isatty.return_value = so.isatty.return_value = True
            with mock.patch("getpass.getpass", side_effect=[PASSWORD, PASSWORD + "x"]):
                self.assertEqual(da.main(["set-password"]), 1)
            self.assertFalse(da.auth_path().exists())
            with mock.patch("getpass.getpass", side_effect=[PASSWORD, PASSWORD]):
                self.assertEqual(da.main(["set-password"]), 0)
        self.assertTrue(da.verify_password(PASSWORD, json.loads(da.auth_path().read_text())))

    def test_logout_all_and_password_change_end_sessions(self):
        run_cli("set-password", "--stdin", stdin=PASSWORD + "\n")
        auth = da.DashboardAuth()
        r = auth.login(PASSWORD, "100.64.0.1")
        cookie = f"{da.COOKIE_NAME}={r['cookie']}"
        self.assertIsNotNone(auth.session(cookie))
        self.assertEqual(run_cli("logout-all")[0], 0)
        self.assertIsNone(auth.session(cookie))
        r = auth.login(PASSWORD, "100.64.0.1")
        cookie = f"{da.COOKIE_NAME}={r['cookie']}"
        # a new password: the old session's generation no longer matches, even if the sessions file were kept
        saved = da.sessions_path().read_text()
        run_cli("set-password", "--stdin", stdin=PASSWORD + " two\n")
        da.write_private(da.sessions_path(), json.loads(saved))
        self.assertIsNone(auth.session(cookie))

    def test_both_kdfs(self):
        with mock.patch.object(da, "scrypt_available", return_value=False):
            rec = da.hash_password(PASSWORD)
            self.assertEqual(rec["kdf"], "pbkdf2_sha256")
            self.assertTrue(da.verify_password(PASSWORD, da.validate_record(rec)))
            self.assertFalse(da.verify_password(WRONG, rec))
            # a scrypt record on a Python without scrypt fails closed, naming the fix
            fake = dict(rec, kdf="scrypt", n=2 ** 15, r=8, p=1)
            with self.assertRaisesRegex(da.AuthFileError, "rerun set-password"):
                da.validate_record(fake)
        if not da.scrypt_available():
            self.skipTest("this Python has no hashlib.scrypt; the scrypt branch runs under a Python built on OpenSSL")
        rec = da.hash_password(PASSWORD)
        self.assertEqual((rec["kdf"], rec["n"], rec["r"], rec["p"]), ("scrypt", 2 ** 15, 8, 1))
        self.assertTrue(da.verify_password(PASSWORD, da.validate_record(rec)))
        self.assertFalse(da.verify_password(WRONG, rec))

    def test_corrupt_file_fails_closed(self):
        da.auth_path().write_text("{not json")
        auth = da.DashboardAuth()
        self.assertTrue(auth.password_set())
        self.assertEqual(auth.login(PASSWORD, "1.2.3.4")["status"], 500)
        self.assertEqual(run_cli("status")[0], 1)
        rec = da.hash_password(PASSWORD)
        for bad in ({"kdf": "scrypt", "n": 2 ** 30, "r": 8, "p": 1},   # gigabytes per login: refused unread
                    {"kdf": "pbkdf2_sha256", "iterations": 10 ** 9},      # minutes of CPU per login
                    {"kdf": "pbkdf2_sha256", "iterations": 1000},         # too cheap to protect anything
                    {"kdf": "md5"}):
            da.write_private(da.auth_path(), dict(rec, **bad))
            with self.assertRaises(da.AuthFileError, msg=str(bad)):
                da.DashboardAuth().record()


class ThrottleTest(unittest.TestCase):
    def test_address_lockout_grows_and_success_resets(self):
        now = [1000.0]
        t = da.LoginThrottle(lambda: now[0])
        for _ in range(da.IP_FAILURES):
            self.assertIsNone(t.blocked("a"))
            t.failed("a")
        secs, kind = t.blocked("a")
        self.assertEqual((secs, kind), (da.IP_LOCKOUT_S, "address"))
        self.assertIsNone(t.blocked("b"))        # another address is unaffected
        now[0] += da.IP_LOCKOUT_S
        self.assertIsNone(t.blocked("a"))
        for _ in range(da.IP_FAILURES):
            t.failed("a")
        self.assertEqual(t.blocked("a")[0], 2 * da.IP_LOCKOUT_S)   # the second lockout doubles
        now[0] += 2 * da.IP_LOCKOUT_S
        t.succeeded("a")
        for _ in range(da.IP_FAILURES):
            t.failed("a")
        self.assertEqual(t.blocked("a")[0], da.IP_LOCKOUT_S)       # a success reset the growth

    def test_global_cap(self):
        now = [0.0]
        t = da.LoginThrottle(lambda: now[0])
        for i in range(da.GLOBAL_FAILURES):
            self.assertIsNone(t.blocked(f"10.0.0.{i}"))
            t.failed(f"10.0.0.{i}")
        secs, kind = t.blocked("10.9.9.9")       # a fresh address is refused too
        self.assertEqual(kind, "global")
        self.assertEqual(secs, da.GLOBAL_WINDOW_S)
        now[0] += da.GLOBAL_WINDOW_S
        self.assertIsNone(t.blocked("10.9.9.9"))

    def test_bounded_addresses(self):
        t = da.LoginThrottle(lambda: 0.0)
        with mock.patch.object(da, "MAX_TRACKED_ADDRESSES", 3), mock.patch.object(da, "GLOBAL_FAILURES", 10 ** 6):
            for i in range(10):
                t.failed(f"a{i}")
        self.assertLessEqual(len(t.addresses), 3)


class HelperTest(unittest.TestCase):
    def headers(self, **kw):
        m = email.message.Message()
        for k, v in kw.items():
            m[k.replace("_", "-")] = v
        return m

    def test_same_origin(self):
        h = self.headers
        self.assertTrue(da.same_origin(h(host="air.example:8444", origin="https://air.example:8444"), "127.0.0.1"))
        self.assertFalse(da.same_origin(h(host="air.example:8444", origin="https://evil.example"), "127.0.0.1"))
        self.assertFalse(da.same_origin(h(host="air.example:8444"), "127.0.0.1"))            # neither header
        self.assertFalse(da.same_origin(h(host="air.example:8444", origin="null"), "127.0.0.1"))
        self.assertTrue(da.same_origin(h(host="air.example:8444", referer="https://air.example:8444/dashboard"),
                                       "127.0.0.1"))
        # X-Forwarded-Host (Tailscale Serve) counts only from a loopback peer
        fwd = h(host="127.0.0.1:8432", x_forwarded_host="air.example:8444", origin="https://air.example:8444")
        self.assertTrue(da.same_origin(fwd, "127.0.0.1"))
        self.assertFalse(da.same_origin(fwd, "100.64.0.9"))

    def test_client_address_and_cookie_values(self):
        self.assertEqual(da.client_address("127.0.0.1", "1.1.1.1, 100.64.0.5"), "100.64.0.5")
        self.assertEqual(da.client_address("100.64.0.9", "1.1.1.1"), "100.64.0.9")   # not loopback: XFF ignored
        self.assertEqual(da.client_address("127.0.0.1", "junk"), "127.0.0.1")
        v = "A" * 43
        self.assertEqual(da.cookie_values(f"x=1; {da.COOKIE_NAME}={v}; {da.COOKIE_NAME}=bad!"), [v])

    def test_cookie_flags(self):
        c = da.session_cookie("v" * 43)
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", f"Max-Age={30 * 24 * 3600}"):
            self.assertIn(flag, c)
        self.assertTrue(c.startswith("cortados_dash="))

    def test_session_scopes(self):
        s = da.Session("0" * 64, "c" * 43, 0)
        self.assertEqual(set(da.SESSION_SCOPES), {"dashboard:read", "local:switch"})
        for path in (da.LIVE_PATH, da.USAGE_PATH, da.TASKS_PATH, da.OFFICE_PATH, da.LOCAL_PATH):
            self.assertTrue(s.allows("GET", path), path)
        self.assertTrue(s.allows("POST", da.LOCAL_SWITCH_PATH))
        for m, path in (("POST", "/v1/messages"), ("POST", "/v1/responses"), ("POST", "/v1/cortados/live/heartbeat"),
                        ("POST", "/v1/cortados/office/heartbeat"), ("GET", "/v1/models"), ("GET", "/resources"),
                        ("POST", da.LIVE_PATH), ("GET", "/v1/cortados/live/x")):
            self.assertFalse(s.allows(m, path), (m, path))


class ServerCase(HomeCase):
    def setUp(self):
        super().setUp()
        self.start()

    def start(self):
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.httpd.usage_view = lambda reg: {"lanes": []}
        self.httpd.tasks_view = lambda reg: {"tasks": []}
        self.httpd.live_view = lambda reg: {"ok": True}
        self.httpd.office_view = lambda reg: {"floors": []}
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop, self.httpd)

    @staticmethod
    def stop(httpd):
        httpd.shutdown(); httpd.server_close()

    def origin(self):
        return f"http://127.0.0.1:{self.port}"

    def req(self, method, path, body=b"", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        finally:
            c.close()

    def set_password(self, pw=PASSWORD):
        da.write_private(da.auth_path(), da.hash_password(pw))

    def login(self, pw=PASSWORD, origin=None, xff=None):
        from urllib.parse import urlencode
        h = {"content-type": "application/x-www-form-urlencoded", "origin": origin or self.origin()}
        if xff:
            h["x-forwarded-for"] = xff
        return self.req("POST", "/dashboard/login", urlencode({"password": pw}).encode(), h)

    def cookie(self, headers):
        m = re.match(r"cortados_dash=([A-Za-z0-9_-]+);", headers["set-cookie"])
        return f"cortados_dash={m.group(1)}"


class LoginTest(ServerCase):
    def test_no_password_serves_token_page(self):
        status, h, body = self.req("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn(b'content="token"', body)
        self.assertIn(b"dashboard_auth set-password", body)
        self.assertIn("script-src 'sha256-", h["content-security-policy"])
        # no password: the login route has nothing to check
        self.assertEqual(self.login()[0], 404)

    def test_login_page_then_session_page(self):
        self.set_password()
        status, h, body = self.req("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(h["content-security-policy"], server.LOGIN_CSP)
        self.assertNotIn(b"<script", body.lower())
        self.assertIn(b'action="/dashboard/login"', body)
        self.assertIn(b'name="password"', body)
        # same-origin, not no-referrer: under no-referrer the browser posts the form with `Origin: null`
        self.assertEqual(h["referrer-policy"], "same-origin")
        status, h, _ = self.login()
        self.assertEqual(status, 303)
        self.assertEqual(h["location"], "/dashboard")
        sc = h["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"):
            self.assertIn(flag, sc)
        cookie = self.cookie(h)
        status, h, body = self.req("GET", "/dashboard", headers={"cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'content="session"', body)
        csrf = re.search(rb'id="cortados-csrf" name="cortados-csrf" content="([A-Za-z0-9_-]+)"', body).group(1).decode()
        stored = json.loads(da.sessions_path().read_text())["sessions"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["csrf"], csrf)
        self.assertNotIn(cookie.split("=", 1)[1], da.sessions_path().read_text())   # only its hash is kept
        self.assertEqual(stat.S_IMODE(os.stat(da.sessions_path()).st_mode), 0o600)
        # the CSP still pins the served script (the meta tags sit outside it)
        scripts = re.findall(rb"<script>(.*?)</script>", body, re.S)
        import base64, hashlib
        digest = base64.b64encode(hashlib.sha256(scripts[0]).digest()).decode()
        self.assertIn(f"'sha256-{digest}'", h["content-security-policy"])

    def test_login_page_headers_not_doubled(self):
        self.set_password()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/dashboard")
        r = c.getresponse(); r.read(); c.close()
        names = [k.lower() for k, _ in r.getheaders()]
        for n in ("referrer-policy", "content-security-policy", "cache-control", "x-frame-options"):
            self.assertEqual(names.count(n), 1, n)

    def test_wrong_password_logged_without_it(self):
        self.set_password()
        with self.assertLogs("cortados.provider.dashboard_auth", level="WARNING") as logs:
            status, _, body = self.login(WRONG)
        self.assertEqual(status, 401)
        self.assertIn(b"wrong password", body)
        self.assertNotIn(WRONG.encode(), body)
        text = "\n".join(logs.output)
        self.assertIn("dashboard login failed", text)
        self.assertNotIn(WRONG, text)
        self.assertNotIn(PASSWORD, text)

    def test_lockout_then_correct_password_still_refused(self):
        self.set_password()
        for _ in range(da.IP_FAILURES):
            self.assertEqual(self.login(WRONG, xff="100.64.0.7")[0], 401)
        status, h, _ = self.login(PASSWORD, xff="100.64.0.7")
        self.assertEqual(status, 429)
        self.assertEqual(int(h["retry-after"]), da.IP_LOCKOUT_S)
        self.assertNotIn("set-cookie", h)
        self.assertEqual(self.login(PASSWORD, xff="100.64.0.8")[0], 303)   # another address is unaffected

    def test_global_cap(self):
        self.set_password()
        with mock.patch.object(da, "GLOBAL_FAILURES", 4):
            for i in range(4):
                self.assertEqual(self.login(WRONG, xff=f"100.64.1.{i}")[0], 401)
            status, h, _ = self.login(PASSWORD, xff="100.64.2.1")
        self.assertEqual(status, 429)
        self.assertIn("retry-after", h)

    def test_cross_site_login_refused(self):
        self.set_password()
        self.assertEqual(self.login(origin="https://evil.example")[0], 403)
        status, _, _ = self.req("POST", "/dashboard/login", b"password=x",
                                {"content-type": "application/x-www-form-urlencoded"})   # no Origin, no Referer
        self.assertEqual(status, 403)
        status, _, _ = self.req("POST", "/dashboard/login", b"x" * (server.LOGIN_MAX_BYTES + 1),
                                {"content-type": "application/x-www-form-urlencoded", "origin": self.origin()})
        self.assertEqual(status, 413)

    def test_corrupt_auth_file_is_500_not_token_page(self):
        da.auth_path().write_text("[]")
        status, _, body = self.req("GET", "/dashboard")
        self.assertEqual(status, 500)
        self.assertNotIn(b'content="token"', body)


class SessionTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.set_password()
        status, h, _ = self.login()
        self.assertEqual(status, 303)
        self.ck = self.cookie(h)
        body = self.req("GET", "/dashboard", headers={"cookie": self.ck})[2]
        self.csrf = re.search(rb'name="cortados-csrf" content="([A-Za-z0-9_-]+)"', body).group(1).decode()

    def test_session_opens_the_views(self):
        for path in ("/v1/cortados/live", "/v1/cortados/usage", "/v1/cortados/tasks", "/v1/cortados/office",
                     "/v1/cortados/tasks?limit=5"):
            self.assertEqual(self.req("GET", path, headers={"cookie": self.ck})[0], 200, path)
        for path in ("/v1/cortados/live", "/v1/cortados/usage"):
            self.assertEqual(self.req("GET", path)[0], 401, path)     # no cookie: refused as before

    def test_session_never_reaches_model_or_write_endpoints(self):
        c = {"cookie": self.ck, "content-type": "application/json", "origin": self.origin(),
             da.CSRF_HEADER: self.csrf}
        body = json.dumps({"model": "cortados", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
        for path in ("/v1/messages", "/v1/responses", "/v1/messages/count_tokens", "/v1/cortados/office/heartbeat",
                     "/v1/cortados/live/heartbeat", "/bridge/call"):
            self.assertEqual(self.req("POST", path, body.encode(), c)[0], 401, path)
        for path in ("/v1/models", "/resources", "/v1/cortados/runs/abcdefgh12"):
            self.assertEqual(self.req("GET", path, headers=c)[0], 401, path)

    def test_token_header_is_judged_alone(self):
        self.assertEqual(self.req("GET", "/v1/cortados/live",
                                  headers={"cookie": self.ck, "authorization": "Bearer nope-nope-nope"})[0], 401)

    def test_owner_token_unchanged(self):
        for path in ("/v1/cortados/live", "/v1/cortados/usage", "/v1/cortados/tasks", "/v1/cortados/office",
                     "/v1/models"):
            self.assertEqual(self.req("GET", path, headers={"authorization": f"Bearer {TOKEN}"})[0], 200, path)
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"x-api-key": TOKEN})[0], 200)

    def test_expiry(self):
        self.reg.dashboard.clock = lambda: 10 ** 12           # far past the 30-day expiry
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"cookie": self.ck})[0], 401)
        self.assertIn(b'action="/dashboard/login"', self.req("GET", "/dashboard", headers={"cookie": self.ck})[2])

    def test_persists_across_restart(self):
        self.stop(self.httpd)
        self.start()                                           # a new Registry and server over the same home
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"cookie": self.ck})[0], 200)

    def test_logout_all_on_a_running_server(self):
        self.assertEqual(run_cli("logout-all")[0], 0)
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"cookie": self.ck})[0], 401)

    def test_logout_csrf(self):
        good = {"cookie": self.ck, "origin": self.origin(), da.CSRF_HEADER: self.csrf}
        for name, h in (("no token", {**good, da.CSRF_HEADER: ""}),
                        ("wrong token", {**good, da.CSRF_HEADER: "x" * 43}),
                        ("wrong origin", {**good, "origin": "https://evil.example"}),
                        ("no origin", {k: v for k, v in good.items() if k != "origin"})):
            self.assertEqual(self.req("POST", "/dashboard/logout", b"", h)[0], 403, name)
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"cookie": self.ck})[0], 200)
        self.assertEqual(self.req("POST", "/dashboard/logout", b"", {"origin": self.origin()})[0], 401)  # no cookie
        status, h, _ = self.req("POST", "/dashboard/logout", b"", good)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", h["set-cookie"])
        self.assertEqual(self.req("GET", "/v1/cortados/live", headers={"cookie": self.ck})[0], 401)
        self.assertEqual(json.loads(da.sessions_path().read_text())["sessions"], [])

    def test_get_does_not_write(self):
        before = os.stat(da.sessions_path())
        for path in ("/dashboard", "/v1/cortados/live"):
            self.req("GET", path, headers={"cookie": self.ck})
        after = os.stat(da.sessions_path())
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_owner_or_session_post_needs_csrf(self):
        """The helper the local switch will use: a POST on a session needs same origin and the CSRF token."""
        def handler(method, path, **hdrs):
            h = server.Handler.__new__(server.Handler)
            h.server, h.path, h.command, h.client_address, h._scoped = self.httpd, path, method, ("127.0.0.1", 1), None
            m = email.message.Message()
            for k, v in {"host": f"127.0.0.1:{self.port}", "cookie": self.ck, **hdrs}.items():
                m[k] = v
            h.headers = m
            return h
        ok = handler("POST", da.LOCAL_SWITCH_PATH, origin=self.origin(), **{da.CSRF_HEADER: self.csrf})
        self.assertTrue(ok._owner_or_session("POST"))
        bad = handler("POST", da.LOCAL_SWITCH_PATH, origin=self.origin())
        self.assertFalse(bad._owner_or_session("POST"))
        self.assertEqual(bad._auth_refusal[0], 403)
        self.assertFalse(handler("POST", da.LOCAL_SWITCH_PATH, origin="https://evil.example",
                                 **{da.CSRF_HEADER: self.csrf})._owner_or_session("POST"))
        self.assertTrue(handler("GET", da.LOCAL_PATH)._owner_or_session("GET"))
        self.assertFalse(handler("POST", "/v1/messages", origin=self.origin(),
                                 **{da.CSRF_HEADER: self.csrf})._owner_or_session("POST"))


if __name__ == "__main__":
    unittest.main()
