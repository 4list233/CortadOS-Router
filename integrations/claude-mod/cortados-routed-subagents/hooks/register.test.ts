import { test, expect } from 'claude-code/testing'
import { lastJsonLine, parseResult, cleanCompletion, scrubToken, normalizeUsage, parseList, mainLoopAllows } from './register'

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
