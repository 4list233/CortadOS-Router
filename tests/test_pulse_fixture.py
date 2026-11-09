"""Provider-generated mod fixture: pin the actual snapshot projection without live state."""
import json
import unittest
from pathlib import Path
from cortados_router import resources

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


class PulseFixtureTests(unittest.TestCase):
    def test_mod_fixture_matches_provider_snapshot(self):
        text = FIXTURE.read_text()
        self.assertEqual(json.loads(text.split("export default ", 1)[1]), fixture())
