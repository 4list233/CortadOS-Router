"""Work split: tokens per routed conversation (core.log_tokens + the routes migration in core._db), their read side
(provider/work_split.routed_tokens, tasks.collect), the heartbeat `orchestrator` block (validate_block, the sender's
orchestrator_block and its 400 fallback) and the live `work_split` section. Throwaway state only: core.STATE points at
a fresh temp dir, the sender's scan is a fake and its cache a temp file; no transcript, vendor CLI or network is read."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from cortados_router import core, live_heartbeat as hbs, orchestrator_usage as ou, router_db
from cortados_router.provider import live, tasks, work_split

NOW = 1_800_000_000.0
TAG = "3f9a1c2e9b7d4a10"
SECRET = "SECRET-PROMPT-TEXT-5519"


def old_db(state: Path) -> None:
    """A router.db as the writer left it before the work split: core.DDL plus the three columns _db() already added."""
    c = sqlite3.connect(state / "router.db")
    c.executescript(core.DDL)
    for col in ("scarcity", "context", "task_context"):
        c.execute(f"ALTER TABLE routes ADD COLUMN {col} TEXT")
    c.execute("INSERT INTO routes (turn_id, ts, session_id, request, lane, model, band, engine) "
              "VALUES ('old-1', ?, 's', ?, 'codex', 'gpt-6.1', 'R1', 's1')", (NOW - 60, SECRET))
    c.commit()
    c.close()


def cols(state: Path) -> list[str]:
    c = sqlite3.connect(state / "router.db")
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(routes)")]
    finally:
        c.close()


class StateBase(unittest.TestCase):
    def setUp(self):
        self._keep = (core.STATE, core.ROUTE_LOG_RETRY_S)
        core.STATE = Path(tempfile.mkdtemp(prefix="cortados-work-split-"))     # never the live router.db
        core.ROUTE_LOG_RETRY_S = 0.001
        router_db._reset()

    def tearDown(self):
        shutil.rmtree(core.STATE, True)
        core.STATE, core.ROUTE_LOG_RETRY_S = self._keep
        router_db._reset()

    def insert(self, turn_id, ts=NOW - 60, lane="claude#b", model="claude-sonnet-5"):
        c = core._db()
        c.execute("INSERT INTO routes (turn_id, ts, session_id, request, lane, model, band, engine) "
                  "VALUES (?, ?, 's', ?, ?, ?, 'R1', 's1')", (turn_id, ts, SECRET, lane, model))
        c.commit()
        c.close()


class MigrationTest(StateBase):
    def test_additive_and_idempotent(self):
        old_db(core.STATE)
        before = cols(core.STATE)
        self.assertNotIn("tok_input", before)
        core._db().close()
        after = cols(core.STATE)
        self.assertEqual(after[:len(before)], before)                    # nothing renamed, dropped or reordered
        self.assertEqual(after[len(before):], [c for c, _ in core.TOKEN_COLUMNS])
        core._db().close()                                               # a second open changes nothing
        self.assertEqual(cols(core.STATE), after)
        c = sqlite3.connect(core.STATE / "router.db")
        row = c.execute("SELECT request, tok_input, tok_turns, office_session FROM routes WHERE turn_id='old-1'").fetchone()
        c.close()
        self.assertEqual(row, (SECRET, None, None, None))                # old rows: NULL = not recorded

    def test_a_fresh_db_gets_the_columns(self):
        core._db().close()
        self.assertTrue(set(c for c, _ in core.TOKEN_COLUMNS) <= set(cols(core.STATE)))

    def test_two_openers_racing_on_an_old_db(self):
        # both read the old column list, both ALTER: the loser's "duplicate column" is the outcome it wanted
        old_db(core.STATE)
        real_connect, gate = sqlite3.connect, threading.Barrier(2)

        class Conn:
            def __init__(self, c):
                self.c = c

            def execute(self, sql, *a):
                r = self.c.execute(sql, *a)
                if sql.startswith("PRAGMA table_info"):
                    rows = r.fetchall()
                    gate.wait(5)
                    return iter(rows)
                return r

            def __getattr__(self, k):
                return getattr(self.c, k)
        errors = []

        def open_once():
            try:
                core._db().close()
            except Exception as exc:                 # noqa: BLE001 - reported below
                errors.append(exc)
        sqlite3.connect = lambda *a, **k: Conn(real_connect(*a, **k))
        try:
            ts = [threading.Thread(target=open_once) for _ in range(2)]
            [t.start() for t in ts]
            [t.join(10) for t in ts]
        finally:
            sqlite3.connect = real_connect
        self.assertEqual(errors, [])
        self.assertIn("tok_cache_write", cols(core.STATE))

    def test_a_reader_never_migrates(self):
        old_db(core.STATE)
        v = tasks.collect(core.STATE, now=NOW)                           # mode=rw + query_only: reads, never ALTERs
        self.assertNotIn("tok_input", cols(core.STATE))
        self.assertEqual(v["errors"], [])
        self.assertEqual(v["token_totals"]["status"], "not recorded")
        self.assertEqual(v["tokens_status"], "not recorded")
        self.assertEqual((v["tasks"][0]["tokens"], v["tasks"][0]["tokens_status"]), (None, "not recorded"))


class LogTokensTest(StateBase):
    def test_sums_turns_and_keeps_the_first_session_tag(self):
        self.insert("r1")
        self.assertTrue(core.log_tokens("r1", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000,
                                               "cache_creation_input_tokens": 200}, TAG))
        self.assertTrue(core.log_tokens("r1", {"input_tokens": 3, "output_tokens": 2}, "ffffffffffffffff"))
        self.assertTrue(core.log_tokens("r1", {"input_tokens": -5, "output_tokens": float("nan"), "x": 1,
                                               "cache_read_input_tokens": "9"}, None))   # junk counts as 0, still a turn
        c = sqlite3.connect(core.STATE / "router.db")
        row = c.execute("SELECT tok_input, tok_output, tok_cache_read, tok_cache_write, tok_turns, office_session "
                        "FROM routes WHERE turn_id='r1'").fetchone()
        c.close()
        self.assertEqual(row, (13, 7, 1000, 200, 3, TAG))

    def test_a_failed_write_is_counted_never_raised(self):
        self.insert("r1")
        real = core._db

        class Failing:
            def __init__(self):
                self.c = real()

            def execute(self, sql, *a):
                if sql.lstrip().upper().startswith("UPDATE"):
                    raise sqlite3.OperationalError("database is locked")
                return self.c.execute(sql, *a)

            def commit(self):
                self.c.commit()

            def close(self):
                self.c.close()
        core._db = Failing
        try:
            self.assertFalse(core.log_tokens("r1", {"input_tokens": 1}, TAG))
        finally:
            core._db = real
        st = router_db.stats()
        self.assertEqual((st["count"], st["lost"], st["last_op"]), (2, 1, "log_tokens"))


class RoutedTokensTest(StateBase):
    def setUp(self):
        super().setUp()
        self.insert("r1", ts=NOW - 600)
        self.insert("r2", ts=NOW - 3 * 86400, lane="codex", model="gpt-6.1")
        self.insert("r3", ts=NOW - 900, lane="codex", model="gpt-6.1")
        self.insert("r4", ts=NOW - 60)                                    # routed, no turn completed yet
        self.insert("r5", ts=NOW - 30 * 86400)                            # outside both windows
        core.log_tokens("r1", {"input_tokens": 1200, "output_tokens": 800, "cache_read_input_tokens": 50000,
                               "cache_creation_input_tokens": 2000}, TAG)
        core.log_tokens("r2", {"input_tokens": 9000, "output_tokens": 1000}, TAG)
        core.log_tokens("r3", {"input_tokens": 300, "output_tokens": 200}, None)
        core.log_tokens("r5", {"input_tokens": 7, "output_tokens": 7}, TAG)

    def test_windows_sessions_and_models(self):
        v = tasks.collect(core.STATE, now=NOW)
        self.assertEqual(v["tokens_status"], "recorded per routed conversation")
        raw = json.dumps(v)
        self.assertNotIn(SECRET, raw)
        by_id = {t["task_id"]: t for t in v["tasks"]}
        self.assertEqual(by_id["r1"]["tokens"], {"input": 1200, "output": 800, "cache_read": 50000, "cache_write": 2000})
        self.assertEqual(by_id["r1"]["tokens_status"], "recorded")
        self.assertEqual((by_id["r4"]["tokens"], by_id["r4"]["tokens_status"]), (None, "not recorded"))
        tt = v["token_totals"]
        self.assertEqual(tt["status"], "recorded")
        d, w = tt["by_window"]["24h"], tt["by_window"]["7d"]
        self.assertEqual(d["totals"], {"input": 1500, "output": 1000, "cache_read": 50000, "cache_write": 2000,
                                       "fresh": 4500, "all": 54500})
        self.assertEqual((d["conversations"], d["recorded"], d["unrecorded"], d["unattributed_fresh"]), (3, 2, 1, 500))
        self.assertEqual(d["by_session"], {TAG: {"input": 1200, "output": 800, "cache_read": 50000, "cache_write": 2000,
                                                 "fresh": 4000, "all": 54000, "conversations": 1}})   # r4: no turn, no tag
        self.assertEqual([m["route"] for m in d["by_model"]], ["claude#b/claude-sonnet-5", "codex/gpt-6.1"])
        self.assertEqual(w["by_session"][TAG]["fresh"], 14000)
        self.assertEqual(w["by_model"][0]["route"], "codex/gpt-6.1")      # most fresh tokens first
        self.assertEqual(w["conversations"], 4)

    def test_live_routed_totals_carry_the_tokens(self):
        v = tasks.collect(core.STATE, now=NOW)
        sec = live.routed_section(v, NOW)
        self.assertEqual(sec["totals"]["tokens"]["by_window"]["24h"]["totals"]["fresh"], 4500)
        self.assertEqual(sec["totals"]["decisions"], 5)


def block(**over):
    b = {"version": 1, "sampled_at": NOW - 30, "salt": True, "windows": {"24h": 86400, "7d": 604800},
         "totals": {"24h": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000, "sessions": 1},
                    "7d": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000, "sessions": 1}},
         "sessions": [{"session": TAG, "project": "CortadOS-Router", "model": "claude-opus-5", "last_at": NOW - 60,
                       "24h": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000},
                       "7d": {"input": 4000, "output": 6000, "cache_read": 900000, "cache_write": 30000}}],
         "sessions_hidden": 0, "excluded": {"child_task_files": 1, "sidechain_entries": 0, "routed_model_entries": 0,
                                            "unreadable_files": 0, "bad_lines": 0}}
    b.update(over)
    return b


class ValidateBlockTest(unittest.TestCase):
    def check(self, b):
        return work_split.validate_block(b, NOW, live.TIME_SKEW_S)

    def test_a_good_block_passes_without_its_version(self):
        out, bad = self.check(block())
        self.assertIsNone(bad)
        self.assertEqual(out["sessions"][0]["session"], TAG)
        self.assertNotIn("version", out)

    def test_bad_blocks_name_the_field_only(self):
        s0 = block()["sessions"][0]
        cases = {
            "": block(prompt=SECRET),
            ".sampled_at": block(sampled_at=NOW + 3600),
            ".windows": block(windows={"24h": 86400}),
            ".totals.24h": block(totals={"24h": {"input": -1, "output": 0, "cache_read": 0, "cache_write": 0},
                                         "7d": block()["totals"]["7d"]}),
            ".sessions[0]": block(sessions=[dict(s0, text=SECRET)]),
            ".sessions[0].session": block(sessions=[dict(s0, session="raw-session-id-1234")]),
            ".sessions[0].project": block(sessions=[dict(s0, project="/Users/o/" + SECRET + "<x>")]),
            ".sessions[0].7d": block(sessions=[dict(s0, **{"7d": {"input": 1.5, "output": 0, "cache_read": 0,
                                                                   "cache_write": 0}})]),
            ".sessions": block(sessions=[s0] * (ou.SESSIONS_MAX + 1)),
            ".excluded": block(excluded={"other": 1}),
            ".salt": block(salt="yes"),
        }
        for want, b in cases.items():
            out, bad = self.check(b)
            self.assertIsNone(out, want)
            self.assertEqual(bad, want)
            self.assertNotIn(SECRET, bad)
        hb, bad = live.validate_heartbeat({"host": "air", "jobs": [], "orchestrator": block(salt=3)}, NOW)
        self.assertEqual((hb, bad), (None, "orchestrator.salt"))


class SectionTest(unittest.TestCase):
    def routed(self):
        c = sqlite3.connect(":memory:")
        c.executescript(core.DDL)
        for col, kind in core.TOKEN_COLUMNS:
            c.execute(f"ALTER TABLE routes ADD COLUMN {col} {kind}")
        for tid, tag, inp in (("a", TAG, 10000), ("b", "aaaaaaaaaaaaaaaa", 500)):
            c.execute("INSERT INTO routes (turn_id, ts, lane, model, office_session, tok_input, tok_output, tok_turns) "
                      "VALUES (?, ?, 'codex', 'gpt-6.1', ?, ?, 0, 1)", (tid, NOW - 60, tag, inp))
        try:
            return work_split.routed_tokens(c, NOW)
        finally:
            c.close()

    def test_join_and_routed_only_sessions(self):
        hb = {"air": {"orchestrator": self_valid(block())}}
        s = work_split.section(self.routed(), hb, NOW)
        self.assertEqual((s["status"], s["errors"]), ("ok", []))
        o = s["overall"]["24h"]
        self.assertEqual((o["orchestrator"]["fresh"], o["routed"]["fresh"], o["routed_pct"]), (40000, 10500, 20.8))
        main, other = s["sessions"]
        self.assertEqual((main["session"], main["host"], main["24h"]["routed_fresh"], main["24h"]["routed_pct"]),
                         (TAG, "air", 10000, 20.0))
        # routed work whose main session no heartbeat reported keeps its own row
        self.assertEqual((other["session"], other["host"], other["24h"]["orchestrator_fresh"], other["24h"]["routed_fresh"]),
                         ("aaaaaaaaaaaaaaaa", None, None, 500))
        self.assertEqual(s["hosts"][0]["host"], "air")
        self.assertFalse(s["hosts"][0]["stale"])

    def test_each_missing_side_is_an_error_not_a_zero(self):
        s = work_split.section(None, {}, NOW)
        self.assertEqual(s["status"], "error")
        self.assertEqual([e["source"] for e in s["errors"]], ["orchestrator", "router.db"])
        self.assertIsNone(s["overall"]["24h"]["routed_pct"])
        s = work_split.section({"status": "not recorded", "by_window": {}}, {"air": {"orchestrator": self_valid(block())}}, NOW)
        self.assertEqual(s["status"], "partial")
        self.assertIn("not migrated", s["errors"][0]["error"])
        old = self_valid(block(sampled_at=NOW - 3600))
        s = work_split.section(self.routed(), {"air": {"orchestrator": old}}, NOW + 0)
        self.assertTrue(s["hosts"][0]["stale"])


def self_valid(b):
    out, bad = work_split.validate_block(b, NOW, live.TIME_SKEW_S)
    assert bad is None, bad
    return out


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.token = self.dir / "token"
        self.token.write_text("heartbeat-token-0123456789abcdef\n")
        os.chmod(self.token, 0o600)
        self.cache = self.dir / "cache" / "orch.json"
        self.scans = []

    def scan(self, projects, *, now, salt, task_roots):
        self.scans.append((projects, salt, tuple(task_roots)))
        return dict(block(), sampled_at=round(now, 3), sessions=[dict(block()["sessions"][0], last_at=now - 60)])

    def config(self, orch):
        p = self.dir / "heartbeat.json"
        p.write_text(json.dumps({"version": 1, "host": "air", "provider_url": "http://provider.test:8432",
                                 "token_file": str(self.token), "jobs": [], "hardware": False, "orchestrator": orch}))
        return p

    def test_config_rules(self):
        cfg = hbs.load_config(self.config({"projects": "/p", "task_roots": ["/r"], "every_s": 120, "cache": "/c"}))
        self.assertEqual(cfg["orchestrator"], {"projects": "/p", "task_roots": ["/r"], "every_s": 120, "cache": "/c"})
        self.assertEqual(hbs.load_config(self.config(True))["orchestrator"]["every_s"], hbs.ORCH_EVERY_S)
        for bad in ({"every_s": 5}, {"task_roots": "/r"}, {"projects": ""}, {"other": 1}, "yes"):
            with self.assertRaises(hbs.ConfigError, msg=bad):
                hbs.load_config(self.config(bad))

    def test_block_is_cached_for_every_s_and_rescanned_after(self):
        cfg = {"projects": "/p", "task_roots": ["/r"], "every_s": 300, "cache": str(self.cache)}
        now = time.time()
        b1 = hbs.orchestrator_block(cfg, now, self.scan, lambda: "f" * 32)
        b2 = hbs.orchestrator_block(cfg, now + 100, self.scan, lambda: "f" * 32)
        self.assertEqual(len(self.scans), 1)
        self.assertEqual(b1, b2)
        self.assertEqual(self.scans[0], ("/p", "f" * 32, ("/r",)))
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o600)
        hbs.orchestrator_block(cfg, now + 400, self.scan, lambda: None)
        self.assertEqual(len(self.scans), 2)

    def test_a_failed_or_invalid_scan_costs_the_block_not_the_heartbeat(self):
        cfg = hbs.load_config(self.config({"cache": str(self.cache)}))

        def boom(*a, **k):
            raise PermissionError(SECRET)
        err = io.StringIO()
        with redirect_stderr(err):
            body = hbs.build(cfg, run=lambda *a, **k: None, scan=boom, salt=lambda: None)
            self.assertNotIn("orchestrator", body)
            body = hbs.build(cfg, run=lambda *a, **k: None, scan=lambda *a, **k: block(prompt=SECRET), salt=lambda: None)
            self.assertNotIn("orchestrator", body)
        self.assertIn("orchestrator scan failed: PermissionError", err.getvalue())
        self.assertIn("orchestrator block dropped", err.getvalue())
        self.assertNotIn(SECRET, err.getvalue())

    def test_posts_the_block_and_an_older_provider_gets_it_dropped(self):
        seen = []

        def opener(req, timeout):
            body = json.loads(req.data)
            seen.append(body)
            if "orchestrator" in body:
                raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(
                    b'{"type": "error", "error": {"message": "cortados: heartbeat field (unknown field) is invalid"}}'))
            return FakeResponse(b"{}")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = hbs.main(["--config", str(self.config({"cache": str(self.cache)}))], run=lambda *a, **k: None,
                            opener=opener, scan=self.scan, salt=lambda: None)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["orchestrator"]["sessions"][0]["session"], TAG)
        self.assertIsNone(live.validate_heartbeat(seen[0], time.time())[1])
        self.assertNotIn("orchestrator", seen[1])
        self.assertIn("posting without it", err.getvalue())

    def test_dry_run_shows_the_block(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = hbs.main(["--config", str(self.config({"cache": str(self.cache)})), "--dry-run"],
                            run=lambda *a, **k: None, scan=self.scan, salt=lambda: None)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["orchestrator"]["totals"]["24h"]["output"], 6000)


if __name__ == "__main__":
    unittest.main()
