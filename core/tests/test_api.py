"""S6 web-UI API: overview, positions, timeline (D-23 truthful attribution),
config editor (only-tighten risk, D-27 clock-reset acknowledgment)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import trader.main
from trader.api import router
from trader.config import AppConfig, load_config
from trader.journal import Journal, new_nonce, utcnow
from trader.risk import RiskManager


class StubGateway:
    """Just enough gateway for the read endpoints."""

    def __init__(self, prices: dict[str, Decimal], equity: Decimal):
        self.prices = prices
        self._equity = equity

    def ticker_price(self, symbol: str) -> Decimal:
        return self.prices[symbol]

    def equity_usdt(self) -> Decimal:
        return self._equity


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = AppConfig(mode="paper")
    journal = Journal(tmp_path / "journal.db")
    app = FastAPI()
    app.include_router(router)
    app.state.config = config
    app.state.journal = journal
    app.state.risk = RiskManager(journal, config.risk)
    app.state.gateway = StubGateway({"BTCUSDT": Decimal("51000")}, Decimal("152.5"))
    app.state.trading_enabled = True
    app.state.reconciliation = {"clean": True}
    monkeypatch.setattr(trader.main, "CONFIG_PATH", str(tmp_path / "config.yaml"))
    # The UI sends the CSRF header on every request (api.ts); tests mirror that.
    with TestClient(app, headers={"x-marnar-ui": "1"}) as c:
        yield SimpleNamespace(http=c, journal=journal, app=app, tmp=tmp_path)
    journal.close()


def _record_trade(journal: Journal, side: str, price: str, qty: str = "0.0003") -> str:
    intent = journal.record_intent(
        symbol="BTCUSDT", side=side, order_type="MARKET", quantity=qty,
        price=None, source="strategy:donchian@1d", nonce=new_nonce(),
    )
    coid = journal.record_order_submitting(intent)
    journal.record_order_ack(coid, "123", "FILLED")
    journal.record_fill(
        client_order_id=coid, fill_id=f"BTCUSDT-{coid[-6:]}", quantity=qty,
        price=price, fee="0.01", fee_asset="USDT", executed_at=utcnow(),
    )
    return coid


def test_overview_shows_equity_pnl_and_series(client):
    client.journal.set_state("engine:paper_started_at", 1_784_000_000_000)
    client.journal.record_equity_snapshot("150.0", "2026-07-16T00:00:00.000+00:00")
    client.journal.record_equity_snapshot("152.5", "2026-07-16T12:00:00.000+00:00")
    client.app.state.risk.note_equity(Decimal("150"))  # sets today's anchor

    data = client.http.get("/api/overview").json()
    assert data["mode"] == "paper"
    assert data["equity_usdt"] == 152.5
    assert data["total_pnl_usdt"] == pytest.approx(2.5)
    assert data["today_pnl_usdt"] == pytest.approx(2.5)
    assert [p["equity"] for p in data["equity_series"]] == [150.0, 152.5]
    assert data["paper_started_at"] == 1_784_000_000_000


def test_positions_include_live_price_unrealized_pnl_and_stops(client):
    _record_trade(client.journal, "BUY", "50000")
    client.journal.set_state("engine:protect:BTCUSDT", {"stop": "48500", "tp": None})

    (pos,) = client.http.get("/api/positions").json()
    assert pos["symbol"] == "BTCUSDT"
    assert pos["entry_price"] == pytest.approx(50000)
    assert pos["current_price"] == pytest.approx(51000)
    assert pos["unrealized_pnl_usdt"] == pytest.approx(0.0003 * 1000)
    assert pos["stop_price"] == pytest.approx(48500)


def test_timeline_carries_reason_and_news_as_separate_context(client):
    coid = _record_trade(client.journal, "BUY", "50000")
    client.journal.record_event(
        "TRADE_REASON", coid,
        {"rule": "close broke above the 15-candle high", "close": 50100, "level": 50000},
    )
    client.journal.record_news_item(
        url="https://example.com/a", title="Bitcoin rallies", source="example.com",
        published=None, seen_at=utcnow(),
    )

    items = client.http.get("/api/timeline").json()
    trade = next(i for i in items if i["type"] == "trade")
    assert trade["reason"]["rule"].startswith("close broke above")
    assert trade["news_context"][0]["title"] == "Bitcoin rallies"
    # D-23: the reason field and news context are separate — news is never the cause.
    assert "news" not in (trade["reason"] or {})


def test_timeline_includes_system_events_and_symbol_filter(client):
    _record_trade(client.journal, "BUY", "50000")
    client.journal.record_event("PAPER_CLOCK_STARTED", "engine", {"interval": "1d"})

    items = client.http.get("/api/timeline").json()
    assert any(i["type"] == "system" and i["kind"] == "PAPER_CLOCK_STARTED" for i in items)
    assert client.http.get("/api/timeline", params={"symbol": "ETHUSDT"}).json() == [
        i for i in items if i["type"] == "system"
    ]


def test_config_get_exposes_floors_and_clock_fields(client):
    data = client.http.get("/api/config").json()
    assert data["risk_floors"]["max_daily_loss_pct"] == 2.0
    assert "entry_n" in data["clock_resetting_fields"]


def test_config_put_rejects_loosened_risk(client):
    resp = client.http.put("/api/config", json={"risk": {"max_daily_loss_pct": 5.0}})
    assert resp.status_code == 422


def test_config_put_strategy_change_requires_clock_ack(client):
    client.journal.set_state("engine:paper_started_at", 1_784_000_000_000)

    resp = client.http.put("/api/config", json={"paper": {"entry_n": 20}})
    assert resp.status_code == 409
    assert client.journal.get_state("engine:paper_started_at") is not None

    resp = client.http.put(
        "/api/config", json={"paper": {"entry_n": 20}, "acknowledge_clock_reset": True}
    )
    assert resp.json() == {"saved": True, "restart_required": True, "clock_reset": True}
    assert client.journal.get_state("engine:paper_started_at") is None
    saved = load_config(client.tmp / "config.yaml")
    assert saved.paper.entry_n == 20


def test_config_put_tightening_risk_needs_no_ack_and_persists(client):
    resp = client.http.put("/api/config", json={"risk": {"max_daily_loss_pct": 1.0}})
    assert resp.json()["clock_reset"] is False
    assert load_config(client.tmp / "config.yaml").risk.max_daily_loss_pct == 1.0


def test_config_put_hot_applies_risk_limits(client):
    """QA1-10: a tightened limit binds immediately, not at next restart."""
    client.http.put("/api/config", json={"risk": {"max_daily_loss_pct": 1.0}})
    assert client.app.state.risk._limits.max_daily_loss_pct == 1.0


def test_config_put_without_ui_header_is_rejected(client):
    """QA1-01: state-changing requests need the CSRF header the UI always sends."""
    with TestClient(client.app) as raw:  # no default headers, like a cross-site form
        resp = raw.put("/api/config", json={"risk": {"max_daily_loss_pct": 1.0}})
    assert resp.status_code == 403
    # nothing was saved or applied
    assert not (client.tmp / "config.yaml").exists()
    assert client.app.state.risk._limits.max_daily_loss_pct == 2.0


def test_reports_archive_list_and_detail(client):
    client.journal.save_report(
        kind="daily", period="2026-07-16",
        summary={"pnl_usdt": 2.5, "quiet": False}, body_md="# Daily report",
    )
    (row,) = client.http.get("/api/reports").json()
    assert row["kind"] == "daily" and row["summary"]["pnl_usdt"] == 2.5
    assert "body_md" not in row  # list stays light; the body is in the detail
    detail = client.http.get("/api/reports/daily/2026-07-16").json()
    assert detail["body_md"] == "# Daily report"
    assert client.http.get("/api/reports/daily/2099-01-01").status_code == 404


def test_report_settings_toggle_and_csrf(client):
    assert client.http.get("/api/reports/settings").json() == {"telegram_enabled": True}
    resp = client.http.put("/api/reports/settings", json={"telegram_enabled": False})
    assert resp.json() == {"telegram_enabled": False}
    assert client.journal.get_state("reports:telegram_enabled") is False
    # state-changing → CSRF header required
    with TestClient(client.app) as raw:
        assert raw.put(
            "/api/reports/settings", json={"telegram_enabled": True}
        ).status_code == 403
    assert client.journal.get_state("reports:telegram_enabled") is False


def test_system_reports_freshness(client):
    client.journal.set_state("engine:last_candle:BTCUSDT", 1_784_160_000_000)
    client.journal.record_news_item(
        url="https://example.com/b", title="t", source=None, published=None, seen_at=utcnow()
    )
    data = client.http.get("/api/system").json()
    assert data["last_candle_processed"] == 1_784_160_000_000
    assert data["news_items_24h"] == 1
