"""The continuous outcome loop: mine → generate → run → judge, until stopped or every pool is at its stop rule.

Stop it with `touch <data home>/stop` (checked between steps; a running agent finishes its task first).

  python3 -B -m outcome.loop [--home ~] [--extra-home DIR:HOST ...]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import common
from cortados_router.provider import routing as provider_routing
from outcome import generate, judge, mine, quota, run, store

core = common.core
# Two ladders at a time: each is a full agent session on a subscription CLI. One leaves the night's quota unused;
# more risks the vendors' concurrency limits and overshooting the 95 % stop between 20-minute usage samples.
WORKERS = 2
# Keep about a night's work queued: generating far ahead spends Codex on tasks that may never be run.
TASK_BACKLOG = 20
# Transcripts grow slowly; re-mining every six hours picks up the day's new sessions without rereading constantly.
MINE_EVERY_S = 6 * 3600
# When every pool is held or nothing is runnable, wait before re-reading the usage (the sampler refreshes ~20 min).
IDLE_SLEEP_S = 600


def _stopped() -> bool:
    return (store.home() / "stop").exists()


def step(homes: list[tuple[Path, str | None]], state: dict) -> bool:
    """One pass. Returns True when it did any work."""
    worked = False
    if time.time() - state.get("mined_at", 0) > MINE_EVERY_S:
        for home, host in homes:
            print(json.dumps({"mine": str(home), **mine.mine(home, host)}), flush=True)
        state["mined_at"] = time.time()
    gates = quota.gates(core)
    print(json.dumps({"gates": {k: v[1] for k, v in gates.items()}}), flush=True)
    backlog = len(run.pending())
    if backlog < TASK_BACKLOG and gates["codex"][0] and not _stopped():
        counts = generate.generate(limit=TASK_BACKLOG - backlog)
        print(json.dumps({"generate": counts}), flush=True)
        worked = worked or counts["tasks"] > 0
    served = provider_routing.served_routes(core)
    batch = run.pending()[:WORKERS * 2]
    if batch and any(ok for ok, _ in gates.values()) and not _stopped():
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for spec, counts in zip(batch, pool.map(lambda s: run.run_task(s, served), batch)):
                print(json.dumps({"run": spec["id"], **counts}), flush=True)
                worked = worked or bool(counts.get("ran"))
    if not _stopped():
        counts = judge.judge()
        print(json.dumps({"judge": counts}), flush=True)
        worked = worked or counts["judged"] > 0
    return worked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=str(Path.home()))
    ap.add_argument("--extra-home", action="append", default=[], help="DIR:HOST, e.g. a copy of the Pro's transcripts")
    a = ap.parse_args()
    homes = [(Path(a.home).expanduser(), None)] + [(Path(h.rsplit(":", 1)[0]), h.rsplit(":", 1)[1]) for h in a.extra_home]
    state: dict = {}
    print(json.dumps({"loop": "start", "home": str(store.home()), "teacher": judge.TEACHER,
                      "generator": generate.GENERATOR}), flush=True)
    while not _stopped():
        try:
            worked = step(homes, state)
        except Exception as exc:                       # logged with its category; the next pass retries
            print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}), flush=True)
            worked = False
        if not worked and not _stopped():
            time.sleep(IDLE_SLEEP_S)
    print(json.dumps({"loop": "stopped"}), flush=True)


if __name__ == "__main__":
    main()
