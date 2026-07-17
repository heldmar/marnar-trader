"""Telegram alerts (Sprint 5, PROJECT-PLAN S5 scope).

Push-only notifier: trade fills, circuit-breaker halts, engine errors, startup.
Credentials come exclusively from environment variables (D-04):

    TELEGRAM_BOT_TOKEN  — BotFather token
    TELEGRAM_CHAT_ID    — investor's chat/channel id

Unset credentials degrade to a logged no-op — alerting must never be the reason
the trading loop dies, so ``send`` swallows (and logs) every delivery failure.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

_API = "https://api.telegram.org"


class TelegramAlerts:
    def __init__(self, token: str = "", chat_id: str = "", *, timeout_s: float = 10.0):
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout_s
        if not self.enabled:
            log.warning("Telegram alerts disabled (no token/chat id in environment)")

    @classmethod
    def from_env(cls) -> TelegramAlerts:
        return cls(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def chat_id(self) -> str:
        return self._chat_id

    def get_updates(self, offset: int = 0) -> list[dict]:
        """Fetch pending bot updates (for the S7 report commands). Best-effort:
        any failure returns [] — command handling must never break a cycle."""
        if not self.enabled:
            return []
        try:
            resp = requests.get(
                f"{_API}/bot{self._token}/getUpdates",
                params={"offset": offset, "timeout": 0},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.warning("telegram getUpdates failed: %s", resp.status_code)
                return []
            return resp.json().get("result", [])
        except Exception as exc:
            log.warning("telegram getUpdates error: %s", exc)
            return []

    def send(self, text: str) -> bool:
        """Deliver *text* (best-effort). Returns True on confirmed delivery."""
        if not self.enabled:
            log.info("alert (telegram disabled): %s", text)
            return False
        try:
            resp = requests.post(
                f"{_API}/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                log.error("telegram send failed: %s %s", resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as exc:
            log.error("telegram send error: %s", exc)
            return False
