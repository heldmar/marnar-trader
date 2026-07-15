"""Historical news-volume counts for the D-23 news-spike rule (numeric only).

Source: GDELT 2.0 DOC API ``timelinevolraw`` — raw worldwide article counts
matching a query, 15-minute buckets. Free, keyless, historical. Constraints
found empirically (2026-07-14): ~1 request per 5 s per IP (hard throttle with
a long penalty box), and 15m resolution only over short spans (a 30-day span
degrades to daily) — hence small chunks, politely paced. A full 730-day
backfill is ~250 requests ≈ 25 minutes.

Storage: single Parquet per query slug at /data/news/{slug}.parquet
(ts_ms, count), merged + deduped on write — same atomic tmp+rename discipline
as the candle store. Resumable from the stored max timestamp.

The counts are market-wide by design ("bitcoin OR cryptocurrency ..."), not
per-pair: the rule pauses *entries* when the whole news cycle spikes (D-23),
it does not pick coins.
"""

from __future__ import annotations

import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
BUCKET_MS = 15 * 60_000
DEFAULT_QUERY = '(bitcoin OR ethereum OR cryptocurrency OR crypto OR binance)'
DEFAULT_SLUG = "crypto"

_SCHEMA = pa.schema([("ts_ms", pa.int64()), ("count", pa.int64())])


class GdeltClient:
    """Minimal, politely-paced GDELT DOC client. One query mode, no key."""

    def __init__(
        self,
        *,
        sleep_s: float = 5.5,
        timeout: float = 30.0,
        max_retries: int = 8,
        backoff_s: float = 75.0,
    ):
        self.sleep_s = sleep_s
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_s = backoff_s  # GDELT's penalty box is minutes, not seconds
        self._last_request = 0.0

    def _pace(self) -> None:
        wait = self._last_request + self.sleep_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def timeline_counts(self, query: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """15m (ts_ms, raw article count) buckets for [start_ms, end_ms].
        Retries throttle responses (429 / plain-text limit message) with a long
        backoff; raises RuntimeError once retries are exhausted or on a
        degraded-resolution response (chunk too large — a config bug, not
        weather)."""
        import requests

        for attempt in range(self.max_retries + 1):
            try:
                return self._timeline_counts_once(query, start_ms, end_ms)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                throttled = status == 429
            except RuntimeError as exc:
                if "degraded resolution" in str(exc):
                    raise
                throttled = "limit requests" in str(exc).lower()
            if not throttled or attempt == self.max_retries:
                raise RuntimeError(
                    f"GDELT still throttling after {attempt} retries "
                    f"({self.backoff_s:.0f}s apart) — try again later"
                )
            time.sleep(self.backoff_s)
        raise AssertionError("unreachable")

    def _timeline_counts_once(
        self, query: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, int]]:
        import requests

        self._pace()
        fmt = "%Y%m%d%H%M%S"
        from datetime import UTC, datetime

        resp = requests.get(
            GDELT_DOC_URL,
            params={
                "query": query,
                "mode": "timelinevolraw",
                "format": "json",
                "startdatetime": datetime.fromtimestamp(start_ms / 1000, tz=UTC).strftime(fmt),
                "enddatetime": datetime.fromtimestamp(end_ms / 1000, tz=UTC).strftime(fmt),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:  # throttle message is plain text
            raise RuntimeError(f"GDELT non-JSON response: {resp.text[:120]!r}") from exc
        if not payload:
            return []
        resolution = payload.get("query_details", {}).get("date_resolution")
        if resolution != "15m":
            raise RuntimeError(f"GDELT degraded resolution {resolution!r} — chunk too large")
        out: list[tuple[int, int]] = []
        for point in payload["timeline"][0]["data"]:
            ts = int(
                datetime.strptime(point["date"], "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=UTC)
                .timestamp()
                * 1000
            )
            out.append((ts, int(point["value"])))
        return out


class NewsCountStore:
    """(ts_ms, count) series per query slug, one Parquet file, atomic writes."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, slug: str) -> Path:
        return self.root / f"{slug}.parquet"

    def read(self, slug: str) -> dict[int, int]:
        path = self._path(slug)
        if not path.exists():
            return {}
        table = pq.read_table(path)
        return dict(zip(table["ts_ms"].to_pylist(), table["count"].to_pylist(), strict=True))

    def append(self, slug: str, points: list[tuple[int, int]]) -> int:
        merged = self.read(slug)
        before = len(merged)
        merged.update(dict(points))
        rows = sorted(merged.items())
        table = pa.Table.from_pydict(
            {"ts_ms": [r[0] for r in rows], "count": [r[1] for r in rows]}, schema=_SCHEMA
        )
        path = self._path(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(path)
        return len(merged) - before

    def coverage(self, slug: str) -> tuple[int, int, int] | None:
        data = self.read(slug)
        if not data:
            return None
        return (min(data), max(data), len(data))


class NewsBackfiller:
    """Chunked, resumable backfill — same shape as KlineDownloader."""

    def __init__(
        self,
        client: GdeltClient,
        store: NewsCountStore,
        *,
        chunk_days: int = 3,
        now_ms: int | None = None,
    ):
        self.client = client
        self.store = store
        self.chunk_ms = chunk_days * 86_400_000
        self._now_ms = now_ms

    def backfill(
        self, query: str, slug: str, *, days: int = 730, progress=None
    ) -> int:
        now = self._now_ms if self._now_ms is not None else int(time.time() * 1000)
        start = now - days * 86_400_000
        cov = self.store.coverage(slug)
        if cov is not None and cov[1] >= start:
            start = cov[1] + BUCKET_MS  # resume forward
        added = 0
        while start < now:
            end = min(start + self.chunk_ms, now)
            points = self.client.timeline_counts(query, start, end)
            if points:
                added += self.store.append(slug, points)
            if progress:
                from datetime import UTC, datetime

                ts = datetime.fromtimestamp(end / 1000, tz=UTC)
                progress(f"  {slug}: up to {ts:%Y-%m-%d} ({added} buckets)")
            start = end
        return added


class CandleCountLookup:
    """Mapping-like view over 15m buckets for candles of any interval: a
    candle reads the bucket containing its open. Satisfies the ``counts``
    contract of NewsSpikePause (only ``.get`` is used)."""

    def __init__(self, counts: dict[int, int]):
        self._counts = counts

    def get(self, open_time_ms: int, default: int = 0) -> int:
        return self._counts.get(open_time_ms - open_time_ms % BUCKET_MS, default)


def main() -> int:
    import argparse

    from trader.config import load_config

    parser = argparse.ArgumentParser(description="Backfill GDELT news counts (D-23)")
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--slug", default=DEFAULT_SLUG)
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    config = load_config()
    if args.data_dir:
        config = config.model_copy(update={"data_dir": Path(args.data_dir)})
    store = NewsCountStore(config.data_dir / "news")
    backfiller = NewsBackfiller(GdeltClient(), store)
    print(f"[{args.slug}] backfilling {args.days}d of news counts for {args.query!r}...")
    added = backfiller.backfill(args.query, args.slug, days=args.days, progress=print)
    print(f"[{args.slug}] done: +{added} buckets, coverage {store.coverage(args.slug)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
