# Cowork connector build status — 2026-10-18

**Not installed or connected. Candidate 4 is rejected and requires design review.** All 899 maintained tests pass across 28 host lanes, but an additional independent integrity test proves that a malformed stored task can stop monitoring while new work remains accepted. Root reproduced the failure. The reviewed [revision 3 proposal](COWORK-MCP-008-REVISION-3-PROPOSAL.md) awaits Forest's approval; no further core or dependent auth implementation is authorized yet.

## Frozen candidate and verification

- Branch: `plan/subagent-mcp`; source observed at `bec965aeff0471c714bd42c9f5b2354010c82a57`. Eighteen source files are frozen, all below 500 physical lines. Candidate source remains uncommitted.
- Codex subscription job `d583bbd4-061b-45ec-a4a9-db5a1ee5a2c8` ran the reviewed correction under grant `9eac6e3`, with `gpt-6.1-sol`, network disabled and no MCP grants. Native completion is distinct from acceptance.
- Full host inventory: **899 tests, 0 failures, 0 errors, 0 skips; 28/28 lanes pass**. This covers the standalone Router/MCP inventory and its declared shared-router compatibility lanes, not unrelated CortadOS workstreams.
- Additional actual-runtime negative: **1 failure, 0 errors**; discriminating controls: **2 passes**. Root reproduced both using unchanged copies of the independent scripts.
- Independent B: **3 failed criteria across all 11 reviewed rows**, B1/B6/B7, from one causal cluster C4-01. Final rejected feedback `a6ab574f-c50d-4d55-b710-774d76d06049`, sequence 28.
- Root verified all 29 final evidence artifacts and 680 source, normative, fixture and prior protected-file identities, with zero mismatches. No builder changed protected exams. Known worker Seatbelt boot-identity errors are retained as environment-blocked evidence; they do not replace the host results.

Exact host command, from `/Users/velox/.cortados-builds/cortados-router-repo`:

```sh
umask 022
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/full-suite
```

Exit 0. [Full report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/full-suite/report.json), SHA256 `456464e6d5f4c9f1a098d529442312d7a939dba2b9adb73d85dc4a06327835d9`. [Source freeze](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/root-freeze.json), SHA256 `683cbaff32b7e74b10d928c23c65796c5f4c4757dac26bdb9ab32486d80fccfc`; cumulative patch `3981d2beff8e7f14d5253f942a90afcd7177e4704379a17fcd981fa76dfa2df4`.

[Independent final B](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/independent/B-FINAL.md), SHA256 `ac979190caa15e1a4f147e44268e238a7db350a4ed197b50cd8ed301a4e4f92b`; final 29-artifact manifest `28fbb2f92cfda2962f712dd6f8e8335b6221ee9e97e99b963c798416dcfdd486`. [Root final verification](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/ROOT-FINAL-PACKET-VERIFIED.json) records the checks.

## Failure and proposed correction

The task reader accesses nested policy fields before validating the complete canonical record. In a private synthetic database, policy `{}` causes a `KeyError` that terminates the real monitor. Fatal and admission latches remain false, no failure signal appears, and an unrelated task creation succeeds through actual IPC. A valid record remains healthy; a malformed scalar record takes the existing fatal path correctly. No user data, native model, provider or credential was involved.

Root reproduced the counterexample and controls with these exact commands, under `umask 022`:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/root-probes/probe-persisted-read.py
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/root-probes/probe-persisted-controls.py
```

The negative exits 1; controls exit 0. [Root host summary](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/ROOT-HOST-SUMMARY.json) preserves the exact logs, observations and durations.

Revision 3 proposes complete canonical validation before field-dependent reads, plus explicit runtime ownership of unexpected monitor termination. Both completion observation and an admission-time check are necessary: an already-ready request can run before a queued completion callback. Expected shutdown remains distinct. Existing fatal signaling, worker containment, deadlines and capacity-only inspection/cancellation behavior remain canonical; no extra watcher, automatic restart, retry or reopening is proposed.

This is the second valid failure under approved revision 2, cumulative fourth. The two v4 failures and both revision 2 failures remain in the ledger. `contracts/CONVENTIONS.md` requires design review after the second valid failure; changing model, branch or agent cannot reset the count. If Forest approves, contract amendments, independent D review and additive committed exams/A precede another bounded build and full B review. The current 114 core tests and all earlier failed evidence remain preserved.

## Completed measurements

All 12 instrumented transfer cases plus monitor completed in 516.031 seconds. Ordinary and supplemental workloads also completed. [Raw comparison](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/independent/performance-comparison.json), SHA256 `ad053352aa9211260f8f69eb86bc734f7f88de9510429bea3c2f572015af570a`, retains samples and limitations.

| Operation | Baseline median | Candidate 4 median |
|---|---:|---:|
| Explicit route | 0.096188 ms | 0.098291 ms |
| Synthetic S1 route | 0.615167 ms | 0.617313 ms |
| Context requirements | 0.004375 ms | 0.004334 ms |
| Job get | 0.027042 ms | 0.028292 ms |
| Job replay | 0.057417 ms | 0.056916 ms |

These route measurements use deterministic scorer/quota captures; they are not actual S1 inference latency. Actual core supplemental medians: task create 15.224 ms, replay 3.471 ms, get 3.138 ms, delete 23.561 ms; ASGI+IPC get 4.055 ms. Four 1 MiB streams completed in 0.589 s. Untraced 16 MiB PUT/GET medians were 1069.697/719.329 ms. No external connections were made in these synthetic measurements.

Instrumented 1/8/16 MiB cases use 4/64 KiB chunks and 1/2 streams. Peak traced allocations stayed below 1.17 MB. This supports bounded buffering on the finite measured workloads; cumulative RSS is not an isolated per-operation measurement or universal guarantee. Two 16 MiB streams took 204.790 s at 4 KiB versus 13.682 s at 64 KiB under tracing. Existing 600 s fixture budget is not a live default. The approved 100 MB file maximum still requires deployment-profile qualification. A 4096-byte overshoot at a synthetic 4096-byte file bound demonstrates sampled containment, not an OS hard quota. No additional efficiency defect was identified; timing cannot accept incorrect code.

## Other package progress and deployment policy

Forest approved decimal 100 MB/file, 1 GB/task, 10 GB total, automatic completed-task payload cleanup after seven days, and GitHub account `4list233` for sign-in through a dedicated login app with no repository permissions. Those choices are settled. One completion-trigger clarification remains pending; no answer is inferred. Retention must extend the existing deletion/lifecycle owners through a separate reviewed package. Job and audit history remains; all-content erasure is not promised.

Auth 009 v6 and operator 010 v3 are committed as document drafts at `9bf0a5d`, following technical reconciliation direction `ab4aff9`. Their full independent D reviews are running. Earlier auth v5 D0 is historical evidence only; it does not approve v6. No auth exams/A/source are accepted yet. The optional 65-package dependency profile was qualified separately and is absent from ordinary MCP. Secret custody and provisioning remain a later concrete operator decision.

Remaining release gates: accepted core; separately gated auth/operator/retention implementation; installed native-tool confinement and live budgets; actual GitHub numeric identity and finite-token refresh flow; hosted Cowork callback and binary-file materialization; then concrete service and ingress activation. No public listener, OAuth app, secret, Funnel route or launchd service has been installed. Existing private HTTPS 443 to 127.0.0.1:8432 is unchanged.

Cowork remains the planner in the Air app/cloud session. Native workers, subscription credentials and optional S1 stay on the Studio. Workers receive explicit task evidence and selected files; they do not inherit Cowork's conversation, connectors or filesystem.

## Preserved history

Candidate 3: 896 tests, 10 failure events, 1 error, 0 skips, 27/28 lanes pass; B1/B5/B6/B7 failed. Its final packet and 27-artifact manifest `62c244bb8584a419633bf494d011d51aab65a087805a1d7554a02ec7ef2ab9f1` remain in [candidate 3 evidence](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-3/independent/B-FINAL.md). Candidates 1 and 2, original red baselines, native sandbox failures and exact historical 007 replay remain intact. No protected test was removed to obtain the current green inventory.

Nightly training is separately complete: 1494 new training labels and 1500 training steps. The scheduled continuation completed evaluation; the unchanged under-routing gate rejected the model, so the incumbent remains. Training verification heartbeat is paused; the existing 01:30 nightly schedule stays active. This connector work did not change training, models or schedules.
