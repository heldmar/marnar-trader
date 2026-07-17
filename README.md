# MarNar Trader

**An autonomous crypto trading system you actually own.** MarNar Trader runs
24/7 on your own hardware, trades Binance Spot with a proven, rules-based
strategy, and reports back in plain language — what it did, why it did it, and
what it cost. No cloud subscription, no black box, no custody of your keys by
anyone but you.

![Dashboard — plain-language P&L, equity curve, holdings](docs/img/s8-home.png)

## Why it's different

Most trading bots optimize for excitement. MarNar Trader optimizes for
**trust**:

- **Safety first, by construction.** Hard circuit breakers — daily-loss stop,
  drawdown halt, per-coin position caps, trade-rate limits — enforced below
  the strategy, with non-negotiable floors that configuration can only
  tighten, never loosen. A red STOP TRADING button is on every screen.
- **Crash-safe by design.** Every decision is journaled to a write-ahead
  SQLite system of record *before* it touches the exchange. Deterministic
  order IDs mean a crash-and-retry can never double-place an order; on every
  boot the system reconciles against the exchange before trading unlocks.
- **Truthful reporting.** Every trade carries the exact rule and numbers that
  fired it. News headlines are shown *around* trades as context — never
  claimed as the cause. A daily report arrives on Telegram after every UTC
  day (and a weekly on Mondays), with the full archive browsable in the UI.
  Quiet days still report, so silence always means something is wrong — never
  "nothing happened".
- **Evidence before money.** The strategy shipped here (Donchian channel
  breakout, daily candles) was selected by walk-forward backtesting over two
  years of data, then must survive a multi-week paper-trading gate — the full
  production system trading simulated money on live prices — before real
  funds are ever enabled.
- **Built for non-traders.** The web UI explains itself in plain language:
  "You made money today", "why: the price broke above its 15-day high",
  "safety exit at $x". Depth is one tap away, jargon is not required.

![Activity — every trade with the rule that fired, news as labeled context](docs/img/s8-activity.png)

## What's inside

| Piece | What it does |
|-------|--------------|
| Core service (Python 3.12, FastAPI) | Strategy engine, risk manager, order manager, reconciler, crash-safe journal |
| Web UI (React + TypeScript) | Dashboard, activity timeline, report archive, settings, kill switch |
| Telegram alerts & reports | Trade/halt alerts in real time; daily & weekly plain-language reports |
| Backtesting & research toolkit | Historical downloader, fee/slippage-modeled backtester, pair screener, walk-forward tuner |
| Paper mode | The whole stack against live market prices with a simulated account — the mandatory proving ground |

It runs anywhere Docker runs — a Raspberry Pi is enough (the reference
deployment uses one). Two containers, ~600 MB of RAM, no external database.

![Architecture — solution and data flow, as built](docs/img/architecture.svg)

![Reports — daily and weekly, plain language, quiet days included](docs/img/s8-reports.png)

![Settings — safety limits with enforced floors, health strip](docs/img/s8-settings.png)

## Get started

**[docs/INSTALL.md](docs/INSTALL.md)** takes you from `git clone` to your own
running instance — including creating your own Telegram bot — in about
fifteen minutes. Start in paper mode (the default): it trades pretend money
on real prices, so you can watch it work with zero risk.

Further reading:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how it's built and why.
- [docs/RUNBOOK-PAPER.md](docs/RUNBOOK-PAPER.md) — operating the paper run.

## Development

```sh
cd core
pip install -e ".[dev]"
pytest -m "not integration"        # unit tests
ruff check .                       # lint

cd ../ui
npm install && npm run lint && npm run build
```

Full local stack (paper mode, UI on `localhost:18081`):

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

## Security posture

- API keys and tokens live only in environment variables — never in files
  tracked by Git, never in the config file.
- Exchange keys are trade-only: **withdrawal permission stays disabled**.
- The core service is never exposed publicly; only the UI container is
  routed, and the reference deployment keeps it behind reverse-proxy
  authentication.
- State-changing endpoints require a CSRF header; risk limits can only be
  tightened at runtime, never loosened.

*Screenshots above are from paper mode — simulated money on live market
prices; no real funds are shown.*
