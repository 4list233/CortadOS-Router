// #127: this session's work split, the local nodes' real state, and the tasks a start_task of this session recorded
// under another tag. The session view is the provider-generated fixture (tests/test_orchestrator_tree.session_fixture).
import { test, expect, mock } from 'claude-code/testing'
import { workSplitLines, tokAmt, nodeLines, localNodeLines, deviceLines, laneLines, startedOf, aliasTag, fetchAlias,
  fetchBounded, outstandingReads, FETCH_TIMEOUT_MS, START_TASK_TOOL, register, sessionTag, LOCAL_SWITCH_PATH, ALIAS_NOTE,
  aliasLines, fetchTree, treeLines, STALE_AFTER_MS } from './register'
import sessionFixture from './session.fixture'

const TOKEN = 'sekrit-token-123'
const MINE = 'aaaaaaaaaaaaaaa1'
const OTHER = 'bbbbbbbbbbbbbbb2'
const WS = (sessionFixture as any).work_split

/** The Pro as GET /v1/cortados/local reported it while the old pane said "local: unavailable" and "pro unconfigured". */
const PRO = { node: 'pro', label: 'MacBook Pro', reachable: true, error: null, switch: 'on', allowed: true,
  reason: 'switch on, AC power', power: 'ac', training: false, ollama: 'up', backend: 'vllm-mlx',
  models_loaded: ['qwen3.5-27b-mlx-4bit'], models: ['qwen3.5-27b'], auto_route: null,
  capacity: { limit: 1, inflight: 0, owner: 'active', pressure: 'warn', reason: 'limit 1: memory pressure warn',
    reserved_bytes: 0, free_bytes: 25 * 2 ** 30, fit_tokens: 60000, free_tokens: 60000 } }

test('the compact work split line: routed share on fresh tokens, top routes, native by model, the all-token share', () => {
  expect(WS.session).toBe(MINE)
  const [first] = workSplitLines(WS, false)
  expect(first!.text).toBe('work split 24h: routed 66% fresh (1.4M tok: codex/gpt-6.1-sol 0.8M, claude#b/claude-opus-5-5 0.5M, …) · ' +
    'native 0.7M (claude-opus-5-5, claude-haiku-5) · 15% counting cache reads')
  expect(workSplitLines(WS, false).length).toBe(1)                     // closed: one line, the fixture has no notes
  expect([tokAmt(950), tokAmt(25_000), tokAmt(800_000), tokAmt(1_375_000), tokAmt(null)]).toEqual(['950', '25k', '0.8M', '1.4M', '—'])
})

test('the breakdown: every route, how it was handed over, native by model, the four token kinds, the other window', () => {
  const all = workSplitLines(WS, true).map(l => l.text)
  for (const want of ['  routed by route (fresh):', '    codex/gpt-6.1-sol 0.8M · 1 conv', '    claude#b/claude-haiku-5 25k · 1 conv',
    '  routed by how: child tasks 1.3M · route_task 25k · routed subagents 50k',
    '  routed tokens: in 1.0M · out 0.3M · cache write 80k · cache read 4.0M',
    '    claude-opus-5-5 0.7M · in 45k · out 0.2M · cache write 0.4M · cache read 29.0M',
    '  7d: routed 66% fresh · 15% counting cache reads · routed 1.4M · native 0.7M',
    '  joined on the session tag · native from pro']) expect(all).toContain(want)
  // metadata only: no other session, no task id, no child's own tag
  const raw = JSON.stringify(workSplitLines(WS, true))
  for (const bad of [OTHER, 'Grounds', 'c0ffeec0ffee', 't2026']) expect(raw.includes(bad)).toBe(false)
})

test('partial data is said: no native report, a stale heartbeat, an older provider, nothing yet', () => {
  const stale = { ...WS, status: 'partial', row: { ...WS.row, native_stale: true },
    notes: ['native tokens are 60 min old: the pro heartbeat is stale'] }
  const lines = workSplitLines(stale, false)
  expect(lines[1]).toEqual({ text: '  native tokens are 60 min old: the pro heartbeat is stale', dim: true })
  const routedOnly = { ...WS, row: { ...WS.row, match: 'routed only', '24h': { ...WS.row['24h'], native: null, routed_pct: null, routed_pct_all: null } },
    notes: ["this session's own (native) tokens are not reported: its machine sends no orchestrator heartbeat"] }
  const r = workSplitLines(routedOnly, true).map(l => l.text)
  expect(r[0]).toBe('work split 24h: routed (1.4M tok: codex/gpt-6.1-sol 0.8M, claude#b/claude-opus-5-5 0.5M, …) · native not reported')
  expect(r).toContain('  no native report for this tag · native from pro')
  expect(workSplitLines(undefined, false)).toEqual([{ text: 'work split: this provider reports no per-session split yet (update it)', dim: true }])
  expect(workSplitLines({ status: 'none', row: null, notes: ['no routed work recorded for this session'] }, false))
    .toEqual([{ text: 'work split: nothing recorded for this session yet' }, { text: '  no routed work recorded for this session', dim: true }])
})

test('the local nodes as their gates report them, not the resource-pool sample', () => {
  expect(nodeLines(PRO as any)).toEqual([
    'pro: switch on · allowed · AC',
    '  switch on, AC power',
    '  vllm-mlx up · loaded qwen3.5-27b-mlx-4bit',
    '  0/1 in flight · owner active · pressure warn · 25.0 GiB free',
    '  limit 1: memory pressure warn',
  ])
  expect(nodeLines({ node: 'studio', reachable: false, error: 'local node studio unreachable (TimeoutError): it may be asleep' } as any))
    .toEqual(['studio: unreachable · local node studio unreachable (TimeoutError): it may be asleep'])
  expect(nodeLines({ node: 'pro', reachable: true, switch: 'off', allowed: false, reason: 'switch off', power: 'battery',
    backend: 'ollama', ollama: 'unreachable', models_loaded: [], capacity: null } as any))
    .toEqual(['pro: switch off · NOT allowed · battery', '  switch off', '  ollama unreachable · no model loaded'])
  expect(localNodeLines({ nodes: [], note: 'no local node is configured on the provider (CORTADOS_LOCAL_RESOURCES_CONFIG)' }, null))
    .toEqual(['no local node is configured on the provider (CORTADOS_LOCAL_RESOURCES_CONFIG)'])
  expect(localNodeLines(null, 'local node status needs the owner token (a scoped pane token cannot read it)'))
    .toEqual(['local node status needs the owner token (a scoped pane token cannot read it)'])
  expect(localNodeLines(null, null)).toEqual(['loading the local nodes…'])
  // the old lines, kept for what they are: the resource-pool probe and the scarcity cache's deployment lane
  expect(deviceLines({ status: 'ok', nodes: [{ id: 'pro', status: 'unconfigured', gpu_headroom_estimate_bytes: null, fits: [] }],
    pool_headroom_estimate_bytes: 0, pool_fits: [] } as any)).toEqual(['pro: not probed (no SSH target in the resource-pool config)'])
  expect(laneLines('local', { ok: false, why: 'no verified local deployment currently available' }, undefined, 0))
    .toEqual(['local (resource-pool deployments): none verified · see Local nodes for the gate'])
})

test('a start_task result names the origin its task was recorded under; another tag than the pane\'s is followed', () => {
  const text = JSON.stringify({ task_id: 't20261116160946-4437347b', state: 'running', host: 'air', origin_session: OTHER })
  expect(startedOf(text)).toEqual({ tag: OTHER, task_id: 't20261116160946-4437347b' })
  for (const bad of ['not json', JSON.stringify({ task_id: 't1', origin_session: OTHER }), JSON.stringify({ task_id: 't20261116160946-4437347b', origin_session: 'grokbot' }),
    JSON.stringify({ task_id: 't20261116160946-4437347b', origin_session: null }), null]) expect(startedOf(bad)).toBe(null)
  const list = [{ tag: MINE, task_id: 'a', at: 2 }, { tag: OTHER, task_id: 'b', at: 1 }]
  expect(aliasTag(list, MINE)).toBe(OTHER)
  expect(aliasTag(list.slice(0, 1), MINE)).toBe(null)
  for (const name of ['mcp__cortados-route__start_task', 'mcp__plugin_cortados_cortados-route__start_task'])
    expect(START_TASK_TOOL.test(name)).toBe(true)
  for (const name of ['mcp__cortados-route__task_status', 'mcp__other__start_task']) expect(START_TASK_TOOL.test(name)).toBe(false)
  expect(ALIAS_NOTE(OTHER)).toMatch(/^tasks this session started under its cortados-route server's tag bbbbbbbb/)
})

/** A minimal `$` for the hooks: env, fs, http, state, clock (with timers). */
function dollar(http: (url: string, init?: any) => any) {
  const state = new Map<string, any>()
  const key = (ref: any) => ref.plugin + '/' + ref.key
  const urls: string[] = []
  const timers: { ms: number; fn: () => void; cancelled: boolean }[] = []
  return {
    urls, timers,
    env: { get: async (n: string) => ({ HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' } as any)[n] },
    fs: { read: async (p: string) => { if (p === '/tmp/home/.config/cortados/provider-token') return TOKEN; throw new Error('ENOENT') },
      stat: async () => { throw new Error('ENOENT') } },
    process: { run: async () => ({ exitCode: 1, stdout: '', stderr: '' }) },
    http: { fetch: async (url: string, init?: any) => { urls.push(url); return http(url, init) } },
    state: {
      get: async (ref: any) => state.get(key(ref)) ?? { value: undefined, version: 0 },
      set: async (ref: any, value: unknown) => { const v = (state.get(key(ref))?.version ?? 0) + 1; state.set(key(ref), { value, version: v }); return { isSet: true, version: v } },
    },
    clock: { now: async () => 1_700_000_000_000, every: () => ({ cancel() {} }),
      after: (ms: number, fn: () => void) => { const t = { ms, fn, cancelled: false }; timers.push(t); return { cancel: () => { t.cancelled = true } } } },
    value: (k: string) => state.get('cortados-pulse/' + k)?.value,
  }
}

test('the tool.call hook keeps the origin a start_task named, reads the live view at once, and passes the result on', async () => {
  const hooks: Record<string, any> = {}
  register(((event: string, a: any, b?: any) => { hooks[event] = b ?? a; return { catch() {} } }) as any, {})
  const $ = dollar(() => ({ status: 404, ok: false, headers: {}, text: '' }))
  const result = { text: JSON.stringify({ task_id: 't20261116160946-4437347b', origin_session: OTHER }) }
  const out = await hooks['tool.call']($, { tool: 'mcp__cortados-route__start_task' }, async () => result)
  expect(out).toBe(result)
  expect($.value('started')).toEqual([{ tag: OTHER, task_id: 't20261116160946-4437347b', at: 1_700_000_000_000 }])
  for (let i = 0; i < 40; i++) await Promise.resolve()
  expect($.urls.some(u => u.includes('/v1/cortados/live'))).toBe(true)   // polled now, not on the next tick
  // another tool, or an errored start, changes nothing
  await hooks['tool.call']($, { tool: 'Bash' }, async () => ({ text: result.text }))
  await hooks['tool.call']($, { tool: 'mcp__cortados-route__start_task' }, async () => ({ isError: true, text: result.text }))
  expect(($.value('started') as any[]).length).toBe(1)
})

test('the alias tag\'s session view is read and held only for a tag this session\'s start_task named', async () => {
  const view = { ...(sessionFixture as any), session_filter: OTHER }
  const $ = dollar(url => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(view) }))
  await fetchAlias($ as any, { providerUrl: '', providerTokenFile: '~/.config/cortados/provider-token', providerToken: '' }, MINE)
  expect($.urls).toEqual([])                                          // nothing started under another tag: nothing read
  await $.state.set({ plugin: 'cortados-pulse', key: 'started' }, [{ tag: OTHER, task_id: 't20261116160946-4437347b', at: 1 }])
  await fetchAlias($ as any, { providerUrl: '', providerTokenFile: '~/.config/cortados/provider-token', providerToken: '' }, MINE)
  expect($.urls).toEqual(['http://air.example:8432/v1/cortados/live/session?session=' + OTHER])
  expect(($.value('alias') as any).tag).toBe(OTHER)
})

/** A host fetch that never answers on its own and honours init.signal: it rejects when the read is aborted. */
function hung(aborts: string[]) {
  return (url: string, init?: any) => new Promise((_, reject) => {
    init?.signal?.addEventListener('abort', () => { aborts.push(url); reject(new Error('aborted')) })
  })
}

test('a provider read that never answers is given up and aborted, so the one-at-a-time poll cannot stall', async () => {
  const aborts: string[] = []
  const $ = dollar(hung(aborts))
  const p = fetchBounded($ as any, 'http://air.example:8432/v1/cortados/live', {})
  expect($.timers.map(t => t.ms)).toEqual([FETCH_TIMEOUT_MS])
  expect(outstandingReads()).toBe(1)
  $.timers[0]!.fn()
  await expect(p).rejects.toThrow(/no answer within 15 s/)
  for (let i = 0; i < 5; i++) await Promise.resolve()
  expect(aborts).toEqual(['http://air.example:8432/v1/cortados/live'])
  expect(outstandingReads()).toBe(0)
  const ok = dollar(() => ({ status: 200 }))
  expect(await fetchBounded(ok as any, 'http://x/', {})).toEqual({ status: 200 })
  expect(ok.timers[0]!.cancelled).toBe(true)
  expect(outstandingReads()).toBe(0)
})

test('three reads that never answer leave none outstanding once their timeouts abort them', async () => {
  const aborts: string[] = []
  const $ = dollar(hung(aborts))
  const urls = ['/v1/cortados/live', '/v1/cortados/live/session?session=' + MINE, '/v1/cortados/local'].map(p => 'http://air.example:8432' + p)
  const reads = urls.map(u => fetchBounded($ as any, u, {}).then(() => 'answered', e => (e as Error).message))
  expect(outstandingReads()).toBe(3)
  for (const t of $.timers) t.fn()
  expect(await Promise.all(reads)).toEqual(urls.map(() => 'no answer within 15 s'))
  for (let i = 0; i < 5; i++) await Promise.resolve()
  expect([...aborts].sort()).toEqual([...urls].sort())
  expect(outstandingReads()).toBe(0)
  // the next poll reads them again: nothing holds them
  const again = dollar(() => ({ status: 200 }))
  for (const u of urls) expect(await fetchBounded(again as any, u, {})).toEqual({ status: 200 })
  expect(outstandingReads()).toBe(0)
})

test('a host that cannot cancel keeps the hung read outstanding, and no second read of that view piles on', async () => {
  let release: (v: any) => void = () => {}
  const deaf = dollar(() => new Promise(r => { release = r }))      // ignores init.signal
  const url = 'http://air.example:8432/v1/cortados/live?session=' + OTHER
  const p = fetchBounded(deaf as any, url, {})
  deaf.timers[0]!.fn()
  await expect(p).rejects.toThrow(/no answer within 15 s/)
  expect(outstandingReads()).toBe(1)
  await expect(fetchBounded(deaf as any, url, {})).rejects.toThrow(/previous read of this view has not ended/)
  expect(deaf.urls).toEqual([url])                                   // the second poll never reached the host
  release({ status: 200 })
  for (let i = 0; i < 5; i++) await Promise.resolve()
  expect(outstandingReads()).toBe(0)
  expect(await fetchBounded(dollar(() => ({ status: 204 })) as any, url, {})).toEqual({ status: 204 })
})

test('a host that refuses an init carrying a signal is asked again without one, and never with one after', async () => {
  const inits: any[] = []
  const picky = dollar((_u, init) => { inits.push(init); if (init?.signal) throw new Error('DataCloneError: AbortSignal could not be cloned'); return { status: 200 } })
  expect(await fetchBounded(picky as any, 'http://y/', { headers: { a: 'b' } })).toEqual({ status: 200 })
  expect(inits.map(i => 'signal' in i)).toEqual([true, false])
  expect(inits[1]).toEqual({ headers: { a: 'b' } })
  expect(await fetchBounded(picky as any, 'http://y/', {})).toEqual({ status: 200 })
  expect(inits.length).toBe(3)
  expect(outstandingReads()).toBe(0)
})

test('an alias read that never settles and ignores the abort goes stale: said, its tasks dropped, nothing ticking', async () => {
  const CFG = { providerUrl: '', providerTokenFile: '~/.config/cortados/provider-token', providerToken: '' }
  const T0 = 1_700_000_000_000
  const view = { ...(sessionFixture as any), session_filter: OTHER,
    orchestrators: { ...(sessionFixture as any).orchestrators, items: (sessionFixture as any).orchestrators.items.map((g: any) => ({ ...g, origin: OTHER })) } }
  const mineView = { ...(sessionFixture as any), session_filter: MINE }
  let deaf = false
  const releases: ((v: any) => void)[] = []
  const answer = (v: any) => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(v) })
  const $ = dollar(url => deaf ? new Promise(r => { releases.push(r) })      // never settles, ignores init.signal
    : answer(url.endsWith(OTHER) ? view : mineView))
  let t = T0
  $.clock.now = async () => t
  await $.state.set({ plugin: 'cortados-pulse', key: 'started' }, [{ tag: OTHER, task_id: 't20261116160946-4437347b', at: 1 }])
  await fetchAlias($ as any, CFG, MINE)
  expect(await fetchTree($ as any, CFG, MINE)).toBe(true)
  const al = $.value('alias') as any
  expect(al.tag).toBe(OTHER)
  const tasks = treeLines(view as any, OTHER, 0).map(l => l.text)
  expect(tasks.length).toBeGreaterThan(0)
  const texts = (now: number) => aliasLines($.value('alias') as any, ($.value('aliasErr') ?? null) as any, MINE, now).map(l => l.text)
  expect(texts(T0)).toEqual(aliasLines(al, null, MINE, T0).map(l => l.text))
  expect(texts(T0).slice(1, 1 + tasks.length)).toEqual(tasks)
  // the host stops answering and cannot cancel: the read times out, the next poll is refused while it is outstanding
  deaf = true
  t = T0 + 10_000
  const p = fetchAlias($ as any, CFG, MINE)
  for (let i = 0; i < 20 && !$.timers.some(x => !x.cancelled && x.ms === FETCH_TIMEOUT_MS && x.fn); i++) await Promise.resolve()
  t = T0 + 25_000
  $.timers[$.timers.length - 1]!.fn()
  await p
  expect(($.value('aliasErr') as any).problem).toMatch(/no answer within 15 s/)
  let shown = texts(t)
  expect(shown.slice(1, 1 + tasks.length).length).toBe(tasks.length)   // within the grace: tasks and the problem, aged
  expect(shown[shown.length - 1]).toMatch(/no answer within 15 s.* · showing 25 s old data$/)
  t = T0 + 35_000
  await fetchAlias($ as any, CFG, MINE)
  const e = $.value('aliasErr') as any
  expect([e.problem.includes('previous read of this view has not ended'), e.pendingSince]).toEqual([true, T0 + 10_000])
  expect(T0 + 35_000 - e.pendingSince).toBeLessThan(STALE_AFTER_MS + 1)
  // past 2x the timeout since the read began: stale, the task list dropped, so nothing keeps ticking
  for (const now of [T0 + 41_000, T0 + 600_000]) {
    shown = texts(now)
    expect(shown.length).toBe(2)
    expect(shown[0]).toBe(ALIAS_NOTE(OTHER))
    expect(shown[1]).toMatch(/^stale since .* ago: a read of this view has not ended for .* \(past its 15 s timeout\); its tasks are hidden until it does$/)
    for (const x of tasks) expect(shown.includes(x)).toBe(false)
  }
  expect(texts(T0 + 41_000)[1]).toMatch(/^stale since 41 s ago: .* not ended for 31 s/)
  expect(texts(T0 + 600_000)[1]).toMatch(/^stale since 10 min ago: .* not ended for 10 min/)
  // the main view is unchanged: its read, on its own URL, still answers and holds its tree
  deaf = false
  expect(await fetchTree($ as any, CFG, MINE)).toBe(true)
  expect([($.value('tree') as any).tag, $.value('treeErr')]).toEqual([MINE, null])
  // once the hung read finally settles, the next poll reads the alias again and the stale mark goes
  for (const r of releases) r(answer(view))
  for (let i = 0; i < 10; i++) await Promise.resolve()
  t = T0 + 700_000
  await fetchAlias($ as any, CFG, MINE)
  expect($.value('aliasErr')).toBe(null)
  expect(texts(t).slice(1, 1 + tasks.length).length).toBe(tasks.length)
  expect(outstandingReads()).toBe(0)
})

test('the mounted pane: this session\'s work split with its breakdown button, and the local nodes section', { options: { providerUrl: 'http://air.example:8432', providerToken: TOKEN } }, async ($, on) => {
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
  const view = { ...(sessionFixture as any), session_filter: tag,
    orchestrators: { ...(sessionFixture as any).orchestrators, items: (sessionFixture as any).orchestrators.items.map((g: any) => ({ ...g, origin: tag })) },
    work_split: { ...WS, session: tag, row: { ...WS.row, session: tag } } }
  const SNAP = { generated_at: 1_800_000_000, usage: { total_routes: 1 }, subscriptions: { local: { ok: false, why: 'no verified local deployment currently available' } },
    readings: {}, devices: { status: 'ok', nodes: [{ id: 'pro', status: 'unconfigured', fits: [] }], pool_headroom_estimate_bytes: 0, pool_fits: [] } }
  on('http.fetch', async (_$, e) => {
    const body = e.url.includes('/v1/cortados/live/session') ? view : e.url.endsWith(LOCAL_SWITCH_PATH) ? { nodes: [PRO] } : SNAP
    return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(body) } }
  })
  await $.command.run({ command: 'cortados-pulse', args: '' } as any)
  await clock.advance(1)
  const ui = await $.ui.mount({ plugin: 'cortados-pulse', surface: 'terminal', component: 'Pane', requestId: 'cortados-pulse',
    props: { title: 'CortadOS pulse', isFocused: false, bodyColumns: 120, placement: 'dock' } as any })
  let texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  let all = texts.join('\n')
  expect(texts[0]).toBe('This session')
  expect(texts[1]).toMatch(/^work split 24h: routed 66% fresh \(1\.4M tok: codex\/gpt-6\.1-sol 0\.8M/)
  expect(all.includes('routed by how')).toBe(false)                    // folded until asked
  expect(all).toMatch(/pro: switch on · allowed · AC/)
  expect(all).toMatch(/vllm-mlx up · loaded qwen3\.5-27b-mlx-4bit/)
  expect(all).toMatch(/0\/1 in flight · owner active · pressure warn · 25\.0 GiB free/)
  expect(all).toMatch(/pro: not probed \(no SSH target in the resource-pool config\)/)
  expect(all.includes('fits 0')).toBe(false)
  expect(all.includes('pool 0.0 GiB')).toBe(false)
  expect(all).toMatch(/local \(resource-pool deployments\): none verified · see Local nodes for the gate/)
  expect(texts.indexOf('Local nodes')).toBeLessThan(texts.indexOf('Devices'))
  expect(all.includes(TOKEN)).toBe(false)
  await ui.press({ key: 'cortados-pulse-ws' })
  texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  all = texts.join('\n')
  expect(all).toMatch(/routed by how: child tasks 1\.3M · route_task 25k · routed subagents 50k/)
  expect(all).toMatch(/native by model \(fresh\):/)
  await ui.unmount()
})
