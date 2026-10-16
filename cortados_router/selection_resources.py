# PURPOSE: Give the existing resolver immutable, explicit subscription resources.
# CONTRACT: RT-MCP-001 section 4.1; no implicit refresh or learned-cost disk reads.
# DEPENDS: Existing core health projection; validated native scarcity record.
"""Resolver resource seam; unlearned token quantities remain unknown."""

from dataclasses import dataclass
import json

from . import core, scarcity


@dataclass(frozen=True)
class SelectionResources:
    """Serialize at construction so later callers cannot alter the observation."""

    _record: bytes

    @classmethod
    def from_quota(cls, record):
        return cls(json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True).encode())

    def health(self):
        record = json.loads(self._record)
        # The existing scarcity roll-up owns tightest-window/expiring selection.
        roll = scarcity._roll(record["windows"])
        return {"codex": {**core._health_row(record["scarcity"], roll["tightest"],
                            roll["expiring_pct"], record["refusing"]), "pools": {}}}

    def tokens_per_pct(self, candidate):
        return None

    def tokens_left(self, candidate):
        return None
