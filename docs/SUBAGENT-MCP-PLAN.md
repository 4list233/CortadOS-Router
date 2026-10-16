# Router as a subagent MCP: plan

Status: **plan only** (Forest, 2026-10-15). Copied from CortadOS `ROUTER-DESIGN.md` §11 so that work can start
in this repo. Section numbers are kept so the two documents stay comparable.


**Goal.** First standalone use of CortadOS Router S1 outside Hermes: an MCP server that Claude Code, Claude Cowork,
Codex or Cursor can load. The caller hands it a task. The router scores the task, picks a model on one of Forest's
subscriptions, runs it as a subagent through that vendor's own CLI, and returns the result. Forest can then use and
analyse his own ranking model from any client.

**Forest's decisions (2026-10-15):**
- Home: the public repo, `integrations/mcp/` in 4list233/CortadOS-Router (GPL-3.0), installable on its own.
- Permissions: subagents may **edit only inside the folder the caller passes**, with no network or outside writes
  unless the call asks for them.
- Run mode: **start + poll**, so long runs survive client MCP timeouts and several subagents can run in parallel.
- Lanes in v1: **Codex, Cursor, Claude and local (Ollama)**. Antigravity stays off; its ceiling is 0 by containment.

### 11.1 Tools (stdio MCP)

| Tool | Does |
|---|---|
| `route(task, cwd?, constraints?)` | Scores and resolves only. Returns band, D, use-case, the chosen `{lane, provider, model}`, the skipped routes with reasons, the suggested sub-agent count and `route_id`. No execution. |
| `delegate(task, cwd, constraints?, route_override?)` | Same routing, then spawns the subagent. Returns `job_id` and the chosen route at once. |
| `job_status(job_id)` / `job_result(job_id)` / `job_cancel(job_id)` | Progress (state, elapsed, last lines), the final answer plus files changed and usage, and cancellation (kills the process group). |
| `escalate(job_id or route_id, scope)` | The existing `router_escalate` semantics: raises the difficulty floor for follow-ups. |
| `feedback(route_id, verdict, note?)` | Optional rating (good / wrong model / too weak / too strong). Logged as training signal for the nightly retrain. |

`constraints` lets the caller narrow lanes (`allowed_lanes`), set a minimum band, or exclude the caller's own
subscription, e.g. "don't route back to Claude".

### 11.2 Lane runners (one owner each, in the MCP package)

| Lane | Command (flags verified on this Mac 10-15 unless marked) | Edit-in-folder default |
|---|---|---|
| Codex | `codex exec -m <model> -C <cwd> -s workspace-write --json -o <last-message file>` | `-s workspace-write`. Network is off by default in that sandbox. |
| Claude | `claude -p --model <model> --output-format stream-json --permission-mode acceptEdits` (cwd = folder; `--add-dir` only if asked) | edits limited to cwd; the Bash/network tool allowlist is passed explicitly |
| Cursor | `cursor-agent -p --model <model> --output-format json` (**flags not yet verified**: `--help` printed nothing here) | to be confirmed. This is the one lane whose sandboxing is unknown. |
| Local | Ollama HTTP `/api/chat` | answers only, no tools. Fresh-context work only (`local_fresh_only_history_tokens`). |

Each runner uses that CLI's own login, never the gateway's owned grant. Usage is reported back from each CLI's
JSON output.

### 11.3 What must be finished in the Router itself first ("finalize the router")

1. **A host-agnostic entrypoint.** `execution_routing.select` is Hermes-shaped (face, session, credential
   resolver). Add one public function, `route_task(task, *, cwd, constraints, context)`, wrapping `core.decide` and
   `core.resolve`, which both the MCP and Hermes call (ENG-03: one owner, no second router).
2. **Live scarcity must be correct.** Since 10-13 the Codex `/status` parser reads every sample as a new pool, and
   the weekly reset date as under 24 h away (P1/P1b, found in the quota-producer D review `9fbb05d6`). The router is
   blind to Codex weekly exhaustion. **This must be fixed before the MCP routes Codex sensibly.** It is the same
   fix the Donna quota producer needs.
3. **A current model inventory.** The routing table still names retired models (e.g. `claude-opus-5`,
   `claude-haiku-4-5`) and has **no local-lane candidates**. Refresh the inventory and tiers (`bin/refresh-*.sh`),
   add the Ollama models (including the abliterated ones) with their fresh-context limits, and map each table model
   to the exact CLI `--model` id per lane.
4. **Task context without Hermes.** S1 scores better with context (§2). In the MCP the caller passes `cwd` and an
   optional context string. Grounds retrieval is an optional later step (D-0043 adapter), not v1.
5. **Route logging and training signal.** MCP routes go to the same route log as Hermes, with
   `platform: "mcp:<client>"`, so the nightly S1 retrain sees them. `feedback` becomes a label source.
6. **Multi-account (§10)** is designed but not built. v1 uses one login per vendor. The runner interface takes an
   `account_ref` so §10 slots in without a redesign.

### 11.4 Not in v1

- No automatic decomposition into parallel subagents (rollout R-3 / the Harness decomposition runtime). `route`
  reports the suggested count; the caller decides.
- No Grounds retrieval.
- No Antigravity.
- No Donna gateway path. The MCP drives CLIs directly; the Donna gateway stays the Hermes-fleet transport.

### 11.5 Work packages (order)

1. P1/P1b parser fix (shared with the Donna quota producer; live-fleet approval needed).
2. `route_task` entrypoint plus a refreshed inventory and CLI model-id map.
3. MCP server with `route` only: read-only and safe, so Forest can start analysing routing decisions from Claude
   Code and Cowork immediately.
4. A job runner (start/poll/cancel, process groups, timeouts, a jobs directory under the Router state home) plus the
   Codex and Claude runners.
5. Cursor runner, once its non-interactive and sandbox flags are verified, plus the local runner.
6. Feedback and route-log labels into the nightly retrain.

Each package gets an exam that pins the real CLI output shapes. Per QA D-0092 that means one real captured run per
lane and output kind, plus the edit-only-in-folder guarantee: a subagent writing outside cwd must fail the case.

### 11.6 Open questions

- Whether vendor terms allow driving personal subscriptions from one automated router (same as §10(c)).
- When Cowork is the caller: Cowork runs in its own sandbox. Confirm it can reach a local stdio MCP that spawns CLIs
  on the Mac, or whether it needs the MCP over a local HTTP transport.

### 11.7 Package 1 in detail: the Codex `/status` parser fix (PKG-A)

Found on 2026-10-15 while designing the Donna gateway's quota producer. Full analysis lives in the CortadOS build
tree, `docs/harness/deepseek-prototype/router-l1/r0/QUOTA-PRODUCER-DESIGN.md` rev 2 §1, §3.3; D review
`QUOTA-PRODUCER-D-REVIEW.md`. The essentials:

- **Root cause.** On 2026-10-13 18:00, `/opt/homebrew/bin/codex` became npm `@openai/codex` **0.162.1**, a manual
  install. Its `/status` screen layout differs from the app-bundled 0.154 the parser was written against.
- **P1, pool label.** `parse_codex_status` (`cortados_router/scarcity.py` ≈ l.325–348) accepts up to 3 free tokens
  before "Weekly limit". On 0.162.1 it captures the probe's own session UUID, so every sample becomes a new pool
  `week (01a1…)`: 88 distinct pools since 10-13 18:39, against 1,171 samples of `week` before that.
- **P1b, weekly reset date.** The reset is always read as under 24 h away, so `elapsed_pct` cycles 85.7 → 99.9
  every day.
- **Live effect.** The codex lane has `scarcity: null`, so the router can't see Codex weekly exhaustion, ranks Codex
  as neutral, and burn-down and unit-cost learning are blind to it. Fixing P1 alone would expose P1b and cause fake
  daily "98 % expiring" burn-downs, so **fix both together**.
- **Fix shape.**
  - Split the Codex screen parser into a new `cortados_router/scarcity_codex.py`, which stays the only parser.
  - Read labels from the screen's line structure.
  - Unknown labels, leftover reset text or out-of-range weekly resets fail loudly; never guess.
  - Collapse repainted duplicate lines (`week (for shortcuts)`).
  - Pin the Codex CLI version, and treat an unexpected version as a failed sample (stale, never fresh).
- **Evidence.** One real redacted `/status` capture per pinned CLI version becomes the regression fixtures QP-01..05.
- **Done when** the live codex lane shows `all:week`, a reset date that matches the screen, and a non-null
  `scarcity`.
- **Live sync.** The CortadOS fleet loads a copy at `plugins/cortados-router/scarcity.py`, which has uncommitted
  Router fixes from 10-13 (`probe_reaper`, the `_codex_binary` fallback, `os._exit` on exec failure). Syncing the
  fix there needs Forest's approval and must keep those changes.
- **Review notes (D re-review 2026-10-15, `7243a55e`), to address before building:**
  - (N1) A correctly dated weekly reset on the last day of the week must be accepted. Define behaviour for a
    missing reset text and for a 5-hour window.
  - (N2) `tests/test_router.py` fixtures use the old format with a fixed past date and need real-screen
    replacements, never weakened.
  - (N2) `tests/test_router_usage_capture.py` patches `scarcity.tui_capture`, so keep the re-exports and look
    it up at call time.
  - (N3) Port to the live copy hunk by hunk, never by file copy. The live copy differs in exactly three places:
    the `context.env_compat` import, the `_plans` budgets path, and a comment.
  - (N3) Take a byte backup before the port, and check afterwards that all three deltas survive.
