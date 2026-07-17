"""S7 daily/weekly investor reports: stats math on a synthetic journal, the
quiet-day contract, the Telegram freshness rule, idempotent catch-up, and the
journal v5 migration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trader.journal import Journal, new_nonce
from trader.reports import (
    ReportBuilder,
    ReportScheduler,
    day_bounds,
    money,
    week_bounds,
    week_period,
)

DAY = "2026-07-16"
T = f"{DAY}T12:00:00.000+00:00"  # mid-period timestamp for synthetic rows


class SpyAlerts:
    enabled = True
    chat_id = "42"

    def __init__(self, updates: list[dict] | None = None):
        self.sent: list[str] = []
        self.updates = updates or []
        self.offsets: list[int] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def get_updates(self, offset: int = 0) -> list[dict]:
        self.offsets.append(offset)
        pending, self.updates = self.updates, []
        return pending


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


def record_trade(journal: Journal, side: str, price: str, *, ts: str = T,
                 qty: str = "0.001", fee: str = "0.05", rule: str | None = None) -> str:
    intent = journal.record_intent(
        symbol="BTCUSDT", side=side, order_type="MARKET", quantity=qty,
        price=None, source="strategy:donchian@1d", nonce=new_nonce(),
    )
    coid = journal.record_order_submitting(intent)
    journal.record_order_ack(coid, "1", "FILLED")
    journal.record_fill(
        client_order_id=coid, fill_id=f"BTCUSDT-{coid[-8:]}", quantity=qty,
        price=price, fee=fee, fee_asset="USDT", executed_at=ts,
    )
    if rule:
        journal.record_event("TRADE_REASON", coid, {"rule": rule, "symbol": "BTCUSDT"})
    return coid


class TestPeriods:
    def test_day_bounds_are_utc_midnights(self):
        start, end = day_bounds(DAY)
        assert start == datetime(2026, 7, 16, tzinfo=UTC)
        assert end == datetime(2026, 7, 17, tzinfo=UTC)

    def test_week_period_and_bounds_round_trip(self):
        # 2026-07-16 is a Thursday in ISO week 29.
        period = week_period(datetime(2026, 7, 16, tzinfo=UTC).date())
        assert period == "2026-W29"
        start, end = week_bounds(period)
        assert start == datetime(2026, 7, 13, tzinfo=UTC)  # Monday
        assert end == datetime(2026, 7, 20, tzinfo=UTC)

    def test_money_is_dollar_presented_usdt(self):
        # D-28: "$" with sign handling, no separate currency line.
        assert money(150.0) == "$150.00"
        assert money(2.5, signed=True) == "+$2.50"
        assert money(-1.25, signed=True) == "-$1.25"


class TestStats:
    def test_quiet_day_reports_explicitly(self, journal):
        journal.record_equity_snapshot("150.0", f"{DAY}T00:05:00.000+00:00")
        builder = ReportBuilder(journal, initial_equity=150.0)
        stats = builder.build_stats(*day_bounds(DAY))
        assert stats["quiet"] is True
        assert stats["pnl_usdt"] == 0.0
        md = builder.render_markdown("daily", DAY, stats)
        assert "No trades" in md and "chose to wait" in md
        tg = builder.render_telegram("daily", DAY, stats)
        assert "All quiet" in tg

    def test_stats_pick_up_trades_pnl_fees_and_reasons(self, journal, monkeypatch):
        journal.record_equity_snapshot("150.0", f"{DAY}T00:00:00.000+00:00")
        journal.record_equity_snapshot("152.0", f"{DAY}T23:45:00.000+00:00")
        record_trade(journal, "BUY", "50000", ts=f"{DAY}T10:00:00.000+00:00",
                     rule="close broke above the 15-candle high")
        # The pnl_ledger stamps write-time; pin it into the period like a
        # live fill would be.
        monkeypatch.setattr("trader.journal.utcnow", lambda: f"{DAY}T18:00:00.000+00:00")
        record_trade(journal, "SELL", "52000", ts=f"{DAY}T18:00:00.000+00:00",
                     rule="close fell below the 15-candle low")
        journal.record_news_item(url="https://e.com/1", title="t", source=None,
                                 published=None, seen_at=T)

        stats = ReportBuilder(journal, initial_equity=150.0).build_stats(*day_bounds(DAY))
        assert stats["quiet"] is False
        assert stats["trade_count"] == 2
        assert stats["trades"][0]["side"] == "BUY"  # oldest first
        assert stats["trades"][0]["reason"].startswith("close broke above")
        assert stats["pnl_usdt"] == pytest.approx(2.0)
        assert stats["wins"] == 1 and stats["losses"] == 0
        assert stats["fees_usdt"] == pytest.approx(0.10)
        assert stats["news_count"] == 1
        md = ReportBuilder(journal).render_markdown("daily", DAY, stats)
        assert "BUY BTCUSDT" in md and "why: close broke above" in md

    def test_stats_ignore_activity_outside_the_period(self, journal):
        record_trade(journal, "BUY", "50000", ts="2026-07-15T10:00:00.000+00:00")
        journal.record_event("HALT_EXECUTED", "risk", {"reason": "x"})  # ts=now, outside
        stats = ReportBuilder(journal).build_stats(*day_bounds(DAY))
        assert stats["trade_count"] == 0
        assert stats["quiet"] is True

    def test_equity_falls_back_to_initial_before_first_snapshot(self, journal):
        stats = ReportBuilder(journal, initial_equity=150.0).build_stats(*day_bounds(DAY))
        assert stats["equity_open"] == 150.0
        assert stats["equity_close"] == 150.0

    def test_halts_flip_quiet_off_and_show_in_both_renderings(self, journal):
        journal._conn.execute(
            "INSERT INTO events(ts, kind, ref_id, payload) VALUES (?,?,?,?)",
            (T, "HALT_EXECUTED", "risk", '{"reason": "daily loss"}'),
        )
        journal._conn.commit()
        builder = ReportBuilder(journal)
        stats = builder.build_stats(*day_bounds(DAY))
        assert stats["quiet"] is False
        assert "HALT_EXECUTED" in builder.render_markdown("daily", DAY, stats)
        assert "halt" in builder.render_telegram("daily", DAY, stats)


class TestScheduler:
    def paper_start(self, journal, iso: str) -> None:
        ms = int(datetime.fromisoformat(iso).timestamp() * 1000)
        journal.set_state("engine:paper_started_at", ms)

    def test_no_paper_clock_means_no_reports(self, journal):
        sched = ReportScheduler(journal, SpyAlerts())
        assert sched.catch_up_once() == 0

    def test_catch_up_backfills_all_completed_days_and_weeks(self, journal):
        self.paper_start(journal, "2026-07-16T18:52:00+00:00")  # Thu, W29
        now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)  # Tue of W30
        alerts = SpyAlerts()
        sched = ReportScheduler(journal, alerts, now=lambda: now)

        n = sched.catch_up_once()
        # Completed days: 16..20 (5). Completed weeks: W29 (Mon 20th closed it).
        assert n == 6
        assert journal.get_report("daily", "2026-07-16") is not None
        assert journal.get_report("daily", "2026-07-20") is not None
        assert journal.get_report("daily", "2026-07-21") is None  # today: not closed
        assert journal.get_report("weekly", "2026-W29") is not None
        assert journal.get_report("weekly", "2026-W30") is None

    def test_catch_up_is_idempotent(self, journal):
        self.paper_start(journal, "2026-07-16T18:52:00+00:00")
        now = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
        sched = ReportScheduler(journal, SpyAlerts(), now=lambda: now)
        assert sched.catch_up_once() == 2  # 16th, 17th
        assert sched.catch_up_once() == 0  # nothing new

    def test_telegram_only_for_fresh_periods(self, journal):
        self.paper_start(journal, "2026-07-16T18:52:00+00:00")
        now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)  # Mon 01:00
        alerts = SpyAlerts()
        sched = ReportScheduler(journal, alerts, now=lambda: now)
        sched.catch_up_once()
        # Days 16..19 generated but only the 19th closed <24h ago; W29 also
        # just closed (it's Monday, D-29) — exactly two sends, rest archived.
        headers = [m.splitlines()[0] for m in alerts.sent]
        assert len(headers) == 2
        assert any("2026-07-19" in h for h in headers)
        assert any("2026-W29" in h for h in headers)

    def test_weekly_sent_fresh_on_monday(self, journal):
        self.paper_start(journal, "2026-07-13T00:00:00+00:00")  # Mon, W29
        now = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)  # next Monday morning
        alerts = SpyAlerts()
        ReportScheduler(journal, alerts, now=lambda: now).catch_up_once()
        weekly = [m for m in alerts.sent if "Weekly report" in m]
        assert len(weekly) == 1 and "2026-W29" in weekly[0]

    def test_generation_journals_report_generated(self, journal):
        self.paper_start(journal, "2026-07-16T18:52:00+00:00")
        now = datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
        ReportScheduler(journal, SpyAlerts(), now=lambda: now).catch_up_once()
        events = journal.events_of_kind(["REPORT_GENERATED"])
        assert len(events) == 1

    def test_alert_failure_still_archives(self, journal):
        class DeadAlerts:
            enabled = True

            def send(self, text: str) -> bool:
                return False

        self.paper_start(journal, "2026-07-16T18:52:00+00:00")
        now = datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
        ReportScheduler(journal, DeadAlerts(), now=lambda: now).catch_up_once()
        assert journal.get_report("daily", "2026-07-16") is not None


def _tg(update_id: int, chat: str, text: str) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


class TestDeliverySwitch:
    """Change request 2026-07-17: reports always archive; the Telegram copy
    can be paused/resumed from the UI or by messaging the bot."""

    def start(self, journal):
        ms = int(datetime(2026, 7, 16, 18, 52, tzinfo=UTC).timestamp() * 1000)
        journal.set_state("engine:paper_started_at", ms)

    def test_disabled_switch_archives_without_sending(self, journal):
        self.start(journal)
        alerts = SpyAlerts()
        sched = ReportScheduler(
            journal, alerts, now=lambda: datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
        )
        sched.set_telegram_enabled(False, via="test")
        sched.catch_up_once()
        assert alerts.sent == []
        assert journal.get_report("daily", "2026-07-16") is not None

    def test_bot_command_pauses_resumes_and_acks(self, journal):
        alerts = SpyAlerts(updates=[_tg(7, "42", "/reports_off")])
        sched = ReportScheduler(journal, alerts)
        assert sched.telegram_enabled is True
        assert sched.process_commands() == 1
        assert sched.telegram_enabled is False
        assert "paused" in alerts.sent[-1]
        assert journal.get_state("reports:tg_update_offset") == 8

        alerts.updates = [_tg(8, "42", "/reports_on@my_bot")]
        assert sched.process_commands() == 1
        assert sched.telegram_enabled is True
        assert "resumed" in alerts.sent[-1]

    def test_commands_from_other_chats_are_ignored_but_consumed(self, journal):
        alerts = SpyAlerts(updates=[_tg(3, "999", "/reports_off")])
        sched = ReportScheduler(journal, alerts)
        assert sched.process_commands() == 0
        assert sched.telegram_enabled is True
        assert alerts.sent == []
        assert journal.get_state("reports:tg_update_offset") == 4  # not re-read

    def test_next_poll_uses_stored_offset(self, journal):
        alerts = SpyAlerts(updates=[_tg(5, "42", "hello")])
        sched = ReportScheduler(journal, alerts)
        sched.process_commands()
        sched.process_commands()
        assert alerts.offsets == [0, 6]


class TestJournalV5:
    def test_save_is_upsert_on_kind_period(self, journal):
        journal.save_report(kind="daily", period=DAY, summary={"a": 1}, body_md="one")
        journal.save_report(kind="daily", period=DAY, summary={"a": 2}, body_md="two")
        row = journal.get_report("daily", DAY)
        assert row["body_md"] == "two"
        assert len(journal.reports_list()) == 1

    def test_list_is_newest_first_without_bodies(self, journal):
        journal.save_report(kind="daily", period="2026-07-15", summary={}, body_md="x")
        journal.save_report(kind="daily", period="2026-07-16", summary={}, body_md="x")
        rows = journal.reports_list()
        assert [r["period"] for r in rows] == ["2026-07-16", "2026-07-15"]
        assert "body_md" not in rows[0].keys()

    def test_v4_journal_migrates_additively(self, tmp_path):
        import sqlite3

        db = tmp_path / "old.db"
        j = Journal(db)
        j.close()
        with sqlite3.connect(db) as conn:  # rewind to v4, drop the v5 table
            conn.execute("DROP TABLE reports")
            conn.execute("UPDATE schema_version SET version=4")
        j2 = Journal(db)  # must migrate, not raise
        assert j2.get_report("daily", DAY) is None
        j2.close()
