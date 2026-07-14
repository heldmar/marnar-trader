from decimal import Decimal

import pytest

from trader.gateway import BinanceGateway, parse_filters

BTCUSDT_INFO = {
    "symbol": "BTCUSDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
        {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
    ],
}


def test_parse_filters():
    f = parse_filters(BTCUSDT_INFO)
    assert f.price_tick == Decimal("0.01")
    assert f.lot_step == Decimal("0.00001")
    assert f.min_notional == Decimal("5")


def test_rounding_respects_filters():
    f = parse_filters(BTCUSDT_INFO)
    assert f.round_price(Decimal("50000.123456")) == Decimal("50000.12")
    assert f.round_qty(Decimal("0.0000199")) == Decimal("0.00001")


def test_min_order_qty_clears_min_notional():
    f = parse_filters(BTCUSDT_INFO)
    price = Decimal("50000")
    qty = f.min_order_qty(price)
    assert qty * price >= f.min_notional
    assert qty >= f.min_qty
    assert qty % f.lot_step == 0


def test_live_mode_is_blocked_before_paper_gate():
    with pytest.raises(NotImplementedError, match="paper gate"):
        BinanceGateway("k", "s", testnet=False)
