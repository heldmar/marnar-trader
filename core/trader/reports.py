"""Daily & weekly investor reports (Sprint 7 — D-28, D-29, D-30).

Every UTC day (and every completed ISO week, D-29) the system writes a
plain-language report of how it went: P&L, equity, the trades it made and why,
fees, halts/skips, news-feed status. One source of truth (the stats dict)
rendered twice — a compact Telegram message and a full markdown body archived
in the journal's ``reports`` table for the UI.

Design rules:

- **Catch-up IS the backfill.** Each scheduler cycle generates every missing
  report for completed periods since ``engine:paper_started_at``. First boot
  after deploy backfills the whole archive; a gap after downtime heals the
  same way. (kind, period) is the idempotency key.
- **Freshness rule:** Telegram delivery only when ``now < period_end + 24h``;
  older reports go to the archive silently (nobody wants 20 stale pings).
- **Quiet days still report** — silence must always mean "something is
  wrong", never "nothing happened".
- **Isolation (same principle as news, D-23):** report generation runs in its
  own asyncio task; failures are logged and retried next cycle, and can never
  touch the trading loop.
- **D-28:** amounts are USDT presented as "$" — no separate USD line, no UYU.

All period boundaries are UTC. Daily delivery therefore lands right after
UTC midnight ≈ 21:00 Montevideo; weekly = ISO week Mon–Sun, sent Mondays.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from trader.alerts import TelegramAlerts
from trader.journal import Journal

log = logging.getLogger(__name__)

# Event kinds surfaced in the report's "system events" section.
_HALT_KINDS = ["HALT_EXECUTED", "RESUMED"]
_SKIP_KINDS = ["ENTRY_SKIPPED_MIN_NOTIONAL", "RISK_REJECTED"]

FRESH_FOR = timedelta(hours=24)  # Telegram-send window after a period closes

# Durable report-delivery switch (change request 2026-07-17): reports are
# always archived; this only controls the Telegram copy. Toggled from the UI
# (PUT /api/reports/settings) or by messaging the bot the commands below.
DELIVERY_KEY = "reports:telegram_enabled"
TG_OFFSET_KEY = "reports:tg_update_offset"
CMD_OFF = "/reports_off"
CMD_ON = "/reports_on"


def money(v: float, *, signed: bool = False) -> str:
    """USDT rendered as '$' (D-28). signed=True prefixes gains with '+'."""
    sign = "-" if v < 0 else ("+" if signed else "")
    return f"{sign}${abs(v):,.2f}"


def _iso(dt: datetime) -> str:
    """Match the journal's ts format so string comparisons work."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def day_bounds(period: str) -> tuple[datetime, datetime]:
    d = date.fromisoformat(period)
    start = datetime.combine(d, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def week_period(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(period: str) -> tuple[datetime, datetime]:
    year, week = int(period[:4]), int(period.split("-W")[1])
    monday = date.fromisocalendar(year, week, 1)
    start = datetime.combine(monday, time.min, tzinfo=UTC)
    return start, start + timedelta(days=7)


class ReportBuilder:
    """Builds the stats for a period from the journal, and renders them."""

    def __init__(self, journal: Journal, *, initial_equity: float = 150.0):
        self.journal = journal
        self.initial_equity = initial_equity

    # -- stats (the single source of truth) --------------------------------

    def build_stats(self, start: datetime, end: datetime) -> dict[str, Any]:
        lo, hi = _iso(start), _iso(end)
        equity_open = self._equity_at(lo)
        equity_close = self._equity_at(hi)
        pnl = equity_close - equity_open

        wins = losses = 0
        realized = 0.0
        for row in self.journal.pnl_entries_since(lo):
            if row["ts"] >= hi:
                break
            v = float(row["realized_pnl"])
            realized += v
            # QA2-07: a break-even round trip is neither a win nor a loss.
            wins, losses = wins + (v > 0), losses + (v < 0)

        # QA2-06: period-bounded queries — no global row caps that could make a
        # regenerated old period silently incomplete once history grows.
        fees = sum(float(f["fee"]) for f in self.journal.fills_between(lo, hi))

        trades = []
        for t in self.journal.trade_history(limit=1000, since_iso=lo, before_iso=hi):
            ts = t["executed_at"] or t["created_at"]
            reason = None
            for ev in self.journal.events_for(t["client_order_id"]):
                if ev["kind"] == "TRADE_REASON":
                    reason = json.loads(ev["payload"])
            trades.append({
                "ts": ts, "symbol": t["symbol"], "side": t["side"],
                "qty": t["qty"], "avg_price": t["avg_price"], "fee": t["fee"],
                "reason": (reason or {}).get("rule"),
            })
        trades.reverse()  # oldest first reads naturally in a report

        halts = self._events_between(_HALT_KINDS, lo, hi)
        skips = self._events_between(_SKIP_KINDS, lo, hi)
        news_count = self.journal.news_count_between(lo, hi)

        return {
            "equity_open": equity_open,
            "equity_close": equity_close,
            "pnl_usdt": pnl,
            "pnl_pct": (pnl / equity_open * 100) if equity_open else 0.0,
            "realized_pnl_usdt": realized,
            "trades": trades,
            "trade_count": len(trades),
            "wins": wins,
            "losses": losses,
            "fees_usdt": fees,
            "halts": halts,
            "skips": skips,
            "news_count": news_count,
            "quiet": not trades and not halts,
        }

    def _equity_at(self, ts_iso: str) -> float:
        """Mark-to-market equity at *ts_iso*: the latest snapshot at or before
        it; before the first snapshot ever, the configured starting money."""
        row = self.journal.equity_at(ts_iso)
        return float(row["equity"]) if row else self.initial_equity

    def _events_between(self, kinds: list[str], lo: str, hi: str) -> list[dict]:
        return [
            {"ts": ev["ts"], "kind": ev["kind"], "detail": json.loads(ev["payload"])}
            for ev in self.journal.events_of_kind_between(kinds, lo, hi)
        ]

    # -- rendering (UI-12..15: plain language, onion layers) ----------------

    def render_telegram(self, kind: str, period: str, s: dict[str, Any]) -> str:
        label = "Daily report" if kind == "daily" else "Weekly report"
        lines = [f"📊 {label} — {period} (UTC)"]
        lines.append(
            f"P&L: {money(s['pnl_usdt'], signed=True)} ({s['pnl_pct']:+.2f}%) · "
            f"Equity: {money(s['equity_close'])}"
        )
        if s["quiet"]:
            lines.append("No trades — no entry signals fired. All quiet.")
        else:
            closed = s["wins"] + s["losses"]
            trade_bits = f"Trades: {s['trade_count']}"
            if closed:
                trade_bits += f" · Closed: {s['wins']} won / {s['losses']} lost"
            lines.append(trade_bits)
        lines.append(
            f"Fees: {money(s['fees_usdt'])} · News headlines stored: {s['news_count']}"
        )
        if s["halts"]:
            lines.append(f"⚠️ {len(s['halts'])} halt/resume event(s) — see the report.")
        lines.append("Full report: web UI → Reports tab.")
        return "\n".join(lines)

    def render_markdown(self, kind: str, period: str, s: dict[str, Any]) -> str:
        label = "Daily report" if kind == "daily" else "Weekly report"
        out = [f"# {label} — {period}", ""]
        out.append(
            f"**P&L: {money(s['pnl_usdt'], signed=True)} ({s['pnl_pct']:+.2f}%)** — "
            f"money went from {money(s['equity_open'])} to {money(s['equity_close'])}."
        )
        if s.get("partial_from"):
            out.append("")
            out.append(
                f"*Partial period: the practice run started on {s['partial_from']}, "
                "so this report covers only the days since then.*"
            )
        out.append("")
        if s["quiet"]:
            out.append(
                "No trades — no entry signals fired and no safety events. "
                "The system checked the market the whole period and chose to wait."
            )
        else:
            out.append(f"## Trades ({s['trade_count']})")
            out.append("")
            for t in s["trades"]:
                why = f" — why: {t['reason']}" if t["reason"] else ""
                out.append(
                    f"- {t['ts'][:16]} **{t['side']} {t['symbol']}** "
                    f"{t['qty']:g} @ {money(t['avg_price'])}"
                    f" (fee {money(t['fee'] or 0)}){why}"
                )
            closed = s["wins"] + s["losses"]
            if closed:
                out.append("")
                out.append(
                    f"Closed round-trips: **{s['wins']} won / {s['losses']} lost**, "
                    f"realized {money(s['realized_pnl_usdt'], signed=True)}."
                )
        out.append("")
        out.append("## Costs & safety")
        out.append("")
        out.append(f"- Fees paid: {money(s['fees_usdt'])}")
        if s["halts"]:
            for ev in s["halts"]:
                out.append(f"- ⚠️ {ev['ts'][:16]} — {ev['kind']}: {ev['detail']}")
        else:
            out.append("- No circuit-breaker halts.")
        if s["skips"]:
            out.append(
                f"- {len(s['skips'])} entr{'y was' if len(s['skips']) == 1 else 'ies were'} "
                "skipped by safety checks (size/risk limits)."
            )
        out.append(
            f"- News headlines stored for context: {s['news_count']}"
            " (D-23: never traded on)."
        )
        out.append("")
        out.append(
            "*All amounts are USDT shown as \"$\" (D-28). Period boundaries are UTC.*"
        )
        return "\n".join(out)


class ReportScheduler:
    """NewsIngestor-pattern task: every cycle, catch up all missing reports.

    Never raises out of a cycle; never touches the trading loop.
    """

    def __init__(
        self,
        journal: Journal,
        alerts: TelegramAlerts | None = None,
        *,
        initial_equity: float = 150.0,
        now: Callable[[], datetime] | None = None,
    ):
        self.journal = journal
        self.alerts = alerts or TelegramAlerts()
        self.builder = ReportBuilder(journal, initial_equity=initial_equity)
        self.now = now or (lambda: datetime.now(UTC))

    # -- delivery switch (UI setting + bot commands) ---------------------------

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.journal.get_state(DELIVERY_KEY, True))

    def set_telegram_enabled(self, enabled: bool, *, via: str) -> None:
        self.journal.set_state(DELIVERY_KEY, enabled)
        self.journal.record_event(
            "REPORT_DELIVERY_CHANGED", "reports", {"telegram_enabled": enabled, "via": via}
        )

    def process_commands(self) -> int:
        """Handle bot commands from the configured chat: /reports_off and
        /reports_on. Messages from any other chat are ignored (and consumed).
        Returns how many commands were applied."""
        offset = int(self.journal.get_state(TG_OFFSET_KEY, 0))
        # QA2-10: drain the queue — Telegram pages at ~100 updates/call, and a
        # spammed bot must not delay the owner's command by many cycles.
        updates: list[dict] = []
        next_offset = offset
        for _ in range(10):
            page = self.alerts.get_updates(next_offset)
            updates.extend(page)
            if page:
                next_offset = max(int(u.get("update_id", 0)) for u in page) + 1
            if len(page) < 100:
                break
        applied = 0
        for u in updates:
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != self.alerts.chat_id:
                continue  # only the owner's chat may command the bot
            text = (msg.get("text") or "").strip().split("@")[0].lower()
            if text == CMD_OFF:
                self.set_telegram_enabled(False, via="telegram")
                self.alerts.send(
                    "🔕 Report delivery paused. Reports keep being written to the "
                    f"web UI archive. Send {CMD_ON} to resume."
                )
                applied += 1
            elif text == CMD_ON:
                self.set_telegram_enabled(True, via="telegram")
                self.alerts.send("🔔 Report delivery resumed — daily and weekly reports "
                                 "will arrive here again.")
                applied += 1
        if updates:
            self.journal.set_state(TG_OFFSET_KEY, offset)
        return applied

    # -- period discovery ----------------------------------------------------

    def _paper_start(self) -> datetime | None:
        ms = self.journal.get_state("engine:paper_started_at")
        return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else None

    def missing_periods(self) -> list[tuple[str, str]]:
        """(kind, period) for every completed period since paper start that
        has no stored report yet — oldest first, dailies before weeklies."""
        start = self._paper_start()
        if start is None:
            return []
        now = self.now()
        out: list[tuple[str, str]] = []
        d = start.date()
        while datetime.combine(d, time.min, tzinfo=UTC) + timedelta(days=1) <= now:
            period = d.isoformat()
            if self.journal.get_report("daily", period) is None:
                out.append(("daily", period))
            d += timedelta(days=1)
        monday = start.date() - timedelta(days=start.date().weekday())
        while datetime.combine(monday, time.min, tzinfo=UTC) + timedelta(days=7) <= now:
            period = week_period(monday)
            if self.journal.get_report("weekly", period) is None:
                out.append(("weekly", period))
            monday += timedelta(days=7)
        return out

    # -- generation ------------------------------------------------------------

    def generate(self, kind: str, period: str) -> dict[str, Any]:
        """Build, archive and (if fresh) deliver one report. Idempotent via
        the reports table's (kind, period) key — regenerating overwrites."""
        bounds = day_bounds(period) if kind == "daily" else week_bounds(period)
        start, end = bounds
        stats = self.builder.build_stats(start, end)
        p_start = self._paper_start()
        if p_start is not None and start < p_start:
            # QA2-09: the first period can begin before the run did — say so.
            stats["partial_from"] = p_start.date().isoformat()
        body_md = self.builder.render_markdown(kind, period, stats)
        summary = {k: v for k, v in stats.items() if k not in ("trades", "halts", "skips")}
        summary["halt_count"] = len(stats["halts"])
        self.journal.save_report(kind=kind, period=period, summary=summary, body_md=body_md)
        fresh = self.now() < end + FRESH_FOR
        sent = False
        if fresh and self.telegram_enabled:
            sent = self.alerts.send(self.builder.render_telegram(kind, period, stats))
        self.journal.record_event(
            "REPORT_GENERATED", "reports",
            {"kind": kind, "period": period, "fresh": fresh, "sent": sent},
        )
        log.info("report generated: %s %s (fresh=%s sent=%s)", kind, period, fresh, sent)
        return stats

    MAX_PER_CYCLE = 12  # QA2-03: spread a huge backfill over cycles, don't stall

    def catch_up_once(self) -> int:
        """One cycle: generate missing reports (capped). Returns how many landed."""
        missing = self.missing_periods()
        for kind, period in missing[: self.MAX_PER_CYCLE]:
            self.generate(kind, period)
        if len(missing) > self.MAX_PER_CYCLE:
            log.info(
                "report backfill: %d generated, %d left for next cycles",
                self.MAX_PER_CYCLE, len(missing) - self.MAX_PER_CYCLE,
            )
        return min(len(missing), self.MAX_PER_CYCLE)

    def _cycle(self) -> None:
        self.process_commands()
        self.catch_up_once()

    async def run(self, *, every_seconds: float = 300.0) -> None:
        while True:
            try:
                # QA2-03: Telegram HTTP + journal scans belong in a worker
                # thread — the event loop (API, kill switch) must stay free.
                await asyncio.to_thread(self._cycle)
            except Exception as exc:
                log.warning("report cycle failed (retrying next cycle): %s", exc)
            await asyncio.sleep(every_seconds)
