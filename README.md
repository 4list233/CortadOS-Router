# CortadOS Router

The model router from CortadOS, a personal fleet of AI agents that runs on the Hermes agent framework. For every LLM call it picks **which model, on which
subscription**: Codex, Claude Code, Cursor, Antigravity or a local model.

It does this in three steps:

1. **Score the request.** A small local model, **CortadOS Router S1**, answers 12 typed questions about the request.
   It never answers the request itself.
2. **Turn the answers into a requirement.** Hard rules and a band table turn the answers into a capability
   requirement: a required benchmark index for the request's kind of work, a band (R0–R4) and a sub-agent count.
3. **Pick the cheapest capable route.** Every model reachable through a subscription qualifies if its use-case index
   clears the requirement. The winner is the cheapest of those, where cost comes from the model's burn on its quota
   pool and that pool's live scarcity.

## The 12 questions

Six 0–3 rubric scores (the anchors are in `cortados_router/core.py → ANCHORS`):

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
- A task can be escalated mid-session, either because the agent says so or because its trajectory is long. It then
  keeps a difficulty floor.
- If S1 is down, the decision is marked degraded. The router then routes by scarcity alone in a fixed fallback band,
  and never defaults to Claude.

**Bands** (`tables/routing-table.json`; the first matching condition wins):

| band | condition | sub-agents |
|---|---|---|
| R0 reflex | D ≤ 2, `blast_radius` = 0, `context_size` ≤ 1 | 0 |
| R1 light | D ≤ 5, `blast_radius` ≤ 1, `context_size` ≤ 1, `parallelizability` ≤ 1 | 0 |
| R3 hard | D ≥ 9 or `blast_radius` = 3 | 3–5 + verifier |
| R2 standard | otherwise | 0–3, only when the work decomposes |
| R4 ceiling | never by score | escalation only |

**Required index.** The router interpolates the required index over D through knots pinned to reference models
(D = 3 / 6 / 9 / 11 / 12). The requirement never drops below the reference of the band the hard rules chose.
Each model has a per-use-case index in `tables/model-scores.generated.json`, built nightly by
`tables/benchmarks/build_tiers.py`. The index comes from public benchmarks (Epoch AI, LiveBench, Aider, SWE-bench,
LMArena, Artificial Analysis) weighted by `tables/use-cases.json`, plus a bounded community-sentiment nudge from
`tables/community/`.

**Cost.** Each candidate's cost is:

```
cost = burn weight + 30 × scarcity − 80 × expiring credit − capacity credit
```

- **Burn weight** is the model's price divided by the learned size of its quota pool (`unitcost.py`).
- **Scarcity** compares the % of a plan window already used with the % of that window's time elapsed. The router
  reads it from each vendor CLI's own `/usage` or `/status` screen through a PTY (`scarcity.py`), so no tokens are
  spent. A pool on pace to overshoot its window gets more expensive. A pool whose allowance is about to expire unused
  gets cheaper. A spent pool is skipped.

**Final gates.**

- Context fit is a hard gate.
- A vendor's models come from that vendor's own subscription first.
- Sessions stay on their model while it stays near the cheapest choice.
- Agent-facing ("face") turns never fall below a minimum general index.

`docs/ROUTER-DESIGN.md` is the full design log, with dates and rulings.

## CortadOS Router S1

S1 is a LoRA adapter on `Qwen3-4B-Instruct-2507` (MLX, 4-bit). Its completion is twelve `question=digit` lines, and
every answer is a single digit token. The router reads a probability for every allowed answer, so type errors are
impossible by construction. When a difficulty feature's top probability is below 0.75, the router takes the higher
of its two most likely levels. The `outward` question is called at the lower probability of 0.25.

The pipeline is in `s1/`; `s1/nightly.sh` runs it every night between 01:30 and 07:00:

| step | file | what it does |
|---|---|---|
| collect | `dataset.py` | collects routed requests |
| label | `label.py` | teacher labels: Claude through a stateless CLI, k = 3 samples per request as soft labels; train split by Sonnet, holdout by Opus |
| build | `build_sft.py` | builds the SFT set, oversampling the rare bands |
| train | `mlx_lm lora` | warm-starts from the promoted adapter |
| grade | `evaluate.py` | grades on a fixed 15 % holdout: band accuracy, under- and over-routing, outward recall, ECE, Brier |
| promote | `nightly.sh` | promotes only when band accuracy holds and neither under-routing nor outward recall regresses |

`serve.py` serves the promoted adapter at `POST 127.0.0.1:8431/score`.

The promoted adapter's holdout grade (229 rows, 2026-09-29) is `s1/current.example.json`:

| metric | value |
|---|---|
| band accuracy | 0.572 |
| under-routed | 0.057 |
| over-routed | 0.371 |
| outward recall | 0.966 |

**Not published:** model weights, adapters, training data and teacher labels. They are built from private
conversations. `.gitignore` keeps `s1/data`, `s1/runs` and `s1/current.json` out of the tree.

## Layout

```
cortados_router/      the router: core (scoring client, rules, resolve, logging), scarcity, probe_reaper, unitcost,
                      transport (input-size estimate), images (local OCR first, vision model only for photos), reviews (opt-in
                      dual review), execution_routing / cron_routing (mandatory per-call selection), task_context
                      (scorer wire contract), compat (env + optional host modules), paths
integrations/hermes/  the Hermes plugin (hooks, tools, /router command) and patches/ that install mandatory routing
                      at Hermes call sites (agent requests, cron, delegation, auxiliary calls)
s1/                   CortadOS Router S1: dataset, label, build_sft, evaluate, serve, student, coverage, drift, nightly
tables/               routing table, budgets, use cases, generated scores and tiers, benchmarks/, community/, inventory/
bin/                  refresh-inventory.sh, refresh-tiers.sh (run nightly by s1/nightly.sh)
tests/                standalone tests; tests/integration needs a Hermes host or the CortadOS tree, and skips without them
```

## Quickstart

```bash
python3 -B -m unittest discover -s tests -t .      # standalone suite (stdlib only)
python3 cortados_router/core.py table              # load and validate the routing table
CORTADOS_HOME=$(mktemp -d) python3 cortados_router/core.py decide "rename the variable in utils.py"   # needs S1 on :8431
python3 cortados_router/scarcity.py                # opens claude / codex / cursor-agent / agy in a PTY and reads /usage
```

| setting | default | purpose |
|---|---|---|
| `CORTADOS_HOME` | `~/.cortados` | state: `router.db`, `scarcity.json`, `unit-cost.json`, `reviews.db` |
| `CORTADOS_ROUTER_TABLES` | `tables/` | tables directory |
| `CORTADOS_ROUTING_TABLE` | `tables/routing-table.json` | a single routing table |
| `CORTADOS_S1_URL` | `http://127.0.0.1:8431` | the S1 scorer |

Legacy `WANGCAI_*` names still work and log a deprecation warning.

**Hermes setup:**

1. Link `integrations/hermes/` into the profile's plugins directory as `cortados-router`. The plugin finds
   `cortados_router/` two levels up.
2. Install the patches against a Hermes checkout. Each script has a `--check` mode:
   `python3 integrations/hermes/patches/required-routing.py --hermes-root $HERMES_ROOT`.
3. Set `agent.model_router: cortados-router` to make routing mandatory.

The CortadOS Context engine (`context.hermes_engine`) and fleet roster (`faces.json`) are optional. Without them the
plugin routes every turn as profile `default` with no task context or recall. It records what it skipped in
`cortados_router.compat.missing_modules()`.

## Status

This is the code CortadOS runs today, extracted from its private repo without rewriting it:

- Live per-turn routing for chat turns.
- Mandatory routing for agent, cron, delegation and auxiliary calls, when a profile sets `agent.model_router`.
- Shadow logging of every decision.

**Tests.** `python3 -B -m unittest discover -s tests -t .` runs 155 tests: 148 pass, 6 are skipped and 1 fails.
- **Skipped:** the six Hermes integration modules in `tests/integration`, which need pytest and an importable Hermes.
- **Failing:** `test_profile_instructions_are_opt_in_and_dynamic`. It reads CortadOS's live `faces.json` and fails until the
  opt-in dual-review rules are applied there; that deployment is still pending.

The tests use a pinned benchmark catalog (`tests/fixtures/`), so the nightly table refresh can't break them.

## License

Copyright (C) 2026 Linjiu Li.

CortadOS Router is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 (GPL-3.0-only), as published by the Free Software Foundation. It is distributed WITHOUT ANY WARRANTY; see `LICENSE` for the full text.

Benchmark scores in `tables/benchmarks/scores*.csv` are derived from public sources listed in `tables/benchmarks/sources.json`; the raw downloads are not redistributed (run `fetch_benchmarks.py` to re-fetch them).
