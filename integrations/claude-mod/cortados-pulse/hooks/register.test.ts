import { test, expect } from 'claude-code/testing'
import { FALLBACK, POLL_MS, STALE_AFTER_S, expandHome, resolveProviderUrl, resolveToken, scrub, readConfig, poll,
  age, pct, gib, leftPct, laneLabel, laneLines, usageLines, deviceLines, laneOrder } from './register'

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
  }
  return $
}

test('defaults commit no address and point the token at the per-machine file', () => {
  expect(FALLBACK.providerUrl).toBe('')
  expect(FALLBACK.providerTokenFile).toBe('~/.config/cortados/provider-token')
  expect(FALLBACK.providerToken).toBe('')
  expect(POLL_MS).toBe(20_000)
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
  expect(gib(2 ** 30 * 1.5)).toBe('1.5 GiB')
  expect(gib(undefined)).toBe('—')
  expect(leftPct({ headroom_pct: 40 })).toBe(40)
  expect(leftPct({ used_pct: 70 })).toBe(30)
  expect(leftPct({})).toBe(null)
  expect(laneOrder(['local', 'codex', 'claude#b', 'cursor', 'claude'])).toEqual(['claude', 'claude#b', 'codex', 'cursor', 'local'])
})

test('lane lines: per-account Claude with weekly windows and pools, Codex, a spent lane, the local lane', () => {
  const now = 1_700_000_000
  const claudeB = laneLines('claude#b', { ok: true, scarcity: -0.3, expiring_pct: 34, config_dir: '/Users/me/.claude-acctB', pools: { 'claude:fable': { ok: true, scarcity: 0.1 } } },
    { fetched_at: now - 600, failed: false, windows: [
      { window: 'week (all models)', pool: 'all', headroom_pct: 62.4, reset_epoch: now + 86400 },
      { window: 'week (fable)', pool: 'fable', used_pct: 70, stale: true },
      { window: '5h', pool: 'all', headroom_pct: 91 }] }, now)
  expect(claudeB).toEqual([
    'claude#b @.claude-acctB: ok · scarcity -0.30 · expiring 34% · sample 10 min old',
    '  week (all models): 62% left',
    '  week (fable) [fable]: 30% left (stale)',
    '  5h: 91% left',
  ])
  expect(laneLabel('claude', {})).toBe('claude')
  const codex = laneLines('codex', { ok: true, scarcity: 0.05, expiring_pct: 0 }, { fetched_at: now - 4000, failed: true, windows: [] }, now)
  expect(codex[0]).toBe('codex: ok · scarcity 0.05 · last sample FAILED · sample 67 min old (stale)')
  expect(codex.length).toBe(1)
  const spent = laneLines('cursor', { ok: false, why: 'month 100% used, resets 2026-12-01', scarcity: 1.2, pools: { 'cursor:cursor-models': { ok: false, scarcity: 1.2 }, 'cursor:other-models': { ok: true, scarcity: 0.2 } } }, undefined, now)
  expect(spent).toEqual([
    'cursor: SPENT · scarcity 1.20',
    '  month 100% used, resets 2026-12-01',
    '  pool cursor:cursor-models: SPENT · scarcity 1.20',
    '  pool cursor:other-models: ok · scarcity 0.20',
  ])
  expect(laneLines('local', { resource_managed: true, deployments: { 'studio/qwen3-4b': {} }, why: 'available: measured local resources' }, undefined, now))
    .toEqual(['local: 1 verified deployment · available: measured local resources'])
  expect(laneLines('local', { resource_managed: true, deployments: {} }, undefined, now)).toEqual(['local: 0 verified deployments'])
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
  expect(every.map(t => t.ms)).toEqual([POLL_MS])
  expect($.calls.urls).toEqual(['http://air.example:8432/resources'])
  expect($.held().data.usage.total_routes).toBe(1)
  await hooks['session.start']($, {}, async (e: any) => e)
  expect(every.length).toBe(2)
  expect(every[0]!.cancelled).toBe(true)
  expect(every[1]!.cancelled).toBe(false)
  expect(await hooks['command.run']($, {})).toEqual({ text: 'CortadOS pulse pane opened.' })
  expect(opened.length).toBe(3)
})
