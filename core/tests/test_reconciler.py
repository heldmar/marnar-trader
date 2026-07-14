"""Reconciler tests — every crash/mismatch scenario from architecture §5.4 (R-08)."""

from __future__ import annotations

from decimal import Decimal

from trader.reconciler import Reconciler


def journal_submitting(journal, nonce="r1", symbol="BTCUSDT", qty="0.001", price="50000"):
    intent = journal.record_intent(symbol=symbol, side="BUY", order_type="LIMIT",
                                   quantity=qty, price=price, source="test", nonce=nonce)
    return journal.record_order_submitting(intent)


def test_submitting_never_sent_is_failed(journal, gateway):
    coid = journal_submitting(journal)
    report = Reconciler(journal, gateway).run()
    assert report.clean
    assert report.failed_pre_submit == [coid]
    assert journal.get_order(coid)["status"] == "FAILED_PRE_SUBMIT"
    assert journal.open_orders() == []


def test_submitting_that_reached_exchange_is_adopted(journal, gateway):
    coid = journal_submitting(journal)
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    report = Reconciler(journal, gateway).run()
    assert report.clean and report.adopted == [coid]
    order = journal.get_order(coid)
    assert order["status"] == "NEW" and order["exchange_order_id"] is not None


def test_order_filled_while_down_syncs_fills_and_position(journal, gateway):
    coid = journal_submitting(journal)
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    journal.record_order_ack(coid, str(gateway.orders[coid]["orderId"]), "NEW")
    gateway.simulate_fill(coid)  # exchange fills it while we are "down"

    report = Reconciler(journal, gateway).run()
    assert report.clean and report.synced == [coid]
    assert journal.get_order(coid)["status"] == "FILLED"
    pos = journal.position_for("BTCUSDT")
    assert pos is not None and Decimal(pos["quantity"]) == Decimal("0.001")


def test_fill_sync_is_idempotent_across_runs(journal, gateway):
    coid = journal_submitting(journal)
    gateway.place_limit_order(symbol="BTCUSDT", side="BUY", quantity="0.001",
                              price="50000", client_order_id=coid)
    journal.record_order_ack(coid, str(gateway.orders[coid]["orderId"]), "NEW")
    gateway.simulate_fill(coid)
    Reconciler(journal, gateway).run()
    Reconciler(journal, gateway).run()  # second run must not double-apply
    pos = journal.position_for("BTCUSDT")
    assert Decimal(pos["quantity"]) == Decimal("0.001")
    assert journal._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_journal_open_but_exchange_unknown_is_dirty(journal, gateway):
    coid = journal_submitting(journal)
    journal.record_order_ack(coid, "424242", "NEW")  # acked, but exchange lost it
    report = Reconciler(journal, gateway).run()
    assert not report.clean
    assert any("no such order" in e for e in report.errors)


def test_orphan_with_our_prefix_is_cancelled(journal, gateway):
    gateway.inject_order(symbol="BTCUSDT", client_order_id="mnt-orphan123")
    report = Reconciler(journal, gateway).run()
    assert report.clean and report.orphans_cancelled == ["mnt-orphan123"]
    assert gateway.orders["mnt-orphan123"]["status"] == "CANCELED"


def test_foreign_order_is_reported_not_touched(journal, gateway):
    gateway.inject_order(symbol="BTCUSDT", client_order_id="web_ui_manual_1")
    report = Reconciler(journal, gateway).run()
    assert report.clean
    assert report.foreign_open_orders == ["BTCUSDT:web_ui_manual_1"]
    assert gateway.orders["web_ui_manual_1"]["status"] == "NEW"  # untouched


def test_exchange_lookup_failure_is_dirty(journal, gateway):
    journal_submitting(journal)

    def boom(**kwargs):
        raise ConnectionError("exchange down")

    gateway.get_order = boom
    report = Reconciler(journal, gateway).run()
    assert not report.clean
    assert any("lookup failed" in e for e in report.errors)


def test_clean_empty_run(journal, gateway):
    report = Reconciler(journal, gateway).run()
    assert report.clean and report.summary()["clean"] is True
