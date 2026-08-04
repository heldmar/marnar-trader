"""Historical market-cap rank tests (D-43b).

Two properties matter. Ranks must be recomputed per day rather than carried
back from today — a coin that has since climbed must not be screened in early.
And ``ranks_at`` must never look forward, for the same reason ``pit.screen_at``
must not: a rank from after the decision is hindsight.
"""

from __future__ import annotations

import json

from trader.marketcap import (
    DAY_MS,
    RankHistory,
    build_rank_history,
    load_raw,
    ranks_at,
    save_raw,
)


def series(*pairs: tuple[int, float]) -> list[tuple[int, float]]:
    return [(day * DAY_MS, cap) for day, cap in pairs]


def test_ranks_are_recomputed_each_day():
    """The whole point: BTC leads on day 0, ALT overtakes it on day 1."""
    histories = {
        "bitcoin": series((0, 1_000.0), (1, 1_000.0)),
        "alt": series((0, 500.0), (1, 5_000.0)),
    }
    ranks = build_rank_history(histories, symbol_of={"bitcoin": "BTC", "alt": "ALT"})
    assert ranks[0] == {"BTC": 1, "ALT": 2}
    assert ranks[1 * DAY_MS] == {"ALT": 1, "BTC": 2}


def test_zero_and_missing_caps_are_dropped():
    histories = {"a": series((0, 0.0)), "b": series((0, 10.0))}
    ranks = build_rank_history(histories, symbol_of={"a": "A", "b": "B"})
    assert ranks[0] == {"B": 1}


def test_coins_without_a_symbol_mapping_are_ignored():
    histories = {"known": series((0, 10.0)), "mystery": series((0, 99.0))}
    ranks = build_rank_history(histories, symbol_of={"known": "K"})
    assert ranks[0] == {"K": 1}


def test_ticker_collision_keeps_the_larger_cap():
    """Two coins genuinely share tickers; production's screener resolves this
    highest-rank-wins, so the historical version must agree."""
    histories = {"big": series((0, 900.0)), "impostor": series((0, 3.0))}
    ranks = build_rank_history(histories, symbol_of={"big": "X", "impostor": "X"})
    assert ranks[0] == {"X": 1}


def test_timestamps_are_floored_to_utc_midnight():
    histories = {"a": [(DAY_MS + 3_600_000, 10.0)]}
    ranks = build_rank_history(histories, symbol_of={"a": "A"})
    assert list(ranks) == [DAY_MS]


# -- the no-lookahead property ---------------------------------------------------


def test_ranks_at_uses_the_most_recent_past_day():
    history = {0: {"A": 1}, 5 * DAY_MS: {"A": 2}, 10 * DAY_MS: {"A": 3}}
    assert ranks_at(history, 7 * DAY_MS) == {"A": 2}
    assert ranks_at(history, 5 * DAY_MS) == {"A": 2}  # exact day is in the past


def test_ranks_at_never_looks_forward():
    history = {10 * DAY_MS: {"A": 1}}
    assert ranks_at(history, 5 * DAY_MS) is None


def test_ranks_at_on_empty_history():
    assert ranks_at({}, 5 * DAY_MS) is None


# -- RankHistory cache -----------------------------------------------------------


def test_missing_cache_file_is_not_an_error(tmp_path):
    history = RankHistory(tmp_path / "nope.json")
    assert history.by_day == {}
    assert history.covered is None
    assert history.at(0) is None


def test_save_then_reload_roundtrips(tmp_path):
    path = tmp_path / "ranks.json"
    history = RankHistory(path)
    history.by_day = {0: {"A": 1}, DAY_MS: {"A": 1, "B": 2}}
    history.save(meta={"source": "test"})

    reloaded = RankHistory(path)
    assert reloaded.by_day == history.by_day
    assert reloaded.meta["source"] == "test"
    assert reloaded.covered == (0, DAY_MS)
    # Keys survive the JSON string-key round trip as ints.
    assert all(isinstance(k, int) for k in reloaded.by_day)


def test_for_dates_omits_dates_outside_coverage(tmp_path):
    """Partial coverage is the expected state on the free tier — uncovered
    screening dates must be omitted so pit skips the criterion there rather
    than screening on stale ranks."""
    history = RankHistory(tmp_path / "r.json")
    history.by_day = {100 * DAY_MS: {"A": 1}}
    got = history.for_dates([50 * DAY_MS, 150 * DAY_MS, 200 * DAY_MS])
    assert set(got) == {150 * DAY_MS, 200 * DAY_MS}
    assert 50 * DAY_MS not in got


def test_save_is_atomic_leaving_no_tmp_file(tmp_path):
    path = tmp_path / "ranks.json"
    history = RankHistory(path)
    history.by_day = {0: {"A": 1}}
    history.save()
    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(path.read_text())["ranks"] == {"0": {"A": 1}}


# -- raw checkpoint: the sweep costs hours, so stopping must not cost the whole run


def test_missing_checkpoint_returns_empty_rather_than_raising(tmp_path):
    assert load_raw(tmp_path / "absent.json") == ({}, {})


def test_checkpoint_roundtrips_with_int_and_float_types_intact(tmp_path):
    path = tmp_path / "raw.json"
    save_raw(path, {"bitcoin": [(DAY_MS, 1.5e12)]}, {"bitcoin": "BTC"})
    histories, symbol_of = load_raw(path)
    assert symbol_of == {"bitcoin": "BTC"}
    # JSON has no int/float distinction; ranks are keyed by int day, so a
    # reloaded timestamp that came back as a float would miss every lookup.
    (ts, cap), = histories["bitcoin"]
    assert isinstance(ts, int) and ts == DAY_MS
    assert cap == 1.5e12


def test_corrupt_checkpoint_is_discarded_not_fatal(tmp_path):
    path = tmp_path / "raw.json"
    path.write_text('{"histories": {"bitcoin": [[1, 2.0')  # truncated mid-write
    assert load_raw(path) == ({}, {})


def test_checkpoint_write_is_atomic(tmp_path):
    path = tmp_path / "raw.json"
    save_raw(path, {"a": [(0, 1.0)]}, {"a": "A"})
    assert not list(tmp_path.glob("*.tmp"))


def test_reloaded_checkpoint_feeds_build_rank_history(tmp_path):
    """The point of the checkpoint: a resumed sweep must produce the same ranks
    as an uninterrupted one, not merely reload without error."""
    histories = {"a": [(0, 3.0)], "b": [(0, 9.0)]}
    symbol_of = {"a": "A", "b": "B"}
    path = tmp_path / "raw.json"
    save_raw(path, histories, symbol_of)
    reloaded, reloaded_symbols = load_raw(path)
    assert build_rank_history(reloaded, symbol_of=reloaded_symbols) == build_rank_history(
        histories, symbol_of=symbol_of
    )
