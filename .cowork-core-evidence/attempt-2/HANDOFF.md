# LOCAL CORE attempt-2 terminal handoff

Task group: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1. Same-design correction under latest ROOT-REVIEW; no delegation, commit, installation, listener, network, real native/model call or activation. This handoff is not acceptance. Source is terminal for independent root/QA review.

## Confinement and identity

Only eleven of the eighteen authorized source files changed from attempt 1. Existing route_task, codex, worker, remote_config, task_workspace, artifact_store and remote_native behavior was adopted; changes extend existing Broker/JobStore, schema, dispatch, ticket, descriptor, runtime, IPC, HTTP and capacity owners. No new owner or source path was introduced. Ordinary admission, routing, fresh quota and native process control remain with their existing owners.

source-review.json contains all eighteen full SHA-256 values and physical line counts (all below500), change disposition, compilation and protected identity results. correction.patch is relative to the independently frozen attempt-1 candidate; candidate.patch is cumulative relative to current HEAD. All eighteen attempt-1 source texts were reconstructed in memory from the preserved attempt-1 patch and verified against independent B-readiness source hashes before producing the correction.

- Correction patch SHA256: fbecf8c02286f2d54d424af112e811797c79e32ea601123614f5a3ad4be70a2f
- Cumulative candidate patch SHA256: 4726a8fc2d1769b0c48d052a7e3d60743a814a5a163695e18726a54cb0351700
- Current observed HEAD: 647fea73341beed45624732efc7fb992730f5873. No Git mutation was performed by this worker.
- Manifest SHA256 remains03589f6ad5394e4c5d3c29f37979f349f7fb4b1c7cd8b6671b0c55f94da49959.
-3normative,641prior protected and18current fixture identities checked: zero mismatches.
-18/18 sources compile in memory without pycache; git diff --check passes.
- Original .cowork-core-evidence/attempt-1 and independent prior failure/benchmark evidence remain untouched.

## Corrections and evidence

All test references below are unchanged protected exams. Diagnostic passes are explicitly synthetic and are NOT acceptance.

| Cluster | Existing-owner correction | Specific outcome / limit |
|---|---|---|
| C1 | remote_schemas projects artifacts directly from the committed publication manifest, preserving its order and immutable descriptors. CoreBroker validates committed membership before result projection. | BuildPublicationRegressionExam.test_committed_manifest_order_survives_reverse_artifact_query_order: private synthetic-worker diagnostic PASS. Original actual-fake-worker test remains host-blocked here. |
| C2 | CoreRuntime.authorize rechecks principal, enabled, ready and both admission latches after admission/task lock waits, after route work, and immediately before acceptance. Stream operations use the same authority check. | BuildLifecycleRegressionExam.test_disable_ahead_of_queued_create_rechecks_authority_after_lock: diagnostic PASS. |
| C3 | CoreClient catches in-flight cancellation, closes the indeterminate endpoint and rethrows cancellation. Sequence is never reused on that endpoint; no job cancellation or resend is introduced. Endpoint teardown retires its leases. | BuildLifecycleRegressionExam.test_cancelled_ipc_call_cannot_deliver_predecessor_response: diagnostic PASS. |
| C4 | transfer_tickets validates complete publication state and the entire ordered unique sealed manifest against owned artifact rows/blobs before output ticket issue, replay and claim. Task/result visibility uses publication authority; inputs keep their existing behavior. | BuildPublicationRegressionExam.test_sealed_output_is_not_downloadable_after_publication_failure and test_unpublished_output_rejects_existing_ticket_replay_and_claim: diagnostic PASS, including pending/failed fault states. |
| C5 | HTTP transfer receive, send, IPC work and close waits are bounded by a fixed total deadline and idle deadline renewed only by nonempty byte progress. Expired cleanup retires the endpoint instead of waiting beyond the transfer budget. Before-header timeout sends408 with bounded error delivery; after-header timeout aborts. Empty GET reads do not renew core idle. | Both BuildHttpDeadlineRegressionExam stalled PUT receive and stalled GET send methods: diagnostic PASS. No real network/backpressure benchmark claim. |
| C6 | task_store reads and compares binding/operation derived indexes rather than discarding them. Preparation path, identity and phase/job relationships are validated before recovery actions. Runtime classification checks discriminator, task/owner/group/policy, handoff/native binding, adopted preparation/version, delegate operation, publication and active-task index relationships; public job projection invokes this classification. Acceptance checks preparation/version/path/policy agreement. | BuildIntegrityRegressionExam.test_binding_derived_task_index_must_equal_immutable_seed: private synthetic-worker diagnostic PASS through restart. Other redundant-field directions were source-reviewed; independent exhaustive host fault coverage remains required. |
| C7 | task_store event boundary latches runtime and JobStore admission on any audit emission failure and retires all broker transfer descriptors/timers. Runtime emits one fixed nonrecursive storage_error event directly to stderr, without raw exception content; failed stderr is retained as fatal rather than retried. JobStore._save rolls back every failed remote transition and latches fatal authority. IPC no longer continues authority after storage failure. Remote containment reporting uses remote audit owner. | Four BuildIntegrityRegressionExam audit boundaries (job transition, acceptance, lease open, malformed frame): diagnostic PASS; rollback, refusal, one signal and empty descriptors asserted. |
| C8 | CapacityMonitor compares each sample start with the preceding sample start before measuring; missed cadence latches admission and uses existing Broker.stop for active remote jobs. It wakes before the hard next deadline (half remaining interval), adding no lateness tolerance. Measurement-duration deadline remains checked. | Missed250ms scheduling stall with50ms poll and timely control: diagnostic PASS. No invented overshoot/latency SLO; real writer/monitor measurements pending host. |
| C9 | NativeTemplate mcp_servers is bounded to zero. All omitted declared private array maxima restored (manifest lists/captures, seed/worker inputs, object IDs, publication entries, capacity tasks). | Unchanged test_schemas:14/14 PASS without monkeypatch, including BuildClosedShapeRegressionExam exact array constraints and constructor/immutability controls. |
| C10 | file_transfer closes the owned FD even if fstat/identity cleanup fails, retires lease/timer bookkeeping in finally and preserves unknown named extents/promises. Runtime fatal cleanup retires every remaining transfer FD/timer; endpoint finally always closes its socket. Shutdown inspects session errors, drains leases before broker/store/root descriptors and lifetime lock, retains ownership on unresolved live workers, and continues cleanup before propagating fatal-report failure. Broker core shutdown attempts all owned jobs despite earlier audit errors using its existing STOP seam. | Two-lease inode replacement cleanup method: diagnostic PASS; every captured FD closed, unknown replacement retained, fatal signal surfaced. Host process-containment failure variants remain independent work. |

Runtime owner now durably emits ready, disabled and closed transitions; repeated disable is a same-state no-op. Existing ready/disabled transition-count test passed in the diagnostic lane.

## Exact commands and raw outcomes

All test commands used umask022 and TMPDIR=$PWD/.cowork-core-evidence/attempt-2/tmp. Interpreter for exams: /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python.

1. python -B -m unittest discover -s acceptance/cowork_mcp -t . -v
   Raw: core.log.89methods,3failures,67errors,0skips;19passes. This was run once before terminal edits. Runtime startup traces show PermissionError errno1 in native_process.host_boot_id's host sysctl observation. The three failed child-readiness assertions also report refused storage_error startup. No green/full-core claim. A final BytesIO BufferError diagnostic is preserved. The known blocked full suite was not repeatedly run.

2. python -B -m unittest acceptance.cowork_mcp.test_schemas -v
   Raw terminal schema check: schemas-final.log.14methods,0failures/errors/skips. Subsequent changes did not alter the schemas. No monkeypatch.

3. python -B .cowork-core-evidence/attempt-2/diagnostic.py
   Raw final relevant run: diagnostic-frozen.log (12methods PASS); diagnostic-terminal.log also preserves the HTTP cleanup check run. The runner substitutes ONLY native_process.host_boot_id with a synthetic value, and executes selected unchanged lifecycle/audit/monitor/cleanup/HTTP exams. No host identity/process-containment or acceptance conclusion follows.

4. python -B .cowork-core-evidence/attempt-2/publication-diagnostic.py
   Raw terminal run: publication-terminal.log.4methods PASS. Runner substitutes synthetic boot identity AND Broker.run_worker with an explicitly synthetic completion that writes private fixture output bytes and invokes real acceptance/store/snapshot/publication/IPC owners. It does not execute a native worker or prove containment. Covers three BuildPublicationRegressionExam methods plus binding-index restart.

5. python3 -B .cowork-core-evidence/attempt-2/freeze.py
   Raw: freeze-terminal.log; source-review.json.18/18 in-memory compilation, source caps and protected hashes verified. git diff --check separately passed.

Private diagnostic setup mistakes are retained: diagnostic.log attempted to patch a nonexistent broker-local host_boot_id (the actual import is local from native_process); publication-diagnostic.log used captured_job.result=null and therefore had3diagnostic fixture errors. Corrected private runner supplies a synthetic typed result. No product behavior or protected test was changed to imitate those faults. Initial identity script used wrong fixture-relative prefix; corrected scan is explicitly recorded in source-review.json. An initial shell here-document was denied before edits; source scripts thereafter used quoted python -c, without widening permissions.

## Boundaries, deviations and remaining verification

No protected exam, fixture, contract, Auth009 source, excluded production path or existing config/schema/native_process/native_profile was edited. No dependency install, permission broadening, API fallback, recursive worker or real provider call was used. Evidence writes are confined to attempt-2. The intentionally modified private diagnostic dependencies are documented above and never counted as acceptance.

The unchanged89-core host suite,874-method/28-lane accumulated host driver and corrected full performance matrix have NOT passed in this worker. The full driver and expensive benchmarks were not run because root explicitly owns the host rerun after terminal freeze and the native observation/external fixture boundaries are known blocked here. The exact protected driver is acceptance/cowork_mcp/evidence/run-full-suite.py, and benchmark is acceptance/cowork_mcp/evidence/benchmark-core.py, invoked by root in its authorized host environment.

All ten owner corrections are implemented, but selective diagnostic success is not proof of full correctness, efficiency, power-loss durability or native isolation. Independent root/QA must review these exact frozen bytes, all failure paths and full host outcomes. Auth, live identity/storage policy, actual exposed-native-tool/document-runtime confinement and Cowork activation remain excluded. If independent review establishes a second valid failed build, return design_error under the existing group; do not start another retry or rename the package.
