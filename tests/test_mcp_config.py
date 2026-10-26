"""The committed project MCP config (.mcp.json) wires the route_task server in for anyone who installs the repo. It must
never carry a literal secret: the provider URL and token come from the environment, so a public checkout ships no address
or credential."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class McpProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / ".mcp.json").read_text())

    def test_declares_the_route_task_server(self):
        servers = self.cfg["mcpServers"]
        self.assertIn("cortados-route", servers)
        server = servers["cortados-route"]
        self.assertTrue(any("subagent_mcp.py" in a for a in server["args"]),
                        "the server must launch cortados_router/subagent_mcp.py")

    def test_env_values_are_placeholders_not_literal_secrets(self):
        env = self.cfg["mcpServers"]["cortados-route"].get("env", {})
        self.assertIn("CORTADOS_PROVIDER_URL", env)
        self.assertIn("CORTADOS_PROVIDER_TOKEN", env)
        for key, value in env.items():
            self.assertTrue(value.startswith("${") and value.endswith("}"),
                            f"{key} must be a ${{...}} environment placeholder, never a literal value")
