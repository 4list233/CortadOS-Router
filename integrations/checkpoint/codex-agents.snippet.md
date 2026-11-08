## Checkpoint rule (CortadOS compaction)

When the context passes 70% of the window (or 140k tokens on a 200k-class window, 300k on a 1M-class one), or the
last turn's growth would cross 85% within 2 turns: at the next idle turn boundary, write a handoff and restart.
Above 85%, do it immediately.

- Baseline: `python3 -B -m cortados_router.compaction.checkpoint build --transcript <rollout .jsonl> --format codex
  --out $CORTADOS_HOME/checkpoint/<session>-<time>`.
- Your own handoff (5-minute deadline, ~10k tokens): Goal (last 3 user prompts verbatim), Standing rules (verbatim,
  newest wins), Decisions, In-flight (interrupted calls marked verify; branches, PRs, hashes, task ids, worktrees),
  Files touched, Excerpts. Keep the baseline's `handoff-id:` line.
- The restarted session's first reply quotes the `handoff-id:` line (canary).
- 3 restarts in 20 minutes: stop, pause 30 minutes, tell the owner.
