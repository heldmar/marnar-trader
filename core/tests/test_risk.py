"""Risk Manager tests — every limit, boundary values, tiered halt actions
(architecture §6 requires 100% branch coverage on trader/risk.py; CI enforces it),
plus the R-02 acceptance fixture: a runaway strategy that tries to violate every
limit and must be stopped each time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trader.risk import HaltState, RiskManager

EQUITY = Decimal("10000")
T0 = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def seed_position(journal, symbol: str, qty: str, price: str, n: int) -> None:
    """Open a journaled position via a filled BUY order."""
    intent_id = journal.record_intent(
        symbol=symbol, side="BUY", order_type="LIMIT", quantity=qty,
        price=price, source="seed", nonce=f"seed-{symbol}-{n}",
    )
    coid = journal.record_order_submitting(intent_id)
    journal.record_order_ack(coid, str(n), "FILLED")
    journal.record_fill(
        client_order_id=coid, fill_id=f"{symbol}-seed-{n}", quantity=qty,
        price=price, fee="0", fee_asset="USDT", executed_at="2026-07-13T00:00:00Z",
    )


# -- state & gate ------------------------------------------------------------------


def test_default_state_is_running(risk):
    assert risk.state == HaltState.RUNNING
    assert risk.trading_allowed()
    st = risk.status()
    assert st["state"] == "RUNNING" and st["reason"] is None


def test_intent_rejected_while_halted(risk):
    risk.halt_manual("test", close_positions=False)
    d = risk.evaluate_intent(symbol="BTCUSDT", side="BUY", notional=Decimal("10"),
                             equity=EQUITY)
    assert not d.approved and "halted" in d.reason


def test_trade_rate_cap(journal, risk, limits):
    for i in range(limits.max_trades_per_hour):
        seed_position(journal, "BTCUSDT", "0.00001", "50000", i)
    d = risk.evaluate_intent(symbol="BTCUSDT", side="SELL", notional=Decimal("10"),
                             equity=EQUITY)
    assert not d.approved and "trade-rate" in d.reason


def test_max_open_positions_blocks_new_symbol(journal, risk, limits):
    for i, sym in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]):
        seed_position(journal, sym, "1", "10", i)
    d = risk.evaluate_intent(symbol="NEWUSDT", side="BUY", notional=Decimal("10"),
                             equity=EQUITY)
    assert not d.approved and "max open positions" in d.reason


def test_max_open_positions_allows_adding_to_held_symbol(journal, risk):
    for i, sym in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]):
        seed_position(journal, sym, "1", "10", i)
    d = risk.evaluate_intent(symbol="AUSDT", side="BUY", notional=Decimal("10"),
                             equity=EQUITY)
    assert d.approved  # not a NEW position; per-coin cap still applies


def test_per_coin_cap_rejects_oversized_buy(risk):
    # 10% of 10000 = 1000; ask for 1001
    d = risk.evaluate_intent(symbol="BTCUSDT", side="BUY", notional=Decimal("1001"),
                             equity=EQUITY)
    assert not d.approved and "per-coin cap" in d.reason


def test_per_coin_cap_counts_existing_exposure(journal, risk):
    seed_position(journal, "BTCUSDT", "0.012", "50000", 1)  # 600 USDT exposure
    d = risk.evaluate_intent(symbol="BTCUSDT", side="BUY", notional=Decimal("500"),
                             equity=EQUITY)  # 600+500 > 1000
    assert not d.approved and "per-coin cap" in d.reason


def test_buy_within_all_limits_is_approved(risk):
    d = risk.evaluate_intent(symbol="BTCUSDT", side="BUY", notional=Decimal("999"),
                             equity=EQUITY)
    assert d.approved and d.reason == "ok"


def test_sell_is_exempt_from_position_caps(journal, risk):
    for i, sym in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]):
        seed_position(journal, sym, "1", "10", i)
    d = risk.evaluate_intent(symbol="AUSDT", side="SELL", notional=Decimal("99999"),
                             equity=EQUITY)
    assert d.approved


# -- equity-driven breakers ----------------------------------------------------------


def test_first_equity_sets_anchor_and_peak_no_trip(risk):
    assert risk.note_equity(EQUITY, now=T0) is None
    st = risk.status()
    assert st["equity_peak"] == "10000"
    assert st["day_anchor"] == {"date": "2026-07-13", "equity": "10000"}


def test_peak_ratchets_up_only(risk):
    risk.note_equity(EQUITY, now=T0)
    risk.note_equity(Decimal("10500"), now=T0)
    assert risk.status()["equity_peak"] == "10500"
    risk.note_equity(Decimal("10400"), now=T0)
    assert risk.status()["equity_peak"] == "10500"


def test_daily_loss_halts_and_holds_positions(risk):
    risk.note_equity(EQUITY, now=T0)
    action = risk.note_equity(Decimal("9800"), now=T0)  # exactly -2%
    assert action is not None
    assert action.state == HaltState.HALTED_DAILY_LOSS
    assert action.force_flat is False  # D-21: cooldown, HOLD positions
    assert not risk.trading_allowed()


def test_drawdown_halts_and_force_flats(risk):
    risk.note_equity(EQUITY, now=T0)
    action = risk.note_equity(Decimal("8000"), now=T0)  # -20% from peak
    assert action is not None
    assert action.state == HaltState.HALTED_DRAWDOWN
    assert action.force_flat is True  # D-21: capital preservation
    assert not risk.trading_allowed()


def test_drawdown_outranks_daily_loss(risk):
    risk.note_equity(EQUITY, now=T0)
    action = risk.note_equity(Decimal("7000"), now=T0)  # breaches both
    assert action.state == HaltState.HALTED_DRAWDOWN


def test_small_dip_trips_nothing(risk):
    risk.note_equity(EQUITY, now=T0)
    assert risk.note_equity(Decimal("9900"), now=T0) is None  # -1%
    assert risk.trading_allowed()


def test_no_new_trip_while_already_halted(risk):
    risk.note_equity(EQUITY, now=T0)
    assert risk.note_equity(Decimal("9800"), now=T0) is not None
    assert risk.note_equity(Decimal("7000"), now=T0) is None  # already halted
    assert risk.state == HaltState.HALTED_DAILY_LOSS


def test_daily_halt_auto_clears_at_day_rollover(risk):
    risk.note_equity(EQUITY, now=T0)
    risk.note_equity(Decimal("9800"), now=T0)
    assert risk.state == HaltState.HALTED_DAILY_LOSS
    next_day = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert risk.note_equity(Decimal("9800"), now=next_day) is None
    assert risk.state == HaltState.RUNNING  # D-06: halt is "for the day"
    assert risk.status()["day_anchor"]["date"] == "2026-07-14"


def test_drawdown_halt_survives_day_rollover(risk):
    risk.note_equity(EQUITY, now=T0)
    risk.note_equity(Decimal("8000"), now=T0)
    next_day = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    risk.note_equity(Decimal("9000"), now=next_day)
    assert risk.state == HaltState.HALTED_DRAWDOWN  # D-07: manual reactivation only


def test_halt_state_survives_restart(journal, limits):
    r1 = RiskManager(journal, limits)
    r1.note_equity(EQUITY, now=T0)
    r1.note_equity(Decimal("8000"), now=T0)
    # "restart": a fresh RiskManager over the same journal (no-UPS discipline)
    r2 = RiskManager(journal, limits)
    assert r2.state == HaltState.HALTED_DRAWDOWN
    assert not r2.trading_allowed()


# -- kill switch ----------------------------------------------------------------------


def test_kill_switch_halt_only(risk):
    action = risk.halt_manual("investor pressed halt", close_positions=False)
    assert action.state == HaltState.HALTED_MANUAL and action.force_flat is False
    assert not risk.trading_allowed()


def test_kill_switch_halt_and_close(risk):
    action = risk.halt_manual("investor pressed halt+close", close_positions=True)
    assert action.force_flat is True


def test_resume_restores_trading(risk):
    risk.halt_manual("test", close_positions=False)
    risk.resume()
    assert risk.trading_allowed()
    assert risk.status()["reason"] == "resumed by investor"


def test_resume_after_drawdown_is_not_a_one_way_door(risk):
    """D-36: the halt force-flats every position, so equity cannot recover
    without trading. If resume() left the high-water mark alone, the very next
    poll re-computed the same breach and re-halted — permanently."""
    risk.note_equity(EQUITY, now=T0)
    assert risk.note_equity(Decimal("8000"), now=T0).state == HaltState.HALTED_DRAWDOWN

    risk.resume()

    # The investor resumes the next day; nothing has recovered, equity is as
    # the halt left it. Pre-D-36 this re-halted on the spot.
    day2 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert risk.note_equity(Decimal("8000"), now=day2) is None
    assert risk.trading_allowed()
    # ...and stays open on the following poll — one quiet poll proves nothing.
    assert risk.note_equity(Decimal("8000"), now=day2) is None
    assert risk.trading_allowed()


def test_same_day_resume_after_drawdown_is_not_re_halted_by_the_daily_breaker(risk):
    """D-36 residual: clearing only the peak left a second door. A drawdown
    halt force-flats far below where the day opened, so a same-day resume trips
    the 2% daily breaker on the next poll — RUNNING for one cycle, then halted
    again, indistinguishable from the original bug."""
    risk.note_equity(EQUITY, now=T0)
    risk.note_equity(Decimal("8000"), now=T0)  # -20%, and -20% on the day too

    risk.resume()

    assert risk.note_equity(Decimal("8000"), now=T0) is None
    assert risk.trading_allowed()
    # The daily breaker re-arms from the resumed level rather than vanishing.
    assert risk.note_equity(Decimal("7900"), now=T0) is None  # -1.25%, inside
    action = risk.note_equity(Decimal("7830"), now=T0)  # -2.1% from 8000
    assert action is not None
    assert action.state == HaltState.HALTED_DAILY_LOSS


def test_resume_does_not_bypass_the_daily_loss_cooldown(risk):
    """D-06's halt is 'for the day' and must stay that way — the day anchor is
    cleared only when escaping a halt that force-flatted the book."""
    risk.note_equity(EQUITY, now=T0)
    assert risk.note_equity(Decimal("9800"), now=T0).state == HaltState.HALTED_DAILY_LOSS

    risk.resume()

    action = risk.note_equity(Decimal("9800"), now=T0)
    assert action is not None
    assert action.state == HaltState.HALTED_DAILY_LOSS  # cooldown survives
    assert not risk.trading_allowed()


def test_resume_re_anchors_the_peak_without_weakening_the_limit(risk):
    """D-07's 20% still bites in full — it is simply measured from the restart."""
    risk.note_equity(EQUITY, now=T0)
    risk.note_equity(Decimal("8000"), now=T0)
    assert risk.status()["equity_peak"] == "10000"

    risk.resume()
    assert risk.status()["equity_peak"] is None  # cleared, awaiting the next poll

    day2 = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    risk.note_equity(Decimal("8000"), now=day2)
    assert risk.status()["equity_peak"] == "8000"  # re-anchored to live equity

    day3 = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    action = risk.note_equity(Decimal("6400"), now=day3)  # -20.0% from 8000
    assert action is not None
    assert action.state == HaltState.HALTED_DRAWDOWN  # outranks the daily breaker
    assert action.force_flat is True


# -- R-02 acceptance: runaway strategy fixture ----------------------------------------


def test_runaway_strategy_is_stopped_on_every_front(journal, risk, limits):
    """A strategy gone rogue tries to violate every limit; the Risk Manager
    must stop all of them (architecture §6, R-02)."""
    # 1. oversized single position
    assert not risk.evaluate_intent(symbol="XUSDT", side="BUY",
                                    notional=EQUITY, equity=EQUITY).approved
    # 2. too many positions
    for i, sym in enumerate(["AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT"]):
        seed_position(journal, sym, "1", "10", i)
    assert not risk.evaluate_intent(symbol="FUSDT", side="BUY",
                                    notional=Decimal("10"), equity=EQUITY).approved
    # 3. machine-gun order spam (rate cap; 5 seeds already placed above)
    for i in range(limits.max_trades_per_hour - 5):
        seed_position(journal, "AUSDT", "0.001", "10", 100 + i)
    assert not risk.evaluate_intent(symbol="AUSDT", side="SELL",
                                    notional=Decimal("1"), equity=EQUITY).approved
    # 4. trading through a drawdown halt
    risk.note_equity(EQUITY, now=T0)
    assert risk.note_equity(Decimal("7999"), now=T0).force_flat
    assert not risk.evaluate_intent(symbol="AUSDT", side="BUY",
                                    notional=Decimal("1"), equity=EQUITY).approved
    # 5. trading through the kill switch after a manual resume-then-halt cycle
    risk.resume()
    risk.halt_manual("kill", close_positions=True)
    assert not risk.evaluate_intent(symbol="AUSDT", side="SELL",
                                    notional=Decimal("1"), equity=EQUITY).approved


# -- QA1-18: zero-equity must halt, never divide by zero ------------------------------


def test_zero_equity_halts_instead_of_dividing(risk):
    action = risk.note_equity(Decimal("0"), now=T0)
    assert action is not None and action.force_flat
    assert risk.state == HaltState.HALTED_DRAWDOWN


def test_zero_day_anchor_reseeds_instead_of_dividing(risk, journal):
    # A zero anchor reaches a RUNNING manager only by surviving a process
    # restart: note_equity halts immediately on zero equity, and D-36's resume
    # now clears the anchor on the way out. What persists is the journal, so
    # seed it the way a crash would have left it and restart from there.
    journal.set_state("risk.day_anchor", {"date": "2026-07-13", "equity": "0"})
    assert risk.state == HaltState.RUNNING

    # Equity recovered: the stale zero anchor is re-seeded, nothing divides.
    assert risk.note_equity(Decimal("100"), now=T0) is None
    # And the breakers work again from the fresh anchor (3% > 2% daily limit).
    action = risk.note_equity(Decimal("97"), now=T0)
    assert action is not None and action.state == HaltState.HALTED_DAILY_LOSS
