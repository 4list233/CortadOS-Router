export interface PulseNode {
  id: string
  role?: string
  status?: string
  gpu_headroom_estimate_bytes?: number | null
  gpu_utilization_pct?: number | null
  fits?: string[]
}
export interface PulseUsage {
  total_routes?: number
  degraded?: { count: number; of: number }
  s1_latency_ms?: { median?: number; p90?: number; window?: number }
  error?: string
}
export interface Pulse {
  usage?: PulseUsage
  subscriptions?: Record<string, { scarcity?: number | null; expiring_pct?: number; ok?: boolean } | string>
  devices?: { status?: string; nodes?: PulseNode[]; pool_fits?: string[]; error?: string | null }
}

declare module 'claude-code' {
  interface PluginState {
    'cortados-pulse': { snap: Pulse | null; err: string | null }
  }
}
