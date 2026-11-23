/**
 * cortados-pulse: a pane beside the chat over two of the CortadOS provider's views. GET /v1/cortados/live
 * (cortados_router/provider/live.py) feeds the RUNNING section: start_task children running or queued, the routed
 * conversations in flight, S1, the lowest subscription and the hosts' job heartbeats. GET /resources
 * (cortados_router/resources.py `snapshot`) feeds the rest: logged routes and S1 latency, every subscription lane's
 * cached scarcity with its per-window "% left" (one row per lane and per Claude account), and the online local devices.
 * The last line links the browser dashboard (<provider>/dashboard), which shows the same live view in full.
 *
 * 0.6.3, the DEFAULT VIEW (owner 2026-11-22: "way too complicated"): this session's child tasks one line each (and this
 * machine's tasks that carry no session tag, under their own heading), the local Pro model, the subscriptions' lowest
 * and any that are out, S1, the dashboard link. Everything described below is the DETAILS, behind `d`.
 *
 * THIS SESSION, at the top: a live tree of this session's own work, flightdeck-style (docs/PULSE-PER-SESSION.md): the
 * start_task children it started, each child's sub-sessions (its routed conversations) under it, nested tasks under
 * their parent, and the subagents this session routed directly; each node with its state glyph, model, phase or tool,
 * tokens and an elapsed clock that keeps counting between polls. The pane names its session the way
 * cortados-routed-subagents names its Office floor and the cortados-route MCP server names a task's origin:
 * sha256("<salt>:<session id>") cut to 16 hex, the salt read from the machine's shared file
 * (~/.config/cortados/office-salt, made by the MCP server; CORTADOS_OFFICE_SALT_FILE overrides). The raw session id
 * never leaves the pane; without the salt file the section says the session cannot be identified.
 *
 * With its tag the pane reads ONLY GET /v1/cortados/live/session?session=<tag>, which the provider builds filtered: that
 * one subtree plus fleet health that names no session (S1, hosts). The fleet view (/v1/cortados/live, every session's
 * tasks and conversations) is read only as a fallback: no tag, or a provider from before the per-session view.
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
  PulseLocalNode, PulseLocalView, PulseOrchestrator, PulseReading, PulseSessionSplit, PulseSessionView, PulseSplitTokens,
  PulseSplitWindow, PulseSubSession, PulseTokens, PulseTreeTask, PulseUsage } from '../types'

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
/** This session's own subtree (provider/live.py SESSION_PATH, session_view). */
export const SESSION_PATH = '/v1/cortados/live/session'
/** Sub-sessions drawn under a running task before "+N more": a side pane, not the dashboard (which lists 8). */
export const SUBS_MAX = 4
/** Subagents this session routed directly, drawn after its tasks. */
export const ROUTED_SHOWN = 3
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
/** GET /v1/cortados/live?session=<tag>: this session's tasks filtered by the provider BEFORE its list caps (a busy fleet
 * pushes them out of the fleet view's lists). null: not fetched, or a provider that ignored ?session= (filter the fleet). */
type MineLive = { data: PulseLive; at: number; tag: string }
const mineLive = atom({ plugin: 'cortados-pulse', key: 'mineLive' } as const, null as MineLive | null)
/** GET /v1/cortados/live/session?session=<tag>: this session's subtree. Drawn only for the tag it was fetched for. */
type TreeHeld = { data: PulseSessionView; at: number; tag: string; base: string }
const tree = atom({ plugin: 'cortados-pulse', key: 'tree' } as const, null as TreeHeld | null)
const treeErr = atom({ plugin: 'cortados-pulse', key: 'treeErr' } as const, null as string | null)
/** This machine's own task log, this session's tasks in it (#118): merged into the tree by task id. */
type LocalHeld = { tag: string; at: number; tasks: LocalTask[]; note?: string | null }
const localHeld = atom({ plugin: 'cortados-pulse', key: 'local' } as const, null as LocalHeld | null)

/** #127: the expanded work split breakdown, toggled by its `w` button. */
const wsOpen = atom({ plugin: 'cortados-pulse', key: 'wsOpen' } as const, false as boolean)
// 0.6.3: everything but the five default lines sits behind `d` (the details), as the work split breakdown sits behind `w`
const detailsOpen = atom({ plugin: 'cortados-pulse', key: 'detailsOpen' } as const, false as boolean)
// 0.6.3: this machine's tag as cortados-route records it (origin_machine: sha256("<salt>:cortados:machine") of the
// machine's 0600 Office salt), computed once: the session view is asked for its tasks that carry no session tag too
// (&machine=), so a task started through a server that knew no session (the Claude desktop app's own cortados-route)
// is still shown, under its own heading. Only a process that reads this machine's salt can name it (review round 1:
// the hostname was caller-supplied and opened another machine's tasks; it is display only now)
const machine = atom({ plugin: 'cortados-pulse', key: 'machine' } as const, null as string | null)
/** #127: GET /v1/cortados/local (provider/local_switch.py): the local nodes as their gates report them. */
type NodesHeld = { data: PulseLocalView; at: number }
const nodes = atom({ plugin: 'cortados-pulse', key: 'nodes' } as const, null as NodesHeld | null)
const nodesErr = atom({ plugin: 'cortados-pulse', key: 'nodesErr' } as const, null as string | null)
/** #127: the origin tags this session's own start_task results named (child_tasks.start's origin_session). */
type Started = { tag: string; task_id: string; at: number }
const started = atom({ plugin: 'cortados-pulse', key: 'started' } as const, [] as Started[])
/** #127: the session view of the newest such tag that is not this session's own. */
export type AliasHeld = { data: PulseSessionView; at: number; tag: string }
const alias = atom({ plugin: 'cortados-pulse', key: 'alias' } as const, null as AliasHeld | null)
/** The alias view's last failed read (its tag, the problem, and since when a read of it has been outstanding when the
 * failure was that one, readSessionView's pendingSince): shown under its tasks as the main view shows treeErr. */
export type AliasErr = { tag: string; problem: string; pendingSince: number | null }
const aliasErr = atom({ plugin: 'cortados-pulse', key: 'aliasErr' } as const, null as AliasErr | null)

/** GET /v1/cortados/local: each local node's switch, admission, backend, loaded models and capacity, asked through its
 * gate (the provider bounds each gate call at 6 s). The owner token opens it. */
export const LOCAL_SWITCH_PATH = '/v1/cortados/local'
/** Read every third live tick: each read asks every node's gate (pmset, the backend's model list). */
export const NODES_POLL_MS = 30_000
/** Every provider read is given up after this long, so one hung request can never stop the polling (the mod host's
 * fetch has no timeout of its own; the poll runs one at a time and would wait on it forever). The read is aborted then,
 * and stays outstanding (fetchBounded) until the host's fetch settles. */
export const FETCH_TIMEOUT_MS = 15_000
/** A read still outstanding this long after it started (a host that ignored the abort, its fetch never settling) makes
 * the alias view stale: its task list is dropped (no elapsed time keeps ticking) and "stale since …" is shown. */
export const STALE_AFTER_MS = 2 * FETCH_TIMEOUT_MS
/** The cortados-route MCP server's start_task, under any MCP prefix (a user, project or plugin server). */
export const START_TASK_TOOL = /^mcp__.*cortados-route__start_task$/
export const STARTED_MAX = 8
const ORIGIN_TAG = /^[0-9a-f]{16,64}$/

/** The machine's shared Office salt (cortados_router/subagent_mcp.py OFFICE_SALT_DEFAULT). */
export const OFFICE_SALT_FILE = '~/.config/cortados/office-salt'
/** This session's finished tasks shown under its running ones: the last few, the dashboard has the rest. */
export const MINE_RECENT_MAX = 3

/** sha256("<salt>:<session id>") as hex, cut to 16: cortados-routed-subagents' sessionTag, subagent_mcp's origin_tag. */
export async function sessionTag(salt: string, sessionId: string): Promise<string> {
  const buf = await (globalThis as any).crypto.subtle.digest('SHA-256', new TextEncoder().encode(`${salt}:${sessionId}`))
  return Array.from(new Uint8Array(buf), (b: number) => b.toString(16).padStart(2, '0')).join('').slice(0, 16)
}

/** The machine's Office salt (32 lowercase hex), or null when the file is missing or holds anything else. */
export async function readSalt($: Pick<EngineInterface, 'env' | 'fs'>): Promise<string | null> {
  try {
    const path = expandHome(String((await $.env.get('CORTADOS_OFFICE_SALT_FILE')) || OFFICE_SALT_FILE).trim(), await $.env.get('HOME'))
    const v = String(await $.fs.read(path)).trim()
    return /^[0-9a-f]{32}$/.test(v) ? v : null
  } catch { return null }
}

/** The machine tag's domain label (subagent_mcp.MACHINE_TAG_LABEL): a session id never holds ":", so no session tag
 * is ever computed from the same input. */
export const MACHINE_TAG_LABEL = 'cortados:machine'

/** sha256("<salt>:cortados:machine") as hex, cut to 16: subagent_mcp.machine_tag, the origin_machine of every task this
 * machine's cortados-route servers start. */
export async function machineTag(salt: string): Promise<string> {
  return sessionTag(salt, MACHINE_TAG_LABEL)
}

/** This session's tag, or null with why (said in the pane). Never throws; never keeps the raw session id. */
export async function identify($: Pick<EngineInterface, 'env' | 'fs' | 'session'>): Promise<Mine> {
  const salt = await readSalt($)
  if (!salt) return { tag: null, why: 'session not identified: no ' + OFFICE_SALT_FILE + ' yet (the cortados-route MCP server makes it)' }
  try {
    const id = await $.session.id()
    if (typeof id !== 'string' || !id) throw new Error()
    return { tag: await sessionTag(salt, id), why: null }
  } catch {
    return { tag: null, why: 'session not identified: its id could not be read' }
  }
}

/** This machine's tag (machineTag of its Office salt), held once computed; null without a salt (then the untagged
 * group is simply not asked for: tasks of a server without the salt carry no machine tag either). */
export async function machineKey($: Pick<EngineInterface, 'env' | 'fs' | 'state'>): Promise<string | null> {
  const known = await read($, machine)
  if (known) return known
  const salt = await readSalt($)
  if (!salt) return null
  const tag = await machineTag(salt)
  await update($, machine, () => tag)
  return tag
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

/** pendingSince: the start (clock ms) of an earlier read of the same URL that is still outstanding, when that is why
 * this one was refused (fetchBounded). */
type Fetched = { ok: true; data: object; url: string } | { ok: false; problem: string; status?: number; pendingSince?: number }

/** GET <provider><path> with the Bearer token: the JSON object (scrubbed of the token, strings and keys alike) or the
 * one-line problem. Shared by both polls so the token rules are written once. */
export async function fetchView($: Pick<EngineInterface, 'env' | 'fs' | 'http' | 'clock'>, cfg: Cfg, path: string): Promise<Fetched> {
  let token = ''
  try {
    const url = await resolveProviderUrl($, cfg.providerUrl)
    token = await resolveToken($, cfg)
    const r = await fetchBounded($, url + path, { headers: { authorization: 'Bearer ' + token } })
    if (r.status === 401 || r.status === 403) {
      return { ok: false, status: r.status, problem: 'provider rejected the token (HTTP ' + r.status + '): check ' + (cfg.providerToken ? 'providerToken' : cfg.providerTokenFile) }
    }
    if (!r.ok) return { ok: false, status: r.status, problem: 'provider returned HTTP ' + r.status }
    const data = scrubResponse(JSON.parse(r.text), token)
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error(path.split('?')[0] + ' did not return a JSON object')
    return { ok: true, data, url }
  } catch (e) {
    const why = (e as Error)?.message ?? String(e)
    const since = (e as any)?.pendingSince
    return { ok: false, problem: redactQuery(scrub('could not read the provider (' + why + ')', token)),
      ...(typeof since === 'number' ? { pendingSince: since } : {}) }
  }
}

/** `text` with every URL query value replaced by <redacted> (names kept). A host's fetch error can echo the URL it was
 * given, and the session view's carries &machine=<tag>, a capability (pulse review round 2): no pane line ever shows a
 * query value, the session tag's included. */
export function redactQuery(text: string): string {
  // The whole value, up to the next `&`, `#`, whitespace or the end (the provider's redact_query, pulse review round 3):
  // punctuation such as `)`, `]`, `,` or a quote can be part of the value, so none of it ends the match.
  return text.replace(/([?&][^=&\s#]*=)[^&\s#]*/g, '$1<redacted>')
}

/** Provider reads whose underlying $.http.fetch has not settled yet, by URL. A read counts here until the host's fetch
 * itself resolves or rejects, not until the poll gave up on it: a timed-out read is aborted, and only a host that
 * honours the abort ends it. A host that does not (this build's HttpInit declares no `signal`) keeps it outstanding,
 * and no second read of the same URL starts until it settles, so hung reads never pile up (one per view at most). */
const outstanding = new Map<string, { since: number | null }>()
/** The host refused a fetch carrying `signal` (an init it cannot take or clone): later reads go without it. */
let signalRefused = false

/** How many provider reads are still outstanding (their host fetch has not settled). */
export function outstandingReads(): number {
  return outstanding.size
}

/** $.http.fetch, given up after FETCH_TIMEOUT_MS (a rejection, said as the poll's problem) when the host has a clock
 * timer; the timer is cancelled once the answer comes. A read that times out is aborted through its AbortController
 * (passed as init.signal). Refused at once while an earlier read of the same URL is still outstanding; the refusal
 * carries `pendingSince`, that read's start (clock ms), so a view can tell a read that never ends from a slow one. */
export async function fetchBounded($: Pick<EngineInterface, 'http' | 'clock'>, url: string, init: any): Promise<any> {
  const prev = outstanding.get(url)
  if (prev) throw Object.assign(new Error('the previous read of this view has not ended yet'),
    prev.since !== null ? { pendingSince: prev.since } : {})
  const stop = typeof AbortController === 'function' ? new AbortController() : null
  const send = (withSignal: boolean): Promise<any> => {
    try {
      return Promise.resolve($.http.fetch(url, withSignal && stop ? { ...init, signal: stop.signal } : init))
    } catch (e) {
      return Promise.reject(e)
    }
  }
  const req = send(!signalRefused && stop !== null).catch(e => {
    // a host that cannot take the signal says so at once: send it again without, and never with one again
    const why = String((e as Error)?.name ?? '') + ' ' + String((e as Error)?.message ?? e)
    if (!signalRefused && stop && !stop.signal.aborted && /signal|clone/i.test(why)) {
      signalRefused = true
      return send(false)
    }
    throw e
  })
  const mark: { since: number | null } = { since: null }
  outstanding.set(url, mark)
  // the start, for pendingSince (set before any timeout can fire; a clock that fails leaves it unknown, never stale)
  try { Promise.resolve($.clock.now()).then(t => { if (typeof t === 'number') mark.since = t }, () => {}) } catch { /* no clock */ }
  const done = () => { if (outstanding.get(url) === mark) outstanding.delete(url) }
  req.then(done, done)
  let timer: Timer | null = null
  const late = new Promise<never>((_, reject) => {
    try {
      timer = $.clock.after(FETCH_TIMEOUT_MS, () => {
        try { stop?.abort() } catch { /* nothing to abort */ }
        reject(new Error('no answer within ' + FETCH_TIMEOUT_MS / 1000 + ' s'))
      })
    } catch { /* a host without timers: the fetch alone decides */ }
  })
  try {
    return await Promise.race([req, late])
  } finally {
    try { (timer as Timer | null)?.cancel() } catch { /* already fired */ }
  }
}

/** One GET /v1/cortados/local: the nodes, or the problem in a line (a scoped token is told it needs the owner's). */
export async function pollNodes($: Pick<EngineInterface, 'env' | 'fs' | 'http' | 'state' | 'clock'>, cfg: Cfg): Promise<void> {
  const f = await fetchView($, cfg, LOCAL_SWITCH_PATH)
  if (!f.ok) {
    await update($, nodesErr, () => f.status === 404 ? 'this provider has no local switch view (update it)'
      : f.status === 401 || f.status === 403 ? 'local node status needs the owner token (a scoped pane token cannot read it)'
      : 'local nodes: ' + f.problem)
    return
  }
  const at = await $.clock.now()
  await update($, nodes, () => ({ data: f.data as PulseLocalView, at }))
  await update($, nodesErr, () => null)
}

/** A start_task result as the model read it: the task id and the origin tag it was recorded under, or null. */
export function startedOf(text: unknown): { tag: string; task_id: string } | null {
  if (typeof text !== 'string' || text.length > 200_000) return null
  try {
    const o = JSON.parse(text)
    const tag = o?.origin_session, id = o?.task_id
    return typeof tag === 'string' && ORIGIN_TAG.test(tag) && typeof id === 'string' && TASK_ID.test(id) ? { tag, task_id: id } : null
  } catch { return null }
}

/** The newest origin a start_task of THIS session named that is not its own tag: the cortados-route server records
 * the session id it was started with, so after a /clear (or wherever the server's id differs) its tasks are under that
 * tag, not the pane's. null when every task went under the pane's own tag. */
export function aliasTag(list: Started[], own: string | null | undefined): string | null {
  return list.find(x => x.tag !== own)?.tag ?? null
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
type LiveDollar = Pick<EngineInterface, 'env' | 'fs' | 'process' | 'http' | 'state' | 'clock'>

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
  // every poll: /clear and resume move the pane to another session id
  const m = me ? await identify($ as unknown as Pick<EngineInterface, 'env' | 'fs' | 'session'>) : null   // the hooks' $ has session
  if (m) await update($, mine, () => m)
  // this machine's own log first: it draws even when the provider cannot be reached (#118)
  if (m?.tag) await readLocal($, m.tag)
  // #127: tasks this session started under another tag (its cortados-route server's): that tag's view too
  if (m?.tag) await fetchAlias($, cfg, m.tag)
  // the per-session view answered (or failed in a way the fleet view would too): nothing fleet-wide is read
  if (m?.tag && await fetchTree($, cfg, m.tag)) return
  try {
    await fetchLive($, cfg)
  } finally {
    if (m) await fetchMine($, cfg, m.tag)
  }
}

/** One read of GET /v1/cortados/live/session?session=<tag>, shared by the main view (fetchTree) and the alias view
 * (fetchAlias) so the two cannot diverge: `held` (that tag's subtree), `absent` (a provider without the view: 404, or an
 * answer that is not that tag's subtree) or `failed` (401/403/5xx/network/timeout, said in a line; `pendingSince` when
 * an earlier read of the view is still outstanding). */
export type SessionRead = { kind: 'held'; data: PulseSessionView; url: string } | { kind: 'absent' }
  | { kind: 'failed'; problem: string; pendingSince: number | null }

export async function readSessionView($: LiveDollar, cfg: Cfg, tag: string, machineTagged: string | null = null): Promise<SessionRead> {
  // &machine= only with a tag of the expected form (an older provider ignores the parameter: no untagged group)
  const m = machineTagged && ORIGIN_TAG.test(machineTagged) ? '&machine=' + machineTagged : ''
  const f = await fetchView($, cfg, SESSION_PATH + '?session=' + tag + m)
  if (f.ok) {
    const data = f.data as PulseSessionView
    return data.session_filter === tag && data.orchestrators && typeof data.orchestrators === 'object'
      ? { kind: 'held', data, url: f.url } : { kind: 'absent' }
  }
  return f.status === 404 ? { kind: 'absent' } : { kind: 'failed', problem: f.problem, pendingSince: f.pendingSince ?? null }
}

/** GET /v1/cortados/live/session?session=<tag>. True when it settled the poll: the subtree is held, or the provider
 * failed (401/403/5xx/network: said, the held subtree stays with its age). False only for a provider without the view
 * (404, or an answer that is not this session's subtree): the pane then falls back to the fleet view. */
export async function fetchTree($: LiveDollar, cfg: Cfg, tag: string): Promise<boolean> {
  const r = await readSessionView($, cfg, tag, await machineKey($))
  if (r.kind === 'held') {
    const at = await $.clock.now()
    await update($, tree, () => ({ data: r.data, at, tag, base: r.url }))
    await update($, treeErr, () => null)
    return true
  }
  if (r.kind === 'failed') {
    await update($, treeErr, () => 'live: ' + r.problem)
    return true
  }
  await update($, tree, () => null)
  await update($, treeErr, () => null)
  return false
}

/** The session view of aliasTag(started, own) (see there), held for that tag; dropped when there is none or the
 * provider has no such view. A failed read keeps what was held for that tag and says the problem (aliasErr), which
 * aliasLines draws with the held data's age, or as stale with no tasks once a read has hung past STALE_AFTER_MS. */
export async function fetchAlias($: LiveDollar, cfg: Cfg, own: string): Promise<void> {
  const tag = aliasTag(await read($, started), own)
  if (!tag) {
    if (await read($, alias)) await update($, alias, () => null)
    if (await read($, aliasErr)) await update($, aliasErr, () => null)
    return
  }
  const r = await readSessionView($, cfg, tag)
  if (r.kind === 'failed') {
    if ((await read($, alias))?.tag !== tag) await update($, alias, () => null)   // never another tag's held view
    await update($, aliasErr, () => ({ tag, problem: 'live: ' + r.problem, pendingSince: r.pendingSince }))
    return
  }
  const at = await $.clock.now()
  await update($, alias, () => r.kind === 'held' ? { data: r.data, at, tag } : null)
  await update($, aliasErr, () => null)
}

/** This session's own view (?session=). Kept only when the provider says it filtered for this tag (session_filter):
 * an older provider ignores the query and answers the fleet view, which sessionLines then filters itself and says so.
 * A failed fetch drops the held view rather than showing an old one; the fleet poll reports the provider's problems. */
async function fetchMine($: LiveDollar, cfg: Cfg, tag: string | null): Promise<void> {
  if (!tag) return          // a held view of another tag is never drawn (the render matches it to `mine`)
  const f = await fetchView($, cfg, LIVE_PATH + '?session=' + tag)
  const data = f.ok ? f.data as PulseLive : null
  const at = await $.clock.now()
  await update($, mineLive, () => data && data.session_filter === tag ? { data, at, tag } : null)
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
  // #127: this is the scarcity cache's `local` lane, read from the resource-pool sample's configured deployments
  // (local_resources.sample), not the local gate that serves the local lane: labelled as such; the gate's own state is
  // the Local nodes section
  if (lane === 'local') return [`local (resource-pool deployments): ${health.ok === false ? 'none verified' : health.ok === true ? 'eligible' : 'unknown'}` +
    (health.why && health.ok !== false ? ` · ${health.why}` : '') + (health.ok === false ? ' · see Local nodes for the gate' : '')]
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

/** The Devices section: the resource-pool probe (`/resources` devices, local_resources.sample over the fleet config's
 * SSH targets). It is NOT the local lane's state: a node with no SSH target in that config is "not probed", never
 * "free · fits 0", and the pool line is drawn only over nodes the probe actually reached (#127). */
export function deviceLines(dev: PulseDevices): string[] {
  const out: string[] = []
  if (dev.status !== 'ok') {
    out.push(dev.status === 'pending' ? 'sample pending' : 'unavailable' + (dev.error ? ': ' + dev.error : ''))
    return out
  }
  const list = dev.nodes ?? []
  for (const n of list) {
    if (n.status === 'unconfigured') { out.push(`${n.id}: not probed (no SSH target in the resource-pool config)`); continue }
    if (n.status !== 'online') { out.push(`${n.id}: ${n.status ?? 'unknown'}`); continue }
    out.push(`${n.id} online · ${gib(n.gpu_headroom_estimate_bytes)} free` +
      (typeof n.gpu_utilization_pct === 'number' ? ` · gpu ${Math.round(n.gpu_utilization_pct)}%` : '') +
      ` · fits ${(n.fits ?? []).length}`)
  }
  if (typeof dev.pool_headroom_estimate_bytes === 'number' && list.some(n => n.status === 'online')) {
    out.push(`pool ${gib(dev.pool_headroom_estimate_bytes)} · fits ${(dev.pool_fits ?? []).length}`)
  }
  if (!list.length) out.push('no nodes online')
  return out
}

/** One local node as its gate reports it (GET /v1/cortados/local): switch, admission and why, power, backend and its
 * reachability, loaded models; then the gate's admission: in flight / limit, owner, memory pressure, free memory. */
export function nodeLines(n: PulseLocalNode): string[] {
  const name = n.node ?? '?'
  if (!n.reachable) return [`${name}: unreachable` + (n.error ? ' · ' + n.error : '')]
  const loaded = n.models_loaded ?? null
  const head = join([
    'switch ' + (n.switch ?? 'unknown'),
    n.allowed === true ? 'allowed' : n.allowed === false ? 'NOT allowed' : 'allowed unknown',
    n.power && n.power !== 'unknown' ? (n.power === 'ac' ? 'AC' : n.power) : null,
    n.training ? 'training' : null,
  ])
  const out = [`${name}: ${head}`]
  if (n.reason) out.push('  ' + n.reason)
  out.push('  ' + join([(n.backend ?? 'unknown') + (n.ollama && n.ollama !== 'up' ? ' ' + n.ollama : n.ollama === 'up' ? ' up' : ''),
    loaded === null ? 'loaded unknown' : loaded.length ? 'loaded ' + loaded.join(', ') : 'no model loaded']))
  const c = n.capacity
  if (c) {
    const num = (v: unknown) => typeof v === 'number' && Number.isFinite(v)
    out.push('  ' + join([
      num(c.limit) ? `${num(c.inflight) ? c.inflight : '?'}/${c.limit} in flight` : 'no admission limit',
      c.owner ? 'owner ' + c.owner : null,
      c.pressure ? 'pressure ' + c.pressure : null,
      num(c.free_bytes) ? gib(c.free_bytes) + ' free' : null,
    ]))
    if (c.reason) out.push('  ' + c.reason)
  }
  return out
}

/** The Local nodes section: each node, or the view's note (none configured) or problem. */
export function localNodeLines(v: PulseLocalView | null, problem: string | null): string[] {
  const out: string[] = []
  if (problem) out.push(problem)
  if (!v) return problem ? out : ['loading the local nodes…']
  const list = v.nodes ?? []
  if (!list.length) out.push(v.note || 'no local node is configured on the provider')
  for (const n of list) out.push(...nodeLines(n))
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
  // a provider from before task origins sends no origins at all: nothing can be attributed, which is not "none started"
  if (t.origins === undefined) return ['this provider records no task origins yet (update it to see this session\'s tasks)']
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
  // the fleet view filtered here: its lists were capped before, so on a busy fleet some of this session's tasks are cut
  if (live.session_filter !== me.tag && t.truncated) out.push('  (fleet lists are capped: some of this session\'s tasks may be missing; update the provider)')
  return out
}

// ---- this session's tree (flightdeck-style; pure, tested) -----------------------------------------------------------

/** A tree line: its text, and whether it needs attention (failed, unknown, or a task with a failed sub-session:
 * drawn bold, the glyph or the words say which). */
export type TreeLine = { text: string; attn?: boolean }

const GLYPH: Record<string, string> = { running: '●', waiting: '◷', done: '✓', failed: '✕', unknown: '?' }
export const glyph = (status?: string | null, state?: string | null): string =>
  state === 'cancelled' ? '⊘' : GLYPH[status ?? ''] ?? '•'

/** Fresh tokens (input + output + cache writes) in a few characters: 950 tok, 14k tok, 1.2M tok. */
export function tokShort(t?: PulseTokens | null): string | null {
  const n = t?.fresh
  if (typeof n !== 'number' || !Number.isFinite(n) || n <= 0) return null
  return (n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n)) + ' tok'
}

/** Elapsed as it should read now: a node still running or waiting keeps counting from the snapshot (`since` s ago). */
const clockOf = (x: { status?: string; ended_at?: number | null; elapsed_s?: number | null }, since: number): string | null =>
  typeof x.elapsed_s !== 'number' ? null
    : (x.status === 'running' || x.status === 'waiting') && x.ended_at == null ? dur(x.elapsed_s + since)
    : 'took ' + dur(x.elapsed_s)

function subLine(x: PulseSubSession, last: boolean, indent: string, since: number): TreeLine {
  const who = x.title || (x.role === 'main' ? 'main loop' : 'subagent')
  return { text: indent + (last ? '└ ' : '├ ') + glyph(x.status) + ' ' + join([who, x.model, x.tool ? 'tool ' + x.tool : x.phase,
    tokShort(x.tokens), clockOf(x, since)]), attn: x.status === 'failed' }
}

/** One task and what hangs under it. A live task (running, waiting) opens: its model line and its sub-sessions; a
 * finished one is one line, as flightdeck folds a finished card. Nested tasks follow, indented under their parent. */
function taskLines(t: PulseTreeTask, depth: number, since: number): TreeLine[] {
  const ind = '  '.repeat(depth)
  const live = t.status === 'running' || t.status === 'waiting'
  const bad = t.status === 'failed' || t.status === 'unknown'
  // a task that is not itself failed but has failed sub-sessions (counted in the group's attention): said on its own
  // line, which a finished task folds to, and also when the failed one is past the sub-session cap
  const subFailed = bad ? 0 : Math.max(0, t.sub_sessions_failed ?? 0)
  const attn = bad || subFailed > 0
  const name = t.title || ('task ' + (t.task_id ?? '?'))
  const out: TreeLine[] = [{ attn, text: ind + (depth ? '↳ ' : '') + glyph(t.status, t.state) + ' ' + join([name,
    live ? null : t.state === 'elsewhere' ? 'on ' + t.host + ', state there' : t.state,
    subFailed ? subFailed + ' sub-session' + (subFailed === 1 ? '' : 's') + ' failed' : null,
    live ? clockOf(t, since) : clockOf(t, 0)]) }]
  const pad = ind + (depth ? '  ' : '') + '  '
  if (live) {
    const meta = join([t.model, t.host ? 'on ' + t.host : null, t.tool ? 'tool ' + t.tool : t.phase, tokShort(t.tokens)])
    if (meta) out.push({ text: pad + meta })
    const subs = t.sub_sessions ?? []
    const shown = subs.slice(0, SUBS_MAX)
    const more = subs.length - shown.length + (t.sub_sessions_hidden ?? 0)
    shown.forEach((x, i) => out.push(subLine(x, i === shown.length - 1 && more <= 0, pad, since)))
    if (more > 0) out.push({ text: pad + '└ +' + more + ' more sub-sessions' })
  }
  for (const k of t.tasks ?? []) out.push(...taskLines(k, depth + 1, since))
  return out
}

/** This session's tree from its session view: a count line, then its live tasks (opened), the last few finished
 * (folded), and the subagents it routed directly. Only `items` whose origin is the session's own tag are drawn. */
export function treeLines(v: PulseSessionView, tag: string, since: number): TreeLine[] {
  const o = v.orchestrators ?? {}
  if (o.status === 'error') return [{ text: 'tasks unavailable: ' + errs(o), attn: true }]
  const g: PulseOrchestrator | undefined = (o.items ?? []).find(x => x.origin === tag)
  const out: TreeLine[] = []
  if (o.status === 'partial') out.push({ text: 'partial: ' + errs(o), attn: true })
  // the provider bounds its inputs by a hard ceiling far above any real fleet; when it cuts one it says so
  if (o.inputs_truncated?.length) out.push({ text: 'inputs capped (' + o.inputs_truncated.join(', ') + '): some items may be missing', attn: true })
  if (!g) return [...out, { text: 'no task started from this session in the last 24 h' }]
  const c = g.counts ?? {}
  const n = (k: string) => c[k] ?? 0
  // counts_at_least (mergeLocal): a local-only task may be one the provider counted but did not list: lower bounds
  const ge = g.counts_at_least ? '≥' : ''
  const cnt = (k: string) => n(k) ? ge + n(k) + ' ' + k : null
  out.push({ text: join([cnt('running'), cnt('waiting'), cnt('done'), cnt('failed'), cnt('unknown')]) ||
    'no task started from this session in the last 24 h' })
  if (g.attention) out.push({ text: '! ' + ge + g.attention + (g.attention === 1 && !ge ? ' needs' : ' need') + ' attention', attn: true })
  const tasks = g.tasks ?? []
  const live = tasks.filter(t => t.status === 'running' || t.status === 'waiting')
  const rest = tasks.filter(t => !(t.status === 'running' || t.status === 'waiting'))
  for (const t of live.slice(0, RUNNING_MAX)) out.push(...taskLines(t, 0, since))
  if (live.length > RUNNING_MAX) out.push({ text: `  +${live.length - RUNNING_MAX} more running (dashboard)` })
  for (const t of rest.slice(0, MINE_RECENT_MAX)) out.push(...taskLines(t, 0, since))
  const hidden = Math.max(0, rest.length - MINE_RECENT_MAX) + (g.tasks_hidden ?? 0)
  if (hidden) out.push({ text: g.counts_at_least ? `  up to ${hidden} more finished (dashboard)` : `  +${hidden} more finished (dashboard)` })
  const routed = g.routed ?? []
  if (routed.length) {
    out.push({ text: 'routed directly' })
    const shown = routed.slice(0, ROUTED_SHOWN)
    const more = routed.length - shown.length + (g.routed_hidden ?? 0)
    shown.forEach((x, i) => out.push(subLine(x, i === shown.length - 1 && more <= 0, '', since)))
    if (more > 0) out.push({ text: '└ +' + more + ' more' })
  }
  return out
}

// ---- this session's work split (#127; pure, tested) ---------------------------------------------------------------

/** The window the compact line speaks for; the breakdown adds the other. */
export const SPLIT_WINDOW = '24h'
/** Token amounts as the work split line writes them: 1.4M, 0.8M, 25k, 950. */
export function tokAmt(n?: number | null): string {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '—'
  if (n >= 1e5) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return Math.round(n / 1e3) + 'k'
  return String(Math.round(n))
}
const VIA_WORDS: Record<string, string> = { child_task: 'child tasks', subagent: 'routed subagents', route_task: 'route_task' }
const kinds = (t: PulseSplitTokens) => join(['in ' + tokAmt(t.input), 'out ' + tokAmt(t.output),
  'cache write ' + tokAmt(t.cache_write), 'cache read ' + tokAmt(t.cache_read)])

/** A work split line: its text, and whether it is a note (drawn dim). */
export type SplitLine = { text: string; dim?: boolean }

/** This session's work split (GET /v1/cortados/live/session's `work_split`): one compact line for SPLIT_WINDOW, e.g.
 * `work split 24h: routed 66% fresh (1.4M tok: codex/gpt-6.1-sol 0.8M, claude#b/claude-opus-5-5 0.5M) · native 0.7M
 * (claude-opus-5-5) · 15% counting cache reads`; with `open`, the breakdown (by route, by how it was handed over,
 * native by model, the four token kinds, the other window); then the provider's notes on what is partial. Fresh =
 * input + output + cache writes. `label` prefixes the line (the alias view). */
export function workSplitLines(ws: PulseSessionSplit | null | undefined, open: boolean, label = 'work split'): SplitLine[] {
  if (!ws) return [{ text: label + ': this provider reports no per-session split yet (update it)', dim: true }]
  const notes = (ws.notes ?? []).map(n => ({ text: '  ' + n, dim: true }))
  const row = ws.row
  const w = row ? row[SPLIT_WINDOW] as PulseSplitWindow | undefined : undefined
  if (!row || !w) return [{ text: label + ': nothing recorded for this session yet' }, ...notes]
  const r = w.routed, n = w.native
  const routes = (r?.by_route ?? []).slice(0, 2).map(x => `${x.route} ${tokAmt(x.fresh)}`)
  const routed = r ? 'routed ' + (typeof w.routed_pct === 'number' ? pct(w.routed_pct) + ' fresh ' : '') +
    `(${tokAmt(r.fresh)} tok` + (routes.length ? ': ' + routes.join(', ') + ((r.by_route ?? []).length > 2 ? ', …' : '') : '') + ')'
    : 'routed none'
  const models = (n?.models ?? []).filter(m => m.model !== 'other').slice(0, 2).map(m => m.model)
  const native = n ? `native ${tokAmt(n.fresh)}` + (models.length ? ` (${models.join(', ')})` : '') : 'native not reported'
  const out: SplitLine[] = [{ text: `${label} ${SPLIT_WINDOW}: ` + join([routed, native,
    typeof w.routed_pct_all === 'number' ? pct(w.routed_pct_all) + ' counting cache reads' : null]) }]
  if (open) {
    if (r) {
      out.push({ text: '  routed by route (fresh):' })
      for (const x of r.by_route ?? []) out.push({ text: `    ${x.route} ${tokAmt(x.fresh)} · ${x.conversations ?? 0} conv` })
      const via = Object.entries(r.by_via ?? {}).map(([k, v]) => `${VIA_WORDS[k] ?? k} ${tokAmt(v.fresh)}`)
      if (via.length) out.push({ text: '  routed by how: ' + via.join(' · ') })
      out.push({ text: '  routed tokens: ' + kinds(r) })
    }
    if (n) {
      out.push({ text: '  native by model (fresh):' })
      for (const m of n.models ?? []) out.push({ text: `    ${m.model} ${tokAmt(m.fresh)} · ${kinds(m)}` })
      out.push({ text: '  native tokens: ' + kinds(n) })
    }
    for (const k of Object.keys(ws.windows ?? {})) {
      if (k === SPLIT_WINDOW) continue
      const o = row[k] as PulseSplitWindow | undefined
      if (o) out.push({ text: `  ${k}: ` + join([typeof o.routed_pct === 'number' ? 'routed ' + pct(o.routed_pct) + ' fresh' : null,
        typeof o.routed_pct_all === 'number' ? pct(o.routed_pct_all) + ' counting cache reads' : null,
        'routed ' + tokAmt(o.routed_fresh), o.native ? 'native ' + tokAmt(o.native.fresh) : 'native not reported']) })
    }
    const on = row.matched_on ?? []
    out.push({ text: '  ' + (row.match === 'both' ? 'joined on ' + (on.includes('session_alt') ? (on.includes('session') ? 'both salts\' tags' : 'the older-salt tag') : 'the session tag')
      : row.match === 'routed only' ? 'no native report for this tag' : row.match === 'native only' ? 'no routed work under this tag' : String(row.match ?? '')) +
      (row.host ? ' · native from ' + row.host : ''), dim: true })
    out.push({ text: '  fresh = input + output + cache writes; cache reads are left out of the headline %', dim: true })
  }
  return [...out, ...notes]
}

/** Shown under This session: /clear gives the pane a new session id, while a task keeps the session it was started
 * for (and the cortados-route server keeps the id it started with: README "Which main session started which task"). */
export const CLEAR_NOTE = 'after a /clear, earlier tasks stay under the previous session (dashboard)'
/** Above the tree of a tag this session's own start_task named that is not the pane's (aliasTag). */
export const ALIAS_NOTE = (tag: string) => 'tasks this session started under its cortados-route server\'s tag ' + tag.slice(0, 8) +
  ' (its session id predates a /clear or resume):'

/** A pane line of the alias view: its text, dim (a note) or attn (bold). */
export type AliasLine = { text: string; dim?: boolean; attn?: boolean }

/** The alias view under This session (pure): ALIAS_NOTE, that tag's tree and work split, and its last problem with the
 * held data's age, as the main view says treeErr. When a read of the view has been outstanding past STALE_AFTER_MS (a
 * host that ignored the abort), the view is stale: its tasks and split are dropped, so no elapsed time keeps ticking,
 * and "stale since …" is said instead. Nothing for no alias, or an alias equal to the pane's own tag. */
export function aliasLines(al: AliasHeld | null, ae: AliasErr | null, own: string, nowMs: number): AliasLine[] {
  const tag = ae?.tag ?? al?.tag
  if (!tag || tag === own) return []
  const h = al && al.tag === tag ? al : null
  const e = ae && ae.tag === tag ? ae : null
  const out: AliasLine[] = [{ text: ALIAS_NOTE(tag), dim: true }]
  const hung = e && typeof e.pendingSince === 'number' ? nowMs - e.pendingSince : null
  if (e && hung !== null && hung > STALE_AFTER_MS) {
    const since = h ? h.at : (e.pendingSince as number)
    out.push({ text: 'stale since ' + age((nowMs - since) / 1000) + ' ago: a read of this view has not ended for ' +
      age(hung / 1000) + ' (past its ' + FETCH_TIMEOUT_MS / 1000 + ' s timeout); its tasks are hidden until it does', dim: true })
    return out
  }
  if (h) {
    out.push(...treeLines(h.data, tag, (nowMs - h.at) / 1000).map(l => ({ text: l.text, ...(l.attn ? { attn: true } : {}) })))
    if (h.data.work_split) out.push(...workSplitLines(h.data.work_split, false, 'work split (server tag)'))
  }
  if (e) out.push({ text: e.problem + (h ? ' · showing ' + age((nowMs - h.at) / 1000) + ' old data' : ''), dim: true })
  return out
}

// ---- this machine's own task log (#118; pure, tested) ----------------------------------------------------------------
// A task started with host "here" runs on THIS machine and logs only here ($CORTADOS_HOME/child-tasks.jsonl). The
// provider learns of it from this host's heartbeat, which may not exist yet (no sender installed) or may not reach it
// (provider unreachable). So the pane also reads the local log for its own session's tasks and merges them, by task id,
// into whatever the provider's view holds: the provider's node wins (it has the sub-sessions and tokens), except that
// a task the local log saw finish is shown finished at once.

/** The log's name in the state dir (child_tasks.EVENT_LOG, office.CHILD_EVENT_LOG). */
export const LOCAL_LOG = 'child-tasks.jsonl'
/** Read every third live tick: the log only grows (never rotated), and a task's start or end may wait 30 s here. */
export const LOCAL_POLL_MS = 30_000
/** The tail parsed, as the provider bounds its own read of the same log (usage.CHILD_TAIL_BYTES / CHILD_TAIL). */
export const LOCAL_TAIL_BYTES = 2 * 2 ** 20
export const LOCAL_TAIL_LINES = 5000
const DAY_S = 86400
const STALE_RUNNING_S = 10 * 3600          // live.STALE_RUNNING_S: no end event after 10 h is unknown, not running
const TASK_ID = /^t\d{14}-[0-9a-f]{8}$/   // child_tasks.TASK_ID_RE
const HOST = /^[a-z][a-z0-9-]{0,31}$/
const TITLE = /^[\x20-\x7e]{1,80}$/
const MODEL = /^[A-Za-z0-9._/\[\]:-]{1,64}$/
const LANE = /^[A-Za-z0-9._#:/-]{1,64}$/
const FINAL: Record<string, string> = { succeeded: 'done', failed: 'failed', cancelled: 'done' }
/** live.STALE_HB_PHASE: the provider's word for a task whose host's heartbeat went stale (tests check they agree). */
export const STALE_HB_PHASE = "its host's heartbeat is stale; state as last reported"
const pick = (v: unknown, re: RegExp): string | null => typeof v === 'string' && re.test(v) ? v : null

/** A task this machine's log knows: a tree node without sub-sessions, its times kept so the merge can clock it. */
export type LocalTask = PulseTreeTask & { parent_task_id?: string | null }

/** This session's own tasks in the log text (`tag`'s origin, or a parent's along parent_task_id within the log), the
 * last 24 h, newest first. Only the metadata fields are read, each by the provider's own rule; the cwd, the prompt
 * and anything else in a line are never kept. Pure: `nowS` is the clock in seconds. */
export function parseLocalTasks(text: string, tag: string, nowS: number, cut = false): LocalTask[] {
  let tail = text.length > LOCAL_TAIL_BYTES ? text.slice(text.length - LOCAL_TAIL_BYTES) : text
  // drop the cut first line: cut here, or by the reader (`cut`: the text is already the file's tail, readLocalLog)
  if (cut || tail.length < text.length) tail = tail.slice(tail.indexOf('\n') + 1)
  const lines = tail.split('\n')
  type Rec = { id: string; start: number | null; end: number | null; final: string | null; title: string | null;
    host: string | null; model: string | null; lane: string | null; origin: string | null; parent: string | null; dur: number | null }
  const by = new Map<string, Rec>()
  for (const ln of lines.slice(-LOCAL_TAIL_LINES)) {
    if (!ln.trim()) continue
    let e: any
    try { e = JSON.parse(ln) } catch { continue }
    if (!e || typeof e !== 'object') continue
    const ts = e.ts, id = pick(e.task_id, TASK_ID), ev = e.event
    if (typeof ts !== 'number' || !Number.isFinite(ts) || ts < nowS - DAY_S || ts > nowS + 120 || !id) continue
    if (ev !== 'started' && ev !== 'finished' && ev !== 'cancelled') continue
    const r = by.get(id) ?? { id, start: null, end: null, final: null, title: null, host: null, model: null, lane: null,
      origin: null, parent: null, dur: null }
    by.set(id, r)
    if (ev === 'started') r.start = ts
    else {
      r.end = ts
      r.final = ev === 'cancelled' ? 'cancelled' : (e.status in FINAL ? e.status : 'failed')
      if (typeof e.duration_s === 'number' && Number.isFinite(e.duration_s) && e.duration_s >= 0) r.dur = e.duration_s
    }
    r.title = pick(e.title, TITLE) ?? r.title
    r.host = pick(e.host, HOST) ?? r.host
    r.model = pick(e.model, MODEL) ?? r.model
    r.lane = pick(e.lane, LANE) ?? r.lane
    r.origin = typeof e.origin_session === 'string' && (e.origin_session === 'grokbot' || /^[0-9a-f]{16,64}$/.test(e.origin_session))
      ? e.origin_session : r.origin
    const p = pick(e.parent_task_id, TASK_ID)
    if (p && p !== id) r.parent = p
  }
  // whose: its own origin, else the nearest ancestor's in this log (as live._origin_of)
  const originOf = (r: Rec): [string | null, boolean] => {
    if (r.origin) return [r.origin, false]
    const seen = new Set([r.id])
    let cur: Rec | undefined = r
    while (cur?.parent && !seen.has(cur.parent) && seen.size <= 16) {
      seen.add(cur.parent)
      cur = by.get(cur.parent)
      if (cur?.origin) return [cur.origin, true]
    }
    return [null, false]
  }
  const out: LocalTask[] = []
  for (const r of by.values()) {
    const [origin, viaParent] = originOf(r)
    if (origin !== tag) continue
    let state = r.final ?? 'running', phase: string | null = null
    if (!r.final && r.start !== null && nowS - r.start > STALE_RUNNING_S) { state = 'unknown'; phase = 'no end event after 10 h' }
    const start = r.start ?? (r.end !== null && r.dur !== null ? r.end - r.dur : null)
    out.push({ task_id: r.id, title: r.title, state, status: (FINAL[state] ?? (state === 'running' ? 'running' : 'unknown')) as any,
      phase, host: r.host, model: r.model, lane: r.lane, started_at: start, ended_at: r.end, elapsed_s: null,
      origin_via: viaParent ? 'parent' : null, parent_task_id: r.parent, sub_sessions: [], tasks: [] })
  }
  return out.sort((a, b) => (b.ended_at ?? b.started_at ?? 0) - (a.ended_at ?? a.started_at ?? 0))
}

const isOpen = (s?: string | null) => s === 'running' || s === 'waiting' || s === 'unknown'
const attnOf = (t: PulseTreeTask) => t.status === 'failed' || t.status === 'unknown' ? 1 : Math.max(0, t.sub_sessions_failed ?? 0)

/** The session view with this machine's own tasks merged in, by task id (see above), counts and attention kept in
 * step. `asOfS` is the time the view was fetched (its nodes' elapsed times are as of then; the render adds the time
 * since). `view` null: a view of the local tasks alone. `local` holds only this session's tasks (parseLocalTasks).
 * Returns the view (a copy; the held one is never changed) and how many tasks came from the local log alone. */
export function mergeLocal(view: PulseSessionView | null, local: LocalTask[], tag: string,
  asOfS: number): { view: PulseSessionView; added: number } {
  const v: PulseSessionView = view ? JSON.parse(JSON.stringify(view))
    : { session_filter: tag, orchestrators: { status: 'ok', errors: [], items: [] } }
  if (!local.length) return { view: v, added: 0 }
  const o = v.orchestrators ??= { status: 'ok', errors: [], items: [] }
  const items = o.items ??= []
  let g = items.find(x => x.origin === tag)
  const make = () => ({ origin: tag, kind: 'session', status: 'not live', counts: {}, attention: 0, tasks: [],
    tasks_hidden: 0, routed: [], routed_hidden: 0 } as PulseOrchestrator)
  const nodes = new Map<string, PulseTreeTask>()
  const walk = (ts: PulseTreeTask[] = []) => ts.forEach(t => { if (t.task_id) nodes.set(t.task_id, t); walk(t.tasks) })
  if (g) walk(g.tasks)
  // The provider lists 20 top-level tasks and counts all of them (tasks_hidden). A top-level task only this log has
  // (or one under such a task) may be one of the hidden ones, already counted: its group's counts are then lower
  // bounds (counts_at_least), per status max(the provider's, those local-only tasks') plus the exact rest, never a
  // sum that may count a task twice. A local-only task under a node the provider listed is exact: it was not hidden.
  const capped = !!g && (g.tasks_hidden ?? 0) > 0
  const orig = { ...(g?.counts ?? {}) }, origAttn = g?.attention ?? 0
  const lower: Record<string, number> = {}
  const maybeHidden = new Set<string>()
  let lowerAttn = 0
  const counts = (gg: PulseOrchestrator) => (gg.counts ??= {})
  const tally = (gg: PulseOrchestrator, t: PulseTreeTask, k: number) => {
    const c = counts(gg)
    c[t.status ?? 'unknown'] = (c[t.status ?? 'unknown'] ?? 0) + k
    gg.attention = (gg.attention ?? 0) + k * attnOf(t)
  }
  const clock = (t: LocalTask): number | null => typeof t.started_at === 'number'
    ? Math.max(0, (t.ended_at ?? asOfS) - t.started_at) : null
  let added = 0
  // oldest first, so a parent from the log is placed before a child that hangs under it
  for (const t of [...local].reverse()) {
    const id = t.task_id!
    const known = nodes.get(id)
    if (known) {
      // first-hand: a task this machine saw end is ended, whatever an older report (a heartbeat, a stub) says; one
      // the view knows only second-hand (a stale heartbeat, a stub) takes this log's state
      const secondHand = known.state === 'elsewhere' || known.phase === STALE_HB_PHASE
      if (g && ((t.ended_at != null && isOpen(known.status)) || (secondHand && t.status !== 'unknown'))) {
        tally(g, known, -1)
        Object.assign(known, { state: t.state, status: t.status, phase: null, ended_at: t.ended_at ?? null,
          elapsed_s: clock(t), host: known.host ?? t.host, title: known.title ?? t.title })
        tally(g, known, 1)
      }
      continue
    }
    if (!g) { g = make(); items.unshift(g) }
    const node: PulseTreeTask = { ...t, elapsed_s: clock(t), sub_sessions: [], tasks: [] }
    const parent = t.parent_task_id ? nodes.get(t.parent_task_id) : undefined
    if (parent) (parent.tasks ??= []).unshift(node)
    else (g.tasks ??= []).unshift(node)
    nodes.set(id, node)
    if (capped && (!parent || maybeHidden.has(t.parent_task_id!))) {   // maybe a hidden one: a lower bound
      maybeHidden.add(id)
      lower[node.status ?? 'unknown'] = (lower[node.status ?? 'unknown'] ?? 0) + 1
      lowerAttn += attnOf(node)
    } else tally(g, node, 1)
    added++
  }
  if (g && Object.keys(lower).length) {
    const c = counts(g)
    for (const [k, n] of Object.entries(lower)) c[k] = (c[k] ?? 0) + Math.max(0, n - (orig[k] ?? 0))
    g.attention = (g.attention ?? 0) + Math.max(0, lowerAttn - origAttn)
    g.counts_at_least = true
  }
  if (g) {
    const rank: Record<string, number> = { running: 0, waiting: 1, failed: 2, unknown: 3, done: 4 }
    g.tasks = (g.tasks ?? []).sort((a, b) => (rank[a.status ?? ''] ?? 5) - (rank[b.status ?? ''] ?? 5) ||
      (b.ended_at ?? b.started_at ?? 0) - (a.ended_at ?? a.started_at ?? 0))
  }
  return { view: v, added }
}

/** The note under a tree that took tasks from this machine's log alone. */
export const LOCAL_NOTE = (n: number) => n + (n === 1 ? ' task' : ' tasks') + " from this machine's task log only (its host's heartbeat has not reported " + (n === 1 ? 'it' : 'them') + ')'

/** The fallback view's extra lines: this session's tasks the local log has and the provider's lists do not (`shown`),
 * as a tree. Empty when there are none. */
export function localLines(local: LocalTask[], tag: string, shown: Set<string>, asOfS: number, since: number): TreeLine[] {
  const only = local.filter(t => !shown.has(t.task_id ?? ''))
  if (!only.length) return []
  const { view } = mergeLocal(null, only, tag, asOfS)
  return [{ text: "from this machine's task log:" }, ...treeLines(view, tag, since)]
}

/** The log's path: the state dir as paths.state_dir() finds it ($CORTADOS_HOME, else $CORTADOS_HERMES_HOME, else
 * ~/.cortados). */
export async function localLogPath($: Pick<EngineInterface, 'env'>): Promise<string> {
  const dir = (await $.env.get('CORTADOS_HOME')) || (await $.env.get('CORTADOS_HERMES_HOME')) ||
    expandHome('~/.cortados', await $.env.get('HOME'))
  return String(dir).replace(/\/+$/, '') + '/' + LOCAL_LOG
}

/** The note when the log is over LOCAL_TAIL_BYTES and its tail could not be read. */
export const LOCAL_SKIP_NOTE = "this machine's task log is over 2 MiB and its tail could not be read: its tasks are not merged"

/** The last LOCAL_TAIL_BYTES of the log, never the whole file (review round 3, P2). The mod host's `$.fs.read` has no
 * offset or length (it reads a whole file, up to 4 MiB), so: `$.fs.stat` first; a log within the bound is read whole
 * (that IS its tail); a larger one through `tail -c` (`$.process.run`, no shell, which seeks to the end and copies
 * only those bytes). `cut`: the text starts mid-file (its first line is partial). Throws when the log is missing or
 * unreadable; `{ text: null }` when it is over the bound and `tail` failed (the caller says so, LOCAL_SKIP_NOTE). */
export async function readLocalLog($: Pick<EngineInterface, 'fs' | 'process'>, path: string): Promise<{ text: string | null; cut: boolean }> {
  const st = await $.fs.stat(path)
  if (st.kind !== 'file') throw new Error('not a file')
  if (st.size <= LOCAL_TAIL_BYTES) return { text: String(await $.fs.read(path)), cut: false }
  try {
    const r = await $.process.run([TAIL_BIN, '-c', String(LOCAL_TAIL_BYTES), path], { timeoutMs: 10_000 })
    if (r.exitCode === 0 && typeof r.stdout === 'string') return { text: r.stdout, cut: true }
  } catch { /* cannot start or timed out: skipped below */ }
  return { text: null, cut: true }
}
/** Absolute, so no PATH entry can stand in for it. */
export const TAIL_BIN = '/usr/bin/tail'

/** Read this machine's log for `tag`'s tasks, at most every LOCAL_POLL_MS per tag, its last LOCAL_TAIL_BYTES only
 * (readLocalLog). A missing or unreadable log is no local tasks, said nowhere: the provider's view stands on its own
 * then. A log over the bound whose tail could not be read is no local tasks, with LOCAL_SKIP_NOTE. Never throws. */
export async function readLocal($: Pick<EngineInterface, 'env' | 'fs' | 'process' | 'state' | 'clock'>, tag: string): Promise<void> {
  const at = await $.clock.now()
  const cur = await read($, localHeld)
  if (cur && cur.tag === tag && at - cur.at < LOCAL_POLL_MS) return
  let tasks: LocalTask[] = [], note: string | null = null
  try {
    const { text, cut } = await readLocalLog($, await localLogPath($))
    if (text === null) note = LOCAL_SKIP_NOTE
    else tasks = parseLocalTasks(text, tag, at / 1000, cut)
  } catch { /* no log yet, or unreadable: no local tasks */ }
  await update($, localHeld, () => ({ tag, at, tasks, note }))
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

// ---- the default view (0.6.3; pure, tested) ------------------------------------------------------------------------
// The owner (2026-11-22): "way too complicated, I don't know if it's right". The default view is five things, top to
// bottom: this session's child tasks (one line each), the local Pro model, the subscriptions (only the lowest and any
// that are out), S1, and the dashboard link. Everything else (work split, sub-sessions, usage, every lane's windows,
// local node details, devices, the hosts' jobs) is behind `d`.

/** How many finished tasks the default view lists under the running ones. */
export const FINISHED_SHOWN = 3
/** A title is cut to this many characters so a task stays one line in a 40-60 column pane. */
export const TITLE_MAX = 40
export const NO_TASKS = 'no task started from this session in the last 24 h'
export const UNTAGGED_HEAD = 'started on this machine without a session tag'
export const UNTAGGED_WHY = '(a cortados-route server that knew no session started them, e.g. the desktop app\'s own; d: details)'

const STATE_WORD: Record<string, string> = { queued: 'waiting', elsewhere: 'state on its host' }
const isLive = (t: PulseTreeTask) => t.status === 'running' || t.status === 'waiting'

/** One task, one line: glyph, title, host, model, state, elapsed (counting between polls while it runs). */
export function taskRow(t: PulseTreeTask, since: number): TreeLine {
  const raw = t.title || ('task ' + (t.task_id ?? '?'))
  const title = raw.length > TITLE_MAX ? raw.slice(0, TITLE_MAX - 1) + '…' : raw
  const state = t.state ? (STATE_WORD[t.state] ?? t.state) : (t.status ?? 'unknown')
  const el = typeof t.elapsed_s === 'number' ? dur(t.elapsed_s + (isLive(t) && t.ended_at == null ? since : 0)) : null
  return { text: glyph(t.status, t.state) + ' ' + join([title, t.host, t.model, state, el]),
    attn: t.status === 'failed' || t.status === 'unknown' }
}

/** Every task of a group, nested ones too, flattened: running and waiting first (newest first), then the last
 * FINISHED_SHOWN finished (newest first); a "+N more" line for the rest. Deduplicated by task id. */
export function taskRows(groups: (PulseOrchestrator | undefined)[], since: number): TreeLine[] {
  const all = new Map<string, PulseTreeTask>()
  const walk = (ts: PulseTreeTask[] = []) => ts.forEach(t => { all.set(t.task_id ?? String(all.size), t); walk(t.tasks) })
  let hidden = 0
  for (const g of groups) { if (g) { walk(g.tasks); hidden += g.tasks_hidden ?? 0 } }
  const newest = (t: PulseTreeTask) => -(t.ended_at ?? t.started_at ?? 0)
  const list = [...all.values()]
  const live = list.filter(isLive).sort((a, b) => -(a.started_at ?? 0) + (b.started_at ?? 0))
  const done = list.filter(t => !isLive(t)).sort((a, b) => newest(a) - newest(b))
  const shownLive = live.slice(0, RUNNING_MAX)
  const out = [...shownLive, ...done.slice(0, FINISHED_SHOWN)].map(t => taskRow(t, since))
  const more = live.length - shownLive.length + Math.max(0, done.length - FINISHED_SHOWN) + hidden
  if (more > 0) out.push({ text: '  +' + more + ' more (dashboard)' })
  return out
}

/** The default view's task block from the session view: this session's own tasks (and its alias tag's, the tasks it
 * started under its cortados-route server's tag), then this machine's untagged ones under their own heading. */
export function defaultTaskLines(v: PulseSessionView, tag: string, since: number, aliasView?: PulseSessionView | null,
  aliasTagged?: string | null): TreeLine[] {
  const o = v.orchestrators ?? {}
  if (o.status === 'error') return [{ text: 'tasks unavailable: ' + errs(o), attn: true }]
  const items = o.items ?? []
  const own = items.find(x => x.origin === tag)
  const al = aliasView && aliasTagged ? (aliasView.orchestrators?.items ?? []).find(x => x.origin === aliasTagged) : undefined
  const out: TreeLine[] = []
  if (o.status === 'partial') out.push({ text: 'partial: ' + errs(o), attn: true })
  const rows = taskRows([own, al], since)
  out.push(...(rows.length ? rows : [{ text: NO_TASKS }]))
  const untagged = items.find(x => x.kind === 'untagged')
  if (untagged && (untagged.tasks ?? []).length) {
    out.push({ text: UNTAGGED_HEAD + ' (' + (untagged.device ?? 'this machine') + ')' })
    out.push(...taskRows([untagged], since))
  }
  return out
}

/** The local model in one line: the Pro's node (else the first node), its switch, and why it is or is not allowed. */
export function localModelLine(v: PulseLocalView | null, problem: string | null, prefer = 'pro'): string {
  if (!v) return 'local model: ' + (problem ?? 'loading…')
  const list = v.nodes ?? []
  const n = list.find(x => x.node === prefer) ?? list[0]
  if (!n) return 'local model: ' + (v.note || 'no local node configured')
  const name = 'local ' + (n.node ?? '?')
  if (!n.reachable) return name + ': unreachable' + (n.error ? ' · ' + n.error : '')
  const sw = n.switch ?? 'unknown'
  if (n.allowed === true) return name + ': ' + sw + ' · in use' + (n.models_loaded?.length ? ' · ' + n.models_loaded.join(', ') : '')
  return name + ': ' + sw + (n.allowed === false ? ' · not used' : ' · allowed unknown') + (n.reason ? ' · ' + n.reason : '')
}

/** The subscriptions in one line: the lane with the least left (its lowest window) and every lane that is out. */
export function subsLine(snap: Pulse | null, lowest?: NonNullable<PulseLive['overview']>['lowest_subscription'] | null): string {
  const subs = snap?.subscriptions ?? {}
  const readings = snap?.readings ?? {}
  const lanes = Object.keys(subs).filter(k => k !== 'error' && k !== 'local' && subs[k] !== null && typeof subs[k] === 'object')
  const out: string[] = []
  let low: { label: string; left: number; window: string } | null = null
  for (const lane of laneOrder(lanes)) {
    const h = subs[lane] as PulseLaneHealth
    const r = typeof readings[lane] === 'object' ? readings[lane] as PulseReading : undefined
    const label = laneLabel(lane, r ?? {})
    if (h.ok === false) { out.push(label); continue }
    for (const w of r?.windows ?? []) {
      const left = leftPct(w)
      // the window's name only ("month", "week"): its parenthesised pool detail is in the details
      if (left !== null && (low === null || left < low.left)) low = { label, left, window: (w.window || 'window').split(' (')[0] ?? 'window' }
    }
  }
  if (!low && lowest && typeof lowest.left_pct === 'number') {
    low = { label: lowest.label || lowest.lane || '?', left: lowest.left_pct, window: (lowest.window || 'window').split(' (')[0] ?? 'window' }
  }
  const lowText = low ? `lowest ${low.label} ${pct(low.left)} left (${low.window})` : snap ? 'no quota known' : 'loading…'
  return 'subscriptions: ' + join([lowText, out.length ? 'OUT: ' + out.join(', ') : null])
}

/** S1 in one line: up or down, and the median scoring latency over the last scored turns (else the last probe's). */
export function s1Line(live: PulseLive | PulseSessionView | null, usage?: PulseUsage | null): string {
  const o = live?.overview ?? {}, s1 = live?.s1 ?? {}
  const median = usage?.s1_latency_ms?.median
  const lat = typeof median === 'number' ? `${median} ms median` : typeof s1.latency_ms === 'number' ? `${s1.latency_ms} ms (last probe)` : null
  if (o.s1 === 'up') return join(['S1 up', lat])
  if (o.s1 === 'down') return join(['S1 DOWN', s1.reason ?? null])
  return 'S1 unknown'
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
    void pollNodes($, cfg)
    ticking?.cancel()                                          // a second session.start must not double the polling
    ticks = 0
    ticking = $.clock.every(LIVE_POLL_MS, () => {              // live every tick, /resources every POLL_MS
      void pollLive($, cfg, true)
      if (++ticks % (POLL_MS / LIVE_POLL_MS) === 0) void poll($, cfg)
      if (ticks % (NODES_POLL_MS / LIVE_POLL_MS) === 0) void pollNodes($, cfg)
    })
    return next(e)
  })

  // #127: a start_task of this session names the origin its task was recorded under. The pane remembers it (so a task
  // recorded under the cortados-route server's tag, after a /clear, is still drawn) and reads the live view at once
  // instead of on the next 10 s tick. The result goes on unchanged.
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    try {
      if (START_TASK_TOOL.test(String((e as any).tool ?? '')) && !(ran as any).isError && !(ran as any).deny) {
        const got = startedOf((ran as any).text)
        if (got) {
          const at = await $.clock.now()
          await update($, started, list => [{ ...got, at }, ...list.filter(x => x.task_id !== got.task_id)].slice(0, STARTED_MAX))
          void pollLive($, cfg, true)
        }
      }
    } catch { /* bookkeeping only: never the tool's result */ }
    return ran
  })

  on('command.run', { command: 'cortados-pulse' }, async $ => {
    await $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void pollLive($, cfg, true)
    void poll($, cfg)
    void pollNodes($, cfg)
    return { text: 'CortadOS pulse pane opened.' }
  })

  // The tree is built with the element table's constructors (what JSX compiles to under the engine's `h`) rather
  // than JSX, so the module has no JSX factory to resolve and the pane draws identically under `claude plugin test`.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const line = (text: string) => Text({ children: text })
    const dim = (text: string) => Text({ dimColor: true, children: text })
    const head = (text: string) => Text({ bold: true, children: text })
    const row = (l: TreeLine | AliasLine) => l.attn ? Text({ bold: true, children: l.text })
      : (l as AliasLine).dim ? dim(l.text) : line(l.text)
    const h = await read($, held)
    const problem = await read($, err)
    const lv = await read($, liveHeld)
    const lvProblem = await read($, liveErr)
    const me = await read($, mine)
    const ml = await read($, mineLive)
    const tr = await read($, tree)
    const trProblem = await read($, treeErr)
    const loc = await read($, localHeld)
    const open = await read($, wsOpen)
    const more = await read($, detailsOpen)
    const nv = await read($, nodes)
    const nvProblem = await read($, nodesErr)
    const al = await read($, alias)
    const alErr = await read($, aliasErr)
    const splitRows = (lines: SplitLine[]) => lines.map(l => l.dim ? dim(l.text) : line(l.text))
    const toggle = Button({ key: 'cortados-pulse-ws', label: open ? 'w: hide the work split breakdown' : 'w: work split breakdown',
      hotkey: 'w', plain: true, dimColor: true, onPress: () => { void update($, wsOpen, v => !v) } })
    const details = Button({ key: 'cortados-pulse-details', label: more ? 'd: hide details' : 'd: details',
      hotkey: 'd', plain: true, dimColor: true, onPress: () => { void update($, detailsOpen, v => !v) } })
    const localFor = (tag: string | null | undefined) => loc && tag && loc.tag === tag ? loc.tasks : []
    const nowMs = await $.clock.now()
    const usage = h?.data.usage ?? null
    const subsText = (lowest?: NonNullable<PulseLive['overview']>['lowest_subscription'] | null) =>
      !h && problem ? 'subscriptions: ' + problem : subsLine(h?.data ?? null, lowest)
    // The default view, top to bottom (0.6.3): this session's child tasks, the local model, the subscriptions, S1, the
    // dashboard link. `d` opens the rest (`extra`).
    let top: ReturnType<typeof line>[], extra: ReturnType<typeof line>[], link: ReturnType<typeof line>[]
    if (tr && me?.tag === tr.tag) {
      // the per-session view: this session's tasks (and this machine's untagged ones), nothing of another session
      const since = (nowMs - tr.at) / 1000
      const merged = mergeLocal(tr.data, localFor(tr.tag), tr.tag, tr.at / 1000)
      const alHeld = al && al.tag !== tr.tag && !(alErr?.tag === al.tag && alErr.pendingSince !== null &&
        nowMs - alErr.pendingSince > STALE_AFTER_MS) ? al : null
      top = [
        head("This session's child tasks"),
        ...defaultTaskLines(merged.view, tr.tag, since, alHeld?.data, alHeld?.tag).map(row),
        ...(trProblem ? [dim(trProblem + ' · showing ' + age(since) + ' old data')] : []),
        line(localModelLine(nv?.data ?? null, nvProblem)),
        line(subsText(tr.data.overview?.lowest_subscription)),
        line(s1Line(tr.data, usage)),
      ]
      link = [dim('dashboard ' + tr.base + '/dashboard')]
      extra = [
        head('This session in detail'),
        ...splitRows(workSplitLines(tr.data.work_split, open)),
        ...(tr.data.work_split?.row ? [toggle] : []),
        ...treeLines(merged.view, tr.tag, since).map(row),
        ...(merged.added ? [dim(LOCAL_NOTE(merged.added))] : []),
        ...(loc?.note && loc.tag === tr.tag ? [dim(loc.note)] : []),
        ...aliasLines(al, alErr, tr.tag, nowMs).map(row),
        dim(CLEAR_NOTE),
        head('Fleet'), ...fleetLines(tr.data as PulseLive).map(line),
      ]
    } else {
      // no tag, or a provider from before the per-session view: the fleet view, this session's tasks picked out of it
      const own = ml && me?.tag === ml.tag ? ml : lv
      // this machine's own tasks the provider's lists do not have (none reached yet, or a host "here" task)
      const shownIds = new Set([...(own?.data.tasks?.running ?? []), ...(own?.data.tasks?.queued ?? []),
        ...(own?.data.tasks?.recent ?? [])].map(x => x.task_id ?? ''))
      const mineLocal = me?.tag && loc ? localLines(localFor(me.tag), me.tag, shownIds, loc.at / 1000, (nowMs - loc.at) / 1000) : []
      top = [
        head("This session's child tasks"),
        ...(trProblem ? [dim(trProblem)] : []),
        ...(own ? sessionLines(own.data, me, (nowMs - own.at) / 1000).map(line) : []),
        ...mineLocal.map(row),
        ...(loc?.note && me?.tag && loc.tag === me.tag ? [dim(loc.note)] : []),
        ...(lv ? [] : [dim(me && !me.tag ? me.why || '' : 'waiting for the live view…')]),
        ...(lvProblem ? [dim(lvProblem + (lv ? ' · showing ' + age((nowMs - lv.at) / 1000) + ' old data' : ''))] : []),
        line(localModelLine(nv?.data ?? null, nvProblem)),
        line(subsText(lv?.data.overview?.lowest_subscription)),
        line(s1Line(lv?.data ?? null, usage)),
      ]
      link = lv ? [dim('dashboard ' + lv.base + '/dashboard')] : []
      extra = [
        ...(me?.tag ? [dim(CLEAR_NOTE)] : []),
        head('Running'),
        ...(lv ? runningLines(lv.data, (nowMs - lv.at) / 1000).map(line) : lvProblem ? [] : [dim('loading the live view…')]),
        ...(lv ? [head('Fleet'), ...fleetLines(lv.data).map(line)] : []),
      ]
    }
    if (more) {
      if (h) {
        const s = h.data
        const nowS = typeof s.generated_at === 'number' ? s.generated_at + (nowMs - h.at) / 1000 : nowMs / 1000
        const subs = s.subscriptions ?? {}
        const readings = s.readings ?? {}
        const lanes = laneOrder(Object.keys(subs).filter(k => k !== 'error' && subs[k] !== null && typeof subs[k] === 'object'))
        const subsError = typeof subs.error === 'string' ? subs.error : null
        const readingsError = typeof readings.error === 'string' ? readings.error : null
        extra.push(
          head('Usage'),
          ...usageLines(s.usage ?? {}).map(line),
          head('Subscriptions'),
          ...(subsError ? [dim('subscriptions: ' + subsError)] : []),
          ...(readingsError ? [dim('readings: ' + readingsError)] : []),
          ...(lanes.length === 0 && !subsError ? [dim('none cached')] : []),
          ...lanes.flatMap(lane => laneLines(lane, subs[lane] as PulseLaneHealth,
            typeof readings[lane] === 'object' ? readings[lane] as PulseReading : undefined, nowS)).map(line),
        )
      }
      extra.push(
        head('Local nodes'),
        ...localNodeLines(nv?.data ?? null, nvProblem).map(line),
      )
      if (h) extra.push(
        head('Devices'),
        dim('resource-pool probe (fleet config SSH targets), not the local gate'),
        ...deviceLines(h.data.devices ?? { status: 'pending', nodes: [] }).map(line),
      )
      extra.push(dim(h ? 'snapshot ' + age((nowMs - h.at) / 1000) + ' old' + (problem ? ' · ' + problem : '')
        : problem || 'loading the resource pool…'))
    }
    return Box({ flexDirection: 'column', children: [...top, ...link, details, ...(more ? extra : [])] })
  })
}
