"""Sprint 2 QA chaos demo — kill the process mid-order, restart, prove the
journal matches the exchange (the S2 investor touchpoint).

Phases:

  crash    Place a real testnet order through the full execution stack
           (risk gate → journal → exchange) and hard-kill the process
           (os._exit) the instant the exchange accepts it — BEFORE the ack is
           journaled. This is the worst-case power cut: the order exists on
           the exchange, the journal only knows "SUBMITTING".
  recover  Cold start: reopen the journal, run the reconciler, verify the
           journal now matches the exchange exactly, then cancel the adopted
           order and print the audit trail. PASS/FAIL.
  full     Run `crash` in a subprocess (it dies violently), then `recover`.

Run:  python -m trader.demo_chaos full [data_dir]
      (needs BINANCE_TESTNET_API_KEY/SECRET in env)
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from trader.config import Secrets, load_config
from trader.execution import OrderManager
from trader.gateway import BinanceGateway
from trader.journal import Journal
from trader.reconciler import Reconciler
from trader.risk import RiskManager

SYMBOL = "BTCUSDT"


class PowerCutGateway:
    """Wraps the real gateway; the box 'loses power' the moment the exchange
    accepts the order — after the request, before the ack reaches the journal."""

    def __init__(self, inner: BinanceGateway):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def place_limit_order(self, **kwargs):
        self._inner.place_limit_order(**kwargs)
        print(f"[CHAOS] exchange ACCEPTED {kwargs['client_order_id']} — POWER CUT NOW", flush=True)
        os._exit(9)  # no cleanup, no journal ack — exactly like pulling the plug


def build(config):
    secrets = Secrets.from_env(config.mode).require()
    gw = BinanceGateway(secrets.binance_api_key, secrets.binance_api_secret, testnet=True)
    journal = Journal(config.db_path)
    risk = RiskManager(journal, config.risk, config.timezone)
    return gw, journal, risk


def phase_crash(config) -> int:
    gw, journal, risk = build(config)
    filters = gw.symbol_filters(SYMBOL)
    market = gw.ticker_price(SYMBOL)
    price = filters.round_price(market * Decimal("0.80"))  # never fills
    qty = filters.min_order_qty(price)
    equity = gw.account_balances().get("USDT", Decimal("0"))
    print(f"[CRASH] placing {SYMBOL} BUY {qty} @ {price} (equity {equity:.2f} USDT) "
          "and dying mid-flight...")
    orders = OrderManager(journal, PowerCutGateway(gw), risk)
    orders.submit_limit(symbol=SYMBOL, side="BUY", quantity=str(qty), price=str(price),
                        equity=equity, source="chaos-demo")
    return 1  # unreachable — os._exit fired inside the gateway


def phase_recover(config) -> int:
    gw, journal, risk = build(config)
    stuck = [o for o in journal.open_orders() if o["status"] == "SUBMITTING"]
    print(f"[RECOVER] cold start: journal has {len(stuck)} order(s) stuck in SUBMITTING")
    for o in stuck:
        print(f"          {o['client_order_id']} ({o['symbol']} {o['side']} "
              f"{o['quantity']} @ {o['price']})")

    report = Reconciler(journal, gw).run()
    print(f"[RECOVER] reconciliation: clean={report.clean} adopted={report.adopted} "
          f"failed_pre_submit={report.failed_pre_submit} synced={report.synced} "
          f"orphans_cancelled={report.orphans_cancelled}")

    # Verify journal now matches the exchange, order by order.
    ok = report.clean
    orders = OrderManager(journal, gw, risk)
    for coid in report.adopted + report.synced:
        j = journal.get_order(coid)
        e = gw.get_order(symbol=j["symbol"], client_order_id=coid)
        match = e is not None and j["status"] == e["status"] \
            and j["exchange_order_id"] == str(e["orderId"])
        print(f"[VERIFY] {coid}: journal={j['status']}/{j['exchange_order_id']} "
              f"exchange={e and e['status']}/{e and e['orderId']} — "
              f"{'MATCH' if match else 'MISMATCH'}")
        ok = ok and match
        if match and e["status"] in ("NEW", "PARTIALLY_FILLED"):
            orders.cancel(coid)
            print(f"[CLEANUP] {coid} cancelled (journaled)")

    print("\n== Journal audit trail (chaos order) ==")
    for coid in report.adopted:
        for ev in journal.events_for(coid):
            print(f"  {ev['ts']}  {ev['kind']:<18} {ev['payload']}")

    leftover = journal.open_orders()
    ok = ok and not leftover and bool(report.adopted)
    print(f"\nFinal: reconciliation clean={report.clean}, adopted={len(report.adopted)}, "
          f"open orders in journal={len(leftover)} — {'PASS' if ok else 'FAIL'}")
    journal.close()
    return 0 if ok else 1


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "full"
    data_dir = sys.argv[2] if len(sys.argv) > 2 else None
    config = load_config()
    if data_dir:
        config = config.model_copy(update={"data_dir": Path(data_dir)})

    if phase == "crash":
        return phase_crash(config)
    if phase == "recover":
        return phase_recover(config)
    if phase == "full":
        print("== MarNar Trader S2 chaos demo ==\n-- phase 1: crash mid-order --")
        proc = subprocess.run(  # noqa: S603 — our own module, args fixed
            [sys.executable, "-m", "trader.demo_chaos", "crash", *(sys.argv[2:])],
        )
        print(f"-- process died with exit code {proc.returncode} (expected 9) --\n"
              "-- phase 2: restart & reconcile --")
        if proc.returncode != 9:
            print("FAIL: crash phase did not die as expected")
            return 1
        return phase_recover(config)
    print(f"unknown phase {phase!r} (use crash|recover|full)")
    return 2


if __name__ == "__main__":
    sys.exit(main())
