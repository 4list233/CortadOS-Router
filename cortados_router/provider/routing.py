"""Model choice for a provider conversation: S1 scores the request once, the router's execution policy picks the route.

Owner of two rules:
  SERVED_PROVIDERS  which routes this endpoint can serve. Choosing among them stays with execution_routing.select,
                    the router's one execution policy (S1 → band → cheapest capable route with live capacity), so the
                    endpoint is a host of the router, not a second router.
  no Claude when degraded  core.resolve's hard rule that a degraded scorer never defaults to Claude. Execution mode
                    skips the table's non-Claude fallback routes, so this host enforces the rule itself: while S1 is
                    down a Claude route is not offered; the conversation runs on a Codex lane, and when no Codex
                    lane survives (binary missing, capacity spent) the request is refused.
  sub/ pin          a client that names `sub/<claude model>` chose the model itself: S1 and the band are skipped, but
                    the served-model, account, capacity and route-log rules still apply (choose_pinned).
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from typing import Any, Callable

from cortados_router import execution_routing
from cortados_router.provider import anthropic_wire as wire

# The Claude CLI (P1, bridge.py) and the Codex CLI (P2, codex_bridge.py). Local models need the 2026-09-28 "no local
# routes" ruling lifted and local entries in the generated model scores. `openai-codex` is deliberately absent: it is
# the Hermes native-API provider this endpoint cannot run; its rows are served as their `codex-cli` twin instead.
SERVED_PROVIDERS = ("claude-cli", "codex-cli")
CLAUDE_PROVIDERS = ("claude-cli",)
# The tables carry no codex-cli rows: core.resolve clones each openai-codex candidate into a codex-cli one under
# execution, but only after the allowed_routes filter, so the allowlist itself must name the clone.
CLONED_PROVIDERS = {"openai-codex": "codex-cli"}
# Coding clients are interactive sessions: the router's face floor (selection.interactive_min_general_index) applies.
PLATFORM = "cli"
# Client-injected context, not the request: Claude Code wraps its reminders and environment in these.
_INJECTED = re.compile(r"<(system-reminder|system-message)>.*?</\1>", re.S)


def _base_provider(p: str) -> str:
    """The backend a per-account provider maps to: `claude-cli#b` -> `claude-cli`."""
    return (p or "").split("#", 1)[0]


def served_routes(core) -> tuple[tuple[str, str], ...]:
    rows = [r for m in (core.model_scores() or {}).get("models", []) for r in m.get("reach", [])]
    rows += [r for band in core.table()["candidates"].values() for r in band]
    pairs = {(CLONED_PROVIDERS.get(r["provider"], r["provider"]), r["model"]) for r in rows}
    served = {p for p in pairs if p[0] in SERVED_PROVIDERS}
    # Expand each Claude pair across configured accounts (claude-cli, claude-cli#b, …) so a second-account candidate
    # survives the allowlist filter in execution_routing.select; one account leaves this unchanged. A broken accounts
    # config refuses Claude (drops its pairs) rather than serving it on an ambient login.
    try:
        accts = core._vendor_accounts("claude", "claude", "claude-cli")
    except core.AccountsConfigError:
        return tuple(sorted(p for p in served if _base_provider(p[0]) != "claude-cli"))
    if len(accts) > 1:
        served |= {(a["provider"], model) for (prov, model) in set(served) if prov == "claude-cli" for a in accts}
    return tuple(sorted(served))


def strip_injected(text: str) -> str:
    """A user message without the client's injected reminders: what S1 scores (also used by the S1 outcome miner)."""
    return _INJECTED.sub("", text).strip()


def request_text(messages: list[dict[str, Any]], *, standalone: bool = False) -> str:
    """What S1 scores, without the client's injected context: the conversation's first user message (the task the
    session is for), or for a standalone tool-less call (a title or summary) its own last user message."""
    users = [m for m in messages if m.get("role") == "user"]
    pick = (users[-1] if standalone else users[0]) if users else {}
    text = "\n".join(b.get("text", "") for b in wire._blocks(pick.get("content")) if b.get("type") == "text")
    return strip_injected(text)


class _NoClaudeWhenDegraded:
    """`core` for execution_routing.select, except that a degraded decision cannot resolve to a Claude route."""

    def __init__(self, core):
        self._core = core

    def __getattr__(self, name):
        return getattr(self._core, name)

    def resolve(self, decision, *args, allowed_routes=None, **kw):
        if decision.get("degraded") or not decision.get("answers"):
            allowed_routes = tuple(p for p in (allowed_routes or ()) if _base_provider(p[0]) not in CLAUDE_PROVIDERS)
        return self._core.resolve(decision, *args, allowed_routes=allowed_routes, **kw)


def _credentials(claude_bin: str, codex_bin: str) -> Callable[..., None]:
    # A PATH check only: a real login check would open the vendor CLI (and for Codex spend a login refresh), which
    # the scarcity sampler alone may do. A missing login surfaces as a 502 on the first turn instead.
    binaries = {"claude-cli": claude_bin, "codex-cli": codex_bin}

    def check(*, requested: str, target_model: str) -> None:
        base = _base_provider(requested)        # a per-account provider (claude-cli#b) shares the base binary/login check
        if base not in SERVED_PROVIDERS:
            raise execution_routing.CredentialsUnavailable(f"{requested} is not served by this endpoint")
        if not shutil.which(binaries[base]):
            raise execution_routing.CredentialsUnavailable(f"{binaries[base]} is not on PATH")
    return check


def request_fit(body: dict[str, Any]) -> dict[str, Any]:
    """The input-fit estimate for a whole client request (core.resolve's `input_fit`, assembled: the client sends the
    full conversation every turn). Used by both the S1 path and the sub/ pin so the two gate on the same number."""
    raw = json.dumps(body)
    return {"known": True, "assembled": True, "input_tokens": len(raw) // 4, "input_bytes": len(raw.encode()),
            "source": "provider_request_estimate"}         # chars / 4; the CLI's own tokenizer is not reachable here


SUB_PREFIX = "sub/"
SUB_DEFAULT = "claude-opus-5-5"
# Short names a person types (`claude --model sub/opus`); each must name a model the tables serve on claude-cli.
SUB_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001",
               "fable": "claude-fable-5-1"}


def sub_pin(body: dict[str, Any]) -> str | None:
    """A request pinned to a Claude subscription model: model `sub/<claude-model or alias>` (bare `sub` for the
    default). Returns the requested model name (unvalidated), or None for a normal S1-routed request."""
    m = (body.get("model") or "").strip()
    if m == "sub":
        return SUB_DEFAULT
    if m.startswith(SUB_PREFIX):
        name = m[len(SUB_PREFIX):].strip()
        return SUB_ALIASES.get(name, name) or SUB_DEFAULT
    return None


def normalize_model(name: str | None) -> str | None:
    """A client-named model as the tables' id: `claude-opus-5-5[1m]`, `sub/opus` and `opus` all name claude-opus-5-5;
    bare `sub` is SUB_DEFAULT (as sub_pin). None for empty or `cortados` (no model named: route freely). "" for a lane
    prefix with no model after it (bare `api`, `sub/`, `api/`): it names a lane, not a model, so choose refuses it as
    an exclusion rather than treating it as absent."""
    m = (name or "").strip().casefold()              # model ids are lower-case; `Opus` and `CLAUDE-OPUS-5-5` name the same model
    if m.endswith("]") and "[" in m:                 # Claude Code's context-window suffix, e.g. `[1m]`
        m = m[:m.rindex("[")].strip()
    if m in ("", "cortados"):
        return None
    if m == "sub":
        return SUB_DEFAULT
    if m == "api":
        return ""
    for prefix in (SUB_PREFIX, "api/"):
        if m.startswith(prefix):
            m = m[len(prefix):].strip()
            if not m:
                return ""
            break
    # Claude Code's /model menu names: `opusplan` runs Opus (it plans on Opus), `default` is the account default. A
    # builder reporting its model as one of these must still match, or the review would not be kept off it.
    return {**SUB_ALIASES, "opusplan": SUB_ALIASES["opus"], "default": SUB_DEFAULT}.get(m, m)


def model_key(name: str | None) -> str | None:
    """A model id with a trailing `-YYYYMMDD` release date dropped, so `claude-haiku-4-5` and the served
    `claude-haiku-4-5-20251001` compare equal (exclusion and pin checks match on this key)."""
    return re.sub(r"-\d{8}$", "", name.casefold()) if name else name


def choose_pinned(core, body: dict[str, Any], model: str, *, client: str, conversation_id: str,
                  claude_bin: str = "claude") -> dict[str, Any]:
    """Route a `sub/`-pinned conversation: the person chose the model, so S1 is not asked and the band is not applied
    (a pin that the band would refuse still runs; that is what pinning is for). What stays is the router's accounting
    and safety: only a model the tables serve on claude-cli, only a configured account whose subscription is not spent
    (ranked by core's static account ranking: reserves spared, expiring credit first, scarce last), a request that
    fits the account's context ceiling, the binary on PATH, and a routes row so the usage is on record (engine
    `pin`, no answers: S1's drift check skips it, and the dataset only harvests the request text for labelling).
    Claude only for now: the Codex lane is paused (Forest, 2026-10-30)."""
    # Accounts first: served_routes drops every Claude pair on a broken config, which would misreport it as an unknown model.
    try:
        accounts = core._vendor_accounts("claude", "claude", "claude-cli")
    except core.AccountsConfigError as exc:         # broken accounts config refuses Claude, never an ambient login
        return {"router": execution_routing.ROUTER, "error": f"sub/{model}: {exc}"}
    claude = sorted({m for p, m in served_routes(core) if _base_provider(p) == "claude-cli"})
    if model not in claude:
        return {"router": execution_routing.ROUTER, "client_error": True,
                "error": f"sub/{model} is not a Claude model this endpoint serves; pin one of: {', '.join(claude)}"}
    try:
        _credentials(claude_bin, "codex")(requested="claude-cli", target_model=model)
    except execution_routing.CredentialsUnavailable as exc:
        return {"router": execution_routing.ROUTER, "error": f"sub/{model}: {exc}"}
    # The same input-fit gate core.resolve applies in execution mode: an assembled request's estimate must fit the
    # chosen lane's ceiling, or the CLI would truncate or reject the conversation after quota was committed to it.
    fit = request_fit(body)
    need = int(fit["input_tokens"])
    health = core.lane_health()
    cands = [{"lane": a["lane"], "provider": a["provider"], "model": model, "account": a["account"],
              "config_dir": a["config_dir"], "reserve": a["reserve"]} for a in accounts]
    ch_of = {id(c): core.candidate_health(c, health) for c in cands}
    byte_caps = core.lane_byte_ceilings()
    too_big = []
    for c in cands:
        cap = core.lane_ceiling(c["lane"], model)
        bcap = byte_caps.get(core._base_lane(c["lane"]))
        why = None
        if need > cap:
            why = f"turn ≈{need // 1000}k tokens exceeds {c['lane']} ceiling {cap // 1000}k"
        elif bcap is not None and int(fit["input_bytes"]) > int(bcap):   # resolve's byte gate, same table
            why = f"input estimate {fit['input_bytes']} bytes exceeds {c['lane']} byte ceiling {bcap}"
        if why:
            ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": why}
            too_big.append(c)
    if cands and len(too_big) == len(cands):
        return {"router": execution_routing.ROUTER, "fit_blocked": True,
                "error": f"sub/{model}: the request (≈{need // 1000}k tokens) does not fit any Claude account's "
                         f"input ceiling ({ch_of[id(too_big[0])]['why']})"}
    ok_of = lambda c: ch_of[id(c)].get("ok", True)
    # Account choice is core's static ranking (core.resolve's unscored path): reserves spared while a non-reserve sibling
    # has room, then expiring credit first and scarce last, then configured order.
    for c in core.spared_reserves(cands, ok_of):
        ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"reserve account; a non-reserve account has {model} with room"}
    demote, expiring = core.static_thresholds()
    base = {id(c): i for i, c in enumerate(cands)}
    ranked = sorted(cands, key=lambda c: core.static_scarcity_rank(ch_of[id(c)], demote, expiring) + (base[id(c)],))
    live = [c for c in ranked if ok_of(c)]
    excluded = [c for c in cands if not ok_of(c)]
    why_of = lambda c: ch_of[id(c)].get("why") or "capacity unavailable"
    if not live:
        # Name what actually excluded each account. Only an account that fit but had no capacity left (not one that was
        # too big, nor one whose cached reading came from a different login) is a reason to point at the API-credit
        # lane: when every account was excluded for fit, the same request would not fit api/ either.
        big = {id(c) for c in too_big}
        stale = [c for c in excluded if id(c) not in big and why_of(c).startswith("stale cross-account")]
        spent = [c for c in excluded if id(c) not in big and id(c) not in {id(s) for s in stale}]
        parts = []
        for label, group in (("no capacity left", spent), (f"request (≈{need // 1000}k tokens) too large", too_big),
                             ("usage reading came from a different login, awaiting a fresh sample", stale)):
            if group:
                parts.append(f"{label}: " + "; ".join(f"{c['provider']} ({why_of(c)})" for c in group))
        hint = f"; pin api/{model} to spend API credit instead" if spent else ""
        return {"router": execution_routing.ROUTER,
                "error": f"sub/{model}: no Claude subscription account can take this request ({' | '.join(parts)}){hint}"}
    chosen = {**live[0], "pool": ch_of[id(live[0])].get("pool")}
    route_id = f"execution:provider:pin:{uuid.uuid4().hex[:12]}"
    # skipped has core.resolve's shape ({lane, model, pool, why}): core.footer and s1/outcome/run.py read it as dicts.
    skipped = [{"lane": c["lane"], "model": model, "pool": ch_of[id(c)].get("pool"), "why": why_of(c)} for c in excluded]
    route = {"band": "pin", "chosen": chosen, "answers": None, "subagents": 0, "verifier": False,
             "hard_rules": [f"pinned by the client: sub/{model}"] + [f"{c['provider']}: {why_of(c)}" for c in excluded],
             "skipped": skipped or None}
    # engine_ms None, not 0: S1 was never asked, so the row carries no S1 latency (core.usage_summary skips pin rows).
    decision = {"answers": None, "degraded": False, "engine": "pin", "engine_ms": None}
    core.log_route(route_id, {"session_id": conversation_id, "platform": f"provider:{client}", "unit_kind": "interactive",
                              "actual_model": None,
                              "request": request_text(body.get("messages") or [], standalone=not body.get("tools"))[:8000]},
                   decision, route)
    return {"router": execution_routing.ROUTER, "provider": chosen["provider"], "model": model,
            "account": chosen["account"], "config_dir": chosen["config_dir"],
            "context_length": core.lane_ceiling(chosen["lane"], model), "route_id": route_id, "api_request_id": "",
            "degraded": False, "band": "pin", "pinned": True, "decision": decision, "route": route}


def _excluded_route_usable(core, routes, body: dict[str, Any], claude_bin: str, codex_bin: str) -> bool | None:
    """Whether any excluded route would itself have served this request had it not been excluded: its binary on PATH,
    its lane (and pool) healthy by core.candidate_health, and the request within its context ceiling, the checks
    execution_routing.select applies to a chosen route. None when that cannot be told (an unknown lane, a broken
    accounts config, a failing health read): the caller then falls back to the coarser rule."""
    try:
        rows = [r for m in (core.model_scores() or {}).get("models", []) for r in m.get("reach", [])]
        rows += [r for band in core.table()["candidates"].values() for r in band]
        row_of: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            row_of.setdefault((CLONED_PROVIDERS.get(r["provider"], r["provider"]), r["model"]), r)
        accounts = {a["provider"]: a for a in core._vendor_accounts("claude", "claude", "claude-cli")}
        health = core.lane_health()
        check = _credentials(claude_bin, codex_bin)
        need = int(request_fit(body)["input_tokens"])
        usable = False
        for p, m in routes:
            row = row_of.get((_base_provider(p), m)) or {}
            acct = accounts.get(p) if _base_provider(p) == "claude-cli" else None
            lane = acct["lane"] if acct else row.get("lane")
            if not lane:
                return None
            try:
                check(requested=p, target_model=m)
            except execution_routing.CredentialsUnavailable:
                continue
            c = {"lane": lane, "provider": p, "model": m,
                 **({"pool": row["pool"]} if row.get("pool") else {}),
                 **({k: acct[k] for k in ("account", "config_dir", "reserve")} if acct else {})}
            if not core.candidate_health(c, health).get("ok", True):
                continue
            cap = core.lane_ceiling(lane, m)
            if _base_provider(p) == "codex-cli":
                cap = int(core._context_cfg().get("provider_ceiling_tokens", {}).get("codex-cli", 200000))
            if need > cap:
                continue
            usable = True
        return usable
    except Exception:          # health is advisory here: never let it turn a routing answer into a crash
        return None


def choose(core, body: dict[str, Any], *, client: str, conversation_id: str, claude_bin: str = "claude",
           codex_bin: str = "codex", exclude_model: str | None = None) -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error.
    A `sub/`-pinned request skips S1 (choose_pinned). `exclude_model` (the x-cortados-exclude-model header) keeps the
    conversation off that model on every lane, so a review never runs on the model that wrote the code."""
    excluded_model = normalize_model(exclude_model)
    if excluded_model == "":
        # `api`, `sub/`, `api/`: a lane named without a model. Ignoring it would route the review freely, possibly onto
        # its builder's model, so it is refused like any other header that names no served model.
        return {"router": execution_routing.ROUTER, "client_error": True,
                "error": f"x-cortados-exclude-model: {exclude_model.strip()} names no model this endpoint serves; "
                         "the review could not be kept off it"}
    pinned = sub_pin(body)
    if pinned:
        if excluded_model and model_key(pinned) == model_key(excluded_model):
            return {"router": execution_routing.ROUTER, "client_error": True,
                    "error": f"sub/{pinned} is the excluded model (x-cortados-exclude-model); pin another model or use cortados"}
        return choose_pinned(core, body, pinned, client=client, conversation_id=conversation_id, claude_bin=claude_bin)
    constraints: dict[str, Any] = {"allowed_routes": list(served_routes(core))}
    if excluded_model:
        constraints["excluded_routes"] = [[p, m] for p, m in constraints["allowed_routes"]
                                          if model_key(m) == model_key(excluded_model)]
        if not constraints["excluded_routes"]:
            # Loud beats silent: routing normally could land the review on its builder's model (e.g. a typo'd id).
            return {"router": execution_routing.ROUTER, "client_error": True,
                    "error": f"x-cortados-exclude-model: {exclude_model.strip()} names no model this endpoint serves; "
                             "the review could not be kept off it"}
        constraints["notes"] = [f"excluded by client: {excluded_model}"]   # select logs it with the route
    fit = request_fit(body)
    r = execution_routing.select(
        _NoClaudeWhenDegraded(core), prompt=request_text(body.get("messages") or [], standalone=not body.get("tools")),
        session_id=conversation_id, platform=PLATFORM, face="provider", entrypoint="provider", input_fit=fit,
        constraints=constraints,
        credential_resolver=_credentials(claude_bin, codex_bin), log_platform=f"provider:{client}",
        supports_accounts=True)   # this endpoint applies the routed account's login (server.Registry → bridge CLAUDE_CONFIG_DIR)
    if r.get("error") and r.get("degraded"):
        r["error"] = ("S1 is unavailable, and a degraded scorer never defaults to Claude; no Codex lane survived either "
                      "(codex-cli missing from PATH, or its capacity spent). " + r["error"])
    if excluded_model and r.get("error"):
        # A 400 only when the exclusion caused the refusal: S1 was up (a degraded result fails on its own, Claude
        # being withheld) and an excluded route would itself have served the request. Otherwise (S1 down, codex-cli
        # missing, every lane spent) the failure is the server's and stays a 503 the client may retry later.
        usable = None if r.get("degraded") else _excluded_route_usable(
            core, constraints["excluded_routes"], body, claude_bin, codex_bin)
        if usable is None:
            usable = not r.get("degraded")     # cannot tell: fall back to "not degraded"
        if usable:
            r["error"] += f" (the client excluded {excluded_model} via x-cortados-exclude-model)"
            # Retrying with the same header cannot succeed, and a 503 would have the client CLI retry until it timed
            # out. It is the client's to change: 400.
            r["client_error"] = True
    elif excluded_model and model_key(r.get("model")) == model_key(excluded_model):
        # Defence in depth: excluded_routes should already keep select off this model, but the review guarantee must
        # not rest only on set arithmetic in another module (an account-expanded provider, a clone added after the
        # allowlist filter, a future fallback). Refuse rather than serve the builder's own model.
        return {"router": execution_routing.ROUTER,
                "error": f"internal: routed to the excluded model {r.get('model')} ({r.get('provider')}); refused "
                         "(x-cortados-exclude-model)"}
    return r
