// 0.6.3: the simplified default view (owner 2026-11-22: "way too complicated, I don't know if it's right") and this
// machine's tasks that carry no session tag (started through a cortados-route server that knew no session, such as
// the Claude desktop app's own). Fakes only: no provider, no process is reached.
import { test, expect, mock } from 'claude-code/testing'
import { taskRow, taskRows, defaultTaskLines, localModelLine, subsLine, s1Line, machineTag, machineKey, fetchTree,
  sessionTag, readSessionView, redactQuery, FALLBACK, FINISHED_SHOWN, TITLE_MAX, NO_TASKS, UNTAGGED_HEAD, MACHINE_TAG_LABEL } from './register'

const TOKEN = 'sekrit-token-123'
const MINE = 'aaaaaaaaaaaaaaa1'
const OTHER = 'bbbbbbbbbbbbbbb2'
const ALIAS = 'ccccccccccccccc3'

const task = (id: string, status: string, extra: Record<string, unknown> = {}) => ({
  task_id: 't20261122150000-' + id.padStart(8, '0'), title: 'Task ' + id, host: 'air', model: 'gpt-6.1', status,
  state: status === 'done' ? 'succeeded' : status === 'waiting' ? 'queued' : status, started_at: 1000, elapsed_s: 60,
  ended_at: status === 'running' || status === 'waiting' ? null : 1060, ...extra })

test('one task, one line: title, host, model, state, elapsed (counting while it runs)', () => {
  expect(taskRow(task('1', 'running') as any, 5)).toEqual({ text: '● Task 1 · air · gpt-6.1 · running · 1m 05s', attn: false })
  expect(taskRow(task('2', 'waiting') as any, 5).text).toBe('◷ Task 2 · air · gpt-6.1 · waiting · 1m 05s')
  expect(taskRow(task('3', 'done') as any, 5).text).toBe('✓ Task 3 · air · gpt-6.1 · succeeded · 1m 00s')
  expect(taskRow(task('4', 'failed') as any, 5)).toEqual({ text: '✕ Task 4 · air · gpt-6.1 · failed · 1m 00s', attn: true })
  expect(taskRow(task('5', 'done', { state: 'cancelled' }) as any, 0).text.startsWith('⊘ ')).toBe(true)
  const long = taskRow(task('6', 'running', { title: 'x'.repeat(80), model: null }) as any, 0).text
  expect(long).toBe('● ' + 'x'.repeat(TITLE_MAX - 1) + '… · air · running · 1m 00s')
})

test('running first (newest first), then the last few finished, nested ones flattened, the rest counted', () => {
  const nestedLive = task('n', 'running', { started_at: 3000 })
  const g = { origin: MINE, tasks: [
    task('a', 'done', { ended_at: 1100 }),
    task('b', 'running', { started_at: 2000, tasks: [nestedLive] }),
    task('c', 'done', { ended_at: 1300 }), task('d', 'failed', { ended_at: 1400 }), task('e', 'done', { ended_at: 1200 }),
  ], tasks_hidden: 2 }
  const lines = taskRows([g as any], 0).map(l => l.text)
  expect(lines.map(t => t.split(' · ')[0])).toEqual(['● Task n', '● Task b', '✕ Task d', '✓ Task c', '✓ Task e',
    '  +3 more (dashboard)'])                                      // a (the oldest finished) + 2 the provider hid
  expect(lines.length).toBe(2 + FINISHED_SHOWN + 1)
  // the same task in two groups (own and alias) is drawn once
  expect(taskRows([{ tasks: [task('z', 'running')] } as any, { tasks: [task('z', 'running')] } as any], 0).length).toBe(1)
})

test('the task block: this session, its alias tag, and this machine\'s untagged tasks under their own heading', () => {
  const view = { session_filter: MINE, orchestrators: { status: 'ok', errors: [], items: [
    { origin: MINE, kind: 'session', tasks: [task('1', 'running')] },
    { origin: null, kind: 'untagged', device: 'Forests-MacBook-Pro', tasks: [task('2', 'running', { title: 'pstack plugin deep-read' })] },
    // never drawn: another session's group, even if a provider sent it
    { origin: OTHER, kind: 'session', tasks: [task('3', 'running', { title: 'Their secret plan' })] },
  ] } }
  const alias = { session_filter: ALIAS, orchestrators: { status: 'ok', items: [{ origin: ALIAS, tasks: [task('4', 'done')] }] } }
  const text = defaultTaskLines(view as any, MINE, 0, alias as any, ALIAS).map(l => l.text)
  expect(text).toEqual([
    '● Task 1 · air · gpt-6.1 · running · 1m 00s',
    '✓ Task 4 · air · gpt-6.1 · succeeded · 1m 00s',
    UNTAGGED_HEAD + ' (Forests-MacBook-Pro)',
    '● pstack plugin deep-read · air · gpt-6.1 · running · 1m 00s',
  ])
  expect(text.join('\n').includes('Their secret plan')).toBe(false)
  // nothing of this session's own: said, and the untagged tasks still show (the 2026-11-22 desktop-app case)
  const none = { orchestrators: { status: 'ok', items: [view.orchestrators.items[1]] } }
  expect(defaultTaskLines(none as any, MINE, 0).map(l => l.text)).toEqual([NO_TASKS, UNTAGGED_HEAD + ' (Forests-MacBook-Pro)',
    '● pstack plugin deep-read · air · gpt-6.1 · running · 1m 00s'])
  expect(defaultTaskLines({ orchestrators: { status: 'ok', items: [] } } as any, MINE, 0).map(l => l.text)).toEqual([NO_TASKS])
  expect(defaultTaskLines({ orchestrators: { status: 'error', errors: [{ source: 'child-tasks.jsonl', error: 'OSError' }] } } as any, MINE, 0))
    .toEqual([{ text: 'tasks unavailable: child-tasks.jsonl: OSError', attn: true }])
})

test('the local model in one line: on and in use, off and why, unreachable, nothing yet', () => {
  const pro = { node: 'pro', reachable: true, switch: 'on', allowed: true, models_loaded: ['qwen3.5-27b'] }
  expect(localModelLine({ nodes: [{ node: 'studio', reachable: true, switch: 'on', allowed: true }, pro] } as any, null))
    .toBe('local pro: on · in use · qwen3.5-27b')
  expect(localModelLine({ nodes: [{ ...pro, switch: 'off', allowed: false, reason: 'switched off by the owner' }] } as any, null))
    .toBe('local pro: off · not used · switched off by the owner')
  expect(localModelLine({ nodes: [{ ...pro, allowed: false, reason: 'on battery' }] } as any, null)).toBe('local pro: on · not used · on battery')
  expect(localModelLine({ nodes: [{ node: 'pro', reachable: false, error: 'gate down' }] } as any, null)).toBe('local pro: unreachable · gate down')
  expect(localModelLine({ nodes: [{ node: 'studio', reachable: true, switch: 'on', allowed: true }] } as any, null)).toBe('local studio: on · in use')
  expect(localModelLine({ nodes: [], note: 'no local node is configured' } as any, null)).toBe('local model: no local node is configured')
  expect(localModelLine(null, 'local node status needs the owner token')).toBe('local model: local node status needs the owner token')
  expect(localModelLine(null, null)).toBe('local model: loading…')
})

test('subscriptions in one line: only the lowest left and the ones that are out', () => {
  const snap = { subscriptions: { claude: { ok: false, why: 'week 100% used' }, 'claude#b': { ok: true, scarcity: 0 },
    codex: { ok: true, scarcity: 0 }, local: { ok: false }, cursor: null },
    readings: { 'claude#b': { windows: [{ window: 'week (all models)', headroom_pct: 55 }, { window: '5h', headroom_pct: 80 }] },
      codex: { windows: [{ window: 'week', used_pct: 70 }] } } }
  expect(subsLine(snap as any)).toBe('subscriptions: lowest codex 30% left (week) · OUT: claude')
  expect(subsLine({ subscriptions: { codex: { ok: true } }, readings: {} } as any)).toBe('subscriptions: no quota known')
  // without /resources' windows, the live view's lowest
  expect(subsLine({ subscriptions: {}, readings: {} } as any, { lane: 'cursor', label: 'cursor', left_pct: 70, window: 'month (x)' }))
    .toBe('subscriptions: lowest cursor 70% left (month)')
  expect(subsLine(null)).toBe('subscriptions: loading…')
})

test('S1 in one line: up with the median, down with the reason', () => {
  expect(s1Line({ overview: { s1: 'up' }, s1: { latency_ms: 20 } } as any, { s1_latency_ms: { median: 31 } } as any)).toBe('S1 up · 31 ms median')
  expect(s1Line({ overview: { s1: 'up' }, s1: { latency_ms: 20 } } as any, null)).toBe('S1 up · 20 ms (last probe)')
  expect(s1Line({ overview: { s1: 'down' }, s1: { reason: 'connection refused' } } as any)).toBe('S1 DOWN · connection refused')
  expect(s1Line(null)).toBe('S1 unknown')
})

// review round 1: the untagged group is opened by this machine's tag (a keyed hash of its 0600 Office salt), never by
// a hostname: the provider returns only tasks recorded with exactly that tag
const SALT_V = '0123456789abcdef0123456789abcdef'
const MACHINE_V = '522d8f041e1bc8d0'   // python3 -c "from cortados_router import subagent_mcp as m; print(m.machine_tag(SALT))"

test('the machine tag is subagent_mcp.machine_tag of the salt, never a session tag', async () => {
  expect(await machineTag(SALT_V)).toBe(MACHINE_V)
  expect(MACHINE_TAG_LABEL).toBe('cortados:machine')
  expect(await machineTag('f'.repeat(32))).not.toBe(MACHINE_V)
  expect(await sessionTag(SALT_V, 'machine')).not.toBe(MACHINE_V)
})

function saltFake(salt: string | null, reads: string[] = []) {
  const state = new Map<string, unknown>()
  return {
    env: { get: async (n: string) => n === 'HOME' ? '/tmp/pulse-home' : n === 'CORTADOS_PROVIDER_URL' ? 'http://air.example:8432' : undefined },
    fs: { read: async (p: string) => { reads.push(p); if (salt !== null && p === '/tmp/pulse-home/.config/cortados/office-salt') return salt
      throw new Error('ENOENT') }, stat: async () => { throw new Error('ENOENT') } },
    process: { run: async () => { throw new Error('the pane runs no process for this') } },
    state: { get: async (r: any) => ({ value: state.get(r.key), version: 0 }),
      set: async (r: any, v: unknown) => { state.set(r.key, v); return { isSet: true, version: 1 } } },
    clock: { now: async () => 1_700_000_000_000, every: () => ({ cancel() {} }), after: () => ({ cancel() {} }) },
  }
}

test('this machine\'s tag comes from its salt, once; no salt or a bad salt is no tag', async () => {
  const reads: string[] = []
  const $ = saltFake(SALT_V, reads)
  expect(await machineKey($ as any)).toBe(MACHINE_V)
  expect(await machineKey($ as any)).toBe(MACHINE_V)
  expect(reads.length).toBe(1)                                   // computed once, then held
  expect(await machineKey(saltFake(null) as any)).toBe(null)
  expect(await machineKey(saltFake('not-a-salt') as any)).toBe(null)
})

test('the session view is asked with &machine= when the salt is readable, never with a hostname', async () => {
  for (const [salt, want] of [[SALT_V, '&machine=' + MACHINE_V], [null, '']] as const) {
    const urls: string[] = []
    const $ = { ...saltFake(salt), http: { fetch: async (url: string) => { urls.push(url)
      return { status: 200, ok: true, headers: {}, text: JSON.stringify({ session_filter: MINE, orchestrators: { status: 'ok', items: [] } }) } } } }
    expect(await fetchTree($ as any, { ...FALLBACK, providerToken: TOKEN }, MINE)).toBe(true)
    expect(urls).toEqual(['http://air.example:8432/v1/cortados/live/session?session=' + MINE + want])
    expect(urls[0]!.includes('device=')).toBe(false)
    if (salt) expect(urls[0]!.includes(salt)).toBe(false)        // the salt itself never leaves the machine
  }
})

test('a failed read never shows the machine tag or the session tag: a host error echoing the URL is redacted', async () => {
  // pulse review round 2: &machine=<tag> is a capability; a fetch error that quotes the URL must not put it on screen
  const echo = { ...saltFake(SALT_V), http: { fetch: async (url: string) => { throw new Error('connect ECONNREFUSED ' + url) } } }
  const r = await readSessionView(echo as any, { ...FALLBACK, providerToken: TOKEN }, MINE, MACHINE_V)
  expect(r.kind).toBe('failed')
  const problem = (r as any).problem as string
  expect(problem.includes(MACHINE_V)).toBe(false)
  expect(problem.includes(MINE)).toBe(false)
  expect(problem).toContain('machine=<redacted>')
  // an answer that is not a JSON object: the error names the path only, never its query
  const odd = { ...saltFake(SALT_V), http: { fetch: async () => ({ status: 200, ok: true, headers: {}, text: '[1]' }) } }
  const r2 = await readSessionView(odd as any, { ...FALLBACK, providerToken: TOKEN }, MINE, MACHINE_V)
  expect(r2.kind).toBe('failed')
  expect((r2 as any).problem).toBe('could not read the provider (/v1/cortados/live/session did not return a JSON object)')
  expect(redactQuery('GET /x?session=' + MINE + '&machine=' + MACHINE_V + ' HTTP/1.1'))
    .toBe('GET /x?session=<redacted>&machine=<redacted> HTTP/1.1')
  expect(redactQuery('no query here')).toBe('no query here')
  // the whole value goes, punctuation included (pulse review round 3): nothing after `=` survives before `&`, `#`,
  // whitespace or the end
  expect(redactQuery('could not read the provider (GET /x?token=abc)SECRET)')).toBe('could not read the provider (GET /x?token=<redacted>')
  expect(redactQuery('/x?a=1]S2,S3;S4\'S5"S6&b=x)y#frag')).toBe('/x?a=<redacted>&b=<redacted>#frag')
  expect(redactQuery('/x?a=v)S1 tail')).toBe('/x?a=<redacted> tail')
})

test('an older provider that ignores &machine= answers without the untagged group: only the session\'s own tasks', () => {
  const v = { session_filter: MINE, orchestrators: { status: 'ok', items: [] } } as any
  expect(defaultTaskLines(v, MINE, 0).map(l => l.text)).toEqual([NO_TASKS])
})

test('the mounted pane shows this machine\'s untagged tasks when this session has none', { options: { providerUrl: 'http://air.example:8432', providerToken: TOKEN } }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const SALT = '0123456789abcdef0123456789abcdef'
  const SID = '11111111-2222-3333-4444-555555555555'
  const tag = await sessionTag(SALT, SID)
  on('ui.open', async () => ({ value: { isPlaced: true } }))
  on('session.id', async () => ({ value: SID }))
  on('env.get', async (_$, e) => ({ value: e.name === 'HOME' ? '/tmp/pulse-home' : undefined }))
  on('fs.read', async (_$, e) => {
    if (e.path === '/tmp/pulse-home/.config/cortados/office-salt') return { value: SALT }
    throw new Error('ENOENT')
  })
  on('fs.stat', async () => { throw new Error('ENOENT') })
  on('process.run', async () => ({ value: { exitCode: 1, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  const urls: string[] = []
  on('http.fetch', async (_$, e) => {
    urls.push(e.url)
    const view = { session_filter: tag, overview: { s1: 'up' }, s1: { latency_ms: 9 }, orchestrators: { status: 'ok', errors: [],
      items: [{ origin: null, kind: 'untagged', device: 'Forests-MacBook-Pro',
        tasks: [{ task_id: 't20261122152600-6a026c4f', title: 'pstack plugin deep-read', host: 'air', model: 'gpt-6.1-sol',
          status: 'running', state: 'running', started_at: 1_799_999_900, elapsed_s: 100 }] }] } }
    const body = e.url.includes('/v1/cortados/live/session') ? view : e.url.endsWith('/v1/cortados/local') ? { nodes: [] } : { subscriptions: {}, readings: {} }
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(body) } }
  })
  await $.command.run({ command: 'cortados-pulse', args: '' } as any)
  await clock.advance(1)
  const ui = await $.ui.mount({ plugin: 'cortados-pulse', surface: 'terminal', component: 'Pane', requestId: 'cortados-pulse',
    props: { title: 'CortadOS pulse', isFocused: false, bodyColumns: 100, placement: 'dock' } as any })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  expect(texts.slice(0, 4)).toEqual(["This session's child tasks", NO_TASKS, UNTAGGED_HEAD + ' (Forests-MacBook-Pro)',
    '● pstack plugin deep-read · air · gpt-6.1-sol · running · 1m 40s'])
  const mtag = await machineTag(SALT)
  expect(urls.some(u => u.endsWith('/v1/cortados/live/session?session=' + tag + '&machine=' + mtag))).toBe(true)
  expect(urls.some(u => u.includes('device='))).toBe(false)
  expect(texts.join('\n').includes(TOKEN)).toBe(false)
  await ui.unmount()
})
