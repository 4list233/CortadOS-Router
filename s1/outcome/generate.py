"""Runnable task specs from seeds: Codex writes a self-contained task resembling each real request (AC-03).

Codex generates because it has the most quota room (Forest 2026-10-24); generation is not labelling, so the teacher
agreement bar (D6) does not apply here. Every spec is validated by tasks.validate before it is stored.

  python3 -B -m outcome.generate [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os

import common
from outcome import agents, quota, store, tasks

GENERATOR = os.environ.get("CORTADOS_OUTCOME_GENERATOR", "gpt-6-astra")
# Eight seeds keep one reply well under the CLI's message size with full fixtures. Fewer: more calls for the same
# tasks. More: the JSON reply truncates and the whole batch is lost.
BATCH = 8
# Writing a task spec is not a hard reasoning problem. The Studio's Codex home defaults to xhigh, measured 2026-10-24
# at 8.5 minutes for a two-seed batch; medium keeps the night's generation from crowding out the runs.
EFFORT = "medium"

PROMPT = """You turn real requests into runnable benchmark tasks for a model router.

For EACH request below, write ONE self-contained task that exercises the same kind of work at a similar difficulty,
so that a model's success or failure on it says how capable a model the original request needed. Keep any domain
detail that matters; replace private names, credentials and client data with realistic fakes.

Each task is a JSON object with exactly these fields:
- "seed": the request's "i"
- "prompt": the instruction an AI coding agent receives, written as the user would write it (under 4000 characters)
- "category": a short tag (e.g. "python-bugfix", "refactor", "data-analysis", "writing", "research-summary", "ops-script")
- "work_kind": one of {work_kinds}
- "fixture": {{"files": {{"relative/path": "file text"}}}}: a small repository the agent works in (at most 30 files,
  150 kB in total). Include whatever the task needs: source, data files, notes. Paths are relative.
- "check": {{"argv": [...], "timeout_s": N}}: a command run in the repository AFTER the agent finishes; exit 0 means the
  task was done correctly. Use Python 3.9 standard library only (e.g. ["python3", "-m", "unittest", "discover", "-s",
  "tests"]); put the tests in the fixture, written so a correct solution passes and the untouched fixture FAILS.
  No network. For work with no executable check (writing, research, planning), use {{"argv": [], "timeout_s": 1}}.
- "rubric": 2-6 concrete criteria a reviewer uses to judge the result
- "outward_tools": names of actions that would affect the outside world in the real request (e.g. "send_email",
  "post_slack_message", "create_invoice"); the agent gets mock tools with these names. [] when none.

Reply with ONLY a JSON array of task objects, no prose, no code fence.

REQUESTS:
{requests}"""


def _pending_seeds() -> list[dict]:
    used = store.ids("tasks", key="seed_id") | store.ids("generation_refusals", key="seed_id")
    return [s for s in store.read("seeds") if s["id"] not in used]


def generate(limit: int = 0, ask=agents.ask_codex, admit=None) -> dict:
    admit = admit or (lambda lane, model: quota.admit(common.core, lane, model))
    seeds = _pending_seeds()[: limit or None]
    counts = {"tasks": 0, "refused": 0, "batches": 0, "failed_batches": 0, "held": 0}
    for k in range(0, len(seeds), BATCH):
        batch = seeds[k:k + BATCH]
        ok, why = admit("codex", GENERATOR)
        if not ok:
            counts["held"] += 1
            print(f"  · generation held: {why}", flush=True)
            break
        payload = json.dumps([{"i": n, "request": s["text"], "tools_used": s["tools"][:10]} for n, s in enumerate(batch)],
                             ensure_ascii=False)
        counts["batches"] += 1
        try:
            reply = agents.json_payload(ask(GENERATOR, PROMPT.format(work_kinds=common.core.WORK_KINDS, requests=payload),
                                            effort=EFFORT))
        except Exception as exc:                       # the batch is retried on the next pass: its seeds stay pending
            counts["failed_batches"] += 1
            print(f"  ! generation batch failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            continue
        answered = set()
        for item in reply if isinstance(reply, list) else []:
            n = item.pop("seed", None) if isinstance(item, dict) else None
            if not isinstance(n, int) or not 0 <= n < len(batch) or n in answered:
                continue
            answered.add(n)
            seed = batch[n]
            spec = {**item, "seed_id": seed["id"], "id": store.digest_id("task", seed["id"], str(item.get("prompt")))}
            try:
                tasks.validate(spec)
            except tasks.SpecError as exc:
                store.append("generation_refusals", {"seed_id": seed["id"], "why": str(exc)})
                counts["refused"] += 1
                continue
            store.append("tasks", spec)
            counts["tasks"] += 1
        for n, seed in enumerate(batch):
            if n not in answered:                      # a seed the generator skipped is not retried forever
                store.append("generation_refusals", {"seed_id": seed["id"], "why": "generator returned no task"})
                counts["refused"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    print(json.dumps(generate(ap.parse_args().limit)))


if __name__ == "__main__":
    main()
