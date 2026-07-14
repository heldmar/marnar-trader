"""Historical market data — download, storage, retrieval (Sprint 3).

Candles come from Binance **production** public market-data endpoints: they are
read-only, need no API key, and the testnet has neither real volume nor real
history (fees there are zero too — see T-02). No credential ever touches this
module; trading still goes exclusively through the testnet gateway.

Storage is Parquet on the data volume (arch §1), partitioned by month:

    /data/candles/{SYMBOL}/{interval}/{YYYY-MM}.parquet

Downloads are resumable: the downloader asks the store how far coverage goes
and continues from there, so a crash mid-backfill costs nothing (no-UPS
discipline applies to long downloads too).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from binance.spot import Spot

PROD_DATA_URL = "https://api.binance.com"

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_SCHEMA = pa.schema(
    [
        ("open_time", pa.int64()),  # epoch ms, UTC
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),  # base asset
        ("quote_volume", pa.float64()),
        ("trades", pa.int64()),
    ]
)


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int


def candle_from_kline(row: list[Any]) -> Candle:
    """Binance kline row: [openTime, o, h, l, c, vol, closeTime, quoteVol, trades, ...]."""
    return Candle(
        open_time=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        quote_volume=float(row[7]),
        trades=int(row[8]),
    )


class MarketDataClient(Protocol):
    """The slice of public market data S3 needs (no auth, read-only)."""

    def klines(
        self, symbol: str, interval: str, *, start_ms: int, limit: int
    ) -> list[list[Any]]: ...
    def exchange_info(self) -> dict[str, Any]: ...
    def ticker_24hr(self) -> list[dict[str, Any]]: ...


class BinancePublicData:
    """Unauthenticated client against production public endpoints."""

    def __init__(self) -> None:
        self._client = Spot(base_url=PROD_DATA_URL)

    def klines(
        self, symbol: str, interval: str, *, start_ms: int, limit: int = 1000
    ) -> list[list[Any]]:
        return self._client.klines(symbol, interval, startTime=start_ms, limit=limit)

    def exchange_info(self) -> dict[str, Any]:
        return self._client.exchange_info()

    def ticker_24hr(self) -> list[dict[str, Any]]:
        return self._client.ticker_24hr()


def _month_key(open_time_ms: int) -> str:
    return datetime.fromtimestamp(open_time_ms / 1000, tz=UTC).strftime("%Y-%m")


class CandleStore:
    """Monthly Parquet partitions; append merges + dedupes on open_time, then
    replaces the partition atomically (tmp file + rename) so a power cut never
    leaves a half-written month."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, symbol: str, interval: str) -> Path:
        return self.root / symbol / interval

    def _partitions(self, symbol: str, interval: str) -> list[Path]:
        d = self._dir(symbol, interval)
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    def append(self, symbol: str, interval: str, candles: list[Candle]) -> int:
        by_month: dict[str, list[Candle]] = {}
        for c in candles:
            by_month.setdefault(_month_key(c.open_time), []).append(c)

        written = 0
        for month, batch in sorted(by_month.items()):
            path = self._dir(symbol, interval) / f"{month}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            merged: dict[int, Candle] = {}
            if path.exists():
                merged = {c.open_time: c for c in self._read_file(path)}
            before = len(merged)
            merged.update({c.open_time: c for c in batch})
            written += len(merged) - before
            rows = [merged[t] for t in sorted(merged)]
            table = pa.Table.from_pylist(list(_as_dicts(rows)), schema=_SCHEMA)
            tmp = path.with_suffix(".parquet.tmp")
            pq.write_table(table, tmp, compression="zstd")
            tmp.replace(path)
        return written

    @staticmethod
    def _read_file(path: Path) -> list[Candle]:
        return [Candle(**row) for row in pq.read_table(path).to_pylist()]

    def read(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[Candle]:
        out: list[Candle] = []
        for path in self._partitions(symbol, interval):
            for c in self._read_file(path):
                if start_ms is not None and c.open_time < start_ms:
                    continue
                if end_ms is not None and c.open_time > end_ms:
                    continue
                out.append(c)
        return out

    def coverage(self, symbol: str, interval: str) -> tuple[int, int, int] | None:
        """(first_open_time, last_open_time, candle_count) or None if empty."""
        parts = self._partitions(symbol, interval)
        if not parts:
            return None
        first = self._read_file(parts[0])
        last = first if len(parts) == 1 else self._read_file(parts[-1])
        count = sum(pq.read_metadata(p).num_rows for p in parts)
        return (first[0].open_time, last[-1].open_time, count)


def _as_dicts(rows: list[Candle]):
    for c in rows:
        yield {
            "open_time": c.open_time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "quote_volume": c.quote_volume,
            "trades": c.trades,
        }


class KlineDownloader:
    """Backfills history through the public API, 1000 candles per request,
    paced under Binance's request-weight budget, resuming from store coverage."""

    def __init__(
        self,
        client: MarketDataClient,
        store: CandleStore,
        *,
        sleep_s: float = 0.15,
        now_ms: int | None = None,
    ):
        self.client = client
        self.store = store
        self.sleep_s = sleep_s
        self._now_ms = now_ms  # injectable for tests

    def backfill(
        self, symbol: str, interval: str = "1m", *, days: int = 730, progress=None
    ) -> int:
        step = INTERVAL_MS[interval]
        now = self._now_ms if self._now_ms is not None else int(time.time() * 1000)
        start = now - days * 86_400_000
        cov = self.store.coverage(symbol, interval)
        if cov is not None and cov[1] >= start:
            start = cov[1] + step  # resume where we left off
        added = 0
        while start < now:
            rows = self.client.klines(symbol, interval, start_ms=start, limit=1000)
            if not rows:
                break
            candles = [candle_from_kline(r) for r in rows]
            # The most recent candle may still be forming — never store it.
            candles = [c for c in candles if c.open_time + step <= now]
            if not candles:
                break
            added += self.store.append(symbol, interval, candles)
            start = candles[-1].open_time + step
            if progress:
                pct_ts = datetime.fromtimestamp(start / 1000, tz=UTC)
                progress(f"  {symbol} {interval}: up to {pct_ts:%Y-%m-%d %H:%M} ({added} candles)")
            if self.sleep_s:
                time.sleep(self.sleep_s)
        return added


def main() -> int:
    """CLI: python -m trader.marketdata SYMBOL [SYMBOL...] [--interval 1m] [--days 730]"""
    import argparse

    from trader.config import load_config

    parser = argparse.ArgumentParser(description="Backfill historical candles to Parquet")
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--interval", default="1m", choices=sorted(INTERVAL_MS))
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    store = CandleStore(config.candles_dir)
    downloader = KlineDownloader(BinancePublicData(), store)
    for symbol in args.symbols:
        print(f"[{symbol}] backfilling {args.days}d of {args.interval} candles...")
        added = downloader.backfill(symbol, args.interval, days=args.days, progress=print)
        cov = store.coverage(symbol, args.interval)
        print(f"[{symbol}] done: +{added} candles, coverage {cov and cov[2]} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
