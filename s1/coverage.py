"""Coverage top-up (Forest 2026-09-24): the live logs are administrative; the scorer has barely seen repo coding, CLI/devops, database
migrations, computer/browser use or data work. A teacher writes realistic requests in those categories, grounded in the operator's systems (CONTEXT),
tagged source=synthetic-coverage, spread across difficulty. They are labelled and split like any other row; the holdout share is kept so
the grade also covers them. Never more than SYNTHETIC_SHARE of the training set (real traffic must stay the majority).
  python3 coverage.py [--per-category 40]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from common import DATA, read_jsonl, split_of

STATE = Path(os.environ.get("CORTADOS_HOME") or Path.home() / ".cortados")
SYNTHETIC_SHARE = 0.35
# Who the synthetic requests are for. The published default is a neutral description; point CORTADOS_S1_COVERAGE_CONTEXT at a
# private text file to ground the teacher in your own systems (that file stays out of the repository).
_DEFAULT_CONTEXT = """The operator runs a small software business and a fleet of AI agents on Hermes from one Mac. Typical systems the agents
work on: a TypeScript/Node product codebase on Postgres deployed to a PaaS (with per-customer copies and custom modules); small
customer projects (an invoicing tool, a community-group website); the agent fleet itself (Python plugins, launchd and cron jobs,
chat bots per agent, a model router, a local memory service in Node/SQLite, ollama models, an MLX training pipeline);
headed-browser runs for logins and developer-portal setup; macOS desktop automation; calendar, email drafts (never sent),
notes, CRM and e-commerce tools; the Cursor, Codex, Claude Code and Antigravity CLIs; git repositories on GitHub."""
CONTEXT = (Path(os.environ["CORTADOS_S1_COVERAGE_CONTEXT"]).read_text() if os.environ.get("CORTADOS_S1_COVERAGE_CONTEXT")
           else _DEFAULT_CONTEXT)
CATEGORIES = {
    "code": "repo coding: implement a feature, fix a bug, refactor, write tests, review a PR, add an API endpoint, in one of the operator's codebases",
    "cli-devops": "CLI/devops: shell work, deployments to the PaaS, launchd/cron, env and secrets handling, logs, package upgrades, git operations",
    "db-migration": "database work: schema migrations, backfills, data model changes, SQLite/Postgres queries, index and constraint changes, exports",
    "computer-use": "computer and browser use: drive a headed browser or the macOS desktop to log in, fill forms, set up a chat-bot app, download a report, take screenshots",
    "data-analysis": "data and analysis: pull numbers from the usage ledger or a CSV, build a chart or report, reconcile invoices, compute a metric",
}
SYSTEM = ("You write REALISTIC requests that the operator would type to their agents, in their voice: terse, lowercase, typos allowed, sometimes a pasted "
          "error or a URL placeholder <url>. Vary difficulty deliberately: a third trivial one-liners, a third medium multi-step, a third genuinely hard "
          "(ambiguous goal, several dependent parts, risky or irreversible, or needing investigation). Some should require a clarifying question; some "
          "should be explicitly outward (deploy, send, publish, delete, spend). No two alike. Reply with ONLY a JSON array of strings.")


def _env():
    env = dict(os.environ)
    for line in (STATE / "env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1); env[k] = v.strip().strip('"')
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def generate(cat: str, what: str, n: int, model: str) -> list[str]:
    cwd = STATE / "teacher-cwd"; cwd.mkdir(parents=True, exist_ok=True)
    prompt = f"{CONTEXT}\n\nCategory: {what}.\nWrite {n} requests."
    argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--max-turns", "1", "--tools", "", "--disable-slash-commands",
            "--strict-mcp-config", "--setting-sources", "project", "--model", model, "--system-prompt", SYSTEM]
    out = json.loads(subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=600, cwd=cwd, env=_env()).stdout)
    m = re.search(r"\[[\s\S]*\]", out.get("result") or "")
    return [x for x in (json.loads(m.group(0)) if m else []) if isinstance(x, str) and 8 <= len(x) <= 1500]


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--per-category", type=int, default=40); ap.add_argument("--model", default="claude-sonnet-5")
    a = ap.parse_args()
    out = DATA / "requests.jsonl"
    rows = read_jsonl(out)
    real = sum(1 for r in rows if not r["source"].startswith("synthetic"))
    synth = sum(1 for r in rows if r["source"].startswith("synthetic"))
    cap = int(real * SYNTHETIC_SHARE / (1 - SYNTHETIC_SHARE)) - synth
    seen = {r["request"] for r in rows}
    added = 0
    with out.open("a") as f:
        for cat, what in CATEGORIES.items():
            for text in generate(cat, what, a.per_category, a.model):
                if text in seen or added >= cap:
                    continue
                rid = "syn." + cat + "." + hashlib.sha1(text.encode()).hexdigest()[:12]
                f.write(json.dumps({"id": rid, "source": "synthetic-coverage:" + cat, "unit_kind": "interactive", "request": text, "split": split_of(rid)}, ensure_ascii=False) + "\n")
                seen.add(text); added += 1
            print(f"{cat}: total added so far {added} (cap {cap})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
