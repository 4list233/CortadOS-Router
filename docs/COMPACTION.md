# Compaction: one long-living session across Claude Code and Codex

Goal: an orchestrating session that lives for days, moving between Claude Code and Codex, without the lossy,
uncontrolled auto-compaction of either client. Three phases share the record shapes in
`cortados_router/compaction/events.py` (ToolEvent, Message, Session, KeepDecision).

## Phase 0: checkpoint and restart (deterministic, no model calls)

- **Parse** (`compaction/transcript.py`): `read_claude_code` / `read_codex` (and `parse_*`, which also return stats)
  turn a Claude Code transcript or a Codex rollout into a `Session`. Tool calls are paired with results by id; a call
  without a result is `interrupted`. Sidechain, meta, injected-context and unknown lines are counted in
  `stats["skipped"]` by kind, never silently dropped.
- **Policy** (`tables/compaction.json`, loaded by `compaction/policy.py`): every value has a `why`; unknown or missing
  keys fail loudly. Status `provisional`: warn 60%, checkpoint 70%, force 85%; absolute ceilings 140k (200k-class
  window) and 300k (1M-class, where quality, not capacity, is the limit); velocity rule (last turn's growth crossing
  force within 2 turns); idle turn boundary only; 5-minute self-handoff deadline; 10k-token handoff with per-section
  budgets; circuit breaker 3 restarts / 20 min then 30 min pause.
- **Handoff** (`compaction/handoff.py`): `build_handoff(session, policy) -> (markdown, manifest)`. Sections in order,
  each under its own budget (tokens estimated as ceil(chars/4)): Goal (last 3 user prompts verbatim), Standing rules
  (rule-word sentences from user messages, deduped, newest wins), Decisions, In-flight (interrupted calls flagged
  VERIFY; branches, PRs, hashes, task ids, worktrees from the last K messages), Files touched (Edit/Write/MultiEdit/
  NotebookEdit and apply_patch paths with counts), Ranked excerpts (+3 per shared goal word, +2 per load-bearing
  signal, recency bonus for the last K messages; greedy fill, then chronological; tool results truncated). Over the
  total budget: trim excerpts, then files, then decisions; Goal and rules are never trimmed — the build fails instead.
  The manifest lists every message / event index per section with scores. Same input -> same bytes.
- **CLI** (`compaction/checkpoint.py`): `build` writes `handoff.md` + `handoff.manifest.json` (0600, no overwrite
  without `--force`); `check` reads a Claude Code status-line JSON and prints ok / warn / checkpoint / force.
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
