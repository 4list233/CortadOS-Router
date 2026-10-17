# Shared frontier exam amendment

Exam owner: `/root/quota_evaluation_qa`. Root explicitly approved this staged amendment on 2026-10-17.
Production routing thresholds are unchanged. The live file is not yet modified.

The shared `tests/test_router.py` still expects frontier selection at planning D=5 and underused D=6.
Current routing-table ceilings are D=7 and D=9, respectively. Standalone `tests/test_router.py` already
records the historical changes (CortadOS commits `634c97bb` and `dc029601`) and asserts those boundaries;
shared `ROUTER-DESIGN.md` §9 documents these stale expectations.

Only the two affected method bodies are replaced with their accepted standalone equivalents, plus
explicit negative assertions for the old D=5 planning and D=6 underuse triggers. This preserves the
original scenarios, boundary positives, below-boundary negatives, and unrelated code/low-difficulty
cases. Every byte outside those two methods is identical to the captured source. No test is skipped,
deleted, or relabeled. The original file and its two failing baseline outcomes remain evidence.

`shared-router-exam-amendment.json` pins the original and staged hashes. The frozen copies are
`shared-router-exam-before.py` and `shared-router-exam-candidate.py`. The candidate is staged at
`/Users/velox/.cortados-builds/fresh-quota-activation/tests/test_router.py`; applying it to the live shared
file requires successful staged checks and an unchanged original hash, and has no shared Git commit.
