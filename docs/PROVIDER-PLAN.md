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

## Conversation lifetime and timeouts

- `CORTADOS_PROVIDER_IDLE_S` (1800 s): reclaim completed, inactive conversations and their CLI resources.
- `CORTADOS_PROVIDER_TOOL_WAIT_S` (10800 s / 3 h): replies requesting client tools retain their conversation
  for at least this window from reply completion, or the ordinary idle TTL if longer. Abandoned tools still expire.
  One effective tool window, `max(IDLE_S, TOOL_WAIT_S)`, governs bridge waits, reaping and the shim.
  Shim HTTP budget = turn timeout + effective tool window + 120 s delivery slack. CLI MCP timeout is strictly
  longer: shim HTTP budget + 120 s abort budget + 120 s startup/scheduling slack, preventing CLI-synthesized
  tool errors before backend shutdown. Provider timeout settings support legacy `WANGCAI_*` aliases through
  `compat.getenv()`; resolved values are explicitly passed to the standalone shim.
- `CORTADOS_PROVIDER_MAX_SESSIONS` (128): bound held conversations/processes and their replay memory (at most
  1 MiB of replay per session). New held conversations receive 503 at capacity; existing tool waits are not evicted.
- `CORTADOS_PROVIDER_TURN_TIMEOUT_S` (1800 s): bounds one backend model turn, not execution of client tools.
  Streaming turns already send Anthropic `ping` SSE events every 10 s. Non-streaming clients must allow the whole
  backend turn in their own read timeout. Socket reads/writes are bounded at 120 s to release stalled connections.

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
| Phase A | multiple Claude accounts (Option A, lane-per-account): each account is its own scarcity lane/pool with its own reset, sampled from its own `CLAUDE_CONFIG_DIR`; the cost formula routes between them (unused weekly credit attracts work before it resets), and a `reserve` account is spared while a non-reserve sibling has room; the provider runs the chosen account on its login. Config: `tables/accounts.json` (git-ignored; copy `accounts.example.json`). Codex multi-account (per-account `CODEX_HOME`) is Phase B. | two accounts show distinct usage/resets; routing steers to the non-reserve/expiring account; a turn runs on the chosen account |
| Later | CLI runners on other machines (the Air); one model across machines; cloud clients (needs a public endpoint with auth, and a verified `BASE_URL` override in the cloud client); API-key lanes (Phase C) | |

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
- **S1 location.** The endpoint scores through `cortados_router.s1_endpoint.resolve_url()`: `CORTADOS_S1_URL` wins if
  set, else a named device from the endpoints file (`CORTADOS_S1_ENDPOINT=<name>`, or its `default`), else loopback
  `127.0.0.1:8431`. The file is `CORTADOS_S1_ENDPOINTS` or `tables/s1-endpoints.json` (git-ignored; copy
  `tables/s1-endpoints.example.json`), mapping device names to URLs so no address is in code. S1 itself listens on
  `127.0.0.1` unless `CORTADOS_S1_BIND` widens it (e.g. the Studio's tailnet IP); on a host that does not run S1 next
  to it, select the serving device or every conversation routes degraded. A widened bind is guarded: body size and
  backlog are capped, a job whose client already timed out is dropped unscored, and `CORTADOS_S1_TOKEN` (when set)
  requires a bearer token. The tailnet (Tailscale ACLs) is the access boundary; the token is the belt-and-braces.
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
  What the match compares (`Registry.find`: `session.history == turn.history`, the list of `reply_fingerprint`s,
  sha256 of each assistant message's trimmed text + tool_use ids) decides each history rewrite (2026-11-03, tests in
  `tests/test_provider.py`): (a) old tool_result content replaced by a placeholder (Claude Code microcompaction) is
  not compared, so the conversation continues; the CLI session still holds the original. (b) Compaction into one
  summary user message leaves no assistant message, so the request opens a new conversation (not a 409); a compacted
  history that keeps some but not all earlier assistant messages is a 409. (c) A removed or edited assistant message,
  or a different tool_use id, is a 409: never continued, because the CLI session would answer from a history the
  client no longer has. Rebuilding (b)/(c) from the transcript is still open. The live 409 bursts of 2026-11-03
  (12:32-12:42, ~40 across several long `claude -p` sessions; no restart, no sleep, reaper last ran 12:07) are NOT
  explained by (a) or the reaper. Leading hypothesis: a lost/timed-out response retried by the client. The session
  already holds turn N's reply, the retry's history is one reply shorter, and that 409 poisons every later request.
  Now handled: (d) a request whose history is a live session's minus its last reply AND whose new user turn (content
  + tool results, `anthropic_wire.turn_fingerprint`) is the one that reply answered gets the stored last response
  again (same id, content, stop reason, usage), without the backend; logged as "replaying its last response". One
  response per session, at most `REPLAY_MAX_BYTES` (1 MiB; larger replies are not kept and their retry stays a 409).
  A different user turn on that history is still a 409, and so is a retry that also changes the `sub/` pin (the
  "model is fixed per conversation" refusal). Replay needs a conversation identity header (one of
  `CONVERSATION_ID_HEADERS`): a request without one is never replayed and gets the ordinary 409, since identity-less
  sessions all share the empty scope and a stored reply would go to a stranger. Only sessions in the request's
  conversation scope (client + identity) opened on the same wire (`/v1/messages` or `/v1/responses`) are replayed, more than one candidate is a 409, and a replay refreshes the conversation's idle clock so the reaper
  does not close it mid-retry. (e) Every lost-conversation 409 logs one `409 lost conversation: class=<c> ...` line
  (counts and lengths only, no content or fingerprints). From `Registry.find`: `class=<c> candidates=<n>
  common_prefix=<i> request_len=<n> candidate_len=<n>` with `class` one of `retry_of_answered_turn` (one reply behind,
  but not replayable: a different user turn, no stored reply, no identity, another scope or wire, or several
  candidates),
  `client_behind=<k>` (a strict prefix k >= 2 replies short: only the last turn is replayed), `client_ahead`,
  `diverged_at=<i>`, `no_candidate`, or `reaped_recently` (the endpoint closed a conversation with the same first
  reply within the last hour: `Registry.closed`, 128 entries); `class=ambiguous candidates=<n> request_len=<n>` when
  several live sessions hold the exact history. From the turn worker, after waiting for the session lock:
  `class=closed_while_waiting` or `class=moved_on_while_waiting` with `conversation=<id> request_len=<n>`
  (and `candidate_len` for the latter). The next burst's log lines say which case it was.
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
  the result is matched by id, never by order. While the call's turn is live it is answered when the client's next
  request carries the result: `{success: not isError, contentItems: [inputText | inputImage]}`; if that turn ended
  first, the result reaches the model only as text in a follow-up turn (code-mode bullet below); images travel as data URLs, because Codex
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
- Code-mode yield. Codex 0.163.0 runs dynamic tool calls inside a code-mode exec cell, and the cell yields back to
  the model after its exec yield time (~30 s default). The model can then end the turn while a client tool call is
  still unanswered. To stop that, the child starts with
  `-c features.code_mode.default_exec_yield_time_ms=<TURN_TIMEOUT_S in ms>` and `config/read` must show that value.
  Without it, any client tool that takes longer than ~30 s (a test run, a build, a slow human approval) would end the
  turn around its own call. The model can still pass its own `yield_time_ms`, so the bridge also handles turns that
  end early:
  - Each call is recorded with the `turnId` that issued it (`item/tool/call` and `item/completed` both carry
    `turnId`). A turn whose `turn/completed` has arrived is ended for good. A call or `agentMessage` naming a turn
    this session never started is a BridgeError.
  - A call from an ended turn (a cell that outlived its turn) is answered at once with "cortados: turn already
    ended; call refused" (`TURN_ENDED`). It is never forwarded to the client, so a superseded turn cannot make the
    client run mutating work. That turn's agent messages are dropped from client output, and each drop is logged.
  - Calls still outstanding when their turn completes are answered to the waiting cell with `ENDED_AROUND` ("the
    turn ended before this call's result arrived; the result is given to the model in the next turn instead"). The
    client still runs them. Their results reach the model only as text in a follow-up `turn/start` (each result by
    call id, tool and status, cut at `LATE_RESULT_CHARS`), never also to the cell: each result is delivered exactly
    once.
  - Queued events are classified in FIFO order. A sibling `tool_use` or `agentMessage` the turn emitted before its
    completion still belongs to that turn and is carried to the client on the next reply, as `tool_use`. Its result
    joins the late ones in the same follow-up.
  - `MAX_FOLLOWUPS = 3` follow-up turns per user message. Each follow-up is an extra model request on the
    subscription, so a model that keeps ending turns around calls cannot loop. Past the cap the bridge raises a
    BridgeError naming the calls; it never replies with a result missing. A new user message instead of the results
    starts no follow-up: the new message supersedes them.
  - Live check, 2026-11-02, Air, codex-cli 0.163.0, branch `codex-yield` fed9a95: `item/tool/call` params were
    `{arguments, callId, namespace, threadId, tool, turnId}`, and `item/completed` (dynamicToolCall, agentMessage)
    carried `turnId` equal to the `turn/started` id. `config/read` showed `code_mode.default_exec_yield_time_ms =
    1800000`. The result was held for 45 s and nothing arrived during the wait (no yield, no `turn/completed`).
    After delivery the turn ended with exactly "hello-cortados".
- Close: `thread/unsubscribe`, close stdin ("stdin closed" ends the process), kill after 5 s.

**Isolation of the child.** There is no `--ignore-user-config` for `app-server`, and a throwaway `CODEX_HOME` has no
login; refresh tokens are single-use, so a copied or linked `auth.json` could break Forest's own login and is never
done. The backend therefore runs on the real Codex home with everything switched off on the command line:

```
codex app-server -c notify=[] -c web_search="disabled" -c include_environment_context=false -c project_doc_max_bytes=0
  -c model_provider="openai"                 # the built-in provider: a config.toml model_provider cannot redirect the lane
  -c include_permissions_instructions=false  # no "sandbox is read-only" block: it describes the backend, not the
                                             # client's tools, and a model refused a write over it (2026-11-02)
  -c features.code_mode.default_exec_yield_time_ms=<turn budget ms>   # code mode must not yield before the turn budget
                                             # (a top-level `code_mode` table is ignored by 0.163.0: captured live in
                                             # tests/fixtures/codex-config-read-0.163.0.json)
  -c mcp_servers.<name>.enabled=false        # for every [mcp_servers.<name>] table of $CODEX_HOME/config.toml
                                             # (the one read of it; `-c mcp_servers={}` merges and does nothing)
  --disable shell_tool --disable unified_exec --disable unified_exec_tty --disable view_image --disable image_generation
  --disable apps --disable plugins --disable hooks --disable multi_agent --disable sleep_tool --disable skill_search
  --disable tool_suggest --disable goals --disable memories --disable browser_use --disable computer_use
  --disable collaboration_modes --enable code_mode_host   # dynamic tools are served through it; checked in config/read
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
  false, every disabled feature false and `code_mode_host` true in `features`, `features.code_mode.default_exec_yield_time_ms`
  equal to the turn budget when reported (unreported: the command-line check stands, logged once), and `openai_base_url` and
  `model_catalog_json` unset: `ollama launch codex` writes both into config.toml, and Codex then sends a
  ChatGPT-authenticated turn to the local Ollama endpoint (seen on the build Mac on 2026-10-21, when that config
  appeared mid-review: the bridge would have run the "subscription" lane on a local model). `thread/start` is when Codex starts MCP servers and binds the
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
  bridge declares colliding client tools under `client_<name>` (`codex_isolation.CODEX_TOOL_NAMES`, `tool_aliases`) and
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
  removes that. On the build Mac the real home now also carries the `ollama launch codex` settings, so the codex-cli
  lane there is refused (502 naming `openai_base_url`) until a dedicated, logged-in home is given with `--codex-home`;
  that is the intended answer, not a bypass.
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

## Routed-run record (2026-11-01)

A headless `claude -p` subagent's JSON output names only `cortados`, so the routed-subagents mod sends
`x-cortados-run-id: <id>` (`^[A-Za-z0-9_-]{8,64}$`, else 400). Every request of the run that is routed (a new
conversation via S1, a `sub/` pin, an `api/` lane request) is recorded in memory under the id: provider, real model,
reveal label (the label the reveal would show, or the model when the request did not ask for a reveal), and whether
it had tools. Limits: 512 run ids (least recently used dropped first) and 16 entries per run; a restart forgets them.
Once a run is full, a new entry with tools replaces the oldest tool-less entry, if there is one. A tool-less entry
past the cap is dropped. This way title and summary side calls can never push out the builder's entry.
`GET /v1/cortados/runs/<id>` (needs the token) returns `{run_id, model, label, provider, models, builder_models}`.
The primary entry is the first one with tools, else the first entry. `models` lists the distinct models of every
entry, including tool-less side calls. `builder_models` lists only the distinct models of entries that had tools,
the ones that could have written code (empty if none). Reviews should be kept off `builder_models`. The endpoint
answers 404 for an unknown id and 400 for an invalid one. The record is advisory, because the client influences it:
any holder of the shared token can add entries under a run id it chooses. That stays inside the token's existing
trust boundary, since the record only labels runs and steers reviews and never authorizes anything.
`x-cortados-exclude-model` now takes a comma list of up to 8 model ids (empty items are ignored). Each listed model is
excluded under the single-model rules, and a pin on any of them is refused.

## Subscription pins on both backends (2026-11-02)

`sub/<model>` now pins any model this endpoint serves, Claude or Codex: the pinnable set is `routing.pinnable_models`,
the `served_routes` pairs (read from `tables/`, the `openai-codex` rows served as their `codex-cli` clone), one per
model. A Claude pin runs on claude-cli (bridge.py) as before; a Codex pin (e.g. `sub/gpt-6.1-sol`) runs on codex-cli
(codex_bridge.py) through the same `SESSION_FACTORIES` dispatch an S1 route uses. Both go through `choose_pinned`: no
S1, but the binary-on-PATH check, the input-fit gate (codex-cli uses `provider_ceiling_tokens.codex-cli`, as
`core.resolve` does), lane capacity, `x-cortados-exclude-model` and a `routes` row (engine `pin`). Codex has one
login here (the backend's `--codex-home`), so a Codex pin is a single candidate on the `codex` lane; a spent Codex
lane is refused without an `api/` hint (that lane is Anthropic's). A name the tables do not serve is a 400 that lists
the pinnable ids. `GET /v1/models` lists `cortados` ("CortadOS") and every pin as `sub/<id>` with display name
`<readable name> · CortadOS` (`routing.readable_model_name`, derived from the id: `Claude Fable 5.1`, `GPT-6.1 Sol`),
so a client's model picker (the Claude desktop app) can choose the orchestrator. A `--fixed-model` endpoint lists only
`cortados`, since it does not honour pins.

## Local models (pin-only) (2026-11-02)

`sub/<local model or alias>` (e.g. `sub/qwen-27b`) relays a `/v1/messages` turn to an Ollama node declared in the
local-resources config (schema: `docs/LOCAL-RESOURCES.md`, "Local models"). It is a lane kind of the one pin path:
`choose_pinned` hands a local name to `_choose_local` (node answers `/api/tags` and lists the tag, input fits the
configured `context_length`, opener logged as a `routes` row on lane `local-<node>`). The registry holds the
conversation as a `LocalSession` (no backend process: the model binding and reply fingerprints), so `find` and
`_model_change` keep it on its model like any pin (409 on a switch, in either direction); the server relays each turn
statelessly (`local_lane.serve`: bounded connect and read timeouts, SSE passed line by line, `tool_use` untouched).
An unreachable node or unlisted model is a 503 naming the cause; an unknown name is a 400 listing every pinnable id,
local ones included. `GET /v1/models` lists each as `<name> · CortadOS (<node label>)`. S1 never routes to a local
lane: they are absent from `SERVED_PROVIDERS`, and the config's `auto_route` switch is refused unless `false` until
local lanes are evaluated. Ollama has no auth, so it stays on `127.0.0.1` and the node runs the token gate
(`cortados_router/local_gate.py`) on its Tailscale address; the lane sends the gate's token (`token_file_env`) on every
probe and turn, and a gate 401 is a 503 naming it. `/v1/responses` (Codex) does not take local pins.

## Web search lane (2026-11-03)

Claude Code's WebSearch sends a request whose only tool is the Anthropic server tool `web_search_*` (often with a forced
`tool_choice`). No bridged backend can run a server tool, so `cortados_router/provider/search_lane.py` serves it on a
separate one-shot `claude -p` whose only tool is WebSearch (`--tools WebSearch --allowedTools WebSearch`, empty MCP
config, no setting sources, `dontAsk`, empty scratch cwd, ANTHROPIC_* stripped as in the bridge). This is the one
owner-approved exception to the bridge rule that the headless CLI never runs a tool itself. Model: `SEARCH_MODEL`
(the routing table's W-fast Claude route, Haiku 4.5). Login (`search_lane.login`, resolved together with the ledger
lane before launch): the account named by `CORTADOS_PROVIDER_SEARCH_ACCOUNT` (resolved through tables/accounts.json;
unresolvable fails, never falls back); else, with accounts configured, the first account (the bare `claude` lane)
passed as an explicit `CLAUDE_CONFIG_DIR`, overriding any the provider inherited; else the provider's own login. An
inherited `CLAUDE_CONFIG_DIR` that matches no configured account (or a named account with no lane) fails with
`no_claude_lane` before the CLI is launched, so no search spends quota the ledger cannot record. A completed search's
ledger row is written after the fact like every other lane's (the ledger is accounting, not the quota gate); a failed
write is logged at error level with the lane and run id. The request thread only puts the row on a bounded in-process
queue (`usage_ledger.QUEUE_MAX`); one writer thread, started with the endpoint and restarted by a supervisor, commits it in batches and retries while usage.db is locked or
migrating, so the request path never touches SQLite or a file lock. A row is lost only on a full queue, a batch that
keeps failing with a non-busy error, or exit with rows still queued; each is counted and shown as `ledger.lost`
(with `ledger.queued`) in /v1/cortados/usage, and logged "LOST". The request's policy headers bind the lane before launch, as on routed calls: `x-cortados-exclude-model`
naming the search model is a 400, and `x-cortados-min-left-pct` is checked and reserved on the search account through
`floor.reserve` (429 on refusal; a fixed-model endpoint or a failing check fails closed).
Hooks are off (`--settings '{"disableAllHooks": true}'`) and every `ANTHROPIC_*` variable is stripped by prefix
(`bridge.claude_env`), so e.g. `ANTHROPIC_CUSTOM_HEADERS` never reaches api.anthropic.com. The query is capped at 2000
characters (`query_too_long`) and rides in argv, like the bridge's system prompt. The web_search tool's
`allowed_domains` / `blocked_domains` are carried into the prompt ("only use results from ...; never use results
from ...") as an instruction to the search model, not an enforced filter.
Timeout 120 s, at most 4 concurrent searches. The CLI runs with `--output-format stream-json --verbose`; a reply needs
evidence that a search ran: a WebSearch `tool_use` answered by a non-error, nonempty `tool_result`. Without it the
reply is "unavailable" with reason "no search was performed" and no ledger row (the result's
`usage.server_tool_use.web_search_requests` is not used: the live CLI reports 0 there after a real search). The last
`result` line's text is the reply (JSON or SSE, end_turn, with usage);
any failure (timeout, nonzero exit, bad output, busy, no login, an unexpected error: `internal`) answers with the
explicit "unavailable" text naming the failure class. Searches are not routed by S1. A successful search is recorded in
the usage ledger against the Claude lane it ran on (`claude`, or the named account's lane such as `claude#b`).

## Open

- Cursor lane as a provider backend: needs its non-interactive and sandbox flags verified.

## Usage endpoint (GET /v1/cortados/usage)

`GET /v1/cortados/usage` (token required, 401 otherwise): one cheap JSON view for a usage dashboard, built by `cortados_router/provider/usage.py` — `lanes` (cached scarcity.json only: % left, windows/resets, sample age, `stale` by the routing freshness rule), `routed` (router.db counts per lane/model today and 7 d, top S1 bands; plus `provider_since_start` from the in-memory run records), `refusals` (24 h by reason, in-memory), `child_tasks` (7 d by last event from child-tasks.jsonl), `s1` (1.5 s `/health` probe), `hardware` (`$CORTADOS_HOME/hardware/*.json` passed through with age), `notes`, `errors` (every failed or absent source), `generated_at`. Opens no vendor CLI.

## Live view (GET /v1/cortados/live) (2026-11-11, #73)

`cortados_router/provider/live.py` composes one metadata-only snapshot for the dashboard's Live tab and the
cortados-pulse pane. Owner token or a `live:read` client token (provider/clients.py); `Cache-Control: no-store`; a view
that fails answers 500 with the exception type in the log. Inputs, each under its own guard: the child-task log (read
once, 24 h, bounded tail; the Office board gets its 4 h slice of the same read), the Office board, the Tasks view, the
usage view's lanes/s1/hardware slice (`usage.collect_live`: same builders and publish pass, no router.db/usage.db scans),
the heartbeat board, the exec-hosts file (for host aliases only; never ssh) and `child_tasks.peek_local` (read-only:
queued marker, workspace record; never `_state()`, which reaps and recovers).

Schema (version 1). Every section has `as_of` (epoch s), `status` and `errors` [{source, error}] (error = a fixed word
or an exception type, never its message):

    {"version": 1, "generated_at": <s>,
     "overview": {"running_tasks", "queued_tasks", "active_conversations", "working_conversations", "s1",
                  "s1_latency_ms", "lowest_subscription": {"lane", "label", "left_pct", "window"} | null,
                  "hosts_with_heartbeat", "errors"},                    # counts are null when their section failed
     "tasks": {..., "window_s": 86400, "counts": {"running", "queued", "succeeded", "failed", "cancelled", "unknown"},
               "running": [task], "queued": [task], "recent": [task], "truncated"},
         task = {"task_id", "title", "state", "phase", "host", "client", "provider", "lane", "model", "tool",
                 "started_at", "ended_at", "elapsed_s", "workspace", "browser", "session", "project", "device"}
         (state unknown: a start with no end event after 10 h, or a result on its host with no end event)
     "conversations": {..., "backend_sessions", "active", "working", "floors", "items": [conv], "hidden"},
         conv = {"kind": orchestrator|subagent, "run_id", "session", "project", "device", "provider", "lane", "model",
                 "status": working|idle, "tool", "source", "title", "started_at", "last_activity", "elapsed_s"}
     "routed": {..., "calls": [{"ts", "task_id", "session_id", "routing_kind", "band", "lane", "model",
                "actual_model", "elapsed_ms", "degraded", "s1_reason", "request_chars", "response_chars"}] (25),
                "totals", "row_limit", "more"},
     "subscriptions": {..., "lanes": [{"lane", "label", "account", "left_pct", "stale", "error", "sample_age_s",
                       "shared_with", "pools", "windows": [{"window", "pool", "left_pct", "resets_in_h", "stale"}]}],
                       "lowest"},
     "s1": {..., "status": up|down|unknown, "available", "http_status", "latency_ms", "reason"},
     "hosts": {..., "provider_host", "heartbeat_path", "items": [{"name", "roles", "heartbeat_state": fresh|stale|none,
               "heartbeat": {"received_at", "age_s", "ttl_s", "stale", "jobs": [job]} | null,
               "hardware": {"cpu_busy_pct", "gpu_busy_pct", "mem_available_bytes", "mem_total_bytes", "chip",
                            "load_1m", "age_s"} | null}]}}

Section status: `ok`; `partial` (data with some errors); `error` (no data: the source failed or is absent). Host names
are exec-host slugs; a hardware sample's hostname is matched by an exec host's `hostnames` alias, else by a name equal to
the hostname or one of its words (`forests-mac-studio` -> studio), never by a guess between two.

**Heartbeat.** `POST /v1/cortados/live/heartbeat` (owner token or a write-only `live:heartbeat` client token):

    {"host": "studio",                 # [a-z][a-z0-9-]{0,31}, the exec-host slug
     "ttl_s": 900,                     # optional, 60..86400: shown fresh this long, then stale; forgotten after 24 h
     "jobs": [                         # optional, at most 16, unique names
       {"name": "s1-continuous",       # [a-z0-9][a-z0-9._-]{0,47}
        "state": "running",            # running | idle | ok | scheduled | failed | stopped
        "summary": "sweep 3/8",        # optional, printable ASCII <= 80, no address
        "progress_pct": 37.5,          # optional, 0..100
        "started_at": 1794400000, "finished_at": null, "next_at": null}]}   # optional epoch s, within 30 d of now

A post replaces the host's previous one. Refusals: 401 without a token, 403 for a token without the scope, 413 over 8 KiB
(checked from content-length before reading), 400 naming the bad field (`jobs[0].state`), never echoing its value. The
board is in memory (16 hosts, LRU): after a provider restart each host shows "no heartbeat yet" until its next post.

How a Studio job would post (documented only: nothing on the Studio is changed by #73). A LaunchAgent running every
5 minutes, or a line at the end of each s1-continuous cycle, with a `live:heartbeat` token minted on the provider host
(`python3 -B -m cortados_router.provider.clients add studio --scope live:heartbeat --token-file <path>`) and copied to
the Studio as `~/.config/cortados/live-heartbeat-token` (0600):

    python3 - <<'PY'
    import json, os, subprocess, urllib.request
    def state(label):   # a LaunchAgent's state from launchctl: running when it has a pid
        out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True)
        return "stopped" if out.returncode else ("running" if '"PID"' in out.stdout else "idle")
    body = {"host": "studio", "ttl_s": 900, "jobs": [
        {"name": "s1-scorer", "state": state("com.cortados.s1-scorer")},
        {"name": "s1-continuous", "state": state("com.cortados.s1-continuous")}]}
    tok = open(os.path.expanduser("~/.config/cortados/live-heartbeat-token")).read().strip()
    url = open(os.path.expanduser("~/.config/cortados/provider-url")).read().strip() + "/v1/cortados/live/heartbeat"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": "Bearer " + tok})
    urllib.request.urlopen(req, timeout=10).read()
    PY

## Compaction content store (phase 1, shadow only)

The provider can keep conversation CONTENT on its host (the Air) so compaction labels can be harvested; the owner
approved this with a retention limit. Nothing it does changes what any backend receives (pinned by
`tests/test_compaction_store.py`, which compares the bytes delivered to the fake backend with the store on and off).

- Switch: `CORTADOS_COMPACTION_STORE=1` (off by default: no files are created).
- Where: `$CORTADOS_HOME/compaction/sessions/<safe-id>-<sha12>.jsonl` (dirs 0700, files 0600). Every file operation
  is anchored to directory fds opened one component at a time from `$CORTADOS_HOME` down with O_NOFOLLOW, and files
  are opened relative to them with O_NOFOLLOW | O_NONBLOCK and an fstat check (regular file with exactly one hard
  link): a symlinked home, `compaction/` or `sessions/`, a symlinked session file, a FIFO or a session name
  hard-linked to a file outside the store is refused, for writes, reads and retention alike. Code: `cortados_router/compaction/store.py`.
- What: per provider turn, a `turn` header (source `claude-code` / `codex`, `x-cortados-run-id`, `x-cortados-session`,
  client), the tool results answering earlier calls, the new user text, the assistant text and the tool calls it
  issued. Each result text and each tool input is capped at `provider_tool_result_cap_tokens` x `chars_per_token` for
  the conversation's source, both from `tables/compaction.json` (8000 x 2.5 = 20000 chars for Claude Code, 8000 x 4 =
  32000 for Codex); the original length and a truncated flag are kept (labels need the content, one huge output or a
  giant Write content must not fill the disk). The input cap holds on the final serialized JSON: an input that
  cannot fit even with its values shortened is stored as `{"_truncated_json": <head>}`. Read back with `store.read_session(path) -> events.Session`.
- Retention: `CORTADOS_COMPACTION_RETENTION_DAYS` (default 14: nightly harvest plus a week of review; tools can print
  secrets, so keep it short), then `CORTADOS_COMPACTION_MAX_MB` (default 2048) deletes the oldest files first. Both
  cover `sessions/` AND the decisions shards under `decisions/` (one budget for the whole store). Pruned at provider
  start and at most hourly after. Each candidate is re-stat'ed just before deletion. `read_session` accepts only a
  path that names a file directly under `sessions/` and reads it through the anchored fds; an alias such as
  `sessions/../sessions/x.jsonl` through a symlinked parent, or a path elsewhere, is refused (no direct `open` fallback).
- One writer: all store I/O (turn appends, shadow decisions, retention) runs on a single background writer thread
  fed by a queue bounded twice: `QUEUE_MAX` = 1024 jobs and `QUEUE_MAX_BYTES` = 64 MiB of retained content. A turn
  is serialized (and capped) on the request path so its size is known before admission and the queued closure holds
  only the capped text, never the request's own content; a job over either bound is dropped and counted (`dropped`),
  never blocking a request. Retention never races a write.
- Shutdown: `STORE.shutdown()` (provider exit and `atexit`) refuses new jobs, drains what was accepted for
  `SHUTDOWN_DRAIN_S` = 5 s, stops the writer and counts what did not run in `lost` (the writer is a daemon thread,
  which the interpreter would otherwise freeze with accepted turns and decisions still queued).
- Failures: I/O failures (`write_failures`), drops (`dropped`), jobs lost at shutdown (`lost`) and jobs that could
  not be scheduled, such as a writer thread that will not start or a turn that could not be built
  (`schedule_failures`), are each logged with the reason and counted in `store.STORE.stats()`. None of them is
  raised into the request, turn finalization or Registry cleanup (the reply is already in hand).
- Shadow pruning: when a conversation closes (drop, failure, idle reap) `compaction/prune.py` decides keep/drop for
  every past tool call on the store's writer thread and appends the decisions, with the chars/tokens they would have saved,
  to `$CORTADOS_HOME/compaction/decisions/decisions-<UTC day>.<n>.jsonl` (0600; a new shard per day and whenever the
  current one reaches `DECISIONS_SHARD_MAX_BYTES` = 16 MiB, so retention deletes decisions in bounded pieces;
  `prune.decision_files()` lists them in order). Each row carries a `gate`: the session's estimated tokens
  against the pruning start min(`start_cap_tokens`, `start_pct` x W) (`would_start`) and whether the drops free at
  least `min_freed_tokens` (`would_prune`), so labels can be joined later. W is `assumed_window_tokens` (200k) because
  the provider does not learn the backend window. Shared thresholds (keep the last 4 results, start, min freed, result
  cap, chars per token) are read from `tables/compaction.json` through `compaction/policy.py`, the one source shared
  with phase 0; `tables/compaction-prune.json` holds only this phase's rule data (tool lists, read-only Bash command shapes,
  MCP side-effect words, small-result N, dropped-result head H, assumed window), provisional.
- The Office view (`office.py`) stays metadata-only; it never reads this store.
