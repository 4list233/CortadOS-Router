"""JSONL records split on "\\n" only (2026-11-17 regression).

A request containing U+2028 was appended to s1/data/requests.jsonl by json.dumps(..., ensure_ascii=False), which leaves
U+2028/U+2029/U+0085 raw inside strings. read_jsonl used str.splitlines(), which also breaks lines at those characters
(and at \\x0b, \\x0c, \\x1c-\\x1e), so every later read raised JSONDecodeError "Unterminated string", dataset.py failed every
cycle and no S1 training ran. Readers now split on "\\n" only; writers (common.dumps_jsonl) also escape the three characters
json.dumps leaves raw, so new files are safe for any line-splitting reader, and files written before stay readable.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "s1"))
import common  # noqa: E402
import dataset  # noqa: E402

# every character str.splitlines() breaks on besides \n and \r, inside a string value
TRICKY = "a b c\x85d\x0ce\x0bf\x1cg\x1dh\x1ei"
ROW = {"id": "rt.x", "source": "hermes-provider", "unit_kind": "interactive", "request": "fix this" + TRICKY, "split": "train"}


class JsonlRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="s1-jsonl-"))

    def test_dumps_jsonl_escapes_the_line_breaks_json_leaves_raw_and_round_trips(self):
        line = common.dumps_jsonl(ROW)
        for ch in (" ", " ", "\x85", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\n", "\r"):
            self.assertNotIn(ch, line)
        self.assertEqual(len(line.splitlines()), 1, "safe even for a splitlines() reader")
        self.assertEqual(json.loads(line), ROW)
        self.assertIn("é", common.dumps_jsonl({"t": "é"}), "other non-ASCII stays readable UTF-8")

    def test_rows_with_line_separators_round_trip_through_write_and_read_jsonl(self):
        p = self.d / "rows.jsonl"
        rows = [ROW, {"id": "2", "request": " "}, {"id": "3", "request": "x\x85"}, {"id": "4", "request": "\x0c"}]
        p.write_text("".join(common.dumps_jsonl(r) + "\n" for r in rows))
        self.assertEqual(common.read_jsonl(p), rows)

    def test_a_file_written_before_the_fix_with_raw_separators_still_reads(self):
        # the existing requests.jsonl on the Studio: raw U+2028 inside a string, CRLF on one line, a blank line
        p = self.d / "old.jsonl"
        raw = json.dumps(ROW, ensure_ascii=False)
        self.assertIn(" ", raw)
        p.write_text(json.dumps({"id": "0", "request": "ok"}) + "\n" + raw + "\r\n\n" + json.dumps({"id": "9", "request": "z"}) + "\n")
        self.assertEqual([r["id"] for r in common.read_jsonl(p)], ["0", "rt.x", "9"])
        self.assertEqual(common.read_jsonl(p)[1], ROW)

    def test_jsonl_lines_splits_on_newline_only(self):
        self.assertEqual(common.jsonl_lines("a b\r\nc\x85\n"), ["a b", "c\x85", ""])


class DatasetResumeReadsSeparators(unittest.TestCase):
    """dataset.py's resume path (`have = {r["id"] for r in read_jsonl(out)}`) on a requests.jsonl holding such a row."""

    def test_resume_over_a_row_with_u2028_appends_and_reads_back(self):
        d = Path(tempfile.mkdtemp(prefix="s1-dataset-"))
        (d / "data").mkdir()
        out = d / "data/requests.jsonl"
        old = {"id": "rt.old", "source": "hermes-provider", "unit_kind": "interactive", "request": "old row here", "split": "train"}
        out.write_text(json.dumps(old, ensure_ascii=False) + "\n")          # as written before the fix: raw separators
        db = d / "router.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE routes (turn_id TEXT, ts REAL, unit_kind TEXT, platform TEXT, request TEXT)")
        c.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", [
            ("old", 1.0, "interactive", "provider", "old row here"),   # already present: not re-added
            ("new", 2.0, "interactive", "provider", "new\x85request with breaks"),
        ])
        c.commit(); c.close()
        env = {k: v for k, v in os.environ.items() if k != "CORTADOS_ROUTER_DB_EXTRA"}
        with mock.patch.object(dataset, "DATA", d / "data"), mock.patch.object(dataset, "ROUTER_DB", db), \
                mock.patch.object(dataset, "SHEET", d / "no-sheet.csv"), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("sys.stdout"):
            dataset.main()
            dataset.main()                                                       # idempotent: the second run adds nothing
        rows = common.read_jsonl(out)
        self.assertEqual([r["id"] for r in rows], ["rt.old", "rt.new"])
        self.assertEqual(rows[0], old)
        self.assertEqual(rows[1]["request"], "new\x85request with breaks")
        appended = out.read_text().split("\n")[1]
        self.assertNotIn(" ", appended)
        self.assertNotIn("\x85", appended)


if __name__ == "__main__":
    unittest.main()
