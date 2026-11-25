#!/usr/bin/env node
// CortadOS Office (#77): deploy-air.sh's end-to-end health probe. It does what the browser does
// when the Office opens: a WebSocket to Studio's own proxy (/api/gateway/ws on loopback), behind
// Studio's access gate, then an OpenClaw `connect`. Studio's proxy injects its gateway token and
// forwards the connect to the bridge; only the bridge's `ok:true` hello passes.
//
// Reason: on 2026-11-24 every other check passed (daemons running, bridge 426, Studio 401/200,
// loopback listeners) while the Office showed "CONNECTING" with 0 agents, because Studio refused
// its upstream (`studio.gateway_url_blocked`, no UPSTREAM_ALLOWLIST in production). Only a real
// connect through the proxy sees that.
//
// It sends two connects, one per gateway socket, under one deadline: first one with no credential,
// then one shaped like the browser's over HTTPS (Claw3D's GatewayBrowserClient in a secure context:
// a complete Ed25519-signed `device` block, signed over the bridge's challenge nonce, and no token).
// Reason: on 2026-11-25 the Office over HTTPS showed "Gateway closed (1012): upstream closed" while
// this check passed. The stock proxy took the browser's device block as the browser's own auth and
// did not inject its token, so the bridge refused; the check's tokenless, deviceless connect never
// took that path. claw3d/proxy-auth.patch makes the proxy always send its token; this connect proves it.
//
//   sudo cat <studio-access-token> | node gateway-check.mjs --port 3000 [--timeout-ms 5000]
//
// The access token comes on stdin only (never argv, never printed); it is sent as the
// `studio_access` cookie, which is what Claw3D's access gate compares. Prints one line: `ok`, or a
// fixed reason (an error code from the proxy is printed only as a short [A-Za-z0-9._-] word).
// Exit 0 only on the bridge's hello to both connects; any error, refusal, close or the deadline is exit 1.
// Node stdlib only; a minimal RFC 6455 client (text frames, masked, no extensions).
import net from 'node:net';
import { createHash, generateKeyPairSync, randomBytes, sign } from 'node:crypto';
import { isMainModule } from './adapter.mjs';

const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';
const TOKEN_RE = /^[A-Za-z0-9._~-]{16,4096}$/;     // cookie-safe, as studio-login.cjs requires
const MAX_BUFFER = 256 * 1024;                      // Claw3D's own frame cap; a hello is far smaller
const CONNECT_ID = 'cortados-health-connect';
const safeWord = v => (typeof v === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(v) ? v : 'unknown');

/** A masked client text frame. */
export function clientFrame(opcode, payload) {
  const body = Buffer.from(payload);
  const mask = randomBytes(4);
  let head;
  if (body.length < 126) head = Buffer.from([0x80 | opcode, 0x80 | body.length]);
  else if (body.length < 65536) { head = Buffer.alloc(4); head[0] = 0x80 | opcode; head[1] = 0x80 | 126; head.writeUInt16BE(body.length, 2); }
  else throw new Error('frame too large');
  const out = Buffer.from(body);
  for (let i = 0; i < out.length; i++) out[i] ^= mask[i & 3];
  return Buffer.concat([head, mask, out]);
}

/** Unmasked server frames from buf: { frames, rest }; throws on anything this probe does not expect. */
export function readServerFrames(buf) {
  const frames = [];
  for (;;) {
    if (buf.length < 2) return { frames, rest: buf };
    const fin = buf[0] & 0x80, opcode = buf[0] & 0x0f;
    if (buf[0] & 0x70) throw new Error('unexpected frame');
    if (buf[1] & 0x80) throw new Error('masked server frame');
    let len = buf[1] & 0x7f, off = 2;
    if (len === 126) { if (buf.length < 4) return { frames, rest: buf }; len = buf.readUInt16BE(2); off = 4; }
    else if (len === 127) {
      if (buf.length < 10) return { frames, rest: buf };
      const big = buf.readBigUInt64BE(2);
      if (big > BigInt(MAX_BUFFER)) throw new Error('frame too large');
      len = Number(big); off = 10;
    }
    if (!fin) throw new Error('fragmented frame');
    if (buf.length < off + len) return { frames, rest: buf };
    frames.push({ opcode, payload: buf.subarray(off, off + len) });
    buf = buf.subarray(off + len);
  }
}

/**
 * The params of the connect Claw3D 51337f7's browser client sends over HTTPS with no token typed in
 * (src/lib/gateway/openclaw/GatewayBrowserClient.ts 509-585): a fresh Ed25519 identity, the device
 * id the SHA-256 hex of the raw public key, the v2 payload signed over the challenge nonce. The key
 * is thrown away: the bridge checks no signature; the point is the shape the proxy decides on.
 */
export function browserConnectParams(nonce) {
  const { publicKey, privateKey } = generateKeyPairSync('ed25519');
  const raw = Buffer.from(publicKey.export({ format: 'jwk' }).x, 'base64url');
  const id = createHash('sha256').update(raw).digest('hex');
  const client = { id: 'openclaw-control-ui', version: 'dev', platform: 'web', mode: 'webchat' };
  const role = 'operator';
  const scopes = ['operator.read', 'operator.admin', 'operator.approvals', 'operator.pairing'];
  const signedAt = Date.now();
  const payload = ['v2', id, client.id, client.mode, role, scopes.join(','), String(signedAt), '', nonce].join('|');
  const signature = sign(null, Buffer.from(payload, 'utf8'), privateKey).toString('base64url');
  return { minProtocol: 3, maxProtocol: 4, client, role, scopes,
    device: { id, publicKey: raw.toString('base64url'), signature, signedAt, nonce },
    caps: [], userAgent: 'cortados-deploy-health', locale: 'en' };
}

const NONCE_RE = /^[A-Za-z0-9._~-]{1,256}$/;

/**
 * One connect through Studio's proxy. Resolves to 'ok' or a fixed reason; never rejects, never
 * includes the token. shape 'plain' sends at once with no credential; 'browser' waits for the
 * upstream's connect.challenge and sends browserConnectParams(nonce).
 */
export function probe({ host = '127.0.0.1', port, token, timeoutMs = 5000, shape = 'plain' }) {
  return new Promise(resolve => {
    if (!TOKEN_RE.test(token ?? '')) { resolve('no usable access token on stdin'); return; }
    if (shape !== 'plain' && shape !== 'browser') { resolve('unknown connect shape'); return; }
    let settled = false, upgraded = false, sent = false, buf = Buffer.alloc(0);
    const key = randomBytes(16).toString('base64');
    const accept = createHash('sha1').update(key + WS_GUID).digest('base64');
    const socket = net.connect({ host, port });
    const finish = verdict => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { if (upgraded && !socket.destroyed) socket.write(clientFrame(8, Buffer.from([0x03, 0xe8]))); } catch { /* closing anyway */ }
      socket.end(); socket.destroy();
      resolve(verdict);
    };
    const sendConnect = params => {
      sent = true;
      socket.write(clientFrame(1, JSON.stringify({ type: 'req', id: CONNECT_ID, method: 'connect', params })));
    };
    const timer = setTimeout(() => finish(`no answer within ${timeoutMs} ms`), timeoutMs);
    socket.on('error', () => finish('cannot reach Studio'));
    socket.on('close', () => finish(upgraded ? 'Studio closed the gateway socket before the bridge answered' : 'Studio closed the connection'));
    socket.on('connect', () => {
      socket.write(`GET /api/gateway/ws HTTP/1.1\r\nHost: ${host}:${port}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n` +
        `Origin: http://${host}:${port}\r\nCookie: studio_access=${token}\r\n\r\n`);
    });
    socket.on('data', chunk => {
      buf = Buffer.concat([buf, chunk]);
      if (buf.length > MAX_BUFFER) return finish('answer too large');
      if (!upgraded) {
        const end = buf.indexOf('\r\n\r\n');
        if (end < 0) return;
        const lines = buf.subarray(0, end).toString('latin1').split('\r\n');
        const status = /^HTTP\/1\.1 (\d{3})/.exec(lines[0])?.[1];
        if (status !== '101') return finish(`gateway upgrade refused (HTTP ${status ?? '?'})`);
        const got = lines.slice(1).find(l => /^sec-websocket-accept:/i.test(l))?.split(':').slice(1).join(':').trim();
        if (got !== accept) return finish('gateway upgrade answered without a valid accept key');
        upgraded = true;
        buf = buf.subarray(end + 4);
        // No auth in params: Studio's proxy injects its configured gateway token, as for a browser.
        if (shape === 'plain') sendConnect({ minProtocol: 3, maxProtocol: 3, client: { id: 'cortados-deploy-health', mode: 'probe' } });
      }
      let parsed;
      try { parsed = readServerFrames(buf); } catch { return finish('unreadable gateway frame'); }
      buf = parsed.rest;
      for (const { opcode, payload } of parsed.frames) {
        if (opcode === 8) return finish(`Studio closed the gateway socket (${payload.length >= 2 ? payload.readUInt16BE(0) : 'no code'})`);
        if (opcode !== 1) continue;                    // ping/pong: not needed for a few seconds
        let frame;
        try { frame = JSON.parse(payload.toString('utf8')); } catch { return finish('non-JSON gateway frame'); }
        if (shape === 'browser' && !sent && frame?.type === 'event' && frame.event === 'connect.challenge') {
          const nonce = frame.payload?.nonce;
          if (typeof nonce !== 'string' || !NONCE_RE.test(nonce)) return finish('connect.challenge without a usable nonce');
          sendConnect(browserConnectParams(nonce));
          continue;
        }
        if (frame?.type !== 'res' || frame.id !== CONNECT_ID) continue;   // connect.challenge, presence...
        if (frame.ok === true && frame.payload?.type === 'hello-ok') return finish('ok');
        return finish(`connect refused: ${safeWord(frame.error?.code)}`);
      }
    });
  });
}

/**
 * deploy-air.sh's verdict: the plain connect, then the browser-shaped one, both within timeoutMs in
 * total. 'ok' only if both get the bridge's hello; a failure of the second names it.
 */
export async function check({ host = '127.0.0.1', port, token, timeoutMs = 5000 }) {
  const deadline = Date.now() + timeoutMs;
  const plain = await probe({ host, port, token, timeoutMs });
  if (plain !== 'ok') return plain;
  const left = deadline - Date.now();
  if (left <= 0) return `no answer within ${timeoutMs} ms`;
  const browser = await probe({ host, port, token, timeoutMs: left, shape: 'browser' });
  return browser === 'ok' ? 'ok' : `browser-shaped connect (signed device, no token): ${browser}`;
}

async function main(argv) {
  let port = NaN, timeoutMs = 5000;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--port') port = Number(argv[++i]);
    else if (argv[i] === '--timeout-ms') timeoutMs = Number(argv[++i]);
    else { console.log('usage: gateway-check.mjs --port N [--timeout-ms MS] < token'); return 2; }
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535 || !Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 60000) {
    console.log('usage: gateway-check.mjs --port N [--timeout-ms MS] < token'); return 2;
  }
  const chunks = [];
  for await (const c of process.stdin) { chunks.push(c); if (chunks.reduce((n, x) => n + x.length, 0) > 8192) break; }
  const token = Buffer.concat(chunks).toString('utf8').trim();
  const verdict = await check({ port, token, timeoutMs });
  console.log(verdict);
  return verdict === 'ok' ? 0 : 1;
}

if (isMainModule(import.meta.url)) {
  process.exitCode = await main(process.argv.slice(2));
}
