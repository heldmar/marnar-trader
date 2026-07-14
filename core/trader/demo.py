"""Sprint 1 demo — place and cancel a testnet order from code, fully journaled.

Flow (exactly the S1 investor touchpoint from the project plan):

1. REST connectivity: server time + account balances.
2. WS connectivity: subscribe to the symbol's ticker stream, receive live messages.
3. Build an intent: BUY limit ~20% below market (guaranteed not to fill), sized to
   the exchange's own minNotional/lotSize filters.
4. Journal the intent, journal SUBMITTING *before* the REST call, place the order,
   journal the ack, cancel it, journal the cancel.
5. Print the resulting journal audit trail.

Run:  python -m trader.demo   (needs BINANCE_TESTNET_API_KEY/SECRET in env)
"""

from __future__ import annotations

import json
import sys
import time
from decimal import Decimal
from pathlib import Path

from trader.config import Secrets, load_config
from trader.gateway import BinanceGateway
from trader.journal import Journal, new_nonce

SYMBOL = "BTCUSDT"


def main(data_dir: str | None = None) -> int:
    config = load_config()
    if data_dir:
        config = config.model_copy(update={"data_dir": Path(data_dir)})
    secrets = Secrets.from_env(config.mode).require()
    gw = BinanceGateway(secrets.binance_api_key, secrets.binance_api_secret, testnet=True)
    journal = Journal(config.db_path)

    print(f"== MarNar Trader S1 demo — mode={config.mode}, journal={config.db_path}\n")

    # 1. REST connectivity
    t0 = time.time()
    server_time = gw.server_time()
    print(f"[REST] server time: {server_time} (round-trip {time.time() - t0:.2f}s)")
    balances = gw.account_balances()
    shown = {a: str(q) for a, q in sorted(balances.items())[:6]}
    print(f"[REST] account balances (first {len(shown)}): {shown}")

    # 2. WS connectivity
    ws_messages: list[dict] = []

    def on_message(_, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if msg.get("e") == "24hrTicker":
            ws_messages.append(msg)

    ws = gw.ws_stream_client(on_message)
    ws.ticker(symbol=SYMBOL.lower())
    deadline = time.time() + 15
    while len(ws_messages) < 2 and time.time() < deadline:
        time.sleep(0.2)
    ws.stop()
    if not ws_messages:
        print("[WS]   FAILED: no ticker messages received within 15s")
        return 1
    last = ws_messages[-1]
    print(f"[WS]   {len(ws_messages)} ticker msgs for {SYMBOL}; last price {last.get('c')}")

    # 3. Build a non-fillable intent from the exchange's own filters
    filters = gw.symbol_filters(SYMBOL)
    market = gw.ticker_price(SYMBOL)
    price = filters.round_price(market * Decimal("0.80"))
    qty = filters.min_order_qty(price)
    notional = price * qty
    print(
        f"[PLAN] {SYMBOL} market={market}  limit BUY {qty} @ {price} "
        f"(notional {notional:.2f} USDT, minNotional {filters.min_notional})"
    )

    # 4. Journaled order lifecycle
    intent_id = journal.record_intent(
        symbol=SYMBOL, side="BUY", order_type="LIMIT",
        quantity=str(qty), price=str(price), source="demo", nonce=new_nonce(),
    )
    coid = journal.record_order_submitting(intent_id)  # journaled BEFORE the network call
    ack = gw.place_limit_order(
        symbol=SYMBOL, side="BUY", quantity=str(qty), price=str(price), client_order_id=coid
    )
    journal.record_order_ack(coid, str(ack["orderId"]), ack["status"])
    print(f"[ORDER] placed: clientOrderId={coid} exchangeOrderId={ack['orderId']} "
          f"status={ack['status']}")

    cancel = gw.cancel_order(symbol=SYMBOL, client_order_id=coid)
    journal.record_order_status(coid, cancel["status"], {"exchange": "cancel ack"})
    print(f"[ORDER] cancelled: status={cancel['status']}")

    # 5. Audit trail
    print("\n== Journal audit trail ==")
    for ev in journal.events_for(intent_id) + journal.events_for(coid):
        print(f"  {ev['ts']}  {ev['kind']:<18} {ev['ref_id']}  {ev['payload']}")

    order = journal.get_order(coid)
    ok = order is not None and order["status"] == "CANCELED" and not journal.open_orders()
    print(f"\nFinal journal state: order={order['status']}, "
          f"open orders in journal: {len(journal.open_orders())} — {'PASS' if ok else 'FAIL'}")
    journal.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(data_dir=sys.argv[1] if len(sys.argv) > 1 else None))
