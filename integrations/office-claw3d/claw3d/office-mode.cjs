'use strict';
// CortadOS Office (#77, fix round 1): the server-side read-only boundary of Claw3D Studio ("office mode").
//
// Why: cortados-brand.patch only HIDES Studio's OpenClaw controls. An authenticated browser (one holding the
// studio_access cookie) could still call Studio's own API directly: PUT /api/studio rewrites the persisted
// gateway settings the proxy takes its upstream from, /api/office/remote-message and /remote-handoff connect
// straight to a configured remote gateway (NodeGatewayClient, outside the proxy's UPSTREAM_ALLOWLIST),
// /api/runtime/custom forwards requests to allowed hosts including any localhost port, the voice routes call
// out, and a dozen more write Studio's local state. The Office is a read-only view of the CortadOS router,
// so the server answers only what that view needs and refuses everything else with 403.
//
// It is copied into Claw3D's server/ as cortados-office-mode.js and wired in by claw3d/office-mode.patch,
// after the login form and the stock access gate (an unauthenticated request still gets the gate's 401),
// before Next. It has no switch: this Studio build serves only the CortadOS bridge (the brand patch is not
// switched either), and a boundary that can be turned off by an env var is one typo from open.
//
// Fail closed: a request is allowed only when its method AND its exact raw path (before `?`) are on the list
// below. No decoding, no normalising: `/api/studio/`, `/API/studio`, `/api/%73tudio`, `/api//studio` or an
// absolute-form `http://…` target match nothing and are refused, so Next's own path handling can never turn
// an allowed string into a refused route. Prefix entries (static files) additionally refuse `..`, empty
// segments and any character outside [A-Za-z0-9._~-].
//
// The list was taken by exercising the patched Studio (fix round 1, 2026-11-25): headless Chrome logged in
// and opened the Office against a local bridge; every request it made is either below or refused on purpose
// (PUT /api/studio, the page's UI-state saves: refused, settings are pinned; the page keeps working).
//
// Node stdlib only, CommonJS like Claw3D's server. Tested in office-mode.test.mjs (the function and a real
// http server with the patched wiring).

const READ = ['GET', 'HEAD'];

/** Exact paths: [path, methods, reason]. The reason is documentation and is asserted to exist in tests. */
const EXACT = [
  ['/cortados-login', ['GET', 'HEAD', 'POST'], 'the login form (studio-login.cjs answers it before this check; listed so the order cannot lock anyone out)'],
  ['/', READ, 'the start page; it only redirects to /office'],
  ['/office', READ, 'the Office page itself'],
  ['/favicon.ico', READ, 'the browser tab icon'],
  ['/api/health', READ, 'liveness: deploy-air.sh and the LaunchDaemon checks'],
  ['/api/studio', READ, 'Studio settings as the page boots (sanitized: the gateway token is never in it); PUT is refused, settings are pinned'],
  ['/api/task-store', READ, "the Office polls the task board; it reads Studio's own local file, which nothing can write now"],
  ['/api/office/standup/config', READ, 'the Office reads the standup preference while it hydrates (read only; PUT refused)'],
  ['/api/office/standup/meeting', READ, 'the Office reads the active standup meeting while it hydrates (read only; PUT refused)'],
];

/** Static file prefixes, GET/HEAD only: [prefix, reason]. */
const PREFIXES = [
  ['/_next/static/', "Next's built JavaScript, CSS and media for the pages above"],
  ['/office-assets/', "the Office's 3D models and backgrounds (Claw3D's public/office-assets)"],
];

/** The one WebSocket upgrade: Studio's gateway proxy, whose upstream is pinned to the CortadOS bridge. */
const WS_PATH = '/api/gateway/ws';

const SAFE_SEGMENT = /^[A-Za-z0-9._~-]+$/;
const exact = new Map(EXACT.map(([path, methods]) => [path, new Set(methods)]));

const rawPath = url => {
  const raw = typeof url === 'string' ? url : '';
  const q = raw.indexOf('?');
  return q === -1 ? raw : raw.slice(0, q);
};

const safeUnder = (path, prefix) => {
  if (!path.startsWith(prefix)) return false;
  const rest = path.slice(prefix.length).split('/');
  return rest.every(seg => SAFE_SEGMENT.test(seg) && seg !== '.' && seg !== '..');
};

/** true when office mode lets this HTTP request through to Next. */
function allowHttp(method, url) {
  const m = String(method || '').toUpperCase();
  const path = rawPath(url);
  const methods = exact.get(path);
  if (methods) return methods.has(m);
  if (!READ.includes(m)) return false;
  return PREFIXES.some(([prefix]) => safeUnder(path, prefix));
}

/** true when office mode lets this upgrade through (only the gateway proxy, exact path). */
const allowUpgrade = url => rawPath(url) === WS_PATH;

const REFUSAL = 'The CortadOS Office is read-only: this route is not served.';

function createOfficeMode() {
  /** true: refused and answered here; false: allowed, hand it on to Next. */
  function handleHttp(req, res) {
    if (allowHttp(req.method, req.url)) return false;
    const isApi = rawPath(req.url).startsWith('/api/');
    res.writeHead(403, {
      'Content-Type': isApi ? 'application/json' : 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    });
    res.end(isApi ? JSON.stringify({ error: REFUSAL }) : REFUSAL);
    req.resume?.();
    return true;
  }

  /** true: refused (socket answered 403 and closed); false: allowed. */
  function handleUpgrade(req, socket) {
    if (allowUpgrade(req.url)) return false;
    try {
      socket.write('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n');
    } finally {
      socket.destroy();
    }
    return true;
  }

  return { handleHttp, handleUpgrade };
}

module.exports = { createOfficeMode, allowHttp, allowUpgrade, EXACT, PREFIXES, WS_PATH, REFUSAL };
