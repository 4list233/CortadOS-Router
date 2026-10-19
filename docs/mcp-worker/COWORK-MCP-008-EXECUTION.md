<!-- PURPOSE: Allocate nonoverlapping future execution responsibilities and independent gates.
     CONTRACT: RT-COWORK-MCP-008 v6; draft, not a dispatch authorization.
     DEPENDS: Parent contract, recorded root policies, independent protected fixtures.
     CEILING: This session writes documents only; no production, exams, commits or live actions. -->
# Cowork 008 execution packages

```yaml
contract_id: RT-COWORK-MCP-008
contract_version: 6
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
status: draft
dispatchable: false
owner: Forest
parent: COWORK-MCP-008.md
attempt_ledger: inherited; see DECISIONS evidence ledger (never reset)
```

This is one coherent LOCAL CORE build; FUTURE AUTH and public activation are excluded. The named module
responsibilities below are internal implementation boundaries, not permission for different workers
to edit the same seams. Package labels retain parent/group identity; never reset failed-attempt count
by renaming package/model. Current task author writes only these three documents. The proposed future
paths are not current write grants. Public endpoint and actual Cowork binary acceptance occur AFTER
local implementation; they are not prebuild requirements.

## Package A — contract/root preparation

Owner: root orchestrator with Forest for policies; author role produces proposals, never self-approval.
Current exact scope_paths:
- /Users/velox/.cortados-builds/cowork-core-contract-revision-3/COWORK-MCP-008.md
- /Users/velox/.cortados-builds/cowork-core-contract-revision-3/COWORK-MCP-008-DECISIONS.md
- /Users/velox/.cortados-builds/cowork-core-contract-revision-3/COWORK-MCP-008-EXECUTION.md

Required inputs before LOCAL CORE dispatch: pinned v6 parent/source hashes, exact reviewed scopes,
synthetic mandatory config fixtures, protected exam commit/manifest, independent D then A and root
package approval. No D/A pass is declared. Settled identity, limits and retention choices are recorded in DECISIONS' current v6 record; concrete qualification/activation gates remain and MUST NOT block parameterized local enforcement.
Auth implementation needs its own future reviewed package; it MUST NOT be smuggled into core scope.
No actual Cowork connection is required for local fixtures.

Standards/rubric and governing file paths are in parent section 9. Source checkout is absolute
/Users/velox/.cortados-builds/cortados-router-repo, inspected HEAD
e055c51 (full v3 review pin). Historical source anchors remain in parent. Before dispatch root records current per-file SHA-256 and
working diff, since HEAD alone cannot identify concurrent edits.

## Package B — one independently gateable LOCAL CORE builder

One accountable builder owns ALL source changes below, including shared seams. Suggested selection:
S1 with full contract/evidence, or explicit supported integration model per project routing policy.
Do not dispatch now. With cortados-workers exposed, discover schemas, call context_requirements, answer
using parent evidence and list unresolved gates; then route/delegate with the SAME task_group_id, absolute
cwd, stable request_id for intended attempt and timeout ≤ configured max. Routing is not delegation.
No network permission, MCP grants, API billing fallback, provider registration, public ingress or live
state access. Inspect job_status before resubmission; no automatic failed-job retry. Retrieve job_result,
review diff/evidence and record job_feedback accepted/rejected/inconclusive. Worker completion is not QA.

Retained exact future scope_paths (all existing in candidate4):
- cortados_router/route_task.py — keyword-only full-handoff seam only
- integrations/mcp/broker.py
- integrations/mcp/job_store.py
- integrations/mcp/codex.py
- integrations/mcp/worker.py
- integrations/mcp/remote_config.py — existing candidate owner
- integrations/mcp/remote_schemas.py — existing candidate owner
- integrations/mcp/remote_dispatch.py — existing candidate owner
- integrations/mcp/remote_http.py — existing candidate owner
- integrations/mcp/task_store.py — existing candidate owner
- integrations/mcp/task_workspace.py — existing candidate owner
- integrations/mcp/artifact_store.py — existing candidate owner
- integrations/mcp/file_transfer.py — existing candidate owner
- integrations/mcp/transfer_tickets.py — existing candidate owner
- integrations/mcp/remote_native.py — existing candidate owner
- integrations/mcp/remote_runtime.py — existing candidate owner
- integrations/mcp/remote_ipc.py — existing candidate owner
- integrations/mcp/remote_capacity.py — existing candidate owner

No wildcard scopes. Parents integrations/mcp and docs/mcp-worker exist; all 18 source paths now exist in the frozen rejected candidate. This is the retained exact future source ceiling, not an author write grant. Every new source file MUST stay below 500 physical lines and have
PURPOSE/CONTRACT/DEPENDS/CEILING headers. Historical prebuild broker/job_store/worker counts were 342/265/262 lines;
keep integration there to seams, not new complete owners. If split needs another path, root amends and
QA rechecks scope before builder writes it. No unreviewed utilities module or copied business rules.
Existing config.py/schemas.py/native_profile.py/native_process.py/router/quota code are read-only;
extend through explicit remote composition, not changing local schemas or fresh-quota algorithms.

<!-- API-BEGIN -->
| Sole owner | Exact callable / return |
|---|---|
| remote_config | load_core_config(raw:bytes,base:BaseConfig)->CoreConfig |
| remote_schemas | validate_remote(tool:str,args:object,config:CoreConfig)->RemoteArgs; remote_tool_schema(tool:str)->JSONSchema; project_remote_result(record:ProjectionInput)->WireResult; RemoteError(code:ErrorCode,message:str); from_worker_error(error:WorkerError)->RemoteError; emit_remote_event(tx:Transaction-or-None,event:RemoteEvent)->None |
| remote_native | load_manifest(raw:bytes,base:BaseConfig,policy:Policy)->NativeManifest; build_handoff(arguments:LocalArgs,inputs:tuple[InputEntry,...],limit_bytes:int)->Handoff; verify_binding(binding:WorkerBinding,manifest:NativeManifest)->None; native_arguments(binding:WorkerBinding,base_args:tuple[str,...])->tuple[str,...] |
| remote_runtime | CoreRuntime(*,base_raw:bytes,core_raw:bytes,manifest_raw:bytes); async __aenter__()->CoreRuntime; async __aexit__(exc_type,exc,tb)->None; async attach(server_socket:socket.socket,principal:Principal)->Session; async disable_owner(owner_id:str)->None |
| broker | Broker(config:BaseConfig,digest:str,*,core_runtime:CoreRuntime-or-None=None); async run_worker(accepted:JobRecord)->None; async stop(job_id:str,reason:str)->JobRecord |
| remote_dispatch | CoreBroker(runtime:CoreRuntime); async dispatch_remote(principal:Principal,tool:str,args:object)->WireResult |
| remote_ipc | CoreClient(socket:socket.socket,*,principal:Principal,config_digest:str,limits:CoreLimits); async bind()->ConnectionResult; async call(tool:str,args:RemoteArgs)->WireResult; async stream(op:str,args:StreamArgs,body:bytes=b'')->FrameResult; async close()->None; async serve_endpoint(socket:socket.socket,core:CoreBroker,principal:Principal)->None |
| remote_http | create_test_app(*,core_client:CoreClient,principal:Principal,config:FrontendConfig)->ASGIApp |
| route_task | route_task(arguments:LocalArgs,config:BaseConfig,metadata:NativeMetadata,*,handoff_packet:str-or-None=None)->LocalRoute |
| JobStore | transaction()->ContextManager[Transaction]; accept(arguments:LocalArgs,route:LocalRoute,*,cli_version:str,acceptance_seed:AcceptanceSeed-or-None=None)->tuple[JobRecord,bool] |
| task_store | migrate(db:Connection)->None; create_intent(tx:Transaction,principal:Principal,args:Create,snapshot:PolicySnapshot)->TaskRow; get_owned(tx:Transaction,owner_id:str,task_id:str)->TaskRow; operation(tx:Transaction,owner_id:str,operation:str,request_id:str,argument_hash:str)->OperationRow-or-None; preparation_intent(tx:Transaction,task:TaskRow,request_id:str,argument_hash:str)->PreparationRow; preparation_transition(tx:Transaction,preparation_id:str,expected_phase:str,next_phase:str,*,workspace_identity:WorkspaceIdentity-or-None=None,job_id:str-or-None=None)->PreparationRow; accept_binding(tx:Transaction,job_id:str,seed:AcceptanceSeed)->RemoteBinding; transition(tx:Transaction,owner_id:str,task_id:str,expected_version:int,next_state:str)->TaskRow |
| task_workspace | prepare_empty(task:TaskRow,intent:PreparationRow)->WorkspaceIdentity; remove_preparation(task:TaskRow,intent:PreparationRow)->None; prepare(task:TaskRow,binding:RemoteBinding)->WorkspaceIdentity; remove(task:TaskRow)->DeleteProgress; reconcile(intent:OperationRow)->RecoveryResult |
| artifact_store | open_stage(intent:ArtifactRow)->int; seal(fd:int,intent:ArtifactRow)->BlobIdentity; open_blob(row:ArtifactRow)->int; snapshot(binding:RemoteBinding)->tuple[OutputEntry,...] |
| transfer_tickets | issue(tx:Transaction,principal:Principal,args:Upload-or-Download,direction:str)->TicketRow; claim(tx:Transaction,ticket_id:str,capability:str,direction:str,connection_id:str)->Lease; revoke(tx:Transaction,owner_id:str,task_id:str-or-None)->tuple[str,...] |
| file_transfer | put_chunk(lease:Lease,offset:int,body:bytes)->int; finish_put(lease:Lease)->Receipt; read_chunk(lease:Lease,offset:int,max_bytes:int)->ReadResult; close(lease:Lease,reason:str)->None |
| remote_capacity | CapacityMonitor(runtime:CoreRuntime); measure()->CapacityUsage; reserve(tx:Transaction,owner_id:str,task_id:str,operation:str,request_id:str,kind:str,bytes:int)->ReservationRow; async run()->None; async close()->None |
<!-- API-END -->

[FIXED] Exact scope rationale: broker.py:133–189 owns dispatch/admission/IPC;
job_store.py:26–162 owns DB version and acceptance transaction; worker.py:31,91 and
codex.py:42 own bootstrap/argv. These are extended only at parent sections 2–4's typed seams.
server.py:20,58 and __main__.py:16 are read-only reuse evidence: core consumes schema/SDK patterns,
but MUST NOT change stdio registration or add a remote entrypoint. pyproject.toml stays read-only:
core uses existing dependencies, no auth extra. New modules' parent integrations/mcp exists.
No alternate route/queue/schema owner is authorized. All new interface records are defined in parent
sections 2–4; any missing observable semantic MUST return to root for amendment, never builder discretion.
Contracts have no line cap under current project AGENTS; each source module MUST remain <500 lines.

Private transport MUST use parent sections 2–4's closed operation table and trusted socketpair construction.
It MUST NOT extend stdio's JSON protocol with a caller-selected owner/channel. No listener or socket
path is installed. The broker owns every staging/blob descriptor across the entire lease; HTTP
receives bounded raw bytes, never an integer pretending to transfer an FD across processes.

Execution order inside B: independent gates → definitions/config → CoreRuntime lock/recovery → additive store/atomic acceptance → workspace/artifact/ticket/capacity owners → native bootstrap → CoreClient/ASGI composition → compatibility review.
Do not commit intermediate protocol assumptions; any contradiction returns to root and independent QA.
B's excluded paths: all tests/acceptance/exam fixtures, this contract, rulings, model catalogs, nightly
training, installed runtime, user homes/credentials, other worktrees, ingress/launchd/activation.
Builder can run independently authorized local exams later; this author session does not run them.

Handback: confined diff, line counts, dependency resolution evidence, exact AC-by-AC results and commands,
input/source/fixture hashes, all suite membership and missing suites, native-policy capture references,
baseline/candidate measurements, residual risks. No live install or commit implied by this package.

## Package Q — independent designated QA

Owner: designated independent MCP QA session, not B or this author. QA owns D/A/B and protected exams,
including separately blocked future-auth qualification already underway. No exam files authored in this session.
Retained exact acceptance_paths under existing acceptance/cowork_mcp (89 methods is a historical baseline; current114 are preserved, not v6 acceptance):
- acceptance/cowork_mcp/__init__.py
- acceptance/cowork_mcp/support.py
- acceptance/cowork_mcp/README.md
- acceptance/cowork_mcp/MANIFEST.json
- acceptance/cowork_mcp/test_schemas.py
- acceptance/cowork_mcp/test_tasks.py
- acceptance/cowork_mcp/test_transfer.py
- acceptance/cowork_mcp/test_recovery.py
- acceptance/cowork_mcp/test_native_policy.py
- acceptance/cowork_mcp/test_compatibility.py
- acceptance/cowork_mcp/test_performance.py
- acceptance/cowork_mcp/fixtures/wire.json
- acceptance/cowork_mcp/fixtures/binary.bin
- acceptance/cowork_mcp/fixtures/native_capture.json
- acceptance/cowork_mcp/evidence/run-full-suite.py
- acceptance/cowork_mcp/evidence/benchmark-core.py
- acceptance/cowork_mcp/evidence/suite-inventory.json
- acceptance/cowork_mcp/evidence/baseline-ordinary.json
- acceptance/cowork_mcp/evidence/historical-007-parity.py

These paths are independently owned protected evidence; required revision-2 exam amendments remain pending, not passed tests. QA commits with Exam-Owner attribution and pins hashes
BEFORE B dispatch. Any needed additional fixture path requires recorded scope amendment. Native capture
file is initially captured shell evidence plus explicit actual-exec pending marker, never synthetic proof.
QA authors deterministic fake-native cases independently; actual native/tool lane is gated separately.

<!-- AC-BEGIN -->
| ID | Canonical acceptance text |
|---|---|
| AC-008-03 | Synthetic config/principal admission refuses absent, foreign, disabled and invalid policy inputs before effects; existing explicit/S1 fresh quota refusals cause zero native starts. Capacity-only attach/bind accepts only the exact enabled trusted principal/config for the operation table; denied new work/replays have zero new admission/resource effect. |
| AC-008-04 | Socketpair-only construction creates no listener/auth/entrypoint; frontend loss leaves one accepted job; ordinary001–007 bytes and history remain unchanged. |
| AC-008-05 | Every closed wire/private record accepts its valid example and rejects its invalid example; safe Error/audit and full-header overflow preserve canonical rows. Primary/fallback socket backpressure and validation consume one IPC deadline; partial frames retire the endpoint without a second frame. |
| AC-008-06 | Owner/task predicates reject foreign and ordinary stdio IDs identically to unknown IDs across status, result, cancel, feedback and transfer. |
| AC-008-07 | When operation admission permits replay, concurrent replay returns one immutable identity with current projection; changed arguments conflict; job, binding, operation and reservation either all commit or all roll back. Replay through denied capacity-only methods returns capacity_exhausted; permitted task/job inspection retains accepted identity. |
| AC-008-08 | Create/delete/delegate/upload/publication races preserve one active task job and exact selected input copies; creating-delete and failed preparation have observable rows. |
| AC-008-09 | Bound manifest, triple discriminator and complete handoff bytes agree at route, acceptance and START; deletion or mutation refuses launch; ordinary argv/bootstrap remain byte-identical. |
| AC-008-10 | Separate frontend/broker processes transfer zero, NUL and non-UTF8 bytes with matching hashes and chunk-bounded memory through the exact CoreClient API. |
| AC-008-11 | Independent ticket encoding, frame lengths, offsets, length/hash checks, renewal, expiry, key rotation and lost ACK produce the specified row, lease and descriptor outcomes. Lost committed PUT receipt is recovered by task_get without consumed-byte retry; empty chunks never renew idle. |
| AC-008-12 | Descriptor-relative traversal rejects links, rename races, directories and nonregular outputs without leaking broker/native evidence or exporting mutable files. |
| AC-008-13 | Ordered all-or-none publication preserves native outcome, partial flags and immutable blob hashes through crashes and later workspace mutation. |
| AC-008-14 | Full GET retries preserve bytes; Range refuses; trusted active disable/delete closes open leases; expiry affects new opens only; no accepted job is implicitly cancelled. Timely PUT201/GET200/Range416/error controls pass; stalled start/body across every response family obey one total/idle budget, no second start, positive-budget-only408 and exhausted-budget abort without headers. Exhausted close retires the shared endpoint without sequence confusion or job cancellation. |
| AC-008-15 | Lock-before-DB construction, second-owner refusal, one ordered recovery, corruption refusal and crash boundaries preserve rows, tombstones and containment without relaunch. |
| AC-008-16 | st_size, promised-write and free-space equalities pass at the boundary and refuse beyond it; monitor overrun retains reservations until proof; no retention scheduler exists. Controlled missed sampling with a fake worker proves stop and durable cancelled/capacity_exhausted inspection through task_get/job_status/job_result; job_cancel stays idempotent. Every denied table operation has zero new task/job/ticket/reservation row or admission/resource effect; close cleans up; fatal corruption/audit and disabled owner override limited access; no automatic reopening/relaunch occurs. |
<!-- AC-END -->

Commands below are FUTURE QA instructions, not a record of runs. QA must pin the interpreter/venv and
fixtures before using them. Use a temporary isolated state directory under an authorized build tree;
never production state. No live credentials are needed for deterministic lanes.

```sh
git rev-parse HEAD
git diff --stat
rg --files tests acceptance
python3 -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest discover -s acceptance/mcp_worker -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/native_profile -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/mcp_learning -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/quota_evaluation -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/fresh_quota -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/quota_sources -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/training_teacher -t . -p 'test_*.py'
python3 -m unittest discover -s acceptance/router_followup -t . -p 'test_*.py'
```

This command inventory is a starting point, not proof that unittest discovers every maintained test.
QA must inspect test entrypoints/decorators and existing QA-BASELINE/001–007 reports, add exact pytest,
standalone, plugin, adapter and package commands where required, and record discovered/executed/skip/
blocked counts. A zero-test invocation is BLOCKED. In particular native_profile fixtures and version
capture entrypoints need their README commands, not a claim that unittest alone runs native capture.
Use existing maintained suite runner if its inventory is independently verified; no tolerated failures
masquerading as all-suite pass. Re-run at final diff, classify baseline failures without weakening them.
No tests have been run by this contract author.

Performance plan: same pinned interpreter/dependencies/host, baseline 4ed7508 vs final diff, raw samples,
fixture hashes and cold/warm labels. Measure existing stdio context/route with deterministic scorer/
quota captures (100 repetitions each), replay/status (1000 stored jobs, 1000 lookups), create/delete
(100 tasks), 0/1 KiB/1 MiB/approved max file PUT and GET (10 samples each), concurrent transfer at limit,
wrong hash/slow peer/disk-full scenarios. Report p50/p95/max, RSS growth, bytes and SQLite query count.
Assert no model/provider calls on replay/refusal; transfer memory must scale with chunk×active streams,
not file size. Measure synthetic ASGI/private IPC overhead without a listener; future-auth overhead is separate.
Measure live GitHub/network/model latency separately and never subtract it into a fake local pass.
No performance threshold invented here: root/QA pins measured latency/RSS/disk-headroom budgets before
B acceptance; unmeasured is pending, not a pass. Client 10s/30s constraints remain external deadlines.

## Package R — root activation and documentation

No production source ownership; no overlap with B/Q. Deferred exact documentation scope:
- docs/mcp-worker/INDEX.md
- docs/mcp-worker/INSTALLATION.md
- docs/mcp-worker/USAGE.md
- docs/mcp-worker/CLAUDE-APP-CONNECTOR-PLAN.md

Root adds RT008 ownership/entrypoint links before declaring integration complete. Current author
cannot edit those files. Live service/ingress/credential paths are intentionally NOT granted here;
root records exact activation/rollback scope from operator evidence in a separately approved package.
Never infer private 443 replacement from candidate public 8443.

Activation prerequisites: independently accepted LOCAL CORE and separately reviewed FUTURE AUTH, complete suite inventory,
captured actual native exec policy, qualified maintained auth/refresh and committed registration/file
policy records; resolved package/dependency lock and disk/headroom budgets. Then root owns public route,
callback registration and real Cowork binary journey. If Cowork cannot send ticket headers or materialize
binary downloads, outcome is BLOCKED, not text-only success. A companion/browser workaround changes
workflow and returns to root/Forest; do not silently implement one. No production/test/activation
action is authorized by this contract-authoring turn.

Independent Rubric B reviews actual implementation and full evidence. Repeated valid failed attempts
follow governing two-failure design-error escalation; missing fixtures, credentials, native canary or
environment are blocked, not valid failures. Final acceptance/activation is recorded by root/QA only.

## Recorded baseline supplement received during authoring

Root's COWORK-MCP-008-ROOT-REVIEW.md records the existing accumulated runner:
```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/baseline-4ed7508
```
This is a historical command, NOT rerun by this author. Result: 785 tests, 784 pass, 1 failure,
0 errors/skips, 26 lanes including two benchmarks. Not green. The failure is 007 installed-parity
whole labels.jsonl hash while separately authorized nightly appends labels; no 008 source change caused it.

QA's historical-007 fixture plan preserves original assertions/AST and original label prefix/hash with
three recorded literal path relocations. It is historical compatibility, not current candidate parity.
Do not edit protected 007 tests, freeze live training or relabel the failure as a pass. Candidate adds
ordinary-profile equality plus separate original-label-prefix/exam-provenance/live partition checks.
Use the actual 26-lane runner inventory and its environment bindings alongside new 008 lane commands;
the generic discovery list above alone is insufficient. Missing/unexecuted suites remain BLOCKED.

## v4 package-specific D/A inputs and AC mapping

[FIXED] Q MUST freeze runnable isolated commands/fixtures before A; this is a draft inventory, not exams.
Q owns the exact acceptance paths listed above; test_auth.py is reserved for a later scope amendment,
not this package. No current turn may create/run tests. Future Q command:
`python3 -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py'`
MUST run every new core lane with zero unexpected skips. Existing accumulated runner remains required.

| Exact Q file | Additional v4 obligations / ground truth |
|---|---|
| test_schemas.py | AC-008-03,05: every config key absent/unknown/type error refuses before side effects; every section 8 success/error example validates; no caller identity/path; complete config snapshot retained |
| test_tasks.py | AC-008-06–08: two synthetic principals and clients; SQL ownership/FKs/unique active reservation; atomic job acceptance rollback; explicit delete tombstone/evidence preserved |
| test_transfer.py | AC-008-10–14: separate frontend/broker processes over socketpair; NUL/non-UTF8/empty bytes; exact offsets and frame lengths; foreign lease, disconnect, backpressure and revoke; inspect broker descriptor close and hash-equal sealed files |
| test_recovery.py | AC-008-07,08,13,15: crash before/after intent/fsync/rename/commit; no relaunch; restart revokes leases; corrupt remote schema refuses; stdio schema unchanged; publication all-or-none |
| test_native_policy.py | AC-008-09,16: fake-native bootstrap/policy identity and watchdog stop on measured overrun; no actual model call in deterministic lane; missing policy refuses START |
| test_compatibility.py | AC-008-04,09,15: ordinary argv/bootstrap/results/hashes unchanged; no remote CLI/global app/listener/auth dependency; no training modification; historical/live checks distinct |
| test_performance.py | AC-008-10,11,14,16: bounded chunk/stream memory, deadline/backpressure, disk accounting/reservation copies, measured poll overshoot; explicit fixture budgets |
| README.md / MANIFEST.json | Q-owned exact fixture/source hashes, commands, suite inventory, independent D/A record and missing lanes; no author pass |
| fixtures/wire.json / binary.bin / native_capture.json | Closed valid/invalid examples, raw synthetic bytes, attributed native evidence and unresolved actual-tool negatives; no credentials |

Independent core acceptance MUST NOT claim AC-008-01/02 or public activation passed. Core AC-008-03/04
covers ownership/lifetime/composition only; future auth still owns real identity/credential checks.
Exact future-auth source candidates (NOT core grants): integrations/mcp/remote_auth.py,
integrations/mcp/remote_http.py, integrations/mcp/__main__.py, integrations/mcp/server.py, pyproject.toml.
Shared remote_http ownership MUST transfer only after core acceptance and a separately approved scope;
no simultaneous builders. No future-auth path may be written under this core package.

No-public-activation gates remain: qualification of the settled identity/application and limits/retention choices in DECISIONS, remaining downstream-client and operator details; independent auth D/A with qualified
Authlib hooks/locking; dependency lock; actual native tool confinement; measured disk/headroom;
exact ingress/callback/rollback scope preserving private 443→8432; actual Cowork byte access,
Authorization-header egress, approved destination and materialized PDF/DOCX/XLSX/non-UTF8 hashes.
No listener, auth registration or live storage may activate until all relevant gates are recorded.

Latest evidence read during v3: native-exec-capture-v2/independent-adjudication.json qualifies thirteen
synthetic shell outcomes for job ba4c3e14-c1f7-47fb-93b6-d703a98a6db7 with a recorded unused dot argument.
The strict exact-command comparison remains false. Parent §9 records hashes/limits; Q MUST preserve
that deviation and original capture, not require a redundant model run or call this full native approval.

## Historical v4 technical scope and stop condition

Historically, root authorized the three then-NEW source modules remote_runtime.py, remote_ipc.py and remote_capacity.py and the two then-NEW exam helper paths __init__.py and support.py as future grants only. Current author scope remains three docs. remote_runtime owns lifetime; remote_ipc owns the only frame codec; remote_capacity owns usage observations/reservation arithmetic, never another DB writer. file_transfer owns descriptors/bytes, not framing. remote_dispatch consumes runtime services, not an already-open external broker.

Root grants the exact keyword-only handoff_packet seam in cortados_router/route_task.py. Existing route_task owns the complete-packet budget and all input-fit calls, preserving ordinary None behavior. Independent QA pins ordinary and full-remote fixtures before B. schemas.py and task_evidence.py remain read-only. No output-counter patch, duplicate resolver or task/context rewrite substitutes for this seam. The preparation journal and JobStore.transaction extension in parent sections 2–4 belong to this same future source scope.

AC-008-03 through AC-008-16 all run through the pinned future command: python3 -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py'. The file mapping above allocates lanes; it is not a second AC wording authority. Missing fixtures/commands with zero tests remain pending A. AC-008-01/02 are excluded future-auth gates.

Root's deferred INDEX.md ownership update is required before integration acceptance. This author does not edit it. Authoring is not a valid production build attempt; the existing ledger is unchanged.

Historical v4 author evidence/closure map and self-rubric remain in DECISIONS. That author ran document JSON/schema/API/AC/diff checks only, not acceptance tests. The ordinary bootstrap capture is now recorded in parent section 9. Independent diagrams/fixtures/new-core measurement gates remain open.

## Root pre-review amendment evidence

Root approved the full-packet seam and private workspace-preparation journal without choosing live identity/storage policy. The independent ordinary-bootstrap capture closes only the old envelope-sample gap. Existing-owner baseline at 4ed7508: 1000 stored jobs, 3000 get and 3000 replay lookups, each one indexed SELECT; median get 0.027042 ms and replay 0.057417 ms. Explicit route median 0.096188 ms, synthetic S1 route 0.615167 ms and context 0.004375 ms. Report SHA256 6e487a4a0ebd45ceef9b98e2fadd6a14e71c0e3739a29057240e0dbe9235deef; 75-file inventory SHA256 c6b9fc5b4bc2a93cf4cc7ae9d154ae0a3239b520d75bfd7f6b6f0dd2afbfb077. Both are under private cowork-mcp-verification/core-baseline. These measurements do not assert new-core or transfer memory/overshoot performance. Historical-007 unchanged assertions pass 4/4 with three recorded literal path relocations and equal normalized AST; original live baseline remains 784/785. Q must preserve the distinction and separately test candidate compatibility and live label-prefix/exam/provenance.

Independent QA command correction: relative package imports require `-t .`. The authoritative core command is `/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v`. This changes invocation only; AC text and production semantics remain unchanged. The measured 100-artifact listing bound is the representative fixture size, not a production cap; each task retains its mandatory configured artifacts_per_task.

## Retained revision-2 execution boundary — [FIXED]

Parent section 4 owns response lifetimes and capacity admission. Callable signatures and wire schemas remain unchanged; all existing entrypoints apply its exact table. For reviewer parity the table is copied verbatim:

<!-- ADMISSION-BEGIN -->
| Operation | Capacity-only latch | Fatal integrity/audit/storage latch | Disabled owner |
|---|---|---|---|
| attach/bind of exact trusted principal/config | Allow endpoint for limited operations below; no new authority | Refuse ordinary tool/stream authority; cleanup still mandatory | Refuse |
| task_get | Allow ordinary owned-task projection; no transfer capability and no mutation/repair | Refuse storage_error | Refuse permission_refused |
| job_status / job_result | Allow only after normal owner/task/job/binding/publication validation; expose actual durable native state/reason | Refuse storage_error | Refuse permission_refused |
| job_cancel | Allow existing owned job only, normal idempotent stop intent and existing containment; never create/restart | Refuse public call; internal shutdown/containment must still attempt cleanup | Refuse public call; existing internal lifetime rules unchanged |
| job_feedback | Refuse capacity_exhausted; append-only feedback may be submitted after explicit qualified restart | Refuse storage_error | Refuse permission_refused |
| task_delete | Refuse capacity_exhausted; no speculative cleanup when usage sample is untrusted | Refuse storage_error | Refuse permission_refused |
| context_requirements | Refuse capacity_exhausted; not required for existing-attempt inspection | Refuse storage_error | Refuse permission_refused |
| task_create / route / delegate | Refuse capacity_exhausted, including replay invocation through these methods; inspect accepted identity with task_get/job methods | Refuse storage_error | Refuse permission_refused |
| file_upload_ticket / file_download_ticket | Refuse capacity_exhausted, including ticket replay/renewal | Refuse storage_error | Refuse permission_refused |
| open_put / open_get / put_chunk / finish_put / read_chunk | Refuse capacity_exhausted; retire the connection's affected existing lease before any further byte effect; no new stream | Refuse storage_error and retire owned descriptors | Refuse permission_refused and revoke/retire |
| close / endpoint teardown / internal lease timer | Cleanup-only, idempotent existing owned lease; no new authority or resource promises | Cleanup still runs without treating corrupt metadata as permission to delete unknown files | Cleanup still runs |
| trusted disable / runtime shutdown | Preserve existing control/lifetime ownership; no admission reopening | Preserve fail-stop/containment and fixed fatal signaling | Same-state disable remains no-op |
<!-- ADMISSION-END -->


### Historical independent revision-2 exams and narrow semantic amendment — [FIXED]

This dated authority was exercised in the recorded v5 exam revisions; it is not new permission to amend any of the current114 methods. Revision3's additive-only boundary below governs the next exams.

Owner: /root/quota_evaluation_qa, the independent MCP exam/review owner. This author and the eventual source builder MUST NOT edit protected exams. After renewed D0, QA MUST preserve historical exam bytes, hashes and raw results before committing the revision exams with Exam-Owner attribution and a new MANIFEST.json/inventory. Renewed A0 and root's explicit revised source grant follow that commit. Existing 89 core / 874 accumulated methods and 28 lanes are the frozen attempt-2 baseline, not the future count or a v5 pass.

The sole identified incompatible assertion is acceptance/cowork_mcp/test_compatibility.py::BuildHttpDeadlineRegressionExam.test_stalled_http_put_receive_terminates_at_idle_deadline, line172 in the frozen candidate: response.start statuses equal [408] after exhausted idle budget. The independent exam owner MUST amend only that incompatible expected response to no headers (empty start list), preserving its nonconflicting deadline, cleanup and other assertions. QA MUST add a timely positive-remaining-budget timeout control that delivers408. This is the exact narrow semantic authority from approved revision2; it does not claim every previous assertion stays byte-identical. Historical bytes/results remain historical, not rewritten to pass. Any additional observable conflict MUST return to root/Forest rather than being selected by author, builder or QA.

| Existing protected path (relative to acceptance/cowork_mcp) | Required revision-2 ground truth | Parent ACs |
|---|---|---|
| test_compatibility.py | Narrow stalled-PUT amendment above plus positive-budget408; timely PUT201, GET200, Range416 and each refusal family; blocked response.start/body, including partial header visibility, have at most one start; no error allocates fresh time | 04,11,14 |
| test_transfer.py | Exact total/idle boundary and exhausted-budget abort; acknowledged nonempty progress only; exhausted close retires endpoint; accepted PUT with lost receipt exposes committed seal via task_get without byte retry; concurrent MCP/transfer caller gets closed-connection refusal, never wrong seq/response | 10,11,14 |
| test_schemas.py | Actual AF_UNIX backpressure on primary and output_limit fallback with only domain dispatch return instrumented; validation time deducted; applicable lease bounds; partial/indeterminate frame retirement and unchanged committed rows | 05,10,11 |
| test_tasks.py | Parameterized exact operation table: trusted attach/bind; task_get/job_status/job_result projection; idempotent owned job_cancel; every denied operation including replay/renewal causes no new task/job/ticket/reservation/worker/byte authority; normal refusal audit remains required | 03,06,07,16 |
| test_recovery.py | Controlled missed sample with actual fake worker and proven stop; durable cancelled/capacity_exhausted inspection; close/endpoint/timer cleanup; fatal audit/integrity/projection corruption and disabled overrides; explicit qualified restart without automatic relaunch | 06,15,16 |
| test_performance.py | Retain measured transfer/memory/monitor and ordinary comparison lanes, record revised candidate evidence without inventing latency, grace, jitter or memory defaults | 10,11,14,16 |

No new production or exam path is needed by this amendment. QA records any missing fixture as pending A; the concrete paths above are independent exam work, not permission for this author to implement them. Execute the pinned QA interpreter with -B and an independently authorized temporary tree, from /Users/velox/.cortados-builds/cortados-router-repo. QA sets COWORK_REVISION_QA_OUTPUT to a newly authorized evidence directory so the full-suite driver cannot overwrite historical output:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest acceptance.cowork_mcp.test_compatibility acceptance.cowork_mcp.test_transfer acceptance.cowork_mcp.test_schemas acceptance.cowork_mcp.test_tasks acceptance.cowork_mcp.test_recovery acceptance.cowork_mcp.test_performance -v
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py "$COWORK_REVISION_QA_OUTPUT"
```

The full inventory/manifest and existing benchmark-core.py remain independently owned. QA pins any driver operands from its maintained README before execution; zero discovered tests is blocked. Production/native/provider calls are excluded from synthetic revision exams. Full B, accumulated regressions and measurements remain necessary after a revised candidate; earlier completed measurements do not accept either failed candidate.


## v6 revision-3 execution boundary — [FIXED]

Authority and current ledger are DECISIONS' current v6 record and root grant88defac. Parent section4 and R093–R102 are the sole new semantic authority. All revision-2 response/deadline and capacity-only rules above remain operative. Current source candidate4 is rejected/design_error; 899 green maintained methods/28 lanes are historical candidate evidence, not v6 acceptance. The exact18source ceiling and callable/AC/admission copies above remain unchanged. No source/exam/fixture or Git mutation belongs to this authoring.

Disposition for the later gated builder: extend task_store's canonical reader using existing remote_schemas constructors before nested semantic access; extend CoreRuntime's existing monitor-task ownership, fatal signal and authorize boundary; adopt CapacityMonitor accounting and extend the existing Broker/Watchdog containment connection for monitor loss, including durable-enumeration and fatal-sink failure, without adding an owner. Existing dispatch/IPC/file-transfer post-wait authorization consumes the same runtime check. No new file, public API/schema/state, watcher, restart, retry, grace or reopening is granted. Parent section12 pins the actual candidate4 seams.

### Additive independent regression outline (not authored exams)

After fresh D0, the designated independent exam owner preserves every current114 method and assertion, adds cases in these existing files, commits with Exam-Owner attribution and updates the existing manifest/inventory before renewed A. support.py may provide synthetic synchronization only within that later grant. No installation, provider/model invocation, production listener or live state is needed. The pinned existing QA interpreter/commands above remain the execution environment; independent QA owns a newly authorized evidence destination, never overwrites old reports and records actual collected counts rather than a guessed total.

| Existing acceptance/cowork_mcp path | Additive ground truth / discrimination | Rules / ACs |
|---|---|---|
| test_schemas.py | Complete closed persisted-record validation before nested reads: absent nested required field, unknown field, invalid scalar/array and semantic redundant-identity mismatch; valid record control. Exercise actual reader, not only constructor rejection; strict duplicate/nonfinite decoding remains covered. | R093–095; AC05/15 |
| test_recovery.py | Reproduce C4-01 by persisting policy{} in a synthetic actual TaskRow, running the actual monitor and attempting unrelated task_create through actual IPC; fatal refusal and one safe signal precede effects. Valid and malformed-type controls distinguish blanket refusal and selective catch. | R093–097/R100; AC05/15/16 |
| test_recovery.py | Unexpected monitor normal return, exception and cancellation each close admission and signal without new traffic. Order admission before the queued completion callback, then drain callback and prove one signal. Cover startup before ready and terminal detection while capacity-only/disabled. | R096–100; AC03/15/16 |
| test_tasks.py / test_transfer.py | Hold an existing admission/task/stream wait with fixture events, terminate monitor, release the wait before observer execution and verify existing post-wait check refuses before task/job/ticket/reservation/worker/byte effect. Include attach/bind and all operation-table categories; cleanup-only close still settles descriptors. | R098/R101; AC03/06/15/16 |
| test_recovery.py | Owned normal shutdown/startup unwind closes admission before monitor stop, awaits teardown and produces no false monitor-loss fatal; already-terminal unexpected loss is not excused by later shutdown. Read failure plus observer plus admission yields one signal attempt and containment continuation; signal-write failure remains fatal, no retry or raw callback exception, and surfaces safe storage_error at teardown. Monitor loss with a running fake worker attempts STOP independently of sink success and later traffic. Distinguish stop intent from actual receipt/death proof; unavailable proof retains ownership/reservations/lifetime resources through the existing Broker path. | R099–102; AC04/05/15/16 |
| test_tasks.py / test_native_policy.py / test_recovery.py | Add valid/unknown/foreign lookup and ordinary caller-validation controls; preserve existing preacceptance generation-mismatch policy_unavailable, definitive rollback and capacity-only inspection assertions. Actual corrupt binding/publication and uncertain commit stay fatal. | R094/R095; AC06/07/09/15/16 |

Required evidence separates observer promptness from authorization safety: no arbitrary sleep or grace may manufacture callback ordering. Use the real event-loop ready ordering with explicit fixture synchronization. Existing monitor cadence, first-capacity audit and HTTP/IPC deadline tests remain unmodified. Source/protected identity verification, all accumulated lanes, matrix/performance and full B still follow any separately authorized frozen candidate. No acceptance or activation follows author completion.
