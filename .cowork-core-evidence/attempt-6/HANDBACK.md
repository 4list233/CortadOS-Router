# RT-COWORK-MCP-008 v6 — cumulative source attempt 6 / revision 3 attempt 2

Builder handback only. Completion is not acceptance. Five prior rejected sources,
their evidence and the frozen contracts/exams remain unchanged. No commit made.
Assignment group: 53f0ea39-a61e-4307-9c2c-466c1b4b0bd1.
Starting HEAD/root grant: 32178270e522606f7409c544171fa39edb9f79ea,
branch plan/subagent-mcp. No other source writer was used.

## Confined correction and ownership

Only integrations/mcp/broker.py changed relative to the supplied candidate5:
14 added physical lines, now 445 physical lines. See correction.diff for the
attempt-only patch. before-working.diff preserves the inherited cumulative Git
diff; before/ preserves all 18 supplied source files. verification.json records
every source identity and line count, including all 17 unchanged sources.

Search/read evidence: Broker.run_worker, admit_launch, stop, contain_remote,
finish and shutdown; CoreRuntime.authorize/observe_monitor/fatal; the maintained
MonitorPendingLaunchExam in test_tasks.py and Watchdog bootstrap/command handling.
Disposition: extend Broker's existing launch owner; adopt runtime monitor
observation and once-only fatal signal; adopt Broker.stop and existing Watchdog
receipt/death proof. No replacement owner, queue, watcher, restart or module.

The remote launch checks the runtime after bootstrap send following subprocess
return, after readiness read, and after fresh launch admission returns. This
covers the three maintained waits, including when the one containment enumeration
has already finished without discovering the pending process. Monitor observation
runs synchronously before checking the fatal/capacity/storage latch, so queued
observer order cannot authorize START. On loss the existing stop owner receives
this accepted job ID and capacity_exhausted; its persisted prior stop intent and
terminal state remain authoritative. The ordinary branch never calls this guard.
No await occurs between a healthy final guard return, the canonical record read,
and START's write. Existing stop failures still attempt STOP through Broker.stop;
existing worker/receipt/publication cleanup remains the owner of proof.

Internal implementation choice: one Broker helper shared by those launch seams,
using the existing latch and stop owner. It deliberately does not apply the public
disabled-owner permission rule to accepted worker lifetime. No product-policy
BOUNDED choice or contract amendment was needed. No new timeout, grace, retry,
schema, state or dependency was introduced.

## Verification and limits

All commands ran from /Users/velox/.cortados-builds/cortados-router-repo.
Build evidence uses only .cowork-core-evidence/attempt-6, umask 022, and TMPDIR
set to its absolute tmp child. Initial zsh heredoc preparation failed twice with
"can't create temp file for here document: operation not permitted"; neither
attempt edited sources. Preparation then used Python -c, with no bypass.

Input hashes verified before edit:

* Parent: 16ce4ca69cfd7e43684f7a76de43c386b277467f3d193491f9d64e69cfe1388f
* EXECUTION: a6f93d4b51d050e0b7fca16cdd291b2bc844a81bd798ceba0ab3c3e815fb6750
* Exam MANIFEST: 01c2cfc758cb829f97a89830325a7ce93be88a091b411c2fc016e60d26af3fd0

inputs.json preserves all 18 pre-edit identities and 1016 tracked protected-file
hashes. Final verification found zero protected changes, including tests,
manifests, contracts and historical ledgers. All 18 sources parse with ast.parse
and remain below 500 physical lines. No bytecode was written.

Executed exactly:

```
umask 022
export TMPDIR="$PWD/.cowork-core-evidence/attempt-6/tmp"
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v
```

Raw core.log/core.exit: 162 methods, 3.967 seconds, exit 1; 20 passing methods,
3 failure events, 152 error events, zero skips. Events overlap methods (including
subtest/teardown errors); these figures must not be added as disjoint methods.
Host boot identity read raises PermissionError errno 1 at native_process.py:40,
before runtime construction can exercise the launch correction. All nine pending
launch methods are ERROR at that setup boundary, not successful launch proofs.
The three assertion failures are two crash-preparation child startup checks and
the separate-process transfer startup check, each observing storage_error rather
than ready. The log also retains the final BytesIO BufferError diagnostic.

Independent environment probes, without bypass:

* /usr/sbin/sysctl -n kern.bootsessionuuid: exit 1,
  "sysctl: sysctl fmt -1 1024 1: Operation not permitted".
* /bin/ps -o pid,ppid,command -p $$: exit 127, operation not permitted.

These are native sandbox environment blockages. No synthetic boot identity,
process-observation substitution or weakened test/production check was used.
git diff --check exited 0 (diff-check.log). Native launches, receipts, containment,
timings and resource scaling for the revised path remain unproven here.

## Per-AC observed ground truth

No complete AC is claimed passing from this blocked core run. This table states
the executable observations, not implementation claims or independent acceptance.

| AC | Observed local ground truth / remaining check |
|---|---|
| 008-03 | Synthetic principal/config constructor controls pass; runtime admission and all nine launch controls blocked at host boot read. |
| 008-04 | Ordinary bootstrap, argv/environment, live protected training-input guard and handoff seam controls pass; frontend/process lifetime checks blocked. Source diff confined to remote launch calls. |
| 008-05 | Closed wire/private constructors, errors, strict JSON, bounds and immutable-copy controls pass; actual canonical-reader/runtime/IPC cases blocked. |
| 008-06 | Owned/foreign lookup, cancel, feedback and stream lifetime cases blocked before runtime ready; no ownership acceptance claim. |
| 008-07 | Atomic acceptance/replay/rollback cases blocked; supplied store/dispatch implementations unchanged. |
| 008-08 | Preparation and race cases blocked; child crash observers fail startup ready checks. |
| 008-09 | Ordinary argv/bootstrap, full-packet handoff seam and watchdog discriminator control pass; actual remote launch/policy cases blocked. |
| 008-10 | Separate-process transfer observer fails startup ready check; binary/chunk/memory behavior not measured. |
| 008-11 | Ticket, offset, lease, lost-ACK and frame cases blocked; no protocol change in correction. |
| 008-12 | Descriptor/path/output cases blocked; no filesystem owner changed. |
| 008-13 | Publication/immutable-output cases blocked; native outcome and publication owners unchanged. |
| 008-14 | Deadline/Range/disable/response lifetime cases blocked; no HTTP, IPC, lease or deadline code changed. |
| 008-15 | Invalid-core-before-DB control passes; recovery, reader-corruption, monitor-lifetime and containment cases blocked at host boot read. |
| 008-16 | Config dimensional controls pass; actual accounting/monitor/STOP/receipt/reservation and pending launch ground truth blocked. |

AC-008-01/02 remain excluded future auth, not passed or implemented.

## Remaining independent obligations and deviations

No semantic deviation selected. Execution environment prevents host acceptance.
cortados-workers tools were not exposed, so no route/delegate/job retry or fallback
was made. No shell network, connector access, API/model/provider call, listener,
installation, credential, auth/operator/retention or schedule change was made.

As assigned, root separately runs the full 947-method / 28-lane host inventory,
independent B and required matrix, ordinary comparison and supplemental benchmarks.
Those suites and measurements were not run here; no performance or full-system
pass is claimed. Earlier evidence is historical, not a substitute for a new run.
Root must specifically reproduce all nine pending-launch cases and the full
monitor/sink/receipt-uncertainty matrix on the real host before accepting this
same-design correction. No missing native proof is inferred from code inspection.
