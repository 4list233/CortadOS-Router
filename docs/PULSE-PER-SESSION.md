# Per-session pulse: each orchestrator's own tree

Branch `pulse-per-session`. The cortados-pulse pane in a Claude Code session shows only that session's work, as a live
tree: its child tasks, each child's sub-sessions, nested tasks under their parent, and the subagents it routed directly.
The dashboard's Live tab shows every orchestrating session's tree, the Grok Bot's, and an "Unattributed" group.

## 1. What we took from claude-flightdeck

[scasella/claude-flightdeck](https://github.com/scasella/claude-flightdeck) is a Claude Code mod: a live agent dashboard
in a pane (main-loop vitals, an advisor timeline, a permission gate, subagents as cards or swimlanes, a turn receipt, a
log). I read its README and `hooks/core.ts` on 2026-11-14, read only: nothing was installed or run. It watches one
session's own events (`agent.spawn`, `turn.step`, `tool.call`), so it sees nothing across sessions; CortadOS has to
build its tree on the provider from the child-task log and the routed traffic.

What fits, and where it went:

| Flightdeck idea | Here |
| --- | --- |
| Subagents drawn as cards while they fit, then swimlanes on one time axis | A tree: session → task → sub-session. A side pane is 40–60 columns, so it gets indented lines, not cards. |
| A running clock per agent that redraws between events (`elapsed.tsx`) | Every running or waiting node has an elapsed time that keeps counting between polls: the pane adds the time since the snapshot, the dashboard ticks once a second. |
| Status shown as a glyph and a word, not colour alone | The same glyphs in both views: `●` running, `◷` waiting, `✓` done, `✕` failed, `?` unknown, `⊘` cancelled. The dashboard pairs each with its word in a pill. |
| What needs attention is red (`max_tokens`, denials, errors) | Failed or unknown tasks are counted in `attention`, and so is each failed sub-session of a task that is neither (a failed task counts once, however many of its sub-sessions failed). The pane draws them bold under a `! N needs attention` line, and a task with failed sub-sessions says "N sub-sessions failed" on its own line, even folded or past the list cap. The dashboard draws them red and opens their group and task. |
| Panels with nothing to show take no room; a finished card folds | A finished task is one line. A running task opens with its model line and its sub-sessions. An orchestrator with no tasks shows one line. |
| Expand an agent on demand (keys `1`, `2`, …) | Dashboard: each group and each task with children is a native `<details>`. It opens by default when it has live work or a failure, and it stays as you left it across the 5 s re-render. The pane has no key handling, so it uses fixed folding rules and the dashboard holds the rest ("+N more (dashboard)"). |
| Only real event data; inferences labelled | Tokens are only shown when a backend reported them, never estimated. The one inference, which sub-session is the "main loop", is described in §4. |
| Narrow-width rules (draws at 24–120 columns) | Pane lines are short and dot-separated. Lanes are not shown in the pane, and lists are capped: 6 live tasks, the last 3 finished, 4 sub-sessions per task, 3 routed subagents. |

What did not fit: per-agent context gauges and cost, which need the session's own `turn.step` usage. The provider
reports fresh tokens per routed conversation instead. Also left out: the permission gate, the advisor timeline and
animated connectors.

## 2. What the owner sees

The pane (`cortados-pulse` 0.5.0), from the generated fixture (`hooks/session.fixture.ts`) at about 48 columns:

```
This session
2 running · 1 done · 1 unknown
! 1 needs attention
● Build the tree · 5m 00s
  claude-sonnet-5 · on air · tool Edit · 14k tok
  ├ ● main loop · claude-sonnet-5 · model turn · 4.9k tok · 4m 50s
  ├ ● subagent · gpt-6.1 · running a tool · 1m 00s
  └ ✓ subagent · qwen3-30b · closed idle · 1.0k tok · took 1m 30s
  ↳ ● Nested step · 3m 20s
      claude-haiku-5 · on air
      └ ◷ main loop · claude-haiku-5 · idle between turns · 3m 10s
    ↳ ✓ Deeper step · succeeded · took 50s
? task t20261111093000-a0000009 · on studio, state there
routed directly
└ ● Check the docs · gpt-6.1-mini · model turn · 40s
after a /clear, earlier tasks stay under the previous session (dashboard)
Usage …  Subscriptions …  Devices …
Fleet
S1 up · 23 ms
lowest left: cursor (shared with Grok Bot) 70% (month (Cursor models: Grok, Composer, Auto))
air: com.cortados.provider running · cpu 13% · gpu 3% · disk 40% free
studio: s1-scorer running, s1-continuous running 38% · cpu 30% · gpu 37% · disk 62% free
pro: no heartbeat yet
dashboard http://<provider>:8432/dashboard
```

The pane no longer has the fleet-wide "Running" section (other sessions' tasks and conversations). It comes back only
in the fallback (§5).

The dashboard's Live tab gets an **Orchestrators** panel above Tasks. Text mock (▾ open, ▸ folded):

```
Orchestrators                                    2 main sessions · Grok Bot · tasks on air only
▾ ● working  CortadOS-Router  on pro · session aaaaaaaa · claude-opus-5 · tool mcp__cortados-route__start_task
             ● 2 running · ✓ 1 done · ? 1 unknown   ! 1 needs attention
  │ ▾ ● running  Build the tree                                                         5m 00s
  │     on air · claude-sonnet-5 (claude#b) · tool Edit · 14.0k tok (+50.0k cached) · 3 sub-sessions
  │       ├ ●  main loop · claude-sonnet-5 (claude#b) · model turn · 4.9k tok (+40.0k cached)   4m 50s
  │       ├ ●  subagent · gpt-6.1 (codex) · running a tool                                    1m 00s
  │       └ ✓  subagent · qwen3-30b (local-studio) · closed idle · 1.0k tok                   1m 30s
  │       ┆ ▾ ● running  Nested step · orchestrator inherited from its parent                 3m 20s
  │       ┆     └ ◷  main loop · claude-haiku-5 (claude#a) · idle between turns               3m 10s
  │       ┆     ✓ done  Deeper step                                                           50s
  │   ? unknown  task t2026…000009 · runs on studio; its state is on that host   (red)
  │   Routed directly by this session
  │   └ ●  Check the docs · gpt-6.1-mini (codex) · model turn                                40s
▾ ● working  Grounds  on studio · session bbbbbbbb · claude-opus-5     ● 1 running · ✕ 1 failed   ! 1 needs attention
  │   ● running  Follow-up by Grounds                                                         2m 00s
  │ ▾ ✕ failed   Their secret plan   (red)                                                    16m 40s
  │       └ ✕  main loop · FAILED
▸ • —  Grok Bot  bot gateway                              ✓ 1 done
▸ • —  Unattributed  tasks with no orchestrator recorded …  ✓ 3 done
```

The existing Tasks, Conversations, Routed calls, Work split and Hosts panels are unchanged.

## 3. Linkage: how every node knows its orchestrator

| Link | How | Where |
| --- | --- | --- |
| Claude Code session → task | Unchanged: `subagent_mcp.task_origin` records the session's salted Office tag (`sha256("<salt>:<CLAUDE_CODE_SESSION_ID>")[:16]`, the salt per machine in `~/.config/cortados/office-salt`) as the task's `origin`. It goes into `meta.json` and into every event's `origin_session/_project/_device`. | `subagent_mcp.py`, `child_tasks.clean_origin` |
| Grok Bot → task | Unchanged: a start from the bot gateway's client records the named origin `grokbot` (`child_tasks.BOT_ORIGIN`). The tree draws it as an orchestrator of kind `bot`. | `subagent_mcp.task_origin` |
| Parent task → nested task | **New:** `parent_task_id` was only in `meta.json`. It is now in every lifecycle event too (`_event_fields`), set before `cmd.json`'s event fields are written so the `finished` event carries it as well. | `child_tasks._start`, `_event_fields` |
| Nested task without an origin of its own | **New:** at start, if the caller has no session tag (a launcher with `CORTADOS_PARENT_TASK_ID` and no Claude Code session, or a session without the salt) and a parent is known, the task takes the parent's recorded origin. `meta.json` and the events then carry `origin_via: "parent"`. The view does the same along the `parent_task_id` chain for older records, and marks those tasks `origin_via: "parent"` too. A task that has an origin of its own keeps it: a follow-up started by another session belongs to that session's tree, and its parent is shown as "continues another orchestrator's task". | `child_tasks.parent_origin`, `live._origin_of` |
| Remote task → orchestrator | **New:** the local stub of a task started onto another execution host (`remote.json`) now records the origin. The tree lists such a task under its orchestrator as `elsewhere` (status unknown, "its state is on that host"), because the provider host's view never uses ssh. | `child_tasks._write_stub`, `remote_stubs` |
| Task → its sub-sessions | The child's routed traffic carries `x-cortados-run-id: <task id>`, so every provider conversation it opens has the Office key `r:<task id>`. The child's main loop and each routed subagent inside it are separate conversations under that key. **New:** the provider keeps a bounded memory of recently closed run conversations (`Registry.ended`, 256), so a finished subagent stays visible under its task. | `server.Registry`, `live._subs_by_run` |
| Session → its own routed subagents | Routed subagents of the main session itself (route_task, cortados-routed-subagents) carry `x-cortados-session` and appear under the session as "routed directly". | `live.orchestrators_section` |
| Tokens | **New:** an in-memory total per conversation (`session.live_tokens`) and per run id (`RunRecords.add_tokens`), summed from each backend turn's usage. This is the same usage `core.log_tokens` writes to router.db, but kept by run id, which router.db does not record. The totals start at zero when the provider restarts. They are shown as fresh tokens (input + output + cache writes) plus cache reads. | `server.Handler._log_turn_tokens`, `work_split.usage_tokens` |

No raw session id, prompt, request text, path, command or tool argument enters any of this. Every string in the tree
comes from fields `live.py` already re-validates (task id, title ≤ 80 printable characters, host slug, model, lane,
tool name, Office tag) or from fixed vocabularies (status, phase, role).

## 4. Statuses and what is inferred

| Node | running | waiting | done | failed | unknown |
| --- | --- | --- | --- | --- | --- |
| task | `running` (no end event) | `queued` (waiting for the host's Chrome) | `succeeded`, `cancelled` (⊘) | `failed` | no end event after 10 h, ended without one, or `elsewhere` |
| sub-session | a model turn on the provider, or the client is running a tool the model asked for | open, idle between turns | closed (exited, reaped idle, rebuilt) | closed as failed | — |

- **"main loop" is inferred:** under a task, the first-opened conversation of the run is labelled `main`, because
  Claude Code opens a child's main conversation before any subagent's. The others are `subagent`.
- **Phase or tool:** a task's tool is the latest tool name its run asked the client to run (Office board). A
  sub-session's phase is the provider's own state.
- **Tokens:** shown only when recorded (`null` otherwise). Local-lane turns and anything before a provider restart are
  not counted.
- **Scope:** as before, the provider host's child-task log. A task on another execution host shows only through the
  stub of a start made from this machine.

## 5. Privacy: how the pane proves its session

**Choice: the pane computes its own tag locally and the provider builds a view filtered for that tag. A new scope,
`live:session`, opens only that view.**

- The pane already computes `sessionTag(salt, session id)` from the shared per-machine salt file and Claude Code's
  `$.session.id()`, exactly as the MCP server computes the task origin. The raw session id never leaves the pane.
- It calls `GET /v1/cortados/live/session?session=<tag>`. The provider builds the orchestrator section with that
  filter **before** anything is grouped, nested or capped (`orchestrators_section(session=)`). Then
  `live.session_view` returns only that subtree plus fleet health that names no session: S1, the hosts' job heartbeats
  and hardware, the lowest subscription window. The response has no tasks section, no conversations, no routed calls,
  no work split and no fleet counts. The hosts section carries no orchestrator block, and a test checks that. A parent
  task that belongs to another orchestrator is never named (`parent_task_id: null, parent_hidden: true`). The
  unattributed group is never in a session's view.
- The endpoint requires `?session=` (400 without it) and never lists sessions. A tag is 64 bits of salted hash: a
  holder of a `live:session` token can read a subtree only for a tag it already knows, which in practice means it is
  that session or its machine's salt holder. Tags are cosmetic labels inside the owner's trust boundary (as
  `x-cortados-session` already is), so the provider cannot check that the caller "is" the session. What it can and does
  guarantee is that a session's view contains nothing about another session.
- **Independent of other sessions' activity** (reviews 2026-11-14): the tree is built from the uncapped task rows,
  conversation items and remote stubs, narrowed to the session's origin first, and only then capped. The fleet lists'
  own caps (64 conversations, 50 stubs, the Office's 32 workers per floor) never decide what a session sees, and every
  flag in a session's `inputs_truncated` is about that session's own data. Where each input comes from, and its bound:

  | Input | How the session's own part is found | Bound and cost per request | Said when cut |
  | --- | --- | --- | --- |
  | Child tasks (local) | The child-task log (24 h, read once per collect), filtered by origin through the parent chain. | The log tail the live view already reads (`usage.CHILD_TAIL` lines / `CHILD_TAIL_BYTES`). | — (unchanged by this review) |
  | Remote stubs | The **origin index**: `start_task` writes the stub (`<tasks>/<id>/remote.json`) and an empty entry `<tasks>/by-origin/<origin>/<id>` (`child_tasks._write_stub`). The session endpoint reads only `by-origin/<its tag>/` and those tasks' `remote.json` (`child_tasks.remote_stubs_for`); it never lists the task dirs, so other sessions' stubs, however many or new, are neither read nor counted. | One `listdir` of this origin's own entries, O(its stubs since the last daily prune), then at most `REMOTE_STUBS_SCAN` (200) stub reads, newest first. | `task stubs`, only when THIS origin has more than 200 entries. |
  | Conversations | Each open conversation's own Office tags, kept per conversation by the registry (`Registry._conversation_meta`: session, project, device, title), uncapped. The board only fills a conversation that carries none. | The registry's open conversations (all of them, as the fleet list), then `TREE_INPUT_MAX` (2000) after the filter. | `conversations` |
  | One-shot routed workers (no open conversation) | The Office board's **uncapped** worker list (`OfficeBoard.view(uncapped=True)`, the live view only; the Office endpoint is unchanged), each worker with its own session tag, before the per-floor (32), lobby and floor caps. | The board's memory: `office.MAX_WORKERS` (1024, an LRU across the fleet). | `routed workers`, only for a session that lost a worker to that LRU in the last 10 min (`OfficeBoard.evicted`). |

  The fleet view keeps its own scan of the newest 200 task dirs (O(task dirs) `listdir`, ≤ 200 stub reads) for its
  tasks section and its tree, and says `task stubs` when that scan stopped; that flag never reaches a session view.
  Stubs written before the index existed are indexed by the next daily prune (`child_tasks.prune`), which also drops
  entries whose task dir is gone (O(index entries), never on a view). Until then such a stub shows only in the fleet
  view. The session endpoint never opens another session's task directory (its own open tasks are peeked first, so
  others cannot use up the 64 peeks), and a task-dir error is published only with the task it belongs to: another
  session's `PermissionError` never reaches this view or changes its status.
- Why not have the provider derive the session from the request? The provider never sees the session id, and must not:
  it is a `claude --resume` handle. Why not filter in the pane? The pane would then download every session's metadata,
  which was the old behaviour.
- Token: the default pane token is still the owner token, because `/resources` needs it. For a pane that must not be
  able to read the fleet view at all, give it a `live:session` client token
  (`python3 -B -m cortados_router.provider.clients add pane-<host> --scope live:session --token-file <path>`). It opens
  only `/v1/cortados/live/session`: 403 for `/v1/cortados/live`, the office, `/resources` and the model endpoints. The
  Usage, Subscriptions and Devices sections then say the provider rejected the token. `live:read` also opens the
  session view.
- The dashboard, behind the owner token or the password session, reads the full `GET /v1/cortados/live`, whose new
  `orchestrators` section has every orchestrator, Grok Bot and the unattributed group.
- **Fallback:** if the session cannot be identified (no salt file yet) or the provider is older (404, or an answer that
  is not this session's subtree), the pane falls back to the 0.4 behaviour: the fleet view, filtered in the pane. It
  says why. This keeps the pane useful during the rollout. Once the provider is updated and the salt exists, the fleet
  view is never fetched.

## 6. API

`GET /v1/cortados/live` gains `orchestrators` (and `overview.orchestrators`, the number of non-unattributed groups).
`?session=<tag>` narrows it like the other sections. `GET /v1/cortados/live/session?session=<tag>` is the pane's view.

```
orchestrators: {as_of, status, errors[], scope: "provider host", window_s, truncated, inputs_truncated[], session_filter?,
  items: [{origin: <tag> | "grokbot" | null, kind: session | bot | unattributed, project, device,
           status (working / idle / not live; "n/a" for bot and unattributed), model, tool, last_activity,
           counts {running, waiting, done, failed, unknown}, attention,
           tasks: [task], tasks_hidden, routed: [sub-session], routed_hidden}]}
task:        {task_id, title, state, status, phase, tool, host, provider, lane, model, started_at, ended_at, elapsed_s,
              workspace, browser, parent_task_id, parent_hidden, origin_via, tokens, sub_sessions: [sub-session],
              sub_sessions_hidden, sub_sessions_failed (all of them, listed or not), tasks: [task]}
sub-session: {ref (c-<12 hex digest> or a run id), role: main | subagent, title?, status, phase, tool?, provider, lane,
              model, started_at, ended_at, last_activity, elapsed_s, tokens}
tokens:      {input, output, cache_read, cache_write, fresh} | null
```

Caps: 32 orchestrators, 20 top-level tasks each (active first; counts cover all), nesting depth 4 (a deeper or cyclic
parent chain is drawn flat, never lost), 8 sub-sessions per task, 8 routed subagents. The section has its own
`as_of`, `status` and `errors`. Failures of the task log, the office board, the conversation list, the ended list or
the run tokens make it `partial` or `error`, never an empty success. `attention` = failed + unknown tasks + the failed
sub-sessions of every other task. `inputs_truncated` names an input that was cut: `conversations` or `task stubs`
past the hard ceiling (`TREE_INPUT_MAX`, after the filter), `task stubs` when the stub read stopped at its 200 (the
fleet's task-dir scan in the fleet view, the session's own origin index in a session view), `routed workers` when the
board's memory dropped a worker of this session (the fleet view: of any). `truncated` is then true as well. Bounds and
costs per input: §5.

## 7. What the owner must do

1. **Provider (the Air):** merge the branch when reviewed, then restart the provider the usual way
   (`com.cortados.provider`). Until then, panes use the fallback.
2. **cortados-route MCP servers:** no action. The linkage changes take effect at the next `start_task`, which runs the
   new `child_tasks.py` from the checkout the server launches. Remote execution hosts pick up the event fields once
   their checkout is updated (`git pull` on each exec host).
3. **The pulse mod (each machine):** installed from the marketplace:
   `/plugin marketplace update cortados-router`, then `/plugin update cortados-pulse@cortados-router` (or `/plugin`
   → cortados-pulse → Update), then `/reload-plugins`. The version reads 0.5.0. Loaded with `--plugin-dir`: pull the
   checkout and restart Claude Code (or `/reload-plugins`). Nothing to configure. The salt file
   `~/.config/cortados/office-salt` is made by the cortados-route MCP server on first start, as before.
4. **Optional:** a narrow `live:session` token for panes (§5).

## 8. Tests

- `tests/test_orchestrator_tree.py`: the tree (groups, nesting and inheritance, a follow-up by another session, a
  parent cycle, sub-sessions with roles, statuses and tokens, routed subagents, Grok Bot, unattributed, attention,
  caps, source errors). The session filter: nothing of another origin, the bot or the unattributed group appears,
  checked by tag, task id, title and conversation ref; filtering happens before the caps, and 64+ foreign
  conversations or 50+ foreign remote stubs hide nothing of this session's; the hard ceiling applies after the filter
  and is said; an unknown tag gets no foreign task-dir error and opens no foreign directory; attention counts a failed
  sub-session once. Review 2026-11-14 (second round): 300 newer foreign remote stubs in a real tasks dir neither hide
  a session's older stub nor give any flag to it or to an unknown tag, and the session endpoint never lists the task
  dirs; the fleet scan's cut is said in the fleet view only; 40 routed agents of one session on one floor are all
  counted (open conversations with or without their own tags, or one-shot workers), with no flag; a worker lost to
  the board's LRU is said for its own session only; the owner's view is unchanged under the caps. Metadata only. The endpoint:
  required tag, `live:session` versus other scopes, a real routed run appearing as a sub-session and staying after it
  closes. Linkage helpers. The generated `hooks/session.fixture.ts` is checked against `live.session_view`.
- `tests/test_live.py` (`StubScanTest`): the origin index reads one origin's stubs only, read-only, with its own
  truncation; the daily prune drops entries of removed stubs and indexes older stubs.
- `tests/test_child_tasks.py`: inheritance at start (no origin of its own → the parent's, `origin_via: parent`, in
  `meta.json` and both events). A nested task with its own origin keeps it. A remote stub names the orchestrator.
  All with the fake claude and the fake ssh transport: no real child runs.
- `tests/test_dashboard.py`: the Live tab draws the tree from the same fakes.
- The mod (`claude plugin test integrations/claude-mod/cortados-pulse`): the tree lines from the provider-generated
  fixture, glyphs, tokens, failure and cap lines, and that a poll with a tag reads only the session view. A 401 or 5xx
  settles the poll with the token scrubbed; a 404 falls back.
  Regenerate the fixtures after a deliberate shape change:
  `python3 -B -c "from tests import test_orchestrator_tree as t; t.write_session_fixture()"` and
  `python3 -B -c "from tests import test_pulse_fixture as t; t.write_live_fixture()"`.
