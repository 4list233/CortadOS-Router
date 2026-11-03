# Attempt4 terminal worker handback

RT-COWORK-MCP-008 v5 LOCAL CORE; group53f0ea39-a61e-4307-9c2c-466c1b4b0bd1; request3340443b-27f2-4cf2-89d2-395df6a2cf59. Bounded correction under root9eac6e3. Source handback, not independent B acceptance. Prior two v4 failures and first v5 failure remain preserved.

## Correction and owner decisions

Extend CoreRuntime with fail_capacity, called by all three existing monitor latch paths. It sets the one-way capacity/admission latch before audit and persists one existing-schema runtime transition ready/disabled → failed, reason capacity_exhausted, through task_store.event and JobStore.transaction. Repeated bad/good samples return without another transition or reopening. Audit persistence/commit failure invokes the existing fatal owner; its guarded fixed stderr signal cannot recursively retry audit. Existing monitor sample/containment timing and Broker.stop remain unchanged.

Extend CoreBroker dispatch: remove public-code-only fatal promotion. The authoritative JobStore admission_closed result still invokes fatal and returns storage_error. Definitive precommit RemoteError rollback leaves the task inspectable; pre-exec load_manifest generation mismatch retains policy_unavailable. Unexpected exceptions remain fatal.

Retain fatal integrity at causal owners: task_store.rows closes admission on corrupt canonical reads; CoreRuntime.classify closes it on persisted discriminator/binding agreement failure; CoreBroker task/job projection boundaries close it on observation integrity/projection failure while unknown/foreign job pairs retain normal job_not_found; transfer_tickets.require_publication closes it on missing/corrupt committed publication membership. Valid noncomplete publication remains publication_failed without fatal promotion. Reuse existing validators, publication checker, fatal primitive and JobStore transaction owner; no new owner/module/schema/enum/policy.

Ownership predicates, strict HTTP/IPC/monitor deadlines, capacity operation table, reservation arithmetic, definitive rollback and existing fatal precedence were preserved by confined source review. Runtime behavior remains host-unverified because startup is blocked here.

## Verification

All18 exact starting source pins verified against PREBUILD-ATTEMPT-4.json, itself SHA10dcb4b09d9018c2fb9bdb3f1596ff6b28e1eadb98d1ab5a823c505325fe8f70. Root grant SHA40641177da41910a8e3dd2d93e252e69ee9c7d71401adf0274ce7a4232b8c046 and exam manifest SHA8e32a0d697e1da3b06fef53ea8be9e2fd93363c7b88e2e2ab5b9fec8d73525dc match. Before/after682identities cover18source plus664protected:3normative,18fixtures,641priorprotected,rootgrant,currentmanifest. Protected mismatches0;13source unchanged. AST parsing all18 and line caps pass. git diff --check exits0.

source-freeze.json contains exact paths/SHA256/physical line counts for all18. Five changed source counts: remote_runtime402, remote_dispatch261, remote_capacity146, task_store325, transfer_tickets166. Maximum across18 is414. attempt-4.patch is the exact incremental correction against before-source; cumulative-source.patch includes the full18-file candidate against HEAD. checks.json contains machine-readable verification and count accounting.

Shell commands used umask022. Runtime commands set TMPDIR=$PWD/.cowork-core-evidence/attempt-4/tmp:

```text
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest discover -s acceptance/cowork_mcp -t . -p 'test_*.py' -v
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B -m unittest -v tests.test_mcp_route_task tests.test_mcp_worker tests.test_mcp_learning
git diff --check
```

core.log and core-final.log retain initial and final-candidate runs, both exit1:114methods,20pass,94distinct methods unsuccessful;3failure events,92error events,0skips.91error blocks show PermissionError EPERM reading host boot identity;3child-startup assertions report safe storage_error; one of those methods additionally errors with BrokenPipeError during cleanup. Finalization BufferError is also preserved. Failure/error events overlap methods and must not be subtracted blindly.

ordinary.log exit1:52methods,49pass,3errors,0failures,0skips; all3errors have the same host-boot-identity denial. No source/test/permission shim or native fallback was applied. No host qualification is claimed.

The three new capacity transition exams and unchanged generation/definitive-rollback exams were collected and attempted but blocked before runtime startup. Full28host lanes/899expectedmethods and required performance remain root/independent-QA obligations after freeze. No unexecuted suite is called passing. Root command:

```text
/Users/velox/.cortados-builds/mcp-worker-qa-env/bin/python -B acceptance/cowork_mcp/evidence/run-full-suite.py <fresh-private-output>
```

## AC accounting

AC01/02 are excluded future-auth. AC03 through AC16 all have attempted coverage through core-final.log, but no complete AC pass is claimed:

| AC | Available evidence and remaining block |
|---|---|
| 03 | Schema/config controls pass; runtime admission blocked |
| 04 | Ordinary compatibility controls pass; runtime composition blocked |
| 05 | Closed shape/error controls pass; runtime framing/audit blocked |
| 06 | Runtime ownership/inspection blocked |
| 07 | Runtime replay/definitive rollback blocked |
| 08 | Task preparation/concurrency runtime blocked |
| 09 | Ordinary handoff/bootstrap/argv and downgrade controls pass; remote runtime/native generation blocked |
| 10 | Runtime byte transfer blocked; host performance pending |
| 11 | Runtime tickets/lease/deadline blocked |
| 12 | Runtime filesystem containment blocked |
| 13 | Runtime output publication blocked |
| 14 | Runtime HTTP/disable/transfer blocked |
| 15 | Runtime recovery/audit/containment blocked |
| 16 | Runtime capacity/audit/table blocked; host performance pending |

All14ACs require full independent host reproduction. This handback assigns no RubricB score.

## Residual risks and confinement

Five allowed files changed. Runtime behavior, strict timing, containment, performance and full regression acceptance remain pending host evidence. Existing performance on prior rejected candidates does not qualify this one.

No tests/contracts/dependencies/auth/retention/operator/live state changed; no commit/push/deployment/listener/network/model/provider call or nested delegation. No cortados-workers tools were exposed to this worker. No additional attempt, model substitution or automatic failed-job retry was started. Evidence only in new attempt-4 subtree; previous evidence retained. Initial here-document shell commands were blocked before Python execution; subsequent checks used python -c. An overly broad standards-path discovery produced OS-denied listings; no unrelated conversations/drafts were read. Subsequent standards reads used the exact pinned local directory.

Source terminal freeze: no further implementation changes after source-freeze.json. Root remains reviewer and acceptance owner.
