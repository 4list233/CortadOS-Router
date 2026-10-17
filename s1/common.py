"""Shared pieces of the CortadOS Router S1 pipeline: the question set (imported from the live router so there is ONE definition), text cleaning, splits."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]            # this repository (cortados_router/, s1/, tables/)
DATA = REPO / "s1/data"                                 # requests, teacher labels, SFT files: local only, never published
_spec = importlib.util.spec_from_file_location("cortados_router_s1_core", str(REPO / "cortados_router/core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

QUESTIONS = core.FEATURES + ["outward", "work_kind", "urgency", "needs_clarification", "parts", "diversity"]
clean_request = core.clean_request   # one definition, shared with the live hook


from cortados_router.learning_data import split_of


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def read_labels() -> list[dict]:
    """Full teacher labels with any per-question override passes (labels-override-*.jsonl) merged over them, matched by (id, teacher, sample)."""
    rows = read_jsonl(DATA / "labels.jsonl")
    for f in sorted(DATA.glob("labels-override-*.jsonl")):
        ov = {(l["id"], l["teacher"], l["sample"]): l["answers"] for l in read_jsonl(f)}
        for r in rows:
            k = (r["id"], r["teacher"], r["sample"])
            if k in ov:
                r["answers"] = {**r["answers"], **ov[k]}
    return rows


def valid_answers(a: dict) -> bool:
    try:
        return (all(isinstance(a[f], int) and 0 <= a[f] <= 3 for f in core.FEATURES) and isinstance(a["outward"], bool) and a["work_kind"] in core.WORK_KINDS
                and a["urgency"] in core.URGENCY and isinstance(a["needs_clarification"], bool) and isinstance(a["parts"], int) and 0 <= a["parts"] <= 4
                and isinstance(a["diversity"], bool))
    except Exception:
        return False
