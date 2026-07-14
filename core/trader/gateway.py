"""Exchange Gateway — the only component that talks to Binance.

One interface, three implementations planned (live / paper / backtest); Sprint 1
ships the real client pointed at the Spot **testnet**. Strategies never import
this module directly — they emit intents, and only the Order Manager (via the
Risk Manager, S2) reaches the gateway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from binance.spot import Spot
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient

log = logging.getLogger(__name__)

TESTNET_REST_URL = "https://testnet.binance.vision"
TESTNET_WS_URL = "wss://stream.testnet.binance.vision"


@dataclass(frozen=True)
class SymbolFilters:
    """The exchange filters that matter for order sizing (D-08 minNotional concern)."""

    symbol: str
    price_tick: Decimal
    lot_step: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def round_price(self, price: Decimal) -> Decimal:
        return price.quantize(self.price_tick, rounding=ROUND_DOWN)

    def round_qty(self, qty: Decimal) -> Decimal:
        return qty.quantize(self.lot_step, rounding=ROUND_DOWN)

    def min_order_qty(self, price: Decimal) -> Decimal:
        """Smallest valid quantity at *price* that clears minQty and minNotional."""
        qty = max(self.min_qty, self.min_notional / price)
        rounded = self.round_qty(qty)
        if rounded < qty:
            rounded += self.lot_step
        return rounded


def parse_filters(symbol_info: dict[str, Any]) -> SymbolFilters:
    f = {flt["filterType"]: flt for flt in symbol_info["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return SymbolFilters(
        symbol=symbol_info["symbol"],
        price_tick=Decimal(f["PRICE_FILTER"]["tickSize"]).normalize(),
        lot_step=Decimal(f["LOT_SIZE"]["stepSize"]).normalize(),
        min_qty=Decimal(f["LOT_SIZE"]["minQty"]).normalize(),
        min_notional=Decimal(notional.get("minNotional", "0")).normalize(),
    )


class ExchangeGateway(Protocol):
    """Mode-agnostic exchange interface (R-04 parity: callers can't tell the mode)."""

    def server_time(self) -> int: ...
    def account_balances(self) -> dict[str, Decimal]: ...
    def symbol_filters(self, symbol: str) -> SymbolFilters: ...
    def ticker_price(self, symbol: str) -> Decimal: ...
    def place_limit_order(
        self, *, symbol: str, side: str, quantity: str, price: str, client_order_id: str
    ) -> dict[str, Any]: ...
    def place_market_order(
        self, *, symbol: str, side: str, quantity: str, client_order_id: str
    ) -> dict[str, Any]: ...
    def cancel_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any]: ...
    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]: ...
    def get_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any] | None: ...
    def my_trades(self, *, symbol: str, order_id: int) -> list[dict[str, Any]]: ...


class BinanceGateway:
    """Real Binance Spot client. Sprint 1 always constructs it against the testnet."""

    def __init__(self, api_key: str, api_secret: str, *, testnet: bool = True):
        if not testnet:
            # Live trading is gated behind the mandatory paper period (Phase 4).
            raise NotImplementedError("live mode is not enabled before the paper gate")
        self._client = Spot(api_key=api_key, api_secret=api_secret, base_url=TESTNET_REST_URL)
        self._ws_url = TESTNET_WS_URL

    # -- REST -----------------------------------------------------------------

    def server_time(self) -> int:
        return int(self._client.time()["serverTime"])

    def account_balances(self) -> dict[str, Decimal]:
        acct = self._client.account()
        return {
            b["asset"]: Decimal(b["free"]) + Decimal(b["locked"])
            for b in acct["balances"]
            if Decimal(b["free"]) + Decimal(b["locked"]) > 0
        }

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        info = self._client.exchange_info(symbol=symbol)
        return parse_filters(info["symbols"][0])

    def ticker_price(self, symbol: str) -> Decimal:
        return Decimal(self._client.ticker_price(symbol=symbol)["price"])

    def place_limit_order(
        self, *, symbol: str, side: str, quantity: str, price: str, client_order_id: str
    ) -> dict[str, Any]:
        return self._client.new_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            timeInForce="GTC",
            quantity=quantity,
            price=price,
            newClientOrderId=client_order_id,
        )

    def place_market_order(
        self, *, symbol: str, side: str, quantity: str, client_order_id: str
    ) -> dict[str, Any]:
        return self._client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            newClientOrderId=client_order_id,
        )

    def cancel_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any]:
        return self._client.cancel_order(symbol=symbol, origClientOrderId=client_order_id)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return self._client.get_open_orders(symbol=symbol)

    def get_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        """Order lookup by our idempotent client order id; None if the exchange
        never saw it (the reconciler's it-never-left-the-building case)."""
        from binance.error import ClientError

        try:
            return self._client.get_order(symbol=symbol, origClientOrderId=client_order_id)
        except ClientError as exc:
            if exc.error_code == -2013:  # "Order does not exist."
                return None
            raise

    def my_trades(self, *, symbol: str, order_id: int) -> list[dict[str, Any]]:
        return self._client.my_trades(symbol=symbol, orderId=order_id)

    # -- WebSocket --------------------------------------------------------------

    def ws_stream_client(self, on_message) -> SpotWebsocketStreamClient:
        """Market-data stream client against the testnet stream endpoint.
        Caller subscribes (e.g. ``client.ticker(symbol)``) and must ``stop()`` it."""
        return SpotWebsocketStreamClient(stream_url=self._ws_url, on_message=on_message)
