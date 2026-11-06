/**
 * cortados-routed-subagents
 *
 * Every subagent the session spawns is intercepted at the Agent tool call and run instead as a headless `claude`
 * subprocess ON THIS MACHINE, pointed at the CortadOS provider (ANTHROPIC_BASE_URL). The provider scores the task
 * with S1 and runs the chosen model headless on the provider host; that model's tool calls are bridged back and
 * executed by this subprocess, here, against this machine's files. The main session keeps its own direct model.
 *
 * Claude Code has no per-subagent provider endpoint (an agent.spawn hook or AgentSpec can set model/tools/mode but
 * not a base URL), so a subprocess is the only way to route a subagent while the parent stays direct.
 *
 * Passes through to the native in-process subagent when the work cannot or should not be routed:
 *   - fork: inherits the parent's context and model; it cannot run on another provider.
 *   - isolation (worktree/remote): the native runner owns the worktree/remote semantics; routing locally would
 *     silently drop them and let parallel isolated agents clobber one another.
 *   - a named agent: the parent registers it for SendMessage; a subprocess cannot join that registry.
 *   - background (run_in_background): can outlive the subprocess timeout (subagentTimeoutMs), unless routeBackground is on.
 *   - no provider token available: better a working native subagent than a failed call.
 *
 * Once the child has been launched it has already acted, so the hook NEVER falls back to native after that point
 * (that would re-run non-idempotent work). Any failure past the launch becomes a clean `deny`, not a re-dispatch.
 */
import type { Register } from 'claude-code'

type Cfg = {
  providerUrl: string
  providerTokenFile: string
  providerToken: string
  model: string
  permissionMode: string
  allowedTools: string
  claudeBin: string
  routeBackground: boolean
  orchestratorMode: boolean
  orchestratorEnforce: boolean
  orchestratorAllowedTools: string
  subagentTimeoutMs: number
  maxTurns: number
  excludeBuilderOnReview: boolean
  mainSessionGuard: boolean
}

const FALLBACK: Cfg = {
  providerUrl: '',
  providerTokenFile: '~/.config/cortados/provider-token',
  providerToken: '',
  model: 'cortados',
  permissionMode: 'bypassPermissions',
  allowedTools: 'Read,Write,Edit,MultiEdit,Glob,Grep,Bash,WebSearch,WebFetch,TodoWrite,NotebookEdit',
  claudeBin: 'claude',
  routeBackground: false,
  // Orchestrator mode: the main session (the "brain") is DIRECTED to plan and delegate work to routed subagents,
  // via a standing system-prompt directive. By default this is GUIDANCE, not a cage: the brain keeps its tools and
  // may act directly when that is clearly the sensible choice (a trivial edit, its own bookkeeping) — delegation is
  // the strong default, not an absolute rule. Turn on `orchestratorEnforce` to additionally HARD-BLOCK every tool
  // outside `orchestratorAllowedTools` on the main loop (an allowlist, so even Monitor / memory_write / mcp__ tools /
  // future tools are refused) for a strict brain that cannot act even if it tried.
  orchestratorMode: true,
  orchestratorEnforce: false,
  // route_task is a delegation primitive too (it hands a self-contained text subtask to the router and returns the
  // answer), so it belongs with Agent in the brain's allowed set: a lightweight text delegation should not have to pay
  // for a full routed-subagent subprocess. Listed here for visibility; ALWAYS_ALLOWED guarantees it regardless of config.
  orchestratorAllowedTools: 'Read,Grep,Glob,TodoWrite,Agent,mcp__cortados-route__route_task,AskUserQuestion,ExitPlanMode,Skill,ToolSearch',
  // How long a routed subagent subprocess may run before it is aborted. The default is the host's own ceiling
  // (MAX_SUBAGENT_TIMEOUT_MS): a larger value is rejected by $.process.run and the subagent never launches.
  // Background routed work is separate.
  subagentTimeoutMs: 600_000,
  // `claude -p --max-turns N`: the child stops after N agentic turns instead of looping until the timeout kills it
  // (incident 2026-11-03: a research child made 70 tool calls against a broken WebSearch, ran 600 s, cost $1.57 and
  // returned nothing). OPT-IN (0 = off): there is no evidence yet that any blanket budget is safe for builders, whose
  // legitimate runs can need many turns, and a stopped builder has acted but not finished. The partial-result
  // handback (see partialFromTranscript) and the stop-on-dead-tool prompt line cover the incident without it.
  maxTurns: 0,
  // A review subagent is kept off the MAIN SESSION's own model AND the models routed (non-review) subagents ran on
  // since the last review (x-cortados-exclude-model, a comma-separated list; see workerModels).
  excludeBuilderOnReview: true,
  // Warn-only main-session guard (see GUARD_* below): a toast + status counter when the main loop does a big edit or
  // a build/test run itself. Never blocks. /cortados-guard on|off overrides this, persisted in $.store.
  mainSessionGuard: true,
}

// The host API's own limit: Claude Code (seen in 2.1.314) validates $.process.run's timeoutMs as an integer in
// 1..600000, and rejects anything outside it, so a routed subagent with a larger timeout fails to launch at all.
// resolveTimeoutMs clamps into this range rather than passing an out-of-range value through.
const MIN_SUBAGENT_TIMEOUT_MS = 1
// A retry with less than this left cannot start `claude -p` and finish a turn; it would only fail on its own timeout.
const MIN_RETRY_MS = 30_000
const MAX_SUBAGENT_TIMEOUT_MS = 600_000

// Delegation must never be gated off, whatever the allowlist config says, or the brain could not delegate at all.
// Both the Agent tool and the route_task MCP tool are delegation primitives.
const ALWAYS_ALLOWED = ['Agent', 'mcp__cortados-route__route_task']

// Host-app plumbing, not work: the Claude desktop app's turn tools that the main loop must call itself (a subagent
// cannot update the parent's plan or end its turn). Seen 2026-10-30: the strict gate refused wrap_up and the desktop
// turn showed "Wrapping up — Failed". So even an enforced gate lets these through. Exact names, not a prefix: a
// prefix such as `mcp__ccd_` would let any similarly named MCP server (e.g. `ccd_external`) bypass the gate.
const HOST_PLUMBING_TOOLS: ReadonlySet<string> = new Set([
  'mcp__ccd_turn__update_plan',
  'mcp__ccd_turn__wrap_up',
  'mcp__ccd_session__mark_chapter',
])

/** The standing directive injected into the orchestrator's system prompt so it delegates by default, every session.
 * Keep in step with integrations/orchestrator-directive.md (the whole-session launcher's copy, adapted: there
 * subagents DO have MCP tools). The plugin is paused; the two must match in intent. */
const ORCHESTRATOR_DIRECTIVE =
  'You are operating as an ORCHESTRATOR — the planning "brain" of a routed system. Your job is to decide and ' +
  'DELEGATE: decompose the request into self-contained subtasks and hand EACH to a subagent via the Agent tool (or a ' +
  'self-contained text subtask via route_task). Those subagents are automatically routed to the CortadOS model pool ' +
  'and carry out the work on this machine, so pushing work to them — instead of doing it yourself — is the whole ' +
  'point of this mode: it keeps the expensive brain model free and runs each piece on the cheapest capable model. ' +
  'DELEGATE BY DEFAULT for anything substantive: editing or writing files beyond a trivial touch, running commands, ' +
  'building, broad searching, web access, long analyses. You MAY act directly when delegating would clearly be ' +
  'wasteful or slower — a one-line edit, your own planning and bookkeeping, a quick read or check — but treat that ' +
  'as the exception, and never let it become a habit of quietly doing the work yourself. IMPORTANT: routed subagents ' +
  'have ONLY built-in tools (files, shell, web search/fetch) — no MCP tools: no browser/Chrome, no connectors (Slack, ' +
  'Gmail, Drive, …), no app tools. Any step that needs one of those (e.g. driving the signed-in browser) cannot be ' +
  'delegated; do that step yourself and delegate only the rest. Give each subagent a ' +
  'complete, standalone task (it cannot see this conversation or your context). When subagents report back, ' +
  'synthesize their results and plan the next step.'

/** Split a comma/space-separated tool list into clean names. */
export function parseList(s: string): string[] {
  return String(s)
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean)
}

/** A well-formed tool name: built-ins are PascalCase alphanumerics, MCP tools are mcp__server__tool — both are
 * [A-Za-z0-9_] only. Anything else (brackets, pipes, punctuation from a fat-fingered config) is not a tool name. */
export function validToolName(s: string): boolean {
  return /^[A-Za-z0-9_]+$/.test(s)
}

/** Build the main-loop allowlist from config: drop malformed tokens, and fall back to the default set when nothing
 * valid remains (so a mistyped value can never silently block reading and delegation). */
export function resolveAllowSet(configStr: string): Set<string> {
  const parsed = parseList(configStr).filter(validToolName)
  return new Set(parsed.length ? parsed : parseList(FALLBACK.orchestratorAllowedTools))
}

/** Whether a tool call may run on the MAIN loop under orchestrator mode. True = allow, false = refuse (delegate).
 * Off-mode and any non-main loop (agentId present) always allow; Agent is always allowed so delegation works. */
export function mainLoopAllows(tool: string, agentId: string | null | undefined, allow: ReadonlySet<string>, orchestratorMode: boolean): boolean {
  if (!orchestratorMode) return true
  if (agentId != null) return true // not the main loop (subagent/teammate/engine fork)
  return ALWAYS_ALLOWED.includes(tool) || HOST_PLUMBING_TOOLS.has(tool) || allow.has(tool)
}

/** Appended to every routed child's prompt: a child stuck on a broken tool should report, not burn its budget. */
export const STOP_ON_DEAD_TOOL = 'If a tool returns nothing useful twice in a row, stop and report what you have and what failed.'

/** The --max-turns value from config: a positive integer, or null (flag off) for 0/negative/invalid. */
export function resolveMaxTurns(v: unknown): number | null {
  const n = typeof v === 'number' || typeof v === 'string' ? Number(v) : NaN
  if (!Number.isFinite(n) || n < 1) return null
  return Math.floor(n)
}

/** A fresh v4 UUID for the child's --session-id (the CLI requires a UUID), so its transcript can be found on failure. */
export function newSessionId(): string {
  const c: any = (globalThis as any).crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  const h = (n: number) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('')
  return `${h(8)}-${h(4)}-4${h(3)}-${'89ab'[Math.floor(Math.random() * 4)]}${h(3)}-${h(12)}`
}

/** A lowercase v4 UUID exactly as newSessionId makes it. The session id goes into a shell command (as a positional
 * argument, never spliced into the script), and this strict check is the second guard: nothing else can reach it. */
export function isSessionUuid(s: unknown): s is string {
  return typeof s === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(s)
}

/** The fixed shell script that finds a session transcript by its UUID under the CLI's projects dir. Located by name,
 * not by deriving the project folder from the cwd: Claude Code hashes long project-folder names, so a slug of the cwd
 * misses them. $1 is the (validated) UUID; depth 2 is projects/<folder>/<uuid>.jsonl. */
export const FIND_TRANSCRIPT_SH =
  'find "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects" -maxdepth 2 -type f -name "$1.jsonl" 2>/dev/null | head -n 1'

/** Milliseconds as whole seconds for a message ("600 s"), so a 600000 ms timeout never reads as "60000s". */
export function fmtSecs(ms: number): string {
  return `${Math.round(ms / 1000)} s`
}

export type PartialRun = { lastText: string | null; toolUses: number; assistantTurns: number }

/** The last assistant text and how far the child got (tool calls, assistant turns), from a session transcript jsonl.
 * The CLI writes one record per content block, so records sharing a message.id are ONE assistant turn (a record
 * without an id counts on its own); every tool_use block is still counted, and the text blocks of the last turn are
 * joined. Malformed lines are skipped (the file may be cut mid-line by the kill). */
export function partialFromTranscript(jsonl: string): PartialRun {
  let lastText: string | null = null
  let lastTextId: string | null = null
  let toolUses = 0
  let anonymousTurns = 0
  const ids = new Set<string>()
  for (const line of jsonl.split('\n')) {
    const t = line.trim()
    if (!t.startsWith('{')) continue
    let d: any
    try { d = JSON.parse(t) } catch { continue }
    if (!d || d.type !== 'assistant' || !d.message) continue
    const id = typeof d.message.id === 'string' && d.message.id ? d.message.id : null
    if (id) ids.add(id)
    else anonymousTurns += 1
    const content = Array.isArray(d.message.content) ? d.message.content : []
    const texts: string[] = []
    for (const b of content) {
      if (b && b.type === 'tool_use') toolUses += 1
      else if (b && b.type === 'text' && typeof b.text === 'string' && b.text.trim()) texts.push(b.text)
    }
    if (texts.length) {
      // Another block of the same turn extends its text; a new turn replaces it.
      lastText = id && id === lastTextId && lastText ? `${lastText}\n${texts.join('\n')}` : texts.join('\n')
      lastTextId = id
    }
  }
  return { lastText, toolUses, assistantTurns: ids.size + anonymousTurns }
}

/** The last brace-delimited line of `claude -p --output-format json` output (warnings may precede it). */
export function lastJsonLine(stdout: string): string | null {
  const lines = stdout.split('\n').map((l) => l.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].startsWith('{') && lines[i].endsWith('}')) return lines[i]
  }
  return null
}

export type ParsedRun = {
  text: string | null
  model: string | null
  isError: boolean
  usage: Record<string, unknown>
  totalTokens: number
  totalToolUseCount: number
  totalDurationMs: number
  sessionId: string | null
}

/** Pull result text, the model that ran, and the counters the Agent tool's result schema wants, out of a
 * `claude -p --output-format json` reply. */
export function parseResult(stdout: string): ParsedRun {
  const empty: ParsedRun = { text: null, model: null, isError: false, usage: {}, totalTokens: 0, totalToolUseCount: 0, totalDurationMs: 0, sessionId: null }
  const line = lastJsonLine(stdout)
  if (!line) return empty
  try {
    const d = JSON.parse(line) as Record<string, any>
    // Only finite numbers; Infinity/NaN (e.g. JSON 1e400) would be rejected by the CLI's numeric validator.
    const n = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
    const usage = d.usage && typeof d.usage === 'object' ? (d.usage as Record<string, unknown>) : {}
    const u = usage as Record<string, unknown>
    const sum = n(u.input_tokens) + n(u.output_tokens) + n(u.cache_read_input_tokens) + n(u.cache_creation_input_tokens)
    const totalTokens = Number.isFinite(sum) ? sum : 0 // guard overflow to Infinity from huge finite terms
    const mu = d.modelUsage && typeof d.modelUsage === 'object' ? Object.keys(d.modelUsage)[0] : undefined
    return {
      text: typeof d.result === 'string' ? d.result : null,
      model: (typeof d.model === 'string' && d.model) || mu || null,
      isError: Boolean(d.is_error),
      usage,
      totalTokens,
      totalToolUseCount: n(d.num_turns),
      totalDurationMs: n(d.duration_ms),
      sessionId: typeof d.session_id === 'string' ? d.session_id : null,
    }
  } catch {
    return empty
  }
}

/** A completed routed run only when the child exited cleanly AND produced usable text: nonzero exit, a terminal
 * error, truncated stdout, or empty text all mean "not clean", so the caller refuses instead of returning a
 * result (which, for a child that already acted, must never fall back to a native re-run). The usage object is
 * not gated here because normalizeUsage() always builds a schema-valid one. */
export function cleanCompletion(exitCode: number, isStdoutTruncated: boolean, p: ParsedRun): boolean {
  return exitCode === 0 && !p.isError && !isStdoutTruncated && typeof p.text === 'string' && p.text.length > 0
}

/** The Agent result's `usage` object, every required key present (nullable ones may be null). */
export type AgentUsage = {
  input_tokens: number
  output_tokens: number
  cache_creation_input_tokens: number | null
  cache_read_input_tokens: number | null
  server_tool_use: { web_search_requests: number; web_fetch_requests: number } | null
  service_tier: string | null
  cache_creation: { ephemeral_1h_input_tokens: number; ephemeral_5m_input_tokens: number } | null
}

/** Coerce whatever the child reported into the exact usage shape the Agent result schema requires, so a completed
 * result is always accepted by core (a rejected result would be dropped and the task re-run natively). */
export function normalizeUsage(raw: unknown): AgentUsage {
  // Finite only: a non-finite number (Infinity/NaN) in the result's usage is rejected by the CLI's validator.
  const num = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) ? x : 0)
  const numOrNull = (x: unknown): number | null => (typeof x === 'number' && Number.isFinite(x) ? x : null)
  const obj = (x: unknown): Record<string, unknown> | null =>
    x && typeof x === 'object' && !Array.isArray(x) ? (x as Record<string, unknown>) : null
  const u = obj(raw) ?? {}
  const stu = obj(u.server_tool_use)
  const cc = obj(u.cache_creation)
  return {
    input_tokens: num(u.input_tokens),
    output_tokens: num(u.output_tokens),
    cache_creation_input_tokens: numOrNull(u.cache_creation_input_tokens),
    cache_read_input_tokens: numOrNull(u.cache_read_input_tokens),
    server_tool_use: stu
      ? { web_search_requests: num(stu.web_search_requests), web_fetch_requests: num(stu.web_fetch_requests) }
      : null,
    service_tier: typeof u.service_tier === 'string' ? u.service_tier : null,
    cache_creation: cc
      ? { ephemeral_1h_input_tokens: num(cc.ephemeral_1h_input_tokens), ephemeral_5m_input_tokens: num(cc.ephemeral_5m_input_tokens) }
      : null,
  }
}

/** Replace the provider token with *** anywhere it appears in model-visible text. Any non-empty token is scrubbed
 * regardless of length — a configured credential is redacted even if short; mangling a coincidental word is an
 * acceptable price for never surfacing the secret. (An empty token is a no-op; split('') would shatter the string.) */
export function scrubToken(s: string, token: string): string {
  return token ? s.split(token).join('***') : s
}

/** Whether a delegated subagent is a review. A matching review is kept off the main session's own model and the
 * models routed subagents ran on since the last review (see modelsToExclude). */
export function isReview(description: string, subagentType: string): boolean {
  return /\breview/i.test(String(description ?? '')) || /review/i.test(String(subagentType ?? ''))
}

/** A model id the provider accepts in x-cortados-exclude-model — exactly the server's rule: 1..128 chars of
 * [A-Za-z0-9._/[]:-]. Anything else (CR/LF that could inject a header, spaces, non-ASCII) is refused here, since the
 * provider would answer it with HTTP 400 anyway. */
export function headerSafeModel(model: unknown): boolean {
  return typeof model === 'string' && model.length <= 128 && /^[A-Za-z0-9._\/\[\]:-]+$/.test(model)
}

/** When a review run that sent x-cortados-exclude-model failed with the provider's own 400 refusal of that header (not a
 * served model id, not a model id at all, or excluding it left nothing usable), a short reason; otherwise null (no
 * retry). Only the provider's exact refusal phrasings next to a 400 count: a mere mention of the header name is not
 * enough, since a review of code that talks about the header quotes it (Sonnet 5 review, 2026-11-01). */
export function excludeRefusalReason(text: string): string | null {
  if (!/\b400\b|invalid_request_error/.test(text)) return null
  // Each phrasing is matched in its single- and multi-model form ("a" or "a, b": the provider joins names with ", ").
  if (/x-cortados-exclude-model: \S+(?:, \S+)* names no model this endpoint serves/.test(text)) return 'it serves no model by that name'
  if (/x-cortados-exclude-model is not a model id/.test(text)) return 'header refused'
  if (/\(the client excluded \S+(?:, \S+)* via x-cortados-exclude-model\)/.test(text)) return 'no other usable model'
  // The mod's own `model` option is a sub/ or api/ pin on the excluded model.
  if (/\bis the excluded model \(x-cortados-exclude-model\)/.test(text)) return 'this mod is pinned to that model'
  return null
}

/** The provider's cap on x-cortados-exclude-model entries. */
const MAX_EXCLUDED_MODELS = 8
/** How many worker models are remembered between reviews. */
const MAX_WORKER_MODELS = 3

/** Models the routed NON-review subagents built with since the last review (distinct, most recent last, at most
 * MAX_WORKER_MODELS), from each run's provider record (see builderModelsOf). Each entry carries the sequence number of
 * its latest tracking, so a review forgets only what it was kept off AS OF ITS DISPATCH — a worker that finishes on
 * the same model mid-review re-tracks it with a newer seq and survives. Module-level, so it resets on mod reload —
 * acceptable: a reload only loses the exclusion for the next review, it never mis-routes anything. */
let workerModels: Array<{ model: string; seq: number }> = []
let workerSeq = 0

/** Test hook: forget the tracked worker models. */
export function resetWorkerModels(): void {
  workerModels = []
}

/** Note the models a non-review run built with: each moves to the end (most recent) with a fresh seq, the oldest
 * drop past the cap. Pass builderModelsOf(record), so the run's primary model comes last and is the last to be evicted. */
export function trackWorkerModels(models: readonly string[]): void {
  for (const m of models) {
    if (!headerSafeModel(m) || m === 'cortados') continue
    workerModels = workerModels.filter((x) => x.model !== m)
    workerModels.push({ model: m, seq: (workerSeq += 1) })
  }
  if (workerModels.length > MAX_WORKER_MODELS) workerModels = workerModels.slice(-MAX_WORKER_MODELS)
}

/** The highest seq tracked so far (a review's dispatch-time watermark for untrackWorkerModels). */
export function workerModelsWatermark(): number {
  return workerSeq
}

/** Forget the given models (those a completed review was kept off) as tracked at or before `upToSeq` (the review's
 * dispatch-time watermark), keeping any tracked later — e.g. by a worker that finished, even on the same model,
 * while the review ran. */
export function untrackWorkerModels(models: readonly string[], upToSeq: number = Infinity): void {
  workerModels = workerModels.filter((e) => !(e.seq <= upToSeq && models.includes(e.model)))
}

/** The models a run built with, primary LAST: the record's `builder_models` (models of its tool-bearing requests)
 * when the provider sends them, else just the primary `model` — never the side calls (title/summary one-shots)
 * in `models`, which would otherwise evict the real builder from the capped list. */
export function builderModelsOf(record: RunRecord): string[] {
  const primary = record.model
  const out = (record.builder_models ?? []).filter((m) => m !== primary)
  if (primary) out.push(primary)
  return out
}

/** The tracked worker models (a copy). */
export function trackedWorkerModels(): string[] {
  return workerModels.map((e) => e.model)
}

/** The main session's own model when it can be excluded, else null with the reason it can't. */
async function sessionModelToExclude($: any): Promise<{ model: string | null; problem: string | null }> {
  let model: unknown
  try {
    model = await $.session.model()
  } catch {
    model = ''
  }
  if (typeof model !== 'string' || !model) return { model: null, problem: 'unreadable' }
  if (model === 'cortados') return { model: null, problem: 'routed' }
  if (!headerSafeModel(model)) return { model: null, problem: 'unsafe' }
  return { model, problem: null }
}

/** The models to keep a review off: the main session's own model plus the tracked worker models (distinct, each
 * header-safe, at most MAX_EXCLUDED_MODELS). Toasts when the session model is left out; the "not enforced" toasts
 * only when NOTHING can be excluded. */
async function modelsToExclude($: any): Promise<string[]> {
  const { model, problem } = await sessionModelToExclude($)
  const out: string[] = []
  for (const m of [model, ...workerModels.map((e) => e.model)]) {
    if (m && headerSafeModel(m) && m !== 'cortados' && !out.includes(m)) out.push(m)
  }
  const list = out.slice(0, MAX_EXCLUDED_MODELS)
  if (!list.length) {
    if (problem === 'unreadable') $.ui.toast('routed-subagents: could not read the session model; review not kept off it')
    else if (problem === 'routed') $.ui.toast("routed-subagents: this session is itself routed (model 'cortados'); the review's independence from the builder can't be enforced")
    else if (problem === 'unsafe') $.ui.toast('routed-subagents: session model id is not a safe header value; review not kept off it')
  } else if (problem === 'unreadable' || problem === 'unsafe') {
    $.ui.toast(`routed-subagents: could not use the session model; review kept off the worker models only (${list.join(', ')})`)
  }
  return list
}

/** A fresh id for one routed run, matching the provider's ^[A-Za-z0-9_-]{8,64}$ (not a secret). */
export function newRunId(): string {
  return ('r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)).replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64)
}

export type RunRecord = { label: string | null; model: string | null; models: string[]; builder_models: string[] | null }

/** Parse the provider's run record body; null when it is not a usable record. Labels/models are kept to one short
 * printable line (they end up in the model-visible result header). */
export function parseRunRecord(text: string): RunRecord | null {
  let d: any
  try {
    d = JSON.parse(text)
  } catch {
    return null
  }
  if (!d || typeof d !== 'object' || Array.isArray(d)) return null
  const line = (x: unknown): string | null => {
    if (typeof x !== 'string') return null
    const s = x.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim().slice(0, 200)
    return s || null
  }
  const models = Array.isArray(d.models) ? d.models.filter((m: unknown) => headerSafeModel(m)) as string[] : []
  // builder_models: absent (null) on an older provider.
  const builder_models = Array.isArray(d.builder_models) ? d.builder_models.filter((m: unknown) => headerSafeModel(m)) as string[] : null
  return { label: line(d.label), model: line(d.model), models, builder_models }
}

/** Fetch the provider's record of one routed run, or null on any failure (never throws). $.http.fetch takes no
 * timeout, so it races a short timer. */
async function fetchRunRecord($: any, cfg: Cfg, token: string, runId: string): Promise<RunRecord | null> {
  let timer: any
  try {
    const url = String(cfg.providerUrl).replace(/\/+$/, '') + '/v1/cortados/runs/' + encodeURIComponent(runId)
    const req = Promise.resolve($.http.fetch(url, { method: 'GET', headers: { 'x-api-key': token, Authorization: `Bearer ${token}` } }))
    req.catch(() => {}) // a late rejection after the timeout must not go unhandled
    const timeout = new Promise<null>((resolve) => { timer = setTimeout(() => resolve(null), RUN_RECORD_TIMEOUT_MS) })
    const res: any = await Promise.race([req, timeout])
    if (!res || !res.ok || typeof res.text !== 'string') return null
    return parseRunRecord(res.text)
  } catch {
    return null
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

const RUN_RECORD_TIMEOUT_MS = 3_000

let routedSeq = 0

/** Explicit plugin config stays an override; otherwise env > selected file > per-machine file. */
export async function resolveProviderUrl($: any, configured = ''): Promise<string> {
  let source = 'providerUrl config'
  let value = String(configured).trim()
  if (!value) {
    const result = await $.process.run(['sh', '-c',
      'printf "%s\\0%s\\0%s" "$CORTADOS_PROVIDER_URL" "$CORTADOS_PROVIDER_URL_FILE" "$HOME"'])
    if (result.exitCode !== 0) throw new Error('provider URL environment could not be read')
    const [envUrl, envFile, home] = String(result.stdout).split('\0')
    value = (envUrl || '').trim()
    source = 'CORTADOS_PROVIDER_URL'
    if (!value) {
      let path = envFile || '~/.config/cortados/provider-url'
      if (path.startsWith('~/')) path = home + path.slice(1)
      source = 'provider URL file ' + path
      try { value = String(await $.fs.read(path)).trim() }
      catch { throw new Error(source + ': cannot read URL') }
    }
  }
  try {
    const parsed = new URL(value)
    if (!/^https?:\/\//.test(value) || !parsed.hostname || /[\s\x00-\x1f\x7f]/.test(value)) throw new Error()
  } catch { throw new Error(source + ': expected one http:// or https:// URL with a host') }
  return value.replace(/\/+$/, '')
}

/** Read the provider token: the inline override if set, otherwise the token file (with ~ expanded to $HOME). */
async function resolveToken($: any, cfg: Cfg): Promise<string> {
  if (cfg.providerToken && String(cfg.providerToken).trim()) return String(cfg.providerToken).trim()
  let path = String(cfg.providerTokenFile)
  if (path.startsWith('~')) {
    const home = await $.process.run(['sh', '-c', 'printf %s "$HOME"'])
    path = String(home.stdout) + path.slice(1)
  }
  return (await $.fs.read(path)).trim()
}

/** The subagent timeout from config as an integer the host accepts (1..600000 ms, see MAX_SUBAGENT_TIMEOUT_MS):
 * a non-number, NaN or non-positive value falls back to the default (a mistyped value never disables the cap), a
 * fractional value is floored (the host requires an integer), and anything above the host's cap is clamped to it. */
export function resolveTimeoutMs(v: unknown): number {
  const n = typeof v === 'number' || typeof v === 'string' ? Number(v) : NaN // JSON config may deliver a string
  if (Number.isNaN(n) || n <= 0) return FALLBACK.subagentTimeoutMs
  return Math.min(MAX_SUBAGENT_TIMEOUT_MS, Math.max(MIN_SUBAGENT_TIMEOUT_MS, Math.floor(n)))
}

// ---- Main-session guard (WARN ONLY) ------------------------------------------------------------------------------
// The owner runs the main session on direct Claude and routes every subagent; the guard nudges the main session when
// it does substantive work itself. It NEVER blocks, denies or rewrites a call (the owner chose "warn only"): every
// guarded call goes to next(e) unchanged, and any failure in the guard's own bookkeeping is swallowed.

/** An Edit/Write/NotebookEdit whose NEW content reaches either limit is "substantive". 40 lines is about one function
 * or a screenful: the orchestrator directive's "trivial touch" (a one-line fix, a config tweak, a short docstring) stays
 * far under it, while writing a new module or rewriting a block goes over. 2000 characters catches what is long but has
 * few lines (minified JSON, long prose paragraphs, a generated table) that the line count alone would miss. */
export const GUARD_EDIT_MAX_LINES = 40
export const GUARD_EDIT_MAX_CHARS = 2000
/** At most one guard toast per 10 minutes: one nudge per stretch of work is enough to be seen, and a toast on every
 * edit of a long session would be noise the owner learns to ignore. The status-line counter still updates each time. */
export const GUARD_TOAST_INTERVAL_MS = 10 * 60_000
/** $.store key of the /cortados-guard on|off override; when unset the userConfig `mainSessionGuard` decides. */
export const GUARD_STORE_KEY = 'mainSessionGuard'
export const GUARD_COMMAND = 'cortados-guard'
const GUARD_TOOLS = ['Edit', 'Write', 'NotebookEdit', 'Bash'] as const

/** The new content a file-writing call carries: Edit's new_string, Write's content, NotebookEdit's new_source. */
export function newContentOf(tool: string, e: any): string {
  const v = tool === 'Edit' ? e?.new_string : tool === 'Write' ? e?.content : tool === 'NotebookEdit' ? e?.new_source : ''
  return typeof v === 'string' ? v : ''
}

/** Whether new content is a "big edit": over GUARD_EDIT_MAX_LINES lines or GUARD_EDIT_MAX_CHARS characters. */
export function isBigEdit(content: string): boolean {
  if (!content) return false
  return content.split('\n').length > GUARD_EDIT_MAX_LINES || content.length > GUARD_EDIT_MAX_CHARS
}

/** A build or test run: python -m unittest/pytest, pytest, make, npm/yarn/pnpm build or test. Checked per command
 * segment (split on ; & | ( ) and newlines) after leading VAR=value assignments, so `cd x && make` matches while
 * `grep make Makefile` or `echo pytest` does not. A heuristic on the command text, not a parse of the shell. */
export function isBuildOrTest(command: string): boolean {
  for (const raw of String(command ?? '').split(/[;&|()\n]+/)) {
    const seg = raw.trim().replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, '')
    if (/^python[0-9.]*\s+(?:-\S+\s+)*-m\s+(?:unittest|pytest)\b/.test(seg)) return true
    if (/^(?:pytest|py\.test)(?:\s|$)/.test(seg)) return true
    if (/^g?make(?:\s|$)/.test(seg)) return true
    if (/^(?:npm|yarn|pnpm)\s+(?:run\s+)?(?:build|test)\b/.test(seg)) return true
  }
  return false
}

/** The kind of substantive work a main-session call is, or null when it is not substantive. */
export function substantiveKind(tool: string, e: any): 'edit' | 'run' | null {
  if (tool === 'Bash') return isBuildOrTest(e?.command) ? 'run' : null
  return isBigEdit(newContentOf(tool, e)) ? 'edit' : null
}

/** The guard's status line: "main did 3 big edits, 1 build/test run", or undefined when nothing was counted. */
export function guardStatusText(edits: number, runs: number): string | undefined {
  const parts: string[] = []
  if (edits) parts.push(`${edits} big edit${edits === 1 ? '' : 's'}`)
  if (runs) parts.push(`${runs} build/test run${runs === 1 ? '' : 's'}`)
  return parts.length ? `main did ${parts.join(', ')}; delegate to routed subagents` : undefined
}

/** The guard's toggle from /cortados-guard's argument: true/false, or null for anything else. */
export function parseGuardArg(args: string): boolean | null {
  const a = String(args ?? '').trim().toLowerCase()
  if (a === 'on') return true
  if (a === 'off') return false
  return null
}

export type GuardState = {
  edits: number
  runs: number
  lastToastAt: number
  override: boolean | undefined
  loaded: boolean
  /** Bumped whenever /cortados-guard sets the override: a store read begun before that is stale and is dropped. */
  gen: number
  /** Failure kinds already reported (each is reported once per mod load). */
  reported: Set<string>
  /** Routed subagents running now: while any is, the status line is theirs and the guard does not publish. */
  routedActive: number
}

export function newGuardState(): GuardState {
  return { edits: 0, runs: 0, lastToastAt: -Infinity, override: undefined, loaded: false, gen: 0, reported: new Set(), routedActive: 0 }
}

/** The one-line diagnostic for a guard step that failed. Names the step and the error's class only, never its message
 * or any input, so it can carry no credential or file content. */
export function guardDiagnostic(where: string, err: unknown): string {
  const name = err && typeof err === 'object' && typeof (err as any).name === 'string' ? (err as any).name : typeof err
  const cls = /^[A-Za-z0-9_]{1,40}$/.test(name) ? name : 'Error'
  return `cortados-routed-subagents: main-session guard ${where} failed (${cls}); the tool call ran unchanged`
}

/** Report a guard failure ONCE per kind, as a dim transcript notice ($.ui.log is not sent to the model). Never throws:
 * a diagnostic that cannot be shown is dropped, so the call it is about still runs. */
function guardReport($: any, guard: GuardState, where: string, err: unknown): void {
  if (guard.reported.has(where)) return
  guard.reported.add(where)
  try {
    const r = $.ui.log(guardDiagnostic(where, err))
    if (r && typeof r.then === 'function') r.then(undefined, () => {})
  } catch { /* nowhere left to report; the call still runs */ }
}

/** Run one guard side effect ($.ui.status / $.ui.toast), reporting a failure, sync or async, once under `where`. */
function guardTry($: any, guard: GuardState, where: string, fn: () => unknown): void {
  try {
    const r: any = fn()
    if (r && typeof r.then === 'function') r.then(undefined, (err: unknown) => guardReport($, guard, where, err))
  } catch (err) {
    guardReport($, guard, where, err)
  }
}

/** Whether the guard is on: the /cortados-guard override in $.store when set (read once per mod load), else the
 * userConfig default. A read that resolves after a /cortados-guard toggle is stale (its gen moved on) and is dropped,
 * so a slow first read can never undo the toggle. A failed read is reported once and falls back to the default. */
async function guardEnabled($: any, guard: GuardState, configDefault: boolean): Promise<boolean> {
  if (!guard.loaded) {
    const gen = guard.gen
    try {
      const v = await $.store.get(GUARD_STORE_KEY)
      if (!guard.loaded && guard.gen === gen) {
        guard.override = typeof v === 'boolean' ? v : undefined
        guard.loaded = true
      }
    } catch (err) {
      guardReport($, guard, 'store read', err)
    }
  }
  return guard.override ?? configDefault
}

/** Put the guard's counter on the status line, unless a routed subagent owns it right now. */
function guardPublish($: any, guard: GuardState, text: string | undefined): void {
  if (guard.routedActive > 0) return
  guardTry($, guard, 'status', () => $.ui.status(text))
}

export const register: Register = (on, options) => {
  const cfg: Cfg = { ...FALLBACK, ...(options as Partial<Cfg>) }
  const subagentTimeoutMs = resolveTimeoutMs(cfg.subagentTimeoutMs)

  // Guard state for this session. Closure variables, so a mod reload (or Claude Code restart) starts the counters and
  // the toast throttle over; the on/off override lives in $.store and survives both.
  const guard: GuardState = newGuardState()
  const guardDefault = cfg.mainSessionGuard !== false
  /** What the status line rests on when the mod has nothing else to show: the guard's counter, if any. */
  const restingStatus = (): string | undefined =>
    (guard.override ?? guardDefault) ? guardStatusText(guard.edits, guard.runs) : undefined

  on('tool.call', { tool: 'Agent' }, async ($: any, e: any, next: any) => {
    const subagentType = String(e.subagent_type ?? '')
    const background = Boolean(e.run_in_background)
    const prompt = String(e.prompt ?? '')
    const description = String(e.description ?? 'routed subagent')

    // Pass through BEFORE launching anything — these cannot or should not be routed. After the launch below the
    // hook never returns next(e), because the child will already have acted and native would re-run it.
    if (subagentType === 'fork' || !prompt.trim()) return next(e)
    if (background && !cfg.routeBackground) return next(e)
    if (e.isolation) return next(e) // worktree/remote: let the native runner own isolation
    if (e.name) return next(e) // a named agent stays addressable by SendMessage only if the parent spawns it

    // Per-call copy: concurrent agents must use the same resolved URL for launch and run-record lookup.
    let resolvedUrl: string
    try { resolvedUrl = await resolveProviderUrl($, cfg.providerUrl) }
    catch (err) { return { deny: String(err) } }
    const runCfg = { ...cfg, providerUrl: resolvedUrl }
    let token: string
    try {
      token = await resolveToken($, cfg)
    } catch (err) {
      $.ui.toast(`routed-subagents: no provider token (${scrubToken(String(err), '')}); running natively`)
      return next(e)
    }
    if (!token) return next(e)

    // A review must not run on a model that built the code: ask the router to keep it off the main session's own
    // model and the models routed (non-review) subagents ran on since the last review.
    const review = isReview(description, subagentType)
    let customHeaders = 'x-cortados-reveal: 1'
    let excluded: string[] = []
    // Dispatch-time watermark: on completion the review forgets only entries tracked up to here (see untrackWorkerModels).
    const excludedUpToSeq = workerModelsWatermark()
    if (cfg.excludeBuilderOnReview && review) {
      excluded = await modelsToExclude($)
      if (excluded.length) customHeaders += '\nx-cortados-exclude-model: ' + excluded.join(',')
    }
    const excludedNames = excluded.join(', ')

    // --tools bounds the built-in tools the child actually has; --allowedTools grants them (needed under non-bypass
    // modes). --settings disableAllHooks stops THIS mod from firing inside the child and routing recursively.
    // --strict-mcp-config + an empty --mcp-config start the child with NO MCP servers: otherwise it inherits the
    // user's MCP servers and advertises their tools to the provider, and a Codex lane refuses a thread whose dynamic
    // tools include a reserved name (seen 2026-10-28: "dynamic tool name is reserved: mcp__cortados-route__route_task",
    // code -32600). Routed subagents are built-in-tools-only by design (README).
    const tools = String(cfg.allowedTools)
    const args = [
      '-p', prompt + '\n\n' + STOP_ON_DEAD_TOOL,
      '--model', String(cfg.model),
      '--permission-mode', String(cfg.permissionMode),
      '--tools', tools,
      '--allowedTools', tools,
      '--settings', JSON.stringify({ disableAllHooks: true }),
      // Load NO user/project/local settings, as the provider's own bridge does: otherwise a settings `env` block
      // could restore a conflicting ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL and override the launch env below, sending
      // the provider token to the wrong endpoint or a stale key to the provider (Codex review, 2026-10-28).
      // Known residual (defense-in-depth): a managed-settings `env` block or a legacy global `env` in ~/.claude.json
      // can still apply over the launch env even with no setting sources. Fully closing that needs an isolated
      // CLAUDE_CONFIG_DIR; it does not apply to a fleet without managed settings or a global env block.
      '--setting-sources', '',
      '--strict-mcp-config',
      '--mcp-config', JSON.stringify({ mcpServers: {} }),
      // json = the result object only. NOT stream-json: that puts every intermediate message on stdout, so a
      // tool-heavy child that succeeded can overrun the host's stdout cap, come back truncated and be refused. How far
      // a failed child got is read from its transcript instead (partialNote).
      '--output-format', 'json',
    ]
    const maxTurns = resolveMaxTurns(cfg.maxTurns)
    if (maxTurns !== null) args.push('--max-turns', String(maxTurns))

    type RunRes = { exitCode: number; stdout: string; stderr: string; isStdoutTruncated: boolean }
    const startedAt = Date.now()
    // Each run (the retry too) carries a fresh x-cortados-run-id, so its provider record can be fetched afterwards.
    let runId = ''
    // Each run also gets a session id we choose, so on a timeout (when $.process.run rejects and its output is gone)
    // the child's own transcript can still be read for how far it got.
    let sessionId = ''
    const runChild = (headers: string, timeoutMs: number): Promise<RunRes> => {
      runId = newRunId()
      sessionId = newSessionId()
      return $.process.run(
        [String(cfg.claudeBin), ...args, '--session-id', sessionId],
        {
          // The child inherits the parent's environment and credentials. Two things there can make it send the
          // wrong token to the provider and get a 401:
          //   1. Under the desktop app, CLAUDE_CODE_ENTRYPOINT=claude-desktop makes the CLI authenticate with the
          //      host's claude.ai OAuth session and ignore the env token (found 2026-10-28 with a header sniffer).
          //      Forcing the entrypoint to `cli` stops that.
          //   2. The CLI resolves credentials over BOTH channels — x-api-key (ANTHROPIC_API_KEY, a saved Console
          //      key, or apiKeyHelper output) and bearer (ANTHROPIC_AUTH_TOKEN). An empty ANTHROPIC_API_KEY still
          //      lets a saved key / apiKeyHelper load and be sent (Codex review, 2026-10-28). So set BOTH channels
          //      to the provider token: whichever the CLI selects, it is the provider token, and no inherited key
          //      leaks. The child only ever contacts the provider (ANTHROPIC_BASE_URL pins it), never the real API.
          // Neutralize inherited vars that could override or redirect the child regardless of the above: a custom
          // x-api-key header (ANTHROPIC_CUSTOM_HEADERS) is merged after the SDK credentials and would replace the
          // provider key, and the Bedrock/Vertex selectors would send the turn to another transport entirely
          // (Codex review, 2026-10-28). $.process.run only ADDS over the host env, so blanking is how these are cleared.
          // ANTHROPIC_CUSTOM_HEADERS is set rather than blanked: it carries only `x-cortados-reveal: 1`, which asks the
          // provider to name the real routed model and its lane's remaining % in `model` (the label below), and as
          // the whole value it still replaces any inherited custom headers, so no x-api-key override survives.
          // For a review it also carries `x-cortados-exclude-model: <models>` (newline-separated line, comma-separated
          // ids): the main session's model and the tracked worker models (see modelsToExclude). Every run also
          // carries `x-cortados-run-id: <id>`, used to fetch the run's record (the real routed model) afterwards.
          env: {
            ANTHROPIC_BASE_URL: resolvedUrl,
            ANTHROPIC_API_KEY: token,
            ANTHROPIC_AUTH_TOKEN: token,
            CLAUDE_CODE_ENTRYPOINT: 'cli',
            ANTHROPIC_CUSTOM_HEADERS: headers + '\nx-cortados-run-id: ' + runId,
            ANTHROPIC_UNIX_SOCKET: '',
            CLAUDE_CODE_USE_BEDROCK: '',
            CLAUDE_CODE_USE_VERTEX: '',
            CLAUDE_CODE_USE_FOUNDRY: '',
            CLAUDE_CODE_USE_MANTLE: '',
            CLAUDE_CODE_USE_ANTHROPIC_AWS: '',
            CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD: '',
          },
          timeoutMs,
        },
      )
    }
    /** The provider's record of the last run, or null; tracks a non-review run's models. Never throws. */
    const recordLastRun = async (): Promise<RunRecord | null> => {
      if (!runId) return null
      const record = await fetchRunRecord($, runCfg, token, runId)
      try {
        if (record && !review) trackWorkerModels(builderModelsOf(record))
      } catch { /* never fail the subagent over bookkeeping */ }
      return record
    }
    /** What the child left behind: its last assistant text and progress, read from its transcript, found by its
     * session UUID under the CLI's config dir (CLAUDE_CONFIG_DIR or ~/.claude; the child inherits the same env). A
     * summary for the refusal, capped; the caller scrubs the whole refusal. Never throws. */
    const partialNote = async (): Promise<string> => {
      const elapsed = `elapsed ${fmtSecs(Date.now() - startedAt)}`
      if (!sessionId) return `[${elapsed}]`
      let where = `session ${sessionId}`
      try {
        if (!isSessionUuid(sessionId)) throw new Error('session id is not a UUID; transcript not searched')
        const loc = await $.process.run(['sh', '-c', FIND_TRANSCRIPT_SH, 'sh', sessionId])
        const file = String(loc.stdout).split('\n')[0].trim()
        if (!file || !file.endsWith(`/${sessionId}.jsonl`)) throw new Error('transcript not found under the projects dir')
        where += `, transcript ${file}`
        const pt = partialFromTranscript(String(await $.fs.read(file)))
        const head = `[${elapsed}; ${where}; ${pt.assistantTurns} assistant turns, ${pt.toolUses} tool calls]`
        if (!pt.lastText) return `${head}\n(no assistant text yet)`
        // Scrubbed before the cut too, so the 4000-char window never starts inside a token the final scrub would miss.
        return `${head}\nLast assistant text (partial result):\n${scrubToken(pt.lastText, token).slice(-4000)}`
      } catch (err) {
        return `[${elapsed}; ${where}; transcript unreadable: ${scrubToken(String(err), token).slice(0, 200)}]`
      }
    }
    // Counted inside the try, so the finally below always takes it back off, whatever throws. While any child runs
    // the status line is its own; the guard holds its counter until none is active.
    let counted = false
    try {
      guard.routedActive += 1
      counted = true
      // Through guardTry: a status failure is reported once and never stops the launch.
      guardTry($, guard, 'status', () => $.ui.status(`↗ routed subagent (${description})`))
      let res = await runChild(customHeaders, subagentTimeoutMs)
      let p = parseResult(res.stdout)
      let note = ''
      // Fallback: the provider refuses (HTTP 400) an exclude header naming no model it serves — e.g. after a model
      // rename — or one that leaves nothing usable. Without this every review would fail. Retry ONCE without the
      // exclusion, and tell the user and the parent model that independence was NOT enforced. The refusal comes on
      // the request, normally the first one, so the child has not acted yet; only a pool change mid-run could make a
      // later request fail this way. The retry gets the REMAINING share of subagentTimeoutMs (one overall budget).
      if (excluded.length && !cleanCompletion(res.exitCode, res.isStdoutTruncated, p)) {
        // Only an error result or stderr is read: a successful-looking result text is the child's own words.
        const reason = excludeRefusalReason(`${p.isError ? p.text ?? '' : ''}\n${res.stderr}`)
        const remainingMs = Math.floor(subagentTimeoutMs - (Date.now() - startedAt))
        if (reason && remainingMs >= MIN_RETRY_MS) {
          $.ui.toast(`routed-subagents: the router refused to keep this review off ${excludedNames} (${reason}); ran it without the exclusion`)
          note = `[review NOT kept off ${excludedNames}: ${reason}]\n`
          res = await runChild('x-cortados-reveal: 1', Math.min(MAX_SUBAGENT_TIMEOUT_MS, remainingMs))
          p = parseResult(res.stdout)
        }
      }
      // Success or failure, ask the provider which model really ran (p.model is just the requested `cortados`).
      const record = await recordLastRun()
      if (!cleanCompletion(res.exitCode, res.isStdoutTruncated, p)) {
        // The child may already have acted; refuse rather than re-dispatch natively (which would replay the work).
        const why = `exit ${res.exitCode}${p.isError ? ', error' : ''}${res.isStdoutTruncated ? ', truncated' : ''}`
        // Surface only the parsed result text (a complete JSON field — never cut mid-token), scrubbed. Raw stdout/
        // stderr are NOT surfaced: the CLI truncates them at a byte boundary that could split a token past the
        // exact-match scrubber. They remain in the child's on-disk logs for debugging.
        const detail = typeof p.text === 'string' && p.text ? scrubToken(p.text, token).slice(-500) : '(no parseable output)'
        // The WHOLE refusal is scrubbed: the transcript path, session id and partial note are model-visible too.
        return { deny: scrubToken(`${note}routed subagent did not complete cleanly (${why}): ${detail}\n${await partialNote()}`, token) }
      }
      // A completed review has checked the work of the models it was kept off: forget exactly those entries (as of
      // dispatch), keeping workers that finished while it ran — even on the same model. A failed review keeps the
      // list, so the retried review is still kept off them; so does a review that ran WITHOUT the exclusion (`note`:
      // the router refused it), since it was not kept off them — the next review still is.
      if (review && !note) untrackWorkerModels(excluded, excludedUpToSeq)
      const routedTo = record?.label || record?.model || p.model
      const header = note + (routedTo ? `[routed via CortadOS → ${routedTo}]\n\n` : note ? '\n' : '')
      // Match the Agent tool's "completed" result shape so core accepts it in place of a native subagent.
      return {
        result: {
          content: [{ type: 'text', text: scrubToken(header + (p.text as string), token) }],
          agentId: scrubToken(p.sessionId || `routed-${(routedSeq += 1)}`, token),
          status: 'completed',
          prompt,
          totalToolUseCount: p.totalToolUseCount,
          totalDurationMs: p.totalDurationMs,
          totalTokens: p.totalTokens,
          usage: normalizeUsage(p.usage),
          // This routed child has no entry in the parent's agent registry, so it cannot be continued by SendMessage.
          canContinueAgent: false,
        },
      }
    } catch (err) {
      // Launch/runtime failure (a timeout lands here: $.process.run rejects and the output is lost): the child may
      // have started, so refuse cleanly instead of re-dispatching natively — but hand back what it left behind.
      // The host's own message words its timeout oddly ("still running after 60000s" for 600000 ms), so state it.
      const timedOut = Date.now() - startedAt >= subagentTimeoutMs - 1_000
      const limit = timedOut ? ` (timeout ${fmtSecs(subagentTimeoutMs)})` : ''
      return { deny: scrubToken(`routed subagent failed${limit}: ${err}\n${await partialNote()}`, token) }
    } finally {
      // Guarded: a throw here would escape to the outer .catch and trigger a native re-dispatch (replay). Never
      // let clearing the status line do that.
      // Restores the main-session guard's counter (one status line per plugin) rather than wiping it — once the
      // last routed child is done; while another still runs, its line stays.
      if (counted) guard.routedActive = Math.max(0, guard.routedActive - 1)
      // guardPublish publishes only once no child is active, and never throws: a failure gets its one notice.
      guardPublish($, guard, restingStatus())
    }
  }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

  // ---- Main-session guard: WARN ONLY. Every call proceeds unchanged; only a toast and a status counter. ----------
  for (const tool of GUARD_TOOLS) {
    on('tool.call', { tool }, async ($: any, e: any, next: any) => {
      // Every step is guarded on its own and reports its failure once (guardReport); whatever fails, the call below
      // runs exactly once and unchanged.
      try {
        // Subagents (and teammates, engine forks) carry an agentId; only the main loop is nudged.
        const kind = e?.agentId == null ? substantiveKind(tool, e) : null
        if (kind && (await guardEnabled($, guard, guardDefault))) {
          if (kind === 'edit') guard.edits += 1
          else guard.runs += 1
          guardPublish($, guard, guardStatusText(guard.edits, guard.runs))
          let now = NaN
          try {
            now = Number(await $.clock.now())
          } catch (err) {
            guardReport($, guard, 'clock', err)
          }
          // No usable time: skip the toast rather than toast on every call; the counter above still counted it.
          if (Number.isFinite(now) && now - guard.lastToastAt >= GUARD_TOAST_INTERVAL_MS) {
            guard.lastToastAt = now
            const text = kind === 'edit'
              ? 'main session did a large edit; delegate this kind of work to a routed subagent'
              : 'main session ran a build/test; delegate this kind of work to a routed subagent'
            guardTry($, guard, 'toast', () => $.ui.toast(text))
          }
        }
      } catch (err) {
        guardReport($, guard, 'bookkeeping', err)
      }
      return next(e)
    }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))
  }

  on('session.start', async ($: any, e: any, next: any) => {
    const r = await next(e)
    try {
      await $.command.register({ name: GUARD_COMMAND, description: 'Main-session guard (warn only): on, off, or no argument for its state.', argumentHint: '[on|off]', immediate: true })
    } catch (err) {
      // The guard still runs from userConfig without its command; say the toggle is missing.
      guardReport($, guard, `/${GUARD_COMMAND} registration`, err)
    }
    return r
  }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

  on('command.run', { command: GUARD_COMMAND }, async ($: any, e: any) => {
    const want = parseGuardArg(e?.args)
    if (want === null && String(e?.args ?? '').trim()) return { text: `usage: /${GUARD_COMMAND} on|off` }
    let note = ''
    if (want !== null) {
      // In memory first, and the generation bumped, so a first store read still in flight cannot undo this.
      guard.override = want
      guard.loaded = true
      guard.gen += 1
      guardPublish($, guard, restingStatus())
      try {
        await $.store.set(GUARD_STORE_KEY, want)
      } catch (err) {
        guardReport($, guard, 'store write', err)
        note = '; not saved, applies to this session only'
      }
    }
    const on_ = await guardEnabled($, guard, guardDefault)
    const counts = guardStatusText(guard.edits, guard.runs)
    return { text: `main-session guard ${on_ ? 'on' : 'off'} (warn only${on_ && counts ? `; ${counts}` : ''}${note})` }
  })

  // ---- Orchestrator mode: direct the main session to plan and delegate; routed subagents do the work. ----------
  if (cfg.orchestratorMode) {
    // Inject the standing directive so the brain delegates by default — no per-session instruction needed. Only on
    // the main render: a teammate render is a different in-process worker that keeps its tools and must not be told
    // to delegate everything. This directive is the GUIDANCE; it is always on under orchestratorMode.
    on('prompt.compose', async ($: any, e: any, next: any) => {
      const r = await next(e)
      if (Array.isArray(e?.traits) && e.traits.includes('teammate')) return r
      const section = { id: 'cortados-routed-subagents:orchestrator', text: ORCHESTRATOR_DIRECTIVE, scope: 'session' }
      return { sections: [...r.sections, section] }
    }).catch(($: any, e: any, next: any) => (next.called ? undefined : next(e)))

    // HARD ENFORCEMENT is opt-in (orchestratorEnforce). By default the directive above is trusted and the brain keeps
    // full tool access — it may act directly when that is clearly sensible. With enforce on, EVERY tool call on the
    // main loop is gated against the allowlist (an allowlist, not a denylist, so Monitor/memory_write/ExitWorktree/
    // mcp__ tools/future tools are refused unless listed). Non-main loops and allowed tools pass straight through.
    if (cfg.orchestratorEnforce) {
      // An empty/malformed allowlist would lock out every tool (including Agent); resolveAllowSet drops malformed
      // tokens and falls back to the default set so the brain can always at least read and delegate.
      const allow: ReadonlySet<string> = resolveAllowSet(cfg.orchestratorAllowedTools)
      on('tool.call', async ($: any, e: any, next: any) => {
        if (mainLoopAllows(String(e.tool), e.agentId, allow, true)) return next(e)
        return {
          deny:
            `Orchestrator mode (enforced): the brain plans and delegates — it does not run ${e.tool} itself. ` +
            `Spawn a subagent with the Agent tool (it is routed to the CortadOS pool and runs the work here) ` +
            `instead of calling ${e.tool} directly.`,
        }
        // Fail CLOSED for work tools: if the gate throws before deciding, refuse a non-allowed tool (never let it run
        // on a guard failure) but still let an allowed tool (Agent/Read/...) through so the session is not bricked.
      }).catch(($: any, e: any, next: any) =>
        next.called
          ? undefined
          : mainLoopAllows(String(e.tool), e.agentId, allow, true)
            ? next(e)
            : { deny: 'Orchestrator mode gate failed; refusing this tool. Delegate it to a subagent.' },
      )
    }
  }
}
