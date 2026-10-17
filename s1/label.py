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

from common import DATA, QUESTIONS, REPO, core, read_jsonl, valid_answers

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
    payload = json.dumps(teacher_payload(batch), ensure_ascii=False)
    argv = ["claude", "-p", "--output-format", "json", "--no-session-persistence", "--max-turns", "1", "--tools", "", "--disable-slash-commands",
            "--strict-mcp-config", "--setting-sources", "project", "--model", model, "--system-prompt", system_prompt()]
    p = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=600, cwd=cwd, env=_env())
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(str(out.get("result"))[:300])
    m = re.search(r"\[[\s\S]*\]", out.get("result") or "")
    return json.loads(m.group(0)) if m else [], out


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
    good = 0
    with _write, LABELS.open("a") as f:
        for a in answers:
            if not (isinstance(a, dict) and isinstance(a.get("i"), int) and 0 <= a["i"] < len(batch)):
                continue
            if ONLY:
                if all(q in a for q in ONLY) and valid_answers({**DEFAULTS, **{q: a[q] for q in ONLY}}):
                    f.write(json.dumps({"id": batch[a["i"]]["id"], "teacher": model, "sample": sample, "ts": int(time.time()), "override": True,
                                        **label_provenance(batch[a["i"]]), "answers": {q: a[q] for q in ONLY}}, ensure_ascii=False) + "\n")
                    good += 1
            elif valid_answers(a):
                f.write(json.dumps({"id": batch[a["i"]]["id"], "teacher": model, "sample": sample, "ts": int(time.time()),
                                    **label_provenance(batch[a["i"]]), "answers": {q: a[q] for q in QUESTIONS}}, ensure_ascii=False) + "\n")
                good += 1
    try:                                                   # the subscription ledger sees teacher work like any other lane use
        # The usage ledger is CortadOS's PTY plugin ($CORTADOS_PTY_PLUGIN, else <CORTADOS_REPO>/plugins/cortados-pty).
        sys.path.insert(0, str(os.environ.get("CORTADOS_PTY_PLUGIN") or Path(core.REPO) / "plugins/cortados-pty"))
        import usage
        u = meta.get("usage") or {}
        usage.record({"job_id": f"teacher-{meta.get('session_id', time.time())}", "runtime": "claude", "account": "subscription", "model": model, "session_id": meta.get("session_id"),
                      "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
                      "cache_write_tokens": u.get("cache_creation_input_tokens"), "source": "router-teacher", "label": "router-teacher",
                      "dedup_key": f"teacher:{meta.get('session_id')}"})
    except Exception as exc:
        print(f"  ! teacher usage not recorded in the ledger: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return good


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0); ap.add_argument("--k", type=int, default=3); ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--workers", type=int, default=3); ap.add_argument("--split", default="all"); ap.add_argument("--only", default=""); ap.add_argument("--max-scarcity", type=float, default=SCARCITY_STOP, help="override the pool gate for an approved pass")
    a = ap.parse_args()
    global ONLY
    ONLY = [q for q in a.only.split(",") if q]
    global LABELS
    if ONLY:
        LABELS = DATA / f"labels-override-{'-'.join(ONLY)}.jsonl"
    rows = [r for r in bind_snapshot(DATA)["rows"] if a.split in ("all", r["split"])]
    done = {(l["id"], l["teacher"], l["sample"]) for l in read_jsonl(LABELS)}
    if ONLY:                                             # override rows only make sense where a full label exists
        full = {(l["id"], l["teacher"], l["sample"]) for l in read_jsonl(DATA / "labels.jsonl")}
        rows = [r for r in rows if any(k[0] == r["id"] for k in full)]
    jobs = []
    for split, model in TEACHER.items():
        for s in range(a.k):
            todo = [r for r in rows if r["split"] == split and (r["id"], model, s) not in done]
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


if __name__ == "__main__":
    sys.exit(main())
