"""GDELT news-count store, backfiller, and the 15m-bucket candle lookup
feeding the D-23 news-spike rule."""

from __future__ import annotations

import pytest

from trader.newscounts import (
    BUCKET_MS,
    CandleCountLookup,
    GdeltClient,
    NewsBackfiller,
    NewsCountStore,
)

T0 = 1_735_689_600_000  # aligned on 15m
DAY = 86_400_000


class FakeGdelt:
    """Returns one bucket per 15m in the requested range; records calls."""

    def __init__(self):
        self.calls: list[tuple[int, int]] = []

    def timeline_counts(self, query: str, start_ms: int, end_ms: int):
        self.calls.append((start_ms, end_ms))
        first = start_ms - start_ms % BUCKET_MS
        return [(t, 7) for t in range(first, end_ms, BUCKET_MS)]


class TestNewsCountStore:
    def test_append_merges_and_dedupes(self, tmp_path):
        store = NewsCountStore(tmp_path)
        assert store.append("crypto", [(T0, 5), (T0 + BUCKET_MS, 6)]) == 2
        assert store.append("crypto", [(T0, 9), (T0 + 2 * BUCKET_MS, 1)]) == 1
        data = store.read("crypto")
        assert data[T0] == 9  # newer value wins
        assert store.coverage("crypto") == (T0, T0 + 2 * BUCKET_MS, 3)

    def test_read_missing_slug(self, tmp_path):
        assert NewsCountStore(tmp_path).read("nope") == {}


class TestNewsBackfiller:
    def test_chunks_cover_range_and_resume(self, tmp_path):
        store = NewsCountStore(tmp_path)
        client = FakeGdelt()
        bf = NewsBackfiller(client, store, chunk_days=3, now_ms=T0 + 9 * DAY)
        added = bf.backfill("q", "crypto", days=9)
        assert added == 9 * DAY // BUCKET_MS
        assert len(client.calls) == 3  # 9 days / 3-day chunks
        # resume: nothing new to fetch except the tail after coverage
        client2 = FakeGdelt()
        bf2 = NewsBackfiller(client2, store, chunk_days=3, now_ms=T0 + 9 * DAY)
        assert bf2.backfill("q", "crypto", days=9) == 0

    def test_degraded_resolution_raises(self, tmp_path):
        class Degraded:
            def timeline_counts(self, query, start_ms, end_ms):
                raise RuntimeError("GDELT degraded resolution 'day'")

        bf = NewsBackfiller(Degraded(), NewsCountStore(tmp_path), now_ms=T0 + DAY)
        with pytest.raises(RuntimeError, match="degraded"):
            bf.backfill("q", "crypto", days=1)


class TestCandleCountLookup:
    def test_floors_candle_open_to_bucket(self):
        lookup = CandleCountLookup({T0: 42})
        assert lookup.get(T0) == 42
        assert lookup.get(T0 + 60_000) == 42  # 1m candle inside the bucket
        assert lookup.get(T0 + 14 * 60_000) == 42
        assert lookup.get(T0 + BUCKET_MS) == 0  # next bucket missing -> default

    def test_custom_default(self):
        assert CandleCountLookup({}).get(T0, 3) == 3


class TestGdeltClientParsing:
    def test_pace_is_lazy_first_call(self, monkeypatch):
        client = GdeltClient(sleep_s=999)
        slept: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
        client._pace()  # first call must not sleep
        assert slept == []
