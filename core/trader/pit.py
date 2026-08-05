"""Point-in-time backtesting (D-42b) — the honest evidence harness.

Every return figure this project published before 2026-07-30 was measured on
the 20 pairs the screener happened to pick on 2026-07-14, replayed over the two
years *preceding* that pick. That is hindsight: the universe was chosen knowing
which assets had already run. Removing the three biggest movers took the
four-quarter result from +24.9% to −3.1%, and a point-in-time re-screen inverted
the stop-width ranking that D-40 had been approved on — reversed the next day as
D-41.

This module makes the honest measurement the default one. For each period it
re-screens the universe using **only bars that existed at the start of that
period**, then replays the strategy on whatever that screen selected. The
universe becomes an output of the method rather than an input chosen by the
analyst.

**Delisted pairs are now included (2026-08-05).** They used to be the largest
remaining defect: the universe came from today's ``exchangeInfo`` filtered to
``TRADING``, so every pair that died since 2024 was missing — and dead pairs are
disproportionately losers, so every return figure was biased upward. The fix
turned out to need no archival data source at all: Binance keeps delisted pairs
in ``exchangeInfo`` under status ``BREAK`` and still serves their klines. See
``universe_symbols`` for the measured size of what was being dropped.

What it does NOT fix, and callers must keep stating (D-43b):

- **Pairs delisted _before_ the window are still unreachable.** The klines
  endpoint returns nothing for a pair that stopped trading before the requested
  start, so 80 of the 193 delisted pairs contribute no bars. They cannot bias a
  window they never overlap, but a longer backtest would re-expose the gap.
- **The market-cap criterion is only applied if a rank provider is supplied.**
  Production screens on top-N market cap (``ScreenerConfig.max_market_cap_rank``);
  no free source serves *historical* ranks, and the investor declined a paid
  subscription. Without a provider the screen runs volume-and-range-only, which
  is a real divergence from production — ``PitResult.notes`` records it and
  every report must repeat it rather than quietly dropping the caveat.

The replay is ``parity.PortfolioReplay`` — the same shared-cash, risk-capped
engine the D-27 parity gate measures against, so a number produced here and a
number produced there mean the same thing.

CLI: ``python -m trader.pit --data-dir DIR [--quarters 8] [--stop 3.0] ...``
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from trader.config import ScreenerConfig
from trader.marketdata import INTERVAL_MS, Candle
from trader.parity import PortfolioReplay
from trader.screener import excluded_base_reason

DAY_MS = 86_400_000
# One quarter. 91 days keeps every period the same length, which a calendar
# quarter does not — periods of unequal length are not comparable samples.
QUARTER_DAYS = 91
# Trailing window for the liquidity/behaviour statistics. The live screener
# reads a single 24h ticker; daily bars give us no such instant, so we take the
# median over 30 days. Median, not mean: one listing-day volume spike should not
# buy a pair a place in the universe.
SCREEN_LOOKBACK_DAYS = 30


def base_asset(symbol: str, quote: str = "USDT") -> str:
    """``BTCUSDT`` -> ``BTC``. Binance spot symbols are base+quote concatenated."""
    return symbol[: -len(quote)] if symbol.endswith(quote) else symbol


@dataclass(frozen=True, slots=True)
class ScreenPick:
    symbol: str
    base: str
    median_quote_volume: float
    median_daily_range_pct: float
    market_cap_rank: int | None
    bars_available: int


@dataclass(slots=True)
class ScreenAtResult:
    as_of_ms: int
    picks: list[ScreenPick] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    rank_criterion_applied: bool = False

    @property
    def symbols(self) -> list[str]:
        return [p.symbol for p in self.picks]


def screen_at(
    series: dict[str, list[Candle]],
    as_of_ms: int,
    config: ScreenerConfig,
    *,
    interval: str = "1d",
    lookback_days: int = SCREEN_LOOKBACK_DAYS,
    market_cap_ranks: dict[str, int] | None = None,
) -> ScreenAtResult:
    """Reconstruct what the screener would have selected at *as_of_ms*.

    Only candles that had already **closed** at *as_of_ms* are considered — a
    bar opening at ``as_of_ms`` has not happened yet, and a bar opening earlier
    but closing later is still in progress. Getting this boundary wrong is the
    single easiest way to reintroduce lookahead, so it is asserted in tests.

    Mirrors ``screener.Screener`` criterion for criterion, substituting trailing
    daily statistics for the live 24h ticker:

    - asset-class exclusions via the shared ``excluded_base_reason``
    - median trailing quote volume >= ``min_24h_quote_volume`` (liquidity)
    - median trailing daily high/low range >= ``min_24h_range_pct`` (D-40b peg
      check — a dollar peg cannot break out of anything)
    - market-cap rank <= ``max_market_cap_rank``, *only* when ranks are supplied
    - top ``max_pairs`` by median volume

    The ``minNotional`` criterion is deliberately absent: it depends on today's
    exchange filters, which we cannot reconstruct historically, and at a $150
    account it excluded nothing in practice.
    """
    step = INTERVAL_MS[interval]
    result = ScreenAtResult(as_of_ms=as_of_ms, rank_criterion_applied=market_cap_ranks is not None)

    def exclude(reason: str) -> None:
        result.excluded[reason] = result.excluded.get(reason, 0) + 1

    candidates: list[ScreenPick] = []
    for symbol, candles in series.items():
        base = base_asset(symbol, config.quote_asset)
        reason = excluded_base_reason(base, config)
        if reason is not None:
            exclude(reason)
            continue

        # Closed strictly before the decision instant.
        history = [c for c in candles if c.open_time + step <= as_of_ms]
        window = history[-lookback_days:]
        if len(window) < lookback_days:
            exclude(f"fewer than {lookback_days} daily bars of history at this date")
            continue

        volume = statistics.median(c.quote_volume for c in window)
        if volume < config.min_24h_quote_volume:
            exclude(f"median 24h volume below {config.min_24h_quote_volume:,.0f} USDT")
            continue

        ranges = [(c.high / c.low - 1) * 100 for c in window if c.low > 0]
        range_pct = statistics.median(ranges) if ranges else 0.0
        if range_pct < config.min_24h_range_pct:
            exclude(
                f"median daily range below {config.min_24h_range_pct:g}% "
                "(de-facto peg — nothing to trend-trade)"
            )
            continue

        rank = market_cap_ranks.get(base) if market_cap_ranks is not None else None
        if market_cap_ranks is not None and rank is not None and rank > config.max_market_cap_rank:
            exclude(f"market-cap rank beyond top {config.max_market_cap_rank}")
            continue

        candidates.append(
            ScreenPick(
                symbol=symbol,
                base=base,
                median_quote_volume=volume,
                median_daily_range_pct=range_pct,
                market_cap_rank=rank,
                bars_available=len(history),
            )
        )

    # Ties broken on symbol so a screen is reproducible regardless of dict order.
    candidates.sort(key=lambda p: (-p.median_quote_volume, p.symbol))
    dropped = len(candidates) - config.max_pairs
    if dropped > 0:
        result.excluded[f"beyond top {config.max_pairs} by volume"] = dropped
    result.picks = candidates[: config.max_pairs]
    return result


@dataclass(slots=True)
class PeriodResult:
    index: int
    start_ms: int
    end_ms: int
    symbols: list[str]
    start_equity: float
    end_equity: float
    buys: int
    sells: int
    fees: float
    rank_criterion_applied: bool = False
    #: D-46(a) hurdles for this period, in percent, at the same exposure the
    #: risk caps allow. ``None`` when the benchmark could not be priced (no
    #: bars for BTC, or none of the screened symbols priced in the window).
    benchmark_universe_pct: float | None = None
    benchmark_btc_pct: float | None = None

    @property
    def return_pct(self) -> float:
        return (self.end_equity / self.start_equity - 1.0) * 100.0

    @property
    def beats_benchmarks(self) -> bool | None:
        """D-46(a): the strategy must beat BOTH hurdles, not either."""
        if self.benchmark_universe_pct is None or self.benchmark_btc_pct is None:
            return None
        return (
            self.return_pct > self.benchmark_universe_pct
            and self.return_pct > self.benchmark_btc_pct
        )


@dataclass(slots=True)
class PitResult:
    periods: list[PeriodResult] = field(default_factory=list)
    initial_cash: float = 0.0
    notes: list[str] = field(default_factory=list)
    chained: bool = True

    @property
    def final_equity(self) -> float:
        self._require_chained()
        return self.periods[-1].end_equity if self.periods else self.initial_cash

    @property
    def total_return_pct(self) -> float:
        self._require_chained()
        return (self.final_equity / self.initial_cash - 1.0) * 100.0 if self.initial_cash else 0.0

    def _require_chained(self) -> None:
        # An unchained run restarts each period at initial_cash, so compounding
        # these numbers would quote a return the account never earned. Refusing
        # is cheaper than a plausible wrong figure reaching a report.
        if not self.chained:
            raise ValueError(
                "this run did not chain equity across periods (chain_equity=False); "
                "use per-period returns, not a compounded total"
            )

    @property
    def mean_period_return_pct(self) -> float:
        return statistics.fmean(p.return_pct for p in self.periods) if self.periods else 0.0

    @property
    def stdev_period_return_pct(self) -> float:
        rs = [p.return_pct for p in self.periods]
        return statistics.stdev(rs) if len(rs) > 1 else 0.0

    #: Exposure the benchmarks are held at, so they are compared like-for-like
    #: with a book that can never be more than max_open_positions * spend_pct
    #: invested. Set by ``run_point_in_time``; the rest is cash earning nothing.
    benchmark_exposure_pct: float = 45.0

    def _compound(self, attr: str) -> float | None:
        """Chain per-period benchmark returns the way equity chains. Returns
        None if any period could not be priced — a benchmark with a hole in it
        is not a benchmark, and quoting it would understate the hurdle."""
        self._require_chained()
        factor = 1.0
        for p in self.periods:
            value = getattr(p, attr)
            if value is None:
                return None
            factor *= 1.0 + value / 100.0
        return (factor - 1.0) * 100.0

    @property
    def benchmark_universe_total_pct(self) -> float | None:
        """Equal-weight buy-and-hold of whatever the screen picked each period,
        rebalanced at each screen. The 'did the trading logic beat just holding
        what it chose' hurdle."""
        return self._compound("benchmark_universe_pct")

    @property
    def benchmark_btc_total_pct(self) -> float | None:
        """Buy-and-hold BTC at the same exposure — the 'did any of this beat the
        obvious passive alternative' hurdle."""
        return self._compound("benchmark_btc_pct")

    @property
    def beats_benchmarks(self) -> bool | None:
        """D-46(a): real capital requires clearing BOTH hurdles."""
        uni = self.benchmark_universe_total_pct
        btc = self.benchmark_btc_total_pct
        if uni is None or btc is None:
            return None
        return self.total_return_pct > uni and self.total_return_pct > btc

    @property
    def turnover(self) -> list[int]:
        """How many symbols changed between consecutive screens — the quantity
        that makes a frozen universe dishonest. Empty for a single period."""
        out = []
        for prev, cur in zip(self.periods, self.periods[1:], strict=False):
            out.append(len(set(cur.symbols) - set(prev.symbols)))
        return out

    def summary(self) -> str:
        head = (
            f"total {self.total_return_pct:+.2f}% | "
            if self.chained
            else "unchained (per-period only) | "
        )
        lines = [
            f"Point-in-time: {len(self.periods)} periods | {head}"
            f"mean/period {self.mean_period_return_pct:+.2f}% "
            f"(sd {self.stdev_period_return_pct:.2f})",
        ]
        for p in self.periods:
            bench = ""
            if p.benchmark_universe_pct is not None and p.benchmark_btc_pct is not None:
                mark = {True: "WIN ", False: "lose", None: "  ? "}[p.beats_benchmarks]
                bench = (
                    f" | univ {p.benchmark_universe_pct:+7.2f}% "
                    f"btc {p.benchmark_btc_pct:+7.2f}% {mark}"
                )
            lines.append(
                f"  P{p.index} {len(p.symbols):2d} symbols | {p.return_pct:+7.2f}% | "
                f"equity {p.start_equity:8.2f} -> {p.end_equity:8.2f} | "
                f"{p.buys}B/{p.sells}S | fees {p.fees:.2f}{bench}"
            )
        # D-46(a): the hurdle is reported on every run, so no future report can
        # quote a return without saying what the alternatives did.
        if self.chained:
            uni = self.benchmark_universe_total_pct
            btc = self.benchmark_btc_total_pct
            if uni is not None and btc is not None:
                lines.append(
                    f"  BENCHMARKS (D-46a, compounded, {self.benchmark_exposure_pct:.0f}% "
                    f"exposure): universe {uni:+.2f}% | BTC {btc:+.2f}% | "
                    f"strategy {self.total_return_pct:+.2f}%"
                )
                lines.append(
                    "  VERDICT: "
                    + (
                        "clears both hurdles"
                        if self.beats_benchmarks
                        else "FAILS the D-46(a) hurdle — does not beat both"
                    )
                )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _window_closes(
    bars: list[Candle], start_ms: int, end_ms: int
) -> tuple[float, float] | None:
    """First and last close inside ``[start_ms, end_ms)``, or None if the symbol
    has no bars there. A pair that stops trading mid-period marks out at its last
    available close, which flatters the benchmark — a delisted coin rarely
    liquidates at its last print. The hurdle is therefore, if anything, easier
    than reality, which is the safe direction for a hurdle to err."""
    first = last = None
    for c in bars:
        if c.open_time < start_ms:
            continue
        if c.open_time >= end_ms:
            break
        if first is None:
            first = c.close
        last = c.close
    if first is None or last is None:
        return None
    return first, last


def buy_and_hold_return_pct(
    series: dict[str, list[Candle]],
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    *,
    exposure_pct: float,
) -> float | None:
    """D-46(a) hurdle: equal-weight buy-and-hold of ``symbols`` over the period,
    with only ``exposure_pct`` of capital invested and the rest in idle cash.

    The exposure haircut is what makes the comparison fair: our book can never
    hold more than ``max_open_positions * spend_pct`` of equity in coins, so
    measuring it against a 100%-invested index would penalise it for a cap the
    investor chose deliberately. Returns None when nothing could be priced.
    """
    legs = []
    for s in symbols:
        closes = _window_closes(series.get(s, []), start_ms, end_ms)
        if closes is None:
            continue
        first, last = closes
        if first <= 0:
            continue
        legs.append(last / first - 1.0)
    if not legs:
        return None
    return (sum(legs) / len(legs)) * exposure_pct


def run_point_in_time(
    series: dict[str, list[Candle]],
    *,
    start_ms: int,
    periods: int,
    config: ScreenerConfig,
    initial_cash: float = 150.0,
    period_days: int = QUARTER_DAYS,
    interval: str = "1d",
    entry_n: int = 15,
    exit_n: int = 15,
    stop_loss_pct: float = 3.0,
    # Percentage-of-equity sizing, as production does it (PaperConfig.spend_pct).
    # A fixed dollar spend is not merely a different choice here, it is broken:
    # $15 against a 10%-of-equity cap blocks every entry after the first fee
    # drops equity below $150 — the same structural deadlock that froze live
    # trading in the 2026-07-21 incident. Measured on this universe it produced
    # 1 trade in 8 quarters, so a fixed-dollar harness would have "measured"
    # stop widths that no trade ever reached.
    spend_pct: float = 9.0,
    spend_usdt: float = 15.0,
    max_open_positions: int = 5,
    max_position_pct: float = 10.0,
    fee_rate: float = 0.001,
    slippage_bps: float = 5.0,
    event_blackout: bool = True,
    market_cap_ranks_at: dict[int, dict[str, int]] | None = None,
    chain_equity: bool = True,
) -> PitResult:
    """Re-screen and replay over consecutive equal-length periods.

    Equity chains: each period starts with what the previous one ended holding,
    marked to its last close. Positions are not force-flattened at a boundary —
    a symbol dropping out of the screen simply stops being traded, and its
    holding is marked out. That mirrors the engineering constraint the live
    screener faces (a departing symbol's position must be allowed to close),
    and avoids inventing a quarterly liquidation the strategy never performs.

    ``market_cap_ranks_at`` maps a period's ``as_of_ms`` to the ranks known at
    that date. Omit it and the rank criterion is skipped entirely, which
    ``PitResult.notes`` will say (D-43b).

    ``chain_equity=False`` restarts every period from ``initial_cash``. That is
    not how the account behaves and must never be used to quote a return; it
    exists so a parameter sweep can compare two settings period by period
    without the comparison being dominated by whichever one happened to enter
    the period richer.
    """
    step = INTERVAL_MS[interval]
    warmup_ms = max(entry_n, exit_n) * step * 2  # generous lead-in so channels are primed
    result = PitResult(initial_cash=initial_cash, chained=chain_equity)

    # The book can hold at most this fraction in coins; the benchmarks are held
    # at the same fraction so the hurdle measures skill, not leverage (D-46a).
    # ``spend_pct=None`` means the fixed-dollar sizing path, where the invested
    # fraction is set by the dollar spend against starting equity instead.
    per_slot_pct = (
        spend_pct
        if spend_pct is not None
        else (spend_usdt / initial_cash * 100.0 if initial_cash > 0 else 0.0)
    )
    exposure_pct = min(100.0, max_open_positions * min(per_slot_pct, max_position_pct))
    result.benchmark_exposure_pct = exposure_pct
    btc_symbol = f"BTC{config.quote_asset}"

    equity = initial_cash
    for i in range(periods):
        period_start = start_ms + i * period_days * DAY_MS
        period_end = period_start + period_days * DAY_MS

        ranks = (market_cap_ranks_at or {}).get(period_start)
        screen = screen_at(
            series, period_start, config, interval=interval, market_cap_ranks=ranks
        )
        if not screen.picks:
            result.notes.append(f"period {i}: screen selected nothing — period skipped")
            continue

        window = {
            s: [
                c
                for c in series[s]
                if period_start - warmup_ms <= c.open_time < period_end
            ]
            for s in screen.symbols
        }
        window = {s: cs for s, cs in window.items() if cs}

        replay = PortfolioReplay(
            initial_cash=equity,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            max_open_positions=max_open_positions,
            max_position_pct=max_position_pct,
            spend_usdt=spend_usdt,
            spend_pct=spend_pct,
            entry_n=entry_n,
            exit_n=exit_n,
            stop_loss_pct=stop_loss_pct,
            event_blackout=event_blackout,
        )
        portfolio = replay.run(window, trade_from_ms=period_start, trade_to_ms=period_end)

        result.periods.append(
            PeriodResult(
                index=i,
                start_ms=period_start,
                end_ms=period_end,
                symbols=screen.symbols,
                start_equity=equity,
                end_equity=portfolio.final_equity,
                buys=portfolio.buys,
                sells=portfolio.sells,
                fees=portfolio.fees,
                rank_criterion_applied=ranks is not None,
                benchmark_universe_pct=buy_and_hold_return_pct(
                    series,
                    screen.symbols,
                    period_start,
                    period_end,
                    exposure_pct=exposure_pct,
                ),
                benchmark_btc_pct=buy_and_hold_return_pct(
                    series,
                    [btc_symbol],
                    period_start,
                    period_end,
                    exposure_pct=exposure_pct,
                ),
            )
        )
        equity = portfolio.final_equity if chain_equity else initial_cash

    # D-43(b): the divergence from production is never allowed to go silent.
    applied = sum(1 for p in result.periods if p.rank_criterion_applied)
    if applied < len(result.periods):
        result.notes.append(
            f"market-cap criterion applied to {applied} of {len(result.periods)} periods. "
            f"Production screens on top-{config.max_market_cap_rank} market cap; where "
            "no historical rank was available the screen ran on volume and range alone, "
            "so that part of the universe is wider than production's and the divergence "
            "is unquantified (free rank sources reach back ~1 year)."
        )

    return result


# -- universe fetch + CLI ---------------------------------------------------------


#: Pair statuses that belong in a *backtest* universe. ``BREAK`` is Binance's
#: state for a delisted/halted pair: it stays in ``exchangeInfo`` and the klines
#: endpoint still serves its history, so including it is what removes most of
#: the survivorship bias. Live trading must never see these — ``screener.py``
#: keeps its own ``TRADING``-only check and is deliberately not widened here.
BACKTEST_STATUSES = frozenset({"TRADING", "BREAK"})


def universe_symbols(
    exchange_info: dict,
    config: ScreenerConfig,
    *,
    include_delisted: bool = True,
) -> list[str]:
    """Every spot pair worth downloading: right quote asset, not excluded by
    asset class, and — by default — *including pairs that have since died*.

    Survivorship used to enter right here. Restricting to ``TRADING`` dropped
    every pair delisted since 2024, and those are disproportionately losers, so
    every return figure the harness produced was biased upward. Measured
    2026-08-05: of 723 USDT pairs that ever traded, 489 are ``TRADING`` and 193
    are genuine delisted spot pairs in ``BREAK`` — **113 of which have real
    daily history inside the 8-quarter backtest window** (median 338 bars).
    Only one genuine pair (``NBTUSDT``) has left ``exchangeInfo`` altogether;
    the other 40 absentees are leveraged UP/DOWN tokens we exclude anyway.

    Pass ``include_delisted=False`` to reproduce the old survivorship-biased
    universe — kept only so the bias can be *measured* by differencing the two
    runs, never as the default.
    """
    statuses = BACKTEST_STATUSES if include_delisted else frozenset({"TRADING"})
    out = []
    for info in exchange_info["symbols"]:
        if info["quoteAsset"] != config.quote_asset:
            continue
        if info["status"] not in statuses or not info.get("isSpotTradingAllowed", True):
            continue
        if excluded_base_reason(info["baseAsset"], config) is not None:
            continue
        out.append(info["symbol"])
    return sorted(out)


def load_series(store, symbols: list[str], interval: str = "1d") -> dict[str, list[Candle]]:
    series = {}
    for symbol in symbols:
        candles = store.read(symbol, interval)
        if candles:
            series[symbol] = candles
    return series


def main() -> int:
    """CLI: fetch the universe once, then screen-and-replay point-in-time.

    ``--fetch`` hits Binance for every USDT spot pair's daily history and is the
    slow step (a few hundred pairs); the store is reused on later runs.
    """
    import argparse
    from datetime import UTC, datetime
    from pathlib import Path

    from trader.config import load_config
    from trader.marketdata import CandleStore, KlineDownloader

    parser = argparse.ArgumentParser(description="Point-in-time screen and backtest (D-42b)")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--fetch", action="store_true", help="download/refresh daily history")
    parser.add_argument("--days", type=int, default=1000, help="history depth to fetch")
    parser.add_argument("--periods", type=int, default=8)
    parser.add_argument("--period-days", type=int, default=QUARTER_DAYS)
    parser.add_argument("--entry-n", type=int, default=15)
    parser.add_argument("--exit-n", type=int, default=15)
    parser.add_argument("--stop", type=float, default=3.0, help="stop %%; 0 disables the stop")
    parser.add_argument("--capital", type=float, default=150.0)
    parser.add_argument(
        "--ranks",
        default=None,
        help="path to marketcap-ranks.json (trader.marketcap); omit to skip the criterion",
    )
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    store = CandleStore(config.data_dir / "candles")

    from trader.marketdata import BinancePublicData

    public = BinancePublicData()
    symbols = universe_symbols(public.exchange_info(), config.screener)
    print(f"{len(symbols)} USDT spot pairs in today's exchangeInfo")

    if args.fetch:
        downloader = KlineDownloader(public, store)
        for i, symbol in enumerate(symbols, 1):
            try:
                added = downloader.backfill(symbol, "1d", days=args.days)
            except Exception as exc:  # noqa: BLE001 — one dead pair must not stop the sweep
                print(f"  [{i}/{len(symbols)}] {symbol}: FAILED ({exc})")
                continue
            print(f"  [{i}/{len(symbols)}] {symbol}: +{added} daily candles")

    series = load_series(store, symbols)
    print(f"{len(series)} pairs with stored history")
    if not series:
        print("No history stored. Run once with --fetch.")
        return 1

    newest = max(c.open_time for candles in series.values() for c in candles)
    span = args.periods * args.period_days * DAY_MS
    start_ms = newest - span

    ranks_at_date = None
    if args.ranks:
        from trader.marketcap import RankHistory

        history = RankHistory(Path(args.ranks))
        dates = [start_ms + i * args.period_days * DAY_MS for i in range(args.periods)]
        ranks_at_date = history.for_dates(dates)
        print(
            f"market-cap ranks cover {len(ranks_at_date)} of {args.periods} screening dates "
            f"(free tier reaches back ~1 year — D-43b)"
        )

    result = run_point_in_time(
        series,
        start_ms=start_ms,
        periods=args.periods,
        config=config.screener,
        initial_cash=args.capital,
        period_days=args.period_days,
        entry_n=args.entry_n,
        exit_n=args.exit_n,
        stop_loss_pct=args.stop,
        market_cap_ranks_at=ranks_at_date,
    )
    print()
    print(
        f"Window {datetime.fromtimestamp(start_ms / 1000, tz=UTC):%Y-%m-%d} -> "
        f"{datetime.fromtimestamp(newest / 1000, tz=UTC):%Y-%m-%d}"
    )
    print(result.summary())
    print(f"universe turnover between screens: {result.turnover}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
