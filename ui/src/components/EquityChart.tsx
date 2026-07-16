// Equity curve (UI-15): the single most important picture — is the line
// above or below where the money started? Green fill above start, clay below.

import type { EquityPoint } from '../api'
import { money, shortDate } from '../format'

const W = 640
const H = 180
const PAD = { top: 12, right: 8, bottom: 22, left: 8 }

export function EquityChart({ series, initial }: { series: EquityPoint[]; initial: number }) {
  if (series.length < 2) {
    return (
      <div className="chart-empty">
        The money line will appear here once the system has been running for a few hours.
      </div>
    )
  }

  const t0 = new Date(series[0].ts).getTime()
  const t1 = new Date(series[series.length - 1].ts).getTime()
  const values = series.map((p) => p.equity)
  const lo = Math.min(...values, initial)
  const hi = Math.max(...values, initial)
  const span = hi - lo || 1

  const x = (ts: string) =>
    PAD.left + ((new Date(ts).getTime() - t0) / (t1 - t0 || 1)) * (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + (1 - (v - lo) / span) * (H - PAD.top - PAD.bottom)

  const line = series.map((p, i) => `${i ? 'L' : 'M'}${x(p.ts).toFixed(1)},${y(p.equity).toFixed(1)}`).join(' ')
  const baseline = y(initial)
  const area = `${line} L${x(series[series.length - 1].ts).toFixed(1)},${baseline.toFixed(1)} L${x(series[0].ts).toFixed(1)},${baseline.toFixed(1)} Z`

  const last = series[series.length - 1]
  const up = last.equity >= initial

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label={`Account value over time, now ${money(last.equity)}`}>
        {/* dashed line = where the money started */}
        <line x1={PAD.left} x2={W - PAD.right} y1={baseline} y2={baseline}
              stroke="#b9b3a4" strokeDasharray="5 4" strokeWidth="1" />
        <path d={area} fill={up ? 'rgba(62,125,79,0.14)' : 'rgba(184,86,63,0.14)'} />
        <path d={line} fill="none" stroke={up ? '#3e7d4f' : '#b8563f'}
              strokeWidth="2.2" strokeLinejoin="round" strokeLinecap="round" />
        <circle cx={x(last.ts)} cy={y(last.equity)} r="4" fill={up ? '#3e7d4f' : '#b8563f'} />
        <text x={PAD.left} y={H - 6} fontSize="11" fill="#6c756c">{shortDate(series[0].ts)}</text>
        <text x={W - PAD.right} y={H - 6} fontSize="11" fill="#6c756c" textAnchor="end">
          {shortDate(last.ts)}
        </text>
      </svg>
      <div className="legend">
        <span><span className="key" style={{ background: up ? '#3e7d4f' : '#b8563f' }} />account value</span>
        <span><span className="key" style={{ background: '#b9b3a4' }} />starting money ({money(initial, 0)})</span>
      </div>
    </div>
  )
}
