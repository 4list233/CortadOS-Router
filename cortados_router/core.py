"""Router core: typed decision (interim engine), band resolution, lane choice, sub-agent count, logging.
Pure stdlib so it runs inside Hermes' venv and from the command line:  python core.py decide "text" | report | table
"""

from __future__ import annotations

# This file is also loaded by path (Hermes plugin loader, s1/, tests, `python core.py`): make its package importable.
import sys as _compat_sys
from pathlib import Path as _CompatPath
_compat_root = str(_CompatPath(__file__).resolve().parents[1])
if _compat_root not in _compat_sys.path:
    _compat_sys.path.insert(0, _compat_root)

from cortados_router.compat import getenv as _env_get

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from cortados_router.paths import s1_current, tables_dir

REPO = Path(_env_get("CORTADOS_REPO") or Path.home() / "cortados")   # the CortadOS checkout (fleet roster faces.json); not this repo
STATE = Path(_env_get("CORTADOS_HOME") or _env_get("CORTADOS_HERMES_HOME") or Path.home() / ".cortados")
TABLES = tables_dir()
TABLE_PATH = Path(_env_get("CORTADOS_ROUTING_TABLE") or TABLES / "routing-table.json")
BUDGETS_PATH = TABLES / "budgets.json"
S1_CURRENT = s1_current()   # promoted scorer record; its mtime keys execution_routing's score cache
# ollama's default local endpoint (the same value as CortadOS context.extractor.DEFAULT_URL). Nothing in the router
# calls it since decide_ollama was removed (2026-09-30); kept as a module attribute (removal is a separate change).
OLLAMA = _env_get("CORTADOS_ROUTER_OLLAMA", "http://127.0.0.1:11434")
ENGINE_MODEL = _env_get("CORTADOS_ROUTER_MODEL", "qwen3-8b-nothink-64k")   # Forest 2026-09-26 20:50: only the 8B may be resident on this Mac (memory); reverses the 09-19 14B ruling
# Shadow mode never blocks a reply on this. The deadline is long on purpose: on this single 32 GB Mac the resident 14B is one
# shared queue (memory extraction holds it for 1-2 min at a time; extra scorer models thrash memory, measured 2026-09-19), and a
# late decision is still a logged training row. Live routing will need the small trained scorer, not this engine.
DEADLINE_S = float(_env_get("CORTADOS_ROUTER_DEADLINE_S", "300"))

BAND_ORDER = ["R0", "R1", "R2", "R3", "R4"]
FEATURES = ["steps", "ambiguity", "blast_radius", "context_size", "novelty", "parallelizability"]
ANCHORS = {  # verbatim from the 2.0 routing-table-v1.json
    "steps": ["one action or lookup", "a short sequence with one dependency", "a multi-step chain with several dependencies", "a long dependent chain"],
    "ambiguity": ["directive fully determines the work", "minor interpretation with an evident objective", "substantial interpretation among plausible objectives", "intent must be substantially interpreted"],
    "blast_radius": ["words only, nothing changes", "a bounded reversible workspace change", "a broad or difficult-to-reverse system change", "law, money, production, or irreversible effects nearby"],
    "context_size": ["fits well inside the local 40,960-token window", "fits the local window with moderate headroom", "approaches or crosses the local window and benefits from decomposition", "exceeds a single frontier context; must be decomposed"],
    "novelty": ["matches patterns in memory/corpus", "a known pattern with a small variation", "a materially new pattern needing investigation", "nothing like it recorded"],
    "parallelizability": ["strictly serial", "mostly serial with one independent branch", "several independent subtasks can proceed concurrently", "mostly independent subtasks"],
}
WORK_KINDS = ["conversation", "capture/log", "code", "research", "writing", "planning", "ops"]
URGENCY = ["now", "today", "overnight"]
PARTS_TO_COUNT = [1, 2, 3, 5, 6]  # Score levels: 1 · 2 · 3 · 4–5 · 6+

OUTWARD_DEF = ('carrying out THIS request, in this turn, actually sends, spends, books, publishes, deletes, or changes something outside this machine (an email or message goes out, money moves, a booking is made, something is posted or deployed, a record elsewhere is changed). Drafting, reading, looking up, planning, summarising, or preparing something for Forest to send later is NOT outward. Mentioning email, money or a client is NOT outward. When in doubt: would anyone outside notice before Forest looks at the result? If not, false.')   # tightened 2026-09-22 (Forest): the old wording made 'draft a message' outward → R3 on 42% of turns

SCHEMA = {"type": "object", "additionalProperties": False, "required": FEATURES + ["outward", "work_kind", "urgency", "needs_clarification", "parts", "diversity"],
          "properties": {**{f: {"type": "integer", "minimum": 0, "maximum": 3} for f in FEATURES},
                         "outward": {"type": "boolean"}, "work_kind": {"type": "string", "enum": WORK_KINDS},
                         "urgency": {"type": "string", "enum": URGENCY}, "needs_clarification": {"type": "boolean"},
                         "parts": {"type": "integer", "minimum": 0, "maximum": 4}, "diversity": {"type": "boolean"}}}

SYSTEM = ("You are a routing scorer. You never answer the request. You rate it with typed answers only, as JSON matching the schema. "
          "Judge the REQUEST, not the conversation around it. Each question is independent.\n\nScore questions (integer level 0-3):\n"
          + "\n".join(f"- {f}: " + " | ".join(f"{i}={a}" for i, a in enumerate(ANCHORS[f])) for f in FEATURES)
          + "\n\nOther questions:\n- outward (true/false): " + OUTWARD_DEF
          + "\n- work_kind: one of " + ", ".join(WORK_KINDS) + "."
            "\n- urgency: now = the person is waiting for the answer; today = wanted today; overnight = explicitly fine to take hours."
            "\n- needs_clarification (true/false): the request cannot be done without a clarifying answer from Forest."
            "\n- parts (level 0-4): independent parts of work: 0=one, 1=two, 2=three, 3=four or five, 4=six or more."
            "\n- diversity (true/false): two independent attempts by different models would materially improve the result.")

MAX_REQUEST_CHARS = 2400


def clean_request(text: str) -> str | None:
    """The scorer judges the REQUEST, so strip what the gateway wraps around it. ONE definition for the live hook and for training.
    Discord turns arrive as [Replying to: "…"] [Triggering message id…] [Recent channel messages] … [New message]\n<the actual message>."""
    t = text or ""
    t = re.sub(r"\[cortados-image-route:[a-f0-9]{32}\]\s*", "", t)
    reply = re.match(r'\[Replying to: "([\s\S]{0,4000}?)"\]\s*', t)
    if "[New message]" in t:
        t = t.rsplit("[New message]", 1)[1]
    elif "[Recent channel messages]" in t:
        return None                                   # channel context whose [New message] part was cut off (an old truncated log row): nothing to judge
    elif reply:
        t = t[reply.end():]
    t = re.sub(r"\[Triggering message id:[^\]]*\]\s*", "", t)
    t = re.sub(r"\[Wangcai-on-Hermes runtime note[\s\S]*?\[End runtime note\]\s*", "", t)
    t = re.sub(r"^\[IMPORTANT: The user has invoked the \"[^\"]+\" skill[\s\S]*?\n\n(?=\S)", "", t.strip()) if t.strip().startswith("[IMPORTANT: The user has invoked") and "[End runtime note]" not in t else t
    t = re.sub(r"(?m)^-# (?:router \(shadow|answered by)[^\n]*$", "", t).strip()
    if not t:
        return None
    if len(t) > MAX_REQUEST_CHARS:                    # long pastes: head + tail keeps the ask and the size signal
        t = t[:1600] + f"\n[… {len(t) - 2200} characters omitted …]\n" + t[-600:]
    if reply and reply.group(1).strip() and not reply.group(1).startswith("(no reply"):
        t = f'(in reply to: "{" ".join(reply.group(1).split())[:240]}")\n' + t
    return t


# ------------------------------------------------------------------ mutable table, hot reload, last-good
_table_lock = threading.Lock()
_table: dict[str, Any] | None = None
_table_mtime = 0.0
_table_error: str | None = None
KNOWN_PROVIDERS = {"custom", "claude-cli", "codex-cli", "openai-codex", "agy-cli", "cursor-cli"}
GROK_RE = re.compile(r"^(auto|latest-grok|grok[-a-z0-9.]*|cursor-grok-[-a-z0-9.]+)$", re.I)


def _cursor_inventory() -> set[str]:
    try:
        return {l.split(" - ")[0].strip() for l in (TABLES / "inventory/cursor.txt").read_text().splitlines() if " - " in l}
    except Exception:
        return set()


def validate_table(t: dict[str, Any]) -> None:
    bands = [b["band"] for b in t["bands"]]
    if len(set(bands)) != len(bands):
        raise ValueError("duplicate band")
    for band, cands in t["candidates"].items():
        if band not in bands:
            raise ValueError(f"candidates for unknown band {band}")
        for c in cands:
            if c["provider"] not in KNOWN_PROVIDERS:
                raise ValueError(f"unknown provider {c['provider']!r} in {band}")
            if c["lane"] == "cursor" and not GROK_RE.match(c["model"]):
                if not (t.get("policy_cursor") or {}).get("allow_other_models") or c["model"] not in _cursor_inventory():
                    raise ValueError(f"cursor lane accepts Grok/auto, or (policy_cursor.allow_other_models) an id from tables/inventory/cursor.txt; got {c['model']!r}")
                if "fable" in c["model"].lower():
                    raise ValueError("Cursor's Fable ids are NO-ZDR: not routable")
            ceiling = c.get("tier") == "W-ceiling" or "fable" in c["model"].lower()
            if ceiling and band != "R4" and not (t.get("selection") or {}).get("ceiling_by_score"):
                raise ValueError(f"ceiling model {c['model']!r} outside R4 (G112: ceiling is escalation only)")
    for b in t["bands"]:
        if not (b["when"].get("never") or b["when"].get("all") or b["when"].get("any")):
            raise ValueError(f"band {b['band']} has no condition")


def table() -> dict[str, Any]:
    global _table, _table_mtime, _table_error
    with _table_lock:
        try:
            m = TABLE_PATH.stat().st_mtime
            if _table is None or m != _table_mtime:
                cand = json.loads(TABLE_PATH.read_text())
                validate_table(cand)
                _table, _table_mtime, _table_error = cand, m, None
        except Exception as exc:  # keep the last good table; surface the error in the log row
            _table_error = f"{type(exc).__name__}: {exc}"
            _table_mtime = TABLE_PATH.stat().st_mtime if TABLE_PATH.exists() else 0.0
            if _table is None:
                raise
        return _table


# ------------------------------------------------------------------ interim decision engine (typed JSON, local 14B)
S1_URL = _env_get("CORTADOS_S1_URL", "http://127.0.0.1:8431")
S1_TIMEOUT_S = float(_env_get("CORTADOS_S1_TIMEOUT_S", "15"))
FAIL_UP_BELOW = float(_env_get("CORTADOS_S1_FAIL_UP", "0.75"))   # Forest 2026-09-24: the right answer over cost   # a feature whose top probability is below this takes the HIGHER of its two most likely levels
OUTWARD_AT = float(_env_get("CORTADOS_S1_OUTWARD_AT", "0.25"))   # the destructive question is called at a lower probability (Jev: stricter gate for destructive actions)


def decide_s1(text: str, unit_kind: str = "interactive", *, task_context=None) -> dict[str, Any] | None:
    """The trained scorer, CortadOS Router S1 (s1/serve.py): ~1 s, typed answers with probabilities. None when the service is down."""
    t0 = time.time()
    from cortados_router.task_context import validate_context, context_digest
    digest = None
    try:
        payload = {"request": text[:6000], "unit_kind": unit_kind}
        task_context = validate_context(task_context)
        if task_context is not None:
            if task_context['status'] != 'bound':
                raise ValueError('task context unavailable')
            digest = context_digest(task_context)
            payload['task_context'] = task_context
        req = urllib.request.Request(f"{S1_URL}/score", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=S1_TIMEOUT_S) as r:
            d = json.loads(r.read())
    except Exception as exc:
        return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000), "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if d.get("error") or not d.get("answers"):
        return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000), "error": str(d.get("error"))[:200]}
    if digest is not None and d.get('task_context_digest') != digest:
        return {'answers': None, 'degraded': True, 'engine': 's1',
                'engine_ms': int((time.time()-t0)*1000),
                'error': 'scorer did not acknowledge the task context snapshot'}
    ans, probs = dict(d["answers"]), d.get("probs") or {}
    po = probs.get("outward") or []
    if len(po) == 2 and po[1] >= OUTWARD_AT:
        ans["outward"] = True
    for f in FEATURES:                                   # fail-up under uncertainty
        p = probs.get(f) or []
        if p and max(p) < FAIL_UP_BELOW:
            top2 = sorted(range(len(p)), key=lambda i: -p[i])[:2]
            ans[f] = max(top2)
    return {"answers": ans, "degraded": False, "engine": d.get("model") or "s1", "engine_ms": int((time.time() - t0) * 1000),
            "confidence": d.get("confidence"), "probs": probs,
            "task_context_digest": digest}


NO_LOCAL_FLAG = STATE / "no-local-fallback"   # touch it when memory is tight: the router then never pulls the 14B in for scoring


def decide(text: str, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    """The trained scorer only. Forest 2026-09-28: no local models in routing — when the scorer is down the decision is degraded and the face
    keeps its configured brain; nothing loads the 8B for scoring."""
    options = {'task_context': ctx['task_context']} if (ctx or {}).get('task_context') is not None else {}
    d = decide_s1(text, (ctx or {}).get("unit_kind") or "interactive", **options)
    if d and not d["degraded"]:
        return d
    return {**(d or {}), "answers": None, "degraded": True, "engine": "s1", "error": (d or {}).get("error") or "scorer down"}


# decide_ollama removed 2026-09-30: the router has not scored with ollama since 09-28 (Forest: no local models in routing); the 8B is the memory engine's.


# ------------------------------------------------------------------ band resolution (2.0 grammar: all | any | lte | gte | eq | never)
def _holds(cond: dict[str, Any], v: dict[str, int]) -> bool:
    if cond.get("never"):
        return False
    if "all" in cond:
        return all(_holds(c, v) for c in cond["all"])
    if "any" in cond:
        return any(_holds(c, v) for c in cond["any"])
    for op in ("lte", "gte", "eq"):
        if op in cond:
            name, k = cond[op]
            x = v[name]
            return x <= k if op == "lte" else x >= k if op == "gte" else x == k
    raise ValueError(f"bad condition {cond}")


_sc_mod = None


def _scarcity_module():
    """core.py is loaded by file path under a unique name (no package), so its sibling is loaded the same way."""
    global _sc_mod
    if _sc_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("cortados_router_scarcity", str(Path(__file__).with_name("scarcity.py")))
        _sc_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sc_mod)
    return _sc_mod


SCORES_PATH = TABLES / "model-scores.generated.json"
_scores_cache: tuple[float, dict[str, Any]] | None = None


def model_scores() -> dict[str, Any] | None:
    global _scores_cache
    try:
        m = SCORES_PATH.stat().st_mtime
        if not _scores_cache or _scores_cache[0] != m:
            _scores_cache = (m, json.loads(SCORES_PATH.read_text()))
        return _scores_cache[1]
    except Exception:
        return None


def required_index(t: dict[str, Any], ms: dict[str, Any], use_case: str, D: int | None, band: str) -> float:
    """Continuous requirement: interpolate over D through the knots; never below the reference of the band the hard rules chose."""
    refs = ms["reference_index"]
    by_score = bool((t.get("selection") or {}).get("ceiling_by_score"))
    best = max((m["index"].get(use_case, m["index"]["general"]) for m in ms["models"] if by_score or not m["ceiling"]), default=100.0)
    val = lambda ref: best if ref in ("best_non_ceiling", "best") else refs[ref].get(use_case, refs[ref]["general"])
    knots = [(k["D"], val(k["ref"])) for k in t["selection"]["knots"]]
    d = knots[0][0] if D is None else max(knots[0][0], min(knots[-1][0], D))
    req = knots[-1][1]
    for (d0, v0), (d1, v1) in zip(knots, knots[1:]):
        if d0 <= d <= d1:
            req = v0 + (v1 - v0) * (d - d0) / (d1 - d0)
            break
    if band == "R0":
        return 0.0                                        # reflex: anything qualifies, the cheapest route wins (normally local)
    floor = val(band) if band in refs and band != "R4" else 0.0
    cw = (t.get("selection") or {}).get("ceiling_work") or {}
    if by_score and use_case in (cw.get("use_cases") or []) and D is not None and D >= int(cw.get("min_D", 5)):
        floor = max(floor, val(cw.get("require", "R4")))  # insight work goes to the frontier (Forest 2026-09-25)
    return round(min(max(req, floor), best), 1)


def scored_candidates(t: dict[str, Any], band: str, use_case: str | None) -> list[dict[str, Any]] | None:
    """Every reachable model × every subscription that can run it, with its index for this use case. No band lists."""
    ms = model_scores()
    if (t.get("tiering") or {}).get("mode") != "scores" or not ms:
        return None
    u = use_case if use_case in ms.get("use_cases", {}) else "general"
    out = []
    for m in ms["models"]:
        if not (t.get("selection") or {}).get("ceiling_by_score") and m["ceiling"] != (band == "R4"):   # G112 mode: ceiling only by escalation
            continue
        for r in m["reach"]:
            if r["provider"] in KNOWN_PROVIDERS:
                out.append({"lane": r["lane"], "provider": r["provider"], "model": r["model"], "family": m["family"], "index": m["index"].get(u, m["index"]["general"]),
                            "general": m["index"]["general"], "price": m.get("price_1m_blended_usd"), "tokens_per_s": m.get("tokens_per_s"), "ceiling": m["ceiling"], "use_case": u, "tier": "scored", **({"pool": r["pool"]} if r.get("pool") else {}), **({"proxy": True} if m.get("proxy") else {})})
    return out or None


NATIVE_LANE = (("claude", "claude"), ("gpt-oss", None), ("gpt", "codex"), ("o1", "codex"), ("o3", "codex"), ("o4", "codex"), ("gemini", "antigravity"))


def native_lane_for(model_or_family: str) -> str | None:
    """Forest 2026-09-23: a vendor's models come from the vendor's own subscription first: Claude → Claude Code, GPT → Codex, Gemini → Antigravity.
    Cursor (and Antigravity's Claude/GPT pool) are SECONDARY sources for those, used only when the native pool has run out; Cursor stays primary
    for what has no native lane here (Grok, Composer, Kimi, GLM, Muse …)."""
    m = (model_or_family or "").lower().replace("cursor-", "")
    for prefix, lane in NATIVE_LANE:
        if m.startswith(prefix):
            return lane
    return None


def _pool_tokens_per_pct(c: dict[str, Any]) -> float | None:
    """Learned size of the pool this route draws on (tokens per 1 % of its window), from unit-cost.json; None if unknown."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        key = {("claude", "all"): "claude:all", ("claude", "fable"): "claude:fable", ("codex", "all"): "codex:all", ("antigravity", "gemini"): "antigravity:gemini",
               ("antigravity", "claude-gpt"): "antigravity:claude-gpt", ("cursor", "cursor-models"): "cursor:cursor-models", ("cursor", "other-models"): "cursor:other-models"}.get((c["lane"], pool))
        uc = (sc.unit_costs() or {}).get(key or "") or {}
        return float(uc["tokens_per_pct"]) if uc.get("tokens_per_pct") else None
    except Exception:
        return None


def _tokens_left_for(c: dict[str, Any]) -> float | None:
    """Absolute room in the pool this route draws on, from the learned unit cost (None when unknown)."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        key = {("claude", "all"): "claude:all", ("claude", "fable"): "claude:fable", ("codex", "all"): "codex:all", ("antigravity", "gemini"): "antigravity:gemini",
               ("antigravity", "claude-gpt"): "antigravity:claude-gpt", ("cursor", "cursor-models"): "cursor:cursor-models", ("cursor", "other-models"): "cursor:other-models"}.get((c["lane"], pool))
        uc = (sc.unit_costs() or {}).get(key or "") or {}
        return float(uc["remaining_tokens"]) if uc.get("remaining_tokens") else None
    except Exception:
        return None


def _policy() -> dict[str, Any]:
    try:
        return json.loads(BUDGETS_PATH.read_text()).get("policy") or {}
    except Exception:
        return {}


def lane_health() -> dict[str, dict[str, Any]]:
    """Health = measured subscription scarcity (scarcity.py: % used vs % of the window elapsed, per vendor usage screen).
    ok=False only when a window is spent or the vendor is refusing; everything softer is a ranking signal, not a gate.
    `pools` holds the separately metered pools of a lane (Claude Fable week, Cursor models vs other models, …): see candidate_health."""
    sc = _scarcity_module()
    out: dict[str, dict[str, Any]] = {}
    for lane, v in sc.lane_scarcity().items():
        out[lane] = {**_health_row(v.get("scarcity"), v.get("tightest"), v.get("expiring_pct"), v.get("refusing")),
                     "pools": {p: _health_row(pv.get("scarcity"), pv.get("tightest"), pv.get("expiring_pct"), pv.get("spent")) for p, pv in (v.get("pools") or {}).items()}}
    return out


def _health_row(s, w, expiring, spent) -> dict[str, Any]:
    w = w or {}
    spent = bool(spent) or (s is not None and s >= 1.0)
    why = (f"{w.get('window', 'window')} {w.get('used_pct')}% used, resets {w.get('resets_at')}" if w else "refusing") if spent else None
    return {"ok": not spent, "why": why, "scarcity": s, "tightest": w, "expiring_pct": expiring or 0}


def candidate_health(c: dict[str, Any], health: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A model is as scarce as the tightest pool it draws on: the lane's general windows plus its own pool when it has one."""
    lane = health.get(c["lane"]) or {}
    pools = lane.get("pools") or {}
    pool = c.get("pool") or (_scarcity_module().pool_for(c["lane"], c.get("model", ""), list(pools)) if pools else None)
    return {**(pools.get(pool) or lane), "pool": pool if pool in pools else None} if lane else {"ok": True, "scarcity": None, "expiring_pct": 0, "tightest": {}, "pool": None}


def _context_cfg() -> dict[str, Any]:
    try:
        return json.loads(BUDGETS_PATH.read_text()).get("context") or {}
    except Exception:
        return {}


def turn_tokens(history_chars: int) -> int:
    """Measured harness + this session's history (chars/4), with headroom."""
    cx = _context_cfg()
    return int((int(cx.get("harness_tokens", 31000)) + history_chars // 4) * float(cx.get("headroom", 1.2)))


def lane_ceiling(lane: str, model: str = "", transport: str | None = None) -> int:
    # A transport-keyed ceiling applies only to the transport that measured it (the Donna
    # model gateway's calibrated fit); unknown or absent transports keep the lane ceiling.
    by_transport = (_context_cfg().get("ceiling_tokens_by_transport") or {}).get(lane) or {}
    if transport is not None and transport in by_transport:
        return int(by_transport[transport])
    cx = _context_cfg().get("ceiling_tokens") or {}
    if lane == "claude" and "[1m]" in (model or ""):
        return int(cx.get("claude[1m]", 1000000))
    return int(cx.get(lane, 200000))


def resolve(decision: dict[str, Any], unit_kind: str = "interactive", history_chars: int = 0, sticky: dict[str, Any] | None = None,
            escalate: dict[str, Any] | None = None, input_fit: dict[str, Any] | None = None,
            allowed_routes: tuple[tuple[str, str], ...] | None = None,
            execution: bool = False, minimum_band: str | None = None) -> dict[str, Any]:
    t = table()
    rules: list[str] = []
    if decision["degraded"] or not decision["answers"]:
        band = t["fallback"]["interactive" if unit_kind == "interactive" else "background"]
        v, D, ans = None, None, {}
        rules.append("engine degraded → fallback band")
    else:
        ans = dict(decision["answers"])
        v = {f: int(ans[f]) for f in FEATURES}
        if ans.get("outward") and v["blast_radius"] < 3:
            v["blast_radius"] = 3
            rules.append("outward effect → blast_radius floored at 3 (never lowered)")
        D = sum(v[k] for k in t["difficulty"]["terms"])
        # escalation (Forest 2026-09-29: a small-sounding change can turn into a large refactor once the code is mapped; the scorer only sees
        # the words). A session that found its task bigger — the face said so via router_escalate, or its trajectory (tool calls / files touched)
        # crossed the budgets.json thresholds — has its difficulty floored for the rest of the session and every sub-agent it launches.
        if escalate and int(escalate.get("D_floor") or 0) > D:
            D = int(escalate["D_floor"])
            rules.append(f"escalated ({escalate.get('source', 'face')}): {str(escalate.get('reason') or '')[:80]} → D floored at {D}")
        v["D"] = D
        v["parts"] = PARTS_TO_COUNT[int(ans.get("parts", 0))]
        band = next((b["band"] for b in t["bands"] if _holds(b["when"], v)), t["fallback"]["interactive"])
    if minimum_band is not None:
        if minimum_band not in BAND_ORDER:
            raise ValueError('Invalid minimum routing band')
        if BAND_ORDER.index(band) < BAND_ORDER.index(minimum_band):
            band = minimum_band
            rules.append(f'trusted capability requirement floors band at {band}')
    row = next(b for b in t["bands"] if b["band"] == band)
    # candidates: table order, work-kind preference first; then subscription scarcity re-ranks them (never below the band):
    #   spent / refusing lane → skipped · lane burning faster than its window → behind the others · lane whose headroom expires soon → first
    health, pol = lane_health(), _policy()
    demote, expiring = float(pol.get("demote_above", 0.5)), float(pol.get("prefer_expiring_headroom_pct", 10))
    pref = (t.get("work_kind_preference") or {}).get(ans.get("work_kind") or "", [])
    scored = scored_candidates(t, band, ans.get("work_kind"))
    cands = scored if scored else list(t["candidates"].get(band, []))
    # Explicit degraded-service policy: do not spend Claude by default when
    # the scorer cannot classify a request. Retain quota and context-fit gates.
    fallback_routes = (t.get("fallback", {}).get("routes")
                       if not execution and (decision.get("degraded") or not decision.get("answers")) else None)
    if fallback_routes:
        cands = [dict(c) for c in fallback_routes]
        scored = []
        pref = []
        rules.append("scorer unavailable → configured GPT Terra fallback")
    if execution:
        # An execution always passes the same capacity/quality/fit policy, even
        # when scoring failed. A fixed profile default is never a rescue route.
        if decision.get('degraded'):
            rules.append('scorer unavailable → dynamic selection in the conservative fallback band')
        # Codex CLI is the same vendor pool, with its own transport envelope.
        # Prefer native when it fits; CLI remains eligible for larger requests.
        cands = [dict(c) for c in cands]
        cands += [dict(c, provider='codex-cli') for c in cands
                  if c['provider'] == 'openai-codex'
                  and not any(k['provider'] == 'codex-cli' and k['model'] == c['model'] for k in cands)]
        if scored:
            scored = cands
    if allowed_routes is not None:
        if not isinstance(allowed_routes, tuple) or any(
                not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(v, str) or not v for v in pair) for pair in allowed_routes):
            raise ValueError('Explicit provider/model route pairs required')
        cands = [c for c in cands if (c['provider'], c['model']) in allowed_routes]
        if scored:
            scored = cands
        rules.append('deployment provider/model allowlist applied before selection')
    req = None
    if scored:
        pref = []
        req = required_index(t, model_scores(), scored[0]["use_case"], D, band)
        rules.append(f"needs index ≥ {req} for {scored[0]['use_case']} (D={D}, {band}); {sum(c['index'] >= req for c in scored)} of {len(scored)} reachable model routes qualify")
    ch_of = {id(c): candidate_health(c, health) for c in cands}
    # Fit is a hard eligibility gate, before quality fallback or scarcity ranks
    # candidates, including unscored/degraded selection. Never rescue a route
    # which would lose input just because every candidate failed the gate.
    fit = input_fit or {}
    need = (int(fit['input_tokens']) if execution and fit.get('assembled') is True
            else max(turn_tokens(history_chars), int(fit.get('input_tokens') or 0)))
    cx = _context_cfg()
    byte_caps = {'antigravity': 180000, **(cx.get('ceiling_bytes') or {})}
    transport_rejected = set()
    for c in cands:
        cap = lane_ceiling(c['lane'], c['model'], fit.get('transport'))
        if execution and c['provider'] == 'codex-cli':
            cap = int(cx.get('provider_ceiling_tokens', {}).get('codex-cli', 200000))
        why = None
        if need > cap:
            why = f"turn ≈{need // 1000}k tokens exceeds {c['lane']} ceiling {cap // 1000}k"
        elif fit.get('known') is False and c['lane'] in ('antigravity', 'local'):
            why = 'exact session history unavailable; constrained transport not admitted'
        elif c['lane'] in byte_caps and int(fit.get('input_bytes') or 0) > int(byte_caps[c['lane']]):
            why = f"input estimate {fit['input_bytes']} bytes exceeds {c['lane']} byte ceiling {byte_caps[c['lane']]}"
        elif c['lane'] == 'local' and history_chars // 4 > int(cx.get('local_fresh_only_history_tokens', 8000)):
            why = 'local lanes take fresh-context work only'
        if why:
            transport_rejected.add(id(c))
            ch_of[id(c)] = {**ch_of[id(c)], 'ok': False, 'why': why}
    if transport_rejected:
        rules.append(f'turn ≈{need // 1000}k tokens: routes that cannot carry the intact input excluded')
    sc_of = lambda c: ch_of[id(c)].get("scarcity") or 0.0
    exp_of = lambda c: ch_of[id(c)].get("expiring_pct") or 0
    ok_of = lambda c: ch_of[id(c)].get("ok", True)
    base = {id(c): i for i, c in enumerate(cands)}
    if scored:
        sel = t["selection"]
        tie = sel.get("lane_tiebreak", [])
        best_avail = max((c["index"] for c in cands if ok_of(c)), default=0)
        target = req if any(ok_of(c) and c["index"] >= req for c in cands) else best_avail       # nothing qualifies → strongest available
        for c in cands:
            c["surplus"] = round(c["index"] - target, 1)
            # qualify on the USE-CASE index; pay by the GENERAL index (the heavier the model, the slower and the scarcer its subscription)
            sc = sc_of(c)
            # weight of a route = what it burns: real $/1M (Artificial Analysis, blended 3:1) scaled so the priciest non-ceiling reachable model = 100;
            # the general index stands in only where no price is published; local = 0
            # BURN, not price (Forest 2026-09-29, after the Claude pool ran out while Codex had barely moved): what a call costs is the share of ITS POOL's window it
            # consumes. Pools differ ~10× in size (Claude ≈ 53 M of our tokens per week, Codex ≈ 563 M), so weight = model price ÷ pool size,
            # normalised so the heaviest candidate = 100. Price alone (the old weight) is used only for pools whose size is not yet learned.
            c["burn"] = (c["price"] / (_pool_tokens_per_pct(c) / 1e6)) if c.get("price") and _pool_tokens_per_pct(c) else None
        burn_ref = max([x["burn"] for x in cands if x.get("burn")] + [0.0])
        price_ref = max([x["price"] for x in cands if x.get("price") and not x.get("ceiling")] + [1.0])
        for c in cands:
            if c.get("burn") is not None and burn_ref:
                c["weight"] = round(100.0 * c["burn"] / burn_ref, 1)
            else:
                c["weight"] = round(100.0 * c["price"] / price_ref, 1) if c.get("price") else (0.0 if c["lane"] == "local" else c["general"])
        for c in cands:
            sc = sc_of(c)
            c["cost"] = round(c["weight"] + sel.get("scarcity_points", 30) * max(sc, 0.0) - sel.get("expiring_credit_points", 80) * max(-sc, 0.0), 1)
            # capacity (Forest 2026-09-26): plans are not normalised; among pools with room, the one with the most TOKENS left gets a further credit
            tl = _tokens_left_for(c)
            if tl is not None and sc <= 0:
                c["tokens_left"] = tl
        most = max((c.get("tokens_left") or 0) for c in cands) or 0
        for c in cands:
            if c.get("tokens_left") and most:
                c["cost"] = round(c["cost"] - float(sel.get("capacity_credit_points", 20)) * c["tokens_left"] / most, 1)
        # one quota pool, models of the same weight class (general index within 20 points): the better one costs the same, so keep only it
        # context policy: the route must fit the measured turn; local lanes only for fresh context (Hermes Agents Context, 2026-09-24)
        # face turns never drop below a Sonnet-class model, whatever the band (selection.interactive_min_general_index)
        floor_g = float(sel.get("interactive_min_general_index", 0)) if unit_kind == "interactive" else 0.0
        if floor_g:
            weak = [c for c in cands if c["general"] < floor_g and c["lane"] != "local"]
            if len(weak) < len(cands):
                for c in weak:
                    ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"below the face floor (general {c['general']} < {floor_g:g})"}
        # primary-source rule: a Claude/GPT/Gemini route on another provider is secondary, usable only when the vendor's own pool is spent
        for c in cands:
            native = native_lane_for(c.get("family") or c["model"])
            c["secondary"] = bool(native) and c["lane"] != native
            if c["secondary"]:
                nat = health.get(native) or {}
                nat_pool = candidate_health({"lane": native, "model": c.get("family") or c["model"]}, health) if nat else {}
                if nat and nat_pool.get("ok", True):
                    ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"secondary source; {native} still has {c.get('family')}"}
        keep = []
        for c in sorted(cands, key=lambda c: -c["index"]):          # same lane+pool and same price class (±25 % when priced) → keep the strongest only
            same = lambda k: (abs(k["price"] - c["price"]) <= 0.25 * max(k["price"], c["price"]) if k.get("price") and c.get("price")
                              else k["general"] - c["general"] <= sel.get("same_price_band", 20))
            if not any(k["lane"] == c["lane"] and k.get("pool") == c.get("pool") and same(k)
                       and (ok_of(k) or not ok_of(c)) for k in keep):
                keep.append(c)
        cands = keep
        cands.sort(key=lambda c: (0 if c["index"] >= target else 1, c["cost"], -(c.get("tokens_per_s") or 0), tie.index(c["lane"]) if c["lane"] in tie else len(tie), base[id(c)]))
        # stickiness: keep the session's current model unless the band rose, the requirement outgrew it, or its pool is spent
        if sticky and sticky.get("model"):
            cur = next((c for c in cands if c["model"] == sticky["model"] and c["lane"] == sticky.get("lane")), None)
            band_rose = BAND_ORDER.index(band) > BAND_ORDER.index(sticky.get("band", band)) if band in BAND_ORDER else False
            best_cost = min((c["cost"] for c in cands if ok_of(c) and c["index"] >= target), default=None)
            near = best_cost is not None and cur is not None and cur["cost"] <= best_cost + float(sel.get("sticky_slack_points", 15))
            # inside one big pool the burn weights sit within ~3 points of each other (Codex: Luna 0.1 … Astra 2.7), so "near-tie on cost" alone
            # kept a session on Astra for every trivial follow-up (Forest 2026-09-29). Stay only if the current model is also in the best route's
            # price class (≤ 2× its $/1M); otherwise the session steps back down to the cheapest qualifying model.
            best = next((c for c in cands if ok_of(c) and c["index"] >= target), None)
            same_class = not (best and best.get("price") and cur and cur.get("price")) or cur["price"] <= 2.0 * best["price"]
            # stay only when it is a near-tie on cost (Forest 2026-09-24: scarcity and price move sessions; a browser/desktop pin is the one exception)
            if cur and ok_of(cur) and cur["index"] >= target and not band_rose and (sticky.get("pinned") or (near and same_class)):
                cands.remove(cur); cands.insert(0, cur)
                rules.append(f"sticky: session stays on {cur['lane']}/{cur['model']}" + (" (browser/desktop session pinned)" if sticky.get("pinned") else " (near-tie on cost)"))
            elif cur:
                rules.append(f"session moves off {cur['lane']}/{cur['model']}: " + ("pool spent" if not ok_of(cur) else "band rose" if band_rose else "outgrown" if cur["index"] < target else f"cost {cur['cost']} vs best {best_cost}"))
        cu = sel.get("ceiling_underuse") or {}
        if D is not None and D >= int(cu.get("min_D", 6)):
            spare = [c for c in cands if c.get("ceiling") and ok_of(c) and sc_of(c) <= float(cu.get("attract_when_scarcity_below", -0.3))]
            if spare:
                top = min(spare, key=lambda c: c["cost"])
                cands.remove(top); cands.insert(0, top)
                rules.append(f"{top['lane']}/{top['model']} takes it: its pool is underused (scarcity {sc_of(top)}), paid-for frontier capacity is used rather than lost")
        if target != req:
            rules.append(f"no available model reaches {req}: strongest available ({best_avail}) taken")
    else:
        cands.sort(key=lambda c: (0 if exp_of(c) >= expiring and sc_of(c) < demote else 1, 1 if sc_of(c) >= demote else 0,
                                  pref.index(c["lane"]) if c["lane"] in pref else len(pref), base[id(c)]))
    usable = [c for c in cands if ok_of(c)]
    skipped = [{"lane": c["lane"], "model": c["model"], "pool": ch_of[id(c)].get("pool"), "why": ch_of[id(c)].get("why")} for c in cands if not ok_of(c) and not (ch_of[id(c)].get("why") or "").startswith("secondary")][:8]
    fitting = [c for c in cands if id(c) not in transport_rejected]
    chosen = usable[0] if usable else (fitting[0] if fitting and not fallback_routes and not execution else None)
    if not usable and fitting:
        rules.append("no available candidate; execution refused" if execution else
                     "every candidate of this band is spent → first candidate shown; live mode would fail UP one band")
    if cands and not fitting:
        rules.append('no router candidate fits input estimate; host assembly/preflight required')
    if scored and chosen:
        rules.append(f"chosen {chosen['lane']}/{chosen['model']}: {chosen['use_case']} index {chosen['index']} clears it; cheapest qualifying route (${chosen.get('price') if chosen.get('price') is not None else '?'}/1M → weight {chosen['weight']}, pool scarcity {sc_of(chosen)}, cost {chosen['cost']})")
    for c in ([] if scored else usable):
        if sc_of(c) >= demote and c is not chosen:
            w = ch_of[id(c)].get("tightest") or {}
            rules.append(f"{c['lane']}/{c['model']} demoted: {w.get('window')} {w.get('used_pct')}% used at {w.get('elapsed_pct')}% of its window (on pace for {w.get('projected_pct_at_reset')}%)")
    if not scored and chosen and exp_of(chosen) >= expiring and base[id(chosen)] != 0:
        rules.append(f"{chosen['lane']}/{chosen['model']} preferred: about {exp_of(chosen)}% of its window expires unused at {(ch_of[id(chosen)].get('tightest') or {}).get('resets_at')}")
    chosen_scarcity = sc_of(chosen) if chosen else 0.0
    if chosen and chosen_scarcity >= float(pol.get("reserve_heavy_tiers_when_scarcity_above", 0.7)):
        if unit_kind != "interactive":
            rules.append(f"background unit on a scarce pool ({chosen['lane']}/{chosen['model']} {chosen_scarcity}) → live mode would defer it past the reset or run it local overnight")
        else:
            rules.append(f"{chosen['lane']}/{chosen['model']} is scarce ({chosen_scarcity}) → sub-agents held to the band minimum")
    # sub-agents: band row gives the range; R2 decomposes only when its decompose_when holds
    sp = row.get("subagents") or {"min": 0, "max": 0}
    n = 0
    if v is not None and sp.get("max", 0) > 0:
        if "decompose_when" not in sp or _holds(sp["decompose_when"], v):
            n = max(sp.get("min", 0), min(sp["max"], max(2, v["parts"])))
    n = min(n, (t.get("subagent_policy") or {}).get("max_concurrent_workers", 3) + (1 if sp.get("verifier") else 0)) if n else 0
    if n and chosen_scarcity >= float(pol.get("reserve_heavy_tiers_when_scarcity_above", 0.7)):
        n = sp.get("min", 0)
    if ans.get("needs_clarification"):
        rules.append("needs clarification → live mode would park and ask Forest before spending a lane")
    return {"band": band, "label": row.get("label"), "D": D, "features": {f: v[f] for f in FEATURES} if v else None,
            "orchestrator": row.get("orchestrator"), "chosen": chosen, "runner_up": usable[1] if len(usable) > 1 else None,
            "skipped": skipped, "subagents": n, "verifier": bool(sp.get("verifier")) and n > 0, "hard_rules": rules,
            "table_version": t.get("version"), "table_error": _table_error, "answers": ans or None,
            "scarcity": {**{lane: h.get("scarcity") for lane, h in health.items()},
                         **{f"{lane}:{p}": pv.get("scarcity") for lane, h in health.items() for p, pv in (h.get("pools") or {}).items()}},
            "required_index": req, "turn_tokens": need, "input_fit": input_fit, "fit_blocked": bool(cands) and not fitting,
            "alternatives": [{k: c.get(k) for k in ("lane", "model", "index", "cost")} for c in usable[1:6]] if scored else None,
            "chosen_window": (ch_of[id(chosen)].get("tightest") if chosen else None), "chosen_pool": (ch_of[id(chosen)].get("pool") if chosen else None)}


# ------------------------------------------------------------------ log
_db_lock = threading.Lock()
DDL = """CREATE TABLE IF NOT EXISTS routes (
  turn_id TEXT PRIMARY KEY, ts REAL, session_id TEXT, platform TEXT, unit_kind TEXT, actual_model TEXT,
  request TEXT, request_chars INTEGER, history_msgs INTEGER,
  engine TEXT, engine_ms INTEGER, degraded INTEGER, engine_error TEXT, answers TEXT, features TEXT, D INTEGER,
  band TEXT, orchestrator TEXT, lane TEXT, provider TEXT, model TEXT, runner_up TEXT, skipped TEXT,
  subagents INTEGER, verifier INTEGER, hard_rules TEXT, table_version INTEGER, table_error TEXT,
  response_chars INTEGER, turn_ms INTEGER, forest_correction TEXT);"""


def _db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE / "router.db", timeout=10)
    c.executescript(DDL)
    cols = [r[1] for r in c.execute("PRAGMA table_info(routes)")]
    if "scarcity" not in cols:
        c.execute("ALTER TABLE routes ADD COLUMN scarcity TEXT")
    if "context" not in cols:   # Forest 2026-09-30: the scorer must judge difficulty WITH what memory knows; the recall slice is logged with every
        c.execute("ALTER TABLE routes ADD COLUMN context TEXT")   # request from day one so labels accumulate with context before the retrain
    if 'task_context' not in cols:
        c.execute('ALTER TABLE routes ADD COLUMN task_context TEXT')
    try:
        os.chmod(STATE / "router.db", 0o600)
    except OSError:
        pass
    return c


def log_route(turn_id: str, meta: dict[str, Any], decision: dict[str, Any], r: dict[str, Any]) -> None:
    ch = r.get("chosen") or {}
    with _db_lock:
        c = _db()
        try:
            c.execute("INSERT OR REPLACE INTO routes (turn_id, ts, session_id, platform, unit_kind, actual_model, request, request_chars, history_msgs, "
                      "engine, engine_ms, degraded, engine_error, answers, features, D, band, orchestrator, lane, provider, model, runner_up, skipped, "
                      "subagents, verifier, hard_rules, table_version, table_error, scarcity, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (turn_id, time.time(), meta.get("session_id"), meta.get("platform"), meta.get("unit_kind"), meta.get("actual_model"),
                       meta.get("request"), len(meta.get("request") or ""), meta.get("history_msgs"), decision["engine"], decision["engine_ms"],
                       int(decision["degraded"]), decision.get("error"), json.dumps(r.get("answers")), json.dumps(r.get("features")), r.get("D"),
                       r["band"], r.get("orchestrator"), ch.get("lane"), ch.get("provider"), ch.get("model"), json.dumps(r.get("runner_up")),
                       json.dumps(r.get("skipped")), r["subagents"], int(r["verifier"]), json.dumps(r["hard_rules"]), r.get("table_version"), r.get("table_error"), json.dumps(r.get("scarcity")), meta.get("context")))
            c.execute('UPDATE routes SET task_context=? WHERE turn_id=?',
                      (json.dumps(meta['task_context'], ensure_ascii=False) if meta.get('task_context') is not None else None, turn_id))
            c.commit()
        finally:
            c.close()


def log_execution(route_id: str, provider: str, model: str) -> None:
    """Only a completed provider response is evidence of actual execution."""
    with _db_lock:
        c = _db()
        try:
            c.execute("UPDATE routes SET actual_model=? WHERE turn_id=?",
                      (f"{provider}/{model}", route_id))
            c.commit()
        finally:
            c.close()


def log_outcome(turn_id: str, response_chars: int, turn_ms: int) -> None:
    with _db_lock:
        c = _db()
        try:
            c.execute("UPDATE routes SET response_chars=?, turn_ms=? WHERE turn_id=?", (response_chars, turn_ms, turn_id))
            c.commit()
        finally:
            c.close()


def log_call_context(turn_id, api_request_id, *, session_id, model, provider,
                     task_context, scored_digest, current_digest, status):
    """An immutable pre-request observation, never proof of provider execution.

    Keep the task used at routing separate from later task state. No request
    messages, tool results or model reasoning are stored by this observer.
    """
    from cortados_router.task_context import validate_context,context_digest
    snapshot=validate_context(task_context)
    if not turn_id or not api_request_id or status not in ('unchanged','changed','score_pending'):
        raise ValueError('Invalid call-context observation')
    if current_digest!=context_digest(snapshot):
        raise ValueError('Call-context digest differs from snapshot')
    payload=json.dumps({'session_id':session_id,'model':model,'provider':provider,
        'task_context':snapshot,'scored_digest':scored_digest,'current_digest':current_digest,
        'status':status,'authority':'pre-request observation; not provider execution or model rerouting'},
        ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if not _db_lock.acquire(timeout=.025):
        raise TimeoutError('Call-context writer busy')
    c=None
    try:
        STATE.mkdir(parents=True,exist_ok=True)
        c=sqlite3.connect(STATE/'router.db',timeout=.05)
        try:
            c.execute('CREATE TABLE IF NOT EXISTS route_call_contexts('
                'turn_id TEXT NOT NULL,api_request_id TEXT NOT NULL,payload TEXT NOT NULL,'
                'PRIMARY KEY(turn_id,api_request_id))')
            for operation in ('UPDATE','DELETE'):
                c.execute(f'CREATE TRIGGER IF NOT EXISTS route_call_contexts_no_{operation.lower()} '
                    f'BEFORE {operation} ON route_call_contexts BEGIN '
                    "SELECT RAISE(ABORT,'immutable call-context observation'); END")
            prior=c.execute('SELECT payload FROM route_call_contexts WHERE turn_id=? AND api_request_id=?',
                (turn_id,api_request_id)).fetchone()
            if prior is not None and prior[0]!=payload:
                raise ValueError('Call-context identity reused with different evidence')
            c.execute('INSERT OR IGNORE INTO route_call_contexts VALUES(?,?,?)',(turn_id,api_request_id,payload))
            c.commit()
            try:os.chmod(STATE/'router.db',0o600)
            except OSError:pass
        finally:c.close()
    finally:_db_lock.release()


def footer(r: dict[str, Any], decision: dict[str, Any]) -> str:
    ch = r.get("chosen") or {}
    bits = [f"{r['band']} {r.get('label') or ''}".strip(), f"{ch.get('lane', '?')} · {ch.get('model', '?')}"
            + (f" (index {ch['index']:g}, needs {r['required_index']:g} for {ch.get('use_case')})" if ch.get("index") is not None and r.get("required_index") is not None else "")]
    bits.append(f"{r['subagents']} sub-agents" + (" + verifier" if r["verifier"] else "") if r["subagents"] else "no sub-agents")
    if r.get("D") is not None:
        bits.append(f"D={r['D']}")
    w = r.get("chosen_window") or {}
    if w.get("used_pct") is not None:
        bits.append(f"{w.get('window')}: {w['used_pct']:g}% used / {w['elapsed_pct']:g}% elapsed")
    if r.get("skipped"):
        bits.append("skipped " + ", ".join(f"{s['lane']}/{s['model']} ({s['why']})" if s.get("why") else s["lane"] for s in r["skipped"][:2]) + (f" +{len(r['skipped']) - 2} more" if len(r["skipped"]) > 2 else ""))
    if decision["degraded"]:
        bits.append("scorer degraded")
    return "-# router (shadow, nothing changed): " + " | ".join(bits)


def recent(n: int = 8) -> list[dict[str, Any]]:
    with _db_lock:
        c = _db()
        c.row_factory = sqlite3.Row
        try:
            return [dict(x) for x in c.execute("SELECT ts, platform, actual_model, band, lane, model, subagents, D, engine_ms, degraded, substr(request,1,70) AS request "
                                               "FROM routes ORDER BY ts DESC LIMIT ?", (n,))]
        finally:
            c.close()


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "decide":
        d = decide(" ".join(sys.argv[2:])); r = resolve(d)
        print(json.dumps({"decision": d, "route": r}, indent=1)); print(footer(r, d))
    elif cmd == "table":
        t = table(); print("table v", t.get("version"), "ok" if not _table_error else _table_error)
    else:
        for x in recent(20):
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(x['ts']))} {x['platform'] or '':8s} {x['band']:3s} {str(x['lane']):11s} {str(x['model'])[:26]:26s} sub={x['subagents']} D={x['D']} {x['engine_ms']}ms {'DEGRADED ' if x['degraded'] else ''}| {x['request']}")
