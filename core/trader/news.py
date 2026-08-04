"""News headline ingestion (Sprint 5, D-23a; multi-source since D-42c).

Stores crypto headlines timestamped in the journal's ``news_items`` table
(schema v3), from two kinds of source:

- **Publisher RSS feeds** (default): CoinDesk, Cointelegraph, Decrypt. Keyless,
  effectively unthrottled, and crypto-specific rather than a general news crawl.
- **GDELT 2.0 DOC API** (``ArtList`` mode): the original source, kept as an
  optional extra because its breadth is genuinely wider.

GDELT became the default's *supplement* rather than its backbone because it
rate-limited this deployment's home IP continuously from 2026-07-29 to at least
2026-08-04 — six days without a single successful cycle, where earlier 429s had
recovered within a cycle or two. It was verified as IP-level throttling
(identical 429 from two different machines behind the same address, on both the
production query and a trivial one, under three different user agents), so no
amount of backoff on our side clears it. D-42(c) authorised replacing the feed.

Truthful-attribution contract (D-23): nothing trades on this data. Headlines
exist so the S6 dashboard can show what the news cycle looked like around each
trade — context, not cause. The strategy path never reads this table.

Runs inside the core service as a periodic asyncio task (default: every 15
minutes). One failing source never blocks the others, and a failing cycle never
takes the trading loop down.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from trader.journal import Journal, utcnow
from trader.newscounts import DEFAULT_QUERY, GDELT_DOC_URL

log = logging.getLogger(__name__)

# Keyless publisher feeds. Kept short and mainstream on purpose: this is
# dashboard context, and a long tail of niche feeds would add failure modes
# without adding signal.
DEFAULT_RSS_FEEDS = (
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
)


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


def _text(element, *names: str) -> str | None:
    """First non-empty child matching any of *names*, namespace-insensitive.
    Feeds disagree constantly about namespaces; matching on the local tag name
    is what makes one parser work for RSS 2.0 and Atom alike."""
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in names:
            if child.text and child.text.strip():
                return child.text.strip()
            # Atom links carry the URL in an attribute, not the body.
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return None


def _iso_published(raw: str | None) -> str | None:
    """RSS/Atom timestamps normalised to ISO 8601 UTC.

    Feeds emit RFC 822 (``Mon, 03 Aug 2026 12:00:00 +0000``) or ISO; GDELT emits
    ``20260803T120000Z``. The column is display-only — every query orders by
    ``seen_at`` — so an unparseable value is stored verbatim rather than dropped.
    """
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw).astimezone(UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(raw).astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        pass
    if re.fullmatch(r"\d{8}T\d{6}Z", raw):  # GDELT's own compact form
        return (
            datetime.strptime(raw, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=UTC)
            .isoformat(timespec="seconds")
        )
    return raw


class RssHeadlines:
    """Publisher RSS/Atom feeds — the D-42(c) replacement for GDELT.

    Emits the same dict shape as ``GdeltHeadlines.fetch`` so ``NewsIngestor``
    stays source-agnostic. Each feed is fetched independently: a publisher
    changing its URL or serving malformed XML must cost us that feed only, not
    the cycle.
    """

    def __init__(self, feeds: tuple[str, ...] = DEFAULT_RSS_FEEDS, *, timeout: float = 20.0):
        self.feeds = feeds
        self.timeout = timeout

    def fetch(self) -> list[dict]:
        import requests

        out: list[dict] = []
        for feed in self.feeds:
            try:
                resp = requests.get(
                    feed,
                    timeout=self.timeout,
                    # Some publishers 403 an unrecognised client outright.
                    headers={"User-Agent": "marnar-trader/1.0 (+news context, D-23)"},
                )
                resp.raise_for_status()
                out.extend(self.parse(resp.content, feed))
            except Exception as exc:  # noqa: BLE001 — isolate per feed
                log.warning("news: feed %s failed (skipped this cycle): %s", feed, exc)
        return out

    @staticmethod
    def parse(payload: bytes, feed_url: str = "") -> list[dict]:
        """RSS 2.0 ``<item>`` and Atom ``<entry>`` into article dicts."""
        root = ElementTree.fromstring(payload)
        domain = ""
        match = re.match(r"https?://([^/]+)", feed_url)
        if match:
            domain = match.group(1).removeprefix("www.")

        articles = []
        for node in root.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in ("item", "entry"):
                continue
            title = _text(node, "title")
            url = _text(node, "link")
            if not title or not url:
                continue
            articles.append(
                {
                    "url": url,
                    "title": title,
                    "domain": domain,
                    "seendate": _iso_published(
                        _text(node, "pubDate", "published", "updated", "date")
                    ),
                }
            )
        return articles


class NewsIngestor:
    """Aggregates every configured source; one bad source is not a bad cycle."""

    def __init__(
        self,
        journal: Journal,
        client: GdeltHeadlines | None = None,
        *,
        sources: list | None = None,
    ):
        self.journal = journal
        # Back-compat: an explicitly passed client stays the only source, which
        # is what the existing tests and callers expect.
        if sources is not None:
            self.sources = sources
        elif client is not None:
            self.sources = [client]
        else:
            self.sources = [RssHeadlines(), GdeltHeadlines()]
        self.client = self.sources[0]

    def _collect(self) -> list[dict]:
        articles: list[dict] = []
        failures = 0
        for source in self.sources:
            try:
                articles.extend(source.fetch())
            except Exception as exc:  # noqa: BLE001 — isolate per source
                failures += 1
                log.warning(
                    "news: source %s failed (other sources continue): %s",
                    type(source).__name__,
                    exc,
                )
        if failures == len(self.sources) and self.sources:
            raise RuntimeError(f"all {failures} news sources failed this cycle")
        return articles

    def ingest_once(self) -> int:
        """One fetch-and-store cycle; returns how many NEW headlines landed."""
        articles = self._collect()
        added = 0
        for a in articles:
            url, title = a.get("url"), (a.get("title") or "").strip()
            if not url or not title:
                continue
            # QA1-06: the feed is an external crawler — only http(s) URLs may
            # reach the UI's clickable links (a javascript: URL would execute).
            if not url.startswith(("http://", "https://")):
                continue
            if self.journal.record_news_item(
                url=url,
                title=title,
                source=a.get("domain"),
                # Sources disagree on timestamp format; store one (D-42c).
                published=_iso_published(a.get("seendate")),
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
