# Claw3D gateway protocol research (2026-11-07, extended 2026-11-22 for #77)

Sources (public main, inspected; upstream can change):
- [README](https://github.com/iamlukethedev/claw3d/blob/main/README.md): Studio browser connects to /api/gateway/ws; Studio proxies an upstream gateway.
- [docs/hermes-gateway.md](https://github.com/iamlukethedev/claw3d/blob/main/docs/hermes-gateway.md): Hermes compatibility adapter, normally ws://localhost:18789.
- [server/hermes-gateway-adapter.js](https://github.com/iamlukethedev/claw3d/blob/main/server/hermes-gateway-adapter.js): handshake, request dispatch, agents.list, sessions.list, status, chat and presence envelopes.
- [src/features/agents/state/runtimeAgentEventWorkflow.ts](https://github.com/iamlukethedev/claw3d/blob/main/src/features/agents/state/runtimeAgentEventWorkflow.ts): agent event streams and lifecycle/tool state.

## Wire subset

JSON WebSocket frames:
- Client request: `{"type":"req","id":"1","method":"agents.list","params":{}}`.
- Success: `{"type":"res","id":"1","ok":true,"payload":{...}}`.
- Error: `{"type":"res","id":"1","ok":false,"error":{"code":"READ_ONLY","message":"..."}}`.
- Event: `{"type":"event","event":"presence","payload":{...}}`.

Server begins with connect.challenge, payload {nonce}. Client sends connect; response
payload has type hello-ok, protocol 3, adapterType, features {methods,events},
snapshot {health:{agents,defaultAgentId},sessionDefaults:{mainKey:"main"}},
auth {role,scopes}, policy {tickIntervalMs}. Hermes advertises admin scopes;
**this adapter does not**. Its connect is protocol negotiation, NOT authentication.
A real transport must enforce local client authentication separately.

agents.list returns {defaultId,mainKey:"main",agents:[{id,name,identity:{name,emoji},role}]}.
Hermes also supplies workspace paths; deliberately omitted here.
sessions.list returns {sessions:[{key:"agent:<id>:main",agentId,displayName,
origin:{label,provider},model,modelProvider}]}.
status and presence use {sessions:{recent:[{key,...}],byAgent:[{agentId,recent:[...]}]}}.
Hermes includes updatedAt when known; skeleton does not invent activity timestamps.

Runtime agent events consume payload {runId,stream,data}; lifecycle stream uses
data.phase start/end/error; tool stream uses name and phase (result is completion).
Skeleton additionally supplies agentId and sessionKey to associate runs, and sends
only tool names, never tool arguments/results. Stable synthetic run IDs pair start
and terminal events across polls. Working => start; idle/done => end; error => error;
disappeared running agents => end. No repeated start for unchanged working agents.

Hermes emits chat events {runId,sessionKey,state:delta/final/error/aborted}, often
with assistant content. **Do not synthesize chat text** for metadata-only office.

## Claw3D itself (researched 2026-11-22, upstream `51337f7f250459af61a7cf92b7b0015d59faed5c`)

- **Repo and licence**: github.com/iamlukethedev/Claw3D (owner's 2026-11-04 choice),
  **MIT**, © 2026 Luke The Dev; v0.1.4, about 2.5k stars, last push 2026-11-04. A Next.js 16 /
  React 19 app ("Studio") with a custom Node server (`server/index.js`), three.js via
  react-three-fiber for the 3D office, and Phaser for the 2D builder. It needs npm and a
  `next build`.
- **How a gateway feeds it**: the browser talks only to Studio (`/api/gateway/ws`). Studio's
  server (`server/gateway-proxy.js`) opens a second WebSocket to the configured upstream: the
  `gateway` entry of `<state dir>/claw3d/settings.json` (`server/studio-settings.js`; default
  `ws://localhost:18789` with no token). The proxy does not read `CLAW3D_GATEWAY_URL` /
  `CLAW3D_GATEWAY_TOKEN`; those only seed Studio's settings page when the file is missing (#77
  fix round 1, which is why deploy-air.sh writes the file). It sends `Origin:` derived from that
  URL (127.0.0.1/::1 → `http://localhost:<port>`) and injects its configured token into
  `connect.params.auth.token`. That token may also reach the browser (Claw3D README), so it
  must never be a provider credential. Hello `adapterType` values Studio does not know are
  treated as `openclaw`. Studio binds 127.0.0.1 by default and refuses a public bind without
  `STUDIO_ACCESS_TOKEN`.
- **Methods a gateway must answer**: on connect, `GatewayClient` calls `config.get` (and
  `config.patch` unless the reload mode is `hot`/`off`). A failing `config.get` fails the
  connect. Fleet hydration (`agentFleetHydration.ts`) calls `config.get`,
  `exec.approvals.get`, `agents.list` (the only fatal one), `agents.files.get` (only when
  an identity name is missing), `sessions.list` per agent, then `status` +
  `sessions.preview` together (one failure drops both). The bridge answers all of these
  read-only (README "Methods").
- **How people are drawn**: in 3D (`src/features/retro-office/objects/agents.tsx`) they are
  voxel humanoids built from an `AgentAvatarProfile` (`src/lib/avatars/profile.ts`): skin,
  hair style and colour, top `tee|hoodie|jacket` and its colour, bottom, shoes, hat,
  glasses, headset, backpack, each from a fixed option list. A profile comes from Studio
  settings (`settings.avatars[gatewayKey][agentId]`, set in the avatar editor), otherwise
  `createDefaultAgentAvatarProfile(agent.id)` (a hash of the id). **A gateway cannot set
  it in stock Claw3D.** In 2D (fleet sidebar, chat panel) `AgentAvatar` shows
  `identity.avatarUrl` (or `identity.avatar`) when it is an `http(s)://` or
  `data:image/` URL. That path is stock, and the bridge uses it for the pixel sprites.
- **How the floor is drawn**: procedurally, in `src/features/retro-office/scene/environment.tsx`.
  The local office floor is a plane of warm wood `#c8a97e` with darker floor lines, on a
  `#263238` ground. Furniture comes from GLB models under `public/office-assets/models/furniture/`.
  The layout is per Studio (`/api/office/layout`, the builder). There is no floor
  texture or theme hook, so the Claw3D floor style is the stock one, and our tiles reuse its
  colour for the 2D preview and sprites.
- **Live roster**: the Office screen handles every `presence`/`heartbeat` event by calling
  `loadAgents()` (at most every 3 s), which re-reads `agents.list`. The bridge sends
  `presence` on every poll, so people appear and leave live in stock Studio. No extension
  consumer is needed for roster changes.

### Smallest real integration path (chosen)

1. `bridge.mjs` is Studio's upstream gateway (stock Studio and stock proxy, configured by env).
2. Sprites ride on the stock `identity.avatarUrl` (2D panels, no Claw3D change).
3. The 3D suits need one upstream change: `claw3d/avatar-profile-hint.patch` (6 lines in
   `agentFleetHydrationDerivation.ts`). It uses `identity.avatarProfile` from `agents.list`,
   normalized by Claw3D's own `normalizeAgentAvatarProfile`, when no saved Studio profile
   exists. It was checked with `git apply --check` against 51337f7, but not compiled here
   (that needs npm).
4. Nothing else in Claw3D is changed: no floor texture patch, no `office.snapshot` consumer.

## Explicit extensions / remaining integration

office.snapshot {agents} and each agent's office metadata are CortadOS extensions,
not stock Claw3D protocol. They carry complete roster replacement, room (group, groupLabel,
room index, kind, parent, depth), device, project, status, task status, whether a finished
task is reporting back (returning, returnUntil), provider, lane, model, tool name, task title
and appearance (characterFamily, familySource, outfit, sprite). Stock Claw3D ignores them;
nothing requires them. The roster itself comes from `GET /v1/cortados/office/tree` (#77
rooms; FLOORS-DESIGN.md §8).

Office v1 workers (and a routed orchestrator) carry `local_backend`: `ollama` | `vllm-mlx` for
a local worker whose node gate reported its backend, else null (additive since #77 round 1; an
older provider omits it, and the adapter then falls back to the model id's shape).

characterFamily is claude/codex/grok/ollama/local/unknown, and outfit is worker or suit
(orchestrator). Mapping rules are in README "Appearance". Unknown provider gets the neutral
`unknown` sprite, with provider kept null. The orchestrator's suit comes from the bot origin,
the provider's `client` (the routed main loop's wire, else the heartbeat's claim) or, failing
those, a guess from its model name (`familySource` says which); its published provider/lane
stay null: never inferred.

## Gaps (open)

- **Floors → rooms (built, #77)**: Claw3D shows one office per gateway, and its floors are a
  compiled list keyed by adapter type (FLOORS-DESIGN.md §3). CortadOS draws one ROOM per
  orchestrating session on that one floor with `claw3d/cortados-rooms.patch` (8 rooms x 6
  desks, sticky desk allocation, off unless this bridge is the gateway). Real floors per
  orchestrator remain out of scope (owner decision E1); the allocator is where a later "spill
  to another floor" would go. The flat view's lobby is not drawn (FLOORS-DESIGN.md E9).
- **Grok workers**: office.py's `provider_of` maps only claude-cli, codex-cli and local-*,
  so no worker is `grok` today. The sprite and mapping are ready for when a grok lane exists.
- **Desk count**: 8 rooms x (1 head + 2 report + 6 team) desks. Extra people get no desk and
  walk the floor, Claw3D's own behaviour for an unassigned agent (FLOORS-DESIGN.md §8.3
  "Overflow"; not checked live).
- **Not verified live**: no patched Claw3D Studio has run against the bridge. The rooms patch
  applies with the other two on 51337f7 and type-checks (`tsc --noEmit`); lint, `next build`
  and a live Studio are the deploy's own build and the design's step 4.
