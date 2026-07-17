"""Scheduled macro event timestamps for the D-23 event-blackout rule.

Numeric inputs only (D-23): these are *times*, not text. Two calendars matter
enough to crypto volatility to backtest first: FOMC rate decisions (14:00 ET
statement) and US CPI releases (08:30 ET). Sources: federalreserve.gov FOMC
calendar; BLS CPI schedule (2025–2026 verified 2026-07-14, including the
late-2025 shutdown anomalies — Sep data out 10-24, Oct release canceled,
Nov data out 12-18).

Times below are UTC. ET offset shifts with US daylight saving; each entry is
the actual UTC moment of that specific date (EDT = UTC-4, EST = UTC-5).
Live mode will read future events from config, not from this file — this
module is the *historical* record the backtests replay.
"""

from __future__ import annotations

from datetime import UTC, datetime

_FMT = "%Y-%m-%d %H:%M"

# FOMC statement release: 14:00 ET on the decision day.
_FOMC_UTC = [
    "2024-07-31 18:00",
    "2024-09-18 18:00",
    "2024-11-07 19:00",
    "2024-12-18 19:00",
    "2025-01-29 19:00",
    "2025-03-19 18:00",
    "2025-05-07 18:00",
    "2025-06-18 18:00",
    "2025-07-30 18:00",
    "2025-09-17 18:00",
    "2025-10-29 18:00",
    "2025-12-10 19:00",
    "2026-01-28 19:00",
    "2026-03-18 18:00",
    "2026-04-29 18:00",
    "2026-06-17 18:00",
    # H2 2026 verified 2026-07-16 (federalreserve.gov FOMC calendar) — QA1-14
    "2026-07-29 18:00",
    "2026-09-16 18:00",
    "2026-10-28 18:00",
    "2026-12-09 19:00",
]

# CPI release: 08:30 ET.
_CPI_UTC = [
    "2024-07-11 12:30",
    "2024-08-14 12:30",
    "2024-09-11 12:30",
    "2024-10-10 12:30",
    "2024-11-13 13:30",
    "2024-12-11 13:30",
    "2025-01-15 13:30",
    "2025-02-12 13:30",
    "2025-03-12 12:30",
    "2025-04-10 12:30",
    "2025-05-13 12:30",
    "2025-06-11 12:30",
    "2025-07-15 12:30",
    "2025-08-12 12:30",
    "2025-09-11 12:30",
    "2025-10-24 12:30",  # Sep data, shutdown-delayed
    # October 2025 release canceled (government shutdown)
    "2025-12-18 13:30",  # Nov data, rescheduled from 12-10
    "2026-01-13 13:30",
    "2026-02-13 13:30",  # rescheduled from 02-11
    "2026-03-11 12:30",
    "2026-04-10 12:30",
    "2026-05-12 12:30",
    "2026-06-10 12:30",
    "2026-07-14 12:30",
    # H2 2026 verified 2026-07-16 (BLS CPI schedule) — QA1-14
    "2026-08-12 12:30",
    "2026-09-11 12:30",
    "2026-10-14 12:30",
    "2026-11-10 13:30",
    "2026-12-10 13:30",
]


def _to_ms(entries: list[str]) -> list[int]:
    return [
        int(datetime.strptime(e, _FMT).replace(tzinfo=UTC).timestamp() * 1000)
        for e in entries
    ]


def fomc_times_ms() -> list[int]:
    return _to_ms(_FOMC_UTC)


def cpi_times_ms() -> list[int]:
    return _to_ms(_CPI_UTC)


def all_event_times_ms() -> list[int]:
    """FOMC + CPI, sorted — the default blackout calendar for S4 backtests."""
    return sorted(_to_ms(_FOMC_UTC) + _to_ms(_CPI_UTC))
