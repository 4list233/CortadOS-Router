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
- Codex as a backend uses `codex app-server` dynamic tools (`item/tool/call` server requests): the thread starts
  with the client's tools as `dynamicTools` and every built-in tool feature off. Verified by the P2 spike (P2 status).

## Phases

| # | Phase | Acceptance check |
|---|---|---|
| P0 | Anthropic Messages endpoint → bridged `claude -p`; no routing | `ANTHROPIC_BASE_URL=http://127.0.0.1:<port> claude -p --model cortados "summarize README.md"` from a repo: the Read runs in the client, the answer comes from the headless CLI |
| P1 | Router in the loop: host-neutral `route_task()`, S1 scores the first request, one model per conversation, route log with `platform: "provider:<client>"`; local-model backend (Ollama / llama.cpp tool calls) | requests of different difficulty land on different lanes; every decision is in the route log |
| P2 | Codex backend (app-server spike first); Codex as a client (OpenAI Responses wire) — built 2026-10-21, both live acceptance checks passed the same day (P2 status below) | the same summarize check from Codex and onto Codex |
| P3 | Codex `/status` parser fix (SUBAGENT-MCP-PLAN §11.7) and inventory refresh, alongside P1–P2 | codex lane shows a non-null scarcity |
| Later | multiple accounts per vendor; CLI runners on other machines (the Air); one model across machines; cloud clients (needs a public endpoint with auth, and a verified `BASE_URL` override in the cloud client); API-key lanes | |

## P1 status (2026-10-20)

Built: `cortados_router/provider/routing.py` routes each new conversation once through
`execution_routing.select` (the router's one execution policy), limited to the routes this endpoint serves. S1 scores
the first user message without the client's injected `<system-reminder>` context. The decision goes to the route log
with `platform: provider:<client>` (the client comes from its User-Agent), and `log_execution` records it once the
first turn completes, so the nightly retrain sees provider traffic. Each response carries
`x-cortados-route: <provider>/<model> band=<band> id=<route_id>`. `--fixed-model` (or
`CORTADOS_PROVIDER_FIXED_MODEL`) turns routing off for bridge testing.

Verified with the real Claude Code client on throwaway state; on fixed S1 answers an easy request routes to
`claude-sonnet-5` (R0, held up by the interactive floor) and a hard one to `claude-opus-5-5` (R3, required index 78.5).
Tool-less calls (titles, summaries) are scored and logged as their own last user message, not as the conversation's
task.

**S1 down → a Codex lane (P2), 503 in P1.** The hard rule "a degraded scorer never defaults to Claude" is enforced
by this host: execution mode skips the table's non-Claude fallback routes, so while S1 is unavailable Claude routes
are not offered. In P1, with only Claude served, that meant 503 until S1 was back. Since P2 a degraded conversation
runs on a Codex lane (the table's fallback band, cheapest capable `codex-cli` model with live capacity); the endpoint
answers 503 only when no Codex lane survives either (the Codex binary missing from PATH, or its capacity spent), and
the error names both rules. The first real-client run, before this rule was enforced, had routed degraded to
`claude-sonnet-5`; Codex's P1 review caught it.

Not yet in P1, each needing a decision or another phase:
- **Served lanes.** `claude-cli` and, since P2, `codex-cli`: every `openai-codex` row of the tables is served as its
  `codex-cli` twin through the app-server bridge (`openai-codex` itself, the Hermes native-API provider, is never
  served). Local models need the 2026-09-28 ruling ("local models are OUT of routing",
  `tables/benchmarks/build_tiers.py`) lifted, local entries in the model scores, and the local-resources work on
  `main` (uncommitted) merged.
- **S1 location.** The endpoint scores through `CORTADOS_S1_URL` (default `127.0.0.1:8431`). When the endpoint does
  not run next to S1 (the Studio), point it there, or every conversation routes degraded.
- **Live usage probes.** Routing reads live lane scarcity, which samples the vendor CLIs in a PTY every 20 minutes,
  exactly as under Hermes. Run verification on a throwaway `CORTADOS_HOME` with a fresh `scarcity.json`.
- **Interactive floor for subagents.** Every provider conversation is scored as interactive (`platform: cli`), so the
  face floor keeps cheap models out even for a client's subagents.
- **Context window.** Claude Code still assumes 200k for `cortados`.

## P0 known limits (each removed by a later phase or a named follow-up)

- Routing: see P1 status above. `--fixed-model` restores the P0 behaviour (one Claude CLI model, no routing).
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

## P3 status (2026-10-21): the Codex `/status` parser

Built from one real capture of Codex 0.163.0 (Forest approved the probe; the redacted screen is
`tests/fixtures/codex-status-0.163.0.txt`). `cortados_router/scarcity_codex.py` is the one Codex screen parser:

- A pool label is the text on the same line as `limit:`, matched against `^(GPT-… )?(5h|Weekly)$` with runs of
  spaces collapsed. The session uuid above the entry is no longer a pool (P1).
- `resets 12:01 PM on 27 Oct` reads as a date; a weekly reset must land inside the week (plus an hour of slack),
  a 5-hour reset inside its five hours. Missing reset text is allowed only for an untouched window (P1b).
- An unknown label, unreadable or leftover reset text, an out-of-range reset, or a CLI version not in
  `PINNED_VERSIONS` raises; `_codex` then keeps the last good reading marked with the error, so the lane goes stale,
  never fresh-and-wrong. The screen's `OpenAI Codex (vX.Y.Z)` line is the version source; the reading records it.
- A third fault found by the capture: the probe's ready pattern was `model:` and the screen prints `Model:`, so every
  probe waited out the full 20-second boot and retried (67 s a sample). It is case-insensitive now.
- Codex's adversarial review of the first build (2026-10-21) found that a refused screen left the previous reading
  "fresh" for routing for up to six hours, that a wrapped entry could borrow the next entry's figures, that truncated
  or misplaced reset text passed as "no reset", that a quota line the grammar could not read vanished silently, and
  that `25:00 PM` was normalised to 13:00. All five are fixed with regression cases: an entry ends at the next
  `Name: value` field and must read completely; any `limit:` line must parse; clock values are validated before
  conversion; and `lane_scarcity` treats a lane whose latest try failed as stale at once, for every vendor.
- Its second pass found the staleness incomplete: the stale lane kept its window and "expiring unused" credit, and
  the scored selector still paid the tokens-left credit on an unknown pool, so a failed Codex sample could beat
  Claude. Now every routing signal goes with freshness (a stale lane has no window, no expiring credit, and earns no
  capacity credit); a digit-leading `5h limit:` line ends the entry above it; and `(resets )` is an error, not "no
  reset". Both selectors have resolve-level regressions.
- Passes three to five went after the reset arithmetic: a time-only reset read during its displayed minute rolled to
  tomorrow; `11:59 PM` read at 00:00:01 and `31 Dec` read on 1 Jan anchored to today's date or this year; naive
  wall-clock subtraction misjudged windows across a DST change; the hour DST repeats was guessed; the hour DST skips
  normalised to a real time. `reset_time` now turns every calendar reading the screen could mean (yesterday, today,
  tomorrow; last, this, next year; both folds) into a timestamp, drops any that does not round-trip, keeps the earliest
  not more than the slack hour in the past, bounds it by the window in elapsed seconds, and refuses two readings inside
  one window. Tests pin America/Toronto for the midnight, year-end and both DST cases. The sixth pass approved.

Done-when, on the real capture at today's clock: window `all:week`, `resets_at 2026-10-27 12:01` as on the screen,
scarcity −0.6 (non-null). Parser fixtures run at a frozen clock (the capture day) so they never age.

Not done here: the port into the live fleet's copy (`plugins/cortados-router/scarcity.py` on the Studio, hunk by
hunk, with the three live-only deltas kept and a `hermes gateway restart` at an idle point) waits for Forest's
decision on whether the frozen Hermes fleet still gets fixes.

## P2 status (2026-10-21): the Codex backend and Codex as a client

Built on `p2-codex`: `cortados_router/provider/codex_bridge.py` (`CodexSession`: one `codex app-server` thread per
conversation, the client's tools as dynamic tools), `responses_wire.py` (the OpenAI Responses wire, served at
`POST /v1/responses`), the backend map and the per-wire seam in `server.py` (`SESSION_FACTORIES`, `WIRES`), and
`codex-cli` in `routing.py`. Tests run on `tests/fixtures/fake_codex_app_server.py` (the exact line shapes of the
spike log) and the fake Claude CLI; no test opens a vendor CLI. Suite: 291 run, OK, 7 skipped (Hermes integration).
Both live acceptance checks passed on 2026-10-21 (P2 live findings below); each spends subscription quota.

**The app-server protocol** (verified against Codex CLI 0.163.0 with the spike; the API is experimental, so
`SUPPORTED_CLI_VERSIONS` pins the version the same way the `/status` parser does):
- stdio, one JSON object per line. `initialize {capabilities: {experimentalApi: true}}` (without it,
  `thread/start.dynamicTools` is refused with -32600) → `initialized` → `thread/start {cwd: an empty scratch dir,
  ephemeral: true, sandbox: "read-only", approvalPolicy: "never", model, baseInstructions: the client's system
  prompt (it replaces Codex's own base prompt), dynamicTools}` → one `turn/start {threadId, input}` per client
  request. `ephemeral` writes no session files (verified).
- A tool call is the server request `item/tool/call {callId, tool, arguments}`. `callId` equals the
  `dynamicToolCall` item id and becomes the `tool_use` id (Messages) or `call_id` (Responses) the client sees, so
  the result is matched by id, never by order. It is answered when the client's next request arrives:
  `{success: not isError, contentItems: [inputText | inputImage]}`; images travel as data URLs, because Codex
  refuses remote image URLs. Sibling calls of one model message are collected for the same settle window as the
  Claude bridge. Every pending call must be answered in one request (Codex continues only then); a partial set is
  refused.
- Final text is `item/completed` with an `agentMessage` (deltas are ignored). `turn/completed` with status
  `completed` ends the turn; `failed` is a 502 carrying the message and `codexErrorInfo` (`usageLimitExceeded`,
  `rateLimitExceeded`, `contextWindowExceeded`, `unauthorized`).
- `thread/tokenUsage/updated` carries cumulative totals once per model request; a client request's usage is the
  delta of `total` between turn boundaries. A `tool_use` reply therefore reports 0/0 when the server has not yet
  reported that model request, and the following reply carries the whole turn.
- Server requests that would hang the turn are answered defensively. `item/tool/requestUserInput` gets empty answers
  (`request_user_input` is the one built-in tool that cannot be switched off; a warning is logged). Approval requests
  are declined and raised as an invariant violation, like any item a built-in tool produces (`commandExecution`,
  `fileChange`, `mcpToolCall`, `webSearch`, `imageView`, `collabAgentToolCall`, `subAgentActivity`), and like
  `mcpServer/startupStatus/updated` (an MCP server started = isolation failed). The headless Codex never runs a
  tool itself, the same invariant as the Claude bridge.
- A new user message while a tool call is unanswered (a client interrupt): every pending call is answered
  `success: false` with "cortados: the client moved on without a result", then `turn/interrupt`, drain to that turn's
  `turn/completed`, then the new `turn/start`. A `turn/start` during a live turn would be steering, not a new turn.
- Close: `thread/unsubscribe`, close stdin ("stdin closed" ends the process), kill after 5 s.

**Isolation of the child.** There is no `--ignore-user-config` for `app-server`, and a throwaway `CODEX_HOME` has no
login; refresh tokens are single-use, so a copied or linked `auth.json` could break Forest's own login and is never
done. The backend therefore runs on the real Codex home with everything switched off on the command line:

```
codex app-server -c notify=[] -c web_search="disabled" -c include_environment_context=false -c project_doc_max_bytes=0
  -c model_provider="openai"                 # the built-in provider: a config.toml model_provider cannot redirect the lane
  -c mcp_servers.<name>.enabled=false        # for every [mcp_servers.<name>] table of $CODEX_HOME/config.toml
                                             # (the one read of it; `-c mcp_servers={}` merges and does nothing)
  --disable shell_tool --disable unified_exec --disable unified_exec_tty --disable view_image --disable image_generation
  --disable apps --disable plugins --disable hooks --disable multi_agent --disable sleep_tool --disable skill_search
  --disable tool_suggest --disable goals --disable memories --disable browser_use --disable computer_use
  --disable collaboration_modes
```

- With that set the tool list Codex builds is exactly `request_user_input` plus the dynamic tools (verified through
  an exec capture under the same flags). `code_mode_host` stays on: dynamic tools are served through it, and the
  first spike run, with it disabled, had the model report the Read tool as unavailable.
- The child's environment drops `OPENAI_API_KEY`, `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` (an API key would bill an
  API account instead of the subscription), `OPENAI_BASE_URL` (it would repoint the openai provider) and the Anthropic
  trio of the Claude bridge.
- The command line is asked for, then checked (Codex review of PR #5): before `thread/start` the bridge calls
  `account/read {refreshToken:false}` and requires a `chatgpt` login (an API-key login or none is a 502: the lane bills
  the subscription), and `config/read`, the effective config with every layer merged (config.toml, the `-c`
  overrides, a managed `requirements.toml`), and requires `model_provider="openai"`, no redefined
  `model_providers.openai`, the default `chatgpt_base_url`, the four overrides above, every `mcp_servers.*.enabled`
  false and every disabled feature false in `features`. `thread/start` is when Codex starts MCP servers and binds the
  provider, so a refusal here means nothing ran. The config.toml reader (which names the servers to disable) is the
  pre-spawn best effort; a server it could not name (`["mcp_servers".x]`, a dotted key, another layer) is caught here.
  Verified on 0.163.0: `config/read` reflects the `-c` overrides and `--disable` flags exactly.
- Second review pass: a client that disconnects before the first SSE byte closes its one-shot backend (the write of
  `response.created` / `message_start` is inside the cleanup); the drain after `turn/interrupt` and `run_turn`'s
  wait both check the absolute deadline on every iteration, so a server that streams events without end cannot hold
  the request thread, the session lock or the backend past `TURN_TIMEOUT_S`.
- Two leaks remain under the real home and are documented, not hidden: `~/.codex/AGENTS.md` is loaded
  (`instructionSources`, logged at thread start; it reaches the thread as a user message) and the user's skills list
  is injected as a developer message. The fix is `CORTADOS_PROVIDER_CODEX_HOME` / `--codex-home`: a dedicated
  `CODEX_HOME` Forest logs into once (`CODEX_HOME=<dir> codex login`), the multi-account path of ROUTER-DESIGN §10.
- `--codex-bin` / `CORTADOS_PROVIDER_CODEX_BIN` names the binary (default `codex` on PATH). Scarcity's
  `CORTADOS_CODEX_BIN` is a different setting: it points at the ChatGPT.app copy for the `/status` probe.
- Models the account offers (`model/list`) are the routing table's `openai-codex` rows (gpt-6.1-sol, gpt-6-astra,
  gpt-6-sol, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna). `effort` goes on `turn/start` only when the
  route carries one.

**The spike** (Forest approved; two live runs on `gpt-5.6-luna`, about 14k tokens in all; the second run's event
stream is the model for the fake). Run one, with `code_mode_host` disabled, answered that it had no Read tool. Run
two, with it on, made one dynamic `Read` round trip (`item/tool/call` → the client's answer → `agentMessage`) and
answered from the file; `instructionSources` listed `~/.codex/AGENTS.md`, `ephemeral` left no session file, and the
usage, item and turn shapes above are from its log.

**Codex as a client: `POST /v1/responses`** (verified from one real request of Codex 0.163.0, kept with its prose
replaced as `tests/fixtures/codex-responses-request-0.163.0.json`). Headers: `authorization: Bearer <env_key>`,
`user-agent: codex_exec/0.163.0 (...)` (the route log's platform is `provider:codex_exec`). Body: `model:
"cortados"`, `stream: true`, `input`, `tools`, `tool_choice: "auto"`, `store: false`, `include:
["reasoning.encrypted_content"]`, `prompt_cache_key`. There is no `instructions` key: the system prompt arrives as
`developer` message items (Codex's base prompt, then the skills block), followed by `user` items (the AGENTS.md
block, then the prompt). The wire folds the developer items into the session's system prompt, scores the user and
assistant text, and answers with `response.created`, per block `response.output_item.added` and `.done` (Codex
builds its history from the done items; a `function_call`'s `call_id` is the session's tool_use id), then
`response.completed` with the usage; `: ping` comment lines keep the stream alive while a turn runs. A 400 (Codex
prints the body verbatim) refuses a model other than `cortados`, `previous_response_id`, a `tool_choice` other than
auto, assistant prefill, and `namespace` tools (ChatGPT apps; run Codex with `--disable apps`). Non-function tools
(`web_search`) are dropped with one warning. Every error on the path, the 401 included, is in the Responses shape.

Codex's configuration is Forest's to add; this repo never writes `~/.codex`. Inline, nothing on disk:

```
CORTADOS_PROVIDER_TOKEN=<token> codex exec --ignore-user-config \
  -c 'model_providers.cortados={ name="CortadOS", base_url="http://127.0.0.1:8432/v1", env_key="CORTADOS_PROVIDER_TOKEN", wire_api="responses", request_max_retries=0, stream_max_retries=0, stream_idle_timeout_ms=1800000 }' \
  -c model_provider="cortados" -m cortados -c model_context_window=200000 \
  --disable apps --disable multi_agent -c web_search="disabled" -s read-only --ephemeral --skip-git-repo-check \
  "summarize README.md" < /dev/null
```

Or as a profile file, `$CODEX_HOME/cortados.config.toml`, used with `codex --profile cortados` (0.163.0 refuses
`[profiles.X]` tables; `wire_api = "chat"` is gone, so the Responses wire is the only one):

```toml
model = "cortados"
model_provider = "cortados"
model_context_window = 200000

[model_providers.cortados]
name = "CortadOS"
base_url = "http://127.0.0.1:8432/v1"
env_key = "CORTADOS_PROVIDER_TOKEN"
wire_api = "responses"
request_max_retries = 0            # a CortadOS turn is minutes long and not idempotent: no silent re-run
stream_max_retries = 0
stream_idle_timeout_ms = 1800000   # must exceed the endpoint's turn timeout (CORTADOS_PROVIDER_TURN_TIMEOUT_S, 1800 s)
```

A `model_catalog_json` entry for `cortados` would silence "Model metadata for cortados not found" and set the shell
tool type; optional, later.

**P2 live findings (2026-10-21, acceptance runs on the real app-server).**
- A dynamic tool whose name is one of Codex's own tool names (`exec_command`, `write_stdin`, `view_image`,
  `request_user_input`, the goal tools, ...) passes `thread/start` but is never offered to the model, even with the
  built-in's feature disabled: the model answered "I can't access README.md". The same tool declared as
  `client_exec_command` was called at once and the turn succeeded. Codex as a client sends exactly those names, so the
  bridge declares colliding client tools under `client_<name>` (`codex_bridge.CODEX_TOOL_NAMES`, `tool_aliases`) and
  maps `item/tool/call` back to the client's name. A collision with a Codex name outside that set is undetectable
  from the bridge (known limit).
- Codex registers its MCP *resource* tools (`list_mcp_resources`, `list_mcp_resource_templates`,
  `read_mcp_resource`, items of type `mcpToolCall` with server `codex`) whenever config.toml declares MCP servers,
  disabled or not. They run inside the app-server against servers the effective-config check proved disabled, so the
  bridge logs them instead of treating them as a leak (`BENIGN_MCP_TOOLS`); every other `mcpToolCall` is still fatal.
- Check 3 (Claude Code → `cortados` → Codex backend) passed: the client's Read ran, the answer was the heading,
  route `codex-cli/gpt-6.1-sol` (S1 down on the build Mac, so a degraded decision). Check 2 (Codex → `cortados` →
  Codex backend) first failed on the two findings above and passed after the fix: the Codex client ran the
  `exec_command` (`sed` on README.md) itself, the backend answered the heading, Codex reported 23,161 tokens and
  showed no error for the `: ping` comments or `response.created`. The endpoint ran on a throwaway `CORTADOS_HOME`
  with `CORTADOS_SCARCITY_REFRESH=off`. Spend for the whole P2 acceptance, Pro plan: two turns on gpt-6.1-sol plus
  five spike turns on gpt-5.6-luna, roughly 100k tokens in all.
- Scripting `codex exec`: it reads stdin until EOF ("Reading additional input from stdin...") and hangs when stdin is
  open, so redirect `< /dev/null`; macOS has no `timeout`, use `perl -e 'alarm N; exec @ARGV' --`.

**P2 acceptance** (run 2026-10-21, both passed; they spend quota, so rerun only on request):
1. Start the endpoint: `CORTADOS_PROVIDER_TOKEN=<token> python3 -B -m cortados_router.provider.server`
   (`--codex-home <dir>` for the dedicated home).
2. From Codex: the inline `codex exec` command above, from a repo. Pass: the `exec_command` runs in Codex, the
   answer comes from the lane the server log names in `x-cortados-route`, and Codex shows no error for the `: ping`
   lines or `response.created` (both unverified against its parser until this run).
3. Onto Codex: `ANTHROPIC_BASE_URL=http://127.0.0.1:8432 ANTHROPIC_AUTH_TOKEN=<token> claude -p --model cortados
   "summarize README.md"` with a request S1 routes to a Codex lane. Pass: the header reads `codex-cli/<model>`, the
   Read ran in the client, the log shows `instructionSources` and no isolation error.

**P2 known limits.**
- A running CLI keeps the system prompt it started with. A continuation whose system text differs (a changed
  Responses `instructions`, a rewritten developer item before the first reply) is served under the original with a
  WARNING naming the conversation; a developer item after a reply is passed as operator text instead (above).
- Usage: both bridges report the Messages shape (input excludes cache reads/writes); the Responses wire adds them
  into `input_tokens` and reports the cached part as `cached_tokens`, so Codex's totals match what the backend
  billed. The Codex bridge takes `cachedInputTokens` out of Codex's inclusive `inputTokens` for the same reason.
- Images: a tool-less single-message request keeps its image blocks on both wires; the text transcript a longer
  tool-less conversation is folded into is text only (P0 behaviour, both wires; images are P4).
- No resume with tools: `thread/resume` and `thread/fork` take no `dynamicTools`, so a restarted endpoint answers
  409 for a tool-bearing Codex conversation, exactly as P0 does for Claude.
- `request_user_input` cannot be switched off. The backend answers it empty and logs a warning, so a model that asks
  the user a question gets no answer and carries on.
- A tool-less request (Codex's own summaries, Claude Code's titles) boots a whole app-server for one turn.
- `account/rateLimits/updated` arrives on every turn with `primary`/`secondary {usedPercent, windowDurationMins,
  resetsAt}`: a structured quota reading for P3's scarcity, later. Today the `/status` parser is the source.
- Under the real Codex home the thread sees `~/.codex/AGENTS.md` and the skills list (above); the dedicated home
  removes that.
- The usage on a `tool_use` reply can be 0/0 (the delta rule above); clients that bill per message see the turn's
  tokens on the next reply.
- Unverified until the live checks: that Codex's SSE parser ignores `: ping` comments and accepts
  `response.created`; and that the app-server accepts a dynamic tool named `request_user_input` (Codex as a client
  sends that function tool, and the backend declares it beside the built-in of the same name; the fake accepts it,
  the real server may refuse it as reserved, which would show as a 502 at thread/start and would mean dropping that
  tool on the wire).

## S1 track (Forest's decisions 2026-10-21)

Today the teacher sees only the request text and `unit_kind`, while S1 can also receive a task context: the two are
trained on different inputs. Teachers are hardcoded (`claude-sonnet-5`, `claude-opus-5`), and the scarcity gate
reads the Claude lane only.

| # | Change |
|---|---|
| S1-a | Teachers resolved nightly from the model inventory, never hardcoded; labelling fails loudly if one can't be resolved. Holdout: Claude Fable 5.1, one teacher at a time; a change re-labels the holdout. Train: current top Claude model or `gpt-6-astra` by scarcity. Before a second teacher's labels count, both label a shared overlap set and their agreement is recorded. |
| S1-b | One routing input for S1 and teacher: request, compact tool/MCP inventory (names by server), environment (repo, cwd, none). The teacher also labels the expected steps / tool calls. Fits S1's 6,000-character budget. |
| S1-c | Provider requests, with their tool lists, are written to the route log and become training data (route logging landed in P1; the tool list is not logged yet). |
| S1-d | Outcome benchmark: runnable real tasks (coding in a disposable worktree, graded by tests plus a judge rubric), each run at the routed band and one band either side; the lowest band that succeeded becomes a label that outranks teacher opinion. Outward tasks run against mock MCP tools that record calls only. |
| S1-e | Surplus scheduling: when a pool is projected to reset with allowance unused, extra labelling and S1-d runs draw on it, across all accounts once multi-account lands. Stops once the pool's projected use reaches 95 %, and never runs in the last hour before that pool's reset. |
| S1-f | Manual training, on demand, on the Pro or the Studio (below). |

### S1-f: manual training and machine roles

The M5 Max Pro is the stronger trainer but is in daily use; the Studio is always on and serves S1.

- **Roles.** The Pro trains the adapter (the GPU-heavy step). The Studio serves S1, owns the canonical data home,
  runs teacher labelling (CLI calls, light on compute) and S1-d outcome runs, and does promotion. Both can work at
  once on different jobs; one adapter run is never split across two machines.
- **The Pro trains only when Forest starts it by hand** (`s1/train start`, e.g. before bed) and stops when he runs
  `s1/train stop`. No idle detection and no scheduled start on the Pro (Forest 2026-10-21).
- **Data.** Before training, the Pro pulls the SFT files from the Studio; afterwards it pushes the adapter and its
  report back, and the Studio grades and promotes. Copy-back, not a network mount, so training never depends on the
  link staying up; sync runs over Tailscale when away from home.
- **Start:** `s1/train start [--teacher <model>] [--holdout-teacher <model>] [--label-only | --train-only]`. Runs
  outside 01:30–07:00 (today `overnight.py` refuses: "nothing started").
- **Teacher choice:** `--teacher` and `--holdout-teacher` override the inventory resolution for this run only and
  are written into every label row. A holdout-teacher override re-labels the holdout, as in S1-a.
- **Stop and save:** `s1/train stop` stops at once, keeps every finished label and writes a checkpoint before
  exiting. Today the trainer saves every 100 steps, so a stop can lose up to 99.
- **Resume:** the next start, manual or nightly, continues the unfinished run from its checkpoint.
- **`s1/train status`:** machine, phase, step / target, teacher, ETA, last checkpoint.
- **One adapter run at a time:** a lock in the Studio's data home.
- **Per-machine services:** the Studio stops and restarts the S1 scorer around its own training; the Pro has no
  scorer to stop and skips that step.

## Open

- Cursor lane as a provider backend: needs its non-interactive and sandbox flags verified.
