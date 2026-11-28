// Office round 2 (owner decisions 2026-11-27): rooms named after their orchestrator's Claude Code session, children
// "<orchestrator name> › <task title>", a stable colour per room, and the server room (racks per host and the S1
// console) from GET /v1/cortados/office/servers. The bridge stays read-only: the servers view is one more exact GET.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { mapTree, mapServers, roomColor, roomHash, ROOM_COLORS, NEUTRAL_ROOM_COLOR, CHILD_SEPARATOR, SERVERS_KEY,
  SERVERS_PATH, POLL_PATHS, pollOffice, pollServers, runPolling, OfficeGateway } from './adapter.mjs';
import { OfficeHub, rateCap, RATE_BASE_FRAMES, RATE_FRAMES_PER_PERSON, RATE_MAX_FRAMES } from './bridge.mjs';
import { avatarProfile, ROOM_TROUSERS } from './sprites.mjs';

const HERE = new URL('.', import.meta.url);
const SAMPLE = JSON.parse(await readFile(new URL('fixtures/sample-tree.json', HERE), 'utf8'));
const SERVERS = JSON.parse(await readFile(new URL('fixtures/sample-servers.json', HERE), 'utf8'));
const MINE = 'aaaaaaaaaaaaaaa1';
const OTHER = 'bbbbbbbbbbbbbbb2';
const ROOT = 't20261111090000-a0000001';
const sample = () => structuredClone(SAMPLE);
const servers = () => structuredClone(SERVERS);
const named = (name = 'Office round 2') => {
  const t = sample();
  t.orchestrators.items.find(g => g.origin === MINE).name = name;
  return t;
};
const by = agents => Object.fromEntries(agents.map(a => [a.id, a]));

// ---- names ----
test('a room is named after its session; the plate keeps project, model and host', () => {
  const a = by(mapTree(named()));
  const orch = a[`office-${MINE}-orchestrator`];
  assert.equal(orch.name, 'Office round 2');
  assert.equal(orch.office.groupLabel, 'Office round 2');
  assert.equal(orch.role, 'CortadOS-Router · claude-opus-5 · Pro');
});

test('unnamed: the project as before, and the role is model and host only', () => {
  const a = by(mapTree(sample()));
  assert.equal(a[`office-${MINE}-orchestrator`].name, 'CortadOS-Router');
  assert.equal(a[`office-${MINE}-orchestrator`].role, 'claude-opus-5 · Pro');
  assert.equal(a[`office-${OTHER}-orchestrator`].name, 'Grounds');
});

test('children are "<orchestrator name> › <task title>"; the unattributed room has no prefix', () => {
  const agents = mapTree(named());
  const a = by(agents);
  assert.equal(a[`office-${MINE}-task-${ROOT}`].name, 'Office round 2' + CHILD_SEPARATOR + 'Build the tree');
  assert.equal(a[`office-${MINE}-sub-${ROOT}-c-00000000000b`].name,
    'Office round 2' + CHILD_SEPARATOR + 'Helper · Build the tree');
  assert.equal(a[`office-${MINE}-routed-mine-sub-00001`].name.startsWith('Office round 2' + CHILD_SEPARATOR), true);
  for (const p of agents.filter(x => x.office.group === MINE && x.office.kind !== 'orchestrator')) {
    assert.ok(p.name.startsWith('Office round 2' + CHILD_SEPARATOR), p.name);
  }
  for (const p of agents.filter(x => x.office.group === 'unattributed' || x.office.group === 'lobby')) {
    assert.ok(!p.name.includes(CHILD_SEPARATOR), p.name);
  }
  // the bot's tasks carry its fixed label
  for (const p of agents.filter(x => x.office.group === 'grokbot' && x.office.kind === 'task')) {
    assert.ok(p.name.startsWith('Grok Bot' + CHILD_SEPARATOR), p.name);
  }
});

test('long names are cut: the orchestrator part to 24, the whole line to 64; never a raw non-string', () => {
  const long = 'An extremely long session name that goes on';
  const a = by(mapTree(named(long)));
  const task = a[`office-${MINE}-task-${ROOT}`].name;
  assert.ok(task.length <= 64, task);
  assert.ok(task.startsWith(long.slice(0, 23) + '…' + CHILD_SEPARATOR), task);
  assert.equal(a[`office-${MINE}-orchestrator`].name, long);
  const bad = sample();
  bad.orchestrators.items[0].name = 7;
  assert.throws(() => mapTree(bad), /invalid office tree group/);
  // a bot or unattributed group never takes a name from the tree
  const bot = sample();
  bot.orchestrators.items.find(g => g.kind === 'bot').name = 'Not the bot';
  assert.equal(by(mapTree(bot))['office-grokbot-orchestrator'].name, 'Grok Bot');
});

test('a bot origin named like the server room is refused, never a second server room', () => {
  const t = sample();
  t.orchestrators.items.find(g => g.kind === 'bot').origin = SERVERS_KEY;
  assert.throws(() => mapTree(t), /invalid office tree group/);
});

// ---- room colours ----
test('each room has a stable colour from its key; its people wear it; neutral rooms are grey', () => {
  const agents = mapTree(named());
  for (const a of agents) {
    assert.equal(a.office.roomColor, roomColor(a.office.group), a.id);
    assert.equal(a.identity.avatarProfile.clothing.bottomColor, a.office.roomColor, a.id);
  }
  assert.equal(roomColor(MINE), ROOM_COLORS[roomHash(MINE) % ROOM_COLORS.length]);
  for (const k of ['unattributed', 'lobby', SERVERS_KEY]) assert.equal(roomColor(k), NEUTRAL_ROOM_COLOR);
  // FNV-1a 32-bit reference values: cortadosRooms.ts computes the same (rooms.test.mjs checks the two agree)
  assert.equal(roomHash(''), 0x811c9dc5);
  assert.equal(roomHash('a'), 0xe40c292c);
  assert.deepEqual([...ROOM_TROUSERS].sort(), [...ROOM_COLORS, NEUTRAL_ROOM_COLOR].sort());
  // exactly Claw3D 51337f7's clothing colours (profile.ts AGENT_AVATAR_CLOTHING_COLOR_OPTIONS): any other is replaced
  assert.deepEqual([...ROOM_TROUSERS].sort(), ['#2d3748', '#7090ff', '#34d399', '#f59e0b', '#f43f5e', '#8b5cf6',
    '#f5f5f4', '#64748b'].sort());
  // an unknown colour is never put on a figure
  assert.equal(avatarProfile('claude', 'worker', 'x', { roomColor: 'red; x' }).clothing.bottomColor, '#64748b');
});

// ---- the server room: data, not people (office round 2 fix, owner 2026-11-27) ----
test('the server room is not people: a rack per host and the S1 console, numbers and fixed words only', () => {
  const agents = mapTree(sample());
  assert.ok(!agents.some(a => a.office.group === SERVERS_KEY || a.id.startsWith('office-servers-')));
  const room = mapServers(servers());
  assert.deepEqual(room.racks.map(r => r.host), ['air', 'studio', 'pro', 'pc']);
  const r = Object.fromEntries(room.racks.map(x => [x.host, x]));
  assert.deepEqual(r.air, { host: 'air', name: 'Air', state: 'up', note: 'up · provider' });
  assert.equal(r.studio.note, 'up · 8 s ago');
  assert.deepEqual([r.pro.state, r.pro.note], ['unknown', 'no heartbeat']);
  assert.deepEqual([r.pc.state, r.pc.note], ['down', 'down · seen 30 min ago']);
  assert.deepEqual(room.s1, { name: 'S1 · s1-qwen3-4b-sft-v7', state: 'up', alert: false, working: true,
    note: 'up · p50 181 ms · p90 420 ms · 90 scored/h' });
  const keys = new Set(['host', 'name', 'state', 'note']);
  for (const x of room.racks) assert.deepEqual(new Set(Object.keys(x)), keys);
});

test('S1 down or degraded is an alert; nothing scored is not working', () => {
  const s = servers();
  s.s1.degraded = true;
  assert.equal(mapServers(s).s1.alert, true);
  assert.ok(mapServers(s).s1.note.endsWith(' · degraded'));
  assert.equal(mapServers(s).s1.working, false);
  const d = servers();
  d.s1.status = 'down';
  assert.deepEqual([mapServers(d).s1.alert, mapServers(d).s1.state], [true, 'down']);
  const q = servers();
  q.s1.scored_1h = 0;
  assert.deepEqual([mapServers(q).s1.alert, mapServers(q).s1.working], [false, false]);
});

test('a malformed servers view is refused whole', () => {
  const cases = [
    s => { s.version = 2; }, s => { s.hosts.items[0].state = 'busy'; }, s => { s.hosts.items[0].name = 'Forests-Mac'; },
    s => { s.hosts.items.push(structuredClone(s.hosts.items[0])); }, s => { s.s1.p50_ms = -1; },
    s => { s.s1.p50_ms = '181'; }, s => { s.s1.model = 7; }, s => { s.s1.degraded = 'no'; },
    s => { s.hosts.items = Array.from({ length: 9 }, (_, i) => ({ name: 'h' + i, state: 'up', as_of: null, provider: false })); },
  ];
  for (const change of cases) {
    const s = servers();
    change(s);
    assert.throws(() => mapServers(s), /invalid office servers/, String(change));
    const g = new OfficeGateway(() => {});
    assert.throws(() => g.update(sample(), s), /invalid office servers/, String(change));
    assert.deepEqual(g.agents, [], 'nothing replaced');
  }
});

test('the bridge sends the server room in its hello and in every office.snapshot, never as people', () => {
  const frames = [];
  const g = new OfficeGateway(f => frames.push(f));
  g.update(sample(), servers());                 // before connect: kept for the hello
  g.request({ type: 'req', id: 'c', method: 'connect' });
  const hello = frames.find(f => f.type === 'res' && f.id === 'c').payload;
  assert.deepEqual(hello.snapshot.cortadosServers, mapServers(servers()));
  assert.ok(!hello.snapshot.health.agents.some(a => a.id.startsWith('office-servers-')));
  g.update(sample(), servers());
  const snap = frames.filter(f => f.event === 'office.snapshot').at(-1).payload;
  assert.deepEqual(snap.servers, mapServers(servers()));
  assert.ok(!snap.agents.some(a => a.office.group === SERVERS_KEY));
  // no servers view yet (an older provider): no server room, said as null
  const h = new OfficeGateway(f => frames.push(f));
  h.request({ type: 'req', id: 'd', method: 'connect' });
  assert.equal(frames.find(f => f.id === 'd').payload.snapshot.cortadosServers, null);
  h.update(sample());
  assert.equal(frames.filter(f => f.event === 'office.snapshot').at(-1).payload.servers, null);
});

// ---- the frame budget (office round 2 fix: "ROUTER • DISCONNECTED" with ~20 people) ----
test('sessions.list lists no sessions: the Office has none, so Studio fetches no per-person history', () => {
  // Measured 2026-11-27 (README "Frame budget"): listing one session per person made Studio's per-person session
  // inference fetch sessions.preview and chat.history for every listed session (Claw3D 51337f7 OfficeScreen
  // inferRunningFromAgentSessions; the bridge ignored its agentId) and its history bootstrap one chat.history per
  // person: ~265 browser frames per connect for 20 people, over the Studio proxy's 120-frame burst.
  const frames = [];
  const g = new OfficeGateway(f => frames.push(f));
  g.request({ type: 'req', id: 'c', method: 'connect' });
  g.update(sample());
  for (const params of [{}, { agentId: g.agents[0].id, includeUnknown: true, limit: 8 },
    { agentId: g.agents[0].id, search: 'agent:' + g.agents[0].id + ':main', limit: 4 }]) {
    g.request({ type: 'req', id: 's', method: 'sessions.list', params });
    assert.deepEqual(frames.at(-1).payload, { sessions: [] });
  }
});

test('the bridge rate cap grows with the roster, within a fixed bound', () => {
  assert.equal(rateCap(0), RATE_BASE_FRAMES);
  assert.equal(rateCap(20), RATE_BASE_FRAMES + 20 * RATE_FRAMES_PER_PERSON);
  assert.equal(rateCap(10_000), RATE_MAX_FRAMES);
  for (const bad of [-1, 1.5, NaN, undefined]) assert.equal(rateCap(bad), RATE_BASE_FRAMES);
});

test('presence goes out only when it changes: Studio reloads its whole fleet on each one', () => {
  const frames = [];
  const g = new OfficeGateway(f => frames.push(f));
  g.request({ type: 'req', id: 'c', method: 'connect' });
  const presences = () => frames.filter(f => f.event === 'presence').length;
  g.update(sample());
  assert.equal(presences(), 1);
  const later = sample();
  later.generated_at += 5;
  g.update(later);
  g.update(later);
  assert.equal(presences(), 1, 'same people, same names: no presence');
  assert.equal(frames.filter(f => f.event === 'office.snapshot').length, 3, 'the snapshot still goes out every poll');
  g.update(named('Renamed'));
  assert.equal(presences(), 2, 'a name changed');
  const fewer = named('Renamed');
  fewer.lobby.items = [];
  g.update(fewer);
  assert.equal(presences(), 3, 'people left');
});

test('after Studio reloads its fleet (agents.list), the next two polls announce working and failed people again', () => {
  const frames = [];
  const g = new OfficeGateway(f => frames.push(f));
  g.request({ type: 'req', id: 'c', method: 'connect' });
  const t = sample();
  t.orchestrators.items.find(x => x.origin === MINE).tasks[0].status = 'running';
  g.update(t);
  const starts = () => frames.filter(f => f.event === 'agent' && f.payload.data.phase === 'start');
  const working = g.agents.filter(a => a.office.status === 'working').map(a => a.id).sort();
  const errors = g.agents.filter(a => a.office.status === 'error').map(a => a.id).sort();
  assert.ok(working.length > 0);
  const first = starts().length;
  g.update(t);
  assert.equal(starts().length, first, 'no reload: nothing announced again');
  g.request({ type: 'req', id: 'l', method: 'agents.list', params: {} });
  for (let poll = 0; poll < 2; poll += 1) {
    const before = frames.length;
    g.update(t);
    const again = frames.slice(before).filter(f => f.event === 'agent' && f.payload.data.phase === 'start');
    assert.deepEqual([...new Set(again.map(f => f.payload.agentId))].sort(), [...working, ...errors].sort());
    // the same run for the working, so Studio keeps one run per person
    for (const f of again.filter(f => working.includes(f.payload.agentId))) {
      assert.equal(f.payload.runId, g.runs.get(f.payload.agentId));
    }
  }
  const before = frames.length;
  g.update(t);
  assert.equal(frames.slice(before).filter(f => f.event === 'agent').length, 0, 'two polls, then quiet');
});

// ---- polling: one more exact GET, and the tree still decides the roster ----
test('the bridge polls only the exact paths, GET only, and the servers path is one of them', async () => {
  assert.deepEqual([...POLL_PATHS], ['/v1/cortados/office', '/v1/cortados/office/tree', SERVERS_PATH]);
  const seen = [];
  const fetchImpl = async (url, init) => { seen.push([String(url), init.method, init.redirect]);
    return { ok: true, json: async () => servers() }; };
  const dir = await mkdtemp(join(tmpdir(), 'office-r2-'));
  await writeFile(join(dir, 't'), 'office-token-0123456789');
  const tok = { tokenFile: join(dir, 't') };
  await pollServers({ url: 'http://127.0.0.1:8432/v1/cortados/office', fetchImpl, ...tok });
  assert.deepEqual(seen, [['http://127.0.0.1:8432/v1/cortados/office/servers', 'GET', 'error']]);
  await assert.rejects(pollOffice({ url: 'http://127.0.0.1:8432/v1/cortados/office', fetchImpl, path: '/v1/cortados/live',
    ...tok }), /invalid office URL/);
  await assert.rejects(pollServers({ url: 'http://127.0.0.1:8432/v1/cortados/office/tree', fetchImpl, ...tok }),
    /invalid office URL/);
  await rm(dir, { recursive: true, force: true });
});

test('the default poll asks for the tree and the server room, both GET, nothing else', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'office-r2-'));
  await writeFile(join(dir, 't'), 'office-token-0123456789');
  const controller = new AbortController();
  const seen = [];
  const gateway = new OfficeGateway(() => {});
  await runPolling({ gateway, intervalMs: 1000, signal: controller.signal, onError: () => {},
    url: 'http://127.0.0.1:8432/v1/cortados/office', tokenFile: join(dir, 't'),
    fetchImpl: async (url, init) => {
      seen.push([url.pathname, init.method]);
      if (url.pathname.endsWith('/tree')) controller.abort();
      return { ok: true, json: async () => (url.pathname.endsWith('/servers') ? servers() : sample()) };
    } });
  assert.deepEqual(seen, [[SERVERS_PATH, 'GET'], ['/v1/cortados/office/tree', 'GET']]);
  assert.deepEqual(gateway.servers, mapServers(servers()));
  await rm(dir, { recursive: true, force: true });
});

test('a failing servers poll keeps the rooms and the last good server room', async () => {
  const frames = [];
  const gateway = new OfficeGateway(f => frames.push(f));
  const controller = new AbortController();
  const errors = [];
  let round = 0;
  const poll = async () => sample();
  const pollRoom = async () => {
    round += 1;
    if (round === 2) throw new Error('403');
    if (round === 3) { controller.abort(); return { version: 9 }; }
    return servers();
  };
  const updates = [];
  const spy = { update: (t, s) => { updates.push(s); gateway.update(t, s); } };
  await runPolling({ gateway: spy, intervalMs: 1000, signal: controller.signal, onError: m => errors.push(m), poll,
    pollRoom }).catch(() => {});
  assert.ok(updates.length >= 2);
  assert.deepEqual(updates[0], servers());
  assert.deepEqual(updates[1], servers());                 // round 2 failed: the last good room kept
  assert.ok(errors.some(m => /server room unavailable/.test(m)));
  assert.deepEqual(gateway.servers, mapServers(servers()));
  assert.ok(gateway.agents.some(a => a.office.group === MINE));
});

test('the hub validates both and replays both to a new client', () => {
  const hub = new OfficeHub();
  assert.throws(() => hub.update(sample(), { version: 2 }));
  assert.equal(hub.last, null);
  hub.update(sample(), servers());
  assert.deepEqual(hub.lastServers, servers());
  const g = new OfficeGateway(() => {});
  g.update(hub.last, hub.lastServers);
  assert.deepEqual(g.servers, mapServers(servers()));
});
