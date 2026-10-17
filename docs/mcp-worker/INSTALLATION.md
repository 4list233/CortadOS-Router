<!-- PURPOSE: Record the authorized local installation and its independently observed status.
     CONTRACT: RT-MCP-001, FOLLOWUP-001 and NATIVE-PROFILE-002; no new service policy.
     DEPENDS: Pinned bd01f179 source wheel, native ChatGPT login, Claude Code user configuration. -->
# Local Claude Code installation

Updated on 2026-10-17 at Forest's request. Claude remains the planner; Codex uses the existing
ChatGPT subscription for worker execution. The current non-editable wheel contains the exact
reviewed source at `bd01f179`, branch `plan/subagent-mcp`. Nothing was pushed.
It retains `context_requirements`, `job_feedback` and the installed nightly learning owner, and adds
private actual-source binding and the native Cursor quota reader. See the [source-repair report](FRESH-QUOTA-SOURCES-BUILD-REPORT.md)
for independent verification and current operational limits.

| Item | Local path or setting |
|---|---|
| Release | `/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017` |
| Interpreter | Release directory plus `/venv/bin/python` (Python 3.14.6) |
| Configuration | `/Users/velox/.config/cortados-workers/config.json` (mode 0600) |
| Durable state | `/Users/velox/.local/state/cortados-workers` (directory mode 0700) |
| Native CLI | `/opt/homebrew/bin/codex`, validated version 0.163.0 |
| Native login home | `/Users/velox/.codex`; credentials remain managed by Codex |
| Native models | `gpt-6-luna`, `gpt-6.1-sol`, `gpt-6-astra` |
| Claude entry | `cortados-workers`, user scope, stdio |
| Optional S1 | `CORTADOS_S1_URL=http://127.0.0.1:8431` |
| Network and connectors | Shell network disabled; connector registry initially empty |

Wheel SHA-256: `3307d94c4a0857214fae4a949fd603ca72c8aa2a9b45271b3104f8345f8b4d90`.
All 55 installed package/data files match the tested source and wheel. Dependencies are pinned to the prior
tested release's installed versions and recorded in `release.json`. Editing the development checkout
does not alter this installed release. Other Claude MCP entries were preserved; no credentials changed.

The equivalent registration command is:

```sh
/opt/homebrew/bin/claude mcp add --scope user --transport stdio cortados-workers \
  --env CORTADOS_S1_URL=http://127.0.0.1:8431 -- \
  /Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/bin/python \
  -B -m integrations.mcp --config /Users/velox/.config/cortados-workers/config.json
```

`claude mcp get cortados-workers` reports **Connected**. Reopen an existing Claude Code session to
load the new user configuration, then inspect `/mcp`. Request explicit model selection or S1 delegation
through `cortados-workers`; Claude supplies the task, relevant context, folder and permissions and
reviews the returned result. S1 must already be running for S1 selection; exact-model mode does not
require S1. This registration adds tools; it does not force every Claude turn to delegate.

## Operator limits

These values are explicit local configuration, not library defaults. Installed native smoke checks
used the same process/startup/freshness limits; their individual requests were capped at 90 seconds.
The 30-minute operator ceiling permits a planner to request longer coding jobs; such durations were
not load-tested. The 8 MiB output ceiling is workload headroom, not a measured output-size guarantee.

| Setting | Value and units | Reason; effects of choosing lower or higher |
|---|---:|---|
| `max_concurrent_jobs` | 2 jobs | Tested capacity for disjoint folders; lower serializes work, higher increases resource and quota contention. |
| `max_timeout_s` | 1,800 seconds | Ceiling for coding assignments; lower can interrupt useful work, higher holds stalled workspace claims longer. |
| `termination_grace_s` | 5 seconds | Allows measured native cleanup; lower can interrupt cleanup, higher delays forced containment. |
| `startup_timeout_s` | 20 seconds | Room for native metadata and proxy startup; lower can reject valid setup, higher delays a stalled admission. |
| `max_handoff_bytes` | 65,536 UTF-8 bytes | Captured boundary verified on all three models; lower rejects useful context, higher exceeds the validated handoff profile. |
| `max_output_bytes` | 8,388,608 bytes | Room for coding events; lower can cut off valid logs, higher raises retained disk and parsing costs. |
| `metadata_max_age_s` | 30 seconds | Bounds quota staleness across short jobs; lower adds native metadata probes, higher admits against older observations. |
| `process_poll_ms` | 20 milliseconds | Captured native descendant sampling; lower increases process-table CPU work, higher widens short-lived-process observation gaps. |

## Verification and activation

The current source-repair update passed **695 tests**, with no failures/errors/skips, across the
complete scoped Router/MCP inventory and maintained host/nightly/PTY lanes. A bounded installed
metadata check verified Cursor and the strict Codex source; Claude and Antigravity remain unknown.
No worker/model turn was used to check quota. Existing jobs/feedback were preserved, Claude's MCP
entry reports Connected, and all 12 enabled fleet faces reconnected. Reopen existing Claude sessions
to load the new frontend. Unrelated whole-CortadOS suites were not run.

The earlier 605-test fresh-quota release additionally passed a real Codex subscription scratch task
and durable feedback/replay. The earlier 388-test learning update verified read-only collection.
These unchanged owners and their accumulated exams remain covered. See
[QUOTA-OPERATIONS.md](QUOTA-OPERATIONS.md) for provider limitations and
[AIR-TO-STUDIO.md](AIR-TO-STUDIO.md) for remote terminal use.

The following evidence describes the original installation, before the learning/context update:

- Complete router inventory: **331 passed, 0 failures, 0 errors, 0 skips** at `a27afd0`.
- Installed wheel, outside the source checkout: **6/6 native subscription probes passed**, covering
  all three models, S1, granted MCP access with denied outside-folder write, and real child cancellation.
- Native 0.163.0 compatibility captures: **12/12** per-model execution/boundary/cancellation checks.
- Claude MCP registration and connection: **passed**.
- Claude planner → MCP → S1 → Codex: **passed**, independently checked against the actual persisted
  job, worker events, exact marker file and Claude's final result.

The user confirmed that the same absolute Claude binary reports a Claude Max account in their Terminal.
Processes launched from Codex returned an expired OAuth error even after that sign-in. No credential
deletion, extraction or replacement was attempted. Computer Use refused access to Terminal, so the
user ran the prepared operator check successfully in the working terminal environment:

```sh
python3 /Users/velox/.cortados-builds/mcp-worker-followup-verification/claude-planner-check/run.py
```

It asks Claude to submit one fixed-id S1 assignment, checks the persisted job/version, verifies an exact
marker file in a scratch folder and Claude's retrieved result, then writes `report.json` beside the
script. It disables unrelated hooks/tools and API-provider overrides for this invocation. Repeating
the command retains the same request ID rather than automatically creating another worker attempt.
The saved [report](evidence/a27afd0/claude/report.json) confirms job
`5e612218-93ea-46dc-8471-fef3acdbb7e8`: S1 returned R1/D=3, the resolver chose `gpt-6.1-sol` with fresh
quota, native 0.163.0 succeeded, and Claude returned `CLAUDE_CODEX_MCP_OK`. Exactly one job exists for
the request ID. The successful planner invocation took 20.455 seconds. Reported Claude `costUSD` is
list-price telemetry, not evidence of an API charge; the invocation used the existing Max login.
The Codex-launched Claude authentication discrepancy remains an environment limitation, but does not
block this verified user-Terminal workflow or the registered MCP service.

Connector sharing is supported through explicit registry entries and per-job grants; none are configured
in this installation yet. Existing Claude connectors and in-memory OAuth sessions are not inherited.
See [USAGE.md](USAGE.md) for grants and [S1-CONTEXT.md](S1-CONTEXT.md) for the scoring boundary.

## Disable or update

To remove the Claude entry, use `claude mcp remove --scope user cortados-workers`. This disconnects
the frontend; accepted durable jobs can continue. Inspect/cancel active jobs through the service
before deliberately shutting down its broker. Preserve the job database and artifacts for review.
The previous Claude configuration is retained privately at
`/Users/velox/.config/cortados-workers/claude-config-before-mcp.json`; do not blindly restore it over
newer unrelated configuration changes. No launchd job was installed.

An update should build and verify another pinned release, then change the registered interpreter.
The service refuses unvalidated native Codex versions; an automatic CLI upgrade requires a reviewed
profile extension before worker execution resumes. Existing job provenance remains unchanged.
