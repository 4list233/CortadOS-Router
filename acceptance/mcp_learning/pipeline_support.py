# PURPOSE: Run actual training entrypoints with isolated data and boundary doubles.
# CONTRACT: RT-MCP-LEARNING-003 AC-L08/09; no teacher, weights, secrets or live data.
# DEPENDS: Unchanged student prompt formatter; synthetic teacher and tokenizer observers.
# CEILING: Tokenizer double pins use of measured count, not real tokenizer accuracy.
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
from unittest.mock import patch

from .support import SECTION, canonical, digest

ROOT = Path(__file__).resolve().parents[2]
ANSWERS = {key: 0 for key in ("steps", "ambiguity", "blast_radius", "context_size", "novelty", "parallelizability")}
ANSWERS.update(outward=False, work_kind="code", urgency="now", needs_clarification=False, parts=0, diversity=False)


def write_rows(path, values):
    path.write_text("".join(json.dumps(row) + "\n" for row in values))


def fixture_row(index=1, text="Synthetic task with exact λ context", *, split="train"):
    # Independent application of the contract's stable partition formula.
    for suffix in range(1000):
        group_id = f"00000000-0000-4000-8000-{index * 1000 + suffix:012d}"
        group = f"mcp-group:fixture:{group_id}"
        actual = "holdout" if int(hashlib.sha1(group.encode()).hexdigest(), 16) % 100 < 15 else "train"
        valid = int(hashlib.sha1(("v" + group).encode()).hexdigest(), 16) % 10 == 0
        if actual == split and not valid:
            break
    job_id = f"00000000-0000-4000-8000-{index:012d}"
    supplied = {"schema": "cortados.mcp-task-evidence.v1", "task_group_id": group_id,
                "components": [], "dependencies": [], "constraints": [], "uncertainties": [], "verification": ["Inspect fixture"]}
    learning_text = text + "\n\nCONTEXT:\n" + SECTION + canonical(supplied)
    check = {"kind": "review", "reference": "synthetic evidence", "claim": "passed"}
    feedback_id = f"00000000-0000-4000-8000-{100000 + index:012d}"
    reviewed = {"job_id": job_id, "feedback_id": feedback_id, "verdict": "accepted", "summary": "PRIVATE_OUTCOME_SENTINEL",
                "checks": [check], "seq": index, "created_at_ms": 1000}
    worker = {"provider": "codex-cli", "requested_model": "gpt-6-luna", "reported_model": "gpt-6-luna", "cli_version": "0.163.0"}
    observation = {"schema": "cortados.mcp-learning.v1", "job_id": job_id,
                   "route_id": f"00000000-0000-4000-8000-{200000 + index:012d}", "request_id": f"00000000-0000-4000-8000-{300000 + index:012d}",
                   "client_id": "fixture", "state": "succeeded", "reason": None, "task": text, "context": "", "task_evidence": supplied,
                   "selection_mode": "explicit", "scoring_text": None, "scoring_digest": None, "learning_text": learning_text,
                   "task_group_id": group_id, "worker": worker, "usage": None, "elapsed_ms": 10, "feedback": [reviewed],
                   "native_outcome": "completed", "planner_verdict": "accepted", "eligibility": True, "exclusion_reason": None}
    material = {key: observation[key] for key in ("job_id", "learning_text", "native_outcome", "reason", "worker", "usage", "elapsed_ms", "feedback")}
    observation_digest = digest(canonical(material))
    identifier = "mcp." + job_id + "." + observation_digest
    observation.update(learning_id=identifier, observation_digest=observation_digest)
    annotation = {"native_outcome": "completed", "worker": worker, "usage": None, "elapsed_ms": 10, "reason": None,
                  "planner_verdict": "accepted", "verification_status": "planner_asserted", "summary": "PRIVATE_OUTCOME_SENTINEL",
                  "checks": [check], "feedback_ids": [feedback_id], "observation_digest": observation_digest}
    request = {"id": identifier, "source": "mcp-worker", "unit_kind": "interactive", "request": learning_text, "split": split,
               "group_key": group, "task_fingerprint": digest(text), "scoring_digest": None,
               "job_id": job_id, "observation_digest": observation_digest, "outcome_annotation": annotation}
    label = {"id": identifier, "teacher": "synthetic-teacher", "sample": 0, "ts": 1, "answers": dict(ANSWERS),
             "observation_digest": observation_digest, "feedback_ids": annotation["feedback_ids"]}
    return request, observation, label


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def pipeline(name, data, *, source=None, tokenizer_missing=False):
    traces = {"teacher": [], "tokenizer": [], "scorer": [], "usage": []}
    previous = dict(sys.modules)
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, str(ROOT / "s1"))
        enabled_activation = source is not None and os.environ.get("CORTADOS_MCP_LEARNING_V1") == "1"
        if enabled_activation:
            for key in list(sys.modules):
                if key == "cortados_router" or key.startswith("cortados_router."):
                    del sys.modules[key]
            sys.path.insert(0, os.environ["CORTADOS_MCP_LEARNING_SITE_PACKAGES"])
        common = load("common", ROOT / "s1/common.py")
        common.DATA = data
        student = load("student", ROOT / "s1/student.py")
        usage = types.ModuleType("usage")
        usage.record = traces["usage"].append
        sys.modules["usage"] = usage
        transformers = types.ModuleType("transformers")
        class Tokenizer:
            def apply_chat_template(self, messages, **options):
                traces["tokenizer"].append({"messages": copy.deepcopy(messages), "options": options})
                return list(range(1537 if "EXAM_OVER_TOKENS" in json.dumps(messages) else 1536))
        class AutoTokenizer:
            @classmethod
            def from_pretrained(cls, base, **options):
                traces["tokenizer_load"] = {"base": base, **options}
                if tokenizer_missing:
                    raise FileNotFoundError("synthetic unavailable tokenizer")
                return Tokenizer()
        transformers.AutoTokenizer = AutoTokenizer
        sys.modules["transformers"] = transformers
        class FakeScorer:
            def __init__(self, *args, **kwargs):
                self.T = {}
            def score(self, request, unit_kind):
                traces["scorer"].append({"request": request, "unit_kind": unit_kind})
                return {"answers": dict(ANSWERS), "probs": {question: [1 / count] * count for question, count in student.N_OPTIONS.items()}, "ms": 1}
        student.Scorer = FakeScorer
        module = load("exam_" + name, source or ROOT / "s1" / (name + ".py"))
        if enabled_activation:
            import cortados_router.learning_data as installed_owner
            traces["learning_owner"] = str(Path(installed_owner.__file__).resolve())
            expected = Path(os.environ["CORTADOS_MCP_LEARNING_SITE_PACKAGES"]) / "cortados_router/learning_data.py"
            assert Path(installed_owner.__file__).resolve() == expected.resolve(), "activation used checkout owner instead of installed release"
        module.DATA = data
        if name == "label":
            module.STATE = data / "private-state"
            module.LABELS = data / "labels.jsonl"
            module._env = lambda: {}
            module._scarce = lambda model: 0
            def run(argv, **kwargs):
                payload = json.loads(kwargs["input"])
                traces["teacher"].append({"argv": argv, "payload": payload})
                result = [{"i": row["i"], **ANSWERS} for row in payload]
                return types.SimpleNamespace(stdout=json.dumps({"result": json.dumps(result), "usage": {}, "session_id": "synthetic-teacher-session"}))
            with patch.object(module.subprocess, "run", run):
                yield module, traces
        else:
            # Existing evaluation reads current.json relative to its source. Use an absent private
            # incumbent so this exam never loads the live model or changes promotion metadata.
            if name == "evaluate":
                module.__file__ = str(data / "evaluate.py")
            yield module, traces
    finally:
        sys.path[:] = previous_path
        for key in set(sys.modules) - set(previous):
            del sys.modules[key]
        sys.modules.update(previous)
