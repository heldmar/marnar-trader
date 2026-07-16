// Settings — config editor. Safety limits can only be tightened (the floors
// are non-negotiable, D-06/07/08); strategy changes warn that they restart
// the practice-run clock (D-27) and require an explicit confirmation.

import { useEffect, useState } from 'react'
import { api, ApiError, type ConfigView, type Overview, type SystemInfo } from '../api'
import { ResumeButton } from '../components/KillSwitch'
import { money, shortDateTime } from '../format'

const RISK_FIELDS: { key: string; label: string; help: string; unit: string }[] = [
  { key: 'max_daily_loss_pct', label: 'Daily loss limit', unit: '%',
    help: 'If the account loses this much in one day, trading pauses until tomorrow.' },
  { key: 'max_drawdown_pct', label: 'Total loss limit', unit: '%',
    help: 'If the account falls this far from its best point, everything is sold and trading stops until you restart it.' },
  { key: 'max_position_pct_per_coin', label: 'Most money in one coin', unit: '%',
    help: 'No single coin may hold more than this share of the account.' },
  { key: 'max_open_positions', label: 'Most coins held at once', unit: '',
    help: 'The system will never hold more different coins than this.' },
  { key: 'max_trades_per_hour', label: 'Most trades per hour', unit: '',
    help: 'A brake against runaway behavior.' },
]

const STRATEGY_FIELDS: { key: string; label: string; help: string; unit: string }[] = [
  { key: 'spend_usdt', label: 'Money per purchase', unit: '$',
    help: 'How many dollars each single purchase uses.' },
  { key: 'entry_n', label: 'Buy signal lookback (days)', unit: '',
    help: 'Buy when the price breaks above its highest point of this many days.' },
  { key: 'exit_n', label: 'Sell signal lookback (days)', unit: '',
    help: 'Sell when the price falls below its lowest point of this many days.' },
  { key: 'stop_loss_pct', label: 'Safety exit', unit: '%',
    help: 'Each purchase is sold automatically if it drops this much from the buy price.' },
]

export function Settings({
  overview,
  system,
  onChanged,
}: {
  overview: Overview
  system: SystemInfo | null
  onChanged: () => void
}) {
  const [config, setConfig] = useState<ConfigView | null>(null)
  const [risk, setRisk] = useState<Record<string, number>>({})
  const [paper, setPaper] = useState<Record<string, number>>({})
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ kind: 'ok' | 'bad' | 'warn'; text: string } | null>(null)
  const [confirmReset, setConfirmReset] = useState<string | null>(null)

  const load = () =>
    api.config().then((c) => {
      setConfig(c)
      setRisk(Object.fromEntries(RISK_FIELDS.map((f) => [f.key, Number(c.risk[f.key])])))
      setPaper(Object.fromEntries(STRATEGY_FIELDS.map((f) => [f.key, Number(c.paper[f.key])])))
    })
  useEffect(() => {
    load().catch(() => setMessage({ kind: 'bad', text: 'Could not load settings.' }))
  }, [])

  if (!config) return <div className="loading">Loading settings…</div>

  const dirtyRisk = RISK_FIELDS.some((f) => risk[f.key] !== Number(config.risk[f.key]))
  const dirtyPaper = STRATEGY_FIELDS.some((f) => paper[f.key] !== Number(config.paper[f.key]))

  const save = async (acknowledge = false) => {
    setSaving(true)
    setMessage(null)
    try {
      const changedPaper = Object.fromEntries(
        STRATEGY_FIELDS.filter((f) => paper[f.key] !== Number(config.paper[f.key]))
          .map((f) => [f.key, paper[f.key]]),
      )
      const changedRisk = Object.fromEntries(
        RISK_FIELDS.filter((f) => risk[f.key] !== Number(config.risk[f.key]))
          .map((f) => [f.key, risk[f.key]]),
      )
      const res = await api.putConfig({
        paper: changedPaper,
        risk: changedRisk,
        acknowledge_clock_reset: acknowledge,
      })
      setConfirmReset(null)
      setMessage({
        kind: 'ok',
        text:
          'Saved.' +
          (res.clock_reset ? ' The practice-run clock has restarted from zero.' : '') +
          (res.restart_required
            ? ' The change takes full effect the next time the system restarts.'
            : ''),
      })
      await load()
      onChanged()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setConfirmReset(e.message)
      } else if (e instanceof ApiError && e.status === 422) {
        setMessage({
          kind: 'bad',
          text:
            'Not saved: a safety limit cannot be made looser than its built-in maximum. ' +
            'Limits can only be made stricter.',
        })
      } else {
        setMessage({ kind: 'bad', text: e instanceof Error ? e.message : String(e) })
      }
    } finally {
      setSaving(false)
    }
  }

  const numInput = (
    state: Record<string, number>,
    setState: (v: Record<string, number>) => void,
    f: { key: string; label: string; help: string; unit: string },
    extra?: string,
  ) => (
    <label className="field" key={f.key}>
      <div className="name">{f.label} {f.unit && <span className="muted">({f.unit})</span>}</div>
      <div className="help">{f.help}</div>
      <input
        type="number"
        step="any"
        value={Number.isFinite(state[f.key]) ? state[f.key] : ''}
        onChange={(e) => setState({ ...state, [f.key]: Number(e.target.value) })}
      />
      {extra && <span className="floor-note"> {extra}</span>}
    </label>
  )

  return (
    <div>
      <div className="card">
        <h2>Trading status</h2>
        <p className="muted">
          {overview.risk_state === 'RUNNING' && overview.trading_enabled
            ? 'The system is allowed to trade.'
            : `Trading is currently stopped${overview.halt_reason ? ` — ${overview.halt_reason}` : ''}.`}
        </p>
        {overview.risk_state !== 'RUNNING' && overview.risk_state !== 'HALTED_DAILY_LOSS' && (
          <ResumeButton onDone={onChanged} />
        )}
        {overview.risk_state === 'HALTED_DAILY_LOSS' && (
          <p className="muted small">
            This pause clears by itself at the start of the next trading day.
          </p>
        )}
      </div>

      <div className="grid2">
        <div className="card">
          <h2>Safety limits</h2>
          <p className="muted small">
            These protect the money. You can make them stricter here; the built-in
            maximums can never be loosened, not even by you.
          </p>
          {RISK_FIELDS.map((f) =>
            numInput(risk, setRisk, f,
              `built-in maximum: ${config.risk_floors[f.key]}${f.unit}`),
          )}
        </div>

        <div className="card">
          <h2>Strategy settings</h2>
          <div className="notice warn">
            Changing anything here restarts the practice-run clock — the 3-week
            evaluation starts over, because results before and after the change are not
            comparable.
          </div>
          {STRATEGY_FIELDS.map((f) => numInput(paper, setPaper, f))}
          <p className="muted small">
            Trading style: {String(config.paper.interval)} candles across{' '}
            {(config.paper.symbols as string[])?.length ?? 0} coins. The coin list is
            managed by the screener, not edited here.
          </p>
        </div>
      </div>

      {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
      <button
        className="save-btn"
        disabled={saving || (!dirtyRisk && !dirtyPaper)}
        onClick={() => save(false)}
      >
        {saving ? 'Saving…' : 'Save changes'}
      </button>

      {confirmReset && (
        <div className="modal-backdrop" onClick={() => setConfirmReset(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>This restarts the practice run</h2>
            <p>
              You changed how the strategy trades. The 3-week practice evaluation will
              start over from day one, because old results would no longer say anything
              about the new settings.
            </p>
            <div className="choices">
              <button disabled={saving} onClick={() => save(true)}>
                <b>I understand — save and restart the clock</b>
              </button>
            </div>
            <button className="cancel" onClick={() => setConfirmReset(null)}>
              Cancel — keep current settings
            </button>
          </div>
        </div>
      )}

      {system && (
        <div className="card" style={{ marginTop: 24 }}>
          <h2>System health</h2>
          <div className="healthrow">
            <span className={`pill ${system.reconciliation?.clean ? 'ok' : 'bad'}`}>
              <span className="dot" />
              records {system.reconciliation?.clean ? 'match the exchange' : 'need attention'}
            </span>
            <span className={`pill ${system.news_last_seen ? 'ok' : 'warn'}`}>
              <span className="dot" />
              news feed: {system.news_items_24h} headlines in 24h
            </span>
            <span className="pill ok">
              <span className="dot" />
              last market check: {system.last_candle_processed
                ? shortDateTime(new Date(system.last_candle_processed).toISOString())
                : '—'}
            </span>
          </div>
          <p className="muted small num" style={{ marginBottom: 0 }}>
            Orders in the last 24 hours: {system.orders_last_24h}. Money per purchase:{' '}
            {money(Number(config.paper.spend_usdt), 0)}.
          </p>
        </div>
      )}
    </div>
  )
}
