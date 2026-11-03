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

from cortados_router.provider import usage
from cortados_router.provider.server import ProviderServer, Registry

NOW = 1_800_000_000.0
TOKEN = "t-usage"
SCARCITY = {"fetched_at": NOW - 60, "lanes": {
    "claude": {"fetched_at": NOW - 60, "windows": [{"name": "5h", "used_pct": 40, "reset_at": NOW + 3600}]},
    "codex": {"fetched_at": NOW - 7 * 3600, "windows": [{"name": "weekly", "used_pct": 90}]}}}


def fixture_home() -> Path:
    d = Path(tempfile.mkdtemp())
    c = sqlite3.connect(d / "router.db")
    c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, lane TEXT, model TEXT, band TEXT)")
    c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
        ("a", NOW - 10, "claude", "sonnet", "B"), ("b", NOW - 20, "claude", "sonnet", "B"),
        ("c", NOW - 3 * 86400, "codex", "gpt", "C"), ("d", NOW - 30 * 86400, "codex", "gpt", "A")])
    c.commit(); c.close()
    (d / "child-tasks.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": NOW - 100, "event": "started", "task_id": "t1"}, {"ts": NOW - 50, "event": "done", "task_id": "t1"},
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
        self.assertEqual(lanes["claude"]["left_pct"], 60.0)
        self.assertFalse(lanes["claude"]["stale"])
        self.assertTrue(lanes["codex"]["stale"])                     # 7 h old > 6 h routing freshness
        self.assertEqual(out["routed"]["today"], [{"lane": "claude", "model": "sonnet", "calls": 2}])
        self.assertEqual(len(out["routed"]["last_7d"]), 2)
        self.assertEqual(out["routed"]["top_bands_7d"][0], {"band": "B", "calls": 2})
        self.assertEqual(out["refusals"], {"window": "24h", "by_reason": {"bad pin": 2}, "total": 2})
        self.assertEqual(out["child_tasks"]["by_state"], {"done": 1, "started": 1})
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
        self.assertEqual(srcs, {"scarcity", "router.db", "child-tasks.jsonl", "s1"})
        self.assertEqual(out["hardware"], [])
        self.assertEqual(out["lanes"], [])

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


if __name__ == "__main__":
    unittest.main()
