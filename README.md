# CortadOS Router

A model router. For every LLM call it decides **which model, on which subscription** should run it — Claude Code, Codex, Cursor, a local model — and sends the call there. The goal is one place that spends the cheapest capable resource for each request across a fleet of machines and subscriptions, instead of every tool reaching for the same frontier model.

It works in three steps:

1. **Score the request.** A small local model, **CortadOS Router S1**, answers 12 typed questions about the request. It never answers the request itself.
2. **Turn the answers into a requirement.** Hard rules and a band table turn the answers into a capability requirement: a required benchmark index for that kind of work, a band (R0–R4), and a sub-agent count.
3. **Pick the cheapest capable route.** Every model reachable through a subscription qualifies if its use-case index clears the requirement. The winner is the cheapest, where cost comes from the model's burn on its quota pool and that pool's live scarcity.

## Two ways to use it

**As a model provider (the main path).** The router exposes a single model named `cortados` over an HTTP endpoint. Point any coding tool at it; each new conversation is scored once and routed to a model, which it keeps for its follow-up turns, while a standalone tool-less request is routed on its own. The chosen model's text comes back, and its tool calls run in the client (a tool bridge). Claude Code talks to it on `/v1/messages`, Codex on `/v1/responses`. See `docs/PROVIDER-PLAN.md`.

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

Local hardware can be metered as a resource pool with `CORTADOS_LOCAL_RESOURCES_CONFIG`; see `docs/LOCAL-RESOURCES.md`.

## CortadOS Router S1

S1 is a LoRA adapter on `Qwen3-4B-Instruct-2507` (MLX, 4-bit). Its completion is twelve `question=digit` lines, one single-digit token each, so the router reads a probability per allowed answer and type errors are impossible by construction. When a difficulty feature's top probability is below 0.75 the router takes the higher of its two likeliest levels; the `outward` question is called at a stricter 0.25.

The pipeline is in `s1/`, run nightly by `s1/nightly.sh`:

| step | file | what it does |
|---|---|---|
| collect | `dataset.py` | gathers routed requests; a request's train/holdout split is fixed by its id |
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
                           transport, images, reviews, execution_routing / cron_routing, task_context,
                           s1_endpoint (where the scorer lives, config-driven), compat, paths
cortados_router/provider/  the `cortados` endpoint: server (HTTP + conversation registry + wire seam),
                           anthropic_wire (/v1/messages) and responses_wire (/v1/responses), routing (lane choice),
                           bridge (headless Claude) and codex_bridge (codex app-server) with codex_isolation,
                           mcp_shim (serves the client's tools to the backend)
s1/                        CortadOS Router S1: dataset, label, build_sft, evaluate, serve, student, outcome/, nightly
tables/                    routing table, budgets, use cases, generated scores, benchmarks/, community/, inventory/,
                           s1-endpoints.example.json
integrations/hermes/       the frozen Hermes plugin (reference only)
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

| setting | default | purpose |
|---|---|---|
| `CORTADOS_HOME` | `~/.cortados` | state: `router.db`, `scarcity.json`, `unit-cost.json`, `reviews.db` |
| `CORTADOS_ROUTER_TABLES` | `tables/` | tables directory |
| `CORTADOS_S1_URL` | resolved | the scorer URL; wins over the endpoints file |
| `CORTADOS_S1_ENDPOINT` | — | a device name in `tables/s1-endpoints.json` (`CORTADOS_S1_ENDPOINTS` relocates that file); else its `default`, else loopback `127.0.0.1:8431` |
| `CORTADOS_S1_TOKEN` | unset | bearer sent by clients and required by a token-guarded scorer |
| `CORTADOS_S1_BIND` | `127.0.0.1` | interface `s1/serve.py` listens on |
| `tables/accounts.json` | absent | multiple accounts per vendor (git-ignored; copy `accounts.example.json`). Each Claude account is its own lane/pool metered from its own `CLAUDE_CONFIG_DIR`; routing steers to the account with unused expiring credit, and a `reserve` account is spared while another has room |
| `CORTADOS_PROVIDER_TOKEN` | generated | the `cortados` endpoint's bearer token |
| `CORTADOS_PROVIDER_CLAUDE_BIN` / `CORTADOS_PROVIDER_CODEX_BIN` | `claude` / `codex` | backend CLIs the provider drives |
| `CORTADOS_PROVIDER_CODEX_HOME` | unset | a dedicated logged-in `CODEX_HOME` for the Codex backend |
| `CORTADOS_PROVIDER_FIXED_MODEL` | unset | pin one Claude model, no routing |

Legacy `WANGCAI_*` names still work and log a deprecation warning.

## Status

- Live per-turn routing, with mandatory routing for agent, cron, delegation and auxiliary calls when enabled.
- The `cortados` provider endpoint with Claude and Codex backends (text), the Codex `/status` scarcity parser, local-hardware metering, and the S1 outcome loop.
- Shadow logging of every decision.

**Tests:** `python3 -B -m unittest discover -s tests -t .` runs **440 tests, 7 skipped** (the Hermes integration suite, which needs an importable Hermes). The tests pin a benchmark catalog (`tests/fixtures/`), so the nightly table refresh cannot break them.

## License

Copyright (C) 2026 Linjiu Li.

CortadOS Router is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 (GPL-3.0-only) as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see `LICENSE` for the full text.

Benchmark scores in `tables/benchmarks/scores*.csv` are derived from public sources listed in `tables/benchmarks/sources.json`; the raw downloads are not redistributed (run `fetch_benchmarks.py` to re-fetch).
