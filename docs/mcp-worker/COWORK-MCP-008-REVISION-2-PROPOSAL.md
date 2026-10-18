# Proposed bounded core revision after second valid failure

Proposal only, for Forest's decision. No contract, source or protected exam change is made. Retain both failed build attempts, all raw host/probe results and the original reviewed design. No third same-design retry or auth integration is authorized by this document.

## Transfer response lifetime: one owner, all response paths

Keep `remote_http` as the sole frontend transfer-lifetime owner and `file_transfer` as the sole broker descriptor owner. Do not introduce a wrapper service, new timeout, grace interval or response retry.

At transfer entry capture one monotonic nonrenewing total deadline and one idle deadline. Only acknowledged nonempty byte progress renews idle. Every awaited request receive, response send, IPC step and bounded cleanup uses the remaining minimum deadline. Success, Range416 and all refusal responses pass through the same guarded emission path. An exception handler never allocates a new idle interval.

Track response state conservatively as `not_attempted`, `headers_attempted`, `headers_complete`, `finished`. Set headers_attempted BEFORE awaiting send(response.start): cancellation/failure may leave partially visible headers even if that await does not return. Any error or timeout in headers_attempted or later aborts the response; it never emits another response.start. Header state is shared by JSON success/error and binary GET paths, not inferred from status200 only. Preserve successful statuses, binary bytes, Content-Disposition and sealed artifact semantics.

Explicit observable clarification needed: a strict expired deadline leaves no time to guarantee408 before headers. Proposed rule: before any header attempt, a timeout refusal may emit408 only while positive remaining transfer budget exists; if no budget remains, abort without headers. After any header attempt, abort. Never obtain new time to make error delivery reliable. This is a narrowly reviewed change to the unconditional former phrase “408 before headers,” not permission to extend the lifetime or fabricate an ACK.

On exhausted frontend budget, close the indeterminate CoreClient endpoint rather than issue an unbounded close RPC. Broker endpoint teardown/timers remain responsible for descriptor retirement; never cancel/retry accepted model jobs as a side effect. Shared-client users receive the existing closed-connection refusal and must reconnect explicitly. Define and test an in-flight accepted PUT whose receipt was lost: task_get reports its committed seal, no retry of consumed bytes.

Required independent revision exams: timely PUT201/GET200/Range416/error controls; stalled receive and send at response.start and body for each response family; no second start after attempted headers; total/idle exact deadline and exhausted-budget cases; no progress renewal on empty chunks; close-path exhaustion; a concurrent MCP/transfer client sees endpoint retirement without wrong sequence/response or job cancellation. These amplify existing assertions, not replace them.

## Capacity-only admission latch: exact operation table

Separate two reasons in the existing CoreRuntime owner, without a second policy owner: capacity-only failure (missed sample/unmeasurable/over-limit) versus fatal integrity/audit/storage failure. Do not reinterpret `store.admission_closed` as capacity-only. No automatic reopening, resampling reset or new config/default. A capacity failure still blocks new work and invokes the existing Broker.stop/Watchdog containment path. A failed ownership/projection/audit check escalates to the existing fatal refusal even on the limited observation path.

Proposed smallest observation surface retaining the frozen ability to inspect an accepted attempt and its task after proven termination:

| Operation | Capacity-only latch | Fatal integrity/audit/storage latch | Disabled owner |
|---|---|---|---|
| attach/bind of exact trusted principal/config | Allow endpoint for limited operations below; no new authority | Refuse ordinary tool/stream authority; cleanup still mandatory | Refuse |
| task_get | Allow ordinary owned-task projection; no transfer capability and no mutation/repair | Refuse storage_error | Refuse permission_refused |
| job_status / job_result | Allow only after normal owner/task/job/binding/publication validation; expose actual durable native state/reason | Refuse storage_error | Refuse permission_refused |
| job_cancel | Allow existing owned job only, normal idempotent stop intent and existing containment; never create/restart | Refuse public call; internal shutdown/containment must still attempt cleanup | Refuse public call; existing internal lifetime rules unchanged |
| job_feedback | Refuse capacity_exhausted; append-only feedback may be submitted after explicit qualified restart | Refuse storage_error | Refuse permission_refused |
| task_delete | Refuse capacity_exhausted; no speculative cleanup when usage sample is untrusted | Refuse storage_error | Refuse permission_refused |
| context_requirements | Refuse capacity_exhausted; not required for existing-attempt inspection | Refuse storage_error | Refuse permission_refused |
| task_create / route / delegate | Refuse capacity_exhausted, including replay invocation through these methods; inspect accepted identity with task_get/job methods | Refuse storage_error | Refuse permission_refused |
| file_upload_ticket / file_download_ticket | Refuse capacity_exhausted, including ticket replay/renewal | Refuse storage_error | Refuse permission_refused |
| open_put / open_get / put_chunk / finish_put / read_chunk | Refuse capacity_exhausted; retire the connection's affected existing lease before any further byte effect; no new stream | Refuse storage_error and retire owned descriptors | Refuse permission_refused and revoke/retire |
| close / endpoint teardown / internal lease timer | Cleanup-only, idempotent existing owned lease; no new authority or resource promises | Cleanup still runs without treating corrupt metadata as permission to delete unknown files | Cleanup still runs |
| trusted disable / runtime shutdown | Preserve existing control/lifetime ownership; no admission reopening | Preserve fail-stop/containment and fixed fatal signaling | Same-state disable remains no-op |

This is a proposal, not a claim all these dispositions were previously enumerated. It deliberately avoids broad “reads allowed.” Root/Forest should pin the exact table and fixed codes before dependent implementation. Normal inspection still validates principal, enabled state, task ownership, execution discriminator, binding agreement, output publication and response schema. It must not enter a routing/scorer/quota/model path, mint tickets, repair rows or allocate a worker.

Capacity diagnostics must remain observable separately from storage corruption. The deterministic current evidence shows capacity latch=true with JobStore admission_closed=false and fatal_signalled=false, a valid termination receipt and durable cancelled/capacity_exhausted; rejecting its job_result as storage_error loses the frozen outcome. The unrelated historical host test's exact scheduling cause is not proven: its one observational rerun passed and remains separate. No latency/jitter allowance is proposed.

Required independent revision exams: controlled missed sample with actual fake worker and proven stop; task_get/job_status/job_result allowed after capacity-only latch; job_cancel remains idempotent and never relaunches; every denied operation above has zero new admission/row/resource effect; cleanup close works; fatal integrity/audit and disabled owner override the capacity-only exception; projection corruption refuses; explicit restart requires existing recovery and no automatic relaunch. No provider/native production calls in QA.
