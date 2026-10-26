import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Pulse } from '../types'

const PANE = 'cortados-pulse'
const snap = atom({ plugin: 'cortados-pulse', key: 'snap' } as const, null as Pulse | null)
const err = atom({ plugin: 'cortados-pulse', key: 'err' } as const, null as string | null)

async function poll($: EngineInterface, url: string, token: string): Promise<void> {
  if (!token) {
    await update($, err, () => 'Set providerToken in the mod config to load the pulse.')
    return
  }
  try {
    const r = await $.http.fetch(url + '/resources', { headers: { authorization: 'Bearer ' + token } })
    if (!r.ok) {
      await update($, err, () => 'provider returned HTTP ' + r.status)
      return
    }
    const data = JSON.parse(r.text) as Pulse
    await update($, snap, () => data)
    await update($, err, () => null)
  } catch (e) {
    await update($, err, () => 'could not reach the provider (' + ((e as Error)?.message ?? String(e)) + ')')
  }
}

export const register: Register = (on, options) => {
  const cfg = (options ?? {}) as { providerUrl?: string; providerToken?: string }
  const url = String(cfg.providerUrl || 'http://127.0.0.1:8432').replace(/\/+$/, '')
  const token = String(cfg.providerToken || '')

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'cortados-pulse', description: 'Open the CortadOS router pulse pane' })
    void $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void poll($, url, token)
    $.clock.every(20000, () => void poll($, url, token))
    return next(e)
  })

  on('command.run', { command: 'cortados-pulse' }, async $ => {
    await $.ui.open({ id: PANE, title: 'CortadOS pulse' })
    void poll($, url, token)
    return { text: 'CortadOS pulse pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snap)
    const problem = await read($, err)
    const gib = (b?: number | null) => (b === null || b === undefined) ? '—' : (b / 2 ** 30).toFixed(1) + ' GiB'
    if (!s) {
      return (
        <Box flexDirection="column">
          <Text dimColor>{problem || 'loading the resource pool…'}</Text>
        </Box>
      )
    }
    const u = s.usage ?? {}
    const lat = u.s1_latency_ms ?? {}
    const deg = u.degraded ?? { count: 0, of: 0 }
    const dev = s.devices ?? { status: 'pending' }
    const subs = Object.entries(s.subscriptions ?? {}).filter(([k, v]) => k !== 'error' && typeof v === 'object')
    return (
      <Box flexDirection="column">
        {u.error && <Text dimColor>usage: {u.error}</Text>}
        <Text>routes {u.total_routes ?? '—'} · degraded {deg.count}/{deg.of}</Text>
        <Text>S1 {lat.median ?? '—'}ms median · p90 {lat.p90 ?? '—'}ms</Text>
        <Text dimColor>— subscriptions —</Text>
        {subs.length === 0 && <Text dimColor>none cached</Text>}
        {subs.map(([lane, hv]) => { const h = hv as { scarcity?: number | null; expiring_pct?: number; ok?: boolean }; return (
          <Text>{lane}: scarcity {h.scarcity ?? '—'} · expiring {h.expiring_pct ?? 0}%{h.ok === false ? ' SPENT' : ''}</Text>
        ) })}
        <Text dimColor>— devices ({dev.status ?? '—'}) —</Text>
        {(dev.nodes ?? []).map(n => (
          <Text>{n.id} {n.status ?? ''} · {gib(n.gpu_headroom_estimate_bytes)} · fits {(n.fits ?? []).length}</Text>
        ))}
        {problem && <Text dimColor>({problem})</Text>}
      </Box>
    )
  })
}
