# Cowork connector build status — 2026-10-18

**Local core accepted and committed at `88e8a5dd7b8c0bd69719b649390a81fb881f25c3`. The Air connector is not installed or connected yet.** Candidate 6 passes all 33 D, 37 A and 11 B criteria. Root verified the exact source and evidence before acceptance. OAuth, operator setup, seven-day cleanup and actual Cowork qualification remain separate packages.

## Accepted core and verification

The core provides generated task folders, exact binary file transfer, scoped worker handoffs and durable task/job inspection through the existing subscription worker lifecycle. Candidate 6 extends the existing Broker's post-wait launch checks so an unexpected monitor termination blocks native START even before the queued monitor-completion callback runs. It reuses existing stop and receipt ownership. No new retry, timer, watcher or quota bypass was introduced.

- Branch `plan/subagent-mcp`; exact 18 production paths committed locally, nothing pushed. All parse and remain below 500 physical lines; maximum 445. Only Broker changed relative to candidate 5.
- Codex subscription job `b96d455a-9f27-4c56-a474-ab6cfd654226`, explicit `gpt-6.1-sol`, native-default, network disabled, no MCP grants; root build grant `3217827`.
- Full maintained package inventory: **947 passed, 0 failures, 0 errors, 0 skips; 28/28 lanes pass**, including 162 protected core methods and all 785 other methods. This inventory is the declared Router package and imported integration checks, not every unrelated CortadOS workstream.
- Independent review: all 11 B rows score zero. The actual queued-callback ordering probe passes 2/2 independently and 2/2 in root's unchanged-byte reproduction. Healthy execution reaches START; monitor loss reaches STOP with zero native executions. Real watchdog termination receipts and empty owned-worker maps are retained.
- Root verified all 72 final artifacts, 18 source files and frozen copies, 1,016 protected files, three normative documents and the exact exam manifest. Root feedback `9540e8cc-c87f-4f29-a61f-d0c596f7c625`, sequence 41, accepts only this local core.

Exact host command, working directory `/Users/velox/.cortados-builds/cortados-router-repo`:

```sh
umask 022
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/full-suite
```

Exit 0. [Full report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/full-suite/report.json), SHA256 `9cd602e1cff729ce604dbac0a605b4ac7436a5919b703d851768f820c96f95bf`; [source freeze](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/root-freeze.json), SHA256 `480ebff8d48512b5b0e2e2ab98588cec1fcb58d1902ffb72ed1e2e60f9963b7e`; tested cumulative patch SHA256 `71c2c77392012065764de5bae970a7cf5ae3daed70fc5ff4a801559ab6fee606`. Committed bytes match the frozen tested source.

[Independent B review](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/independent/QA-B-REVIEW.md) and [72-artifact manifest](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/independent/FINAL-MANIFEST.json), SHA256 `386138f629aa1818cb8ca960899932869b003895c31abd173bf466468f58b277`, retain every disposition and limitation. [Root final verification](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/ROOT-FINAL-PACKET-VERIFIED.json) records zero mismatches. An initial root checker resolved three document basenames from the wrong directory; its failed record is preserved alongside the corrected verification. No source or exam changed to correct the checker.

Root's additional probe command used the same working directory and umask:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/root-reproduction/probe-callback-order.py
```

Exit 0, 2 tests in 1.982 seconds. Probe SHA256 `c4020bdfb3f6e545ec0abf86a5c2f7568d8368a549861929652dc9d996a5a9c0`. [Probe verification](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/ROOT-CALLBACK-PROBE-VERIFIED.json) records actual process and receipt evidence. No synthetic worker command remained in the bounded post-run observation. Three database-only temporary directories were recorded untouched; no global cleanup claim is made. Worker sandbox boot-identity failures remain environment-blocked evidence and do not substitute for the host pass.

## Measured efficiency

All 12 transfer matrix cases plus monitor passed in 527.742 seconds, versus candidate 5's 517.127 seconds. Ordinary and supplemental workloads ran sequentially after the full matrix. [Full comparison](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-6/independent/PERFORMANCE-COMPARISON.json) and raw reports retain distributions and exact source identities.

| Operation | Original baseline median | Candidate 5 | Candidate 6 |
|---|---:|---:|---:|
| Explicit route | 0.096188 ms | 0.099167 ms | 0.101105 ms |
| Synthetic S1 route | 0.615167 ms | 0.627834 ms | 0.677667 ms |
| Context requirements | 0.004375 ms | 0.004334 ms | 0.004333 ms |
| Job get | 0.027042 ms | 0.027333 ms | 0.027208 ms |
| Job replay | 0.057417 ms | 0.055417 ms | 0.055375 ms |

The route workload uses deterministic scorer/quota captures; it does not measure actual S1 inference or justify an instant-routing claim. Candidate 5 to 6 task creation changed from 15.3183 to 15.6330 ms; ASGI plus IPC get changed from 4.0534 to 3.9956 ms. No fixed latency SLO or causal regression attribution follows from these samples. The transfer matrix covers 1/8/16 MiB, 4/64 KiB chunks and one/two streams; traced peaks remain at most 1,159,711 bytes and measured RSS is 49,283,072 bytes. These are bounded workloads, not a universal memory guarantee or qualification of the approved 100 MB live maximum. Capacity containment is sampled, not a hard OS disk quota.

## Remaining work and settled policy

Approved policy remains decimal 100 MB/file, 1 GB/task and 10 GB total; delete uploaded/generated payload seven days after the latest job safely settles, including failure/cancellation; retain job/audit history. A new accepted job invalidates the deadline; uploads inherit it; never-run tasks remain unarmed. GitHub `4list233` is the approved sign-in identity for a dedicated login app with no repository permissions. Subscription credentials remain on the Studio.

Auth 009 v6 and operator 010 v3 at `9bf0a5d` each passed all 33 technical D rows. Before their independent exams are authored, the staged test contract needs to distinguish auth-only B1 tests from later core-composition B2 tests while preserving cumulative coverage. The reviewed [staging plan](/Users/velox/.cortados-builds/cowork-mcp-verification/auth-exam-planning/PLAN.md) changes test staging only. Astra author job `a760864c-ea7d-4c9e-a09c-78a3f82e6f11` failed because the model was at capacity and changed no files; feedback sequence 40 is inconclusive. Root requested a narrow Sol authorship exception under the project's Astra author rule. That answer is pending. There is no automatic model retry or API fallback, and the existing D0 will not transfer to edited documents without fresh review.

The private retention v3 draft closed five earlier document findings but has not passed full D/A/B or entered canonical source. A new [source-grounded readiness audit](/Users/velox/.cortados-builds/cowork-retention-core6-readiness/REPORT.md) identifies exact accepted-core binding, cumulative file headroom, additive test inventory and constructor provenance work. Root accepts that bounded investigation only (feedback sequence 42). Technical sequence: qualify auth's core composition first, then pin retention to that accepted cumulative dependency. One integration owner controls shared runtime/config files. Reserve RT-COWORK-RETENTION-011/111; no retention source grant follows. All current tests accumulate; proposed retention modules join the existing discovery lane and their exact method union must be checked independently.

Remaining release gates: separately reviewed auth/operator/retention implementation and full suites; installed native-tool confinement and live budgets; actual GitHub numeric identity and finite-token refresh; hosted Cowork callback and binary-file materialization; concrete credential custody, service and ingress setup. No public listener, OAuth app, secret, Funnel route or launchd service has been installed for this connector. Existing private HTTPS 443 to 127.0.0.1:8432 is unchanged.

Cowork remains the planner in the Air app/cloud session. Native workers, subscription credentials and optional S1 stay on the Studio. Workers receive explicit task evidence and selected files; they do not automatically inherit Cowork's conversation, connectors or filesystem. S1 selects the worker; it does not process every output token.

## Preserved history

Candidate 5's full 938-method inventory had one separately confirmed new-fixture error, E5-01. Its independent pending-launch race C5-01 was a real source defect, reproduced by root; B1/B7 failed. Original records remain in [candidate 5 evidence](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-5/ROOT-FINAL-PACKET-VERIFIED.json). The independently approved fixture correction observes actual descriptor writes and preserves canonical null-before-seal fields. Exam commit `88b2eb5e5b45232ed503e7982ed7c73955863bff` adds nine pending-launch methods, preserves all 153 prior method ASTs and carries Exam-Owner. All 162 core methods now pass.

Candidate 4's green 899-method inventory was rejected for C4-01: malformed canonical policy ended the monitor without closing admission, failing B1/B6/B7. [Candidate 4 evidence](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/ROOT-FINAL-PACKET-VERIFIED.json) and candidates 1–3 remain intact. Candidate 6 is the second implementation attempt under approved revision 3 and cumulative sixth; all five prior valid source failures are preserved. Historical baseline red, native sandbox limitations and exact historical 007 replay remain recorded. No protected test was removed to obtain green.

Nightly training is separately complete: 1,494 new labels and 1,500 steps. Scheduled continuation evaluated the saved candidate; the unchanged under-routing gate rejected it, preserving the incumbent. The training verification heartbeat is paused; the existing 01:30 nightly schedule remains active. Connector work has not changed training, models or schedules.
