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
| [S1-CONTEXT.md](S1-CONTEXT.md) | Root; scoring brief, worker handoff and measured classifier limitations. |
| [USAGE.md](USAGE.md) | Root orchestrator; standalone setup, Claude handoff and explicit connector grants. |
| [QA-BASELINE.md](QA-BASELINE.md) | Independent QA; all maintained suites and pre-existing failures. |
| `acceptance/mcp_worker/` | Independent QA; protected exams, native captures and evidence. Builders may read only. |
| `acceptance/router_followup/` | Independent QA; six baseline repair cases and pristine Hermes Git source fixtures. |
| `acceptance/native_profile/` | Independent QA; nine version/capture cases and real per-model 0.163.0 captures. |
| `bin/route-crons.py` | Router; adopted cron outcome collector, occurrence attribution, saved job pins remain inputs; AC-F02/F03 and original cron integration. |
| `integrations/mcp/native_profile.py` | Router; exact captured-version observation and legacy job provenance; AC-N01–N04. |
| `integrations/mcp/` | Scoped production builder after D/A approval; MCP service, lifecycle and native boundary. |
| `cortados_router/route_task.py` | Scoped production builder; optional scoring and existing resolver integration. |
| `cortados_router/worker_admission.py` | Scoped production builder; common execution admission. |
| `cortados_router/scarcity_codex_native.py` | Scoped production builder; typed native quota input to the existing scarcity owner. |

The [original branch plan](../SUBAGENT-MCP-PLAN.md) remains historical until the reviewed contract
supersedes its implementation details. No document here changes CortadOS's global decisions log or
claims that its live Hermes deployment has been modified.
