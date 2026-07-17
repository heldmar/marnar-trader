"""Live strategy engine (Sprint 5) — runs the D-26 reference strategy against
live market data through the same intent path as everything else:
strategy → RiskManager → OrderManager → gateway → journal.

The engine is mode-blind: it drives whatever ExchangeGateway it is given
(paper today, live after the Q13 gate). Design mirrors the backtester so the
parity report compares like with like:

- Strategies see only *closed* candles (fetched from production public data,
  the S3 source) and emit intents through the same ``StrategyContext`` shape.
- Intents execute as market orders on the next poll after the candle closes —
  the live analogue of the backtester's "fill at next open".
- Protective stops are engine-held: levels are persisted in the journal
  (crash-safe) and checked against the live price every poll. Stop-loss wins
  over take-profit when both would fire (same pessimism as the backtester).
- Candles the engine consumes are appended to the CandleStore, so the parity
  report replays *exactly* the data the live strategy saw.

Restart semantics (no-UPS discipline): strategy indicator state is rebuilt by
replaying stored/fetched history with entries+exits gated; only candles that
closed after the last processed one are acted on. Stop levels survive restarts
via the journal. A gap while offline is therefore handled conservatively —
stale candles inform the indicators but never trade.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from trader.alerts import TelegramAlerts
from trader.backtest import Position
from trader.execution import OrderManager, OrderRejected, halt_and_apply
from trader.journal import Journal
from trader.macro_events import all_event_times_ms
from trader.marketdata import INTERVAL_MS, Candle, CandleStore, MarketDataClient, candle_from_kline
from trader.risk import RiskManager
from trader.strategies import DonchianBreakout, EventBlackout

log = logging.getLogger(__name__)

LAST_CANDLE_KEY = "engine:last_candle:{symbol}"
PROTECT_KEY = "engine:protect:{symbol}"


class LiveContext:
    """StrategyContext-shaped adapter for live runs: exposes the journal's view
    of the position and collects the strategy's requests for the engine to
    execute. Same surface the backtester provides — strategies can't tell."""

    def __init__(self, position_qty: float, cash: float):
        self.position = Position(qty=position_qty)
        self.cash = cash
        self.requests: list[tuple[str, float, float | None, float | None]] = []

    def buy(
        self,
        quote_amount: float,
        *,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> None:
        self.requests.append(("BUY", quote_amount, stop_loss_pct, take_profit_pct))

    def sell_all(self) -> None:
        self.requests.append(("SELL", 0.0, None, None))


class _Muted:
    """Warmup context: indicators learn from history, nothing trades."""

    def __init__(self, position_qty: float):
        self.position = Position(qty=position_qty)
        self.cash = 0.0

    def buy(self, quote_amount: float, **kwargs) -> None:
        return None

    def sell_all(self) -> None:
        return None


class PaperEngine:
    """Polls live data, feeds closed candles to per-symbol strategies, executes
    their intents, enforces engine-held protective stops, and keeps the risk
    manager fed with mark-to-market equity."""

    def __init__(
        self,
        *,
        journal: Journal,
        gateway: Any,  # PaperGateway today; ExchangeGateway + equity_usdt()/poll()
        orders: OrderManager,
        risk: RiskManager,
        market: MarketDataClient,
        store: CandleStore,
        symbols: list[str],
        interval: str = "1d",
        entry_n: int = 15,
        exit_n: int = 15,
        stop_loss_pct: float = 3.0,
        spend_usdt: float = 15.0,
        poll_seconds: float = 60.0,
        alerts: TelegramAlerts | None = None,
        event_blackout: bool = True,  # D-23/D-26: keep as cheap insurance
        now_ms: Callable[[], int] | None = None,
    ):
        self.journal = journal
        self.gateway = gateway
        self.orders = orders
        self.risk = risk
        self.market = market
        self.store = store
        self.symbols = symbols
        self.interval = interval
        self.step = INTERVAL_MS[interval]
        self.spend = spend_usdt
        self.poll_seconds = poll_seconds
        self.alerts = alerts or TelegramAlerts()
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._warmup_candles = max(entry_n, exit_n) + 5
        events = list(all_event_times_ms()) if event_blackout else []
        if event_blackout and (not events or max(events) < self.now_ms()):
            # QA1-14: an exhausted calendar means the rule is configured on but
            # silently inert — that must be loud, not invisible.
            log.warning(
                "event blackout is enabled but the macro-events calendar has no "
                "future entries (latest: %s) — extend trader/macro_events.py",
                max(events) if events else "none",
            )

        def make_strategy():
            inner = DonchianBreakout(
                entry_n, exit_n, spend_usdt=spend_usdt, stop_loss_pct=stop_loss_pct
            )
            return EventBlackout(inner, events) if events else inner

        self.strategies = {s: make_strategy() for s in self.symbols}
        self._started = False

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Rebuild indicator state from history; act on nothing stale."""
        latest_closed = self._latest_closed_open_time()
        for symbol in self.symbols:
            last_done = self.journal.get_state(LAST_CANDLE_KEY.format(symbol=symbol))
            candles = self._fetch_closed(
                symbol, latest_closed - (self._warmup_candles - 1) * self.step
            )
            qty = self._position_qty(symbol)
            for c in candles:
                self.strategies[symbol].on_candle(c, _Muted(qty))  # type: ignore[arg-type]
            if candles:
                self.store.append(symbol, self.interval, candles)
            self.journal.set_state(
                LAST_CANDLE_KEY.format(symbol=symbol),
                max(latest_closed, last_done or 0),
            )
        self._started = True
        # First-ever start stamps the paper clock (Q13: 3 consecutive weeks) and
        # anchors the parity window; restarts do not move it.
        if self.journal.get_state("engine:paper_started_at") is None:
            self.journal.set_state("engine:paper_started_at", self.now_ms())
            self.journal.record_event(
                "PAPER_CLOCK_STARTED", "engine",
                {"interval": self.interval, "symbols": len(self.symbols)},
            )
        self.journal.record_event(
            "ENGINE_STARTED", "engine",
            {"interval": self.interval, "symbols": self.symbols},
        )
        log.info(
            "engine warmed up: %d symbols, interval %s, next actionable close at %d",
            len(self.symbols), self.interval, latest_closed + self.step,
        )

    async def run(self) -> None:
        """The 24/7 loop. Exceptions are contained per-cycle: a failed poll is
        logged and alerted, never fatal (the Pi restarts container-level only)."""
        while not self._started:
            try:
                self.start()
            except Exception as exc:
                log.exception("engine warmup failed — retrying in %ss", self.poll_seconds)
                self.alerts.send(f"⚠️ engine warmup failed (will retry): {exc}")
                await asyncio.sleep(self.poll_seconds)
        self.alerts.send(
            f"📈 MarNar paper engine up: {len(self.symbols)} pairs, "
            f"Donchian @ {self.interval}, risk state {self.risk.state.value}"
        )
        consecutive_errors = 0
        while True:
            try:
                self.poll_once()
                consecutive_errors = 0
            except Exception as exc:
                consecutive_errors += 1
                log.exception("engine poll failed")
                if consecutive_errors in (1, 10, 100):  # alert with backoff, not spam
                    self.alerts.send(f"⚠️ engine poll failing (x{consecutive_errors}): {exc}")
            await asyncio.sleep(self.poll_seconds)

    # -- one poll cycle ------------------------------------------------------------

    def poll_once(self) -> None:
        for order in getattr(self.gateway, "poll", list)() or []:
            self._journal_paper_fills(order)
        self._check_protective_stops()
        for symbol in self.symbols:
            self._process_new_candles(symbol)
        equity = Decimal(str(self.gateway.equity_usdt()))
        self._snapshot_equity(equity)
        result = halt_and_apply(self.risk, self.orders, equity)
        if result is not None:
            for symbol in self.symbols:  # positions are gone or intentional — clear stops
                self.journal.set_state(PROTECT_KEY.format(symbol=symbol), None)
            self.alerts.send(
                f"🛑 circuit breaker: {result['state']} — cancelled {result['cancelled']} "
                f"orders, closed {len(result['closed_positions'])} positions. "
                "Manual /api/resume required."
            )

    SNAPSHOT_EVERY_MS = 15 * 60 * 1000  # UI equity curve granularity

    def _snapshot_equity(self, equity: Decimal) -> None:
        last = self.journal.get_state("engine:last_equity_snapshot") or 0
        now = self.now_ms()
        if now - last >= self.SNAPSHOT_EVERY_MS:
            self.journal.record_equity_snapshot(str(equity))
            self.journal.set_state("engine:last_equity_snapshot", now)

    # -- candles ---------------------------------------------------------------------

    def _latest_closed_open_time(self) -> int:
        now = self.now_ms()
        return (now // self.step - 1) * self.step

    def _fetch_closed(self, symbol: str, start_ms: int) -> list[Candle]:
        rows = self.market.klines(symbol, self.interval, start_ms=start_ms, limit=1000)
        now = self.now_ms()
        return [
            c for r in rows
            if (c := candle_from_kline(r)).open_time + self.step <= now
        ]

    def _process_new_candles(self, symbol: str) -> None:
        key = LAST_CANDLE_KEY.format(symbol=symbol)
        last_done = self.journal.get_state(key) or 0
        latest = self._latest_closed_open_time()
        if latest <= last_done:
            return
        candles = self._fetch_closed(symbol, last_done + self.step)
        if not candles:
            return
        self.store.append(symbol, self.interval, candles)
        for candle in candles:
            ctx = LiveContext(self._position_qty(symbol), float(self._usdt_free()))
            strategy = self.strategies[symbol]
            strategy.on_candle(candle, ctx)  # type: ignore[arg-type]
            # Unwrap restrict-only wrappers (EventBlackout) to reach the signal.
            signal = getattr(getattr(strategy, "inner", strategy), "last_signal", None)
            for side, quote_amount, sl_pct, tp_pct in ctx.requests:
                if side == "BUY":
                    self._execute_buy(symbol, quote_amount, sl_pct, tp_pct, signal=signal)
                else:
                    self._execute_sell(symbol, "strategy exit", signal=signal)
            self.journal.set_state(key, candle.open_time)

    # -- account helpers ---------------------------------------------------------------

    def _position_qty(self, symbol: str) -> float:
        pos = self.journal.position_for(symbol)
        return float(pos["quantity"]) if pos else 0.0

    def _usdt_free(self) -> Decimal:
        return self.gateway.account_balances().get("USDT", Decimal("0"))

    # -- order execution ------------------------------------------------------------------

    def _execute_buy(
        self,
        symbol: str,
        quote_amount: float,
        sl_pct: float | None,
        tp_pct: float | None,
        signal: dict | None = None,
    ) -> None:
        price = self.gateway.ticker_price(symbol)
        filters = self.gateway.symbol_filters(symbol)
        budget = min(Decimal(str(quote_amount)), self._usdt_free() / (1 + Decimal("0.001")))
        qty = filters.round_qty(budget / price)
        if qty < filters.min_qty or qty * price < filters.min_notional:
            self.journal.record_event(
                "ENTRY_SKIPPED_MIN_NOTIONAL", symbol,
                {"qty": str(qty), "price": str(price)},
            )
            return
        try:
            coid = self.orders.submit_market(
                symbol=symbol, side="BUY", quantity=str(qty), mark_price=price,
                equity=Decimal(str(self.gateway.equity_usdt())),
                source=f"strategy:donchian@{self.interval}",
            )
        except OrderRejected as exc:
            log.info("entry rejected by risk: %s %s", symbol, exc)
            return
        if signal:
            self.journal.record_event("TRADE_REASON", coid, {**signal, "symbol": symbol})
        fill_price = self._avg_fill_price(coid) or price
        protect = {
            "stop": str(fill_price * (1 - Decimal(str(sl_pct)) / 100)) if sl_pct else None,
            "tp": str(fill_price * (1 + Decimal(str(tp_pct)) / 100)) if tp_pct else None,
        }
        self.journal.set_state(PROTECT_KEY.format(symbol=symbol), protect)
        self.alerts.send(
            f"🟢 BUY {qty} {symbol} @ ~{fill_price} "
            f"(stop {protect['stop'] or '—'}, target {protect['tp'] or '—'})"
        )

    def _execute_sell(
        self, symbol: str, reason: str, *, protective: bool = False, signal: dict | None = None
    ) -> None:
        pos = self.journal.position_for(symbol)
        if pos is None:
            return
        filters = self.gateway.symbol_filters(symbol)
        qty = filters.round_qty(Decimal(pos["quantity"]))
        if qty <= 0:
            return
        price = self.gateway.ticker_price(symbol)
        try:
            if protective:
                coid = self.orders.exit_market(symbol=symbol, quantity=str(qty), reason=reason)
            else:
                coid = self.orders.submit_market(
                    symbol=symbol, side="SELL", quantity=str(qty), mark_price=price,
                    equity=Decimal(str(self.gateway.equity_usdt())),
                    source=f"strategy:donchian@{self.interval}",
                )
        except OrderRejected as exc:
            log.warning("sell rejected (%s): %s %s", reason, symbol, exc)
            return
        self.journal.set_state(PROTECT_KEY.format(symbol=symbol), None)
        if signal or protective:
            payload = {**(signal or {}), "symbol": symbol}
            if protective:
                payload["rule"] = f"protective {reason} level was hit"
            self.journal.record_event("TRADE_REASON", coid, payload)
        fill_price = self._avg_fill_price(coid) or price
        self.alerts.send(f"🔴 SELL {qty} {symbol} @ ~{fill_price} ({reason})")

    def _avg_fill_price(self, coid: str) -> Decimal | None:
        order = self.journal.get_order(coid)
        if order is None or order["exchange_order_id"] is None:
            return None
        trades = self.gateway.my_trades(
            symbol=order["symbol"], order_id=int(order["exchange_order_id"])
        )
        total_qty = sum(Decimal(t["qty"]) for t in trades)
        if not total_qty:
            return None
        return sum(Decimal(t["qty"]) * Decimal(t["price"]) for t in trades) / total_qty

    # -- protective stops -----------------------------------------------------------------

    def _check_protective_stops(self) -> None:
        for pos in self.journal.positions():
            symbol = pos["symbol"]
            protect = self.journal.get_state(PROTECT_KEY.format(symbol=symbol))
            if not protect:
                continue
            price = self.gateway.ticker_price(symbol)
            stop = Decimal(protect["stop"]) if protect.get("stop") else None
            tp = Decimal(protect["tp"]) if protect.get("tp") else None
            # Stop beats target — same pessimistic ordering as the backtester.
            if stop is not None and price <= stop:
                self._execute_sell(symbol, "stop_loss", protective=True)
            elif tp is not None and price >= tp:
                self._execute_sell(symbol, "take_profit", protective=True)

    # -- paper-only plumbing ------------------------------------------------------------

    def _journal_paper_fills(self, order: dict) -> None:
        """A resting paper limit order filled during poll(): journal its fills
        (idempotent — record_fill dedupes on fill_id)."""
        from trader.journal import utcnow

        coid = order.get("clientOrderId", "")
        if self.journal.get_order(coid) is None:
            return
        self.journal.record_order_status(coid, order["status"], {"via": "paper poll"})
        for f in order.get("fills", []):
            self.journal.record_fill(
                client_order_id=coid,
                fill_id=f"{order['symbol']}-{f['tradeId']}",
                quantity=f["qty"],
                price=f["price"],
                fee=f.get("commission", "0"),
                fee_asset=f.get("commissionAsset"),
                executed_at=utcnow(),
            )
