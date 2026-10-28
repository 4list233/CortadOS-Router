"""Anthropic API-credit lane: a dollar-metered monthly window for the router's resource pool.

A prepaid API credit (default $200, resets on the 1st of the month) is modeled as a one-month budget WINDOW, so it
reuses the same pacing every other lane uses (`scarcity.window_view`): % of the window spent vs % of its time
elapsed, use-it-or-lose-it as the reset nears, and scarcity 1.0 once it is spent. The only difference is the unit:
dollars, for this one window (Forest's "scarcity is subscription units, never dollars" holds for the plan lanes).

This module owns, stdlib only so it imports nothing from the package:
  - PRICES: USD per million tokens per model (from the claude-api skill, cached 2026-10-06),
  - cost_usd / cost_from_usage: dollars for one API call from its response usage,
  - a self-metered spend file (~/.cortados/api-credit-meter.json), {"month": "YYYY-MM", "spent_usd": float},
    which zeroes when the month rolls over,
  - record / spent / remaining / over_budget: the provider's hard cap (never bill past the credit),
  - window_numbers(now): (used_pct, reset_at_epoch, window_s) for scarcity.window_view.

Spend is priced CONSERVATIVELY — an unknown model and cache-write tokens are rounded UP (to the most expensive
model / input×1.25) — so the cap stops a little early rather than overspending real credits. Scarcity readings can
lag the meter between samples; the provider enforces the cap from the live meter at call time, so the lag is safe.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# USD per 1,000,000 tokens. cache_write ≈ input × 1.25 (5-minute write); cache_read per the published rate.
PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.25, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.25, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.02, "cache_write": 0.125},
}
# Most expensive tier, so an unrecognized model is OVER-counted toward the cap, never under-counted.
DEFAULT_PRICE: dict[str, float] = {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5}

DEFAULT_BUDGET_USD = 200.0


def _state_dir() -> Path:
    return Path(os.environ.get("CORTADOS_HOME") or os.environ.get("CORTADOS_HERMES_HOME") or (Path.home() / ".cortados"))


def meter_path() -> Path:
    return _state_dir() / "api-credit-meter.json"


def budget_usd() -> float:
    """The monthly credit in dollars; CORTADOS_API_CREDIT_BUDGET_USD overrides the $200 default. Non-positive or
    unparseable values fall back to the default rather than disabling the cap."""
    raw = os.environ.get("CORTADOS_API_CREDIT_BUDGET_USD")
    try:
        v = float(raw) if raw not in (None, "") else DEFAULT_BUDGET_USD
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_USD
    return v if v > 0 else DEFAULT_BUDGET_USD


def price_for(model: str) -> dict[str, float]:
    """Prices for a model id: exact match, else a prefix match (a dated/suffixed id resolves to its base), else the
    most-expensive default so an unknown model is never under-priced."""
    m = (model or "").strip().lower()
    if m in PRICES:
        return PRICES[m]
    for base, p in PRICES.items():
        if m.startswith(base):
            return p
    return DEFAULT_PRICE


def cost_usd(model: str, input_tokens: float = 0, output_tokens: float = 0, cache_read_tokens: float = 0, cache_creation_tokens: float = 0) -> float:
    """Dollars for one call's token counts. Negative/non-finite counts are treated as 0."""
    def n(x: Any) -> float:
        try:
            v = float(x)
        except (TypeError, ValueError):
            return 0.0
        return v if v == v and v not in (float("inf"), float("-inf")) and v > 0 else 0.0  # NaN/inf/negative -> 0

    p = price_for(model)
    return (n(input_tokens) * p["input"] + n(output_tokens) * p["output"] + n(cache_read_tokens) * p["cache_read"] + n(cache_creation_tokens) * p["cache_write"]) / 1_000_000.0


def cost_from_usage(model: str, usage: dict[str, Any] | None) -> float:
    """Dollars for one call, reading the Anthropic response `usage` block's standard token fields."""
    u = usage if isinstance(usage, dict) else {}
    return cost_usd(
        model,
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_read_tokens=u.get("cache_read_input_tokens", 0),
        cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
    )


def month_key(now: float) -> str:
    return datetime.fromtimestamp(now).astimezone().strftime("%Y-%m")


def month_bounds(now: float) -> tuple[float, float]:
    """(start_epoch, next_start_epoch) of the local calendar month containing `now`: the window's edges."""
    dt = datetime.fromtimestamp(now).astimezone()
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.timestamp(), nxt.timestamp()


def _read_meter(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_meter(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def spent(now: float, path: Path | None = None) -> float:
    """Dollars spent in the current month. A meter from an earlier month reads as 0 (the window reset); the stored
    file is left as-is until the next record() rewrites it."""
    d = _read_meter(path or meter_path())
    if d.get("month") != month_key(now):
        return 0.0
    try:
        return max(0.0, float(d.get("spent_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def record(model: str, usage: dict[str, Any] | None, now: float, path: Path | None = None) -> float:
    """Add one call's cost to this month's meter (resetting it if the month rolled over) and return the new total."""
    p = path or meter_path()
    d = _read_meter(p)
    key = month_key(now)
    base = spent(now, p)  # 0 across a month boundary, else the stored total
    total = round(base + cost_from_usage(model, usage), 6)
    _write_meter(p, {"month": key, "spent_usd": total})
    return total


def remaining(now: float, path: Path | None = None) -> float:
    return max(0.0, budget_usd() - spent(now, path))


def over_budget(now: float, path: Path | None = None) -> bool:
    """The hard cap the provider checks before each call: true once the month's credit is spent."""
    return spent(now, path) >= budget_usd()


def window_numbers(now: float, path: Path | None = None) -> tuple[float, float, float]:
    """(used_pct, reset_at_epoch, window_s) for scarcity.window_view — the credit lane's single 1-month window."""
    start, nxt = month_bounds(now)
    used_pct = min(100.0, 100.0 * spent(now, path) / budget_usd()) if budget_usd() else 100.0
    return used_pct, nxt, nxt - start
