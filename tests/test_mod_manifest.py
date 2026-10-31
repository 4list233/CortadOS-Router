"""The routed-subagents mod's manifest declares every setting its hooks read: Claude Code's configure/install flows
reject undeclared userConfig keys, so a setting missing from plugin.json cannot be set by a user (seen 2026-10-30:
orchestratorEnforce and subagentTimeoutMs were documented but undeclared)."""
import json
import re
import unittest
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "integrations" / "claude-mod" / "cortados-routed-subagents"


def fallback_defaults() -> dict:
    """The `FALLBACK: Cfg = { ... }` object literal in register.ts, as key -> literal source text."""
    src = (MOD / "hooks" / "register.ts").read_text()
    body = re.search(r"const FALLBACK: Cfg = \{\n(.*?)\n\}", src, re.S).group(1)
    return dict(re.findall(r"^\s{2}(\w+): (.+?),?$", body, re.M))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((MOD / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
        self.defaults = fallback_defaults()

    def test_every_setting_is_declared(self):
        self.assertTrue(self.defaults)
        self.assertEqual(set(self.defaults) - set(self.config), set())

    def test_declared_defaults_match_the_code(self):
        for key, literal in self.defaults.items():
            declared = self.config[key].get("default")
            if declared is None:
                continue                                    # e.g. the sensitive inline token has no default
            code = json.loads(literal.replace("_", "")) if literal[0] not in "'\"" else literal[1:-1]
            self.assertEqual(declared, code, key)


if __name__ == "__main__":
    unittest.main()
