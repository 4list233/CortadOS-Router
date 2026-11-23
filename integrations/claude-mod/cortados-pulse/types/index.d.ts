// The shape of the provider's GET /resources (cortados_router/resources.py `snapshot`), as this pane reads it. Every
// field is optional: the provider stores a failed source as {error} in its place rather than failing the whole GET.

export interface PulseNode {
  id: string
  role?: string
  status?: string
  host_memory_total_bytes?: number | null
  gpu_headroom_estimate_bytes?: number | null
  gpu_utilization_pct?: number | null
  fits?: string[]
}

export interface PulseDevices {
  status?: 'ok' | 'pending' | 'unavailable' | string
  error?: string | null
  nodes?: PulseNode[]
  pool_headroom_estimate_bytes?: number | null
  pool_fits?: string[]
  note?: string | null
}

export interface PulseUsage {
  total_routes?: number
  by_band?: Record<string, number>
  by_lane?: Record<string, number>
  degraded?: { count: number; of: number }
  s1_latency_ms?: { median?: number; p90?: number; p90_convention?: string; window?: number }
  recent?: unknown[]
  error?: string
}

/** resources.snapshot subscription projection; account identity and pools come from readings. */
export interface PulseHealthRow {
  ok?: boolean
  why?: string | null
  scarcity?: number | null
  tightest?: { window?: string; used_pct?: number; resets_at?: string } | Record<string, never> | null
  expiring_pct?: number | null
}
export interface PulseLaneHealth extends PulseHealthRow {}

/** core.subscription_readings: the lane's cached usage reading window by window, for a door that applies its own
 * stop rule. `headroom_pct` is what is left; an older provider sends only `used_pct`. */
export interface PulseWindow {
  window?: string
  pool?: string | null
  used_pct?: number | null
  headroom_pct?: number | null
  reset_epoch?: number | null
  stale?: boolean | null
}
export interface PulseReading {
  fetched_at?: number | null
  failed?: boolean
  windows?: PulseWindow[]
  config_dir?: string | null
  login?: string | null
}

export interface Pulse {
  generated_at?: number
  usage?: PulseUsage
  subscriptions?: Record<string, PulseLaneHealth | string>
  readings?: Record<string, PulseReading | string>
  devices?: PulseDevices
}

// The provider's GET /v1/cortados/live (cortados_router/provider/live.py `collect`), the fields this pane reads. Each
// section carries as_of, status (ok / partial / error; s1: up / down / unknown) and errors [{source, error}].
export interface PulseLiveError { source?: string; error?: string }
export interface PulseLiveSection { as_of?: number; status?: string; errors?: PulseLiveError[] }
export interface PulseLiveTask {
  task_id?: string
  title?: string | null
  state?: string
  phase?: string | null
  host?: string | null
  model?: string | null
  lane?: string | null
  tool?: string | null
  elapsed_s?: number | null
  ended_at?: number | null
  // the main session that started it: its Office tag, "grokbot" for the bot gateway, or null (unattributed)
  origin_session?: string | null
  origin_project?: string | null
  origin_device?: string | null
}
export interface PulseLiveConversation {
  kind?: string
  run_id?: string | null
  phase?: string | null
  title?: string | null
  project?: string | null
  device?: string | null
  model?: string | null
  lane?: string | null
  status?: string
  tool?: string | null
  elapsed_s?: number | null
}
export interface PulseLiveJob { name?: string; state?: string; summary?: string | null; progress_pct?: number | null }
export interface PulseLiveHost {
  name?: string
  heartbeat_state?: 'fresh' | 'stale' | 'none' | string
  heartbeat?: { age_s?: number; jobs?: PulseLiveJob[]; s1?: PulseLiveS1 | null } | null
  hardware?: { cpu_busy_pct?: number | null; gpu_busy_pct?: number | null; disk_free_pct?: number | null; source?: string; filled_by?: string | null } | null
}
/** The S1 host's heartbeat block (provider/live.py): health, the served model, hold flags, a pending candidate's reason. */
export interface PulseLiveS1 { health?: string; model?: string | null; flags?: string[]; pending?: string | null }
export interface PulseLive {
  version?: number
  session_filter?: string     // set by ?session=<tag>: the task lists hold only that session's tasks
  generated_at?: number
  overview?: {
    running_tasks?: number | null
    queued_tasks?: number | null
    active_conversations?: number | null
    working_conversations?: number | null
    s1?: string
    s1_latency_ms?: number | null
    lowest_subscription?: { lane?: string; label?: string | null; left_pct?: number; window?: string | null } | null
  }
  // scope: always "provider host" (tasks on other execution hosts log there); elsewhere: tasks started from the provider
  // host onto another host in the last 10 h, their state unknown to the view
  tasks?: PulseLiveSection & { counts?: Record<string, number> | null; running?: PulseLiveTask[]; queued?: PulseLiveTask[]; recent?: PulseLiveTask[]
    scope?: string; elsewhere?: { task_id?: string; host?: string; started_at?: number }[]; truncated?: boolean
    // the tasks grouped by the session that started them (absent from a provider before task origins)
    origins?: { session?: string | null; running?: number; queued?: number; finished?: number; task_ids?: string[] }[] }
  conversations?: PulseLiveSection & { items?: PulseLiveConversation[]; hidden?: number; active?: number | null; working?: number | null }
  s1?: PulseLiveSection & { latency_ms?: number | null; reason?: string | null }
  hosts?: PulseLiveSection & { items?: PulseLiveHost[]; provider_host?: string | null }
}

// GET /v1/cortados/live/session?session=<tag> (provider/live.py `session_view`): this session's orchestrator subtree
// and session-free fleet health. Statuses in the owner's words: running, waiting, done, failed, unknown.
export type PulseTreeStatus = 'running' | 'waiting' | 'done' | 'failed' | 'unknown' | string
export interface PulseTokens { input?: number; output?: number; cache_read?: number; cache_write?: number; fresh?: number }
/** A sub-session: one of a child task's routed conversations (role main = its main loop), or one of the session's own
 * routed subagents (orchestrator `routed`). */
export interface PulseSubSession {
  ref?: string | null
  role?: 'main' | 'subagent' | string
  title?: string | null
  status?: PulseTreeStatus
  phase?: string | null
  tool?: string | null
  model?: string | null
  lane?: string | null
  started_at?: number | null
  ended_at?: number | null
  elapsed_s?: number | null
  tokens?: PulseTokens | null
}
export interface PulseTreeTask {
  task_id?: string
  title?: string | null
  state?: string
  status?: PulseTreeStatus
  phase?: string | null
  tool?: string | null
  host?: string | null
  model?: string | null
  lane?: string | null
  started_at?: number | null
  ended_at?: number | null
  elapsed_s?: number | null
  origin_via?: string | null
  parent_task_id?: string | null
  parent_hidden?: boolean
  tokens?: PulseTokens | null
  sub_sessions?: PulseSubSession[]
  sub_sessions_hidden?: number
  /** every failed sub-session of the task, listed or not (counted in its group's attention) */
  sub_sessions_failed?: number
  tasks?: PulseTreeTask[]
}
export interface PulseOrchestrator {
  origin?: string | null
  // 'untagged' (0.6.3): this machine's tasks that carry no session tag, in a session view asked with &machine=
  kind?: 'session' | 'bot' | 'unattributed' | 'untagged' | string
  project?: string | null
  device?: string | null
  status?: string
  model?: string | null
  tool?: string | null
  counts?: Record<string, number>
  attention?: number
  tasks?: PulseTreeTask[]
  tasks_hidden?: number
  routed?: PulseSubSession[]
  routed_hidden?: number
  /** Set by the pane only (mergeLocal): a local-only task may be one the provider counted but hid; counts are lower bounds. */
  counts_at_least?: boolean
}
export interface PulseSessionView {
  version?: number
  generated_at?: number
  session_filter?: string
  overview?: PulseLive['overview']
  // scope "provider host and heartbeats": the tree also holds the tasks the hosts' heartbeats carry (#118),
  // task_hosts names those hosts
  orchestrators?: PulseLiveSection & { items?: PulseOrchestrator[]; scope?: string; truncated?: boolean;
    inputs_truncated?: string[]; session_filter?: string; task_hosts?: string[]; conflicts?: number }
  s1?: PulseLive['s1']
  hosts?: PulseLive['hosts']
  /** #127: this session's own work split row only (provider/work_split.session_split) */
  work_split?: PulseSessionSplit
}

/** Four token counts plus fresh (input + output + cache writes) and all (fresh + cache reads). */
export interface PulseSplitTokens { input?: number; output?: number; cache_read?: number; cache_write?: number; fresh?: number; all?: number }
export interface PulseSplitWindow {
  orchestrator_fresh?: number | null
  orchestrator_all?: number | null
  routed_fresh?: number
  routed_all?: number
  routed_conversations?: number
  /** routed fresh ÷ (native fresh + routed fresh); null without a native side */
  routed_pct?: number | null
  /** the same on all tokens, cache reads included */
  routed_pct_all?: number | null
  native?: (PulseSplitTokens & { models?: (PulseSplitTokens & { model?: string })[] | null }) | null
  routed?: (PulseSplitTokens & { conversations?: number; by_route?: (PulseSplitTokens & { route?: string; conversations?: number })[];
    by_via?: Record<string, PulseSplitTokens & { conversations?: number }> }) | null
}
export interface PulseSplitRow {
  session?: string | null
  session_alt?: string | null
  host?: string | null
  project?: string | null
  model?: string | null
  last_at?: number | null
  native_stale?: boolean | null
  native_age_s?: number | null
  match?: 'both' | 'native only' | 'routed only' | 'untagged' | string
  matched_on?: string[]
  [window: string]: unknown
}
export interface PulseSessionSplit {
  as_of?: number
  status?: 'ok' | 'partial' | 'none' | string
  session?: string
  definition?: string
  windows?: Record<string, number>
  row?: PulseSplitRow | null
  notes?: string[]
}

/** GET /v1/cortados/local (provider/local_switch.py view): each local node as its gate reports it. */
export interface PulseLocalNode {
  node?: string
  label?: string | null
  reachable?: boolean
  error?: string | null
  switch?: 'on' | 'off' | 'unknown'
  allowed?: boolean | null
  reason?: string | null
  power?: 'ac' | 'battery' | 'unknown'
  training?: boolean | null
  ollama?: 'up' | 'unreachable' | 'unreadable' | 'unknown'
  backend?: 'ollama' | 'vllm-mlx' | 'unknown'
  models_loaded?: string[] | null
  models?: string[]
  auto_route?: { model?: string; bands?: string[] } | null
  capacity?: { limit?: number | null; inflight?: number | null; owner?: string | null; pressure?: string | null;
    reason?: string | null; reserved_bytes?: number | null; free_bytes?: number | null; fit_tokens?: number | null;
    free_tokens?: number | null } | null
}
export interface PulseLocalView { nodes?: PulseLocalNode[]; note?: string }

declare module 'claude-code' {
  interface PluginState {
    'cortados-pulse': {
      held: { data: Pulse; at: number } | null; err: string | null
      live: { data: PulseLive; at: number; base: string } | null; liveErr: string | null
      mine: { tag: string | null; why: string | null } | null
      mineLive: { data: PulseLive; at: number; tag: string } | null
      tree: { data: PulseSessionView; at: number; tag: string; base: string } | null; treeErr: string | null
      local: { tag: string; at: number; tasks: (PulseTreeTask & { parent_task_id?: string | null })[]; note?: string | null } | null
      /** #127: the expanded work split breakdown (the `w` button) */
      wsOpen: boolean
      /** #127: GET /v1/cortados/local, the local nodes as their gates report them */
      nodes: { data: PulseLocalView; at: number } | null; nodesErr: string | null
      /** #127: the origin tags this session's own start_task results named, newest first */
      started: { tag: string; task_id: string; at: number }[]
      /** #127: the session view of the newest such tag that is not this session's own (a /clear or resume) */
      alias: { data: PulseSessionView; at: number; tag: string } | null
      /** #127: the alias view's last failed read (its problem, and since when a read of it has been outstanding) */
      aliasErr: { tag: string; problem: string; pendingSince: number | null } | null
      /** 0.6.3: the details (the `d` button): everything but the default view's five lines */
      detailsOpen: boolean
      /** 0.6.3: this machine's tag as cortados-route records origin_machine (machineTag of the Office salt), once computed */
      machine: string | null
    }
  }
}
