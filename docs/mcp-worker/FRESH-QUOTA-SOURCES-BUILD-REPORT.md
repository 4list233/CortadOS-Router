<!-- PURPOSE: Record independently verified quota-source repair and remaining provider limits.
     CONTRACT: RT-QUOTA-SOURCES-006, preserving RT-FRESH-QUOTA-005.
     DEPENDS: Frozen source, protected exams and private operator evidence named below. -->
# Quota-source repair — October 17, 2026

The common source-verification owner and Cursor reader are implemented. Claude and Antigravity
remain unavailable to strict fleet admission because their required source evidence is incomplete.
This is not a claim that all three providers have been repaired. Air terminal instructions are in
[AIR-TO-STUDIO.md](AIR-TO-STUDIO.md).

## Revision and verification

- Branch: `plan/subagent-mcp`; production commits `b0ed0fb` and `bd01f179e2874bb6b9abac86a3d2874d1b44a28f`.
- Frozen contract/exams preceded builders at `f484e45`; independent D=0 then A=0.
- Initial integrated attempt: 693 methods, 689 passing, 3 failures and 1 error, no skips.
  Independent review found an ambient sibling-module import and a PTY acquisition failure that
  leaked its child/descriptor. Regressions were committed before the grouped repair.
- Final complete maintained **Router/MCP inventory: 695 passed, 0 failures, 0 errors, 0 skips**.
  This includes all 605 earlier scoped methods, 83 source exams and 7 ordinary regressions.
  Unrelated whole-CortadOS suites remain unverified; the shared full-inventory `qa/` runner is absent.
- Fifty-five installed package/data files match source and wheel bytes. Existing dependency versions,
  learning source, task schema, input cleaner and native process owner remain identical.
- Independent final B=0 at `bf87e39`; the guarded rollout below completed.

The root rerun command was:

```sh
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B \
  /Users/velox/.cortados-builds/quota-verification-repair/run-full-suite.py \
  /Users/velox/.cortados-builds/quota-verification-repair/full-suite-attempt2
```

The report retains every lane's exact command, environment, counts and log. Protected test assertions
were not weakened. The six skips in a builder's generic discovery are not used as acceptance:
the root runner executes the maintained integration lanes in their configured test environment.

## What changed

The existing admission/observation owners now retain an immutable private context for the actual
executable, argv, environment, working directory and native source files. Public scope metadata alone
cannot authorize dispatch or continuation. Native collection uses a bounded isolated child and a
nonce-bound response. Final admission rechecks source generations, both 30-second clocks and native
credential validity. Context loss, version changes, unsupported settings or failed refresh refuse.

Cursor's reader reuses its pinned native credential/configuration/dashboard implementations. It reads
the existing file-backed subscription and exact billing-cycle geometry, without starting a model,
refreshing credentials, writing settings, changing accounts or enabling on-demand billing. The
previous interactive probe invoked a setuid process whose ownership could not be proved; strict
containment correctly refused it. The native reader avoids that path. ProcessTracker's permission
and identity rules remain unchanged.

The staged fleet and teacher adapters pass this private context through their existing final gates.
The status command delegates to the same bounded Cursor owner. Existing scarcity arithmetic and
Cursor overflow behavior remain in place. No additional MCP worker backend is added: the worker
service still executes native Codex, while the wider fleet owns the other native lanes.

## Actual provider evidence

At **20:59 UTC on October 17**, a bounded installed-source check constructed the actual staged
Runner environment and brain argv and called the installed owner. It started zero worker/model turns.

| Source | Observation | Admission implication |
|---|---|---|
| Cursor | Auto/Grok pool 100% used; other-model pool 33.2222% used, **66.7778% left**; 22.4% of cycle elapsed. Exact reset: November 10, 22:32:35 UTC; cycle 2,678,400 seconds. | Available under the existing applicable overflow rule; no paid overage authorization. |
| Codex | Native subscription preflight returned available. | Existing strict MCP profile remains usable; this does not enable non-strict legacy argv. |
| Claude | Actual brain and teacher source preparation refused before starting a process. | Unknown; no fresh teacher labeling. |
| Antigravity | Actual source preparation refused before starting a process. | Unknown; native display alone is insufficient. |

These are timestamped observations, not enduring balances. Dispatch always requires its own fresh
check. Cursor's positive result is not a claim that an actual Cursor model task was run.

Claude's native usage endpoint returned 429 again at **21:00:54 UTC**, after honoring its previous
cooldown. It requested another 3,600 seconds; no further diagnostic read is eligible before
**22:00:54 UTC**. Separately, native startup may fetch managed policy that changes transport/auth
selection, so a prelaunch environment-token read does not yet prove the later worker uses that
same source. A successful usage HTTP response alone would not close that gap. The disabled
[Claude annex](CLAUDE-QUOTA-SOURCE-006-ANNEX.md) records the exact native evidence and remaining proof.

Antigravity exposes a typed quota RPC, but an authenticated quota-only source with complete reset
geometry has not been established. Its visible 0% usage lacks the required reset. No reset time,
credential or CSRF token was fabricated, and no authentication boundary was bypassed.

## Timing

The unchanged synthetic native-protocol workload compares baseline `a5a2733` with the candidate.
All results and source-call counts match. Values are milliseconds; each row has eight samples,
except the cache row with sixty. These exclude S1 inference and real provider latency.

| Workload | Baseline median | Candidate median | Baseline p95 | Candidate p95 |
|---|---:|---:|---:|---:|
| Uncached success | 280.349 | 280.337 | 488.727 | 290.754 |
| Fresh cached selection | 1.228 | 1.220 | 1.355 | 1.390 |
| Expired observation refresh | 138.299 | 138.278 | 142.703 | 140.322 |
| Unknown-source refusal | 278.954 | 284.366 | 287.405 | 300.692 |
| Concurrent same source | 282.486 | 281.997 | 293.799 | 308.443 |
| Spent, zero dispatch | 279.818 | 278.868 | 297.500 | 300.997 |

The new actual-source owner separately measured 34.546 ms uncached, 0.340 ms cached,
58.976 ms after expiry, 34.261 ms unknown, 66.448 ms concurrent and 34.469 ms spent, using
the frozen synthetic child seam. Pure native file capture measured 87.555 ms median across eight
samples. The old version has no successful equivalent Cursor admission path, so no fabricated
native speedup ratio is claimed. The real installed Cursor read above completed with a 0.903-second
quota age; this one observation is not a latency distribution.

Commands are `benchmark-mcp-quota.py --source <baseline-or-candidate> --out <output>` and the frozen
`benchmark-actual-source.py --source <candidate> --out <output>`, run with the same QA interpreter
from the private evidence directory. Raw per-sample times, outputs and source-call counts are retained.

## Nightly training and operational limits

The existing 01:30 America/Toronto launchd job remains enabled. A read-only readiness check found
4,554 eligible requests and 4,071 eligible IDs with existing labels, including historical fleet work.
The current nightly script contains teacher quota refusal and continues with existing eligible labels.
It can therefore train without generating new Claude labels, but no future training success is promised.
No new nightly run, checkpoint promotion or teacher call was initiated by this repair.

The October 17 candidate still failed its existing evaluation gates; October 16 remains the served
model. Weights, labels, current pointer, evaluation gates and schedule were not changed.

## Release and rollback evidence

Candidate release: `~/.local/share/cortados-workers/releases/quota-sources-v1-20261017`.
Wheel SHA-256: `3307d94c4a0857214fae4a949fd603ca72c8aa2a9b45271b3104f8345f8b4d90`.
Previous `fresh-quota-v1-20261017` remains the rollback release. Failed attempt-one artifacts are
preserved separately and are not live. Activation must verify exact installed bytes and all live
before hashes, back up files, preserve unrelated Claude settings/job records and install nightly last.
An intervening edit stops replacement. Rollback likewise requires matching current hashes.

Private raw evidence is under `~/.cortados-builds/quota-verification-repair/`: `full-suite-attempt2/`,
`performance-{baseline,candidate,actual-candidate}/`, `installed-candidate-parity.json`,
`learning-parity.json`, `installed-source-check.json`, `installed-teacher-source-check.json`,
`staged006-attempt2/`, `nightly-readiness-readonly.json` and native diagnosis files.
Independent acceptance evidence is committed under `acceptance/quota_sources/`.


## Completed activation

Independent signoff `bf87e39` approved the exact source/wheel/stage hashes. Root verified those
artifacts again before activation. Nine live files changed: shared Router `quota_policy.py` and
`scarcity.py`; PTY `model_routing.py` and `runner.py`; and training `dataset.py`, `label.py`,
`build_sft.py`, `evaluate.py`, `nightly.sh`. Nightly was installed last. All before bytes and modes
were backed up. Other 005 consumers had no dependency literal to change and remained identical.

The idle broker was replaced with a new process from the reviewed release; prior job and feedback
records remain identical. Only the `cortados-workers` command in Claude's user configuration changed.
The first operator attempt stopped before mutation because macOS displays the Python framework
executable rather than the venv path in `ps`. Independently reviewed amendment `8908d32` binds the
recorded old launch argv, exact native creation identity and captured command; it does not relax
process ownership. The guarded second operator invocation succeeded.

Claude reports the service Connected. The actual Hermes Python 3.12.13 interpreter resolves the
new pinned owner. The supported gateway restart reconnected all **12 enabled faces**, preserving
RC28's existing disabled state; the unregistered build profile was not enabled. Gateway, cron units
and nightly launchd job remain loaded. S1 reports healthy with the October 16 model.

Private operational records: `activation/report.json`, `mcp-activation/report.json`,
`host-origin-check.json`, `post-activation-health.json`, plus exact backup bytes in each activation
folder. No shared-tree Git commit or push was made. Air-side login and interactive `/mcp` remain
unverified until Forest runs the guide's checks in the existing Tailscale SSH session.


Independent postactivation verification passed at `bcd6f85`: nine live/backup pairs, all 55 installed
files, live host owner, broker identity, three prior jobs, two feedback records, worker configuration,
training pointer/schedule and health evidence. After the operator's exact command-only configuration
check, `/fullscreenBootPending` changed in Claude's app state. Attribution is unproven; the concurrent
change was preserved. This does not claim whole-current-file equality with the configuration backup.
