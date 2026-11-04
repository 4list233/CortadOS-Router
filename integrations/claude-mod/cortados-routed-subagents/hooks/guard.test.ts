import { test, expect, mock } from 'claude-code/testing'
import { isBigEdit, isBuildOrTest, substantiveKind, guardStatusText, parseGuardArg, GUARD_TOAST_INTERVAL_MS } from './register'

// Main-session guard: WARN ONLY. These run through the engine's own host (`claude plugin test`): the test's hooks sit
// beneath the plugin and stand for the engine, so a tool call the test's `tool.call` hook sees is a call that ran.

const BIG = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n') // 50 lines > 40
const SMALL = 'x = 1\ny = 2'

/** The engine beneath the plugin: records the tool calls that reached it, the toasts and the status lines. */
function engine(on: any, store: Record<string, unknown> = {}) {
  const ran: any[] = []
  const toasts: string[] = []
  const statuses: (string | undefined)[] = []
  const clock = mock.clock(on, { now: 1_000_000 })
  // The plugin's own $.store, kept in `store` so a test can read what the plugin persisted.
  on('store.get', (_$: any, e: any) => ({ value: store[e.key] }))
  on('store.set', (_$: any, e: any) => { store[e.key] = e.value; return { value: undefined } })
  on('tool.call', (_$: any, e: any) => { ran.push(e); return { result: { ok: true } } })
  on('ui.toast', (_$: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('ui.status', (_$: any, e: any) => { statuses.push(e.text); return { value: undefined } })
  const commands: string[] = []
  on('command.register', (_$: any, e: any) => { commands.push(e.name); return { value: { command: e.name } } })
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  return { ran, toasts, statuses, clock, commands, store }
}

const edit = (new_string: string, extra: Record<string, unknown> = {}) =>
  ({ tool: 'Edit', file_path: '/tmp/x.py', old_string: 'a', new_string, ...extra }) as any

test('a big main-session Edit warns and still runs unchanged', async ($, on) => {
  const w = engine(on)
  const out: any = await $.tool.call(edit(BIG))
  expect(out.deny).toBe(undefined)
  expect(w.ran.length).toBe(1)
  expect(w.ran[0].new_string).toBe(BIG) // input passed through untouched
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toMatch(/main session did a large edit; delegate/)
  expect(w.statuses.at(-1)).toBe('main did 1 big edit; delegate to routed subagents')
})

test('a small main-session Edit is silent', async ($, on) => {
  const w = engine(on)
  await $.tool.call(edit(SMALL))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a subagent call is silent, big edit or build alike', async ($, on) => {
  const w = engine(on)
  await $.tool.call(edit(BIG, { agentId: 'a1' }))
  await $.tool.call({ tool: 'Bash', command: 'python3 -m unittest discover', agentId: 'a1' } as any)
  expect(w.ran.length).toBe(2)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a main-session build/test Bash warns and runs; plain Bash is silent', async ($, on) => {
  const w = engine(on)
  await $.tool.call({ tool: 'Bash', command: 'ls -la' } as any)
  expect(w.toasts).toEqual([])
  await $.tool.call({ tool: 'Bash', command: 'cd repo && npm run build' } as any)
  expect(w.ran.length).toBe(2)
  expect(w.ran[1].command).toBe('cd repo && npm run build')
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toMatch(/build\/test/)
  expect(w.statuses.at(-1)).toBe('main did 1 build/test run; delegate to routed subagents')
})

test('toasts are throttled to one per interval; the counter still counts every call', async ($, on) => {
  const w = engine(on)
  await $.tool.call(edit(BIG))
  await $.tool.call(edit(BIG))
  await $.tool.call({ tool: 'Write', file_path: '/tmp/y', content: 'z'.repeat(2500) } as any)
  expect(w.ran.length).toBe(3)
  expect(w.toasts.length).toBe(1)
  expect(w.statuses.at(-1)).toBe('main did 3 big edits; delegate to routed subagents')
  await w.clock.advance(GUARD_TOAST_INTERVAL_MS - 1)
  await $.tool.call(edit(BIG))
  expect(w.toasts.length).toBe(1)
  await w.clock.advance(1)
  await $.tool.call({ tool: 'Bash', command: 'pytest -q' } as any)
  expect(w.toasts.length).toBe(2)
  expect(w.statuses.at(-1)).toBe('main did 4 big edits, 1 build/test run; delegate to routed subagents')
})

test('userConfig mainSessionGuard off silences the guard; the call still runs', { options: { mainSessionGuard: false } } as any, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call(edit(BIG))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a stored /cortados-guard off silences it across sessions', async ($, on) => {
  const w = engine(on, { mainSessionGuard: false })
  await $.tool.call(edit(BIG))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
})

test('/cortados-guard off then on toggles the guard and persists in the store', async ($, on) => {
  const w = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  expect(w.commands).toEqual(['cortados-guard'])
  const off: any = await $.command.run({ command: 'cortados-guard', args: 'off' } as any)
  expect(off.text).toMatch(/guard off/)
  expect(w.store.mainSessionGuard).toBe(false)
  await $.tool.call(edit(BIG))
  expect(w.toasts).toEqual([])
  const onRes: any = await $.command.run({ command: 'cortados-guard', args: 'on' } as any)
  expect(onRes.text).toMatch(/guard on/)
  expect(w.store.mainSessionGuard).toBe(true)
  await $.tool.call(edit(BIG))
  expect(w.toasts.length).toBe(1)
  const bad: any = await $.command.run({ command: 'cortados-guard', args: 'maybe' } as any)
  expect(bad.text).toMatch(/usage/)
  expect(w.ran.length).toBe(2)
})

test('thresholds and build/test detection', async () => {
  expect(isBigEdit(Array(40).fill('a').join('\n'))).toBe(false) // exactly 40 lines
  expect(isBigEdit(Array(41).fill('a').join('\n'))).toBe(true)
  expect(isBigEdit('a'.repeat(2000))).toBe(false)
  expect(isBigEdit('a'.repeat(2001))).toBe(true)
  expect(isBigEdit('')).toBe(false)
  for (const c of ['python3 -m unittest discover -s tests', 'python -B -m pytest', 'CORTADOS_HOME=/tmp/x python3 -B -m unittest', 'pytest', 'make', 'make test', 'cd a && make -j4', 'npm test', 'npm run build', 'yarn build', 'yarn test --watch=false', 'pnpm run test']) {
    expect([c, isBuildOrTest(c)]).toEqual([c, true])
  }
  for (const c of ['ls', 'grep make Makefile', 'echo pytest', 'git status', 'npm install', 'cat build.log', 'python3 script.py']) {
    expect([c, isBuildOrTest(c)]).toEqual([c, false])
  }
  expect(substantiveKind('NotebookEdit', { new_source: 'b'.repeat(3000) })).toBe('edit')
  expect(substantiveKind('Write', { content: SMALL })).toBe(null)
  expect(guardStatusText(0, 0)).toBe(undefined)
  expect(parseGuardArg(' ON ')).toBe(true)
  expect(parseGuardArg('off')).toBe(false)
  expect(parseGuardArg('')).toBe(null)
})
