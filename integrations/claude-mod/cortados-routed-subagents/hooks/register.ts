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
 *   - background (run_in_background): can outlive the subprocess's 10-minute ceiling, unless routeBackground is on.
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
  orchestratorAllowedTools: string
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
  // Orchestrator mode: the main session (the "brain") only plans and delegates; all actual work is delegated to
  // routed subagents. An ALLOWLIST (not a denylist) gates the main loop, so any mutating/executing tool — including
  // ones a denylist would miss (Monitor, memory_write, ExitWorktree, any mcp__ tool, future tools) — is refused
  // unless explicitly listed. Read-only + planning + delegation tools are allowed so the brain can plan (hybrid).
  orchestratorMode: true,
  orchestratorAllowedTools: 'Read,Grep,Glob,TodoWrite,Agent,AskUserQuestion,ExitPlanMode,Skill,ToolSearch',
}

// Delegation must never be gated off, whatever the allowlist config says, or the brain could not delegate at all.
const ALWAYS_ALLOWED = ['Agent']

/** The standing directive injected into the orchestrator's system prompt so it delegates by default, every session. */
const ORCHESTRATOR_DIRECTIVE =
  'You are operating as an ORCHESTRATOR — the planning "brain" of a routed system. You decide and delegate; you do ' +
  'not do the work yourself. Decompose the request into self-contained subtasks and hand EACH to a subagent via the ' +
  'Agent tool: those subagents are automatically routed to the CortadOS model pool and carry out the work on this ' +
  'machine. You may read to plan (Read, Grep, Glob) and keep a plan (TodoWrite), but you must not edit files, run ' +
  'shell commands, fetch the web, or otherwise act yourself — every such tool is refused on this (main) loop and ' +
  'must be delegated. Give each subagent a complete, standalone task (it cannot see this conversation or your ' +
  'context). When subagents report back, synthesize their results and plan the next step.'

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

export const register: Register = (on, options) => {
  const cfg: Cfg = { ...FALLBACK, ...(options as Partial<Cfg>) }

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
      '--strict-mcp-config',
      '--mcp-config', JSON.stringify({ mcpServers: {} }),
      '--output-format', 'json',
    ]

    $.ui.status(`↗ routed subagent (${description})`)
    try {
      const res: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean } = await $.process.run(
        [String(cfg.claudeBin), ...args],
        {
          // The child inherits the parent's environment. Under the desktop app that includes
          // CLAUDE_CODE_ENTRYPOINT=claude-desktop, which makes the CLI authenticate with the host's claude.ai OAuth
          // session and ignore ANTHROPIC_AUTH_TOKEN, so the provider sees an OAuth bearer it does not know and
          // answers 401 (found 2026-10-28 with a header sniffer). Forcing the entrypoint to `cli` makes the child
          // send the provider token. Blank any inherited ANTHROPIC_API_KEY so the child authenticates with the
          // routed token, not an API key the parent session happened to carry (the provider bridge strips it for
          // the same reason).
          env: {
            ANTHROPIC_BASE_URL: String(cfg.providerUrl),
            ANTHROPIC_AUTH_TOKEN: token,
            ANTHROPIC_API_KEY: '',
            CLAUDE_CODE_ENTRYPOINT: 'cli',
          },
          timeoutMs: 600000,
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

  // ---- Orchestrator mode: the main session only plans and delegates; routed subagents do the work. -----------
  if (cfg.orchestratorMode) {
    // An empty/malformed allowlist would lock out every tool (including Agent); resolveAllowSet drops malformed
    // tokens and falls back to the default set so the brain can always at least read and delegate.
    const allow: ReadonlySet<string> = resolveAllowSet(cfg.orchestratorAllowedTools)

    // Inject the standing directive so the brain delegates by default — no per-session instruction needed. Only on
    // the main render: a teammate render is a different in-process worker that keeps its tools and must not be told
    // to delegate everything.
    on('prompt.compose', async ($: any, e: any, next: any) => {
      const r = await next(e)
      if (Array.isArray(e?.traits) && e.traits.includes('teammate')) return r
      const section = { id: 'cortados-routed-subagents:orchestrator', text: ORCHESTRATOR_DIRECTIVE, scope: 'session' }
      return { sections: [...r.sections, section] }
    }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

    // Gate EVERY tool call on the main loop against the allowlist (a denylist would miss Monitor/memory_write/
    // ExitWorktree/mcp__ tools/future tools). Non-main loops and allowed tools pass straight through.
    on('tool.call', async ($: any, e: any, next: any) => {
      if (mainLoopAllows(String(e.tool), e.agentId, allow, true)) return next(e)
      return {
        deny:
          `Orchestrator mode: the brain plans and delegates — it does not run ${e.tool} itself. ` +
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
