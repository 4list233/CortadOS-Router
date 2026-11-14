import { test, expect, mock } from 'claude-code/testing'
import { register, isBigEdit, isBuildOrTest, substantiveKind, guardStatusText, parseGuardArg, GUARD_TOAST_INTERVAL_MS } from './register'

// Main-session guard: WARN ONLY. These run through the engine's own host (`claude plugin test`): the test's hooks sit
// beneath the plugin and stand for the engine, so a tool call the test's `tool.call` hook sees is a call that ran.

// These tests are about the warn-only guard, which is what is left on the main loop with the gate off; the
// orchestrator gate (balanced by default) has its own tests in gate.test.ts.
const GATE_OFF = { options: { orchestratorGate: 'off' } } as any

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
  on('process.run', () => ({ value: { stdout: '', stderr: '', exitCode: 0 } }))
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  return { ran, toasts, statuses, clock, commands, store }
}

const edit = (new_string: string, extra: Record<string, unknown> = {}) =>
  ({ tool: 'Edit', file_path: '/tmp/x.py', old_string: 'a', new_string, ...extra }) as any

test('a big main-session Edit warns and still runs unchanged', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  const out: any = await $.tool.call(edit(BIG))
  expect(out.deny).toBe(undefined)
  expect(w.ran.length).toBe(1)
  expect(w.ran[0].new_string).toBe(BIG) // input passed through untouched
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toMatch(/main session did a large edit; delegate/)
  expect(w.statuses.at(-1)).toBe('main did 1 big edit; delegate with start_task')
})

test('a small main-session Edit is silent', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call(edit(SMALL))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a subagent call is silent, big edit or build alike', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call(edit(BIG, { agentId: 'a1' }))
  await $.tool.call({ tool: 'Bash', command: 'python3 -m unittest discover', agentId: 'a1' } as any)
  expect(w.ran.length).toBe(2)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a main-session build/test Bash warns and runs; plain Bash is silent', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call({ tool: 'Bash', command: 'ls -la' } as any)
  expect(w.toasts).toEqual([])
  await $.tool.call({ tool: 'Bash', command: 'cd repo && npm run build' } as any)
  expect(w.ran.length).toBe(2)
  expect(w.ran[1].command).toBe('cd repo && npm run build')
  expect(w.toasts.length).toBe(1)
  expect(w.toasts[0]).toMatch(/build\/test/)
  expect(w.statuses.at(-1)).toBe('main did 1 build/test run; delegate with start_task')
})

test('toasts are throttled to one per interval; the counter still counts every call', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call(edit(BIG))
  await $.tool.call(edit(BIG))
  await $.tool.call({ tool: 'Write', file_path: '/tmp/y', content: 'z'.repeat(2500) } as any)
  expect(w.ran.length).toBe(3)
  expect(w.toasts.length).toBe(1)
  expect(w.statuses.at(-1)).toBe('main did 3 big edits; delegate with start_task')
  await w.clock.advance(GUARD_TOAST_INTERVAL_MS - 1)
  await $.tool.call(edit(BIG))
  expect(w.toasts.length).toBe(1)
  await w.clock.advance(1)
  await $.tool.call({ tool: 'Bash', command: 'pytest -q' } as any)
  expect(w.toasts.length).toBe(2)
  expect(w.statuses.at(-1)).toBe('main did 4 big edits, 1 build/test run; delegate with start_task')
})

test('userConfig mainSessionGuard off silences the guard; the call still runs', { options: { mainSessionGuard: false, orchestratorGate: 'off' } } as any, async ($: any, on: any) => {
  const w = engine(on)
  await $.tool.call(edit(BIG))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
  expect(w.statuses).toEqual([])
})

test('a stored /cortados-guard off silences it across sessions', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on, { mainSessionGuard: false })
  await $.tool.call(edit(BIG))
  expect(w.ran.length).toBe(1)
  expect(w.toasts).toEqual([])
})

test('/cortados-guard off then on toggles the guard and persists in the store', GATE_OFF, async ($: any, on: any) => {
  const w = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  expect(w.commands).toEqual(['cortados-guard', 'cortados-gate', 'cortados-local'])
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

// ---- Review r1 regressions -------------------------------------------------------------------------------------

/** The engine with one noun made to fail; records the transcript notices ($.ui.log) the guard reports. */
function faulty(on: any, fail: 'store.get' | 'store.set' | 'clock.now' | 'ui.status' | 'ui.toast' | 'command.register') {
  const ran: any[] = []
  const toasts: string[] = []
  const statuses: (string | undefined)[] = []
  const logs: string[] = []
  const store: Record<string, unknown> = {}
  const boom = () => { throw new TypeError('injected fault secret-ish detail') }
  on('tool.call', (_$: any, e: any) => { ran.push(e); return { result: { ok: true } } })
  on('ui.log', (_$: any, e: any) => { logs.push(e.text); return { value: undefined } })
  on('process.run', () => ({ value: { stdout: '', stderr: '', exitCode: 0 } }))
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  on('store.get', fail === 'store.get' ? boom : (_$: any, e: any) => ({ value: store[e.key] }))
  on('store.set', fail === 'store.set' ? boom : (_$: any, e: any) => { store[e.key] = e.value; return { value: undefined } })
  on('clock.sleep', async () => new Promise(() => {}))
  on('clock.now', fail === 'clock.now' ? boom : () => ({ value: 1_000_000 }))
  on('ui.status', fail === 'ui.status' ? boom : (_$: any, e: any) => { statuses.push(e.text); return { value: undefined } })
  on('ui.toast', fail === 'ui.toast' ? boom : (_$: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('command.register', fail === 'command.register' ? boom : (_$: any, e: any) => ({ value: { command: e.name } }))
  return { ran, toasts, statuses, logs, store }
}

/** The calls each ran exactly once with the input unchanged, and answered what a non-substantive call answers. */
async function runsUnchanged($: any, w: { ran: any[] }) {
  const baseline: any = await $.tool.call(edit(SMALL)) // not substantive: the guard does nothing
  const a: any = await $.tool.call(edit(BIG))
  const b: any = await $.tool.call({ tool: 'Bash', command: 'make test' })
  expect(w.ran.length).toBe(3)
  expect(w.ran[1].new_string).toBe(BIG)
  expect(w.ran[2].command).toBe('make test')
  expect(a).toEqual(baseline)
  expect(b).toEqual(baseline)
}

// The warn guard's own notices; the gate's ("main-session guard gate ...", e.g. no session id in this host) are its own.
const guardNotices = (logs: string[]) => logs.filter((l) => l.includes('main-session guard') && !l.includes('main-session guard gate '))

// The host hands a plugin a failed call on $ as a HooksError, so the notice names that class.
for (const [fail, where] of [['store.get', 'store read'], ['clock.now', 'clock']] as const) {
  test(`a failing ${fail} is reported once, credential-free; every call still runs exactly once, unchanged`, GATE_OFF, async ($: any, on: any) => {
    const w = faulty(on, fail)
    await runsUnchanged($, w)
    const mine = guardNotices(w.logs)
    expect(mine.length).toBe(1)
    expect(mine[0]).toMatch(new RegExp(`^cortados-routed-subagents: main-session guard ${where} failed \\(\\w+\\); the tool call ran unchanged$`))
    expect(mine[0]).not.toMatch(/secret-ish/) // the error's message never reaches the notice
  })
}

// $.ui.status and $.ui.toast are fire-and-forget: a failure beneath them never reaches the plugin (the host drops the
// call and reports it itself: "$.ui.status dropped: ..."). The guard's own wrapper reports a synchronous throw (see
// the unit test below); here the point is that the calls are untouched.
for (const fail of ['ui.status', 'ui.toast'] as const) {
  test(`a failing ${fail} leaves every call running exactly once, unchanged`, GATE_OFF, async ($: any, on: any) => {
    const w = faulty(on, fail)
    await runsUnchanged($, w)
    expect(guardNotices(w.logs).length).toBeLessThanOrEqual(1)
  })
}

test('a failing store write keeps the toggle for this session, says so once, and the call still runs', GATE_OFF, async ($: any, on: any) => {
  const w = faulty(on, 'store.set')
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  const off: any = await $.command.run({ command: 'cortados-guard', args: 'off' } as any)
  expect(off.text).toMatch(/guard off .*not saved/)
  await runsUnchanged($, w)
  expect(w.toasts).toEqual([])
  expect(guardNotices(w.logs).length).toBe(1)
  expect(guardNotices(w.logs)[0]).toMatch(/store write failed/)
})

test('a synchronous throw from $.ui.status/$.ui.toast is reported once under its step and never escapes', async () => {
  const logs: string[] = []
  let statusCalls = 0
  const $: any = {
    store: { get: async () => undefined, set: async () => {} },
    clock: { now: async () => 5 },
    ui: { status() { statusCalls += 1; throw new Error('x') }, toast() { throw new RangeError('y') }, log(t: string) { logs.push(t) } },
  }
  const handlers: Record<string, any> = {}
  const on: any = (ev: string, a: any, b?: any) => { const h = b ?? a; if (ev === 'tool.call' && a?.tool === 'Edit') handlers.edit = h; return { catch() {} } }
  register(on, { orchestratorMode: false } as any)
  const calls: any[] = []
  const next: any = async (x: any) => { calls.push(x); return { result: { ok: 1 } } }
  const e = edit(BIG)
  expect(await handlers.edit($, e, next)).toEqual({ result: { ok: 1 } })
  expect(await handlers.edit($, e, next)).toEqual({ result: { ok: 1 } })
  expect(calls).toEqual([e, e])
  expect(statusCalls).toBe(2)
  expect(logs).toEqual([
    'cortados-routed-subagents: main-session guard status failed (Error); the tool call ran unchanged',
    'cortados-routed-subagents: main-session guard toast failed (RangeError); the tool call ran unchanged',
  ])
})

test('a failing command registration is reported once per command and session.start still completes', GATE_OFF, async ($: any, on: any) => {
  const w = faulty(on, 'command.register')
  const r: any = await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  expect(r.cwd).toBe('/')
  const mine = guardNotices(w.logs)
  expect(mine.length).toBe(3)
  expect(mine[0]).toMatch(/^cortados-routed-subagents: main-session guard \/cortados-guard registration failed \(\w+\)/)
  expect(mine[1]).toMatch(/^cortados-routed-subagents: main-session guard \/cortados-gate registration failed \(\w+\)/)
  expect(mine[2]).toMatch(/^cortados-routed-subagents: main-session guard \/cortados-local registration failed \(\w+\)/)
  const before = w.ran.length
  const out: any = await $.tool.call(edit(BIG))
  expect(w.ran.length).toBe(before + 1)
  expect(out.deny).toBe(undefined)
})

/** Register the mod against a recording `on` and hand back its handlers by name. */
function handlersOf(opts: Record<string, unknown> = {}) {
  const h: Record<string, any> = {}
  const on: any = (ev: string, a: any, b?: any) => {
    const fn = b ?? a
    if (ev === 'tool.call' && a?.tool) h[a.tool] = fn
    else if (ev === 'command.run' && a?.command === 'cortados-guard') h.command = fn
    return { catch() {} }
  }
  register(on, { orchestratorMode: false, providerToken: 'tok', providerUrl: 'http://provider.test:8432', ...opts } as any)
  return h
}

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

test('a first store read that resolves after /cortados-guard off cannot undo the toggle', async () => {
  const h = handlersOf()
  const read = deferred<unknown>()
  let reads = 0
  const toasts: string[] = []
  const saved: Record<string, unknown> = {}
  const $: any = {
    store: { get: () => { reads += 1; return read.promise }, set: async (k: string, v: unknown) => { saved[k] = v } },
    clock: { now: async () => 5 },
    ui: { status() {}, toast(t: string) { toasts.push(t) }, log() {} },
  }
  const calls: any[] = []
  const next: any = async (x: any) => { calls.push(x); return { result: {} } }
  const pending = h.Edit($, edit(BIG), next)       // waits on the slow first read
  await Promise.resolve()
  const r = await h.command($, { command: 'cortados-guard', args: 'off' })
  expect(r.text).toMatch(/guard off/)
  read.resolve(true)                                // the stale read says "on"
  await pending
  await h.Edit($, edit(BIG), next)
  expect(calls.length).toBe(2)
  expect(toasts).toEqual([])                        // the toggle held
  expect(saved.mainSessionGuard).toBe(false)
  expect(reads).toBe(1)
})

test('while a routed subagent runs the guard leaves its status line alone, then restores its counter', async () => {
  const h = handlersOf()
  const child = deferred<any>()
  const statuses: (string | undefined)[] = []
  const toasts: string[] = []
  const $: any = {
    store: { get: async () => undefined, set: async () => {} },
    clock: { now: async () => 5 },
    process: { run: (argv: string[]) => (argv[0] === 'sh' ? Promise.resolve({ exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false }) : child.promise) },
    fs: { read: async () => '' },
    http: { fetch: async () => ({ ok: false, status: 404, headers: {}, text: '{}' }) },
    session: { model: async () => 'claude-opus' },
    ui: { status(t: string | undefined) { statuses.push(t) }, toast(t: string) { toasts.push(t) }, log() {} },
  }
  const agent = h.Agent($, { prompt: 'do it', description: 'build thing' }, async () => { throw new Error('native must not run') })
  await new Promise((r) => setTimeout(r, 0))
  expect(statuses).toEqual(['↗ routed subagent (build thing)'])
  const next: any = async () => ({ result: {} })
  await h.Edit($, edit(BIG), next)
  await h.command($, { command: 'cortados-guard', args: 'off' })
  await h.command($, { command: 'cortados-guard', args: 'on' })
  expect(statuses).toEqual(['↗ routed subagent (build thing)']) // neither the counter nor the toggle touched it
  expect(toasts.length).toBe(1)                                  // the nudge itself still shows
  child.resolve({ exitCode: 0, stdout: JSON.stringify({ result: 'done', model: 'm' }), stderr: '', isStdoutTruncated: false })
  const out: any = await agent
  expect(out.result.status).toBe('completed')
  expect(statuses.at(-1)).toBe('main did 1 big edit; delegate with start_task')
})

// ---- Review r2 regressions: the routed-child count and the status restore ---------------------------------------

/** A $ whose routed children each wait on their own deferred, in launch order; `statusFails(text)` decides a throw. */
function routedWorld(statusFails: (text: string | undefined, n: number) => boolean = () => false) {
  const children: Array<ReturnType<typeof deferred<any>>> = []
  const statuses: (string | undefined)[] = []
  const logs: string[] = []
  let n = 0
  const $: any = {
    store: { get: async () => undefined, set: async () => {} },
    clock: { now: async () => 5 },
    process: {
      run: (argv: string[]) => {
        if (argv[0] === 'sh') return Promise.resolve({ exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false })
        const d = deferred<any>()
        children.push(d)
        return d.promise
      },
    },
    fs: { read: async () => '' },
    http: { fetch: async () => ({ ok: false, status: 404, headers: {}, text: '{}' }) },
    session: { model: async () => 'claude-opus' },
    ui: {
      status(t: string | undefined) { n += 1; if (statusFails(t, n)) throw new Error('status down'); statuses.push(t) },
      toast() {},
      log(t: string) { logs.push(t) },
    },
  }
  const done = { exitCode: 0, stdout: JSON.stringify({ result: 'done', model: 'm' }), stderr: '', isStdoutTruncated: false }
  return { $, children, statuses, logs, finish: (i: number) => children[i].resolve(done) }
}

const tick = () => new Promise((r) => setTimeout(r, 0))
const nativeMustNotRun = async () => { throw new Error('native must not run') }
const okNext: any = async () => ({ result: {} })
const COUNTER = (n: number) => `main did ${n} big edit${n === 1 ? '' : 's'}; delegate with start_task`

test('a startup status failure still launches the child, is reported once, and leaves no routed count behind', async () => {
  const h = handlersOf()
  const w = routedWorld((t) => typeof t === 'string' && t.startsWith('↗'))
  const agent = h.Agent(w.$, { prompt: 'p', description: 'a' }, nativeMustNotRun)
  await tick()
  expect(w.children.length).toBe(1)                // launched despite the status failure
  w.finish(0)
  expect((await agent).result.status).toBe('completed')
  await h.Edit(w.$, edit(BIG), okNext)
  expect(w.statuses.at(-1)).toBe(COUNTER(1))       // the count went back to 0: the guard publishes again
  expect(w.logs.filter((l) => l.includes('status failed')).length).toBe(1)
})

test('a failed counter restore after a child is reported once and the next publish still lands', async () => {
  const h = handlersOf()
  let failedOnce = false
  const w = routedWorld((t) => { if (!failedOnce && t === COUNTER(1)) { failedOnce = true; return true } return false })
  const agent = h.Agent(w.$, { prompt: 'p', description: 'a' }, nativeMustNotRun)
  await tick()
  await h.Edit(w.$, edit(BIG), okNext)             // counted while the child runs: not published
  w.finish(0)
  expect((await agent).result.status).toBe('completed')
  expect(failedOnce).toBe(true)                    // the restore was attempted and failed
  expect(w.logs.filter((l) => l.includes('main-session guard status failed')).length).toBe(1)
  await h.Edit(w.$, edit(BIG), okNext)
  expect(w.statuses.at(-1)).toBe(COUNTER(2))
})

for (const order of [[0, 1], [1, 0]] as const) {
  test(`two routed children at once: the counter returns only after both finish (order ${order.join(',')})`, async () => {
    const h = handlersOf()
    const w = routedWorld()
    const a = h.Agent(w.$, { prompt: 'p', description: 'a' }, nativeMustNotRun)
    const b = h.Agent(w.$, { prompt: 'p', description: 'b' }, nativeMustNotRun)
    await tick()
    expect(w.children.length).toBe(2)
    await h.Edit(w.$, edit(BIG), okNext)
    expect(w.statuses).toEqual(['↗ routed subagent (a)', '↗ routed subagent (b)'])
    w.finish(order[0])
    await (order[0] === 0 ? a : b)
    expect(w.statuses.includes(COUNTER(1))).toBe(false) // one child still runs: its line is not overwritten
    w.finish(order[1])
    await (order[1] === 0 ? a : b)
    expect(w.statuses.at(-1)).toBe(COUNTER(1))
    await h.Edit(w.$, edit(BIG), okNext)
    expect(w.statuses.at(-1)).toBe(COUNTER(2))          // the count is back to 0
  })
}
