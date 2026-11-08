# Compaction: one long-living session across Claude Code and Codex

Goal: an orchestrating session that lives for days, moving between Claude Code and Codex, without the lossy,
uncontrolled auto-compaction of either client. Three phases share the record shapes in
`cortados_router/compaction/events.py` (ToolEvent, Message, Session, KeepDecision).

## Phase 0: checkpoint and restart (deterministic, no model calls)

- **Parse** (`compaction/transcript.py`): `read_claude_code` / `read_codex` (and `parse_*`, which also return stats)
  turn a Claude Code transcript or a Codex rollout into a `Session`. Tool calls are paired with results by id; a call
  without a result is `interrupted`. Sidechain, meta, injected-context and unknown lines are counted in
  `stats["skipped"]` by kind, never silently dropped.
- **Policy** (`tables/compaction.json`, loaded by `compaction/policy.py`): values from the research report
  [COMPACTION-THRESHOLDS.md](COMPACTION-THRESHOLDS.md) (status `researched-2026-11-08`); every value has a `why` citing
  its section / source id; unknown or missing keys fail loudly. Thresholds are min(% of W, absolute cap): warn
  min(55%, 150k), checkpoint min(70%, 200k), force min(85%, 300k) — a 200k window binds on the percentages, a 1M
  window on the caps — and force <= price cliff - 10k for API-billed models listed in `price_cliff_tokens`
  (subscription sessions have none). Velocity: checkpoint if current + 2 x mean growth of the last 3 turns >= force,
  or one turn adds >= 10% of W. At >= warn, checkpoint at the turn boundary, and before resuming after an idle gap
  longer than the cache TTL. Self-handoff deadline 4 min after the current tool ends, forced at 6 min. No time-based
  timer. Circuit breaker: 2 forced restarts in 30 min, or a fresh session opening above 50% of the checkpoint
  threshold -> stop and escalate. The `pruning` and `provider_tool_result_cap_tokens` values are for phase 1, which
  reads them from this file.
- **Handoff** (`compaction/handoff.py`): `build_handoff(session, policy) -> (markdown, manifest)`. Sections in order,
  each under its own cap (16k total; tokens estimated as ceil(chars / chars_per_token): 2.5 for Claude Code, 4 for
  Codex, 2.5 when unknown): Goal (latest user prompt verbatim, 400), Recency pin (the 3 earlier recent user turns
  verbatim + the last 2 tool calls raw, 4000), Standing rules (rule-word sentences from user messages, deduped,
  newest wins, 1500), Decisions (1500), In-flight (interrupted calls flagged VERIFY; branches, PRs, hashes, task ids,
  worktrees from the last 8 messages, 2000), Files touched (<= 40 paths with edit counts and tools, 800), Ranked
  excerpts (+3 per shared goal word, +2 per load-bearing signal, recency bonus; greedy fill, then chronological; tool
  results head + tail with their char count, 6000). Over the total: trim excerpts, files, decisions, then recency
  pin; Goal and rules are never trimmed — the build fails instead. The manifest lists every message / event index
  per section with scores. Same input -> same bytes.
- **CLI** (`compaction/checkpoint.py`): `build` writes `handoff.md` + `handoff.manifest.json` (0600, no overwrite
  without `--force`); `check` reads a Claude Code status-line JSON (plus optional turn history, model id, idle time
  and cache-TTL class, fresh-session flag) and prints ok / warn / checkpoint / force / escalate.
- **Integration** (`integrations/checkpoint/`, templates only): status line + PreCompact hook, a `/checkpoint` skill,
  a Codex AGENTS.md rule, and the canary: the restarted session's first reply quotes the `handoff-id:` line.

## Phase 1: provider shadow pruning and labels

The `cortados` provider keeps its own content store of each conversation and decides, per past tool call, whether
the call and its result stay in the context (`KeepDecision`, rule-based first). Decisions are logged in shadow — the
request sent upstream is unchanged — and labels are harvested from what later turns actually re-used (a result
quoted, a file re-read, a call repeated). Owned by `compaction/store.py`, `compaction/prune.py`,
`tables/compaction-prune.json`.

## Phase 2: S1 keep heads

A separate S1 adapter, trained on the phase 1 labels and the phase 0 manifests, answers `keep_call` / `keep_result`
for a past tool call with probabilities, replacing the rules once it beats them in shadow. It imports the phase 0
parser. Owned by `compaction/labels.py` and `s1/compaction_*`.
