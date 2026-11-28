# Steering a running child task (`steer_task`), and the sub-orchestrator cap

Owner decisions 2026-11-27. Branch `steer-task`. Code: `cortados_router/steer.py` (the note store and the child's hook,
standalone stdlib), `cortados_router/child_tasks.py` (`steer`, the remote `steer` op, the `steer` capability, the
runner's `steer_delivered` events, task_status counts, the cap), `cortados_router/session.py` (`--steer-dir`: the
hook entries in the child's settings file), `cortados_router/subagent_mcp.py` (the `steer_task` tool),
`cortados_router/provider/server.py` (the run-id tool-wait bound). Tests: `tests/test_steer_task.py`.

## Spec

**Outcome.** (A) A task started with `delegate: true` (a sub-orchestrator) may run 6 hours; every other child keeps
3 hours. (B) The owner's orchestrating session can send a short steering note to a RUNNING child task; the child sees it
at its next tool boundary, once. The Office page stays read-only: there is no browser or office write path.

**Acceptance check.**
`env -u CORTADOS_CHILD_TASK -u CORTADOS_WHOLE_SESSION -u CORTADOS_DELEGATE_BUDGET CORTADOS_HOME=$(mktemp -d)
CORTADOS_TASKS_DIR=$(mktemp -d) python3 -B -m unittest tests.test_steer_task tests.test_nested_tasks
tests.test_child_tasks tests.test_task_watch tests.test_exec_hosts tests.test_subagent_mcp tests.test_live
tests.test_office tests.test_office_tree tests.test_bot_gateway tests.test_session` OK, and
`python3 -B cortados_router/core.py table` prints `table v <N> ok`.

## A. Run-time caps

| task | cap | override |
|---|---|---|
| `delegate: true` (sub-orchestrator) | 6 h (`SUB_ORCHESTRATOR_MAX_S_DEFAULT`) | `CORTADOS_SUB_ORCHESTRATOR_MAX_S` |
| every other child (plain, worker, chrome) | 3 h (`CHILD_MAX_S_DEFAULT`, unchanged) | `CORTADOS_CHILD_MAX_S` |

The cap is chosen where the task starts (`_start_task` on the execution host: a remote start computes it there, from the
`delegate` the request carries) and recorded in `meta.json` / `cmd.json` as `max_s`; the runner enforces it as before.
The timeout error names the variable that applies. A worker keeps 3 h whatever its sub-orchestrator has.

The provider's `CHILD_RUN_MAX_S` (how long a conversation's `x-cortados-run-id` earns the long tool-wait idle limit,
counted from its first request) is the larger of the two caps, 6 h by default, so a sub-orchestrator's conversation is
not reaped at the ordinary idle limit after its third hour. The provider cannot tell a sub-orchestrator's run id from a
plain child's; a plain child's conversation therefore may keep the long limit up to 6 h too, bounded as before by the
child-task log (a run the log shows ended loses it at once). [ESCALATE] 1.

## B. `steer_task(task_id, message)`

### Who may call it (the same ancestry rule as start_task)

`caller_identity()` (the nearest child-task runner among the caller's process ancestors on this host, never an env var;
docs/NESTED-TASKS.md "How depth is enforced"):

- **depth 0** (the owner's main session, anything not under a runner): any task this machine knows (a local task dir,
  or a remote stub it holds).
- **a sub-orchestrator** (depth 1, `delegate`): only its OWN workers, the ids in `<its task dir>/children/`.
- **anyone else is refused** and counted as a `refused` event with `reason: steer_scope`: a worker (depth 2), a plain
  child (depth 1 without `delegate`), a sub-orchestrator naming a task that is not its worker, and a process that claims
  to be in a child without a runner above it (caller_identity's refusal).
- The bot gateway does not expose the tool (`bot_gateway.EXPOSED` is unchanged): steering is the owner's only.

A worker of a sub-orchestrator on ANOTHER host is not reachable from the main session by id (the main session holds no
stub for it, the same as task_status); the main session steers the sub-orchestrator, which relays. [ESCALATE] 3.

### What is refused

- An id that is not `t<14 digits>-<8 hex>`, or that this machine has no record of: "no such task".
- A task that is not running (succeeded, failed, cancelled, a tombstone, a remote `start_failed`): "has ended
  (<state>); a steering note reaches only a running task". A remote stub whose start is unconfirmed, or whose cancel is
  pending, is refused too ("its start is not confirmed" / "is being cancelled").
- A task started before this change (its `meta.json` has no `steer: true`, so its settings carry no steering hook): the
  note could never be delivered.
- The note: empty after trimming, longer than `MAX_NOTE_CHARS` (2000), or holding a non-printable character (anything
  `str.isprintable()` rejects except newline and tab).
- More than `MAX_PENDING` (10) notes waiting for the same task: the child has not reached a tool boundary since; more
  notes would pile up unread.

### Delivery

1. **Requester.** `steer()` appends `{"id": <32 hex>, "at": <epoch>, "text": <note>}` to `<task dir>/steer.jsonl` (0600,
   under an exclusive flock on `<task dir>/steer.lock`), logs `steer_sent` and answers `{task_id, steer_id, state:
   "queued", pending}`.
   For a REMOTE task (the requester holds a stub): the host must confirm the `steer` capability (`REMOTE_CAPABILITIES`,
   checked with the same cached handshake as a start, BEFORE anything is sent; a host without it is refused with
   "update this host's cortados-router checkout"), then one `steer` remote op (`{"task_id", "text"}`) runs `steer()` on
   the host, which appends to the host's task dir and logs there. The requester logs its own `steer_sent` with `host`.
   `steer` is also part of what every remote START requires (all hosts deploy from the same main, docs/NESTED-TASKS.md).
2. **The child.** Its settings file (written by `session.prepare` from `--steer-dir <task dir>`; for a chrome child by
   `_prepare_login`) holds a `PostToolUse` and a `PostToolUseFailure` hook, matcher `*`:
   `<absolute python3> -I -S -B <absolute steer.py> --hook <task dir>`. After every tool call of the child's MAIN loop
   the hook takes the pending notes, marks them delivered, and prints
   `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Steering note from the owner's
   orchestrator: <text>"}}` (one paragraph per note, oldest first). Claude Code adds `additionalContext` to the model's
   context with that tool result.
3. **Exactly once.** Under the same flock, the hook appends each delivered id to `<task dir>/steer-delivered.jsonl`
   (fsynced) BEFORE it writes its output, and never takes an id that file holds. A hook call that dies before marking
   leaves the note pending for the next boundary; one that dies between marking and writing loses that note (at most
   once, never twice). The runner logs `steer_delivered` for each newly marked id (it checks the file about once a
   second while the child runs, and once more after it exits).
4. **Bound per boundary.** One hook call delivers notes up to `MAX_CONTEXT_CHARS` (8000) in total; the rest wait for the
   next boundary. (Our bound: Claude Code documents no limit, and a large block of injected text would crowd the tool
   result it is attached to.)
5. **Subagents.** A hook call from a subagent's tool (Claude Code sets `agent_id` in the hook input only then) delivers
   nothing: the note is for the child's own main loop, which is the session the orchestrator talks to.

### Why this hook point

Investigated in this repo's setup (the child is `claude -p --output-format stream-json --verbose`, prompt on stdin from
`prompt.txt`; Claude Code 2.1.332 on the Pro):

- **PostToolUse `additionalContext` (chosen).** The CLI's hook-output schema for `PostToolUse` and
  `PostToolUseFailure` has `additionalContext` ("reaches the model"); the hook input carries `agent_id` only inside a
  subagent ("use this field to distinguish subagent calls from main-thread calls"). The child already gets its hooks
  from the settings file the launcher writes (the boundary hook is a PreToolUse entry there, `disableAllHooks: false`),
  so this is one more entry in the same file, with no change to how the child is started, fed or stopped. A tool
  boundary is where a running agent naturally re-reads its situation.
- **A user turn over `--input-format stream-json` (rejected).** The runner feeds the prompt from a file and the child
  ends when its turn ends (`-p`). Switching to stream-json input means keeping stdin open, writing user messages into a
  live session and deciding when to close it: a change to the runner's whole lifecycle (cancel, timeout, end-of-input)
  for a feature that only needs to add text. And a note sent while the model is mid-turn would wait for the turn's end,
  which for a long-running child is the end of the task.
- **PreToolUse (rejected).** Its output decides whether a tool call runs; mixing a context note into the permission
  path is the opposite of "never treated as permission changes". UserPromptSubmit never fires again in a `-p` child.

### A note is text, never a permission

The hook returns only `additionalContext`. It never returns `decision`, `permissionDecision`, `updatedInput`,
`updatedToolOutput` or `classifierContext` (the CLI offers the last one to PostToolUse hooks as "host-asserted context
shown to the auto-mode permission classifier": deliberately not used, so a note cannot sway the classifier as a
host assertion), and it always exits 0 (an error is one line on stderr: a non-blocking hook error; the tool result
stands). The child's permission mode, allowed tools, MCP servers and boundary hook are fixed at launch and unchanged by
a note. The directive text of a child does not promise steering; the note's own prefix says where it comes from.

### What is recorded where

- `child-tasks.jsonl`: `steer_sent` `{task_id, steer_id, host?, caller_task_id?}` and `steer_delivered`
  `{task_id, steer_id}`; refusals as `refused` `{reason: steer_scope, task_id, caller_task_id?}`. Never the note text,
  never its length. The Office and live views ignore these events (they read started / finished / cancelled only).
- `task_status`: `steer: {pending: <n>, delivered: <n>}` for a task that has steering (its host's counts for a remote
  task).
- The note text lives only in the task dir (`steer.jsonl`, 0600, in a 0700 dir; pruned with the task dir) and, once
  delivered, in the child's own conversation: its redacted `stdout.log` and its run export (`task-runs.jsonl`), like the
  prompt.

## [ESCALATE] — owner decisions (defaults built)

1. **The provider's run-id bound is 6 h for every child.** Default built: `CHILD_RUN_MAX_S` = the larger cap (6 h),
   since the provider cannot tell a sub-orchestrator's run id. Alternative: pass the cap in a header and bound each run
   by its own.
2. **Limits on a note.** Default built: 2000 characters, printable plus newline and tab; at most 10 pending per task;
   at most 8000 characters injected per tool boundary.
3. **A worker on another host than its sub-orchestrator.** Default built: the main session steers what it can see by id
   (its own tasks and the stubs it holds); a sub-orchestrator steers its own workers wherever they run. Steering a
   remote grandchild from the main session directly is not built.
4. **A note sent just as the task ends.** The running check and the append are not one transaction with the runner: a
   note appended in the last moment of a run stays pending, and task_status shows it as `pending` after the end. Default
   built: shown, not retried.
5. **Subagent tool calls deliver nothing.** Default built: only the main loop's tool calls deliver; a child that spends
   a long stretch inside one subagent sees the note when the subagent returns and the main loop makes its next tool
   call.
