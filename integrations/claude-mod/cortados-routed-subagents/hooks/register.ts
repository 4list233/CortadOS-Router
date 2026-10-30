/**
 * cortados-routed-subagents
 *
 * Every subagent the session spawns is intercepted at the Agent tool call and run instead as a headless `claude`
 * subprocess ON THIS MACHINE, pointed at the CortadOS provider (ANTHROPIC_BASE_URL). The provider scores the task
 * with S1 and runs the chosen model headless on the provider host; that model's tool calls are bridged back and
 * executed by this subprocess, here, against this machine's files. The main session keeps its own direct model.
 *
 * Claude Code has no per-subagent provider endpoint (an agent.spawn hook or AgentSpec can set model/tools/mode but
 * not a base URL), so a subprocess is the only way to route a subagent while the parent stays direct.
 *
 * Passes through to the native in-process subagent when the work cannot or should not be routed:
 *   - fork: inherits the parent's context and model; it cannot run on another provider.
 *   - isolation (worktree/remote): the native runner owns the worktree/remote semantics; routing locally would
 *     silently drop them and let parallel isolated agents clobber one another.
 *   - a named agent: the parent registers it for SendMessage; a subprocess cannot join that registry.
 *   - background (run_in_background): can outlive the subprocess timeout (subagentTimeoutMs), unless routeBackground is on.
 *   - no provider token available: better a working native subagent than a failed call.
 *
 * Once the child has been launched it has already acted, so the hook NEVER falls back to native after that point
 * (that would re-run non-idempotent work). Any failure past the launch becomes a clean `deny`, not a re-dispatch.
 */
import type { Register } from 'claude-code'

type Cfg = {
  providerUrl: string
  providerTokenFile: string
  providerToken: string
  model: string
  permissionMode: string
  allowedTools: string
  claudeBin: string
  routeBackground: boolean
  orchestratorMode: boolean
  orchestratorEnforce: boolean
  orchestratorAllowedTools: string
  subagentTimeoutMs: number
}

const FALLBACK: Cfg = {
  providerUrl: 'http://127.0.0.1:8432',
  providerTokenFile: '~/.config/cortados/provider-token',
  providerToken: '',
  model: 'cortados',
  permissionMode: 'bypassPermissions',
  allowedTools: 'Read,Write,Edit,MultiEdit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite,NotebookEdit',
  claudeBin: 'claude',
  routeBackground: false,
  // Orchestrator mode: the main session (the "brain") is DIRECTED to plan and delegate work to routed subagents,
  // via a standing system-prompt directive. By default this is GUIDANCE, not a cage: the brain keeps its tools and
  // may act directly when that is clearly the sensible choice (a trivial edit, its own bookkeeping) — delegation is
  // the strong default, not an absolute rule. Turn on `orchestratorEnforce` to additionally HARD-BLOCK every tool
  // outside `orchestratorAllowedTools` on the main loop (an allowlist, so even Monitor / memory_write / mcp__ tools /
  // future tools are refused) for a strict brain that cannot act even if it tried.
  orchestratorMode: true,
  orchestratorEnforce: false,
  // route_task is a delegation primitive too (it hands a self-contained text subtask to the router and returns the
  // answer), so it belongs with Agent in the brain's allowed set: a lightweight text delegation should not have to pay
  // for a full routed-subagent subprocess. Listed here for visibility; ALWAYS_ALLOWED guarantees it regardless of config.
  orchestratorAllowedTools: 'Read,Grep,Glob,TodoWrite,Agent,mcp__cortados-route__route_task,AskUserQuestion,ExitPlanMode,Skill,ToolSearch',
  // How long a routed subagent subprocess may run before it is aborted. 30 min by default (was a hard 600 s, which
  // killed long legitimate routed work such as an adversarial code review). Background routed work is separate.
  subagentTimeoutMs: 1_800_000,
}

// Delegation must never be gated off, whatever the allowlist config says, or the brain could not delegate at all.
// Both the Agent tool and the route_task MCP tool are delegation primitives.
const ALWAYS_ALLOWED = ['Agent', 'mcp__cortados-route__route_task']

/** The standing directive injected into the orchestrator's system prompt so it delegates by default, every session. */
const ORCHESTRATOR_DIRECTIVE =
  'You are operating as an ORCHESTRATOR — the planning "brain" of a routed system. Your job is to decide and ' +
  'DELEGATE: decompose the request into self-contained subtasks and hand EACH to a subagent via the Agent tool (or a ' +
  'self-contained text subtask via route_task). Those subagents are automatically routed to the CortadOS model pool ' +
  'and carry out the work on this machine, so pushing work to them — instead of doing it yourself — is the whole ' +
  'point of this mode: it keeps the expensive brain model free and runs each piece on the cheapest capable model. ' +
  'DELEGATE BY DEFAULT for anything substantive: editing or writing files beyond a trivial touch, running commands, ' +
  'building, broad searching, web access, long analyses. You MAY act directly when delegating would clearly be ' +
  'wasteful or slower — a one-line edit, your own planning and bookkeeping, a quick read or check — but treat that ' +
  'as the exception, and never let it become a habit of quietly doing the work yourself. Give each subagent a ' +
  'complete, standalone task (it cannot see this conversation or your context). When subagents report back, ' +
  'synthesize their results and plan the next step.'

/** Split a comma/space-separated tool list into clean names. */
export function parseList(s: string): string[] {
  return String(s)
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean)
}

/** A well-formed tool name: built-ins are PascalCase alphanumerics, MCP tools are mcp__server__tool — both are
 * [A-Za-z0-9_] only. Anything else (brackets, pipes, punctuation from a fat-fingered config) is not a tool name. */
export function validToolName(s: string): boolean {
  return /^[A-Za-z0-9_]+$/.test(s)
}

/** Build the main-loop allowlist from config: drop malformed tokens, and fall back to the default set when nothing
 * valid remains (so a mistyped value can never silently block reading and delegation). */
export function resolveAllowSet(configStr: string): Set<string> {
  const parsed = parseList(configStr).filter(validToolName)
  return new Set(parsed.length ? parsed : parseList(FALLBACK.orchestratorAllowedTools))
}

/** Whether a tool call may run on the MAIN loop under orchestrator mode. True = allow, false = refuse (delegate).
 * Off-mode and any non-main loop (agentId present) always allow; Agent is always allowed so delegation works. */
export function mainLoopAllows(tool: string, agentId: string | null | undefined, allow: ReadonlySet<string>, orchestratorMode: boolean): boolean {
  if (!orchestratorMode) return true
  if (agentId != null) return true // not the main loop (subagent/teammate/engine fork)
  return ALWAYS_ALLOWED.includes(tool) || allow.has(tool)
}

/** The last brace-delimited line of `claude -p --output-format json` output (warnings may precede it). */
export function lastJsonLine(stdout: string): string | null {
  const lines = stdout.split('\n').map((l) => l.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].startsWith('{') && lines[i].endsWith('}')) return lines[i]
  }
  return null
}

export type ParsedRun = {
  text: string | null
  model: string | null
  isError: boolean
  usage: Record<string, unknown>
  totalTokens: number
  totalToolUseCount: number
  totalDurationMs: number
  sessionId: string | null
}

/** Pull result text, the model that ran, and the counters the Agent tool's result schema wants, out of a
 * `claude -p --output-format json` reply. */
export function parseResult(stdout: string): ParsedRun {
  const empty: ParsedRun = { text: null, model: null, isError: false, usage: {}, totalTokens: 0, totalToolUseCount: 0, totalDurationMs: 0, sessionId: null }
  const line = lastJsonLine(stdout)
  if (!line) return empty
  try {
    const d = JSON.parse(line) as Record<string, any>
    // Only finite numbers; Infinity/NaN (e.g. JSON 1e400) would be rejected by the CLI's numeric validator.
    const n = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
    const usage = d.usage && typeof d.usage === 'object' ? (d.usage as Record<string, unknown>) : {}
    const u = usage as Record<string, unknown>
    const sum = n(u.input_tokens) + n(u.output_tokens) + n(u.cache_read_input_tokens) + n(u.cache_creation_input_tokens)
    const totalTokens = Number.isFinite(sum) ? sum : 0 // guard overflow to Infinity from huge finite terms
    const mu = d.modelUsage && typeof d.modelUsage === 'object' ? Object.keys(d.modelUsage)[0] : undefined
    return {
      text: typeof d.result === 'string' ? d.result : null,
      model: (typeof d.model === 'string' && d.model) || mu || null,
      isError: Boolean(d.is_error),
      usage,
      totalTokens,
      totalToolUseCount: n(d.num_turns),
      totalDurationMs: n(d.duration_ms),
      sessionId: typeof d.session_id === 'string' ? d.session_id : null,
    }
  } catch {
    return empty
  }
}

/** A completed routed run only when the child exited cleanly AND produced usable text: nonzero exit, a terminal
 * error, truncated stdout, or empty text all mean "not clean", so the caller refuses instead of returning a
 * result (which, for a child that already acted, must never fall back to a native re-run). The usage object is
 * not gated here because normalizeUsage() always builds a schema-valid one. */
export function cleanCompletion(exitCode: number, isStdoutTruncated: boolean, p: ParsedRun): boolean {
  return exitCode === 0 && !p.isError && !isStdoutTruncated && typeof p.text === 'string' && p.text.length > 0
}

/** The Agent result's `usage` object, every required key present (nullable ones may be null). */
export type AgentUsage = {
  input_tokens: number
  output_tokens: number
  cache_creation_input_tokens: number | null
  cache_read_input_tokens: number | null
  server_tool_use: { web_search_requests: number; web_fetch_requests: number } | null
  service_tier: string | null
  cache_creation: { ephemeral_1h_input_tokens: number; ephemeral_5m_input_tokens: number } | null
}

/** Coerce whatever the child reported into the exact usage shape the Agent result schema requires, so a completed
 * result is always accepted by core (a rejected result would be dropped and the task re-run natively). */
export function normalizeUsage(raw: unknown): AgentUsage {
  // Finite only: a non-finite number (Infinity/NaN) in the result's usage is rejected by the CLI's validator.
  const num = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
  const numOrNull = (x: unknown): number | null => (typeof x === 'number' && Number.isFinite(x) ? x : null)
  const obj = (x: unknown): Record<string, unknown> | null =>
    x && typeof x === 'object' && !Array.isArray(x) ? (x as Record<string, unknown>) : null
  const u = obj(raw) ?? {}
  const stu = obj(u.server_tool_use)
  const cc = obj(u.cache_creation)
  return {
    input_tokens: num(u.input_tokens),
    output_tokens: num(u.output_tokens),
    cache_creation_input_tokens: numOrNull(u.cache_creation_input_tokens),
    cache_read_input_tokens: numOrNull(u.cache_read_input_tokens),
    server_tool_use: stu
      ? { web_search_requests: num(stu.web_search_requests), web_fetch_requests: num(stu.web_fetch_requests) }
      : null,
    service_tier: typeof u.service_tier === 'string' ? u.service_tier : null,
    cache_creation: cc
      ? { ephemeral_1h_input_tokens: num(cc.ephemeral_1h_input_tokens), ephemeral_5m_input_tokens: num(cc.ephemeral_5m_input_tokens) }
      : null,
  }
}

/** Replace the provider token with *** anywhere it appears in model-visible text. Any non-empty token is scrubbed
 * regardless of length — a configured credential is redacted even if short; mangling a coincidental word is an
 * acceptable price for never surfacing the secret. (An empty token is a no-op; split('') would shatter the string.) */
export function scrubToken(s: string, token: string): string {
  return token ? s.split(token).join('***') : s
}

let routedSeq = 0

/** Read the provider token: the inline override if set, otherwise the token file (with ~ expanded to $HOME). */
async function resolveToken($: any, cfg: Cfg): Promise<string> {
  if (cfg.providerToken && String(cfg.providerToken).trim()) return String(cfg.providerToken).trim()
  let path = String(cfg.providerTokenFile)
  if (path.startsWith('~')) {
    const home = await $.process.run(['sh', '-c', 'printf %s "$HOME"'])
    path = String(home.stdout) + path.slice(1)
  }
  return (await $.fs.read(path)).trim()
}

/** A positive, finite subagent timeout from config, else the default (a mistyped/negative value never disables the cap). */
export function resolveTimeoutMs(v: unknown): number {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : FALLBACK.subagentTimeoutMs
}

export const register: Register = (on, options) => {
  const cfg: Cfg = { ...FALLBACK, ...(options as Partial<Cfg>) }
  const subagentTimeoutMs = resolveTimeoutMs(cfg.subagentTimeoutMs)

  on('tool.call', { tool: 'Agent' }, async ($: any, e: any, next: any) => {
    const subagentType = String(e.subagent_type ?? '')
    const background = Boolean(e.run_in_background)
    const prompt = String(e.prompt ?? '')
    const description = String(e.description ?? 'routed subagent')

    // Pass through BEFORE launching anything — these cannot or should not be routed. After the launch below the
    // hook never returns next(e), because the child will already have acted and native would re-run it.
    if (subagentType === 'fork' || !prompt.trim()) return next(e)
    if (background && !cfg.routeBackground) return next(e)
    if (e.isolation) return next(e) // worktree/remote: let the native runner own isolation
    if (e.name) return next(e) // a named agent stays addressable by SendMessage only if the parent spawns it

    let token: string
    try {
      token = await resolveToken($, cfg)
    } catch (err) {
      $.ui.toast(`routed-subagents: no provider token (${scrubToken(String(err), '')}); running natively`)
      return next(e)
    }
    if (!token) return next(e)

    // --tools bounds the built-in tools the child actually has; --allowedTools grants them (needed under non-bypass
    // modes). --settings disableAllHooks stops THIS mod from firing inside the child and routing recursively.
    // --strict-mcp-config + an empty --mcp-config start the child with NO MCP servers: otherwise it inherits the
    // user's MCP servers and advertises their tools to the provider, and a Codex lane refuses a thread whose dynamic
    // tools include a reserved name (seen 2026-10-28: "dynamic tool name is reserved: mcp__cortados-route__route_task",
    // code -32600). Routed subagents are built-in-tools-only by design (README).
    const tools = String(cfg.allowedTools)
    const args = [
      '-p', prompt,
      '--model', String(cfg.model),
      '--permission-mode', String(cfg.permissionMode),
      '--tools', tools,
      '--allowedTools', tools,
      '--settings', JSON.stringify({ disableAllHooks: true }),
      // Load NO user/project/local settings, as the provider's own bridge does: otherwise a settings `env` block
      // could restore a conflicting ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL and override the launch env below, sending
      // the provider token to the wrong endpoint or a stale key to the provider (Codex review, 2026-10-28).
      // Known residual (defense-in-depth): a managed-settings `env` block or a legacy global `env` in ~/.claude.json
      // can still apply over the launch env even with no setting sources. Fully closing that needs an isolated
      // CLAUDE_CONFIG_DIR; it does not apply to a fleet without managed settings or a global env block.
      '--setting-sources', '',
      '--strict-mcp-config',
      '--mcp-config', JSON.stringify({ mcpServers: {} }),
      '--output-format', 'json',
    ]

    $.ui.status(`↗ routed subagent (${description})`)
    try {
      const res: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean } = await $.process.run(
        [String(cfg.claudeBin), ...args],
        {
          // The child inherits the parent's environment and credentials. Two things there can make it send the
          // wrong token to the provider and get a 401:
          //   1. Under the desktop app, CLAUDE_CODE_ENTRYPOINT=claude-desktop makes the CLI authenticate with the
          //      host's claude.ai OAuth session and ignore the env token (found 2026-10-28 with a header sniffer).
          //      Forcing the entrypoint to `cli` stops that.
          //   2. The CLI resolves credentials over BOTH channels — x-api-key (ANTHROPIC_API_KEY, a saved Console
          //      key, or apiKeyHelper output) and bearer (ANTHROPIC_AUTH_TOKEN). An empty ANTHROPIC_API_KEY still
          //      lets a saved key / apiKeyHelper load and be sent (Codex review, 2026-10-28). So set BOTH channels
          //      to the provider token: whichever the CLI selects, it is the provider token, and no inherited key
          //      leaks. The child only ever contacts the provider (ANTHROPIC_BASE_URL pins it), never the real API.
          // Neutralize inherited vars that could override or redirect the child regardless of the above: a custom
          // x-api-key header (ANTHROPIC_CUSTOM_HEADERS) is merged after the SDK credentials and would replace the
          // provider key, and the Bedrock/Vertex selectors would send the turn to another transport entirely
          // (Codex review, 2026-10-28). $.process.run only ADDS over the host env, so blanking is how these are cleared.
          // ANTHROPIC_CUSTOM_HEADERS is set rather than blanked: it carries only `x-cortados-reveal: 1`, which asks the
          // provider to name the real routed model and its lane's remaining % in `model` (the label below), and as
          // the whole value it still replaces any inherited custom headers, so no x-api-key override survives.
          env: {
            ANTHROPIC_BASE_URL: String(cfg.providerUrl),
            ANTHROPIC_API_KEY: token,
            ANTHROPIC_AUTH_TOKEN: token,
            CLAUDE_CODE_ENTRYPOINT: 'cli',
            ANTHROPIC_CUSTOM_HEADERS: 'x-cortados-reveal: 1',
            ANTHROPIC_UNIX_SOCKET: '',
            CLAUDE_CODE_USE_BEDROCK: '',
            CLAUDE_CODE_USE_VERTEX: '',
            CLAUDE_CODE_USE_FOUNDRY: '',
            CLAUDE_CODE_USE_MANTLE: '',
            CLAUDE_CODE_USE_ANTHROPIC_AWS: '',
            CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD: '',
          },
          timeoutMs: subagentTimeoutMs,
        },
      )
      const p = parseResult(res.stdout)
      if (!cleanCompletion(res.exitCode, res.isStdoutTruncated, p)) {
        // The child may already have acted; refuse rather than re-dispatch natively (which would replay the work).
        const why = `exit ${res.exitCode}${p.isError ? ', error' : ''}${res.isStdoutTruncated ? ', truncated' : ''}`
        // Surface only the parsed result text (a complete JSON field — never cut mid-token), scrubbed. Raw stdout/
        // stderr are NOT surfaced: the CLI truncates them at a byte boundary that could split a token past the
        // exact-match scrubber. They remain in the child's on-disk logs for debugging.
        const detail = typeof p.text === 'string' && p.text ? scrubToken(p.text, token).slice(-500) : '(no parseable output)'
        return { deny: `routed subagent did not complete cleanly (${why}): ${detail}` }
      }
      const header = p.model ? `[routed via CortadOS → ${p.model}]\n\n` : ''
      // Match the Agent tool's "completed" result shape so core accepts it in place of a native subagent.
      return {
        result: {
          content: [{ type: 'text', text: scrubToken(header + (p.text as string), token) }],
          agentId: scrubToken(p.sessionId || `routed-${(routedSeq += 1)}`, token),
          status: 'completed',
          prompt,
          totalToolUseCount: p.totalToolUseCount,
          totalDurationMs: p.totalDurationMs,
          totalTokens: p.totalTokens,
          usage: normalizeUsage(p.usage),
          // This routed child has no entry in the parent's agent registry, so it cannot be continued by SendMessage.
          canContinueAgent: false,
        },
      }
    } catch (err) {
      // Launch/runtime failure: the child may have started, so refuse cleanly instead of re-dispatching natively.
      return { deny: scrubToken(`routed subagent failed: ${err}`, token) }
    } finally {
      // Guarded: a throw here would escape to the outer .catch and trigger a native re-dispatch (replay). Never
      // let clearing the status line do that.
      try { $.ui.status(undefined) } catch { /* ignore */ }
    }
  }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

  // ---- Orchestrator mode: direct the main session to plan and delegate; routed subagents do the work. ----------
  if (cfg.orchestratorMode) {
    // Inject the standing directive so the brain delegates by default — no per-session instruction needed. Only on
    // the main render: a teammate render is a different in-process worker that keeps its tools and must not be told
    // to delegate everything. This directive is the GUIDANCE; it is always on under orchestratorMode.
    on('prompt.compose', async ($: any, e: any, next: any) => {
      const r = await next(e)
      if (Array.isArray(e?.traits) && e.traits.includes('teammate')) return r
      const section = { id: 'cortados-routed-subagents:orchestrator', text: ORCHESTRATOR_DIRECTIVE, scope: 'session' }
      return { sections: [...r.sections, section] }
    }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

    // HARD ENFORCEMENT is opt-in (orchestratorEnforce). By default the directive above is trusted and the brain keeps
    // full tool access — it may act directly when that is clearly sensible. With enforce on, EVERY tool call on the
    // main loop is gated against the allowlist (an allowlist, not a denylist, so Monitor/memory_write/ExitWorktree/
    // mcp__ tools/future tools are refused unless listed). Non-main loops and allowed tools pass straight through.
    if (cfg.orchestratorEnforce) {
      // An empty/malformed allowlist would lock out every tool (including Agent); resolveAllowSet drops malformed
      // tokens and falls back to the default set so the brain can always at least read and delegate.
      const allow: ReadonlySet<string> = resolveAllowSet(cfg.orchestratorAllowedTools)
      on('tool.call', async ($: any, e: any, next: any) => {
        if (mainLoopAllows(String(e.tool), e.agentId, allow, true)) return next(e)
        return {
          deny:
            `Orchestrator mode (enforced): the brain plans and delegates — it does not run ${e.tool} itself. ` +
            `Spawn a subagent with the Agent tool (it is routed to the CortadOS pool and runs the work here) ` +
            `instead of calling ${e.tool} directly.`,
        }
        // Fail CLOSED for work tools: if the gate throws before deciding, refuse a non-allowed tool (never let it run
        // on a guard failure) but still let an allowed tool (Agent/Read/...) through so the session is not bricked.
      }).catch(($: any, e: any, next: any) =>
        next.called
          ? undefined
          : mainLoopAllows(String(e.tool), e.agentId, allow, true)
            ? next(e)
            : { deny: 'Orchestrator mode gate failed; refusing this tool. Delegate it to a subagent.' },
      )
    }
  }
}
