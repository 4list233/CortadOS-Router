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
  server (`server/gateway-proxy.js`) opens a second WebSocket to the configured upstream
  (`CLAW3D_GATEWAY_URL`, default `ws://localhost:18789`). It sends `Origin:` derived from that
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
not stock Claw3D protocol. They carry complete roster replacement, floor, device,
project, status, provider, lane, model, tool name, task title and appearance
(characterFamily, outfit, sprite). Stock Claw3D ignores them; nothing requires them.

characterFamily is claude/codex/grok/ollama/local/unknown, and outfit is worker or suit
(orchestrator). Mapping rules are in README "Appearance". Unknown provider gets the neutral
`unknown` sprite, with provider kept null. The orchestrator's sprite comes from its
model name, but its published provider/lane stay null: never inferred.

## Gaps (open)

- **CortadOS floors vs Claw3D floors**: Claw3D shows one office per gateway. All CortadOS
  floors (sessions) and the lobby share it; each person's floor is in `office.floor`, and
  sessions are not separate rooms. Multi-floor (`docs/office_sys/multi-floor-runtime-architecture.md`
  upstream) is an upstream roadmap item, not a stable API.
- **Ollama vs other local backends**: office v1 says `provider: local` and the lane key,
  not the node's backend (`ollama` | `vllm-mlx`). The llama is chosen from the model id's
  shape. office.py publishing the backend would make it exact.
- **Grok workers**: office.py's `provider_of` maps only claude-cli, codex-cli and local-*,
  so no worker is `grok` today. The sprite and mapping are ready for when a grok lane exists.
- **Desk count**: the default Claw3D office has 8 desks and office.py caps 32 workers
  per floor. Extra people stand in Claw3D's own overflow behaviour (not checked live).
- **Not verified live**: no Claw3D instance has run against the bridge. The method set
  and payload shapes come from reading 51337f7's source, and the patch is not compiled.
