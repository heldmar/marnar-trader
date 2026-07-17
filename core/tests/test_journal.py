import threading
from decimal import Decimal

from trader.journal import SCHEMA_VERSION, Journal, client_order_id_for, make_intent_id


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


# -- v2 (Sprint 2): migration, positions, P&L, app state -----------------------------


def filled_buy(j, symbol="BTCUSDT", qty="0.002", price="50000", nonce="v2-1", trade="t-1"):
    intent = j.record_intent(symbol=symbol, side="BUY", order_type="LIMIT",
                             quantity=qty, price=price, source="test", nonce=nonce)
    coid = j.record_order_submitting(intent)
    j.record_order_ack(coid, "1", "FILLED")
    j.record_fill(client_order_id=coid, fill_id=trade, quantity=qty, price=price,
                  fee="0", fee_asset="USDT", executed_at="2026-07-13T00:00:00Z")
    j.record_order_status(coid, "FILLED")
    return coid


def test_v1_db_migrates_to_current_version(tmp_path):
    j = make_journal(tmp_path)
    j.close()
    import sqlite3

    conn = sqlite3.connect(tmp_path / "journal.db")
    with conn:
        conn.execute("UPDATE schema_version SET version=1")
        conn.executescript(
            "DROP TABLE positions; DROP TABLE pnl_ledger; DROP TABLE app_state;"
        )
    conn.close()

    j2 = make_journal(tmp_path)  # v1 DB (like the Pi's) must open and migrate
    assert j2._conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    j2.set_state("probe", {"ok": True})  # new tables exist and work
    assert j2.get_state("probe") == {"ok": True}
    j2.close()


def test_buy_fill_opens_position_and_sell_realizes_pnl(tmp_path):
    j = make_journal(tmp_path)
    filled_buy(j, qty="0.002", price="50000")  # cost 100
    pos = j.position_for("BTCUSDT")
    assert Decimal(pos["quantity"]) == Decimal("0.002")
    assert Decimal(pos["cost"]) == Decimal("100")

    intent = j.record_intent(symbol="BTCUSDT", side="SELL", order_type="LIMIT",
                             quantity="0.001", price="60000", source="test", nonce="v2-2")
    coid = j.record_order_submitting(intent)
    j.record_order_ack(coid, "2", "FILLED")
    j.record_fill(client_order_id=coid, fill_id="t-2", quantity="0.001", price="60000",
                  fee="0.06", fee_asset="USDT", executed_at="2026-07-13T01:00:00Z")

    pos = j.position_for("BTCUSDT")
    assert Decimal(pos["quantity"]) == Decimal("0.001")  # half sold
    # realized: (60000-50000)*0.001 - 0.06 fee = 9.94
    assert j.realized_pnl_since("2026-01-01") == Decimal("9.94")
    j.close()


def test_full_sell_closes_position(tmp_path):
    j = make_journal(tmp_path)
    filled_buy(j, qty="0.001", price="50000")
    intent = j.record_intent(symbol="BTCUSDT", side="SELL", order_type="MARKET",
                             quantity="0.001", price=None, source="test", nonce="v2-3")
    coid = j.record_order_submitting(intent)
    j.record_order_ack(coid, "3", "FILLED")
    j.record_fill(client_order_id=coid, fill_id="t-3", quantity="0.001", price="49000",
                  fee="0", fee_asset="USDT", executed_at="2026-07-13T02:00:00Z")
    assert j.position_for("BTCUSDT") is None
    assert j.realized_pnl_since("2026-01-01") == Decimal("-1.000")  # loss journaled too
    j.close()


def test_duplicate_fill_does_not_double_apply(tmp_path):
    j = make_journal(tmp_path)
    coid = filled_buy(j, qty="0.002", price="50000")
    # reconciler re-syncs the same trade id after a crash
    j.record_fill(client_order_id=coid, fill_id="t-1", quantity="0.002", price="50000",
                  fee="0", fee_asset="USDT", executed_at="2026-07-13T00:00:00Z")
    assert Decimal(j.position_for("BTCUSDT")["quantity"]) == Decimal("0.002")
    assert j._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    j.close()


def test_sell_without_position_is_flagged_not_crashing(tmp_path):
    j = make_journal(tmp_path)
    intent = j.record_intent(symbol="ETHUSDT", side="SELL", order_type="LIMIT",
                             quantity="1", price="3000", source="test", nonce="v2-4")
    coid = j.record_order_submitting(intent)
    j.record_order_ack(coid, "4", "FILLED")
    j.record_fill(client_order_id=coid, fill_id="t-9", quantity="1", price="3000",
                  fee="0", fee_asset="USDT", executed_at="2026-07-13T03:00:00Z")
    flagged = j._conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='FILL_WITHOUT_POSITION'"
    ).fetchone()[0]
    assert flagged == 1
    j.close()


def test_app_state_roundtrip_and_overwrite(tmp_path):
    j = make_journal(tmp_path)
    assert j.get_state("missing") is None
    assert j.get_state("missing", "fallback") == "fallback"
    j.set_state("k", {"a": 1})
    j.set_state("k", {"a": 2})
    assert j.get_state("k") == {"a": 2}
    j.close()
    j2 = make_journal(tmp_path)
    assert j2.get_state("k") == {"a": 2}  # survives restart
    j2.close()


def test_orders_created_since_counts_for_rate_cap(tmp_path):
    j = make_journal(tmp_path)
    filled_buy(j, nonce="r1", trade="t-r1")
    filled_buy(j, nonce="r2", trade="t-r2")
    assert j.orders_created_since("2000-01-01") == 2
    assert j.orders_created_since("2999-01-01") == 0
    j.close()


def test_get_state_survives_concurrent_writes(tmp_path):
    """Production bug (2026-07-17): get_state read the shared sqlite3
    Connection without the journal's lock while set_state wrote under it,
    and a read racing a write intermittently came back with a NULL/None
    value column, crashing json.loads with a TypeError. Every read method
    must take self._lock too."""
    j = make_journal(tmp_path)
    j.set_state("k", {"n": 0})
    errors: list[Exception] = []
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            j.set_state("k", {"n": n})

    def reader():
        while not stop.is_set():
            try:
                j.get_state("k")
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    stop.wait(1.0)
    stop.set()
    for t in threads:
        t.join()
    assert errors == []
    j.close()
