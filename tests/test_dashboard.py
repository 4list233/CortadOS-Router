"""GET /dashboard: a static page with security headers and no secret; the data route still needs the token."""
import re
import threading
import unittest
import urllib.error
import urllib.request

from cortados_router.provider import server
from cortados_router.provider.server import ProviderServer, Registry

TOKEN = "t-dashboard-secret"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.httpd.usage_view = lambda reg: {"lanes": []}
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def test_page_and_headers(self):
        with urllib.request.urlopen(self.base + "/dashboard", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/html", r.headers["content-type"])
            self.assertEqual(r.headers["cache-control"], "no-store")
            self.assertEqual(r.headers["content-security-policy"], server.DASHBOARD_HEADERS["Content-Security-Policy"])
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["referrer-policy"], "no-referrer")
            body = r.read().decode()
        self.assertNotIn(TOKEN, body)
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", body))   # no IP addresses
        self.assertIn("/v1/cortados/usage", body)

    def test_page_source_is_safe(self):
        html = server._DASHBOARD_PATH.read_text()
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("localStorage", html)
        self.assertIn("sessionStorage", html)
        self.assertNotRegex(html, r"(?i)<(script|link|img)[^>]+(src|href)=")   # no external assets

    def test_usage_still_needs_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/v1/cortados/usage", timeout=10)
        self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
