---
name: verify-router
description: Verify a change to the CortadOS router by driving the real core.py entrypoint on throwaway state and producing an evidence block. Use after the full test suite passes and before calling any router, table or scorer change done.
---

# Verify the router

Tests prove the pieces; this proves the real entrypoint still makes sane decisions.

1. Run the full suite: `python3 -B -m unittest discover -s tests -t .` — it must be OK.
   Report run / skipped counts; the 2026-10-20 baseline is 172 run, 7 skipped.
2. Run `python3 -B .claude/skills/verify-router/verify.py`.
3. If the change touches routing behaviour, add one scenario to `verify.py` that exercises it
   through `decide` with an expected band or route, and the reason that outcome is required.
   Scenarios that need S1 must report BLOCKED, never PASS, when S1 is down.
4. Paste the evidence block (both outputs) into the handback / PR description.

Verdicts: `PASS` only if every scenario passed. `BLOCKED` means a scenario could not run
(usually S1 on :8431 is down — S1 normally runs on the home Mac Studio); say so plainly and do
not describe the change as fully verified. `FAIL` means stop and fix.

Never point verification at the live `~/.cortados` or run `scarcity.py` (it opens the real vendor CLIs).
