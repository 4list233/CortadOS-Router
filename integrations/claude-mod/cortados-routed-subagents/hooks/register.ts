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
    const tools = String(cfg.allowedTools)
    const args = [
      '-p', prompt,
      '--model', String(cfg.model),
      '--permission-mode', String(cfg.permissionMode),
      '--tools', tools,
      '--allowedTools', tools,
      '--settings', JSON.stringify({ disableAllHooks: true }),
      '--output-format', 'json',
    ]

    $.ui.status(`↗ routed subagent (${description})`)
    try {
      const res: { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean } = await $.process.run(
        [String(cfg.claudeBin), ...args],
        {
          // Blank any inherited ANTHROPIC_API_KEY so the child authenticates with the routed token, not an API key
          // the parent session happened to carry (the provider bridge strips it for the same reason).
          env: { ANTHROPIC_BASE_URL: String(cfg.providerUrl), ANTHROPIC_AUTH_TOKEN: token, ANTHROPIC_API_KEY: '' },
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
}
