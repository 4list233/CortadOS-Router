# Checkpoint integration templates (not installed anywhere)

Templates that wire the phase 0 checkpoint (`docs/COMPACTION.md`) into Claude Code and Codex. Nothing here is
installed by the repo; copy by hand after review. `$REPO` is this repository's path.

| File | What it is |
|---|---|
| `claude-settings.snippet.json` | `statusLine` command that saves the status JSON to `$CORTADOS_HOME/checkpoint/<session>.status.json` (previous sample kept as `.prev.json` for the velocity rule) and prints the level; `PreCompact` hook that runs `checkpoint build` on the session's `transcript_path` before Claude Code's own compaction. |
| `statusline.sh` | The status-line command the snippet points at. |
| `checkpoint-skill.md` | `/checkpoint` slash command / skill: how the orchestrator writes its own handoff and restarts. |
| `codex-agents.snippet.md` | The same rule for Codex's AGENTS.md. |

## Flow
1. Every status-line refresh: `checkpoint check` prints `ok` / `warn` / `checkpoint` / `force`.
2. `warn`: finish the current step. `checkpoint`: at the next idle turn boundary (no tool call in flight), run
   `/checkpoint`. `force`: run it now.
3. The session writes its handoff (sections below) within `self_handoff_deadline_s` (5 min). If it does not, the
   deterministic one from `checkpoint build` is used.
4. A fresh session starts with the handoff as its first prompt.
5. Circuit breaker: 3 restarts in 20 minutes -> pause 30 minutes and tell the owner.

## Post-restart canary
The new session's FIRST reply must quote the handoff's `handoff-id: <id>` line verbatim. If it does not, the handoff
did not arrive (wrong file, truncated prompt, wrong session): stop and re-send it; do not let the session continue
on an empty context.
