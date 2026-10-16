<!-- PURPOSE: Map the standalone MCP worker package and its independent evidence owners.
     CONTRACT: RT-MCP-001. This index does not approve an implementation or live installation.
     DEPENDS: CONTRACT.md, QA-REVIEW.md, and the frozen acceptance manifest. -->
# MCP worker package index

Forest requested a Codex-first subscription worker service on `plan/subagent-mcp`. Claude remains the
planner and reviewer. S1 is optional. On 2026-10-15 Forest required known quota before worker execution
and designated independent agent `/root/mcp_qa` as this standalone package's contract reviewer and
acceptance-test owner. Local scoped commits are authorized; publication and live installation are not
part of this build.

| Document or path | Owner and purpose |
|---|---|
| [CONTRACT.md](CONTRACT.md) | Contract author; public behavior, lifecycle, permissions and source scope. |
| [REUSE.md](REUSE.md) | Contract author; existing-owner search and reuse dispositions. |
| [DECISIONS-PENDING.md](DECISIONS-PENDING.md) | Contract author; resolved user direction and remaining evidence gates. |
| [QA-REVIEW.md](QA-REVIEW.md) | Independent QA; D/A findings and exact review revisions. |
| [USAGE.md](USAGE.md) | Root orchestrator; standalone setup, Claude handoff and explicit connector grants. |
| [QA-BASELINE.md](QA-BASELINE.md) | Independent QA; all maintained suites and pre-existing failures. |
| `acceptance/mcp_worker/` | Independent QA; protected exams, native captures and evidence. Builders may read only. |
| `integrations/mcp/` | Scoped production builder after D/A approval; MCP service, lifecycle and native boundary. |
| `cortados_router/route_task.py` | Scoped production builder; optional scoring and existing resolver integration. |
| `cortados_router/worker_admission.py` | Scoped production builder; common execution admission. |
| `cortados_router/scarcity_codex_native.py` | Scoped production builder; typed native quota input to the existing scarcity owner. |

The [original branch plan](../SUBAGENT-MCP-PLAN.md) remains historical until the reviewed contract
supersedes its implementation details. No document here changes CortadOS's global decisions log or
claims that its live Hermes deployment has been modified.
