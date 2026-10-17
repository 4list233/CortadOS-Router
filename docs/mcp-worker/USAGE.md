<!-- PURPOSE: Explain standalone operator setup and Claude-to-Codex delegation.
     CONTRACT: RT-MCP-001; examples do not install a service or grant live access.
     DEPENDS: Built package, captured native Codex profiles, saved ChatGPT login, optional S1. -->
# Use Claude as planner and Codex as worker

The service gives Claude seven MCP tools: `route`, `delegate`, `job_status`, `job_result`,
`job_cancel`, `context_requirements` and `job_feedback`. Claude prepares the assignment and context, requests a worker, then reviews its result.
Codex runs its own tool loop and uses its saved ChatGPT subscription login. This package currently
implements only the Codex provider; Cursor, Claude worker and local-model adapters remain future work.

S1 is optional. Explicit selection sends the assignment directly to the exact configured model after
admission checks. S1 selection scores a brief through the existing router, which combines capability
requirements with the observed subscription scarcity. Both modes refuse unknown or exhausted quota.
A route is advice; execution rechecks admission. A worker result always requires planner review.

## Install the standalone package

Use a validated macOS Codex CLI **0.162.1 or 0.163.0** profile with an existing ChatGPT login.
The [profile amendment](NATIVE-PROFILE-002.md) pins the observed version for each job and rechecks it
before execution. Other versions and API-key logins refuse; subscription credentials stay native.
Create a Python environment and install from this branch:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

The package pins the official MCP SDK. Choose an absolute, private state directory separate from
`~/.cortados`. Save the following as a private configuration file, replacing all example paths:

```json
{
  "schema": "cortados.mcp-worker.config.v1",
  "client_id": "claude-local",
  "state_dir": "/Users/example/mcp-worker-state",
  "codex_bin": "/opt/homebrew/bin/codex",
  "codex_home": "/Users/example/.codex",
  "model_allowlist": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
  "allow_network": false,
  "connector_registry": {},
  "limits": {
    "max_concurrent_jobs": 2,
    "max_timeout_s": 30,
    "termination_grace_s": 1,
    "startup_timeout_s": 5,
    "max_handoff_bytes": 65536,
    "max_output_bytes": 1048576,
    "metadata_max_age_s": 1,
    "process_poll_ms": 20
  }
}
```

These are the acceptance fixture's measured settings, **not general production defaults**. Thirty
seconds is suitable for a smoke test, not a substantial coding task. Select limits for your workload
and record their reasons and measurements before operational use. Lower time/output bounds interrupt
useful work; higher bounds retain more data and delay containment. Lower freshness/poll intervals add
native metadata/process-table work; higher intervals allow older quota observations and wider process
observation gaps. The [acceptance profile](../../acceptance/mcp_worker/README.md) explains each value.
The 65,536-byte handoff was tested on all three models; it does not bound later repository/tool context.

Launch with the absolute interpreter and configuration paths:

```sh
/absolute/router/.venv/bin/python -m integrations.mcp --config /absolute/worker-config.json
```

For a compatible stdio MCP client, the server entry is:

```json
{
  "mcpServers": {
    "cortados-workers": {
      "command": "/absolute/router/.venv/bin/python",
      "args": ["-m", "integrations.mcp", "--config", "/absolute/worker-config.json"]
    }
  }
}
```

This is the portable configuration shape. The [installation record](INSTALLATION.md) records the
actual local package, operator limits, Claude registration and verification status. A client must be able to start the local process. A remote or sandboxed client cannot assume
it can reach your Mac's saved native login. Managed native policy or requirements that the captured
profile cannot establish cause a visible startup refusal.

## Delegate one assignment

Call `delegate` with a new UUIDv4 request ID for each new attempt:

```json
{
  "request_id": "390c3357-721c-48b7-9624-97225ed817a5",
  "task": "Add a regression test for the documented parser error and run the relevant suite.",
  "context": "Expected error: invalid_reset. Read the repository rules before editing. Report the exact test command and result.",
  "cwd": "/absolute/project-folder",
  "selection": {"mode": "explicit", "model": "gpt-6-luna"},
  "permissions": {"network": false, "mcp_servers": []},
  "timeout_s": 30
}
```

The UUID above is illustrative; generate a fresh one for real work. Retry an uncertain submission with
**the same ID and arguments** to recover the original job without running it twice. Changing arguments
under that ID returns `idempotency_conflict`. The returned `job_id` is used by the three job tools:

```json
{"job_id": "<returned UUIDv4>"}
```

The durable broker continues accepted work if Claude's MCP client disconnects. Reconnect and retrieve
that same job. A broker crash never automatically retries a task. Interrupted work requires inspection
before the planner decides on a new attempt. Concurrent jobs need disjoint canonical directories;
requests for an already claimed folder return `workspace_busy`. There is no implicit queue or worktree.

To use S1, replace selection with:

```json
{"mode": "s1", "routing_brief": "Add one parser regression test. Expected invalid_reset; no runtime changes."}
```

The existing S1 endpoint is selected by `CORTADOS_S1_URL` (default `http://127.0.0.1:8431`). The service
does not start or train S1. The complete scoring text, including any task evidence, is limited to 6,000 Unicode characters and is rejected
when too large. The full task and context go to the worker separately and unchanged. Omitting the
brief scores `task + "\n\nCONTEXT:\n" + context`; the worker handoff has its own explicit reference-data label. S1 does not retain a conversation of its
own; the planner supplies the relevant context. If S1 is unavailable, the request refuses visibly.
See [S1 context and difficulty](S1-CONTEXT.md) for the exact context flow, evaluation limits and a useful brief.

## Share selected MCP tools

Register a compatible MCP endpoint in `connector_registry`, then grant exact tools per job. For example:

```json
{
  "project-search": {
    "transport": "stdio",
    "command": "/absolute/search-server",
    "args": [],
    "env_vars": ["PROJECT_SEARCH_TOKEN"],
    "tools": ["search_docs"]
  }
}
```

The corresponding request permission is:

```json
{"network": false, "mcp_servers": [{"name": "project-search", "tools": ["search_docs"]}]}
```

Environment references are variable names, not secret values. Only the selected endpoint's proxy gets
those values. An HTTP endpoint can instead use `transport`, `url`, `bearer_token_env_var` and `tools`.
HTTPS and loopback HTTP are accepted. The per-job proxy exposes only granted tools; it does not forward
resources or prompts. A grant authorizes that tool's effects, including remote effects. Worker shell
network remains a separate permission requiring both operator allowance and a per-job request.

Connectors must be configured independently for this service. Claude's in-memory OAuth sessions and
app-only tools do not automatically transfer to Codex. The worker also does not inherit ambient native
plugins, apps, hooks, skills or project MCP servers. Give it the necessary instructions in task/context
and only the connectors needed for that assignment.

## Inspect results and retained evidence

`job_result` returns the summary, observed token counters, worker identity, relative artifact paths and
`verification: "planner_review_required"`. Missing counters stay null. `changed_paths` is null because
this version does not independently audit filesystem changes. Review the actual diff and tests before
accepting the worker's claims.

`state_dir/jobs.db` is the canonical job store. Completed event/final-message artifacts are retained
under `state_dir/jobs/<job_id>/`; service transitions and refusals are in `broker.jsonl`. They are not
pruned automatically. Preserve a stopped-broker SQLite backup together with its artifacts. Corrupt or
incompatible storage refuses startup rather than replacing your evidence. The service's process
tracking is validated for the captured native harness; it does not promise containment of deliberately
unobserved daemon escapes.

See [the contract](CONTRACT.md) for the exact protocol and [the baseline report](QA-BASELINE.md) for
historical repository failures. The [build report](BUILD-REPORT.md) records their repairs and the
current green verification separately.

## Supply task evidence and resolve context gaps

Use `context_requirements` before delegation to inspect structural gaps and exact context budgets:

```json
{
  "task": "Inspect the parser regression and propose a fix.",
  "context": "Synthetic example: invalid_reset should identify the rejected input.",
  "selection": {"mode": "s1"},
  "task_evidence": {
    "schema": "cortados.mcp-task-evidence.v1",
    "task_group_id": "00000000-0000-4000-8000-000000000003",
    "components": ["parser.py"],
    "dependencies": [],
    "constraints": ["Preserve existing error codes"],
    "uncertainties": [],
    "verification": ["Run parser regressions and inspect the failing input"],
    "references": [{"source": "synthetic bug report", "text": "The invalid input currently crashes."}]
  }
}
```

Generate a real UUIDv4 for `task_group_id` and reuse it for every retry and revision of the same
assignment, including reworded requests. Distinct attempts still use distinct `request_id` values;
uncertain transport retries retain the original request ID and exact arguments. Without a declared
group the learner can match cleaned task text, but cannot infer that different paraphrases are one task.

The report lists missing fields and ordinary follow-up questions. Claude answers those questions in
its next tool call; this dialogue does not use recursive MCP sampling. Absent arrays mean unknown; `[]`
means explicitly none. Verification requires at least one observable check. `ready` means structurally
complete and within the current limits; routing/delegation still performs ordinary admission. The
tool does no inference, fetching, repository reading or reservation. Return the complete evidence in
the later route/delegate call. Supplied incomplete evidence refuses `needs_clarification`; older calls
that omit evidence retain their original behavior.

Evidence is preserved in caller order and marked as untrusted reference data in both scorer text and
worker handoff. A routing brief does not hide evidence. S1 checks the complete 6,000-character scoring
text; explicit selection checks only the configured worker UTF-8 handoff budget. Neither silently
truncates evidence. References are attributed text and are never fetched by this tool.

## Record planner review and learning provenance

After a job reaches a terminal state, inspect its diff, commands and artifacts, then call `job_feedback`:

```json
{
  "job_id": "00000000-0000-4000-8000-000000000004",
  "feedback_id": "00000000-0000-4000-8000-000000000005",
  "verdict": "accepted",
  "summary": "Reviewed the synthetic parser change and regression result.",
  "checks": [{"kind": "command", "reference": "private test-log hash or location", "claim": "passed"}]
}
```

Generate fresh real IDs. Accepted feedback needs passed checks only; rejected feedback needs at least
one failed check; inconclusive feedback may have none. Kind is command, artifact or review; claim is
passed, failed or unknown. References identify planner evidence and are not fetched. Exact feedback-ID
replay returns the original receipt. A correction uses a new feedback ID and preserves all earlier
assertions. Feedback changes no native result, job state, route or permission.

Receipts say `verification_status: "planner_asserted"`. Native `succeeded` records completion; it is
not evidence that the answer is correct. The latest planner assertion supplies the current verdict,
while the complete history remains attributable. No feedback means unknown, not accepted or rejected.

The standalone `s1/dataset.py` phase collects accepted jobs and route observations from read-only
snapshots, defaulting to `~/.local/state/cortados-workers`; use `--mcp-state-dir /absolute/private/state`
for an isolated source. Optional-source failure emits a structured degraded diagnostic and preserves
previous provenance and the existing sheet/Hermes collection. Each outcome or feedback revision adds
one immutable request row; only the current revision can be labeled, trained or evaluated. Group or
cleaned-task conflicts spanning train/validation/holdout are excluded rather than rewriting old rows.

Only teachers receive attributed, fallible outcome annotations. Student prompts and evaluation inputs
retain the exact pre-execution task/context text. New MCP SFT examples require the selected local
training tokenizer (`CORTADOS_S1_BASE`) and the complete answer to fit the unchanged 1,536-token
training limit. Missing tokenizer evidence or oversize examples produce explicit exclusion diagnostics.
This standalone implementation does not activate the separate live nightly adapters or change promotion.
