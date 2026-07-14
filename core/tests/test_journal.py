from trader.journal import Journal, client_order_id_for, make_intent_id


def make_journal(tmp_path):
    return Journal(tmp_path / "journal.db")


def record_demo_intent(j, nonce="n1"):
    return j.record_intent(
        symbol="BTCUSDT", side="BUY", order_type="LIMIT",
        quantity="0.001", price="50000", source="test", nonce=nonce,
    )


def test_pragmas_are_crash_safe(tmp_path):
    j = make_journal(tmp_path)
    assert j._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert j._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    j.close()


def test_intent_id_is_deterministic():
    a = make_intent_id("BTCUSDT", "BUY", "LIMIT", "0.001", "50000", "abc")
    b = make_intent_id("BTCUSDT", "BUY", "LIMIT", "0.001", "50000", "abc")
    c = make_intent_id("BTCUSDT", "BUY", "LIMIT", "0.001", "50000", "other-nonce")
    assert a == b != c


def test_client_order_id_valid_for_binance():
    coid = client_order_id_for("a" * 32)
    assert len(coid) <= 36
    assert coid.startswith("mnt-")


def test_full_order_lifecycle_journaled(tmp_path):
    j = make_journal(tmp_path)
    intent_id = record_demo_intent(j)
    coid = j.record_order_submitting(intent_id)
    assert j.get_order(coid)["status"] == "SUBMITTING"
    assert len(j.open_orders()) == 1

    j.record_order_ack(coid, "12345", "NEW")
    order = j.get_order(coid)
    assert order["status"] == "NEW"
    assert order["exchange_order_id"] == "12345"

    j.record_fill(
        client_order_id=coid, fill_id="t-1", quantity="0.001", price="50000",
        fee="0.05", fee_asset="USDT", executed_at="2026-07-13T00:00:00Z",
    )
    j.record_order_status(coid, "CANCELED")
    assert j.get_order(coid)["status"] == "CANCELED"
    assert len(j.open_orders()) == 0

    kinds = [e["kind"] for e in j.events_for(coid)]
    assert kinds == ["ORDER_SUBMITTING", "ORDER_ACK", "FILL", "ORDER_CANCELED"]
    j.close()


def test_crash_retry_cannot_double_place(tmp_path):
    """Same intent replayed after a crash yields the same client order id and one row."""
    j = make_journal(tmp_path)
    intent_id = record_demo_intent(j)
    coid1 = j.record_order_submitting(intent_id)

    # simulate crash + restart + replay of the same intent
    j.close()
    j2 = make_journal(tmp_path)
    intent_id_replayed = record_demo_intent(j2)  # INSERT OR IGNORE — same nonce, same id
    assert intent_id_replayed == intent_id
    coid2 = j2.record_order_submitting(intent_id_replayed)
    assert coid2 == coid1
    assert j2._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    j2.close()


def test_journal_survives_reopen(tmp_path):
    j = make_journal(tmp_path)
    intent_id = record_demo_intent(j)
    coid = j.record_order_submitting(intent_id)
    j.close()

    j2 = make_journal(tmp_path)
    assert j2.get_order(coid)["status"] == "SUBMITTING"
    assert len(j2.open_orders()) == 1  # reconciler's input after a power cut
    j2.close()
