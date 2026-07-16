// Home screen — the top onion layer (UI-13): one glance answers "how is it
// going?" (UI-02, UI-14, UI-15). Details live one layer down (positions here,
// full history in Activity).

import type { Overview, Position } from '../api'
import { EquityChart } from '../components/EquityChart'
import { TradeBars } from '../components/TradeBars'
import { ResumeButton } from '../components/KillSwitch'
import {
  baseAsset, daysBetween, gainClass, money, pct, plainPnl, shortDate, signedMoney,
} from '../format'

function RiskPill({ overview }: { overview: Overview }) {
  const s = overview.risk_state
  if (s === 'RUNNING' && overview.trading_enabled)
    return <span className="pill ok"><span className="dot" />Trading normally</span>
  if (s === 'HALTED_MANUAL')
    return <span className="pill warn"><span className="dot" />Stopped by you</span>
  if (s === 'HALTED_DAILY_LOSS')
    return <span className="pill warn"><span className="dot" />Paused for today (daily safety limit)</span>
  if (s === 'HALTED_DRAWDOWN')
    return <span className="pill bad"><span className="dot" />Stopped by safety limit — needs your OK</span>
  return <span className="pill bad"><span className="dot" />Not trading</span>
}

export function Dashboard({
  overview,
  positions,
  onChanged,
}: {
  overview: Overview
  positions: Position[]
  onChanged: () => void
}) {
  const halted = overview.risk_state !== 'RUNNING'
  const days = overview.paper_started_at ? daysBetween(overview.paper_started_at) : null

  return (
    <div>
      {/* Hero: the one sentence that matters (UI-14). */}
      <div className="card hero">
        <div className="headline">{plainPnl(overview.today_pnl_usdt, 'today')}</div>
        <div className={`amount num ${gainClass(overview.today_pnl_usdt)}`}>
          {signedMoney(overview.today_pnl_usdt)}
        </div>
        <div className="subline num">
          Since the start: <b className={overview.total_pnl_usdt != null && overview.total_pnl_usdt < 0 ? 'loss-text' : 'gain-text'}>
            {signedMoney(overview.total_pnl_usdt)}
          </b>{' '}
          {overview.total_pnl_usdt != null && (
            <span className="muted">({pct(overview.total_pnl_usdt, overview.initial_usdt)})</span>
          )}
          {' — '}your {money(overview.initial_usdt, 0)} is now{' '}
          <b>{money(overview.equity_usdt)}</b>
        </div>
        <div style={{ marginTop: 12 }}>
          <RiskPill overview={overview} />
          {days !== null && (
            <span className="muted small" style={{ marginLeft: 10 }}>
              practice run: day {days + 1}
            </span>
          )}
        </div>
        {halted && (
          <div style={{ marginTop: 14 }}>
            {overview.halt_reason && <div className="notice warn">{overview.halt_reason}</div>}
            {overview.risk_state !== 'HALTED_DAILY_LOSS' && <ResumeButton onDone={onChanged} />}
          </div>
        )}
      </div>

      <div className="card">
        <h2>Your money over time</h2>
        <EquityChart series={overview.equity_series} initial={overview.initial_usdt} />
      </div>

      <div className="grid2">
        <div className="card">
          <h2>Wins and losses, trade by trade</h2>
          <TradeBars results={overview.trade_results} />
          <p className="muted small num">
            {overview.closed_trades} finished trades so far
            {overview.closed_trades > 0 &&
              ` — ${overview.winning_trades} won, ${overview.closed_trades - overview.winning_trades} lost`}
            . Paid in fees: {money(overview.fees_usdt)}.
          </p>
        </div>

        <div className="card">
          <h2>What the system is holding now</h2>
          {positions.length === 0 ? (
            <p className="muted">
              Nothing right now — all the money is in dollars, waiting for the next
              opportunity. That is normal and safe.
            </p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Coin</th>
                  <th className="num">Worth now</th>
                  <th className="num">Result so far</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol}>
                    <td>
                      <b>{baseAsset(p.symbol)}</b>
                      <div className="muted small">bought {shortDate(p.opened_at)}</div>
                    </td>
                    <td className="num">{money(p.value_usdt)}</td>
                    <td className="num">
                      <span className={p.unrealized_pnl_usdt != null && p.unrealized_pnl_usdt < 0 ? 'loss-text' : 'gain-text'}>
                        {signedMoney(p.unrealized_pnl_usdt)}
                      </span>
                      <div className="muted small">{pct(p.unrealized_pnl_usdt, p.cost_usdt)}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {positions.length > 0 && (
            <p className="muted small">
              Each holding has an automatic safety exit: if its price falls to the stop
              level, it is sold to limit the loss. Details are in Activity.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
