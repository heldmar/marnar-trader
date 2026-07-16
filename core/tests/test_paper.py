"""PaperGateway: simulated account over (fake) live prices — fills, fees,
persistence, idempotency, and reconciler compatibility."""

from __future__ import annotations

from decimal import Decimal

import pytest

from trader.execution import OrderManager
from trader.journal import Journal
from trader.paper import PaperGateway
from trader.reconciler import Reconciler
from trader.risk import RiskManager


class FakePrices:
    def __init__(self, prices: dict[str, str]):
        self.prices = {k: Decimal(v) for k, v in prices.items()}

    def ticker_price(self, symbol: str) -> Decimal:
        return self.prices[symbol]

    def symbol_info(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }

    def server_time(self) -> int:
        return 1_760_000_000_000


@pytest.fixture
def prices():
    return FakePrices({"BTCUSDT": "50000"})


@pytest.fixture
def paper(tmp_path, prices):
    return PaperGateway(tmp_path / "paper.json", prices, initial_usdt=Decimal("150"))


def test_market_buy_fills_at_slipped_price_and_charges_fee(paper):
    ack = paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-t1"
    )
    assert ack["status"] == "FILLED"
    fill = ack["fills"][0]
    # 5 bps against us: 50000 * 1.0005 = 50025
    assert Decimal(fill["price"]) == Decimal("50025.00")
    assert fill["commissionAsset"] == "USDT"
    balances = paper.account_balances()
    assert balances["BTC"] == Decimal("0.0002")
    notional = Decimal("0.0002") * Decimal("50025.00")
    assert balances["USDT"] == Decimal("150") - notional - notional * Decimal("0.001")


def test_market_sell_roundtrip_updates_balances(paper):
    paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-b"
    )
    paper.place_market_order(
        symbol="BTCUSDT", side="SELL", quantity="0.0002", client_order_id="mnt-s"
    )
    balances = paper.account_balances()
    assert "BTC" not in balances  # zero balances are hidden
    assert balances["USDT"] < Decimal("150")  # fees + slippage both ways


def test_sell_without_holdings_is_rejected(paper):
    with pytest.raises(ValueError, match="insufficient paper"):
        paper.place_market_order(
            symbol="BTCUSDT", side="SELL", quantity="1", client_order_id="mnt-x"
        )


def test_market_order_is_idempotent_on_crash_retry(paper):
    a1 = paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-dup"
    )
    a2 = paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-dup"
    )
    assert a1["orderId"] == a2["orderId"]
    assert paper.account_balances()["BTC"] == Decimal("0.0002")  # only once


def test_limit_order_rests_then_fills_on_poll(paper, prices):
    ack = paper.place_limit_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", price="49000",
        client_order_id="mnt-l1",
    )
    assert ack["status"] == "NEW"
    assert paper.poll() == []  # price still above the limit
    prices.prices["BTCUSDT"] = Decimal("48900")
    filled = paper.poll()
    assert [o["clientOrderId"] for o in filled] == ["mnt-l1"]
    assert paper.get_order(symbol="BTCUSDT", client_order_id="mnt-l1")["status"] == "FILLED"
    assert paper.account_balances()["BTC"] == Decimal("0.0002")


def test_marketable_limit_fills_immediately(paper):
    ack = paper.place_limit_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", price="51000",
        client_order_id="mnt-l2",
    )
    assert ack["status"] == "FILLED"
    assert Decimal(ack["fills"][0]["price"]) == Decimal("51000")  # no price improvement


def test_state_survives_restart(tmp_path, prices):
    p1 = PaperGateway(tmp_path / "paper.json", prices, initial_usdt=Decimal("150"))
    p1.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-r1"
    )
    p2 = PaperGateway(tmp_path / "paper.json", prices, initial_usdt=Decimal("150"))
    assert p2.account_balances()["BTC"] == Decimal("0.0002")
    assert p2.get_order(symbol="BTCUSDT", client_order_id="mnt-r1")["status"] == "FILLED"


def test_equity_marks_positions_at_live_price(paper, prices):
    paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id="mnt-e1"
    )
    prices.prices["BTCUSDT"] = Decimal("60000")
    equity = paper.equity_usdt()
    assert equity > Decimal("150")  # position appreciated past costs


def test_reconciler_runs_clean_against_paper_gateway(tmp_path, paper):
    journal = Journal(tmp_path / "journal.db")
    risk = RiskManager(journal, __import__("trader.config", fromlist=["RiskLimits"]).RiskLimits())
    orders = OrderManager(journal, paper, risk)
    orders.submit_market(
        symbol="BTCUSDT", side="BUY", quantity="0.0002",
        mark_price=Decimal("50000"), equity=Decimal("150"), source="test",
    )
    report = Reconciler(journal, paper).run()
    assert report.clean
    journal.close()


def test_reconciler_adopts_paper_order_after_crash_mid_submit(tmp_path, paper):
    """Journal says SUBMITTING, paper exchange has the (persisted) order —
    the reconciler must adopt it, exactly like on the real exchange."""
    journal = Journal(tmp_path / "journal.db")
    intent = journal.record_intent(
        symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity="0.0002",
        price=None, source="test", nonce="n1",
    )
    coid = journal.record_order_submitting(intent)
    paper.place_market_order(
        symbol="BTCUSDT", side="BUY", quantity="0.0002", client_order_id=coid
    )
    # crash here: journal stuck in SUBMITTING, paper state persisted
    report = Reconciler(journal, paper).run()
    assert report.clean
    assert coid in report.adopted
    pos = journal.position_for("BTCUSDT")
    assert pos is not None and Decimal(pos["quantity"]) == Decimal("0.0002")
    journal.close()
