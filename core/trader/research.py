"""Sprint 4 research runner — the candidate × interval × pair backtest matrix
behind the trade-rhythm recommendation (Q9/D-11) and the D-23 rule gating.

Replays stored 1m candles (resampled per candidate timeframe) through the
Backtester for every screened pair, each candidate twice: bare, and wrapped in
the D-23 event-blackout rule (FOMC+CPI calendar) so the rule's effect is a
measured delta, not an opinion. Two windows per cell — full history and the
recent regime — because a strategy that only worked in last year's market is
not a recommendation.

CLI: python -m trader.research [--data-dir DIR] [--symbols S ...]
     [--recent-days 180] [--capital 150]
Report: /data/reports/backtest-matrix-YYYY-MM-DD.md
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, median

from trader.backtest import Backtester, BacktestParams, BacktestResult, Strategy
from trader.macro_events import all_event_times_ms
from trader.marketdata import INTERVAL_MS, Candle, CandleStore, resample
from trader.strategies import (
    DonchianBreakout,
    EventBlackout,
    RsiMeanReversion,
    TrendFollow,
)


@dataclass(frozen=True, slots=True)
class CandidateSpec:
    """A strategy archetype pinned to a timeframe. ``rhythm`` labels the Q9
    bucket the cell's evidence speaks for."""

    name: str
    rhythm: str  # scalp | momentum | intraday
    interval: str
    factory: Callable[[], Strategy]


def default_candidates(spend: float) -> list[CandidateSpec]:
    return [
        CandidateSpec(
            "RSI mean-reversion", "scalp", "1m",
            lambda: RsiMeanReversion(spend_usdt=spend),
        ),
        CandidateSpec(
            "RSI mean-reversion", "scalp", "5m",
            lambda: RsiMeanReversion(spend_usdt=spend),
        ),
        CandidateSpec(
            "Donchian breakout", "momentum", "15m",
            lambda: DonchianBreakout(spend_usdt=spend),
        ),
        CandidateSpec(
            "Donchian breakout", "momentum", "1h",
            lambda: DonchianBreakout(spend_usdt=spend),
        ),
        CandidateSpec(
            "EMA trend-follow", "intraday", "5m",
            lambda: TrendFollow(spend_usdt=spend),
        ),
        CandidateSpec(
            "EMA trend-follow", "intraday", "15m",
            lambda: TrendFollow(spend_usdt=spend),
        ),
    ]


@dataclass(slots=True)
class Cell:
    spec: CandidateSpec
    symbol: str
    window: str  # full | recent
    bare: BacktestResult
    blackout: BacktestResult
    blocked_candles: int

    @property
    def blackout_delta_pct(self) -> float:
        return self.blackout.total_return_pct - self.bare.total_return_pct


class MatrixRunner:
    def __init__(
        self,
        store: CandleStore,
        *,
        capital: float = 150.0,
        recent_days: int = 180,
        event_times_ms: Sequence[int] | None = None,
        now_ms: int | None = None,
    ):
        self.store = store
        self.capital = capital
        self.recent_days = recent_days
        self.events = list(event_times_ms if event_times_ms is not None else all_event_times_ms())
        self.now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.params = BacktestParams(initial_cash=capital)
        self.candidates = default_candidates(capital * 0.10)  # D-08 sizing

    def discover_symbols(self) -> list[str]:
        root = self.store.root
        if not root.is_dir():
            return []
        return sorted(
            p.name
            for p in root.iterdir()
            if (p / "1m").is_dir() and any((p / "1m").glob("*.parquet"))
        )

    def run_symbol(self, symbol: str, progress: Callable[[str], None] | None = None) -> list[Cell]:
        base = self.store.read(symbol, "1m")
        if not base:
            return []
        recent_start = self.now_ms - self.recent_days * 86_400_000
        windows = [("full", base)]
        recent = [c for c in base if c.open_time >= recent_start]
        # only report 'recent' separately when it differs from full history
        if recent and len(recent) < len(base):
            windows.append(("recent", recent))

        cells: list[Cell] = []
        by_interval: dict[tuple[str, str], list[Candle]] = {}
        for window, candles in windows:
            for spec in self.candidates:
                key = (window, spec.interval)
                if key not in by_interval:
                    by_interval[key] = (
                        candles if spec.interval == "1m"
                        else resample(candles, INTERVAL_MS[spec.interval])
                    )
                data = by_interval[key]
                bt = Backtester(self.params)
                bare = bt.run(spec.factory(), data, symbol=symbol, interval=spec.interval)
                wrapped = EventBlackout(spec.factory(), self.events)
                blk = bt.run(wrapped, data, symbol=symbol, interval=spec.interval)
                cells.append(Cell(spec, symbol, window, bare, blk, wrapped.blocked_candles))
                if progress:
                    progress(
                        f"  {symbol} {window:6s} {spec.name} @ {spec.interval}: "
                        f"{bare.total_return_pct:+.2f}% "
                        f"(blackout {blk.total_return_pct:+.2f}%)"
                    )
        return cells


def render_report(cells: list[Cell], *, capital: float, recent_days: int, generated: str) -> str:
    lines = [
        "# Backtest matrix — Sprint 4 candidates",
        "",
        f"Generated: {generated}",
        f"Params: capital {capital:g} USDT, spend/trade {capital * 0.10:g} USDT (D-08), "
        "fees 0.1%/side, slippage 5 bps, fills at next open (S3 engine).",
        f"Windows: full stored history and the most recent {recent_days} days.",
        "Blackout column: same candidate wrapped in the D-23 FOMC+CPI event-blackout "
        "rule (entries blocked ±60 min around each event).",
        "",
    ]
    specs = sorted({(c.spec.rhythm, c.spec.name, c.spec.interval) for c in cells})
    for rhythm, name, interval in specs:
        group = [
            c for c in cells
            if (c.spec.rhythm, c.spec.name, c.spec.interval) == (rhythm, name, interval)
        ]
        lines += [
            f"## {name} @ {interval}  ({rhythm})",
            "",
            "| Pair | Window | Return | Max DD | Trips | Win rate | Fees | "
            "Blackout Δ | Blocked candles |",
            "|------|--------|--------|--------|-------|----------|------|"
            "-----------|-----------------|",
        ]
        for c in sorted(group, key=lambda c: (c.symbol, c.window)):
            wr = c.bare.win_rate_pct
            lines.append(
                f"| {c.symbol} | {c.window} | {c.bare.total_return_pct:+.2f}% | "
                f"{c.bare.max_drawdown_pct:.2f}% | {c.bare.round_trips} | "
                f"{'n/a' if wr is None else f'{wr:.0f}%'} | "
                f"{c.bare.total_fees:.2f} | {c.blackout_delta_pct:+.2f}pp | "
                f"{c.blocked_candles} |"
            )
        full = [c for c in group if c.window == "full"]
        recent = [c for c in group if c.window == "recent"]
        for label, sub in (("full", full), ("recent", recent)):
            if not sub:
                continue
            rets = [c.bare.total_return_pct for c in sub]
            lines.append(
                f"| **all ({label})** | | mean {mean(rets):+.2f}% / median "
                f"{median(rets):+.2f}% | worst {max(c.bare.max_drawdown_pct for c in sub):.2f}% | "
                f"{sum(c.bare.round_trips for c in sub)} | | "
                f"{sum(c.bare.total_fees for c in sub):.2f} | "
                f"mean {mean(c.blackout_delta_pct for c in sub):+.2f}pp | |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse
    from pathlib import Path

    from trader.config import load_config

    parser = argparse.ArgumentParser(description="Run the S4 candidate backtest matrix")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--recent-days", type=int, default=180)
    parser.add_argument("--capital", type=float, default=150.0)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    store = CandleStore(config.candles_dir)
    runner = MatrixRunner(store, capital=args.capital, recent_days=args.recent_days)

    symbols = args.symbols or runner.discover_symbols()
    if not symbols:
        print("No stored 1m candles found — run trader.marketdata first.")
        return 1

    cells: list[Cell] = []
    for symbol in symbols:
        print(f"[{symbol}] running {len(runner.candidates)} candidates x 2 windows x A/B...")
        cells.extend(runner.run_symbol(symbol, progress=print))

    generated = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    md = render_report(
        cells, capital=args.capital, recent_days=args.recent_days, generated=generated
    )
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"backtest-matrix-{datetime.now(tz=UTC):%Y-%m-%d}.md"
    out.write_text(md)
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
