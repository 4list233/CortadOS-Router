import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mapOffice, characterFamily, OfficeGateway, pollOffice, runPolling } from './adapter.mjs';

const worker = { run_id: 'run-12345678', provider: 'codex', lane: 'codex', model: 'gpt',
  title: 'Review', status: 'working', tool: 'Read' };
const view = () => ({ version: 1, floors: [{ session: '0123456789abcdef',
  project: 'Router', device: 'pro', orchestrator: { model: 'claude', status: 'idle' },
  workers: [{ ...worker }] }], lobby: { workers: [] } });

for (const family of ['claude', 'codex', 'grok', 'ollama', 'local']) {
  test('character family ' + family, () => assert.equal(characterFamily(family), family));
}
test('unknown provider is neutral, not a vendor guess', () => {
  assert.equal(characterFamily(null), 'local');
  assert.equal(characterFamily('other'), 'local');
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
  gateway.update(view());
  for (const method of ['agents.list', 'sessions.list', 'status']) {
    gateway.request({ type: 'req', id: method, method });
    assert.equal(frames.at(-1).ok, true);
  }
  gateway.request({ type: 'req', id: 'deny', method: 'chat.send' });
  assert.equal(frames.at(-1).error.code, 'READ_ONLY');
});
test('working lifecycle does not duplicate start, ends on removal', () => {
  const { gateway, frames } = connected();
  gateway.update(view());
  gateway.update(view());
  const lifecycle = () => frames.filter(f => f.event === 'agent' && f.payload.stream === 'lifecycle');
  assert.equal(lifecycle().length, 1);
  assert.equal(lifecycle()[0].payload.data.phase, 'start');
  gateway.update({ version: 1, floors: [], lobby: { workers: [] } });
  assert.equal(lifecycle().at(-1).payload.data.phase, 'end');
  assert.deepEqual(frames.at(-1).payload.agents, []);
});
test('error terminal event and invalid snapshot retains good state', () => {
  const { gateway, frames } = connected();
  gateway.update(view());
  const input = view(); input.floors[0].workers[0].status = 'error';
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
  } finally { await rm(dir, { recursive: true, force: true }); }
});
test('poll loop cancellation and interval validation', async () => {
  const controller = new AbortController(); controller.abort();
  await runPolling({ gateway: new OfficeGateway(() => {}), signal: controller.signal });
  await assert.rejects(runPolling({ intervalMs: 0 }), /interval/);
});
