// Studio login form tests (claw3d/studio-login.cjs). Fake requests and responses only:
// no Claw3D, no server, no socket, no real token.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';

const require = createRequire(import.meta.url);
const { createStudioLogin, LOGIN_PATH, COOKIE, MAX_BODY_BYTES, MAX_FAILURES } = require('./claw3d/studio-login.cjs');

const TOKEN = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90';
const TAILNET = 'air.example-tailnet.ts.net:8446';

function fakeReq({ method = 'POST', url = LOGIN_PATH, headers = {}, body = null } = {}) {
  const req = new EventEmitter();
  Object.assign(req, { method, url, headers, socket: {}, destroyed: false, resume() {}, destroy() { this.destroyed = true; } });
  req.feed = () => {
    if (body !== null) req.emit('data', Buffer.from(body));
    req.emit('end');
  };
  return req;
}
function fakeRes() {
  return { status: null, headers: null, body: null,
    writeHead(status, headers) { this.status = status; this.headers = headers; },
    end(body) { this.body = String(body ?? ''); } };
}
const viaServe = (extra = {}) => ({ host: TAILNET, origin: 'https://' + TAILNET, 'x-forwarded-proto': 'https',
  'content-type': 'application/x-www-form-urlencoded', ...extra });
function post(login, body, headers = viaServe()) {
  const req = fakeReq({ headers, body });
  const res = fakeRes();
  assert.equal(login.handle(req, res), true);
  req.feed();
  return { req, res };
}

test('inert without a usable token: every request goes on to the stock gate', () => {
  for (const token of [undefined, '', '   ', 'short', 'has;semicolon-0123456789', 'x'.repeat(4097)]) {
    const login = createStudioLogin({ token });
    assert.equal(login.enabled, false);
    assert.equal(login.handle(fakeReq({ method: 'GET' }), fakeRes()), false, String(token));
  }
});

test('only /cortados-login is handled; every other path passes through', () => {
  const login = createStudioLogin({ token: TOKEN });
  for (const url of ['/', '/api/gateway/ws', '/cortados-login/', '/cortados-loginx', '/x/cortados-login']) {
    assert.equal(login.handle(fakeReq({ method: 'GET', url }), fakeRes()), false, url);
  }
  const res = fakeRes();
  assert.equal(login.handle(fakeReq({ method: 'GET', url: LOGIN_PATH + '?next=/' }), res), true);
  assert.equal(res.status, 200);
  assert.match(res.body, /type="password" name="token"/);
  assert.equal(res.headers['Cache-Control'], 'no-store');
  assert.match(res.headers['Content-Security-Policy'], /frame-ancestors 'none'/);
  assert.ok(!res.body.includes(TOKEN));
  const put = fakeRes();
  login.handle(fakeReq({ method: 'PUT' }), put);
  assert.equal(put.status, 405);
});

test('the right token over Tailscale Serve sets a Secure HttpOnly SameSite=Strict cookie and redirects', () => {
  const login = createStudioLogin({ token: TOKEN });
  const { res } = post(login, 'username=cortados-office&token=' + TOKEN);
  assert.equal(res.status, 303);
  assert.equal(res.headers.Location, '/');
  const cookie = res.headers['Set-Cookie'];
  assert.ok(cookie.startsWith(`${COOKIE}=${TOKEN}; `));
  for (const attr of ['Path=/', 'HttpOnly', 'SameSite=Strict', 'Secure', 'Max-Age=2592000']) assert.ok(cookie.includes(attr), attr);
  assert.ok(!res.body.includes(TOKEN));
});

test('plain http on the Air loopback: same cookie without Secure', () => {
  const login = createStudioLogin({ token: TOKEN });
  const { res } = post(login, 'token=' + TOKEN, { host: '127.0.0.1:3000', origin: 'http://127.0.0.1:3000',
    'content-type': 'application/x-www-form-urlencoded' });
  assert.equal(res.status, 303);
  assert.ok(!res.headers['Set-Cookie'].includes('Secure'));
});

test('X-Forwarded-Host is an accepted authority (Serve may rewrite Host)', () => {
  const login = createStudioLogin({ token: TOKEN });
  const { res } = post(login, 'token=' + TOKEN, viaServe({ host: '127.0.0.1:3000', 'x-forwarded-host': TAILNET }));
  assert.equal(res.status, 303);
});

test('cross-origin, missing Origin, scheme mismatch and non-form posts are refused before reading the body', () => {
  const login = createStudioLogin({ token: TOKEN });
  const cases = [
    [viaServe({ origin: 'https://evil.example' }), 403],
    [viaServe({ origin: undefined }), 403],
    [viaServe({ origin: 'http://' + TAILNET }), 403],           // came over https, Origin says http
    [viaServe({ origin: 'null' }), 403],
    [viaServe({ 'content-type': 'application/json' }), 415],
    [viaServe({ 'content-type': 'multipart/form-data; boundary=x' }), 415],
  ];
  for (const [headers, status] of cases) {
    const req = fakeReq({ headers, body: 'token=' + TOKEN });
    const res = fakeRes();
    assert.equal(login.handle(req, res), true);
    assert.equal(res.status, status, JSON.stringify(headers));
    assert.equal(res.headers['Set-Cookie'], undefined);
  }
});

test('a wrong token is 401 with no cookie; oversize bodies are 413', () => {
  const login = createStudioLogin({ token: TOKEN });
  const wrong = post(login, 'token=' + TOKEN.slice(0, -1) + 'f').res;
  assert.equal(wrong.status, 401);
  assert.equal(wrong.headers['Set-Cookie'], undefined);
  assert.equal(post(login, 'username=x').res.status, 401);          // no token field at all
  const big = post(login, 'token=' + 'a'.repeat(MAX_BODY_BYTES + 1));
  assert.equal(big.res.status, 413);
  assert.equal(big.req.destroyed, true);
  const declared = fakeReq({ headers: viaServe({ 'content-length': String(MAX_BODY_BYTES + 1) }) });
  const res = fakeRes();
  login.handle(declared, res);
  assert.equal(res.status, 413);
});

test('ten failures a minute lock the form for everyone, then it recovers', () => {
  let clock = 1_000_000;
  const login = createStudioLogin({ token: TOKEN, now: () => clock });
  for (let i = 0; i < MAX_FAILURES; i++) assert.equal(post(login, 'token=wrong-guess-' + i).res.status, 401);
  assert.equal(post(login, 'token=' + TOKEN).res.status, 429);     // even the right token waits
  clock += 61_000;
  assert.equal(post(login, 'token=' + TOKEN).res.status, 303);
});

test('the wiring patch only adds the three login lines ahead of the stock gate', async () => {
  const patch = await readFile(new URL('./claw3d/studio-login.patch', import.meta.url), 'utf8');
  const added = patch.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++'));
  assert.deepEqual(added.map(l => l.slice(1).trim()), [
    'const { createStudioLogin } = require("./cortados-studio-login");',
    'const studioLogin = createStudioLogin({ token: process.env.STUDIO_ACCESS_TOKEN });',
    'if (studioLogin.handle(req, res)) return;',
    'if (studioLogin.handle(req, res)) return;',
  ]);
  assert.ok(!patch.split('\n').some(l => l.startsWith('-') && !l.startsWith('---')), 'removes nothing');
  assert.match(patch, /^\+\+\+ b\/server\/index\.js$/m);
});
