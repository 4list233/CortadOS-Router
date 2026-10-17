# Fresh quota and saved-checkpoint recovery

October 17, 2026. Forest authorized strict fresh-quota admission for both MCP and the legacy fleet,
local scoped commits, and independent MCP QA. No shared-checkout commit or remote push was made.

## Result and source identity

Production revision `660ae56` on `plan/subagent-mcp` contains the reviewed standalone implementation.
The full test run used exam pin `0d2de57d0cd74839b8185cf076e66d388a929e6d` plus the exact candidate
file hashes, subsequently committed unchanged. Independent overall D=0/A=0/B=0 signoff is `c3a6173`;
the QA-owned shared exam activation record is `0b78d39`.

The non-editable release is
`/Users/velox/.local/share/cortados-workers/releases/fresh-quota-v1-20261017`.
Wheel SHA-256: `5278262c19de76c6111a6b3f0f807d012a93e482c4cacd91eda48d01374e3893`.
All 75 release source/data inputs match the verified checkout. Dependencies retain the previous
release's pinned versions. The learning modules, schemas, native profile and package initializers
are byte-identical to the previous release; `core.clean_request` is source/AST identical.

The new quota owners extend the existing observation, pool, scarcity and resolver responsibilities.
They collect bounded read-only observations, bind them to configured authentication sources, and
construct immutable selection inputs. Existing pool mapping, pricing and scarcity arithmetic are
adopted unchanged. Legacy adapters call these installed owners. No duplicate quota rule is added to
the PTY or host dispatch paths. See [the contract](FRESH-QUOTA-005.md) and
[operator explanation](QUOTA-OPERATIONS.md).

## Verification

The full accumulated **Router/MCP package inventory passed 605 tests, with 0 failures, 0 errors and
0 skips**. This includes 130 independent fresh-quota exams, existing worker/context/learning exams,
host integrations, preserved PTY delivery/FD tests, the shared Router tests and migrated nightly
adapter tests. The benchmark lane is separately classified and contains six measured workloads.
Unrelated whole-CortadOS context/harness suites were not run; this is not a whole-CortadOS green claim.

Exact command:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B \
  /Users/velox/.cortados-builds/fresh-quota-verification/run-full-suite.py \
  /Users/velox/.cortados-builds/fresh-quota-verification/full-suite-final
```

The [independent B2 evidence](../../acceptance/fresh_quota/evidence/B2/signoff.json) retains exact
commands, source/test hashes, logs, origins and raw performance samples. The initial full run had one
cron-fixture failure: its synthetic health input had not been connected to the new quota owner.
Independent QA approved an isolated fixture correction with every original assertion unchanged.
The original failure is retained. Two historical shared frontier fixtures were amended only by the
exam owner under their existing D7/D9 behavior; negative boundary cases remain.

Synthetic native-protocol fixture, mocked S1; milliseconds, baseline → candidate:

| Workload | Samples | Median | p95 |
|---|---:|---:|---:|
| Uncached success | 8 | 295.702 → 322.552 | 479.735 → 485.211 |
| Fresh cached selection | 60 | 1.177 → 1.269 | 1.697 → 1.520 |
| Expired source refresh | 8 | 152.013 → 150.767 | 161.783 → 160.080 |
| Unknown source | 8 | 293.744 → 298.503 | 298.485 → 319.199 |
| Concurrent same source | 8 | 292.912 → 293.391 | 301.772 → 305.734 |
| Spent refusal | 8 | 291.857 → 295.419 | 304.313 → 306.545 |

Outputs and probe counts match; each benchmark used zero model turns. These are routing-overhead
measurements, not S1 inference or real provider-network timing. No speed improvement is claimed.

## Installed activation

Every one of 16 production destinations matched its frozen before hash immediately before atomic
replacement. Modes and private backups were preserved. The four training Python adapters were
installed before `nightly.sh`; all five installed release literals match. No trainer was active.
`train/current.json` and the existing nightly launchd plist remained byte-identical.

Destinations: `plugins/cortados-router/{core,execution_routing,__init__,scarcity,quota_policy}.py`,
`plugins/cortados-pty/{model_routing,runner}.py`, the auxiliary-routing patch, active Hermes
`agent/{auxiliary_model_routing,auxiliary_client}.py` and `hermes_cli/plugins.py`, plus
`train/{dataset,label,build_sft,evaluate}.py` and `train/nightly.sh`.
QA separately activated `tests/test_router.py`; the Router handoff was updated by root.

Only the interpreter string in Claude's `cortados-workers` entry changed. Arguments, environment,
other entries and credentials were preserved. `claude mcp get cortados-workers` reports Connected
at the new release. The idle durable broker was gracefully replaced after identity and active-job
checks. Its prior two jobs and one feedback row remained byte-identical; private SQLite backups
and integrity checks were recorded.

Installed verification passed the seven-tool catalog, a real fresh native quota observation, routing,
and one actual `gpt-6-luna` subscription worker with network off and no MCP grants. The operator
independently read the exact scratch marker and recorded accepted feedback with identical replay.
Job `bcaeb64a-2cb1-423a-aadd-ba24242a108a` succeeded; final state was three jobs, two feedback rows,
zero active jobs, integrity OK. The installed owner refused an unknown provider and unproven or
mismatched scopes with zero transport starts. A private operator-script response-envelope typo was
corrected before any delegate/model call; that initial record is retained.

The actual Hermes Python 3.12.13 interpreter successfully imports the pinned quota/config owners.
The supported gateway restart recovered all 12 enabled Discord faces. RC28 was already explicitly
disabled. The script exited 0 after its wait, but its default 13-face expectation was **not met**;
this is a failed operational assertion, separate from the verified 12/12 enabled roster. Both held
cron units were released and loaded, and gateway PID 222 is supervised by launchd. The roster and
restart script were not changed. Final evidence is retained with the activation records.

## Evaluation and operating limits

The saved October 17 checkpoint completed evaluation with no retraining. Candidate and incumbent
used the same frozen 610 held-out requests. Accuracy was 64.1% versus 64.9%; under-routing was 13.8%
versus 6.6%; outward recall was 92.9% versus 88.1%. Accuracy and under-routing gates failed, so the
candidate was **not promoted**. The report was published to its original run directory and the
unfinished-run marker cleared through the existing report semantics. The October 16 model remains
served; the 01:30 America/Toronto schedule and all promotion thresholds are unchanged. Launchd still
shows the old scheduled run's exit code 1; the separate saved-checkpoint recovery exited 0. No new
scheduled invocation has been claimed successful.

The strict check is source-bound and at most 30 seconds old at routing/dispatch boundaries; native
CLI internal model calls remain controlled by that provider. Legacy native OAuth, non-strict Codex
invocations and unproven local endpoints refuse. On the live all-lane check at 14:24 EDT, Codex was
available; Claude/Antigravity metadata was unavailable, Cursor process containment was unproven,
and local endpoint identity was unproven. Those routes refused. Diagnostic status can still display
an older reading, so it is not an admission receipt. The nightly schedule remains enabled, but its
teacher must also pass fresh quota before spending subscription usage.

Public evidence: [activation and metrics](evidence/fresh-quota-20261017/manifest.json).
Private raw verification and rollback backups remain under
`/Users/velox/.cortados-builds/fresh-quota-verification/`; evaluator evidence remains under
`/Users/velox/.cortados-builds/quota-evaluation-verification/`. Forward/reverse patches are evidence,
not authorization to overwrite later changes. Check current hashes before any rollback.
