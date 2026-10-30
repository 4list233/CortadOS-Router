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
    too_big = []
    for c in cands:
        cap = core.lane_ceiling(c["lane"], model)
        if need > cap:
            ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"turn ≈{need // 1000}k tokens exceeds {c['lane']} ceiling {cap // 1000}k"}
            too_big.append(c)
    if cands and len(too_big) == len(cands):
        return {"router": execution_routing.ROUTER, "fit_blocked": True,
                "error": f"sub/{model}: the request (≈{need // 1000}k tokens) does not fit any Claude account's context "
                         f"ceiling ({ch_of[id(too_big[0])]['why']})"}
    ok_of = lambda c: ch_of[id(c)].get("ok", True)
    # Account choice is core's static ranking (core.resolve's unscored path): reserves spared while a non-reserve sibling
    # has room, then expiring credit first and scarce accounts last, then configured order.
    for c in core.spared_reserves(cands, ok_of):
        ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": f"reserve account; a non-reserve account has {model} with room"}
    demote, expiring = core.static_thresholds()
    base = {id(c): i for i, c in enumerate(cands)}
    ranked = sorted(cands, key=lambda c: core.static_scarcity_rank(ch_of[id(c)], demote, expiring) + (base[id(c)],))
    live = [c for c in ranked if ok_of(c)]
    spent = [f"{c['provider']}: {ch_of[id(c)].get('why') or 'capacity unavailable'}" for c in cands if not ok_of(c)]
    if not live:
        return {"router": execution_routing.ROUTER,
                "error": f"sub/{model}: every Claude subscription account is spent ({'; '.join(spent)}); "
                         f"pin api/{model} to spend API credit instead"}
    chosen = {**live[0], "pool": ch_of[id(live[0])].get("pool")}
    route_id = f"execution:provider:pin:{uuid.uuid4().hex[:12]}"
    route = {"band": "pin", "chosen": chosen, "answers": None, "subagents": 0, "verifier": False,
             "hard_rules": [f"pinned by the client: sub/{model}"] + spent, "skipped": spent or None}
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


def choose(core, body: dict[str, Any], *, client: str, conversation_id: str, claude_bin: str = "claude",
           codex_bin: str = "codex") -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error.
    A `sub/`-pinned request skips S1 (choose_pinned)."""
    pinned = sub_pin(body)
    if pinned:
        return choose_pinned(core, body, pinned, client=client, conversation_id=conversation_id, claude_bin=claude_bin)
    fit = request_fit(body)
    r = execution_routing.select(
        _NoClaudeWhenDegraded(core), prompt=request_text(body.get("messages") or [], standalone=not body.get("tools")),
        session_id=conversation_id, platform=PLATFORM, face="provider", entrypoint="provider", input_fit=fit,
        constraints={"allowed_routes": list(served_routes(core))},
        credential_resolver=_credentials(claude_bin, codex_bin), log_platform=f"provider:{client}",
        supports_accounts=True)   # this endpoint applies the routed account's login (server.Registry → bridge CLAUDE_CONFIG_DIR)
    if r.get("error") and r.get("degraded"):
        r["error"] = ("S1 is unavailable, and a degraded scorer never defaults to Claude; no Codex lane survived either "
                      "(codex-cli missing from PATH, or its capacity spent). " + r["error"])
    return r
