"""The resource-pool snapshot behind the dashboard: one JSON view of (1) logged router usage, (2) each subscription's
cached scarcity, (3) the online local devices and their memory, and (4) a rough estimate of which local models would fit.

It is read-only and must stay cheap: it reads router.db and the CACHED subscription reading (never opens a vendor CLI),
and it samples local devices on a background timer so an HTTP GET never blocks on SSH. Stdlib only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from cortados_router.paths import tables_dir

GIB = 2 ** 30
LOCAL_TTL_S = float(os.environ.get("CORTADOS_RESOURCES_LOCAL_TTL_S", "30"))   # how long a device sample is reused


def local_models() -> list[dict[str, Any]]:
    """The candidate local models and their rough 4-bit weight footprints (tables/local-models.json); [] if absent."""
    path = tables_dir() / "local-models.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("models", [])
    except (OSError, ValueError):
        return []


def models_that_fit(headroom_bytes: float | None, models: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Which models fit in `headroom_bytes` by weight estimate alone (biggest first). An ESTIMATE: it ignores KV-cache
    and context, so it is a ceiling, not a promise — real admission needs a verified deployment."""
    if headroom_bytes is None:
        return []
    models = local_models() if models is None else models
    fit = [m for m in models if isinstance(m.get("approx_gib"), (int, float)) and m["approx_gib"] * GIB <= headroom_bytes]
    return sorted(fit, key=lambda m: -m["approx_gib"])


# ---- local device telemetry, refreshed off the request path ------------------------------------------------------
_local = {"at": 0.0, "data": None, "refreshing": False}
_local_lock = threading.Lock()


def _default_sampler(config_path: str) -> dict[str, Any]:
    from cortados_router.local_resources import sample
    return sample(config_path)


def refresh_local(config_path: str, sampler=None) -> dict[str, Any]:
    """Sample the fleet synchronously and store it. A failure is stored as an {error} reading, not raised, so the
    dashboard shows 'unavailable' rather than breaking."""
    try:
        data = (sampler or _default_sampler)(config_path)
    except Exception as exc:                            # SSH/collector failure: surfaced, not fatal
        data = {"error": f"{type(exc).__name__}: {exc}"}
    with _local_lock:
        _local.update(at=time.time(), data=data, refreshing=False)
    return data


def local_snapshot(config_path: str | None, *, now: float | None = None, sampler=None) -> dict[str, Any] | None:
    """The last device sample, refreshing it on a background thread when stale so the caller never blocks on SSH. None
    when no fleet config is set. The first call returns None (sample pending) and later calls return the reading."""
    if not config_path:
        return None
    now = now if now is not None else time.time()
    with _local_lock:
        stale = _local["data"] is None or now - _local["at"] > LOCAL_TTL_S
        if stale and not _local["refreshing"]:
            _local["refreshing"] = True
            threading.Thread(target=refresh_local, args=(config_path, sampler), name="cortados-resources-sample", daemon=True).start()
        return _local["data"]


def _devices_and_fit(local: dict[str, Any] | None) -> dict[str, Any]:
    """Shape the device telemetry for the dashboard and attach per-node and pooled model-fit estimates."""
    if not local or local.get("error"):
        return {"status": "pending" if local is None else "unavailable", "error": (local or {}).get("error"), "nodes": []}
    models = local_models()
    nodes = []
    for n in local.get("nodes", []):
        headroom = n.get("gpu_headroom_estimate_bytes")
        nodes.append({"id": n.get("id"), "role": n.get("role"), "status": n.get("status"),
                      "inventory": n.get("inventory", {}),
                      "host_memory_total_bytes": n.get("host_memory_total_bytes"),
                      "gpu_headroom_estimate_bytes": headroom,
                      "gpu_utilization_pct": n.get("gpu_utilization_pct"),
                      "fits": [m["id"] for m in models_that_fit(headroom, models)]})
    pool_headroom = local.get("known_pool_headroom_estimate_bytes")
    return {"status": "ok", "nodes": nodes,
            "pool_headroom_estimate_bytes": pool_headroom,
            "pool_fits": [m["id"] for m in models_that_fit(pool_headroom, models)],
            "note": local.get("note")}


def snapshot(core, *, local_config: str | None = None, usage_limit: int = 12,
             now: float | None = None, sampler=None) -> dict[str, Any]:
    """The full resource-pool view. `core` is cortados_router.core (injected for testing). Reads cached state only."""
    now = now if now is not None else time.time()
    try:
        usage = core.usage_summary(usage_limit)
    except Exception as exc:
        usage = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        health = core.lane_health()                     # cached subscription scarcity; never opens a vendor CLI
        subscriptions = {lane: {"ok": h.get("ok"), "scarcity": h.get("scarcity"), "expiring_pct": h.get("expiring_pct"),
                                "tightest": h.get("tightest"), "why": h.get("why")} for lane, h in health.items()}
    except Exception as exc:
        subscriptions = {"error": f"{type(exc).__name__}: {exc}"}
    return {"generated_at": now, "usage": usage, "subscriptions": subscriptions,
            "devices": _devices_and_fit(local_snapshot(local_config, now=now, sampler=sampler))}
