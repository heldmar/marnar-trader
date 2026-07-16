// Typed client for the core service's /api endpoints (core/trader/api.py).

export interface EquityPoint {
  ts: string
  equity: number
}

export interface TradeResult {
  ts: string
  symbol: string
  pnl: number
}

export interface Overview {
  mode: string
  risk_state: string
  halt_reason: string | null
  trading_enabled: boolean
  paper_started_at: number | null
  initial_usdt: number
  equity_usdt: number | null
  today_pnl_usdt: number | null
  total_pnl_usdt: number | null
  open_positions: number
  equity_series: EquityPoint[]
  trade_results: TradeResult[]
  closed_trades: number
  winning_trades: number
  fees_usdt: number
}

export interface Position {
  symbol: string
  quantity: number
  entry_price: number
  current_price: number | null
  cost_usdt: number
  value_usdt: number | null
  unrealized_pnl_usdt: number | null
  stop_price: number | null
  take_profit_price: number | null
  opened_at: string
}

export interface TradeReason {
  rule: string
  close?: number
  level?: number
  n?: number
  [key: string]: unknown
}

export interface NewsItem {
  seen_at: string
  source: string | null
  title: string
  url: string
}

export interface TimelineTrade {
  type: 'trade'
  ts: string | null
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  avg_price: number
  fee_usdt: number
  source: string
  reason: TradeReason | null
  news_context: NewsItem[]
}

export interface TimelineSystem {
  type: 'system'
  ts: string
  kind: string
  detail: Record<string, unknown>
}

export type TimelineItem = TimelineTrade | TimelineSystem

export interface ConfigView {
  mode: string
  paper: Record<string, unknown>
  risk: Record<string, number>
  risk_floors: Record<string, number>
  clock_resetting_fields: string[]
}

export interface SystemInfo {
  reconciliation: { clean: boolean } | null
  trading_enabled: boolean
  last_candle_processed: number | null
  news_last_seen: string | null
  news_items_24h: number
  orders_last_24h: number
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const body = await resp.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* non-JSON error body: keep statusText */
    }
    throw new ApiError(resp.status, detail)
  }
  return resp.json() as Promise<T>
}

export const api = {
  overview: () => request<Overview>('/api/overview'),
  positions: () => request<Position[]>('/api/positions'),
  timeline: (symbol?: string) =>
    request<TimelineItem[]>(`/api/timeline${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`),
  config: () => request<ConfigView>('/api/config'),
  system: () => request<SystemInfo>('/api/system'),
  putConfig: (body: {
    paper?: Record<string, unknown>
    risk?: Record<string, number>
    acknowledge_clock_reset?: boolean
  }) =>
    request<{ saved: boolean; restart_required: boolean; clock_reset: boolean }>('/api/config', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  killSwitch: (closePositions: boolean) =>
    request<{ state: string }>('/api/kill-switch', {
      method: 'POST',
      body: JSON.stringify({ close_positions: closePositions, reason: 'kill switch (web UI)' }),
    }),
  resume: () =>
    request<{ state: string; trading_enabled: boolean }>('/api/resume', { method: 'POST' }),
}
