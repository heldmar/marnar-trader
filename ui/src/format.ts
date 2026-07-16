// Plain-language money formatting (UI-14): a non-financial reader must
// instantly see whether a number is a gain or a loss and how big it is.

export function money(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `$${v.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

/** Signed money: "+$2.10" / "−$1.35" / "$0.00". */
export function signedMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  if (v > 0) return `+${money(v)}`
  if (v < 0) return `−${money(Math.abs(v))}`
  return money(0)
}

/** Percent next to amounts so scale is graspable (UI-14). */
export function pct(part: number | null | undefined, whole: number): string {
  if (part === null || part === undefined || !whole) return ''
  return `${part >= 0 ? '+' : '−'}${Math.abs((part / whole) * 100).toFixed(2)}%`
}

export function gainClass(v: number | null | undefined): string {
  if (v === null || v === undefined || v === 0) return 'flat'
  return v > 0 ? 'gain' : 'loss'
}

/** "You made $2.10 today" / "You lost $1.35 today" — the hero sentence. */
export function plainPnl(v: number | null | undefined, period: string): string {
  if (v === null || v === undefined) return `No result for ${period} yet`
  if (v > 0.005) return `You made money ${period}`
  if (v < -0.005) return `You lost money ${period}`
  return `You broke even ${period}`
}

export function shortDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function shortDateTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function daysBetween(fromMs: number, to: Date = new Date()): number {
  return Math.floor((to.getTime() - fromMs) / 86_400_000)
}

/** Base asset of a Binance spot symbol: BTCUSDT → BTC. */
export function baseAsset(symbol: string): string {
  return symbol.endsWith('USDT') ? symbol.slice(0, -4) : symbol
}
