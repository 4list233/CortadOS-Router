<!-- PURPOSE: Record inspected owners and reuse decisions before building the standalone MCP worker.
     CONTRACT: Evidence and proposals only; not a product ruling or runtime configuration.
     DEPENDS: SUBAGENT-MCP-PLAN.md and the approved CortadOS engineering standards. -->
# Existing owners and proposed disposition

Inspected 2026-10-15. Router baseline: `39df4854de213313b929caa4c8230c57e65457d7`, branch
`plan/subagent-mcp`. Official Codex plugin inspected from the installed, read-only version `1.0.6`.
Line references describe that baseline, not future candidates.

Searches executed from the Router checkout:

```sh
rg --files cortados_router integrations tests docs
rg -n '^def |^class |context|capabil|scarcity|allowed_lanes' cortados_router/core.py cortados_router/execution_routing.py cortados_router/scarcity.py cortados_router/transport.py
rg -n 'max_length|6000|task_context_contract|CONTRACT' s1/serve.py s1/student.py
```

The checkout has no MCP worker lifecycle implementation. `integrations/hermes/` is the only integration.
No new selection algorithm is warranted.

| Responsibility | Existing owner and evidence | Disposition |
|---|---|---|
| S1 scoring | `cortados_router/core.py:168` (`decide_s1`); `core.py:210` (`decide`) | **Adopt.** The MCP adapter supplies an explicit selection brief. It must not implement another scorer. |
| Candidate ranking and fit | `cortados_router/core.py:408` (`resolve`) | **Adopt.** Invoke with an explicit Codex route allowlist, `execution=True`, and a measured/admitted input envelope. No second quality or scarcity formula. |
| Hermes request projection | `cortados_router/execution_routing.py:56` (`scoring_input`) | **Keep.** It intentionally clips the advisory projection; the new public request must not inherit that clipping silently. |
| Execution integration | `cortados_router/execution_routing.py:78` (`select`) | **Extend through a separate host-neutral adapter.** Existing API has face/session and credential-resolver concerns; keep old callers and tests intact. |
| Route identity/logging | `cortados_router/core.py:679` (`log_route`) | **Adopt schema and owner.** Use the separately configured MCP state home; do not write the live CortadOS database by default. Log `platform: mcp:<client>` and selection mode. |
| Task-context wire type | `cortados_router/task_context.py:26` (`validate_context`) | **Keep.** This existing typed snapshot is not arbitrary Claude conversation history. The initial plain selection brief does not claim typed acknowledgement. |
| Quota measurement | `cortados_router/scarcity.py:325` (`parse_codex_status`), `:361` (`_codex`), `:568` (`lane_scarcity`) | **Extend with a typed native source.** `scarcity_codex_native.py` consumes captured native app-server windows and calls existing `window_view`/`finish`; TUI/Hermes remain unchanged. No duplicate scarcity formula or inferred balance. |
| Native Codex execution/auth | Installed Codex CLI `0.162.1`; native saved account | **Adopt.** No OAuth token extraction, custom subscription proxy, or API fallback. Native harness compatibility needs real captures. |
| MCP protocol | Maintained official MCP SDK | **Adopt after pinning.** Protocol version/dependency pin and independent client proof belong in the packaging evidence. Do not create a private JSON-RPC variant. |
| Job lifecycle | No Router implementation; official plugin evaluated below | **Build-new narrow job owner.** Inspected plugin storage and permission gaps justify a separate transactional lifecycle around the adopted native harness; no installed plugin change. |

`core.py` is already over the guidance cap. It must receive only call-site-sized edits under an agreed
split, or remain unchanged while a new bounded adapter calls its existing interfaces. All new modules
must stay within the adopted 500-line soft / 600-line hard source guidance. Contract files have no
user-imposed line cap.

## Official Codex plugin / Claudex finding

The official [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc) already provides
Claude-to-Codex tasks, status, results and cancellation through native `codex app-server`.
The [Claudex Stereo](https://github.com/vsladkov/claudex-stereo) project extends that relationship.
The official plugin is a relevant reuse candidate missed by the first comparison; it is not an inference
router and does not replace CortadOS S1.

The locally installed code was inspected at:

`~/.codex/plugins/cache/openai-codex/codex/1.0.6/scripts/`.

| Evidence | Consequence for unchanged adoption |
|---|---|
| `codex-companion.mjs:78` prints task/background/status/result/cancel commands. | Existing task UI and transport concepts can be adopted; no reason to re-create a complete coding harness. |
| `lib/app-server.mjs:190` spawns `codex app-server` using the inherited environment and no config-isolation flags. | It does not establish per-job MCP allowlists or subscription-only provider isolation. |
| `lib/codex.mjs:63` builds thread params from cwd/model/approvalPolicy/sandbox/serviceName/ephemeral. | Per-job selected connector sets are not an exposed seam in this inspected implementation. |
| `lib/workspace.mjs:3` resolves a Git root or returns cwd. | It does not allocate isolated worktrees or serialize writers; it must not be described as filesystem isolation. |
| `lib/state.mjs:58` returns an empty default state when JSON parsing fails. | Corrupt durable state becomes invisible; this conflicts with fail-loudly integrity requirements. |
| `lib/state.mjs:80` prunes to `MAX_JOBS=50`; `:105` deletes excluded results/logs. | Retention is an unapproved observable data-loss policy for this service. |
| `lib/state.mjs:114` writes the state file directly; `:118` uses unlocked read-modify-write. | Concurrent processes and partial writes do not provide the proposed single-writer transactional identity invariant. |
| `codex-companion.mjs:688` starts the background child before `:697` writes its queued request. | The start/read race needs a discriminating exam; unchanged adoption cannot claim write-before-start recovery. |
| `lib/tracked-jobs.mjs:143` maps zero execution exit status to completed. | This service needs its own externally verified terminal receipt definition. |
| `codex-companion.mjs:963` cancellation interrupts/kills and then records cancelled. | A terminal state needs observed process termination and a cancellation-race test before acceptance. |

These findings do not prove the plugin fails in ordinary use. They show why its convenience lifecycle
cannot be claimed to satisfy stronger isolation and persistence guarantees without extension.
The review considered these concrete choices:

1. **Extend an upstream/pinned plugin fork**, adding transactional lifecycle ownership and scoped native
   configuration. Preserve its CLI APIs and migrate its state explicitly.
2. **Build a narrow Router job owner around native Codex**, retaining Codex as the harness and declining to
   import the plugin's independent state/broker implementation. Existing plugin users remain untouched;
   nothing is retired or migrated because Router has no previous job store.

The selected disposition is option 2: a narrow Router job owner. Neither option permits copying the
plugin's weak persistence implementation under a new helper name. Independent D/A and the protected
exam precede production dispatch.

## Context and connector boundaries

- `core.decide_s1` sends at most 6,000 request characters; `s1/serve.py:41` also clips at that boundary.
  `task_context.MAX_CONTEXT_CHARS` independently bounds serialized typed snapshots to 6,000 characters.
  These are wire limits, not proof of an unlimited model context window.
- `execution_routing.scoring_input` projects roughly 3,500 task characters, 1,700 snapshot characters and
  500 recall characters. It preserves the execution prompt separately but is not the proposed MCP
  no-silent-truncation interface.
- S1 has a fresh scoring context per request. No inspected code establishes persistent router dialogue.
- A parent can send selected context and desired tools in a task. A native Codex worker cannot inherit
  an in-memory Claude connection or use Claude-only tools merely because the parent has them.
- An MCP endpoint may be configured in both clients when its transport and authentication work in both.
  The registry must distinguish the endpoint configuration from the per-job grant.
- Codex `--ignore-user-config` retains account authentication but does not, by itself, disable project
  `.codex/config.toml`. A project-config injection fixture is a prerequisite to any least-authority claim.
- Official Codex MCP configuration includes `required`, `enabled_tools`, stdio `env_vars`, and HTTP
  `bearer_token_env_var`. Source: [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
  and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
- MCP servers execute outside the Codex command sandbox in some configurations. File-write sandboxing
  must not be described as preventing remote MCP side effects. Named connector/tool grants are separate.

## Readiness constraints

D-0092 requires real redacted captures for every enabled model and output kind. Mock CLI streams add
fault coverage; they do not establish provider compatibility. Unknown usage is `null`, never zero.
A failed or stale quota probe must remain observable. Explicit selection does not establish model
quality and must not manufacture an S1 score. S1 failure must not silently become paid API execution.

## Native evidence amendment

Read-only `account/read`, `model/list`, and `account/rateLimits/read` captures from CLI 0.162.1 now
establish native ChatGPT login, seven listed models, and a typed general quota bucket. The schema says
`ordinaryUsageAllowed:null` is unavailable and must not be inferred from percentages/reset times.
Forest's latest instruction requires known quota for both explicit and S1 selection. Existing scarcity
math remains the owner; native values are only typed inputs. This standalone source does not change
Hermes' older PTY contract or repair its existing parser.

The new immutable resource snapshot for `core.resolve` prevents hidden `scarcity.current()` refreshes
or unit-cost reads from the live CortadOS state. Omitted snapshots preserve existing callers. The MCP
broker starts with isolated `CORTADOS_HOME` before Router imports; no module-global monkeypatch is used.

Real captures now show command/final/usage for Luna, Sol 6.1 and Astra, and an exact 65,536-byte mixed
ASCII/Unicode handoff boundary for each. This is a handoff byte bound, not a total context-window proof.
Native tool calls create independent process groups and sessions; killing only the Codex root leaves
the tool alive. SIGINT allows native cancellation, and descendant-before-root escalation proves the
owned child stops. Those observations require identity-tracked process containment.

The selected native MCP tool requires per-tool `approval_mode="approve"` under global `never`. Native
`enabled_tools` still exposes endpoint resources, as a real resource read proved. The new per-job SDK
grant proxy forwards only allowed tools; native Codex never receives upstream credentials. Both temp
exclusions are necessary for workspace-only writes. Whole-table TOML encoding is required for project
paths containing periods. Captured policy preflight includes layer sources and requirements; unknown
managed sources refuse rather than being assumed absent. These are captured near misses, not optional
cleanup or speculative hardening.
