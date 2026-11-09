"""scripts/tracker_check.py with fakes only: gh and git are injected callables that return canned text or raise.
GitHub is never called and no subprocess is started."""
import datetime as dt
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tracker_check as tc  # noqa: E402

MAIN_SHA = "a" * 40
REFS = "\n".join([
    f"main {MAIN_SHA}",
    f"done-work {'b' * 40}",          # merged
    f"in-progress {'c' * 40}",        # unmerged, mentioned by an open issue
    f"forgotten-branch {'d' * 40}",   # unmerged, mentioned nowhere
    f"fresh-worktree {MAIN_SHA}",     # at main's tip: no commits yet
    f"other-work {'e' * 40}",         # unmerged, mentioned only in a title
]) + "\n"
MERGED = "main\ndone-work\nfresh-worktree\n"

ISSUES = [
    {"number": 10, "title": "[provider] Done thing", "body": "## What\nStuff.\n\nBranch: `done-work`\n"},
    {"number": 11, "title": "[s1] Live thing", "body": "Builds on `in-progress`."},
    {"number": 12, "title": "[ops] Decide other-work branch", "body": None},
    {"number": 13, "title": "Tracker", "body": "## Live status — updated 2026-11-08 evening\n\nmore"},
]


def fake_gh_factory(issues=ISSUES, tracker_body=ISSUES[3]["body"], calls=None):
    import json

    def gh(cmd):
        if calls is not None:
            calls.append(cmd)
        assert cmd[0] == "gh", cmd
        if cmd[1:3] == ["issue", "list"]:
            assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "open"
            return json.dumps(issues)
        if cmd[1:3] == ["issue", "view"]:
            return json.dumps({"body": tracker_body})
        raise AssertionError(f"unexpected gh call {cmd}")
    return gh


def fake_git(cmd):
    assert cmd[0] == "git", cmd
    if cmd[1] == "for-each-ref":
        return REFS
    if cmd[1] == "branch":
        assert "--merged" in cmd
        return MERGED
    raise AssertionError(f"unexpected git call {cmd}")


class ParsingTests(unittest.TestCase):
    def test_parse_branch_reads_the_branch_line_only(self):
        self.assertEqual(tc.parse_branch("x\nBranch: `usage-api`\ny"), "usage-api")
        self.assertEqual(tc.parse_branch("  Branch: `fix/teacher-identity`"), "fix/teacher-identity")
        self.assertIsNone(tc.parse_branch("mentions `usage-api` without the line"))
        self.assertIsNone(tc.parse_branch(""))
        self.assertIsNone(tc.parse_branch(None))

    def test_live_status_date_accepts_em_dash_en_dash_and_hyphen(self):
        for dash in ("—", "–", "-", " — "):
            body = f"## Live status{dash}updated 2026-11-03 afternoon (Toronto)"
            self.assertEqual(tc.parse_live_status_date(body), dt.date(2026, 11, 3), repr(dash))
        self.assertIsNone(tc.parse_live_status_date("no such heading"))

    def test_live_status_date_rejects_impossible_date(self):
        with self.assertRaises(tc.CheckError):
            tc.parse_live_status_date("Live status — updated 2026-13-40")

    def test_mentions_is_whole_token(self):
        self.assertTrue(tc.mentions("see `foo-bar` here", "foo-bar"))
        self.assertTrue(tc.mentions("branch foo-bar, then", "foo-bar"))
        self.assertFalse(tc.mentions("see foo-bar-baz", "foo-bar"))
        self.assertFalse(tc.mentions("see foo", "foo-bar"))
        self.assertFalse(tc.mentions("fix/teacher-identity-two", "fix/teacher-identity"))
        self.assertTrue(tc.mentions("tag fix/teacher-identity.", "fix/teacher-identity"))
        for suffix in ("+bar", "@bar", "=bar", "$bar"):
            self.assertFalse(tc.mentions("see foo" + suffix, "foo"))
            self.assertTrue(tc.mentions("see foo" + suffix, "foo" + suffix))

    def test_parse_branches_classifies_merged_unmerged_and_empty(self):
        b = tc.parse_branches(REFS, MERGED, "main")
        self.assertEqual(b.merged, {"done-work"})
        self.assertEqual(b.unmerged, {"in-progress", "forgotten-branch", "other-work"})
        self.assertEqual(b.empty, {"fresh-worktree"})

    def test_parse_branches_needs_main(self):
        with self.assertRaises(tc.CheckError):
            tc.parse_branches(f"dev {MAIN_SHA}\n", "", "main")
        with self.assertRaises(tc.CheckError):
            tc.parse_branches("garbage line without sha\n", "", "main")

    def test_parse_issue_list_rejects_bad_json_and_shapes(self):
        with self.assertRaises(tc.CheckError):
            tc.parse_issue_list("not json")
        with self.assertRaises(tc.CheckError):
            tc.parse_issue_list('{"number": 1}')
        with self.assertRaises(tc.CheckError):
            tc.parse_issue_list('[{"title": "no number"}]')
        issues = tc.parse_issue_list('[{"number": 3, "title": "t", "body": null}]')
        self.assertEqual((issues[0].number, issues[0].body), (3, ""))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.issues = tc.parse_issue_list(__import__("json").dumps(ISSUES))
        self.branches = tc.parse_branches(REFS, MERGED, "main")

    def test_report_finds_merged_open_orphans_and_age(self):
        r = tc.build_report(self.issues, self.branches, ISSUES[3]["body"], dt.date(2026, 11, 9))
        self.assertEqual(r.merged_but_open, [(10, "[provider] Done thing", "done-work")])
        self.assertEqual(r.orphan_branches, ["forgotten-branch"])  # other-work is named in a title; in-progress in a body
        self.assertEqual(r.live_status_date, dt.date(2026, 11, 8))
        self.assertEqual(r.live_status_age_days, 1)
        self.assertFalse(r.stale)
        self.assertFalse(r.clean)
        text = "\n".join(r.lines())
        self.assertIn("#10", text)
        self.assertIn("forgotten-branch", text)
        self.assertIn("result: drift found", text)

    def test_stale_tracker_warns_past_two_days(self):
        fresh = tc.build_report([], tc.Branches(), ISSUES[3]["body"], dt.date(2026, 11, 10))
        self.assertEqual(fresh.live_status_age_days, 2)
        self.assertFalse(fresh.stale)
        self.assertTrue(fresh.clean)
        stale = tc.build_report([], tc.Branches(), ISSUES[3]["body"], dt.date(2026, 11, 11))
        self.assertTrue(stale.stale)
        self.assertFalse(stale.clean)
        self.assertIn("WARNING: older than 2 days", "\n".join(stale.lines()))

    def test_missing_live_status_line_is_never_clean(self):
        r = tc.build_report([], tc.Branches(), "no heading here", dt.date(2026, 11, 8))
        self.assertIsNone(r.live_status_date)
        self.assertTrue(r.stale)
        self.assertFalse(r.clean)
        self.assertIn("NOT FOUND", "\n".join(r.lines()))

    def test_empty_branch_at_main_tip_appears_in_no_finding(self):
        issues = tc.parse_issue_list('[{"number": 1, "title": "t", "body": "Branch: `fresh-worktree`"}]')
        r = tc.build_report(issues, self.branches, ISSUES[3]["body"], dt.date(2026, 11, 8))
        self.assertEqual(r.merged_but_open, [])
        self.assertNotIn("fresh-worktree", r.orphan_branches)


class MainTests(unittest.TestCase):
    def run_main(self, argv, gh, git):
        out, err = io.StringIO(), io.StringIO()
        rc = tc.main(argv, gh=gh, git=git, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_drift_exits_one_and_prints_findings(self):
        calls = []
        rc, out, err = self.run_main(["--today", "2026-11-09"], fake_gh_factory(calls=calls), fake_git)
        self.assertEqual(rc, 1)
        self.assertIn("#10 [provider] Done thing", out)
        self.assertIn("forgotten-branch", out)
        self.assertIn("1 day(s) old", out)
        self.assertEqual(err, "")
        self.assertTrue(all(c[0] == "gh" for c in calls))
        self.assertTrue(all("--repo" in c and c[c.index("--repo") + 1] == tc.DEFAULT_REPO for c in calls))
        # Only read-only gh subcommands are used.
        self.assertEqual(sorted({tuple(c[1:3]) for c in calls}), [("issue", "list"), ("issue", "view")])

    def test_clean_exits_zero(self):
        issues = [{"number": 11, "title": "[s1] Live thing", "body": "`in-progress` `forgotten-branch` other-work"}]
        rc, out, _ = self.run_main(["--today", "2026-11-08"], fake_gh_factory(issues=issues), fake_git)
        self.assertEqual(rc, 0, out)
        self.assertIn("result: clean", out)

    def test_gh_failure_exits_two_and_never_says_clean(self):
        def broken_gh(cmd):
            raise tc.CheckError("gh issue list failed (exit 4): not logged in")
        rc, out, err = self.run_main(["--today", "2026-11-08"], broken_gh, fake_git)
        self.assertEqual(rc, 2)
        self.assertIn("tracker check FAILED", err)
        self.assertIn("not logged in", err)
        self.assertNotIn("clean", out + err)

    def test_git_failure_exits_two(self):
        def broken_git(cmd):
            raise tc.CheckError("git for-each-ref failed (exit 128): not a git repository")
        rc, out, err = self.run_main(["--today", "2026-11-08"], fake_gh_factory(), broken_git)
        self.assertEqual(rc, 2)
        self.assertIn("not a git repository", err)
        self.assertNotIn("clean", out + err)

    def test_full_issue_page_is_a_failure_not_a_clean_result(self):
        issues = [{"number": n, "title": "t", "body": ""} for n in range(tc.ISSUE_PAGE)]
        rc, out, err = self.run_main(["--today", "2026-11-08"], fake_gh_factory(issues=issues), fake_git)
        self.assertEqual(rc, 2)
        self.assertIn("page cap", err)

    def test_bad_today_exits_two(self):
        rc, _, err = self.run_main(["--today", "yesterday"], fake_gh_factory(), fake_git)
        self.assertEqual(rc, 2)
        self.assertIn("FAILED", err)

    def test_run_reports_missing_binary_as_check_error(self):
        with self.assertRaises(tc.CheckError) as cm:
            tc.run(["/nonexistent/binary/for/tracker-check-test", "x"])
        self.assertIn("not installed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
