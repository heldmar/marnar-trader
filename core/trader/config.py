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
    # QA1-08: unknown keys are typos, not silent no-ops.
    model_config = {"extra": "forbid"}

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
    # D-38: a breakout strategy needs something to break out of. UUSDT reached
    # the live universe as a de-facto dollar peg (0.4% annualised volatility,
    # 0.2% total range over 184 days) because the stablecoin filter matches on
    # the *name* — "USD" in base — and the token is called "U". Screening on
    # behaviour instead catches the whole class. 1% of the 24h high/low range
    # separates pegs (UUSDT 0.05%) from even the quietest real assets
    # (gold-backed XAUT/PAXG ~2%, BTC ~3%).
    min_24h_range_pct: float = 1.0
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
    walk-forward result (2026-07-16 tuning run):
    Donchian 15/15 with a 3% stop on 1d candles — chosen by every fold,
    validated out-of-sample against the D-26 baseline."""

    # QA1-08: reject typo'd keys instead of silently ignoring them, and bound
    # the operational knobs (poll_seconds=0.001 would hammer Binance from the Pi).
    model_config = {"extra": "forbid"}

    # D-09 screener universe (screener-2026-07-14.md), minus UUSDT — a de-facto
    # dollar peg that reached the list through the name-based stablecoin filter
    # (D-38). The screener now rejects its whole class behaviourally via
    # ScreenerConfig.min_24h_range_pct.
    symbols: list[str] = [
        "BTCUSDT", "ETHUSDT", "SXTUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT",
        "BNBUSDT", "NEARUSDT", "TRXUSDT", "WLDUSDT", "DEXEUSDT", "TAOUSDT",
        "DOGEUSDT", "UNIUSDT", "AAVEUSDT", "XAUTUSDT", "PEPEUSDT",
        "XLMUSDT", "币安人生USDT",
    ]
    interval: str = "1d"
    entry_n: int = 15
    exit_n: int = 15
    # D-37/D-40: briefly widened to 10.0, reverted the same day. The +14.5pp
    # that justified widening was measured on the 20 pairs the screener picked
    # on 2026-07-14 and then backtested over the *preceding* two years. Screened
    # point-in-time instead — each quarter using only the data available at its
    # start — the ranking inverts and falls monotonically with stop width:
    # 3% +3.33%/qtr, 10% +1.50, 2xATR -0.09, no stop -1.56. Paired over 12 seeds
    # x 8 quarters the 3% advantage is +2.33pp (t=3.22), holding in 8 of 8
    # leave-one-quarter-out cuts. A wide stop pays only when the universe holds
    # monster trends to ride; on an honest universe it just absorbs bigger
    # losses. From the 2026-07-30 stop-width remeasurement, §11.
    stop_loss_pct: float = 3.0
    # D-08 was a fixed $15 (10% of the $150 pilot capital). Fixed-dollar sizing
    # deadlocks against risk.max_position_pct_per_coin the moment equity dips
    # even slightly below the original balance (incident 2026-07-21: a $2.97
    # paper loss dropped the 10% cap to $14.70, permanently rejecting every
    # future $15 entry since equity can't recover without a trade succeeding).
    # spend_pct tracks *current* equity instead, 1pp under the cap floor so it
    # can never structurally exceed it regardless of drawdown.
    spend_pct: float = 9.0
    initial_usdt: float = 150.0  # D-05
    poll_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    event_blackout: bool = True  # D-23 rule, kept as cheap insurance (D-26)
    news_ingest_seconds: float = Field(default=900.0, ge=60.0)  # D-23a headline cycle
    # D-09 screener-driven rotation. Off by default: switching it on changes the
    # book the paper gate is measuring, so it is a deliberate act. Note it is
    # NOT in CLOCK_RESETTING_FIELDS — the rotations it performs are exempt
    # (D-42a), and a gate that restarted on every rotation could never be
    # reached. Weekly, so the three-week gate actually exercises the machinery.
    rotate_universe: bool = False
    rotate_seconds: float = Field(default=7 * 24 * 3600.0, ge=3600.0)
    report_check_seconds: float = Field(default=300.0, ge=30.0)  # S7 report catch-up cycle


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
    from trader.fsutil import atomic_write_text  # QA1-11: power-cut-safe write

    data = config.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False))
