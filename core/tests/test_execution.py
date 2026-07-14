"""Order Manager tests: journal-before-call ordering, risk gate wiring,
cancel-all and force-flat halt execution (D-21 tiers)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trader.execution import OrderRejected, halt_and_apply
from trader.risk import HaltState

EQUITY = Decimal("10000")


def buy(orders, qty="0.001", price="50000", nonce="n1"):
    return orders.submit_limit(
        symbol="BTCUSDT", side="BUY", quantity=qty, price=price,
        equity=EQUITY, source="test", nonce=nonce,
    )


def open_position(orders, gateway, qty="0.01", price="50000", nonce="pos1"):
    """BUY limit, then the exchange fills it and the journal ingests the fill."""
    coid = buy(orders, qty=qty, price=price, nonce=nonce)
    gateway.simulate_fill(coid)
    order = gateway.orders[coid]
    trade = gateway.trades[order["orderId"]][0]
    orders._journal.record_fill(
        client_order_id=coid, fill_id=f"BTCUSDT-{trade['id']}", quantity=trade["qty"],
        price=trade["price"], fee="0", fee_asset="USDT", executed_at="t",
    )
    orders._journal.record_order_status(coid, "FILLED")
    return coid


def test_submit_journals_before_and_after_network_call(journal, gateway, orders):
    coid = buy(orders)
    kinds = [e["kind"] for e in journal.events_for(coid)]
    assert kinds == ["ORDER_SUBMITTING", "ORDER_ACK"]
    assert journal.get_order(coid)["status"] == "NEW"
    assert gateway.orders[coid]["clientOrderId"] == coid


def test_rejected_intent_never_reaches_exchange(journal, gateway, orders, risk):
    risk.halt_manual("halted", close_positions=False)
    with pytest.raises(OrderRejected, match="halted"):
        buy(orders)
    assert gateway.orders == {}
    assert journal._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    events = journal._conn.execute(
        "SELECT kind FROM events WHERE kind='RISK_REJECTED'"
    ).fetchall()
    assert len(events) == 1


def test_market_fill_updates_position_and_cancel_roundtrip(journal, gateway, orders):
    open_position(orders, gateway)
    pos = journal.position_for("BTCUSDT")
    assert pos is not None and Decimal(pos["quantity"]) == Decimal("0.01")

    coid = buy(orders, nonce="n2")
    assert orders.cancel(coid) == "CANCELED"
    assert journal.get_order(coid)["status"] == "CANCELED"


def test_cancel_unknown_order_raises(orders):
    with pytest.raises(KeyError):
        orders.cancel("mnt-nope")


def test_cancel_all_open_skips_submitting_and_survives_errors(journal, gateway, orders):
    coid1 = buy(orders, nonce="n1")
    buy(orders, nonce="n2")
    # a third order stuck in SUBMITTING (crash mid-flight) must be left alone
    intent = journal.record_intent(symbol="BTCUSDT", side="BUY", order_type="LIMIT",
                                   quantity="0.001", price="1", source="t", nonce="n3")
    stuck = journal.record_order_submitting(intent)
    # and one whose exchange cancel blows up
    del gateway.orders[coid1]

    cancelled = orders.cancel_all_open("test halt")
    assert cancelled == 1  # n2 only: n1 errored, n3 skipped
    assert journal.get_order(stuck)["status"] == "SUBMITTING"


def test_force_flat_market_sells_positions_and_skips_dust(journal, gateway, orders):
    open_position(orders, gateway, qty="0.01")
    # dust position in another symbol: below minNotional at current price
    gateway.prices["DUSTUSDT"] = Decimal("1")
    gateway.filters["DUSTUSDT"] = gateway.filters["BTCUSDT"].__class__(
        symbol="DUSTUSDT", price_tick=Decimal("0.01"), lot_step=Decimal("0.01"),
        min_qty=Decimal("0.01"), min_notional=Decimal("5"),
    )
    intent = journal.record_intent(symbol="DUSTUSDT", side="BUY", order_type="LIMIT",
                                   quantity="1", price="1", source="t", nonce="d1")
    coid = journal.record_order_submitting(intent)
    journal.record_order_ack(coid, "9", "FILLED")
    journal.record_fill(client_order_id=coid, fill_id="DUSTUSDT-1", quantity="1",
                        price="1", fee="0", fee_asset="USDT", executed_at="t")
    journal.record_order_status(coid, "FILLED")

    exits = orders.force_flat("drawdown")
    assert len(exits) == 1  # BTC sold, dust skipped
    assert journal.position_for("BTCUSDT") is None
    assert journal.position_for("DUSTUSDT") is not None  # held, flagged as dust
    dust_events = journal._conn.execute(
        "SELECT kind FROM events WHERE kind='FORCE_FLAT_DUST'"
    ).fetchall()
    assert len(dust_events) == 1
    # the exit realized P&L (price unchanged → 0)
    assert journal.realized_pnl_since("2000-01-01") == Decimal("0.0")


def test_halt_and_apply_daily_loss_holds_positions(journal, gateway, orders, risk):
    open_position(orders, gateway)
    pending = buy(orders, nonce="pend")
    risk.note_equity(EQUITY)
    result = halt_and_apply(risk, orders, Decimal("9700"))  # -3% day
    assert result["state"] == HaltState.HALTED_DAILY_LOSS.value
    assert result["cancelled"] == 1 and result["closed_positions"] == []
    assert journal.get_order(pending)["status"] == "CANCELED"
    assert journal.position_for("BTCUSDT") is not None  # D-21: HOLD


def test_halt_and_apply_drawdown_force_flats(journal, gateway, orders, risk):
    open_position(orders, gateway)
    risk.note_equity(EQUITY)
    result = halt_and_apply(risk, orders, Decimal("7900"))  # -21% from peak
    assert result["state"] == HaltState.HALTED_DRAWDOWN.value
    assert len(result["closed_positions"]) == 1
    assert journal.position_for("BTCUSDT") is None  # D-21: FORCE-FLAT


def test_halt_and_apply_no_trip_returns_none(orders, risk):
    risk.note_equity(EQUITY)
    assert halt_and_apply(risk, orders, Decimal("9990")) is None
