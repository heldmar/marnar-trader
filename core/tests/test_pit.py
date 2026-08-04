"""Point-in-time harness tests (D-42b).

The property that matters here is *absence of lookahead*: a screen taken at
time T must be a function only of bars closed before T. Everything else in this
module is a convenience; that one property is the reason the module exists, so
it is tested from several directions — including a shuffled-future test that
fails loudly if any future bar can reach the decision.
"""

from __future__ import annotations

import random

import pytest

from trader.config import ScreenerConfig
from trader.marketdata import Candle
from trader.pit import (
    DAY_MS,
    base_asset,
    run_point_in_time,
    screen_at,
    universe_symbols,
)

CFG = ScreenerConfig(
    min_24h_quote_volume=1_000_000.0,
    min_24h_range_pct=1.0,
    max_pairs=3,
    max_market_cap_rank=100,
)


def make_candles(
    n: int,
    *,
    start_ms: int = 0,
    price: float = 100.0,
    drift: float = 0.0,
    range_pct: float = 5.0,
    quote_volume: float = 5_000_000.0,
) -> list[Candle]:
    out = []
    for i in range(n):
        close = price * (1 + drift) ** i
        half = close * range_pct / 200.0
        out.append(
            Candle(
                open_time=start_ms + i * DAY_MS,
                open=close,
                high=close + half,
                low=close - half,
                close=close,
                volume=quote_volume / close,
                quote_volume=quote_volume,
                trades=100,
            )
        )
    return out


def make_trending_series(
    cycles: int = 6,
    *,
    up_bars: int = 40,
    down_bars: int = 25,
    quote_volume: float = 9_000_000.0,
) -> list[Candle]:
    """A market that actually trades: rallies steep enough to clear the Donchian
    channel, then sells off hard enough to hit a stop. ``make_candles`` alone
    cannot produce a breakout — its daily range always straddles the close, so
    the prior high is never cleared.
    """
    out: list[Candle] = []
    price = 100.0
    t = 0
    for _ in range(cycles):
        for _ in range(up_bars):
            price *= 1.02
            out.append(_bar(t, price, quote_volume))
            t += 1
        for _ in range(down_bars):
            price *= 0.97
            out.append(_bar(t, price, quote_volume))
            t += 1
    return out


def _bar(i: int, close: float, quote_volume: float) -> Candle:
    # 1% half-range: wide enough to clear the peg filter, tight enough that a
    # 2%/day advance breaks the previous high.
    half = close * 0.01
    return Candle(
        open_time=i * DAY_MS,
        open=close,
        high=close + half,
        low=close - half,
        close=close,
        volume=quote_volume / close,
        quote_volume=quote_volume,
        trades=100,
    )


# -- screen_at: exclusions mirror production -------------------------------------


def test_base_asset_strips_quote():
    assert base_asset("BTCUSDT") == "BTC"
    assert base_asset("币安人生USDT") == "币安人生"


def test_stablecoins_and_leveraged_tokens_are_excluded():
    series = {
        "BTCUSDT": make_candles(60),
        "USDCUSDT": make_candles(60),  # "USD" in base
        "EURUSDT": make_candles(60),  # in exclude_bases — the bug the scratchpad had
        "BTCUPUSDT": make_candles(60),  # leveraged
    }
    result = screen_at(series, 60 * DAY_MS, CFG)
    assert result.symbols == ["BTCUSDT"]
    assert "stablecoin or fiat base asset" in result.excluded


def test_illiquid_pairs_are_excluded():
    series = {
        "BTCUSDT": make_candles(60, quote_volume=5_000_000.0),
        "TINYUSDT": make_candles(60, quote_volume=1_000.0),
    }
    assert screen_at(series, 60 * DAY_MS, CFG).symbols == ["BTCUSDT"]


def test_dollar_peg_is_excluded_on_behaviour_not_name():
    """D-40(b): UUSDT reached the live universe because the filter matched the
    *name*. A peg not called USD* must still be caught by its flatness."""
    series = {
        "BTCUSDT": make_candles(60, range_pct=5.0),
        "PEGUSDT": make_candles(60, range_pct=0.05),
    }
    result = screen_at(series, 60 * DAY_MS, CFG)
    assert result.symbols == ["BTCUSDT"]
    assert any("de-facto peg" in r for r in result.excluded)


def test_short_history_is_excluded():
    """A pair listed last week has no measurable liquidity profile — screening
    it in would be the survivorship bug in miniature."""
    series = {"BTCUSDT": make_candles(60), "NEWUSDT": make_candles(10, start_ms=50 * DAY_MS)}
    result = screen_at(series, 60 * DAY_MS, CFG)
    assert result.symbols == ["BTCUSDT"]


def test_max_pairs_caps_by_volume_descending():
    series = {
        f"C{i}USDT": make_candles(60, quote_volume=1_000_000.0 * (i + 2)) for i in range(6)
    }
    result = screen_at(series, 60 * DAY_MS, CFG)
    assert len(result.symbols) == CFG.max_pairs
    assert result.symbols == ["C5USDT", "C4USDT", "C3USDT"]


def test_ties_break_on_symbol_so_screens_are_reproducible():
    series = {f"{c}USDT": make_candles(60, quote_volume=5_000_000.0) for c in "ZYXWV"}
    first = screen_at(series, 60 * DAY_MS, CFG).symbols
    shuffled = dict(random.sample(list(series.items()), len(series)))
    assert screen_at(shuffled, 60 * DAY_MS, CFG).symbols == first


# -- the lookahead property ------------------------------------------------------


def test_screen_ignores_the_bar_still_in_progress():
    """A bar opening at as_of has not happened; a bar opening before but closing
    after as_of is still forming. Neither may influence the decision."""
    series = {"BTCUSDT": make_candles(31)}
    # 30 closed bars at as_of = 30 days: bars 0..29 closed, bar 30 opens exactly now.
    assert screen_at(series, 30 * DAY_MS, CFG).picks[0].bars_available == 30
    # One millisecond before bar 29 closes, only 29 bars are available -> too short.
    assert screen_at(series, 30 * DAY_MS - 1, CFG).symbols == []


def test_future_bars_cannot_change_the_screen():
    """The strongest form: append arbitrary future history to every pair and the
    screen at T must not move. If any lookahead exists, wild future volume on a
    pair that was illiquid at T will drag it into the universe."""
    as_of = 40 * DAY_MS
    base = {
        "AUSDT": make_candles(80, quote_volume=5_000_000.0),
        "BUSDTX": make_candles(80, quote_volume=4_000_000.0),
        "CUSDT": make_candles(80, quote_volume=2_000_000.0),
        "DUSDT": make_candles(80, quote_volume=1_500_000.0),
    }
    before = screen_at(base, as_of, CFG)

    poisoned = {s: list(cs) for s, cs in base.items()}
    for symbol, candles in poisoned.items():
        for c in candles:
            if c.open_time >= as_of:
                # Rewrite the future: the weakest pair becomes the most liquid.
                boost = 100_000_000.0 if symbol == "DUSDT" else 1.0
                candles[candles.index(c)] = Candle(
                    open_time=c.open_time,
                    open=c.open,
                    high=c.high,
                    low=c.low,
                    close=c.close,
                    volume=c.volume,
                    quote_volume=boost,
                    trades=c.trades,
                )
    after = screen_at(poisoned, as_of, CFG)
    assert after.symbols == before.symbols


def test_a_symbol_can_screen_in_only_after_it_becomes_liquid():
    """ZEC screened in only after its run — that is the finding the harness has
    to be able to reproduce, so assert the mechanism directly."""
    quiet = make_candles(40, quote_volume=10_000.0)
    loud = make_candles(40, start_ms=40 * DAY_MS, quote_volume=9_000_000.0)
    series = {"LATEUSDT": quiet + loud, "BTCUSDT": make_candles(80, quote_volume=5_000_000.0)}
    assert "LATEUSDT" not in screen_at(series, 40 * DAY_MS, CFG).symbols
    assert "LATEUSDT" in screen_at(series, 80 * DAY_MS, CFG).symbols


# -- market-cap criterion --------------------------------------------------------


def test_rank_criterion_skipped_when_no_ranks_supplied():
    series = {"BTCUSDT": make_candles(60)}
    result = screen_at(series, 60 * DAY_MS, CFG)
    assert result.rank_criterion_applied is False
    assert result.symbols == ["BTCUSDT"]


def test_rank_criterion_excludes_when_ranks_supplied():
    series = {"BTCUSDT": make_candles(60), "SMALLUSDT": make_candles(60)}
    result = screen_at(
        series, 60 * DAY_MS, CFG, market_cap_ranks={"BTC": 1, "SMALL": 900}
    )
    assert result.rank_criterion_applied is True
    assert result.symbols == ["BTCUSDT"]


def test_unknown_rank_is_kept_like_production():
    series = {"BTCUSDT": make_candles(60)}
    result = screen_at(series, 60 * DAY_MS, CFG, market_cap_ranks={"ETH": 2})
    assert result.symbols == ["BTCUSDT"]
    assert result.picks[0].market_cap_rank is None


# -- run_point_in_time -----------------------------------------------------------


def test_run_rescreens_each_period_and_chains_equity():
    """Universe must change between periods when liquidity changes, and each
    period must start from the previous one's ending equity."""
    series = {
        "AUSDT": make_candles(400, quote_volume=9_000_000.0, drift=0.002),
        "BUSDT": make_candles(400, quote_volume=8_000_000.0, drift=-0.001),
        # Illiquid for the first half, liquid afterwards.
        "LATEUSDT": (
            make_candles(200, quote_volume=1_000.0, drift=0.001)
            + make_candles(200, start_ms=200 * DAY_MS, quote_volume=9_500_000.0, drift=0.003)
        ),
    }
    result = run_point_in_time(
        series,
        start_ms=100 * DAY_MS,
        periods=3,
        config=CFG,
        period_days=91,
        initial_cash=150.0,
    )
    assert len(result.periods) == 3
    assert "LATEUSDT" not in result.periods[0].symbols
    assert "LATEUSDT" in result.periods[-1].symbols
    for prev, cur in zip(result.periods, result.periods[1:], strict=False):
        assert cur.start_equity == pytest.approx(prev.end_equity)
    assert result.periods[0].start_equity == 150.0


def test_missing_rank_source_is_recorded_as_a_note():
    """D-43(b): the divergence from production must never be silent."""
    series = {"AUSDT": make_candles(300, quote_volume=9_000_000.0, drift=0.002)}
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=2, config=CFG, initial_cash=150.0
    )
    assert any("market-cap criterion NOT applied" in n for n in result.notes)


def test_turnover_counts_arrivals_between_screens():
    series = {
        "AUSDT": make_candles(400, quote_volume=9_000_000.0),
        "LATEUSDT": (
            make_candles(200, quote_volume=1_000.0)
            + make_candles(200, start_ms=200 * DAY_MS, quote_volume=9_500_000.0)
        ),
    }
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0
    )
    assert sum(result.turnover) >= 1


def test_no_trades_leaves_equity_untouched():
    """A market that is liquid and volatile enough to be screened in, but never
    breaks its channel, must return exactly the starting cash — a harness that
    leaks fees on zero trades is miscounting."""
    series = {"AUSDT": make_candles(300, drift=0.0, range_pct=2.0, quote_volume=9_000_000.0)}
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=2, config=CFG, initial_cash=150.0
    )
    assert result.periods, "flat-but-liquid pair should still be screened in"
    assert result.final_equity == pytest.approx(150.0)
    assert result.total_return_pct == pytest.approx(0.0)


def test_deterministic_across_runs():
    """Same inputs, same numbers — arch §6. A stochastic harness cannot settle
    a parameter question."""
    series = {
        "AUSDT": make_candles(400, quote_volume=9_000_000.0, drift=0.002),
        "BUSDT": make_candles(400, quote_volume=8_000_000.0, drift=-0.001),
    }
    kwargs = dict(start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0)
    first = run_point_in_time(series, **kwargs)
    second = run_point_in_time(series, **kwargs)
    assert [p.end_equity for p in first.periods] == [p.end_equity for p in second.periods]


def test_the_harness_actually_trades():
    """If the replay never fires, every comparison built on it is vacuous."""
    series = {"AUSDT": make_trending_series()}
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0
    )
    assert sum(p.buys for p in result.periods) > 0
    assert sum(p.fees for p in result.periods) > 0


def test_stop_width_is_actually_applied():
    """Guard against the parameter being silently ignored — the harness exists to
    compare stop widths, so a dropped stop would invalidate its whole output."""
    series = {"AUSDT": make_trending_series()}
    tight = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0, stop_loss_pct=1.0
    )
    wide = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0, stop_loss_pct=25.0
    )
    assert tight.final_equity != wide.final_equity


# -- universe_symbols ------------------------------------------------------------


def test_universe_symbols_filters_quote_status_and_asset_class():
    info = {
        "symbols": [
            {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "DEADUSDT", "baseAsset": "DEAD", "quoteAsset": "USDT",
             "status": "BREAK", "isSpotTradingAllowed": True},
            {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "MARGINUSDT", "baseAsset": "MARGIN", "quoteAsset": "USDT",
             "status": "TRADING", "isSpotTradingAllowed": False},
        ]
    }
    assert universe_symbols(info, CFG) == ["BTCUSDT"]
