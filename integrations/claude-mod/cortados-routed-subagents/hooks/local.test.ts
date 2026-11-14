import { test, expect } from 'claude-code/testing'
import { register, parseLocalArgs, localNodeLine, runLocalCommand, LOCAL_COMMAND, LOCAL_TIMEOUT_MS } from './register'

// /cortados-local: the Pro's local-models switch through the provider (GET /v1/cortados/local, POST
// /v1/cortados/local/switch). A fake `$` stands in for the host; no network.

const PRO = { node: 'pro', label: 'Pro', reachable: true, error: null, switch: 'on', allowed: true,
  reason: 'local enabled: AC power + manual switch on', power: 'ac', training: false, ollama: 'up',
  models_loaded: ['Qwen27b_heretic:latest'], auto_route: { model: 'qwen-27b', bands: ['R0'] }, models: ['qwen-27b'] }

type Answer = { status: number; body: unknown } | 'throw' | 'hang'

function world(answer: Answer, opts: Record<string, unknown> = {}) {
  const fetches: { url: string; init: any }[] = []
  const $: any = {
    process: { run: async () => ({ exitCode: 0, stdout: '\0\0/tmp/home', stderr: '' }) },
    fs: { read: async () => { throw new Error('no file') } },
    http: {
      fetch: (url: string, init: any) => {
        fetches.push({ url, init })
        if (answer === 'throw') return Promise.reject(new TypeError('network down'))
        if (answer === 'hang') return new Promise(() => {})
        return Promise.resolve({ status: answer.status, ok: answer.status >= 200 && answer.status < 300, headers: {},
          text: JSON.stringify(answer.body) })
      },
    },
  }
  const cfg: any = { providerToken: 'secret-token-123', providerUrl: 'http://provider.test:8432/', providerTokenFile: '~/x', ...opts }
  return { $, cfg, fetches }
}

test('arguments: on, off, status, a node, and bad input', () => {
  expect(parseLocalArgs('')).toEqual({ state: 'status', node: null })
  expect(parseLocalArgs(' ON ')).toEqual({ state: 'on', node: null })
  expect(parseLocalArgs('off studio')).toEqual({ state: 'off', node: 'studio' })
  expect(parseLocalArgs('maybe')).toBe(null)
  expect(parseLocalArgs('on pro extra')).toBe(null)
  expect(parseLocalArgs('on ../x;rm')).toBe(null)
})

test('status asks GET /v1/cortados/local with the token and prints one line per node', async () => {
  const w = world({ status: 200, body: { nodes: [PRO] } })
  const text = await runLocalCommand(w.$, w.cfg, 'status')
  expect(w.fetches.length).toBe(1)
  expect(w.fetches[0].url).toBe('http://provider.test:8432/v1/cortados/local')
  expect(w.fetches[0].init.method).toBe('GET')
  expect(w.fetches[0].init.headers['x-api-key']).toBe('secret-token-123')
  expect(text).toBe('Pro: switch on · local models allowed · AC power · loaded: Qwen27b_heretic:latest · auto_route qwen-27b for R0')
})

test('on / off POST {node, on} to /v1/cortados/local/switch (default node pro)', async () => {
  const w = world({ status: 200, body: { ...PRO, switch: 'off', allowed: false, reason: 'local switch off', models_loaded: [] } })
  const text = await runLocalCommand(w.$, w.cfg, 'off')
  expect(w.fetches[0].url).toBe('http://provider.test:8432/v1/cortados/local/switch')
  expect(w.fetches[0].init.method).toBe('POST')
  expect(JSON.parse(w.fetches[0].init.body)).toEqual({ node: 'pro', on: false })
  expect(text).toBe('switched off: Pro: switch off · refused (local switch off) · AC power · no model loaded · auto_route qwen-27b for R0')
  const w2 = world({ status: 200, body: PRO })
  await runLocalCommand(w2.$, w2.cfg, 'on studio')
  expect(JSON.parse(w2.fetches[0].init.body)).toEqual({ node: 'studio', on: true })
})

test('battery and an unreachable node are said plainly', () => {
  expect(localNodeLine({ ...PRO, allowed: false, power: 'battery', reason: 'local disabled: on battery' }))
    .toContain('refused (local disabled: on battery) · on battery')
  expect(localNodeLine({ node: 'pro', label: 'Pro', reachable: false, error: 'local node pro unreachable (TimeoutError)' }))
    .toBe('Pro: unreachable (local node pro unreachable (TimeoutError))')
})

test('provider errors, unknown node, network failure and timeout come back as text, token scrubbed', async () => {
  const w = world({ status: 404, body: { error: { message: "cortados: unknown local node 'x' secret-token-123" } } })
  const text = await runLocalCommand(w.$, w.cfg, 'on x')
  expect(text).toBe("/cortados-local: provider answered HTTP 404: cortados: unknown local node 'x' ***")
  const w401 = world({ status: 401, body: { error: { message: 'bad or missing token' } } })
  expect(await runLocalCommand(w401.$, w401.cfg, 'status')).toBe('/cortados-local: provider answered HTTP 401: bad or missing token')
  const wt = world('throw')
  expect(await runLocalCommand(wt.$, wt.cfg, 'status')).toBe('/cortados-local: cannot reach the provider (TypeError)')
  expect(await runLocalCommand(wt.$, wt.cfg, 'bogus')).toBe(`usage: /${LOCAL_COMMAND} on|off|status [node]`)
  expect(wt.fetches.length).toBe(1)                                    // a bad argument sends nothing
  const wn = world({ status: 200, body: { nodes: [PRO] } })
  expect(await runLocalCommand(wn.$, wn.cfg, 'status studio')).toBe("/cortados-local: no local node 'studio' on the provider")
  expect(LOCAL_TIMEOUT_MS).toBe(10_000)
})

test('the command is registered and runs through command.run', async () => {
  const h: Record<string, any> = {}
  const on: any = (ev: string, a: any, b?: any) => { if (ev === 'command.run') h[a.command] = b; return { catch() {} } }
  register(on, { providerToken: 'secret-token-123', providerUrl: 'http://provider.test:8432', orchestratorMode: false, officeView: false } as any)
  const w = world({ status: 200, body: { nodes: [PRO] } })
  const r = await h[LOCAL_COMMAND](w.$, { command: LOCAL_COMMAND, args: '' })
  expect(r.text).toContain('Pro: switch on')
})
