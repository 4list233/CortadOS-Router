<!-- PURPOSE: Report the rejected training teacher candidate and the required design review.
     CONTRACT: RT-TRAINING-TEACHER-007; independent B2 design_error, no activation authority.
     DEPENDS: B1/B2 review packets, frozen exams, exact source/wheel and private verification. -->
# Codex training fallback: design review required

As of October 17, 2026, 22:11 UTC, the Codex training-label fallback is **not enabled**.
Independent QA rejected the second candidate because reusing a label writer can preserve stale
reservation indexes. RT-TRAINING-TEACHER-007 is `design_error`; implementation and activation stop
pending Forest's review. The existing nightly schedule remains loaded. No new live labels were
generated, no model was promoted, and the existing held-out labels and promotion gates are unchanged.

## Authorized scope and candidate

Forest approved Codex subscription workers generating new final-training labels while preserving
the existing exam. Claude remains the primary teacher; typed pre-start quota refusal permits the
Codex fallback only for final-training rows. Accepted jobs have durable identities and reserved label
slots. Unknown or exhausted Codex quota still refuses; there is no API-key fallback or automatic new
attempt after an accepted terminal failure. Ordinary worker configuration remains unchanged.

The candidate extends existing broker/native admission owners. `teacher.py` owns teacher selection
and the broker adapter; `teacher_attempts.py` owns reservations and label publication. Staged adapters
change only training `label.py` and the nightly label interpreter. The live adapters were not replaced.

## Failed attempts and proposed revision

The independent [B1 review](../../acceptance/training_teacher/B1-REVIEW.json) found writer-lifetime
violations, differences in preserved Claude error/row handling, and repeated historical parsing.
The grouped second candidate addressed those findings. The independent
[B2 packet](../../acceptance/training_teacher/B2-REVIEW.json) has one remaining source defect:

1. Writer A acquires the lock, computes empty occupancy, then exits.
2. Writer B acquires the lock and durably reserves slots for Codex.
3. Writer A is reused. It reloads the manifest, but keeps its prior derived reservation cache.
4. It incorrectly treats the reserved slots as free, permitting duplicate training labels.

Root reproduced the new protected regression. This is a source failure, not a missing credential
or test-environment failure. No third implementation attempt has started.

**Recommended design revision for Forest's approval:** acquiring each new invocation lock starts a
fresh derived-cache generation. The existing sole writer discards prior parsed-label and reservation
indexes, reloads and validates current persisted state, and then derives occupancy. Exit retires that
invocation's authority while retaining serialization and PID/inode checks. Reuse remains supported.
This extends the existing owner; it adds no second cache/store/lock and changes no teacher selection,
quota policy, permissions, evaluation labels, promotion gates or budget.

After approval, independent QA must review/freeze the revised design and acceptance evidence before
an MCP worker implements it. The original contract identity and both failures remain recorded. A new
candidate needs the complete accumulated suite, source/wheel parity, benchmark, independent B0 and
guarded activation before the already-authorized bounded real label check. Approval of this design
does not bypass any gate.

The stop is required by [CONVENTIONS](../../../../cortados/contracts/CONVENTIONS.md), Attempts and
escalation: “The second valid failure sets `design_error`, stops dependent builds and returns a
design-error packet to the orchestrator.” The pinned standards' ORCHESTRATION section 7 requires
the orchestrator to return an Attempt 2 failure to the owner. This request is that review, not a
request to relax strict quota or alter the exam.

## Verification and exact candidate

Full scoped inventory before the new regression: **782 passed, 0 failures, 0 errors, 0 skipped**.
The subsequently added independent regression: **1 failed, 0 errors, 0 skipped**. Across those two
runs the current accumulated evidence is 782 passing and 1 failing test. **Overall acceptance failed.**
The 782 count covers standalone, integrations, all maintained package acceptance directories,
preserved PTY checks, selected shared router/nightly suites and installed adapters/parity. It is not
verification of every unrelated CortadOS workstream. The new failing regression has not been hidden,
weakened or excluded from future full runs.

Commands, run from the standalone checkout:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py /Users/velox/.cortados-builds/training-teacher-verification/full-suite-attempt2
PYTHONDONTWRITEBYTECODE=1 /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest -v acceptance.training_teacher.test_invocation_cache
```

The full runner contains exact lane commands and environment overrides in its report. The first
command returned 0; the second returned 1 with `Reentered writer lost the current reservation index`.
Full run source base: `e9ea9da9ad7a4983f3caf3360d3dea0b2e81541b` plus the exact candidate below.
QA recorded B2 and the additive regression in local commit `0414d21`; production bytes did not change.

| Evidence | SHA256 |
|---|---|
| Candidate manifest, seven production/stage files plus two builder test files | `07cb69f7523a17fdbf54f350fdc2826e0d825018ae4cdf44e7ddd3a6f56b867b` |
| Full 782-test report | `10d383b9560218ce95c3b599c0a097b4d6f196b08a365ffe17065ee8be8555e3` |
| Independent B2 packet | `183a5b720c998176cdf9265291061933c073c471b119941895313ae7c0da0a0a` |
| Amendment 4 freeze, 81 verified files | `9443562b4e4bd050a4ea403706929b404a38d741702ce9028864805522e985dc` |
| Inactive candidate wheel | `74a0931153e15ea6b0896a08005eac87bbd82aa2917d6ccf690a40cdff6368c2` |

Private raw evidence is in `/Users/velox/.cortados-builds/training-teacher-verification/`:
`full-suite-attempt2/`, `attempt2-inputs/`, `release.json`, and `design-error-final.json`.
Attempt 1 remains preserved separately. The unregistered release at
`~/.local/share/cortados-workers/releases/training-teacher-v1-20261017` is rejected and inactive;
57 declared installed Python/CJS files passed source/wheel parity. Its presence is not activation.
Production candidate changes remain uncommitted in this isolated checkout, with exact copies saved.

## Efficiency evidence

The synthetic labeling benchmark made zero provider calls. It tests complete local labeling work,
excluding provider latency and model quality; it does not measure S1 routing latency.

| Workload | Legacy primary median | Candidate primary median | Candidate fallback |
|---|---:|---:|---:|
| 12 requests, no history, one sample | 0.006659 s | 0.007791 s | 0.016141 s, one run |
| 4,554 requests, 4,071 historical IDs, three samples | 0.361271 s | 1.936517 s | 15.386183 s, one run |

Primary medians use three repetitions. Large primary runs retain 123 calls, 1,449 new labels and
matching answer signatures. Candidate peak Python allocation is approximately 35.6 MB versus
28.3 MB baseline. Durable atomic replacements write 512,897,684 logical bytes for primary and
550,399,032 for fallback. The fallback makes 96 synthetic starts for 1,140 final-training labels;
there is no successful legacy fallback comparator. B1's approximately 29.26-second primary median
fell to 1.94 seconds after removing repeated parsing, but the candidate remains slower than legacy.
The benchmark's mechanical checks passing does not override the cache-integrity failure or establish
a latency budget. Raw runs: `full-suite-attempt2/teacher-performance.json`.

## Live state and Air setup

Read-only verification at 22:11 UTC found all 11,660 existing label lines (4,312,808 bytes), protected
exam/model files and shared label/nightly sources unchanged. Teacher configuration, private teacher
broker state and activation approval are absent. No real Codex labeling check was run. Ordinary MCP
configuration retains SHA256 `41b579779ad64ada69c8d31ef70090fbac5a6ca5b9fdffe4e1a6c30ee85ade82`.

`com.cortados.router-nightly` remains loaded for 01:30 Toronto and was idle. Its recorded last exit
code was **1**, not a successful run. Existing code can train existing labels when the teacher phase
refuses, but this report does not promise the next run will succeed. Fresh Claude and Antigravity
quota remain unverified; common/Cursor fixes from 006 remain installed.

Unknown quota means the system cannot verify the remaining allowance for the actual worker's
account and launch context. It does not mean the subscription is empty or the planner is signed out.
Claude's prior quota endpoint returned a rate limit and its worker source binding remains unproven.
Antigravity lacks verified reset geometry. Codex fallback retains the same strict admission checks.

The independent Air setup is complete on the Studio: `/Users/velox/.claude/CLAUDE.md` teaches new
Claude sessions `context_requirements`, delegation, result review and `job_feedback`. The file is
0600 with SHA256 `ba5726d105955a02e18fc042592db0d14d0ff6b22480dd650fb9b5e8ab641cfa`.
From the Air, SSH to `velox@forests-mac-studio`, enter the Studio project directory and start
`/opt/homebrew/bin/claude`. Confirm `/memory` or `/context` and `/mcp` in that session.
The [Air guide](AIR-TO-STUDIO.md) records the commands and limits. Actual Air interactive
authentication was not observed. Credentials stay on the Studio; S1 scores delegated assignments,
not every output token. The current Codex session follows Forest's separate Codex planner workflow.
