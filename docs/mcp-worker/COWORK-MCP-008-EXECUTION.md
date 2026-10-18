<!-- PURPOSE: Allocate nonoverlapping future execution responsibilities and independent gates.
     CONTRACT: RT-COWORK-MCP-008 v3; draft, not a dispatch authorization.
     DEPENDS: Parent contract, recorded root policies, independent protected fixtures.
     CEILING: This session writes documents only; no production, exams, commits or live actions. -->
# Cowork 008 execution packages

```yaml
contract_id: RT-COWORK-MCP-008
contract_version: 3
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
- docs/mcp-worker/COWORK-MCP-008.md
- docs/mcp-worker/COWORK-MCP-008-DECISIONS.md
- docs/mcp-worker/COWORK-MCP-008-EXECUTION.md

Required inputs before LOCAL CORE dispatch: pinned v3 parent/source hashes, exact reviewed scopes,
synthetic mandatory config fixtures, protected exam commit/manifest, independent D then A and root
package approval. No D/A pass is declared. O1 actual registration/identity and O5 deployed limits/
retention remain pending activation choices; they MUST NOT block parameterized local enforcement.
Auth implementation needs its own future reviewed package; it MUST NOT be smuggled into core scope.
No actual Cowork connection is required for local fixtures.

Standards/rubric and governing file paths are in parent §1. Source checkout is absolute
/Users/velox/.cortados-builds/cortados-router-repo, inspected HEAD
d4c99dbfb065813e61cdaed6c4a86e9edce73ca6. Historical source anchors remain in parent. Before dispatch root records current per-file SHA-256 and
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

Exact future scope_paths (existing unless marked NEW):
- integrations/mcp/broker.py
- integrations/mcp/job_store.py
- integrations/mcp/codex.py
- integrations/mcp/worker.py
- integrations/mcp/remote_config.py — NEW
- integrations/mcp/remote_schemas.py — NEW
- integrations/mcp/remote_dispatch.py — NEW
- integrations/mcp/remote_http.py — NEW
- integrations/mcp/task_store.py — NEW
- integrations/mcp/task_workspace.py — NEW
- integrations/mcp/artifact_store.py — NEW
- integrations/mcp/file_transfer.py — NEW
- integrations/mcp/transfer_tickets.py — NEW
- integrations/mcp/remote_native.py — NEW

No wildcard scopes. Parents integrations/mcp and docs/mcp-worker exist; new modules above are planned,
not already implemented. Every new source file MUST stay below 500 physical lines and have
PURPOSE/CONTRACT/DEPENDS/CEILING headers. Current broker/job_store/worker are 342/265/262 lines;
keep integration there to seams, not new complete owners. If split needs another path, root amends and
QA rechecks scope before builder writes it. No unreviewed utilities module or copied business rules.
Existing config.py/schemas.py/native_profile.py/native_process.py/router/quota code are read-only;
extend through explicit remote composition, not changing local schemas or fresh-quota algorithms.

| Responsibility / sole module owner | Interface and consumers | Disposition / forbidden duplication |
|---|---|---|
| remote_config.py | load_core_config(raw,base) → CoreConfig (§8) | Closed mandatory config; no environment or implicit defaults. |
| remote_schemas.py | validate_remote(tool,args,limits); remote_tool_schema(tool); project_remote_result(record) | Own parent schemas/errors/projections; reuse local evidence/selection validators where behavior agrees. No storage/routing. |
| remote_dispatch.py | dispatch_remote(principal,tool,args) through broker-owned instance | Authorize, lock/replay, invoke task store and existing broker routing/stop/feedback; never another queue. |
| task_store.py | transaction-scoped task/operation/binding/publication mutations on JobStore.db | No independent connection/commit; migration SQL applied once by JobStore. All owner predicates central here. |
| task_workspace.py | prepare/reconcile/remove generated workspace by broker intent and trusted descriptors | Folder identity and safe filesystem traversal owner; no caller-selected path. |
| artifact_store.py | stage/seal/snapshot/reconcile immutable blobs from broker intents | Hash/fsync/inode rules; broker-local fd passed to file mover; no metadata commits. |
| transfer_tickets.py | mint/replay/claim/revoke/authorize using task_store transaction context | Capability construction and lease transitions; no OAuth or byte mover. |
| file_transfer.py | Broker-side §8 framed stream operations through artifact_store | Broker owns all descriptors; raw bytes cross socketpair; no FD-ID handoff. |
| remote_http.py | create_test_app with trusted explicit synthetic principal (§8) | No auth adapter, listener, CLI or global ASGI app. Reuse installed MCP SDK; no FastMCP dependency in core. |
| remote_native.py | verify persisted manifest; construct remote argv/manifest handoff from trusted binding | Complete remote-only policy template; reuse TOML encoder/feature constants, no ordinary argv changes. |
| job_store.py / broker.py | additive transaction/binding seam, remote IPC dispatch, policy bootstrap | One integration owner; existing acceptance and fresh START admission retained. |
| codex.py / worker.py | branch only on trusted persisted policy; pass/check bootstrap identity | Ordinary branch byte-identical; missing remote identity fails closed. |

[FIXED] Exact scope rationale: broker.py:133–189 owns dispatch/admission/IPC;
job_store.py:26–162 owns DB version and acceptance transaction; worker.py:31,91 and
codex.py:42 own bootstrap/argv. These are extended only at parent §8's typed seams.
server.py:20,58 and __main__.py:16 are read-only reuse evidence: core consumes schema/SDK patterns,
but MUST NOT change stdio registration or add a remote entrypoint. pyproject.toml stays read-only:
core uses existing dependencies, no auth extra. New modules' parent integrations/mcp exists.
No alternate route/queue/schema owner is authorized. All new interface records are defined in parent
§8; any missing observable semantic MUST return to root for amendment, never builder discretion.
Contracts have no line cap under current project AGENTS; each source module MUST remain <500 lines.

Private transport MUST use parent §8's closed operation table and trusted socketpair construction.
It MUST NOT extend stdio's JSON protocol with a caller-selected owner/channel. No listener or socket
path is installed. The broker owns every staging/blob descriptor across the entire lease; HTTP
receives bounded raw bytes, never an integer pretending to transfer an FD across processes.

Execution order inside B: schemas/config → additive store/atomic acceptance → generated workspace/
immutable artifacts/tickets → native policy bootstrap → synthetic ASGI/private IPC composition → compatibility review.
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
Planned exact acceptance_paths, all NEW, under verified parent acceptance/ (cowork_mcp directory is NEW):
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

These exact new paths are candidates for QA's independent fixture design; missing directory/fixtures
remain explicit blockers, not passed tests. QA commits with Exam-Owner attribution and pins hashes
BEFORE B dispatch. Any needed additional fixture path requires recorded scope amendment. Native capture
file is initially captured shell evidence plus explicit actual-exec pending marker, never synthetic proof.
QA authors deterministic fake-native cases independently; actual native/tool lane is gated separately.

| AC | Deterministic/captured local ground truth for Q | Later gated evidence |
|---|---|---|
| 008-01 | FUTURE AUTH only; no auth fixture or implementation in LOCAL CORE | Independent future-auth D/A includes discovery/challenges/claims tests; then actual hosted callback |
| 008-02 | FUTURE AUTH only; private review D-008-01/02 remain blockers | Preparse initial/explicit/transparent refresh, full credential-load lock, synthetic concurrency/crash and sentinel exams MUST precede future-auth dispatch; live rotation/deadlines follow |
| 008-03 | Synthetic trusted principal mapped to owner; second principal/client denied; expired/unknown/spent quota refuses explicit and S1 without native/API fallback | Root owner/client registration evidence |
| 008-04 | ASGI disconnect/transport DELETE cannot kill/retry accepted job; no new stdio imports/entrypoints; identity injection possible only through Python constructor | Preserve existing private 443→8432 while public candidate disabled/enabled |
| 008-05 | Closed schemas for every request/result/error; UUID spelling, unknown fields, cwd, title/name/media boundaries, non-UTF8 JSON rejected; repo-free task | Actual Cowork tool-call payload |
| 008-06 | Local stdio job ID, foreign task/job/artifact/status/cancel/feedback all refused with identical not-found response | Single-owner operator mapping |
| 008-07 | Concurrent identical create/upload/delegate/feedback returns one identity; changed text/order conflicts; replay before scorer and quota; no new attempt after lost ACK | None needed to build |
| 008-08 | Delete/upload/delegate/publication races; no overlapping job; incomplete/foreign inputs refuse; clean attempt workspace contains only named inputs | None needed to build |
| 008-09 | Ordinary argv golden byte comparison; policy persisted across restart/replay/config changes; missing policy denies START; shell review 13/13 remains accurately labeled | Actual Codex exec tool canary: positive task use, negative credential/broker/sibling/tmp/symlink/network, native auth/quota/termination succeeds |
| 008-10 | Byte-equal binary PUT/GET, zero bytes, no text decoding, archives opaque, no URL fetch/base64 API | Actual Cowork PDF/DOCX/XLSX/non-UTF8 local hash→upload→worker output→download→materialized file hash |
| 008-11 | Content-Length absent/false, chunked/encoding, early EOF/extra byte/hash mismatch, concurrent PUT, expired/spent ticket, key rotation/restart revoke, lost ACK | Header-capability support in Cowork actual egress |
| 008-12 | fd-anchored symlink/hardlink/rename/FIFO/device/traversal cases, no events/auth/log export, forbidden output directory fails publication | OS/runtime capture on target Studio |
| 008-13 | Native terminal differs from publication failure; all-or-none manifest; failed output partial flag; future workspace mutation leaves old blob unchanged | Planner review/feedback in actual Cowork |
| 008-14 | Repeated full GET exact bytes, Range refusal, delete/disable closes stream, opened expiry behavior; foreign capability denied | Cowork materialization, not merely HTTP 200 |
| 008-15 | Crash injection before/after mkdir/fsync/rename/commit/association/delete; one accepted job; corrupt DB/blob fail closed; restore disabled first | Auth-store restart/crash qualification |
| 008-16 | Decimal budget boundaries, copy/staging reservation counted once, count/deadline/backpressure exhaustion, no scheduled sweep exists; active/uncertain never deleted, tombstones retained | Live retention freeze, native disk-growth overshoot/headroom qualification |

Commands below are FUTURE QA instructions, not a record of runs. QA must pin the interpreter/venv and
fixtures before using them. Use a temporary isolated state directory under an authorized build tree;
never production state. No live credentials are needed for deterministic lanes.

```sh
git rev-parse HEAD
git diff --stat
rg --files tests acceptance
python3 -m unittest discover -s acceptance/cowork_mcp -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest discover -s acceptance/mcp_worker -p 'test_*.py'
python3 -m unittest discover -s acceptance/native_profile -p 'test_*.py'
python3 -m unittest discover -s acceptance/mcp_learning -p 'test_*.py'
python3 -m unittest discover -s acceptance/quota_evaluation -p 'test_*.py'
python3 -m unittest discover -s acceptance/fresh_quota -p 'test_*.py'
python3 -m unittest discover -s acceptance/quota_sources -p 'test_*.py'
python3 -m unittest discover -s acceptance/training_teacher -p 'test_*.py'
python3 -m unittest discover -s acceptance/router_followup -p 'test_*.py'
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

## v3 package-specific D/A inputs and AC mapping

[FIXED] Q MUST freeze runnable isolated commands/fixtures before A; this is a draft inventory, not exams.
Q owns the exact acceptance paths listed above; test_auth.py is reserved for a later scope amendment,
not this package. No current turn may create/run tests. Future Q command:
`python3 -m unittest discover -s acceptance/cowork_mcp -p 'test_*.py'`
MUST run every new core lane with zero unexpected skips. Existing accumulated runner remains required.

| Exact Q file | Additional v3 obligations / ground truth |
|---|---|
| test_schemas.py | AC-008-03,05: every config key absent/unknown/type error refuses before side effects; every §3 success/error example validates; no caller identity/path; complete config snapshot retained |
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

No-public-activation gates remain: Forest identity/provider/client decision; concrete deployed limits
and retention (payload, prompt/history, tombstones, backups); independent auth D/A with qualified
Authlib hooks/locking; dependency lock; actual native tool confinement; measured disk/headroom;
exact ingress/callback/rollback scope preserving private 443→8432; actual Cowork byte access,
Authorization-header egress, approved destination and materialized PDF/DOCX/XLSX/non-UTF8 hashes.
No listener, auth registration or live storage may activate until all relevant gates are recorded.

Latest evidence read during v3: native-exec-capture-v2/independent-adjudication.json qualifies thirteen
synthetic shell outcomes for job ba4c3e14-c1f7-47fb-93b6-d703a98a6db7 with a recorded unused dot argument.
The strict exact-command comparison remains false. Parent §9 records hashes/limits; Q MUST preserve
that deviation and original capture, not require a redundant model run or call this full native approval.
