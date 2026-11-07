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
import os
import threading
import time

from outcome import store

# Forest 2026-10-21: surplus quota may be spent up to 95 % projected. Lower leaves allowance to expire unused;
# higher risks Forest's own work hitting the cap before the reset.
STOP_PROJECTED_PCT = 95.0
# The stop rules shared with the provider's floor (one canonical copy, with their reasons): never in the last hour
# before a reset, no reading older than an hour, measured per-lane run costs, in-flight horizon, Fable pool cost 2x.
from cortados_router.provider.floor import (assumed_pct_per_call, IN_FLIGHT_S, MAX_READING_AGE_S,  # noqa: E402
                                            POOL_CAP_SCALE, STOP_HOURS_BEFORE_RESET)
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
REQUIRED_GENERAL = {"claude": ("week",)}
# General windows the vendor has DROPPED from its usage screen (Forest 2026-11-03: Claude accounts now show only the weekly
# windows, "week (all models)" / "week (Fable)", no 5-hour session panel). Absent, such a window is treated as gone — not
# as an incomplete capture — so long as every REQUIRED_GENERAL window is in hand; present, it is checked like any other.
# Without this every Claude call was held forever ("required general window(s) ['5-hour'] ... incomplete capture").
DROPPABLE_GENERAL = {"claude": ("5-hour",)}


def _panel_unavailable(raw: dict) -> bool:
    note = raw.get("per_model")
    return isinstance(note, str) and ("not shown" in note or "unavailable" in note)


def _confirmed_shared(lane: str, model: str | None) -> bool:
    return lane == "claude" and any(tok in (model or "").lower() for tok in CLAUDE_SHARED_MODELS)


# Fable's own pool, when its per-model panel is rate-limited, is modelled from the visible weekly window. Fable's cap is
# HALF the weekly budget, so Fable utilisation = 2 × (Fable's share of weekly usage) × weekly utilisation. That share is
# unknown and unenforceable (ladder runs use Codex/other Claude models, judging uses Fable), so the SAFE default assumes
# the worst case — ALL weekly usage is Fable (share 1.0 → the derived pool is 2× the weekly window), which refuses once
# weekly passes ~48 %. Forest asserted a 50/50 split for his account (2026-10-25); set CORTADOS_FABLE_WEEKLY_SHARE=0.5 to
# model Fable at that split (then Fable util == weekly util) at his own risk (Codex review 2026-10-25).
FABLE_WEEKLY_SHARE = float(os.environ.get("CORTADOS_FABLE_WEEKLY_SHARE", "1.0"))
# How much of ITS OWN cap one call consumes relative to the weekly budget, per separately-metered pool.
# Fable's cap is HALF that budget, so the same run costs twice its lane's reserved weekly percentage.
# Reservations against ANY Fable window — derived from the weekly window or measured from the panel — count at this
# scale, so pending calls are not under-counted when the panel recovers before they appear in a sample (Codex review).


def _derive_fable_pool(lane: str, owner: str, windows: list[dict]) -> dict | None:
    """Model Fable's pool from the visible weekly window when its own per-model panel is rate-limited, scaled by its
    half-sized cap and assumed weekly share (FABLE_WEEKLY_SHARE) so judging is not blocked. Returns None — and the
    caller refuses — when any real Fable reading exists (a known, possibly exhausted pool is never overridden with an
    estimate) or no weekly window is in hand (Codex review 2026-10-25)."""
    if lane != "claude" or owner != "fable":
        return None
    if any(w.get("pool") == owner for w in windows):
        return None            # a real Fable reading exists (fresh OR carried-forward): use it, never override a known
                               # — possibly exhausted — pool with a lower estimate (Codex review 2026-10-25)
    week = next((w for w in windows if "week" in (w.get("window") or "").lower()
                 and w.get("pool", "all") == "all" and not w.get("stale")), None)
    if not week:
        return None
    factor = 2.0 * FABLE_WEEKLY_SHARE                           # Fable cap is half the budget → ×2; share 1.0 = worst case
    derived = {**week, "window": "week (fable, derived from weekly)", "pool": "fable", "derived": True}
    for k in ("used_pct", "projected_pct_at_reset"):            # convert weekly utilisation into Fable-cap utilisation
        if isinstance(week.get(k), (int, float)):
            derived[k] = min(100.0, week[k] * factor)
    return derived                                             # cap_scale for reservations is set from POOL_CAP_SCALE in _windows


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
    droppable = DROPPABLE_GENERAL.get(lane, ())
    missing = sorted(w for w in set(known_general) - general_now
                     if not any(d in (w or "").lower() for d in droppable))
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
        # Forest 2026-11-03: a model of unknown ownership while the per-model panel is down no longer holds forever (the
        # panel has been unavailable for days and the loop sat idle). It falls back to the general weekly window(s), which
        # are its cap if it has no pool of its own, with the 95 % / last-hour stop rule intact. Set
        # CORTADOS_OUTCOME_STRICT_PANEL=1 to restore the old refusal.
        if (owner is None and _panel_unavailable(raw) and not _confirmed_shared(lane, model)
                and os.environ.get("CORTADOS_OUTCOME_STRICT_PANEL") == "1"):
            return None, (f"{lane} per-model panel unavailable ({raw.get('per_model')}); {model}'s ownership is "
                          "unknown and it may have an unobserved cap")
    pool = sc.pool_for(lane, model or "", sorted(pools_now)) if (model and pools_now) else None
    applicable = []
    for w in windows:
        wp = w.get("pool", "all")
        if wp not in ("all", pool) or w.get("stale"):
            continue
        # Reservation identity is STABLE across a pool's derived and measured readings (their window names differ):
        # pooled windows key on the pool, general windows on the window name (Codex review 2026-10-25).
        key = f"{lane}|pool|{wp}" if wp != "all" else f"{lane}|win|{w.get('window')}"
        applicable.append(dict(w, lane=lane, reset_epoch=_reset_epoch(w, fetched), observed_at=fetched, key=key,
                               cap_scale=w.get("cap_scale", POOL_CAP_SCALE.get(wp, 1.0))))
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
        # Scale the canonical lane cost to THIS window's budget: Fable has half the weekly cap.
        # Missing lane identity retains the conservative unmeasured default, never a cheap family guess.
        per_call = assumed_pct_per_call(w.get("lane", "")) * w.get("cap_scale", 1.0)
        used = used + (reserved.get(w.get("key"), 0) + 1) * per_call
        projected = max(w["projected_pct_at_reset"], used / max(elapsed / 100.0, 0.05))
        hours = (reset - now) / 3600
        if max(projected, used) > STOP_PROJECTED_PCT:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: projected {projected:.0f}% used {used:.0f}% would pass {STOP_PROJECTED_PCT:.0f}%"
        if hours < STOP_HOURS_BEFORE_RESET:
            return False, f"{w.get('window')}/{w.get('pool', 'all')}: reset in {hours:.1f} h (< {STOP_HOURS_BEFORE_RESET:.0f} h)"
        notes.append(f"{w.get('window')}/{w.get('pool', 'all')} {projected:.0f}%, reset {hours:.0f}h")
    return True, ", ".join(notes)


def _canonical_key(key: str) -> str:
    """Map an admission key to the current identity. Rows persisted before the key change use lane|window|pool; the
    current scheme is lane|pool|<pool> for pooled windows and lane|win|<window> for general ones. Normalising on read
    means reservations outstanding across an upgrade still count (Codex review 2026-10-25)."""
    parts = key.split("|")
    if len(parts) == 3 and parts[1] in ("pool", "win"):
        return key                                   # already the current scheme
    if len(parts) >= 3:                               # legacy lane|window(…)|pool (a window name may itself contain '|')
        lane, pool, window = parts[0], parts[-1], "|".join(parts[1:-1])
        return f"{lane}|pool|{pool}" if pool != "all" else f"{lane}|win|{window}"
    return key


def _reserved(windows: list[dict]) -> dict:
    """Calls that may not show in a window's reading yet: every admission from IN_FLIGHT_S before that reading's
    sample on. Work admitted earlier is assumed visible; counting a visible call twice only stops the loop sooner."""
    since = {w["key"]: w["observed_at"] - IN_FLIGHT_S for w in windows}
    counts: dict = {}
    for row in store.read("admissions"):
        for k in row.get("windows", []):
            ck = _canonical_key(k)                    # legacy keys normalise to the current identity so they still count
            if ck in since and row.get("ts", 0) >= since[ck]:
                counts[ck] = counts.get(ck, 0) + 1
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
