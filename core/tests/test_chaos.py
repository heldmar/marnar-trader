"""QA chaos suite (architecture §5.5, S2 exit criterion): simulate a power cut
at every order-lifecycle stage, then 'restart' (reopen the journal file cold,
run the reconciler) and require state to match the exchange exactly.

The 'crash' is real for the journal: we abandon the Journal instance and reopen
the SQLite file, exactly what a new process after a power cut sees. The
FakeGateway plays the exchange, whose state survives our crash by definition.
"""

from __future__ import annotations

from decimal import Decimal

from trader.config import RiskLimits
from trader.execution import OrderManager
from trader.journal import Journal
from trader.reconciler import Reconciler
from trader.risk import HaltState, RiskManager


def restart(tmp_path, gateway):
    """Cold start after the 'power cut': fresh journal handle + reconcile."""
    journal = Journal(tmp_path / "journal.db")
    report = Reconciler(journal, gateway).run()
    risk = RiskManager(journal, RiskLimits())
    orders = OrderManager(journal, gateway, risk)
    return journal, report, risk, orders


def start_intent(journal, nonce="c1"):
    intent = journal.record_intent(symbol="BTCUSDT", side="BUY", order_type="LIMIT",
                                   quantity="0.001", price="50000", source="chaos",
                                   nonce=nonce)
    return intent, journal.record_order_submitting(intent)


def test_crash_before_network_call(tmp_path, gateway):
    """Die after journaling SUBMITTING, before the request left the box."""
    journal = Journal(tmp_path / "journal.db")
    _, coid = start_intent(journal)
    # power cut — no close(), no exchange call

    journal2, report, _, _ = restart(tmp_path, gateway)
    assert report.clean and report.failed_pre_submit == [coid]
    assert journal2.get_order(coid)["status"] == "FAILED_PRE_SUBMIT"
    assert gateway.open_orders() == []  # matches exchange
    journal2.close()


def test_crash_after_place_before_ack(tmp_path, gateway):
    """Die between the exchange accepting the order and us journaling the ack."""
    journal = Journal(tmp_path / "journal.db")
    _, coid = start_intent(journal)
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    # power cut before record_order_ack

    journal2, report, _, _ = restart(tmp_path, gateway)
    assert report.clean and report.adopted == [coid]
    order = journal2.get_order(coid)
    exch = gateway.get_order(symbol="BTCUSDT", client_order_id=coid)
    assert order["status"] == exch["status"] == "NEW"
    assert order["exchange_order_id"] == str(exch["orderId"])
    journal2.close()


def test_crash_retry_does_not_double_place(tmp_path, gateway):
    """Replaying the same intent after the crash reuses the same client order id,
    so the exchange ends up with exactly one order."""
    journal = Journal(tmp_path / "journal.db")
    intent, coid = start_intent(journal, nonce="retry-me")
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    # power cut; on restart the strategy re-emits the *same* intent (same nonce)

    journal2, report, _, _ = restart(tmp_path, gateway)
    intent2 = journal2.record_intent(symbol="BTCUSDT", side="BUY", order_type="LIMIT",
                                     quantity="0.001", price="50000", source="chaos",
                                     nonce="retry-me")
    assert intent2 == intent
    coid2 = journal2.record_order_submitting(intent2)
    assert coid2 == coid  # deterministic id — placing again would be the SAME order
    assert len(gateway.orders) == 1
    journal2.close()


def test_crash_then_fill_while_down(tmp_path, gateway):
    """Order fills while the box is dark; restart must rebuild the position."""
    journal = Journal(tmp_path / "journal.db")
    _, coid = start_intent(journal)
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    gateway.simulate_fill(coid)  # happens while we are down

    journal2, report, _, _ = restart(tmp_path, gateway)
    assert report.clean
    assert journal2.get_order(coid)["status"] == "FILLED"
    pos = journal2.position_for("BTCUSDT")
    assert pos is not None and Decimal(pos["quantity"]) == Decimal("0.001")
    journal2.close()


def test_crash_during_drawdown_halt_keeps_halt(tmp_path, gateway):
    """A drawdown halt tripped just before the crash MUST still be in force
    after restart (D-07: manual reactivation only)."""
    journal = Journal(tmp_path / "journal.db")
    risk = RiskManager(journal, RiskLimits())
    risk.note_equity(Decimal("10000"))
    action = risk.note_equity(Decimal("7500"))
    assert action is not None and action.force_flat
    # power cut before force-flat completed

    journal2, report, risk2, _ = restart(tmp_path, gateway)
    assert report.clean
    assert risk2.state == HaltState.HALTED_DRAWDOWN
    assert not risk2.trading_allowed()
    journal2.close()
