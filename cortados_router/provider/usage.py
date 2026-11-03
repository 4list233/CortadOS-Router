"""GET /v1/cortados/usage: one cheap JSON view across every resource, for a usage dashboard.

Every source is injectable (tests pass fixtures). Reading is cheap by rule: the cached scarcity.json only (never a
sample, which opens vendor CLIs), router.db read-only, child-tasks.jsonl and hardware/*.json from the state dir, and
one short S1 health call. A source that fails is named in `errors`; nothing falls back silently."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable

STALE_AFTER_S = 6 * 3600      # scarcity.reading_freshness's routing default: the dashboard calls stale what routing does
S1_TIMEOUT_S = 1.5            # a dashboard GET must not hang on an unreachable scorer
CHILD_TAIL = 5000             # recent child-task events read (the log only grows; bound the read)


def _lanes(scarcity: dict[str, Any], now: float, freshness: Callable | None) -> list[dict[str, Any]]:
    out = []
    for lane, v in sorted((scarcity.get("lanes") or {}).items()):
        fetched = float(v.get("fetched_at") or 0)
        if freshness is not None:
            fresh, failed = freshness(lane, v, STALE_AFTER_S, now)
        else:
            fresh, failed = 0 <= now - fetched < STALE_AFTER_S, False
        wins = [{k: w.get(k) for k in ("name", "pool", "used_pct", "left_pct", "reset_at", "resets_in_h", "window_s",
                                        "stale") if k in w} for w in (v.get("windows") or [])]
        for w in wins:
            if "left_pct" not in w and isinstance(w.get("used_pct"), (int, float)):
                w["left_pct"] = round(100 - float(w["used_pct"]), 1)
        lefts = [w["left_pct"] for w in wins if isinstance(w.get("left_pct"), (int, float))]
        out.append({"lane": lane, "account": v.get("account") or v.get("config_dir"),
                    "left_pct": min(lefts) if lefts else None, "windows": wins,
                    "sample_age_s": round(now - fetched, 1) if fetched else None,
                    "stale": not fresh or failed, "error": v.get("error")})
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
    lines = path.read_text().splitlines()[-CHILD_TAIL:]
    last: dict[str, str] = {}
    bad = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict) and rec.get("task_id") and float(rec.get("ts") or 0) >= now - 7 * 86400:
            last[str(rec["task_id"])] = str(rec.get("state") or rec.get("event"))
    return {"window": "7d", "by_state": dict(Counter(last.values())), "unparsable_lines": bad}


def _s1(url: str | None) -> dict[str, Any]:
    if not url:
        return {"available": False, "reason": "no S1 URL configured"}
    t = time.monotonic()
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=S1_TIMEOUT_S) as r:
            r.read(4096)
            return {"available": True, "url": url, "status": r.status, "latency_ms": round((time.monotonic() - t) * 1000)}
    except Exception as exc:
        return {"available": False, "url": url, "reason": type(exc).__name__,
                "latency_ms": round((time.monotonic() - t) * 1000)}


def _hardware(d: Path, now: float, errors: list) -> list[dict[str, Any]]:
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append({"file": f.name, "age_s": round(now - f.stat().st_mtime, 1), "data": json.loads(f.read_text())})
        except Exception as exc:
            errors.append({"source": f"hardware/{f.name}", "error": f"{type(exc).__name__}: {exc}"})
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
    out["hardware"] = _hardware(state_dir / "hardware", now, errors)
    out["notes"] = notes
    out["errors"] = errors
    return out
