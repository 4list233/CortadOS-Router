# cortados-routed-subagents

A Claude Code mod that routes **every subagent** your session spawns through the CortadOS provider, while the
main session keeps its own direct model.

## What it does

When the session calls the Agent tool to spawn a subagent, this mod intercepts the call and instead runs the
subagent as a headless `claude` process **on this machine**, pointed at the CortadOS provider
(`ANTHROPIC_BASE_URL`). From there:

1. The provider scores the task with **S1** and routes it to the cheapest capable model in the pool.
2. That model runs **headless on the provider host** (it never runs a tool itself).
3. When the model wants a tool (read a file, edit, search the web, run a command), the call is **bridged back**
   to the subagent process here and executed **on this machine**, against these files — then the result is fed
   back to the model, which continues.

So: the subagent's harness and its tools live on this machine; the model reasoning is routed and runs on the
provider host; S1 does the scoring. The subagent's answer comes back tagged `[routed via CortadOS → <model>]`.

This is the agentic counterpart to `route_task` (the project's text-only delegation tool): `route_task` returns a
routed model's text with no tools; this mod gives a routed subagent your machine's full toolset.

## Install

```
/plugin marketplace add 4list233/CortadOS-Router
/plugin install cortados-routed-subagents@cortados-router
```

Then set the provider URL (and, if you keep the token inline rather than in a file) in the plugin's config.

## Configuration

| Field | Default | Meaning |
| --- | --- | --- |
| `providerUrl` | *(unset)* | Explicit override. Otherwise uses `CORTADOS_PROVIDER_URL`, then `CORTADOS_PROVIDER_URL_FILE` or `~/.config/cortados/provider-url`. HTTP and HTTPS supported; invalid configuration refuses routing. |
| `providerTokenFile` | `~/.config/cortados/provider-token` | File holding `CORTADOS_PROVIDER_TOKEN`, read fresh at spawn time so the token is never stored in settings. `~` expands to `$HOME`. |
| `providerToken` | *(unset)* | Optional inline token override. Prefer the file. |
| `model` | `cortados` | Model id sent to the provider. `cortados` lets S1 route; a concrete id pins one lane. |
| `permissionMode` | `bypassPermissions` | Headless permission mode for each routed subagent (see **Blast radius**). |
| `allowedTools` | `Read,Write,Edit,MultiEdit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite,NotebookEdit` | The built-in tools a routed subagent has **on this machine**. Passed as both `--tools` (availability — the real boundary, so a tool left out is genuinely absent even under `bypassPermissions`) and `--allowedTools` (permission). Covers built-in tools only; MCP tools are controlled separately. |
| `claudeBin` | `claude` | The `claude` executable used to run each routed subagent. |
| `routeBackground` | `false` | Whether to route background subagents too (off because a routed subagent runs under the `subagentTimeoutMs` ceiling). |
| `subagentTimeoutMs` | `600000` (10 min) | How long a routed subagent subprocess may run before it is aborted. The max is `600000` (10 min): that is the host's own cap (Claude Code validates `$.process.run`'s `timeoutMs` as an integer from 1 to 600000), so a larger value is clamped to it and a fractional one is rounded down. Lower it to fail fast; a non-positive/invalid value falls back to the default. |
| `maxTurns` | `0` (off) | Opt-in. When positive, passed as `claude -p --max-turns N`: the child stops after N agentic turns instead of looping until the timeout kills it. Off by default because there is no evidence yet that a blanket turn budget is safe for builders: a legitimate build can need many turns, and a builder stopped mid-way has acted without finishing. The runaway case that prompted it (incident 2026-11-03) is covered without it by the partial-result handback and the stop-on-dead-tool prompt line. `0` (or any non-positive/invalid value) leaves the flag off. A stopped child is reported like any other failure, with its partial result (see **When a routed subagent fails**). |

Runs under the Claude desktop app as well as the CLI: the child is started with `CLAUDE_CODE_ENTRYPOINT=cli` so it authenticates with the provider token rather than the host's claude.ai session.
The child starts with an empty MCP config (built-in tools only); the parent's MCP servers are never exposed to the provider.
| `orchestratorMode` | `true` | Direct the main session to plan and delegate (see **Orchestrator mode**). Guidance by default; keeps full tool access. |
| `orchestratorEnforce` | `false` | Also **hard-block** every tool outside `orchestratorAllowedTools` on the main loop (a strict brain that cannot act even if it tried). Off by default — the directive is trusted. |
| `orchestratorAllowedTools` | `Read,Grep,Glob,TodoWrite,Agent,mcp__cortados-route__route_task,AskUserQuestion,ExitPlanMode,Skill,ToolSearch` | Allowlist used **only when `orchestratorEnforce` is on**: the tools the brain may run itself; everything else is refused and must be delegated. `Agent` and `route_task` (both delegation primitives) are always allowed regardless. |
| `excludeBuilderOnReview` | `true` | When a delegated Agent call is a review (see **Independent reviews**), send `x-cortados-exclude-model` to the provider with the **main session's own model** plus the models routed (non-review) subagents built with since the last review (up to 3), so that review is kept off them on every lane. |
| `mainSessionGuard` | `true` | Warn-only nudge when the **main session** does substantive work itself (see **Main-session guard**). Never blocks. `/cortados-guard on\|off` overrides it. |
| `officeView` | `true` | Feed the CortadOS **Office view** (`GET /v1/cortados/office`): each routed request carries `x-cortados-session` (sha256 of a per-install salt kept in the mod's store and the session id, cut to 16 hex; never the raw id), `x-cortados-project` (the project folder's name), `x-cortados-device` (`hostname -s`) and `x-cortados-task-title` (the Agent call's description, 80 printable ASCII chars at most); the main session POSTs `/v1/cortados/office/heartbeat` with its status and current tool **name** on session start/end, turn end and tool calls (on change at most every 2 s, else every 30 s; serialized and coalesced with a trailing send; tool hooks never wait, session end flushes and awaits `ended`; a slow fetch is noted after 2 s but retains the send lock until it settles because the host HTTP API cannot cancel requests; a failure is noted once in the transcript). Metadata only: never prompt text, tool input or file contents. Takes effect after a Claude Code restart. |

## Orchestrator mode

With `orchestratorMode` on (the default), the main session is directed to act as a **planning brain**: whatever
top-tier model you run it on (e.g. `--model fable`) plans and decides, and substantive work is delegated to routed
subagents. This is **guidance, not a cage** by default:

- A standing **directive** is injected into the system prompt telling the model to decompose each request and
  delegate via the Agent tool (or a text subtask via `route_task`), delegating by default for anything substantive —
  but allowing it to act directly when delegating would clearly be wasteful (a trivial edit, its own bookkeeping, a
  quick check). The brain keeps its full tool set; the directive is trusted to keep work flowing to the pool.
- **Hard enforcement is opt-in.** Set `orchestratorEnforce: true` to additionally refuse, on the main loop, every
  tool not in `orchestratorAllowedTools` — so a brain that forgets or hallucinates still cannot do the work itself.
  An allowlist is used (not a denylist) precisely because a denylist would miss mutating tools like `Monitor`,
  `memory_write`, or `ExitWorktree`; `Agent` and `route_task` are always allowed so delegation is never locked out.
  Subagents are never gated — they run the full toolset (and are themselves routed).

The result: the top model is the brain and work flows through routed subagents, while the brain can still handle the
odd trivial thing itself unless you turn on strict enforcement. Turn the whole thing off with `orchestratorMode:
false` (the mod then only routes subagents, without directing the main session at all).

## Main-session guard (warn only)

With `mainSessionGuard` on (the default), the mod watches the **main session's** `Edit`, `Write`, `NotebookEdit`
and `Bash` calls and nudges it to delegate when one is substantive. It **never blocks, denies or rewrites a call**:
every call runs unchanged, and a failure in the guard itself is swallowed. Subagents, teammates and engine forks
(any call carrying an `agentId`) are never watched.

A call is substantive when:

- an `Edit`, `Write` or `NotebookEdit` carries new content (`new_string` / `content` / `new_source`) of **more than 40
  lines or more than 2000 characters**. 40 lines is about one function or a screenful, so the directive's "trivial
  touch" (a one-line fix, a config tweak) stays far under it; 2000 characters catches long-but-few-lines content
  (minified JSON, prose) the line count would miss; or
- a `Bash` command, in any `;`/`&&`/`|` segment and after leading `VAR=value` assignments, starts like a build or test
  run: `python -m unittest` / `python -m pytest`, `pytest`, `make`, `npm`/`yarn`/`pnpm` `build` or `test` (with or
  without `run`). A text heuristic, not a shell parse: `grep make Makefile` or `echo pytest` do not count.

Each substantive call bumps a session counter shown on the mod's status line (`main did 3 big edits, 1 build/test
run; delegate to routed subagents`) and shows a toast at most **once per 10 minutes** (one nudge per stretch of work;
a toast per edit would be noise). The counters and the throttle live in memory and start over when the mod reloads.

While a routed subagent is running, the status line stays its own (`↗ routed subagent (...)`): the guard keeps
counting and still toasts, and puts its counter back once the last routed child finishes. A guard step that fails
(store read or write, clock, status, toast, registering `/cortados-guard`) is reported **once per kind** as a dim
transcript notice naming only the step and the error class (no message text, no input), and the tool call still runs
exactly once, unchanged. A failed `$.ui.status`/`$.ui.toast` underneath the host is dropped and reported by the host
itself, since those calls are fire-and-forget.

`/cortados-guard off` / `/cortados-guard on` turns it off or on and is kept in the mod's `$.store`, so it survives
restarts and overrides `mainSessionGuard` (it takes effect at once, even if the first store read is still in flight;
if saving fails it applies to this session only and says so); `/cortados-guard` with no argument reports the state. No model calls, no
network.

## Independent reviews

With `excludeBuilderOnReview` on (the default), a delegated Agent call that looks like a review — its `description`
matches `/\breview/i`, or its `subagent_type` matches `/review/i` — is sent to the provider with
`x-cortados-exclude-model: <models>` (comma-separated): the main session's own model plus the **worker models** — the
backend models routed non-review subagents actually built with since the last review. The provider keeps that review's
routed conversation off those models on every lane (date suffixes and case ignored; a `sub/`/`api/` pin on it, or an unknown model name, is
refused with 400; if excluding it would leave nothing that fits, the provider returns 400 rather than a retryable
503). Say "review" in the Agent description if you want this to kick in.

Honest limits:

- **How the builder is tracked.** Every routed run sends a fresh `x-cortados-run-id`; afterwards (success or
  failure) the mod fetches `GET <providerUrl>/v1/cortados/runs/<id>` and remembers, for each non-review run, the
  models that built with tools — the record's `builder_models`, or just its primary `model` on an older provider
  that doesn't send them (side calls such as title/summary one-shots don't count) — distinct, most recent last,
  **up to 3**, each run's primary model added last so it is the last evicted. The next review excludes those plus
  the session model; a review that completes while kept off them forgets exactly those models as tracked at its
  dispatch (a worker that finished while it ran stays tracked, even on the same model); a failed one — or one the
  router refused and that was retried without the exclusion — keeps them, so the next review is still kept off them.
  The list lives in memory and **resets when the mod reloads** (e.g. a Claude Code restart) — the first review after
  that only excludes the session model. If the record can't be fetched (provider down, older provider, 3 s timeout)
  that run's models are simply not tracked; the subagent itself is never failed over it. Work the main session did
  itself is covered by excluding the session model; work done outside this mod (e.g. `route_task`) is not tracked.
- **The result label shows the real routed model.** `[routed via CortadOS → …]` uses the run record's reveal label
  (e.g. `claude-sonnet-5 · 62% left`), falling back to what the CLI reported (`cortados`) if the record is missing.
- **Excluding can quietly cost strength.** The 400 for "nothing fits" only fires when excluding leaves *no usable
  model at all*. When the excluded model was the only one meeting the band's required strength, the router still
  routes the review to the strongest remaining model — possibly a weaker one — and notes that only in its route log,
  not in the reply.
- **Fallback when the provider refuses the header.** If a review run that sent the header fails and its output or
  stderr names `x-cortados-exclude-model` (e.g. the session model was renamed and the provider no longer serves that
  id, or nothing usable is left), the mod retries the review **once without the exclusion**, within what remains of
  `subagentTimeoutMs` (one budget for both runs). It shows a toast and prefixes the result with
  `[review NOT kept off <models>: <reason>]` (naming the full excluded list) so the parent model knows independence was not enforced. Any other
  failure is not retried.
- A model is only sent if it matches the provider's id rule (at most 128 characters of
  `A-Z a-z 0-9 . _ / [ ] : -`). If the session model can't be read, isn't a safe id, or the session is itself routed
  (model `cortados`), only the worker models are excluded; when nothing at all can be excluded, nothing is sent and a
  toast says the review's independence isn't enforced.
- `route_task` does not send this header at all — only the Agent tool does.
- Detection is a plain keyword heuristic on the description/subagent type, not a semantic check.

## Blast radius

The tools a routed subagent calls **execute on this machine**. With the defaults (`allowedTools` including `Bash`
and `permissionMode: bypassPermissions`), a routed — and possibly cheap — model can run shell and edit files here
unattended. That is the full-agentic setting. To narrow it, drop tools from `allowedTools` (e.g. remove `Bash`,
`Write`, `Edit` for a read-only researcher) or set `permissionMode` to `acceptEdits` (auto-approve edits, no
unattended Bash) or `default`.

## What is not routed

These pass through to a normal in-process subagent on your direct model:

- **`fork`** — a fork inherits the parent's context and model and cannot run on another provider.
- **isolated agents** (`isolation: worktree` / `remote`) — the native runner owns the worktree/remote semantics;
  routing locally would silently drop them and let parallel isolated agents clobber each other.
- **named agents** (`Agent({ name })`, including teammates) — the parent registers the name for `SendMessage`; a
  subprocess can't join that registry, so it would lose its messaging lifecycle.
- **background subagents** — unless `routeBackground` is on, because the routed subprocess runs under a
  10-minute ceiling.
- **when no provider token is available** — better a working native subagent than a failed call.

Once a routed subagent has been launched it has already acted, so if it does not finish cleanly (nonzero exit, a
terminal error, truncated output, or a result the Agent schema would reject) the mod returns a **refusal** with the
reason — it never silently re-runs the task as a native subagent, which would duplicate any non-idempotent work.

## When a routed subagent fails

Each child runs with `--output-format json` (the result object only: `stream-json` would put every intermediate
message on stdout, and a tool-heavy child could overrun the host's output cap and be refused even though it succeeded)
and a `--session-id` (a v4 UUID) the mod chooses. On failure its transcript is found by that UUID with one fixed
`find "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects" -maxdepth 2 -name '<uuid>.jsonl'` (the UUID is checked against a
strict regex and passed as an argument, never spliced into the script); it is not derived from the cwd, because
Claude Code hashes long project-folder names. On a timeout, a `--max-turns` stop or any other unclean finish, the
refusal names the elapsed time, the timeout in seconds, the session id and transcript path, how many assistant turns
(distinct message ids) and tool calls the child made, and its **last assistant text** (last 4000 characters), so the
parent can see how far it got instead of losing all the work. The whole refusal is scrubbed of the provider token. The
child's prompt also ends with: "If a tool returns nothing useful twice in a row, stop and report what you have and
what failed."

## Tests

Run the mod's tests in the native hook host (no model calls):

```sh
CORTADOS_HOME=$(mktemp -d) CORTADOS_SCARCITY_REFRESH=off claude plugin test integrations/claude-mod/cortados-routed-subagents
```

Run from the repository root. This covers `hooks/register.test.ts` and `hooks/guard.test.ts`.

## Notes

- Claude Code logs `unrecognized_model: cortados` and assumes a 200k context window for the routed subprocess.
  Cosmetic; the route still works.
- Recursion is prevented: each routed subprocess runs with hooks disabled, so it will not re-intercept and route
  its own subagents.
