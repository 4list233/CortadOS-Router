# Office floors per orchestrator: design (issue #77)

Status: **design only**. No code in the router, the adapter or Claw3D changes with this document, and nothing
is deployed. Claw3D evidence comes from a read-only clone of `github.com/iamlukethedev/Claw3D` at
`51337f7f250459af61a7cf92b7b0015d59faed5c` (the commit `deploy-air.sh` pins). Paths below are relative to that
clone (`claw3d:`) or to this repository.

## 1. Short answer: do we fork Claw3D?

**No.** Claw3D's floors are a fixed, compiled-in list of runtime *types*, and each floor is reached by
reconnecting Studio's one server-wide gateway, so floor-per-orchestrator would mean rewriting its floor model.
That is a fork of the part upstream is changing right now. Room-per-orchestrator on one floor needs one more
small patch, of the same kind as the two we already carry. If the owner needs real floors, building our own
lightweight view costs less to maintain than a fork (section 5).

## 2. Target picture

- **One building, one CortadOS floor** in Claw3D. On it, **one room (a pod of desks) per orchestrating
  session**: each main Claude Code / Codex session that delegates, plus the bot gateway (`grokbot`) and an
  "unattributed" pod for tasks with no origin. Pods with activity come first, using the order the provider's tree
  already sorts by (`live.py:1353`).
- In each pod the **orchestrator** wears a suit and sits at the head desk. Its **child tasks** (`start_task`) are its
  team, at the next desks in depth-first tree order, so a nested child sits next to its parent. A task's **routed
  sub-sessions** sit or stand next to it (or appear as a count, see [ESCALATE] E4).
- **Family at a glance**: the figure's suit or hoodie colour and the 2D sprite show claude / codex / grok / ollama /
  local (MLX) / unknown. Antigravity/Gemini comes later.
- **On the figure** (Claw3D's stock nameplate): the name line is the task title (or the project for an
  orchestrator) and the subtitle line is the model. The hover tooltip shows the name and working/idle/error. A click opens
  Claw3D's agent panel with the model from `sessions.list`.
- Read-only throughout, as today: the bridge refuses every mutating method.
- If true floors are needed (E1): a floor per orchestrator in our own 2D view (option c), not in Claw3D.

## 3. Findings

### 3.1 How Claw3D defines and feeds floors

| Finding | Evidence (`claw3d:`) |
|---|---|
| Floors are a **compiled TypeScript constant**: a closed `FloorId` union (`lobby`, `openclaw-ground`, `hermes-first`, `local-runtime`, `claw3d-runtime`, `custom-second`, `training`, `traders-floor`, `campus`) and a fixed `OFFICE_FLOORS` array. A gateway cannot add, name or remove a floor. | `src/lib/office/floors.ts:1-8` (`FloorProvider`), `:11-20` (`FloorId`), `:36-136` (`OFFICE_FLOORS`) |
| A runtime floor is **one per adapter type**. The nav shows the lobby, non-runtime floors, and only the runtime floor whose `provider` equals the active adapter type. | `src/lib/office/floors.ts:174-190` (`listAvailableFloorsForAdapter`) |
| An unknown hello `adapterType` (ours is `cortados-office`) becomes `openclaw`. That is why the live directory shows "Lobby (DEMO)" and "OpenClaw Floor", and our whole office lives on `openclaw-ground`. | `src/lib/studio/settings.ts:933-945` (`normalizeGatewayAdapterType`, fallback `"openclaw"`) |
| **Switching floors reconnects the gateway.** `handleSelectFloor` takes the floor's saved `gatewayUrl` (else the adapter profile's URL), sets adapter type, URL and token, and queues a runtime switch. A runtime floor with no URL falls back to the lobby. | `src/features/office/screens/OfficeScreen.tsx:1407-1465`, URL choice `:1440-1442`, lobby fallback `:1451-1455`, switch effect `:1200-1240` |
| **Studio's proxy has exactly one upstream, server-wide**: it reads `gateway.url`/`token`/`adapterType` from the Studio settings file on every browser connection. Several floors means several upstreams taking turns on that one setting. They are never shown at the same time. | `server/index.js:92-95`, `server/studio-settings.js:85-110`, `server/gateway-proxy.js:290-291` |
| The per-floor roster is a cache of the **one connected** gateway's agents, rebuilt when that floor is active. | `src/lib/office/floorRoster.ts:39-56`, `:71-77`; `OfficeScreen.tsx:1514-1528` |
| The 3D scene remounts per floor (`key` and `storageNamespace` = floor id) and uses the `lobby` preset only for the lobby. | `OfficeScreen.tsx:4712-4725` |
| Upstream's multi-floor note is explicit: "One runtime per floor", "Each floor has exactly one runtime binding at a time". Its status section marks the registry, per-floor state, roster cache and switcher as done, and cross-floor work as deferred. This is the area upstream is still changing. | `docs/office_sys/multi-floor-runtime-architecture.md` ("Core Principles", "Runtime Rules", "Current Implementation Status") |
| Teams and hierarchy (`reportsTo`, teams) exist only as a spec. No source file implements them. | `docs/office_sys/hierarchy-and-teams-spec.md`; no `reportsTo`/`teamId`/`parentAgentId` anywhere under `src/` |

**Answers.**
- *Can one gateway drive several floors?* No. A gateway feeds the single runtime floor of its adapter type.
- *One bridge per orchestrator?* No. The number of floors is fixed at build time and keyed by adapter type (at most
  one each for openclaw/hermes/local/claw3d/custom). Only one upstream is connected at a time, and switching rewrites
  Studio's server-side gateway setting. Orchestrators come and go every hour, so per-orchestrator bridges would
  need a dynamic floor registry: the fork in option (b).
- *Several floors from one bridge?* Not in stock. The hello carries no floor list, and nothing reads one.

### 3.2 Layout builder, rooms and desks

| Finding | Evidence (`claw3d:`) |
|---|---|
| A layout is a flat list of `FurnitureItem {_uid, type, x, y, w, h, r, …}` on an 1800×720 canvas. "Rooms" are only walls placed in it. There is no room or zone entity with a name or members. | `src/features/retro-office/core/types.ts:67-80`; `src/lib/office/layoutSnapshot.ts:3-9`; `core/district.ts:12-20` |
| The named places are **functional** (desk, server room, meeting room, gym, QA lab, booths) and drive animations. They are not groups. | `src/lib/office/places.ts:1-10` |
| The default office has **8 `desk_cubicle`s**. | `core/furnitureDefaults.ts` (8 `desk_cubicle` seeds) |
| The **layout lives in the browser's localStorage** per floor namespace. The initial layout is the saved one, else `materializeDefaults(preset)`. | `core/persistence.ts:31-41`; `RetroOffice3D.tsx:2216-2230`, `:2501-2504` |
| `/api/office/layout` is **not** the live layout. GET returns a server-side snapshot keyed by gateway URL, or a remote office's layout. PUT stores a snapshot for publishing. Nothing loads it into the local scene, so it cannot steer our floor. | `src/app/api/office/layout/route.ts:56-98`, `:100-116`; only caller `RetroOffice3D.tsx:3166` (publish) and `useRemoteOfficeLayout.ts:30` (remote) |
| **Desk assignment is manual and user-owned**: `deskAssignments[gatewayUrl][deskUid] = agentId` in Studio settings, written by the desk editor. An agent with no assignment gets **no desk** and wanders. | `OfficeScreen.tsx:1530-1563` (write), `:1640-1660` (load); `settings.ts:254`, `:275`; `RetroOffice3D.tsx:2767-2780` (index from assignments only), `:990-999` |
| A gateway could in theory PATCH `deskAssignments` through Studio's settings API. But desk `_uid`s exist only in the browser's localStorage layout, and the assignments belong to the user. That approach is not sound. | as above |
| A **second zone** exists (the "remote office", agents with the `remote:` id prefix, one zone 1020 px below). There is exactly one, it is fed by a presence URL or a second OpenClaw gateway, and it holds no groups. | `core/district.ts:29-34`, `:50`; `OfficeScreen.tsx:591-605` |
| **Nameplate**: every figure shows `name` and a `subtitle`. The subtitle is the agent's `role` from `agents.list`, carried through stock hydration. So title and model on the figure need **no patch**: send `role` = the model. | `agentFleetHydrationDerivation.ts:272`; `OfficeScreen.tsx:567-589`; `objects/agents.tsx:673-674` and the nameplate block after `:1100` |
| **Hover tooltip**: name, a decorative `item` and the status. Model and title are not in it unless a patch adds them. | `RetroOffice3D.tsx:6138-6195`; hover hooks `objects/agents.tsx:682-686` |

**Answer.** Neither the stock protocol nor the layout API can assign desks or rooms per group. The smallest way is a
Claw3D patch that, **only for agents with no manual assignment**, assigns desks by a group derived from the agent
id. Our ids already carry the session (`office-<session>-…`, `adapter.mjs:124-125`), so the patch needs no new
field threaded through hydration.

### 3.3 Our side today

| Finding | Evidence |
|---|---|
| The adapter flattens every floor into one roster. The floor is only in `office.floor`, an extension stock ignores. | `integrations/office-claw3d/adapter.mjs:110-165` (esp. `:141-145`), PROTOCOL.md "Gaps (open)" |
| `role` is sent as `orchestrator`/`worker`, so the stock nameplate subtitle shows that word, not the model. | `adapter.mjs:135` (`agents.push({ id, name, role, …`) |
| The office view has no `parent_task_id` and no sub-sessions. A child task is one worker merged with its run's traffic. Caps: 64 floors, 32 workers per floor, 64 in the lobby. | `cortados_router/provider/office.py:294-385`, `:106-111` |
| The provider already builds the wanted grouping. The `orchestrators` tree runs session → tasks (nested by `parent_task_id`, depth ≤ 4) → sub-sessions (≤ 8, main loop first), plus the orchestrator's own routed subagents (≤ 8). Caps: 32 groups, 20 top tasks per group. | `cortados_router/provider/live.py:158-162`, `:1210-1350`, `_sub_node` `:1119`, `_subs_by_run` `:1142`, `_task_node` `:1186` |
| The bridge's scope `office:read` covers only `GET /v1/cortados/office`. The tree is on `/v1/cortados/live` (`live:read`), together with subscriptions, hosts, S1 and routing rows the Office does not need. `/live/session` needs a tag it already knows and cannot list sessions. | `cortados_router/provider/clients.py:54-60`; `server.py:2436`, `:2447`, `:2462` |
| Families: `provider_of` maps only `claude-cli`, `codex-cli` and `local-*`. `PROVIDERS = (claude, codex, local)`. | `office.py:83`, `:125-135` |
| An orchestrator has no provider: `_orchestrator` returns model/status/tool only. The heartbeat accepts only `session, project, device, model, status, tool`. The adapter guesses the orchestrator's family from the model name. | `office.py:486-500`, `:398-425`; `adapter.mjs:133-134`, `modelFamily` `:96-108` |
| The bot origin is a named constant (`grokbot`), so a bot group's family is known without guessing. | `cortados_router/child_tasks.py:957-959` |

## 4. Options

| | (a) Stock Claw3D + small patches (rooms on one floor) | (b) Maintained fork (floors per orchestrator) | (c) Our own lightweight view |
|---|---|---|---|
| What it gives | One pod per orchestrator, nested team order, family colours, title and model on nameplates. One floor. | Real floors per orchestrator in Claw3D's building directory. | Floors (or tabs) per orchestrator, the tree drawn as rooms with our pixel sprites. 2D, no 3D figures. |
| Claw3D changes | Existing `avatar-profile-hint.patch` (6 lines) and `studio-login.patch` (3 lines), plus **`cortados-rooms.patch`**: about 15 lines of hooks in `RetroOffice3D.tsx` (initial layout, desk index) and one new file `src/features/retro-office/core/cortadosRooms.ts` (~150 lines: pod layout and group → desk allocation). Optional about 5 lines in the tooltip (E5). | Dynamic `FloorId` (string) and a floor list sent by the gateway; `floors.ts`, `floorRoster.ts`, `settings.ts` (typed `officeFloors`/`activeFloorId`), `OfficeFloorNav.tsx`, and `OfficeScreen.tsx`'s switch state machine (`:1080-1240`, `:1407-1528`) changed so a gateway floor filters the roster instead of reconnecting. Estimate 500-900 lines in 6+ files, plus the rooms work of (a). | None. Claw3D can stay as it is or be retired (E2). |
| Our changes | Adapter reads the tree; provider gets a narrow tree endpoint (E3); tests. | Same as (a), plus the bridge's floor protocol. | A static page served by the provider beside `dashboard.html`, behind the dashboard session, reading `/v1/cortados/live` directly (the Live tab already draws the tree, `dashboard.html:454-560`). Estimate 600-900 lines of HTML/JS plus tests. No adapter, bridge, Studio, npm or extra macOS user. |
| Effort | Small to medium | Large | Medium |
| Risk | Low. The new file never conflicts. The hooks sit in a 6.2k-line, actively edited file, but on two narrow seams. Not compiled until a Studio build. | High. It rewrites the area upstream's own multi-floor roadmap is changing (slices 3-7). We would own a Next.js 16 app's dependency updates. | Low. Stdlib server; the tree is already validated metadata. |
| Upkeep on Claw3D upgrades | Re-apply 3 patches; `deploy-air.sh` already runs `git apply --check` per patch (`deploy-air.sh:374`) and fails closed. | Rebase on every upgrade, and conflicts are certain. Upstream: one maintainer, last push 2026-11-04. | None (Claw3D-free). |

**Recommendation: (a).** Room-per-orchestrator on one Claw3D floor, with the adapter reading the provider's tree.
It delivers grouping, nesting, families, model and title for a patch the deploy script already knows how to check. Do
**not** fork. If the owner decides that real floors are required (E1), build (c), not (b). The provider already has the
data and the dashboard already draws it, so (c) is a view, not a platform to maintain.

## 5. Build steps (option a)

Each step is its own branch and PR, built and then reviewed by Codex per the repo rules. Steps 1-2 are provider and
adapter work with fakes only. Steps 3-4 need an npm build of Claw3D in a scratch dir, not on the Air.

**Step 1: a narrow tree endpoint (provider).** `GET /v1/cortados/office/tree` returns only
`live.orchestrators_section(...)` for the fleet, with `tokens` stripped. Add `local_backend` to task and sub-session
nodes and the orchestrator `client` (step 5). Scope: grant `office:read` (E3), so the bridge's token never gains
`live:read`'s subscriptions, hosts and routing rows. Bounded by the tree's existing caps.
*Acceptance:* `python3 -B -m unittest tests.test_live tests.test_office tests.test_provider` (new cases: an `office:read`
token gets 200 on `/office/tree` and 403 on `/live`; the body has no `tokens`, no subscriptions or hosts sections, and
no other session's data under a session filter) and the full suite
`python3 -B -m unittest discover -s tests -t .` passes.

**Step 2: adapter maps the tree (adapter).** `mapTree(tree, office)`: one group per `orchestrators.items[]`
(`origin` = session tag, `grokbot` or `null` → `unattributed`). Ids: `office-<group>-orchestrator`,
`office-<group>-task-<task_id>`, `office-<group>-sub-<task_id>-<ref>`, `office-<group>-routed-<ref>`. These keep
the existing grammar, so the Claw3D patch derives the group from the id. Emit `agents.list` in depth-first order
(orchestrator, then each task followed by its nested tasks and its sub-sessions). Set `name` to the title (the
orchestrator: project or "Orchestrator") and `role` to the model (the stock nameplate subtitle). Add
`office.group`, `office.groupLabel`, `office.parent` and `office.depth` to the extension. Workers from the office view
whose run is in no task (lobby, outcome loop) go to the `unattributed` pod. Poll the tree and the office view one
after the other on one token, and join `status`/`tool`/`local_backend` by run id = task id. If the tree is missing or
invalid, keep the last good roster (as `update()` does today). Never fall back silently to the flat view: report
`tree unavailable` in the fixed diagnostics.
*Acceptance:* `node --test integrations/office-claw3d/*.test.mjs` with new fixtures `fixtures/sample-tree.json`
(two sessions, a nested task, a task with 3 sub-sessions, the bot, an unattributed task). The tests check: ids are
stable across polls; order is depth-first; a child appears after its parent; each group's people share the id prefix;
`role` is the model; no token or path appears in output. `node build-assets.mjs --check` still passes.

**Step 3: `claw3d/cortados-rooms.patch` (Claw3D).** (i) New file `core/cortadosRooms.ts`: a deterministic
`cortados` layout preset with P pods (E6), each walled with a head desk and D desks, inside the 1800×720 canvas
(a taller canvas would need `district.ts` changes, so stay inside it). Plus `cortadosGroupOf(agentId)` (the id
grammar from step 2) and `allocateDesks(agents, deskItems, manual)` → `Record<agentId, deskIndex>`: a manual
assignment wins; then each group takes the next free pod in agents.list order and fills its desks in order; overflow
stands in its pod. (ii) Hooks: `buildInitialFurnitureLayout` uses the cortados preset when no layout is saved
(`RetroOffice3D.tsx:2230`), and `assignedDeskIndexByAgentId` (`:2771-2780`) merges `allocateDesks` for unassigned
agents. Gated on the hello `adapterType === "cortados-office"`, so a stock gateway is untouched.
*Acceptance:* `git apply --check` on a clean 51337f7; `npm ci && npx tsc --noEmit && npm test` in a scratch clone (the
upstream unit test runner) with a new unit test for `allocateDesks`; `deploy-air.sh` lists the patch in
`ADAPTER_FILES` and its apply loop (`:49`, `:374`), and `deploy.test.mjs` covers that.

**Step 4: live check on a scratch Studio (not the Air).** Run the bridge against a fake provider serving
`fixtures/sample-tree.json` and a patched Studio on loopback.
*Acceptance:* a screenshot shows one pod per group, the orchestrator at the head desk, a nested child next to its
parent, family colours, and the nameplate "title / model"; agents.list order survives hydration (verify; if
hydration sorts, the allocator sorts by `office.depth`/parent instead).

**Step 5: orchestrator family (provider and mod).** Order of sources, first that applies, and its source is
published: (1) `origin == grokbot` → `grok` (`bot`). (2) The routed main loop's wire, which the provider observes:
`/v1/messages` → Claude Code, `/v1/responses` → Codex (`wire`). (3) A new optional heartbeat field `client` from a
fixed enum (`claude-code | codex | grok | antigravity`), validated like `status` (`heartbeat`). (4) Otherwise the
model-name guess the adapter does today (`model-guess`). The adapter shows the guess and marks it in
`office.familySource`. Published `provider` stays `null` unless the source is (1) or (2).
*Acceptance:* provider unit tests for each source and for a refused unknown `client`; adapter tests that the sprite
follows `family` and that `familySource` is published.

**Step 6: new families when lanes exist.** `provider_of` gains `grok` (and `antigravity`/`gemini`) when those lanes
land; `sprites.mjs` gains a gemini figure (E7). *Acceptance:* the existing sprite tests extended; `build-assets.mjs
--check`.

**Step 7: docs.** Update PROTOCOL.md "Gaps (open)" (floors → rooms) and README "Appearance".

## 6. [ESCALATE] decisions for the owner

- **E1. Rooms or floors?** Rooms on one Claw3D floor (recommended, option a). For real floors per orchestrator,
  choose between (c) our own 2D view (recommended if floors are a must) and (b) a fork (not recommended).
- **E2. If (c) is chosen, does Claw3D stay?** Keep both (3D office plus our floors view) or retire the Air's
  Claw3D, bridge and `_cortados-office` user.
- **E3. Which token reads the tree?** A new `GET /v1/cortados/office/tree` under `office:read` (recommended: the
  bridge sees only Office metadata), versus giving the bridge's client `live:read` (it would also read subscriptions,
  hosts and routing rows).
- **E4. Sub-sessions: people or a count?** Each routed sub-session as its own figure next to its task (desks fill
  fast: a task may have up to 8), or one figure per task with "model · N subs" in its nameplate.
- **E5. Hover tooltip.** Stock shows name and status only; the model is on the nameplate. Add a ~5-line tooltip patch
  so hover also shows the model and title?
- **E6. Pod size.** How many pods on the floor and desks per pod (e.g. 6 pods × 6 desks), and what happens to
  groups past that: the oldest share an overflow pod, or they are left out with a "+N" sign.
- **E7. Family colour for an orchestrator: harness or model?** Claude Code routed to a GPT model is a Claude Code
  session running a Codex-family model. Should the suit follow the harness (Claude Code / Codex / Grok) or the model?
  Also choose colours for Antigravity/Gemini.
- **E8. Which groups appear?** Include the "unattributed" pod, and finished tasks within the tree's 24 h window, or
  only running and waiting work?
