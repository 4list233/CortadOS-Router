import { test, expect, mock } from 'claude-code/testing'

const TOKEN = 'sekrit-token-123'
const SNAP = { generated_at: 1_700_000_000, usage: { total_routes: 5, degraded: { count: 0, of: 5 }, s1_latency_ms: { median: 300, p90: 500, window: 5 } },
  subscriptions: { codex: { ok: true, scarcity: 0.1 }, 'claude#b': { ok: true, scarcity: -0.2, config_dir: '/u/.claude-acctB' }, claude: { ok: false, why: 'week 100% used, resets soon', scarcity: 1.0 } },
  readings: { 'claude#b': { fetched_at: 1_700_000_000 - 60, failed: false, windows: [{ window: 'week (all models)', pool: 'all', headroom_pct: 55 }] } },
  devices: { status: 'ok', nodes: [{ id: 'studio', status: 'online', gpu_headroom_estimate_bytes: 2 ** 30 * 10, fits: ['a'] }], pool_headroom_estimate_bytes: 2 ** 30 * 10, pool_fits: ['a'] } }

test('the pane draws the lanes from /resources and never the token', { options: { providerUrl: 'http://air.example:8432', providerToken: TOKEN } }, async ($, on) => {
  const clock = mock.clock(on, { now: 1_700_000_000_000 })
  const opened: any[] = []
  on('ui.open', async (_$, e) => { opened.push(e); return { value: { isPlaced: true } } })
  const fetched: any[] = []
  on('http.fetch', async (_$, e) => { fetched.push(e); return { value: { status: 200, ok: true, headers: {}, text: JSON.stringify(SNAP) } } })
  await $.command.run({ command: 'cortados-pulse', args: '' } as any)
  await clock.advance(1)                                        // lets the poll's state write settle
  expect(opened.length).toBe(1)
  expect(opened[0].id).toBe('cortados-pulse')
  expect(fetched.length).toBe(1)
  expect(fetched[0].url).toBe('http://air.example:8432/resources')
  expect(fetched[0].init.headers.authorization).toBe('Bearer ' + TOKEN)
  const ui = await $.ui.mount({ plugin: 'cortados-pulse', surface: 'terminal', component: 'Pane', requestId: 'cortados-pulse',
    props: { title: 'CortadOS pulse', isFocused: false, bodyColumns: 80, placement: 'dock' } as any })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  const all = texts.join('\n')
  expect(all.includes(TOKEN)).toBe(false)
  expect(all).toMatch(/routes 5 · degraded 0\/5/)
  expect(all).toMatch(/claude: SPENT · scarcity 1.00/)
  expect(all).toMatch(/claude#b @.claude-acctB: ok · scarcity -0.20 · sample 60 s old/)
  expect(all).toMatch(/week \(all models\): 55% left/)
  expect(all).toMatch(/studio online · 10.0 GiB free · fits 1/)
  const order = texts.filter(t => /^(claude|claude#b|codex)[ :]/.test(t)).map(t => t.split(/[ :]/)[0])
  expect(order).toEqual(['claude', 'claude#b', 'codex'])
  await ui.unmount()
})
