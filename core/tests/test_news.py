"""News headline ingestion (D-23a): storage, dedupe, resilience."""

from __future__ import annotations

import pytest

from trader.news import NewsIngestor, RssHeadlines


class FakeHeadlines:
    def __init__(self, articles):
        self.articles = articles
        self.calls = 0

    def fetch(self, *args, **kwargs):
        self.calls += 1
        return self.articles


ARTICLES = [
    {"url": "https://x.test/a", "title": "Bitcoin does a thing", "domain": "x.test",
     "seendate": "20260716T100000Z"},
    {"url": "https://y.test/b", "title": "ETF flows spike", "domain": "y.test",
     "seendate": "20260716T101500Z"},
    {"url": "https://x.test/a", "title": "Bitcoin does a thing", "domain": "x.test",
     "seendate": "20260716T100000Z"},  # duplicate in the same batch
    {"url": "", "title": "no url — dropped"},
    {"url": "https://z.test/c", "title": "  "},  # no title — dropped
]


def test_ingest_stores_and_dedupes(journal):
    ingestor = NewsIngestor(journal, FakeHeadlines(ARTICLES))
    assert ingestor.ingest_once() == 2
    assert ingestor.ingest_once() == 0  # second cycle: all known
    rows = journal.news_since("1970")
    assert {r["url"] for r in rows} == {"https://x.test/a", "https://y.test/b"}
    assert all(r["seen_at"] for r in rows)


def test_news_since_filters_by_time(journal):
    NewsIngestor(journal, FakeHeadlines(ARTICLES)).ingest_once()
    assert journal.news_since("2999-01-01") == []


def test_strategy_tables_untouched_by_news(journal):
    """D-23 truthful attribution: news lands in its own table only."""
    NewsIngestor(journal, FakeHeadlines(ARTICLES)).ingest_once()
    assert journal.positions() == []
    assert journal.open_orders() == []


@pytest.mark.parametrize("bad", [Exception("boom"), RuntimeError("gdelt down")])
def test_fetch_failure_propagates_for_caller_retry(journal, bad):
    class Exploding:
        def fetch(self, *a, **k):
            raise bad

    with pytest.raises(type(bad)):
        NewsIngestor(journal, Exploding()).ingest_once()


def test_non_http_urls_are_dropped_at_ingest(journal):
    """QA1-06: a javascript: URL from the crawler must never become a link."""
    evil = [
        {"url": "javascript:alert(1)", "title": "Click me", "domain": "x"},
        {"url": "https://example.com/ok", "title": "Fine article", "domain": "x"},
    ]
    assert NewsIngestor(journal, FakeHeadlines(evil)).ingest_once() == 1
    (item,) = journal.news_since("1970")
    assert item["url"] == "https://example.com/ok"


# -- RSS source (D-42c) ------------------------------------------------------------

RSS_2_0 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Feed</title>
  <item>
    <title>Bitcoin rallies</title>
    <link>https://pub.test/one</link>
    <pubDate>Mon, 03 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Ether follows</title>
    <link>https://pub.test/two</link>
    <pubDate>Mon, 03 Aug 2026 13:30:00 -0300</pubDate>
  </item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom headline</title>
    <link href="https://atom.test/x"/>
    <updated>2026-08-03T09:15:00Z</updated>
  </entry>
</feed>"""


def test_parses_rss_2_0_with_timezone_normalisation():
    articles = RssHeadlines.parse(RSS_2_0, "https://www.pub.test/rss")
    assert [a["title"] for a in articles] == ["Bitcoin rallies", "Ether follows"]
    assert articles[0]["url"] == "https://pub.test/one"
    assert articles[0]["seendate"] == "2026-08-03T12:00:00+00:00"
    # -0300 is 16:30 UTC — a feed's local time must not be stored as if it were UTC.
    assert articles[1]["seendate"] == "2026-08-03T16:30:00+00:00"
    assert articles[0]["domain"] == "pub.test"


def test_parses_atom_where_the_url_is_an_attribute():
    articles = RssHeadlines.parse(ATOM, "https://atom.test/feed")
    assert articles == [
        {
            "url": "https://atom.test/x",
            "title": "Atom headline",
            "domain": "atom.test",
            "seendate": "2026-08-03T09:15:00+00:00",
        }
    ]


def test_items_without_title_or_link_are_skipped():
    payload = b"""<rss><channel>
      <item><title>No link</title></item>
      <item><link>https://pub.test/no-title</link></item>
      <item><title>Good</title><link>https://pub.test/good</link></item>
    </channel></rss>"""
    assert [a["url"] for a in RssHeadlines.parse(payload)] == ["https://pub.test/good"]


def test_unparseable_date_is_kept_verbatim_not_dropped():
    payload = b"""<rss><channel><item>
      <title>T</title><link>https://p.test/1</link><pubDate>sometime tuesday</pubDate>
    </item></channel></rss>"""
    assert RssHeadlines.parse(payload)[0]["seendate"] == "sometime tuesday"


def test_gdelt_compact_timestamps_normalise_too(journal):
    """GDELT emits 20260716T100000Z; both sources must land in one format."""
    NewsIngestor(journal, FakeHeadlines(ARTICLES)).ingest_once()
    published = {r["published"] for r in journal.news_since("1970")}
    assert published == {"2026-07-16T10:00:00+00:00", "2026-07-16T10:15:00+00:00"}


# -- multi-source aggregation ------------------------------------------------------


def test_sources_are_aggregated(journal):
    a = FakeHeadlines([ARTICLES[0]])
    b = FakeHeadlines([ARTICLES[1]])
    assert NewsIngestor(journal, sources=[a, b]).ingest_once() == 2
    assert a.calls == 1 and b.calls == 1


def test_one_dead_source_does_not_stop_the_others(journal):
    """The GDELT outage is exactly this case: it must degrade, not fail."""

    class Dead:
        def fetch(self):
            raise RuntimeError("429 Too Many Requests")

    ingestor = NewsIngestor(journal, sources=[Dead(), FakeHeadlines(ARTICLES)])
    assert ingestor.ingest_once() == 2


def test_all_sources_failing_raises_for_caller_retry(journal):
    class Dead:
        def fetch(self):
            raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="all 2 news sources failed"):
        NewsIngestor(journal, sources=[Dead(), Dead()]).ingest_once()


def test_default_sources_put_rss_first(journal):
    """GDELT rate-limited this deployment for six days; RSS carries the feature
    now and GDELT is the supplement."""
    ingestor = NewsIngestor(journal)
    assert isinstance(ingestor.sources[0], RssHeadlines)
    assert [type(s).__name__ for s in ingestor.sources] == ["RssHeadlines", "GdeltHeadlines"]
