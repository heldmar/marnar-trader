"""Sprint 4 strategy candidates and D-23 news-rule wrappers, plus the
1m→coarser resampler that feeds them."""

from __future__ import annotations

import pytest

from trader.backtest import Backtester, BacktestParams, StrategyContext
from trader.marketdata import Candle, resample
from trader.strategies import (
    DonchianBreakout,
    EventBlackout,
    NewsSpikePause,
    RsiMeanReversion,
    TrendFollow,
)

MIN = 60_000
T0 = 1_735_689_600_000  # aligned on every interval we use


def candle(i: int, o: float, h: float, lo: float, c: float, *, vol: float = 10.0) -> Candle:
    return Candle(T0 + i * MIN, o, h, lo, c, vol, vol * c, 5)


def close_series(closes: list[float]) -> list[Candle]:
    return [candle(i, c, c, c, c) for i, c in enumerate(closes)]


def pending_sides(ctx: StrategyContext) -> list[str]:
    return [p[0] for p in ctx._pending]


def feed(strategy, candles: list[Candle], *, position_qty: float = 0.0) -> StrategyContext:
    """Drive a strategy directly and return the context afterwards."""
    ctx = StrategyContext()
    ctx.cash = 150.0
    ctx.position.qty = position_qty
    for c in candles:
        strategy.on_candle(c, ctx)
    return ctx


# --- resample -------------------------------------------------------------


class TestResample:
    def test_aggregates_ohlcv_per_bucket(self):
        candles = [
            candle(0, 100, 105, 99, 101),
            candle(1, 101, 110, 100, 108),
            candle(2, 108, 109, 95, 96),
            candle(3, 96, 97, 94, 97),
            candle(4, 97, 99, 96, 98),
        ]
        out = resample(candles, 5 * MIN)
        assert len(out) == 1
        bar = out[0]
        assert bar.open_time == T0
        assert (bar.open, bar.high, bar.low, bar.close) == (100, 110, 94, 98)
        assert bar.volume == pytest.approx(50.0)
        assert bar.trades == 25

    def test_epoch_alignment_and_trailing_partial_kept(self):
        candles = [candle(i, 100 + i, 100 + i, 100 + i, 100 + i) for i in range(3, 8)]
        out = resample(candles, 5 * MIN)
        # candles 3,4 belong to the T0 bucket; 5,6,7 to the next (partial, kept)
        assert [b.open_time for b in out] == [T0, T0 + 5 * MIN]
        assert out[0].open == 103 and out[0].close == 104
        assert out[1].open == 105 and out[1].close == 107

    def test_gap_in_source_data(self):
        candles = [candle(0, 1, 1, 1, 1), candle(11, 2, 2, 2, 2)]
        out = resample(candles, 5 * MIN)
        assert [b.open_time for b in out] == [T0, T0 + 10 * MIN]

    def test_empty(self):
        assert resample([], 5 * MIN) == []


# --- RsiMeanReversion -----------------------------------------------------


class TestRsiMeanReversion:
    def test_rsi_extremes(self):
        s = RsiMeanReversion(period=14)
        feed(s, close_series([100 + i for i in range(20)]))
        assert s.rsi == 100.0  # all gains, no losses

    def test_buys_on_oversold_and_exits_on_recovery(self):
        s = RsiMeanReversion(period=3, oversold=30, exit_rsi=55, spend_usdt=15)
        # steady, then a hard multi-candle drop → RSI collapses
        ctx = feed(s, close_series([100.0] * 5 + [97.0, 94.0, 91.0]))
        # position stays flat in this harness (no fills), so every oversold
        # candle re-signals — the backtester collapses these into one fill
        assert set(pending_sides(ctx)) == {"BUY"}
        assert s.rsi is not None and s.rsi < 30
        # now flat→long recovery: same strategy object, position open
        ctx2 = feed(s, close_series([95.0, 99.0, 103.0]), position_qty=1.0)
        assert "SELL" in pending_sides(ctx2)

    def test_no_signal_before_seed_complete(self):
        s = RsiMeanReversion(period=14)
        ctx = feed(s, close_series([100.0, 90.0, 80.0]))  # oversold but unseeded
        assert ctx._pending == []

    def test_rejects_bad_period(self):
        with pytest.raises(ValueError):
            RsiMeanReversion(period=1)


# --- DonchianBreakout -------------------------------------------------------


class TestDonchianBreakout:
    def test_buys_on_breakout_of_prior_channel(self):
        s = DonchianBreakout(entry_n=3, exit_n=2)
        flat = close_series([100.0] * 3)
        ctx = feed(s, flat + close_series([101.0]))
        assert pending_sides(ctx) == ["BUY"]

    def test_no_buy_inside_channel(self):
        s = DonchianBreakout(entry_n=3, exit_n=2)
        ctx = feed(s, close_series([100.0, 101.0, 102.0, 102.0]))
        assert ctx._pending == []  # equals prior high, does not clear it

    def test_exits_on_low_break(self):
        s = DonchianBreakout(entry_n=3, exit_n=2)
        feed(s, close_series([100.0, 100.0, 100.0]))
        ctx = feed(s, close_series([99.0]), position_qty=1.0)
        assert pending_sides(ctx) == ["SELL"]

    def test_rejects_bad_lengths(self):
        with pytest.raises(ValueError):
            DonchianBreakout(entry_n=1)

    def test_spend_pct_sizes_off_live_equity_not_fixed_dollars(self):
        """Incident 2026-07-21: a fixed spend_usdt deadlocks against the
        per-coin risk cap once equity drops below the value it was sized
        against. spend_pct + equity_provider must track *current* equity."""
        equity = {"value": 147.03}
        s = DonchianBreakout(
            entry_n=3, exit_n=2, spend_pct=9.0, equity_provider=lambda: equity["value"],
        )
        flat = close_series([100.0] * 3)
        ctx = feed(s, flat + close_series([101.0]))
        assert pending_sides(ctx) == ["BUY"]
        amount = ctx._pending[0][1]
        assert amount == pytest.approx(147.03 * 0.09)
        assert amount < 147.03 * 0.10  # strictly under the 10% per-coin cap floor


# --- TrendFollow ------------------------------------------------------------


class TestTrendFollow:
    def test_no_signals_during_seeding(self):
        s = TrendFollow(fast=2, slow=3, trend=5)
        ctx = feed(s, close_series([100, 90, 110, 80, 120]))
        assert ctx._pending == []

    def test_buys_on_cross_up_in_uptrend_and_exits_on_cross_down(self):
        s = TrendFollow(fast=2, slow=4, trend=6, spend_usdt=15)
        # seed with a decline (fast below slow), then rally through everything
        seed = close_series([110, 108, 106, 104, 102, 100])
        rally = close_series([101, 104, 108, 113])
        ctx = feed(s, seed + rally)
        assert "BUY" in pending_sides(ctx)
        drop = close_series([108, 102, 96, 90])
        ctx2 = feed(s, drop, position_qty=1.0)
        assert "SELL" in pending_sides(ctx2)

    def test_cross_up_below_trend_filter_is_ignored(self):
        s = TrendFollow(fast=2, slow=4, trend=8)
        # long decline keeps price far below the trend EMA, then a weak bounce
        # crosses fast over slow while still under trend
        seed = close_series([200, 190, 180, 170, 160, 150, 140, 130, 120])
        bounce = close_series([118, 121, 124])
        ctx = feed(s, seed + bounce)
        assert pending_sides(ctx) == []

    def test_rejects_bad_periods(self):
        with pytest.raises(ValueError):
            TrendFollow(fast=10, slow=10, trend=20)


# --- D-23 wrappers ----------------------------------------------------------


class AlwaysBuy:
    warmup = 0

    def on_candle(self, c: Candle, ctx) -> None:
        if ctx.position.qty == 0:
            ctx.buy(15.0)


class AlwaysSell:
    warmup = 0

    def on_candle(self, c: Candle, ctx) -> None:
        if ctx.position.qty > 0:
            ctx.sell_all()


class TestEventBlackout:
    EVENT = T0 + 100 * MIN

    def test_blocks_entries_inside_window_only(self):
        s = EventBlackout(AlwaysBuy(), [self.EVENT], before_min=10, after_min=10)
        inside = candle(95, 100, 100, 100, 100)  # 5 min before event
        outside = candle(200, 100, 100, 100, 100)
        ctx = feed(s, [inside])
        assert ctx._pending == [] and s.blocked_candles == 1
        ctx2 = feed(s, [outside])
        assert pending_sides(ctx2) == ["BUY"]

    def test_window_edges_inclusive(self):
        s = EventBlackout(AlwaysBuy(), [self.EVENT], before_min=10, after_min=20)
        assert s.in_blackout(self.EVENT - 10 * MIN)
        assert s.in_blackout(self.EVENT + 20 * MIN)
        assert not s.in_blackout(self.EVENT - 11 * MIN)
        assert not s.in_blackout(self.EVENT + 21 * MIN)

    def test_exits_pass_during_blackout(self):
        s = EventBlackout(AlwaysSell(), [self.EVENT], before_min=10, after_min=10)
        ctx = feed(s, [candle(100, 100, 100, 100, 100)], position_qty=1.0)
        assert pending_sides(ctx) == ["SELL"]

    def test_multiple_events_bisect_paths(self):
        events = [T0 + 100 * MIN, T0 + 500 * MIN]
        s = EventBlackout(AlwaysBuy(), events, before_min=5, after_min=5)
        assert s.in_blackout(T0 + 497 * MIN)
        assert not s.in_blackout(T0 + 300 * MIN)
        assert s.in_blackout(T0 + 103 * MIN)


class TestNewsSpikePause:
    def test_spike_pauses_entries_then_resumes(self):
        t_spike = T0 + 10 * MIN
        counts = {T0 + i * MIN: 1 for i in range(10)}
        counts[t_spike] = 50
        s = NewsSpikePause(
            AlwaysBuy(), counts, baseline_candles=10, spike_mult=3.0, pause_min=5
        )
        calm = [candle(i, 100, 100, 100, 100) for i in range(10)]
        ctx = feed(s, calm)
        assert pending_sides(ctx) == ["BUY"] * 10  # calm period unaffected
        spike = candle(10, 100, 100, 100, 100)
        during = [candle(i, 100, 100, 100, 100) for i in (11, 12, 13, 14)]
        after = candle(15, 100, 100, 100, 100)
        ctx2 = feed(s, [spike] + during)
        assert ctx2._pending == [] and s.blocked_candles == 5
        ctx3 = feed(s, [after])
        assert pending_sides(ctx3) == ["BUY"]

    def test_small_absolute_counts_are_noise(self):
        counts = {T0: 0, T0 + MIN: 2}  # 0 -> 2: huge ratio, tiny count
        s = NewsSpikePause(AlwaysBuy(), counts, min_spike_count=5)
        ctx = feed(s, [candle(0, 1, 1, 1, 1), candle(1, 1, 1, 1, 1)])
        assert pending_sides(ctx) == ["BUY", "BUY"]

    def test_exits_pass_while_paused(self):
        counts = {T0: 100}
        s = NewsSpikePause(AlwaysSell(), counts, pause_min=60)
        ctx = feed(s, [candle(0, 1, 1, 1, 1), candle(1, 1, 1, 1, 1)], position_qty=1.0)
        assert pending_sides(ctx) == ["SELL", "SELL"]


# --- end-to-end through the Backtester --------------------------------------


class TestCandidatesThroughBacktester:
    def test_donchian_full_round_trip_deterministic(self):
        # flat, breakout rally, then collapse below the exit channel
        prices = [100.0] * 25 + [100 + i for i in range(1, 11)] + [104, 98, 94, 90, 88]
        candles = close_series([float(p) for p in prices])
        params = BacktestParams()

        def run():
            s = DonchianBreakout(entry_n=20, exit_n=5, spend_usdt=15, stop_loss_pct=8.0)
            return Backtester(params).run(s, candles, symbol="TESTUSDT", interval="1m")

        r1, r2 = run(), run()
        assert r1.round_trips >= 1
        assert r1.fingerprint() == r2.fingerprint()

    def test_blackout_wrapper_reduces_or_equals_trades(self):
        prices = [100.0] * 25 + [100 + i for i in range(1, 11)] + [104, 98, 94, 90, 88]
        candles = close_series([float(p) for p in prices])
        params = BacktestParams()

        def run(events):
            inner = DonchianBreakout(entry_n=20, exit_n=5, spend_usdt=15, stop_loss_pct=8.0)
            s = EventBlackout(inner, events, before_min=60, after_min=60) if events else inner
            return Backtester(params).run(s, candles, symbol="TESTUSDT", interval="1m")

        baseline = run([])
        blacked = run([T0 + 30 * MIN])  # window covers the whole breakout
        assert blacked.round_trips <= baseline.round_trips
        assert blacked.round_trips == 0
