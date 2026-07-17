"""Paper-vs-backtest parity report (Sprint 5 — the Q13/D-27 measuring stick).

The Q13 gate (Context/Q13-PAPER-GATE-PROPOSAL.md) requires paper results to
stay within agreed bands of a backtest run on the *same weeks* of data:

- fee-adjusted return within ±2 percentage points,
- trade count within ±30%,
- realized fees within ±10% of the fee model.

The comparison must be like-for-like, so the backtest side here is a
*portfolio* replay: all symbols share one cash balance and the same risk caps
the live RiskManager enforces (max open positions, per-coin % of equity) —
the single-symbol S3 Backtester would overstate what paper was allowed to do.
Candles come from the CandleStore, which the engine itself appends to, so the
replay sees exactly the data the live strategy saw.

CLI: python -m trader.parity [--data-dir DIR] [--db PATH]
Report: /data/reports/parity-YYYY-MM-DD.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trader.journal import Journal
from trader.macro_events import all_event_times_ms
from trader.marketdata import INTERVAL_MS, Candle, CandleStore
from trader.strategies import DonchianBreakout, EventBlackout

# Q13 proposed (and D-27 approved) bands.
RETURN_BAND_PP = 2.0
TRADES_BAND_PCT = 30.0
FEES_BAND_PCT = 10.0


# -- portfolio replay -------------------------------------------------------------


@dataclass(slots=True)
class _Pos:
    qty: float = 0.0
    cost: float = 0.0
    stop: float | None = None
    tp: float | None = None


class _ReplayCtx:
    """StrategyContext shape for the portfolio replay."""

    def __init__(self) -> None:
        self.position = _Pos()
        self.cash = 0.0
        self.requests: list[tuple[str, float, float | None, float | None]] = []

    def buy(self, quote_amount, *, stop_loss_pct=None, take_profit_pct=None) -> None:
        self.requests.append(("BUY", quote_amount, stop_loss_pct, take_profit_pct))

    def sell_all(self) -> None:
        self.requests.append(("SELL", 0.0, None, None))


@dataclass(slots=True)
class PortfolioResult:
    initial_cash: float
    final_equity: float
    buys: int = 0
    sells: int = 0
    fees: float = 0.0
    skipped_position_cap: int = 0
    skipped_coin_cap: int = 0

    @property
    def return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1.0) * 100.0

    @property
    def trades(self) -> int:
        return self.buys + self.sells


class PortfolioReplay:
    """Multi-symbol replay with shared cash and the live risk caps. Fill model
    matches the S3 Backtester (next-open fills, slippage against us, fee per
    side, intra-candle stops with stop-beats-target)."""

    def __init__(
        self,
        *,
        initial_cash: float = 150.0,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        max_open_positions: int = 5,
        max_position_pct: float = 10.0,
        spend_usdt: float = 15.0,
        entry_n: int = 15,
        exit_n: int = 15,
        stop_loss_pct: float = 3.0,
        event_blackout: bool = True,
    ):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.fee = fee_rate
        self.slip = slippage_bps / 10_000.0
        self.max_pos = max_open_positions
        self.max_pct = max_position_pct
        self.spend = spend_usdt
        events = list(all_event_times_ms()) if event_blackout else []

        def make():
            inner = DonchianBreakout(
                entry_n, exit_n, spend_usdt=spend_usdt, stop_loss_pct=stop_loss_pct
            )
            return EventBlackout(inner, events) if events else inner

        self._make = make
        self.positions: dict[str, _Pos] = {}
        self.result = PortfolioResult(initial_cash=initial_cash, final_equity=initial_cash)

    def run(
        self, series: dict[str, list[Candle]], *, trade_from_ms: int, trade_to_ms: int | None = None
    ) -> PortfolioResult:
        strategies = {s: self._make() for s in series}
        pending: dict[str, list[tuple[str, float, float | None, float | None]]] = {}
        last_close: dict[str, float] = {}

        ticks = sorted({c.open_time for candles in series.values() for c in candles})
        by_time: dict[str, dict[int, Candle]] = {
            s: {c.open_time: c for c in candles} for s, candles in series.items()
        }
        for t in ticks:
            if trade_to_ms is not None and t >= trade_to_ms:
                break
            for symbol in series:
                candle = by_time[symbol].get(t)
                if candle is None:
                    continue
                # 1. pending intents fill at this open (next-open rule)
                for side, quote, sl, tp in pending.pop(symbol, []):
                    if side == "BUY":
                        self._buy(symbol, candle, quote, sl, tp)
                    else:
                        self._sell(symbol, candle.open * (1 - self.slip))
                # 2. intra-candle protective exits, stop first
                pos = self.positions.get(symbol)
                if pos and pos.qty > 0:
                    if pos.stop is not None and candle.low <= pos.stop:
                        self._sell(symbol, pos.stop * (1 - self.slip))
                    elif pos.tp is not None and candle.high >= pos.tp:
                        self._sell(symbol, pos.tp * (1 - self.slip))
                # 3. strategy sees the closed candle
                ctx = _ReplayCtx()
                pos = self.positions.get(symbol)
                ctx.position = pos if pos else _Pos()
                ctx.cash = self.cash
                strategies[symbol].on_candle(candle, ctx)  # type: ignore[arg-type]
                if candle.open_time >= trade_from_ms and ctx.requests:
                    pending[symbol] = ctx.requests
                last_close[symbol] = candle.close

        equity = self.cash + sum(
            p.qty * last_close.get(s, 0.0) for s, p in self.positions.items()
        )
        self.result.final_equity = equity
        return self.result

    def _equity_mark(self, marks: dict[str, float]) -> float:
        return self.cash + sum(p.qty * marks.get(s, 0.0) for s, p in self.positions.items())

    def _buy(self, symbol: str, candle: Candle, quote: float, sl, tp) -> None:
        held = {s for s, p in self.positions.items() if p.qty > 0}
        if symbol not in held and len(held) >= self.max_pos:
            self.result.skipped_position_cap += 1
            return
        price = candle.open * (1 + self.slip)
        # Per-coin cap vs current equity, marked at this candle's open.
        pos = self.positions.get(symbol, _Pos())
        equity = self.cash + sum(
            p.cost for p in self.positions.values()  # conservative: cost basis mark
        )
        if pos.cost + quote > equity * self.max_pct / 100.0:
            self.result.skipped_coin_cap += 1
            return
        spend = min(quote, self.cash / (1 + self.fee))
        qty = spend / price
        if qty <= 0:
            return
        fee = spend * self.fee
        self.cash -= spend + fee
        pos.qty += qty
        pos.cost += spend
        pos.stop = price * (1 - sl / 100.0) if sl else None
        pos.tp = price * (1 + tp / 100.0) if tp else None
        self.positions[symbol] = pos
        self.result.buys += 1
        self.result.fees += fee

    def _sell(self, symbol: str, price: float) -> None:
        pos = self.positions.get(symbol)
        if not pos or pos.qty <= 0:
            return
        notional = pos.qty * price
        fee = notional * self.fee
        self.cash += notional - fee
        self.result.sells += 1
        self.result.fees += fee
        del self.positions[symbol]


# -- paper side (journal + paper account) ---------------------------------------------


@dataclass(slots=True)
class PaperStats:
    started_at_ms: int
    buys: int
    sells: int
    fees: float
    equity: float
    initial: float

    @property
    def return_pct(self) -> float:
        return (self.equity / self.initial - 1.0) * 100.0

    @property
    def trades(self) -> int:
        return self.buys + self.sells


def paper_stats(
    journal: Journal, store: CandleStore, *, interval: str, initial: float
) -> PaperStats | None:
    started = journal.get_state("engine:paper_started_at")
    if started is None:
        return None
    # QA1-15: the paper side must count only what happened since the clock
    # started — after a D-27 clock reset, pre-reset fills would otherwise make
    # the parity comparison structurally wrong.
    started_iso = datetime.fromtimestamp(int(started) / 1000, tz=UTC).isoformat(
        timespec="milliseconds"
    )
    baseline = journal.get_state("engine:paper_initial_equity")
    if baseline is not None:
        initial = float(baseline)
    buys = sells = 0
    fees = 0.0
    for r in journal.fills_between(started_iso, "9999"):
        if r["side"] == "BUY":
            buys += 1
        else:
            sells += 1
        fees += float(r["fee"])
    # Mark open positions at the last stored close for that symbol.
    equity_open = 0.0
    for pos in journal.positions():
        cov = store.coverage(pos["symbol"], interval)
        if cov is None:
            continue
        last = store.read(pos["symbol"], interval, start_ms=cov[1])
        equity_open += float(pos["quantity"]) * last[-1].close
    # Cash reconstructed from flows. With the QA1-16 convention (entry fee in
    # the cost basis) a round trip's realized P&L equals its net cash flow, and
    # open positions' cost already includes their buy fees.
    realized = float(journal.realized_pnl_since(started_iso))
    open_cost = sum(float(p["cost"]) for p in journal.positions())
    cash = initial - open_cost + realized
    return PaperStats(
        started_at_ms=int(started),
        buys=buys,
        sells=sells,
        fees=fees,
        equity=cash + equity_open,
        initial=initial,
    )


# -- report ------------------------------------------------------------------------------


def _band_check(label: str, delta: float, band: float, unit: str) -> str:
    ok = abs(delta) <= band
    return (
        f"| {label} | {delta:+.2f}{unit} | ±{band:g}{unit} | "
        f"{'✅ PASS' if ok else '❌ FAIL'} |"
    )


def render_report(
    paper: PaperStats, replay: PortfolioResult, *, interval: str, generated: str
) -> str:
    started = datetime.fromtimestamp(paper.started_at_ms / 1000, tz=UTC)
    trades_delta_pct = (
        (paper.trades - replay.trades) / replay.trades * 100.0 if replay.trades else 0.0
    )
    fees_delta_pct = (
        (paper.fees - replay.fees) / replay.fees * 100.0 if replay.fees else 0.0
    )
    lines = [
        "# Paper-vs-backtest parity report (Q13/D-27)",
        "",
        f"Generated: {generated} · Paper clock started: {started:%Y-%m-%d %H:%M UTC} "
        f"({(datetime.now(tz=UTC) - started).days} days ago; gate needs 21)",
        "",
        "| | Paper (live) | Backtest (same weeks) |",
        "|---|---:|---:|",
        f"| Return | {paper.return_pct:+.2f}% | {replay.return_pct:+.2f}% |",
        f"| Trades (fills) | {paper.trades} | {replay.trades} |",
        f"| Fees (USDT) | {paper.fees:.4f} | {replay.fees:.4f} |",
        f"| Final equity | {paper.equity:.2f} | {replay.final_equity:.2f} |",
        "",
        "Backtest side: portfolio replay over the engine's own stored candles "
        f"(@ {interval}), shared cash, max-position and per-coin caps applied "
        f"(skipped: {replay.skipped_position_cap} position-cap, "
        f"{replay.skipped_coin_cap} coin-cap).",
        "",
        "## Q13 bands",
        "",
        "| Check | Delta | Band | Verdict |",
        "|-------|------:|-----:|---------|",
        _band_check("Return (paper − backtest)", paper.return_pct - replay.return_pct,
                    RETURN_BAND_PP, "pp"),
        _band_check("Trade count", trades_delta_pct, TRADES_BAND_PCT, "%"),
        _band_check("Fees vs model", fees_delta_pct, FEES_BAND_PCT, "%"),
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse
    from pathlib import Path

    from trader.config import load_config

    parser = argparse.ArgumentParser(description="Q13 paper-vs-backtest parity report")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    journal = Journal(config.db_path)
    store = CandleStore(config.candles_dir)
    p = config.paper

    stats = paper_stats(journal, store, interval=p.interval, initial=p.initial_usdt)
    if stats is None:
        print("Paper engine has never started (no paper clock in journal) — nothing to compare.")
        return 1

    series: dict[str, list[Candle]] = {}
    warmup_ms = (max(p.entry_n, p.exit_n) + 5) * INTERVAL_MS[p.interval]
    for symbol in p.symbols:
        candles = store.read(symbol, p.interval, start_ms=stats.started_at_ms - warmup_ms)
        if candles:
            series[symbol] = candles
    replay = PortfolioReplay(
        initial_cash=p.initial_usdt,
        fee_rate=config.fee_rate_per_side,
        slippage_bps=config.backtest.slippage_bps,
        max_open_positions=config.risk.max_open_positions,
        max_position_pct=config.risk.max_position_pct_per_coin,
        spend_usdt=p.spend_usdt,
        entry_n=p.entry_n,
        exit_n=p.exit_n,
        stop_loss_pct=p.stop_loss_pct,
        event_blackout=p.event_blackout,
    ).run(series, trade_from_ms=stats.started_at_ms)

    generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    md = render_report(stats, replay, interval=p.interval, generated=generated)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"parity-{datetime.now(tz=UTC):%Y-%m-%d}.md"
    out.write_text(md)
    print(md)
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
