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
- Next: the `/resources` snapshot and dashboard show these files per host; later, routing can use `headroom` and
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

**History.** The backend never sees the notice. Notices are matched by provenance, never by shape (review round 3):
when the endpoint injects one, the session records it in its own history (`sensitive_notice.Injected`: the exact text,
block index 0, at that turn), over the fingerprint of the model's own blocks. A continuation matches its session however
the client kept the reply: notice as its own block, merged into the next text block (a leading prefix exactly equal to
the recorded text), or dropped. This covers both wires (`responses_wire` fingerprints through
`anthropic_wire.client_fingerprint`) and the replay of a retried turn, which stores the reply as shown. A rebuilt
backend's transcript leaves out exactly the recorded injections (`anthropic_wire.rebuild_transcript`), and a rebuilt
session keeps them (`sensitive_notice.carry`). Text the model wrote that only looks like a notice (a reply that quotes
the warning, leading or not) is never removed, and fingerprints are taken over everything the client sent, so two
different notice-only replies never share one.

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
admit    iff  count < count limit  and  reserved in flight + reserved ≤ memory_budget_bytes − weights_bytes
```

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

- Two full 128k turns still run at once at 4-bit; a third waits (529). This is the same as before, since long turns
  were already capped.
- With the owner active, every row admits at least 2 by bytes, so short turns are limited by the count cap first.
- With the owner idle, bytes limit before the count of 8 except for small turns with small max_tokens. A turn with
  vllm-mlx's default max_tokens (32k, as Claude Code sends) reserves ≥ 4.2 GiB whatever its size.

This is concurrency given up for a bound that holds by construction. The provider's own skip estimate
(`local_lane.request_tokens`, characters / 3.5) is unchanged. It only decides whether to try the node, and the gate's
refusal handles the rest.

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
| `memory_budget_bytes` | 40 GiB | of the Pro's 48 GB, leaving ~8 GB for macOS and the owner's apps |
| `weights_bytes` | 15 GiB (4-bit); 22.4 GB (6-bit, measured idle) | resident weights |
| `kv_bytes_per_token` | 65,536 | 16 full-attention layers × K/V × 4 KV heads × head_dim 256 × 2 bytes; 128k tokens = 8 GiB |
| `per_seq_state_bytes` | 0.15 GiB | the recurrent layers' state per sequence |
| `prefill_margin_bytes` | 1.5 GiB | prefill activations and allocator slack: the 6-bit copy used 2.3 GB over idle for one ~9k-token request, KV + state explain ~0.7 GiB |
| `context_tokens` | 131072 | the Pro block's `context_length` on the provider host; tokens are capped here |

What that admits, in RESERVED tokens (the bound's, not a turn's real size; "The cost" above maps one to the other;
owner idle, pressure normal; 25 GiB beside the 4-bit weights, 17.6 GiB beside the 6-bit):

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
- **A turn that could never fit** (its reservation exceeds `memory_budget_bytes − weights_bytes` even with nothing
  else running) is refused for good, not left to an endless 529 loop: see Enforcement.
- **Pressure stays the safety net**: on warn or critical the count caps apply even when the bytes fit.
- Both bounds gate **admission only**: when the owner comes back with 6 running, the limit drops to 2, nothing new is
  admitted until fewer than 2 run, and the 6 finish on their own (never killed). A reservation is released however
  its turn ends: answered, backend error, upstream timeout, backend unreachable, client gone mid-stream, or an
  exception in the relay (a `Ticket`, released once in the gate's `finally`).
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
"reserved_bytes", "free_bytes", "fit_tokens", "free_tokens"}` read-only as `cortados_gate` in every `GET /api/tags`
answer and as `capacity` in the switch status (provider `GET /v1/cortados/local`; the dashboard's Local models card
shows "vllm-mlx: n of limit running, R of B GiB reserved (reason)"). `free_bytes` is the budget beside the weights
minus the reservations; `fit_tokens` is the most context one turn may hold alone, `free_tokens` beside what runs now
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

### Switch the Pro to MLX

Owner steps; nothing here was run by the build. The provider's lane config needs no protocol change.

1. On the Pro: copy `tables/local-backend.example.json` to `~/.config/cortados/local-backend.json` and check it:
   `venv` (`~/venvs/mlx-bench`), `vllm_mlx.model` and its entry's `path` (the model directory; a 6-bit copy is another
   entry, selected here), `served_model_name` = the id the Air's config gives this model (`Qwen27b_heretic:latest`),
   and `serve.tool_call_parser` (set it to the parser the bench verified tool calls with). Validate:
   `python3 -B -m cortados_router.local_backend --print-config`.
2. On the Pro: `scripts/install_mlx_backend.sh --dry-run`, then `scripts/install_mlx_backend.sh` (LaunchAgent
   `com.cortados.vllm-mlx` on 127.0.0.1:8000 under the prompt guard, log `~/.cortados/vllm-mlx.log`). Both run the
   guard's `--check`: it must print `ok (vllm-mlx 0.6.0, pinned files match, text route)`. Wait for `curl -s
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

## What this setup does not claim

The home machines are not verified until their SSH targets are configured and
real distributed inference succeeds. Model shards do not migrate live when a
laptop disconnects. Removing a member means restarting/reloading that deployment
on a placement which still fits. CPU offload can expand capacity but needs its
own performance measurements before the router chooses it for latency-sensitive
work. No new model benchmark scores or routes were invented for the downloaded
community fine-tunes.
