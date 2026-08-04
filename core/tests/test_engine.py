"""PaperEngine: candle-driven strategy loop — entries, protective stops,
restart semantics, halt integration."""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_paper import FakePrices

from trader.config import RiskLimits
from trader.engine import PaperEngine
from trader.execution import OrderManager
from trader.journal import Journal
from trader.marketdata import INTERVAL_MS, CandleStore
from trader.paper import PaperGateway
from trader.risk import RiskManager

H = INTERVAL_MS["1h"]
T0 = 1_760_000_000_000 - (1_760_000_000_000 % H)  # aligned epoch base


def kline(open_time: int, o: float, h: float, lo: float, c: float) -> list:
    return [open_time, str(o), str(h), str(lo), str(c), "10", open_time + H - 1,
            "1000", 42, "0", "0", "0"]


class FakeMarket:
    """MarketDataClient over a scripted kline list."""

    def __init__(self, klines_by_symbol: dict[str, list[list]]):
        self.data = klines_by_symbol

    def klines(self, symbol, interval, *, start_ms, limit=1000):
        return [r for r in self.data.get(symbol, []) if r[0] >= start_ms][:limit]

    def exchange_info(self):  # pragma: no cover - unused
        raise NotImplementedError

    def ticker_24hr(self):  # pragma: no cover - unused
        raise NotImplementedError


@pytest.fixture
def world(tmp_path):
    """A small universe: 10 flat candles then a clean 3-bar-high breakout."""
    prices = FakePrices({"BTCUSDT": "110"})
    paper = PaperGateway(tmp_path / "paper.json", prices, initial_usdt=Decimal("150"))
    journal = Journal(tmp_path / "journal.db")
    risk = RiskManager(journal, RiskLimits())
    orders = OrderManager(journal, paper, risk)
    flat = [kline(T0 + i * H, 100, 105, 95, 100) for i in range(10)]
    breakout = kline(T0 + 10 * H, 100, 111, 99, 110)  # closes above prior highs
    market = FakeMarket({"BTCUSDT": flat + [breakout]})
    now = {"ms": T0 + 10 * H}  # breakout candle still forming

    def engine(**kw):
        return PaperEngine(
            journal=journal, gateway=paper, orders=orders, risk=risk,
            market=market, store=CandleStore(tmp_path / "candles"),
            symbols=["BTCUSDT"], interval="1h", entry_n=3, exit_n=3,
            stop_loss_pct=5.0, spend_pct=10.0, event_blackout=False,
            now_ms=lambda: now["ms"], **kw,
        )

    yield {
        "engine": engine, "paper": paper, "prices": prices, "journal": journal,
        "market": market, "now": now, "risk": risk, "orders": orders,
    }
    journal.close()


def test_warmup_does_not_trade_on_stale_candles(world):
    eng = world["engine"]()
    eng.start()
    eng.poll_once()
    assert world["journal"].positions() == []
    assert world["journal"].get_state("engine:paper_started_at") is not None


def test_breakout_candle_close_triggers_buy_with_protection(world):
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H  # breakout candle has now closed
    eng.poll_once()
    positions = world["journal"].positions()
    assert len(positions) == 1 and positions[0]["symbol"] == "BTCUSDT"
    protect = world["journal"].get_state("engine:protect:BTCUSDT")
    assert protect and Decimal(protect["stop"]) < Decimal("110")
    # journal knows the fills (paper ack carried them through OrderManager)
    assert world["journal"].fills_with_side()[0]["side"] == "BUY"


def test_candle_is_processed_once_across_restarts(world):
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    fills_before = len(world["journal"].fills_with_side())

    eng2 = world["engine"]()  # container restart: fresh strategies, same journal
    eng2.start()
    eng2.poll_once()
    assert len(world["journal"].fills_with_side()) == fills_before


def test_stop_loss_fires_from_live_price(world):
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    world["prices"].prices["BTCUSDT"] = Decimal("90")  # below the ~4.5% stop
    eng.poll_once()
    assert world["journal"].positions() == []
    assert world["journal"].get_state("engine:protect:BTCUSDT") is None
    sells = [f for f in world["journal"].fills_with_side() if f["side"] == "SELL"]
    assert len(sells) == 1
    # journaled through exit_market with the trigger recorded
    orders = world["journal"].open_orders()
    assert orders == []  # everything terminal


def test_engine_feeds_equity_to_circuit_breakers(world):
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    # crash the market: mark-to-market equity breaches the 20% drawdown floor
    world["prices"].prices["BTCUSDT"] = Decimal("1")
    eng.poll_once()
    assert world["risk"].state.value != "RUNNING"


def test_engine_stores_consumed_candles_for_parity(world, tmp_path):
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    store = CandleStore(tmp_path / "candles")
    stored = store.read("BTCUSDT", "1h")
    assert stored and stored[-1].open_time == T0 + 10 * H


def test_engine_warns_when_blackout_calendar_is_exhausted(world, caplog, tmp_path):
    """QA1-14: event_blackout=true with no future calendar entries must be loud."""
    from trader.macro_events import all_event_times_ms

    beyond_calendar = max(all_event_times_ms()) + H
    with caplog.at_level("WARNING", logger="trader.engine"):
        PaperEngine(
            journal=world["journal"], gateway=world["paper"], orders=world["orders"],
            risk=world["risk"], market=world["market"],
            store=CandleStore(tmp_path / "candles2"),
            symbols=["BTCUSDT"], interval="1h", entry_n=3, exit_n=3,
            stop_loss_pct=5.0, spend_pct=10.0, event_blackout=True,
            now_ms=lambda: beyond_calendar,
        )
    assert any("no future entries" in r.message for r in caplog.records)


def test_engine_no_warning_while_blackout_calendar_has_future_events(world, caplog, tmp_path):
    from trader.macro_events import all_event_times_ms

    within_calendar = max(all_event_times_ms()) - H
    with caplog.at_level("WARNING", logger="trader.engine"):
        PaperEngine(
            journal=world["journal"], gateway=world["paper"], orders=world["orders"],
            risk=world["risk"], market=world["market"],
            store=CandleStore(tmp_path / "candles3"),
            symbols=["BTCUSDT"], interval="1h", entry_n=3, exit_n=3,
            stop_loss_pct=5.0, spend_pct=10.0, event_blackout=True,
            now_ms=lambda: within_calendar,
        )
    assert not any("no future entries" in r.message for r in caplog.records)


# -- per-symbol isolation ---------------------------------------------------------


class BrokenForSymbol(FakeMarket):
    """A market where exactly one pair's klines endpoint is down — the ordinary
    case of a delist, a regional 451, or a transient 5xx."""

    def __init__(self, data, broken: str):
        super().__init__(data)
        self.broken = broken
        self.calls = 0

    def klines(self, symbol, interval, *, start_ms, limit=1000):
        self.calls += 1
        if symbol == self.broken:
            raise RuntimeError(f"{symbol} unavailable")
        return super().klines(symbol, interval, start_ms=start_ms, limit=limit)


@pytest.fixture
def two_symbol_world(world, tmp_path):
    """The same breakout on BTCUSDT, plus an ETHUSDT that is broken."""
    data = dict(world["market"].data)
    data["ETHUSDT"] = list(data["BTCUSDT"])
    world["prices"].prices["ETHUSDT"] = "110"
    market = BrokenForSymbol(data, broken="ETHUSDT")

    def engine(**kw):
        eng = world["engine"](**kw)
        eng.market = market
        eng.symbols = ["BTCUSDT", "ETHUSDT"]
        eng.strategies["ETHUSDT"] = eng.strategies["BTCUSDT"].__class__(
            entry_n=3, exit_n=3, stop_loss_pct=5.0
        )
        return eng

    return {**world, "engine2": engine, "market2": market}


def test_one_broken_symbol_does_not_stop_the_engine_starting(two_symbol_world):
    """Before this, a single failing pair aborted warmup, so the engine never
    started and *nothing* traded — a one-symbol outage became a total one."""
    eng = two_symbol_world["engine2"]()
    eng.start()
    assert eng._started
    assert eng._unwarmed == {"ETHUSDT"}


def test_the_healthy_symbol_still_trades_when_another_is_broken(two_symbol_world):
    eng = two_symbol_world["engine2"]()
    eng.start()
    two_symbol_world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    symbols = [p["symbol"] for p in two_symbol_world["journal"].positions()]
    assert symbols == ["BTCUSDT"]


def test_an_unwarmed_symbol_is_never_traded(two_symbol_world):
    """Its Donchian channel was primed with nothing, so any signal it produced
    would be an artefact. It must sit out until warmup succeeds."""
    eng = two_symbol_world["engine2"]()
    eng.start()
    two_symbol_world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    assert "ETHUSDT" not in {p["symbol"] for p in two_symbol_world["journal"].positions()}
    assert "ETHUSDT" in eng._unwarmed


def test_a_recovered_symbol_rejoins_on_a_later_poll(two_symbol_world):
    eng = two_symbol_world["engine2"]()
    eng.start()
    assert eng._unwarmed == {"ETHUSDT"}
    two_symbol_world["market2"].broken = None  # the outage clears
    two_symbol_world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()
    assert eng._unwarmed == set()
    assert two_symbol_world["journal"].events_of_kind(["WARMUP_RECOVERED"])


def test_the_circuit_breaker_still_runs_when_a_symbol_fails(two_symbol_world):
    """The worst failure mode of the old loop: one raising symbol skipped the
    equity snapshot and the halt check for the whole cycle."""
    eng = two_symbol_world["engine2"]()
    eng.start()
    eng._unwarmed = set()  # force ETHUSDT to be polled, and to raise
    two_symbol_world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()  # must not raise
    assert two_symbol_world["journal"].get_state("engine:last_equity_snapshot")
    assert two_symbol_world["journal"].events_of_kind(["POLL_SYMBOL_FAILURES"])


def test_total_warmup_failure_is_still_fatal(world, tmp_path):
    """One bad symbol is isolated; every symbol failing is a real outage and
    must keep surfacing as a warmup failure the run loop retries and alerts."""
    eng = world["engine"]()
    eng.market = BrokenForSymbol(dict(world["market"].data), broken="BTCUSDT")
    with pytest.raises(RuntimeError, match="all 1 symbols"):
        eng.start()
    assert not eng._started


# -- hot universe swap (D-09 rotation) --------------------------------------------


def test_set_symbols_adds_new_pairs_unwarmed(world):
    """A rotated-in pair must not trade on an unprimed channel — it enters
    unwarmed and the next poll primes it."""
    eng = world["engine"]()
    eng.start()
    change = eng.set_symbols(["BTCUSDT", "ETHUSDT"])
    assert change["added"] == ["ETHUSDT"]
    assert "ETHUSDT" in eng._unwarmed
    assert "ETHUSDT" in eng.strategies


def test_set_symbols_drops_pairs_we_do_not_hold(world):
    eng = world["engine"]()
    eng.start()
    change = eng.set_symbols(["ETHUSDT"])
    assert change["removed"] == ["BTCUSDT"]
    assert "BTCUSDT" not in eng.strategies
    assert world["journal"].get_state("engine:protect:BTCUSDT") is None


def test_set_symbols_keeps_a_pair_we_still_hold(world):
    """Dropping it would strand the position: no exit rule, no protective stop,
    nothing left to sell it."""
    eng = world["engine"]()
    eng.start()
    world["now"]["ms"] = T0 + 11 * H
    eng.poll_once()  # opens BTCUSDT
    assert world["journal"].positions()

    change = eng.set_symbols(["ETHUSDT"])
    assert change["kept_for_open_position"] == ["BTCUSDT"]
    assert "BTCUSDT" in eng.symbols
    assert "BTCUSDT" in eng.strategies
    assert world["journal"].get_state("engine:protect:BTCUSDT") is not None


def test_set_symbols_is_idempotent(world):
    eng = world["engine"]()
    eng.start()
    change = eng.set_symbols(["BTCUSDT", "BTCUSDT"])
    assert eng.symbols == ["BTCUSDT"]
    assert change == {"added": [], "removed": [], "kept_for_open_position": []}
