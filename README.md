# CortadOS Router

A model router. For every LLM call it decides **which model, on which subscription** should run it — Claude Code, Codex, Cursor, a local model — and sends the call there. The goal is one place that spends the cheapest capable resource for each request across a fleet of machines and subscriptions, instead of every tool reaching for the same frontier model.

It works in three steps:

1. **Score the request.** A small local model, **CortadOS Router S1**, answers 12 typed questions about the request. It never answers the request itself.
2. **Turn the answers into a requirement.** Hard rules and a band table turn the answers into a capability requirement: a required benchmark index for that kind of work, a band (R0–R4), and a sub-agent count.
3. **Pick the cheapest capable route.** Every model reachable through a subscription qualifies if its use-case index clears the requirement. The winner is the cheapest, where cost comes from the model's burn on its quota pool and that pool's live scarcity.

## Provider URL

Keep one provider base URL per machine in `~/.config/cortados/provider-url`
(for example `https://provider.example:8443`). Python clients resolve a non-empty
`CORTADOS_PROVIDER_URL` first, then `CORTADOS_PROVIDER_URL_FILE`, then that default
file. Whitespace and trailing slashes are stripped; missing, unreadable or invalid
selected files fail clearly. Only HTTP(S) URLs with a host are accepted.
Remove old per-client URL overrides to make moving the provider a one-file edit.
The session's `--provider-url` and the Claude mod's explicit `providerUrl` remain
overrides; leave them unset to use the shared file. Restart long-running clients
after editing it. HTTPS uses each client's normal certificate verification.

## Two ways to use it

**As a model provider (the main path).** The router exposes a single model named `cortados` over an HTTP endpoint. Point any coding tool at it; each new conversation is scored once and routed to a model, which it keeps for its follow-up turns, while a standalone tool-less request is routed on its own. The chosen model's text comes back, and its tool calls run in the client (a tool bridge). Claude Code talks to it on `/v1/messages`, Codex on `/v1/responses`. A client can instead pin a model (`sub/<model>`, `api/<model>`; see the table below) and skip S1 entirely. See `docs/PROVIDER-PLAN.md`.

Claude Code's WebSearch (a request whose only tool is the `web_search_*` server tool) is answered by a separate one-shot `claude -p` whose only tool is WebSearch, on a small Claude model; if that lane fails the model is told the search did not run. This is the one exception to "the headless CLI never runs a tool itself" (`cortados_router/provider/search_lane.py`, `docs/PROVIDER-PLAN.md` "Web search lane").

Two more pieces build on the provider, both delegating work rather than routing a whole session:

- **The `cortados-routed-subagents` Claude Code mod** (`integrations/claude-mod/cortados-routed-subagents/`). In its default **orchestrator mode** it directs the main session to plan and delegate through the cortados-route MCP tools (`start_task` / `route_task`) rather than do substantive work itself, and refuses the main session's own Agent-tool calls (the owner's rule; a routed Agent child dies with the session's hooks). Guidance for every other tool by default; `orchestratorEnforce` makes it a hard allowlist. Agent-tool subagents spawned below the main loop (or with orchestrator mode off) are intercepted and run as a headless `claude` against the provider instead: S1 scores and routes the subagent's model, while its tools still execute on your machine. Built-in tools only (no MCP, no browser); worktree-isolated, remote, forked and named agents pass through unrouted; a routed subagent runs under a 10-minute cap. Each run's real backend model and the lane's remaining window are shown via the run record (below), not just `cortados`.
- **The `cortados-route` MCP tool**, `route_task(task, context?, max_tokens?, model?)` (`cortados_router/subagent_mcp.py`), is text-only delegation: the main session keeps its own model and tools, and hands a self-contained subtask to the provider for S1 to score and route; the answer comes back as text, with no tool access of its own. `model` (optional) pins a model instead of S1: any name `start_task`'s `orchestrator` accepts (`fable`, `opus`, `claude-*`, `gpt-*`, or a friendly name such as `GPT 6.1` or `ollama 27b q3km` — see **Friendly model names**; one validation, `child_tasks.pin_model`), sent as `sub/<id>`; the result's `_meta` carries the requested and the routed model, and the text ends with `[cortados: S1 → <model> · <pct>% left]` (routed) or `[cortados: <model> · <pct>% left]` (pinned): route_task asks the provider to reveal the backend model. Token from `CORTADOS_PROVIDER_TOKEN`/`_FILE` or `~/.config/cortados/provider-token` by default. Setup below (Delegating sub-agent work).

**Recommended setup:** pin the main session to a strong model with `claude --model sub/opus` against the provider, and let the routed-subagents mod send its workers through S1 (`model: cortados` in the mod config, the default). S1 only scores a conversation's first message and the route is then fixed for that conversation — good for a worker whose task is stated up front, not for an orchestrating session whose first message ("let's get started") is too thin to score well. Pinning the planner sidesteps that; routing the workers still gets each one the cheapest capable model.

**As a decision library/CLI.** Call `core.decide` to get a routing decision for a request without running anything, for shadow logging or for a host that dispatches calls itself.

A frozen Hermes plugin lives in `integrations/hermes/`; it is kept for reference and is not the active direction.

## How the pieces run (a three-machine example)

The router is built to span machines on a private network (here, Tailscale). A working deployment separates three roles; all three can also run on one machine.

- **The scorer** serves S1. `python3 s1/serve.py` loads the promoted adapter and answers `POST /score`. It binds loopback by default; set `CORTADOS_S1_BIND` to a private-network address (and `CORTADOS_S1_TOKEN`) to let other machines reach it.
- **The provider** hosts the subscription CLIs it drives and exposes the `cortados` endpoint. It scores each conversation by calling the scorer, picks a lane, and drives a headless `claude` or `codex` as the backend. It resolves the scorer's address through `cortados_router.s1_endpoint` (an explicit URL, a named device in an endpoints file, or loopback).
- **The client** is the coding tool. It points `ANTHROPIC_BASE_URL` at the provider and runs `--model cortados`.

No model address is hardcoded. Devices live in `tables/s1-endpoints.json` (git-ignored; copy `tables/s1-endpoints.example.json`), so moving S1 to another machine is a one-line config change.

## Studio capacity controller (experimental; NOT deployed)

`cortados_router/studio_capacity.py` is a stdlib residency controller for a future
Qwen3 8B Q4_K_M Studio deployment. **No routing, local pin admission, S1 job, or
live configuration is changed.** Its status is an advisory ABI for a future
`local_resources` reader; this branch does not connect that reader. Locals remain
pin-only. Do not enable this service until Forest approves deployment.

Policy (binary GiB):
- Load only with no S1 heavy cycle/process and ≥16 GiB estimated reclaimable
  memory continuously for 120 seconds (8 GiB model allowance + 8 GiB safety).
- Unload immediately on an observed training/evaluation process, headroom <8 GiB
  (host/S1 reserve), non-normal kernel memory pressure, or swap ≥4 GiB. The swap
  ceiling tolerates the survey's existing ~1.84 GiB while conservatively shedding
  excess pressure; it is not a measured optimum or a swap-growth detector.
- Poll every 5 seconds. Missing/malformed measurements fail closed and attempt
  unload. Restart or a sampling gap >10 seconds resets the dwell (no proof of
  continuous safety across missing samples). A process lock prevents duplicate
  controllers using the same status path.
- Memory = vm_stat free + inactive + speculative pages × reported page size.
  memory_pressure -Q's percentage is diagnostic, **not** a byte budget.
- Detect actual `nightly.sh` processes through `ps -axo pid=,stat=,command=`,
  conservatively reserving the entire cycle including MLX/Laya preflight;
  also recognize direct MLX LoRA, Laya training and evaluate.py. Stopped
  processes count as busy. The idle overnight.py supervisor does not.
  `s1/overnight.py:status_path` points at historical dashboard events, not a
  trustworthy live heavy-phase lease, so it is deliberately not used.

**Training handshake (lease/ack):** every S1 training/eval step in `s1/nightly.sh`
(MLX LoRA, Laya `train.sh`, `evaluate.py`) runs under
`python -m cortados_router.studio_capacity gate -- <cmd>` (`training_gate()`). It writes an atomic
intent (`$CORTADOS_HOME/studio-gate/intent-<pid>-<nonce>.json`: pid, nonce, 300 s expiry refreshed
every 60 s) and waits up to 30 s for the daemon's `ack-<nonce>.json`. Each daemon tick treats any
live intent (unexpired, pid alive) as training: it sheds, confirms the model absent via `/api/ps`,
then acks; it never loads or renews while an intent is live. Expired/dead-pid intents are deleted.
On timeout the trainer proceeds with a log line if the daemon's status file is stale/missing (daemon
down), or, if the daemon is alive, sends `keep_alive=0` itself and proceeds only after `/api/ps`
confirms the model is gone (else it refuses to start). The intent is removed in `finally`. The
`ps`-based detection stays as a second line of defence. The daemon reads intents from `--gate`
(default `$CORTADOS_HOME/studio-gate`); the trainer reads status from `CORTADOS_STUDIO_STATUS`
(default the template's `~/Library/Application Support/CortadOS/studio-capacity.json`), so both
must agree on `CORTADOS_HOME` on the Studio.

**Remaining deployment blockers:** this branch does not prevent a pinned inference request from reloading an unloaded model.
A future gate must enforce fresh availability, one request at a time, exact
model/context identity and 8K total context. Residency here is verified by exact
Ollama model name, not a model provenance/context smoke test. No production-ready
admission guarantee is claimed.

### Status format (v1)

An atomic mode-0600 JSON file contains `version:1`, Unix `sampled_at` and
`expires_at` (15 seconds after sampling), `model`, `context_tokens:8192`,
`available` (boolean), `model_loaded`, `action` (load/unload/hold), `reason`,
and optional `inputs`/`unload_error`. Inputs contain training/eval flags,
reclaimable/swap bytes and diagnostic pressure percentage. `model_loaded` is
observational; failures may leave actual residency unknown. Consumers must
require version 1, exact model, available=true and
`sampled_at <= now < expires_at`; absent, malformed, future or expired snapshots
are unavailable. Status is local metadata, not an HTTP endpoint. A controller
crash leaves a snapshot to expire, not permission to infer.

### Proposed installation — for Forest to approve later

Nothing below has been executed on the Studio:
1. Review/implement the lease and gate blockers above; profile the idle/training
   peaks. Approve an existing/provenance-checked Q4_K_M 8B runtime named
   `studio-qwen3-8b:latest`; no model is downloaded or created by this daemon.
2. Configure Ollama loopback-only on 127.0.0.1:11434 with
   `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`. The controller uses
   only loopback /api/ps and /api/generate, disables HTTP proxies, preloads with
   a bounded keep_alive lease of 180 s (renewed only after a fresh safe observation, so a
   dead controller frees RAM within 3 minutes) and num_ctx=8192, and unloads with keep_alive=0
   (also on SIGTERM or a status-write failure). No pull API.
3. Copy `integrations/studio-capacity/com.cortados.studio-capacity.plist.template`
   to a temporary file; replace __HOME__ and __REPO__ with approved absolute
   Studio paths and verify the Python path. Create the status/log parent
   directories privately. Inspect with `plutil -lint` before installation.
4. After explicit approval, copy the rendered file into
   `~/Library/LaunchAgents/com.cortados.studio-capacity.plist`, then
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cortados.studio-capacity.plist`.
   Roll back using `launchctl bootout` with the same domain/file. Bootout alone
   does not prove the model unloaded; confirm release separately before training.

Focused acceptance (all I/O faked; fixtures include a local Mac vm_stat capture):
`python3 -B -m unittest tests.test_studio_capacity`.
Full regression and independent review are not part of this time-boxed build.

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

The pipeline is in `s1/`, run nightly by `s1/nightly.sh` (or back to back by `s1/overnight.py --continuous`; see `docs/ROUTER-DESIGN.md` §4d.1):

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
integrations/claude-mod/   cortados-routed-subagents: orchestrator directive + gate (delegate via cortados-route, Agent refused at top level), routes lower Agent-tool subagents through the provider; cortados-pulse:
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
| `sub/<friendly name>` (e.g. `sub/gpt-6.1`, `sub/sonnet-5`, `sub/ollama-27b-q3km`; spaces or dashes) | the one pool model whose name carries every word (see **Friendly model names**); an ambiguous name is refused with its candidates, never guessed | skipped |
| `api/<claude-model>` (bare `api` = Opus) | that model on the dollar-metered Anthropic **API credit** lane | skipped |

A `sub/` pin still writes a `routes` row (engine `pin`), so a pinned planner's usage is on record next to the routed workers'. Use it for an orchestrating main session whose first message is too thin to score well, with the routed-subagents mod sending the workers through S1. `GET /v1/models` lists `cortados` and every pinnable model as `sub/<id>` (display name e.g. `GPT-6.1 Sol · CortadOS`), derived from the tables, so a model picker can choose the orchestrator. When every Claude subscription account is spent the pin is refused with a pointer to the `api/` pin; it never falls over to API credit on its own.

**Friendly model names.** A pin may name its model in plain words instead of the exact id: `GPT 6.1`, `Sonnet 5`, `Opus 5.5`, `haiku 4.5`, `ollama 27b q3km`. One resolver, `routing.resolve_pin`, reads every pinned name: exact first (a served id, with or without its release date; a `sub/` alias; a local alias or tag), then friendly — the name is split into words on spaces, dashes, underscores, colons and slashes (never on `.`, so `6.1` is one word and a dashed Claude version such as `5-1` reads as `5.1`), and it means the single pool model (served Claude and Codex models plus the local pins) whose id, alias or display name carries every word; `ollama`/`local`, `claude`/`anthropic` and `codex`/`openai` only narrow the lane. A name that fits several models is refused with the candidates (`sub/gpt-6` → `gpt-6-astra, gpt-6-luna, gpt-6-sol`) and one that fits none with the pinnable list; nothing is guessed. Only the **model field** is read: prompt text is never searched, so "compare GPT 6.1 with Sonnet" in a request pins nothing. The same names work wherever a model is named — `route_task`'s `model`, `start_task`'s `orchestrator` (spaces become dashes on the way: `GPT 6.1` travels as `sub/gpt-6.1`), the `/cortados` command's first argument (quote a multi-word name) — and in a subagent prompt through the routed-subagents mod, where the one explicit form is the very first line of the prompt:

```
[cortados: model=GPT 6.1]
```

The mod strips that line, launches the child with `--model sub/gpt-6.1`, and reads no later directives; tags elsewhere (including pasted or fenced content) are left alone. `api/` directives are refused to prevent unexpected API bills. A continuation turn that repeats the friendly pin stays on the model it opened on.

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
  work and asks it to delegate with `mcp__cortados-route__start_task` / `route_task`. Subagents (hook input contains
  `agent_id`) keep every tool without a budget. The cortados-route tools (`mcp__cortados-route__*`, exact server
  prefix), TodoWrite, Read, Grep, Glob, ToolSearch, Skill, AskUserQuestion, ExitPlanMode and EnterPlanMode are
  unlimited; every other tool, including unknown future tools, Bash, edits, web and MCP, spends the budget.
  Set `CORTADOS_DELEGATE_ALLOW_MCP` to a comma-separated list of **exact** `mcp__...` tool names to exempt owner-approved
  MCP tools (default none; no wildcards). The launcher writes `CORTADOS_DELEGATE_BUDGET` into settings env.
  `--no-gate` disables enforcement independently of `--no-delegate-directive`.
  **Router outage:** once the budget is spent, the gate opens a TCP connection to the provider cortados-route
  delegates through (`CORTADOS_PROVIDER_URL`, else the launcher's `ANTHROPIC_BASE_URL`; 2 s timeout). If it cannot
  connect, the call is allowed and not counted, with a one-line stderr notice to the owner, so the session can do the
  work directly as the owner's rule says; Agent/Task stay refused. With no provider URL in the hook env there is
  nothing to check and the budget holds. If the provider is up but cortados-route is missing, the only override is the
  owner's: relaunch with `--no-gate` or a larger `--delegate-budget` (a switch the model cannot flip).
  The **Agent and Task tools are always denied in the main loop**, budget or not, with a reason naming `start_task`
  and the fallback when cortados-route is not connected (tell the owner in one line, do the work directly): the
  owner's rule (`~/.config/ai-coding/CORTADOS-ORCHESTRATOR.md`) makes cortados-route the only delegation path, and a
  routed Agent child dies with the session's hooks (2026-11-08, six builders aborted). This also covers fork
  subagents, which used to be denied separately (a fork inherits the pinned orchestrator model and history, skips S1
  and collides with the parent's backend conversation, 409).
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
`route_task` is refused inside a child task (`CORTADOS_CHILD_TASK` set): children may not route, as they may not start
children (the child's directive says so too).

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

## Local models (pin-only)

The owner's machines can serve their Ollama models through the provider as pins: `claude --model sub/qwen-27b`
relays each turn to that node's Ollama Messages API. S1 never routes there (the `auto_route` switch is off and
refused when on until local lanes are evaluated); an asleep or busy node fails the pin with a clear 503, never a
fallback. Ollama has no auth, so it stays on localhost and the node runs a token gate
(`python3 -m cortados_router.local_gate`) on its Tailscale address. Config schema, gate and launchd template:
`docs/LOCAL-RESOURCES.md` ("Local models").

### Local models on the Pro

The Pro gate admits probes and inference only on **AC power AND with the manual switch ON**.
The switch defaults off: `python3 -B -m cortados_router.local_resources toggle on|off|status`
(run with one of those three states). It uses `$CORTADOS_HOME/local-enabled` (default
`~/.cortados/local-enabled`); status prints the switch, availability and reason.
Unknown/failed `pmset -g ps` readings fail closed. Power is cached for at most 3 seconds;
the switch is checked on every request, including again after body upload. Disabled requests
return 503 without reaching Ollama. Fleet telemetry for node `pro` also includes this policy;
its deployments are unavailable when the policy is disabled or unknown. This does not
start/stop Ollama or unload resident models;
existing in-flight inference is not cancelled. Other fit, busy and exclusion checks still apply.

## Delegating whole tasks from the Claude app

The same `cortados-route` server also hands off WHOLE tasks: each one runs as a separate headless Claude Code session
on the configured EXECUTION HOST (below; by default the always-on Air, whichever machine runs the server), `claude -p`
launched through `cortados_router.session`, so the provider env, the `sub/<model>` pin and S1-routed native subagents
are exactly those of a `cortados` session, with every tool and MCP server Claude Code has there. Only the result comes
back; the calling orchestrator verifies it.

Tools: `start_task(task, repo?, branch?, cwd?, host?, orchestrator?, permission_mode?, allowed_tools?, chrome?, connectors?)` returns a `task_id` at once;
`task_status(task_id)`, `wait_task(task_id, timeout_s≤50)` (50 s default and cap: the Claude desktop app times MCP requests out at 60 s, and the reply, post-wait status work included, lands inside it; `CORTADOS_WAIT_MAX_S` (0-3600 s; a bad value is an error) raises it for clients with a longer timeout; a result with state `running` means call again), `list_tasks(limit?)`, `cancel_task(task_id)`.

- Being woken when a task ends (issue #87): `start_task`'s result carries `watch_command`, the exact shell command for
  the requesting machine (its absolute python and checkout, plus `CORTADOS_TASKS_DIR` / `CORTADOS_EXEC_HOSTS_FILE`
  when set), e.g. `/usr/bin/python3 -B /path/to/cortados_router/child_tasks.py --watch <task_id>`. **The orchestrator
  runs it in the background right after every start_task**: a Claude Code session is woken when a background command
  it started exits, and `wait_task` cannot block past 50 s. `--watch <task_id> [<task_id> ...] [--timeout S]
  [--interval S] [--stall S]` polls each task through the same path `task_status` takes (a remote task is asked on its
  host over ssh; never a local guess), every 30 s by default, and prints one compact JSON line per task as it ends
  (`task_id`, `state`, `host`, `elapsed_s`, `exit_code`, `error`, `workspace` state/path/branch, a warnings count;
  never the result text, the stderr tail or the prompt: `task_status` has those). Exit 0 when every task ended
  `succeeded` — not yet a checked result: read each line's `warnings` count and `workspace` state (`held` means
  unpushed work), or call `task_status`, before trusting it; 1 when any `failed`, `cancelled` or `stalled`; 2 when
  the watch itself could not run (an unknown id, a task whose status was never readable in 3 attempts — a down host
  or a typo fails in about a minute — or an unexpected error, named by type only on stderr); 3 when `--timeout`
  passed with tasks pending (those lines say `"watch": "timeout"`). 2 outranks 3 outranks 1 however the watch ends.
  `--timeout` is checked before every poll and caps a remote poll's ssh timeout, so it is overshot by one short poll
  at most. `stalled` means nothing about a running task changed for 30 minutes (`--stall`): state, phase, workspace
  state and `last_output_at`, the mtime of the child's logs that `task_status` now reports while a task runs (claude
  streams an event per tool call and reply, so a working child writes) — or that its status was readable and then
  was not for that long (a vanished task record or a host that stopped answering counts as unreadable; when every
  failed read in that window was a local error — a corrupt record on this machine, a permission — the line says so,
  `unwatchable`, exit 2: the watcher cannot read it, which says nothing about the task). Thirty minutes is above any
  single tool call seen on a child and far below the child's 3 h cap, so a hung child surfaces hours earlier. A task
  with a `phase` (`queued` for the host's Chrome lock) writes nothing while it waits, so it gets a longer window: the
  runner's own 6 h queue cap (`CHROME_QUEUE_MAX_S`: a third chrome task legitimately waits behind two) plus the 30 min
  (`WATCH_QUEUED_STALL_S`), after which a lock that never changed hands is reported as stalled too. Stalled is a
  report, not a cancel: the task keeps running on its host. Several task ids in ONE watch
  (`--watch <id> <id> ...`, one line per task as each ends) cost less than one watch per task: each remote poll is
  one ssh call, and one command wakes the session once per ending either way.

- Execution host (owner decision 2026-11-08, #89/#86; `cortados_router/exec_hosts.py`): `~/.config/cortados/exec-hosts.json`
  (`CORTADOS_EXEC_HOSTS_FILE`; copy `tables/exec-hosts.example.json`, which holds the Air entry) names the hosts and the
  `default` new tasks run on; `host` picks another; `CORTADOS_EXEC_HOST` overrides the default. Without the file
  start_task refuses: a task never quietly runs on the requesting machine. An entry whose `hostnames` include this
  machine (or has no `ssh`) is local and the child is spawned as before; otherwise start_task runs
  `ssh <entry.ssh> <python> <router>/cortados_router/child_tasks.py --remote` (BatchMode, StrictHostKeyChecking=yes,
  10 s connect, 30 s per call) with a JSON request on stdin, and keeps a stub (`<task_id>/remote.json`) so
  `task_status`, `wait_task` (the host waits; one ssh session held open), `list_tasks` (one round trip per host) and
  `cancel_task` (stops the process there) know where to ask. An unreachable host is a clear error naming it. The Air
  needs: this checkout at `router` at the same commit (the protocol version is checked), the hosts file with its own
  entry, `~/.config/cortados/provider-url` or `provider_url` in its entry, `~/.config/cortados/provider-token` (routed
  children authenticate to the provider with it; they need no Keychain), `claude` (its ssh PATH is bare: set `path`).
- Workspaces (`cortados_router/workspaces.py`; layout `~/CortadOS/README.md` on the Air): `repo` (a clone URL, or the
  name of a clone already under `<root>/repos/`) [+ `branch`] gives the task a git worktree at `<root>/active/<task_id>`:
  the host clones to `repos/<name>` if needed, fetches, and checks out `branch` (from `origin/<branch>` when it exists,
  else a new branch from the default; refused while another task has it checked out, or while a `held/` worktree
  holds it with unpushed work, the error naming that path) or, with no branch, a new
  `task/<task_id>` from the default branch, so parallel tasks never share a checkout. No repo and no cwd: a plain
  folder there. `cwd`: an existing directory ON THE HOST (not managed; a path that exists only on the requesting machine
  is refused saying so). The runner prepares the workspace before spawning (a clone can take minutes; start_task
  answers at once; a failed clone fails the task with the reason) and, when the child ends, moves the folder to
  `archive/` (every commit on a remote and nothing uncommitted or untracked, or no repository) or `held/` (anything
  else, or a worktree git cannot assess) with `git worktree move`, and records it in `<root>/index.jsonl` (one line per
  task, rewritten under `index.lock`). A plain folder containing a `.git` entry at any depth is conservatively moved
  to `held/` too (symlinks are never followed). An archived worktree has its HEAD detached at the same commit so its
  branch is free for the next task (`git update-ref --no-deref HEAD` with hooks disabled: no checkout, no file
  written; a failed detach is recorded as `detach_error` and loses nothing); a held one keeps its branch. An archive
  entry from before this that still holds the branch is detached when a new task asks for it, under the archive
  cleanup's lock, only if its tree is clean and its HEAD is the `origin/<branch>` tip fetched just then with an
  explicit all-heads refspec (a failed fetch refuses), and only if `active/`, `archive/` and `held/` are real
  directories and the entry is unchanged between assessment and detach; otherwise the start is refused. The start
  waits at most 60 s (`CLEANUP_LOCK_WAIT_S`) for that lock; past it the start is refused with "archive cleanup is
  running; retry in a minute" and never proceeds without the lock (#93).
  `task_status` shows `workspace` (kind, state, path, reason).
- Archive cleanup: `python3 cortados_router/child_tasks.py --cleanup-archive [--root <dir>]` deletes `archive/<task_id>`
  entries whose index record says archived more than 3 days ago, and nothing else: not a name that is not a task id,
  not a symlink (never followed), not a non-directory, not an entry resolving outside `archive/`, not one without an
  archive record, never anything in `held/`, `repos/`, `services/`. It has per-run deletion and elapsed-time limits;
  reassessed work moved to `held/` and errors consume the same time budget, and the time left in the budget is every
  git call's subprocess timeout, so one hung fetch cannot hold `cleanup.lock` past it (#93). An entry whose
  reassessment times out is left exactly as it is (reported under `timed_out`, counted as kept, retried by the next
  run), never deleted or moved. The cleanup rechecks plain folders for a nested `.git` immediately before deletion.
  Worktree metadata is never pruned globally (#93): `git worktree prune` is never run, every clone has
  `gc.worktreePruneExpire=never`, a deleted worktree's own admin dir is removed alone, every move is followed by
  `git worktree repair` (a failure is recorded as `repair_error`, never rolled back), and each cleanup (and each start
  that finds `cleanup.lock` free) repairs any stale registry entry under `active/`, `archive/` or `held/`: under the
  run's deadline, after the deferred settles, with a per-entry backoff in `index.jsonl` (`repair_failures`,
  `repair_next_at`: 1 h doubling to a week). An admin dir whose backlink names another existing worktree that names it
  back belongs to that worktree: it is never repaired over (reported, `registry_conflict`) and never deleted with
  another entry. With the admin dir kept, commits reachable from a worktree's HEAD or index survive any gc; a commit
  reachable only from reflog entries (committed, then reset away) is lost once git's normal reflog expiry drops those
  entries. That is acceptable: held work's commits are on its HEAD. `settle()`
  moves under `cleanup.lock` (waits at most 60 s; past it the folder stays in `active/` marked `settle_deferred` and
  the next cleanup settles it; `task_status` then reports the index's settled record). Lock order: clone lock -> `cleanup.lock` -> `index.lock`. `integrations/launchd/com.cortados.archive-cleanup.plist` runs it daily on
  the Air; task startup never runs cleanup, so starting a task is not delayed by cleanup locks or remote fetches.
- `chrome: true` on a `gui` host runs the child as a per-task LaunchAgent in the desktop session
  (`launchctl bootstrap gui/<uid> <task dir>/launchd.plist`, label `com.cortados.task.<task_id>`, booted out when the
  task is final): an ssh session cannot read the login Keychain that holds Claude's login, and Claude in Chrome needs
  the session Chrome runs in. The plist carries only the login-mode allowlisted env (`LOGIN_ENV_ALLOW`); the login
  rules (fail-closed settings check, which the runner repeats on the project files once the workspace exists; unrouted,
  main-subscription lane) are unchanged. Chrome serves one client at a time, so chrome children on a host take
  `<tasks dir>/chrome.lock` one at a time: the others wait (`task_status` phase `queued`, cancellable while waiting,
  given up after 6 h). Chrome on an ssh host without `gui: true` is refused before any round trip.
- A routed (non-chrome) task that names `connectors` on a `gui` host takes the same LaunchAgent path, because Claude
  Code keeps MCP OAuth tokens in the login Keychain too (seen on the Air 2026-11-09: a child spawned from ssh got no
  tools from its connectors and `CREDENTIAL_STORE_UNAVAILABLE`). It stays routed: S1, the provider URL and token from
  the 0600 settings file, the same env as a spawned child; no chrome lock or queue, no login mode. Its plist carries
  no env at all: the runner rebuilds its env from `<task dir>/session-runner-env.json` (0600, without the provider
  token or any variable the settings file sets) plus the settings file, and both are removed when the child exits. On
  a host without `gui: true` such a task spawns as before and its start and status carry a warning that connector
  OAuth may be unavailable. Tasks without connectors always spawn (`launch: "spawn"`).

- `orchestrator`: `cortados` (default, S1 routes every turn) or a pinnable model (`fable`, `opus`, `gpt-6.1-sol`,
  or a friendly name such as `GPT 6.1` or `ollama 27b q3km`, see **Friendly model names**), which pins the child's
  MAIN model; the child's subagents are still routed by S1. A `chrome` (login-mode) child runs unrouted on the
  claude.ai login, so it needs an alias or a full `claude-*` id, not a friendly name.
- `permission_mode`: `auto` (default: a safety classifier approves or denies each action, so a child can run Bash and
  tests unattended), `acceptEdits`, `manual`, `dontAsk`, `plan`. The child also gets `--permission-prompts none`, so
  anything that would prompt is denied. `bypassPermissions` is deliberately not offered for unattended runs; widen
  with `allowed_tools` (e.g. `["Bash(npm test:*)"]`) instead. Not every model has auto mode: Claude Code 2.1.316
  drops a session on a haiku model other than `claude-haiku-5-5` to `default` at startup ("does not support auto
  mode"), and a child then gets every approval-needing call denied ("no approval surface"). So with the mode unset
  such a model gets `acceptEdits` (start_task and task_status say so in `permission_mode_note`), and an explicit
  `auto` on it is refused at `start_task`.
- What the child really did: the child prints `--output-format stream-json`, and `task_status` reports `tool_uses`
  (counts by tool), `tool_errors`, `files_written` (writes whose tool_result came back without an error, matched by
  tool_use id), `write_attempts_unconfirmed` (failed or never answered, with the outcome), `effective_permission_mode`
  (the mode Claude Code actually ran in) and `permission_denials`, plus `warnings` when a "succeeded" child's result is
  not backed by its actions: denied tool calls, a mode Claude Code left, confirmed writes outside `cwd`, or write
  attempts that were not confirmed, or no tool call at all (a heuristic: the result's claims are then the model's
  word alone).
- Log redaction: stdout is redacted structurally first: a legacy single JSON record is parsed whole, otherwise each
  stream record (split on "\n" only) is parsed, its strings redacted (JSON held inside a string decoded and redacted
  too) and re-serialized ASCII-only. This fails closed: a record that does not parse, or a string or record too deep
  to decode or walk (~1000 levels), is replaced wholesale by a `[withheld: content could not be safely redacted]`
  placeholder and counted in `redact_unparsed_lines`, never text-redacted. The raw pass (each credential at up to
  eight JSON-escaping depths) then runs over everything as a second layer. If the structural pass itself fails, the
  task fails with `redact_error` (logs withheld) and the raw pass still runs. A `manual` child is reported by Claude
  Code as mode `default`, which is not treated as leaving the requested mode. A string that looks like JSON (an
  object, array or string literal) is decoded recursively, so a chain of quoted strings is unwound; one that does not
  decode is withheld, not text-redacted. Keys get the same treatment, and an object whose key cannot be redacted
  safely, or whose redacted keys collide, is withheld whole. The one exception is the child's free-form final
  `result` text (what `task_status` / `wait_task` return): prose may start with `[`, `{` or `"`, so it gets text
  redaction (every credential at all eight escaping depths) when stdout is rewritten, in `result.json` and in the
  status, and is never reparsed after truncation; it is withheld only if that redaction cannot run.
- Threat model: redaction protects against ACCIDENTAL credential exposure in logs and tool results, at any realistic
  encoding depth, and fails closed on anything it cannot parse. Encodings a child builds on purpose to smuggle out its
  own token are out of scope: the child already holds that token in its environment, and the same OS user can read
  the token file. The real fix for that is per-task scoped tokens, which the provider does not offer yet.
- Relay scratch dir: the provider's headless backend (Claude CLI or Codex) runs in an empty scratch dir, and its CLI
  tells the model so in its own environment block, next to the client's real one. A model that trusted it aimed a
  child's Write at `<scratch>/work/c.txt` (2026-11-02; the file landed on the client machine in a directory nobody
  looks at). The backend's system prompt now says which block to trust, and a bridged tool call whose input names the
  scratch dir is never forwarded: the model gets a tool error and retries with the user's path.
- Relay sandbox: the Codex backend's thread is read-only, and Codex used to say so in a `<permissions instructions>`
  block ("The sandbox only permits reading files"). A gpt-6.1-sol child took that as its own limit and "succeeded"
  with "this session's filesystem is read-only", without calling a tool (2026-11-02). The backend now starts with
  `include_permissions_instructions=false` (checked in the effective config like the other overrides; the sandbox
  itself stays read-only), and the relay note says the tools are the user's, on the user's machine, and that a
  backend sandbox or approval policy does not apply to them.
- Every child gets a fixed CHILD TASK directive (`child_tasks.CHILD_DIRECTIVE`: do the work yourself, do not call
  `start_task`, end with a report) via `--append-system-prompt`, so the orchestrator directive in a shared
  `~/.claude/CLAUDE.md` does not make it delegate again. Children never get the whole-session ORCHESTRATOR directive
  or its delegation gate, whatever `orchestrator` is: a pin only pins the child's main model (its native subagents
  are still S1-routed).
- `claude -p` and deferred tool loading: with tool search on, a headless session may not have an MCP tool's schema
  loaded and report e.g. "start_task is not a function". Run such sessions with `ENABLE_TOOL_SEARCH=false` so every
  MCP tool is loaded up front.
- `chrome: true` (default false) runs the child in LOGIN MODE (decided 2026-11-03): `claude` directly on the user's
  claude.ai login. Its env is an ALLOWLIST (`LOGIN_ENV_ALLOW`: shell basics, the bookkeeping paths, proxy/CA vars
  with any `user:pass@` proxy URL dropped); `LOGIN_FORBIDDEN_ENV` / `_PREFIXES` can never pass (provider URL and
  token, every `ANTHROPIC_*` / `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`, `SSH_AUTH_SOCK`, `CLAUDECODE`), so the provider
  credential never reaches a session that holds a signed-in browser. Its `--settings` file is secret-free (0600,
  `session-login.json`, removed when the child exits): only the routed-subagents plugin switched off. Refused when a
  managed-policy settings file sets those auth keys or `apiKeyHelper` (managed policy outranks the files below). `mcp__cortados-route` is in `--disallowedTools`. Why: through the provider (token
  or API-key auth) Claude Code 2.1.316 loads no `claude-in-chrome` server; on the login it does. So chrome tasks are
  NOT routed by S1 and spend the main Claude subscription; status shows `lane: "login (unrouted, chrome)"` and the
  model. Model: `cortados` -> `claude-sonnet-5-5`; a `claude-*` or `sub/claude-*` model as given; anything else is
  refused. Refused (fail closed) when `~/.claude/settings.json` or the cwd's `.claude/settings{,.local}.json` sets env
  `ANTHROPIC_API_KEY/AUTH_TOKEN/BASE_URL` or `apiKeyHelper`. `connectors` is refused for chrome tasks (the login loads
  the user's claude.ai connectors itself). Other browser control is blocked with `--disallowedTools` (osascript,
  `open -a`, Playwright, Puppeteer) and the child is told to use only `mcp__claude-in-chrome__*` and to stop and report
  if those tools are missing: a routed child once drove Chrome through osascript and kept stealing focus. Without
  `chrome` the child gets an explicit `--no-chrome` and is routed exactly as before.
  Caveat (verified 2026-11-03): a headless `-p` login child does load the 22 `mcp__claude-in-chrome__*` tools, but its
  calls returned "Browser extension is not connected" while the Claude desktop app held the extension. The child then
  stops and reports; a chrome task works only when no other session holds the extension.
- Connectors (opt-in): claude.ai connectors are disabled in token-auth sessions, so their servers are listed in
  `~/.config/cortados/connectors.mcp.json` (or the file named by `CORTADOS_CONNECTORS_MCP_CONFIG`). A child gets only
  the servers named in `connectors` (default none), written to a private per-task `--mcp-config` file that is removed
  when the child exits. Why: every connected server's tool schemas go up front in every request (a custom provider URL
  gets no client-side tool search). The full set measured ~1.1 MB, about 290k estimated tokens across 495 MCP tools
  (Vercel alone 241), on 2026-11-02. That is over a 200k model's input ceiling, so the provider refused every such
  child with 413, and Claude Code shows a 413 as "Request too large (max 32MB) … images".
- Refusals: the provider logs every refusal it answers (JSON errors, SSE error events, the local relay's errors) and,
  for an authenticated request, records it under its `x-cortados-run-id`; an unauthenticated one is logged apart and
  never recorded. Only status, error type and a fixed reason of the provider's own (for a backend failure, its
  allowlisted category) are kept, and the log line names an endpoint id, never the path; backend text stays out of
  both (the full message still goes to the client in the error response).
  `task_status` shows the last one as `provider_refusal` (and a `hint` for a 413).
- State: `~/.config/cortados/tasks/<task_id>/` (0700; logs, `result.json`). The token-bearing settings file is deleted
  when the child exits; finished task dirs older than 14 days are pruned on the next `start_task`, once their run
export succeeded (see "Task run export" below). A task record that
  exists but cannot be read (corrupt `meta.json` / `result.json`) never blocks `start_task` or `list_tasks`: it is
  skipped, logged to `child-tasks.jsonl` as `record_unreadable` (id, file, exception type; never content), reported
  in `start_task`'s `prune_errors` and shown by `list_tasks` as an `unreadable` row — and it is never pruned
  automatically, whatever its age or lock state, since without its pid a still-running child could lose the records
  its cancellation and recovery need; remove such a dir by hand once sure no child is behind it (#94).
  If the runner itself dies, the next status call stops claude's leftover process group (SIGTERM, then SIGKILL after
  5 s, a shorter grace than the runner's 30 s because recovery runs inside status/list/wait calls; pid and start time
  are checked, so a reused pid is never signalled) and shows the unredacted logs only when the current credentials match
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

### Task run export (Grounds capture)

Each finished child task (any terminal state) is exported as one JSON line for another system (Grounds) to capture
(#91). The export is written by the task runner, outside the child; the child's isolation is unchanged.

- Path: `$CORTADOS_HOME/exports/task-runs.jsonl` on the execution host the task ran on (dir 0700, file 0600), or the
  file named by `CORTADOS_TASK_EXPORT`. A remote task is exported on its host; `task_status` shows its `export`
  field. There is no network handoff: Grounds reads the file.
- Schema `cortados.task-run/1`, one object per line: `task_id`, `parent_task_id` plus `parent` (`known`, or the
  explicit marker `missing` when no parent is known; the parent comes from `start_task`'s optional `parent_task_id`, else
  `CORTADOS_PARENT_TASK_ID` in the launching session, validated as a task id), `title`, `host`, `client`, `state`,
  `exit_code`, `cancelled`, `timed_out`, `started_at` / `ended_at` / `elapsed_s`, `cwd` (home shown as `~`),
  `workspace` (kind, state, repo, branch, reason; no path), `orchestrator`, `permission_mode`,
  `effective_permission_mode`, `chrome`, `launch`, `lane` / `model` (login mode), `router_run` (model, label, provider
  with account alias such as `claude-cli#b`, models, builder_models, refusals; fetched by the runner while the
  provider still holds it, else a note or an error field), `tool_uses`, `tool_errors`, `error`, `prompt`, `events` in
  stream order (`text`, `tool_use` with name and input, `tool_result` with content as text, `is_error` and
  `omitted_parts` for non-text parts, `withheld`, and `result` with the final text), `event_counts`,
  `events_dropped` (past 2000 events), `redact_unparsed_lines` (what the runner's redaction withheld), `withheld`
  (that plus anything the export's own pass withheld), `truncated` (fields cut), `scrubbed` (pattern-scrub matches by
  type, below), `logs` (`included` or why withheld), `withheld_reason` (only when prompt, events or caller fields are
  withheld), `withheld_fields` (only when caller fields are, below), `exported_at`.
- Finalized and redacted: the line is written only after the run's logs were redacted and `result.json` is final.
  The runner redacts `stdout.log`, `stderr.log` and `prompt.txt` in place (temp file 0600, atomic rename) with the
  exact launch credentials before it writes the final `result.json`, and records `logs_redacted` / `prompt_redacted`
  there. Every text then goes through the Redactor that produced `result.json` and the pattern scrub, in that order,
  before it is capped at 32,000 characters per field (the cut is marked `[truncated by cortados: N of M characters
  cut]` and counted). A line that still matches a credential is not written. Raw `stdout.log` content is never
  written.
- Caller text: `start_task` checks what the caller supplies against every credential it knows, including the child's
  launch credentials, before `meta.json` exists. A label (the title, the client name, the Office tags) is redacted
  there; a value the task runs with (`cwd`, `repo`, `branch`, the orchestrator, `allowed_tools`, the session tag) that
  holds a credential is refused, naming the field, never the value. `meta.json` is then marked `caller_text_redacted`,
  and the runner redacts `result.json`'s error and workspace text with the launch credentials too.
- A late export (a status call after the runner died, or the cleanup retry) reads prompt and logs only when they are
  marked redacted, or when the current credentials provably include the launch ones (sha256 fingerprints in
  `meta.json`); it never redacts raw material with credentials that may have rotated since. Otherwise the record goes
  out with `prompt: null`, no events and a `withheld_reason`, keeping the metadata and `event_counts`. The caller
  fields (`title`, `client`, `cwd`, `orchestrator`, `model`, `error`, `parent_note`, the workspace's `repo`, `branch`
  and `reason`) follow the same rule: without the `caller_text_redacted` mark (a task started before it existed) and
  without matching credentials they are null and listed in `withheld_fields`. Withheld logs are only counted: they
  are parsed for `event_counts`, never redacted, scrubbed or kept. A runner that
  died before redaction: its task is exported by the first status call that can release its logs, else just before
  cleanup with them withheld.
- Best-effort pattern scrub, export only: after the known-value redaction, `Authorization:` / `Proxy-Authorization:`
  header values (and a value under such a key in a tool input), `Bearer <token>`, and the shapes `sk-…`, `sk-ant-…`,
  `ghp_`/`gho_`/`ghs_`/`github_pat_…`, `xox[abpr]-…`, `AKIA…`, `-----BEGIN … PRIVATE KEY-----` blocks and JWTs
  (`eyJ….eyJ….…`) become `[scrubbed:<type>]`, counted in `scrubbed`. The patterns run in linear time on any input
  (bounded quantifiers; a run of token characters is matched once, and a key's digit or a JWT's dotted parts are
  checked on that match), and only the first 32,000 + 4,096 characters of a field are scrubbed: the rest is cut, and
  the slack still catches a secret that straddles the cut. This is best effort, not a guarantee: the
  known-value redaction covers only the provider credentials (`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`,
  `CORTADOS_PROVIDER_TOKEN`), and any other secret of another shape passes. **The export holds far more than
  `result.json`: the full prompt and every tool input and result (file contents, command output, fetched pages) up
  to the field cap.** Treat the file as you would the task's workspace.
- Append-only, written once: one `os.write` of the whole line under an `flock` on `<file>.lock`, so concurrent
  runners never interleave. The file, the lock and the index are opened without following symlinks and must be
  regular files of this user; one with group/other permission bits is set to 0600 first (or refused if that fails),
  and the default directory to 0700; each tightening is listed in the outcome's `tightened`. A
  `CORTADOS_TASK_EXPORT` file in a directory that group or others can write is refused. The file is never rewritten or truncated by the router. A sidecar `<file>.ids` (same
  lock) holds the exported task ids, so a retry never appends a task twice.
- Outcome: `result.json` gets `export: {"ok": true, "at", "path_kind": "default"|"env"}` (plus `tightened`,
  `logs_withheld`, `prompt_withheld` when they apply) or
  `{"ok": false, "error": "<exception type>"}`; `child-tasks.jsonl` gets `task_exported` / `task_export_failed`.
- Never pruned unexported: the 14-day cleanup first retries a failed export (up to 10 per `start_task`, all within one
  5 s budget that includes the export-lock waits, so a stuck lock cannot hold `start_task`; the rest wait for the next
  start). A task whose export still fails or was not retried is kept, reported in `start_task`'s `prune_errors` and
  logged as `task_kept_unexported`.

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

## Bot gateway on the Air

`integrations/bot-gateway/install.sh` runs the bot gateway (`cortados_router/bot_gateway.py`: the full task surface
for Grok Bot since #86, design and decisions in `docs/GROKBOT-BRIDGE.md` section 0) as the LaunchAgent
`com.cortados.bot-gateway`, as the Air's normal user, on loopback port 8434 behind Tailscale Funnel. Run these on the
Air, from the checkout, in order:

```bash
cd ~/cortados-router && git pull
integrations/bot-gateway/install.sh token    # creates ~/.config/cortados/bot-gateway-token (0600); never prints it
integrations/bot-gateway/install.sh plist    # writes ~/Library/LaunchAgents/com.cortados.bot-gateway.plist
integrations/bot-gateway/install.sh load     # (re)loads it
integrations/bot-gateway/install.sh status   # launchctl state, unauthenticated POST /mcp (expect 401), log path + line count
```

The owner views the bot token himself (`cat ~/.config/cortados/bot-gateway-token`) and pastes it into Grok Bot's
secret field. The system prompt is in `docs/BOT-INSTRUCTIONS.md`. `install.sh unload` stops the gateway.

The plist holds `RunAtLoad`, `KeepAlive` (it keeps retrying while Tailscale is not up yet), logs to
`~/.cortados/bot-gateway.out`, `CORTADOS_PROVIDER_URL=http://127.0.0.1:8432` (the gateway runs on the provider's
host, and the provider listens on loopback only; other machines reach it through a tailnet-only Tailscale Serve), and
the gateway flags `--host 127.0.0.1 --port 8434 --token-file ~/.config/cortados/bot-gateway-token`. Set
`CORTADOS_BOT_PROVIDER_URL` before `plist` to point the gateway elsewhere; it must be an http(s) URL with a host and no
credentials, query or fragment (anything else is refused). Rerun `plist` and `load` after changing it. There is no
`--children-run-as-me` (the gateway binds loopback, so it does not need it). Since #86 the gateway starts children as
the owner on Grok Bot's behalf: tasks run on the Air's configured execution host (`~/.config/cortados/exec-hosts.json`
there) in `CortadOS/active/<task_id>` workspaces or any existing folder the bot names, with the caps and the
owner-approval rule described in `docs/GROKBOT-BRIDGE.md` section 0. A plist written before #86 carries
`--workspace-root`, which the gateway now ignores (it says so on stderr); rerun `plist` and `load` to drop it. The plist
holds no secret values.

**Provider token for route_task: design (a), read from `~/.cortados/provider.env` as data.** The provider's own
LaunchAgent already keeps its token in that file, so the gateway uses the same file and there is no second copy of the
secret to keep in step. Fallback, design (b): if provider.env does not assign the name, `plist` uses a 0600 file the
owner creates himself, `~/.config/cortados/bot-gateway-provider-token`, holding the token alone. If neither exists,
`plist` refuses to run. The installer only checks that a line assigns the variable *name* `CORTADOS_PROVIDER_TOKEN`
(`grep -q`, with the launcher's own line grammar: leading spaces or tabs and an optional `export`); it never reads
the value.

Both designs run the same stdlib launcher: `/usr/bin/python3 -I integrations/bot-gateway/launch.py
(--provider-env F | --provider-token-file F) --python /usr/bin/python3 -- <gateway flags>`. It:

- never executes provider.env. Lines are split on LF only (CRLF accepted), and only spaces and tabs are trimmed. It
  accepts only blank lines, `#` comments and `[export ]NAME=value`, with the value unquoted, single-quoted or
  double-quoted, and expands nothing;
- refuses the whole file, with exit 78 (EX_CONFIG) and a message naming the line number only (never a name or a
  value), for any other line: a control character (NUL, U+001C, a stray CR), a Unicode line separator, `$`, a
  backtick, a backslash, `set -x`, a trap, a command, `readonly`, or a repeated name;
- reads the token file (design b) as the token plus at most one trailing LF or CRLF, nothing else trimmed. The file
  must be 0600;
- requires the token to be non-empty printable ASCII with no spaces. A symlink, a file owned by someone else, a file
  that others may write (or, for the token file, read), or a missing `HOME`, `CORTADOS_HOME` or
  `CORTADOS_PROVIDER_URL` also exits 78;
- passes gateway flags only if each is an exact allowed spelling (`--host`, `--port`, `--token-file`,
  `--allowed-host`, `--workspace-root` for an old plist, `--children-run-as-me`). So the plist alone configures the
  gateway, and the gateway's own argparse does not expand abbreviations either (`allow_abbrev=False`);
- `os.execve`s the gateway as `python3 -I -S -B -c <bootstrap>`. The fixed bootstrap puts only this checkout on
  `sys.path` and runs `cortados_router.bot_gateway` with `runpy`. `-I -S` keep the child from loading any
  `sitecustomize` / `usercustomize` from the user site under `HOME` or from `PYTHONPATH`, code that would see the
  token and could edit the gateway's argv (review r4). The child gets an explicit environment: exactly `HOME`, a fixed
  `PATH`, `CORTADOS_HOME` and `CORTADOS_PROVIDER_URL` (all from the plist) plus the token. launchd's own environment
  (a `PYTHONPATH`, proxies, an ambient `CORTADOS_PROVIDER_TOKEN`) never reaches the gateway. The token is never on a
  command line and never printed.

The design does not source provider.env as shell code (review r2). Sourcing would let anything in the file print the
token (`set -x`, traps), hand it to another program, or put variables into the gateway's environment. Python adds
`LC_CTYPE` when it starts in the C locale, and `/usr/bin/python3` (Apple's shim) adds its developer-tools variables;
neither carries anything from provider.env. Tests: `tests/test_bot_gateway_install.py` (temp HOME, fake `tailscale` /
`launchctl` / `curl`, a fake `execve`).

## The resource pool at a glance

A read-only `GET /resources` on the provider (same token) returns one JSON snapshot of the pool: logged routing usage (counts by band and lane, the degraded rate, S1 latency), each subscription's cached scarcity and expiring credit, the online local devices with their memory and GPU headroom, and a rough estimate of which local models would fit (`tables/local-models.json`; weights-only, an upper bound, never an admission promise). It opens no vendor CLI and never blocks on a probe: subscription numbers come from the cached reading and device telemetry is sampled on a background timer. This is the data behind the usage dashboard. The provider serves a browser dashboard at `GET /dashboard` (see below), and a Claude Code pane that stays beside the chat lives in `integrations/claude-mod/cortados-pulse`.

## This machine's hardware as a resource

`python3 -m cortados_router.hardware sample [--out [PATH]]` measures this machine like a subscription's "% left": host,
chip, total and available memory (`vm_stat` free + inactive + speculative), CPU busy % (second sample of `top -l 2`),
load averages, GPU busy % (Apple IOAccelerator "Device Utilization %"), the Ollama models resident now (`/api/ps`,
0.5 s timeout), and a `headroom` block (free_mem_gb, cpu_idle_pct, gpu_idle_pct). It takes about 2 s (worst case about 22.5 s: every command hanging to its 4 s timeout plus the 2 s Ollama fetch budget and one last 0.5 s read). Anything that
cannot be read is marked unavailable with the reason and listed in `errors`; nothing is guessed. `--out` writes the
JSON atomically to the state dir (`$CORTADOS_HOME`, else `$CORTADOS_HERMES_HOME`, else `~/.cortados`) as `hardware/<host>.json` (0600; directories it creates are 0700, existing ones keep their mode). Each sample carries `sampled_at` (epoch s). It feeds the dashboard next and
routing later; nothing in routing reads it yet. Details: `docs/LOCAL-RESOURCES.md`.

## Status

- Live per-turn routing, with mandatory routing for agent, cron, delegation and auxiliary calls when enabled.
- The `cortados` provider endpoint with Claude and Codex backends (text), the Codex `/status` scarcity parser, local-hardware metering, and the S1 outcome loop.
- Shadow logging of every decision, and a `GET /resources` snapshot (usage, subscription scarcity, local devices, model-fit).
- The `cortados-routed-subagents` Claude Code mod, `sub/`/`api/` pins, independent reviews via `x-cortados-exclude-model`, and the run record behind `x-cortados-reveal`/`x-cortados-run-id`.

**Known limits:** pointing a whole session at the provider (`cortados`, `sub/`, `api/`) needs the Claude Code CLI, not the desktop app (the routed-subagents mod runs under both); a routed subagent has built-in tools only (no MCP, no browser) and runs under a 10-minute cap; worktree-isolated, remote, forked and named subagents pass through unrouted, not through the provider; and a Codex client on `/v1/responses` cannot use a `sub/`/`api/` pin (refused with 400) — it always routes through `cortados`.

**Tests:** `python3 -B -m unittest discover -s tests -t .` runs **2069 tests, OK, 7 skipped** (2026-11-08) (the Hermes integration suite, which needs an importable Hermes). The tests pin a benchmark catalog (`tests/fixtures/`), so the nightly table refresh cannot break them.

## License

Copyright (C) 2026 Linjiu Li.

CortadOS Router is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 (GPL-3.0-only) as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see `LICENSE` for the full text.

Benchmark scores in `tables/benchmarks/scores*.csv` are derived from public sources listed in `tables/benchmarks/sources.json`; the raw downloads are not redistributed (run `fetch_benchmarks.py` to re-fetch).

### Usage endpoint

`GET /v1/cortados/usage` (token required, 401 otherwise): one cheap JSON view for a usage dashboard, built by `cortados_router/provider/usage.py` — `lanes` (cached scarcity.json only: headline % left from the general `pool == "all"` windows routing uses — never a stale or per-model pool — per-window `window`/`resets_at`/`reset_epoch`/`headroom_pct`, per-pool headroom, account from the lane key (no paths; a cached Claude reading sampled from another login than the one configured for that key shows no numbers, `stale`, `error: reading_for_other_login`), sanitised `error_kind`, sample age, `stale` by the routing freshness rule; `cursor` carries `shared_with: ["Grok Bot"]`), `routed` (router.db counts per lane/model today and 7 d, top S1 bands, `today_truncated` / `last_7d_truncated` / `bands_truncated` when a list filled its cap; plus `provider_since_start` from the in-memory run records), `refusals` (24 h by reason, in-memory), `child_tasks` (7 d `by_event`/`by_status`/`by_client` from the bounded tail of child-tasks.jsonl — `by_client` counts tasks per attributing client, e.g. `grokbot` for the bot gateway, `direct` for the rest — `truncated` when clipped), `ledger` (usage.db rows from `usage_ledger`: turns and input/output/cache tokens per lane/model today and 7 d; pre-`lane` Hermes rows show under their runtime), `bots` (7 d bot-gateway calls from bot-usage.jsonl and its rotated `.1`: total, `by_client`, `by_tool`, `by_outcome`; absent until the gateway first runs), `s1` (1.5 s `/health` probe, memoised 10 s, no proxy, URL not echoed), `hardware` (`$CORTADOS_HOME/hardware/*.json` written by `cortados_router/hardware.py`, filtered to the sampler's own fields — an unreadable field keeps `available: false` and the reason's kind only when that is identifier-like (an exception type name; anything else becomes `error`), never its message — freshest 32 regular files (a FIFO or symlink is reported `not_a_regular_file` and never opened), with age; a file name that is not plain `[A-Za-z0-9._-]{1,64}` or holds an IPv4-looking address is shown as `<unnamed:<i>>` (its freshest-first position, so two stay distinct); any list field other than `load_avg` (3 numbers) is `available: false, reason: unexpected_type`; a file with none of its fields is reported `no_known_fields`; `errors[].field` is one of the sampler's field names or `unknown` (also for a null or container); an Ollama model name is published per `/` segment (`hf.co/<user>/<repo>:Q4`, at most 4 segments), any failing segment makes it `error`), `notes`, `errors` (every failed or absent source; a lane error scarcity did not classify is `unclassified`), `generated_at`. Every value leaves through one publish pass (`usage._publish` over the declared schema `_VIEW`), whatever section built it: undeclared keys are dropped; a leaf is only its declared type (bool, finite number, enum, or text) and a container where a leaf is expected is null; text is at most 60 chars (refusal reasons 120) of `[A-Za-z0-9 ._:()+%,#'…-]` with no IPv4-, IPv6- (`::` or 3+ colon-separated hex groups) or MAC-looking address, else `"error"` (so no path, `user@host`, markup or serialised object passes); every list and map is capped (lanes 32, windows per lane 16, pools 16, hardware files 32, Ollama models 32, card errors 16, counters 32, `load_avg` 3, `errors` 64 ending in `usage/errors_truncated`) two keys of one map that publish alike are combined (counters add, pool headroom keeps the minimum) and named `keys_merged`; and each cut adds one deduped `errors` entry naming its source (`scarcity/<lane>` + `windows_truncated` / `pools_truncated`, `scarcity` + `lanes_truncated`, `hardware/<file>` + `load_avg_truncated` / `models_truncated` / `errors_truncated`); the body is strict JSON. Memoised 2 s (the encoded bytes, so a GET serialises nothing; least recently used state dirs evicted first) and single-flighted, so concurrent polls run one collect; `router.db` has a `routes(ts)` index for the window counts. A failing view answers 500 `api_error` (the exception type goes to the provider log). Opens no vendor CLI.

Open `http://<provider>:8432/dashboard` in a browser on the tailnet to see it: one self-contained page (no external assets, served with `Cache-Control: no-store`, `nosniff` and a CSP whose `script-src` is only the sha256 of its one inline script) that asks for the provider token once, keeps it in the tab's sessionStorage only (in page memory, with a notice, when storage is unavailable), and every 30 s fetches `/v1/cortados/usage` with a Bearer header. It shows the view's `errors` first (every failed or absent source, e.g. `scarcity: OSError` when scarcity.json is missing, counted in the header and repeated where the empty section would be, so a missing source never reads as "no usage"), then a card per lane with its % left (stale readings greyed with their age, per-pool headroom, shared pools named, the lane's `error`), routed calls per lane/model today and over 7 days, the top S1 bands, ledger turns and tokens per lane/model, refusals by reason, child tasks by client/status/event, bot-gateway calls by client/tool/outcome, S1 health, HTTP status and latency (or its reason when down), a card per machine (CPU/GPU busy, memory, load, headroom, resident Ollama models, unavailable fields and sampler errors, sample age) and the notes. Capped lists say so; a failed poll greys the last good view and names the failure in red. A 401 forgets the token and asks again.

`GET /v1/cortados/tasks` (same token, 401 otherwise; built by `cortados_router/provider/tasks.py`) feeds the page's **Tasks** section: the latest 200 routing decisions from `router.db` (`ORDER BY ts DESC` over the `routes(ts)` index, one look-ahead row sets `truncated`; no `COUNT(*)`, no `core._db_lock`, the dashboard's read-only deadline-limited connection), each as metadata only — `task_id`, `ts`, `session_id` (the router's conversation id, not an Office tag), `lane`, `model`, `actual_model`, `band`, `degraded`, `elapsed_ms` (the recorded `turn_ms`), `request_chars`/`response_chars` (recorded sizes), `routing_kind` (`routed` / `pinned` / `unselected`) and `s1_reason`, a fixed-vocabulary summary of the recorded band, the typed S1 answers (`steps=…, work_kind=…, outward=…`) and the hard rules by category (`cheapest qualifying route`, `escalated difficulty floor`, …; a rule's text is never published, since escalation reasons and fit failures can embed user text). The `request`, `context`, `task_context`, `engine_error` and `features` columns are never read. Per-task **tokens are not recorded** (`usage.db` counts tokens per backend turn with no `turn_id` to join on): every task carries `tokens: null, tokens_status: "not recorded"`, no estimate. `sessions[]` gives, per recorded session within that window, `decisions`, `routed`, `pinned`, `unselected` and `routed_pct` = 100 × routed ÷ decisions (pins counted separately, degraded fallbacks counted as routed; the definition is in `percentage_definition` and printed on the page); `totals` is the same over the whole window and `unassigned_decisions` counts rows with no session id (never pooled into an invented session). Identifiers are validated with the Office view's rules (an id or model name holding an address or foreign characters is `null`). A missing or unreadable database is an `errors[]` entry (`router.db: absent` / the sqlite exception class), never an empty success; the page shows a failed tasks poll in red inside its section and leaves the usage view current.
