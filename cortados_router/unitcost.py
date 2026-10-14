"""Tokens per percent of each subscription pool, learned from our own data (Forest 2026-09-26: plans are not normalised; route on tokens left).

Every 15-30 min scarcity.py records each window's % used (scarcity-history.jsonl). The usage ledger records every token we sent per lane
and model. Between two readings of the same window with no reset in between, tokens_per_pct = Σ ledger tokens on that pool / Δ% used.
Pooled over the last 14 days with intervals weighted by their Δ% (noisy 1-point moves count little). Output ~/.cortados/unit-cost.json:
  {pool: {"tokens_per_pct", "capacity_tokens" (=100×), "remaining_tokens" (from the latest reading), "intervals", "confidence"}}
Refreshed daily by the nightly job and whenever the sampler runs with --learn. The router uses remaining_tokens to prefer the pool with the
most ABSOLUTE room among the underused ones, and the readout shows "≈ N M tokens left" next to the percentage."""
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
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

STATE = Path(_env_get("CORTADOS_HOME") or Path.home() / ".cortados")
OUT = STATE / "unit-cost.json"
# ledger runtime/model → the pool its tokens are charged to
def pool_of(runtime: str, model: str) -> str | None:
    m = (model or "").lower()
    if runtime == "claude":
        return "claude:fable" if "fable" in m else "claude:all"
    if runtime == "codex":
        return "codex:all"
    if runtime == "agy":
        return "antigravity:gemini" if "gemini" in m else "antigravity:claude-gpt"
    if runtime == "cursor":
        return "cursor:cursor-models" if any(k in m for k in ("grok", "composer", "auto")) else "cursor:other-models"
    return None


WINDOW_POOL = {("claude", "week (all models)"): "claude:all", ("claude", "week (fable)"): "claude:fable", ("codex", "week"): "codex:all",
               ("antigravity", "week (gemini)"): "antigravity:gemini", ("antigravity", "week (claude-gpt)"): "antigravity:claude-gpt",
               ("cursor", "month (Cursor models: Grok, Composer, Auto)"): "cursor:cursor-models", ("cursor", "month (other models: Claude, GPT, Gemini …)"): "cursor:other-models"}


def learn(days: int = 14) -> dict:
    since = time.time() - days * 86400
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for line in (STATE / "scarcity-history.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r["ts"] < since:
            continue
        for lane, v in r["lanes"].items():
            for w in v.get("windows") or []:
                p = WINDOW_POOL.get((lane, w[0]))
                if p:
                    series[p].append((r["ts"], float(w[1])))
    c = sqlite3.connect(STATE / "usage.db")
    rows = c.execute("SELECT ts, runtime, model, input_tokens+output_tokens+cache_read_tokens+cache_write_tokens FROM usage WHERE ts > ?", (since,)).fetchall()
    c.close()
    tokens: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for ts, rt, model, tok in rows:
        p = pool_of(rt, model)
        if p and tok:
            tokens[p].append((ts, int(tok)))
    out = {}
    for p, pts in series.items():
        pts.sort()
        num = den = 0.0
        n = 0
        for (t0, u0), (t1, u1) in zip(pts, pts[1:]):
            d = u1 - u0
            if d <= 0 or d > 40 or t1 - t0 > 6 * 3600:      # reset, no movement, or a gap too long to attribute
                continue
            tok = sum(k for ts, k in tokens.get(p, []) if t0 < ts <= t1)
            if tok <= 0:
                continue
            num += tok; den += d; n += 1                    # weighted by Δ%: Σtokens / Σ%
        latest = pts[-1][1] if pts else None
        if den > 0:
            tpp = num / den
            out[p] = {"tokens_per_pct": round(tpp), "capacity_tokens": round(100 * tpp), "remaining_tokens": round((100 - latest) * tpp) if latest is not None else None,
                      "latest_used_pct": latest, "intervals": n, "pct_covered": round(den, 1), "confidence": "good" if den >= 20 and n >= 5 else "rough" if den >= 5 else "weak"}
        else:
            out[p] = {"tokens_per_pct": None, "intervals": 0, "confidence": "none", "latest_used_pct": latest}
    OUT.write_text(json.dumps({"learned_at": time.strftime("%Y-%m-%d %H:%M"), "days": days, "pools": out}, indent=1))
    return out


def fmt(n: float | None) -> str:
    return "?" if n is None else (f"{n / 1e9:.1f} B" if n >= 1e9 else f"{n / 1e6:.0f} M" if n >= 1e6 else f"{n / 1e3:.0f} k")


if __name__ == "__main__":
    for p, v in learn().items():
        print(f"{p:26} {fmt(v.get('tokens_per_pct'))}/pt  capacity ≈ {fmt(v.get('capacity_tokens'))} tokens  left ≈ {fmt(v.get('remaining_tokens'))}  ({v['confidence']}, {v['intervals']} intervals, {v.get('pct_covered', 0)} pts)")
