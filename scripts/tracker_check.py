#!/usr/bin/env python3
"""Tracker drift check: compares the GitHub issues with the local branches and the tracker's Live status date.

Reads only. Uses `gh` (read-only subcommands) and `git` (local refs) and reports:
  (a) open issues whose named branch ("Branch: `name`" in the body) is already merged to main,
  (b) local branches with unmerged commits that no open issue mentions,
  (c) the age in days of the "Live status — updated YYYY-MM-DD" line of the tracker issue, with a warning past
      MAX_LIVE_STATUS_AGE_DAYS (reason: the tracker went stale for five days in November 2026 while pause notes and
      memory carried their own lists).

Exit codes: 0 clean, 1 drift found, 2 the check itself failed (gh or git unavailable, auth missing, unparsable
output). A failure never prints a clean result, so a caller gating a merge on this script cannot mistake a broken
check for a passing one. Stdlib only. Run: `python3 -B scripts/tracker_check.py` (add `--today YYYY-MM-DD` to pin the
date, `--repo`, `--tracker`, `--main`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

DEFAULT_REPO = "4list233/CortadOS-Router"
DEFAULT_TRACKER = 60
DEFAULT_MAIN = "main"
# Two days: a session pauses at most daily, so a date older than that means a pause happened without an update.
MAX_LIVE_STATUS_AGE_DAYS = 2
# gh's page cap for `issue list`; the repo had 85 issues on 2026-11-08, so one page is enough and more would hide
# a silent truncation behind a clean result. The check fails loudly when the page is full (see fetch_open_issues).
ISSUE_PAGE = 200

Runner = Callable[[list[str]], str]


class CheckError(Exception):
    """The check could not run; the caller exits 2 and reports nothing as clean."""


@dataclass
class Issue:
    number: int
    title: str
    body: str

    @property
    def branch(self) -> str | None:
        return parse_branch(self.body)


@dataclass
class Branches:
    """Local branches by state relative to main. `empty` are branches at main's tip (a worktree with no commits yet):
    neither merged work nor unmerged work, so they appear in no finding."""

    merged: set[str] = field(default_factory=set)
    unmerged: set[str] = field(default_factory=set)
    empty: set[str] = field(default_factory=set)


@dataclass
class Report:
    merged_but_open: list[tuple[int, str, str]]  # (issue number, title, branch)
    orphan_branches: list[str]
    live_status_date: dt.date | None
    live_status_age_days: int | None

    @property
    def stale(self) -> bool:
        return self.live_status_age_days is None or self.live_status_age_days > MAX_LIVE_STATUS_AGE_DAYS

    @property
    def clean(self) -> bool:
        return not self.merged_but_open and not self.orphan_branches and not self.stale

    def lines(self) -> list[str]:
        out = ["(a) open issues whose branch is merged to main:"]
        out += [f"    #{n} {title} — branch `{b}`" for n, title, b in self.merged_but_open] or ["    none"]
        out.append("(b) local unmerged branches no open issue mentions:")
        out += [f"    {b}" for b in self.orphan_branches] or ["    none"]
        if self.live_status_date is None:
            out.append("(c) Live status date: NOT FOUND in the tracker body (expected 'Live status — updated YYYY-MM-DD')")
        else:
            flag = "  WARNING: older than %d days" % MAX_LIVE_STATUS_AGE_DAYS if self.stale else ""
            out.append(f"(c) Live status dated {self.live_status_date.isoformat()}, {self.live_status_age_days} day(s) old{flag}")
        out.append("result: clean" if self.clean else "result: drift found")
        return out


# ---- parsing (pure; tested with fakes) ----

_BRANCH_LINE = re.compile(r"^\s*Branch:\s*`([^`\s]+)`", re.MULTILINE)
# The dash between "status" and "updated" has been an em dash, an en dash or a hyphen; accept any run of them.
_LIVE_DATE = re.compile(r"Live status\s*[-–—]+\s*updated\s+(\d{4}-\d{2}-\d{2})")


def parse_branch(body: str) -> str | None:
    """The branch an issue names with a `Branch: \\`name\\`` line; None when there is none."""
    m = _BRANCH_LINE.search(body or "")
    return m.group(1) if m else None


def parse_live_status_date(body: str) -> dt.date | None:
    m = _LIVE_DATE.search(body or "")
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError as exc:
        raise CheckError(f"Live status date {m.group(1)!r} is not a calendar date: {exc}") from exc


# These characters can continue a Git branch name in prose. Keep punctuation such as commas and periods
# available as delimiters, while preventing a short branch from matching the prefix of names like ``foo+bar``.
_BRANCH_TOKEN_CONTINUATION = r"\w/.\-+@=$"
_BRANCH_TOKEN_AFTER = r"\w/\-+@=$"


def mentions(text: str, branch: str) -> bool:
    """True when `branch` appears in `text` as a whole token.

    Substring matching would let ``foo`` claim ``foo-bar`` or ``foo+bar``. A dot after the name ends a sentence
    unless another branch-name character follows it (``foo.bar`` is a longer name, ``foo.`` is a mention of ``foo``).
    """
    return re.search(
        r"(?<![" + _BRANCH_TOKEN_CONTINUATION + r"])" + re.escape(branch)
        + r"(?![" + _BRANCH_TOKEN_AFTER + r"]|\.[" + _BRANCH_TOKEN_CONTINUATION + r"])",
        text or "",
    ) is not None


def parse_issue_list(raw: str) -> list[Issue]:
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckError(f"gh issue list returned non-JSON output: {exc}") from exc
    if not isinstance(rows, list):
        raise CheckError("gh issue list returned JSON that is not a list")
    issues = []
    for row in rows:
        try:
            issues.append(Issue(int(row["number"]), str(row.get("title") or ""), str(row.get("body") or "")))
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckError(f"gh issue list row missing fields: {row!r} ({exc})") from exc
    return issues


def parse_branches(refs: str, merged_list: str, main: str) -> Branches:
    """`refs` is `git for-each-ref --format='%(refname:short) %(objectname)' refs/heads`; `merged_list` is
    `git branch --format=%(refname:short) --merged <main>`."""
    tips: dict[str, str] = {}
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise CheckError(f"unexpected git for-each-ref line: {line!r}")
        tips[parts[0]] = parts[1]
    if main not in tips:
        raise CheckError(f"branch {main!r} not found among local refs")
    merged = {b.strip() for b in merged_list.splitlines() if b.strip()}
    out = Branches()
    for name, sha in tips.items():
        if name == main:
            continue
        if sha == tips[main]:
            out.empty.add(name)
        elif name in merged:
            out.merged.add(name)
        else:
            out.unmerged.add(name)
    return out


def build_report(issues: list[Issue], branches: Branches, tracker_body: str, today: dt.date) -> Report:
    merged_but_open = sorted(
        (i.number, i.title, i.branch) for i in issues if i.branch and i.branch in branches.merged
    )
    corpus = [f"{i.title}\n{i.body}" for i in issues]
    orphans = sorted(b for b in branches.unmerged if not any(mentions(text, b) for text in corpus))
    live = parse_live_status_date(tracker_body)
    age = (today - live).days if live else None
    return Report(merged_but_open, orphans, live, age)


# ---- runners (subprocess; replaced by fakes in tests) ----

def run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise CheckError(f"{cmd[0]} is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"{' '.join(cmd)} timed out after 60 s") from exc
    if proc.returncode != 0:
        raise CheckError(f"{' '.join(cmd)} failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def fetch_open_issues(gh: Runner, repo: str) -> list[Issue]:
    raw = gh(["gh", "issue", "list", "--repo", repo, "--state", "open", "--limit", str(ISSUE_PAGE),
              "--json", "number,title,body"])
    issues = parse_issue_list(raw)
    if len(issues) >= ISSUE_PAGE:
        raise CheckError(f"gh returned {len(issues)} open issues, the page cap; raise ISSUE_PAGE before trusting (b)")
    return issues


def fetch_tracker_body(gh: Runner, repo: str, tracker: int) -> str:
    raw = gh(["gh", "issue", "view", str(tracker), "--repo", repo, "--json", "body"])
    try:
        body = json.loads(raw)["body"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CheckError(f"gh issue view {tracker} returned no body: {exc}") from exc
    return body or ""


def fetch_branches(git: Runner, main: str) -> Branches:
    refs = git(["git", "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"])
    merged = git(["git", "branch", "--format=%(refname:short)", "--merged", main])
    return parse_branches(refs, merged, main)


def main(argv: list[str] | None = None, gh: Runner = run, git: Runner = run, out=sys.stdout, err=sys.stderr) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--tracker", type=int, default=DEFAULT_TRACKER, help="tracker issue number")
    p.add_argument("--main", default=DEFAULT_MAIN, help="name of the main branch")
    p.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to the local date")
    args = p.parse_args(argv)
    try:
        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        issues = fetch_open_issues(gh, args.repo)
        tracker_body = fetch_tracker_body(gh, args.repo, args.tracker)
        branches = fetch_branches(git, args.main)
        report = build_report(issues, branches, tracker_body, today)
    except (CheckError, ValueError) as exc:
        print(f"tracker check FAILED: {exc}", file=err)
        return 2
    print(f"tracker check — {args.repo} #{args.tracker}, {len(issues)} open issues, "
          f"{len(branches.unmerged)} unmerged / {len(branches.merged)} merged / {len(branches.empty)} empty local branches",
          file=out)
    for line in report.lines():
        print(line, file=out)
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
