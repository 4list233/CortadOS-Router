import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';

const families = new Set(['claude', 'codex', 'grok', 'ollama', 'local']);
const text = value => typeof value === 'string' ? value : null;
export function characterFamily(provider) {
  // Unknown gets a neutral local character, never a guessed vendor identity.
  return families.has(provider) ? provider : 'local';
}

/** Pure allowlist projection. No source objects are spread into the output. */
export function mapOffice(view) {
  if (view?.version !== 1 || !Array.isArray(view.floors) ||
      !Array.isArray(view.lobby?.workers)) throw new Error('invalid office snapshot');
  const agents = [];
  function add(record, floor, role, index) {
    if (!record || typeof record !== 'object') throw new Error('invalid office record');
    const provider = text(record.provider);
    // v1 omits conversation IDs in the lobby. Metadata fingerprint is best-effort
    // identity there; ordinal disambiguates otherwise identical anonymous records.
    const workerId = text(record.run_id) ?? createHash('sha256')
      .update(JSON.stringify([provider, record.model, record.started_at, index])).digest('hex').slice(0, 20);
    const id = role === 'orchestrator' ? 'office-' + floor.session + '-orchestrator'
      : 'office-' + floor.session + '-' + workerId;
    const status = ['working', 'idle', 'done', 'error'].includes(record.status) ? record.status : 'idle';
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
    if (typeof floor?.session !== 'string' || !Array.isArray(floor.workers)) {
      throw new Error('invalid office floor');
    }
    if (floor.orchestrator) add(floor.orchestrator, floor, 'orchestrator', 0);
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
  if (!/^[\x21-\x7e]{16,4096}$/.test(token)) throw new Error('invalid office token file');
  const response = await fetchImpl(endpoint, {
    method: 'GET', redirect: 'error', headers: { Authorization: 'Bearer ' + token },
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(10000)]) : AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error('office request failed');
  return response.json();
}

export async function runPolling({ gateway, intervalMs = 5000, signal, onError = () => {}, ...options }) {
  // One second floor prevents accidental tight-loop polling; no overlapping fetches.
  if (!Number.isFinite(intervalMs) || intervalMs < 1000 || intervalMs > 3600000) {
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
