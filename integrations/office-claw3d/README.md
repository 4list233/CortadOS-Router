# CortadOS → Claw3D office adapter (step 2, not deployed)

New integration: no existing office adapter was found. Node 22+ stdlib only.
No npm install, Claw3D process, account creation, or Air configuration is part of
this change. See [PROTOCOL.md](PROTOCOL.md) for the researched protocol and gaps.

## Test

```sh
node --test integrations/office-claw3d/
```

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
cancellation. Remote providers require HTTPS, redirects are refused. Only GET of
the exact office endpoint is possible. Failures emit fixed diagnostics, retaining
the last successful snapshot, which may be stale. No prompts, commands, file
contents, source response bodies, token paths or exception text are logged.
Metadata stdout still deserves local-only access and should not be publicly logged.

The future WebSocket host MUST bind only 127.0.0.1/::1, enforce local auth and
Origin allowlisting, and must not advertise writable methods. All methods except
connect, agents.list, sessions.list and status are rejected. No transport dependency
is installed: OfficeGateway(send) is the transport interface. A per-client gateway
accepts request(frame) and update(officeSnapshot). Poll updates fan out to those
instances. Existing Claw3D UI still needs an extension bridge for office.snapshot,
floor layout and character hints; skeleton is not a deployable gateway yet.

Stable IDs namespace orchestrators separately from workers, and named workers
separately from anonymous fingerprints. Duplicate identities reject the snapshot.
Nested records require a recognized status and typed projection metadata (nullable
strings, not arrays/objects); workers require run_id (nullable only in the lobby).
Malformed snapshots leave the previous roster and lifecycle state untouched.
The exported polling API reports fixed, credential-safe diagnostics to stderr by
default; callers may supply an onError function. Operational limits and their
local-policy rationale are documented beside their constants in adapter.mjs.
Lobby
records lacking run_id use a best-effort metadata fingerprint + ordinal: v1 hides
conversation IDs, so reordering anonymous records may recreate their characters.
Floors with absent orchestrators do not get fabricated people. Appearance art is
future work; provider families and the orchestrator suit are metadata only.
