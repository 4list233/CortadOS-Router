# Attempt 3 / revision 2 — LOCAL CORE handback

Status: implementation supplied; acceptance BLOCKED by host-process sandbox. This is not an accepted core, a host-suite pass, independent Rubric B, or activation approval. Group 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1; grant 62fcd07; parent v5; protected exam commit 3cda1fea79c60dd5e5e9d86a0e504591e37a4a68. Prior attempts remain unchanged.

## Confined implementation

Only six of the exact18 source files differ from the rejected attempt2: remote_http.py, remote_ipc.py, remote_runtime.py, remote_dispatch.py, remote_capacity.py, file_transfer.py (all in integrations/mcp). The other twelve source hashes match attempt2. No ordinary001–007 owner was changed in this revision.

- HTTP captures one entry time, uses one total/idle minimum for receive, IPC (including lock wait), sends and close; it renews idle only after acknowledged nonempty payload. All transfer response families use one conservative state set before attempting headers. Exhaustion aborts without new headers and closes the shared endpoint. A timely preheader timeout can return408 within the original positive budget. No fresh error/cleanup interval.
- IPC preencodes a bounded complete frame before writing. Primary validation and the single pre-write output_limit fallback consume one captured deadline. Current/opened/prior applicable lease deadlines constrain the response, including a lease retired by finish_put. Expired or indeterminate writes retire the endpoint; no second frame. Full immutable committed results are not truncated.
- CoreRuntime is the one operation-admission owner. Capacity-only and fatal reasons are separate, while admission_failed remains an aggregate compatibility diagnostic. Capacity observation/cancellation/cleanup use the exact table; forbidden replay and byte methods refuse before effects. Principal/disabled/fatal checks retain precedence. Observation integrity failures latch fatal. The monitor latches missed samples/unmeasurable usage/overruns without reopening and uses existing Broker.stop.
- Owned stream refusal retires the affected lease; existing cleanup works under capacity, fatal and disabled states. Repeated closed-lease close is effect-free. Unknown/foreign task/job pairs return the same job_not_found refusal.

Diffs: correction.patch is relative to the exact reconstructed attempt2 candidate; candidate.patch is cumulative against HEAD, including previously untracked source owners. source-review.json lists all18 terminal SHA256/line counts, protected identities and diff hashes. freeze.py reconstructs and verifies every attempt2 source hash before comparison; it does not change production behavior. Maximum source length remains414 (<500); all18 compile without pycache. git diff --check passes (tracked diff); cumulative new sources were compiled and bounded separately.

## Commands and raw results

All commands run from /Users/velox/.cortados-builds/cortados-router-repo. Final runs use umask022 and TMPDIR=$PWD/.cowork-core-evidence/attempt-3/tmp. The interpreter is /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python, always -B.

Core command: python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v
Final raw log: core-final-frozen.log. It collects111 methods, reports3 failure events/89 error events/0skips. Error/failure events overlap methods and cannot be subtracted to derive pass counts. Most runtime-dependent methods fail before effects at unchanged native_process.host_boot_id: PermissionError errno1, Cannot read host boot identity. Three assertions in crash-boundary/separate-process methods follow the same blocked startup. Raw BufferError finalization diagnostic is preserved. No native_process, fixture, security boundary or permission workaround was used.

Full driver: python -B acceptance/cowork_mcp/evidence/run-full-suite.py ABSOLUTE_NEW_EVIDENCE_DIRECTORY
full-suite/ is the first accumulated run during source review; full-terminal/ began before the final retained-lease deadline fix. Neither is terminal-source acceptance. The final source run and counts are recorded in TERMINAL-RESULTS.md. Every invocation remains preserved. All failed/blocked/empty lanes remain red.

Direct nonruntime core controls pass for ordinary argv/environment/bootstrap equality, complete-handoff seam/default routing, live original-prefix/protected-input guard, closed config/record/wire validation and preflight refusal. This does not establish the blocked runtime journeys.

## Per-AC checks

All rows require independent host rerun; no row is claimed fully accepted. Module names below are the protected test_*.py files under acceptance/cowork_mcp; exact methods are frozen in MANIFEST.json.

| AC | Implementation/source check | Protected verification | Result here |
|---|---|---|---|
| 00803 | Central exact-principal admission before replay and after locks; capacity table | schemas, tasks | Partial config controls pass; runtime BLOCKED |
| 00804 | Existing socketpair composition, endpoint-only retirement, unchanged ordinary owners | compatibility, transfer | Ordinary bytes controls pass; lifetime/process BLOCKED |
| 00805 | Single full-frame encoder, deadline before encoding, prewrite-only fallback | schemas, tasks, transfer | Closed schemas pass; AF_UNIX backpressure/partial frame BLOCKED |
| 00806 | Scoped selectors; uniform unknown/foreign job refusal; fatal observation checks | tasks, transfer, recovery | Runtime BLOCKED |
| 00807 | Capacity admission precedes replay; existing atomic accept/operation identities retained | tasks, recovery | Runtime BLOCKED |
| 00808 | Existing journal/workspace/store owners unchanged | tasks, recovery, native_policy | Runtime/crash boundaries BLOCKED |
| 00809 | Existing full-handoff and manifest/binding owners unchanged | native_policy, compatibility | Ordinary/default handoff controls pass; START/native binding BLOCKED |
| 00810 | Chunk-bounded frame/descriptor owners retained, shared endpoint retirement | transfer, compatibility, benchmark-core | Binary/process/memory measurement BLOCKED |
| 00811 | Ticket/offset/seal owners retained; deadline and committed-receipt inspection paths | transfer | Runtime BLOCKED |
| 00812 | Existing descriptor-relative workspace/artifact owners unchanged | recovery, native_policy | Runtime BLOCKED |
| 00813 | Existing ordered all-or-none publication retained | native_policy, recovery | Runtime BLOCKED |
| 00814 | One HTTP state/budget, strict exhausted abort, bounded cleanup and endpoint retirement | compatibility, transfer | Runtime response-family/deadline controls BLOCKED |
| 00815 | Existing lock-before-DB and ordered recovery retained; fatal and capacity separated | recovery, compatibility | Pre-DB invalid config control passes; recovery BLOCKED |
| 00816 | Existing accounting owner extended with capacity-only overrun/missed-sample latch and containment | performance, recovery, benchmark-core | Runtime boundaries/stop/measurements BLOCKED |

## Bounded implementation choices and limitations

Adopted existing named owners; no new module, dependency, policy constant, deadline default, grace, retry or relaunch. Chose preencoding once within the response budget to make post-write fallback structurally unavailable. Kept the old aggregate admission_failed diagnostic because the retained missed-sample exam observes it; distinct capacity_failed/fatal_failed reasons control admission. HTTP and IPC retain their distinct contract-owned lifetimes, not duplicate policy timers. Retained lease snapshots bound response delivery after successful seal/close. These are implementation representations within the approved behavior; no product-policy deviation or contract contradiction was selected.

Dependency resolution: existing QA interpreter imports the current MCP SDK and all source owners through protected discovery; no installation/auth extra. Metadata/native execution remain subject to unchanged security boundaries.

No new accepted-job cancellation owner: transport close never invokes Broker.stop. Capacity overrun still does; disabled owner does not implicitly cancel accepted work. Durable accepted outcomes, receipts and publication remain with the existing owners. Root must independently verify them on the host.

No final transfer matrix, RSS scaling, SQL workload, overshoot or native-policy acceptance can be inferred from blocked startup. Existing baseline samples are historical comparators, not candidate measurements. Full host suite and benchmark-core (all12 matrix keys plus monitor/ordinary/adverse workloads), per-AC review and all11 independent B rows remain required.

No auth/public Cowork/native-tool confinement/live deployment claim. No commit, reset, push, install, listener, credential, network, live-service or training/schedule change. Evidence is confined to attempt-3; attempts1/2 and protected inputs remain unchanged.

