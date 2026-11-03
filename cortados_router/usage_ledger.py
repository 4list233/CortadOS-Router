"""Usage ledger: one row per completed backend turn, in $CORTADOS_HOME/usage.db, for unitcost.learn (tokens per % of a pool).

The old writer was the Hermes plugin; the provider is now the only thing that runs subscription turns, so it writes here.
Schema is the one unitcost.learn reads, plus `lane` (the scarcity lane the turn drew on: claude, claude#b, codex), because two
Claude accounts are two separate pools. A write never breaks or slows a turn: a short sqlite timeout (a locked db loses one row,
it does not hold the reply) and failures are never raised. Consecutive failures are counted (`consecutive_failures()`) and
logged with their reason on the first and every LOG_EVERY-th one, so a ledger that keeps failing stays visible without flooding."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("cortados.usage_ledger")
TIMEOUT_S = 0.5          # a turn's reply must never wait on this file; a lost row only weakens one learning interval
_SCHEMA = ("CREATE TABLE IF NOT EXISTS usage (ts REAL, runtime TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
           "cache_read_tokens INTEGER, cache_write_tokens INTEGER, lane TEXT)")
LOG_EVERY = 100          # a steady failure (full disk, read-only home) repeats every turn: one line per 100 turns stays readable
_fail_lock = threading.Lock()
_consecutive_failures = 0


def db_path() -> Path:
    from cortados_router import paths
    return paths.state_dir() / "usage.db"


def consecutive_failures() -> int:
    """Writes that failed in a row since the last success (0 when the ledger is healthy)."""
    with _fail_lock:
        return _consecutive_failures


def _ensure_lane_column(c: sqlite3.Connection) -> None:
    """Add `lane` to a ledger from the Hermes plugin (old rows stay lane-less). Two processes can migrate at once: the
    check and the ALTER run under BEGIN IMMEDIATE (one writer at a time), and a duplicate-column error from a racer that
    still got there first means the column exists, which is the goal."""
    if "lane" in {r[1] for r in c.execute("PRAGMA table_info(usage)")}:
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        if "lane" not in {r[1] for r in c.execute("PRAGMA table_info(usage)")}:
            try:
                c.execute("ALTER TABLE usage ADD COLUMN lane TEXT")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def record(lane: str, model: str, usage: dict | None, *, ts: float | None = None, path: Path | None = None) -> bool:
    """Append one turn. `lane` is the scarcity lane (claude, claude#b, codex); its base is the runtime. Returns False on failure."""
    global _consecutive_failures
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
            c.commit()
            _ensure_lane_column(c)
            c.execute("INSERT INTO usage (ts, runtime, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, lane) "
                      "VALUES (?,?,?,?,?,?,?,?)", row)
            c.commit()
        finally:
            c.close()
    except Exception as exc:                            # never raised: the turn already succeeded
        with _fail_lock:
            _consecutive_failures += 1
            n = _consecutive_failures
        if n == 1 or n % LOG_EVERY == 0:
            logger.warning("usage ledger write failed (%s: %s); %d consecutive failures, logged on the first and every %d",
                           type(exc).__name__, str(exc)[:120], n, LOG_EVERY)
        return False
    with _fail_lock:
        if _consecutive_failures:
            logger.info("usage ledger writes recovered after %d failures", _consecutive_failures)
        _consecutive_failures = 0
    return True


def record_async(lane: str, model: str, usage: dict | None) -> threading.Thread:
    """Off the request thread, so even the sqlite timeout never delays a reply. Returns the started thread (tests join it)."""
    t = threading.Thread(target=record, args=(lane, model, usage), name="cortados-usage-ledger", daemon=True)
    t.start()
    return t
