# The CortadOS Office: brand, host badge, and what comes next (issue #77)

Status 2026-11-25, branch `office-cortados-brand`. Part A is **built, not deployed**: `claw3d/cortados-brand.patch`
and the host badge (`live.py`, `adapter.mjs`). Part B is **design only**: nothing in it is built. Claw3D paths are
relative to a clone at `51337f7f250459af61a7cf92b7b0015d59faed5c` (the commit `deploy-air.sh` pins) with the earlier
patches applied (`claw3d:`). The rooms design this builds on is [FLOORS-DESIGN.md](FLOORS-DESIGN.md).

## A. What was built

### A.1 Branding (`claw3d/cortados-brand.patch`)

Each string is changed where it comes from, at the narrowest point.

| Seen by the owner | Comes from (`claw3d:`) | Now |
|---|---|---|
| Header "LUKE HEADQUARTERS" | `src/lib/studio/settings.ts` `DEFAULT_OFFICE_TITLE` (per-gateway office title preference; the header upper-cases it with CSS) and `RetroOffice3D.tsx`'s default prop | "CortadOS Router" (shown "CORTADOS ROUTER"). A saved title equal to the upstream default `Luke Headquarters` is read as unset, so a Studio that already stored it shows the new title. A title the owner typed is kept. |
| Floor card "OpenClaw Floor" | `src/lib/office/floors.ts` `OFFICE_FLOORS["openclaw-ground"].label` | "CortadOS Floor" |
| Roster line "OPENCLAW · ROSTER 4" | the floor's `shortLabel` (`floors.ts`) and `PROVIDER_LABEL.openclaw` (`OfficeFloorNav.tsx`) | `shortLabel` "CortadOS"; the provider label "Router", so the card reads "ROUTER · ROSTER 4 \| LOADED" |
| Badge "OPENCLAW • CONNECTED" | `RetroOffice3D.tsx` toolbar: `{activeAdapterType} • {gatewayStatus}` (the bridge's `cortados-office` adapter type is normalised to `openclaw` by Studio, `settings.ts` `normalizeGatewayAdapterType`) | "ROUTER • CONNECTED", and the same word for every status Studio has (`GatewayStatus`: connected, connecting, disconnected): `ROUTER • CONNECTING`, `ROUTER • DISCONNECTED`. Its tooltip: "CortadOS router (status)". |

The gateway hello (`bridge.mjs`) is unchanged: Studio never shows its `server.version`, and the adapter type it
reports is normalised anyway.

### A.2 Props removed

| Prop | Where (`claw3d:`) | Why it showed |
|---|---|---|
| US flag on a pole | `src/features/retro-office/scene/environment.tsx` `WallPictures` → `OfficeFlagPole` with `UsaFlagArt`, at canvas (180, 610) | `WallPictures` is drawn for every layout, so the rooms layout kept it; it stood inside room 5 (bottom-left) |
| Brazil flag on a pole | same component, `BrazilFlagArt`, in the remote-office zone | drawn only when the remote office is on (it is off, and its settings are now hidden), removed with the US one |

Both sit behind `CORTADOS_FLAGS = false`. I found no other national or brand prop: the rest of `WallPictures` is
abstract framed art (colour blocks, a landscape, a sun). The jukebox, ATM, vending machine, QA lab, gym, phone and
SMS booths, kanban board and server room are furniture of the stock layout, which the rooms layout already replaces.

### A.3 OpenClaw controls: inventory

Against our bridge, every method that is not a read is refused with `READ_ONLY`, and the reads Studio makes get fixed,
empty answers (`adapter.mjs` `NEUTRAL_READS`). So, per control:

| Control (as labelled) | Upstream it… | Against the CortadOS bridge | Done |
|---|---|---|---|
| **OPEN HQ** rail tab + the HQ sidebar: Inbox, History, Kanban, Playbooks | inbox of agent activity, run history from chat runs, a task board (local + `cron.*`), scheduled playbooks (`cron.add`) | inbox and history stay empty (no chat runs); kanban and playbooks write cron jobs, refused | hidden (`HQSidebar` not rendered) |
| **MARKETPLACE** rail tab | skills marketplace: installs packaged skills into an agent (`skills.install`, `agents.files.set`) | refused | hidden (its only trigger left in our layout was the rail; the modal stays closed) |
| **ANALYTICS** rail tab | cost, budgets and approvals from `sessions.usage`, `exec.approvals.*` | nothing to show; approvals refused | hidden |
| "Add Agent" / "Build Company" (sidebar header and the empty-office banner) | create an agent (`agents.create`, files) or a whole company plan | refused | hidden; the banner keeps **Retry** |
| **ADD** (toolbar, top right) | opens the create-agent wizard | refused | hidden (`onAddAgent` not passed) |
| **CHAT** button and the chat dock: agent list, transcript, composer with model picker (e.g. `claude/claude-opus-5-5`), thinking level (High), **Attach**, **Mic**, **Send**, **Stop**, **New session**, the remote-agent handoff form | `chat.send`, `sessions.reset`, `chat.abort`, `agents.update` (model), voice transcription then `chat.send` | every write is refused by the bridge (`READ_ONLY`), and the transcript is always empty (`chat.history` answers `[]`), so the panel shows nothing of ours | **hidden, dock and composer both** (`CORTADOS_OPENCLAW_UI = false`): nothing typed in the Office can reach any agent. The read-only bridge was already the guarantee; hiding removes the controls that suggested otherwise |
| Agent editor (click a figure in the roster strip): avatar, IDENTITY.md and other files, rename, delete | `agents.files.*`, `agents.update`, `agents.delete`; avatar saved to Studio settings | files empty, writes refused; a saved avatar would override the family suit the bridge sends | hidden (`onAgentEdit` not passed): a click only spotlights the person |
| Delete agent (roster panel trash icon) | `agents.delete` | refused | hidden (`onAgentDelete` not passed) |
| Push-to-talk (hold Option) | records, transcribes, sends to the main agent | refused after transcription; it needs a `main` agent we never have | off (`useVoiceRecorder` disabled) |
| Studio settings (gear): gateway URL, token, adapter, connect/disconnect, office title, remote office, voice replies | edits Studio's settings file; voice replies call a TTS service | the gateway is pinned by `deploy-air.sh` (settings file + `UPSTREAM_ALLOWLIST`); voice replies read assistant messages we never send | hidden (`CORTADOS_OFFICE_CONTROLS = false`). When the bridge is down, Studio's own connect screen still appears |
| Edit office (pencil) and its Reset / Delete / Show Objects | layout editor | works, but it edits the rooms layout the desk allocator relies on (fixed desk uids) | hidden with the gear |
| **BUILDING DIRECTORY** (Prev/Next, Lobby, other floors) | switches floor, which reconnects the gateway | the Lobby is Claw3D's demo floor; every other floor is disabled or another runtime | hidden (`CORTADOS_BUILDING_DIRECTORY = false`). Studio still moves from the Lobby to the CortadOS floor on connect (its own auto-navigation); the "Current floor" card stays |
| "Welcome to Claw3D" onboarding wizard | 6 steps: connect a gateway, create agents, build a company | its connect step duplicates the deploy, the rest is refused | hidden |
| Agent Event Console (bottom left: Copy JSON, Download JSON, Clear) | a debug log of gateway events; upstream shows it whenever `DEBUG` is unset, so the Air showed it | it shows the bridge's presence events (metadata only) | hidden (debug only). Building it back is one constant |
| Kept | camera presets, heatmap and trails toggles, the roster strip (names, status dots, spotlight, desk monitor), nameplates, the floor card, the connection badge | read-only views of our people | unchanged |

Scope of the patch: it is unconditional (unlike the rooms patch, which switches itself on only for the bridge's
agents). This Studio build exists only for the CortadOS bridge: its settings file and `UPSTREAM_ALLOWLIST=localhost`
pin it there. The rooms patch's promise "with any other gateway connected, Studio is unchanged" now covers the layout
only.

Hiding a control does not stop a logged-in browser calling the route behind it: Studio's server enforces that, A.5.

### A.4 Host badge

Each person shows where it runs, at the end of the nameplate's second line: `claude-opus-5 · Pro`,
`gpt-6.1 · Air`, `done · gpt-6.1 · Studio`. A finished task's subtitle keeps its `done · ` / `failed · ` /
`cancelled · ` prefix, which the desk allocator reads.

| Person | Host from | Field |
|---|---|---|
| Child task | the task's own exec host, which the provider already had | tree task `host` (unchanged; `exec_hosts.HOST_NAME_RE` slug) |
| Orchestrator | its session's device (the `x-cortados-device` header / heartbeat / the task's `origin_device`), **matched to a known host name**: exec-hosts.json `hostnames` aliases, `CORTADOS_LIVE_HOSTS` or the defaults, heartbeat hosts, or the provider host's own name | **new** group `host` (`live.orchestrator_hosts`, published in the tree by `_TREE_GROUP_KEYS`) |
| A task's helper (routed sub-session) | the task's host | adapter |
| An orchestrator's own routed helper | the session's host | adapter |
| "No orchestrator" room | none | null |

Only a known host name is ever published or shown (`air`, `pro`, `studio`, shown capitalised). A device that matches
no name gives no badge; the raw hostname is never used as the badge (it can carry the owner's name, the same rule as
the hosts section). The adapter shows a host only when it is a host slug (`/^[a-z][a-z0-9-]{0,31}$/`), never an IP,
a path or a mixed-case hostname. No new scope: the tree stays under `office:read`.

The tree also carries each group's raw `device` (a short hostname such as `<owner>-macbook-pro`). The adapter no
longer forwards it: `agents.list` has no `office.device` (review fix round 1, 2026-11-25; `rooms.test.mjs`,
`bridge.test.mjs`). Only `host`, the matched known name, reaches the browser. The tree itself still carries `device`
under `office:read` (the bridge's token only); dropping it from `_TREE_GROUP_KEYS` is B5.

### A.5 Studio's server is read-only (`claw3d/office-mode.patch`, `claw3d/office-mode.cjs`)

Review of this branch (P1, 2026-11-25): hiding controls did not make Studio read-only. Anyone holding the Studio cookie
could still call Studio's own API directly: `PUT /api/studio` (rewrite the settings file the gateway proxy takes its
upstream from, or turn on a remote office with its own gateway and token), `POST /api/office/remote-message` and
`/remote-handoff` (connect straight to a configured remote gateway with `NodeGatewayClient`, outside the proxy and its
`UPSTREAM_ALLOWLIST`), `POST /api/runtime/custom` (forwards requests to allowed hosts, any localhost port included), the
voice routes, and a dozen routes that write Studio's local state.

"Office mode" is a check in Studio's custom server (`server/index.js`), after the login form and the stock access gate
(an unauthenticated request still gets the gate's 401), before Next. It lets through only a request whose method and
exact raw path (nothing decoded or normalised) are on its list, and answers 403 to everything else, unknown routes
included. Upgrades: only `/api/gateway/ws`, before Next's upgrade handler. It has no switch: this build serves only
the bridge. The list was taken by loading the Office in headless Chrome against a local bridge and recording every
request (2026-11-25):

| Route | Methods | Why |
|---|---|---|
| `/cortados-login` | GET, HEAD, POST | the login form (answered before the check; listed so the order cannot lock anyone out) |
| `/` | GET, HEAD | the start page; it only redirects to `/office` |
| `/office` | GET, HEAD | the Office page |
| `/favicon.ico` | GET, HEAD | the tab icon |
| `/_next/static/…` | GET, HEAD | the built JavaScript, CSS and media (safe characters only, no `..`) |
| `/office-assets/…` | GET, HEAD | the 3D models and backgrounds (same rule) |
| `/api/health` | GET, HEAD | liveness for deploy-air.sh |
| `/api/studio` | GET, HEAD | settings as the page boots; sanitized upstream, the gateway token is never in it |
| `/api/task-store` | GET, HEAD | the Office polls the task board (Studio's own local file; no route can create or change a record now, though this GET loader may create its own empty directory if absent) |
| `/api/office/standup/config` | GET, HEAD | read while the Office hydrates |
| `/api/office/standup/meeting` | GET, HEAD | read while the Office hydrates |

The task-store and standup GET loaders may create their own empty directory if it is absent; they never create or
change a record.
| `/api/gateway/ws` (upgrade) | WebSocket | Studio's gateway proxy, still behind the access gate's cookie check |

Everything else is 403: every other method on those paths (`PUT /api/studio` included: settings are pinned), every
other API route (the remote office, runtime, voice, upload, layout, publish, standup writes, task-store writes,
agent-state, skills; and the GETs the Office does not use, such as `path-suggestions`, `gateway/media`,
`browser-preview`, `github`, `presence`), the other pages (`/agents`, `/office/builder`, `/spotify/callback`), Next's
image optimizer and server actions, and any other upgrade (the dev server's HMR socket included).

The page itself saves UI state with `PUT /api/studio` and awaited that save before connecting, so a refused save
stopped the Office from connecting. The patch's second hunk (`src/lib/gateway/GatewayClient.ts`, one line) lets
`connect()` go on when the save fails; the upstream comes from the server's own settings, never from that save. The
refused saves show as console errors in the browser; nothing else changes.

The upstream cannot be chosen by the browser: the proxy reads it only from `<state dir>/claw3d/settings.json` (or
`openclaw.json` in the same state dir, or the default), never from the connect frame or the URL; the three routes
that write that file (`PUT /api/studio`, `PUT /api/office/standup/config`, `POST /api/office/standup/run`) are
refused; `UPSTREAM_ALLOWLIST=localhost` still applies; and proxy-auth.patch still replaces the browser's credentials
with the configured token. Tests: `office-mode.test.mjs` (the list against every route Claw3D 51337f7 serves, read
from the checkout when `CORTADOS_CLAW3D_CHECKOUT` is set; a real server wired as the patch wires it; the patch).

## B. Design (not built)

### B.1 Orchestrator identifier

Today an orchestrator's name is its project (`CortadOS-Router`), so two sessions in one project are two rooms with the
same name, and the Grok Bot and "Unattributed" are fixed labels.

| Option | What it looks like | For | Against |
|---|---|---|---|
| **(a) Coffee codename from the session tag** | `Ristretto`, `Lungo`, `Macchiato`, `Cortado`, `Flat White`, `Affogato`, … chosen as `NAMES[hash(session tag) mod N]` with N ≈ 64 | distinct, stable for the session's life, personal, on-brand, needs no input; the session tag is already in the tree | two live sessions can collide (64 names, 8 rooms: about 36 % chance of at least one collision among 8). Fix: the adapter picks the next free name in the list when the hashed one is taken by another live room, sticky while that room lives (the allocator's room stickiness already does this for desks) |
| (b) project + host + short id | `CortadOS-Router @ Pro · a3f9` | says exactly what it is | not memorable; the project is already shown, the host now has its badge |
| (c) Owner-chosen names | a `name` field the session sends in its heartbeat (`/office name Lungo` in the mod) | fully personal | needs input every session; most sessions would stay unnamed |

**Recommendation: (a), with (c) as an override later.** The codename is the room's name and the orchestrator's
nameplate first line (`Lungo`); the second line becomes `CortadOS-Router · claude-opus-5 · Pro` (project, model,
host). Children are tied to it three ways, all doable in the adapter plus the existing patches:
1. a child's nameplate subtitle ends with the codename (`claude-sonnet-5 · Air · Lungo`), or its name line is prefixed
   (`Lungo › Build the tree`). The prefix is the clearer one for a 2-line plate;
2. the room colour (B.2) is shared by the orchestrator and all its people;
3. the existing report-back walk (E8) already sends a finished task to its orchestrator's desk.

The list and the hash live in the adapter (no provider change). The Grok Bot keeps its fixed name; the unattributed and
"No orchestrator" rooms keep theirs.

### B.2 More personalisation

All of these fit in the adapter and the existing patches; none needs a new Claw3D feature.

| Idea | How | Cost |
|---|---|---|
| **Room colour per orchestrator** | a colour from the codename (8 well-separated colours, one per room). The rooms patch already builds each room's walls and floor (`buildCortadosRoomsLayout`); give each room a floor tint or a coloured wall strip. People carry it in a colour slot the family does not use: the voxel profile's `bottomColor` (trousers) or `shoesColor` (the family owns `topColor`), which the avatar patch already passes through | small: `cortadosRooms.ts` + adapter |
| **Room sign** | a `Text` plate on the room's back wall with the codename and host (`LUNGO · PRO`). Claw3D draws 3D text already (`drei` `Text`, used by the machines and nameplates); one sign per room, read from the room's head-desk agent | small: ~20 lines in the rooms patch |
| **Avatar per model family** | built: suit and hoodie colours per family (claude, codex, grok, local, ollama, unknown) | done |
| **Avatar detail per host** | one accessory per host from the voxel profile's own set (`accessories`: `hatStyle`, `headset`, `glasses`, `backpack`), e.g. Air: a cap, Pro: a headset, Studio: glasses | small: `sprites.mjs` profiles |
| **Desk props per role** | the head desk gets a coffee cup (on-brand) and a second monitor; a report desk a clipboard | small: rooms layout items using existing furniture types |
| Mood/status | stock already shows working / idle / error dots and a mood emoji | done |

Recommendation: room colour + room sign + per-host accessory first (each one small, all visible at a glance).

### B.3 Server room

A room (the 9th, or a fixed alcove at the floor's end) showing the router itself: S1 and the machines.

**What exists today** (all already in `GET /v1/cortados/live`, scope `live:read`):

| Wanted | Live view field (`live.py`) | Exists? |
|---|---|---|
| S1 up / down | `s1.status` (`up`/`down`/`unknown`), `s1.available`, `s1.http_status`, `s1.reason` | yes (the provider's probe) |
| S1 probe latency | `s1.latency_ms` (one probe) | yes |
| Latency p50 / p90 | `s1.routing.windows[]` for `5m` and `60m`: `s1_ms_p50`, `s1_ms_p90`, `calls`, `degraded`, `degraded_pct`, `degraded_wait_ms_p50` | yes |
| Degraded share | the same `degraded_pct`, with `s1.routing.status` `warn` over `warn_above_pct` | yes |
| Current model / adapter | the Studio heartbeat's `s1.model` (the served model's name) | yes, name only; the adapter version is in that name when the scorer reports it |
| Training state | heartbeat `s1.flags` (`no-promote`, `no-train-tonight`, `nightly-stop`, `sweep-requested`), `s1.pending` (a candidate awaiting promotion, by reason slug), `s1.guard` (`running`/`paused`/`idle`/`stopped_for_day`, its reason, p50/p90 against the budget, `until`); the heartbeat's `jobs[]` (`s1-scorer`, `s1-continuous` with `summary` and `progress_pct`) | yes |
| Queue | none for S1 itself (it scores inline, one call per routed turn); the router's own queue is the child tasks' `overview.queued_tasks` and `working_conversations` | partly: queued child tasks yes, an S1 queue does not exist |
| Per-host machines | `hosts.items[]`: `name`, `roles` (provider, default exec host, exec host), `heartbeat_state`, `hardware` (`cpu_busy_pct`, `gpu_busy_pct`, `mem_available_bytes`/`mem_total_bytes`, `load_1m`, `disk_free_pct`, `chip`) | yes, for hosts that send a heartbeat (`live_heartbeat.py`) |

**Scope.** The bridge's client holds `office:read` only (E3, decided 2026-11-24: the bridge never gets `live:read`, which
also opens subscriptions, routed-call rows and the work split).

| Option | What | For | Against |
|---|---|---|---|
| **(a) A small `server_room` block in the tree, under `office:read`** | `live.office_tree` adds an allowlisted block built from the same snapshot: `s1: {status, latency_ms, windows: [{window, calls, degraded_pct, s1_ms_p50, s1_ms_p90}], model, guard_state, flags, training: {state, progress_pct}}` and `hosts: [{name, roles, heartbeat_state, cpu_busy_pct, gpu_busy_pct, mem_used_pct, load_1m}]`, every field picked by name like `_TREE_*_KEYS` | one token, one poll, nothing else of `live:read` reaches the Air's Studio user; the same review rule as the tree (an allowlist, a test that the keys are exact) | the tree grows a non-tree block (or a sibling path `GET /v1/cortados/office/server-room` under the same scope) |
| (b) Grant the bridge `live:read` | the bridge reads `/v1/cortados/live` | no provider change | reverses E3: subscriptions, routed-call rows, work split and every host field reach a user that serves a browser app |
| (c) A second token with a new narrow scope `office:server` | a new `GET /v1/cortados/office/server-room` | separable revocation | one more secret on the Air for little gain over (a) |

**Recommendation: (a), as a sibling path `GET /v1/cortados/office/server-room` added to the `office:read` scope**
(one token, no tree shape change, its own allowlist and memo). The bridge maps it to people and props:
- **S1** as a figure at a "rack" desk in the server room: subtitle `S1 · up · p50 180 ms`, status `working` while
  `s1-continuous` trains, `error` when `s1.status` is `down` or `routing.status` is `warn`; its name the served model.
- **Each host as a rack** (`server_rack` is a stock Claw3D furniture type: `core/geometry.ts`, drawn in `RetroOffice3D.tsx`), labelled `AIR`,
  `PRO`, `STUDIO` with a sign as in B.2, and a load bar from `cpu_busy_pct` (or a "stale" grey when the heartbeat is
  stale). Racks are furniture, not agents, so this needs ~30 more lines in the rooms patch to draw them from a small
  array the bridge sends; the alternative without a patch is one "machine" figure per host, subtitle `cpu 42 % · mem
  61 %`.
- No subscription, account, token count or routed-call row is in the block.

### B.4 Steering a running process

**What exists today.**
- A child task is a headless Claude Code run started by `cortados_router/child_tasks.py`: `claude -p
  --output-format stream-json --verbose ...`, its prompt on stdin from `prompt.txt` (a file, closed after the
  read). Its stdin then has nothing more to read, so **there is no mid-run input path**.
- `cancel_task` exists (owner's MCP tool): SIGTERM to the runner, which stops claude's process group (SIGKILL after
  `KILL_GRACE_S`).
- Hooks: every child gets a settings file (`session.py`) with a PreToolUse hook (`boundary_hook.py`) for local-routed
  turns. Claude Code also runs `PostToolUse` and `UserPromptSubmit` hooks, whose output can add context to the model's
  next turn. This is the one place a running child already consults something outside itself at each tool boundary.
- Main sessions (orchestrators) are the owner's own interactive Claude Code or Codex sessions: the owner can type to
  them directly.
- The Office path is read-only by design: the browser talks to Studio (user `_cortados-claw3d`), Studio's proxy to the
  bridge with a bridge secret that opens only the read-only bridge, the bridge to the provider with an `office:read`
  token that opens two GETs. Studio's own API is held read-only by office mode (A.5): only the Office's GET routes
  and the gateway socket are served, every write route (settings, remote office, runtime, voice, uploads, layout,
  standup, task board) is refused with 403, and the proxy's upstream is pinned to the bridge. Nothing a browser
  sends on that path can change Studio's records or reach an agent (the task-store and standup GET loaders may
  create their own empty directory if absent, but never create or change a record).

**Options.**

| Option | How | Security |
|---|---|---|
| **(a) Steer queue read at tool boundaries** | a new owner-only MCP tool `steer_task(task_id, note)` on `cortados-route` (the same channel as `cancel_task`, owner token) appends the note to `<task dir>/steer.jsonl` on the task's host. A PostToolUse hook installed in every child (next to `boundary_hook.py`) reads new lines after each tool call and returns them as additional context ("Message from the owner: …"); a delivered line is marked. The Office shows a "steer pending/delivered" marker as metadata only | no browser write path. The note comes only from a session that holds the owner token. Delivery is bounded (a size cap, plain text, marked as the owner's). It reaches the model at the next tool boundary, not instantly; a child stuck in one long tool call waits |
| (b) Stream-json input | start children with `--input-format stream-json` and keep the runner's stdin open; the runner forwards owner notes as user messages | the same owner-only channel; but it changes how every child is launched and how the runner lives (remote hosts over ssh, the LaunchAgent path for connectors), and a closed or broken pipe becomes a new failure mode |
| (c) Cancel + restart with a note | `cancel_task`, then `start_task` with the original task plus "Owner's note: …", on the same branch (its worktree is held when unpushed) | uses only what exists; loses the child's context and the in-flight tool call; fine for a wrong direction, poor for a small nudge |
| (d) Steer from the Office (Claw3D chat → bridge → provider) | the bridge would accept `chat.send` for a task's agent and forward it with a new `office:steer` scope | **not recommended.** It makes a write reachable from the browser: anyone with the Studio cookie (every tailnet device that logged in once) could put instructions into an autonomous agent with shell and repo access: a prompt-injection channel. The bridge and Studio users would hold a write credential, against the owner's decision that the office token is never exposed to Claw3D, and Studio is a large npm application (the reason it runs as its own role user). It would also need the chat composer this branch hides |

**Recommendation: (a), owner-only, outside the Office.** Steering is an owner action from the owner's own session
(the same trust as `start_task` and `cancel_task` today), delivered at tool boundaries by a hook. The Office stays
read-only and may later *show* that a note is pending or delivered (a metadata flag in the tree). Use (c) for a
change of direction. Build steps for (a), when decided: the MCP tool and its remote op in `child_tasks.py`
(`--remote`), the hook script and its entry in the child's settings (`session.py`), a size cap and a test that the
hook never runs a note as a command, and the tree flag.

## C. Office round 2 (built 2026-11-27, branch `office-round-2`, not deployed)

The owner answered B1-B4 and B6 on 2026-11-27. The office is read-only and stays so: steering (B4) is built separately
as an orchestrator-only tool, and the Office gets **no** write path. What was built, per decision:

### C.1 Orchestrator name = its Claude Code session name (B1)

**Where the name comes from.** Claude Code keeps a session's name in the session's own transcript
(`$.session.info().transcriptPath`, `~/.claude/projects/<project>/<session id>.jsonl`) as one-line records: a
`custom-title` record (`customTitle`) each time the owner runs `/rename`, and an `ai-title` record with the short title
Claude Code gives the session itself (what `/resume` and the terminal title show). The routed-subagents mod
(`integrations/claude-mod/cortados-routed-subagents/hooks/register.ts`, `readOfficeSessionName`) reads only those
records, with a fixed-string `grep -F -e '"type":"custom-title"' -e '"type":"ai-title"'` over that file at most once a
minute, parses each matched line and checks its top-level `type` (inside a message the same words are JSON-escaped,
`\"type\":\"…\"`, so the pattern cannot match message text). The newest custom title wins, else the newest AI title.
It is cut to 64 printable ASCII characters and sent as the session heartbeat's new optional field `name`
(`POST /v1/cortados/office/heartbeat`). Nothing else of the transcript is read or sent; the mod sends no name for a
session that has none yet. The Claude Code mod API has no session-name accessor (`$.session` offers id, model, root,
info…, checked against the 2.1.332 types), which is why the transcript is the source.

**Provider.** `office.validate_heartbeat` accepts `name` (rule `office.SESSION_NAME_RE`: printable ASCII, 1-64 chars, no
address-looking string; anything else refuses the heartbeat with 400 `name`). The floor's orchestrator carries it
(`_orchestrator`), the conversations section's orchestrator item carries it (both the heartbeat-only and the routed
main-loop case), and the tree's session group publishes it as `name` (`live._TREE_GROUP_KEYS`). A bot or unattributed
group has `name: null`.

**Office.** The room's name, door sign and the orchestrator's nameplate first line are the session name; unknown (an
older mod, a session not named yet) they fall back to the current identifier, the project (`CortadOS-Router`), then
"Orchestrator". Named, the plate's second line keeps the project: `CortadOS-Router · claude-opus-5 · Pro`. Every child
(task, a task's helper, the orchestrator's routed helper) is named `<orchestrator name> › <task title>`
(`adapter.CHILD_SEPARATOR`), the orchestrator part cut to 24 characters and the whole line to 64. The Grok Bot keeps its
fixed label (and its tasks read `Grok Bot › …`); the unattributed and "No orchestrator" rooms have no prefix.

### C.2 Personal touches (B2)

- **Room colour.** Seven colours (`adapter.ROOM_COLORS`, the same list as `cortadosRooms.ts CORTADOS_ROOM_COLORS`, a test
  keeps them equal), one per room by FNV-1a of the room's key (session tag or named origin), so a session keeps its
  colour across polls, restarts and room moves. The rooms patch tints the room's floor and its door sign's stripe with
  it; the room's people wear it as their trousers (the voxel profile's `bottomColor`; the family keeps the top). The
  colours are Claw3D's own clothing list (`src/lib/avatars/profile.ts AGENT_AVATAR_CLOTHING_COLOR_OPTIONS`: sky, mint,
  amber, rose, violet, cream, graphite), because `normalizeAgentAvatarProfile` replaces any other value with a random
  one. The eighth, slate (`#64748b`), is the unattributed, "No orchestrator" and server rooms' grey.
- **Door sign.** A plate on each room's back wall with the room's name (the orchestrator's name, else the room's
  label), drawn by the rooms patch from the head-desk person's name.
- **Avatars by model family** (Claude / GPT / local, plus Grok, Ollama, unknown): unchanged, already built.
- **Host badge.** The existing `host` field, unchanged: the nameplate subtitle ends with `· Air` / `· Pro` / `· Studio`
  for every person whose host is known (A.4).

### C.3 Server room (B3)

**Endpoint: `GET /v1/cortados/office/servers`**, under the existing `office:read` scope (`clients.SCOPES`; exact path,
GET only, no query: `?…` is a 400), built by `live.office_servers` from the same memoised collect as the tree. Exact
fields:

```
{ "version": 1, "generated_at": <epoch s>,
  "hosts": { "as_of": <epoch s|null>, "status": "ok|partial|error", "errors": [{"source","error"}],
             "truncated": <bool>, "items": [ { "name": "air|pro|studio|pc|<host slug>",
                                               "state": "up|stale|down|unknown", "as_of": <epoch s|null>,
                                               "provider": <bool> } ] },          // at most 8, air/studio/pro first
  "s1": { "status": "up|down|unknown",        // the provider's own probe
          "health": "up|down|unknown|null",   // the S1 host's own heartbeat
          "model": "<served model id, version in the name>|null", "as_of": <epoch s|null>,
          "probe_ms", "p50_ms", "p90_ms",     // probe latency; S1 scoring time p50/p90 over the last hour
          "calls_1h", "scored_1h",            // routed calls in the last hour; of those, scored by S1 (not degraded)
          "degraded_pct_1h": <number|null>, "degraded": <bool>,   // degraded = the routing check warns (live.s1_routing)
          "errors": [{"source","error"}] } }
```

A host is `up` when it is the provider host (it is answering) or its heartbeat is fresh, `down` when its heartbeat went
stale, `unknown` when it never sent one since the provider started. Numbers and fixed words only: no hardware figure,
job, flag, account, token, routed-call row or request text (`tests/test_office_tree.py ServerRoom`).

**Bridge.** `adapter.POLL_PATHS` is the bridge's whole list of GETs (office, tree, servers); `pollServers` requests
exactly `/v1/cortados/office/servers` beside the configured office URL with the same rules (no redirect, HTTPS off
loopback, the office:read token from its file). The tree still decides the roster: a failed servers poll keeps the last
good server room and says so on stderr; a provider without the endpoint draws every orchestrator room and an empty
server room. `mapServers` validates the view whole before it replaces the last one.

**Office mode.** Studio's server never serves the provider's path: the browser only ever talks to Studio, and the
server room reaches it through the gateway socket as data (below). As the owner asked, `office-mode.cjs` lists
`/v1/cortados/office/servers` among its exact entries as **GET only** (HEAD and every other method 403), with that
reason; a GET there reaches Next, which has no such page (404), and the office:read token is never in Studio. The list
test pins the entry and its one method.

**The room.** A fixed room of the floor (see C.5 for where). Hosts and S1 are not people (office round 2 fix, owner
2026-11-27: as figures they walked the floor and filled the roster). The bridge sends the room as data,
`snapshot.cortadosServers` in its hello and `payload.servers` in every `office.snapshot` event: `racks`, one per host
(`{host, name "Air", state up|stale|down|unknown, note}`, note `up · provider`, `up · 8 s ago`, `stale · seen 18 min ago`,
`down · seen 30 min ago` or `no heartbeat`; `stale` for the first 10 minutes past the heartbeat's freshness limit, B9), and `s1` (`{name "S1 · <served model>", state, note "up · p50 181 ms · p90 420 ms · 90 scored/h"
(· degraded when the routing check warns), alert (down or degraded), working (scored in the last hour)}`). One line in
Studio's `GatewayBrowserClient.handleMessage` hands every frame to `cortadosRooms.ts cortadosNoteGatewayFrame`, which
keeps only those two frames' room, validated field by field (a malformed one is dropped, the last good one stays).
`objects/cortadosSigns.tsx` draws a rack per host along the room's open side, facing in, with a status strip and a
sign (name over note, green / amber / red / grey), and the S1 console in front of them with its sign; no desks or chairs. A
bot origin named `servers` is refused, so it can never be an orchestrator's room.

### C.4 Event console (B6): still [ESCALATE], hidden by default

No owner answer. Built default: the Agent Event Console stays hidden unless the Studio build sets
`NEXT_PUBLIC_CORTADOS_DEBUG=1` (Next inlines it at build time; `deploy-air.sh` does not set it, so the Air's Studio never
shows it). Onboarding stays hidden unconditionally.

## [ESCALATE] for the owner

Decided 2026-11-27 and built in C: B1 (session name, prefix `<name> › <title>`), B2 (colour, door sign, family avatar,
host badge), B3 (narrow `office:read` endpoint), B4 (steering outside the Office; the Office stays read-only).
Decided by the owner 2026-11-28, all as built except B9, which changed:

- **B5. Raw `device` in the tree.** Kept in the provider's tree, never forwarded to the browser (the adapter drops it).
- **B6. Event console.** Hidden unless the Studio build sets `NEXT_PUBLIC_CORTADOS_DEBUG=1` (C.4).
- **B7. Claude Code's own title as the name.** When the owner never ran `/rename`, the room takes Claude Code's own short
  session title (`ai-title`).
- **B8. Where the server room sits.** The floor's 8th room slot (bottom right), leaving 7 orchestrator rooms (C.5); an
  8th orchestrator waits unseated.
- **B9. A stopped heartbeat is amber "stale", then red "down".** A host whose heartbeat passed its freshness limit
  (`ttl_s`) shows amber `stale · seen N min ago` for its first 10 minutes past it (`live.SERVER_STALE_WINDOW_S`: it may
  only have missed a post or two), then red `down · seen N min ago`. Computed by the provider in `live._server_host`
  (`GET /v1/cortados/office/servers`); the adapter writes the note and `cortadosRooms.ts CORTADOS_SERVER_STATE_COLOR`
  colours the rack sign (green up, amber stale, red down, grey unknown).
- **B10. Shared room colours.** Accepted: seven colours by hash, so two live rooms share one about 1 time in 7 per pair;
  their door signs still differ.

### Earlier list (2026-11-25), kept for the record

- **B1. Orchestrator names.** Coffee codenames from the session tag (recommended; next free name on a collision,
  sticky while the room lives), project + host + short id, or owner-chosen names? And: tie children to their
  orchestrator by a name prefix (`Lungo › Build the tree`, recommended) or a subtitle suffix?
- **B2. Personalisation.** Build room colour + room sign + per-host accessory (recommended), and which colours / which
  accessory per host?
- **B3. Server room data.** A new `GET /v1/cortados/office/server-room` under `office:read`, allowlisted (recommended),
  or grant the bridge `live:read` (reverses E3), or a separate `office:server` token? And: racks as furniture (a rooms
  patch extension) or one "machine" person per host (no Claw3D change)?
- **B4. Steering.** An owner-only `steer_task` MCP tool delivered by a PostToolUse hook at tool boundaries
  (recommended), stream-json input, or cancel + restart only? Confirm that no steering path will ever start from the
  Office (browser).
- **B5. Raw `device` in the tree.** The adapter no longer forwards it to the browser (A.4, review fix round 1). The
  provider's tree still publishes each group's raw short hostname (`device`) to the bridge under `office:read`: drop
  it from `_TREE_GROUP_KEYS` too, now that `host` carries the known name?
- **B6. Onboarding and event console.** Both are hidden unconditionally. Keep them hidden, or bring the event console
  back behind an explicit `DEBUG=1` on the Studio launcher (it shows only the bridge's metadata events)?
