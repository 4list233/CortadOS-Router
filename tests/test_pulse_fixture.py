"""Provider-generated mod fixture: pin the actual snapshot projection without live state."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from cortados_router import core, resources, scarcity

FIXTURE = Path(__file__).resolve().parents[1] / "integrations/claude-mod/cortados-pulse/hooks/resources.fixture.ts"


class FixtureCore:
    def usage_summary(self, limit):
        return {"total_routes": 5}

    def lane_health(self, *, refresh):
        assert refresh is False
        return {
            "codex": {"ok": True, "scarcity": None},
            "claude#b": {"ok": True, "scarcity": -0.2, "config_dir": "/u/.claude-acctB",
                         "pools": {"fable": {"ok": True, "scarcity": 0.1}}},
            "local": {"ok": True, "resource_managed": True, "deployments": {"studio/model": {}},
                      "why": "available: measured local resources"},
        }

    def subscription_readings(self):
        return {"claude#b": {"config_dir": "/u/.claude-acctB", "fetched_at": 1699999940,
                            "windows": [{"window": "week", "pool": "fable", "headroom_pct": 55}]}}


def fixture():
    return resources.snapshot(FixtureCore(), now=1700000000)


def stale_fixture():
    """Exercise real freshness, health and /resources projections."""
    now = 1700000000
    cached = {"lanes": {"codex": {
        "scarcity": 1.2, "fetched_at": now - 7 * 3600, "tried_at": now - 60,
        "error": "sample failed", "windows": [
            {"window": "week", "pool": "all", "used_pct": 100, "headroom_pct": 0}]
    }}}
    with patch.object(core, "_scarcity_module", return_value=scarcity), \
         patch.object(scarcity, "current", return_value=cached), \
         patch.object(scarcity.time, "time", return_value=now), \
         patch.object(core, "usage_summary", return_value={"total_routes": 0}):
        return resources.snapshot(core, now=now)


class PulseFixtureTests(unittest.TestCase):
    def test_stale_mod_fixture_matches_real_provider_projection(self):
        path = FIXTURE.with_name("stale-resources.fixture.ts")
        self.assertEqual(json.loads(path.read_text().split("export default ", 1)[1]), stale_fixture())

    def test_mod_fixture_matches_provider_snapshot(self):
        text = FIXTURE.read_text()
        self.assertEqual(json.loads(text.split("export default ", 1)[1]), fixture())
