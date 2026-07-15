"""S4 research runner — matrix construction, windows, report rendering —
and the macro-event calendar feeding the D-23 blackout rule."""

from __future__ import annotations

from datetime import UTC, datetime

from trader.macro_events import all_event_times_ms, cpi_times_ms, fomc_times_ms
from trader.marketdata import Candle, CandleStore
from trader.research import MatrixRunner, render_report

MIN = 60_000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC, aligned on 1h


class TestMacroEvents:
    def test_sorted_and_merged(self):
        merged = all_event_times_ms()
        assert merged == sorted(merged)
        assert len(merged) == len(fomc_times_ms()) + len(cpi_times_ms())

    def test_known_event_utc(self):
        # FOMC 2025-06-18 14:00 ET = 18:00 UTC (EDT)
        expected = int(datetime(2025, 6, 18, 18, 0, tzinfo=UTC).timestamp() * 1000)
        assert expected in fomc_times_ms()

    def test_all_within_backtest_horizon(self):
        lo = int(datetime(2024, 7, 1, tzinfo=UTC).timestamp() * 1000)
        hi = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
        assert all(lo <= t <= hi for t in all_event_times_ms())


def make_store(tmp_path, symbol: str, n: int) -> CandleStore:
    """A store with *n* 1m candles of a gentle sine-ish walk (deterministic)."""
    store = CandleStore(tmp_path / "candles")
    candles = []
    price = 100.0
    for i in range(n):
        drift = 0.3 if (i // 200) % 2 == 0 else -0.28  # alternating regimes
        o = price
        price = max(1.0, price + drift * ((i * 7919) % 13 - 6) / 6)
        hi, lo = max(o, price) * 1.001, min(o, price) * 0.999
        candles.append(Candle(T0 + i * MIN, o, hi, lo, price, 10.0, 1000.0, 5))
    store.append(symbol, "1m", candles)
    return store


class TestMatrixRunner:
    def test_discover_symbols(self, tmp_path):
        store = make_store(tmp_path, "AAAUSDT", 50)
        runner = MatrixRunner(store, now_ms=T0 + 50 * MIN)
        assert runner.discover_symbols() == ["AAAUSDT"]

    def test_run_symbol_produces_full_and_recent_cells(self, tmp_path):
        n = 3 * 1440  # 3 days of 1m
        store = make_store(tmp_path, "AAAUSDT", n)
        runner = MatrixRunner(
            store, recent_days=1, event_times_ms=[], now_ms=T0 + n * MIN
        )
        cells = runner.run_symbol("AAAUSDT")
        windows = {c.window for c in cells}
        assert windows == {"full", "recent"}
        # every candidate ran in both windows
        assert len(cells) == len(runner.candidates) * 2

    def test_blackout_never_increases_trades(self, tmp_path):
        n = 2 * 1440
        store = make_store(tmp_path, "AAAUSDT", n)
        events = [T0 + i * 6 * 60 * MIN for i in range(8)]  # every 6h
        runner = MatrixRunner(
            store, recent_days=9999, event_times_ms=events, now_ms=T0 + n * MIN
        )
        for cell in runner.run_symbol("AAAUSDT"):
            assert cell.blackout.round_trips <= cell.bare.round_trips

    def test_determinism_across_runs(self, tmp_path):
        n = 1440
        store = make_store(tmp_path, "AAAUSDT", n)
        runner = MatrixRunner(store, event_times_ms=[], now_ms=T0 + n * MIN)
        a = runner.run_symbol("AAAUSDT")
        b = runner.run_symbol("AAAUSDT")
        assert [c.bare.fingerprint() for c in a] == [c.bare.fingerprint() for c in b]

    def test_render_report_contains_every_candidate(self, tmp_path):
        n = 1440
        store = make_store(tmp_path, "AAAUSDT", n)
        runner = MatrixRunner(store, event_times_ms=[], now_ms=T0 + n * MIN)
        cells = runner.run_symbol("AAAUSDT")
        md = render_report(cells, capital=150.0, recent_days=180, generated="test")
        for spec in runner.candidates:
            assert f"{spec.name} @ {spec.interval}" in md
        assert "Blackout" in md and "AAAUSDT" in md
