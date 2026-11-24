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
GOOD = {"security": {"auth": {"selectedType": "oauth-personal"}}}
# The owner's signed-in settings (keys of ~/.gemini/settings.json on the Pro, 2026-11-24) minus the keys that are not
# provably harmless (ide.enabled true, general.*, output.*): what the guard must accept.
OWNER_LIKE = {"security": {"auth": {"selectedType": "oauth-personal"}},
              "ide": {"hasSeenNudge": True, "enabled": False},
              "ui": {"showStatusInTitle": True, "showMemoryUsage": True, "footer": {"hideContextPercentage": False},
                     "theme": "Default Dark"},
              "hasSeenIdeIntegrationNudge": True}


class SettingsAllowlist(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.p = self.home / iso.SETTINGS_RELPATH
        self.p.parent.mkdir(parents=True)

    def check(self, data):
        self.p.write_text(data if isinstance(data, str) else json.dumps(data))
        iso.check_settings(self.home)

    def refused(self, data, why: str = ""):
        with self.assertRaises(iso.SettingsRefused) as cm:
            self.check(data)
        if why:
            self.assertRegex(str(cm.exception), why)
        return str(cm.exception)

    def test_subscription_login_is_the_recorded_value(self):
        self.assertEqual(iso.SUBSCRIPTION_AUTH, "oauth-personal")

    def test_accepts_only_known_keys_with_the_subscription_login(self):
        self.check(GOOD)
        self.check(OWNER_LIKE)

    def test_reviewer_cases_are_refused(self):
        self.refused([], "not a JSON object")
        self.refused({"security": {"auth": {"selectedType": "api-key"}}}, "bills outside")
        self.refused({"security": {"auth": {"selectedType": "oauth-personal", "useExternal": True}}},
                     "unknown setting 'security.auth.useExternal'")
        self.refused({"security": {"auth": {"selectedType": "oauth-personal"}, "folderTrust": {"enabled": False}}},
                     "unknown setting 'security.folderTrust'")

    def test_every_billing_mode_is_refused(self):
        for mode in ("gemini-api-key", "vertex-ai", "compute-default-credentials", "cloud-shell", "adc", "API-KEY"):
            with self.subTest(mode=mode):
                self.refused({"security": {"auth": {"selectedType": mode}}}, "bills outside")

    def test_auth_mode_is_required(self):
        self.refused({}, r"security\.auth\.selectedType missing")
        self.refused({"ui": {"theme": "x"}}, r"security\.auth\.selectedType missing")
        self.refused({"security": {"auth": {}}}, r"security\.auth\.selectedType missing")
        self.refused({"security": {"auth": {"selectedType": 1}}}, "not the subscription login")
        self.refused({"security": {"auth": {"selectedType": None}}}, "not the subscription login|missing")

    def test_a_secret_like_auth_value_is_not_echoed(self):
        msg = self.refused({"security": {"auth": {"selectedType": "AIzaSySECRETVALUE"}}})
        self.assertNotIn("AIzaSySECRETVALUE", msg)

    def test_missing_and_unreadable_files_are_refused(self):
        self.assertFalse(self.p.exists())
        with self.assertRaisesRegex(iso.SettingsRefused, "missing"):
            iso.check_settings(self.home)
        self.refused("{not json", "fail closed")
        self.refused("null", "not a JSON object")
        self.refused('"oauth-personal"', "not a JSON object")

    def test_mcp_and_auto_approve_are_refused(self):
        for extra, why in (({"mcpServers": {"x": {"command": "y"}}}, "MCP servers"),
                           ({"mcpServers": {}}, "MCP servers"),
                           ({"tools": {"autoAccept": True}}, "tool settings"),
                           ({"general": {"approvalMode": "yolo"}}, "unknown setting 'general'"),
                           ({"approvalMode": "always-proceed"}, "approval mode"),
                           ({"yolo": True}, "yolo"),
                           ({"permissions": {"mode": "proceed-in-sandbox"}}, "unknown setting 'permissions'")):
            with self.subTest(extra=extra):
                self.refused({**GOOD, **extra}, why)

    def test_unknown_keys_anywhere_are_refused_and_types_are_strict(self):
        self.refused({**GOOD, "renderingMode": "alt-screen"}, "unknown setting 'renderingMode'")
        self.refused({**GOOD, "ui": {"theme": "dark", "customThemes": {}}}, "unknown setting 'ui.customThemes'")
        self.refused({**GOOD, "ide": {"enabled": True}}, r"ide\.enabled has a value outside")
        self.refused({**GOOD, "ide": {"enabled": 0}}, r"ide\.enabled has a value outside")      # 0 is not False
        self.refused({**GOOD, "ui": {"showMemoryUsage": "yes"}}, r"ui\.showMemoryUsage is not a bool")
        self.refused({**GOOD, "ui": []}, r"ui is not a JSON object")


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
            (self.home / iso.SETTINGS_RELPATH).write_text(json.dumps({"security": {"auth": {"selectedType": "api-key"}}}))
            with self.assertRaises(iso.SettingsRefused):
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
