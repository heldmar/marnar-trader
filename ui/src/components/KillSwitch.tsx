// The kill switch (UI-04): red, reachable from every screen, plain words.
// Mirrors D-21: "stop trading" vs "stop trading AND sell everything".

import { useState } from 'react'
import { api } from '../api'

export function KillSwitchButton({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fire = async (closePositions: boolean) => {
    setBusy(true)
    setError(null)
    try {
      await api.killSwitch(closePositions)
      setOpen(false)
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button className="kill-btn" onClick={() => setOpen(true)}>
        STOP TRADING
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => !busy && setOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>Stop trading?</h2>
            <p className="muted">
              This immediately blocks all new trades. You can start again later from the
              Settings screen.
            </p>
            <div className="choices">
              <button disabled={busy} onClick={() => fire(false)}>
                <b>Stop trading, keep my coins</b>
                <span className="muted small">
                  No new trades. Anything already bought stays as it is.
                </span>
              </button>
              <button disabled={busy} onClick={() => fire(true)}>
                <b>Stop trading and sell everything</b>
                <span className="muted small">
                  No new trades, and everything held is sold back to dollars now.
                </span>
              </button>
            </div>
            {error && <div className="notice bad">Could not stop: {error}</div>}
            <button className="cancel" disabled={busy} onClick={() => setOpen(false)}>
              Cancel — keep trading
            </button>
          </div>
        </div>
      )}
    </>
  )
}

export function ResumeButton({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resume = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.resume()
      onDone()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <span>
      <button className="resume-btn" disabled={busy} onClick={resume}>
        Start trading again
      </button>
      {error && <div className="notice bad">Could not resume: {error}</div>}
    </span>
  )
}
