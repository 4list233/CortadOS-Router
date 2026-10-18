<!-- PURPOSE: Define prospective bounded auth build, exam and activation ownership.
     CONTRACT: RT-COWORK-AUTH-009 v2; draft, not dispatchable.
     DEPENDS: COWORK-AUTH-009.md, accepted RT-COWORK-MCP-008 and independent D/A.
     CEILING: Current turn writes only these two drafts; no code, tests or commit. -->
# Cowork future authentication execution package

```yaml
contract_id: RT-COWORK-AUTH-009
contract_version: 2
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

[FIXED] Future dispatch requires accepted core implementation plus auth-specific independent D=0 then A=0, committed protected exams, resolved applicable O rows, approved dependency lock and nonoverlapping scope. Core D0 at 1637088 is not accepted production; QA is still authoring core exams. Local synthetic auth design and exam specifications can proceed without actual GitHub identity/file-policy choices. Actual live access is never a prerequisite for writing synthetic exams.

[FIXED] The parent owns all schemas, API signatures, examples E1–E30, R rules and contradictions. This execution document references them instead of copying nested examples. Any change in observable semantics returns to root for a versioned amendment before build.

Future worker dispatch MUST use the existing group, absolute Studio cwd `/Users/velox/.cortados-builds/cortados-router-repo`, a stable per-attempt request_id and configured timeout. Root MUST inspect an existing job before resubmitting. Root MUST call context_requirements before route/delegate and supply the pinned parent, exact scope, accepted-core hashes, resolved O decisions, exam hashes, constraints and verification in task_evidence. S1 recommendation does not execute; delegate accepts the attempt. No automatic retry/quota fallback. Root tracks job_status/job_result and records evidence-based job_feedback. Worker MCP and permissions are explicit grants, never inherited connectors. No nested workers in this assignment.

## 2. Prospective source scopes and ownership

All paths in this section are proposals, NOT current grants. Verified existing parents: `integrations/mcp/`, `docs/mcp-worker/`, repository root, `acceptance/`. `acceptance/cowork_auth/` is a proposed NEW directory. Source MUST remain <500 physical lines per responsibility; no minification/generic utils split. New source headers MUST state PURPOSE/CONTRACT/DEPENDS/CEILING. No broad globs.

Package B1 owns thin auth policy/lifetime after its separate gates:

| Exact prospective scope_path | Status / single responsibility |
|---|---|
| integrations/mcp/remote_auth_config.py | NEW; closed AuthConfig validation; no secret resolution effects. |
| integrations/mcp/remote_auth_provider.py | NEW; StrictGitHubProvider's two maintained hooks and response validation. |
| integrations/mcp/remote_auth_claims.py | NEW; strict claims and credential-free facts projection. |
| integrations/mcp/remote_auth_runtime.py | NEW; one process gate, private store/key lifetime, exclusive lock and fail-closed state. Owns section 8 lifetime marker/barriers, subject to independent qualification. |
| integrations/mcp/remote_auth_errors.py | NEW; dependency-log suppression, bounded safe OAuth response filtering and AuthEvent emission. |

B1 MUST NOT duplicate OAuth protocol transitions or maintain token records. Root MUST return a scope amendment before splitting any of these responsibilities into additional paths.

Package B2, sequential after accepted core and B1, owns production composition and packaging:

| Exact prospective scope_path | Status / constrained change |
|---|---|
| integrations/mcp/remote_config.py | FUTURE core file; only approved O2 service validation seam. Existing local_test path preserved. |
| integrations/mcp/remote_runtime.py | FUTURE core file; only approved O2 trusted constructor selection. Existing recovery/locks/job lifetime preserved. |
| integrations/mcp/remote_http.py | FUTURE core file; one HTTP composition owner and guarded MCP entry; preserve exact core ticket transfer route. |
| integrations/mcp/remote_service.py | NEW; trusted launcher/composition CLI, provider/client lifetime, no transport/transfer reimplementation. |
| pyproject.toml | EXISTING; reviewed optional remote extra only, no implicit ordinary dependency upgrade or default command change. |
| docs/mcp-worker/cowork-auth-requirements.lock | NEW; exact remote resolved dependencies and artifact hashes after separately authorized resolution. Research freeze alone is not a release lock. |

B2 MUST NOT change server.py or __main__.py. Invoke the separate module explicitly, keeping ordinary stdio lazy imports unchanged. No core file ownership transfers until core acceptance is recorded and no core builder holds it. B2 MUST keep existing managers/descriptors/IPC/byte handlers as their sole owners. It MUST NOT alter worker, broker, quota, route, task store, ticket or file-transfer algorithms. A missing disable/supervisor seam returns to root rather than widening scope.

Future CLI proposal (argument syntax to freeze in Q, never run in this turn):

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
| test_auth_lifetime.py (fault lanes) | Kill isolated synthetic subprocess at every observed multiwrite await boundary (callback code put/transaction delete; code delete/upstream/JTI/refresh puts; explicit upstream/new JTI/old JTI/metadata changes; transparent upstream put). Restart must refuse uncertain state under approved O3; corrupt ciphertext/wrong key/missing reference/invalid stored schema cannot authenticate. Cancellation during upstream exchange/store fails lifetime, no retry; gate wait cancellation never starts exchange. Preserve files and distinguish process kill from untested power loss. |
| test_safe_errors.py | R12, AC01/02: inject unique secret sentinel in token/code/refresh/error body/exception/request URL/HTML/redirect/cookie and dependency traceback. Capture log records before handlers, stdout/stderr and every ASGI output byte/header. No sentinel escapes. Validate E7 fixed body, safe challenge, no partial success sent on overflow. Debug-off alone is a negative control. |
| test_core_composition.py | R4,11,13 / O2, AC01/02: accepted CoreRuntime and CoreClient over inherited socketpair; actual manager initialized once, exact 12 descriptors and unchanged transfer route. No broker call before auth; no secret over IPC; no auth gate held during call. Disable closes tickets/streams through core, does not cancel accepted jobs; disconnect/expiry never resubmits. Local_test loader still rejects service mode; no startup DB effect on malformed auth config. |
| test_compatibility.py | AC01/02 integration regressions: stdio startup without remote extra, ordinary argv/schema/result bytes and training unchanged. Existing subscription/quota refusals start zero native jobs and no API fallback. Historical 007 and current training integrity remain distinct lanes. |
| test_resources.py | All R measured boundaries: configured request/response/gate limits at bound and one beyond; query/provider counts, waiting/active memory, disconnect/backpressure; no model calls. Dataset/procedure in §5. |

Q's prebuild command proposal, after independent exam authoring in the pinned existing research environment (not run here):

```sh
/Users/velox/.cortados-builds/cowork-mcp-verification/auth-research-venv/bin/python -B -m unittest discover -s acceptance/cowork_auth -p 'test_*.py'
```

Q MUST make the command runnable without internet, packages, real credentials or live files. In prebuild mode it proves maintained seams and exam integrity; missing future production imports remain explicit expected-red build checks, not full acceptance. Candidate mode MUST exercise the actual future adapters/entrypoint, with no substitute implementation. No “probe passed” converts to candidate B pass.

## 4. Gates and dispatch order

1. [FIXED] Root pins O2–O7 decisions at 495973c and the parent v2 semantics; copies section6 into accepted core before B2. Forest's deployed O1 remains separate, never filled with synthetic values. Parent/contract amendment precedes dependent code.
2. [FIXED] Q performs independent auth D. D0 requires exact callable/schema/ownership closure and rendered diagrams; this author asserts no rubric score.
3. [FIXED] Q freezes and commits synthetic exams, including D-008-01/02 regressions, and performs A. Missing local fixtures, unresolved recovery semantics or unmeasured required budgets block A.
4. [FIXED] Root waits for accepted core B and releases its shared-file ownership. Core D0/A0 alone does not authorize auth integration.
5. [FIXED] B1 implements only its scope; B2 follows accepted B1 and approved sequential seam transfer. Dependency install/resolution requires separately authorized bounded work; no ordinary environment upgrade.
6. [FIXED] Q/root rerun the actual final candidate plus full accumulated system inventory. Builder success narration is insufficient. Root records accepted/rejected/inconclusive feedback against stable job/group and AC IDs.
7. [FIXED] Activation remains blocked until §6 gates. Local acceptance never proves live GitHub refresh, Claude OAuth, native tools or binary materialization.

Full regression requirement: adopt the actual 26-lane accumulated runner described in 008 EXECUTION and `V/provider-preflight.md`, then add core/auth lanes. Q MUST inventory maintained unittest/pytest/standalone/native/plugin/adapter entrypoints and report discovered/executed/skipped/blocked counts at final diff. Starting references, not permission to run:

```text
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py ISOLATED_EVIDENCE_DIR
python -B -m unittest discover -s acceptance/cowork_mcp -p 'test_*.py'
python -B -m unittest discover -s acceptance/cowork_auth -p 'test_*.py'
```

The pinned environment for each lane must match its dependency lock, not the shell's incidental python. The original baseline remains 785 tests / 784 pass / 1 fail, not green. The live whole-label-hash parity failure is retained; historical-007 separately passed 4/4 after three literal path relocations and equal normalized AST. Do not weaken that exam or freeze authorized nightly appends. Candidate verifies ordinary profile equality plus existing label prefix, exam provenance and final live partition checks.

## 5. Resource measurement plan

[FIXED] Baseline is accepted core without auth, same host/interpreter/SDK transport/fixture. Candidate is final auth diff with synthetic maintained GitHub responses and external sockets forbidden. Research raw counts are evidence only, not budgets. Q MUST record machine/OS/CPU/RAM, interpreter, dependency and input hashes, cold/warm labels, raw samples and error/refusal counts.

| Scenario | Samples / observations |
|---|---|
| Cold startup and restart refusal | 30 isolated synthetic roots each; wall/CPU time, RSS peak, file/byte count, startup reads, lock attempts; restart versus initial registration separated. |
| Valid MCP verification | 1000 serial requests after 100 warmups; p50/p95/max elapsed, gate wait and hold time, CoreClient time separately, get/put/delete count, GitHub mock requests per verification. No positive cache; normal maintained /user and /user/repos calls expected. |
| Wrong/missing bearer and owner disable | 1000 each; zero broker/native calls; signature failures cause zero upstream calls; measure bounded refusal response and log bytes. |
| Token lifecycle | 100 independent synthetic authorization/code/explicit-refresh/transparent-refresh sequences; record all store operations and bytes, no implicit retries; compare observed transitions to pinned maintained trace. |
| Contention | Concurrency 1,2,8,32 for valid verify and mixed refresh; gate queue occupancy/RSS/wait time; one active auth operation. Apply configured wait timeout; no unbounded new application queue. |
| Large/adverse | 0,1KiB, configured max and max+1 request/response sizes, slow upstream, disconnect, storage full/corrupt; measure allocation and settle time; no partial secret output. |
| Transfer coexistence | Accepted core's 0/1KiB/1MiB/max synthetic byte cases while auth contends; gate released before transfer/core work; memory tracks existing chunk×streams, no auth copy of payload. |

[FIXED] Root/QA MUST derive and freeze gate_wait_ms, operation_ms, response_bytes and numeric latency/RSS/auth-disk/query budgets from this data and user-visible deadlines before acceptance. Too-small limits reject valid login; too-large limits retain requests/credentials in memory and can exceed Claude's deadline. An unmeasured budget is pending, not a claimed pass. Parent control_bytes remains the core-owned transport bound. Auth disk capacity is separate measured secret-store overhead, not a new file quota owner.

[FIXED] Upstream HTTP timeout at proxy.py:734 is 30 seconds, while GitHub verifier's default per-call timeout is 10 seconds and it may issue two calls. Gate delay and retry-free multi-call duration MUST be measured against the external client 10s/30s requirements. Proposed operation cancellation needs R5 settlement; simply setting a timeout cannot guarantee upstream cancellation. Live provider/network latency is a separately labeled activation measurement, never subtracted to fabricate a local pass. Model tokens/cost/provider inference count MUST remain zero in synthetic lanes; no invented subscription price or quota expiry.

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
| O6 | Q/root: numeric measured bounds before A; final candidate comparison before B; live deadlines at activation. |
| O7 | B2/root: isolated reviewed lock, ordinary parity and existing-index update before integration acceptance. |

No row selects a product, permission, retention or identity alternative. Parent section12 lists technical contradictions requiring root response.

## 8. Revision-specific ownership and verification obligations

Root authority is standalone Router commit495973c. The current user explicitly prohibits nested workers; the general worker workflow applies only to a separately authorized future dispatch. This authoring does not start an attempt or alter the inherited group/ledger. Prior auth author job20d04076-54f8-46a1-b527-4597ca5ad88c and draft feedback23b83eaa-1170-4288-92c7-87da03b6b853 remain draft-only history.

B1 owns the section11 local constructors in its existing five proposed files. remote_auth_runtime owns marker/gate/settlement and no token serialization. remote_auth_config owns pure config/marker/secret record validation. remote_auth_claims owns strict claims/facts. remote_auth_provider owns only maintained factory/authorize hooks and response projection. remote_auth_errors owns fixed errors/events/log suppression. B2's remote_service owns metadata/route adaptation and trusted launch; remote_http alone owns ASGI server/manager/transfer composition. Each source file must remain strictly below500 physical lines including comments/blanks. If this partition cannot meet that ceiling, return a concrete scope amendment before implementation; no generic utils file or second engine.

Additional mandatory protected scenarios, in already listed Q-owned files:

- test_auth_lifetime.py: clean provisioned generation0 -> dirty1 -> clean1 -> dirty2; unchanged pre-provisioned client and still-valid credentials across clean restart. Kill before/after every marker write/flush/rename/directory barrier and every provider multiwrite await; assert exact allowed restart outcome and byte preservation on refusal. Include missing marker with populated namespace, pending file, wrong encryption/signing/upstream key, changed config/lock digest, generation overflow, second process and symlink/hardlink substitution. Failed lifetime never becomes clean. Trace store file and directory barriers before clean seal. Keep process-kill and actual power-loss guarantees separate.
- test_provider_hooks.py: pure projection returns a new response containing only allowed fields, strips all supplied absolute expiry/id_token/headers, validates aliases and now+duration bound. Instrument all maintained paths, including swallowed exceptions and zero-parse malformed replies. A malformed response after an upstream call must latch failed even when local token writes are zero.
- test_oauth_http.py: exact section9 path/method table including automatic HEAD, no DCR/CIMD/revoke, metadata none/S256 only, exact challenge resource, duplicate/unknown ingress refusal, actual maintained consent/code/refresh flow. Verify guarded /mcp405 and capability-only transfer route.
- test_core_composition.py: original loader refuses oauth_service, service loader refuses local_test; trusted Principal/core/AuthConfig equality and base-state mismatch reject before effects. One manager and one adopted CoreClient, no auth gate held during broker/transfer work. Preserve local_test descriptors/results/route bytes.
- test_safe_errors.py: do not treat intended successful synthetic token/code/state fields as leakage; inject separate sentinels in error-only inputs and assert zero diagnostic/reflected output. Check response_bytes at bound/+1, no partial response, no plaintext marker/key digests in events.
- test_resources.py: distinguish hook acceptance-size bound from network allocation bound; measure waiter growth, active operation, frozen failed-settlement memory and disk flush/manifest work. Missing bounded-network seam or finite waiter cap is blocked A, never a pass.

Before A, Q records an isolated baseline manifest with hardware/OS/filesystem, Python/lock/source/fixture hashes, fixture sizes/concurrency, exact command, warmup/sample counts and raw measurements. EXECUTION section5 is the reproducible sampling protocol. For prebuild, maintained methods plus independent exam-owned harness characterize seams; they do not substitute for candidate adapters. Q/root must freeze numeric budgets and cap semantics in a versioned amendment before A. After B implementation, rerun same fixture matrix on actual candidate and compare to accepted core without auth. Report p50/p95/max, RSS peak and growth, bytes/operations, upstream mock calls, wait/hold/settlement time, refused counts and total SQL separately. Baseline measurements do not prove candidate compliance.

O7 exact future packaging boundary: add optional project extra named remote, with fastmcp-slim==3.4.9 and Authlib==1.9.1 while preserving base dependency mcp==1.30.0 and the ordinary package entrypoints. The reviewed lock at docs/mcp-worker/cowork-auth-requirements.lock must pin all transitive versions/artifact hashes, target Python/platform, source indexes and resolver command. Resolve into a new isolated environment only under later authorization; never update installed releases, default interpreter or research venv. Ordinary startup must work with only the original base dependencies. A conflicting resolution returns O7, never relaxes pins or silently upgrades MCP. Research Python3.14 captures do not establish compatibility with every Python>=3.12 supported by the project; target interpreter is a lock/QA gate.

Current handback checks are document-only: confined files, preserved historical AC/defect/history/group IDs, JSON block syntax and source hashes. No auth/core acceptance, tests, worker, installation, commit, activation or D/A self-score is claimed. Root reviews the isolated diff and technical gaps before copying; independent QA remains the only D/A authority.
