<!-- PURPOSE: Protect regression ground truth for the quota-evaluation crash repair. -->
# Independent quota-evaluation exam

Owner: `/root/quota_evaluation_qa`, the designated independent MCP QA role.
Contract author: `/root/mcp_learning_contract`.
Production baseline: standalone `a5ea70e`; actual live-core snapshot SHA-256
`8454018479497287fe9ee3109493a5ffdc7f2e7fe14888e3ca63660107b26532`.
The builder may read and run this package, but may not edit it or older acceptance packages.

The 21 cases run seven consistency scenarios through three actual source bindings:
standalone legacy reader, standalone injected resource reader, and the frozen live core's
legacy reader. They assert returned route, burn and weight, calls per candidate, unknown
quota preservation, unchanged spent exclusion and fresh observation on a later calculation.
Synthetic observations do not establish actual provider quota freshness or model quality.

Run the frozen baseline from the standalone repository root:

```sh
PYTHONPATH=/Users/velox/cortados CORTADOS_QUOTA_CORE=/Users/velox/.cortados-builds/quota-evaluation-verification/live-before/plugins/cortados-router/core.py /Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/quota_evaluation -t . -v
```

For candidate verification, change only `CORTADOS_QUOTA_CORE` to
`/Users/velox/.cortados-builds/quota-evaluation-activation/plugins/cortados-router/core.py`.
The resolver source is imported by that exact path; the standalone source is explicitly
refused as a substitute. `PYTHONPATH` supplies the actual live file's existing shared
`context` imports. The fixture binds state and repository paths to temporary private
storage and replaces external observations before calling the resolver. No account,
provider, GPU, teacher, training, release or promotion operation runs in these exams.

Baseline: 21 cases, 12 pass, 6 assertion failures, 3 TypeError errors, zero skip.
Each source binding has two assertion failures for double reads/ranking changes and one
`float / NoneType` crash. Missing and zero observations, unpriced candidates and spent
exclusion already pass. The raw baseline is `evidence/prebuild.log`; this is prebuild
regression evidence, not a failed builder attempt.

During exam authoring, the draft unknown-window assertion was corrected from null to
the observed existing empty object. The initial live import also lacked its shared
`context` import path. Both fixture issues were resolved before the frozen baseline.
No production changes or existing protected-test changes were made.

Independent D/A review: zero unresolved defects for scoped dispatch under RT-QUOTA-EVALUATION-004 v1.
Root authorized the mechanical metadata corrections; behavior is unchanged. Initial review closed
metadata/authority, requirement mapping, source references, staged-versus-live scope, runnable commands
and diagram evidence gaps. The attempt ledger is empty; baseline red tests are not builder attempts.

B remains pending production candidate,
full accumulated suite accounting, performance comparison, copied-live evaluation and
release evidence. These 21 tests augment every existing maintained test; they cannot
replace the prior inventory. Root owns saved-checkpoint evaluation and existing promotion
gates; the deterministic test data cannot serve as promotion evidence.

Both Mermaid diagrams render with the existing CLI and installed Chrome configuration. The first
render attempt lacked the expected cached headless version; selecting the existing Puppeteer config
resolved the environment issue without changing diagram content. Raw logs are retained.

The performance driver retains 100 local timing samples for 2, 12 and 48 priced candidates and 12
unknown estimates. Imported core state is reused within each scenario; samples include mock boundary
overhead equally for baseline and candidate. Baseline source hashes and observations are recorded.

Original protected manifests verified before freeze: 167 MCP worker, 98 native-profile, 18 router
follow-up and 38 MCP-learning entries; all 321 matched. Strict fresh quota and the known Codex
status/reset parser defects are follow-up 005, not claims made by this exam.
