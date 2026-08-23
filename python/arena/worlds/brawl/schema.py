"""The canonical shape of an aggregated Brawl observation.

One row is one (brawler, stratum, sub-window) cell. Aggregates rather than raw
battles, because settlement never needs an individual battle and storing the
aggregate keeps the settlement path fast and auditable. The raw battles are
still kept by the collector -- this is the derived layer.

A stratum is (mode, map, trophy_bucket). That triple is the finest grain the
contracts can filter on and the grain the standardization weights are defined
over, so it is the grain the derived table is built at.

Two counts that look redundant are not:

    brawler_battles   appearances of *this* brawler in the stratum
    stratum_slots     total brawler appearances by *anyone* in the stratum

The first is the denominator for a win rate, the second for a use rate. Storing
both means neither has to be reconstructed by joining the table to itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = ["StratumKey", "AggregateRow", "FIELDS"]

FIELDS = (
    "observed_at",
    "window_start",
    "window_end",
    "brawler_id",
    "mode_id",
    "map_id",
    "trophy_bucket",
    "brawler_battles",
    "brawler_wins",
    "brawler_draws",
    "stratum_battles",
    "stratum_slots",
    "source_id",
)


@dataclass(frozen=True, slots=True, order=True)
class StratumKey:
    """The cell a measurement belongs to."""

    mode_id: str
    map_id: str
    trophy_bucket: str

    @property
    def key(self) -> str:
        return f"{self.mode_id}/{self.map_id}/{self.trophy_bucket}"

    def to_dict(self) -> dict[str, str]:
        return {
            "mode_id": self.mode_id,
            "map_id": self.map_id,
            "trophy_bucket": self.trophy_bucket,
        }


@dataclass(frozen=True, slots=True)
class AggregateRow:
    """One aggregated cell of the derived table.

    ``observed_at`` is when this aggregate became *knowable* -- the moment the
    underlying battles had all been collected and the cell could have been
    computed. It is not the same as ``window_end``, and conflating them is the
    single easiest way to leak the future into a replay: an agent standing at
    time t may see a row only if ``observed_at <= t``, regardless of which
    window the row describes.
    """

    observed_at: datetime
    window_start: datetime
    window_end: datetime
    brawler_id: str
    mode_id: str
    map_id: str
    trophy_bucket: str
    brawler_battles: int
    brawler_wins: int
    # Draws are stored separately rather than folded into wins or losses,
    # because the scoring convention is a decision the metric makes, not one
    # the collector should bake in. Scoring them as half a win is what keeps a
    # mode's pooled rate independent of its draw rate -- see
    # :mod:`arena.worlds.brawl.modes`.
    brawler_draws: int
    stratum_battles: int
    stratum_slots: int
    source_id: str

    def __post_init__(self) -> None:
        for label in ("observed_at", "window_start", "window_end"):
            moment = getattr(self, label)
            if moment.tzinfo is None:
                raise ValueError(f"{label} must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must precede window_end")
        if self.observed_at < self.window_end:
            raise ValueError(
                f"observed_at {self.observed_at.isoformat()} precedes window_end "
                f"{self.window_end.isoformat()}; an aggregate cannot be knowable "
                "before the battles it summarizes have happened"
            )
        if self.brawler_wins + self.brawler_draws > self.brawler_battles:
            raise ValueError(
                f"{self.brawler_id}: {self.brawler_wins} wins and {self.brawler_draws} "
                f"draws in only {self.brawler_battles} battles"
            )
        for label in (
            "brawler_battles",
            "brawler_wins",
            "brawler_draws",
            "stratum_battles",
            "stratum_slots",
        ):
            if getattr(self, label) < 0:
                raise ValueError(f"{label} cannot be negative")
        if self.brawler_battles > self.stratum_slots:
            raise ValueError(
                f"{self.brawler_id}: {self.brawler_battles} appearances exceeds the "
                f"{self.stratum_slots} total slots in its stratum"
            )

    @property
    def stratum(self) -> StratumKey:
        return StratumKey(self.mode_id, self.map_id, self.trophy_bucket)

    @classmethod
    def from_csv(cls, record: dict[str, str]) -> AggregateRow:
        return cls(
            observed_at=_parse_utc(record["observed_at"]),
            window_start=_parse_utc(record["window_start"]),
            window_end=_parse_utc(record["window_end"]),
            brawler_id=record["brawler_id"],
            mode_id=record["mode_id"],
            map_id=record["map_id"],
            trophy_bucket=record["trophy_bucket"],
            brawler_battles=int(record["brawler_battles"]),
            brawler_wins=int(record["brawler_wins"]),
            brawler_draws=int(record["brawler_draws"]),
            stratum_battles=int(record["stratum_battles"]),
            stratum_slots=int(record["stratum_slots"]),
            source_id=record["source_id"],
        )

    def to_csv(self) -> dict[str, Any]:
        return {
            "observed_at": _format_utc(self.observed_at),
            "window_start": _format_utc(self.window_start),
            "window_end": _format_utc(self.window_end),
            "brawler_id": self.brawler_id,
            "mode_id": self.mode_id,
            "map_id": self.map_id,
            "trophy_bucket": self.trophy_bucket,
            "brawler_battles": self.brawler_battles,
            "brawler_wins": self.brawler_wins,
            "brawler_draws": self.brawler_draws,
            "stratum_battles": self.stratum_battles,
            "stratum_slots": self.stratum_slots,
            "source_id": self.source_id,
        }


def _parse_utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
