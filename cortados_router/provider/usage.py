"""GET /v1/cortados/usage: one cheap JSON view across every resource, for a usage dashboard.

Every source is injectable (tests pass fixtures). Reading is cheap by rule: the cached scarcity.json only (never a
sample, which opens vendor CLIs), router.db read-only, child-tasks.jsonl and hardware/*.json from the state dir, and
one short S1 health call. A source that fails is named in `errors`; nothing falls back silently."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

STALE_AFTER_S = 6 * 3600      # scarcity.reading_freshness's routing default: the dashboard calls stale what routing does
S1_TIMEOUT_S = 1.5            # a dashboard GET must not hang on an unreachable scorer
S1_CACHE_S = 10.0             # a 1 Hz dashboard must not add 1 req/s (and up to 1.5 s per GET) to the scorer
CHILD_TAIL = 5000             # recent child-task events parsed (the log only grows and is never rotated)
CHILD_TAIL_BYTES = 2 << 20    # ... and bytes read from its end: the read itself is bounded, not only the parse

# The keys scarcity.window_view writes (plus the pool/stale stamps the samplers add): the only ones passed through.
_WIN_KEYS = ("window", "pool", "used_pct", "headroom_pct", "resets_at", "reset_epoch", "resets_in_h", "scarcity",
             "expiring_pct", "stale")
# Lanes without general (pool "all") windows are headlined by this pool: the same fallback scarcity.finish routes on.
_FALLBACK_POOL = {"cursor": "cursor-models", "antigravity": "gemini"}
# Pools a lane shares with another product (Forest 2026-11-03, docs.x.ai/grok-bot: Cursor and Grok Bot draw on one
# weekly pool), so a dashboard reading "cursor" knows the headroom is not Cursor's alone.
SHARED_WITH = {"cursor": ["Grok Bot"]}


def _lane_label(lane: str) -> str:
    base = lane.split("#", 1)[0]
    shared = SHARED_WITH.get(base)
    return f"{lane} (shared with {', '.join(shared)})" if shared else lane


def _one_lane(lane: str, v: dict[str, Any], now: float, freshness: Callable | None) -> dict[str, Any]:
    fetched = float(v.get("fetched_at") or 0)
    if freshness is not None:
        fresh, failed = freshness(lane, v, STALE_AFTER_S, now)
    else:
        fresh, failed = 0 <= now - fetched < STALE_AFTER_S, False
    wins = [{k: w.get(k) for k in _WIN_KEYS if k in w} for w in (v.get("windows") or []) if isinstance(w, dict)]
    for w in wins:
        if isinstance(w.get("headroom_pct"), (int, float)):
            w["left_pct"] = float(w["headroom_pct"])
        elif isinstance(w.get("used_pct"), (int, float)):
            w["left_pct"] = round(100 - float(w["used_pct"]), 1)
    # The headline is the lane's GENERAL windows only (pool "all"), the set scarcity.finish / lane_scarcity route on,
    # and never a window the sampler marked stale: a separately-metered or carried-forward pool does not speak for the lane.
    usable = [w for w in wins if not w.get("stale")]
    general = [w for w in usable if w.get("pool", "all") == "all"]
    if not general:
        general = [w for w in usable if w.get("pool") == _FALLBACK_POOL.get(lane.split("#", 1)[0])]
    lefts = [w["left_pct"] for w in general if isinstance(w.get("left_pct"), (int, float))]
    pools: dict[str, float] = {}
    for w in usable:
        p = w.get("pool", "all")
        if p not in ("all", "info") and isinstance(w.get("left_pct"), (int, float)):
            pools[p] = min(pools.get(p, 101.0), w["left_pct"])
    entry = {"lane": lane, "label": _lane_label(lane),
             # the account label comes from the lane key (claude#b); config_dir is a filesystem path and never leaves
             "account": lane.split("#", 1)[1] if "#" in lane else None,
             "left_pct": min(lefts) if lefts else None, "windows": wins, "pools": pools,
             "sample_age_s": round(now - fetched, 1) if fetched else None,
             "stale": not fresh or failed,
             # error_kind is scarcity's sanitised classification; the raw error can carry vendor screen text
             "error": v.get("error_kind") if v.get("error") or v.get("error_kind") else None}
    shared = SHARED_WITH.get(lane.split("#", 1)[0])
    if shared:
        entry["shared_with"] = shared
    return entry


def _lanes(scarcity: dict[str, Any], now: float, freshness: Callable | None) -> list[dict[str, Any]]:
    out = []
    for lane, v in sorted((scarcity.get("lanes") or {}).items()):
        try:                    # one malformed lane is reported on its own, not by erasing the whole section
            out.append(_one_lane(lane, v, now, freshness))
        except Exception as exc:
            out.append({"lane": lane, "error": f"{type(exc).__name__}"})
    return out


def _routed(db_path: Path, now: float) -> dict[str, Any]:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
    try:
        day0 = time.mktime(  # local midnight
            time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        def by(since: float) -> list[dict[str, Any]]:
            rows = c.execute("SELECT lane, model, COUNT(*) FROM routes WHERE ts >= ? GROUP BY lane, model "
                             "ORDER BY 3 DESC", (since,)).fetchall()
            return [{"lane": l, "model": m, "calls": n} for l, m, n in rows]
        bands = c.execute("SELECT band, COUNT(*) FROM routes WHERE ts >= ? GROUP BY band ORDER BY 2 DESC LIMIT 5",
                          (now - 7 * 86400,)).fetchall()
        return {"source": "router.db", "today": by(day0), "last_7d": by(now - 7 * 86400),
                "top_bands_7d": [{"band": b, "calls": n} for b, n in bands]}
    finally:
        c.close()


def _child_tasks(path: Path, now: float) -> dict[str, Any]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - CHILD_TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    truncated = size > CHILD_TAIL_BYTES
    if truncated:
        lines = lines[1:]                 # the first line was cut mid-record
    if len(lines) > CHILD_TAIL:
        truncated = True
        lines = lines[-CHILD_TAIL:]
    last: dict[str, str] = {}
    status: dict[str, str] = {}
    bad = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict) and rec.get("task_id") and float(rec.get("ts") or 0) >= now - 7 * 86400:
            tid = str(rec["task_id"])
            last[tid] = str(rec.get("event"))          # child_tasks.log_event writes {"ts", "event", **fields}
            if rec.get("event") == "finished" and rec.get("status") is not None:
                status[tid] = str(rec["status"])
    # truncated: the 7-day window may be clipped (only the log's tail is read), so counts can under-report
    return {"window": "7d", "by_event": dict(Counter(last.values())), "by_status": dict(Counter(status.values())),
            "unparsable_lines": bad, "truncated": truncated}


_S1_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_S1_LOCK = threading.Lock()
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # a loopback/tailnet probe never takes a proxy


def _s1(url: str | None) -> dict[str, Any]:
    """S1 /health, memoised S1_CACHE_S. The URL is never echoed: it is a machine-local tailnet address."""
    if not url:
        return {"available": False, "reason": "no S1 URL configured"}
    with _S1_LOCK:
        hit = _S1_CACHE.get(url)
        if hit and time.monotonic() - hit[0] < S1_CACHE_S:
            return dict(hit[1])
    t = time.monotonic()
    try:
        with _NO_PROXY.open(f"{url.rstrip('/')}/health", timeout=S1_TIMEOUT_S) as r:
            r.read(4096)
            res = {"available": True, "status": r.status, "latency_ms": round((time.monotonic() - t) * 1000)}
    except Exception as exc:
        res = {"available": False, "reason": type(exc).__name__, "latency_ms": round((time.monotonic() - t) * 1000)}
    with _S1_LOCK:
        _S1_CACHE[url] = (time.monotonic(), res)
    return dict(res)


def _hardware(d: Path, now: float, errors: list) -> list[dict[str, Any]]:
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            out.append({"file": f.name, "age_s": round(now - f.stat().st_mtime, 1), "data": json.loads(f.read_text())})
        except Exception as exc:
            errors.append({"source": f"hardware/{f.name}", "error": type(exc).__name__})   # OSError text carries the path
    return out


def collect(*, state_dir: Path, scarcity_reader: Callable[[], dict[str, Any]], refusals: list[dict[str, Any]],
            runs_since_start: dict[str, int] | None = None, s1_url: str | None = None,
            s1_probe: Callable[[str | None], dict[str, Any]] = _s1, freshness: Callable | None = None,
            now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    out: dict[str, Any] = {"generated_at": round(now, 3)}

    def guard(name: str, fn: Callable[[], Any], empty: Any) -> Any:
        try:
            return fn()
        except Exception as exc:
            errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"})
            return empty

    out["lanes"] = guard("scarcity", lambda: _lanes(scarcity_reader(), now, freshness), [])
    db = state_dir / "router.db"
    if db.exists():
        out["routed"] = guard("router.db", lambda: _routed(db, now), {})
    else:
        errors.append({"source": "router.db", "error": "absent"})
        out["routed"] = {}
    if runs_since_start is not None:
        out["routed"]["provider_since_start"] = runs_since_start
        notes.append("routed.provider_since_start comes from the provider's in-memory run records: since process "
                     "start, bounded LRU, no timestamps")
    recent = [r for r in refusals if float(r.get("at") or 0) >= now - 86400]
    out["refusals"] = {"window": "24h", "by_reason": dict(Counter(r.get("reason") for r in recent)),
                       "total": len(recent)}
    notes.append("refusals are the provider's in-memory records (authenticated requests naming a run id); a restart forgets them")
    cpath = state_dir / "child-tasks.jsonl"
    if cpath.exists():
        out["child_tasks"] = guard("child-tasks.jsonl", lambda: _child_tasks(cpath, now), {})
    else:
        errors.append({"source": "child-tasks.jsonl", "error": "absent"})
        out["child_tasks"] = {}
    out["s1"] = guard("s1", lambda: s1_probe(s1_url), {"available": False, "reason": "probe failed"})
    if not out["s1"].get("available"):
        errors.append({"source": "s1", "error": str(out["s1"].get("reason"))})
    hw = state_dir / "hardware"
    if hw.is_dir():
        out["hardware"] = guard("hardware", lambda: _hardware(hw, now, errors), [])
    else:
        errors.append({"source": "hardware", "error": "absent"})
        out["hardware"] = []
    out["notes"] = notes
    out["errors"] = errors
    return out
