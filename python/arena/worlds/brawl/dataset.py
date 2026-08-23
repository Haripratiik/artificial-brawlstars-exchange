"""The canonical dataset and its no-lookahead view.

Two responsibilities, kept in one place because they share an invariant.

First, loading: rows come from CSV with a recorded digest of the bytes, so a
settlement can name the exact file content it was computed from.

Second, and more important, *visibility*. ``CanonicalDataset.visible_at(t)``
returns the subset of rows an observer standing at time ``t`` could legitimately
have known, filtering on ``observed_at`` rather than on the window a row
describes. This is the single mechanism that prevents lookahead in historical
replay, and it lives at the dataset layer on purpose: an agent cannot forget to
apply it, because it never receives the full dataset in the first place.

The research harness is the deliberate exception. It evaluates the market
against outcomes the market could not have known, which is the entire point of
measuring forecast error, so it holds the unfiltered dataset. That asymmetry is
correct and is why the two access paths are named differently.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from arena.contracts.spec import ObservationWindow
from arena.contracts.underlying import ALL, MetricRef
from arena.determinism import digest_of
from arena.worlds.brawl.schema import FIELDS, AggregateRow

__all__ = ["CanonicalDataset"]


@dataclass(frozen=True, slots=True)
class CanonicalDataset:
    """An immutable table of aggregate rows with provenance."""

    rows: tuple[AggregateRow, ...]
    source_id: str
    source_digest: str

    @classmethod
    def from_csv(cls, path: str | Path, source_id: str | None = None) -> CanonicalDataset:
        location = Path(path)
        payload = location.read_bytes()
        text = payload.decode("utf-8")
        reader = csv.DictReader(text.splitlines())
        missing = set(FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"{location.name} is missing required columns: {sorted(missing)}"
            )
        rows = tuple(AggregateRow.from_csv(record) for record in reader)
        return cls(
            rows=rows,
            source_id=source_id or location.stem,
            source_digest=digest_of(payload),
        )

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[AggregateRow]:
        return iter(self.rows)

    def visible_at(self, moment: datetime) -> CanonicalDataset:
        """Rows knowable at ``moment``, for agent-facing access.

        Filters on ``observed_at``, never on window bounds. A row describing
        last week's battles that has not finished being collected yet is not
        visible, and a row describing a window that has not closed cannot exist.
        """
        if moment.tzinfo is None:
            raise ValueError("visibility cutoff must be timezone-aware")
        return CanonicalDataset(
            rows=tuple(row for row in self.rows if row.observed_at <= moment),
            source_id=f"{self.source_id}@{moment.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            source_digest=self.source_digest,
        )

    def select(self, ref: MetricRef, window: ObservationWindow) -> tuple[AggregateRow, ...]:
        """Rows matching a metric reference's subject and universe, within a window.

        A row is in-window when its own window sits entirely inside the
        contract's. Partial overlap is excluded rather than pro-rated: a row is
        an already-aggregated count, and splitting one would require assuming
        battles were uniformly distributed across its span.
        """
        return tuple(
            row
            for row in self.rows
            if row.brawler_id == ref.subject
            and window.start <= row.window_start
            and row.window_end <= window.end
            and _matches(row.mode_id, ref.modes)
            and _matches(row.map_id, ref.maps)
            and _matches(row.trophy_bucket, ref.trophy_buckets)
        )

    def subjects(self) -> tuple[str, ...]:
        return tuple(sorted({row.brawler_id for row in self.rows}))


def _matches(value: str, allowed: Sequence[str]) -> bool:
    return allowed[0] == ALL or value in allowed
