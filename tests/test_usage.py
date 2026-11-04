"""GET /v1/cortados/usage: auth, each section from fixtures, missing sources in errors, stale scarcity flagged."""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cortados_router import scarcity
from cortados_router import hardware
from cortados_router import usage_ledger
from cortados_router.provider import usage
from tests import test_hardware
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
    "codex#b": {"fetched_at": NOW - 60, "tried_at": NOW - 10, "error": "RuntimeError: screen refused", "error_kind": "parse",
                "windows": [{**_wv("weekly", 10.0, NOW + 86400, 7 * 86400, now=NOW), "pool": "all"}]},
    "broken": {"fetched_at": "not-a-number", "windows": []}}}
# Built FROM hardware.sample (the writer of hardware/<host>.json) with canned command output, so it cannot drift.
HW_SAMPLE = hardware.sample(
    run=test_hardware.fake_run({(hardware.SYSCTL, "-n", "hw.memsize"): FileNotFoundError(2, "No such file", "/Users/someone/sysctl")}),
    fetch=lambda url, timeout=None: json.dumps({"models": [
        {"name": "qwen", "size": 1, "size_vram": 1, "digest": "abc", "path": "/Users/someone/m"}]}),
    system="Darwin", now=NOW - 30, hostname="studio")
_TMP: list = []


def fixture_home() -> Path:
    d = Path(tempfile.mkdtemp())
    _TMP.append(d)
    c = sqlite3.connect(d / "router.db")
    c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, lane TEXT, model TEXT, band TEXT)")
    c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
        ("a", NOW - 10, "claude", "sonnet", "B"), ("b", NOW - 20, "claude", "sonnet", "B"),
        ("c", NOW - 3 * 86400, "codex", "gpt", "C"), ("d", NOW - 30 * 86400, "codex", "gpt", "A")])
    c.commit(); c.close()
    (d / "child-tasks.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts": NOW - 100, "event": "started", "task_id": "t1"}, {"ts": NOW - 50, "event": "finished", "status": "succeeded", "task_id": "t1"},
        {"ts": NOW - 40, "event": "started", "task_id": "t2", "client": "grokbot"}]) + "\nnot json\n")
    # usage.db written BY usage_ledger.record (the provider's writer), so the fixture cannot drift from its schema
    usage_ledger.record("claude#b", "sonnet", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
                        ts=NOW - 10, path=d / "usage.db")
    usage_ledger.record("claude#b", "sonnet", {"input_tokens": 1, "output_tokens": 2}, ts=NOW - 20, path=d / "usage.db")
    usage_ledger.record("codex", "gpt", {"input_tokens": 7}, ts=NOW - 3 * 86400, path=d / "usage.db")
    usage_ledger.record("codex", "gpt", {"input_tokens": 9}, ts=NOW - 30 * 86400, path=d / "usage.db")
    (d / "bot-usage.jsonl").write_text("\n".join(json.dumps(r) for r in [   # bot_gateway.record's line shape
        {"ts": NOW - 30, "client": "grokbot", "tool": "route_task", "task_id": None, "outcome": "ok"},
        {"ts": NOW - 20, "client": "grokbot", "tool": "start_task", "task_id": "t2", "outcome": "ok"},
        {"ts": NOW - 10, "client": "grokbot", "tool": "route_task", "task_id": None, "outcome": "refused", "reason": "cap"},
        {"ts": NOW - 30 * 86400, "client": "grokbot", "tool": "route_task", "task_id": None, "outcome": "ok"}]) + "\n")
    (d / "hardware").mkdir()
    (d / "hardware" / "studio.json").write_text(json.dumps({**HW_SAMPLE, "ssh_target": "forest@100.64.0.9"}))
    return d


def tearDownModule():
    for d in _TMP:
        for p in [d, *d.rglob("*")]:
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


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
        self.assertEqual(len(lanes), 6)
        self.assertTrue(lanes["broken"]["stale"]); self.assertIsNone(lanes["broken"]["left_pct"])
        self.assertEqual(lanes["broken"]["windows"], [])
        self.assertTrue(lanes["codex"]["stale"])                     # 7 h old > 6 h routing freshness
        self.assertEqual(out["routed"]["today"], [{"lane": "claude", "model": "sonnet", "calls": 2}])
        self.assertEqual(len(out["routed"]["last_7d"]), 2)
        self.assertEqual(out["routed"]["top_bands_7d"][0], {"band": "B", "calls": 2})
        self.assertEqual(out["refusals"], {"window": "24h", "by_reason": {"bad pin": 2}, "total": 2})
        self.assertEqual(out["child_tasks"]["by_event"], {"finished": 1, "started": 1})
        self.assertEqual(out["child_tasks"]["by_status"], {"succeeded": 1})
        self.assertEqual(out["child_tasks"]["by_client"], {"direct": 1, "grokbot": 1})
        self.assertEqual(out["ledger"]["today"], [{"lane": "claude#b", "model": "sonnet", "turns": 2, "input_tokens": 11,
                                                   "output_tokens": 7, "cache_read_tokens": 100, "cache_write_tokens": 0}])
        self.assertEqual([(r["lane"], r["turns"]) for r in out["ledger"]["last_7d"]], [("claude#b", 2), ("codex", 1)])
        self.assertEqual(out["bots"], {"window": "7d", "calls": 3, "by_client": {"grokbot": 3},
                                       "by_tool": {"route_task": 2, "start_task": 1}, "by_outcome": {"ok": 2, "refused": 1},
                                       "unparsable_lines": 0, "truncated": False})
        self.assertFalse(out["child_tasks"]["truncated"])
        self.assertEqual(out["child_tasks"]["unparsable_lines"], 1)
        self.assertTrue(out["s1"]["available"])
        hw = out["hardware"][0]["data"]
        self.assertEqual(hw["host"], "studio"); self.assertNotIn("ssh_target", hw)
        # every sampler field survives the filter (the allowlist matches the real writer), the extra key does not
        self.assertEqual(set(hw), set(HW_SAMPLE))
        self.assertEqual(hw["chip"], HW_SAMPLE["chip"]); self.assertEqual(hw["headroom"], HW_SAMPLE["headroom"])
        self.assertEqual(hw["ollama_models"], [{"name": "qwen", "size": 1, "size_vram": 1}])
        self.assertEqual(hw["mem_total_bytes"], {"available": False, "reason": "FileNotFoundError"})
        self.assertEqual(hw["errors"], [{"field": "mem_total_bytes", "error": "FileNotFoundError"}])
        self.assertEqual(out["errors"], [])

    def test_missing_sources_reported(self):
        def boom():
            raise OSError("no cache")
        empty = Path(tempfile.mkdtemp()); _TMP.append(empty)
        out = self.collect(empty, scarcity_reader=boom,
                           s1_probe=lambda url: {"available": False, "reason": "no S1 URL configured"})
        srcs = {e["source"] for e in out["errors"]}
        self.assertEqual(srcs, {"scarcity", "router.db", "child-tasks.jsonl", "s1", "hardware", "usage.db", "bot-usage.jsonl"})
        self.assertEqual(out["hardware"], [])
        self.assertEqual(out["lanes"], [])
        self.assertIn({"source": "scarcity", "error": "OSError"}, out["errors"])   # type only, no message text

    def test_no_paths_or_vendor_text_leak(self):
        blob = json.dumps(self.collect(fixture_home()))
        self.assertNotIn("/Users/", blob); self.assertNotIn(str(Path.home()), blob)
        self.assertNotIn("example.com", blob); self.assertNotIn("100.64.", blob); self.assertNotIn("digest", blob)

    def test_freshness_rule_marks_failed_retry_stale(self):
        lanes = {l["lane"]: l for l in self.collect(fixture_home(), freshness=scarcity.reading_freshness)["lanes"]}
        self.assertTrue(lanes["codex#b"]["stale"])                   # error + tried_at > fetched_at: stale at once
        self.assertFalse(lanes["claude"]["stale"])

    def test_reading_for_other_login_shows_no_numbers(self):
        # accounts reordered: each cached reading's config_dir now belongs to the other lane key
        swapped = [("claude", "/Users/someone/.claude-acctB"), ("claude#b", "/Users/someone/.claude-acctA")]
        lanes = {l["lane"]: l for l in self.collect(fixture_home(), claude_lanes=swapped)["lanes"]}
        for k in ("claude", "claude#b"):
            self.assertEqual((lanes[k]["left_pct"], lanes[k]["windows"], lanes[k]["pools"], lanes[k]["stale"],
                              lanes[k]["error"]), (None, [], {}, True, "reading_for_other_login"))
        self.assertEqual(lanes["codex"]["windows"][0]["window"], "weekly")          # non-Claude lanes untouched
        same = [("claude", "/Users/someone/.claude-acctA"), ("claude#b", "/Users/someone/.claude-acctB")]
        lanes = {l["lane"]: l for l in self.collect(fixture_home(), claude_lanes=same)["lanes"]}
        self.assertEqual(lanes["claude"]["left_pct"], 80.0)
        self.assertNotIn("/Users/", json.dumps(lanes))

    def test_non_finite_and_pool_capped(self):
        sc = {"lanes": {"x": {"fetched_at": float("inf"), "windows": [
            {"window": "w", "pool": "p" * 500, "used_pct": 10.0}] + [
            {"window": f"w{i}", "pool": f"q{i}", "used_pct": 10.0} for i in range(40)]}}}
        out = self.collect(fixture_home(), scarcity_reader=lambda: sc)
        lane = out["lanes"][0]
        self.assertIn({"source": "scarcity/x", "error": "pools_truncated"}, out["errors"])     # per-lane source
        self.assertIn({"source": "scarcity/x", "error": "windows_truncated"}, out["errors"])
        self.assertEqual(len(lane["windows"]), usage.WINDOWS_MAX)
        self.assertNotIn("_pools_truncated", lane)
        self.assertIsNone(lane["sample_age_s"]); self.assertTrue(lane["stale"])
        self.assertEqual(len(lane["windows"][0]["pool"]), usage.TEXT_MAX)
        self.assertEqual(len(lane["pools"]), usage.POOLS_MAX)
        json.dumps(lane, allow_nan=False)

    def test_hardware_cap_keeps_freshest(self):
        home = fixture_home()
        old = home / "hardware" / "aaa.json"
        old.write_text(json.dumps(HW_SAMPLE)); os.utime(old, (time.time() - 9999, time.time() - 9999))
        keep = usage.HW_MAX_FILES
        usage.HW_MAX_FILES = 1
        try:
            out = self.collect(home)
        finally:
            usage.HW_MAX_FILES = keep
        self.assertEqual([h["file"] for h in out["hardware"]], ["studio.json"])

    def test_window_name_capped(self):
        long = {"lanes": {"claude": {"fetched_at": NOW, "windows": [{**_wv("week (" + "x" * 500 + ")", 1.0, NOW + 9, 9, now=NOW)}]}}}
        w = self.collect(fixture_home(), scarcity_reader=lambda: long)["lanes"][0]["windows"][0]["window"]
        self.assertEqual(len(w), usage.TEXT_MAX)

    def test_bad_ts_counted_not_section_erased(self):
        home = fixture_home()
        with (home / "child-tasks.jsonl").open("a") as f:
            f.write(json.dumps({"ts": "soon", "event": "started", "task_id": "z"}) + "\n")
        out = self.collect(home)
        self.assertEqual(out["child_tasks"]["unparsable_lines"], 2)
        self.assertEqual(out["child_tasks"]["by_event"], {"finished": 1, "started": 1})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads mode-000 files")
    def test_unreadable_sources_report_type_only(self):
        home = fixture_home()
        os.chmod(home / "child-tasks.jsonl", 0)
        os.chmod(home / "router.db", 0)
        os.chmod(home / "hardware", 0)
        with self.assertLogs("cortados.provider.usage", "WARNING"):
            out = self.collect(home)
        errs = {e["source"]: e["error"] for e in out["errors"]}
        self.assertEqual(errs["child-tasks.jsonl"], "PermissionError")
        self.assertEqual(errs["hardware"], "PermissionError")              # an unlistable dir is reported, not []
        self.assertIn("router.db", errs)
        self.assertEqual(out["hardware"], [])
        blob = json.dumps(out)
        self.assertNotIn("/Users/", blob); self.assertNotIn(str(Path.home()), blob); self.assertNotIn(str(home), blob)

    def test_hardware_bounded(self):
        home = fixture_home()
        (home / "hardware" / "huge.json").write_bytes(b" " * (usage.HW_MAX_BYTES + 1))
        out = self.collect(home)
        self.assertIn({"source": "hardware/huge.json", "error": "oversize"}, out["errors"])
        self.assertEqual([h["file"] for h in out["hardware"]], ["studio.json"])
        keep = usage.HW_MAX_FILES
        usage.HW_MAX_FILES = 1
        try:
            (home / "hardware" / "huge.json").unlink()
            (home / "hardware" / "z.json").write_text("{}")
            out = self.collect(home)
        finally:
            usage.HW_MAX_FILES = keep
        self.assertIn({"source": "hardware", "error": "too_many_files"}, out["errors"])
        self.assertEqual(len(out["hardware"]), 1)

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

    def test_ledger_rows_without_lane_fall_back_to_runtime(self):
        home = fixture_home()
        (home / "usage.db").unlink()
        c = sqlite3.connect(home / "usage.db")      # the Hermes plugin's schema: no lane column
        c.execute("CREATE TABLE usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
                  "cache_read_tokens INTEGER, cache_write_tokens INTEGER)")
        c.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?)", (NOW - 5, "claude", "opus", 3, 4, 0, 0))
        c.commit(); c.close()
        out = self.collect(home)
        self.assertEqual(out["ledger"]["today"][0]["lane"], "claude")
        self.assertEqual(out["ledger"]["today"][0]["turns"], 1)

    def test_bots_read_rotated_log_and_count_bad_lines(self):
        home = fixture_home()
        (home / "bot-usage.jsonl.1").write_text(json.dumps(
            {"ts": NOW - 86400, "client": "grokbot", "tool": "route_task", "outcome": "ok"}) + "\n[1]\nnope\n")
        out = self.collect(home)
        self.assertEqual(out["bots"]["calls"], 4)
        self.assertEqual(out["bots"]["unparsable_lines"], 1)   # "[1]" is JSON but not a record: skipped, not counted bad

    def test_bots_unknown_tool_name_bucketed_gateway_to_view(self):
        # Review P2: a refused call logs the tool name the bot sent; a secret-looking one must not reach by_tool.
        from unittest import mock
        from cortados_router import bot_gateway as bg
        home, root = fixture_home(), Path(tempfile.mkdtemp())
        _TMP.append(root)
        secret = "sk-live-SECRETVALUE0123456789"
        with mock.patch.dict(os.environ, {"CORTADOS_HOME": str(home), "CORTADOS_SCARCITY_REFRESH": "off"}):
            gw = bg.Gateway("gw-token-0123456789abcdef", str(root), {}, child=lambda *a: {}, route=lambda *a, **k: {})
            with self.assertRaises(bg.PolicyRefusal):
                gw.call(secret, {})
        self.assertIn(secret, (home / "bot-usage.jsonl").read_text())       # the gateway did log it raw
        out = self.collect(home, now=time.time())
        self.assertNotIn("SECRETVALUE", json.dumps(out))
        self.assertEqual(out["bots"]["by_tool"].get(usage.BOT_OTHER_TOOL), 1)
        self.assertTrue(set(out["bots"]["by_tool"]) <= set(bg.EXPOSED) | {usage.BOT_OTHER_TOOL})

    def test_ledger_scan_does_not_block_concurrent_record(self):
        # Review P2: under the rollback journal a long view scan made record()'s commit time out.
        home = fixture_home()
        db = home / "usage.db"
        c = sqlite3.connect(db)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertIn("usage_ts", [r[1] for r in c.execute("PRAGMA index_list(usage)")])
        c.executemany("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
                      ((NOW - (i % 600000), "claude", f"m{i % 40}", 1, 1, 0, 0, "claude") for i in range(300000)))
        c.commit(); c.close()
        stop, results = threading.Event(), []

        def writer():
            while not stop.is_set():
                results.append(usage_ledger.record("codex", "gpt", {"input_tokens": 1}, path=db))
        t = threading.Thread(target=writer)
        t.start()
        try:
            outs = [usage._ledger(db, NOW) for _ in range(5)]
        finally:
            stop.set(); t.join()
        self.assertTrue(results and all(results), f"{results.count(False)} of {len(results)} writes failed")
        self.assertTrue(all(o["last_7d"] for o in outs))

    def test_ledger_query_time_capped(self):
        home = fixture_home()
        keep = usage.LEDGER_QUERY_S, usage._PROGRESS_OPS
        usage.LEDGER_QUERY_S, usage._PROGRESS_OPS = -1.0, 1   # already past the deadline, checked every VM step
        try:
            with self.assertRaises(sqlite3.OperationalError):
                usage._ledger(home / "usage.db", NOW)
        finally:
            usage.LEDGER_QUERY_S, usage._PROGRESS_OPS = keep

    def test_routed_query_time_capped_and_reported_as_error(self):
        # Review P2: router.db reads had no execution deadline; they share _ledger's progress-handler cap.
        home = fixture_home()
        keep = usage.LEDGER_QUERY_S, usage._PROGRESS_OPS
        usage.LEDGER_QUERY_S, usage._PROGRESS_OPS = -1.0, 1
        try:
            with self.assertRaises(sqlite3.OperationalError):
                usage._routed(home / "router.db", NOW)
            out = self.collect(home)
        finally:
            usage.LEDGER_QUERY_S, usage._PROGRESS_OPS = keep
        self.assertIn({"source": "router.db", "error": "OperationalError"}, out["errors"])

    def test_large_indexed_router_db_collects_within_deadline(self):
        home = fixture_home()
        c = sqlite3.connect(home / "router.db")
        c.execute("CREATE INDEX IF NOT EXISTS routes_ts ON routes(ts)")          # as core.DDL creates it
        c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)",
                      ((f"x{i}", NOW - 30 * 86400 - i, "claude", f"m{i % 40}", "B") for i in range(300000)))
        c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)",
                      ((f"y{i}", NOW - (i % 500000), "codex", f"m{i % 40}", "C") for i in range(50000)))
        c.commit(); c.close()
        t0 = time.monotonic()
        r = usage._routed(home / "router.db", NOW)
        self.assertLess(time.monotonic() - t0, usage.LEDGER_QUERY_S)
        self.assertTrue(r["last_7d"])
        out = self.collect(home)
        self.assertNotIn("router.db", [e["source"] for e in out["errors"]])

    def test_home_path_with_hash_and_question_mark_opens_read_only(self):
        # Review P2: a string-built 'file:' URI read '#'/'?' as fragment/query, dropping mode=ro (and creating files).
        base = Path(tempfile.mkdtemp())
        _TMP.append(base)
        home = base / "we#ird?home"
        shutil.copytree(fixture_home(), home)
        before = sorted(p.relative_to(base) for p in base.rglob("*"))
        out = self.collect(home)
        self.assertEqual(sorted(p.relative_to(base) for p in base.rglob("*")), before)
        self.assertEqual(out["routed"]["today"][0]["calls"], 2)
        self.assertEqual(out["ledger"]["today"][0]["turns"], 2)
        self.assertNotIn("router.db", [e["source"] for e in out["errors"]])
        self.assertNotIn("usage.db", [e["source"] for e in out["errors"]])

    def test_tail_read_ignores_bytes_appended_after_size_check(self):
        # Review P2: a bare f.read() after the size check read whatever was appended since, past the byte cap.
        from unittest import mock
        home = fixture_home()
        path = home / "child-tasks.jsonl"
        keep = usage.CHILD_TAIL_BYTES
        usage.CHILD_TAIL_BYTES = 64
        real_open = Path.open
        reads = []

        class Racing:
            def __init__(self, f):
                self.f = f
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.f.close()
            def seek(self, *a):
                return self.f.seek(*a)
            def tell(self):
                pos = self.f.tell()
                with real_open(path, "ab") as w:          # a writer appends right after the size was taken
                    w.write(b"x" * 10000 + b"\n")
                return pos
            def read(self, *a):
                data = self.f.read(*a)
                reads.append(len(data))
                return data

        def fake_open(self, *a, **k):
            f = real_open(self, *a, **k)
            return Racing(f) if self == path else f
        try:
            with mock.patch.object(Path, "open", fake_open):
                lines, cut = usage._tail_lines(path)
        finally:
            usage.CHILD_TAIL_BYTES = keep
        self.assertEqual(reads, [64])
        self.assertTrue(cut)
        self.assertFalse(any("xxxx" in ln for ln in lines))

    def test_bots_absent_reported(self):
        home = fixture_home()
        (home / "bot-usage.jsonl").unlink()
        out = self.collect(home)
        self.assertEqual(out["bots"], {})
        self.assertIn({"source": "bot-usage.jsonl", "error": "absent"}, out["errors"])

    def test_s1_without_url(self):
        self.assertEqual(usage._s1(None)["available"], False)

    def _hw_collect(self, home, timeout=10):
        """collect() in a thread with a deadline, so a regression that blocks (a FIFO open) fails instead of hanging."""
        box = {}
        def run():
            try:
                box["out"] = self.collect(home)
            except BaseException as exc:   # re-raised below, so a failure shows its cause, not a KeyError
                box["exc"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start(); t.join(timeout)
        self.assertFalse(t.is_alive(), "collect blocked on a hardware file")
        if "exc" in box:
            raise box["exc"]
        return box["out"]

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_hardware_fifo_and_symlink_refused(self):
        home = fixture_home()
        os.mkfifo(home / "hardware" / "x.json")
        outside = home / "elsewhere.json"
        outside.write_text(json.dumps(HW_SAMPLE))
        os.symlink(outside, home / "hardware" / "link.json")
        out = self._hw_collect(home)
        self.assertIn({"source": "hardware/x.json", "error": "not_a_regular_file"}, out["errors"])
        self.assertIn({"source": "hardware/link.json", "error": "not_a_regular_file"}, out["errors"])
        self.assertEqual([h["file"] for h in out["hardware"]], ["studio.json"])

    def test_no_known_fields_only_when_none_survive(self):
        home = fixture_home()
        (home / "hardware" / "z.json").write_text(json.dumps({"ssh_target": "x"}))
        no_schema = {k: v for k, v in HW_SAMPLE.items() if k != "schema"}
        (home / "hardware" / "y.json").write_text(json.dumps(no_schema))
        out = self.collect(home)
        self.assertIn({"source": "hardware/z.json", "error": "no_known_fields"}, out["errors"])
        self.assertNotIn({"source": "hardware/y.json", "error": "no_known_fields"}, out["errors"])
        self.assertEqual({h["file"]: h["data"].get("host") for h in out["hardware"]}["y.json"], "studio")

    def test_hardware_name_sanitised(self):
        home = fixture_home()
        (home / "hardware" / "bad name ü.json").write_text(json.dumps(HW_SAMPLE))
        (home / "hardware" / "other odd!.json").write_text("not json")
        out = self.collect(home)
        files = [h["file"] for h in out["hardware"]]
        odd = [f for f in files if f.startswith("<unnamed:")]
        self.assertEqual(len(odd), 1)
        err_src = [e["source"] for e in out["errors"] if e["source"].startswith("hardware/<unnamed:")]
        self.assertEqual(len(err_src), 1)
        self.assertNotEqual(err_src[0], f"hardware/{odd[0]}")    # two odd names stay distinct
        self.assertNotIn("bad name", json.dumps(out)); self.assertNotIn("odd!", json.dumps(out))

    def test_hardware_error_text_and_kinds_sanitised(self):
        home = fixture_home()
        s = {**HW_SAMPLE, "chip": {"available": False, "reason": "/usr/sbin/sysctl failed"},
             "ncpu": {"available": False, "reason": "bad kind with spaces: detail"},
             "errors": [{"field": "chip", "error": "/Users/someone/bin/ioreg: exit 1"},
                        {"field": "ncpu", "error": "ValueError: /Users/someone/x"}]}
        (home / "hardware" / "studio.json").write_text(json.dumps(s))
        hw = self.collect(home)["hardware"][0]["data"]
        self.assertEqual(hw["errors"], [{"field": "chip", "error": "error"}, {"field": "ncpu", "error": "ValueError"}])
        self.assertEqual(hw["chip"], {"available": False, "reason": "error"})
        self.assertEqual(hw["ncpu"], {"available": False, "reason": "error"})

    def test_hardware_lists_capped_and_said(self):
        home = fixture_home()
        s = {**HW_SAMPLE, "ollama_models": [{"name": f"m{i}"} for i in range(usage.HW_MODELS_MAX + 3)],
             "errors": [{"field": "f", "error": "OSError: x"}] * (usage.HW_ERRORS_MAX + 3)}
        (home / "hardware" / "studio.json").write_text(json.dumps(s))
        out = self.collect(home)
        hw = out["hardware"][0]["data"]
        self.assertEqual(len(hw["ollama_models"]), usage.HW_MODELS_MAX)
        self.assertEqual(len(hw["errors"]), usage.HW_ERRORS_MAX)
        self.assertIn({"source": "hardware/studio.json", "error": "models_truncated"}, out["errors"])
        self.assertIn({"source": "hardware/studio.json", "error": "errors_truncated"}, out["errors"])

    def test_nested_non_finite_nulled(self):
        home = fixture_home()
        (home / "hardware" / "studio.json").write_text(
            '{"schema": 1, "host": "h", "os": [Infinity, [NaN], {"a": -Infinity}], "load_avg": [1.0, Infinity],'
            ' "headroom": {"free_mem_gb": Infinity}, "ollama_models": [{"name": "m", "size": NaN}]}')
        out = self.collect(home)
        hw = out["hardware"][0]["data"]
        self.assertEqual(hw["os"], {"available": False, "reason": "unexpected_type"})
        self.assertEqual(hw["load_avg"], [1.0, None])
        self.assertIsNone(hw["headroom"]["free_mem_gb"]); self.assertIsNone(hw["ollama_models"][0]["size"])
        json.dumps(out, allow_nan=False)

    def test_unexpected_hw_lists_not_published(self):
        home = fixture_home()
        (home / "hardware" / "studio.json").write_text(json.dumps({
            "schema": 1, "os": [{"ssh_target": "user@100.64.0.9", "note": "x" * 5000}], "chip": [["/Users/x/secret"]],
            "ncpu": [1] * 4000, "load_avg": [1.0, 2.0, 3.0, 4.0, 5.0]}))
        out = self.collect(home)
        hw = out["hardware"][0]["data"]
        body = json.dumps(out)
        for leak in ("ssh_target", "100.64", "/Users/", "xxxxxxxx"):
            self.assertNotIn(leak, body)
        for k in ("os", "chip", "ncpu"):
            self.assertEqual(hw[k], {"available": False, "reason": "unexpected_type"})
        self.assertEqual(hw["load_avg"], [1.0, 2.0, 3.0])

    def test_null_lane_model_band_labelled(self):
        home = fixture_home()
        c = sqlite3.connect(home / "router.db")
        c.execute("INSERT INTO routes VALUES ('n', ?, NULL, NULL, NULL)", (NOW - 5,))
        c.commit(); c.close()
        r = self.collect(home)["routed"]
        self.assertIn({"lane": "(none)", "model": "(none)", "calls": 1}, r["today"])
        self.assertIn({"band": "(none)", "calls": 1}, r["top_bands_7d"])

    def test_routed_and_bands_truncated(self):
        home = fixture_home()
        keep = (usage.ROUTED_TOP, usage.BAND_TOP)
        usage.ROUTED_TOP, usage.BAND_TOP = 1, 1
        try:
            r = self.collect(home)["routed"]
        finally:
            usage.ROUTED_TOP, usage.BAND_TOP = keep
        self.assertEqual(r["last_7d"], [{"lane": "claude", "model": "sonnet", "calls": 2}])
        self.assertEqual(r["top_bands_7d"], [{"band": "B", "calls": 2}])
        self.assertTrue(r["today_truncated"]); self.assertTrue(r["last_7d_truncated"]); self.assertTrue(r["bands_truncated"])
        full = self.collect(home)["routed"]
        for k in ("today_truncated", "last_7d_truncated", "bands_truncated", "truncated"):
            self.assertNotIn(k, full)

    def test_window_nan_used_pct_has_no_left(self):
        sc = {"lanes": {"x": {"fetched_at": NOW, "windows": [{"window": "w", "pool": "all", "used_pct": float("nan")}]}}}
        lane = self.collect(fixture_home(), scarcity_reader=lambda: sc)["lanes"][0]
        self.assertIsNone(lane["windows"][0].get("left_pct")); self.assertIsNone(lane["left_pct"])
        json.dumps(lane, allow_nan=False)

    def test_unclassified_error_and_label_caps(self):
        long = "claude#" + "a" * 200
        sc = {"lanes": {"codex": {"fetched_at": NOW, "error": "RuntimeError: raw screen text", "windows": []},
                        long: {"fetched_at": NOW, "windows": []}}}
        lanes = {l["lane"]: l for l in self.collect(fixture_home(), scarcity_reader=lambda: sc)["lanes"]}
        self.assertEqual(lanes["codex"]["error"], "unclassified")
        self.assertNotIn("raw screen", json.dumps(lanes))
        capped = [l for k, l in lanes.items() if k != "codex"][0]
        for k in ("lane", "label", "account"):
            self.assertLessEqual(len(capped[k]), usage.LABEL_MAX)

    def test_other_login_nulls_sample_age(self):
        swapped = [("claude", "/Users/someone/.claude-acctB"), ("claude#b", "/Users/someone/.claude-acctA")]
        lanes = {l["lane"]: l for l in self.collect(fixture_home(), claude_lanes=swapped)["lanes"]}
        self.assertIsNone(lanes["claude"]["sample_age_s"])

    def test_pool_min_without_sentinel(self):
        sc = {"lanes": {"x": {"fetched_at": NOW, "windows": [
            {"window": "a", "pool": "p", "headroom_pct": 150.0}, {"window": "b", "pool": "p", "headroom_pct": 120.0}]}}}
        lane = self.collect(fixture_home(), scarcity_reader=lambda: sc)["lanes"][0]
        self.assertEqual(lane["pools"], {"p": 120.0})                # no 101 sentinel capping the minimum

    def test_concurrent_misses_single_flight(self):
        home = fixture_home()
        calls = []
        def slow_reader():
            calls.append(1)
            time.sleep(0.3)
            return SCARCITY
        usage._COLLECT_CACHE.clear()
        args = dict(state_dir=home, scarcity_reader=slow_reader, refusals=[],
                    s1_probe=lambda url: {"available": True})
        outs = []
        ts = [threading.Thread(target=lambda: outs.append(usage.collect(**args))) for _ in range(3)]
        try:
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
        finally:
            usage._COLLECT_CACHE.clear(); usage._COLLECT_FLIGHT.clear()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(outs), 3)
        self.assertEqual(outs[0]["lanes"], outs[2]["lanes"])


    def test_collect_memo_bounded(self):
        usage._COLLECT_CACHE.clear(); usage._COLLECT_FLIGHT.clear()
        try:
            for _ in range(usage._COLLECT_KEYS_MAX + 4):
                usage.collect(state_dir=fixture_home(), scarcity_reader=lambda: {}, refusals=[],
                              s1_probe=lambda url: {"available": True})
            self.assertLessEqual(len(usage._COLLECT_CACHE), usage._COLLECT_KEYS_MAX)
            self.assertLessEqual(len(usage._COLLECT_FLIGHT), usage._COLLECT_KEYS_MAX)
        finally:
            usage._COLLECT_CACHE.clear(); usage._COLLECT_FLIGHT.clear()

    def test_hot_key_survives_eviction(self):
        usage._COLLECT_CACHE.clear(); usage._COLLECT_FLIGHT.clear()
        hot, calls = fixture_home(), []
        def reader():
            calls.append(1)
            return SCARCITY
        hot_args = dict(state_dir=hot, scarcity_reader=reader, refusals=[], s1_probe=lambda url: {"available": True})
        saved, usage.COLLECT_CACHE_S = usage.COLLECT_CACHE_S, 3600.0   # 49 real collects on a loaded box can pass 2 s
        try:
            usage.collect(**hot_args)
            for _ in range(12):
                usage.collect(state_dir=fixture_home(), scarcity_reader=lambda: {}, refusals=[],
                              s1_probe=lambda url: {"available": True})
                for _ in range(3):
                    usage.collect(**hot_args)
            self.assertIn(str(hot), usage._COLLECT_CACHE)
            self.assertEqual(len(calls), 1)                      # every hammer was a memo hit
            body = usage.collect(**hot_args, encoded=True)
            self.assertIs(body, usage._COLLECT_CACHE[str(hot)][1])   # the memo holds the bytes: a hit serialises nothing
        finally:
            usage.COLLECT_CACHE_S = saved
            usage._COLLECT_CACHE.clear(); usage._COLLECT_FLIGHT.clear()


# a 100 KB string publishes at most its first REASON_MAX (refusal reasons) or TEXT_MAX chars
LEAKS = ("ssh_target", "100.64", "/Users/", "R" * (usage.REASON_MAX + 1), "fd7a", "aa:bb")
PAYLOADS = [{"ssh_target": "forest@100.64.0.9"}, "/Users/someone/.ssh/id", "R" * 100000,
            [{"ssh_target": "x"}, ["/Users/a"]], {"available": False, "reason": "forest@100.64.0.9: /Users/x"},
            "forest@100.64.0.9", "10.0.100.64", "ok\\Users", "fd7a:115c:a1e0::1", "aa:bb:cc:dd:ee:ff",
            "fd7a:115c:a1e0:ab12:4843:cd96:6263:1234"]


class PublishTest(unittest.TestCase):
    """The publish pass: a planted payload in ANY field of ANY section never reaches the body; real values do."""

    def setUp(self):
        self.home = fixture_home()

    def view(self, sc=None, hw=None, runs=None, refusals=None, s1=None):
        if hw is not None:
            (self.home / "hardware" / "studio.json").write_text(json.dumps(hw))
        return usage.collect(state_dir=self.home, scarcity_reader=lambda: sc if sc is not None else SCARCITY,
                             refusals=refusals or [], runs_since_start=runs,
                             s1_probe=s1 or (lambda u: {"available": True}), now=NOW)

    def clean(self, view):
        body = json.dumps(view, allow_nan=False)
        for leak in LEAKS:
            self.assertNotIn(leak, body)
        self.assertLess(len(body), 200_000)
        return body

    def test_every_field_every_section(self):
        lane = SCARCITY["lanes"]["claude"]
        win = lane["windows"][0]
        for bad in PAYLOADS:
            for k in list(win) + ["extra"]:                                  # every window field
                self.clean(self.view({"lanes": {"claude": {**lane, "windows": [{**win, k: bad}]}}}))
            for k in ("fetched_at", "error", "error_kind", "windows", "config_dir", "extra"):   # every lane field
                self.clean(self.view({"lanes": {"claude": {**lane, k: bad}}}))
            self.clean(self.view({"lanes": {"forest@100.64.0.9": lane, "/Users/x": lane, "R" * 100000: lane}}))
            self.clean(self.view({"lanes": {"x": {"fetched_at": NOW, "windows": [
                {"window": "w", "pool": bad if isinstance(bad, str) else "p", "headroom_pct": 5.0}]}}}))  # pool keys
            for k in list(HW_SAMPLE) + ["extra"]:                            # every hardware field
                self.clean(self.view(hw={**HW_SAMPLE, k: bad}))
            for k in _hk:
                self.clean(self.view(hw={**HW_SAMPLE, "headroom": {**HW_SAMPLE["headroom"], k: bad}}))
            for k in ("name", "size", "size_vram", "extra"):
                self.clean(self.view(hw={**HW_SAMPLE, "ollama_models": [{"name": "q", k: bad}]}))
            for k in ("field", "error", "extra"):
                self.clean(self.view(hw={**HW_SAMPLE, "errors": [{"field": "chip", "error": "E", k: bad}]}))
            self.clean(self.view(hw={**HW_SAMPLE, "errors": [bad], "ollama_models": [bad], "load_avg": [bad] * 9}))
            self.clean(self.view(runs={"claude-cli/sonnet": 1, "forest@100.64.0.9/x": bad if not isinstance(bad, str) else 2,
                                       "a/" + ("R" * 100000 if isinstance(bad, str) else "b"): 1}))
            self.clean(self.view(refusals=[{"reason": bad if isinstance(bad, str) else "x", "at": NOW}]))
            self.clean(self.view(s1=lambda u: {"available": False, "reason": bad, "status": bad, "x": bad}))
        with (self.home / "child-tasks.jsonl").open("a") as f:
            for bad in PAYLOADS:
                f.write(json.dumps({"ts": NOW - 1, "task_id": "z" + str(len(str(bad))), "event": bad, "status": bad}) + "\n")
                f.write(json.dumps({"ts": NOW - 1, "task_id": "y", "event": "finished", "status": bad}) + "\n")
        self.clean(self.view())

    def test_hw_error_field_is_a_sampler_field(self):
        hw = self.view(hw={**HW_SAMPLE, "errors": [{"field": "/Users/a", "error": "E: x"},
                                                   {"field": {"ssh_target": 1}, "error": "E"},
                                                   {"field": "platform", "error": "OSError: y"}]})["hardware"][0]["data"]
        self.assertEqual([e["field"] for e in hw["errors"]], ["unknown", "unknown", "platform"])   # one "unknown" shape

    def test_text_rule(self):
        for bad in ("/Users/x", "a\\b", "u@h", "10.1.2.3", "x <b>", '{"a": 1}'):
            self.assertEqual(usage._text(bad), "error", bad)
        for good in ("week (all models)", "claude-opus-5-5", "2027-01-15 04:00", "claude#b", "qwen2.5:7b",
                     "month (other models: Claude, GPT, Gemini \u2026)", "cursor (shared with Grok Bot)", "50%"):
            self.assertEqual(usage._text(good), good)
        self.assertEqual(len(usage._text("R" * 100000)), usage.TEXT_MAX)
        self.assertIsNone(usage._text(5)); self.assertIsNone(usage._text({"a": "b"}))

    def test_legit_values_publish_unchanged(self):
        out = self.view(runs={"claude-cli/sonnet": 3})
        hw = out["hardware"][0]["data"]
        want = {k: v for k, v in HW_SAMPLE.items() if k not in ("mem_total_bytes", "errors")}
        self.assertEqual({k: hw[k] for k in want}, want)
        cur = {l["lane"]: l for l in out["lanes"]}["cursor"]
        names = [w["window"] for w in cur["windows"]]
        self.assertEqual(names, [w["window"] for w in SCARCITY["lanes"]["cursor"]["windows"]])   # incl. the ellipsis
        claude = {l["lane"]: l for l in out["lanes"]}["claude"]["windows"][0]
        src = SCARCITY["lanes"]["claude"]["windows"][0]
        self.assertEqual({k: claude[k] for k in usage._WIN_KEYS if k in src}, {k: src[k] for k in usage._WIN_KEYS if k in src})
        self.assertEqual(out["routed"]["provider_since_start"], {"claude-cli/sonnet": 3})
        self.assertEqual(out["errors"], [])

    def test_200_lanes_by_200_windows_bounded(self):
        w = {**_wv("5-hour", 10.0, NOW + 3600, 5 * 3600, now=NOW), "pool": "all"}
        sc = {"lanes": {f"l{i}": {"fetched_at": NOW, "windows": [{**w, "pool": f"p{j}"} for j in range(200)]}
                        for i in range(200)}}
        out = self.view(sc)
        body = self.clean(out)
        self.assertEqual(len(out["lanes"]), usage.LANES_MAX)
        self.assertTrue(all(len(l["windows"]) == usage.WINDOWS_MAX for l in out["lanes"]))
        self.assertTrue(all(len(l["pools"]) == usage.POOLS_MAX for l in out["lanes"]))
        self.assertIn({"source": "scarcity", "error": "lanes_truncated"}, out["errors"])
        self.assertIn({"source": "scarcity/l0", "error": "windows_truncated"}, out["errors"])
        self.assertLessEqual(len(out["errors"]), usage.ERRORS_MAX)
        self.assertEqual(out["errors"][-1], {"source": "usage", "error": "errors_truncated"})
        self.assertLess(len(body), 200_000)

    def test_ipv4_hardware_file_name_unnamed(self):
        (self.home / "hardware" / "100.64.0.9.json").write_text(json.dumps({**HW_SAMPLE, "load_avg": [1.0] * 4}))
        t = time.time() + 100; os.utime(self.home / "hardware" / "100.64.0.9.json", (t, t))   # freshest: position 0
        out = self.view()
        self.assertEqual(out["hardware"][0]["file"], "<unnamed:0>")
        self.assertIn({"source": "hardware/<unnamed:0>", "error": "load_avg_truncated"}, out["errors"])
        self.assertNotIn("100.64", json.dumps(out))
        self.assertEqual(usage._file("100.64.0.9.json"), "error")
        self.assertEqual(usage._hw_name("10.0.0.1.json", 3), "<unnamed:3>")
        self.assertEqual(usage._hw_name("studio.json", 3), "studio.json")

    def test_ipv6_and_mac_refused(self):
        for bad in ("fd7a:115c:a1e0::1", "::1", "fd7a:115c:a1e0:ab12:4843:cd96:6263:1234", "aa:bb:cc:dd:ee:ff",
                    "host fe80::1 up"):
            self.assertEqual(usage._text(bad), "error", bad)
            self.assertEqual(usage._kind(bad), "error", bad)
        self.assertEqual(usage._kind("TimeoutError: fd7a::1"), "TimeoutError")
        for good in ("04:00:00", "qwen2.5:7b", "2027-01-15 04:00:00", "month (other models: Claude, GPT)"):
            self.assertEqual(usage._text(good), good)
        hw = self.view(hw={**HW_SAMPLE, "host": "fd7a:115c:a1e0:ab12:4843:cd96:6263:1234", "chip": "fd7a:115c:a1e0::1",
                           "os": "aa:bb:cc:dd:ee:ff"})
        self.clean(hw)

    def test_merged_keys_keep_counts(self):
        long = "R" * usage.REASON_MAX
        out = self.view(refusals=[{"reason": long + "a", "at": NOW}, {"reason": long + "b", "at": NOW},
                                  {"reason": long + "b", "at": NOW}])
        self.assertEqual(sum(out["refusals"]["by_reason"].values()), out["refusals"]["total"])
        self.assertEqual(out["refusals"]["by_reason"], {long: 3})
        self.assertIn({"source": "usage", "error": "keys_merged"}, out["errors"])
        out = self.view({"lanes": {"x": {"fetched_at": NOW, "windows": [
            {"window": "w", "pool": "/a", "headroom_pct": 30.0}, {"window": "v", "pool": "/b", "headroom_pct": 5.0}]}}})
        self.assertEqual(out["lanes"][0]["pools"], {"error": 5.0})            # pool headroom keeps the minimum
        self.assertIn({"source": "scarcity/x", "error": "keys_merged"}, out["errors"])

    def test_ollama_registry_names(self):
        for good in ("hf.co/bartowski/Llama-3.2-3B:Q4", "library/llama3", "qwen2.5:7b"):
            self.assertEqual(usage._model_name(good), good)
        for bad in ("/Users/x", "a//b", "../x", "u@h/x", "100.64.0.9/x", "a/b/c/d/e", "fd7a::1/m"):
            self.assertEqual(usage._model_name(bad), "error", bad)
        self.assertLessEqual(len(usage._model_name("a/" + "R" * 100000)), usage.TEXT_MAX)
        out = self.view(hw={**HW_SAMPLE, "ollama_models": [{"name": "hf.co/bartowski/Llama-3.2-3B:Q4", "size": 1}]})
        self.assertEqual(out["hardware"][0]["data"]["ollama_models"][0]["name"], "hf.co/bartowski/Llama-3.2-3B:Q4")

    def test_load_avg_cut_flagged(self):
        out = self.view(hw={**HW_SAMPLE, "load_avg": [1.0, 2.0, 3.0, 4.0]})
        self.assertEqual(out["hardware"][0]["data"]["load_avg"], [1.0, 2.0, 3.0])
        self.assertIn({"source": "hardware/studio.json", "error": "load_avg_truncated"}, out["errors"])


_hk = usage._HW_HEADROOM_KEYS


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

    def test_view_failure_is_a_clean_500(self):
        def boom(reg):
            raise RuntimeError("secret /Users/someone/path")
        self.httpd.usage_view = boom
        with self.assertLogs("cortados.provider", "WARNING") as logs:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(TOKEN)
        self.assertEqual(cm.exception.code, 500)
        body = cm.exception.read().decode()
        self.assertIn("usage view unavailable", body); self.assertNotIn("/Users/", body)
        self.assertTrue(any("usage: view failed: RuntimeError" in m for m in logs.output))
        self.assertFalse(any("/Users/" in m for m in logs.output))

    def test_nested_nan_in_lane_window_is_null_not_500(self):
        home = fixture_home()
        sc = {"lanes": {"x": {"fetched_at": NOW, "windows": [
            {"window": "w", "pool": "all", "used_pct": 10.0, "resets_at": {"deep": [float("nan")]}}]}}}
        self.httpd.usage_view = lambda reg: usage.collect(
            state_dir=home, scarcity_reader=lambda: sc, refusals=[], s1_probe=lambda u: {"available": True}, now=NOW)
        out = self.get(TOKEN)
        self.assertIsNone(out["lanes"][0]["windows"][0]["resets_at"])      # a container where a leaf is expected

    def test_non_finite_view_is_a_500_not_invalid_json(self):
        self.httpd.usage_view = lambda reg: {"x": [float("inf")]}
        with self.assertLogs("cortados.provider", "WARNING"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(TOKEN)
        self.assertEqual(cm.exception.code, 500)

    def test_authorized(self):
        self.reg.runs.record("r1", provider="claude-cli", model="sonnet", label="x", tools=True)
        out = self.get(TOKEN)
        self.assertEqual(out["routed"]["provider_since_start"], {"claude-cli/sonnet": 1})
        self.assertIn("lanes", out); self.assertIn("generated_at", out)
        self.assertEqual(out["errors"], [{"source": "s1", "error": "test"}])


class RealWiringTest(unittest.TestCase):
    """ProviderServer.usage_view itself, with the real scarcity reader (scarcity.current is never called, nothing samples),
    state from the env's throwaway CORTADOS_HOME."""

    def view(self, home: Path) -> dict:
        import cortados_router.s1_endpoint as s1e
        keep = (scarcity.current, scarcity.sample, s1e.resolve_url, os.environ.get("CORTADOS_SCARCITY_REFRESH"),
                os.environ.get("CORTADOS_HOME"))
        def no_url():
            raise RuntimeError("no endpoints file")
        scarcity.current = lambda *a, **k: self.fail("usage_view must read scarcity.json strictly, not scarcity.current")
        scarcity.sample = lambda *a, **k: self.fail("usage_view must never sample")
        s1e.resolve_url = no_url
        os.environ["CORTADOS_SCARCITY_REFRESH"] = "on"           # prove the view itself (not the env switch) never samples
        os.environ["CORTADOS_HOME"] = str(home)
        try:
            reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
            httpd = ProviderServer(("127.0.0.1", 0), reg)
            try:
                raw = httpd.usage_view(reg)
                self.assertIsInstance(raw, bytes)                   # the live view arrives encoded (memoised bytes)
                return json.loads(raw)
            finally:
                httpd.server_close()
        finally:
            scarcity.current, scarcity.sample, s1e.resolve_url = keep[:3]
            for name, val in (("CORTADOS_SCARCITY_REFRESH", keep[3]), ("CORTADOS_HOME", keep[4])):
                if val is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = val

    def test_usage_view_never_samples(self):
        home = fixture_home()
        (home / "scarcity.json").write_text(json.dumps(SCARCITY))
        out = self.view(home)
        self.assertIn({"lane": "claude", "model": "sonnet", "calls": 2}, out["routed"]["today"])   # read from CORTADOS_HOME
        self.assertTrue(out["lanes"])
        self.assertNotIn("scarcity", [e["source"] for e in out["errors"]])
        self.assertFalse(out["s1"]["available"])                  # resolve_url raising lands in the s1 section, not a 500

    def test_missing_scarcity_cache_is_an_error_not_empty_lanes(self):
        # Review P2: scarcity.current() returned an empty cache for a missing file, so the view showed lanes: [] silently
        home = fixture_home()
        self.assertFalse((home / "scarcity.json").exists())
        out = self.view(home)
        self.assertEqual(out["lanes"], [])
        self.assertIn({"source": "scarcity", "error": "FileNotFoundError"}, out["errors"])

    def test_corrupt_scarcity_cache_is_an_error_distinct_from_missing(self):
        home = fixture_home()
        (home / "scarcity.json").write_text("{not json")
        out = self.view(home)
        self.assertEqual(out["lanes"], [])
        self.assertIn({"source": "scarcity", "error": "JSONDecodeError"}, out["errors"])
        home2 = fixture_home()
        (home2 / "scarcity.json").write_text("[1, 2]")             # valid JSON, wrong shape
        self.assertIn({"source": "scarcity", "error": "ValueError"}, self.view(home2)["errors"])


if __name__ == "__main__":
    unittest.main()
