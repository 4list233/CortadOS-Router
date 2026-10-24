"""Judge each finished ladder and label it from what the work actually took (AC-08, AC-11; D3, D5, D6).

One Claude Fable 5.1 call per task returns a verdict per band and the 12 routing answers, written by a teacher who
has seen the runs. A band passes when its check passed (if it has one) and the judge accepts it; the outcome band is
the lowest band that passed.

  python3 -B -m outcome.judge [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os

import common
from outcome import agents, quota, store

core = common.core
# D5: the holdout judge and trace teacher is Claude Fable 5.1. D6: no second teacher counts before it clears the
# agreement bar, and Codex did not; one teacher for both splits also keeps every label on one rubric.
TEACHER = os.environ.get("CORTADOS_OUTCOME_TEACHER", "claude-fable-5-1")
DIFF_CHARS = 12_000                  # per band: the judge needs the shape of each change, not every line

SYSTEM = (core.SYSTEM + """

You are now the JUDGE and TEACHER for a model router's training data. A request was given to AI agents of increasing
capability (bands R0 < R1 < R2 < R3 < R4), each in its own copy of the same repository. For each band you see what
the agent did: tools it used, its final message, the diff it produced, the result of an automatic check (when the task
has one) and any calls to mock outward tools.

1. For every band, decide whether the work meets the task's rubric (and the check, if one ran). Judge the result,
   not the effort; a confident message without the change is a fail.
2. Then answer the twelve routing questions about the REQUEST, as a router scorer should have answered them before
   any work began, but informed by what carrying it out actually took (steps, ambiguity met, files touched, effects).

Reply with ONLY one JSON object: {"verdicts": {"<band>": {"pass": true|false, "why": "<one sentence>"}, ...},
"labels": {""" + ", ".join(f'"{q}": …' for q in common.QUESTIONS) + """}}. No prose, no code fence.""")


def _band_view(run: dict) -> dict:
    if not run.get("result"):
        return {"band": run["band"], "ran": False, "why": run.get("why_none")}
    res = run["result"]
    tr = res["trace"]
    return {"band": run["band"], "model": run["route"]["model"], "ran": True, "tools": tr.get("tools"),
            "timed_out": tr.get("timed_out"), "final_message": tr.get("final"), "diff": (res.get("diff") or "")[:DIFF_CHARS],
            "check": res.get("check"), "outward_calls": tr.get("outward_calls")}


def outcome_band(verdicts: dict, runs: list[dict]) -> str:
    """Lowest band whose run passed both its check (when it has one) and the judge; 'above' when none did."""
    for run in sorted(runs, key=lambda r: core.BAND_ORDER.index(r["band"])):
        res = run.get("result")
        if not res:
            continue
        check = res.get("check") or {}
        if check.get("ran") and not check.get("passed"):
            continue
        if (verdicts.get(run["band"]) or {}).get("pass") is True:
            return run["band"]
    return "above"


def judge_task(spec: dict, runs: list[dict], ask=agents.ask_claude) -> dict:
    user = json.dumps({"request": spec["prompt"], "rubric": spec["rubric"], "category": spec["category"],
                       "bands": [_band_view(r) for r in sorted(runs, key=lambda r: core.BAND_ORDER.index(r["band"]))]},
                      ensure_ascii=False)
    reply = agents.json_payload(ask(TEACHER, SYSTEM, user), opener="{")
    verdicts, labels = reply.get("verdicts") or {}, reply.get("labels") or {}
    if not isinstance(verdicts, dict) or not common.valid_answers(labels):
        raise ValueError("judge reply has invalid verdicts or labels")
    return {"verdicts": verdicts, "labels": {q: labels[q] for q in common.QUESTIONS}, "outcome_band": outcome_band(verdicts, runs)}


def judge(limit: int = 0, ask=agents.ask_claude, gate=None) -> dict:
    gate = gate or (lambda: quota.gates(core))
    specs = {t["id"]: t for t in store.read("tasks")}
    done = store.ids("outcomes", key="task_id")
    runs_by_task: dict[str, list] = {}
    for r in store.read("runs"):
        runs_by_task.setdefault(r["task_id"], []).append(r)
    ladders = [l for l in store.read("ladders") if l["id"] not in done and l["id"] in specs][: limit or None]
    counts = {"judged": 0, "held": 0, "failed": 0}
    for lad in ladders:
        ok, why = gate().get("claude", (False, "no rule"))
        if not ok:
            counts["held"] += 1
            print(f"  · judging held: {why}", flush=True)
            break
        spec, runs = specs[lad["id"]], runs_by_task.get(lad["id"], [])
        try:
            result = judge_task(spec, runs, ask)
        except Exception as exc:                       # retried next pass; the ladder stays unjudged
            counts["failed"] += 1
            print(f"  ! judge {lad['id']}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            continue
        store.append("outcomes", {"id": store.digest_id("outcome", lad["id"]), "task_id": lad["id"],
                                  "split": common.split_of(lad["id"]), "teacher": TEACHER, "s1_band": lad["s1_band"],
                                  "request": spec["prompt"], **result})
        counts["judged"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    print(json.dumps(judge(ap.parse_args().limit)))


if __name__ == "__main__":
    main()
