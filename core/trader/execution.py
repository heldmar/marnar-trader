"""Order Manager — turns risk-approved intents into exchange orders.

Crash-safety contract (docs/ARCHITECTURE.md §5):

- The intent and the SUBMITTING transition are committed to the journal
  *before* the network call; the ack *after*. A crash at any point leaves a
  journal state the reconciler can resolve against the exchange.
- Client order IDs are deterministic per intent, so a crash-and-retry of the
  same intent can never double-place.

Every order — including the risk manager's force-flat exits — goes through
this module and is journaled. There is no unjournaled path to the exchange.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from trader.gateway import ExchangeGateway
from trader.journal import Journal, new_nonce, utcnow
from trader.risk import HaltAction, RiskManager

log = logging.getLogger(__name__)


class OrderRejected(Exception):
    """The Risk Manager said no. Carries the human-readable reason."""


class OrderManager:
    def __init__(self, journal: Journal, gateway: ExchangeGateway, risk: RiskManager):
        self._journal = journal
        self._gateway = gateway
        self._risk = risk

    # -- order entry ---------------------------------------------------------------

    def submit_limit(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        equity: Decimal,
        source: str,
        nonce: str | None = None,
    ) -> str:
        notional = Decimal(quantity) * Decimal(price)
        decision = self._risk.evaluate_intent(
            symbol=symbol, side=side, notional=notional, equity=equity
        )
        if not decision.approved:
            self._journal.record_event("RISK_REJECTED", symbol, {"reason": decision.reason})
            raise OrderRejected(decision.reason)
        return self._place(
            symbol=symbol, side=side, order_type="LIMIT", quantity=quantity,
            price=price, source=source, nonce=nonce or new_nonce(),
        )

    def submit_market(
        self,
        *,
        symbol: str,
        side: str,
        quantity: str,
        mark_price: Decimal,
        equity: Decimal,
        source: str,
        nonce: str | None = None,
    ) -> str:
        """Risk-gated market order (S5 strategy engine entry point). *mark_price*
        is the caller's live reference price — used only for the risk notional
        check; the fill price is the exchange's business."""
        notional = Decimal(quantity) * mark_price
        decision = self._risk.evaluate_intent(
            symbol=symbol, side=side, notional=notional, equity=equity
        )
        if not decision.approved:
            self._journal.record_event("RISK_REJECTED", symbol, {"reason": decision.reason})
            raise OrderRejected(decision.reason)
        return self._place(
            symbol=symbol, side=side, order_type="MARKET", quantity=quantity,
            price=None, source=source, nonce=nonce or new_nonce(),
        )

    def _place(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None,
        source: str,
        nonce: str,
    ) -> str:
        intent_id = self._journal.record_intent(
            symbol=symbol, side=side, order_type=order_type,
            quantity=quantity, price=price, source=source, nonce=nonce,
        )
        # Committed BEFORE the network call — a crash from here on is recoverable.
        coid = self._journal.record_order_submitting(intent_id)
        if order_type == "LIMIT":
            ack = self._gateway.place_limit_order(
                symbol=symbol, side=side, quantity=quantity,
                price=price or "0", client_order_id=coid,
            )
        else:
            ack = self._gateway.place_market_order(
                symbol=symbol, side=side, quantity=quantity, client_order_id=coid
            )
        self._journal.record_order_ack(coid, str(ack["orderId"]), ack["status"])
        self._record_ack_fills(coid, symbol, ack)
        return coid

    def _record_ack_fills(self, coid: str, symbol: str, ack: dict) -> None:
        """MARKET acks (FULL response) carry their fills inline."""
        for f in ack.get("fills", []):
            self._journal.record_fill(
                client_order_id=coid,
                fill_id=f"{symbol}-{f['tradeId']}",
                quantity=f["qty"],
                price=f["price"],
                fee=f.get("commission", "0"),
                fee_asset=f.get("commissionAsset"),
                executed_at=utcnow(),
            )

    def exit_market(self, *, symbol: str, quantity: str, reason: str) -> str:
        """Protective exit (engine-held stop-loss / take-profit). Bypasses the
        intent gate on the same principle as force_flat: a restriction layer
        must never block reducing risk — but the order is journaled like any
        other, with the trigger in its source."""
        return self._place(
            symbol=symbol, side="SELL", order_type="MARKET", quantity=quantity,
            price=None, source=f"protective:{reason}", nonce=new_nonce(),
        )

    # -- cancels & halts -------------------------------------------------------------

    def cancel(self, client_order_id: str) -> str:
        order = self._journal.get_order(client_order_id)
        if order is None:
            raise KeyError(f"unknown order {client_order_id}")
        ack = self._gateway.cancel_order(
            symbol=order["symbol"], client_order_id=client_order_id
        )
        self._journal.record_order_status(client_order_id, ack["status"], {"via": "cancel"})
        return ack["status"]

    def cancel_all_open(self, reason: str) -> int:
        """Cancel every journal-open order on the exchange (halt step 1, both tiers)."""
        cancelled = 0
        for order in self._journal.open_orders():
            if order["status"] == "SUBMITTING":
                continue  # in-flight at crash time; the reconciler owns these
            try:
                ack = self._gateway.cancel_order(
                    symbol=order["symbol"], client_order_id=order["client_order_id"]
                )
                self._journal.record_order_status(
                    order["client_order_id"], ack["status"], {"via": f"halt: {reason}"}
                )
                cancelled += 1
            except Exception as exc:
                # Best-effort during a halt; the reconciler re-syncs on next start.
                log.error("cancel failed for %s: %s", order["client_order_id"], exc)
        return cancelled

    def force_flat(self, reason: str) -> list[str]:
        """Market-sell every open position (D-21: drawdown halt / kill switch with
        close). Runs while halted, so it bypasses the intent gate — but every exit
        is still journaled like any other order."""
        exited: list[str] = []
        for pos in self._journal.positions():
            symbol = pos["symbol"]
            filters = self._gateway.symbol_filters(symbol)
            qty = filters.round_qty(Decimal(pos["quantity"]))
            price = self._gateway.ticker_price(symbol)
            if qty < filters.min_qty or qty * price < filters.min_notional:
                self._journal.record_event(
                    "FORCE_FLAT_DUST", symbol,
                    {"qty": str(qty), "reason": "below exchange minimums"},
                )
                continue
            coid = self._place(
                symbol=symbol, side="SELL", order_type="MARKET", quantity=str(qty),
                price=None, source=f"risk:force_flat({reason})", nonce=new_nonce(),
            )
            exited.append(coid)
        return exited

    def apply_halt(self, action: HaltAction) -> dict:
        """Execute a HaltAction from the Risk Manager per D-21: always cancel
        pending orders; force-flat only when the action demands it."""
        cancelled = self.cancel_all_open(action.reason)
        exited = self.force_flat(action.reason) if action.force_flat else []
        self._journal.record_event(
            "HALT_EXECUTED", action.state.value,
            {"reason": action.reason, "cancelled": cancelled, "force_flat": exited},
        )
        return {"state": action.state.value, "cancelled": cancelled, "closed_positions": exited}


def halt_and_apply(risk: RiskManager, orders: OrderManager, equity: Decimal) -> dict | None:
    """Convenience used by trading loops: feed equity, execute any tripped halt."""
    action = risk.note_equity(equity)
    if action is None:
        return None
    log.warning("circuit breaker tripped: %s — %s", action.state.value, action.reason)
    return orders.apply_halt(action)
