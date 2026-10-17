<!-- PURPOSE: Independent frozen acceptance inventory for RT-FRESH-QUOTA-005. -->
# Fresh quota admission exam

Independent author/exam owner: `/root/quota_evaluation_qa`. Contract author:
`/root/mcp_learning_contract`. D=0 and A=0; implementation/B is pending. This package
is protected. Builders may inspect it and report disagreements, never edit it.
The freeze commits only the contract and this new QA package. No production change
or shared/live test activation is included.

The native and TUI fixtures are sanitized actual operator captures with provenance
in `fixtures/PROVENANCE.json`. Native primary is weekly (10080 minutes), secondary
is null, used is 36%, reset epoch is 1792653737. The TUI UUID is not a model/pool;
`3:22 AM on 22 Oct` means October 22 at 03:22 in its observed timezone. Captured old
parser output is defect evidence, never expected behavior.

| Contract requirement | Executable module(s), all under `acceptance.fresh_quota` |
|---|---|
| AC-FQ01 temporal/geometry completeness | `test_admission`, `test_collection`, `test_route_temporal` |
| AC-FQ02 native weekly/spent/malformed | `test_native`, `test_admission` |
| AC-FQ03 frozen resources/exact scope/route aliases | `test_admission`, `test_auth_scope`, `test_selection` |
| AC-FQ04 scorer/cache/final-resolution expiry | `test_route_temporal`, `test_selection` |
| AC-FQ05 main/auxiliary final boundary and concrete identity | `test_main_pty`, `test_auxiliary_dispatch`, `test_client_binding` |
| AC-FQ06 PTY start/reuse/send and nightly teacher | `test_main_pty`, `test_teacher`, `test_auth_scope` |
| AC-FQ07 bounded concurrent readers/no learner/cache isolation | `test_collection`, `test_native` |
| AC-FQ08 actual native and TUI status regression | `test_codex_status`, `test_native` |
| AC-FQ09 accumulated and installed-release verification | full runner and preserved commands below; candidate performance/learning parity required |
| AC-FQ10 exact staged source and activation | stage fixtures, before/dependency manifests, candidate operator hash/origin/reload evidence required |

`evidence/selectors.json` is the exact 116-method inventory (subtests add matrix
coverage). Prebuild results have 60 failure records and 69 error records, **not**
129 separate methods: unittest counts subtest outcomes independently. There are
zero skips. Errors are solely absent new APIs/owners or unchanged old signatures/
raw fields. These are intended prebuild failures, not a passing candidate result.
The complete report preserves each failure/error traceback. Candidate B requires
116 passing methods, zero failures/errors/skips, plus every accumulated lane.

Run from `/Users/velox/.cortados-builds/cortados-router-repo`:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/fresh_quota -t . -v
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python /Users/velox/.cortados-builds/fresh-quota-verification/run-full-suite.py
/Users/velox/.cortados-builds/target-test-env/venv/bin/python -B -m acceptance.fresh_quota.run_preserved_pty delivery /Users/velox/.cortados-builds/fresh-quota-verification/delivery-candidate.json
/Users/velox/.cortados-builds/target-test-env/venv/bin/python -B -m acceptance.fresh_quota.run_preserved_pty fd /Users/velox/.cortados-builds/fresh-quota-verification/fd-candidate.json
```

`CORTADOS_FRESH_QUOTA_STAGE` selects the copied activation tree, defaulting to
`/Users/velox/.cortados-builds/fresh-quota-activation`. This is a QA loader selector,
not a production policy override. StageCase loads complete copied main/PTY/dispatch
modules and the immutable provider plugin. It changes only literal deployment
paths in private fixture copies: RELEASE_SITE_PACKAGES, RELEASE_PYTHON, CONFIG_PATH,
and PROVIDER_PLUGIN_SOURCE. The teacher fixture additionally relocates its existing
_MCP_SITE_PACKAGES literal to the same inert fixture installation. No live source
is changed. The fixture installation's owner answers are explicit injected trusted
observations; real-policy tests independently test refusal and canonicalization.

`test_client_binding` executes exact source AST functions for the handoff seams,
including new same-file helper dependencies transitively, with inert constructor
imports. It does not claim that isolated handoff tests execute the full host module.
Full staged auxiliary dispatch, preserved delivery and maintained host integration
lanes provide that separate coverage. `client-return-inventory.json` enumerates
actual resolver/cache return arms for independent candidate source review. Exact
source/class/runtime/original-method proof is tested using the entire frozen CLI
provider dependency. Merely assigning a matching class name or preflight attribute
cannot pass; preflight never grants final dispatch permission.

Preserved shared delivery and FD bodies run unchanged against the staged Runner,
with an explicit quota-owner fixture and private state. Current baseline: 25/25
and 2/2 respectively, no failures/errors/skips. The two old shared frontier tests
are amended separately only in staged `tests/test_router.py`, as authorized by root:
D7/D9 boundaries with explicit former D5/D6 negative cases. See
`evidence/SHARED-EXAM-AMENDMENT.md`; no other test body changes. Shared activation
requires the original before hash again, and QA alone may activate this one exam
file. `prior-protected-hashes.json` freezes all 335 tracked prior acceptance files.

Root owns these mandatory B obligations: every standalone and seven maintained
host integration suites; every existing acceptance package; shared nightly 16;
staged shared router 30; preserved PTY 27; unchanged required-routing fixture with
an explicit quota seam; migrated learning-activation fixture using the fresh stage;
installed origin and Python 3.12 import checks; byte-identical learning owners and
clean_request AST; six raw performance workloads (uncached success, fresh cache,
expired refresh, unknown, concurrent same-source, zero-dispatch refusal). Candidate
reports must name revisions, hashes, command, counts and any blocked lane. Old
learning release remains immutable. Activation waits for completed 004 evaluation
and report publication and no active training invocation. No GPU/teacher calls
are made by these exams. Unrelated whole-CortadOS suites remain separately unverified.
