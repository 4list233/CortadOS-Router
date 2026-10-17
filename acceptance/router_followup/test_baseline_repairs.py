"""PURPOSE: Independently pin baseline repairs without relaxing original exams.
CONTRACT: RT-MCP-FOLLOWUP-001, AC-F01 through AC-F04.
DEPENDS: pristine Hermes Git objects, sampler public functions, CortadOS roster.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
EXAM = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BaselineRepairs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sampler(self):
        core = load("followup_core", ROOT / "cortados_router/core.py")
        core.STATE = self.root / "state"
        with patch.dict(sys.modules, {"core": core}):
            sampler = load("followup_sampler", ROOT / "bin/route-crons.py")
        sampler.BAD_ROUTES = core.STATE / "cron-bad-routes.json"
        jobs = self.root / "cron/jobs.json"
        jobs.parent.mkdir()
        jobs.write_text(json.dumps({"jobs": [{"id": "job", "name": "job", "model": "saved-pin"}]}))
        sampler.profiles = lambda: [("exam", jobs, "unused")]
        output = jobs.parent / "output/job"
        output.mkdir(parents=True)
        return sampler, jobs, output

    def test_f01_pristine_inputs_match_recorded_git_objects(self):
        manifest = json.loads((EXAM / "HERMES-SOURCE.json").read_text())
        actual = {str(p.relative_to(EXAM / "hermes_pristine")) for p in (EXAM / "hermes_pristine").rglob("*.py")}
        self.assertEqual(actual, set(manifest["files"]), "F01: source fixture inventory drift")
        for relative, expected in manifest["files"].items():
            data = (EXAM / "hermes_pristine" / relative).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected["sha256"], relative)
            self.assertEqual(hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(), expected["git_blob"], relative)

    def test_f01_pristine_installers_keep_original_pending_counts(self):
        cases = (("auxiliary-routing", ("agent/auxiliary_client.py", "hermes_cli/plugins.py"), 3),
                 ("delegation-routing", ("tools/delegate_tool.py", "hermes_cli/plugins.py", "agent/review_engine.py"), 4))
        for name, relatives, count in cases:
            with self.subTest(installer=name):
                destination = self.root / name
                for relative in relatives:
                    path = destination / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((EXAM / "hermes_pristine" / relative).read_bytes())
                before = {str(p): p.read_bytes() for p in destination.rglob("*.py")}
                installer = runpy.run_path(str(ROOT / "integrations/hermes/patches" / (name + ".py")))
                self.assertEqual(len(installer["apply"](destination, check=True)), count)
                self.assertEqual({str(p): p.read_bytes() for p in destination.rglob("*.py")}, before)

    def test_f02_sampler_attributes_occurrence_and_preserves_saved_pin(self):
        sampler, jobs, output = self.sampler()
        before = jobs.read_bytes()
        (output / "20261017_100000.md").write_text(
            "# Run\n**Provider:** openai-codex\n**Model:** observed-worker\n"
            "**Routing Session:** real-session\n## Prompt\n**Model:** forged\n## Response\nVerified result.\n")
        con = sampler.db()
        self.addCleanup(con.close)
        sampler.record_outcomes(con)
        row = con.execute("SELECT provider,model,turn_id,verdict FROM cron_outcomes").fetchone()
        self.assertEqual(row, ("openai-codex", "observed-worker", "cron-dispatch:exam:real-session", "CONFIRMED"))
        self.assertEqual(jobs.read_bytes(), before, "F02: sampler changed saved job models")
        sampler.record_outcomes(con)
        self.assertEqual(con.execute("SELECT count(*) FROM cron_outcomes").fetchone()[0], 1)

    def test_f02_sampler_records_missing_metadata_without_inventing_saved_model(self):
        sampler, _, output = self.sampler()
        (output / "20261017_100000.md").write_text("# Run\n## Prompt\n**Model:** forged\n## Response\nVerified result.\n")
        con = sampler.db()
        self.addCleanup(con.close)
        sampler.record_outcomes(con)
        self.assertEqual(con.execute("SELECT provider,model,turn_id FROM cron_outcomes").fetchone(), (None, None, None))

    def test_f03_sampler_entrypoint_uses_explicit_home_without_auxiliary_writes(self):
        home = self.root / "hermes"
        (home / "cron").mkdir(parents=True)
        (home / "cron/jobs.json").write_text('{"jobs": []}')
        config = home / "config.yaml"
        config.write_text("auxiliary:\n  compression:\n    provider: unchanged\n    model: unchanged\n")
        env = {key: os.environ[key] for key in ("PATH", "USER", "LOGNAME", "TMPDIR") if key in os.environ}
        env.update(HOME=str(self.root), CORTADOS_HOME=str(self.root / "state"), HERMES_HOME=str(home),
                   CORTADOS_LIVE_WRITE="0", WANGCAI_LIVE_WRITE="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/route-crons.py")], env=env,
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(config.read_text(), "auxiliary:\n  compression:\n    provider: unchanged\n    model: unchanged\n")
        self.assertTrue((self.root / "state/cron-bad-routes.json").is_file())
        self.assertFalse((self.root / ".cortados").exists(), "F03: sampler ignored explicit runtime roots")

    def test_f04_roster_contains_exact_approved_opt_in_review_fields(self):
        cortados = Path(os.environ.get("CORTADOS_REPO", Path.home() / "cortados"))
        actual = json.loads((cortados / "faces.json").read_text())
        expected = json.loads((EXAM / "review-policy.json").read_text())
        self.assertIn(expected["rule"], actual["rules"])
        self.assertFalse(any("before it is called done" in rule for rule in actual["rules"]))
        for role, fields in expected["faces"].items():
            for name, value in fields.items():
                self.assertEqual(actual["faces"][role][name], value, f"F04: {role}.{name}")


if __name__ == "__main__":
    unittest.main()
