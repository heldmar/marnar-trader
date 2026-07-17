"""Configuration store.

Layered configuration:

1. Code defaults (this module) — includes non-negotiable risk floor values.
2. YAML file on the data volume (``/data/config.yaml`` in production) — operator
   overrides, later edited via the web UI.
3. Environment variables — secrets only (Binance keys, Telegram token). Secrets
   never live in the YAML file or in Git (D-04).

Risk limits can only be *tightened* relative to the floors defined here; a config
file asking for looser limits is rejected at load time (R-02, G3).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

# Non-overridable risk floors (D-06, D-07, D-08). Config may tighten, never loosen.
RISK_FLOORS = {
    "max_daily_loss_pct": 2.0,
    "max_drawdown_pct": 20.0,
    "max_position_pct_per_coin": 10.0,
    "max_open_positions": 5,
    "max_trades_per_hour": 30,
}


class RiskLimits(BaseModel):
    max_daily_loss_pct: float = RISK_FLOORS["max_daily_loss_pct"]
    max_drawdown_pct: float = RISK_FLOORS["max_drawdown_pct"]
    max_position_pct_per_coin: float = RISK_FLOORS["max_position_pct_per_coin"]
    max_open_positions: int = RISK_FLOORS["max_open_positions"]
    max_trades_per_hour: int = RISK_FLOORS["max_trades_per_hour"]

    @model_validator(mode="after")
    def _enforce_floors(self) -> RiskLimits:
        for name, floor in RISK_FLOORS.items():
            if getattr(self, name) > floor:
                raise ValueError(
                    f"risk limit {name}={getattr(self, name)} is looser than the "
                    f"non-negotiable floor {floor}"
                )
        return self


class ScreenerConfig(BaseModel):
    quote_asset: str = "USDT"  # D-10
    min_24h_quote_volume: float = 5_000_000.0
    max_pairs: int = 20
    # D-09 market-cap criterion: base asset must rank in the top N by market cap
    # (CoinGecko). Pairs whose rank is unknown are kept but flagged in the report.
    max_market_cap_rank: int = 150
    # D-05/D-08: pilot capital used to verify each pair's minNotional is tradeable
    # at the 10%-per-coin position size.
    capital_usdt: float = 150.0
    # Base assets that are themselves stable/fiat-pegged — no trend to trade.
    exclude_bases: list[str] = ["USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "AEUR", "XUSD"]


class BacktestConfig(BaseModel):
    initial_cash_usdt: float = 150.0  # D-05 pilot sizing
    slippage_bps: float = 5.0  # applied against us on every market fill


class PaperConfig(BaseModel):
    """S5 paper-trading engine settings. Strategy parameters default to the
    walk-forward result (Context/reports/donchian-walkforward-2026-07-16.md):
    Donchian 15/15 with a 3% stop on 1d candles — chosen by every fold,
    validated out-of-sample against the D-26 baseline."""

    symbols: list[str] = [  # D-09 screener universe (screener-2026-07-14.md)
        "BTCUSDT", "ETHUSDT", "SXTUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT",
        "BNBUSDT", "NEARUSDT", "TRXUSDT", "WLDUSDT", "DEXEUSDT", "TAOUSDT",
        "UUSDT", "DOGEUSDT", "UNIUSDT", "AAVEUSDT", "XAUTUSDT", "PEPEUSDT",
        "XLMUSDT", "币安人生USDT",
    ]
    interval: str = "1d"
    entry_n: int = 15
    exit_n: int = 15
    stop_loss_pct: float = 3.0
    spend_usdt: float = 15.0  # D-08: 10% of pilot capital per position
    initial_usdt: float = 150.0  # D-05
    poll_seconds: float = 60.0
    event_blackout: bool = True  # D-23 rule, kept as cheap insurance (D-26)
    news_ingest_seconds: float = 900.0  # D-23a headline cycle
    report_check_seconds: float = 300.0  # S7 daily/weekly report catch-up cycle


class AppConfig(BaseModel):
    mode: str = Field(default="testnet", pattern="^(testnet|paper|live)$")
    timezone: str = "America/Montevideo"
    trading_window: str = "24/7"  # D-12; scheduler lands in S2+
    data_dir: Path = Path("/data")
    risk: RiskLimits = RiskLimits()
    screener: ScreenerConfig = ScreenerConfig()
    backtest: BacktestConfig = BacktestConfig()
    paper: PaperConfig = PaperConfig()
    # Testnet charges zero fees; simulations must model real fees (~0.1%/side).
    fee_rate_per_side: float = 0.001

    @property
    def db_path(self) -> Path:
        return self.data_dir / "journal.db"

    @property
    def candles_dir(self) -> Path:
        return self.data_dir / "candles"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


class Secrets(BaseModel):
    """Secrets come exclusively from environment variables (Portainer stack env vars
    in production, secrets/binance.env locally). Never from files in the repo."""

    binance_api_key: str = ""
    binance_api_secret: str = ""

    @classmethod
    def from_env(cls, mode: str) -> Secrets:
        prefix = "BINANCE_TESTNET_" if mode == "testnet" else "BINANCE_"
        return cls(
            binance_api_key=os.environ.get(f"{prefix}API_KEY", ""),
            binance_api_secret=os.environ.get(f"{prefix}API_SECRET", ""),
        )

    def require(self) -> Secrets:
        if not self.binance_api_key or not self.binance_api_secret:
            raise RuntimeError(
                "Binance API credentials missing from environment "
                "(BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET)"
            )
        return self


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load config: code defaults overridden by the YAML file at *path* (if it exists)."""
    overrides: dict[str, Any] = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            overrides = yaml.safe_load(p.read_text()) or {}
    return AppConfig(**overrides)


def save_config(config: AppConfig, path: Path | str) -> None:
    data = config.model_dump(mode="json")
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False))
