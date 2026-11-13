"""Shared pieces of the CortadOS Router S1 pipeline: the question set (imported from the live router so there is ONE definition), text cleaning, splits."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]            # this repository (cortados_router/, s1/, tables/)
DATA = REPO / "s1/data"                                 # requests, teacher labels, SFT files: local only, never published
_spec = importlib.util.spec_from_file_location("cortados_router_s1_core", str(REPO / "cortados_router/core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

QUESTIONS = core.FEATURES + ["outward", "work_kind", "urgency", "needs_clarification", "parts", "diversity"]
clean_request = core.clean_request   # one definition, shared with the live hook


def bands_only() -> None:
    """For the s1 tools that use core.resolve only to turn twelve answers into a band (build_sft, evaluate, drift, and
    calibrate through evaluate): never open a vendor usage probe. Stubbing lane_health alone was not enough: resolve's
    capacity helpers (_pool_tokens_per_pct, _tokens_left_for) call scarcity.current() with refresh on, and a due reading
    starts the background sample that opens the claude/codex/cursor-agent/agy CLIs. On the Studio on 2026-11-12 the
    nightly's SFT build did exactly that ("cortados scarcity: cursor reading failed ..." between the label and SFT log
    lines). CORTADOS_SCARCITY_REFRESH=off makes every reading cache-only (scarcity.current); the band never depends on
    usage. Process-wide, so call it only in band-only tools: label.py's cli backend and the outcome loop need live usage."""
    os.environ["CORTADOS_SCARCITY_REFRESH"] = "off"
    core.lane_health = lambda **_: {}


def split_of(row_id: str) -> str:
    """Fixed by hash, forever: a row never moves between train and holdout, whatever gets added later."""
    return "holdout" if int(hashlib.sha1(row_id.encode()).hexdigest(), 16) % 100 < 15 else "train"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


# The tag at the head of every s1/label.py teacher message sent through the provider, so s1/dataset.py can tell the labeler's own router
# rows from real requests (the provider logs the message text but keeps no run-id column). One definition for both sides.
LABELER_MARKER = "[cortados-s1-label]"
# The same mark for the S1 outcome loop's provider traffic (judge, generate and every ladder band run as a Claude Code
# session): its user messages are synthesized tasks and the loop's own prompts, never a person's request, so the router
# rows they leave must not be mined back into S1 training data (s1/dataset.py MACHINE_MARKERS).
OUTCOME_MARKER = "[cortados-s1-outcome]"


def is_claude_teacher(teacher) -> bool:
    """Only Claude teachers' labels train or grade S1. The gpt-6.1-sol labels (Oct 17-24) rated tasks much easier than the Claude teachers and
    caused under-routing; Forest excluded Codex labels 2026-11-02. A label row names its teacher in the `teacher` field (the model id)."""
    return isinstance(teacher, str) and teacher.startswith("claude-")


def read_labels(claude_only: bool = True) -> list[dict]:
    """Full teacher labels with any per-question override passes (labels-override-*.jsonl) merged over them, matched by (id, teacher, sample).
    Non-Claude teacher rows stay in labels.jsonl (the record is kept) but are dropped here, with a counted line on stderr."""
    rows = read_jsonl(DATA / "labels.jsonl")
    if claude_only:
        dropped = Counter(str(r.get("teacher")) for r in rows if not is_claude_teacher(r.get("teacher")))
        rows = [r for r in rows if is_claude_teacher(r.get("teacher"))]
        print(f"labels: kept {len(rows)} Claude-teacher rows; excluded {sum(dropped.values())} non-Claude rows "
              f"{json.dumps(dict(sorted(dropped.items())))}", file=sys.stderr)
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
