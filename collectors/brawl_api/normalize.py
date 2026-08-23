"""Turn raw battles into the canonical aggregates settlement reads.

This is the step that makes the corpus affordable. A raw battle costs about 227
bytes gzipped; the normalized facts settlement actually consumes cost about 35.
Rolling up and pruning is therefore a 6x reduction, which is the difference
between 41 GB and 6 GB after a year at half a million battles a day.

**What can and cannot be scored, and why it matters.**

A battlelog is written from the perspective of the player whose log it is.
``teams[0]`` is their team and ``result`` is their result. That is enough to
score a 3v3 battle completely: the log owner's team gets ``result``, the other
team gets its inverse, and all six slots are accounted for. This is why the
mechanical baseline holds for team modes -- we genuinely observe three wins and
three losses per battle, so the pooled rate over all brawlers is exactly 0.500
and any deviation is a pipeline defect.

Showdown is different and worse. The API reports ``rank`` for the log owner
only; the other nine players appear in the roster with no placement attached.
So we can score exactly one of ten slots, and that one belongs to a player our
crawler chose to visit -- which is a selection effect, not a composition
effect, and standardization does not remove it.

Two consequences are recorded rather than papered over:

  * Showdown participants other than the log owner are counted toward
    ``stratum_slots`` -- they were genuinely picked, so use rate is still
    measurable -- but contribute no battles or wins.
  * The mechanical baseline check applies to team modes only. For Showdown the
    observed pooled rate reflects crawled-player skill, not the game's 0.450.

The practical recommendation that follows: write the first contracts on 3v3
modes. Showdown win-rate contracts rest on a tenth of the evidence and a
selection effect nobody has modelled yet.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from arena.worlds.brawl.modes import MECHANICS
from arena.worlds.brawl.schema import AggregateRow

__all__ = [
    "Outcome",
    "Participant",
    "TROPHY_BUCKETS",
    "trophy_bucket",
    "participants",
    "aggregate",
    "read_shard",
]


class Outcome:
    WIN = "win"
    LOSS = "loss"
    DRAW = "draw"
    # Observed in the battle, but with no placement reported. Counts toward the
    # slot denominator for use rate; contributes nothing to a win rate.
    UNKNOWN = "unknown"


# Stratum boundaries on a brawler's trophy count. These are *definitional*
# rather than estimated -- they declare what a stratum is, the way a contract
# declares its observation window -- so they are a named constant rather than a
# fitted parameter. They are recorded in the normalization provenance, and could
# reasonably be replaced by population quantiles once a corpus exists.
TROPHY_BUCKETS: tuple[tuple[str, int], ...] = (
    ("low", 500),
    ("mid", 1000),
    ("high", 1 << 30),
)


def trophy_bucket(trophies: int | None) -> str | None:
    """Which trophy stratum a brawler's trophy count falls in."""
    if trophies is None or trophies < 0:
        return None
    for name, ceiling in TROPHY_BUCKETS:
        if trophies < ceiling:
            return name
    return TROPHY_BUCKETS[-1][0]


@dataclass(frozen=True, slots=True)
class Participant:
    """One brawler's appearance in one battle, with whatever outcome is known."""

    brawler_id: str
    mode_id: str
    map_id: str
    trophy_bucket: str
    outcome: str


_INVERSE = {Outcome.WIN: Outcome.LOSS, Outcome.LOSS: Outcome.WIN, Outcome.DRAW: Outcome.DRAW}
_RESULT_TO_OUTCOME = {
    "victory": Outcome.WIN,
    "defeat": Outcome.LOSS,
    "draw": Outcome.DRAW,
}


def participants(battle: dict[str, Any]) -> list[Participant]:
    """Extract every brawler appearance from a raw battle, with its outcome.

    Returns an empty list for a battle we cannot place -- an unknown mode, a
    missing map, a shape the API has not shown us before. Dropping is correct:
    a battle we cannot stratify cannot contribute to a standardized metric, and
    guessing its stratum would corrupt one.
    """
    event = battle.get("event") or {}
    detail = battle.get("battle") or {}
    mode = detail.get("mode") or event.get("mode")
    map_id = event.get("map")
    if not mode or not map_id or mode not in MECHANICS:
        return []

    teams = detail.get("teams") or []
    if teams:
        return _team_participants(teams, detail, mode, map_id)
    return _showdown_participants(detail, mode, map_id)


def _team_participants(
    teams: list[list[dict[str, Any]]],
    detail: dict[str, Any],
    mode: str,
    map_id: str,
) -> list[Participant]:
    """Score every slot of a team battle.

    ``teams[0]`` is the log owner's side, so ``result`` describes them and its
    inverse describes everyone else. Getting this backwards would invert half
    the corpus and still look entirely plausible, which is why the pooled-rate
    check against 0.500 is worth having.
    """
    outcome = _RESULT_TO_OUTCOME.get(str(detail.get("result", "")).lower())
    if outcome is None:
        return []
    # Duo Showdown also reports `teams`, but of five pairs rather than two
    # sides, and `rank` rather than `result`. Two teams is the signature of a
    # genuine 3v3; anything else is not scoreable from one perspective.
    if len(teams) != 2:
        return _unscored(
            [player for team in teams for player in (team or [])], mode, map_id
        )

    found: list[Participant] = []
    for index, team in enumerate(teams):
        side = outcome if index == 0 else _INVERSE[outcome]
        for player in team or []:
            entry = _participant(player, mode, map_id, side)
            if entry is not None:
                found.append(entry)
    return found


def _showdown_participants(
    detail: dict[str, Any], mode: str, map_id: str
) -> list[Participant]:
    """Score the log owner only; record everyone else as observed-but-unscored.

    The API gives ``rank`` for one player. The other nine are named with no
    placement, so inventing one would fabricate outcomes.
    """
    players = detail.get("players") or []
    if not players:
        return []

    mechanics = MECHANICS[mode]
    rank = detail.get("rank")
    entries = _unscored(players, mode, map_id)
    if rank is None or not entries:
        return entries

    # The log owner is listed first in the shapes observed so far. If that ever
    # stops holding, the effect is one mis-scored slot in ten rather than a
    # systematic inversion -- and the use-rate denominator is unaffected.
    if rank <= mechanics.winning_slots:
        owner_outcome = Outcome.WIN
    elif rank <= mechanics.winning_slots + mechanics.drawing_slots:
        owner_outcome = Outcome.DRAW
    else:
        owner_outcome = Outcome.LOSS

    first = entries[0]
    entries[0] = Participant(
        brawler_id=first.brawler_id,
        mode_id=first.mode_id,
        map_id=first.map_id,
        trophy_bucket=first.trophy_bucket,
        outcome=owner_outcome,
    )
    return entries


def _unscored(
    players: Iterable[dict[str, Any]], mode: str, map_id: str
) -> list[Participant]:
    found: list[Participant] = []
    for player in players:
        entry = _participant(player, mode, map_id, Outcome.UNKNOWN)
        if entry is not None:
            found.append(entry)
    return found


def _participant(
    player: dict[str, Any], mode: str, map_id: str, outcome: str
) -> Participant | None:
    brawler = (player or {}).get("brawler") or {}
    name = brawler.get("name")
    bucket = trophy_bucket(brawler.get("trophies"))
    if not name or bucket is None:
        return None
    return Participant(
        brawler_id=str(name).upper(),
        mode_id=mode,
        map_id=str(map_id),
        trophy_bucket=bucket,
        outcome=outcome,
    )


def read_shard(path: str | Path) -> Iterator[dict[str, Any]]:
    """Stream records out of one gzipped raw shard.

    Streaming rather than loading: a day's shard can hold a million battles,
    and the whole point of this module is to be runnable on a laptop.
    """
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def aggregate(
    records: Iterable[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    observed_at: datetime,
    source_id: str,
) -> list[AggregateRow]:
    """Fold raw battle records into canonical aggregate rows.

    Each battle is counted once, however many players' logs surfaced it -- the
    caller is responsible for having deduplicated, which the collector's store
    already does at write time.
    """
    # (brawler, mode, map, bucket) -> [battles, wins, draws]
    cells: dict[tuple[str, str, str, str], list[int]] = {}
    # (mode, map, bucket) -> [distinct battles, total slots]
    strata: dict[tuple[str, str, str], list[int]] = {}

    for record in records:
        found = participants(record.get("battle") or {})
        if not found:
            continue

        seen_strata: set[tuple[str, str, str]] = set()
        for entry in found:
            stratum = (entry.mode_id, entry.map_id, entry.trophy_bucket)
            totals = strata.setdefault(stratum, [0, 0])
            totals[1] += 1  # every appearance is a slot, scored or not
            seen_strata.add(stratum)

            if entry.outcome == Outcome.UNKNOWN:
                continue
            cell = cells.setdefault(
                (entry.brawler_id, *stratum), [0, 0, 0]
            )
            cell[0] += 1
            if entry.outcome == Outcome.WIN:
                cell[1] += 1
            elif entry.outcome == Outcome.DRAW:
                cell[2] += 1

        # A battle spanning several trophy buckets counts as a battle in each of
        # them, because the stratum is a property of the slot rather than of the
        # match. Counting it once globally would understate every bucket.
        for stratum in seen_strata:
            strata[stratum][0] += 1

    rows: list[AggregateRow] = []
    for (brawler, mode, map_id, bucket), (battles, wins, draws) in sorted(cells.items()):
        stratum_battles, stratum_slots = strata[(mode, map_id, bucket)]
        rows.append(
            AggregateRow(
                observed_at=observed_at,
                window_start=window_start,
                window_end=window_end,
                brawler_id=brawler,
                mode_id=mode,
                map_id=map_id,
                trophy_bucket=bucket,
                brawler_battles=battles,
                brawler_wins=wins,
                brawler_draws=draws,
                stratum_battles=stratum_battles,
                stratum_slots=stratum_slots,
                source_id=source_id,
            )
        )
    return rows
