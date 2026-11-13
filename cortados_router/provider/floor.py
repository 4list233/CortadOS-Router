"""The client's quota floor (request header x-cortados-min-left-pct): the provider is its single enforcer.

A client that must not spend an account below n % weekly headroom (the S1 outcome loop: 5 %, its 95 % stop rule) sends
the header on every request. The provider then, in ONE place, for the account that will actually execute the turn:
checks the account's cached usage reading against the floor and the stop rules below, and on yes reserves the turn
durably against THAT account (so turns outstanding before the next usage sample count), under a file lock shared by
every provider process on this CORTADOS_HOME. New conversations are filtered before ranking (routing.choose_pinned) or
checked on the account select chose (routing.choose); every continuation rechecks and reserves its session's account
before the paid turn (server). The header only tightens: without it nothing here runs and routing is unchanged.

The rules and constants are the canonical ones; s1/outcome/quota.py imports them rather than keeping its own copy.
"""
from __future__ import annotations

import fcntl
import json
import threading
import time
from typing import Any

# Forest 2026-10-21: never in the last hour before a reset, so a late burst cannot spill the next window's start.
STOP_HOURS_BEFORE_RESET = 1.0
# The sampler refreshes every ~20 minutes; routing tolerates 6-hour-old readings, but a client that spends steadily
# must not act on a reading older than an hour.
MAX_READING_AGE_S = 3600
# ENG-10 is now measured (Air usage history, 2026-11-07): weekly % used per admitted run was
# claude 0.107 %, claude#b 0.084 %, codex 0.023 %. Method: delta weekly % used between usage
# readings divided by runs admitted; background spend is included, so real per-run cost is lower.
# Forest's conservative reservations below apply until the next sample shows actual usage.
# A call is one RUN (a whole conversation including tool turns, or one judge/generate request),
# charged once via reserve's `conv`/route_id, not once per turn. Later turns are visible only in
# the next reading; the S1 loop's 900 s run timeout bounds that for its runs.
# Keys are cand['lane'] account identities, NOT provider names. Unmeasured lanes keep 1 %:
# inheriting a measured sibling's cheap value could overshoot the floor between samples.
ASSUMED_PCT_PER_CALL_BY_LANE = {"claude": 0.5, "claude#b": 0.5, "codex": 0.15}
DEFAULT_ASSUMED_PCT_PER_CALL = 1.0

# How long admitted work can be running before the vendor's usage screen shows it: up to one task run (the run
# timeout, 900 s) plus one sampler interval (~20 min). Shorter: running work is forgotten and spending overshoots.
# Longer: finished work is counted twice and work pauses early.
IN_FLIGHT_S = 900 + 1200
# A call consumes cap_scale x its lane's reserved weekly percentage of a pool's own budget.
# The Fable pool is about half the weekly budget, so one call is twice the slice of it (s1/outcome/quota.py's derived pool, Codex review 2026-10-25).
POOL_CAP_SCALE = {"fable": 2.0}

LEDGER = "floor-reservations.jsonl"
_thread_lock = threading.Lock()


def assumed_pct_per_call(lane: str) -> float:
    """Reserved weekly percentage per run; unmeasured accounts retain the conservative default."""
    return ASSUMED_PCT_PER_CALL_BY_LANE.get(lane, DEFAULT_ASSUMED_PCT_PER_CALL)


def _left(w: dict) -> float | None:
    left = w.get("headroom_pct")
    if left is None and isinstance(w.get("used_pct"), (int, float)):
        left = 100.0 - float(w["used_pct"])
    return float(left) if isinstance(left, (int, float)) else None


def _key(lane: str, w: dict) -> str:
    pool = w.get("pool") or "all"
    return f"{lane}|pool|{pool}" if pool != "all" else f"{lane}|win|{w.get('window')}"


def executing_login(cand: dict[str, Any]) -> str | None:
    """The login the CLI for this candidate EXECUTES on, stated the way its backend builds the child's env.
    Claude: the effective login of bridge.claude_env, so an inherited CLAUDE_CONFIG_DIR or OAuth token is never mistaken
    for the sampler's login (the same expression as server._route_pct_left, the reveal).
    Codex: the home codex_bridge resolves (the provider's --codex-home, else CODEX_HOME, else ~/.codex) under
    codex_isolation.child_env, which strips every key/token override, so the login is that home's auth.json."""
    from cortados_router import core
    if str(cand.get("provider") or "").split("#", 1)[0] == "codex-cli":
        if cand.get("codex_login"):
            # The canonical home this conversation's backend was (or will be) started on, bound at admission: never
            # re-resolved, so a CODEX_HOME symlink moved after admission or between turns cannot change the identity
            # checked from the one executing (Codex review fu1).
            return cand["codex_login"]
        import os
        from cortados_router.provider.codex_isolation import child_env
        home = os.path.abspath(os.path.expanduser(cand.get("codex_home") or os.environ.get("CODEX_HOME") or "~/.codex"))
        return core.codex_login_identity(child_env(home))
    from cortados_router.provider.bridge import claude_env
    # Registry.new applies a config_dir only when the route names an account; otherwise the default login runs.
    return core.claude_login_identity(claude_env(cand.get("config_dir") if cand.get("account") else None))


def _identity_unproven(cand: dict[str, Any], reading: dict[str, Any]) -> str | None:
    """Why the reading cannot be shown to come from the login this candidate executes on, or None.
    Claude and Codex: the reading's `login` stamp (scarcity: the login its sampler CLI effectively ran on) must equal
    the login this provider's CLI would execute on (executing_login); config_dir alone is not proof (None == None says
    nothing about the inherited env), and a Codex reading for home A never authorizes spending on the provider's home B
    (Codex review r3). A reading without the stamp (one sampled before stamping existed), or an executing login that
    cannot be stated, holds. Any other provider has no stamped readings: held."""
    if str(cand.get("provider") or "").split("#", 1)[0] not in ("claude-cli", "codex-cli"):
        return "usage reading is not bound to the login this provider executes on"
    login = reading.get("login")
    if not (isinstance(login, str) and login) or login != executing_login(cand):
        return "usage reading was not sampled from the login this account executes on"
    return None


def block(core, cand: dict[str, Any], model: str, floor: float, readings: dict | None = None,
          reserved: dict | None = None, now: float | None = None, extra: int = 1) -> tuple[str | None, list]:
    """(why the account may not take one more run under `floor`, or None; the reservation keys of that run).
    `extra`: runs this check adds on top of `reserved` (0 when the conversation's own run is already counted)."""
    now = time.time() if now is None else now
    reserved = reserved or {}
    lane = cand.get("lane") or ""
    readings = core.subscription_readings() if readings is None else readings
    reading = readings.get(lane) if isinstance(readings, dict) else None
    tag = f"x-cortados-min-left-pct {floor:g}"
    if not isinstance(reading, dict) or reading.get("failed"):
        return f"no current usage reading to check {tag}", []
    why = _identity_unproven(cand, reading)
    if why:
        return f"{why}; held under {tag}", []
    fetched = reading.get("fetched_at")
    if not isinstance(fetched, (int, float)) or not 0 <= now - fetched < MAX_READING_AGE_S:
        return f"usage reading older than {MAX_READING_AGE_S // 60} min; held under {tag}", []
    weekly = [w for w in reading.get("windows") or [] if str(w.get("window") or "").startswith("week")]
    pools = sorted({w.get("pool") for w in weekly if w.get("pool") not in (None, "all")})
    # pool_for names a model's pool even when no window of it is observed this read (Fable's panel rate-limited), so
    # the pool's key is always reserved: runs admitted while the panel is missing still count when it comes back.
    pool = core._scarcity_module().pool_for(lane, model or "", pools)
    relevant = [w for w in weekly if (w.get("pool") or "all") == "all" or (pool and w.get("pool") == pool)]
    current = [w for w in relevant if not w.get("stale")]
    if not [w for w in current if (w.get("pool") or "all") == "all"]:
        return f"no current weekly window to check {tag}", []
    for w in relevant:
        left = _left(w)
        if w.get("stale"):
            # A carried-forward reading is still a known figure: known low, or no figure at all, holds (Codex review
            # r3); only a pool with no reading whatever is bounded by the general windows alone.
            if left is None or left < floor:
                return f"{w.get('window')}: carried-forward reading {'has no figure' if left is None else f'{left:.0f}% left'}; held under {tag}", []
            continue
        if left is None:
            return f"{w.get('window')}: no usage figure to check {tag}", []
        per_call = assumed_pct_per_call(lane) * POOL_CAP_SCALE.get(w.get("pool") or "all", 1.0)
        n = reserved.get(_key(lane, w), 0)
        if left - (n + extra) * per_call < floor:
            return (f"{w.get('window')}: {left:.0f}% left, {n} run(s) outstanding; one more ({per_call:g}%) would pass "
                    f"the client's floor {tag}"), []
        reset = w.get("reset_epoch")
        if not isinstance(reset, (int, float)):
            return f"{w.get('window')}: reset time unknown; held under {tag}", []
        if reset - now < STOP_HOURS_BEFORE_RESET * 3600:
            return f"{w.get('window')}: resets within {STOP_HOURS_BEFORE_RESET:g} h; held under {tag}", []
    keys = {_key(lane, w) for w in current}
    if pool:
        keys.add(f"{lane}|pool|{pool}")
    return None, sorted(keys)


def _ledger(core):
    return core.STATE / LEDGER


def _reserved(rows: list[dict], reading: dict) -> dict:
    """Turns reserved since the reading was sampled (minus IN_FLIGHT_S): work admitted earlier is assumed visible."""
    since = float(reading.get("fetched_at") or 0) - IN_FLIGHT_S
    counts: dict = {}
    for row in rows:
        if float(row.get("ts", 0)) >= since:
            for k in row.get("keys", []):
                counts[k] = counts.get(k, 0) + 1
    return counts


def _rows(path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue                           # a torn last line from a crash: ignored, never fatal
            if isinstance(row, dict):
                rows.append(row)
    return rows


def check(core, cand: dict[str, Any], model: str, floor: float, readings: dict | None = None,
          now: float | None = None) -> str | None:
    """block() counting the outstanding reservations, without reserving (candidate filtering before ranking)."""
    readings = core.subscription_readings() if readings is None else readings
    reading = (readings or {}).get(cand.get("lane")) or {}
    mine = [r for r in _rows(_ledger(core)) if r.get("lane") == cand.get("lane")]
    return block(core, cand, model, floor, readings, _reserved(mine, reading), now)[0]


def reserve(core, cand: dict[str, Any], model: str, floor: float, readings: dict | None = None,
            now: float | None = None, conv: str | None = None, rid: str | None = None) -> str | None:
    """Check `cand` under `floor`, counting outstanding reservations, and on yes reserve one run on it: atomically
    (thread lock + flock on the ledger), durably. `conv` (the route_id) makes it idempotent per conversation: while the
    conversation's own reservation still counts, a continuation is rechecked without being charged again; once it has
    aged out of the counting window (the usage reading now shows the work), the next turn reserves afresh.
    `rid`: a caller-chosen id stamped on the new row so release(rid) can take it back if the run never starts.
    Returns why it was refused, or None when reserved."""
    now = time.time() if now is None else now
    readings = core.subscription_readings() if readings is None else readings
    path = _ledger(core)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, (path.parent / (LEDGER + ".lock")).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = _rows(path)
        reading = (readings or {}).get(cand.get("lane")) or {}
        mine = [r for r in rows if r.get("lane") == cand.get("lane")]
        since = float(reading.get("fetched_at") or 0) - IN_FLIGHT_S
        counted = bool(conv) and any(r.get("conv") == conv and float(r.get("ts", 0)) >= since for r in mine)
        why, keys = block(core, cand, model, floor, readings, _reserved(mine, reading), now, extra=0 if counted else 1)
        if why:
            return why
        if counted:
            return None
        horizon = now - MAX_READING_AGE_S - IN_FLIGHT_S     # rows older than this can no longer count
        keep = [r for r in rows if float(r.get("ts", 0)) >= horizon]
        row = {"ts": now, "lane": cand.get("lane"), "model": model, "keys": keys, "conv": conv}
        if rid:
            row["rid"] = rid
        keep.append(row)
        _write(path, keep)
        return None


def _write(path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(path)


def release(core, rid: str) -> bool:
    """Take back the reservation stamped `rid` (reserve(..., rid=)), under the same locks as reserve. Reason: a
    reservation only counts once its run actually started (#79); a run whose CLI failed to launch spent nothing, and
    leaving its row would count it until it aged out, so a burst of launch failures could push a healthy account under
    the client's floor. Idempotent: True when a row was removed, False when none carries `rid` (already released, aged
    out, or never reserved), so a second call can never refund a different run."""
    if not rid:
        return False
    path = _ledger(core)
    if not path.exists():
        return False
    with _thread_lock, (path.parent / (LEDGER + ".lock")).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = _rows(path)
        keep = [r for r in rows if r.get("rid") != rid]
        if len(keep) == len(rows):
            return False
        _write(path, keep)
        return True
