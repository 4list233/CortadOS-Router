"""Where the router finds its shipped data. Owner of the tables directory and the scorer's promotion file.

Runtime state (router.db, scarcity.json, unit-cost.json, reviews.db) is NOT resolved here: it stays under
CORTADOS_HOME / CORTADOS_HERMES_HOME (default ~/.cortados), resolved by each module that writes it.
"""
from __future__ import annotations

from pathlib import Path

from cortados_router.compat import getenv

ROUTER_ROOT = Path(__file__).resolve().parents[1]


def tables_dir() -> Path:
    """Routing tables, budgets, benchmark scores and inventories: $CORTADOS_ROUTER_TABLES, else this repo's tables/."""
    return Path(getenv("CORTADOS_ROUTER_TABLES") or ROUTER_ROOT / "tables")


def s1_current() -> Path:
    """The promoted CortadOS Router S1 adapter record. Fixed at s1/current.json: s1/serve.py loads it, s1/evaluate.py
    grades against it and s1/nightly.sh writes it, so it has no override that only some of them would honour."""
    return ROUTER_ROOT / "s1" / "current.json"
