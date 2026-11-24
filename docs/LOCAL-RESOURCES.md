# Local resources and the home cluster

Use the home Mac Studio as the stable coordinator. The RTX 3060 worker connects
over the home LAN. The Air can join jobs when it will remain awake for their full
duration. The traveling Pro is a client over Tailscale, and is excluded from home
model placement by default. A router transports traffic; its port speeds and the
links between workers still determine whether distributed inference is useful.

| Node | GPU-addressable memory inventory | Separate host RAM | Membership |
| --- | --- | --- | --- |
| M1 Studio | 32 GiB unified physical memory | Same shared memory | Home coordinator |
| RTX 3060 PC | 12 GiB dedicated VRAM | 32 GiB DDR4-3200 | Home worker, WSL2 |
| M2 Air | 16 GiB unified physical memory | Same shared memory | Optional |
| M5 Max Pro | 48 GiB unified physical memory | Same shared memory | Traveling client |

These are inventory figures, not live free memory. The reliable Studio + PC pair
has 44 GiB nominal GPU/unified capacity; the Air brings that to 60 GiB. The PC's
32 GiB RAM is CPU-offload capacity, not an extra 32 GiB of VRAM. The collector
reports WSL's VM memory limit separately from Windows' physical RAM inventory.

## Opt-in integration

No provider credentials or subscription probes are needed to collect local
resources. The new sampler is selected by `CORTADOS_LOCAL_RESOURCES_CONFIG`.
Without it, the existing router's local sampling behavior is preserved.

```sh
export CORTADOS_LOCAL_RESOURCES_CONFIG=/path/to/resources.json
python3 -B -m cortados_router.local_resources --config "$CORTADOS_LOCAL_RESOURCES_CONFIG"
python3 -B cortados_router/scarcity.py local --force
```

Use your actual `CORTADOS_HOME` for production; a separate directory lets you
review the readings before changing the live router. The last command samples
only the local lane, without opening subscription CLIs.

Configuration structure:

```json
{
  "max_age_s": 120,
  "nodes": [
    {"id": "studio", "target": "owner@studio.local", "pool_eligible": true,
     "metal_probe": "/absolute/path/to/metal-budget", "reserve_gib": 4},
    {"id": "pc3060", "target": "owner@wsl-host", "pool_eligible": true,
     "reserve_gib": 2},
    {"id": "pro", "target": "local", "pool_eligible": false,
     "metal_probe": "/absolute/path/to/metal-budget", "reserve_gib": 6}
  ],
  "deployments": []
}
```

Targets are SSH destinations with existing host trust and noninteractive key
authentication. A null target means unconfigured, not offline or free. The
collector is streamed to `python3 -` over SSH; it creates no remote files and
does not probe the single-connection RPC port. On the coordinator, `local` means
the coordinator itself: update the example's local Pro entry when moving this
configuration to the Studio. Use the Studio's LAN address for home workers;
Tailscale allows the traveling Pro to access the home API privately.

## What is measured

- Apple: actual page size and reclaimable-memory estimate from `vm_stat`, driver
  GPU utilization/allocation counters from IOAccelerator, and Metal's recommended
  working-set budget from a tiny local helper. Free GPU headroom is an estimate,
  capped by both the remaining Metal budget and reclaimable host memory, minus
  the configured reserve. Unified memory is never counted twice.
- NVIDIA: `nvidia-smi` total/free VRAM and utilization. Linux/WSL host memory is
  sampled from `/proc/meminfo` and stays separate from dedicated VRAM.
- Offline, stale, unconfigured, permission-denied and unavailable readings stay
  unknown. A fresh online Pro contributes no home-pool capacity while excluded.
- Utilization percentages remain per node. They do not turn into additive FLOPS
  or tokens/sec. Record speed from a representative inference benchmark for each
  actual model, context length, quantization and worker placement.

The helper source is `Ai/local-ai/cluster/metal-budget.swift` in this setup. On
each Mac with Xcode Command Line Tools, compile it with:

```sh
xcrun swiftc -framework Metal metal-budget.swift -o metal-budget
```

## Per-machine hardware sample (`cortados_router/hardware.py`)

A single-machine snapshot, separate from the fleet collector above, that treats local hardware as a measured
resource in the pool next to subscription "% left".

- Measured (macOS Apple Silicon): chip (`sysctl machdep.cpu.brand_string`), total memory (`hw.memsize`), available
  memory (`vm_stat` free + inactive + speculative pages x page size; inactive is reclaimable, not guaranteed free),
  CPU busy % from the second `top -l 2 -n 0 -s 1` sample (the first is since boot; `ps` %cpu is a decaying lifetime
  average and lags), 1/5/15 min load, GPU busy % (`ioreg -c IOAccelerator` "Device Utilization %", busiest
  accelerator), and resident Ollama models (`GET 127.0.0.1:11434/api/ps`: name, size, size_vram).
- Derived `headroom`: free_mem_gb, cpu_idle_pct, gpu_idle_pct; `null` when the input was unavailable.
- Other OSes: host, load and CPU count are reported; the rest is `{"available": false, "reason": ...}`. Never crashes.
- Every command has a 4 s timeout, the Ollama fetch 0.5 s per socket operation within a 2 s whole-body budget; each failure is an entry in `errors`.
- `sample --out` writes `<state dir>/hardware/<host>.json` atomically (state dir per `paths.state_dir()`; 0600 file; only directories it creates are tightened to 0700). `--out PATH` never changes the mode of an existing directory. GPU is read from `ioreg -a` (plist), never the width-clipped text form.
- No installer schedules `sample --out` any more: each host's live heartbeat (`cortados_router/live_heartbeat.py`)
  carries the sample, and the dashboard's Usage and Live tabs read it from the provider's heartbeat board; a
  `hardware/` directory is only read, as a fallback, where it still exists (#123).
- Next: the `/resources` snapshot shows these samples per host; later, routing can use `headroom` and
  the resident models to decide whether a local lane is worth trying (a loaded model costs no load time). This
  ticket does not wire it into routing or admission.

## Per-model admission

The router admits a local candidate only when its exact model ID has a verified
deployment and every assigned node is eligible, online, fresh, and has enough
estimated memory for the next request. Availability of one model does not admit
another model. Unknown telemetry cannot make a route free. Context is capped by
both the existing routing policy and the deployment's verified context limit.

After a real inference check, add a deployment like this (values must come from
your measurement; these example memory numbers are not capacity predictions):

```json
{
  "model": "exact-candidate-model-id",
  "nodes": ["studio", "pc3060"],
  "ready": true,
  "endpoint": "http://home-api:8080/v1",
  "max_context_tokens": 4096,
  "additional_memory_gib_by_node": {"studio": 1, "pc3060": 1},
  "measured_tokens_per_second": null
}
```

`additional_memory_gib_by_node` is incremental memory needed beyond current
allocations: include weights when loading cold, and context/scratch requirements
for the request when weights are already resident. Re-profile it when context,
quantization, concurrency, resident models or placement changes. A verified
endpoint must still be the one used by the host's local-model transport; this
module gates routes and reports telemetry, it does not rewrite the transport or
reserve GPU allocations atomically. There is a race between sampling and actual
allocation; the inference runtime remains the final memory authority.

Scarcity is the maximum of memory pressure and GPU utilization for a verified
deployment. High utilization re-ranks it without pretending a renewable quota was
spent. A missing node or insufficient memory makes that deployment unavailable.
A managed local reading expires with its node telemetry: `expires_at` is the
earliest sample time among the nodes a healthy deployment stands on, plus the
configured `max_age_s`, and never more than 120 seconds after the reading. The
router admits no local route past it. Background local refresh is due every 60
seconds independently of subscription-window readings. It never postpones them
(a local-only refresh keeps the previous full-sample stamp) and never waits for
them (it runs outside the vendor-probe sampler, which can hold its lock for up
to 240 seconds, and publishes through the same merge-on-write), so a `max_age_s`
under 60 leaves the lane unavailable between refreshes rather than admitting
stale capacity.

## Local models (the Pro)

Owner decision 2026-11-08, confirmed 2026-11-13 (#75): local chat models run on the **Pro** only, and only on AC power
with the manual switch on. The Studio runs S1 (scoring, compaction) and **no chat models**: its config has no
`ollama` block, its Ollama service and gate may stay installed but idle. A node with an `ollama` block offers its
Ollama models to the provider as `sub/<alias or tag>` pins (`cortados_router/provider/local_lane.py`) and, when its
`auto_route` is set, as an auto-route target (below), and, for a model with a published proxy index, as a candidate
in selection (Local models in selection, below). The provider reads the same file as the sampler,
`CORTADOS_LOCAL_RESOURCES_CONFIG`; `tables/resources.example.json` shows the Pro's block (placeholders).

| key | meaning |
| --- | --- |
| `base_url_env` | name of an env var holding the node's URL: its local gate, e.g. `http://<tailscale-ip>:11435` (wins over `base_url`) |
| `token_file_env` / `token_file` | env var naming, or the path of, the file holding the gate's token on the provider host (same rules as the gate: mode 0600, ≥ 32 chars); sent as `Authorization: Bearer` on the probe and every turn |
| `base_url` | the URL itself, for the git-ignored `tables/resources.json` only; never commit a real address |
| `label` | the suffix `/v1/models` shows: `<name> · CortadOS (<label>)` (default: the node id, capitalised) |
| `auto_route` | `false` (default): pin-only. Or `{"model": "<alias or tag of one of this node's models>", "bands": ["R0", …]}`: the S1 bands in which that model may take a new conversation (Local auto-route, below). There is no default band; `true`, an unknown model, an empty list, unknown keys and the bands `R3`/`R4` are refused with a config error |
| `index_route` | `true` (default) or `false`: whether this node's models with a published proxy index are offered in selection (Local models in selection, below); anything else is refused |
| `max_inflight` | positive integer, default 1: how many turns this provider is relaying to the node at once make it busy; a busy node is not offered by the index route (pins and the manual `auto_route` are unaffected) |
| `probe_timeout_s` / `connect_timeout_s` / `read_timeout_s` | `/api/tags` probe (2), connect (3), wait per response chunk (300; a first model load can take a minute); each must be positive |
| `models[]` | `id` (the Ollama tag, required), `alias` (the short pin name), `name` (display name), `context_length` (a positive integer of tokens; default 32768 when absent, the input-fit gate; set it to the node's Ollama context; the owner's decision is 128k, 131072, on the Pro); `thinking` (`"off-unless-asked"`, the default, or `"passthrough"`; anything else is refused: Thinking models, below); `efforts` (optional: the reasoning-effort levels the node accepts, a non-empty list from `low`, `medium`, `high`, `xhigh`, `max`; absent, the client's effort is dropped: Reasoning effort, below) |

Pin names (aliases and tags, case-insensitive) must be unique across nodes. Behaviour:

- Only on `/v1/messages` (Ollama implements the Anthropic Messages API there), only on a routing endpoint (not
  `--fixed-model`). The turn is relayed as-is (streaming or not, tools and `tool_use` blocks untouched); `model` is
  rewritten to the tag (and `thinking` set as in Thinking models, below), and the response names the pin the client
  asked for. The relay is stateless (every turn
  carries the whole conversation), but the conversation's model binding is kept in the provider's registry
  (`server.LocalSession`: the reply fingerprints, as for every pin), so a conversation keeps its model: a local
  conversation whose next turn names `sub/opus`, a `sub/opus` or S1-routed one whose next turn names a local pin, gets
  the same 409 "the model is fixed per conversation". A tool-less request (a title call) is a one-shot, not tracked.
- Routing goes through `routing.choose_pinned` (local branch `_choose_local`): no S1; the node must answer `/api/tags`
  within `probe_timeout_s` and list the tag; the request must fit `context_length` (else 413, checked again on every
  turn, keeping the history); `x-cortados-exclude-model` treats a local alias and its tag as one model; the conversation's
  opening request is logged as a `routes` row (engine `pin`, lane and provider `local-<node>`), continuations are not.
- Availability is the `/api/tags` answer; there is no quota. The Pro is the owner's laptop: asleep, closed, off the
  tailnet, busy, on battery or with the switch off (the gate's 503), it fails the pin with a 503 that says so. Nothing
  falls back to another model.
- Not listed in `served_routes`: S1 and `core.resolve` never see a local lane.

**Deploy (moving local chat models from the Studio to the Pro).** In this order; nothing here restarts the provider:

1. On the Air (the provider host), delete the Studio's `ollama` block from the live, git-ignored
   `tables/resources.json` (the file `CORTADOS_LOCAL_RESOURCES_CONFIG` names). No restart: `local_lane.nodes()` re-reads
   the file on every call, so the Studio's pins and lane disappear with the next request.
2. On the Studio, stop its gate and its Ollama (owner decision 2026-11-13: the Studio runs no chat models; S1 does not
   use either): `launchctl bootout gui/$(id -u)/com.cortados.local-gate`, then quit Ollama (the menu-bar app, or
   `launchctl bootout` its own job if it was installed as one). **Done 2026-11-13:** `com.cortados.local-gate` and
   `com.cortados.ollama` were booted out and disabled on the Studio.
3. On the Pro: nothing is downloaded (owner decision 2026-11-13: the lane uses the models already on the Pro, renamed
   with `ollama cp` to `Qwen27b_heretic`, `Huihui_Qwen_9b`, `Huihui_Qwen3.8` and `Phi_4`; the Pro block's `models` must
   list these tags as `ollama list` prints them, e.g. `Qwen27b_heretic:latest`). Set Ollama's context to at least the
   configured `context_length` (131072, 128k, owner decision):
   `OLLAMA_CONTEXT_LENGTH=131072` for `ollama serve`, or Ollama.app's context setting, then restart Ollama (see "Context
   length must match the node" below). Only then set the Pro block's `auto_route` in the Air's `tables/resources.json`
   (Local auto-route, below); before that the Pro is pin-only.

### The manual switch: three ways to flip it

The Pro admits local models only on AC power with its switch file (`$CORTADOS_HOME/local-enabled` on the Pro) present.
It can be flipped:

- on the Pro: `python3 -B -m cortados_router.local_resources toggle on|off|status`;
- from the dashboard's Live tab (**Local models** card, On/Off button; turning off with a model loaded asks first);
- from any Claude Code session: the `local_switch` MCP tool (`cortados-route`) or `/cortados-local on|off|status` (the
  `cortados-routed-subagents` mod).

The last two go provider → gate: `GET /v1/cortados/local` and `POST /v1/cortados/local/switch {"node", "on"}` on the
provider (owner token; `provider/local_switch.py`), which call the node's gate at `GET`/`POST /cortados/local-switch`
with the node's gate token (`token_file_env` above; a node without one is refused with a 409). On the node the gate
writes or removes the switch file through `local_resources.set_local_switch` (atomic: a private temp file renamed into
place; off is one unlink), the same function the CLI uses. The status endpoint answers whatever the switch and power
say (it is how the state is seen); `/api/tags` and `/v1/messages` keep their 503 while off or on battery. The status
carries the switch, `allowed` + reason, `power` (`ac`/`battery`/`unknown`), whether an S1 training hold is live, and the
names (only) of the models Ollama has loaded (`/api/ps` on loopback, 2 s bound). `/api/ps` itself is not forwarded.

Installing the new gate needs it restarted on the Pro (`launchctl kickstart -k gui/$(id -u)/com.cortados.local-gate`)
and the provider restarted on the Air; the owner/orchestrator does both. Until then the provider reports the Pro's gate
as an older one (502).

**Model ids and tags.** Write each model's `id` exactly as `ollama list` prints it, tag included
(`Qwen27b_heretic:latest`). The provider also treats `name` and `name:latest` as the same model
(`local_lane.normal_tag` / `local_lane.lists`, the one comparison behind "does not list"), because Ollama does; a
different tag (`name:q8`) is a different model.

### Local auto-route

Superseded by the published proxy index (Local models in selection, below) for any model that has one; it keeps
working for a config that sets it, and is checked first. With `auto_route` set, `routing.auto_local` runs after S1 has routed a new `cortados` conversation (the routed path
only; pins are unaffected). It moves the conversation to the node's `auto_route.model` when **all** of these hold, and
otherwise leaves S1's route exactly as it was:

1. the shared gate `routing.local_admission` admits S1's decision (Sensitive requests, below): not degraded (a degraded
   decision has no band of its own; the "degraded scorer never defaults to Claude" rule is untouched because nothing
   here ever adds a Claude route), `outward` exactly false and `blast_radius` exactly 0 or 1;
2. S1's band is in `auto_route.bands` (`R0`/`R1`/`R2` only: `R3` is where the hard rules put outward and
   blast-radius-3 work, `R4` is escalation only);
3. the request came in on `/v1/messages` (the relay speaks the Messages API; `/v1/responses` bodies carry
   `routing.NO_LOCAL_KEY`);
4. the client did not exclude the model (`x-cortados-exclude-model`, by alias or tag);
5. the node answers `GET /api/tags` within `probe_timeout_s` and lists the model. On the Pro the gate answers that
   probe with a 503 while on battery or with the switch off, so this one check covers AC power, the manual switch
   and gate/Ollama health. A failed probe is remembered for `routing.AUTO_DOWN_S` (30 s) so an asleep Pro does not add
   the probe wait to every new conversation; turning the switch on takes effect within that window;
6. the request fits the model's `context_length`.

The first node in config order that passes wins. The routes row S1's select wrote is rewritten under the same id
(`INSERT OR REPLACE`: lane/provider `local-<node>`, the model's tag, S1's own choice as `runner_up`, S1's runner_up kept
as an entry in `skipped`, a `local auto-route: …` hard-rules entry), and the conversation then runs exactly like a local
pin (same relay, same per-turn context gate, the model is fixed for the conversation, the client's quota floor does not
apply), except that every later turn is rescored by S1 first and refused when it is not admitted (Continuations of a
local-bound conversation, under Sensitive requests). Each skipped condition is logged at INFO (`local auto-route: … not taken …`), except a gate 401 (the provider's
token file does not match the gate's), which is a WARNING: it is a misconfiguration for the owner, not the node's state.

**An auto-routed conversation stays on the Pro until it returns.** The decision is made once, on the opening turn. If
the Pro then becomes unavailable (asleep, off the tailnet, on battery, switch off, gate refusing the token), the next
turn fails with a 503 worded for a routed conversation, not a pin: "this conversation was auto-routed by S1 to
`<model>` on `<node>`; the node is `<reason>`; start a new conversation (it is routed again) or send
`x-cortados-exclude-model: <alias>`". The conversation keeps its history and continues when the node is back. It is
**not** re-routed mid-conversation to another lane: whether to do that is an owner decision still pending, so nothing
falls back silently. (A pinned conversation keeps the pin wording: "local node `<node>` unreachable for
`sub/<alias>` … No other model was tried".)

**Parallel conversations share one model.** With `OLLAMA_NUM_PARALLEL=1` (the Pro's setting) Ollama runs one request
at a time: parallel auto-routed openers (or several subagents) serialize on the one loaded model and wait in Ollama's
queue (`OLLAMA_MAX_QUEUE`), each up to `read_timeout_s` per chunk. They are not refused and not spread to other lanes,
so many parallel local conversations are slower than the same number on S1's lanes.

Model choice for the Pro (M5 Max, 48 GB unified, reserve 6 GiB; `OLLAMA_NUM_PARALLEL=1`): one auto-route model loaded
at a time. The lane uses the four models already on the Pro (owner decision 2026-11-13: nothing is downloaded; the
tags below are the names given with `ollama cp`):

| model | Ollama tag | alias (example config) | size | use |
| --- | --- | --- | --- | --- |
| Qwen3.8 27B Heretic, Q4_K_M | `Qwen27b_heretic` | `qwen-27b` | 16 GB | in selection by its published proxy index (`qwen-27b`, `qwen-27b-think`) |
| Huihui Qwythos 9B, Q6_K | `Huihui_Qwen_9b` | `qwythos-9b` | 7.6 GB | pin-only (base model not identified; could be a manual `auto_route` model) |
| Huihui Qwen3.8 Abliterated | `Huihui_Qwen3.8` | `qwen-abliterated` | 17 GB | pin-only |
| Phi-4 Heretic, Q4_K_M | `Phi_4` | `phi-4` | 9.1 GB | pin-only |

All four are community fine-tunes without a published evaluation, which is one more reason to start auto-route on
reflex work only. Recommended first setting:
`"auto_route": {"model": "qwen-27b", "bands": ["R0"]}` (reflex work only: D ≤ 2, blast radius 0, small context);
widen to `R1` only after reviewing the `local auto-route` rows in router.db. Which bands go local is the owner's call,
so the shipped example keeps `auto_route: false`. (Owner decision 2026-11-13: the published proxy index below replaces
this manual rule for `Qwen27b_heretic`.)

### Local models in selection (published proxy index)

Owner decision 2026-11-13 (branch `local-published-index`): a local model is placed by the same rule as a cloud model,
an index per use case that must clear the request's required index, instead of a hand-picked band list. A local
model has no published scores of its own, so it borrows its **published base model's** index, minus a deduction:

- **Declared** in `tables/local-models.json` → `index_proxies.variants`: per variant the Ollama tag (`id`, matched
  case-insensitively, `:latest` optional), `thinking` (`off` / `on`), `published` (the base model's name exactly as in
  `tables/benchmarks/scores.csv`) and `deduct` (a fraction). `not_mapped` records why the other models have none.
- **Built** by `tables/benchmarks/build_tiers.py` (nightly, or by hand; never hand-edit the generated file) into
  `tables/model-scores.generated.json` → `local_models`: per use case `index = base family index × (1 − deduct)`, with
  `base_index`, `published`, `published_family`, `proxy: true` and `provenance` ("proxy: published base model, −10%").
  A base the data does not know (or with fewer than `tiering.min_benchmarks` benchmarks), or a malformed entry, is never
  given an index: it is listed in `local_unranked` with the reason and printed, and the cloud build goes on. Local
  variants are kept out of `models`, so no cloud consumer (the band references, `served_routes`, reviews) sees them.
  `--keep-sentiment` rebuilds from the committed `community-sentiment.json` (the claims fold keeps a 30-day window by
  the clock, so a rebuild on a later day would otherwise also move cloud indexes).
- **Measured results replace the proxy later** (plugged-in benchmarking, a separate plan).

Today only `Qwen27b_heretic` has one (owner: its copy is Q4_K_M and abliterated, so −10 %), as two variants of the
same weights chosen per request. Artificial Analysis publishes both modes of the base model; the index is the
builder's fit over every source in `scores.csv` for that family (Qwen3.8 27B reasoning has 32 benchmarks across
Artificial Analysis, LiveBench, LMArena and Epoch; the non-reasoning entry has Artificial Analysis's 3 only, so the
fit's ridge shrinks it toward the average model):

| variant | thinking | published base | AA intelligence / coding | general | code | ops | research | planning | writing | conversation | capture/log |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `qwen-27b` | off | `qwen3-8-27b-non-reasoning` | 20.2 / 44.6 | 16.7 | 15.1 | 15.0 | 19.6 | 16.3 | 17.6 | 20.5 | 19.1 |
| `qwen-27b-think` | on | `qwen3-8-27b` | 33.7 / 68.1 | 38.8 | 39.6 | 31.7 | 43.3 | 35.8 | 46.3 | 33.9 | 44.6 |
| (cloud) `gpt-5-6-luna` | | | 37.3 / 71.4 | 50.2 | 49.9 | 51.3 | 47.5 | 44.8 | 34.3 | 44.4 | 48.3 |
| (cloud) `gpt-6-luna` | | | | 47.4 | 44.9 | 50.8 | 45.0 | 43.9 | 21.9 | 40.5 | 38.4 |
| (cloud) `claude-haiku-4-5` (R1 reference) | | | | 13.3 | 14.2 | 13.8 | 11.0 | 6.1 | 18.2 | 19.0 | 11.5 |

(Luna's family index takes its best published configuration, so its −low/−high entries do not appear separately.)
With the knots of `routing-table.json` (version as committed), that places them at:

| use case | `qwen-27b` | `qwen-27b-think` (max_tokens ≥ 4096) |
| --- | --- | --- |
| code, ops, research, planning, conversation, capture/log | R0; R1 at D ≤ 3 | R0; R1 at D ≤ 5 (ops, planning, conversation: D ≤ 4) |
| writing | R0 | R0; R1 at D ≤ 5; R2 at D ≤ 6 (R2's floor, 42.3, is below its 46.3) |

Nothing else: every other R2 floor (the Sonnet 5 / GPT-5.6 Terra reference, 46.6–50.8) is above both, and R3/R4 are
never local.

**How a request is placed** (`routing.index_local`, run by `auto_local` after S1 has routed a new `cortados`
conversation and the manual `auto_route` did not take it). S1 and `core.resolve` still never see a local lane; the
provider compares the variants against the route S1 chose:

1. Hard rules first: never when S1 was degraded, never outward work, never blast radius 3, never R3/R4; never on
   `/v1/responses`; a route without a required index (the table-fallback path) has nothing to clear.
2. A variant qualifies when its index for S1's use case is at least S1's `required_index`. The think variant also needs
   `max_tokens ≥ local_lane.THINK_MIN_MAX_TOKENS` (4096). Reason (#108): Qwen3.8 27B thinking spent all of
   `max_tokens=20` and wrote no answer, thinking counts against `max_tokens`, Anthropic's extended thinking needs a
   budget ≥ 1024 and below `max_tokens`, and the think index comes from published reasoning runs with large budgets.
   4096 splits into ≥ 2048 for thinking and ≥ 2048 for the answer. A client that asked for thinking itself gets only
   the think variant (its turn thinks either way). A node entry with `thinking: "passthrough"` takes no variant (its
   thinking cannot be set).
3. Cost: a local lane spends no subscription quota (`core.resolve` already weighs local at 0, and its face floor
   exempts local), so a qualifying variant wins over S1's cloud choice; the lighter one first (lower general index,
   the selection's "pay by general index": the non-thinking variant, which also answers sooner). This is the owner's
   rule ("the local lane wins where it qualifies unless the node is busy"); it also takes precedence over a cloud
   window whose credit is about to expire unused.
4. It is not taken when its node has `index_route: false`, is **busy** (this provider already relays `max_inflight`
   turns to it; Ollama runs one request per loaded model on the Pro, so another would queue for minutes while a cloud
   lane answers now), failed a probe within `AUTO_DOWN_S`, does not answer `/api/tags` (asleep, battery, switch off,
   gate down), does not list the model, the request outgrows `context_length`, or the client excluded the model
   (`x-cortados-exclude-model` by alias or tag; both variants are one model). S1's route then stands; the skip is
   logged at INFO (`local index route: … not taken …`, node, variant and reason only, never the request).

When taken, the routes row is rewritten under the same id as for the manual rule, with the variant, its index,
use case and provenance on `chosen` and a `local index route: …` hard-rules entry. Each turn of a think-variant
conversation is sent `"thinking": {"type": "enabled", "budget_tokens": max_tokens // 2}`, or disabled when that turn's
`max_tokens` is below 4096; the non-thinking variant is sent thinking disabled; a client's own `thinking` passes
through. The #108 safety net (a thinking-only reply is an error, not an empty answer) still applies to both.

### Sensitive requests

The Pro's local models are uncensored ("abliterated") fine-tunes the owner runs himself, so work that cloud models
refuse, red-teaming and sensitive topics may run there (owner decision 2026-11-13). A node opts in with a `sensitive`
section in its `ollama` block; absent, the feature is off on that node:

```json
"sensitive": {"model": "qwen-27b",
              "keywords_file": "~/.config/cortados/sensitive-keywords.txt",
              "refusal_fallback": true}
```

`model` names one of the node's models (alias or tag). `keywords_file` is the keyword list's path on the provider host;
`keywords_file_env` instead names an environment variable holding the path (it wins when set; named but unset, the
keyword trigger logs a warning and matches nothing). `refusal_fallback` defaults to false. Unknown keys and wrong types
are refused (`LocalConfigError`), as for `auto_route`.

**Three triggers** (`routing.sensitive_local`, `routing.refusal_route`):

1. **Explicit flag.** The request header `x-cortados-local: sensitive` (or `prefer`, "local if eligible": the same
   rules, recorded as `local: prefer`), honoured on `/v1/messages` and `/v1/responses` and read for a new conversation
   only. Any other value is a 400. The MCP tools send it: `route_task(…, local="sensitive")`, and
   `start_task(…, local="sensitive")` makes every request of the child carry it (not with `chrome: true`: login mode is
   not routed). A `sub/` pin ignores it (the person chose the model). A Claude Code session-wide toggle
   (`/cortados-local prefer`) is a follow-up after branch `local-switch-remote`.
2. **Keyword list.** One term or phrase per line, `#` comments and blank lines skipped (format:
   `tables/sensitive-keywords.example.txt`). Matched case-insensitively (casefold) as whole words or phrases: a term
   never matches inside a longer word, and a phrase's words may be separated by any whitespace. Only the **latest user
   turn's text** is matched: client reminders (`<system-reminder>`) are removed first, tool results are not the person's
   words, and the **system prompt is not matched**, because it is the client's fixed instructions plus project files
   (CLAUDE.md), the same for every conversation of a project: one word there would send every conversation local.
3. **Refusal fallback** (`refusal_fallback: true`). When the cloud model refuses the **opening turn** of an S1-routed
   conversation on `/v1/messages`, the provider asks the node for a whole answer to the same request, sends the client
   only that answer, and binds the conversation to the local model (later turns relay there after the continuation
   re-check below). The
   Claude bridge recognises a refusal from the Claude CLI's own markers: an assistant frame with `stop_reason:
   "refusal"` or the `model_refusal_no_fallback` system event (the bridge passes no `--fallback-model`). The Codex
   app-server has no refusal signal (a refused Codex turn completes like any other, with text), and guessing from the
   text is not reliable, so Codex lanes have no refusal fallback. Not re-run, ever: a turn after client tools ran or
   were requested (the request carries an assistant message or a tool result, or the refused reply holds a
   `tool_use`), so a tool's effect never repeats. A refusal later in a conversation does not move it (hook
   `HOOK(rebuild-on-409)` in `server.Handler._refusal_fallback`; follow-up once branch `rebuild-on-409`, which seeds a new
   backend from the client's transcript, merges). If the node is unavailable or its answer fails, the refusal stands
   and reaches the client unchanged. An answer counts only when it holds a non-blank text block or a valid tool call (a
   tool the request declared, with an object input): a thinking-only reply, an empty `content` (e.g. `stop_reason:
   "max_tokens"` after thinking) or only malformed tool calls is a failed answer, so the original refusal stands and
   nothing is bound or recorded locally (`local_lane.complete`).

**Which variant runs.** When the sensitive model has published-index variants (today `qwen-27b` and `qwen-27b-think`,
"Local models in selection" above), a sensitive move (flag, prefer, keyword, refusal) picks one per request with the
index route's own per-request rules (`routing.choose_variant`, sharing `_variant_why` with `index_local`): a node entry
with `thinking: "passthrough"` takes none; a client that asked for thinking gets only the think variant; the think
variant needs `max_tokens ≥ 4096`. A sensitive request runs local in any band, so the variant need not clear S1's
required index: of the eligible ones it takes the lighter one that clears it (what the index route would take), else
the strongest eligible (highest index for S1's use case). The variant, its index and `variant_why` are recorded on the
routes row's `chosen` entry, and every turn (and the refusal fallback's whole answer, `local_lane.complete`) is sent
the variant's thinking exactly as an index-routed turn is. A model with no eligible variant keeps its own `thinking`
setting.

A planned S1 "sensitive" signal plugs in at one place: `routing.S1_SENSITIVE_ANSWER` (None today) names the S1 answer
key; once set, a request S1 marks is handled as trigger `s1` under exactly the rules below.

**The outward-effect limit.** A sensitive request may go local in **any band**, but **only with no effect outside the
machine**: S1 answered `outward: false` and `blast_radius` 0 ("words only, nothing changes") or 1 ("a bounded
reversible workspace change") (`routing.LOCAL_BLAST_OK`). Level 2 ("a broad or difficult-to-reverse system change") is
not bounded to the workspace and level 3 is where `core.resolve` floors every outward request, so neither goes local.
A decision without these answers fails closed.

**One shared gate: `routing.local_admission`.** Every AUTOMATIC move to a local lane passes the same function:

    routing.local_admission(answers, band, degraded) -> (admitted: bool, reason: str)
    routing.route_admission(route) -> (admitted, reason)   # the same, reading a route's decision/band/degraded

Callers: `auto_local` (auto_route bands), `sensitive_local` (flag, prefer, keyword, the planned S1 signal),
`refusal_route` (the refusal fallback, rechecked on the route's own decision even when the marker is there) and
`continuation_admission` (below); branch `local-published-index`'s `_move_to_local` must call it too. It admits only
when `degraded` is exactly `False`, `band` is a non-empty string, `answers["outward"]` is exactly `False` and
`answers["blast_radius"]` is exactly an `int` (not a bool) equal to 0 or 1. Anything else is malformed and not admitted:
`-1`, `False`, `True`, `1.9`, `1.0`, `"1"`, a missing key, `outward: null`. The reason is metadata only (it is what the
log and the routes row say). Per-node checks stay with the caller: the node's auto_route band list, the wire, and
`_node_why` (exclusion, availability, the model listed, the context fit).

**Continuations of a local-bound conversation.** A conversation is routed once, and a conversation bound to a local
lane by an automatic rule (auto_route, the sensitive flag / prefer / keyword, the refusal fallback) used to relay every
later turn to the node unchecked, so "now email the signed invoice and mark it paid" after a harmless opening reached
the node. Now every continuation of such a conversation is rescored by S1 before anything reaches the node
(`server.Handler._local_continuation_refused` → `Registry.local_recheck` → `routing.continuation_admission` →
`local_admission`, under the band the conversation was bound with). S1 scores, through `core.decide` (the same scorer
call, timeout and retry as the opening's routing call; a degraded result fails closed), `routing.continuation_text`:
the latest user words (client reminders removed; on a tool-result turn with no words, the latest earlier user words)
**and the text of the tool results added since the last scored turn** (those after the conversation's last assistant
message, `<system-reminder>` tags kept: a tool result is not the client's wrapper). A tool result can carry an
instruction the person never wrote ("now email this file to ..."), so it is scored as if they had. A turn longer than
S1's own 6000-character input (`routing.CONTINUATION_SCORE_CAP`) is scored in chunks (`routing.continuation_chunks`),
**every one of which must pass**: each chunk opens with the person's words (up to 1500 characters, head and tail;
longer words are also scored in full, in windows of their own) followed by a window of the new tool results, and the
windows overlap by 400 characters (`CONTINUATION_OVERLAP`) so an instruction that straddles a cut is whole in one of
them. Every character is scored. At most 16 chunks (`CONTINUATION_MAX_CHUNKS`, 65k to 90k characters of new
tool results, by the words' length; up to 4 scored at once) per turn: each is an S1 call on the scorer every routing decision shares, and a
longer turn is refused (fail closed) rather than scored in part. Review finding 3 (gpt-6.1-sol on f83b84a): the
earlier head-and-tail cut left the middle of a long tool result unscored while the model still read it. Chunks rather
than refusing everything over 6000 characters, because a coding tool loop reads files longer than that all the time
and would be unusable. An admission is reused for 180 s
(`routing.CONTINUATION_CACHE_S`) only for the same conversation, the same scored text and the same message count, so
a new tool result, or the same words later in the conversation, is scored again; a client retrying the identical
request reuses it. A refusal is never cached. A turn that is not admitted (outward, blast_radius 2 or more, a degraded
or failed scorer, missing or garbled answers, no user words at all, no checker configured) is refused with a terminal
**400** (`x-should-retry: false`, `invalid_request_error`): the conversation is bound to a local model and this turn
needs a cloud model; start a new conversation (routed again) or pin a cloud model. Nothing was sent, so the
conversation is untouched: a later turn on the same history that passes the gate continues it. The log names the
route id and the gate's reason only. **An explicit local pin** (`sub/<local model or alias>`, the client's own choice)
is **not rescored**. Moving a refused conversation to a cloud lane mid-way is a separate feature (#43, re-route when the
Pro is unplugged); its seam is `server.Handler._local_continuation_refused`. `core.resolve`'s hard rule (outward
effects floor the blast radius to 3, strong cloud models) is untouched: an outward request is never moved, whatever
the trigger.

**Effect boundary.** Scoring judges what the person (and the tools) said, not what the local model then does: asked to
"draft a message locally; do not send", a model may still answer with a `send_email` call. So every turn an automatic
rule sends to a local model (auto_route, flag, prefer, keyword, the refusal fallback, and the published index route:
all relay through `local_lane.serve(auto_routed=True, bind_reason=...)` or, for the refusal fallback,
`local_lane.complete(..., boundary=True, bind_reason="refusal")`) passes `cortados_router/provider/effect_boundary.py`.

*Profiles, split by reason* (owner decision, Forest 2026-11-14). The route records WHICH rule bound the conversation
local (`routing._moved(..., reason=...)` → the route's `bind_reason`); the route is the conversation's, so every
continuation (and a rebuilt session) carries the same reason and gets the same profile. `effect_boundary.BIND_PROFILES`
maps it; a missing or unknown reason gets the strictest profile. The published-score index route
(`routing._move_to_local`) passes `reason="index"` to `_moved` (`"index": COST`). `prefer` is a cost
route (review of 0961c80): `x-cortados-local: prefer` asks for local because it is cheaper, not because the work is
sensitive.

| Bind reason | Profile | Tools offered and allowed | File writes (Write, Edit, MultiEdit, NotebookEdit) |
|---|---|---|---|
| `sensitive_flag`, `keyword`, `refusal` | sensitive | Read, Glob, Grep, LS, NotebookRead, Edit, MultiEdit, Write, NotebookEdit, TodoWrite, ExitPlanMode. **No Bash family** (Bash, BashOutput, KillShell, KillBash are not offered, and an emitted one is blocked). **Without `x-cortados-boundary-hook: 1`: read-only** (Edit, MultiEdit, Write, NotebookEdit not offered; an emitted one is blocked, rule `write-no-hook`) | inside the project folder; every `PROTECTED_PATHS` entry refused, `package.json` / `pyproject.toml` / `Makefile` included; checked again on the client by the boundary hook |
| `prefer`, `auto_route`, `index` | cost | the same, plus Bash, BashOutput, KillShell, KillBash; every Bash command checked against `OUTWARD_BASH_RULES` and `OUTWARD_BASH_TEXT_RULES` (**best-effort**, below) | inside the project folder; `PROTECTED_PATHS` refused except `package.json`, `pyproject.toml`, `Makefile` (normal coding targets) |
| unknown / missing | sensitive | as sensitive | as sensitive |
| explicit `sub/` pin | none | untouched: every tool, no inspection | untouched |

*The project folder* (`effect_boundary.project_dir`) is the working directory Claude Code names in the request's
`system` prompt: `Primary working directory: <path>` (2.x environment block) or `Working directory: <path>` (the older
`<env>` block). Only `system` is read, never the messages or tool results, so neither the model nor a fetched page can
move it. It is unknown, and **every write of the turn is blocked** (fail closed), when no such line is there, two
different ones are, or the path is relative, holds `..`, or is `/`, `/tmp`, `/Users`, `/home` or a home directory
itself.

*Path rules* (`effect_boundary.blocked_write`). The target is normalised (`.` and `//` removed; a relative path is
taken from the project folder) and must lie strictly inside the folder (compared case-sensitively, so a differently
cased folder on a case-sensitive disk is outside). Refused: any `..` component, a path starting with `~` or holding
`$` (not resolved here, so not provably inside), and a symlink the provider's own filesystem shows leading out of the
folder or onto a protected name (`os.path.realpath`; a symlink that only exists on the client's machine cannot be seen
here: the client-side boundary hook, docs/BOUNDARY-HOOK.md, resolves the path there; see
the honest limits). `PROTECTED_PATHS`, matched case-insensitively on the path below the folder, each read and acted on
later by something other than the turn:

| Kind | Entries |
|---|---|
| any component | `.git` (hooks, config; a submodule's too), `.husky`, `.claude`, `.ssh` |
| any component pair | `.github/workflows`, `Library/LaunchAgents`, `Library/LaunchDaemons` |
| any file name | `.gitlab-ci.yml`, `.pre-commit-config.yaml`, `.envrc`, `.mcp.json`, `setup.py`, `setup.cfg`, shell startup files (`.zshrc`, `.zshenv`, `.zprofile`, `.zlogin`, `.bashrc`, `.bash_profile`, `.profile`, ...), `crontab` |
| build files | `package.json`, `pyproject.toml`, `Makefile` / `GNUmakefile`: refused on sensitive routes, allowed on cost routes |
| the project root | any dotfile or dot-directory directly in the folder |

Outside the folder every write is refused anyway (`~/Library/LaunchAgents`, `~/.ssh`, `~/.zshrc`, the system crontab).
A blocked write takes the same terminal 400 / stream `error` path as a blocked Bash call; the log names the rule id
(`write-outside-project`, `write-dotdot`, `write-protected`, `write-symlink`, `write-no-project`, `write-malformed`) and
the tool name, never the path.

Two layers apply on every automatic-local turn:

1. *Tool list.* Before the request reaches the node, its declared `tools` are cut to the profile's tools (an
   allowlist: a name it does not know is dropped), and a `tool_choice` naming a dropped tool is removed. The model
   cannot name what it is not offered. Always dropped:

   | Dropped (everything not in the profile, including) |
   |---|
   | `WebFetch`, `WebSearch` (network) |
   | every `mcp__*` tool (connectors: mail, chat, drive, payments, ...) |
   | `Task` / `Agent` (a subagent is routed afresh, possibly to a cloud model with every tool) |
   | `Skill`, `SlashCommand`, any typed server tool (`web_search_*`, `bash_*`), any unknown name |
   | on a sensitive route: `Bash`, `BashOutput`, `KillShell`, `KillBash` |

2. *Emitted calls.* Every tool call in the local reply is inspected before the client sees it: a call to a tool off
   the profile (defence in depth), a file write the path rules refuse, a malformed `Bash` input, or (cost routes) a
   `Bash` command one of whose simple commands (the line is cut at `; & | && || ( $( \` ` newlines and quotes,
   wrappers such as `sudo`, `env`, `xargs`, `timeout 30` skipped) matches `OUTWARD_BASH_RULES`, or whose whole text
   matches `OUTWARD_BASH_TEXT_RULES` or the interpreter check:

   | Rule | Matches |
   |---|---|
   | `curl-upload` | `curl` with `-X`/`--request`, `-d`/`--data*`, `-F`/`--form*`, `-T`/`--upload-file`, `--json` (combined short flags too) |
   | `wget-upload` | `wget --post-data/--post-file/--body-data/--body-file/--method` |
   | `ssh` | `ssh`, `scp`, `sftp`, `mosh`, `autossh` |
   | `netcat` | `nc`, `ncat`, `netcat`, `socat`, `telnet`, `ftp`, `lftp` |
   | `rsync-remote` | `rsync` with a `host:` / `host::` / `rsync://` argument |
   | `git-push` | `git ... push`, `send-email`, `request-pull` |
   | `gh-write` | `gh` with create, edit, merge, comment, close, delete, release, reopen, review, ready, transfer, lock, unlock, fork, rename, (un)archive, run, rerun, cancel, set, upload, sync, enable, disable, (un)pin, develop, add, remove, login, logout |
   | `gh-api-write` | `gh api` with `-X`, `--method`, `-f`, `-F`, `--field`, `--raw-field`, `--input` |
   | `publish` | `npm`/`pnpm`/`yarn`/`bun`/`cargo`/`poetry`/`flit`/`hatch`/`uv`/`pip` publish, unpublish or upload; `twine upload` (also `python -m twine upload`); `gem`/`docker`/`podman`/`helm push` |
   | `osascript` | any `osascript` (AppleScript drives Mail, Messages and every scriptable app) |
   | `open-url-scheme` | `open` / `xdg-open` on `mailto:`, `sms:`, `imessage:`, `facetime:`, `tel:`, `message:` |
   | `mail` | `sendmail`, `mail`, `mailx`, `mutt`, `msmtp`, `ssmtp` (also at the end of a pipe) |
   | `git-config` | `git config` (any write: aliases, `core.hooksPath`; `--get`/`--list` pass) and `git -c` |
   | `persistence` | `launchctl`, `crontab`, `at`, `batch` |
   | `symlink` | `ln -s` / `--symbolic` (a link turns a project write into an outside one) |
   | `pipe-to-shell` | a pipe into `sh`, `bash`, `zsh`, `python`, `node`, `perl`, `ruby`, `php`, `osascript`, `source` (`... \| base64 -d \| sh`, `curl ... \| bash`) |
   | `protected-path` (text) | the command mentions `.git/hooks`, `.git/config`, LaunchAgents/LaunchDaemons, `.ssh`, a shell startup file or `crontab` |
   | `computed-command` (text) | `eval` of a `$(...)` or backtick substitution, `source <(...)` / `. <(...)` |
   | `interpreter-network` | a `python*`/`node`/`deno`/`bun`/`perl`/`ruby`/`php` command whose command line names a network or mail module (`socket`, `urllib`, `requests`, `http`, `https`, `smtplib`, `fetch`, `net`, `child_process`, ...), including a script written by a heredoc and run in the same command |

   A match blocks the whole reply; nothing of it reaches the client's tools. **Non-streaming:** the same terminal
   **400** as the continuation gate (`x-should-retry: false`, `invalid_request_error`, "the local model proposed an
   action with an outward effect; run this turn on a cloud model ..."). **Streaming:** the relay holds every tool-call
   block until its `content_block_stop` and judges the whole input (`input_json_delta` fragments included), and holds
   `message_start` until the first content it may show. A block found before anything was written is answered with that
   same JSON 400 (no stream starts). A block found after text was already streamed ends the stream with one
   `event: error` (`invalid_request_error`, the same message): the client keeps the text it was shown and gets no
   `tool_use`, no `message_delta` and no `message_stop`, so Claude Code shows an API error for the turn and runs no
   tool. Either way the conversation is not advanced (a blocked opener is not bound; a blocked continuation leaves the
   history as it was). The refusal fallback's local answer is judged the same way, and a blocked one leaves the cloud
   refusal standing. The log names the rule id and the tool name only, never the arguments.

   **An explicit `sub/` local pin** is the client's own choice: its tools are relayed untouched and its calls are not
   inspected.

**Honest limits.** On cost routes the Bash pattern lists are **best-effort**: they cannot recognise every outward
shell command. A script written in one turn and run in a later one, a Makefile target or npm script that sends (cost
routes may write `Makefile` and `package.json`), an alias or shell function the person already has (`git p` defined in
their own gitconfig), a command name in a variable, obfuscation the lists do not know, or a plain `curl` GET whose URL
carries data all pass. **Claude Code's own permission prompts are the last guard for what Bash runs**; sensitive
routes have no shell for exactly this reason. A false positive (a quoted message that reads like a command,
`git commit -m 'ssh fix'`, `cat ~/.zshrc`) refuses one turn, which the person reruns on a cloud model. The path rules
read the path's text: a symlink is seen only when it exists on the provider's own machine, and on a cost route Bash
could create one the same turn (`ln -s` is blocked, other ways are not). A write inside the project to a file that
some tool later executes and that is not in `PROTECTED_PATHS` (a test file, a script the person runs) can still stage
an effect; permission prompts remain the guard there too.

**Every local guard still applies.** A degraded scorer never routes local; the request must arrive on `/v1/messages`
(the relay speaks Messages only; a `/v1/responses` request is logged and stays on S1's route); the client's
`x-cortados-exclude-model` is honoured (an excluded sensitive model also turns the refusal fallback off); the node must
answer its probe (on the Pro: AC power + manual switch + gate up) and list the model; the request must fit the model's
`context_length`. A request that fails any check keeps S1's route, and the reason is logged; if a detector marked it,
the reply tells the owner so ("Sensitive requests answered in the cloud" below). When a request moves, the
routes row is rewritten under the same route_id as for auto-route, with a `sensitive: flag|keyword|refusal|s1` or
`local: prefer` hard-rules entry. The sensitive check runs before auto-route; a request it does not move still goes
through auto-route as before.

**Privacy of the logs.** The provider log names only the rule (`sensitive: keyword`, `sensitive: flag`,
`sensitive: refusal`, `local: prefer`), the route id, the node/model and a metadata reason. It never logs which keyword
matched, the keyword list's contents or any request or reply text; an unreadable list is a warning naming the file only.
(The routes row keeps the request text exactly as for every routed request: that is S1's training record, unchanged.)

**Editing the keyword list on the Air.** The list lives on the provider host only (the repository is public;
`tables/sensitive-keywords.txt` is git-ignored in case one is placed there by mistake). Edit the file the config names,
e.g. `~/.config/cortados/sensitive-keywords.txt`, one term per line; keep it `chmod 600`. It is re-read when its
modification time or size changes, so no provider restart is needed. Check a change by sending a test request and
looking for `sensitive: keyword` in the provider log, or the hard-rules entry in router.db's routes table.

**Exposure and the local gate.** Ollama has no authentication: anyone who reaches its port can run, pull or delete
models. So Ollama listens on `127.0.0.1:11434` only (its default; leave `OLLAMA_HOST` unset), and the one door from the
tailnet is the token gate `cortados_router/local_gate.py`, run on the node:

- listens on the node's Tailscale address (`--listen <tailscale-ip>`, default port 11435), parsed as a numeric
  ASCII IP literal and bound in its canonical form, exactly as checked (hostnames, legacy forms like `0` and non-ASCII digits are
  refused); `0.0.0.0`/`::` is refused unless `--allow-any` (a laptop joins untrusted networks);
- forwards only `POST /v1/messages` and `GET /api/tags` to `http://127.0.0.1:11434`; every other path is a 404;
- requires `Authorization: Bearer <token>` or `x-api-key: <token>`, compared in constant time with the token in a
  0600 file (default `~/.config/cortados/local-gate-token`); it refuses to start when the file is missing, shorter than
  32 characters or readable by group/others. A wrong token is a fixed 401 that echoes nothing;
- does not cap concurrent connections by default (`--max-connections 0` / `CORTADOS_LOCAL_GATE_MAX_CONNECTIONS`,
  owner decision 2026-11-08): several orchestrating sessions may each run many local-model subagents at once, and
  Ollama's `OLLAMA_NUM_PARALLEL` (how many run) and `OLLAMA_MAX_QUEUE` (how many wait) are the real limits, so a
  request beyond `NUM_PARALLEL` queues inside Ollama instead of getting a 503 from the gate. A positive value caps
  (over it: an immediate 503) for an operator who wants a thread bound;
- streams SSE through line by line; bounds the body (32 MiB), the loopback
  connect (3 s), each read (300 s), an idle client socket (30 s) and, as absolute deadlines a trickling client cannot extend, the
  headers (10 s), the body (120 s) and the drain of a refused body (5 s, then closed without the 401); every timeout and
  size limit must be a finite positive number; logs method, path and status only, never the token or a body.

**Power and switch check on every node.** Before forwarding, the gate calls `pro_local_allowed()` (AC power + the
local-enabled flag). Despite the name it runs on any node that runs the gate, the Studio included: a Studio on AC with
the flag on passes it; turning the flag off takes that node's lane down.

**S1 training hold (Studio).** The guarantee is a kernel file lock on `$CORTADOS_HOME/studio-gate/node.lock`
(created 0600; `studio_capacity.node_lock_path`). Every forwarded gate request takes `flock(LOCK_SH|LOCK_NB)` before it
reaches Ollama and holds it until the response (streamed or not) is fully relayed or fails; `training_gate` takes
`flock(LOCK_EX)` before writing its intent, polling 1 s for at most 120 s while in-flight requests drain (then
`GateRefused`, retry later), and holds it for the whole training block. A gate request that finds the exclusive lock
gets a 503 ("S1 training in progress"; no path, pid or nonce); a lock file it cannot open or create is a 503 with a
logged warning (fail closed). The kernel drops a dead holder's lock, so there is no expiry or pid logic. The gate's
intent check is a read-only advisory fast path and never deletes an intent. Override the directory with
`--studio-gate-dir` / `CORTADOS_LOCAL_GATE_STUDIO_GATE_DIR`. Inside the lock, after the daemon's ack or its timeout,
`training_gate` unloads EVERY model `/api/ps` lists (`keep_alive=0` each) and refuses to start if any remains or the
reply is malformed. Writer starvation is accepted: a steady stream of overlapping gate requests can keep LOCK_EX from ever being granted, in which case the trainer refuses after the 120 s cap and retries on its next run. The gate directory must be a real directory (not a symlink) owned by the running user and not group/world-writable (a created one is chmod'ed 0700 regardless of umask), and `node.lock` is opened with `O_NOFOLLOW` and must be a regular file the user owns; otherwise the gate answers 503 and the trainer refuses. `gate` passes its LOCK_EX fd to the training command and runs it in its own process group: the lock stays held until every holder exits (a SIGKILLed wrapper does not release it), and on SIGTERM/SIGINT or any exit the whole group gets SIGTERM, 30 s grace, then SIGKILL before the lock is released.

**Thinking models (#108).** Ollama's `/v1/messages` lets a thinking model (the Pro's Qwen3.x models; `ollama show`
lists `thinking` among the capabilities) think whenever the request has **no** `thinking` field, and the thinking counts
against `max_tokens`. Observed on Ollama 0.40.16 with `Qwen27b_heretic`: "What is 6*7?" with `max_tokens: 20` and no
`thinking` came back as `content: [{"type": "thinking", …}]`, `stop_reason: "max_tokens"`, no text (streamed: one
thinking block, then `message_delta` with `stop_reason: "max_tokens"`); the same request with
`"thinking": {"type": "disabled"}` answered `[{"type": "text", "text": "42"}]`, `end_turn`, 3 output tokens (so did
`Huihui_Qwen_9b`), and `{"type": "enabled", "budget_tokens": 1024}` returned a thinking block then the text. Anthropic's
own default is the opposite (no field, no thinking), and clients send small `max_tokens` on exactly the reflex (R0)
requests auto-routed here, so each model entry's `thinking` says what the relay sends:

- `"off-unless-asked"` (default): a request without `thinking` (or with one that is not an object) is sent with
  `"thinking": {"type": "disabled"}`; a client's own `thinking` (enabled, adaptive or disabled) is passed through.
- `"passthrough"`: the request goes as the client sent it. For a model or Ollama build that rejects the field.

Safety net: when an **auto-routed** turn whose client did not ask for thinking still ends on `max_tokens` with no
non-blank text and no `tool_use` (only thinking, or empty), the client gets an error instead of a silent empty answer: a
502 `api_error` (streamed: the final `message_delta`/`message_stop` are replaced by an `event: error`) saying "this
request was auto-routed by S1 to `<model>` on `<node>`, which used the whole max_tokens (`<n>`) without writing an
answer (thinking only); retry with a larger max_tokens, or send `x-cortados-exclude-model: <alias>`". The turn counts as
not completed (an opening turn leaves no conversation; a later one keeps the history). It is not retried: thinking was
already asked off (or the model is set to `passthrough`), so the same request would fail the same way. A pinned model
(the person's choice) and a client that asked for thinking get the reply exactly as the node sent it. No prompt or reply
text is logged; the refusal record carries a fixed reason only.

**Reasoning effort (#122).** Claude Code sends `output_config: {"effort": "high"}` beside
`thinking: {"type": "adaptive"}`. Ollama's `/v1/messages` turns that effort into the chat request's think level
whenever `thinking` is neither `enabled` nor `disabled`; it rewrites `xhigh` to `high` first. The level reaches the
model's chat template as its reasoning effort, and a template that does not accept it fails the turn with HTTP 500. This
was observed 2026-11-15 under llama-server: `Unexpected reasoning effort high. Supported types are xhigh (default),
medium, and low.` So the relay (`local_lane._node_body`, on every turn, pins included) holds `output_config.effort` to
the model entry's `efforts`:

- absent (the default): the effort is dropped, and an `output_config` left empty is dropped too. The template keeps its
  own default, so no client value can fail the turn.
- declared: the client's level is sent when it is listed. Otherwise the relay sends the highest listed level below it,
  never more effort than the client asked, because the local node's time is the scarce resource. When no listed level
  is lower, it sends the lowest one. An unknown value is dropped.

Declare the levels as the node's API passes them on. Through Ollama, `xhigh` arrives as `high`, so the template above
is declared `["medium", "low"]`, and Claude Code's `high` (also `xhigh` and `max`) is sent as `medium`. `high→xhigh`
cannot be expressed through Ollama at all. The MLX backend is unchanged: an effort never becomes a
`chat_template_kwargs` key (only `enable_thinking` is allowed).

**A node error before any byte is routed elsewhere on a cost bind (#122).** Every other HTTP 5xx from the node is
handed back unanswered when all of these hold: it arrives before anything was written to the client, it is not the
gate's 529 busy answer (that keeps its retry), it answers the **opening turn** of a conversation that a COST-profile
rule moved here (`auto_route`, `index`, `prefer`: local because it is cheaper), and **no sensitivity detector marks the
request**. That last check is asked of the request itself, never inferred from the bind reason's name
(`routing.request_sensitivity`, decided once in `routing.choose` before any rule binds; the route carries
`cloud_reroute_ok: true` only when the flag, every node's keyword list and the S1 signal all cleared it; an unreadable
keyword list or local config is doubt, and doubt is not clearance). Sensitivity wins over preference everywhere:
`routing.sensitive_trigger` asks flag, keyword and S1 before `prefer` (`routing.TRIGGER_PRECEDENCE`), so a
keyword-sensitive request carrying `x-cortados-local: prefer` is bound `keyword`; and a request a detector marks that a
cost rule bound anyway (auto_route or index after the sensitive move was not taken) takes the detector's bind reason, so
the effect boundary gives it the sensitive profile (#122 round 2). The provider then routes the
conversation again, once, with that local model excluded, as for a never-fits turn (`local_lane.NODE_ERROR`, one of
`local_lane.REROUTE`, logged as a warning with the status only). These cases still answer the node's error as before,
and nothing leaves the machine:

- a request any sensitivity detector marks (flag, keyword, S1), or one a detector could not check, whatever rule bound
  it (`prefer` included);
- a SENSITIVE-profile bind (the sensitive flag, a keyword, a cloud refusal, an unknown reason; #109: sensitive work never
  goes outward by this path);
- a pin;
- a continuation;
- a stream that breaks after its first byte;
- a 4xx.

**Sensitive requests answered in the cloud: warn, then use the cloud (#124).** Owner decision 2026-11-15: a request a
sensitivity detector marked normally runs only on the Pro. When the Pro cannot take it, the request goes to the cloud
anyway, and the owner is told. The same rule covers private data sent while the Pro is away. The detectors are the
`x-cortados-local: sensitive` flag, the node keyword list, the S1 signal, a detector that could not be read
(`unknown`), and a cloud refusal whose fallback was not taken. One mechanism serves every path
(`provider/sensitive_notice.py`):

- **Routing** (`routing.choose`). A marked request that `sensitive_local` did not take stays on S1's cloud route.
  That route then carries `sensitive_cloud: {detector, reason}`, and the reason is the first node's skip as a
  category (`routing.why_category`): `switched_off`, `on_battery`, `unavailable` (no answer to the probe, or inside
  the `AUTO_DOWN_S` window, which keeps the reason of the probe that opened it), `busy`, `model_missing`,
  `too_large` (over the model's context or the gate's memory), `excluded`, `outward_effect`, `s1_degraded`,
  `not_admitted`, `responses_wire` (the local relay speaks Messages only, so a flagged `/v1/responses` request
  always lands here), `not_configured` (no node has a `sensitive` section) or `config_error`.
- **Gate refusals of a bound opening turn** (`local_lane.TOO_LARGE` / `UNSUPPORTED`). The re-route names the gate's
  outcome (`too_large`, `unsupported`), with the detector from the new route or from the one the local bind recorded
  (`sensitivity` on the local route).
- **Refusals** (`server.Handler._refusal_fallback`). A refused opening turn whose fallback was not taken leads with the
  notice under detector `cloud refusal`, with the node's skip reason, `no_answer`, `not_admitted`, `tools_ran` or
  `responses_wire`.
- **Refused continuations** (review round 2). A turn after the opening one is never moved (a turn with tool effects is
  never re-run, and moving a later turn is the unbuilt rebuild-on-409 follow-up), but when the cloud model refuses it
  the refusal detector has fired all the same: the refusal leads with its own notice under reason
  `refused_continuation`, and the route row records it unless the row already holds a record (the first detection
  stays; the log line names every one). Rows written before round 2 may hold `not_opening_turn`, still valid.
- **Unchanged (#122).** A node 5xx on a sensitive request is answered from the node and never re-routed, so it never
  reaches this path.

**What the client sees.** The opening turn carries the notice, because that is the turn whose routing kept the
request off the Pro; a later turn carries one only when the cloud model refused it. It comes as its own leading text
block, before the model's content:

    ⚠ CortadOS: this request was marked sensitive (<detector>) but the Pro couldn't take it (<reason>), so it was answered by <model> in the cloud.

A refused continuation reads instead:

    ⚠ CortadOS: <model> in the cloud refused this turn (marked sensitive: cloud refusal). The turn was not moved to the Pro (refusal after the opening turn): only a conversation's opening turn can be moved, so the refusal stands.

On `/v1/messages` it is content block 0, in both the JSON reply and SSE (start, delta and stop at index 0, so the
model's blocks follow from index 1). On `/v1/responses` it is a separate leading `message` output item (output_index
0). `tool_use` blocks, `function_call` items and the stop reason are the model's own, unchanged. The text names fixed
detector and reason labels and the routed model only, never a keyword, a matched term or request text. A value outside
the vocabulary is shown as the fallback label, never as itself.

**History.** The delivered form is the only form (review round 5; rounds 2 and 3 stripped the notice back out of
history, round 4 accepted recorded variant forms by content, and each rule broke: content-keyed provenance let an edit
to one turn pass as another turn's accepted form). The session's history holds the reply exactly as the client received
it, notice included, and every history comparison is plain equality against that delivered form, on both wires and for
the replay of a retried turn (which stores the reply as shown). A client that sends an earlier turn in any other form
(the notice merged into the model's text or dropped, for example by its own compaction) has rewritten its history, and
it is handled like any other rewritten history: the rebuild-on-rewrite path or the 409, with the same rebuild budget and
terminal behaviour, and no special case. So, as for any edited reply: when the changed turn is the request's last
reply, no live session wrote it and the request gets the existing terminal refusal; when a later reply is intact, the
conversation is rebuilt once from the lineage's budget. A rebuilt backend's transcript replays the client's text as
sent, with no labelling by wording (wording cannot tell model text from provider text): the rebuilt model may read the
notice as part of a past assistant turn. That is harmless and intended: the notice is true and is part of what the
user saw. Nothing is kept per injection, so no conversation state grows with notices. Two different notice-only replies
have different fingerprints (the notice text is in them).

**Opt-out.** A caller that must not get injected text (one parsing the reply as data) sends
`x-cortados-sensitive-notice: off` on the request. Only the block is suppressed: the warning log line and the route
row's record are still written, marked `opted_out`. Any other value, or no header, shows the notice.

**Record.** Each warned turn is:

- logged as a warning: `sensitive-cloud: route <id> marked by <detector> answered in the cloud by <provider>/<model>:
  the Pro did not take it (<reason>); notice shown|opted_out`;
- written to the route's router.db row as `sensitive_cloud` (JSON `{detector, reason, notice}`, written by
  `core.log_sensitive_cloud`; a nullable column added on open like the token columns; a lost write is counted like any
  route-log error, #99).

GET /v1/cortados/tasks publishes it per decision, re-validated against the vocabulary (anything else is dropped), and
counts it in `totals.sensitive_cloud`. The Live view's routed calls carry it, and the dashboard's Routed calls and
Decisions tables show it in a "sensitive in cloud" column.

**Not covered (by design).** These cases get no notice:

- a `sub/` or `api/` pin: the person chose the model, and `choose` ignores the flag for a pin;
- the web-search lane: a search is an outward call by nature, and the Pro has no search;
- a fixed-model endpoint (`--fixed-model`): no routing, so no detector runs;
- a later turn of a conversation already on a cloud model whose new words match a keyword: a conversation is routed
  once, and this turn did not go to the cloud because the Pro could not take it.

**Context length must match the node.** A model's `context_length` here is only the provider's input-fit gate; Ollama
uses the node's own `OLLAMA_CONTEXT_LENGTH` (or Ollama.app's setting). Set the node to the same value, or requests the
provider admits get truncated by Ollama. The gate does not check this at startup.

A launchd template is `integrations/local-gate/com.cortados.local-gate.plist` (RunAtLoad + KeepAlive; placeholders
only). Check a setup with `python3 -m cortados_router.local_gate --listen <tailscale-ip> --print-config` (validates the
token file and prints the settings, never the token). On the provider, a 401 from the gate is a 503 "local gate on
node <id> refused the provider token", with no fallback.

## MLX backend (vllm-mlx) and the activity governor

Ollama does not batch decode on Apple Silicon: on the Pro its aggregate output stays at ~27 tok/s whatever the slot
count. `vllm-mlx` (an MLX server with continuous batching) serves the same 27B several requests at once. Measured on
the Pro (48 GB), `vllm-mlx 0.6.0`, `~/models/mlx/Qwen3.8-27B-heretic-ara-4bit-bench` (15 GB, 4-bit), ~8.5k-token
prompts, 512 output tokens:

| Requests at once | Aggregate tok/s | Per request | TTFT | Makespan |
|---|---|---|---|---|
| 1 | 27.1 | 27.1 | 9.5 s | 28 s |
| 2 | 47.8 | 23.9 | 21 s | 42 s |
| 4 | 74.6 | 18.7 | 43 s | 70 s |
| 8 | 87.9 | 11.0 | 85 s | 131 s |
| 16 | 64.8 | worse | | |

Peak footprint 31 GB at 8; no swap growth.

### How it fits

Nothing changes for the provider's lane protocol. vllm-mlx 0.6.0 serves the Anthropic Messages API itself
(`POST /v1/messages`: text, `tool_use`/`tool_result`, streaming SSE, stop reasons, usage; read from its source), so
**no translator was built**: turns are relayed as they are today. The gate (`local_gate.py`) adapts the rest, per
backend, from a config file on the Pro (`cortados_router/local_backend.py`; example
`tables/local-backend.example.json`, real file `~/.config/cortados/local-backend.json`; no file = Ollama, no governor):

| Client route (unchanged) | Ollama upstream | vllm-mlx upstream |
|---|---|---|
| `GET /api/tags` | `GET /api/tags` | `GET /v1/models`, answered in `/api/tags` shape |
| `POST /v1/messages` | `POST /v1/messages` | `POST /v1/messages`, body adapted (below) |
| switch status "models loaded" | `GET /api/ps` | `GET /v1/models` |

Nothing else reaches either backend (`local_backend.UPSTREAM` is the whole table). The adaptation is in the gate, not
the lane, because only the gate knows its node's backend: the lane stays one Anthropic relay for every node and the
gate's client-facing routes do not grow. On `POST /v1/messages` to vllm-mlx the gate:

- maps thinking: vllm-mlx ignores Anthropic's `thinking`; the gate sets `chat_template_kwargs.enable_thinking` from it
  (`disabled` → false, any other type → true; absent → the server default, false in the example). The think variant
  (`qwen-27b-think`, max_tokens ≥ 4096) therefore thinks; vllm-mlx has no per-request thinking budget on this endpoint,
  so its `budget_tokens` is not enforced (max_tokens bounds thinking + answer; the lane's #108 guard still turns a
  turn that thought its whole budget away into a clear error);
- checks the model: vllm-mlx serves one model; the request must name it (`name` = `name:latest`) and is sent with the
  exact served name. Any other model (e.g. a `sub/phi-4` conversation started on Ollama) is a 404 at the gate, never
  silently answered by the MLX model;
- allowlists `chat_template_kwargs`: only `enable_thinking` (a boolean) passes; any other key, or a non-boolean
  value, is a `400 invalid_request_error` with `x-should-retry: false`. vllm-mlx 0.6.0 merges these kwargs into the
  template call (`engine/batched.py` `_apply_chat_template`: `template_kwargs.update(chat_template_kwargs)`), so a
  client could replace the template itself: `{"chat_template": "{{ 'a!' * 50000 }}"}` is a 155-byte body that renders
  a 100k-token prompt, which no bound read from the body can see. The server's own default
  (`vllm_mlx.serve.default_chat_template_kwargs`, emitted as `--default-chat-template-kwargs`) reaches the same
  template call, so it is held to the same allowlist: `local_backend.check()` refuses the config and `serve_argv()`
  re-checks before it emits the flag (round 6);
- refuses a body nested too deeply to parse or re-encode (Python's recursion limit, ~1,000 levels) with the same
  structured 400, instead of an uncaught `RecursionError` that dropped the connection;
- rewraps FastAPI's `{"detail": …}` errors in Anthropic's error shape. The backend prompt guard's refusal (below)
  becomes the gate's own never-fits 413.

### The governor (owner-approved policy) and token-aware memory admission

`cortados_router/local_governor.py`, run inside the gate on the Pro. Inputs, sampled at most every `sample_s` (5 s):
keyboard/mouse idle seconds (`ioreg -r -c IOHIDSystem -d 1`, `HIDIdleTime`) and macOS memory pressure (`sysctl -n
kern.memorystatus_vm_pressure_level`: 1 normal, 2 warn, 4 critical). A turn is admitted only when it passes **both**
bounds below; defaults, each with its reason, in `tables/local-backend.example.json` → `governor._reasons`.

**Count** (`policy()`): an upper bound on how many requests run at once, whatever their size.

| Owner | Memory pressure | Count limit (requests running at once) | Default (4-bit) |
|---|---|---|---|
| active (idle < `active_idle_s` 600 s) | normal | min(`active_cap`, model `idle_cap`) | 2 |
| idle (≥ 600 s) | normal | model `idle_cap` | 8 |
| any | warn | min(the row above, `warn_cap`) | 1 |
| any | critical | min(the row above, `critical_cap`) | 0 (nothing new) |
| unreadable idle | — | counted active | |
| — | unreadable | counted `unknown_pressure` | warn |

**Bytes** (`request_bytes()`): each turn reserves the memory its context will hold, and turns are admitted while the
reservations in flight fit beside the weights. The fixed "2 GB per request" cap is gone: it came from ~8.5k-token
benchmark prompts, and real Claude Code turns of 30k–130k tokens hold four to sixteen times that.

```
tokens   = min(input tokens + max_tokens, context_tokens)
reserved = tokens × kv_bytes_per_token + per_seq_state_bytes + prefill_margin_bytes
admit    iff  count < count limit  and  reserved in flight + reserved ≤ budget in force − weights_bytes
budget in force = active_memory_budget_bytes (owner active)  |  memory_budget_bytes (owner idle)
```

The budget follows the same owner signal as the count ("Active budget" below): 17.25 GiB beside the 4-bit weights
while the owner works, 25 GiB while away. A full 128k text turn (17.06 GiB since the 2026-11-22 KV correction) fits
either way.

**Since #126 this per-request formula is a floor on the served text-only model, not the reservation.** Its text turns
are reserved as one set, from the computed peak of the guard's chunked prefill and of the decode batch, which pads
every request's KV cache to the longest one. That peak is never less than this formula's sum. "Text prefill and the
text route's reservation (#126)" below has the formula, the numbers and what changed. A vision model's turns (#125)
still reserve per request, as here. The tables in this section are the per-request numbers: the floor on the text-only
model, and the reservation on a vision model.

**The input bound** (`local_governor.input_tokens`). A turn's input tokens are bounded from above, never estimated and
never asked of the backend:

```
input tokens ≤ bytes of the request body exactly as received
             + template allowance   1024 + 16 per message + 32 per content block + 64 per tool   (TEMPLATE_TOKENS_*)
             + re-serialized bytes  what vllm-mlx writes wider than it came (below)
             + normalization bytes  what the tokenizer's NFC normalizer writes wider (below)
capped at context_tokens; then + max_tokens, capped at context_tokens again (request_tokens)
the cap holds because the backend refuses a longer prompt itself ("The backend's prompt cap", below)
```

An average such as "characters / 3.5" is not a bound: 100,000 characters of `a!a!…` are 100,000 tokens on Qwen's
tokenizer (1 byte a token), and the old estimate called them 28,572, so seven such turns were admitted at once while
each really holds ~7.9 GiB. An earlier version asked vllm-mlx's `POST /v1/messages/count_tokens` and added the bytes of
what that count leaves out. Three review rounds each found another under-count (nested tool_result items joined with
`\n`, tool descriptions through `tojson`, `response_format` schemas injected into the system prompt): modelling a
counter's omissions does not converge. The count call, its 2-slot semaphore, its 5 s deadline and `uncounted_bytes()`
are gone, and the gate makes no backend call before the turn itself.

**Why it cannot under-count.** The body is the JSON the client sent, before the gate adapts it. Tokens ≤ UTF-8 bytes
of the rendered prompt after the tokenizer's normalization, because Qwen's tokenizer normalizes with NFC and then runs
a byte-level BPE in which every token covers at least one byte. Checked on the public `Qwen/Qwen3.5-27B` tokenizer.json
(the closest public family to the Pro's Qwen3.8 27B; sha256 `5f9e4d49…`, read with the stdlib, never loaded as a
model): `normalizer` `{"type": "NFC"}`, pre-tokenizer `Split` + `ByteLevel`, BPE without byte fallback, all 248,044
vocabulary entries in the byte-level alphabet and at least one byte long; special tokens are matched before
normalization and cover several bytes each. `Qwen/Qwen3-8B`'s tokenizer.json is also NFC. The Pro's own
`~/models/mlx/Qwen3.8-27B-heretic-ara-4bit-bench/tokenizer.json` was not read (this was built on the Air); its
`normalizer` should be checked to be NFC or none before relying on this. So it is enough that the normalized prompt
has no more bytes than the bound:

- Text the model sees is a copy of a JSON string in the body, and a JSON string costs at least as many bytes as it
  renders to. ASCII is one byte either way. Non-ASCII is raw UTF-8 (the same bytes) or a `\uXXXX` escape (6 bytes for
  ≤ 3, 12 for a 4-byte character). An escape such as `\n` or `\"` is 2 bytes for 1.
- Where text is escaped again on the way in (`tojson` writes a newline as `\n`), the result is never longer than the
  body's own escape: `\n` is 2 bytes in the body and 2 characters after `tojson`.
- Separators vllm-mlx adds between copied items are paid for by the items' own JSON. Joining tool_result or system
  items with `\n` costs 1 byte per item, and each item costs at least 3 body bytes (`"",`). System messages are joined
  with `\n\n`, but each message costs ~30 bytes.
- `response_format` instruction text is in the body (name, description), apart from the fixed wording counted below.
- The chat template's own text (role markers, `<tool_call>` wrappers, the tool-use preamble, about half the
  allowance) is the only text from outside the body. The allowance covers it, and as a token count it is also at least
  as many bytes as that text. The Qwen3.5 template is pure ASCII, so normalization does not change it.
- **Normalization** (`local_governor.normalization_bytes`). NFC can widen text: U+FA6C (3 bytes) becomes U+242EE (4
  bytes), so `"\ufa6c" * 20000` is 60,000 body bytes and 80,000 prompt bytes (up to 80,000 tokens); U+1D160 (4 bytes)
  becomes three code points (12 bytes). A ratio cannot bound this, so the bound measures each string in the body, keys
  included: it adds `len(NFD(s)) − len(s)` UTF-8 bytes (when positive) for every time vllm-mlx can write `s` raw into
  the prompt. That is once per string, and twice for a forced tool_choice's tool name. NFD, not NFC, is measured for
  two reasons. NFD is per character, so strings measured apart add up to the prompt they are concatenated into, while
  NFC can compose across a string boundary. And NFC never has more bytes than NFD: canonical composition replaces a
  starter and a combining character by one code point that is no longer in UTF-8 than the two. A test checks this over
  every composition in the Unicode table. A code point this Python's Unicode table leaves unassigned (the tokenizer's
  table may be newer) counts 16 more bytes, its widest possible decomposition. Text vllm-mlx writes ASCII-escaped
  (`json.dumps`) is ASCII and normalizes to itself. The cost: text with combining forms counts more than its raw
  bytes (`é` is 2 bytes and counts 3; a Hangul syllable is 3 and counts 9), and ASCII counts nothing extra. Strings are
  measured in 64K-character chunks, and measuring stops once the bound passes the cap.

**What vllm-mlx 0.6.0 writes wider than it came, bounded explicitly** (`local_governor.reserialized_bytes`; read from
its source). Re-serializing a JSON value is not a copy. The rendered text can outgrow the body bytes it came from, so
each of these is added in full, as long as its widest rendering, on top of the body bytes:

| Re-serialized | How vllm-mlx renders it | Why it can outgrow the body | Added |
|---|---|---|---|
| each tool definition | the template's `tojson` of the OpenAI-shaped tool (`_convert_tool`) | compact `,` / `:` become `, ` / `: ` (`[0,0,…]` 2 bytes an item → 3); the `{"type": "function", …}` wrapper and the default schema of a tool without `input_schema` are not in the body | `json.dumps` of that OpenAI-shaped tool |
| each `tool_use` input | `json.dumps(input)` (ASCII-escaped) as the call's `arguments`, or `tojson` of it | `é` is 2 bytes raw and 6 as `é` (3×); separators as above | `json.dumps(input)` |
| `response_format` | `build_json_system_prompt`: fixed wording + name + description + `json.dumps(schema, indent=2)` | `indent=2` writes a line and its indentation per value: a 300-deep schema of ~3 KB becomes ~90 KB | `json.dumps(response_format, indent=2)` + `INJECTED_TEXT_BYTES` (1024) |
| a forced `tool_choice` (`any` / `tool`) | an `[IMPORTANT INSTRUCTION] You MUST call …` line, naming the tool twice | ~190 bytes of fixed wording | the `tool_choice` JSON + 1024 |

`json.dumps` with its defaults (ASCII-escaped, `, ` / `: `) is never shorter than any of these renderings: `tojson`
uses `ensure_ascii=False` and the same separators. So each addition is at least as long as what vllm-mlx writes. The
sizes are counted chunk by chunk (`JSONEncoder.iterencode`) and the count stops at the context cap, so a 32 MiB body
or a deep schema is never built in memory. A value nested too deeply to encode counts as the whole context.

The tests render each reviewer case as vllm-mlx 0.6.0 does and check that the bound covers it (`tests/test_local_mlx.py`
`InputTokensTests` and `MlxGateBoundTests`): 1,000,000 empty tool_result items (999,999 newlines), a 100,000-newline
tool description, a 100 KB `response_format` description, a 300-deep schema under `indent=2`, a non-ASCII `tool_use`
input and a compact enum through `tojson`.

**The cost.** Code-heavy Claude Code turns run about 3.5–4.5 body bytes per token, so the bound reserves roughly four
times the real input. A turn whose real input passes ~22k–38k tokens (depending on max_tokens and density) reserves
the whole context. Reservations, and how
many of one size fit at once by bytes (25 GiB beside the 4-bit weights, 17.6 GiB beside the 6-bit), with an allowance
for ~20 messages and 40 blocks:

| Real input tokens | max_tokens | Reserved tokens (3.5–4.5 B/token) | GiB each | Fit by bytes, 4-bit | Fit by bytes, 6-bit |
|---|---|---|---|---|---|
| 2,000 | 512 | 10.1k–12.1k | 2.3–2.4 | 10–11 | 7 |
| 2,000 | 32,000 | 41.6k–43.6k | 4.2–4.3 | 5 | 4 |
| 8,500 | 512 | 32.9k–41.4k | 3.7–4.2 | 5–6 | 4 |
| 8,500 | 32,000 | 64.4k–72.9k | 5.6–6.1 | 4 | 2–3 |
| 16,000 | 4,096 | 62.7k–78.7k | 5.5–6.5 | 3–4 | 2–3 |
| 38,000 and up | any | 131,072 (the cap) | 9.65 | 2 | 1 |

The count limit applies on top: 2 with the owner active, 8 idle (6 for the 6-bit), 1 under warn pressure. So:

- (Before #126.) Two full 128k turns ran at once at 4-bit. On the text-only model a full 128k turn now reserves 24.6
  GiB alone, and nothing else runs beside it (#126, below).
- With the owner active, every row admits at least 2 by bytes, so short turns are limited by the count cap first.
- With the owner idle, bytes limit before the count of 8 except for small turns with small max_tokens. A turn with
  vllm-mlx's default max_tokens (32k, as Claude Code sends) reserves ≥ 4.2 GiB whatever its size.

This is concurrency given up for a bound that holds by construction. The provider's own skip estimate
(`local_lane.request_tokens`, characters / 3.5) is unchanged. It only decides whether to try the node, and the gate's
refusal handles the rest.

### Active budget: smaller while the owner works (2026-11-22)

**Why.** The hardware test on 2026-11-22 (report on the owner's Pro: `CortadOS-Router-notes/hwtest-20261122/REPORT.md`)
ran with the owner's apps open. vllm-mlx hit a Metal out-of-memory at ~38 GiB of MLX memory, ~24 GiB above the 4-bit
27B's weights (14.73 GiB idle footprint), and swap grew 5 GiB at a ~31 GB process footprint. The byte budget was 25 GiB
beside the weights (`free_bytes` 26,843,545,600), and one full 128k text turn reserved 24.57 GiB, right at that edge.
Eight short turns measured 3.33 GiB at peak against 14.32 GiB reserved, so short traffic was never the problem. A later
measurement on the same day (one full 130,872-token turn alone: **11.93 GiB** over idle) showed that 24.57 GiB was an
overcount, and the KV term was corrected ("Measured against the hardware" in "Text prefill" below): a full 128k turn
now reserves **17.06 GiB**.

**Owner decision 2026-11-22, "smaller while I work".** While the owner is **active** (the governor's existing signal:
keyboard/mouse idle under `active_idle_s`, 600 s; an unreadable idle time counts active), requests reserve against
`active_memory_budget_bytes` (32.25 GiB: 17.25 GiB beside the 4-bit weights). While the owner is **idle**, against
`memory_budget_bytes` (40 GiB: 25 GiB). The owner asked for ~17 GiB and for every request to keep the full 128k
context while active if physically possible. The measured peak of a full turn is 11.93 GiB, so it is. Its corrected
reservation is 17.06 GiB: at exactly 17.00 GiB the most one turn may hold is 130,617 tokens, 455 short of the context.
17.25 GiB is the smallest quarter-GiB step that serves the whole context. Both are totals, like the old single budget,
so heavier weights leave less room beside them: the 6-bit entry (22.4 GB) gets ~9.85 GiB while the owner works
(72,361 tokens) and 17.6 GiB while away (the whole context), and the vision entry (16.5 GiB of weights) 15.75 GiB, so
its largest image turn (5 images at the 64k image context, 15.9 GiB) goes to the cloud while the owner works and is
served when the owner is away. The config check that one image turn fits uses the active (smaller) budget.

What fits alone (text-only; "Text prefill" has the formula):

| Weights | Owner | Beside the weights | `fit_tokens` | 64k turn (9.01 GiB) | 128k turn (17.06 GiB) |
|---|---|---|---|---|---|
| 4-bit | active | 17.25 GiB | 131,072 | fits (two at once) | fits (nothing beside it) |
| 4-bit | idle | 25 GiB | 131,072 | fits (four at once) | fits (nothing beside it) |
| 6-bit | active | 9.85 GiB | 72,361 | fits | too large: 413, routed elsewhere |
| 6-bit | idle | 17.6 GiB | 131,072 | fits | fits |

**Transitions.** The budget is chosen per governor sample (`sample_s`, 5 s), from the same `owner` the count uses.

- idle → active never revokes a running turn: its reservation stays, the gate does not touch it, and it runs to the
  end. Every new turn that does not fit the smaller budget beside what still runs is a retryable `529` (memory budget:
  0.0 GiB free), and the report says `free_bytes` 0 and `free_tokens` 0, so the provider skips the node for new
  conversations until enough of the running turns end. This is the count rule's "stop admitting beyond the active
  count, let running replies finish", applied to the bytes. It is not a drain: the set in flight keeps its width as
  usual and the turns that fit are admitted as soon as room frees.
- active → idle widens the room at once (the next sample), for the turns running and the ones to come.

**413, not 529, for "fits only when the owner is away".** While the owner works, a turn over the active budget alone is
`too_large`: a non-retryable `413` (`x-should-retry: false`), and the capacity report's `fit_tokens` is the active one,
so the provider's skip check (`local_lane.gate_fit`) already routes such a turn elsewhere before sending, and an opening
turn the gate still refuses is re-routed at once (`local_lane.TOO_LARGE`). The retryable 529 would be wrong here: it
means "room frees when a running turn ends", but this room frees only when the owner steps away for 10 minutes, which
may be hours. The client would retry every 5 s (Claude Code gives up after its retries) and the request would wait
instead of going to the cloud. On the 4-bit default no turn is in this case any more (the whole context fits the
active budget). On the 6-bit copy it costs this: a **continuation** of a conversation already bound to the Pro that
grows past ~72k tokens while the owner works gets the terminal 413 a too-large continuation always got ("start a new conversation,
compact, or exclude the model"); it is not resumed on the Pro when the owner leaves.

### The backend's prompt cap (vllm-mlx runs under a guard)

The reservation is capped at `context_tokens` (131,072): the KV cache of a sequence no longer than that is all the
backend can hold, **provided the backend refuses longer prompts**. vllm-mlx 0.6.0 does not. It neither truncates nor
refuses a prompt over the model's context, and `--max-request-tokens` bounds only `max_tokens`. So user content
`"a!" * 100000` with `max_tokens: 1` (200 KB, a 200k-token prompt) reserved the 131k cap at the gate, ran at 200k
tokens, and two of them passed admission. The fix is the second of three options, for the reasons below.

1. **An exact count on the generation path (not available).** vllm-mlx 0.6.0's HTTP routes (`server.py`) are
   `/v1/messages`, `/v1/messages/count_tokens`, `/v1/chat/completions`, `/v1/completions`, `/v1/responses`,
   `/v1/embeddings`, `/v1/rerank`, `/v1/audio/*`, `/v1/mcp/*`, `/v1/models`, `/v1/status`, `/v1/cache/stats`,
   `/v1/requests/{id}/cancel`, `/health` and `/metrics`. None of them tokenizes a request through the template and
   tokenizer generation uses. `count_tokens` is the path an earlier round dropped because it is not that path.
2. **The backend refuses itself (taken).** On the text (LLM) route, vllm-mlx tokenizes a prompt for generation in one
   place: `Scheduler.add_request` (`vllm_mlx/scheduler.py`, lines 2096–2125:
   `request.prompt_token_ids = self.tokenizer.encode(request.prompt)` when unset). It runs before the request is
   queued: `self.waiting.append(request)` at line 2221 is the only place a request enters the queue, and
   `EngineCore.add_request` / `generate_batch_sync` (`engine_core.py` 493, 496, 719) are its only callers.
   `integrations/vllm-mlx/cortados_vllm_mlx_guard.py` replaces that method with one that tokenizes the same way, sets
   the ids and refuses when `prompt tokens + max_tokens > context_tokens`, before calling the original. Because it
   sets the ids, the original prefills exactly what was counted: the count is the prompt, not a second opinion of it.
   The refusal is fastapi's `HTTPException(413, "cortados guard: prompt too long for this node's context (prompt N
   tokens + max_tokens M = T, over the C-token context)")`. Nothing is queued or prefilled, and
   `engine_core.add_request` aborts and cleans up a request whose `add_request` raised (line 503,
   `except BaseException`). Prompt + max_tokens is capped, not only the prompt, so a running sequence never outgrows
   its reservation either.
3. **Refuse in the gate (not taken).** Refusing every turn whose byte bound + max_tokens exceeds the context would
   cap the local lane at about 22k–28k real input tokens with Claude Code's 32k max_tokens (131,072 − 32,768 −
   ~1–2k of allowances ≈ 97k body bytes, at 3.5–4.5 bytes a token), and ~28k–36k even at max_tokens 4,096: a
   quarter of the context. With the guard, the lane serves turns up to the real 131k (their reservation is the whole
   context, as before).

How the guard runs. `scripts/install_mlx_backend.sh` copies it to `<venv>/bin/cortados-vllm-mlx-guard.py`. The
LaunchAgent runs `<venv>/bin/python <venv>/bin/cortados-vllm-mlx-guard.py --context-tokens <governor context_tokens>
-- serve <model> …` (`local_backend.serve_argv`). The guard installs itself and hands over to vllm-mlx's own CLI
(`vllm_mlx.cli.main`); it loads no model itself. It runs in the MLX venv, so the router package's stdlib-only rule
does not apply to it (it imports `vllm_mlx` and `fastapi` from there). It is pinned: `vllm_mlx.__version__` must be
`0.6.0` and `scheduler.py`, `engine_core.py`, `engine/batched.py` and the engine dispatch (`cli.py`, `server.py`,
`model_registry.py`, `engine/__init__.py`, `engine/base.py`) must match the sha256 of the PyPI wheel
`vllm_mlx-0.6.0-py3-none-any.whl` (`a5e5a52a…f062`). Anything else stops startup, so a new vllm-mlx gets this argument
re-read before it serves. The multimodal route (`MLLMScheduler`) tokenizes for generation inside mlx_vlm's
`prepare_inputs`, which the guard cannot count the same way, so it is refused: the guard stops startup if vllm-mlx
resolves the model to the MLLM route, and `MLLMScheduler.add_request` refuses every request. The install script runs
`--check <model dir>` (pins and route; serves nothing) before it writes the plist, so a model that would take the MLLM
route is reported at install time and nothing is installed.

Only the batched engine is guarded, so only it may serve (round 6). vllm-mlx 0.6.0 picks `BatchedEngine` (its text
route is `AsyncEngineCore` → `Scheduler.add_request`) when served with `--continuous-batching`, and `SimpleEngine`
(mlx-lm directly, no scheduler, no cap) otherwise. Pins on the files that choose do not prove the choice, so the guard
enforces it at runtime: `serve` without the exact `--continuous-batching` flag is refused before vllm-mlx starts;
`BaseEngine.__new__` admits only `BatchedEngine` itself, so building `SimpleEngine` (or any other engine) raises and
the server exits at startup; every other engine's `start`/`generate`/`stream_generate`/`chat`/`stream_chat` raises; and
a `BaseEngine` subclass defined after install is refused at class creation. Tests use a fake `vllm_mlx` package
(`tests/test_vllm_mlx_guard.py`).

What the client sees. A non-streamed turn gets vllm-mlx's 413 `{"detail": …}`, which the gate answers as its own
never-fits refusal (`413 request_too_large`, `x-should-retry: false`, message `local gate: too large for this node's
memory (cortados guard: …)`), so the provider routes the turn elsewhere as for any never-fits turn. A streamed turn
has already received `message_start` from vllm-mlx before the scheduler is reached. It ends with an Anthropic `error`
event carrying the guard's text, then `message_stop`, and is not rerouted. Either way nothing ran on the node. Only a
turn whose real prompt + max_tokens exceeds 131k tokens gets this. A pre-stream refusal (a clean 413 for streamed
turns too) was considered in round 6 and not taken: vllm-mlx renders the template inside `BatchedEngine.stream_chat`,
which `server._stream_anthropic_messages` calls lazily inside the `StreamingResponse`, so a check before the response
would render and tokenize the whole prompt a second time on the event loop, and its ids could not be reused without
also patching `stream_chat`, `stream_generate` and `AsyncEngineCore`.

Keep the two numbers in step. The gate's cap and the guard's come from the same config value (`context_tokens`). After
lowering it, reinstall the backend (`install_mlx_backend.sh`) **before** restarting the gate; after raising it,
restart the gate first. A gate capped below the guard reserves less than a prompt the guard admits.

**Admission is one step.** With no count call to wait for, the count slot and the byte reservation are checked and
taken together under one lock (`Admission.try_acquire`). A refused turn takes nothing, and an admitted turn's
`Ticket` is released exactly once, in the gate's `finally`, however the turn ends.

**Text only.** The Pro's vllm-mlx serves the 27B with its vision tower excluded at load. vllm-mlx 0.6.0's adapter
turns a top-level image into image input, which the loaded model cannot use. It silently drops a document, any other
non-text block, and an image inside a tool_result, so the model would answer as if it had seen them. On a vllm-mlx
node the gate therefore refuses a turn carrying any of these (`local_governor.non_text_content`: message blocks other
than text, thinking, redacted_thinking, tool_use and tool_result; tool_result content or system entries other than text
blocks and strings). The refusal comes before admission: `400 invalid_request_error`,
`x-should-retry: false`, no Retry-After, message `local gate: content this node's model cannot read (image block in
message 3; it serves a text-only model)` (`local_gate.UNSUPPORTED_MESSAGE`). An Ollama node relays images as before:
its model decides. The provider treats the refusal like "never fits" (below).

A request without `max_tokens` reserves vllm-mlx's default (`vllm_mlx.serve.max_tokens`, 32768). Per model (a
`vllm_mlx.models` entry overrides the `governor` default), measured and derived for Qwen3.8 27B (hybrid attention /
recurrent; vllm-mlx keeps the KV cache unquantized, fp16):

| Setting | Default | Why |
|---|---|---|
| `memory_budget_bytes` | 40 GiB | of the Pro's 48 GB, leaving ~8 GB for macOS and the owner's apps; the budget while the owner is **idle** (25 GiB beside the 4-bit weights: a full 128k text turn fits) |
| `active_memory_budget_bytes` | 32.25 GiB | the budget while the owner is **active** (17.25 GiB beside the 4-bit weights, the smallest quarter-GiB step at which a full 128k turn's corrected 17.06 GiB fits; owner decision 2026-11-22 "~17 GiB, keep 128k if physically possible", "Active budget" below); absent: min(32.25 GiB, `memory_budget_bytes`); never above it |
| `weights_bytes` | 15 GiB (4-bit); 22.4 GB (6-bit, measured idle) | resident weights |
| `kv_bytes_per_token` | 65,536 | 16 full-attention layers × K/V × 4 KV heads × head_dim 256 × 2 bytes; 128k tokens = 8 GiB. Only qwen3_5's full-attention layers (every `full_attention_interval` = 4th of 64, `layer_types` in config.json) keep per-token KV; the 48 linear-attention layers keep fixed-size state (`per_seq_state_bytes`). `local_governor.text_kv_bytes_per_token()` derives it from the pinned `TEXT_DIMS` |
| `per_seq_state_bytes` | 0.15 GiB | the recurrent layers' state per sequence |
| `prefill_margin_bytes` | 1.5 GiB | prefill activations and allocator slack: the 6-bit copy used 2.3 GB over idle for one ~9k-token request, KV + state explain ~0.7 GiB. Since #126 the per-request floor on the text-only model (its text turns reserve the computed set peak, "Text prefill" below) |
| `context_tokens` | 131072 | the Pro block's `context_length` on the provider host; tokens are capped here |
| `text_prefill_chunk_tokens` | 256 | #126: the guard's text prefill chunk (`--text-prefill-tokens`; upstream 2,048); the score term is linear in it |
| `text_score_buffers` | 4 | #126: score-sized buffers per chunk (q·kᵀ, masked copy, softmax, one for the allocator); computed, to be measured |
| `kv_copies` | 2 | #126, corrected 2026-11-22: the padded decode batch's KV once, plus `kv_copies` − 1 replaced copies of one full-attention layer's slice of it (1/16); at least 2 |
| `buffer_cache_bytes` | 512 MiB | #126: MLX's buffer cache limit as the guard sets it (`--buffer-cache-bytes`; upstream 90% of the GPU working set); a recycling threshold, so one largest buffer is counted on top |

What the per-request formula admits, in RESERVED tokens (the bound's, not a turn's real size; "The cost" above maps one
to the other; owner idle, pressure normal; 25 GiB beside the 4-bit weights, 17.6 GiB beside the 6-bit). On the
text-only model these are superseded by #126's set reservation ("Text prefill", below). Only the short rows still
stand there, byte for byte:

| Mix | Reserved | 4-bit | 6-bit |
|---|---|---|---|
| one 128k-token turn | 9.65 GiB | yes | yes |
| two 128k-token turns | 19.3 GiB | yes | no (the second waits) |
| three 128k-token turns | 29.0 GiB | no (the third waits) | no |
| eight ~8.5k-token turns (512 out) | 8 × 2.17 GiB | yes (the count's 8) | count-capped at 6 |
| two 128k + short turns | 19.3 + 2.17 each | two short fit, a third waits | — |

Measured footprints the defaults were checked against: 4-bit ~31 GB at 8 × ~8.5k tokens; 6-bit 24.7 / 25.9 / 27.9 /
31.5 GB at 1 / 2 / 4 / 8. The reservations are on the safe side of both (8 short 4-bit turns reserve 17.4 GiB against 16
GB measured above the weights).

Rules on top:

- **A single turn is always admitted when nothing runs and it fits alone** (and the count allows one: under critical
  pressure nothing is admitted).
- **A turn that cannot fit now even alone** (its reservation exceeds the budget in force − `weights_bytes` with
  nothing else running) is refused non-retryably, not left to an endless 529 loop: see Enforcement. While the owner is
  active that includes a turn that would fit the idle budget ("Active budget" below).
- **Pressure stays the safety net**: on warn or critical the count caps apply even when the bytes fit.
- Both bounds gate **admission only**: when the owner comes back with 6 running, the limit drops to 2, nothing new is
  admitted until fewer than 2 run, and the 6 finish on their own (never killed). The byte budget shrinks the same way:
  three 64k turns admitted while the owner was away keep their 18.17 GiB when the owner returns to a 17.25 GiB
  budget, and every new turn is a retryable 529 until enough of the running ones end. A reservation is released however
  its turn ends: answered, backend error, upstream timeout, backend unreachable, client gone mid-stream, or an
  exception in the relay (a `Ticket`, released once). On Ollama and on a vision model it is released in the gate's
  `finally`; on a vllm-mlx text-only model only when the guard confirms the turn left vllm-mlx's scheduler
  ("Retirement" below), because closing the connection does not end the request there.
- vllm-mlx's own `--max-num-seqs` is `max_seqs()`: the idle cap, bounded by how many empty-context reservations fit.
- **`vllm_mlx.extra_args` cannot override what the generator sets.** argparse keeps the last value of a repeated flag
  and expands abbreviations (`--ho` is `--host`), so `["--host", "0.0.0.0", "--max-num-seqs", "64"]` would expose the
  unauthenticated backend and lift its bound under the gate. `extra_args` is checked against an allowlist
  (`local_backend.EXTRA_ARGS_ALLOWED`; exact flag names, `--flag value` or `--flag=value`): `--default-temperature`,
  `--default-top-p`, `--default-top-k`, `--default-min-p`, `--default-presence-penalty`, `--default-repetition-penalty`,
  `--stream-interval`, `--chunked-prefill-tokens` and `--disable-prefix-cache`: sampling defaults, output pacing or
  less memory, never exposure, admission or memory per request. Any other flag (`--host`, `--port`, `--max-num-seqs`,
  `--max-tokens`, `--api-key`, `--mcp-config`, …) or a bare argument fails config validation (`--print-config`, the
  gate's start, the install script) with a message naming it and the allowed list; `serve_argv` checks again.

Old configs: the count-only keys `per_request_gib`, `memory_budget_gib` and `weights_gib` are refused by name, with the
key that replaced them (a gate given an old config does not start; update `~/.config/cortados/local-backend.json`
from the example).

Enforcement, in the gate on `POST /v1/messages` (the probe is never counted); nothing reaches the backend when a
turn is refused:

- over either bound **now**: `529 overloaded_error`, `Retry-After: retry_after_s` (5), message `local gate: busy (n of
  limit running: reason)`; the reason names the memory budget when the bytes refused it (`memory budget: 5.7 GiB free,
  request needs 9.6 GiB`);
- **never fits**: `413 request_too_large`, `x-should-retry: false`, no Retry-After, message `local gate: too large for
  this node's memory (…; at most N tokens of input + max_tokens fit)`.
- **content the model cannot read** (vllm-mlx, an image or document; above): `400 invalid_request_error`,
  `x-should-retry: false`, before admission.

Both carry the capacity report. The gate reports `{"backend", "limit", "inflight", "owner", "pressure", "reason",
"reserved_bytes", "budget_bytes", "free_bytes", "fit_tokens", "free_tokens"}` read-only as `cortados_gate` in every `GET /api/tags`
answer and as `capacity` in the switch status (provider `GET /v1/cortados/local`; the dashboard's Local models card
shows "vllm-mlx: n of limit running, R of B GiB reserved (owner active|idle budget) (reason)"). `budget_bytes` is the
budget in force beside the weights (smaller while the owner is active), `free_bytes` that budget minus the reservations
(0 while the reservations exceed it after the owner's return); `fit_tokens` is the most context one turn may hold alone, `free_tokens` beside what runs now
(0 when the count is full). Bytes and tokens are null without a governor.

The provider (`local_lane.gate_busy`, `local_lane.gate_fit`) keeps the latest report per node for 10 s and ESTIMATES a
turn's tokens (`local_lane.request_tokens`: body characters / 3.5 + max_tokens; it cannot count, and the estimate only
decides whether to skip the node, so a turn it lets through may still be refused by the gate, below). A full gate, a turn over `free_tokens` (busy) or
over `fit_tokens` (too large) is skipped by the index route, auto_route and the sensitive moves after their probe, and
S1's route stands ("routes elsewhere"). A turn the gate still refuses:

- busy (a race between probe and turn, or a continuation of a local-bound conversation): answered to the client as
  `529 overloaded_error` with the gate's `Retry-After`, the provider's existing busy shape (BUSY_STATUS), which Claude
  Code and the SDK retry; a refused opening turn leaves no conversation behind, so its retry is routed afresh (to the
  cloud while the node stays full);
- too large, on the **opening turn of a conversation a rule moved to the node**: nothing is sent to the client; the
  provider drops the conversation and routes it again once with that local model excluded (S1's route, or another
  node), and the client gets that route's answer;
- too large on a **pinned** turn (`sub/<local model>`) or a **continuation**: a terminal `413 request_too_large` with
  `x-should-retry: false`, saying the turn is too large for the node's memory (compact, lower max_tokens, start a new
  conversation, or pin a cloud model); the conversation is unchanged;
- content the text-only model cannot read (`local_lane.GATE_UNSUPPORTED`), handled the same way: on the opening turn of
  a conversation a rule moved to the node it is routed again with that local model excluded (`local_lane.UNSUPPORTED`,
  one of `local_lane.REROUTE`); on a pinned turn or a continuation, a terminal `400 invalid_request_error` with
  `x-should-retry: false`, saying the node serves a text-only model (pin a model that reads images, or send text only).

- any other HTTP 5xx before the first byte, on the opening turn of a COST-profile bind of a request no sensitivity
  detector marks: routed again the same way (`local_lane.NODE_ERROR`; "A node error before any byte" above). A
  sensitive request or bind, a pin or a continuation gets the node's error as before.

The refusal fallback (`local_lane.complete`) treats a busy, too-large or text-only refusal as an unavailable node.

### Text prefill and the text route's reservation (#126)

**What the released code does** (vllm-mlx 0.6.0, mlx-lm 0.31.3, MLX 0.32.4; read from the PyPI wheels and MLX's
v0.32.4 source, never run). On a text-only model, `Scheduler._create_batch_generator` (`scheduler.py` 1518-1600) builds
mlx-lm's `BatchGenerator` with vllm-mlx's `--prefill-step-size` (default **2,048**, not 1,024 as #126 first said) and
`--prefill-batch-size` (default 8).

- **Chunking: yes, at 2,048, up to 8 prompts together.** `PromptProcessingBatch.prompt` (`generate.py` 1124-1170)
  runs the model over at most `prefill_step_size` tokens at a time for every prompt in the prompt batch, right-padded
  to the longest, against one `BatchKVCache`. Between steps it evaluates the cache state and clears MLX's cache.
- **Full-vocabulary logits: no.** The prefill evaluates only `[c.state for c in cache]`, so the vocabulary projection
  of prompt positions is never computed (MLX is lazy). The last prompt token goes to the first decode step, which keeps
  `logits[:, -1, :]` of a one-token input. Nothing like #125's 27.8 GiB of logits exists on this route.
- **Attention scores: unfused, per chunk against the whole prompt.** qwen3_5's full-attention layers
  (`qwen3_next.Qwen3NextAttention`) call `mx.fast.scaled_dot_product_attention` with the batch cache's boolean mask.
  MLX 0.32.4's `use_fallback` (`scaled_dot_product_attention.cpp` 1390-1462) takes the unfused path for head_dim 256
  below 1,024 query tokens. Above that it does so too, unless the GPU has neural accelerators or the chunk is the first
  one. The fallback (`fast.cpp`) builds bfloat16 `q·kᵀ`, a masked copy and a softmax of Hq × chunk × context each.
  At the upstream step, a buffer is up to **8 × 24 × 2,048 × 131,072 × 2 bytes = 96 GiB** (12 GiB for one prompt). The
  issue's "6 GiB a buffer" was one prompt at 1,024.
- **The decode batch pads.** `GenerationBatch.extend` → `BatchKVCache.extend` (`cache.py` 1035-1078) pads every row's
  KV to the longest row. So n requests beside a 128k one hold n × 128k of KV, not the sum of their lengths. The
  per-request formula above is not a bound for a mixed set.
- **KV growth.** `KVCache` / `BatchKVCache` grow by 256 positions by concatenation. A request joining or leaving the
  batch (`extend`, `filter`) and a finished request's extracted row (`extract`) each build a new copy beside the old
  one. vllm-mlx sets MLX's buffer cache limit to 90% of the GPU's working set (`engine/batched.py`
  `_resolve_metal_buffer_cache_limit`), so freed copies stayed resident until the next periodic clear (every ≤ 32
  scheduler steps).
- **The gated-delta state.** Per sequence, 48 linear-attention layers × 48 × 128 × 128 float32 (`gated_delta.py` 279)
  plus the conv state = 0.144 GiB, under `per_seq_state_bytes` (0.15 GiB). It is copied when the batch is reshaped.

**What the guard changes** (`cortados_vllm_mlx_guard.py` "Text prefill"; flags from `local_backend.text_argv`):

- Every `BatchGenerator` the Scheduler builds prefills **one sequence at a time**, in chunks of at most
  `text_prefill_chunk_tokens` (256): `prefill_batch_size = 1`, `prefill_step_size` and the prompt batch's own step
  clamped. vllm-mlx also gets `--prefill-step-size 256 --prefill-batch-size 1`, but the runtime clamp is the proof. A
  generator without that layout, or a model other than mlx-lm's `qwen3_5`, fails the request instead of prefilling it.
- MLX's buffer cache limit is set to `buffer_cache_bytes` (512 MiB, `--buffer-cache-bytes`). The limit is a
  recycling threshold, not a hard cap. MLX's allocator (`allocator.cpp` `free`) caches a whole freed buffer whenever
  the cache is under the limit, and trims back to the limit on the next allocation. So the cache holds at most the
  limit plus one buffer, and the reservation counts both (`buffer_cache` + `cache_overshoot`). A replaced KV copy or
  score buffer no longer stays resident until the next periodic clear.
- **Same outputs.** A chunk boundary only splits the prompt into causal attention against the cached keys plus the
  gated-delta and conv state carried in the cache. That is the path upstream already takes at 2,048. Not batching
  prefills is a scheduling choice: each row was already masked to its own tokens. Floating-point results can differ
  at the rounding level, as between any two chunk sizes, because GEMM tiling depends on the chunk's rows. No token is
  dropped or reordered. The cost is prefill speed: more, smaller steps, and concurrent prompts prefill one after
  another. Not measured.
- **Pinned.** mlx-lm 0.31.3 (`MLX_LM_PINNED_SHA256`, from the wheel `mlx_lm-0.31.3-py3-none-any.whl`, sha256
  `758cfddf…83b8`): `generate.py`, `sample_utils.py`, `models/cache.py`, `models/base.py`, `models/qwen3_5.py`,
  `models/qwen3_next.py`, `models/gated_delta.py`, `models/activations.py`, `models/rope_utils.py`,
  `models/switch_layers.py`, and `__init__.py`, `_version.py`, `models/__init__.py`; since #126 review round 4 also the
  rest of what `import mlx_lm` runs (`convert.py`, `utils.py`, `tokenizer_utils.py`: `__init__` imports convert,
  generate and utils) and every module loading a tokenizer may import by name (`tool_parsers/*.py`,
  `chat_templates/*.py`); any entry in those two folders that the release lacks is refused, and so is a `config.json`
  naming a `model_file` (mlx-lm would execute it from the model folder). Since #126 review round 5 (P2c) "any entry"
  means any: a subdirectory (a regular or namespace package named like a pinned module is imported in its place,
  `FileFinder` tries the package first: the reviewer's `tool_parsers/qwen3_coder/__init__.py`), an extension module of
  any suffix in `importlib.machinery.all_suffixes()`, a `.pth`, a stray `.pyc`, or a pinned name that is not a regular
  file. The one exception is `__pycache__` (a real directory, not a link), accepted only while the guard's bytecode is
  isolated. Every pinned mlx-lm module must also resolve, through the import system's finders and without importing
  anything (`verify_lm_resolution`), to its pinned file: a `utils/` package beside `utils.py` is refused too. Since
  #126 review round 6 (P2c) none is skipped: `mlx_lm` itself (resolved within the folder holding it) and `_version`
  (`__init__` runs `from ._version import __version__`; a pyc-only `_version/` package won over the pinned file and
  ran). **Sourceless modules** (round 6): `sys.pycache_prefix` keeps `__pycache__` unread but does not stop Python's
  sourceless loader, so in every verified package tree (vllm-mlx, mlx-lm, mlx-vlm and mlx's Python folder,
  `verify_sources_only`, before anything is imported) a `.pyc` / `.pyo` outside `__pycache__`, an extension module
  beside a `.py` of its name, and a package folder (an `__init__` of any importable suffix) beside a `.py` of its name
  are refused. **Bytecode**:
  Python accepts a `__pycache__` file whose header carries its source's mtime and size, without reading the source, so a
  cached file beside a pinned source could run in its place. The guard's `main()` first sets `sys.dont_write_bytecode`
  and points `sys.pycache_prefix` at a fresh empty directory of its own (`isolate_bytecode`), before anything outside
  the standard library is located or imported: no in-tree cache is read, every verified source is compiled as it is
  imported (a slower start, once per backend start). The vllm-mlx files are the text route's existing pins
  (`scheduler.py`, `engine/batched.py`, `engine_core.py`). The text route now also requires `mlx` / `mlx-metal` 0.32.4
  (`verify_mlx`, as vision mode does), because the attention fallback and the allocator are in the compiled library. And
  `config.json` must be the verified text geometry (`check_text_model`, `TEXT_DIMS`: the language dimensions above, 64
  layers, full attention every 4th, dense, bfloat16). This is checked at `--check` and before `serve`. Any drift stops
  startup.

**The reservation** (`local_governor.text_set_bytes`). Every text turn on the text-only model joins one set; the
gate admits it while the set's total fits beside the weights. Notation: n = turns in the set; M = the longest turn's
tokens (input bound + max_tokens, capped) + 256; P = a turn's prompt bound (its input bound, capped at the context);
C = min(256, P); a = 2; Hq 24, Hkv 4, D 256, V 248,320; width = 314,112 bytes per token and layer. M also covers
the widest turn admitted since the set was last empty (a high-water mark, #126 review). When the longest row leaves
the decode batch, `BatchKVCache.filter` (`cache.py` 1016-1033) gathers the survivors at the old width and slices the
padding off. The slice is a view, so the wide buffer stays alive until the batch grows, a turn joins, or the batch
empties. The gate cannot see that moment, so it keeps the width until no text turn runs. For the same reason (review
round 2), the set's rows × width (n × M, "cells") and its `cache_overshoot` never shrink while any text turn runs.
filter's gather is lazy: it is evaluated on the survivors' next step, so a batch of eight can outlive seven finished
tickets. And the buffer the cache overshoots by can be a departed turn's. Both reset when the set drains: an empty
batch holds nothing, and vllm-mlx clears MLX's cache when requests finish (`scheduler.py` `_cleanup_finished`).

| term | bytes | what it holds |
| --- | --- | --- |
| kv | n × M × 65,536 + (kv_copies − 1) × n × M × 65,536 / 16 | the decode batch, every row padded to the longest, and one replaced copy of one of the 16 full-attention layers' slices (corrected 2026-11-22: it was kv_copies whole copies) |
| decode | 4 × Hq × n × M × a + n × M | one decode step's attention rows and mask against the padded batch |
| fixed | n × (2 × per_seq_state_bytes + V × (a + 8)) | each turn's recurrent state and one copy; its decode row's logits and float32 log-probabilities |
| prefill | max over the set of: 4 × Hq × C × P × a + 2 × C × P + 2 × C × width + 8 × P | ONE prompt's chunk at a time: unfused scores (q·kᵀ, masked copy, softmax, one for the allocator), the boolean mask and its left-padding product, one layer's activations doubled for the allocator, the prompt's ids |
| buffer_cache | buffer_cache_bytes (512 MiB) | MLX's buffer cache limit as the guard sets it, once while anything runs |
| cache_overshoot | max(Hq × C × P × a, n × M × Hkv × D × a), P the longest prompt | the one buffer the cache can hold over its limit: a chunk's score buffer, or one layer's K (or V) of the padded batch |
| **total** | max(sum of the above, the per-request formula summed) | the old reservation stands, byte for byte, wherever it was already at least the computed peak |

width is the larger layer: the MLP's gate, up and product plus the residual, norms and output: 2 × (3 × 17,408 + 4 ×
5,120). Plus the linear-attention layer: in_proj_qkv, the conv input and output (3 × 10,240), z, the gated-delta
output, its norm and gated product (4 × 6,144), the normed q and k (2 × 2,048), all bfloat16, and the float32
precise-swiglu and gate temporaries (4 × (2 × 6,144 + 4 × 48)). The full-attention layer is smaller (2 × (6 × 24 × 256
+ 4 × 4 × 256)).

By term, one turn whose prompt is its whole context (4-bit defaults; `tests/test_text_prefill.py`):

| tokens | scores | mask + chunk + ids | kv | decode + fixed | buffer cache + overshoot | **total** | old formula |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1,024 | 0.05 GiB | 0.15 GiB | 0.08 GiB | 0.30 GiB | 0.51 GiB | **1.71 GiB** (the old formula: computed 1.09) | 1.71 GiB |
| 32,768 | 1.50 GiB | 0.17 GiB | 2.14 GiB | 0.31 GiB | 0.88 GiB | **4.99 GiB** (#126 as merged: 6.88) | 3.65 GiB |
| 65,536 | 3.00 GiB | 0.18 GiB | 4.27 GiB | 0.31 GiB | 1.25 GiB | **9.01 GiB** (12.78) | 5.65 GiB |
| 131,072 | 6.00 GiB | 0.21 GiB | 8.52 GiB | 0.33 GiB | 2.00 GiB | **17.06 GiB** (24.57) | 9.65 GiB |

**The maximum admitted text prompt.** 4-bit, owner idle (25 GiB beside 15 GiB of weights) or active (17.25 GiB):
**the whole context**. Any prompt with prompt + max_tokens ≤ 131,072 fits, alone (17.06 GiB at the cap, so nothing
runs beside a full-context turn: the padded batch would make a second row 128k wide too). The 6-bit copy: the whole
context while the owner is away (17.6 GiB beside 22.4 GB); while the owner works (9.85 GiB) at most **72,362 tokens
of context** alone (`fit_tokens` reports 72,361, counting the prompt as the whole context). In the gate's input
bound, that is 72,361 with max_tokens 1, 70,191 with 4,096, and 54,999 with Claude Code's 32,768. Anything larger is a
**413 never-fits** (`x-should-retry: false`), which the provider routes elsewhere. A turn that fits alone but not
beside what runs now is a retryable **529**, as before.

**What changes for current traffic** (text-only 4-bit, owner idle, count 8; per-turn size from the input bound, which
counts ~4 body bytes per real token of code). Before #126 → #126 as merged → since the 2026-11-22 KV correction:

| input bound + max_tokens | reserved alone | fit at once (idle) |
| --- | --- | --- |
| 1,000 + 256 | 1.73 → 1.73 → 1.73 GiB | 8 → 8 → 8 (the count) |
| 8,000 + 512 (~2k real) | 2.17 → 2.49 → 2.17 GiB; 8 together reserve exactly the old 17.4 GiB | 8 → 8 → 8 |
| 36,000 + 512 (~9k real) | 3.88 → 7.52 → 5.42 GiB | 6 → 4 → 6 |
| 8,000 + 32,000 (~2k real, Claude Code) | 4.09 → 6.34 → 4.09 GiB | 6 → 4 → 6 |
| 34,000 + 32,000 (~8.5k real, Claude Code) | 5.68 → 11.01 → 7.22 GiB | 4 → 2 → 4 |
| 64,000 + 4,096 | 5.81 → 13.00 → 9.09 GiB | 4 → 2 → 4 |
| 99,000 + 32,000 (the cap) | 9.65 → 24.57 → 15.20 GiB | 2 → 1 → 2 |

A mixed set pays for its longest row: a short turn beside a 128k one now waits (529) instead of running, because it
would hold 128k of KV in the padded batch. After a long turn, or a full batch, ends, the turns still running keep
its width and row count in the reservation until no text turn runs. That is the price of not knowing when the backend
compacts.

**The drain** (#126 review rounds 2 and 3, P2a). Kept that way alone, the stale width could last forever: overlapping
short turns keep the set from ever being empty, and the reviewer's repro (a 64k turn, then 100 overlapping replacements
of a short one) ended with two 100-token turns holding 17.6 GiB and a third refused. So the set **drains** when the
kept terms (the high-water width and peak rows) withhold room: the most tokens one more turn may hold beside the live
turns (`text_room`, the report's `free_tokens`) is smaller with them than for the live turns reserved as a fresh set.
The governor checks this on its own state, at every departure, admission and capacity snapshot (round 3: the
provider reads `free_tokens` before sending, `local_lane.gate_fit`, and skips a node with no room, so a refusal that
would start the drain may never come). A text turn refused for bytes only the stale terms take drains the set too.
While draining, every new text turn is a retryable 529 ("text set draining"), the report says so in `reason` and
`draining`, and `free_tokens` is 0, so the provider counts the node busy, not too small. When the turns in flight end
(and are retired) the empty set resets and admission resumes at the fresh size. The wait is bounded by those turns'
`max_tokens` and the gate's read timeout. Room, not bytes, is the trigger: `text_room` is monotone in the tokens, so
equal rooms mean the kept terms refuse no request size a fresh set would admit, while their bytes differ after almost
every overlapping departure (two 20k turns overlapping: 9.0 against 5.6 GiB kept, the same room), which would drain
for nothing. A turn that would not fit a fresh set either, or a full count, is plain busy and starts no drain.

Why a drain rather than reclaiming when the backend says its batch shrank: the gate cannot see when `filter`'s lazy
gather is evaluated, when the old wide buffer is dropped, or which freed buffer sits in MLX's cache. A "freed" report
from the guard would need a safe point in mlx-lm's generation loop where all of that is known, and the released code
offers none without patching the loop. The drain needs no backend signal: it never shrinks the reservation while a
turn of the old set runs, so the lazy-free protection holds, and it costs only a short pause in admissions after a
long turn ends beside short ones. Retirement (below) tells the gate when a request has left the scheduler, which is
when its ticket may leave the set; it does not say when the batch's lazy buffers go, so the set's kept terms stay.

**Retirement** (#126 review round 3, P1). Closing the upstream connection does not end a request in vllm-mlx 0.6.0:
`_disconnect_guard` defers the stream generator's close by 0.5 s (`server.py` 5481-5497), and `Scheduler.abort_request`
only queues the id (`scheduler.py` 2227-2242) until the executor's next `step()` processes it
(`_process_pending_aborts`, then `_do_abort_request` removes its rows from the batch generator). Releasing a ticket when
forwarding ended let the reviewer's repro run 8 rows upstream (a timed-out 64k turn and 7 short ones, 31.25 GiB of KV)
against 7 tickets and 12.05 GiB reserved. Now, on a vllm-mlx node serving a text-only model (governed or not):

- each forwarded turn carries a fresh ticket id (`x-cortados-ticket: <epoch>.<seq>.<nonce>`: 16 hex, 16 hex, 32 hex,
  no request data; the gate process's epoch, its counter that only goes up, and 128 random bits that only make the id
  unguessable; epochs below);
- when forwarding ends the ticket turns **retiring**: it keeps its count slot, its bytes and its place in the text set,
  and the report counts it under `retiring` (`reason`: "N ended turns await backend release");
- the gate's `Retirer` thread asks the guard (`POST /cortados/retire {"tickets": [...]}` on the loopback backend, every
  0.2 s while any ticket retires, backing off to 2 s after a failure) and releases exactly the ids it answers retired;
- the guard (`TicketLedger`) refuses a generating request (any method but GET / HEAD / OPTIONS) without a fresh, unused
  ticket (409, before vllm-mlx reads it), binds every scheduler request id to its ticket in `EngineCore.add_request`
  (the request's own task; a scheduler request bound to no open ticket is refused), and at the **safe point**, the end
  of every `Scheduler.step()` and `reset()` (or on a query when no step is running), marks a request gone once it is in
  none of the waiting queue, the running map, the request-to-uid map (rows in the batch generator) or the unfinished
  requests. A ticket is retired when its HTTP exchange has ended and every request bound to it is gone or never entered
  the queue; it is then closed, and nothing more may bind to it.
- An id the guard never saw is retired, untracked, only when it can never be admitted: malformed, of an epoch below
  the current one, or of the current epoch below its floor; of the current epoch it is tombstoned first (tracked,
  refused should it still arrive). It never entered this process's scheduler. An unseen id of an epoch above the
  current one, and any id while no epoch is current (a new process before any fence), is never retired: that epoch
  may still be fenced and the id admitted. So a backend restart, a new process whose rows are all freed, retires every
  retiring ticket once the gate has fenced it (the gate fences on the new boot id).
- **Epochs, no wall clock** (#126 review rounds 5 and 6). Until round 4 an id began with its minting time; round 5
  replaced the clock with a random 128-bit gate generation, which gave identity but not order: a fresh guard adopted
  the first ticket's generation, acknowledged another generation's unseen id without recording it, and that
  generation could still be fenced and its delayed id admitted after its reservation was released (round 6, P2a); and
  a delayed old fence reaching a fresh guard after the live gate's recorded the live gate as replaced, fencing it out
  for good (P2b). Now each gate start takes the node's next **epoch**, a strictly increasing integer
  (`local_gate.next_epoch`, `~/.config/cortados/local-gate-epoch`, overridable with `CORTADOS_LOCAL_GATE_EPOCH_FILE`):
  under an exclusive `flock` on `<file>.lock` it reads the last epoch (a missing file is 0), adds 1, writes it to a
  temp file, fsyncs it, renames it over the file and fsyncs the directory, before the epoch is used. Files are 0600,
  a created directory 0700. A file that is not one decimal integer from 1 to 2^64-1, not a regular file, or a link is
  refused loudly and the gate does not start; it is never reset (a reset could hand out an epoch the guard already
  superseded). The guard has **no current epoch until a fence arrives** (no adoption: a fresh process refuses every
  ticket, 409); a fence of epoch E is accepted only if E is at least the current epoch (the highest this process has
  seen) and a lower one is refused (`"refused": true`) and changes nothing; once E is current an id of any other epoch
  is refused on arrival, at `bind` (`EngineCore.add_request`) and at `Scheduler.add_request`: one below E for good,
  one above E until its gate fences. A guard restart forgets everything, which is safe: the new process holds no rows,
  and a delayed old fence reaching it first is superseded by the live gate's higher epoch. Replay within an epoch is
  stopped by the record (open, ended, closed or tombstoned) and, once evicted, by the epoch's counter floor. On the
  guard's 409 ticket refusal (a backend restart while the gate sat idle, so no poll saw the new boot id) the gate
  fences again and retries the turn once, under a new id and the same reservation, once the fence lifts: the first
  turn after a backend restart costs one upstream retry and no client retry; the refused id stays known to the fence,
  with no reservation of its own, until the guard retires it. A refused retry answers 529 busy.
- **Fail closed**: no confirmation (the guard hung, a step that never returns, the backend unreachable) keeps the ticket
  reserved and the node busy. A connect that failed sent nothing and is released at once.
- **Atomic with admission** (#126 review round 4, P1a). `Scheduler.add_request` is not under the step lock, so a
  retire query's snapshot could start, an add could queue a bound request, and the query would then mark it gone while
  it ran. Now `Scheduler.add_request` (entering, the scheduler's own add, entered) holds the ledger's `admit_lock`, and
  so does every safe point from noting the entry sequence number to marking what is gone. A request is numbered as its
  add completes; a safe point may mark gone only a request numbered below the number it noted before reading, i.e. one
  that was queued before the read began. The lock makes the check redundant; the number keeps it true for any path that
  ever adds outside the lock. A query takes the step lock and the admit lock without waiting: with either busy it
  answers from the last safe point.
- **Bounded** (P2b). The guard tracks at most `TICKET_MAX` (8192) ids on every insert path (arrival and tombstone). At
  the bound it evicts closed ids (and ended ones of an epoch no longer current whose requests are all gone), old
  epochs first, then the current one by counter (every id of an old epoch, and at least the oldest quarter), and the
  current epoch's **floor** rises past every evicted counter: an id below the floor is refused on arrival
  and, asked about, retired without being tracked. So an evicted tombstone can never be replayed. Open turns are never
  evicted; an unknown id that cannot be tracked (every slot an open turn) is not retired, so the gate keeps it reserved.

**The fence** (#126 review round 4, P1b). The gate's admissions and retiring tickets live in its process only, and its
LaunchAgent restarts it (`KeepAlive`) while vllm-mlx runs on. The reviewer's repro: an old 64k turn survives a gate
restart, and the fresh gate admits two more 64k turns, 28.8 GiB computed against 25 GiB. So a gate on a vllm-mlx text
node starts **fenced**:

- it admits no text turn (529 busy, `Retry-After`, the reason in the message and in the capacity report's `fence`)
  until the guard answers `POST /cortados/fence {"epoch": <this gate's>, "known": [ids this gate minted and has
  not released]}` with `"clear": true`;
- the guard, under the same locks as the safe point, makes that epoch the only one admitted (no id of a previous
  gate is admitted or bound any more, whatever either clock says: a request a dead gate sent late cannot slip in
  afterwards), and answers clear only if no request bound to a ticket outside `known` is pending, queued or in the
  scheduler and no such exchange is open. It counts a request as present until a safe point has seen it leave, so it
  errs towards fenced. A gate whose epoch is below the guard's (a newer gate fenced it) stays fenced;
- the gate fences itself again whenever it cannot vouch for the backend's state: a retirement poll or a fence query
  failed (lost contact), the guard's boot id changed, or the guard refused a turn's ticket (409). A changed boot id is a new vllm-mlx process: the old rows went
  with the old process, so with only this gate's own turns known the fence lifts at once, and the new process then
  retires every old id (the restart path above);
- a hung or unreachable guard keeps the fence up, visibly (`fence`: "the backend's guard did not confirm the fence");
  the Retirer thread retries with the same back-off, and a request arriving while fenced triggers one query at most
  every 2 s after a failure (a burst shares the one running).

Chosen over the alternative of reserving the backend's reported rows at full width: that needs a per-row width or a
worst case per row and a second reservation path in the governor, while the fence needs one query and admits nothing
it cannot account for. Its cost is a pause after a gate restart while a previous gate's turns finish.

**Before enabling vision**: a vision model's turns are still released at **transport end**. The MLLM scheduler has
the same deferred-abort shape, so a vision turn's rows can outlive its connection exactly as the text route's did
before retirement. Vision (#125) must not be enabled on the Pro until its route carries tickets and the retirement
protocol (the ledger, the safe point after the MLLM scheduler's step, the fence) too. This is not a text-only merge
blocker (#126 review round 4).

The gate and the guard ship together: an old guard answers 404 there, so every ticket would stay retiring and the node
busy (and an old guard has no fence endpoint, so the gate would stay fenced). A vision model's turns are unchanged
(#125): its guard has no ledger and the gate releases them as before (see "Before enabling vision" above).
`fit_tokens` / `free_tokens` in the capacity report are computed from
the same set formula (`text_room`), so the provider's skip check sees the new room. Short turns are unchanged: their
old reservation was already at least the computed peak, so it stands byte for byte (eight ~2k-token turns still run
at once). A vision model's turns, text-only ones included, reserve exactly as in #125 (`text_route` is false there).

**Measured against the hardware (2026-11-22).** These were computed bounds until the hardware test on the owner's
Pro (4-bit Qwen3.8 27B, vllm-mlx 0.6.0 under the guard, 256-token text prefill, prefix cache off). Two cases are now
measured. "Over idle" is the process footprint (including the kernel's recorded peak) above the idle footprint with
the weights loaded:

| case | measured peak over idle | `mx.get_peak_memory()` over idle | reserved (#126 as merged) | reserved (corrected) |
| --- | --- | --- | --- | --- |
| one full turn alone, 130,872 tokens | **11.93 GiB** | 10.59 GiB | 24.54 GiB | **17.06 GiB** (5.13 GiB, 43 %, above the measured peak) |
| eight short ~2k turns at once | **3.33 GiB** | — | 14.32 GiB | 14.32 GiB (unchanged: the per-request floor) |

**What the full turn shows: the KV term counted the batch twice.** The per-token KV was right: 65,536 bytes, from
the 16 full-attention layers only (`TEXT_FULL_ATTENTION_LAYERS` = `num_hidden_layers` 64 / `full_attention_interval`
4; the 48 linear-attention layers keep fixed-size state, `per_seq_state_bytes`), so 8 GiB at 128k. But the kv term
was `kv_copies` (2) × the whole padded batch, 16.03 GiB, and alone that is above the measured 11.93 GiB. A turn's KV
is 8 GiB of it, the rest (3.9 GiB) the prefill chunk, state and cache. So the whole batch was never held twice.
That fits how the replacements happen: the cache grows by 256 positions (512 times in that turn), joins, filters and
extracts **one layer at a time**. Each layer's `KVCache` concatenates or gathers its own arrays, and MLX returns the
old buffer once its replacement is evaluated. The term is now the padded batch once plus `kv_copies` − 1 replaced
copies of one layer's slice: n × M × 65,536 × (1 + 1/16). At 128k that is 8.52 GiB instead of 16.03 GiB.

**The margin that remains** at 128k (17.06 reserved against 11.93 measured, 5.13 GiB) is in the other terms, which
are kept as they were: four score buffers of the last chunk (6 GiB; the measurement leaves room for about two of
them live at once beside the KV), the buffer cache plus one overshooting score buffer (2 GiB), and a whole layer's
replaced slice. It covers MLX batching several layers' replacements into one command buffer before their old buffers
return: up to ten layers' worth fits in it. The decode padding term is kept (measured to be real: a short row beside
a long one is held at the long one's width).

**The short turns** stay reserved at the per-request floor (`prefill_margin_bytes`, 1.5 GiB each): eight ~2k turns
reserve 14.3 GiB for a measured 3.33 GiB. That floor is not the text formula's to change here. Lowering it for the
text route is a separate decision (it would let more short turns run while the owner works).

The cases not yet measured are a 64k turn beside short ones and the 6-bit copy. If a measured peak ever exceeds its
reservation, the counts or the chunk are corrected before relying on them.

**The prefix cache is off on the text route** (#126 review round 2, P1). vllm-mlx's own prefix cache
(`MemoryAwarePrefixCache`, on by default, `scheduler.py` 93-99 and 1402-1445) keeps finished prompts' KV as separate
copies, up to 20% of the RAM available when it starts (`--cache-memory-mb` unset), across requests. That pool was
outside every reservation: with ~4 GiB in it, a full-context turn's 24.57 GiB plus the pool is ~28.6 GiB beside the
weights, over the 25 GiB budget (since the 2026-11-22 KV correction: 17.06 + 4 = 21.06 GiB, over the 17.25 GiB
owner-active budget). Its insertions are unbounded too: `memory_cache.py` `commit_prepared` (1551-1568)
materialises a turn's new snapshot before it evicts older entries.

So the text route serves with it off:

- `local_backend.text_argv` gives vllm-mlx `--disable-prefix-cache` (`PREFIX_CACHE_OFF`), so the generated
  LaunchAgent always carries it. In 0.6.0 (`cli.py` 319-328) `enable_prefix_cache` is `--enable-prefix-cache` (default
  on) and not `--disable-prefix-cache`, and no flag turns it back on.
- The guard refuses to start the text route without that exact argument (an abbreviation such as `--disable-prefix`,
  which argparse would accept, is refused too). At runtime it wraps `Scheduler.__init__`: a Scheduler built with no
  config (0.6.0's default enables the cache) or `enable_prefix_cache` not False is refused before it builds anything,
  and one that still ends up holding a `prefix_cache`, `memory_aware_cache`, `block_aware_cache`, `paged_cache_manager`
  or SSD tier is refused after. Either stops the engine from starting.
- With no pool, cached earlier turns cannot add to the process's footprint: what runs beside the weights is the
  reservation.

**The trade-off.** Without the cache, every turn prefills its whole prompt. A multi-turn conversation's next turn
re-reads the earlier turns instead of reusing their KV, so the **first token comes later on multi-turn
conversations**, roughly by the prefill time of the history (at 256-token chunks, one prompt at a time). Output tokens
per second and the answers themselves do not change. The owner is asked to confirm "cache off".

**No "cache on with a reserved budget" option.** The obvious alternative is a fixed `--cache-memory-mb` that the
governor subtracts from the budget, plus one snapshot's transient. It is left out because it is not sound against the
released code without a further read: the pool's accounting estimates an entry from its arrays' sizes, while an entry
cut from a filtered batch row can be a slice that keeps the whole padded buffer alive, so the pool's real footprint
can exceed its limit. And the room it would cost is large: a full-context turn on the 4-bit model fits only while the
pool is ≤ 0.43 GiB. If prefix reuse is wanted later, it needs its own issue that reads `memory_cache.py`'s estimate
and eviction and bounds both. The vision route is unchanged (#125): its MLLM scheduler's text-only prefix cache is not
touched here.

### Vision: still images on the MLX lane (#125)

> **Before enabling vision** (#126 review round 4): vision turns are still released at transport end; they need the
> retirement protocol (tickets, the ledger, the safe point, the fence) before vision is turned on. See "Before
> enabling vision" under the text route's reservation above.

Owner decisions 2026-11-15/16: the Pro's model may read images, served from a separate folder holding the 4-bit language
weights plus the BF16 vision tower (`~/models/mlx/Qwen3.8-27B-heretic-ara-4bit-vision`, `qwen3_5`,
`Qwen3_5ForConditionalGeneration`). **The production switch needs the owner's separate approval after measurements**;
until then the text-only entry stays the served one, and nothing below changes a running node.

**The policy** (a model entry's `vision` block in the backend config; `local_governor.VISION_DEFAULTS` has each reason,
the example's `qwen-27b-4bit-vision` entry repeats them):

| setting | initial | what it bounds |
| --- | --- | --- |
| `max_images` | 5 | still images per request (counted over the whole conversation the client resends) |
| `image_tokens_max` | 1,024 | tokens per image: ~1 MP after the processor's resize (1,024 pixels per token); the guard sets the processor's `max_pixels` to 1,048,576 (upstream: 16.7 MP = 16,384 tokens) |
| `image_context_tokens` | 65,536 | input + max_tokens of an image-bearing request, until measured |
| `image_concurrency` | 1 | image-bearing requests at once, until measured (text turns still run beside it) |
| `prefill_chunk_tokens` | 256 | the guard's media prefill chunk (`--image-prefill-tokens`); bounds the attention scores (below) |
| `score_buffers`, `act_bytes` | 4, 2 | the formula's allocator and dtype counts (below); `act_bytes` is fixed with the geometry |
| model dimensions | Qwen3.8 27B | `hidden_size` 5,120, `intermediate_size` 17,408, 24 attention / 4 KV heads, `head_dim` 256, linear attention 16 key / 48 value heads of 128, `vocab_size` 248,320, tower 1,152 / 4,304 with 16 heads (`vision_num_heads`) and 27 blocks (`vision_depth`); **fixed** (the one verified geometry, below: a block may repeat them, never change them); checked against `config.json` and the loaded tower at startup (`--image-dims`) |
| `max_image_bytes` | 5 MiB | encoded bytes per image (the Anthropic API's own limit) |
| `max_image_pixels` | 32 Mi | decoded pixels per image, read from the header before decoding; PIL's bomb limit is the same |

Only inline base64 PNG, JPEG and WebP **stills** are taken, in a user message's own content. Refused, before any
admission and non-retryably (the gate's `UNSUPPORTED_MESSAGE`, which the provider routes elsewhere as before): url
sources (nothing is fetched: no SSRF), GIF, animated PNG/WebP, multi-picture JPEG, a declared type that is not the real
one, video, audio, documents, images in the system prompt or an assistant turn, and **images inside a `tool_result`**
(vllm-mlx 0.6.0's adapter keeps only a tool result's text, so such an image would be dropped silently; failing
explicitly is the rule). `cortados_router/image_check.py` holds the header checks; the gate and the guard both use it.

**Memory** (KV cache 65,536 B per token, "The governor" above). One image at its ceiling is 1,024 tokens = 64 MiB of KV;
five are 320 MiB. An image-bearing request reserves

    tokens   = min(text input bound + images x 1,024 + max_tokens, 65,536)
    reserved = tokens x 65,536 + 0.15 GiB state + image_prefill_bytes(tokens, images)

where the text input bound is "The input bound" with the validated base64 left out (it is not text the model reads; a
1 MB screenshot is ~1.3 MB of base64 and at most 1,024 tokens). `image_prefill_bytes` (`local_governor`) is the
computed worst case of the guard's media prefill below, not a fixed margin (#125 review round 2 found the fixed
1.5 + 2 GiB margins far below the released prefill's real peak).

**Why a formula, and which one.** vllm-mlx 0.6.0 prefills an image-bearing prompt in ONE forward
(`mllm_batch_generator.py` 2233-2261 → `_run_vision_encoding` 1615-1662: `self.model(input_ids, cache=cache, ...)`
with no `logits_to_keep`), so mlx-vlm 0.7.12's qwen3_5 language model projects EVERY position to the 248,320-word
vocabulary (`language.py` 1910-1920, `models/base.py` `project_logits` 64-80) before the scheduler keeps the last row:
27.8 GiB of bfloat16 logits for a ~60k-token prompt with one tiny image, which passed both guard layers and reserved
7.65 GiB. Unchunked, its full attention (head_dim 256, outside MLX's fused kernel for 64/80/128,
`models/base.py` `ensure_fused_sdpa`) would also hold heads x P x P scores. So the guard replaces that forward
("Media prefill" below): chunks of `prefill_chunk_tokens`, the last position's logits only. What remains is bounded
per term, and the reservation is their sum (P = tokens, C = min(256, P), T = images x 1,024 image tokens, N = 4T vision
patches, a = 2 bytes; H 5,120, I 17,408, Hq 24, Hkv 4, D 256, linear Kh 16 / Vh 48 of 128, V 248,320, tower Hv 1,152 /
Iv 4,304, Wp 1,280 = the tower's padded attention width, below):

| term | bytes | what it holds |
| --- | --- | --- |
| sequence | P x (3aH + 2H + 64) | the whole prompt's embeddings, live for the whole prefill: `embed_tokens`' output, `masked_scatter`'s merged copy and one more, the image mask broadcast to P x H and its numpy copy, ids and mRoPE position ids |
| media | T x H x (a + 12) + N x (1,536 x (4 + a) + a x (13 Hv + 4 Wp + 2 Iv) + 24 Hv) | merged image features and the scatter's int64/uint32 indices; float32 pixel rows and their bf16 copy; one tower block's activations per patch: 13 unpadded (residual in, two norms, qkv x 3, rotated q and k, the attention output concatenated and reshaped, projection, residual sum, MLP out), q / k / v padded to 80 and the fused kernel's padded output (4 Wp), MLP in and its activation (2 Iv); the rotary embedding's float32 temporaries (three Hv-wide float32 arrays for each of q and k) |
| kv_regrow | P x 65,536 | `KVCache` grows in 256-token steps by concatenation; the replaced buffers wait in MLX's allocator cache until the guard clears it after each chunk |
| scores | 4 x Hq x C x P x a + C x P | one chunk's unfused attention against the whole context (q·kᵀ, masked copy, softmax, one held by the allocator) and the causal mask |
| chunk | 2 x C x a x (3I + max(3 Hq D + 2 Hkv D, 4 (Kh Kd + Vh Vd)) + 4H) | one layer's activations for a chunk (MLP gate/up/product; attention or linear-attention projections; residual and norms), doubled for the allocator |
| logits | V x (a + 8) | the one kept position's logits and float32 log-probabilities |

**The encoder's attention: one verified geometry** (#125 review rounds 3-4). The tower (mlx-vlm 0.7.12
`qwen3_vl/vision.py`, which `qwen3_5/vision.py` only inherits) runs every one of its blocks as full attention over one
image at a time (`cu_seqlens` splits; `window_size` and `fullatt_block_indexes` are read into its config and used by no
block) through `ensure_fused_sdpa` (`models/base.py`, pinned), which zero-pads q, k and v from head_dim d to the first
of 64 / 80 / 128 that is ≥ d, calls `mlx.core.fast.scaled_dot_product_attention` and slices the output back to d.
Each review round found a geometry the general formula did not bound: round 2, 8 heads (head_dim 144, past 128, so
MLX's unfused fallback held 8 x n x n scores, 16 GiB at n = 32,768 patches); round 3, 288 heads of 4, which pass a
"head_dim ≤ 128" rule but are padded to 64, so q, k, v and the output take 288 x 64 = 18,432 per patch, not 1,152
(4.5 GiB at 32,768 patches against 4.18 GiB reserved). So the class is closed by restriction, not by a more general
formula: vision mode serves **exactly one** geometry, the Pro's (`config.json` checked by the orchestrator 2026-11-16):

| where | must be exactly |
| --- | --- |
| `vision_config` | `depth` 27, `num_heads` 16, `hidden_size` 1,152, `intermediate_size` 4,304, `patch_size` 16, `spatial_merge_size` 2, `temporal_patch_size` 2, `out_hidden_size` 5,120 (every key present: mlx-vlm's `VisionConfig` defaults differ), `in_channels` 3 (its default when absent), no `deepstack_visual_indexes` |
| `text_config` | the language dims of the policy table above (`hidden_size` 5,120 … `vocab_size` 248,320) |
| `--image-dims` / the `vision` block | those values and `act_bytes` 2 (`guard.VISION_DIMS_ALLOWED` = `local_governor.VISION_DEFAULTS` over `VISION_FIXED_KEYS`) |

Refused otherwise at every check point: config load (`local_governor.check_vision`: a `vision` block may repeat a
dimension but not change it), `--check` and `serve` (`VisionPolicy`: `--image-dims` must be the allowlist, before
anything loads), route resolution and `--check` (`check_vision_model`: `config.json`), and when `MLLMScheduler` is
built (`vision_tower_problem`: 27 blocks, each `attn` 16 heads of 72, the patch geometry). There is no "any head count
that divides evenly" acceptance any more. **A future model with another geometry is added to both allowlists
explicitly, with its own reservation argument** (its pad target, its fused-kernel proof, its per-patch count); nothing
generalises to it.

The padding, for the allowed geometry: d = 1,152 / 16 = 72; `ensure_fused_sdpa`'s first fused dim ≥ 72 is 80, so q, k,
v and the kernel's output are 16 x 80 = **1,280** wide (`VISION_PADDED_WIDTH`), 128 more per patch than 1,152. Per
patch and block, at a = 2: a x (13 x 1,152 + 4 x 1,280 + 2 x 4,304) + 24 x 1,152 = 2 x 28,704 + 27,648 = 85,056 bytes
(round 3 counted 2 x (12 x 1,152 + 2 x 4,304) = 44,864: no padding, no float32 rotary temporaries, no residuals), plus
the pixel rows' 1,536 x 6 = 9,216: 94,272 bytes per patch, 368.25 MiB per 1,024-token image (4,096 patches), plus its
1,024 x 5,120 x 14 = 70 MiB of merged features and indices. The encoder holds no heads x n x n scores (the kernel is
fused), so there is no `encoder_scores` term. An `image_tokens_max` override is accepted only while its computed peak
(this padded count, linear in the patches) fits: config load refuses a policy whose one-image turn could not be
admitted beside the weights (e.g. 32,768 tokens in a 20 GiB budget).

**MLX is pinned too** (round 4, P2): `ensure_fused_sdpa` only pads; whether `fast.scaled_dot_product_attention`
dispatches to the fused kernel or an unfused fallback is decided in MLX itself. So vision mode requires `mlx` 0.32.4 and
`mlx-metal` 0.32.4 (the Pro's installed versions), checked by `verify_mlx` at `--check` and `serve`: the installed
dist-info's version, its single `WHEEL` tag (which must have a pinned row; mlx's and mlx-metal's macOS tags must agree),
the `RECORD` entries of the files that implement the dispatch, equal to the released wheels' — `mlx/core.cpython-*-darwin.so`
(mlx), `mlx/lib/libmlx.dylib` and `mlx/lib/mlx.metallib` (mlx-metal) — and those files' own sha256 equal to them; and the
`mlx.core` actually imported must be that pinned file, beside that `libmlx.dylib`. MLX's Python side is pinned too
(#126 review round 2, P2b; `MLX_PY_SHA256`): the models import `mlx.nn`, whose `__init__` imports every module under
`mlx/nn` (activations, `Linear` / `QuantizedLinear`, `RMSNorm`, `Embedding`, `Module`, ...), and `mlx.utils`, and the
extension imports `mlx._reprlib_fix` and `mlx.__array_api_info`. Those 24 files (`utils.py`, `_reprlib_fix.py`,
`__array_api_info.py`, `nn/__init__.py`, `nn/init.py`, `nn/losses.py`, `nn/utils.py` and the 16 files of `nn/layers/`)
must equal the released wheels' bytes beside the pinned `mlx.core`. They are byte-identical in all 27 macOS arm64
wheels (fetched from PyPI 2026-11-17), so every tag's row is one table. `mlx` must stay a namespace package (no
`mlx/__init__.py`, which would run on import), and `mlx.nn`, `mlx.utils`, `mlx._reprlib_fix` and `mlx.__array_api_info`
must resolve to the pinned files. A drifted file, such as `silu` promoting to float32, stops startup. `mlx.nn` itself
is a regular package in every 0.32.4 wheel, so its `nn/__init__.py` is pinned by hash rather than required absent.
Any mismatch fails closed. **Order** (#126 review round 3, P2b): every one of these checks reads the disk only, and
all of them run before anything is imported (`_preflight`: vllm-mlx's pinned files, its package `__init__.py` now
among them; mlx-lm's or mlx-vlm's; mlx's and mlx-metal's), each package located through `find_without_import` (the
import system's own finders, asked directly) and the dist-info `RECORD`s, neither of which runs package code. A dotted
name is never handed to `importlib.util.find_spec`, which imports the parent first (#126 review round 4, P2a: a shadow
`mlx/__init__.py` earlier on `sys.path` ran before it was refused): `mlx` must resolve as a top-level name to the
namespace package whose locations include the pinned folder, and `mlx.nn`, `mlx.utils`, `mlx._reprlib_fix`,
`mlx.__array_api_info` and `mlx.core` are resolved within those locations to the pinned files. Only then are they
imported, and
each import is checked to come from the folder verified (and `mlx.core` to be the pinned extension). Before, importing
vllm-mlx's scheduler pulled in `mlx.core`, `mlx_lm` and `mlx.nn` ahead of their checks, so a line planted in
`mlx/nn/__init__.py` ran even though startup then refused. The
expected hashes come from the released PyPI wheels (fetched 2026-11-16; each RECORD hash re-checked against the file
bytes in its wheel). The Pro's Python and macOS tag could not be read from the build machine, so every macOS arm64
wheel of 0.32.4 is pinned: CPython 3.10–3.15 and the free-threaded 3.13t–3.15t (27 mlx rows), macOS 14.0 / 15.0 / 26.0
(3 mlx-metal rows); the installed `WHEEL` tag picks the row. A Linux or Intel build, another version or a rebuilt
binary is refused.

At the 64k image context with 5 images: sequence 2.50, media 2.14, kv_regrow 4.00, scores 3.02, chunk 0.10, logits
0.002 GiB = 11.76 GiB, so the largest image reservation is 4 GiB KV + 0.15 GiB + 11.76 GiB = **15.91 GiB** (the
reviewer's 60k tokens + one image: 13.48 GiB). Beside ~16.5 GiB of weights (15 GiB language + the tower, provisional:
measure it) in the 40 GiB budget, 23.5 GiB remain: one image turn at its cap fits alone, and beside a full 128k text
turn (9.65 GiB) the two reserve 25.56 GiB, which fits neither the 23.5 GiB nor the 25 GiB of the 15 GiB `weights_bytes`
default, so the second is a retryable 529 (a short image turn still runs beside a full text turn). A request whose
reservation exceeds the budget
beside the weights is never-fits (413, non-retryable): the provider routes it elsewhere. The chunk is the lever: the
score term is linear in it (1,024 would make it 12 GiB at 64k). The count slot, the bytes and the image slot are taken
in one step under one lock (`Admission.try_acquire(reserve, image=True)`); busy (529, retryable) and never-fits (413,
non-retryable) keep their meaning, and a reservation is released however the turn ends. Text-only turns, on either
model, reserve exactly as before.

**These numbers are a computed bound, not a measurement.** The acceptance gate is the real-model peak measured in the
hardware test window: the 64k image context with 5 images at ~1 MP, and the reviewer's ~60k tokens + one small image,
each read as the process's peak footprint (and MLX's `get_peak_memory`) against its reservation. If a measured peak
exceeds its reservation, the formula's counts (`score_buffers`, the allocator doublings) or the chunk are corrected
before the vision entry is served; the owner's production switch waits for that measurement anyway.

**The guard's vision mode** (`--vision` and its flags, built by `local_backend.vision_argv` from the model entry). On an
MLLM model every request, text-only included, goes `BatchedEngine` → `MLLMScheduler` → `MLLMBatchGenerator`, so the text
route's cap never runs; the MLLM route counts text tokens only (`mllm_scheduler.py:486`), and the real length is known
only after `mlx_vlm.utils.prepare_inputs` expands each image placeholder. The guard extends itself, never bypassed:

- route: only a model dir whose `config.json` is `qwen3_5` / `Qwen3_5ForConditionalGeneration` with a `vision_config`,
  only the MLLM route, only BatchedEngine (the engine refusal is unchanged); in vision mode the text `Scheduler` refuses
  every request (a vision model must never be served by the text route, whose chat drops images);
- startup (`MLLMScheduler.__init__`): the model and processor are mlx-vlm's `qwen3_5.Model` / `Qwen3VLProcessor` with 16 x
  2 patches; drafters and specprefill refused; the processor's `max_pixels` overridden;
- layer 1 (`MLLMScheduler.add_request`, the one entry to the MLLM queue, before anything is queued): images checked from
  their bytes, one placeholder per image, then the exact length P = text tokens − placeholders + Σ the processor's own
  `num_image_tokens(h, w)` (the function its preprocessing shares, `processing_qwen3_vl.py:459-488`); P + max_tokens
  over the context (64k with images) is the text route's 413, which the gate turns into its never-fits 413;
- layer 2 (`MLLMBatchGenerator._preprocess_request`, every path to the vision encoder and prefill: the normal batch,
  mid-batch extend, chunked prefill, the early text preprocessing, the pixel cache's hit branch and its miss branch):
  after `prepare_inputs`, the actual P and the placeholder, `image_grid_thw` and pixel-row counts must agree. A
  refusal releases the request's tensors and fails only that request (a mismatch left to the model would raise inside
  prefill and fail every request of its batch);
- decode (`models.mllm.process_image_input`): checked `data:` URLs only, inside layer 2 only, to a private temp file
  removed after preprocessing (upstream downloads http(s) URLs and caches every base64 image as a temp file until exit);
- caches: `VisionEmbeddingCache` (count-only, 100 / 50 entries) is disabled. The prefix cache serves text-only requests
  only (`memory_cache.is_text_only_prefix_cache_request`), unchanged;
- media prefill (`MLLMBatchGenerator._run_vision_encoding`, the one forward of an image-bearing prompt; guard
  `media_prefill`): mlx-vlm 0.7.12's own chunked VLM prefill (`generate/ar.py` 422-517, which `LanguageModel.
  chunked_prefill_policy` enables for qwen3_5 without a drafter) on vllm-mlx's cache: the embeddings once, exactly as
  `Model.__call__` builds them (`qwen3_vl.py` 224-233: tower, merge, mRoPE positions, `rope_deltas`), then the language
  model over chunks of `--image-prefill-tokens` with `logits_to_keep=1` and the chunk's slice of the positions; the
  cache evaluated and MLX's buffer cache cleared between chunks; the result checked to be one position. Same result:
  `logits_to_keep` slices only what the vocabulary projection sees, after every position's K/V and recurrent state is
  in the cache (`project_logits`: "never to caches"), and the projection is row-wise; chunking is causal attention
  against cached keys plus the gated-delta state carried in the cache, the path vllm-mlx's text prefill and mlx-vlm's
  generate take for this model. Pinned: `qwen3_vl.py` joins the mlx-vlm pins, and startup stops unless the generator
  class has the seam, its module `_eval_prompt_cache` / `PrefillAbortedError` / `mx.clear_cache`, and its model is
  qwen3_5's `Model` with its `LanguageModel`; `config.json` must have exactly the `--image-dims` the reservation used.

Pinned on top of the text route's pins: vllm-mlx 0.6.0's MLLM files (`VISION_PINNED_SHA256`) and mlx-vlm 0.7.12
(`MLX_VLM_PINNED_SHA256`, from the PyPI wheel `mlx_vlm-0.7.12-py3-none-any.whl`, sha256 `0412a883…8e37`); any drift stops
startup. The full set, by sha256 from the released wheels:

- vllm-mlx 0.6.0: `mllm_scheduler.py`, `mllm_batch_generator.py`, `vision_embedding_cache.py`, `models/mllm.py`,
  `api/anthropic_adapter.py`, `api/utils.py`, `memory_cache.py`, `multimodal_processor.py`, `patches/qwen3_5_mllm.py`
  (and the text route's `PINNED_SHA256`);
- mlx-vlm 0.7.12, the processor and loader: `version.py`, `utils.py`, `models/qwen3_vl/processing_qwen3_vl.py`;
- the model: `models/base.py` (`ensure_fused_sdpa`, `scaled_dot_product_attention`, `project_logits`),
  `models/qwen3_vl/qwen3_vl.py`, `models/qwen3_5/__init__.py`, `models/qwen3_5/qwen3_5.py`,
  `models/qwen3_5/language.py`, `models/qwen3_5/config.py`, `models/qwen3_5/speculative_verifier.py`,
  `models/qwen3_vl/__init__.py`, `models/qwen3_vl/language.py`;
- the vision encoder (round 3): `models/qwen3_5/vision.py` (inherits only) and the real encoder
  `models/qwen3_vl/vision.py`, its `VisionConfig` defaults `models/qwen3_vl/config.py`;
- the linear (recurrent) attention: `models/qwen3_5/gated_delta.py`;
- the caches and the attention dispatch's cache types: `models/cache.py`, `turboquant.py`, `kv_quant.py`;
- the rest of the import chain: `models/rope_utils.py`, `models/activations.py`, `models/fast_ops.py`,
  `models/linear.py`, `models/mlp.py`, `models/switch_layers.py`, `models/quantized_verifier.py`,
  `models/exact_speculative_verify.py`, `quantization/nvfp4.py`, `speculative/cache_state.py`,
  `speculative/ops/linear.py`, and the package files `__init__.py`, `models/__init__.py`, `speculative/__init__.py`,
  `speculative/ops/__init__.py`, `quantization/__init__.py`.

Not pinned: the lazy `deepseek_v4/hyper_connection.py` import inside `switch_layers.py`'s DeepSeek-only branch, the
modules `mlx_vlm/__init__.py` pulls in for generation, conversion and training, and the drafters `speculative/__init__.py`
imports (the guard refuses drafters). MLX (`mlx` and `mlx-metal` 0.32.4) is pinned separately, above. `scripts/install_mlx_backend.sh` copies `cortados_router/image_check.py` beside the guard as
`cortados_image_check.py` and runs `--check` with the entry's vision flags.

**The provider** relays the client's `/v1/messages` body to the node unchanged (images included). On the provider
host, give the node's model `"vision": true` (or `{"max_images": 5, "image_tokens": 1024}`) in `tables/resources.json`
so the local lane's fit checks (`local_lane.input_tokens`: the pin, the auto and index routes, every turn) count each
image as its tokens rather than its base64 as text; without it a screenshot's base64 alone exceeds the 131k context.
The gate's capacity report carries the served model's `vision` settings and `image_inflight` / `image_limit`.

**Switching it on** (owner steps, after the measurements are approved): select `qwen-27b-4bit-vision` as
`vllm_mlx.model` with its measured `weights_bytes`, rerun `scripts/install_mlx_backend.sh` (its `--check` must print
`vision route qwen3_5`), restart the gate, and declare `vision` on the provider host's model entry. Rolling back is
selecting the text entry again.

**Not covered yet**: images inside tool results (refused, so Claude Code's image reads go elsewhere); GIF; a client whose
max_tokens leaves no room under the 64k image context (Claude Code's 32k default leaves ~32k for text and images; a
larger max_tokens is refused by the guard and the turn routed elsewhere); the image reservation's formula, the tower's
resident size and the concurrency of one are provisional until measured; text-only prompts on a vision model still
reserve the per-request formula (#126 bounds the text-only model's text route, above; a vision model's text turns go
through the MLLM route, which #126 did not read); layer 2's refusal reaches a client as vllm-mlx's generic
generation error (layer 1 refuses everything layer 2 would, so it is a backstop, not a path).

### Switch the Pro to MLX

Owner steps; nothing here was run by the build. The provider's lane config needs no protocol change.

1. On the Pro: copy `tables/local-backend.example.json` to `~/.config/cortados/local-backend.json` and check it:
   `venv` (`~/venvs/mlx-bench`), `vllm_mlx.model` and its entry's `path` (the model directory; a 6-bit copy is another
   entry, selected here), `served_model_name` = the id the Air's config gives this model (`Qwen27b_heretic:latest`),
   and `serve.tool_call_parser` (set it to the parser the bench verified tool calls with). Validate:
   `python3 -B -m cortados_router.local_backend --print-config`.
2. On the Pro: `scripts/install_mlx_backend.sh --dry-run`, then `scripts/install_mlx_backend.sh` (LaunchAgent
   `com.cortados.vllm-mlx` on 127.0.0.1:8000 under the prompt guard, log `~/.cortados/vllm-mlx.log`). Both run the
   guard's `--check`: it must print `ok (vllm-mlx 0.6.0, mlx-lm 0.31.3 and mlx / mlx-metal 0.32.4, pinned files match, only BatchedEngine may serve, text route)` (#126: the text route also needs mlx-lm 0.31.3, mlx / mlx-metal 0.32.4 and the verified text geometry in `config.json`). An already-installed LaunchAgent from before #126 runs without the text prefill bounds: reinstall it, then restart the gate. Wait for `curl -s
   http://127.0.0.1:8000/health`; the log's first line names the cap (`prompt + max_tokens capped at 131072 tokens`).
   An already-installed LaunchAgent from before the guard runs vllm-mlx without it: reinstall it before the gate runs
   with this version.
3. On the Pro: unload Ollama's 27B so both copies are not held in memory: `ollama stop Qwen27b_heretic:latest`.
   Nothing reloads it: the gate no longer sends requests to Ollama. (Quit Ollama too if no other use needs it.)
4. On the Pro: restart the gate so it reads the config: `launchctl kickstart -k gui/$(id -u)/com.cortados.local-gate`.
   Check: `python3 -m cortados_router.local_gate --listen <tailscale-ip> --print-config` shows `"backend":
   "vllm-mlx"` and the governor.
5. On the provider host (the Air), in `tables/resources.json`'s Pro block: `max_inflight` to the idle cap (8), so the
   provider's own ceiling does not stop at 1 (the gate's governor sets the real limit, by count and by bytes), and `read_timeout_s` to 900
   (as the backend config's). Restart the provider when the owner chooses.

With the gate on vllm-mlx the Pro serves only the MLX model: the Ollama-only pins (`qwythos-9b`, `qwen-abliterated`,
`phi-4`) are "does not list" (a clear 503) until the rollback.

### Roll back to Ollama

1. On the Pro: set `"backend": "ollama"` in `~/.config/cortados/local-backend.json` (or remove the file) and restart
   the gate (`launchctl kickstart -k gui/$(id -u)/com.cortados.local-gate`).
2. On the Pro: `scripts/install_mlx_backend.sh --uninstall` (boots `com.cortados.vllm-mlx` out, frees its memory);
   Ollama loads its 27B again on the next request.
3. On the Air: `max_inflight` back to 1 (Ollama runs one 27B request at a time) and `read_timeout_s` as before.

## PC node (Strata on the Windows gaming PC, #145)

Option A of `docs/runbooks/PC-RTX3060-ENROLLMENT.md` (owner decisions 2026-11-24): the owner's Windows PC (RTX 3060
12 GB, 32 GB RAM) is a second local model node, `pc`, beside the Pro. It is a model server only, never an execution
host. It serves **only while the owner is not using it**, and the owner can switch it off when he starts a game.

### Shape

```
provider (Air) --tailnet--> local gate on the PC (<pc-ts-ip>:11435, Python 3.12) --loopback--> Strata (127.0.0.1:8080)
```

- The gate is the same `local_gate.py` as the Pro's, run on Windows through `cortados_router/gate_platform.py`, the one
  seam for what differs: `msvcrt.locking` for the epoch file's lock, no `O_NOFOLLOW` (a reparse point is refused from
  `lstat` first), no directory fsync, and the token file's owner and DACL in place of its mode (`icacls`: read for the
  gate's account, full for SYSTEM and Administrators, inheritance removed; anything granting Everyone, Users,
  Authenticated Users or another account is refused). The S1-training node lock is fcntl-only and the PC trains
  nothing, so the gate runs with `--no-studio-gate` (required on Windows: the gate refuses to start without it). On
  macOS each seam call does exactly what the gate did before.
- Backend `strata` (`local_backend.py`; example `tables/local-backend.strata.example.json`): the same two forwarded
  routes; `/api/tags` answered from Strata's `/v1/models` as the configured served name only; the model name enforced
  (Strata serves any name it is sent); `thinking` passed through (Strata started with `anthropic_thinking:
  on_request`); Strata's 503 "GPU in use" and 429 queue timeout turned into the gate's retryable 529. Strata allocates
  the KV cache for its whole context at load, so there is no per-request byte governor: admission is a count,
  `strata.max_inflight` (1), and `max_tokens` at or over the context is refused as never fitting. Strata's control
  endpoints (`/load`, `/unload`, `/v1/vram`, `/slots`, `/mcp`) are never forwarded.
- **The model is configuration, not code** (the choice is not final): `strata.models.<key>` on the PC (served name,
  `context_tokens`, `upstream_ids`) and the `pc` node's `models[]` on the provider host
  (`tables/resources.pc-node.example.json`: id, alias, `context_length`). The shipped default is Qwen3.6-35B-A3B
  UD-Q4_K_XL at 48K (measured 30.8 tok/s), pin-only: `auto_route` and `index_route` false, and no proxy-index variant
  (`tables/local-models.json` → `index_proxies.not_mapped` says why), so S1 never picks it until the owner sets a
  deduction (runbook step 6). The Qwen3.8 Coder 125B is left out.

### When the PC serves (`node_policy.py`, kind `windows-idle`)

A gate whose backend config has a `node_policy` decides with it instead of `pro_local_allowed`. In order, each refusal a
fixed reason the provider may repeat (`node_policy.REFUSALS`, kept in step with `local_lane.PRO_LOCAL_REFUSALS`):

| check | refused with | threshold and reason |
| --- | --- | --- |
| manual switch (same flag file as the Pro) | `local switch off` | the owner's toggle |
| AC power (GetSystemPowerStatus) | `on battery` / `unknown power source` / `power check failed` | `require_ac: false` for a desktop that reports unknown |
| signed-in sessions (WTSEnumerateSessions, from session 0) | `owner activity unknown` when unreadable | none active (signed out, disconnected remote desktop) = away |
| activity file, when a session is active | `owner active` / `full-screen app running` / `owner activity unknown` | `idle_after_s` 300: shorter would serve under a paused game; the writer writes every 5 s and `activity_stale_s` 20 allows four misses |
| GPU (nvidia-smi `utilization.gpu`) | `GPU busy with another program` / `GPU state unknown` | `gpu_busy_pct` 40: compositing, an animated wallpaper and video decode stay well under it; skipped while the gate's own turn runs and `own_gpu_grace_s` 10 after |
| memory load (GlobalMemoryStatusEx) | `memory load high` | off by default: with the model loaded free RAM measured ~0.4 GiB (~99 %) |

Why an activity writer: `GetLastInputInfo` and `SHQueryUserNotificationState` answer for the caller's session only,
and the gate runs in session 0 (a startup task, up with nobody signed in), where they would always say idle. So
`python -m cortados_router.node_policy activity --out <file>` runs at logon in the owner's session (task
`CortadOS-Activity`, pythonw) and writes `{"v", "at", "idle_s", "fullscreen"}` every 5 s; stale, missing or malformed
means unknown, which refuses. Every unreadable input refuses; nothing unknown counts as free.

When the owner comes back mid-turn, the running turn finishes (bounded by the read timeout and its `max_tokens`; about
two minutes for 4K tokens at ~30 tok/s) and nothing new is admitted: cutting it off would break a conversation bound to
the node. To give a game the VRAM at once, the toggle's `off -FreeGpu` also asks Strata to unload.

### The toggle

The PC's switch is the gate's `/cortados/local-switch`, as on the Pro, so all of these flip the same flag:

- on the PC: the desktop shortcuts **CortadOS serving ON** / **CortadOS serving OFF (game)** (installed by
  `scripts/windows/install_pc_node.ps1`), or `scripts/windows/cortados-serving.ps1 on|off|status [-FreeGpu]`; it reads
  the gate address and token path from `%USERPROFILE%\.config\cortados\pc-node\pc-node.json` and never prints the token;
- from any session: the `local_switch` MCP tool or `/cortados-local` with node `pc`; the status carries `activity`
  (`away` / `idle` / `active` / `unknown`) besides the usual fields.

### Down, asleep or busy

Nothing new on the provider: the PC's probe is the same `GET /api/tags` through its gate. A PC that is asleep or off the
tailnet fails the probe within `connect_timeout_s`; the node is marked down for `routing.AUTO_DOWN_S` (30 s) and skipped
without another probe, S1's route (or another node) stands, and a pin gets a 503 naming the node. A refusing policy is a
503 with its fixed reason; Strata's own refusals are the retryable 529; a second turn while one runs is "busy"
(`max_inflight` 1). Tests: `tests/test_pc_node.py`.

### Keeping it up (`scripts/windows/install_pc_node.ps1`)

Dry run by default (`-Apply` makes the changes, `-Remove` takes them away). It registers `CortadOS-Strata` and
`CortadOS-Gate` as startup tasks (S4U: no stored password, run with nobody signed in; highest privileges; priority
Normal, because the default priority throttled Strata's expert loads ~25x; the gate 60 s after boot so Tailscale has its
address) whose launchers restart the process 30 s after it exits, and `CortadOS-Activity` at logon. It creates the token
file with its ACL, copies the example backend config if none exists, adds a firewall rule for the gate's port from the
tailnet range only, and the two desktop shortcuts. PowerShell 5.1 safe (ASCII only). Deploy steps and acceptance checks:
the runbook, section 12.

## What this setup does not claim

The home machines are not verified until their SSH targets are configured and
real distributed inference succeeds. Model shards do not migrate live when a
laptop disconnects. Removing a member means restarting/reloading that deployment
on a placement which still fits. CPU offload can expand capacity but needs its
own performance measurements before the router chooses it for latency-sensitive
work. No new model benchmark scores or routes were invented for the downloaded
community fine-tunes.
