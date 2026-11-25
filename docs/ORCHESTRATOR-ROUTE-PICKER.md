# Orchestrator route picker (design, not built)

Owner request, 2026-11-24 (condensed): whenever a new Claude Code session starts, let me pick the route of the
orchestrating (main-loop) session instead of a hard-coded band or model:

1. pick a band (R1–R4) or "local";
2. see every model that band can use, and multi-select any of them;
3. the router picks among only the selected models by scarcity, across every account and lane that serves them
   ("Opus 5.5" can run on account A's Opus or account B's Opus; "local" means a local model such as Qwen on the Pro or
   the PC orchestrates);
4. "any route" (free S1 routing) and "any capable model in this band" are options too.

This document is a design. Nothing in it is built. Every statement about existing behaviour cites `file:line` on
`origin/main` at `60febb6` unless it names another commit. Open questions for the owner are marked **[ESCALATE]** and
collected at the end with a recommended default each.

---

## 0. What exists today (the starting point)

### 0.1 The band window (merged 762a5c4)

- The launcher routes the main loop through S1 only with `--orchestrator cortados`; the window rides on the main loop's
  model name, `--model cortados@<window>` (`cortados_router/session.py:85-102`, `build_argv` at `session.py:443-447`).
  On main the default window is `R4` (`session.py:78`); branch `band-window-r3-default` (commit `4717cde`) changes it
  to `R3`.
- Why R3: R4 has no index floor of its own (`core.py:334`: `floor = val(band) if band in refs and band != "R4"`), so
  an R4 window required only the D-interpolated index and let easy turns fall to Haiku-class models (`4717cde`'s
  message and its `docs/ROUTER-DESIGN.md` §13 hunk). R3 is a **minimum index, not a list**: every model whose index
  clears R3's reference for the turn's use case is a candidate, and the cheapest wins (README on `4717cde`).
- The provider reads the window from `x-cortados-band` or the `cortados@` model name on **every** request
  (`provider/server.py:3662-3672`, `provider/routing.py:121-155`); the two must agree; an invalid window is a 400.
- The window is part of a conversation's identity: the route stores it under `ASKED_WINDOW_KEY`
  (`server.py:2075`, set at `server.py:3154`), `_in_window` matches only conversations of the same window
  (`server.py:457-468`), and a request whose history belongs to another window opens a new conversation seeded with
  that history (a Fork; `server.py:1327-1346`, join gate `server.py:1494-1506`). A `sub/` pin is in every window
  (`server.py:468`).
- Claude Code sends the main loop's `--model` only on main-loop requests; subagents are forced to `cortados` and side
  calls are pinned to haiku (`session.py:283-291`; capture check of 2026-11-23 recorded at `README.md:354-357`).
  Changing the window mid-session with `/model cortados@R3` re-routes from the next turn at the cost of the backend's
  prompt cache (`README.md:378-380`).
- Child tasks launch with `--orchestrator-band any` and keep free routing (`child_tasks.py:1526-1528`).
- The routes row records the window in `routes.band_window` (`core.py:1644`, written at `core.py:1725-1726`, cut to 8
  characters) and as `band window …` rules; the Tasks view labels them (`provider/tasks.py:113-134`).

### 0.2 How the provider chooses a route

- **Once per conversation.** "The route is chosen once, for the whole conversation: switching models mid-conversation
  would lose the CLI session and its prompt cache" (`server.py:3115-3116`). S1 scores the conversation's **first** user
  message (`routing.py:203-209`). So for a main loop, "the router picks" means: at the opening turn, and again only when
  a request forks or rebuilds a conversation.
- **Candidates.** `core.scored_candidates` lists every model of `model-scores.generated.json` × every reach row
  (`core.py:341-368`), expanded per Claude account (`core.py:448`). The provider then restricts them to the pairs it can
  serve: `routing.served_routes` keeps only `SERVED_PROVIDERS = ("claude-cli", "codex-cli")` (`routing.py:68`,
  `routing.py:181-195`; `openai-codex` rows are served as their `codex-cli` clone, `routing.py:72`), expands Claude
  pairs across accounts (`routing.py:193-194`), and adds the API credit lane's Claude pairs when the request may take
  them (`routing.py:799`). `core.resolve` filters its candidates to that allowlist (`core.py:1242-1248`).
  Consequence: **Cursor, Antigravity and local rows are never provider candidates.** (The `4717cde` README hunk says an
  R3 window can pick "Sonnet 5.5 through Cursor"; Sonnet 5.5's only reach row is `cursor-cli`, so that holds for a shadow
  `core.py decide`, which has no allowlist, not for the provider. The picker below lists only served routes, so it
  cannot show a model the provider would never run.)
- **Requirement.** `core.required_index` interpolates over D through the knots and floors at the band's reference,
  except R0 (0) and R4 (no floor) (`core.py:319-338`). When **no** available candidate reaches it, the strongest
  available is taken and the rule says so (`core.py:1347`, `core.py:1565`).
- **Gates and ranking inside resolve.** Context fit is hard (`core.py:1276-1293`); spent pools and refusing vendors are
  `ok=False` (`core.py:835-847`, `candidate_health`); the interactive face floor (general ≥ 50, local exempt;
  `core.py:1407-1411`); native-first for secondary sources (`core.py:1414-1429`); reserve accounts
  (`core.py:1430`); the frontier rule for hard planning/research/code (`core.py:1442-1465`); the switching margin
  (`core.py:1501-1519`); stickiness; the underused-frontier rule (`core.py:1536-1563`).
- **Hard rules.** An outward request is floored at blast radius 3 (`core.py:1140-1142`), which keeps its band over a
  window's max (`core.py:1171-1173`). A degraded scorer never defaults to Claude: the provider strips Claude and API
  pairs from the allowlist (`routing.py:212-226`). Sensitive and local moves run **after** select
  (`routing.py:876-887`).
- **Pins.** `sub/<model>` skips S1 and the band and chooses among that model's accounts by the static ranking
  (`routing.py:509-651`). A local pin goes through `_choose_local` (`routing.py:654-695`). A friendly name resolves
  through `resolve_pin` (`routing.py:394-433`); `SUB_ALIASES` holds `opus`, `sonnet`, `haiku`, `fable`
  (`routing.py:267-268`).
- **Exclusions** are the closest existing mechanism to a model set: `x-cortados-exclude-model` removes every allowed
  pair of a named model before select (`routing.py:801-815`), and a refusal it caused becomes a 400 instead of a 503
  because "a 503 would have the client CLI retry until it timed out" (`routing.py:853-867`).

### 0.3 The bands and models today (table v23)

Reference indexes (`tables/model-scores.generated.json`, `reference_index`): general R1 13.3, R2 52.8, R3 79.6, R4 91.2;
code R2 47.9, R3 66.2, R4 78.5; planning R2 50.8, R3 76.1, R4 87.4. `band_references` (`tables/routing-table.json`,
`tiering.band_references`): R3 = Opus 5, Sol 5.6, Sol 6.1; R4 = Fable 5.1, Astra.

Served models (claude-cli / codex-cli rows only) and the bands whose general reference they clear:

| model (family) | general | code | planning | ceiling | served on | ≥ R4 | ≥ R3 | ≥ R2 |
|---|---|---|---|---|---|---|---|---|
| gpt-6-astra | 100.0 | 78.5 | 100.0 | yes | codex | ✓ | ✓ | ✓ |
| claude-opus-5-5 | 99.8 | 100.0 | 97.7 | yes | claude A, claude B, api credit | ✓ | ✓ | ✓ |
| claude-fable-5-1 | 91.2 | 84.0 | 87.4 | yes | claude A, claude B, api credit | ✓ | ✓ | ✓ |
| gpt-6.1-sol | 90.1 | 66.2 | 88.8 | no | codex | | ✓ | ✓ |
| claude-opus-5 | 81.5 | 76.3 | 82.4 | no | claude A, claude B, api credit | | ✓ | ✓ |
| gpt-5.6-sol | 79.6 | 67.0 | 76.1 | no | codex | | ✓ | ✓ |
| gpt-6-sol | 73.6 | 57.6 | 71.6 | no | codex | | | ✓ |
| gpt-5.5 | 73.0 | 57.5 | 66.2 | no | codex | | | ✓ |
| gpt-5.6-terra | 65.7 | 54.7 | 62.2 | no | codex | | | ✓ |
| claude-sonnet-5 | 52.8 | 47.9 | 50.8 | no | claude A, claude B, api credit | | | ✓ |
| gpt-5.6-luna | 50.2 | 49.9 | 44.8 | no | codex | | | |
| gpt-6-luna | 47.4 | 44.9 | 43.9 | no | codex | | | |
| claude-haiku-4-5 | 13.3 | 14.2 | 6.1 | no | claude A, claude B, api credit | | | |

(Account B and the API lane appear only when configured: `routing.py:189-194`, `routing.py:1025`.) Local models with a
published proxy index: `qwen-27b` (thinking off) general 16.7, `qwen-27b-think` 38.8 (`model-scores.generated.json`
`local_models`); the Pro's other models and the PC's Qwen3.6-35B-A3B are pin-only (`tables/local-models.json`
`index_proxies.not_mapped`).

---

## 1. Picker UX per entry point

### 1.1 The vocabulary the picker offers

| choice | meaning | wire (§3) |
|---|---|---|
| **any** | free S1 routing, no window, no model set (today's `--orchestrator-band any`) | `cortados` |
| **band only** | today's window: S1's band clamped into it, any served model clearing the requirement | `cortados@R3` |
| **any capable model in a band** | the model set = every served model whose general index clears the band's reference, resolved at conversation open; scarcity picks among them | `cortados@R3:*` |
| **selected models in a band** | the model set = the owner's selection; scarcity picks among their accounts and lanes | `cortados@R3:claude-opus-5-5+gpt-6.1-sol` |
| **local** | a local model orchestrates (§5) | `cortados@local` or `cortados@local:qwen-27b` |
| **one pinned model** | today's `--orchestrator opus` etc., unchanged (`sub/`, no S1) | `sub/claude-opus-5-5` |

`R3` and `R3:*` differ on purpose. `R3` keeps today's semantics (a floor on the index; R4 has no floor, `core.py:334`).
`R3:*` is a **list**: only the models listed for R3 are candidates, which is what the owner asked for and what the R4
default lacked. With a list, an R4 pick (`R4:*` = Astra, Opus 5.5, Fable) can never fall to Haiku.

### 1.2 Terminal launcher (`python3 -m cortados_router.session`)

**Interactive prompt** (only when stdin and stdout are a TTY and no route flag was given), shown before `os.execvpe`
(`session.py:529`):

```
CortadOS: route for this session's main loop        [Enter = last: R3 · Claude Opus 5.5 + GPT-6.1 Sol]
  1 R1   2 R2   3 R3   4 R4   5 local   6 any (S1 routes freely)   7 pin one model
> 3
R3 models (general index ≥ 79.6; state from cached readings, no vendor CLI opened)
  [x] 1 Claude Opus 5.5   claude A 62% left · claude B 40% left · API credit $142 left
  [x] 2 GPT-6.1 Sol       codex 71% left
  [ ] 3 GPT-6 Astra       codex 71% left                        frontier
  [ ] 4 Claude Fable 5.1  claude A Fable week 12% left · claude B spent      frontier
  [ ] 5 Claude Opus 5     claude A · claude B · API credit
  [ ] 6 GPT-5.6 Sol       codex 71% left
  a = any capable R3 model     b = band only (today's R3 window)
  numbers toggle, Enter accepts
> 
→ claude --model cortados@R3:claude-opus-5-5+gpt-6.1-sol
```

- The list comes from a new provider view, `GET /v1/cortados/route-options?band=R3` (§2), with a 2 s timeout. If the
  provider does not answer, the launcher says so in one line and offers only the last pick, the band choices and
  typed model names (validated by the provider on the first request; an unknown name is a 400 then).
- **Flags** for non-interactive use: `--route <pick>` with the grammar of §3 without the `cortados@` prefix
  (`--route R3:opus+sol`, `--route R4:*`, `--route R3`, `--route any`, `--route local:qwen-27b`), `--route last`, and
  `--no-route-prompt`. The existing `--orchestrator` / `--orchestrator-band` keep working; `--route` conflicts with
  them and is refused when both are given. Friendly names (`opus`, `sol`) are resolved by the launcher through the
  provider's list into table ids before they are written into the model name, so the wire carries canonical ids only.
- **Remembered last choice:** `~/.config/cortados/route-pick.json` (`{"pick": "R3:claude-opus-5-5+gpt-6.1-sol",
  "saved_at": …}`), 0600, written only after an interactive pick, under the same directory rules the launcher already
  enforces for its settings directory (`session.py` `write_settings`: 0700, no symlinked ancestors, owned by the user).
  Enter with no saved pick uses today's default (`--orchestrator fable`, `session.py:33`) — whether the default
  should become a routed pick is **[ESCALATE] E1**.
- **Never prompts** without a TTY, with `--print-env`, or under a child task (child tasks pass
  `--orchestrator-band any`, `child_tasks.py:1528`, and run headless).

### 1.3 Claude Code desktop app

What the repo shows a mod can do (the `cortados-routed-subagents` mod, `integrations/claude-mod/cortados-routed-subagents/hooks/register.ts`):

- events: `session.start` (`register.ts:3017`), `session.end` (`:3010`), `prompt.submit` (`:2897`), `prompt.compose`
  (system-prompt sections, `:3112-3120`), `tool.call` (`:2424`, `:2850`), `turn.start` / `turn.complete`
  (`:2923`, `:2940`), `prompt.attachment` (`:2956`), `command.run` (`:3050`, `:3074`, `:3109`);
- host APIs: `$.command.register` (slash commands registered at session start, `:3032-3046`), `$.ui.toast`,
  `$.ui.status`, `$.store`, `$.http.fetch` (`:2370-2372`), `$.session.id` (`:3082`), `$.process.run`.

**Nothing in the repo shows a mod changing the main loop's model, base URL or request headers.** And the repo states
that pointing a whole session at the provider needs the Claude Code CLI, not the desktop app (`README.md:1262`;
`README.md:435`: the whole-session launcher is "terminal Claude Code only"). The mod's own description says "The main
session stays on direct Claude" (`.claude-plugin/plugin.json`, `description`).

So in the desktop app **the main loop is not routed at all today**, and a picker there has nothing to steer. The design:

- **D-spike (step 0, §7):** check (a) whether desktop Code sessions honour `env.ANTHROPIC_BASE_URL` / a custom model
  from a settings file, and (b) whether the mod host API offers any way to set the session's model. Neither is assumed.
- Until the spike says yes, the desktop gets a **`/route` command** in the mod (registered at `session.start` like
  `/cortados-local`, `register.ts:3043`) that shows the options view and says, in one line, that this session's main
  loop runs on direct Claude and that a routed main loop needs the terminal launcher. It changes nothing.
- If the spike says the desktop can run a whole session on the provider, `/route` shows the same list as the launcher
  and prints the exact `/model cortados@…` line; if the host API can set the model, `/route` applies it directly.
  **[ESCALATE] E9** (how much desktop work is wanted before the spike).

A Claude Code `SessionStart` settings hook (a command hook, not a mod) is not used anywhere in the repo; nothing here
shows it can prompt the user or change the model, so it is not relied on.

### 1.4 Changing the pick mid-session

- **Terminal:** `/model cortados@R3:claude-opus-5-5` (Claude Code's own command; already the documented way to change a
  window, `README.md:378-380`). The launcher turns the routed-subagents mod off for its sessions (`session.py:319`,
  `README.md:406`), so a mod `/route` is not available there unless a small separate mod is enabled for launcher
  sessions; it would only print the `/model` line (no evidence a mod can set the model). Recommended: the launcher
  prints the exact `/model` spellings of the current pick's neighbours (e.g. "widen: `/model cortados@R3:*`") at start.
- **What a change costs.** The pick is part of the conversation's identity (§3.4), exactly like the window today: the
  next request opens a **new backend conversation** seeded with the history it carries (Fork). That costs (1) the
  backend's prompt cache, (2) the new route re-reading the whole history on its first turn, and (3) a 413 if the
  history does not fit any selected model's context (`core.py:1280-1282`; pins: `routing.py:570-586`). Nothing is
  lost on the client side; switching back later forks again (it does not return to the first backend, whose
  conversation is reaped on its own schedule).

### 1.5 Other clients

- **Codex** (`/v1/responses`): it cannot use `sub/` pins (`README.md:1262`) and has no subagents to separate, so it
  takes the pick as a header, `x-cortados-pick: R3:gpt-6.1-sol+gpt-6-astra` (§3.3), from its provider config. No
  interactive picker (Codex owns its own UI); a `--route`-style helper that prints the config line is enough.
- **Grok / bot gateway:** bots never pick models ("model pins refused (routed only)", `docs/GROKBOT-BRIDGE.md:23`).
  Out of scope.
- **Child tasks** (`start_task`): unchanged, free-routed (§4.6).

---

## 2. The model list per band

New owner-token view `GET /v1/cortados/route-options?band=R3` (also `band=local`), metadata only. It reuses what the
router already computes and **opens no vendor CLI**: readings come from the cache (`core.subscription_readings`,
cache-only by design, `core.py:850-856`; `core.lane_health(refresh=False)`, `core.py:835`), never
`lane_health()`'s default `refresh=True`.

For a cloud band:

1. Families: every model of `model-scores.generated.json` with at least one reach row in `routing.served_routes`
   (`routing.py:181-195`) plus the API lane's Claude pairs (`routing.api_lane_pairs`, `routing.py:1025`).
2. Listed for band B when `index.general ≥ reference_index[B].general`. For R0 every served model (R0's requirement is
   0, `core.py:336-337`). Each row also carries the use cases where it falls **short** of B's reference (e.g. a model
   clearing R3 general but not R3 code), so the owner sees that S1 may judge it too weak for some turns (§4.3).
3. Models below the interactive face floor (`selection.interactive_min_general_index` = 50, `core.py:1407-1411`) are
   shown greyed with "below the face floor: never chosen for an interactive turn" (gpt-6-luna, claude-haiku-4-5).
4. Per model, its routes grouped by lane and account: `claude-cli` (A), `claude-cli#b` (B), `anthropic-api`,
   `codex-cli`, each with `state` (`ok`, `spent`, `refusing`, `unread`), the tightest window's percent left and reset
   time, `reserve` (accounts config), and pool notes (Fable is sized by the all-models week, ROUTER-DESIGN §12).
5. Flags: `ceiling` (frontier), `secondary` (another vendor's copy; none among served routes today).
6. Not listed: Cursor, Antigravity, and local rows (never provider candidates, `routing.py:68`), with one summary line
   naming how many models were left out and why, so "where is Sonnet 5.5?" has an answer.

For `band=local`:

- Every local pin of `local_lane.pins()` (`local_lane.py:800`) with its node, alias, tag, `context_length`, published
  proxy index if any, and live state from the node's gate (`provider/local_switch.py` `GET /v1/cortados/local`:
  switch, `allowed` + reason, power, models loaded; `local_switch.py:1-8`, `:28`), plus the `/api/tags` probe
  (`local_lane.probe`, `local_lane.py:874`): `available`, `switch off`, `on battery`, `node unreachable`,
  `not listed on the node`, `busy`. A local config clash (`routing.local_clashes`, `routing.py:436-449`) is shown as
  the misconfiguration it is, never guessed around.

Freshness: each section carries `as_of`; readings older than the scarcity cache's own staleness limit are shown as
"unread", not as a number.

---

## 3. The wire format

### 3.1 Constraints

- Claude Code sends the main loop's `--model` string as given; `cortados@R1-R2` survived unchanged on the main loop and
  did not reach subagents (`README.md:354-357`). So the characters `@` and `-`, letters and digits are proven.
- `:` and `+` are **not yet proven** to survive `--model` and `/model` unchanged. Step 0 (§7) checks them with the same
  capture-server method; if either is altered or refused, the fallback spelling uses `~` for both separators
  (`cortados@R3~claude-opus-5-5~gpt-6.1-sol`), checked the same way. Also checked there: that Claude Code does not
  change behaviour based on substrings of a custom model name (for example treating a name containing `opus` or
  `haiku` as that model). Unverified today.
- Bounded: at most 8 models and 128 characters after `cortados@`, the same cap as pin names (`routing.py:276`,
  `PIN_NAME_MAX`) and the exclusion header (`server.py:440`, `:445`).
- Identity-safe: the canonical label (§3.4) is built only from band names and table model ids, never from client text,
  so it can be logged and shown in the views as metadata.

### 3.2 Grammar (ABNF; case-insensitive)

```
pick-model   = "cortados@" pick                     ; Anthropic wire, main loop's model name
pick-header  = pick                                 ; x-cortados-pick, both wires
pick         = cloud-pick / local-pick
cloud-pick   = window [ ":" model-set ]
window       = band [ "-" band ] / "any"            ; today's grammar, core.parse_band_window (core.py:1083-1090)
band         = "R" ("0" / "1" / "2" / "3" / "4")
local-pick   = "local" [ ":" local-set ]
model-set    = "*" / model *7( "+" model )
local-set    = model *7( "+" model )
model        = 1*64( ALPHA / DIGIT / "." / "-" / "_" )
```

Rules on top of the grammar:

- `cortados@R3` (no `:`) is exactly today's window, parsed by the existing `band_model_window` / `parse_band_window`
  path (`routing.py:121-155`, `core.py:1083-1090`), so every existing session and test is unchanged.
- `any:<models>` is a model set with no window (S1's band, restricted to the set). `any:*` is refused (it means
  `cortados`).
- Each `model` resolves to one served model id: exact table id or reach id first (release date optional, as
  `model_key`, `routing.py:324-327`), then a `SUB_ALIASES` key (`routing.py:267-268`), then the friendly resolver
  `resolve_pin` (`routing.py:394-433`) restricted to served cloud models (for a cloud pick) or local pins (for a local
  pick). Ambiguous → 400 with the candidates (`AmbiguousPin`, `routing.py:344-351`); unknown → 400 with the band's
  list; a duplicate after resolution is dropped.
- A cloud model in a local pick, or a local model in a cloud pick, is a 400 in v1 (mixed picks: **[ESCALATE] E6**).
- A selected model outside the window's list (e.g. `R4:gpt-5.5`) is allowed: the owner named it. The options view
  marks it; the route row records `route pick: gpt-5.5 below R4's reference` (**[ESCALATE] E3** asks whether to refuse
  instead).
- An invalid pick is refused with a 400 on every request, continuations included, before any conversation lookup,
  as an invalid window is today (`server.py:3662-3672`).

### 3.3 Header

`x-cortados-pick` carries the same `pick` on both wires (owner token only, like every routing header,
`routing.py:87-90`). When both the header and a `cortados@` model name are present they must name the same canonical
pick, else 400 (the rule `band_window` applies today, `routing.py:151-152`). `x-cortados-band` stays; a request
carrying both `x-cortados-band` and a pick with a different window is a 400.

### 3.4 Identity

- **Canonical label:** `<window label>:<sorted unique model ids joined by +>`, or `<window label>:*`, or
  `local:<sorted ids>`; e.g. `R3:claude-opus-5-5+gpt-6.1-sol`. `R3` and `R3-R3` are one window (as today,
  `core.band_window_label`, `core.py:1093-1095`), and `opus+sol` and `sol+opus` are one pick. A window-only pick keeps
  today's label (`R3`), so existing conversations keep their identity across the upgrade.
- The label replaces the window label wherever identity is decided: `ASKED_WINDOW_KEY` in the route
  (`server.py:3154`), `_in_window` (`server.py:457-468`), the failure key (`server.py:1199-1214`), the fork join gate
  key (`server.py:1503`) and the replay filter (`server.py:1605-1623`). One generalisation, no second mechanism.
- `R3:*` is identified by the label, not by the set it resolved to: a nightly table rebuild that changes which models
  clear R3 does not fork a running conversation (the conversation's route is already chosen, `server.py:3115`). The
  resolved set is recorded in the route row (§6).
- A `sub/` pin stays outside picks, as it is outside windows (`server.py:468`).

---

## 4. Server semantics

### 4.1 The candidate set

In `routing.choose` (`routing.py:736`), after the API lane pairs are added (`routing.py:799`) and before exclusions
(`routing.py:800-815`):

```
allowed = served_routes + api_lane_pairs
if pick.models:                      # explicit set or the band's resolved list for "*"
    allowed = [(p, m) for (p, m) in allowed if family_of(m) in pick.models]
allowed -= excluded_routes           # x-cortados-exclude-model still applies
constraints["allowed_routes"] = allowed
constraints["band_window"] = pick.window   # unchanged path; None for "any:<models>"
```

`family_of` maps every reach id to its model-scores family (`claude-opus-5-5` ← `claude-opus-5-5`; `gpt-6-1-sol` ←
`gpt-6.1-sol`), so "Opus 5.5" means Opus 5.5 on `claude-cli` (A), `claude-cli#b` (B) and `anthropic-api` alike.
`core.resolve` then does what it already does inside that set: health, fit, face floor, reserve accounts, the frontier
rule, scarcity cost, underuse split, switching margin. Nothing in `core.resolve` changes: the restriction is an
allowlist, the mechanism `resolve` already applies before selection (`core.py:1242-1248`).

If the filter leaves no pair at all (every selected model unserved, or all excluded), it is a 400 naming the models,
as an exclusion that empties the pool is today (`routing.py:808-813`).

### 4.2 The hard rules (unchanged; how they meet a pick)

| rule | with a pick |
|---|---|
| Outward / blast radius 3 floors the band (`core.py:1140-1142`, `:1171-1173`) | Still floors the band, so the required index is the floored band's. The pick still limits the models. If no selected model reaches that index, resolve takes the strongest selected one and records it (`core.py:1347`, `:1565`). **[ESCALATE] E3**. |
| Degraded scorer never defaults to Claude (`routing.py:212-226`) | Unchanged and not overridable. A Claude-only pick while S1 is degraded has no candidate: refused (503) with a message naming the pick and the rule. A pick with a Codex member runs on it. The launcher warns at pick time when a pick is Claude-only. |
| Sensitive → local (`routing.py:876-881`, `sensitive_local`) | Runs after select as today and **wins over the pick** (it exists so that sensitive work stays on the owner's machine); recorded as `route pick … overridden: sensitive`. **[ESCALATE] E5**. |
| Cost moves to local (`auto_local`, `index_local`: `routing.py:882`, `:1452`, `:1631`) | **Skipped** for a cloud pick: they move work off the selected models for cost, which is exactly what a pick forbids. Recorded as `route pick: local cost route not taken`. |
| API credit lane (`routing.py:1025`, `core.py:971-985`) | A selected Claude model includes its API-lane pair when the lane would be offered anyway; its own refusals (degraded, outward, blast radius > 1, stop line) still apply. **[ESCALATE] E4** (whether "every lane" should include paid credit). |
| Frontier rule (`core.py:1442-1465`) | Applies within the set. A pick with no frontier model gets "no frontier model available … the index rule alone applies" (`core.py:1464`), the existing graceful path. |
| Underused-frontier rule (`core.py:1536-1563`) | Only spare frontier pools inside the set. |
| Context fit (`core.py:1276-1293`) | Hard, per route. All selected routes too small → 413 (`server.py:3146-3149`). |
| 5-hour and weekly windows | A spent window makes the route `ok=False` (`core.py:835-847`); a hot 5-hour window raises the cost (`core.py:1373-1396`). The client floor (`x-cortados-min-left-pct`) applies through `hold_ok` and `floor.reserve` (`routing.py:822-826`, `:888-905`). |
| Face floor (`core.py:1407-1411`) | Applies; a pick made only of models below it gets no candidate on an interactive turn unless every candidate is below it (the floor only bites when something stronger remains, `core.py:1410`). |
| Switching margin (`routing.py:161`, `core.py:1501-1519`) | Unchanged: a previous route is held only if it is a candidate, so a pick naturally excludes routes outside it. |

### 4.3 When the selection cannot meet the requirement

S1 may score a turn above what the selected models clear (e.g. `R3:gpt-6.1-sol` and a code turn at D 9, where the
frontier rule wants Opus 5.5 / Fable / Astra). Today's behaviour for an unrestricted request is "strongest available
taken" (`core.py:1347`, `:1565`). With a pick that becomes "strongest **selected** model taken". Recommended: keep it
(the owner chose the set), record `route pick: no selected model reaches 87.4; strongest selected taken`, and surface
that rule in the Tasks view. **[ESCALATE] E3**.

### 4.4 When every selected model is unavailable or exhausted

Resolve then has no usable candidate and execution is refused (`core.py:1570-1576`); the server answers 503
(`server.py:3149-3150`), and Claude Code retries a 503 until it times out (`routing.py:864-865`). Options:

- **A. Refuse, clearly (recommended).** The error names each selected route and why it was skipped (the shape
  `choose_pinned` already uses, `routing.py:600-621`), says when the earliest one resets, and gives the exact widening
  line (`/model cortados@R3:*` or `/model cortados@R3`). Status: 429 when every route is spent or under the client
  floor (retrying soon will not help, and Claude Code shows a 429 as a limit), 413 for fit, 503 only when a dependency
  failed. Nothing runs on a model the owner did not pick.
- **B. Fall back to the band.** Route as `cortados@<window>` for this conversation, with a rule and a one-line notice
  in the reply (the `sensitive_notice` mechanism, `provider/sensitive_notice.py`). Keeps the session moving; spends
  models the owner excluded.
- **C. Ask.** The provider cannot ask mid-request; in practice this is A with the widening instructions, plus the
  launcher/`/route` showing live state before the pick.

**[ESCALATE] E2**, recommended A.

This applies when a conversation opens or forks. A conversation already running keeps its route
(`server.py:3115`); what happens when its own route's pool is spent mid-conversation is unchanged by this design.

### 4.5 Local picks

Local lanes are never in `served_routes` and `core.resolve` never sees them (`routing.py:11-12`,
`docs/LOCAL-RESOURCES.md:190`). So a local pick does not go through S1's selection. It is a **pin over a set**: the
first selected local model (in the pick's order as typed, then config order) whose node answers the probe, lists the
model, is not busy, and fits the request (the checks of `_choose_local`, `routing.py:654-695`) takes the conversation,
logged as a pin row with the pick. No quota, so no scarcity ranking (the owner asked for "scarcity across accounts",
which does not exist for local; availability is the only signal). Nothing falls back to the cloud, as with a local pin
today (`docs/LOCAL-RESOURCES.md:187-189`). §5 covers whether this is wise.

### 4.6 Subagents and child tasks

- Subagents send plain `cortados` (`session.py:283-290`; capture check `README.md:354-357`), so a pick does not reach
  them and they stay free-routed. A forked subagent that inherits the main loop's history opens its own conversation in
  no window (the Fork path, `README.md:375-379`); that stays true for picks.
- Child tasks launch with `--orchestrator-band any` (`child_tasks.py:1526-1528`) and stay free-routed.
- **[ESCALATE] E7**: whether a pick should ever propagate (to subagents via a header, or to child tasks via a
  `start_task` option). Recommended: no; add a separate `start_task` `route` option later if wanted.

---

## 5. The local orchestrator: what it would mean, honestly

A local main loop is the owner's explicit choice, so it would be a pin. Today a local pin is already possible from the
launcher (`--orchestrator <alias>` becomes `sub/<alias>`, `session.py:103-110`; local pins resolve through
`routing.py:528-529`). The picker would make it a menu item. What that main loop would face:

**Capability.** The only local model with an index is the Pro's Qwen3.8 27B Heretic: general 16.7 with thinking off,
38.8 with thinking on (`model-scores.generated.json` `local_models`). That is below R2's reference (52.8) and below the
interactive face floor (50) that keeps every routed interactive turn on at least a Sonnet-class model
(`core.py:1407-1411`; local lanes are exempt only because they are never routed there by S1). The other three Pro
models and the PC's Qwen3.6-35B-A3B have no published base mapping (`tables/local-models.json` `not_mapped`); all are
community fine-tunes without a published evaluation (`docs/LOCAL-RESOURCES.md:291-293`). An orchestrator plans,
decomposes and verifies others' work: the hardest job in the session, given to the weakest model in the pool.

**Tools.** The relay passes `tools` and `tool_use` blocks through untouched (`docs/LOCAL-RESOURCES.md:175-178`), so
Claude Code's tools reach the model. How reliably an abliterated 27B emits valid tool calls over a long session is not
measured anywhere in the repo.

**Context and speed.** The Pro's configured context is 128k (`docs/LOCAL-RESOURCES.md:171`, `:203-205`). The relay is
stateless: every turn carries the whole conversation (`docs/LOCAL-RESOURCES.md:178-179`). On the vllm-mlx backend the
prefix cache is off and text is prefilled one sequence at a time in 256-token chunks (`AGENTS.md:32`), so each main-loop
turn re-prefills the entire history plus Claude Code's system prompt and tool schemas. One request runs at a time
(`OLLAMA_NUM_PARALLEL=1`, `docs/LOCAL-RESOURCES.md:275-278`): the main loop would queue behind, and block, any local
subagent or auto-routed work. Expect turns measured in tens of seconds to minutes at mid-session sizes.

**Availability.** The Pro serves only on AC power with its switch on (`docs/LOCAL-RESOURCES.md:153-155`, `:211`); the
PC serves only when the owner is not gaming or using the GPU (`AGENTS.md`, `node_policy.py` summary). A closed lid, a
battery, a switch flip or a game ends the session's ability to answer (503, no fallback,
`docs/LOCAL-RESOURCES.md:187-189`). The conversation's model is fixed (`docs/LOCAL-RESOURCES.md:180-182`); changing to
a cloud pick forks (§1.4).

**Safety.** An explicit local pin never passes the effect boundary (`effect_boundary.py:29-30`); only automatic local
moves do. The models are abliterated ("Heretic"). As a pinned main loop it would drive the session's full toolset:
Bash, edits, every MCP connector, Chrome, and `start_task` (child tasks with their own auto-mode tools), with Claude
Code's permission prompts as the only guard. The automatic boundary's profiles would not help as they stand: their
allowlist drops every `mcp__*` tool (`effect_boundary.py:8-11`), which removes `mcp__cortados-route__start_task` /
`route_task`, so an orchestrator under them could not delegate.

**What would make it usable and safe enough (proposal, not built):**

1. A third boundary profile, `orchestrator`, applied to every local main-loop pick: allow `Read`, `Grep`, `Glob`,
   `TodoWrite`, `AskUserQuestion` and exactly the `mcp__cortados-route__` tools (`start_task`, `route_task`,
   `task_status`, `wait_task`, `list_tasks`, `cancel_task`); no edits, no Bash except start_task's `watch_command`
   shape (the gate already recognises it, `register.ts:1675-1695`), no other connector. The local model then only
   plans and delegates, which is the role the orchestrator rules give the main loop anyway. The client hook
   (`docs/BOUNDARY-HOOK.md`) enforces it on the client as for other boundary turns.
2. Child tasks it starts keep free routing and their own guards; whether a local orchestrator may start tasks with
   outward effects at all is the owner's call.
3. A visible banner at launch: model, index, context, node state, "plans and delegates only".

**Verdict.** Feasible as an opt-in experiment behind the `orchestrator` profile; not fit as a default, and not without
the profile. **[ESCALATE] E8** (whether to build local picks at all in v1, and with which profile). Recommended: build
the cloud picker first; ship local picks only behind the `orchestrator` profile and after a shadow comparison of S1's
bands for the owner's real first prompts against the local model's index.

---

## 6. Logging and views

- **Routes row:** a new nullable column `route_pick TEXT` (the canonical label, ≤ 136 characters) added to
  `EXTRA_COLUMNS` (`core.py:1644`) and written the way `band_window` is (`core.py:1725-1726`). `band_window` keeps
  its meaning and its 8-character cut, so existing readers are unaffected. The resolved set of a `*` pick goes into
  `hard_rules` as `route pick R3:*: 6 models (claude-opus-5-5, …)`; other rules: `route pick R3:…: 2 models, 4
  routes`, `route pick … overridden: sensitive`, `route pick: local cost route not taken`, `route pick: no selected
  model reaches <req>; strongest selected taken`, `route pick: <model> below R4's reference`.
- **Pins:** a local pick's row is a pin row (engine `pin`, `routing.py:681-688`) with `route_pick` set.
- **Tasks view** (`provider/tasks.py`): a `_PICK_RULE` beside `_WINDOW_RULE` (`tasks.py:117`) labels the rules from a
  fixed vocabulary: `route pick R3 (2 models)`, `route pick overridden`, `route pick below requirement`. Model ids in
  labels are accepted only when they are table ids (never free text), as the window pattern admits only band names
  (`tasks.py:113-116`). A `pick` field carries the label.
- **Live view** (`provider/live.py`): each routed conversation item gains `pick` (label); the orchestrators tree shows
  it on the main loop's sub-session; the session view (`GET /v1/cortados/live/session`) and the pulse pane show the
  current session's pick and its chosen route ("R3: Opus 5.5 · claude B").
- **Office view** (`provider/office.py`): the desk of a routed main loop shows the pick label as a badge (metadata
  only, `office:read` scope unchanged).
- **Dashboard:** the Tasks tab's band column shows `R3 · pick` with the label on hover; the route-options view (§2) is
  a card on the Live tab.

---

## 7. Build plan (small reviewable steps)

Every step: focused tests while working, then `python3 -B -m unittest discover -s tests -t .`, then the
`verify-router` skill's evidence block. Tests use fakes and a throwaway `CORTADOS_HOME`; none opens a vendor CLI or
starts the provider.

| step | what | acceptance |
|---|---|---|
| 0 | **Spike, no merge.** Capture server (as on 2026-11-23): does Claude Code pass `cortados@R3:claude-opus-5-5+gpt-6.1-sol` unchanged on `--model` and `/model`, only on main-loop requests, subagents still `cortados`? Same for the `~` fallback. Does a custom name containing `opus`/`haiku` change the request shape? Desktop: does a Code session honour a settings `env.ANTHROPIC_BASE_URL` and custom model? Does the mod host API expose any model setter? | A short note with captured request lines, added to this doc; the grammar's separators fixed accordingly. |
| 1 | **Parser.** `routing.route_pick(header, model)` → `(Pick, error)`; `Pick(window, models, local, label)`; canonicalisation; model resolution against served/local lists. Window-only picks delegate to `band_window` unchanged. | Table-driven tests: every grammar case, case-insensitivity, ordering/duplicates give one label, `R3` keeps label `R3`, 400s (bad band, empty set, `any:*`, > 8 models, > 128 chars, ambiguous, unknown, mixed local/cloud, header ≠ model), friendly names. Existing band-window tests pass unchanged. |
| 2 | **Selection.** `routing.choose(..., pick=)`: allowlist filter by family across accounts and the API lane; `*` resolution; cost local moves skipped; sensitive still wins; refusal shapes (§4.4, per the E2 decision). | Fake-core tests: `R3:claude-opus-5-5` offers exactly `claude-cli`, `claude-cli#b`, `anthropic-api` Opus 5.5 pairs; scarcity picks B when A is hot; all spent → the chosen refusal status with every route named; degraded + Claude-only → refusal naming the rule; degraded + mixed → Codex member; outward turn → band R3, still within the set; `R4:*` never yields a model below R4's reference; exclusion emptying the set → 400. |
| 3 | **Identity.** The pick label replaces the window label in `ASKED_WINDOW_KEY`, `_in_window`, failure key, fork gate and replays. | Tests: changing the pick forks with the inherited history; `opus+sol` and `sol+opus` continue one conversation; `R3` conversations opened before the change still match `R3`; a pin is in every pick; concurrent identical forks open once. |
| 4 | **Options view.** `GET /v1/cortados/route-options` (owner token). | Tests: no vendor CLI opened (fake scarcity asserts `refresh=False`); Cursor-only models absent and counted; face-floor rows flagged; per-account state; local section from a fake gate; misconfigured local config shown as such; response is metadata only. |
| 5 | **Logging and views.** `route_pick` column, rules, Tasks labels, Live/Office fields, dashboard. | Tests: column migrates on an old db; labels only from table ids; Live session view shows the pick; Office badge; nothing request-derived. |
| 6 | **Launcher.** `--route`, `--route last`, `--no-route-prompt`, interactive prompt on a TTY, `route-pick.json`. | Tests: no prompt without a TTY, under `--print-env`, or for child tasks; `--route R3:opus+sol` → `--model cortados@R3:claude-opus-5-5+gpt-6.1-sol`; conflicts with `--orchestrator-band` refused; pick file 0600 under the settings directory rules; provider unreachable → offline menu. |
| 7 | **`/route` helper** (per step 0's findings): desktop mod command and, if wanted, a launcher-session mini mod. | Mod tests with the existing harness (`hooks/*.test.ts`): command registered at session start; text only; never claims to have switched unless the host API did. |
| 8 | **Local picks** (only after E8): `orchestrator` boundary profile, local pick selection, banner. | Tests: profile allowlist exact; client hook denies edits/Bash; pick picks the first available local model; node down → 503 with the node state, no cloud fallback. |
| 9 | **Codex header docs** and the README / ROUTER-DESIGN §13 update. | Docs reviewed; README test count refreshed. |

Steps 1–3 are one PR's worth of risk (routing and identity) and should be reviewed adversarially (identity and
concurrency); 4–6 are a second PR; 7–8 follow decisions.

---

## 8. [ESCALATE] questions for the owner

| # | question | options | recommended default |
|---|---|---|---|
| E1 | What does Enter do at the launcher's prompt the very first time (no saved pick), and should the prompt show on every TTY launch? | (a) today's default, `--orchestrator fable` pinned; (b) `R3:*`; (c) `R3` window | Prompt on every TTY launch (the request says "whenever a new session starts"), `--no-route-prompt` to skip; first-time Enter = (a), today's behaviour, until the owner saves a pick. |
| E2 | Every selected model is unavailable or exhausted when a conversation opens or forks. | A refuse with each route's reason, earliest reset and the widening `/model` line; B fall back to the band with a notice; C ask (= A in practice) | **A**, 429 for quota, 413 for fit, 503 only for a failed dependency. |
| E3 | A turn's required index (outward floor, frontier rule, high D) exceeds every selected model; and may a pick include a model below its band's reference? | run on the strongest selected and record it; refuse; widen to the band for that conversation | Run on the strongest selected, recorded and shown in Tasks; allow below-band models but mark them in the picker and the row. |
| E4 | Does "every lane that serves the model" include the paid API credit lane for a selected Claude model? | include when the lane would be offered anyway; exclude unless named (`api/…` in the pick) | Include (it is scored like a pool since table v22 and keeps its own refusals and stop line). |
| E5 | A sensitive-marked request under a cloud pick. | sensitive rule wins (moves to local, recorded); pick wins (stays cloud with the existing "warn, then use cloud" notice) | Sensitive rule wins. |
| E6 | Mixed local + cloud picks (`R2:qwen-27b+gpt-5.5`). | refuse in v1; allow with local taken when admitted (cost boundary applies, which drops the delegation tools) | Refuse in v1. |
| E7 | Should a pick propagate to subagents or child tasks? | no; subagents via header; child tasks via a `start_task` `route` option | No. A `start_task` `route` option can be a separate request. |
| E8 | Build local picks at all, and under which guard? | not in v1; opt-in with the `orchestrator` profile (plan and delegate only); opt-in as a plain pin (full tools) | Opt-in with the `orchestrator` profile, after the cloud picker ships; never as a plain full-tool pin. |
| E9 | Desktop app: how much to build before step 0 shows whether a desktop session can run on the provider at all? | nothing; a read-only `/route` that shows options and explains; full picker | Read-only `/route` only; the full picker waits for step 0. |
| E10 | The `R3` window (no model list) stays as an option beside `R3:*`? | keep both; make `R3` mean `R3:*` | Keep both: `R3` is today's floor semantics and existing sessions' identity; the picker's band screen offers `*` by default and `band only` as one key. |
