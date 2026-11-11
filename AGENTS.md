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
  `transport.py`, `router_db.py` (router.db reader opener — `mode=rw` + `query_only`, never `mode=ro` in a process that
  also writes it, #99 — and the route-log error counter), `local_resources.py`, `local_gate.py` (token-checked reverse proxy run on a local Ollama node: the
  provider's local lane reaches Ollama only through it), `subagent_mcp.py` (stdio MCP server: a `route_task` tool that delegates a
  self-contained subtask to the `cortados` provider endpoint and returns the routed model's text; stdlib, runs standalone),
  `child_tasks.py` (whole tasks as headless Claude Code children; `--run` runner, `--remote` host side, `--cleanup-archive`;
  the runner appends each finished run, redacted, to the append-only `$CORTADOS_HOME/exports/task-runs.jsonl` for
  Grounds (#91, README "Task run export"); a task dir whose export failed is never pruned),
  `exec_hosts.py` (which machine a child runs on: `~/.config/cortados/exec-hosts.json`, ssh transport), `workspaces.py`
  (per-task repo worktrees / folders under `<root>/active`, archive-or-held settling, index.jsonl, archive-only cleanup).
- `cortados_router/provider/` — the `cortados` model endpoint (design and status: `docs/PROVIDER-PLAN.md`).
  `server.py` HTTP endpoint, `Registry` of conversations and the `Wire` seam; `anthropic_wire.py` (Claude Code,
  `/v1/messages`) and `responses_wire.py` (Codex, `/v1/responses`) turn requests into one `ClientTurn`;
  `routing.py` picks the lane through the router's execution policy; `bridge.py` drives a headless Claude CLI with
  `mcp_shim.py` serving the client's tools; `codex_bridge.py` drives `codex app-server` with the client's tools as
  dynamic tools, under the isolation policy of `codex_isolation.py` (command line, config and login checks, aliases).
  `office.py` is the Office view (`GET /v1/cortados/office`, metadata-only floors per routing session, fed by the
  `x-cortados-session/-project/-device/-task-title` headers, child-tasks.jsonl and `POST /v1/cortados/office/heartbeat`);
  `clients.py` holds the scoped per-client tokens (`clients.json`, hashes + named scopes: `office:read`, `live:read`,
  `live:heartbeat`, a heartbeat client bound to one `host`; the owner token is checked first and unchanged). `live.py` builds `GET /v1/cortados/live` (one
  metadata-only snapshot of running child tasks, routed conversations, routed calls, subscriptions, S1 and hosts, each
  section with its own `as_of`/`status`/`errors`, composed from the office, tasks and usage views) and validates
  `POST /v1/cortados/live/heartbeat` (host job states, e.g. the Studio's S1 jobs); the dashboard's Live tab and the
  cortados-pulse pane read it. Its host-side sender is `cortados_router/live_heartbeat.py` (launchd jobs, hardware, the Studio's S1
  flags and /health; installed per host by `scripts/install_live_heartbeat.sh`, README "Live view"). `usage.py` builds `GET /v1/cortados/usage` and `tasks.py` builds
  `GET /v1/cortados/tasks` (the latest 200 `routes` rows as metadata: model, band, S1 reason by category, elapsed, % routed
  per session; never the request text; tokens are not recorded per route), both behind the owner token and both read by
  `dashboard.html` (`GET /dashboard`).
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

## Project todos
- The "Live status" section at the top of issue #60 on 4list233/CortadOS-Router is the one todo list. Every item has
  an issue; pause notes and memory link to #60 and hold no lists of their own (reason: separate lists drifted from
  the tracker for five days in November 2026).
- The top-level orchestrator is the only writer of #60. It updates the section when a branch merges, a review lands,
  a decision is made or a session pauses. Delegates and child tasks report back; they do not edit it.
- The drift check runs at session start and before any merge:
  `python3 -B scripts/tracker_check.py`
  It is read-only (`gh issue list` / `gh issue view` and local git refs) and reports open issues whose branch is
  already merged, unmerged local branches that no open issue mentions, and the age of the Live status date (warning
  past 2 days). Exit 0 clean, 1 drift, 2 the check could not run; a failed check is never a clean result. Tests:
  `tests/test_tracker_check.py` (fakes only, no GitHub call).

## Delegating subtasks through the router (route_task)

This repo ships a project MCP server, `cortados-route` (`.mcp.json` launches `cortados_router/subagent_mcp.py`), that
exposes one tool, `route_task`. It hands a single self-contained subtask to the `cortados` provider, which scores it with
S1 and runs it on the cheapest capable model in the pool, then returns the answer as text. The main session keeps its own
model and login; only the delegated subtask is routed.

Prefer `route_task` over a full-model subagent when you want a RESULT for a self-contained piece of work — an analysis, a
draft, a focused reasoning or coding question — and do not need tools run on this machine. Put everything the delegate
needs into the task: it has no access to this conversation, the repo, or your files. For work that must read or edit the
repo, use an ordinary subagent, not `route_task`.

It needs `CORTADOS_PROVIDER_URL` and `CORTADOS_PROVIDER_TOKEN` in the environment and the provider reachable. If the tool
is unconfigured or the provider is unreachable it returns a clear error; fall back to an ordinary subagent then.

## Delegating whole tasks from the Claude app
The same server serves `start_task` / `task_status` / `wait_task` / `list_tasks` / `cancel_task`
(`cortados_router/child_tasks.py`): each task runs as a separate headless Claude Code session on the configured
EXECUTION HOST (`cortados_router/exec_hosts.py`; `~/.config/cortados/exec-hosts.json`, default the Air; a remote host
is reached by `ssh` running this module's `--remote` mode there; never a silent fallback to the requesting machine)
through the provider (every child spends subscription quota). Workspaces (`cortados_router/workspaces.py`): `repo`
[+ `branch`] gives a git worktree under `<root>/active/<task_id>`, settled to `archive/` (pushed; HEAD detached so
the branch is free for the next task) or `held/` (unpushed, or a plain folder containing a nested `.git`; keeps its
branch, so a new task on it is refused naming the held path) when the task ends; only `archive/` is ever cleaned up (3 days),
by the daily cleanup job rather than task startup. `chrome: true` on a `gui` host launches
through `launchctl bootstrap gui/<uid>` and chrome children queue on one lock per host. A routed task with
`connectors` on a `gui` host takes the same LaunchAgent path (MCP OAuth tokens live in the login Keychain) but stays
routed, without the chrome lock; its plist holds no env (the runner loads a 0600 `session-runner-env.json` plus the
settings file). Without connectors, or on a non-gui host (with a warning), it is a plain detached spawn. Child traffic carries
`x-cortados-run-id: <task_id>`; lifecycle events go to `$CORTADOS_HOME/child-tasks.jsonl` (tests must use a throwaway
`CORTADOS_HOME`). Registration snippets (desktop app and `claude mcp add -s user`) are in README.md under the same
heading. Tests use a fake claude binary, a fake transport in place of ssh and a fake launchctl
(`tests/test_child_tasks.py`, `tests/test_exec_hosts.py`, `tests/test_workspaces.py`); never launch a real child,
open ssh or call the real launchctl in tests.

Being told when a child ends (#87): `start_task` returns `watch_command`, a blocking CLI for this machine
(`python3 -B <repo>/cortados_router/child_tasks.py --watch <task_id> [...] [--timeout S] [--interval S] [--stall S]`).
**After every start_task the orchestrator runs `watch_command` in the background** (a Claude Code session is woken when a
background command exits; `wait_task` is capped at 50 s by the MCP client timeout, and hand-written `ssh air pgrep` loops
cannot tell success from failure). It polls `task_status`'s own path (remote tasks over ssh, never a local guess), prints
one JSON line of metadata per task as it ends and exits 0 all succeeded / 1 any failed, cancelled or stalled (no change
in state, phase, workspace or child output for 30 min; a report, not a cancel) / 2 could not watch (bad id, host never
reachable in 3 tries) / 3 `--timeout`. Tests: `tests/test_task_watch.py` (same fakes; the fixtures are
`tests/test_child_tasks.py`'s `ChildTaskBase`).
