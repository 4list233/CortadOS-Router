"""Each Claude Code mod's manifest declares every setting its hooks read: Claude Code's configure/install flows
reject undeclared userConfig keys, so a setting missing from plugin.json cannot be set by a user (seen 2026-10-30:
orchestratorEnforce and subagentTimeoutMs were documented but undeclared). Both mods keep their defaults in a
`const FALLBACK: Cfg = { ... }` literal, so the same check reads both."""
import json
import re
import unittest
from pathlib import Path

MODS_DIR = Path(__file__).resolve().parent.parent / "integrations" / "claude-mod"
MODS = {"cortados-routed-subagents": "register.ts", "cortados-pulse": "register.ts"}


def fallback_defaults(mod: str) -> dict:
    """The `FALLBACK: Cfg = { ... }` object literal in the hooks module, as key -> literal source text."""
    src = (MODS_DIR / mod / "hooks" / MODS[mod]).read_text()
    body = re.search(r"const FALLBACK: Cfg = \{\n(.*?)\n\}", src, re.S).group(1)
    return dict(re.findall(r"^\s{2}(\w+): (.+?),?$", body, re.M))


def declared(mod: str) -> dict:
    return json.loads((MODS_DIR / mod / ".claude-plugin" / "plugin.json").read_text())["userConfig"]


class ManifestTests(unittest.TestCase):
    def test_every_setting_is_declared(self):
        for mod in MODS:
            with self.subTest(mod=mod):
                defaults = fallback_defaults(mod)
                self.assertTrue(defaults)
                self.assertEqual(set(defaults) - set(declared(mod)), set())

    def test_declared_defaults_match_the_code(self):
        for mod in MODS:
            config = declared(mod)
            for key, literal in fallback_defaults(mod).items():
                with self.subTest(mod=mod, key=key):
                    default = config[key].get("default")
                    if default is None:
                        continue                                # e.g. the sensitive inline token has no default
                    code = json.loads(literal.replace("_", "")) if literal[0] not in "'\"" else literal[1:-1]
                    self.assertEqual(default, code)

    def test_no_mod_defaults_to_a_provider_address(self):
        """The provider runs on another host, so an address default (the pane shipped 127.0.0.1:8432 until 2026-11-08)
        would be wrong on every install; the URL comes from config, CORTADOS_PROVIDER_URL or the URL file."""
        for mod in MODS:
            with self.subTest(mod=mod):
                self.assertEqual(declared(mod)["providerUrl"]["default"], "")
                self.assertEqual(declared(mod)["providerTokenFile"]["default"], "~/.config/cortados/provider-token")
                self.assertTrue(declared(mod)["providerToken"].get("sensitive"))
                self.assertNotIn("default", declared(mod)["providerToken"])

    def test_marketplace_lists_every_mod(self):
        market = json.loads((MODS_DIR.parent.parent / ".claude-plugin" / "marketplace.json").read_text())
        listed = {p["name"]: p["source"] for p in market["plugins"]}
        for mod in MODS:
            with self.subTest(mod=mod):
                self.assertEqual(listed.get(mod), f"./integrations/claude-mod/{mod}")


if __name__ == "__main__":
    unittest.main()
