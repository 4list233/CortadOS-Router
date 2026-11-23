// CortadOS Office -> Claw3D gateway bridge (issue #77). Node 22+ stdlib only.
//
// Claw3D Studio's server-side proxy (server/gateway-proxy.js upstream) opens ONE
// WebSocket to an "upstream gateway"; this file is that gateway. It polls the
// provider's metadata-only office view with an office:read token (adapter.mjs) and
// serves the read-only frames OfficeGateway builds. It is NOT reachable from the
// browser directly: Studio connects to it server-side.
//
// Guarantees, each enforced here and tested in bridge.test.mjs:
//   - binds 127.0.0.1 or ::1 only (anything else refuses to start);
//   - accepts upgrades only from a loopback peer, to path "/", with a Host of this
//     port on localhost/127.0.0.1/[::1] (DNS-rebinding guard) and an Origin on the
//     allowlist (Studio's proxy sends http://localhost:<this port>);
//   - the first frame must be `connect` carrying the local bridge secret in
//     params.auth.token (Studio injects its configured upstream token there);
//     compared in constant time, re-read from its file on every connect;
//   - bounded frames (64 KiB), no fragmentation, no binary, client frames masked,
//     at most 8 clients, a rate cap, a connect deadline, slow readers dropped;
//   - read methods only (adapter.READ_METHODS); every other method -> READ_ONLY.
// The bridge secret is NOT the office:read token: Studio may hand its upstream token
// to the browser (Claw3D README), so the office token never leaves this process.
import http from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';
import { readFile, stat } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { OfficeGateway, mapOffice, runPolling } from './adapter.mjs';

export const LOOPBACK_BIND = Object.freeze(['127.0.0.1', '::1']);
const LOOPBACK_PEERS = new Set(['127.0.0.1', '::1', '::ffff:127.0.0.1']);
export const DEFAULT_PORT = 18789;             // Claw3D's default upstream gateway port
// Local policy, not protocol limits: the largest frame Studio sends is a connect with
// device/auth params (well under 8 KiB); 64 KiB leaves headroom and bounds memory.
export const MAX_FRAME_BYTES = 64 * 1024;
export const MAX_CLIENTS = 8;                  // one Studio proxy per open browser tab
export const CONNECT_DEADLINE_MS = 10000;
export const RATE_WINDOW_MS = 10000;
export const RATE_MAX_FRAMES = 200;            // Studio's hydration burst is ~20 requests
const MAX_PENDING_WRITE = 1024 * 1024;         // a reader this far behind is dropped
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const TOKEN_MIN_CHARS = 16;
const TOKEN_MAX_CHARS = 4096;

export const defaultOrigins = port => [`http://localhost:${port}`];
const allowedHosts = port => new Set([`localhost:${port}`, `127.0.0.1:${port}`, `[::1]:${port}`]);

/** Why an upgrade request is refused (an HTTP status), or null to accept it. */
export function checkUpgrade(req, { port, origins }) {
  const h = req.headers ?? {};
  if (!LOOPBACK_PEERS.has(req.socket?.remoteAddress)) return 403;
  if (req.method !== 'GET' || req.url !== '/') return 404;
  if (!allowedHosts(port).has(String(h.host ?? '').toLowerCase())) return 403;
  if (typeof h.origin !== 'string' || !origins.includes(h.origin)) return 403;
  if (String(h.upgrade ?? '').toLowerCase() !== 'websocket' || h['sec-websocket-version'] !== '13') return 426;
  const key = h['sec-websocket-key'];
  if (typeof key !== 'string' || !/^[A-Za-z0-9+/]{22}==$/.test(key)) return 400;
  return null;
}

export const acceptKey = key => createHash('sha1').update(key + WS_GUID).digest('base64');

export class CloseError extends Error {
  constructor(code) { super('websocket close ' + code); this.code = code; }
}

/** Incremental RFC 6455 reader for client frames. Text, close, ping and pong only. */
export class FrameReader {
  constructor(maxBytes = MAX_FRAME_BYTES) { this.max = maxBytes; this.buf = Buffer.alloc(0); }
  push(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    const frames = [];
    for (;;) {
      if (this.buf.length < 2) return frames;
      const [b0, b1] = this.buf;
      const fin = b0 & 0x80, opcode = b0 & 0x0f, masked = b1 & 0x80;
      if (b0 & 0x70) throw new CloseError(1002);                   // no extensions negotiated
      if (!masked) throw new CloseError(1002);                     // clients must mask
      if (![1, 8, 9, 10].includes(opcode)) throw new CloseError(1003); // no binary/continuation
      if (!fin) throw new CloseError(1003);                        // no fragmentation
      let len = b1 & 0x7f, offset = 2;
      if (opcode >= 8 && len > 125) throw new CloseError(1002);
      if (len === 126) {
        if (this.buf.length < 4) return frames;
        len = this.buf.readUInt16BE(2); offset = 4;
      } else if (len === 127) {
        if (this.buf.length < 10) return frames;
        const big = this.buf.readBigUInt64BE(2);
        if (big > BigInt(this.max)) throw new CloseError(1009);
        len = Number(big); offset = 10;
      }
      if (len > this.max) throw new CloseError(1009);
      if (this.buf.length < offset + 4 + len) return frames;
      const mask = this.buf.subarray(offset, offset + 4);
      const payload = Buffer.from(this.buf.subarray(offset + 4, offset + 4 + len));
      for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
      this.buf = this.buf.subarray(offset + 4 + len);
      frames.push({ opcode, payload });
    }
  }
}

/** A server frame (never masked). */
export function encodeFrame(opcode, payload) {
  const body = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  let head;
  if (body.length < 126) head = Buffer.from([0x80 | opcode, body.length]);
  else if (body.length < 65536) { head = Buffer.alloc(4); head[0] = 0x80 | opcode; head[1] = 126; head.writeUInt16BE(body.length, 2); }
  else { head = Buffer.alloc(10); head[0] = 0x80 | opcode; head[1] = 127; head.writeBigUInt64BE(BigInt(body.length), 2); }
  return Buffer.concat([head, body]);
}
const closeFrame = code => { const b = Buffer.alloc(2); b.writeUInt16BE(code, 0); return encodeFrame(8, b); };

/** A secret from a file: printable ASCII, 16..4096 chars, owner-only permissions. */
export async function readSecret(path) {
  const info = await stat(path);
  if (!info.isFile() || (info.mode & 0o077)) throw new Error('secret file must be a 0600 regular file');
  const value = (await readFile(path, 'utf8')).trim();
  if (value.length < TOKEN_MIN_CHARS || value.length > TOKEN_MAX_CHARS || !/^[\x21-\x7e]+$/.test(value)) {
    throw new Error('invalid secret file');
  }
  return value;
}
const digest = value => createHash('sha256').update(String(value)).digest();
export const sameSecret = (a, b) => typeof a === 'string' && typeof b === 'string' &&
  timingSafeEqual(digest(a), digest(b));

/** Fan-out target for runPolling: validates once, then updates every connected gateway. */
export class OfficeHub {
  constructor() { this.gateways = new Set(); this.last = null; }
  update(view) {
    mapOffice(view);                       // throws on a malformed snapshot: nothing changes
    this.last = view;
    for (const g of this.gateways) {
      try { g.update(view); } catch { /* one client's failure never stops the others */ }
    }
  }
}

/** The HTTP server (WebSocket upgrades only). Does not listen until listen() is called. */
export function createBridge({ port = DEFAULT_PORT, origins = defaultOrigins(port), bridgeTokenFile,
  hub = new OfficeHub(), log = message => process.stderr.write(message + '\n'),
  connectDeadlineMs = CONNECT_DEADLINE_MS } = {}) {
  if (!bridgeTokenFile) throw new Error('bridge token file required');
  if (!Array.isArray(origins) || !origins.length || origins.some(o => typeof o !== 'string' || o === '*' || o === 'null')) {
    throw new Error('origin allowlist must list explicit origins');
  }
  const clients = new Set();
  const server = http.createServer((req, res) => {
    res.writeHead(426, { 'Content-Type': 'text/plain', Connection: 'close' });
    res.end('WebSocket only\n');
  });
  // `head`: bytes the client sent after the upgrade headers in the same packet (a pipelined connect frame). Node
  // hands them here, not to the socket's 'data' event, so serveSocket feeds them through the same frame path.
  server.on('upgrade', (req, socket, head) => {
    const refuse = status => {
      socket.end(`HTTP/1.1 ${status} ${http.STATUS_CODES[status]}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
      socket.destroy();
    };
    const verdict = checkUpgrade(req, { port: server.address()?.port ?? port, origins });
    if (verdict) { log('Office bridge: upgrade refused (' + verdict + ')'); return refuse(verdict); }
    if (clients.size >= MAX_CLIENTS) { log('Office bridge: client limit reached'); return refuse(503); }
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
      `Sec-WebSocket-Accept: ${acceptKey(req.headers['sec-websocket-key'])}\r\n\r\n`);
    serveSocket(socket, head);
  });

  function serveSocket(socket, head = null) {
    socket.setNoDelay(true);
    const client = { socket, authed: false, closed: false };
    clients.add(client);
    const reader = new FrameReader();
    let windowStart = Date.now(), frames = 0;
    const write = buf => {
      if (client.closed) return;
      if (socket.writableLength > MAX_PENDING_WRITE) return close(1008);
      socket.write(buf);
    };
    const close = code => {
      if (client.closed) return;
      client.closed = true;
      cleanup();
      if (socket.writableLength <= MAX_PENDING_WRITE) socket.write(closeFrame(code));
      socket.end();
      setTimeout(() => socket.destroy(), 1000).unref();
    };
    const gateway = new OfficeGateway(frame => write(encodeFrame(1, JSON.stringify(frame))));
    if (hub.last) { try { gateway.update(hub.last); } catch { /* validated by the hub already */ } }
    const deadline = setTimeout(() => { if (!client.authed) close(1008); }, connectDeadlineMs);
    deadline.unref();
    function cleanup() {
      clearTimeout(deadline);
      clients.delete(client);
      hub.gateways.delete(gateway);
    }
    gateway.challenge();

    async function onRequest(frame) {
      if (!client.authed) {
        if (frame.method !== 'connect') {
          write(encodeFrame(1, JSON.stringify({ type: 'res', id: frame.id, ok: false,
            error: { code: 'NOT_CONNECTED', message: 'Connect first' } })));
          return;
        }
        let expected = null;
        try { expected = await readSecret(bridgeTokenFile); } catch { log('Office bridge: bridge token file unreadable'); }
        const offered = frame.params?.auth?.token;
        if (client.closed) return;
        if (!expected || !sameSecret(offered, expected)) {
          log('Office bridge: connect refused (authentication)');
          write(encodeFrame(1, JSON.stringify({ type: 'res', id: frame.id, ok: false,
            error: { code: 'UNAUTHORIZED', message: 'Bridge authentication failed' } })));
          return close(1008);
        }
        client.authed = true;
        clearTimeout(deadline);
        hub.gateways.add(gateway);
      }
      gateway.request(frame);
    }

    let queue = Promise.resolve();
    function onData(chunk) {
      let parsed;
      try { parsed = reader.push(chunk); } catch (error) { return close(error instanceof CloseError ? error.code : 1002); }
      for (const { opcode, payload } of parsed) {
        if (client.closed) return;
        const now = Date.now();
        if (now - windowStart > RATE_WINDOW_MS) { windowStart = now; frames = 0; }
        if (++frames > RATE_MAX_FRAMES) return close(1008);
        if (opcode === 8) return close(1000);
        if (opcode === 9) { write(encodeFrame(10, payload)); continue; }
        if (opcode === 10) continue;
        let frame;
        try { frame = JSON.parse(payload.toString('utf8')); } catch { return close(1007); }
        if (frame?.type !== 'req' || typeof frame.id !== 'string' || frame.id.length > 128 ||
            typeof frame.method !== 'string' || frame.method.length > 64) continue;
        queue = queue.then(() => onRequest(frame)).catch(() => close(1011));
      }
    }
    socket.on('data', onData);
    socket.on('error', () => { client.closed = true; cleanup(); });
    socket.on('close', () => { client.closed = true; cleanup(); });
    if (head?.length) onData(Buffer.from(head));       // same limits, rate cap and auth as any later chunk
  }

  return {
    server, hub, clients,
    listen(host = '127.0.0.1') {
      if (!LOOPBACK_BIND.includes(host)) throw new Error('bridge must bind 127.0.0.1 or ::1');
      return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen({ host, port, exclusive: true }, () => { server.off('error', reject); resolve(server.address()); });
      });
    },
    close() {
      for (const c of clients) { c.closed = true; c.socket.destroy(); }
      clients.clear();
      return new Promise(resolve => server.close(() => resolve()));
    },
  };
}

/** Startup checks of the two secrets: both 0600 files, and never the same value. */
export async function checkSecrets(officeTokenFile, bridgeTokenFile) {
  const [office, bridge] = await Promise.all([readSecret(officeTokenFile), readSecret(bridgeTokenFile)]);
  if (sameSecret(office, bridge)) throw new Error('bridge secret must differ from the office token');
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const env = process.env;
  const port = Number(env.CORTADOS_OFFICE_BRIDGE_PORT ?? DEFAULT_PORT);
  const host = env.CORTADOS_OFFICE_BRIDGE_HOST ?? '127.0.0.1';
  const origins = env.CORTADOS_OFFICE_BRIDGE_ORIGINS
    ? env.CORTADOS_OFFICE_BRIDGE_ORIGINS.split(',').map(o => o.trim()).filter(Boolean) : defaultOrigins(port);
  const fail = message => { process.stderr.write('Office bridge stopped: ' + message + '\n'); process.exitCode = 1; };
  try {
    if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('invalid port');
    if (!env.CORTADOS_OFFICE_TOKEN_FILE || !env.CORTADOS_OFFICE_BRIDGE_TOKEN_FILE) throw new Error('token files not configured');
    await checkSecrets(env.CORTADOS_OFFICE_TOKEN_FILE, env.CORTADOS_OFFICE_BRIDGE_TOKEN_FILE);
    const bridge = createBridge({ port, origins, bridgeTokenFile: env.CORTADOS_OFFICE_BRIDGE_TOKEN_FILE });
    await bridge.listen(host);
    process.stderr.write(`Office bridge listening on ${host === '::1' ? '[::1]' : host}:${port} (read-only)\n`);
    const controller = new AbortController();
    process.once('SIGINT', () => controller.abort());
    process.once('SIGTERM', () => controller.abort());
    await runPolling({
      gateway: bridge.hub, signal: controller.signal,
      intervalMs: Number(env.CORTADOS_OFFICE_POLL_SECONDS ?? 5) * 1000,
      url: env.CORTADOS_OFFICE_URL ?? 'http://127.0.0.1:8432/v1/cortados/office',
      tokenFile: env.CORTADOS_OFFICE_TOKEN_FILE,
    });
    await bridge.close();
  } catch (error) {
    // Fixed messages only: never echo paths, tokens or exception text from fetch.
    const known = ['invalid port', 'token files not configured', 'secret file must be a 0600 regular file',
      'invalid secret file', 'bridge secret must differ from the office token', 'bridge must bind 127.0.0.1 or ::1',
      'origin allowlist must list explicit origins', 'poll interval must be 1..3600 seconds'];
    fail(known.includes(error?.message) ? error.message : 'startup failed');
  }
}
