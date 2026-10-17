<!-- PURPOSE: Record verified training fallback activation and its operating limits.
     CONTRACT: RT-TRAINING-TEACHER-007 revision 2, independently accepted cumulative attempt 3.
     DEPENDS: B3 signoff, exact candidate wheel, complete scoped suite and live label receipts. -->
# Codex training-label fallback: enabled and verified

On October 17, 2026, the user-approved revision passed independent D0/A0/B0 review and was activated
on the Studio. A bounded live run produced **12 new final-training labels** through the existing
Codex subscription. All **785 tests passed**, with zero failures, errors or skips. The original
11,660 labels, all 2,663 evaluation-label rows, overrides, evaluation code, promotion gates and
current model pointer are unchanged. No model promotion or full training run was performed here.

## Implemented behavior

Claude remains the primary teacher. A typed pre-start unknown/exhausted/scarce quota refusal can
select the explicit `gpt-6.1-sol` subscription fallback for final-training rows only. Codex must
independently pass strict fresh quota and the configured scarcity ceiling. No API-key fallback,
automatic new attempt after accepted failure, evaluation relabeling or relaxed gate is introduced.

The canonical `LabelAttempts` owner reserves training slots and publishes labels atomically.
Each successful invocation lock acquisition clears prior parsed-label and reservation indexes
before current persisted state is loaded and validated. Serialized exit also retires the caches.
Sequential instance reuse works; within-invocation append caching, lock/PID/inode checks and
immutable attempt identities remain. The revised source delta was five added lines in the existing
owner, now 491 lines, plus one builder regression. No second writer or reservation store was added.

The user approved this revised design after independent B1/B2 failures. Their findings and rejected
artifacts remain preserved; the accepted result is cumulative attempt 3 under the same 007 package,
revision 2. The [B3 signoff](../../acceptance/training_teacher/B3-SIGNOFF.json) records the resolution.

Implementation used `cortados-workers` after `context_requirements`, with S1 selection, no shell
network or MCP connector grants, and the exact frozen contract. S1 selected `gpt-6-astra`; worker
job `4a0f24b6-5e05-4884-845c-e65ede9c81b9` completed in 92.4 seconds. Root reviewed its diff,
reran checks, obtained independent QA and recorded accepted `job_feedback` with evidence.
This implementation worker is distinct from the explicit `gpt-6.1-sol` training teacher.

## Verification and exact source

The accumulated suite contains 785 tests across 26 recorded lanes, including two benchmark lanes
that are not counted as tests. It covers standalone code, integrations, package acceptance suites,
preserved PTY checks, scoped shared router/nightly tests, seven installed adapter tests and four
installed parity tests. All 57 declared installed package files match the accepted source and wheel.
Dependencies retain the prior pinned versions. Unrelated CortadOS workstreams were not verified.

Root ran the 61 protected teacher tests and 18 builder tests independently before the full suite.
The worker's initial restrictive umask affected a permissions fixture; rerunning under the established
022 umask passed without changing tests. All full-suite lanes ran under that established environment.

Exact full-suite command, from the standalone checkout:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/training_teacher/evidence/run-full-suite.py /Users/velox/.cortados-builds/training-teacher-verification/full-suite-attempt3-revision2
```

| Identity | Value |
|---|---|
| Frozen revision 2 contract/exams | `8217baf` |
| Independent B3 acceptance | `3516c95` |
| Accepted implementation commit | `b91e232` on `plan/subagent-mcp`; local only |
| Exact nine-file candidate manifest SHA256 | `fcbdf80e1b9416741d2bf166505c373895f204ee65f9bc0c03101e82025193c8` |
| Full-suite report SHA256 | `6149368019003e54ec01145cc2c62e4e10ae89e687747d5daeb3c51069c39d47` |
| B3 signoff SHA256 | `6dfd578b17b0ab2b334e76220ae7f012064273d13a25284a2ed651f14562530d` |
| Installed wheel SHA256 | `c240455c13958a712bbe007f85a8efc00829edcd3627e0e563a3b0666caa0081` |
| Cache-owner SHA256 | `562661698ccd83cdd3d13d46b1aa5738ba2cf497bc381df6298d96597b7cb621` |

The full run tested `8217baf` plus these exact candidate bytes; subsequent commits saved the same
production bytes and QA metadata. No code changed after verification. Raw commands, environment
overrides and logs are under `~/.cortados-builds/training-teacher-verification/full-suite-attempt3-revision2/`.

## Efficiency

Synthetic complete-labeling measurements made zero provider calls and exclude provider latency
and model quality. Primary medians use three repetitions; fallback has one measured repetition.

| Workload | Legacy primary median | Candidate primary median | Candidate fallback |
|---|---:|---:|---:|
| 12 requests, no history, one sample | 0.002202 s | 0.006081 s | 0.014551 s |
| 4,554 requests, 4,071 historical IDs, three samples | 0.339296 s | 1.865469 s | 14.643813 s |

Large primary runs preserve 123 calls, 1,449 labels and identical answer signatures. Peak Python
allocation is approximately 35.6 MB versus 28.3 MB baseline. Durable atomic replacements write
512,897,684 logical bytes for primary and 550,399,032 for fallback. Fallback makes 96 synthetic
starts for 1,140 final-training labels; there is no legacy fallback comparator. Required durability
remains slower than legacy append. Repeated historical parsing from B1 was removed without retaining
stale authority across invocations. This is not a faster-routing or improved-model-quality claim.

## Activation and live evidence

The reviewed operator verified exact source/wheel/exam identities, absence of active training,
unchanged live input hashes and full-suite acceptance before installing the dedicated teacher
configuration and two adapters. `train/nightly.sh` was replaced last; backups and before/after hashes
are in the private `activation/` directory. The ordinary worker broker/config and other consumers
remain on release 006. Only the label phase uses release 007; other nightly phases keep their prior
learning owner. No launchd cadence, credentials or Hermes service was changed.

| Teacher installation | Path/setting |
|---|---|
| Immutable release | `~/.local/share/cortados-workers/releases/training-teacher-v1-20261017` |
| Configuration | `~/.config/cortados-workers/teacher.json`, mode 0600 |
| Private broker state | `~/.local/state/cortados-teacher-workers`, mode 0700 |
| Label journal | `/Users/velox/cortados/train/data/.teacher-attempts`, mode 0700 |
| Capacity | One Codex teacher job; 600-second configured maximum |
| Quota | Fresh actual-source verification, maximum age 30 seconds; scarcity ceiling 0.7 |
| Permissions | Shell network off, no MCP connector grants; standard native worker tools retained |

The authorized live command was:

```sh
CORTADOS_TRAINING_TEACHER_V1=1 CORTADOS_MCP_LEARNING_V1=1 CORTADOS_MCP_LEARNING_SITE_PACKAGES=/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages /Users/velox/.local/share/cortados-workers/releases/training-teacher-v1-20261017/venv/bin/python -B /Users/velox/cortados/train/label.py --split train --k 1 --batch 12 --limit 1 --workers 1
```

It exited 0 and published 12 final-training, sample-0 labels with no duplicate occupied slots.
Native job `78cefa5a-d34f-4c4c-9eb3-76199d2b0f50` succeeded in approximately 12.6 seconds, with
fresh `codex-app-server` quota and CLI 0.163.0. Requested model is `gpt-6.1-sol`; the CLI does not
report an independent model identifier. One durable attempt and one broker job exist for this check.
The original 4,312,808-byte label prefix and 2,663 raw evaluation-label rows match their before hashes.
`live-label-verification.json` records the receipts and all protected-file checks privately.

At 18:28 Toronto, nightly remained loaded for **01:30 Toronto**, idle, with no stop/skip-training flag.
Its last full-run exit code is still 1; this live label check is not a new full-nightly success claim.
The next scheduled run can use the fallback, subject to fresh quota and the existing promotion gates.
Claude and Antigravity quota-source verification remains unresolved; unknown means unverified allowance,
not an empty subscription. The existing Cursor/common fixes remain installed.

Rollback must preserve accepted jobs, label slots and the new single writer. Disable fallback via
`CORTADOS_TRAINING_TEACHER_V1=0` while retaining the reviewed 007 label owner; do not blindly restore
the pre-007 writer after Codex labels or reservations exist. Compare current hashes before changes.

The Studio-wide Claude instructions and [Air guide](AIR-TO-STUDIO.md) remain installed. No Air-side
credentials or public S1 port are needed. S1 scores delegated assignments rather than every output
token; Claude remains the chosen Air planner, while this Codex session follows its own worker workflow.


Independent [postactivation verification](../../acceptance/training_teacher/POSTACTIVATION.json)
passed in local QA commit `356eca6`. It independently rechecked the installed file set, live adapters,
configuration/modes, one successful native job, twelve final-training labels and unchanged exam/model
inputs. Report SHA256: `7d6479aa1f25038c483e01e970e4f42242b42004b89fc14293e7998b5b63ca08`.
The shared edits in this follow-up are `train/label.py`, `train/nightly.sh` and the router handoff;
runtime label/journal/private teacher state changes are recorded separately. No shared Git mutation
or push occurred.
