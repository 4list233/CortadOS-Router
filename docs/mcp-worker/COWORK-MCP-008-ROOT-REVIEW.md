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
