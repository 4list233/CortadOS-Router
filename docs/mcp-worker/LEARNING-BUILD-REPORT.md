<!-- PURPOSE: Record independent MCP learning/context verification, measurements and local activation.
     CONTRACT: RT-MCP-LEARNING-003; distinguishes tests, live observations and unproven quality.
     DEPENDS: Frozen contract/exams, e1fb2e4 source, pinned non-editable wheel and five staged adapters. -->
# MCP learning, context and nightly activation

Implemented and installed on October 17, 2026. Claude can request a context checklist before assigning
work and record its review after completion. The nightly pipeline now has an MCP source containing
the exact routing input, native outcome, available usage and attributed planner review. The existing
October 17 training run continued on historical CortadOS data; the integration activates on the next
nightly invocation. No new scheduler, teacher provider, training window or promotion policy was added.

## Source and review chain

Standalone checkout: `/Users/velox/.cortados-builds/cortados-router-repo`, branch `plan/subagent-mcp`.
Local scoped commits were authorized; nothing was pushed. The shared CortadOS checkout was not committed.

| Stage | Frozen identity / result |
|---|---|
| Inspection baseline | `6a64b77853ca990a5e641918098c55e0976858f3` |
| Contract and independent protected exams | `26773c597d505524f626d9be737e80f128b68278`; independent D=0/A=0 |
| Narrow superseded-diagnostic clarification and exam | `49ea2b608b8a00b9609863b5b4de90333351929b` |
| Production implementation tested and installed | `e1fb2e4ded95a298946cdcd9d26a15d35f07c42a`; 16 authorized paths |
| Activation adapter diff | SHA-256 `b7c01774037b25ff229547bd12e0d634b41fb83c5c783bd56f22b6fde657c19d`; five exact staged files |
| Wheel | SHA-256 `fb717b214580304b3468d4ee3ccab273887ba14cdf828c28792405829bef2289` |

The contract author and independent exam owner were separate from both builders. Root independently
reviewed the implementation and ran the accumulated suites. Source review caught explicit SQLite
closure, malformed native-worker containment and incomplete evaluation-cache identity; the builder
fixed these and added regressions before the final production commit. The installed 51 source/data
entries match the tested checkout and wheel. Import checks outside the checkout confirmed the pinned
owner under Python 3.14 and the existing MLX Python 3.12. Actual live common/student imports also
passed under Python 3.9 and 3.12 without loading model weights.

## User-visible behavior

- `context_requirements` returns missing context fields and questions for Claude to answer in its
  next ordinary tool call. It performs no model inference, quota admission or repository fetching.
  `ready` means structurally complete and within budget, not proof of semantic sufficiency.
- `task_evidence` supplies components, dependencies, constraints, uncertainties, verification and
  attributed reference excerpts. S1 receives the complete evidence even when a shorter routing brief
  is provided. The worker receives the same evidence. Both boundaries reject overflow without clipping.
- `job_feedback` appends an accepted/rejected/inconclusive planner assertion with check references.
  Exact-ID replay returns the original receipt. Corrections preserve earlier assertions. Native
  completion remains separate from correctness; the broker does not independently verify the review.
- Collection reads the jobs/feedback and route databases without changing them. Immutable request
  revisions and a replaceable observation projection let later reviews supersede eligibility without
  overwriting history. Retry groups and matching task fingerprints share a partition or are quarantined
  on conflict. Teacher annotations do not leak into the student's inference text.
- The selected tokenizer measures the complete MCP training example, including its answer. Oversized
  examples remain observable but are excluded with a diagnostic. Existing teacher questions, scarcity
  gates, 1,536-token training ceiling and promotion rules are retained.

Legacy requests that omit evidence remain compatible. The catalog now has seven tools. See
[USAGE.md](USAGE.md) for exact inputs and [LEARNING-CONTEXT.md](LEARNING-CONTEXT.md) for the pinned rules.

## Independent verification

**388 passed, 0 failures, 0 errors, 0 skips.** The complete maintained standalone Router inventory is
372 tests: the original 331, ten builder regressions and 31 new protected learning/activation cases.
Sixteen additional tests exercise the actual shared nightly supervisor/run code. The benchmark lane
is recorded separately and is not counted as a test. Both protected manifests verified all 205 entries.

This is not a claim that every unrelated suite in the shared CortadOS checkout passed. That tree
contains context-engine, research and other workstream suites outside this standalone contract; those
were not run. Its five modified training adapters were independently tested with enabled and disabled
paths, and its two maintained nightly suites were included. All seven standalone host integration
modules are accounted for, using the pinned pristine Hermes fixtures where required.

Exact full-inventory command, from the standalone checkout:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B \
  /Users/velox/.cortados-builds/mcp-learning-verification/run_full_suite.py \
  /Users/velox/.cortados-builds/mcp-learning-verification/full-suite-activated
```

The output directory must be new when repeating the command. The [runner](evidence/learning-context-20261017/run_full_suite.py)
and [report](evidence/learning-context-20261017/full-suite/report.json) record every command, interpreter,
environment, lane, elapsed time and count. Logs are adjacent. The report pins the production source
revision; subsequent report/documentation edits do not change the installed wheel.

Activation-specific commands:

```sh
CORTADOS_MCP_ACTIVATION_ROOT=/Users/velox/.cortados-builds/mcp-learning-activation \
  /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest \
  acceptance.mcp_learning.test_activation -v
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B \
  /Users/velox/.cortados-builds/mcp-learning-verification/prove_old_run.py \
  --candidate /Users/velox/.cortados-builds/mcp-learning-activation/train/evaluate.py \
  --output /Users/velox/.cortados-builds/mcp-learning-verification/old-run-proof-final.json
```

The activation builder additionally passed six protected pipeline cases against staged sources,
three disabled collector/teacher/SFT comparisons, twelve release-path refusal probes, four Python
syntax checks and `bash -n`. A supplemental runner initially selected the checkout owner and correctly
hit the import guard; fixing that runner's import order made all six cases pass without weakening
source guards or exams. Root's old-process proof independently retained byte-identical evaluation
inputs/outputs after atomic script replacement.

| Contract checks | Independent evidence |
|---|---|
| AC-L01–L05 | Protected context/feedback cases, originals, installed seven-tool/native/feedback check |
| AC-L06–L09 | Protected collection/pipeline cases; real source collection; late-feedback revision and partition check |
| AC-L10 | Raw baseline/candidate public-tool and collection measurements; larger-context experiment below |
| AC-L11 | Frozen exams/manifests, source scope/headers/size, full inventory, four QA-rendered diagrams |
| AC-L12 | Pinned wheel/import checks, exact five-file hashes, enabled/disabled exams, real read-only source smoke, actual old Bash inode and unchanged process identities |

Root B review: zero unresolved implementation defects against this contract. Increased context quality
and scorer acceleration remain unproven; neither was promoted by this implementation check.

## Measurements

Public stdio MCP calls used the same synthetic native/scorer fixture before and after. Each cell has
one first-call observation and 30 warm calls; these isolate service overhead, not real S1 inference.

| Path | Baseline warm median | Candidate warm median |
|---|---:|---:|
| Legacy route | 1.895 ms | 1.911 ms |
| Route with structured evidence | Not available | 2.159 ms |
| Context questions | Not available | 0.720 ms |
| Context ready | Not available | 0.743 ms |

Legacy first calls were 138.835/155.039 ms. Later candidate tools reused that initialized service;
their first-call values are not independent process cold-start measurements. Raw samples are in
[baseline](evidence/learning-context-20261017/path-baseline.json) and
[candidate](evidence/learning-context-20261017/path-candidate.json).

Complete collection medians across three passes were 0.592 ms for zero jobs, 0.992 ms for one,
8.325 ms for 100 and 830.203 ms for 10,000. First passes added N rows, both replays added zero,
and all projections contained N observations. Raw SQL reads are retained as a lower-bound comparison,
not an equivalent earlier implementation. No live database was expanded for this measurement.

The larger-context experiment measured two synthetic tasks at each of 2,048/4,096/8,192 actual total
input tokens with a fresh per-request KV cache. Baseline timings were 10.98/11.21 seconds,
16.71/29.17 seconds and 49.93/30.82 seconds respectively. The base model's 262,144-token metadata
is not evidence of reliable routing at that length. No labeled long-context accuracy result was produced.

The last-position vocabulary-projection experiment reduced two 2k timings to 10.22/9.71 seconds,
but maximum probability difference was 0.079883 against a declared 0.0001 tolerance. Answers and bands
matched on those two tasks; probability equivalence did not. **Rejected; production scorer unchanged.**
A CPU-only exact-score cache prototype had approximately 0.018 ms hit latency but no measured real
hit rate; it was not deployed and did not cache quota/admission decisions.

All GPU timings were contended by nightly training. The trainer remained the same process and
advanced during the experiment, but system swap grew from zero to about 2.1 GB. RSS/free-memory
guards did not fully bound unified GPU pressure. No further GPU experiment was run, and no claim of
zero impact or idle latency is made. [Raw captures, measured/unrun cells and limitations](evidence/learning-context-20261017/context-benchmark/REPORT.md)
remain available. The 6,000-character service bound and 1,536-token training window are unchanged.

## Installed and scheduled state

Release: `/Users/velox/.local/share/cortados-workers/releases/mcp-learning-v1-20261017`.
Claude user entry `cortados-workers` points to its `venv/bin/python` and reports Connected. Other MCP
entries and the operator configuration were preserved. The old broker had zero active jobs; it was
stopped gracefully before starting the new release. Existing accepted job bytes are unchanged;
jobs.db stays at schema version 1 with an additive append-only feedback table.

The installed-wheel check used the existing native ChatGPT login, requested `gpt-6-luna`, and observed
Codex CLI 0.163.0. Its reported model field was unavailable and remains null. The worker wrote the
exact synthetic marker, which root read independently. Feedback replay created one row. Read-only
collection then added one new revision, excluded the old revision as `superseded`, and retained the
train partition. Total check time: 6.717 seconds. [Saved check](evidence/learning-context-20261017/native-learning.json).
This verifies the MCP/native/learning chain, not a fresh Claude planner LLM turn. The prior actual
Claude Max → MCP → S1 → Codex check remains recorded in [INSTALLATION.md](INSTALLATION.md).
The Codex-launched Claude OAuth discrepancy was not changed. S1 scoring was not restarted during training.

Only `train/{dataset,label,build_sft,evaluate}.py` and `train/nightly.sh` changed in the shared runtime,
plus its Router handoff. The existing daily 01:30 launchd schedule remains loaded and enabled.
The active supervisor, Bash and MLX trainer retained their identities and absent MCP feature variables.
Actual Bash fd 255 still references the old script inode after atomic replacement. Its later
evaluation uses the preserved legacy branch. The next invocation exports the feature flag, exact
installed path and existing selected base before collection/labeling/SFT. It uses the existing MLX
Python for tokenizer availability. No live labels, current adapter, retained checkpoints or credentials
were edited by this task; the running trainer continues its normal writes.

Tonight's dataset contains 4,554 collected requests and 4,071 labeled unique requests: **3,171 are
historical CortadOS activity**, 700 are the existing source sheet and 200 are pre-existing synthetic
coverage rows. Weighted SFT has 18,922 train and 932 validation examples; these are not unique task
counts. The active run targets 1,500 iterations. Training completion and holdout promotion are pending
the existing nightly process; this report does not claim a newly improved model.

Rollback evidence is the exact [five-file reverse patch](evidence/learning-context-20261017/activation-rollback.patch)
and [before/after record](evidence/learning-context-20261017/activation-record.json). Check current hashes
before applying a rollback; preserve subsequent owner edits. The previous Claude entry is in the
[service activation record](evidence/learning-context-20261017/service-activation.json). Reverting a
registration must preserve active jobs and append-only evidence. Private full configuration and DB
backups remain under `/Users/velox/.cortados-builds/mcp-learning-verification/`; they are not published here.
