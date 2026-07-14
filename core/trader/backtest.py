"""Backtesting engine (Sprint 3) — replays historical candles through the same
strategy socket the live Strategy Engine will use (arch §2: strategies emit
intents and cannot tell which mode they run in).

Fill model (deliberately pessimistic — testnet fees are 0, reality is not):
- Intents emitted on candle N execute as market fills at candle N+1's *open*
  (a strategy can never trade on information from the candle it is reading).
- Slippage: fill price moves against us by ``slippage_bps`` on every fill.
- Fees: ``fee_rate`` of traded notional charged per side (D-05 note, ~0.1%).
- Stop-loss / take-profit attached to a position trigger intra-candle using the
  candle's low/high; if both could trigger in one candle, the stop wins
  (pessimistic).

Determinism (arch §6): same candles + same config ⇒ byte-identical result
fingerprint. No randomness, no wall clock, no network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from trader.marketdata import Candle


@dataclass(frozen=True, slots=True)
class BacktestParams:
    initial_cash: float = 150.0  # D-05 pilot capital
    fee_rate: float = 0.001  # per side
    slippage_bps: float = 5.0


@dataclass(slots=True)
class Position:
    qty: float = 0.0
    cost: float = 0.0  # total quote spent incl. nothing (avg entry basis)
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


@dataclass(frozen=True, slots=True)
class Trade:
    open_time: int  # candle open_time of the fill
    side: str  # BUY | SELL
    price: float
    qty: float
    fee: float
    reason: str  # strategy | stop_loss | take_profit | end_of_data
    realized_pnl: float  # 0.0 on BUY


class StrategyContext:
    """What a strategy is allowed to see and do. Mirrors the live intent API:
    requests are queued and executed by the engine at the next candle open —
    never immediately."""

    def __init__(self) -> None:
        self.position = Position()
        self.cash = 0.0
        self._pending: list[tuple[str, float, float | None, float | None]] = []

    def buy(
        self,
        quote_amount: float,
        *,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> None:
        """Spend up to *quote_amount* USDT at the next open. Optional protective
        exits as percentages below/above the fill price (e.g. 1.0 = 1%)."""
        self._pending.append(("BUY", quote_amount, stop_loss_pct, take_profit_pct))

    def sell_all(self) -> None:
        self._pending.append(("SELL", 0.0, None, None))


class Strategy(Protocol):
    """The strategy socket. ``warmup`` candles are fed before trading starts."""

    warmup: int

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None: ...


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    interval: str
    params: BacktestParams
    candles: int
    initial_cash: float
    final_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)  # sampled at each close

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_cash - 1.0) * 100.0

    @property
    def max_drawdown_pct(self) -> float:
        peak, worst = float("-inf"), 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            worst = max(worst, (peak - eq) / peak * 100.0)
        return worst

    @property
    def round_trips(self) -> int:
        return sum(1 for t in self.trades if t.side == "SELL")

    @property
    def win_rate_pct(self) -> float | None:
        sells = [t for t in self.trades if t.side == "SELL"]
        if not sells:
            return None
        return sum(1 for t in sells if t.realized_pnl > 0) / len(sells) * 100.0

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.trades)

    def fingerprint(self) -> str:
        """Determinism check (arch §6): identical run ⇒ identical hash."""
        payload = {
            "symbol": self.symbol,
            "params": [self.params.initial_cash, self.params.fee_rate, self.params.slippage_bps],
            "trades": [
                [t.open_time, t.side, f"{t.price:.10g}", f"{t.qty:.10g}", f"{t.fee:.10g}",
                 f"{t.realized_pnl:.10g}", t.reason]
                for t in self.trades
            ],
            "final_equity": f"{self.final_equity:.10g}",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def summary(self) -> str:
        wr = self.win_rate_pct
        return (
            f"{self.symbol} {self.interval}: {self.candles} candles | "
            f"return {self.total_return_pct:+.2f}% | max DD {self.max_drawdown_pct:.2f}% | "
            f"{self.round_trips} round trips | "
            f"win rate {'n/a' if wr is None else f'{wr:.1f}%'} | "
            f"fees {self.total_fees:.4f} USDT"
        )


class Backtester:
    """Single-symbol long-only spot replay (matches v1 scope: USDT quote, D-10;
    no shorting on spot)."""

    def __init__(self, params: BacktestParams):
        self.params = params

    def run(
        self, strategy: Strategy, candles: list[Candle], *, symbol: str, interval: str
    ) -> BacktestResult:
        p = self.params
        slip = p.slippage_bps / 10_000.0
        ctx = StrategyContext()
        ctx.cash = p.initial_cash
        result = BacktestResult(
            symbol=symbol,
            interval=interval,
            params=p,
            candles=len(candles),
            initial_cash=p.initial_cash,
            final_equity=p.initial_cash,
        )

        def fill(side: str, candle: Candle, price: float, qty: float, reason: str) -> None:
            pos = ctx.position
            notional = price * qty
            fee = notional * p.fee_rate
            if side == "BUY":
                ctx.cash -= notional + fee
                pos.qty += qty
                pos.cost += notional
                realized = 0.0
            else:
                realized = (price - pos.avg_cost) * qty - fee
                ctx.cash += notional - fee
                pos.cost -= pos.avg_cost * qty
                pos.qty -= qty
                if pos.qty <= 1e-12:
                    ctx.position = Position()
            result.trades.append(
                Trade(candle.open_time, side, price, qty, fee, reason, realized)
            )

        for i, candle in enumerate(candles):
            # 1. Execute intents queued on the previous candle, at this open.
            pending, ctx._pending = ctx._pending, []
            if i >= 1:
                for side, quote_amount, sl_pct, tp_pct in pending:
                    if side == "BUY" and ctx.position.qty == 0 and ctx.cash > 0:
                        price = candle.open * (1 + slip)
                        spend = min(quote_amount, ctx.cash / (1 + p.fee_rate))
                        qty = spend / price
                        if qty > 0:
                            fill("BUY", candle, price, qty, "strategy")
                            if sl_pct is not None:
                                ctx.position.stop_loss = price * (1 - sl_pct / 100.0)
                            if tp_pct is not None:
                                ctx.position.take_profit = price * (1 + tp_pct / 100.0)
                    elif side == "SELL" and ctx.position.qty > 0:
                        price = candle.open * (1 - slip)
                        fill("SELL", candle, price, ctx.position.qty, "strategy")

            # 2. Protective exits trigger intra-candle; stop beats target.
            pos = ctx.position
            if pos.qty > 0:
                if pos.stop_loss is not None and candle.low <= pos.stop_loss:
                    fill("SELL", candle, pos.stop_loss * (1 - slip), pos.qty, "stop_loss")
                elif pos.take_profit is not None and candle.high >= pos.take_profit:
                    fill("SELL", candle, pos.take_profit * (1 - slip), pos.qty, "take_profit")

            # 3. Strategy sees the completed candle (after warmup).
            if i >= strategy.warmup:
                strategy.on_candle(candle, ctx)

            result.equity_curve.append(ctx.cash + ctx.position.qty * candle.close)

        # Liquidate at the last close so results compare on cash, not open risk.
        if ctx.position.qty > 0 and candles:
            last = candles[-1]
            fill("SELL", last, last.close * (1 - slip), ctx.position.qty, "end_of_data")
            result.equity_curve[-1] = ctx.cash

        result.final_equity = ctx.cash
        return result


class SmaCross:
    """Reference strategy proving the socket end to end: buy when the fast
    simple moving average crosses above the slow one, exit on the reverse
    cross. Real S4 candidates will be tuned/validated with real data — this
    exists so the engine has a working occupant, not as a recommendation."""

    def __init__(
        self,
        fast: int = 20,
        slow: int = 60,
        *,
        spend_usdt: float = 15.0,  # 10% of pilot capital (D-08)
        stop_loss_pct: float = 1.0,
        take_profit_pct: float = 2.0,
    ):
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow")
        self.fast, self.slow = fast, slow
        self.spend = spend_usdt
        self.sl, self.tp = stop_loss_pct, take_profit_pct
        self.warmup = slow
        self._closes: list[float] = []

    def _sma(self, n: int) -> float:
        return sum(self._closes[-n:]) / n

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        self._closes.append(candle.close)
        if len(self._closes) < self.slow + 1:
            return
        fast_now, slow_now = self._sma(self.fast), self._sma(self.slow)
        prev = self._closes[:-1]
        fast_prev = sum(prev[-self.fast:]) / self.fast
        slow_prev = sum(prev[-self.slow:]) / self.slow
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        if crossed_up and ctx.position.qty == 0:
            ctx.buy(self.spend, stop_loss_pct=self.sl, take_profit_pct=self.tp)
        elif crossed_down and ctx.position.qty > 0:
            ctx.sell_all()
