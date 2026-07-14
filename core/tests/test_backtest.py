"""Backtesting engine — fill model, fees/slippage, protective exits,
metrics, and the arch §6 determinism guarantee."""

from __future__ import annotations

import pytest

from trader.backtest import Backtester, BacktestParams, SmaCross, StrategyContext
from trader.marketdata import Candle

MIN = 60_000
T0 = 1_735_689_600_000


def candle(i: int, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(T0 + i * MIN, o, h, lo, c, 10.0, 1000.0, 5)


def flat_candles(n: int, price: float = 100.0) -> list[Candle]:
    return [candle(i, price, price, price, price) for i in range(n)]


class BuyOnce:
    """Buys a fixed amount on the first candle it sees, then holds."""

    warmup = 0

    def __init__(self, spend=100.0, sl=None, tp=None):
        self.spend, self.sl, self.tp = spend, sl, tp
        self.bought = False

    def on_candle(self, c: Candle, ctx: StrategyContext) -> None:
        if not self.bought:
            ctx.buy(self.spend, stop_loss_pct=self.sl, take_profit_pct=self.tp)
            self.bought = True


class BuyThenSell:
    """Buys on candle 0, sells on candle 2 (fills land on 1 and 3)."""

    warmup = 0

    def __init__(self, spend=100.0):
        self.spend = spend
        self.n = 0

    def on_candle(self, c: Candle, ctx: StrategyContext) -> None:
        if self.n == 0:
            ctx.buy(self.spend)
        elif self.n == 2:
            ctx.sell_all()
        self.n += 1


def run(strategy, candles, *, fee=0.001, slip=5.0, cash=150.0):
    params = BacktestParams(initial_cash=cash, fee_rate=fee, slippage_bps=slip)
    return Backtester(params).run(strategy, candles, symbol="TESTUSDT", interval="1m")


def test_intent_fills_at_next_open_with_slippage_and_fee():
    candles = [candle(0, 100, 100, 100, 100), candle(1, 110, 110, 110, 110),
               candle(2, 110, 110, 110, 110)]
    result = run(BuyOnce(spend=100.0), candles)
    buy = result.trades[0]
    # Fill at candle 1's open (110), not candle 0's price — plus 5 bps against us.
    assert buy.price == pytest.approx(110 * 1.0005)
    assert buy.fee == pytest.approx(100.0 * 0.001)
    assert buy.qty == pytest.approx(100.0 / (110 * 1.0005))


def test_round_trip_charges_both_sides_and_realizes_pnl():
    candles = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100),
               candle(2, 100, 100, 100, 100), candle(3, 120, 120, 120, 120),
               candle(4, 120, 120, 120, 120)]
    result = run(BuyThenSell(spend=100.0), candles, slip=0.0)
    assert [t.side for t in result.trades] == ["BUY", "SELL"]
    buy, sell = result.trades
    assert sell.reason == "strategy"
    expected_pnl = (120 - 100) * buy.qty - 120 * buy.qty * 0.001
    assert sell.realized_pnl == pytest.approx(expected_pnl)
    assert result.round_trips == 1
    assert result.win_rate_pct == 100.0
    assert result.total_fees == pytest.approx(buy.fee + sell.fee)
    # No open position at the end; equity is pure cash.
    assert result.final_equity == pytest.approx(
        150.0 - 100.0 - buy.fee + 120 * buy.qty - sell.fee
    )


def test_stop_loss_triggers_intra_candle():
    candles = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100),
               candle(2, 100, 101, 94, 95), candle(3, 95, 95, 95, 95)]
    result = run(BuyOnce(spend=100.0, sl=2.0), candles, slip=0.0)
    sell = result.trades[1]
    assert sell.reason == "stop_loss"
    assert sell.price == pytest.approx(100 * 0.98)  # the stop level
    assert sell.realized_pnl < 0
    assert result.win_rate_pct == 0.0


def test_stop_beats_take_profit_when_both_hit():
    # One violent candle spans both the stop (98) and the target (103).
    candles = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100),
               candle(2, 100, 105, 95, 100), candle(3, 100, 100, 100, 100)]
    result = run(BuyOnce(spend=100.0, sl=2.0, tp=3.0), candles, slip=0.0)
    assert result.trades[1].reason == "stop_loss"  # pessimistic by design


def test_take_profit_triggers():
    candles = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100),
               candle(2, 100, 104, 100, 103), candle(3, 103, 103, 103, 103)]
    result = run(BuyOnce(spend=100.0, tp=3.0), candles, slip=0.0)
    sell = result.trades[1]
    assert sell.reason == "take_profit"
    assert sell.realized_pnl > 0


def test_open_position_is_liquidated_at_end_of_data():
    candles = flat_candles(4)
    result = run(BuyOnce(spend=100.0), candles, slip=0.0)
    assert result.trades[-1].reason == "end_of_data"
    # Flat market: we lose exactly the two fees.
    assert result.final_equity == pytest.approx(150.0 - result.total_fees)
    assert result.total_return_pct < 0


def test_cannot_spend_more_than_cash():
    candles = flat_candles(3)
    result = run(BuyOnce(spend=10_000.0), candles, slip=0.0, cash=150.0)
    buy = result.trades[0]
    assert buy.price * buy.qty + buy.fee == pytest.approx(150.0)


def test_max_drawdown_and_equity_curve():
    candles = [candle(0, 100, 100, 100, 100), candle(1, 100, 100, 100, 100),
               candle(2, 80, 80, 80, 80), candle(3, 80, 80, 80, 80)]
    result = run(BuyOnce(spend=150.0), candles, fee=0.0, slip=0.0)
    assert len(result.equity_curve) == 4
    # Equity peaked at 150, trough when price fell 20% with all-in position.
    assert result.max_drawdown_pct == pytest.approx(20.0)


def test_determinism_same_input_same_fingerprint():
    candles = [candle(i, 100 + (i % 7), 101 + (i % 7), 99, 100 + ((i + 3) % 5))
               for i in range(300)]
    r1 = run(SmaCross(fast=5, slow=15, spend_usdt=15.0), list(candles))
    r2 = run(SmaCross(fast=5, slow=15, spend_usdt=15.0), list(candles))
    assert r1.fingerprint() == r2.fingerprint()
    assert r1.summary() == r2.summary()


def test_different_params_different_fingerprint():
    candles = [candle(i, 100 + (i % 7), 101 + (i % 7), 99, 100 + ((i + 3) % 5))
               for i in range(300)]
    r1 = run(SmaCross(fast=5, slow=15), list(candles), fee=0.001)
    r2 = run(SmaCross(fast=5, slow=15), list(candles), fee=0.002)
    assert r1.fingerprint() != r2.fingerprint()


def test_sma_cross_trades_a_trend_reversal():
    # Long flat head: the strategy only sees candles after engine warmup, and
    # needs slow+1 closes of its own before it can detect a cross.
    prices = [100.0] * 50 + [100 + i * 0.5 for i in range(40)] + \
             [120 - i * 0.5 for i in range(40)]
    candles = [candle(i, p, p, p, p) for i, p in enumerate(prices)]
    result = run(SmaCross(fast=5, slow=20, spend_usdt=15.0, stop_loss_pct=50.0,
                          take_profit_pct=50.0), candles)
    sides = [t.side for t in result.trades]
    assert "BUY" in sides and "SELL" in sides
    buy = next(t for t in result.trades if t.side == "BUY")
    sell = next(t for t in result.trades if t.side == "SELL")
    assert sell.realized_pnl > 0  # bought the rise, exited on the reverse cross
    assert buy.open_time < sell.open_time


def test_warmup_prevents_early_trading():
    strategy = SmaCross(fast=5, slow=20, spend_usdt=15.0)
    result = run(strategy, flat_candles(10))  # fewer candles than warmup
    assert result.trades == []
    assert result.final_equity == 150.0
