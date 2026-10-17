<!-- PURPOSE: Complete the existing nightly run after a successful recovery evaluation. -->
# RT-QUOTA-EVALUATION-004 — operator completion addendum

Status: approved by independent QA `/root/quota_evaluation_qa` (2026-10-17); no builder or evaluator change.
Owner: Forest. Operator author: `/root`. Exam owner: `/root/quota_evaluation_qa`.
Authority: Forest requested recovery of the saved checkpoint, promotion only through existing gates,
and continued nightly training. This addendum implements the existing nightly completion marker.

The pinned 004 evaluation invocation, frozen inputs, inference settings and promotion expression
remain unchanged. This adds only a post-evaluation operator publication step.

Evidence: `/Users/velox/cortados/train/nightly.sh` functions `newest_unfinished_run` and its `pending`
calculation treat a run with checkpoints but without its own `report.json` as unfinished. Leaving the
successful report only in the private verification directory would cause the next nightly invocation
to resume that completed segment instead of marking this recovery complete.

[FIXED] After the actual recovery process exits zero, its newly generated report passes provenance
checks, and all protected input/checkpoint hashes still match, publish identical report bytes to
`/Users/velox/cortados/train/runs/2026-10-17-0130/report.json`. This completion marker is written whether
`promote` is true or false. If evaluation failed, the report is absent/stale, input hashes differ, or
the original run already has a different report, stop publication and report the conflict.

The original report must still be absent immediately before publication, as recorded in
`/Users/velox/.cortados-builds/quota-evaluation-verification/nightly-completion-before.json`.
Write a temporary file in the destination directory, flush it, then publish atomically without
replacing an existing file (same-filesystem exclusive hard-link publication is acceptable). Record
both report hashes and the process exit status. Remove only the operator-created temporary file.
A concurrent identical report may be recognized as already completed after comparing every byte;
a different report must never be overwritten.

[FIXED] Completion is not promotion. The live `train/current.json` remains unchanged unless the
fresh evaluator report says `promote:true` under 004's existing gates. Conditional pointer publication
and service verification retain their existing 004 procedure. The published report path may point
to the original run's identical report; the private evaluation report remains preserved as evidence.

This addendum does not edit training sources, data, labels, adapters, inference thresholds, scheduler,
or `last-train-count`. The latter is not a completion marker under the current nightly cadence.
No model call or retraining is triggered by publication.

Acceptance evidence: (1) source inspection of the existing nightly marker predicates; (2) exit-zero
current invocation and fresh report, with unchanged protected hashes; (3) identical SHA-256 of private
and original-run reports; (4) recomputation of the existing pending predicate returns false for the
October 17 run; (5) model pointer changes only if existing promotion gates pass. A successful code
suite alone is insufficient for this state publication.

Independent review: D=0, A=0 for this bounded operator completion step. Frozen nightly source
confirms the report-existence predicates and always-nightly cadence. Exclusive publication prevents
replacement races. Runtime acceptance evidence above remains required before claiming completion.
