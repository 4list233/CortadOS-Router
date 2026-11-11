import { test, expect } from 'claude-code/testing'
import { FALLBACK, POLL_MS, LIVE_POLL_MS, STALE_AFTER_S, expandHome, resolveProviderUrl, resolveToken, scrub, readConfig, poll,
  pollLive, age, pct, gib, dur, leftPct, laneLabel, laneLines, usageLines, deviceLines, laneOrder, taskLine, conversationLine,
  runningLines, fleetLines, hwText } from './register'
import liveFixture from './live.fixture'

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
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  expect(registered).toEqual([{ name: 'cortados-pulse', description: 'Open the CortadOS router pulse pane' }])
  expect(opened).toEqual([{ id: 'cortados-pulse', title: 'CortadOS pulse' }])
  expect(every.map(t => t.ms)).toEqual([LIVE_POLL_MS])
  expect($.calls.urls).toEqual(['http://air.example:8432/v1/cortados/live', 'http://air.example:8432/resources'])
  expect($.held().data.usage.total_routes).toBe(1)
  await hooks['session.start']($, {}, async (e: any) => e)
  expect(every.length).toBe(2)
  expect(every[0]!.cancelled).toBe(true)
  expect(every[1]!.cancelled).toBe(false)
  expect(await hooks['command.run']($, {})).toEqual({ text: 'CortadOS pulse pane opened.' })
  expect(opened.length).toBe(3)
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
