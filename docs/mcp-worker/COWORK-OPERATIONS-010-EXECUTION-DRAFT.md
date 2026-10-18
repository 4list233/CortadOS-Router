<!-- PURPOSE: Bound a future implementation/exam package for operator setup.
     CONTRACT: RT-COWORK-OPERATIONS-110 v2, implementing proposed010.
     DEPENDS: Accepted amended009 interfaces and accepted008, independently frozen exams.
     CEILING: This authoring grants only two new documents; no dispatch or activation. -->
# Cowork operator setup — execution proposal

```yaml
contract_id: RT-COWORK-OPERATIONS-110
contract_version: 2
status: draft
owner: Forest
orchestrator: root
implements: RT-COWORK-OPERATIONS-010
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
dispatchable: false
capability_tier: stateful integration with filesystem and multiprocessing expertise
source_attempt: none authorized by this proposal
```

The parent `COWORK-OPERATIONS-010-DRAFT.md` owns the only type/API/state registry, O0–O9, and atomic obligations R010-01–18. This execution proposal introduces no competing types. Its acceptance sentences are verbatim parent ACs. All execution choices below are [FIXED] proposed boundaries; unresolved decisions are [ESCALATE], not builder latitude.

## 1. Current author scope and evidence

Current scope_paths, two existing isolated copies only:

```text
/Users/velox/.cortados-builds/cowork-operations-contract-revision/COWORK-OPERATIONS-010-DRAFT.md
/Users/velox/.cortados-builds/cowork-operations-contract-revision/COWORK-OPERATIONS-010-EXECUTION-DRAFT.md
```

Current acceptance_paths: empty. Everything else is read-only, including Router source,008/009, exams and evidence. No test/helper source, temp evidence file, network, install, secret read/generation, registration, service/listener/plist activation, permissions change, worker/model/provider call or git mutation is authorized. Reading dependency source is not importing or executing it. The current cwd contains the two copied drafts; their original author history is retained. No attempt ledger is reset.

Parent O0 pins actual009v5 parent e9b2c0d1c22954da894643b8638ab9e43aa22f2b7e9fef03df2f10087f022174 and execution 83052dcde6977750ee633d5a6fb7c179e30f466c75181981ed442eaec94d142b, with independent auth D=0/33 evidence and no A/source acceptance. The prior author's initially matched v4 hashes and failed final unchanged-hash check remain historical in O0. This revision reconciles v5 rather than retaining the obsolete v4 pin. Core008 v5 candidate3 is frozen under independent B/current full host runner; two reported source causes remain to correct, no core acceptance. Both valid failed implementations, setup blocks, C1–C10, host/measurement/IPC failures and later observations remain in the unchanged ledger. Operator010/110 D/A/B remain pending. No protected exam was changed or run.

Governing paths read for authoring: `/Users/velox/cortados/contracts/CONVENTIONS.md` and `/Users/velox/cortados/context-staging/research/ai-coding-std-workflow/CONVENTIONS.md`, `RUBRIC.md`, `ORCHESTRATION.md`, `PLAYBOOK.md`, `standards/00-ENGINEERING-STANDARD.md`; Router `docs/mcp-worker/COWORK-MCP-008.md`, `COWORK-MCP-008-EXECUTION.md`, `COWORK-MCP-008-DECISIONS.md`, `COWORK-MCP-008-ROOT-REVIEW.md`, `COWORK-AUTH-009.md`, `COWORK-AUTH-009-EXECUTION.md`, `CLAUDE-APP-CONNECTOR-PLAN.md`, `INSTALLATION.md`, `INDEX.md`, `USAGE.md`; V/auth-dependency-lock/README.md and bounded maintained source excerpts. Parent O3 records repeatable owner searches and observed callable signatures. Current evidence additionally includes project docs/DECISIONS.md, V/auth-v5-D review/manifest/readiness, USER-LIVE-POLICY-APPROVAL.json, OFFICIAL-OAUTH-EXPIRY-CHECK.md and the retention proposal/activation choices; exact hashes are in O0.

## 2. Future ownership proposal — not current writable scope

All paths in this section are relative to R, the absolute Studio checkout in parent O0. Parent directories `integrations/mcp`, `docs/mcp-worker` and `acceptance` exist. The new operations acceptance subtree does not exist and has no committed fixtures; that is an explicit A blocker. The proposed009 source files are not accepted implementations today. Root first accepts008, amends/reviews009 as necessary, and transfers these exact owners exclusively. No overlapping concurrent source dispatch is permitted.

One bounded implementation assignment can extend the six existing proposed009 owners, in this dependency order:

| Exact future source path | Proposed ownership delta |
|---|---|
| integrations/mcp/remote_auth_config.py | OperatorConfig validation; no file/secret read effects. |
| integrations/mcp/remote_auth_storage.py | Callable-under-lock constructor, initial purpose, six maintained setup calls and initial seal predicate; sole marker/lock/barrier owner. |
| integrations/mcp/remote_auth_provider.py | Extract make_provider from009 constructor wiring; no new issuance/serialization. |
| integrations/mcp/remote_auth_runtime.py | Pass unresolved callable to storage; initial-only offline composition using same provider factory. |
| integrations/mcp/remote_supervisor.py | Existing child startup passes unresolved callable through runtime; top-level initial spawn target only; no new supervisor/control protocol. |
| integrations/mcp/remote_service.py | Pure/nonsecret check, bounded top-level resolver, CLI parsing and one-shot provisioning spawn. |

The following is a verbatim mirror of parent O2 for review; O2 remains the sole canonical API registry. All signatures are proposals requiring O9 adoption, not existing imports.

| Owner | Exact proposed callable/config shape |
|---|---|
| remote_auth_config | `load_operator_config(raw:bytes,*,auth_config:AuthConfig)->OperatorConfig` — pure schema/cross-reference validation |
| remote_service | `validate_operator_setup(*,operator:OperatorConfig,base_raw:bytes,core_raw:bytes,manifest_raw:bytes,auth_config:AuthConfig)->None` — nonsecret release/filesystem/config/dependency/native-manifest validation only; no runtime enter, state mutation, secret read, network or listener |
| remote_service | `resolve_private_secrets(*,operator:OperatorConfig,auth_config:AuthConfig)->ResolvedAuthSecrets` — trusted module-level function; only invoked in spawned auth/provision child after AuthStorage lock acquisition; fixed-safe exceptions |
| remote_auth_storage | `AuthStorage(*,config:AuthConfig,resolve_secrets:Callable[[],ResolvedAuthSecrets],dependency_sha256:str,on_failure:Callable[[],None],purpose:Literal['restart','initial']='restart')` replaces D0.13's secrets operand; all existing methods remain. Add read-only `resolved_secrets:ResolvedAuthSecrets`, accessible only by entered owner while lock held; no serialization. `client_storage` remains inaccessible before the dirty barrier. |
| remote_auth_runtime | `AuthRuntime(*,config:AuthConfig,resolve_secrets:Callable[[],ResolvedAuthSecrets],dependency_sha256:str,on_failure:Callable[[],None])` replaces D0.10's secrets operand; runtime always constructs AuthStorage with purpose='restart'; provider receives storage.resolved_secrets only after enter. |
| remote_auth_provider | `make_provider(*,config:AuthConfig,secrets:ResolvedAuthSecrets,client_storage:AsyncKeyValue,identity_client:httpx.AsyncClient,on_failure:Callable[[],None])->StrictGitHubProvider` — single extraction of 009 D0.16 constructor wiring; no routes/enter/network/registration; no second issuer |
| remote_auth_runtime | `async provision_initial(*,config:AuthConfig,dependency_sha256:str,resolve_secrets:Callable[[],ResolvedAuthSecrets])->None` — offline composition using AuthStorage purpose='initial', make_provider and maintained register_client; no AuthRuntime enter, core, listener or token flow |
| remote_supervisor | `run_initial_provision(*,auth_config:AuthConfig,dependency_sha256:str,resolve_secrets:Callable[[],ResolvedAuthSecrets])->None` — top-level synchronous multiprocessing spawn target calling asyncio.run(provision_initial(...)); no CoreRuntime or RemoteSupervisor instance |

Adopt009v5 D0.11/D0.17 service-core seams in remote_config/remote_runtime/remote_http only through separately gated009 B2. Those files are dependencies, not additional010 scope. validate_operator_setup delegates to their validators; no oauth_service-to-local_test translation or second HTTP composition. The factory forwards D0.16 exactly, including config/on_response_failure and the canonical guarded identity client. Private-file custody is conditional on O010-04 final setup review. Retention belongs to the separate existing-core-owner extension and adds no010 source path.

No new source module, source utility file, dependency, pyproject change, shell wrapper, plist installer or retry scheduler is included. If accepted009 owners cannot remain strictly below500 physical source lines including blanks/comments, the builder returns the exact seam/line inventory to root; it does not split files privately. In particular, do not move marker work into remote_service to meet a line ceiling.

Root-only future documentation incorporation paths: `docs/mcp-worker/INDEX.md`, `docs/mcp-worker/INSTALLATION.md`, `docs/mcp-worker/USAGE.md`, `docs/mcp-worker/CLAUDE-APP-CONNECTOR-PLAN.md`. Root decides the canonical010/110 contract location under the project's single contract-home convention before dispatch. These isolated draft filenames are not an index amendment. No live release/config/secret/state/LaunchAgents path is a builder scope_path. Activation is a later distinct reviewed operation.

Independent Q-owned future acceptance_paths (NEW, missing fixtures, not writable by source builder):

```text
acceptance/cowork_operations/__init__.py
acceptance/cowork_operations/test_contract.py
acceptance/cowork_operations/test_operator_config.py
acceptance/cowork_operations/test_secret_spawn.py
acceptance/cowork_operations/test_initial_provision.py
acceptance/cowork_operations/test_initial_faults.py
acceptance/cowork_operations/test_service_lifetime.py
acceptance/cowork_operations/test_installed_compatibility.py
acceptance/cowork_operations/test_resources.py
acceptance/cowork_operations/test_release_gates.py
acceptance/cowork_operations/support.py
acceptance/cowork_operations/fixtures/operator.json
acceptance/cowork_operations/fixtures/launchd-manual.plist
acceptance/cowork_operations/MANIFEST.json
acceptance/cowork_operations/README.md
```

Q creates/commits these only in a separately authorized exam assignment. Fixtures use synthetic keys generated only then in disposable authorized roots; no production credentials or retained secret values in captures. `support.py` is exam-only fixture/process instrumentation, never imported by runtime. No modification of frozen008 or009 protected exams is implicit; Q adds targeted operations tests that call accepted owners and runs the inherited suites unchanged.

## 3. Gate sequence and amendment review

1. Root reviews parent O9 exact API changes, initial-only auto_create exception, lock preprovision requirement and resolver choice. Any disagreement stays [ESCALATE];010 does not silently amend009. Independent D reviews the complete resulting010/110 and affected009 dependency text.
2. Root supplies accepted source/version hashes and exclusive source ownership. Q freezes maintained source/lock hashes, exact synthetic captures, expected-red new imports/CLI flags, fixture hashes and failure oracles. Missing credentials or unqualified filesystem are blocked prerequisites, not valid failed builds.
3. Independent A verifies the full runnable package, all paths and protected exams, actual target interpreter/lock/barrier qualification, and no unresolved build decisions. Synthetic policy parameters suffice for local implementation; no invented live policy defaults unblock A.
4. A future authorized source builder implements only the six listed owners. No installed environment edits or live secret use. It returns a confined diff, per-AC evidence, source line inventory, source/lock/fixture hashes and measurements.
5. Root/Q runs B independently against actual candidate and installed isolated package after explicit test/install authorization. Completion alone is insufficient. Preserve inherited baseline exceptions and every valid attempt in the unchanged ledger; two valid failures return design_error under governing convention, not a fresh contract/task group.
6. Root prepares reviewable local results and precise remaining activation actions before seeking Forest's final activation approval. Settled account/app policy, decimal payload limits and seven-day direction are recorded, not asked again. Completion semantics and concrete secret custody remain pending; app creation, provisioning, root/lock creation, registration and listener/network/service actions require their later exact scope.

No independent D/A/B has been run by this author. No source dispatch or model selection is asserted. The user's current role instruction prohibits nested workers; future worker workflow requires its own authorized dispatch and tool schema discovery.

## 4. Verbatim acceptance criteria and executable exam plan

Commands here are prospective, not executed. From R, use the exact qualified isolated interpreter, represented by `QA_PY=/absolute/qualified/venv/bin/python`; never the incidentally installed python. Run with `-B` to avoid source bytecode writes. Test fixtures accept only their synthetic roots and no external connections. MANIFEST/README pins exact command/environment and every generated output location before A.

| AC | Exact acceptance sentence |
|---|---|
| AC010-01 | The reviewed diff contains only authorized files and preserves dependency hashes, ownership gates and attempt history. |
| AC010-02 | Invalid or mismatched operator configuration refuses before secret reads, state writes, runtime entry or network activity. |
| AC010-03 | Spawn traces show one child-only resolution after lock acquisition and zero secret sentinels in parent memory captures, argv, environment, IPC, diagnostics or task files. |
| AC010-04 | Every noninitial, corrupt or uncertain root is refused with unchanged bytes and zero registration calls. |
| AC010-05 | Owner traces show one storage lock/marker owner, six maintained collection setups, one maintained registration and no replacement serializer or store. |
| AC010-06 | Fault traces prove the initial seal barriers and preserve every partial state without retry or a false successful-close report. |
| AC010-07 | Qualified restart retains exact identity/config/dependency binding and performs zero registration or repair. |
| AC010-08 | One supervisor controls admission and close, while every auth/close failure suppresses automatic relaunch. |
| AC010-09 | Installed-package validation preserves ordinary MCP, training and the existing private route without activating the candidate endpoint. |
| AC010-10 | Actual candidate measurements report secret-read, storage, registration, barrier and launch overhead separately with raw counters and uncertainty. |
| AC010-11 | Root's release record distinguishes every answered choice and actual provider/Claude/file/native gate from synthetic local evidence. |

Exact command map (each test module contains named assertions for all indicated ACs; collection alone is not pass evidence):

```sh
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_contract
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_operator_config
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_secret_spawn
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_initial_provision
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_initial_faults
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_service_lifetime
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_installed_compatibility
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_resources
"$QA_PY" -B -m unittest acceptance.cowork_operations.test_release_gates
"$QA_PY" -B -m unittest discover -s acceptance/cowork_operations -t . -p 'test_*.py'
"$QA_PY" -B -m unittest discover -s acceptance/cowork_auth -t . -p 'test_*.py'
"$QA_PY" -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py'
```

Existing accumulated Router/MCP/training suites use their accepted inventory and pinned original interpreter per lane; no reduced inventory substitutes for them.009's recorded ordinary baseline785 tests/784 pass/1 live whole-label-hash parity failure remains historical evidence until root supplies a newer independently reviewed baseline. Historical007 relocated parity4/4 is separate, not permission to weaken an exam or stop nightly appends.

| Module / AC | Ground truth and mandatory scenarios |
|---|---|
| test_contract /01 | Exact parent/execute AC equality, all R IDs mapped, dependency hash mismatch refusal in package preflight, owned path inventory, no additional modules/marker writer/credential DB/issuer/broker/scheduler; attempt ledger links unchanged. Independent AST/call-site inventory complements tests. |
| test_operator_config /02 | O8 valid object and every single-field invalid mutation; unknown/duplicate keys, bool integers, missing fields, path overlap, reference collisions, hash/path/CLI mismatch, unqualified interpreter symlink or writable release; secret open/provider/core/listener counters all zero for check and validation failures. Check success remains nonsecret only. |
| test_secret_spawn /03 | Actual multiprocessing spawn from installed module; parent never invokes resolver; child PID and lock-owner PID equal; second concurrent child refuses before resolver. Bind a top-level partial without secret operands and inspect its pickle and all control frames. Synthetic files with exact/+1 lengths, invalid encodings, wrong uid/mode/ACL, symlink ancestor, hardlink, special file, pre/post stat race, missing/replaced file and identical decoded signing/storage key. Record process-visible argv/env, intercepted parent objects/buffers, raw LogRecords/stdout/stderr and task-tree scan for unique sentinels. Parent fixture writer retains no secret literals; use a separately isolated fixture process and one-way digest comparison for scans. This is instrumented evidence plus source review, not proof against all memory/privileged inspection. |
| test_initial_provision /04,05 | Real maintained FileTree/Fernet/Pydantic stack, all six directories and six metadata files captured through maintained setup; exactly one persisted public client, no other rows, correct excluded-field/default behavior. Instrument one register_client call, one readback and zero OAuth/identity requests. Refuse lockless root, populated/empty credentials directory, pending file, clean/dirty/corrupt marker, unknown file, symlink or second invocation; compare before/after bytes, inodes and directory entries, zero writes/registration on preexisting refusal. No hand-serialized token fixture as success oracle. |
| test_initial_faults /06 | Inject before/after every marker write/flush/full-flush/rename/dir barrier, collection setup await, registration await, readback, identity close, file flush/deepest-directory flush, manifest scan and final seal. Kill actual child at those boundaries. Preserve exact leftover bytes; no retry and no cleanup. Initial clean0 only after verified row/settlement/barriers. A final-barrier error returns failure even if the final disk image looks clean. QA accepts009's enumerated possible all-process-loss disk outcomes; never requires impossible universal uncertainty detection. |
| test_service_lifetime /07,08 | Initial clean0→dirty1→clean1→dirty2; client ciphertext/registration unchanged across restarts; wrong keys/config/lock digest and generation overflow refuse. Delete collection metadata under a stopped synthetic root and prove no lazy setup repair. Bad secret fails before provider/admission. Child death, control EOF, disable, auth failure, unproven close, SIGTERM and parent shutdown preserve009 job ownership/control ordering; kill/join only through supervisor. Parse plist and reject KeepAlive/RunAtLoad true or any scheduling/watch/socket key. No restart loop; no parent CoreRuntime shutdown inferred from frontend exit. |
| test_installed_compatibility /09 | New isolated wheel/venv import with -I from immutable release, base-only ordinary package still imports/works without remote extra; pinned dependencies/file hashes and no source-cwd imports. Compare ordinary entry/config/state/tool schema and training ownership against root's read-only baseline. Compare private route configuration through supplied sanitized baseline/candidate records; no route mutation or live networking in this local exam. Actual host unchanged-route observation is later root release evidence. |
| test_resources /10 | Actual candidate measurements specified in section5; every sample has counter/timing/raw-error evidence and no hidden setup excluded from totals. |
| test_release_gates /11 | A synthetic pending record never qualifies as released. Audit root's supplied approval/evidence inventory without reading credentials; missing genuinely pending choice or separate retention acceptance, provider lifetime, public-client compatibility, transfer materialization, native all-tool isolation or final approval leaves BLOCKED. This test validates evidence completeness, not external truth. |

Exams requiring permission to kill child processes, create synthetic credential roots, install a package or bind an ephemeral loopback listener receive that exact bounded permission in the future Q assignment. No external-provider/model call occurs in local exams. Missing permission is reported as blocked; it does not broaden the test harness automatically.

## 5. Measurement plan and numerical boundaries

Before A, Q pins actual fixture files/versions, target OS/CPU/RAM/filesystem/mount/interpreter, source/lock hashes, instrumented method boundaries and exact fault schedule. The existing maintained baseline and009's seven method vectors remain unchanged; registration and setup are separate paths. No candidate performance pass can be produced before candidate code exists.

After implementation, before B:

| Measurement | Repeatable input / required report |
|---|---|
| Initial flow | 30 independent lock-only synthetic roots; record full parent+spawn+child elapsed, CPU, RSS high-water/tracemalloc separately, opened files/bytes, three secret reads, six setup calls, one registration put/readback, logical versus backing I/O, full-flush/fsync/rename counts and spans. Never reuse a failed root. |
| Restart/stop | 30 provisioned roots; two qualified lifetimes each. Separate immutable release validation, spawn/import, lock, resolver, preflight/decrypt, dirty seal, provider/manager enter and full shutdown/clean seal. End-to-end totals include all costs. |
| Refusal | 30 samples each missing secret, bad-key, noninitial root, corrupt marker, second-process lock, missing metadata and wrong digest. Report zero unintended writes/provider requests/admission, preservation hashes and refusal latency. |
| Bounded reads | Synthetic upstream bound4096 at4095/4096/4097; signing31/32/33; storage43/44/45 bytes. Metadata/config validation and actual file-read bounds checked before allocation. Change configured upstream bound to1024 and65536 and repeat; numbers are exam inputs, not deployment defaults. |
| Filesystem faults | Each deterministic kill/error boundary at least once for ordering; repeat scheduling-sensitive spawn/lock/close cases30 times. Record attempted and completed barriers separately. Process-kill evidence is not power-loss evidence. |
| Cold check | 30 installed-package executions, proving zero credential/state/network calls; report validation bytes/stats/hash cost, interpreter startup and imports. |
| Service parity | Existing009 complete-auth/core fixtures unchanged plus operations startup; include all inherited gate/transport/barrier overhead rather than subtracting setup to make a pass. |

Report sample count, p50/p95/max wall/CPU, failures/refusals, RSS units and measurement method. Darwin high-water RSS is cumulative; no per-operation allocation claim follows from it. No latency/RSS ceiling is invented. Regression in owner counts, unbounded reads, secret leakage, retries or missing barriers fails correctness independent of speed. Numerical deployment budgets/SLOs require root review of measured candidate evidence. Key independence is a separately attested provisioning fact, not inferred from low latency or unequal hashes.

The semantic bound on resolved secret memory is one upstream file <=configured bound plus32+44 key bytes, before documented decode/library copies. Python/runtime/crypto/HTTP/process allocations remain additional measured memory. Three file reads is a logical count; fstat/ACL/path validation/short reads and encrypted marker decrypt are reported separately. Re-reading nonsecret config in parent/child to verify immutable identity is not a secret read. No polling helper, watcher, key caching daemon or periodic credential scan is added.

## 6. Source-backed near misses for independent review

- `register_client` is an upsert through the maintained client store. Pre-checking only client absence is insufficient; O4 requires the whole initial root predicate under the one lock before any registration.
- `auto_create=False` prevents missing directories but maintained metadata creation can still occur during lazy collection setup. Normal startup validates all six directory/metadata pairs before maintained access; it does not call setup to heal a missing file.
- Using provider defaults can derive signing/storage material, create a different root or treat decryption failure as a cache miss. O2's shared factory always receives explicit stable independent keys and the existing strict encrypted store.
- A top-level resolver that the parent invokes before spawning still leaks secrets across a process boundary. The callable is transported unresolved; AuthStorage invokes it after locking in the child.
- A parent-provisioning shortcut using the same private files violates child-only resolution. Initial provisioning has its own one-shot spawned target in the existing supervisor module, without constructing a live supervisor or core.
- A successful clean rename followed by failed directory flush is not successful close. Do not “fix” it by a second dirty overwrite, automatically restart, or claim all-process-loss detection that local hashes cannot provide.
- A launchd plist with KeepAlive=false but RunAtLoad=true still restarts after login/boot. Both are false; scheduled/watch triggers are absent. External service policies remain part of release review.
- A config file that embeds a secret and is later called “operator-only” still violates the argv/environment/task/config boundary. The closed record permits references only; raw secret bytes exist solely in separately provisioned private files and child memory.

## 7. Document-only checks and handback

Current authorized checks: ensure the cwd contains exactly the two requested files; UTF-8/Markdown fence balance; parse embedded JSON with duplicate/nonfinite rejection; compare AC rows byte-for-byte; map every R010 ID to an AC; check whitespace and hash both outputs; recheck009 source hashes. No secret or source execution is needed. These are author hygiene checks, not D/A or implementation validation.

Revision2 author checks: the workspace contains only the two requested Markdown files; all28 O0 source/evidence hash rows match the read-only bytes, including actual009v5. The11 AC rows and O2 API mirror match byte-for-byte; all18 R010 obligations map to ACs. The one embedded JSON example parses with duplicate/nonfinite rejection; fences and whitespace check cleanly. The six proposed operator source paths are absent beneath the verified integrations/mcp parent, so none is described as an implemented import. No tests, diagrams, imports, provider calls or state operations were run. Operator D/A/B remain pending.

Prospective diagram rendering uses an already authorized local Mermaid CLI with no install/network, after Q extracts diagrams into its authorized evidence root. Example command shape: `mmdc -i /ABS/Q/diagram-1.mmd -o /ABS/Q/diagram-1.svg`. This turn does not create those files or claim rendered diagrams. Rendering remains an independent D check.

Future builder handback includes exact confined diff, per-file physical line counts, dependency/source/fixture hashes, per-AC raw ground truth, all failure/settlement traces, installed package provenance, measurement report, unresolved decisions and preserved attempt links. Root records accepted/rejected/inconclusive only after review; no completion-as-correctness inference. No commit/install/publication follows automatically.

## 8. Unresolved choices and activation boundary

| Decision | Owner / status |
|---|---|
| Parent O010-01 | Root/Q: accepted008 B,009v5 O9 amendment review plus A/source/B, operator D/A/B and exclusive source ownership; [ESCALATE]. Existing009v5 D0 is evidence only for its unmodified pins. |
| Parent O010-02/03 | Already approved:4list233 dedicated OAuth App/no repository permissions; decimal100000000/1000000000/10000000000 bytes; automatic7-day completed-task cleanup. Required qualification: actual app/subject/client. Awaiting user choice: completion semantics; separate retention extension, no auth/operator TTL substitute. |
| Parent O010-04/06 | Forest/root/Q: concrete final setup review for private-file versus qualified alternative custody remains unapproved; resolver/secret custody/root/lock permission, ACL/crash policy, target barriers and independently reviewed recovery/rotation/restore/reset/deletion; [ESCALATE]. |
| Parent O010-05 | Forest/root: release paths/interpreter, service mode/label/port and server ingress measurements; [ESCALATE]. |
| Parent O010-07/08 | Root/Q: actual finite provider lifetimes, Claude public-client OAuth, binary transfer materialization and native all-tool isolation; [ESCALATE]. |
| Parent O010-09 | Forest: exact final activation after root's reviewable local completion; [ESCALATE]. |

No app creation, permission expansion, secret provisioning, registration, live configuration, listener, launchd job, networking change or candidate8443 activation is performed or approved by this package. Ordinary MCP, training, native subscription ownership and private443→127.0.0.1:8432 remain unchanged. Cowork URL+OAuth is still the intended experience; SSH, companions, API-key fallback and automatic connector/file/history inheritance are not substitutes.


## Root incorporation note — document proposal only

The authoring-time core status above is historical. Candidate3 host896methods/10failureevents/1error/0skips and independent11-row B have completed: four failed rows, two product causes. Root recorded rejection and narrow exam-owner correction authority at0ae0cf4; source remains unaccepted. No010 D/A/B or009 amendment is accepted. The ROOT-REVIEW hash in O0 identifies the inspected62fcd07 bytes, verified with git show; root subsequently appended the final rejection/exam grant. All other27 cited source/evidence hashes still match. This annotation changes provenance/current status only, not the proposed API, AC or deployment behavior.
