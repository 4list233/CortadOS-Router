"""The loop's quota rule (D4, AC-09): spend a pool until its projected use at reset reaches 95 %, never in the last
hour before its reset, and never on an unknown or stale reading.

Readings come from the router's own scarcity owner (scarcity.current / lane_scarcity), the windows routing uses. Every
window that applies to the model being called is checked: the lane's general windows and the model's own pool.
`admit` is the one door every model call goes through. It reserves every applicable window atomically, and the
reservations are a durable store (admissions.jsonl) taken under a file lock, so they survive restarts and are shared
by every process on the data home: calls admitted on one reading count toward the stop until the next sample.
"""
from __future__ import annotations

import fcntl
import json
import threading
import time

from outcome import store

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

_thread_lock = threading.Lock()


def _reset_epoch(w: dict, fetched_at: float) -> float | None:
    """The window's absolute reset: the producer's reset_epoch, or for readings written before it existed, the
    sample time plus the countdown recorded with that sample (never a countdown read later)."""
    if isinstance(w.get("reset_epoch"), (int, float)):
        return float(w["reset_epoch"])
    if isinstance(w.get("resets_in_h"), (int, float)) and fetched_at:
        return fetched_at + w["resets_in_h"] * 3600
    return None


def _windows(sc, lane: str, model: str | None):
    """(applicable windows, None) or (None, why)."""
    raw = (sc.current().get("lanes") or {}).get(lane) or {}
    view = sc.lane_scarcity(max_reading_age_s=MAX_READING_AGE_S).get(lane) or {}
    if not raw or view.get("scarcity") is None:
        return None, "usage reading is missing or stale"
    if view.get("refusing"):
        return None, "provider is refusing or the window is spent"
    fetched = float(raw.get("fetched_at") or 0)
    windows = raw.get("windows") or []
    pools = sorted({w.get("pool") for w in windows if w.get("pool") not in (None, "all")})
    pool = sc.pool_for(lane, model or "", pools) if (model and pools) else None
    applicable = [dict(w, reset_epoch=_reset_epoch(w, fetched), key=f"{lane}|{w.get('window')}|{w.get('pool', 'all')}|{fetched}")
                  for w in windows if w.get("pool", "all") in ("all", pool)]
    if not applicable:
        return None, "no usage window applies"
    return applicable, None


def verdict(windows: list[dict], reserved: dict | None = None, now: float | None = None) -> tuple[bool, str]:
    """Whether one more call fits every window, counting the calls already reserved against each."""
    now = time.time() if now is None else now
    reserved = reserved or {}
    notes = []
    for w in windows:
        projected, used, reset = w.get("projected_pct_at_reset"), w.get("used_pct"), w.get("reset_epoch")
        if not all(isinstance(x, (int, float)) for x in (projected, used, reset)):
            return False, f"window {w.get('window')} reading is incomplete"
        extra = (reserved.get(w.get("key"), 0) + 1) * ASSUMED_PCT_PER_CALL
        projected, used = projected + extra, used + extra
        hours = (reset - now) / 3600
        if max(projected, used) > STOP_PROJECTED_PCT:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: projected {projected:.0f}% used {used:.0f}% would pass {STOP_PROJECTED_PCT:.0f}%"
        if hours < STOP_HOURS_BEFORE_RESET:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: reset in {hours:.1f} h (< {STOP_HOURS_BEFORE_RESET:.0f} h)"
        notes.append(f"{w.get('window')}/{w.get('pool', 'all')} {projected:.0f}%, reset {hours:.0f}h")
    return True, ", ".join(notes)


def _reserved(keys: set) -> dict:
    counts: dict = {}
    for row in store.read("admissions"):
        for k in row.get("windows", []):
            if k in keys:
                counts[k] = counts.get(k, 0) + 1
    return counts


def admit(core, lane: str, model: str | None = None) -> tuple[bool, str]:
    """The door for every model call: (may call, why). On yes, every applicable window is reserved durably."""
    sc = core._scarcity_module()
    with _thread_lock, (store.home() / "quota.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)    # one admission at a time across every process on this home
        windows, why = _windows(sc, lane, model)
        if windows is None:
            return False, why
        keys = {w["key"] for w in windows}
        ok, why = verdict(windows, _reserved(keys))
        if ok:
            store.append("admissions", {"ts": time.time(), "lane": lane, "model": model, "windows": sorted(keys)})
        return ok, why


def status(core) -> dict[str, str]:
    """Each lane's verdict for one more call, without reserving anything (for the loop's log)."""
    sc = core._scarcity_module()
    out = {}
    for lane in LANES:
        windows, why = _windows(sc, lane, None)
        out[lane] = why if windows is None else verdict(windows, _reserved({w["key"] for w in windows}))[1]
    return out
