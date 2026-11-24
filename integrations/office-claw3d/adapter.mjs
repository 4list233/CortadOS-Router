import { readFile } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { resolve } from 'node:path';
import { createHash, randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { avatarDataUrl, avatarProfile } from './sprites.mjs';

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
  for (const field of ['provider', 'lane', 'local_backend', 'model', 'title', 'tool']) {
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
// ---- appearance (issue #77): which sprite a person wears. Appearance only: it never
// changes the published provider/lane, which stay exactly as office.py reported them.
// Maps, not object literals: a provider such as "__proto__" must miss, not hit a prototype.
const PROVIDER_FAMILY = new Map(Object.entries({ claude: 'claude', codex: 'codex', openai: 'codex', grok: 'grok',
  xai: 'grok', ollama: 'ollama', local: 'local' }));
const LANE_FAMILY = new Map(Object.entries({ claude: 'claude', 'claude-cli': 'claude', codex: 'codex',
  'codex-cli': 'codex', grok: 'grok' }));
// Ollama names a model `name:tag` (qwen3:27b, library/llama3.3:70b-q4_K_M). The vllm-mlx
// backend serves Hugging Face style ids (mlx-community/...), never with a tag.
const OLLAMA_TAG = /^[a-z0-9][a-z0-9._-]*(\/[a-z0-9][a-z0-9._-]*)?:[a-z0-9][a-z0-9._-]*$/i;

// The local node's backend as office.py publishes it (`local_backend`, from the node gate's own report): exact when
// present. Any other non-empty value is a backend this adapter does not know: a local node, but not Ollama.
const LOCAL_BACKEND_FAMILY = new Map(Object.entries({ ollama: 'ollama', 'vllm-mlx': 'local' }));
/** The published backend when it is one office.py names, else null (never echoed as free text). */
export const knownBackend = backend => LOCAL_BACKEND_FAMILY.has(text(backend)) ? backend : null;

/** Family of a local-lane model: the published backend when there is one; only without it, the llama for an
 * Ollama-shaped tag (a name-shape guess), else generic local. */
function localFamily(model, backend = null) {
  const b = text(backend);
  if (b) return LOCAL_BACKEND_FAMILY.get(b) ?? 'local';
  const m = text(model) ?? '';
  return OLLAMA_TAG.test(m) && !/(^|[/._:-])mlx([/._:-]|$)/i.test(m) ? 'ollama' : 'local';
}

/** Worker sprite: by the provider it ran on, then its lane key; never by a guess. A local worker's llama-or-chip
 * comes from `backend` (office.py's local_backend), else from the model id's shape. */
export function characterFamily(provider, lane = null, model = null, backend = null) {
  const p = text(provider)?.toLowerCase() ?? null;
  const l = text(lane)?.toLowerCase() ?? '';
  const family = PROVIDER_FAMILY.get(p) ?? (p === null ? laneFamily(l) : null);
  if (family === 'local') return localFamily(model, backend);
  return family ?? 'unknown';
}
function laneFamily(lane) {
  if (lane.startsWith('local-')) return 'local';
  const base = lane.split(/[#:/]/, 1)[0];
  return LANE_FAMILY.get(base) ?? null;
}

/** Orchestrator sprite: by its MODEL (office v1 reports no provider/lane for it).
 * Styling only; the published provider stays null. */
export function modelFamily(model, backend = null) {
  // a routed local main loop: office.py names its node's backend, which is exact
  if (text(backend)) return localFamily(model, backend);
  const m = (text(model) ?? '').toLowerCase().replace(/^(sub|api)\//, '');
  if (!m) return 'unknown';
  if (/^(claude|anthropic)|(^|[-/])(opus|sonnet|haiku|fable)([-.\d]|$)/.test(m)) return 'claude';
  if (/^(gpt|codex|openai|chatgpt)|^o\d([-.]|$)/.test(m)) return 'codex';
  if (/^(grok|xai)/.test(m)) return 'grok';
  if (OLLAMA_TAG.test(m)) return localFamily(m);
  if (/^(local|mlx|qwen|llama|gemma|mistral|deepseek|phi)/.test(m)) return 'local';
  return 'unknown';
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
    const outfit = role === 'orchestrator' ? 'suit' : 'worker';
    const backend = knownBackend(record.local_backend);
    const family = role === 'orchestrator' ? modelFamily(record.model, record.local_backend)
      : characterFamily(provider, record.lane, record.model, record.local_backend);
    agents.push({
      id, name, role,
      // avatarUrl: Claw3D's 2D panels show it as is. avatarProfile: its 3D voxel figure,
      // read only by a Claw3D carrying claw3d/avatar-profile-hint.patch; stock ignores it.
      identity: { name, emoji: role === 'orchestrator' ? '👔' : '🤖',
        avatarUrl: avatarDataUrl(family, outfit), avatarProfile: avatarProfile(family, outfit, id) },
      office: {
        floor: floor.session, project: text(floor.project), device: text(floor.device),
        provider, lane: text(record.lane), localBackend: backend, model: text(record.model), status,
        tool: text(record.tool), title: text(record.title),
        characterFamily: family, outfit, sprite: family + (outfit === 'suit' ? '-suit' : ''),
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
// Read methods Claw3D Studio calls while it connects and hydrates the fleet (upstream
// 51337f7: GatewayClient.connect -> config.get; agentFleetHydration -> exec.approvals.get,
// agents.files.get, sessions.preview; panels -> chat.history, models.list, skills.status,
// cron.list). Each answer is FIXED and empty: no transcript, file or config is ever
// served. config.get reports reload mode "off" so Studio never attempts config.patch.
const NEUTRAL_READS = {
  'config.get': () => ({ config: { gateway: { reload: { mode: 'off' } } }, hash: 'cortados-office-readonly', exists: true }),
  'exec.approvals.get': () => ({ path: '', exists: false, hash: 'cortados-office-readonly',
    file: { version: 1, defaults: {}, agents: {} } }),
  'agents.files.get': () => ({ file: { missing: true } }),
  'sessions.preview': params => ({ ts: 0, previews: (Array.isArray(params?.keys) ? params.keys : [])
    .filter(k => typeof k === 'string' && k.length <= 256).slice(0, 64).map(key => ({ key, status: 'empty', items: [] })) }),
  'chat.history': params => ({ sessionKey: typeof params?.sessionKey === 'string' ? params.sessionKey : '', messages: [] }),
  'models.list': () => ({ models: [] }),
  'skills.status': () => ({ skills: [] }),
  'cron.list': () => ({ jobs: [] }),
};
export const READ_METHODS = Object.freeze([...methods, ...Object.keys(NEUTRAL_READS)]);

/** Transport seam: send(object) emits a JSON frame to one authenticated client.
 * The WS host (bridge.mjs) owns localhost bind, Origin/auth checks, frame limits and clients.
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
    this.send(event('connect.challenge', { nonce: randomBytes(16).toString('hex') }));
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
        features: { methods: [...READ_METHODS], events: ['agent', 'presence', 'office.snapshot'] },
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
    else if (Object.hasOwn(NEUTRAL_READS, frame.method)) reply(NEUTRAL_READS[frame.method](frame.params));
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

/**
 * True when the module at `moduleUrl` is the script node was started with. Shared by adapter.mjs,
 * bridge.mjs and build-assets.mjs (all of which already import this file, so no new file to deploy).
 * Node resolves symlinks for the main module (and /var -> /private/var on macOS), so its import.meta.url
 * is the real path while process.argv[1] stays the path as given. The deploy runs the bridge through
 * the `adapter` symlink (/opt/cortados-office/adapter -> adapters/<commit>), so a plain URL comparison
 * silently skipped the main block there (exit 0, no output, launchd restart loop). Compare real paths.
 * A realpath error never silently skips the main block: it falls back to the plain path comparison
 * and, if that does not match either, says so on stderr.
 */
export function isMainModule(moduleUrl, argv1 = process.argv[1]) {
  if (!argv1) return false;
  const modulePath = fileURLToPath(moduleUrl);
  try {
    return realpathSync(argv1) === realpathSync(modulePath);
  } catch (error) {
    if (resolve(argv1) === modulePath) return true;
    process.stderr.write(`entry check: cannot resolve the start script (${error?.code ?? 'error'}); not run as main\n`);
    return false;
  }
}

if (isMainModule(import.meta.url)) {
  if (!process.argv.includes('--stdio') || !process.env.CORTADOS_OFFICE_TOKEN_FILE) {
    process.stderr.write('Diagnostic only: use --stdio and CORTADOS_OFFICE_TOKEN_FILE; the WebSocket host is bridge.mjs\n');
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
