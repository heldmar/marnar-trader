"""Core service entrypoint — FastAPI app exposing health, risk status, kill switch.

Startup order is a crash-safety rule (docs/ARCHITECTURE.md §5.4): open the
journal, then reconcile against the exchange, and only if reconciliation is
clean does trading unlock. The kill switch is available regardless.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from trader import __version__
from trader.alerts import TelegramAlerts
from trader.api import router as api_router
from trader.config import Secrets, load_config
from trader.execution import OrderManager
from trader.gateway import BinanceGateway
from trader.journal import Journal
from trader.reconciler import Reconciler
from trader.risk import RiskManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("trader")

CONFIG_PATH = os.environ.get("TRADER_CONFIG", "/data/config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(CONFIG_PATH)
    app.state.config = config
    app.state.journal = Journal(config.db_path)
    app.state.gateway = None
    app.state.orders = None
    app.state.reconciliation = None
    app.state.trading_enabled = False
    app.state.risk = RiskManager(app.state.journal, config.risk, config.timezone)

    tasks: list[asyncio.Task] = []
    if config.mode == "paper":
        # Paper mode (S5): live production market data, simulated account.
        # No Binance credentials involved — the paper exchange needs none.
        from trader.engine import PaperEngine
        from trader.marketdata import BinancePublicData, CandleStore
        from trader.news import NewsIngestor
        from trader.paper import BinancePublicPrices, PaperGateway

        app.state.gateway = PaperGateway(
            config.data_dir / "paper-account.json",
            BinancePublicPrices(),
            initial_usdt=Decimal(str(config.paper.initial_usdt)),
            fee_rate=Decimal(str(config.fee_rate_per_side)),
            slippage_bps=Decimal(str(config.backtest.slippage_bps)),
        )
        app.state.orders = OrderManager(app.state.journal, app.state.gateway, app.state.risk)
        try:
            report = Reconciler(app.state.journal, app.state.gateway).run()
            app.state.reconciliation = report.summary()
            app.state.trading_enabled = report.clean
        except Exception as exc:
            log.error("reconciliation failed hard: %s", exc)
            app.state.reconciliation = {"clean": False, "errors": [str(exc)]}
        if app.state.trading_enabled:
            alerts = TelegramAlerts.from_env()
            engine = PaperEngine(
                journal=app.state.journal,
                gateway=app.state.gateway,
                orders=app.state.orders,
                risk=app.state.risk,
                market=BinancePublicData(),
                store=CandleStore(config.candles_dir),
                symbols=config.paper.symbols,
                interval=config.paper.interval,
                entry_n=config.paper.entry_n,
                exit_n=config.paper.exit_n,
                stop_loss_pct=config.paper.stop_loss_pct,
                spend_usdt=config.paper.spend_usdt,
                poll_seconds=config.paper.poll_seconds,
                alerts=alerts,
                event_blackout=config.paper.event_blackout,
            )
            app.state.engine = engine
            tasks.append(asyncio.create_task(engine.run(), name="paper-engine"))
            tasks.append(
                asyncio.create_task(
                    NewsIngestor(app.state.journal).run(
                        every_seconds=config.paper.news_ingest_seconds
                    ),
                    name="news-ingest",
                )
            )
        else:
            log.error("paper engine NOT started: reconciliation was not clean")
    else:
        secrets = Secrets.from_env(config.mode)
        if secrets.binance_api_key:
            app.state.gateway = BinanceGateway(
                secrets.binance_api_key, secrets.binance_api_secret, testnet=True
            )
            app.state.orders = OrderManager(app.state.journal, app.state.gateway, app.state.risk)
            # Reconcile FIRST, trade second — trading stays locked on a dirty report.
            try:
                report = Reconciler(app.state.journal, app.state.gateway).run()
                app.state.reconciliation = report.summary()
                app.state.trading_enabled = report.clean
            except Exception as exc:
                log.error("reconciliation failed hard: %s", exc)
                app.state.reconciliation = {"clean": False, "errors": [str(exc)]}
        else:
            log.warning("no Binance credentials in environment — exchange connectivity disabled")
    log.info(
        "core started: mode=%s db=%s trading_enabled=%s risk=%s",
        config.mode, config.db_path, app.state.trading_enabled, app.state.risk.state.value,
    )
    yield
    for task in tasks:
        task.cancel()
    app.state.journal.close()


app = FastAPI(title="MarNar Trader core", version=__version__, lifespan=lifespan)
app.include_router(api_router)


@app.get("/api/health")
async def health() -> dict:
    gw: BinanceGateway | None = app.state.gateway
    exchange = "unconfigured"
    if gw is not None:
        try:
            gw.server_time()
            exchange = "ok"
        except Exception as exc:  # report, never crash the health endpoint
            exchange = f"error: {exc}"
    return {
        "status": "ok" if exchange in ("ok", "unconfigured") else "degraded",
        "version": __version__,
        "mode": app.state.config.mode,
        "exchange": exchange,
        "trading_enabled": app.state.trading_enabled,
        "risk": app.state.risk.status(),
        "reconciliation": app.state.reconciliation,
        "open_orders_journaled": len(app.state.journal.open_orders()),
        "positions": [dict(p) for p in app.state.journal.positions()],
    }


class KillSwitchRequest(BaseModel):
    close_positions: bool = False
    reason: str = "kill switch"


@app.post("/api/kill-switch")
async def kill_switch(req: KillSwitchRequest) -> dict:
    """Manual halt (D-21): always stops trading and cancels pending orders;
    optionally force-flats all positions."""
    action = app.state.risk.halt_manual(req.reason, close_positions=req.close_positions)
    app.state.trading_enabled = False
    if app.state.orders is not None:
        return app.state.orders.apply_halt(action)
    return {"state": action.state.value, "cancelled": 0, "closed_positions": []}


@app.post("/api/resume")
async def resume() -> dict:
    """Manual reactivation (required after drawdown/manual halts, D-07)."""
    if app.state.reconciliation is not None and not app.state.reconciliation.get("clean"):
        raise HTTPException(409, "cannot resume: last reconciliation was not clean")
    app.state.risk.resume()
    app.state.trading_enabled = app.state.gateway is not None
    return {"state": app.state.risk.state.value, "trading_enabled": app.state.trading_enabled}
