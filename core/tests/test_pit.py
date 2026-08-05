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
    PeriodResult,
    PitResult,
    base_asset,
    buy_and_hold_return_pct,
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
    assert any("applied to 0 of 2 periods" in n for n in result.notes)
    assert not any(p.rank_criterion_applied for p in result.periods)


def test_partial_rank_coverage_is_reported_honestly():
    """The expected state on the free tier: ranks reach back about a year, so
    later periods get the criterion and earlier ones do not. Claiming full
    coverage would be the exact dishonesty this harness exists to remove."""
    series = {
        "AUSDT": make_candles(400, quote_volume=9_000_000.0, drift=0.002),
        "BUSDT": make_candles(400, quote_volume=8_000_000.0, drift=0.001),
    }
    start = 100 * DAY_MS
    # Ranks known only for the final period; B is ranked out of the top 100.
    last_period_start = start + 2 * 91 * DAY_MS
    result = run_point_in_time(
        series,
        start_ms=start,
        periods=3,
        config=CFG,
        initial_cash=150.0,
        market_cap_ranks_at={last_period_start: {"A": 1, "B": 900}},
    )
    assert [p.rank_criterion_applied for p in result.periods] == [False, False, True]
    assert "BUSDT" in result.periods[0].symbols
    assert "BUSDT" not in result.periods[2].symbols
    assert any("applied to 1 of 3 periods" in n for n in result.notes)


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


_FILTERS = [
    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "NOTIONAL", "minNotional": "5"},
]

UNIVERSE_INFO = {
    "symbols": [
        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
         "status": "TRADING", "isSpotTradingAllowed": True, "filters": _FILTERS},
        {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC",
         "status": "TRADING", "isSpotTradingAllowed": True, "filters": _FILTERS},
        {"symbol": "DEADUSDT", "baseAsset": "DEAD", "quoteAsset": "USDT",
         "status": "BREAK", "isSpotTradingAllowed": True, "filters": _FILTERS},
        {"symbol": "USDCUSDT", "baseAsset": "USDC", "quoteAsset": "USDT",
         "status": "TRADING", "isSpotTradingAllowed": True, "filters": _FILTERS},
        {"symbol": "MARGINUSDT", "baseAsset": "MARGIN", "quoteAsset": "USDT",
         "status": "TRADING", "isSpotTradingAllowed": False, "filters": _FILTERS},
    ]
}


def test_universe_symbols_filters_quote_and_asset_class():
    """Quote asset, spot permission and asset-class exclusions still bind. The
    only thing that changed is that a dead pair is no longer thrown away."""
    got = universe_symbols(UNIVERSE_INFO, CFG)
    assert "ETHBTC" not in got, "wrong quote asset"
    assert "USDCUSDT" not in got, "stablecoin"
    assert "MARGINUSDT" not in got, "spot trading not allowed"


def test_delisted_pairs_are_included_by_default():
    """The survivorship fix. A pair Binance has stopped trading keeps status
    BREAK in exchangeInfo and its klines stay served, so it belongs in the
    backtest universe — dead pairs are disproportionately losers and dropping
    them biased every return figure upward."""
    assert universe_symbols(UNIVERSE_INFO, CFG) == ["BTCUSDT", "DEADUSDT"]


def test_survivorship_bias_can_still_be_measured_by_excluding_them():
    """The biased universe stays reachable so the size of the bias can be
    differenced, but it must never be what a caller gets by default."""
    assert universe_symbols(UNIVERSE_INFO, CFG, include_delisted=False) == ["BTCUSDT"]


def test_live_screener_still_refuses_delisted_pairs():
    """The widening is backtest-only. If this ever fails, live trading can pick
    a pair that no longer trades — orders would be rejected or, worse, fill
    against a stale book. `screener.py` must keep its own TRADING-only check."""
    from trader.screener import Screener

    huge = 10_000_000_000.0
    tickers = [
        {"symbol": s["symbol"], "quoteVolume": str(huge), "highPrice": "2",
         "lowPrice": "1", "lastPrice": "1.5"}
        for s in UNIVERSE_INFO["symbols"]
    ]
    report = Screener(
        CFG,
        exchange_info=UNIVERSE_INFO,
        tickers_24h=tickers,
        market_cap_ranks=None,
    ).run()

    selected = {v.symbol for v in report.qualified}
    assert "DEADUSDT" not in selected, "a delisted pair reached the LIVE universe"
    assert "BTCUSDT" in selected, "the live screen should still pick live pairs"


def test_sizing_tracks_equity_so_the_cap_cannot_deadlock():
    """Regression for the harness's own version of the 2026-07-21 incident: a
    fixed $15 spend against a 10%-of-equity cap blocks every entry after the
    first fee, so the replay silently stops trading and every comparison built
    on it becomes vacuous. Percentage sizing must keep trading."""
    series = {
        f"{c}USDT": make_trending_series(quote_volume=9_000_000.0 - i * 1000)
        for i, c in enumerate("ABCDE")
    }
    live_sizing = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0
    )
    fixed_dollar = run_point_in_time(
        series,
        start_ms=100 * DAY_MS,
        periods=3,
        config=CFG,
        initial_cash=150.0,
        spend_pct=None,
        spend_usdt=15.0,
    )
    assert sum(p.buys for p in live_sizing.periods) > sum(p.buys for p in fixed_dollar.periods)
    # Trading must not stop after the opening period.
    assert all(p.buys > 0 for p in live_sizing.periods)


# -- D-46(a) benchmarks ----------------------------------------------------------


def _closes(closes: list[float], *, start_ms: int = 0) -> list[Candle]:
    """Bars with exactly the given closes, priced so the screen accepts them."""
    return [_bar(start_ms // DAY_MS + i, c, 9_000_000.0) for i, c in enumerate(closes)]


def test_buy_and_hold_is_equal_weight_and_scaled_to_exposure():
    """The hurdle is held at the same exposure our caps allow, not 100% invested
    — otherwise the strategy would be penalised for a risk limit the investor
    chose on purpose."""
    series = {
        "AUSDT": _closes([100.0, 150.0, 200.0]),  # +100%
        "BUSDT": _closes([100.0, 90.0, 50.0]),  # -50%
    }
    # Equal weight over the two legs: (+100% + -50%) / 2 = +25%, at 45% exposure.
    got = buy_and_hold_return_pct(
        series, ["AUSDT", "BUSDT"], 0, 3 * DAY_MS, exposure_pct=45.0
    )
    assert got == pytest.approx(25.0 * 0.45)


def test_buy_and_hold_prices_only_bars_inside_the_window():
    """A benchmark that reads bars outside its period is lookahead by another
    name — the exact defect this module exists to prevent."""
    series = {"AUSDT": _closes([100.0, 200.0, 400.0, 800.0])}
    inside = buy_and_hold_return_pct(series, ["AUSDT"], DAY_MS, 3 * DAY_MS, exposure_pct=100.0)
    # Bars 1 and 2 only: 200 -> 400.
    assert inside == pytest.approx(100.0)


def test_unpriceable_benchmark_is_none_not_zero():
    """Silently reporting 0% for a benchmark with no data would understate the
    hurdle and hand the strategy a win it did not earn."""
    series = {"AUSDT": _closes([100.0, 110.0])}
    assert buy_and_hold_return_pct(series, ["ZZZUSDT"], 0, 2 * DAY_MS, exposure_pct=45.0) is None


def test_run_records_both_benchmarks_for_every_period():
    series = {
        f"{c}USDT": make_trending_series(quote_volume=9_000_000.0 - i * 1000)
        for i, c in enumerate("ABCDE")
    }
    series["BTCUSDT"] = make_trending_series(quote_volume=9_500_000.0)
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=3, config=CFG, initial_cash=150.0
    )
    assert result.benchmark_exposure_pct == pytest.approx(45.0)  # 5 slots x 9%
    for p in result.periods:
        assert p.benchmark_universe_pct is not None
        assert p.benchmark_btc_pct is not None
    assert result.benchmark_universe_total_pct is not None
    assert result.benchmark_btc_total_pct is not None
    assert isinstance(result.beats_benchmarks, bool)


def test_missing_btc_history_leaves_the_verdict_undecided():
    """No BTC bars means no BTC hurdle, and a hurdle we cannot price must read
    as 'unknown', never as 'cleared'."""
    series = {
        f"{c}USDT": make_trending_series(quote_volume=9_000_000.0 - i * 1000)
        for i, c in enumerate("ABCDE")
    }
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=2, config=CFG, initial_cash=150.0
    )
    assert all(p.benchmark_btc_pct is None for p in result.periods)
    assert result.benchmark_btc_total_pct is None
    assert result.beats_benchmarks is None


def _period(ret_pct: float, uni: float | None, btc: float | None) -> PeriodResult:
    return PeriodResult(
        index=0,
        start_ms=0,
        end_ms=DAY_MS,
        symbols=["AUSDT"],
        start_equity=100.0,
        end_equity=100.0 * (1 + ret_pct / 100.0),
        buys=1,
        sells=1,
        fees=0.0,
        benchmark_universe_pct=uni,
        benchmark_btc_pct=btc,
    )


@pytest.mark.parametrize(
    ("uni", "btc", "expected"),
    [
        (5.0, 5.0, True),  # beats both
        (5.0, 20.0, False),  # loses to BTC
        (20.0, 5.0, False),  # loses to its own universe
        (20.0, 20.0, False),  # loses to both
        (5.0, None, None),  # one hurdle unpriceable
    ],
)
def test_both_hurdles_must_be_cleared(uni, btc, expected):
    """D-46(a) is an AND, not an OR. Beating BTC while losing to a coin-flip
    hold of the very coins the screen picked is not an edge."""
    period = _period(10.0, uni, btc)
    assert period.beats_benchmarks is expected

    result = PitResult(initial_cash=100.0, chained=True)
    result.periods.append(period)
    assert result.beats_benchmarks is expected


def test_benchmarks_compound_across_periods():
    """Chaining, not averaging: a hurdle quoted as a mean would be a different
    number from the equity curve it is being compared against."""
    result = PitResult(initial_cash=100.0, chained=True)
    result.periods.append(_period(0.0, 10.0, 10.0))
    result.periods.append(_period(0.0, 10.0, -10.0))
    assert result.benchmark_universe_total_pct == pytest.approx(21.0)  # 1.1 * 1.1
    assert result.benchmark_btc_total_pct == pytest.approx(-1.0)  # 1.1 * 0.9


def test_summary_prints_the_verdict():
    series = {
        f"{c}USDT": make_trending_series(quote_volume=9_000_000.0 - i * 1000)
        for i, c in enumerate("ABCDE")
    }
    series["BTCUSDT"] = make_trending_series(quote_volume=9_500_000.0)
    result = run_point_in_time(
        series, start_ms=100 * DAY_MS, periods=2, config=CFG, initial_cash=150.0
    )
    text = result.summary()
    assert "BENCHMARKS (D-46a" in text
    assert "VERDICT:" in text
