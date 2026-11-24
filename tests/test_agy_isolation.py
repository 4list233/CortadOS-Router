"""agy_isolation (#144): the settings ALLOWLIST and the one launch path for every agy process (review round 1).

Fake agy binaries only (small scripts); no vendor CLI, no network, no account; HOME and CORTADOS_HOME are throwaway.
"""
from __future__ import annotations
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

os.environ.setdefault("CORTADOS_HOME", tempfile.mkdtemp())

from cortados_router import agy_isolation as iso
from cortados_router import scarcity as sc

ROOT = Path(__file__).resolve().parents[1]
# agy 1.5.16's real shapes (read on the Air 2026-11-24, key names and types only). The workspace paths are placeholders.
GOOD = {"trustedWorkspaces": ["/Users/example/projects/alpha", "/Users/example/projects/beta"]}
CONFIG = {"plugins": {"swarm": {"enabled": True}}}


class SettingsAllowlist(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.p = self.home / iso.SETTINGS_RELPATH
        self.p.parent.mkdir(parents=True)
        self.cfg = self.home / iso.CONFIG_RELPATH
        self.mcp = self.home / iso.MCP_CONFIG_RELPATH
        self.gem = self.home / iso.GEMINI_CLI_SETTINGS_RELPATH
        self.cfg.parent.mkdir(parents=True)
        # the Air's install exactly: settings, config with swarm, an EMPTY mcp_config, no Gemini CLI settings.json
        self.p.write_text(json.dumps(GOOD))
        self.cfg.write_text(json.dumps(CONFIG))
        self.mcp.write_text("")

    @staticmethod
    def put(path: Path, data) -> None:
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def check(self, data=None, path=None):
        if data is not None:
            self.put(path or self.p, data)
        iso.check_settings(self.home)

    def refused(self, data, why: str = "", path=None):
        with self.assertRaises(iso.SettingsRefused) as cm:
            self.check(data, path)
        if why:
            self.assertRegex(str(cm.exception), why)
        return str(cm.exception)

    def test_the_real_install_passes(self):
        self.check()
        self.assertEqual(self.mcp.stat().st_size, 0)
        self.assertFalse(self.gem.exists())
        self.check({"trustedWorkspaces": []})
        self.check({})
        self.check({"plugins": {"swarm": {"enabled": False}}}, self.cfg)
        self.check({}, self.cfg)
        self.cfg.unlink(); self.check()                                    # no config.json: no plugin configured
        self.mcp.unlink(); self.check()                                    # no mcp_config.json: no server

    def test_missing_agy_settings_is_first_run_pending_not_unsafe(self):
        self.p.unlink()
        with self.assertRaises(iso.FirstRunPending) as cm:
            iso.check_settings(self.home)
        self.assertNotIsInstance(cm.exception, iso.SettingsRefused)
        self.assertEqual(cm.exception.kind, "first-run-pending")
        self.assertEqual(sc.lane_state(sc._failed_record("antigravity", None, cm.exception)), "first-run setup pending")

    def test_agy_settings_allowlist(self):
        self.refused([], "not a JSON object")
        self.refused("null", "not a JSON object")
        self.refused("{not json", "fail closed")
        self.refused("", "fail closed")                                    # settings.json empty is not the real shape
        self.refused({"trustedWorkspaces": "/Users/example"}, "trustedWorkspaces is not a list")
        self.refused({"trustedWorkspaces": ["/a", 1]}, "holds an item that is not a str")
        self.refused({**GOOD, "renderingMode": "alt-screen"}, "unknown setting 'renderingMode'")
        self.refused({**GOOD, "security": {"auth": {"selectedType": "oauth-personal"}}}, "security settings")
        for extra, why in (({"mcpServers": {"x": {"command": "y"}}}, "MCP servers"),
                           ({"tools": {"autoAccept": True}}, "tool settings"),
                           ({"approvalMode": "always-proceed"}, "approval mode"),
                           ({"yolo": True}, "yolo")):
            with self.subTest(extra=extra):
                self.refused({**GOOD, **extra}, why)

    def test_config_allowlist_refuses_unknown_plugins_and_keys(self):
        self.refused({"plugins": {"swarm": {"enabled": True}, "browser": {"enabled": True}}}, "unknown setting 'plugins.browser'",
                     self.cfg)
        self.refused({"plugins": {"swarm": {"enabled": True, "maxAgents": 8}}}, "unknown setting 'plugins.swarm.maxAgents'",
                     self.cfg)
        self.refused({"plugins": {"swarm": {"enabled": "yes"}}}, r"plugins\.swarm\.enabled is not a bool", self.cfg)
        self.refused({"plugins": {"swarm": {"enabled": 1}}}, r"plugins\.swarm\.enabled is not a bool", self.cfg)
        self.refused({**CONFIG, "mcpServers": {"x": {}}}, "MCP servers", self.cfg)
        self.refused([], "not a JSON object", self.cfg)
        self.refused("{garbage", "fail closed", self.cfg)

    def test_mcp_config_empty_shapes_pass(self):
        for ok in ("", "   \n\t", "{}", '{"mcpServers": {}}', ' { "mcpServers" : { } } '):
            with self.subTest(ok=ok):
                self.check(ok, self.mcp)

    def test_mcp_config_with_a_server_or_garbage_is_refused(self):
        self.refused({"mcpServers": {"fs": {"command": "npx", "args": ["x"]}}}, "MCP servers are configured", self.mcp)
        self.refused("not json at all", "fail closed", self.mcp)
        self.refused("[]", "only an empty file", self.mcp)
        self.refused("null", "only an empty file", self.mcp)
        self.refused({"mcpServers": []}, "only an empty file", self.mcp)
        self.refused({"mcpServers": {}, "other": 1}, "only an empty file", self.mcp)

    def test_gemini_cli_settings_billing_modes_are_refused(self):
        for mode in ("gemini-api-key", "api-key", "vertex-ai", "compute-default-credentials", "cloud-shell", "adc", "API-KEY"):
            with self.subTest(mode=mode):
                self.refused({"security": {"auth": {"selectedType": mode}}}, "bills outside", self.gem)
                self.refused({"selectedAuthType": mode}, "bills outside", self.gem)       # the older top-level key
        self.refused({"mcpServers": {"x": {"command": "y"}}}, "MCP servers", self.gem)
        self.refused("{garbage", "fail closed", self.gem)
        self.refused([], "not a JSON object", self.gem)

    def test_gemini_cli_settings_login_passes_and_unknown_modes_are_not_echoed(self):
        self.check({"security": {"auth": {"selectedType": "oauth-personal"}}, "ui": {"theme": "x"}, "general": {}}, self.gem)
        self.check({}, self.gem)
        msg = self.refused({"security": {"auth": {"selectedType": "AIzaSySECRETVALUE"}}}, "not the Google login", self.gem)
        self.assertNotIn("AIzaSySECRETVALUE", msg)

    def test_values_are_never_echoed(self):
        msg = self.refused({"trustedWorkspaces": ["/Users/private-name/x", 3]})
        self.assertNotIn("private-name", msg)


class SubscriptionProof(unittest.TestCase):
    USAGE = ("╭ Models & Quota ╮\nAccount: someone.private@example.com\nPlan: Google AI Pro\n"
             "GEMINI MODELS\nWeekly Limit Remaining 87.50%\n")

    def confirmed(self):
        return mock.patch.object(iso, "TIER_PATTERN_CONFIRMED", True)

    def test_provisional_pattern_keeps_the_lane_refusing(self):
        self.assertFalse(iso.TIER_PATTERN_CONFIRMED)
        with self.assertRaisesRegex(iso.SubscriptionUnproven, r"PROVISIONAL.*\[ESCALATE\]") as cm:
            iso.check_subscription(self.USAGE)
        self.assertEqual(cm.exception.kind, "subscription-unproven")
        self.assertNotIn("someone.private", str(cm.exception))
        self.assertEqual(sc.lane_state(sc._failed_record("antigravity", None, cm.exception)),
                         "refused: subscription login not proven")

    def test_personal_tiers_pass_once_confirmed(self):
        with self.confirmed():
            self.assertEqual(iso.check_subscription(self.USAGE), "AI Pro")
            self.assertEqual(iso.check_subscription("(Google AI Pro) Gemini 4 Neon (High)"), "AI Pro")
            self.assertEqual(iso.check_subscription("Plan: AI Ultra"), "AI Ultra")

    def test_non_subscription_screens_are_refused(self):
        with self.confirmed():
            for screen in ("Plan: Google AI Pro\nAuth: API key", "Plan: Google AI Pro\nVertex AI project my-proj",
                           "Gemini Code Assist Enterprise", "Plan: Pay-as-you-go", "Pay as you go billing",
                           "Using Application Default Credentials", "Account: x@y.com\nGEMINI MODELS",
                           "Plan: Free", ""):
                with self.subTest(screen=screen), self.assertRaises(iso.SubscriptionUnproven) as cm:
                    iso.check_subscription(screen)
                self.assertNotIn("x@y.com", str(cm.exception))
                self.assertNotIn("my-proj", str(cm.exception))

    def test_account_email_is_scrubbed(self):
        self.assertEqual(iso.scrub_account("Account: a.b+c@mail.example.co.uk |"), "Account: <account> |")


FAKE_AGY = r'''#!{py}
import os, sys
with open({log!r}, "w") as f:
    f.write(repr(sys.argv[1:]) + "\n" + os.getcwd() + "\n" + "\n".join(sorted(os.environ)))
print("gemini-4-neon-high        Gemini 4 Neon (High)")
print("claude-opus-5-5-high      Claude Opus 5.5 (High)")
'''


class OneLaunchPath(unittest.TestCase):
    """`agy models` (bin/refresh-inventory.sh) runs through the same prepare() as the quota probe."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / iso.SETTINGS_RELPATH).parent.mkdir(parents=True)
        (self.home / iso.SETTINGS_RELPATH).write_text(json.dumps(GOOD))
        self.log = self.tmp / "ran"
        self.bin = self.tmp / "bin" / "agy"
        self.bin.parent.mkdir()
        self.bin.write_text(FAKE_AGY.format(py=sys.executable, log=str(self.log)))
        self.bin.chmod(0o755)
        self.environ = {"HOME": str(self.home), "USER": "t", "PATH": str(self.bin.parent) + ":/usr/bin:/bin",
                        "GEMINI_API_KEY": "gk", "AGY_ADC_AUTH": "true", "ANTHROPIC_API_KEY": "a"}

    def test_run_models_is_isolated(self):
        out = iso.run_models(str(self.bin), self.environ, {})
        self.assertIn("gemini-4-neon-high", out)
        argv, cwd, *names = self.log.read_text().split("\n")
        self.assertEqual(argv, "['models']")
        self.assertNotEqual(Path(cwd).resolve(), ROOT.resolve())
        self.assertFalse(Path(cwd).exists())                            # its per-run folder is removed
        for n in ("GEMINI_API_KEY", "AGY_ADC_AUTH", "ANTHROPIC_API_KEY"):
            self.assertNotIn(n, names)
        for n in names:
            self.assertTrue(iso.allowed(n) or n == "PATH" or n.startswith("__CF") or n.startswith("__PYVENV"), n)

    def test_relative_binary_is_refused_and_never_searched_on_path(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            with self.assertRaisesRegex(iso.ProbeBinaryMissing, "not absolute"):
                iso.run_models("agy", self.environ, {})                  # "agy" IS on self.environ's PATH
        self.assertFalse(self.log.exists())

    def test_guards_run_before_any_start(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            with self.assertRaises(iso.ForbiddenEnv):
                iso.run_models(str(self.bin), self.environ, {"GOOGLE_API_KEY": "x"})
            (self.home / iso.MCP_CONFIG_RELPATH).parent.mkdir(parents=True)
            (self.home / iso.MCP_CONFIG_RELPATH).write_text(json.dumps({"mcpServers": {"x": {"command": "y"}}}))
            with self.assertRaises(iso.SettingsRefused):
                iso.run_models(str(self.bin), self.environ, {})
            (self.home / iso.SETTINGS_RELPATH).unlink()
            with self.assertRaises(iso.FirstRunPending):
                iso.run_models(str(self.bin), self.environ, {})
            with self.assertRaises(iso.LaneNotConfigured):
                iso.run_models(None, self.environ, {})
        self.assertFalse(self.log.exists())

    def test_cli_entrypoint_exit_codes(self):
        state = self.tmp / "state"
        state.mkdir()
        with mock.patch.object(sc, "STATE", state):
            for configured, code in (("agy", 2), (None, 2), (str(self.bin), 0)):
                env = dict(self.environ)
                if configured:
                    env["CORTADOS_AGY_BIN"] = configured
                out, err = io.StringIO(), io.StringIO()
                with self.subTest(configured=configured), mock.patch.dict(os.environ, env, clear=True), \
                        redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(iso.main(["models"]), code)
                if code == 0:
                    self.assertIn("claude-opus-5-5-high", out.getvalue())
                else:
                    self.assertEqual(out.getvalue(), "")
                    self.assertRegex(err.getvalue(), r"^agy: (binary-missing|not-configured): ")
            (state / "env").write_text("GEMINI_API_KEY=x\n")
            with mock.patch.dict(os.environ, {**self.environ, "CORTADOS_AGY_BIN": str(self.bin)}, clear=True), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(iso.main(["models"]), 2)
            self.assertIn("forbidden-env", err.getvalue())
        with redirect_stderr(io.StringIO()):
            self.assertEqual(iso.main(["something-else"]), 2)

    def test_refresh_script_never_starts_agy_itself(self):
        script = (ROOT / "bin" / "refresh-inventory.sh").read_text()
        code = "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn("python3 -B -m cortados_router.agy_isolation models", code)
        self.assertNotRegex(code, r'"\$\{?CORTADOS_AGY_BIN')               # never executed (or tested) directly
        self.assertNotRegex(code, r"(^|[\s;|&(])agy\s+models")               # nor `agy models` by name on PATH


if __name__ == "__main__":
    unittest.main()
