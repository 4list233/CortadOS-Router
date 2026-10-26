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
  `scarcity.py` quota reading via vendor CLIs (`scarcity_codex.py` parses Codex's `/status`), `unitcost.py`,
  `transport.py`, `local_resources.py`, `subagent_mcp.py` (stdio MCP server: a `route_task` tool that delegates a
  self-contained subtask to the `cortados` provider endpoint and returns the routed model's text; stdlib, runs standalone).
- `cortados_router/provider/` — the `cortados` model endpoint (design and status: `docs/PROVIDER-PLAN.md`).
  `server.py` HTTP endpoint, `Registry` of conversations and the `Wire` seam; `anthropic_wire.py` (Claude Code,
  `/v1/messages`) and `responses_wire.py` (Codex, `/v1/responses`) turn requests into one `ClientTurn`;
  `routing.py` picks the lane through the router's execution policy; `bridge.py` drives a headless Claude CLI with
  `mcp_shim.py` serving the client's tools; `codex_bridge.py` drives `codex app-server` with the client's tools as
  dynamic tools, under the isolation policy of `codex_isolation.py` (command line, config and login checks, aliases).
- `tables/` — routing table, budgets, generated model scores and tiers (`*.generated.json` are built
  nightly by `tables/benchmarks/build_tiers.py`; do not hand-edit them).
- `s1/` — S1 scorer: dataset, labelling, SFT, evaluation, serving, nightly job.
- `integrations/hermes/` — Hermes plugin and patches.
- `tests/` — stdlib unittest suite; `tests/fixtures/` pins the benchmark catalog;
  `tests/integration/` needs pytest + an importable Hermes and is skipped otherwise.

## Commands
- Full suite (run before calling any change done):
  `python3 -B -m unittest discover -s tests -t .`
  Baseline 2026-10-21 on the M5 Max, system Python 3.9, branch p2-codex: 334 run, OK, 7 skipped
  (Hermes integration). The README's "155 / 1 failing" is stale.
- Provider tests alone (fakes only; no vendor CLI is opened):
  `python3 -B -m unittest tests.test_provider tests.test_provider_codex tests.test_provider_responses tests.test_provider_routing tests.test_responses_wire`
- Table check: `python3 -B cortados_router/core.py table` → `table v <N> ok`
- Provider endpoint with both backends (every conversation spends subscription quota: start it only when asked):
  `CORTADOS_PROVIDER_TOKEN=<token> python3 -B -m cortados_router.provider.server [--port 8432] [--claude-bin claude] [--codex-bin codex] [--codex-home <dir>] [--fixed-model <claude model>]`
  Env equivalents: `CORTADOS_PROVIDER_CLAUDE_BIN`, `CORTADOS_PROVIDER_CODEX_BIN` (not scarcity's `CORTADOS_CODEX_BIN`,
  which is the ChatGPT.app copy for the `/status` probe), `CORTADOS_PROVIDER_CODEX_HOME` (a dedicated, logged-in
  `CODEX_HOME` for the Codex backend; unset, the backend runs on `~/.codex`, whose AGENTS.md and skills reach the
  thread), `CORTADOS_PROVIDER_FIXED_MODEL` (no routing). Clients: Claude Code on `/v1/messages`
  (`ANTHROPIC_BASE_URL=http://127.0.0.1:8432 ANTHROPIC_AUTH_TOKEN=<token> claude --model cortados`), Codex on
  `/v1/responses` with the provider config in `docs/PROVIDER-PLAN.md` (P2 status); this repo never writes `~/.codex`.
- Real decision (shadow only, changes nothing): always set a throwaway home and turn the usage refresh off —
  `CORTADOS_HOME=$(mktemp -d) CORTADOS_SCARCITY_REFRESH=off python3 -B cortados_router/core.py decide "<request>"`
  The throwaway home protects the live state; the switch protects the subscriptions, because a fresh home is
  always "due" for a usage sample and that sample opens the vendor CLIs.

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
