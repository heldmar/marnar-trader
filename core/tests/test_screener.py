"""Pair screener — every D-09 criterion, the D-08 minNotional check, and the
CoinGecko degradation path. All offline on synthetic exchange data."""

from __future__ import annotations

from datetime import UTC, datetime

from trader.config import ScreenerConfig
from trader.screener import Screener

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def symbol_info(
    symbol: str,
    base: str,
    *,
    quote: str = "USDT",
    status: str = "TRADING",
    spot: bool = True,
    min_notional: str = "5.0",
):
    return {
        "symbol": symbol,
        "baseAsset": base,
        "quoteAsset": quote,
        "status": status,
        "isSpotTradingAllowed": spot,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": min_notional},
        ],
    }


def ticker(symbol: str, volume: float, price: float = 100.0, range_pct: float = 5.0):
    """``range_pct`` is the 24h high/low spread — the D-38 peg check. Defaults to
    a normal, comfortably-trending 5% so existing cases exercise their own
    criterion and not this one."""
    return {
        "symbol": symbol,
        "quoteVolume": str(volume),
        "lastPrice": str(price),
        "lowPrice": str(price),
        "highPrice": str(price * (1 + range_pct / 100)),
    }


def screen(symbols, tickers, *, ranks=None, config=None):
    cfg = config or ScreenerConfig(min_24h_quote_volume=1_000_000, max_pairs=5)
    return Screener(
        cfg,
        exchange_info={"symbols": symbols},
        tickers_24h=tickers,
        market_cap_ranks=ranks,
        now=NOW,
    ).run()


def test_happy_path_sorted_by_volume():
    report = screen(
        [symbol_info("BTCUSDT", "BTC"), symbol_info("ETHUSDT", "ETH")],
        [ticker("BTCUSDT", 2_000_000), ticker("ETHUSDT", 9_000_000)],
        ranks={"BTC": 1, "ETH": 2},
    )
    assert [p.symbol for p in report.qualified] == ["ETHUSDT", "BTCUSDT"]
    assert report.excluded == {}
    assert all(p.notional_ok for p in report.qualified)


def test_non_usdt_quotes_ignored_silently():
    report = screen(
        [symbol_info("BTCEUR", "BTC", quote="EUR"), symbol_info("BTCUSDT", "BTC")],
        [ticker("BTCEUR", 9_999_999), ticker("BTCUSDT", 2_000_000)],
        ranks={"BTC": 1},
    )
    assert [p.symbol for p in report.qualified] == ["BTCUSDT"]


def test_exclusion_reasons_are_counted():
    report = screen(
        [
            symbol_info("DEADUSDT", "DEAD", status="BREAK"),
            symbol_info("USDCUSDT", "USDC"),
            symbol_info("BTCUPUSDT", "BTCUP"),
            symbol_info("THINUSDT", "THIN"),
            symbol_info("NOTICKUSDT", "NOTICK"),
            symbol_info("BTCUSDT", "BTC"),
        ],
        [
            ticker("DEADUSDT", 9_000_000),
            ticker("USDCUSDT", 9_000_000),
            ticker("BTCUPUSDT", 9_000_000),
            ticker("THINUSDT", 5),
            ticker("BTCUSDT", 2_000_000),
        ],
        ranks={"BTC": 1},
    )
    assert [p.symbol for p in report.qualified] == ["BTCUSDT"]
    assert report.excluded["not in TRADING status / spot disabled"] == 1
    assert report.excluded["stablecoin or fiat base asset"] == 1
    # Pattern rule: unknown new stables with USD in the name are caught too.
    usd1 = screen(
        [symbol_info("USD1USDT", "USD1"), symbol_info("RLUSDUSDT", "RLUSD")],
        [ticker("USD1USDT", 9_000_000), ticker("RLUSDUSDT", 9_000_000)],
        ranks={"USD1": 24, "RLUSD": 50},
    )
    assert usd1.qualified == []
    assert usd1.excluded["stablecoin or fiat base asset"] == 2
    assert report.excluded["leveraged token (UP/DOWN/BULL/BEAR)"] == 1
    assert report.excluded["no 24h ticker data"] == 1
    assert sum(1 for k in report.excluded if "volume below" in k) == 1


def test_market_cap_rank_filter():
    report = screen(
        [symbol_info("BTCUSDT", "BTC"), symbol_info("MEMEUSDT", "MEME")],
        [ticker("BTCUSDT", 2_000_000), ticker("MEMEUSDT", 9_000_000)],
        ranks={"BTC": 1, "MEME": 900},
    )
    assert [p.symbol for p in report.qualified] == ["BTCUSDT"]
    assert any("market-cap rank" in k for k in report.excluded)


def test_unknown_rank_kept_but_flagged():
    report = screen(
        [symbol_info("NEWUSDT", "NEW")],
        [ticker("NEWUSDT", 2_000_000)],
        ranks={"BTC": 1},  # NEW not in CoinGecko's map
    )
    assert [p.symbol for p in report.qualified] == ["NEWUSDT"]
    assert report.qualified[0].market_cap_rank is None
    assert any("Rank unknown" in n for n in report.notes)


def test_coingecko_down_degrades_to_volume_only_with_note():
    report = screen(
        [symbol_info("MEMEUSDT", "MEME")],
        [ticker("MEMEUSDT", 9_000_000)],
        ranks=None,
    )
    assert [p.symbol for p in report.qualified] == ["MEMEUSDT"]
    assert any("volume-only" in n for n in report.notes)


def test_min_notional_unaffordable_at_pilot_size_is_excluded():
    # D-08: 10% of 150 USDT = 15; a 20-USDT minNotional pair is untradeable for us.
    report = screen(
        [symbol_info("BIGUSDT", "BIG", min_notional="20.0"), symbol_info("BTCUSDT", "BTC")],
        [ticker("BIGUSDT", 9_000_000), ticker("BTCUSDT", 2_000_000)],
        ranks={"BIG": 10, "BTC": 1},
    )
    assert [p.symbol for p in report.qualified] == ["BTCUSDT"]
    assert any("minNotional" in k and "D-08" in k for k in report.excluded)


# -- D-38: behavioural peg check -----------------------------------------------------


def test_dollar_peg_without_usd_in_its_name_is_excluded():
    # The real case: UUSDT traded a 0.05% 24h range at ~$1.00 with $14.9M of
    # volume and a top-64 market cap, so it passed every other criterion. It is
    # named "U", so the `"USD" in base` filter never saw it.
    report = screen(
        [symbol_info("UUSDT", "U"), symbol_info("BTCUSDT", "BTC")],
        [ticker("UUSDT", 14_900_000, price=1.0007, range_pct=0.05),
         ticker("BTCUSDT", 2_000_000, range_pct=3.0)],
        ranks={"U": 64, "BTC": 1},
    )
    assert [p.symbol for p in report.qualified] == ["BTCUSDT"]
    assert any("de-facto peg" in k for k in report.excluded)


def test_quiet_but_real_assets_survive_the_peg_check():
    # Gold-backed tokens are the tightest genuine assets in the universe
    # (XAUT/PAXG run ~2% daily). The floor must not cost us those.
    report = screen(
        [symbol_info("XAUTUSDT", "XAUT"), symbol_info("PAXGUSDT", "PAXG")],
        [ticker("XAUTUSDT", 9_000_000, price=4097.0, range_pct=1.99),
         ticker("PAXGUSDT", 8_000_000, price=4101.0, range_pct=2.05)],
        ranks={"XAUT": 38, "PAXG": 43},
    )
    assert {p.symbol for p in report.qualified} == {"XAUTUSDT", "PAXGUSDT"}
    assert report.excluded == {}


def test_zero_low_price_does_not_divide():
    report = screen(
        [symbol_info("DEADUSDT", "DEAD")],
        [{"symbol": "DEADUSDT", "quoteVolume": "9000000", "lastPrice": "0",
          "lowPrice": "0", "highPrice": "0"}],
        ranks={"DEAD": 50},
    )
    assert report.qualified == []
    assert any("de-facto peg" in k for k in report.excluded)


def test_max_pairs_caps_the_universe():
    n = 8
    symbols = [symbol_info(f"C{i}USDT", f"C{i}") for i in range(n)]
    tickers = [ticker(f"C{i}USDT", 2_000_000 + i) for i in range(n)]
    report = screen(symbols, tickers, ranks={f"C{i}": i + 1 for i in range(n)})
    assert len(report.qualified) == 5
    assert report.excluded["beyond top 5 by volume"] == 3
    # Highest volume survives the cut.
    assert report.qualified[0].symbol == f"C{n-1}USDT"


def test_markdown_report_renders():
    report = screen(
        [symbol_info("BTCUSDT", "BTC")],
        [ticker("BTCUSDT", 2_000_000, price=65000.0)],
        ranks={"BTC": 1},
    )
    md = report.to_markdown()
    assert "# Screener report" in md
    assert "| 1 | BTCUSDT |" in md
    assert "2026-07-14" in md
