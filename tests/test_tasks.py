"""GET /v1/cortados/tasks (provider/tasks.py): a bounded, metadata-only projection of router.db for the dashboard's
Tasks section. The database is built from core.DDL (the live writer's schema) in a throwaway directory; the endpoint
runs over the real HTTP server with no vendor CLI."""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from cortados_router import core
from cortados_router.provider import tasks
from cortados_router.provider.server import ProviderServer, Registry

TOKEN = "t-tasks-secret"
NOW = 1_800_000_000.0
SECRET = "SECRET PROMPT TEXT never to be published"
ANSWERS = {"steps": 1, "ambiguity": 0, "blast_radius": 1, "context_size": 0, "novelty": 0, "parallelizability": 0,
           "outward": False, "work_kind": "code", "urgency": "now", "needs_clarification": False, "parts": 1, "diversity": False}
ROUTED_RULES = ["needs index ≥ 3 for code (D=4, R1); 5 of 9 reachable model routes qualify",
                "chosen claude-cli/sonnet: code index 4 clears it; cheapest qualifying route ($3/1M → weight 2, pool scarcity 0.1, cost 12)",
                f"escalated (face): {SECRET} → D floored at 6"]


def home() -> Path:
    d = Path(tempfile.mkdtemp())
    c = sqlite3.connect(d / "router.db")
    c.executescript(core.DDL)
    for col in ("scarcity", "context", "task_context"):     # the columns core._db() adds to the DDL on open
        c.execute(f"ALTER TABLE routes ADD COLUMN {col} TEXT")
    c.commit(); c.close()
    return d


def insert(d: Path, **cols) -> None:
    """One `routes` row with log_route's defaults: an S1 decision carrying the secret prompt, answers and rules."""
    row = {"turn_id": f"execution:provider:{os.urandom(4).hex()}", "ts": NOW, "session_id": "sess-a", "request": SECRET,
           "request_chars": len(SECRET), "engine": "s1", "degraded": 0, "answers": json.dumps(ANSWERS), "band": "R1",
           "lane": "claude-cli", "model": "sonnet", "actual_model": "anthropic/claude-sonnet",
           "hard_rules": json.dumps(ROUTED_RULES), "turn_ms": 1234, "response_chars": 500, "context": SECRET,
           "engine_error": None}
    row.update(cols)
    c = sqlite3.connect(d / "router.db")
    c.execute(f"INSERT INTO routes ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    c.commit(); c.close()


def pin(d: Path, **cols) -> None:
    """A sub/ pin as routing.choose_pinned logs it: engine `pin`, no answers, and no outcome yet (log_outcome and
    log_execution fill actual_model, turn_ms and response_chars only once the turn has run)."""
    insert(d, **{"engine": "pin", "band": "pin", "answers": "null", "lane": "codex-cli", "model": "gpt-5",
                 "actual_model": None, "turn_ms": None, "response_chars": None,
                 "hard_rules": json.dumps(["pinned by the client: sub/gpt-5"]), **cols})


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.home = home()
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_payload_shape_and_no_request_text(self):
        insert(self.home, turn_id="t-routed")
        pin(self.home, turn_id="t-pin", ts=NOW - 1)
        v = tasks.collect(self.home)
        raw = json.dumps(v, allow_nan=False)
        self.assertNotIn("SECRET", raw)                          # request, context and the escalation reason stay out
        self.assertEqual(set(v), {"tasks", "sessions", "totals", "row_limit", "truncated", "unassigned_decisions",
                                  "tokens_status", "percentage_definition", "notes", "errors", "token_totals"})
        self.assertEqual(v["errors"], [])
        self.assertEqual(v["row_limit"], tasks.ROW_LIMIT)
        self.assertEqual([t["task_id"] for t in v["tasks"]], ["t-routed", "t-pin"])    # newest first
        t = v["tasks"][0]
        self.assertEqual(set(t), {"task_id", "ts", "session_id", "lane", "model", "actual_model", "band", "degraded",
                                  "elapsed_ms", "request_chars", "response_chars", "tokens", "tokens_status",
                                  "routing_kind", "s1_reason"})
        for absent in ("request", "context", "task_context", "engine_error", "answers", "hard_rules", "engine", "features"):
            self.assertNotIn(absent, t)
        self.assertEqual((t["lane"], t["model"], t["actual_model"], t["band"], t["degraded"]),
                         ("claude-cli", "sonnet", "anthropic/claude-sonnet", "R1", False))
        self.assertEqual((t["elapsed_ms"], t["request_chars"], t["response_chars"]), (1234, len(SECRET), 500))
        # tokens are not recorded per route: the field says so and never carries a number
        self.assertIsNone(t["tokens"]); self.assertEqual(t["tokens_status"], "not recorded")
        self.assertEqual(v["tokens_status"], "not recorded")
        self.assertEqual(t["routing_kind"], "routed")
        self.assertEqual(t["s1_reason"], "S1 decision; band R1; steps=1, ambiguity=0, blast_radius=1, context_size=0, "
                         "novelty=0, parallelizability=0, work_kind=code, urgency=now, outward=false; "
                         "rules: cheapest qualifying route, escalated difficulty floor, index requirement")
        p = v["tasks"][1]
        self.assertEqual(p["routing_kind"], "pinned")
        self.assertEqual(p["s1_reason"], "client pin; S1 not asked; band pin; rules: client pin")
        self.assertIsNone(p["actual_model"]); self.assertIsNone(p["elapsed_ms"])     # not yet logged: null, never 0
        self.assertIn("pinned", v["percentage_definition"]); self.assertIn(str(tasks.ROW_LIMIT), v["percentage_definition"])
        self.assertTrue(any("not recorded" in n for n in v["notes"]))

    def test_percentage_per_session_and_totals(self):
        for i in range(3):
            insert(self.home, session_id="sess-a", ts=NOW - i)
        pin(self.home, session_id="sess-a", ts=NOW - 10)
        pin(self.home, session_id="sess-b", ts=NOW - 20)
        pin(self.home, session_id="sess-b", ts=NOW - 21)
        insert(self.home, session_id="sess-c", ts=NOW - 30, lane=None, model=None, band="R2")   # no route chosen
        insert(self.home, session_id=None, ts=NOW - 40)                                          # no session recorded
        v = tasks.collect(self.home)
        by = {s["session_id"]: s for s in v["sessions"]}
        self.assertEqual([s["session_id"] for s in v["sessions"]], ["sess-a", "sess-b", "sess-c"])   # most recent first
        self.assertEqual(by["sess-a"], {"session_id": "sess-a", "decisions": 4, "routed": 3, "pinned": 1, "unselected": 0,
                                        "last_ts": NOW, "routed_pct": 75.0})
        self.assertEqual((by["sess-b"]["routed_pct"], by["sess-b"]["pinned"]), (0.0, 2))      # pinned work is not routed
        self.assertEqual((by["sess-c"]["routed_pct"], by["sess-c"]["unselected"]), (0.0, 1))
        self.assertEqual(v["unassigned_decisions"], 1)                                         # counted, not pooled
        self.assertEqual(v["totals"], {"decisions": 8, "routed": 4, "pinned": 3, "unselected": 1, "routed_pct": 50.0})

    def test_degraded_decision_counts_as_routed(self):
        insert(self.home, degraded=1, answers="null", band="R2",
               hard_rules=json.dumps(["engine degraded → fallback band", "no available candidate; execution refused"]))
        t = tasks.collect(self.home)["tasks"][0]
        self.assertTrue(t["degraded"]); self.assertEqual(t["routing_kind"], "routed")
        # S1 returned no answers (log_route writes JSON null): "not recorded", never "unreadable" or "invalid"
        self.assertEqual(t["s1_reason"], "scorer degraded; band R2; typed answers not recorded; "
                         "rules: degraded scorer fallback, no candidate")

    def test_unreadable_or_foreign_inputs_are_named_not_guessed(self):
        insert(self.home, turn_id="cut", answers='{"steps": 1, "amb', hard_rules='["x"', band="R9")
        insert(self.home, turn_id="foreign", ts=NOW - 1, answers=json.dumps({"steps": 9, "work_kind": "x"}),
               hard_rules=json.dumps(["some rule nobody labelled", 7]))
        a, b = tasks.collect(self.home)["tasks"]
        self.assertEqual(a["s1_reason"], "S1 decision; band not recorded; typed answers unreadable; hard rules unreadable")
        self.assertIsNone(a["band"])
        self.assertEqual(b["s1_reason"], "S1 decision; band R1; typed answers recorded but none readable; "
                         "2 more rule(s), details withheld")

    def test_identifiers_are_validated(self):
        insert(self.home, turn_id="ok", session_id="10.0.0.1", lane="claude cli", model="a b", actual_model="a/b:c")
        insert(self.home, turn_id="<b>x</b>", ts=NOW - 1, session_id="s" * 200)
        v = tasks.collect(self.home)
        a, b = v["tasks"]
        self.assertEqual((a["task_id"], a["session_id"], a["lane"], a["model"], a["actual_model"]), ("ok", None, None, None, "a/b:c"))
        self.assertEqual((b["task_id"], b["session_id"]), (None, None))
        self.assertEqual((v["sessions"], v["unassigned_decisions"]), ([], 2))

    def test_row_limit_and_truncation_flag(self):
        for i in range(tasks.ROW_LIMIT + 1):
            insert(self.home, turn_id=f"t{i}", ts=NOW - i)
        v = tasks.collect(self.home)
        self.assertTrue(v["truncated"])
        self.assertEqual(len(v["tasks"]), tasks.ROW_LIMIT)
        self.assertEqual(v["tasks"][0]["task_id"], "t0")                    # newest kept, oldest dropped
        self.assertEqual(v["totals"]["decisions"], tasks.ROW_LIMIT)          # the share uses the same window
        sqlite3.connect(self.home / "router.db").execute("DELETE FROM routes WHERE turn_id = 't0'").connection.commit()
        self.assertFalse(tasks.collect(self.home)["truncated"])

    def test_absent_or_unreadable_database_is_an_error_not_empty(self):
        d = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, d, True)
        v = tasks.collect(d)
        self.assertEqual(v["errors"], [{"source": "router.db", "error": "absent"}])
        self.assertEqual((v["tasks"], v["sessions"], v["totals"]["routed_pct"]), ([], [], None))
        (d / "router.db").write_bytes(b"not a database")
        v = tasks.collect(d)
        self.assertEqual(v["errors"], [{"source": "router.db", "error": "DatabaseError"}])
        self.assertNotIn(str(d), json.dumps(v))                              # no path in the published error

    def test_reads_only_and_never_takes_the_writer_lock(self):
        insert(self.home)
        with mock.patch.object(core, "_db_lock") as lock:
            v = tasks.collect(self.home)
        lock.__enter__.assert_not_called(); lock.acquire.assert_not_called()
        self.assertEqual(len(v["tasks"]), 1)


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.home = home()
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {"CORTADOS_HOME": str(self.home)})
        env.start(); self.addCleanup(env.stop)
        self.reg = Registry(gateway_url="", token=TOKEN, claude_bin=["true"])
        self.httpd = ProviderServer(("127.0.0.1", 0), self.reg)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()

    def get(self, path, token=None):
        h = {"authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=h), timeout=10) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_refused_without_or_with_a_wrong_token(self):
        insert(self.home)
        for tok in (None, "wrong-token-00000000"):
            code, body, _ = self.get("/v1/cortados/tasks", tok)
            self.assertEqual(code, 401, tok)
            self.assertEqual(json.loads(body)["error"]["type"], "authentication_error")
            self.assertNotIn("SECRET", body); self.assertNotIn("sess-a", body)

    def test_owner_token_gets_the_collected_view(self):
        insert(self.home, turn_id="t1")
        pin(self.home, turn_id="t2", ts=NOW - 1)
        code, body, headers = self.get("/v1/cortados/tasks?since=0", TOKEN)
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("SECRET", body)
        self.assertEqual(json.loads(body), tasks.collect(self.home))
        self.assertEqual(json.loads(body)["sessions"][0]["routed_pct"], 50.0)

    def test_a_failing_view_is_a_clean_500(self):
        def boom(reg):
            raise RuntimeError("/Users/someone/secret path")
        self.httpd.tasks_view = boom
        code, body, _ = self.get("/v1/cortados/tasks", TOKEN)
        self.assertEqual(code, 500)
        self.assertNotIn("someone", body)
        self.assertIn("tasks view unavailable", body)

    def test_absent_database_is_reported_in_the_payload(self):
        shutil.rmtree(self.home); self.home.mkdir()
        code, body, _ = self.get("/v1/cortados/tasks", TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["errors"], [{"source": "router.db", "error": "absent"}])


if __name__ == "__main__":
    unittest.main()
