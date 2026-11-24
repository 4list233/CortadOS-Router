# Scarcity v2: one standardized usage, equal end-of-window use, expiry pressure, burn-up and drain work

Design only (2026-11-24). Nothing here is built; no production code changed. Branch `scarcity-v2-design`, on top of
#143 (`83d2197`, routing table v21, under review). The replay script is `scripts/scarcity_v2_replay.py`; it runs offline
on a read-only copy of the router's state.

## 0. Summary

**The owner's goal (2026-11-24, condensed):**
- use every expiring credit;
- no fixed account order;
- one standardized usage figure across all subscriptions, so every pool ends each window at about the same level;
- burn expiring pools on bigger models, never weaker than the task needs;
- a pipeline of backlog "drain" work for quota that would otherwise lapse;
- the $200 API grant spent (to the $190 stop line) before it expires on 12-02 at 19:00.

**The design in one paragraph.** Every pool gets one number: its **standardized usage**, the % of its binding window
it is forecast to have used at its reset, including the owner's own use and work that can only run there. A
**controller** runs after every usage sample (20 min). It finds the single **water level L** that the forecast routable
demand can lift every pool to, and gives each pool a **rate**: the API-price dollars per hour it must absorb to end its
window at L. It does this with every pool's size in one currency, **$-eq**: API list-price dollars, the vendor-neutral
unit the meters roughly track. A pool resetting soon has fewer hours, so its rate rises as the reset nears; that is
the expiry pressure. The API grant never refills, so it is paced to its stop line first.

S1, the band, the required index, the outward floor and every gate stay exactly as they are and decide which routes
qualify. Among them, conversations are split across pools in proportion to their rates.

Inside the chosen pool:
- **burn-up** puts real work on the strongest model of the band window while the pool is forecast to end below 85 %;
- a **drain queue** takes backlog work in the last 48 hours of a window forecast to end below 90 %.

Pins choose their account by the same rates, so account A is no longer first by configured order. A carries the
owner's own sessions, and its owner reserve keeps routed work off it.

**Headline replay numbers.** Same S1 answers and token counts, 2026-11-23 23:40 → 12-02 18:00, 16,925 conversations;
details in §8.

| | #143 (v21, today) | v2 | v2 without drain |
|---|---|---|---|
| **API grant spent by 12-02 18:00** | **$41** (≈ $30 without the log-gap fill) | **$188** of $190 | $185 |
| Claude B, full week ending 12-02 07:00 | 57 % | **89 %** | 78 % |
| Codex, full week ending 12-02 11:41 | 61 % | **89 %** | 76 % |
| Claude B, week ending 11-25 07:00 (36 % used at start) | 50 % | 84 % | 47 % |
| Codex, week ending 11-25 11:41 (18 % used at start) | 32 % | 85 % | 44 % |
| Claude A, week ending 11-30 14:00 | 97 % | 88 % (≈ 87 % is the owner's own use) | 88 % |
| Claude B Fable week ending 12-02 | 0.5 % | 41 % | 52 % |
| Pins and routed work on A ($-eq) | 274 | 29 | 29 |
| Drain work run ($-eq; hours) | 0 | B 633 (69 h), Codex 659 (77 h), API 47 (70 h) | 0 |
| Frontier models at R3 | 95 % | 84 % | 89 % |

**Without the drain queue**, v2 alone spreads real work so that B and Codex end the week within 2 points of each
other (78 % / 76 %), against 57 % / 61 % today. It also spends the grant ($185). **The drain queue** lifts both to
89 %. For scale, the drain is three to four weeks of this week's pinned child-task volume on Claude.

Every [ESCALATE] decision is collected in §10.

---

## 1. Why #143 is not enough

#143 made the API lane a scored candidate, split work among underused pools in proportion to their expected loss, and
made the frontier reachable. The replay below runs its code (`core.resolve` of `83d2197`) through the next nine days,
with usage evolving as the work lands. It shows the structure is still a sum of credits, not a target:

1. **The API grant gets a weight, not a deadline.** Its cost term is `weight × (1 − expected loss)`, which is near 0
   while the grant is forecast to lapse. A near-zero cost wins only the calls where it beats every other near-zero
   credit, so the grant takes about 5 % of S1-routed conversations (438 of 8,878) and ends at **$41** (§8). It needs a pace to the stop line
   by the expiry, not a discount.
2. **The underuse credit is per call, not per window.** `−80 × expected loss` says how much a pool would lose. It does
   not say how much work the pool must take, per hour, to stop losing it. The split shares calls by expected lost
   tokens, but those tokens are measured in each pool's own learned `tokens_per_pct`, a lower bound off by about 2.7×
   for Claude (#143 audit §4). Pools still end far apart (B 57 %, Codex 61 %, A 97 %).
3. **Pins are outside the ranking.** `choose_pinned` uses `static_scarcity_rank` and then configured order. A is
   first until its capped scarcity reaches 0.5, and `window_view` holds a fresh week at ≤ 0.3 until 25 % is used. Pins
   took 229 $-eq of A's quota in the replay. A is the account the owner works in.
4. **Expiry is two unrelated switches.** `expiring_soon_points` (only past 85 % of a window) and the API's
   `prefer_within_days: 3` (local-bound work only). Neither grows smoothly as the reset nears.

---

## 2. A standardized usage metric per pool

### 2.1 Pools and the one currency

A **pool** is one quota that resets as a unit:
- `claude` (account A) and `claude#b` (account B), each a "week (all models)";
- each account's **Fable week**, a sub-pool;
- `codex` (the week);
- `anthropic-api` (the grant, up to its stop line).

Cursor and Antigravity have no provider backend (#143 audit §5) and stay out until they do.

Vendors meter in their own units: Claude's % counts Opus heavier than Sonnet, and Codex's % has its own weights. v2
keeps each vendor's % as the measurement. To compare and move work between pools, it expresses each pool's size in
one currency:

> **$-eq** of a call = weighted tokens × the model's blended $/1M ÷ 2, with weighted tokens = input + 5 × output +
> 0.1 × cache read + 1.25 × cache write. For Claude models that is exactly Anthropic's API list price. For Codex
> models it is the same blended-price convention the router already uses for burn.
>
> **K_p** = the pool's size in $-eq per 1 % of its window.

It is the same idea as today's burn weight (`price ÷ pool size`), made into a unit: 1 % of a pool is K_p dollars of
API-priced work, whichever vendor it is. The API grant needs no estimate: K_api = stop line / 100 = **$1.90 per %**.

**Calibration from the #143 audit's copy** (clean intervals with no reset, no route-log gap and readings at both ends):

| Pool | Interval | Δ used | $-eq in the ledger | K (per 1 %) | Week in $-eq |
|---|---|---|---|---|---|
| Claude B | 11-22 15:10 → 11-23 23:35 | 14 → 36 % | 260 | 11.8 | ≈ 1,180 |
| Claude A | 11-23 14:00 → 23:35 (incl. owner sessions, from the orchestrator scan) | 0 → 25 % | ≈ 330 | ≈ 13 | ≈ 1,300 |
| Codex | 11-22 15:10 → 11-23 23:35 | 2 → 18 % | 175 | 10.9 | ≈ 1,090 (lower bound: direct Codex use is not in the ledger) |
| Codex | 11-18 12:30 → 11-20 17:00 | 0 → 2 % | 18 | 9.2 | (short) |
| API grant | by definition | | | 1.90 | 190 |

The replay uses **K = 12.5 for each Claude account and 11 for Codex** (sensitivity: 16 for Codex, §8.4). In $-eq, the
two Claude accounts and Codex are about the same size. In `tokens_per_pct` they looked 1.5–3× apart, because
"all tokens" counts cache reads, which dominate Claude Code sessions but cost almost nothing.

### 2.2 Definition

For pool p at time t:

- `u_p` = its **used %** now, from the latest reading: the vendor's own %, read from its usage screen. For the grant,
  `100 × spent / stop line`. A model that draws on two pools (Fable: its Fable week and the all-models week) is
  charged to both, and is as available as the tighter one, as today.
- `H_p` = **hours until the pool stops being usable** for planning: its reset (the grant: its last counting hour),
  minus the hour `floor.STOP_HOURS_BEFORE_RESET` already forbids.
- `o_p` = the **owner's own use** forecast until the reset (%): work on that login that the provider never saw. It is
  measured as the % the pool moved beyond what the provider's ledger explains (Δu − ledger $-eq / K_p). The live
  view's `work_split` already has the owner's orchestrator tokens per window, which cross-checks it. Rate: an EWMA over
  the window so far, by hour of week once a few weeks are recorded.
- `g_p` = **forced demand** forecast until the reset (%): work that may run only on this pool. Today that is S1's
  degraded traffic (hard rule: never Claude, so it is Codex's) and Codex pins.
- **Baseline forecast** `f_p = u_p + o_p + g_p`: where the pool ends if the router sends it nothing more.
- **Standardized usage at reset**: `F_p = f_p + (routed $-eq it will absorb) / K_p`. This is the one number the ranking
  equalizes, and the dashboard shows it per pool.

### 2.3 Windows of different lengths and phases

- **Long windows** (Claude weeks with different reset days, the Codex week, the grant's ~34 days, Cursor's month once
  served) all enter through `H_p`. A window that resets in 6 hours and one that resets in 6 days are compared by the
  rate each needs: `K_p × (L − f_p) / H_p` dollars per hour. There is no conversion between window lengths.
- **5-hour session windows** renew too fast to lose anything (as `scarcity._roll` already says). They are **gates,
  not targets**: a pool whose 5-hour window is ≥ 95 % used takes no new conversation until it rolls. Its rate is held,
  not given away, and the split skips it for that sample.
- **The Fable week** is a sub-pool with its own `u`, `H` and `f`. It never takes a share of conversations on its own:
  Fable is a model choice inside the account (§4). Its forecast decides whether Fable goes first in burn-up.

### 2.4 Confidence

- `u_p` is measured by the vendor every ~20 minutes and needs no model. The controller is **closed-loop**: each sample
  replaces the forecast. A wrong K_p, owner rate or demand forecast changes how fast a pool converges, not where it
  ends.
- **K_p** is learned the way `unitcost.py` learns `tokens_per_pct`, but in $-eq and with the owner's tokens added
  (work_split heartbeats), so it is no longer a pure lower bound. It carries the same confidence (intervals, %
  covered). While confidence is "weak" or "none", K_p is the table's prior and the pool's target is capped 10 points
  lower.
- **A stale, failed or other-login reading** (the rules of `reading_freshness` and `_reading_for_other_login`) takes
  the pool out of the controller for that sample: rate 0, no burn-up, no drain. It never gets a guessed rate.
  Conversations already on it continue as today.

---

## 3. The ranking: one "use this pool now" figure

### 3.1 The water level

Let **R** be the forecast **flexible demand** in $-eq per hour, priced at the cheapest qualifying model per call:
S1-routed work, plus pins on Claude, which may go to either account. It is the trailing 24 hours of what the provider
routed, blended with the same 24 hours one week earlier once the history holds it. Let `S` be the spend-down pools
(the API grant; `budgets.json` already marks it `"spend_down": true`) and `P` the other pools.

1. **Spend-down first.** For each s in S: `r_s = K_s × (cap_s − f_s) / H_s`, the pace that reaches its stop line exactly
   at its expiry.
2. **Water-fill the rest.** Find L in [0, 100] such that

   `Σ_{p ∈ P} K_p × (min(L, cap_p) − f_p)⁺ / H_p = max(0, R − Σ_s r_s)`

   (monotone in L; bisection). Then each pool's rate is `r_p = K_p × (min(L, cap_p) − f_p)⁺ / H_p`, and its forecast
   end is `F_p = max(f_p, min(L, cap_p))`.
3. `cap_p` is the owner reserve of §7: `95 − 0.5 × o_p` for a subscription, 100 for the grant (its 100 % already is
   the 95 % stop line).

This is the "equal standardized usage" rule. Every pool that receives work is forecast to end at the same L, and a
pool already above L (its own baseline, e.g. A with the owner's week) receives nothing. A pool with more room in
dollars (larger K_p) or more hours takes proportionally more work. If demand can fill everything, L reaches the caps.
Otherwise L is as high as demand allows, and the shortfall below the burn and drain lines is what §4 and §6 fill.

### 3.2 Expiry pressure

Expiry pressure is the `1 / H_p` in the rate. The same gap (L − f_p) needs 6× the hourly rate with 24 hours left as
with 6 days left, so a pool's share of new conversations rises smoothly as its reset nears. It falls back once the
pool catches up. Nothing switches on at 85 % of a window, and no separate "expiring soon" bonus is needed. A pool that
has fallen behind gets a growing share each sample because its `f_p` stays low while its `H_p` shrinks.

For the grant the effect is direct: $190 over 209 hours is $0.91 per hour on 11-23; the same $190 with 3 days left is
$2.6 per hour. The replay's spend path is $30 by 11-27, $89 by 11-29, $148 by 11-30, $176 by 12-02 01:00 and **$188** by the
last counting hour (§8).

### 3.3 From rates to a route

Per conversation (new conversations only; continuations stay on their route as today):

1. `core.resolve` runs unchanged up to qualification: S1's answers, the outward floor, band, D, required index, input
   fit, health, the interactive floor, `ceiling_work`, native-first, `api_lane_refusal` and the host's `hold_ok`
   (client floor). It yields the **qualifying routes**, grouped by pool.
2. For each pool p with a qualifying route, its **base model** `m_p` is the cheapest qualifying model there (today's
   selection inside a pool).
3. The **weight** of p for this call is `w_p = r_p / price(m_p)`: its rate in conversations of this kind per hour.
   Pools without a qualifying route drop out, and the rest are renormalised. A pool whose rates are all 0 (L below
   everyone's baseline) falls back to the pool with the most room under its cap, `cap_p − F_p`.
4. The pool is picked by `split_pick(conversation id, weights)`, the deterministic hash of #143. The same conversation
   id gets the same pool while the weights hold, and a small change of weights moves only conversations near a
   boundary.
5. The model inside the pool is `m_p`, unless burn-up applies (§4).

**How today's terms map in.** All of them are replaced by this one controller:

| Today (v21) | v2 |
|---|---|
| capped scarcity (`scarcity_points × max(s, 0)`) | `f_p` vs `cap_p`: a pool forecast over its cap has rate 0 |
| expected loss credit (`expiring_credit_points × loss`) | `(L − f_p)`: the gap to the common level |
| underuse split by expected lost tokens | split by `r_p`, in one currency |
| unit cost / burn weight (`price ÷ pool size`) | `w_p = r_p / price(m_p)`; K_p converts % ↔ $-eq |
| capacity credit (most tokens left) | `K_p` and `cap_p` in the water level |
| `expiring_soon_points` past 85 % | `1 / H_p` |
| API `weight × (1 − loss)` and `prefer_within_days` | spend-down pacing `r_s` |
| `ceiling_underuse` | burn-up within the band window (§4) |
| switching margin | the conversation hash (deterministic per conversation) |

Unchanged: `scarcity ≥ 1.0` (spent or refusing) skips a pool, and `ceiling_work` keeps hard planning, research and
code on a frontier model.

### 3.4 Worked example: the audit's reading, 2026-11-23 23:40

Inputs (rounded):
- A: 25 % used, 157 h left, owner ≈ 0.39 %/h. So o_A = 61 %, f_A = 86 %, cap_A = 95 − 30.7 = 64 %.
- B: 36 % used, 30 h left, f_B = 36 %.
- Codex: 18 % used, 35 h left, plus degraded demand ≈ $3/h. So f_C = 18 + 3 × 35 / 11 = 27.5 %.
- API: $0 spent, 209 h left, cap 100.
- Flexible demand R ≈ $9/h.

1. **API pace:** r_API = 1.90 × 100 / 209 = **$0.91/h**. That leaves $8.09/h for A, B and Codex.
2. **A:** f_A 86 % is above cap_A 64 %, so A gets no routed work.
3. **Water level:** solve 12.5 × (L − 36) / 30 + 11 × (L − 27.5) / 35 = 8.09, so **L = 43.5 %**.
   r_B = 12.5 × 7.5 / 30 = **$3.08/h**; r_C = 11 × 16 / 35 = **$5.01/h**.
4. **An R2 code call** whose base model is $4 on each pool (Sonnet 5 on B, Sol 6.1 on Codex, Sonnet 5 on the API) is
   split **B 34 % · Codex 56 % · API 10 %**. #143 routed this one 100 % to whichever pool had the larger credit.
5. **Burn-up:** L = 43.5 % is far below the 85 % burn line, so B and Codex run burn-up (§4).
6. **Drain:** both end inside 48 hours below 90 %, so the drain queue opens for both at its cap. In the replay, B ends
   that week at 84 % and Codex at 85 %, against 50 % and 32 % under #143.

---

## 4. Model choice inside a pool under burn

**Burn-up is on for pool p** when its forecast end, at what it is actually absorbing now (real models, last 24 h,
plus `o_p` and `g_p`), stays below the **burn line** (85 %; for a spend-down pool, its stop line). It switches itself
off: upgraded calls raise the measured rate, the forecast crosses the line, and the next sample turns it off. The
replay showed no oscillation at the 20-minute sample.

When burn-up is on, the model for a call routed to p is, among the qualifying routes on p:

1. **Bounded by the requirement:** every candidate already clears the task's required index. Nothing weaker than the
   task needs is ever chosen. The base model (the cheapest qualifier) is the floor.
2. **Bounded by the band window:** the model's tier is at most one band above the call's band, or the base model's own
   tier if that is higher. A model's tier is the highest band whose reference index it clears for the use case
   (`model-scores.reference_index`). So R0/R1 may go up to R2-class models (Sonnet 5, Sol 6.1), R2 up to R3-class
   models (Opus 5, Sol 5.6), and R3 to any frontier model. No trivial turn ever runs on Astra.
3. **Separately metered quota first:** a model on a sub-pool forecast to lapse (the account's Fable week) goes first.
4. **Then the strongest:** the highest general index, then the higher price.

Unchanged around it:
- the **outward floor**: band and blast radius are computed before any of this;
- the **interactive floor** (general ≥ 50);
- the client's **quota floor** (`x-cortados-min-left-pct`, `hold_ok`): a burn-up model the floor would refuse is not
  picked.

In the replay, burn-up moves R2 work from Luna (1,475 → 621 calls) to R3-class models (0 → 1,624). It puts 719 calls
on Fable on B, whose Fable week otherwise ends at 0.5 % used, and keeps Astra on Codex at R3 (703 → 759). Frontier
share at R3 goes from 95 % to 84 %: the routes that lose it are R3 code calls at D 8, where Sol 6.1 qualifies while
Codex is not in burn mode. `ceiling_work` still forces a frontier model for planning/research at D ≥ 7 and code at
D ≥ 9 [ESCALATE 7].

---

## 5. Pins

`choose_pinned` drops `static_scarcity_rank` and the configured order. A Claude pin (`sub/claude-*`) has a fixed model,
so only the account is chosen:

1. **The usual gates first:** fit, the client's quota floor, spent/refusing, reserve accounts while a non-reserve
   sibling has the model, and a stale or other-login reading.
2. **The controller's weights over the accounts that pass:** `w_a = r_a / price(model)`, picked by `split_pick` on the
   conversation id. For a Fable pin, the account's Fable sub-pool must also be available.
3. **All weights 0:** the account with the most room under its cap (`cap_a − F_a`). A tie is broken by the conversation
   hash, not by configured order.

So a pin lands where routed work would, and A, which carries the owner's sessions, gets pins only when its forecast
leaves room under its reserve. In the replay, pins and routed work on A fall from 274 to 29 $-eq, and A ends its week
at 88.5 % instead of 96.7 %. At the owner's measured pace, 87 % of A's week is his own use. Codex pins stay on the one
Codex login.

`accounts.json` `"reserve": true` keeps its meaning as an explicit override. The default ordering no longer needs it.

---

## 6. Drain work

### 6.1 What exists

- **S1 labelling** (`s1/label.py`): already goes through the provider with `sub/` pins, resumable, stops below
  `--min-left-pct`, exit 3 with a reason. It used 1,625 calls and 28 M weighted tokens in the audit week.
- **The S1 outcome loop** (`s1/outcome/`, `quota.py`): spends a pool until its projected use reaches 95 %, never in
  the last hour, never on a stale reading. It is already use-it-or-lose-it work with its own quota door.
- **`s1/overnight.py --continuous`** runs `nightly.sh` cycles under a latency guard. Benchmark/tier builds
  (`tables/benchmarks/build_tiers.py`) are local compute, not quota; model-provider comparisons (`s1/evaluate.py`)
  do spend quota.
- **Child tasks** (`child_tasks.py`, `start_task`) can run any backlog item on any host with a pinned or routed model,
  in a worktree, unattended.

What is missing is a single place that says how much each pool may drain right now, and a runner that pulls backlog
work only then.

### 6.2 The drain budget (provider side)

After each sample the controller also computes, per pool, the **drain rate** `d_p` in $-eq per hour:

- `d_p > 0` only when all hold:
  - the pool is inside its **drain horizon** (48 h before the reset; 96 h for a spend-down pool);
  - its forecast end **with burn-up** is below the **drain line** (90 %; 99 % of the stop line for the grant);
  - the pool is not **owner-active** (§7);
  - the reading is fresh;
  - more than an hour is left.
- `d_p = min(drain cap, K_p × (drain line − forecast end) / H_p)`; the replay's cap is $15/h per pool.
- The budget is a **token bucket per pool** in $-eq, in the same flock'd ledger style as `floor.reserve` and
  `api_credits`. A drain call reserves its upper-bound $-eq before it runs and settles after. An empty bucket refuses
  with 429 and `retry-after`.

It is published as `GET /v1/cortados/drain` (owner token; `live:read` for the dashboard):
- per pool: `d_p`, bucket level, forecast end with and without drain, H_p, and the stop reason when 0;
- per job kind: running, queued, done.

### 6.3 The queue and the runner

- **Backlog:** `$CORTADOS_HOME/drain/queue.jsonl`, append-only, one job per line:
  `{id, kind, repo, prompt or command, pools allowed, model family, max $-eq, outward: false|true, added_by}`.
- **Job kinds:**
  - `s1-label`: Claude teachers only, so Claude accounts and the API;
  - `s1-outcome`: Claude and Codex, through its own quota door;
  - `provider-compare`: `s1/evaluate.py` on a fixed set;
  - `backlog-build`: a child task on a GitHub issue the owner labelled for drain work;
  - `experiment`.
- **The runner:** `cortados_router/drain.py`, one launchd job on the always-on host, every 5 minutes. It reads
  `GET /drain`. For each pool with bucket > 0, it takes the oldest job whose kind may run there and starts it:
  - `s1-label` with `--drain <pool>`;
  - a child task with the pool's burn-up model pinned.

  Drain traffic carries `x-cortados-workload: drain` and `x-cortados-drain-pool: <pool>`. The provider charges the
  pool's bucket, and **refuses a drain call on any other pool**: drain work never decides where it runs.
- **What is eligible:** only resumable or idempotent work (a refused or stopped drain call must lose nothing).
  - **On the API grant:** no outward effect, blast radius ≤ 1, and no tools beyond the client tools #114 allows. That
    makes S1 labelling, provider comparisons and analysis prompts eligible, and no child tasks that push.
  - **Outward work** (pushing a branch) is never drain-eligible: drain changes cost, not authority. [ESCALATE 4]

### 6.4 Stop rules

A pool's drain stops at once when any of these holds; each is a recorded stop reason:

1. its forecast end with drain reaches the drain line, or its bucket is empty;
2. less than one hour to its reset (the floor's rule);
3. the reading is older than an hour, failed, or from another login (fail closed);
4. the owner's own use on that login was seen in the last 2 hours;
5. real demand rose: `d_p` is recomputed every sample from the forecast that includes real work, so rising real
   traffic shrinks the bucket's refill. A drain call is also refused when the pool's real-work rate in the last 20
   minutes is above twice its forecast;
6. the pool moved more than 5 points above its forecast in a sample (meter surprise);
7. the kill switch `drain.enabled: false` in the table, or the file `$CORTADOS_HOME/drain/stop`;
8. **for the grant: every #114 rule, unchanged.** That covers the 95 % stop line, upper-bound reservation, the expiry
   margin and fail-closed.

### 6.5 Interaction with real traffic

Real work is never queued behind drain:
- drain only gets what the forecast says real work will **not** use, recomputed every 20 minutes;
- each drain call is small (one labelling batch, one child-task turn), so stopping costs at most one turn;
- the runner limits concurrency to 2 drain children per pool;
- the S1 latency guard still pauses `overnight.py` work when the scorer slows.

The routes table records drain rows with `workload = 'drain'` (the existing column), so the dashboard and S1's
datasets can separate them.

### 6.6 Observability

- **Dashboard "Drain" panel:** per pool, the forecast end at real work only, with burn-up, and with drain against the
  drain line; drain $-eq per hour (budget vs used); jobs running, queued and done by kind; the last stop reason.
- **Live view:** a `drain` section with the same metadata.
- **Work split:** a fourth source next to orchestrator, routed and pinned.

### 6.7 What plugs in first

1. **S1 labelling on B and the API** (Claude teachers):
   - already provider-routed and resumable;
   - needs only `--drain <pool>` (send the two headers, stop on a drain refusal) and enough unlabelled rows;
   - at this week's rate it is ~$69 $-eq a week, a small share of the ~$1,300 the replay drains.
2. **The S1 outcome loop on Codex and B:** already gated at projected 95 %; switch its gate to the drain bucket.
3. **Provider comparisons** (`s1/evaluate.py` on a fixed request set across models): fits the API well.
4. **Backlog builds as child tasks:** the volume needed to drain ~$1,300 per week. Owner-labelled issues only.
   [ESCALATE 4]

The benchmark tier build is local compute and needs no drain budget.

---

## 7. Safety

- **Hard rules unchanged:**
  - the outward floor (blast radius 3, R3) is applied by `resolve` before any of this;
  - a degraded scorer never defaults to Claude: degraded traffic is forced Codex demand (`g_codex`), and the API lane
    refuses it (`api_lane_refusal`), as today.
- **The owner's reserve:** a subscription's cap for routed work is `95 − 0.5 × o_p`. The pool must keep half of the
  owner's forecast remaining use as slack on top of the forecast itself, plus the 5 % floor. With A's measured owner
  pace (≈ 61 % of the week still to come at its start), routed work cannot use A at all until late in its week. The
  floor's existing 95 % stop and `reserve_for_forest_interactive_pct` (30, budgets.json) stay as hard limits.
- **Owner-active pools:** a login with owner use seen in the last 2 hours gets no drain and no burn-up. Its rate is
  computed at `cap − 10`.
- **The client floor** (`x-cortados-min-left-pct`) and every continuation recheck are unchanged; v2 never picks a route
  the floor would refuse.
- **API money (#114) unchanged:**
  - promotional credit only;
  - 95 % stop;
  - upper-bound reservations under a file lock;
  - a grant stops counting an hour before expiry;
  - fail closed;
  - no sensitive, local-only, outward, blast radius > 1 or degraded work, opening or continuing.

  v2 changes only how much eligible traffic is offered to it.
- **No silent fallbacks:** a pool without a fresh reading has rate 0. A missing K_p uses the table prior, with a lower
  cap and a log line. A controller that cannot run (exception) leaves `resolve`'s v21 ranking in place for that sample
  and says so in the route's hard rules and the live view.

---

## 8. Replay

### 8.1 Method

`scripts/scarcity_v2_replay.py` (stdlib; never touches a live `~/.cortados`, never opens a vendor CLI):

- **Data:** the #143 audit's read-only copy of the always-on host's state, taken 2026-11-23 23:37:
  - `router.db` (28,004 rows);
  - `scarcity.json` and `unit-cost.json`;
  - the tail of `scarcity-history.jsonl`.

  Nothing was re-read from the host.
- **Time:** 11-23 23:40 (the readings' fetch time) → 12-02 18:00 (the grant's last counting hour), on a simulated clock
  (`time.time` patched).
- **Demand:** every routes row of the audit week (11-16 23:36 → 11-23 23:36) replayed one week later, and the first
  1.8 days again two weeks later. Each keeps its weekday and hour, S1 answers, band and tokens.
  - The log has **no rows at all from 11-20 17:08 to 11-22 14:57**. That hole is filled with the same hours two days
    earlier, as distinct conversations.
  - Without the fill, #143's grant spend is **$30**, matching its own estimate of $25–30.
  - 16,925 conversations in total: 8,878 S1-routed, 4,702 degraded, 2,973 pins, 372 local.
- **Usage:** each call charges its pool `$-eq / K_p` (Fable also its Fable week at twice that, the table's ratio).
  - The owner's own sessions add **0.39 % per hour** to account A. That is the audit's previous A week, 67 % owner by
    weighted tokens ≈ 62 % of the week.
  - Windows reset 7 days after their last reset. A reading is "sampled" every 20 minutes and the router only sees the
    latest sample (burn rates from the simulated history, as `recent_burn_rates`).
- **Policies:**
  - **#143**: the checked-out `core.resolve`, exactly as the provider calls it (execution, accounts aware, served
    routes + the API lane's pairs while the grant has room, conversation id as split key). Pins use
    `static_scarcity_rank`, then configured order. Degraded calls go to Codex Sol 6.1.
  - **v2**: `core.resolve` for band, D and the required index (all hard rules), then §3–§6 as written: K = 12.5 per
    Claude account, 11 for Codex; burn line 85 %, drain line 90 %, 48 h horizon (grant 99 %, 96 h), $15/h drain cap
    per pool.
  - **v2 no drain** and **v2 no burn** separate the effects.
- **Not modelled** (all affect both policies alike, or are noted):
  - token counts are fixed per conversation whatever model runs it;
  - local-pro rows stay local;
  - 5-hour windows are not simulated;
  - the owner's pace is constant;
  - Codex has no owner use;
  - S1 is never degraded beyond the logged rows.

Reproduce (any directory holding the four copied files):
```
python3 -B scripts/scarcity_v2_replay.py --repo . --data <copy> --policy before --out before.json
python3 -B scripts/scarcity_v2_replay.py --repo . --data <copy> --policy v2 --out v2.json   # also v2-nodrain, v2-noburn
```
Each run takes ~45 s.

### 8.2 Standardized usage at each window end, and the API by 12-02

| Pool | Window ends | Used at start | #143 | v2 | v2 no drain | v2 no burn |
|---|---|---|---|---|---|---|
| Claude B | 11-25 07:00 | 36 % | 49.5 | **84.1** | 47.1 | 43.6 |
| Codex | 11-25 11:41 | 18 % | 31.8 | **85.2** | 43.7 | 28.9 |
| Claude A | 11-30 14:00 | 25 % | 96.7 | **88.5** | 88.5 | 86.9 |
| Claude B | 12-02 07:00 | 0 (full week) | 57.3 | **89.3** | 78.2 | 55.3 |
| Codex | 12-02 11:41 | 0 (full week) | 60.9 | **89.4** | 76.3 | 44.3 |
| Claude B, Fable week | 11-25 06:59 | 4 % | 4.0 | 10.8 | 10.6 | 4.0 |
| Claude B, Fable week | 12-02 06:59 | 0 | 0.5 | **41.4** | 52.5 | 1.4 |
| Claude A, Fable week | 11-30 13:59 | 6 % | 7.7 | 6.0 | 6.0 | 6.0 |
| **API grant spent by 12-02 18:00** | | $0 | **$41** | **$188** | $185 | $156 |

A's week is the owner's: at his pace it ends at ≈ 87 % with nothing routed to it, which is v2's result.

### 8.3 Where the work went ($-eq, the whole replay)

| | #143 | v2 | v2 no drain | v2 no burn |
|---|---|---|---|---|
| routed on Claude A | 45 | 9 | 9 | 0 |
| routed on Claude B | 570 | 574 | 582 | 232 |
| routed on Codex | 673 | 932 | 966 | 448 |
| routed on the API grant | 41 | 141 | 185 | 156 |
| pins on Claude A | 229 | 20 | 20 | 0 |
| pins on Claude B | 340 | 550 | 549 | 570 |
| pins and degraded on Codex | 162 | 162 | 162 | 162 |
| drain on Claude B (hours open) | — | 633 (69 h) | — | — |
| drain on Codex (hours open) | — | 659 (77 h) | — | — |
| drain on the API grant (hours open) | — | 47 (70 h) | — | — |

The drain is **≈ $1,340 $-eq over 8.8 days**. That is three to four weeks of this week's pinned child-task work on
Claude ($374 $-eq), or ~20 weeks of S1 labelling at this week's rate ($69). The backlog has to be that large for the drain line to be
reached; with less, drain stops at the queue's end and pools end between the "no drain" and "v2" columns.

**Band and model mix** (the 8,878 S1-routed conversations):

| Band | Model class | #143 | v2 | v2 no drain |
|---|---|---|---|---|
| R0–R1 | small (Luna 5.6) | 345 | 579 | 580 |
| R0–R1 | R2-class (Sonnet 5, Sol 6.1) | 451 | 223 | 222 |
| R0–R1 | frontier | 6 | 0 | 0 |
| R2 | small (Luna 5.6) | 1,475 | 621 | 334 |
| R2 | R2-class (Sonnet 5, Sol 6.1, Terra) | 2,720 | 2,155 | 2,322 |
| R2 | R3-class (Opus 5, Sol 5.6, GPT-5.5) | 0 | 1,624 | 1,699 |
| R2 | frontier (Opus 5.5, Fable, Astra) | 1,870 | 1,665 | 1,710 |
| R3 | R2-class (Sol 6.1) | 104 | 324 | 230 |
| R3 | frontier (Opus 5.5, Fable, Astra) | 1,907 | 1,687 | 1,781 |

| Band | Pool | #143 | v2 | v2 no drain |
|---|---|---|---|---|
| R0–R1 | A / B / Codex / API | 21 / 374 / 345 / 62 | 2 / 134 / 579 / 87 | 3 / 87 / 580 / 132 |
| R2 | A / B / Codex / API | 161 / 3,081 / 2,447 / 376 | 52 / 1,437 / 3,552 / 1,024 | 49 / 1,256 / 3,503 / 1,257 |
| R3 | A / B / Codex | 110 / 1,094 / 807 | 39 / 889 / 1,083 | 38 / 885 / 1,088 |

The frontier models specifically:

| Model | #143 | v2 |
|---|---|---|
| Fable 5.1 on A | 95 | 0 |
| Fable 5.1 on B | 0 | 719 (R2 350, R3 369) |
| Opus 5.5 on B at R3 | 1,094 | 520 |
| Astra on Codex (R2 + R3) | 1,177 | 1,054 |

**Pins:** Sonnet 5 pins moved from A (1,432) to B; all 246 Opus 5 pins (S1's holdout teacher) moved to B.

The bands themselves are identical in every column: band, D and the required index come from the same `resolve` with
the same hard rules.

### 8.4 Sensitivity

| | #143, Codex K 16 | v2, Codex K 16 | #143, owner 0.5 %/h | v2, owner 0.5 %/h |
|---|---|---|---|---|
| Claude B week ending 12-02 | 52.9 | 89.1 | 62.4 | 89.3 |
| Codex week ending 12-02 | 48.2 | 88.1 | 63.3 | 89.3 |
| Claude A week ending 11-30 | 96.8 | 86.9 | **100** | **100** |
| API by 12-02 | $33 | $188 | $77 | $188 |

- A **larger Codex** (K 16, if the owner's direct Codex use is a third of it) changes nothing at the targets. The loop
  reads the vendor's %.
- A **heavier owner** fills A by himself under both policies. v2 then sends A nothing, while #143's pins keep landing
  on A until its scarcity crosses 0.5.

### 8.5 Caveats

- One week of demand, replayed. The 46-hour gap is filled from two days earlier.
- K comes from two clean intervals per pool. The owner's model mix on A is assumed Opus-priced.
- The demand forecast in the replay is a trailing 24 hours, so nights pull L down (the trace shows L near 0 for a few
  night hours when the grant's pace alone takes all demand). Production should blend in the same hours one week
  earlier (§3.1).
- Token counts do not change with the model. In reality a stronger model often uses fewer turns, so burn-up spends
  somewhat less than the replay says.

---

## 9. Build plan

Each step is a separate branch and review (Codex adversarial review for 3, 5, 6 and 7: routing, money, concurrency).
Each runs the full suite and the verify-router evidence block.

1. **The metric, shadow only.** New `cortados_router/standard_usage.py` (pure, stdlib): pools, `u`, `H`, `o`, `g`, `f`;
   K_p learned in $-eq with confidence (extends `unitcost.learn` with the work_split owner tokens; the old
   `tokens_per_pct` stays). Live view and dashboard show per pool `u`, forecast end, K and confidence.
   - *Accept:* unit tests on fixed readings (the §3.4 numbers); `python3 -B cortados_router/core.py standard` on a
     copied home prints the table; nothing in routing changes (suite green, routes identical on a replay).
2. **The controller, shadow only.** `allocate(readings, demand) → {L, r_p, cap_p, F_p, burn_p, d_p}`, pure, with the
   spend-down step. Logged per sample to the live view; each routed conversation logs the pool v2 would have picked.
   - *Accept:* tests for the water level, spend-down, caps and the all-zero fallback; a 24-hour shadow on the host
     shows the predicted shares; the replay script imports this module instead of its own copy and reproduces §8
     within ±2 points.
3. **Routing by the controller** behind `selection.scarcity_v2.enabled` (table v22). `resolve` qualifies as today, then
   splits by `w_p`; it replaces the credit terms, `underuse_split` and `ceiling_underuse` while on. The flag off is
   exactly v21.
   - *Accept:* the suite plus new tests: hard rules (outward → R3, degraded → never Claude; the existing verify-router
     checks), the client floor respected, the same conversation id → same pool, and v21 when off. The replay shows
     "v2 no drain" numbers within ±3 points.
4. **API spend-down pacing.** This is the time-critical piece. The grant is paced to its stop line (§3.1 step 1);
   `prefer_api_credit`'s 3-day rule is retired. It could ship before 3 as a narrow change: an API weight computed from
   `r_api` inside v21's cost. [ESCALATE 5]
   - *Accept:* verify-router's API checks unchanged (sensitive, outward, degraded refused; stop line); the replay
     reaches ≥ $180 by 12-02; the #114 tests unchanged.
5. **Pins by the controller.** `choose_pinned` uses the rates (§5); `static_scarcity_rank` stays only for the unscored
   fallback path.
   - *Accept:* tests: no configured-order preference; a reserve account still spared; the floor still applies; a Fable
     pin needs the Fable sub-pool. Replay: pins on A < 10 % of today.
6. **Burn-up.** The band window, the tier helper and the Fable-first rule; closed loop by the forecast.
   - *Accept:* tests: never below the required index; never beyond band + 1 (or the base tier); off when the forecast
     reaches the line; the outward floor and interactive floor unchanged. Replay: R3 frontier share ≥ 80 %.
7. **Drain budget and runner.** The bucket ledger, `GET /v1/cortados/drain`, the `workload: drain` admission, the
   runner (`cortados_router/drain.py`, launchd), `label.py --drain`, the queue file.
   - *Accept:* tests with fakes (no vendor CLI, no real launchctl): each stop rule of §6.4; a drain call on another pool
     refused; an empty bucket → 429; the grant's #114 rules unchanged. On the host, one labelling drain run on B
     stopping at its line.
8. **Dashboard and live view.** The Drain panel, the per-pool standardized usage and forecast end, and the work split's
   drain source.
   - *Accept:* the dashboard renders from a fixture snapshot; metadata only (no request text).

---

## 10. [ESCALATE] decisions

1. **Replace the credit-sum ranking with the controller.** This changes every routed decision: v21's scarcity points,
   expected-loss credit, capacity credit, underuse split, expiring-soon bonus and `ceiling_underuse` give way to the
   water level and rates. It sits behind a table flag, with the flag off = v21.
2. **Pins follow the ranking; account A is protected by the owner reserve** (`cap = 95 − 0.5 × owner forecast`), not by
   configured order or a `reserve` flag. To decide:
   - the factor 0.5;
   - the 2-hour "owner-active" window;
   - whether A should also stay `reserve: true` as a belt-and-braces override.
3. **Burn-up:**
   - burn line 85 %;
   - band window = one band up (or the base model's tier);
   - Fable first when its week would lapse;
   - "strongest, then priciest" inside the window.

   On the API this puts Opus 5.5 / Opus 5 / Fable ($10/$50) on eligible R2 work while the grant lags its pace.
4. **Drain:**
   - drain line 90 % (99 % for the grant), horizon 48 h (96 h for the grant), cap $15/h per pool, 2 concurrent children;
   - which job kinds are eligible, and which GitHub label marks a backlog issue as drain work;
   - that drain is never outward (no pushes);
   - the scale: ≈ $1,300 $-eq of drain over 8.8 days in the replay. Is there that much backlog, and should a lower
     weekly cap apply?
5. **API spend-down pacing replaces `prefer_within_days`.** The grant takes ~10 % of eligible flexible demand from
   day 1, rising as it lags, instead of only local-bound work in its last 3 days. Should it ship as a narrow change
   before the full controller, given the 12-02 expiry? The #114 money rules do not change.
6. **The $-eq currency and K values:** K = 12.5 per Claude Max 20x account and 11 for Codex, learned in $-eq with the
   owner's heartbeat tokens added. This is the assumption that a vendor's % is roughly proportional to API list price
   across its models. The closed loop tolerates error in K, but the split between vendors starts from it.
7. **Frontier share at R3** falls from 95 % to 84 % in the replay: D 8 code on Sol 6.1 when Codex is not burning.
   `ceiling_work` is unchanged. Keep `ceiling_underuse` as well (more frontier, less equalization), or accept?
8. **5-hour windows as gates only**: no new conversation on a pool at ≥ 95 % of its 5-hour window; never a target.
9. **Equal usage vs spare for the owner across accounts:** with B and Codex filled to ~89 %, the owner has little B left
   late in a week if he switches his own sessions to B. Should the reserve formula also apply to B, or only to the
   account his sessions run on (A today)?

---

## 11. Files

- `scripts/scarcity_v2_replay.py`: the replay (offline, stdlib, takes the data directory as an argument).
- This document. The replay's JSON results and summary tables stay in the owner's notes folder, next to the #143 audit
  data (not in the repository: they derive from the private route log).
