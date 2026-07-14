"""Shared fixtures: an in-memory fake exchange with Binance-shaped responses.

The FakeGateway lets QA simulate every crash point and exchange-side event
(fill while we were down, orphan orders, foreign orders) without the network.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any

import pytest

from trader.config import RiskLimits
from trader.execution import OrderManager
from trader.gateway import SymbolFilters
from trader.journal import Journal
from trader.risk import RiskManager

DEFAULT_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    price_tick=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("5"),
)


class FakeGateway:
    """Binance-shaped in-memory exchange (same dict shapes as the REST API)."""

    def __init__(self):
        self._ids = itertools.count(1000)
        self._trade_ids = itertools.count(5000)
        self.orders: dict[str, dict[str, Any]] = {}  # by clientOrderId
        self.trades: dict[int, list[dict[str, Any]]] = {}  # by orderId
        self.prices: dict[str, Decimal] = {"BTCUSDT": Decimal("50000")}
        self.filters: dict[str, SymbolFilters] = {"BTCUSDT": DEFAULT_FILTERS}

    # -- ExchangeGateway interface ------------------------------------------------

    def server_time(self) -> int:
        return 1_760_000_000_000

    def account_balances(self) -> dict[str, Decimal]:
        return {"USDT": Decimal("10000")}

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        return self.filters[symbol]

    def ticker_price(self, symbol: str) -> Decimal:
        return self.prices[symbol]

    def place_limit_order(self, *, symbol, side, quantity, price, client_order_id):
        order = {
            "orderId": next(self._ids), "clientOrderId": client_order_id,
            "symbol": symbol, "side": side, "type": "LIMIT", "status": "NEW",
            "origQty": quantity, "executedQty": "0", "price": price,
        }
        self.orders[client_order_id] = order
        return dict(order)

    def place_market_order(self, *, symbol, side, quantity, client_order_id):
        price = self.prices[symbol]
        order = {
            "orderId": next(self._ids), "clientOrderId": client_order_id,
            "symbol": symbol, "side": side, "type": "MARKET", "status": "FILLED",
            "origQty": quantity, "executedQty": quantity, "price": "0",
        }
        self.orders[client_order_id] = order
        fill = {"tradeId": next(self._trade_ids), "qty": quantity, "price": str(price),
                "commission": "0", "commissionAsset": "USDT"}
        self.trades[order["orderId"]] = [
            {"id": fill["tradeId"], "qty": quantity, "price": str(price),
             "commission": "0", "commissionAsset": "USDT", "time": 0}
        ]
        return dict(order) | {"fills": [fill]}

    def cancel_order(self, *, symbol, client_order_id):
        order = self.orders[client_order_id]
        order["status"] = "CANCELED"
        return dict(order)

    def open_orders(self, symbol: str | None = None):
        return [dict(o) for o in self.orders.values()
                if o["status"] in ("NEW", "PARTIALLY_FILLED")]

    def get_order(self, *, symbol, client_order_id):
        order = self.orders.get(client_order_id)
        return dict(order) if order else None

    def my_trades(self, *, symbol, order_id):
        return list(self.trades.get(order_id, []))

    # -- test helpers ---------------------------------------------------------------

    def simulate_fill(self, client_order_id: str) -> None:
        """Exchange fills a resting limit order (e.g. while we were 'down')."""
        order = self.orders[client_order_id]
        order["status"] = "FILLED"
        order["executedQty"] = order["origQty"]
        self.trades[order["orderId"]] = [
            {"id": next(self._trade_ids), "qty": order["origQty"], "price": order["price"],
             "commission": "0", "commissionAsset": "USDT", "time": 0}
        ]

    def inject_order(self, *, symbol, client_order_id, side="BUY", status="NEW",
                     quantity="0.001", price="50000") -> None:
        """Plant an exchange-side order the journal knows nothing about."""
        self.orders[client_order_id] = {
            "orderId": next(self._ids), "clientOrderId": client_order_id,
            "symbol": symbol, "side": side, "type": "LIMIT", "status": status,
            "origQty": quantity, "executedQty": "0", "price": price,
        }


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def limits():
    return RiskLimits()


@pytest.fixture
def risk(journal, limits):
    return RiskManager(journal, limits)


@pytest.fixture
def orders(journal, gateway, risk):
    return OrderManager(journal, gateway, risk)
