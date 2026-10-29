"""Anthropic API-credit lane: a dollar-metered, admission-controlled monthly window for the router's resource pool.

A prepaid API credit (default $200, resets on the billing day) funds a credit lane. This module owns the ACCOUNTING
for that budget — it does not itself talk to the network. Design points (hardened after a Codex money-path review):

  * Admission control, not after-the-fact counting. A request reserves a CONSERVATIVE maximum cost under a file
    lock BEFORE it is dispatched (`reserve`), and settles the actual cost after (`settle`, full reservation if the
    actual is unknown — e.g. the client disconnected). So concurrent requests cannot all pass the same check and
    overshoot, and a dropped turn never under-charges.
  * File-locked, per-period ledger. The meter file holds one sub-ledger per billing period; `fcntl.flock`
    serializes read-modify-write across the threading HTTP server AND across processes. A late settle lands in its
    own period, never erasing the new one. Writes use a unique temp file, so two writers cannot collide.
  * Fail closed. If the ledger cannot be read or written, the operation RAISES — the caller then refuses admission
    rather than spending blind. A genuinely-absent ledger is fresh (zero), a corrupt one is an error.
  * Conservative pricing, but the Anthropic API is the AUTHORITATIVE hard cap. With the credit org set to
    overage-off, the API itself rejects calls past the included credit; this meter is a conservative PACING guard
    and use-it-or-lose-it signal on top of that. Cache writes are priced at the 1-hour rate and unknown models at
    the most-expensive tier, so the pacing guard does not under-count.

Stdlib only.
"""
from __future__ import annotations

import calendar
import fcntl
import json
import math
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _entry(inp: float, out: float) -> dict[str, float]:
    # cache_write at the 1-hour rate (input x2, the higher write rate); cache_read bounded above by input x0.1.
    return {"input": inp, "output": out, "cache_read": round(inp * 0.1, 4), "cache_write": inp * 2}


# USD per 1,000,000 tokens (claude-api skill, cached 2026-10-06), priced conservatively.
PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": _entry(10.0, 50.0),
    "claude-fable-5": _entry(10.0, 50.0),
    "claude-mythos-5-1": _entry(10.0, 50.0),
    "claude-opus-5-5": _entry(4.0, 20.0),
    "claude-opus-5": _entry(5.0, 25.0),
    "claude-opus-4-8": _entry(5.0, 25.0),
    "claude-sonnet-5-5": _entry(2.0, 10.0),
    "claude-sonnet-5": _entry(2.0, 10.0),
    "claude-haiku-5-5": _entry(0.50, 2.50),  # the >100k-token tier, so the guard never under-counts Haiku
}
DEFAULT_PRICE = _entry(10.0, 50.0)  # unknown model -> most expensive tier

# Per-request charges the API bills for server-side tools ON TOP OF tokens (USD each), priced conservatively.
# Anthropic bills web search at $10 / 1,000 requests = $0.01 each; the count comes back in usage.server_tool_use.
SERVER_TOOL_USD: dict[str, float] = {
    "web_search_requests": 0.01,
}
DEFAULT_SERVER_TOOL_USES = 20  # a declared web-search tool with no max_uses: reserve AND enforce this many calls

# Server tools the API itself EXECUTES and bills. The credit lane admits a request using one only when it can both
# BOUND the cost up front and reconcile it from usage afterwards. Keyed by the tool-type prefix, since Anthropic
# suffixes a date (e.g. "web_search_20250305"). An ordinary client-executed tool is not listed here and is fine.
#   metered   -> per-request charge, bounded by an enforced max_uses and settled from usage.server_tool_use
#   free      -> no per-request charge (tokens only)
#   unbounded -> billed by server-side time/resource the meter cannot bound or reconcile yet -> refuse admission
_SERVER_TOOL_KIND: dict[str, str] = {
    "web_search": "metered",
    "web_fetch": "free",
    "code_execution": "unbounded",
}


def unbounded_server_tools(body: dict[str, Any] | None) -> list[str]:
    """The server-executed tools in `body` whose cost the meter cannot bound and reconcile. A request declaring one
    must be refused on the credit lane (its spend is unknowable in advance and not recoverable from usage)."""
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        return []
    bad: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type") or "").lower()
        for prefix, kind in _SERVER_TOOL_KIND.items():
            if ttype.startswith(prefix) and kind == "unbounded":
                bad.append(ttype)
    return bad


def enforce_server_tool_caps(body: dict[str, Any]) -> dict[str, Any]:
    """Return `body` (a shallow copy if anything changed) with every metered server tool carrying an explicit cap
    equal to what the meter reserves for it, so the API cannot run more billable calls than were paid for. A
    web-search tool with no max_uses gets DEFAULT_SERVER_TOOL_USES; a declared max_uses is left as the client set
    it (the API already enforces it, and the reservation matches)."""
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

DEFAULT_BUDGET_USD = 200.0


class LedgerError(RuntimeError):
    """The ledger could not be read or written; the caller must fail closed (refuse admission)."""


def _state_dir() -> Path:
    return Path(os.environ.get("CORTADOS_HOME") or os.environ.get("CORTADOS_HERMES_HOME") or (Path.home() / ".cortados"))


def meter_path() -> Path:
    return _state_dir() / "api-credit-meter.json"


def budget_usd() -> float:
    raw = os.environ.get("CORTADOS_API_CREDIT_BUDGET_USD")
    try:
        v = float(raw) if raw not in (None, "") else DEFAULT_BUDGET_USD
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_USD
    return v if v > 0 else DEFAULT_BUDGET_USD


def reset_day() -> int:
    """Day of the month the included credit renews (the billing day). Default 1; accepts 1..31 (a day past a given
    month's length is clamped to that month's last day where the boundary is computed)."""
    raw = os.environ.get("CORTADOS_API_CREDIT_RESET_DAY")
    try:
        d = int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        return 1
    return min(31, max(1, d))


def price_for(model: str) -> dict[str, float]:
    m = (model or "").strip().lower()
    if m in PRICES:
        return PRICES[m]
    for base, p in PRICES.items():
        if m.startswith(base):
            return p
    return DEFAULT_PRICE


def _n(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def cost_usd(model: str, input_tokens: float = 0, output_tokens: float = 0, cache_read_tokens: float = 0, cache_creation_tokens: float = 0) -> float:
    p = price_for(model)
    total = _n(input_tokens) * p["input"] + _n(output_tokens) * p["output"] + _n(cache_read_tokens) * p["cache_read"] + _n(cache_creation_tokens) * p["cache_write"]
    return total / 1_000_000.0


def server_tool_cost(usage: dict[str, Any] | None) -> float:
    """Per-request charges the API billed for server-side tools (e.g. web search), read from usage.server_tool_use.
    These are flat per-call charges on top of tokens, so fast mode's 2x on tokens does not apply to them."""
    u = usage if isinstance(usage, dict) else {}
    stu = u.get("server_tool_use")
    if not isinstance(stu, dict):
        return 0.0
    return sum(SERVER_TOOL_USD.get(k, 0.0) * _n(v) for k, v in stu.items())


def cost_from_usage(model: str, usage: dict[str, Any] | None) -> float:
    u = usage if isinstance(usage, dict) else {}
    base = cost_usd(model, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
    if str(u.get("speed") or "").lower() == "fast":
        base *= 2  # fast mode is billed at 2x (tokens only)
    return base + server_tool_cost(u)  # server-tool charges are flat per call, added after the fast multiplier


def estimate_max_cost(body: dict[str, Any], model: str) -> float:
    """A CONSERVATIVE upper bound on one request's cost, used to reserve credit before dispatch: the prompt bytes as
    input tokens (deliberately low bytes/token), PLUS the API-added overhead the body doesn't carry (system prompt
    and the rendered tool schemas), priced at the CACHE-WRITE rate (the most expensive input treatment), plus
    max_tokens (or the 128k ceiling) at the output rate, doubled for fast mode."""
    p = price_for(model)
    raw = json.dumps(body) if isinstance(body, dict) else ""
    tools = body.get("tools") if isinstance(body, dict) else None
    n_tools = len(tools) if isinstance(tools, list) else 0
    input_tokens = len(raw.encode()) / 2.0 + 4000 + 1500 * n_tools   # ~2 bytes/token is a conservative upper bound
    try:
        max_out = int(body.get("max_tokens") or 0) if isinstance(body, dict) else 0
    except (TypeError, ValueError):
        max_out = 0
    if max_out <= 0:
        max_out = 128000  # no cap given: assume the model's max output
    in_rate, out_rate = max(p["input"], p["cache_write"]), p["output"]
    if isinstance(body, dict) and str(body.get("speed") or "").lower() == "fast":
        in_rate, out_rate = in_rate * 2, out_rate * 2
    token_cost = (input_tokens * in_rate + max_out * out_rate) / 1_000_000.0
    return token_cost + _server_tool_allowance(tools)


def _server_tool_allowance(tools: Any) -> float:
    """Conservative dollar reserve for any server-side tool the request declares (e.g. web search): its max_uses
    at the per-request rate, or a default call count when no cap is given. Reserved up front so a burst of searches
    cannot overrun the budget between reservation and settlement."""
    if not isinstance(tools, list):
        return 0.0
    allow = 0.0
    for t in tools:
        if not isinstance(t, dict):
            continue
        ttype = str(t.get("type") or "").lower()   # a server tool is identified by `type`, never a client tool's name
        if "web_search" not in ttype:
            continue
        try:
            uses = int(t.get("max_uses") or 0)
        except (TypeError, ValueError):
            uses = 0
        if uses <= 0:
            uses = DEFAULT_SERVER_TOOL_USES
        allow += uses * SERVER_TOOL_USD["web_search_requests"]
    return allow


# ------------------------------------------------------------------ billing period
def _effective_reset(year: int, month: int) -> int:
    """The reset day for a given month, clamped to that month's last day (so a 31st billing day is the 28th/30th in
    shorter months)."""
    return min(reset_day(), calendar.monthrange(year, month)[1])


def _start_month(now: float) -> tuple[int, int]:
    dt = datetime.fromtimestamp(now).astimezone()
    if dt.day >= _effective_reset(dt.year, dt.month):
        return dt.year, dt.month
    return (dt.year - 1, 12) if dt.month == 1 else (dt.year, dt.month - 1)


def period_key(now: float) -> str:
    """The billing period `now` falls in, labelled 'YYYY-MM' by the month the period starts in."""
    y, m = _start_month(now)
    return f"{y:04d}-{m:02d}"


def period_bounds(now: float) -> tuple[float, float]:
    """(start_epoch, next_start_epoch) of the billing period containing `now`, on each month's effective reset day."""
    sy, sm = _start_month(now)
    ny, nm = (sy + 1, 1) if sm == 12 else (sy, sm + 1)
    start = datetime(sy, sm, _effective_reset(sy, sm)).astimezone()
    nxt = datetime(ny, nm, _effective_reset(ny, nm)).astimezone()
    return start.timestamp(), nxt.timestamp()


# ------------------------------------------------------------------ locked per-period ledger
def _blank() -> dict[str, Any]:
    return {"periods": {}}


def _finite_nonneg(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _validate(data: Any) -> dict[str, Any]:
    """Strict schema check shared by reads and mutations: periods is a dict; each period has a finite non-negative
    spent_usd and a reserved map of finite non-negative amounts. Anything else is a LedgerError (fail closed)."""
    if not isinstance(data, dict) or not isinstance(data.get("periods"), dict):
        raise LedgerError("api-credit ledger is corrupt (unexpected shape)")
    for period, led in data["periods"].items():
        if not isinstance(led, dict) or not _finite_nonneg(led.get("spent_usd")) or not isinstance(led.get("reserved"), dict):
            raise LedgerError(f"api-credit ledger period {period!r} is invalid")
        for rid, amt in led["reserved"].items():
            if not _finite_nonneg(amt):
                raise LedgerError(f"api-credit ledger reservation {rid!r} is invalid")
    return data


def _locked(path: Path, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Run fn on the ledger under an exclusive file lock, persisting whatever fn leaves in `data`. Fail closed:
    any read/parse/write error raises LedgerError so the caller refuses admission instead of spending blind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = open(path.with_suffix(".lock"), "w")
    except OSError as exc:
        raise LedgerError(f"cannot open the api-credit lock: {exc}") from exc
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists():
            try:
                data = _validate(json.loads(path.read_text()))
            except LedgerError:
                raise
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                raise LedgerError(f"api-credit ledger unreadable: {exc}") from exc
        else:
            data = _blank()
        out = fn(data)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data))
            tmp.replace(path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise LedgerError(f"api-credit ledger write failed: {exc}") from exc
        return out
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _ledger_for(data: dict[str, Any], period: str) -> dict[str, Any]:
    periods = data.setdefault("periods", {})
    led = periods.setdefault(period, {"spent_usd": 0.0, "reserved": {}})
    led.setdefault("spent_usd", 0.0)
    led.setdefault("reserved", {})
    # Prune resolved ledgers beyond the two most recent, so the file does not grow without bound. NEVER drop a
    # period that still holds outstanding reservations: a later settle reads its period from the reservation id, and
    # a pruned period would be recreated empty, losing the charge. Keep such periods until their reservations settle.
    keep = set(sorted(periods)[-2:]) | {p for p, l in periods.items() if l.get("reserved")}
    for stale in [p for p in list(periods) if p not in keep]:
        periods.pop(stale, None)
    return led


def reserve(max_cost: float, now: float, path: Path | None = None) -> str | None:
    """Reserve `max_cost` against this period's remaining credit, atomically. Returns a reservation id to settle
    later, or None if the reservation would exceed the budget (the caller then refuses the request). Raises
    LedgerError if accounting is unavailable (fail closed)."""
    p = path or meter_path()
    period = period_key(now)
    budget = budget_usd()
    cost = max(0.0, float(max_cost))

    def fn(data: dict[str, Any]) -> str | None:
        led = _ledger_for(data, period)
        committed = float(led["spent_usd"]) + sum(float(v) for v in led["reserved"].values())
        if committed + cost > budget:
            return None
        rid = f"{period}:{uuid.uuid4().hex}"
        led["reserved"][rid] = round(cost, 6)
        return rid

    return _locked(p, fn)


def settle(res_id: str, actual_cost: float, now: float | None = None, path: Path | None = None) -> None:
    """Replace a reservation with the actual spend (recorded in full — an overrun past the reserved max is NOT
    clamped, so it stays visible). The reservation's own period is read from its id, so a late settle lands in the
    right period. Idempotent: the charge is recorded only while the reservation is still outstanding, so a retried
    or duplicated settle cannot double-charge. Raises LedgerError on accounting failure."""
    p = path or meter_path()
    period = res_id.split(":", 1)[0]
    actual = max(0.0, float(actual_cost))

    def fn(data: dict[str, Any]) -> None:
        led = _ledger_for(data, period)
        if res_id not in led["reserved"]:
            return   # already settled (or never reserved here) -> idempotent no-op, never a second charge
        led["reserved"].pop(res_id)                                       # release the reservation, exactly once
        led["spent_usd"] = round(float(led["spent_usd"]) + actual, 6)     # record the ACTUAL — overruns are not hidden

    _locked(p, fn)


def _current(now: float, path: Path | None = None) -> dict[str, float]:
    p = path or meter_path()
    period = period_key(now)
    try:
        if not p.exists():
            return {"spent_usd": 0.0, "reserved": 0.0}
        data = _validate(json.loads(p.read_text()))
        led = data["periods"].get(period) or {"spent_usd": 0.0, "reserved": {}}
        return {"spent_usd": float(led.get("spent_usd") or 0.0),
                "reserved": sum(float(v) for v in (led.get("reserved") or {}).values())}
    except (LedgerError, ValueError, OSError, TypeError, AttributeError):
        # A read-only view that cannot validate treats the period as fully committed, so pacing fails safe (the
        # authoritative admission path, reserve(), raises instead).
        return {"spent_usd": budget_usd(), "reserved": 0.0}


def spent(now: float, path: Path | None = None) -> float:
    return _current(now, path)["spent_usd"]


def committed(now: float, path: Path | None = None) -> float:
    """Spent plus outstanding reservations — what admission and pacing both read against the budget."""
    c = _current(now, path)
    return c["spent_usd"] + c["reserved"]


def remaining(now: float, path: Path | None = None) -> float:
    return max(0.0, budget_usd() - committed(now, path))


def over_budget(now: float, path: Path | None = None) -> bool:
    return committed(now, path) >= budget_usd()


def window_numbers(now: float, path: Path | None = None) -> tuple[float, float, float]:
    """(used_pct, reset_at_epoch, window_s) for scarcity.window_view — committed dollars over the billing window."""
    start, nxt = period_bounds(now)
    b = budget_usd()
    used_pct = min(100.0, 100.0 * committed(now, path) / b) if b else 100.0
    return used_pct, nxt, nxt - start
