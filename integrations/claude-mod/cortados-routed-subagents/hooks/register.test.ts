import { test, expect } from 'claude-code/testing'
import { lastJsonLine, parseResult, cleanCompletion, scrubToken, normalizeUsage, parseList, mainLoopAllows, validToolName, resolveAllowSet, resolveTimeoutMs, register } from './register'

test('validToolName accepts built-in and MCP names, rejects punctuation', async () => {
  expect(validToolName('Read')).toBe(true)
  expect(validToolName('mcp__server__tool')).toBe(true)
  expect(validToolName('[Read')).toBe(false)
  expect(validToolName('Bash!')).toBe(false)
  expect(validToolName('Read|Grep')).toBe(false)
})

test('resolveAllowSet drops malformed tokens and falls back to defaults when empty', async () => {
  const a = resolveAllowSet('Read, Glob, Bash!')
  expect(a.has('Read')).toBe(true)
  expect(a.has('Glob')).toBe(true)
  expect(a.has('Bash!')).toBe(false)
  // all-malformed -> fallback to the default set (so Read/Grep/Agent still work)
  const fb = resolveAllowSet('[Read,Grep]')
  expect(fb.has('Read')).toBe(true)
  expect(fb.has('Grep')).toBe(true)
  expect(fb.has('Agent')).toBe(true)
  // whitespace-only -> fallback
  expect(resolveAllowSet('   ').has('Agent')).toBe(true)
})

test('parseList splits on commas and whitespace and drops empties', async () => {
  expect(parseList('Write,Edit, Bash')).toEqual(['Write', 'Edit', 'Bash'])
  expect(parseList('Write,,  Edit ,')).toEqual(['Write', 'Edit'])
  expect(parseList('  Bash  ')).toEqual(['Bash'])
  expect(parseList('')).toEqual([])
})

test('mainLoopAllows gates only the main loop, allowlist-style, with Agent always allowed', async () => {
  const allow = new Set(['Read', 'Grep', 'TodoWrite'])
  // main loop (no agentId): allowlisted -> allow; non-listed work tool -> deny
  expect(mainLoopAllows('Read', undefined, allow, true)).toBe(true)
  expect(mainLoopAllows('Bash', undefined, allow, true)).toBe(false)
  expect(mainLoopAllows('Monitor', undefined, allow, true)).toBe(false) // denylist would have missed this
  expect(mainLoopAllows('mcp__x__write', undefined, allow, true)).toBe(false) // and this
  // Agent is always allowed so delegation is never locked out, even if absent from the allowlist
  expect(mainLoopAllows('Agent', undefined, allow, true)).toBe(true)
  // a non-main loop (agentId present) is never gated
  expect(mainLoopAllows('Bash', 'agent-123', allow, true)).toBe(true)
  // mode off -> everything allowed
  expect(mainLoopAllows('Bash', undefined, allow, false)).toBe(true)
})

test('route_task is a delegation primitive, always allowed on the brain even if not in the allowlist', async () => {
  const allow = new Set(['Read', 'Grep'])                       // route_task deliberately absent from this set
  expect(mainLoopAllows('mcp__cortados-route__route_task', undefined, allow, true)).toBe(true)
  // the default allow-set also lists it, so a default config advertises it
  expect(resolveAllowSet('').has('mcp__cortados-route__route_task')).toBe(true)
})

test('host-app plumbing: only the exact desktop turn tools pass an enforced gate', async () => {
  const allow = new Set(['Read'])
  expect(mainLoopAllows('mcp__ccd_turn__update_plan', undefined, allow, true)).toBe(true)
  expect(mainLoopAllows('mcp__ccd_turn__wrap_up', undefined, allow, true)).toBe(true)
  expect(mainLoopAllows('mcp__ccd_session__mark_chapter', undefined, allow, true)).toBe(true)
  // a similarly named server does NOT ride the ccd_ name past the gate (exact names, not a prefix)
  expect(mainLoopAllows('mcp__ccd_external__execute_shell', undefined, allow, true)).toBe(false)
  expect(mainLoopAllows('mcp__ccd_session_mgmt__send_message', undefined, allow, true)).toBe(false)
  // a real work MCP tool is still refused under enforcement
  expect(mainLoopAllows('mcp__claude-in-chrome__tabs_context_mcp', undefined, allow, true)).toBe(false)
})

test('resolveTimeoutMs keeps the timeout an integer inside the host range 1..600000', async () => {
  expect(resolveTimeoutMs(undefined)).toBe(600_000)            // the default is the host's cap
  expect(resolveTimeoutMs(300_000)).toBe(300_000)
  expect(resolveTimeoutMs('120000')).toBe(120_000)             // JSON config may deliver a string
  expect(resolveTimeoutMs(1)).toBe(1)
  expect(resolveTimeoutMs(1_800_000)).toBe(600_000)            // above the host's cap -> clamped, never rejected
  expect(resolveTimeoutMs(Infinity)).toBe(600_000)
  expect(resolveTimeoutMs(1234.9)).toBe(1234)                  // fractional -> integer
  expect(Number.isInteger(resolveTimeoutMs(0.5))).toBe(true)
  expect(resolveTimeoutMs(0.5)).toBe(1)                        // floors to 0, clamped up into range
  for (const bad of [0, -5, -Infinity, NaN, null, 'soon', '', {}, [], true]) {
    expect(resolveTimeoutMs(bad)).toBe(600_000)                // never 0/negative/NaN/non-number -> default
  }
})

/** A $.process stub that validates timeoutMs the way the host does (Claude Code 2.1.314: integer in 1..600000). */
function hostLikeProcess(seen: { timeoutMs?: unknown }) {
  return {
    run: async (_argv: string[], opts: any) => {
      const t = opts?.timeoutMs
      seen.timeoutMs = t
      if (t !== undefined && !(Number.isInteger(t) && t >= 1 && t <= 600_000)) {
        throw new Error(`$.process.run: timeoutMs must be an integer from 1 to 600000 (got ${t})`)
      }
      return { exitCode: 0, stdout: JSON.stringify({ result: 'pong', model: 'm' }), stderr: '', isStdoutTruncated: false }
    },
  }
}

for (const [label, timeout, expected] of [
  ['the default timeout', undefined, 600_000],
  ['a configured 1_800_000 (clamped)', 1_800_000, 600_000],
  ['a fractional timeout', 90_000.7, 90_000],
] as const) {
  test(`a routed subagent launches under the host's timeout validator with ${label}`, async () => {
    let agentHandler: any = null
    const on = (_event: string, filterOrHandler: any, maybeHandler?: any) => {
      if (maybeHandler && filterOrHandler && filterOrHandler.tool === 'Agent') agentHandler = maybeHandler
      return { catch() {} }
    }
    const opts: any = { providerToken: 'tok', providerUrl: 'http://provider.test:8432', orchestratorMode: false }
    if (timeout !== undefined) opts.subagentTimeoutMs = timeout
    register(on as any, opts)
    const seen: { timeoutMs?: unknown } = {}
    const $ = { process: hostLikeProcess(seen), fs: { read: async () => 'unused' }, ui: { status() {}, toast() {} } }
    const out = await agentHandler($, { subagent_type: 'general-purpose', prompt: 'say pong', description: 'd' }, async () => 'native')
    expect(seen.timeoutMs).toBe(expected)
    expect(out.result.status).toBe('completed')
  })
}

test('lastJsonLine returns the final brace-delimited line past leading warnings', async () => {
  const s = [
    '⚠ claude.ai connectors are disabled ...',
    '[claude-code:unrecognized_model] {"model":"cortados"}',
    '{"result":"hi","session_id":"abc"}',
  ].join('\n')
  expect(lastJsonLine(s)).toBe('{"result":"hi","session_id":"abc"}')
  expect(lastJsonLine('no json at all')).toBe(null)
})

test('parseResult pulls text, model and the Agent-result counters from a claude -p json reply', async () => {
  const reply = JSON.stringify({
    result: '# Title',
    model: 'cortados',
    is_error: false,
    num_turns: 2,
    duration_ms: 1234,
    session_id: 'sess-1',
    usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 3, cache_creation_input_tokens: 0 },
  })
  const p = parseResult('oops a warning line\n' + reply)
  expect(p.text).toBe('# Title')
  expect(p.model).toBe('cortados')
  expect(p.totalTokens).toBe(18)
  expect(p.totalToolUseCount).toBe(2)
  expect(p.totalDurationMs).toBe(1234)
  expect(p.sessionId).toBe('sess-1')
  expect(p.isError).toBe(false)
})

test('parseResult falls back to modelUsage for the model and is safe on junk', async () => {
  const empty = parseResult('not json')
  expect(empty.text).toBe(null)
  expect(empty.totalTokens).toBe(0)
  const p = parseResult(JSON.stringify({ result: 'x', modelUsage: { 'gpt-5.6-luna': {} }, is_error: true }))
  expect(p.model).toBe('gpt-5.6-luna')
  expect(p.isError).toBe(true)
})

const good: Parameters<typeof cleanCompletion>[2] = {
  text: '# ok', model: 'cortados', isError: false, usage: { input_tokens: 1 },
  totalTokens: 1, totalToolUseCount: 0, totalDurationMs: 0, sessionId: 's',
}

test('cleanCompletion requires clean exit, no error, no truncation and real text', async () => {
  expect(cleanCompletion(0, false, good)).toBe(true)
  // nonzero exit, even with good text, is not a completion (no native replay, caller denies)
  expect(cleanCompletion(1, false, good)).toBe(false)
  // terminal error flag
  expect(cleanCompletion(0, false, { ...good, isError: true })).toBe(false)
  // truncated stdout
  expect(cleanCompletion(0, true, good)).toBe(false)
  // empty text
  expect(cleanCompletion(0, false, { ...good, text: '' })).toBe(false)
  // usage shape is NOT gated here (normalizeUsage fixes it), so a thin usage is still a clean completion
  expect(cleanCompletion(0, false, { ...good, usage: {} })).toBe(true)
})

test('normalizeUsage always yields every required key, coercing junk', async () => {
  const full = normalizeUsage({
    input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 3, cache_creation_input_tokens: 0,
    server_tool_use: { web_search_requests: 1, web_fetch_requests: 2 }, service_tier: 'standard',
    cache_creation: { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 4 },
  })
  expect(full.input_tokens).toBe(10)
  expect(full.server_tool_use).toEqual({ web_search_requests: 1, web_fetch_requests: 2 })
  expect(full.cache_creation).toEqual({ ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 4 })
  // junk / empty / array all produce a schema-valid object with nullable keys present
  for (const bad of [{}, [1], null, 'x', undefined]) {
    const u = normalizeUsage(bad)
    expect(u.input_tokens).toBe(0)
    expect(u.output_tokens).toBe(0)
    expect(u.cache_creation_input_tokens).toBe(null)
    expect(u.cache_read_input_tokens).toBe(null)
    expect(u.server_tool_use).toBe(null)
    expect(u.service_tier).toBe(null)
    expect(u.cache_creation).toBe(null)
    expect('server_tool_use' in u).toBe(true)
  }
  // non-finite numbers are coerced away (the CLI validator rejects Infinity/NaN)
  const nf = normalizeUsage({ input_tokens: Infinity, output_tokens: NaN, cache_read_input_tokens: -Infinity })
  expect(nf.input_tokens).toBe(0)
  expect(nf.output_tokens).toBe(0)
  expect(nf.cache_read_input_tokens).toBe(null)
})

test('parseResult keeps totalTokens finite even when terms overflow', async () => {
  const p = parseResult(JSON.stringify({ result: 'x', usage: { input_tokens: 1e308, output_tokens: 1e308 } }))
  expect(Number.isFinite(p.totalTokens)).toBe(true)
  expect(p.totalTokens).toBe(0) // 1e308 + 1e308 overflows to Infinity -> guarded to 0
})

test('scrubToken redacts any non-empty token everywhere; empty token is a no-op', async () => {
  const tok = 'abcdef0123456789abcdef0123456789'
  expect(scrubToken(`auth failed: ${tok} at line 3 (${tok})`, tok)).toBe('auth failed: *** at line 3 (***)')
  expect(scrubToken('nothing secret here', tok)).toBe('nothing secret here')
  // short tokens ARE now scrubbed (a configured credential is redacted regardless of length)
  expect(scrubToken('the word short appears', 'short')).toBe('the word *** appears')
  // empty token must never split the string into characters
  expect(scrubToken('anything', '')).toBe('anything')
})

test('the routed child is started with the provider credentials and CLAUDE_CODE_ENTRYPOINT=cli', async () => {
  // Capture the Agent tool.call handler the hook registers; orchestrator mode off keeps the registration minimal.
  let agentHandler: any = null
  const on = (_event: string, filterOrHandler: any, maybeHandler?: any) => {
    if (maybeHandler && filterOrHandler && filterOrHandler.tool === 'Agent') agentHandler = maybeHandler
    return { catch() {} }
  }
  register(on as any, { providerToken: 'tok', providerUrl: 'http://provider.test:8432', orchestratorMode: false } as any)
  expect(agentHandler).not.toBe(null)

  let recordedEnv: Record<string, string> | null = null
  let recordedArgv: string[] = []
  const $ = {
    process: {
      run: async (argv: string[], opts: any) => {
        recordedEnv = opts.env
        recordedArgv = argv
        return hostLikeProcess({}).run(argv, opts) // rejects an out-of-range timeoutMs like the host does
      },
    },
    fs: { read: async () => 'unused' },
    ui: { status() {}, toast() {} },
  }
  const next = async () => 'native'
  const out = await agentHandler($, { subagent_type: 'general-purpose', prompt: 'say pong', description: 'd' }, next)
  expect(out.result.status).toBe('completed')
  expect(recordedEnv).toEqual({
    ANTHROPIC_BASE_URL: 'http://provider.test:8432',
    ANTHROPIC_API_KEY: 'tok',
    ANTHROPIC_AUTH_TOKEN: 'tok',
    CLAUDE_CODE_ENTRYPOINT: 'cli',
    ANTHROPIC_CUSTOM_HEADERS: 'x-cortados-reveal: 1',
    ANTHROPIC_UNIX_SOCKET: '',
    CLAUDE_CODE_USE_BEDROCK: '',
    CLAUDE_CODE_USE_VERTEX: '',
    CLAUDE_CODE_USE_FOUNDRY: '',
    CLAUDE_CODE_USE_MANTLE: '',
    CLAUDE_CODE_USE_ANTHROPIC_AWS: '',
    CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD: '',
  })
  // The child loads no user/project settings, so a settings env block can't override the launch env above.
  expect(recordedArgv).toContain('--setting-sources')
  // The child starts with no MCP servers: built-in tools only, nothing of the parent's exposed to the provider.
  expect(recordedArgv).toContain('--strict-mcp-config')
  const mcpIdx = recordedArgv.indexOf('--mcp-config')
  expect(mcpIdx).toBeGreaterThan(-1)
  expect(recordedArgv[mcpIdx + 1]).toBe('{"mcpServers":{}}')
})
