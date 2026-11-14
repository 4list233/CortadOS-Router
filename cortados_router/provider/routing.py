"""Model choice for a provider conversation: S1 scores the request once, the router's execution policy picks the route.

Owner of two rules:
  SERVED_PROVIDERS  which routes this endpoint can serve. Choosing among them stays with execution_routing.select,
                    the router's one execution policy (S1 → band → cheapest capable route with live capacity), so the
                    endpoint is a host of the router, not a second router.
  no Claude when degraded  core.resolve's hard rule that a degraded scorer never defaults to Claude. Execution mode
                    skips the table's non-Claude fallback routes, so this host enforces the rule itself: while S1 is
                    down a Claude route is not offered; the conversation runs on a Codex lane, and when no Codex
                    lane survives (binary missing, capacity spent) the request is refused.
  local pins        `sub/<local model or alias>` names a model a local Ollama node offers (local_lane.py). Local lanes
                    are never in SERVED_PROVIDERS: S1 and core.resolve never see one.
  local admission   local_admission is THE one gate every automatic move to a local lane passes (auto_local,
                    index_local, sensitive_local, refusal_route, the continuation re-check server.py runs on every later turn of a
                    conversation an automatic rule bound to a local lane): S1 not degraded, a band, `outward` exactly
                    False and `blast_radius` exactly the int 0 or 1. A missing or garbled answer is not admitted.
  local auto-route  after S1 has chosen a route (not degraded), auto_local may hand the conversation to a node's
                    auto_route model instead, only when local_admission admits it, S1's band is one the node's config
                    lists, the node answers its probe (on the Pro: AC power + manual switch + gate up) and lists the
                    model, the request fits the model's context, the client did not exclude it, and the request is on
                    /v1/messages. Otherwise S1's
                    route stands unchanged: a local node is an extra, never a fallback (#75). Kept for configs that
                    set it; the index route below supersedes it.
  local index route a node model with a published proxy index (model-scores.generated.json → local_models, from
                    tables/local-models.json) takes the conversation wherever its index for S1's use case clears S1's
                    required index, like a cloud model (index_local). A local lane spends no quota, so where it
                    qualifies and its node is available and not busy it wins over S1's cloud choice. Only what
                    local_admission admits (no outward effect, blast_radius 0 or 1, not degraded, a band), never R3/R4;
                    an index-bound conversation is an automatic bind, so every continuation is re-checked too.
  sensitive         after S1 has chosen a route, sensitive_local may move a request marked sensitive (the
                    x-cortados-local header, the node's keyword list; a planned S1 signal plugs in at S1_SENSITIVE_ANSWER)
                    to the node's `sensitive.model` in ANY band, but only when S1 says it has no effect outside this
                    machine (local_admission) and the local guards pass. refusal_route does the same for an opening
                    turn a cloud model refused (server.py).
                    core.resolve's outward floor is untouched: an outward request never reaches a local model.
  sub/ pin          a client that names `sub/<model>` (any model the tables serve on claude-cli or codex-cli) chose the
                    model itself: S1 and the band are skipped, but the served-model, account, capacity, fit and
                    route-log rules still apply (choose_pinned). pinnable_models is the list GET /v1/models offers.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
import shutil
import logging
import time
import uuid
from typing import Any, Callable

from cortados_router import execution_routing
from cortados_router.provider import anthropic_wire as wire
from cortados_router.provider import floor
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


class AmbiguousPin(ValueError):
    """A friendly pin name that fits more than one pool model. `candidates` are the pin names it could mean; the
    router never guesses among them (the person asked for one model, and a guess could spend the wrong quota)."""

    def __init__(self, name: str, candidates: list[str]):
        super().__init__(f"sub/{name} is ambiguous: it could be {', '.join(candidates)}; pin one of them by its full name")
        self.name, self.candidates = name, candidates


# Words that name a lane rather than a model. In a friendly name they narrow the candidates to that lane and are not
# matched as tokens (`ollama 27b q3km` means the local 27b q3km; no local tag carries the word "ollama").
LANE_WORDS = {"ollama": "local", "local": "local", "claude": "claude-cli", "anthropic": "claude-cli",
              "codex": "codex-cli", "openai": "codex-cli", "chatgpt": "codex-cli"}
# A friendly name is split on these, never on `.`: `6.1` and `qwen3.8` are single tokens, so `GPT 6.1` cannot mean
# `gpt-6-sol`.
_SEPARATORS = re.compile(r"[\s\-_:/]+")


def name_tokens(name: str) -> list[str]:
    """The words of a model name, casefolded, split on spaces, dashes, underscores, colons and slashes. Consecutive
    all-digit words merge into one version (`claude-fable-5-1` -> `5.1`), so a dashed Claude version and a dotted
    friendly one compare equal. A trailing `-YYYYMMDD` release date is dropped first (model_key)."""
    parts = [p for p in _SEPARATORS.split(model_key(name) or "") if p]
    out: list[str] = []
    for p in parts:
        if p.isdigit() and out and out[-1].replace(".", "").isdigit():
            out[-1] += "." + p
        else:
            out.append(p)
    return out


def pin_candidates(core) -> list[tuple[str, str, set[str]]]:
    """Every name a client may pin, as `(pin name, lane, tokens)`: the served models (pinnable_models) under their
    PIN_VENDORS provider, and the local pins (alias or tag) under `local`, each with the words of its id, alias and
    display name. A broken local config contributes no local candidates (choose_pinned reports it)."""
    out = [(m, p, set(name_tokens(m))) for p, m in pinnable_models(core)]
    try:
        local = local_lane.pins()
    except local_lane.LocalConfigError:
        local = []
    for _node, m in local:
        words = set(name_tokens(m["id"])) | set(name_tokens(m.get("alias") or "")) | set(name_tokens(m.get("name") or ""))
        out.append((local_lane.pin_id(m), "local", words))
    return out


def resolve_pin(core, name: str) -> str | None:
    """THE resolver for a pinned model name, exact or friendly: the one pin name (a served model id, or a local alias
    or tag, as choose_pinned and local_lane.find take it) that `name` means, None when it means none, AmbiguousPin
    when it fits several. Exact first: a SUB_ALIASES key, a served id (release date optional), a local alias or tag,
    case-insensitively. Then friendly: every word of the name must be a word of the candidate (`GPT 6.1` ->
    gpt-6.1-sol; `Sonnet 5` -> claude-sonnet-5; `ollama 27b q3km` -> the local 27b q3km tag), a lane word (LANE_WORDS)
    only narrows the lane. The model field is the only input: prompt text is never searched, so a passing mention of
    a model in a request cannot pin it."""
    raw = (name or "").strip()
    key = model_key(raw)
    if not key:
        return None
    if key in SUB_ALIASES:
        return SUB_ALIASES[key]
    candidates = pin_candidates(core)
    # A set: a name that is both a served id and a local pin is one exact name here, and choose_pinned refuses the
    # clash as a config error (local_clashes), not as an ambiguity the client could resolve.
    exact = sorted({pin for pin, _lane, _words in candidates if model_key(pin) == key})
    if not exact:
        try:                                   # a local tag pinned by its alias's model, or the other way round
            loc = local_lane.find(raw)
        except local_lane.LocalConfigError:
            loc = None
        if loc:
            return local_lane.pin_id(loc[1])
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise AmbiguousPin(raw, sorted(exact))
    words = name_tokens(raw)
    lanes = {LANE_WORDS[w] for w in words if w in LANE_WORDS}
    words = [w for w in words if w not in LANE_WORDS]
    if not words and not lanes:
        return None
    hits = [pin for pin, lane, have in candidates
            if (not lanes or lane in lanes) and all(w in have for w in words)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise AmbiguousPin(raw, sorted(hits))
    return None


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


def choose_pinned(core, body: dict[str, Any], model: str, *, client: str, conversation_id: str,
                  claude_bin: str = "claude", codex_bin: str = "codex", min_left_pct: float | None = None,
                  codex_home: str | None = None) -> dict[str, Any]:
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
            why = floor.check(core, {**c, "codex_home": codex_home}, model, min_left_pct, readings) \
                if ch_of[id(c)].get("ok", True) else None
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
    if min_left_pct is not None:
        # Reserve the turn on the account that will execute it, atomically with its floor recheck (floor.reserve): a
        # concurrent request may have taken the last room since the filter above, so a refusal here is a quota refusal.
        route_id = f"execution:provider:pin:{uuid.uuid4().hex[:12]}"
        bound = _bind_codex(chosen, codex_home)
        why = floor.reserve(core, {**chosen, **bound}, model, min_left_pct, readings, conv=route_id)
        if why:
            return {"router": execution_routing.ROUTER, "quota_blocked": True,
                    "error": f"sub/{model}: {chosen['provider']}: {why}"}
    if min_left_pct is None:
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
            "degraded": False, "band": "pin", "pinned": True, "decision": decision, "route": route,
            **({"min_left_pct": min_left_pct} if min_left_pct is not None else {}),
            **({"codex_home": bound["codex_login"]} if min_left_pct is not None and bound else {})}


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
           min_left_pct: float | None = None, codex_home: str | None = None,
           local: str | None = None) -> dict[str, Any]:
    """Route a new conversation. Returns execution_routing.select's result: provider, model, route_id, band, … or error.
    A `sub/`-pinned request skips S1 (choose_pinned). `exclude_model` (the x-cortados-exclude-model header, a comma
    list) keeps the conversation off each listed model on every lane, so a review never runs on a model that wrote the
    code. `local` (the x-cortados-local header: LOCAL_MODES) marks the request for a node's sensitive model
    (sensitive_local); a pin ignores it (the person chose the model)."""
    if local is not None and local not in LOCAL_MODES:
        return {"router": execution_routing.ROUTER, "client_error": True,
                "error": f"{LOCAL_HEADER}: must be one of {', '.join(LOCAL_MODES)}"}
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
        # A friendly or undated name becomes its one pin name here, before the exclusion check, so that check sees the
        # model the pin means. A name that means nothing stays as typed: choose_pinned refuses it with the pinnable list.
        try:
            pinned = resolve_pin(core, pinned) or pinned
        except AmbiguousPin as exc:
            return {"router": execution_routing.ROUTER, "client_error": True, "error": str(exc)}
        if local:
            logger.info("%s: %s ignored: the request pins sub/%s", LOCAL_HEADER, local, pinned)
        if hit(pinned):
            return {"router": execution_routing.ROUTER, "client_error": True,
                    "error": f"sub/{pinned} is the excluded model (x-cortados-exclude-model); pin another model or use cortados"}
        r = choose_pinned(core, body, pinned, client=client, conversation_id=conversation_id, claude_bin=claude_bin,
                          codex_bin=codex_bin, min_left_pct=min_left_pct, codex_home=codex_home)
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
                continue                           # only a local model: S1 never routes there; auto_local checks it
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
    if not r.get("error"):
        r = sensitive_local(core, body, r, client=client, conversation_id=conversation_id, excluded=hit, local=local)
        if not r.get("local"):
            r = auto_local(core, body, r, client=client, conversation_id=conversation_id, excluded=hit)
    if min_left_pct is not None and not r.get("error"):
        # The routed path's floor check runs on the account select chose (execution_routing ranks inside select); a
        # chosen account below the client's floor is refused like a spent one, never swapped for an unscored sibling.
        chosen = (r.get("route") or {}).get("chosen") or {}
        lane = chosen.get("lane")
        if lane and _floor_exempt(lane):
            return r                               # a local node spends no subscription quota: the floor does not apply
        bound = _bind_codex(floor_candidate(r), codex_home)
        why = floor.reserve(core, {**floor_candidate(r, codex_home), **bound}, r.get("model") or "", min_left_pct,
                            conv=r.get("route_id")) \
            if lane else "routed account unknown; cannot check x-cortados-min-left-pct"
        if why:
            return {"router": execution_routing.ROUTER, "quota_blocked": True,
                    "error": f"routed to {r.get('provider')}/{r.get('model')} on {lane}: {why}"}
        r["min_left_pct"] = min_left_pct           # the session keeps it: every continuation is rechecked under it
        if bound:
            r["codex_home"] = bound["codex_login"]  # the backend starts on exactly the home that was checked
    return r


# A routing body carrying this key came from a wire the local relay cannot serve (/v1/responses: local_lane relays the
# Anthropic Messages shape only), so auto_local leaves its route alone. Set by server.RESPONSES.routing_body.
NO_LOCAL_KEY = "_cortados_no_local"
# How long a node that failed its probe is not probed again for auto-routing. Reason: an asleep or off-tailnet Pro costs
# a full probe_timeout_s per probe, and without this every new S1-routed conversation would wait for it. A node refusing
# on battery or with the switch off answers at once, so this only bounds the slow case; turning the switch back on
# takes effect within this window. Pins never use it (a person who pins a model is told the node's state now).
AUTO_DOWN_S = 30.0
_auto_down: dict[str, float] = {}


def _node_why(node: dict[str, Any], entry: dict[str, Any], body: dict[str, Any],
              excluded: Callable[[str], bool], need: list) -> tuple[str | None, bool]:
    """The local guards every move to a node passes (auto_local, sensitive_local, refusal_route): the client did not
    exclude the model, the node is not inside its AUTO_DOWN_S window, it answers its probe (on the Pro the gate refuses
    that probe on battery or with the manual switch off) and lists the model, and the request fits the model's context.
    Returns (why not, or None; whether the reason is a gate 401). `need` caches the request's size across nodes."""
    pin = local_lane.pin_id(entry)
    if excluded(entry["id"]) or excluded(pin):
        return "excluded by the client (x-cortados-exclude-model)", False
    if _auto_down.get(node["node"], 0.0) > _now():
        return "probe failed within the last %gs" % AUTO_DOWN_S, False
    try:
        tags = local_lane.probe(node)
    except local_lane.LocalUnavailable as exc:
        _auto_down[node["node"]] = _now() + AUTO_DOWN_S
        # A gate 401 is the provider's misconfiguration (token files out of step), not the node's state: it needs the
        # owner's attention, so it is a warning, where battery or a switched-off node is routine.
        return f"unavailable ({exc})", str(exc) == local_lane.GATE_REFUSED.format(node=node["node"])
    _auto_down.pop(node["node"], None)
    if entry["id"] not in tags:
        return f"does not list {entry['id']}", False
    if not need:
        need.append(int(request_fit(body)["input_tokens"]))
    if need[0] > entry["context_length"]:
        return f"request ≈{need[0] // 1000}k tokens exceeds its {entry['context_length'] // 1000}k context", False
    return None, False


def _moved(r: dict[str, Any], node: dict[str, Any], entry: dict[str, Any], rule: str) -> dict[str, Any]:
    """S1's route `r` moved to `entry` on `node`: the routes record names the local lane, S1's choice becomes runner_up,
    S1's own runner_up is kept as s1_runner_up and as a skipped entry, and `rule` joins the hard rules. Nothing is
    written here (_log_moved writes it), so a move that does not happen leaves no trace in the routes table."""
    old = r.get("route") or {}
    chosen = {"lane": node["lane"], "provider": node["lane"], "model": entry["id"], "account": None,
              "config_dir": None, "reserve": False, "pool": None}
    # runner_up is S1's choice (what runs when the node is not there); S1's own runner_up is not dropped: it is
    # kept as s1_runner_up and, so the routes row still shows it, as a skipped entry naming why.
    s1_runner_up = old.get("runner_up")
    skipped = list(old.get("skipped") or [])
    if isinstance(s1_runner_up, dict) and s1_runner_up.get("model"):
        skipped.append({"lane": s1_runner_up.get("lane"), "model": s1_runner_up.get("model"),
                        "why": "S1's runner_up (s1_runner_up), behind S1's choice the local lane replaced"})
    route = {**old, "chosen": chosen, "runner_up": old.get("chosen"), "s1_runner_up": s1_runner_up,
             "skipped": skipped or None, "hard_rules": list(old.get("hard_rules") or []) + [rule]}
    return {"router": r.get("router", execution_routing.ROUTER), "provider": node["lane"], "model": entry["id"],
            "account": None, "config_dir": None, "context_length": entry["context_length"],
            "route_id": r["route_id"], "api_request_id": r.get("api_request_id", ""), "degraded": False,
            "band": r.get("band"), "projection": r.get("projection"), "decision": r.get("decision"), "route": route,
            "local": {"node": node, "model": entry}}


def _log_moved(core, body: dict[str, Any], moved: dict[str, Any], *, client: str, conversation_id: str) -> None:
    """Rewrite the routes row S1's select wrote, under the same route_id, with what will run (_moved's record)."""
    core.log_route(moved["route_id"], {"session_id": conversation_id, "platform": f"provider:{client}",
                                       "unit_kind": "interactive", "actual_model": None,
                                       "request": request_text(body.get("messages") or [],
                                                               standalone=not body.get("tools"))[:8000]},
                   moved.get("decision") or {"answers": None, "degraded": False, "engine": "s1", "engine_ms": None},
                   moved["route"])


# ----------------------------------------------------------------------------------------------- local admission
# The S1 blast_radius levels that mean "no effect outside this machine" (core.ANCHORS["blast_radius"]): 0 "words only,
# nothing changes" and 1 "a bounded reversible workspace change". 2 ("a broad or difficult-to-reverse system change")
# is not bounded to the workspace and 3 is "law, money, production, or irreversible effects nearby", where core.resolve
# floors every outward request. Owner rule (Forest, 2026-11-13): the Pro's local models may take any band, sensitive
# and red-team work included, but only with no effect outside the Pro; the outward floor itself is core.resolve's.
LOCAL_BLAST_OK = (0, 1)


def local_admission(answers: Any, band: Any, degraded: Any) -> tuple[bool, str]:
    """THE gate for every AUTOMATIC move of a request to a local lane: (admitted, reason). The reason is metadata only
    (never request text), suitable for logs and the routes row.

    Callers: auto_local (a node's auto_route bands), sensitive_local (flag, prefer, keyword, the planned S1 signal),
    refusal_route (a refused opening turn), continuation_admission (every later turn of a conversation an automatic
    rule bound to a local lane; server.py), and index_local / _move_to_local (index-based local candidates). An explicit local pin (`sub/<local model or alias>`) is the client's choice and does not pass
    here. What the gate does not cover is per node and stays with the caller: the node's band list (auto_local), the
    wire (NO_LOCAL_KEY), and _node_why (exclusion, availability, the model listed, the context fit).

    Admitted only when ALL hold, and fails closed on anything else:
      degraded   exactly False (a degraded scorer is never local; None or a non-bool is not "not degraded")
      band       a non-empty string (a decision without a band is not a routed decision)
      answers    a dict holding `outward` exactly False (not 0, None or a string) and `blast_radius` exactly an int
                 (not a bool) in LOCAL_BLAST_OK. -1, 1.9, "1", True/False and a missing key are malformed: refused.
    `answers` are S1's raw answers (decision["answers"], after decide_s1's fail-up), not core.resolve's floored view."""
    if degraded is not False:
        return False, "S1 is degraded (a degraded scorer never routes local)" if degraded is True else \
            "S1's degraded flag is missing or malformed"
    if not isinstance(band, str) or not band:
        return False, "no band (S1 did not route this request)"
    if not isinstance(answers, dict):
        return False, "S1 gave no answers"
    if "outward" not in answers or "blast_radius" not in answers:
        return False, "S1 gave no outward / blast_radius answer"
    outward, br = answers["outward"], answers["blast_radius"]
    if outward is True:
        return False, "outward effect (S1 outward=true): never local"
    if outward is not False:
        return False, "S1's outward answer is malformed"
    if type(br) is not int:                          # bool is an int subclass: excluded by the exact type check
        return False, "S1's blast_radius answer is malformed"
    if br not in LOCAL_BLAST_OK:
        if br > max(LOCAL_BLAST_OK):
            return False, f"blast_radius {br} > {max(LOCAL_BLAST_OK)} (an effect outside the workspace)"
        return False, "S1's blast_radius answer is malformed"
    return True, f"no outward effect (blast_radius {br}, band {band})"


def route_admission(r: dict[str, Any] | None) -> tuple[bool, str]:
    """local_admission for a route or decision as routing builds them (execution_routing.select's result, a _moved
    route): S1's answers from r["decision"], the band from r["band"], degraded when either r or its decision says so.
    The decision's own flag must be present (exactly False); a route without a top-level flag defers to it."""
    r = r if isinstance(r, dict) else {}
    decision = r.get("decision") if isinstance(r.get("decision"), dict) else {}
    flags = [r.get("degraded", False), decision.get("degraded")]
    degraded = True if any(f is True for f in flags) else False if all(f is False for f in flags) else None
    return local_admission(decision.get("answers"), r.get("band"), degraded)


def continuation_text(body: dict[str, Any]) -> str:
    """What S1 scores for a continuation of a locally bound conversation: the latest user turn's own words (text
    blocks, client reminders removed), or, on a tool-result turn that has none, the latest earlier user words. The
    opening request is not rescored as such: it was admitted when the conversation was bound, and scoring it again
    would hide a new instruction ("now email the invoice") behind the old one."""
    for m in reversed(body.get("messages") or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        text = "\n".join(b.get("text", "") for b in wire._blocks(m.get("content"))
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
        text = strip_injected(text).strip()
        if text:
            return text
    return ""


# Bounds the re-check's added S1 latency in a tool loop: a turn whose scored text (continuation_text) is the same as
# one this conversation had admitted within CONTINUATION_CACHE_S reuses that admission (the same window
# execution_routing.select caches a score for). Only admissions are cached; a refusal is rescored every time.
CONTINUATION_CACHE_S = 180.0
_admitted: "OrderedDict[tuple[str, str], tuple[float, str]]" = OrderedDict()
_admitted_lock = threading.Lock()


def continuation_admission(core, body: dict[str, Any], route: dict[str, Any] | None) -> tuple[bool, str]:
    """A continuation of a conversation an AUTOMATIC rule bound to a local lane (auto_local, sensitive_local,
    refusal_route): S1 scores this turn (continuation_text) through core.decide, the same scorer call and the same
    bounded timeout and retry as the opening's routing call (core.S1_TIMEOUT_S, S1_RETRIES), and local_admission
    judges the answers under the band the conversation was bound with. (admitted, metadata reason). Fails closed: no
    text to score, a degraded or failed scorer, or any exception is not admitted. server.Handler runs it before every
    local dispatch of such a conversation; an explicit local pin (route["pinned"]) is never rescored.

    Not admitted, the turn is refused and the conversation stays bound (server.py). Moving the conversation to a cloud
    lane instead is #43's re-route seam (server.Handler._local_continuation_refused), not built here."""
    route = route if isinstance(route, dict) else {}
    text = continuation_text(body)
    if not text:
        return False, "no user text to score on this turn"
    key = (str(route.get("route_id") or ""), hashlib.sha256(text.encode()).hexdigest())
    with _admitted_lock:
        hit = _admitted.get(key) if key[0] else None
        if hit and _now() - hit[0] < CONTINUATION_CACHE_S:
            return True, hit[1] + " (admitted earlier in this conversation)"
    try:
        decision = core.decide(text, {"platform": PLATFORM, "unit_kind": "interactive"})
    except Exception as exc:                         # the scorer call failed: the same as a degraded scorer
        return False, f"S1 scoring failed ({type(exc).__name__})"
    if not isinstance(decision, dict):
        return False, "S1 gave no decision"
    deg = decision.get("degraded")
    ok, why = local_admission(decision.get("answers"), route.get("band"),
                              True if deg is True else False if deg is False else None)
    if ok and key[0]:
        with _admitted_lock:
            _admitted[key] = (_now(), why)
            _admitted.move_to_end(key)
            while len(_admitted) > 256:
                _admitted.popitem(last=False)
    return ok, why


def auto_local(core, body: dict[str, Any], r: dict[str, Any], *, client: str, conversation_id: str,
               excluded: Callable[[str], bool] = lambda name: False) -> dict[str, Any]:
    """S1's route `r`, or the same decision moved to a local node's auto_route model (local_lane.auto_routes) when every
    condition holds: local_admission admits S1's decision (not degraded, a band, no outward effect: outward exactly
    False, blast_radius exactly 0 or 1; a degraded decision keeps core's no-Claude rule and its route), the band is
    one the node lists, the request is on a wire the relay serves (no NO_LOCAL_KEY), and
    the local guards pass (_node_why: not excluded, node available and listing the model, request fits). The first
    node in config order that passes wins. A skip leaves `r` exactly as it was, with the reason logged; a broken local
    config skips too (logged as a warning), so a config error never blocks S1 routing. When moved, the routes row S1's
    select wrote is rewritten under the same route_id (_moved), so the record names what will run and loses nothing S1
    decided. When no manual auto_route takes it, the published proxy index gets its turn (index_local), behind the
    same early checks and the same admission."""
    if body.get(NO_LOCAL_KEY) or r.get("degraded") or not r.get("band") or not r.get("route_id"):
        return r
    try:
        targets = local_lane.auto_routes()
    except local_lane.LocalConfigError as exc:
        logger.warning("local auto-route skipped: local config error on node %s: %s", exc.node, exc)
        return r
    ok, why = route_admission(r)
    if not ok:
        logger.info("local auto-route not taken for %s: %s", r["route_id"], why)
        return r
    band = r["band"]
    need: list = []
    for node, entry, bands in targets:
        pin = local_lane.pin_id(entry)
        why, gate_refused = (f"band {band} is not in its auto_route bands", False) if band not in bands else \
            _node_why(node, entry, body, excluded, need)
        if why:
            logger.log(logging.WARNING if gate_refused else logging.INFO,
                       "local auto-route: %s/%s not taken for %s: %s", node["lane"], pin, r["route_id"], why)
            continue
        moved = _moved(r, node, entry,
                       f"local auto-route: band {band} is in {node['lane']}'s auto_route bands and the node is "
                       f"available; {pin} instead of {r.get('provider')}/{r.get('model')}")
        _log_moved(core, body, moved, client=client, conversation_id=conversation_id)
        logger.info("local auto-route: %s -> %s/%s (band %s)", r["route_id"], node["lane"], pin, band)
        return moved
    return index_local(core, body, r, client=client, conversation_id=conversation_id, excluded=excluded)
    return r


def _move_to_local(core, body: dict[str, Any], r: dict[str, Any], node: dict[str, Any], entry: dict[str, Any], *,
                   client: str, conversation_id: str, rule: str, chosen_extra: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
    """S1's route `r` moved to `entry` on `node` for the index route (index_local), or `r` unchanged when the shared
    local admission gate (route_admission → local_admission) does not admit S1's decision. The gate is applied here,
    at the move itself, so no index-based path can reach a local lane around it. The move is _moved's record with
    `chosen_extra` (variant, index, provenance) on the chosen entry, written by _log_moved under the same route_id.
    The result is not `pinned`: an index-bound conversation is an automatic bind, so server.Handler re-checks every
    later turn (_local_continuation_refused → continuation_admission)."""
    ok, why = route_admission(r)
    if not ok:
        logger.info("local index route: %s/%s not taken for %s: %s", node["lane"], local_lane.pin_id(entry),
                    r.get("route_id"), why)
        return r
    moved = _moved(r, node, entry, rule)
    if chosen_extra:
        moved["route"]["chosen"] = {**moved["route"]["chosen"], **chosen_extra}
    _log_moved(core, body, moved, client=client, conversation_id=conversation_id)
    logger.info("local index route: %s -> %s/%s (band %s)", r["route_id"], node["lane"], local_lane.pin_id(entry),
                r.get("band"))
    return moved


def _tag_key(tag: str | None) -> str:
    """An Ollama tag as the proxy table names it: case-insensitive, `:latest` optional (Ollama's implicit tag)."""
    t = str(tag or "").strip().casefold()
    return t[:-len(":latest")] if t.endswith(":latest") else t


def index_local(core, body: dict[str, Any], r: dict[str, Any], *, client: str, conversation_id: str,
                excluded: Callable[[str], bool] = lambda name: False) -> dict[str, Any]:
    """S1's route `r`, or the same decision moved to a local model variant whose published proxy index clears it
    (local_models in the generated scores). Called by auto_local after its early checks (wire, degraded scorer, band,
    route id) and the manual auto_route rule.

    Qualifying is the cloud rule: the variant's index for S1's use case >= S1's required index for this request.
    Hard rules: the shared local admission gate (route_admission → local_admission: not degraded, a band, outward
    exactly False, blast_radius exactly 0 or 1), applied before any probe and again in _move_to_local; never R3/R4
    (local_lane.AUTO_ROUTE_BANDS); and core's face floor does not apply to a local lane
    (core.resolve exempts local the same way). The think variant needs max_tokens >= local_lane.THINK_MIN_MAX_TOKENS;
    a client that asked for thinking gets only the think variant (its turn thinks either way).
    Cost: a local lane spends no subscription quota (core.resolve's weight for local is 0), so a qualifying variant
    wins over S1's cloud choice, the lighter variant first (lower general index: the selection's "pay by general
    index" — the non-thinking variant also answers sooner). It is not taken when its node is busy
    (local_lane.busy), down, does not list the model, the request outgrows the model's context, or the client excluded
    the model; then S1's route stands, with the skip logged (metadata only)."""
    route = r.get("route") or {}
    rid = r.get("route_id")
    skip = lambda what, why: logger.info("local index route: %s not taken for %s: %s", what, rid, why)
    band = r.get("band")
    req = route.get("required_index")
    use_case = (route.get("chosen") or {}).get("use_case")
    if band not in local_lane.AUTO_ROUTE_BANDS:
        return r                                   # R3/R4: outward, blast-radius-3 or escalation work never runs local
    ok, why = route_admission(r)                   # the shared gate, before any probe (_move_to_local applies it again)
    if not ok:
        skip("every local variant", why)
        return r
    if not isinstance(req, (int, float)) or not use_case:
        return r                                   # an unscored (table-fallback) route has no index to clear
    variants = (core.model_scores() or {}).get("local_models") or []
    if not variants:
        return r
    try:
        nodes = [n for n in local_lane.nodes() if n.get("index_route")]
    except local_lane.LocalConfigError as exc:
        logger.warning("local index route skipped: local config error on node %s: %s", exc.node, exc)
        return r
    asked = local_lane.client_asked_thinking(body)
    room = local_lane.think_budget(body.get("max_tokens")) is not None
    offers = []
    for n_i, node in enumerate(nodes):
        for e_i, entry in enumerate(node["models"]):
            for v in variants:
                if _tag_key(v.get("local_id")) != _tag_key(entry["id"]):
                    continue
                name, thinking = v.get("family"), v.get("thinking")
                idx = (v.get("index") or {}).get(use_case, (v.get("index") or {}).get("general"))
                why = None
                if thinking not in local_lane.VARIANT_THINKING:
                    why = f"unknown thinking {thinking!r}"
                elif entry.get("thinking") == "passthrough":
                    why = "its node entry is thinking: passthrough, so the variant's thinking cannot be set"
                elif asked and thinking != "on":
                    why = "the client asked for thinking: only a thinking variant matches what will run"
                elif thinking == "on" and not room:
                    why = (f"max_tokens {body.get('max_tokens')!r} < {local_lane.THINK_MIN_MAX_TOKENS}: no room to "
                           "think and answer")
                elif not isinstance(idx, (int, float)) or idx < req:
                    why = f"{use_case} index {idx} < required {req}"
                if why:
                    skip(f"{node['lane']}/{name}", why)
                    continue
                offers.append((float((v.get("index") or {}).get("general") or 0.0), n_i, e_i, node, entry, v, idx))
    offers.sort(key=lambda o: o[:3])
    need = None
    for _, _, _, node, entry, v, idx in offers:
        name, pin = v["family"], local_lane.pin_id(entry)
        what = f"{node['lane']}/{name}"
        if excluded(entry["id"]) or excluded(pin) or excluded(name):
            skip(what, "excluded by the client (x-cortados-exclude-model)")
            continue
        if _auto_down.get(node["node"], 0.0) > _now():
            skip(what, "probe failed within the last %gs" % AUTO_DOWN_S)
            continue
        if local_lane.busy(node):
            skip(what, f"node busy ({local_lane.inflight(node)} turn(s) in flight, max_inflight "
                       f"{node.get('max_inflight')})")
            continue
        try:
            tags = local_lane.probe(node)
        except local_lane.LocalUnavailable as exc:
            _auto_down[node["node"]] = _now() + AUTO_DOWN_S
            gate = str(exc) == local_lane.GATE_REFUSED.format(node=node["node"])
            logger.log(logging.WARNING if gate else logging.INFO,
                       "local index route: %s not taken for %s: unavailable (%s)", what, rid, exc)
            continue
        _auto_down.pop(node["node"], None)
        if entry["id"] not in tags:
            skip(what, f"does not list {entry['id']}")
            continue
        need = int(request_fit(body)["input_tokens"]) if need is None else need
        if need > entry["context_length"]:
            skip(what, f"request ≈{need // 1000}k tokens exceeds its {entry['context_length'] // 1000}k context")
            continue
        chosen = route.get("chosen") or {}
        rule = (f"local index route: {name} {use_case} index {idx} ({v.get('provenance')}, {v.get('published')}) "
                f"clears the required {req}; a local lane spends no quota, so it is taken instead of "
                f"{r.get('provider')}/{r.get('model')} (cost {chosen.get('cost')})")
        return _move_to_local(core, body, r, node, {**entry, "variant": name, "variant_thinking": v["thinking"]},
                              client=client, conversation_id=conversation_id, rule=rule,
                              chosen_extra={"variant": name, "index": idx, "use_case": use_case, "proxy": True,
                                            "provenance": v.get("provenance"), "weight": 0.0, "cost": 0.0})
    return r


# ---------------------------------------------------------------------------------------------- sensitive requests
# The x-cortados-local request header (both wires; the MCP route_task/start_task `local` option sends it):
#   sensitive  the request is sensitive: run it on the node's `sensitive.model` when it is eligible (below)
#   prefer     "local if eligible": the same eligibility and model, recorded as a preference rather than a sensitivity
LOCAL_HEADER = "x-cortados-local"
LOCAL_MODES = ("sensitive", "prefer")
# HOOK for the planned S1 "sensitive" signal (not built): the name of the S1 answer that marks a request sensitive.
# None = not wired. When S1 learns the question, set this to its answers key; sensitive_trigger then reports rule "s1"
# for a request S1 marks, and every eligibility rule below applies to it unchanged.
S1_SENSITIVE_ANSWER: str | None = None


def latest_user_text(body: dict[str, Any]) -> str:
    """The text of the request's latest user turn (text blocks only, client reminders removed): what the keyword list is
    matched against. The system prompt is deliberately not matched: it is the client's fixed instructions plus project
    files (CLAUDE.md), the same for every conversation of a project, so one word there would send every conversation
    local; a tool result is not the person's words either."""
    for m in reversed(body.get("messages") or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return strip_injected(c)
        if isinstance(c, list):
            return strip_injected("\n".join(b.get("text", "") for b in c
                                             if isinstance(b, dict) and b.get("type") == "text"
                                             and isinstance(b.get("text"), str)))
        return ""
    return ""


def sensitive_trigger(body: dict[str, Any], r: dict[str, Any], section: dict[str, Any], local: str | None,
                      text: str | None = None) -> str | None:
    """Which trigger marks this request for `section`'s node: "flag" (x-cortados-local: sensitive), "prefer"
    (x-cortados-local: prefer), "keyword" (the latest user turn matches the node's keyword list), "s1" (the planned S1
    signal, S1_SENSITIVE_ANSWER), or None. A keyword list that cannot be read is a warning naming the file, never a
    term, and matches nothing."""
    if local == "sensitive":
        return "flag"
    if local == "prefer":
        return "prefer"
    try:
        if local_lane.keyword_match(section, latest_user_text(body) if text is None else text):
            return "keyword"
    except local_lane.KeywordListError as exc:
        logger.warning("sensitive: keyword list skipped: %s", exc)
    ans = (r.get("decision") or {}).get("answers") or {}
    if S1_SENSITIVE_ANSWER and ans.get(S1_SENSITIVE_ANSWER) is True:
        return "s1"
    return None


def _not_admitted(r: dict[str, Any]) -> str | None:
    """route_admission as sensitive_local reads it: None when admitted, else why not."""
    ok, why = route_admission(r)
    return None if ok else why


def _rule_label(rule: str) -> str:
    return "local: prefer" if rule == "prefer" else f"sensitive: {rule}"


def sensitive_local(core, body: dict[str, Any], r: dict[str, Any], *, client: str, conversation_id: str,
                    excluded: Callable[[str], bool] = lambda name: False, local: str | None = None) -> dict[str, Any]:
    """S1's route `r`, or the same decision moved to a node's `sensitive.model` when a trigger marks the request
    (sensitive_trigger: flag, prefer, keyword) and it is eligible: on a wire the relay serves (no NO_LOCAL_KEY), with no
    effect outside this machine (local_admission; this also refuses a degraded scorer), and through the same local
    guards as auto_local (_node_why). Any band. The first node in config order that passes wins; the routes row is
    rewritten as auto_local does, with a `sensitive: <rule>` hard-rules entry. Every skip is logged as the rule and a
    metadata reason only, never the request text or the matched term.

    When the request is not moved but a node has `refusal_fallback` on and the request is eligible except for the
    node's live state, the route carries `refusal_fallback` (the candidate nodes, the conversation id and the client),
    which refusal_route reads if the cloud model refuses the opening turn."""
    if r.get("error") or not r.get("route_id") or r.get("pinned"):
        return r
    try:
        targets = local_lane.sensitive_routes()
    except local_lane.LocalConfigError as exc:
        logger.warning("sensitive: skipped: local config error on node %s: %s", exc.node, exc)
        return r
    rid = r["route_id"]
    if not targets:
        if local:
            logger.info("%s not taken for %s: no local node has a sensitive section", _rule_label(
                "flag" if local == "sensitive" else local), rid)
        return r
    static = "the request came in on /v1/responses (the local relay speaks Messages only)" \
        if body.get(NO_LOCAL_KEY) else _not_admitted(r)
    text = latest_user_text(body)
    need: list = []
    for node, entry, section in targets:
        rule = sensitive_trigger(body, r, section, local, text)
        if rule is None:
            continue
        why, gate_refused = (static, False) if static else _node_why(node, entry, body, excluded, need)
        if why:
            logger.log(logging.WARNING if gate_refused else logging.INFO, "%s: %s/%s not taken for %s: %s",
                       _rule_label(rule), node["lane"], local_lane.pin_id(entry), rid, why)
            continue
        pin = local_lane.pin_id(entry)
        moved = _moved(r, node, entry,
                       f"{_rule_label(rule)}: no outward effect and {node['lane']} is available; {pin} instead of "
                       f"{r.get('provider')}/{r.get('model')} (band {r.get('band')})")
        _log_moved(core, body, moved, client=client, conversation_id=conversation_id)
        logger.info("%s: %s -> %s/%s (band %s)", _rule_label(rule), rid, node["lane"], pin, r.get("band"))
        return moved
    if not static:
        cands = [{"node": n["node"], "model": e["id"]} for n, e, sec in targets
                 if sec["refusal_fallback"] and not excluded(e["id"]) and not excluded(local_lane.pin_id(e))]
        if cands:
            r = {**r, "refusal_fallback": {"targets": cands, "conversation_id": conversation_id, "client": client}}
    return r


def refusal_route(core, body: dict[str, Any], route: dict[str, Any] | None,
                  run: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """The opening turn of an S1-routed conversation was refused by its cloud model (server.py checks that it was the
    opening turn, on /v1/messages, with no tool run). Returns the route moved to a node's `sensitive.model`, or None.
    Eligibility was settled when the route was chosen (sensitive_local's `refusal_fallback` marker: no outward effect,
    not degraded, the client's exclusions) and local_admission is applied again to the route's own decision; here the
    node's live state is checked again (_node_why) and `run(moved)`
    must produce the local answer (True) before the routes row is rewritten, so a failed fallback leaves S1's record
    as it was. Logs the rule (`sensitive: refusal`) and metadata reasons only."""
    marker = (route or {}).get("refusal_fallback")
    rid = (route or {}).get("route_id")
    if not marker:
        logger.info("sensitive: refusal not taken for %s: not eligible when routed (no refusal_fallback node, an "
                    "outward effect, a degraded scorer, a pin, an exclusion or the /v1/responses wire)", rid)
        return None
    try:
        targets = local_lane.sensitive_routes()
    except local_lane.LocalConfigError as exc:
        logger.warning("sensitive: refusal not taken for %s: local config error on node %s: %s", rid, exc.node, exc)
        return None
    ok, why = route_admission(route)                 # the shared gate again: the marker alone is not admission
    if not ok:
        logger.info("sensitive: refusal not taken for %s: %s", rid, why)
        return None
    wanted = {(t.get("node"), t.get("model")) for t in marker.get("targets") or []}
    need: list = []
    for node, entry, section in targets:
        if not section["refusal_fallback"] or (node["node"], entry["id"]) not in wanted:
            continue
        pin = local_lane.pin_id(entry)
        why, gate_refused = _node_why(node, entry, body, lambda name: False, need)   # exclusions applied when routed
        if why is None:
            moved = _moved(route, node, entry,
                           f"sensitive: refusal: {route.get('provider')}/{route.get('model')} refused the opening "
                           f"turn; no outward effect and {node['lane']} is available; {pin} instead "
                           f"(band {route.get('band')})")
            moved.pop("refusal_fallback", None)
            if run(moved):
                _log_moved(core, body, moved, client=marker.get("client") or "",
                           conversation_id=marker.get("conversation_id") or "")
                logger.info("sensitive: refusal: %s -> %s/%s", rid, node["lane"], pin)
                return moved
            why = "the local model gave no usable answer"
        logger.log(logging.WARNING if gate_refused else logging.INFO, "sensitive: refusal: %s/%s not taken for %s: %s",
                   node["lane"], pin, rid, why)
    return None


def _now() -> float:
    return time.monotonic()


def _floor_exempt(lane: str) -> bool:
    """Lanes with no subscription quota: the client's floor is about subscription headroom only. A local node is named
    `local` on the S1-routed path and `local-<node>` (local_lane.LANE_PREFIX) on a local pin."""
    base = lane.split("#", 1)[0]
    return base == "local" or base.startswith(local_lane.LANE_PREFIX)


def _bind_codex(cand: dict[str, Any], codex_home: str | None) -> dict[str, Any]:
    """For a Codex candidate: {"codex_login": <canonical home>} resolved once, now, from the provider's --codex-home.
    The floor checks that identity and the route carries it, so the backend starts on that very directory
    (server.Registry.new) and every continuation is checked against it, never re-resolved. {} for other providers."""
    if str(cand.get("provider") or "").split("#", 1)[0] != "codex-cli":
        return {}
    login = floor.executing_login({**cand, "codex_home": codex_home, "codex_login": None})
    return {"codex_login": login} if login else {}


def floor_candidate(route: dict[str, Any], codex_home: str | None = None) -> dict[str, Any]:
    """The executing account of a chosen route, in the shape floor.block reads (lane, provider, account, config_dir, and
    the provider's --codex-home for a Codex route)."""
    chosen = (route.get("route") or {}).get("chosen") or {}
    return {"lane": chosen.get("lane"), "provider": route.get("provider") or chosen.get("provider"),
            "account": route.get("account", chosen.get("account")),
            "config_dir": route["config_dir"] if "config_dir" in route else chosen.get("config_dir"),
            "codex_home": codex_home,
            # A conversation opened under a floor carries the canonical home its backend runs on: that is its identity.
            **({"codex_login": route["codex_home"]} if route.get("codex_home") else {})}


def floor_turn(core, route: dict[str, Any] | None, min_left_pct: float, codex_home: str | None = None) -> str | None:
    """A continuation's paid turn under the floor: recheck and reserve its session's executing account (the one
    account this conversation runs on), before the turn is sent to the backend."""
    if not route or not ((route.get("route") or {}).get("chosen") or {}).get("lane"):
        return "the conversation's executing account is unknown; cannot check x-cortados-min-left-pct"
    if _floor_exempt(route["route"]["chosen"]["lane"]):
        return None
    if str(route.get("provider") or "").split("#", 1)[0] == "codex-cli" and not route.get("codex_home"):
        # A Codex continuation must name the home its backend was frozen on (server.Registry.new): resolving it again
        # could accept another login's reading for a backend still running on the first (Codex review fu2).
        return "the conversation's executing Codex home is unbound; cannot check x-cortados-min-left-pct"
    return floor.reserve(core, floor_candidate(route, codex_home), route.get("model") or "", min_left_pct,
                         conv=route.get("route_id"))
