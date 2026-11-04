"""Model choice for a provider conversation: S1 scores the request once, the router's execution policy picks the route.

Owner of two rules:
  SERVED_PROVIDERS  which routes this endpoint can serve. Choosing among them stays with execution_routing.select,
                    the router's one execution policy (S1 → band → cheapest capable route with live capacity), so the
                    endpoint is a host of the router, not a second router.
  no Claude when degraded  core.resolve's hard rule that a degraded scorer never defaults to Claude. Execution mode
                    skips the table's non-Claude fallback routes, so this host enforces the rule itself: while S1 is
                    down a Claude route is not offered; the conversation runs on a Codex lane, and when no Codex
                    lane survives (binary missing, capacity spent) the request is refused.
  local pins        `sub/<local model or alias>` names a model a local Ollama node offers (local_lane.py): pin-only,
                    never in SERVED_PROVIDERS, so S1 auto-routing cannot pick it (its auto_route switch stays off).
  sub/ pin          a client that names `sub/<model>` (any model the tables serve on claude-cli or codex-cli) chose the
                    model itself: S1 and the band are skipped, but the served-model, account, capacity, fit and
                    route-log rules still apply (choose_pinned). pinnable_models is the list GET /v1/models offers.
"""
from __future__ import annotations

import json
import re
import shutil
import logging
import uuid
from typing import Any, Callable

from cortados_router import execution_routing
from cortados_router.provider import anthropic_wire as wire
from cortados_router.provider import local_lane

logger = logging.getLogger("cortados.provider.routing")

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


# The vendor each pinnable backend belongs to: its scarcity lane (capacity, fit) and the label its errors and model
# names use. Keyed by the same providers as SERVED_PROVIDERS, so a backend added there must be given a row here before
# its models become pinnable (pinnable_models skips nothing silently: a served provider without a row raises).
PIN_VENDORS = {"claude-cli": {"lane": "claude", "family": "Claude"},
               "codex-cli": {"lane": "codex", "family": "Codex"}}

SUB_PREFIX = "sub/"
SUB_DEFAULT = "claude-opus-5-5"
# Short names a person types (`claude --model sub/opus`); each must name a model the tables serve on claude-cli.
SUB_ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5-20251001",
               "fable": "claude-fable-5-1"}


def sub_pin(body: dict[str, Any]) -> str | None:
    """A request pinned to a subscription model: model `sub/<model id or Claude alias>` (bare `sub` for the
    default), a Claude model (claude-cli) or a Codex one (codex-cli). Returns the requested model name (unvalidated), or None for a normal S1-routed request."""
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


def canonical_model(name: str | None) -> str | None:
    """The identity a model name resolves to, for exclusion checks: a local pin's alias and its Ollama tag are one
    model (the tag, model_key'd); any other name is its model_key. A broken local config resolves nothing locally
    (choose_pinned reports it)."""
    if not name:
        return name
    try:
        loc = local_lane.find(name)
    except local_lane.LocalConfigError:
        loc = None
    return model_key(loc[1]["id"] if loc else name)


RESERVED_NAMES = ("sub", "api", "cortados", "opusplan", "default")


def local_clashes(core) -> dict[str, str]:
    """Local pin names (alias or tag, as model keys) that equal a model this endpoint serves, a SUB_ALIASES key or a
    reserved name, mapped to their node. A clash would let a local entry take over `sub/<served id>` and blur model
    exclusions, so any clash is a config error the owner must fix (choose_pinned and /v1/models refuse it)."""
    served = {model_key(m) for _, m in pinnable_models(core)}
    served |= {model_key(a) for a in SUB_ALIASES} | set(RESERVED_NAMES)
    out = {}
    for n, m in local_lane.pins():
        for name in {m["id"], local_lane.pin_id(m)}:
            if model_key(name) in served:
                out[model_key(name)] = n["node"]
    return out


def local_pins(core) -> list:
    """local_lane.pins(), refused with a LocalConfigError when any local name clashes (local_clashes)."""
    clashes = local_clashes(core)
    if clashes:
        raise local_lane.LocalConfigError("local pin names collide with served or reserved model names: "
                                          + ", ".join(f"{k} (node {v})" for k, v in sorted(clashes.items()))
                                          + "; rename the alias", next(iter(clashes.values())))
    return local_lane.pins()


def misconfigured(model: str, exc: "local_lane.LocalConfigError") -> str:
    """What a client is told about a broken local config: the node, never the message (it can hold the provider's
    file paths). The full message goes to the provider log."""
    logger.warning("local config error for sub/%s: %s", model, exc)
    node = getattr(exc, "node", None) or "?"
    return f"sub/{model}: local node {node} is misconfigured on the provider (see provider log)"


def _is_local(name: str) -> bool:
    try:
        return local_lane.find(name) is not None
    except local_lane.LocalConfigError:
        return False


def pinnable_models(core) -> list[tuple[str, str]]:
    """Every `(provider, model)` a client may pin as `sub/<model>`: the routes this endpoint serves (served_routes, read
    from the tables, never a list kept here), one entry per model on its base provider (accounts are chosen at pin
    time, not by the client). Claude first, then Codex, each sorted by id. A broken accounts config drops the Claude
    models here exactly as served_routes does (a pin of one would be refused with the config error)."""
    out = sorted({(_base_provider(p), m) for p, m in served_routes(core)},
                 key=lambda pm: (list(PIN_VENDORS).index(pm[0]) if pm[0] in PIN_VENDORS else len(PIN_VENDORS), pm[1]))
    for p, m in out:
        if p not in PIN_VENDORS:     # a served backend with no vendor row would be pinned without its capacity gates
            raise ValueError(f"served provider {p!r} has no PIN_VENDORS row; give it one before {m} can be pinned")
    return out


def readable_model_name(model: str) -> str:
    """A person's name for a table model id, derived from the id (the tables carry no display name for the
    claude-cli/codex-cli rows): `claude-fable-5-1` -> `Claude Fable 5.1`, `gpt-6.1-sol` -> `GPT-6.1 Sol`. The release
    date suffix is dropped. An id of another shape is returned unchanged rather than guessed at."""
    parts = (model_key(model) or "").split("-")
    words: list[str] = []
    for part in parts[1:]:
        if part.replace(".", "").isdigit() and words and words[-1][0].isdigit():
            words[-1] += "." + part                       # Claude ids spell versions with dashes: 5-1 is 5.1
        else:
            words.append(part if part[:1].isdigit() else part.capitalize())
    if parts[0] == "claude" and words:
        return " ".join(["Claude"] + words)
    if parts[0] == "gpt" and words and words[0][0].isdigit():
        return " ".join([f"GPT-{words[0]}"] + words[1:])
    return model


def min_left_block(core, lane: str, model: str, min_left_pct: float | None, readings: dict | None = None) -> str | None:
    """Why the account on `lane` may not serve `model` under the client's x-cortados-min-left-pct floor, or None.
    The floor only tightens: it is applied on top of every existing gate, to the cached reading's current weekly windows
    that bound the model (general + the model's own pool). An account with no readable weekly figure is excluded too:
    a floor the provider cannot check would otherwise pass silently (fail closed, the client asked for a guarantee)."""
    if min_left_pct is None:
        return None
    readings = core.subscription_readings() if readings is None else readings
    reading = readings.get(lane) if isinstance(readings, dict) else None
    if not isinstance(reading, dict) or reading.get("failed"):
        return f"no current usage reading to check x-cortados-min-left-pct {min_left_pct:g}"
    weekly = [w for w in reading.get("windows") or [] if str(w.get("window") or "").startswith("week") and not w.get("stale")]
    pools = sorted({w.get("pool") for w in weekly if w.get("pool") not in (None, "all")})
    pool = core._scarcity_module().pool_for(lane, model or "", pools) if pools else None
    relevant = [w for w in weekly if (w.get("pool") or "all") == "all" or (pool and w.get("pool") == pool)]
    if not relevant:
        return f"no current weekly window to check x-cortados-min-left-pct {min_left_pct:g}"
    for w in relevant:
        left = w.get("headroom_pct")
        if left is None and isinstance(w.get("used_pct"), (int, float)):
            left = 100.0 - float(w["used_pct"])
        if not isinstance(left, (int, float)):
            return f"{w.get('window')}: no usage figure to check x-cortados-min-left-pct {min_left_pct:g}"
        if left < min_left_pct:
            return f"{w.get('window')}: {left:.0f}% left, below the client's floor x-cortados-min-left-pct {min_left_pct:g}"
    return None


def choose_pinned(core, body: dict[str, Any], model: str, *, client: str, conversation_id: str,
                  claude_bin: str = "claude", codex_bin: str = "codex", min_left_pct: float | None = None) -> dict[str, Any]:
    """Route a `sub/`-pinned conversation: the person chose the model, so S1 is not asked and the band is not applied
    (a pin that the band would refuse still runs; that is what pinning is for). What stays is the router's accounting
    and safety: only a model the tables serve on claude-cli or codex-cli, only a configured account whose subscription
    is not spent (ranked by core's static account ranking: reserves spared, expiring credit first, scarce last), a
    request that fits the lane's context ceiling, the binary on PATH, and a routes row so the usage is on record
    (engine `pin`, no answers: S1's drift check skips it, and the dataset only harvests the request text for
    labelling). A Codex pin runs on the provider's one Codex login (codex_bridge on --codex-home): this endpoint
    has no per-account Codex backend yet (server._codex_session), so it is a single candidate on the `codex` lane."""
    try:
        clashes = local_clashes(core)
        local = local_lane.find(model)
    except local_lane.LocalConfigError as exc:      # a broken local config refuses local pins, never guesses a node
        clashes, local, local_error = {}, None, exc
    else:
        local_error = None
    if model_key(model) in clashes:                 # the name is both a served model and a local pin: refuse, loudly
        exc = local_lane.LocalConfigError(f"local pin name {model!r} on node {clashes[model_key(model)]} collides with "
                                          "a served or reserved model name; rename the alias", clashes[model_key(model)])
        return {"router": execution_routing.ROUTER, "error": misconfigured(model, exc)}
    if local is not None:
        return _choose_local(core, body, *local, client=client, conversation_id=conversation_id)
    # Accounts first: served_routes drops every Claude pair on a broken config, which would misreport a Claude model as
    # unknown. Only a Claude-shaped name is refused for it, so a broken Claude config never blocks a Codex pin.
    try:
        claude_accounts = core._vendor_accounts("claude", "claude", "claude-cli")
    except core.AccountsConfigError as exc:         # broken accounts config refuses Claude, never an ambient login
        if model.startswith("claude"):
            return {"router": execution_routing.ROUTER, "error": f"sub/{model}: {exc}"}
        claude_accounts = []
    pinnable = pinnable_models(core)
    provider = next((p for p, m in pinnable if m == model), None)
    if provider is None:
        if local_error:
            return {"router": execution_routing.ROUTER, "error": misconfigured(model, local_error)}
        names = [m for _, m in pinnable] + [local_lane.pin_id(m) for _, m in local_lane.pins()]
        return {"router": execution_routing.ROUTER, "client_error": True,
                "error": f"sub/{model} is not a model this endpoint serves; pin one of: {', '.join(names)}"}
    vendor = PIN_VENDORS[provider]
    family = vendor["family"]
    if provider == "claude-cli":
        accounts = claude_accounts
    else:
        # One candidate, the default login: the same shape core._vendor_accounts gives a vendor with no accounts entry.
        accounts = [{"lane": vendor["lane"], "provider": provider, "account": None, "reserve": False, "config_dir": None}]
    try:
        _credentials(claude_bin, codex_bin)(requested=provider, target_model=model)
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
    # core.resolve's execution ceiling: the codex-cli transport has its own (provider_ceiling_tokens), not the lane's.
    cap_of = lambda c: (int(core._context_cfg().get("provider_ceiling_tokens", {}).get("codex-cli", 200000))
                        if provider == "codex-cli" else core.lane_ceiling(c["lane"], model))
    too_big = []
    for c in cands:
        cap = cap_of(c)
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
                "error": f"sub/{model}: the request (≈{need // 1000}k tokens) does not fit any {family} account's "
                         f"input ceiling ({ch_of[id(too_big[0])]['why']})"}
    floored = []
    if min_left_pct is not None:                   # the client's floor (x-cortados-min-left-pct): before ranking, only tightens
        readings = core.subscription_readings()
        for c in cands:
            why = min_left_block(core, c["lane"], model, min_left_pct, readings) if ch_of[id(c)].get("ok", True) else None
            if why:
                ch_of[id(c)] = {**ch_of[id(c)], "ok": False, "why": why}
                floored.append(c)
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
        # The API-credit lane is Anthropic's: there is no api/ pin for a Codex model to point at.
        hint = f"; pin api/{model} to spend API credit instead" if spent and provider == "claude-cli" else ""
        return {"router": execution_routing.ROUTER, **({"quota_blocked": True} if floored else {}),
                "error": f"sub/{model}: no {family} subscription account can take this request ({' | '.join(parts)}){hint}"}
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
            "context_length": cap_of(chosen), "route_id": route_id, "api_request_id": "",
            "degraded": False, "band": "pin", "pinned": True, "decision": decision, "route": route}


def _choose_local(core, body: dict[str, Any], node: dict[str, Any], entry: dict[str, Any], *, client: str,
                  conversation_id: str) -> dict[str, Any]:
    """The local-lane kind of a sub/ pin (choose_pinned's one branch for it): the node must answer /api/tags and list
    the model (no quota: available or not), the request must fit the model's configured context, and the opening
    request of a conversation is put on record as a routes row (engine `pin`, lane local-<node>). The lane is
    stateless, so every turn is checked again but only the opener is logged: one row per conversation, as other pins.
    Never a fallback: an unavailable node is an error the person sees, not a quiet hop to another model."""
    pin = local_lane.pin_id(entry)
    try:
        tags = local_lane.probe(node)
    except local_lane.LocalUnavailable as exc:
        return {"router": execution_routing.ROUTER, "error": f"sub/{pin}: {exc}; no other model was tried"}
    if entry["id"] not in tags:
        return {"router": execution_routing.ROUTER,
                "error": f"sub/{pin}: node {node['node']} does not list {entry['id']} (ollama pull it there); "
                         "no other model was tried"}
    need = int(request_fit(body)["input_tokens"])
    if need > entry["context_length"]:
        return {"router": execution_routing.ROUTER, "fit_blocked": True,
                "error": f"sub/{pin}: the request (≈{need // 1000}k tokens) exceeds {entry['id']}'s configured "
                         f"context ({entry['context_length'] // 1000}k) on {node['lane']}"}
    chosen = {"lane": node["lane"], "provider": node["lane"], "model": entry["id"], "account": None,
              "config_dir": None, "reserve": False, "pool": None}
    route = {"band": "pin", "chosen": chosen, "answers": None, "subagents": 0, "verifier": False,
             "hard_rules": [f"pinned by the client: sub/{pin}"], "skipped": None}
    decision = {"answers": None, "degraded": False, "engine": "pin", "engine_ms": None}
    opener = not any(m.get("role") == "assistant" for m in body.get("messages") or [])
    route_id = f"execution:provider:pin:{uuid.uuid4().hex[:12]}" if opener else None
    if opener:
        core.log_route(route_id, {"session_id": conversation_id, "platform": f"provider:{client}",
                                  "unit_kind": "interactive", "actual_model": None,
                                  "request": request_text(body.get("messages") or [],
                                                          standalone=not body.get("tools"))[:8000]},
                       decision, route)
    return {"router": execution_routing.ROUTER, "provider": node["lane"], "model": entry["id"], "account": None,
            "config_dir": None, "context_length": entry["context_length"], "route_id": route_id,
            "api_request_id": "", "degraded": False, "band": "pin", "pinned": True, "decision": decision,
            "route": route, "local": {"node": node, "model": entry}}


def _excluded_route_usable(core, routes, body: dict[str, Any], claude_bin: str, codex_bin: str) -> bool | None:
    """Whether any excluded route would itself have served this request had it not been excluded: its binary on PATH,
    its lane (and pool) healthy by core.candidate_health, and the request within its token ceiling. That is a subset
    of resolve's fit gates: the byte ceilings and unknown-fit gates are skipped because no lane this endpoint serves
    has one today; give it them if `ceiling_bytes` ever gains a claude/codex entry. None when that cannot be told (an unknown lane, a broken
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
           codex_bin: str = "codex", exclude_model: str | None = None,
           min_left_pct: float | None = None) -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error.
    A `sub/`-pinned request skips S1 (choose_pinned). `exclude_model` (the x-cortados-exclude-model header, a comma
    list) keeps the conversation off each listed model on every lane, so a review never runs on a model that wrote the
    code."""
    items = [i.strip() for i in (exclude_model or "").split(",") if i.strip()]
    excluded_models: list[str] = []
    for item in items:
        m = normalize_model(item)
        if m == "":
            # `api`, `sub/`, `api/`: a lane named without a model. Ignoring it would route the review freely, possibly onto
            # its builder's model, so it is refused like any other header that names no served model.
            return {"router": execution_routing.ROUTER, "client_error": True,
                    "error": f"x-cortados-exclude-model: {item} names no model this endpoint serves; "
                             "the review could not be kept off it"}
        if m and m not in excluded_models:
            excluded_models.append(m)
    # Both identities of each name: the canonical one (a local alias and its tag are one model) and the plain model
    # key (what the name means on the served lanes), so neither recheck below can miss a model by spelling.
    excluded_keys = {canonical_model(m) for m in excluded_models} | {model_key(m) for m in excluded_models}
    hit = lambda name: bool({canonical_model(name), model_key(name)} & excluded_keys)
    names = ", ".join(excluded_models)            # one model: exactly the single-model wording
    pinned = sub_pin(body)
    if pinned:
        if hit(pinned):
            return {"router": execution_routing.ROUTER, "client_error": True,
                    "error": f"sub/{pinned} is the excluded model (x-cortados-exclude-model); pin another model or use cortados"}
        r = choose_pinned(core, body, pinned, client=client, conversation_id=conversation_id, claude_bin=claude_bin,
                          codex_bin=codex_bin, min_left_pct=min_left_pct)
        if not r.get("error") and hit(r.get("model")):
            # Defence in depth, as on the S1 path: the review guarantee must hold on what was selected, not only on
            # how the client spelled the pin.
            return {"router": execution_routing.ROUTER,
                    "error": f"internal: sub/{pinned} resolved to the excluded model {r.get('model')}; refused "
                             "(x-cortados-exclude-model)"}
        return r
    constraints: dict[str, Any] = {"allowed_routes": list(served_routes(core))}
    if excluded_models:
        constraints["excluded_routes"] = []
        for item in items:
            m = normalize_model(item)
            if not m:                              # `cortados`: names no model to stay off
                continue
            routes = [[p, r] for p, r in constraints["allowed_routes"] if model_key(r) == model_key(m)]
            if not routes and _is_local(m):
                continue                           # only a local model: S1 never routes there, so it is avoided
            if not routes:
                # Loud beats silent: routing normally could land the review on its builder's model (e.g. a typo'd id).
                return {"router": execution_routing.ROUTER, "client_error": True,
                        "error": f"x-cortados-exclude-model: {item} names no model this endpoint serves; "
                                 "the review could not be kept off it"}
            constraints["excluded_routes"] += [r for r in routes if r not in constraints["excluded_routes"]]
        constraints["notes"] = [f"excluded by client: {names}"]   # select logs it with the route
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
    if excluded_models and r.get("error"):
        # A 400 only when the exclusion caused the refusal: S1 was up (a degraded result fails on its own, Claude
        # being withheld) and an excluded route would itself have served the request. Otherwise (S1 down, codex-cli
        # missing, every lane spent) the failure is the server's and stays a 503 the client may retry later.
        usable = None if r.get("degraded") else _excluded_route_usable(
            core, constraints["excluded_routes"], body, claude_bin, codex_bin)
        if usable is None:
            usable = not r.get("degraded")     # cannot tell: fall back to "not degraded"
        if usable:
            r["error"] += f" (the client excluded {names} via x-cortados-exclude-model)"
            # Retrying with the same header cannot succeed, and a 503 would have the client CLI retry until it timed
            # out. It is the client's to change: 400.
            r["client_error"] = True
    elif excluded_models and model_key(r.get("model")) in excluded_keys:
        # Defence in depth: excluded_routes should already keep select off these models, but the review guarantee must
        # not rest only on set arithmetic in another module (an account-expanded provider, a clone added after the
        # allowlist filter, a future fallback). Refuse rather than serve a builder's own model.
        return {"router": execution_routing.ROUTER,
                "error": f"internal: routed to the excluded model {r.get('model')} ({r.get('provider')}); refused "
                         "(x-cortados-exclude-model)"}
    if min_left_pct is not None and not r.get("error"):
        # The routed path's floor check runs on the account select chose (execution_routing ranks inside select); a
        # chosen account below the client's floor is refused like a spent one, never swapped for an unscored sibling.
        lane = ((r.get("route") or {}).get("chosen") or {}).get("lane") or r.get("lane")
        why = min_left_block(core, lane, r.get("model") or "", min_left_pct) if lane else \
            "routed account unknown; cannot check x-cortados-min-left-pct"
        if why:
            return {"router": execution_routing.ROUTER, "quota_blocked": True,
                    "error": f"routed to {r.get('provider')}/{r.get('model')} on {lane}: {why}"}
    return r
