// App shell: wordmark (UI-08), amber TEST MODE banner whenever no real money
// is involved (UI-05), onion navigation (UI-13: Home → Activity → Settings),
// and the red kill switch visible on every screen (UI-04).

import { useCallback, useEffect, useState } from 'react'
import { api, type Overview, type Position, type SystemInfo, type TimelineItem } from './api'
import { KillSwitchButton } from './components/KillSwitch'
import { Dashboard } from './screens/Dashboard'
import { Activity } from './screens/Activity'
import { Settings } from './screens/Settings'

type Tab = 'home' | 'activity' | 'settings'
const REFRESH_MS = 30_000

function tabFromUrl(): Tab {
  const t = new URLSearchParams(window.location.search).get('tab')
  return t === 'activity' || t === 'settings' ? t : 'home'
}

export default function App() {
  const [tab, setTabState] = useState<Tab>(tabFromUrl)
  const setTab = useCallback((t: Tab) => {
    setTabState(t)
    const url = t === 'home' ? window.location.pathname : `?tab=${t}`
    window.history.replaceState(null, '', url)
  }, [])
  const [overview, setOverview] = useState<Overview | null>(null)
  const [positions, setPositions] = useState<Position[]>([])
  const [timeline, setTimeline] = useState<TimelineItem[]>([])
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [ov, pos, tl, sys] = await Promise.all([
        api.overview(), api.positions(), api.timeline(), api.system(),
      ])
      setOverview(ov)
      setPositions(pos)
      setTimeline(tl)
      setSystem(sys)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, REFRESH_MS)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="wordmark"><b>MarNar</b> <span>Trader</span></div>
        <nav className="nav">
          <button className={tab === 'home' ? 'active' : ''} onClick={() => setTab('home')}>
            Home
          </button>
          <button className={tab === 'activity' ? 'active' : ''} onClick={() => setTab('activity')}>
            Activity
          </button>
          <button className={tab === 'settings' ? 'active' : ''} onClick={() => setTab('settings')}>
            Settings
          </button>
        </nav>
        <KillSwitchButton onDone={refresh} />
      </header>

      {overview && overview.mode !== 'live' && (
        <div className="testmode">
          PRACTICE MODE — the system trades with pretend money on real market prices. No
          real money is at risk.
        </div>
      )}

      {error && (
        <div className="notice bad">
          Cannot reach the trading system right now ({error}). Showing the last data
          received; retrying automatically.
        </div>
      )}

      {!overview ? (
        <div className="loading">Connecting to the trading system…</div>
      ) : tab === 'home' ? (
        <Dashboard overview={overview} positions={positions} onChanged={refresh} />
      ) : tab === 'activity' ? (
        <Activity items={timeline} positions={positions} />
      ) : (
        <Settings overview={overview} system={system} onChanged={refresh} />
      )}
    </div>
  )
}
