# CortadOS Router

A model router. For every LLM call it decides **which model, on which subscription** should run it — Claude Code, Codex, Cursor, a local model — and sends the call there. The goal is one place that spends the cheapest capable resource for each request across a fleet of machines and subscriptions, instead of every tool reaching for the same frontier model.

It works in three steps:

1. **Score the request.** A small local model, **CortadOS Router S1**, answers 12 typed questions about the request. It never answers the request itself.
2. **Turn the answers into a requirement.** Hard rules and a band table turn the answers into a capability requirement: a required benchmark index for that kind of work, a band (R0–R4), and a sub-agent count.
3. **Pick the cheapest capable route.** Every model reachable through a subscription qualifies if its use-case index clears the requirement. The winner is the cheapest, where cost comes from the model's burn on its quota pool and that pool's live scarcity.

## Two ways to use it

**As a model provider (the main path).** The router exposes a single model named `cortados` over an HTTP endpoint. Point any coding tool at it; each new conversation is scored once and routed to a model, which it keeps for its follow-up turns, while a standalone tool-less request is routed on its own. The chosen model's text comes back, and its tool calls run in the client (a tool bridge). Claude Code talks to it on `/v1/messages`, Codex on `/v1/responses`. A client can instead pin a model (`sub/<model>`, `api/<model>`; see the table below) and skip S1 entirely. See `docs/PROVIDER-PLAN.md`.

Two more pieces build on the provider, both delegating work rather than routing a whole session:

- **The `cortados-routed-subagents` Claude Code mod** (`integrations/claude-mod/cortados-routed-subagents/`) intercepts every Agent-tool subagent the session spawns and runs it as a headless `claude` against the provider instead: S1 scores and routes the subagent's model, while its tools still execute on your machine. Built-in tools only (no MCP, no browser); worktree-isolated, remote, forked and named agents pass through unrouted; a routed subagent runs under a 10-minute cap. An optional **orchestrator mode** directs the main session to plan and delegate rather than do substantive work itself (guidance by default; `orchestratorEnforce` makes it a hard allowlist). Each run's real backend model and the lane's remaining window are shown via the run record (below), not just `cortados`.
- **The `cortados-route` MCP tool**, `route_task(task, context?, max_tokens?, model?)` (`cortados_router/subagent_mcp.py`), is text-only delegation: the main session keeps its own model and tools, and hands a self-contained subtask to the provider for S1 to score and route; the answer comes back as text, with no tool access of its own. `model` (optional) pins a model instead of S1: any name `start_task`'s `orchestrator` accepts (`fable`, `opus`, `claude-*`, `gpt-*`; one validation, `child_tasks.pin_model`), sent as `sub/<id>`; the result's `_meta` carries the requested and the routed model. Token from `CORTADOS_PROVIDER_TOKEN`/`_FILE` or `~/.config/cortados/provider-token` by default. Setup below (Delegating sub-agent work).

**Recommended setup:** pin the main session to a strong model with `claude --model sub/opus` against the provider, and let the routed-subagents mod send its workers through S1 (`model: cortados` in the mod config, the default). S1 only scores a conversation's first message and the route is then fixed for that conversation — good for a worker whose task is stated up front, not for an orchestrating session whose first message ("let's get started") is too thin to score well. Pinning the planner sidesteps that; routing the workers still gets each one the cheapest capable model.

**As a decision library/CLI.** Call `core.decide` to get a routing decision for a request without running anything, for shadow logging or for a host that dispatches calls itself.

A frozen Hermes plugin lives in `integrations/hermes/`; it is kept for reference and is not the active direction.

## How the pieces run (a three-machine example)

The router is built to span machines on a private network (here, Tailscale). A working deployment separates three roles; all three can also run on one machine.

- **The scorer** serves S1. `python3 s1/serve.py` loads the promoted adapter and answers `POST /score`. It binds loopback by default; set `CORTADOS_S1_BIND` to a private-network address (and `CORTADOS_S1_TOKEN`) to let other machines reach it.
- **The provider** hosts the subscription CLIs it drives and exposes the `cortados` endpoint. It scores each conversation by calling the scorer, picks a lane, and drives a headless `claude` or `codex` as the backend. It resolves the scorer's address through `cortados_router.s1_endpoint` (an explicit URL, a named device in an endpoints file, or loopback).
- **The client** is the coding tool. It points `ANTHROPIC_BASE_URL` at the provider and runs `--model cortados`.

No model address is hardcoded. Devices live in `tables/s1-endpoints.json` (git-ignored; copy `tables/s1-endpoints.example.json`), so moving S1 to another machine is a one-line config change.

## The 12 questions

Six 0–3 rubric scores (anchors in `cortados_router/core.py → ANCHORS`):

| question | 0 | 3 |
|---|---|---|
| `steps` | one action or lookup | a long dependent chain |
| `ambiguity` | the directive fully determines the work | intent must be substantially interpreted |
| `blast_radius` | words only, nothing changes | law, money, production or irreversible effects nearby |
| `context_size` | fits well inside a local window | exceeds a single frontier context |
| `novelty` | matches known patterns | nothing like it recorded |
| `parallelizability` | strictly serial | mostly independent subtasks |

Six more:

| question | type | values |
|---|---|---|
| `outward` | yes/no | does carrying this out send, spend, book, publish, delete or change something outside the machine? |
| `needs_clarification` | yes/no | can it be done without a clarifying answer? |
| `diversity` | yes/no | would two independent attempts by different models materially help? |
| `work_kind` | choice | conversation, capture/log, code, research, writing, planning, ops |
| `urgency` | choice | now, today, overnight |
| `parts` | level 0–4 | independent parts: 1, 2, 3, 4–5, 6+ |

Difficulty is `D = steps + ambiguity + blast_radius + novelty` (0–12).

## How a route is chosen

The hard rules come first and cannot be out-voted (`core.resolve`):

- An outward effect floors `blast_radius` at 3.
- A task can be escalated mid-session, by the agent or by a long trajectory; it then keeps a difficulty floor.
- If S1 is down, the decision is marked degraded. The router then routes by scarcity alone in a fixed fallback band and **never defaults to Claude**.

**Bands** (`tables/routing-table.json`, version 17; the first matching condition wins):

| band | condition | sub-agents |
|---|---|---|
| R0 reflex | `D ≤ 2`, `blast_radius = 0`, `context_size ≤ 1` | 0 |
| R1 light | `D ≤ 5`, `blast_radius ≤ 1`, `context_size ≤ 1`, `parallelizability ≤ 1` | 0 |
| R3 hard | `D ≥ 9` or `blast_radius = 3` | 3–5 + verifier |
| R2 standard | otherwise (`D ≤ 8`, or parallel/large) | 0–3, only when the work decomposes |
| R4 ceiling | never by score | escalation only |

**Required index.** The router interpolates the required index over `D` through knots pinned to reference models (D = 3 / 6 / 9 / 11 / 12), and never below the reference of the band the hard rules chose. Each model has a per-use-case index in `tables/model-scores.generated.json`, built nightly by `tables/benchmarks/build_tiers.py` from public benchmarks (Epoch AI, LiveBench, Aider, SWE-bench, LMArena, Artificial Analysis) weighted by `tables/use-cases.json`, plus a bounded community-sentiment nudge.

**Cost.**

```
cost = burn weight + 30 × scarcity − 80 × expiring credit − capacity credit
```

- **Burn weight** is the model's price divided by the learned size of its quota pool (`unitcost.py`).
- **Scarcity** compares the share of a plan window already used against the share of its time elapsed, read from each vendor CLI's own `/usage` or `/status` screen through a PTY (`scarcity.py`), so no tokens are spent. A pool on pace to overshoot gets more expensive; one about to expire unused gets cheaper; a spent pool is skipped.

**Final gates:** context fit is hard; a vendor's models come from that vendor's own subscription first; sessions stay on their model while it stays near the cheapest choice; agent-facing turns never fall below a minimum general index. `docs/ROUTER-DESIGN.md` is the full design log.

Local hardware can be metered as a resource pool: copy `tables/resources.example.json` to `tables/resources.json` (git-ignored; the Studio, the RTX 3060 PC, the Air and the Pro) and point `CORTADOS_LOCAL_RESOURCES_CONFIG` at it. See `docs/LOCAL-RESOURCES.md`.

## CortadOS Router S1

S1 is a LoRA adapter on `Qwen3-4B-Instruct-2507` (MLX, 4-bit). Its completion is twelve `question=digit` lines, one single-digit token each, so the router reads a probability per allowed answer and type errors are impossible by construction. When a difficulty feature's top probability is below 0.75 the router takes the higher of its two likeliest levels; the `outward` question is called at a stricter 0.25.

The pipeline is in `s1/`, run nightly by `s1/nightly.sh`:

| step | file | what it does |
|---|---|---|
| collect | `dataset.py` | gathers routed requests from the local `router.db` and any in `CORTADOS_ROUTER_DB_EXTRA` (e.g. rsynced from the provider host); a request's train/holdout split is fixed by its id |
| label | `label.py` | teacher labels via a stateless CLI, k = 3 soft samples; train split by Sonnet, holdout by a stronger Opus |
| build | `build_sft.py` | builds the SFT set, oversampling rare bands |
| train | `mlx_lm lora` | warm-starts from the promoted adapter |
| grade | `evaluate.py` | grades on the held-out split: band accuracy, under/over-routing, outward recall, ECE, Brier |
| promote | `nightly.sh` | promotes only with no regression where mistakes are expensive — under-routing and outward recall never regress, band accuracy holds within noise — measured against the incumbent re-graded on the same rows |

An **outcome loop** (`s1/outcome/`) adds a second signal beyond teacher agreement: real routed tasks are run and judged by a frontier model (Fable 5.1), feeding the training set so S1 is graded on outcomes, not only on agreeing with teachers.

`s1/serve.py` serves the promoted adapter at `POST <bind>:8431/score`. The ground truth is always an external frontier teacher and real outcomes, never S1's own output, so the loop does not self-reinforce.

**Not published:** model weights, adapters, training data and teacher labels — built from private conversations. `.gitignore` keeps `s1/data`, `s1/runs`, `s1/current.json` and `tables/s1-endpoints.json` out of the tree.

## Layout

```
cortados_router/           the router: core (scoring client, rules, resolve, logging), scarcity, unitcost,
                           transport, images, reviews, resources (the pool snapshot), execution_routing / cron_routing, task_context,
                           s1_endpoint (where the scorer lives, config-driven), compat, paths
cortados_router/provider/  the `cortados` endpoint: server (HTTP + conversation registry + wire seam),
                           anthropic_wire (/v1/messages) and responses_wire (/v1/responses), routing (lane choice),
                           bridge (headless Claude) and codex_bridge (codex app-server) with codex_isolation,
                           mcp_shim (serves the client's tools to the backend)
s1/                        CortadOS Router S1: dataset, label, build_sft, evaluate, serve, student, outcome/, nightly
tables/                    routing table, budgets, use cases, generated scores, benchmarks/, community/, inventory/,
                           s1-endpoints.example.json, resources.example.json, local-models.json
integrations/hermes/       the frozen Hermes plugin (reference only)
integrations/claude-mod/   cortados-routed-subagents: routes Agent-tool subagents through the provider; cortados-pulse:
                           a Claude Code pane that shows the resource pool beside the chat
tests/                     stdlib unittest suite; tests/integration needs a Hermes host and skips without it
```

## Quickstart

```bash
# tests and table (stdlib only, no network, no CLIs opened)
python3 -B -m unittest discover -s tests -t .
python3 -B cortados_router/core.py table                 # -> table v 17 ok

# a routing decision, changing nothing (needs S1 reachable; the switches protect live state + subscriptions)
CORTADOS_HOME=$(mktemp -d) CORTADOS_SCARCITY_REFRESH=off python3 -B cortados_router/core.py decide "rename the variable in utils.py"
```

**Serve S1** (on the scorer machine):

```bash
# loopback only
python3 -B s1/serve.py
# reachable by other machines on a private network, with a token
CORTADOS_S1_BIND=<private-ip> CORTADOS_S1_TOKEN=<token> python3 -B s1/serve.py
```

**Run the provider** (on the machine hosting the CLIs):

```bash
CORTADOS_PROVIDER_TOKEN=<token> \
CORTADOS_S1_URL=http://<scorer-ip>:8431 CORTADOS_S1_TOKEN=<s1-token> \
  python3 -B -m cortados_router.provider.server \
    --host <private-ip> --port 8432 \
    --claude-bin /path/to/claude --codex-bin /path/to/codex
```

`CORTADOS_S1_URL` names the scorer; without it (and without a configured endpoints file that selects a remote device) the provider looks for S1 on loopback and routes degraded. On one machine, drop both `CORTADOS_S1_*` and S1 is found on loopback.

**Point a client at it:**

```bash
ANTHROPIC_BASE_URL=http://<provider-ip>:8432 ANTHROPIC_AUTH_TOKEN=<token> claude --model cortados
```

Codex as a client uses `/v1/responses`; its provider config is in `docs/PROVIDER-PLAN.md`.

**Pin a model instead of routing.** The model name picks the path:

| `--model` | runs on | S1 |
|---|---|---|
| `cortados` | the cheapest capable route S1 and the tables pick, fixed for the conversation | scores the first user message |
| `sub/<claude-model>` (or `sub/opus`, `sub/sonnet`, `sub/haiku`, `sub/fable`; bare `sub` = Opus) | that model on a Claude **subscription** account: the least-scarce configured account whose quota is not spent | skipped |
| `sub/<codex-model>` (e.g. `sub/gpt-6.1-sol`; any model the tables serve on codex-cli) | that model on the Codex backend (`codex app-server`, the provider's one Codex login), if its lane has capacity | skipped |
| `api/<claude-model>` (bare `api` = Opus) | that model on the dollar-metered Anthropic **API credit** lane | skipped |

A `sub/` pin still writes a `routes` row (engine `pin`), so a pinned planner's usage is on record next to the routed workers'. Use it for an orchestrating main session whose first message is too thin to score well, with the routed-subagents mod sending the workers through S1. `GET /v1/models` lists `cortados` and every pinnable model as `sub/<id>` (display name e.g. `GPT-6.1 Sol · CortadOS`), derived from the tables, so a model picker can choose the orchestrator. When every Claude subscription account is spent the pin is refused with a pointer to the `api/` pin; it never falls over to API credit on its own.

The `sub/` and `api/` pins are for Claude Code clients (`/v1/messages`); Codex clients (`/v1/responses`) use `cortados` (a pin on `/v1/responses` is refused with a 400). The model is fixed per conversation: a continuation that names a different `sub/` model, or pins an S1-routed conversation, is refused with a 409; start a new conversation to switch.

| setting | default | purpose |
|---|---|---|
| `CORTADOS_HOME` | `~/.cortados` | state: `router.db`, `scarcity.json`, `unit-cost.json`, `reviews.db` |
| `CORTADOS_ROUTER_TABLES` | `tables/` | tables directory |
| `CORTADOS_S1_URL` | resolved | the scorer URL; wins over the endpoints file |
| `CORTADOS_S1_ENDPOINT` | — | a device name in `tables/s1-endpoints.json` (`CORTADOS_S1_ENDPOINTS` relocates that file); else its `default`, else loopback `127.0.0.1:8431` |
| `CORTADOS_S1_TOKEN` | unset | bearer sent by clients and required by a token-guarded scorer |
| `CORTADOS_S1_BIND` | `127.0.0.1` | interface `s1/serve.py` listens on |
| `tables/accounts.json` | absent | multiple accounts per vendor (git-ignored; copy `accounts.example.json`). Each Claude account is its own lane/pool metered from its own `CLAUDE_CONFIG_DIR`; routing steers to the account with unused expiring credit, and a `reserve` account is spared while another has room |
| `CORTADOS_ROUTER_DB_EXTRA` | unset | extra `router.db` files to feed S1 training, `os.pathsep`-separated — e.g. one rsynced from the provider host, so the whole fleet's daily usage is labelled, not just this machine's |
| `CORTADOS_PROVIDER_TOKEN` | generated | the `cortados` endpoint's bearer token |
| `CORTADOS_PROVIDER_CLAUDE_BIN` / `CORTADOS_PROVIDER_CODEX_BIN` | `claude` / `codex` | backend CLIs the provider drives |
| `CORTADOS_PROVIDER_CODEX_HOME` | unset | a dedicated logged-in `CODEX_HOME` for the Codex backend |
| `CORTADOS_PROVIDER_FIXED_MODEL` | unset | pin one Claude model, no routing |

Legacy `WANGCAI_*` names still work and log a deprecation warning.

**Independent reviews.** A review should not run on the model that built the work it is reviewing. The routed-subagents mod detects a review by its description/subagent-type (`/review/i`) and sends `x-cortados-exclude-model` naming the main session's model plus the recent routed workers' backend models (up to 3, from the run record below); the provider refuses to route, or pin, that review onto any of them. Limits: it's a keyword heuristic, not a semantic check; excluding a model can route the review to a weaker remaining one if the excluded model was the only one meeting the band's required strength; and `route_task` never sends the header. Full detail in the mod's README (**Independent reviews**).

**Provider headers**, sent by clients that want more than the routed text back:

| header | direction | purpose |
|---|---|---|
| `x-cortados-reveal` | request | ask for the real routed model + the lane's % remaining in the response's `model` field, instead of `cortados` |
| `x-cortados-exclude-model` | request | comma list (≤8) of model ids this conversation must not route or pin to (independent reviews) |
| `x-cortados-run-id` | request | tag this request as part of a client-tracked run (`[A-Za-z0-9_-]{8,64}`), so later requests and `GET /v1/cortados/runs/<id>` can learn what it actually ran on |
| `GET /v1/cortados/runs/<id>` | — | returns `{run_id, model, label, provider, models, builder_models}` for a tagged run (404 unknown id, 400 invalid); advisory, since any token holder can write to a run id |

## Whole session on the router (recommended)

```sh
export CORTADOS_PROVIDER_URL=http://<provider-host>:<port>   # token in ~/.config/cortados/provider-token
python3 -m cortados_router.session                           # --orchestrator fable|opus|sonnet|haiku|<model id>|cortados
```

What runs where:

- **Main loop (orchestrator):** pinned with `--model sub/claude-fable-5-1` (default). The short names come from the
  provider's own `sub/` alias table (`routing.SUB_ALIASES`): `opus` = `claude-opus-5-5`, `sonnet` = `claude-sonnet-5`,
  `haiku` = `claude-haiku-4-5-20251001`; any other Claude id gets `sub/` prepended. No S1. `--orchestrator cortados` lets S1 route the main loop too.
- **Native subagents:** `CLAUDE_CODE_SUBAGENT_MODEL=cortados` (+ `_FORCE`, and the workflow variant), so S1 routes
  each one. They are Claude Code's own subagents, so they keep every tool, MCP included; the provider bridges the tool
  calls back to this Claude Code, which runs them locally. Caveat: what `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` does is
  inferred from the Claude Code binary (it is listed beside `CLAUDE_CODE_SUBAGENT_MODEL`), namely that it overrides a
  model named by an Agent call or agent definition. That is not yet verified; a live end-to-end check will confirm it.
  The provider tests submit `model=cortados` directly, so they cover routing once that model arrives, not that Claude
  Code sends it for every subagent. Until the live check passes, treat "every subagent is routed" as expected, not
  guaranteed.
- **Side calls** (titles, summaries): `ANTHROPIC_SMALL_FAST_MODEL=sub/claude-haiku-4-5-20251001`, so they stay cheap and skip
  S1 latency.
- The provider token is set as both `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY`; Bedrock/Vertex/Foundry selectors
  are blanked; `x-cortados-reveal: 1` is sent; `CORTADOS_WHOLE_SESSION=1` is set.
- Claude Code applies a settings file's `env` over the process environment, so a saved `ANTHROPIC_API_KEY` or
  `ANTHROPIC_BASE_URL` in `~/.claude/settings.json` (or a project's) would override the launcher. So every launcher
  variable, token included, is written to a private per-session settings file,
  `~/.config/cortados/sessions/session-<random>.json` (a new 0600 file per launch, so concurrent launches never share
  one; the directory must be 0700 and it and its ancestors must not be symlinks or owned by another user; files
  older than 7 days are pruned at launch), and passed with `--settings <path>`. Args after `--` may not include
  `--settings`, `--setting-sources` or `--model`. That is flag precedence, which beats user, project and local settings; the token is never
  in argv. The same variables stay in the process env. Only managed policy settings outrank flag settings, so if
  `/Library/Application Support/ClaudeCode/managed-settings.json` (or `/etc/claude-code/…`, or a
  `managed-settings.d/*.json` drop-in) sets any of these variables in `env`, the launcher refuses to start.
- The orchestrator directive (`integrations/orchestrator-directive.md`) is appended with `--append-system-prompt`
  (`--no-delegate-directive` skips it). The same settings file turns the routed-subagents plugin off for this session
  (`"enabledPlugins": {"cortados-routed-subagents@cortados-router": false}`) so subagents are not routed twice and the
  directive is not added twice.
- **Delegation gate:** a standalone stdlib PreToolUse command in that settings file allows the main loop
  `--delegate-budget N` direct work calls per user prompt (default **3**; **0** is strict), then denies further
  work and asks it to delegate with Agent. Subagents (hook input contains `agent_id`) keep every tool without a
  budget. Agent, Task, TodoWrite, Read, Grep, Glob, ToolSearch, Skill, AskUserQuestion, ExitPlanMode and EnterPlanMode
  are unlimited; every other tool, including unknown future tools, Bash, edits, web and MCP, spends the budget.
  Set `CORTADOS_DELEGATE_ALLOW_MCP` to a comma-separated list of **exact** `mcp__...` tool names to exempt owner-approved
  MCP tools (default none; no wildcards). The launcher writes `CORTADOS_DELEGATE_BUDGET` into settings env.
  `--no-gate` disables enforcement independently of `--no-delegate-directive`.
  Fork subagents (`subagent_type: "fork"`) are always denied in the main loop, budget or not: a fork inherits the
  pinned orchestrator model and its history, so it skips S1 and collides with the parent's backend conversation (409).
  Counters live in `~/.config/cortados/gate/` (0700 directory, 0600 files), keyed by a hashed session ID and then
  prompt ID (missing prompt ID uses `none`). Atomic replacements and a shared file lock protect parallel calls;
  counter files older than seven days are pruned. Any internal error **fails open** with one stderr diagnostic:
  a broken gate must never brick a session. This is an orchestration guardrail, not a security sandbox.
- `--print-env` shows the env (token masked) and the `claude` argv without launching. Args after `--` go to `claude`.

Known limits: terminal Claude Code only. Desktop-app-only MCP tools (for example the in-app browser) are not
available. Whether claude.ai connectors load when the session uses a provider token is untested.

## Delegating sub-agent work through the router

Point a whole tool at `cortados` to route everything, or keep your main session on its own model and route only the
sub-tasks it chooses to hand off. The repo ships a project MCP server, `cortados-route` (`.mcp.json`), exposing one tool,
`route_task(task, context?, max_tokens?)`: it sends a self-contained sub-task to the provider, S1 scores it, the cheapest
capable model runs it, and the text comes back. The main session keeps its own model and login.

Set two environment variables and have the provider running:

```bash
export CORTADOS_PROVIDER_URL=http://<provider-ip>:8432
export CORTADOS_PROVIDER_TOKEN=<token>
```

Claude Code reads `.mcp.json` from the repo root and offers to enable the server. Codex keeps MCP servers in its own
global config, so register it once (run from the repo root so the path resolves absolutely):

```bash
codex mcp add cortados-route \
  --env CORTADOS_PROVIDER_URL=$CORTADOS_PROVIDER_URL \
  --env CORTADOS_PROVIDER_TOKEN=$CORTADOS_PROVIDER_TOKEN \
  -- python3 "$PWD/cortados_router/subagent_mcp.py"
```

No address or token is committed; `.mcp.json` reads both from the environment, and a test (`tests/test_mcp_config.py`)
fails if a literal value is ever placed there.

## Delegating whole tasks from the Claude app

The same `cortados-route` server also hands off WHOLE tasks: each one runs as a separate headless Claude Code session
on the machine that runs the server (`claude -p` launched through `cortados_router.session`, so the provider env, the
`sub/<model>` pin and S1-routed native subagents are exactly those of a `cortados` session), with every tool and MCP
server the user's Claude Code has. Only the result comes back; the calling orchestrator verifies it.

Tools: `start_task(task, cwd, orchestrator?, permission_mode?, allowed_tools?, chrome?)` returns a `task_id` at once;
`task_status(task_id)`, `wait_task(task_id, timeout_s≤540)`, `list_tasks(limit?)`, `cancel_task(task_id)`.

- `orchestrator`: `cortados` (default, S1 routes every turn) or a pinnable model (`fable`, `opus`, `gpt-6.1-sol`,
  ...), which pins the child's MAIN model; the child's subagents are still routed by S1.
- `permission_mode`: `auto` (default: a safety classifier approves or denies each action, so a child can run Bash and
  tests unattended), `acceptEdits`, `manual`, `dontAsk`, `plan`. The child also gets `--permission-prompts none`, so
  anything that would prompt is denied. `bypassPermissions` is deliberately not offered for unattended runs; widen
  with `allowed_tools` (e.g. `["Bash(npm test:*)"]`) instead.
- Every child gets a fixed CHILD TASK directive (`child_tasks.CHILD_DIRECTIVE`: do the work yourself, do not call
  `start_task`, end with a report) via `--append-system-prompt`, so the orchestrator directive in a shared
  `~/.claude/CLAUDE.md` does not make it delegate again. With a pinned orchestrator the launcher's whole-session
  directive comes first and the child directive last, in the same flag.
- `chrome: true` (default false) passes `--chrome` so the child can use Claude in Chrome, which acts in the user's real
  signed-in Chrome: opt-in per task, no concurrency limit (each session gets its own tab group). Without it the
  child gets an explicit `--no-chrome`, so a `claudeInChromeDefaultEnabled` setting cannot turn Chrome on. Chrome inside a
  headless `-p` child is unverified until a live test.
- Connectors: claude.ai connectors are disabled in token-auth sessions, so if `~/.config/cortados/connectors.mcp.json`
  exists (or the file named by `CORTADOS_CONNECTORS_MCP_CONFIG`) the child gets it as `--mcp-config`, loading those
  servers directly; without it the child runs without connector servers (noted in `runner.log`).
- State: `~/.config/cortados/tasks/<task_id>/` (0700; logs, `result.json`). The token-bearing settings file is deleted
  when the child exits; finished task dirs older than 14 days are pruned on the next `start_task`.
  If the runner itself dies, the next status call stops claude's leftover process group (pid and start time are
  checked, so a reused pid is never signalled) and shows the unredacted logs only when the current credentials match
  the launch ones by sha256 fingerprint; otherwise it withholds them.
- Limits: no cap on parallel children (capacity is the router's job: S1 and the quota gates refuse or redirect a spent
  lane); 3 h per child (`CORTADOS_CHILD_MAX_S`), after which claude's process group gets SIGTERM, then SIGKILL 30 s
  later (cancel does the same); a child cannot start children.
- The token is read from `~/.config/cortados/provider-token` (or `CORTADOS_PROVIDER_TOKEN_FILE`). Both it and
  `CORTADOS_PROVIDER_TOKEN` are redacted from everything the tools return; a token shorter than 8 characters is refused
  (it could not be redacted safely). The desktop app
  starts servers with a thin PATH, so set `CORTADOS_CHILD_CLAUDE_BIN` to claude's absolute path.

Router logs: the child sends `x-cortados-run-id: <task_id>` on every provider request (added to
`ANTHROPIC_CUSTOM_HEADERS` next to `x-cortados-reveal`), so the provider's run record holds it under the task id; when
the task is done, `task_status`/`wait_task` fetch `GET /v1/cortados/runs/<task_id>` and return it as `router_run`
(models, primary lane; a fetch failure is a field, not an error). The run record is in memory, bounded (16 entries per
run, 512 runs) and forgotten on a provider restart. Each lifecycle event (`started`, `finished` with status, exit code
and duration, `cancelled`) is also appended as one JSON line to `$CORTADOS_HOME/child-tasks.jsonl` (default
`~/.cortados`): task id, cwd, orchestrator, permission mode, chrome flag, status and timings; never the prompt or a
secret.

Claude desktop app (`claude_desktop_config.json`, under `mcpServers`):

```json
"cortados-route": {
  "command": "python3",
  "args": ["/ABSOLUTE/PATH/TO/CortadOS-Router/cortados_router/subagent_mcp.py"],
  "env": {
    "CORTADOS_PROVIDER_URL": "http://<provider-host>:8432",
    "CORTADOS_CHILD_CLAUDE_BIN": "/ABSOLUTE/PATH/TO/claude"
  }
}
```

Claude Code, user scope:

```bash
claude mcp add -s user cortados-route \
  --env CORTADOS_PROVIDER_URL=http://<provider-host>:8432 \
  --env CORTADOS_CHILD_CLAUDE_BIN="$(command -v claude)" \
  -- python3 /ABSOLUTE/PATH/TO/CortadOS-Router/cortados_router/subagent_mcp.py
```

## The resource pool at a glance

A read-only `GET /resources` on the provider (same token) returns one JSON snapshot of the pool: logged routing usage (counts by band and lane, the degraded rate, S1 latency), each subscription's cached scarcity and expiring credit, the online local devices with their memory and GPU headroom, and a rough estimate of which local models would fit (`tables/local-models.json`; weights-only, an upper bound, never an admission promise). It opens no vendor CLI and never blocks on a probe: subscription numbers come from the cached reading and device telemetry is sampled on a background timer. This is the data behind the usage dashboard. The provider serves a browser dashboard at `GET /dashboard` (reachable from any device on the tailnet; it asks for the token and never embeds it), and a Claude Code pane that stays beside the chat lives in `integrations/claude-mod/cortados-pulse`.

## Status

- Live per-turn routing, with mandatory routing for agent, cron, delegation and auxiliary calls when enabled.
- The `cortados` provider endpoint with Claude and Codex backends (text), the Codex `/status` scarcity parser, local-hardware metering, and the S1 outcome loop.
- Shadow logging of every decision, and a `GET /resources` snapshot (usage, subscription scarcity, local devices, model-fit).
- The `cortados-routed-subagents` Claude Code mod, `sub/`/`api/` pins, independent reviews via `x-cortados-exclude-model`, and the run record behind `x-cortados-reveal`/`x-cortados-run-id`.

**Known limits:** pointing a whole session at the provider (`cortados`, `sub/`, `api/`) needs the Claude Code CLI, not the desktop app (the routed-subagents mod runs under both); a routed subagent has built-in tools only (no MCP, no browser) and runs under a 10-minute cap; worktree-isolated, remote, forked and named subagents pass through unrouted, not through the provider; and a Codex client on `/v1/responses` cannot use a `sub/`/`api/` pin (refused with 400) — it always routes through `cortados`.

**Tests:** `python3 -B -m unittest discover -s tests -t .` runs **747 tests, OK, 7 skipped** (the Hermes integration suite, which needs an importable Hermes). The tests pin a benchmark catalog (`tests/fixtures/`), so the nightly table refresh cannot break them.

## License

Copyright (C) 2026 Linjiu Li.

CortadOS Router is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 (GPL-3.0-only) as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see `LICENSE` for the full text.

Benchmark scores in `tables/benchmarks/scores*.csv` are derived from public sources listed in `tables/benchmarks/sources.json`; the raw downloads are not redistributed (run `fetch_benchmarks.py` to re-fetch).
