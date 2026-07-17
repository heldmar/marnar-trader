"""News headline ingestion (D-23a): storage, dedupe, resilience."""

from __future__ import annotations

import pytest

from trader.news import NewsIngestor


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
