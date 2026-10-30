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
| `providerUrl` | `http://127.0.0.1:8432` | Provider base URL. Set to `http://<provider-host>:8432` for a remote provider. |
| `providerTokenFile` | `~/.config/cortados/provider-token` | File holding `CORTADOS_PROVIDER_TOKEN`, read fresh at spawn time so the token is never stored in settings. `~` expands to `$HOME`. |
| `providerToken` | *(unset)* | Optional inline token override. Prefer the file. |
| `model` | `cortados` | Model id sent to the provider. `cortados` lets S1 route; a concrete id pins one lane. |
| `permissionMode` | `bypassPermissions` | Headless permission mode for each routed subagent (see **Blast radius**). |
| `allowedTools` | `Read,Write,Edit,MultiEdit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite,NotebookEdit` | The built-in tools a routed subagent has **on this machine**. Passed as both `--tools` (availability — the real boundary, so a tool left out is genuinely absent even under `bypassPermissions`) and `--allowedTools` (permission). Covers built-in tools only; MCP tools are controlled separately. |
| `claudeBin` | `claude` | The `claude` executable used to run each routed subagent. |
| `routeBackground` | `false` | Whether to route background subagents too (off because a routed subagent runs under the `subagentTimeoutMs` ceiling). |
| `subagentTimeoutMs` | `600000` (10 min) | How long a routed subagent subprocess may run before it is aborted. The max is `600000` (10 min): that is the host's own cap (Claude Code validates `$.process.run`'s `timeoutMs` as an integer from 1 to 600000), so a larger value is clamped to it and a fractional one is rounded down. Lower it to fail fast; a non-positive/invalid value falls back to the default. |

Runs under the Claude desktop app as well as the CLI: the child is started with `CLAUDE_CODE_ENTRYPOINT=cli` so it authenticates with the provider token rather than the host's claude.ai session.
The child starts with an empty MCP config (built-in tools only); the parent's MCP servers are never exposed to the provider.
| `orchestratorMode` | `true` | Direct the main session to plan and delegate (see **Orchestrator mode**). Guidance by default; keeps full tool access. |
| `orchestratorEnforce` | `false` | Also **hard-block** every tool outside `orchestratorAllowedTools` on the main loop (a strict brain that cannot act even if it tried). Off by default — the directive is trusted. |
| `orchestratorAllowedTools` | `Read,Grep,Glob,TodoWrite,Agent,mcp__cortados-route__route_task,AskUserQuestion,ExitPlanMode,Skill,ToolSearch` | Allowlist used **only when `orchestratorEnforce` is on**: the tools the brain may run itself; everything else is refused and must be delegated. `Agent` and `route_task` (both delegation primitives) are always allowed regardless. |

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

## Notes

- Claude Code logs `unrecognized_model: cortados` and assumes a 200k context window for the routed subprocess.
  Cosmetic; the route still works.
- Recursion is prevented: each routed subprocess runs with hooks disabled, so it will not re-intercept and route
  its own subagents.
