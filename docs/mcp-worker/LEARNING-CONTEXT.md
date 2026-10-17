<!-- PURPOSE: Specify MCP task evidence, planner feedback and nightly learning integration.
     CONTRACT: RT-MCP-LEARNING-003; independently reviewed D/A with pinned exams.
     DEPENDS: RT-MCP-001, RT-MCP-NATIVE-002; existing nightly training and promotion owners.
     CEILING: No new scorer policy, runtime context ceiling, scheduler, or model promotion. -->
# MCP learning and context

```yaml
contract_id: RT-MCP-LEARNING-003
contract_version: 1
owner: Forest
status: approved
implements: [RT-MCP-001]
depends_on: [RT-MCP-NATIVE-002]
blocks: []
capability_tier: integration-stateful
attempt_ledger: []
```

Forest's current instruction is to wire MCP tasks and worker outcomes into training, give S1 richer
context, benchmark larger context and faster routing, and keep nightly training running. This
contract specifies that instruction; it does not append a ruling to the global decisions log.
The independent exam owner is `/root/mcp_qa_followup`; root owns integration and activation.
The recorded samples and protected exams are frozen with this contract; see acceptance/mcp_learning/README.md for the independent D/A review.
Split rationale if this contract exceeds 500 lines: it keeps the revision/partition and activation chain auditable in one review; production is split into bounded evidence, source and learning owners. No production file inherits this document size.

## 1. Existing owners and inspected evidence

Inspection baseline: standalone `6a64b77853ca990a5e641918098c55e0976858f3`.
Shared `train/` is a separate, dirty live deployment; per-file hashes are required for activation.
Searches: `rg --files cortados_router integrations s1 tests`; `rg -n 'context|handoff|feedback|split|score'`
against the files in this table. No existing planner-feedback or MCP training-source owner was found.

| Responsibility and current evidence | Disposition and ceiling |
|---|---|
| `integrations/mcp/schemas.py:115` validates closed arguments; `:161` hashes canonical arguments | Extend optional fields and new tools; retain old normalized bytes when fields are absent. |
| `integrations/mcp/server.py:19` publishes tool schemas; `:53` lists five tools | Extend to seven tools. QA alone amends the historical exact-five assertion. |
| `cortados_router/route_task.py:20,68,77` assembles handoff, logs task-only evidence, constructs scorer text | Extend using one new `task_evidence.py` owner; keep legacy text byte-for-byte. |
| `cortados_router/core.py:168,408,680` owns scorer calls, selection, route writing | Adopt unchanged. No growth to this oversized file; structured provenance uses existing `context` JSON column. |
| `integrations/mcp/job_store.py:45,102,139` owns jobs.db and accepted arguments | Extend additive feedback table through this sole writer; never rewrite accepted arguments or native result. |
| `integrations/mcp/broker.py:133` serializes dispatch and replay | Extend two branches; no second broker or lifecycle. |
| `s1/common.py:21` fixes hash-based split | Move the unchanged split_of primitive into learning_data and re-export it from common; existing callers retain their name and outputs. |
| `s1/dataset.py:18` appends requests from sheet and one router.db | Extend through new installed `learning_data.py`; this remains the nightly collection invocation. |
| `s1/label.py:81` emits teacher payload with request/unit_kind only | Extend with attributed outcome annotation and label provenance; keep the twelve answer questions. |
| `s1/build_sft.py:33,43` and `s1/evaluate.py:143,148` join rows and split validation by row ID | Extend callers of a shared eligibility/partition owner; keep existing label distribution, training and promotion math. |
| `s1/student.py:50,79` formats request and runs twelve autoregressive passes | Adopt unchanged. One prefill plus eleven cache continuations is not twelve full-prompt evaluations. |
| `s1/serve.py:31,43`; `route_task.py:17`; `s1/nightly.sh:162` | Keep 6,000-character service bound and 1,536-token training setting; larger context is an offline experiment. |
| shared `train/nightly.sh:70–72` invokes dataset, label, build_sft | Adopt cadence, PID owner, scarcity, resume, retention and promotion; next-run adapters leave the active run on its legacy branch. |

Recorded existing public shapes: `docs/mcp-worker/evidence/a27afd0/claude/{request.json,persisted-job.json,report.json}`,
`acceptance/mcp_worker/fixtures/s1/{request.json,response.json}`, and original protected native corpus.
Before dispatch QA MUST pin privacy-preserving schema samples of live requests, labels, route rows,
and legacy jobs rows in its fixture directory. (AC-L11)
No production request text, credentials, or weights may enter the public repository. (AC-L11)

## 2. Scope and compatibility

[FIXED] The builder MUST edit only the following standalone paths. (AC-L11)
Existing directories `cortados_router/`, `integrations/mcp/`, `s1/`, `tests/`, `docs/mcp-worker/` exist.
New files are marked `(new)`; no new production directory is required.

1. `cortados_router/task_evidence.py` (new): strict evidence schema, context report and exact projections.
2. `cortados_router/learning_data.py` (new): collection/publication, partition eligibility and teacher annotation interfaces; delegates source reads.
3. `cortados_router/route_task.py`: call projection owner and log exact scoring provenance.
4. `integrations/mcp/schemas.py`: transport validation and new error/tool names.
5. `integrations/mcp/server.py`: public schemas and descriptions.
6. `integrations/mcp/broker.py`: context report and feedback dispatch.
7. `integrations/mcp/job_store.py`: additive feedback persistence and replay.
8. `s1/dataset.py`: invoke MCP collection after existing sources.
9. `s1/label.py`: use eligible rows and annotation helper.
10. `s1/build_sft.py`: use eligible rows and shared validation partition.
11. `s1/evaluate.py`: use eligible rows, shared partition and content-sensitive evaluation cache identity.
12. `tests/test_mcp_learning.py` (new): implementation regressions, not protected exams.
13. `docs/mcp-worker/INDEX.md`: map these owners and ACs in the existing index.
14. `docs/mcp-worker/USAGE.md`: planner examples and feedback limitations.
15. `s1/common.py`: re-export split_of from its installed learning_data owner after moving that unchanged primitive.
16. `cortados_router/learning_source.py` (new): read-only SQLite snapshots and typed source-to-observation projection, called only through learning_data collection.

[FIXED] All other standalone paths MUST remain builder-read-only. (AC-L11)
[FIXED] The builder MUST NOT edit shared `train/`, live state, installed releases, launchd, global
DECISIONS, existing protected fixtures, or this contract. (AC-L11)
[FIXED] New source files MUST carry PURPOSE/CONTRACT/DEPENDS/CEILING headers. (AC-L11)
[FIXED] Each new source file MUST stay below 500 physical lines; splitting requires a scope amendment. (AC-L11)
The threshold preserves the existing source guidance; packing statements is not an escape.

[FIXED] Legacy route/delegate calls MUST retain their exact normalization and scorer/handoff bytes. (AC-L01)
[FIXED] Existing route/result/job envelopes MUST retain their exact keys and types. (AC-L01)
[FIXED] Existing permissions, subscription admission, native profile and lifecycle rules MUST remain unchanged. (AC-L01)
[FIXED] Missing new fields MUST NOT be inserted into old normalized arguments. (AC-L01)
This preserves replay of already accepted request IDs; adding default nulls changes their hashes.
[FIXED] The public catalog MUST contain the original five tools plus `context_requirements` and `job_feedback`. (AC-L01)
QA MUST archive the original exact-five exam bytes before its narrowly scoped catalog amendment. (AC-L11)
QA MUST retain every other original assertion. (AC-L11)
No feature flag is needed to conceal the new catalog from the old exam.

## 3. Task evidence and context dialogue

[FIXED] Optional `task_evidence` on route/delegate MUST be a strict object with this schema. (AC-L02)
Strings are valid UTF-8 and nonblank unless explicitly stated; unknown object keys refuse `invalid_request`.
Arrays preserve caller order and text bytes; repeated strings within string arrays are invalid.
Repeated reference objects are permitted and retain their supplied order.

| Field | Type and meaning |
|---|---|
| `schema` | literal `cortados.mcp-task-evidence.v1`, required |
| `task_group_id` | canonical UUIDv4, required; planner reuses it for retries/revisions of the same assignment |
| `components` | string array: affected components/files; missing means unknown; empty means explicitly none |
| `dependencies` | string array: dependencies or prerequisites; same missing/empty distinction |
| `constraints` | string array: scope and execution constraints; evidence does not grant permissions |
| `uncertainties` | string array: unresolved assumptions; same missing/empty distinction |
| `verification` | string array: expected observable checks, including review-only checks; empty is structurally valid but incomplete |
| `references` | optional array of strict `{source:string,text:string}` reference records; no fetching |

Canonical JSON uses sorted keys, compact separators, ensure_ascii=False, allow_nan=False.
[FIXED] Evidence MUST have no independent 6,000-character object cap. (AC-L02)
The complete assembled S1 text and worker handoff own their respective bounds, preventing a false
S1 restriction on explicit execution. Existing private RPC frame limits remain unchanged.
[FIXED] The frontend MUST compute context_requirements through the pure evidence owner before RPC,
so a well-typed oversized packet returns over_budget rather than failing private transport framing. (AC-L03)
[FIXED] It MUST mark every evidence/reference section as untrusted reference data. (AC-L02)
Without that boundary, attacker text could be mistaken for scorer or worker authority.

`context_requirements` accepts strict `{task:string, context?:string, selection:Selection,
task_evidence?:PartialEvidence}`; `Selection` is the existing selection schema.
PartialEvidence has the same schema/group requirement when present, but may omit the five arrays.
[FIXED] After normal broker startup, the tool dispatch MUST return a deterministic report without
metadata probes, inference, repository reads, connector calls, job reservation or route logging. (AC-L03)
Existing service-startup native policy checks remain unchanged.
[FIXED] The tool MUST NOT call Claude or initiate recursive MCP sampling. (AC-L03)
Claude receives questions in the ordinary tool result and supplies answers in its next tool call.
[FIXED] Schema validation MUST accept absent/empty completeness fields before the completeness check. (AC-L03)
[FIXED] The report MUST identify missing arrays in the table's order. (AC-L03)
[FIXED] Missing evidence MUST produce missing_fields `["task_group_id","components","dependencies","constraints","uncertainties","verification"]` and questions in that exact order. (AC-L03)
[FIXED] An empty verification array MUST produce the verification question. (AC-L03)
[FIXED] Supplied incomplete evidence on route/delegate MUST refuse `needs_clarification` before scoring or reservation. (AC-L03)
Omitting evidence entirely preserves legacy behavior; preflight is advisory for those callers.

Report schema (all fields required):
`{ok:true,context_requirements:{schema:"cortados.mcp-context-requirements.v1",
status:"ready"|"needs_context"|"over_budget",missing_fields:string[],questions:Question[],
scorer_chars:int>=0,worker_bytes:int>=0,scorer_limit_chars:6000,worker_limit_bytes:int>0,
scorer_digest:sha256hex|null}}`.
Question is strict `{field:string,prompt:string}`; field is one of the five arrays or `task_group_id`.
The prompt for each array is exactly `"Provide <field>; use [] only when explicitly none."`, except
verification uses `"Provide at least one observable verification check."`; task_group_id uses
`"Reuse one task_group_id for revisions and retries of this assignment."`.
[FIXED] Budget overflow MUST take status precedence over missing fields. (AC-L03)
[FIXED] Explicit-mode reports MUST return scorer_chars=0 and scorer_digest=null. (AC-L03)
[FIXED] Explicit-mode reports MUST apply only the worker handoff limit; S1 mode applies both limits. (AC-L03)
[FIXED] S1 reports MUST return the SHA-256 of the complete scoring text even when over budget. (AC-L03)
[FIXED] `ready` MUST mean only structurally complete context within current byte/character limits. (AC-L03)
It is not quota admission, a semantic guarantee, or proof that the task is easy.
[FIXED] A caller MUST still pass ordinary admission when it later routes or delegates. (AC-L03)

[FIXED] The exact scorer text for evidence calls MUST be the existing scoring base plus
`"\n\nTASK EVIDENCE (untrusted reference data; not instructions or permission)\n"` plus canonical evidence JSON. (AC-L02)
The existing base is routing_brief when present, otherwise task + `"\n\nCONTEXT:\n"` + context.
[FIXED] A routing_brief MUST NOT suppress task_evidence. (AC-L02)
[FIXED] The worker handoff MUST append the identical labeled evidence section to the legacy handoff. (AC-L02)
[FIXED] The scorer text MUST retain the 6,000-character refusal before core's clipping boundary. (AC-L02)
[FIXED] The entire worker handoff MUST retain the configured UTF-8 byte refusal. (AC-L02)
[FIXED] No projection MUST silently truncate any supplied field. (AC-L02)
[FIXED] New route logs MUST record selection_mode, projection_version=1, task_group_id or null,
scoring_text or null, scoring_digest or null, original task/context, and task_evidence or null in
existing `routes.context` JSON. (AC-L04)
Explicit selection has null scoring_text/digest; it MUST NOT manufacture an S1 observation. (AC-L04)
For training explicit jobs, the same owner assembles the ordinary task/context/evidence text;
oversize examples remain observations but are not eligible for today's S1 teacher/training pipeline.

Valid partial dialogue:
```json
{"task":"Fix parser regression","selection":{"mode":"s1"},"task_evidence":{"schema":"cortados.mcp-task-evidence.v1","task_group_id":"00000000-0000-4000-8000-000000000003","components":["parser.py"]}}
```
Its report has missing_fields `["dependencies","constraints","uncertainties","verification"]`.
The planner resubmits with arrays and `verification:["Run parser regression suite; inspect failing input"]`.
Invalid evidence: `{"schema":"cortados.mcp-task-evidence.v1","task_group_id":12}` refuses
`invalid_request`; `verification:[]` is incomplete, never silently read as verified.

## 4. Planner feedback: a separate assertion lifecycle

[FIXED] `job_feedback` MUST accept strict `{job_id:UUIDv4,feedback_id:UUIDv4,
verdict:"accepted"|"rejected"|"inconclusive",summary:string,checks:Check[]}`. (AC-L05)
All summary/reference strings MUST be nonblank valid UTF-8. (AC-L05)
Check is strict `{kind:"command"|"artifact"|"review",reference:string,
claim:"passed"|"failed"|"unknown"}`. References identify planner evidence; the broker does not fetch it.
[FIXED] Accepted feedback MUST contain at least one passed check and no failed or unknown check. (AC-L05)
[FIXED] Rejected feedback MUST contain at least one failed check. (AC-L05)
[FIXED] Inconclusive feedback MAY have no checks. (AC-L05)
[FIXED] Canonical feedback JSON MUST fit the configured max_handoff_bytes. (AC-L05)
The existing private RPC frame budget motivates this bound; too small rejects useful review evidence,
too large permits unbounded canonical records. The limit measures complete serialized feedback bytes.
[FIXED] Feedback MUST require an existing terminal job owned by config.client_id. (AC-L05)
[FIXED] Nonterminal jobs MUST refuse `job_not_terminal`; unknown or foreign jobs MUST refuse `job_not_found`. (AC-L05)
[FIXED] Neither feedback nor context text MUST change job state, worker result, route or permissions. (AC-L05)

[FIXED] job_feedback validation MUST run before the existing generic job_ validation branch. (AC-L05)
[FIXED] The closed error/event vocabulary MUST add only job_not_terminal for feedback. (AC-L05)
[FIXED] JobStore MUST create an additive `job_feedback` table within jobs.db. (AC-L05)
Columns: `seq INTEGER PRIMARY KEY AUTOINCREMENT`, `client_id TEXT NOT NULL`, `job_id TEXT NOT NULL`,
`feedback_id TEXT NOT NULL`, `argument_hash TEXT NOT NULL`, `created_at_ms INTEGER NOT NULL`,
`payload TEXT NOT NULL`, UNIQUE(client_id,feedback_id), FK(job_id)->jobs(job_id).
[FIXED] JobStore MUST keep user_version=1 because old clients can still read unchanged jobs rows. (AC-L05)
[FIXED] JobStore MUST enforce terminal/ownership checks and insertion in one BEGIN IMMEDIATE transaction. (AC-L05)
[FIXED] UPDATE and DELETE of feedback rows MUST be refused by table triggers. (AC-L05)
[FIXED] Exact ID replay MUST return its original receipt without another row. (AC-L05)
[FIXED] Reusing feedback_id with any different input MUST refuse `idempotency_conflict`. (AC-L05)
A correction is a new feedback_id; contradictory earlier assertions remain evidence.
[FIXED] A committed feedback response MUST be
`{ok:true,feedback:{job_id,feedback_id,seq,created_at_ms,verdict,verification_status:"planner_asserted"}}`. (AC-L05)
All IDs are UUIDv4; seq is integer >=1; created_at_ms is nonnegative UTC epoch milliseconds.
[FIXED] Failed feedback transactions MUST return `storage_error` with no success receipt. (AC-L05)
[FIXED] Existing emit_event MUST emit one refusal for each failed request with no private summary text. (AC-L05)

Valid feedback: `{"job_id":"00000000-0000-4000-8000-000000000004","feedback_id":"00000000-0000-4000-8000-000000000005","verdict":"accepted","summary":"Reviewed parser change","checks":[{"kind":"command","reference":"test-log.sha256:abc","claim":"passed"}]}`.
Its receipt has verification_status `planner_asserted`, even if native state was `succeeded`.
Invalid: accepted feedback with `checks:[]`; rejected feedback with passed-only checks.
[FIXED] Native `succeeded` MUST mean native completion only in all learning projections. (AC-L06)
[FIXED] Missing feedback MUST project to `planner_verdict:null`, never accepted or rejected. (AC-L06)
[FIXED] Planner assertions MUST NOT be described as independently verified ground truth. (AC-L06)
[FIXED] Runtime failure, timeout or cancellation MUST NOT be converted into an answer on any S1 question. (AC-L06)

## 5. Read-only collection and training ownership

[FIXED] `learning_data.py` MUST expose `collect_mcp(state_dir, data_dir)` for dataset's existing phase. (AC-L06)
Both arguments are absolute filesystem paths (str or pathlib.Path); data_dir is an existing private directory.
[FIXED] It MUST return `{status:"ok"|"degraded",added:int>=0,observations:int>=0,diagnostics:Diagnostic[]}`. (AC-L06/07)
Diagnostic is `{component:"mcp_learning",code:string,source:string|null,id:string|null}`.
The code vocabulary is mcp_source_unavailable, mcp_collection_busy, mcp_source_invalid,
scoring_digest_mismatch, scoring_context_too_large or split_conflict; source is an absolute DB path or null.
No row text belongs in diagnostics; storage publication errors return degraded with mcp_source_invalid.
[FIXED] It MUST return added=0 for a failure before request publication. (AC-L07)
[FIXED] Its learning_source reader MUST open `state_dir/jobs.db` and `state_dir/router/router.db` with SQLite mode=ro using explicit read transactions. (AC-L06)
[FIXED] It MUST NOT set immutable=1, because committed WAL rows belong to the live snapshot. (AC-L06)
[FIXED] It MUST NOT instantiate JobStore, broker, native CLI or core's database writer. (AC-L06)
[FIXED] It MUST never crawl job artifact paths, cwd, connector data or native account files. (AC-L06)
The source is only already accepted task arguments, route observations, terminal summaries and feedback.
[FIXED] Native state mapping MUST be succeeded→completed; failed/cancelled/timed_out/interrupted→the same value; queued/running/cancelling→pending. (AC-L06)
[FIXED] It MUST read a consistent jobs/feedback snapshot from one jobs.db transaction. (AC-L06)
Separate route transactions are not atomic with jobs; job arguments are authoritative when they join.
[FIXED] It MUST join jobs.route_id to routes.turn_id and avoid importing that route twice. (AC-L06)
[FIXED] It MUST retain unjoined route rows in observations with eligibility=false until a later scan. (AC-L06)
An advice-only route is useful diagnostic evidence but does not create a second teacher sample.
[FIXED] Legacy accepted jobs MUST be importable without new evidence or feedback fields. (AC-L06)
[FIXED] It MUST capture every job state, including active/unknown outcomes, in observations. (AC-L06)
[FIXED] It MUST NOT include native final answer content in the S1 request. (AC-L06)

[FIXED] `data_dir/mcp-observations.jsonl` MUST be a deterministic replaceable Projection owned by
collect_mcp, sorted by job_id then feedback sequence, with unjoined routes sorted after jobs. (AC-L06)
Each job observation is strict `{schema:"cortados.mcp-learning.v1",job_id,route_id,request_id,client_id,
state,reason,task,context,task_evidence,selection_mode,scoring_text,scoring_digest,learning_text,learning_id,observation_digest,
task_group_id,worker:WorkerObservation,usage:Usage|null,elapsed_ms:int>=0|null,feedback:FeedbackRecord[],native_outcome:"completed"|"failed"|"cancelled"|"timed_out"|"interrupted"|"pending",
planner_verdict:"accepted"|"rejected"|"inconclusive"|null,eligibility:bool,exclusion_reason:string|null}`.
FeedbackRecord contains the exact payload plus seq and created_at_ms; IDs/text retain source values.
WorkerObservation is `{provider:"codex-cli",requested_model:string,reported_model:string|null,cli_version:string}`.
Usage is the existing native result.usage schema, retaining nullable token counters without inferred money.
Terminal elapsed_ms is the existing nonnegative job elapsed_ms; pending elapsed_ms is null.
The requested model comes from accepted route.chosen; reported model/usage come only from native result.
Historical missing cli_version resolves through the existing native_profile.record_version owner.
[FIXED] Outcome annotation MUST retain worker, usage, terminal elapsed_ms and reason from that observation. (AC-L08)
Nullable task_evidence/scoring fields retain nulls; all job states are eligible if learning text is valid.
The closed observation exclusion vocabulary is null, scoring_digest_mismatch or scoring_context_too_large.
Source rows with unknown schema/state/type fail the snapshot rather than becoming another exclusion category.
Unjoined route record shape is `{schema:"cortados.mcp-route-observation.v1",route_id,context,
eligibility:false,exclusion_reason:"no_accepted_job"}`; context is its stored JSON value or string.
[FIXED] `planner_verdict` MUST be the verdict of greatest feedback seq, or null when absent. (AC-L06)
[FIXED] Full feedback history MUST remain in the projection even after a correction. (AC-L06)
[FIXED] Source parse/integrity failure MUST publish no replacement projection. (AC-L07)
[FIXED] Dataset MUST publish request additions by atomic replacement preserving every old byte as a prefix. (AC-L07)
[FIXED] It MUST fsync the complete requests file before replacing the observation projection under the same collection lock. (AC-L07)
A crash between the two replacements leaves an unselected extra revision; the next collection reuses that ID.
A crash cannot select a projection revision whose request was only partly appended.
[FIXED] The writer MUST use a same-directory temporary file, flush/fsync, atomic replace and directory fsync. (AC-L07)
[FIXED] A nonblocking exclusive flock on `data_dir/mcp-collection.lock` MUST serialize collection. (AC-L07)
[FIXED] Lock contention MUST report `mcp_collection_busy` and leave existing data unchanged. (AC-L07)
[FIXED] Repeated collection with unchanged sources MUST produce byte-identical projection and no duplicate requests. (AC-L07)
[FIXED] Collection MUST rescan source rows each invocation rather than use a creation-time cursor. (AC-L07)
[FIXED] Collection MUST issue no more than three full-table data SELECTs (jobs, feedback, routes), excluding bounded schema/integrity introspection. (AC-L10)
Three scans cover the three source relations; fewer misses evidence and more adds repeated full-scan work.
A creation cursor misses later terminal states and planner feedback for old jobs. The once-nightly scan
has no arbitrary row cap; its work is bounded by the snapshot row count and measured in AC-L10.

[FIXED] The optional source default MUST be `~/.local/state/cortados-workers`; the CLI MAY override
it with `--mcp-state-dir ABS_PATH` for isolated tests/deployments. (AC-L06)
[FIXED] Missing source MUST report `mcp_source_unavailable` without creating source files. (AC-L07)
[FIXED] Source failure MUST leave the existing sheet/Hermes collection and nightly schedule usable. (AC-L07)
[FIXED] A source failure MUST retain previous observation provenance without claiming a fresh snapshot. (AC-L07)
Previously committed observations remain usable historical evidence; no current revision is invented.
A printed structured diagnostic identifies component, code and source path, never task text.

[FIXED] Eligible accepted jobs MUST append one request row per distinct observation_digest with ID
`mcp.<job_id>.<observation_digest>` through dataset's existing requests writer. (AC-L08)
[FIXED] observation_digest MUST be SHA-256 of canonical JSON `{job_id,learning_text,native_outcome,reason,worker,usage,elapsed_ms,feedback}`. (AC-L08)
The full ordered feedback records participate; terminal elapsed_ms is immutable and pending elapsed_ms is null.
Queued/running/cancelling states all project to pending; a transient progress update cannot create a learning revision.
[FIXED] The current projection MUST identify the one latest learning_id for each job. (AC-L08)
[FIXED] A later outcome or feedback revision MUST create a new learning_id without changing old rows. (AC-L08)
[FIXED] Their `request` MUST be exact learning_text, without clean_request or text-only dedup. (AC-L08)
[FIXED] Existing request rows MUST never be edited or deleted. (AC-L08)
New MCP request fields are `{id,source:"mcp-worker",unit_kind:"interactive",request,split,
group_key,task_fingerprint,scoring_digest,job_id,observation_digest,outcome_annotation}`; split is
existing train/holdout; scoring_digest may be null for explicit. outcome_annotation is frozen with that revision.
[FIXED] S1 jobs' learning_text MUST equal stored scoring_text with its SHA-256 verified. (AC-L08)
[FIXED] Legacy S1 jobs MUST reconstruct that exact text using the immutable accepted arguments. (AC-L08)
[FIXED] Explicit jobs' learning_text MUST use ordinary task/context/evidence projection, not fabricated S1 answers. (AC-L08)
[FIXED] Invalid digest or >6,000-character learning text MUST be excluded with an observable reason. (AC-L08)

## 6. Grouping, teacher evidence and honest labels

[FIXED] `learning_data.eligible_rows(requests, observations)` MUST own train/valid/holdout eligibility for all consumers. (AC-L09)
Both arguments are lists of parsed row objects, not paths; callers bind one complete JSONL snapshot.
The result is `{rows:RequestRow[],partitions:dict[id,"train"|"valid"|"holdout"],excluded:Exclusion[]}`.
Exclusion is `{id:string,reason:"superseded"|"split_conflict"|"scoring_digest_mismatch"|"scoring_context_too_large"}`.
[FIXED] rows and excluded MUST be sorted by ID; partitions MUST contain exactly the returned row IDs. (AC-L09)
[FIXED] Input objects MUST remain unmodified. (AC-L09)
[FIXED] `learning_data.split_of(row_id)` MUST be the existing unchanged split_of primitive moved from s1/common.py. (AC-L09)
[FIXED] s1/common.py MUST re-export that name without another implementation. (AC-L09)
The live legacy branch keeps its existing deployed common.py; all enabled MCP grouping uses the installed owner.
[FIXED] It MUST select only the current projection's learning_id for each MCP job. (AC-L09)
[FIXED] Superseded request revisions and their teacher labels MUST be excluded from all three partitions. (AC-L09)
Their bytes remain historical evidence; one job cannot acquire extra training weight by sending feedback.
[FIXED] A structured MCP row's group_key MUST be `mcp-group:<client_id>:<task_group_id>`. (AC-L09)
[FIXED] Legacy MCP group_key MUST be `mcp-task:<sha256(clean_request(task) or task)>`. (AC-L09)
[FIXED] task_fingerprint MUST be SHA-256 of clean_request(task) or task for every MCP row. (AC-L09)
[FIXED] The collector MUST use existing split_of(group_key) for train versus holdout. (AC-L09)
[FIXED] Validation membership MUST use existing SHA1("v"+group_key)%10==0 among train rows. (AC-L09)
This preserves existing 15% holdout/10% validation ratios without letting attempts choose different sets.
[FIXED] Legacy non-MCP rows MUST retain their current split and row-ID validation assignment. (AC-L09)
[FIXED] Eligibility MUST connect rows sharing group_key or task_fingerprint, including fingerprints
of cleaned legacy request text. (AC-L09)
[FIXED] A connected component assigned more than one partition MUST be excluded from all three
partitions with `split_conflict`, without rewriting historical rows. (AC-L09)
[FIXED] The same eligible component MUST never enter training and calibration/holdout. (AC-L09)
Different paraphrases without declared task_group_id cannot be inferred to be the same task; planner
documentation MUST require the same group ID on retries and revisions. (AC-L09)

[FIXED] Teacher payload MUST retain the existing exact request/unit_kind and add optional
`outcome_annotation` only for MCP rows. (AC-L08)
Annotation is `{native_outcome,worker,usage,elapsed_ms,reason,planner_verdict,verification_status:"planner_asserted"|"unreviewed",
summary:string|null,checks:Check[],feedback_ids:UUIDv4[],observation_digest:sha256hex}`.
[FIXED] summary/checks MUST come from the greatest-seq assertion; feedback_ids MUST list every assertion in seq order. (AC-L08)
[FIXED] Unknown feedback MUST produce null summary, empty checks/IDs and unreviewed status. (AC-L08)
[FIXED] Teacher instructions MUST state that annotations are fallible historical evidence, that
native completion is not correctness, and that answers must still use the existing twelve anchors. (AC-L08)
[FIXED] Outcome annotations MUST NOT be copied into student training, calibration, holdout or runtime prompts. (AC-L08)
They are unavailable before execution; including them would leak the answer into inference inputs.
[FIXED] Label rows for MCP samples MUST record observation_digest and feedback_ids from the immutable request revision used by that call. (AC-L08)
[FIXED] Existing completed teacher sample IDs MUST retain existing resume behavior within their revision. (AC-L08)
[FIXED] Late feedback MUST produce new teacher work through its new learning_id. (AC-L08)
[FIXED] Each label/build/evaluate process MUST bind one complete projection snapshot for its invocation. (AC-L08)
[FIXED] A new collection MUST NOT run concurrently with nightly labeling/build/evaluation. (AC-L08)
[FIXED] An unlabeled latest revision MUST remain unlabeled rather than fall back to a superseded label. (AC-L08)
This preserves teacher provenance and avoids silently reusing labels produced before material outcome evidence.
[FIXED] Training MUST use only valid twelve-answer teacher rows, never planner verdicts as numeric labels. (AC-L08)
[FIXED] Before SFT publication each MCP example MUST be tokenized with the selected base tokenizer and the exact student.messages chat template including its answer. (AC-L08)
[FIXED] An MCP example exceeding the unchanged 1,536-token training limit MUST be excluded with training_context_too_large and its measured token count. (AC-L08)
[FIXED] Missing tokenizer evidence MUST exclude MCP SFT rows with training_tokenizer_unavailable while retaining old non-MCP behavior. (AC-L08)
[FIXED] Tokenization MUST use the existing MLX training interpreter and selected CORTADOS_S1_BASE. (AC-L08)
[FIXED] build_sft MUST load that interpreter's installed AutoTokenizer with local_files_only=True and trust_remote_code=False. (AC-L08)
[FIXED] Token measurement MUST NOT load model weights. (AC-L08)
[FIXED] The measurement MUST include BOS/EOS behavior from the same chat template used by training. (AC-L08)
Character count is not a substitute for this token measurement; long new evidence must not silently disappear during training truncation.
[FIXED] Evaluation cache identity MUST include exact request digests, group partitions, teacher-label
content, scorer source hash, base/tokenizer/config hashes, adapter-weight content hashes and existing fail-up/outward settings. (AC-L09)
A cache based only on row IDs cannot prove a richer-context comparison.

## 7. Persistence, failures and resource evidence

| Aspect | Fixed behavior and proof |
|---|---|
| Identity | Existing request identity; new feedback(client_id,feedback_id); one active learning revision per accepted job; AC-L01/05/08. |
| Representation | Canonical task/feedback bytes; derived exact scorer text; unknown remains null; AC-L02/06. |
| Store classes | jobs/feedback Canonical (JobStore); route log Evidence (core); observations Projection; requests/labels existing training Evidence; reports View. |
| Mutation | Accepted args/native results immutable; feedback append-only; correction new ID; observations rebuilt; AC-L05/07. |
| Read/query | One read snapshot per DB, no per-job DB reopening; rows/bytes/time measured on 0/1/100/10,000 synthetic jobs; AC-L10. |
| Recovery | Feedback crash before commit yields no row; crash after commit replays receipt; projection crash leaves old/new complete file; AC-L05/07. |
| Corruption/containment | Source malformed row prevents new snapshot; prior provenance retained; foreground worker unaffected; AC-L07. |

[FIXED] Any eligibility exclusion MUST emit one structured diagnostic with row ID and reason per collection. (AC-L07)
[FIXED] Unknown or corrupt source state MUST NOT be silently replaced by successful outcome data. (AC-L06/07)
[FIXED] New numeric tuning values MUST state units, reason and both too-small/too-large effects beside definitions. (AC-L10)

## 8. Offline experiment and activation boundaries

The larger-context/faster-inference experiment is a separate root-owned report, not builder scope.
The first 2k paired candidate changed probabilities by 0.07988 and was rejected; raw evidence remains recorded.
[FIXED] Later 4k/8k capacity cells MAY run baseline-only under the same guards and MUST NOT be reported as candidate comparisons. (AC-L10)
A sequential activation subpackage is owned by the delegated activation builder. Its only staged paths,
relative to `/Users/velox/.cortados-builds/mcp-learning-activation`, are train/dataset.py, train/label.py,
train/build_sft.py, train/evaluate.py and train/nightly.sh. Their train/ parent MUST exist before dispatch. (AC-L12)
The verified scratch train/ parent and activation-inputs.json exist; copied common.py, student.py and evaluate dependencies are read-only. Staged files never execute against live data.
No live file edit is builder-authorized; root's operator application follows the exact tested diff.
[FIXED] It MUST compare 2k/4k/8k total tokenizer-measured prompt tokens with the same pinned local adapter. (AC-L10)
[FIXED] It MUST report any truncated, failed, unavailable or unrun cell as such. (AC-L10)
[FIXED] It MUST record raw paired timing, exact answers, maximum probability difference, resolved bands,
peak/resident memory, hardware, runtime, adapter hash, input hashes, warm/cold state and sample counts. (AC-L10)
[FIXED] It MUST keep fresh per-request KV caches. (AC-L10)
[FIXED] It MUST NOT restore the previously leaking cross-request prefix cache. (AC-L10)
[FIXED] It MUST leave production student/service limits and nightly max_seq_length unchanged. (AC-L10)
The 262,144-token base-model window is capacity metadata, not routing-quality validation; today's
1,536-token training setting is not evidence of reliable 8k routing.
[FIXED] A benchmark while nightly is active MUST use one nice+10 isolated process with a 180-second global deadline. (AC-L10)
[FIXED] It MUST check at least 25% system memory free before loading and between reference/candidate pairs. (AC-L10)
[FIXED] It MUST stop when that guard fails or its own RSS exceeds 6 GiB. (AC-L10)
[FIXED] It MUST leave the trainer and scorer service processes untouched. (AC-L10)
The bounded probe is not a contention-free speed claim; concurrent trainer iteration timings MUST be recorded. (AC-L10)
180 seconds limits disruption; a smaller window loses cells and a larger one competes longer with nightly.
25% free on the 32 GiB host leaves 8 GiB headroom; less risks paging and more can prevent measurement.
6 GiB RSS bounds a 4-bit 4B probe with context; less can reject valid prefill, more competes with training.
Priority +10 reduces CPU contention; it does not claim GPU isolation.
[FIXED] The only experimental candidate MUST postpone vocabulary projection until the final hidden-state position while preserving all twelve causal answer passes. (AC-L10)
[FIXED] Exact answers and resolved bands MUST match; probability differences MUST be reported without relabeling nonzero differences as exact equality. (AC-L10)
[FIXED] Larger budgets, altered answer conditioning, or a faster implementation MUST NOT auto-promote. (AC-L10)
A timing-only experiment cannot establish long-context quality; promotion still belongs to the existing gate.

[FIXED] Root MUST freeze hashes of live train/{dataset,label,build_sft,evaluate}.py and nightly.sh before activation. (AC-L12)
[FIXED] Root MUST record the current nightly process identity and demonstrate that its inherited
`CORTADOS_MCP_LEARNING_V1` is absent before activation. (AC-L12)
[FIXED] The separately delegated activation builder MUST prepare minimal call-site adapters to the installed learning_data owner in an isolated
copy of those five files with private synthetic data. (AC-L12)
[FIXED] Each Python adapter MUST preserve the exact legacy branch unless the process environment
has `CORTADOS_MCP_LEARNING_V1=1`. (AC-L12)
The fixed release root is `/Users/velox/.local/share/cortados-workers/releases/mcp-learning-v1-20261017`.
Its site-packages suffix is `/venv/lib/python3.14/site-packages`; wheel hash and Git SHA pin content without a self-referential commit-named path.
[FIXED] Each adapter MUST compare CORTADOS_MCP_LEARNING_SITE_PACKAGES against that exact absolute site-packages path before importing. (AC-L12)
[FIXED] Missing, different or non-directory values MUST refuse the enabled integration. (AC-L12)
[FIXED] The enabled branch MUST import learning_data from that validated directory. (AC-L12)
Repeating this deployment constant in four adapters is configuration repetition, not a second business-rule implementation.
[FIXED] QA's relocated scratch execution MAY replace only that exact release-path literal in private adapter copies. (AC-L12)
[FIXED] QA MUST record that single relocation and verify no other staged bytes changed for the enabled-path exam. (AC-L12)
[FIXED] Root's external activation record MUST pin that exact release path, its wheel hash and the emitted nightly export value before operator application. (AC-L12)
No additional live activation-record file or runtime policy reader is introduced.
[FIXED] Root MUST NOT copy a second learning_data implementation into train/. (AC-L12)
[FIXED] The installed package MUST be a newly pinned non-editable release verified outside its checkout. (AC-L12)
[FIXED] The activation builder MUST prepare exports for these two variables only at the beginning of the next-run nightly
script, before the supervised re-exec, for atomic replacement of nightly.sh. (AC-L12)
[FIXED] The new script MUST export its existing BASE value as CORTADOS_S1_BASE before dataset/label/build_sft. (AC-L12)
[FIXED] Only the enabled next-run branch MUST invoke build_sft with existing "$PY"; the legacy branch retains python3. (AC-L12)
[FIXED] Root MUST apply only the independently tested activation builder's exact patches as operator. (AC-L12)
[FIXED] The ongoing Bash process MUST keep executing its already-open old script inode with its unchanged environment. (AC-L12)
The adapters are release switches, not a substitute for enabled-path verification: the scratch next-run
exam MUST exercise collection, teacher, SFT and evaluation with the variables enabled. (AC-L12)
[FIXED] A synthetic old process that starts before replacement MUST later execute the modified eval
adapter with no flag and observe byte-identical legacy inputs/outputs. (AC-L12)
[FIXED] Root MUST recheck frozen hashes immediately before each activation edit. (AC-L12)
[FIXED] An intervening owner edit MUST stop that activation with a reported conflict. (AC-L12)
[FIXED] Activation MUST leave nightly schedule, resume/retention/promotion behavior, current.json,
labels, retained checkpoints, credentials and active jobs unchanged. (AC-L12)
[FIXED] Activation MUST NOT start another collector, teacher or trainer against live data. (AC-L12)
[FIXED] The first collection smoke MUST use read-only real MCP sources and an isolated destination. (AC-L12)
[FIXED] Root MUST retain before/after hashes and an exact rollback patch for its five live files. (AC-L12)
Rollback removes only those adapters/exports and restores the previous pinned MCP registration;
append-only feedback/job/data evidence is retained. No live reset/stash/checkout is authorized here.
[FIXED] Changing MCP registration MUST preserve accepted jobs in the existing broker until terminal. (AC-L12)
[FIXED] A broker configuration/version conflict MUST defer the registration change rather than stop an active worker. (AC-L12)

## 9. Acceptance and handback

QA owns new directory `acceptance/mcp_learning/` under existing `acceptance/`, including __init__.py,
README.md, SHA256SUMS, fixtures/, evidence/, support.py, pipeline_support.py, test_context.py,
test_feedback.py, test_learning.py, test_pipeline.py and test_activation.py.
QA owns the narrow original catalog-test amendment and its historical-byte evidence.
Builder reads these files only. The exam commit MUST precede dispatch. (AC-L11)

From the standalone checkout, define the verified interpreter once:
`PY=/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python`.
`EXAM` denotes `"$PY" -B -m unittest discover -s acceptance/mcp_learning -t . -v`.
`CONTEXT`, `FEEDBACK`, `LEARNING`, `PIPELINE`, and `ACTIVATION` denote respectively
`"$PY" -B -m unittest acceptance.mcp_learning.test_context -v`,
`"$PY" -B -m unittest acceptance.mcp_learning.test_feedback -v`,
`"$PY" -B -m unittest acceptance.mcp_learning.test_learning -v`,
`"$PY" -B -m unittest acceptance.mcp_learning.test_pipeline -v`, and
`CORTADOS_MCP_ACTIVATION_ROOT=/Users/velox/.cortados-builds/mcp-learning-activation "$PY" -B -m unittest acceptance.mcp_learning.test_activation -v`.
Before dispatch QA MUST replace table selectors with actual independently committed test names if its
module organization differs. The commands resolve to the protected exam package; red prebuild and staged-activation BLOCKED results are retained.

| AC | Required ground truth | Independent command |
|---|---|---|
| AC-L01 | Seven-tool catalog; byte-identical legacy request replay and exact old result/route keys; original protections pass | CONTEXT; original acceptance/mcp_worker suite |
| AC-L02 | Captured scorer text, native stdin and persisted args contain identical ordered evidence; Unicode boundary and unknown fields refuse before exec | CONTEXT |
| AC-L03 | Partial/ready/over-budget context reports; zero inference/native/DB writes during preflight; incomplete delegate has no job | CONTEXT |
| AC-L04 | Route row exact scorer digest/text matches transmitted text; explicit has null score; legacy request shape unchanged | CONTEXT |
| AC-L05 | SQL rows/receipts prove replay, conflict, terminal ownership, immutable feedback, restart and injected transaction failure | FEEDBACK |
| AC-L06 | Read-only DB audit and projection bytes preserve legacy/active/terminal/late-feedback observations, nulls and no joined duplicate | LEARNING |
| AC-L07 | Double collection identical; late feedback appears; crash/corrupt/lock/missing cases retain complete old output and diagnostic; nightly sources remain usable | LEARNING |
| AC-L08 | Teacher captured payload includes exact context and attributed outcome; SFT/eval request excludes outcome; no fabricated labels; append rows unique | LEARNING; PIPELINE |
| AC-L09 | Retry/context-revision/legacy-duplicate fixtures share partition or quarantine; teacher/SFT/eval membership disjoint; content changes invalidate evaluation cache | LEARNING; PIPELINE |
| AC-L10 | Raw baseline/candidate ingestion and route timings; isolated larger-context experiment report includes measured/unrun cells and no production promotion | LEARNING plus root benchmark report |
| AC-L11 | Exact diff scope, header/size checks, all fixture hashes, recorded samples, diagram renders and complete accumulated suite accounting | EXAM; `shasum -a 256 -c acceptance/mcp_learning/SHA256SUMS`; root full inventory |
| AC-L12 | Frozen live hashes, scratch adapter trace, installed-wheel import, real read-only smoke, old-run/new-run adapter proof and unchanged active process identities | ACTIVATION plus root activation record |

[FIXED] Handback MUST include tested revision/diff, exact commands, pass/fail/error/skip counts,
per-AC artifact paths, source hashes, raw performance samples and deviations. (AC-L11)
[FIXED] Missing exams, GPU window or live activation opportunity MUST be reported BLOCKED, never passing. (AC-L11/12)
[FIXED] Complete acceptance MUST account for every maintained suite, including original 331 baseline
and added exams, with no silently omitted integration lane. (AC-L11)
Independent D/A review reports zero unresolved defects in acceptance/mcp_learning/README.md. Root independently runs B against each production candidate.

## 10. Structure and open gates

`learning-context-examples.json` contains complete deterministic valid/invalid examples for context reports,
feedback, observations, request revisions, teacher annotations, eligibility, collection and unjoined routes.
QA MUST verify and hash these examples before D/A signoff. (AC-L11)

The adjacent `learning-context-flow.mmd`, `learning-context-sequence.mmd`, and
`learning-context-state.mmd` and `learning-context-er.mmd` describe data flow, transaction order,
feedback lifecycle and additive schema.
[FIXED] QA MUST render each through Mermaid CLI before D/A signoff. (AC-L11)

| Gate | Blocks | Status |
|---|---|---|
| Recorded live training/source schema samples and synthetic projections | D/A | Recorded and type-preserving redacted; independently verified |
| Independent exam commit, narrow catalog amendment, exact runnable selectors | Dispatch | Frozen with this contract in the independent Exam-Owner commit |
| Mermaid renders, numeric baseline query/route measurements | D/A/B respectively | Four renders passed; candidate query/route measurements remain B evidence |
| Active nightly remains running | Activation and GPU experiment | Next-run adapters and guarded probe only; no stop or restart |
| Larger context or faster scorer production promotion | Outside this package | Existing promotion gate; no change authorized by experiment result alone |
