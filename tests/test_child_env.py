"""#106: every vendor CLI the provider starts runs on an allowlisted environment (cortados_router/provider/child_env.py).

The spawn tests start real (fake) child processes through the three spawn sites, the Codex bridge, the Claude bridge
and the search lane, and read the environment the child actually got. This module deliberately does NOT import
tests.provider_fakes: nothing widens the allowlist here.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cortados_router.provider import bridge, child_env, codex_bridge, codex_isolation, search_lane
from cortados_router.provider.bridge import BridgeError

# Secrets a provider process can hold. None may reach any backend child.
SECRETS = {"GITHUB_TOKEN": "ghp_x", "GH_TOKEN": "gh_x", "AWS_SECRET_ACCESS_KEY": "aws_x", "AWS_ACCESS_KEY_ID": "AKIA",
           "CORTADOS_PROVIDER_TOKEN": "cortados_x", "SSH_AUTH_SOCK": "/tmp/agent.sock", "OPENAI_API_KEY": "sk-x",
           "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8432",
           "ANTHROPIC_CUSTOM_HEADERS": "x-cortados-run-id: r", "CODEX_API_KEY": "sk-c", "CODEX_ACCESS_TOKEN": "t",
           "OPENAI_BASE_URL": "http://elsewhere", "CLAUDE_CODE_USE_BEDROCK": "1", "SOME_FUTURE_SECRET": "s"}
# Infrastructure every child needs, with the values the provider was started with.
PASSED = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/Users/x"),
          "HTTP_PROXY": "http://proxy:3128", "http_proxy": "http://proxy:3128",
          "HTTPS_PROXY": "http://proxy:3128", "https_proxy": "http://proxy:3128",
          "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
          # an authenticated proxy URL passes unchanged: proxy credentials the owner configured (accepted exception)
          "ALL_PROXY": "socks5://owner:pw@proxy:1080", "all_proxy": "socks5://owner:pw@proxy:1080",
          "SSL_CERT_FILE": "/ca/bundle.pem", "SSL_CERT_DIR": "/ca", "REQUESTS_CA_BUNDLE": "/ca/bundle.pem",
          "NODE_EXTRA_CA_CERTS": "/ca/bundle.pem", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TZ": "UTC"}
CODEX_CA = {"CODEX_CA_CERTIFICATE": "/ca/macos-system-roots.pem"}

# A fake CLI: writes its whole environment to the file named by its first argument, then exits without answering.
DUMP_ENV = "import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))\n"


def _allowed_or_injected(name: str, backend: str) -> bool:
    # __PYVENV_LAUNCHER__ / PYTHON*: what a macOS framework Python may add to its own environment at startup
    return child_env.allowed(name, backend) or name == "__PYVENV_LAUNCHER__"


class BackendEnvTests(unittest.TestCase):
    def test_secrets_are_dropped_and_infrastructure_is_kept(self):
        for backend in ("codex", "claude"):
            env = child_env.backend_env(backend, environ={**SECRETS, **PASSED, **CODEX_CA, "KEEP_ME": "1"})
            for name in SECRETS:
                self.assertNotIn(name, env, (backend, name))
            self.assertNotIn("KEEP_ME", env, "an unlisted variable never passes, whatever its name")
            for name, value in PASSED.items():
                self.assertEqual(env.get(name), value, (backend, name))

    def test_all_proxy_passes_in_both_cases_credentials_unchanged(self):
        environ = {"ALL_PROXY": "http://u:p@proxy:3128", "all_proxy": "socks5h://u:p@proxy:1080"}
        for backend in ("codex", "claude"):
            self.assertEqual(child_env.backend_env(backend, environ=environ), environ, backend)

    def test_each_backend_gets_only_its_own_variables(self):
        environ = {"CODEX_HOME": "/h", **CODEX_CA, "CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        self.assertEqual(child_env.backend_env("codex", environ=environ), {"CODEX_HOME": "/h", **CODEX_CA})
        self.assertEqual(child_env.backend_env("claude", environ=environ),
                         {"CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_CODE_OAUTH_TOKEN": "tok"})

    def test_extras_win_and_are_passed_even_when_not_listed(self):
        env = child_env.backend_env("codex", {"CODEX_HOME": "/abs/home", "X_SET_ON_PURPOSE": "1"},
                                    environ={"CODEX_HOME": "rel/home"})
        self.assertEqual(env, {"CODEX_HOME": "/abs/home", "X_SET_ON_PURPOSE": "1"})

    def test_unknown_backend_is_refused(self):
        with self.assertRaises(ValueError):
            child_env.backend_env("cursor")

    def test_the_lists_hold_no_vendor_secret_family(self):
        # The allowlist must never grow a key/token family by accident: the only vendor-prefixed entries are the
        # four named ones, each with its reason in child_env.py.
        listed = set(child_env.BASE_ENV).union(*child_env.BACKEND_ENV.values())
        vendor = {n for n in listed if n.startswith(("OPENAI_", "CODEX_", "ANTHROPIC_", "CLAUDE_", "CORTADOS_",
                                                      "GITHUB_", "GH_", "AWS_", "SSH_"))}
        self.assertEqual(vendor, {"CODEX_HOME", "CODEX_CA_CERTIFICATE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"})
        self.assertEqual(child_env.BASE_PREFIXES, ("LC_",))

    def test_builders_follow_the_allowlist(self):
        with mock.patch.dict(os.environ, {**SECRETS, **PASSED, **CODEX_CA}):
            codex = codex_isolation.child_env("/abs/home")
            claude = bridge.claude_env(None)
            account = bridge.claude_env("/cfg/b", {"MCP_TOOL_TIMEOUT": "5"})
        for env in (codex, claude, account):
            self.assertFalse(set(SECRETS) & set(env))
        self.assertEqual((codex["CODEX_HOME"], codex["CODEX_CA_CERTIFICATE"]), ("/abs/home", CODEX_CA["CODEX_CA_CERTIFICATE"]))
        self.assertNotIn("CODEX_CA_CERTIFICATE", claude)
        self.assertEqual((account["CLAUDE_CONFIG_DIR"], account["MCP_TOOL_TIMEOUT"]), ("/cfg/b", "5"))


class SpawnSiteTests(unittest.TestCase):
    """Each spawn site, with a provider environment full of secrets, against a fake CLI that dumps what it got."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = Path(self.tmp, "fake_cli.py")
        self.fake.write_text(DUMP_ENV)
        self.dump = Path(self.tmp, "env.json")
        self.bin = [sys.executable, str(self.fake), str(self.dump)]
        self.env = mock.patch.dict(os.environ, {**SECRETS, **PASSED, **CODEX_CA, "CODEX_HOME": "/inherited/home"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def child_env_seen(self) -> dict:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                return json.loads(self.dump.read_text())
            except (OSError, ValueError):
                time.sleep(0.05)
        self.fail("the fake CLI never wrote its environment")

    def assert_clean(self, seen: dict, backend: str, extras: set) -> None:
        for name in SECRETS:
            self.assertNotIn(name, seen, name)
        for name, value in PASSED.items():
            self.assertEqual(seen.get(name), value, name)
        stray = {k for k in seen if not _allowed_or_injected(k, backend) and k not in extras}
        self.assertEqual(stray, set(), "only allowlisted variables and the call's own extras reach the child")

    def test_codex_app_server(self):
        home = Path(self.tmp, "codex-home")
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-test"\n')
        with self.assertRaises(BridgeError):                    # the fake answers nothing; the handshake fails
            codex_bridge.CodexSession(system="", tools=[], codex_bin=self.bin, codex_home=str(home),
                                      scratch_root=self.tmp)
        seen = self.child_env_seen()
        self.assert_clean(seen, "codex", {"CODEX_HOME"})
        self.assertEqual(seen["CODEX_HOME"], str(home), "the bridge's resolved home, not the inherited one")
        self.assertEqual(seen["CODEX_CA_CERTIFICATE"], CODEX_CA["CODEX_CA_CERTIFICATE"])

    def test_claude_bridge(self):
        s = bridge.BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9",
                                 claude_bin=self.bin, scratch_root=self.tmp)
        try:
            seen = self.child_env_seen()
        finally:
            s.close()
        self.assert_clean(seen, "claude", {"MCP_TOOL_TIMEOUT"})
        self.assertIn("MCP_TOOL_TIMEOUT", seen)
        self.assertNotIn("CODEX_CA_CERTIFICATE", seen)

    def test_claude_bridge_on_a_routed_account(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "ambient"}):
            s = bridge.BridgeSession(system="", tools=[], gateway_url="http://127.0.0.1:9",
                                     claude_bin=self.bin, scratch_root=self.tmp, config_dir="/cfg/b")
            try:
                seen = self.child_env_seen()
            finally:
                s.close()
        self.assert_clean(seen, "claude", {"MCP_TOOL_TIMEOUT"})
        self.assertEqual(seen["CLAUDE_CONFIG_DIR"], "/cfg/b")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", seen)

    def test_search_lane(self):
        with self.assertRaises(search_lane.SearchFailed):      # no result event: bad_output
            search_lane.run(self.bin, "weather", None, timeout=30)
        self.assert_clean(self.child_env_seen(), "claude", set())


if __name__ == "__main__":
    unittest.main()
