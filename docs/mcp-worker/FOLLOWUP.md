<!-- PURPOSE: Pin narrowly authorized baseline repairs and their independent gate.
     CONTRACT: RT-MCP-FOLLOWUP-001 v1 extends the completed RT-MCP-BUILD-001 scope.
     DEPENDS: RT-MCP-001; approved CortadOS ROUTER-DESIGN §9; pristine Hermes Git inputs. -->
# Baseline repair package

```yaml
contract_id: RT-MCP-FOLLOWUP-001
contract_version: 1
owner: Forest
status: approved
implements: [RT-MCP-001]
depends_on: [RT-MCP-BUILD-001]
attempt_ledger: []
```

Forest requested all four remaining baseline failures fixed, the full suite green, then Claude Code
installation on 2026-10-17. Local scoped commits on `plan/subagent-mcp` remain authorized. The separate
shared CortadOS checkout MUST NOT be committed by this package. This package changes neither the MCP
wire contract nor its accepted native version. Native 0.163.0 readiness remains a separate amendment.

## Fixed source scope and existing owners

Search inspected `tests/integration/test_cron_dispatch_routing.py:82`, the donor
`/Users/velox/cortados/bin/route-crons.py:1`, `ROUTER-DESIGN.md:404` in that donor checkout,
and `tests/integration/test_router_reviews_cortados.py:35`. The standalone sampler is absent.
The shared roster still declares compulsory review and fixed Gemini reviewer brains. The two installer
tests stage an already-patched host, although their original assertions require pristine inputs.

- [FIXED] The builder MUST adopt the donor cron **outcome collection** at new `bin/route-crons.py`.
  This is the standalone export of that responsibility, not a second algorithm inside this package.
- [FIXED] The builder MUST retain `profiles()`, `db()`, `record_outcomes(connection)`, `BAD_ROUTES`, and
  the `core` module injection seam used by the original integration test.
- [FIXED] The builder MUST bootstrap the standalone `cortados_router` source directory for `import core`.
  This keeps existing direct-file callers while preventing imports from an unrelated CortadOS plugin.
- [FIXED] `HERMES_HOME`, when set, MUST select the sampler's Hermes profile root.
  Its absent-variable default MUST remain `Path.home() / '.cortados/hermes'`.
- [FIXED] The sampler MUST retain donor header-only attribution, repeated-occurrence idempotency,
  failed-route recording, and existing output scan limits.
- [FIXED] The sampler MUST NOT rewrite saved job models or invoke auxiliary provider steering.
  Copying the donor's unrelated steering function would turn a missing export into an unrequested
  configuration deployment.
- [FIXED] The builder MUST add PURPOSE/CONTRACT/DEPENDS and reasons, units, and two-sided consequences
  at retained numeric limits; this is explanation of retained behavior, not tuning.
- [FIXED] The builder MUST modify only `rules`' one compulsory-review item and the `brain`, `role`,
  `identity` fields of `rachel` and `samantha` in `/Users/velox/cortados/faces.json`.
- [FIXED] The replacement values MUST equal `acceptance/router_followup/review-policy.json` exactly.
  These implement the previously requested opt-in dual review in CortadOS `ROUTER-DESIGN.md` §9.
- [FIXED] The builder MUST re-read `faces.json` immediately before editing and retain a private full
  before-image for root's structural diff audit. All other JSON values MUST remain identical.
- [FIXED] The builder MUST NOT run `apply-review-policy.py`, render fleet files, change SOUL files,
  restart a gateway, or mutate live Hermes state. That deployment is outside these baseline repairs.

The exact two builder-writable paths are `bin/route-crons.py` (new; `bin/` exists) in the standalone
repo and `/Users/velox/cortados/faces.json` (existing shared source). All acceptance files, existing
tests, existing implementation, contracts and other paths are excluded. Source size guidance remains
500 soft / 600 hard. The root owns the package index, handoff, usage and final evidence reports.

## Exam input repair and source provenance

The independent owner captured exactly four pristine source files at Hermes Git revision
`9796235822b89e08597a402dad045b5b4464e474`. Their immutable bytes live in
`acceptance/router_followup/hermes_pristine/`; `HERMES-SOURCE.json` records original Git blob IDs,
SHA-256 and byte counts. These are external source fixtures, not newly authored source modules.
The upstream MIT license is retained as `HERMES-LICENSE.txt`. No current mutable host file supplies
these installer inputs. The real imported runtime remains the existing isolated host environment.

- [FIXED] Full-suite execution MUST set `HERMES_TEST_ROOT` to this protected pristine directory only
  for `test_auxiliary_model_routing.py` and `test_delegation_model_routing.py`.
- [FIXED] The other integration modules MUST retain their full isolated Hermes host input.
  The full-loop module requires `run_agent.py` and runtime dependencies absent from the four-file fixture.
- [FIXED] All original tests and assertions MUST remain unchanged.
- [FIXED] The full inventory MUST include the six new follow-up acceptance cases.

This repairs test setup rather than weakening the required pending counts: original auxiliary check
still expects three, delegation four, prepatched generic-hook delegation three, and repeat install zero.
The recorded original failures remain in the baseline evidence. No original test is retired.

## Observable acceptance

| ID | Required ground truth | Independent command |
|---|---|---|
| AC-F01 | Four source bytes match recorded Git blobs; installer dry runs report 3/4 pending and write zero files; original installer modules pass. | Follow-up unittest command plus both original pytest modules with pristine `HERMES_TEST_ROOT`. |
| AC-F02 | Actual occurrence provider/model/session populate `cron_outcomes`; model text after `## Prompt` is ignored; stored job pin bytes do not change; replay adds zero rows. | Follow-up unittest command and original cron integration module. |
| AC-F03 | Sampler entrypoint uses explicitly configured temporary roots; auxiliary config bytes remain unchanged; no default live-state directory appears. | Follow-up unittest command. |
| AC-F04 | Shared roster's exact seven authorized fields match the frozen policy; original review integration passes; root's before/after JSON diff contains no other change. | Follow-up unittest command, original review integration, independent structural diff. |
| AC-F05 | Every accumulated lane has zero failures, errors, skips, or absent cases. | Root's full-inventory runner, retaining all commands, counts and source hashes. |

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest \
  discover -s acceptance/router_followup -t . -v
```

All tests use temporary mutable state. The follow-up exam reads the shared roster but never writes it.
No new hot-path algorithm is introduced: native worker performance remains the existing benchmark
obligation; the sampler adopts the donor's behavior and is not part of per-request worker execution.

## Independent readiness record

Owner: `/root/mcp_qa_followup`, continuing Forest's independent MCP QA designation. D/A review covers
the narrow repair only. Existing lifecycle/permission/store diagrams and measured benchmark remain
in RT-MCP-001; this package adds no lifecycle, store, or wire format. The sampler's `cron_outcomes`
projection retains its donor writer and bounded rescan behavior (only retained output files are recoverable); no worker store changes.

**D = 0. A = 0 at the commit containing this document and the protected manifest.** The package is
dispatchable only after that commit exists. Before dispatch, independent fixture dry runs returned
three auxiliary and four delegation changes. The new six-case prebuild result is retained beside
the exam. Expected absent-sampler and stale-roster failures do not count as builder attempts.

Root MUST independently rerun B after the builder handback. Full acceptance remains FAIL until every
accumulated case is green. Claude Code installation proceeds only after that gate and native-version
readiness. The shared roster before-image remains private outside this Git repository.

Independent prebuild: six cases, 2 pass / 2 fail / 2 error / 0 skip. The unchanged original installer modules pass 31 auxiliary and 17 delegation cases with pristine inputs. Raw commands, environments, logs and XML live under the protected follow-up evidence directory. Root independently reviewed the exact policy, source scope and exams before dispatch.
