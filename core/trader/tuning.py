"""Sprint 5 — Donchian walk-forward tuning (D-26 follow-through).

The S4 matrix picked DonchianBreakout on *untuned defaults* (20/10, 2% stop).
This module asks whether tuning helps — without curve-fitting. Method:

Walk-forward validation. The stored history is split into consecutive
out-of-sample (OOS) test quarters. For each quarter, a parameter grid is
scored ONLY on data strictly before the quarter (expanding train window,
portfolio-mean return across all pairs); the winning combo then trades the
quarter it never saw. The tuned OOS sequence is compared against the D-26
baseline (20/10/2%) trading the same quarters. Tuning is adopted only if it
beats the baseline out-of-sample — "keep the defaults" is a pass, not a
failure.

Test quarters get a warmup lead-in: candles before the quarter are fed to the
strategy so its channels are formed on day one, but entries are gated until
the quarter starts (same restrict-only trick as the D-23 wrappers), so no
capital result leaks in from the train period.

CLI: python -m trader.tuning [--data-dir DIR] [--intervals 1d 4h]
     [--folds 4] [--fold-days 91] [--symbols S ...]
Report: /data/reports/donchian-walkforward-YYYY-MM-DD.md
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, median

from trader.backtest import Backtester, BacktestParams, Strategy, StrategyContext
from trader.marketdata import INTERVAL_MS, Candle, CandleStore, resample
from trader.strategies import DonchianBreakout

DAY_MS = 86_400_000

# D-26 baseline: the exact untuned parameters the S4 recommendation measured.
BASELINE = (20, 10, 2.0)


@dataclass(frozen=True, slots=True)
class DonchianParams:
    entry_n: int
    exit_n: int
    stop_loss_pct: float

    def label(self) -> str:
        return f"{self.entry_n}/{self.exit_n}/{self.stop_loss_pct:g}%"


def default_grid() -> list[DonchianParams]:
    """Small, coarse grid on purpose: a fine grid on two years of data is a
    curve-fitting machine. exit_n <= entry_n (an exit channel longer than the
    entry channel means exits lag entries — never the Donchian intent)."""
    grid = []
    for entry_n in (10, 15, 20, 30, 40, 55):
        for exit_n in (5, 10, 15, 20):
            if exit_n > entry_n:
                continue
            for stop in (1.5, 2.0, 3.0, 4.0):
                grid.append(DonchianParams(entry_n, exit_n, stop))
    return grid


class _NoEntryBefore:
    """Strategy wrapper: feeds every candle to the inner strategy (so channels
    warm up on pre-window history) but drops buy intents before *start_ms*.
    Sells always pass — same restrict-only contract as the D-23 wrappers."""

    def __init__(self, inner: Strategy, start_ms: int):
        self.inner = inner
        self.warmup = inner.warmup
        self.start_ms = start_ms

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        if candle.open_time >= self.start_ms:
            self.inner.on_candle(candle, ctx)
        else:
            self.inner.on_candle(candle, _BuyGate(ctx))  # type: ignore[arg-type]


class _BuyGate:
    def __init__(self, inner: StrategyContext):
        self._inner = inner

    @property
    def position(self):
        return self._inner.position

    @property
    def cash(self) -> float:
        return self._inner.cash

    def buy(self, quote_amount: float, **kwargs) -> None:
        return None

    def sell_all(self) -> None:
        self._inner.sell_all()


@dataclass(slots=True)
class FoldResult:
    fold: int
    test_start_ms: int
    test_end_ms: int
    chosen: DonchianParams
    train_mean_pct: float  # chosen combo's train score (in-sample, for honesty)
    tuned_oos_pct: float  # portfolio mean, out of sample
    baseline_oos_pct: float
    tuned_by_symbol: dict[str, float]
    baseline_by_symbol: dict[str, float]


class WalkForwardTuner:
    def __init__(
        self,
        store: CandleStore,
        *,
        capital: float = 150.0,
        grid: Sequence[DonchianParams] | None = None,
        folds: int = 4,
        fold_days: int = 91,
        min_train_days: int = 180,
        now_ms: int | None = None,
    ):
        self.store = store
        self.capital = capital
        self.spend = capital * 0.10  # D-08 sizing
        self.grid = list(grid) if grid is not None else default_grid()
        self.folds = folds
        self.fold_days = fold_days
        self.min_train_days = min_train_days
        self.now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.params = BacktestParams(initial_cash=capital)

    def _make(self, p: DonchianParams) -> DonchianBreakout:
        return DonchianBreakout(
            p.entry_n, p.exit_n, spend_usdt=self.spend, stop_loss_pct=p.stop_loss_pct
        )

    def _run(
        self, p: DonchianParams, candles: list[Candle], *, symbol: str, interval: str,
        entries_from_ms: int | None = None,
    ) -> float:
        strat: Strategy = self._make(p)
        if entries_from_ms is not None:
            strat = _NoEntryBefore(strat, entries_from_ms)
        result = Backtester(self.params).run(strat, candles, symbol=symbol, interval=interval)
        return result.total_return_pct

    def run(
        self,
        symbols: Sequence[str],
        interval: str,
        progress: Callable[[str], None] | None = None,
    ) -> list[FoldResult]:
        step = INTERVAL_MS[interval]
        series: dict[str, list[Candle]] = {}
        for s in symbols:
            base = self.store.read(s, "1m")
            if base:
                series[s] = base if interval == "1m" else resample(base, step)
        if not series:
            return []

        lead = max(p.entry_n for p in self.grid) + 5  # warmup lead-in, in candles
        results: list[FoldResult] = []
        for k in range(self.folds):
            test_end = self.now_ms - (self.folds - 1 - k) * self.fold_days * DAY_MS
            test_start = test_end - self.fold_days * DAY_MS

            # Score the grid on everything strictly before the test quarter.
            trains: dict[str, list[Candle]] = {}
            for sym, candles in series.items():
                train = [c for c in candles if c.open_time < test_start]
                if train and train[-1].open_time - train[0].open_time >= (
                    self.min_train_days * DAY_MS
                ):
                    trains[sym] = train
            best: tuple[float, DonchianParams] | None = None
            for p in self.grid:
                rets = [
                    self._run(p, train, symbol=sym, interval=interval)
                    for sym, train in trains.items()
                ]
                if not rets:
                    continue
                score = mean(rets)
                # Deterministic tie-break: fewer trades wins is unmeasured here;
                # grid order (coarser first) breaks exact ties instead.
                if best is None or score > best[0]:
                    best = (score, p)
            if best is None:
                continue
            train_score, chosen = best

            tuned_by, base_by = {}, {}
            for sym, candles in series.items():
                test = [
                    c for c in candles
                    if test_start - lead * step <= c.open_time < test_end
                ]
                in_window = [c for c in test if c.open_time >= test_start]
                if len(in_window) < self.fold_days // 4:  # pair barely listed — skip
                    continue
                tuned_by[sym] = self._run(
                    chosen, test, symbol=sym, interval=interval, entries_from_ms=test_start
                )
                base_by[sym] = self._run(
                    DonchianParams(*BASELINE), test, symbol=sym, interval=interval,
                    entries_from_ms=test_start,
                )
            if not tuned_by:
                continue
            fr = FoldResult(
                fold=k + 1,
                test_start_ms=test_start,
                test_end_ms=test_end,
                chosen=chosen,
                train_mean_pct=train_score,
                tuned_oos_pct=mean(tuned_by.values()),
                baseline_oos_pct=mean(base_by.values()),
                tuned_by_symbol=tuned_by,
                baseline_by_symbol=base_by,
            )
            results.append(fr)
            if progress:
                d0 = datetime.fromtimestamp(test_start / 1000, tz=UTC)
                d1 = datetime.fromtimestamp(test_end / 1000, tz=UTC)
                progress(
                    f"  fold {fr.fold} [{d0:%Y-%m-%d} → {d1:%Y-%m-%d}] "
                    f"chose {chosen.label()} (train {train_score:+.2f}%) — "
                    f"OOS tuned {fr.tuned_oos_pct:+.2f}% vs baseline {fr.baseline_oos_pct:+.2f}%"
                )
        return results


def render_report(
    by_interval: dict[str, list[FoldResult]], *, capital: float, generated: str
) -> str:
    lines = [
        "# Donchian walk-forward tuning — Sprint 5",
        "",
        f"Generated: {generated}",
        f"Params: capital {capital:g} USDT, spend/trade {capital * 0.10:g} USDT (D-08), "
        "fees 0.1%/side, slippage 5 bps, fills at next open (S3 engine).",
        f"Baseline: D-26 untuned defaults {BASELINE[0]}/{BASELINE[1]}/{BASELINE[2]:g}%.",
        "Method: expanding-train walk-forward; each test quarter is traded by the grid "
        "combo that scored best on data strictly before it. Test quarters include a "
        "warmup lead-in with entries gated (no capital leakage).",
        "",
    ]
    for interval, folds in by_interval.items():
        lines += [f"## {interval}", ""]
        if not folds:
            lines += ["_No folds could be evaluated (insufficient history)._", ""]
            continue
        lines += [
            "| Fold | Test window | Chosen params | Train mean | OOS tuned | OOS baseline | Δ |",
            "|------|-------------|---------------|-----------:|----------:|-------------:|---:|",
        ]
        for fr in folds:
            d0 = datetime.fromtimestamp(fr.test_start_ms / 1000, tz=UTC)
            d1 = datetime.fromtimestamp(fr.test_end_ms / 1000, tz=UTC)
            lines.append(
                f"| {fr.fold} | {d0:%Y-%m-%d} → {d1:%Y-%m-%d} | {fr.chosen.label()} | "
                f"{fr.train_mean_pct:+.2f}% | {fr.tuned_oos_pct:+.2f}% | "
                f"{fr.baseline_oos_pct:+.2f}% | "
                f"{fr.tuned_oos_pct - fr.baseline_oos_pct:+.2f}pp |"
            )
        tuned = [f.tuned_oos_pct for f in folds]
        base = [f.baseline_oos_pct for f in folds]
        lines += [
            f"| **all** | | | | mean {mean(tuned):+.2f}% / median {median(tuned):+.2f}% | "
            f"mean {mean(base):+.2f}% / median {median(base):+.2f}% | "
            f"{mean(tuned) - mean(base):+.2f}pp |",
            "",
            f"**Verdict ({interval}):** "
            + (
                "tuned parameters beat the baseline out-of-sample — consider adopting "
                f"the most recent fold's pick {folds[-1].chosen.label()} for paper."
                if mean(tuned) > mean(base)
                else "tuning does NOT beat the untuned D-26 baseline out-of-sample — "
                "keep the defaults for paper (this is the honest outcome, not a failure)."
            ),
            "",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse
    from pathlib import Path

    from trader.config import load_config

    parser = argparse.ArgumentParser(description="Donchian walk-forward tuning (S5)")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--intervals", nargs="*", default=["1d", "4h"])
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--fold-days", type=int, default=91)
    parser.add_argument("--capital", type=float, default=150.0)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    store = CandleStore(config.candles_dir)
    tuner = WalkForwardTuner(
        store, capital=args.capital, folds=args.folds, fold_days=args.fold_days
    )
    symbols = args.symbols or sorted(
        p.name for p in store.root.iterdir()
        if (p / "1m").is_dir() and any((p / "1m").glob("*.parquet"))
    )
    if not symbols:
        print("No stored 1m candles found — run trader.marketdata first.")
        return 1

    by_interval: dict[str, list[FoldResult]] = {}
    for interval in args.intervals:
        print(f"[{interval}] walk-forward over {len(symbols)} pairs, "
              f"{len(tuner.grid)} grid combos, {args.folds} folds...")
        by_interval[interval] = tuner.run(symbols, interval, progress=print)

    generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    md = render_report(by_interval, capital=args.capital, generated=generated)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"donchian-walkforward-{datetime.now(tz=UTC):%Y-%m-%d}.md"
    out.write_text(md)
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
