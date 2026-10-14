# Router and sub-agent design for Cortados (draft for Forest, 2026-09-19)

> Public copy of the CortadOS design log. It is a dated record, so early sections describe plans that later sections
> supersede (4c-1 supersedes the band lists, 4d the interim engine). Paths are rewritten to this repository's layout
> (`cortados_router/`, `integrations/hermes/`, `s1/`, `tables/`); references to `faces.json`, the PTY plugin, `bin/` tools
> other than `refresh-*.sh`, `discovery/` and the 2.0 code base point into the private CortadOS tree. The trained
> scorer formerly called "wangcai-s1" is **CortadOS Router S1**. Private request text and quotes have been paraphrased.

Ports the Wangcai OS 2.0 routing scale onto a Hermes hook, with TypeSafe's Jev-style typed decisions as the
decision format. Evidence: `discovery/2.0-routing/` (files copied verbatim from `scaffold-broker`), the Jev docs
(docs.typesafe.ai), Hermes source (`gateway/run_inbound.py`, `agent/turn_context.py`, `gateway/slash_commands_model.py`).
Nothing here is built yet.

## 1. What 2.0 already decided (and what it did not)

**Law (G-entries):** G112 layered brain, pool {Codex, Claude, local, Gemini}, *Fable is escalation ceiling only, never a
default*; G153 pool = local / claude / codex / antigravity / cursor (Grok-first, `auto` fallback, every other vendor id
refused on Cursor); G154 every model call routed; G156 learning starts from live usage, one bounded shadow at a time,
blast radius may never be trained downward; G159 "bands follow the host benchmark battery".
**Not law:** the routing table itself (`routing-table-v1.json` says "CANDIDATE, NOT LAW"), every tier→model pin
(`recommended_awaiting_ESC-RES-G`), all concurrency caps and spend ceilings (ESC-RES-A/B unset). Two RAM-gate factors
exist (1.5 in scaffold-broker, 1.25 in scaffold-pool-v2), unreconciled.

### The scale (verbatim from `routing-table-v1.json`)
Six features, integers 0–3:

| feature | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| steps | one action or lookup | a short sequence with one dependency | a multi-step chain with several dependencies | a long dependent chain |
| ambiguity | directive fully determines the work | minor interpretation with an evident objective | substantial interpretation among plausible objectives | intent must be substantially interpreted |
| blast_radius | words only, nothing changes | a bounded reversible workspace change | a broad or difficult-to-reverse system change | law, money, production, or irreversible effects nearby |
| context_size | fits well inside the local 40,960-token window | fits the local window with moderate headroom | approaches or crosses the local window and benefits from decomposition | exceeds a single frontier context; must be decomposed |
| novelty | matches patterns in memory/corpus | a known pattern with a small variation | a materially new pattern needing investigation | nothing like it recorded |
| parallelizability | strictly serial | mostly serial with one independent branch | several independent subtasks can proceed concurrently | mostly independent subtasks |

Difficulty `D = steps + ambiguity + blast_radius + novelty` (0–12). `context_size` and `parallelizability` are modifiers.

Bands, first match wins, evaluated in this order:

| band | condition | orchestrator | worker tier |
|---|---|---|---|
| R0 reflex | D≤2 and blast_radius=0 and context_size≤1 | none | W-local |
| R1 light | D≤5 and blast_radius≤1 and context_size≤1 and parallelizability≤1 | none | W-fast |
| R3 hard | D≥9 or blast_radius=3 | O-opus | W-std (W-heavy when blast_radius=3) |
| R2 standard | D≤8 or parallelizability≥2 or context_size≥2 | O-sonnet | W-std, per-subtask rescore |
| R4 ceiling | never by score; escalation only | O-fable | W-ceiling |

Degraded scorer → R2 for interactive, R1 for background. Ladders step one tier at a time, never skip, never go down:
`W-local → W-fast → W-std → W-heavy → W-ceiling`, `O-sonnet → O-opus → O-fable`. An outward verb in the directive floors
`blast_radius` at 3 (`src/canary/routing.mjs`).

Candidate order per band (`canary-lane-order.json`, authority G153/G154/G155.2):

| band | 1st | 2nd |
|---|---|---|
| R0 | local `qwen3-14b-nothink` (Forest 2026-09-19: the 8B hallucinates; was `qwen3:8b` in 2.0) | — |
| R1 | cursor `latest-grok` | antigravity `Gemini 3.7 Flash (Medium)` |
| R2 | claude `claude-sonnet-5` | codex `gpt-5.6-terra` |
| R3 | claude `claude-opus-5` | codex `gpt-5.6-sol` |
| R4 | claude `claude-fable-5-1` | codex `gpt-6-astra` (Forest 2026-09-19: R4 may now be the GPT ceiling as well as Fable; 2.0 had Fable 5 only and no substitute) |

Full lane registry: local (`qwen3:8b`), cursor (`latest-grok`, `auto`), antigravity (`Gemini 3.7 Flash (Medium)`,
`Gemini 3.1 Pro (High)`), claude (haiku-4-5 / sonnet-5 / opus-5 / fable-5), codex (`gpt-5.3-codex-spark`, `gpt-5.6-luna`,
`gpt-5.6-terra`, `gpt-5.6-sol`). No DeepSeek or Copilot lane exists.

### The one real measurement (BATTERY 2026-09-02, 77 live calls, Cursor excluded: keychain)
Median latency ms: qwen3:8b 4156 · Sonnet 5 5544 · Opus 5 6266 · Fable 5 6361 · Haiku 4.5 6606 · Gemini 3.7 Flash 7022 ·
Gemini 3.1 Pro 10135 · Codex Spark 14050 · GPT-5.6 luna 15207 · sol 15929 · terra 17361. All models 5/5 on the
deterministic tasks; the run proposed no bands. `MODEL-BENCHMARKS.md` is third-party literature, not Forest's harness.
My own 8-task check on 2026-09-18 is NOT a benchmark of this scale; its one useful datum is that `qwen3:8b` invented a
workout when asked to log one with no details, which matters for R0 (see Q7).

### Decomposition in 2.0 (`src/orc`)
Direct answer only if D≤5 and blast_radius=0 and context_size≤1 and parallelizability≤1 and no escalations; otherwise the
plan is compiled into subcontracts. Hard bounds: **max 3 subcontracts**, 1 acceptance row each, 3,000-byte plan document.
`blocked_by` edges → waves by topological sort → each wave runs in parallel. Budgets are divided evenly across
subcontracts. Every planned subtask must report exactly once; any failure makes the reply a bounded refusal. Judge effort
follows the route band (R0/R1→b1 … R4→b4). Workers never pick their own route. "Worker death is not evidence of task
difficulty."

### Learning in 2.0
Logged per routed turn: `res.route.v1` (features, band, tier, lane, engine), `res.outcome.v1` (result class, tokens,
wall-clock, verdict), `res.escalation.v1`. Trainer status: 3–4 examples, collapsed to "everything heavy" (6/16), never
promoted. No shadow-sampling code exists. So: the logging schema is reusable, the trained router is not.

## 2. Jev, and why its shape fits

Jev (TypeSafe, "System One" model): you send a **state** plus **typed questions**, it returns typed answers with
calibrated probabilities; it cannot emit free text. Three primitives, evaluated in parallel and in isolation in one call:
**Choice** (one of N options), **Score** (ordered rubric levels), **Noul** (probability a statement is true).
70–500 ms, ~$0.042 per million input tokens, output free, ~32k tokens for state + questions, no images, early access,
no open weights, evals are their own workflows. Trained with "Reinforcement Learning for Calibrated Decisions".
Their patterns: composite scoring (independent rubric scores, weights in code), intent routing (Choice + complexity
Score), confidence-gated routing (act / caution / escalate, stricter for destructive actions), speculative fan-out.

The 2.0 scorer IS a composite score: six rubric Scores combined by a band table in code. So the question set is:

| # | type | question | used for |
|---|---|---|---|
| 1–6 | Score ×6 | the six features with the anchors above, verbatim | D, band |
| 7 | Noul | "Carrying this out sends, spends, books, publishes, deletes, or changes something outside this machine" | floors blast_radius at 3; replaces the brittle outward-verb regex (the 2.0 Scenario-10 bug: the noun "message" matched) |
| 8 | Choice | work kind: conversation · capture/log · code · research · writing · planning · ops | lane family preference inside a band (code → codex first, research/writing → claude/antigravity) |
| 9 | Choice | urgency: answer now · today · overnight is fine | lets non-urgent hard work go to the slow local 27B |
| 10 | Noul | "The request cannot be done without a clarifying answer from Forest" | park instead of guessing (A-012) |
| 11 | Score | independent parts: 1 · 2 · 3 · 4–5 · 6+ | sub-agent count input |
| 12 | Noul | "Two independent attempts by different models would materially improve the result" | diversity mode (section 4) |

**Engine (Forest 2026-09-19): not the Jev API. Train our own local decision model to Jev's standard.**
Same interface `decide(state, questions) → answers{value, probabilities, confidence}`, two implementations:

*Interim engine (day one):* `qwen3-14b-nothink` with a JSON schema for all twelve answers, 2.0's method with the bigger
model. Uncalibrated, a few seconds per decision. Good enough for shadow mode and for producing comparison data.

*Trained engine (CortadOS Router S1), what "Jev's standard" means concretely:*
1. **Typed outputs only.** The model never writes prose. For each question we read the probability of each allowed
   answer token (`0 1 2 3`, the option letters, `yes no`) at one fixed position. One forward pass gives a full
   distribution per question: type errors are impossible by construction, as with Jev.
2. **Parallel questions.** State is encoded once; the twelve question suffixes share that prefix (KV-cache reuse), so
   adding questions is nearly free. Target under 500 ms on this Mac with a 4B-class model, 4-bit.
3. **Calibrated probabilities.** Train on SOFT labels, then temperature-scale on a held-out set. Report Expected
   Calibration Error and Brier score per question, not just accuracy. Confidence = a statistic of the distribution, used
   for fail-up exactly as designed above. (Jev's RLCD recipe is unpublished; soft-label distillation plus temperature
   scaling is the standard, reproducible route to calibrated classifiers. A calibration-reward RL stage can come later.)
4. **Data.** Seed = `labeling-sheet.csv`: 761 real, scrubbed requests Forest sent to 1.0 (median 65 chars; heuristic
   bands R0 536 / R1 159 / R2 43 / R3 23; Forest's `correct_band` column is empty). Plus Hermes turns so far (~100) and
   every routed turn from shadow mode onward. Rare classes (R2/R3, outward actions, clarification-needed, many-part
   tasks) are topped up with teacher-written variants of real requests, tagged synthetic.
5. **Teacher.** Each example is labelled on all twelve questions by a strong model through the stateless Claude brain,
   k=5 samples each → the empirical distribution is the soft label. Disagreement between samples is itself the signal
   for "uncertain". Cost estimate: ~900 examples × 5 samples, short prompts, on Sonnet; run overnight, watched by the
   scarcity rule below so it never eats the daily Claude budget.
6. **Gold set.** ~100 rows Forest labels by hand (the sheet is built for 30 s per row). Never trained on; it is the
   promotion gate. 2.0's rule carries over: a candidate is promoted only if it beats the incumbent on routing accuracy
   with zero regression on the outward-action and clarification questions, and blast radius is never trained downward.
7. **Training.** LoRA with MLX on the local Mac, overnight. Base: a Qwen3 4B-class model (8B weights are already on
   disk as a fallback base). Loss = cross-entropy against the soft label at the answer position, all twelve questions
   multi-task. One run is on the order of an hour or two at this data size. MLX is not installed yet.
8. **Serving.** A tiny local HTTP service next to the memory gateway, loaded permanently (a 4B at 4-bit is ~2.5 GB).
   Timeout or error → 2.0's fallback row (R2 interactive, R1 background).
9. **Loop.** Nightly: export the day's routed turns + Forest's corrections → relabel uncertain ones with the teacher →
   retrain → gate on the gold set → promote or discard, with the result posted in `#ops`.
Confidence rule (both engines): if a feature's confidence is below threshold, take the HIGHER of its two most likely
levels (fail-up); `blast_radius` and question 7 are never lowered by any later step.

## 3. The hook in Hermes

```
Discord/Telegram message
  └─ pre_gateway_dispatch hook (plugin cortados-router)            ← receives event + gateway
       1. hard rules (section 5)  2. decide() with the 12 questions  3. band table → band, tier, orchestrator
       4. lane order for the band, filtered by lane health (ledger cooldowns, fleet-health probes, RAM gate for local)
       5. set the conversation's ONE-TURN model override {provider, model}   (same structure /model --once uses)
       6. stash the decision for this turn
  └─ pre_llm_call hook                                             ← can inject text into the turn
       inject the plan: "band R2, you are orchestrator on sonnet; split into 3 sub-agents: A codex, B claude, C agy…"
  └─ the face (persona unchanged, brain swapped) answers directly or fans out (section 4)
  └─ post_llm_call / on_session_end hooks → write route + outcome rows to the ledger
```
Lane → Hermes surface:

| 2.0 lane | as a face's brain (provider) | as a sub-agent |
|---|---|---|
| local-lane | `custom` → ollama | Hermes `delegate_task` child on `custom` |
| claude-lane | `claude-cli` (haiku / sonnet / opus / fable) | `agent_run runtime=claude` or delegate child |
| codex-lane | native `openai-codex` (Forest's ruling) with luna / terra / sol / spark | `agent_run runtime=codex` |
| antigravity-lane | `agy-cli` | `agent_run runtime=agy` |
| cursor-lane (Grok) | to build: `cursor-cli` provider + worker runtime, blocked on the keychain login | same |

Caveats: step 5 writes an internal gateway structure, so it needs a guard test against Hermes updates. Cron jobs keep
their pins in v1; "route at fire time" comes after the interactive path is proven. A local brain needs a slim prompt:
a cold Hermes-sized prompt (~14k tokens) takes ~50 s on the 8B here, so R0 should be served by a small "logger"
identity with its own tiny toolset rather than by a face swapping onto the local model mid-conversation.

## 4. Sub-agents: when and how many

Forest's directive (2026-09-19): simple tasks get no sub-agents; anything complex in any way gets several, because the
goal is the best result. 2.0's rule is stricter on count (max 3). Proposed policy, numbers are Forest's to set:

| band | default | sub-agents | how |
|---|---|---|---|
| R0, R1 | answer directly | 0 | 2.0 direct-answer rule unchanged |
| R2 | decompose when Q11 ≥ 2 or parallelizability ≥ 2 or context_size ≥ 2 | 2–3 | one wave if independent, else waves by `blocked_by` |
| R3 | always decompose | 3–5 plus one verifier | orchestrator on O-opus, workers rescored per subtask |
| R4 | as R3, entered only by escalation evidence | as R3 | orchestrator O-fable |

- **Count** = clamp(Q11 parts, band range), then capped by free capacity (section 5 caps).
- **Per-subtask rescore** (2.0 policy): each subtask gets its own six scores and its own lane, so a hard task still sends
  its mechanical pieces to cheap lanes.
- **Diversity mode** (Q12): for judgment-heavy subtasks run the same subtask on two different vendors and let the
  orchestrator reconcile; disagreements are surfaced to Forest instead of averaged away.
- **Verifier**: one extra sub-agent checks the merged result against the acceptance row of each subtask (2.0's judge-band
  mapping: R2→b2, R3→b3, R4→b4). A failed check escalates that subtask one tier, never the whole job.
- **Execution surface:** reasoning subtasks → Hermes `delegate_task` children with per-child provider/model; code,
  research-with-tools and long jobs → `agent_run` CLI workers (stateless, usage in the ledger). Merge rule from 2.0:
  every planned subtask reports exactly once, otherwise the answer says what is missing.
- Visible on Discord: the orchestrating face posts the plan and the per-sub-agent results in the channel thread-free,
  or on `#agent-bus` when another face is involved.

## 4b. Scarcity as a routing parameter (Forest 2026-09-19)
Every lane draws on a finite weekly or 5-hour pool, and running dry takes a whole lane down (Codex is out until
2026-09-23). Scarcity is a per-lane number in [0,1], recomputed every few minutes:

`scarcity = max(burn_pressure, refusal_pressure)`
- **burn_pressure** = share of the lane's budget used in its current window ÷ share of the window elapsed, squashed to
  [0,1]. Budget per lane is Forest's number (weekly tokens or turns). Source: the ledger (exact for every CLI lane and
  brain call), plus Codex's own usage endpoint where readable.
- **refusal_pressure** = 1.0 while a lane is in cooldown after a usage-limit or auth refusal (2.0's provider-refusal
  taxonomy), decaying to 0 at the announced reset time.
- local lane: scarcity = RAM-gate pressure instead of quota (0 when the model is resident and memory is free).

How it acts (it can reorder and defer, it can never weaken safety):
1. **Within a band, candidates are ordered by `preference − w·scarcity`**, so R2 goes to Terra when Sonnet is running hot
   and back again when it recovers. 2.0's roster law is the tie-break: free local → untouched dedicated pool →
   most-headroom pool → shared pool last.
2. **Expensive tiers get stricter as they get scarcer:** above a threshold, Opus/Sol/ceiling are reserved for D≥10 or
   blast_radius=3; everything else in R3 runs on the W-std pair with a verifier.
3. **Sub-agent count shrinks under scarcity** (section 4 counts are maxima); diversity mode switches off first.
4. **Urgency trades against scarcity:** "overnight is fine" work is deferred to the local 27B or to after a reset rather
   than spending a scarce lane now.
5. **Never:** route outward or client-facing work below W-std because a lane is scarce; drop below the resolved band;
   spend the ceiling because cheaper lanes are scarce (the ceiling still needs escalation evidence).
Scarcity, the chosen lane and the runner-up are logged with every route row, so the learning loop can see the trade.


### 4b-1. BUILT 2026-09-21: scarcity is % used versus % of the window elapsed (Forest's ruling, no dollars anywhere)
Forest's ruling, paraphrased: budget is not money but the percentage of the subscription used versus the time left until its reset
(a rolling weekly limit); and every vendor is read the same way, through its own `/usage` screen, never through an API.

**One method for every subscription** (`cortados_router/scarcity.py`): open the vendor's own CLI in a PTY inside the empty folder
`~/.cortados/usage-probe`, type its usage command, read the screen, close. Costs no tokens, about 25 s, all four in parallel.

| lane | command | what the screen gives | windows |
|---|---|---|---|
| claude | `claude` → `/usage` | "Current session N% used · Resets 12pm", "Current week (all models) N% used · Resets Sep 21 at 3pm" (+ per-model weeks when shown) | 5 h, 7 d |
| codex | `codex` → `/status` | "Weekly limit: N% left (resets 11:35 on 23 Sep)" (+ 5h limit when shown); answers "run /status again" once, so one retry | 5 h, 7 d |
| cursor | `cursor-agent` → `/usage` | "Included N% used", "Auto N%", "API N%", "Resets Oct 10" | month |
| antigravity | `agy` → `/usage` | per model group: Weekly / Five Hour "Limit Remaining N%", "Refreshes in 35h 32m". Our lane = Gemini group; a separate **Claude + GPT group** exists on the plan, untouched | 5 h, 7 d |
| local | `vm_stat` + `ollama ps` | free memory vs resident model (2.0 RAM gate ×1.5 when cold) | none |

Every window becomes the same record: `used_pct`, `elapsed_pct`, `projected_pct_at_reset = used / elapsed`, `headroom_pct`,
`allowance_pct_per_h = headroom / hours to reset`, `resets_at`, `expiring_pct` (headroom the reset will wipe; only in the last 15% of a ≥1-day window),
and `scarcity`: 0 when on pace for ≤70% of the window, rising to 0.9 at ≥124%; capped at 0.3 while under 25% used (early pace is noise);
≥0.8 once 90% is used; **1.0 only when the window is spent or the vendor refuses**. Lane scarcity = its tightest window
(Cursor: the vendor's "Included" headline; which pool Cursor Grok draws from is not stated, both pools are recorded).
Readings: `~/.cortados/scarcity.json`; series: `scarcity-history.jsonl` (feeds the 24 h change and, later, the unit-cost learner).
Refresh: claude/codex 15 min, cursor/antigravity 30 min, in a background thread; `resolve()` never waits for a probe.

**What routing does with it** (`core.resolve`, inside the band only, never below it; knobs in `tables/budgets.json` → `policy`):
1. scarcity 1.0 → lane skipped, reason and reset time logged (replaces the hand-written `known_state`);
2. scarcity ≥ `demote_above` (0.5) → behind its band-mates;
3. `expiring_pct` ≥ `prefer_expiring_headroom_pct` (10) → first: headroom about to be wiped is free;
4. chosen lane ≥ `reserve_heavy_tiers_when_scarcity_above` (0.7) → sub-agents held to the band minimum; background units flagged "defer past the reset or run local overnight";
5. `routes.scarcity` stores the per-lane values with every decision (a feature for the trained scorer); the footer shows the chosen lane's "% used / % elapsed".

**Agents see it too**: tool `subscription_usage` (toolset `cortados`; `refresh`, `format`) and `/router usage [refresh]`.
Crons `fleet-health` (2 h) and `usage-daily` (21:00) were rewritten around it: breaches are SPENT, BURNING FAST (≥25% used and on pace ≥110%),
USE IT OR LOSE IT (≥20% expiring within 24 h) and UNREADABLE; the old $60/week and $15/day thresholds and the lane "Reply ok" pings are gone.

First readings, 2026-09-21 11:15: claude week 70% used at 97.8% elapsed (≈28% expires unused at 15:00) · codex week 100%, resets 09-23 11:35 ·
cursor included 17% at 36% of the month (named-model pool 33%) · antigravity week 1%, 5-hour 2% · local free.

### 4b-2. Pools inside a subscription (Forest 2026-09-21, second ruling)
A subscription is not one meter. Each separately metered **pool** is scored on its own, and a model is as scarce as the tightest pool it
draws on (the lane's general windows + its own pool). `scarcity.pool_for(lane, model)` maps a model to its pool; a candidate may pin `pool`.

| lane | general windows | pools | who draws on them |
|---|---|---|---|
| claude | 5-hour session, week (all models) | `fable` = "Current week (Fable)" (any other per-model week the screen shows becomes a pool by name) | Fable 5.1 ← `fable`; Opus/Sonnet/Haiku ← general only |
| codex | week (+5-hour when shown) | any `<model> limit:` line on `/status` becomes a pool | today the screen shows only the general week: no separately limited model (no Spark) on this account; models: gpt-6-astra, gpt-reserve, gpt-5.6-sol/terra/luna, gpt-5.5 |
| cursor | none | `cursor-models` (screen line "Auto": Cursor Grok, Grok, Composer, Auto) · `other-models` (screen line "API": Claude, GPT, Gemini, Kimi, GLM, Muse Spark) | overflow of `cursor-models` spills into `other-models`, so a spent first pool inherits the second's score; "Included" is the blend, info only |
| antigravity | none | `gemini` · `claude-gpt` (Claude Sonnet/Opus 4.6, GPT-OSS 120B) | lane default = `gemini` |

Claude's per-model weeks load from a vendor panel that rate-limits ("r to retry"): the probe presses `r` twice at most, and when the panel
stays closed the last per-model reading is carried (marked stale) until its own reset passes. 2026-09-21 it was seeded once from Forest's
claude.ai screenshot (Fable 95%, resets 15:00).

**Model inventory** (`routing-table.json` v3 → `inventory`, full id lists in `tables/inventory/`, refreshed by `bin/refresh-inventory.sh`):
claude 4 · codex 7 · cursor 231 ids in about 50 families · antigravity 14 · local 7. Listing a model does not place it in a band.
Placed today, as same-model fallbacks on a second subscription only (no invented tiers): R2 `cursor/claude-sonnet-5-thinking-high`,
`cursor/gpt-5.6-terra-medium`; R3 `cursor/claude-opus-5-thinking-high`, `cursor/gpt-5.6-sol-high` (all pool `other-models`).
The Cursor lane now accepts Grok/auto **or an exact id Cursor lists** (`policy_cursor.allow_other_models`; supersedes the 2.0 Grok-only rule
for this system). Cursor's Fable ids are flagged NO ZDR by Cursor: refused by the runner and the table validator.
Unplaced and waiting for a benchmark or a ruling: Composer 2.5, Grok 4.7, Gemini 3.8/3.1 Pro (Cursor and Antigravity), Kimi K3, GLM 5.2,
Muse Spark 1.3, GPT-5.6 Luna, GPT-5.5, Antigravity's Claude 4.6 + GPT-OSS group, local qwen3.5:27b.

Still open: unit cost per lane and model (Δ% used / Δ ledger tokens from the history series) so the router can price a task before running it;
policy numbers are first guesses (Q-scarcity below); live mode must also re-read a lane right after any refusal.

## 4c. Dynamic tiers from public benchmarks (Forest 2026-09-21: rank the models by public benchmarks rather than by his judgment)
`tables/benchmarks/fetch_benchmarks.py` (stdlib, plain GETs, no keys) pulls 8 sources into `scores.csv` (5,332 rows, 1,139 published names):
Epoch AI benchmarking hub (GPQA, HLE, SWE-bench, Terminal-Bench, FrontierMath, Aider …), LiveBench subtasks, Aider polyglot, SWE-bench Verified,
LMArena text / style-controlled / WebDev / agent. `build_tiers.py`: names → families (effort/date suffixes stripped, best configuration counts) →
per-benchmark min-max → logit → additive ability/difficulty fit over all ~600 families, ridge-shrunk → one ability overall and per skill →
band floors = midpoints between the abilities of the 2.0 reference models (haiku / sonnet+terra / opus+sol / fable+astra) → for every band and
work kind: the best model of EVERY subscription pool first, then the rest by score (cap 6), then a 3-entry fail-up tail from the band above
(never the ceiling). Output `tables/tiers.generated.json`; `tiering.mode = "benchmarks"` makes `resolve()` use it (R0 stays local, hand-written
`candidates` are the fallback). Scarcity re-ranks inside the list exactly as before. Refresh: Sundays via `bin/refresh-tiers.sh` (previous file kept on any failure, band changes printed).
First build: R4 gpt-6-astra, claude-fable-5-1 · R3 opus-5, gpt-5.6-sol, gpt-5.5, muse-spark-1.3, gemini-3.7-flash, opus-4.8 · R2 gemini-3.8-flash, kimi-k3,
gpt-5.6-terra, opus-4.6, grok-4.6 (Cursor Grok = proxy), … sonnet-5, luna, glm-5.2 · R1 kimi-k2.7-code, gpt-5.4-mini/nano, gpt-5-mini, haiku-4.5, gpt-oss-120b.
Unranked (no public score): Composer 2.5, Grok 4.7. Known gaps: no speed/price axis (Artificial Analysis needs a free API key: Forest's call), so R1 is
"least capable" rather than "fastest"; benchmark numbers are vendors' best configurations, not the effort level we actually run.

### 4c-1. SUPERSEDES the band lists above (Forest 2026-09-21 evening): every model scored per use case, continuous requirement
Forest's ruling, paraphrased: bands do not distinguish models; every model gets its own benchmark-based score for each of our use
cases. So bands no longer choose models. They keep only what they always did: hard rules, orchestrator, sub-agent counts.
- `tables/use-cases.json` (mutable): our seven work kinds, each defined by the public benchmarks that resemble it, weight 1–3 (code → DeepSWE, SWE-bench
  Verified, Terminal-Bench, FrontierCode, WebDev arena …; ops → Terminal-Bench, APEX-Agents, agent arena, METR horizons, OSWorld …; research →
  DeepResearch Bench, SimpleQA, GPQA, HLE; writing → style-controlled arena, Lech Mazur, LiveBench story/summarize; …).
- `build_tiers.py` → `tables/model-scores.generated.json`: per family, a general index and one index per use case = weighted mean of (result + fitted
  benchmark difficulty) on that use case's benchmarks, shrunk toward the general ability where evidence is thin. 100 = the best published model on
  that use case, 0 = the average published model. 33 reachable families scored, 41 routes (family × subscription).
- `core.required_index`: requirement for use case u = linear interpolation over difficulty D through knots D=3/6/9/12 whose values are the use-case
  indices of the 2.0 reference models (haiku / sonnet+terra / opus+sol / best non-ceiling), never below the reference of the band the hard rules chose.
- Choice: all routes whose use-case index clears the requirement qualify; winner = lowest cost = GENERAL index (weight of the model = slowness and
  subscription burn) + 30 × scarcity of its pool − 10 when that pool's headroom is about to expire. Spent pools skipped; ceiling models (general
  ability ≥ fable/astra) only by escalation; nothing qualifies → strongest available. Logged with every route: required index, winner, five alternatives.
- New models need no edits: the nightly job re-reads what Cursor / Antigravity / Codex list, re-fetches benchmarks and re-scores. A model Forest enables
  in Cursor's settings shows up in `cursor-agent models`, gets scored that night and is routable the next morning. No public score → unranked → not routed
  (today: Composer 2.5, Grok 4.7). Cursor's Fable ids stay refused (NO ZDR).
- Open: no speed/price data (general index is the stand-in); use-case weights are Claude's first reading of each benchmark and should be revisited once
  our own outcome data exists (per-use-case win rates from routed turns can replace them).

## 4d. CortadOS Router S1 training pipeline (built 2026-09-21, `s1/`)
`dataset.py` (1.0 sheet + every shadow-routed turn, cleaned by `core.clean_request`, the same function the live hook now uses; split fixed by id hash:
15% holdout) → `label.py` (stateless Claude teacher, 12 requests per call, k=3 independent samples; train split Sonnet 5, holdout Opus 5; resume-safe;
stops when its Claude pool's scarcity ≥ 0.7; usage ledger label `router-teacher`) → `build_sft.py` (one example per teacher sample = soft labels in
expectation; R2/R3 requests repeated 3–4×; 10% valid) → `mlx_lm lora` on the 4-bit base → `evaluate.py` (temperature per question fitted on valid;
holdout graded against Opus: band accuracy, under-/over-routing, outward recall, per-question accuracy, ECE, Brier, latency; incumbent 14B graded on the
same rows; promote only if band accuracy ≥, under-routing ≤ and outward recall ≥ the incumbent → `s1/current.json`).
`student.py`: completion is twelve `name=digit` lines; every answer is one digit token, read as a distribution over the allowed digits only.
The scorer never sees a model name: it scores the TASK. Models, tiers and scarcity live in tables, so new models never need a retrain.
Cadence (Claude's call, Forest 2026-09-21): label every night 01:30 (`com.cortados.router-nightly`); train when there is no promoted model, on Sunday nights,
or when ≥150 newly labelled requests have accumulated. Serving the promoted model in place of the 14B engine is the next build.

## 5. Hard rules before any scoring (deterministic, cannot be out-voted)
1. Fable only by escalation evidence (G112 / INV-RES-003).
2. Outward effect (Q7 true or outward verb) → blast_radius 3 → at least R3, and the Door still requires Forest's yes.
3. `#log` channel → R0 capture path, unless Q7 or Q10 fires.
4. Client-facing text, money, legal → never below W-std.
5. Cursor lane accepts only Grok ids and `auto`.
6. Local lane only when the RAM gate passes (pick ONE factor: 1.5 or 1.25) and never for outward work.
7. Lane in cooldown (usage limit, auth failure, keychain) is skipped; fail-up never goes below the resolved band.

## 6. Logging and learning
Ledger rows per routed turn, 2.0 shape: route {features, confidences, band, tier, lane, model, engine, hard-rule hits,
plan size}, outcome {tokens, wall-clock, error class, Forest's correction or thumbs}, escalation {from, to, evidence}.
Brain inputs/outputs are already saved per call. With a few hundred labelled turns: fit thresholds and weights first
(cheap, transparent), fine-tune a small local scorer only after that. Shadow rule from G156/G159: one bounded shadow at
a time, never on the critical path, never outward, blast radius never trained downward.

## 7. Rollout
- **R-1 Shadow:** hook decides and logs only; a footer on replies shows "router would have chosen: R2 · sonnet · 3
  sub-agents". No behaviour change. Compare engine A vs B here.
- **R-2 Model routing on** for interactive turns, faces keep their voice.
- **R-3 Sub-agents on**, starting in one build channel.
- **R-4 Crons routed at fire time; learning loop; Cursor/Grok lane once logged in; local 27B for "overnight".**

## 7b. Rulings folded in 2026-09-19 (evening)
- **The table is mutable.** `tables/routing-table.json` is the live table; the router reloads it when the file changes and
  keeps the last good version if validation fails (unknown lane or provider, a ceiling model outside R4, a non-Grok id on
  the cursor lane). `tables/scale-2.0-reference.json` is the untouched 2.0 original. Changing a model is a one-line edit.
- **Sub-agents go through the same router.** Every subtask is scored and routed by the same table (`route_each_subtask`);
  band rows carry the sub-agent min/max; counts are maxima that scarcity and the concurrency cap reduce.
- **Scarcity is in subscription units, not dollars.** `tables/budgets.json`: for each plan we learn the percent of the
  plan's window one token costs, from the provider's own usage readout divided by the ledger's token counts, per lane and
  model. Local lanes are free and priced only by free VRAM.
- **No hand-labelled gold set for now.** The teacher generates labels; evaluation tasks are built from public benchmark
  task types mapped to the six features (for example single-function fixes → low steps, multi-file refactors → high steps
  and parallelizability, agentic terminal tasks → high blast radius). When Forest later corrects routes in chat, those
  corrections become the first human gold rows.
- **Cursor lane unblocked without the keychain.** The CLI's supported `AGENT_CLI_CREDENTIAL_STORE=file` switch skips the
  login-keychain check (2.0 had tried an env var the CLI ignores). Set in every Hermes profile and the worker env; Cursor is
  a worker runtime and a stateless brain with the Grok-only guard. One login remains: `bin/cursor-login.sh`.

## 7c. Shadow mode is LIVE (built 2026-09-19)
Plugin `cortados_router` (enabled in all three faces). `pre_llm_call` starts the decision in a background thread,
`transform_llm_output` appends the footer only if the decision is already there, `post_llm_call` logs the outcome. It never
delays or changes a reply. Decisions and outcomes: `~/.cortados/router.db` (table `routes`, includes the request text,
mode 0600); `/router` in chat or `python cortados_router/core.py report` lists recent ones; `… core.py decide "text"`
scores one request by hand. Set `CORTADOS_ROUTER_FOOTER=0` in a profile's `.env` to hide the footer.
Measured on this Mac: a decision costs ~8 s of compute on the resident 14B (530-token prompt, ~100-token typed JSON). The
14B is ONE shared queue: memory extraction holds it for 1–2 minutes per conversation, so decisions made during active chat
can take a minute or more, which is why the log deadline is 300 s and the footer is best-effort. Things that did NOT help,
measured: a second ollama parallel slot (requests still queued; reverted), and separate small scorer models (three resident
models thrashed memory, 30–118 s per decision; the 4B scored a multi-module refactor as all zeros). Asking ollama for a
different context size than the memory gateway uses forces a 46 s reload per switch, so the router sends no `num_ctx`.
Consequence for the plan: live routing needs the small trained scorer running in its own lightweight runtime, with a
single forward pass per decision. The interim engine is for shadow data only.
Not in v1: channel-based hard rules (the hook does not see the Discord channel yet), usage-readout scarcity (only known
cooldowns and the Cursor login state feed lane health), routing of cron turns beyond logging.

## 8. Rulings
**Decided 2026-09-19:** engine = our own locally trained decision model to Jev's standard, trained and served on the local
Mac overnight (no Jev API); local lane model = the 14B, not the 8B; R4 = Fable 5.1 with GPT-6 Astra as the GPT ceiling;
scarcity is a routing parameter.
**Still open:**
- **Q1** Adopt the table for Hermes as written (R0–R4 conditions, lane order), or change rows? It was never ratified in 2.0.
- **Q3** Sub-agent maxima per band and the cap on concurrent workers on this Mac (suggest 3).
- **Q4** Diversity mode: which work kinds.
- **Q5** RAM-gate factor: 1.5 or 1.25.
- **Q6** R1's first choice is Grok via Cursor, blocked on the keychain. Until then: Gemini Flash first, or Haiku?
- **Q8** Routing footer on replies: always, or shadow phase only.
- **Q9** R4 order: Fable 5.1 first and GPT-6 Astra second, or chosen by work kind (code → GPT, judgment → Fable)? And does
  2.0's "ceiling only by escalation evidence" still hold for both? (I assumed yes.)
- **Q10** Lane budgets for the scarcity number: weekly allowance per lane (Claude, Codex, Antigravity, Cursor) and the
  share the nightly teacher-labelling job may use.
- **Q11** Will you hand-label ~100 rows of `labeling-sheet.csv` as the gold set? Without it the promotion gate has only
  teacher labels to measure against.

## 9. Requested dual review (2026-09-30)

Forest replaced the fixed Gemini reviewer seats and the mandatory-review rule:
Samantha matches the actual author's capability on another model vendor;
Rachel uses the opposite capability level on another vendor. Both are requested
independently from the same original artifact only when a review is explicitly
requested. Ordinary author turns never dispatch the pair.

`cortados_router/reviews.py` uses the benchmark catalog's `ceiling` flag
for frontier versus smaller models. Samantha stays in the author's class, with
a configurable maximum use-case index difference (20); Rachel switches classes.
OpenAI and Anthropic are preferred reciprocal partners. Another eligible vendor
can fill in when available; a Claude model sold through Cursor is still Anthropic.
Capability constraints and context fit are hard gates; scarcity ranks eligible
routes. Unknown authors or no eligible reviewers block rather than silently
violating the policy. The interactive floor is capped at the author's own index
so a small worker can still receive a comparable review.

Actual post-API-call model observations live in `.cortados/reviews.db`; completed
worker metadata is the alternative provenance source. New face turns must have
been observed after this plugin loads; old configured defaults are not backfilled
as evidence. `router_review` exposes authors/request/status and dispatches both
roles through the existing bus transport. Durable tickets prevent duplicate
dispatch on retry and bind each incoming review to its requester/profile/message.
One-turn overrides select the reviewer before its first call; normal scoring
does not replace that choice. If a provider fallback changes the actual reviewer
model, tools and final review output are blocked and the request reports blocked.
The requests are independent; existing reviewer session history is retained, so
the brief explicitly forbids reading the peer review before submission.

Deployment is staged, not live-verified: preview `python3 bin/apply-review-policy.py`,
then in the coordinated gateway window run it with `--apply` and use
`bin/gateway-restart.sh`. The prose update is limited to review rules, reviewer
identities and roster lines, with backups; it does not change memory/model config.
Do not run the full fleet renderer merely for this migration. The shared gateway
was intentionally left running while Claude owns the Context rollout. No review
messages or production review turns were sent as tests. Confirm tool registration
and a real requested pair after coordinated activation.

Validation: `python3 -m unittest tests.test_router_reviews` (offline policy,
provenance, paired dispatch/retry, gateway override and fallback guard tests).
The existing router suite has two baseline failures: its frontier tests still
assert the old D=5 strategic / D=6 underuse triggers; current policy uses D=7/D=9.

## 10. Multiple accounts per vendor as separate resources (Forest 2026-10-13: "yes"; design, not built)

Forest wants extra subscriptions (for example more Claude Pro/Max accounts) used as separate token providers,
"resources". Today the account is never a routing dimension: a route is `{lane, provider, model, pool}`, and the
account is whatever login the CLI or Hermes finds by default.

**Resource identity.** A resource is `(vendor, account, pool)`. Scarcity, usage probes, unit-cost learning,
stickiness and route logging are keyed by it. A route becomes `{lane, account, provider, model, pool}`.

**How choice works.** Each account's pools are separate candidates, scored exactly like pools today:
`30 × scarcity`, the expiring-headroom bonus, and spent pools skipped. Accounts on the same vendor therefore spread
load by pace without a new policy. A conversation stays on its account (stickiness keyed by account) unless
that account is spent, because prompt caches and native replay are per account.

**Account registry** (`tables/accounts.json`, new, mutable, no secrets). Per account: `vendor`, `id`, `plan`,
`windows` and the credential *location*: `CLAUDE_CONFIG_DIR` or a token-file path for Claude, `CODEX_HOME` for
Codex, the credential-store path for Cursor and agy. The current single logins become each vendor's `default`
account, so existing behaviour is the one-account case.

**Changes, by owner:**
- Router (`cortados_router`):
  - `scarcity.py`: one sampler run per account, `MIN_INTERVAL` per vendor, cache and history keyed by resource.
  - `core.py`: candidates expanded per account; health, dedupe, stickiness, resolve output and `routes` gain an `account` column.
  - `unitcost.py`: pools keyed by account.
  - Model gateway (L1): one owned grant store per account, already per-account by design.
- Worker/provider path (shared, `plugins/cortados-pty`, `plugins/model-providers/cortados-cli`):
  - `_child_env` takes the route's account env.
  - `run_brain` passes it through, and `harvest_session_usage` reads the job's env, not the host's.
  - Needs Harness agreement: CLAUDE.md lists `plugins/` under Harness, while the 10-13 split gives connectors to Router.
- Native `openai-codex` in Hermes: pin the chosen Hermes `credential_pool` entry rather than leave Hermes's own rotation in charge.

**Known obstacle.** A non-default `CLAUDE_CONFIG_DIR` reports `loggedIn:false`, because the Keychain item is tied to
the default dir (`runner.py:200-204`). Second and later Claude accounts therefore authenticate with a per-account
`CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) stored in a file only the router and worker read.

**Open for Forest:**
- (a) Which accounts exist per vendor, and their plans.
- (b) Spread by pace (default above), or use one account first and the next only when it runs hot.
- (c) Whether each vendor's terms allow several personal subscriptions to be driven by one automated system.
