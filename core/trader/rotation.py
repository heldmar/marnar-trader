"""Screener-driven universe rotation (D-09, exempted from the clock by D-42a).

The screener has always been able to pick a universe; until now a human copied
its output into `symbols`. This runs it on a schedule and applies the result to
the live engine.

**Why this does not restart the D-27 paper clock.** A manual `symbols` edit
through `PUT /api/config` still does — that is an ad-hoc change to the
experiment. Rotation is the opposite: it is the automation the gate exists to
exercise, and if every rotation restarted the three weeks the gate could never
be reached at all. D-42(a) records the cost the investor accepted: the gate now
certifies that the *machinery* runs cleanly, not that the book stood still, so
passing it does not mean the system held one universe for 21 days.

Two safety properties matter more than the schedule:

- A symbol with an open position is never rotated out. The engine is the only
  thing that can produce its exit; dropping it would strand the position with
  no sell rule and no protective stop.
- A rotation that would empty the universe, or that the screener could not
  complete, changes nothing. Trading the wrong book is worse than trading a
  stale one.
"""

from __future__ import annotations

import asyncio
import logging

from trader.config import AppConfig, save_config
from trader.journal import Journal
from trader.screener import Screener, fetch_market_cap_ranks

log = logging.getLogger(__name__)

WEEK_SECONDS = 7 * 24 * 3600


class UniverseRotator:
    """Re-screens on a schedule and hands the new universe to the engine."""

    def __init__(
        self,
        journal: Journal,
        state,
        public,
        *,
        config_path,
        alerts=None,
        every_seconds: float = WEEK_SECONDS,
    ):
        self.journal = journal
        self.state = state  # app.state — holds .config and .engine
        self.public = public
        self.config_path = config_path
        self.alerts = alerts
        self.every_seconds = every_seconds

    def screen(self) -> list[str]:
        config = self.state.config
        report = Screener(
            config.screener,
            exchange_info=self.public.exchange_info(),
            tickers_24h=self.public.ticker_24hr(),
            market_cap_ranks=fetch_market_cap_ranks(),
        ).run()
        return [p.symbol for p in report.qualified]

    def rotate_once(self) -> dict | None:
        """One screen-and-apply. Returns the change, or None if nothing moved."""
        engine = getattr(self.state, "engine", None)
        if engine is None:
            log.info("rotation skipped: engine not running")
            return None

        picks = self.screen()
        if not picks:
            # Either every pair failed the filters or the data was bad. Either
            # way, an empty universe is never the right answer to apply.
            log.warning("rotation skipped: screener returned no qualifying pairs")
            self.journal.record_event("ROTATION_SKIPPED", "screener", {"reason": "empty screen"})
            return None

        before = list(engine.symbols)
        if set(picks) == set(before):
            return None

        change = engine.set_symbols(picks)
        config = self.state.config
        new_paper = config.paper.model_copy(update={"symbols": list(engine.symbols)})
        new_config = config.model_copy(update={"paper": new_paper})
        save_config(new_config, self.config_path)
        self.state.config = new_config

        # D-42(a): recorded as its own event kind precisely so the audit trail
        # distinguishes an automated rotation from a manual symbols edit — only
        # the latter resets the clock, and the journal has to show which was which.
        payload = {"before": before, "after": list(engine.symbols), **change}
        self.journal.record_event("UNIVERSE_ROTATED", "screener", payload)
        log.info(
            "universe rotated: +%s -%s (kept for open positions: %s)",
            change["added"], change["removed"], change["kept_for_open_position"],
        )
        if self.alerts and (change["added"] or change["removed"]):
            self.alerts.send(
                f"🔁 universe rotated: +{', '.join(change['added']) or '—'} / "
                f"−{', '.join(change['removed']) or '—'} "
                f"({len(engine.symbols)} pairs; paper clock unaffected, D-42a)"
            )
        return payload

    async def run(self, *, every_seconds: float | None = None) -> None:
        every = every_seconds or self.every_seconds
        while True:
            # Sleep first: boot already screened when the config was written, and
            # rotating during startup would race the engine's own warmup.
            await asyncio.sleep(every)
            try:
                await asyncio.to_thread(self.rotate_once)
            except Exception as exc:  # noqa: BLE001 — never take the loop down
                log.exception("universe rotation failed (retrying next cycle)")
                self.journal.record_event("ROTATION_FAILED", "screener", {"error": str(exc)})


def rotation_enabled(config: AppConfig) -> bool:
    return bool(getattr(config.paper, "rotate_universe", False))
