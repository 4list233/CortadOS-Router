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
    scope?: string; elsewhere?: { task_id?: string; host?: string; started_at?: number }[] }
  conversations?: PulseLiveSection & { items?: PulseLiveConversation[]; hidden?: number; active?: number | null; working?: number | null }
  s1?: PulseLiveSection & { latency_ms?: number | null; reason?: string | null }
  hosts?: PulseLiveSection & { items?: PulseLiveHost[]; provider_host?: string | null }
}

declare module 'claude-code' {
  interface PluginState {
    'cortados-pulse': {
      held: { data: Pulse; at: number } | null; err: string | null
      live: { data: PulseLive; at: number; base: string } | null; liveErr: string | null
    }
  }
}
