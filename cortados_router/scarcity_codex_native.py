# PURPOSE: Validate the captured native quota wire type and project it to scarcity.
# CONTRACT: RT-MCP-001 section 4.1; shape/unit conversion only, no new quota formula.
# DEPENDS: Existing scarcity.window_view/finish and transport-independent WorkerError.
"""Native Codex subscription windows, without PTY probes or live-state reads."""

import time
import math

from integrations.mcp.schemas import WorkerError
from . import scarcity


def project(value, *, now=None, admission=True):
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise WorkerError("quota_unknown", "Current included usage is unavailable")
    allowed = value.get("ordinaryUsageAllowed")
    if allowed is False and admission:
        raise WorkerError("quota_spent", "Included native usage is not permitted")
    if allowed is not True and allowed is not False:
        raise WorkerError("quota_unknown", "Current included usage permission is unavailable")
    buckets = value.get("rateLimitsByLimitId")
    if buckets is not None:
        if not isinstance(buckets, dict) or set(buckets) != {"codex"}:
            raise WorkerError("quota_unknown", "Native quota bucket mapping is not captured")
        bucket = buckets["codex"]
    else:
        bucket = value.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex") or bucket.get("normalModelSlug") is not None:
        raise WorkerError("quota_unknown", "Native quota model mapping is not captured")
    spent = allowed is False or bucket.get("spendControlReached") is True or bucket.get("rateLimitReachedType") is not None
    if spent and admission:
        raise WorkerError("quota_spent", "Native subscription reports its usage limit reached")
    windows = []
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise WorkerError("quota_unknown", "Native quota window is invalid")
        used, duration, reset = (window.get(key) for key in ("usedPercent", "windowDurationMins", "resetsAt"))
        if (type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100 or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
                or type(reset) not in (int, float) or not math.isfinite(reset) or not now < reset <= now + duration * 60):
            raise WorkerError("quota_unknown", "Native quota window geometry is unavailable or invalid")
        label = "week" if duration == 10080 else "5-hour" if duration == 300 else f"{duration:g}-minute"
        windows.append({**scarcity.window_view(label, used, reset, duration * 60, now=now), "pool": "all", "native_window": name})
    if not windows and not (spent and not admission):
        raise WorkerError("quota_unknown", "Native quota contains no complete usage window")
    result = scarcity.finish("codex", {"source": "codex-app-server", "windows": windows})
    result["refusing"] = result["refusing"] or spent
    if result["refusing"] and admission:
        raise WorkerError("quota_spent", "Included native usage is spent")
    return result
