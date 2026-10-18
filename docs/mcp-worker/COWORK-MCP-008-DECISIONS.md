<!-- PURPOSE: Record remaining policy/evidence gates without author rulings.
     CONTRACT: RT-COWORK-MCP-008 v5 draft; same assignment, no self-approval.
     DEPENDS: Parent source inspection and independent root/QA evidence.
     CEILING: No global decisions, production, tests, registration or activation edits. -->
# Cowork 008 decision and gate record

Contract: RT-COWORK-MCP-008 v5. Group: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1.
Status: **draft; dispatchable=false**. Existing broad OAuth/tasks/file-transfer scope is authorized;
do not ask for it again. This document records technical proposals and honest unresolved evidence,
not Forest rulings. No provider registration/file-policy approval arrived in recorded docs during
this authoring run. [Root review](COWORK-MCP-008-ROOT-REVIEW.md) and independent provider-preflight.md /
auth-composition-result.json arrived during revision and were read. They supply source/synthetic
evidence, NOT D/A or product approval. Root/contract owner must record any later approval explicitly.

| ID | Current disposition | Owner / evidence needed | Gate location |
|---|---|---|---|
| O1 maintained auth | Candidate changed from tsidp to FastMCP3.4.9 GitHubProvider/OAuthProxy. Pinned composition and narrow resource/identity policy adapter specified in parent section 10. No bespoke OAuth server. | Root selects/records GitHub registration and downstream client; independent QA qualifies actual supported flow, refresh, concurrency/restart, safe logs and identity-only permissions. | FUTURE AUTH blocked separately; independent future-auth D/A and live OAuth before activation. Not a core prebuild prerequisite. |
| O2 native isolation | Concrete remote-only cowork-task-v1 profile proposed; persisted policy digest/CLI version required at job acceptance/bootstrap. Ordinary argv byte-for-byte unchanged. Shell capability review 13/13 passes only its bounded shell lane. | QA actual Codex exec model/tool capture: positive task work; denied credential/broker/sibling/tmp/symlink/network; native auth/quota and termination still work. :minimal runtime surface must be reported. | Local native acceptance before enabling remote execution; NOT a prerequisite to schema/store implementation. |
| O3 binary transport | Technical protocol fixed: raw PUT/GET with header-only capability, immutable content identity, no URL fetching; exact requests/results/errors/recovery in parent. | QA local binary hash/transaction exams; root actual Cowork header egress and download materialization. No query-ticket fallback or silent companion. | Deterministic local build/acceptance first, actual Cowork journey later. |
| O4 technical contract | RESOLVED AS DRAFT DESIGN: closed schemas, result projection, one owner, additive same-transaction task/job binding, canonical replay, recovery, source scopes and remote-policy identity specified. No dependency on live Cowork connection for local builder start after gates. | Independent D then A still required; author resolution is not approval. Any real source seam contradiction returns to root, never private builder change. | Root granted the exact source seam; independent review remains; not dispatchable. |
| O5 size/retention | Pending user policy, already requested by root: 100000000 bytes/file, 1000000000/task, 10000000000/installation (decimal). Live choice remains pending. LOCAL CORE implements explicit task_delete only, no scheduler/automatic cleanup, and makes no retention recommendation or selection. | Root records Forest response, backup/payload versus prompt/history distinction; QA pins technical budgets and measures disk-growth overshoot/headroom. | Mandatory synthetic config before core D/A; live policy record before activation. No implicit defaults or unapproved cleanup. |

## O1 facts and qualification boundary

Pinned FastMCP HEAD: 83c9764e0bc2d9d32910b82264c09370da24ab7a, tag v3.4.9.
Optional dependency proposal fastmcp-slim[server]==3.4.9; base MCP remains exactly 1.30.0.
Its metadata supports mcp>=1.24,<2; this is dependency-range evidence, not a successful install exam.

Maintained primitives exist: GitHubProvider constructor, OAuthProxy routes/JWTIssuer/token swap,
encrypted persistent FileTreeStore wrapper, code exchange and refresh rotation. Parent names precise
adapter seams for exact resource, required exp/use claims, numeric GitHub subject and downstream
client preservation. Scope defaults must be overridden: required_scopes=[] is supported, broad user
default is not accepted. read:user only if independently justified; never repo permission.
Verifier calls /user/repos for scope headers; identity-only GitHub App behavior remains a qualification
question. Raw GitHub tokens/PATs are not accepted MCP bearers.

Updated recorded root/QA evidence supersedes the initial no-refresh assumption: current GitHub OAuth
App docs permit expiring tokens via offline_access. Parent specifies the inherited authorize-URL
hook to add that hint without requiring it as an API scope. OAuthProxy still does not synthesize
refresh: actual upstream fields, finite lifetimes, rotation and restart need qualification.
GitHub App user authorization remains an alternative, not a mandatory migration. No credentials/app have been created by this author.
Persistent storage is not proof of atomic refresh rotation: interruption between multiple writes,
parallel refresh and reauthentication behavior need independent tests. No cache or handwritten refresh
fallback closes that gap. GitHubProvider timeout_seconds concerns API verification; source inspection
does not establish every inherited token exchange meets Claude's 30-second refresh deadline.

tsidp is rejected as current candidate based on recorded optional/plain PKCE and supplied-secret
logging gaps. This is not permission to patch tsidp, deploy a proxy workaround or broaden credentials.
If GitHubProvider qualification fails, root records factual blockers and separately reviewed maintained
alternative; task/file schemas and ownership remain independent of provider choice.

## O5 pending live policy versus mandatory local parameters

File policy candidates above replace v1 MiB/GiB values. Effective limits are copied into each task's
canonical record and returned by API. No automatic policy change on replay or restart.
Historical proposed technical values are NOT defaults or approved deployment values.
Parent section 2 owns the closed required config. Every byte/count/time/policy/principal/client input MUST
be explicit; absent/unknown MUST refuse before side effects. QA pins synthetic fixtures and measures
local bounds. Actual operator values remain a separate activation record.

Stored accounting includes staging, input copies and mutable workspaces. Byte transfer reservations
can refuse exactly at their boundary. Worker filesystem growth monitoring is NOT a hard disk quota;
QA measures overshoot and root approves headroom/poll budgets or selects a qualified storage-quota
mechanism if strict physical ceilings are required. That distinction is an explicit resource gate,
not a hidden weakening of the promised task budget. No new OS component is authorized here.

Explicit task deletion does not remove append-only job/feedback/prompt evidence. Request tombstones
remain indefinitely in v1; no backup/history pruning inferred from payload retention. If full-history
erasure or backup TTL is required, root must freeze a separate policy before claiming it exists.

## Required recorded evidence, in order

1. Root reviews the exact LOCAL CORE package, source pins, closed config/IPC/DB and synthetic-only
   constraints. Actual provider identity and deployed limits/retention remain visibly pending.
2. Designated independent QA freezes protected local fixtures and reviews D then A; root records
   bounded build approval. This draft claims neither pass nor dispatch permission.
3. One core builder implements only EXECUTION's exact source scope. QA checks accumulated 001–007,
   new core exams, baseline/performance and native integration. Actual model-tool confinement remains
   a separately authorized activation evidence gate; synthetic tests do not replace it.
4. Future auth requires its own scope freeze and independent D/A, including preparse token checks,
   safe errors and concurrency/crash fixtures. No actual auth adapter or deployable listener is core delivery.
5. Root records Forest's identity/provider/client and concrete storage/retention answers, qualified
   auth and native evidence, exact live scope/rollback, and actual Cowork file materialization before
   activation. Preserve private 443→8432 and training. No automatic cleanup is authorized by core.

Current author evidence: local source/document reads and contract self-check only; no production/test
files, exams, provider calls, native canary, installation, credentials, commits or live routes changed.

Recorded supplements: root review keeps provider/file choices pending; independent provider-preflight.md
and auth-composition-result.json are source/synthetic evidence, not approval. Native prepare-review.json
has model_executed=false and native_exec_strict_parser_proven=false. Current baseline is 784/785 passing,
one historical 007 parity failure, not green; execution package records preservation requirements.
Candidate downstream client profile now explicitly disables public DCR/CIMD and uses maintained
public-PKCE pre-registration. Root must confirm actual Cowork registration UI compatibility; never
supply the GitHub upstream secret as a downstream secret.

## Historical v3 amendment and retained attempt/evidence ledger

Historical record: RT-COWORK-MCP-008, group 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1, version 3; current version is 5.
Package narrowing MUST NOT reset failed-attempt history, create a replacement group or count author
completion as acceptance. Full existing root/worker evidence remains authoritative, not truncated
to the entries below. Root MUST import any additional job/failure records before dispatch.

| Existing evidence | Disposition preserved |
|---|---|
| a21a86cf-d936-4be9-af76-ddec5048e479; feedback 11947912-9f05-4687-92da-ceb9703fa959 | Initial draft accepted as documents only |
| 5c236b3c-ffc4-4526-ad03-dbf5da0ac957; feedback 3f278224-bfa1-49c5-a3f2-318485c8ba0f | v2 draft-only acceptance; no D/A or build approval |
| Historical bounded fallback delegation | Failed before start; exact job identity unavailable in supplied evidence; do not erase or invent it, automatically retry, or infer a zero-failure ledger |
| Private 008-v2-technical-review.md | Reviewed d4c99db, parent SHA256 1bfb6e6e2a162415c53811187a85ca9a22099a4189c6d73c67e9c23e0e0661d8; D NOT READY, A BLOCKED/not run |
| Native 97417125-08c6-4df8-b916-5edac704a359 | Actual task read/write/network evidence only; ten filesystem-denial claims unverified |
| Direct shell 13/13 and separate Unix socket denial | Bounded evidence retained; not substituted for actual model-tool proof |
| Live baseline 784/785; historical 007 4/4 | Different evidence lanes; live baseline not relabeled green |

The v3 author session had no cortados-workers tools/resources exposed. No route/delegate/model fallback
or new worker job was started. v3 author edits were confined to the three documents. Concurrent root
review changes belong to their author and are not part of this handback's write scope.

### Explicit unresolved future-auth defects

D-008-01: _extract_upstream_claims is too late (parsing/storage precede it) and absent on transparent
refresh. Candidate Authlib access_token_response/refresh_token_response hooks installed through
_create_upstream_oauth_client require independent qualification of initial, explicit and transparent
refresh. GitHub refresh_token_expires_in normalization and conflicting aliases need frozen exams.
D-008-02: one process does not serialize requests. Candidate gate must encompass credential loading
through exchange/store/delete plus transparent verification, acquire once, and release before broker
locks. Synthetic redemption races/restart/crash are future-auth prebuild A requirements.
Neither candidate is declared qualified or a D pass. Auth issuance/storage stay with maintained owner.

### Pending user questions remain pending

[ESCALATE: activation only] GitHub identity/provider/application and downstream client registration.
Publicly observed 4list233 / 237812476 MUST NOT become enabled configuration without recorded choice.
[ESCALATE: activation only] Actual file/task/installation byte values and retention semantics.
Root's question offered 100000000/1000000000/10000000000 decimal bytes and explicit deletion versus
seven-day cleanup. No answer is inferred. No seven-day mechanism is built here.
Synthetic explicit_delete fixtures exercise already authorized task_delete, not a selection of live
payload/history/tombstone/backup policy. Activation MUST freeze each of those semantics concretely.
If the live choice requires behavior outside core, root MUST amend scope and get independent review;
production author MUST NOT choose missing semantics.

LOCAL CORE can be gated independently only against the explicit package, not against an assumed
future login or deployment. No unauthenticated deployable remote entrypoint is an acceptable handback.

Latest supplement: independent-adjudication.json for native job ba4c3e14-c1f7-47fb-93b6-d703a98a6db7
accepts the bounded thirteen-case shell evidence with recorded deviation; strict_exact_command_pass=false
remains unchanged. Parent §9 retains exact hashes and exclusions. This closes that shell-negative gap,
not the complete native/activation gate, and does not rewrite first-capture unverified claims.
Root records revision job 0cf07890-97dd-401c-86ac-cd8bf976a01d under the existing group; no nested
worker dispatch was made by this author.

## Historical v4 author handback before root amendment (not current gate state)

The root accepted the missing CoreRuntime diagnosis and three-module scope proposal. All eight findings are treated as one lifetime, with sole transaction/launch owners retained. The same group and entire prior ledger remain authoritative. This document-only revision is not a valid production build attempt and does not increment or reset valid-failure count. Current root review attributes author job 162d277f-c2dd-48e8-b1ed-f663af7caa22 / request 1d94027e-9e3b-4ef8-8a0f-94dc49c362f0 to this revision; no nested worker was started here.

| Finding | Concrete v4 disposition | Residual |
|---|---|---|
| C1 | Section 2 NativeManifest/AcceptanceSeed/WorkerBinding; section 4 supplier/hash, binary/constants, triple persisted/bootstrap/CLI discriminator, single exact appendix and full-byte checks; R028–035 | S1: existing route_task internally resolves against its own shorter packet. Proposed keyword-only handoff_packet seam needs root source grant; file remains read-only. No false closure by rewriting output counters. |
| C2 | CoreRuntime owns validation→existing lock→Broker/JobStore→one recovery→monitor/sessions→reverse teardown; explicit socketpair spawn/attach/CoreClient APIs; R010–013 | Independent separate-process and crash exams pending A. |
| C3 | RemoteError independent from closed WorkerError; explicit broker/watchdog/JobStore conversion; closed remote_audit transaction events and fatal audit-failure path; R037–042 | Independent event-count/error sentinel exams pending A. |
| C4 | Single copied callable table; closed nested records/diagnostic schema; seed has no job ID, JobStore allocates and binds in same transaction; general canonical encoder adopted; R005,019–027 | Private examples are author schema checks, not committed protected fixtures. |
| C5 | Broker-owned CapacityMonitor; logical st_size, sparse/link/symlink treatment, root inclusion, remaining promises, exact task/installation/free-space inequalities, poll-stop proof and overshoot method; R064–067 | Independent measured baseline/query budgets and live headroom approval pending; no hard quota claim. |
| C6 | Trusted async runtime.disable_owner latches denial, revokes tickets/leases and awaits fd close without cancelling jobs; R016–018 | Synthetic active-stream disable exam pending A. |
| C7 | Identity-versus-live projections; declared/observed artifact rules; UTF-8 output order; strict GET/PUT offsets; bounded control overflow; exact HMAC version/array/base64url and offline vector; R021,043–063 | Independent cryptographic/stream/crash assertions pending A. |
| C8 | Numbered FIXED single-rule registry with AC/reasons; canonical AC and API copies; sequence/ER/annotated-state/classed-flow diagrams; seven-aspect grid; captured paths/hashes and ownership searches | Ordinary bootstrap envelope capture not located; new diagram rendering not performed; independent D/A remain pending. |

### Residual objective blockers

1. **S1 source seam:** root decision for exact cortados_router/route_task.py extension. Existing source cannot convey the appended packet to its resolver. schemas.py/task_evidence.py stay read-only. This technical scope decision is separate from Forest's pending activation choices.
2. **Captured bootstrap envelope:** historical job/route/DB/termination samples are pinned, but a standalone ordinary bootstrap capture was not located. Q freezes one without treating a reconstructed example as captured evidence.
3. **Independent D then A:** revised diagrams still require independent rendering; no D0/A0 is declared.
4. **Protected fixtures and measurements:** acceptance/cowork_mcp paths remain future exam grants; missing committed fixtures, full-suite inventory and measured query/performance budgets remain pending A, not valid build failures.
5. **Integration map:** root's deferred INDEX.md update precedes integration acceptance; current author has no write grant there.

Future-auth and activation remain separately blocked on identity/provider/client registration, deployed file/task/installation limits and retention, qualified auth/storage/refresh, actual native runtime confinement, measured disk headroom, ingress/callback/rollback and actual Cowork binary materialization. No pending prompt is treated as an answered policy choice.

### Honest author self-rubric — not an independent score

All D rows are accounted for here. “Addressed” is an author assessment of the text, not a D0 certification. The objective residuals keep the package draft.

| Rows | Author disposition / evidence |
|---|---|
| D1.1, D1.6 | Numbered MUST/MUST NOT registry replaces local prose requirements; definitions provide exact operands; independent single-rule review pending. |
| D1.2, D1.3 | Closed nested records, ranges/enums, diagnostic schema and callable operands specified; source-fit seam explicitly blocked. |
| D1.4, D1.5 | Wire examples plus 32 valid/invalid private record pairs and semantic negative cases; additional envelope/projection variants remain independent fixture work. No claim exhaustive independent example review passed. |
| D1.7 | Normative prose excludes banned vocabulary; literal required exam filename support.py is retained as a path, not a vague requirement. |
| D2.1, D2.2 | R rules map to AC-008-03–16; ACs name rows, identities, bytes, hashes, descriptors and state. |
| D2.3 | Execution pins future core discovery and accumulated runner commands; absent exams remain pending A. |
| D2.4 | Exact byte/count/time/free-space definitions and measurement plan; performance results still unmeasured. |
| D3.1, D3.2, D3.4 | Definitions and rules FIXED; no open BOUNDED options or delegated policy judgement. |
| D3.3 | Draft, not approved; S1 and activation escalations explicit. |
| D4.1, D4.3, D4.4 | Shared API/AC tables copied verbatim; sole runtime/codec/capacity/error/appendix owners. |
| D4.2 | Wire/private definitions precede consumers; recursive schema refs are schema references; retained auth/history is excluded. Independent locality review pending. |
| D4.5 | Project overlay removes contract line cap; source <500 remains binding. |
| D5.1 | **Residual defect:** no standalone recorded ordinary bootstrap envelope located; pinned constituent captures are not a substitute. |
| D5.2, D5.3, D5.4, D5.5 | Actual constructor/error/route-fit conflicts, exact source owners, hashes and reproducible searches recorded. |
| D6.1, D6.2 | CoreRuntime owns cross-component invariants; S1 remains a pre-dispatch blocker rather than a bypass. |
| D6.3, D6.4 | Four required diagram kinds with action labels and store classes. |
| D6.5 | **Unverified:** new diagrams not rendered in this author turn; independent render evidence required. |
| D7.1, D7.2, D7.3 | Each R entry names consequence/caller; near misses explicitly include post-open locking, after-commit binding, counter-only routing fix, descriptor-number transfer and inferred retention. |

A is pending, not zero: A1.8–10/A5.5 protected exams absent; A2.2 bootstrap sample missing; A3.4 query budgets unmeasured; A6.2 renders unverified; A7.1 S1 unresolved. B is not applicable to this document authoring. Full v3 D=21 remains recorded history, not silently rewritten as a pass.

### Document-only verification record

The author parsed 43 embedded JSON blocks with duplicate-key/nonfinite rejection and checked both Draft 2020-12 schemas, including local reference resolution. All 32 private valid examples validated; all 32 corresponding unknown-field examples were rejected. The canonical API and AC blocks were compared byte-for-byte between parent and EXECUTION. Wire success examples were schema-checked separately. Numbered R001–R080 each carry FIXED, AC and reason; banned vocabulary was checked in their normative sentences. git diff --check and final diff scope are part of the final handback check. These are document validations, not tests or independent D/A. No model/provider/native call was made.

## Historical v4 root amendment, after author handback

This section supersedes the source-seam and missing-bootstrap blockers in the historical author self-review above. Root granted the keyword-only route_task handoff_packet extension, updated exact future source scope and the INDEX, and added a task_store-owned private workspace-preparation journal with JobStore-owned transactions. Preparation commits before mkdir, is adopted with job acceptance, and is reconciled after crashes without paid execution. Retired preparation is not accepted public replay. Existing schema/evidence/quota owners remain read-only.

Independent ordinary bootstrap capture now exists: parent section 9 pins the actual 1749-byte owner output and exact report/script hashes. It makes no native-execution claim. Existing-owner measurements and the complete 75-file inventory are pinned in EXECUTION and ROOT-REVIEW; new-core query/transfer/overshoot budgets remain pending independent A. Exact additional QA evidence paths are granted in EXECUTION. No protected exam or production source has been authored. Parent/EXECUTION API and AC tables remain exact copies. Independent D then A still precede dispatch; all prior review results and attempts remain preserved. User activation choices remain unanswered.


## Current v5 revision-2 approval and preserved implementation ledger

[FIXED] Contract RT-COWORK-MCP-008 v5 retains group 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1 and **two valid failed implementations**. No historical entry above is erased. This document-only revision neither increments nor resets that count. A third same-design retry remains forbidden; Forest approved a bounded revised design, not acceptance, automatic retry or immediate production dispatch.

| Record | Preserved evidence and disposition |
|---|---|
| First valid failure: job 87c9ffee-4119-4301-bafc-4668d8dbf8ca | Rejected feedback 99d61643-cfe6-458a-b639-81d783578670; B=3 at B1/B6/B7; C1–C10 remain historical. Host 857 methods:856 pass/1 failure/0 errors/0 skips;26/28 lanes pass. Output ordering failed. Original matrix timeout and measurement-client idle failures stay preserved alongside later completed measurements. |
| First candidate identities | Cumulative patch 672771b8461d04793bafe30f3dd4da0e6cdcef10bd664a47488e4b27d5576d15; host report 32111c97256707c86b072665260f69f58db7554f635a4bc5dfa77934b204e38e; QA B review 98a22bf2fd621d191d1f2bfbee8c205b5c2f2ae9bf3e00569c0358a382c93e5c; B readiness 378e5740f559c287727cfd150eaadf6d35c0335a68c5fd11c8c1ac9be653ff3b; B manifest e48c8fb814b1b658a353fd716df9da4df5951f8776386e0eac7bc5b4632f8edf. |
| Second valid failure: job d37a73fb-aa46-491f-9a5d-98fa60c682e8 | Intended request d145b0f9-1dca-4fe8-96b5-74e86c707dbf; rejection 52cbfe2a-6eb5-4bea-90b6-9ea0fe162ec3/seq19; closeout rejection 854778c2-cdfb-4e40-b675-2114ffe64050/seq20. B=3 at B1/B5/B7, other eight rows zero. Host 874 methods:873 pass/1 failure/0 errors/0 skips;27/28 lanes pass. Capacity-overrun result inspection failed. |
| Second candidate identities | Cumulative patch 634cce15a58df7f94512a9402cb2d22dfc26ec582ce55a2ae7f98b3409227394; host report 756c7221d561f42e6b3265f4807e68e51beb66968b8f599b6546c19502160ce0; final readiness f3400cca106486da66bdcdae29721a95ac64417a35e83c9f10611ef62866c560; final measurement review 0ad6ad01896961c875e4634a101a6f0946b35b93ac65b7866dcd719a5627477c; final manifest c8de880246b75efff27dd54e4f544015096e1ccfd9b13d2d10508279b421a3fd. |
| Completed measurement evidence | All12 transfer matrix cases, monitor, ordinary comparison and four supplemental workloads completed. FINAL-MEASUREMENT-REVIEW.md under private core-candidate-attempt-2 records limits; completion does not accept incorrect behavior or erase original failures. The later observational capacity pass remains separate and does not prove the original scheduling cause. |
| IPC diagnostics | Corrected actual AF_UNIX primary/fallback probe:1 pass/1 failure, fallback still open at361.115ms with ipc_idle_ms=100. Initial expected-BrokenPipe fixture error remains separate. Neither becomes a new implementation attempt. |
| Setup-only records and original reviews | Earlier preflight-only jobs remain blocked, not valid failures. Historical v4 D0/A0 apply only to their exact old normative/fixture identities; v5 requires renewed review. Existing 89 core/874 accumulated inventory and all prior evidence remain retained. |

Approval chronology: proposal v2 at7e8b1cd, SHA256 a4fa9ebfac15604d5d516c25dadf3c87ff690ec4305a8c24b0c634a011b34bbf; Forest answered “Approve the revised design and continue”; root recorded approval at18e4a7f and /Users/velox/.cortados-builds/cowork-mcp-verification/core-revision-2/USER-APPROVAL.json. That record reports680 checked prior identities, zero mismatches, prior_valid_failures=2 and source_build_authorized_now=false. The approval permits these revised contracts and independently committed revision exams, then a revised build after renewed D0/A0 and root grant. It does not select live identity/storage/retention/operator policy or authorize auth/production/protected exam writes by this author.

### Per-approved-rule closure map — review pending

| Approved rule | Normative location | Independent verification path |
|---|---|---|
| One HTTP lifetime; acknowledged nonempty progress only; no renewed error wait | Parent section4 revision-2 definitions, R088; AC10/11/14 | EXECUTION revision matrix: test_compatibility.py, test_transfer.py |
| Conservative header-attempt state for JSON, Range416, binary and errors; positive-budget408, exhausted abort | Parent response state and HTTP status map, R088; AC14 | Same paths: timely controls plus every start/body stall and exact deadline boundary |
| Endpoint retirement, shared-client consequence, lost committed PUT receipt, no job cancellation/resend | Parent CoreClient/lifetime definitions, R089; AC11/14 | test_transfer.py, test_recovery.py |
| IPC primary/fallback one deadline, validation deducted, lease bound, no second frame | Parent response-write definition, R046/R090; AC05/10/11 | test_schemas.py actual AF_UNIX backpressure and partial-frame evidence |
| Exact capacity table; inspect accepted outcomes; idempotent cancel; deny replay/new authority | Parent/EXECUTION identical table, R019/R091; AC03/06/07/16 | test_tasks.py and test_recovery.py |
| Fatal/disabled override; containment/cleanup; no automatic reopening/relaunch | Parent table and R066/R092; AC06/15/16 | test_recovery.py plus full table negatives |
| Existing sole owners, no new policy/default, subscription quota/job lifetime/integrity preserved | Unchanged API copy, parent section11 source grounding; retained18-path future ceiling | Renewed independent D/A then full B/inventory/measurements |

### Exact exam authority and remaining gates

The only identified prior semantic conflict is the frozen test_compatibility.py::BuildHttpDeadlineRegressionExam.test_stalled_http_put_receive_terminates_at_idle_deadline expectation [408] at line172. Under approved revision2, exhausted idle leaves no response budget. After D0, only /root/quota_evaluation_qa may preserve historical bytes/results and amend that incompatible expectation to an empty response.start list, retaining all nonconflicting assertions and adding a timely positive-budget408 control. EXECUTION specifies exact protected paths and commit/manifest gates. This is a narrow semantic amendment, not a claim that every prior assertion remains byte-identical. Additional observable conflicts must return to root/Forest.

Independent renewed D and A remain pending; no author self-score or approval is issued. Original repository, frozen source, protected exams, providers, native execution, secrets, listeners, training and schedules are outside current write scope. This author writes exactly the three copied normative documents under /Users/velox/.cortados-builds/cowork-core-contract-revision-2, without commits or nested delegation. No unresolved additional observable proposal conflict was identified by the author; independent review may find one. Live identity/storage/retention/operator decisions remain unresolved and outside this revision.


### v5 document verification record (not D/A)

The author checked all45 embedded JSON blocks for parsing, duplicate keys and nonfinite values; both Draft2020-12 schemas pass schema validation. All33 private valid examples validate and all33 corresponding unknown-field examples refuse. Parent/EXECUTION API, AC and admission blocks compare byte-for-byte; the admission table also equals the approved proposal bytes. Historical AC IDs03–16 and rules R001–R087 are retained, with R088–R092 added. Four Mermaid blocks remain: three are byte-identical to v4; the sequence adds the operation-permission check using its existing participant/message syntax. Rendering has not been performed and remains independent review work.

Original18 candidate source hashes matched root-freeze.json. Original repository normative files remain untouched; only the three copied files changed. The copy directory is not a Git checkout, so whitespace/fence/diff checks use direct text comparison to the original repository v4 files rather than claiming a git diff run. No production tests, protected exams or external calls were run. The one identified semantic exam conflict is documented above; additional observable conflicts are not silently resolved.
