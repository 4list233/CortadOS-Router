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

// ---- rooms (issue #77, FLOORS-DESIGN.md): one room per orchestrator, read from GET /v1/cortados/office/tree ----
// A finished child task stands at a report desk beside its orchestrator for this long, then leaves the office (owner
// decision E8, 2026-11-24). 10 min is the Office view's own keep time for finished work (office.py WORKER_KEEP_S): a
// glance at the floor still shows what just finished or failed, and a busy room's two report desks free up again.
export const RETURN_KEEP_S = 600;
// The nameplate subtitle of a finished task: its end state, then its model. claw3d/cortadosRooms.ts recognises exactly
// these prefixes (CORTADOS_RETURN_SUBTITLE) and seats the task at a report desk; a test keeps the two equal.
export const RETURN_LABEL = Object.freeze({ succeeded: 'done', failed: 'failed', cancelled: 'cancelled' });
export const RETURN_SEPARATOR = ' · ';
// The provider tree's caps (live.py NEST_MAX 4, ORCH_MAX 32, ORCH_TASKS_MAX 20, SUBSESSIONS_MAX 8, ORCH_ROUTED_MAX 8)
// bound it; these looser ones only stop a malformed body from recursing or growing without limit.
const TREE_DEPTH_MAX = 8;
const TREE_PEOPLE_MAX = 2048;
const GROUP_RE = /^[0-9a-f]{16,64}$/;          // a session's Office tag (office.py header rule)
const NAMED_ORIGIN_RE = /^[a-z]{2,15}$/;       // a named origin (child_tasks.ORIGIN_NAMED: grokbot): never a tag
const ID_RE = /^[A-Za-z0-9_-]{8,64}$/;         // task ids and sub-session refs (office._RUN_ID_RE)
const TASK_STATUSES = new Set(['running', 'waiting', 'done', 'failed', 'unknown']);
const SUB_STATUSES = new Set(['running', 'waiting', 'done', 'failed']);
// The harness of an orchestrator (provider live.WIRE_CLIENT / office.CLIENTS) -> its suit. Antigravity has no lane yet:
// the neutral `unknown` suit until it does (E7; TODO in FLOORS-DESIGN.md).
const CLIENT_FAMILY = new Map(Object.entries({ 'claude-code': 'claude', codex: 'codex', grok: 'grok',
  antigravity: 'unknown' }));
const CLIENT_SOURCES = new Set(['wire', 'heartbeat']);
const NAME_MAX = 64;
// The room of routed work with no orchestrator and no task (E9). A bot origin of the same name is refused (groupKey),
// so the key can never be two rooms.
const LOBBY_KEY = 'lobby';
const LOBBY_LABEL = 'No orchestrator';

// The host badge (#77 brand): where a person runs, as the provider's KNOWN host name (exec-hosts.json: air, pro, studio),
// shown at the end of the nameplate subtitle ("claude-opus-5 · Air"). A task's own exec host; an orchestrator's from
// its session's device (live.orchestrator_hosts); a helper runs where its task or session does; the lobby has none.
// Anything that is not a host slug (exec_hosts.HOST_NAME_RE) is no badge, never shown as given.
const HOST_RE = /^[a-z][a-z0-9-]{0,31}$/;
export const hostName = value => (typeof value === 'string' && HOST_RE.test(value) ? value : null);
export const hostLabel = host => (host === null ? null : host[0].toUpperCase() + host.slice(1));
const withHost = (role, host) => (host === null ? role : role + RETURN_SEPARATOR + hostLabel(host));
const shortText = (value, max = NAME_MAX) => {
  const s = text(value);
  return s === null ? null : (s.length > max ? s.slice(0, max - 1) + '…' : s);
};
const finite = value => typeof value === 'number' && Number.isFinite(value);

function checkStrings(node, fields, what) {
  for (const field of fields) {
    if (Object.hasOwn(node, field) && node[field] !== null && typeof node[field] !== 'string') {
      throw new Error('invalid office tree ' + what);
    }
  }
}

/** Orchestrator suit and where it came from (E7), first that applies: the bot origin; the provider's `client` (the
 * routed main loop's wire, else the session heartbeat's claim); else a guess from the model, marked `model-guess`. */
export function orchestratorFamily(group) {
  if (group.kind === 'bot') return { family: 'grok', source: 'bot' };
  if (CLIENT_FAMILY.has(text(group.client)) && CLIENT_SOURCES.has(group.client_source)) {
    return { family: CLIENT_FAMILY.get(group.client), source: group.client_source };
  }
  return { family: modelFamily(group.model), source: 'model-guess' };
}

/** The room's key in every id: the session tag, the named origin, or "unattributed". null: not a room of the fleet
 * tree (an "untagged" group exists only in a session's own view). */
function groupKey(group) {
  if (group.kind === 'unattributed' && group.origin === null) return 'unattributed';
  if (group.kind === 'session' && typeof group.origin === 'string' && GROUP_RE.test(group.origin)) return group.origin;
  if (group.kind === 'bot' && typeof group.origin === 'string' && NAMED_ORIGIN_RE.test(group.origin) &&
      group.origin !== 'unattributed' && group.origin !== LOBBY_KEY) return group.origin;
  if (group.kind === 'untagged') return null;
  throw new Error('invalid office tree group');
}

/** Whether a tree section (orchestrators, lobby) may replace the roster: status "ok", or "partial" when every error is
 * one task directory's own (source "task_dir": live.tasks_section; that task is still in the tree, its directory just
 * could not be peeked). Any other error is a whole source (the child-task log, the board, the conversations, the
 * heartbeats) whose people would vanish from the floor, so the section is unavailable and the poll fails. Reason: a
 * single unreadable task directory may last for days; freezing the floor on it would hide every other change. */
function treeUsable(section) {
  if (section.status === 'ok') return true;
  if (section.status !== 'partial' || !Array.isArray(section.errors) || section.errors.length === 0) return false;
  return section.errors.every(e => object(e) && e.source === 'task_dir');
}

/**
 * The provider's orchestrator tree -> Claw3D agents, one room per orchestrator, in agents.list order: rooms in the
 * tree's order (active first) with the unattributed room last; in each room the orchestrator, then each task
 * depth-first (a task, its live helpers, then its nested tasks), then the orchestrator's own routed helpers.
 *
 * Who is shown (E4, E8): every task that is running, waiting or unknown, at a team desk; each LIVE helper (a running
 * or waiting sub-session other than the task's own main loop, which the task figure already is) beside its task; a
 * finished task for RETURN_KEEP_S after it ended, at a report desk beside its orchestrator, its subtitle
 * "done · model" (status idle) or "failed · model" / "cancelled · model" (status error). A finished helper is not
 * shown. The unattributed room has no orchestrator: its finished tasks leave at once, and the room is drawn only while
 * someone in it is active. A session room is drawn while its orchestrator is live (working/idle) or anyone is in it.
 * The tree's `lobby` (E9: routed work with no orchestrator and no task) is the "No orchestrator" room, after the
 * unattributed one, drawn while one of its items is running or waiting. A tree whose orchestrators or lobby section is
 * unavailable (treeUsable) throws, as a malformed one does: the caller keeps the last roster.
 *
 * Pure: the same tree gives the same agents (time is the tree's own generated_at, the provider's clock).
 */
export function mapTree(tree) {
  if (!object(tree) || tree.version !== 1 || !finite(tree.generated_at) || !object(tree.orchestrators) ||
      !Array.isArray(tree.orchestrators.items)) throw new Error('invalid office tree');
  // An unavailable tree is a failed poll, never an empty floor (#77 review round 1: an HTTP 200 carrying
  // {status: "error", items: []} replaced 13 people with none): the last roster stays.
  if (!treeUsable(tree.orchestrators)) throw new Error('office tree unavailable');
  const lobby = tree.lobby === undefined ? null : tree.lobby;   // an older provider sends no lobby (E9)
  if (lobby !== null && (!object(lobby) || !Array.isArray(lobby.items))) throw new Error('invalid office tree lobby');
  if (lobby !== null && !treeUsable(lobby)) throw new Error('office tree unavailable');
  const now = tree.generated_at;
  const rooms = [];
  const ids = new Set();
  let people = 0;
  for (const g of tree.orchestrators.items) {
    if (!object(g)) throw new Error('invalid office tree group');
    checkStrings(g, ['project', 'device', 'host', 'status', 'model', 'tool', 'client', 'client_source'], 'group');
    if (!Array.isArray(g.tasks) || !Array.isArray(g.routed)) throw new Error('invalid office tree group');
    const key = groupKey(g);
    if (key === null) continue;
    const label = key === 'unattributed' ? 'Unattributed' : g.kind === 'bot' ? 'Grok Bot'
      : shortText(g.project) ?? 'Orchestrator';
    const room = [];
    const orchestratorId = key === 'unattributed' ? null : 'office-' + key + '-orchestrator';
    const push = person => {
      if (ids.has(person.id)) throw new Error('duplicate office identity');
      if (++people > TREE_PEOPLE_MAX) throw new Error('invalid office tree');
      ids.add(person.id);
      room.push(person);
    };
    const groupHost = hostName(g.host);
    const base = (id, kind, parent, depth, host = groupHost) => ({ id, kind, parent, depth, group: key,
      groupLabel: label, project: text(g.project), device: text(g.device), host });
    if (orchestratorId) {
      const { family, source } = orchestratorFamily(g);
      push({ ...base(orchestratorId, 'orchestrator', null, 0), name: label,
        role: withHost(shortText(g.model) ?? (g.kind === 'bot' ? 'bot gateway' : 'orchestrator'), groupHost),
        status: g.status === 'working' ? 'working' : 'idle', taskStatus: null, returning: false,
        family, familySource: source, outfit: 'suit', provider: null, lane: null, localBackend: null,
        model: text(g.model), tool: text(g.tool), title: null, client: CLIENT_FAMILY.has(text(g.client)) ? g.client : null });
    }
    const visit = (n, depth) => {
      if (depth > TREE_DEPTH_MAX || !object(n) || typeof n.task_id !== 'string' || !ID_RE.test(n.task_id) ||
          !TASK_STATUSES.has(n.status) || !Array.isArray(n.sub_sessions) || !Array.isArray(n.tasks) ||
          (n.ended_at !== null && n.ended_at !== undefined && !finite(n.ended_at))) {
        throw new Error('invalid office tree task');
      }
      checkStrings(n, ['title', 'state', 'phase', 'tool', 'host', 'provider', 'lane', 'local_backend', 'model',
        'parent_task_id'], 'task');
      const id = 'office-' + key + '-task-' + n.task_id;
      const parent = depth === 0 ? orchestratorId : 'office-' + key + '-task-' + n.parent_task_id;
      const finished = n.status === 'done' || n.status === 'failed';
      const model = shortText(n.model);
      const taskHost = hostName(n.host);
      const common = { ...base(id, 'task', parent, depth + 1, taskHost), name: shortText(n.title) ?? 'Task ' + n.task_id.slice(-8),
        taskStatus: n.status, provider: text(n.provider), lane: text(n.lane), localBackend: knownBackend(n.local_backend),
        model: text(n.model), tool: text(n.tool), title: text(n.title),
        family: characterFamily(n.provider, n.lane, n.model, n.local_backend), outfit: 'worker' };
      if (finished) {
        // back to its orchestrator for RETURN_KEEP_S, then gone; no orchestrator (unattributed): gone at once
        const end = finite(n.ended_at) ? n.ended_at : null;
        const state = Object.hasOwn(RETURN_LABEL, n.state) ? n.state : (n.status === 'failed' ? 'failed' : 'succeeded');
        if (orchestratorId && end !== null && now - end <= RETURN_KEEP_S) {
          push({ ...common, parent: orchestratorId,
            role: withHost(RETURN_LABEL[state] + RETURN_SEPARATOR + (model ?? 'model unknown'), taskHost),
            status: state === 'succeeded' ? 'idle' : 'error', returning: true, returnUntil: end + RETURN_KEEP_S,
            tool: null });
        }
      } else {
        push({ ...common, role: withHost(model ?? (n.status === 'unknown' ? 'state unknown' : 'model pending'), taskHost),
          status: n.status === 'running' ? 'working' : 'idle', returning: false });
        for (const s of n.sub_sessions) {
          if (!object(s) || typeof s.ref !== 'string' || !ID_RE.test(s.ref) || !SUB_STATUSES.has(s.status)) {
            throw new Error('invalid office tree helper');
          }
          checkStrings(s, ['role', 'phase', 'provider', 'lane', 'local_backend', 'model'], 'helper');
          // the task's own main loop is the task figure; a finished helper just leaves (E8)
          if (s.role === 'main' || (s.status !== 'running' && s.status !== 'waiting')) continue;
          push({ ...base('office-' + key + '-sub-' + n.task_id + '-' + s.ref, 'helper', id, depth + 2, taskHost),
            name: 'Helper · ' + (shortText(n.title, 48) ?? n.task_id.slice(-8)),
            role: withHost(shortText(s.model) ?? 'helper', taskHost),
            status: s.status === 'running' ? 'working' : 'idle', taskStatus: s.status, returning: false,
            provider: text(s.provider), lane: text(s.lane), localBackend: knownBackend(s.local_backend),
            model: text(s.model), tool: null, title: text(n.title),
            family: characterFamily(s.provider, s.lane, s.model, s.local_backend), outfit: 'worker' });
        }
      }
      for (const k of n.tasks) visit(k, depth + 1);
    };
    for (const n of g.tasks) visit(n, 0);
    for (const r of g.routed) {
      if (!object(r) || typeof r.ref !== 'string' || !ID_RE.test(r.ref) || !SUB_STATUSES.has(r.status)) {
        throw new Error('invalid office tree helper');
      }
      checkStrings(r, ['title', 'phase', 'tool', 'provider', 'lane', 'model'], 'helper');
      if (r.status !== 'running' && r.status !== 'waiting') continue;
      push({ ...base('office-' + key + '-routed-' + r.ref, 'helper', orchestratorId, 1),
        name: shortText(r.title) ?? 'Helper', role: withHost(shortText(r.model) ?? 'helper', groupHost),
        status: r.status === 'running' ? 'working' : 'idle', taskStatus: r.status, returning: false,
        provider: text(r.provider), lane: text(r.lane), localBackend: null, model: text(r.model), tool: text(r.tool),
        title: text(r.title), family: characterFamily(r.provider, r.lane, r.model), outfit: 'worker' });
    }
    const someoneElse = room.some(p => p.kind !== 'orchestrator');
    const live = g.status === 'working' || g.status === 'idle';
    if (key === 'unattributed' ? room.some(p => !p.returning) : (someoneElse || live)) {
      rooms.push({ key, people: room });
    }
  }
  // E9 (owner, 2026-11-24): routed work with no orchestrator and no task (one-off routed questions, the S1 outcome
  // loop) sits in the "No orchestrator" room, drawn last and only while someone is in it. Nobody heads it.
  if (lobby !== null) {
    const room = [];
    for (const r of lobby.items) {
      if (!object(r) || typeof r.ref !== 'string' || !ID_RE.test(r.ref) || !SUB_STATUSES.has(r.status)) {
        throw new Error('invalid office tree lobby');
      }
      checkStrings(r, ['kind', 'title', 'tool', 'provider', 'lane', 'local_backend', 'model'], 'lobby');
      if (r.status !== 'running' && r.status !== 'waiting') continue;
      const id = 'office-' + LOBBY_KEY + '-routed-' + r.ref;
      if (ids.has(id)) throw new Error('duplicate office identity');
      if (++people > TREE_PEOPLE_MAX) throw new Error('invalid office tree');
      ids.add(id);
      room.push({ id, kind: 'helper', parent: null, depth: 1, group: LOBBY_KEY, groupLabel: LOBBY_LABEL,
        project: null, device: null, host: null, name: shortText(r.title) ?? (r.kind === 'conversation' ? 'Conversation' : 'Helper'),
        role: shortText(r.model) ?? 'routed', status: r.status === 'running' ? 'working' : 'idle', taskStatus: r.status,
        returning: false, provider: text(r.provider), lane: text(r.lane), localBackend: knownBackend(r.local_backend),
        model: text(r.model), tool: text(r.tool), title: text(r.title),
        family: characterFamily(r.provider, r.lane, r.model, r.local_backend), outfit: 'worker' });
    }
    if (room.length) rooms.push({ key: LOBBY_KEY, people: room });
  }
  const last = key => (key === 'unattributed') + 2 * (key === LOBBY_KEY);
  rooms.sort((a, b) => last(a.key) - last(b.key));   // stable: tree order otherwise
  return rooms.flatMap((room, index) => room.people.map(p => toAgent(p, index)));
}

/** One person as a Claw3D agent: the stock fields Studio reads (name; role = the nameplate subtitle, E5) and the
 * CortadOS `office` extension. Allowlisted: nothing of the tree is spread into the output. */
function toAgent(p, roomIndex) {
  const emoji = p.kind === 'orchestrator' ? '👔' : p.kind === 'helper' ? '🛠️' : '🤖';
  return {
    id: p.id, name: p.name, role: p.role,
    identity: { name: p.name, emoji, avatarUrl: avatarDataUrl(p.family, p.outfit),
      avatarProfile: avatarProfile(p.family, p.outfit, p.id) },
    office: {
      floor: p.group, group: p.group, groupLabel: p.groupLabel, room: roomIndex, kind: p.kind, parent: p.parent,
      depth: p.depth, project: p.project, device: p.device, host: p.host, provider: p.provider, lane: p.lane,
      localBackend: p.localBackend, model: p.model, status: p.status, taskStatus: p.taskStatus,
      returning: p.returning, returnUntil: p.returnUntil ?? null, tool: p.tool, title: p.title,
      characterFamily: p.family, familySource: p.familySource ?? null, outfit: p.outfit,
      sprite: p.family + (p.outfit === 'suit' ? '-suit' : ''),
    },
  };
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
    this.errored = new Set();   // agents whose error Claw3D has been told (a run ended in error, or start + error)
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
  /** One poll's tree (GET /v1/cortados/office/tree): mapTree validates it before the last good roster is replaced. */
  update(tree) {
    const next = mapTree(tree); // validate before replacing the last good state
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
        if (a.office.status === 'error') this.errored.add(a.id);
      } else if (a.office.status === 'error' && !this.errored.has(a.id)) {
        // an error this gateway never saw running (a failed task first seen as it reports back, or a bridge that
        // restarted): Claw3D shows an error only after a run's error event, so the run is opened and failed at once
        const runId = a.id + ':' + (++this.generation);
        lifecycle(a, 'start', runId);
        lifecycle(a, 'error', runId);
        this.errored.add(a.id);
      }
      if (a.office.status !== 'error') this.errored.delete(a.id);
    }
    for (const [id, a] of old) {
      if (!next.some(n => n.id === id)) {
        this.errored.delete(id);
        if (this.runs.has(id)) {
          lifecycle(a, 'end', this.runs.get(id));
          this.runs.delete(id);
        }
      }
    }
    this.send(event('presence', this.presence()));
    // Extension for room/appearance/status and roster removal; stock UI may ignore.
    this.send(event('office.snapshot', { agents: next }));
  }
}

const OFFICE_PATH = '/v1/cortados/office';
export const TREE_PATH = '/v1/cortados/office/tree';
/** Only GET the exact office endpoint (or, with `path`, the tree beside it). Reject redirects to avoid credential
 * forwarding. */
export async function pollOffice({ url, tokenFile, fetchImpl = fetch, signal, path = OFFICE_PATH }) {
  let endpoint = new URL(url);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password ||
      endpoint.pathname !== OFFICE_PATH || endpoint.search || endpoint.hash ||
      ![OFFICE_PATH, TREE_PATH].includes(path)) {
    throw new Error('invalid office URL');
  }
  endpoint = new URL(path, endpoint);
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

/** GET the office's rooms: the exact tree endpoint beside the configured office URL (CORTADOS_OFFICE_URL names
 * /v1/cortados/office, as the deploy's token check does; the tree is /v1/cortados/office/tree on the same provider,
 * under the same office:read token). Same rules as pollOffice: no redirects, HTTPS off loopback, token from its file. */
export async function pollTree({ url, ...options }) {
  const endpoint = new URL(url);
  if (endpoint.pathname !== '/v1/cortados/office' || endpoint.search || endpoint.hash) throw new Error('invalid office URL');
  return pollOffice({ ...options, url, path: TREE_PATH });
}

export async function runPolling({ gateway, intervalMs = 5000, signal, onError = reportPollError, poll = pollTree,
  ...options }) {
  if (typeof onError !== 'function') throw new Error('error reporter must be a function');
  if (!Number.isFinite(intervalMs) || intervalMs < POLL_MIN_MS || intervalMs > POLL_MAX_MS) {
    throw new Error('poll interval must be 1..3600 seconds');
  }
  while (!signal?.aborted) {
    try { gateway.update(await poll({ ...options, signal })); }
    catch {
      if (signal?.aborted) break;
      // Never log exceptions: fetch/read errors may contain URLs, paths or tokens. Never fall back to the flat
      // office view either (FLOORS-DESIGN.md): the rooms stay as last drawn, and this says so.
      onError('Office tree unavailable; last successful roster retained (may be stale)');
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
