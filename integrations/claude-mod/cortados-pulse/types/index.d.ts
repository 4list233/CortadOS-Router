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
  stale?: boolean
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

declare module 'claude-code' {
  interface PluginState {
    'cortados-pulse': { held: { data: Pulse; at: number } | null; err: string | null }
  }
}
