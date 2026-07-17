"""Risk Manager — the only path from intent to order (R-02, G3).

Enforces, non-overridably: daily-loss halt (D-06), drawdown full-halt (D-07),
per-coin exposure cap and max open positions (D-08), trade-rate cap, and the
manual kill switch. Strategy code has no API around this module.

Halt behavior is tiered per D-21 (Q11):

- Daily-loss halt (2%):  cancel pending orders, HOLD positions — it is a
  cooldown; the halt auto-clears at the next trading day.
- Drawdown halt (20%):   FORCE-FLAT all positions; manual reactivation required.
- Kill switch:           investor chooses "Halt" or "Halt + close positions".

All halt state is persisted in the journal's ``app_state`` table so it survives
a crash/restart — a drawdown halt cannot be forgotten by rebooting (no-UPS
discipline, D-13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from trader.config import RiskLimits
from trader.journal import Journal

STATE_KEY = "risk.halt"
PEAK_KEY = "risk.equity_peak"
DAY_ANCHOR_KEY = "risk.day_anchor"


class HaltState(StrEnum):
    RUNNING = "RUNNING"
    HALTED_DAILY_LOSS = "HALTED_DAILY_LOSS"
    HALTED_DRAWDOWN = "HALTED_DRAWDOWN"
    HALTED_MANUAL = "HALTED_MANUAL"


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = "ok"


@dataclass(frozen=True)
class HaltAction:
    state: HaltState
    reason: str
    force_flat: bool  # D-21: True for drawdown halt / kill switch with close


class RiskManager:
    """Stateful circuit breakers. Reads exposure facts from the journal; the
    caller supplies current equity (quote-asset value of the account)."""

    def __init__(self, journal: Journal, limits: RiskLimits, timezone: str = "America/Montevideo"):
        self._journal = journal
        self._limits = limits
        self._tz = ZoneInfo(timezone)

    def set_limits(self, limits: RiskLimits) -> None:
        """Hot-apply a (floor-validated) limits change — a tightened limit must
        bind immediately, not at the next restart (QA1-10)."""
        self._limits = limits

    # -- state ------------------------------------------------------------------

    @property
    def state(self) -> HaltState:
        raw = self._journal.get_state(STATE_KEY)
        return HaltState(raw["state"]) if raw else HaltState.RUNNING

    def status(self) -> dict:
        raw = self._journal.get_state(STATE_KEY) or {"state": HaltState.RUNNING.value}
        return {
            "state": raw["state"],
            "reason": raw.get("reason"),
            "since": raw.get("since"),
            "equity_peak": self._journal.get_state(PEAK_KEY),
            "day_anchor": self._journal.get_state(DAY_ANCHOR_KEY),
        }

    def trading_allowed(self) -> bool:
        return self.state == HaltState.RUNNING

    def _set_halt(self, state: HaltState, reason: str) -> None:
        self._journal.set_state(
            STATE_KEY,
            {"state": state.value, "reason": reason, "since": datetime.now(UTC).isoformat()},
        )

    # -- intent gate --------------------------------------------------------------

    def evaluate_intent(
        self,
        *,
        symbol: str,
        side: str,
        notional: Decimal,
        equity: Decimal,
        now: datetime | None = None,
    ) -> RiskDecision:
        """Approve or reject an intent BEFORE it becomes an order. SELLs reduce
        exposure, so position-count and per-coin caps apply to BUYs only; the
        halt gate and trade-rate cap apply to everything."""
        if self.state != HaltState.RUNNING:
            return RiskDecision(False, f"trading halted: {self.state.value}")

        now = now or datetime.now(UTC)
        hour_ago = (now - timedelta(hours=1)).isoformat(timespec="milliseconds")
        if self._journal.orders_created_since(hour_ago) >= self._limits.max_trades_per_hour:
            return RiskDecision(
                False, f"trade-rate cap: {self._limits.max_trades_per_hour} orders/hour reached"
            )

        if side == "BUY":
            positions = self._journal.positions()
            held = {p["symbol"] for p in positions}
            if symbol not in held and len(positions) >= self._limits.max_open_positions:
                return RiskDecision(
                    False, f"max open positions ({self._limits.max_open_positions}) reached"
                )
            current = self._journal.position_for(symbol)
            exposure = Decimal(current["cost"]) if current else Decimal("0")
            cap = equity * Decimal(str(self._limits.max_position_pct_per_coin)) / 100
            if exposure + notional > cap:
                return RiskDecision(
                    False,
                    f"per-coin cap: {symbol} exposure {exposure + notional} would exceed "
                    f"{self._limits.max_position_pct_per_coin}% of equity ({cap})",
                )
        return RiskDecision(True)

    # -- equity-driven breakers -----------------------------------------------------

    def note_equity(self, equity: Decimal, now: datetime | None = None) -> HaltAction | None:
        """Feed the current account equity; trips the daily-loss / drawdown
        breakers. Returns the HaltAction the caller must execute (per D-21),
        or None if nothing tripped."""
        now = now or datetime.now(UTC)
        today = now.astimezone(self._tz).date().isoformat()

        anchor = self._journal.get_state(DAY_ANCHOR_KEY)
        if anchor is None or anchor["date"] != today:
            self._journal.set_state(DAY_ANCHOR_KEY, {"date": today, "equity": str(equity)})
            anchor = {"date": today, "equity": str(equity)}
            if self.state == HaltState.HALTED_DAILY_LOSS:
                # D-06: the daily-loss halt is "for the day" — new day, auto-resume.
                self._set_halt(HaltState.RUNNING, "daily-loss halt cleared at day rollover")

        peak = self._journal.get_state(PEAK_KEY)
        if peak is None or equity > Decimal(peak):
            self._journal.set_state(PEAK_KEY, str(equity))
            peak = str(equity)

        if self.state != HaltState.RUNNING:
            return None  # already halted; nothing new to trip

        drawdown_pct = (Decimal(peak) - equity) / Decimal(peak) * 100
        if drawdown_pct >= Decimal(str(self._limits.max_drawdown_pct)):
            reason = (
                f"drawdown {drawdown_pct:.2f}% from peak {peak} breached "
                f"{self._limits.max_drawdown_pct}% limit"
            )
            self._set_halt(HaltState.HALTED_DRAWDOWN, reason)
            return HaltAction(HaltState.HALTED_DRAWDOWN, reason, force_flat=True)

        day_equity = Decimal(anchor["equity"])
        daily_loss_pct = (day_equity - equity) / day_equity * 100
        if daily_loss_pct >= Decimal(str(self._limits.max_daily_loss_pct)):
            reason = (
                f"daily loss {daily_loss_pct:.2f}% from day start {day_equity} breached "
                f"{self._limits.max_daily_loss_pct}% limit"
            )
            self._set_halt(HaltState.HALTED_DAILY_LOSS, reason)
            return HaltAction(HaltState.HALTED_DAILY_LOSS, reason, force_flat=False)
        return None

    # -- kill switch ---------------------------------------------------------------

    def halt_manual(self, reason: str, *, close_positions: bool) -> HaltAction:
        self._set_halt(HaltState.HALTED_MANUAL, reason)
        return HaltAction(HaltState.HALTED_MANUAL, reason, force_flat=close_positions)

    def resume(self, who: str = "investor") -> None:
        """Manual reactivation (required after drawdown/manual halts, D-07)."""
        self._set_halt(HaltState.RUNNING, f"resumed by {who}")
        self._journal.record_event("RESUMED", "risk", {"who": who})
