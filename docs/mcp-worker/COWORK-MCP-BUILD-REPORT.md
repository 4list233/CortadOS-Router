# Cowork connector build status — 2026-10-18

**Not installed or connected.** The second local-core candidate failed independent review. The assignment is in `design_error`; another implementation attempt and dependent OAuth source work await Forest's decision on the [bounded revision proposal](COWORK-MCP-008-REVISION-2-PROPOSAL.md). Existing subscriptions, ordinary MCP, private Studio HTTPS443→8432 and nightly training remain unchanged. No public listener, OAuth application, secret, Funnel route or launchd service has been created.

## What exists

The local candidate implements automatic task workspaces, task/job ownership, sealed input/output artifacts, raw binary transfer tickets, the existing subscription worker broker and optional S1 handoff. It is a reviewed candidate, not an accepted release. Source changes span18 authorized paths, maximum 414 physical lines. All protected tests and frozen008 contract bytes remain unchanged by the builders.

OAuth 009 v5 passed all 33 independent design checks at commit d96dc1c. Authentication source and its protected exams have not been built. The optional 65-package dependency profile was qualified separately; it was not installed into ordinary MCP. The operator setup 010/110 documents are nonbinding proposals for existing auth/service owners; their initial provisioning and secret-resolver amendments are not accepted implementation authority.

Cowork remains the planner in the Air app/cloud session. Native workers, saved subscription credentials and S1 remain on the Studio. The connector will advertise delegation instructions. It does not automatically share Cowork's conversation, connectors, skills or filesystem. Actual hosted OAuth and binary upload/download in this account remain completion gates.

## Frozen candidate and verification

- Branch: `plan/subagent-mcp`; source baseline 4ed7508, terminal observation HEAD d96dc1cf08c9e32c1da7cabc7f47f358d8dd697e. Later commits change documents only.
- Corrective worker: d37a73fb-aa46-491f-9a5d-98fa60c682e8; same assignment group 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1. Native Codex subscription, explicit gpt-6.1-sol, shell network disabled and no MCP grants.
- Root cumulative source patch SHA256 634cce15a58df7f94512a9402cb2d22dfc26ec582ce55a2ae7f98b3409227394; source map in [root-freeze.json](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-2/root-freeze.json).
- Independent identity checks: 18 sources, 3 normative documents, 18 core fixture files and 641 prior protected files match. Current core manifest 03589f6ad5394e4c5d3c29f37979f349f7fb4b1c7cd8b6671b0c55f94da49959.
- Full standalone Router/MCP inventory, including its maintained shared-router compatibility lanes: **874 methods: 873 passing, 1 failure, 0 errors, 0 skips; 27 of 28 lanes passed.** This is not a claim that unrelated CortadOS workstreams were tested.

Exact command from the standalone checkout:

```sh
umask 022
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-2/full-suite
```

Exit 1. [Full report](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-2/full-suite/report.json) SHA256 756c7221d561f42e6b3265f4807e68e51beb66968b8f599b6546c19502160ce0. The sole failing method is `CapacityExam.test_workspace_file_overrun_stops_actual_fake_worker`; job_result returns storage_error instead of the contained cancelled/capacity_exhausted outcome. A separate observational rerun passed; it does not erase the original failure or establish its exact scheduling cause. A deterministic missed-cadence probe reproduces the observation-policy conflict with a verified termination receipt.

The original 785-method baseline was784 pass / 1 failure while authorized training appended labels. Its red report remains intact. The declared inventory separately preserves exact historical 007 assertions/data, current immutable-prefix/exam guards and candidate ordinary compatibility; no test was weakened or silently skipped. Worker sandbox-blocked sysctl/process observations are retained separately and are not substituted for the host run.

## Independent rejection and proposed correction

Full 11-row source/correctness B review scores 3, at B1/B5/B7. This is the second valid failed source attempt; earlier preflight-only environment blocks are separate. Feedback 52cbfe2a-6eb5-4bea-90b6-9ea0fe162ec3 / sequence 19 records rejection. Changing worker/model/ID cannot reset that history.

1. Response lifetime: Range 416 can stall beyond the deadline; a stalled PUT 201 response produced a second 408 response start; timeout error delivery acquired a fresh idle interval. A related actual AF_UNIX probe confirms that the oversized-response IPC fallback writes without the original deadline. The [IPC review](COWORK-MCP-008-IPC-FALLBACK-REVIEW.md) distinguishes real transport evidence from its synthetic domain-result selector.
2. Capacity inspection: the monitor correctly detects a missed sample and stops the worker, but its admission latch also blocks the required saved-result inspection. This differs from fatal storage/audit corruption, which must continue to refuse authority.

The proposed revision keeps one deadline per existing HTTP/IPC owner, tracks header/frame emission before an awaited send, aborts when no budget remains, and defines a narrow capacity-only inspection/cancellation surface. New work and transfers remain blocked; integrity failure and disabled identity override the inspection exception. The proposal changes no source, frozen contract or exam until approved. Independent additive tests and renewed design/acceptance gates precede any revised builder.

## Measurements

The unchanged protected transfer matrix completed all 12 cases and its monitor scenario in 511.93 seconds. This is completed measurement, not correctness acceptance. [Raw matrix](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-2/full-suite/core-performance.json) SHA256 2ce47c0abd4855673cc5c581224f186ad1504d0b549f4e5bb1a1e6a45d844fae. Tracemalloc is enabled, so these are instrumented local-core timings, not hosted network or model latency.

| Payload per stream |4KiB chunks,1stream|4KiB chunks,2streams|64KiB chunks,1stream|64KiB chunks,2streams|
|---|---:|---:|---:|---:|
|1MiB|6.53s|13.10s|0.72s|1.33s|
|8MiB|50.24s|102.45s|3.49s|7.18s|
|16MiB|98.84s|202.54s|6.82s|13.79s|

Observed retained traced allocations range 131,244–542,483 bytes; peaks 283,827–1,122,840 bytes. These values alone do not prove total-process memory bounds. The monitor saw a synthetic 8,192-byte burst against 4,096 bytes, with 4,096 bytes overshoot; it is not an OS hard quota. Ordinary matched comparison and all four supplemental workloads also completed; final independent measurement review is recorded below.

## Remaining gates

Forest's revision decision is pending. Live identity/application selection and file-size/retention choices also remain unanswered; elapsed time is not consent. Auth acceptance/implementation, reviewed initial provisioning and secret custody, installed/native-tool confinement, public ingress qualification, actual Cowork OAuth and exact binary materialization remain incomplete. Only a fully reviewed service and concrete activation configuration will be presented for final activation approval.

## Final independent measurement closeout

[Final measurement review](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-2/FINAL-MEASUREMENT-REVIEW.md) SHA256 0ad6ad01896961c875e4634a101a6f0946b35b93ac65b7866dcd719a5627477c; final readiness f3400cca106486da66bdcdae29721a95ac64417a35e83c9f10611ef62866c560; final manifest c8de880246b75efff27dd54e4f544015096e1ccfd9b13d2d10508279b421a3fd. All source and protected identities were rechecked unchanged after measurement.

| Ordinary workload | Original baseline median | Corrected candidate median |
|---|---:|---:|
| Explicit route | 0.096188 ms | 0.100125 ms |
| Synthetic S1 route | 0.615167 ms | 0.618875 ms |
| Context requirements | 0.004375 ms | 0.004292 ms |
| Job get | 0.027042 ms | 0.027291 ms |
| Job replay | 0.057417 ms | 0.055125 ms |

The unchanged direct-owner driver uses synthetic quota/scorer inputs; process/network calls remain zero and all counted calls match the baseline. These numbers do not measure real S1 inference. Historical prebuild labels in raw driver metadata are retained and explicitly qualified by the candidate source hashes.

Actual core supplemental medians: task create 15.09 ms, replay 3.47 ms, get 3.14 ms and delete 23.36 ms across 100 cycles; ASGI plus actual IPC task_get 3.86 ms across 100 calls. Four concurrent 1 MiB streams completed in 0.597 s. A 16 MiB PUT/GET measured 1,105/729 ms without the matrix's tracing overhead. Wrong-hash, idle, headroom and ENOSPC fault workloads completed with expected refusal; zero external connections were attempted. The previously approved private measurement fixture correction is explicitly recorded, not a product-timeout amendment.

Completed measurements do not grant efficiency acceptance to an incorrect candidate. Final status remains B failure / design_error, two valid failed builds and no same-design retry. The revised-design approval question is pending; no approval is inferred from elapsed time.
