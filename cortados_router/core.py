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
import urllib.error
import http.client
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
from cortados_router.s1_endpoint import resolve_url as _resolve_s1_url
S1_URL = _resolve_s1_url()   # explicit CORTADOS_S1_URL, else a named device from the endpoints file, else loopback
S1_TIMEOUT_S = float(_env_get("CORTADOS_S1_TIMEOUT_S", "15"))
S1_RETRIES = int(_env_get("CORTADOS_S1_RETRIES", "1"))   # one retry on a transient connection error before degrading (Forest 2026-10-26)
S1_RETRY_BACKOFF_S = float(_env_get("CORTADOS_S1_RETRY_BACKOFF_S", "0.3"))
_S1_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})   # only these HTTP statuses are worth a retry; a 4xx (auth, body too large) is permanent
S1_TOKEN = _env_get("CORTADOS_S1_TOKEN")   # sent as a bearer when the chosen scorer requires one (a non-loopback bind)
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
        headers = {"Content-Type": "application/json", "X-Request-Timeout-S": str(S1_TIMEOUT_S)}
        if S1_TOKEN:
            headers["Authorization"] = f"Bearer {S1_TOKEN}"
        req = urllib.request.Request(f"{S1_URL}/score", data=json.dumps(payload).encode(),
                                     headers=headers, method="POST")
    except Exception as exc:                             # building the request failed: nothing to retry
        return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000), "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    # One quick retry on a transient connection error: a restarting or momentarily overloaded S1 closes the connection
    # (Connection refused / RemoteDisconnected), which a single retry rides out before we fall back to degraded (Forest 2026-10-26).
    _transient = (urllib.error.URLError, http.client.HTTPException, OSError)
    d = None
    for _attempt in range(S1_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=S1_TIMEOUT_S) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as exc:             # an HTTP status reply: 4xx (auth, body too large) is permanent — only 429/5xx are worth a retry
            if exc.code in _S1_RETRYABLE_HTTP and _attempt < S1_RETRIES:
                time.sleep(S1_RETRY_BACKOFF_S)
                continue
            return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000), "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        except _transient as exc:                         # a connection-level drop (refused, RemoteDisconnected, timeout): retry once
            if _attempt < S1_RETRIES:
                time.sleep(S1_RETRY_BACKOFF_S)
                continue
            return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000), "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        except Exception as exc:                          # a non-transient error (e.g. a malformed body): do not retry
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


def scored_candidates(t: dict[str, Any], band: str, use_case: str | None, accounts_aware: bool = False) -> list[dict[str, Any]] | None:
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
            if r["provider"] not in KNOWN_PROVIDERS:
                continue
            out.append({"lane": r["lane"], "provider": r["provider"], "model": r["model"], "family": m["family"], "index": m["index"].get(u, m["index"]["general"]),
                        "general": m["index"]["general"], "price": m.get("price_1m_blended_usd"), "tokens_per_s": m.get("tokens_per_s"), "ceiling": m["ceiling"], "use_case": u, "tier": "scored", **({"pool": r["pool"]} if r.get("pool") else {}), **({"proxy": True} if m.get("proxy") else {})})
    # One candidate per configured Claude account (Option A, lane-per-account); single default login leaves this unchanged.
    return _expand_claude_accounts(out, accounts_aware) or None


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


def _base_lane(lane: str) -> str:
    """The vendor lane a per-account lane belongs to: `claude#b` -> `claude`. A bare lane is its own base."""
    return (lane or "").split("#", 1)[0]


class AccountsConfigError(Exception):
    """tables/accounts.json is present but unreadable/malformed. A broken file must never silently fall back to the
    default login (that would run on an account outside the configured set); callers refuse the vendor instead."""


def _vendor_accounts(vendor: str, base_lane: str, base_provider: str) -> list[dict[str, Any]]:
    """One entry per configured account of a vendor: {lane, provider, account, reserve, config_dir}. The first account
    keeps the bare lane/provider; others get a `#<id>` suffix. An ABSENT accounts.json (or no entries for this vendor)
    -> the single default login, exactly today's behaviour. A PRESENT-but-broken file raises AccountsConfigError rather
    than defaulting. A `reserve` account is spared while a non-reserve sibling has room (see resolve)."""
    from cortados_router.paths import tables_dir
    default = [{"lane": base_lane, "provider": base_provider, "account": None, "reserve": False, "config_dir": None}]
    path = tables_dir() / "accounts.json"
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise AccountsConfigError(f"{path} is present but unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise AccountsConfigError(f"{path}: root must be a JSON object, got {type(data).__name__}")
    if vendor not in data:
        return default                                         # vendor key simply absent
    raw = data[vendor]
    if not isinstance(raw, list):                              # explicit null, a scalar or an object → malformed, never default
        raise AccountsConfigError(f"{path}: {vendor!r} must be a list of accounts, got {type(raw).__name__}")
    if not raw:
        return default                                         # explicit empty list: no accounts for this vendor
    for a in raw:
        if not isinstance(a, dict) or not (isinstance(a.get("config_dir"), str) and a["config_dir"].strip()):
            raise AccountsConfigError(f"{path}: each {vendor!r} account needs a non-empty string config_dir")
    accts = raw
    out = []
    for i, a in enumerate(accts):
        aid = str(a.get("id") or i)
        out.append({"lane": base_lane if i == 0 else f"{base_lane}#{aid}",
                    "provider": base_provider if i == 0 else f"{base_provider}#{aid}",
                    "account": aid, "reserve": bool(a.get("reserve")),
                    "config_dir": os.path.expanduser(a["config_dir"])})
    # Duplicate ids (or ids that collide with a generated lane key) would mix execution and quota identities: routing
    # would keep two candidates while sampling collapses them to one directory. Reject the collision.
    lanes = [a["lane"] for a in out]
    if len(set(a["account"] for a in out)) != len(out) or len(set(lanes)) != len(lanes):
        raise AccountsConfigError(f"{path}: {vendor!r} account ids must be unique (they key both routing and quota)")
    return out


def _base_provider(provider: str) -> str:
    """The backend a per-account provider maps to: `claude-cli#b` -> `claude-cli`."""
    return (provider or "").split("#", 1)[0]


def _expand_claude_accounts(cands: list[dict[str, Any]], accounts_aware: bool = False) -> list[dict[str, Any]]:
    """Replace each Claude candidate with one per configured account (Option A), binding account id, reserve flag and
    config_dir. Applied to scored and static/fallback lists so account identity is never lost.

    `accounts_aware` is the host's capability (execution_routing.select sets it from supports_accounts). When accounts
    ARE configured but the host cannot honour a specific account (accounts_aware False), Claude is excluded entirely
    rather than offered on the first bare-provider account — otherwise the host would run on its ambient login while the
    decision was metered against account A. A single default login (no accounts.json) leaves candidates unchanged for
    every host; a broken config refuses Claude."""
    try:
        accts = _vendor_accounts("claude", "claude", "claude-cli")
    except AccountsConfigError:
        accts = None                                           # broken config: refuse Claude rather than run on an ambient login
    configured = accts is not None and (len(accts) > 1 or accts[0].get("account") is not None)
    out = []
    for c in cands:
        is_claude = c.get("lane") == "claude" and _base_provider(c.get("provider", "")) == "claude-cli"
        if not is_claude or c.get("account") is not None:      # non-Claude, or already expanded
            out.append(c)
        elif accts is None:
            continue                                           # unreadable accounts config: drop Claude
        elif not configured:
            out.append(c)                                      # single default login: plain Claude, unchanged for all hosts
        elif not accounts_aware:
            continue                                           # accounts configured but host can't honour one: do not offer Claude
        else:
            for a in accts:
                out.append({**c, "lane": a["lane"], "provider": a["provider"], "account": a["account"],
                            "reserve": a["reserve"], "config_dir": a["config_dir"]})
    return out


def _pool_tokens_per_pct(c: dict[str, Any]) -> float | None:
    """Learned size of the pool this route draws on (tokens per 1 % of its window), from unit-cost.json; None if unknown."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        key = {("claude", "all"): "claude:all", ("claude", "fable"): "claude:fable", ("codex", "all"): "codex:all", ("antigravity", "gemini"): "antigravity:gemini",
               ("antigravity", "claude-gpt"): "antigravity:claude-gpt", ("cursor", "cursor-models"): "cursor:cursor-models", ("cursor", "other-models"): "cursor:other-models"}.get((_base_lane(c["lane"]), pool))
        uc = (sc.unit_costs() or {}).get(key or "") or {}
        return float(uc["tokens_per_pct"]) if uc.get("tokens_per_pct") else None
    except Exception:
        return None


def _tokens_left_for(c: dict[str, Any]) -> float | None:
    """Absolute room in the pool this route draws on, from the learned unit cost (None when unknown)."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
        if _reading_for_other_login(lane, c):
            return None                                              # cached reading is for a different login (reorder or default<->named): no capacity credit
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        key = {("claude", "all"): "claude:all", ("claude", "fable"): "claude:fable", ("codex", "all"): "codex:all", ("antigravity", "gemini"): "antigravity:gemini",
               ("antigravity", "claude-gpt"): "antigravity:claude-gpt", ("cursor", "cursor-models"): "cursor:cursor-models", ("cursor", "other-models"): "cursor:other-models"}.get((_base_lane(c["lane"]), pool))
        uc = (sc.unit_costs() or {}).get(key or "") or {}
        if "#" not in c["lane"]:                                     # base/default account: the learned absolute remaining
            return float(uc["remaining_tokens"]) if uc.get("remaining_tokens") else None
        # A sibling account shares the base pool-SIZE estimate (tokens_per_pct) but NOT its remaining quota: derive this
        # account's remaining from its OWN window headroom, or unitcost.learn's bare-lane remaining would be reused for both.
        tpp = uc.get("tokens_per_pct")
        headroom = _min_headroom_pct(lane, pool)
        return float(tpp) * float(headroom) if tpp and headroom is not None else None
    except Exception:
        return None


def _min_headroom_pct(lane: dict[str, Any], pool: str) -> float | None:
    """The model is as scarce as the tightest window it draws on: the lane's general windows AND its own pool (e.g.
    Fable), so the MIN headroom across both, not general alone (and not the pacing-tightest window)."""
    relevant = [w for w in (lane.get("windows") or []) if w.get("pool", "all") in ("all", pool)]
    return min((float(w["headroom_pct"]) for w in relevant if w.get("headroom_pct") is not None), default=None)


def _reading_for_other_login(reading: dict[str, Any], c: dict[str, Any]) -> bool:
    """The cached reading under this positional lane key was sampled from a DIFFERENT login than the candidate's
    (accounts reordered, or a default<->named transition: the default login's config_dir None is a distinct identity).
    The one identity rule of candidate_health, _tokens_left_for and headroom_pct_for."""
    return "config_dir" in reading and reading.get("config_dir") != c.get("config_dir")


def claude_login_identity(env: dict[str, str]) -> str | None:
    """The login a Claude CLI started with `env` runs on (scarcity's rule, which also stamps each reading's `login`)."""
    return _scarcity_module().claude_login_identity(env)


def headroom_pct_for(c: dict[str, Any], *, refresh: bool = False) -> float | None:
    """% left in the pool this route draws on, from the cached reading (refresh=False: never opens a vendor CLI).
    STRICTLY conservative (cosmetic; a wrong % is worse than none): a number only when the reading is trustworthy by the
    standard routing uses, else None:
      - the reading exists, is fresh and its last try did not fail (scarcity.reading_freshness, lane_scarcity's rule);
      - it is this candidate's login: candidate_health's config_dir rule (_reading_for_other_login) AND the EFFECTIVE
        login the reading was sampled on (its `login`) equals the one the caller executes on (c["login"]), since
        config_dir None only says "no explicit account", not which CLAUDE_CONFIG_DIR / token either process inherited;
      - a current general ("all") window is present (shared session/weekly capacity, which constrains every model):
        a reading of a per-model panel alone cannot bound the model;
      - when the model draws on a separately metered pool (Fable, …), a current window of THAT pool is in the reading
        (not rate-limited `per_model`, nor one only carried forward from an earlier read, `stale`).
    A relative CLAUDE_CONFIG_DIR also yields no login match (scarcity.claude_login_identity), so no % there either.
    (Known limit: only Fable is treated as separately metered on Claude; a model that resolved to the general pool is
    assumed to truly use it. If the vendor ever meters another model separately, add it to scarcity.pool_for.)"""
    try:
        sc = _scarcity_module()
        lane = (sc.current(refresh=refresh).get("lanes") or {}).get(c["lane"])
        if not isinstance(lane, dict) or not lane or lane.get("error"):
            return None
        fresh, failed = sc.reading_freshness(c["lane"], lane)
        if not fresh or failed:
            return None
        if _reading_for_other_login(lane, c):
            return None
        if not c.get("login") or lane.get("login") != c["login"]:
            return None                                              # effective login unconfirmed (or unknowable)
        windows = lane.get("windows") or []
        if not any(w.get("pool", "all") == "all" and w.get("headroom_pct") is not None and not w.get("stale")
                   for w in windows):
            return None                                              # the general ("all") windows that ALSO constrain
                                                                     # every model are missing/stale -> shared capacity
                                                                     # is unknown, so no % (finding 3)
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        relevant = [w for w in windows if w.get("pool", "all") in ("all", pool)]
        if any(w.get("stale") for w in relevant):
            return None                                              # part of THIS model's picture is a carried old read
        if pool != "all" and not any(
                w.get("pool") == pool and w.get("headroom_pct") is not None for w in relevant):
            return None                                              # the model's OWN separate pool is not observed this
                                                                     # read. NOTE: `per_model` (set when ANY pool was
                                                                     # carried) is NOT a blanket block here — a pool that
                                                                     # DID show fresh is bound by its own fresh window
                                                                     # above; the stale check already withholds a model
                                                                     # whose own pool was only carried forward.
        return _min_headroom_pct(lane, pool)
    except Exception:
        return None


def _policy() -> dict[str, Any]:
    try:
        return json.loads(BUDGETS_PATH.read_text()).get("policy") or {}
    except Exception:
        return {}


def lane_health(*, refresh: bool = True) -> dict[str, dict[str, Any]]:
    """Health = measured subscription scarcity (scarcity.py: % used vs % of the window elapsed, per vendor usage screen).
    ok=False only when a window is spent or the vendor is refusing; everything softer is a ranking signal, not a gate.
    `pools` holds the separately metered pools of a lane (Claude Fable week, Cursor models vs other models, …): see candidate_health."""
    sc = _scarcity_module()
    out: dict[str, dict[str, Any]] = {}
    for lane, v in sc.lane_scarcity(refresh=refresh).items():
        out[lane] = {**_health_row(v.get("scarcity"), v.get("tightest"), v.get("expiring_pct"), v.get("refusing")),
                     "pools": {p: _health_row(pv.get("scarcity"), pv.get("tightest"), pv.get("expiring_pct"), pv.get("spent")) for p, pv in (v.get("pools") or {}).items()},
                     "config_dir": v.get("config_dir")}   # the login this reading was sampled from (account identity, not the positional lane name)
        if lane == "local" and v.get("resource_managed"):
            out[lane].update(resource_managed=True, deployments=v.get("deployments", {}), why=v.get("why"))
    return out


def _health_row(s, w, expiring, spent) -> dict[str, Any]:
    w = w or {}
    spent = bool(spent) or (s is not None and s >= 1.0)
    why = (f"{w.get('window', 'window')} {w.get('used_pct')}% used, resets {w.get('resets_at')}" if w else "refusing") if spent else None
    return {"ok": not spent, "why": why, "scarcity": s, "tightest": w, "expiring_pct": expiring or 0}


def candidate_health(c: dict[str, Any], health: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A model is as scarce as the tightest pool it draws on: the lane's general windows plus its own pool when it has one."""
    lane = health.get(c["lane"]) or {}
    if _reading_for_other_login(lane, c):
        # the cached reading under this positional lane key was sampled from a DIFFERENT login (accounts reordered, or a
        # default<->named transition — the default login's config_dir is None, a distinct identity). Do not trust it
        # either way: unavailable until a fresh same-identity sample, rather than risk routing to a spent login.
        return {"ok": False, "scarcity": None, "expiring_pct": 0, "tightest": {}, "pool": None,
                "why": "stale cross-account reading; awaiting a fresh sample for this login"}
    if c["lane"] == "local" and lane.get("resource_managed"):
        deployment = (lane.get("deployments") or {}).get(c.get("model"))
        # resource_rejected: unverified or stale local capacity is unroutable, not merely spent, so resolve's last-resort
        # "first candidate shown" fallback must never pick it either (Codex review 2026-10-21).
        if deployment is None:
            return {"ok": False, "scarcity": 1.0, "expiring_pct": 0, "tightest": {}, "pool": None, "resource_rejected": True,
                    "why": lane.get("why") or "local model has no verified deployment"}
        return {**deployment, "expiring_pct": 0, "tightest": {}, "pool": None, "resource_rejected": not deployment.get("ok", False)}
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
    b = _base_lane(lane)   # a per-account lane (claude#b) inherits its vendor's context limits, including [1m]
    # A transport-keyed ceiling applies only to the transport that measured it (the Donna
    # model gateway's calibrated fit); unknown or absent transports keep the lane ceiling.
    by_transport = (_context_cfg().get("ceiling_tokens_by_transport") or {}).get(b) or {}
    if transport is not None and transport in by_transport:
        return int(by_transport[transport])
    cx = _context_cfg().get("ceiling_tokens") or {}
    if b == "claude" and "[1m]" in (model or ""):
        return int(cx.get("claude[1m]", 1000000))
    return int(cx.get(b, 200000))


def static_thresholds(pol: dict[str, Any] | None = None) -> tuple[float, float]:
    """(demote_above, prefer_expiring_headroom_pct) from budgets.json's policy: the two thresholds of the static
    (unscored) account ranking. Shared by resolve and the provider's sub/ pin so both rank accounts the same way."""
    pol = _policy() if pol is None else pol
    return float(pol.get("demote_above", 0.5)), float(pol.get("prefer_expiring_headroom_pct", 10))


def spared_reserves(cands: list[dict[str, Any]], ok_of: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """Reserve accounts (accounts.json "reserve": true) to hold back: each one whose SAME model on the same base lane has
    an available non-reserve sibling. A reserve account is used once every non-reserve sibling for that model is spent.
    Only non-reserve siblings are consulted, so marking the returned ones unavailable does not change the answer."""
    return [c for c in cands
            if c.get("reserve") and any(k.get("model") == c.get("model") and _base_lane(k["lane"]) == _base_lane(c["lane"])
                                        and not k.get("reserve") and ok_of(k) for k in cands)]


def static_scarcity_rank(h: dict[str, Any], demote: float, expiring: float) -> tuple[int, int]:
    """The scarcity part of the static ranking for one candidate's health: first an account whose expiring credit would
    otherwise be lost (and that is not scarce), last a scarce (demoted) one. Lower sorts first."""
    sc, exp = h.get("scarcity") or 0.0, h.get("expiring_pct") or 0
    return (0 if exp >= expiring and sc < demote else 1, 1 if sc >= demote else 0)


def resolve(decision: dict[str, Any], unit_kind: str = "interactive", history_chars: int = 0, sticky: dict[str, Any] | None = None,
            escalate: dict[str, Any] | None = None, input_fit: dict[str, Any] | None = None,
            allowed_routes: tuple[tuple[str, str], ...] | None = None,
            execution: bool = False, minimum_band: str | None = None, accounts_aware: bool = False) -> dict[str, Any]:
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
    demote, expiring = static_thresholds(pol)
    pref = (t.get("work_kind_preference") or {}).get(ans.get("work_kind") or "", [])
    scored = scored_candidates(t, band, ans.get("work_kind"), accounts_aware)
    # The static band list carries no account metadata; expand it too, so a missing/unreadable score catalog still binds
    # the routed Claude account (identity is never lost on the fallback path).
    cands = scored if scored else _expand_claude_accounts([dict(c) for c in t["candidates"].get(band, [])], accounts_aware)
    # Explicit degraded-service policy: do not spend Claude by default when
    # the scorer cannot classify a request. Retain quota and context-fit gates.
    fallback_routes = (t.get("fallback", {}).get("routes")
                       if not execution and (decision.get("degraded") or not decision.get("answers")) else None)
    if fallback_routes:
        cands = _expand_claude_accounts([dict(c) for c in fallback_routes], accounts_aware)
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
        local_cap = ch_of[id(c)].get("max_context_tokens") if c['lane'] == 'local' else None
        if isinstance(local_cap, int) and local_cap > 0:
            cap = min(cap, local_cap)
        if execution and c['provider'] == 'codex-cli':
            cap = int(cx.get('provider_ceiling_tokens', {}).get('codex-cli', 200000))
        why = None
        if need > cap:
            why = f"turn ≈{need // 1000}k tokens exceeds {c['lane']} ceiling {cap // 1000}k"
        elif fit.get('known') is False and c['lane'] in ('antigravity', 'local'):
            why = 'exact session history unavailable; constrained transport not admitted'
        elif _base_lane(c['lane']) in byte_caps and int(fit.get('input_bytes') or 0) > int(byte_caps[_base_lane(c['lane'])]):
            why = f"input estimate {fit['input_bytes']} bytes exceeds {c['lane']} byte ceiling {byte_caps[_base_lane(c['lane'])]}"
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

    def apply_reserve():
        # reserve accounts (accounts.json "reserve": true): spared only while the SAME model on a non-reserve sibling is
        # available, so account A stays in reserve while B can serve this model, but A is used when B's pool for it is spent
        # (never letting the reserve preference pick an underqualified model). Applied on BOTH the scored and static paths.
        for c in spared_reserves(cands, ok_of):
            ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"reserve account; a non-reserve account has {c['model']} with room"}

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
            # Forest 2026-10-26 (reserve by time-to-reset): a window that resets SOON with credit unspent must beat one
            # that is merely under-pace with a far reset. scarcity.window_view sets expiring_pct only for a weekly+ window
            # past 85% elapsed, equal to the share wasted at the current pace. Gate it on sc <= 0: a lane whose tightest
            # window is scarce NOW (e.g. its 5-hour session is nearly spent, sc > 0) cannot spend the weekly credit anyway,
            # so the deadline discount must not override that immediate limit (Codex review 2026-10-26). Zero outside the
            # expiring zone, so routing elsewhere is unchanged.
            expiring_soon = float(sel.get("expiring_soon_points", 250)) * (exp_of(c) / 100.0) if sc <= 0 else 0.0
            c["cost"] = round(c["weight"] + sel.get("scarcity_points", 30) * max(sc, 0.0)
                              - sel.get("expiring_credit_points", 80) * max(-sc, 0.0) - expiring_soon, 1)
            # capacity (Forest 2026-09-26): plans are not normalised; among pools with room, the one with the most TOKENS left gets a further credit
            tl = _tokens_left_for(c)
            # the credit needs a FRESH reading: an unknown or failed pool (scarcity None, read as 0 above) earns nothing
            if tl is not None and sc <= 0 and ch_of[id(c)].get("scarcity") is not None:
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
        # primary-source rule: a Claude/GPT/Gemini route on another VENDOR is secondary, usable only when the vendor's own pool is spent.
        # Compare the base lane so a second same-vendor account (claude#b) is NOT treated as a cross-vendor secondary of claude.
        for c in cands:
            native = native_lane_for(c.get("family") or c["model"])
            c["secondary"] = bool(native) and _base_lane(c["lane"]) != native
        for c in cands:
            if c.get("secondary"):
                # native-first holds only while an ELIGIBLE native candidate for the same model is available (any
                # configured account, including an unsampled one that reads ok). Checking the candidates themselves,
                # not the scarcity cache, both protects unsampled accounts and never suppresses a secondary when there
                # is no native route to prefer (single-account deployments keep their Cursor fallback).
                fam = c.get("family") or c["model"]
                native = native_lane_for(fam)
                if any(not k.get("secondary") and _base_lane(k["lane"]) == native
                       and (k.get("family") or k["model"]) == fam and ok_of(k) for k in cands):
                    ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"secondary source; {native} still has {fam}"}
        apply_reserve()      # the expiring-credit term still steers usage onto whichever account's credit is about to reset
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
        apply_reserve()      # the static/fallback path honours the reserve policy too, not only scored selection
        cands.sort(key=lambda c: static_scarcity_rank(ch_of[id(c)], demote, expiring)
                   + (pref.index(c["lane"]) if c["lane"] in pref else len(pref), base[id(c)]))
    usable = [c for c in cands if ok_of(c)]
    skipped = [{"lane": c["lane"], "model": c["model"], "pool": ch_of[id(c)].get("pool"), "why": ch_of[id(c)].get("why")} for c in cands if not ok_of(c) and not (ch_of[id(c)].get("why") or "").startswith("secondary")][:8]
    fitting = [c for c in cands if id(c) not in transport_rejected and not ch_of[id(c)].get("resource_rejected")]
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


def usage_summary(limit: int = 12) -> dict[str, Any]:
    """A roll-up of logged routes for the dashboard: counts by band and chosen lane, the degraded rate, S1 latency
    percentiles, and the most recent turns. Read-only and bounded: it opens a read-only connection to router.db (never
    creating or migrating it, never taking the write lock), aggregates in SQL, and fetches only the N recent rows and the
    handful of order statistics it needs — so a dashboard poll neither blocks logging nor grows with history. No
    subscription CLI is opened. Request text is never included."""
    import math
    # A negative LIMIT is "no limit" to SQLite: clamp, so a bad caller value cannot turn the bounded read unbounded.
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    empty = {"total_routes": 0, "by_band": {}, "by_lane": {}, "degraded": {"count": 0, "of": 0}, "s1_latency_ms": {}, "recent": []}
    path = STATE / "router.db"
    if not path.exists():
        return empty
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        return {**empty, "error": f"{type(exc).__name__}: {exc}"}
    conn.row_factory = sqlite3.Row
    LAT_WINDOW = 500                                        # S1 latency is read over the most recent turns, so work is bounded, not O(history)
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routes'").fetchone():
            return empty                                   # a db that exists but has no routes table yet: no data, not an error
        total = conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        by_band = {r["band"]: r["n"] for r in conn.execute("SELECT band, COUNT(*) AS n FROM routes WHERE band IS NOT NULL GROUP BY band")}
        by_lane = {f'{r["lane"]}/{r["model"]}': r["n"] for r in conn.execute("SELECT lane, model, COUNT(*) AS n FROM routes WHERE lane IS NOT NULL GROUP BY lane, model")}
        # S1 health (degraded rate, latency) is measured over SCORED rows only: a client pin (engine 'pin', the provider's
        # sub/ path) never asked S1, so counting it would dilute the degraded rate and pull latency toward zero. Pins stay
        # in the route totals above. A routes table without an engine column (pre-DDL fixture) cannot hold pin rows.
        has_engine = any(r[1] == "engine" for r in conn.execute("PRAGMA table_info(routes)"))
        scored_only = "WHERE COALESCE(engine, '') != 'pin'" if has_engine else ""
        degraded, scored = conn.execute(f"SELECT COALESCE(SUM(degraded), 0), COUNT(*) FROM routes {scored_only}").fetchone()
        ems = sorted(r[0] for r in conn.execute(
            f"SELECT engine_ms FROM routes {scored_only or 'WHERE 1'} AND engine_ms IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (LAT_WINDOW,)))
        window = [dict(r) for r in conn.execute("SELECT ts, platform, band, lane, model, degraded, turn_ms, "
                                                "response_chars FROM routes ORDER BY ts DESC LIMIT ?", (limit,))]
    except sqlite3.Error as exc:
        return {**empty, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()
    if ems:
        n = len(ems)
        median = ems[n // 2] if n % 2 else (ems[n // 2 - 1] + ems[n // 2]) / 2
        lat = {"median": median, "p90": ems[min(n - 1, max(0, math.ceil(0.9 * n) - 1))],
               "p90_convention": "nearest-rank", "window": n}
    else:
        lat = {}
    recent = [{"ts": r["ts"], "platform": r["platform"], "band": r["band"], "lane": r["lane"], "model": r["model"],
               "degraded": bool(r["degraded"]), "turn_ms": r["turn_ms"], "response_chars": r["response_chars"]}
              for r in window[:limit]]
    return {"total_routes": total, "by_band": by_band, "by_lane": by_lane,
            "degraded": {"count": int(degraded), "of": scored}, "s1_latency_ms": lat, "recent": recent}


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
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(x['ts']))} {x['platform'] or '':8s} {x['band']:3s} {str(x['lane']):11s} {str(x['model'])[:26]:26s} sub={x['subagents']} D={x['D']} {'-' if x['engine_ms'] is None else x['engine_ms']}ms {'DEGRADED ' if x['degraded'] else ''}| {x['request']}")
