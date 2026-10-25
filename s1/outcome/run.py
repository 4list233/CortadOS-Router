"""Run each task at bands B-1, B, B+1 around S1's band (AC-04, AC-05).

The route for band b is the router's own choice with b as the floor (core.resolve, minimum_band=b) for a decision that
carries only the task's work kind, so the band is exactly b. Served routes are the provider's: one list, one owner.

  python3 -B -m outcome.run [--limit N]
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time

import common
from cortados_router.provider import routing as provider_routing
from outcome import agents, quota, store, tasks

core = common.core
# A strong model finishes a fixture task in minutes; 15 minutes leaves room for a slow lane. Shorter: real passes
# recorded as timeouts. Longer: one stuck run holds a worker for the night.
RUN_TIMEOUT_S = 900
LANE_OF_PROVIDER = {"claude-cli": "claude", "codex-cli": "codex"}
# Ladder runs are sandboxed benchmark work, not a person waiting: the interactive face floor does not apply.
UNIT_KIND = "background"
# S1 scores the prompt as what it was: a request someone typed into Claude Code, Codex or Cowork and waited on.
SCORE_UNIT_KIND = "interactive"


def zero_decision(work_kind: str) -> dict:
    answers = {f: 0 for f in core.FEATURES}
    answers.update(outward=False, work_kind=work_kind, urgency="overnight", needs_clarification=False, parts=0,
                   diversity=False)
    return {"answers": answers, "degraded": False}


def s1_score(spec: dict, served) -> dict | None:
    """S1's band for a task, scored once and stored, so a retried task keeps the same ladder."""
    for row in store.read("scores"):
        if row["task_id"] == spec["id"]:
            return row
    decision = core.decide(spec["prompt"], {"unit_kind": SCORE_UNIT_KIND})
    if decision.get("degraded") or not decision.get("answers"):
        return None
    r = core.resolve(decision, unit_kind=SCORE_UNIT_KIND, execution=True, allowed_routes=served)
    row = {"id": store.digest_id("score", spec["id"]), "task_id": spec["id"], "band": r["band"],
           "answers": decision["answers"], "engine": decision.get("engine"), "D": r.get("D")}
    store.append("scores", row)
    return row


# R4 is the table's escalation-only ceiling: core.required_index gives it no capability floor, so a zero-difficulty
# decision floored at R4 resolves to an R1-level model (Codex review 2026-10-24). The ladder tops out at R3.
TOP_BAND = "R3"


def ladder(band: str, work_kind: str, served) -> list[dict]:
    order = core.BAND_ORDER[: core.BAND_ORDER.index(TOP_BAND) + 1]
    i = order.index(band) if band in order else len(order) - 1
    rungs = []
    for b in order[max(0, i - 1): i + 2]:
        r = core.resolve(zero_decision(work_kind), unit_kind=UNIT_KIND, execution=True, minimum_band=b,
                         allowed_routes=served)
        chosen = r.get("chosen")
        rungs.append({"band": b, "required_index": r.get("required_index"),
                      "route": {k: chosen.get(k) for k in ("lane", "provider", "model")} if chosen else None,
                      "why_none": None if chosen else "; ".join(f"{s.get('model')}: {s.get('why')}" for s in (r.get("skipped") or [])[:5])})
    return rungs


def run_one(spec: dict, rung: dict) -> dict:
    route = rung["route"]
    lane = LANE_OF_PROVIDER[route["provider"]]
    workspace = tempfile.mkdtemp(prefix="outcome-")
    try:
        tasks.materialize(spec, workspace)
        trace = agents.run_task(lane, route["model"], spec["prompt"], workspace, spec["outward_tools"], RUN_TIMEOUT_S)
        diff = tasks.workspace_diff(workspace)
        check = tasks.run_check(spec, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return {"trace": trace, "diff": diff, "check": check}


CLAIMS = store.Claims()


def run_task(spec: dict, served, admit=None) -> dict:
    """All ladder runs for one task not yet run. Returns counts; rows go to runs.jsonl as they finish.

    A task is claimed first, so no two workers launch paid work for it; every run is announced in attempts.jsonl
    before it starts, so a crash mid-run leaves a visible spent attempt (the run itself is safe to repeat)."""
    admit = admit or (lambda lane, model: quota.admit(core, lane, model))
    if not CLAIMS.take(spec["id"]):
        return {"skipped": "claimed by another worker"}
    try:
        return _run_claimed(spec, served, admit)
    finally:
        CLAIMS.release(spec["id"])


def _run_claimed(spec: dict, served, admit) -> dict:
    previous = {r["band"]: r for r in store.read("runs") if r["task_id"] == spec["id"]}
    score = s1_score(spec, served)
    if score is None:
        return {"skipped": "S1 degraded"}
    band = score["band"]
    counts = {"ran": 0, "held": 0}
    by_route: dict[tuple, dict] = {(r["route"]["provider"], r["route"]["model"]): r["result"]
                                   for r in previous.values() if r.get("route") and r.get("result")}
    rungs = ladder(band, spec["work_kind"], served)
    for rung in rungs:
        if rung["band"] in previous:
            continue
        row = {"id": store.digest_id("run", spec["id"], rung["band"]), "task_id": spec["id"], "band": rung["band"],
               "s1_band": band, "split": common.split_of(spec["id"]), "route": rung["route"],
               "required_index": rung.get("required_index")}
        if rung["route"] is None:
            store.append("runs", {**row, "result": None, "why_none": rung["why_none"]})
            continue
        key = (rung["route"]["provider"], rung["route"]["model"])
        if key in by_route:                            # two bands resolved to the same model: one run serves both
            store.append("runs", {**row, "result": by_route[key], "reused": True})
            continue
        ok, why = admit(LANE_OF_PROVIDER[rung["route"]["provider"]], rung["route"]["model"])
        if not ok:
            counts["held"] += 1
            print(f"  · {spec['id']} {rung['band']} held: {why}", flush=True)
            continue
        store.append("attempts", {"id": row["id"] + "." + str(time.time()), "run_id": row["id"], "task_id": spec["id"],
                                  "band": rung["band"], "route": rung["route"], "started": time.time()})
        result = run_one(spec, rung)
        by_route[key] = result
        store.append("runs", {**row, "result": result})
        counts["ran"] += 1
    if not counts["held"]:                             # every rung has a row: the task is ready for the judge
        store.append("ladders", {"id": spec["id"], "s1_band": band, "bands": [r["band"] for r in rungs]})
    return counts


def pending(limit: int = 0) -> list[dict]:
    done = store.ids("ladders")
    return [t for t in store.read("tasks") if t["id"] not in done][: limit or None]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    served = provider_routing.served_routes(core)
    with store.single_instance():
        for spec in pending(ap.parse_args().limit):
            print(json.dumps({"task": spec["id"], **run_task(spec, served)}), flush=True)


if __name__ == "__main__":
    main()
