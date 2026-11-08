# Checkpoint integration templates (not installed anywhere)

Templates that wire the phase 0 checkpoint (`docs/COMPACTION.md`) into Claude Code and Codex. Nothing here is
installed by the repo; copy by hand after review. `$REPO` is this repository's path.

| File | What it is |
|---|---|
| `claude-settings.snippet.json` | `statusLine` command that saves the status JSON to `$CORTADOS_HOME/checkpoint/<session>.status.json`, keeps a `<session>.history` of used tokens for the velocity rule, and prints the level; `PreCompact` hook that runs `checkpoint build` on the session's `transcript_path` before Claude Code's own compaction. |
| `statusline.sh` | The status-line command the snippet points at. |
| `checkpoint-skill.md` | `/checkpoint` slash command / skill: how the orchestrator writes its own handoff and restarts. |
| `codex-agents.snippet.md` | The same rule for Codex's AGENTS.md. |

## Flow
Values: `tables/compaction.json`, sources in `docs/COMPACTION-THRESHOLDS.md`.

1. Every status-line refresh: `checkpoint check` prints `ok` / `warn` / `checkpoint` / `force` / `escalate`.
   Optional env for the template: `CORTADOS_CACHE_TTL_CLASS` (claude-api / claude-subscription / openai) and
   `CORTADOS_ROUTED_MODEL` (an API-billed model id with a price cliff).
2. `warn`: checkpoint at the end of the current turn, not mid-tool-chain. `checkpoint`: at the next idle turn
   boundary (no tool call in flight), run `/checkpoint`. `force`: run it now. `escalate`: stop, tell the owner.
3. The session writes its handoff within 4 minutes after the current tool call ends; at 6 minutes the deterministic
   one from `checkpoint build` is used.
4. A fresh session starts with the handoff as its first prompt.
5. Circuit breaker: 2 forced restarts in 30 minutes, or a fresh session that opens above 50% of the checkpoint
   threshold -> stop and escalate; no automatic restart. No time-based timer.

## Post-restart canary
The new session's FIRST reply must quote the handoff's `handoff-id: <id>` line verbatim. If it does not, the handoff
did not arrive (wrong file, truncated prompt, wrong session): stop and re-send it; do not let the session continue
on an empty context.
