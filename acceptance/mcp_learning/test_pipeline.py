# PURPOSE: Exercise teacher, student publication and evaluation boundaries with isolated data.
# CONTRACT: RT-MCP-LEARNING-003 AC-L08/09; exact context and honest label provenance.
# DEPENDS: Actual candidate entrypoints, synthetic boundary observers, no MLX weights.
# CEILING: Valid synthetic answers pin transport, not accuracy or model promotion.
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from .pipeline_support import ANSWERS, fixture_row, pipeline, write_rows


class TrainingPipeline(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-learning-pipeline-")
        self.data = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def seed(self, entries):
        write_rows(self.data / "requests.jsonl", [entry[0] for entry in entries])
        write_rows(self.data / "mcp-observations.jsonl", [entry[1] for entry in entries])
        write_rows(self.data / "labels.jsonl", [entry[2] for entry in entries])

    def test_l08_teacher_receives_exact_context_fallible_annotation_and_records_provenance(self):
        request, observation, label = fixture_row()
        self.seed([(request, observation, label)])
        with pipeline("label", self.data) as (module, traces):
            self.assertEqual(module.work(("synthetic-teacher", 1, [request])), 1)
        call = traces["teacher"][0]
        self.assertEqual(call["payload"], [{"i": 0, "unit_kind": "interactive", "request": request["request"], "outcome_annotation": request["outcome_annotation"]}])
        instructions = call["argv"][call["argv"].index("--system-prompt") + 1].lower()
        self.assertIn("fallible", instructions)
        self.assertIn("completion", instructions)
        self.assertIn("correctness", instructions)
        latest = json.loads((self.data / "labels.jsonl").read_text().splitlines()[-1])
        self.assertEqual(latest["answers"], ANSWERS)
        self.assertEqual(latest["observation_digest"], request["observation_digest"])
        self.assertEqual(latest["feedback_ids"], request["outcome_annotation"]["feedback_ids"])

    def test_l08_sft_token_boundary_preserves_context_and_never_leaks_outcome(self):
        first = fixture_row(1, "Exact λ task context")
        long = fixture_row(2, "EXAM_OVER_TOKENS")
        self.seed([first, long])
        with patch.dict(os.environ, {"CORTADOS_S1_BASE": "synthetic-local-base"}), pipeline("build_sft", self.data) as (module, traces):
            module.main()
        all_rows = [json.loads(line) for path in (self.data / "sft").glob("*.jsonl") for line in path.read_text().splitlines() if line.strip()]
        self.assertGreater(len(all_rows), 0)
        serialized = json.dumps(all_rows, ensure_ascii=False)
        self.assertTrue(any(message["role"] == "user" and message["content"].endswith(first[0]["request"])
                            for row in all_rows for message in row["messages"]))
        self.assertNotIn("EXAM_OVER_TOKENS", serialized)
        self.assertNotIn("PRIVATE_OUTCOME_SENTINEL", serialized)
        self.assertEqual(traces["tokenizer_load"], {"base": "synthetic-local-base", "local_files_only": True, "trust_remote_code": False})
        self.assertEqual(len(traces["tokenizer"]), 2)
        for trace in traces["tokenizer"]:
            self.assertEqual(trace["messages"][-1]["role"], "assistant", "token measurement must include the answer")

    def test_l08_missing_tokenizer_excludes_mcp_sft_without_changing_legacy_examples(self):
        current = fixture_row()
        legacy = {"id": "legacy-plain", "source": "1.0-sheet", "unit_kind": "interactive", "request": "Legacy plain fixture", "split": "train"}
        self.seed([current])
        with (self.data / "requests.jsonl").open("a") as stream:
            stream.write(json.dumps(legacy) + "\n")
        with (self.data / "labels.jsonl").open("a") as stream:
            stream.write(json.dumps({"id": legacy["id"], "teacher": "synthetic-teacher", "sample": 0, "ts": 1, "answers": ANSWERS}) + "\n")
        with pipeline("build_sft", self.data, tokenizer_missing=True) as (module, _):
            module.main()
        serialized = "".join(path.read_text() for path in (self.data / "sft").glob("*.jsonl"))
        self.assertIn(legacy["request"], serialized)
        self.assertNotIn("Synthetic task", serialized)

    def test_l09_superseded_label_never_substitutes_for_unlabelled_latest_revision(self):
        current, observation, old_label = fixture_row(split="holdout")
        old = copy.deepcopy(current)
        old["id"] = "mcp." + old["job_id"] + "." + "f" * 64
        old["observation_digest"] = "f" * 64
        old["outcome_annotation"]["observation_digest"] = "f" * 64
        old_label["id"] = old["id"]
        write_rows(self.data / "requests.jsonl", [old, current])
        write_rows(self.data / "mcp-observations.jsonl", [observation])
        write_rows(self.data / "labels.jsonl", [old_label])
        with pipeline("evaluate", self.data) as (module, traces), patch.object(sys, "argv", ["evaluate.py", "--base", "synthetic", "--out", str(self.data / "report")]):
            module.main()
        report = json.loads((self.data / "report/report.json").read_text())
        self.assertEqual(report["holdout_rows"], 0)
        self.assertEqual(traces["scorer"], [])
        with pipeline("label", self.data) as (module, traces), patch.object(sys, "argv", ["label.py", "--k", "1", "--workers", "1"]):
            module.main()
        self.assertEqual(sum(len(call["payload"]) for call in traces["teacher"]), 1)
        new_label = json.loads((self.data / "labels.jsonl").read_text().splitlines()[-1])
        self.assertEqual(new_label["id"], current["id"])

    def test_l09_evaluation_receives_exact_request_without_future_outcome(self):
        entry = fixture_row(split="holdout")
        self.seed([entry])
        with pipeline("evaluate", self.data) as (module, traces), patch.object(sys, "argv", ["evaluate.py", "--base", "synthetic", "--out", str(self.data / "report")]):
            module.main()
        self.assertEqual(traces["scorer"], [{"request": entry[0]["request"], "unit_kind": "interactive"}])
        self.assertNotIn("PRIVATE_OUTCOME_SENTINEL", json.dumps(traces["scorer"]))

    def test_l09_same_count_incumbent_report_is_not_content_identity(self):
        entry = fixture_row(split="holdout")
        self.seed([entry])
        adapter = self.data / "old-adapter"
        adapter.mkdir()
        old_report = self.data / "old-report.json"
        old_report.write_text(json.dumps({"holdout_rows": 1, "fail_up_below": 0.6, "outward_at": 0.5, "candidate": {"band_accuracy": 1.0}}))
        (self.data / "current.json").write_text(json.dumps({"adapter": str(adapter), "report": str(old_report)}))
        with pipeline("evaluate", self.data) as (module, _):
            module.OUTWARD_AT = 0.5
            with self.assertRaises(RuntimeError, msg="same count and settings cannot prove identical context, labels or adapter content"):
                module.promoted_bar_for_laya([(entry[0], module.teacher_view([ANSWERS]))], 0.6)
