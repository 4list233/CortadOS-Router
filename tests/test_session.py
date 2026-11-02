import io
import os
import tempfile
import unittest

from cortados_router import session

SECRET = "tok-SECRET-123"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tok = os.path.join(self.tmp.name, "token")
        with open(self.tok, "w") as f:
            f.write(SECRET + "\n")
        self.base = {"PATH": "/usr/bin", "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": "console-key"}

    def run_main(self, *argv, base=None):
        out, err = io.StringIO(), io.StringIO()
        rc = session.main(list(argv), base_env=self.base if base is None else base, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_model_mapping(self):
        self.assertEqual(session.resolve_model("fable"), "sub/claude-fable-5-1")
        self.assertEqual(session.resolve_model("opus"), "sub/claude-opus-5-5")
        self.assertEqual(session.resolve_model("sonnet"), "sub/claude-sonnet-5-5")
        self.assertEqual(session.resolve_model("claude-opus-5-5"), "sub/claude-opus-5-5")
        self.assertEqual(session.resolve_model("sub/claude-x"), "sub/claude-x")
        self.assertEqual(session.resolve_model("cortados"), "cortados")

    def test_env(self):
        _, env, cmd = session.prepare(["--provider-url", "http://router.example:8787", "--token-file", self.tok], self.base)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://router.example:8787")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], SECRET)
        self.assertEqual(env["ANTHROPIC_API_KEY"], SECRET)
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "cortados")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"], "1")
        self.assertEqual(env["CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL"], "cortados")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "sub/claude-haiku-5-5")
        self.assertIn("x-cortados-reveal: 1", env["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertEqual(env["CORTADOS_WHOLE_SESSION"], "1")
        self.assertEqual(env["CLAUDE_CODE_ENTRYPOINT"], "cli")
        for k in session.BLANKED:
            self.assertEqual(env[k], "")
        self.assertEqual(cmd[:3], ["claude", "--model", "sub/claude-fable-5-1"])
        self.assertIn("--settings", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("MCP", cmd[cmd.index("--append-system-prompt") + 1])

    def test_url_from_env_and_extra_args(self):
        base = dict(self.base, CORTADOS_PROVIDER_URL="http://r.example")
        _, env, cmd = session.prepare(
            ["--orchestrator", "opus", "--token-file", self.tok, "--no-delegate-directive", "--", "-c", "--verbose"], base
        )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://r.example")
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[2], "sub/claude-opus-5-5")
        self.assertEqual(cmd[-2:], ["-c", "--verbose"])

    def test_missing_url(self):
        rc, out, err = self.run_main("--token-file", self.tok, "--print-env")
        self.assertEqual(rc, 2)
        self.assertIn("provider URL", err)

    def test_missing_token(self):
        rc, _, err = self.run_main("--provider-url", "http://r.example", "--token-file", self.tok + ".nope")
        self.assertEqual(rc, 2)
        self.assertIn("token", err)

    def test_print_env_masks(self):
        rc, out, err = self.run_main("--provider-url", "http://r.example", "--token-file", self.tok, "--print-env")
        self.assertEqual(rc, 0, err)
        self.assertNotIn(SECRET, out)
        self.assertIn("ANTHROPIC_AUTH_TOKEN=***", out)
        self.assertIn("ANTHROPIC_API_KEY=***", out)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=cortados", out)
        self.assertIn("argv: claude --model sub/claude-fable-5-1", out)


if __name__ == "__main__":
    unittest.main()
