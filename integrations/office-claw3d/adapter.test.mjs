import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mapOffice, characterFamily, knownBackend, modelFamily, READ_METHODS, OfficeGateway, pollOffice, pollTree,
  runPolling } from './adapter.mjs';

const worker = { run_id: 'run-12345678', provider: 'codex', lane: 'codex', model: 'gpt',
  title: 'Review', status: 'working', tool: 'Read' };
const view = () => ({ version: 1, floors: [{ session: '0123456789abcdef',
  project: 'Router', device: 'pro', orchestrator: { model: 'claude', status: 'idle', tool: null },
  workers: [{ ...worker }] }], lobby: { workers: [] } });

// The same office in the shape the gateway now reads (GET /v1/cortados/office/tree, live.office_tree).
const NOW = 1_800_000_000;
const task = { task_id: 't20261124100000-a0000001', title: 'Review', state: 'running', status: 'running', phase: null,
  tool: 'Read', host: 'air', provider: 'codex', lane: 'codex', local_backend: null, model: 'gpt',
  started_at: NOW - 60, ended_at: null, elapsed_s: 60, parent_task_id: null, origin_via: null, sub_sessions_hidden: 0,
  sub_sessions: [], tasks: [] };
const tree = () => ({ version: 1, generated_at: NOW, orchestrators: { as_of: NOW, status: 'ok', errors: [],
  window_s: 86400, truncated: false, items: [{ origin: '0123456789abcdef', kind: 'session', project: 'Router',
    device: 'pro', status: 'idle', model: 'claude', tool: null, client: null, client_source: null,
    last_activity: NOW, counts: {}, attention: 0, tasks_hidden: 0, routed_hidden: 0,
    tasks: [structuredClone(task)], routed: [] }] } });
const emptyTree = () => ({ ...tree(), orchestrators: { ...tree().orchestrators, items: [] } });

for (const family of ['claude', 'codex', 'grok', 'ollama']) {
  test('character family ' + family, () => assert.equal(characterFamily(family), family));
}
test('unknown provider is neutral, not a vendor guess', () => {
  assert.equal(characterFamily(null), 'unknown');
  assert.equal(characterFamily('other'), 'unknown');
  assert.equal(characterFamily(null, null, 'claude-opus-5-5'), 'unknown'); // a worker's model never decides
  assert.equal(characterFamily('other', 'claude#b'), 'unknown');           // a named unknown provider wins over lane
  for (const odd of ['__proto__', 'constructor', 'toString', 'hasOwnProperty']) {
    assert.equal(characterFamily(odd), 'unknown', odd);
    assert.equal(characterFamily(null, odd), 'unknown', odd);
    assert.equal(characterFamily(null, odd + '#b'), 'unknown', odd);
  }
});
test('floors map orchestrator suit and provider metadata', () => {
  const agents = mapOffice(view());
  assert.equal(agents.length, 2);
  assert.equal(agents[0].office.outfit, 'suit');
  assert.equal(agents[0].office.provider, null);
  assert.equal(agents[1].office.characterFamily, 'codex');
  assert.equal(agents[1].office.project, 'Router');
  assert.equal(agents[1].office.tool, 'Read');
});
test('projection is pure and drops arbitrary sensitive fields', () => {
  const input = view();
  input.floors[0].workers[0].prompt = 'SECRET';
  input.floors[0].workers[0].commands = 'SECRET';
  const before = JSON.stringify(input);
  assert.ok(!JSON.stringify(mapOffice(input)).includes('SECRET'));
  assert.equal(JSON.stringify(input), before);
});
test('IDs stable across status changes and distinct across floors', () => {
  const input = view();
  const id = mapOffice(input)[1].id;
  input.floors[0].workers[0].status = 'done';
  assert.equal(mapOffice(input)[1].id, id);
  input.floors.push({ ...input.floors[0], session: 'abcdef0123456789' });
  assert.equal(new Set(mapOffice(input).map(a => a.id)).size, 4);
});
test('lobby anonymous IDs distinct and no fake orchestrator', () => {
  const input = { version: 1, floors: [], lobby: { workers: [{ ...worker, run_id: null }, { ...worker, run_id: null }] } };
  const agents = mapOffice(input);
  assert.notEqual(agents[0].id, agents[1].id);
  assert.equal(agents[0].office.floor, 'lobby');
  assert.deepEqual(mapOffice(input), agents);
});
test('invalid snapshots rejected and empty board accepted', () => {
  for (const input of [null, {}, { ...view(), version: 2 }]) assert.throws(() => mapOffice(input));
  assert.deepEqual(mapOffice({ version: 1, floors: [], lobby: { workers: [] } }), []);
});
function connected() {
  const frames = [];
  const gateway = new OfficeGateway(frame => frames.push(frame));
  gateway.request({ type: 'req', id: 'connect', method: 'connect' });
  return { gateway, frames };
}
test('handshake, lists, sessions, status and mutation rejection', () => {
  const { gateway, frames } = connected();
  assert.equal(frames[0].payload.protocol, 3);
  gateway.update(tree());
  for (const method of ['agents.list', 'sessions.list', 'status']) {
    gateway.request({ type: 'req', id: method, method });
    assert.equal(frames.at(-1).ok, true);
  }
  gateway.request({ type: 'req', id: 'deny', method: 'chat.send' });
  assert.equal(frames.at(-1).error.code, 'READ_ONLY');
});
test('hello advertises read methods only; mutations stay READ_ONLY', () => {
  const { gateway, frames } = connected();
  const advertised = frames[0].payload.features.methods;
  assert.deepEqual(advertised, [...READ_METHODS]);
  for (const m of advertised) assert.ok(!/\.(send|set|patch|create|update|delete|add|run|remove|reset|abort|resolve)$/.test(m), m);
  for (const method of ['chat.send', 'config.patch', 'config.set', 'agents.create', 'agents.update', 'agents.delete',
    'agents.files.set', 'sessions.patch', 'sessions.reset', 'chat.abort', 'exec.approvals.set', 'exec.approval.resolve',
    'cron.add', 'cron.run', 'cron.remove', 'wake', 'agent.wait', 'toString', '__proto__', 'constructor']) {
    gateway.request({ type: 'req', id: method, method });
    assert.equal(frames.at(-1).ok, false, method);
    assert.equal(frames.at(-1).error.code, 'READ_ONLY', method);
  }
});
test('neutral reads Studio needs are fixed and carry no data', () => {
  const { gateway, frames } = connected();
  gateway.update(tree());
  const ask = (method, params = {}) => { gateway.request({ type: 'req', id: method, method, params }); return frames.at(-1); };
  // reload mode "off": Studio's ensureGatewayReloadModeHotForLocalStudio then never writes config
  assert.equal(ask('config.get').payload.config.gateway.reload.mode, 'off');
  assert.deepEqual(ask('exec.approvals.get').payload.file.agents, {});
  assert.deepEqual(ask('agents.files.get', { agentId: 'x', name: 'IDENTITY.md' }).payload, { file: { missing: true } });
  const preview = ask('sessions.preview', { keys: ['agent:a:main', 7, 'agent:b:main'] }).payload.previews;
  assert.deepEqual(preview, [{ key: 'agent:a:main', status: 'empty', items: [] }, { key: 'agent:b:main', status: 'empty', items: [] }]);
  assert.equal(ask('sessions.preview', { keys: Array(500).fill('k') }).payload.previews.length, 64);
  assert.deepEqual(ask('chat.history', { sessionKey: 'agent:a:main' }).payload.messages, []);
  for (const m of ['models.list', 'skills.status', 'cron.list']) {
    assert.deepEqual(Object.values(ask(m).payload), [[]], m);
  }
});
test('workers wear their provider/lane; the orchestrator wears a suit styled by its model', () => {
  const input = view();
  input.floors[0].orchestrator.model = 'claude-opus-5-5';
  input.floors[0].workers = [
    { ...worker, run_id: 'w-claude-1', provider: 'claude', lane: 'claude#b', model: 'claude-sonnet-5' },
    { ...worker, run_id: 'w-codex-01', provider: 'codex', lane: 'codex', model: 'gpt-6.1-sol' },
    { ...worker, run_id: 'w-ollama-1', provider: 'local', lane: 'local-pro', model: 'qwen3:27b' },
    { ...worker, run_id: 'w-mlx-0001', provider: 'local', lane: 'local-pro', model: 'mlx-community/Qwen3.8-27B-4bit' },
    { ...worker, run_id: 'w-mlxtag-1', provider: 'local', lane: 'local-pro', model: 'qwen3-mlx:27b' },
    { ...worker, run_id: 'w-none-001', provider: null, lane: null, model: 'gpt-6.1' },
    { ...worker, run_id: 'w-lane-001', provider: null, lane: 'codex', model: null },
  ];
  const agents = mapOffice(input);
  assert.deepEqual(agents.map(a => [a.office.characterFamily, a.office.outfit, a.office.sprite]), [
    ['claude', 'suit', 'claude-suit'], ['claude', 'worker', 'claude'], ['codex', 'worker', 'codex'],
    ['ollama', 'worker', 'ollama'], ['local', 'worker', 'local'], ['local', 'worker', 'local'],
    ['unknown', 'worker', 'unknown'], ['codex', 'worker', 'codex']]);
  // appearance never rewrites the published provider: the orchestrator's stays null
  assert.equal(agents[0].office.provider, null);
  assert.equal(agents[6].office.provider, null);
  for (const a of agents) {
    assert.match(a.identity.avatarUrl, /^data:image\/png;base64,[A-Za-z0-9+/=]+$/);
    assert.equal(a.identity.avatarProfile.seed, a.id);
    assert.equal(a.identity.avatarProfile.clothing.topStyle, a.role === 'orchestrator' ? 'jacket' : 'hoodie');
  }
  assert.equal(agents[1].identity.avatarUrl, agents[0 + 1].identity.avatarUrl);
  assert.notEqual(agents[0].identity.avatarUrl, agents[1].identity.avatarUrl); // suit differs from worker
});
test('orchestrator model families', () => {
  const cases = { 'claude-opus-5-5': 'claude', 'sub/claude-sonnet-5': 'claude', opus: 'claude', 'fable': 'claude',
    'gpt-6.1-sol': 'codex', 'codex-mini': 'codex', 'o4-mini': 'codex', 'grok-5': 'grok', 'xai/grok-4': 'grok',
    'qwen3:27b': 'ollama', 'llama3.3:70b-q4_K_M': 'ollama', 'mlx-community/Qwen3-4bit': 'local', 'qwen3.8-27b': 'local',
    cortados: 'unknown', '': 'unknown', 'gemini-3': 'unknown', 'kimi-k3': 'unknown' };
  for (const [model, family] of Object.entries(cases)) assert.equal(modelFamily(model), family, model);
  assert.equal(modelFamily(null), 'unknown');
  assert.equal(modelFamily(42), 'unknown');
});
test('local_backend from office.py makes the local sprite exact; the name shape is only the fallback', () => {
  // the backend wins over the model id's shape, both ways
  assert.equal(characterFamily('local', 'local-pro', 'mlx-community/Qwen3-4bit', 'ollama'), 'ollama');
  assert.equal(characterFamily('local', 'local-pro', 'qwen3:27b', 'vllm-mlx'), 'local');
  assert.equal(characterFamily(null, 'local-studio', 'qwen3:27b', 'vllm-mlx'), 'local');      // lane-keyed local too
  assert.equal(characterFamily('local', 'local-pro', 'qwen3:27b', 'some-future-backend'), 'local'); // local, not Ollama
  // absent or null: the name-shape guess, as before
  assert.equal(characterFamily('local', 'local-pro', 'qwen3:27b', null), 'ollama');
  assert.equal(characterFamily('local', 'local-pro', 'qwen3:27b'), 'ollama');
  assert.equal(characterFamily('local', 'local-pro', 'mlx-community/Qwen3-4bit', undefined), 'local');
  // a backend never makes a cloud worker local
  assert.equal(characterFamily('claude', 'claude#b', 'claude-sonnet-5', 'ollama'), 'claude');
  assert.equal(characterFamily('other', null, 'qwen3:27b', 'ollama'), 'unknown');
  // the orchestrator: exact when its routed local main loop names a backend
  assert.equal(modelFamily('mlx-community/Qwen3-4bit', 'ollama'), 'ollama');
  assert.equal(modelFamily('qwen3:27b', 'vllm-mlx'), 'local');
  assert.equal(modelFamily('qwen3:27b', null), 'ollama');
  assert.equal(modelFamily('claude-opus-5-5', null), 'claude');
  assert.equal(knownBackend('ollama'), 'ollama');
  assert.equal(knownBackend('vllm-mlx'), 'vllm-mlx');
  for (const odd of ['__proto__', 'constructor', 'Ollama', '', null, 7]) assert.equal(knownBackend(odd), null, String(odd));
});
test('mapOffice reads local_backend per record and publishes only known values', () => {
  const input = view();
  input.floors[0].orchestrator = { ...input.floors[0].orchestrator, model: 'qwen3:27b', local_backend: 'vllm-mlx' };
  input.floors[0].workers = [
    { ...worker, run_id: 'w-exact-ol', provider: 'local', lane: 'local-pro', local_backend: 'ollama',
      model: 'mlx-community/Qwen3-4bit' },
    { ...worker, run_id: 'w-exact-ml', provider: 'local', lane: 'local-pro', local_backend: 'vllm-mlx', model: 'qwen3:27b' },
    { ...worker, run_id: 'w-guess-01', provider: 'local', lane: 'local-pro', local_backend: null, model: 'qwen3:27b' },
    { ...worker, run_id: 'w-future-1', provider: 'local', lane: 'local-pro', local_backend: 'llama.cpp', model: 'qwen3:27b' },
  ];
  const agents = mapOffice(input);
  assert.deepEqual(agents.map(a => [a.office.characterFamily, a.office.localBackend]), [
    ['local', 'vllm-mlx'], ['ollama', 'ollama'], ['local', 'vllm-mlx'], ['ollama', null], ['local', null]]);
  const bad = view();
  bad.floors[0].workers[0].local_backend = 42;                    // a wrong type is a malformed snapshot
  assert.throws(() => mapOffice(bad), /invalid office record/);
});
test('appearance output is metadata only and the same for the same input', () => {
  const input = view();
  input.floors[0].workers[0].prompt = 'SECRET-PROMPT';
  const a = JSON.stringify(mapOffice(input));
  assert.ok(!a.includes('SECRET'));
  assert.equal(JSON.stringify(mapOffice(input)), a);
});
test('working lifecycle does not duplicate start, ends on removal', () => {
  const { gateway, frames } = connected();
  gateway.update(tree());
  gateway.update(tree());
  const lifecycle = () => frames.filter(f => f.event === 'agent' && f.payload.stream === 'lifecycle');
  assert.equal(lifecycle().length, 1);
  assert.equal(lifecycle()[0].payload.data.phase, 'start');
  gateway.update(emptyTree());
  assert.equal(lifecycle().at(-1).payload.data.phase, 'end');
  assert.deepEqual(frames.at(-1).payload.agents, []);
});
test('error terminal event and invalid snapshot retains good state', () => {
  const { gateway, frames } = connected();
  gateway.update(tree());
  const input = tree();
  Object.assign(input.orchestrators.items[0].tasks[0], { state: 'failed', status: 'failed', ended_at: NOW - 5 });
  gateway.update(input);
  assert.ok(frames.some(f => f.event === 'agent' && f.payload.data.phase === 'error'));
  assert.throws(() => gateway.update({}));
  assert.equal(gateway.agents.length, 2);
});
test('requests before connect refused', () => {
  const frames = []; const gateway = new OfficeGateway(f => frames.push(f));
  gateway.request({ type: 'req', id: 'a', method: 'agents.list' });
  assert.equal(frames[0].ok, false);
});
test('poll uses file credential, exact GET, no redirects; rereads rotation', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'office-adapter-'));
  try {
    const tokenFile = join(dir, 'token');
    await writeFile(tokenFile, 'test-token-1234567890\n', { mode: 0o600 });
    const options = { url: 'http://127.0.0.1:8432/v1/cortados/office', tokenFile,
      fetchImpl: async (url, init) => {
        assert.equal(url.pathname, '/v1/cortados/office');
        assert.equal(init.method, 'GET'); assert.equal(init.redirect, 'error');
        assert.equal(init.headers.Authorization, 'Bearer test-token-1234567890');
        return { ok: true, json: async () => view() };
      } };
    assert.equal((await pollOffice(options)).version, 1);
    await writeFile(tokenFile, 'replacement-token-12345');
    await pollOffice({ ...options, fetchImpl: async (_, init) => {
      assert.equal(init.headers.Authorization, 'Bearer replacement-token-12345');
      return { ok: true, json: async () => view() };
    } });
    await assert.rejects(pollOffice({ ...options, url: 'http://example.com/v1/cortados/office' }));
    await assert.rejects(pollOffice({ ...options, url: 'https://example.com/v1/messages' }));
    await assert.rejects(pollOffice({ ...options, fetchImpl: async () => ({ ok: false }) }), /office request failed/);
    // the tree: the exact path beside the configured office URL, same token, same rules
    let asked = null;
    await pollTree({ ...options, fetchImpl: async (url, init) => {
      asked = url.href; assert.equal(init.redirect, 'error');
      return { ok: true, json: async () => tree() };
    } });
    assert.equal(asked, 'http://127.0.0.1:8432/v1/cortados/office/tree');
    for (const url of ['http://127.0.0.1:8432/v1/cortados/office/tree', 'http://127.0.0.1:8432/v1/cortados/office?x=1',
      'http://example.com/v1/cortados/office', 'http://127.0.0.1:8432/v1/cortados/live']) {
      await assert.rejects(pollTree({ ...options, url }), /office URL/, url);
    }
    await assert.rejects(pollOffice({ ...options, path: '/v1/cortados/live' }), /office URL/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
test('a task named like the orchestrator has independent roster, session and lifecycle IDs', () => {
  const input = tree();
  input.orchestrators.items[0].status = 'working';
  input.orchestrators.items[0].tasks[0].task_id = 'orchestrator';
  const { gateway, frames } = connected();
  gateway.update(input);
  assert.equal(new Set(gateway.agents.map(a => a.id)).size, 2);
  assert.equal(new Set(gateway.sessions().map(s => s.key)).size, 2);
  assert.equal(gateway.runs.size, 2);
  const starts = frames.filter(f => f.event === 'agent' && f.payload.stream === 'lifecycle');
  assert.equal(starts.length, 2);
  assert.notEqual(starts[0].payload.runId, starts[1].payload.runId);
});

test('nested malformed records retain all previous state and emit nothing', () => {
  const badTasks = [[], {}, null, false, { ...task, status: 'unknown-state' }, { ...task, status: null },
    { ...task, provider: [] }, { ...task, model: {} }, { ...task, task_id: '' }, { ...task, task_id: 'short' },
    { ...task, task_id: 123 }, { ...task, ended_at: 'yesterday' }, { ...task, sub_sessions: null },
    { ...task, tasks: {} }, { ...task, sub_sessions: [{ ref: 'c-0000000000aa', status: 'running', model: [] }] },
    { ...task, sub_sessions: [{ ref: '../x', status: 'running' }] }];
  const group = tree().orchestrators.items[0];
  const badGroups = [[], false, { ...group, kind: 'session', origin: 'lobby' }, { ...group, kind: 'bot', origin: 'x' },
    { ...group, kind: 'unattributed', origin: '0123456789abcdef' }, { ...group, model: {} }, { ...group, tasks: null },
    { ...group, routed: [{ ref: 'r', status: 'running' }] }, { ...group, client: 7 }];
  for (const target of ['task', 'group']) {
    for (const bad of target === 'group' ? badGroups : badTasks) {
      const { gateway, frames } = connected();
      gateway.update(tree());
      const previous = gateway.agents;
      const runs = [...gateway.runs];
      const generation = gateway.generation;
      const count = frames.length;
      const input = tree();
      if (target === 'task') input.orchestrators.items[0].tasks = [bad];
      else input.orchestrators.items = [bad];
      assert.throws(() => gateway.update(input), /invalid office tree|duplicate/, JSON.stringify(bad));
      assert.equal(gateway.agents, previous);
      assert.deepEqual([...gateway.runs], runs);
      assert.equal(gateway.generation, generation);
      assert.equal(frames.length, count);
    }
  }
});

test('duplicate and malformed floor identities fail closed', () => {
  for (const mutate of [
    input => input.floors.push(structuredClone(input.floors[0])),
    input => input.floors[0].workers.push({ ...worker }),
    input => { input.floors[0].session = 'lobby'; },
    input => { input.floors[0].project = []; },
    input => { input.floors[0].orchestrator = false; },
  ]) {
    const input = view(); mutate(input);
    assert.throws(() => mapOffice(input));
  }
});

test('token length boundaries are enforced before fetching', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'office-token-boundaries-'));
  try {
    const tokenFile = join(dir, 'token');
    for (const length of [15, 16, 4096, 4097]) {
      await writeFile(tokenFile, 'x'.repeat(length), { mode: 0o600 });
      let calls = 0;
      const promise = pollOffice({ url: 'http://localhost/v1/cortados/office', tokenFile,
        fetchImpl: async () => { calls++; return { ok: true, json: async () => view() }; } });
      if (length === 16 || length === 4096) {
        await promise; assert.equal(calls, 1);
      } else {
        await assert.rejects(promise, /invalid office token file/); assert.equal(calls, 0);
      }
    }
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('request timeout is ten seconds and combines caller cancellation', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'office-timeout-'));
  const timeoutController = new AbortController();
  const caller = new AbortController();
  t.mock.method(AbortSignal, 'timeout', milliseconds => {
    assert.equal(milliseconds, 10000);
    return timeoutController.signal;
  });
  try {
    const tokenFile = join(dir, 'token');
    await writeFile(tokenFile, 'test-token-1234567890');
    for (const signal of [undefined, caller.signal]) {
      let requestSignal;
      await pollOffice({ url: 'http://localhost/v1/cortados/office', tokenFile, signal,
        fetchImpl: async (_, init) => {
          requestSignal = init.signal;
          return { ok: true, json: async () => view() };
        } });
      assert.equal(requestSignal.aborted, false);
      if (signal) {
        caller.abort(); assert.equal(requestSignal.aborted, true);
      } else {
        assert.equal(requestSignal, timeoutController.signal);
      }
    }
    timeoutController.abort();
    assert.equal(timeoutController.signal.aborted, true);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('default reporter exposes failed polls safely and loop recovers', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'office-recovery-'));
  const controller = new AbortController();
  const diagnostics = [];
  t.mock.method(process.stderr, 'write', message => { diagnostics.push(message); return true; });
  try {
    const tokenFile = join(dir, 'token');
    await writeFile(tokenFile, 'test-token-1234567890');
    const gateway = new OfficeGateway(() => {});
    gateway.update(tree());
    const previous = gateway.agents;
    let calls = 0;
    await runPolling({ gateway, intervalMs: 1000, signal: controller.signal,
      url: 'http://localhost/v1/cortados/office', tokenFile,
      fetchImpl: async url => {
        calls++;
        assert.equal(url.pathname, '/v1/cortados/office/tree');   // the rooms, never the flat view as a fallback
        assert.equal(gateway.agents, previous);
        if (calls === 1) return { ok: false }; // authentication/HTTP failure (an older provider: 404)
        if (calls === 2) throw new Error('SECRET token path URL'); // network failure
        if (calls === 3) return { ok: true, json: async () => {
          const bad = tree(); bad.orchestrators.items[0].tasks = [{}]; return bad;
        } };
        controller.abort();
        const recovered = tree(); recovered.orchestrators.items[0].tasks[0].status = 'waiting';
        return { ok: true, json: async () => recovered };
      } });
    assert.equal(calls, 4);
    assert.equal(diagnostics.length, 3);
    for (const message of diagnostics) {
      assert.equal(message, 'Office tree unavailable; last successful roster retained (may be stale)\n');
      assert.ok(!message.includes('SECRET'));
    }
    assert.equal(gateway.agents[1].office.status, 'idle');
    assert.notEqual(gateway.agents, previous);
  } finally { controller.abort(); await rm(dir, { recursive: true, force: true }); }
});

test('poll interval exact boundaries and invalid reporter', async () => {
  const controller = new AbortController(); controller.abort();
  for (const intervalMs of [1000, 3600000]) {
    await runPolling({ signal: controller.signal, intervalMs });
  }
  for (const intervalMs of [999, 3600001, NaN, Infinity, '1000']) {
    await assert.rejects(runPolling({ signal: controller.signal, intervalMs }), /interval/);
  }
  await assert.rejects(runPolling({ signal: controller.signal, onError: null }), /reporter/);
});

test('poll loop cancellation and interval validation', async () => {
  const controller = new AbortController(); controller.abort();
  await runPolling({ gateway: new OfficeGateway(() => {}), signal: controller.signal });
  await assert.rejects(runPolling({ intervalMs: 0 }), /interval/);
});
