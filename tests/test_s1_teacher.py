"""s1/label.py's provider-backed teacher (Forest 2026-11-02): teacher calls go through the cortados provider's /v1/messages with a
sub/<model> pin, so the provider chooses the account by scarcity. Tested against an in-thread fake provider (no real network):
success, retry on a retryable answer, a clean stop on a quota refusal, the Codex-label exclusion in common.read_labels, and the
dataset's exclusion of the labeler's own router rows."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
import common  # noqa: E402
import dataset  # noqa: E402
import label  # noqa: E402

ANSWER = {"steps": 1, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0, "outward": False,
          "work_kind": "conversation", "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False}


class FakeProvider:
    """Answers each POST /v1/messages with the next scripted (status, headers, body); records what it received."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        owner = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, headers, out = owner.script.pop(0) if owner.script else (500, {}, {"error": {"message": "script exhausted"}})
                if callable(out):
                    out = out(body)
                raw = json.dumps(out).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def answers_for(body):
    batch = json.loads(body["messages"][0]["content"].split("\n", 1)[1])
    return {"model": body["model"], "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": json.dumps([{"i": r["i"], **ANSWER} for r in batch])}]}


def err(msg, kind="overloaded_error"):
    return {"type": "error", "error": {"type": kind, "message": msg}}


class ProviderTeacherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._labels, self._backend, self._provider = label.LABELS, label.BACKEND, label.PROVIDER
        self._backoff = label.PROVIDER_BACKOFF_S
        label.LABELS = self.tmp / "labels.jsonl"
        label.PROVIDER_BACKOFF_S = (0.0, 0.0)               # no real sleeping in tests
        label.BACKEND = "provider"
        label._stop.clear()
        self.batch = [{"id": f"r{i}", "unit_kind": "interactive", "request": f"task {i}"} for i in range(3)]

    def tearDown(self):
        label.LABELS, label.BACKEND, label.PROVIDER = self._labels, self._backend, self._provider
        label.PROVIDER_BACKOFF_S = self._backoff
        label._stop.clear()
        if getattr(self, "fake", None):
            self.fake.close()

    def _serve(self, script):
        self.fake = FakeProvider(script)
        label.PROVIDER = (self.fake.url, "tok-test")

    def _work(self, model="claude-sonnet-5"):
        out = io.StringIO()
        with redirect_stdout(out):
            got = label.work((model, 0, self.batch))
        return got, out.getvalue()

    def test_success_pins_the_model_and_marks_the_traffic(self):
        self._serve([(200, {}, answers_for)])
        got, _ = self._work()
        self.assertEqual(got, 3)
        req = self.fake.seen[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["body"]["model"], "sub/claude-sonnet-5")
        self.assertEqual(req["headers"]["x-api-key"], "tok-test")
        run_id = req["headers"]["x-cortados-run-id"]
        self.assertRegex(run_id, r"^s1-label-\d{8}-\d+-\d+$")
        self.assertLessEqual(len(run_id), 64)
        self.assertTrue(req["body"]["messages"][0]["content"].startswith(common.LABELER_MARKER))
        rows = common.read_jsonl(label.LABELS)
        self.assertEqual([(r["id"], r["teacher"], r["sample"]) for r in rows], [(f"r{i}", "claude-sonnet-5", 0) for i in range(3)])
        self.assertFalse(label._stop.is_set())

    def test_retryable_status_is_retried_then_succeeds(self):
        self._serve([(529, {}, err("busy")), (502, {}, err("bad gateway", "api_error")), (200, {}, answers_for)])
        got, log = self._work()
        self.assertEqual(got, 3)
        self.assertEqual(len(self.fake.seen), 3)
        self.assertIn("attempt 1/3 failed", log)

    def test_should_retry_false_is_not_retried(self):
        self._serve([(503, {"x-should-retry": "false"}, err("conversation failed")), (200, {}, answers_for)])
        got, log = self._work()
        self.assertEqual((got, len(self.fake.seen)), (0, 1))
        self.assertTrue(label._stop.is_set())

    def test_retries_are_bounded(self):
        self._serve([(500, {}, err("x", "api_error"))] * 5)
        got, log = self._work()
        self.assertEqual((got, len(self.fake.seen)), (0, label.PROVIDER_ATTEMPTS))
        self.assertTrue(label._stop.is_set())
        self.assertIn("still failing", log)

    def test_quota_refusal_stops_the_pass_cleanly(self):
        msg = "cortados: sub/claude-sonnet-5: no claude subscription account can take this request (no capacity left: claude-cli (5h 97%))"
        self._serve([(503, {}, err(msg))])
        got, log = self._work()
        self.assertEqual((got, len(self.fake.seen)), (0, 1))     # no retry against spent quota
        self.assertTrue(label._stop.is_set())
        self.assertIn("stopping the pass", log)
        self.assertEqual(label.work(("claude-sonnet-5", 1, self.batch)), 0)   # later batches do not call at all
        self.assertEqual(len(self.fake.seen), 1)

    def test_client_error_stops_and_413_only_skips(self):
        self._serve([(413, {}, err("too large", "request_too_large"))])
        got, log = self._work()
        self.assertEqual(got, 0)
        self.assertFalse(label._stop.is_set())
        self.assertIn("batch dropped", log)
        self.fake.close()
        self._serve([(401, {}, err("bad token", "authentication_error"))])
        got, _ = self._work()
        self.assertTrue(label._stop.is_set())

    def test_unreachable_provider_stops_after_bounded_retries(self):
        label.PROVIDER = ("http://127.0.0.1:9", "tok-test")        # discard port: connection refused
        got, log = self._work()
        self.assertEqual(got, 0)
        self.assertTrue(label._stop.is_set())

    def test_classify(self):
        self.assertEqual(label.classify(429, None, "rate limited"), "retry")
        self.assertEqual(label.classify(529, None, "busy"), "retry")
        self.assertEqual(label.classify(503, "false", "x"), "stop")
        self.assertEqual(label.classify(503, None, "no capacity left"), "stop")
        self.assertEqual(label.classify(400, None, "unserved pin"), "stop")
        self.assertEqual(label.classify(413, None, "too large"), "skip")

    def test_main_refuses_a_codex_teacher(self):
        argv = sys.argv
        sys.argv = ["label.py", "--train-teacher", "gpt-6.1-sol"]
        try:
            with redirect_stderr(io.StringIO()) as e:
                self.assertEqual(label.main(), 2)
        finally:
            sys.argv = argv
        self.assertIn("only Claude teachers", e.getvalue())

    def test_provider_config_needs_url_and_token_file(self):
        old = {k: os.environ.get(k) for k in ("CORTADOS_PROVIDER_URL", "CORTADOS_PROVIDER_TOKEN_FILE")}
        try:
            os.environ.pop("CORTADOS_PROVIDER_URL", None)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                label.provider_config()
            self.assertEqual(cm.exception.code, label.EXIT_CONFIG)
            tf = self.tmp / "tok"; tf.write_text("secret-x\n")
            os.environ.update(CORTADOS_PROVIDER_URL="http://h:8432/", CORTADOS_PROVIDER_TOKEN_FILE=str(tf))
            self.assertEqual(label.provider_config(), ("http://h:8432", "secret-x"))
            os.environ["CORTADOS_PROVIDER_TOKEN_FILE"] = str(self.tmp / "missing")
            with redirect_stderr(io.StringIO()) as e, self.assertRaises(SystemExit) as cm:
                label.provider_config()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("unreadable", e.getvalue())
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


class MainExitCodeTests(unittest.TestCase):
    """0 = pass completed, 2 = config error, 3 = stopped early (partial labels kept): nightly branches on the code, not the log."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        reqs = [{"id": f"r{i}", "split": "train", "unit_kind": "interactive", "request": f"task {i}"} for i in range(4)]
        (self.tmp / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reqs))
        (self.tmp / "tok").write_text("tok-test\n")
        self._saved = (label.DATA, label.LABELS, label.BACKEND, label.PROVIDER, label.PROVIDER_BACKOFF_S, dict(label.TEACHER), sys.argv)
        self._env = {k: os.environ.get(k) for k in ("CORTADOS_PROVIDER_URL", "CORTADOS_PROVIDER_TOKEN_FILE",
                                                     "CORTADOS_S1_TEACHER_TRAIN", "CORTADOS_S1_TEACHER_HOLDOUT")}
        for k in self._env:
            os.environ.pop(k, None)
        label.DATA, label.LABELS, label.PROVIDER_BACKOFF_S = self.tmp, self.tmp / "labels.jsonl", (0.0, 0.0)
        label._stop.clear()
        self.fake = None

    def tearDown(self):
        (label.DATA, label.LABELS, label.BACKEND, label.PROVIDER, label.PROVIDER_BACKOFF_S, teacher, sys.argv) = self._saved
        label.TEACHER.clear(); label.TEACHER.update(teacher)
        for k, v in self._env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        label._stop.clear()
        if self.fake:
            self.fake.close()

    def _main(self, script, url=None):
        if script is not None:
            self.fake = FakeProvider(script)
            url = self.fake.url
        if url:
            os.environ["CORTADOS_PROVIDER_URL"] = url
        os.environ["CORTADOS_PROVIDER_TOKEN_FILE"] = str(self.tmp / "tok")
        sys.argv = ["label.py", "--k", "2", "--batch", "2", "--workers", "1", "--split", "train"]
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            try:
                code = label.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_completed_pass_exits_0(self):
        code, out = self._main([(200, {}, answers_for)] * 4)
        self.assertEqual(code, label.EXIT_OK)
        self.assertEqual(len(common.read_jsonl(label.LABELS)), 8)          # 4 requests x k=2

    def test_quota_refusal_exits_3_and_keeps_partial_labels(self):
        code, out = self._main([(200, {}, answers_for), (503, {}, err("no claude subscription account can take this request"))])
        self.assertEqual(code, label.EXIT_STOPPED)
        self.assertEqual(len(common.read_jsonl(label.LABELS)), 2)          # the first batch's labels survive
        self.assertIn("stopped early", out)

    def test_auth_error_exits_3(self):
        self.assertEqual(self._main([(401, {}, err("bad token", "authentication_error"))])[0], 3)

    def test_should_retry_false_exits_3(self):
        self.assertEqual(self._main([(503, {"x-should-retry": "false"}, err("terminal"))])[0], 3)

    def test_retries_exhausted_exits_3(self):
        self.assertEqual(self._main([(500, {}, err("x", "api_error"))] * 3)[0], 3)

    def test_missing_url_exits_2(self):
        self.assertEqual(self._main(None)[0], label.EXIT_CONFIG)


class CodexLabelExclusionTests(unittest.TestCase):
    def test_read_labels_drops_non_claude_teachers_and_counts_them(self):
        tmp = Path(tempfile.mkdtemp())
        rows = [{"id": "a", "teacher": "claude-sonnet-5", "sample": 0, "answers": ANSWER},
                {"id": "a", "teacher": "gpt-6.1-sol", "sample": 0, "answers": ANSWER},
                {"id": "b", "teacher": "gpt-6.1-sol", "sample": 1, "answers": ANSWER},
                {"id": "c", "teacher": "claude-opus-5", "sample": 0, "answers": ANSWER}]
        (tmp / "labels.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        old = common.DATA
        common.DATA = tmp
        try:
            with redirect_stderr(io.StringIO()) as e:
                got = common.read_labels()
            self.assertEqual(sorted(r["teacher"] for r in got), ["claude-opus-5", "claude-sonnet-5"])
            self.assertIn('excluded 2 non-Claude rows {"gpt-6.1-sol": 2}', e.getvalue())
            self.assertEqual(len(common.read_jsonl(tmp / "labels.jsonl")), 4)          # recorded rows are kept on disk
            with redirect_stderr(io.StringIO()):
                self.assertEqual(len(common.read_labels(claude_only=False)), 4)
        finally:
            common.DATA = old


class DatasetMachineRowTests(unittest.TestCase):
    def test_labeler_and_review_rows_are_excluded_and_counted(self):
        db = Path(tempfile.mkdtemp()) / "router-air.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, unit_kind TEXT, platform TEXT, request TEXT)")
        c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
            ("t1", 1.0, "interactive", "provider:claude-code", "refactor the parser"),
            ("t2", 2.0, "interactive", "provider:claude-code", f"{common.LABELER_MARKER} run=s1-label-20261102-1-1\n[{{\"i\": 0}}]"),
            ("t3", 3.0, "interactive", "provider:claude-code", "[cortados-review:" + "a" * 32 + ":rachel] Review requested; ..."),
        ])
        c.commit(); c.close()
        excluded = Counter()
        rows = dataset._rows_from_db(db, excluded)
        self.assertEqual([r["request"] for r in rows], ["refactor the parser"])
        self.assertEqual(excluded, Counter({"labeler": 1, "review": 1}))
        self.assertEqual(len(dataset._rows_from_db(db)), 1)        # the counter is optional


if __name__ == "__main__":
    unittest.main()
