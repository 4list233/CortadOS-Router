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
Orchestrators                                    2 main sessions · Grok Bot · tasks on air, pro
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
- **Scope (#118):** the provider host's child-task log, plus the task entries each exec host's heartbeat carries from
  its own log (§9). Without a heartbeat from that host, a task there shows only through the stub of a start made from
  the provider host, or in the pane from the pane's own machine's log.

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
  view. The session endpoint never peeks at another session's task directory (its own open tasks are peeked first, so
  others cannot use up the 64 peeks), and a task-dir error is published only with the task it belongs to: another
  session's `PermissionError` never reaches this view or changes its status. Its one read outside its own records is the
  ownership check of round 4 (§9): the `remote.json` of each task id another host's heartbeat names, compared and never
  published.
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
   → cortados-pulse → Update), then `/reload-plugins`. The version reads 0.5.2 (#118, §9). Loaded with `--plugin-dir`: pull the
   checkout and restart Claude Code (or `/reload-plugins`). Nothing to configure. The salt file
   `~/.config/cortados/office-salt` is made by the cortados-route MCP server on first start, as before.
4. **Optional:** a narrow `live:session` token for panes (§5).

## 8. Tests

- Review round 4: `tests/test_orchestrator_tree.py` `ProviderObservedJoinTest`: the reviewer's P1 repro (the Studio
  claims VICTIM first under MINE with VICTIM's child tag: MINE's view gets no title, tool, model, sub-session or
  token), a provider-observed origin joining the run, a heartbeat-only task (no observed origin, or none of its own)
  getting nothing joined, a run seen with two origins joining nothing, the P2 stub repro (another session's stub
  rejects a forged report in OTHER's session view, also over real stub files; an unreadable stub or failed lookup
  trusts no report), the restart repro (VICTIM bound to Pro/OTHER, a fresh board on the same file, the Studio posts
  first: refused), persistence (0600, atomic, expired entries dropped at load, bad entries and an unreadable file
  skipped and said, a failed save said), and a full table refusing new ids without evicting live ones.
  `SessionEndpointTest.test_a_heartbeat_task_joins_its_run_only_through_the_origin_the_provider_saw` does it over the
  real endpoint with real headers. `tests/test_office.py` `OriginHeaderTests`: the header's rule and name equal
  child_tasks', invalid ones dropped and counted, never published by the Office. `tests/test_child_tasks.py`: a
  nested child's requests carry its inherited `x-cortados-origin`; a task with no orchestrator sends none.
- Review round 3: `tests/test_orchestrator_tree.py` `HostBindingTest`: the reviewer's repro (a newer Studio heartbeat
  claiming a Pro task under another origin gets nothing; the owner keeps it, with its run), the same host changing an
  origin or parent, a forger reporting first (it owns the id but gets no title, tool, model, sub-session or token of
  the real run, even with a guessed tag), two beats a restart never compared, the provider host's log and stubs
  binding their ids, a run conversation with a foreign tag, the bounds of the bindings. `tests/test_live_heartbeat.py`
  `TasksBlockTest`: a fake pre-#118 provider's 413 on a valid 40-entry body (over 24 KiB, within 48 KiB) sheds task
  entries, then the block, never the jobs, hardware or S1. The mod: the bounded read itself (a 5 MiB log is never read
  whole, only `tail -c` crosses, its cut line is dropped, a failing tail is said) and the "≥N" counts.
- #118 (§9): `tests/test_orchestrator_tree.py` `HostTasksTest` and `SessionEndpointTest.
  test_a_posted_pro_heartbeat_puts_its_task_in_its_sessions_view_only`: a Pro-hosted task with origin X appears in X's
  session view and under X in the fleet tree, never in another session's view or an unknown tag's (checked by id and
  title, over the real endpoint too); a task nested on the Pro under an Air task inherits its orchestrator; the provider
  host's own copy wins and nothing doubles; a stale heartbeat's task is unknown; its routed conversations are its
  sub-sessions; a host's report replaces a remote stub; a task started from the Pro onto the Air needs no stub on the
  Air. `tests/test_live.py` `HeartbeatTest.test_tasks_block`: every field rule, another host's task refused, the 24 h
  window. `tests/test_live_heartbeat.py` `TasksBlockTest`: the sender's block from a fake log (metadata only, running
  first, capped and said, an unreadable log costs the block only, an oversize body sheds task entries first) and the
  installer on the Pro (`com.cortados.task.*` skipped, `--no-tasks`). `tests/test_dashboard.py`: the Pro task under its
  session in the Orchestrators panel, "tasks on air, pro". The mod: the local log's parse (own tasks only, metadata
  only, bounded tail, 10 h rule), the merge (by id, once, counts and attention in step, a locally finished task or a
  stale heartbeat's guess corrected), the fallback lines, and the poll (read every 30 s, standing when the provider is
  unreachable).

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

## 9. Tasks on every exec host (#118)

**Problem.** A main session on the Pro that calls `start_task` with host "here" runs the child on the Pro, which logs
it only in the Pro's own `~/.cortados/child-tasks.jsonl`. The provider (the Air) builds the tree from its own log, so
neither the pane's "This session" nor the dashboard showed such a task. The round-2 origin index does not help either:
`tasks/by-origin/<tag>/` is written on the machine that called `start_task`, and the Air's session endpoint reads the
Air's. For a session on the Pro whose task ran **on the Air**, that path never applies, and it is not needed: the Pro's
`start_task` sends the origin with the request, the Air's `--remote` start records it, and the task is in the Air's own
log under that origin (`HostTasksTest.test_a_task_started_from_the_pro_onto_the_air_needs_no_stub_on_the_air`).

**Fix, provider side.** Each host's heartbeat sender (`live_heartbeat.tasks_block`) reads its own log through the live
view's own reader (`office.read_child_events`, the bounded tail; `live.tasks_section` for the state, peeking at its own
task dirs) and posts up to 40 entries of the last 24 h: task id, origin tag (and project/device), title, state, phase
(fixed phrases only), model and lane when logged, host, start and end times, parent task id, workspace state, browser
flag, and the child's own Office tag (`session`; since round 4 the provider checks it and drops it, see below). Never
the prompt, cwd, command or output. `live.validate_tasks` re-checks every field
by the rule the view applies to its own log, refuses an entry for another host (a host speaks for its own tasks; a
`live:heartbeat` token is already bound to its host) and drops entries outside the window.

A task id's host and origin are bound once (review rounds 3 and 4). The provider host's own records win: its log,
and every remote stub it wrote for a start it sent onto another host (that host's own report then replaces the stub).
Ownership is checked against ALL of them before any session filtering, in every view: besides the stubs a view already
read, the stub of each id a heartbeat names is read by id (`child_tasks.remote_stubs_by_id`: one file read per
claimed id, no listing), so another session's stub rejects a forged report in this session's view exactly as in the
fleet's. Those records are compared, never published; a stub that exists but cannot be read (or a failed lookup)
makes the view trust no report of that id. Otherwise the first host that reports an id owns it: the board binds each
id to (host, origin, parent) (`HeartbeatBoard._bind_locked`), and a later report that differs is dropped and counted.
The bindings persist in `$CORTADOS_HOME/live-task-bindings.json` (0600, written atomically when they change; a
re-reported id's time reaches the file at most hourly), loaded at start, so a restarted provider still refuses a
newcomer's claim. Each lasts 24 h after its last report (the heartbeat window); only expired bindings are removed. At
4096 live bindings a NEW id is refused and counted, never bound by evicting a live one. An unreadable file starts an
empty table and says `task bindings` in the tree's errors. The count is the fleet view's `orchestrators.conflicts` and
the poster's `task_conflicts`; it is never in a session's view, and never names an id. `live.heartbeat_rows` turns the
entries into tree rows: a heartbeat past its TTL shows its running tasks as `unknown`.

**Joins (round 4): a join key is something the provider observed itself, never something a heartbeat supplied.** The
child runner sends `x-cortados-origin: <the task's origin tag>` (from the task's own metadata, the inherited one for a
nested task) on every routed request of the run, next to `x-cortados-run-id` (`child_tasks._prepare`, allowed by
`session.OFFICE_HEADERS`). The provider records it per Office worker, per conversation (`Registry._conversation_meta`)
and per run's tokens (`RunRecords.run_origins`), the first one seen; a run that ever carries two different origins
matches nothing (`office.note_origin`). A heartbeat's task gets the run's Office worker (title, model, lane, provider,
current tool), its sub-sessions and its run tokens only when the provider observed the run's own requests carry the
task's bound origin. A heartbeat-reported task with no such observation (a run never routed here, a runner from before
round 4, a task without an origin) shows only its heartbeat's own fields. The child tag a sender still reports is
checked and dropped: no key, and no part of the binding. The observed origin is never published.

**Threat note.** Heartbeat clients are the owner's own machines (one `live:heartbeat` token per host, bound to its host
name). Such a token cannot reach the model endpoints, so it cannot create the provider observations the joins need;
`x-cortados-origin` is sent only by holders of the owner token, inside its existing trust boundary (like
`x-cortados-run-id`). The residual risk after this round is integrity only: a compromised host can show fabricated
entries for ids nobody has reported (under any origin, with its own titles), or report a real id first so the real
host's report conflicts and is dropped. It cannot read anything: no title, tool, sub-session, token or other metadata
of another session's run is joined onto what it reports, and a session's view still holds nothing of another session.

The rows join the tree's input before the session filter, so `/v1/cortados/live/session` and the
Orchestrators panel hold every host's tasks, each attributed by its origin tag (and the parent chain), and a session's
view still holds nothing of another session. The tree's `scope` is `provider host and heartbeats`, and `task_hosts`
names the hosts that sent a block (host names only). The Tasks panel keeps its provider-host scope.

Bounds: 40 entries per heartbeat, 16 (+ configured) hosts on the board, the heartbeat's byte cap raised to 48 KiB. The
sender halves its entries (`truncated`) before it drops the orchestrator block, and never drops the jobs. An older
provider's 413 (its own 24 KiB cap, checked before any field: a full 40-entry block is over it) makes the sender halve
its task entries, then go without the tasks block, then without the orchestrator block; the jobs, hardware and S1 always
go. An older provider's 400 makes it retry without the block. A host whose block was cut (more than 40 tasks in its window)
is said in the fleet view as `inputs_truncated: ["host tasks"]`, never in a session's view: that cut is over every
session's tasks on the host, and a session's flags are about its own data only (§5).

**Fix, pane side (0.5.1; 0.5.2 bounded read and counts).** The pane also reads its own machine's log (`$CORTADOS_HOME`, else
`$CORTADOS_HERMES_HOME`, else `~/.cortados`, then `/child-tasks.jsonl`) at most every 30 s, keeps only its own session's
tasks (its tag, or a parent's within the log), from the last 2 MiB / 5000 lines, and merges them by task id into the
provider's subtree (`mergeLocal`). The read is bounded (`readLocalLog`): the mod host's `$.fs.read` has no offset or
length (it reads a whole file, up to 4 MiB), so the pane stats the log first, reads it whole only when it is within
2 MiB, and otherwise runs `/usr/bin/tail -c 2097152` (`$.process.run`, no shell), which copies only the tail; if that
fails, it merges nothing and says "this machine's task log is over 2 MiB and its tail could not be read". When the
provider hid tasks past its 20-task cap (`tasks_hidden`), a top-level task only the local log has may be one of them,
already counted: the group's counts are then lower bounds, drawn "≥N" (per status, the larger of the provider's count
and those local-only tasks', plus the exact rest), and the hidden line says "up to N more". The provider's node wins (it has sub-sessions and tokens), except that a task the
local log saw end is shown ended, and a task the view knows only second-hand (a stale heartbeat, an `elsewhere` stub)
takes the local state. A task only the local log knows is drawn too, with a dim "from this machine's task log only"
line. Without any provider answer (unreachable, or never reached), the fallback view draws the local tasks the
provider's lists lack. So a host "here" task shows in its own session's pane at once, before any heartbeat exists.

**Deploy (the orchestrator, after review; nothing is installed by this branch):**

1. Merge, `git pull` on the Air, restart the provider (`com.cortados.provider`). Until then it refuses the block, and
   the senders post without it.
2. `git pull` on the Studio (its sender runs from the checkout). Its `heartbeat.json` needs no change: an absent
   `tasks` key means on.
3. The Pro, which runs no heartbeat yet. On the Air, mint its token:
   `python3 -B -m cortados_router.provider.clients add pro-heartbeat --scope live:heartbeat --host pro --token-file ~/.config/cortados/heartbeat-tokens/pro`.
   Copy it to the Pro as `~/.config/cortados/live-heartbeat-token`, mode 600. Then, on the Pro, from its checkout:

   ```sh
   scripts/install_live_heartbeat.sh pro --orchestrator --dry-run   # look first: no com.cortados.task.* label in jobs
   scripts/install_live_heartbeat.sh pro --orchestrator
   ```

   The Pro's `~/.config/cortados/provider-url` (the pane's own pointer) is where it posts. `--orchestrator` adds the
   main sessions' tokens to the Work split, as on the Air.
4. Each machine's pulse mod: `/plugin marketplace update cortados-router`, then
   `/plugin update cortados-pulse@cortados-router`, then `/reload-plugins` (0.5.2).
