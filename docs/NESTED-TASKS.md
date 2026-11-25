# Nested child tasks: sub-orchestrators, one more layer

Owner decision 2026-11-25. Branch `nested-child-tasks`. Code: `cortados_router/child_tasks.py` (caller_identity,
check_depth, _children_guard, cancel, _children_report), `cortados_router/subagent_mcp.py` (`delegate`, route_task in
a sub-orchestrator). Tests: `tests/test_nested_tasks.py`.

## Spec

**Outcome.** The main orchestrating session can hand a big task to a child task that is itself an orchestrator (a
*sub-orchestrator*): it breaks the task down, starts its own worker tasks, verifies them and reports. Exactly one more
layer:

| depth | who | may start tasks |
|---|---|---|
| 0 | the main session (anything not under a child-task runner: the owner's Claude Code session, the bot gateway) | yes; `delegate: true` allowed |
| 1 | a child task. With `delegate: true`: a sub-orchestrator | only a sub-orchestrator, only workers (no `delegate`, no `chrome`) |
| 2 | a worker (a child of a sub-orchestrator) | never |

**Files in scope.** `cortados_router/child_tasks.py`, `cortados_router/subagent_mcp.py`, `tests/__init__.py`,
`tests/test_child_tasks.py` (the remote fixture and one spawn-failure test), `tests/test_nested_tasks.py` (new),
README "Delegating whole tasks", AGENTS.md, this file.

**Acceptance check.**
`python3 -B -m unittest tests.test_nested_tasks tests.test_child_tasks tests.test_task_watch tests.test_subagent_mcp`
and the full suite (`python3 -B -m unittest discover -s tests -t .`), both OK.

### Rules (what start_task does)

- `delegate` (boolean, default false) is a new start_task argument. Only a depth-0 caller may set it.
- A sub-orchestrator gets `SUB_ORCHESTRATOR_DIRECTIVE` instead of `CHILD_DIRECTIVE` (plan, delegate, verify, report;
  act directly only for trivial steps; how to wait; list every child's id and state), and the cortados-route server
  (added with `--mcp-config` to the host's own servers; with `connectors` it is already in the strict config). Its
  start_task / task_status / wait_task / list_tasks / cancel_task and route_task work. A child without `delegate` keeps
  today's behaviour exactly (CHILD_DIRECTIVE; start_task and route_task refused).
- Refused, each with a message naming the rule, and counted as a `refused` event with `reason: depth_limit` in
  `child-tasks.jsonl` (the live view and the Office ignore unknown events):
  - any start from a worker (depth 2): "workers never start tasks";
  - any start from a depth-1 child started without `delegate`;
  - `delegate: true` from a sub-orchestrator (or any depth other than 1);
  - `chrome: true` from a sub-orchestrator (Chrome serves one client per machine and browser work is the owner's);
  - a computed depth above 2.
  `delegate: true` with `chrome: true` is refused at any depth (a login-mode sub-orchestrator would hold the host's
  Chrome lock while it waits on its workers).
- Inside a sub-orchestrator: `parent_task_id` is set to the sub-orchestrator's id (a different value is refused);
  `host` defaults to `here`, the sub-orchestrator's own host (`here` from a child always means the child's host, as the
  cortados-route server runs there); the origin (Office/live attribution) is the parent's, so workers stay in the main
  session's tree (also when sent to another host: the parent's origin is sent along).
- No cap on the number of workers beyond what exists (owner: no caps): routed children are bounded by the router's
  quota gates, the machine-load guards are unchanged.
- Workspaces: unchanged. A worker with `repo` gets its own worktree; naming the branch the sub-orchestrator holds is
  refused as today. The directive tells the sub-orchestrator to give each worker no `branch` (it gets `task/<id>`) or a
  new branch of its own, then fetch and merge them into its own branch itself and run the tests.

### How depth is enforced, and why a child cannot forge a lower one

Depth is never read from an env var. `caller_identity()` reads one `ps -axww -o pid=,ppid=,command=` table and walks
from the calling process up its parent chain to the NEAREST child-task runner (`child_tasks.py --run <task dir>
[--lock-fd N]`, the command line `start()` and the launchd plist give every runner). That runner's `meta.json` on this
host holds the task's `depth` and `delegate` (written by `start()` before the spawn; a task from before this change
reads as depth 1, no delegate). The new task's depth is that + 1 (0 + 1 when there is no runner above the caller).

- Unsetting `CORTADOS_CHILD_TASK` / `CORTADOS_TASK_ID`, pointing `CORTADOS_TASKS_DIR` elsewhere, starting an edited copy
  of the cortados-route server, or editing its MCP config changes nothing: the runner is still an ancestor, and its
  command line names its own task dir (any tasks root counts in production).
- A process that *claims* to be in a child (`CORTADOS_CHILD_TASK` or `CORTADOS_TASK_ID` set) with no runner above it (it
  detached) gets its claimed task's record when that task is running on this host, else is refused; never depth 0.
- `ps` failing is a refusal ("cannot tell"), never depth 0.
- Over ssh (`--remote`), the requesting host computed the depth (it can see the caller's process tree) and sends it;
  the host takes the deeper of that and its own ancestry check (a `--remote` started by hand inside a child on the host
  itself still finds that child's runner) and applies the same rules.
- **Older hosts fail closed (owner default, fix round 1).** A nested start on another host (`delegate: true`, or any
  start from a sub-orchestrator) first asks the host's `capabilities` (`depth`, `delegate`, `start_id`,
  `cancel_tombstone`; `REMOTE_CAPABILITIES`). A host on an older checkout answers "unknown remote operation", and the
  start is refused with that said, before anything is sent. Plain tasks skip the handshake and stay backward compatible
  (an older host that ignores the requested id gets its stub under its own id, as before). The warning when a host's
  reply does not echo `depth`/`delegate` stays as a backstop.
- A start inside a sub-orchestrator records the child in `<sub task dir>/children/<child id>` before the spawn (for
  another host: before the request is sent, see the remote-start intent below), under `<sub task dir>/children.lock`.

**Residual, stated plainly.** A child that both detaches from its process tree (double fork) and scrubs its
environment, or runs its own Python against this module's internals, is running arbitrary code as the owner: it could
equally run `claude` directly or call the provider with the token in its settings file. That was already outside the
child-task boundary and stays outside it. The ssh hop trusts the requesting host's depth as it trusts the rest of the
request (the ssh key is the authentication).

Test seam: `ANCESTRY_SCOPE` (module attribute, default `"any"`). `tests/__init__.py` sets `"tasks_dir"` (only runners
under the test's own tasks dir count) and removes the claim variables, so a suite run inside a real child task does
not see that child's runner as the caller of every in-process start. Not an env var on purpose.

### Lifecycle

- **Cancel cascade.** `cancel_task` on a sub-orchestrator: under its children lock it writes the `cancelled` marker
  (from then on its start_task refuses, "is being cancelled"), signals it, then cancels EVERY recorded child (remote
  ones on their host; `children_of` is not bounded, only what task_status shows is, `CHILDREN_SHOWN` = 500) and returns
  them in `children`. Cancelling a sub-orchestrator that already ended still cancels its running workers. A remote
  sub-orchestrator's cascade runs on its own host (where its children list is).
- **No worker escapes a cancel, also when the lock wait times out.** A start holds the children lock from its marker
  check to the child record. If a cancel cannot get the lock in `CHILDREN_LOCK_WAIT_S` (120 s, a stuck holder) it
  writes the marker without it; a start already past its marker check then records and launches its worker, and
  re-checks the marker AFTER that (`_start`, `_cancel_late_child`): marked, it cancels the worker it just launched and
  fails ("was cancelled while this start ran"). The cancel enumerates the children only after writing the marker, so
  either it sees the child or the start sees the marker. A task cancelled while it is still starting (no runner pid
  yet) gets the marker alone: its runner checks it before the spawn and, from now on, while the child runs.
- **The remote-start intent (fix round 1, P1).** For a start on another host the requesting side chooses the task id,
  writes the stub (flagged `start_unconfirmed`) and the parent's child record BEFORE the request is sent, and sends the
  id. The host starts the task under exactly that id; a second start with it launches nothing and answers with the
  existing task (`duplicate: true`; idempotent: mkdir of the task dir is the start's first step). A host that refuses
  cleanly, or a request that provably never left, drops the intent. "Provably never left" (fix round 2, P1) is an ssh
  exit 255 with no output whose last stderr line is ssh's own pre-session failure (connect refused / no route / timed
  out, name resolution, host key, authentication: `exec_hosts.PRE_SESSION_RE`); any other exit 255 is ambiguous (an
  established connection that drops after the launch also exits 255) and keeps the intent. The stub is drawn under a
  fresh id: a taken id is never touched (fix round 2, P2: a collision once removed another task's record); a new id is
  drawn up to `STUB_ID_TRIES` (5) times, then the start is refused. A lost or timed-out reply keeps it: the start
  fails with the id named, `task_status <id>` asks the host (and clears the flag once the host answers for the id),
  `cancel_task <id>` and the parent's cascade cancel it by id. A cancel of a `start_unconfirmed` id the host never
  started leaves a TOMBSTONE there (a cancelled record under the id), so a start still in flight finds the dir and
  launches nothing.
- **A cancel always marks an existing task dir (fix round 2, P2).** Whatever `_state()` reads (a start paused between
  its mkdir and its `meta.json` past `META_GRACE_S` reads as failed), cancel writes the `cancelled` marker into the dir;
  a dir with no metadata answers `cancelling` (a stalled or still-running start, not a failed task). The start checks
  the marker immediately before the launch: marked, it records the task as cancelled (result.json, the settings file
  removed), keeps the dir (a repeated start of the id launches nothing) and fails; the runner checks it again before
  the spawn and while the child runs.
- **End of a sub-orchestrator (owner defaults 2026-11-25, recommended by the orchestrator; the owner may adjust).** When
  its run ends, its runner reads every child by explicit id (local ones one by one, remote ones `CHILDREN_LIST_BATCH` =
  200 ids per host `list` call; never a recent-tasks listing) into `result.json` `children`. A child's state is its own,
  or `unknown` (no record, not listed) or `unreachable` (its host did not answer); neither is taken for running or ended.
  Then:
  - It ended normally (its result would be `succeeded`) with workers not ended: those its final message names on a
    HANDOFF line keep running (`children_running`, `children_handoff`); every other one
    is cancelled (`children_cancelled`) and the sub-orchestrator's outcome is `incomplete`: `outcome: "incomplete"` and
    an `error` listing them, so its state is `failed` (the watcher exits 1; no new terminal state). Listing a worker's
    id and state in the report is not a handoff: only a HANDOFF line is: a whole line exactly `HANDOFF: <task id>`
    (a valid id, optional trailing whitespace), outside fenced code blocks; a quoted (`> HANDOFF: …`) or indented line,
    or prose such as "No handoff is authorized for <id>", is not (fix round 2, P2).
  - It failed, was cancelled or hit its run-time cap (also when its runner died and the next status recovers it):
    every worker not ended is cancelled.
  - Workers that could not be cancelled (their host did not answer) are `children_unreachable`, with a warning
    (`task_status.warnings`, the watcher's `warnings` count) for follow-up.
- **The run-time cap is unchanged** (`CORTADOS_CHILD_MAX_S`, 3 h, for sub-orchestrators too). A sub-orchestrator
  waiting on 3 h workers can hit it first, which now cancels those workers. A larger cap for `delegate` tasks is an
  owner decision still pending ([ESCALATE] 2 below).

### Waiting (the reliable blocking wait)

The main session waits by running `watch_command` in the background: an interactive Claude Code session is woken when
a background command it started exits. A headless child (`claude -p`) may end when its turn ends, so a sub-orchestrator
does not rely on that. (Whether 2.1.330's `-p` keeps the process alive for background-task notifications was not
verified in this change: inspecting past children's stream logs was not permitted in the building session. The
foreground wait below does not depend on the answer.)

start_task's result inside a sub-orchestrator carries `wait_command` = `watch_command` + ` --timeout 540`
(`SUB_WAIT_TIMEOUT_S`). The sub-orchestrator runs it in the FOREGROUND with the Bash tool's timeout at 600000 ms (the
tool's 10-minute maximum), with every running worker's id in one command; exit 3 means "still running", run it again;
0/1/2 mean the watch ended for every listed task, not that every worker ended: exit 1 also means a worker STALLED (no
progress for 30 minutes) and may still be running, 2 that one could not be watched. So the directive says to read
each worker's task_status before treating it as ended. While a tool call runs, the
provider keeps the conversation (a child's run id earns the 3 h tool-wait idle limit, `provider/server.py`). The
command matches the local orchestrator boundary's watcher rule (`effect_boundary.WATCH_TAIL_RE` allows `--timeout`).
Tested end to end with the fake claude binary: a fake sub-orchestrator starts a worker from a process under its runner,
loops the wait command (exit 3, then 0) and reports the worker's state (`tests/test_nested_tasks.py`).

## Limitations: same-owner bypasses of the ancestry check

The depth check reads the caller's process ancestry on one host. Anything that runs a process OUTSIDE the child's
process tree, with the claim variables scrubbed, reads as depth 0. Each of these is the child running its own code as
the owner, which could equally run `claude` itself or call the provider with the token in its settings file; they are
outside the child-task boundary, stated plainly rather than closed:

- **ssh.** `ssh localhost …` (or `ssh <another host> …`) from inside a child starts the command under that host's
  sshd, not under the child's runner: no runner among its ancestors. With the claim variables removed, a start_task (or a
  `--remote` request) made from there is depth 0. The requesting-host depth sent over the ssh hop is trusted like the
  rest of the request (the ssh key is the authentication).
- **LaunchAgents / launchd.** `launchctl bootstrap`/`submit` (or `open`, or any other launchd-started job) runs the
  command as a child of launchd, outside the runner's tree, with an environment of the job's choosing: depth 0 the same
  way.
- **Detach + scrub, or its own code.** A double fork that reparents to launchd (pid 1) with the environment scrubbed, or
  its own Python against this module's internals.

## [ESCALATE] — owner decisions

1. **A sub-orchestrator that ends while workers still run.** Default implemented in fix round 1 (recommended by the
   orchestrator, the owner may adjust): workers not named on a HANDOFF line are cancelled and the outcome is
   `incomplete` (state `failed`). See "Lifecycle".
2. **A sub-orchestrator that fails or hits its time cap.** Default implemented: its workers are cancelled, unreachable
   ones listed for follow-up. **Still pending:** its cap is the children's (`CORTADOS_CHILD_MAX_S`, 3 h, unchanged); a
   larger cap for `delegate` tasks would be a new user-visible default.
3. **No delegation gate in a sub-orchestrator.** Children run with `--no-gate`; "act directly only for trivial steps"
   is an instruction, not enforced by `orchestrator_gate.py`'s budget.
4. **The refusal counter.** The task named "the existing depth_limit / concurrency_limit / budget counters"; no such
   counters exist in this repository's code. Refusals are now `refused` events (`reason: depth_limit`) in
   `child-tasks.jsonl`; showing them in the live view or dashboard is not done.
5. **cortados-route twice.** Without `connectors`, a sub-orchestrator gets cortados-route through `--mcp-config` in
   addition to the host's own MCP servers; if the host's user config also registers cortados-route, Claude Code's
   precedence picks one. Both run that host's checkout and decide depth by the process tree, so either is correct; a
   strict config (only cortados-route) would drop the host's other servers from sub-orchestrators.
6. **Older execution hosts.** Default implemented: a nested start fails closed after the capability handshake (see
   "How depth is enforced"); plain tasks stay backward compatible.
7. **Threat-model residual**: the same-owner bypasses above.

## Proposed change to the owner's orchestrator directive (NOT applied)

File: `~/.config/ai-coding/CORTADOS-ORCHESTRATOR.md`, section "Who this applies to". Today:

> - Applies to the TOP-LEVEL session Forest talks to.
> - If you are a SUBAGENT, a CortadOS CHILD TASK, a routed delegate, or your prompt says you were handed a
>   self-contained task: do the work yourself, directly. Do not delegate again and do not call `start_task`.

Proposed replacement (apply after merge and deploy to every execution host):

```markdown
## Who this applies to
- Applies to the TOP-LEVEL session Forest talks to (depth 0).
- A CortadOS SUB-ORCHESTRATOR (a child task started with `delegate: true`; its system prompt says so) follows this
  file one level down: it plans, delegates to WORKER tasks with `start_task` / `route_task`, verifies and reports. It
  waits for its workers in the FOREGROUND (the `wait_command` from start_task's result, Bash timeout 600000 ms, again
  while it exits 3), never by a background command, and ends with every worker's task id and final state. It does not
  edit #60 and never starts a sub-orchestrator or a `chrome: true` task (both are refused).
- If you are a SUBAGENT, a routed delegate, any other CortadOS CHILD TASK (a worker, or a child started without
  `delegate`), or your prompt says you were handed a self-contained task: do the work yourself, directly. Do not
  delegate again and do not call `start_task` (it is refused there).
- From the top level, pass `delegate: true` only for a big task that splits into several independent pieces worth
  their own sessions; one piece of work is still a plain `start_task`. Cancelling a sub-orchestrator cancels its
  workers.
```
