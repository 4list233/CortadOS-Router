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
import socket
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

from cortados_router import router_db
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
S1_RETRIES = int(_env_get("CORTADOS_S1_RETRIES", "1"))   # retries when S1 was not reached at all (refused / reset); see _s1_unreached
S1_RETRY_BACKOFF_S = float(_env_get("CORTADOS_S1_RETRY_BACKOFF_S", "0.3"))
S1_TOKEN = _env_get("CORTADOS_S1_TOKEN")   # sent as a bearer when the chosen scorer requires one (a non-loopback bind)
FAIL_UP_BELOW = float(_env_get("CORTADOS_S1_FAIL_UP", "0.75"))   # Forest 2026-09-24: the right answer over cost   # a feature whose top probability is below this takes the HIGHER of its two most likely levels
OUTWARD_AT = float(_env_get("CORTADOS_S1_OUTWARD_AT", "0.25"))   # the destructive question is called at a lower probability (Jev: stricter gate for destructive actions)


def _s1_unreached(exc: BaseException) -> bool:
    """True only when the request did not reach a serving S1: the connection was refused, or reset / closed before any
    reply (http.client.RemoteDisconnected is a ConnectionResetError). Only that case is retried.

    Changed 2026-11-22 (owner-approved) from the 2026-10-26 rule, which also retried timeouts and 429/5xx replies. On
    the Studio that day S1 ran one serial worker at ~4.2 s per score; a timed-out request stayed in its queue and was
    still scored while the retry queued a second copy behind it (259 first attempts timed out, 190 failed twice), so a
    retry on a timeout doubled the load exactly when S1 was saturated. A timeout or any HTTP status (including 503
    busy, S1's answer when it cannot finish before the deadline) now degrades at once; degraded never defaults to
    Claude (resolve's hard rule)."""
    if isinstance(exc, urllib.error.HTTPError):           # S1 answered: whatever the status, it was reached
        return False
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return False
    return isinstance(reason, (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError))


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
    # Retry only when S1 was never reached (refused / reset: a restarting S1, or its connection cap). Never on a timeout
    # or an HTTP reply: see _s1_unreached for the 2026-11-22 change to the 2026-10-26 rule.
    d = None
    for _attempt in range(S1_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=S1_TIMEOUT_S) as r:
                d = json.loads(r.read())
            break
        except Exception as exc:
            if _s1_unreached(exc) and _attempt < S1_RETRIES:
                time.sleep(S1_RETRY_BACKOFF_S)
                continue
            busy = isinstance(exc, urllib.error.HTTPError) and exc.code == 503   # S1 shed it: queue full or no time left
            return {"answers": None, "degraded": True, "engine": "s1", "engine_ms": int((time.time() - t0) * 1000),
                    "error": ("S1 busy: " if busy else "") + f"{type(exc).__name__}: {str(exc)[:120]}"}
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
# One loader at a time: callers on worker threads (the S1 outcome loop's run workers) can arrive while the first is still
# executing scarcity.py, and must wait for it rather than see a half-executed module (Studio 2026-11-04).
_sc_lock = threading.Lock()


def _scarcity_module():
    """core.py is loaded by file path under a unique name (no package), so its sibling is loaded the same way.
    The module is published only after it has executed completely; a failed load publishes nothing (the next call
    retries)."""
    global _sc_mod
    if _sc_mod is None:
        with _sc_lock:
            if _sc_mod is None:
                import importlib.util
                spec = importlib.util.spec_from_file_location("cortados_router_scarcity",
                                                              str(Path(__file__).with_name("scarcity.py")))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _sc_mod = mod
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
    if by_score and ceiling_work_applies(cw, use_case, D):
        floor = max(floor, val(cw.get("require", "R4")))  # insight work goes to the frontier (Forest 2026-09-25)
    return round(min(max(req, floor), best), 1)


def scored_candidates(t: dict[str, Any], band: str, use_case: str | None, accounts_aware: bool = False,
                      api_lane: bool = False) -> list[dict[str, Any]] | None:
    """Every reachable model × every subscription that can run it, with its index for this use case. No band lists.
    `api_lane` (#143): also each Claude model on the API credit lane (API_LANE), offered only when the host lists its
    pairs in allowed_routes (resolve decides; provider/routing.api_lane_pairs chooses which models)."""
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
    if api_lane:
        # The same Claude models on Anthropic's pay-per-use API, spending only promotional credit (#114). One pool (the
        # grant's dollars), no accounts, no separately metered Fable week: so no `pool` and never account-expanded.
        out += [dict(c, lane=API_LANE, provider=API_LANE) for c in out
                if c["lane"] == "claude" and c["provider"] == "claude-cli"]
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


def account_row(accounts: list[dict[str, Any]], account: str | None) -> dict[str, Any] | None:
    """The row of one already-loaded `_vendor_accounts` list whose account id is `account`; None when no account is
    named or none matches. Takes the list rather than reading accounts.json, so a caller that needs several fields of
    one account (its config_dir AND its lane) gets them from a single snapshot of the file."""
    if not account:
        return None
    return next((a for a in accounts if a.get("account") == account), None)


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


# (base lane, pool) -> the vendor's unit-cost key; an account lane (claude#b) gets its own key with the same suffix
# (claude#b:all), which is what unitcost.learn writes for it (#132).
_POOL_KEYS = {("claude", "all"): "claude:all", ("claude", "fable"): "claude:fable", ("codex", "all"): "codex:all",
              ("antigravity", "gemini"): "antigravity:gemini", ("antigravity", "claude-gpt"): "antigravity:claude-gpt",
              ("cursor", "cursor-models"): "cursor:cursor-models", ("cursor", "other-models"): "cursor:other-models"}


def _pool_keys(lane: str, pool: str) -> tuple[str | None, str | None]:
    """(this lane's own unit-cost key, the vendor's base key): ('claude#b:all', 'claude:all') for claude#b, both the
    base key for a bare lane, (None, None) for a pool nobody learns."""
    base = _POOL_KEYS.get((_base_lane(lane), pool))
    if base is None:
        return None, None
    return (lane + base[base.index(":"):] if "#" in lane else base), base


def _pool_size_priors() -> dict[str, float]:
    """selection.pool_size_prior_tokens_per_pct: per base pool key, the size a LOWER-BOUND learned value may not undercut
    (tables/routing-table.json `_pool_size_prior` says why). Bad entries are ignored."""
    try:
        raw = (table().get("selection") or {}).get("pool_size_prior_tokens_per_pct") or {}
        return {k: float(v) for k, v in raw.items() if not k.startswith("_") and isinstance(v, (int, float)) and v > 0}
    except Exception:
        return {}


def _pool_size_derivations() -> dict[str, dict[str, Any]]:
    """selection.pool_size_from_pool: per base pool key, {"of": <pool of the same lane>, "ratio": r} for a pool that
    ALSO draws on `of` (#143: Fable draws on the all-models week too; tables/routing-table.json `_pool_size_from_pool`
    has the evidence). Bad entries are ignored."""
    try:
        raw = (table().get("selection") or {}).get("pool_size_from_pool") or {}
        return {k: {"of": str(v["of"]), "ratio": float(v["ratio"])} for k, v in raw.items()
                if not k.startswith("_") and isinstance(v, dict) and isinstance(v.get("of"), str)
                and isinstance(v.get("ratio"), (int, float)) and not isinstance(v.get("ratio"), bool) and v["ratio"] > 0}
    except Exception:
        return {}


def _binding_pool(reading: dict[str, Any], pool: str, of: str) -> str:
    """Of two pools a route draws on at once, the one whose windows leave it less headroom (`of` on a tie or when
    `pool` has no window in the reading): the same "as scarce as the tightest window" rule as _min_headroom_pct."""
    def least(p: str) -> float | None:
        return min((float(w["headroom_pct"]) for w in (reading.get("windows") or [])
                    if isinstance(w, dict) and w.get("pool", "all") == p and w.get("headroom_pct") is not None),
                   default=None)
    h_pool, h_of = least(pool), least(of)
    if h_pool is None or (h_of is not None and h_of <= h_pool):
        return of
    return pool


API_LANE = "anthropic-api"   # #114 / #143: the pay-per-use Anthropic API lane, spending only promotional credit


def _api_pool_size(c: dict[str, Any], reading: dict[str, Any]) -> dict[str, Any]:
    """#143: the API credit lane's size as _pool_size gives it, in tokens of THIS model per 1 % of the grant. The
    reading's 100 % is the stop line (api_credits.window_numbers), so 1 % = the stop line's dollars / 100, bought at the
    model's blended $/1M (the same price the subscription burn uses). The stop line is the reading's own
    `stop_line_usd`; a reading written before it carried one gives only the grant total, so the module's default stop
    fraction applies to it. No price or no budget: size unknown (None), like any unlearned pool."""
    from cortados_router import api_credits
    stop = reading.get("stop_line_usd")
    if stop is None and reading.get("budget_usd") is not None:
        stop = float(reading["budget_usd"]) * api_credits.STOP_FRACTION
    price = c.get("price")
    tpp = (float(stop) / 100.0) / (float(price) / 1e6) if stop and price else None
    return {"tpp": tpp, "learned": None, "rec": {}, "own": True, "floored": False, "reading": reading, "pool": "all",
            "api": True}


def _pool_size(c: dict[str, Any]) -> dict[str, Any]:
    """The learned size of the pool this route draws on, from unit-cost.json:
      tpp      tokens per 1 % of the pool's window, after the prior (None if unknown);
      rec      the unit-cost record used; own: it is this lane's own record (claude#b:all for claude#b, #132), not the
               vendor's base record a sibling account falls back to when its own is not learned yet;
      floored  the learned value was a lower bound below the table's prior, so the prior was used.
    A learned value flagged `lower_bound` (unitcost.py: the ledger sees only provider-routed tokens, so direct use of
    the same login moves the % without adding tokens) never makes a pool look smaller than its prior; a value that is
    not a lower bound, or a pool with no learned value, is left as learned (no prior is invented for an unknown pool).
    #143: a pool that also draws on another pool of its lane (selection.pool_size_from_pool: Fable also draws on the
    all-models week) is sized by whichever of the two binds it (_binding_pool), with `bound_by` naming it; when its own
    window binds, a missing or lower-bound learned size is raised to ratio × the other pool's size. The API credit lane
    is sized in dollars (_api_pool_size)."""
    sc = _scarcity_module()
    lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
    if _base_lane(c["lane"]) == API_LANE:
        return _api_pool_size(c, lane)
    pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
    own, base = _pool_keys(c["lane"], pool)
    derived = _pool_size_derivations().get(base or "") if pool != "all" else None
    if derived and pool != derived["of"] and _binding_pool(lane, pool, derived["of"]) == derived["of"]:
        return {**_pool_size({**c, "pool": derived["of"]}), "pool": pool, "bound_by": derived["of"]}
    ucs = sc.unit_costs() or {}
    rec, is_own = ucs.get(own or "") or {}, True
    if not rec.get("tokens_per_pct") and base != own:
        rec, is_own = ucs.get(base or "") or {}, False             # the sibling's own pool is not learned yet: the vendor's size
    tpp = float(rec["tokens_per_pct"]) if rec.get("tokens_per_pct") else None
    prior = _pool_size_priors().get(base or "")
    if derived and pool != derived["of"]:
        of_size = _pool_size({**c, "pool": derived["of"]})["tpp"]
        estimate = derived["ratio"] * of_size if of_size else None
        if estimate and (tpp is None or (rec.get("lower_bound") and estimate > tpp)):
            prior = max(prior or 0.0, estimate)
            if tpp is None:                                         # nothing learned: the estimate is the size
                return {"tpp": estimate, "learned": None, "rec": rec, "own": is_own, "floored": True,
                        "reading": lane, "pool": pool}
    floored = bool(tpp is not None and rec.get("lower_bound") and prior and prior > tpp)
    return {"tpp": prior if floored else tpp, "learned": tpp, "rec": rec, "own": is_own, "floored": floored,
            "reading": lane, "pool": pool}


def _pool_tokens_per_pct(c: dict[str, Any]) -> float | None:
    """Learned size of the pool this route draws on (tokens per 1 % of its window), from unit-cost.json under the lane's
    OWN key (claude#b:all for account B, falling back to the vendor's claude:all while B's is unlearned), floored by
    the table's prior for a lower-bound value (_pool_size); None if unknown."""
    try:
        return _pool_size(c)["tpp"]
    except Exception:
        return None


def _tokens_left_for(c: dict[str, Any]) -> float | None:
    """Absolute room in the pool this route draws on, from the learned unit cost (None when unknown)."""
    try:
        ps = _pool_size(c)
        lane, rec, tpp = ps["reading"], ps["rec"], ps["tpp"]
        if _reading_for_other_login(lane, c):
            return None                                              # cached reading is for a different login (reorder or default<->named): no capacity credit
        if ps.get("api") or (ps["floored"] and ps["learned"] is None):
            # #143: a size that is not learned from this pool's own record (the API grant's dollars, or a Fable week
            # estimated from the all-models week): its room is that size times the window headroom it reads now.
            headroom = _min_headroom_pct(lane, ps["pool"])
            return float(tpp) * float(headroom) if tpp and headroom is not None else None
        if ps["own"] and rec.get("remaining_tokens"):
            # this lane's own learned remaining (scaled up when the size was floored by the prior: same % left, bigger pool)
            scale = (tpp / ps["learned"]) if ps["floored"] and ps["learned"] else 1.0
            return float(rec["remaining_tokens"]) * scale
        if "#" not in c["lane"] and ps["own"]:
            return None                                              # base lane without a learned remaining: unknown, as before
        # A sibling account on the vendor's base size estimate (its own is not learned yet), or its own record without
        # a remaining: this account's remaining from its OWN window headroom, never the base lane's remaining.
        headroom = _min_headroom_pct(lane, ps["pool"])
        return float(tpp) * float(headroom) if tpp and headroom is not None else None
    except Exception:
        return None


def window_expected_loss_pct(w: dict[str, Any], fetched_at: float | None, now: float,
                             burn_pct_per_h: float | None = None) -> float | None:
    """% of a window's quota expected to be LOST at its reset: headroom minus the use expected until the reset
    (burn rate × hours to reset), floored at 0 (#132, owner 2026-11-22). The burn rate is the learned recent rate
    (scarcity.recent_burn_rates) when given, else the window's own pace so far (used % ÷ hours elapsed, the pace
    window_view projects with). None when the window has no reset time, the reading lacks what the pace needs, or the
    reset has already passed (the reading is from the previous window)."""
    reset, used = w.get("reset_epoch"), w.get("used_pct")
    if reset is None or used is None:
        return None
    headroom = max(0.0, 100.0 - float(used))
    h_left = (float(reset) - now) / 3600.0
    if h_left <= 0:
        return None
    if burn_pct_per_h is None:
        burn_pct_per_h = _window_pace_pct_per_h(w, fetched_at)
        if burn_pct_per_h is None:
            return None
        if burn_pct_per_h == IMMINENT:
            return round(headroom, 1)                                # the reset is imminent: what is left is lost
    return round(max(0.0, headroom - max(0.0, float(burn_pct_per_h)) * h_left), 1)


IMMINENT = float("inf")   # _window_pace_pct_per_h: the window is (almost) over at the sample, so it has no pace left


def _window_pace_pct_per_h(w: dict[str, Any], fetched_at: float | None) -> float | None:
    """A window's own pace so far, % per hour (used % ÷ hours elapsed, with window_view's 5 % floor on the elapsed
    share): the burn rate window_expected_loss_pct and api_pace fall back to when no learned recent rate exists.
    IMMINENT when the window was at least 99.9 % elapsed at the sample; None when the reading lacks what it needs."""
    reset, used, e = w.get("reset_epoch"), w.get("used_pct"), w.get("elapsed_pct")
    if reset is None or used is None or e is None or not fetched_at:
        return None
    e = float(e) / 100.0
    left_at_sample = (float(reset) - float(fetched_at)) / 3600.0
    if left_at_sample <= 0:
        return None
    if e >= 0.999:
        return IMMINENT
    window_h = left_at_sample / (1.0 - e)
    return float(used) / (max(e, 0.05) * window_h)


def _expected_loss_pct(c: dict[str, Any], now: float | None = None) -> float | None:
    """% of this route's pool expected to be lost at its next reset: the smallest across the long windows it draws on
    (the lane's general windows and its own pool; 5-hour windows renew too fast to lose anything, as in scarcity._roll),
    since a model can only use what EVERY window it draws on leaves it. None (callers fall back to today's capped
    scarcity) when any of those windows lacks a reset time, is a carried-forward stale read, or the reading is for
    another login."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(c["lane"]) or {}
        if not lane or _reading_for_other_login(lane, c):
            return None
        pool = c.get("pool") or sc.pool_for(c["lane"], c.get("family") or c["model"], list(lane.get("pools") or {})) or "all"
        wins = [w for w in (lane.get("windows") or []) if isinstance(w, dict) and w.get("pool", "all") in ("all", pool)
                and not str(w.get("window", "")).startswith("5-hour")]
        if not wins or any(w.get("stale") for w in wins):
            return None
        now = time.time() if now is None else now
        burns = sc.recent_burn_rates() if hasattr(sc, "recent_burn_rates") else {}
        losses = [window_expected_loss_pct(w, lane.get("fetched_at"), now, burns.get((c["lane"], w.get("window"))))
                  for w in wins]
        return None if any(x is None for x in losses) else min(losses)
    except Exception:
        return None


def api_pacing_cfg(t: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """selection.api_pacing when it is on (table v22), else None: the flag of the API grant's spend-down pacing."""
    t = table() if t is None else t
    cfg = (t.get("selection") or {}).get("api_pacing")
    return cfg if isinstance(cfg, dict) and cfg.get("enabled") is True else None


def api_pace(now: float | None = None) -> dict[str, Any] | None:
    """The API credit grant's ONE standardized "percent used" and its pace to expiry (scarcity v2 §9 step 4, owner
    2026-11-24: the grant is "just like another subscription", ranked only by its percent used against its pace).
      used_pct      100 × dollars spent ÷ the stop line's dollars (the reading's own % : api_credits.window_numbers
                    makes 100 % the stop line, $190 of a $200 grant);
      hours_left    hours until the grant stops counting (the window's reset: an hour before the expiry);
      needed_pct_h  the pace that reaches the stop line exactly then: (100 − used) ÷ hours_left;
      actual_pct_h  what it is absorbing now: the learned recent burn (scarcity.recent_burn_rates), else the window's
                    own pace so far, the same two sources a subscription's expected loss uses;
      forecast_pct  used + actual × hours_left (capped at 100), the % it is forecast to have used at expiry;
      share         needed ÷ (needed + actual): the grant's share of an underuse split it takes part in (underuse_split).
                    Lagging its pace (actual < needed) it draws more than half, ahead of it less, and 1 while it has
                    absorbed nothing at all. Expiry pressure is the 1 ÷ hours_left in `needed`.
    None (the grant takes no paced share) when the reading is missing, stale, failed or carries no grant window;
    {"error": <exception name>} when computing it failed (resolve names both in the decision's rules). A grant past its
    stop line or its last counting hour has share 0: it is never paced into more work."""
    try:
        sc = _scarcity_module()
        lane = (sc.current().get("lanes") or {}).get(API_LANE) or {}
        if not lane:
            return None
        fresh, failed = sc.reading_freshness(API_LANE, lane)
        if not fresh or failed:
            return None
        w = next((x for x in (lane.get("windows") or []) if isinstance(x, dict) and x.get("pool", "all") == "all"
                  and x.get("reset_epoch") is not None and x.get("used_pct") is not None and not x.get("stale")), None)
        if w is None:
            return None
        now = time.time() if now is None else now
        used = min(100.0, max(0.0, float(w["used_pct"])))
        h_left = (float(w["reset_epoch"]) - now) / 3600.0
        out = {"used_pct": round(used, 2), "hours_left": round(h_left, 2), "window": w.get("window")}
        if h_left <= 0 or used >= 100.0:
            return {**out, "needed_pct_h": 0.0, "actual_pct_h": None, "forecast_pct": used, "share": 0.0}
        needed = (100.0 - used) / h_left
        burns = sc.recent_burn_rates() if hasattr(sc, "recent_burn_rates") else {}
        actual = burns.get((API_LANE, w.get("window")))
        if actual is None:
            actual = _window_pace_pct_per_h(w, lane.get("fetched_at"))
            if actual is None:
                return None
            if actual == IMMINENT:
                actual = 0.0                                      # no pace left in the window: only the need counts
        actual = max(0.0, float(actual))
        return {**out, "needed_pct_h": round(needed, 4), "actual_pct_h": round(actual, 4),
                "forecast_pct": round(min(100.0, used + actual * h_left), 1),
                "share": round(needed / (needed + actual), 4)}
    except Exception as exc:                       # never breaks routing; resolve records it in the decision's rules
        return {"error": type(exc).__name__}


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


def codex_login_identity(env: dict[str, str]) -> str | None:
    """The login a Codex CLI started with `env` runs on (scarcity's rule, which also stamps each Codex reading's `login`)."""
    return _scarcity_module().codex_login_identity(env)


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


def subscription_readings() -> dict[str, dict[str, Any]]:
    """Each lane's cached usage reading, window by window, for a remote door that must apply its OWN stop rule (the S1
    outcome loop's 95 % rule via the provider's GET /resources): lane_health only carries the pacing-tightest general
    window and drops freshness, which cannot show a weekly window at 3 % left. Cache-only (refresh=False: never opens a
    vendor CLI). `fetched_at` is the reading's sample time and `failed` scarcity.reading_freshness's "last try failed"
    flag, so the caller judges freshness by its own age limit against the snapshot's generated_at."""
    sc = _scarcity_module()
    out: dict[str, dict[str, Any]] = {}
    for lane, v in (sc.current(refresh=False).get("lanes") or {}).items():
        if not isinstance(v, dict):
            continue
        _, failed = sc.reading_freshness(lane, v)
        out[lane] = {"fetched_at": v.get("fetched_at"), "failed": failed,
                     "windows": [{k: w.get(k) for k in ("window", "pool", "used_pct", "headroom_pct", "reset_epoch", "stale")}
                                 for w in (v.get("windows") or []) if isinstance(w, dict)]}
        if "config_dir" in v:
            # The login the reading was sampled from (scarcity stamps Claude readings): provider/floor.py requires it to
            # match the executing account, and holds when it is absent.
            out[lane]["config_dir"] = v.get("config_dir")
        if "login" in v:
            # The login the sampler's CLI effectively ran on: the floor's identity proof (provider/floor.py).
            out[lane]["login"] = v.get("login")
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


def lane_byte_ceilings(cx: dict[str, Any] | None = None) -> dict[str, int]:
    """Per-lane input byte ceilings (base lane -> bytes) on top of the token ceiling: transports that cap the raw
    request size. Shared by core.resolve and the provider's sub/ pin so the two fit gates cannot drift."""
    cx = _context_cfg() if cx is None else cx
    return {'antigravity': 180000, **(cx.get('ceiling_bytes') or {})}


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


def ceiling_work_applies(cw: dict[str, Any], use_case: str | None, D: int | None) -> bool:
    """selection.ceiling_work holds for this use case at this difficulty: the use case is listed and D reaches its
    min_D (min_D_by_use_case for that use case, else min_D; #143 adds code at D >= 9)."""
    if D is None or use_case not in (cw.get("use_cases") or []):
        return False
    by_uc = cw.get("min_D_by_use_case") if isinstance(cw.get("min_D_by_use_case"), dict) else {}
    return D >= int(by_uc.get(use_case, cw.get("min_D", 5)))


def api_lane_refusal(decision: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Why the API credit lane may not open this conversation (#143), or None. The continuation gate re-checks every
    later turn with routing.local_admission (no outward effect, blast radius 0-1, scorer not degraded, #114), so the
    opening is held to the same rule: a conversation is never opened where its next turn would be refused. Judged on
    S1's raw answers (not resolve's outward floor), exact types only, as local_admission does."""
    if decision.get("degraded") is not False or not isinstance(decision.get("answers"), dict):
        return "API credit lane: a degraded scorer never defaults to Claude"
    ans = decision["answers"]
    if ans.get("outward") is not False:
        return "API credit lane: never for an outward effect"
    br, most = ans.get("blast_radius"), int(cfg.get("max_blast_radius", 1))
    if type(br) is not int or not 0 <= br <= most:
        return f"API credit lane: blast radius above {most} (its continuations would be refused)"
    return None


def split_pick(key: str, weights: list[tuple[str, float]]) -> str | None:
    """#143: one of `weights`' keys, chosen with probability proportional to its weight by a hash of `key` alone, so the
    same conversation key always gets the same answer for the same weights, and a small change of the weights moves only
    the keys that sit near a boundary. Deterministic (sha256, groups in sorted order); None when no weight is positive."""
    import hashlib
    live = sorted((k, float(w)) for k, w in weights if w and w > 0)
    total = sum(w for _, w in live)
    if not live or total <= 0:
        return None
    u = int.from_bytes(hashlib.sha256(str(key).encode()).digest()[:8], "big") / 2.0 ** 64
    acc = 0.0
    for k, w in live:
        acc += w / total
        if u < acc:
            return k
    return live[-1][0]


def _quota_group(c: dict[str, Any]) -> str:
    """The quota a route spends, for the underuse split: its lane and the pool that binds it (Fable on account B is
    bound by B's all-models week today, so it is the same quota as Opus on B and is counted once)."""
    try:
        ps = _pool_size(c)
        return f"{c['lane']}:{ps.get('bound_by') or ps.get('pool') or 'all'}"
    except Exception:
        return f"{c['lane']}:{c.get('pool') or 'all'}"


def _expected_lost_tokens(c: dict[str, Any]) -> float | None:
    """Tokens of this route's quota expected to be lost at its reset: expected loss % × the pool's size per %. None
    when either is unknown (such a pool cannot be weighed against the others)."""
    loss, tpp = c.get("expected_loss_pct"), _pool_tokens_per_pct(c)
    return float(loss) * float(tpp) if loss is not None and tpp else None


def underuse_split(cands: list[dict[str, Any]], key: str, eligible: Callable[[dict[str, Any]], bool],
                   min_loss_pct: float, api_share: float | None = None
                   ) -> tuple[dict[str, Any], dict[str, float]] | None:
    """#143: (the route picked for this conversation, {quota group: expected lost tokens}) when at least two underused
    quota groups take part, else None. `cands` is in preference order (cheapest first); each group is represented by
    its first eligible route, weighted by the quota it is expected to lose at its reset (_expected_lost_tokens). A
    group takes part only when its route is `eligible`, expected to lose at least `min_loss_pct` % and of known size.
    `api_share` (selection.api_pacing on: api_pace's share) paces the API credit grant: when it takes part, its weight
    is set so that it gets exactly that share of the split and the subscription groups share the rest in their own
    proportions (their equalization among themselves is unchanged). None keeps v21's weighting."""
    best: dict[str, dict[str, Any]] = {}
    lost: dict[str, float] = {}
    for c in cands:
        loss = c.get("expected_loss_pct")
        if not eligible(c) or loss is None:
            continue
        if api_share is not None and _base_lane(c["lane"]) == API_LANE:
            # Paced, the grant takes part while it lags its stop line at all (forecast below 100 %): min_loss_pct
            # keeps a subscription's last points as margin for a noisy % and forecast, but the grant's 100 % already
            # IS its margin (the stop line, 95 % of the grant) and every turn is reserved at its upper-bound cost
            # before it is sent. Both together would stop it at 95 % of the stop line ($180.50 of $190).
            if float(loss) <= 0.0:
                continue
        elif float(loss) < min_loss_pct:
            continue
        g = _quota_group(c)
        if g in best:
            continue
        tokens = _expected_lost_tokens(c)
        if not tokens:
            continue
        best[g], lost[g] = c, tokens
    if len(best) < 2:
        return None
    if api_share is not None:
        api = [g for g in best if _base_lane(best[g]["lane"]) == API_LANE]
        others = sum(v for g, v in lost.items() if g not in api)
        if api and others > 0:
            s = min(1.0, max(0.0, float(api_share)))
            for g in api:                          # one grant group in practice; split its share evenly otherwise
                lost[g] = (others * s / (1.0 - s) / len(api)) if s < 1.0 else float("inf")
            if s >= 1.0:                           # absorbed nothing yet: the grant takes the split
                g = api[0] if len(api) == 1 else split_pick(key, [(x, 1.0) for x in api])
                return (best[g], {x: (1.0 if x in api else 0.0) for x in lost})
            if s <= 0.0:
                for g in api:
                    lost.pop(g)
                if len(lost) < 2:
                    return None
    g = split_pick(key, list(lost.items()))
    return (best[g], lost) if g else None


def check_band_window(window: Any) -> tuple[str, str]:
    """A band window (lo, hi) as resolve takes it: two bands of BAND_ORDER, lo not above hi; ValueError otherwise."""
    if (not isinstance(window, (tuple, list)) or len(window) != 2 or any(b not in BAND_ORDER for b in window)
            or BAND_ORDER.index(window[0]) > BAND_ORDER.index(window[1])):
        raise ValueError('Invalid routing band window')
    return window[0], window[1]


def parse_band_window(raw: Any) -> tuple[str, str] | None:
    """A band window as clients write it: `R2` (exactly R2) or `R1-R2` (lo-hi), case-insensitive; `any` is no window
    (None). ValueError for anything else, so a typo never silently routes unwindowed."""
    text = raw.strip().upper() if isinstance(raw, str) else ""
    if text == "ANY":
        return None
    lo, sep, hi = text.partition("-")
    return check_band_window((lo, hi if sep else lo))


def band_window_label(lo: str, hi: str) -> str:
    """`R2` for an exact window, `R1-R2` for a range: the one spelling of a window in rules, logs and headers."""
    return lo if lo == hi else f"{lo}-{hi}"


def band_max_D(t: dict[str, Any], band: str) -> int | None:
    """The highest difficulty a band admits: its own `when` (an `all` holding {"lte": ["D", n]}), else the D of the
    selection knot whose reference is that band; None when the table says neither."""
    row = next((b for b in t.get("bands") or [] if b.get("band") == band), None) or {}
    for cond in (row.get("when") or {}).get("all") or []:
        if isinstance(cond, dict) and isinstance(cond.get("lte"), list) and cond["lte"][:1] == ["D"]:
            return int(cond["lte"][1])
    return next((int(k["D"]) for k in (t.get("selection") or {}).get("knots") or [] if k.get("ref") == band), None)


def resolve(decision: dict[str, Any], unit_kind: str = "interactive", history_chars: int = 0, sticky: dict[str, Any] | None = None,
            escalate: dict[str, Any] | None = None, input_fit: dict[str, Any] | None = None,
            allowed_routes: tuple[tuple[str, str], ...] | None = None,
            execution: bool = False, minimum_band: str | None = None, accounts_aware: bool = False,
            maximum_band: str | None = None, previous: dict[str, Any] | None = None,
            hold_ok: Callable[[dict[str, Any]], bool] | None = None, split_key: str | None = None,
            band_window: tuple[str, str] | None = None) -> dict[str, Any]:
    """`maximum_band` (a client's workload hint, e.g. batch → R1) caps the band from above, mirroring `minimum_band`,
    and caps the difficulty the requirement is computed from at the most that band admits (band_max_D); the hard rules
    still win: a request whose blast radius is 3 (an outward effect is floored there) keeps its band and difficulty,
    and a `minimum_band` that raises the band above the cap undoes it. `previous` maps band → {"lane", "model"} of the
    client's previous route in that band, held under selection.switch_margin_points. `hold_ok` is the host's own
    admission check (e.g. the provider's quota floor): the margin holds a previous route only when it passes, so
    stickiness never picks a route the host would then refuse; the underuse split and ceiling_underuse never take a
    route that fails it either (#143). `split_key` (the conversation's id, #143) seeds selection.underuse_split; None
    keeps the cheapest route. An API credit lane pair in `allowed_routes` (the host opting in, #143) adds that lane's
    Claude candidates (scored_candidates api_lane).
    `band_window` (lo, hi): a trusted client's band window (the provider's x-cortados-band header or a `cortados@<window>`
    model name, e.g. the whole-session launcher's routed main loop at R2). S1's band is clamped into it through the
    same two steps: above `hi` it is capped like the workload cap (difficulty counted at the most `hi` admits), below
    `lo` it is floored like the trusted capability floor. The hard rules still win: blast radius 3 (an outward effect)
    keeps its band over `hi`, a trusted `minimum_band` above `hi` wins, and a degraded scorer keeps its no-Claude rule
    (the window moves only the band). Every step and every override is a `band window …` rule on the routes row."""
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
    capped = False
    s1_band = band                             # S1's own band (or the fallback), before any cap or floor
    if maximum_band is not None:
        if maximum_band not in BAND_ORDER:
            raise ValueError('Invalid maximum routing band')
        if BAND_ORDER.index(band) > BAND_ORDER.index(maximum_band):
            if v is not None and v["blast_radius"] >= 3:
                rules.append(f"workload cap {maximum_band} not applied: blast radius 3 (outward effect) keeps {band}")
            else:
                band, capped = maximum_band, True
                rules.append(f"client workload hint caps band at {band}")
    win = None
    if band_window is not None:
        lo, hi = check_band_window(band_window)
        win = band_window_label(lo, hi)
        natural = band                         # the band the window receives (after a workload cap)
        # The window's max: the workload cap's step (capped → the difficulty is counted at the most `hi` admits below),
        # under the same hard rule: blast radius 3 (an outward effect is floored there) keeps its band.
        if BAND_ORDER.index(band) > BAND_ORDER.index(hi):
            if v is not None and v["blast_radius"] >= 3:
                rules.append(f"band window {win} overridden: blast radius 3 (outward effect) keeps {band}")
            else:
                band, capped = hi, True
                rules.append(f"band window {win}: band {natural} lowered to {band}")
        # The window's min: a floor, as the trusted capability floor. A workload cap it lifts the band over still caps
        # the difficulty while the band stays below S1's own (the window's max still holds).
        if BAND_ORDER.index(band) < BAND_ORDER.index(lo):
            before, band = band, lo
            rules.append(f"band window {win}: band {before} raised to {band}")
            capped = capped and BAND_ORDER.index(band) < BAND_ORDER.index(s1_band)
        if band == natural:
            rules.append(f"band window {win}: band {band} within the window")
    if minimum_band is not None:
        if minimum_band not in BAND_ORDER:
            raise ValueError('Invalid minimum routing band')
        if BAND_ORDER.index(band) < BAND_ORDER.index(minimum_band):
            band = minimum_band
            rules.append(f'trusted capability requirement floors band at {band}')
            if win is not None and BAND_ORDER.index(band) > BAND_ORDER.index(band_window[1]):
                rules.append(f"band window {win} overridden: the trusted capability requirement floors band at {band}")
            if capped:
                capped = False                 # the trusted floor wins over the client's cap: the requirement is uncapped
                if maximum_band is not None:
                    rules.append(f"workload cap {maximum_band} not applied: the trusted capability requirement wins")
    if capped and D is not None:
        # #132 review round 1: the cap lowers the effective requirement, not only the label. The difficulty counted for
        # the required index and for the frontier rules (ceiling_work, ceiling_underuse) is capped at the most the
        # capped band admits, so a capped call is selected exactly like a natural request of that band.
        d_max = band_max_D(t, band)
        if d_max is not None and D > d_max:
            rules.append(f"{'band window' if win is not None and maximum_band is None else 'workload cap'}: "
                         f"difficulty {D} counted as {d_max}, the most {band} admits (required index and frontier rules)")
            D = d_max
            v["D"] = D
    row = next(b for b in t["bands"] if b["band"] == band)
    # candidates: table order, work-kind preference first; then subscription scarcity re-ranks them (never below the band):
    #   spent / refusing lane → skipped · lane burning faster than its window → behind the others · lane whose headroom expires soon → first
    health, pol = lane_health(), _policy()
    demote, expiring = static_thresholds(pol)
    pref = (t.get("work_kind_preference") or {}).get(ans.get("work_kind") or "", [])
    # #143: the API credit lane is offered only by a host that lists its pairs (it must be able to run and meter it).
    api_lane = isinstance(allowed_routes, tuple) and any(isinstance(p, tuple) and p[:1] == (API_LANE,)
                                                         for p in allowed_routes)
    scored = scored_candidates(t, band, ans.get("work_kind"), accounts_aware, **({"api_lane": True} if api_lane else {}))
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
    api_cfg = (t.get("selection") or {}).get("api_credit_lane") or {}
    for c in cands:
        if c["lane"] == API_LANE:
            why = api_lane_refusal(decision, api_cfg)
            if why:
                ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": why}
    # Fit is a hard eligibility gate, before quality fallback or scarcity ranks
    # candidates, including unscored/degraded selection. Never rescue a route
    # which would lose input just because every candidate failed the gate.
    fit = input_fit or {}
    need = (int(fit['input_tokens']) if execution and fit.get('assembled') is True
            else max(turn_tokens(history_chars), int(fit.get('input_tokens') or 0)))
    cx = _context_cfg()
    byte_caps = lane_byte_ceilings(cx)
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
        # Scarcity v2 §9 step 4 (selection.api_pacing, table v22; owner 2026-11-24): the API credit grant is ranked
        # like any other pool, by its percent used against its pace to expiry (api_pace). It keeps the same models,
        # bands, gates and cost terms as a subscription; its v21 "free until it lapses" weight discount is gone, and
        # its share of an underuse split is its pace share instead of its expected lost tokens (unit-mismatched:
        # list-price tokens of a $190 grant against subscription ledger tokens). Off: v21 exactly.
        pacing = api_pacing_cfg(t) if any(c["lane"] == API_LANE for c in cands) else None
        pace = api_pace() if pacing is not None else None
        if pacing is not None and (pace is None or "error" in pace):
            # No paced share without a fresh grant reading (its expected loss is unknown too, so it takes no part in a
            # split); said here, so a decision never hides why the grant was left out.
            rules.append("api pacing: " + (f"failed ({pace['error']})" if pace else "no fresh grant reading") +
                         "; the grant takes no paced share")
            pace = None
        api_share = pace["share"] if pace is not None else None
        if pace is not None:
            # The grant's scarcity is its pace to expiry, not window_view's linear-life pace: window_view calls a
            # refilling window comfortable at 70 % projected use, but the grant never refills and its 100 % is already
            # the stop line, so it is underused exactly while it is forecast to end below that line:
            # −(100 − forecast) / 100, in [−1, 0]. Only the ranking signal changes; a spent or refusing grant stays
            # unavailable (ok), and its expected loss is the same 100 − forecast (_expected_loss_pct).
            # Past the stop line or the last counting hour the grant takes nothing (#114's money rules, fail closed in
            # the ranking too: the provider also stops offering the lane and api_credits.reserve refuses the turn).
            done = pace["used_pct"] >= 100.0 or pace["hours_left"] <= 0
            for c in cands:
                if c["lane"] != API_LANE:
                    continue
                if done:
                    ch_of[id(c)] = {**ch_of[id(c)], "ok": False,
                                    "why": "API credit grant past its stop line or its last counting hour"}
                elif ch_of[id(c)].get("scarcity") is not None:
                    ch_of[id(c)] = {**ch_of[id(c)], "scarcity": round(-(100.0 - pace["forecast_pct"]) / 100.0, 2)}
            if done:
                rules.append(f"api pacing: grant {pace['used_pct']:g}% of its stop line used, {pace['hours_left']:g} h "
                             "to its last counting hour: it takes no work")
            else:
                rules.append(f"api pacing: grant {pace['used_pct']:g}% of its stop line used, {pace['hours_left']:g} h "
                             f"to expiry; needs {pace['needed_pct_h']:g}%/h, absorbing {pace['actual_pct_h']:g}%/h "
                             f"(forecast {pace['forecast_pct']:g}% at expiry) -> {100 * pace['share']:.0f}% of a split")
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
        # #143: the API credit lane's burn is never the normalising reference: its pool is a $190 grant, ~20× smaller
        # than a subscription week in tokens, so it would shrink every subscription's weight; its own weight is capped
        # at 100 (the heaviest subscription route) before its use-it-or-lose-it discount below.
        burn_ref = max([x["burn"] for x in cands if x.get("burn") and x["lane"] != API_LANE] + [0.0])
        price_ref = max([x["price"] for x in cands if x.get("price") and not x.get("ceiling")] + [1.0])
        for c in cands:
            if c.get("burn") is not None and burn_ref and c["lane"] == API_LANE:
                c["weight"] = round(min(100.0, 100.0 * c["burn"] / burn_ref), 1)
            elif c.get("burn") is not None and burn_ref:
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
            # Underuse credit (#132, owner 2026-11-22): the capped scarcity sits at −1 for every comfortably underused
            # pool, so it cannot tell a pool that will lose 90 % at its reset from one that will lose 50 %. When the
            # pool's reset times are known, the credit is the share expected to be LOST at that reset (headroom minus
            # the use expected until then, _expected_loss_pct); without them it stays −scarcity, as before.
            loss = _expected_loss_pct(c) if sc < 0 and ch_of[id(c)].get("scarcity") is not None else None
            if loss is not None:
                c["expected_loss_pct"] = loss
            if c["lane"] == API_LANE and loss is not None and pacing is None:
                # #143 (owner 2026-11-23): the grant's dollars are "free until the grant lapses". Only the share of
                # this spend the grant would NOT otherwise lose costs anything, so the weight is scaled by
                # (1 − expected loss): ~0 while the forecast says most of the grant lapses unspent, rising as it is
                # spent faster, falling again as the expiry nears with credit left. Unknown loss: no discount.
                c["weight"] = round(c["weight"] * (1.0 - min(100.0, max(0.0, float(loss))) / 100.0), 1)
            under = loss / 100.0 if loss is not None else max(-sc, 0.0)
            c["cost"] = round(c["weight"] + sel.get("scarcity_points", 30) * max(sc, 0.0)
                              - sel.get("expiring_credit_points", 80) * under - expiring_soon, 1)
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
            # #143: the API credit lane is Anthropic's own API, not another vendor's copy: never a secondary source.
            c["secondary"] = bool(native) and _base_lane(c["lane"]) != native and c["lane"] != API_LANE
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

        def admits(c: dict[str, Any]) -> bool:
            # the host's own admission check (the client's quota floor): a rule that moves a route to the front may
            # only move one the host would then run (#132 for the margin, #143 for the split and the frontier rules)
            if hold_ok is None:
                return True
            try:
                return bool(hold_ok(c))
            except Exception:                      # an admission check that cannot answer admits nothing
                return False
        cw = sel.get("ceiling_work") or {}
        if cw.get("require_frontier") and ceiling_work_applies(cw, scored[0]["use_case"], D):
            # #143: hard planning/research/code needs a frontier (ceiling) model, not merely the R4 reference index,
            # which gpt-6.1-sol clears. Only while a frontier route is available, qualifies and passes the host's check:
            # a spent frontier never blocks routing (the index rule alone applies then).
            # Review round 1 (gpt-6.1-sol): the rule enforces only the ADMISSIBLE frontier routes. A frontier route the
            # host refuses (its quota floor) is withdrawn with the non-frontier ones; otherwise a cheaper refused one
            # (Claude B Opus under the floor) was chosen and the host answered quota_blocked although Astra was free.
            admissible = [c for c in cands if c.get("ceiling") and ok_of(c) and c["index"] >= target and admits(c)]
            if admissible:
                keep_ids = {id(c) for c in admissible}
                for c in cands:
                    if id(c) in keep_ids or not ok_of(c):
                        continue
                    if not c.get("ceiling"):
                        why = f"ceiling_work: {scored[0]['use_case']} at D={D} needs a frontier model"
                    elif c["index"] >= target:
                        why = "ceiling_work: frontier route fails the host's admission check"
                    else:
                        continue                   # a frontier route below the target: sorted after the qualifying ones
                    ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": why}
                rules.append(f"ceiling_work: {scored[0]['use_case']} at D={D} needs a frontier (ceiling) model")
            else:
                rules.append(f"ceiling_work: no frontier model available for {scored[0]['use_case']} at D={D}; "
                             "the index rule alone applies")
        keep = []
        for c in sorted(cands, key=lambda c: -c["index"]):          # same lane+pool and same price class (±25 % when priced) → keep the strongest only
            same = lambda k: (abs(k["price"] - c["price"]) <= 0.25 * max(k["price"], c["price"]) if k.get("price") and c.get("price")
                              else k["general"] - c["general"] <= sel.get("same_price_band", 20))
            if not any(k["lane"] == c["lane"] and k.get("pool") == c.get("pool") and same(k)
                       and (ok_of(k) or not ok_of(c)) for k in keep):
                keep.append(c)
        cands = keep
        cands.sort(key=lambda c: (0 if c["index"] >= target else 1, c["cost"], -(c.get("tokens_per_s") or 0), tie.index(c["lane"]) if c["lane"] in tie else len(tie), base[id(c)]))
        # underuse split (#143, selection.underuse_split): when the cheapest qualifying route is on a pool expected to
        # lose quota at its reset, the underused pools with a qualifying route share the work in proportion to the quota
        # each would lose, picked by the conversation key (deterministic), instead of the largest credit taking all.
        us = sel.get("underuse_split") or {}
        split = None
        if split_key and us.get("enabled"):
            qualifies = lambda c: ok_of(c) and c["index"] >= target and admits(c)
            first = next((c for c in cands if qualifies(c)), None)
            min_loss = float(us.get("min_loss_pct", 0) or 0)
            if first is not None and first.get("expected_loss_pct") is not None and float(first["expected_loss_pct"]) >= min_loss:
                split = underuse_split(cands, split_key, qualifies, min_loss, api_share)
            if split:
                pick, lost = split
                total = sum(lost.values())
                cands.remove(pick); cands.insert(0, pick)
                rules.append(f"underuse split: {pick['lane']}/{pick['model']} for this conversation; pools share it by the "
                             "quota each would lose at its reset (" + ", ".join(
                                 f"{g} {100.0 * v / total:.0f}%" for g, v in sorted(lost.items())) + ")")
        # switching margin (#132): costs move by a point or two with every usage reading, which flipped R2 between Opus
        # and Sol call by call. The client's previous route for this band (`previous`, kept by the host) stays first
        # while it still qualifies, is available and costs at most switch_margin_points more than the cheapest; with
        # no previous route among them, the cheapest stands (the sort above is deterministic). Not after an underuse
        # split (#143): the margin is for cost noise, and the split picks by the conversation key on purpose; holding the
        # client's previous route would undo the split for every route within the margin.
        margin = float(sel.get("switch_margin_points", 0) or 0)
        prev = (previous or {}).get(band) if isinstance(previous, dict) else None
        if margin > 0 and not split and isinstance(prev, dict) and prev.get("model"):
            best = next((c for c in cands if ok_of(c) and c["index"] >= target), None)
            held = next((c for c in cands if c["model"] == prev["model"] and c["lane"] == prev.get("lane")
                         and ok_of(c) and c["index"] >= target), None)
            if held is not None and held is not best and hold_ok is not None:
                try:
                    admitted = bool(hold_ok(held))
                except Exception:                  # an admission check that cannot answer never lets the margin hold
                    admitted = False
                if not admitted:
                    rules.append(f"switching margin: previous {band} route {held['lane']}/{held['model']} not held "
                                 "(fails the host's admission check)")
                    held = None
            if best is not None and held is not None and held is not best and held["cost"] - best["cost"] <= margin:
                cands.remove(held); cands.insert(0, held)
                rules.append(f"switching margin: stays on the client's previous {band} route {held['lane']}/{held['model']} "
                             f"(cost {held['cost']} vs {best['cost']}, within {margin:g})")
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
            # #143: never a route the host's admission check (the client's quota floor) refuses.
            spare = [c for c in cands if c.get("ceiling") and ok_of(c) and sc_of(c) <= float(cu.get("attract_when_scarcity_below", -0.3))
                     and admits(c)]
            if spare:
                # #132 (owner 2026-11-22): rank the underused pools by the share expected to be LOST at each one's reset
                # (cost breaks ties); the capped scarcity cannot tell pools near −1 apart. Any spare pool without reset
                # times → today's ranking by cost for all of them. #143: with a conversation key the spare pools share
                # the work in proportion to the quota each would lose (underuse_split, cheapest route per pool).
                shared = None
                if split_key and us.get("enabled") and all(c.get("expected_loss_pct") is not None for c in spare):
                    in_spare = {id(c) for c in spare}
                    shared = underuse_split(sorted(spare, key=lambda c: c["cost"]), split_key,
                                            lambda c: id(c) in in_spare, float(us.get("min_loss_pct", 0) or 0),
                                            api_share)
                if shared:
                    top, lost = shared
                    total = sum(lost.values())
                    why = ("spare frontier pools share it by the quota each would lose: " +
                           ", ".join(f"{g} {100.0 * v / total:.0f}%" for g, v in sorted(lost.items())))
                elif all(c.get("expected_loss_pct") is not None for c in spare):
                    top = min(spare, key=lambda c: (-c["expected_loss_pct"], c["cost"]))
                    why = f"about {top['expected_loss_pct']:g}% of its pool would be lost at the reset"
                else:
                    top = min(spare, key=lambda c: c["cost"])
                    why = f"scarcity {sc_of(top)}"
                cands.remove(top); cands.insert(0, top)
                rules.append(f"{top['lane']}/{top['model']} takes it: its pool is underused ({why}), paid-for frontier capacity is used rather than lost")
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
            "alternatives": [{k: c.get(k) for k in ("lane", "model", "index", "cost", "weight", "expected_loss_pct")}
                             for c in usable[1:6]] if scored else None,
            "chosen_window": (ch_of[id(chosen)].get("tightest") if chosen else None), "chosen_pool": (ch_of[id(chosen)].get("pool") if chosen else None)}


# ------------------------------------------------------------------ log
_db_lock = threading.Lock()
DDL = """CREATE TABLE IF NOT EXISTS routes (
  turn_id TEXT PRIMARY KEY, ts REAL, session_id TEXT, platform TEXT, unit_kind TEXT, actual_model TEXT,
  request TEXT, request_chars INTEGER, history_msgs INTEGER,
  engine TEXT, engine_ms INTEGER, degraded INTEGER, engine_error TEXT, answers TEXT, features TEXT, D INTEGER,
  band TEXT, orchestrator TEXT, lane TEXT, provider TEXT, model TEXT, runner_up TEXT, skipped TEXT,
  subagents INTEGER, verifier INTEGER, hard_rules TEXT, table_version INTEGER, table_error TEXT,
  response_chars INTEGER, turn_ms INTEGER, forest_correction TEXT);
-- GET /v1/cortados/usage counts rows by ts window on every collect: without this index each count is a full scan
-- holding a SHARED lock that delays log_route's commit. IF NOT EXISTS: existing databases gain it on the next open.
CREATE INDEX IF NOT EXISTS routes_ts ON routes(ts);"""


# Tokens a routed conversation spent, summed over its backend turns (log_tokens), and the x-cortados-session tag of the
# orchestrator whose work it was. provider/work_split.py and provider/tasks.py read them; NULL = not recorded.
# The per-session work split (#127) adds three provider-observed keys, each the first one seen on the conversation:
# office_origin (x-cortados-origin: a child task's orchestrator, which a child's own x-cortados-session is not),
# office_run (x-cortados-run-id: the child task) and office_via (x-cortados-via: "route_task" for a route_task call).
TOKEN_COLUMNS = (("tok_input", "INTEGER"), ("tok_output", "INTEGER"), ("tok_cache_read", "INTEGER"),
                 ("tok_cache_write", "INTEGER"), ("tok_turns", "INTEGER"), ("office_session", "TEXT"),
                 ("office_origin", "TEXT"), ("office_run", "TEXT"), ("office_via", "TEXT"))
# #124: a request a sensitivity detector marked, answered in the cloud because the Pro could not take it. JSON of
# provider/sensitive_notice.record (detector, reason, notice shown or opted out: fixed categories, never text); NULL for
# every other route. Additive and nullable like the token columns.
# #132: the client's workload hint (x-cortados-workload: "batch"), NULL when none was sent. Metadata only.
# The client's band window (x-cortados-band, or the routed main loop's `cortados@<window>` model) as band_window_label
# writes it, NULL when none was given. Metadata only: the hard_rules column says what it did.
EXTRA_COLUMNS = TOKEN_COLUMNS + (("sensitive_cloud", "TEXT"), ("workload", "TEXT"), ("band_window", "TEXT"))


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
    # Work split (tokens per routed conversation, joined to the orchestrator's Office session tag): additive, nullable
    # columns only, so an old reader never sees a change and a NULL means "not recorded" (rows from before this). Two
    # processes opening an old db at once can both try the same ALTER: the loser's "duplicate column" is the outcome
    # it wanted, so only that error is passed over.
    for col, kind in EXTRA_COLUMNS:
        if col not in cols:
            try:
                c.execute(f"ALTER TABLE routes ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
    try:
        os.chmod(STATE / "router.db", 0o600)
    except OSError:
        pass
    return c


ROUTE_LOG_RETRY_S = 0.05     # one brief retry: the lock errors seen in #99 were transient


def _write_routes(op: str, write: Callable[[sqlite3.Connection], None], *, raise_final: bool) -> bool:
    """Run one router.db write (open, write, commit) with one retry after ROUTE_LOG_RETRY_S. Every failed attempt is
    counted in router_db (published by the usage and live views). After the retry fails: raise when raise_final, else
    return False. The retry waits outside _db_lock so other writers are not held up."""
    for attempt in (1, 2):
        try:
            with _db_lock:
                c = _db()
                try:
                    write(c)
                    c.commit()
                finally:
                    c.close()
        except sqlite3.Error as exc:
            router_db.note_error(op, exc, final=attempt == 2)
            if attempt == 2:
                if raise_final:
                    raise
                return False
            time.sleep(ROUTE_LOG_RETRY_S)
        else:
            if attempt == 2:
                router_db.note_recovered(op)
            return True
    return False   # unreachable


def log_route(turn_id: str, meta: dict[str, Any], decision: dict[str, Any], r: dict[str, Any]) -> bool:
    """Log one routing decision. Never raises sqlite3.Error: a failure to LOG a route must not fail the routed request
    (#99). Returns False when the row could not be written after one retry; the failure is counted in router_db."""
    ch = r.get("chosen") or {}
    row = (turn_id, time.time(), meta.get("session_id"), meta.get("platform"), meta.get("unit_kind"), meta.get("actual_model"),
           meta.get("request"), len(meta.get("request") or ""), meta.get("history_msgs"), decision["engine"], decision["engine_ms"],
           int(decision["degraded"]), decision.get("error"), json.dumps(r.get("answers")), json.dumps(r.get("features")), r.get("D"),
           r["band"], r.get("orchestrator"), ch.get("lane"), ch.get("provider"), ch.get("model"), json.dumps(r.get("runner_up")),
           json.dumps(r.get("skipped")), r["subagents"], int(r["verifier"]), json.dumps(r["hard_rules"]), r.get("table_version"), r.get("table_error"), json.dumps(r.get("scarcity")), meta.get("context"))
    task_context = json.dumps(meta['task_context'], ensure_ascii=False) if meta.get('task_context') is not None else None

    def write(c: sqlite3.Connection) -> None:
        c.execute("INSERT OR REPLACE INTO routes (turn_id, ts, session_id, platform, unit_kind, actual_model, request, request_chars, history_msgs, "
                  "engine, engine_ms, degraded, engine_error, answers, features, D, band, orchestrator, lane, provider, model, runner_up, skipped, "
                  "subagents, verifier, hard_rules, table_version, table_error, scarcity, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  row)
        c.execute('UPDATE routes SET task_context=? WHERE turn_id=?', (task_context, turn_id))
        if meta.get("workload"):
            c.execute('UPDATE routes SET workload=? WHERE turn_id=?', (str(meta["workload"])[:32], turn_id))
        if meta.get("band_window"):
            c.execute('UPDATE routes SET band_window=? WHERE turn_id=?', (str(meta["band_window"])[:8], turn_id))
    return _write_routes("log_route", write, raise_final=False)


def log_execution(route_id: str, provider: str, model: str) -> None:
    """Only a completed provider response is evidence of actual execution. Raises sqlite3.Error after one retry (the
    provider retries it on the conversation's next turn); every failed attempt is counted in router_db."""
    _write_routes("log_execution", lambda c: c.execute("UPDATE routes SET actual_model=? WHERE turn_id=?",
                                                       (f"{provider}/{model}", route_id)), raise_final=True)


def log_outcome(turn_id: str, response_chars: int, turn_ms: int) -> None:
    _write_routes("log_outcome", lambda c: c.execute("UPDATE routes SET response_chars=?, turn_ms=? WHERE turn_id=?",
                                                     (response_chars, turn_ms, turn_id)), raise_final=True)


def log_sensitive_cloud(turn_id: str, record: dict[str, Any]) -> bool:
    """Mark a route's row as a sensitive request answered in the cloud (#124). `record` is sensitive_notice.record's
    categories; only its three known keys are written, as strings. Never raises sqlite3.Error (a lost write is counted
    in router_db, #99): the reply was already good. Returns False when it was not written."""
    rec = {k: str(record.get(k)) for k in ("detector", "reason", "notice") if record.get(k) is not None}
    return _write_routes("log_sensitive_cloud", lambda c: c.execute(
        "UPDATE routes SET sensitive_cloud=? WHERE turn_id=?", (json.dumps(rec, sort_keys=True), turn_id)),
        raise_final=False)


def log_tokens(turn_id: str, usage: dict[str, Any], office_session: str | None = None, *,
               office_origin: str | None = None, office_run: str | None = None, office_via: str | None = None) -> bool:
    """Add one backend turn's tokens to its route's row (Messages-shaped usage: input_tokens excludes the cache reads
    and writes, which come as cache_read_input_tokens / cache_creation_input_tokens; both bridges report that shape)
    and remember the first orchestrator session tag seen, and the first origin, run id and via (TOKEN_COLUMNS); the
    caller (the provider) has validated each. Never raises sqlite3.Error: accounting must not fail a reply;
    a lost write is counted in router_db like a lost route (#99). Returns False when it was not written, including when
    no routes row has this turn_id (the tokens have nowhere to go: counted in router_db as a lost write too)."""
    def n(k: str) -> int:
        v = usage.get(k) if isinstance(usage, dict) else None
        return int(v) if type(v) in (int, float) and v == v and 0 <= v < 1e12 else 0
    vals = (n("input_tokens"), n("output_tokens"), n("cache_read_input_tokens"), n("cache_creation_input_tokens"))
    matched = [0]

    def write(c: sqlite3.Connection) -> None:
        matched[0] = c.execute(
            "UPDATE routes SET tok_input=COALESCE(tok_input,0)+?, tok_output=COALESCE(tok_output,0)+?, "
            "tok_cache_read=COALESCE(tok_cache_read,0)+?, tok_cache_write=COALESCE(tok_cache_write,0)+?, "
            "tok_turns=COALESCE(tok_turns,0)+1, office_session=COALESCE(office_session,?), "
            "office_origin=COALESCE(office_origin,?), office_run=COALESCE(office_run,?), office_via=COALESCE(office_via,?) "
            "WHERE turn_id=?",
            (*vals, office_session, office_origin, office_run, office_via, turn_id)).rowcount
    if not _write_routes("log_tokens", write, raise_final=False):
        return False
    if matched[0] == 0:                             # e.g. the route's own log_route was lost: no row to add to
        router_db.note_error("log_tokens", LookupError("no routes row for this turn"), final=True)
        return False
    return True


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
        conn = router_db.reader(path, timeout=5)   # not mode=ro: this process may also write router.db (#99)
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
