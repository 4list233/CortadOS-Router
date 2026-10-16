<!-- PURPOSE: Constrain one independent production build of the MCP worker service.
     CONTRACT: RT-MCP-BUILD-001 v1 implements RT-MCP-001 v1 after D/A approval.
     DEPENDS: Frozen parent, independently committed exam, real native captures. -->
# MCP worker execution package

```yaml
contract_id: RT-MCP-BUILD-001
contract_version: 1
status: approved
owner: Forest
implements: [RT-MCP-001]
depends_on: []
blocks: []
approval: independent D=0 and A=0 at the protected package commit; see QA-REVIEW.md
attempt_ledger: []
```

This package implements the parent without changing its APIs, policy or acceptance criteria. The root
orchestrator owns this brief. The independent MCP QA reviewer owns the exam. The production builder
may read both and must report contradictions before changing either.

## Production scope

The following paths are new except the one explicitly marked existing:

- `pyproject.toml`
- `cortados_router/route_task.py`
- `cortados_router/worker_admission.py`
- `cortados_router/scarcity_codex_native.py`
- `cortados_router/selection_resources.py`
- `cortados_router/core.py` (existing; the parent's optional-resource parameter and call sites only)
- `integrations/mcp/__init__.py`
- `integrations/mcp/__main__.py`
- `integrations/mcp/server.py`
- `integrations/mcp/config.py`
- `integrations/mcp/schemas.py`
- `integrations/mcp/native_metadata.py`
- `integrations/mcp/grant_proxy.py`
- `integrations/mcp/broker.py`
- `integrations/mcp/job_store.py`
- `integrations/mcp/codex.py`
- `integrations/mcp/worker.py`
- `integrations/mcp/native_process.py`
- `tests/test_mcp_worker.py`
- `tests/test_mcp_route_task.py`

Every new source file carries a PURPOSE/CONTRACT/DEPENDS header. New source modules remain below the
500-line soft and 600-line hard guidance, split by responsibility. The existing oversized core only
receives the approved optional resource seam; existing calls keep their behavior. The public wheel
includes the existing routing tables without modifying their contents.

Excluded paths: `acceptance/`, all contract/review/decision documents, existing test files, live state,
Hermes integrations, installed plugins, scorer weights/training data, model catalog changes, and all
other files outside the list above. The root owns README, original-plan status, index and final QA
report updates. If an additional source file is needed for a coherent responsibility split, request a
scope amendment before creating it; no source gets hidden in an unchecked script.

## Existing owners and technical choices

The dispositions in [REUSE.md](REUSE.md) bind this build. Adopt native Codex authentication/execution,
the official MCP SDK, existing S1/core selection and scarcity formulas. Build the missing job owner
and per-job grant proxy. Do not copy an independent quota/ranking algorithm or the reference plugin's
persistence code. Configured limits carry units and two-sided reasons at their definitions.

The builder may select standard-library implementation mechanics that leave the parent behavior
unchanged. Any additional runtime dependency needs an exact pin and an explanation in the handback.
The minimum claimed host is the captured macOS native CLI profile; do not claim untested platform or
CLI-version compatibility. No live deployment, push, merge, account-login change or model training is
part of this package.

## Verification and handback

Every parent AC-MCP-01 through AC-MCP-13 applies unchanged. The independent frozen-manifest and exam
commit are recorded before dispatch. Run their exact commands from the parent, all accumulated suites
from [QA-BASELINE.md](QA-BASELINE.md), package installation and native service seam checks. Existing
baseline failures remain failures in the report; do not weaken tests or change fixtures to conceal them.

Handback includes source diff/commit, exact commands and counts, raw evidence locations, source/exam
hashes, selected implementation mechanics, measured baseline/candidate overhead, and any residual
limitations. A successful worker response is evidence of native completion; Claude still reviews the
result. Do not self-certify the independent D/A/B gates.

The orchestrator independently reruns acceptance from a clean pinned candidate and records Rubric B.
No production builder has been dispatched at this approved package revision.
