# Connector manifest and policy core (not wired)

Nothing is wired yet. No discovery, authentication, execution, network access or
live connector configuration is added. The example is illustrative, not an
inventory of the owner's actual connectors. The stdlib-only canonical module is
`cortados_router/connectors.py`; no equivalent policy was found in the existing
connector-adjacent child-task, MCP or bot modules.

## Manifest version 1

`tables/connectors.example.json` demonstrates the closed schema enforced by
`validate_manifest` (raises `ManifestError` with a field path).
Root: integer `version: 1`, `servers: []`. Each server has a unique nonempty
`name`, `kind` (`claude_ai|mcp_stdio|mcp_http`), optional `url` (required for
mcp_http), and `tools: []`. Each tool has a per-server unique `name` and
`class: read|write|outward`. URLs must be HTTP(S), with no credentials,
query or fragment. Unknown fields, duplicate names and invalid classes fail.
Names are exact and case-sensitive; URL is metadata, not authority.

`suggest_class(name)` offers human-authoring suggestions: send/post/publish/
delete/share/pay/invite/deploy/transfer → outward; get/list/search/read/query/
fetch → read; create/update/edit/write/upload/draft → write; otherwise outward.
Outward verbs win mixed names. **Suggestions never grant access.** Every
unlisted server/tool is outward, including names like `get_messages`.

## Pure decision API

`decide(caller_tier, server, tool, args, run_state, approvals)` returns
`("allow"|"ask"|"deny", reason)`. Server/tool are names, args is a JSON object.
Use `RunState(manifest, tainted=False, test_targets=())`.
Malformed calls, manifests, state or unknown tiers deny.

| Tier | Declared read | Declared write | Outward / unknown |
| --- | --- | --- | --- |
| owner-interactive | allow | allow | exact-call approval or ask |
| owner-unattended | allow | allow | exact-call approval or ask |
| bot | allow | deny | deny, even with approval |
| outcome-loop | allow on test targets | allow on test targets | always deny |

Outcome-loop denies **all** calls outside the explicit test-target server list;
it never approves and cannot use even an owner approval to perform outward calls.
An ask is never permission to execute; unattended hosts must hold the call
until the owner explicitly approves. Writes are allowed for owners under the
2026-11-07 “every tool gets read and write” decision; outward effects remain gated.

Approval records are trusted host-owned dictionaries:
`{call_hash(server, tool, args): {"approved": True}}`.
The digest is SHA-256 of canonical JSON `[server, tool, args]` (sorted object
keys, compact separators, ASCII escapes). Argument order within objects is
irrelevant; array order and values matter. Non-JSON/coercible values and
nonfinite floats are rejected. No wildcard, name-only or truthy approval works.
The host must authenticate approvals, scope them to the current run, and prevent
model/connector content from changing the manifest, taint, targets or approvals.
This core does not issue, store, consume or authenticate approvals.

After **any outside-content read** (email, chat, docs, web), the trusted host
must replace state using `mark_external_read(state)`; taint remains set for the
run. Decisions never mutate state. Taint does not revoke declared read/write
access or turn writes outward; every outward call still needs its own exact-call
approval, including after taint. A matching explicit approval can allow it.
A new run starts with new state/approval scope; no live host integration exists.

## Verification

`python3 -B -m unittest tests.test_connectors`

Only the focused suite is intended for this change; no live connector tests.
