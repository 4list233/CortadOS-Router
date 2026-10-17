# PURPOSE: Verify captured native tokenizer shapes and worked contract identities.
# CONTRACT: RT-MCP-LEARNING-003 AC-L11; source evidence independent of the builder.
# DEPENDS: Frozen privacy-preserving captures, synthetic real-tokenizer inputs, stdlib.
# CEILING: Captured synthetic labels do not establish routing accuracy.
import hashlib
import json
from pathlib import Path
import unittest

from .support import SECTION, canonical, digest, legacy_handoff

HERE = Path(__file__).parent
ROOT = HERE.parents[1]


class GroundTruth(unittest.TestCase):
    def test_l11_native_training_tokenizer_boundary_includes_complete_answer(self):
        for count in (1536, 1537):
            value = json.loads((HERE / "fixtures/tokenizer" / f"training-{count}.json").read_text())
            self.assertEqual(len(value["input_ids"]), count)
            self.assertEqual(value["full_training_token_count"], count)
            self.assertEqual(digest(canonical(value["input_ids"])), value["input_ids_sha256"])
            self.assertEqual(value["supervised_token_count"], count - value["mask_prompt_offset"])
            self.assertEqual(value["supervised_token_count"], 69)
            self.assertEqual(value["messages"][-1]["role"], "assistant")
            self.assertIn("diversity=0", value["messages"][-1]["content"])
            self.assertEqual(value["fits_1536_without_truncation"], count == 1536)
        provenance = json.loads((HERE / "fixtures/tokenizer/provenance.json").read_text())
        self.assertFalse(provenance["gpu_model_loaded"])
        self.assertFalse(provenance["mlx_core_imported"])

    def test_l11_worked_contract_lengths_hashes_and_partitions_are_independent(self):
        examples = {row["name"]: row for row in json.loads((ROOT / "docs/mcp-worker/learning-context-examples.json").read_text())["examples"]}
        request = examples["context_requirements"]["input"]
        handoff = legacy_handoff(request["task"], request["context"]) + SECTION + canonical(request["task_evidence"])
        self.assertEqual(len(handoff.encode()), examples["context_requirements"]["valid"]["context_requirements"]["worker_bytes"])
        observation = examples["job_observation"]["valid"]
        material = {key: observation[key] for key in ("job_id", "learning_text", "native_outcome", "reason", "worker", "usage", "elapsed_ms", "feedback")}
        self.assertEqual(digest(canonical(material)), observation["observation_digest"])
        revision = examples["request_revision"]["valid"]
        self.assertEqual(revision["id"], observation["learning_id"])
        self.assertEqual(revision["outcome_annotation"], examples["outcome_annotation"]["valid"])
        self.assertEqual(revision["task_fingerprint"], digest(observation["task"]))
        group = revision["group_key"]
        expected = "holdout" if int(hashlib.sha1(group.encode()).hexdigest(), 16) % 100 < 15 else "valid" if int(hashlib.sha1(("v" + group).encode()).hexdigest(), 16) % 10 == 0 else "train"
        self.assertEqual(examples["eligible_rows"]["valid"]["partitions"][revision["id"]], expected)
