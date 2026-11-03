"""GET /v1/cortados/usage: auth, each section from fixtures, missing sources in errors, stale scarcity flagged."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router import scarcity
from cortados_router.provider import usage
from cortados_router.provider.server import ProviderServer, Registry

NOW = 1_800_000_000.0
TOKEN = "t-usage"
_wv = scarcity.window_view
# Built FROM scarcity.window_view (the sampler's own writer) so the fixture cannot drift from the real shape.
SCARCITY = {"fetched_at": NOW - 60, "lanes": {
    "claude": {"fetched_at": NOW - 60, "config_dir": "/Users/someone/.claude-acctA", "error": None, "windows": [
        {**_wv("5-hour", 10.0, NOW + 3600, 5 * 3600, now=NOW), "pool": "all"},
        {**_wv("week (all models)", 20.0, NOW + 96 * 3600, 7 * 86400, now=NOW), "pool": "all"},
        {**_wv("week (Opus)", 97.0, NOW + 96 * 3600, 7 * 86400, now=NOW), "pool": "claude-opus", "stale": True}]},
    "claude#b": {"fetched_at": NOW - 60, "config_dir": "/Users/someone/.claude-acctB",
                 "error": "RuntimeError: no '% used' lines on the /usage screen: Logged in as x@example.com",
                 "error_kind": "no_usage_lines",
                 "windows": [{**_wv("5-hour", 50.0, NOW + 3600, 5 * 3600, now=NOW), "pool": "all"}]},
    "codex": {"fetched_at": NOW - 7 * 3600, "windows": [{**_wv("weekly", 90.0, NOW + 86400, 7 * 86400, now=NOW), "pool": "all"}]},
    "cursor": {"fetched_at": NOW - 60, "windows": [
        {**_wv("month (included, both pools)", 70.0, NOW + 10 * 86400, 30.4 * 86400, now=NOW), "pool": "info"},
        {**_wv("month (Cursor models: Grok, Composer, Auto)", 30.0, NOW + 10 * 86400, 30.4 * 86400, now=NOW), "pool": "cursor-models"},
        {**_wv("month (other models: Claude, GPT, Gemini …)", 60.0, NOW + 10 * 86400, 30.4 * 86400, now=NOW), "pool": "other-models"}]},
    "broken": {"fetched_at": "not-a-number", "windows": []}}}


def fixture_home() -> Path:
    d = Path(tempfile.mkdtemp())
    c = sqlite3.connect(d / "router.db")
    c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, lane TEXT, model TEXT, band TEXT)")
    c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
        ("a", NOW - 10, "claude", "sonnet", "B"), ("b", NOW - 20, "claude", "sonnet", "B"),
        ("c", NOW - 3 * 86400, "codex", "gpt", "C"), ("d", NOW - 30 * 86400, "codex", "gpt", "A")])
    c.commit(); c.close()
    (d / "child-tasks.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": NOW - 100, "event": "started", "task_id": "t1"}, {"ts": NOW - 50, "event": "finished", "status": "succeeded", "task_id": "t1"},
        {"ts": NOW - 40, "event": "started", "task_id": "t2"}]) + "\nnot json\n")
    (d / "hardware").mkdir()
    (d / "hardware" / "studio.json").write_text(json.dumps({"host": "studio", "memory": {"free_gb": 40}}))
    return d


class CollectTest(unittest.TestCase):
    def collect(self, home, **kw):
        args = dict(state_dir=home, scarcity_reader=lambda: SCARCITY,
                    refusals=[{"reason": "bad pin", "at": NOW - 10}, {"reason": "bad pin", "at": NOW - 5},
                              {"reason": "old", "at": NOW - 2 * 86400}],
                    s1_probe=lambda url: {"available": True, "latency_ms": 3}, now=NOW)
        args.update(kw)
        return usage.collect(**args)

    def test_sections_from_fixtures(self):
        out = self.collect(fixture_home())
        lanes = {l["lane"]: l for l in out["lanes"]}
        c = lanes["claude"]
        self.assertEqual(c["left_pct"], 80.0)                       # general windows only; the stale Opus pool (3 %) does not speak
        self.assertEqual([w["window"] for w in c["windows"]], ["5-hour", "week (all models)", "week (Opus)"])
        self.assertEqual(c["windows"][0]["reset_epoch"], NOW + 3600)
        self.assertIsNotNone(c["windows"][0]["resets_at"])
        self.assertEqual(c["pools"], {})                             # the only non-general pool is stale
        self.assertIsNone(c["account"]); self.assertIsNone(c["error"])
        self.assertFalse(c["stale"])
        self.assertEqual(lanes["claude#b"]["account"], "b")
        self.assertEqual(lanes["claude#b"]["error"], "no_usage_lines")
        cur = lanes["cursor"]
        self.assertEqual(cur["left_pct"], 70.0)                      # no general windows: the cursor-models pool, as finish() routes
        self.assertEqual(cur["shared_with"], ["Grok Bot"])
        self.assertEqual(cur["label"], "cursor (shared with Grok Bot)")
        self.assertEqual(cur["pools"], {"cursor-models": 70.0, "other-models": 40.0})
        self.assertIn("error", lanes["broken"])                      # one bad lane does not erase the section
        self.assertEqual(len(lanes), 5)
        self.assertTrue(lanes["codex"]["stale"])                     # 7 h old > 6 h routing freshness
        self.assertEqual(out["routed"]["today"], [{"lane": "claude", "model": "sonnet", "calls": 2}])
        self.assertEqual(len(out["routed"]["last_7d"]), 2)
        self.assertEqual(out["routed"]["top_bands_7d"][0], {"band": "B", "calls": 2})
        self.assertEqual(out["refusals"], {"window": "24h", "by_reason": {"bad pin": 2}, "total": 2})
        self.assertEqual(out["child_tasks"]["by_event"], {"finished": 1, "started": 1})
        self.assertEqual(out["child_tasks"]["by_status"], {"succeeded": 1})
        self.assertFalse(out["child_tasks"]["truncated"])
        self.assertEqual(out["child_tasks"]["unparsable_lines"], 1)
        self.assertTrue(out["s1"]["available"])
        self.assertEqual(out["hardware"][0]["data"]["host"], "studio")
        self.assertEqual(out["errors"], [])

    def test_missing_sources_reported(self):
        def boom():
            raise OSError("no cache")
        out = self.collect(Path(tempfile.mkdtemp()), scarcity_reader=boom,
                           s1_probe=lambda url: {"available": False, "reason": "no S1 URL configured"})
        srcs = {e["source"] for e in out["errors"]}
        self.assertEqual(srcs, {"scarcity", "router.db", "child-tasks.jsonl", "s1", "hardware"})
        self.assertEqual(out["hardware"], [])
        self.assertEqual(out["lanes"], [])

    def test_no_paths_or_vendor_text_leak(self):
        blob = json.dumps(self.collect(fixture_home()))
        self.assertNotIn("/Users/", blob); self.assertNotIn(str(Path.home()), blob)
        self.assertNotIn("example.com", blob)

    def test_child_tail_read_is_bounded(self):
        home = fixture_home()
        keep = usage.CHILD_TAIL_BYTES
        usage.CHILD_TAIL_BYTES = 200
        try:
            with (home / "child-tasks.jsonl").open("a") as f:
                for i in range(50):
                    f.write(json.dumps({"ts": NOW - 1, "event": "started", "task_id": f"x{i}"}) + "\n")
            ct = self.collect(home)["child_tasks"]
        finally:
            usage.CHILD_TAIL_BYTES = keep
        self.assertTrue(ct["truncated"]); self.assertEqual(ct["unparsable_lines"], 0)
        self.assertLess(sum(ct["by_event"].values()), 50)

    def test_s1_probe_cached_and_url_not_echoed(self):
        usage._S1_CACHE.clear()
        a = usage._s1("http://127.0.0.1:9")                          # nothing listens on the discard port
        self.assertFalse(a["available"]); self.assertNotIn("url", a)
        usage._S1_CACHE["http://127.0.0.1:9"] = (time.monotonic(), {"available": True, "status": 200})
        self.assertTrue(usage._s1("http://127.0.0.1:9")["available"])   # served from the memo, no second probe
        usage._S1_CACHE.clear()

    def test_s1_without_url(self):
        self.assertEqual(usage._s1(None)["available"], False)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        home = fixture_home()
        self.httpd.usage_view = lambda reg: usage.collect(
            state_dir=home, scarcity_reader=lambda: SCARCITY, refusals=reg.runs.all_refusals(),
            runs_since_start=reg.runs.model_counts(), s1_probe=lambda u: {"available": False, "reason": "test"}, now=NOW)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/cortados/usage"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def get(self, token):
        h = {"authorization": f"Bearer {token}"} if token else {}
        with urllib.request.urlopen(urllib.request.Request(self.url, headers=h), timeout=10) as r:
            return json.loads(r.read())

    def test_auth_required(self):
        for tok in (None, "wrong"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(tok)
            self.assertEqual(cm.exception.code, 401)

    def test_authorized(self):
        self.reg.runs.record("r1", provider="claude-cli", model="sonnet", label="x", tools=True)
        out = self.get(TOKEN)
        self.assertEqual(out["routed"]["provider_since_start"], {"claude-cli/sonnet": 1})
        self.assertIn("lanes", out); self.assertIn("generated_at", out)
        self.assertEqual(out["errors"], [{"source": "s1", "error": "test"}])


class RealWiringTest(unittest.TestCase):
    """ProviderServer.usage_view itself: refresh=False reaches scarcity even with the env switch on, state is the env's home."""

    def test_usage_view_never_samples(self):
        import cortados_router.s1_endpoint as s1e
        home = fixture_home()
        seen = {}
        keep = (scarcity.current, scarcity.sample, s1e.resolve_url, os.environ.get("CORTADOS_SCARCITY_REFRESH"),
                os.environ.get("CORTADOS_HOME"))
        def fake_current(*a, **k):
            seen["refresh"] = k.get("refresh", True)
            return SCARCITY
        def no_url():
            raise RuntimeError("no endpoints file")
        scarcity.current = fake_current
        scarcity.sample = lambda *a, **k: self.fail("usage_view must never sample")
        s1e.resolve_url = no_url
        os.environ["CORTADOS_SCARCITY_REFRESH"] = "on"           # prove refresh=False (not the env switch) blocks sampling
        os.environ["CORTADOS_HOME"] = str(home)
        try:
            reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
            httpd = ProviderServer(("127.0.0.1", 0), reg)
            try:
                out = httpd.usage_view(reg)
            finally:
                httpd.server_close()
        finally:
            scarcity.current, scarcity.sample, s1e.resolve_url = keep[:3]
            for name, val in (("CORTADOS_SCARCITY_REFRESH", keep[3]), ("CORTADOS_HOME", keep[4])):
                if val is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = val
        self.assertIs(seen["refresh"], False)
        self.assertIn({"lane": "claude", "model": "sonnet", "calls": 2}, out["routed"]["today"])   # read from CORTADOS_HOME
        self.assertFalse(out["s1"]["available"])                  # resolve_url raising lands in the s1 section, not a 500


if __name__ == "__main__":
    unittest.main()
