'use strict';
// CortadOS Office (#77): a login form for Claw3D Studio's own access gate.
//
// Claw3D 51337f7 (server/access-gate.js) admits a request only when its `studio_access` cookie
// equals STUDIO_ACCESS_TOKEN, and ships no way to set that cookie: no login page, no query
// parameter. Published on the tailnet (deploy-air.sh --publish-tailnet), a phone or the Pro
// would have no way in. This module serves one path, /cortados-login: a password form whose
// POST sets that same cookie when the token matches. It is copied into Claw3D's server/ as
// cortados-studio-login.js and wired in by claw3d/studio-login.patch (3 lines, before the gate).
//
// Node stdlib only, CommonJS like Claw3D's server. Guarantees, each tested in
// studio-login.test.mjs with fake requests:
//   - inert (handles nothing) when no usable token is configured; the stock gate then decides;
//   - only GET/HEAD/POST on exactly /cortados-login; every other path passes through untouched;
//   - POST must be same-origin (Origin equal to the request's own scheme and Host, or the
//     X-Forwarded-Host/-Proto Tailscale Serve sets) and form-encoded, body capped at 4 KiB;
//   - constant-time compare; 10 failures a minute -> 429 for everyone (all traffic reaches
//     Studio from loopback, so per-IP limits cannot tell devices apart);
//   - the cookie is HttpOnly, SameSite=Strict, Path=/, 30 days, Secure when the request came
//     over HTTPS (Tailscale Serve); the token is never echoed into a page, header or log.
const crypto = require('node:crypto');

const LOGIN_PATH = '/cortados-login';
const COOKIE = 'studio_access';                 // access-gate.js's default cookie name
const MAX_BODY_BYTES = 4096;
const MAX_FAILURES = 10;                        // access-gate.js's own limit and window
const FAILURE_WINDOW_MS = 60000;
const COOKIE_MAX_AGE_S = 30 * 24 * 3600;
// The cookie value is the token itself (that is what the gate compares), so it must be
// cookie-safe. deploy-air.sh makes it with `openssl rand -hex 32`.
const TOKEN_RE = /^[A-Za-z0-9._~-]{16,4096}$/;

const digest = value => crypto.createHash('sha256').update(String(value)).digest();
const sameSecret = (a, b) => typeof a === 'string' && typeof b === 'string' &&
  crypto.timingSafeEqual(digest(a), digest(b));

const page = message => '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1"><title>CortadOS Office</title>' +
  '<style>body{font:16px system-ui,sans-serif;background:#1d1b19;color:#f3ece2;display:grid;place-items:center;' +
  'min-height:90vh;margin:0}form{display:grid;gap:12px;width:min(22rem,90vw)}input,button{font:inherit;' +
  'padding:10px;border-radius:6px;border:1px solid #6b5b4a}button{background:#c8a97e;color:#1d1b19}' +
  'p{margin:0;color:#e8a0a0}</style></head><body><form method="post" action="' + LOGIN_PATH + '">' +
  '<h1>CortadOS Office</h1>' + (message ? '<p>' + message + '</p>' : '') +
  '<input type="text" name="username" value="cortados-office" autocomplete="username" hidden>' +
  '<input type="password" name="token" autocomplete="current-password" required maxlength="4096" ' +
  'aria-label="Studio access token" placeholder="Studio access token" autofocus>' +
  '<button type="submit">Log in</button></form></body></html>';

const first = value => (Array.isArray(value) ? value[0] : value);

/** The scheme and the authorities this request may call its own origin. */
function ownOrigins(req) {
  const proto = String(first(req.headers['x-forwarded-proto']) ?? '').toLowerCase() === 'https' ? 'https'
    : req.socket?.encrypted ? 'https' : 'http';
  const hosts = [first(req.headers.host), first(req.headers['x-forwarded-host'])]
    .filter(h => typeof h === 'string' && h.length > 0 && h.length <= 255 && !/[\s/@]/.test(h));
  return { proto, origins: new Set(hosts.map(h => proto + '://' + h.toLowerCase())) };
}

function createStudioLogin({ token, now = Date.now } = {}) {
  const secret = String(token ?? '').trim();
  const enabled = TOKEN_RE.test(secret);
  let windowStart = 0, failures = 0;
  const limited = () => {
    if (now() - windowStart > FAILURE_WINDOW_MS) { windowStart = now(); failures = 0; }
    return failures >= MAX_FAILURES;
  };
  const send = (res, status, body, extra = {}) => {
    res.writeHead(status, {
      'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
      'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
      ...extra,
    });
    res.end(body);
  };

  /** true: this module answered the request; false: not ours, hand it on (to the gate). */
  function handle(req, res) {
    if (!enabled) return false;
    const raw = String(req.url || '/');
    const path = raw.split('?', 1)[0];
    if (path !== LOGIN_PATH) return false;
    if (req.method === 'GET' || req.method === 'HEAD') { send(res, 200, page('')); return true; }
    if (req.method !== 'POST') { send(res, 405, page('Use the form.'), { Allow: 'GET, HEAD, POST' }); return true; }
    const { proto, origins } = ownOrigins(req);
    const origin = first(req.headers.origin);
    if (typeof origin !== 'string' || !origins.has(origin.toLowerCase())) {
      send(res, 403, page('Log in from this page.')); req.resume?.(); return true;
    }
    const type = String(first(req.headers['content-type']) ?? '').split(';', 1)[0].trim().toLowerCase();
    if (type !== 'application/x-www-form-urlencoded') {
      send(res, 415, page('Use the form.')); req.resume?.(); return true;
    }
    const declared = Number(first(req.headers['content-length']));
    if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
      send(res, 413, page('Too long.'), { Connection: 'close' }); req.destroy?.(); return true;
    }
    if (limited()) { send(res, 429, page('Too many attempts. Wait a minute.')); req.resume?.(); return true; }
    const chunks = [];
    let size = 0, done = false;
    req.on('data', chunk => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        done = true;
        send(res, 413, page('Too long.'), { Connection: 'close' });
        req.destroy?.();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (done) return;
      done = true;
      if (limited()) { send(res, 429, page('Too many attempts. Wait a minute.')); return; }
      const offered = new URLSearchParams(Buffer.concat(chunks).toString('utf8')).get('token');
      if (!sameSecret(typeof offered === 'string' ? offered.trim() : null, secret)) {
        failures += 1;
        send(res, 401, page('That token is not right.'));
        return;
      }
      const cookie = `${COOKIE}=${secret}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${COOKIE_MAX_AGE_S}` +
        (proto === 'https' ? '; Secure' : '');
      send(res, 303, '', { Location: '/', 'Set-Cookie': cookie });
    });
    req.on('error', () => { done = true; });
    return true;
  }

  return { enabled, handle };
}

module.exports = { createStudioLogin, LOGIN_PATH, COOKIE, MAX_BODY_BYTES, MAX_FAILURES };
