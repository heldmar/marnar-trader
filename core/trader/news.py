"""News headline ingestion (Sprint 5, D-23a).

Fetches recent crypto headlines from the GDELT 2.0 DOC API (``ArtList`` mode —
same free, keyless source as the news-count backfiller) and stores them
timestamped in the journal's ``news_items`` table (schema v3).

Truthful-attribution contract (D-23): nothing trades on this data. Headlines
exist so the S6 dashboard can show what the news cycle looked like around each
trade — context, not cause. The strategy path never reads this table.

Runs inside the core service as a periodic asyncio task (default: every 15
minutes, matching GDELT's bucket size). Failures are logged and retried next
cycle — news is never allowed to take the trading loop down.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from trader.journal import Journal, utcnow
from trader.newscounts import DEFAULT_QUERY, GDELT_DOC_URL

log = logging.getLogger(__name__)


class GdeltHeadlines:
    """ArtList-mode client: latest articles matching the crypto query."""

    def __init__(self, *, timeout: float = 30.0):
        self.timeout = timeout

    def fetch(self, query: str = DEFAULT_QUERY, *, span_minutes: int = 60, max_records: int = 50):
        """Recent articles as dicts: url / title / domain / seendate.
        Raises on transport errors; the caller decides retry policy."""
        import requests

        start = datetime.now(tz=UTC) - timedelta(minutes=span_minutes)
        resp = requests.get(
            GDELT_DOC_URL,
            params={
                "query": f"{query} sourcelang:english",
                "mode": "ArtList",
                "format": "json",
                "maxrecords": str(max_records),
                "sort": "DateDesc",
                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"GDELT non-JSON response: {resp.text[:120]!r}") from exc
        return payload.get("articles", [])


class NewsIngestor:
    def __init__(self, journal: Journal, client: GdeltHeadlines | None = None):
        self.journal = journal
        self.client = client or GdeltHeadlines()

    def ingest_once(self) -> int:
        """One fetch-and-store cycle; returns how many NEW headlines landed."""
        articles = self.client.fetch()
        added = 0
        for a in articles:
            url, title = a.get("url"), (a.get("title") or "").strip()
            if not url or not title:
                continue
            if self.journal.record_news_item(
                url=url,
                title=title,
                source=a.get("domain"),
                published=a.get("seendate"),
                seen_at=utcnow(),
            ):
                added += 1
        if added:
            log.info("news: stored %d new headlines", added)
        return added

    async def run(self, *, every_seconds: float = 900.0) -> None:
        while True:
            try:
                self.ingest_once()
            except Exception as exc:
                log.warning("news ingestion cycle failed (retrying next cycle): %s", exc)
            await asyncio.sleep(every_seconds)
