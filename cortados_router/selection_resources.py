# PURPOSE: Freeze resolver quota and optional learned-cost inputs.
# CONTRACT: RT-FRESH-QUOTA-005; no refresh or disk reads during ranking.
# DEPENDS: Existing core health and learned-pool mapping owners.
"""Serialized resource snapshots cannot be altered by later observations."""
from dataclasses import dataclass
import json
from . import core, scarcity


@dataclass(frozen=True)
class SelectionResources:
    _record: bytes
    allowed_routes: tuple = ()

    @classmethod
    def from_snapshot(cls, lanes, health, costs, allowed_routes):
        data = dict(lanes=lanes, health=health, costs=costs)
        return cls(json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True).encode(), allowed_routes)

    @classmethod
    def from_quota(cls, record):
        roll = scarcity._roll(record['windows'])
        health = {'codex': {**core._health_row(record['scarcity'], roll['tightest'], roll['expiring_pct'], record['refusing']), 'pools': {}}}
        return cls.from_snapshot({'codex': record}, health, {}, ())

    def health(self):
        return json.loads(self._record)['health']

    def tokens_per_pct(self, candidate):
        data = json.loads(self._record)
        if not data['costs']:
            return None
        return core._pool_tokens_per_pct(candidate, lanes=data['lanes'], costs=data['costs'])

    def tokens_left(self, candidate):
        data = json.loads(self._record)
        if not data['costs']:
            return None
        return core._tokens_left_for(candidate, lanes=data['lanes'], costs=data['costs'])
