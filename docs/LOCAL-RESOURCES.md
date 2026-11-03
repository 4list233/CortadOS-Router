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
- Every command has a 4 s timeout, the Ollama fetch 0.5 s; each failure is an entry in `errors`.
- `sample --out` writes `$CORTADOS_HOME/hardware/<host>.json` atomically (0600 file, 0700 directory).
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

## Local models (pin-only)

A node with an `ollama` block offers its Ollama models to the provider as `sub/<alias or tag>` pins
(`cortados_router/provider/local_lane.py`). The provider reads the same file as the sampler,
`CORTADOS_LOCAL_RESOURCES_CONFIG`; `tables/resources.example.json` shows the Pro's block.

| key | meaning |
| --- | --- |
| `base_url_env` | name of an env var holding the node's URL: its local gate, e.g. `http://<tailscale-ip>:11435` (wins over `base_url`) |
| `token_file_env` / `token_file` | env var naming, or the path of, the file holding the gate's token on the provider host (same rules as the gate: mode 0600, ≥ 32 chars); sent as `Authorization: Bearer` on the probe and every turn |
| `base_url` | the URL itself, for the git-ignored `tables/resources.json` only; never commit a real address |
| `label` | the suffix `/v1/models` shows: `<name> · CortadOS (<label>)` (default: the node id, capitalised) |
| `auto_route` | the S1 auto-routing switch; default `false`. `true` is refused with a config error: a local lane must be evaluated and given routing-table candidates before S1 may pick it |
| `probe_timeout_s` / `connect_timeout_s` / `read_timeout_s` | `/api/tags` probe (2), connect (3), wait per response chunk (300; a first model load can take a minute); each must be positive |
| `models[]` | `id` (the Ollama tag, required), `alias` (the short pin name), `name` (display name), `context_length` (a positive integer of tokens; default 32768 when absent, the input-fit gate; set it to the node's Ollama context, 65536 for Ollama.app's setting on the Pro) |

Pin names (aliases and tags, case-insensitive) must be unique across nodes. Behaviour:

- Only on `/v1/messages` (Ollama implements the Anthropic Messages API there), only on a routing endpoint (not
  `--fixed-model`). The turn is relayed as-is (streaming or not, tools and `tool_use` blocks untouched); only `model` is
  rewritten to the tag, and the response names the pin the client asked for. The relay is stateless (every turn
  carries the whole conversation), but the conversation's model binding is kept in the provider's registry
  (`server.LocalSession`: the reply fingerprints, as for every pin), so a conversation keeps its model: a local
  conversation whose next turn names `sub/opus`, a `sub/opus` or S1-routed one whose next turn names a local pin, gets
  the same 409 "the model is fixed per conversation". A tool-less request (a title call) is a one-shot, not tracked.
- Routing goes through `routing.choose_pinned` (local branch `_choose_local`): no S1; the node must answer `/api/tags`
  within `probe_timeout_s` and list the tag; the request must fit `context_length` (else 413, checked again on every
  turn, keeping the history); `x-cortados-exclude-model` treats a local alias and its tag as one model; the conversation's
  opening request is logged as a `routes` row (engine `pin`, lane and provider `local-<node>`), continuations are not.
- Availability is the `/api/tags` answer; there is no quota. The Pro is the owner's laptop: asleep, closed, off the
  tailnet or busy, it fails the pin with a 503 that says so. Nothing falls back to another model.
- Not listed in `served_routes`, so S1 cannot choose a local lane.

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
- streams SSE through line by line; bounds the body (32 MiB), concurrent connections (8, then 503), the loopback
  connect (3 s), each read (300 s), an idle client socket (30 s) and, as absolute deadlines a trickling client cannot extend, the
  headers (10 s), the body (120 s) and the drain of a refused body (5 s, then closed without the 401); every limit must
  be a finite positive number; logs method, path and status only, never the token or a body.

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
