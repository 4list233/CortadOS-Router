"""GET /dashboard: a static page with security headers and no secret; the data route still needs the token."""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router.provider import server, usage
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


# A minimal DOM for node: enough of document/sessionStorage for the page's script to define and run render(), which
# then prints every text node (marked [ERR] when its class carries err) and the header status line.
_NODE_HARNESS = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[1], "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>"));
function mk(tag) { const n = { tag, kids: [], className: "", style: {}, hidden: false, textContent: "",
  classList: { add(c) { n.className += " " + c; } }, appendChild(k) { n.kids.push(k); return k; },
  replaceChildren() { n.kids = []; }, addEventListener() {}, focus() {} }; return n; }
const ids = {}; global.document = { createElement: mk, getElementById: (i) => ids[i] || (ids[i] = mk("div")) };
global.sessionStorage = { getItem: () => "", setItem() {}, removeItem() {} };
global.setInterval = () => 1; global.clearInterval = () => {}; global.fetch = async () => ({});
eval(js + "\nglobal.render = render;");
function text(n, out) { if (n.textContent) out.push((n.className.includes("err") ? "[ERR] " : "") + n.textContent);
  n.kids.forEach(k => text(k, out)); return out; }
render(JSON.parse(fs.readFileSync(0, "utf8")));
console.log(text(ids.main, []).join("\n")); console.log("STATUS: " + ids.status.className + " " + ids.status.textContent);
"""


class DashboardSchemaTest(unittest.TestCase):
    """The page reads the merged usage view (usage._VIEW): each section it publishes, and errors[] shown, not hidden."""

    def test_page_reads_every_section(self):
        html = server._DASHBOARD_PATH.read_text()
        for key in ("d.lanes", "d.routed", "d.ledger", "d.bots", "d.child_tasks", "ct.by_client", "d.refusals", "d.s1",
                    "d.hardware", "d.notes", "d.errors", "s1.reason", "cache_read_tokens"):
            self.assertIn(key, html)
        self.assertNotIn("error_kind", html)        # the merged lane publishes `error` only
        self.assertEqual(set(usage._VIEW.fields) - {"generated_at"},
                         {"lanes", "routed", "refusals", "child_tasks", "ledger", "bots", "s1", "hardware", "notes"})

    @unittest.skipUnless(shutil.which("node"), "node not installed: the page's script is not executed")
    def test_render_real_views(self):
        from tests import test_usage as tu

        def render(view):
            r = subprocess.run(["node", "-e", _NODE_HARNESS, str(server._DASHBOARD_PATH)], input=json.dumps(view),
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        base = dict(scarcity_reader=lambda: tu.SCARCITY, refusals=[], now=tu.NOW,
                    s1_probe=lambda url: {"available": True, "status": 200, "latency_ms": 3})
        out = render(usage.collect(state_dir=tu.fixture_home(), **base))
        self.assertIn("Errors (0)", out)
        self.assertIn("[ERR] error: no_usage_lines", out)                        # a lane's error is shown on its card
        self.assertIn("[ERR] memory total: unavailable (FileNotFoundError)", out)  # an unavailable hardware field
        self.assertIn("grokbot", out); self.assertIn("route_task", out)          # child_tasks.by_client and bots
        self.assertIn("cache r", out); self.assertIn("HTTP 200", out)
        self.assertNotIn("[object Object]", out)

        def boom():
            raise OSError("no cache")
        empty = Path(tempfile.mkdtemp()); tu._TMP.append(empty)
        out = render(usage.collect(state_dir=empty, **{**base, "scarcity_reader": boom}))
        self.assertIn("[ERR] scarcity: OSError", out)                           # the errors list, at the top
        self.assertIn("[ERR] no lane readings (scarcity: OSError)", out)         # and at the empty lanes grid
        self.assertIn("[ERR] usage.db: absent", out)
        self.assertRegex(out, r"STATUS: err .* 6 errors")
        self.assertLess(out.index("scarcity: OSError"), out.index("Resources"))


if __name__ == "__main__":
    unittest.main()
