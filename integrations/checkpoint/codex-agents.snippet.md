## Checkpoint rule (CortadOS compaction)

Values: `tables/compaction.json` (sources: `docs/COMPACTION-THRESHOLDS.md`). W = context window.

- Warn at min(55% W, 150k): checkpoint at the end of the current turn, and before resuming after an idle gap longer
  than the prompt-cache TTL (30 min on OpenAI).
- Checkpoint at min(70% W, 200k), or when current + 2 x mean growth of the last 3 turns would reach force, or one
  turn added >= 10% of W: at the next idle turn boundary, write a handoff and restart.
- Force at min(85% W, 300k) (and <= 262k when billed through the API): do it immediately.
- Baseline: `python3 -B -m cortados_router.compaction.checkpoint build --transcript <rollout .jsonl> --format codex
  --out $CORTADOS_HOME/checkpoint/<session>-<time>`.
- Your own handoff (4 min after the current tool call ends, forced at 6; <= 16k tokens): Goal (latest prompt
  verbatim), Recency pin (3 earlier user turns verbatim + last 2 tool calls raw), Standing rules (verbatim, newest
  wins), Decisions, In-flight (interrupted calls marked verify; branches, PRs, hashes, task ids, worktrees), Files
  touched (<= 40), Excerpts. Keep the baseline's `handoff-id:` line.
- The restarted session's first reply quotes the `handoff-id:` line (canary).
- 2 forced restarts in 30 minutes, or a fresh session opening above 50% of the checkpoint threshold: stop and tell
  the owner. No time-based timer.
