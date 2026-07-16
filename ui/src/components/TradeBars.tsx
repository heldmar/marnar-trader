// Win/loss bars (UI-15): one bar per finished trade — green up for a win,
// clay down for a loss. Height = how much money. Readable with zero training.

import type { TradeResult } from '../api'
import { baseAsset, shortDate, signedMoney } from '../format'

const W = 640
const H = 150
const MID = H / 2 - 8

export function TradeBars({ results }: { results: TradeResult[] }) {
  if (results.length === 0) {
    return (
      <div className="chart-empty">
        No finished trades yet. Each finished trade will show up here as a green (win) or
        clay (loss) bar.
      </div>
    )
  }

  const shown = results.slice(-40) // most recent 40 keeps bars readable
  const maxAbs = Math.max(...shown.map((r) => Math.abs(r.pnl)), 0.01)
  const bw = Math.min(26, (W - 16) / shown.length - 4)

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
         aria-label={`Results of the last ${shown.length} trades`}>
      <line x1="0" x2={W} y1={MID} y2={MID} stroke="#e3ded2" strokeWidth="1" />
      {shown.map((r, i) => {
        const h = Math.max(3, (Math.abs(r.pnl) / maxAbs) * (MID - 14))
        const xPos = 8 + i * ((W - 16) / shown.length)
        const win = r.pnl >= 0
        return (
          <g key={`${r.ts}-${i}`}>
            <title>
              {`${baseAsset(r.symbol)} · ${shortDate(r.ts)} · ${signedMoney(r.pnl)}`}
            </title>
            <rect
              x={xPos}
              y={win ? MID - h : MID}
              width={bw}
              height={h}
              rx="3"
              fill={win ? '#3e7d4f' : '#b8563f'}
              opacity="0.85"
            />
          </g>
        )
      })}
      <text x="8" y={H - 4} fontSize="11" fill="#6c756c">older</text>
      <text x={W - 8} y={H - 4} fontSize="11" fill="#6c756c" textAnchor="end">newest</text>
    </svg>
  )
}
