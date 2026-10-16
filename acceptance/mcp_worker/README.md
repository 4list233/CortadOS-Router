# Independent MCP worker acceptance

Owner: `/root/mcp_qa`, designated by Forest. These tests, fixtures and evidence are read-only to the
production builder. Report contradictions to the exam owner; do not alter an expectation to pass.
No production module is imported by the black-box tests. The official MCP 1.30.0 client launches the
public stdio entrypoint. A process double replays captured native output and adds explicitly synthetic
failure modes. Local MCP fixtures provide real SDK endpoints with observable forbidden operations.

## Exact environment and commands

The prepared environment is `/private/tmp/cortados-mcp-sdk-env/bin/python`, Python 3.14.6,
`mcp==1.30.0`. Its SDK dependencies include the HTTP fixture's uvicorn. Run from the repository root:

```sh
/private/tmp/cortados-mcp-sdk-env/bin/python -B -m unittest discover -s acceptance/mcp_worker -t . -v
/private/tmp/cortados-mcp-sdk-env/bin/python -B acceptance/mcp_worker/benchmark.py --output /private/tmp/mcp-worker-candidate-performance.json
```

The full legacy-suite commands/environment are in `docs/mcp-worker/QA-BASELINE.md`. A source-only
unittest command does not account for the exported integration suites. Installation/wheel startup
outside the checkout and real service/native calls must be measured after implementation; standalone
native captures are not substitutes for those integrated checks. Missing or skipped lanes are BLOCKED.
The packaged entrypoint must behave the same as the source entrypoint used by the fault scenarios.

Every case uses private temporary state/native home/workspaces and synthetic credentials. The test
PATH starts with the isolated interpreter directory so the native-double shebang can import the SDK.
The service must not reach real saved credentials or live CortadOS state during this exam. Only the
separate, explicitly authorized live seam run may use native subscription authentication.

## Coverage and limits

| Exam | Observable ground truth |
|---|---|
| protocol | Official MCP discovery, exact tool set, strict inputs, typed errors |
| codex_boundary | Three real model streams, version refusal, malformed/duplicate/missing final evidence |
| selection | Exact Unicode handoff, zero explicit S1 requests, exact brief digest, 6,000-character and 65,536-byte bounds |
| admission | Native account/quota/inventory wire, model pagination, stale refresh, managed-policy refusal |
| identity | Concurrent retries, conflict refusal, default/realpath canonicalization, invalid request rollback |
| recovery | Frontend disconnect, broker kill, descendant containment, invalid receipt, corrupt storage, stopped backup/restore |
| permissions | SDK stdio and authenticated HTTP grants, guessed tool/resource/prompt attacks, native isolation argv, secret/lifetime checks |
| results | Observed usage, immutable artifacts/results, unknown usage/events, bounded output failure |
| concurrency | Canonical overlap/symlink exclusion and disjoint capacity |
| cancellation | Separate process-session child, timeout release, late success, immutable terminal result |
| observability | Exact structured transition/refusal events, no polling duplicates/private payloads |
| fixtures | Protected native corpus hashes, all enabled output kinds, replay double selfcheck |
| benchmark | Correct retained results, no metadata/process work on polling, frozen reference-derived query tolerance |

Black-box crash cases exercise accepted-running recovery and missing/invalid termination evidence.
Source review and builder regression evidence must additionally show the durable queued/START handshake,
partial-transaction rollback, process creation identity before every signal, schema migration refusal,
and unique indexed lookups. Timing a process kill cannot prove every internal instruction boundary.
The exam does not pretend to prove a general malicious-daemon containment guarantee. Native permission
captures and actual integrated service probes remain required for sandbox claims.

## Fixture and measurement provenance

`fixtures/native-boundaries/PROVENANCE.json` records original and protected hashes. Raw private config
was never saved; policy wire retained full structure with string/dynamic-key redaction and source hashes.
Native metadata separately redacts account/email, home path and a private credit balance string while
retaining its type. The fixture's credit value is not quota. Process-double reset timestamps are
synthetically moved into the future; original recorded resets remain unchanged in the wire fixture.

`evidence/performance/reference.json` is a new-path reference, not a historical service baseline.
It measures indexed retrieval plus JSON decoding of four real-sized serialized job/result envelopes
at 1, 1,000 and 10,000 rows, and direct native-double process execution of the same handoffs.
The frozen public-query p99 tolerance is 420.958375 ms (5 times maximum native-process p99 plus maximum
reference-storage p99). This deliberately loose local regression check permits scheduler variation;
it is not a user-facing service-level promise. Candidate end-to-end overhead is reported separately.
Real provider latency is recorded separately in native capture metadata.

The synthetic operator profile uses these explicit values, not production defaults:

| Limit | Value and reason |
|---|---|
| max_concurrent_jobs | 2 jobs, enough to exercise disjoint overlap and reject a third; lower loses the concurrency case, higher consumes needless test processes. |
| max_timeout_s | 30 s, bounds abandoned fixture work; lower can interrupt containment cases, higher prolongs failed tests. |
| termination_grace_s | 1 s, allows native interrupt before forced fallback; lower races child cleanup, higher slows failure containment. |
| startup_timeout_s | 5 s, exceeds measured native-double p99 by more than 50 times; lower can flake under startup load, higher conceals stalled metadata. |
| max_handoff_bytes | 65,536 UTF-8 bytes, captured on all three enabled models; lower rejects the pinned boundary, higher lacks this profile's evidence. |
| max_output_bytes | 1,048,576 combined bytes, contains the real small fixtures and gives a bounded overflow case; lower clips legitimate fixture output, higher increases retained failed-output cost. |
| metadata_max_age_s | 1 s, makes stale-admission refresh testable; lower causes unnecessary refresh, higher delays the freshness case. |
| process_poll_ms | 20 ms, samples a persistent separate-session fixture child before escalation; lower adds process-table load, higher expands the observation gap. Candidate cancellation checks validate this setting. |

Test wait ceilings detect stalls and do not set runtime product policy. No benchmark or test purchases
credits, sends external messages, deploys configuration, or changes native login.
