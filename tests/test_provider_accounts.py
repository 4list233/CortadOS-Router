"""Phase A increment 3: the provider executes a routed Claude account on that account's login — the bridge sets its
CLAUDE_CONFIG_DIR, the allowlist and degraded filter are account-aware, and the config dir resolves from accounts.json."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cortados_router.provider import routing
from cortados_router.provider.bridge import BridgeError, BridgeSession
from cortados_router.provider.server import Registry


class BridgeConfigDirTests(unittest.TestCase):
    def test_config_dir_becomes_claude_config_dir_in_child_env(self):
        seen = {}

        def fake_popen(args, **kw):
            seen["env"] = kw.get("env")
            raise OSError("stop before running")             # record env, then abort; __init__ maps this to BridgeError
        with self.assertRaises(BridgeError):
            BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9", token="t",
                          claude_bin="claude", config_dir="/tmp/acctB", popen=fake_popen)
        self.assertEqual(seen["env"].get("CLAUDE_CONFIG_DIR"), "/tmp/acctB")

    def test_no_config_dir_leaves_claude_config_dir_unset(self):
        seen = {}

        def fake_popen(args, **kw):
            seen["env"] = kw.get("env"); raise OSError("stop")
        with self.assertRaises(BridgeError):
            BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9", token="t",
                          claude_bin="claude", popen=fake_popen)
        self.assertNotIn("CLAUDE_CONFIG_DIR", seen["env"])    # default login


class ServedRoutesTests(unittest.TestCase):
    def _core(self, accts):
        return SimpleNamespace(
            model_scores=lambda: {"models": [{"reach": [{"provider": "claude-cli", "model": "m1"},
                                                        {"provider": "openai-codex", "model": "g1"}]}]},
            table=lambda: {"candidates": {}},
            _vendor_accounts=lambda v, bl, bp: accts)

    def test_single_account_unchanged(self):
        core = self._core([{"provider": "claude-cli", "account": None}])
        self.assertEqual(set(routing.served_routes(core)), {("claude-cli", "m1"), ("codex-cli", "g1")})

    def test_two_accounts_expand_claude_pairs(self):
        core = self._core([{"provider": "claude-cli", "account": "a"}, {"provider": "claude-cli#b", "account": "b"}])
        served = set(routing.served_routes(core))
        self.assertIn(("claude-cli", "m1"), served)
        self.assertIn(("claude-cli#b", "m1"), served)         # B's candidate survives the allowlist
        self.assertIn(("codex-cli", "g1"), served)            # codex clone unaffected


class DegradedFilterTests(unittest.TestCase):
    def test_degraded_excludes_every_claude_account(self):
        rec = {}
        inner = SimpleNamespace(resolve=lambda decision, *a, allowed_routes=None, **k: rec.update(allowed=allowed_routes))
        routing._NoClaudeWhenDegraded(inner).resolve(
            {"degraded": True, "answers": None},
            allowed_routes=[("claude-cli", "m"), ("claude-cli#b", "m"), ("codex-cli", "g")])
        self.assertNotIn(("claude-cli", "m"), rec["allowed"])
        self.assertNotIn(("claude-cli#b", "m"), rec["allowed"])   # a second account is still Claude: not when degraded
        self.assertIn(("codex-cli", "g"), rec["allowed"])


class ConfigDirResolverTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        (Path(self.dir) / "accounts.json").write_text(json.dumps({"claude": [
            {"id": "a", "config_dir": "~/.claude-acctA"}, {"id": "b", "config_dir": "~/.claude-acctB"}]}))
        self._old = os.environ.get("CORTADOS_ROUTER_TABLES")
        os.environ["CORTADOS_ROUTER_TABLES"] = self.dir

    def tearDown(self):
        os.environ.pop("CORTADOS_ROUTER_TABLES", None) if self._old is None else os.environ.__setitem__("CORTADOS_ROUTER_TABLES", self._old)

    def test_account_resolves_to_its_config_dir(self):
        reg = Registry(gateway_url="http://x", token="t")
        self.assertTrue(reg.claude_config_dir("b").endswith(".claude-acctB"))
        self.assertTrue(reg.claude_config_dir("a").endswith(".claude-acctA"))
        self.assertIsNone(reg.claude_config_dir(None))        # no account named → default login


if __name__ == "__main__":
    unittest.main()
