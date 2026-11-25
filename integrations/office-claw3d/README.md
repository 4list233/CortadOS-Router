# CortadOS → Claw3D office adapter (steps 2–3, not deployed)

Node 22+ stdlib only. No npm install, Claw3D process, account creation, or Air
configuration is part of this folder's code; [DEPLOY.md](DEPLOY.md) is the (unrun)
runbook for the Air. See [PROTOCOL.md](PROTOCOL.md) for the researched Claw3D
protocol, how it renders people and floors, and the remaining gaps.

| File | What |
|---|---|
| `adapter.mjs` | the provider's orchestrator tree (`GET /v1/cortados/office/tree`) → Claw3D gateway frames, one room per orchestrator (`mapTree`); sprite choice per person; polling |
| `bridge.mjs` | the WebSocket gateway Claw3D Studio's proxy connects to (loopback, auth, Origin, read-only) |
| `sprites.mjs` | original pixel art as text grids, PNG encoder, Claw3D 3D avatar profiles |
| `build-assets.mjs` | regenerates `assets/` and `assets/preview.png` from the fixture (`--write` / `--check`) |
| `assets/` | sprites, floor tiles, preview; origin and licence in `assets/NOTICE.md` |
| `claw3d/avatar-profile-hint.patch` | 6-line Claw3D change so the 3D figures wear the family suits |
| `claw3d/studio-login.cjs`, `claw3d/studio-login.patch` | login form for Studio's access cookie (`/cortados-login`) and its 3-line wiring |
| `claw3d/system-fonts.patch` | drops `next/font/google` for system font stacks, so the build never fetches from Google Fonts (its `&skey=` font URLs broke the Turbopack build, 2026-11-24) |
| `claw3d/proxy-auth.patch` | Studio's gateway proxy always sends its configured token to the bridge and drops the browser's device block and credentials (over HTTPS the stock proxy sent no token and the Office closed with 1012, 2026-11-25) |
| `gateway-check.mjs` | deploy-air.sh's end-to-end health probe: a plain `connect` and a browser-shaped one (signed device block, no token) through Studio's own proxy must each get the bridge's hello |
| `studio-settings.py` | builds Studio's gateway settings file (`<state dir>/claw3d/settings.json`: the bridge URL and secret), the only upstream config Claw3D's proxy reads |
| `claw3d/cortados-rooms.patch`, `claw3d/cortadosRooms.ts` | rooms per orchestrator: the floor's 8 rooms x 6 desks and the desk allocation (the patch adds the `.ts` file and a 40-line hook; [FLOORS-DESIGN.md](FLOORS-DESIGN.md) §8) |
| `claw3d/cortados-brand.patch` | brands the Office as the CortadOS router (title, floor card, connection badge), removes the flag poles and hides the OpenClaw controls the read-only bridge cannot serve, the chat composer included ([OFFICE-CORTADOS-DESIGN.md](OFFICE-CORTADOS-DESIGN.md) part A) |
| `claw3d/office-mode.patch`, `claw3d/office-mode.cjs` | "office mode": Studio's server answers only the Office's own routes and methods and 403 to the rest (`PUT /api/studio`, remote office, runtime, voice, every write), so the cookie opens a read-only view ([OFFICE-CORTADOS-DESIGN.md](OFFICE-CORTADOS-DESIGN.md) A.5) |
| `DEPLOY.md`, `deploy-air.sh`, `deploy-lib.sh` | Air runbook, script (install / upgrade / tailnet publish) and its pure checks, written and **not run** |

## Test

```sh
node --test integrations/office-claw3d/                # Node 26 resolves the folder
node --test integrations/office-claw3d/*.test.mjs      # portable form (Node 22/24)
# rooms.test.mjs runs the Claw3D allocator (claw3d/cortadosRooms.ts) under Node's type stripping
# (module.stripTypeScriptTypes, Node >= 22.13); on older Node those tests are skipped and say why.
node integrations/office-claw3d/build-assets.mjs --check
```

## Rooms (issue #77)

One room per orchestrating session on the one CortadOS floor ([FLOORS-DESIGN.md](FLOORS-DESIGN.md), decisions of
2026-11-24 in §7). The bridge polls `GET /v1/cortados/office/tree` (the provider's orchestrator tree, office:read,
metadata only) and draws, per room: the orchestrator in a suit at the head desk; its running, waiting or unknown child
tasks at the team desks, each followed by its live helpers (routed sub-sessions) and its nested tasks; the
orchestrator's own routed helpers. A finished task leaves its desk and sits at a report desk beside the orchestrator for
10 minutes with the subtitle `done · <model>`, `failed · <model>` or `cancelled · <model>` (the last two in error
status), then leaves; a finished helper just leaves. The Grok Bot has a room; the unattributed room comes last and only
while someone in it is active. 8 rooms x 6 team desks; past that, people walk the floor unseated (Claw3D's behaviour
for anyone without a desk). Rooms and seats are sticky while their people stay. With any other gateway connected,
the stock layout comes back; the brand patch (below) is not switched, since this Studio build serves only the bridge.

Each nameplate's second line ends with where the person runs (`claude-opus-5 · Pro`): a task's exec host, an
orchestrator's session host (its device matched to a known host name by the provider: `live.orchestrator_hosts`, the
tree's group `host`), a helper's task or session host. No badge when no known name matches; a raw hostname is never
shown, and the group's raw `device` is not in `agents.list` at all. Branding, the removed props, the hidden controls and the design of what comes next (orchestrator names, server
room, steering) are in [OFFICE-CORTADOS-DESIGN.md](OFFICE-CORTADOS-DESIGN.md).

## Appearance (issue #77)

![preview](assets/preview.png)

Each person gets a sprite, as metadata only. The published `provider`/`lane` never change:

- **Worker**: by the provider it ran on (`claude`, `codex`/`openai` → codex robot, `grok`/`xai`
  → dark robot, `local`), else by its lane key (`claude#b`, `codex`, `local-*`). Otherwise
  it gets the grey `unknown` visitor. A worker's model never chooses its sprite.
- **Local lane**: by the node's backend, which office.py publishes per local worker as
  `local_backend` (`ollama` → the llama, `vllm-mlx` → the green chip `local`, any other value →
  `local`). The provider takes it from the node gate's own report. Only when `local_backend` is
  absent or null (an older provider, or a node whose gate never said) does a name-shape
  guess apply: the llama for an Ollama-shaped tag (`name:tag`, e.g. `qwen3:27b`) that does not
  name mlx, else `local`. Each agent's `office.localBackend` holds the published value (known
  values only, else null).
- **Orchestrator**: wears a suit, styled by its **harness**, first source that applies (E7):
  the bot origin (`grokbot` → grok); the provider's `client` for the session (the routed main
  loop's wire: `/v1/messages` → Claude Code → claude, `/v1/responses` → Codex → codex; else the
  session heartbeat's optional `client`; `antigravity` → the neutral unknown suit until it has a
  lane); otherwise a guess from its **model**: `claude-*`/opus/sonnet/haiku/fable → claude,
  `gpt-*`/`codex*`/`o<N>` → codex, `grok*` → grok, an Ollama tag → ollama,
  qwen/llama/gemma/mistral/mlx… → local, anything else → unknown. `office.familySource` says which
  (`bot | wire | heartbeat | model-guess`). Its published provider stays `null`.

Each agent in `agents.list` carries `identity.avatarUrl` (the sprite as a 128 px PNG data
URL, which Claw3D's 2D panels show unmodified) and `identity.avatarProfile` (a Claw3D voxel
figure profile: family colour, a hoodie for workers, Claw3D's own jacket and pants as the
suit). Stock Claw3D ignores `avatarProfile`; the patch makes it use it.

## Intended runtime

Run as dedicated macOS user **_cortados-office**, separate from the provider and
Claw3D installation. Give that user only a dedicated **office:read** scoped client
token, never the provider owner token. Provider registration lives in
cortados_router/provider/clients.py; an owner provisions it separately.
Store token outside the repo in a file owned by that user, mode 0600. The adapter
rereads it each poll for rotation; never puts it in output or command arguments.
Do not expose the office token to the browser.

A local diagnostic transport (metadata JSONL stdout, not a WebSocket listener):

```sh
CORTADOS_OFFICE_TOKEN_FILE=/private/path/to/office-token \
CORTADOS_OFFICE_URL=http://127.0.0.1:8432/v1/cortados/office \
CORTADOS_OFFICE_POLL_SECONDS=5 \
node integrations/office-claw3d/adapter.mjs --stdio
```

This command is illustrative, **not run during development**. Poll interval 1–3600
seconds (default 5); ten-second request timeout, sequential polls, SIGINT/SIGTERM
cancellation. Remote providers require HTTPS, redirects are refused. `CORTADOS_OFFICE_URL`
names the office endpoint; the only request is a GET of the tree beside it
(`/v1/cortados/office/tree`). Failures emit the fixed diagnostic `Office tree unavailable;
last successful roster retained (may be stale)`; there is no fallback to the flat office view. A tree
that answers but says it is unavailable (its `orchestrators` or `lobby` status `error`, or `partial`
with any error other than one task directory's own `task_dir`) is a failure too: the roster stays. No prompts, commands, file
contents, source response bodies, token paths or exception text are logged.
Metadata stdout still deserves local-only access and should not be publicly logged.

The WebSocket host is `bridge.mjs` (stdlib, no `ws` dependency). It binds only
127.0.0.1/::1 and accepts an upgrade only from a loopback peer, on path `/`, with a
localhost Host and an allowlisted Origin (default `http://localhost:<port>`, which
Studio's proxy sends). The first frame must be `connect` carrying the local **bridge
secret** in `params.auth.token`. That secret is never the office token, and the bridge
refuses to start if they match. Frames are text only, masked, unfragmented and at most
64 KiB, with at most 8 clients, a rate cap and a 10 s connect deadline.

```sh
CORTADOS_OFFICE_TOKEN_FILE=/private/path/office-token \
CORTADOS_OFFICE_BRIDGE_TOKEN_FILE=/private/path/bridge-token \
node integrations/office-claw3d/bridge.mjs     # 127.0.0.1:18789; illustrative, not run here
```

Both token files must be 0600. Optional settings: `CORTADOS_OFFICE_BRIDGE_HOST` (127.0.0.1
or ::1), `_PORT`, `_ORIGINS` (comma list, `*` refused), `CORTADOS_OFFICE_URL`,
`CORTADOS_OFFICE_POLL_SECONDS`.

Methods: `connect`, `agents.list`, `sessions.list` and `status` serve the office roster.
Studio also calls `config.get`, `exec.approvals.get`, `agents.files.get`,
`sessions.preview`, `chat.history`, `models.list`, `skills.status` and `cron.list` while
it connects and hydrates. These get FIXED, empty answers: no transcript, file or config
is served. `config.get` says reload mode `off`, so Studio never tries `config.patch`.
Every other method (`chat.send`, `*.set`, `*.patch`, `agents.create`, `wake`, …) is
refused with `READ_ONLY`, and the hello advertises read methods only.
OfficeGateway(send) stays the transport seam: one gateway per authenticated
connection, and poll updates fan out through `OfficeHub`.

Stable IDs carry the room (`office-<session tag | grokbot | unattributed>-orchestrator`,
`-task-<task id>`, `-sub-<task id>-<ref>`, `-routed-<ref>`; the "No orchestrator" room's people are
`office-lobby-routed-<ref>`). Duplicate identities reject the
tree. Nested records require a recognized status, a valid id and typed metadata (nullable
strings, not arrays/objects). A malformed tree leaves the previous roster and lifecycle state
untouched. (`mapOffice`, the flat view's projection, is still exported and tested; the bridge
no longer draws from it.)
The exported polling API reports fixed, credential-safe diagnostics to stderr by
default; callers may supply an onError function. Operational limits and their
local-policy rationale are documented beside their constants in adapter.mjs.
Lobby
records lacking run_id use a best-effort metadata fingerprint + ordinal: v1 hides
conversation IDs, so reordering anonymous records may recreate their characters.
Floors with absent orchestrators do not get fabricated people.
