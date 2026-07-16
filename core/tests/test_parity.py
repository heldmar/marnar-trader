"""Parity tooling (Q13): portfolio replay caps + paper stats reconstruction."""

from __future__ import annotations

from decimal import Decimal

from trader.marketdata import INTERVAL_MS, Candle, CandleStore
from trader.parity import PortfolioReplay, paper_stats, render_report

H = INTERVAL_MS["1h"]
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H)


def candle(i: int, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(T0 + i * H, o, h, lo, c, 10.0, 1000.0, 42)


def breakout_series() -> list[Candle]:
    flat = [candle(i, 100, 105, 95, 100) for i in range(10)]
    burst = [candle(10, 100, 111, 99, 110), candle(11, 110, 112, 108, 111)]
    return flat + burst


def test_replay_takes_breakout_and_pays_fees():
    replay = PortfolioReplay(entry_n=3, exit_n=3, event_blackout=False)
    res = replay.run({"AAAUSDT": breakout_series()}, trade_from_ms=T0)
    assert res.buys == 1
    assert res.fees > 0
    assert res.final_equity != res.initial_cash


def test_replay_enforces_max_open_positions():
    series = breakout_series()
    replay = PortfolioReplay(
        entry_n=3, exit_n=3, max_open_positions=1, event_blackout=False
    )
    res = replay.run(
        {"AAAUSDT": series, "BBBUSDT": series}, trade_from_ms=T0
    )
    assert res.buys == 1
    assert res.skipped_position_cap >= 1


def test_replay_gates_entries_before_window():
    series = breakout_series()
    after_everything = series[-1].open_time + H
    res = PortfolioReplay(entry_n=3, exit_n=3, event_blackout=False).run(
        {"AAAUSDT": series}, trade_from_ms=after_everything
    )
    assert res.buys == 0
    assert res.final_equity == res.initial_cash


def test_paper_stats_reconstructs_cash_from_journal(journal, tmp_path):
    journal.set_state("engine:paper_started_at", T0)
    intent = journal.record_intent(
        symbol="AAAUSDT", side="BUY", order_type="MARKET", quantity="0.1",
        price=None, source="strategy:test", nonce="n1",
    )
    coid = journal.record_order_submitting(intent)
    journal.record_order_ack(coid, "1", "FILLED")
    journal.record_fill(
        client_order_id=coid, fill_id="AAAUSDT-1", quantity="0.1", price="100",
        fee="0.01", fee_asset="USDT", executed_at="2026-07-16T00:00:00",
    )
    store = CandleStore(tmp_path / "candles")
    store.append("AAAUSDT", "1h", [candle(0, 100, 105, 95, 120)])  # mark @ 120

    stats = paper_stats(journal, store, interval="1h", initial=150.0)
    assert stats is not None
    assert stats.buys == 1 and stats.sells == 0
    # cash = 150 - 10 (cost) - 0.01 (fee); equity marks 0.1 @ 120 = 12
    assert abs(stats.equity - (150 - 10 - 0.01 + 12)) < 1e-9
    assert stats.fees == 0.01


def test_paper_stats_none_before_engine_ever_started(journal, tmp_path):
    assert paper_stats(
        journal, CandleStore(tmp_path / "c"), interval="1h", initial=150.0
    ) is None


def test_render_report_flags_band_breaches(journal, tmp_path):
    journal.set_state("engine:paper_started_at", T0)
    store = CandleStore(tmp_path / "candles")
    stats = paper_stats(journal, store, interval="1h", initial=150.0)
    replay = PortfolioReplay(entry_n=3, exit_n=3, event_blackout=False)
    res = replay.run({"AAAUSDT": breakout_series()}, trade_from_ms=T0)
    md = render_report(stats, res, interval="1h", generated="test")
    assert "Q13 bands" in md
    assert "PASS" in md or "FAIL" in md


def test_paper_equity_math_matches_gateway(tmp_path):
    """The journal-reconstructed cash must equal the paper account's balance."""
    from trader.config import RiskLimits
    from trader.execution import OrderManager
    from trader.journal import Journal
    from trader.paper import PaperGateway
    from trader.risk import RiskManager

    from test_paper import FakePrices

    prices = FakePrices({"BTCUSDT": "100"})
    paper = PaperGateway(tmp_path / "p.json", prices, initial_usdt=Decimal("150"))
    journal = Journal(tmp_path / "j.db")
    journal.set_state("engine:paper_started_at", T0)
    orders = OrderManager(journal, paper, RiskManager(journal, RiskLimits()))
    orders.submit_market(
        symbol="BTCUSDT", side="BUY", quantity="0.1", mark_price=Decimal("100"),
        equity=Decimal("150"), source="test",
    )
    store = CandleStore(tmp_path / "candles")
    store.append("BTCUSDT", "1h", [candle(0, 100, 100, 100, 100)])

    stats = paper_stats(journal, store, interval="1h", initial=150.0)
    gateway_cash = paper.account_balances()["USDT"]
    reconstructed_cash = stats.equity - 0.1 * 100  # minus the marked position
    assert abs(float(gateway_cash) - reconstructed_cash) < 1e-6
    journal.close()
