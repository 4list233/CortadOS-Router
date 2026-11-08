---
name: checkpoint
description: Write a handoff document for this session and restart fresh from it. Use when the status line says checkpoint or force, or when the owner asks for a checkpoint.
---

# Checkpoint: hand off and restart

Do this at an idle turn boundary: no tool call in flight, no subagent running. If the status says `force`, do it now;
if it says `escalate`, do not restart: tell the owner. You have 4 minutes after the current tool call ends; at 6
minutes the deterministic handoff from `checkpoint build` is used instead.

1. Run `python3 -B -m cortados_router.compaction.checkpoint build --transcript <this session's transcript>
   --out $CORTADOS_HOME/checkpoint/<session>-<time>` for the deterministic baseline and its `handoff-id`.
2. Write `handoff.self.md` next to it, under 16k tokens (estimate: chars / 2.5 for Claude, chars / 4 for GPT), with
   these sections in order:
   - **Goal** (400): the owner's latest prompt, verbatim, plus the acceptance check.
   - **Recency pin** (4000): the 3 earlier recent user turns verbatim and the last 2 tool calls raw.
   - **Standing rules** (1500): every never / always / must / don't / only / every rule the owner gave, verbatim, newest
     wins on conflict. Never drop one.
   - **Decisions** (1500): what was decided, and why, with commit / PR references.
   - **In-flight** (2000): unfinished tool calls (mark "verify"), branches, PR numbers, commit hashes, task / agent ids,
     worktree paths. Anything not verified after the restart must be re-checked, not assumed.
   - **Files touched** (800): up to 40 paths, one line each.
   - **Excerpts** (6000): the few passages the next session cannot work without (errors, exact commands, numbers).
   Keep the baseline's `handoff-id:` line as the first line after the title.
3. If the status said `escalate`, stop here and tell the owner; after they decide, clear it with
   `python3 -B -m cortados_router.compaction.checkpoint ack --breaker-file $CORTADOS_HOME/checkpoint/breaker.json`.
   Record the restart, then restart: `python3 -B -m cortados_router.compaction.checkpoint record-restart
   --restart-log $CORTADOS_HOME/checkpoint/restarts.jsonl --handoff-id <id> [--forced]` (`--forced` when the status
   said `force`), then start a fresh session whose first prompt is: "Resume from the handoff at <ABSOLUTE path to handoff.md>. Read
   it and every `full text:` file it names (absolute paths) before acting. Quote its handoff-id line first." Use the
   absolute path `checkpoint build` printed: the new session's working directory may differ.
4. Canary: the new session's first reply must quote the `handoff-id:` line. If it does not, re-send the handoff.
5. Circuit breaker: if this would be the 2nd forced restart in 30 minutes, or the fresh session opens above 50% of
   the checkpoint threshold, do not restart: stop and tell the owner.
