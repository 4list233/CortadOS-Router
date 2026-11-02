"""Regression (2026-11-01): ChatGPT.app moved its Codex CLI to Resources/codex-cli/bin/codex; the probe still exec'd the
old Resources/codex, the child exited 127 and every reading was "Codex /status empty ... stage=boot, eof=True"."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortados_router import scarcity as sc


def _exe(d: Path, rel: str) -> str:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return str(p)


class CodexBinaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.empty_path = str(self.tmp / "nobin")

    def test_new_app_layout_found_when_old_path_gone(self):
        new = _exe(self.tmp, "Resources/codex-cli/bin/codex")
        old = str(self.tmp / "Resources/codex")   # not created: the 2026-10 layout change
        with mock.patch.object(sc, "CODEX_APP_BINARIES", (new, old)):
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path}), (new, None))

    def test_configured_binary_wins_and_dead_one_is_reported(self):
        conf = _exe(self.tmp, "my/codex")
        new = _exe(self.tmp, "Resources/codex-cli/bin/codex")
        with mock.patch.object(sc, "CODEX_APP_BINARIES", (new,)):
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path, "CORTADOS_CODEX_BIN": conf}), (conf, None))
            dead = str(self.tmp / "gone/codex")
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path, "CORTADOS_CODEX_BIN": dead}), (new, dead))

    def test_no_binary_fails_loudly_without_forking(self):
        with mock.patch.object(sc, "CODEX_APP_BINARIES", (str(self.tmp / "none"),)), \
             mock.patch.object(sc, "_cli_env", return_value={"PATH": self.empty_path}), \
             mock.patch.object(sc, "tui_capture", side_effect=AssertionError("must not fork")):
            with self.assertRaisesRegex(RuntimeError, "Codex CLI not found"):
                sc._codex()

    def test_failure_kind_is_stable_across_attempts(self):
        a = "RuntimeError: Codex /status empty (attempts=2): stage=boot, eof=True, startup_chars=0, ready_seen=False"
        b = "RuntimeError: Codex /status empty (attempts=1): stage=boot, eof=True, startup_chars=12, ready_seen=False"
        self.assertEqual(sc.failure_kind(a), "codex-status-empty:boot")
        self.assertEqual(sc.failure_kind(a), sc.failure_kind(b))
        self.assertEqual(sc.failure_kind("RuntimeError: Codex CLI not found: no `codex` on PATH"), "codex-cli-not-found")


if __name__ == "__main__":
    unittest.main()
