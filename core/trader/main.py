"""Core service entrypoint — FastAPI app exposing health and status.

Sprint 1 keeps the service minimal: it opens the journal, verifies exchange
connectivity, and reports health. Trading loops arrive in later sprints.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from trader import __version__
from trader.config import Secrets, load_config
from trader.gateway import BinanceGateway
from trader.journal import Journal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("trader")

CONFIG_PATH = os.environ.get("TRADER_CONFIG", "/data/config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config(CONFIG_PATH)
    app.state.config = config
    app.state.journal = Journal(config.db_path)
    app.state.gateway = None
    secrets = Secrets.from_env(config.mode)
    if secrets.binance_api_key:
        app.state.gateway = BinanceGateway(
            secrets.binance_api_key, secrets.binance_api_secret, testnet=True
        )
    else:
        log.warning("no Binance credentials in environment — exchange connectivity disabled")
    log.info("core started: mode=%s db=%s", config.mode, config.db_path)
    yield
    app.state.journal.close()


app = FastAPI(title="MarNar Trader core", version=__version__, lifespan=lifespan)


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
        "open_orders_journaled": len(app.state.journal.open_orders()),
    }
