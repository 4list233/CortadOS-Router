# Independent MCP worker contract review

Reviewer and exam owner: `/root/mcp_qa`, designated by Forest through the root session on 2026-10-15.
This record covers design/readiness, not production acceptance. No builder has been dispatched.

## D review 1 — draft returned with findings

Inspected parent: `RT-MCP-001` v1, `CONTRACT.md` sha256
`e68d81576791d4c28ca099f00c84bf652b0a24e2aeb792ce94723c23de851e94`.
`REUSE.md` and `DECISIONS-PENDING.md` were inspected with the parent; baseline is `39df485`.

**D: 8 grouped actionable defects; NOT PASSED. A: NOT RUN.** These are authoring findings,
not failed builder attempts. Policy/evidence decisions already marked pending remain readiness items;
they are not newly invented approval requirements. The upstream soft contract line cap is superseded by
the applicable repository instruction and is not a defect.

| Finding | Primary rubric check | Evidence and correction |
|---|---|---|
| D-F01 Canonical request identity | D1.2 | Lines 55–57 use “canonical arguments” without defining default insertion, object ordering/encoding, path normalization, or whether list order is meaningful. Define one normalized representation, preserve task/context contents exactly, and reject invalid arguments before claiming the request key. Otherwise retries can execute twice or spuriously conflict. |
| D-F02 Native terminal/result grammar | D1.3 | Lines 186–198 and 288 rely on “final native message” and “accepted completion” without their decision predicate. Specify terminal count/order, exit status, last final message versus commentary, artifact consistency, missing/invalid usage and partial failure result rules. The real Sol capture contains nonterminal error-typed warning items and later succeeds; treating all error items as turn failures is disproved. |
| D-F03 Recovery and competing terminal intents | D6.1 | Lines 272–299 do not close spawn-before-identity recovery or define the result of unproven group termination. Define watchdog ownership before spawn, PID-reuse-safe termination evidence, reservation retention on containment failure, deadline origin, first cancellation/timeout intent, and immutable terminal elapsed time. A terminal interrupted record must not free a workspace while its worker may still run. |
| D-F04 Native metadata/state integration collision | D5.3 | The contract mandates a TUI parser repair while the new native typed metadata evidence supplies account/model/quota directly. Record the selected disposition and single scarcity owner, pin pagination/freshness/ordinaryUsageAllowed behavior, and bind the core resolver to the explicit isolated state/injected health. The original transformed metadata view is not a D-0092 wire fixture; the corrected wire capture is now available. |
| D-F05 Missing process-order diagram | D6.3 | The service spans frontend, broker, watchdog and native worker but carries only flow/state diagrams. Add the required sequence diagram showing durable claim, spawn authorization, worker identity, terminal artifact commit and client reply. Both existing diagrams render successfully. |
| D-F06 Complete result/error examples | D1.4 | Only a nested terminal result is shown, with no complete job_result envelope or failure example. Add complete concrete success/failure output shapes and scalar/nullability definitions so the independent client exam has an answer key. |
| D-F07 Reasons for invariant groups | D7.1 | Most lifecycle/identity/permission rules lack their load-bearing reason in the contract. A concise reason per coherent invariant group is sufficient; explain the duplicate effect, leaked grant, false success or lost evidence each group prevents. No repetitive per-line prose is requested. |
| D-F08 Connector grant bypass | D6.2 | The real native connector probe shows `enabled_tools=[worker_probe]` still permits built-in `read_mcp_resource` on that server. Native tool filtering alone does not enforce the proposed exact tool grant. Root selected a narrow per-job MCP proxy exposing only granted tools and no resources/prompts; this needs its own boundary/schema/ownership and protected negative cases before D can pass. |

Author received these as one amendment packet. Root subsequently confirmed known quota is required
for both selection modes and selected a typed native scarcity adapter, UTF-8 handoff measurement and
per-job connector proxy. The next parent revision must express those choices explicitly.

## Evidence already checked

- Full accumulated baseline: 221 unique cases, 217 pass / 4 pre-existing failures / zero errors or skips
  after integration execution. See `QA-BASELINE.md`; no existing test was changed.
- Real `codex-cli 0.162.1` command/final/usage captures are retained for `gpt-6-luna`, `gpt-6.1-sol`
  and `gpt-6-astra`. Sol/Astra include commentary before the final agent message. Sol includes
  ignored-config warnings as nonterminal `item.completed` records of type `error`.
- Native account/model/quota JSON-RPC wire capture retains all frame fields. Email/account ID,
  native home path and private credit balance string are redacted with provenance. The balance is
  represented by fixed `0.0000000000` and must never be used for quota admission.
- Flow and state Mermaid diagrams rendered with the existing pinned local mermaid-cli/Chrome setup,
  exit 0 each. Raw commands and SVGs are in `acceptance/mcp_worker/evidence/design-review-1/`.
- An error or synthetic fake provider case will not be represented as real-provider compatibility.

## Readiness boundary

The exam remains independently owned. Production builders may read but never edit it.
A cannot pass until the revised contract passes D, exact production scope is packaged, independently
written tests/fixtures are committed with their owner trailer, required native seam captures are present,
and the declared commands/environment actually collect the expected cases. A red prebuild test is
expected; an absent or undiscovered test is not a readiness pass.


## D review 2 — approved design

Final author revision: `82cc0d717f3d0542e350c67225ac029c2b838634710f2bb208512bfa371fbdff`.
The reviewer then applied root-authorized approval/status metadata only. Reviewed final parent SHA-256:
`a7934cdb9a78e18dc18e70eaf8973052308cec696776e2b9c55ac88768b9a740`. Final execution package SHA-256:
`2a1165b2b9d70a445088b360ba8af1c408748aea59de07a93099edc92eac19ce`. `REUSE.md`, `DECISIONS-PENDING.md`, `INDEX.md` and the exact execution scope were included.

**D: PASS — zero remaining defects across D1–D7.** Initial eight findings are closed in the amended
parent. This is prebuild review, not a builder attempt or implementation acceptance.

| Group | Independent review outcome |
|---|---|
| D1 Executability | Strict requests/config/results/errors, canonical encoding/defaults, native terminal grammar, closed events and termination receipt are defined. Worked JSON examples parse. The model-list cursor and managed-policy refusal error codes are pinned. |
| D2 Falsifiability | AC-MCP-01–13 name returned records, effects, files and observable boundaries. Commands collect real tests. The measured reference query budget has units, raw samples, formula and candidate gate. |
| D3 Latitude | Fixed behavior is explicit. No unresolved escalation remains. Operator limits are required fields, not invented product defaults. Internal standard-library mechanics stay within exact source scope. |
| D4 Locality | Names and owners align across contract, source map and exam. RT-MCP-BUILD-001 implements the parent without restating its ACs. The applicable user instruction supersedes the older contract line cap; source limits remain. |
| D5 Evidence | Existing core/scarcity owners and dispositions cite the baseline. Recorded native account/model/quota/policy, all three command/MCP/final/usage kinds, handoff bounds and containment captures exist with provenance. Conflicts and native near misses are recorded. |
| D6 Structure | Broker owns durable identity/claims, watchdog owns process containment, native adapter owns typed observations and existing scarcity owns formulas. Exact grants use a proxy. Flow, state and sequence diagrams each render, exit 0. |
| D7 Reasons | Grouped reasons identify duplicate effects, stale capacity, leaked connectors, false success and uncontained workers. Real root-group/SID and resource-exposure near misses constrain the design. |

## A review — implementation readiness

**A review: zero remaining defects across A1–A7.** The approval boundary is the successful local scoped
commit containing this report and the protected manifest. Dispatch must use that commit; an uncommitted
copy of this report is not permission to start. No production builder wrote or graded these exams.

| Group | Independent review outcome |
|---|---|
| A1 Identity/scope | RT-MCP-001 and RT-MCP-BUILD-001 are unique in this repository, version 1, with Forest accountable. The parent is referenced by the execution package. All 20 source/test scope paths are existing or marked new; parents exist, including integrations/mcp. Exact protected paths are frozen in SHA256SUMS. No live/shared-tree changes are in scope. |
| A2 Evidence | Real wire/source captures are retained before dispatch; synthetic faults are labeled. Runtime providers and host compatibility are limited to the captured profile. Existing responsibilities are adopted/extended explicitly. |
| A3 Store aspects | All seven aspects exist. SQLite is canonical, outputs are evidence, no replay-from-log rebuild is claimed. Backup/restore has a named test. Indexed lookup reference covers four serialized row sizes at 1/1,000/10,000 records; candidate compliance remains a B check. |
| A4 Invariants | Identity/workspace/capacity share the broker transaction. The watchdog START handshake, identity matching and fail-closed receipt behavior are specified. Native environment, frozen registry, proxy and policy preflight prevent caller bypass. Refusals/transitions have one emitting owner. |
| A5 Exam readiness | 53 tests collect; no loader errors/skips. Official SDK probes for stdio and bearer-authenticated HTTP work, including forbidden-operation controls. Native process replay, fixture provenance and source shapes are selfchecked. Tests are independently owned and committed with the package. |
| A6 Diagrams | All three final diagrams render; raw commands and SVGs are in evidence/design-review-2. |
| A7 Readiness | No product question remains. Non-goals, containment, handback and attempt ledger are explicit. Native deployment and publication are excluded. Source builder may now implement only the approved package after commit verification. |

### Exact prebuild execution

`/private/tmp/cortados-mcp-sdk-env/bin/python -B -m unittest discover -s acceptance/mcp_worker -t . -v`
collected and executed **53 cases: 3 pass, 3 fail, 47 error, 0 skip**, in 6.857 seconds.
Three fixture-only checks pass. The service is deliberately absent: three startup-refusal checks fail
their required error-content assertion, and the remaining service cases fail MCP connection setup.
No service behavior is claimed to pass. The prebuild red log and exact IDs are in evidence/prebuild.
Python 3.14.6, official `mcp==1.30.0`, isolated SDK environment; subprocess/ps access was authorized.
All protected Python ASTs parse, and the SDK environment is put first in the fixture PATH.

Reference command:
`/private/tmp/cortados-mcp-sdk-env/bin/python -B acceptance/mcp_worker/benchmark.py --reference-only --output acceptance/mcp_worker/evidence/performance/reference.json`.
The stored reference hash is `9d63c195195daf6509fe75947ae08c08ea4baf773ab55e8d82887290834a8b80`.
The frozen query p99 threshold is 420.958375 ms; it is a regression tolerance, not a product SLA.
Candidate correctness, no-work-on-poll checks and timing have not run because no implementation exists.

### Implementation acceptance remains open

Rubric B is NOT RUN. A passing design/readiness gate does not erase the full baseline's four failures:
221 unique maintained legacy cases, 217 pass / 4 fail / zero errors or skips. The implementation must
rerun that inventory and all new exams at one pinned candidate. Installed wheel execution outside the
checkout, real service/native/subscription/S1/MCP seams and native containment remain candidate evidence.
The automated black-box crash cases do not claim exhaustive instruction-boundary or PID-reuse proof;
source review and builder regression evidence must verify the queued/START handshake, rollback and
creation-identity checks. Those mechanisms are explicit contract obligations, not silently waived tests.

The production builder cannot edit tests, fixtures or this review. Any discovered exam defect returns
to this reviewer for an independently recorded amendment, preserving the original failure evidence.


## Exam-owner amendment 1 — observe environment before fixture runtime mutation

Date: 2026-10-16. Independent owner: `/root/mcp_qa`. Root requested review of the builder's
first integrated run: 53 cases, 49 pass / 1 fail / 3 errors. The permission environment failure
reported only `__CF_USER_TEXT_ENCODING`. Original output is preserved without editing at
`acceptance/mcp_worker/evidence/environment-observation-amendment/original-builder-failure.log`.
The three other errors are not excused by this amendment and require their own disposition.

The owner independently reproduced the distinction: `/usr/bin/env` receives exactly the provided
names; the macOS framework Python runtime introduces the CoreFoundation name before the fixture
observes `os.environ`. Python also normalizes a supplied sentinel value, so merely exempting that name
would not preserve the inherited-environment invariant. The builder's proof is retained separately
from the independent positive and negative probe controls.

Disposition: **exam observation defect, repaired; no contract semantics change**. A protected native
C launcher records names before exec of the unchanged Python harness. The single environment test
compiles it with the validated Apple clang 21.0.0 and requires the boundary record to exist before
applying the original exact allowed-name set. Runtime additions are recorded separately. API keys,
provider overrides, connector values, arbitrary names and the CoreFoundation variable remain excluded
at the actual exec boundary. The negative control records deliberately injected ungranted names,
proving the predicate still detects such a service defect. No production path was edited.

Focused command and source identity are in `focused-checks.json`: the corrected environment case
and three fixture-integrity cases **4 pass / 0 fail / 0 error / 0 skip**, 1.501 seconds. Independent
native-probe controls passed with clean and deliberately ungranted environments; clang warnings are
errors. The production tree was under concurrent implementation, so these checks are evidence for
this exam repair, not complete candidate acceptance. The root will rerun the full protected suite.

D/A remain at zero defects: public behavior, source scope and acceptance intent are unchanged.
The new compiler prerequisite is documented and validated on the captured host. The commit containing
this amendment carries the required Exam-Owner trailer and refreshes SHA256SUMS; the original protected
revision remains available in `4c41a3f5b13185b70c3e5c097a093c8ae20fd1be` with manifest hash
`799ecb4950b96e513fa4f5ede6b0e0a638630038eb82a54c291a5432fa5ed3dd`. B remains a separate gate.
