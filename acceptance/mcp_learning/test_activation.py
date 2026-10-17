# PURPOSE: Independently pin disabled and enabled staged nightly adapter behavior.
# CONTRACT: RT-MCP-LEARNING-003 AC-L12; this stage is BLOCKED until its candidate exists.
# DEPENDS: Frozen live-before source; root-provided staged clone and installed release.
# CEILING: Never executes nightly.sh or writes live training/model/scheduler data.
from contextlib import redirect_stdout
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from .pipeline_support import fixture_row, pipeline, write_rows

BEFORE = Path("/Users/velox/.cortados-builds/mcp-learning-verification/live-before/train")


class Activation(unittest.TestCase):
    def setUp(self):
        configured = os.environ.get("CORTADOS_MCP_ACTIVATION_ROOT")
        self.assertTrue(configured, "BLOCKED: supply CORTADOS_MCP_ACTIVATION_ROOT for staged adapter acceptance")
        self.stage = Path(configured) / "train"
        self.assertTrue(self.stage.is_dir(), "BLOCKED: staged activation train directory missing")
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-learning-activation-exam-")
        self.data = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def seed(self, entries):
        write_rows(self.data / "requests.jsonl", [entry[0] for entry in entries])
        write_rows(self.data / "mcp-observations.jsonl", [entry[1] for entry in entries])
        write_rows(self.data / "labels.jsonl", [entry[2] for entry in entries])

    def site_packages(self):
        values = {node.value for name in ("dataset", "label", "build_sft", "evaluate")
                  for node in ast.walk(ast.parse((self.stage / (name + ".py")).read_text()))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and node.value.startswith("/") and node.value.endswith("site-packages")}
        self.assertEqual(len(values), 1, "BLOCKED: adapters must pin exactly one installed release site-packages path")
        value = values.pop()
        self.assertTrue((Path(value) / "cortados_router/learning_data.py").is_file(), "BLOCKED: pinned non-editable release not installed")
        return value

    def test_l12_only_five_adapters_change_and_next_run_exports_precede_reexec(self):
        for name in ("common.py", "student.py", "current.json"):
            self.assertEqual((self.stage / name).read_bytes(), (BEFORE / name).read_bytes(), name)
        for name in ("dataset.py", "label.py", "build_sft.py", "evaluate.py"):
            source = (self.stage / name).read_text()
            self.assertIn("CORTADOS_MCP_LEARNING_V1", source, "BLOCKED: activation builder has not staged guarded adapter " + name)
            self.assertIn("CORTADOS_MCP_LEARNING_SITE_PACKAGES", source)
        source = (self.stage / "nightly.sh").read_text()
        self.assertLess(source.index("CORTADOS_MCP_LEARNING_V1"), source.index("overnight.py"))
        self.assertIn("CORTADOS_MCP_LEARNING_SITE_PACKAGES", source)
        self.assertIn("CORTADOS_S1_BASE", source)

    def test_l12_disabled_modified_eval_preserves_legacy_inputs_and_outputs(self):
        self.assertIn("CORTADOS_MCP_LEARNING_V1", (self.stage / "evaluate.py").read_text(), "BLOCKED: staged eval adapter missing")
        self.seed([fixture_row(split="holdout")])
        observed = []
        environment = dict(os.environ)
        environment.pop("CORTADOS_MCP_LEARNING_V1", None)
        environment.pop("CORTADOS_MCP_LEARNING_SITE_PACKAGES", None)
        with patch.dict(os.environ, environment, clear=True):
            for source in (BEFORE / "evaluate.py", self.stage / "evaluate.py"):
                with pipeline("evaluate", self.data, source=source) as (module, trace), patch.object(sys, "argv", ["evaluate.py", "--base", "synthetic", "--out", str(self.data / "report")]):
                    module.main()
                report = json.loads((self.data / "report/report.json").read_text())
                report.pop("when", None)
                observed.append((report, trace["scorer"]))
        self.assertEqual(observed[0], observed[1], "old inherited environment must keep exact legacy evaluation")

    def test_l12_enabled_collection_teacher_sft_and_eval_use_installed_learning_owner(self):
        site = self.site_packages()
        entries = [fixture_row(), fixture_row(2, "Held-out exact context", split="holdout")]
        self.seed(entries)
        environment = {"CORTADOS_MCP_LEARNING_V1": "1", "CORTADOS_MCP_LEARNING_SITE_PACKAGES": site, "CORTADOS_S1_BASE": "synthetic-local-base"}
        with patch.dict(os.environ, environment):
            with pipeline("dataset", self.data, source=self.stage / "dataset.py") as (module, _), patch.object(sys, "argv", ["dataset.py", "--mcp-state-dir", str(self.data / "absent-source")]):
                module.SHEET = self.data / "no-sheet.csv"
                module.ROUTER_DB = self.data / "no-hermes.db"
                output = io.StringIO()
                with redirect_stdout(output):
                    module.main()
                self.assertIn("mcp_source_unavailable", output.getvalue())
                self.assertFalse((self.data / "absent-source").exists())
            with pipeline("label", self.data, source=self.stage / "label.py") as (module, trace):
                module.work(("synthetic-teacher", 1, [entries[0][0]]))
                self.assertEqual(trace["teacher"][0]["payload"][0]["outcome_annotation"], entries[0][0]["outcome_annotation"])
            with pipeline("build_sft", self.data, source=self.stage / "build_sft.py") as (module, trace):
                module.main()
                self.assertTrue(trace["tokenizer"], "enabled SFT must measure full training tokens")
            with pipeline("evaluate", self.data, source=self.stage / "evaluate.py") as (module, trace), patch.object(sys, "argv", ["evaluate.py", "--base", "synthetic", "--out", str(self.data / "report")]):
                module.main()
                self.assertEqual(trace["scorer"], [{"request": entries[1][0]["request"], "unit_kind": "interactive"}])
        self.assertFalse((self.data / "absent-source").exists())
