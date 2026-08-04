"""Walk-forward selection tests (D-43c).

The properties that matter are the ones that stop this from becoming another
hindsight number: selection must see only finished periods, an unchained run
must refuse to quote a compounded total, and a skipped period must count as
flat rather than vanish.
"""

from __future__ import annotations

import pytest

from tests.test_pit import make_trending_series
from trader.config import ScreenerConfig
from trader.pit import DAY_MS, PitResult, run_point_in_time
from trader.walkforward import (
    BASELINE,
    Params,
    Step,
    WalkForwardResult,
    _compound,
    score,
    walk_forward,
)

CFG = ScreenerConfig(
    min_24h_quote_volume=1_000_000.0,
    min_24h_range_pct=1.0,
    max_pairs=3,
    max_market_cap_rank=100,
)


def universe() -> dict:
    return {
        f"{c}USDT": make_trending_series(quote_volume=9_000_000.0 - i * 1000)
        for i, c in enumerate("ABCDE")
    }


# -- the guard against quoting an unchained total --------------------------------


def test_unchained_run_refuses_to_quote_a_compounded_total():
    """Each period restarts at initial_cash, so a compounded figure would be a
    return the account never earned. It must raise, not round."""
    result = run_point_in_time(
        universe(), start_ms=100 * DAY_MS, periods=3, config=CFG, chain_equity=False
    )
    with pytest.raises(ValueError, match="did not chain equity"):
        _ = result.total_return_pct
    with pytest.raises(ValueError, match="did not chain equity"):
        _ = result.final_equity
    # Per-period figures stay available — that is the whole point of the mode.
    assert result.mean_period_return_pct == result.mean_period_return_pct
    assert "unchained" in result.summary()


def test_unchained_periods_all_start_from_the_same_capital():
    result = run_point_in_time(
        universe(), start_ms=100 * DAY_MS, periods=3, config=CFG,
        initial_cash=150.0, chain_equity=False,
    )
    assert [p.start_equity for p in result.periods] == [150.0] * len(result.periods)


def test_chained_is_the_default_and_still_compounds():
    result = run_point_in_time(universe(), start_ms=100 * DAY_MS, periods=3, config=CFG)
    assert result.chained
    for prev, cur in zip(result.periods, result.periods[1:], strict=False):
        assert cur.start_equity == prev.end_equity
    assert isinstance(result.total_return_pct, float)


# -- selection may not see the future --------------------------------------------


def test_selection_uses_only_periods_before_the_one_it_is_scored_on():
    """The whole exercise collapses if the chooser can see the period it is
    about to be graded on. Assert it through the score: the chosen setting must
    be the argmax over the *training* slice, never over the full history."""
    calls: list[int] = []
    real_score = score

    def spy(returns):
        calls.append(len(returns))
        return real_score(returns)

    import trader.walkforward as wf

    wf.score = spy
    try:
        result = walk_forward(
            universe(),
            start_ms=100 * DAY_MS,
            periods=6,
            config=CFG,
            grid=[(15, 15, 3.0), (20, 10, 5.0)],
            min_train=3,
        )
    finally:
        wf.score = real_score

    # Training slices are 3, 4, 5 long for k = 3, 4, 5 — never 6.
    assert max(calls) < 6
    assert [s.period for s in result.steps] == [3, 4, 5]


def test_min_train_is_respected():
    result = walk_forward(
        universe(), start_ms=100 * DAY_MS, periods=8, config=CFG,
        grid=[(15, 15, 3.0), (20, 10, 5.0)], min_train=5,
    )
    assert [s.period for s in result.steps] == [5, 6, 7]


def test_baseline_is_always_in_the_grid():
    """Even if the caller's grid omits 15/15/3.0, the comparison needs it."""
    result = walk_forward(
        universe(), start_ms=100 * DAY_MS, periods=5, config=CFG,
        grid=[(20, 10, 5.0)], min_train=3,
    )
    assert result.grid_size == 2
    assert all(isinstance(s.baseline_return_pct, float) for s in result.steps)


def test_baseline_matches_production_parameters():
    assert BASELINE == (15, 15, 3.0)


# -- scoring ---------------------------------------------------------------------


def test_score_prefers_consistency_over_one_lucky_period():
    """A setting that made +40% once and lost the rest must not outrank a
    setting that made a steady +5% — that preference is the only thing standing
    between this and refitting D-40."""
    lucky = [40.0, -8.0, -8.0, -8.0]
    steady = [5.0, 4.0, 5.0, 6.0]
    assert score(steady) > score(lucky)


def test_score_of_nothing_is_negative_infinity():
    assert score([]) == float("-inf")


def test_compounding_is_multiplicative_not_additive():
    assert _compound([10.0, -10.0]) == pytest.approx(-1.0)
    assert _compound([]) == 0.0


# -- reporting -------------------------------------------------------------------


def test_skipped_periods_count_as_flat_not_missing():
    """A period the screen skipped is cash, 0%. Dropping it would let a setting
    that trades rarely look good by simply having fewer scored periods."""
    # A universe nothing can screen into: everything is a de-facto peg.
    from tests.test_pit import make_candles
    from trader.walkforward import period_returns

    flat = {"AUSDT": make_candles(400, range_pct=0.01)}
    returns = period_returns(
        flat, Params(15, 15, 3.0), start_ms=100 * DAY_MS, periods=4, config=CFG
    )
    assert returns == [0.0, 0.0, 0.0, 0.0]


def test_stability_counts_how_often_each_setting_won():
    result = WalkForwardResult(
        steps=[
            Step(3, Params(15, 15, 3.0), 1.0, 2.0, 2.0),
            Step(4, Params(15, 15, 3.0), 1.0, 1.0, 1.0),
            Step(5, Params(20, 10, 5.0), 1.0, 3.0, 1.0),
        ]
    )
    assert result.stability == {"15/15/3": 2, "20/10/5": 1}
    assert result.periods_beating_baseline == 1


def test_a_short_evaluation_is_labelled_as_inconclusive():
    """Five quarters cannot establish an edge, and the report has to say so
    rather than let a favourable number speak for itself (D-43c)."""
    result = walk_forward(
        universe(), start_ms=100 * DAY_MS, periods=6, config=CFG,
        grid=[(15, 15, 3.0), (20, 10, 5.0)], min_train=3,
    )
    assert any("too few to establish an edge" in n for n in result.notes)
    assert "too few" in result.summary()


def test_summary_survives_having_no_evaluation_periods():
    assert "no evaluation periods" in WalkForwardResult().summary()


def test_pit_result_default_is_chained():
    assert PitResult().chained
