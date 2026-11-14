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
(usually S1 is down — S1 normally runs on the home Mac Studio); say so plainly and do
not describe the change as fully verified. `FAIL` means stop and fix.

Where S1 is found (#112): the same place the live router on this host finds it. `CORTADOS_S1_*` in
the environment win; otherwise `verify.py` reads the `CORTADOS_S1_*` lines of `~/.cortados/provider.env`
(the live provider's config, read as data, never sourced) and hands them to its children; otherwise
the router's own default (endpoints file, then loopback). The evidence header names the source, masks
non-loopback hosts and never prints the token. If it says `S1: down`, the probe of that URL's
`/health` failed, and the reason is printed next to it.

Live home (#112): on a host running the live provider, `~/.cortados` changes all the time, so the
check compares only what verification could write. Every child runs with `HOME` and `CORTADOS_HOME`
in a throwaway folder (no `.cortados` may appear under that HOME), the live `router.db` must gain no
`provider:verify` rows, and any other path verification wrote into its own homes must be unchanged
in the live home. Files the live services change are counted, not checked.

Never point verification at the live `~/.cortados` or run `scarcity.py` (it opens the real vendor CLIs).
`verify.py` runs every child with `CORTADOS_SCARCITY_REFRESH=off`, because a fresh throwaway home would
otherwise schedule a usage sample that opens those CLIs; keep that when adding scenarios.
