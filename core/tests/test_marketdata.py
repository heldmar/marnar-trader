"""CandleStore + KlineDownloader — offline, against a fake klines API."""

from __future__ import annotations

from trader.marketdata import INTERVAL_MS, Candle, CandleStore, KlineDownloader

MIN = INTERVAL_MS["1m"]
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC


def make_candle(open_time: int, close: float = 100.0) -> Candle:
    return Candle(
        open_time=open_time,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10.0,
        quote_volume=1000.0,
        trades=5,
    )


class FakeKlinesAPI:
    """Serves a fixed minute-candle history like GET /api/v3/klines."""

    def __init__(self, start_ms: int, count: int):
        self.candles = [make_candle(start_ms + i * MIN, 100.0 + i) for i in range(count)]
        self.calls = 0

    def klines(self, symbol, interval, *, start_ms, limit=1000):
        self.calls += 1
        rows = []
        for c in self.candles:
            if c.open_time >= start_ms and len(rows) < limit:
                rows.append(
                    [c.open_time, str(c.open), str(c.high), str(c.low), str(c.close),
                     str(c.volume), c.open_time + MIN - 1, str(c.quote_volume), c.trades,
                     "0", "0", "0"]
                )
        return rows


def test_store_roundtrip_and_coverage(tmp_path):
    store = CandleStore(tmp_path)
    candles = [make_candle(T0 + i * MIN, 50.0 + i) for i in range(5)]
    assert store.append("BTCUSDT", "1m", candles) == 5

    back = store.read("BTCUSDT", "1m")
    assert back == candles
    assert store.coverage("BTCUSDT", "1m") == (T0, T0 + 4 * MIN, 5)
    assert store.coverage("ETHUSDT", "1m") is None


def test_store_append_dedupes_and_merges(tmp_path):
    store = CandleStore(tmp_path)
    store.append("BTCUSDT", "1m", [make_candle(T0), make_candle(T0 + MIN)])
    # Overlapping append: one duplicate (updated), one new.
    added = store.append("BTCUSDT", "1m", [make_candle(T0 + MIN, 999.0), make_candle(T0 + 2 * MIN)])
    assert added == 1
    back = store.read("BTCUSDT", "1m")
    assert len(back) == 3
    assert back[1].close == 999.0  # newer data wins


def test_store_partitions_by_month_and_reads_range(tmp_path):
    store = CandleStore(tmp_path)
    jan_31 = 1_738_367_940_000  # 2025-01-31 23:59 UTC
    feb_1 = jan_31 + MIN
    store.append("BTCUSDT", "1m", [make_candle(jan_31), make_candle(feb_1)])
    files = sorted(p.name for p in (tmp_path / "BTCUSDT" / "1m").glob("*.parquet"))
    assert files == ["2025-01.parquet", "2025-02.parquet"]
    assert [c.open_time for c in store.read("BTCUSDT", "1m", start_ms=feb_1)] == [feb_1]
    assert [c.open_time for c in store.read("BTCUSDT", "1m", end_ms=jan_31)] == [jan_31]


def test_downloader_backfills_full_range(tmp_path):
    api = FakeKlinesAPI(T0, 2500)
    now = T0 + 2500 * MIN
    store = CandleStore(tmp_path)
    dl = KlineDownloader(api, store, sleep_s=0, now_ms=now)
    added = dl.backfill("BTCUSDT", "1m", days=3650)
    assert added == 2500  # paged over 3 requests
    assert api.calls >= 3
    assert store.coverage("BTCUSDT", "1m") == (T0, T0 + 2499 * MIN, 2500)


def test_downloader_resumes_from_coverage(tmp_path):
    api = FakeKlinesAPI(T0, 1500)
    now = T0 + 1500 * MIN
    store = CandleStore(tmp_path)
    KlineDownloader(api, store, sleep_s=0, now_ms=T0 + 1000 * MIN).backfill(
        "BTCUSDT", "1m", days=3650
    )
    first_calls = api.calls
    added = KlineDownloader(api, store, sleep_s=0, now_ms=now).backfill(
        "BTCUSDT", "1m", days=3650
    )
    assert added == 500  # only the new tail, no re-download
    assert api.calls == first_calls + 1
    assert store.coverage("BTCUSDT", "1m")[2] == 1500


def test_downloader_never_stores_forming_candle(tmp_path):
    api = FakeKlinesAPI(T0, 10)
    # "now" is mid-way through the last candle — it must not be stored.
    now = T0 + 9 * MIN + 30_000
    store = CandleStore(tmp_path)
    KlineDownloader(api, store, sleep_s=0, now_ms=now).backfill("BTCUSDT", "1m", days=3650)
    assert store.coverage("BTCUSDT", "1m")[2] == 9
