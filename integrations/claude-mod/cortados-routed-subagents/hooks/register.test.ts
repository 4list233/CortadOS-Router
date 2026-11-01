import { test, expect } from 'claude-code/testing'
import { lastJsonLine, parseResult, cleanCompletion, scrubToken, normalizeUsage, parseList, mainLoopAllows, validToolName, resolveAllowSet, resolveTimeoutMs, isReview, headerSafeModel, excludeRefusalReason, register, newRunId, parseRunRecord, resetWorkerModels, trackedWorkerModels, trackWorkerModels, builderModelsOf } from './register'

const RUN_ID = /^[A-Za-z0-9_-]{8,64}$/

/** Split ANTHROPIC_CUSTOM_HEADERS into the lines before the trailing run-id line, and the run id (asserted well-formed). */
function splitRunId(h: string | undefined): { base: string; runId: string } {
  const m = /^([\s\S]*)\nx-cortados-run-id: (\S+)$/.exec(String(h))
  expect(m).not.toBe(null)
  expect(RUN_ID.test(m![2])).toBe(true)
  return { base: m![1], runId: m![2] }
}

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
  const { runId } = splitRunId(recordedEnv!.ANTHROPIC_CUSTOM_HEADERS)
  expect(recordedEnv).toEqual({
    ANTHROPIC_BASE_URL: 'http://provider.test:8432',
    ANTHROPIC_API_KEY: 'tok',
    ANTHROPIC_AUTH_TOKEN: 'tok',
    CLAUDE_CODE_ENTRYPOINT: 'cli',
    ANTHROPIC_CUSTOM_HEADERS: 'x-cortados-reveal: 1\nx-cortados-run-id: ' + runId,
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

test('isReview matches a review description (word start) or a review subagent type', async () => {
  expect(isReview('Review the diff', 'general-purpose')).toBe(true)
  expect(isReview('code REVIEWER pass', '')).toBe(true)
  expect(isReview('d', 'code-reviewer')).toBe(true)
  expect(isReview('Build the feature', 'general-purpose')).toBe(false)
  expect(isReview('preview the page', 'general-purpose')).toBe(false) // \b: "preview" is not a review
  expect(isReview('', '')).toBe(false)
})

test('headerSafeModel accepts exactly the server rule: <=128 chars of [A-Za-z0-9._/[]:-]', async () => {
  expect(headerSafeModel('claude-opus-5-5[1m]')).toBe(true)
  expect(headerSafeModel('api/claude-opus-4-1-20250805')).toBe(true)
  expect(headerSafeModel('ollama:qwen3.5_coder')).toBe(true)
  expect(headerSafeModel('a'.repeat(128))).toBe(true)
  expect(headerSafeModel('a'.repeat(129))).toBe(false)
  expect(headerSafeModel('opus 4')).toBe(false) // space is printable but not in the server's id charset
  expect(headerSafeModel('opus,sonnet')).toBe(false)
  expect(headerSafeModel('opus;x')).toBe(false)
  expect(headerSafeModel(42)).toBe(false)
  expect(headerSafeModel('opus\r\nx-api-key: evil')).toBe(false)
  expect(headerSafeModel('opus\n')).toBe(false)
  expect(headerSafeModel('opus\u00e9')).toBe(false)
  expect(headerSafeModel('opus\t')).toBe(false)
  expect(headerSafeModel('')).toBe(false)
})

/** Run one Agent call through the hook and return the child's ANTHROPIC_CUSTOM_HEADERS, the toasts, and how many
 * times the session model was read. */
async function customHeadersFor(call: { description: string; subagent_type: string }, sessionModel: () => Promise<string>, extraOpts: any = {}) {
  let agentHandler: any = null
  const on = (_event: string, filterOrHandler: any, maybeHandler?: any) => {
    if (maybeHandler && filterOrHandler && filterOrHandler.tool === 'Agent') agentHandler = maybeHandler
    return { catch() {} }
  }
  register(on as any, { providerToken: 'tok', providerUrl: 'http://provider.test:8432', orchestratorMode: false, ...extraOpts } as any)
  let headers: string | undefined
  const toasts: string[] = []
  let modelReads = 0
  const $ = {
    process: {
      run: async (argv: string[], opts: any) => {
        headers = splitRunId(opts.env.ANTHROPIC_CUSTOM_HEADERS).base
        return hostLikeProcess({}).run(argv, opts)
      },
    },
    fs: { read: async () => 'unused' },
    ui: { status() {}, toast(m: string) { toasts.push(m) } },
    session: { model: async () => { modelReads += 1; return sessionModel() } },
  }
  const out = await agentHandler($, { prompt: 'check it', ...call }, async () => 'native')
  expect(out.result.status).toBe('completed')
  return { headers, toasts, modelReads }
}

test('a review call keeps the routed child off the session model via x-cortados-exclude-model', async () => {
  const r = await customHeadersFor({ description: 'Review the change', subagent_type: 'general-purpose' }, async () => 'claude-opus-5-5[1m]')
  expect(r.headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5[1m]')
  expect(r.toasts).toEqual([])
})

test('a non-review call sends no exclude header and never reads the session model', async () => {
  const r = await customHeadersFor({ description: 'Build the feature', subagent_type: 'general-purpose' }, async () => 'claude-opus-5-5')
  expect(r.headers).toBe('x-cortados-reveal: 1')
  expect(r.modelReads).toBe(0)
})

test('a routed session (model cortados) sends no exclude header and toasts', async () => {
  const r = await customHeadersFor({ description: 'review', subagent_type: '' }, async () => 'cortados')
  expect(r.headers).toBe('x-cortados-reveal: 1')
  expect(r.toasts.length).toBe(1)
  expect(r.toasts[0]).toContain("can't be enforced")
})

test('an unreadable, empty or unsafe session model sends no exclude header', async () => {
  const thrown = await customHeadersFor({ description: 'review', subagent_type: '' }, async () => { throw new Error('nope') })
  expect(thrown.headers).toBe('x-cortados-reveal: 1')
  expect(thrown.toasts).toEqual(['routed-subagents: could not read the session model; review not kept off it'])
  const empty = await customHeadersFor({ description: 'review', subagent_type: '' }, async () => '')
  expect(empty.headers).toBe('x-cortados-reveal: 1')
  expect(empty.toasts).toEqual(['routed-subagents: could not read the session model; review not kept off it'])
  const crlf = await customHeadersFor({ description: 'review', subagent_type: '' }, async () => 'opus\r\nx-api-key: evil')
  expect(crlf.headers).toBe('x-cortados-reveal: 1')
})

test('excludeBuilderOnReview off sends no exclude header even for a review', async () => {
  const r = await customHeadersFor({ description: 'Review the change', subagent_type: 'code-reviewer' }, async () => 'claude-opus-5-5', { excludeBuilderOnReview: false })
  expect(r.headers).toBe('x-cortados-reveal: 1')
  expect(r.modelReads).toBe(0)
})

test('excludeRefusalReason fires only on the provider\'s own 400 refusal of the exclude header', async () => {
  expect(excludeRefusalReason('API Error: 400 x-cortados-exclude-model: foo names no model this endpoint serves; the review could not be kept off it')).toBe('it serves no model by that name')
  expect(excludeRefusalReason('API Error: 400 no usable model (the client excluded opus via x-cortados-exclude-model)')).toBe('no other usable model')
  expect(excludeRefusalReason('API Error: 400 cortados: x-cortados-exclude-model is not a model id')).toBe('header refused')
  expect(excludeRefusalReason('API Error: 400 sub/claude-opus-5-5 is the excluded model (x-cortados-exclude-model); pin another model or use cortados')).toBe('this mod is pinned to that model')
  expect(excludeRefusalReason('API Error: 400 cortados: api/claude-opus-5-5 is the excluded model (x-cortados-exclude-model); pin another model or use cortados')).toBe('this mod is pinned to that model')
  // multi-model forms (the provider joins excluded names with ", ")
  expect(excludeRefusalReason('API Error: 400 no usable model (the client excluded claude-opus-5-5, claude-sonnet-5 via x-cortados-exclude-model)')).toBe('no other usable model')
  expect(excludeRefusalReason('API Error: 400 x-cortados-exclude-model: claude-opus-5-5, claude-sonnet-5 names no model this endpoint serves; the review could not be kept off it')).toBe('it serves no model by that name')
  expect(excludeRefusalReason('API Error: 400 x-cortados-exclude-model: foo names no model this endpoint serves')).toBe('it serves no model by that name')
  // with several models sent, the pin and bad-id refusals still name one item
  expect(excludeRefusalReason('API Error: 400 sub/claude-sonnet-5 is the excluded model (x-cortados-exclude-model); pin another model or use cortados')).toBe('this mod is pinned to that model')
  expect(excludeRefusalReason('API Error: 400 cortados: x-cortados-exclude-model is not a model id')).toBe('header refused')
  expect(excludeRefusalReason('no usable model (the client excluded a, b via x-cortados-exclude-model)')).toBe(null)
  expect(excludeRefusalReason('API Error: 529 overloaded')).toBe(null)
  // A failure that merely quotes the header (e.g. a review of code about it) is not a refusal: no retry.
  expect(excludeRefusalReason('API Error: 529 overloaded while reviewing x-cortados-exclude-model handling')).toBe(null)
  expect(excludeRefusalReason('no usable model (the client excluded opus via x-cortados-exclude-model)')).toBe(null)
})

/** Run a review through the hook where each child call is answered by `replies` in turn; record each call's headers
 * and timeout. */
async function reviewWithReplies(replies: Array<{ exitCode: number; stdout: string; stderr: string }>) {
  let agentHandler: any = null
  const on = (_event: string, filterOrHandler: any, maybeHandler?: any) => {
    if (maybeHandler && filterOrHandler && filterOrHandler.tool === 'Agent') agentHandler = maybeHandler
    return { catch() {} }
  }
  register(on as any, { providerToken: 'tok', providerUrl: 'http://provider.test:8432', orchestratorMode: false, subagentTimeoutMs: 300_000 } as any)
  const calls: Array<{ headers: string; timeoutMs: number }> = []
  const toasts: string[] = []
  const $ = {
    process: {
      run: async (argv: string[], opts: any) => {
        calls.push({ headers: splitRunId(opts.env.ANTHROPIC_CUSTOM_HEADERS).base, timeoutMs: opts.timeoutMs })
        await hostLikeProcess({}).run(argv, opts) // host timeout validation
        const r = replies[calls.length - 1]
        return { ...r, isStdoutTruncated: false }
      },
    },
    fs: { read: async () => 'unused' },
    ui: { status() {}, toast(m: string) { toasts.push(m) } },
    session: { model: async () => 'claude-opus-5-5' },
  }
  const out = await agentHandler($, { prompt: 'check it', description: 'Review the change', subagent_type: 'general-purpose' }, async () => 'native')
  return { out, calls, toasts }
}

test('a review whose exclude header is refused is retried once without it, with a note', async () => {
  const refused = JSON.stringify({ is_error: true, result: 'API Error: 400 {"error":{"message":"x-cortados-exclude-model: claude-opus-5-5 names no model this endpoint serves; the review could not be kept off it"}}' })
  const { out, calls, toasts } = await reviewWithReplies([
    { exitCode: 1, stdout: refused, stderr: '' },
    { exitCode: 0, stdout: JSON.stringify({ result: 'looks good', model: 'gpt-x' }), stderr: '' },
  ])
  expect(calls.length).toBe(2)
  expect(calls[0].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5')
  expect(calls[1].headers).toBe('x-cortados-reveal: 1')
  expect(calls[1].timeoutMs).toBeLessThanOrEqual(300_000)
  expect(calls[1].timeoutMs).toBeGreaterThan(290_000)
  expect(out.result.status).toBe('completed')
  const text = out.result.content[0].text
  expect(text.startsWith('[review NOT kept off claude-opus-5-5: it serves no model by that name]\n')).toBe(true)
  expect(text).toContain('looks good')
  expect(toasts).toEqual(['routed-subagents: the router refused to keep this review off claude-opus-5-5 (it serves no model by that name); ran it without the exclusion'])
})

test('a refusal named only on stderr also triggers the retry', async () => {
  const { out, calls } = await reviewWithReplies([
    { exitCode: 1, stdout: '', stderr: 'error: 400 ... (the client excluded claude-opus-5-5 via x-cortados-exclude-model)' },
    { exitCode: 0, stdout: JSON.stringify({ result: 'ok', model: 'm' }), stderr: '' },
  ])
  expect(calls.length).toBe(2)
  expect(out.result.content[0].text).toContain('[review NOT kept off claude-opus-5-5: no other usable model]')
})

test('a review failure that does not mention the exclude header is not retried', async () => {
  const { out, calls, toasts } = await reviewWithReplies([
    { exitCode: 1, stdout: JSON.stringify({ is_error: true, result: 'API Error: 529 overloaded' }), stderr: 'boom' },
  ])
  expect(calls.length).toBe(1)
  expect(out.deny).toContain('did not complete cleanly')
  expect(toasts).toEqual([])
})

test('newRunId matches the provider rule and differs per call', async () => {
  const a = newRunId(), b = newRunId()
  expect(RUN_ID.test(a)).toBe(true)
  expect(a).not.toBe(b)
})

test('parseRunRecord keeps one-line label/model and only header-safe models', async () => {
  const r = parseRunRecord(JSON.stringify({ run_id: 'x', model: 'm1', label: 'm1 · 5% left\nX', models: ['m1', 'bad id', 'm2'] }))
  expect(r).toEqual({ label: 'm1 · 5% left X', model: 'm1', models: ['m1', 'm2'], builder_models: null })
  expect(parseRunRecord(JSON.stringify({ model: 'm1', models: ['m1', 'm2'], builder_models: ['m1', 'bad id'] }))?.builder_models).toEqual(['m1'])
  expect(parseRunRecord('nope')).toBe(null)
  expect(parseRunRecord('[1]')).toBe(null)
})

/** One harness for several Agent calls sharing one registered mod: each child returns `reply`, each run-record fetch
 * is answered by `records[runId]` (looked up by the id the child was sent), or `fetchImpl` when given. */
function routedHarness(sessionModel: string, fetchImpl?: (url: string, init: any) => Promise<any>) {
  let agentHandler: any = null
  const on = (_event: string, filterOrHandler: any, maybeHandler?: any) => {
    if (maybeHandler && filterOrHandler && filterOrHandler.tool === 'Agent') agentHandler = maybeHandler
    return { catch() {} }
  }
  register(on as any, { providerToken: 'tok', providerUrl: 'http://provider.test:8432/', orchestratorMode: false } as any)
  const calls: Array<{ headers: string; runId: string }> = []
  const fetches: Array<{ url: string; init: any }> = []
  const toasts: string[] = []
  let nextModels: string[] = []
  let nextBuilders: string[] | undefined
  let nextLabel = ''
  let duringRun: (() => Promise<void>) | null = null
  let childReply = { exitCode: 0, stdout: JSON.stringify({ result: 'done', model: 'cortados' }), stderr: '' }
  const queued: Array<typeof childReply> = []
  const records: Record<string, any> = {}
  const $ = {
    process: {
      run: async (argv: string[], opts: any) => {
        const { base, runId } = splitRunId(opts.env.ANTHROPIC_CUSTOM_HEADERS)
        calls.push({ headers: base, runId })
        records[runId] = { run_id: runId, model: nextModels[0], label: nextLabel, provider: 'p', models: nextModels }
        if (nextBuilders) records[runId].builder_models = nextBuilders
        if (duringRun) { const f = duringRun; duringRun = null; await f() }
        await hostLikeProcess({}).run(argv, opts)
        return { ...(queued.shift() ?? childReply), isStdoutTruncated: false }
      },
    },
    http: {
      fetch: async (url: string, init: any) => {
        fetches.push({ url, init })
        if (fetchImpl) return fetchImpl(url, init)
        const id = url.split('/').pop() as string
        const rec = records[id]
        return rec ? { status: 200, ok: true, headers: {}, text: JSON.stringify(rec) } : { status: 404, ok: false, headers: {}, text: '{}' }
      },
    },
    fs: { read: async () => 'unused' },
    ui: { status() {}, toast(m: string) { toasts.push(m) } },
    session: { model: async () => sessionModel },
  }
  return {
    calls, fetches, toasts,
    reply(r: typeof childReply) { childReply = r },
    /** Use `r` for the next child run only (queued, before the default reply). */
    replyOnce(r: typeof childReply) { queued.push(r) },
    /** Run `f` inside the next child run (after its record is stored), e.g. a worker finishing mid-review. */
    during(f: () => Promise<void>) { duringRun = f },
    async run(description: string, models: string[], label = '', builders?: string[]) {
      nextModels = models
      nextBuilders = builders
      nextLabel = label
      return agentHandler($, { prompt: 'do it', description, subagent_type: 'general-purpose' }, async () => 'native')
    },
  }
}

test('each run sends a well-formed run id and the label comes from the fetched run record', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  const out = await h.run('Build it', ['claude-sonnet-5'], 'claude-sonnet-5 · 62% left')
  expect(out.result.status).toBe('completed')
  expect(out.result.content[0].text.startsWith('[routed via CortadOS → claude-sonnet-5 · 62% left]\n\ndone')).toBe(true)
  expect(h.calls[0].headers).toBe('x-cortados-reveal: 1')
  expect(h.fetches.length).toBe(1)
  expect(h.fetches[0].url).toBe('http://provider.test:8432/v1/cortados/runs/' + h.calls[0].runId)
  expect(h.fetches[0].init.headers).toEqual({ 'x-api-key': 'tok', Authorization: 'Bearer tok' })
  // no label -> the record's model
  const out2 = await h.run('Build more', ['gpt-x'], '')
  expect(out2.result.content[0].text.startsWith('[routed via CortadOS → gpt-x]')).toBe(true)
  expect(h.calls[1].runId).not.toBe(h.calls[0].runId)
  resetWorkerModels()
})

test('a run-record fetch failure falls back to p.model and the subagent still succeeds', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5', async () => { throw new Error('connect ECONNREFUSED tok') })
  const out = await h.run('Build it', ['claude-sonnet-5'])
  expect(out.result.status).toBe('completed')
  expect(out.result.content[0].text.startsWith('[routed via CortadOS → cortados]\n\ndone')).toBe(true)
  expect(trackedWorkerModels()).toEqual([])
  const h404 = routedHarness('claude-opus-5-5', async () => ({ status: 404, ok: false, headers: {}, text: '{}' }))
  const out404 = await h404.run('Build it', ['x'])
  expect(out404.result.content[0].text.startsWith('[routed via CortadOS → cortados]')).toBe(true)
})

test('worker models are excluded on the next review with the session model, then cleared', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-a'])
  // older provider (no builder_models): only the primary model counts, not the side call on m-a
  await h.run('Build B', ['m-b', 'm-a'])
  await h.run('Build C', ['m-c'])
  await h.run('Build D', ['m-d'])
  expect(trackedWorkerModels()).toEqual(['m-b', 'm-c', 'm-d'])
  const rev = await h.run('Review the change', ['m-z'])
  expect(rev.result.status).toBe('completed')
  expect(h.calls[4].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-b,m-c,m-d')
  // the review's own model is not tracked, and the list is cleared after it
  expect(trackedWorkerModels()).toEqual([])
  await h.run('Review again', ['m-z'])
  expect(h.calls[5].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5')
})

test('a failed review keeps the tracked worker models for the next review', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-a'])
  h.reply({ exitCode: 1, stdout: JSON.stringify({ is_error: true, result: 'API Error: 529 overloaded' }), stderr: '' })
  const failed = await h.run('Review it', [])
  expect(failed.deny).toContain('did not complete cleanly')
  expect(trackedWorkerModels()).toEqual(['m-a'])
  resetWorkerModels()
})

test('session model cortados + a tracked worker model -> the header has only the worker model, no toast', async () => {
  resetWorkerModels()
  const h = routedHarness('cortados')
  await h.run('Build A', ['m-a'])
  await h.run('Review the change', [])
  expect(h.calls[1].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: m-a')
  expect(h.toasts).toEqual([])
  expect(trackedWorkerModels()).toEqual([])
})

test('builderModelsOf: builder_models with the primary last, else only the primary; side calls never evict it', async () => {
  expect(builderModelsOf({ label: null, model: 'p', models: ['p', 's1', 's2', 's3'], builder_models: null })).toEqual(['p'])
  expect(builderModelsOf({ label: null, model: 'p', models: ['p', 'b2', 's1'], builder_models: ['p', 'b2'] })).toEqual(['b2', 'p'])
  expect(builderModelsOf({ label: null, model: null, models: [], builder_models: null })).toEqual([])
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-main', 's1', 's2', 's3'], '', ['m-main', 'b2', 'b3', 'b4'])
  expect(trackedWorkerModels()).toEqual(['b3', 'b4', 'm-main'])
  resetWorkerModels()
})

test('a worker model added while a review runs survives the review', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-a'])
  h.during(async () => { await h.run('Build E', ['m-e']) })
  const rev = await h.run('Review the change', ['m-z'])
  expect(rev.result.status).toBe('completed')
  expect(h.calls[1].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-a')
  expect(trackedWorkerModels()).toEqual(['m-e'])
  resetWorkerModels()
})

test('a review retried WITHOUT the exclusion keeps the worker models for the next review', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-a'])
  h.replyOnce({ exitCode: 1, stdout: JSON.stringify({ is_error: true, result: 'API Error: 400 x-cortados-exclude-model: claude-opus-5-5, m-a names no model this endpoint serves; the review could not be kept off it' }), stderr: '' })
  const rev = await h.run('Review the change', ['m-z'])
  expect(rev.result.status).toBe('completed')
  expect(rev.result.content[0].text.startsWith('[review NOT kept off claude-opus-5-5, m-a: it serves no model by that name]\n')).toBe(true)
  expect(h.calls[1].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-a')
  expect(h.calls[2].headers).toBe('x-cortados-reveal: 1')
  expect(trackedWorkerModels()).toEqual(['m-a'])
  const next = await h.run('Review again', ['m-z'])
  expect(next.result.status).toBe('completed')
  expect(h.calls[3].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-a')
  expect(trackedWorkerModels()).toEqual([])
  resetWorkerModels()
})

test('a worker that finishes on the SAME model mid-review keeps that model tracked', async () => {
  resetWorkerModels()
  const h = routedHarness('claude-opus-5-5')
  await h.run('Build A', ['m-a'])
  h.during(async () => { await h.run('Build B', ['m-a']) })
  const rev = await h.run('Review the change', ['m-z'])
  expect(rev.result.status).toBe('completed')
  expect(h.calls[1].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-a')
  expect(trackedWorkerModels()).toEqual(['m-a'])
  await h.run('Review again', ['m-z'])
  expect(h.calls[3].headers).toBe('x-cortados-reveal: 1\nx-cortados-exclude-model: claude-opus-5-5,m-a')
  expect(trackedWorkerModels()).toEqual([])
  resetWorkerModels()
})

test('re-tracking a model moves it to the end; the cap still applies', async () => {
  resetWorkerModels()
  trackWorkerModels(['a', 'b', 'c'])
  trackWorkerModels(['a'])
  expect(trackedWorkerModels()).toEqual(['b', 'c', 'a'])
  trackWorkerModels(['d'])
  expect(trackedWorkerModels()).toEqual(['c', 'a', 'd'])
  resetWorkerModels()
})
