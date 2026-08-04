"""Walk-forward parameter selection on the point-in-time harness (D-42b, D-43c).

The question "what should ``entry_n``/``exit_n``/``stop_loss_pct`` be" has an
easy wrong answer and a hard right one.

The wrong answer is to run a grid over the whole history and report the winner.
That number is unobtainable: it required knowing the whole period in advance,
and on a 150-point grid over 8 quarters the best cell is mostly noise. It is
also exactly how D-40 was approved and reversed inside a single day — the
hindsight-picked setting looked decisively better and wasn't.

The right answer is to ask what a rule would have produced *in real time*: at
each quarter boundary, choose parameters using only quarters already finished,
then record what that choice earned in the quarter that followed. Chaining
those out-of-sample quarters gives a return the account could actually have
had. The incumbent 15/15/3.0 is replayed over the identical quarters, so the
comparison is like-for-like.

What this module cannot do is make 8 quarters into a large sample. Even a clean
walk-forward edge over 5 evaluation quarters is weak evidence, and the report
says so rather than rounding it up to a recommendation. Per D-43(c) the output
is a written finding for the investor; production keeps running 15/15/3.0 until
he rules.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from trader.config import ScreenerConfig
from trader.marketdata import Candle
from trader.pit import run_point_in_time

# The incumbent, and the thing every candidate is measured against.
BASELINE = (15, 15, 3.0)

# Deliberately coarse. A fine grid over 8 quarters does not find a better
# setting, it finds a better-fitting one: adjacent cells differ by less than the
# quarter-to-quarter noise, so the extra resolution is all overfit.
DEFAULT_GRID = tuple(
    (entry_n, exit_n, stop)
    for entry_n in (10, 15, 20, 30, 40)
    for exit_n in (5, 10, 15, 20)
    for stop in (2.0, 3.0, 5.0, 8.0)
)


@dataclass(frozen=True, slots=True)
class Params:
    entry_n: int
    exit_n: int
    stop_loss_pct: float

    def __str__(self) -> str:
        return f"{self.entry_n}/{self.exit_n}/{self.stop_loss_pct:g}"


@dataclass(slots=True)
class Step:
    """One walk-forward decision: chosen on the past, scored on the next period."""

    period: int
    chosen: Params
    train_score: float
    oos_return_pct: float
    baseline_return_pct: float


@dataclass(slots=True)
class WalkForwardResult:
    steps: list[Step] = field(default_factory=list)
    grid_size: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def oos_returns(self) -> list[float]:
        return [s.oos_return_pct for s in self.steps]

    @property
    def baseline_returns(self) -> list[float]:
        return [s.baseline_return_pct for s in self.steps]

    @property
    def chosen_compounded_pct(self) -> float:
        return _compound(self.oos_returns)

    @property
    def baseline_compounded_pct(self) -> float:
        return _compound(self.baseline_returns)

    @property
    def periods_beating_baseline(self) -> int:
        return sum(1 for s in self.steps if s.oos_return_pct > s.baseline_return_pct)

    @property
    def stability(self) -> dict[str, int]:
        """How often each setting was chosen. A rule that picks a different
        winner every quarter has not found a parameter, it has found noise —
        so this is reported next to the return, not buried."""
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[str(step.chosen)] = counts.get(str(step.chosen), 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def summary(self) -> str:
        n = len(self.steps)
        if not n:
            return "walk-forward: no evaluation periods (need more history than warmup)"
        diffs = [s.oos_return_pct - s.baseline_return_pct for s in self.steps]
        lines = [
            f"Walk-forward over {n} out-of-sample periods, grid of {self.grid_size}",
            f"  selected  : {self.chosen_compounded_pct:+7.2f}% compounded | "
            f"mean/period {statistics.fmean(self.oos_returns):+.2f}%",
            f"  baseline  : {self.baseline_compounded_pct:+7.2f}% compounded | "
            f"mean/period {statistics.fmean(self.baseline_returns):+.2f}%  ({Params(*BASELINE)})",
            f"  selection beat baseline in {self.periods_beating_baseline}/{n} periods, "
            f"mean edge {statistics.fmean(diffs):+.2f}pp"
            + (f" (sd {statistics.stdev(diffs):.2f})" if n > 1 else ""),
            f"  settings chosen: {self.stability}",
        ]
        for step in self.steps:
            lines.append(
                f"  P{step.period}: chose {step.chosen} (train {step.train_score:+.2f}%) "
                f"-> {step.oos_return_pct:+7.2f}% vs baseline {step.baseline_return_pct:+7.2f}%"
            )
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _compound(returns: list[float]) -> float:
    equity = 1.0
    for r in returns:
        equity *= 1.0 + r / 100.0
    return (equity - 1.0) * 100.0


def score(returns: list[float]) -> float:
    """Training score for a candidate: mean period return penalised by spread.

    Ranking on mean return alone hands the grid to whichever setting caught one
    outsized quarter — on this universe a single quarter swings ~40pp, so mean
    return is nearly a lottery over which cell held the winner. Subtracting half
    a standard deviation prefers a setting that was merely adequate repeatedly,
    which is the property that has any chance of persisting.
    """
    if not returns:
        return float("-inf")
    mean = statistics.fmean(returns)
    spread = statistics.stdev(returns) if len(returns) > 1 else 0.0
    return mean - 0.5 * spread


def period_returns(
    series: dict[str, list[Candle]],
    params: Params,
    *,
    start_ms: int,
    periods: int,
    config: ScreenerConfig,
    **kwargs,
) -> list[float]:
    """Each period's return for one setting, every period starting from the same
    capital so settings are comparable period by period."""
    result = run_point_in_time(
        series,
        start_ms=start_ms,
        periods=periods,
        config=config,
        entry_n=params.entry_n,
        exit_n=params.exit_n,
        stop_loss_pct=params.stop_loss_pct,
        chain_equity=False,
        **kwargs,
    )
    by_index = {p.index: p.return_pct for p in result.periods}
    # A skipped period (empty screen) is flat, not missing: the strategy holds
    # cash. Dropping it would let a setting look good by trading less often.
    return [by_index.get(i, 0.0) for i in range(periods)]


def walk_forward(
    series: dict[str, list[Candle]],
    *,
    start_ms: int,
    periods: int,
    config: ScreenerConfig,
    grid=DEFAULT_GRID,
    min_train: int = 3,
    progress=None,
    **kwargs,
) -> WalkForwardResult:
    """Select on periods ``[0, k)``, score on period ``k``, for each ``k``.

    ``min_train`` is the number of finished periods required before the rule is
    allowed to choose at all — with fewer, the "selection" is a single quarter's
    winner and the exercise measures nothing.
    """
    grid_params = [Params(*g) for g in grid]
    baseline = Params(*BASELINE)
    if baseline not in grid_params:
        grid_params.append(baseline)

    table: dict[Params, list[float]] = {}
    for i, params in enumerate(grid_params):
        table[params] = period_returns(
            series, params, start_ms=start_ms, periods=periods, config=config, **kwargs
        )
        if progress:
            progress(i + 1, len(grid_params), params)

    result = WalkForwardResult(grid_size=len(grid_params))
    for k in range(min_train, periods):
        best = max(grid_params, key=lambda p: (score(table[p][:k]), -p.entry_n, -p.exit_n))
        result.steps.append(
            Step(
                period=k,
                chosen=best,
                train_score=score(table[best][:k]),
                oos_return_pct=table[best][k],
                baseline_return_pct=table[baseline][k],
            )
        )

    if len(result.steps) < 6:
        result.notes.append(
            f"{len(result.steps)} out-of-sample periods is too few to establish an edge. "
            "Treat any difference from the baseline as indicative, not decided."
        )
    return result


def main() -> int:
    """CLI: walk-forward re-derivation over the stored universe (D-43c report)."""
    import argparse
    from datetime import UTC, datetime
    from pathlib import Path

    from trader.config import load_config
    from trader.marketdata import CandleStore
    from trader.pit import DAY_MS, QUARTER_DAYS, base_asset, load_series
    from trader.screener import excluded_base_reason

    parser = argparse.ArgumentParser(description="Walk-forward parameter selection (D-43c)")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--periods", type=int, default=8)
    parser.add_argument("--period-days", type=int, default=QUARTER_DAYS)
    parser.add_argument("--min-train", type=int, default=3)
    parser.add_argument("--capital", type=float, default=150.0)
    parser.add_argument("--ranks", default=None, help="marketcap-ranks.json (D-43b)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    config = load_config().screener
    store = CandleStore(data_dir / "candles")
    # The universe is whatever pit already fetched, so the sweep runs against
    # the exact stored history rather than re-hitting Binance for a list that
    # may have changed since.
    symbols = [
        d.name
        for d in sorted((data_dir / "candles").glob("*"))
        if d.is_dir()
        and d.name.endswith(config.quote_asset)
        and excluded_base_reason(base_asset(d.name, config.quote_asset), config) is None
    ]
    series = load_series(store, symbols)
    print(f"{len(series)} pairs with stored history")
    if not series:
        print(f"No history stored in {data_dir}. Run `python -m trader.pit --fetch` first.")
        return 1

    newest = max(c.open_time for candles in series.values() for c in candles)
    start_ms = newest - args.periods * args.period_days * DAY_MS

    ranks_at = None
    if args.ranks:
        from trader.marketcap import RankHistory

        history = RankHistory(Path(args.ranks))
        dates = [start_ms + i * args.period_days * DAY_MS for i in range(args.periods)]
        ranks_at = history.for_dates(dates)
        print(f"market-cap ranks cover {len(ranks_at)}/{args.periods} screening dates (D-43b)")

    end_ms = start_ms + args.periods * args.period_days * DAY_MS
    print(
        f"Window {datetime.fromtimestamp(start_ms / 1000, tz=UTC):%Y-%m-%d} -> "
        f"{datetime.fromtimestamp(end_ms / 1000, tz=UTC):%Y-%m-%d}"
    )

    def progress(done: int, total: int, params: Params) -> None:
        if done % 10 == 0 or done == total:
            print(f"  grid {done}/{total} (last {params})", flush=True)

    result = walk_forward(
        series,
        start_ms=start_ms,
        periods=args.periods,
        config=config,
        min_train=args.min_train,
        initial_cash=args.capital,
        period_days=args.period_days,
        market_cap_ranks_at=ranks_at,
        progress=progress,
    )
    print()
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
