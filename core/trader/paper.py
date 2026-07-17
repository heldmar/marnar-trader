"""Paper exchange gateway (Sprint 5) — the `paper` implementation of the
ExchangeGateway protocol (arch: live / paper / backtest behind one interface).

Real prices, fake money: market data comes from Binance **production** public
endpoints (same source as the S3 downloader — real book, real volume, no keys),
while orders fill against a simulated account. Callers cannot tell the mode
(R-04); the OrderManager, RiskManager, Reconciler and journal all run unchanged.

Fill model — deliberately identical to the backtester's, so the Q13
paper-vs-backtest parity check compares like with like:
- market orders fill immediately at the live ticker price, moved against us by
  ``slippage_bps``;
- limit orders rest and fill (fully) when the ticker crosses them — checked on
  every ``poll()``;
- fees are ``fee_rate`` of notional per side, charged in USDT
  (``commissionAsset: USDT``) exactly as the backtest models them.

Crash-safety: account state (balances, orders, trade counter) lives in a JSON
file on the data volume, replaced atomically (tmp + rename) *before* an ack is
returned — the paper exchange never acknowledges anything it hasn't persisted.
A crash mid-call therefore looks exactly like a live-exchange crash and the S2
reconciler resolves it the same way (SUBMITTING + not-found → FAILED_PRE_SUBMIT;
SUBMITTING + found → adopt).
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Protocol

from trader.fsutil import atomic_write_text
from trader.gateway import SymbolFilters, parse_filters

log = logging.getLogger(__name__)


class PriceSource(Protocol):
    """The slice of live market data the paper exchange needs."""

    def ticker_price(self, symbol: str) -> Decimal: ...
    def symbol_info(self, symbol: str) -> dict[str, Any]: ...
    def server_time(self) -> int: ...


class BinancePublicPrices:
    """Production public endpoints — read-only, unauthenticated (same trust
    boundary as trader.marketdata: no credential ever touches paper mode)."""

    def __init__(self) -> None:
        from binance.spot import Spot

        from trader.marketdata import PROD_DATA_URL

        # timeout: a hung call must not freeze the shared event loop (QA1-09)
        self._client = Spot(base_url=PROD_DATA_URL, timeout=10)

    def ticker_price(self, symbol: str) -> Decimal:
        return Decimal(self._client.ticker_price(symbol=symbol)["price"])

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        return self._client.exchange_info(symbol=symbol)["symbols"][0]

    def server_time(self) -> int:
        return int(self._client.time()["serverTime"])


class PaperGateway:
    """Simulated Binance Spot account over live production prices."""

    def __init__(
        self,
        state_path: Path | str,
        prices: PriceSource,
        *,
        initial_usdt: Decimal = Decimal("150"),  # D-05 pilot capital
        fee_rate: Decimal = Decimal("0.001"),
        slippage_bps: Decimal = Decimal("5"),
    ):
        self._path = Path(state_path)
        self._prices = prices
        self._fee_rate = fee_rate
        self._slip = slippage_bps / Decimal(10_000)
        self._filters: dict[str, SymbolFilters] = {}
        if self._path.exists():
            try:
                self._state = json.loads(self._path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                # QA1-11: a corrupt state file must refuse to trade with a clear
                # message, not crash-loop the container on a cryptic parse error.
                raise RuntimeError(
                    f"paper account state file {self._path} is corrupt ({exc}); "
                    "refusing to trade — restore it from backup or delete it to "
                    "start a fresh paper account (resets the run)"
                ) from exc
        else:
            self._state = {
                "balances": {"USDT": str(initial_usdt)},
                "orders": {},  # client_order_id -> order dict (Binance-shaped)
                "next_order_id": 1,
                "next_trade_id": 1,
            }
            self._persist()

    # -- persistence -----------------------------------------------------------

    def _persist(self) -> None:
        # QA1-11: fsync'd atomic write — a power cut mid-persist leaves the old
        # state, never a truncated file (the no-UPS discipline).
        atomic_write_text(self._path, json.dumps(self._state, indent=1))

    # -- balances ---------------------------------------------------------------

    def _balance(self, asset: str) -> Decimal:
        return Decimal(self._state["balances"].get(asset, "0"))

    def _set_balance(self, asset: str, value: Decimal) -> None:
        if value < 0:
            raise ValueError(f"paper balance for {asset} would go negative: {value}")
        self._state["balances"][asset] = str(value)

    @staticmethod
    def _base_asset(symbol: str) -> str:
        if not symbol.endswith("USDT"):  # D-10: USDT-quoted universe only
            raise ValueError(f"paper gateway only supports USDT pairs, got {symbol}")
        return symbol[: -len("USDT")]

    # -- ExchangeGateway protocol -------------------------------------------------

    def server_time(self) -> int:
        return self._prices.server_time()

    def account_balances(self) -> dict[str, Decimal]:
        return {a: Decimal(v) for a, v in self._state["balances"].items() if Decimal(v) > 0}

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        if symbol not in self._filters:
            self._filters[symbol] = parse_filters(self._prices.symbol_info(symbol))
        return self._filters[symbol]

    def ticker_price(self, symbol: str) -> Decimal:
        return self._prices.ticker_price(symbol)

    def place_market_order(
        self, *, symbol: str, side: str, quantity: str, client_order_id: str
    ) -> dict[str, Any]:
        if client_order_id in self._state["orders"]:
            return self._state["orders"][client_order_id]  # idempotent crash-retry
        mark = self._prices.ticker_price(symbol)
        price = mark * (1 + self._slip) if side == "BUY" else mark * (1 - self._slip)
        filters = self.symbol_filters(symbol)
        price = price.quantize(filters.price_tick, rounding=ROUND_DOWN)
        # QA1-12: verify funds at the fill price BEFORE the order exists in
        # state — a failed market order must leave no phantom NEW order behind.
        qty = Decimal(quantity)
        if side == "BUY":
            need = qty * price * (1 + self._fee_rate)
            if self._balance("USDT") < need:
                raise ValueError(f"insufficient paper USDT for {symbol} buy (need {need})")
        elif self._balance(self._base_asset(symbol)) < qty:
            raise ValueError(f"insufficient paper balance to sell {qty} {symbol}")
        order = self._new_order(
            symbol=symbol, side=side, order_type="MARKET", quantity=quantity,
            price=None, client_order_id=client_order_id,
        )
        try:
            self._fill(order, price)
        except Exception:
            # Belt and braces: never leave a half-made order in state.
            self._state["orders"].pop(client_order_id, None)
            raise
        self._persist()
        return order

    def place_limit_order(
        self, *, symbol: str, side: str, quantity: str, price: str, client_order_id: str
    ) -> dict[str, Any]:
        if client_order_id in self._state["orders"]:
            return self._state["orders"][client_order_id]
        order = self._new_order(
            symbol=symbol, side=side, order_type="LIMIT", quantity=quantity,
            price=price, client_order_id=client_order_id,
        )
        # A marketable limit fills immediately at its limit price (price
        # improvement is not modeled — pessimistic, like the backtester).
        mark = self._prices.ticker_price(symbol)
        limit = Decimal(price)
        if (order["side"] == "BUY" and mark <= limit) or (
            order["side"] == "SELL" and mark >= limit
        ):
            self._fill(order, limit)
        self._persist()
        return order

    def cancel_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any]:
        order = self._state["orders"].get(client_order_id)
        if order is None or order["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise KeyError(f"no open paper order {client_order_id}")
        order["status"] = "CANCELED"
        self._release_lock(order)
        self._persist()
        return order

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [
            o for o in self._state["orders"].values()
            if o["status"] in ("NEW", "PARTIALLY_FILLED")
            and (symbol is None or o["symbol"] == symbol)
        ]

    def get_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        order = self._state["orders"].get(client_order_id)
        if order is None or order["symbol"] != symbol:
            return None
        return order

    def my_trades(self, *, symbol: str, order_id: int) -> list[dict[str, Any]]:
        out = []
        for o in self._state["orders"].values():
            if o["symbol"] != symbol or int(o["orderId"]) != order_id:
                continue
            for f in o.get("fills", []):
                out.append(
                    {
                        "id": f["tradeId"],
                        "orderId": order_id,
                        "symbol": symbol,
                        "qty": f["qty"],
                        "price": f["price"],
                        "commission": f["commission"],
                        "commissionAsset": f["commissionAsset"],
                        "time": f.get("time", 0),
                    }
                )
        return out

    # -- paper-only surface ---------------------------------------------------------

    def poll(self) -> list[dict[str, Any]]:
        """Check resting limit orders against the live price; fill the crossed
        ones. Returns the orders that filled this poll (engine journals them)."""
        filled = []
        for order in list(self._state["orders"].values()):
            if order["status"] != "NEW" or order["type"] != "LIMIT":
                continue
            mark = self._prices.ticker_price(order["symbol"])
            limit = Decimal(order["price"])
            if (order["side"] == "BUY" and mark <= limit) or (
                order["side"] == "SELL" and mark >= limit
            ):
                self._fill(order, limit)
                filled.append(order)
        if filled:
            self._persist()
        return filled

    def equity_usdt(self) -> Decimal:
        """Mark-to-market equity in USDT at live prices (risk-manager input)."""
        total = self._balance("USDT")
        for asset, value in self._state["balances"].items():
            qty = Decimal(value)
            if asset == "USDT" or qty == 0:
                continue
            total += qty * self._prices.ticker_price(f"{asset}USDT")
        return total

    # -- internals -------------------------------------------------------------------

    def _new_order(
        self, *, symbol: str, side: str, order_type: str, quantity: str,
        price: str | None, client_order_id: str,
    ) -> dict[str, Any]:
        oid = self._state["next_order_id"]
        self._state["next_order_id"] = oid + 1
        order = {
            "symbol": symbol,
            "orderId": oid,
            "clientOrderId": client_order_id,
            "side": side,
            "type": order_type,
            "origQty": quantity,
            "executedQty": "0",
            "price": price or "0",
            "status": "NEW",
            "time": self._prices.server_time(),
            "fills": [],
        }
        if order_type == "LIMIT":
            self._take_lock(order)  # funds check up front, like a real exchange
        self._state["orders"][client_order_id] = order
        return order

    def _take_lock(self, order: dict[str, Any]) -> None:
        """Verify (not escrow) funds at placement; simple model, no `locked`
        bookkeeping — the risk layer already caps exposure well below balance."""
        qty = Decimal(order["origQty"])
        if order["side"] == "SELL":
            if self._balance(self._base_asset(order["symbol"])) < qty:
                raise ValueError(f"insufficient paper balance to sell {qty} {order['symbol']}")
        else:
            need = qty * Decimal(order["price"]) * (1 + self._fee_rate)
            if self._balance("USDT") < need:
                raise ValueError(f"insufficient paper USDT for {order['symbol']} buy")

    def _release_lock(self, order: dict[str, Any]) -> None:
        return None  # no escrow taken (see _take_lock)

    def _fill(self, order: dict[str, Any], price: Decimal) -> None:
        symbol, side = order["symbol"], order["side"]
        base = self._base_asset(symbol)
        qty = Decimal(order["origQty"])
        notional = qty * price
        fee = notional * self._fee_rate
        if side == "BUY":
            self._set_balance("USDT", self._balance("USDT") - notional - fee)
            self._set_balance(base, self._balance(base) + qty)
        else:
            if self._balance(base) < qty:
                raise ValueError(f"insufficient paper {base} to sell {qty}")
            self._set_balance(base, self._balance(base) - qty)
            self._set_balance("USDT", self._balance("USDT") + notional - fee)
        tid = self._state["next_trade_id"]
        self._state["next_trade_id"] = tid + 1
        order["fills"].append(
            {
                "tradeId": tid,
                "qty": str(qty),
                "price": str(price),
                "commission": str(fee),
                "commissionAsset": "USDT",
                "time": self._prices.server_time(),
            }
        )
        order["executedQty"] = str(qty)
        order["status"] = "FILLED"
        log.info("paper fill: %s %s %s @ %s (fee %s USDT)", side, qty, symbol, price, fee)
