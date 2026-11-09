/**
 * cortados-pulse: a pane beside the chat over the CortadOS provider's GET /resources (cortados_router/resources.py
 * `snapshot`): logged routes and S1 health, every subscription lane's cached scarcity with its per-window "% left"
 * (one row per lane and per Claude account: `claude`, `claude#<id>`, `codex`, `cursor`, ...), and the online local
 * devices with their GPU headroom and model fit.
 *
 * Where the provider is comes from the plugin config, else CORTADOS_PROVIDER_URL, else the per-machine URL file
 * (CORTADOS_PROVIDER_URL_FILE or ~/.config/cortados/provider-url): the same chain as cortados-routed-subagents, and
 * no address is committed because the provider runs on another host. The token is read from providerTokenFile
 * (default ~/.config/cortados/provider-token) on every poll, or from the inline providerToken override; it is sent as
 * the Bearer header and never logged, rendered or kept in an atom. Every message the pane shows is scrubbed of it.
 */
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { Pulse, PulseDevices, PulseLaneHealth, PulseReading, PulseUsage } from '../types'

type Cfg = {
  providerUrl: string
  providerTokenFile: string
  providerToken: string
}

/** Defaults for a setting the config leaves unset. tests/test_mod_manifest.py checks each key is declared in
 * plugin.json with the same default, since Claude Code refuses an undeclared userConfig key. */
export const FALLBACK: Cfg = {
  // Empty on purpose: the provider runs on another host, so no address (and no private 127.0.0.1) is a sane default.
  providerUrl: '',
  providerTokenFile: '~/.config/cortados/provider-token',
  providerToken: '',
}

/** How often the pane re-reads /resources. The scarcity cache behind it is sampled every 15-30 min and the route
 * counts move slowly, so 20 s already shows every change promptly; /resources is cache-only on the provider (never
 * opens a vendor CLI), so the poll is cheap on both ends. */
export const POLL_MS = 20_000
/** A reading older than this is drawn as stale: the sampler's own cycle is at most 30 min (scarcity.MIN_INTERVAL),
 * so an hour-old reading means sampling has stopped, not that the window is quiet. */
export const STALE_AFTER_S = 3600
export const URL_FILE_DEFAULT = '~/.config/cortados/provider-url'

const PANE = 'cortados-pulse'
type Held = { data: Pulse; at: number }
const held = atom({ plugin: 'cortados-pulse', key: 'held' } as const, null as Held | null)
const err = atom({ plugin: 'cortados-pulse', key: 'err' } as const, null as string | null)

/** `~` and `~/...` to $HOME (the only form the config documents; `~user` is left alone). */
export function expandHome(path: string, home: string | undefined): string {
  if (path === '~' || path.startsWith('~/')) {
    if (!home) throw new Error('cannot expand ~: HOME is unset')
    return home + path.slice(1)
  }
  return path
}

/** Plugin config stays an override; otherwise env, then the selected or per-machine URL file. Refuses anything that is
 * not one http(s) URL with a host, naming the source (never a silent fallback to a guessed address). */
export async function resolveProviderUrl($: Pick<EngineInterface, 'env' | 'fs'>, configured = ''): Promise<string> {
  let source = 'providerUrl config'
  let value = String(configured ?? '').trim()
  if (!value) {
    value = ((await $.env.get('CORTADOS_PROVIDER_URL')) || '').trim()
    source = 'CORTADOS_PROVIDER_URL'
    if (!value) {
      const path = expandHome((await $.env.get('CORTADOS_PROVIDER_URL_FILE')) || URL_FILE_DEFAULT, await $.env.get('HOME'))
      source = 'provider URL file ' + path
      try { value = String(await $.fs.read(path)).trim() }
      catch { throw new Error(source + ': cannot read it; set providerUrl in the plugin config or CORTADOS_PROVIDER_URL') }
    }
  }
  try {
    const parsed = new URL(value)
    if (!/^https?:\/\//.test(value) || !parsed.hostname || /[\s\x00-\x1f\x7f]/.test(value)) throw new Error()
  } catch { throw new Error(source + ': expected one http:// or https:// URL with a host') }
  return value.replace(/\/+$/, '')
}

/** The inline override if set, else the token file (read fresh each poll, so a rotated token is picked up without a
 * restart). The error for a missing file names the path, never any content. */
export async function resolveToken($: Pick<EngineInterface, 'env' | 'fs'>, cfg: Cfg): Promise<string> {
  const inline = String(cfg.providerToken ?? '').trim()
  if (inline) return inline
  const path = expandHome(String(cfg.providerTokenFile || FALLBACK.providerTokenFile), await $.env.get('HOME'))
  let text: string
  try { text = String(await $.fs.read(path)) }
  catch { throw new Error('token file ' + path + ': cannot read it (set providerTokenFile, or providerToken inline)') }
  const token = text.trim()
  if (!token) throw new Error('token file ' + path + ' is empty')
  return token
}

/** Every occurrence of the token replaced, so no message the pane shows can leak it. */
export function scrub(text: string, token: string): string {
  return token ? text.split(token).join('***') : text
}

/** Scrub parsed strings AND keys before state storage; JSON escapes cannot bypass token redaction. */
export function scrubResponse(value: unknown, token: string): unknown {
  if (typeof value === 'string') return scrub(value, token)
  if (Array.isArray(value)) return value.map(v => scrubResponse(v, token))
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.entries(value).map(([k, v]) => [scrub(k, token), scrubResponse(v, token)]))
  return value
}

export function readConfig(options: unknown): Cfg {
  const o = (options ?? {}) as Partial<Record<keyof Cfg, unknown>>
  return {
    providerUrl: typeof o.providerUrl === 'string' ? o.providerUrl : FALLBACK.providerUrl,
    providerTokenFile: typeof o.providerTokenFile === 'string' && o.providerTokenFile.trim() ? o.providerTokenFile : FALLBACK.providerTokenFile,
    providerToken: typeof o.providerToken === 'string' ? o.providerToken : FALLBACK.providerToken,
  }
}

/** One poll: resolve where and how, fetch, keep the snapshot; any failure becomes the pane's one-line problem. */
export async function poll($: Pick<EngineInterface, 'env' | 'fs' | 'http' | 'state' | 'clock'>, cfg: Cfg): Promise<void> {
  let token = ''
  try {
    const url = await resolveProviderUrl($, cfg.providerUrl)
    token = await resolveToken($, cfg)
    const r = await $.http.fetch(url + '/resources', { headers: { authorization: 'Bearer ' + token } })
    if (r.status === 401 || r.status === 403) {
      await update($, err, () => 'provider rejected the token (HTTP ' + r.status + '): check ' + (cfg.providerToken ? 'providerToken' : cfg.providerTokenFile))
      return
    }
    if (!r.ok) {
      await update($, err, () => 'provider returned HTTP ' + r.status)
      return
    }
    const data = scrubResponse(JSON.parse(r.text), token)
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('/resources did not return a JSON object')
    const at = await $.clock.now()
    await update($, held, () => ({ data: data as Pulse, at }))
    await update($, err, () => null)
  } catch (e) {
    const why = (e as Error)?.message ?? String(e)
    await update($, err, () => scrub('could not read the provider (' + why + ')', token))
  }
}

// ---- formatting (pure; tested) ----------------------------------------------------------------------------------

export const gib = (b?: number | null): string => typeof b === 'number' ? (b / 2 ** 30).toFixed(1) + ' GiB' : '—'
export const pct = (v?: number | null): string => typeof v === 'number' ? Math.round(v) + '%' : '—'

export function age(seconds: number): string {
  if (!(seconds >= 0)) return '?'
  if (seconds < 90) return Math.round(seconds) + ' s'
  if (seconds < 5400) return Math.round(seconds / 60) + ' min'
  return (seconds / 3600).toFixed(1) + ' h'
}

/** The window's % left as the S1 quota door computes it (s1/outcome/provider.py _left): headroom_pct, else 100 -
 * used_pct, else unknown. */
export function leftPct(w: { headroom_pct?: number | null; used_pct?: number | null }): number | null {
  if (typeof w.headroom_pct === 'number') return w.headroom_pct
  if (typeof w.used_pct === 'number') return 100 - w.used_pct
  return null
}

/** Account identity is exposed in readings, not the subscription projection. */
export function laneLabel(lane: string, health: Pick<PulseReading, 'config_dir'>): string {
  const dir = typeof health.config_dir === 'string' ? health.config_dir.replace(/\/+$/, '') : ''
  const base = dir.slice(dir.lastIndexOf('/') + 1)
  return base ? lane + ' @' + base : lane
}

/** One lane as lines: the headline (quota state, scarcity, expiring credit), then each cached window's % left with
 * its separately metered pool. `nowS` is the pane's clock in seconds, for the reading's age. */
export function laneLines(lane: string, health: PulseLaneHealth, reading: PulseReading | undefined, nowS: number): string[] {
  const out: string[] = []
  // The projection exposes local admission and its reason, not deployment counts. Never infer quota from admission.
  if (lane === 'local') return [`local: ${health.ok === false ? 'unavailable' : health.ok === true ? 'eligible' : 'unknown'}` +
    (health.why ? ` · ${health.why}` : '')]
  const known = typeof health.scarcity === 'number' || (reading?.windows ?? []).some(w => leftPct(w) !== null)
  const bits = [health.ok === false ? 'SPENT' : known ? 'ok' : 'quota unknown']
  if (typeof health.scarcity === 'number') bits.push('scarcity ' + health.scarcity.toFixed(2))
  if (typeof health.expiring_pct === 'number' && health.expiring_pct > 0) bits.push('expiring ' + Math.round(health.expiring_pct) + '%')
  if (reading) {
    if (reading.failed) bits.push('last sample FAILED')
    if (typeof reading.fetched_at === 'number') {
      const s = nowS - reading.fetched_at
      bits.push('sample ' + age(s) + ' old' + (s > STALE_AFTER_S ? ' (stale)' : ''))
    }
  }
  out.push(laneLabel(lane, reading ?? {}) + ': ' + bits.join(' · '))
  if (health.ok === false && health.why) out.push('  ' + health.why)
  for (const w of reading?.windows ?? []) {
    const name = (w.window || 'window') + (w.pool && w.pool !== 'all' ? ` [${w.pool}]` : '')
    out.push(`  ${name}: ${pct(leftPct(w))} left` + (w.stale ? ' (stale)' : ''))
  }
  return out
}

export function usageLines(u: PulseUsage): string[] {
  const out: string[] = []
  if (u.error) out.push('usage: ' + u.error)
  const deg = u.degraded ?? { count: 0, of: 0 }
  out.push(`routes ${u.total_routes ?? '—'} · degraded ${deg.count}/${deg.of}`)
  const lat = u.s1_latency_ms ?? {}
  out.push(`S1 ${lat.median ?? '—'} ms median · p90 ${lat.p90 ?? '—'} ms` + (lat.window ? ` (last ${lat.window})` : ''))
  const top = Object.entries(u.by_lane ?? {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 3)
  if (top.length) out.push('top ' + top.map(([k, n]) => `${k} ${n}`).join(' · '))
  return out
}

export function deviceLines(dev: PulseDevices): string[] {
  const out: string[] = []
  if (dev.status !== 'ok') {
    out.push(dev.status === 'pending' ? 'sample pending' : 'unavailable' + (dev.error ? ': ' + dev.error : ''))
    return out
  }
  for (const n of dev.nodes ?? []) {
    out.push(`${n.id} ${n.status ?? ''} · ${gib(n.gpu_headroom_estimate_bytes)} free` +
      (typeof n.gpu_utilization_pct === 'number' ? ` · gpu ${Math.round(n.gpu_utilization_pct)}%` : '') +
      ` · fits ${(n.fits ?? []).length}`)
  }
  if (typeof dev.pool_headroom_estimate_bytes === 'number') {
    out.push(`pool ${gib(dev.pool_headroom_estimate_bytes)} · fits ${(dev.pool_fits ?? []).length}`)
  }
  if (!(dev.nodes ?? []).length) out.push('no nodes online')
  return out
}

/** Subscription lanes in the order the pane shows them: the Claude accounts first, then the rest alphabetically. */
export function laneOrder(lanes: string[]): string[] {
  const rank = (l: string) => (l === 'claude' || l.startsWith('claude#')) ? 0 : l === 'local' ? 2 : 1
  return [...lanes].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
}

// ---- hooks -------------------------------------------------------------------------------------------------------

let ticking: Timer | null = null

export const register: Register = (on, options) => {
  const cfg = readConfig(options)

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'cortados-pulse', description: 'Open the CortadOS router pulse pane' })
    void $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void poll($, cfg)
    ticking?.cancel()                                          // a second session.start must not double the polling
    ticking = $.clock.every(POLL_MS, () => void poll($, cfg))
    return next(e)
  })

  on('command.run', { command: 'cortados-pulse' }, async $ => {
    await $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void poll($, cfg)
    return { text: 'CortadOS pulse pane opened.' }
  })

  // The tree is built with the element table's constructors (what JSX compiles to under the engine's `h`) rather
  // than JSX, so the module has no JSX factory to resolve and the pane draws identically under `claude plugin test`.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const line = (text: string) => Text({ children: text })
    const dim = (text: string) => Text({ dimColor: true, children: text })
    const h = await read($, held)
    const problem = await read($, err)
    if (!h) return Box({ flexDirection: 'column', children: [dim(problem || 'loading the resource pool…')] })
    const s = h.data
    const nowMs = await $.clock.now()
    const nowS = typeof s.generated_at === 'number' ? s.generated_at + (nowMs - h.at) / 1000 : nowMs / 1000
    const subs = s.subscriptions ?? {}
    const readings = s.readings ?? {}
    const lanes = laneOrder(Object.keys(subs).filter(k => k !== 'error' && typeof subs[k] === 'object'))
    const subsError = typeof subs.error === 'string' ? subs.error : null
    const readingsError = typeof readings.error === 'string' ? readings.error : null
    const rows = [
      ...usageLines(s.usage ?? {}).map(line),
      dim('— subscriptions —'),
      ...(subsError ? [dim('subscriptions: ' + subsError)] : []),
      ...(readingsError ? [dim('readings: ' + readingsError)] : []),
      ...(lanes.length === 0 && !subsError ? [dim('none cached')] : []),
      ...lanes.flatMap(lane => laneLines(lane, subs[lane] as PulseLaneHealth,
        typeof readings[lane] === 'object' ? readings[lane] as PulseReading : undefined, nowS)).map(line),
      dim('— devices —'),
      ...deviceLines(s.devices ?? { status: 'pending', nodes: [] }).map(line),
      dim('snapshot ' + age((nowMs - h.at) / 1000) + ' old' + (problem ? ' · ' + problem : '')),
    ]
    return Box({ flexDirection: 'column', children: rows })
  })
}
