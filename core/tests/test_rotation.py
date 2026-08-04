"""Screener-driven universe rotation (D-09, clock-exempt per D-42a).

Three things must hold or this feature is dangerous rather than useful: a held
position is never rotated away from its exit logic, a bad screen changes
nothing, and rotation never touches the paper clock that a manual symbols edit
does reset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trader.config import AppConfig, PaperConfig
from trader.journal import Journal
from trader.rotation import UniverseRotator


class FakeEngine:
    """Just enough of PaperEngine: the real set_symbols semantics are pinned in
    test_engine.py; here we care about what the rotator does with them."""

    def __init__(self, symbols, held=()):
        self.symbols = list(symbols)
        self.held = set(held)
        self.calls = []

    def set_symbols(self, symbols):
        self.calls.append(list(symbols))
        wanted = list(dict.fromkeys(symbols))
        kept = [s for s in self.symbols if s not in wanted and s in self.held]
        added = [s for s in wanted if s not in self.symbols]
        removed = [s for s in self.symbols if s not in wanted + kept]
        self.symbols = wanted + kept
        return {"added": added, "removed": removed, "kept_for_open_position": kept}


@pytest.fixture
def world(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    config = AppConfig(paper=PaperConfig(symbols=["AAAUSDT", "BBBUSDT"]))
    state = SimpleNamespace(config=config, engine=FakeEngine(["AAAUSDT", "BBBUSDT"]))
    sent = []

    def rotator(picks, **kw):
        rot = UniverseRotator(
            journal, state, public=None,
            config_path=tmp_path / "config.yaml",
            alerts=SimpleNamespace(send=sent.append),
            **kw,
        )
        rot.screen = lambda: picks
        return rot

    yield {"journal": journal, "state": state, "rotator": rotator, "sent": sent,
           "path": tmp_path / "config.yaml"}
    journal.close()


def kinds(journal) -> list[str]:
    return [r["kind"] for r in journal.events_of_kind(
        ["UNIVERSE_ROTATED", "ROTATION_SKIPPED", "PAPER_CLOCK_RESET"], limit=50)]


# -- the clock exemption ----------------------------------------------------------


def test_rotation_never_resets_the_paper_clock(world):
    """D-42a: a manual symbols edit resets the clock, an automated rotation does
    not. If this ever flips, the three-week gate becomes unreachable."""
    world["journal"].set_state("engine:paper_started_at", 1_700_000_000_000)
    world["rotator"](["CCCUSDT", "DDDUSDT"]).rotate_once()
    assert world["journal"].get_state("engine:paper_started_at") == 1_700_000_000_000
    assert "PAPER_CLOCK_RESET" not in kinds(world["journal"])


def test_rotation_is_journalled_under_its_own_event_kind(world):
    """The audit trail has to distinguish automated rotation from a manual
    edit, because only one of them resets the clock."""
    world["rotator"](["CCCUSDT"]).rotate_once()
    events = world["journal"].events_of_kind(["UNIVERSE_ROTATED"], limit=5)
    assert len(events) == 1


# -- safety -----------------------------------------------------------------------


def test_a_symbol_with_an_open_position_is_never_rotated_out(world):
    """The engine is the only thing that can exit it — dropping it would strand
    the position with no sell rule and no protective stop."""
    world["state"].engine.held = {"BBBUSDT"}
    change = world["rotator"](["CCCUSDT"]).rotate_once()
    assert change["kept_for_open_position"] == ["BBBUSDT"]
    assert "BBBUSDT" in world["state"].engine.symbols
    assert "BBBUSDT" not in change["removed"]


def test_an_empty_screen_changes_nothing(world):
    before = list(world["state"].engine.symbols)
    assert world["rotator"]([]).rotate_once() is None
    assert world["state"].engine.symbols == before
    assert "ROTATION_SKIPPED" in kinds(world["journal"])


def test_an_unchanged_screen_is_a_no_op(world):
    """Order must not count as a change — rewriting config and alerting weekly
    for nothing would be noise."""
    assert world["rotator"](["BBBUSDT", "AAAUSDT"]).rotate_once() is None
    assert world["state"].engine.calls == []
    assert kinds(world["journal"]) == []


def test_rotation_without_a_running_engine_does_nothing(world):
    world["state"].engine = None
    assert world["rotator"](["CCCUSDT"]).rotate_once() is None


def test_a_failing_screen_does_not_change_the_universe(world):
    rot = world["rotator"](["CCCUSDT"])

    def boom():
        raise RuntimeError("CoinGecko down")

    rot.screen = boom
    with pytest.raises(RuntimeError):
        rot.rotate_once()  # run() catches this; rotate_once must not swallow it
    assert world["state"].engine.symbols == ["AAAUSDT", "BBBUSDT"]


# -- persistence ------------------------------------------------------------------


def test_the_new_universe_is_persisted_so_a_restart_keeps_it(world):
    world["rotator"](["CCCUSDT", "DDDUSDT"]).rotate_once()
    assert world["state"].config.paper.symbols == ["CCCUSDT", "DDDUSDT"]
    assert world["path"].exists()
    assert "CCCUSDT" in world["path"].read_text()


def test_persisted_symbols_include_positions_held_past_the_screen(world):
    """A restart must not forget the pair we are still holding."""
    world["state"].engine.held = {"BBBUSDT"}
    world["rotator"](["CCCUSDT"]).rotate_once()
    assert world["state"].config.paper.symbols == ["CCCUSDT", "BBBUSDT"]


def test_the_investor_is_told_what_moved(world):
    world["rotator"](["CCCUSDT"]).rotate_once()
    assert any("universe rotated" in m for m in world["sent"])
    assert any("D-42a" in m for m in world["sent"])


# -- configuration ----------------------------------------------------------------


def test_rotation_is_off_by_default():
    """Switching it on changes the book the paper gate measures, so it is a
    deliberate act rather than a default."""
    assert PaperConfig().rotate_universe is False


def test_symbols_edits_still_reset_the_clock_but_rotation_settings_do_not():
    from trader.api import CLOCK_RESETTING_FIELDS

    assert "symbols" in CLOCK_RESETTING_FIELDS
    assert "rotate_universe" not in CLOCK_RESETTING_FIELDS
    assert "rotate_seconds" not in CLOCK_RESETTING_FIELDS
