# Claw3D gateway protocol research (2026-11-07)

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

## Explicit extensions / remaining integration

office.snapshot {agents} and each agent's office metadata are CortadOS extensions,
not stock Claw3D protocol. They carry complete roster replacement, floor, device,
project, status, provider, lane, model, tool name, task title and appearance hints.
Stock Claw3D may ignore these; a later UI bridge must consume them (including roster
removal) and associate unsolicited lifecycle runs. No end-to-end compatibility is
claimed: current work is a transport-free skeleton.

characterFamily is claude/codex/grok/ollama/local; suit is the orchestrator outfit.
Requested art: orange Claude mascot, Codex robot, dark Grok robot, Ollama llama.
These are hints, NOT installed sprites or existing Claw3D family identifiers.
Unknown provider uses neutral local appearance, retaining provider null/unknown.
Office v1 currently emits claude/codex/local only; local is NOT assumed Ollama.
Orchestrator v1 has no provider/lane: never infer these from model names.

Node 22 has a WebSocket client, not a server. No package installed or required here.
Future host can implement send(frame), forward decoded requests to request(frame),
and call challenge() per authenticated connection; e.g. a separately reviewed ws
server dependency, with loopback-only bind, Origin/auth checks and bounded frames.
Use one gateway instance per connection. Polling and mapping remain independent.
