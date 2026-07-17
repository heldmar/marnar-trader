# Runbook — Paper-trading mode (Sprint 5)

## What it is
`mode: paper` runs the full production stack — journal, risk manager, order
manager, reconciler, strategy engine — against **live Binance production market
data** with a **simulated account** (`PaperGateway`). No API keys are used or
needed: market data is public, and the "exchange" is a crash-safe local state
file. The Q13/D-27 paper clock (3 consecutive weeks) starts the first time the
engine boots and is stamped in the journal (`PAPER_CLOCK_STARTED` event).

## Configuration
`/data/config.yaml`:
```yaml
mode: paper
```
Everything else defaults sensibly (see `PaperConfig` in `trader/config.py`):
20-pair D-09 universe, Donchian 15/15, 3% stop @ 1d (walk-forward validated,
`Context/reports/donchian-walkforward-2026-07-16.md`), 15 USDT per position,
150 USDT paper capital, 60 s poll, event-blackout on, news ingestion every 15 min.

Environment variables (Portainer stack env vars in production — never in files):
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — trade/halt alerts (optional but
  the Q13 operational-readiness checklist requires them working).

## State on the data volume
- `/data/journal.db` — system of record (orders, fills, positions, news, paper clock).
- `/data/paper-account.json` — the simulated exchange account (balances, orders).
  **Deleting it resets the paper account and invalidates the run — don't.**
- `/data/candles/{SYMBOL}/1d/` — candles the engine consumed (parity input).

## Operations
- Health: `GET /api/health` (mode, risk state, reconciliation, positions).
- Kill switch: `POST /api/kill-switch {"close_positions": bool, "reason": str}`.
- Resume after halt: `POST /api/resume` (refused while reconciliation is dirty).
- Parity report (Q13 evidence, run any time):
  `python -m trader.parity` → `/data/reports/parity-YYYY-MM-DD.md`.

## Weekly ops checklist (Q13 / D-27 gate evidence)
Run once a week during the paper period; all three must pass:
1. **Parity:** `docker exec marnar-trader-core python -m trader.parity` —
   report lands in `/data/reports/`, verdict must be PASS (D-27 bands:
   ±2pp return, ±30% trades, ±10% fees).
2. **Health:** `GET /api/health` → `status: ok`, reconciliation clean,
   risk state RUNNING (or an explained halt).
3. **Telegram alive:** the daily report arrived this week (or the delivery
   switch is deliberately off — see below).

## Daily & weekly reports (S7, D-28/D-29)
- Written automatically after every UTC day and every ISO week (Mondays) by a
  scheduler task inside the core service; archived in the journal (`reports`
  table), browsable in the UI's **Reports** tab, and pushed to Telegram while
  fresh (<24 h after the period closed). Catch-up doubles as backfill: gaps
  after downtime heal on their own, archive-only (no stale pings).
- **Pausing the Telegram copy:** UI → Settings → "Telegram reports" toggle,
  or message the bot `/reports_off` / `/reports_on` (commands are accepted
  only from the configured `TELEGRAM_CHAT_ID`; picked up within one scheduler
  cycle, ~5 min). Reports are always archived regardless of the switch; the
  setting survives restarts (journal `app_state`).

## Restart / crash behavior
Same discipline as testnet mode: journal-before-call, deterministic client
order ids, reconcile-before-trade. On boot the engine rebuilds strategy state
by replaying recent candles without trading, restores protective-stop levels
from the journal, and only acts on candles that close after the last processed
one. A restart therefore never double-trades and never re-trades a stale candle.
The paper clock does NOT restart on reboot — only on a material strategy/risk
change (D-27), which is a config change we make deliberately.

## Known model simplifications (documented for the Q13 gate)
- Market fills at live ticker price ±5 bps slippage; fees 0.1%/side charged in
  USDT (matches the backtest fee model by construction; real Binance charges
  buy-side fees in the base asset).
- Protective stops are engine-held and checked every poll (60 s), not
  exchange-held; at 1d rhythm with 3% stops the discretization error is
  negligible, and in live mode stops will be exchange-held (arch note).
- Limit orders fill fully at the limit price when crossed (no partial fills,
  no price improvement). The Donchian engine uses market orders only.
