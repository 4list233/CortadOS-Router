# RT-COWORK-MCP-008 v6 revision3 — cumulative attempt5

Builder handback only; completion is not acceptance. No commit, contract or exam edit.
Task group: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1.
Root grant supplied by dispatch: e3baa71. All four earlier failures remain historical.
No cortados-workers tools were exposed in this session; no delegated, nested, provider or network job was used.

## Inputs and confinement
Parent SHA256: 16ce4ca69cfd7e43684f7a76de43c386b277467f3d193491f9d64e69cfe1388f.
Execution SHA256: a6f93d4b51d050e0b7fca16cdd291b2bc844a81bd798ceba0ab3c3e815fb6750.
Manifest SHA256: 3a979fbaa015408dd1d84e5f1f564e933ec970633fc63bd05936726179855261.
All 20 tracked acceptance/cowork_mcp paths compare byte-identical to 8f3c8fe3f85b2f060562ad4c8b8b24de2921a2d6.
See protected-identities.json, source-identities.json (all18 before/after hashes and physical counts), and confined.diff (against inherited candidate4, not HEAD).
Only broker.py, task_store.py and remote_runtime.py differ from session entry.
Earlier uncommitted source files and evidence were retained. All18 source ASTs parse and each is below500 physical lines.
git diff --check exits0. Contracts and tests remain read-only.

## Owner disposition and bounded choices
Extend task_store.rows: construct the existing complete closed record before nested semantics; reject corrupt SQL Boolean domains, including binding active; normalize every causal reader Exception through runtime fatal. Preserve semantic policy_unavailable and ordinary absence outside this boundary.
Adopt remote_schemas constructors, strict_json, CapacityMonitor measurement/cadence and existing dispatch/IPC/transfer authorization checks.
Extend CoreRuntime: monitor completion observer consumes exceptions; authorize checks done synchronously before principal/capacity disposition. Owned teardown observes prior terminal loss before marking its own stop expected. The existing once-only fatal owner stays authoritative; sink failures are safe storage_error and remain reportable at teardown.
One runtime-owned containment continuation calls existing Broker.stop with capacity_exhausted independently of sink delivery. No watcher, restart, grace, reopening, retry or new schema/public API was added.
Extend Broker stop/enumeration so known workers still receive STOP after store/sink failure. Receipt absence retains known-worker ownership and makes shutdown retain runtime roots/lock/store instead of treating STOP intent as death proof. Existing watchdog and run_worker still own receipts and publication.
No new module/dependency or alternate policy owner. No auth, retention, operator, stdio registration or listener change.

## Verification and raw outcomes
Pinned command, from repository cwd, umask022 and TMPDIR=$PWD/.cowork-core-evidence/attempt-5/tmp:
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v

Final raw output final-core-2.log: 153 methods, 3 failure events, 143 error events, 0 skips; exit1; elapsed4.372s.
Counts include subtests and are not disjoint method counts. 18 method lines report ok.
Repeated startup cause is native_process.host_boot_id PermissionError errno1: Cannot read host boot identity, before Broker/store/runtime readiness.
Separate-process ready assertions consequently fail. No host pass is inferred. A finalization BufferError is retained verbatim.
Earlier core-tests.log contains inherited-candidate runs accidentally launched when shell here-doc creation was denied; revised-core.log/final-core.log are intermediate source observations. None is acceptance.
Initial here-document execution was denied even with the authorized TMPDIR; source edits and evidence capture used python -c afterward. No sandbox bypass.
Direct /usr/sbin/sysctl kern.boottime and /bin/ps -p $$ -o pid=,ppid= results are preserved in sysctl.log and ps.log.

## Per-AC observed ground truth
| AC | Observation / remaining status |
|---|---|
|03|Config/principal constructor controls run; runtime admission and post-wait/monitor-loss checks BLOCKED by boot identity.|
|04|Ordinary argv and handoff-seam controls pass; frontend/runtime shutdown and monitor containment BLOCKED.|
|05|Closed constructor, strict JSON, errors and wire examples pass; actual canonical reader and IPC pressure/lifetime cases BLOCKED.|
|06|Owner/task/job/public lookup and stream runtime cases BLOCKED.|
|07|Replay, acceptance transaction and rollback runtime cases BLOCKED.|
|08|Preparation/delete/delegate races BLOCKED.|
|09|Ordinary argv, handoff routing and worker discriminator controls pass; actual remote launch and generation cases BLOCKED.|
|10|Binary/separate-process/chunk transfer BLOCKED; separate-process startup refusal appears as an assertion failure.|
|11|Tickets, deadlines, offsets, lost ACK and descriptor outcomes BLOCKED.|
|12|Actual filesystem traversal/snapshot cases BLOCKED.|
|13|Publication/crash/native-state cases BLOCKED.|
|14|HTTP response budgets and stream revoke/cleanup cases BLOCKED.|
|15|Invalid-core-before-DB control passes; actual recovery, canonical-read and monitor lifecycle cases BLOCKED.|
|16|Capacity accounting, running-worker STOP/death proof, monitor-loss and durable inspection cases BLOCKED.|

AC01/02 excluded future auth. No AC is claimed accepted.
All 39 additive revision3 methods are discovered, but their runtime assertions remain unproven here.

## Deviations, missing lanes and limits
Environment blockage is not fixed or weakened: host boot identity, sysctl/ps remain read-only production checks.
No patched host identity, test skip, fake receipt, relaunch or broad acceptance reclassification was used.
Root must run full938-method/28-lane inventory and independent B, the unchanged benchmark matrix, ordinary/supplemental measurements, actual native qualification and source review.
Those full inventory/benchmark/host lanes were not run here; performance and efficiency acceptance are unproven. Historical measurements are not substituted.
Root's deferred index update remains outside builder scope. Live/public activation remains excluded.
