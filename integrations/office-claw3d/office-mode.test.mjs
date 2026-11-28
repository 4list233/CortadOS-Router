// Office mode tests (claw3d/office-mode.cjs, #77 review fix round 1): Studio's server-side read-only boundary.
// The function's allowlist, then a real 127.0.0.1 http server on an ephemeral port wired exactly as
// claw3d/office-mode.patch wires Studio (login form -> access gate -> office mode -> Next; office mode first on
// upgrades), with stand-ins for Next and the gateway proxy that record what reached them. No Claw3D build, no
// network beyond loopback, no real token. With CORTADOS_CLAW3D_CHECKOUT set, the real access gate of Claw3D 51337f7
// is used and the route table is read from the checkout itself (every route and method it serves).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtemp, readFile, rm, readdir } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const HERE = fileURLToPath(new URL('.', import.meta.url));
const { createOfficeMode, allowHttp, allowUpgrade, EXACT, PREFIXES, WS_PATH } = require('./claw3d/office-mode.cjs');
const { createStudioLogin, COOKIE } = require('./claw3d/studio-login.cjs');
const CLAW3D_COMMIT = '51337f7f250459af61a7cf92b7b0015d59faed5c';
const CHECKOUT = process.env.CORTADOS_CLAW3D_CHECKOUT;
const TOKEN = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90';

// Every API route of Claw3D 51337f7 and the methods its route.ts exports (src/app/api/**/route.ts).
const CLAW3D_API = {
  '/api/files/[file]': ['GET'], '/api/files/upload': ['POST'], '/api/gateway/agent-state': ['POST', 'PUT'],
  '/api/gateway/media': ['GET'], '/api/gateway/skills/remove': ['POST'], '/api/health': ['GET'],
  '/api/office/browser-preview': ['GET'], '/api/office/call': ['POST'], '/api/office/github': ['GET', 'POST'],
  '/api/office/layout': ['GET', 'PUT'], '/api/office/presence': ['GET'], '/api/office/publish': ['PUT'],
  '/api/office/remote-handoff': ['POST'], '/api/office/remote-message': ['POST'], '/api/office': ['GET', 'PUT'],
  '/api/office/standup/config': ['GET', 'PUT'], '/api/office/standup/meeting': ['GET', 'PUT'],
  '/api/office/standup/run': ['POST'], '/api/office/text': ['POST'], '/api/office/voice/reply': ['POST'],
  '/api/office/voice/transcribe': ['POST'], '/api/path-suggestions': ['GET'], '/api/runtime/custom': ['POST'],
  '/api/studio': ['GET', 'PUT'], '/api/task-store': ['GET', 'PUT', 'DELETE'],
};
// The read-only Office's API needs, taken by exercising the page (office-mode.cjs header).
const ALLOWED_API_GET = ['/api/health', '/api/studio', '/api/task-store', '/api/office/standup/config',
  '/api/office/standup/meeting'];

// The review's list (P1): each must be refused for a logged-in browser.
const REVIEW_REFUSED = [
  ['PUT', '/api/studio'], ['POST', '/api/office/remote-message'], ['POST', '/api/office/remote-handoff'],
  ['POST', '/api/runtime/custom'], ['POST', '/api/office/voice/transcribe'], ['POST', '/api/office/voice/reply'],
  ['PUT', '/api/office/layout'], ['PUT', '/api/office/publish'], ['PUT', '/api/office'],
  ['PUT', '/api/office/standup/config'], ['PUT', '/api/office/standup/meeting'], ['POST', '/api/office/standup/run'],
  ['PUT', '/api/task-store'], ['DELETE', '/api/task-store'], ['POST', '/api/files/upload'],
  ['POST', '/api/gateway/agent-state'], ['PUT', '/api/gateway/agent-state'], ['POST', '/api/gateway/skills/remove'],
  ['POST', '/api/office/call'], ['POST', '/api/office/text'], ['POST', '/api/office/github'],
];
// GET routes the Office does not use, some of them reads of the machine or fetches out: refused too (fail closed).
const UNUSED_GETS = ['/api/path-suggestions?q=/', '/api/gateway/media?path=/etc/hosts', '/api/files/x.png',
  '/api/office/browser-preview?url=http://127.0.0.1:8432/', '/api/office/github', '/api/office/layout',
  '/api/office/presence', '/api/office'];
// Unknown or disguised targets: nothing is decoded or normalised, so each matches nothing.
const TRICKY_GETS = ['/api/studio/', '/API/studio', '/api/%73tudio', '/api//studio', '//api/studio',
  'http://127.0.0.1:3000/api/studio', '/api/studio%2F', '/api/nope', '/office/builder', '/agents',
  '/agents/a/settings', '/spotify/callback', '/_next/image?url=http://127.0.0.1:8432/', '/_next/static/../../api/studio',
  '/_next/static/%2e%2e/api/studio', '/_next/static//x.js', '/_next/static/', '/_next/static', '/office-assets/..',
  '/office-assets/a\\b', '/_next/webpack-hmr', '/next.svg', '/robots.txt', '', '*'];

test('every route on the allowlist carries its reason, methods and nothing writable on the API', () => {
  for (const [path, methods, reason] of EXACT) {
    assert.ok(typeof reason === 'string' && reason.length > 10, path);
    if (path.startsWith('/api/')) assert.deepEqual(methods, ['GET', 'HEAD'], path + ' is read-only');
  }
  for (const [prefix, reason] of PREFIXES) assert.ok(prefix.endsWith('/') && reason.length > 10, prefix);
  const nonRead = EXACT.filter(([, methods]) => methods.some(m => m !== 'GET' && m !== 'HEAD')).map(([p]) => p);
  assert.deepEqual(nonRead, ['/cortados-login'], 'the login form is the only non-GET route');
  assert.equal(WS_PATH, '/api/gateway/ws');
});

test('office round 2: the server room path is exact and GET only', () => {
  const entry = EXACT.find(([p]) => p === '/v1/cortados/office/servers');
  assert.ok(entry, 'listed');
  assert.deepEqual(entry[1], ['GET']);
  assert.equal(allowHttp('GET', '/v1/cortados/office/servers'), true);
  for (const m of ['HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
    assert.equal(allowHttp(m, '/v1/cortados/office/servers'), false, m);
  }
  for (const url of ['/v1/cortados/office/servers/', '/v1/cortados/office/tree', '/v1/cortados/office',
    '/v1/cortados/live', '/v1/cortados/office/%73ervers', '/V1/cortados/office/servers']) {
    assert.equal(allowHttp('GET', url), false, url);
  }
  const v1 = EXACT.filter(([p]) => p.startsWith('/v1/')).map(([p]) => p);
  assert.deepEqual(v1, ['/v1/cortados/office/servers'], 'no other provider path is listed');
});

test('the review list: each write, remote, runtime and voice route is refused', () => {
  for (const [method, path] of REVIEW_REFUSED) assert.equal(allowHttp(method, path), false, `${method} ${path}`);
});

test('every route and method Claw3D 51337f7 serves is refused unless it is one of the Office\'s five reads', () => {
  for (const [route, methods] of Object.entries(CLAW3D_API)) {
    const path = route.replace('[file]', 'x.png');
    for (const m of ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
      const want = ALLOWED_API_GET.includes(path) && (m === 'GET' || m === 'HEAD');
      assert.equal(allowHttp(m, path), want, `${m} ${path} (serves ${methods})`);
    }
  }
  for (const p of ALLOWED_API_GET) assert.ok(CLAW3D_API[p]?.includes('GET'), p + ' is a real GET route');
  for (const url of UNUSED_GETS) assert.equal(allowHttp('GET', url), false, url);
});

test('unknown, disguised and non-GET page requests are refused; the pages and assets the Office loads are not', () => {
  for (const url of TRICKY_GETS) assert.equal(allowHttp('GET', url), false, JSON.stringify(url));
  for (const url of [undefined, null, 42]) assert.equal(allowHttp('GET', url), false, String(url));
  for (const [m, url] of [['POST', '/office'], ['PUT', '/'], ['POST', '/_next/static/chunks/a.js'],
    ['OPTIONS', '/api/studio'], ['TRACE', '/api/health'], ['CONNECT', '/api/health'], ['', '/office'],
    ['PUT', '/cortados-login'], ['DELETE', '/cortados-login']]) {
    assert.equal(allowHttp(m, url), false, `${m} ${url}`);
  }
  for (const [m, url] of [['GET', '/'], ['GET', '/office'], ['HEAD', '/office'], ['get', '/office'],
    ['GET', '/office?x=1'], ['GET', '/favicon.ico?favicon.0b3bf435.ico'], ['GET', '/api/health'],
    ['GET', '/api/studio'], ['HEAD', '/api/studio'], ['GET', '/api/task-store'],
    ['GET', '/api/office/standup/config?gatewayUrl=ws%3A%2F%2Flocalhost%3A18789'], ['GET', '/api/office/standup/meeting'],
    ['GET', '/_next/static/chunks/0a1b-c_d.js'], ['GET', '/_next/static/media/x.woff2'],
    ['GET', '/office-assets/models/desk.glb'], ['GET', '/cortados-login'], ['POST', '/cortados-login']]) {
    assert.equal(allowHttp(m, url), true, `${m} ${url}`);
  }
});

test('upgrades: only the gateway proxy, exact path', () => {
  assert.equal(allowUpgrade('/api/gateway/ws'), true);
  assert.equal(allowUpgrade('/api/gateway/ws?x=1'), true);
  for (const url of ['/', '/_next/webpack-hmr', '/api/gateway/ws/', '/api/gateway/wss', '/API/gateway/ws',
    '/api/gateway/%77s', 'ws://127.0.0.1:3000/api/gateway/ws', '', undefined]) {
    assert.equal(allowUpgrade(url), false, String(url));
  }
});

// ---- the wiring, in a real server -----------------------------------------------------------------------
/** Claw3D's access gate: the real one from the checkout, else a stand-in with the same contract (cookie == token). */
function accessGateFor(token) {
  if (CHECKOUT) {
    const dir = join(CHECKOUT, '.cortados-office-mode-gate');
    const src = spawnSync('git', ['-C', CHECKOUT, 'show', CLAW3D_COMMIT + ':server/access-gate.js'], { encoding: 'utf8' });
    assert.equal(src.status, 0, String(src.stderr));
    const Module = require('node:module');
    const m = new Module(join(dir, 'access-gate.js'));
    m.paths = Module._nodeModulePaths(dir);
    m._compile(src.stdout, join(dir, 'access-gate.js'));
    return m.exports.createAccessGate({ token });
  }
  const ok = req => (req.headers.cookie || '').split(';').some(c => c.trim() === `${COOKIE}=${token}`);
  return {
    handleHttp(req, res) { if (ok(req)) return false; res.writeHead(401); res.end('Studio access token required.'); return true; },
    allowUpgrade: ok,
  };
}

async function startStudio(t) {
  const reached = { next: [], proxy: [] };
  const studioLogin = createStudioLogin({ token: TOKEN });
  const accessGate = accessGateFor(TOKEN);
  const officeMode = createOfficeMode();
  // as in office-mode.patch: login form, then the stock gate, then office mode, then Next
  const server = http.createServer((req, res) => {
    if (studioLogin.handle(req, res)) return;
    if (accessGate.handleHttp(req, res)) return;
    if (officeMode.handleHttp(req, res)) return;
    reached.next.push(`${req.method} ${req.url}`);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end('{"next":true}');
  });
  server.on('upgrade', (req, socket) => {
    if (officeMode.handleUpgrade(req, socket)) return;
    if (req.url.split('?')[0] === '/api/gateway/ws') {
      reached.proxy.push(req.url);
      socket.end('HTTP/1.1 418 Proxy Stand-in\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    reached.next.push('UPGRADE ' + req.url);       // Next's upgrade handler: must never be reached
    socket.destroy();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.closeAllConnections?.(); server.close(resolve); }));
  return { port: server.address().port, reached };
}

function request(port, method, path, { cookie, body, headers = {} } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path, agent: false,
      headers: { ...(cookie ? { cookie } : {}), ...(body ? { 'content-type': 'application/json' } : {}), ...headers } },
    res => { let data = ''; res.on('data', c => { data += c; }); res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data })); });
    req.on('error', reject);
    req.end(body);
  });
}

function rawUpgrade(port, path, cookie) {
  return new Promise((resolve, reject) => {
    const s = net.connect(port, '127.0.0.1', () => s.write(`GET ${path} HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n` +
      `Connection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n` +
      (cookie ? `Cookie: ${cookie}\r\n` : '') + '\r\n'));
    let data = '';
    s.on('data', c => { data += c; });
    s.on('close', () => resolve(data.split('\r\n', 1)[0]));
    s.on('error', reject);
  });
}

async function logIn(port) {
  const res = await request(port, 'POST', '/cortados-login', { body: 'token=' + TOKEN, headers: {
    'content-type': 'application/x-www-form-urlencoded', origin: `http://127.0.0.1:${port}`, host: `127.0.0.1:${port}` } });
  assert.equal(res.status, 303);
  return res.headers['set-cookie'][0].split(';', 1)[0];
}

test('server: a logged-in browser gets 403 on every refused route, and Next never sees the request', async t => {
  const { port, reached } = await startStudio(t);
  const cookie = await logIn(port);
  for (const [method, path] of REVIEW_REFUSED) {
    const res = await request(port, method, path, { cookie, body: '{"gateway":{"url":"ws://evil.example:1"}}' });
    assert.equal(res.status, 403, `${method} ${path}`);
    assert.equal(res.headers['content-type'], 'application/json');
    assert.match(JSON.parse(res.body).error, /read-only/);
  }
  for (const path of [...UNUSED_GETS, '/api/nope', '/office/builder', '/agents', '/_next/image?url=x', '/api/studio/']) {
    assert.equal((await request(port, 'GET', path, { cookie })).status, 403, path);
  }
  assert.equal((await request(port, 'POST', '/office', { cookie, headers: { 'next-action': 'x' } })).status, 403,
    'no server action on a page');
  assert.deepEqual(reached.next, [], 'nothing refused reached Next');
});

test('server: the Office\'s own requests still reach Next; logged out is still the gate\'s 401 first', async t => {
  const { port, reached } = await startStudio(t);
  const cookie = await logIn(port);
  const allowed = ['/', '/office', '/favicon.ico', '/api/health', '/api/studio', '/api/task-store',
    '/api/office/standup/config?gatewayUrl=ws%3A%2F%2Flocalhost%3A18789', '/api/office/standup/meeting',
    '/_next/static/chunks/main.js', '/office-assets/models/desk.glb'];
  for (const path of allowed) assert.equal((await request(port, 'GET', path, { cookie })).status, 200, path);
  assert.deepEqual(reached.next, allowed.map(p => 'GET ' + p));
  assert.equal((await request(port, 'GET', '/cortados-login', { cookie })).status, 200, 'the login form');
  for (const [m, p] of [['GET', '/api/studio'], ['PUT', '/api/studio'], ['GET', '/office']]) {
    assert.equal((await request(port, m, p)).status, 401, `${m} ${p} without the cookie`);
  }
  assert.equal(reached.next.length, allowed.length);
});

test('server: upgrades other than the gateway proxy are refused before Next; the proxy still needs the cookie', async t => {
  const { port, reached } = await startStudio(t);
  const cookie = await logIn(port);
  for (const path of ['/_next/webpack-hmr', '/', '/api/gateway/ws/', '/api/studio']) {
    assert.equal(await rawUpgrade(port, path, cookie), 'HTTP/1.1 403 Forbidden', path);
  }
  assert.equal(await rawUpgrade(port, '/api/gateway/ws', cookie), 'HTTP/1.1 418 Proxy Stand-in');
  assert.deepEqual(reached.proxy, ['/api/gateway/ws']);
  assert.deepEqual(reached.next, []);
});

// ---- the patch ---------------------------------------------------------------------------------------------
test('office-mode.patch: office mode after the gate on both servers, first on upgrades, and connect survives a refused save', async () => {
  const patch = await readFile(join(HERE, 'claw3d', 'office-mode.patch'), 'utf8');
  const files = [...patch.matchAll(/^\+\+\+ b\/(.+)$/gm)].map(m => m[1]);
  assert.deepEqual(files, ['server/index.js', 'src/lib/gateway/GatewayClient.ts']);
  assert.deepEqual(patch.split('\n').filter(l => l.startsWith('-') && !l.startsWith('---')).map(l => l.slice(1).trim()),
    ['await settingsCoordinator.flushPending();'], 'removes nothing but the one await it makes tolerant');
  const added = patch.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).map(l => l.slice(1).trim())
    .filter(l => !l.startsWith('//'));
  assert.deepEqual(added, [
    'const { createOfficeMode } = require("./cortados-office-mode");',
    'const officeMode = createOfficeMode();',
    'if (officeMode.handleUpgrade(req, socket)) return;',
    'if (officeMode.handleHttp(req, res)) return;',
    'if (officeMode.handleHttp(req, res)) return;',
    'await settingsCoordinator.flushPending().catch(() => undefined);',
  ]);
  // each handleHttp sits right after the access gate's line
  assert.equal((patch.match(/^ {11}if \(accessGate\.handleHttp\(req, res\)\) return;\n\+ {10}if \(officeMode\.handleHttp\(req, res\)\) return;$/gm) || []).length, 2);
  assert.match(patch, /^ {3}const handleServerUpgrade = \(req, socket, head\) => \{\n\+ {4}if \(officeMode\.handleUpgrade\(req, socket\)\) return;$/m);
});

test('the patched Studio (CORTADOS_CLAW3D_CHECKOUT): wired in order, modules load, every served route is covered', async t => {
  if (!CHECKOUT) return t.skip('CORTADOS_CLAW3D_CHECKOUT is not set: no Claw3D 51337f7 checkout');
  const dir = await mkdtemp(join(tmpdir(), 'claw3d-office-mode-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const tree = spawnSync('sh', ['-c', 'git -C "$1" archive "$2" | tar -x -C "$3"', 'sh', CHECKOUT, CLAW3D_COMMIT, dir]);
  assert.equal(tree.status, 0, String(tree.stderr));
  for (const p of ['avatar-profile-hint', 'studio-login', 'cortados-rooms', 'system-fonts', 'proxy-auth', 'cortados-brand',
    'office-mode']) {
    const r = spawnSync('git', ['apply', join(HERE, 'claw3d', p + '.patch')], { cwd: dir });
    assert.equal(r.status, 0, `${p}: ${r.stderr}`);
  }
  const index = readFileSync(join(dir, 'server/index.js'), 'utf8');
  for (const server of index.split(/\? https\.createServer|: http\.createServer/).slice(1)) {
    const order = ['studioLogin.handle', 'accessGate.handleHttp', 'officeMode.handleHttp', 'handle(req, res);']
      .map(s => server.indexOf(s));
    assert.ok(order.every((v, i) => v >= 0 && (i === 0 || v > order[i - 1])), 'login, gate, office mode, Next: ' + order);
  }
  const upgrade = index.slice(index.indexOf('const handleServerUpgrade'));
  assert.ok(upgrade.indexOf('officeMode.handleUpgrade') < upgrade.indexOf('proxy.handleUpgrade'));
  // the route table above is the checkout's own: every route.ts and the methods it exports
  const routes = {};
  const walk = async rel => {
    for (const e of await readdir(join(dir, rel), { withFileTypes: true })) {
      if (e.isDirectory()) await walk(join(rel, e.name));
      else if (e.name === 'route.ts') {
        const src = readFileSync(join(dir, rel, e.name), 'utf8');
        routes['/' + rel.slice('src/app/'.length)] = [...src.matchAll(/export (?:async )?(?:function|const) (GET|POST|PUT|PATCH|DELETE)\b/g)].map(m => m[1]);
      }
    }
  };
  await walk('src/app/api');
  assert.deepEqual(routes, CLAW3D_API);
  // the server-side upstream comes only from the settings file, which no allowed route writes
  assert.ok(existsSync(join(dir, 'server/studio-settings.js')));
  for (const p of ALLOWED_API_GET) {
    const src = readFileSync(join(dir, 'src/app', p, 'route.ts'), 'utf8');
    const get = src.slice(src.indexOf('export async function GET'));
    const end = get.indexOf('\nexport ', 1);
    assert.doesNotMatch(end === -1 ? get : get.slice(0, end), /applyStudioSettingsPatch|saveStudioSettings|writeFile|fetch\(/,
      p + ' GET writes and fetches nothing');
  }
});
