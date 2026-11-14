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
`auto_route` is set, as an auto-route target (below). The provider reads the same file as the sampler,
`CORTADOS_LOCAL_RESOURCES_CONFIG`; `tables/resources.example.json` shows the Pro's block (placeholders).

| key | meaning |
| --- | --- |
| `base_url_env` | name of an env var holding the node's URL: its local gate, e.g. `http://<tailscale-ip>:11435` (wins over `base_url`) |
| `token_file_env` / `token_file` | env var naming, or the path of, the file holding the gate's token on the provider host (same rules as the gate: mode 0600, ≥ 32 chars); sent as `Authorization: Bearer` on the probe and every turn |
| `base_url` | the URL itself, for the git-ignored `tables/resources.json` only; never commit a real address |
| `label` | the suffix `/v1/models` shows: `<name> · CortadOS (<label>)` (default: the node id, capitalised) |
| `auto_route` | `false` (default): pin-only. Or `{"model": "<alias or tag of one of this node's models>", "bands": ["R0", …]}`: the S1 bands in which that model may take a new conversation (Local auto-route, below). There is no default band; `true`, an unknown model, an empty list, unknown keys and the bands `R3`/`R4` are refused with a config error |
| `probe_timeout_s` / `connect_timeout_s` / `read_timeout_s` | `/api/tags` probe (2), connect (3), wait per response chunk (300; a first model load can take a minute); each must be positive |
| `models[]` | `id` (the Ollama tag, required), `alias` (the short pin name), `name` (display name), `context_length` (a positive integer of tokens; default 32768 when absent, the input-fit gate; set it to the node's Ollama context, 65536 for Ollama.app's setting on the Pro); `thinking` (`"off-unless-asked"`, the default, or `"passthrough"`; anything else is refused: Thinking models, below) |

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
   list these tags as `ollama list` prints them, e.g. `Qwen27b_heretic:latest`). Set Ollama's context to at least the configured `context_length` (65536):
   `OLLAMA_CONTEXT_LENGTH=65536` for `ollama serve`, or Ollama.app's context setting, then restart Ollama (see "Context
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

With `auto_route` set, `routing.auto_local` runs after S1 has routed a new `cortados` conversation (the routed path
only; pins are unaffected). It moves the conversation to the node's `auto_route.model` when **all** of these hold, and
otherwise leaves S1's route exactly as it was:

1. S1 was not degraded (a degraded decision has no band of its own; the "degraded scorer never defaults to Claude" rule
   is untouched because nothing here ever adds a Claude route);
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
apply). Each skipped condition is logged at INFO (`local auto-route: … not taken …`), except a gate 401 (the provider's
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
| Qwen3.8 27B Heretic, Q4_K_M | `Qwen27b_heretic` | `qwen-27b` | 16 GB | default auto-route model |
| Huihui Qwythos 9B, Q6_K | `Huihui_Qwen_9b` | `qwythos-9b` | 7.6 GB | fast/small option (auto-route model if the 27B is too slow) |
| Huihui Qwen3.8 Abliterated | `Huihui_Qwen3.8` | `qwen-abliterated` | 17 GB | pin-only |
| Phi-4 Heretic, Q4_K_M | `Phi_4` | `phi-4` | 9.1 GB | pin-only |

All four are community fine-tunes without a published evaluation, which is one more reason to start auto-route on
reflex work only. Recommended first setting:
`"auto_route": {"model": "qwen-27b", "bands": ["R0"]}` (reflex work only: D ≤ 2, blast radius 0, small context);
widen to `R1` only after reviewing the `local auto-route` rows in router.db. Which bands go local is the owner's call,
so the shipped example keeps `auto_route: false`.

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

**Context length must match the node.** A model's `context_length` here is only the provider's input-fit gate; Ollama
uses the node's own `OLLAMA_CONTEXT_LENGTH` (or Ollama.app's setting). Set the node to the same value, or requests the
provider admits get truncated by Ollama. The gate does not check this at startup.

A launchd template is `integrations/local-gate/com.cortados.local-gate.plist` (RunAtLoad + KeepAlive; placeholders
only). Check a setup with `python3 -m cortados_router.local_gate --listen <tailscale-ip> --print-config` (validates the
token file and prints the settings, never the token). On the provider, a 401 from the gate is a 503 "local gate on
node <id> refused the provider token", with no fallback.

## What this setup does not claim

The home machines are not verified until their SSH targets are configured and
real distributed inference succeeds. Model shards do not migrate live when a
laptop disconnects. Removing a member means restarting/reloading that deployment
on a placement which still fits. CPU offload can expand capacity but needs its
own performance measurements before the router chooses it for latency-sensitive
work. No new model benchmark scores or routes were invented for the downloaded
community fine-tunes.
