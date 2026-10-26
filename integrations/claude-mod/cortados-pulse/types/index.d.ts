export interface PulseNode {
  id: string
  role?: string
  status?: string
  gpu_headroom_estimate_bytes?: number | null
  gpu_utilization_pct?: number | null
  fits?: string[]
}
export interface Pulse {
  total_routes?: number
  degraded?: { count: number; of: number }
  s1_latency_ms?: { median?: number; p90?: number; window?: number }
  subscriptions?: Record<string, { scarcity?: number | null; expiring_pct?: number; ok?: boolean }>
  devices?: { status?: string; nodes?: PulseNode[]; pool_fits?: string[]; error?: string | null }
}

declare module 'claude-code' {
  interface PluginState {
    'cortados-pulse': { snap: Pulse | null; err: string | null }
  }
}
