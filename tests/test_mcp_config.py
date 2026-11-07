"""The project MCP config launches route_task without requiring client-side environment expansion.

The server resolves its URL from the process environment or the shared provider-url file and reads its token itself.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
class McpProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.server = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["cortados-route"]

    def test_launches_the_route_task_script_with_python(self):
        self.assertEqual(self.server["command"], "python3")               # not an arbitrary executable
        self.assertIn("cortados_router/subagent_mcp.py", self.server["args"])
        self.assertTrue((ROOT / "cortados_router" / "subagent_mcp.py").is_file(),
                        "the referenced server script must exist in the checkout")

    def test_config_does_not_require_or_embed_provider_environment(self):
        self.assertNotIn("env", self.server)
        self.assertNotIn("CORTADOS_PROVIDER_URL", json.dumps(self.server))
