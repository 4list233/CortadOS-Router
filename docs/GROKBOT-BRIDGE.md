# Grok Bot ↔ CortadOS bridge — architecture plan

Status: PLAN ONLY (2026-11-03). Nothing here is built or deployed. Decisions marked [ESCALATE] are Forest's.
Machines are named Pro, Air and Studio only; no addresses, hostnames or secrets appear in this file.

## 1. What triggered this

An agent calling itself "Router Bot" asked the owner to: add `tag:grokbox` to `tagOwners`, add an ACL that lets only
that tag reach "the Mac Studio provider on port 8433" and denies everything else for the tag, generate an ephemeral,
non-reusable auth key tagged `tag:grokbox`, and submit the key through "Router Bot's secure card". It stated that the
Studio provider already runs as `com.cortados.provider` on port 8433.

## 2. Findings (read-only investigation, 2026-11-03)

### 2.1 The Studio's `com.cortados.provider` (port 8433)

- It IS this repo's provider (`python3 -B -m cortados_router.provider.server --host <Studio tailnet address> --port
  8433`), working directory `~/cortados-router`, branch `main` at `a75d304` (the same commit the Air runs; one merge
  behind `origin/main`). The checkout has uncommitted nightly table rebuilds (`tables/*.generated.json`, benchmarks).
- It was installed on 2026-11-02 19:49 local time, from a separate folder `~/cortados-provider-svc/` (plist copy,
  a `provider-token` file, logs). Its only log line is the start banner; it has served no logged traffic.
- Differences from the Air's (the reviewed, documented deployment):

  | | Air `com.cortados.provider` | Studio `com.cortados.provider` |
  |---|---|---|
  | Port | 8432 on its tailnet address | 8433 on its tailnet address |
  | Python | system `/usr/bin/python3` (3.9) | Homebrew Python 3.14 |
  | Secrets | sourced at start from `~/.cortados/provider.env` | **inline in the LaunchAgent plist** (`CORTADOS_PROVIDER_TOKEN`, `CORTADOS_S1_TOKEN`) |
  | Backend CLIs | explicit `--claude-bin` / `--codex-bin` | none given: whatever `claude` / `codex` is first on the plist's PATH (both are installed on the Studio) |
  | State | Air `~/.cortados` | Studio `~/.cortados` — the Studio's older, much larger fleet state folder (Hermes-era files, bus, mail, jobs), not a provider-only home |
  | Provider token | its own | a **different** token (compared by hash only) |
  | Documented anywhere | `README.md`, memory notes | **nowhere**: no repo doc, commit or memory note mentions 8433 |

- Consequences: a second, unreviewed router instance reachable from every tailnet device that holds its token, spending
  the Studio's own Claude/Codex logins, writing a second `router.db` / scarcity view (the fleet's learned state splits),
  with its credentials stored in a plaintext plist.
- Most likely origin (inferred, NOT proven): Grok Bot's local-exec daemon on the Studio (see 2.2) — its setting
  `localToolPermission` is `always`, the provider folder has no matching shell history, and the request that names
  this port came from a Grok Bot agent. Forest should confirm whether he or an agent installed it.

### 2.2 What Grok Bot / Router Bot / grokbox are

Established:
- **Grok Bot** is an Electron desktop app (`/Applications/Grok Bot.app`, v0.78.1, bundle id `com.anysphere.sand` —
  i.e. built by Anysphere, the Cursor company; xAI's Grok models). Installed on Pro, Air and Studio; running on the
  Studio. Its agents run in a cloud "box"; usage is metered on the Cursor account's weekly pool shared with Cursor
  (earlier finding, docs.x.ai/grok-bot). Its own model traffic goes to xAI over `wss://api.x.ai/`; no setting for a
  custom model base URL was found, so its LLM calls cannot be pointed at CortadOS.
- It ships a **local-exec daemon** (`~/.grokbot/`, `local-exec-daemon*.json`, a persistent outbound watch stream) that
  lets the cloud agent run tools on the Mac. On the Studio: `localToolPermission: always`, `localEgressAllowed: true`;
  computer use and Mac device tools gated off. The Pro's daemon is also connected.
- It has **user-defined "Bots"**, a **secure field** for secrets ("box secrets", kept in the OS keychain), box MCP
  servers (`mcpBoxServers`, none configured) and an **egress tunnel** setting (off).
- It has a **native Tailscale plugin**: the app joins a *Grok Bot device* to your tailnet through an interactive
  Tailscale sign-in by a tailnet owner/admin; the device appears on the Machines page named `grokbot-…`
  (`grokbot-team-…` for team tailnets), must be approved by the admin if device approval is on, signs itself out if
  the login is not an owner/admin or changes, and honours Tailnet Lock. The bundle has no auth-key input path
  (no "auth key"/"authkey" strings).

Inferred:
- **Router Bot** is one of the owner's Grok Bot Bots; its "secure card" is Grok Bot's secure secret field.
- **grokbox** is a tag name Router Bot invented for the Grok Bot cloud box; it appears nowhere else (repo, `~/Ai`,
  `~/code`, Studio LaunchAgents).

Unknown:
- Who created the Studio 8433 instance, and what Router Bot intends to send to it.
- Whether the Grok Bot device keeps working after an admin applies an ACL tag to it (tagging changes the device's
  owner from the user to the tag; the app signs out when "the device's Tailscale login is no longer the one you
  confirmed"). Must be tested (ticket G5).
- Whether Grok Bot's box can call an HTTP MCP server on the tailnet (`mcpBoxServers`) — would allow a tool instead of
  raw HTTP (ticket G8, optional).

## 3. Direction

- **(a) Grok Bot as a model client — not possible.** Its own LLM is Grok on xAI, billed to the Cursor pool, with no
  base-URL override. What *is* possible is Grok Bot as a **delegating caller**: its agent sends self-contained,
  tool-less subtasks to CortadOS over HTTP (the `route_task` pattern) and gets text back. That makes the router pick
  a model for work Grok Bot hands off.
- **(b) Grok/Cursor as a pool resource — not now.** No API to drive Grok Bot; the Cursor lane is parked (glob/grep
  cannot be denied). Grok Bot stays dashboard-only: its usage is the shared Cursor weekly % (`usage.py`
  `SHARED_WITH = {"cursor": ["Grok Bot"]}`).

### Recommendation

1. Build (a) as a **narrow delegating-caller** path: one HTTP endpoint use (`POST /v1/messages`, model `cortados`,
   no `tools`), nothing else.
2. **Consolidate on the Air provider.** Retire the Studio 8433 instance; do not build a dedicated provider. One router
   keeps one learned state, one scarcity view, one place to cap spend; the Studio stays S1-only (as decided when the
   Air became the provider host). Isolation comes from a per-client token and scope, not a second router.
3. **Per-client tokens** with endpoint/model scope, rate and daily caps, expiry and revocation, so a remote bot can
   never drain subscriptions or reach the owner's endpoints (dashboard data, `/bridge/call`, run records).
4. **Network: use Grok Bot's native Tailscale plugin**, not a hand-made auth key in a chat agent's secret card; then
   restrict that device with an ACL to the Air's provider port only — and first replace the default allow-all rule,
   otherwise the "only this port" rule restricts nothing.
5. **Contain the local-exec path first**: while Grok Bot can run tools on the Studio with permission `always`, it can
   read any token on that machine and any ACL is moot. Set it to ask (or off) on Studio and Pro before bridging.
6. Attribute all of its traffic as `provider:grokbot` (from the token, not the User-Agent) in the route log, run
   records and the dashboard's usage view, with its own refusal counts and cap meters.
7. No child tasks, no chrome, no tools executed on this fleet for this client. `start_task` is stdio-MCP only today
   and must stay unreachable over HTTP (pinned by a test).

## 4. Which provider instance

Consolidate on the Air (port 8432). Reasons: it is the documented, reviewed deployment with explicit CLI paths and an
env file for secrets; it is what every other client (Pro harness, child tasks, routed subagents) uses, so caps and the
dashboard see all spend in one place; two routers would each believe they own the scarcity budget and would double
the risk of exhausting a login. A dedicated instance would only add isolation the token scope already gives, at the
cost of split state. If Forest later wants hard process isolation, run a **front door** (a thin token-checking proxy in
the style of `local_gate.py`) on the Air in front of the same provider — still one router.

Retire the Studio instance (owner step, G0): `launchctl bootout gui/$(id -u)/com.cortados.provider` on the Studio,
move its plist out of `~/Library/LaunchAgents`, delete `~/cortados-provider-svc/provider-token`, and treat both tokens
that were inline in that plist (provider and S1) as exposed: rotate the S1 token on the Studio scorer and on the Air's
env file.

## 5. Network boundary

Goal: the Grok Bot device can open exactly one TCP port on exactly one machine (Air, provider port), nothing else; no
fleet machine needs to reach it.

Corrections to the request:
- **Wrong target.** Point at the Air's provider (8432), not the Studio's unreviewed 8433 instance.
- **Wrong join method.** Grok Bot's own plugin joins through an admin's interactive Tailscale sign-in plus device
  approval; it has no auth-key input. An auth key pasted into a Bot's secret store is a bearer credential held by a
  cloud agent that can join *any* machine it likes as `tag:grokbox`. If an auth key is used anyway (only if G5 shows
  the plugin cannot keep a tag): one-off, ephemeral, tagged, **not pre-approved**, expiry 1 hour, revoked after the
  join; the owner enters it himself in Grok Bot's Tailscale UI, not through a Bot conversation.
- **"Deny everything else" is not a rule Tailscale has.** Tailscale policy is default-deny with allow-only rules. The
  catch is the default policy's allow-all rule (`src: ["*"]`), which also matches tagged devices: if the tailnet still
  has it, the new tag can reach every port on every machine. It must be replaced by an explicit rule for the owner's
  own devices.
- **SSH:** no `ssh` rule may name `tag:grokbox` or `*` as source.

## 6. Auth: per-client tokens (to build)

Today one shared token per provider grants everything (`Handler._authorized`), and the client label is the
User-Agent (client-asserted). Proposal:

- **Registry** `$CORTADOS_HOME/clients.json` (mode 0600, owner-checked like `hardware.py`'s state tree): per client
  `id`, `token_sha256` (the raw token is never stored), `endpoints` allowlist, `models` allowlist, `max_input_tokens`,
  `rate` (requests per minute, concurrent), `daily` caps (requests, estimated tokens), `expires`, `revoked`. Reloaded on
  mtime change, so revoking is one edit with no restart. The existing provider token stays the `owner` principal with
  today's full rights.
- **`_authorized` returns a principal** (`owner` or a client id), checked with `hmac.compare_digest` against each
  hash; unknown or revoked → 401, expired → 401 with a fixed reason.
- **Scope for `grokbot`** ([ESCALATE] for the values; proposed defaults):
  - endpoints: `POST /v1/messages` only. Refused: `/v1/responses`, `/bridge/call`, `/resources`, `/v1/cortados/*`
    (usage, runs), `/v1/models` beyond listing `cortados`.
  - body: `tools` absent or empty, `tool_choice` absent (tool-less only, so no tool loop and nothing runs on the
    fleet); no `x-cortados-run-id` of another principal.
  - models: `cortados` (routed) only — no `sub/` pins, no `api/`, no local pins.
  - band ceiling: optional `max_band` so S1 can never put a Grok Bot subtask on the most expensive tier [ESCALATE].
  - caps: 10 requests/min, 2 concurrent, 200 requests/day, 2 M estimated input tokens/day, 50 k tokens per request.
    Breach → 429 with a fixed reason; counters persist in `$CORTADOS_HOME/client-usage.json` so a restart does not
    reset a day.
  - `expires` 30 days; renewal is a new token.
- **Kill switch:** `CORTADOS_CLIENTS=off` in the provider env refuses every non-owner principal.
- The hard rules in `core.resolve` are untouched; scope is enforced before any routing or spend.

## 7. What the bot may and may not do

May: send tool-less, self-contained prompts to `POST /v1/messages` with model `cortados` and read the text answer
and the `x-cortados-route` header; read `GET /health`.

May not: start child tasks (any mode, chrome above all); call `route_task` (it is a local stdio MCP tool); send tool
definitions; pin models; read the dashboard data, usage, scarcity, run records or hardware state; reach any port but
the provider's or any machine but the Air; hold the owner token. Any later widening (e.g. tools that run in Grok Bot's
own box) is a new scoped ticket.

## 8. Logging and attribution

- The principal, not the User-Agent, sets `client` for scoped tokens: route log `platform: "provider:grokbot"`.
- Run records and refusals carry the principal; `GET /v1/cortados/usage` gains `routed.by_client` (today / 7 d) and
  `clients` (per client: caps, used, remaining, last seen, revoked/expired), through the existing `_publish` schema.
- The dashboard shows a Grok Bot row: delegated calls, lanes/models used, refusals by reason, cap meters, next to the
  shared Cursor/Grok weekly %.
- Provider log lines name the principal and never the token.

## 9. Owner steps in the Tailscale console (corrected)

Do these only after G0–G4 are merged and deployed.

1. **Policy file — check the base rule.** If it contains the default allow-all (`"src": ["*"], "dst": ["*:*"]`
   or a grant with `"src": ["*"]`), replace it with `autogroup:member` as source (keeps the owner's devices working).
2. **Policy file — add** (placeholder in angle brackets; fill the Air's tailnet address in the console only):

   ```jsonc
   "tagOwners": { "tag:grokbox": ["autogroup:admin"] },
   "hosts":     { "cortados-air": "<Air tailnet address>" },
   "grants": [
     { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] },          // replaces the default "*" rule
     { "src": ["tag:grokbox"], "dst": ["cortados-air"], "ip": ["tcp:8432"] }
   ]
   // ssh: no rule with tag:grokbox or "*" as src
   ```

   A host alias for the Air avoids re-tagging it (tagging the Air would change its ownership and could affect its own
   SSH and access rules). Use the console's policy preview / tests (`"tests": [{"src": "tag:grokbox",
   "accept": ["cortados-air:8432"], "deny": ["cortados-air:22", "cortados-air:8431"]}]`) before saving.
3. **Device approval** on (Settings → Device management), if not already.
4. **Join:** in Grok Bot, open the Tailscale plugin and sign in as the owner. On the Machines page approve the
   device named `grokbot-…`, disable its key expiry only if wanted, and use "Edit ACL tags" to set `tag:grokbox`.
5. **Verify** (G5 acceptance) from the Grok Bot agent: `GET http://cortados-air:8432/health` ok; port 22 and 8431 on
   the Air, and any port on Pro/Studio, time out.
6. **Give the token** (G1) to Grok Bot by typing it into Grok Bot's own secure secret field yourself; never in chat,
   never via a Bot conversation.
7. **Revoke path:** remove the device on the Machines page and set `revoked: true` in `clients.json`.

Fallback if step 4's tag signs the device out (G5 finding): leave it user-owned and restrict it by its own host alias
as `src` instead of the tag.

Not needed: a reusable or pre-approved auth key, the Studio as destination, port 8433.

## 10. Build tickets

| # | Ticket | Acceptance check |
|---|---|---|
| G0 | Containment (owner, no code): retire Studio 8433 instance, rotate the two exposed tokens, set Grok Bot `localToolPermission` to ask/off on Studio and Pro | On the Studio, `lsof -nP -iTCP:8433 -sTCP:LISTEN` prints nothing; `launchctl print gui/$(id -u)/com.cortados.provider` fails there; Air provider still answers `/health`; S1 calls from the Air succeed with the new token |
| G1 | Client registry + principal auth in `server.py` (`clients.json`, hash-only, mtime reload, owner principal unchanged, kill switch) | New unit tests: valid/unknown/revoked/expired tokens; owner token behaviour byte-identical to today; full suite `python3 -B -m unittest discover -s tests -t .` OK |
| G2 | Scope enforcement: endpoint allowlist, tool-less body, model allowlist, `max_band`, `max_input_tokens`, foreign run id refused — all before routing/spend | Tests: each refused case returns 403/400 with a fixed reason and no route log row; `/bridge/call`, `/resources`, `/v1/responses` refused for `grokbot` |
| G3 | Rate, concurrency and daily caps with persisted counters; 429 on breach | Tests with a fake clock: 11th request in a minute → 429; daily cap survives a provider restart; counters in a throwaway `CORTADOS_HOME` |
| G4 | Attribution: principal-derived `client`, run records, `usage.routed.by_client` and `clients` section through `_publish`, dashboard row | Test: a scoped request logs `platform: provider:grokbot`; usage view lists it; no token or path in the published view |
| G5 | Tailnet join test (owner + agent, after G0–G4 deploy): native plugin, approve, tag, ACL with tests block | From the Grok Bot box: `/health` on Air:8432 ok; Air:22, Air:8431, any Pro/Studio port refused; device stays signed in after tagging (or fallback applied) |
| G6 | Grok Bot standing instructions (`docs/GROKBOT-INSTRUCTIONS.md`): request shape, tool-less only, token from secure storage, what 401/403/429 mean | Doc reviewed; a dry-run prompt in Grok Bot produces a correct request against a fake endpoint |
| G7 | Adversarial review of G1–G4 (auth surface), routed per the Codex-as-resource rule | Review findings resolved; evidence block from `.claude/skills/verify-router/SKILL.md` in the PR |
| G8 | Optional: HTTP MCP front for `route_task`-style delegation if `mcpBoxServers` can reach a tailnet MCP server | Spike note answering yes/no; no build without a new decision |

Invariant tests to add in G2: no HTTP path reaches `child_tasks.start_task`; a scoped principal can never select
`chrome`, pins or tools.
