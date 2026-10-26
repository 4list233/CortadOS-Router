"""Phase A: Claude accounts expand into one scarcity lane each. No network/CLI — claude_lanes() and the trust pre-seed
are pure, driven by a temp tables dir."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from cortados_router import scarcity as sc


class ClaudeLanesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._old = os.environ.get("CORTADOS_ROUTER_TABLES")
        os.environ["CORTADOS_ROUTER_TABLES"] = self.dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CORTADOS_ROUTER_TABLES", None)
        else:
            os.environ["CORTADOS_ROUTER_TABLES"] = self._old

    def _write(self, data):
        (Path(self.dir) / "accounts.json").write_text(json.dumps(data))

    def test_no_accounts_file_is_single_default_login(self):
        self.assertEqual(sc.claude_lanes(), [("claude", None)])

    def test_empty_claude_list_is_single_default(self):
        self._write({"claude": []})
        self.assertEqual(sc.claude_lanes(), [("claude", None)])

    def test_two_accounts_expand_first_keeps_bare_name(self):
        self._write({"claude": [{"id": "a", "config_dir": "~/.claude-acctA"},
                                {"id": "b", "config_dir": "~/.claude-acctB"}]})
        lanes = sc.claude_lanes()
        self.assertEqual([k for k, _ in lanes], ["claude", "claude#b"])
        self.assertTrue(lanes[0][1].endswith(".claude-acctA"))      # config_dir is expanded to an absolute path
        self.assertTrue(lanes[1][1].endswith(".claude-acctB"))
        self.assertNotIn("~", lanes[0][1])

    def test_entry_without_config_dir_is_malformed_and_refuses(self):
        # a present-but-malformed config (an entry missing config_dir) must not silently default/ignore: refuse Claude sampling
        self._write({"claude": [{"id": "a", "config_dir": "~/.claude-acctA"}, {"id": "x"}]})
        self.assertEqual(sc.claude_lanes(), [])
        with self.assertRaises(ValueError):
            sc._accounts("claude")


class TrustPreseedTests(unittest.TestCase):
    def test_ensure_trust_accepts_the_home_project(self):
        d = tempfile.mkdtemp()
        sc._ensure_trust(d)                                   # no .claude.json yet: created
        cfg = json.loads((Path(d) / ".claude.json").read_text())
        self.assertTrue(cfg["projects"][str(Path.home())]["hasTrustDialogAccepted"])

    def test_ensure_trust_preserves_existing_config(self):
        d = tempfile.mkdtemp()
        (Path(d) / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {}}))
        sc._ensure_trust(d)
        cfg = json.loads((Path(d) / ".claude.json").read_text())
        self.assertEqual(cfg["keep"], 1)                      # untouched
        self.assertTrue(cfg["projects"][str(Path.home())]["hasTrustDialogAccepted"])


if __name__ == "__main__":
    unittest.main()
