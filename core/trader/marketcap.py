"""Historical market-cap ranks for the point-in-time screen (D-42b, D-43b).

Production's screener applies a top-N market-cap criterion
(``ScreenerConfig.max_market_cap_rank``). Reproducing it historically needs
market caps *as they stood* on each screening date, and that is the one input
this project could not source: CoinGecko serves no historical ranks on the free
tier, and the investor declined a paid subscription (D-43b).

What free sources do provide is a **year** of daily market-cap history per coin
— CoinGecko's ``market_chart/range`` returns the whole series in a single call.
Ranks are then derived by sorting all coins on each day. So the criterion can be
applied honestly to roughly the last four quarters and not before.

That partial coverage is the deliberate outcome. The alternative considered and
rejected was a proxy — today's circulating supply times each day's price — which
would produce a plausible-looking rank for every date in history and quietly
misstate exactly the assets whose supply schedules differ. D-41 established the
principle: fabricating the input would be worse than omitting it. Callers get
ranks where ranks are real and ``None`` where they are not, and
``trader.pit.screen_at`` skips the criterion whenever it gets ``None``.

Fetching is slow (one throttled call per coin, ~500 coins) so results are cached
on disk and reused. ``python -m trader.marketcap --data-dir DIR`` refreshes it.
"""

from __future__ import annotations

import json
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_RANGE_URL = "https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range"
DAY_MS = 86_400_000
# CoinGecko's public tier stops at 365 days and rejects anything older outright.
FREE_TIER_MAX_DAYS = 360


@dataclass(frozen=True, slots=True)
class Coin:
    coin_id: str
    symbol: str  # upper-case ticker, matches Binance base assets
    rank_today: int | None


def build_rank_history(
    histories: dict[str, list[tuple[int, float]]],
    *,
    symbol_of: dict[str, str],
) -> dict[int, dict[str, int]]:
    """Daily market caps per coin -> ``{day_ms: {SYMBOL: rank}}``.

    Rank is recomputed from scratch on each day by sorting that day's caps, so a
    coin that was 40th a year ago and 8th now is ranked correctly on both dates —
    which is the entire point of the exercise.

    Days are floored to midnight UTC. Where two coins share a ticker (they do),
    the larger cap wins, mirroring the live screener's first-rank-wins rule.
    """
    by_day: dict[int, dict[str, float]] = {}
    for coin_id, series in histories.items():
        symbol = symbol_of.get(coin_id)
        if symbol is None:
            continue
        for ts_ms, cap in series:
            if not cap:
                continue
            day = (int(ts_ms) // DAY_MS) * DAY_MS
            caps = by_day.setdefault(day, {})
            if cap > caps.get(symbol, 0.0):
                caps[symbol] = cap

    ranks: dict[int, dict[str, int]] = {}
    for day, caps in by_day.items():
        ordered = sorted(caps.items(), key=lambda kv: (-kv[1], kv[0]))
        ranks[day] = {sym: i for i, (sym, _) in enumerate(ordered, start=1)}
    return ranks


def ranks_at(rank_history: dict[int, dict[str, int]], as_of_ms: int) -> dict[str, int] | None:
    """Ranks known on the most recent day at or before *as_of_ms*.

    Returns None when the history does not reach back that far — the signal
    ``screen_at`` needs in order to skip the criterion rather than silently
    screen on stale ranks. Never looks forward: that would reintroduce exactly
    the hindsight the point-in-time harness exists to remove.
    """
    if not rank_history:
        return None
    days = sorted(rank_history)
    idx = bisect_right(days, as_of_ms) - 1
    if idx < 0:
        return None
    return rank_history[days[idx]]


class RankHistory:
    """Disk-cached rank history, safe to construct when the cache is absent."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.by_day: dict[int, dict[str, int]] = {}
        self.meta: dict = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text())
            self.meta = payload.get("meta", {})
            self.by_day = {
                int(day): ranks for day, ranks in payload.get("ranks", {}).items()
            }

    @property
    def covered(self) -> tuple[int, int] | None:
        return (min(self.by_day), max(self.by_day)) if self.by_day else None

    def at(self, as_of_ms: int) -> dict[str, int] | None:
        return ranks_at(self.by_day, as_of_ms)

    def for_dates(self, dates_ms: list[int]) -> dict[int, dict[str, int]]:
        """Ranks for each screening date that the history actually covers.
        Dates outside coverage are omitted, so ``trader.pit`` applies the
        criterion where it is real and skips it where it is not."""
        out = {}
        for date_ms in dates_ms:
            ranks = self.at(date_ms)
            if ranks:
                out[date_ms] = ranks
        return out

    def save(self, meta: dict | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": meta or self.meta,
            "ranks": {str(day): ranks for day, ranks in sorted(self.by_day.items())},
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)


# -- fetching ---------------------------------------------------------------------


def fetch_top_coins(pages: int = 2, *, timeout: float = 20.0) -> list[Coin]:
    import requests

    coins: list[Coin] = []
    for page in range(1, pages + 1):
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
        for row in resp.json():
            coins.append(
                Coin(
                    coin_id=row["id"],
                    symbol=row["symbol"].upper(),
                    rank_today=row.get("market_cap_rank"),
                )
            )
        time.sleep(3.0)
    return coins


def fetch_cap_history(
    coin_id: str,
    *,
    days: int = FREE_TIER_MAX_DAYS,
    timeout: float = 25.0,
    attempts: int = 5,
    backoff: float = 15.0,
) -> list[tuple[int, float]]:
    """One coin's daily market caps. Retries 429s with a growing wait — the free
    tier throttles aggressively and unpredictably, and dropping a coin on the
    first refusal silently biases the ranking (the coins that fail are not
    random with respect to how often they are queried)."""
    import requests

    now = int(time.time())
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = requests.get(
                COINGECKO_RANGE_URL.format(coin_id=coin_id),
                params={"vs_currency": "usd", "from": now - days * 86_400, "to": now},
                timeout=timeout,
            )
            if resp.status_code == 429:
                raise requests.HTTPError("429 rate limited", response=resp)
            resp.raise_for_status()
            return [(int(ts), float(cap)) for ts, cap in resp.json().get("market_caps", [])]
        except Exception as exc:  # noqa: BLE001 — retried below, re-raised if terminal
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def main() -> int:
    """CLI: build/refresh the cached rank history."""
    import argparse

    parser = argparse.ArgumentParser(description="Fetch historical market-cap ranks (D-43b)")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--pages", type=int, default=2, help="250 coins per page")
    parser.add_argument("--sleep", type=float, default=2.5, help="seconds between calls")
    args = parser.parse_args()

    out = Path(args.data_dir) / "marketcap-ranks.json"
    print(f"Fetching top {args.pages * 250} coins from CoinGecko...")
    coins = fetch_top_coins(args.pages)
    print(f"{len(coins)} coins")

    histories: dict[str, list[tuple[int, float]]] = {}
    symbol_of = {c.coin_id: c.symbol for c in coins}
    failed = 0
    for i, coin in enumerate(coins, 1):
        try:
            histories[coin.coin_id] = fetch_cap_history(coin.coin_id)
        except Exception as exc:  # noqa: BLE001 — one bad coin must not end the sweep
            failed += 1
            print(f"  [{i}/{len(coins)}] {coin.symbol}: FAILED ({exc})")
            time.sleep(args.sleep * 4)  # back off — usually a rate limit
            continue
        if i % 25 == 0:
            print(f"  [{i}/{len(coins)}] {coin.symbol}: {len(histories[coin.coin_id])} days")
        time.sleep(args.sleep)

    history = RankHistory(out)
    history.by_day = build_rank_history(histories, symbol_of=symbol_of)
    covered = history.covered
    history.save(
        meta={
            "source": "CoinGecko public API (free tier, 365-day limit)",
            "coins_requested": len(coins),
            "coins_fetched": len(histories),
            "coins_failed": failed,
            "built_at_ms": int(time.time() * 1000),
        }
    )
    print(f"\n{len(history.by_day)} days of ranks -> {out}")
    if covered:
        from datetime import UTC, datetime

        lo, hi = covered
        print(
            f"coverage {datetime.fromtimestamp(lo / 1000, tz=UTC):%Y-%m-%d} .. "
            f"{datetime.fromtimestamp(hi / 1000, tz=UTC):%Y-%m-%d}"
        )
    print(f"{failed} coins failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
