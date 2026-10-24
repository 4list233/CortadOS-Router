"""The loop's quota rule (D4, AC-09): spend a pool until its projected use at reset reaches 95 %, never in the last
hour before its reset, and never on an unknown or stale reading.

Readings come from the router's own scarcity owner (scarcity.current / lane_scarcity), the windows routing uses. Every
window that applies to the model being called is checked: the lane's general windows and the model's own pool.
`admit` is the one door every model call goes through; it is atomic, and between two usage samples it counts the
calls it has already let through, so concurrent workers cannot overshoot the stop on one stale reading.
"""
from __future__ import annotations

import threading
import time

# Forest 2026-10-21: surplus quota may be spent up to 95 % projected. Lower leaves allowance to expire unused;
# higher risks Forest's own work hitting the cap before the reset.
STOP_PROJECTED_PCT = 95.0
# Forest 2026-10-21: never in the last hour before a reset, so a late burst cannot spill the next window's start.
STOP_HOURS_BEFORE_RESET = 1.0
# The sampler refreshes every ~20 minutes; routing tolerates 6-hour-old readings, but a loop that spends steadily
# must not act on a reading older than an hour.
MAX_READING_AGE_S = 3600
# Assumed share of a window one admitted call consumes until the next sample shows the real figure. Not yet measured
# (ENG-10: recorded as an assumption): an agent run on a fixture task is a few hundred thousand tokens at most, about
# 1 % of a weekly window on the plans seen so far. Too small: workers overshoot 95 % between samples. Too large: the
# loop stalls until each new sample although room is left.
ASSUMED_PCT_PER_CALL = 1.0
LANES = ("claude", "codex")

_lock = threading.Lock()
_admitted: dict[tuple, int] = {}          # (lane, pool, fetched_at) -> calls admitted on that reading


def _windows(sc, lane: str, model: str | None) -> tuple[list[dict] | None, str, float]:
    raw = (sc.current().get("lanes") or {}).get(lane) or {}
    view = sc.lane_scarcity(max_reading_age_s=MAX_READING_AGE_S).get(lane) or {}
    if not raw or view.get("scarcity") is None:
        return None, "usage reading is missing or stale", 0.0
    if view.get("refusing"):
        return None, "provider is refusing or the window is spent", 0.0
    windows = raw.get("windows") or []
    pools = sorted({w.get("pool") for w in windows if w.get("pool") not in (None, "all")})
    pool = sc.pool_for(lane, model or "", pools) if (model and pools) else None
    applicable = [w for w in windows if w.get("pool", "all") in ("all", pool)]
    return applicable, pool or "all", float(raw.get("fetched_at") or 0)


def verdict(windows: list[dict] | None, why_none: str, extra_pct: float = 0.0, now: float | None = None) -> tuple[bool, str]:
    if windows is None:
        return False, why_none
    if not windows:
        return False, "no usage window applies"
    now = time.time() if now is None else now
    worst = []
    for w in windows:
        projected, used, reset = w.get("projected_pct_at_reset"), w.get("used_pct"), w.get("reset_epoch")
        if not all(isinstance(x, (int, float)) for x in (projected, used, reset)):
            return False, f"window {w.get('window')} reading is incomplete"
        projected, used = projected + extra_pct, used + extra_pct
        hours = (reset - now) / 3600
        if max(projected, used) >= STOP_PROJECTED_PCT:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: projected {projected:.0f}% used {used:.0f}% ≥ {STOP_PROJECTED_PCT:.0f}%"
        if hours < STOP_HOURS_BEFORE_RESET:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: reset in {hours:.1f} h (< {STOP_HOURS_BEFORE_RESET:.0f} h)"
        worst.append(f"{w.get('window')} {projected:.0f}%→reset {hours:.0f}h")
    return True, ", ".join(worst)


def admit(core, lane: str, model: str | None = None) -> tuple[bool, str]:
    """The door for every model call: (may call, why). Counts the call against the reading it was admitted on."""
    sc = core._scarcity_module()
    with _lock:
        windows, pool_or_why, fetched = _windows(sc, lane, model)
        if windows is None:
            return False, pool_or_why
        key = (lane, pool_or_why, fetched)
        ok, why = verdict(windows, "", _admitted.get(key, 0) * ASSUMED_PCT_PER_CALL)
        if ok:
            _admitted[key] = _admitted.get(key, 0) + 1
        return ok, why


def status(core) -> dict[str, str]:
    """Each lane's verdict without admitting anything (for the loop's log)."""
    sc = core._scarcity_module()
    out = {}
    for lane in LANES:
        windows, why_none, _ = _windows(sc, lane, None)
        out[lane] = verdict(windows, why_none)[1]
    return out
