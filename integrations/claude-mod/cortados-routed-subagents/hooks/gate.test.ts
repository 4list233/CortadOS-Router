import { test, expect, mock } from 'claude-code/testing'
import {
  register, balancedClass, parseGateLevel, configGateLevel, resolveEditBudget, gateStatusText, gateDenyMessage,
  parseGateMap, withGateOverride, AGENT_REFUSAL, AGENT_FORK_ONLY, isForkRequest, isOwnerPrompt, agentDecision, GATE_COMMAND, GATE_STORE_KEY, GATE_STORE_MAX_SESSIONS,
  GATE_HEALTH_TTL_MS, GATE_BASH_MAX_TIMEOUT_MS, GATE_EDIT_BUDGET_DEFAULT, GATE_HEALTH_TIMEOUT_MS,
  isTaskWatchCommand, isBuildOrTest, shellSegments, checkDelegation,
} from './register'

// The orchestrator gate (balanced by default, owner 2026-11-12). Most tests drive the mod's own handlers with a fake
// `$` (as register.test.ts does); the last ones run through the engine's test host to pin the hook ORDER.

const BIG = Array.from({ length: 50 }, (_, i) => `line ${i}`).join('\n') // 50 lines > GUARD_EDIT_MAX_LINES
const SMALL = 'x = 1'
const ROUTE_TOOLS = ['start_task', 'route_task', 'task_status', 'wait_task', 'list_tasks', 'cancel_task'].map((t) => `mcp__cortados-route__${t}`)
const BASE_TOOLS = ['Read', 'Edit', 'Write', 'Bash', 'WebSearch', 'WebFetch', 'ToolSearch']

type World = {
  tools?: string[]
  health?: 'ok' | 'down' | 'throw' | 'notok'
  env?: Record<string, string | undefined>
  sid?: string | null
  store?: Record<string, unknown>
}

/** Register the mod and hand back its handlers plus a fake `$` recording what the gate did. */
function gateWorld(opts: Record<string, unknown> = {}, w: World = {}) {
  const h: Record<string, any> = {}
  const on: any = (ev: string, a: any, b?: any) => {
    const fn = b ?? a
    if (ev === 'tool.call') h[b ? String(a?.tool) : '*'] = fn
    else if (ev === 'command.run') h[`/${a?.command}`] = fn
    else h[ev] = fn
    return { catch() {} }
  }
  register(on, { providerToken: 'tok', providerUrl: 'http://provider.test:8432', mainSessionGuard: false, officeView: false, ...opts } as any)
  const state = {
    tools: w.tools ?? [...BASE_TOOLS, ...ROUTE_TOOLS],
    health: w.health ?? 'ok',
    env: w.env ?? {},
    sid: w.sid === undefined ? 'session-1' : w.sid,
    store: w.store ?? {},
    now: 1_000_000,
    fetches: [] as string[],
    statuses: [] as (string | undefined)[],
    toasts: [] as string[],
    logs: [] as string[],
    ran: [] as any[],
  }
  const $: any = {
    session: { id: async () => state.sid },
    store: { get: async (k: string) => state.store[k], set: async (k: string, v: unknown) => { state.store[k] = JSON.parse(JSON.stringify(v)) } },
    env: { get: async (name: string) => state.env[name] },
    tool: { list: async () => state.tools.map((name) => ({ name, mcp: name.startsWith('mcp__') })) },
    clock: { now: async () => state.now },
    process: { run: async () => ({ exitCode: 0, stdout: '', stderr: '' }) },
    fs: { read: async () => '' },
    http: {
      fetch: async (url: string) => {
        state.fetches.push(url)
        if (state.health === 'throw') throw new Error('ECONNREFUSED')
        if (state.health === 'down') return { ok: false, status: 503, headers: {}, text: '' }
        return { ok: true, status: 200, headers: {}, text: JSON.stringify({ ok: state.health === 'ok' }) }
      },
    },
    ui: { status(t: string | undefined) { state.statuses.push(t) }, toast(t: string) { state.toasts.push(t) }, log(t: string) { state.logs.push(t) } },
    command: { register: async () => {} },
  }
  const next: any = async (e: any) => { state.ran.push(e); return { result: 'ran' } }
  const call = (e: Record<string, unknown>) => h['*']($, e, next)
  return { h, $, state, call, next }
}

const isDeny = (r: any) => typeof r?.deny === 'string'

// ---- What is denied and what is allowed under balanced (provider healthy, cortados-route present) ---------------

const DENIED: Array<[string, Record<string, unknown>]> = [
  ['WebSearch', { tool: 'WebSearch', query: 'x' }],
  ['WebFetch', { tool: 'WebFetch', url: 'https://example.com', prompt: 'p' }],
  ['NotebookEdit', { tool: 'NotebookEdit', notebook_path: '/n.ipynb', new_source: 'x' }],
  ['big Edit', { tool: 'Edit', file_path: '/a', old_string: 'a', new_string: BIG }],
  ['big Write', { tool: 'Write', file_path: '/a', content: 'z'.repeat(2500) }],
  ['big MultiEdit', { tool: 'MultiEdit', file_path: '/a', edits: [{ old_string: 'a', new_string: BIG }] }],
  ['build Bash', { tool: 'Bash', command: 'cd repo && python3 -B -m unittest discover -s tests' }],
  ['test Bash', { tool: 'Bash', command: 'npm test' }],
  ['make Bash', { tool: 'Bash', command: 'make -j4' }],
  ['background Bash', { tool: 'Bash', command: 'sleep 1', run_in_background: true }],
  ['long-timeout Bash', { tool: 'Bash', command: 'rsync -a a b', timeout: GATE_BASH_MAX_TIMEOUT_MS + 1 }],
  // orchestratorEditBudget 0 by default (owner 2026-11-13): even a one-character edit is delegated work.
  ['small Edit', { tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL }],
  ['small Write', { tool: 'Write', file_path: '/b', content: SMALL }],
  ['small MultiEdit', { tool: 'MultiEdit', file_path: '/a', edits: [{ old_string: 'a', new_string: SMALL }] }],
]

const ALLOWED: Array<[string, Record<string, unknown>]> = [
  ...ROUTE_TOOLS.map((t) => [t, { tool: t }] as [string, Record<string, unknown>]),
  ...['Read', 'Grep', 'Glob', 'ToolSearch', 'Skill', 'AskUserQuestion', 'TodoWrite', 'ExitPlanMode'].map((t) => [t, { tool: t }] as [string, Record<string, unknown>]),
  ...['mcp__ccd_turn__update_plan', 'mcp__ccd_turn__wrap_up', 'mcp__ccd_session__mark_chapter'].map((t) => [t, { tool: t }] as [string, Record<string, unknown>]),
  ['git status', { tool: 'Bash', command: 'git status && git log --oneline -5 && git diff --stat' }],
  ['ls/cat', { tool: 'Bash', command: 'ls -la && cat README.md' }],
  ['curl health', { tool: 'Bash', command: 'curl -s http://127.0.0.1:8432/health' }],
  ['ssh one-liner', { tool: 'Bash', command: 'ssh air uptime' }],
  ['Bash at the 120 s timeout', { tool: 'Bash', command: 'git fetch', timeout: GATE_BASH_MAX_TIMEOUT_MS }],
  ['Chrome', { tool: 'mcp__claude-in-chrome__navigate', url: 'https://x' }],
  ['Chrome tabs', { tool: 'mcp__claude-in-chrome__tabs_context_mcp' }],
  ['connector (Gmail)', { tool: 'mcp__claude_ai_Gmail__search_threads', query: 'x' }],
  ['connector (Slack)', { tool: 'mcp__slack__post_message', text: 'x' }],
  ['another server start_task', { tool: 'mcp__velox-workspace__start_task' }],
  ['Artifact', { tool: 'Artifact' }],
  ['ArtifactData', { tool: 'ArtifactData' }],
  ['Monitor', { tool: 'Monitor' }],
]

test('balanced (the default): every deny class is refused naming start_task, route_task and the escape hatch', async () => {
  for (const [name, e] of DENIED) {
    const w = gateWorld()
    const r: any = await w.call(e)
    expect([name, isDeny(r)]).toEqual([name, true])
    expect(r.deny).toContain('mcp__cortados-route__start_task')
    expect(r.deny).toContain('mcp__cortados-route__route_task')
    expect(r.deny).toContain(`/${GATE_COMMAND} off`)
    expect(r.deny).toContain('acceptance check')
    expect(w.state.ran).toEqual([])
  }
})

test('balanced: reading, inspection Bash, Chrome, connectors, host plumbing, Artifact and delegation run', async () => {
  for (const [name, e] of ALLOWED) {
    const w = gateWorld()
    const r: any = await w.call(e)
    expect([name, r]).toEqual([name, { result: 'ran' }])
  }
})

test('the allow path asks the provider nothing: no tool list, no health fetch', async () => {
  const w = gateWorld()
  let lists = 0
  w.$.tool.list = async () => { lists += 1; return [] }
  for (const [, e] of ALLOWED) await w.call(e)
  expect(lists).toBe(0)
  expect(w.state.fetches).toEqual([])
})

test('research is pointed at route_task for a text answer', async () => {
  const w = gateWorld()
  const r: any = await w.call({ tool: 'WebSearch', query: 'x' })
  expect(r.deny).toMatch(/WebSearch \(web research\) is delegated work — route_task returns a researched text answer/)
})

// ---- The small-edit budget ---------------------------------------------------------------------------------------

test('the default edit budget is 0: every small edit on the main loop is refused, saying the session plans and delegates', async () => {
  expect(GATE_EDIT_BUDGET_DEFAULT).toBe(0)
  const w = gateWorld()
  for (const e of [{ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL }, { tool: 'Write', file_path: '/b', content: SMALL }]) {
    const r: any = await w.call(e)
    expect(r.deny).toMatch(/on the main session is implementation work \(orchestratorEditBudget is 0: the main session plans and delegates\)/)
    expect(r.deny).toContain('mcp__cortados-route__start_task')
    expect(r.deny).toContain(`/${GATE_COMMAND} off`)
  }
  // a new user prompt does not open a budget of 0
  await w.h['prompt.submit'](w.$, { text: 'next request', origin: { kind: 'composer' } }, async () => ({}))
  expect(isDeny(await w.call({ tool: 'Edit', new_string: SMALL }))).toBe(true)
  expect(w.state.ran).toEqual([])
})

test('with the router unreachable an edit still fails open under the default budget of 0', async () => {
  const w = gateWorld({}, { health: 'down' })
  expect(await w.call({ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL })).toEqual({ result: 'ran' })
})

test('small edits with orchestratorEditBudget 2: 2 per user turn, the next is refused; a new user prompt resets the budget', async () => {
  const w = gateWorld({ orchestratorEditBudget: 2 })
  const small = { tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL }
  expect(await w.call(small)).toEqual({ result: 'ran' })
  expect(await w.call({ tool: 'Write', file_path: '/b', content: SMALL })).toEqual({ result: 'ran' })
  const third: any = await w.call({ tool: 'MultiEdit', file_path: '/a', edits: [{ old_string: 'a', new_string: SMALL }] })
  expect(third.deny).toMatch(/already made 2 small edits this turn/)
  expect(isDeny(await w.call(small))).toBe(true)
  // A meta prompt (not typed by the person) is not a new user turn.
  await w.h['prompt.submit'](w.$, { text: 'x', isMeta: true }, async () => ({}))
  expect(isDeny(await w.call(small))).toBe(true)
  await w.h['prompt.submit'](w.$, { text: 'next request' }, async () => ({}))
  expect(await w.call(small)).toEqual({ result: 'ran' })
  expect(await w.call(small)).toEqual({ result: 'ran' })
  expect(isDeny(await w.call(small))).toBe(true)
})

test('a big edit is refused at once, even as the first edit of the turn, and does not use the budget', async () => {
  const w = gateWorld({ orchestratorEditBudget: 2 })
  const r: any = await w.call({ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: BIG })
  expect(r.deny).toMatch(/is a big edit \(over 40 lines or 2000 characters/)
  expect(await w.call({ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL })).toEqual({ result: 'ran' })
  expect(await w.call({ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL })).toEqual({ result: 'ran' })
})

test('orchestratorEditBudget is configurable; 0 refuses every edit', async () => {
  const w = gateWorld({ orchestratorEditBudget: 0 })
  const r: any = await w.call({ tool: 'Edit', file_path: '/a', old_string: 'a', new_string: SMALL })
  expect(r.deny).toMatch(/orchestratorEditBudget is 0/)
  const w1 = gateWorld({ orchestratorEditBudget: 1 })
  expect(await w1.call({ tool: 'Edit', new_string: SMALL })).toEqual({ result: 'ran' })
  expect((await w1.call({ tool: 'Edit', new_string: SMALL }) as any).deny).toMatch(/already made 1 small edit this turn/)
  for (const bad of [-1, 'x', '']) expect(resolveEditBudget(bad)).toBe(0)
  expect(resolveEditBudget('3')).toBe(3)
  const w5 = gateWorld({ orchestratorEditBudget: 5 })
  for (let i = 0; i < 5; i++) expect(await w5.call({ tool: 'Edit', new_string: SMALL })).toEqual({ result: 'ran' })
  expect(isDeny(await w5.call({ tool: 'Edit', new_string: SMALL }))).toBe(true)
})

// ---- Fail open --------------------------------------------------------------------------------------------------

test('fail open when cortados-route is not in the session: the call runs and the status says the gate is off', async () => {
  const w = gateWorld({}, { tools: BASE_TOOLS })
  expect(await w.call({ tool: 'WebSearch', query: 'x' })).toEqual({ result: 'ran' })
  expect(await w.call({ tool: 'Bash', command: 'make test' })).toEqual({ result: 'ran' })
  expect(w.state.statuses.at(-1)).toBe('gate balanced; router unreachable: gate off (cortados-route tools not in this session)')
  expect(w.state.toasts.length).toBe(1) // once per outage, not per call
  expect(w.state.toasts[0]).toMatch(/router unreachable .*gate off/)
  expect(w.state.fetches).toEqual([]) // no health request without the tools
})

for (const health of ['down', 'throw', 'notok'] as const) {
  test(`fail open when the provider health check fails (${health}); the result is cached for 60 s`, async () => {
    const w = gateWorld({}, { health })
    expect(await w.call({ tool: 'WebFetch', url: 'u' })).toEqual({ result: 'ran' })
    expect(await w.call({ tool: 'NotebookEdit', new_source: 'x' })).toEqual({ result: 'ran' })
    expect(w.state.fetches).toEqual(['http://provider.test:8432/health'])
    expect(w.state.statuses.at(-1)).toMatch(/^gate balanced; router unreachable: gate off \(provider /)
    // Within the TTL nothing is re-checked; past it the provider is asked again and, healthy, the gate is back.
    w.state.now += GATE_HEALTH_TTL_MS - 1
    w.state.health = 'ok'
    expect(await w.call({ tool: 'WebSearch', query: 'q' })).toEqual({ result: 'ran' })
    expect(w.state.fetches.length).toBe(1)
    w.state.now += 1
    const r: any = await w.call({ tool: 'WebSearch', query: 'q' })
    expect(isDeny(r)).toBe(true)
    expect(w.state.fetches.length).toBe(2)
    expect(w.state.statuses.at(-1)).toBe('gate balanced; 1 denied')
  })
}

test('a healthy check is cached too: a burst of refusals costs one health request', async () => {
  const w = gateWorld()
  for (let i = 0; i < 5; i++) expect(isDeny(await w.call({ tool: 'WebSearch', query: 'q' }))).toBe(true)
  expect(w.state.fetches.length).toBe(1)
  expect(w.state.statuses.at(-1)).toBe('gate balanced; 5 denied')
})

test('a provider that hangs past the health timeout fails open', async () => {
  const w = gateWorld()
  w.$.http.fetch = () => new Promise(() => {})
  const r = await w.call({ tool: 'WebSearch', query: 'q' })
  expect(r).toEqual({ result: 'ran' })
  expect(w.state.statuses.at(-1)).toMatch(/health timed out/)
})

test('fail open on an unreadable tool list or no provider URL', async () => {
  const a = gateWorld()
  a.$.tool.list = async () => { throw new Error('x') }
  expect(await a.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
  const b = gateWorld({ providerUrl: 'not a url' })
  expect(await b.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
  expect(b.state.statuses.at(-1)).toMatch(/no provider URL/)
})

test('one deadline covers the whole availability check: a hanging tool discovery fails open', async () => {
  const w = gateWorld()
  w.$.tool.list = () => new Promise(() => {})
  const r = await w.call({ tool: 'WebSearch', query: 'q' })
  expect(r).toEqual({ result: 'ran' })
  expect(w.state.statuses.at(-1)).toMatch(/tool list timed out/)
  expect(w.state.fetches).toEqual([])
})

test('one deadline covers the whole availability check: a hanging provider-URL resolution fails open', async () => {
  const w = gateWorld({ providerUrl: '' }) // no configured URL: resolution runs a process to read the environment
  w.$.process.run = () => new Promise(() => {})
  const r = await w.call({ tool: 'WebSearch', query: 'q' })
  expect(r).toEqual({ result: 'ran' })
  expect(w.state.statuses.at(-1)).toMatch(/provider URL timed out/)
  expect(w.state.fetches).toEqual([])
})

test('the deadline is shared, not per step: slow discovery plus slow health still ends within one timeout', async () => {
  const w = gateWorld()
  const half = GATE_HEALTH_TIMEOUT_MS * 0.75
  w.$.tool.list = () => new Promise((res) => setTimeout(() => res(ROUTE_TOOLS.map((name) => ({ name }))), half))
  w.$.http.fetch = () => new Promise((res) => setTimeout(() => res({ ok: true, status: 200, text: '{"ok":true}' }), half))
  const t0 = Date.now()
  const r = await checkDelegation(w.$, 'http://provider.test:8432')
  expect(r).toEqual({ ok: false, reason: 'provider health timed out' })
  expect(Date.now() - t0).toBeLessThan(GATE_HEALTH_TIMEOUT_MS + 500)
})

// ---- The task watcher start_task hands back (watch_command) runs in the background ------------------------------

const TID = 't20261112234010-189eafa9'
const XCODE_PY = '/Applications/Xcode.app/Contents/Developer/usr/bin/python3'
const SCRIPT = '/Users/5425855/Ai/CortadOS-Router/cortados_router/child_tasks.py'
const WATCHERS = [
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID}`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID}; echo "exit $?"`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} t20261112234313-fcb66672 --timeout 7000 --interval 15 --stall 1800 ; echo "exit $?"`,
  `python3 ${SCRIPT} --watch --timeout 600 ${TID}`,
  `python3.12 -B cortados_router/child_tasks.py --watch ${TID}`,
  `env CORTADOS_TASKS_DIR='/Users/x/My Tasks' CORTADOS_EXEC_HOSTS_FILE=/Users/x/hosts.json '/opt/my py/bin/python3' -B ${SCRIPT} --watch ${TID}`,
]
const NOT_WATCHERS = [
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} && make`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID}; make test`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} | tee /tmp/x`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} > /tmp/out`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID}; echo "exit $?"; rm -rf /`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID}\nmake`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} & sleep 999`,
  `${XCODE_PY} -B ${SCRIPT} --watch $(make)`,
  `${XCODE_PY} -B ${SCRIPT} --watch`,
  `${XCODE_PY} -B ${SCRIPT} --watch --timeout 5`,
  `${XCODE_PY} -B ${SCRIPT} --watch not-a-task-id`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} --timeout abc`,
  `${XCODE_PY} -B ${SCRIPT} --cleanup-archive`,
  `${XCODE_PY} -B ${SCRIPT} --watch ${TID} --run x`,
  `${XCODE_PY} -B /tmp/evil.py --watch ${TID}`,
  `bash -c "python3 ${SCRIPT} --watch ${TID}"`,
  `make; python3 ${SCRIPT} --watch ${TID}`,
  `X=$(make) python3 ${SCRIPT} --watch ${TID}`,
]

test('the task watcher (watch_command, with or without the exit echo) runs in the background and with a long timeout', async () => {
  for (const command of WATCHERS) {
    expect([command, isTaskWatchCommand(command)]).toEqual([command, true])
    for (const extra of [{ run_in_background: true }, { timeout: 600_000 }, { run_in_background: true, timeout: 600_000 }, {}]) {
      const w = gateWorld()
      const r: any = await w.call({ tool: 'Bash', command, ...extra })
      expect([command, extra, r]).toEqual([command, extra, { result: 'ran' }])
    }
  }
})

test('anything chained to, piped from or substituted into the watcher is not exempt and is refused in the background', async () => {
  for (const command of NOT_WATCHERS) {
    expect([command, isTaskWatchCommand(command)]).toEqual([command, false])
    const w = gateWorld()
    const r: any = await w.call({ tool: 'Bash', command, run_in_background: true })
    expect([command, isDeny(r)]).toEqual([command, true])
  }
  // `&& make` in the foreground is still a build run
  const w = gateWorld()
  expect(isDeny(await w.call({ tool: 'Bash', command: `python3 ${SCRIPT} --watch ${TID} && make` }))).toBe(true)
})

// ---- Build/test detection follows quotes ---------------------------------------------------------------------------

test('a quoted pattern naming pytest or make is inspection, not a build run', async () => {
  const cmd = `ssh air "grep -E '(pytest|make) failed' /tmp/job.log"`
  expect(shellSegments(cmd)).toEqual([cmd])
  expect(isBuildOrTest(cmd)).toBe(false)
  expect(isBuildOrTest(`grep -E 'a|make' x; echo "b; pytest"`)).toBe(false)
  expect(isBuildOrTest(`echo "it's (make)" && ls`)).toBe(false)
  const w = gateWorld()
  expect(await w.call({ tool: 'Bash', command: cmd })).toEqual({ result: 'ran' })
})

test('claude plugin test is a test run; claude plugin validate is not', async () => {
  for (const c of [
    'claude plugin test integrations/claude-mod/cortados-routed-subagents',
    'cd /tmp/x && CORTADOS_HOME=/tmp/h CORTADOS_SCARCITY_REFRESH=off claude plugin test .',
    'claude plugin test',
  ]) {
    expect([c, isBuildOrTest(c)]).toEqual([c, true])
    const w = gateWorld()
    expect([c, isDeny(await w.call({ tool: 'Bash', command: c }))]).toEqual([c, true])
  }
  expect(isBuildOrTest('claude plugin validate .')).toBe(false)
  expect(isBuildOrTest('echo "claude plugin test"')).toBe(false)
})

test('unparseable quoting fails open (allowed), as the gate does on any doubt', async () => {
  for (const c of [`make "unterminated`, `pytest 'open`, 'make \\']) {
    expect([c, shellSegments(c), isBuildOrTest(c)]).toEqual([c, null, false])
  }
  // Quoting that parses still splits on the real separators
  expect(shellSegments(`a 'x;y' ; b "p|q" && c\\;d`)).toEqual([`a 'x;y' `, ` b "p|q" `, ` c\\;d`])
  expect(isBuildOrTest(`echo 'done' && make`)).toBe(true)
})

// ---- Who is never gated -------------------------------------------------------------------------------------------

test('a CortadOS child task (CORTADOS_CHILD_TASK set) and any subagent (agentId) are never gated', async () => {
  const child = gateWorld({}, { env: { CORTADOS_CHILD_TASK: '1' } })
  for (const [, e] of DENIED) expect(await child.call(e)).toEqual({ result: 'ran' })
  const main = gateWorld()
  for (const [, e] of DENIED) expect(await main.call({ ...e, agentId: 'agent-1' })).toEqual({ result: 'ran' })
  expect(main.state.fetches).toEqual([])
})

test('orchestratorMode off: no gate, whatever orchestratorGate says', async () => {
  const w = gateWorld({ orchestratorMode: false, orchestratorGate: 'strict' })
  for (const [, e] of DENIED) expect(await w.call(e)).toEqual({ result: 'ran' })
})

// ---- strict (and the orchestratorEnforce alias) -----------------------------------------------------------------

for (const opts of [{ orchestratorEnforce: true }, { orchestratorGate: 'strict' }]) {
  test(`strict via ${JSON.stringify(opts)}: today's allowlist cage, no fail-open`, async () => {
    const w = gateWorld(opts, { tools: BASE_TOOLS, health: 'down' })
    for (const t of ['Read', 'ToolSearch', ...ROUTE_TOOLS, 'mcp__ccd_turn__wrap_up']) expect(await w.call({ tool: t })).toEqual({ result: 'ran' })
    for (const e of [{ tool: 'Bash', command: 'ls' }, { tool: 'mcp__claude-in-chrome__navigate' }, { tool: 'Edit', new_string: SMALL }, { tool: 'Monitor' }]) {
      const r: any = await w.call(e)
      expect(r.deny).toMatch(/Orchestrator mode \(enforced\): .* does not run/)
      expect(r.deny).toContain(`/${GATE_COMMAND} balanced or off`)
    }
    expect(await w.call({ tool: 'Agent', prompt: 'p' })).toEqual({ deny: AGENT_REFUSAL })
    expect(w.state.fetches).toEqual([])
  })
}

// ---- The per-session escape hatch -------------------------------------------------------------------------------

test('/cortados-gate off persists for THIS session (survives a reload), not for the next session', async () => {
  const store: Record<string, unknown> = {}
  const w = gateWorld({}, { store })
  expect(isDeny(await w.call({ tool: 'WebSearch' }))).toBe(true)
  const off: any = await w.h[`/${GATE_COMMAND}`](w.$, { command: GATE_COMMAND, args: 'off' })
  expect(off.text).toBe('orchestrator gate off (this session; 1 denied)')
  expect((store[GATE_STORE_KEY] as any)['session-1'].level).toBe('off')
  expect(await w.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
  // A reload: a new mod instance on the same store and session id keeps it off.
  const again = gateWorld({}, { store })
  expect(await again.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
  // The next session starts at the configured level.
  const next = gateWorld({}, { store, sid: 'session-2' })
  expect(isDeny(await next.call({ tool: 'WebSearch' }))).toBe(true)
  // And /clear in the reloaded instance (a new id) reads again.
  again.state.sid = 'session-3'
  expect(isDeny(await again.call({ tool: 'WebSearch' }))).toBe(true)
})

test('/cortados-gate strict, balanced, no argument and a bad argument', async () => {
  const w = gateWorld()
  const cmd = (args: string) => w.h[`/${GATE_COMMAND}`](w.$, { command: GATE_COMMAND, args })
  expect((await cmd('')).text).toBe('orchestrator gate balanced (config)')
  expect((await cmd(' STRICT ')).text).toBe('orchestrator gate strict (this session)')
  expect(isDeny(await w.call({ tool: 'Bash', command: 'ls' }))).toBe(true)
  expect((await cmd('balanced')).text).toMatch(/^orchestrator gate balanced \(this session; 1 denied\)$/)
  expect(await w.call({ tool: 'Bash', command: 'ls' })).toEqual({ result: 'ran' })
  expect((await cmd('maybe')).text).toBe(`usage: /${GATE_COMMAND} off|balanced|strict`)
})

test('the escape hatch also lowers an orchestratorEnforce cage for one session', async () => {
  const w = gateWorld({ orchestratorEnforce: true })
  expect(isDeny(await w.call({ tool: 'Bash', command: 'ls' }))).toBe(true)
  await w.h[`/${GATE_COMMAND}`](w.$, { command: GATE_COMMAND, args: 'off' })
  expect(await w.call({ tool: 'Bash', command: 'make' })).toEqual({ result: 'ran' })
})

test('a failed store write keeps the level for this mod load and says so', async () => {
  const w = gateWorld()
  w.$.store.set = async () => { throw new Error('disk') }
  const r: any = await w.h[`/${GATE_COMMAND}`](w.$, { command: GATE_COMMAND, args: 'off' })
  expect(r.text).toMatch(/^orchestrator gate off \(this session; not saved, applies until the mod reloads\)$/)
  expect(await w.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
})

test('without a session id the override is held in memory only', async () => {
  const w = gateWorld({}, { sid: null })
  const r: any = await w.h[`/${GATE_COMMAND}`](w.$, { command: GATE_COMMAND, args: 'off' })
  expect(r.text).toMatch(/this session, unsaved; no session id, so not saved/)
  expect(await w.call({ tool: 'WebSearch' })).toEqual({ result: 'ran' })
})

test('the session override map keeps only the newest sessions and drops junk', async () => {
  let m: any = {}
  for (let i = 0; i < GATE_STORE_MAX_SESSIONS + 5; i++) m = withGateOverride(m, `s${i}`, 'off', i)
  expect(Object.keys(m).length).toBe(GATE_STORE_MAX_SESSIONS)
  expect(m.s0).toBe(undefined)
  expect(m[`s${GATE_STORE_MAX_SESSIONS + 4}`].level).toBe('off')
  expect(parseGateMap({ a: { level: 'OFF', at: 1 }, b: { level: 'nope' }, c: 5 })).toEqual({ a: { level: 'off', at: 1 } })
  expect(parseGateMap(null)).toEqual({})
  expect(parseGateMap([1])).toEqual({})
})

// ---- Pure helpers -------------------------------------------------------------------------------------------------

test('levels: balanced by default, enforce is the strict alias, a typo stays balanced, mode off is off', async () => {
  expect(configGateLevel({ orchestratorMode: true })).toBe('balanced')
  expect(configGateLevel({ orchestratorMode: true, orchestratorGate: 'Strict' })).toBe('strict')
  expect(configGateLevel({ orchestratorMode: true, orchestratorGate: 'off' })).toBe('off')
  expect(configGateLevel({ orchestratorMode: true, orchestratorGate: 'balancde' })).toBe('balanced')
  expect(configGateLevel({ orchestratorMode: true, orchestratorEnforce: true, orchestratorGate: 'off' })).toBe('strict')
  expect(configGateLevel({ orchestratorMode: false, orchestratorEnforce: true })).toBe('off')
  expect(parseGateLevel(' off ')).toBe('off')
  expect(parseGateLevel('')).toBe(null)
  expect(resolveEditBudget(undefined)).toBe(GATE_EDIT_BUDGET_DEFAULT)
  expect(resolveEditBudget('3')).toBe(3)
  expect(resolveEditBudget(-1)).toBe(GATE_EDIT_BUDGET_DEFAULT)
  expect(resolveEditBudget(0)).toBe(0)
  expect(resolveEditBudget(2.7)).toBe(2)
})

test('balancedClass and the status text', async () => {
  expect(balancedClass('Bash', { command: 'echo pytest' })).toBe(null)
  expect(balancedClass('Bash', { command: 'pytest -q' })).toBe('build')
  expect(balancedClass('Bash', { command: 'ls', run_in_background: true })).toBe('long-bash')
  expect(balancedClass('Bash', { command: 'ls', timeout: '600000' })).toBe('long-bash')
  expect(balancedClass('Write', { content: SMALL })).toBe('edit')
  expect(balancedClass('MultiEdit', { edits: [{ new_string: 'a'.repeat(1500) }, { new_string: 'b'.repeat(1500) }] })).toBe('big-edit')
  expect(balancedClass('Agent', {})).toBe(null) // the Agent hook refuses it
  expect(gateStatusText('balanced', 0, null)).toBe(undefined)
  expect(gateStatusText('off', 3, 'x')).toBe(undefined)
  expect(gateStatusText('balanced', 2, null)).toBe('gate balanced; 2 denied')
  expect(gateStatusText('balanced', 1, 'provider unreachable')).toBe('gate balanced; router unreachable: gate off (provider unreachable); 1 denied')
  expect(gateDenyMessage('long-bash', 'Bash', 2)).toMatch(/timeout over 120 s/)
})

test('the gate status and the guard counter share the status line', async () => {
  const w = gateWorld({ mainSessionGuard: true })
  expect(isDeny(await w.call({ tool: 'WebSearch' }))).toBe(true)
  await w.h.Edit(w.$, { tool: 'Edit', new_string: BIG }, w.next) // the guard hook alone (as if the gate had let it pass)
  expect(w.state.statuses.at(-1)).toBe('gate balanced; 1 denied · main did 1 big edit; delegate with start_task')
})

// ---- Through the engine's test host: the gate is the outer hook --------------------------------------------------

/** The engine beneath the plugin, with cortados-route in the tool pool and a healthy provider. */
function engine(on: any, opts: { healthy?: boolean } = {}) {
  const ran: any[] = []
  const statuses: (string | undefined)[] = []
  const toasts: string[] = []
  mock.clock(on, { now: 1_000_000 })
  const store: Record<string, unknown> = {}
  on('store.get', (_$: any, e: any) => ({ value: store[e.key] }))
  on('store.set', (_$: any, e: any) => { store[e.key] = e.value; return { value: undefined } })
  on('session.id', () => ({ value: 'engine-session' }))
  on('env.get', () => ({ value: undefined }))
  on('tool.list', () => ({ value: [...BASE_TOOLS, ...ROUTE_TOOLS].map((name) => ({ name, description: name, mcp: name.startsWith('mcp__') })) }))
  on('http.fetch', () => ({ value: opts.healthy === false ? { ok: false, status: 503, headers: {}, text: '' } : { ok: true, status: 200, headers: {}, text: '{"ok":true}' } }))
  on('process.run', () => ({ value: { stdout: '', stderr: '', exitCode: 0 } }))
  on('tool.call', (_$: any, e: any) => { ran.push(e); return { result: { ok: true } } })
  on('ui.toast', (_$: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('ui.status', (_$: any, e: any) => { statuses.push(e.text); return { value: undefined } })
  on('command.register', (_$: any, e: any) => ({ value: { command: e.name } }))
  on('session.start', (_$: any, e: any) => ({ cwd: e.cwd }))
  return { ran, statuses, toasts, store }
}

const ENGINE = { options: { providerUrl: 'http://provider.test:8432', officeView: false } } as any

test('engine host: a refused big edit never reaches the tool or the warn guard\'s counter', ENGINE, async ($: any, on: any) => {
  const w = engine(on)
  const r: any = await $.tool.call({ tool: 'Edit', file_path: '/tmp/x', old_string: 'a', new_string: BIG })
  expect(typeof r.deny).toBe('string')
  expect(r.deny).toContain('mcp__cortados-route__start_task')
  expect(w.ran).toEqual([])
  expect(w.statuses.at(-1)).toBe('gate balanced; 1 denied')
  expect(w.statuses.some((s) => String(s).includes('main did'))).toBe(false)
  const ok: any = await $.tool.call({ tool: 'Bash', command: 'git status' })
  expect(ok.deny).toBe(undefined)
  expect(w.ran.length).toBe(1)
})

test('engine host: an unhealthy provider fails open and the big edit is counted by the guard', ENGINE, async ($: any, on: any) => {
  const w = engine(on, { healthy: false })
  const r: any = await $.tool.call({ tool: 'Edit', file_path: '/tmp/x', old_string: 'a', new_string: BIG })
  expect(r.deny).toBe(undefined)
  expect(w.ran.length).toBe(1)
  expect(w.statuses.at(-1)).toBe('gate balanced; router unreachable: gate off (provider health HTTP 503) · main did 1 big edit; delegate with start_task')
})

test('engine host: the edit budget holds through the host, and /cortados-gate off lifts it', ENGINE, async ($: any, on: any) => {
  const w = engine(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  const small = { tool: 'Edit', file_path: '/tmp/x', old_string: 'a', new_string: SMALL } as any
  expect(typeof ((await $.tool.call(small)) as any).deny).toBe('string') // budget 0 by default
  const off: any = await $.command.run({ command: GATE_COMMAND, args: 'off' } as any)
  expect(off.text).toMatch(/^orchestrator gate off \(this session/)
  expect(((await $.tool.call(small)) as any).deny).toBe(undefined)
  expect(w.ran.length).toBe(1)
})

// ---- The Agent tool: refused on the main loop unless the owner asks to fork this turn (owner 2026-11-13) ----------

const owner = (text: string, kind = 'composer', extra: Record<string, unknown> = {}) => ({ text, origin: { kind }, wait: false, ...extra })
const FORK_CALL = { tool: 'Agent', subagent_type: 'fork', prompt: 'try the other approach', description: 'fork' }
const GP_CALL = { tool: 'Agent', subagent_type: 'general-purpose', prompt: 'build it', description: 'build' }

/** The Agent hook (registered first, the outer one) chained into the matcher-less gate, as the engine runs them. */
async function agentCall(w: ReturnType<typeof gateWorld>, e: Record<string, unknown>) {
  return w.h.Agent(w.$, e, (x: any) => w.h['*'](w.$, x, w.next))
}
async function submit(w: ReturnType<typeof gateWorld>, e: Record<string, unknown>) {
  return w.h['prompt.submit'](w.$, e, async () => ({}))
}

test('isForkRequest: the word fork in any case, not inside another word', () => {
  for (const t of ['fork a session', '/fork', 'Fork this', 'please FORK', 'forking is fine', 'fork-it', 'try (fork)']) expect([t, isForkRequest(t)]).toEqual([t, true])
  for (const t of ['pitchfork', 'forklift', 'forkbomb', 'nothing here', '', undefined, 42]) expect([t, isForkRequest(t)]).toEqual([t, false])
})

test('isOwnerPrompt: owner origins and no origin count; meta and every other origin do not', () => {
  for (const k of ['composer', 'bridge', 'sdk', 'slack-ping']) expect(isOwnerPrompt(owner('x', k))).toBe(true)
  expect(isOwnerPrompt({ text: 'x' })).toBe(true)
  expect(isOwnerPrompt({ text: 'x', isMeta: true })).toBe(false)
  expect(isOwnerPrompt(owner('x', 'composer', { isMeta: true }))).toBe(false)
  for (const k of ['task-notification', 'scheduled-trigger', 'peer', 'peer-send-message', 'plugin', 'unclassified', 'coordinator', 'channel']) {
    expect(isOwnerPrompt(owner('x', k))).toBe(false)
  }
  expect(agentDecision(false, FORK_CALL)).toBe(AGENT_REFUSAL)
  expect(agentDecision(true, FORK_CALL)).toBe(null)
  expect(agentDecision(true, GP_CALL)).toBe(AGENT_FORK_ONLY)
})

test('Agent on the main loop is refused by default, whatever the subagent type; the message says how a fork is allowed', async () => {
  const w = gateWorld()
  await submit(w, owner('build the thing'))
  for (const e of [FORK_CALL, GP_CALL, { tool: 'Agent', prompt: 'p' }]) expect(await agentCall(w, e)).toEqual({ deny: AGENT_REFUSAL })
  expect(AGENT_REFUSAL).toContain('in-process subagents')
  expect(AGENT_REFUSAL).toContain('mcp__cortados-route__start_task')
  expect(AGENT_REFUSAL).toMatch(/saying "fork" in their message/)
  expect(w.state.ran).toEqual([])
})

test('the owner says fork this turn: a fork passes through to the native runner; another subagent type is told to fork', async () => {
  const w = gateWorld()
  await submit(w, owner('Fork a session and try the other approach'))
  expect(await agentCall(w, FORK_CALL)).toEqual({ result: 'ran' })
  expect(w.state.ran.map((e: any) => e.subagent_type)).toEqual(['fork'])
  expect(await agentCall(w, GP_CALL)).toEqual({ deny: AGENT_FORK_ONLY })
  // still the same turn: a second fork is fine
  expect(await agentCall(w, FORK_CALL)).toEqual({ result: 'ran' })
  // the desktop app (sdk origin) and /fork work too
  const w2 = gateWorld()
  await submit(w2, owner('/fork', 'sdk'))
  expect(await agentCall(w2, FORK_CALL)).toEqual({ result: 'ran' })
})

test('fork only in a system reminder, a notification, a tool result or a previous turn allows nothing', async () => {
  // meta prompt (system reminder) saying fork
  const meta = gateWorld()
  await submit(meta, owner('build it'))
  await submit(meta, { text: 'fork a session', isMeta: true, turnId: 't1' })
  expect(await agentCall(meta, FORK_CALL)).toEqual({ deny: AGENT_REFUSAL })
  // a task notification or peer message saying fork
  for (const kind of ['task-notification', 'peer', 'plugin', 'unclassified']) {
    const w = gateWorld()
    await submit(w, owner('fork a session', kind))
    expect([kind, await agentCall(w, FORK_CALL)]).toEqual([kind, { deny: AGENT_REFUSAL }])
  }
  // "fork" in the Agent call's own prompt (model-written, or quoted from a tool result) is not the owner's request
  const tr = gateWorld()
  await submit(tr, owner('review the diff'))
  expect(await agentCall(tr, { ...FORK_CALL, prompt: 'the owner said: fork a session' })).toEqual({ deny: AGENT_REFUSAL })
  // the previous turn said fork; this turn's owner prompt does not
  const prev = gateWorld()
  await submit(prev, owner('fork a session'))
  expect(await agentCall(prev, FORK_CALL)).toEqual({ result: 'ran' })
  await submit(prev, owner('now merge it'))
  expect(await agentCall(prev, FORK_CALL)).toEqual({ deny: AGENT_REFUSAL })
  // a notification that starts a turn of its own ends the fork permission; one delivered into the running turn keeps it
  const notif = gateWorld()
  await submit(notif, owner('fork a session'))
  await submit(notif, { text: 'task done', origin: { kind: 'task-notification' }, turnId: 'running' })
  expect(await agentCall(notif, FORK_CALL)).toEqual({ result: 'ran' })
  await submit(notif, { text: 'task done', origin: { kind: 'task-notification' } })
  expect(await agentCall(notif, FORK_CALL)).toEqual({ deny: AGENT_REFUSAL })
})

test('the owner-asked fork passes the strict cage too, and is refused again without the request', async () => {
  const w = gateWorld({ orchestratorGate: 'strict' })
  expect(await agentCall(w, FORK_CALL)).toEqual({ deny: AGENT_REFUSAL })
  expect(await w.call(FORK_CALL)).toEqual({ deny: AGENT_REFUSAL })
  await submit(w, owner('fork this'))
  expect(await agentCall(w, FORK_CALL)).toEqual({ result: 'ran' })
  expect(await agentCall(w, GP_CALL)).toEqual({ deny: AGENT_FORK_ONLY })
  expect(isDeny(await w.call({ tool: 'Bash', command: 'ls' }))).toBe(true)
})
