"""Pair auto-screener (Sprint 3, D-09) — selects the tradeable USDT universe
from investor-configured criteria. No manual whitelist.

Criteria applied, in order (each exclusion is counted and reported):
1. Quote asset is USDT (D-10) and the pair is TRADING with spot enabled.
2. Base asset is not a stablecoin/fiat (nothing to trend-trade) and not a
   leveraged token (UP/DOWN/BULL/BEAR — Binance's 3x products, not spot assets).
3. 24h quote volume >= configured minimum (liquidity floor), and the 24h
   high/low range >= configured minimum — a behavioural peg check (D-38),
   because a token pegged to the dollar without "USD" in its name passes
   criterion 2 and then cannot do anything but pay fees under a breakout rule.
4. Market cap: base asset ranks within the configured top-N (CoinGecko public
   data). If CoinGecko is unreachable the screener degrades to volume-only and
   says so in the report; pairs with unknown rank are kept but flagged.
5. Binance minNotional must fit the pilot position size — capital x 10%/coin
   (D-05/D-08). A pair we cannot legally size is not tradeable for us.

Top ``max_pairs`` by 24h quote volume survive. Report goes to stdout and
``/data/reports/screener-YYYY-MM-DD.md`` (the S3 investor touchpoint).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trader.config import ScreenerConfig
from trader.gateway import parse_filters

log = logging.getLogger(__name__)

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"


def excluded_base_reason(base: str, config: ScreenerConfig) -> str | None:
    """Why *base* can never be part of our universe, or None if it may pass.

    Shared with the point-in-time screen (``trader.pit``) so the historical
    harness and production apply the identical asset-class exclusions — the
    2026-07-30 scratchpad screen omitted ``exclude_bases`` and leaked EUR into
    its picks, which is exactly the divergence this function exists to prevent.
    """
    # Static list + pattern: any base with "USD" in its name is a dollar-pegged
    # stable (USDC, FDUSD, USD1, RLUSD, ...) — new stables appear faster than a
    # hardcoded list can follow.
    if base in config.exclude_bases or "USD" in base:
        return "stablecoin or fiat base asset"
    if any(base.endswith(sfx) for sfx in LEVERAGED_SUFFIXES):
        return "leveraged token (UP/DOWN/BULL/BEAR)"
    return None


@dataclass(frozen=True, slots=True)
class PairVerdict:
    symbol: str
    base: str
    quote_volume_24h: float
    last_price: float
    min_notional: float
    position_size: float  # capital x 10%/coin — what we would trade with
    market_cap_rank: int | None  # None = unknown to CoinGecko

    @property
    def notional_ok(self) -> bool:
        return self.position_size >= self.min_notional


@dataclass(slots=True)
class ScreenerReport:
    generated_at: str
    config: ScreenerConfig
    qualified: list[PairVerdict] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)  # reason -> count
    notes: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        cfg = self.config
        lines = [
            "# Screener report — tradeable pair universe",
            "",
            f"Generated: {self.generated_at}",
            f"Criteria: quote={cfg.quote_asset}, 24h volume >= "
            f"{cfg.min_24h_quote_volume:,.0f} USDT, 24h range >= "
            f"{cfg.min_24h_range_pct:g}%, market-cap rank <= "
            f"{cfg.max_market_cap_rank}, top {cfg.max_pairs} by volume, "
            f"minNotional fits {cfg.capital_usdt:,.0f} USDT pilot capital at 10%/coin.",
            "",
            f"## Qualified pairs ({len(self.qualified)})",
            "",
            "| # | Pair | 24h volume (USDT) | Last price | MCap rank | minNotional | "
            "Our size | Sizing |",
            "|---|------|-------------------|------------|-----------|-------------|"
            "----------|--------|",
        ]
        for i, p in enumerate(self.qualified, 1):
            rank = str(p.market_cap_rank) if p.market_cap_rank else "?"
            lines.append(
                f"| {i} | {p.symbol} | {p.quote_volume_24h:,.0f} | {p.last_price:g} | "
                f"{rank} | {p.min_notional:g} | {p.position_size:.2f} | "
                f"{'OK' if p.notional_ok else 'TOO SMALL'} |"
            )
        lines += ["", "## Excluded", ""]
        for reason, count in sorted(self.excluded.items()):
            lines.append(f"- {count} pair(s): {reason}")
        if self.notes:
            lines += ["", "## Notes", ""]
            lines += [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


def fetch_market_cap_ranks(timeout: float = 10.0) -> dict[str, int] | None:
    """Base-asset ticker -> market cap rank from CoinGecko (top 250 covers any
    rank threshold we would realistically configure). None on any failure —
    the screener degrades gracefully."""
    import requests

    ranks: dict[str, int] = {}
    try:
        for page in (1, 2):
            resp = requests.get(
                COINGECKO_MARKETS_URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            for coin in resp.json():
                sym = coin["symbol"].upper()
                rank = coin.get("market_cap_rank")
                if rank and sym not in ranks:  # first (highest) rank wins on collisions
                    ranks[sym] = rank
    except Exception as exc:  # noqa: BLE001 — degrade, never block the screener
        log.warning("CoinGecko unavailable, screening on volume only: %s", exc)
        return None
    return ranks


class Screener:
    """Pure decision logic — exchange/tickers/ranks are passed in, so tests
    and the report CLI share the exact same code path."""

    def __init__(
        self,
        config: ScreenerConfig,
        *,
        exchange_info: dict[str, Any],
        tickers_24h: list[dict[str, Any]],
        market_cap_ranks: dict[str, int] | None,
        now: datetime | None = None,
    ):
        self.config = config
        self.symbols = {s["symbol"]: s for s in exchange_info["symbols"]}
        self.tickers = {t["symbol"]: t for t in tickers_24h}
        self.ranks = market_cap_ranks
        self.now = now or datetime.now(tz=UTC)

    def run(self) -> ScreenerReport:
        cfg = self.config
        report = ScreenerReport(
            generated_at=self.now.strftime("%Y-%m-%d %H:%M UTC"), config=cfg
        )
        if self.ranks is None:
            report.notes.append(
                "CoinGecko unreachable — market-cap criterion NOT applied this run "
                "(volume-only screening)."
            )

        def exclude(reason: str) -> None:
            report.excluded[reason] = report.excluded.get(reason, 0) + 1

        candidates: list[PairVerdict] = []
        for symbol, info in self.symbols.items():
            base = info["baseAsset"]
            if info["quoteAsset"] != cfg.quote_asset:
                continue  # not part of our universe at all — not worth counting
            if info["status"] != "TRADING" or not info.get("isSpotTradingAllowed", True):
                exclude("not in TRADING status / spot disabled")
                continue
            base_reason = excluded_base_reason(base, cfg)
            if base_reason is not None:
                exclude(base_reason)
                continue
            ticker = self.tickers.get(symbol)
            if ticker is None:
                exclude("no 24h ticker data")
                continue
            volume = float(ticker["quoteVolume"])
            if volume < cfg.min_24h_quote_volume:
                exclude(f"24h volume below {cfg.min_24h_quote_volume:,.0f} USDT")
                continue
            # D-38: behavioural stablecoin check. The name-based filter above
            # cannot catch a peg that isn't called *USD*, and a pair that never
            # moves can only pay fees under a breakout rule.
            low = float(ticker["lowPrice"])
            range_pct = (float(ticker["highPrice"]) / low - 1) * 100 if low > 0 else 0.0
            if range_pct < cfg.min_24h_range_pct:
                exclude(
                    f"24h range {range_pct:.2f}% below {cfg.min_24h_range_pct:g}% "
                    "(de-facto peg — nothing to trend-trade)"
                )
                continue
            rank = self.ranks.get(base) if self.ranks is not None else None
            if self.ranks is not None and rank is not None and rank > cfg.max_market_cap_rank:
                exclude(f"market-cap rank beyond top {cfg.max_market_cap_rank}")
                continue
            filters = parse_filters(info)
            verdict = PairVerdict(
                symbol=symbol,
                base=base,
                quote_volume_24h=volume,
                last_price=float(ticker["lastPrice"]),
                min_notional=float(filters.min_notional),
                position_size=cfg.capital_usdt * 0.10,  # D-08: 10% per coin
                market_cap_rank=rank,
            )
            if not verdict.notional_ok:
                exclude(
                    f"minNotional {verdict.min_notional:g} exceeds our "
                    f"{verdict.position_size:.2f} USDT position size (D-08)"
                )
                continue
            candidates.append(verdict)

        candidates.sort(key=lambda p: p.quote_volume_24h, reverse=True)
        dropped = len(candidates) - cfg.max_pairs
        if dropped > 0:
            report.excluded[f"beyond top {cfg.max_pairs} by volume"] = dropped
        report.qualified = candidates[: cfg.max_pairs]
        if self.ranks is not None:
            unknown = [p.symbol for p in report.qualified if p.market_cap_rank is None]
            if unknown:
                report.notes.append(
                    "Rank unknown to CoinGecko (kept, flagged '?'): " + ", ".join(unknown)
                )
        return report


def main() -> int:
    """CLI: python -m trader.screener [--data-dir DIR] — the S3 investor report."""
    import argparse

    from trader.config import load_config
    from trader.marketdata import BinancePublicData

    parser = argparse.ArgumentParser(description="Run the pair screener and write the report")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})

    print("Fetching exchange info, 24h tickers (Binance) and market caps (CoinGecko)...")
    public = BinancePublicData()
    report = Screener(
        config.screener,
        exchange_info=public.exchange_info(),
        tickers_24h=public.ticker_24hr(),
        market_cap_ranks=fetch_market_cap_ranks(),
    ).run()

    md = report.to_markdown()
    print("\n" + md)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    out = config.reports_dir / f"screener-{datetime.now(tz=UTC):%Y-%m-%d}.md"
    out.write_text(md)
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
