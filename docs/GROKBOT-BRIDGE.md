# Bots on CortadOS — the bot gateway (Grok Bot first)

Status: PLAN ONLY (2026-11-03). Nothing here is built or deployed. Values marked [ESCALATE] are Forest's to set.
Machines are named Pro, Air and Studio only; no addresses, hostnames or secrets appear in this file.

## 1. Goal

Bots — Grok Bot first, others later — use CortadOS as their **main build tool**, the way Cursor gives its bots cloud
agents: whenever a bot needs work done, it hands the task to CortadOS, S1 routes it to the cheapest capable model, and
the result comes back. Each bot, on whatever computer it runs, reaches CortadOS through **one network MCP endpoint**
that exposes the tools the fleet already has:

- whole tasks: `start_task`, `task_status`, `wait_task`, `list_tasks`, `cancel_task` (`cortados_router/child_tasks.py`)
- text subtasks: `route_task` (`cortados_router/subagent_mcp.py`)

Grok usage as a pool resource is out of scope: Grok Bot has no API to drive, and the Cursor lane stays parked. Its
usage stays dashboard-only, as the Cursor weekly pool it shares (`usage.py` `SHARED_WITH`).

## 2. What exists today (findings, 2026-11-03)

- **Tools are local only.** `subagent_mcp.py` is a stdio MCP server; child tasks run as the *owner's* macOS user on
  whichever machine launched the server, with the owner's provider token in the child's settings file. A child in
  auto/acceptEdits mode with Bash can read anything the owner's user can (Claude Code's cwd rules are not an OS
  boundary). Fine for the owner; not for a bot.
- **One shared token per provider** (`Handler._authorized`); the client label is the User-Agent, which the caller
  asserts.
- **Air** runs the reviewed provider (`com.cortados.provider`, port 8432, `main` at `a75d304`, secrets from an env
  file, explicit CLI paths). It is the fleet's CLI host.
- **Studio** also runs `com.cortados.provider` — this repo's provider at the same commit — on **port 8433**, installed
  2026-11-02 19:49 from an undocumented folder `~/cortados-provider-svc/`, with its provider and S1 tokens **inline in
  the LaunchAgent plist**, no explicit CLI paths (first `claude`/`codex` on PATH), the Studio's large legacy
  `~/.cortados` as its state, and a token different from the Air's. No doc, commit or memory note mentions it; it has
  logged no traffic. Likely installed by a Grok Bot agent through the Studio's local-exec daemon (inferred, not proven;
  Forest to confirm). It is a second, unreviewed router that splits learned state and spends the Studio's logins.
- **Grok Bot** (`/Applications/Grok Bot.app`, v0.78.1, bundle id `com.anysphere.sand`: an Anysphere/Cursor Electron
  app on xAI's Grok models) is installed on Pro, Air and Studio. Its agents run in a cloud box; it has user-defined
  Bots, a secure secret field ("box secrets", keychain-backed), box MCP servers (`mcpBoxServers`, none configured), an
  egress tunnel setting (off), and a **local-exec daemon** in `~/.grokbot/` that lets the cloud agent run tools on the
  Mac. On the Studio its `localToolPermission` is `always`. It has a **native Tailscale plugin**: the admin signs in
  interactively, a device named `grokbot-…` appears on the Machines page for approval; there is no auth-key input.
- **Router Bot** is, by inference, one of the owner's Grok Bot Bots; its "secure card" is that secret field.
  `grokbox` is a tag name it chose; it appears nowhere else.
- Unknown: whether `mcpBoxServers` accepts a remote streamable-HTTP MCP server with a bearer header (ticket B1), and
  whether the Grok Bot device stays signed in after an admin tags it (ticket B9).

## 3. Architecture

```
  Grok Bot box (cloud) ──tailnet, tag:grokbox──┐
  Grok Bot on a fleet machine (Pro, Studio) ───┤   one port, bearer = per-bot token
  later bots ──────────────────────────────────┘
                                               ▼
  Air ─ bot gateway  (streamable HTTP MCP, /mcp, port 8434, runs as user `cortados-gw`)
          │  auth: per-bot token → bot principal + scope
          │  route_task  ─────────────► provider :8432 (bot's provider token, tool-less)
          │  start_task  ─► sudo -n -u cb-<bot> child runner (exact command only)
          ▼
        per-bot macOS user `cb-<bot>`: home = workspaces only
          ~cb-grokbot/work/<task_id>/<repo>   (git clone of an allowlisted URL)
          headless Claude Code child → provider :8432 (bot's child token) → S1 → cheapest capable lane
          results / logs ─► task dir ─► task_status / wait_task / task_log
```

### 3.1 One host: the Air

The gateway and every bot child run on the **Air**, next to the provider:
- It is the reviewed provider host and CLI host; children call the provider on the same machine, and every bot's
  spend lands in the same router state and caps as the owner's.
- The Pro is the owner's daily machine with all his folders and logins; a bot boundary there has the most to lose.
- The Studio is the S1 scorer; long builds there would compete with scoring latency (S1 budget is +1 s), and its home
  holds the legacy fleet state.
- Children need no vendor login: they talk to the provider by token, and the provider's lanes run under the owner's
  user as today. A bot user holds no Claude/Codex/Cursor credential. If build load outgrows the Air, a second child
  host is a later ticket; it keeps one gateway and one provider.

**Retire the Studio 8433 instance** (B0) rather than reuse it: consolidating on one provider keeps one learned state,
one scarcity view, one place to cap spend.

### 3.2 The endpoint: streamable HTTP MCP

- New stdlib module `cortados_router/bot_gateway.py`: MCP streamable HTTP transport on `POST /mcp` (JSON-RPC
  `initialize`, `tools/list`, `tools/call`; plain JSON responses, no SSE needed for these tools), `Mcp-Session-Id`,
  `Origin` check, request-size and time caps. It reuses `subagent_mcp`'s tool definitions and handlers, so stdio and
  HTTP expose one implementation per tool.
- Binds the Air's tailnet address, port **8434**, launchd LaunchDaemon with `UserName cortados-gw`.
- A bot on a fleet machine (Grok Bot's local daemon on the Pro or Studio) uses the **same** URL and its own token;
  there is no second path. The owner's own sessions keep the stdio server.
- `wait_task` keeps its 50 s request budget (`WAIT_MAX_S`), so long tasks are polled, as in the desktop app.

### 3.3 Per-bot principal and scope

Registry `/Library/Application Support/CortadOS/bots.json` (owned by `cortados-gw`, mode 0600, owner/mode checked
like `hardware.py`'s state tree, reloaded on mtime so revoking needs no restart). Per bot:

| Field | Meaning | Proposed for `grokbot` [ESCALATE] |
|---|---|---|
| `id`, `token_sha256` | principal; raw token never stored | `grokbot` |
| `tools` | MCP tools allowed | all six; `cancel_task`/`task_status` only on its own tasks |
| `chrome` | login-mode children | **never** (hard false for every bot; the schema cannot express true) |
| `repos` | git URLs a task may clone (exact match, https or ssh) | the repos Forest names |
| `push` | branch prefix the bot may push, or none | `bot/grokbot/*`, no force push, never default branch |
| `permission_modes` | child permission modes allowed | `acceptEdits`, `dontAsk` + reviewed `allowed_tools`; not `bypassPermissions` |
| `orchestrator` / models | routed only (`cortados`), no `sub/` or `api/` pins, optional `max_band` | routed, `max_band` [ESCALATE] |
| `max_concurrent` | running children | 2 |
| `daily` | tasks/day, route_task calls/day, child runtime minutes/day, estimated tokens/day | 20 / 200 / 300 min / 5 M |
| `max_task_s` | per-task wall clock (`CORTADOS_CHILD_MAX_S`) | 3600 |
| `expires`, `revoked` | lifecycle | 30 days |

Every check runs before any clone, spawn or routing. A breach answers a tool error with a fixed reason (`quota`,
`scope`, `revoked`), counted for the dashboard. Kill switch: `CORTADOS_BOTS=off` refuses every bot.

The gateway mints two **provider-side** credentials per bot, both registered with the provider's new per-client token
table (`$CORTADOS_HOME/clients.json`, hash only):
- `grokbot/route` — `POST /v1/messages`, tool-less, routed model only (used by `route_task`).
- `grokbot/child` — `POST /v1/messages` with tools plus `/bridge/call` for its own sessions (what a child's tool
  bridge needs); no `/resources`, `/v1/cortados/*`, `/v1/responses`.
The provider stamps `platform: provider:grokbot` from the token, enforces the same daily caps a second time, and the
owner's token keeps today's rights byte-for-byte.

### 3.4 Workspaces and repos

- `start_task` gains `repo` (an allowlisted git URL) and optional `ref`; free-form `cwd` is refused for bots.
- The runner, as `cb-<bot>`, clones into `~cb-<bot>/work/<task_id>/<name>` (shallow, `--no-recurse-submodules` unless
  scoped), then starts the child with that folder as cwd.
- Git credentials: a per-bot deploy key or fine-grained token in `cb-<bot>`'s own keychain, read access to the
  allowlisted repos, write only to the `push` prefix (enforced on the forge by branch rules where possible, and by the
  runner refusing any other refspec). No owner ssh-agent (`SSH_AUTH_SOCK` never passed, as for chrome children today).
- The bot user cannot read the owner's home (macOS user separation; home 0700) or other bots' homes. On top, children
  run under the Seatbelt profile already used by the outcome loop (`s1/outcome/containment.py`): writes only in the
  task folder and the user's caches, network only to the provider, the git forge and package registries [ESCALATE:
  registry list].
- Workspaces are swept after N days [ESCALATE: 7]; the whole `cb-<bot>` process set can be killed as a unit
  (`pkill -U`), which also closes the orphan gap noted in the outcome-loop follow-up.

### 3.5 Results and logs

- `task_status` / `wait_task`: as today (status, routed lane and model, result text), plus `branch` and `commit` when
  the task pushed.
- New `task_log` tool: the last N sanitised events of the child (tool names, file paths inside the workspace,
  errors), never env, tokens or paths outside the workspace.
- `list_tasks` returns only the calling bot's tasks.

### 3.6 Attribution

- Child-task events (`child-tasks.jsonl`) and run records carry `principal`; child traffic carries
  `x-cortados-run-id: <task_id>` as today, and the provider ties it to the principal from the token, not the header.
- `GET /v1/cortados/usage` gains `bots`: per bot tasks today / 7 d by status, route_task calls, lanes and models used,
  runtime minutes, refusals by reason, cap meters, last seen, revoked/expired — through the existing `_publish` schema.
- The dashboard shows one row per bot next to the shared Cursor/Grok weekly %.

## 4. Network boundary

Remote bots join the tailnet with a tag and may open exactly one port on one machine: the Air's gateway, 8434. The
bot never needs the provider port (8432) or S1 (8431); the gateway reaches the provider locally. Corrections to the
"Router Bot" request:

- Target is **Air : 8434** (the bot gateway), not Studio : 8433 (an instance to retire).
- Join with Grok Bot's **native Tailscale plugin** (admin sign-in + device approval), not an auth key handed to a Bot.
  An auth key in a Bot's secret store is a bearer credential a cloud agent could use to join any machine as the tag.
  Only if B9 shows the plugin cannot keep a tag: a one-off, ephemeral, tagged, **not pre-approved** key, 1-hour
  expiry, entered by the owner himself in Grok Bot's Tailscale UI, revoked after the join.
- Tailscale has no "deny" rule: policy is default-deny with allow-only rules. The default policy's allow-all rule
  (`src: ["*"]`) also matches tagged devices, so as long as it exists the tag reaches everything. Replace it first.
- No `ssh` rule may name the tag or `*` as source.

### Owner steps in the Tailscale console (after B0–B8 are deployed)

1. Policy file: if a rule has `"src": ["*"]` (the default allow-all), change its source to `autogroup:member`.
2. Policy file — add (fill the Air's tailnet address in the console; it is not written here):

   ```jsonc
   "tagOwners": { "tag:grokbox": ["autogroup:admin"] },
   "hosts":     { "cortados-air": "<Air tailnet address>" },
   "grants": [
     { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] },     // replaces the default "*" rule
     { "src": ["tag:grokbox"], "dst": ["cortados-air"], "ip": ["tcp:8434"] }
   ],
   "tests": [
     { "src": "tag:grokbox", "accept": ["cortados-air:8434"],
       "deny": ["cortados-air:8432", "cortados-air:8431", "cortados-air:22"] }
   ]
   // ssh: no rule with tag:grokbox or "*" as src
   ```

   A host alias for the Air avoids re-tagging the Air itself (which would change its ownership and its own rules).
3. Settings → Device management: device approval on.
4. In Grok Bot open the Tailscale plugin and sign in as the owner. On the Machines page approve the device named
   `grokbot-…` and use "Edit ACL tags" to set `tag:grokbox`. (Fallback if tagging signs it out: keep it user-owned,
   add it as a host alias and use that alias as `src`.)
5. Verify from the bot: MCP `initialize` on Air:8434 succeeds with its token; Air:8432, Air:22 and any Pro/Studio port
   time out.
6. Put the bot token into Grok Bot's own secure secret field yourself — never in chat, never via a Bot conversation.
7. Revoke: remove the device on the Machines page and set `revoked: true` for the bot in `bots.json`.

Not needed: a reusable or pre-approved auth key, the Studio as destination, port 8433.

## 5. Build tickets, in order

| # | Ticket | Acceptance check |
|---|---|---|
| B0 | **Containment (owner, no code)**: retire the Studio 8433 instance (`launchctl bootout gui/$(id -u)/com.cortados.provider` there, move its plist out, delete `~/cortados-provider-svc/provider-token`); rotate the S1 token and the Studio provider token that sat inline in that plist; set Grok Bot `localToolPermission` to ask (or local exec off) on Studio and Pro | On the Studio `lsof -nP -iTCP:8433 -sTCP:LISTEN` prints nothing; Air `/health` ok; routed request through the Air still scores with the new S1 token |
| B1 | **Spike: Grok Bot remote MCP.** Can `mcpBoxServers` (or the local daemon) call a streamable-HTTP MCP server with a bearer header? Test against a throwaway local fake server, no fleet access | Note in this doc: yes/no, header name, transport version; if no, choose the fallback (bot calls a small HTTP JSON wrapper of the same tools) before B4 |
| B2 | **Provider per-client tokens**: `clients.json` (hash only, mtime reload), `_authorized` returns a principal, principal sets `client`, scopes `route` and `child` as in 3.3, caps with persisted counters, kill switch; owner token unchanged | Unit tests for valid/unknown/revoked/expired, each refused endpoint, 429 on caps, counters survive restart; owner-path tests byte-identical; full suite `python3 -B -m unittest discover -s tests -t .` OK |
| B3 | **Bot registry and scope engine** (`bots.json`, schema with no way to express chrome, repo/push/permission/model/caps checks) as a pure module | Table-driven tests: every scope field refuses out-of-scope input before any side effect |
| B4 | **`bot_gateway.py`**: streamable HTTP MCP transport, bearer auth to a bot principal, reuses `subagent_mcp` tool handlers, `list_tasks`/`task_status`/`cancel_task` filtered to the caller | Tests with a fake child runner: `initialize`/`tools/list`/`tools/call` round trip; foreign task id → not found; no HTTP path reaches chrome mode (invariant test) |
| B5 | **Per-bot OS user and runner**: `cb-<bot>` creation script (no admin, home 0700, no login shell), sudoers rule for the exact runner command, clone of allowlisted repo into the per-task workspace, per-bot git credential, push-prefix refusal, Seatbelt profile, workspace sweep | Tests with a fake git/claude; manual check on the Air: as `cb-grokbot`, `ls ~owner` is denied; a task cannot clone a non-allowlisted URL or push to `main` |
| B6 | **Results and logs**: `task_log` (sanitised tail), `branch`/`commit` in status | Tests: no env value, token or out-of-workspace path appears in `task_log` output |
| B7 | **Attribution**: `principal` in child-task events and run records, `usage.bots` section, dashboard row | Test: a fake bot task appears under `bots.grokbot` with lane, model, runtime and cap meters; `_publish` drops anything undeclared |
| B8 | **Adversarial review** of B2–B6 (auth, sandbox, git push), routed through S1 per the Codex-as-resource rule; then deploy on the Air (LaunchDaemon for the gateway, provider restart with `clients.json`) | Findings resolved; evidence block from `.claude/skills/verify-router/SKILL.md` in the PR; `/health` on 8432 and MCP `initialize` on 8434 from the Pro |
| B9 | **Tailnet join** (owner steps in section 4) and end-to-end test from Grok Bot: `start_task` on an allowlisted test repo, `wait_task` to done, branch pushed under `bot/grokbot/` | Bot gets result + commit; denied ports time out; dashboard shows the task under `grokbot`; device stays tagged (or fallback applied) |
| B10 | **Bot instructions** (`docs/BOT-INSTRUCTIONS.md`): what to delegate, tool shapes, polling `wait_task`, what `quota`/`scope`/`revoked` mean, token only from secure storage | A dry-run in Grok Bot against a fake gateway produces correct calls |
| B11 | Later: onboarding a second bot = one `bots.json` entry, one `cb-<bot>` user, one tag + grant | Checklist in the doc; no code change needed |
