"""Telegram alerts: no-credentials no-op, delivery, and failure swallowing."""

from __future__ import annotations

from trader.alerts import TelegramAlerts


def test_disabled_without_credentials():
    alerts = TelegramAlerts()
    assert not alerts.enabled
    assert alerts.send("hello") is False  # logged, not raised


def test_send_posts_to_telegram(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, json, timeout):
        sent["url"], sent["json"] = url, json
        return Resp()

    monkeypatch.setattr("requests.post", fake_post)
    alerts = TelegramAlerts("TOKEN", "CHAT")
    assert alerts.send("hello") is True
    assert "botTOKEN" in sent["url"]
    assert sent["json"] == {"chat_id": "CHAT", "text": "hello"}


def test_send_swallows_transport_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr("requests.post", boom)
    assert TelegramAlerts("TOKEN", "CHAT").send("hello") is False


def test_send_reports_api_rejection(monkeypatch):
    class Resp:
        status_code = 403
        text = "forbidden"

    monkeypatch.setattr("requests.post", lambda *a, **k: Resp())
    assert TelegramAlerts("TOKEN", "CHAT").send("hello") is False


def test_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "C")
    assert TelegramAlerts.from_env().enabled
