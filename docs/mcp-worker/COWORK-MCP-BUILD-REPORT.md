# Cowork connector build status — 2026-10-18

**Not installed or connected. Candidate 5 is rejected for a confirmed pending-launch race.** The approved revision 3 contract passes all 33 D criteria. Independent QA is committing nine additional boundary regressions and correcting one new upload-test oracle before a fresh 37-row A review. One bounded correction remains under this design; no source grant has been issued for it yet.

## Frozen candidate and verification

- Branch: `plan/subagent-mcp`. Candidate 5 changes only the existing Broker, task-store and runtime owners relative to candidate 4. All 18 scoped source files are frozen below 500 physical lines; production changes remain uncommitted.
- Codex subscription job `b96d1d96-06fd-4d77-a377-d1df5460bd06` ran under root grant `e3baa71`, using `gpt-6.1-sol`, network disabled and no MCP grants. Completion is distinct from acceptance.
- Host inventory: **938 tests, 1 failure, 0 errors, 0 skips; 27/28 lanes pass**. All 785 tests outside the core lane and all 114 original core tests pass. The single inventory failure is an independently confirmed error in a new fixture, E5-01, described below.
- Separate actual-runtime probe: **3 tests, 1 failure, 0 errors**. Healthy and readable-store controls pass; the unreadable-store pending-launch case starts native execution after fatal monitor loss. Root reproduced this using the unchanged independent script.
- Independent B: **2 failed criteria across all 11 reviewed rows**, B1/B7, from one source cause C5-01. Final rejected feedback `e05f41df-c55b-415d-b0a8-fc9a6dcc553e`, sequence 39.
- Root verified all 36 final packet artifacts, all 18 sources and snapshots, the 3 normative documents and all 641 prior protected identities. Original fixture identities at exam commit `8f3c8fe` were checked. Worker Seatbelt boot-identity errors remain environment-blocked evidence and do not replace host results.

Exact host command from `/Users/velox/.cortados-builds/cortados-router-repo`:

```sh
umask 022
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/full-suite
```

Exit 1. [Full report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/full-suite/report.json), SHA256 `3055e8a52e2008c04c9590b72be19ab3c3dc75d108c227f88ae2fd0b8554348f`. [Source freeze](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/root-freeze.json), SHA256 `78336c7e069adcd7da8b21b49de9dc0be8092c9dcbf164d052cc8e15b4100623`; cumulative patch `8786c3fdb0670ae965329930be6c929a1c7325199d168b8788dd9cdc6a86da4c`.

[Independent full B review](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/independent/QA-B-REVIEW.md), SHA256 `a10c52ad928405d389f9f78e8d7cc797ca31f36bed506d49f058b02a6509786c`; final 36-artifact manifest `d438c18e57dd1f619e2c2cc60b7711f0c2165e032710d0761ec153d8f251b3e0`. [Root final verification](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/ROOT-FINAL-PACKET-VERIFIED.json) records the checks.

## Confirmed race and exam correction

C5-01 pauses the return of actual subprocess creation before the Broker has registered its new watchdog. The real monitor stops unexpectedly, and active-job enumeration fails during containment. Containment finishes with fatal admission latched and one failure signal. When subprocess creation resumes, the watchdog nevertheless receives START and performs one native execution. The pending launch must observe the existing fatal state before that effect. This violates approved R098/R101; correcting it does not require a new product policy.

Root ran the unchanged private probe under `umask 022`:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/root-reproduction/probe-pending-spawn.py
```

Probe SHA256 `7329cb1028de49f996a851663dd6318318b327f88909d8deb0e387d694e39ab6`; root output `9d7ebdee83a5aadc2d58d610f981fa89b4f2def0f019d3edab9ea4a1248e97e4`. Every synthetic worker has a real containment receipt, return code 0 and an empty final owned-worker map. No user data, provider or live credential was involved. Three database-only temporary directories from the combined verification window remain explicitly recorded and untouched; no universal cleanup claim is made.

E5-01 expected `observed_bytes == 0` before upload sealing, whereas parent section 4 requires null until exact sealing. QA has authority under `742e229` to correct this new oracle and strengthen direct evidence: observe the real upload-descriptor writes, require zero writes after monitor loss, and require the actual three-byte write in the healthy control. Original bytes and the failed run remain preserved. The original 114 tests are unchanged. Nine isolated pending-launch cases cover process creation, readiness and admission waits with healthy, readable-store-loss and unreadable-store-loss modes. Expected discovery is 162 core / 947 accumulated tests across 28 lanes; that count remains subject to final independent A verification.

This is the first valid source failure under approved revision 3, cumulative fifth. All previous failures remain. The second valid failure under this design would require a design-error packet under `contracts/CONVENTIONS.md`; changing model, branch or agent does not reset the count. Fresh committed exams/A and separate root authorization precede the remaining bounded correction.

## Completed measurements

All 12 transfer matrix cases plus monitor passed in 517.127 seconds. Ordinary and supplemental measurements completed sequentially. [Matrix report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/full-suite/core-performance.json), [ordinary report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/ordinary-measurements/report.json), and [supplemental report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/supplemental-measurements/report.json) retain raw samples and limitations.

| Operation | Baseline median | Candidate 5 median |
|---|---:|---:|
| Explicit route | 0.096188 ms | 0.099167 ms |
| Synthetic S1 route | 0.615167 ms | 0.627834 ms |
| Job get | 0.027042 ms | 0.027333 ms |
| Job replay | 0.057417 ms | 0.055417 ms |

These route measurements use deterministic scorer/quota captures, not actual S1 inference. Actual supplemental task creation changed from candidate 4's 15.224 ms to 15.318 ms; ASGI+IPC get changed from 4.054834 ms to 4.053417 ms. Finite measured transfer workloads support bounded buffering and index budgets, without establishing a universal RSS guarantee. The approved 100 MB file maximum still requires deployment-profile qualification. Incorrect code cannot be accepted on timing results.

## Other packages and release gates

Settled policy: decimal 100 MB/file, 1 GB/task and 10 GB total; delete uploaded/generated payload seven days after the latest job safely settles, including failure/cancellation; retain job/audit history. A new accepted job invalidates the deadline; later uploads inherit it; tasks without a settled job remain unarmed. GitHub `4list233` is the approved sign-in identity for a dedicated login app with no repository permissions. Credentials stay on the Studio.

The private retention v3 draft closes all five findings from its previous review. Root verified the author output and separate critical review, recording bounded document-only acceptance. It has not passed full D/A/B or been copied into the canonical package. It still needs accepted-core pinning, source headroom checks, independent exams and implementation. Evidence: [retention review](/Users/velox/.cortados-builds/cowork-retention-v3-review/REVIEW.md).

Auth 009 v6 and operator 010 v3 draft documents remain at `9bf0a5d`; each passes all 33 independent technical D rows. A, protected exams and source builds remain blocked on accepted core. The optional dependency profile is qualified separately and absent from ordinary MCP. Secret custody/provisioning remains a later concrete operator decision.

Remaining release gates: accepted core; separately gated auth/operator/retention builds; installed native-tool confinement and live budgets; actual GitHub numeric identity and finite-token refresh flow; hosted Cowork callback and binary-file materialization; concrete service/ingress activation. No public listener, OAuth app, secret, Funnel route or launchd service has been installed. Existing private HTTPS 443 to 127.0.0.1:8432 is unchanged.

Cowork remains the planner in the Air app/cloud session. Native workers, subscription credentials and optional S1 stay on the Studio. Workers receive explicit task evidence and selected files; they do not inherit Cowork's conversation, connectors or filesystem.

## Preserved history

Candidate 4's 899-test green inventory was independently rejected for C4-01 (malformed canonical policy ending the monitor without closing admission), B1/B6/B7. Its complete packet remains in [candidate 4 evidence](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/ROOT-FINAL-PACKET-VERIFIED.json). Candidate 3 had 896 tests / 10 failure events / 1 error / 0 skips and failed B1/B5/B6/B7; its packet remains in [candidate 3 evidence](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-3/independent/B-FINAL.md). Candidates 1/2, red baselines, native sandbox failures and exact historical 007 replay remain intact. No protected test was removed to improve the result.

Nightly training is separately complete: 1494 new labels and 1500 steps. The scheduled continuation finished evaluation; the unchanged under-routing gate rejected the candidate, preserving the incumbent. Training verification heartbeat is paused; the existing 01:30 nightly schedule remains active. Connector work has not changed training, models or schedules.
