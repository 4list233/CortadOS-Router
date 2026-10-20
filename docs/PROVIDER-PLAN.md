# CortadOS as a model provider: plan

Status: P0 built, acceptance check passed 2026-10-20 (real Claude Code 2.1.304 client → `cortados` → headless
`claude -p --model haiku`; the client ran the Read, the headless CLI wrote the answer). Supersedes `plan/subagent-mcp` as the first standalone use of the router;
that branch stays parked.

**Goal.** Claude Code, Codex or any coding tool points at a local CortadOS endpoint and sees one model, `cortados`
(display name CortadOS). For each conversation S1 scores the task and the router picks a lane from the resource pool:
a headless subscription CLI (Claude Code, Codex) or a local model. The client keeps running its own agent loop and
its own tools; the chosen model only thinks.

**Forest's decisions (2026-10-20):**
- Not tied to Hermes. The Hermes plugin is frozen and removed later.
- Subscription CLIs are first-class backends, and their tool calls run in the **client** (the tool bridge), under the
  client's own permission prompts. The headless CLI never touches the project's files.
- One model name, `cortados`. No tiers: S1 sets the band.
- Provider first on one login per vendor; multiple accounts per vendor (ROUTER-DESIGN §10) next.
- Vendor terms: confirmed by Forest as compatible.
- S1 keeps training nightly on the Studio (or this Mac), from the route log the endpoint writes.

## How the bridge works (verified 2026-10-20, Claude Code 2.1.304)

```
client (Claude Code, the repo's machine)           CortadOS endpoint
  POST /v1/messages {system, tools, messages} ──►  new conversation → spawn headless CLI in an empty scratch dir:
                                                     claude -p --input-format/--output-format stream-json
                                                       --tools ""                     (no built-in tools)
                                                       --strict-mcp-config --mcp-config <bridge shim>
                                                       --setting-sources "" --settings {"disableAllHooks":true}
                                                     the shim lists the CLIENT's tools as MCP tools
  ◄── tool_use {Read, file_path}  ─────────────────  CLI calls mcp__bridge__Read; the shim blocks on the endpoint
  runs Read locally
  POST /v1/messages [..., tool_result] ──────────►  result handed to the waiting shim call → CLI continues
  ◄── text, end_turn ─────────────────────────────  CLI's final text for the turn
```

- Each MCP `tools/call` carries `_meta["claudecode/toolUseId"]`, the same id as the `tool_use` block on the CLI's
  stdout, so results are matched exactly, never by order.
- Isolation, measured: `--setting-sources ""` with `disableAllHooks` stops the user's hooks and CLAUDE.md reaching the
  child. `--bare` is NOT usable: it disables subscription OAuth.
- The child's environment drops `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY`: the first two
  would loop the child back into CortadOS, the third would bill an API key instead of the subscription.
- Codex as a backend uses `codex app-server` dynamic tools (`item/tool/call` server requests). Experimental: P2
  starts with a spike.

## Phases

| # | Phase | Acceptance check |
|---|---|---|
| P0 | Anthropic Messages endpoint → bridged `claude -p`; no routing | `ANTHROPIC_BASE_URL=http://127.0.0.1:<port> claude -p --model cortados "summarize README.md"` from a repo: the Read runs in the client, the answer comes from the headless CLI |
| P1 | Router in the loop: host-neutral `route_task()`, S1 scores the first request, one model per conversation, route log with `platform: "provider:<client>"`; local-model backend (Ollama / llama.cpp tool calls) | requests of different difficulty land on different lanes; every decision is in the route log |
| P2 | Codex backend (app-server spike first); Codex as a client (OpenAI Responses wire) | the same summarize check from Codex and onto Codex |
| P3 | Codex `/status` parser fix (SUBAGENT-MCP-PLAN §11.7) and inventory refresh, alongside P1–P2 | codex lane shows a non-null scarcity |
| Later | multiple accounts per vendor; CLI runners on other machines (the Air); one model across machines; cloud clients (needs a public endpoint with auth, and a verified `BASE_URL` override in the cloud client); API-key lanes | |

## P0 known limits (each removed by a later phase or a named follow-up)

- No routing: every conversation goes to the Claude CLI (`CORTADOS_PROVIDER_CLAUDE_MODEL`, else the CLI's default).
- A conversation lives in one CLI process and is identified by the exact sequence of replies the endpoint wrote
  (text and tool_use ids). If the endpoint restarts, or the client compacts or edits its history, the next request
  fails with 409 instead of rebuilding the session from the transcript. Two live conversations with identical
  histories (same opening, same text-only replies) are refused with 409 rather than guessed between.
- Requests without tools (titles, summaries) always get their own throwaway CLI with the conversation as text.
- Only `tool_choice: auto` is accepted (400 otherwise). `max_tokens` is not enforced: the CLI has no output-cap flag.
- Responses are sent as SSE after the turn completes (with pings while waiting), not token by token.
- Text a client appends next to tool results (Claude Code's reminders) is folded into the last tool result.
- `count_tokens` returns a character-based estimate (chars / 4), marked as an estimate.
- Mid-conversation `role: "system"` messages (Claude Code sends one per turn) reach the CLI as user text wrapped in
  `<system-message>`, because the CLI's stream-json input only takes user messages.
- Claude Code warns that it does not recognize `cortados` and assumes a 200k context window. P1 sets the window
  for the gateway model (see the link in that warning) to match the chosen lane.
