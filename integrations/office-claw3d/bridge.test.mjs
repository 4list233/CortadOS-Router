// Bridge tests. The only "network" is an ephemeral 127.0.0.1 port opened by the test and a
// raw-socket fake of Claw3D Studio's proxy. No provider, no Claw3D, no real token.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { randomBytes } from 'node:crypto';
import { mkdtemp, writeFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CloseError, FrameReader, MAX_FRAME_BYTES, OfficeHub, acceptKey, checkSecrets, checkUpgrade,
  createBridge, defaultOrigins, encodeFrame, readSecret, sameSecret } from './bridge.mjs';

const SECRET = 'bridge-secret-0123456789abcdef';
// one room: an orchestrator and its running task (the shape of GET /v1/cortados/office/tree)
const taskNode = (id, over = {}) => ({ task_id: id, title: 'Summarise', state: 'running', status: 'running', phase: null,
  tool: 'Read', host: 'air', provider: 'local', lane: 'local-pro', local_backend: null, model: 'qwen3:27b',
  started_at: 1, ended_at: null, elapsed_s: 1, parent_task_id: null, origin_via: null, sub_sessions_hidden: 0,
  sub_sessions: [], tasks: [], ...over });
const view = () => ({ version: 1, generated_at: 1_800_000_000, orchestrators: { status: 'ok', errors: [], items: [{
  origin: '0123456789abcdef', kind: 'session', project: 'Router', device: 'pro', status: 'working',
  model: 'claude-opus-5-5', tool: null, client: null, client_source: null,
  tasks: [taskNode('t20261124100000-a0000001')], routed: [] }] } });

function maskedFrame(opcode, payload, { fin = true, mask = true, rsv = 0 } = {}) {
  const body = Buffer.from(payload);
  const key = randomBytes(4);
  const len = body.length;
  const head = len < 126 ? Buffer.from([0, len]) : len < 65536 ? Buffer.alloc(4) : Buffer.alloc(10);
  if (len >= 126 && len < 65536) { head[1] = 126; head.writeUInt16BE(len, 2); }
  if (len >= 65536) { head[1] = 127; head.writeBigUInt64BE(BigInt(len), 2); }
  head[0] = (fin ? 0x80 : 0) | rsv | opcode;
  if (!mask) return Buffer.concat([head, body]);
  head[1] |= 0x80;
  const masked = Buffer.from(body.map((b, i) => b ^ key[i & 3]));
  return Buffer.concat([head, key, masked]);
}

test('upgrade checks: loopback peer, exact path, local Host, allowlisted Origin, valid key', () => {
  const port = 18789, origins = defaultOrigins(port);
  const ok = { method: 'GET', url: '/', socket: { remoteAddress: '127.0.0.1' }, headers: {
    host: 'localhost:18789', origin: 'http://localhost:18789', upgrade: 'websocket',
    'sec-websocket-version': '13', 'sec-websocket-key': 'dGhlIHNhbXBsZSBub25jZQ==' } };
  const v = patch => checkUpgrade({ ...ok, ...patch, headers: { ...ok.headers, ...patch.headers } }, { port, origins });
  assert.equal(v({}), null);
  assert.equal(v({ socket: { remoteAddress: '::1' } }), null);
  assert.equal(v({ socket: { remoteAddress: '::ffff:127.0.0.1' } }), null);
  assert.equal(v({ socket: { remoteAddress: '192.168.1.5' } }), 403);
  assert.equal(v({ socket: { remoteAddress: '100.64.0.2' } }), 403);
  assert.equal(v({ url: '/api/gateway/ws' }), 404);
  assert.equal(v({ url: '/?token=x' }), 404);
  assert.equal(v({ method: 'POST' }), 404);
  assert.equal(v({ headers: { host: 'evil.example:18789' } }), 403);     // DNS rebinding
  assert.equal(v({ headers: { host: 'localhost:3000' } }), 403);
  assert.equal(v({ headers: { origin: 'http://localhost:3000' } }), 403); // the browser's own origin
  assert.equal(v({ headers: { origin: 'http://evil.example' } }), 403);
  assert.equal(v({ headers: { origin: undefined } }), 403);
  assert.equal(v({ headers: { upgrade: 'h2c' } }), 426);
  assert.equal(v({ headers: { 'sec-websocket-version': '8' } }), 426);
  assert.equal(v({ headers: { 'sec-websocket-key': 'short' } }), 400);
  assert.equal(acceptKey('dGhlIHNhbXBsZSBub25jZQ=='), 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='); // RFC 6455 example
});

test('frame reader: masked text across chunks, control frames, and every refusal', () => {
  const r = new FrameReader();
  const f = maskedFrame(1, 'hello');
  assert.deepEqual(r.push(f.subarray(0, 3)), []);
  const [got] = r.push(f.subarray(3));
  assert.equal(got.opcode, 1); assert.equal(got.payload.toString(), 'hello');
  const long = 'x'.repeat(70000);
  assert.equal(new FrameReader(100000).push(maskedFrame(1, long))[0].payload.length, 70000);
  assert.equal(new FrameReader().push(maskedFrame(1, 'y'.repeat(300)))[0].payload.length, 300);
  assert.equal(new FrameReader().push(maskedFrame(9, 'p'))[0].opcode, 9);
  const code = (buf, max) => { try { new FrameReader(max).push(buf); return null; } catch (e) { assert.ok(e instanceof CloseError); return e.code; } };
  assert.equal(code(maskedFrame(1, 'a', { mask: false })), 1002);
  assert.equal(code(maskedFrame(2, 'a')), 1003);                 // binary
  assert.equal(code(maskedFrame(0, 'a')), 1003);                 // continuation
  assert.equal(code(maskedFrame(1, 'a', { fin: false })), 1003); // fragmented
  assert.equal(code(maskedFrame(1, 'a', { rsv: 0x40 })), 1002);  // compression bit without negotiation
  assert.equal(code(maskedFrame(1, 'z'.repeat(MAX_FRAME_BYTES + 1))), 1009);
  const huge = Buffer.from([0x81, 0xff, 0x7f, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff]);
  assert.equal(code(huge), 1009);                                // 64-bit length refused before buffering
  assert.equal(code(maskedFrame(9, 'q'.repeat(126))), 1002);     // control frame > 125
});

test('server frames encode all three length forms', () => {
  for (const n of [5, 300, 70000]) {
    const f = encodeFrame(1, 'a'.repeat(n));
    assert.equal(f[0], 0x81);
    const len = (f[1] & 0x7f) === 126 ? f.readUInt16BE(2) : (f[1] & 0x7f) === 127 ? Number(f.readBigUInt64BE(2)) : f[1];
    assert.equal(len, n);
    assert.equal(f[1] & 0x80, 0); // never masked
  }
});

test('secrets: 0600 files only, length rules, constant-time compare, never equal to the office token', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'office-bridge-'));
  try {
    const a = join(dir, 'a'), b = join(dir, 'b');
    await writeFile(a, SECRET + '\n', { mode: 0o600 });
    await writeFile(b, 'office-read-token-abcdef123456', { mode: 0o600 });
    assert.equal(await readSecret(a), SECRET);
    await checkSecrets(b, a);
    await assert.rejects(checkSecrets(a, a), /must differ/);
    await chmod(a, 0o644);
    await assert.rejects(readSecret(a), /0600/);
    await writeFile(b, 'short', { mode: 0o600 });
    await assert.rejects(readSecret(b), /invalid secret/);
    await assert.rejects(readSecret(join(dir, 'missing')));
    assert.equal(sameSecret('abc', 'abc'), true);
    assert.equal(sameSecret('abc', 'abd'), false);
    assert.equal(sameSecret(undefined, 'abc'), false);
    assert.equal(sameSecret({ toString: () => 'abc' }, 'abc'), false);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('bridge refuses non-loopback binds and wildcard origins', async () => {
  assert.throws(() => createBridge({}), /token file/);
  assert.throws(() => createBridge({ bridgeTokenFile: '/x', origins: ['*'] }), /explicit origins/);
  assert.throws(() => createBridge({ bridgeTokenFile: '/x', origins: [] }), /explicit origins/);
  const bridge = createBridge({ bridgeTokenFile: '/x', port: 0 });
  for (const host of ['0.0.0.0', '::', '192.168.1.5', 'localhost', '100.64.0.1']) {
    assert.throws(() => bridge.listen(host), /127\.0\.0\.1 or ::1/, host);
  }
});

test('hub validates once and leaves every client untouched on a bad snapshot', () => {
  const hub = new OfficeHub();
  const seen = [];
  hub.gateways.add({ update: v => seen.push(v) });
  hub.gateways.add({ update: () => { throw new Error('client gone'); } });
  hub.update(view());
  assert.equal(seen.length, 1);
  assert.throws(() => hub.update({ version: 2 }));
  assert.equal(seen.length, 1);
  assert.equal(hub.last.version, 1);
});

// ---- end to end over loopback with a fake Studio proxy ----
async function startBridge(t, options = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'office-bridge-'));
  const tokenFile = join(dir, 'bridge-token');
  await writeFile(tokenFile, SECRET, { mode: 0o600 });
  const logs = [];
  const probe = net.createServer();
  await new Promise(r => probe.listen(0, '127.0.0.1', r));
  const port = probe.address().port;
  await new Promise(r => probe.close(r));
  const bridge = createBridge({ port, bridgeTokenFile: tokenFile, log: m => logs.push(m), ...options });
  await bridge.listen('127.0.0.1');
  t.after(async () => { await bridge.close(); await rm(dir, { recursive: true, force: true }); });
  return { bridge, port, logs };
}

function fakeStudio(port, headers = {}, { pipelined = null } = {}) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1');
    const key = randomBytes(16).toString('base64');
    const h = { Host: `localhost:${port}`, Origin: `http://localhost:${port}`, Upgrade: 'websocket',
      Connection: 'Upgrade', 'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': key, ...headers };
    let head = Buffer.alloc(0), upgraded = false, rest = Buffer.alloc(0);
    const frames = [], waiters = [];
    let closedWith = null;
    const deliver = () => {
      for (;;) {
        if (rest.length < 2) return;
        let len = rest[1] & 0x7f, off = 2;
        if (len === 126) { if (rest.length < 4) return; len = rest.readUInt16BE(2); off = 4; }
        else if (len === 127) { if (rest.length < 10) return; len = Number(rest.readBigUInt64BE(2)); off = 10; }
        if (rest.length < off + len) return;
        const opcode = rest[0] & 0x0f, payload = rest.subarray(off, off + len);
        rest = rest.subarray(off + len);
        if (opcode === 8) closedWith = payload.readUInt16BE(0);
        else frames.push(opcode === 1 ? JSON.parse(payload.toString()) : { opcode, payload: payload.toString() });
        waiters.splice(0).forEach(w => w());
      }
    };
    socket.on('data', chunk => {
      if (upgraded) { rest = Buffer.concat([rest, chunk]); return deliver(); }
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf('\r\n\r\n');
      if (end < 0) return;
      const status = Number(head.toString('latin1', 9, 12));
      if (status !== 101) { resolve({ status, socket }); socket.destroy(); return; }
      assert.match(head.toString('latin1', 0, end), new RegExp('Sec-WebSocket-Accept: ' + acceptKey(key).replace(/[+/]/g, '\\$&')));
      upgraded = true;
      rest = head.subarray(end + 4);
      const client = {
        status, socket, frames, get closedWith() { return closedWith; },
        send: obj => socket.write(maskedFrame(1, JSON.stringify(obj))),
        raw: buf => socket.write(buf),
        async next(pred = () => true, timeoutMs = 3000) {
          const start = Date.now();
          for (;;) {
            const i = frames.findIndex(pred);
            if (i >= 0) return frames.splice(i, 1)[0];
            if (Date.now() - start > timeoutMs) throw new Error('timed out waiting for a frame');
            await new Promise(r => { waiters.push(r); setTimeout(r, 50); });
          }
        },
        async closed(timeoutMs = 3000) {
          const start = Date.now();
          while (closedWith === null && !socket.destroyed && Date.now() - start < timeoutMs) await new Promise(r => setTimeout(r, 20));
          return closedWith;
        },
      };
      resolve(client);
      deliver();
    });
    socket.on('error', reject);
    const request = Buffer.from('GET / HTTP/1.1\r\n' + Object.entries(h).filter(([, v]) => v !== undefined)
      .map(([k, v]) => `${k}: ${v}`).join('\r\n') + '\r\n\r\n', 'latin1');
    // `pipelined`: frame bytes sent in the SAME write as the headers (Node passes them as the upgrade's `head`)
    socket.write(pipelined ? Buffer.concat([request, pipelined]) : request);
  });
}
const connect = (c, token = SECRET) => c.send({ type: 'req', id: 'c1', method: 'connect',
  params: { minProtocol: 3, maxProtocol: 3, client: { id: 'openclaw-control-ui' }, auth: { token } } });

test('e2e: wrong Origin / Host are refused before any WebSocket exists', async t => {
  const { port, bridge } = await startBridge(t);
  assert.equal((await fakeStudio(port, { Origin: 'http://localhost:3000' })).status, 403);
  assert.equal((await fakeStudio(port, { Origin: undefined })).status, 403);
  assert.equal((await fakeStudio(port, { Host: 'attacker.example' })).status, 403);
  assert.equal(bridge.clients.size, 0);
});

test('e2e: challenge, unauthenticated requests refused, wrong token closes 1008', async t => {
  const { port, logs } = await startBridge(t);
  const c = await fakeStudio(port);
  const challenge = await c.next(f => f.event === 'connect.challenge');
  assert.match(challenge.payload.nonce, /^[0-9a-f]{32}$/);
  c.send({ type: 'req', id: 'early', method: 'agents.list' });
  assert.equal((await c.next(f => f.id === 'early')).error.code, 'NOT_CONNECTED');
  connect(c, 'wrong-secret-0123456789abcdef');
  assert.equal((await c.next(f => f.id === 'c1')).error.code, 'UNAUTHORIZED');
  assert.equal(await c.closed(), 1008);
  assert.ok(logs.some(l => l.includes('authentication')));
  assert.ok(!logs.join('\n').includes(SECRET) && !logs.join('\n').includes('wrong-secret'));
});

test('e2e: authenticated Studio gets hello, roster with sprites, read-only refusals and live pushes', async t => {
  const { port, bridge } = await startBridge(t);
  bridge.hub.update(view());
  const c = await fakeStudio(port);
  connect(c);
  const hello = await c.next(f => f.id === 'c1');
  assert.equal(hello.ok, true);
  assert.equal(hello.payload.type, 'hello-ok');
  assert.deepEqual(hello.payload.auth.scopes, ['operator.read']);
  assert.equal(hello.payload.snapshot.health.agents.length, 2);
  c.send({ type: 'req', id: 'cfg', method: 'config.get', params: {} });
  assert.equal((await c.next(f => f.id === 'cfg')).payload.config.gateway.reload.mode, 'off');
  c.send({ type: 'req', id: 'list', method: 'agents.list', params: {} });
  const agents = (await c.next(f => f.id === 'list')).payload.agents;
  assert.deepEqual(agents.map(a => a.office.sprite), ['claude-suit', 'ollama']);
  assert.ok(agents.every(a => a.identity.avatarUrl.startsWith('data:image/png;base64,')));
  // the group's raw device ('pro' here; a real one can be '<owner>-macbook-pro') never reaches the browser
  assert.ok(agents.every(a => !('device' in a.office)), 'no office.device in agents.list');
  assert.ok(!JSON.stringify(agents).includes('"device"'));
  c.send({ type: 'req', id: 'm', method: 'chat.send', params: { message: 'hi' } });
  assert.equal((await c.next(f => f.id === 'm')).error.code, 'READ_ONLY');
  // a poll lands: presence (Studio's Office screen reloads its roster on it) and the extension snapshot
  const next = view();
  next.orchestrators.items[0].tasks.push(taskNode('t20261124100100-a0000002', { provider: 'codex', lane: 'codex',
    model: 'gpt-6.1', title: 'Review', tool: 'Bash' }));
  bridge.hub.update(next);
  await c.next(f => f.event === 'presence');
  const snap = await c.next(f => f.event === 'office.snapshot');
  assert.deepEqual(snap.payload.agents.map(a => a.office.characterFamily), ['claude', 'ollama', 'codex']);
  c.raw(maskedFrame(9, 'ping'));
  assert.equal((await c.next(f => f.opcode === 10)).payload, 'ping');
});

const connectFrame = (token = SECRET) => maskedFrame(1, JSON.stringify({ type: 'req', id: 'c1', method: 'connect',
  params: { minProtocol: 3, maxProtocol: 3, client: { id: 'openclaw-control-ui' }, auth: { token } } }));

test('e2e: a connect frame pipelined with the upgrade headers is served (Node\'s upgrade head)', async t => {
  const { port, bridge } = await startBridge(t, { connectDeadlineMs: 1500 });
  bridge.hub.update(view());
  const c = await fakeStudio(port, {}, { pipelined: connectFrame() });
  const hello = await c.next(f => f.id === 'c1');
  assert.equal(hello.ok, true);
  assert.equal(hello.payload.type, 'hello-ok');
  // still open after the deadline: authenticated, not timed out
  await new Promise(r => setTimeout(r, 1700));
  assert.equal(c.closedWith, null);
  c.send({ type: 'req', id: 'list', method: 'agents.list', params: {} });
  assert.equal((await c.next(f => f.id === 'list')).payload.agents.length, 2);
  // header + connect + a second request, all in one write: both answered, in order
  const d = await fakeStudio(port, {}, { pipelined: Buffer.concat([connectFrame(),
    maskedFrame(1, JSON.stringify({ type: 'req', id: 'cfg', method: 'config.get', params: {} }))]) });
  assert.equal((await d.next(f => f.id === 'c1')).ok, true);
  assert.equal((await d.next(f => f.id === 'cfg')).ok, true);
});

test('e2e: a pipelined head gets the same checks as any frame (wrong token, bad frame)', async t => {
  const { port } = await startBridge(t);
  const wrong = await fakeStudio(port, {}, { pipelined: connectFrame('wrong-secret-0123456789abcdef') });
  assert.equal((await wrong.next(f => f.id === 'c1')).error.code, 'UNAUTHORIZED');
  assert.equal(await wrong.closed(), 1008);
  const unmasked = await fakeStudio(port, {}, { pipelined: maskedFrame(1, 'x', { mask: false }) });
  assert.equal(await unmasked.closed(), 1002);
  // a frame split between the head and the next packet is reassembled
  const f = connectFrame();
  const split = await fakeStudio(port, {}, { pipelined: f.subarray(0, 5) });
  split.raw(f.subarray(5));
  assert.equal((await split.next(m => m.id === 'c1')).ok, true);
});

test('e2e: protocol abuse closes the socket with the right code', async t => {
  const { port } = await startBridge(t);
  const cases = [
    [maskedFrame(1, 'not json'), 1007],
    [maskedFrame(2, 'bin'), 1003],
    [maskedFrame(1, 'x', { mask: false }), 1002],
    [maskedFrame(1, 'z'.repeat(MAX_FRAME_BYTES + 10)), 1009],
  ];
  for (const [buf, code] of cases) {
    const c = await fakeStudio(port);
    connect(c);
    await c.next(f => f.id === 'c1');
    c.raw(buf);
    assert.equal(await c.closed(), code);
  }
});

test('e2e: a client that never authenticates is closed at the deadline', async t => {
  const { port } = await startBridge(t, { connectDeadlineMs: 100 });
  const c = await fakeStudio(port);
  assert.equal(await c.closed(), 1008);
});

test('e2e: plain HTTP gets 426 and no data', async t => {
  const { port } = await startBridge(t);
  const res = await fetch(`http://127.0.0.1:${port}/`);
  assert.equal(res.status, 426);
  assert.equal(await res.text(), 'WebSocket only\n');
});

// Regression (#77): the deploy starts `$ROOT/adapter/bridge.mjs` where `adapter` is a symlink to
// adapters/<commit>. Node resolves the main module's symlinks, so a URL-vs-argv[1] entry check skipped
// the main block (exit 0, no output) and launchd kept restarting the bridge. Run each entry through a
// symlinked directory with an empty env and require its main block's own refusal.
test('entry checks run the main block when started through a symlinked directory', async () => {
  const { spawnSync } = await import('node:child_process');
  const { copyFile, mkdir, symlink } = await import('node:fs/promises');
  const { fileURLToPath } = await import('node:url');
  const here = fileURLToPath(new URL('.', import.meta.url));
  const dir = await mkdtemp(join(tmpdir(), 'office-entry-'));
  try {
    const real = join(dir, 'adapters', 'commit');
    await mkdir(real, { recursive: true });
    for (const f of ['adapter.mjs', 'bridge.mjs', 'sprites.mjs']) await copyFile(join(here, f), join(real, f));
    await symlink(real, join(dir, 'adapter'));
    const run = (script, ...args) => spawnSync(process.execPath, [join(dir, 'adapter', script), ...args],
      { cwd: dir, env: {}, encoding: 'utf8', timeout: 20000 });
    const bridge = run('bridge.mjs');
    assert.match(bridge.stderr, /Office bridge stopped: token files not configured/);
    assert.equal(bridge.status, 1);
    const adapter = run('adapter.mjs');
    assert.match(adapter.stderr, /Diagnostic only/);
    assert.equal(adapter.status, 1);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
