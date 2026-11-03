"""GET /v1/cortados/usage: one cheap JSON view across every resource, for a usage dashboard.

Every source is injectable (tests pass fixtures). Reading is cheap by rule: the cached scarcity.json only (never a
sample, which opens vendor CLIs), router.db read-only, child-tasks.jsonl and hardware/*.json from the state dir, and
one short S1 health call. A source that fails is named in `errors`; nothing falls back silently."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import stat
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
HW_MAX_BYTES = 64 << 10       # one node sample is ~1-2 KB; a larger file is not a sample and is never read into memory
HW_MAX_FILES = 32             # one file per node; the fleet is a handful of machines, so more means a stray directory
WINDOW_NAME_MAX = 60          # window names are captured vendor screen text, bounded only by the sampler's regex
COLLECT_CACHE_S = 2.0         # one 1 Hz dashboard re-scans router.db / the child log at most every 2 s; a memo miss is
                              # single-flighted per state dir, so N concurrent GETs run one collect (each scan holds a
                              # SHARED lock on router.db that delays core.log_route's commit)
POOLS_MAX = 16                # pool names are slugged from captured vendor screen text: a bounded set, like window names
ROUTED_TOP = 50               # lane x model pairs per period; the fleet has a few dozen, more is noise at dashboard size
HW_MODELS_MAX = 32            # models Ollama holds loaded on one node at once: a handful; more is not a real ps reply
HW_ERRORS_MAX = 16            # hardware.sample() records at most one error per field (~11 fields); more is not a sample
LABEL_MAX = 60                # lane keys / account labels come from scarcity.json keys: bounded like window names
DB_TIMEOUT_S = 0.5            # a dashboard GET must not wait on a router.db writer holding the lock
BAND_TOP = 5                  # the dashboard shows the five busiest bands; the rest are noise at this size
S1_BODY_MAX = 4096            # /health is a tiny JSON; only its status is used, the body is drained, bounded

# The keys scarcity.window_view writes (plus the pool/stale stamps the samplers add): the only ones passed through.
_WIN_KEYS = ("window", "pool", "used_pct", "headroom_pct", "resets_at", "reset_epoch", "resets_in_h", "scarcity",
             "expiring_pct", "stale")
# The fields cortados_router/hardware.py's sample() writes (saved as hardware/<host>.json): the only ones passed
# through, so an unexpected key (an SSH target, a tailnet address) never reaches the dashboard JSON. A field the
# sampler could not read is {"available": false, "reason": "<Type>: <message>"}; only `available` and the reason's kind
# (the part before the first colon, bounded) pass, since the message can carry a path or command line.
_HW_KEYS = ("schema", "sampled_at", "host", "os", "load_avg", "ncpu", "chip", "mem_total_bytes", "mem_available_bytes",
            "cpu_busy_pct", "gpu_busy_pct")
_HW_HEADROOM_KEYS = ("free_mem_gb", "cpu_idle_pct", "gpu_idle_pct")
_HW_MODEL_KEYS = ("name", "size", "size_vram")      # hardware.parse_ollama_ps's entries
# An unavailable field's reason is reduced to its kind (the part before the first colon), published only when it
# looks like an exception type name (<= 40 chars); anything else (a message, a path, a command line) becomes "error".
_KIND_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
# A hardware file name is published only when it is a plain bounded name; anything else is "<unnamed:i>".
_FNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Lanes without general (pool "all") windows are headlined by this pool: the same fallback scarcity.finish uses.
_FALLBACK_POOL = {"cursor": "cursor-models", "antigravity": "gemini"}
# Pools a lane shares with another product (Forest 2026-11-03, docs.x.ai/grok-bot: Cursor and Grok Bot draw on one
# weekly pool), so a dashboard reading "cursor" knows the headroom is not Cursor's alone.
SHARED_WITH = {"cursor": ["Grok Bot"]}


logger = logging.getLogger("cortados.provider.usage")


def _lane_label(lane: str) -> str:
    base = lane.split("#", 1)[0]
    shared = SHARED_WITH.get(base)
    return (f"{lane} (shared with {', '.join(shared)})" if shared else lane)[:LABEL_MAX]


def _account(lane: str) -> str | None:
    # the account label comes from the lane key (claude#b); config_dir is a filesystem path and never leaves
    return lane.split("#", 1)[1][:LABEL_MAX] if "#" in lane else None


def _finite(x: Any) -> Any:
    """Nulls every non-finite float at any depth: json.dumps would emit NaN/Infinity, which strict parsers reject."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_finite(v) for v in x]
    return x


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _one_lane(lane: str, v: dict[str, Any], now: float, freshness: Callable | None,
              configured: dict[str, str | None] | None = None) -> dict[str, Any]:
    fetched = float(v.get("fetched_at") or 0)
    if not math.isfinite(fetched):   # json.dumps would emit -Infinity/NaN, which strict JSON parsers reject
        fetched = 0.0
    if freshness is not None:
        fresh, failed = freshness(lane, v, STALE_AFTER_S, now)
    else:
        fresh, failed = 0 <= now - fetched < STALE_AFTER_S, False
    wins = [{k: w.get(k) for k in _WIN_KEYS if k in w} for w in (v.get("windows") or []) if isinstance(w, dict)]
    for w in wins:
        for k in ("window", "pool"):    # both come from captured vendor screen text (scarcity slugs pool from it)
            if isinstance(w.get(k), str):
                w[k] = w[k][:WINDOW_NAME_MAX]
        if _num(w.get("headroom_pct")):
            w["left_pct"] = float(w["headroom_pct"])
        elif _num(w.get("used_pct")):
            w["left_pct"] = round(100 - float(w["used_pct"]), 1)
    # The headline is the lane's GENERAL windows only (pool "all"), the set scarcity.finish routes on,
    # and never a window the sampler marked stale: a separately-metered or carried-forward pool does not speak for the lane.
    usable = [w for w in wins if not w.get("stale")]
    general = [w for w in usable if w.get("pool", "all") == "all"]
    if not general:
        general = [w for w in usable if w.get("pool") == _FALLBACK_POOL.get(lane.split("#", 1)[0])]
    lefts = [w["left_pct"] for w in general if isinstance(w.get("left_pct"), (int, float))]
    pools: dict[str, float] = {}
    pools_dropped = False
    for w in usable:
        p = w.get("pool", "all")
        if p not in ("all", "info") and isinstance(w.get("left_pct"), (int, float)):
            if p not in pools and len(pools) >= POOLS_MAX:
                pools_dropped = True
                continue
            left = w["left_pct"]
            pools[p] = left if p not in pools else min(pools[p], left)
    if v.get("error_kind"):
        err = str(v["error_kind"])[:LABEL_MAX]
    elif v.get("error"):     # an error scarcity did not classify: flagged, its raw text (vendor screen) never published
        err = "unclassified"
    else:
        err = None
    entry = {"lane": lane[:LABEL_MAX], "label": _lane_label(lane), "account": _account(lane),
             "left_pct": min(lefts) if lefts else None, "windows": wins, "pools": pools,
             "sample_age_s": round(now - fetched, 1) if fetched else None,
             "stale": not fresh or failed,
             # error_kind is scarcity's sanitised classification; the raw error can carry vendor screen text
             "error": err}
    # A positional Claude lane key (claude#<i>) can point at a different login after accounts.json is reordered: core's one
    # identity rule (_reading_for_other_login) decides, and such a reading publishes no number, as headroom_pct_for does.
    if configured is not None and lane.split("#", 1)[0] == "claude":
        from cortados_router import core
        if core._reading_for_other_login(v, {"config_dir": configured.get(lane)}):
            entry.update(left_pct=None, windows=[], pools={}, sample_age_s=None, stale=True, error="reading_for_other_login")
    shared = SHARED_WITH.get(lane.split("#", 1)[0])
    if shared:
        entry["shared_with"] = shared
    if pools_dropped and entry["pools"]:
        entry["_pools_truncated"] = True   # popped by collect into errors[]: a clipped pool map is said to be clipped
    return entry


def _lanes(scarcity: dict[str, Any], now: float, freshness: Callable | None,
           configured: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    out = []
    for lane, v in sorted((scarcity.get("lanes") or {}).items()):
        try:                    # one malformed lane is reported on its own, not by erasing the whole section
            out.append(_one_lane(lane, v, now, freshness, configured))
        except Exception as exc:
            # same shape as a good entry, flagged stale with no reading, so a dashboard never renders it healthy
            out.append({"lane": str(lane)[:LABEL_MAX], "label": _lane_label(str(lane)), "account": _account(str(lane)),
                        "left_pct": None, "windows": [], "pools": {}, "sample_age_s": None, "stale": True,
                        "error": type(exc).__name__})
    return out


def _routed(db_path: Path, now: float) -> dict[str, Any]:
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
    try:
        day0 = time.mktime(  # local midnight
            time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        def by(since: float) -> list[dict[str, Any]]:
            # lane/model are NULL for turns logged without a route: labelled, not shown as a null lane
            rows = c.execute("SELECT COALESCE(lane, '(none)'), COALESCE(model, '(none)'), COUNT(*) FROM routes "
                             "WHERE ts >= ? GROUP BY 1, 2 ORDER BY 3 DESC LIMIT ?", (since, ROUTED_TOP)).fetchall()
            return [{"lane": l, "model": m, "calls": n} for l, m, n in rows]
        today, week = by(day0), by(now - 7 * 86400)
        bands = c.execute("SELECT COALESCE(band, '(none)'), COUNT(*) FROM routes WHERE ts >= ? GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
                          (now - 7 * 86400, BAND_TOP)).fetchall()
        res = {"source": "router.db", "today": today, "last_7d": week,
               "top_bands_7d": [{"band": b, "calls": n} for b, n in bands]}
        # a list that filled its LIMIT may have been cut: said so (a count equal to the cap cannot tell "exactly N")
        if len(today) >= ROUTED_TOP or len(week) >= ROUTED_TOP:
            res["truncated"] = True
        if len(bands) >= BAND_TOP:
            res["bands_truncated"] = True
        return res
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
            recent = isinstance(rec, dict) and rec.get("task_id") and float(rec.get("ts") or 0) >= now - 7 * 86400
        except (ValueError, TypeError):   # one bad record (or a non-numeric ts) is counted, not the section erased
            bad += 1
            continue
        if recent:
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
            r.read(S1_BODY_MAX)
            res = {"available": True, "status": r.status, "latency_ms": round((time.monotonic() - t) * 1000)}
    except Exception as exc:
        res = {"available": False, "reason": type(exc).__name__, "latency_ms": round((time.monotonic() - t) * 1000)}
    with _S1_LOCK:
        _S1_CACHE[url] = (time.monotonic(), res)
    return dict(res)


def _hw_kind(reason: Any) -> str:
    """'<Type>: <message>' -> '<Type>' when that part is identifier-like (an exception type name); otherwise 'error'."""
    kind = str(reason).split(":", 1)[0].strip()
    return kind if _KIND_RE.fullmatch(kind) else "error"


def _hw_value(x: Any, *, key: str = "") -> Any:
    if isinstance(x, dict):   # the sampler's unavailable(reason) shape is the only dict a scalar field can hold
        return {"available": False, "reason": _hw_kind(x.get("reason"))}
    if isinstance(x, list):
        # hardware.sample() writes exactly one list field, load_avg (three numbers): capped to 3, each a finite number
        # or null. Any other list (nested lists/dicts can carry an SSH target, a tailnet address, a path, or be
        # unbounded) is never published verbatim.
        if key == "load_avg":
            return [v if _num(v) else None for v in x[:3]]
        return {"available": False, "reason": "unexpected_type"}
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x[:WINDOW_NAME_MAX] if isinstance(x, str) else x


def _hw_name(name: str, i: int) -> str:
    """A hardware file name as published: plain and bounded, or '<unnamed:i>' (a name is attacker-chosen text; `i`,
    the freshest-first position, keeps two odd names apart in errors[] and hardware[])."""
    return name if _FNAME_RE.fullmatch(name) else f"<unnamed:{i}>"


def _hw_sample(raw: Any, truncated: list[str] | None = None) -> dict[str, Any]:
    """Only hardware.sample()'s fields; headroom, ollama_models and errors reduced to their known sub-fields.
    A list cut to its cap appends '<list>_truncated' to `truncated`, so a clipped card is said to be clipped."""
    truncated = [] if truncated is None else truncated
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    out = {k: _hw_value(raw[k], key=k) for k in _HW_KEYS if k in raw}
    if isinstance(raw.get("headroom"), dict):
        out["headroom"] = {k: _hw_value(raw["headroom"].get(k)) for k in _HW_HEADROOM_KEYS}
    models = raw.get("ollama_models")
    if isinstance(models, list):
        out["ollama_models"] = [{k: _hw_value(m[k]) for k in _HW_MODEL_KEYS if k in m}
                                for m in models[:HW_MODELS_MAX] if isinstance(m, dict)]
        if len(models) > HW_MODELS_MAX:
            truncated.append("models_truncated")
    elif isinstance(models, dict):
        out["ollama_models"] = _hw_value(models)
    if isinstance(raw.get("errors"), list):   # the error text can carry a host, path or command line: its kind only
        out["errors"] = [{"field": str(e.get("field"))[:WINDOW_NAME_MAX], "error": _hw_kind(e.get("error"))}
                         for e in raw["errors"][:HW_ERRORS_MAX] if isinstance(e, dict)]
        if len(raw["errors"]) > HW_ERRORS_MAX:
            truncated.append("errors_truncated")
    return _finite(out)


def _hardware(d: Path, now: float, errors: list) -> list[dict[str, Any]]:
    try:   # Path.glob swallows a listing OSError, which would look exactly like "no samples yet"
        files = [p for p in (d / n for n in os.listdir(d)) if p.suffix == ".json"]
    except OSError as exc:
        logger.warning("usage: hardware listing failed: %s", exc)
        errors.append({"source": "hardware", "error": type(exc).__name__})
        return []
    def mtime(p: Path) -> float:
        try:
            return os.lstat(p).st_mtime   # lstat: never follows a link, never opens (a FIFO is only stat'ed)
        except OSError:          # vanished or unreadable: sorted last, reported by the read below
            return float("-inf")
    files.sort(key=lambda p: (-mtime(p), p.name))   # freshest first, so the cap drops the oldest, not by alphabet
    if len(files) > HW_MAX_FILES:
        errors.append({"source": "hardware", "error": "too_many_files"})
        files = files[:HW_MAX_FILES]
    out = []
    for i, f in enumerate(files):
        name = _hw_name(f.name, i)
        src = f"hardware/{name}"
        try:
            # lstat, never stat: a FIFO (or a link to one) would block the open forever and leak a request thread per
            # poll; a symlink could point anywhere. hardware.py writes regular files only.
            st = os.lstat(f)
            if not stat.S_ISREG(st.st_mode):
                errors.append({"source": src, "error": "not_a_regular_file"})
                continue
            if st.st_size > HW_MAX_BYTES:
                errors.append({"source": src, "error": "oversize"})
                continue
            # O_NONBLOCK: a file swapped for a FIFO between lstat and open still cannot block; O_NOFOLLOW: nor a link
            fd = os.open(f, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    errors.append({"source": src, "error": "not_a_regular_file"})
                    continue
                raw = fh.read(HW_MAX_BYTES + 1)
            if len(raw) > HW_MAX_BYTES:   # grew between stat and read
                errors.append({"source": src, "error": "oversize"})
                continue
            truncated: list[str] = []
            data = _hw_sample(json.loads(raw), truncated)
            if not data:   # none of hardware.sample()'s fields: said so, never an empty card that reads as fine
                errors.append({"source": src, "error": "no_known_fields"})
            errors.extend({"source": src, "error": t} for t in truncated)
            out.append({"file": name, "age_s": round(now - st.st_mtime, 1), "data": data})
        except Exception as exc:
            logger.warning("usage: %s failed: %s", src, type(exc).__name__)
            errors.append({"source": src, "error": type(exc).__name__})   # OSError text carries the path
    return out


_COLLECT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_COLLECT_LOCK = threading.Lock()                       # guards the two dicts; never held across a collect
_COLLECT_FLIGHT: dict[str, threading.Lock] = {}        # one per state dir: held across a miss (single flight)
# Keys are state dirs: one in production, a handful in tests. Bounded so a caller passing many dirs cannot grow either
# dict without limit; past the cap the oldest entries go (an evicted flight lock only costs one duplicate collect).
_COLLECT_KEYS_MAX = 8


def _evict_locked() -> None:
    """Called with _COLLECT_LOCK held: drops the oldest keys of both dicts past _COLLECT_KEYS_MAX (insertion order)."""
    for d in (_COLLECT_CACHE, _COLLECT_FLIGHT):
        while len(d) > _COLLECT_KEYS_MAX:
            del d[next(iter(d))]


def collect(*, state_dir: Path, scarcity_reader: Callable[[], dict[str, Any]], refusals: list[dict[str, Any]],
            runs_since_start: dict[str, int] | None = None, s1_url: str | None = None,
            s1_probe: Callable[[str | None], dict[str, Any]] = _s1, freshness: Callable | None = None,
            claude_lanes: list[tuple[str, str | None]] | None = None, now: float | None = None) -> dict[str, Any]:
    if now is None:   # live calls (no injected clock) are memoised COLLECT_CACHE_S; tests pass now and never hit it
        key = str(state_dir)
        def fresh() -> dict[str, Any] | None:
            with _COLLECT_LOCK:
                hit = _COLLECT_CACHE.get(key)
                return hit[1] if hit and time.monotonic() - hit[0] < COLLECT_CACHE_S else None
        res = fresh()
        if res is None:
            with _COLLECT_LOCK:
                flight = _COLLECT_FLIGHT.setdefault(key, threading.Lock())
                _evict_locked()
            with flight:      # concurrent misses wait here; the first collects, the rest read its result
                res = fresh()
                if res is None:
                    res = collect(state_dir=state_dir, scarcity_reader=scarcity_reader, refusals=refusals,
                                  runs_since_start=runs_since_start, s1_url=s1_url, s1_probe=s1_probe,
                                  freshness=freshness, claude_lanes=claude_lanes, now=time.time())
                    with _COLLECT_LOCK:
                        _COLLECT_CACHE.pop(key, None)   # re-inserted last, so eviction drops the stalest
                        _COLLECT_CACHE[key] = (time.monotonic(), res)
                        _evict_locked()
        return json.loads(json.dumps(res))
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    out: dict[str, Any] = {"generated_at": round(now, 3)}

    def guard(name: str, fn: Callable[[], Any], empty: Any) -> Any:
        try:
            return fn()
        except Exception as exc:
            # the bare type only: OSError text carries the absolute path (home dir, username); detail goes to the log
            logger.warning("usage: %s failed: %s", name, exc)
            errors.append({"source": name, "error": type(exc).__name__})
            return empty

    # claude_lanes: scarcity.claude_lanes()'s (lane_key, config_dir), the configured login per lane key (None: unchecked)
    configured = dict(claude_lanes) if claude_lanes is not None else None
    out["lanes"] = guard("scarcity", lambda: _lanes(scarcity_reader(), now, freshness, configured), [])
    for lane in out["lanes"]:
        if lane.pop("_pools_truncated", False):
            errors.append({"source": "scarcity", "error": "pools_truncated"})
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
    # last step, over the whole view: a non-finite float at any depth of any section becomes null, so the strict
    # encoding in the server never turns one stray NaN into a 500
    return _finite(out)
