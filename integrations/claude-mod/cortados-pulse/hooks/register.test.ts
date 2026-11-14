import { test, expect } from 'claude-code/testing'
import { FALLBACK, POLL_MS, LIVE_POLL_MS, STALE_AFTER_S, expandHome, resolveProviderUrl, resolveToken, scrub, readConfig, poll,
  pollLive, age, pct, gib, dur, leftPct, laneLabel, laneLines, usageLines, deviceLines, laneOrder, taskLine, conversationLine,
  runningLines, fleetLines, hwText, identify, sessionLines, sessionTag, OFFICE_SALT_FILE, MINE_RECENT_MAX, CLEAR_NOTE,
  SESSION_PATH, treeLines, tokShort, glyph, fetchTree, parseLocalTasks, mergeLocal, localLines, readLocal, LOCAL_POLL_MS,
  LOCAL_TAIL_BYTES, LOCAL_NOTE, STALE_HB_PHASE } from './register'
import liveFixture from './live.fixture'
import sessionFixture from './session.fixture'

const TOKEN = 'sekrit-token-123'

/** A `$` with env, fs, http, state and clock: the five nouns poll() touches. */
function fakeDollar(opts: { env?: Record<string, string>; files?: Record<string, string>; http?: (url: string, init: any) => any }) {
  const state = new Map<string, { value: unknown; version: number }>()
  const key = (ref: any) => ref.plugin + '/' + ref.key
  const calls: { urls: string[]; inits: any[] } = { urls: [], inits: [] }
  const $ = {
    env: { get: async (name: string) => opts.env?.[name] },
    fs: { read: async (path: string) => { if (opts.files && path in opts.files) return opts.files[path]; throw new Error('ENOENT ' + path) } },
    http: { fetch: async (url: string, init: any) => { calls.urls.push(url); calls.inits.push(init); return opts.http ? opts.http(url, init) : { status: 200, ok: true, headers: {}, text: '{}' } } },
    state: {
      get: async (ref: any) => state.get(key(ref)) ?? { value: undefined, version: 0 },
      set: async (ref: any, value: unknown, options?: { ifVersion?: number }) => {
        const cur = state.get(key(ref)) ?? { value: undefined, version: 0 }
        if (options?.ifVersion !== undefined && options.ifVersion !== cur.version) return { isSet: false, version: cur.version }
        state.set(key(ref), { value, version: cur.version + 1 })
        return { isSet: true, version: cur.version + 1 }
      },
    },
    clock: { now: async () => 1_700_000_000_000 },
    calls,
    held: () => state.get('cortados-pulse/held')?.value as any,
    err: () => state.get('cortados-pulse/err')?.value as string | null | undefined,
    live: () => state.get('cortados-pulse/live')?.value as any,
    liveErr: () => state.get('cortados-pulse/liveErr')?.value as string | null | undefined,
  }
  return $
}

test('defaults commit no address and point the token at the per-machine file', () => {
  expect(FALLBACK.providerUrl).toBe('')
  expect(FALLBACK.providerTokenFile).toBe('~/.config/cortados/provider-token')
  expect(FALLBACK.providerToken).toBe('')
  expect(POLL_MS).toBe(20_000)
  expect(LIVE_POLL_MS).toBe(10_000)
  expect(POLL_MS % LIVE_POLL_MS).toBe(0)                      // /resources rides every other live tick
  expect(STALE_AFTER_S).toBe(3600)
  expect(readConfig(undefined)).toEqual(FALLBACK)
  expect(readConfig({ providerUrl: 'https://x.example', providerTokenFile: '  ' }).providerTokenFile).toBe(FALLBACK.providerTokenFile)
  expect(readConfig({ providerUrl: 7 as any }).providerUrl).toBe('')
})

test('~ expands to HOME and only at the front', () => {
  expect(expandHome('~/.config/x', '/Users/me')).toBe('/Users/me/.config/x')
  expect(expandHome('~', '/Users/me')).toBe('/Users/me')
  expect(expandHome('/abs/~/x', '/Users/me')).toBe('/abs/~/x')
  expect(expandHome('~other/x', '/Users/me')).toBe('~other/x')
  let error = ''
  try { expandHome('~/x', undefined) } catch (e) { error = String(e) }
  expect(error).toMatch(/HOME is unset/)
})

test('provider URL: config, then env, then the selected or per-machine file; no silent fallback', async () => {
  const $ = fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/provider-url': ' https://file.example:8443/\n' } })
  expect(await resolveProviderUrl($ as any)).toBe('https://file.example:8443')
  expect(await resolveProviderUrl($ as any, 'https://config.example/')).toBe('https://config.example')
  const envd = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: ' http://env.example:8432/ ' } })
  expect(await resolveProviderUrl(envd as any)).toBe('http://env.example:8432')
  const selected = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL_FILE: '/etc/cortados-url' }, files: { '/etc/cortados-url': 'https://sel.example' } })
  expect(await resolveProviderUrl(selected as any)).toBe('https://sel.example')
  const none = fakeDollar({ env: { HOME: '/tmp/home' } })
  let error = ''
  try { await resolveProviderUrl(none as any) } catch (e) { error = String(e) }
  expect(error).toMatch(/provider URL file \/tmp\/home\/.config\/cortados\/provider-url: cannot read it/)
  for (const bad of ['', 'ftp://host', 'https://', 'https://host\nhttps://other', '127.0.0.1:8432']) {
    const b = fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/provider-url': bad } })
    error = ''
    try { await resolveProviderUrl(b as any) } catch (e) { error = String(e) }
    expect(error.includes('provider URL file')).toBe(true)
  }
})

test('token: inline override, else the file with ~ expanded; errors name the path and never the content', async () => {
  const $ = fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/provider-token': TOKEN + '\n' } })
  expect(await resolveToken($ as any, FALLBACK)).toBe(TOKEN)
  expect(await resolveToken($ as any, { ...FALLBACK, providerToken: ' inline ' })).toBe('inline')
  const missing = fakeDollar({ env: { HOME: '/tmp/home' } })
  let error = ''
  try { await resolveToken(missing as any, FALLBACK) } catch (e) { error = String(e) }
  expect(error).toMatch(/token file \/tmp\/home\/.config\/cortados\/provider-token: cannot read it/)
  const empty = fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/provider-token': '  \n' } })
  error = ''
  try { await resolveToken(empty as any, FALLBACK) } catch (e) { error = String(e) }
  expect(error).toMatch(/is empty/)
})

test('scrub removes every occurrence of the token', () => {
  expect(scrub('a ' + TOKEN + ' b ' + TOKEN, TOKEN)).toBe('a *** b ***')
  expect(scrub('plain', '')).toBe('plain')
})

test('poll sends the Bearer token to <url>/resources and keeps the snapshot with its local time', async () => {
  const snap = { generated_at: 1_700_000_000, usage: { total_routes: 3 }, subscriptions: {}, readings: {}, devices: { status: 'pending', nodes: [] } }
  const $ = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN }, http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(snap) }) })
  await poll($ as any, FALLBACK)
  expect($.calls.urls).toEqual(['http://air.example:8432/resources'])
  expect($.calls.inits[0].headers.authorization).toBe('Bearer ' + TOKEN)
  expect($.held().data.usage.total_routes).toBe(3)
  expect($.held().at).toBe(1_700_000_000_000)
  expect($.err()).toBe(null)
})

test('poll: 401 names the token source, other statuses the code, failures are scrubbed of the token', async () => {
  const base = { env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' }, files: { '/tmp/home/.config/cortados/provider-token': TOKEN } }
  const unauth = fakeDollar({ ...base, http: () => ({ status: 401, ok: false, headers: {}, text: '' }) })
  await poll(unauth as any, FALLBACK)
  expect(unauth.err()).toBe('provider rejected the token (HTTP 401): check ~/.config/cortados/provider-token')
  expect(unauth.held()).toBe(undefined)
  const inline = fakeDollar({ ...base, http: () => ({ status: 403, ok: false, headers: {}, text: '' }) })
  await poll(inline as any, { ...FALLBACK, providerToken: TOKEN })
  expect(inline.err()).toBe('provider rejected the token (HTTP 403): check providerToken')
  const five = fakeDollar({ ...base, http: () => ({ status: 503, ok: false, headers: {}, text: '' }) })
  await poll(five as any, FALLBACK)
  expect(five.err()).toBe('provider returned HTTP 503')
  const thrown = fakeDollar({ ...base, http: () => { throw new Error('refused Bearer ' + TOKEN) } })
  await poll(thrown as any, FALLBACK)
  expect(thrown.err()).toBe('could not read the provider (refused Bearer ***)')
  expect(String(thrown.err()).includes(TOKEN)).toBe(false)
  const notJson = fakeDollar({ ...base, http: () => ({ status: 200, ok: true, headers: {}, text: '[1]' }) })
  await poll(notJson as any, FALLBACK)
  expect(notJson.err()).toMatch(/did not return a JSON object/)
  const noUrl = fakeDollar({ env: { HOME: '/tmp/home' } })
  await poll(noUrl as any, FALLBACK)
  expect(noUrl.err()).toMatch(/provider URL file .*cannot read it; set providerUrl/)
  expect(noUrl.calls.urls).toEqual([])
})

test('formatting helpers', () => {
  expect(age(12)).toBe('12 s')
  expect(age(600)).toBe('10 min')
  expect(age(7200)).toBe('2.0 h')
  expect(age(-1)).toBe('?')
  expect(pct(62.4)).toBe('62%')
  expect(pct(null)).toBe('—')
  expect(pct(NaN)).toBe('—')
  expect(pct(Infinity)).toBe('—')
  expect(pct(-Infinity)).toBe('—')
  expect(gib(2 ** 30 * 1.5)).toBe('1.5 GiB')
  expect(gib(undefined)).toBe('—')
  expect(leftPct({ headroom_pct: 40 })).toBe(40)
  expect(leftPct({ used_pct: 70 })).toBe(30)
  expect(leftPct({ headroom_pct: NaN })).toBe(null)
  expect(leftPct({ used_pct: Infinity })).toBe(null)
  expect(leftPct({})).toBe(null)
  expect(laneOrder(['local', 'codex', 'claude#b', 'cursor', 'claude'])).toEqual(['claude', 'claude#b', 'codex', 'cursor', 'local'])
})

test('unknown or missing quota never renders as healthy', () => {
  for (const health of [{ ok: true, scarcity: null }, { ok: true }, {}]) {
    expect(laneLines('codex', health, undefined, 1700000000)).toEqual(['codex: quota unknown'])
  }
})

import staleFixture from './stale-resources.fixture'

test('real failed seven-hour-old projection cannot restore healthy quota from historical windows', () => {
  const s = staleFixture
  expect(s.subscriptions.codex.ok).toBe(true)
  expect(s.subscriptions.codex.scarcity).toBe(null)
  expect(laneLines('codex', s.subscriptions.codex, s.readings.codex, s.generated_at)).toEqual([
    'codex: quota unknown · last sample FAILED · sample 7.0 h old (stale)',
    '  week: 0% left',
  ])
})

test('missing and non-finite scarcity stays unknown even with historical window headroom', () => {
  for (const scarcity of [undefined, null, NaN, Infinity, -Infinity]) {
    expect(laneLines('codex', { ok: true, scarcity }, { windows: [{ headroom_pct: 50 }] }, 1700000000))
      .toEqual(['codex: quota unknown', '  window: 50% left'])
  }
})

import fixture from './resources.fixture'

test('provider-generated snapshot preserves account and pool readings and local admission reason', () => {
  const s = fixture
  expect(laneLines('claude#b', s.subscriptions['claude#b'], s.readings['claude#b'], s.generated_at)).toEqual([
    'claude#b @.claude-acctB: ok · scarcity -0.20 · sample 60 s old', '  week [fable]: 55% left'])
  expect(laneLines('local', s.subscriptions.local, undefined, s.generated_at)).toEqual([
    'local: eligible · available: measured local resources'])
  expect(laneLines('codex', s.subscriptions.codex, undefined, s.generated_at)).toEqual(['codex: quota unknown'])
})

test('poll redacts all response strings and keys before storage and rendering', async () => {
  const snap = { usage: { error: TOKEN, by_lane: { [TOKEN]: 2 } }, subscriptions: { error: TOKEN,
    codex: { ok: false, why: TOKEN } }, readings: { error: TOKEN }, devices: { status: 'unavailable', error: TOKEN } }
  const $ = fakeDollar({ http: () => ({ status: 200, ok: true, text: JSON.stringify(snap) }) })
  await poll($ as any, { ...FALLBACK, providerUrl: 'http://example.test', providerToken: TOKEN })
  expect(JSON.stringify($.held()).includes(TOKEN)).toBe(false)
  expect($.held().data).toEqual({ usage: { error: '***', by_lane: { '***': 2 } }, subscriptions: { error: '***',
    codex: { ok: false, why: '***' } }, readings: { error: '***' }, devices: { status: 'unavailable', error: '***' } })
})

test('lane samples retain age, failure, stale windows and spent reasons', () => {
  const now = 1700000000
  expect(laneLines('codex', { ok: true, scarcity: 0.05 }, { fetched_at: now - 4000, failed: true, windows: [] }, now))
    .toEqual(['codex: ok · scarcity 0.05 · last sample FAILED · sample 67 min old (stale)'])
  expect(laneLines('cursor', { ok: false, scarcity: 1.2, why: 'month spent' }, { windows: [
    { window: 'month', pool: 'other', used_pct: 70, stale: true }, { window: '5h', pool: 'all', headroom_pct: 91 }] }, now))
    .toEqual(['cursor: SPENT · scarcity 1.20', '  month spent', '  month [other]: 30% left (stale)', '  5h: 91% left'])
  expect(laneLines('local', { ok: false, why: 'no verified deployment' }, undefined, now))
    .toEqual(['local: unavailable · no verified deployment'])
  expect(laneLabel('claude', {})).toBe('claude')
})

test('usage and device lines', () => {
  expect(usageLines({ total_routes: 12, degraded: { count: 1, of: 10 }, s1_latency_ms: { median: 410, p90: 900, window: 10 },
    by_lane: { 'claude/claude-sonnet-5': 7, 'codex/gpt-5.4': 3, 'local/qwen3-4b': 1, 'cursor/grok': 1 } })).toEqual([
    'routes 12 · degraded 1/10',
    'S1 410 ms median · p90 900 ms (last 10)',
    'top claude/claude-sonnet-5 7 · codex/gpt-5.4 3 · cursor/grok 1',
  ])
  expect(usageLines({ error: 'OperationalError: locked' })).toEqual(['usage: OperationalError: locked', 'routes — · degraded 0/0', 'S1 — ms median · p90 — ms'])
  expect(deviceLines({ status: 'pending', nodes: [] })).toEqual(['sample pending'])
  expect(deviceLines({ status: 'unavailable', error: 'OSError: ssh down', nodes: [] })).toEqual(['unavailable: OSError: ssh down'])
  expect(deviceLines({ status: 'ok', nodes: [{ id: 'studio', status: 'online', gpu_headroom_estimate_bytes: 2 ** 30 * 40, gpu_utilization_pct: 12.6, fits: ['a', 'b'] }],
    pool_headroom_estimate_bytes: 2 ** 30 * 48, pool_fits: ['a', 'b', 'c'] })).toEqual([
    'studio online · 40.0 GiB free · gpu 13% · fits 2',
    'pool 48.0 GiB · fits 3',
  ])
  expect(deviceLines({ status: 'ok', nodes: [] })).toEqual(['no nodes online'])
})

import { register } from './register'

/** Wire register() and return its hooks by event name, like the host would. */
function wired(options: any) {
  const hooks: Record<string, any> = {}
  const on = (event: string, a: any, b?: any) => { hooks[event] = b ?? a; return { catch() {} } }
  register(on as any, options)
  return hooks
}

test('session.start registers the command, opens the pane, polls once and keeps one interval; command.run reopens', async () => {
  const hooks = wired({})
  const snap = { generated_at: 1_700_000_000, usage: { total_routes: 1 }, subscriptions: { codex: { ok: true, scarcity: 0.1 } }, readings: {}, devices: { status: 'pending', nodes: [] } }
  const base = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN }, http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(snap) }) })
  const registered: any[] = []
  const opened: any[] = []
  const every: { ms: number; cancelled: boolean }[] = []
  const $ = { ...base,
    command: { register: async (spec: any) => { registered.push(spec) } },
    ui: { open: async (p: any) => { opened.push(p); return { isPlaced: true } } },
    clock: { ...base.clock, every: (ms: number, _fn: () => void) => { const t = { ms, cancelled: false }; every.push(t); return { cancel: () => { t.cancelled = true } } } },
  }
  expect(await hooks['session.start']($, { kind: 'start' }, async (e: any) => e)).toEqual({ kind: 'start' })
  for (let i = 0; i < 30; i++) await Promise.resolve()       // the live poll identifies the session before it fetches
  expect(registered).toEqual([{ name: 'cortados-pulse', description: 'Open the CortadOS router pulse pane' }])
  expect(opened).toEqual([{ id: 'cortados-pulse', title: 'CortadOS pulse' }])
  expect(every.map(t => t.ms)).toEqual([LIVE_POLL_MS])
  // no salt file here: the session has no tag, so the fleet view is read (the fallback), never the per-session one
  expect([...$.calls.urls].sort()).toEqual(['http://air.example:8432/resources', 'http://air.example:8432/v1/cortados/live'])
  expect($.held().data.usage.total_routes).toBe(1)
  await hooks['session.start']($, {}, async (e: any) => e)
  expect(every.length).toBe(2)
  expect(every[0]!.cancelled).toBe(true)
  expect(every[1]!.cancelled).toBe(false)
  expect(await hooks['command.run']($, {})).toEqual({ text: 'CortadOS pulse pane opened.' })
  expect(opened.length).toBe(3)
  for (let i = 0; i < 60; i++) await Promise.resolve()       // let the last poll settle: one live poll runs at a time
})

test('pollLive reads /v1/cortados/live with the Bearer token and keeps the provider base for the dashboard link', async () => {
  const $ = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432/' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN }, http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(liveFixture) }) })
  await pollLive($ as any, FALLBACK)
  expect($.calls.urls).toEqual(['http://air.example:8432/v1/cortados/live'])
  expect($.calls.inits[0].headers.authorization).toBe('Bearer ' + TOKEN)
  expect($.live().base).toBe('http://air.example:8432')
  expect($.live().data.overview.running_tasks).toBe(1)
  expect($.liveErr()).toBe(null)
  expect($.held()).toBe(undefined)                            // the /resources snapshot is a separate atom
})

test('pollLive: an older provider (404) is said as such; 401 and failures are scrubbed like /resources', async () => {
  const base = { env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' }, files: { '/tmp/home/.config/cortados/provider-token': TOKEN } }
  const old = fakeDollar({ ...base, http: () => ({ status: 404, ok: false, headers: {}, text: '' }) })
  await pollLive(old as any, FALLBACK)
  expect(old.liveErr()).toBe('this provider has no live view yet (update it to show running work)')
  const unauth = fakeDollar({ ...base, http: () => ({ status: 401, ok: false, headers: {}, text: '' }) })
  await pollLive(unauth as any, FALLBACK)
  expect(unauth.liveErr()).toBe('live: provider rejected the token (HTTP 401): check ~/.config/cortados/provider-token')
  const thrown = fakeDollar({ ...base, http: () => { throw new Error('refused Bearer ' + TOKEN) } })
  await pollLive(thrown as any, FALLBACK)
  expect(thrown.liveErr()).toBe('live: could not read the provider (refused Bearer ***)')
  const leaky = fakeDollar({ ...base, http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify({ tasks: { running: [{ title: TOKEN }] } }) }) })
  await pollLive(leaky as any, FALLBACK)
  expect(JSON.stringify(leaky.live()).includes(TOKEN)).toBe(false)
})

test('pollLive: a tick while the previous answer is still out is skipped, not stacked', async () => {
  let release: () => void = () => {}
  const gate = new Promise<void>(r => { release = r })
  const $ = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN },
    http: async () => { await gate; return { status: 200, ok: true, headers: {}, text: JSON.stringify(liveFixture) } } })
  const first = pollLive($ as any, FALLBACK)
  await pollLive($ as any, FALLBACK)                          // returns at once: the first is in flight
  release()
  await first
  expect($.calls.urls.length).toBe(1)
  await pollLive($ as any, FALLBACK)                          // the guard is released after the answer
  expect($.calls.urls.length).toBe(2)
  const boom = fakeDollar({ env: {}, files: {} })               // a failing poll releases it too
  await pollLive(boom as any, FALLBACK)
  await pollLive($ as any, FALLBACK)
  expect($.calls.urls.length).toBe(3)
})

test('the tasks scope, tasks started elsewhere and a partial tasks section are said', () => {
  const lines = runningLines({ overview: { running_tasks: 0, queued_tasks: 0, active_conversations: 0 },
    tasks: { status: 'partial', scope: 'provider host', errors: [{ source: 'task_dir', error: 'TaskError' }],
      elsewhere: [{ task_id: 't1', host: 'air' }, { task_id: 't2', host: 'studio' }, { task_id: 't3', host: 'air' }] },
    conversations: { status: 'ok', items: [] }, hosts: { provider_host: null } } as any, 0)
  expect(lines).toEqual(['0 running · 0 queued · 0 conversations', 'tasks partial: task_dir: TaskError',
    'tasks on the provider host only · 3 started elsewhere (air, studio)', 'nothing running'])
})

test('elapsed times read at a glance', () => {
  expect(dur(42)).toBe('42s')
  expect(dur(125)).toBe('2m 05s')
  expect(dur(4320)).toBe('1h 12m')
  expect(dur(2 * 86400 + 3 * 3600)).toBe('2d 3h')
  expect(dur(null)).toBe('—')
  expect(dur(-1)).toBe('—')
  expect(dur(NaN)).toBe('—')
})

test('the Running section from the provider-generated live snapshot', () => {
  expect(runningLines(liveFixture as any, 0)).toEqual([
    '1 running · 1 queued · 5 conversations (3 working)',
    '● Refactor the scarcity reader · on air · claude-sonnet-5 (claude#b) · tool Bash · 2m 05s',
    '◷ Check the release page · on air · queued 10m 00s',
    'tasks on air only',
    // the running child's own conversation is its task line above, listed once
    '▸ Review the diff · gpt-6.1 (codex) · tool Read · 1m 30s',
    '▸ CortadOS-Router · main session · claude-opus-5 (claude#a) · tool Edit · 15m 00s',
    '· conversation · qwen3-30b (local-studio) · idle · 30s',
    '· conversation · gpt-6.1-mini (codex) · idle · 6m 40s',
  ])
  // elapsed keeps counting between polls
  expect(runningLines(liveFixture as any, 60)[1]).toMatch(/· 3m 05s$/)
  expect(fleetLines(liveFixture as any)).toEqual([
    'S1 up · 23 ms',
    'lowest left: cursor (shared with Grok Bot) 70% (month (Cursor models: Grok, Composer, Auto))',
    'air: com.cortados.provider running · cpu 13% · gpu 3% · disk 40% free',
    'studio: s1-scorer running, s1-continuous running 38% · cpu 30% · gpu 37% · disk 62% free',
    '  S1 up: CortadOS Router S1 2026-11-09 (run-20261109) · promotion held · candidate promotion held',
    'pro: no heartbeat yet',
  ])
})

test('an unreadable section is said, never drawn as nothing running', () => {
  const lines = runningLines({ overview: { running_tasks: null, queued_tasks: null, active_conversations: 0 },
    tasks: { status: 'error', errors: [{ source: 'child-tasks.jsonl', error: 'PermissionError' }] },
    conversations: { status: 'ok', items: [] } } as any, 0)
  expect(lines).toEqual(['? running · ? queued · 0 conversations', 'tasks unavailable: child-tasks.jsonl: PermissionError'])
  expect(runningLines({ overview: { running_tasks: 0, queued_tasks: 0, active_conversations: 0, working_conversations: 0 },
    tasks: { status: 'ok' }, conversations: { status: 'ok', items: [] } } as any, 0))
    .toEqual(['0 running · 0 queued · 0 conversations (0 working)', 'nothing running'])
  const many = { overview: {}, tasks: { running: Array.from({ length: 9 }, (_, i) => ({ task_id: 't' + i, state: 'running', elapsed_s: i })) } }
  expect(runningLines(many as any, 0).slice(-1)).toEqual(['  +3 more tasks'])
  expect(taskLine({ task_id: 't1', state: 'running' }, 0)).toBe('● task t1 · —')
  expect(conversationLine({ kind: 'orchestrator', status: 'idle' }, 0)).toBe('· main session · idle')
  expect(conversationLine({ kind: 'child_task', status: 'working', model: 'm' }, 0)).toBe('▸ child task · m')
  // a child task with no task line (finished in the log, or not on this host) keeps its conversation line
  expect(runningLines({ overview: {}, tasks: { status: 'ok', running: [] }, conversations: { status: 'ok', items: [
    { kind: 'child_task', run_id: 't1', status: 'working', model: 'm' }] } } as any, 0)).toContain('▸ child task · m')
  expect(hwText(null)).toBe(null)
  expect(hwText({ disk_free_pct: 9.6 })).toBe('disk 10% free')
  expect(fleetLines({ overview: {}, hosts: { items: [{ name: 'studio', heartbeat_state: 'fresh',
    heartbeat: { age_s: 5, jobs: [], s1: { health: 'down', flags: ['nightly-stop', 'other'], pending: null } } }] } } as any))
    .toEqual(['S1 unknown', 'studio: no jobs', '  S1 DOWN: no model loaded · nightly loop stopping · other'])
  expect(fleetLines({ overview: { s1: 'down' }, s1: { reason: 'URLError' }, hosts: { items: [{ name: 'studio',
    heartbeat_state: 'stale', heartbeat: { age_s: 1800, jobs: [] } }] } } as any))
    .toEqual(['S1 DOWN · URLError', 'studio: STALE heartbeat 30 min old · no jobs'])
})

const SALT = '0123456789abcdef0123456789abcdef'
const SID = '792b087a-1983-4baf-b701-77792e4b77e4'

test('this session is named by the shared salt and its id, the way the mod and the MCP server name it', async () => {
  expect(OFFICE_SALT_FILE).toBe('~/.config/cortados/office-salt')
  const $ = { ...fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/office-salt': SALT + '\n' } }),
    session: { id: async () => SID } }
  const me = await identify($ as any)
  // the value tests/test_child_tasks.py pins for subagent_mcp.origin_tag(SALT, SID)
  expect(me).toEqual({ tag: await sessionTag(SALT, SID), why: null })
  expect(me.tag).toBe('ea4383005613c21c')        // the cross-language vector (tests/test_child_tasks.py HASH_VECTOR)
  expect(/^[0-9a-f]{16}$/.test(me.tag!)).toBe(true)
  expect(JSON.stringify(me).includes(SID)).toBe(false)
  const none = { ...fakeDollar({ env: { HOME: '/tmp/home' } }), session: { id: async () => SID } }
  expect((await identify(none as any)).tag).toBe(null)
  expect((await identify(none as any)).why).toMatch(/no ~\/.config\/cortados\/office-salt yet/)
  const bad = { ...fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/office-salt': 'nope' } }), session: { id: async () => SID } }
  expect((await identify(bad as any)).tag).toBe(null)
  const noId = { ...fakeDollar({ env: { HOME: '/tmp/home' }, files: { '/tmp/home/.config/cortados/office-salt': SALT } }),
    session: { id: async () => { throw new Error('x') } } }
  expect((await identify(noId as any)).why).toMatch(/its id could not be read/)
  const over = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_OFFICE_SALT_FILE: '/etc/salt' }, files: { '/etc/salt': SALT } }),
    session: { id: async () => SID } }
  expect((await identify(over as any)).tag).toBe(me.tag)
})

test('pollLive identifies the session every poll, and only with a session to ask', async () => {
  const $ = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN, '/tmp/home/.config/cortados/office-salt': SALT },
    http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(liveFixture) }) }), session: { id: async () => SID } }
  await pollLive($ as any, FALLBACK, true)
  const mine = await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'mine' })
  expect(mine.value.tag).toBe(await sessionTag(SALT, SID))
})

test('this session\'s own tasks: running and queued, then the last finished; others are not shown', () => {
  const tag = 'aaaaaaaaaaaaaaaa'
  const live = { tasks: { status: 'ok', origins: [],
    running: [{ task_id: 'r1', state: 'running', title: 'Mine running', host: 'air', model: 'm1', elapsed_s: 65, origin_session: tag },
      { task_id: 'r2', state: 'running', title: 'Someone else', origin_session: 'bbbbbbbbbbbbbbbb' }],
    queued: [{ task_id: 'q1', state: 'queued', title: 'Mine queued', host: 'air', elapsed_s: 5, origin_session: tag }],
    recent: [{ task_id: 'd1', state: 'succeeded', title: 'Mine done', host: 'studio', elapsed_s: 600, origin_session: tag },
      { task_id: 'd2', state: 'failed', title: 'Mine failed', elapsed_s: 30, origin_session: tag },
      { task_id: 'd3', state: 'succeeded', title: 'Unattributed', origin_session: null }] } }
  expect(sessionLines(live as any, { tag, why: null }, 10)).toEqual([
    '● Mine running · on air · m1 · 1m 15s',
    '◷ Mine queued · on air · queued 15s',
    '✓ Mine done · on studio · succeeded · took 10m 00s',
    '✕ Mine failed · failed · took 30s',
  ])
  expect(sessionLines(live as any, { tag: 'cccccccccccccccc', why: null }, 0)).toEqual(['no task started from this session in the last 24 h'])
  expect(sessionLines(live as any, { tag: null, why: 'session not identified: x' }, 0)).toEqual(['session not identified: x'])
  expect(sessionLines(live as any, null, 0)).toEqual(['identifying this session…'])
  expect(sessionLines({ tasks: { status: 'error', errors: [{ source: 'child-tasks.jsonl', error: 'OSError' }] } } as any, { tag, why: null }, 0))
    .toEqual(['tasks unavailable: child-tasks.jsonl: OSError'])
  const many = { tasks: { status: 'ok', origins: [], recent: Array.from({ length: MINE_RECENT_MAX + 2 }, (_, i) => ({ task_id: 'd' + i, state: 'cancelled', origin_session: tag })) } }
  expect(sessionLines(many as any, { tag, why: null }, 0).slice(-1)).toEqual(['  +2 more finished (dashboard)'])
})

test('an older provider without task origins is said as such, never as "no task started"', () => {
  const tag = 'aaaaaaaaaaaaaaaa'
  expect(sessionLines({ tasks: { status: 'ok', running: [], recent: [] } } as any, { tag, why: null }, 0))
    .toEqual(['this provider records no task origins yet (update it to see this session\'s tasks)'])
  expect(sessionLines({ tasks: { status: 'ok', origins: [], running: [], recent: [] } } as any, { tag, why: null }, 0))
    .toEqual(['no task started from this session in the last 24 h'])
  expect(CLEAR_NOTE).toMatch(/after a \/clear, earlier tasks stay under the previous session/)
})

test('the fleet view filtered in the pane says when its capped lists may hide this session\'s tasks', () => {
  const tag = 'aaaaaaaaaaaaaaaa'
  const fleet = { tasks: { status: 'ok', origins: [], truncated: true, running: [], recent: [] } }
  expect(sessionLines(fleet as any, { tag, why: null }, 0).slice(-1)[0]).toMatch(/fleet lists are capped/)
  // the provider's ?session= view was filtered before its caps: no such warning
  const own = { session_filter: tag, tasks: { status: 'ok', origins: [], truncated: true, running: [], recent: [] } }
  expect(sessionLines(own as any, { tag, why: null }, 0)).toEqual(['no task started from this session in the last 24 h'])
})

test('pollLive asks the provider for this session\'s own view, and keeps it only when the provider filtered it', async () => {
  // Review 2026-11-13: on a busy fleet the fleet view's lists are capped (50 running, 40 recent) before any filter, so
  // the pane must read the session's tasks from ?session=, which the provider filters before its caps.
  const tag = await sessionTag(SALT, SID)
  const mineTask = { task_id: 't20261111000900-aaaaaaaa', state: 'running', title: 'Mine, pushed out of the fleet list',
    host: 'air', elapsed_s: 3000, origin_session: tag }
  const fleet = { tasks: { status: 'ok', origins: [{ session: tag, running: 1, task_ids: [mineTask.task_id] }], truncated: true,
    running: Array.from({ length: 50 }, (_, i) => ({ task_id: 'o' + i, state: 'running', origin_session: 'b'.repeat(16) })), recent: [] } }
  const own = { session_filter: tag, tasks: { status: 'ok', origins: fleet.tasks.origins, truncated: false, running: [mineTask], recent: [] } }
  for (const filtered of [true, false]) {
    const $ = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
      files: { '/tmp/home/.config/cortados/provider-token': TOKEN, '/tmp/home/.config/cortados/office-salt': SALT },
      http: (url: string) => ({ status: 200, ok: true, headers: {},
        // an older provider ignores ?session= and answers the fleet view
        text: JSON.stringify(url.includes('?session=') && filtered ? own : fleet) }) }), session: { id: async () => SID } }
    await pollLive($ as any, FALLBACK, true)
    // the per-session view is asked first; these answers are not a subtree (an older provider): the fleet fallback
    expect($.calls.urls).toEqual(['http://air.example:8432/v1/cortados/live/session?session=' + tag,
      'http://air.example:8432/v1/cortados/live', 'http://air.example:8432/v1/cortados/live?session=' + tag])
    const held = (await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'mineLive' })).value
    if (filtered) {
      expect(held.tag).toBe(tag)
      expect(sessionLines(held.data, { tag, why: null }, 0)).toEqual(['● Mine, pushed out of the fleet list · on air · 50m 00s'])
    } else {
      expect(held).toBe(null)                              // the pane falls back to the fleet view and says it is capped
      expect(sessionLines($.live().data, { tag, why: null }, 0)).toEqual(['no task started from this session in the last 24 h',
        '  (fleet lists are capped: some of this session\'s tasks may be missing; update the provider)'])
    }
  }
  // no tag: no session request at all
  const anon = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN }, http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(fleet) }) }),
    session: { id: async () => SID } }
  await pollLive(anon as any, FALLBACK, true)
  expect(anon.calls.urls).toEqual(['http://air.example:8432/v1/cortados/live'])
})

test('the provider-generated fixture carries origins: its main session sees its own tasks', async () => {
  const tag = (liveFixture as any).tasks.running[0].origin_session
  expect(/^[0-9a-f]{16}$/.test(tag)).toBe(true)
  expect(sessionLines(liveFixture as any, { tag, why: null }, 0)).toEqual([
    '● Refactor the scarcity reader · on air · claude-sonnet-5 (claude#b) · tool Bash · 2m 05s',
    '✓ Write the changelog · on air · succeeded · took 16m 40s',
  ])
})

// ---- this session's tree (GET /v1/cortados/live/session; tests/test_orchestrator_tree.py generates the fixture) ----

const MINE_TAG = 'aaaaaaaaaaaaaaa1'                  // the fixture's session (tests/test_orchestrator_tree.MINE)

test('the session view holds this session only, and the pane draws it as a tree', () => {
  const raw = JSON.stringify(sessionFixture)
  for (const other of ['bbbbbbbbbbbbbbb2', 'Grounds', 'Their secret plan', 'grokbot', 'Legacy task', 'SECRET']) {
    expect(raw.includes(other)).toBe(false)
  }
  expect(SESSION_PATH).toBe('/v1/cortados/live/session')
  const lines = treeLines(sessionFixture as any, MINE_TAG, 0)
  expect(lines.map(l => l.text)).toEqual([
    '2 running · 1 done · 1 unknown',
    '! 1 needs attention',
    '● Build the tree · 5m 00s',
    '  claude-sonnet-5 · on air · tool Edit · 14k tok',
    '  ├ ● main loop · claude-sonnet-5 · model turn · 4.9k tok · 4m 50s',
    '  ├ ● subagent · gpt-6.1 · running a tool · 1m 00s',
    '  └ ✓ subagent · qwen3-30b · closed idle · 1.0k tok · took 1m 30s',
    '  ↳ ● Nested step · 3m 20s',
    '      claude-haiku-5 · on air',
    '      └ ◷ main loop · claude-haiku-5 · idle between turns · 3m 10s',
    '    ↳ ✓ Deeper step · succeeded · took 50s',
    '? task t20261111093000-a0000009 · on studio, state there',
    'routed directly',
    '└ ● Check the docs · gpt-6.1-mini · model turn · 40s',
  ])
  expect(lines.filter(l => l.attn).map(l => l.text)).toEqual(['! 1 needs attention',
    '? task t20261111093000-a0000009 · on studio, state there'])
  // clocks of live nodes keep counting between polls; finished ones do not
  const later = treeLines(sessionFixture as any, MINE_TAG, 10).map(l => l.text)
  expect(later[2]).toBe('● Build the tree · 5m 10s')
  expect(later[10]).toBe('    ↳ ✓ Deeper step · succeeded · took 50s')
  // another tag's pane draws nothing from this view (the provider would not have sent it, and the pane checks too)
  expect(treeLines(sessionFixture as any, 'cccccccccccccccc', 0).map(l => l.text))
    .toEqual(['no task started from this session in the last 24 h'])
})

test('tree glyphs, tokens and failures', () => {
  expect([glyph('running'), glyph('waiting'), glyph('done'), glyph('failed'), glyph('unknown'), glyph('done', 'cancelled')])
    .toEqual(['●', '◷', '✓', '✕', '?', '⊘'])
  expect([tokShort({ fresh: 950 }), tokShort({ fresh: 4900 }), tokShort({ fresh: 14000 }), tokShort({ fresh: 2_500_000 }),
    tokShort(null), tokShort({ fresh: 0 })]).toEqual(['950 tok', '4.9k tok', '14k tok', '2.5M tok', null, null])
  const v = { session_filter: MINE_TAG, orchestrators: { status: 'ok', items: [{ origin: MINE_TAG, counts: { failed: 1 }, attention: 1,
    tasks: [{ task_id: 't1', title: 'Broke', state: 'failed', status: 'failed', elapsed_s: 30, ended_at: 5 }] }] } }
  expect(treeLines(v as any, MINE_TAG, 0)).toEqual([{ text: '1 failed' }, { text: '! 1 needs attention', attn: true },
    { text: '✕ Broke · failed · took 30s', attn: true }])
  expect(treeLines({ orchestrators: { status: 'error', errors: [{ source: 'child-tasks.jsonl', error: 'OSError' }] } } as any, MINE_TAG, 0))
    .toEqual([{ text: 'tasks unavailable: child-tasks.jsonl: OSError', attn: true }])
  const many = { orchestrators: { status: 'ok', items: [{ origin: MINE_TAG, counts: { running: 1 }, tasks: [{ task_id: 't', title: 'Fan',
    status: 'running', state: 'running', elapsed_s: 1, sub_sessions: Array.from({ length: 6 }, () => ({ status: 'running', role: 'subagent' })),
    sub_sessions_hidden: 2 }] }] } }
  expect(treeLines(many as any, MINE_TAG, 0).slice(-1)).toEqual([{ text: '  └ +4 more sub-sessions' }])
})

test('a failed sub-session needs attention: on a running task, a folded finished task, and past the cap', () => {
  const sub = (status: string) => ({ status, role: 'subagent' })
  const v = { session_filter: MINE_TAG, orchestrators: { status: 'ok', inputs_truncated: ['conversations'], items: [{ origin: MINE_TAG,
    counts: { running: 1, done: 1 }, attention: 3, tasks: [
      { task_id: 't1', title: 'Runs on', state: 'running', status: 'running', elapsed_s: 10, sub_sessions_failed: 1,
        sub_sessions: [sub('running'), sub('running'), sub('running'), sub('running'), sub('failed')] },
      { task_id: 't2', title: 'Finished', state: 'succeeded', status: 'done', elapsed_s: 30, ended_at: 5, sub_sessions_failed: 2,
        sub_sessions: [sub('failed'), sub('failed')] },
      { task_id: 't3', title: 'Broke', state: 'failed', status: 'failed', elapsed_s: 30, ended_at: 5, sub_sessions_failed: 1 }] }] } }
  const lines = treeLines(v as any, MINE_TAG, 0)
  expect(lines.slice(0, 3)).toEqual([{ text: 'inputs capped (conversations): some items may be missing', attn: true },
    { text: '1 running · 1 done' }, { text: '! 3 need attention', attn: true }])
  expect(lines.filter(l => l.attn).map(l => l.text).slice(2)).toEqual([
    '● Runs on · 1 sub-session failed · 10s',                // its failed subagent is past the pane's 4: said on the task
    '✓ Finished · succeeded · 2 sub-sessions failed · took 30s',  // folded to one line, still said
    '✕ Broke · failed · took 30s'])                           // a failed task once: no sub-session count on it
})

test('pollLive with a tag reads only the session view: nothing fleet-wide', async () => {
  const salt = '0123456789abcdef0123456789abcdef', sid = '792b087a-1983-4baf-b701-77792e4b77e4'
  const tag = await sessionTag(salt, sid)
  const view = { ...sessionFixture, session_filter: tag, orchestrators: { ...(sessionFixture as any).orchestrators, session_filter: tag } }
  const $ = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN, '/tmp/home/.config/cortados/office-salt': salt },
    http: () => ({ status: 200, ok: true, headers: {}, text: JSON.stringify(view) }) }), session: { id: async () => sid } }
  await pollLive($ as any, FALLBACK, true)
  expect($.calls.urls).toEqual(['http://air.example:8432/v1/cortados/live/session?session=' + tag])
  expect($.calls.inits[0].headers.authorization).toBe('Bearer ' + TOKEN)
  const held = (await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'tree' })).value
  expect([held.tag, held.base]).toEqual([tag, 'http://air.example:8432'])
  // a provider failure settles the poll (said, the held tree kept); a 404 falls back to the fleet view
  for (const [status, fallback] of [[503, false], [401, false], [404, true]] as const) {
    const f = { ...fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
      files: { '/tmp/home/.config/cortados/provider-token': TOKEN },
      http: () => ({ status, ok: false, headers: {}, text: 'secret ' + TOKEN }) }) }
    expect(await fetchTree(f as any, FALLBACK, tag)).toBe(!fallback)
    const e = (await (f as any).state.get({ plugin: 'cortados-pulse', key: 'treeErr' })).value
    if (fallback) expect(e).toBe(null)
    else { expect(e).toMatch(status === 401 ? /rejected the token/ : /HTTP 503/); expect(String(e).includes(TOKEN)).toBe(false) }
  }
})

// ---- this machine's own task log (#118) -------------------------------------------------------------------------------

const NOW_S = 1_800_000_000                               // the fixture's clock (tests/test_live NOW)
const HERE = 't20261114160000-0000000a'                   // a host "here" task on this machine, only in its own log
const HERE_KID = 't20261114160500-0000000b'               // nested under HERE, no origin of its own
const HERE_DONE = 't20261114150000-0000000c'
const THEIRS_HERE = 't20261114160000-0000000f'           // another session's task on this machine
const SECRET_LOCAL = 'SECRET-PROMPT-TEXT-0118'

function localLog(extra: object[] = []): string {
  const base = { cwd: '/Users/someone/secret-project', prompt: SECRET_LOCAL, orchestrator: 'claude-app', host: 'pro' }
  return [
    { ts: NOW_S - 30 * 3600, event: 'started', task_id: 't20261113010000-00000001', ...base, origin_session: MINE_TAG },
    { ts: NOW_S - 600, event: 'started', task_id: HERE, ...base, origin_session: MINE_TAG, title: 'Here and now', model: 'claude-sonnet-5' },
    { ts: NOW_S - 300, event: 'started', task_id: HERE_KID, ...base, parent_task_id: HERE, title: 'Its child' },
    { ts: NOW_S - 4000, event: 'started', task_id: HERE_DONE, ...base, origin_session: MINE_TAG, title: 'Done here' },
    { ts: NOW_S - 3000, event: 'finished', task_id: HERE_DONE, ...base, origin_session: MINE_TAG, status: 'failed', duration_s: 1000 },
    { ts: NOW_S - 100, event: 'started', task_id: THEIRS_HERE, ...base, origin_session: 'bbbbbbbbbbbbbbb2', title: 'Their task here' },
    { ts: NOW_S - 50, event: 'started', task_id: '../../etc/passwd', origin_session: MINE_TAG, title: 'bad id' },
    { ts: NOW_S - 40, event: 'task_exported', task_id: HERE },
    ...extra,
  ].map(e => JSON.stringify(e)).join('\n') + '\nnot json\n'
}

test('the local log gives this session\'s own tasks only, metadata only', () => {
  const got = parseLocalTasks(localLog(), MINE_TAG, NOW_S)
  expect(got.map(t => [t.task_id, t.state, t.status, t.host])).toEqual([
    [HERE_KID, 'running', 'running', 'pro'], [HERE, 'running', 'running', 'pro'], [HERE_DONE, 'failed', 'failed', 'pro']])
  const kid = got[0]
  expect([kid.parent_task_id, kid.origin_via]).toEqual([HERE, 'parent'])      // inherited through its parent
  expect(got[1].model).toBe('claude-sonnet-5')
  expect([got[2].started_at, got[2].ended_at]).toEqual([NOW_S - 4000, NOW_S - 3000])
  const raw = JSON.stringify(got)
  for (const bad of [SECRET_LOCAL, '/Users', 'claude-app', 'Their task here', THEIRS_HERE, 'passwd']) expect(raw.includes(bad)).toBe(false)
  expect(parseLocalTasks(localLog(), 'cccccccccccccccc', NOW_S)).toEqual([])
  // ten hours without an end event is unknown, never running
  const late = parseLocalTasks(localLog(), MINE_TAG, NOW_S + 11 * 3600 - 700)
  expect(late.find(t => t.task_id === HERE)?.state).toBe('unknown')
  // the read is bounded: a log far past the tail still gives its newest tasks
  const big = 'x'.repeat(LOCAL_TAIL_BYTES + 1000) + '\n' + localLog()
  expect(parseLocalTasks(big, MINE_TAG, NOW_S).map(t => t.task_id)).toEqual([HERE_KID, HERE, HERE_DONE])
})

test('local tasks merge into the session view by task id, once, with the counts in step', () => {
  const view = sessionFixture as any
  const before = JSON.stringify(view)
  const ROOT = 't20261111090000-a0000001'                  // in the view (from the provider host's log)
  const local = parseLocalTasks(localLog([
    { ts: NOW_S - 200, event: 'started', task_id: ROOT, origin_session: MINE_TAG, title: 'local copy of root' }]), MINE_TAG, NOW_S)
  const { view: merged, added } = mergeLocal(view, local, MINE_TAG, NOW_S)
  expect(JSON.stringify(view)).toBe(before)                 // the held view is never changed
  expect(added).toBe(3)
  const g = merged.orchestrators!.items!.find(x => x.origin === MINE_TAG)!
  const all: string[] = []
  const walk = (ts: any[] = []) => ts.forEach(t => { all.push(t.task_id); walk(t.tasks) })
  walk(g.tasks)
  expect(all.filter(x => x === ROOT).length).toBe(1)       // the provider's node, not doubled
  expect(JSON.stringify(merged).includes('local copy of root')).toBe(false)
  const here = g.tasks!.find(t => t.task_id === HERE)!
  expect(here.tasks!.map(t => t.task_id)).toEqual([HERE_KID])          // nested under its parent
  const og = (view.orchestrators.items as any[]).find(x => x.origin === MINE_TAG)
  expect(g.counts!.running).toBe(og.counts.running + 2)
  expect(g.counts!.failed ?? 0).toBe((og.counts.failed ?? 0) + 1)
  expect(g.attention).toBe(og.attention + 1)
  const lines = treeLines(merged, MINE_TAG, 0).map(l => l.text)
  expect(lines.some(l => l.startsWith('● Here and now'))).toBe(true)
  expect(lines.some(l => /↳ ● Its child/.test(l))).toBe(true)
  expect(lines.some(l => l.startsWith('✕ Done here · failed · took 16m 40s'))).toBe(true)
  // nothing local for this session: the view as it was
  expect(mergeLocal(view, [], MINE_TAG, NOW_S).view).toEqual(view)
  expect(LOCAL_NOTE(1)).toMatch(/1 task from this machine's task log only/)
})

test('a task this machine saw end, or knows first-hand, replaces an older report of it', () => {
  const view = { session_filter: MINE_TAG, orchestrators: { status: 'ok', errors: [], items: [{ origin: MINE_TAG, kind: 'session',
    counts: { running: 1, unknown: 1 }, attention: 1, tasks: [
      { task_id: HERE_DONE, state: 'running', status: 'running', host: 'pro', started_at: NOW_S - 4000, elapsed_s: 3990, tasks: [] },
      { task_id: HERE, state: 'unknown', status: 'unknown', phase: STALE_HB_PHASE, host: 'pro', started_at: NOW_S - 600, tasks: [] }] }] } }
  const { view: v, added } = mergeLocal(view as any, parseLocalTasks(localLog(), MINE_TAG, NOW_S), MINE_TAG, NOW_S)
  const g = v.orchestrators!.items![0]
  const node = (id: string) => g.tasks!.find(t => t.task_id === id)!
  expect([node(HERE_DONE).status, node(HERE_DONE).ended_at]).toEqual(['failed', NOW_S - 3000])
  expect([node(HERE).status, node(HERE).phase]).toEqual(['running', null])   // a stale heartbeat's guess, corrected
  expect(added).toBe(1)                                                       // HERE_KID only
  expect(g.counts).toEqual({ running: 2, unknown: 0, failed: 1 })
  expect(g.attention).toBe(1)                                                 // the failed one; the unknown is gone
})

test('the fallback draws this machine\'s tasks the provider\'s lists do not have', () => {
  const local = parseLocalTasks(localLog(), MINE_TAG, NOW_S)
  const lines = localLines(local, MINE_TAG, new Set([HERE_DONE]), NOW_S, 5).map(l => l.text)
  expect(lines[0]).toBe("from this machine's task log:")
  expect(lines.some(l => l.startsWith('● Here and now') && l.endsWith('10m 05s'))).toBe(true)   // keeps counting
  expect(lines.join('\n').includes('Done here')).toBe(false)                   // the provider's list has it
  expect(localLines(local, MINE_TAG, new Set(local.map(t => t.task_id!)), NOW_S, 0)).toEqual([])
})

test('the poll reads the local log every 30 s per session, and it stands when the provider is unreachable', async () => {
  const salt = '0123456789abcdef0123456789abcdef', sid = '792b087a-1983-4baf-b701-77792e4b77e4'
  const tag = await sessionTag(salt, sid)
  const log = localLog().split(MINE_TAG).join(tag)
  let reads = 0, now = NOW_S * 1000
  const base = fakeDollar({ env: { HOME: '/tmp/home', CORTADOS_HOME: '/tmp/state/', CORTADOS_PROVIDER_URL: 'http://air.example:8432' },
    files: { '/tmp/home/.config/cortados/provider-token': TOKEN, '/tmp/home/.config/cortados/office-salt': salt,
      '/tmp/state/child-tasks.jsonl': log },
    http: () => { throw new Error('connect ECONNREFUSED') } })
  const realRead = base.fs.read
  const $ = { ...base, session: { id: async () => sid }, clock: { now: async () => now },
    fs: { read: async (p: string) => { if (p.endsWith('child-tasks.jsonl')) reads++; return realRead(p) } } }
  await pollLive($ as any, FALLBACK, true)
  const held = (await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'local' })).value
  expect([held.tag, held.tasks.map((t: any) => t.task_id)]).toEqual([tag, [HERE_KID, HERE, HERE_DONE]])
  expect((await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'treeErr' })).value).toMatch(/could not read the provider/)
  expect((await ($ as any).state.get({ plugin: 'cortados-pulse', key: 'tree' })).value ?? null).toBe(null)
  // with no provider answer at all, the fallback lines still show this session's local tasks
  const lines = localLines(held.tasks, tag, new Set(), held.at / 1000, 0).map(l => l.text)
  expect(lines.some(l => l.startsWith('● Here and now'))).toBe(true)
  now += 10_000
  await pollLive($ as any, FALLBACK, true)
  expect(reads).toBe(1)                                         // within LOCAL_POLL_MS: not read again
  now += LOCAL_POLL_MS
  await pollLive($ as any, FALLBACK, true)
  expect(reads).toBe(2)
  // no log on this machine: no local tasks, nothing said
  const none = { ...fakeDollar({ env: { HOME: '/tmp/home' }, files: {} }) }
  await readLocal(none as any, tag)
  expect((await (none as any).state.get({ plugin: 'cortados-pulse', key: 'local' })).value.tasks).toEqual([])
})
