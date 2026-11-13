/**
 * cortados-pulse: a pane beside the chat over two of the CortadOS provider's views. GET /v1/cortados/live
 * (cortados_router/provider/live.py) feeds the RUNNING section: start_task children running or queued, the routed
 * conversations in flight, S1, the lowest subscription and the hosts' job heartbeats. GET /resources
 * (cortados_router/resources.py `snapshot`) feeds the rest: logged routes and S1 latency, every subscription lane's
 * cached scarcity with its per-window "% left" (one row per lane and per Claude account), and the online local devices.
 * The last line links the browser dashboard (<provider>/dashboard), which shows the same live view in full.
 *
 * THIS SESSION, at the top: the start_task children this very session started (running and queued, then the last few
 * finished), picked out of the live view by their origin_session. The pane names its session the way
 * cortados-routed-subagents names its Office floor and the cortados-route MCP server names a task's origin:
 * sha256("<salt>:<session id>") cut to 16 hex, the salt read from the machine's shared file
 * (~/.config/cortados/office-salt, made by the MCP server; CORTADOS_OFFICE_SALT_FILE overrides). The raw session id
 * never leaves the pane; without the salt file the section says the session cannot be identified.
 *
 * Where the provider is comes from the plugin config, else CORTADOS_PROVIDER_URL, else the per-machine URL file
 * (CORTADOS_PROVIDER_URL_FILE or ~/.config/cortados/provider-url): the same chain as cortados-routed-subagents, and
 * no address is committed because the provider runs on another host. The token is read from providerTokenFile
 * (default ~/.config/cortados/provider-token) on every poll, or from the inline providerToken override; it is sent as
 * the Bearer header and never logged, rendered or kept in an atom. Every message the pane shows is scrubbed of it.
 */
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { Pulse, PulseDevices, PulseLaneHealth, PulseLive, PulseLiveConversation, PulseLiveHost, PulseLiveTask,
  PulseReading, PulseUsage } from '../types'

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
/** How often the pane re-reads /v1/cortados/live: children start and end and conversations come and go within
 * seconds, so the Running section is read twice per /resources poll. The live view is cache-only on the provider too
 * (no vendor CLI, no ssh), and the pane's one timer ticks at this rate, reading /resources every other tick. */
export const LIVE_POLL_MS = 10_000
export const LIVE_PATH = '/v1/cortados/live'
/** Lines the Running section draws per list before it says "+N more": the pane is a side column, not the dashboard. */
export const RUNNING_MAX = 6
export const CONVERSATIONS_MAX = 4
/** A reading older than this is drawn as stale: the sampler's own cycle is at most 30 min (scarcity.MIN_INTERVAL),
 * so an hour-old reading means sampling has stopped, not that the window is quiet. */
export const STALE_AFTER_S = 3600
export const URL_FILE_DEFAULT = '~/.config/cortados/provider-url'

const PANE = 'cortados-pulse'
type Held = { data: Pulse; at: number }
const held = atom({ plugin: 'cortados-pulse', key: 'held' } as const, null as Held | null)
const err = atom({ plugin: 'cortados-pulse', key: 'err' } as const, null as string | null)
type LiveHeld = { data: PulseLive; at: number; base: string }
const liveHeld = atom({ plugin: 'cortados-pulse', key: 'live' } as const, null as LiveHeld | null)
const liveErr = atom({ plugin: 'cortados-pulse', key: 'liveErr' } as const, null as string | null)
type Mine = { tag: string | null; why: string | null }
const mine = atom({ plugin: 'cortados-pulse', key: 'mine' } as const, null as Mine | null)

/** The machine's shared Office salt (cortados_router/subagent_mcp.py OFFICE_SALT_DEFAULT). */
export const OFFICE_SALT_FILE = '~/.config/cortados/office-salt'
/** This session's finished tasks shown under its running ones: the last few, the dashboard has the rest. */
export const MINE_RECENT_MAX = 3

/** sha256("<salt>:<session id>") as hex, cut to 16: cortados-routed-subagents' sessionTag, subagent_mcp's origin_tag. */
export async function sessionTag(salt: string, sessionId: string): Promise<string> {
  const buf = await (globalThis as any).crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${salt}:${sessionId}`))
  return Array.from(new Uint8Array(buf), (b: number) => b.toString(16).padStart(2, '0')).join('').slice(0, 16)
}

/** This session's tag, or null with why (said in the pane). Never throws; never keeps the raw session id. */
export async function identify($: Pick<EngineInterface, 'env' | 'fs' | 'session'>): Promise<Mine> {
  let salt: string | null = null
  try {
    const path = expandHome(String((await $.env.get('CORTADOS_OFFICE_SALT_FILE')) || OFFICE_SALT_FILE).trim(), await $.env.get('HOME'))
    const v = String(await $.fs.read(path)).trim()
    if (/^[0-9a-f]{32}$/.test(v)) salt = v
  } catch { /* said below */ }
  if (!salt) return { tag: null, why: 'session not identified: no ' + OFFICE_SALT_FILE + ' yet (the cortados-route MCP server makes it)' }
  try {
    const id = await $.session.id()
    if (typeof id !== 'string' || !id) throw new Error()
    return { tag: await sessionTag(salt, id), why: null }
  } catch {
    return { tag: null, why: 'session not identified: its id could not be read' }
  }
}

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

type Fetched = { ok: true; data: object; url: string } | { ok: false; problem: string; status?: number }

/** GET <provider><path> with the Bearer token: the JSON object (scrubbed of the token, strings and keys alike) or the
 * one-line problem. Shared by both polls so the token rules are written once. */
export async function fetchView($: Pick<EngineInterface, 'env' | 'fs' | 'http'>, cfg: Cfg, path: string): Promise<Fetched> {
  let token = ''
  try {
    const url = await resolveProviderUrl($, cfg.providerUrl)
    token = await resolveToken($, cfg)
    const r = await $.http.fetch(url + path, { headers: { authorization: 'Bearer ' + token } })
    if (r.status === 401 || r.status === 403) {
      return { ok: false, status: r.status, problem: 'provider rejected the token (HTTP ' + r.status + '): check ' + (cfg.providerToken ? 'providerToken' : cfg.providerTokenFile) }
    }
    if (!r.ok) return { ok: false, status: r.status, problem: 'provider returned HTTP ' + r.status }
    const data = scrubResponse(JSON.parse(r.text), token)
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error(path + ' did not return a JSON object')
    return { ok: true, data, url }
  } catch (e) {
    const why = (e as Error)?.message ?? String(e)
    return { ok: false, problem: scrub('could not read the provider (' + why + ')', token) }
  }
}

/** One /resources poll: keep the snapshot; any failure becomes the pane's one-line problem. */
export async function poll($: Pick<EngineInterface, 'env' | 'fs' | 'http' | 'state' | 'clock'>, cfg: Cfg): Promise<void> {
  const f = await fetchView($, cfg, '/resources')
  if (!f.ok) {
    await update($, err, () => f.problem)
    return
  }
  const at = await $.clock.now()
  await update($, held, () => ({ data: f.data as Pulse, at }))
  await update($, err, () => null)
}

let liveInFlight = false

/** One /v1/cortados/live poll. A provider from before the live view answers 404: said as such, never as an outage.
 * One at a time: a tick that lands while the previous answer is still coming is skipped, never stacked. */
/** What a live poll touches. With `me` (the hooks pass true) it also names this session ($.session), every poll. */
type LiveDollar = Pick<EngineInterface, 'env' | 'fs' | 'http' | 'state' | 'clock'>

export async function pollLive($: LiveDollar, cfg: Cfg, me = false): Promise<void> {
  if (liveInFlight) return
  liveInFlight = true
  try {
    await pollLiveOnce($, cfg, me)
  } finally {
    liveInFlight = false
  }
}

async function pollLiveOnce($: LiveDollar, cfg: Cfg, me: boolean): Promise<void> {
  try {
    await fetchLive($, cfg)
  } finally {
    // after the fetch, every poll: /clear and resume move the pane to another session id
    if (me) {
      const m = await identify($ as Pick<EngineInterface, 'env' | 'fs' | 'session'>)
      await update($, mine, () => m)
    }
  }
}

async function fetchLive($: LiveDollar, cfg: Cfg): Promise<void> {
  const f = await fetchView($, cfg, LIVE_PATH)
  if (!f.ok) {
    await update($, liveErr, () => f.status === 404 ? 'this provider has no live view yet (update it to show running work)' : 'live: ' + f.problem)
    return
  }
  const at = await $.clock.now()
  await update($, liveHeld, () => ({ data: f.data as PulseLive, at, base: f.url }))
  await update($, liveErr, () => null)
}

// ---- formatting (pure; tested) ----------------------------------------------------------------------------------

export const gib = (b?: number | null): string => typeof b === 'number' ? (b / 2 ** 30).toFixed(1) + ' GiB' : '—'
export const pct = (v?: number | null): string => typeof v === 'number' && Number.isFinite(v) ? Math.round(v) + '%' : '—'

export function age(seconds: number): string {
  if (!(seconds >= 0)) return '?'
  if (seconds < 90) return Math.round(seconds) + ' s'
  if (seconds < 5400) return Math.round(seconds / 60) + ' min'
  return (seconds / 3600).toFixed(1) + ' h'
}

/** The window's % left as the S1 quota door computes it (s1/outcome/provider.py _left): headroom_pct, else 100 -
 * used_pct, else unknown. */
export function leftPct(w: { headroom_pct?: number | null; used_pct?: number | null }): number | null {
  if (typeof w.headroom_pct === 'number' && Number.isFinite(w.headroom_pct)) return w.headroom_pct
  if (typeof w.used_pct === 'number' && Number.isFinite(w.used_pct)) return 100 - w.used_pct
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
export function laneLines(lane: string, health: PulseLaneHealth | null | undefined, reading: PulseReading | undefined, nowS: number): string[] {
  const out: string[] = []
  health ??= {}
  // The projection exposes local admission and its reason, not deployment counts. Never infer quota from admission.
  if (lane === 'local') return [`local: ${health.ok === false ? 'unavailable' : health.ok === true ? 'eligible' : 'unknown'}` +
    (health.why ? ` · ${health.why}` : '')]
  // Only the provider's valid projection establishes quota; historical windows survive freshness invalidation.
  const known = typeof health.scarcity === 'number' && Number.isFinite(health.scarcity)
  const bits = [health.ok === false ? 'SPENT' : known ? 'ok' : 'quota unknown']
  if (known) bits.push('scarcity ' + health.scarcity!.toFixed(2))
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

/** An elapsed time at a glance, as the browser dashboard writes it: 42s, 3m 05s, 1h 12m, 2d 3h. */
export function dur(s?: number | null): string {
  if (typeof s !== 'number' || !Number.isFinite(s) || s < 0) return '—'
  s = Math.floor(s)
  if (s < 60) return s + 's'
  if (s < 3600) return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's'
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm'
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h'
}

const join = (parts: (string | null | undefined | false)[]) => parts.filter(Boolean).join(' · ')
const errs = (sec?: { errors?: { source?: string; error?: string }[] }) =>
  (sec?.errors ?? []).map(e => (e.source ?? '?') + ': ' + (e.error ?? 'error')).join('; ')
const modelOf = (x: { model?: string | null; lane?: string | null }) => x.model ? x.model + (x.lane ? ` (${x.lane})` : '') : (x.lane ?? null)

/** One start_task child: its glyph and state, title (else id), host, model and how long it has run or waited. `since`
 * is the seconds since the snapshot was fetched, so an elapsed time keeps counting between polls. */
export function taskLine(t: PulseLiveTask, since: number): string {
  const queued = t.state === 'queued'
  const name = t.title || ('task ' + (t.task_id ?? '?'))
  const elapsed = typeof t.elapsed_s === 'number' ? dur(t.elapsed_s + since) : '—'
  return (queued ? '◷ ' : '● ') + join([name, t.host ? 'on ' + t.host : null, modelOf(t), t.tool ? 'tool ' + t.tool : null,
    queued ? 'queued ' + elapsed : elapsed])
}

const KINDS: Record<string, string> = { orchestrator: 'main session', child_task: 'child task' }

export function conversationLine(c: PulseLiveConversation, since: number): string {
  const kind = KINDS[c.kind ?? ''] || c.kind || 'conversation'
  const name = c.title || join([c.project, kind])
  return (c.status === 'working' ? '▸ ' : '· ') + join([name, modelOf(c), c.tool ? 'tool ' + c.tool : null,
    c.status === 'working' ? null : c.status, typeof c.elapsed_s === 'number' ? dur(c.elapsed_s + since) : null])
}

/** The Running section: one summary line, the running and queued children, the conversations in flight. A section the
 * provider could not read says so in its place; it is never drawn as "nothing running". */
export function runningLines(live: PulseLive, since: number): string[] {
  const out: string[] = []
  const t = live.tasks ?? {}, c = live.conversations ?? {}, o = live.overview ?? {}
  const n = (v: unknown) => typeof v === 'number' ? String(v) : '?'
  out.push(`${n(o.running_tasks)} running · ${n(o.queued_tasks)} queued · ${n(o.active_conversations)} conversations` +
    (typeof o.working_conversations === 'number' ? ` (${o.working_conversations} working)` : ''))
  if (t.status === 'error') out.push('tasks unavailable: ' + errs(t))
  if (t.status === 'partial') out.push('tasks partial: ' + errs(t))
  const active = [...(t.running ?? []), ...(t.queued ?? [])]
  for (const task of active.slice(0, RUNNING_MAX)) out.push(taskLine(task, since))
  if (active.length > RUNNING_MAX) out.push(`  +${active.length - RUNNING_MAX} more tasks`)
  // The tasks are the provider host's only (scope): a task on another execution host logs there. Those started from
  // here are counted by host, state unknown.
  const away = t.elsewhere ?? []
  if (t.scope === 'provider host' || away.length) {
    const hosts = [...new Set(away.map(x => x.host ?? '?'))].join(', ')
    out.push(join([t.scope === 'provider host' ? `tasks on ${live.hosts?.provider_host || 'the provider host'} only` : null,
      away.length ? `${away.length} started elsewhere (${hosts})` : null]))
  }
  if (c.status === 'error') out.push('conversations unavailable: ' + errs(c))
  // A running child's own conversation is its task line above: listed once (it still counts in the summary).
  const shown = new Set(active.map(x => x.task_id))
  const conv = (c.items ?? []).filter(x => !(x.kind === 'child_task' && x.run_id && shown.has(x.run_id)))
  for (const x of conv.slice(0, CONVERSATIONS_MAX)) out.push(conversationLine(x, since))
  const more = conv.length - CONVERSATIONS_MAX + (c.hidden ?? 0)
  if (more > 0) out.push(`  +${more} more conversations`)
  if (!active.length && !conv.length && t.status !== 'error' && c.status !== 'error') out.push('nothing running')
  return out
}

/** This session's own tasks: running and queued first (as in the Running section), then the last few finished with
 * their state and how long they took. `tag` null: the reason the session cannot be identified. */
export function sessionLines(live: PulseLive, me: Mine | null, since: number): string[] {
  if (!me) return ['identifying this session…']
  if (!me.tag) return [me.why || 'session not identified']
  const t = live.tasks ?? {}
  if (t.status === 'error') return ['tasks unavailable: ' + errs(t)]
  const own = (x: PulseLiveTask) => x.origin_session === me.tag
  const active = [...(t.running ?? []), ...(t.queued ?? [])].filter(own)
  const done = (t.recent ?? []).filter(own)
  const out = active.slice(0, RUNNING_MAX).map(x => taskLine(x, since))
  if (active.length > RUNNING_MAX) out.push(`  +${active.length - RUNNING_MAX} more tasks`)
  for (const x of done.slice(0, MINE_RECENT_MAX)) {
    const mark = x.state === 'succeeded' ? '✓ ' : x.state === 'cancelled' ? '⊘ ' : x.state === 'failed' ? '✕ ' : '? '
    out.push(mark + join([x.title || ('task ' + (x.task_id ?? '?')), x.host ? 'on ' + x.host : null, modelOf(x),
      x.state, typeof x.elapsed_s === 'number' ? 'took ' + dur(x.elapsed_s) : null]))
  }
  if (done.length > MINE_RECENT_MAX) out.push(`  +${done.length - MINE_RECENT_MAX} more finished (dashboard)`)
  if (!active.length && !done.length) out.push('no task started from this session in the last 24 h')
  return out
}

const S1_FLAGS: Record<string, string> = { 'no-promote': 'promotion held', 'no-train-tonight': 'no training tonight',
  'nightly-stop': 'nightly loop stopping', 'sweep-requested': 'sweep requested' }

/** A host's hardware in a few words: CPU and GPU busy, disk free. */
export function hwText(hw: PulseLiveHost['hardware']): string | null {
  if (!hw) return null
  const p = (n: string, v: unknown) => typeof v === 'number' ? `${n} ${Math.round(v)}%` : null
  return join([p('cpu', hw.cpu_busy_pct), p('gpu', hw.gpu_busy_pct),
    typeof hw.disk_free_pct === 'number' ? `disk ${Math.round(hw.disk_free_pct)}% free` : null]) || null
}

/** S1, the lowest subscription and each host's heartbeat, one short line each (the S1 host's pipeline on its own line);
 * an absent heartbeat is said. */
export function fleetLines(live: PulseLive): string[] {
  const o = live.overview ?? {}, s1 = live.s1 ?? {}
  const out = [o.s1 === 'up' ? 'S1 up' + (typeof s1.latency_ms === 'number' ? ` · ${s1.latency_ms} ms` : '')
    : o.s1 === 'down' ? 'S1 DOWN' + (s1.reason ? ' · ' + s1.reason : '') : 'S1 unknown']
  const low = o.lowest_subscription
  if (low && typeof low.left_pct === 'number') out.push(`lowest left: ${low.label || low.lane} ${pct(low.left_pct)}` + (low.window ? ` (${low.window})` : ''))
  for (const h of live.hosts?.items ?? []) {
    const hb = h.heartbeat
    if (!hb) { out.push(`${h.name}: no heartbeat yet`); continue }
    const jobs = (hb.jobs ?? []).map(j => `${j.name} ${j.state}` + (typeof j.progress_pct === 'number' ? ' ' + Math.round(j.progress_pct) + '%' : ''))
    out.push(`${h.name}: ` + join([h.heartbeat_state === 'stale' ? 'STALE heartbeat ' + age(hb.age_s ?? NaN) + ' old' : null,
      jobs.length ? jobs.join(', ') : 'no jobs', hwText(h.hardware)]))
    const s = hb.s1
    if (s && h.heartbeat_state !== 'stale') out.push(`  S1 ${s.health === 'up' ? 'up' : s.health === 'down' ? 'DOWN' : 'unknown'}: ` +
      join([s.model || 'no model loaded', ...(s.flags ?? []).map(f => S1_FLAGS[f] || f),
        s.pending ? 'candidate ' + s.pending.replace(/_/g, ' ') : null]))
  }
  return out
}

/** Subscription lanes in the order the pane shows them: the Claude accounts first, then the rest alphabetically. */
export function laneOrder(lanes: string[]): string[] {
  const rank = (l: string) => (l === 'claude' || l.startsWith('claude#')) ? 0 : l === 'local' ? 2 : 1
  return [...lanes].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
}

// ---- hooks -------------------------------------------------------------------------------------------------------

let ticking: Timer | null = null
let ticks = 0

export const register: Register = (on, options) => {
  const cfg = readConfig(options)

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'cortados-pulse', description: 'Open the CortadOS router pulse pane' })
    void $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void pollLive($, cfg, true)
    void poll($, cfg)
    ticking?.cancel()                                          // a second session.start must not double the polling
    ticks = 0
    ticking = $.clock.every(LIVE_POLL_MS, () => {              // live every tick, /resources every POLL_MS
      void pollLive($, cfg, true)
      if (++ticks % (POLL_MS / LIVE_POLL_MS) === 0) void poll($, cfg)
    })
    return next(e)
  })

  on('command.run', { command: 'cortados-pulse' }, async $ => {
    await $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void pollLive($, cfg, true)
    void poll($, cfg)
    return { text: 'CortadOS pulse pane opened.' }
  })

  // The tree is built with the element table's constructors (what JSX compiles to under the engine's `h`) rather
  // than JSX, so the module has no JSX factory to resolve and the pane draws identically under `claude plugin test`.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const line = (text: string) => Text({ children: text })
    const dim = (text: string) => Text({ dimColor: true, children: text })
    const head = (text: string) => Text({ bold: true, children: text })
    const h = await read($, held)
    const problem = await read($, err)
    const lv = await read($, liveHeld)
    const lvProblem = await read($, liveErr)
    const me = await read($, mine)
    const nowMs = await $.clock.now()
    const running = [
      head('This session'),
      ...(lv ? sessionLines(lv.data, me, (nowMs - lv.at) / 1000).map(line) : []),
      ...(lv ? [] : [dim(me && !me.tag ? me.why || '' : 'waiting for the live view…')]),
      head('Running'),
      ...(lv ? runningLines(lv.data, (nowMs - lv.at) / 1000).map(line) : lvProblem ? [] : [dim('loading the live view…')]),
      ...(lvProblem ? [dim(lvProblem + (lv ? ' · showing ' + age((nowMs - lv.at) / 1000) + ' old data' : ''))] : []),
    ]
    const fleet = lv ? [head('Fleet'), ...fleetLines(lv.data).map(line)] : []
    const link = lv ? [dim('dashboard ' + lv.base + '/dashboard')] : []
    if (!h) return Box({ flexDirection: 'column', children: [...running, ...fleet, dim(problem || 'loading the resource pool…'), ...link] })
    const s = h.data
    const nowS = typeof s.generated_at === 'number' ? s.generated_at + (nowMs - h.at) / 1000 : nowMs / 1000
    const subs = s.subscriptions ?? {}
    const readings = s.readings ?? {}
    const lanes = laneOrder(Object.keys(subs).filter(k => k !== 'error' && subs[k] !== null && typeof subs[k] === 'object'))
    const subsError = typeof subs.error === 'string' ? subs.error : null
    const readingsError = typeof readings.error === 'string' ? readings.error : null
    const rows = [
      ...running,
      head('Usage'),
      ...usageLines(s.usage ?? {}).map(line),
      head('Subscriptions'),
      ...(subsError ? [dim('subscriptions: ' + subsError)] : []),
      ...(readingsError ? [dim('readings: ' + readingsError)] : []),
      ...(lanes.length === 0 && !subsError ? [dim('none cached')] : []),
      ...lanes.flatMap(lane => laneLines(lane, subs[lane] as PulseLaneHealth,
        typeof readings[lane] === 'object' ? readings[lane] as PulseReading : undefined, nowS)).map(line),
      head('Devices'),
      ...deviceLines(s.devices ?? { status: 'pending', nodes: [] }).map(line),
      ...fleet,
      dim('snapshot ' + age((nowMs - h.at) / 1000) + ' old' + (problem ? ' · ' + problem : '')),
      ...link,
    ]
    return Box({ flexDirection: 'column', children: rows })
  })
}
