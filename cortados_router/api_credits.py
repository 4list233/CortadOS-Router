"""Anthropic API-credit lane (#114): spend free promotional credit, and only that, never anything that could be billed.

This module owns the lane's ACCOUNTING; it never talks to the network. The provider (server._api_turn) reserves an
upper-bound cost here before a request is sent to api.anthropic.com and settles the real cost from the response's
usage after it.

Budget (owner, 2026-11-14 and 2026-11-22). The credit is a set of promotional GRANTS, each with an amount and an expiry,
plus the organisation's monthly spend limit (UTC month). They are configured by the owner in a small JSON file
(config_path(): ~/.config/cortados/api-credit.json; example tables/api-credit.example.json; never the key):

  * A grant COUNTS from `granted` until EXPIRY_MARGIN_S before `expires`. Every upstream turn has a hard wall-clock
    cap MAX_TURN_S < EXPIRY_MARGIN_S (anthropic_proxy aborts it there and the reservation is then kept in full), and
    reserve() admits a turn only when its cap ends EXPIRY_SLACK_S before the earliest counting grant expires, so no
    turn admitted under a grant can still be running when it expires. After the last grant stops counting the lane
    is off until the owner adds a new grant: credit is never assumed to refill.
  * Grant budget G = the sum of the counting grants' amounts. Grant use U_G = what the owner saw used outside this
    ledger (amount - remaining_usd, when a grant states its remaining as of a date) + every dollar this ledger committed
    (spent + reserved) since the earliest counting grant's start (its remaining_as_of, else its granted time; the whole
    UTC day, so earlier spend that day is counted too). Spend on a grant that has since expired stays counted: that
    can only make the remaining smaller, never larger.
  * Month budget M = the org monthly limit. Month use U_M = this ledger's committed dollars from the last UTC day of the
    previous month on (a turn reserved just before midnight on the 1st may be billed in the new month).
  * STOP LINE (hard stop, owner rule): a request is admitted only when, after reserving it, U_G <= 95% of G AND
    U_M <= 95% of M. headroom = min(0.95 G - U_G, 0.95 M - U_M). headroom <= 0 turns the lane off by itself.
  * Alerts at 50 / 80 / 95 % of the binding budget: pct_used = max(U_G / G, U_M / M).

Ledger (ledger_path(): $CORTADOS_HOME/api-credit-ledger.json), one entry per UTC day: {"spent_usd", "reserved":
{reservation id: usd}}. Every read-modify-write is under an exclusive flock, so concurrent requests (threads of the
HTTP server and other processes) cannot together pass the stop line: each reservation is checked against everything
already spent and reserved. A reservation is settled exactly once with the real cost (an overrun is recorded, never
clamped), or kept at its full amount when the cost is unknown. FAIL CLOSED: a missing config, an unreadable / corrupt
config or ledger, a ledger that cannot be written, a second credit key, and a ledger that no longer holds a grant's
window all turn the lane off (LedgerError / ConfigError; state() says which).

Per-request cost estimate (estimate_max_cost) is an UPPER bound: every byte of the serialized request counts as one
input token (a token is never shorter than one byte), plus a template allowance and per-tool allowance, priced at the
cache-write rate (the dearest input treatment); max_tokens (or the model's 128k ceiling) as output; fast mode, data
residency and the long-context tier priced in; no cache-read or batch discount assumed. A request carrying server-side
`fallbacks` is refused (REFUSED_FIELDS), so one reservation covers one model's one attempt. Content whose cost the request
bytes cannot bound (a server-executed tool, a URL or Files API source, a PDF, an image whose size cannot be read) is
refused (Unbounded). Settlement prices the real usage the same way (cost_from_usage), so the ledger never under-counts.

Stdlib only.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

KEY_ENV = "CORTADOS_ANTHROPIC_API_KEY"          # the credit key: read from the provider's environment only
CONFIG_ENV = "CORTADOS_API_CREDIT_CONFIG"       # override of the grants file's path (tests)
LANE = "anthropic-api"

STOP_FRACTION = 0.95                           # owner 2026-11-14: hard stop at 95% of the budget; a config may lower it
ALERT_LEVELS = (50, 80, 95)                    # owner 2026-11-14: dashboard alerts
EXPIRY_MARGIN_S = 3600.0                       # a grant stops counting an hour before it expires
# #114 review round 1: the hard WALL-CLOCK cap of one upstream turn (anthropic_proxy aborts the turn there, whatever the
# socket does), strictly below the margin, so a turn admitted while its grant counts has ended before the grant expires.
MAX_TURN_S = 1800.0
EXPIRY_SLACK_S = EXPIRY_MARGIN_S - MAX_TURN_S  # what is left between a capped turn's end and the grant's expiry
assert 0 < MAX_TURN_S < EXPIRY_MARGIN_S
MONTH_SPILL_DAYS = 1                           # the previous month's last UTC day also counts against this month
DEFAULT_PREFER_DAYS = 3.0                      # expiry preference window (see prefer_window_s)
KEEP_DAYS = 400                                # ledger days older than this with nothing reserved are pruned
LEDGER_VERSION = 2


# ------------------------------------------------------------------------------------------------------ pricing
def _entry(inp: float, out: float) -> dict[str, float]:
    # cache_write at the 1-hour rate (input x2, the higher write rate); cache_read bounded above by input x0.1.
    return {"input": inp, "output": out, "cache_read": round(inp * 0.1, 4), "cache_write": inp * 2}


# USD per 1,000,000 tokens (claude-api skill, model table cached 2026-11-03), priced conservatively.
PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-2": _entry(8.0, 40.0),
    "claude-mythos-5-2": _entry(8.0, 40.0),
    "claude-fable-5-1": _entry(10.0, 50.0),
    "claude-fable-5": _entry(10.0, 50.0),
    "claude-mythos-5-1": _entry(10.0, 50.0),
    "claude-opus-5-5": _entry(4.0, 20.0),
    "claude-opus-5": _entry(5.0, 25.0),
    "claude-opus-4-8": _entry(5.0, 25.0),
    "claude-opus-4-7": _entry(5.0, 25.0),
    "claude-opus-4-6": _entry(5.0, 25.0),
    "claude-sonnet-5-5": _entry(2.0, 10.0),
    "claude-sonnet-5": _entry(2.0, 10.0),
    "claude-sonnet-4-6": _entry(3.0, 15.0),
    "claude-haiku-5-5": _entry(0.50, 2.50),  # the >100k-token tier, so the guard never under-counts Haiku 5.5
    "claude-haiku-4-5": _entry(1.0, 5.0),
}
DEFAULT_PRICE = _entry(10.0, 50.0)  # unknown model -> most expensive tier
# Models whose PRICES entry already is their dearest tier (no long-context premium on top).
FLAT_TIER = frozenset({"claude-haiku-5-5"})
LONG_CONTEXT_TOKENS = 200_000      # above this many input tokens a long-context tier may apply: priced in
LONG_INPUT_FACTOR, LONG_OUTPUT_FACTOR = 2.0, 1.5
FAST_FACTOR = 2.0                  # fast mode: $8/$40 on Opus 5.5 ($4/$20), $16/$80 on Fable 5.2 ($8/$40)
GEO_FACTOR = 1.1                   # an explicit inference_geo (data residency) is billed at a premium
MAX_OUTPUT_TOKENS = 128_000        # no max_tokens given: the models' largest output
TEMPLATE_TOKENS = 4000             # what the API adds that the body does not carry (tool-use system prompt etc.)
PER_TOOL_TOKENS = 1500             # per declared tool, on top of its schema's own bytes
IMAGE_PIXELS_PER_TOKEN = 750.0     # Anthropic's vision sizing: tokens ~ width x height / 750 (resizing only lowers it)
IMAGE_TOKEN_FACTOR, IMAGE_TOKEN_EXTRA = 1.5, 200
IMAGE_MAX_BYTES = 32 * 1024 * 1024  # the API's whole-request limit
IMAGE_MAX_PIXELS = 400_000_000

# Per-request charges the API bills for server-side tools ON TOP OF tokens (USD each), priced conservatively.
SERVER_TOOL_USD: dict[str, float] = {
    "web_search_requests": 0.01,
}
DEFAULT_SERVER_TOOL_USES = 20  # a declared web-search tool with no max_uses: reserve AND enforce this many calls

# Typed tools the CLIENT executes (their cost is only their schema, which the request bytes and PER_TOOL_TOKENS bound).
# Every other typed tool is executed and billed by the API (web search / fetch pull unbounded content into the turn's
# input, code execution bills container time, an advisor or MCP toolset runs further calls): refused on this lane.
CLIENT_TOOL_TYPES = ("custom", "bash_", "text_editor_", "computer_", "memory_", "tool_search_tool_")
# Top-level request fields whose cost the request bytes cannot bound: refused.
UNBOUNDED_FIELDS = ("mcp_servers", "container", "thread")
# Top-level fields refused whenever PRESENT (any value, empty or null included). `fallbacks` (server-side fallback models,
# #114 review round 1): every attempt is billed, each fallback may carry its own max_tokens and speed, and the bill comes
# back as usage.iterations; one reservation cannot bound that, so a request carrying it is never sent on this lane.
REFUSED_FIELDS = ("fallbacks",)


class LedgerError(RuntimeError):
    """The ledger could not be read or written; the caller must fail closed (refuse admission, lane off)."""


class ConfigError(LedgerError):
    """The grants config is missing, unreadable or invalid; the lane is off until the owner fixes it."""


class Unbounded(ValueError):
    """The request carries something whose cost cannot be bounded from its bytes; the message is safe to show."""


def unbounded_server_tools(body: dict[str, Any] | None) -> list[str]:
    """The typed tools in `body` that the API itself executes and bills (any type not in CLIENT_TOOL_TYPES). A request
    declaring one is refused on the credit lane: its spend is unknowable in advance and not recoverable from usage."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return []
    bad: list[str] = []
    for t in tools:
        if not isinstance(t, dict) or "type" not in t:
            continue                                   # a client tool: name, description, input_schema
        ttype = str(t.get("type") or "").lower()
        if ttype and not ttype.startswith(CLIENT_TOOL_TYPES):
            bad.append(ttype)
        elif not ttype:
            bad.append("(empty type)")
    return bad


def enforce_server_tool_caps(body: dict[str, Any]) -> dict[str, Any]:
    """Return `body` (a shallow copy if anything changed) with every metered server tool carrying an explicit cap
    equal to what the meter reserves for it. (Every server tool is refused today: unbounded_server_tools; kept so a
    tool moved out of that set is capped the moment it is.)"""
    if not isinstance(body, dict):
        return body
    tools = body.get("tools")
    if not isinstance(tools, list):
        return body
    changed = False
    new_tools: list[Any] = []
    for t in tools:
        if isinstance(t, dict) and "web_search" in str(t.get("type") or "").lower():
            try:
                uses = int(t.get("max_uses") or 0)
            except (TypeError, ValueError):
                uses = 0
            if uses <= 0:
                t = dict(t, max_uses=DEFAULT_SERVER_TOOL_USES)
                changed = True
        new_tools.append(t)
    if not changed:
        return body
    out = dict(body)
    out["tools"] = new_tools
    return out


def price_for(model: str) -> dict[str, float]:
    m = (model or "").strip().lower()
    if m in PRICES:
        return PRICES[m]
    for base in sorted(PRICES, key=len, reverse=True):      # longest first: claude-opus-5-5 before claude-opus-5
        if m.startswith(base):
            return PRICES[base]
    return DEFAULT_PRICE


def _flat(model: str) -> bool:
    m = (model or "").strip().lower()
    return any(m == f or m.startswith(f + "-") for f in FLAT_TIER)


def _n(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def _factors(model: str, input_total: float, *, fast: bool, geo: bool) -> tuple[float, float]:
    """(input multiplier, output multiplier) on top of PRICES for one turn."""
    fi = fo = 1.0
    if input_total > LONG_CONTEXT_TOKENS and not _flat(model):
        fi, fo = LONG_INPUT_FACTOR, LONG_OUTPUT_FACTOR
    if fast:
        fi, fo = fi * FAST_FACTOR, fo * FAST_FACTOR
    if geo:
        fi, fo = fi * GEO_FACTOR, fo * GEO_FACTOR
    return fi, fo


def cost_usd(model: str, input_tokens: float = 0, output_tokens: float = 0, cache_read_tokens: float = 0,
             cache_creation_tokens: float = 0, *, fast: bool = False, geo: bool = False) -> float:
    p = price_for(model)
    i, o, cr, cw = _n(input_tokens), _n(output_tokens), _n(cache_read_tokens), _n(cache_creation_tokens)
    fi, fo = _factors(model, i + cr + cw, fast=fast, geo=geo)
    total = (i * p["input"] + cr * p["cache_read"] + cw * p["cache_write"]) * fi + o * p["output"] * fo
    return total / 1_000_000.0


def server_tool_cost(usage: dict[str, Any] | None) -> float:
    """Per-request charges the API billed for server-side tools (e.g. web search), read from usage.server_tool_use."""
    u = usage if isinstance(usage, dict) else {}
    stu = u.get("server_tool_use")
    if not isinstance(stu, dict):
        return 0.0
    return sum(SERVER_TOOL_USD.get(k, 0.0) * _n(v) for k, v in stu.items())


def _usage_geo(u: dict[str, Any], geo: bool) -> bool:
    if not geo and u.get("inference_geo"):
        geo = str(u.get("inference_geo")).lower() not in ("global", "not_available")
    return geo


def _block_cost(model: str, u: dict[str, Any], *, geo: bool, fast: bool = False) -> float:
    fast = fast or str(u.get("speed") or "").lower() == "fast"
    return cost_usd(model, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0),
                    u.get("cache_creation_input_tokens", 0), fast=fast, geo=_usage_geo(u, geo))


def _iteration_model(it: dict[str, Any], model: str) -> str:
    """The model an iteration is priced at: the dearer of the one it names and the requested one; an iteration naming no
    model (or not a Claude model) is priced at DEFAULT_PRICE, the dearest tier (iterations are unexpected on this lane:
    `fallbacks` is refused, so doubt is priced up, never down)."""
    named = it.get("model")
    if not isinstance(named, str) or not named.strip().lower().startswith("claude-"):
        return "unknown"
    pn, pm = price_for(named), price_for(model)
    return named if (pn["input"], pn["output"]) >= (pm["input"], pm["output"]) else model


def cost_from_usage(model: str, usage: dict[str, Any] | None, *, geo: bool = False) -> float:
    """The cost of a finished turn from its usage block, priced like the estimate (long-context tier, fast mode, data
    residency, cache writes at the 1-hour rate), so a settled charge is never below the bill.

    usage.iterations (#114 review round 1): when the API reports billed iterations (server-side fallbacks, compaction or
    any other multi-attempt turn), each is priced at its own model's rates (_iteration_model: never below the requested
    model, an unnamed model at the dearest tier) and its own speed, and the charge is the LARGER of their sum and the
    top-level usage priced at the requested model, so whichever of the two the API meant as the bill is covered."""
    u = usage if isinstance(usage, dict) else {}
    base = _block_cost(model, u, geo=geo)
    its = u.get("iterations")
    if isinstance(its, list) and its:
        top_fast = str(u.get("speed") or "").lower() == "fast"
        total = 0.0
        for it in its:
            if not isinstance(it, dict):
                continue
            total += _block_cost(_iteration_model(it, model), it, geo=geo or _usage_geo(u, False), fast=top_fast) \
                + server_tool_cost(it)
        base = max(base, total)
    return base + server_tool_cost(u)


def _image_tokens(src: dict[str, Any]) -> int:
    from cortados_router import image_check
    try:
        info, _ = image_check.check_base64(src.get("media_type"), src.get("data"), max_bytes=IMAGE_MAX_BYTES,
                                           max_pixels=IMAGE_MAX_PIXELS)
    except image_check.ImageRefused as exc:
        raise Unbounded(f"an image whose size cannot be bounded ({exc})") from None
    return int(math.ceil(info["width"] * info["height"] / IMAGE_PIXELS_PER_TOKEN * IMAGE_TOKEN_FACTOR)) + \
        IMAGE_TOKEN_EXTRA


def _bounded(x: Any, extra: list[int]) -> Any:
    """`x` with every inline image's base64 data blanked (its tokens are appended to `extra` from its pixel size), or
    Unbounded for any source whose content the request does not carry (a URL, a Files API file, a PDF)."""
    if isinstance(x, list):
        return [_bounded(v, extra) for v in x]
    if not isinstance(x, dict):
        return x
    if x.get("type") == "container_upload":
        raise Unbounded("a container upload")
    src = x.get("source")
    out = {k: _bounded(v, extra) for k, v in x.items() if k != "source"}
    if "source" in x:
        if not isinstance(src, dict):
            raise Unbounded("a content source of unknown shape")
        kind = src.get("type")
        if kind == "base64":
            media = str(src.get("media_type") or "")
            if not media.startswith("image/"):
                raise Unbounded(f"an inline {media or 'untyped'} document (page images are not bounded by its bytes)")
            extra.append(_image_tokens(src))
            out["source"] = {**src, "data": ""}
        elif kind in ("text", "content"):
            out["source"] = _bounded(src, extra)
        else:
            raise Unbounded(f"a {kind!r} source (its content is not in the request)")
    return out


def estimate_max_cost(body: dict[str, Any], model: str) -> float:
    """An UPPER bound on one request's cost (see the module doc). Raises Unbounded when no bound exists."""
    if not isinstance(body, dict):
        raise Unbounded("the request is not a JSON object")
    bad = [f for f in UNBOUNDED_FIELDS if body.get(f)]
    if bad:
        raise Unbounded(f"request field(s) {', '.join(bad)}")
    refused = [f for f in REFUSED_FIELDS if f in body]
    if refused:
        raise Unbounded(f"request field(s) {', '.join(refused)} (server-side fallbacks bill every attempt, each with its "
                        "own model, max_tokens and speed; the credit lane sends one model per request)")
    tools = body.get("tools")
    servers = unbounded_server_tools(body)
    if servers:
        raise Unbounded(f"server-executed tool(s) {', '.join(sorted(set(servers)))}")
    extra: list[int] = []
    stripped = _bounded(body, extra)
    n_tools = len(tools) if isinstance(tools, list) else 0
    input_tokens = (len(json.dumps(stripped).encode("utf-8")) + sum(extra) + TEMPLATE_TOKENS
                    + PER_TOOL_TOKENS * n_tools)
    try:
        max_out = int(body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        max_out = 0
    if max_out <= 0:
        max_out = MAX_OUTPUT_TOKENS
    fast = str(body.get("speed") or "").lower() == "fast"
    geo = bool(body.get("inference_geo"))
    p = price_for(model)
    fi, fo = _factors(model, input_tokens, fast=fast, geo=geo)
    in_rate = max(p["input"], p["cache_write"]) * fi
    return (input_tokens * in_rate + max_out * p["output"] * fo) / 1_000_000.0 + _server_tool_allowance(tools)


def _server_tool_allowance(tools: Any) -> float:
    """Dollar reserve for any declared web-search tool: its max_uses (or DEFAULT_SERVER_TOOL_USES) at $0.01."""
    if not isinstance(tools, list):
        return 0.0
    allow = 0.0
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "web_search" not in str(t.get("type") or "").lower():   # a server tool is identified by `type`
            continue
        try:
            uses = int(t.get("max_uses") or 0)
        except (TypeError, ValueError):
            uses = 0
        if uses <= 0:
            uses = DEFAULT_SERVER_TOOL_USES
        allow += uses * SERVER_TOOL_USD["web_search_requests"]
    return allow


# ------------------------------------------------------------------------------------------------------- key
def api_key() -> str:
    """The credit key, from the provider's environment (KEY_ENV). Never logged, never written, never passed on."""
    return (os.environ.get(KEY_ENV) or "").strip()


def key_problem() -> str | None:
    """Why the configured key cannot be used, or None. A second key (the pre-#114 multi-org slots) is refused: the
    grants file describes ONE organisation, and spend on a second key would not be counted against its grants."""
    if not api_key():
        return f"no API key ({KEY_ENV} is not set in the provider's environment)"
    if (os.environ.get(KEY_ENV + "_2") or "").strip():
        return f"a second credit key ({KEY_ENV}_2) is set; the grants file covers one organisation: remove it"
    return None


def configured_orgs() -> list[tuple[str, str]]:
    """[("org", key)] when the one credit key is usable, else []. (Kept for callers of the pre-#114 interface.)"""
    return [("org", api_key())] if key_problem() is None else []


# ------------------------------------------------------------------------------------------------------ config
def config_path() -> Path:
    raw = os.environ.get(CONFIG_ENV)
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "cortados" / "api-credit.json"


def _when(v: Any, what: str) -> float:
    """An ISO 8601 time WITH a UTC offset (e.g. 2026-12-02T19:00:00-05:00) as epoch seconds; ConfigError otherwise."""
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"{what} must be an ISO 8601 time with a UTC offset")
    s = v.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ConfigError(f"{what} is not an ISO 8601 time: {v[:40]!r}") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ConfigError(f"{what} has no UTC offset (a time without one is ambiguous): {v[:40]!r}")
    return dt.timestamp()


def _money(v: Any, what: str, *, positive: bool) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0 or (positive and v == 0):
        raise ConfigError(f"{what} must be a {'positive' if positive else 'non-negative'} number of dollars")
    return float(v)


def parse_config(raw: Any) -> dict[str, Any]:
    """The grants config, validated strictly (any doubt is a ConfigError: the lane stays off)."""
    if not isinstance(raw, dict):
        raise ConfigError("the API credit config must be a JSON object")
    known = {"_comment", "enabled", "grants", "org_monthly_limit_usd", "stop_fraction", "prefer_within_days"}
    unknown = sorted(k for k in raw if k not in known and not str(k).startswith("_"))
    if unknown:
        raise ConfigError(f"unknown config field(s): {', '.join(unknown)}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be true or false")
    limit = _money(raw.get("org_monthly_limit_usd"), "org_monthly_limit_usd", positive=True)
    stop = raw.get("stop_fraction", STOP_FRACTION)
    if not isinstance(stop, (int, float)) or isinstance(stop, bool) or not (0 < stop <= STOP_FRACTION):
        raise ConfigError(f"stop_fraction must be above 0 and at most {STOP_FRACTION} (owner rule: never over 95%)")
    prefer = raw.get("prefer_within_days", DEFAULT_PREFER_DAYS)
    if not isinstance(prefer, (int, float)) or isinstance(prefer, bool) or not math.isfinite(prefer) or prefer < 0:
        raise ConfigError("prefer_within_days must be a non-negative number of days")
    grants_raw = raw.get("grants")
    if not isinstance(grants_raw, list):
        raise ConfigError("grants must be a list")
    grants = []
    for i, g in enumerate(grants_raw):
        if not isinstance(g, dict):
            raise ConfigError(f"grant {i} must be an object")
        gknown = {"name", "amount_usd", "granted", "expires", "remaining_usd", "remaining_as_of"}
        extra = sorted(k for k in g if k not in gknown and not str(k).startswith("_"))
        if extra:
            raise ConfigError(f"grant {i}: unknown field(s): {', '.join(extra)}")
        name = g.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ConfigError(f"grant {i}: name must be a non-empty string")
        amount = _money(g.get("amount_usd"), f"grant {name!r} amount_usd", positive=True)
        granted = _when(g.get("granted"), f"grant {name!r} granted")
        expires = _when(g.get("expires"), f"grant {name!r} expires")
        if expires <= granted:
            raise ConfigError(f"grant {name!r} expires before it is granted")
        has_rem, has_asof = "remaining_usd" in g, "remaining_as_of" in g
        if has_rem != has_asof:
            raise ConfigError(f"grant {name!r}: remaining_usd and remaining_as_of go together")
        remaining, since = amount, granted
        if has_rem:
            remaining = _money(g["remaining_usd"], f"grant {name!r} remaining_usd", positive=False)
            if remaining > amount:
                raise ConfigError(f"grant {name!r}: remaining_usd is more than amount_usd")
            since = _when(g["remaining_as_of"], f"grant {name!r} remaining_as_of")
            if since < granted:
                raise ConfigError(f"grant {name!r}: remaining_as_of is before granted")
        grants.append({"name": name.strip(), "amount_usd": amount, "granted": granted, "expires": expires,
                       "used_outside_usd": round(amount - remaining, 6), "counted_from": since})
    return {"enabled": enabled, "grants": grants, "org_monthly_limit_usd": limit, "stop_fraction": float(stop),
            "prefer_within_days": float(prefer)}


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or config_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        raise ConfigError("no API credit config (the owner lists the grants in ~/.config/cortados/api-credit.json)") \
            from None
    except OSError as exc:
        raise ConfigError(f"the API credit config cannot be read ({type(exc).__name__})") from None
    try:
        raw = json.loads(text)
    except ValueError:
        raise ConfigError("the API credit config is not valid JSON") from None
    return parse_config(raw)


# ------------------------------------------------------------------------------------------------------ ledger
def _state_dir() -> Path:
    return Path(os.environ.get("CORTADOS_HOME") or os.environ.get("CORTADOS_HERMES_HOME") or (Path.home() / ".cortados"))


def ledger_path() -> Path:
    return _state_dir() / "api-credit-ledger.json"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _valid_day(s: Any) -> bool:
    if not isinstance(s, str) or len(s) != 10:
        return False
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except ValueError:
        return False


def _finite_nonneg(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _blank() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "days": {}, "pruned_through": ""}


def _validate(data: Any) -> dict[str, Any]:
    """Strict schema check for every read: anything unexpected is a LedgerError (fail closed)."""
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION or not isinstance(data.get("days"), dict):
        raise LedgerError("api-credit ledger is corrupt (unexpected shape or version)")
    cursor = data.get("pruned_through")
    if not isinstance(cursor, str) or (cursor and not _valid_day(cursor)):
        raise LedgerError("api-credit ledger has an invalid prune cursor")
    for day, led in data["days"].items():
        if not _valid_day(day) or not isinstance(led, dict) or not _finite_nonneg(led.get("spent_usd")) \
                or not isinstance(led.get("reserved"), dict):
            raise LedgerError("api-credit ledger has an invalid day entry")
        for rid, amt in led["reserved"].items():
            if not isinstance(rid, str) or not rid.startswith(day + ":") or not _finite_nonneg(amt):
                raise LedgerError("api-credit ledger has an invalid reservation")
    return data


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _blank()
    try:
        return _validate(json.loads(path.read_text()))
    except LedgerError:
        raise
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        raise LedgerError(f"api-credit ledger unreadable ({type(exc).__name__})") from None


def _locked(path: Path, fn: Callable[[dict[str, Any]], Any], *, write: bool = True) -> Any:
    """Run fn on the ledger under an exclusive file lock, persisting whatever fn leaves in `data` (atomically: a
    unique temp file, fsynced, renamed over). Any read/parse/write error is a LedgerError."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.with_name(path.name + ".lock"), "a")
    except OSError as exc:
        raise LedgerError(f"cannot open the api-credit ledger lock ({type(exc).__name__})") from None
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _read(path)
        out = fn(data)
        if write:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                with open(tmp, "w") as f:
                    f.write(json.dumps(data, sort_keys=True))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except OSError as exc:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise LedgerError(f"api-credit ledger write failed ({type(exc).__name__})") from None
        return out
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _up(x: float) -> float:
    """`x` rounded UP to a millionth of a dollar: what the ledger stores is never below what was spent or reserved."""
    return math.ceil(x * 1_000_000 - 1e-6) / 1_000_000 if x > 0 else 0.0


def _committed(led: dict[str, Any]) -> float:
    return float(led.get("spent_usd") or 0.0) + sum(float(v) for v in (led.get("reserved") or {}).values())


def _sum_since(data: dict[str, Any], first_day: str) -> float:
    return sum(_committed(led) for day, led in data["days"].items() if day >= first_day)


def _month_first_day(now: float) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc) - timedelta(days=MONTH_SPILL_DAYS)
    return start.strftime("%Y-%m-%d")


def _month_end(now: float) -> float:
    dt = datetime.fromtimestamp(now, timezone.utc)
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc).timestamp()


def counting(cfg: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """The grants that count at `now`: granted, and more than EXPIRY_MARGIN_S before their expiry."""
    return [g for g in cfg["grants"] if g["granted"] <= now < g["expires"] - EXPIRY_MARGIN_S]


def _numbers(cfg: dict[str, Any], data: dict[str, Any], now: float) -> dict[str, Any]:
    """The budget arithmetic of the module doc, on a validated config and ledger. Raises LedgerError when the ledger
    no longer holds a window it must sum (pruned)."""
    grants = counting(cfg, now)
    stop = cfg["stop_fraction"]
    month_from = _month_first_day(now)
    cursor = data.get("pruned_through") or ""
    if cursor and month_from <= cursor:
        raise LedgerError("the ledger no longer holds this month's days (pruned); refusing")
    month_used = _sum_since(data, month_from)
    month_limit = cfg["org_monthly_limit_usd"]
    out: dict[str, Any] = {
        "month": {"limit_usd": month_limit, "used_usd": round(month_used, 6),
                  "stop_line_usd": round(stop * month_limit, 6), "resets_at": _month_end(now)},
        "grants": [{"name": g["name"], "amount_usd": g["amount_usd"], "expires_at": g["expires"],
                    "counting": g in grants} for g in cfg["grants"]],
        "stop_fraction": stop,
    }
    if not grants:
        out.update(grant=None, headroom_usd=0.0, pct_used=None)
        return out
    first = min(_day(g["counted_from"]) for g in grants)
    if cursor and first <= cursor:
        raise LedgerError("the ledger no longer holds a counting grant's window (pruned); refusing")
    amount = sum(g["amount_usd"] for g in grants)
    grant_used = sum(g["used_outside_usd"] for g in grants) + _sum_since(data, first)
    headroom = min(stop * amount - grant_used, stop * month_limit - month_used)
    pct = 100.0 * max(grant_used / amount, month_used / month_limit)
    out.update(grant={"amount_usd": amount, "used_usd": round(grant_used, 6), "stop_line_usd": round(stop * amount, 6),
                      "left_usd": round(max(0.0, amount - grant_used), 6),
                      "expires_at": min(g["expires"] for g in grants),
                      "stops_counting_at": min(g["expires"] for g in grants) - EXPIRY_MARGIN_S,
                      "granted_at": min(g["granted"] for g in grants)},
               headroom_usd=round(headroom, 6), pct_used=round(pct, 2))
    return out


def alert_level(pct_used: float | None) -> int:
    """The highest ALERT_LEVELS threshold reached (0 below 50 %)."""
    if pct_used is None:
        return 0
    return max([lvl for lvl in ALERT_LEVELS if pct_used >= lvl] or [0])


def _writable(path: Path) -> bool:
    d = path.parent
    if not d.exists():
        return os.access(d.parent if d.parent.exists() else d, os.W_OK)
    lock = path.with_name(path.name + ".lock")
    return os.access(d, os.W_OK) and all(not p.exists() or os.access(p, os.W_OK) for p in (path, lock))


def state(now: float, *, path: Path | None = None, config: Path | None = None) -> dict[str, Any]:
    """The lane's state for the live view, the dashboard and routing: {"on", "reason", ...numbers}. Metadata only
    (never the key, never a request). Every failure turns the lane off with the reason; this never raises."""
    p = path or ledger_path()
    out: dict[str, Any] = {"lane": LANE, "on": False, "reason": None, "alert_level": 0, "pct_used": None,
                           "headroom_usd": 0.0, "expiring_soon": False, "prefer_within_days": None,
                           "reserved_usd": None, "open_reservations": None, "as_of": round(now, 3)}
    why = key_problem()
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        out["reason"] = str(exc)
        return out
    out["prefer_within_days"] = cfg["prefer_within_days"]
    try:
        data = _read(p)
        nums = _numbers(cfg, data, now)
    except LedgerError as exc:
        out["reason"] = f"ledger: {exc}"
        return out
    out.update(nums)
    out["reserved_usd"] = round(sum(sum(led["reserved"].values()) for led in data["days"].values()), 6)
    out["open_reservations"] = sum(len(led["reserved"]) for led in data["days"].values())
    out["alert_level"] = alert_level(nums.get("pct_used"))
    grant = nums.get("grant")
    if grant:
        out["expiring_soon"] = grant["expires_at"] - now <= cfg["prefer_within_days"] * 86400.0
    if not cfg["enabled"]:
        out["reason"] = "turned off in the API credit config (enabled: false)"
    elif why:
        out["reason"] = why
    elif not grant:
        out["reason"] = ("no counting grant: every configured grant has expired (or is within an hour of expiring) or "
                         "is not granted yet; the owner adds a new grant to turn the lane on")
    elif nums["headroom_usd"] <= 0:
        out["reason"] = (f"hard stop: {round(100 * cfg['stop_fraction'])}% of the budget is spent or reserved "
                         f"({nums['pct_used']:.1f}% used)")
    elif not _writable(p):
        out["reason"] = "the ledger is not writable"
    else:
        out["on"] = True
    return out


def lane_on(now: float) -> bool:
    return bool(state(now)["on"])


def turn_fits_before_expiry(cfg: dict[str, Any], now: float, duration_s: float) -> bool:
    """True when a turn started at `now` and cut off at `duration_s` (its hard wall-clock cap) ends at least
    EXPIRY_SLACK_S before the EARLIEST expiry of the grants counting at `now` (the spend may be billed against any of
    them). False when no grant counts."""
    grants = counting(cfg, now)
    return bool(grants) and now + duration_s <= min(g["expires"] for g in grants) - EXPIRY_SLACK_S


def reserve(max_cost: float, now: float, path: Path | None = None, *, config: Path | None = None,
            duration_s: float = MAX_TURN_S) -> str | None:
    """Reserve `max_cost` against the credit, atomically with every other reservation: returns a reservation id to
    settle later, or None when the lane is off, the cost does not fit under the stop line (state() says why), or the
    turn's hard time cap `duration_s` would end after the grants' cutoff (turn_fits_before_expiry).
    Raises LedgerError (or ConfigError) when accounting is unavailable: the caller refuses (fail closed)."""
    cost = float(max_cost)
    if not math.isfinite(cost) or cost < 0:
        raise LedgerError("refusing a reservation that is not a finite non-negative amount")
    dur = float(duration_s)
    if not math.isfinite(dur) or dur <= 0:
        raise LedgerError("refusing a reservation without a finite positive time cap")
    if key_problem() is not None:
        return None
    cfg = load_config(config)
    if not cfg["enabled"]:
        return None
    p = path or ledger_path()
    today = _day(now)

    def fn(data: dict[str, Any]) -> str | None:
        cursor = data.get("pruned_through") or ""
        if cursor and today <= cursor:          # a clock rollback into pruned days: never reopen them blank
            raise LedgerError("the clock is inside the ledger's pruned days; refusing")
        nums = _numbers(cfg, data, now)          # everything spent and reserved, read under this lock
        if nums.get("grant") is None or cost > nums["headroom_usd"] or not turn_fits_before_expiry(cfg, now, dur):
            return None
        _prune(data, now)
        led = data["days"].setdefault(today, {"spent_usd": 0.0, "reserved": {}})
        rid = f"{today}:{uuid.uuid4().hex}"
        led["reserved"][rid] = _up(cost)
        return rid

    return _locked(p, fn)


def _prune(data: dict[str, Any], now: float) -> None:
    """Drop days older than KEEP_DAYS that hold no reservation, remembering the latest dropped day."""
    cutoff = _day(now - KEEP_DAYS * 86400.0)
    gone = [d for d, led in data["days"].items() if d < cutoff and not led["reserved"]]
    for d in gone:
        del data["days"][d]
    if gone:
        data["pruned_through"] = max([data.get("pruned_through") or ""] + gone)


def settle(res_id: str, actual_cost: float, now: float | None = None, path: Path | None = None) -> None:
    """Replace a reservation with the actual spend (an overrun past the reserved amount is recorded in full, never
    clamped). Idempotent: only an outstanding reservation is charged, so a retried settle cannot double-charge.
    Raises LedgerError on accounting failure (the reservation then stays, at its full amount)."""
    p = path or ledger_path()
    day = (res_id or "").split(":", 1)[0]
    actual = float(actual_cost)
    if not math.isfinite(actual) or actual < 0:
        raise LedgerError("refusing to settle a charge that is not a finite non-negative amount")

    def fn(data: dict[str, Any]) -> None:
        led = data["days"].get(day)
        if not isinstance(led, dict) or res_id not in led["reserved"]:
            return                               # already settled (or never reserved): a no-op, never a second charge
        led["reserved"].pop(res_id)
        led["spent_usd"] = _up(float(led["spent_usd"]) + actual)

    _locked(p, fn)


def window_numbers(now: float, path: Path | None = None) -> tuple[float, float, float]:
    """(used_pct, reset_at_epoch, window_s) for scarcity.window_view: % of the binding budget used, the soonest
    counting grant's end as the reset (credit does not refill), the grant's life as the window. Raises LedgerError
    when the lane cannot be read; a lane with no counting grant reads 100 % used."""
    st = state(now, path=path)
    if str(st.get("reason") or "").startswith("ledger:") or st.get("month") is None:
        raise LedgerError(st.get("reason") or "api credit unavailable")
    grant = st.get("grant")
    if not grant:
        return 100.0, now + 86400.0, 86400.0
    used = min(100.0, max(0.0, float(st["pct_used"] or 0.0) / st["stop_fraction"]))   # 100 % = the stop line
    end = grant["stops_counting_at"]
    return used, end, max(86400.0, end - grant["granted_at"])


def spent(now: float, path: Path | None = None) -> float:
    st = state(now, path=path)
    return float((st.get("grant") or {}).get("used_usd") or 0.0)


def budget_usd(now: float | None = None) -> float:
    """The counting grants' total (0 when none counts or the config cannot be read)."""
    import time as _t
    try:
        cfg = load_config()
    except ConfigError:
        return 0.0
    return sum(g["amount_usd"] for g in counting(cfg, now if now is not None else _t.time()))


total_budget_usd = budget_usd


def main(argv: list[str] | None = None) -> int:
    """`python3 -m cortados_router.api_credits status`: the lane's state as JSON (metadata only, no key, no request)."""
    import sys
    import time as _t
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["status"]:
        print("usage: python3 -m cortados_router.api_credits status", file=sys.stderr)
        return 2
    print(json.dumps(state(_t.time()), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
