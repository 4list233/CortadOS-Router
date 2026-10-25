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
# How long admitted work can be running before the vendor's usage screen shows it: up to one task run (the run
# timeout, 900 s) plus one sampler interval (~20 min). Shorter: running work is forgotten and the loop overshoots.
# Longer: finished work is counted twice and the loop pauses early.
IN_FLIGHT_S = 900 + 1200
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


def _remember(lane: str, general: set, pools: set) -> tuple[list, list]:
    """Every general window name and model pool ever observed on a lane (known_windows.json in the data home), the
    current ones added. An allowance seen before but absent now is missing, not gone: it must not be spent blind."""
    path = store.home() / "known_windows.json"
    try:
        known = json.loads(path.read_text())
    except (OSError, ValueError):
        known = {}
    lane_known = known.get(lane) or {}
    merged_g = sorted(set(lane_known.get("general", [])) | general)
    merged_p = sorted(set(lane_known.get("pools", [])) | pools)
    if merged_g != lane_known.get("general") or merged_p != lane_known.get("pools"):
        known[lane] = {"general": merged_g, "pools": merged_p}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(known, sort_keys=True))
        tmp.replace(path)
    return merged_g, merged_p


# Claude models Forest has confirmed draw ONLY on the shared 5-hour + weekly windows (no separate per-model cap), so
# they may run while the per-model panel is unavailable. Fable is the pooled exception (pool_for knows it). A Claude
# model in neither set has UNKNOWN ownership while the panel is down and is refused rather than spent against the shared
# windows blind — it might have an unobserved cap (Codex review 2026-10-25). Forest 2026-10-25: Fable is the pooled one.
CLAUDE_SHARED_MODELS = ("haiku", "sonnet", "opus")
# General windows a lane ALWAYS has, so a reading missing one is an incomplete capture — not evidence the limit is gone.
# Claude's are the rolling session and the weekly all-models window; both must be in hand before any Claude call, even
# before either has been observed on a fresh home (Codex review 2026-10-25: a partial capture with only the session
# window otherwise admitted a shared model against an unseen, possibly exhausted weekly limit).
REQUIRED_GENERAL = {"claude": ("5-hour", "week")}


def _panel_unavailable(raw: dict) -> bool:
    note = raw.get("per_model")
    return isinstance(note, str) and ("not shown" in note or "unavailable" in note)


def _confirmed_shared(lane: str, model: str | None) -> bool:
    return lane == "claude" and any(tok in (model or "").lower() for tok in CLAUDE_SHARED_MODELS)


def _derive_fable_pool(lane: str, owner: str, windows: list[dict]) -> dict | None:
    """Fable's cap is 50% of the weekly all-models budget and, by Forest's 50/50 split (2026-10-25), its utilisation
    tracks that weekly window. When Fable's own per-model panel is rate-limited we model its pool from the visible
    weekly window so judging is not blocked. This OVER-estimates Fable here: the loop uses it for a minority of calls
    (one judge per task vs. the three-band run ladder), so its true share is below 50% and this errs toward stopping
    early. Returns a derived Fable window (same usage/projection/reset as the weekly window) or None when unavailable."""
    if lane != "claude" or owner != "fable":
        return None
    if any(w.get("pool") == owner for w in windows):
        return None            # a real Fable reading exists (fresh OR carried-forward): use it, never override a known
                               # — possibly exhausted — pool with a lower estimate (Codex review 2026-10-25)
    week = next((w for w in windows if "week" in (w.get("window") or "").lower()
                 and w.get("pool", "all") == "all" and not w.get("stale")), None)
    if not week:
        return None
    return {**week, "window": "week (fable, derived from weekly)", "pool": "fable", "derived": True}


def _windows(sc, lane: str, model: str | None):
    """(applicable windows, None) or (None, why). Refuses unless the full expected inventory for the call is present:
    every general window ever seen, and (for a model call) its own pool — observed this reading, not carried forward."""
    raw = (sc.current().get("lanes") or {}).get(lane) or {}
    view = sc.lane_scarcity(max_reading_age_s=MAX_READING_AGE_S).get(lane) or {}
    if not raw or view.get("scarcity") is None:
        return None, "usage reading is missing or stale"
    if view.get("refusing"):
        return None, "provider is refusing or the window is spent"
    fetched = float(raw.get("fetched_at") or 0)
    windows = raw.get("windows") or []
    general_now = {w.get("window") for w in windows if w.get("pool", "all") == "all" and not w.get("stale")}
    pools_now = {w.get("pool") for w in windows if w.get("pool") not in (None, "all") and not w.get("stale")}
    # A pool's name is remembered even when only ever seen carried-forward (stale), so a model whose panel has not
    # loaded since a fresh home still has its limit treated as known-and-unobserved rather than absent.
    pools_any = {w.get("pool") for w in windows if w.get("pool") not in (None, "all")}
    known_general, known_pools = _remember(lane, general_now, pools_now | pools_any)
    required = [r for r in REQUIRED_GENERAL.get(lane, ()) if not any(r in (w or "").lower() for w in general_now)]
    if required:
        return None, f"{lane} required general window(s) {required} not in the current reading (incomplete capture)"
    missing = sorted(set(known_general) - general_now)
    if missing:
        return None, f"{lane} general window(s) {missing} missing from the current reading"
    if model:
        # A model with its own separately-metered pool (pool_for knows which, e.g. Claude Fable) is refused when that
        # pool is absent from the current reading — including when the per-model panel is rate-limited, since its cap is
        # then unreadable. A model that rides only the shared 'all' windows is unaffected: those windows ARE its cap and
        # they are visible. (Corrects the review-5 blanket per-model-panel refusal that stalled haiku/sonnet/opus runs
        # although their real caps were in hand — Forest 2026-10-25: Fable is the pooled one, the run models are not.)
        owner = sc.pool_for(lane, model or "", sorted(known_pools))
        if owner and owner not in pools_now:
            derived = _derive_fable_pool(lane, owner, windows) if _panel_unavailable(raw) else None
            if derived is not None:                      # model Fable's pool from the weekly window (its panel is down)
                windows = windows + [derived]
                pools_now = pools_now | {owner}
            else:
                carried = any(w.get("pool") == owner and w.get("stale") for w in windows)
                return None, (f"{lane} pool {owner} for {model} is "
                              + ("a carried-forward reading" if carried else "missing from the current reading"))
        if owner is None and _panel_unavailable(raw) and not _confirmed_shared(lane, model):
            return None, (f"{lane} per-model panel unavailable ({raw.get('per_model')}); {model}'s ownership is "
                          "unknown and it may have an unobserved cap")
    pool = sc.pool_for(lane, model or "", sorted(pools_now)) if (model and pools_now) else None
    applicable = [dict(w, reset_epoch=_reset_epoch(w, fetched), observed_at=fetched,
                       key=f"{lane}|{w.get('window')}|{w.get('pool', 'all')}")
                  for w in windows if w.get("pool", "all") in ("all", pool) and not w.get("stale")]
    if not applicable:
        return None, "no usage window applies"
    return applicable, None


def verdict(windows: list[dict], reserved: dict | None = None, now: float | None = None) -> tuple[bool, str]:
    """Whether one more call fits every window, counting the calls already reserved against each.

    Reservations are usage; the projection is recomputed from it the way scarcity.window_view computes it
    (used / elapsed share, the share floored at 5 %), so a reservation late in a window weighs what it really will."""
    now = time.time() if now is None else now
    reserved = reserved or {}
    notes = []
    for w in windows:
        used, elapsed, reset = w.get("used_pct"), w.get("elapsed_pct"), w.get("reset_epoch")
        if not all(isinstance(x, (int, float)) for x in (used, elapsed, reset, w.get("projected_pct_at_reset"))):
            return False, f"window {w.get('window')} reading is incomplete"
        used = used + (reserved.get(w.get("key"), 0) + 1) * ASSUMED_PCT_PER_CALL
        projected = max(w["projected_pct_at_reset"], used / max(elapsed / 100.0, 0.05))
        hours = (reset - now) / 3600
        if max(projected, used) > STOP_PROJECTED_PCT:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: projected {projected:.0f}% used {used:.0f}% would pass {STOP_PROJECTED_PCT:.0f}%"
        if hours < STOP_HOURS_BEFORE_RESET:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: reset in {hours:.1f} h (< {STOP_HOURS_BEFORE_RESET:.0f} h)"
        notes.append(f"{w.get('window')}/{w.get('pool', 'all')} {projected:.0f}%, reset {hours:.0f}h")
    return True, ", ".join(notes)


def _reserved(windows: list[dict]) -> dict:
    """Calls that may not show in a window's reading yet: every admission from IN_FLIGHT_S before that reading's
    sample on. Work admitted earlier is assumed visible; counting a visible call twice only stops the loop sooner."""
    since = {w["key"]: w["observed_at"] - IN_FLIGHT_S for w in windows}
    counts: dict = {}
    for row in store.read("admissions"):
        for k in row.get("windows", []):
            if k in since and row.get("ts", 0) >= since[k]:
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
        ok, why = verdict(windows, _reserved(windows))
        if ok:
            store.append("admissions", {"ts": time.time(), "lane": lane, "model": model,
                                        "windows": sorted(w["key"] for w in windows)})
        return ok, why


def status(core) -> dict[str, str]:
    """Each lane's verdict for one more call, without reserving anything (for the loop's log)."""
    sc = core._scarcity_module()
    out = {}
    for lane in LANES:
        windows, why = _windows(sc, lane, None)
        out[lane] = why if windows is None else verdict(windows, _reserved(windows))[1]
    return out
