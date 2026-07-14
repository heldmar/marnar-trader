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


class AppConfig(BaseModel):
    mode: str = Field(default="testnet", pattern="^(testnet|paper|live)$")
    timezone: str = "America/Montevideo"
    trading_window: str = "24/7"  # D-12; scheduler lands in S2+
    data_dir: Path = Path("/data")
    risk: RiskLimits = RiskLimits()
    screener: ScreenerConfig = ScreenerConfig()
    backtest: BacktestConfig = BacktestConfig()
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
