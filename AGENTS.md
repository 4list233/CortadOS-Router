# AGENTS.md — CortadOS Router

Read by Codex and Cursor directly, and by Claude Code through `CLAUDE.md`. Global rules live in
`~/.config/ai-coding/GLOBAL-AGENTS.md`; the engineering standard is `~/code/Ai-Coding-std-workflow`.
This file adds what is specific to this repository and wins where they differ.

## What this is
The CortadOS model router: for each LLM call, S1 (a small local scorer on `:8431`) answers 12
questions, hard rules + `tables/routing-table.json` turn them into a band and required index, and the
cheapest capable subscription route wins. `README.md` is the design summary; `docs/ROUTER-DESIGN.md`
is the long form.

## Layout
- `cortados_router/` — router package. `core.py` resolve/decide (CLI: `table`, `decide`),
  `scarcity.py` quota reading via vendor CLIs, `unitcost.py`, `transport.py`, `local_resources.py`.
- `tables/` — routing table, budgets, generated model scores and tiers (`*.generated.json` are built
  nightly by `tables/benchmarks/build_tiers.py`; do not hand-edit them).
- `s1/` — S1 scorer: dataset, labelling, SFT, evaluation, serving, nightly job.
- `integrations/hermes/` — Hermes plugin and patches.
- `tests/` — stdlib unittest suite; `tests/fixtures/` pins the benchmark catalog;
  `tests/integration/` needs pytest + an importable Hermes and is skipped otherwise.

## Commands
- Full suite (run before calling any change done):
  `python3 -B -m unittest discover -s tests -t .`
  Baseline 2026-10-20 on the M5 Max, system Python 3.9, dirty tree: 172 run, OK, 7 skipped
  (Hermes integration). The README's "155 / 1 failing" is stale.
- Table check: `python3 -B cortados_router/core.py table` → `table v <N> ok`
- Real decision (shadow only, changes nothing): always set a throwaway home —
  `CORTADOS_HOME=$(mktemp -d) python3 -B cortados_router/core.py decide "<request>"`

## Rules specific to this repo
- Never run against the real `~/.cortados` state in tests or verification; use `CORTADOS_HOME=$(mktemp -d)`.
  Reason: `router.db`, `scarcity.json` and `unit-cost.json` there are the live fleet's learned state.
- `scarcity.py` with no arguments opens the claude / codex / cursor-agent / agy CLIs in a PTY. Do not
  run it unasked; it touches the user's real subscriptions.
- The hard rules in `core.resolve` (outward floors blast radius, degraded scorer never defaults to
  Claude) are invariants. Changing one is an `[ESCALATE]`, not a refactor.
- Stdlib only for the router package and standalone suite; adding a dependency is an `[ESCALATE]`.

## Verification
After the suite passes, follow `.claude/skills/verify-router/SKILL.md` and paste its evidence block
into the handback / PR. Codex and Cursor: read that file and follow it the same way.
