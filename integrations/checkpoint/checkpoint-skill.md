---
name: checkpoint
description: Write a handoff document for this session and restart fresh from it. Use when the status line says checkpoint or force, or when the owner asks for a checkpoint.
---

# Checkpoint: hand off and restart

Do this at an idle turn boundary: no tool call in flight, no subagent running. If the status says `force`, do it now.
You have 5 minutes (`self_handoff_deadline_s`); after that the deterministic handoff from `checkpoint build` is used.

1. Run `python3 -B -m cortados_router.compaction.checkpoint build --transcript <this session's transcript>
   --out $CORTADOS_HOME/checkpoint/<session>-<time>` for the deterministic baseline and its `handoff-id`.
2. Write `handoff.self.md` next to it, under ~10k tokens (estimate: chars / 4), with these sections in order:
   - **Goal**: the owner's last 3 prompts, verbatim.
   - **Standing rules**: every never / always / must / don't / only / every rule the owner gave, verbatim, newest
     wins on conflict. Never drop one.
   - **Decisions**: what was decided, and why, with commit / PR references.
   - **In-flight**: unfinished tool calls (mark "verify"), branches, PR numbers, commit hashes, task / agent ids,
     worktree paths. Anything not verified after the restart must be re-checked, not assumed.
   - **Files touched**: paths with edit counts.
   - **Excerpts**: the few passages the next session cannot work without (errors, exact commands, numbers).
   Keep the baseline's `handoff-id:` line as the first line after the title.
3. Restart: start a fresh session whose first prompt is the handoff file's contents.
4. Canary: the new session's first reply must quote the `handoff-id:` line. If it does not, re-send the handoff.
5. Circuit breaker: if this is the 3rd restart in 20 minutes, do not restart; pause 30 minutes and tell the owner.
