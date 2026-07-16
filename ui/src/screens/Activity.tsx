// Activity — the deep onion layer (UI-13): every trade with the exact rule
// that fired (D-23 truthful attribution) and the news around it as separate,
// clearly-labeled context. System events (halts, config changes) interleaved.

import { useMemo, useState } from 'react'
import type { Position, TimelineItem, TimelineSystem, TimelineTrade } from '../api'
import { baseAsset, money, shortDateTime, signedMoney } from '../format'

function plainSystemEvent(ev: TimelineSystem): string {
  const d = ev.detail
  switch (ev.kind) {
    case 'PAPER_CLOCK_STARTED':
      return 'Practice run started — the evaluation clock is now counting.'
    case 'PAPER_CLOCK_RESET':
      return `Practice clock restarted because settings changed (${(d.fields as string[])?.join(', ')}).`
    case 'HALT_EXECUTED':
      return `Trading was stopped: ${String(d.reason ?? 'safety rule triggered')}.`
    case 'RESUMED':
      return `Trading was started again (by ${String(d.who ?? 'investor')}).`
    case 'CONFIG_CHANGED':
      return 'Settings were changed.'
    case 'ENTRY_SKIPPED_MIN_NOTIONAL':
      return `A buy signal for ${String(d.symbol ?? 'a coin')} was skipped — the amount was below the exchange minimum.`
    case 'RISK_REJECTED':
      return `A trade was blocked by a safety rule: ${String(d.reason ?? '')}.`
    default:
      return ev.kind
  }
}

function TradeItem({ t }: { t: TimelineTrade }) {
  const bought = t.side === 'BUY'
  return (
    <div className="tl-item">
      <div className="tl-when">{shortDateTime(t.ts)}</div>
      <div className="tl-body">
        <div className="tl-title">
          <span className={bought ? 'buy' : 'sell'}>{bought ? 'Bought' : 'Sold'}</span>{' '}
          {baseAsset(t.symbol)}{' '}
          <span className="muted small num">
            — {t.quantity} at {money(t.avg_price)} each
            {t.fee_usdt ? `, fee ${money(t.fee_usdt)}` : ''}
          </span>
        </div>
        {t.reason ? (
          <div className="reason">
            <b>Why:</b> {t.reason.rule}
            {t.reason.close != null && t.reason.level != null && (
              <span className="muted num">
                {' '}
                (price {money(Number(t.reason.close))} vs. line at {money(Number(t.reason.level))})
              </span>
            )}
          </div>
        ) : (
          <div className="muted small">Why: recorded before reasons were kept (older trade).</div>
        )}
        {t.news_context.length > 0 && (
          <details className="newsbox">
            <summary>
              What the news was saying around that time ({t.news_context.length}) — context
              only, not the reason for the trade
            </summary>
            <ul>
              {t.news_context.slice(0, 8).map((n) => (
                <li key={n.url}>
                  <a href={n.url} target="_blank" rel="noreferrer">{n.title}</a>
                  {n.source && <span className="muted small"> — {n.source}</span>}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  )
}

export function Activity({
  items,
  positions,
}: {
  items: TimelineItem[]
  positions: Position[]
}) {
  const [filter, setFilter] = useState<string>('all')
  const symbols = useMemo(() => {
    const s = new Set<string>()
    for (const i of items) if (i.type === 'trade') s.add(i.symbol)
    return [...s].sort()
  }, [items])

  const shown = items.filter(
    (i) => filter === 'all' || i.type === 'system' || (i.type === 'trade' && i.symbol === filter),
  )

  return (
    <div>
      {positions.length > 0 && (
        <div className="card">
          <h2>Currently held — with their safety exits</h2>
          <table>
            <thead>
              <tr>
                <th>Coin</th>
                <th className="num">Amount</th>
                <th className="num">Bought at</th>
                <th className="num">Price now</th>
                <th className="num">Result so far</th>
                <th className="num">Safety exit at</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.symbol}>
                  <td><b>{baseAsset(p.symbol)}</b></td>
                  <td className="num">{p.quantity}</td>
                  <td className="num">{money(p.entry_price)}</td>
                  <td className="num">{money(p.current_price)}</td>
                  <td className="num">
                    <span className={p.unrealized_pnl_usdt != null && p.unrealized_pnl_usdt < 0 ? 'loss-text' : 'gain-text'}>
                      {signedMoney(p.unrealized_pnl_usdt)}
                    </span>
                  </td>
                  <td className="num">{money(p.stop_price)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            "Safety exit" (stop): if the price drops to that level, the coin is sold
            automatically to cap the loss.
          </p>
        </div>
      )}

      <div className="card">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <h2 style={{ flex: 1 }}>Everything the system did, and why</h2>
          {symbols.length > 0 && (
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              style={{ font: 'inherit', padding: '6px 8px', borderRadius: 8 }}
            >
              <option value="all">All coins</option>
              {symbols.map((s) => (
                <option key={s} value={s}>{baseAsset(s)}</option>
              ))}
            </select>
          )}
        </div>
        {shown.length === 0 ? (
          <p className="muted">Nothing yet — actions will appear here as they happen.</p>
        ) : (
          shown.map((i, idx) =>
            i.type === 'trade' ? (
              <TradeItem key={`t-${idx}`} t={i} />
            ) : (
              <div className="tl-item" key={`s-${idx}`}>
                <div className="tl-when">{shortDateTime(i.ts)}</div>
                <div className="tl-body sysline">{plainSystemEvent(i)}</div>
              </div>
            ),
          )
        )}
      </div>
    </div>
  )
}
