<!-- PURPOSE: Map the standalone MCP worker package and its independent evidence owners.
     CONTRACT: RT-MCP-001. This index does not approve an implementation or live installation.
     DEPENDS: CONTRACT.md, QA-REVIEW.md, and the frozen acceptance manifest. -->
# MCP worker package index

Forest requested a Codex-first subscription worker service on `plan/subagent-mcp`. Claude remains the
planner and reviewer. S1 is optional. On 2026-10-15 Forest required known quota before worker execution
and designated independent agent `/root/mcp_qa` as this standalone package's contract reviewer and
acceptance-test owner. On 2026-10-17 Forest authorized the four baseline repairs, full-suite verification,
and local Claude Code installation using existing subscriptions. Local scoped commits remain authorized;
publication is outside this task.

| Document or path | Owner and purpose |
|---|---|
| [CONTRACT.md](CONTRACT.md) | Contract author; public behavior, lifecycle, permissions and source scope. |
| [REUSE.md](REUSE.md) | Contract author; existing-owner search and reuse dispositions. |
| [DECISIONS-PENDING.md](DECISIONS-PENDING.md) | Contract author; resolved user direction and remaining evidence gates. |
| [QA-REVIEW.md](QA-REVIEW.md) | Independent QA; D/A findings and exact review revisions. |
| [BUILD-REPORT.md](BUILD-REPORT.md) | Root orchestrator; independent verification, execution ledger, raw evidence and resolved baseline gate. |
| [FOLLOWUP.md](FOLLOWUP.md) | Independent QA; baseline repair scope, exact review policy and pristine installer inputs. |
| [NATIVE-PROFILE-002.md](NATIVE-PROFILE-002.md) | Independent QA; captured 0.163.0 extension and immutable job version provenance. |
| [INSTALLATION.md](INSTALLATION.md) | Root; actual local release, operator limits, Claude registration and activation evidence. |
| [AIR-TO-STUDIO.md](AIR-TO-STUDIO.md) | Root; existing Tailscale SSH workflow with Claude, S1 and workers on the Studio; actual Air login verification remains an operator check. |
| [S1-CONTEXT.md](S1-CONTEXT.md) | Root; scoring brief, worker handoff and measured classifier limitations. |
| [USAGE.md](USAGE.md) | Root orchestrator; standalone setup, Claude handoff and explicit connector grants. |
| [QA-BASELINE.md](QA-BASELINE.md) | Independent QA; all maintained suites and pre-existing failures. |
| `acceptance/mcp_worker/` | Independent QA; protected exams, native captures and evidence. Builders may read only. |
| `acceptance/router_followup/` | Independent QA; six baseline repair cases and pristine Hermes Git source fixtures. |
| `acceptance/native_profile/` | Independent QA; nine version/capture cases and real per-model 0.163.0 captures. |
| `bin/route-crons.py` | Router; adopted cron outcome collector, occurrence attribution, saved job pins remain inputs; AC-F02/F03 and original cron integration. |
| `integrations/mcp/native_profile.py` | Router; exact captured-version observation and legacy job provenance; AC-N01–N04. |
| [LEARNING-CONTEXT.md](LEARNING-CONTEXT.md) | RT-MCP-LEARNING-003; independent context, feedback, learning and activation contract; AC-L01–L12. |
| [LEARNING-BUILD-REPORT.md](LEARNING-BUILD-REPORT.md) | Root; tested source, complete standalone inventory, context/collection timings, installed release and next-run training activation. |
| `acceptance/mcp_learning/` | Independent MCP QA; protected context/feedback/collection/pipeline exams and synthetic captures; builder read-only. |
| `cortados_router/task_evidence.py` | Exact evidence validation, scorer/worker projections, provenance and pure `context_requirements`; route_task/frontend call this owner; no admission or storage; AC-L01–L04. |
| `integrations/mcp/job_store.py::feedback` | Sole jobs.db feedback writer; append-only assertion transaction, ownership and replay; broker calls it; AC-L05. |
| `cortados_router/learning_source.py::read_observations` | Read-only jobs/feedback and route snapshots; validates source types and assembles native/planner observations without artifact reads; AC-L06/L07/L10. |
| `cortados_router/learning_data.py::collect_mcp` | Sole current-observation publisher; locked/fsynced request revisions then projection; dataset calls it; AC-L06–L08. |
| `cortados_router/learning_data.py::eligible_rows` | Shared revision/group/partition eligibility; `bind_snapshot` binds one phase; label/build_sft/evaluate and later enabled adapters use this owner; AC-L09. |
| `cortados_router/learning_data.py` | Shared teacher payload/provenance, SFT token measurement and content-sensitive evaluation identity; s1/common re-exports its unchanged `split_of`; AC-L08/L09. |
| `tests/test_mcp_learning.py` | Builder regressions for source lifetime/containment/WAL visibility, explicit evidence budgets, connected partitions and cache content identity. |
| `integrations/mcp/` | Scoped production builder after D/A approval; MCP service, lifecycle and native boundary. |
| `cortados_router/route_task.py` | Scoped production builder; optional scoring and existing resolver integration. |
| `cortados_router/worker_admission.py` | Scoped production builder; common execution admission. |
| `cortados_router/scarcity_codex_native.py` | Scoped production builder; typed native quota input to the existing scarcity owner. |
| `cortados_router/core.py::resolve` | Existing candidate burn owner; [RT-QUOTA-EVALUATION-004 v1](QUOTA-EVALUATION-004.md) pins one estimate observation per priced candidate; AC-Q01–Q06, protected `acceptance/quota_evaluation/`. |

The [original branch plan](../SUBAGENT-MCP-PLAN.md) remains historical until the reviewed contract
supersedes its implementation details. No document here changes CortadOS's global decisions log.
The authorized live Hermes and MCP changes are recorded in [FRESH-QUOTA-BUILD-REPORT.md](FRESH-QUOTA-BUILD-REPORT.md).

The standalone learning implementation exposes installed-owner helpers to the separate activation
package. Its source reads only accepted SQLite evidence; requests and labels retain immutable revisions,
and `mcp-observations.jsonl` can be rebuilt by collection. The main-phase protected exam is the five
modules named in [the exam README](../../acceptance/mcp_learning/README.md). The separately tested
activation stage is installed for the next nightly invocation. The October 17 run retained its old
environment/script inode; its saved-checkpoint evaluation is now complete and rejected by the unchanged
promotion gates. The build reports record independent verification and limitations.

| Fresh-quota path | RT-FRESH-QUOTA-005 responsibility |
|---|---|
| [FRESH-QUOTA-005.md](FRESH-QUOTA-005.md) | Frozen strict fresh quota contract; independent `acceptance/fresh_quota/` exams. |
| [FRESH-QUOTA-BUILD-REPORT.md](FRESH-QUOTA-BUILD-REPORT.md) | Root; final suite, frozen source, performance, guarded live activation and operational limitations. |
| [QUOTA-OPERATIONS.md](QUOTA-OPERATIONS.md) | Root; quota percentages, pace, learned cost, inspection command, dispatch limits and saved-checkpoint result. |
| `cortados_router/quota_admission.py` | Sole source scope canonicalizer, fresh pool validation, `check`, `source_preflight`, pure `resources` and `current_resources`. |
| `cortados_router/quota_observations.py` | Bounded per-source process-group collection and ephemeral in-flight/cache owner; no store writes. |
| `cortados_router/selection_resources.py` | Immutable lanes, health, exact allowed routes and optional learned-cost snapshot; adopts core mapping. |
| `integrations/mcp/native_metadata.py::observe` | Uncached read-only validated status, preserving spent readings; snapshot remains strict. |
| `cortados_router/scarcity.py::_codex_native` | Existing inspection source delegates typed native status; TUI parser remains diagnostic only. |

| Quota-source repair path | RT-QUOTA-SOURCES-006 responsibility |
|---|---|
| [FRESH-QUOTA-SOURCES-006.md](FRESH-QUOTA-SOURCES-006.md) | Contract author; native subscription-source repair, private actual-context binding and independent source evidence. Common/Cursor slice D=0/A=0/B=0 and activated; Claude/Antigravity remain unproven. |
| `cortados_router/quota_sources.py` | Native source selection/read-only projection; existing observation/admission owners remain canonical. |
| `cortados_router/cursor_quota.cjs` | Version-bound reuse of installed Cursor native credential/configuration/DashboardClient owners; no quota policy or cache. |
| `acceptance/quota_sources/` | Independent QA; real native shapes, actual-context and staged-boundary exams; builder read-only. |

| [FRESH-QUOTA-SOURCES-BUILD-REPORT.md](FRESH-QUOTA-SOURCES-BUILD-REPORT.md) | Root; 695-test source repair, timing, guarded activation and provider blockers. |
| [CLAUDE-QUOTA-SOURCE-006-ANNEX.md](CLAUDE-QUOTA-SOURCE-006-ANNEX.md) | Disabled evidence draft; no Claude source implementation authority. |


| Training teacher path | RT-TRAINING-TEACHER-007 responsibility |
|---|---|
| [CODEX-TRAINING-TEACHER-007.md](CODEX-TRAINING-TEACHER-007.md) | New final-training label fallback to existing Codex subscription worker; Claude primary, evaluation labels/gates preserved; B1/B2 failures retained; revision2 cumulative attempt3 accepted D0/A0/B0 with 785 passing tests; activated and independently verified with 12 new training labels. |
| `cortados_router/teacher.py` | Teacher selection and existing broker client adapter; no native harness or split implementation. |
| `cortados_router/teacher_attempts.py` | Training slots, private fallback attempt mapping and sole label publication owner. |
| `cortados_router/worker_admission.py` | Existing native admission plus closed teacher-only scarcity ceiling; ordinary worker behavior unchanged. |
| `acceptance/training_teacher/` | Independent exam owner; split isolation, replay/publication, native parity and installed labeling path. |
| [CODEX-TRAINING-TEACHER-BUILD-REPORT.md](CODEX-TRAINING-TEACHER-BUILD-REPORT.md) | Root; accepted revision 2, 785 passing tests, guarded activation, 12 verified training labels, preserved exam and operating limits. |

| Cowork connector path | RT-COWORK-MCP-008 responsibility |
|---|---|
| [COWORK-MCP-008.md](COWORK-MCP-008.md) | Frozen local core: generated task folders, confined native execution and exact binary transfer. Independent v5 D0/A0 and amended114core exams committed at9722a88. Candidate4 passes899 maintained tests but fails independent B at B1/B6/B7. Second revision2 failure is design_error; revision3 proposal awaits Forest. No accepted release; earlier failures remain preserved. OAuth and activation are separate. |
| [COWORK-MCP-008-DECISIONS.md](COWORK-MCP-008-DECISIONS.md) | Pending choices and evidence gates; does not amend global rulings. |
| [COWORK-MCP-008-EXECUTION.md](COWORK-MCP-008-EXECUTION.md) | Proposed implementation and independent exam ownership; local gates precede public activation. |
| [COWORK-MCP-008-ROOT-REVIEW.md](COWORK-MCP-008-ROOT-REVIEW.md) | Planner review, independent sandbox capture, authentication qualification and honest accumulated baseline. No public activation. |
| `integrations/mcp/remote_runtime.py` (new, proposed) | CoreRuntime composes the existing broker under its exclusive state lock; no-listener startup, recovery, IPC session and monitor lifetime. No second job queue. |
| `integrations/mcp/remote_ipc.py` (new, proposed) | One bounded framed socketpair codec and CoreClient/server connection lifecycle; no descriptor-number handoff. |
| `integrations/mcp/remote_capacity.py` (new, proposed) | Task/installation file accounting and reservation measurements; existing JobStore owns writes and Broker owns worker stop. |

| `cortados_router/route_task.py::route_task` | RT008 extends the existing route owner with keyword-only trusted full-handoff input for byte bounds and all resolver input-fit calls; None preserves ordinary behavior. |
| `integrations/mcp/task_store.py` (new, proposed) | RT008 remote SQL record/ownership statements, including private workspace preparation; transactions remain owned by JobStore. |
| `integrations/mcp/task_workspace.py` (new, proposed) | RT008 descriptor-anchored empty preparation, accepted input copies and proven cleanup. |
| `integrations/mcp/remote_config.py` and `remote_schemas.py` (new, proposed) | RT008 closed configuration, private/wire records, safe errors and audit encoding. |
| `integrations/mcp/remote_dispatch.py` and `remote_http.py` (new, proposed) | RT008 owner-scoped core operations and listener-free synthetic ASGI composition; auth is excluded. |
| `integrations/mcp/artifact_store.py`, `transfer_tickets.py`, `file_transfer.py` (new, proposed) | RT008 immutable artifacts, scoped capabilities and bounded descriptor-owned byte streams. |
| `integrations/mcp/remote_native.py` (new, proposed) | RT008 frozen native manifest, complete handoff appendix and execution binding. |
| `integrations/mcp/broker.py`, `job_store.py`, `codex.py`, `worker.py` | RT008 extends the existing lifecycle/transaction/native owners only at reviewed remote seams; ordinary001–007 remains unchanged. |
| `acceptance/cowork_mcp/` | Independent QA owns114protected core methods at9722a88, current manifest8e32a0d697e1da3b06fef53ea8be9e2fd93363c7b88e2e2ab5b9fec8d73525dc. All original assertions are preserved except the explicitly approved v5 deadline response consequence; seven later technical observation corrections are recorded. Full899/28lane inventory passes; additional actual-runtime integrity probe fails. Exams and measurements remain builder read-only; candidate4 rejected. |

| Future authentication draft | RT-COWORK-AUTH-009 responsibility |
|---|---|
| [COWORK-AUTH-009.md](COWORK-AUTH-009.md) | Subordinate draft for inherited AC-008-01/02; maintained OAuth provider, strict identity/lifetime checks and credential-operation serialization. Version6 at9bf0a5d passes all33 independent technical D rows after initial-lifecycle reconciliation; prior v5 evidence remains historical. A/B pending; shared-runner interpreter amendment still proposed. |
| [COWORK-AUTH-009-EXECUTION.md](COWORK-AUTH-009-EXECUTION.md) | Prospective nonoverlapping auth/production composition/exam paths; no source grant until accepted core and independent auth gates. |

RT008/009 retain the existing standalone Router naming/location convention rather than the CortadOS structural engine CTX prefix. No global contract or rulings-log migration follows. The optional remote dependency proposal is isolated from ordinary stdio dependencies; implementation and its lock require their separate reviewed package.

| Authentication ownership being frozen before D/A | Proposed responsibility; no source grant yet |
|---|---|
| `integrations/mcp/remote_auth_config.py` | Pure closed authentication configuration and private record validation. |
| `integrations/mcp/remote_auth_provider.py` | Maintained provider/client hooks and strict upstream response projection; no independent token engine. |
| `integrations/mcp/remote_auth_claims.py` | Strict proxy claims and live owner facts; no duplicate identity request. |
| `integrations/mcp/remote_auth_runtime.py` | Single finite credential-operation admission gate, lifecycle settlement and verification orchestration. |
| `integrations/mcp/remote_auth_storage.py` | Sole authentication lock/marker/manifest/barrier and maintained-store trust guards. |
| `integrations/mcp/remote_auth_transport.py` | Bounded upstream HTTP reads for maintained OAuth and identity clients. |
| `integrations/mcp/remote_auth_errors.py` | Fixed safe errors/events and dependency diagnostic containment. |
| `integrations/mcp/remote_supervisor.py` | Trusted parent core and spawned frontend lifetime; private lifecycle control, no task queue. |
| `integrations/mcp/remote_service.py` | Authentication HTTP route/metadata adaptation and service entrypoint after accepted core. |

The isolated optional dependency qualification requires `fastmcp-slim[server]==3.4.9`, `Authlib==1.9.1` and unchanged `mcp==1.30.0`; plain slim omits required server dependencies. The corrected65-package graph/imports have been tested only on CPython3.14.6/macOS arm64 in a new private environment. This is dependency evidence, not authentication acceptance or installation. Root technical directions and exact evidence are in ROOT-REVIEW at5d98d77/a93f48b.

| Operator setup proposal | RT-COWORK-OPERATIONS-010 /110; no implementation or activation grant |
|---|---|
| [COWORK-OPERATIONS-010-DRAFT.md](COWORK-OPERATIONS-010-DRAFT.md) | Draft-only extension of existing AuthStorage/provider/runtime/service owners for child-only secret resolution and initial offline provisioning. Version3 at9bf0a5d reconciles009v6 under ab4aff9 and passes all33 independent technical D rows; A/source remain blocked. Approved GitHub/limits/retention direction is retained. Secret custody and activation remain separate. |
| [COWORK-OPERATIONS-010-EXECUTION-DRAFT.md](COWORK-OPERATIONS-010-EXECUTION-DRAFT.md) | Proposed exact six-owner extension and independent exams; no new issuer, store, supervisor, scheduler or module. Local and activation gates remain separate. |

| Status and revision | Root-owned evidence and decision |
|---|---|
| [COWORK-MCP-BUILD-REPORT.md](COWORK-MCP-BUILD-REPORT.md) | Root; candidate4 has899 passing maintained tests but a reproduced integrity failure, independent B3 and design_error. Complete measurements and prior failed packets preserved. Not installed. |
| [COWORK-MCP-008-REVISION-2-PROPOSAL.md](COWORK-MCP-008-REVISION-2-PROPOSAL.md) | Forest-approved HTTP/IPC deadline and capacity-only inspection revision (2026-10-18). Renewed contract/exam gates remain mandatory; original attempt history is preserved. |
| [COWORK-MCP-008-REVISION-3-PROPOSAL.md](COWORK-MCP-008-REVISION-3-PROPOSAL.md) | Reviewed canonical-read and monitor-lifetime revision; awaiting Forest approval after the second failed revision2 implementation. No source/exam grant. |
