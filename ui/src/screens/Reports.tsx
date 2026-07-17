// Reports — the S7 archive (D-28/D-29): every daily and weekly report the
// system wrote, newest first, with a P&L badge; tap one to read the full
// plain-language report. Same onion model (UI-13): badge → summary → body.

import { useEffect, useState } from 'react'
import { api, type ReportDetail, type ReportIndexItem } from '../api'
import { money, signedMoney } from '../format'

// Minimal renderer for the report bodies our own builder writes
// (headings, bullets, bold/italic) — no markdown library needed.
function inline(text: string, key: number) {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).filter(Boolean)
  return (
    <span key={key}>
      {parts.map((p, i) =>
        p.startsWith('**') ? (
          <b key={i}>{p.slice(2, -2)}</b>
        ) : p.startsWith('*') ? (
          <i key={i}>{p.slice(1, -1)}</i>
        ) : (
          p
        ),
      )}
    </span>
  )
}

function ReportBody({ md }: { md: string }) {
  const blocks: React.ReactNode[] = []
  let bullets: string[] = []
  const flush = (key: string) => {
    if (bullets.length) {
      blocks.push(
        <ul key={key}>
          {bullets.map((b, i) => (
            <li key={i}>{inline(b, i)}</li>
          ))}
        </ul>,
      )
      bullets = []
    }
  }
  md.split('\n').forEach((line, i) => {
    if (line.startsWith('- ')) {
      bullets.push(line.slice(2))
      return
    }
    flush(`ul-${i}`)
    if (line.startsWith('## ')) blocks.push(<h3 key={i}>{line.slice(3)}</h3>)
    else if (line.startsWith('# ')) blocks.push(<h2 key={i}>{line.slice(2)}</h2>)
    else if (line.trim()) blocks.push(<p key={i}>{inline(line, i)}</p>)
  })
  flush('ul-end')
  return <div className="report-body">{blocks}</div>
}

function periodLabel(r: ReportIndexItem): string {
  if (r.kind === 'weekly') return `Week ${r.period.split('-W')[1]} · ${r.period.slice(0, 4)}`
  return new Date(`${r.period}T12:00:00Z`).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })
}

export function Reports() {
  const [items, setItems] = useState<ReportIndexItem[] | null>(null)
  const [open, setOpen] = useState<ReportDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .reports()
      .then((r) => setItems(r))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const show = (r: ReportIndexItem) => {
    setOpen(null)
    api
      .report(r.kind, r.period)
      .then(setOpen)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }

  if (open) {
    return (
      <div className="card">
        <button className="linklike" onClick={() => setOpen(null)}>
          ← All reports
        </button>
        <ReportBody md={open.body_md} />
        <p className="muted small">
          Written automatically on {new Date(open.generated_at).toLocaleString('en-US')}.
        </p>
      </div>
    )
  }

  return (
    <div className="card">
      <h2>Reports — how each day and week went</h2>
      <p className="muted small">
        The system writes one report after every day (UTC) and every week. A quiet day still
        gets a report — silence would mean something is wrong.
      </p>
      {error && <p className="muted">Could not load reports ({error}); retrying on reload.</p>}
      {items === null ? (
        <p className="muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="muted">
          No reports yet — the first one is written right after the first full day of trading.
        </p>
      ) : (
        <div className="report-list">
          {items.map((r) => {
            const s = r.summary
            const cls = s.pnl_usdt > 0.005 ? 'gain-text' : s.pnl_usdt < -0.005 ? 'loss-text' : ''
            return (
              <button
                key={`${r.kind}-${r.period}`}
                className="report-row"
                onClick={() => show(r)}
              >
                <span className={`pill ${r.kind}`}>{r.kind === 'daily' ? 'Day' : 'Week'}</span>
                <span className="report-when">{periodLabel(r)}</span>
                <span className="muted small">
                  {s.quiet ? 'quiet — no trades' : `${s.trade_count} trade${s.trade_count === 1 ? '' : 's'}`}
                  {s.halt_count > 0 && ' · ⚠ halt'}
                </span>
                <span className={`num report-pnl ${cls}`}>
                  {signedMoney(s.pnl_usdt)}
                  <span className="muted small"> → {money(s.equity_close)}</span>
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
