# MarNar Trader — Technical Architecture (v0.1 DRAFT)

**Author:** Development team (Senior Python/React dev, reviewed by PO/PM)
**Status:** DRAFT — pending investor approval
**Date:** 2026-07-13
**Constraints honored:** RPi 4B arm64 host, /mnt/storage USB data, no UPS (hard-crash safe), Portainer/NPM deployment, secrets never in Git (D-04, D-13, D-15, D-16).

## 1. Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Backend | Python 3.12, asyncio, FastAPI | Team strength; async fits WS market streams; FastAPI serves both UI API and health endpoints |
| Exchange client | `binance-connector` (official) | First-party REST+WS support incl. testnet base URLs; thin enough to audit |
| Frontend | React + TypeScript + Vite | Team strength; static build served by nginx container (MarNarMon pattern) |
| Database | SQLite (WAL mode) on /mnt/storage | Zero-ops on a Pi; WAL + synchronous=FULL for power-cut durability; single writer fits our design |
| Historical data | Parquet files on /mnt/storage | Compact candle storage for backtests; kept out of the DB |
| Alerts/reports | Telegram Bot API | D-17 |
| Packaging | Docker Compose, 2 containers (core, ui), arm64 | Portainer Repository stack; `pull_policy: build` (known Portainer gotcha) |

## 2. Components (all inside the `core` container as asyncio tasks)

```
Binance ──WS/REST──> Exchange Gateway ──> Market Data Service ──> Strategy Engine
                          ▲                        │                    │ signals
                          │                        ▼                    ▼
                     Order Manager <── Risk Manager (circuit breakers) <┘
                          │                        ▲
                          ▼                        │
                    Trade Journal (SQLite) ── Reconciler (startup)
                          │
                          ▼
                 Reporter (Telegram + archive)      Scheduler (windows, 24/7 default)
```

- **Exchange Gateway** — sole component talking to Binance. Enforces rate-limit budget, exposes one interface with three impls: `live`, `paper` (simulated fills from live book/ticker), `backtest` (historical replay). Strategies cannot tell which mode they run in — this is how R-04's mode parity is guaranteed.
- **Market Data Service** — subscribes to kline/ticker streams for the screened universe; runs the **pair screener** (24h volume / market-cap criteria from UI config, USDT pairs only, validates Binance `minNotional` per pair against current position sizing — D-08 note).
- **Strategy Engine** — pluggable rules-based strategies; emit *intents* (buy/sell X at Y), never orders.
- **Risk Manager** — the only path from intent to order. Enforces, non-overridably: 2% daily loss halt, 20% drawdown full-halt, 10%/coin cap, max 5 positions, max trades/hour, kill switch. Limits live in code + config with floor values; strategy code has no API to bypass it (R-02, G3).
- **Order Manager** — places orders with deterministic idempotent `newClientOrderId` (hash of intent), so a crash-and-retry can never double-place. Persists intent → order → fill transitions to the journal *before* each network call.
- **Reconciler** — on every startup: fetch open orders, balances, recent fills from Binance; diff against journal; adopt/cancel orphans; alert on any mismatch it can't auto-resolve (R-08). This is the no-UPS answer.
- **Reporter** — daily report (P&L, fees, trades, win rate, per-coin) at configured time → Telegram + archived in DB for the UI.
- **Scheduler** — trading-window gate (default 24/7 per D-12), timezone America/Montevideo.

## 3. Web UI (`ui` container)

React SPA served by nginx; nginx proxies `/api` to `core` (same-origin, MarNarMon pattern — no tokens in the browser). Views: Dashboard (positions, today's P&L, health, recent trades), Strategy & screener config, Risk limits (view; edits require typed confirmation), Kill switch (halt / halt+close-positions), Report archive, Mode control (backtest/paper/live with the mandatory-paper gate enforced server-side).

## 4. Deployment (uses MarNar Server, doesn't manage it)

- Private GitHub repo `heldmar/marnar-trader` (D-15); Portainer Repository stack `marnar-trader` with read-only deploy token.
- Both containers join `npm-network` (external) so NPM can route `trader.example.com` → `ui:80` with an NPM access list for auth (D-16). `core` is never exposed publicly.
- Secrets (Binance keys, Telegram token) as Portainer **stack env vars** only.
- Data: named volume backed by `/mnt/storage` for SQLite + Parquet.
- Resource budget: core ≤ 512 MB RAM / ui ≤ 64 MB; measured in S1 against the Pi's existing tenants.

## 5. Crash-safety design rules (no-UPS discipline)

1. Journal write **before** exchange call, state transition write **after** — never in memory only.
2. SQLite WAL + `synchronous=FULL`; single-writer task owns all DB writes.
3. Idempotent client order IDs — retries are safe by construction.
4. Startup = reconcile first, trade second; trading is blocked until reconciliation completes clean.
5. QA chaos suite: `docker kill` / power-cut simulation at every order lifecycle stage must leave state recoverable (S2 exit criterion).

## 6. Testing strategy (QA)

- Unit tests on Risk Manager (every limit, boundary values) — 100% branch coverage required on that module.
- Integration tests against Binance **Spot testnet** in CI (keys as GitHub Actions secrets).
- Backtest determinism test: same data + config ⇒ identical results.
- Chaos tests (Section 5.5).
- A "runaway strategy" fixture that tries to violate every limit — Risk Manager must stop all of them (R-02 acceptance).

## 7. Decisions the investor does NOT need to make (team-owned, listed for transparency)

Library versions, DB schema, internal APIs, test framework (pytest), CI provider (GitHub Actions), code style. Anything touching money rules, risk limits, capital, or go-live gates remains investor-approved.

## 8. Open technical investigations (owner: team)

- **T-01 (from D-14/Q20):** measure home IP change frequency via `marnar.ddns.net` history → recommend Binance key IP-whitelist approach.
- **T-02:** confirm Binance Spot testnet WS stream parity for the pairs the screener selects (testnet has fewer/thinner pairs — may need live *market data* + paper *execution* hybrid for realistic paper trading).
- **T-03:** Pi resource headroom measurement with existing tenants under S1 load.
