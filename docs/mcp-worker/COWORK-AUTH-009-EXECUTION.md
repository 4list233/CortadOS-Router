<!-- PURPOSE: Define prospective bounded auth build, exam and activation ownership.
     CONTRACT: RT-COWORK-AUTH-009 v3; draft, not dispatchable.
     DEPENDS: COWORK-AUTH-009.md, accepted RT-COWORK-MCP-008 and independent D/A.
     CEILING: Current turn writes only these two drafts; no code, tests or commit. -->
# Cowork future authentication execution package

```yaml
contract_id: RT-COWORK-AUTH-009
contract_version: 3
parent: COWORK-AUTH-009.md
implements: RT-COWORK-MCP-008 FUTURE AUTH
owner: Forest
status: draft
dispatchable: false
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
capability_tier: stateful integration builder; independent auth and concurrency reviewer
selection: future S1 after context_requirements; no model call in authoring
attempt_ledger: inherit 008 assignment and D-008-01/02; do not reset
```

## 1. Current authorization and prerequisites

[FIXED] Current scope_paths are exactly `/Users/velox/.cortados-builds/cowork-auth-contract-revision/COWORK-AUTH-009.md` and `/Users/velox/.cortados-builds/cowork-auth-contract-revision/COWORK-AUTH-009-EXECUTION.md`. Current acceptance_paths are empty. Only those copied drafts may be edited; original repository is read-only. No implementation, acceptance authoring/execution, package install, network, worker/model invocation, credentials, registration, service changes or commit is authorized. Stop at the two drafts and handback.

[FIXED] Future dispatch requires accepted core implementation plus auth-specific independent D=0 then A=0, committed protected exams, resolved applicable O rows, approved dependency lock and nonoverlapping scope. Frozen core D0/A0 and committed protected exams do not imply core B acceptance; core B is running separately. Local synthetic auth design and exam specifications can proceed without actual GitHub identity/file-policy choices. Actual live access is never a prerequisite for writing synthetic exams.

[FIXED] The parent owns all schemas, API signatures, examples E1–E34, R rules and contradictions. This execution document references them instead of copying nested examples. Any change in observable semantics returns to root for a versioned amendment before build.

Future worker dispatch MUST use the existing group, absolute Studio cwd `/Users/velox/.cortados-builds/cortados-router-repo`, a stable per-attempt request_id and configured timeout. Root MUST inspect an existing job before resubmitting. Root MUST call context_requirements before route/delegate and supply the pinned parent, exact scope, accepted-core hashes, resolved O decisions, exam hashes, constraints and verification in task_evidence. S1 recommendation does not execute; delegate accepts the attempt. No automatic retry/quota fallback. Root tracks job_status/job_result and records evidence-based job_feedback. Worker MCP and permissions are explicit grants, never inherited connectors. No nested workers in this assignment.

## 2. Prospective source scopes and ownership

All paths in this section are proposals, NOT current grants. Verified existing parents: `integrations/mcp/`, `docs/mcp-worker/`, repository root, `acceptance/`. `acceptance/cowork_auth/` is a proposed NEW directory. Source MUST remain <500 physical lines per responsibility; no minification/generic utils split. New source headers MUST state PURPOSE/CONTRACT/DEPENDS/CEILING. No broad globs.

Package B1 owns thin auth policy/lifetime after its separate gates:

| Exact prospective scope_path | Status / single responsibility |
|---|---|
| integrations/mcp/remote_auth_config.py | NEW; closed AuthConfig validation; no secret resolution effects. |
| integrations/mcp/remote_auth_provider.py | NEW; StrictGitHubProvider's two maintained hooks and response validation. |
| integrations/mcp/remote_auth_claims.py | NEW; strict claims and credential-free facts projection. |
| integrations/mcp/remote_auth_runtime.py | NEW; one process admission gate, bounded waiter accounting, operation settlement/failure latch and identity-client lifetime; delegates all storage trust/lock/marker/barriers to remote_auth_storage. |
| integrations/mcp/remote_auth_errors.py | NEW; dependency-log suppression, bounded safe OAuth response filtering and AuthEvent emission. |
| integrations/mcp/remote_auth_transport.py | NEW; supported real HTTPX/Authlib client construction, endpoint/encoding policy and pre-eager-read body/chunk/header guards, parent §12.1. |
| integrations/mcp/remote_auth_storage.py | NEW; sole lock/marker/manifest/barrier and strict encrypted/decoded trust owner around maintained storage, parent §§8/12.2. |

B1 MUST NOT duplicate OAuth protocol transitions or maintain token records. Root MUST return a scope amendment before splitting any of these responsibilities into additional paths.

Package B2, sequential after accepted core and B1, owns production composition and packaging:

| Exact prospective scope_path | Status / constrained change |
|---|---|
| integrations/mcp/remote_config.py | FUTURE core file; only approved O2 service validation seam. Existing local_test path preserved. |
| integrations/mcp/remote_runtime.py | FUTURE core file; only approved O2 trusted constructor selection. Existing recovery/locks/job lifetime preserved. |
| integrations/mcp/remote_http.py | FUTURE core file; one HTTP composition owner and guarded MCP entry; preserve exact core ticket transfer route. |
| integrations/mcp/remote_service.py | NEW; config/CLI and app/metadata composition; delegates trusted process lifetime to remote_supervisor, no transport/transfer reimplementation. |
| integrations/mcp/remote_supervisor.py | NEW; sole parent CoreRuntime/spawned frontend owner and exact private lifecycle channel, parent §12.3; no job or token protocol. |
| pyproject.toml | EXISTING; reviewed optional remote extra only, no implicit ordinary dependency upgrade or default command change. |
| docs/mcp-worker/cowork-auth-requirements.lock | NEW; exact remote resolved dependencies and artifact hashes after separately authorized resolution. Research freeze alone is not a release lock. |

B2 MUST NOT change server.py or __main__.py. Invoke the separate module explicitly, keeping ordinary stdio lazy imports unchanged. No core file ownership transfers until core acceptance is recorded and no core builder holds it. B2 MUST keep existing managers/descriptors/IPC/byte handlers as their sole owners. It MUST NOT alter worker, broker, quota, route, task store, ticket or file-transfer algorithms. Use parent §12.3 and existing CoreRuntime.disable_owner/Session.closed; do not add a CoreClient disable opcode.

Frozen future CLI syntax (Q verifies, never run in this turn):

```text
python -m integrations.mcp.remote_service check --auth-config ABS --core-config ABS --base-config ABS --native-manifest ABS
python -m integrations.mcp.remote_service serve --auth-config ABS --core-config ABS --base-config ABS --native-manifest ABS --host 127.0.0.1 --port PORT
```

[FIXED] `check` is non-listening schema/dependency/seam validation only; it MUST NOT resolve secrets, register clients or contact GitHub. `serve` is activation-only and MUST refuse missing operator settings, non-loopback host, multiple workers, failed root lock or unqualified storage restart. PORT is an operator-approved integer 1..65535 without a default. The eventual release interpreter/paths/service manager remain separately approved; these strings are not deploy commands to execute now. Client provisioning uses maintained register_client offline while stopped; no provisioning CLI is authorized in B1/B2 until root records its exact scope.

Excluded from B1/B2: every acceptance/test fixture, these contracts, existing global rulings/index, all installed releases, live config/state/keys, ingress/launchd, training/labels, upstream library source, task/file-transfer/queue/quota owners, ordinary server registration and user homes. Dependency changes may not silently broaden FS/network/MCP permissions.

## 3. Independent protected exams (Q)

Owner: designated independent MCP QA, never B1/B2. These are prospective acceptance_paths, not files to author in this turn:

* `acceptance/cowork_auth/__init__.py`
* `acceptance/cowork_auth/README.md`
* `acceptance/cowork_auth/MANIFEST.json`
* `acceptance/cowork_auth/support.py`
* `acceptance/cowork_auth/fixtures/examples.json`
* `acceptance/cowork_auth/fixtures/source-pins.json`
* `acceptance/cowork_auth/test_provider_hooks.py`
* `acceptance/cowork_auth/test_claims.py`
* `acceptance/cowork_auth/test_oauth_http.py`
* `acceptance/cowork_auth/test_auth_lifetime.py`
* `acceptance/cowork_auth/test_safe_errors.py`
* `acceptance/cowork_auth/test_core_composition.py`
* `acceptance/cowork_auth/test_compatibility.py`
* `acceptance/cowork_auth/test_resources.py`

[FIXED] Q MUST commit these exams with Exam-Owner attribution before dispatch. Q MUST pin interpreter/source/dependency/fixture hashes in MANIFEST. Q MUST supply fail-before-build cases for each new path. Existing core QA paths remain read-only dependencies, not 009-owned exams. Missing exam or zero discovery count is BLOCKED, not a pass. Required new paths beyond this list require a recorded amendment.

Historical AC text copied byte-for-byte from parent §7, retaining the old split for provenance:

| Stable ID | Historical local/prebuild text | Historical activation text |
|---|---|---|
| AC-008-01 | Real loopback ASGI discovery/401 challenge, all MCP methods auth, exact port/path/resource; PAT/ID/refresh/wrong issuer/audience/expiry denied | Actual hosted discovery/callback |
| AC-008-02 | Maintained-provider local fixtures: S256 only, exact redirect, duplicate/resource rejection, no positive cache, bad-secret logs, encrypted restart/unknown-key fail closed | Real GitHub App identity/refresh/rotation; two parallel refreshes and crash windows; client deadlines |

[FIXED O5, ratified at 495973c] R14 supplements the unchanged historical rows: synthetic races, crash/restart and safe failure are LOCAL exams; actual GitHub/Claude observations are additional deployment evidence. Q MUST NOT reset IDs or hide the historical wording. No distinct AC-009 numbering is introduced.

| Protected file | R / AC coverage and required observable ground truth |
|---|---|
| test_provider_hooks.py | R1–2, AC02: actual pinned maintained callback, explicit refresh and transparent refresh use both hooks. Reproduce the 26 invalid-shape matrix on all three paths plus E1 valid controls; add duplicate JSON keys, unknown absolute expiry, alias-only valid response, error-status/error-field and oversize cases. Observe parser-entry counts, put/delete interception and encrypted-file hashes; malformed success causes zero parse and zero token writes. Downstream code exchange has no upstream fetch. |
| test_claims.py | R7–9, AC01: signature verification precedes facts, typed exp/iat/nbf exact boundaries, scalar exact audience/issuer/client; missing/malformed/foreign live subject, raw PAT/ID/refresh all refuse. Verify returned SDK object contains proxy bearer only and IPC Principal contains no token; never accept display login. Two clients with same owner require explicit stopped-service rollover; owner hash stays equal. |
| test_oauth_http.py | R3,9–11, AC01/02: exercise actual maintained ASGI handlers using in-process HTTPX ASGITransport with all external sockets forbidden. Full authorize/consent/callback/code+S256 verifier exchange, exact state/redirect binding, missing/plain/duplicate/wrong resource, client allowlist, no /register or CIMD fetch, safe metadata and 401 challenge. Test every /mcp method unauthenticated before core 405, no duplicate mount. Synthetic route responses are not actual hosted discovery evidence. Loopback lane is later separately authorized if required by historical AC01. |
| test_auth_lifetime.py | R3–6,13, AC02: barriers before credential get prove one outer gate; two HTTP code redemptions produce one success, second invalid_grant; two refresh redemptions produce one rotation; explicit versus transparent and callback replay serialize. Trace lock enter/read/write/delete/exit; no nested acquisition; gate released before CoreClient. Independent processes prove second lifetime owner refuses before store access. |
| test_auth_lifetime.py (fault lanes) | Kill isolated synthetic subprocess at every observed multiwrite await boundary (callback code put/transaction delete; code delete/upstream/JTI/refresh puts; explicit upstream/new JTI/old JTI/metadata changes; transparent upstream put). Restart outcomes must match parent §8 kill-point table, including its final-barrier/all-process-loss limitation; corrupt ciphertext/wrong key/missing reference/invalid stored schema cannot authenticate. Cancellation during upstream exchange/store fails lifetime, no retry; gate wait cancellation never starts exchange. Preserve files and distinguish process kill from untested power loss. |
| test_safe_errors.py | R12, AC01/02: inject unique secret sentinel in token/code/refresh/error body/exception/request URL/HTML/redirect/cookie and dependency traceback. Capture log records before handlers, stdout/stderr and every ASGI output byte/header. No sentinel escapes. Validate E7 fixed body, safe challenge, no partial success sent on overflow. Debug-off alone is a negative control. |
| test_core_composition.py | R4,11,13 / O2, AC01/02: accepted CoreRuntime and CoreClient over inherited socketpair; actual manager initialized once, exact 12 descriptors and unchanged transfer route. No broker call before auth; no secret over IPC; no auth gate held during call. Disable closes tickets/streams through core, does not cancel accepted jobs; disconnect/expiry never resubmits. Local_test loader still rejects service mode; no startup DB effect on malformed auth config. |
| test_compatibility.py | AC01/02 integration regressions: stdio startup without remote extra, ordinary argv/schema/result bytes and training unchanged. Existing subscription/quota refusals start zero native jobs and no API fallback. Historical 007 and current training integrity remain distinct lanes. |
| test_resources.py | All R measured boundaries: configured request/response/gate limits at bound and one beyond; query/provider counts, waiting/active memory, disconnect/backpressure; no model calls. Dataset/procedure in §5. |

Q's prebuild command proposal, after independent exam authoring in the pinned existing research environment (not run here):

```sh
/Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/bin/python -B -m unittest discover -s acceptance/cowork_auth -t . -p 'test_*.py'
```

Q MUST make the command runnable without internet, packages, real credentials or live files. In prebuild mode it proves maintained seams and exam integrity; missing future production imports remain explicit expected-red build checks, not full acceptance. Candidate mode MUST exercise the actual future adapters/entrypoint, with no substitute implementation. No “probe passed” converts to candidate B pass.

## 4. Gates and dispatch order

1. [FIXED] Root pins 5d98d77 and parent v3 semantics; proposes section6 amendments only after accepted core B and before dependent B2. Forest's deployed O1 remains separate, never filled with synthetic values. Parent/contract amendment precedes dependent code.
2. [FIXED] Q performs independent auth D. D0 requires exact callable/schema/ownership closure and rendered diagrams; this author asserts no rubric score.
3. [FIXED] Q freezes and commits synthetic exams, including D-008-01/02 regressions, and performs A. Missing local fixtures, source qualification or finite cap/call-budget oracles block A. Postbuild candidate latency/RSS is not a prebuild prerequisite; candidate imports are explicitly expected red.
4. [FIXED] Root waits for accepted core B and releases its shared-file ownership. Core D0/A0 alone does not authorize auth integration.
5. [FIXED] B1 implements only its scope; B2 follows accepted B1 and approved sequential seam transfer. Dependency install/resolution requires separately authorized bounded work; no ordinary environment upgrade.
6. [FIXED] Q/root rerun the actual final candidate plus full accumulated system inventory. Builder success narration is insufficient. Root records accepted/rejected/inconclusive feedback against stable job/group and AC IDs.
7. [FIXED] Activation remains blocked until §6 gates. Local acceptance never proves live GitHub refresh, Claude OAuth, native tools or binary materialization.

Full regression requirement: adopt the actual 26-lane accumulated runner described in 008 EXECUTION and `V/provider-preflight.md`, then add core/auth lanes. Q MUST inventory maintained unittest/pytest/standalone/native/plugin/adapter entrypoints and report discovered/executed/skipped/blocked counts at final diff. Starting references, not permission to run:

```text
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py ISOLATED_EVIDENCE_DIR
python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py'
python -B -m unittest discover -s acceptance/cowork_auth -t . -p 'test_*.py'
```

The pinned environment for each lane must match its dependency lock, not the shell's incidental python. The original baseline remains 785 tests / 784 pass / 1 fail, not green. The live whole-label-hash parity failure is retained; historical-007 separately passed 4/4 after three literal path relocations and equal normalized AST. Do not weaken that exam or freeze authorized nightly appends. Candidate verifies ordinary profile equality plus existing label prefix, exam provenance and final live partition checks.

## 5. Resource measurement plan

[FIXED] Independent maintained baseline V/auth-baseline/run-3/report.json is corrected70 samples (seven flows×10), SHA2569612c2037882a3775483918cc8776833bc8ae4f7f2e926a89588dc75338b91f2; manifestf27d143b6b314b4231bd69881b84668a98fe9cb78853e6dfcdff02ac9514f8a2. OWNER-CLOSURE SHA2569728dde230af719cbecd6c3d4c16affd9387bafb6d89caa39aa88f78f069fc2d supplies source gaps. Root5d98d77 approves the following numerical **direct-method regression budgets**, previously proposed in README. They are exact observed vectors for these fixture paths, not live latency/RSS SLOs, public endpoint aggregate caps or SQL counts. Source-backed changes require root disposition before accepting a different vector.

| Direct maintained path | keyed get/put/delete | token POST | identity GET | hook/parse | historical median ms |
|---|---|---|---|---|---|
| callback | 1/1/1 | 1 | 0 | 1/1 | 6.622917 |
| code | 2/4/1 | 0 | 0 | 0/0 | 7.599229 |
| refresh | 3/4/2 | 1 | 0 | 1/1 | 10.588313 |
| resource | 2/0/0 | 0 | 2 | 0/0 | 1.749229 |
| transparent | 3/1/0 | 1 | 4 | 1/1 | 7.216959 |
| transparent_refused | 4/0/0 | 1 | 3 | 1/0 | 7.188521 |
| bad_signature | 0/0/0 | 0 | 0 | 0/0 | 0.011604 |

Count at the same six provider PydanticAdapter method boundaries as the pinned baseline script. Do not count nested FileTree calls as additional logical provider queries or hide added wrapper work. Preserve zero store/provider work for bad signatures. The original direct fixture uses the maintained upstream-client fallback; it excludes registered downstream-client HTTP authentication, consent, PKCE, gate/strict claims/projection/trust wrappers/barriers/shutdown. New synthetic HTTP exams provision E19 through maintained register_client and separately report static-client lookup. transparent_refused historically performs later fallback reads after the rejected hook; candidate failure must latch before success output. If fail-stop safety stops that measured path early, report the exact reduced trace as a safety delta alongside the unchanged maintained comparison, never add calls or pretend it reproduced the full vector.

Original run3 constructs fresh verifier clients. Candidate §12.1 deliberately uses one real lifetime-owned client with keepalive disabled. Remeasure this lifecycle; do not label baseline as already using it or silently substitute preliminary run1. Baseline timings include the maintained hook only; Darwin cumulative high-water RSS126337024→127303680 includes imports/setup/retained samples, not per-call allocation.

### Fixed synthetic caps and prebuild/postbuild split

These are approved **exam inputs** only: E9 control/response65536, upstream body131072, upstream chunk65536, upstream header167936, storage record1048576 bytes, gate wait1000ms, operation5000ms, max_waiters2. Queue matrix max_waiters=1,2,8; body matrix1024/65536/131072; chunk matrix65536/131072 (actual source read remains65536); header source floor167936. Exercise each configured threshold and one byte/slot beyond; reject lower-than-source chunk/header settings. Deadline fault matrix uses synthetic controlled clocks at1ms and10ms as well as E9; these do not assert host performance. All live configuration values remain unset/no defaults.

Before A, Q pins source/fixture hashes, numeric cap semantics, exact seven vectors, measurement scripts and independent assertions; verifies maintained seams and expected-red candidate imports. It records OS/CPU/RAM/filesystem/interpreter/lock, warmups/samples and error counts. No nonexistent candidate is required to provide prebuild resource measurements and no arbitrary new latency/RSS ceiling is invented to unblock A. Dependency resolution/target Python and barrier availability must be qualified in the separately authorized isolated environment before B acceptance.

After implementation, before B acceptance, run the same fixtures on (1) accepted core without auth, (2) unchanged maintained auth baseline, (3) final actual candidate. Baseline(1) cannot perform auth, so compare common HTTP/transfer/startup costs only; (2) compares maintained methods; (3) adds complete auth paths. Measure new gate, strict claims/response validation, transport stream/header/cookie guard, static-client lookup, storage raw preflight/duplicate decode/double decrypt, marker/manifest/fsync/full-flush and shutdown overhead as separate tagged spans and I/O counters. Report inclusive totals as well as components; never subtract overhead to fabricate an endpoint pass. FileTree has no SQL; core SQL is separate. No duplicate identity call may be added by the adapter.

| Scenario | Reproducible postbuild samples / evidence |
|---|---|
| Cold enter/clean close/restart refusal | 30 isolated roots each; startup registration setup separately; elapsed/CPU, RSS, file/bytes, lock/marker/barrier traces. Never reuse failed namespaces. |
| Direct maintained methods | 10 repetitions×7 with original input hashes/counters, then same method paths through actual guards; list safety/lifecycle deltas explicitly. |
| Complete valid MCP verification | 100 warmups then1000 serial; p50/p95/max, gate wait/hold, upstream and CoreClient separately, logical/backing/preflight calls. |
| Wrong/missing bearer/disabled | 1000 each; zero core/native work; bad signature zero credential/provider work; response/log bytes. |
| Full HTTP lifecycle | 100 isolated registered-client consent/S256/callback/code/explicit-refresh/transparent-refresh sequences; clock/provider scripts pinned; no implicit retry. |
| Contention and failure settlement | concurrency1,2,8,32 at waiter caps1,2,8; active<=1, queued<=cap; count every removal once and verify no late acquisition. Observe overflow, queued body readers, timeout/cancel races, frozen failed-process state and supervisor kill/join. |
| Adverse response/storage | Source-backed httpcore fake network reads, oversized complete/incomplete headers, huge invalid transport chunk, absent/lying length, compression, body at/max+1, slow/disconnected stream, duplicate envelope/inner JSON, bool coercions, corrupt/expired rows, full disk. Count allocations before decoding and actual closes. |
| Transfer coexistence | Existing protected core0/1KiB/1MiB/max byte fixtures and chunk×stream matrix while auth contends; no auth copy of payload and no gate across CoreClient/transfer work. |
| Lifecycle control | Valid/invalid/oversized/truncated/wrong-sender frames, every EOF/state/deadline, disable/auth failure/revoke acknowledgment, child death with accepted job surviving, explicit parent shutdown only. |

Independent B reviews raw memory scaling against max_waiters×bounded retained input plus one active guarded response/chunk, fixed server/TLS/parser overhead, storage preflight and manifest size. Report tracemalloc and process RSS separately (high-water is cumulative), per-file scanning versus namespace manifest growth, bytes/files/operations, CPU/wall p50/p95/max and refused counts. Memory must plateau with waiter cap under sustained excess arrivals; no hidden queue or whole transfer copy. Finite configured caps are correctness requirements, not an RSS SLO. Unexplained growth, extra method calls, omitted overhead or missing raw candidate comparison block B; do not mark efficiency accepted merely because tests exit0. A newly proposed latency/RSS threshold needs measured root approval, not author invention.

Live upstream latency and actual Claude10s/30s deadlines remain separate activation evidence. v3 replaces the maintained30s OAuth/10s identity defaults with explicit remaining operation deadline on both clients; synthetic deadlines do not prove live sufficiency. Model/provider inference, network connects and cost remain zero in local fixture lanes. No subscription price/quota expiry is invented.

## 6. Activation and documentation package (root only)

No live path is granted here. Later root documentation ownership is exact `docs/mcp-worker/INDEX.md`, `docs/mcp-worker/INSTALLATION.md`, `docs/mcp-worker/USAGE.md`, `docs/mcp-worker/CLAUDE-APP-CONNECTOR-PLAN.md`; these are existing files excluded from this author turn. Root must update the single existing map before integration acceptance, not add a rival index.

Deployment checklist is a gate specification, not instructions to act now:

* Accepted core and auth B, complete accumulated regressions, resolved release dependency lock and measured budgets.
* Forest's deployed GitHub subject/application choice and downstream client profile; no repo permissions. Actual upstream finite lifetimes, expiry aliases, offline_access or App behavior and rotation captured without token-bearing logs. No invented refresh/fallback TTL.
* Approved numeric file limits and retention for payload/history/tombstones/backups; any mismatch with core schema amended first. Auth backup/key/recovery policy O3 independently qualified.
* Exact HTTPS origin/issuer/resource/host/origin allowlists, callback registration and rollback scope recorded by root. Candidate `https://forests-mac-studio.tailb38028.ts.net:8443/mcp` stays inactive; preserve private `443 → 127.0.0.1:8432`. No default port/route, no takeover of existing service.
* Stable independent secrets and private store provisioning approved separately; client registration uses maintained public-client API, never upstream secret in Claude. Authentication through Claude app custom connector URL+OAuth, no fixed headers, SSH or companion substitution.
* Actual Claude discovery/S256/callback/token/refresh sequence and deadline behavior; no claims from direct provider-method or in-process HTTP tests. If metadata/client profile fails, return O4 rather than enable arbitrary DCR.
* Actual native tool confinement qualified beyond bounded shell evidence, including unresolved file-edit/file-read/document-runtime negatives. Existing shell13-case acceptance with recorded unused-dot deviation is not full tool proof.
* Actual Cowork raw PUT Authorization-capability egress and GET materialization: PDF/DOCX/XLSX/non-UTF8 source→upload→worker output→download→local hash equality. HTTP200 or a URL alone is not success. No new transfer implementation to conceal a client limitation.
* Explicit operator disable/rollback rehearsal: stop public admission, revoke/close through core, retain jobs/evidence and private route/training. No automatic job cancel, state deletion or retry.

Failure of any deployment-only lane leaves activation BLOCKED while preserving independently accepted local work. App creation, secret provisioning, registration and live default selection remain unanswered user actions outside this assignment.

## 7. Handback and unresolved decisions

Future builder handback MUST include confined diff, per-source physical line counts, exact dependency/source/fixture hashes, per-AC returned response/store/event evidence, gate/crash traces, full-suite membership, baseline/candidate measurements, selected bounded choices, deviations and unresolved risks. Root independently reviews and reruns; completion alone is not correctness. No commit/install/push follows automatically.

Current author handback is only these two draft documents. Historical core D21→D3→D0 and auth D-008-01/02 remain recorded in parent §1. No tests or production acceptance are claimed.

| Stable decision | Remaining owner / gate |
|---|---|
| O1 | Forest/root: deployed subject/app/client and file/retention choices unanswered. |
| O2 | Root: matching accepted-core amendment and exclusive shared-file ownership release. |
| O3 | Q/root: independent marker/barrier/kill-point qualification; separate provisioning/recovery authority. |
| O4 | Q: exact none/S256 metadata and full local ASGI profile; live Claude compatibility remains additional. |
| O5 | Ratified 495973c; preserve historical rows and supplemental local R14 tests. |
| O6 | Fixed fixture caps/call vectors before A; actual candidate overhead/resource comparison before B; deployment values/deadlines at activation. |
| O7 | B2/root: isolated reviewed lock, ordinary parity and existing-index update before integration acceptance. |

No row selects a product, permission, retention or identity alternative. Parent §12 freezes technical interfaces; local validation and activation dependencies remain distinct.

## 8. Revision-specific ownership and verification obligations

Root authority is Router commit5d98d77, technical closure after the independent maintained baseline. The current user explicitly prohibits nested workers; the general worker workflow applies only to a separately authorized future dispatch. This authoring does not start an attempt or alter the inherited group/ledger. Prior auth author job20d04076-54f8-46a1-b527-4597ca5ad88c and draft feedback23b83eaa-1170-4288-92c7-87da03b6b853 remain draft-only history.

B1 owns exactly seven files in §2: config, provider, claims, runtime, errors, transport and storage. Storage exclusively owns lock/marker/manifest/barrier/envelope/decoded trust; runtime exclusively owns admission and settlement and the identity client lifetime; transport owns guarded real client construction/reads. B2 adds supervisor to its exact prior integration/package paths; supervisor exclusively owns parent CoreRuntime and child/control lifetime, service owns CLI/routes/metadata, remote_http retains the single manager/transfer composition. No duplicate marker, broker or token owner. Every source remains strictly below500 physical lines including comments/blanks; docs have no cap. A needed extra source path returns to root before implementation.
Additional mandatory protected scenarios, in already listed Q-owned files:

- test_auth_lifetime.py: clean provisioned generation0 -> dirty1 -> clean1 -> dirty2; unchanged pre-provisioned client and still-valid credentials across clean restart. Kill before/after every marker write/flush/rename/directory barrier and every provider multiwrite await; assert exact allowed restart outcome and byte preservation on refusal. Include missing marker with populated namespace, pending file, wrong encryption/signing/upstream key, changed config/lock digest, generation overflow, second process and symlink/hardlink substitution. An auth-failed lifetime never becomes clean; observed final-barrier uncertainty fails the supervisor. All-process-loss outcomes are exactly the parent §8 table, not universal uncertain-history detection. Trace store file and directory barriers before clean seal. Keep process-kill and actual power-loss guarantees separate.
- test_provider_hooks.py: pure projection returns a new response containing only allowed fields, strips all supplied absolute expiry/id_token/headers, validates aliases and now+duration bound. Instrument all maintained paths, including swallowed exceptions and zero-parse malformed replies. A malformed response after an upstream call must latch failed even when local token writes are zero.
- test_oauth_http.py: exact section9 path/method table including automatic HEAD, no DCR/CIMD/revoke, metadata none/S256 only, exact challenge resource, duplicate/unknown ingress refusal, actual maintained consent/code/refresh flow. Verify guarded /mcp405 and capability-only transfer route.
- test_core_composition.py: original loader refuses oauth_service, service loader refuses local_test; trusted Principal/core/AuthConfig equality and base-state mismatch reject before effects. One manager and one adopted CoreClient, no auth gate held during broker/transfer work. Preserve local_test descriptors/results/route bytes.
- test_safe_errors.py: do not treat intended successful synthetic token/code/state fields as leakage; inject separate sentinels in error-only inputs and assert zero diagnostic/reflected output. Check response_bytes at bound/+1, no partial response, no plaintext marker/key digests in events.
- test_resources.py: distinguish hook acceptance-size bound from network allocation bound; measure waiter growth, active operation, frozen failed-settlement memory and disk flush/manifest work. Verify parent §12.1 source read/header floors and mandatory waiter caps; no slicing huge allocated chunks as an allocation proof.

Before A, Q freezes the §5 fixture matrix/counters and source-qualified oracles; after implementation Q/root performs the mandatory actual-candidate comparison before B. Gate/trust/barrier costs remain explicit. Prebuild harnesses establish maintained seams and exam integrity only; they never substitute for candidate adapters. Protected core README pins parent888c677, execution command correction282dc1a and its MANIFEST; package-relative imports require unittest discover -t . from repository root. Reuse its immutable-copy constructors, socketpair/Session.closed lifetime, original ordinary baseline and expected-red prebuild conventions; do not change protected exams or currently building core source. Auth integration amendments wait for accepted008 B.
O7 exact future packaging boundary: add optional project extra named remote, with fastmcp-slim[server]==3.4.9 and Authlib==1.9.1 while preserving base dependency mcp==1.30.0 and the ordinary package entrypoints. The reviewed lock at docs/mcp-worker/cowork-auth-requirements.lock must pin all transitive versions/artifact hashes, target Python/platform, source indexes and resolver command. Resolve into a new isolated environment only under later authorization; never update installed releases, default interpreter or research venv. Ordinary startup must work with only the original base dependencies. A conflicting resolution returns O7, never relaxes pins or silently upgrades MCP. Research Python3.14 captures do not establish compatibility with every Python>=3.12 supported by the project; target interpreter is a lock/QA gate.

Current handback checks are document-only: confined files, preserved historical AC/defect/history/group IDs, JSON block syntax and source hashes. No auth/core acceptance, tests, worker, installation, commit, activation or D/A self-score is claimed. Root reviews the isolated diff and technical gaps before copying; independent QA remains the only D/A authority.

Root O7 mechanical correction at a93f48b: the server extra above is required; plain slim failed actual provider import. Isolated auth-dependency-lock/REPORT.json records65 packages with exact research-version equality and successful provider/storage imports on CPython3.14.6/macOS arm64. Candidate hash lock6099038ad1842f32dd9e4b54946dd3494cb97027851d71c8a066abc6f784cb88 is dependency evidence only; ordinary environments were not modified. Independent D/A/B and final lock incorporation remain required.
