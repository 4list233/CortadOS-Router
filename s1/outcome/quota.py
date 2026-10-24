"""The loop's quota rule (D4, AC-09): spend a pool until its projected use at reset reaches 95 %, never in the last
hour before its reset, and never on an unknown reading.

Readings come from the router's own scarcity owner (scarcity.lane_scarcity via core), the same windows routing uses.
"""
from __future__ import annotations

# Forest 2026-10-21: surplus quota may be spent up to 95 % projected. Lower leaves allowance to expire unused;
# higher risks Forest's own work hitting the cap before the reset.
STOP_PROJECTED_PCT = 95.0
# Forest 2026-10-21: never in the last hour before a reset, so a late burst cannot spill the next window's start.
STOP_HOURS_BEFORE_RESET = 1.0
# Lanes this loop calls, by the router's lane names.
LANES = ("claude", "codex")


def verdict(reading: dict | None) -> tuple[bool, str]:
    """(may spend, why) for one lane's lane_scarcity row."""
    if not reading:
        return False, "no usage reading for this lane"
    if reading.get("refusing"):
        return False, "provider is refusing or the window is spent"
    if reading.get("scarcity") is None:
        # lane_scarcity blanks scarcity for a stale reading but keeps its last windows: never spend on those.
        return False, "usage reading is stale"
    w = reading.get("tightest") or {}
    projected, used, resets_in = w.get("projected_pct_at_reset"), w.get("used_pct"), w.get("resets_in_h")
    if not all(isinstance(x, (int, float)) for x in (projected, used, resets_in)):
        return False, "usage reading is incomplete"
    if max(projected, used) >= STOP_PROJECTED_PCT:
        return False, f"projected {projected:.0f}% / used {used:.0f}% ≥ {STOP_PROJECTED_PCT:.0f}%"
    if resets_in < STOP_HOURS_BEFORE_RESET:
        return False, f"reset in {resets_in:.1f} h (< {STOP_HOURS_BEFORE_RESET:.0f} h)"
    return True, f"projected {projected:.0f}%, used {used:.0f}%, reset in {resets_in:.0f} h"


# The sampler refreshes every ~20 minutes; routing tolerates 6-hour-old readings, but a loop that spends steadily
# must not act on a reading older than an hour, or it can overshoot the 95 % stop between samples.
MAX_READING_AGE_S = 3600


def gates(core) -> dict[str, tuple[bool, str]]:
    readings = core._scarcity_module().lane_scarcity(max_reading_age_s=MAX_READING_AGE_S)
    return {lane: verdict(readings.get(lane)) for lane in LANES}
