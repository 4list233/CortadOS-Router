"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools. A write never breaks or slows a turn: a short sqlite timeout (a locked db loses one row,
it does not hold the reply) and failures are logged with the exception type, once per type, never raised."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortados.usage_ledger")
TIMEOUT_S = 0.5          # a turn's reply must never wait on this file; a lost row only weakens one learning interval
_SCHEMA = ("CREATE TABLE IF NOT EXISTS usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
           "cache_read_tokens INTEGER, cache_write_tokens INTEGER, lane TEXT)")
_warned: set[str] = set()
_warn_lock = threading.Lock()


def db_path() -> Path:
    return Path(os.environ.get("CORTADOS_HOME") or Path.home() / ".cortados") / "usage.db"


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def record(lane: str, model: str, usage: dict | None, *, ts: float | None = None, path: Path | None = None) -> bool:
    """Append one turn. `lane` is the scarcity lane (claude, claude#b, codex); its base is the runtime. Returns False on failure."""
    try:
        u = usage or {}
        runtime = (lane or "").split("#", 1)[0]
        row = (ts or time.time(), runtime, str(model or ""), _int(u.get("input_tokens")), _int(u.get("output_tokens")),
               _int(u.get("cache_read_input_tokens") or u.get("cache_read_tokens")),
               _int(u.get("cache_creation_input_tokens") or u.get("cache_write_tokens")), lane)
        p = path or db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(p), timeout=TIMEOUT_S)
        try:
            c.execute(_SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(usage)")}
            if "lane" not in cols:                     # a ledger from the Hermes plugin: add the column, old rows stay lane-less
                c.execute("ALTER TABLE usage ADD COLUMN lane TEXT")
            c.execute("INSERT INTO usage (ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane) "
                      "VALUES (?,?,?,?,?,?,?,?)", row)
            c.commit()
        finally:
            c.close()
        return True
    except Exception as exc:                            # never raised: the turn already succeeded
        kind = type(exc).__name__
        with _warn_lock:
            first = kind not in _warned
            _warned.add(kind)
        if first:
            logger.warning("usage ledger write failed (%s: %s); later %s failures are not logged", kind, str(exc)[:120], kind)
        return False


def record_async(lane: str, model: str, usage: dict | None) -> None:
    """Off the request thread, so even the sqlite timeout never delays a reply."""
    threading.Thread(target=record, args=(lane, model, usage), name="cortados-usage-ledger", daemon=True).start()
