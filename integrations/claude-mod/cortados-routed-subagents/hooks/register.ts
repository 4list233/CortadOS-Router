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
 *   - teammate / resume: long-lived, not a one-shot delegation.
 *   - background (run_in_background): can outlive the subprocess's 10-minute ceiling, unless routeBackground is on.
 *   - no provider token available: better a working native subagent than a failed call.
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
    const n = (x: unknown): number => (typeof x === 'number' ? x : 0)
    const usage = d.usage && typeof d.usage === 'object' ? (d.usage as Record<string, unknown>) : {}
    const u = usage as Record<string, unknown>
    const totalTokens = n(u.input_tokens) + n(u.output_tokens) + n(u.cache_read_input_tokens) + n(u.cache_creation_input_tokens)
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

    if (subagentType === 'fork' || !prompt.trim()) return next(e)
    if (background && !cfg.routeBackground) return next(e)

    let token: string
    try {
      token = await resolveToken($, cfg)
    } catch (err) {
      $.ui.toast(`routed-subagents: no provider token (${err}); running natively`)
      return next(e)
    }
    if (!token) return next(e)

    // --settings disableAllHooks stops THIS mod from firing inside the child and routing recursively.
    const args = [
      '-p', prompt,
      '--model', String(cfg.model),
      '--permission-mode', String(cfg.permissionMode),
      '--allowedTools', String(cfg.allowedTools),
      '--settings', JSON.stringify({ disableAllHooks: true }),
      '--output-format', 'json',
    ]

    $.ui.status(`↗ routed subagent (${description})`)
    let res: { exitCode: number; stdout: string; stderr: string }
    try {
      res = await $.process.run([String(cfg.claudeBin), ...args], {
        // Blank any inherited ANTHROPIC_API_KEY so the child authenticates with the routed token, not an API key
        // the parent session happened to carry (the provider bridge strips it for the same reason).
        env: { ANTHROPIC_BASE_URL: String(cfg.providerUrl), ANTHROPIC_AUTH_TOKEN: token, ANTHROPIC_API_KEY: '' },
        timeoutMs: 600000,
      })
    } catch (err) {
      $.ui.status(undefined)
      return { deny: `routed subagent failed to start: ${err}` }
    }
    $.ui.status(undefined)

    const p = parseResult(res.stdout)
    if (res.exitCode !== 0 && !p.text) {
      return { deny: `routed subagent exited ${res.exitCode}: ${(res.stderr || res.stdout).slice(-500)}` }
    }
    const header = p.model ? `[routed via CortadOS → ${p.model}]\n\n` : ''
    const body = p.text ?? res.stdout.trim()
    // Match the Agent tool's "completed" result shape so core accepts it in place of a native subagent.
    return {
      result: {
        content: [{ type: 'text', text: header + (body || '(the routed subagent returned no text)') }],
        agentId: p.sessionId || `routed-${(routedSeq += 1)}`,
        status: 'completed',
        prompt,
        totalToolUseCount: p.totalToolUseCount,
        totalDurationMs: p.totalDurationMs,
        totalTokens: p.totalTokens,
        usage: p.usage,
        ...(p.isError ? { isError: true } : {}),
      },
    }
  }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))
}
