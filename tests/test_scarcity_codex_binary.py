"""Regression (2026-11-01): ChatGPT.app moved its Codex CLI to Resources/codex-cli/bin/codex; the probe still exec'd the
old Resources/codex, the child exited 127 and every reading was "Codex /status empty ... stage=boot, eof=True"."""
import io
import json
import os
import shutil
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


def _bundle(root: Path, rel: str, manifest: object = None, manifest_at: str = "Contents/Resources/codex-cli") -> str:
    """A fake .app bundle: an executable at `rel` under the bundle, plus an optional codex-package.json naming it."""
    binary = _exe(root, rel)
    if manifest is not None:
        m = root / manifest_at / "codex-package.json"
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(manifest if isinstance(manifest, str) else json.dumps(manifest))
    return binary


class CodexBinaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.empty_path = str(self.tmp / "nobin")
        # No host bundles in any search: these tests must read only the fake trees they build.
        p = mock.patch.object(sc, "CODEX_APP_BUNDLES", ())
        p.start(); self.addCleanup(p.stop)

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
            with self.assertRaisesRegex(RuntimeError, "Codex CLI not found: searched .*PATH"):
                sc._codex()

    # --- discovery that survives the next relocation -------------------------------------------------------------
    def test_manifest_entrypoint_is_followed_wherever_the_cli_moves(self):
        # The point of the manifest: a layout nobody hardcoded (Resources/codex-cli/vendor/2027/codex) is still found.
        app = self.tmp / "ChatGPT.app"
        found = _bundle(app, "Contents/Resources/codex-cli/vendor/2027/codex", {"entrypoint": "vendor/2027/codex"})
        searched: list = []
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path}, bundles=(str(app),), searched=searched),
                             (found, None))

    def test_manifest_at_the_resources_root_is_read_too(self):
        app = self.tmp / "ChatGPT.app"
        found = _bundle(app, "Contents/Resources/bin/codex", {"entrypoint": "bin/codex"},
                        manifest_at="Contents/Resources")
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path}, bundles=(str(app),))[0], found)

    def test_glob_finds_the_cli_when_the_bundle_ships_no_manifest(self):
        app = self.tmp / "ChatGPT.app"
        found = _exe(app, "Contents/Resources/codex-cli/bin/codex")
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            self.assertEqual(sc._codex_binary({"PATH": self.empty_path}, bundles=(str(app),))[0], found)

    def test_a_broken_manifest_is_skipped_with_a_reason_and_the_glob_still_wins(self):
        app = self.tmp / "ChatGPT.app"
        found = _bundle(app, "Contents/Resources/codex-cli/bin/codex", "{not json")
        searched: list = []
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            got = sc._codex_binary({"PATH": self.empty_path}, bundles=(str(app),), searched=searched)
        self.assertEqual(got[0], found)
        self.assertTrue(any("unreadable: JSONDecodeError" in s for s in searched), searched)

    def test_a_manifest_without_an_entrypoint_is_recorded_not_guessed(self):
        app = self.tmp / "ChatGPT.app"
        _bundle(app, "Contents/Resources/codex-cli/other/codex", {"version": "1.2.3"})
        searched: list = []
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            got = sc._codex_binary({"PATH": self.empty_path}, bundles=(str(app),), searched=searched)
        self.assertEqual(got, (None, None))
        self.assertTrue(any('no "entrypoint"' in s for s in searched), searched)

    def test_missing_bundles_are_skipped_and_the_error_names_everything_searched(self):
        searched: list = []
        legacy = str(self.tmp / "legacy/codex")
        with mock.patch.object(sc, "CODEX_APP_BINARIES", (legacy,)):
            got = sc._codex_binary({"PATH": self.empty_path}, bundles=(str(self.tmp / "Gone.app"),), searched=searched)
        self.assertEqual(got, (None, None))
        self.assertIn("`codex` on PATH", searched)
        self.assertIn(legacy, searched)

    def test_path_beats_the_bundle(self):
        # An explicitly installed CLI is the one the user maintains; both read the same ~/.codex.
        app, bindir = self.tmp / "ChatGPT.app", self.tmp / "bin"
        _bundle(app, "Contents/Resources/codex-cli/bin/codex", {"entrypoint": "bin/codex"})
        on_path = _exe(bindir, "codex")
        self.assertEqual(sc._codex_binary({"PATH": str(bindir)}, bundles=(str(app),))[0], on_path)

    def test_a_directory_named_codex_never_qualifies(self):
        # os.access(X_OK) is true for any searchable directory; forking one is an instant 127.
        (self.tmp / "bin/codex").mkdir(parents=True)
        (self.tmp / "conf/codex").mkdir(parents=True)
        app = self.tmp / "ChatGPT.app"
        (app / "Contents/Resources/codex-cli/bin/codex").mkdir(parents=True)
        (app / "Contents/Resources/codex-cli/codex-package.json").write_text(json.dumps({"entrypoint": "bin/codex"}))
        env = {"PATH": str(self.tmp / "bin"), "CORTADOS_CODEX_BIN": str(self.tmp / "conf/codex")}
        with mock.patch.object(sc, "CODEX_APP_BINARIES", ()):
            self.assertEqual(sc._codex_binary(env, bundles=(str(app),)), (None, str(self.tmp / "conf/codex")))

    # --- failure labelling ---------------------------------------------------------------------------------------
    def test_failure_kind_is_stable_across_attempts(self):
        a = "RuntimeError: Codex /status empty (attempts=2): stage=boot, eof=True, startup_chars=0, ready_seen=False"
        b = "RuntimeError: Codex /status empty (attempts=1): stage=boot, eof=True, startup_chars=12, ready_seen=False"
        self.assertEqual(sc.failure_kind(a), "codex-status-empty:boot")
        self.assertEqual(sc.failure_kind(a), sc.failure_kind(b))
        self.assertEqual(sc.failure_kind("RuntimeError: Codex CLI not found: no `codex` on PATH"), "codex-cli-not-found")

    def test_failure_kind_falls_back_to_the_exception_type_when_there_is_no_message(self):
        self.assertEqual(sc.failure_kind("TimeoutError: "), "timeouterror")
        self.assertEqual(sc.failure_kind("TimeoutError"), "timeouterror")

    def test_failure_kind_ignores_versions_and_counts_so_suppression_holds(self):
        v1 = "ValueError: Codex CLI v0.170.0 is not one this parser was verified against"
        v2 = "ValueError: Codex CLI v0.171.3 is not one this parser was verified against"
        self.assertEqual(sc.failure_kind(v1), sc.failure_kind(v2))
        self.assertEqual(sc.failure_kind("RuntimeError: 2 of 3 nodes unreachable"),
                         sc.failure_kind("RuntimeError: 1 of 3 nodes unreachable"))


class FailedReadingTest(unittest.TestCase):
    """A failed try must persist error_kind and be logged once per kind, on every sampler path."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        # A throwaway HOME too: anything that still resolves ~/.cortados on its own lands here, where tearDown checks it.
        self.user_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.user_home, True)
        for name, value in (("STATE", self.home), ("CACHE", self.home / "scarcity.json"),
                            ("HISTORY", self.home / "scarcity-history.jsonl"),
                            ("PROBE_DIR", self.home / "usage-probe")):
            p = mock.patch.object(sc, name, value)
            p.start(); self.addCleanup(p.stop)
        # _sample_locked re-learns unit costs when learned_at is absent, and unitcost resolves its own home: replace it
        # outright, and point CORTADOS_HOME/HOME at temp dirs for the whole test in case anything else resolves one.
        self.learner = mock.Mock(return_value=None)
        for p in (mock.patch.object(sc, "_plans", return_value={}),
                  mock.patch.object(sc._reaper, "sweep_stale", return_value=None),
                  mock.patch.object(sc, "_learn_unit_costs", self.learner),
                  mock.patch.dict(os.environ, {"CORTADOS_SCARCITY_REFRESH": "off", "CORTADOS_HOME": str(self.home),
                                               "HOME": str(self.user_home)})):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(self._assert_nothing_outside_the_temp_home)

    def _assert_nothing_outside_the_temp_home(self):
        self.assertFalse((self.user_home / ".cortados").exists(), "a test reached a ~/.cortados outside its temp home")
        self.assertEqual(list(self.user_home.iterdir()), [], "a test wrote into HOME")

    def test_the_unit_cost_learner_is_isolated(self):
        self._run(lambda: (_ for _ in ()).throw(RuntimeError("Codex CLI not found: searched nothing")))
        self.learner.assert_called_once_with()          # reached (learned_at absent) but replaced: no live history read

    def _codex_lane(self) -> dict:
        return (json.loads((self.home / "scarcity.json").read_text()).get("lanes") or {}).get("codex") or {}

    def _run(self, sampler) -> str:
        err = io.StringIO()
        with mock.patch.dict(sc.SAMPLERS, {"codex": sampler}), mock.patch.object(sc.sys, "stderr", err):
            sc._sample_locked(force=True, only=["codex"])
        return err.getvalue()

    def test_same_failure_logs_once_and_a_new_kind_logs_again(self):
        def boom():
            raise RuntimeError("Codex CLI not found: searched nothing")

        def other():
            raise ValueError("Codex /status screen is not one this parser accepts")

        first, second = self._run(boom), self._run(boom)
        self.assertEqual(first.count("reading failed"), 1, first)
        self.assertEqual(second.count("reading failed"), 0, second)   # same kind: logged once, not every attempt
        self.assertEqual(self._codex_lane()["error_kind"], "codex-cli-not-found")
        third = self._run(other)
        self.assertEqual(third.count("reading failed"), 1, third)
        self.assertEqual(self._codex_lane()["error_kind"], "codex-status-screen-is-not-one-this-parser-accepts")

    def test_a_succeeding_reading_clears_the_kind(self):
        def boom():
            raise RuntimeError("Codex CLI not found: searched nothing")

        def good():
            return {"windows": [{"window": "week", "pool": "all", "used_pct": 10.0, "elapsed_pct": 50.0,
                                 "scarcity": -0.4, "expiring_pct": 40, "resets_at": "soon"}]}
        self._run(boom)
        self._run(good)
        lane = self._codex_lane()
        self.assertIsNone(lane["error_kind"])
        self.assertIsNone(lane["error"])

    def test_the_managed_local_refresh_records_a_kind_and_logs_like_the_locked_sampler(self):
        # Regression: _sample_local_managed wrote `error` with no error_kind and never logged, so a node-telemetry
        # failure was silent and re-logged nothing while routing quietly lost the lane.
        err = io.StringIO()
        with mock.patch.object(sc, "_local", side_effect=OSError("telemetry socket is gone")), \
             mock.patch.object(sc.sys, "stderr", err):
            sc._sample_local_managed()
            rec = (json.loads((self.home / "scarcity.json").read_text()).get("lanes") or {}).get("local") or {}
            self.assertEqual(rec["error_kind"], "telemetry-socket-is-gone")
            self.assertEqual(err.getvalue().count("local reading failed"), 1, err.getvalue())
            sc._sample_local_managed()      # same kind again: still only the one line
            self.assertEqual(err.getvalue().count("local reading failed"), 1, err.getvalue())


if __name__ == "__main__":
    unittest.main()
