"""Tests for turning raw battles into canonical aggregates.

The load-bearing one is ``test_team_battle_yields_three_wins_and_three_losses``.
A battlelog is written from the log owner's perspective, so scoring the far team
means inverting ``result``. Getting that backwards would invert half the corpus
and still look entirely plausible -- every rate would sit near 0.5, every
distribution would look sane. The only thing that catches it is the mechanical
constraint that a 3v3 battle produces exactly three wins and three losses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from collectors.brawl_api.normalize import (
    Outcome,
    aggregate,
    participants,
    trophy_bucket,
)

UTC = timezone.utc
START = datetime(2026, 8, 3, tzinfo=UTC)
END = START + timedelta(days=1)
OBSERVED = END + timedelta(hours=6)


def player(name: str, trophies: int = 700) -> dict:
    return {
        "tag": f"#{name}",
        "name": name.lower(),
        "brawler": {"id": 1, "name": name, "power": 11, "trophies": trophies},
    }


def team_battle(result: str, near: list[str], far: list[str], mode: str = "gemGrab") -> dict:
    return {
        "battleTime": "20260803T120000.000Z",
        "event": {"id": 15000123, "mode": mode, "map": "Hard Rock Mine"},
        "battle": {
            "mode": mode,
            "result": result,
            "teams": [
                [player(n) for n in near],
                [player(n) for n in far],
            ],
        },
    }


def showdown_battle(rank: int, names: list[str]) -> dict:
    return {
        "battleTime": "20260803T120000.000Z",
        "event": {"id": 15000456, "mode": "soloShowdown", "map": "Skull Creek"},
        "battle": {
            "mode": "soloShowdown",
            "rank": rank,
            "players": [player(n) for n in names],
        },
    }


def wrap(battle: dict) -> dict:
    return {"battle_key": "k", "fetched_at": "2026-08-03T12:00:00Z", "surfaced_by": "#X", "battle": battle}


# --------------------------------------------------------------------------
# Team battles: every slot is scoreable
# --------------------------------------------------------------------------


def test_team_battle_yields_three_wins_and_three_losses():
    """The constraint the whole mechanical check rests on."""
    found = participants(team_battle("victory", ["A", "B", "C"], ["D", "E", "F"]))
    outcomes = [entry.outcome for entry in found]

    assert len(found) == 6
    assert outcomes.count(Outcome.WIN) == 3
    assert outcomes.count(Outcome.LOSS) == 3


def test_the_far_team_gets_the_inverse_of_the_logged_result():
    """`result` describes teams[0] only. Inverting it for teams[1] is the point."""
    found = {e.brawler_id: e.outcome for e in participants(
        team_battle("defeat", ["A", "B", "C"], ["D", "E", "F"])
    )}
    assert found["A"] == Outcome.LOSS
    assert found["D"] == Outcome.WIN


def test_a_drawn_battle_draws_for_everyone():
    found = participants(team_battle("draw", ["A", "B", "C"], ["D", "E", "F"]))
    assert {e.outcome for e in found} == {Outcome.DRAW}
    assert len(found) == 6


def test_pooled_rate_over_many_team_battles_is_exactly_one_half():
    """The mechanical baseline, reproduced end to end through aggregation.

    Any inversion, dropped participant, or double count breaks this.
    """
    battles = [
        wrap(team_battle("victory" if i % 3 else "defeat", ["A", "B", "C"], ["D", "E", "F"]))
        for i in range(30)
    ] + [wrap(team_battle("draw", ["A", "B", "C"], ["D", "E", "F"])) for _ in range(7)]

    rows = aggregate(
        battles, window_start=START, window_end=END, observed_at=OBSERVED, source_id="t"
    )
    points = sum(r.brawler_wins + 0.5 * r.brawler_draws for r in rows)
    total = sum(r.brawler_battles for r in rows)

    assert total == 6 * 37
    assert points / total == pytest.approx(0.5)


# --------------------------------------------------------------------------
# Showdown: only one slot in ten is scoreable
# --------------------------------------------------------------------------


def test_showdown_scores_only_the_log_owner():
    """The API reports a rank for one player; inventing the rest would fabricate."""
    found = participants(showdown_battle(2, [f"P{i}" for i in range(10)]))

    assert len(found) == 10
    assert found[0].outcome == Outcome.WIN
    assert [e.outcome for e in found[1:]] == [Outcome.UNKNOWN] * 9


@pytest.mark.parametrize(
    "rank,expected",
    [(1, Outcome.WIN), (4, Outcome.WIN), (5, Outcome.DRAW), (6, Outcome.LOSS), (10, Outcome.LOSS)],
)
def test_showdown_rank_maps_to_the_games_own_thresholds(rank, expected):
    """Ranks 1-4 win, 5th draws, 6-10 lose -- taken from the mode's mechanics."""
    found = participants(showdown_battle(rank, [f"P{i}" for i in range(10)]))
    assert found[0].outcome == expected


def test_unscored_participants_still_count_as_slots():
    """Use rate remains measurable even where win rate is not.

    Those nine players genuinely picked their brawlers, so the pick is observed
    even though the placement is not.
    """
    rows = aggregate(
        [wrap(showdown_battle(3, [f"P{i}" for i in range(10)]))],
        window_start=START,
        window_end=END,
        observed_at=OBSERVED,
        source_id="t",
    )
    assert len(rows) == 1  # only the log owner produced a scored cell
    assert rows[0].brawler_battles == 1
    assert rows[0].stratum_slots == 10  # but all ten occupied slots


# --------------------------------------------------------------------------
# Stratification and robustness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trophies,expected",
    [(0, "low"), (499, "low"), (500, "mid"), (999, "mid"), (1000, "high"), (5000, "high")],
)
def test_trophy_buckets_partition_the_range(trophies, expected):
    assert trophy_bucket(trophies) == expected


def test_missing_trophies_drops_the_participant():
    """A slot we cannot stratify cannot contribute to a standardized metric."""
    assert trophy_bucket(None) is None
    battle = team_battle("victory", ["A", "B", "C"], ["D", "E", "F"])
    del battle["battle"]["teams"][0][0]["brawler"]["trophies"]
    assert len(participants(battle)) == 5


def test_unknown_mode_is_dropped_not_guessed():
    battle = team_battle("victory", ["A", "B", "C"], ["D", "E", "F"], mode="someFutureMode")
    assert participants(battle) == []


def test_battle_without_a_map_is_dropped():
    battle = team_battle("victory", ["A", "B", "C"], ["D", "E", "F"])
    battle["event"]["map"] = None
    assert participants(battle) == []


def test_malformed_battle_does_not_raise():
    assert participants({}) == []
    assert participants({"battle": {}}) == []
    assert participants({"battle": {"mode": "gemGrab"}, "event": {}}) == []


def test_participants_split_across_trophy_buckets():
    """A stratum is a property of the slot, not of the match."""
    battle = team_battle("victory", ["A", "B", "C"], ["D", "E", "F"])
    battle["battle"]["teams"][0][0]["brawler"]["trophies"] = 100   # low
    battle["battle"]["teams"][1][0]["brawler"]["trophies"] = 1500  # high

    buckets = {e.brawler_id: e.trophy_bucket for e in participants(battle)}
    assert buckets["A"] == "low"
    assert buckets["D"] == "high"
    assert buckets["B"] == "mid"


def test_aggregate_produces_rows_the_schema_accepts():
    """The output must be loadable by the settlement layer without translation."""
    rows = aggregate(
        [wrap(team_battle("victory", ["A", "B", "C"], ["D", "E", "F"]))],
        window_start=START,
        window_end=END,
        observed_at=OBSERVED,
        source_id="t",
    )
    assert rows
    for row in rows:
        assert row.observed_at >= row.window_end
        assert row.brawler_wins + row.brawler_draws <= row.brawler_battles
        assert row.brawler_battles <= row.stratum_slots
