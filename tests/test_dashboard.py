"""GET /dashboard: a static page with security headers and no secret; the data route still needs the token."""
import base64
import hashlib
import json
import os
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


def _csp(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


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
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["referrer-policy"], "no-referrer")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            csp = _csp(r.headers["content-security-policy"])
            raw = r.read()
        body = raw.decode()
        # script-src pins the served page's one inline script by hash; nothing else may run
        scripts = re.findall(rb"<script>(.*?)</script>", raw, re.S)
        self.assertEqual(len(scripts), 1)
        digest = base64.b64encode(hashlib.sha256(scripts[0]).digest()).decode()
        self.assertEqual(csp["script-src"], [f"'sha256-{digest}'"])
        self.assertNotIn("'unsafe-inline'", csp["script-src"])
        self.assertEqual(csp["default-src"], ["'none'"])
        self.assertEqual(csp["connect-src"], ["'self'"])
        for d in ("base-uri", "form-action", "frame-ancestors"):
            self.assertEqual(csp[d], ["'none'"], d)
        self.assertNotIn(TOKEN, body)
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", body))   # no IP addresses
        self.assertIn("/v1/cortados/usage", body)
        self.assertIn("/v1/cortados/tasks", body)

    def test_tasks_still_needs_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/v1/cortados/tasks", timeout=10)
        self.assertEqual(cm.exception.code, 401)

    def test_page_source_is_safe(self):
        html = server._DASHBOARD_PATH.read_text()
        # every endpoint string goes in through textContent; no HTML or code sink exists to misuse
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setAttribute(", "eval(",
                     "Function(", "srcdoc", "javascript:", "localStorage"):
            self.assertNotIn(sink, html)
        self.assertIn("sessionStorage", html)
        # no external assets: no tag that loads or navigates, and no CSS import or url()
        self.assertNotRegex(html, r"(?i)<(script|link|img|iframe|object|embed|a|form)\b[^>]*\b(src|href|action|data)\s*=")
        self.assertNotRegex(html, r"(?i)<(iframe|object|embed)\b")
        self.assertNotRegex(html, r"(?i)@import|url\(")

    def test_page_without_exactly_one_script_is_refused(self):
        # the CSP pins one script by hash: a page with none, two, or a script the exact `<script>` match misses is a 500
        good = server._DASHBOARD_PATH.read_text()
        old_path = server._DASHBOARD_PATH
        def restore():
            server._DASHBOARD_PATH = old_path
            server._dashboard_cache.clear()
        self.addCleanup(restore)
        cases = {
            "zero": good.replace("<script>", "<!--").replace("</script>", "-->"),
            "two": good.replace("</body>", "<script>alert(1)</script></body>"),
            "upper": good.replace("</body>", "<SCRIPT>alert(1)</SCRIPT></body>"),
            "mixed": good.replace("</body>", "<ScRiPt src=x></sCrIpT></body>"),
            "only_upper_attr": good.replace("<script>", "<SCRIPT id=x>"),
            # decoys: a fake <script> the parser does not see as one, beside the real script that would run
            "comment_decoy": good.replace("<script>", "<!--<script>decoy</script>--><script id=real>", 1),
            "attribute_decoy": good.replace("<script>", '<div title="<script>decoy</script>"></div><script id=real>', 1),
            "cdata_decoy": good.replace("<script>", "<![CDATA[<script>decoy</script>]]><script id=real>", 1),
            "self_closing": good.replace("</body>", "<script/></body>"),
            "unclosed": good.replace("</script>", ""),
            "bogus_decl": good.replace("<body>", "<body><![bogus]>"),   # NotImplementedError inside 3.9's parser
        }
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        for name, page in cases.items():
            f = d / f"{name}.html"; f.write_text(page)
            server._DASHBOARD_PATH = f
            server._dashboard_cache.clear()
            with self.assertRaises(urllib.error.HTTPError, msg=name) as cm:
                urllib.request.urlopen(self.base + "/dashboard", timeout=10)
            self.assertEqual(cm.exception.code, 500, name)
            cm.exception.close()
        # a comment decoy beside a plain script is served, and the CSP hashes the script that runs, not the decoy
        f = d / "decoy_ok.html"
        f.write_text("<!--<script>decoy</script>--><SCRIPT>actual</SCRIPT>")
        server._DASHBOARD_PATH = f
        server._dashboard_cache.clear()
        with urllib.request.urlopen(self.base + "/dashboard", timeout=10) as r:
            csp = _csp(r.headers["content-security-policy"])
        want = base64.b64encode(hashlib.sha256(b"actual").digest()).decode()
        self.assertEqual(csp["script-src"], [f"'sha256-{want}'"])
        restore()
        with urllib.request.urlopen(self.base + "/dashboard", timeout=10) as r:   # the real page still serves
            self.assertEqual(r.status, 200)

    def test_usage_still_needs_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/v1/cortados/usage", timeout=10)
        self.assertEqual(cm.exception.code, 401)


# A minimal DOM for node: enough of document/sessionStorage/fetch for the page's script to run. Modes:
#   render        render(view) once
#   fetch500      a good render, then a poll answered 500
#   fetchthrow    a good render, then a poll whose fetch rejects
#   storagethrow  sessionStorage throws on every call; the login form is submitted, then the poll answers the view
#   storagelater  sessionStorage works at startup (token "tok"), then throws; the next poll still carries the token
#   tasks500      the usage poll is good; the tasks poll on the same token answers 500
# The tasks fetch (/v1/cortados/tasks, made after each good usage render) answers TASKS_VIEW from the environment (an
# empty view when unset) and is not counted in POLLS, which counts usage polls only.
# It prints every text node under main (marked [ERR] when its class carries err), then STATUS / BODY / STORAGE / LOGIN.
_NODE_HARNESS = r"""
const fs = require("fs");
const [page, mode] = process.argv.slice(1);
const view = JSON.parse(fs.readFileSync(0, "utf8"));
const tasksView = process.env.TASKS_VIEW ? JSON.parse(process.env.TASKS_VIEW) : {tasks: [], sessions: [], errors: []};
let tasksAuth = "", tasksPolls = 0;
const html = fs.readFileSync(page, "utf8");
const js = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>"));
function mk(tag) { const n = { tag, kids: [], className: "", style: {}, hidden: false, textContent: "", value: "", on: {},
  classList: { add(c) { if (!n.className.split(" ").includes(c)) n.className = (n.className + " " + c).trim(); },
               remove(c) { n.className = n.className.split(" ").filter(x => x && x !== c).join(" "); } },
  appendChild(k) { n.kids.push(k); return k; }, replaceChildren() { n.kids = []; },
  addEventListener(ev, f) { n.on[ev] = f; }, focus() {} }; return n; }
const ids = new Proxy({}, { get: (o, i) => o[i] || (o[i] = mk("div")) });
global.document = { createElement: mk, getElementById: (i) => ids[i], body: mk("body") };
const boom = () => { throw new Error("SecurityError"); };
let broken = mode === "storagethrow";
global.sessionStorage = { getItem: () => broken ? boom() : "tok", setItem: (k, v) => broken ? boom() : undefined,
                          removeItem: () => broken ? boom() : undefined };
global.setInterval = () => 1; global.clearInterval = () => {};
let auth = "", polls = 0;
const ok = { status: 200, ok: true, json: async () => view };
global.fetch = async (url, opts) => {
  if (url === "/v1/cortados/tasks") { tasksAuth = opts.headers.Authorization; tasksPolls++;
    if (mode === "tasks500") return { status: 500, ok: false };
    return { status: 200, ok: true, json: async () => tasksView }; }
  auth = opts.headers.Authorization; polls++;
  if (polls > 1 && mode === "fetch500") return { status: 500, ok: false };
  if (polls > 1 && mode === "fetchthrow") throw new Error("network down");
  return ok; };
const tick = () => new Promise(r => setImmediate(r));
(async () => {
  eval(js + "\nglobal.render = render; global.refresh = refresh;");
  await tick(); await tick();
  if (mode === "render") { render(view); }
  else if (mode === "storagethrow") {
    ids.tok.value = "typed-token"; ids.login.on.submit({ preventDefault() {} });
    await tick(); await tick(); await refresh(); await tick();
  } else if (mode === "storagelater") { broken = true; await refresh(); await tick(); }
  else { await refresh(); await tick(); }
  function text(n, out) { if (n.textContent) out.push((n.className.includes("err") ? "[ERR] " : "") + n.textContent);
    n.kids.forEach(k => text(k, out)); return out; }
  console.log(text(ids.main, []).join("\n"));
  console.log("STATUS: " + ids.status.className + " " + ids.status.textContent);
  console.log("BODY: " + document.body.className);
  console.log("STORAGE: " + (ids.storage.hidden ? "" : ids.storage.textContent));
  console.log("LOGIN: " + (ids.login.hidden ? "hidden" : "shown") + " AUTH: " + auth + " POLLS: " + polls);
  console.log("TASKS: AUTH: " + tasksAuth + " POLLS: " + tasksPolls);
})().catch(e => { console.error(e); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "node not installed: the page's script is not executed")
class DashboardRenderTest(unittest.TestCase):
    """The page reads the merged usage view (usage._VIEW): each section it publishes, and errors[] shown, not hidden."""

    def tmp(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def fixture(self) -> Path:
        from tests import test_usage as tu
        d = tu.fixture_home()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def view(self, home=None, **kw):
        from tests import test_usage as tu
        args = dict(state_dir=home or self.fixture(), scarcity_reader=lambda: tu.SCARCITY, refusals=[], now=tu.NOW,
                    s1_probe=lambda url: {"available": True, "status": 200, "latency_ms": 3})
        args.update(kw)
        return usage.collect(**args)

    def run_page(self, view, mode="render", tasks_view=None):
        env = dict(os.environ)
        if tasks_view is not None:
            env["TASKS_VIEW"] = json.dumps(tasks_view)
        r = subprocess.run(["node", "-e", _NODE_HARNESS, str(server._DASHBOARD_PATH), mode], input=json.dumps(view),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def tasks_fixture(self):
        """A tasks view collected from a router.db built on core.DDL (tests/test_tasks.py's builders)."""
        from tests import test_tasks as tt
        d = tt.home(); self.addCleanup(shutil.rmtree, d, True)
        tt.insert(d, turn_id="routed-1", session_id="session-aaaaaaaa-1111", ts=tt.NOW)
        tt.insert(d, turn_id="routed-2", session_id="session-aaaaaaaa-1111", ts=tt.NOW - 1)
        tt.pin(d, turn_id="pinned-1", session_id="session-aaaaaaaa-1111", ts=tt.NOW - 2)
        tt.pin(d, turn_id="pinned-2", session_id="session-bbbbbbbb-2222", ts=tt.NOW - 3)
        from cortados_router.provider import tasks
        return tasks.collect(d)

    def test_page_renders_tasks_after_the_usage_view(self):
        tv = self.tasks_fixture()
        out = self.run_page(self.view(), "refresh", tv)
        self.assertNotIn("SECRET", json.dumps(tv))
        self.assertIn("Tasks (recent routing decisions)", out)
        self.assertIn("latest 4 of at most 200 logged decisions · routed 50% (2 routed, 2 pinned, 0 unselected)", out)
        self.assertIn(tv["percentage_definition"], out)                          # the definition is stated on the page
        self.assertIn("67%", out); self.assertIn("0%", out)                      # session a: 2 of 3 routed; session b: pinned only
        self.assertIn("sessio…a-1111", out)                                      # ids are shortened, never dropped
        self.assertIn("tokens: not recorded", out)
        self.assertIn("claude-cli · sonnet", out); self.assertIn("anthropic/claude-sonnet", out); self.assertIn("1.2 s", out)
        self.assertIn("S1 decision; band R1; steps=1", out); self.assertIn("client pin; S1 not asked; band pin", out)
        self.assertNotIn("loading tasks", out)
        self.assertNotIn("[object Object]", out)
        self.assertIn("LOGIN: hidden AUTH: Bearer tok POLLS: 2", out)            # startup poll + the harness's refresh
        self.assertIn("TASKS: AUTH: Bearer tok POLLS: 2", out)                   # same token, one tasks poll per usage poll

    def test_tasks_errors_are_shown_in_their_section_only(self):
        d = self.tmp()
        from cortados_router.provider import tasks
        out = self.run_page(self.view(), "refresh", tasks.collect(d))               # no router.db at all
        self.assertIn("[ERR] router.db: absent", out)
        self.assertIn("latest 0 of at most 200 logged decisions · routed –", out)
        self.assertIn("Resources (% left)", out); self.assertNotIn("BODY: stale", out)   # the usage view is unaffected
        out = self.run_page(self.view(), "tasks500")
        self.assertIn("[ERR] tasks unavailable: error 500", out)
        self.assertIn("Resources (% left)", out); self.assertNotIn("BODY: stale", out)
        self.assertRegex(out, r"STATUS:  updated")                               # the header stays green: usage was good

    def test_page_reads_every_section(self):
        html = server._DASHBOARD_PATH.read_text()
        for key in ("d.lanes", "d.routed", "d.ledger", "d.bots", "d.child_tasks", "ct.by_client", "d.refusals", "d.s1",
                    "d.hardware", "d.notes", "d.errors", "s1.reason", "cache_read_tokens"):
            self.assertIn(key, html)
        self.assertNotIn("error_kind", html)        # the merged lane publishes `error` only
        self.assertEqual(set(usage._VIEW.fields) - {"generated_at"},
                         {"lanes", "routed", "refusals", "child_tasks", "ledger", "bots", "s1", "hardware", "notes"})

    def test_render_full_view(self):
        out = self.run_page(self.view())
        self.assertIn("Errors (0)", out)
        self.assertIn("[ERR] error: no_usage_lines", out)                        # a lane's error is shown on its card
        self.assertIn("[ERR] memory total: unavailable (FileNotFoundError)", out)  # an unavailable hardware field
        self.assertIn("grokbot", out); self.assertIn("route_task", out)          # child_tasks.by_client and bots
        self.assertIn("cache r", out); self.assertIn("HTTP 200", out)
        self.assertNotIn("[object Object]", out)

    def test_render_missing_sources(self):
        def boom():
            raise OSError("no cache")
        out = self.run_page(self.view(self.tmp(), scarcity_reader=boom))
        self.assertIn("[ERR] scarcity: OSError", out)                           # the errors list, at the top
        self.assertIn("[ERR] no lane readings (scarcity: OSError)", out)         # and at the empty lanes grid
        self.assertIn("[ERR] usage.db: absent", out)
        self.assertRegex(out, r"STATUS: err .* 6 errors")
        self.assertLess(out.index("scarcity: OSError"), out.index("Resources"))

    def test_lane_errors_on_their_card(self):
        v = self.view()
        v["errors"] = [{"source": "scarcity/claude", "error": "windows_truncated"}]
        out = self.run_page(v)
        card = out[out.index("\nclaude\n"):out.index("\nclaude#b\n")]
        self.assertIn("[ERR] scarcity/claude: windows_truncated", card)

    def test_failed_poll_marks_view_stale(self):
        for mode, msg in (("fetch500", "error 500"), ("fetchthrow", "fetch failed: network down")):
            out = self.run_page(self.view(), mode)
            self.assertIn(f"STATUS: err {msg} · showing data from ", out, mode)
            self.assertIn("BODY: stale", out, mode)
            self.assertIn("Resources (% left)", out, mode)                      # the last good view stays, greyed

    def test_storage_failure_keeps_token_in_memory(self):
        out = self.run_page(self.view(), "storagethrow")
        self.assertIn("STORAGE: Browser storage unavailable; the token is kept only for this page", out)
        self.assertIn("LOGIN: hidden AUTH: Bearer typed-token POLLS: 2", out)    # no login loop: the polls carry it
        self.assertIn("Resources (% left)", out)
        self.assertIn("BODY: \n", out)

    def test_storage_failing_after_startup_keeps_token(self):
        out = self.run_page(self.view(), "storagelater")
        self.assertIn("STORAGE: Browser storage unavailable; the token is kept only for this page", out)
        self.assertIn("LOGIN: hidden AUTH: Bearer tok POLLS: 2", out)            # the startup read was kept in memTok
        self.assertIn("Resources (% left)", out)


if __name__ == "__main__":
    unittest.main()
