import { test, expect } from 'claude-code/testing'
import { lastJsonLine, parseResult } from './register'

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
