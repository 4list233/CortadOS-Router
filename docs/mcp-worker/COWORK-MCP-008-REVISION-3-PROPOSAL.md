# Cowork core revision 3 proposal

Status: proposed for Forest; no contract amendment, new source attempt or public activation authorized.

The second implementation under approved revision 2 still permits an integrity failure to leave admission open. All 899 maintained tests, including the protected 114 core tests, pass across all 28 host lanes, with zero failures, errors or skips. Independent QA found an additional actual-runtime counterexample: one negative test fails and two controls pass. Root reproduced both results. Independent B is complete with three failed criteria across all 11 reviewed rows; all required measurements are complete. A green test inventory cannot override the counterexample.

## What failed

The canonical task reader examines nested fields before validating the complete record. Replacing a private synthetic task's stored policy with `{}` causes a `KeyError`. That error escapes the reader and the capacity monitor. The monitor exits, all fatal/admission latches remain false, no safe failure signal appears, and a subsequent unrelated task creation succeeds through the real IPC service.

The prior correction assumed selected exception classes covered canonical-read failure and that the monitor would continue running. The counterexample disproves both assumptions. Valid-record and malformed-type controls distinguish this from an implementation that always refuses work. No real user data, native model, provider or credential was used.

Evidence: [independent source review](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/independent/SOURCE-B-REVIEW.md), SHA b2981cfbe5437a941ffa076ce68ce51de3d34543b215e60d28f3d09a8ccda6ad. [Actual probe](/Users/velox/.cortados-builds/cowork-mcp-verification/core-candidate-attempt-4/independent/probe-persisted-read.json). Frozen candidate patch SHA3981d2beff8e7f14d5253f942a90afcd7177e4704379a17fcd981fa76dfa2df4.

## Proposed invariant

1. The existing canonical record reader validates the complete closed record before field-dependent ownership and discriminator checks. Every canonical decoding, validation or semantic-integrity failure reaches the existing fatal runtime owner. Ordinary unknown or foreign lookups retain their prescribed not-found result.
2. The existing runtime explicitly observes its monitor's terminal state. Expected shutdown cancellation is separate from unexpected exit or exception. Unexpected monitoring loss immediately latches fatal admission refusal and the existing safe failure signal. It cannot leave a live accepting runtime. The same runtime authorization boundary also checks terminal monitor state, including its existing post-wait checks before effects, so admission cannot slip through before an asynchronously scheduled completion callback. Expected shutdown is identified by owned shutdown state.
3. The existing runtime remains the single owner of once-only failure signaling. The existing Broker remains responsible for accepted worker lifetime and containment. No new watcher process, automatic monitor restart, retry, admission reopening, timeout grace or public-error-code blanket is added.

This preserves the approved single HTTP/IPC deadline and capacity-only inspection/cancellation table. The change closes the integrity boundary around canonical reads and monitor lifetime; it does not change the allowed capacity-only operations or silently cancel jobs because a frontend disconnects.

## Work after approval

Reconcile the invariant into the existing 008 contract and runtime/reader/monitor owners, with independent D review. Preserve all current 114 tests and all earlier failures. The independent exam owner adds the persisted missing-field negative, valid/type-error controls, unexpected monitor exit, admission before a queued completion callback, and explicit shutdown-cancellation cases, then commits the exams and repeats A. A bounded Codex subscription build follows only after those gates, with the full accumulated suite, performance and independent B repeated on the frozen result.

The group remains53f0ea39-a61e-4307-9c2c-466c1b4b0bd1. Four cumulative failed source candidates remain in the ledger: two under v4 and two under approved revision2. The project's contracts/CONVENTIONS.md requires a second valid failure to return for design review; changing model, branch or exception list cannot reset it.

OAuth/operator document preparation can continue independently. Auth source still waits for accepted core; public installation, real GitHub/Cowork qualification, native-tool confinement and seven-day retention remain separate gates.
