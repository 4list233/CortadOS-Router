<!-- PURPOSE: Record planner evidence and unresolved choices for Cowork008.
     CONTRACT: RT-COWORK-MCP-008 draft; this record does not approve deployment.
     DEPENDS: Independent QA captures and the existing accepted001–007 package. -->
# Cowork008 planner review

Forest authorized OAuth, generated task folders, binary file transfer and existing subscription
workers on 2026-10-17. General tasks do not require a repository. Local scoped commits remain
authorized; no push or alteration of the shared checkout's Git state is authorized.

## Current evidence

- Draft author job `a21a86cf-d936-4be9-af76-ddec5048e479` completed under group
  `53f0ea39-a61e-4307-9c2c-466c1b4b0bd1`. Planner accepted the two requested draft documents,
  not an implementation or dispatch approval. Feedback ID `11947912-9f05-4687-92da-ceb9703fa959`.
- Revision author job `5c236b3c-ffc4-4526-ad03-dbf5da0ac957` completed under the same group with
  exact draft API/scope choices, separate prebuild and Cowork activation gates, and persisted native
  policy identity. Root read all three documents and accepted the draft-only deliverable through
  feedback `3f278224-bfa1-49c5-a3f2-318485c8ba0f`. It is not a dispatch or D/A approval.
- Independent native sandbox capability capture passed 13 assertions on Codex0.163.0. Task
  read/write succeeded; synthetic sibling/home/symlink/tmp read/write and loopback network failed.
  This is shell-subcommand evidence, not proof of actual model tool enforcement. Raw evidence:
  `/Users/velox/.cortados-builds/cowork-mcp-verification/native-sandbox-qa/review.json`.
  SHA-256 `8d7b5b53b04a87d32781b2be1e4ed320c2f9d2e47cb7431bcbdb7b3d246514a9`.
- Ordinary worker and training profiles must remain unchanged. A remote profile uses explicit
  `default_permissions` and permissions tables, omits legacy `-s`/`sandbox_mode`, and needs its own
  persisted identity and actual native execution capture. OS runtime read exceptions must be
  reported; a folder by itself is not read isolation.
- Independent AF_UNIX synthetic-socket denial passed separately; the original thirteen shell checks
  remain unchanged. Evidence: `native-sandbox-qa/unix-socket-result.json`, SHA-256
  `cf060e15be35d0a76c5c0c40687e6e8a99c16019b829a4ed3c7e8d69d84193a2` in the private verification root.

## Actual native execution capture

Root reviewed the independent preparation and ran exactly once:

```sh
/Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python -B /Users/velox/.cortados-builds/cowork-mcp-verification/native-exec-capture.py --execute --config /Users/velox/.config/cortados-workers/config.json
```

Capture job `97417125-08c6-4df8-b916-5edac704a359` used native0.163.0, existing subscription,
fresh native quota admission and the remote-only profile. Native execution completed and the existing
watchdog proved containment. Raw events independently record successful task read/write and denied
localhost HTTP with a positive host control. Synthetic outside canaries stayed unchanged; no outside
write files appeared. The driver SHA-256 was
`956e914c9031842fdc37b2bac7b149dba85f0ac93cb6aac68a4fbfd0c8bae74e`.

The model-written receipt reports ten file-boundary denials, but the native JSONL omits those tool
events. Do not accept the receipt as independent proof of the ten attempts. That part of actual-exec
acceptance remains inconclusive. No independent native file-read tool was exposed, and no document
runtime was qualified. This evidence is separate from the direct sandbox thirteen-assertion pass.
No failed job was automatically retried. Raw capture is under
`/Users/velox/.cortados-builds/cowork-mcp-verification/native-exec-capture/`.

A distinct planned v2 experiment (not a retry of a failed job) used one immutable task-local shell
probe so all OS outcomes appear inside one native command event. Root reviewed the independent
driver/probe/profile and ran it once with the same accepted interpreter and ordinary config.
Capture job `ba4c3e14-c1f7-47fb-93b6-d703a98a6db7` records all thirteen outcomes: two task operations
exit0, ten file-boundary operations exit1 with OS permission errors, and loopback exit7. Canaries
and probe hashes are intact, outside writes absent, zero sandbox TCP connections, termination contained.
Raw events SHA-256: `262b41aed6988fa954284093377de928e593a1286c062b046a13dffb56617010`.

The exact-invocation check did not pass: the model included the prompt's trailing period as a single
unused `.` script argument. Root preserved that failed comparison in `native-exec-capture-v2/root-review.json`
and requested independent adjudication; no third invocation or silent test change follows. The immutable
script does not consume outer positional arguments. Native file-read APIs, document runtimes and Cowork
transport remain outside this experiment. Evidence root:
`/Users/velox/.cortados-builds/cowork-mcp-verification/native-exec-capture-v2/`.

Independent QA accepted the bounded thirteen-case evidence **with the recorded deviation** after
checking shell argument semantics, immutable script, raw outputs, all hashes and containment. The strict
comparison remains false and its file is unchanged (SHA-256
`aa0f29a7e58e525a9bfb8190be2c63e8e017875f58bf96d591a49da1745fef88`).
`independent-adjudication.json` SHA-256:
`4e3e350867f48ad50dab64a5cbea5a39af38b780ddc0250accb58e6d8ebc9444`.
This closes the missing shell-negative evidence gap only; it is not a full native or activation pass.

## Local core versus activation

Independent v2 review at draft commit `d4c99db` found two auth design defects, recorded in private
`008-v2-technical-review.md` (SHA-256 `849b5f4d625f51926bc3ffb85a200822363b546f6de1e89741888154da31b56e`).
The proposed upstream lifetime hook was too late and missed transparent refresh; one ASGI process
also does not serialize concurrent redemption. D is not ready; A was blocked/not run. This is not
a production build failure or a completed zero-defect review of the remaining contract.

QA confirmed that mandatory parameterized enforcement can be built locally without choosing live
identity/limits/retention. Root dispatched contract-only revision job
`0cf07890-97dd-401c-86ac-cd8bf976a01d` under the same group completed draft v3 defining an independently
gateable local core and a separate future auth/activation package. It changed only the same three
contract docs. Root read its configuration/IPC/migration/interfaces and parsed sixteen JSON blocks;
draft-only acceptance feedback is `4735cd80-17de-4b5c-b4b8-3098edbf3bdd`. All user choices remain pending.
No implicit policy, automatic cleanup, unauthenticated public entrypoint, provider registration or
live activation is authorized by that narrowed draft. Core D/A and committed exams still precede code.

Before independent v3 freeze, root clarified three technical requirements: preserve ordinary Unicode
and punctuation in filenames with safe UTF-8 HTTP encoding instead of the draft's unnecessarily narrow
ASCII allowlist; bound the four-byte header size to its representable range; and bind base/broker state
to the isolated core state before migration. These do not select live account/storage policy. Parent
hash after these amendments: `f6ed8f473e720cef5345350bdaf97898d77a2c54468af199a4b27e6b069b1363`.

A separate exposed file-edit-tool capture used job `e9ecb003-9ec6-4e2a-aece-ff0f7c7bbe18`. Actual
inside apply_patch and its expected hash are verified. The outside file is unchanged, but native exec
again omits the reported refused call from raw events. Independent QA records the negative as
inconclusive, not passing, in `native-apply-patch-capture/independent-review.json` (SHA-256
`120c1cd026884ab01eda68d8c21741e7fe7682e52924135be8ddb0e2757456a8`). No rerun followed. This remains
a later native activation qualification gap and does not change the bounded shell proof.

## Authentication qualification

Tailscale tsidp v0.0.17 at `972d97d49b6e44d2baf4786def7485f55a7b09c1` was examined and a
checksum-verified published binary staged for help-only inspection. It has not been enrolled or
started. The source permits optional/plain PKCE, logs a supplied secret in an error path, and
holds sessions in memory. It is not the selected production provider.

Current candidate: maintained FastMCP3.4.9 OAuthProxy/GitHubProvider, source at
`/Users/velox/.cortados-builds/cowork-mcp-research/fastmcp`, commit
`83c9764e0bc2d9d32910b82264c09370da24ab7a`. Its supported MCP range includes the existing1.30.0.
Remote installation dependencies require an explicit lock; existing installed releases stay intact.

Current [GitHub OAuth documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
supports expiring access tokens and refresh tokens through `offline_access`. That value is not an
ordinary API permission. The proxy only issues a refresh token if the actual upstream response has
one; a real refresh capture is required. No fabricated refresh or one-year fallback is acceptable.

The proposed login uses existing account `4list233`, publicly verified numeric ID `237812476`,
with no repository scopes. Forest's provider choice is pending. No login application or credential
has been created. GitHub authenticates the connector user; Codex/Claude subscriptions remain
separate Studio credentials.

Independent source review requires the thin adapter to enforce the configured numeric subject,
verified proxy OAuth-client identity, exact resource/issuer and mandatory temporal claims. The
provider's returned `AccessToken.client_id` is a GitHub user ID and its token field contains the
upstream secret: neither may be blindly forwarded as planner/task evidence. Only a sanitized local
principal crosses into broker calls. Disable arbitrary redirects/CIMD/public registration unless
the frozen contract explicitly needs them. Keep consent enabled, durable encrypted private auth
state, and exact hosted Claude callback. Exercise secret-sentinel error tests; library logs/error
bodies are not automatically safe. Full QA notes are in the private verification directory.

## File policy question pending

Forest was asked to choose initial100MB/file,1GB/task,10GB service storage, retaining files until
explicit deletion (recommended), or automatic cleanup seven days after completed work. These are
decimal bytes:100000000,1000000000,10000000000. No choice is inferred from elapsed time. Full
capacity refuses new transfers; it does not delete unrelated data. The author draft's automatic
24-hour/7-day retention proposal has not been approved.

## Accumulated baseline

Exact command at source4ed7508 (only draft docs added):

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py /Users/velox/.cortados-builds/cowork-mcp-verification/baseline-4ed7508
```

Result:785 tests,784 passing,1 failure,0 errors,0 skipped across26 lanes (including two benchmark
lanes). The suite is **not green**. The failure is the historical007 installed-parity test's whole
`labels.jsonl` hash: the separately authorized nightly run is appending training labels. Its other
immutable-file assertions passed. No008 production change exists at this baseline.

QA's preservation plan is to run unchanged007 assertions with explicitly recorded literal path
relocation to an exact frozen historical source/data fixture, preserving original label bytes and
hashes, the real007 installed release, and assertion/AST identity. Separately verify the live
pre-existing label prefix, held-out exam identities, and new rows' final training partition.
Do not mutate or weaken the protected007 exam, or freeze the entire live label file during the
authorized training run. Candidate tests must additionally prove008 ordinary-profile compatibility.

## Release gates

Independent committed local acceptance and D0/A0 precede production dispatch. Actual Cowork
OAuth/binary transfer is an activation-completion gate after implementation and local verification;
requiring that finished connection before implementation would be circular. No public endpoint,
auth service, task store or connector has been installed by this review. The existing private
Studio HTTPS443 route and active nightly remain separate.

## Full v3 design review and lifecycle amendment

Independent QA reviewed the complete LOCAL CORE package at e055c51, parent SHA256
f6ed8f473e720cef5345350bdaf97898d77a2c54468af199a4b27e6b069b1363. Rubric D has
21 noncompliant checks across eight grouped amendments; A was not run. This is a contract
readiness result, not 21 separate product bugs or a failed production build. The preserved report is
`/Users/velox/.cortados-builds/cowork-mcp-verification/core-v3-D/FULL-D-REVIEW.md`, SHA256
d02aca81063b06a599726f3a9b7248a9c8c563e1cdad63071fb81bab6063cb1d.

Root accepts the cluster diagnosis: CoreRuntime ownership of construction, recovery, IPC sessions,
accounting observations and shutdown was missing. The next contract amendment may add the exact
future source paths remote_runtime.py, remote_ipc.py and remote_capacity.py under integrations/mcp.
Existing job storage, native process control, routing and quota owners remain authoritative.
Root also authorizes the designated exam owner to add acceptance/cowork_mcp/__init__.py and
acceptance/cowork_mcp/support.py after D0. This record grants no builder dispatch or source edits.

Contract-only Astra revision job 162d277f-c2dd-48e8-b1ed-f663af7caa22 uses unchanged group
53f0ea39-a61e-4307-9c2c-466c1b4b0bd1 and request1d94027e-9e3b-4ef8-8a0f-94dc49c362f0.
It may edit only the parent, DECISIONS and EXECUTION documents. The MCP context check returned
ready; native fresh quota admission remains mandatory. No retry of an accepted failed job occurred.
Independent QA is separately measuring existing-owner baselines and inventory without model calls.

## Additional synthetic authentication boundary evidence

Root ran the private auth-hook-boundary-probe.py with the isolated auth-research-venv interpreter.
Twenty synthetic Authlib1.9.1 cases passed: both code exchange and refresh execute their compliance
hook before token parsing, accept normalized positive lifetimes and reject missing/invalid lifetimes
or conflicting aliases before parsing. External request count is zero. This qualifies only that
dependency boundary; no OAuthProxy storage, callback, crash, concurrency or real GitHub flow was run.
The probe is not production implementation or a protected exam and grants no auth acceptance.
Source SHA256:173459206b8f4491de7742a84632958fd74a0bd9524f6552771428e7ed2a504c.
Result SHA256:c4a4db7ebe7a62eb47ba493a61bd6c89bffc7a2d657e3ce0159ec5d1d3bf7603.
Both files remain in the private cowork-mcp-verification directory.

## Root technical scope decisions for v4 review

Root authorizes one additional future source path, `cortados_router/route_task.py`, solely for the
keyword-only `handoff_packet: str | None = None` seam proposed by the author. The existing route owner
must use that complete trusted packet for its current byte budget and every resolver input-fit call.
None preserves the original packet and ordinary results. No task/context mutation, duplicate routing
logic or new quota policy follows. This is a technical amendment inside the accepted Router scope;
independent ordinary/full-remote input-fit exams and D/A remain prerequisites to implementation.

Root authorizes these additional future protected exam-owner paths under acceptance/cowork_mcp/evidence:
run-full-suite.py, benchmark-core.py, suite-inventory.json, baseline-ordinary.json and historical-007-parity.py.
They are QA-owned, not builder-writable. No repository exam files have been created at this point.

The measured ordinary baseline is frozen under private cowork-mcp-verification/core-baseline.
Report SHA256:6e487a4a0ebd45ceef9b98e2fadd6a14e71c0e3739a29057240e0dbe9235deef.
Inventory SHA256:c6b9fc5b4bc2a93cf4cc7ae9d154ae0a3239b520d75bfd7f6b6f0dd2afbfb077.
Seven source/fixture hashes equal4ed7508. Each indexed get/replay uses one SELECT;6,000 total lookups
were measured over1,000 real synthetic JobStore rows. Median milliseconds: get0.027042,
replay0.057417, explicit route0.096188, synthetic-S1 route0.615167, context0.004375.
These are existing-owner measurements with zero provider/process/network calls, not new-core latency,
transfer-memory or universal performance thresholds. All75 current test files are inventoried.

## Root v4 completion before independent review

Root reviewed the completed document-only worker result and granted the exact full-packet route seam. A private preparation journal now precedes workspace mkdir and is adopted in the existing acceptance transaction; unknown crash-created inode ownership quarantines rather than being inferred. This is a technical recovery correction within the authorized boundary, not a policy selection. The real ordinary bootstrap envelope is captured under private ordinary-bootstrap with hashes in the parent. No production files changed. All remaining gates stay explicit: independent D/A, committed exams, implementation/full-suite verification, then separately qualified OAuth and live user choices.

Independent v4 review found the synthetic HTTP constructor lacked a core-owned route/lifespan definition; the only detailed SDK composition sat in excluded future-auth. Root added exact listener-free /mcp POST and /transfers/<UUID> PUT/GET dispatch, rejection behavior, already-bound CoreClient ownership, SDK manager lifespan and planner initialization instruction. This fixes the technical composition inside the approved core boundary. No endpoint/auth policy or production source changed. Independent D continues on the amended bytes.

## Independent local-core D closure

At1637088, parent SHA89314886b6aaf26c7070d7b82efafcc2699d6e6b92e610d94d648c3e793a2f5c, independent QA scored all33 D rows zero. Preserved readiness SHA89eb16bf23e6bb620a2da555769c8adb7e9d8e35ec5c490bdbad06df85634999 under private core-v4-D-closure. Prior v3D21 and v4D3 remain history. Q is authoring protected core exams; A and implementation remain pending. Root then confirmed the mechanical private-record constructor convention Name(**fields), immutable named attributes/nested collections and safe invalid_request; Q must review this narrow clarification before A.

Separate bounded auth investigation job4540efb6-75b9-409b-9e85-3c45f21fa08e completed. Root read the two probe modules and independently reran copies with socket.connect forbidden:81 cases across maintained callback/explicit/transparent refresh,78 malformed responses rejected before observed parsing/write,3valid controls; zero network connects. Forced provider-method code/refresh races each yielded two successes, confirming the missing outer serialization gate. Private auth-flow-qualification/REPORT.md SHA375fd5424df242b8a3e78a003ed675029c4cac6e6eaf020a74890b700befe9fe. This is not full HTTP PKCE, gated-race, crash atomicity, safe dependency-error or live-provider acceptance. Feedback212d41de-0509-4bc4-a398-fb8e7fc47e4b accepts the investigation only.

Contract-only future-auth author job20d04076-54f8-46a1-b527-4597ca5ad88c/requestd2232aeb-9975-418a-9621-5d37955c1a14 uses unchanged assignment group. Its exact write scope is two NEW COWORK-AUTH-009 documents; no existing core, exam, source, commit or live access. Auth builder dispatch remains dependent on accepted core, its own D/A, committed exams and no unresolved technical decisions. Live user policy choices remain pending.


## Measured core selector budget and separate auth draft

Root approves the corrected representative selector measurement in private core-baseline/planned-selectors.json, SHA256 3a57f53facdcbef02a80c58018e8cf8efd057a1b8c531a6222e355875cf04625: each unique task/replay/artifact lookup uses one indexed SELECT without a full table scan; the 100-artifact fixture listing uses one primary-key-prefix SELECT and a bounded temporary sort. This is a selector budget, not a cap on whole public operations. Aggregate SQL, recovery and capacity scans remain separately reported. Transfer measurements use 4KiB/64KiB chunks, 1/2 streams and 1/8/16MiB payloads; B must establish file-size-independent memory versus chunk times streams. No fixed latency or RSS ceiling is invented.

Future-auth draft job20d04076-54f8-46a1-b527-4597ca5ad88c completed with only two new documents. Root read both, verified copied historical AC rows and unresolved decisions, and recorded draft-only feedback23b83eaa-1170-4288-92c7-87da03b6b853. Parent SHA6e13773a7075d1c0cf75b8cb39ba6f859e14320bcd04dc908b80b68c86f26ecf; execution SHA254b4238570989288f68ca89222b04bb390ffe77bad841e6ddf83f4dcb0185bb. This is not auth D/A/B or activation approval.

Root technical directions for the next 009 contract revision are: O2 adopt the proposed distinct oauth_service validator and trusted constructor seam, preserving strict local_test; adopt one guarded HTTP composition owner after core acceptance. O3 develop the durable clean/dirty lifetime-marker alternative, with ordinary clean restart supported, uncertain/crashed state refused and preserved for explicit recovery; no automatic deletion, rollback claim or stale-backup restoration. Exact marker format, key/config binding, atomic replace and flush order require independent design and kill-point exams before implementation. O4 correct discovery to advertise the actual public client authentication method none and S256-only profile; retain maintained issuance/verification and forbid DCR/CIMD. O5 retain historical AC-008-01/02 verbatim and ratify R14's additional synthetic hook/race/crash requirements as local acceptance; live GitHub/Claude evidence remains additional. O6 adopt strict preparse response projection and require measured request/gate/resource budgets before A; no invented deployment defaults. O7 approve optional isolated remote dependency resolution and its reviewed lock, preserving ordinary startup environments. These are technical preparation directions within the authorized OAuth connection; they do not authorize production source, dependency installation into an existing environment, app registration or live activation. Forest's O1 identity and deployed storage/retention choices remain pending.


## Bounded root dispatch approval — LOCAL CORE attempt 1

```yaml
contract_id: RT-COWORK-MCP-008
contract_version: 4
status: approved
scope: LOCAL CORE only
task_group_id: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1
owner: Forest
orchestrator: /root
exam_commit: 1037b52db9732a10e1c7b0ce86f5fb22ad1d0c29
exam_manifest_sha256: e0680bf5687e46b339fcea469663a17595c5bad08e1897149b2af45f2aaf2852
parent_sha256: 5b9527ac2e71f8ef6bd345031a9dbfe68f0b743d6b004bf7e8db853b923cfc1a
execution_sha256: 92760ded3f365f7e92074d7b2dd248660ea4b6a6435549acb57978a41f663096
decisions_sha256: 41de4dc4023a69100eb504c1fcc099fa36f0b0d7b0050c43942ef4107d2754eb
independent_D: 0
independent_A: 0
builder_selection: explicit gpt-6.1-sol
request_id: f421590e-5994-4638-8756-41f1a3a23338
timeout_s: 1800
```

This later approval binds the exact immutable normative bytes above; earlier author-draft status is historical, not permission to alter those bytes. Root read all37 A dispositions, current D confirmation and the committed exam README, checked all18 fixture hashes/3normative hashes/4private evidence pins and independently rechecked all641 prior protected-file hashes. The private core-A readiness SHA is6ab1fddb9acc74465aacfc291b73ad7db0cde7f597f4c6039c7a2573e5825014; full A review SHA16cad10e73f4df2c8df5c83df4865d3740d3f87eba42852f4d435c56d244e526; current D SHA7c834bec5abe262fa701a9c223bf056f53489d20cb9fc539f51a49bd11935b34. All33D and37A rows score0. CLAUDE-APP-CONNECTOR-PLAN.md is informational plan provenance under the A1.4 overlay; RT-MCP-001 is the normative dependency.

The one builder receives exactly EXECUTION PackageB's18 source paths. Protected exams, contracts, other sources, installed releases and live state are read-only. Temporary test evidence may be written only to private /tmp/cowork-core-build-attempt-1 directories; root will preserve reviewed evidence afterward. No OAuth, listener, provider registration, dependency installation, native model calls, API fallback, service activation, commit or push is included. S1 is reserved for the ongoing trainer; explicit supported integration-model selection avoids loading another scorer. Native fresh subscription quota admission still applies before acceptance and START.

The72method expected prebuildred (3failure/162subtesterrors/0skip) is a readiness observation, not an implementation attempt. Original785-test live baseline remains784pass/1historical whole-label comparison failure; the separately preserved historical0074/4 and current-prefix/ordinary compatibility exams do not erase it. Earlier design reviews, native evidence limits and auth defects remain. This is the first core implementation attempt; no failed-job auto-retry is authorized. Root/QA must independently verify candidate B, full accumulated inventory, source containment and measured memory/performance before accepting it. Auth integration and every live gate remain excluded.


### Explicit worker-role correction after blocked preflight

Job5eb113c4-952d-45d8-afa5-52b35b36a89c finished its native process after28.590seconds but produced no implementation. It incorrectly applied root's orchestration duties to itself and demanded cortados-workers in its own tool list. Root had already performed context_requirements and quota-gated delegation, with intentional networkfalse/mcpempty. The child is the delegated implementation worker and must implement directly with local file/shell tools; it must not recursively delegate. The general workflow says when MCP is available, and this worker deliberately receives no MCP grants. No additional connector permission is needed or granted.

Root inspected terminal status/result, confirmed no source or protected-exam changes, and recorded inconclusive feedbackc922f515-1018-4b7c-85ef-8c672c3feaf3. This is a blocked setup before implementation and checks, not a valid failed build or a changed design. Root explicitly authorizes one corrected dispatch with new intended-attempt request860667d8-0872-46e2-b510-246a95641c8f, samegroup/model/timeout/exactscope/frozenbytes. Root will repeat context_requirements and native freshquota admission. This is an evidence-reviewed role correction, not an automatic retry of a failed job; no other behavior or permission changes.


### Bounded execution-environment correction

Corrected-role job5317c22b-1a38-4cdf-aeb7-becf7575b998 stopped before code in20.590seconds because the root's /tmp evidence path conflicts with the ordinary native profile. Root verified native_arguments in integrations/mcp/codex.py explicitly sets exclude_slash_tmp and exclude_tmpdir_env_var true. No source or exam changed. Feedback26762c4d-d518-444b-af95-900991e4cfe5 records this as inconclusive, blocked setup rather than a valid build failure.

Root replaces the earlier temporary output path ONLY with /Users/velox/.cortados-builds/cortados-router-repo/.cowork-core-evidence/attempt-1. Generated logs, reports and temporary test fixtures are permitted in that exact hidden subtree, which remains untracked, outside default test discovery and excluded from every commit. Test commands set TMPDIR to its tmp child. This grants no access outside existing native cwd and does not alter the18production or19protected exam paths. Builders must not change tests to relocate hard-coded writes. Any immutable lane requiring broader host writes must be reported BLOCKED in the worker handback; root/QA runs the unchanged lane on the host for acceptance. Complete all other authorized implementation and available checks rather than treating that possible runner restriction as an instruction to stop before coding.

After evidence review, root explicitly authorizes corrected dispatch requestfce3f9d8-0da8-40f0-88b0-62daafdf353b under the unchanged group, model, timeout, core D/A approval and immutable normative/exam hashes. Recheck context and fresh quota. No nested MCP, permission escalation, source-scope widening, automatic retry or provider fallback is authorized. These two preflight-only jobs do not reset history or constitute valid failed builds.


### Auth technical closure directions after independent maintained baseline

Root read the independent OWNER-CLOSURE.md (SHA2569728dde230af719cbecd6c3d4c16affd9387bafb6d89caa39aa88f78f069fc2d) and corrected70-sample baseline report9612c2037882a3775483918cc8776833bc8ae4f7f2e926a89588dc75338b91f2, manifestf27d143b6b314b4231bd69881b84668a98fe9cb78853e6dfcdff02ac9514f8a2. There were zero external connect attempts. These qualify maintained seams, not new adapters or live behavior. Core008 frozen normative bytes and protected exams remain unchanged while its builder runs.

For the next contract-only009 revision, root approves these technical dispositions within the authorized connection; they do not grant source implementation or activation:

1. Extend maintained HTTPX clients with one new remote_auth_transport.py owner. Guard actual upstream reads before eager allocation, using pinned response-hook/stream semantics and identity encoding; apply the same guard to OAuth and identity verification. Freeze the exact supported real-client construction/lifetime, timeout/redirect/environment-proxy/cookie and endpoint policy, actual upstream chunk/header limits and source-backed tests. No replacement OAuth/token issuer. Mandatory configured body/chunk/waiter bounds have no invented live defaults. Add max_waiters to the existing single admission owner and account for bounded request state before queueing.
2. Move the entire lock/marker/manifest/barrier and stored-data trust responsibility to one new remote_auth_storage.py owner. Guard encrypted envelopes below Fernet because maintained decryption accepts plaintext dicts, then validate decoded maintained collection records before Pydantic coercion. Derive exact schemas/TTL/optional fields from pinned maintained source/captured synthetic rows; no new serializer or credential database. Cover every used store entrypoint and key_value logging. Strict JSON duplicate detection at byte boundaries needs an explicit existing-owner extension rather than an impossible dict-level assertion.
3. One new remote_supervisor.py owns parent CoreRuntime and spawned auth/HTTP frontend lifetime. Reuse inherited socketpair/core ownership. Freeze a minimal private closed lifecycle channel with no tasks/tokens/jobs or public listener. Explicit disable and auth failure close admission and revoke tickets/streams through the existing trusted CoreRuntime owner; acknowledgment follows completion, accepted jobs continue. Unexpected frontend loss closes its endpoint and leases, leaves accepted jobs with CoreRuntime and requires explicit operator restart; no automatic relaunch. Parent shutdown owns core shutdown. No public disable opcode or second job queue.
4. Scope durability honestly: supported restart validates an exact clean marker plus complete current manifest under exclusive ownership on the qualified filesystem. An observed uncertain close leaves the live supervisor failed with admission closed and suppresses automatic relaunch; preserve evidence for operator review. This does not prove that a previous process observed the final barrier succeeding after all processes have been lost. Complete stale restore and unobservable failed-barrier/power-loss history detection are explicitly outside this local guarantee. No external fence, silent rollback, automatic reauthentication, data deletion or claim of universal crash atomicity is added. Amend conflicting prior wording consistently and pin allowed kill-point outcomes. This is a technical reliability boundary, not permission to bypass a detected failure.
5. Synthetic and initial qualified profile uses empty identity scopes only. Any read:user variation requires a later explicit source-qualified amendment. Synthetic provisioning uses maintained registration; operator provisioning/secret resolution remains a separately specified activation dependency, never startup repair.
6. Freeze the independent baseline's seven direct-method keyed-store/provider vectors as regression budgets, excluding named setup/gate/claims/static-client/strict-storage/barrier work which is measured separately, never hidden. Mock timings are baseline data, not live deadlines or a fabricated latency/RSS SLO. Mandatory deployment values remain unset. Use explicit fixture bounds and measure memory scaling, finite queue occupancy, cancellation settlement and candidate overhead before B; no unmeasured hard cap masquerades as evidence.

Future B1 scope adds only remote_auth_transport.py and remote_auth_storage.py to its five named files. Future B2 adds only remote_supervisor.py to the existing exact integration scope. The author must freeze callable/record/message examples and ownership, maintain source ceilings and update both draft scope tables. Root grants no production path until independent009 D0/A0, committed protected exams and accepted008. The current author may revise exactly the two isolated copied009 documents and must return any remaining contradiction. Root will review and copy them; no global DECISIONS entry is authored here.


### O7 packaging correction from an isolated resolution

The drafted plain fastmcp-slim==3.4.9 declaration resolves39 packages but its actual GitHubProvider import fails with the maintained server-support-not-installed error. Root preserves that negative evidence in private auth-dependency-lock/plain-results.json and plain-import.stderr. The research environment had used the server extra; successful research imports never qualified the plain declaration.

Root therefore corrects O7 to fastmcp-slim[server]==3.4.9 with Authlib==1.9.1 and unchanged mcp==1.30.0. An isolated official-PyPI binary-only, hash-required resolution/install produced65 packages with zero version differences from the pinned research environment. Actual GitHubProvider/FileTreeStore/Fernet imports passed; the existing Authlib HTTPX deprecation warning is retained. Exact commands/results and qualification limits are in auth-dependency-lock/README.md and REPORT.json. Candidate lock SHA2566099038ad1842f32dd9e4b54946dd3494cb97027851d71c8a066abc6f784cb88 is CPython3.14.6/macOS arm64 evidence only. No existing environment, source, provider or listener changed.

The next draft must use this exact extra declaration and reviewed lock evidence before D/A. This corrects missing declared dependencies, not runtime policy or installation authority. B2 still owns the future pyproject/lock source changes only after gates. The contract author may cite the new evidence; root may mechanically correct the extra declaration before independent review if its revision finishes first.


### Auth v3 independent D11 amendment authority

Independent auth-v3-D/FULL-D-REVIEW.md SHA25605ea4166b1b99d115c99eb58fd920b63360fedd4c79e5f8b5b7bac9a32668c43 scores11 defective rows across all33 D checks, grouped into five amendments; A remains unrun. Root adopts the exact C1–C5 minimal technical corrections: final strict temporal recheck through the existing validate_claims after awaited verification; hook-time and actual storage-time datetime representability checks without arbitrary TTL/clamp; one canonical type/API/state registry followed by atomic modal FIXED requirements with stable sub-IDs/AC/reasons; corrected E12 callback and parseable sequence; mandatory local real-loopback lane. Architecture, protected historical AC rows and activation scope stay unchanged.

For C5 root explicitly authorizes an independent synthetic test lane in the already proposed test_oauth_http.py: bind only127.0.0.1 on an OS-assigned ephemeral port, use private synthetic credentials/state and actual maintained ASGI/HTTP server, restrict client connections to that exact local endpoint, and deny provider/external traffic. Verify discovery/401 and lifecycle with clean process/socket teardown. This is local acceptance work, not public exposure, real-provider access or a persistent listener. Freeze its exact command/lifetime/connection guards in the revision before Q authors it.

For C2 adopt the source-backed UTC datetime/timedelta(float(duration)) checks exactly described by Q; representability may still change at a later live write boundary, which must fail and preserve evidence. Do not promise preparse proof of all future-time representability, change the provider serializer, synthesize expiry or invent a new live TTL. C3 is a mechanical normative registry/locality correction, not new product behavior. Retain D11 and original defects/history.

The next Astra author may revise only the two existing copied009 documents to v4 under these directions, original standalone repository/source/exams read-only. Copy the root-corrected server-extra execution draft before dispatch. Independent D then committed exams/A still precede any auth source build. The core implementation is now terminal and undergoing host/independent B; no core normative amendment occurs during that review.

### Core first-candidate rejection and independent regression preparation

Builder87c9ffee-4119-4301-bafc-4668d8dbf8ca produced the first actual core source candidate. Root froze18source hashes and preserved all worker evidence/source snapshot privately; cumulative patch SHA672771b8461d04793bafe30f3dd4da0e6cdcef10bd664a47488e4b27d5576d15. Unchanged host full-inventory driver completed857methods with856passing/1failure/0errors/0skips;26of28lanes passed. Report SHA32111c97256707c86b072665260f69f58db7554f635a4bc5dfa77934b204e38e retains original historical baseline separately. Output manifest ordering fails. Core matrix stopped after5cases when a120second synthetic total lease expired during two16MiB transfers with4KiB chunks. Worker sandbox-blocked evidence is not substituted for host results.

Root read independent QA-B-REVIEW.md and its grouped C1–C10 existing-contract defects: output order; post-wait disable admission; cancelled IPC response integrity; publication membership before output capabilities; HTTP backpressure deadlines; redundant stored identity agreement; fatal audit latching; missed monitor cadence; omitted private-record array constraints; complete teardown despite an earlier cleanup failure. Missing runtime transition events belong to the same audit correction. These require no new product permission. Feedback99d61643-cfe6-458a-b639-81d783578670 rejects the candidate. This is one valid failed build, not ten attempts; the earlier preflight-only jobs retain their blocked classification.

Independent Q may prepare regression amplification privately, preserving every old assertion. After original source/protected identity verification and original full report preservation, Q may commit additive regression exams with its Exam-Owner trailer and update the manifest. No builder may alter them. The original72method result remains recorded, never relabeled. Root has not dispatched or approved a second build yet; final grouped B/performance evidence and committed strengthened exams precede that explicit decision.

Root approves the QA-only core measurement fixture's transfer_total_ms changing from120000to600000milliseconds so the instrumented12case matrix can complete. The two8MiB case already took95.89seconds;600seconds provides over3times the extrapolated two16MiB duration and avoids confusing instrumentation timeout with a product latency budget. A smaller fixture total truncates measurements; a larger value would delay detection of a hung benchmark. Idle/IPC waits, actual timeout assertions and live configuration remain unchanged. Preserve original benchmark bytes/hash and failed raw report before the exam-owner amendment. This is no live default, SLO waiver or claim that the candidate passed efficiency. Ordinary and supplemental runs complete sequentially; no production changes occur during the frozen first-candidate measurements.

### Bounded root approval — LOCAL CORE one same-design correction

Root independently read the finalized11-row B packet: B=3 at B1/B6/B7, ten defect clusters and one valid failed attempt. Private QA-B-REVIEW.md SHA98a22bf2fd621d191d1f2bfbee8c205b5c2f2ae9bf3e00569c0358a382c93e5c, B-readiness SHA378e5740f559c287727cfd150eaadf6d35c0335a68c5fd11c8c1ac9be653ff3b, B-MANIFEST SHAe48c8fb814b1b658a353fd716df9da4df5951f8776386e0eac7bc5b4632f8edf. All12 matrix keys and monitor completed through an explicitly indexed continuation; original120s-total and expired-idle-control-client failures stay preserved. The latter was a measurement-client lifecycle error, corrected by binding a fresh client before post-transfer query timing, without changing any product deadline. Ordinary and supplemental measurements, retained/peak allocation diagnostic and their limits are in the packet. No correctness/efficiency acceptance is inferred from completed measurements.

Independent exam commits153f73027e8d58d4e1c51853e85d504d3fc1bc5c andf5be56a92a7fc0ba91c0a8056b927c4fd72daf5a preserve all original assertions and add17methods:89core/874accumulated methods in28lanes. Current manifest SHA03589f6ad5394e4c5d3c29f37979f349f7fb4b1c7cd8b6671b0c55f94da49959; final A-amendment-readiness SHA00f8bd160957ef8d33ed9db9e0df9e135a27a925fd832f3cbe1392f8d9cb3c1b scores all37rows0. D's33rows remain0 for unchanged normative bytes. Root separately verified all18source,3normative,18currentfixture and641priorprotected hashes with no mismatches in root-retry-identity.json. The two preflight-only setup blocks remain separate; they never reset this ledger.

Status: approved for exactly one bounded same-design correction attempt, requestd145b0f9-1dca-4fe8-96b5-74e86c707dbf, group53f0ea39-a61e-4307-9c2c-466c1b4b0bd1, explicitgpt-6.1-sol, timeout1800seconds, networkfalse/mcpempty, native-default subscription. Root will call context_requirements and fresh native quota admission before acceptance. This evidence-reviewed correction is not automatic failed-job replay. A second valid failure returns a design_error packet; another agent/model/ID cannot reset the count.

Exact18source scope, immutable normative hashes and read-only exclusions remain those of the original bounded approval. The builder extends the existing owners to satisfy allC1–C10, missing runtime events and the full frozen contract, not merely new test assertions. No architecture, product policy, live default, worker permission, auth source, dependency or public-service change is granted. Direct worker role remains explicit; no nested MCP. Temporary output is only the new untracked .cowork-core-evidence/attempt-2 subtree, TMPDIR its tmp child; preserve attempt-1. Per-commandumask022 matches unchanged historical fixtures. Native Seatbelt-blocked sysctl/ps/external paths remain honestly blocked; do not modify production/tests or permissions to bypass them. Root/QA will run the complete unchanged host inventory and performance after terminal source freeze. No source commit or acceptance is granted to the worker.


### Second valid core rejection; revised design pending, 2026-10-18

Corrective job d37a73fb-aa46-491f-9a5d-98fa60c682e8 completed and root froze all18 source hashes, maximum414 lines. Root cumulative patch634cce15a58df7f94512a9402cb2d22dfc26ec582ce55a2ae7f98b3409227394. The unchanged host full inventory completed874 methods:873pass,1failure,0errors,0skips;27of28lanes passed. Report756c7221d561f42e6b3265f4807e68e51beb66968b8f599b6546c19502160ce0. The original capacity-overrun method fails result inspection; a later observational pass is retained separately and cannot erase it. Independent deterministic capacity and HTTP/IPC backpressure probes confirm the remaining owner-level violations.

Full independent B is3 across11rows, atB1/B5/B7. Final readinessf3400cca106486da66bdcdae29721a95ac64417a35e83c9f10611ef62866c560; final measurement review0ad6ad01896961c875e4634a101a6f0946b35b93ac65b7866dcd719a5627477c; final manifestc8de880246b75efff27dd54e4f544015096e1ccfd9b13d2d10508279b421a3fd. All12matrix cases, monitor, ordinary comparison and4supplemental workloads completed; no speed/memory acceptance is granted to an incorrect candidate. Exact18sources/3normative/18corefixtures/641priorprotected identities remained unchanged. Original failed attempts, raw fixtures, host and private diagnostic outcomes are preserved.

Status is design_error after two valid failed implementations; no same-design retry or dependent auth source. Feedback52cbfe2a-6eb5-4bea-90b6-9ea0fe162ec3/seq19 rejects the candidate. Final closeout feedback854778c2-cdfb-4e40-b675-2114ffe64050/seq20 retains rejection after completed measurements and a root recheck of all680 source/contract/fixture/protected identities plus16 final evidence hashes, with zero mismatches. Root has presented COWORK-MCP-008-REVISION-2-PROPOSAL.md at7e8b1cd for Forest: one strict HTTP/IPC response lifetime with conservative send-attempt state and exhausted-budget abort; an exact capacity-only inspection/cancellation table retaining fatal integrity/disabled refusal. This is pending approval, not an amended contract or builder grant. Independent renewed D/A and additive committed exams must follow any approved revision; no attempt history resets.

Auth009v5 atd96dc1c independently passed all33Drows; A/source remain unrun. Its proposed shared-runner interpreter exception has not been applied. Operator010/110 handback is accepted only as a two-document proposal, feedbacke38cebc4-594b-4fc7-b9c4-9953e7da11a0/seq18 and commitfe1d951; no secret resolver/provisioning policy is accepted. Existing live choices, actual native-tool/Cowork OAuth/file evidence and final activation remain pending. No public endpoint/app/secret/service was created. Detailed final status and commands are in COWORK-MCP-BUILD-REPORT.md.


### Forest approves the bounded core design revision, 2026-10-18

Forest answered the pending proposal-v2 question: “Approve the revised design and continue.” The approved document is COWORK-MCP-008-REVISION-2-PROPOSAL.md at7e8b1cd, including its strict HTTP/IPC remaining-deadline rules and exact capacity-only operation table. Approval permits revised contracts and independent additive regression tests, then a revised build after renewed D0/A0. It does not grant a same-design retry, reset the two valid failures, change global rulings, or approve public activation. The proposal remains preserved as the approval target.

Root rechecked all680 prior source/contract/fixture identities with zero mismatches before revision. Exact approval and the preserved old core manifest are recorded privately in cowork-mcp-verification/core-revision-2/USER-APPROVAL.json and PRE-REVISION-CORE-MANIFEST.json. The contract-author scope is exactly the three copied008 normative documents in cowork-core-contract-revision-2. Original source/exams remain read-only. The independent MCP QA owner must review the revision and commit additive exams with Exam-Owner before any production dispatch. Assignment group remains53f0ea39-a61e-4307-9c2c-466c1b4b0bd1.


### Independent exam consequence of the approved strict deadline

QA preparation identifies one exact semantic conflict: BuildHttpDeadlineRegressionExam.test_stalled_http_put_receive_terminates_at_idle_deadline in acceptance/cowork_mcp/test_compatibility.py asserts response starts equal[408] after the100ms idle budget expires. Forest's approved strict-min-deadline rule requires abort without new headers at that point. After v5 D0, the designated independent exam owner may amend only that incompatible response expectation, preserving its timing/cleanup assertions, original source/history and prior red evidence, and add a separate timely positive-budget408 control. This is an explicit consequence of approved behavior, not weakening a test to fit the candidate. Other original assertions remain protected.

Independent preparation proposes22 additional methods in four existing exam files, plus existing support.py only if needed; README/MANIFEST/suite-inventory provenance/hash/count amendments are exam-owner work after D0. No new filename or runner code is presently required. Exact preparation is private core-revision-2-QA-preparation/PREPARATION.md (SHAadf4467ed52f43df4eb706cfa6ff9c44b4e9b95219f873a5285577571730aaeb), with proposed-exams.json SHA f6593b563a55330da82518cda94519c34c3b8a2f8d2dee805bfe6bc4f031ac7b. Actual collection, not proposed counts, governs A.


### V5 independent D1 reference correction

The complete33-row review atd946935 scores1 solely D4.1/V5-01: both future-auth and newly appended source-grounding sections used number10. Root mechanically renumbered only new source-grounding to11 and its corresponding DECISIONS closure-map citation. Existing future-auth10, schemas, AC/API/admission blocks, source and exams are unchanged. This corrects reference locality, not approved behavior. Original D1 report SHA f774e96ae2329b625452d8137d7961f0598784ba689c6936ad10eec7eec5496c and readiness63989f70ac2e1dc997b610cce8caee41704633ea80dba1b08f542d81a1245fde remain preserved in private core-v5-D. Allfour diagrams rendered;45 strict JSON blocks,2schemas and33valid/invalid private pairs passed. Renewed full-row D and then independent exams/A remain required.


### Revised v5 core dispatch grant after D0/A0, 2026-10-18

Forest approved the bounded revision2; root now authorizes one revised LOCAL CORE source build against v5 atc61e6a0 and independent exam commit3cda1fea79c60dd5e5e9d86a0e504591e37a4a68. This is cumulative source attempt3 on a changed approved design, with both failed v4 implementations preserved. No same-design third retry or history reset is granted. The draft/future-grant markings in the reviewed documents are satisfied only for this exact local build by this record; auth/public activation remains excluded.

Independent D0 covers33rows (readiness9ae3281ca93a6d9c51c4b45718bc2cc19c3144503a485cbbbe5d1d544d0b115b). A0 covers37rows (readiness1bd497610e819e9bfbc34c45f19b0ce295629983ec93c51ed39d3120b586954b). Exam manifest585527a2aaca7780d0ca9509b92e466e7d891147d9866d13be01809643fc8967 freezes111coremethods; unchanged28-lane full driver expects896methods. Final focused prebuild discrimination:23methods/42failureevents/0errors/0skips on rejected source, not a builder failure. Original89methods retained with only approved[408]→[] response expectation amended;641priorprotectedbytes unchanged. Root verified pins, source identities, clean protected working diff and required Exam-Owner trailer.

Builder selection is explicit gpt-6.1-sol/native-default under the project integration policy, fresh native quota admission, timeout1800, shell networkfalse and no MCP grants. Exact18source scope is EXECUTION PackageB; no other source, tests, contracts, dependencies or live state writable. Builder evidence may be written only under cwd/.cowork-core-evidence/attempt-3 (TMPDIR its tmp child), preserving attempts1/2. Existing worker sandbox restrictions on native sysctl/ps must be reported, never coded around or confused with host acceptance. No commit/push/install/registration/listener/training/schedule authority. Root and independent QA must freeze the terminal diff, run complete host inventory and all required measurements, review all11 B rows and record feedback before accepting. Private core-revision-2/PREBUILD-ATTEMPT-3.json records exact source/contract/exam pins and requesta8c501fb-9eb2-4784-aa7d-cbcf7b45b3e4.

### Candidate3 rejection and narrow independent exam correction authority

Root read the complete frozen candidate3 packet. Full host inventory:896 methods,10 failure events,1 error,0 skips;27/28 lanes pass. All12 transfer matrix cases, monitor, ordinary and supplemental measurements completed. Independent B has four defective rows B1/B5/B6/B7, grouped into two product causes: C3-01 missing first capacity-only transition audit; C3-02 blanket public-error-code fatal promotion that overwrites policy_unavailable and disables inspection after definitive acceptance rollback. Existing native-generation and rollback assertions remain unchanged. This is the first valid failed build under approved revision2, cumulative third; both earlier v4 failures remain preserved. No accepted release or dependent auth dispatch follows.

Private core-candidate-attempt-3 freezes root source map22768e54f69096bf8d499a2024517d775d0dd944cb39e22710ce886961334bb7, cumulative patch3bfce1b8bfdfa0f02f4dc7df1d3bef6196c1a38fbf077adfde1076fefdda088d, host report2768296b0701e059f8f1d381f0eca8268098993108281ea31a7108989d9c4a13 and independent B-FINAL.json f530124ae4a0eb3e58aa05faaa0983d37fb1ef282f43d62be0c849ea84d2770b. Final manifest62c244bb8584a419633bf494d011d51aab65a087805a1d7554a02ec7ef2ab9f1 includes cleanup observation and all27 artifacts verified by root. Feedback7aecbc5e-c556-47e9-8a0c-2ea256cce84a/sequence25 records final rejection and corrects sequence24's prematurely claimed manifest verification; B/source outcomes did not change.

Root authorizes only independent exam owner /root/quota_evaluation_qa to apply the precise technical fixture corrections in private independent/EXAM-CORRECTION-PROPOSAL.md SHAe832bccbc260a3458769d959081b87b7cf47d821e1cfee6cdc3b48f5dbf378fd. Original bytes/AST manifest68bb98841dc691228258045805972aba14567fb009451a0bd61526ec6046a76f and all failed reports remain immutable. These corrections observe the already-approved v5 behavior; they grant no production grace or new product choice:

1. E3-01: assert frontend completion/deadline/header state and local endpoint closure before awaiting the existing server Session.closed with a separately explained one-second test-observation ceiling. Assert actual server task completion without unexpected exception and zero addressed leases/descriptors afterward. No cleanup RPC, server cancellation, blind sleep or extended frontend deadline may manufacture success. Timely branches retain ordinary cleanup assertions.
2. E3-02: the expired stalled-PUT fixture may accept only the exact safe RemoteError timeout abort (or normal completion/existing explicit cancellation), preserving no headers, deadline and synchronized cleanup. Other exceptions/codes/messages fail. This corrects Python exception observation, not an HTTP success response.
3. E3-03: control actual publication pending-to-complete with a fixture event, preserve durable cancellation/job/launch/stop identity and one native execution across that progression, then retain whole-snapshot equality after publication stabilizes. Foreign-task refusal remains. Do not freeze or falsify current projections.

Exact writable protected paths: acceptance/cowork_mcp/support.py, test_compatibility.py, test_transfer.py, test_recovery.py, README.md, MANIFEST.json and evidence/suite-inventory.json (all paths after the first share acceptance/cowork_mcp/). No new path, runner/benchmark change or original native-policy/rollback assertion change. Add independent first-capacity-transition, repeated-sample/no-reopen/no-duplicate and audit-persistence-failure regressions in test_recovery.py using actual monitor/runtime/event owners. All existing111 methods are retained; any further AST change requires root review before applying. Preserve prior protected641 files and all18 source hashes.

Q must commit only its scoped exams with the required Exam-Owner trailer, verify original AST preservation outside the named corrections, run source-backed prebuild discrimination and repeat full37-row A. Unchanged v5 normative hashes/D0 remain pinned. This record authorizes exams only: root must separately review final A and record one bounded source correction before a new delegate attempt. A second valid failure of revised design returns a design_error packet; agent/model/name changes never reset counts.

### Direct deployment-policy answers received during candidate3 QA

Forest approved decimal100000000 bytes/file,1000000000 bytes/task,10000000000 bytes/installation, automatic completed-task payload cleanup after7days, and existing GitHub4list233 sign-in through a dedicated CortadOS Studio Workers login app with no repository permissions. The account/limits/duration are settled; do not ask again. Direct evidence is private core-revision-2/USER-LIVE-POLICY-APPROVAL.json SHA d6b813fe0e9a151f5ab3ac2dc55f86916aa4e0fd0e66e199b6d7e41e0b25d477. Actual numeric subject, app expiry/refresh, hosted callback and binary/native confinement still need qualification.

The seven-day policy is a separately reviewed extension of existing task deletion and lifecycle owners, not a silent change to008's frozen no-scheduler baseline. Root asked one concrete completion-trigger question after reviewing the isolated proposal: latest settled job including failures/cancellations, success-only, or explicit Claude completion. That answer remains pending. Payload deletion preserves existing job/audit history; no all-content-erasure claim. Secret custody/provisioning and actual launch remain later concrete operator gates. No live app, credential, public route, listener, installed service or schedule has changed.


### Bounded v5 correction after candidate3 exam amendment

Root verified independent exam commit9722a880a9afda0779ee5b135edd343d33dec4cb and its required Exam-Owner trailer. All37 A rows score0; readiness SHA8bdd96dc552dd729800ac925086341c845fa4da189cba81f9488d293c655c754, A manifest7d2a0577c00d593f86f8f095b205c1b044003aa82df63a1b378007ac40760fda. Current exam manifest8e32a0d697e1da3b06fef53ea8be9e2fd93363c7b88e2e2ab5b9fec8d73525dc pins114core/899expected accumulated methods/28lanes. The expected-red core run has109pass/5failures/0errors/0skips, precisely three first-capacity-audit regressions and the two unchanged policy/definitive-rollback failures. Root independently checked seven authorized method AST corrections, assertion preservation, all18sources,3normative,18currentfixtures and641priorprotected identities, and every19A-packet artifact. Unchanged v5 D0 carries all33 rows.

Root now authorizes exactly one bounded source correction under approved revision2, cumulative attempt4. Extend the existing runtime/event, monitor and dispatch owners to fix C3-01 first-capacity-latch durable transition and C3-02 blanket error-code fatal promotion. Preserve the full contract, not merely tests. A definitive precommit rollback and pre-exec generation mismatch keep their prescribed observable outcomes; authoritative uncertain commit, audit loss and actual binding/publication integrity corruption still fail closed. First capacity latch emits one existing-schema transition; repeated bad/good samples cannot duplicate it or reopen admission. No new wire/state enum or observable policy is granted.

The exact18source ceiling is unchanged EXECUTION PackageB. All tests, contracts, dependencies, other source and live state are read-only. Evidence writes are limited to new cwd/.cowork-core-evidence/attempt-4, with its tmp child as TMPDIR and umask022. Explicit gpt-6.1-sol/native-default, timeout1800seconds, networkfalse/mcpempty, with fresh native quota admission. No source commit, push, auth/retention/operator implementation, listener, app, secret, install or schedule change. Known worker Seatbelt sysctl/ps denial is an environment limitation, never reason to weaken production/tests or claim a host pass. Root and independent QA run all accumulated lanes and required performance after stable terminal freeze.

This reviewed correction is not automatic failed-job replay. Both prior v4 failures and the first v5 failure remain preserved. A second valid failure of this approved revision returns design_error; changing IDs/model/agent cannot reset the ledger. Private core-revision-2/PREBUILD-ATTEMPT-4.json records exact pins and request3340443b-27f2-4cf2-89d2-395df6a2cf59. Source acceptance and dependent auth remain blocked until full B0.


### Candidate 4 final rejection and reviewed revision 3 proposal

Root completed all host and independent verification for terminal job d583bbd4-061b-45ec-a4a9-db5a1ee5a2c8. The frozen source at bec965aeff0471c714bd42c9f5b2354010c82a57 has patch SHA256 3981d2beff8e7f14d5253f942a90afcd7177e4704379a17fcd981fa76dfa2df4. Full maintained host inventory is 899 passes, zero failures/errors/skips, all 28 lanes passing; report SHA256 456464e6d5f4c9f1a098d529442312d7a939dba2b9adb73d85dc4a06327835d9. Required matrix, ordinary and supplemental measurements are complete. Source remains uncommitted.

Independent actual-runtime probe C4-01 nevertheless fails: persisted policy {} raises KeyError before complete validation, ends the actual monitor without fatal admission/signaling and permits an unrelated task creation through real IPC. Root reproduced the unchanged negative script (one failure, zero errors) and both controls (two passes). B1/B6/B7 fail across all eleven reviewed B rows. This is REJECTED/design_error: second valid failure under approved revision 2, cumulative fourth. No same-design retry or dependent auth implementation is granted.

Root checked all 29 final artifacts and 680 source/normative/fixture/prior-protected identities with zero mismatches. B-FINAL.json SHA256 6ecb61b42e88cf4666dd1fafd502f01eddd98d86d8ed063aeb2f2ec0a5023c70; FINAL-MANIFEST.json SHA256 28fbb2f92cfda2962f712dd6f8e8335b6221ee9e97e99b963c798416dcfdd486. Final rejected feedback a6ab574f-c50d-4d55-b710-774d76d06049, sequence 28. Private core-candidate-attempt-4/ROOT-FINAL-PACKET-VERIFIED.json records the complete verification. Prior attempts/results remain unchanged.

Independent QA reviewed the revision 3 proposal for complete canonical validation and explicit runtime monitor-lifetime ownership. Its scheduling demonstration confirms a completion callback alone is insufficient; the same runtime must check monitor terminal state at admission and existing post-wait boundaries before effects. Expected shutdown is separately owned. No new watcher, retry, restart, reopening, timeout grace or blanket public-error policy is proposed. Forest approval is pending; no core contract, source or exam changes follow until that decision. Fresh D and independent additive committed exams/A would then precede a bounded source build and full B.

Separately, auth 009 v6/operator 010 v3 drafts were reconciled under ab4aff9 and committed at 9bf0a5d. Root verified the four-document scope, unchanged examples and interface/AC parity and recorded draft-only acceptance bf94fae7-913d-4b29-aa80-3023361dde59, sequence 27. Independent full D reviews are running; old v5 D0 does not transfer to revised documents. Auth source remains dependent on accepted core. Settled GitHub/storage/retention choices stand, the completion-trigger question is still pending, and no live setup is authorized by these document reviews.


### Independent auth v6 and operator v3 technical D closure

Both complete independent D reviews pass all33 rows with zero findings. Root read every disposition, checked the frozen four-document bytes against current repository files and verified all26 evidence-artifact hashes (14auth/12operator), zero mismatches. Seven diagrams rendered. Auth report SHA256ac404ac2cef5737434a661dbffc480ab645e7b287006c43cff233539fe105480, readiness315cb515a4c4bf85cb4befb62636d74d611536907fd784c71a88ca4e918936d8, manifestcaedc7fff4e8200b9ed8e245336d8b12097e1249d09ad06edd8d03ba421fe419. Operator report3f2581b591f17da9f87fe8440a803ca7ad73ec08fa0082c226473b7700a30e0d, readiness581f8e4dea193953f77f7b491c885959e813172d5ba26516a1c13774be073a4a, manifest4b7f9cdc353efb6088478bd53dbeeaaf7b12c3055085c7142aedec84f8623cbf. Root verification is private core-revision-2/AUTH-OPERATOR-D-ROOT-VERIFICATION.json.

This D0 applies only to technical drafts at9bf0a5d. A remains NOT RUN; no new exam, source, custody, provisioning or deployment grant follows. Core candidate4 design_error and all earlier failures remain. The shared auth runner amendment must wait for accepted core B and explicit exam-owner authority. Core revision3 approval and the separate retention completion-trigger answer remain pending. No provider/model calls, credentials, installs, protected exams or live state were touched by these reviews.
