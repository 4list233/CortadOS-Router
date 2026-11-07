import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const families = new Set(['claude', 'codex', 'grok', 'ollama', 'local']);
const text = value => typeof value === 'string' ? value : null;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const statuses = new Set(['working', 'idle', 'done', 'error']);

// Local adapter guardrails, not provider protocol/security guarantees:
// 16 printable ASCII characters rejects likely truncated/mistaken token files;
// 4096 characters caps the bearer header at 4 KiB. Longer/shorter credentials
// need an explicit policy revision; length alone does not establish entropy.
const TOKEN_MIN_CHARS = 16;
const TOKEN_MAX_CHARS = 4096;
// 10,000 ms bounds a stalled GET/body read while tolerating ordinary remote
// latency. Slow providers fail visibly and retry; polls never overlap.
const REQUEST_TIMEOUT_MS = 10000;
// 1,000 ms avoids accidental busy polling; 3,600,000 ms (one hour) catches
// likely units mistakes and prevents a nominally live view refreshing less
// than hourly. These are local operational policy, not provider requirements.
const POLL_MIN_MS = 1000;
const POLL_MAX_MS = 3600000;
const reportPollError = message => process.stderr.write(message + '\n');

function validateRecord(record, role, lobby) {
  if (!object(record)) throw new Error('invalid office record');
  // Required projection fields come from office.py _orchestrator/_pub_worker.
  // Nullable metadata is valid, but absent fields and wrong types are not.
  const fields = role === 'orchestrator' ? ['model', 'tool'] :
    ['run_id', 'provider', 'lane', 'model', 'title', 'tool'];
  if (!statuses.has(record.status) || fields.some(field =>
    !Object.hasOwn(record, field) || (record[field] !== null && typeof record[field] !== 'string'))) {
    throw new Error('invalid office record');
  }
  for (const field of ['provider', 'lane', 'model', 'title', 'tool']) {
    if (Object.hasOwn(record, field) && record[field] !== null && typeof record[field] !== 'string') {
      throw new Error('invalid office record');
    }
  }
  if (role === 'worker' && (record.run_id === null ? !lobby :
      !/^[A-Za-z0-9_-]{8,64}$/.test(record.run_id))) throw new Error('invalid office worker identity');
  for (const field of ['started_at', 'ended_at', 'last_seen']) {
    if (Object.hasOwn(record, field) && record[field] !== null &&
        (typeof record[field] !== 'number' || !Number.isFinite(record[field]))) {
      throw new Error('invalid office timestamp');
    }
  }
}
export function characterFamily(provider) {
  // Unknown gets a neutral local character, never a guessed vendor identity.
  return families.has(provider) ? provider : 'local';
}

/** Pure allowlist projection. No source objects are spread into the output. */
export function mapOffice(view) {
  if (!object(view) || view.version !== 1 || !Array.isArray(view.floors) ||
      !object(view.lobby) || !Array.isArray(view.lobby.workers)) throw new Error('invalid office snapshot');
  const agents = [];
  const ids = new Set();
  const sessions = new Set();
  function add(record, floor, role, index) {
    validateRecord(record, role, floor.session === 'lobby');
    const provider = text(record.provider);
    // v1 omits conversation IDs in the lobby. Metadata fingerprint is best-effort
    // identity there; ordinal disambiguates otherwise identical anonymous records.
    const workerId = record.run_id === null ? 'anonymous-' + createHash('sha256')
      .update(JSON.stringify([provider, record.model, record.started_at, index])).digest('hex').slice(0, 20)
      : 'run-' + record.run_id;
    const id = role === 'orchestrator' ? 'office-' + floor.session + '-orchestrator'
      : 'office-' + floor.session + '-worker-' + workerId;
    // Duplicate source identities must not conflate roster/lifecycle state.
    if (ids.has(id)) throw new Error('duplicate office identity');
    ids.add(id);
    const status = record.status;
    const name = text(record.title) ?? (role === 'orchestrator' ? 'Orchestrator' : 'Worker');
    agents.push({
      id, name, identity: { name, emoji: role === 'orchestrator' ? '👔' : '🤖' }, role,
      office: {
        floor: floor.session, project: text(floor.project), device: text(floor.device),
        provider, lane: text(record.lane), model: text(record.model), status,
        tool: text(record.tool), title: text(record.title),
        characterFamily: characterFamily(provider), outfit: role === 'orchestrator' ? 'suit' : 'worker',
      },
    });
  }
  for (const floor of view.floors) {
    // Provider v1 session tags are 16..64 lowercase hex (office.py header rule).
    // Enforcing that grammar also prevents delimiter and reserved-lobby aliases.
    if (!object(floor) || typeof floor.session !== 'string' ||
        !/^[0-9a-f]{16,64}$/.test(floor.session) || !Array.isArray(floor.workers) ||
        sessions.has(floor.session)) throw new Error('invalid office floor');
    sessions.add(floor.session);
    for (const field of ['project', 'device']) {
      if (floor[field] != null && typeof floor[field] !== 'string') throw new Error('invalid office floor');
    }
    if (floor.orchestrator !== null && floor.orchestrator !== undefined) {
      add(floor.orchestrator, floor, 'orchestrator', 0);
    }
    floor.workers.forEach((worker, i) => add(worker, floor, 'worker', i));
  }
  view.lobby.workers.forEach((worker, i) => add(worker, { session: 'lobby' }, 'worker', i));
  return agents;
}

const event = (name, payload) => ({ type: 'event', event: name, payload });
const key = agent => 'agent:' + agent.id + ':main';
const methods = ['agents.list', 'sessions.list', 'status'];

/** Transport seam: send(object) emits a JSON frame to one authenticated client.
 * Future WS host owns localhost bind, Origin/auth checks, frame limits and clients.
 * This class never opens a socket and never forwards a mutation upstream.
 */
export class OfficeGateway {
  constructor(send) {
    this.send = send;
    this.agents = [];
    this.connected = false;
    this.generation = 0;
    this.runs = new Map();
  }
  challenge() {
    this.send(event('connect.challenge', { nonce: createHash('sha256')
      .update(String(Math.random())).digest('hex').slice(0, 16) }));
  }
  sessions() {
    return this.agents.map(a => ({
      key: key(a), agentId: a.id, displayName: a.name,
      origin: { label: a.name, provider: a.office.provider },
      model: a.office.model, modelProvider: a.office.provider,
    }));
  }
  presence() {
    const recent = this.sessions();
    return { sessions: { recent, byAgent: this.agents.map(a => ({
      agentId: a.id, recent: recent.filter(s => s.agentId === a.id),
    })) } };
  }
  request(frame) {
    if (frame?.type !== 'req' || typeof frame.id !== 'string') return;
    const reply = payload => this.send({ type: 'res', id: frame.id, ok: true, payload });
    if (frame.method === 'connect') {
      this.connected = true;
      reply({
        type: 'hello-ok', protocol: 3, adapterType: 'cortados-office',
        features: { methods, events: ['agent', 'presence', 'office.snapshot'] },
        snapshot: { health: { agents: this.agents, defaultAgentId: this.agents[0]?.id ?? null },
          sessionDefaults: { mainKey: 'main' } },
        auth: { role: 'operator', scopes: ['operator.read'] },
        policy: { tickIntervalMs: 30000 },
      });
    } else if (!this.connected) {
      this.send({ type: 'res', id: frame.id, ok: false,
        error: { code: 'NOT_CONNECTED', message: 'Connect first' } });
    } else if (frame.method === 'agents.list') {
      reply({ defaultId: this.agents[0]?.id ?? null, mainKey: 'main', agents: this.agents });
    } else if (frame.method === 'sessions.list') reply({ sessions: this.sessions() });
    else if (frame.method === 'status') reply(this.presence());
    else this.send({ type: 'res', id: frame.id, ok: false,
      error: { code: 'READ_ONLY', message: 'Office adapter is read-only' } });
  }
  update(view) {
    const next = mapOffice(view); // validate before replacing the last good state
    const old = new Map(this.agents.map(a => [a.id, a]));
    this.agents = next;
    if (!this.connected) return;
    const lifecycle = (a, phase, runId) => this.send(event('agent', {
      agentId: a.id, sessionKey: key(a), runId, stream: 'lifecycle', data: { phase },
    }));
    for (const a of next) {
      const before = old.get(a.id);
      if (a.office.status === 'working') {
        let runId = this.runs.get(a.id);
        if (!runId) {
          runId = a.id + ':' + (++this.generation);
          this.runs.set(a.id, runId);
          lifecycle(a, 'start', runId);
        }
        if (a.office.tool && a.office.tool !== before?.office.tool) {
          this.send(event('agent', { agentId: a.id, sessionKey: key(a), runId,
            stream: 'tool', data: { phase: 'start', name: a.office.tool } }));
        }
      } else if (this.runs.has(a.id)) {
        lifecycle(a, a.office.status === 'error' ? 'error' : 'end', this.runs.get(a.id));
        this.runs.delete(a.id);
      }
    }
    for (const [id, a] of old) {
      if (!next.some(n => n.id === id) && this.runs.has(id)) {
        lifecycle(a, 'end', this.runs.get(id));
        this.runs.delete(id);
      }
    }
    this.send(event('presence', this.presence()));
    // Extension for floor/appearance/status and roster removal; stock UI may ignore.
    this.send(event('office.snapshot', { agents: next }));
  }
}

/** Only GET the exact office endpoint. Reject redirects to avoid credential forwarding. */
export async function pollOffice({ url, tokenFile, fetchImpl = fetch, signal }) {
  const endpoint = new URL(url);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password ||
      endpoint.pathname !== '/v1/cortados/office' || endpoint.search || endpoint.hash) {
    throw new Error('invalid office URL');
  }
  if (endpoint.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)) {
    throw new Error('remote office URL requires HTTPS');
  }
  const token = (await readFile(tokenFile, 'utf8')).trim();
  if (token.length < TOKEN_MIN_CHARS || token.length > TOKEN_MAX_CHARS ||
      !/^[\x21-\x7e]+$/.test(token)) throw new Error('invalid office token file');
  const response = await fetchImpl(endpoint, {
    method: 'GET', redirect: 'error', headers: { Authorization: 'Bearer ' + token },
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(REQUEST_TIMEOUT_MS)]) : AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error('office request failed');
  return response.json();
}

export async function runPolling({ gateway, intervalMs = 5000, signal, onError = reportPollError, ...options }) {
  if (typeof onError !== 'function') throw new Error('error reporter must be a function');
  if (!Number.isFinite(intervalMs) || intervalMs < POLL_MIN_MS || intervalMs > POLL_MAX_MS) {
    throw new Error('poll interval must be 1..3600 seconds');
  }
  while (!signal?.aborted) {
    try { gateway.update(await pollOffice({ ...options, signal })); }
    catch {
      if (signal?.aborted) break;
      // Never log exceptions: fetch/read errors may contain URLs, paths or tokens.
      onError('Office poll failed; last successful snapshot retained (may be stale)');
    }
    try { await delay(intervalMs, undefined, { signal }); }
    catch (error) { if (!signal?.aborted) throw error; }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (!process.argv.includes('--stdio') || !process.env.CORTADOS_OFFICE_TOKEN_FILE) {
    process.stderr.write('Skeleton only: use --stdio and CORTADOS_OFFICE_TOKEN_FILE; no WebSocket listener implemented\n');
    process.exitCode = 1;
  } else {
    const controller = new AbortController();
    process.once('SIGINT', () => controller.abort());
    process.once('SIGTERM', () => controller.abort());
    const gateway = new OfficeGateway(frame => process.stdout.write(JSON.stringify(frame) + '\n'));
    gateway.request({ type: 'req', id: 'stdio', method: 'connect' });
    await runPolling({
      gateway, signal: controller.signal,
      intervalMs: Number(process.env.CORTADOS_OFFICE_POLL_SECONDS ?? 5) * 1000,
      url: process.env.CORTADOS_OFFICE_URL ?? 'http://127.0.0.1:8432/v1/cortados/office',
      tokenFile: process.env.CORTADOS_OFFICE_TOKEN_FILE,
      onError: message => process.stderr.write(message + '\n'),
    }).catch(() => { process.stderr.write('Office adapter stopped: invalid configuration\n'); process.exitCode = 1; });
  }
}
