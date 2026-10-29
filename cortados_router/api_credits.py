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

import fcntl
import json
import math
import os
import time
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
    """Day of the month the included credit renews (the billing day). Default 1; clamped to 1..28."""
    raw = os.environ.get("CORTADOS_API_CREDIT_RESET_DAY")
    try:
        d = int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        return 1
    return min(28, max(1, d))


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


def cost_from_usage(model: str, usage: dict[str, Any] | None) -> float:
    u = usage if isinstance(usage, dict) else {}
    return cost_usd(model, u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))


def estimate_max_cost(body: dict[str, Any], model: str) -> float:
    """A CONSERVATIVE upper bound on what one request can cost: every prompt byte counted as a (cached-miss) input
    token at the full input rate, plus max_tokens at the output rate. Used to reserve credit before dispatch."""
    p = price_for(model)
    raw = json.dumps(body) if isinstance(body, dict) else ""
    input_tokens = len(raw.encode()) / 3.0  # ~3 bytes/token, deliberately low (more tokens = larger reservation)
    try:
        max_out = int(body.get("max_tokens") or 0) if isinstance(body, dict) else 0
    except (TypeError, ValueError):
        max_out = 0
    if max_out <= 0:
        max_out = 64000  # no cap given: assume a large output
    return (input_tokens * p["input"] + max_out * p["output"]) / 1_000_000.0


# ------------------------------------------------------------------ billing period
def period_key(now: float) -> str:
    """The billing period `now` falls in, as 'YYYY-MM', where a period runs from reset_day to the next reset_day."""
    dt = datetime.fromtimestamp(now).astimezone()
    y, m = (dt.year, dt.month) if dt.day >= reset_day() else (dt.year, dt.month - 1)
    if m < 1:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def period_bounds(now: float) -> tuple[float, float]:
    """(start_epoch, next_start_epoch) of the billing period containing `now`."""
    dt = datetime.fromtimestamp(now).astimezone()
    d = reset_day()
    start = dt.replace(day=d, hour=0, minute=0, second=0, microsecond=0)
    if dt.day < d:
        start = start.replace(year=start.year - 1, month=12) if start.month == 1 else start.replace(month=start.month - 1)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.timestamp(), nxt.timestamp()


# ------------------------------------------------------------------ locked per-period ledger
def _blank() -> dict[str, Any]:
    return {"periods": {}}


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
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or "periods" not in data:
                    raise LedgerError("api-credit ledger is corrupt (unexpected shape)")
            except (ValueError, OSError) as exc:
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
    # prune ledgers older than the two most recent, so the file does not grow without bound
    for stale in sorted(periods)[:-2]:
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
    """Replace a reservation with the actual spend (clamped to the reserved max). The reservation's own period is
    read from its id, so a late settle lands in the right period. Raises LedgerError on accounting failure."""
    p = path or meter_path()
    period = res_id.split(":", 1)[0]
    actual = max(0.0, float(actual_cost))

    def fn(data: dict[str, Any]) -> None:
        led = _ledger_for(data, period)
        held = led["reserved"].pop(res_id, None)
        charge = min(actual, float(held)) if held is not None else actual
        led["spent_usd"] = round(float(led["spent_usd"]) + charge, 6)

    _locked(p, fn)


def _current(now: float, path: Path | None = None) -> dict[str, float]:
    p = path or meter_path()
    period = period_key(now)
    try:
        if not p.exists():
            return {"spent_usd": 0.0, "reserved": 0.0}
        data = json.loads(p.read_text())
        led = (data.get("periods") or {}).get(period) or {}
        return {"spent_usd": float(led.get("spent_usd") or 0.0),
                "reserved": sum(float(v) for v in (led.get("reserved") or {}).values())}
    except (ValueError, OSError, TypeError):
        # A read-only view that cannot parse treats the period as fully committed, so pacing fails safe (the
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
