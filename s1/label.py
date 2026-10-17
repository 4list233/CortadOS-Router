"""Teacher labelling for CortadOS Router S1. Every request is answered on all twelve routing questions k times, independently, by a strong model
through the STATELESS Claude brain (subscription, no tools, no session). The k samples are the soft label; their disagreement is the uncertainty.

  train split   → claude-sonnet-5 × 3        holdout split → claude-opus-5 × 3   (a different, stronger teacher grades the exam)

Resume-safe: finished (id, teacher, sample) triples are skipped. Scarcity-gated: stops when the Claude pool it draws on gets scarce.
  python3 label.py [--limit N] [--k 3] [--batch 12] [--workers 3] [--split train|holdout|all]"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Every label mode enters the same immutable installed interpreter before project imports.
_TEACHER_RELEASE = "/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017"
_TEACHER_PYTHON = _TEACHER_RELEASE + "/venv/bin/python"
if (os.path.normpath(sys.executable) != _TEACHER_PYTHON
        or os.path.normpath(sys.prefix) != _TEACHER_RELEASE + "/venv"):
    if os.environ.get("CORTADOS_TEACHER_REEXEC_V1") == "1":
        raise RuntimeError("teacher_release_mismatch")
    if not os.path.isfile(_TEACHER_PYTHON):
        raise RuntimeError("teacher_release_mismatch: interpreter unavailable")
    _teacher_environment = dict(os.environ, CORTADOS_TEACHER_REEXEC_V1="1")
    os.execve(_TEACHER_PYTHON, [_TEACHER_PYTHON, "-B", str(Path(__file__).resolve()), *sys.argv[1:]], _teacher_environment)
    raise RuntimeError("teacher_release_mismatch: exec returned")

_TEACHER_SITE = _TEACHER_RELEASE + "/venv/lib/python3.14/site-packages"
sys.path.insert(0, _TEACHER_SITE)
from cortados_router import teacher as _TEACHER, teacher_attempts as _ATTEMPTS
from cortados_router import learning_data as _PINNED_LEARNING
for _owner in (_TEACHER, _ATTEMPTS, _PINNED_LEARNING):
    if Path(_owner.__file__).resolve() != Path(_TEACHER_SITE) / "cortados_router" / ( _owner.__name__.rsplit(".", 1)[-1] + ".py"):
        raise RuntimeError("teacher_release_mismatch: owner origin")

from common import DATA, QUESTIONS, REPO, core, read_jsonl, valid_answers

_MCP_LEARNING = None
if os.environ.get("CORTADOS_MCP_LEARNING_V1") == "1":
    _INHERITED_SITE = "/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages"
    if os.environ.get("CORTADOS_MCP_LEARNING_SITE_PACKAGES") != _INHERITED_SITE:
        raise RuntimeError("mcp_learning_release_unavailable: inherited binding differs")
    _MCP_LEARNING = _PINNED_LEARNING
if os.environ.get("CORTADOS_TRAINING_TEACHER_V1") == "1" and _MCP_LEARNING is None:
    raise RuntimeError("mcp_learning_release_unavailable: teacher requires learning")


STATE = Path.home() / ".cortados"
LABELS = DATA / "labels.jsonl"
DEFAULTS = {"steps": 0, "ambiguity": 0, "blast_radius": 0, "context_size": 0, "novelty": 0, "parallelizability": 0, "outward": False, "work_kind": "conversation",
            "urgency": "now", "needs_clarification": False, "parts": 0, "diversity": False}
TEACHER = {"train": "claude-sonnet-5", "holdout": "claude-opus-5"}
SCARCITY_STOP = 0.7
_write = threading.Lock()
_stop = threading.Event()

SYSTEM = (core.SYSTEM + "\n\nYou are the TEACHER producing training labels. You receive a JSON array of requests Forest (a founder running a fleet of AI "
          "agents for his business and personal life) sent to his agents; `unit_kind` says whether a person was waiting (interactive) or a scheduled job "
          "fired it (background). Rate each request on its own. Be decisive and use the whole scale: most chat is 0-1 on everything; reserve 3 for what the "
          "anchor literally says. blast_radius and outward are about what CARRYING OUT the request would touch, not about the topic mentioned. "
          "Reply with ONLY a JSON array, one object per request, in the same order, each: {\"i\": <the request's i>, "
          + ", ".join(f'"{q}": …' for q in QUESTIONS) + "}. No prose, no code fence.")

from cortados_router.learning_data import (bind_snapshot, teacher_payload,
    label_provenance, TEACHER_EVIDENCE_NOTE)
SYSTEM += '\n\n' + TEACHER_EVIDENCE_NOTE


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for line in (STATE / "env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip().strip('"')
    env["PATH"] = f"{Path.home()}/.local/bin:/opt/homebrew/bin:" + env.get("PATH", "")
    return env


def _scarce(model: str) -> float:
    sys.path.insert(0, str(REPO / "cortados_router"))
    import scarcity as sc
    lane = (sc.current().get("lanes") or {}).get("claude") or {}
    pool = sc.pool_for("claude", model, list(lane.get("pools") or {}))
    return float(((lane.get("pools") or {}).get(pool) or lane).get("scarcity") or 0.0)


ONLY: list[str] = []      # --only q1,q2: relabel just these questions; rows are written with "override": true and merged over the full labels


def system_prompt() -> str:
    if not ONLY:
        return SYSTEM
    return (core.SYSTEM + "\n\nYou are the TEACHER producing training labels for ONLY these questions: " + ", ".join(ONLY) + ". Read each definition "
            "again and apply it literally. Reply with ONLY a JSON array, one object per request, in the same order, each: {\"i\": <the request's i>, "
            + ", ".join(f'"{q}": …' for q in ONLY) + "}. No prose, no code fence." + "\n\n" + TEACHER_EVIDENCE_NOTE)


def call_teacher(model: str, batch: list[dict]) -> tuple[list[dict], dict]:
    cwd = STATE / "teacher-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    from integrations.mcp.config import load_config
    config = load_config(Path.home() / ".config/cortados-workers/config.json")
    return _TEACHER.call_claude(model, batch, system_prompt=system_prompt(), environment=_env(),
        cwd=str(cwd), config=config, max_scarcity=_MAX_SCARCITY)


def work(job: tuple[str, int, list[dict]]) -> int:
    model, sample, batch = job
    if _stop.is_set():
        return 0
    try:
        answers, meta = call_teacher(model, batch)
    except Exception as exc:
        msg = str(exc)
        print(f"  ! {model} s{sample}: {type(exc).__name__}: {msg[:160]}", flush=True)
        if re.search(r"limit|429|quota|overloaded", msg, re.I):
            _stop.set()
        return 0
    if ONLY:
        good = _STORE.publish_override(batch, model=model, sample=sample, answers=answers,
            only=ONLY, defaults=DEFAULTS, snapshot=_SNAPSHOT)
    else:
        good = _STORE.publish_claude(batch, model=model, sample=sample, answers=answers,
            meta=meta, snapshot=_SNAPSHOT)
    _TEACHER.record_claude_usage(model, meta)
    return good


def _main_locked() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--k", type=int, default=3); ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--workers", type=int, default=3); ap.add_argument("--split", default="all"); ap.add_argument("--only", default=""); ap.add_argument("--max-scarcity", type=float, default=SCARCITY_STOP, help="override the pool gate for an approved pass")
    a = ap.parse_args()
    if os.environ.get("CORTADOS_TRAINING_TEACHER_V1") == "1" and not a.only:
        if _MCP_LEARNING is None:
            raise RuntimeError("mcp_learning_release_unavailable")
        cwd = STATE / "teacher-cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        with _STORE._handoff():
            summary = _TEACHER.run_labels(data_dir=DATA, snapshot=_MCP_LEARNING.bind_snapshot(DATA),
                options=vars(a), system_prompt=SYSTEM, claude_environment=_env(), claude_cwd=str(cwd),
                worker_config_path=Path.home() / ".config/cortados-workers/teacher.json",
                validate_answers=valid_answers, questions=QUESTIONS)
        print(json.dumps(summary), flush=True)
        return 0
    if a.k < 1 or a.batch < 1 or a.workers < 1 or a.limit < 0 or a.split not in ("all", "train", "holdout"):
        raise ValueError("Invalid label options")
    global _MAX_SCARCITY
    _MAX_SCARCITY = a.max_scarcity
    global ONLY
    ONLY = [q for q in a.only.split(",") if q]
    if any(q not in QUESTIONS for q in ONLY):
        raise ValueError("Unknown override question")
    global LABELS
    LABELS = DATA / "labels.jsonl"
    if ONLY:
        LABELS = DATA / f"labels-override-{'-'.join(ONLY)}.jsonl"
    global _SNAPSHOT
    _SNAPSHOT = _MCP_LEARNING.bind_snapshot(DATA) if _MCP_LEARNING is not None else None
    rows = [r for r in (_SNAPSHOT["rows"] if _SNAPSHOT is not None else read_jsonl(DATA / "requests.jsonl"))
            if a.split in ("all", r["split"])]
    done = {(l["id"], l["teacher"], l["sample"]) for l in read_jsonl(LABELS)}
    if ONLY:                                             # override rows only make sense where a full label exists
        full = {(l["id"], l["teacher"], l["sample"]) for l in read_jsonl(DATA / "labels.jsonl")}
        rows = [r for r in rows if any(k[0] == r["id"] for k in full)]
    jobs = []
    for split, model in TEACHER.items():
        for s in range(a.k):
            todo = [r for r in rows if r["split"] == split and (r["id"], model, s) not in done
                    and (ONLY or not _STORE.occupied(r, s, model, _SNAPSHOT))]
            random.Random(f"{split}-{s}").shuffle(todo)      # each sample sees different neighbours: no batch-context echo between samples
            jobs += [(model, s, todo[i:i + a.batch]) for i in range(0, len(todo), a.batch)]
    random.Random(7).shuffle(jobs)
    if a.limit:
        jobs = jobs[:a.limit]
    print(f"{len(jobs)} teacher calls to make ({sum(len(j[2]) for j in jobs)} request-labels); already done {len(done)}", flush=True)
    total, t0 = 0, time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        for n, got in enumerate(ex.map(work, jobs), 1):
            total += got
            if n % 5 == 0 or n == len(jobs):
                models = {j[0] for j in jobs}
                worst = max(_scarce(m) for m in models)
                print(f"  {n}/{len(jobs)} calls · {total} labels · {int(time.time() - t0)} s · claude scarcity {worst}", flush=True)
                if worst >= a.max_scarcity:
                    print(f"  claude pool scarcity {worst} ≥ {a.max_scarcity}: stopping; rerun after the reset (resume-safe)", flush=True)
                    _stop.set()
    print(f"done: {total} labels written" + (" (stopped early)" if _stop.is_set() else ""), flush=True)
    return 0


_STORE = None
_SNAPSHOT = None
_MAX_SCARCITY = SCARCITY_STOP


def main() -> int:
    global _STORE
    sys.path.insert(0, str(os.environ.get("CORTADOS_PTY_PLUGIN") or Path(core.REPO) / "plugins/cortados-pty"))
    _stop.clear()
    with _ATTEMPTS.LabelAttempts(DATA, validate_answers=valid_answers, questions=QUESTIONS) as store:
        _STORE = store
        try:
            return _main_locked()
        finally:
            _STORE = None


if __name__ == "__main__":
    sys.exit(main())
