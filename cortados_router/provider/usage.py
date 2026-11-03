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

# The keys scarcity.window_view writes (plus the pool/stale stamps the samplers add): the only ones read.
_WIN_KEYS = ("window", "pool", "used_pct", "headroom_pct", "resets_at", "reset_epoch", "resets_in_h", "scarcity",
             "expiring_pct", "stale")
# The fields cortados_router/hardware.py's sample() writes (saved as hardware/<host>.json): the only ones read.
_HW_KEYS = ("schema", "sampled_at", "host", "os", "load_avg", "ncpu", "chip", "mem_total_bytes", "mem_available_bytes",
            "cpu_busy_pct", "gpu_busy_pct")
_HW_HEADROOM_KEYS = ("free_mem_gb", "cpu_idle_pct", "gpu_idle_pct")
_HW_MODEL_KEYS = ("name", "size", "size_vram")      # hardware.parse_ollama_ps's entries
# The field names hardware.sample() records errors under: errors[].field is published only as one of these.
_HW_ERROR_FIELDS = frozenset(_HW_KEYS + _HW_HEADROOM_KEYS + ("platform", "ollama_models"))
# An unavailable field's reason is reduced to its kind (the part before the first colon), published only when it
# looks like an exception type name (<= 40 chars); anything else (a message, a path, a command line) becomes "error".
_KIND_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
# A hardware file name is published only when it is a plain bounded name; anything else is "<unnamed:i>".
_FNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_UNNAMED_RE = re.compile(r"<unnamed:[0-9]{1,4}>")
# Lanes without general (pool "all") windows are headlined by this pool: the same fallback scarcity.finish uses.
_FALLBACK_POOL = {"cursor": "cursor-models", "antigravity": "gemini"}
# Pools a lane shares with another product (Forest 2026-11-03, docs.x.ai/grok-bot: Cursor and Grok Bot draw on one
# weekly pool), so a dashboard reading "cursor" knows the headroom is not Cursor's alone.
SHARED_WITH = {"cursor": ["Grok Bot"]}

# ---- publish caps: every list/map in the view is bounded, and a cut names its source in errors[] ----
LANES_MAX = 32         # the fleet has ~10 lanes (claude#a..d, codex, cursor, antigravity, local); more is not a reading
WINDOWS_MAX = 16       # a vendor /usage screen shows 2-6 windows per lane; 200 x 200 would be a ~3 MB body
ERRORS_MAX = 64        # ~one per source plus one per hardware file (<= 32) and per-lane cuts; more is noise
MAP_MAX = 32           # counters keyed by file text (child events/statuses, refusal reasons): a handful are real
RUNS_MAX = 64          # provider/model pairs since start: a few dozen in a busy day
SHARED_MAX = 4         # products sharing a pool: one today (Grok Bot)
LOAD_AVG_LEN = 3       # os.getloadavg(): 1, 5 and 15 minutes
TEXT_MAX = 60          # window names, model ids, ISO times, kinds: all well under 60 in real readings
REASON_MAX = 120       # refusal reasons are the endpoint's own fixed sentences (server._REFUSAL_REASONS, ~70 chars)
# The one string rule: plain text only. No '/', '\\', '@', '<', '{', quotes (a path, an SSH target, markup or a
# serialised object cannot pass); '#' for lane keys (claude#b), "'" for the endpoint's own sentences, '\u2026' for the
# ellipsis Cursor's screen prints. A string outside it, or holding an IPv4-looking address, publishes as "error".
_TEXT_RE = re.compile(r"[A-Za-z0-9 ._:()+%,#'\u2026-]*")
_IPV4_RE = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")
_SOURCES = ("scarcity", "router.db", "child-tasks.jsonl", "s1", "hardware", "usage")
_NOTE_RUNS = ("routed.provider_since_start comes from the provider's in-memory run records: since process "
              "start, bounded LRU, no timestamps")
_NOTE_REFUSALS = ("refusals are the provider's in-memory records (authenticated requests naming a run id); "
                  "a restart forgets them")


logger = logging.getLogger("cortados.provider.usage")


def _lane_label(lane: str) -> str:
    base = lane.split("#", 1)[0]
    shared = SHARED_WITH.get(base)
    return (f"{lane} (shared with {', '.join(shared)})" if shared else lane)[:LABEL_MAX]


def _account(lane: str) -> str | None:
    # the account label comes from the lane key (claude#b); config_dir is a filesystem path and never leaves
    return lane.split("#", 1)[1][:LABEL_MAX] if "#" in lane else None


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
    for w in usable:   # every pool's minimum; the publish pass caps the map at POOLS_MAX and says so
        p = w.get("pool", "all")
        if isinstance(p, str) and p not in ("all", "info") and isinstance(w.get("left_pct"), (int, float)):
            left = w["left_pct"]
            pools[p] = left if p not in pools else min(pools[p], left)
    if v.get("error_kind"):
        err = str(v["error_kind"])   # published only if it passes the text rule (a dict becomes "error")
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
        if len(today) >= ROUTED_TOP:
            res["today_truncated"] = True
        if len(week) >= ROUTED_TOP:
            res["last_7d_truncated"] = True
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


def _kind(reason: Any) -> str:
    """'<Type>: <message>' -> '<Type>' when that part is identifier-like (an exception type name); otherwise 'error'."""
    kind = reason.split(":", 1)[0].strip() if isinstance(reason, str) else ""
    return kind if _KIND_RE.fullmatch(kind) else "error"


def _hw_name(name: str, i: int) -> str:
    """A hardware file name as published: plain and bounded, or '<unnamed:i>' (a name is attacker-chosen text; `i`,
    the freshest-first position, keeps two odd names apart in errors[] and hardware[])."""
    return name if _FNAME_RE.fullmatch(name) else f"<unnamed:{i}>"


def _hw_sample(raw: Any) -> dict[str, Any]:
    """Only hardware.sample()'s top-level fields; their values are reduced by the publish pass (_HW_CARD)."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    return {k: raw[k] for k in _HW_KEYS + ("headroom", "ollama_models", "errors") if k in raw}


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
            data = _hw_sample(json.loads(raw))
            if not data:   # none of hardware.sample()'s fields: said so, never an empty card that reads as fine
                errors.append({"source": src, "error": "no_known_fields"})
            out.append({"file": name, "age_s": round(now - st.st_mtime, 1), "data": data})
        except Exception as exc:
            logger.warning("usage: %s failed: %s", src, type(exc).__name__)
            errors.append({"source": src, "error": type(exc).__name__})   # OSError text carries the path
    return out


# ---------------------------------------------------------------- the publish pass
# Every value leaves collect through _publish and this schema, whatever section built it: a key not declared here is
# dropped, a leaf gets only its declared type (a container where a leaf is expected becomes null), a string only the
# text rule, and every list/map is capped with the cut named in errors[]. Six review rounds each found one more field
# where file content (a nested dict, a path, an SSH target, a 100 KB string) passed through a builder; this pass makes
# the builders' own filtering a convenience, not the guarantee.

class _Obj:
    def __init__(self, fields: dict[str, Any], source: tuple[str, str] | None = None):
        self.fields, self.source = fields, source   # source: (prefix, key) names cuts inside as "<prefix>/<value>"


class _List:
    def __init__(self, item: Any, cap: int, cut: str):
        self.item, self.cap, self.cut = item, cap, cut


class _Map:
    def __init__(self, key: Callable[[Any], Any], value: Any, cap: int, cut: str):
        self.key, self.value, self.cap, self.cut = key, value, cap, cut


class _Hw:
    """A hardware.sample() field: its value, or the sampler's {"available": false, "reason"} shape (reason: kind only).
    A container the field never holds is {"available": false, "reason": "unexpected_type"}."""
    def __init__(self, inner: Any):
        self.inner = inner


def _text(x: Any, max_len: int = TEXT_MAX) -> str | None:
    if not isinstance(x, str):
        return None
    if not _TEXT_RE.fullmatch(x) or _IPV4_RE.search(x):
        return "error"
    return x[:max_len]


def _label(x: Any) -> str | None:
    return _text(x, LABEL_MAX)


def _reason(x: Any) -> str | None:
    return _text(x, REASON_MAX)


def _number(x: Any) -> Any:
    return x if _num(x) else None


def _bool(x: Any) -> Any:
    return x if isinstance(x, bool) else None


def _enum(*allowed: str) -> Callable[[Any], Any]:
    return lambda x: x if isinstance(x, str) and x in allowed else None


def _file(x: Any) -> str | None:
    if not isinstance(x, str):
        return None
    return x if _FNAME_RE.fullmatch(x) or _UNNAMED_RE.fullmatch(x) else "error"


def _hw_field(x: Any) -> str | None:
    return x if isinstance(x, str) and x in _HW_ERROR_FIELDS else "unknown"


def _source(x: Any) -> str | None:
    """errors[].source: one of _SOURCES, optionally '/<hardware file>' or '/<lane key>'."""
    if not isinstance(x, str):
        return None
    head, sep, tail = x.partition("/")
    if head not in _SOURCES:
        return "unknown"
    return f"{head}/{_file(tail) if head == 'hardware' else _label(tail)}" if sep else head


def _run_key(x: Any) -> str | None:
    """provider_since_start keys are '<provider>/<model>': each half by the text rule, the one slash kept."""
    if not isinstance(x, str):
        return None
    a, sep, b = x.partition("/")
    return f"{_text(a)}/{_text(b)}" if sep and isinstance(b, str) else _text(x)


_UNAVAILABLE = {"available": _bool, "reason": _kind}
_WINDOW = _Obj({"window": _text, "pool": _text, "used_pct": _number, "headroom_pct": _number, "left_pct": _number,
                "resets_at": _text, "reset_epoch": _number, "resets_in_h": _number, "scarcity": _number,
                "expiring_pct": _number, "stale": _bool})
_LANE = _Obj({"lane": _label, "label": _label, "account": _label, "left_pct": _number,
              "windows": _List(_WINDOW, WINDOWS_MAX, "windows_truncated"),
              "pools": _Map(_text, _number, POOLS_MAX, "pools_truncated"),
              "sample_age_s": _number, "stale": _bool, "error": _label,
              "shared_with": _List(_label, SHARED_MAX, "shared_with_truncated")}, source=("scarcity", "lane"))
_HW_CARD = _Obj({**{k: _Hw(_text if k in ("host", "os", "chip") else _number) for k in _HW_KEYS},
                 "load_avg": _Hw(_List(_number, LOAD_AVG_LEN, "load_avg_truncated")),
                 "headroom": _Obj({k: _Hw(_number) for k in _HW_HEADROOM_KEYS}),
                 "ollama_models": _Hw(_List(_Obj({"name": _text, "size": _number, "size_vram": _number}),
                                            HW_MODELS_MAX, "models_truncated")),
                 "errors": _List(_Obj({"field": _hw_field, "error": _kind}), HW_ERRORS_MAX, "errors_truncated")})
_ROUTE = _Obj({"lane": _label, "model": _text, "calls": _number})
_COUNTS = lambda cut: _Map(_text, _number, MAP_MAX, cut)   # noqa: E731
_VIEW = _Obj({
    "generated_at": _number,
    "lanes": _List(_LANE, LANES_MAX, "lanes_truncated"),
    "routed": _Obj({"source": _enum("router.db"), "today": _List(_ROUTE, ROUTED_TOP, "today_truncated"),
                    "last_7d": _List(_ROUTE, ROUTED_TOP, "last_7d_truncated"),
                    "top_bands_7d": _List(_Obj({"band": _text, "calls": _number}), BAND_TOP, "bands_truncated"),
                    "today_truncated": _bool, "last_7d_truncated": _bool, "bands_truncated": _bool,
                    "provider_since_start": _Map(_run_key, _number, RUNS_MAX, "provider_since_start_truncated")}),
    "refusals": _Obj({"window": _enum("24h"), "by_reason": _Map(_reason, _number, MAP_MAX, "refusals_truncated"),
                      "total": _number}),
    "child_tasks": _Obj({"window": _enum("7d"), "by_event": _COUNTS("by_event_truncated"),
                         "by_status": _COUNTS("by_status_truncated"), "unparsable_lines": _number,
                         "truncated": _bool}),
    "s1": _Obj({"available": _bool, "status": _number, "latency_ms": _number, "reason": _label}),
    "hardware": _List(_Obj({"file": _file, "age_s": _number, "data": _HW_CARD}, source=("hardware", "file")),
                      HW_MAX_FILES, "too_many_files"),
    "notes": _List(_enum(_NOTE_RUNS, _NOTE_REFUSALS), 8, "notes_truncated"),
})
_ERROR = _Obj({"source": _source, "error": _label})
# the section a top-level cut is reported under (a lane or hardware card names itself through its _Obj source)
_SECTION_SOURCE = {"lanes": "scarcity", "routed": "router.db", "child_tasks": "child-tasks.jsonl", "s1": "s1",
                   "hardware": "hardware"}


def _pub(spec: Any, x: Any, src: str, cuts: list[tuple[str, str]]) -> Any:
    if x is None:
        return None
    if isinstance(spec, _Hw):
        if isinstance(x, dict):
            return {"available": False, "reason": _kind(x.get("reason"))}
        if isinstance(x, list) != isinstance(spec.inner, _List):
            return {"available": False, "reason": "unexpected_type"}
        return _pub(spec.inner, x, src, cuts)
    if isinstance(spec, _Obj):
        if not isinstance(x, dict):
            return None
        if spec.source:
            prefix, key = spec.source
            src = f"{prefix}/{_pub(spec.fields[key], x.get(key), src, cuts)}"
        return {k: _pub(f, x[k], src, cuts) for k, f in spec.fields.items() if k in x}
    if isinstance(spec, _List):
        if not isinstance(x, list):
            return None
        if len(x) > spec.cap:
            cuts.append((src, spec.cut))
        return [_pub(spec.item, v, src, cuts) for v in x[:spec.cap]]
    if isinstance(spec, _Map):
        if not isinstance(x, dict):
            return None
        out: dict[str, Any] = {}
        for k, v in x.items():
            pk = "(none)" if k is None else spec.key(k)
            if pk is None or pk in out:      # an unpublishable key, or two that publish alike: the first kept
                continue
            if len(out) >= spec.cap:
                cuts.append((src, spec.cut))
                break
            out[pk] = _pub(spec.value, v, src, cuts)
        return out
    if isinstance(x, (dict, list)):          # a container where a leaf is expected
        return None
    return spec(x)


def _publish(view: dict[str, Any], errors: list[Any]) -> dict[str, Any]:
    """The view as it may leave this module: every section through _VIEW, every cut appended to errors (deduped, in
    order), errors capped at ERRORS_MAX with a last 'usage/errors_truncated' entry when cut."""
    cuts: list[tuple[str, str]] = []
    out: dict[str, Any] = {}
    for k, spec in _VIEW.fields.items():
        if k in view:
            out[k] = _pub(spec, view[k], _SECTION_SOURCE.get(k, "usage"), cuts)
    errs = [e for e in (_pub(_ERROR, e, "usage", cuts) for e in errors) if e]
    seen = {(e.get("source"), e.get("error")) for e in errs}
    for src, cut in cuts:
        e = {"source": _source(src), "error": cut}
        if (e["source"], cut) not in seen:
            seen.add((e["source"], cut))
            errs.append(e)
    if len(errs) > ERRORS_MAX:
        errs = errs[:ERRORS_MAX - 1] + [{"source": "usage", "error": "errors_truncated"}]
    out["errors"] = errs
    return out


def _encode(view: dict[str, Any]) -> bytes:
    return json.dumps(view, allow_nan=False).encode()   # strict JSON; _publish leaves no non-finite number


_COLLECT_CACHE: dict[str, tuple[float, bytes]] = {}     # state dir -> (monotonic time, the encoded published view)
_COLLECT_LOCK = threading.Lock()                       # guards the two dicts; never held across a collect
_COLLECT_FLIGHT: dict[str, threading.Lock] = {}        # one per state dir: held across a miss (single flight)
# Keys are state dirs: one in production, a handful in tests. Bounded so a caller passing many dirs cannot grow either
# dict without limit; past the cap the least recently used go (a hit moves its key last, so the dir a dashboard polls
# survives a burst of others; an evicted flight lock only costs one duplicate collect).
_COLLECT_KEYS_MAX = 8


def _evict_locked() -> None:
    """Called with _COLLECT_LOCK held: drops the oldest keys of both dicts past _COLLECT_KEYS_MAX (insertion order)."""
    for d in (_COLLECT_CACHE, _COLLECT_FLIGHT):
        while len(d) > _COLLECT_KEYS_MAX:
            del d[next(iter(d))]


def _touch_locked(key: str) -> None:
    for d in (_COLLECT_CACHE, _COLLECT_FLIGHT):
        if key in d:
            d[key] = d.pop(key)


def collect(*, state_dir: Path, scarcity_reader: Callable[[], dict[str, Any]], refusals: list[dict[str, Any]],
            runs_since_start: dict[str, int] | None = None, s1_url: str | None = None,
            s1_probe: Callable[[str | None], dict[str, Any]] = _s1, freshness: Callable | None = None,
            claude_lanes: list[tuple[str, str | None]] | None = None, now: float | None = None,
            encoded: bool = False) -> dict[str, Any] | bytes:
    """The published view (a fresh dict), or with encoded=True its strict-JSON bytes (what the endpoint sends: the
    memo holds the bytes, so a hit serialises nothing)."""
    args = dict(state_dir=state_dir, scarcity_reader=scarcity_reader, refusals=refusals,
                runs_since_start=runs_since_start, s1_url=s1_url, s1_probe=s1_probe, freshness=freshness,
                claude_lanes=claude_lanes)
    if now is not None:   # tests pass now and never touch the memo
        view = _collect(now=now, **args)
        return _encode(view) if encoded else view
    # live calls (no injected clock) are memoised COLLECT_CACHE_S
    key = str(state_dir)
    def fresh() -> bytes | None:
        with _COLLECT_LOCK:
            hit = _COLLECT_CACHE.get(key)
            if hit and time.monotonic() - hit[0] < COLLECT_CACHE_S:
                _touch_locked(key)
                return hit[1]
            return None
    body = fresh()
    if body is None:
        with _COLLECT_LOCK:
            flight = _COLLECT_FLIGHT.setdefault(key, threading.Lock())
            _touch_locked(key)
            _evict_locked()
        with flight:      # concurrent misses wait here; the first collects, the rest read its result
            body = fresh()
            if body is None:
                body = _encode(_collect(now=time.time(), **args))
                with _COLLECT_LOCK:
                    _COLLECT_CACHE.pop(key, None)   # re-inserted last, so eviction drops the stalest
                    _COLLECT_CACHE[key] = (time.monotonic(), body)
                    _evict_locked()
    return body if encoded else json.loads(body)


def _collect(*, state_dir: Path, scarcity_reader: Callable[[], dict[str, Any]], refusals: list[dict[str, Any]],
             runs_since_start: dict[str, int] | None, s1_url: str | None, s1_probe: Callable[[str | None], dict[str, Any]],
             freshness: Callable | None, claude_lanes: list[tuple[str, str | None]] | None,
             now: float) -> dict[str, Any]:
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
    db = state_dir / "router.db"
    if db.exists():
        out["routed"] = guard("router.db", lambda: _routed(db, now), {})
    else:
        errors.append({"source": "router.db", "error": "absent"})
        out["routed"] = {}
    if runs_since_start is not None:
        out["routed"]["provider_since_start"] = runs_since_start
        notes.append(_NOTE_RUNS)
    recent = [r for r in refusals if float(r.get("at") or 0) >= now - 86400]
    out["refusals"] = {"window": "24h", "by_reason": dict(Counter(r.get("reason") for r in recent)),
                       "total": len(recent)}
    notes.append(_NOTE_REFUSALS)
    cpath = state_dir / "child-tasks.jsonl"
    if cpath.exists():
        out["child_tasks"] = guard("child-tasks.jsonl", lambda: _child_tasks(cpath, now), {})
    else:
        errors.append({"source": "child-tasks.jsonl", "error": "absent"})
        out["child_tasks"] = {}
    out["s1"] = guard("s1", lambda: s1_probe(s1_url), {"available": False, "reason": "probe failed"})
    if not isinstance(out["s1"], dict) or not out["s1"].get("available"):
        errors.append({"source": "s1", "error": str(out["s1"].get("reason") if isinstance(out["s1"], dict) else None)})
    hw = state_dir / "hardware"
    if hw.is_dir():
        out["hardware"] = guard("hardware", lambda: _hardware(hw, now, errors), [])
    else:
        errors.append({"source": "hardware", "error": "absent"})
        out["hardware"] = []
    out["notes"] = notes
    # last step, over the whole view: the declared schema is the only way out (see _publish)
    return _publish(out, errors)
