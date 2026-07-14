"""Startup reconciliation — the no-UPS answer (R-08, docs/ARCHITECTURE.md §5.4).

On every startup, before trading is allowed:

1. Every journal order stuck in SUBMITTING (crash mid-placement) is looked up
   on the exchange by its deterministic client order id:
   - found  → the order DID go out before the crash: adopt it (ack + status).
   - absent → it never reached the exchange: mark FAILED_PRE_SUBMIT. The
     deterministic id means a retry of the same intent is still safe.
2. Every journal-open order (SUBMITTED/NEW/PARTIALLY_FILLED) is re-queried and
   synced: statuses updated, fills fetched from myTrades and applied (position
   and P&L bookkeeping ride along in record_fill, idempotently).
3. Exchange open orders carrying our "mnt-" prefix that the journal does not
   know are orphans (journal lost/rolled back?) — cancelled, loudly.
4. Exchange open orders WITHOUT our prefix (e.g. placed by hand on the web UI)
   are reported but left alone — they are not ours to cancel.

The report says whether the system is clean to trade; main.py blocks trading
until it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from trader.gateway import ExchangeGateway
from trader.journal import Journal

log = logging.getLogger(__name__)

OURS_PREFIX = "mnt-"
# Terminal states an exchange lookup can report.
_TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


@dataclass
class ReconciliationReport:
    adopted: list[str] = field(default_factory=list)        # SUBMITTING → found on exchange
    failed_pre_submit: list[str] = field(default_factory=list)  # SUBMITTING → never sent
    synced: list[str] = field(default_factory=list)         # status/fills refreshed
    orphans_cancelled: list[str] = field(default_factory=list)  # ours on exchange, not in journal
    foreign_open_orders: list[str] = field(default_factory=list)  # not ours; reported only
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.errors

    def summary(self) -> dict:
        return {
            "clean": self.clean,
            "adopted": self.adopted,
            "failed_pre_submit": self.failed_pre_submit,
            "synced": self.synced,
            "orphans_cancelled": self.orphans_cancelled,
            "foreign_open_orders": self.foreign_open_orders,
            "errors": self.errors,
        }


class Reconciler:
    def __init__(self, journal: Journal, gateway: ExchangeGateway):
        self._journal = journal
        self._gateway = gateway

    def run(self) -> ReconciliationReport:
        report = ReconciliationReport()
        self._journal.record_event("RECONCILE_START", "reconciler")

        for order in self._journal.open_orders():
            coid, symbol = order["client_order_id"], order["symbol"]
            try:
                exch = self._gateway.get_order(symbol=symbol, client_order_id=coid)
            except Exception as exc:
                report.errors.append(f"{coid}: exchange lookup failed: {exc}")
                continue

            if order["status"] == "SUBMITTING":
                if exch is None:
                    # Crash happened before the request left: safe to fail it.
                    self._journal.record_order_status(
                        coid, "FAILED_PRE_SUBMIT", {"via": "reconciler"}
                    )
                    report.failed_pre_submit.append(coid)
                    continue
                # It DID reach the exchange before the crash — adopt it.
                self._journal.record_order_ack(coid, str(exch["orderId"]), exch["status"])
                report.adopted.append(coid)
                self._sync_fills(coid, symbol, exch)
                continue

            if exch is None:
                report.errors.append(
                    f"{coid}: journal says {order['status']} but exchange has no such order"
                )
                continue
            self._sync_fills(coid, symbol, exch)
            if exch["status"] != order["status"]:
                self._journal.record_order_status(coid, exch["status"], {"via": "reconciler"})
            report.synced.append(coid)

        self._handle_exchange_open_orders(report)

        self._journal.record_event("RECONCILE_DONE", "reconciler", report.summary())
        if report.clean:
            log.info("reconciliation clean: %s", report.summary())
        else:
            log.error("reconciliation NOT clean: %s", report.summary())
        return report

    def _sync_fills(self, coid: str, symbol: str, exch: dict) -> None:
        """Pull executions for an order and journal any we missed (idempotent)."""
        if float(exch.get("executedQty", 0) or 0) == 0:
            return
        for trade in self._gateway.my_trades(symbol=symbol, order_id=int(exch["orderId"])):
            self._journal.record_fill(
                client_order_id=coid,
                fill_id=f"{symbol}-{trade['id']}",
                quantity=trade["qty"],
                price=trade["price"],
                fee=trade.get("commission", "0"),
                fee_asset=trade.get("commissionAsset"),
                executed_at=str(trade.get("time", "")),
            )

    def _handle_exchange_open_orders(self, report: ReconciliationReport) -> None:
        try:
            open_on_exchange = self._gateway.open_orders()
        except Exception as exc:
            report.errors.append(f"open-orders listing failed: {exc}")
            return
        for exch in open_on_exchange:
            coid = exch.get("clientOrderId", "")
            if not coid.startswith(OURS_PREFIX):
                report.foreign_open_orders.append(f"{exch['symbol']}:{coid}")
                continue
            if self._journal.get_order(coid) is not None:
                continue  # known and already synced above
            # Ours by prefix but unknown to the journal — should not happen with
            # journal-before-call discipline. Cancel it and say so loudly.
            try:
                self._gateway.cancel_order(symbol=exch["symbol"], client_order_id=coid)
                self._journal.record_event(
                    "ORPHAN_CANCELLED", coid, {"symbol": exch["symbol"]}
                )
                report.orphans_cancelled.append(coid)
            except Exception as exc:
                report.errors.append(f"{coid}: orphan cancel failed: {exc}")
