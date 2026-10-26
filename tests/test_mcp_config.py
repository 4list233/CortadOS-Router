"""The committed project MCP config (.mcp.json) wires the route_task server in for anyone who installs the repo. It must
never carry a literal secret: the provider URL and token come from the environment, so a public checkout ships no address
or credential. The guards below are deliberately exact — a bare `${VAR}` form, not merely "looks like a placeholder" —
because Claude Code also expands a `${VAR:-default}` fallback, and a fallback default could smuggle a literal secret into
the committed file while still looking templated (Codex review 2026-10-26)."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BARE_PLACEHOLDER = re.compile(r"^\$\{[A-Z0-9_]+\}$")   # ${VAR} only — no :- fallback default (which could embed a literal)


class McpProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["cortados-route"]

    def test_launches_the_route_task_script_with_python(self):
        self.assertEqual(self.server["command"], "python3")               # not an arbitrary executable
        self.assertIn("cortados_router/subagent_mcp.py", self.server["args"])
        self.assertTrue((ROOT / "cortados_router" / "subagent_mcp.py").is_file(),
                        "the referenced server script must exist in the checkout")

    def test_env_values_are_bare_placeholders_never_literals_or_fallbacks(self):
        env = self.server.get("env", {})
        self.assertEqual(env.get("CORTADOS_PROVIDER_URL"), "${CORTADOS_PROVIDER_URL}")
        self.assertEqual(env.get("CORTADOS_PROVIDER_TOKEN"), "${CORTADOS_PROVIDER_TOKEN}")
        for key, value in env.items():
            self.assertRegex(value, BARE_PLACEHOLDER,
                             f"{key} must be a bare ${{VAR}} placeholder: no literal value, no ':-' fallback default")
