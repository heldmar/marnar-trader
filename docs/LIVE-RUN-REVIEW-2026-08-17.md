# Live paper run review — 18 days after the 2026-08-05 advisory

**Date:** 2026-08-17 · **Window:** 2026-07-30 20:14 UTC → 2026-08-17 23:25 UTC (18 days, D-27 clock) · **Status:** report. Nothing here is a decision.

**Sources:** live account read from `marnar-trader-core` on the Pi (`/api/overview`, `/api/positions`, `/api/health`, `/api/timeline`) and `docker exec marnar-trader-core python -m trader.parity`, 23:25–23:33 UTC. Benchmarks computed from Binance daily klines over the identical window on the 19 configured symbols.

## 1. Result

| | |
|---|---:|
| Baseline equity (07-30 reset) | $147.414210 |
| Equity 2026-08-17 23:25 UTC | $144.366598 |
| **Return** | **−2.07%** |
| Closed trades | 4 |
| Winning trades | **0** |
| Fees | $0.1285 |
| Open positions | 3 (BNB, TRX, XAUT) |

No code has changed since `b993743` (2026-08-05). The run proceeded exactly as D-44(a) intended — as a machinery test.

**All four closed trades exited via `protective:stop_loss`. Not one exit came from the Donchian exit channel.**

| Symbol | Entry | Exit | Held | P&L | Exit |
|---|---|---|---|---:|---|
| BNBUSDT | 07-31 | 08-01 | 1.8 d | −$0.431 | stop loss |
| UNIUSDT | 07-30 | 08-04 | 5.1 d | −$0.477 | stop loss |
| TAOUSDT | 08-09 | 08-11 | 2.6 d | −$0.455 | stop loss |
| WLDUSDT | 08-17 | 08-17 | 0.9 d | −$0.431 | stop loss |

Open: BNB −$0.240, TRX −$0.157, XAUT +$0.419. The only profitable position is tokenised gold.

## 2. D-46(a) hurdle — the live run FAILS both arms

| 18 days, 30 Jul → 17 Aug | fully invested | at 45% exposure |
|---|---:|---:|
| **Donchian 15/15/3.0 (live paper)** | — | **−2.07%** |
| Equal-weight hold, screened 19 | −2.91% | −1.31% |
| Buy and hold BTC | −0.40% | −0.18% |

Strategy loses to its own universe by 0.76pp and to BTC by 1.89pp. **Directionally consistent with the honest 8-quarter backtest** (D-46b: strategy +4.42% vs BTC@45% +14.12%) — the harness and the live machine agree.

**⚠️ 18 days and 4 trades prove nothing about edge.** The advisory's power calculation requires 53 weeks best case, 325 weeks typical; this is 2.6 weeks. The window cannot condemn the strategy, and could not have vindicated it either.

## 3. New findings

### (A) The D-27 gate currently FAILS two of three Q13 parity bands

Gate closes 2026-08-20. `trader.parity` today:

| Check | Paper | Backtest | Delta | Band | Verdict |
|---|---:|---:|---:|---:|---|
| Return | −1.36% | −0.44% | −0.91pp | ±2pp | ✅ PASS |
| Trade count | 10 fills | 5 fills | +100% | ±30% | ❌ FAIL |
| Fees vs model | $0.1285 | $0.0671 | +91.5% | ±10% | ❌ FAIL |

The fee failure follows arithmetically from the trade count, so this is **one** defect: the live engine trades at double the rate its own replay predicts.

**Leading hypothesis — NOT VERIFIED:** live checks protective stops against the ticker every 60s; the replay checks stops once per daily candle, so live stops out on intraday wicks the replay never sees. Consistent with all four exits being stops, but untested. **Falsification test:** re-run the replay with intrabar stop checking against daily high/low and see whether fills converge on 10.

**If it holds, every point-in-time backtest figure in this project understates stop frequency and overstates return**, on top of the measured 2.6pp survivorship correction. It would deepen the existing conclusion, not change its direction.

### (B) Reported P&L does not reconcile with the trade ledger

`total_pnl_usdt` = **−$3.0385**. Sum of attributed components: 4 closed trades −$1.7936, 3 open positions +$0.0217 unrealised = **−$1.7719**. **$1.27 unexplained — 42% of the reported loss.** The headline is internally consistent ($147.4142 − $3.0385 = $144.3757), so the gap sits between the equity curve and the trade attribution.

**Not diagnosed. No cause is asserted here.** Same family as the July reporting clock-boundary bug. A P&L number that cannot be tied to its own trades should not underpin a gate decision.

### (C) Ten poll-cycle failures; the alert about them could not be sent

`engine poll failed` on 10 occasions across 8 of 12 days (08-07, 08, 10, 12, 13, 14, 15, 16), all traced to transient DNS resolution failure on the Pi (`api.binance.com`; the RSS feeds fail at the same moments). The engine recovered every time with **0 container restarts**.

Two consequences: each failed cycle is one in which `_check_protective_stops` did not run; and the Telegram alert raised to report the outage **failed to send because of the same DNS outage**, with no visible retry. The system cannot currently notify that it lost the network.

## 4. Reliability (for the 2026-08-20 gate)

| | |
|---|---|
| Container uptime | 12 days, `healthy`, **0 restarts** (recreated 08-05 for the UI proxy fix) |
| Reconciliation | **clean** — no adopted/failed/synced/orphans/foreign orders/errors |
| Risk state | `RUNNING`, no halts, `equity_peak` 150.0, drawdown −3.75% vs the 20% limit |
| Risk caps | enforcing (per-coin cap observed rejecting, positions ≤ $13.05) |
| Poll-cycle failures | 10 (§3C) |
| Parity bands | 1 of 3 pass (§3A) |
| Config | 15/15/3.0, 19 symbols, `rotate_universe` false — unchanged, as D-44 requires |

## 5. Advisory follow-through since 2026-08-05

| Item | Status |
|---|---|
| R2 — vary the screen, strategy fixed | **Not started** |
| R3 — make `parity.PortfolioReplay` strategy-pluggable | **Not started** |
| F2 — inverse-volatility sizing | **Not started** |
| Paper run to 2026-08-20 | On track |

No research work has been done since 2026-08-05.

## 6. On commissioning a second audit

**Recommendation: no, not now.** The 2026-08-05 advisory produced 8 sections, a ranked family list with falsification tests, and one structural conclusion a second reviewer cannot overturn: the sample cannot validate a modest alpha (residual noise 2.6–5%/week ⇒ 53–325 weeks). That is arithmetic about the available data, not an opinion. A second auditor works from the same two years of daily candles, the same 19-name universe, the same D-10 long-only spot constraint and the same ~$150 book.

The first advisory's top-ranked, cheapest, no-new-engine test (R2) has not been run. Buying a second opinion before running the first opinion's headline experiment buys a document, not information.

**Two conditions would flip this:** (i) R2 returns positive and an independent challenge is wanted before real capital; or (ii) a binding constraint is relaxed (D-10 long-only spot, the 5-position cap, or the daily interval), which reopens families the advisory ruled structurally dead.

## 7. Proposed next steps (investor decisions marked)

1. **⚠️ INVESTOR DECISION, due 2026-08-20:** how the gate handles the §3A parity failure — hold open pending the investigation, pass on the reliability criteria that are met while recording the divergence as a named defect, or extend two weeks per D-27's failure clause.
2. **Engineering, no decision needed:** verify the intrabar-stop hypothesis (§3A) and trace the $1.27 attribution gap (§3B).
3. **R2 as the go/no-go for the trading thesis:** four screen definitions, strategy fixed, ≥6 of 8 leave-one-quarter-out cuts *and* the up-week subsample, survivorship re-differenced per screen (the ≈2.6pp figure does not generalise across screens — D-46b).
4. **⚠️ INVESTOR DECISION, conditional on 3:** if no screen clears both D-46(a) arms, choose between winding down and repositioning as disciplined mechanical allocation (advisory §6). Do not reopen parameter search — D-44(a).
5. **Due for revisit 2026-08-20 per D-44(c):** `rotate_universe`, still off; the live book has been drifting from the screener's picks for nearly three weeks.
