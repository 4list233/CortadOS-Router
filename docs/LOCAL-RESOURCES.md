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
Managed local readings expire after at most 120 seconds, and background local
refresh is due every 60 seconds independently of subscription-window readings.

## What this setup does not claim

The home machines are not verified until their SSH targets are configured and
real distributed inference succeeds. Model shards do not migrate live when a
laptop disconnects. Removing a member means restarting/reloading that deployment
on a placement which still fits. CPU offload can expand capacity but needs its
own performance measurements before the router chooses it for latency-sensitive
work. No new model benchmark scores or routes were invented for the downloaded
community fine-tunes.
