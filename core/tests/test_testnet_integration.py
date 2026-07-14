"""Integration tests against the real Binance Spot testnet.

Skipped automatically when credentials are absent (local runs without env, forks).
In CI the keys come from GitHub Actions secrets.
"""

import os
from decimal import Decimal

import pytest

from trader.gateway import BinanceGateway
from trader.journal import Journal, new_nonce

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("BINANCE_TESTNET_API_KEY"),
        reason="no testnet credentials in environment",
    ),
]


@pytest.fixture()
def gateway():
    return BinanceGateway(
        os.environ["BINANCE_TESTNET_API_KEY"],
        os.environ["BINANCE_TESTNET_API_SECRET"],
        testnet=True,
    )


def test_rest_connectivity(gateway):
    assert gateway.server_time() > 0
    assert isinstance(gateway.account_balances(), dict)


def test_place_and_cancel_journaled(gateway, tmp_path):
    """The Sprint 1 acceptance path: order placed and cancelled, fully journaled."""
    journal = Journal(tmp_path / "journal.db")
    symbol = "BTCUSDT"
    filters = gateway.symbol_filters(symbol)
    price = filters.round_price(gateway.ticker_price(symbol) * Decimal("0.80"))
    qty = filters.min_order_qty(price)

    intent_id = journal.record_intent(
        symbol=symbol, side="BUY", order_type="LIMIT",
        quantity=str(qty), price=str(price), source="ci", nonce=new_nonce(),
    )
    coid = journal.record_order_submitting(intent_id)
    try:
        ack = gateway.place_limit_order(
            symbol=symbol, side="BUY", quantity=str(qty), price=str(price),
            client_order_id=coid,
        )
        journal.record_order_ack(coid, str(ack["orderId"]), ack["status"])
    finally:
        cancel = gateway.cancel_order(symbol=symbol, client_order_id=coid)
        journal.record_order_status(coid, cancel["status"])

    assert journal.get_order(coid)["status"] == "CANCELED"
    assert journal.open_orders() == []
    kinds = [e["kind"] for e in journal.events_for(coid)]
    assert kinds == ["ORDER_SUBMITTING", "ORDER_ACK", "ORDER_CANCELED"]
    journal.close()
