from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arena.contracts.spec import DataPolicy, ObservationWindow
from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.oracle import BrawlOracle
from arena.worlds.brawl.reference import load_reference
from arena.worlds.brawl.schema import AggregateRow

REPO = Path(__file__).resolve().parents[1]
FIXTURE_CSV = REPO / "data" / "fixtures" / "brawl_aggregates.csv"
REFERENCE_JSON = REPO / "data" / "reference" / "ref-2026S09-v1.json"

UTC = timezone.utc


@pytest.fixture(scope="session")
def reference():
    return load_reference(REFERENCE_JSON)


@pytest.fixture(scope="session")
def dataset():
    return CanonicalDataset.from_csv(FIXTURE_CSV)


@pytest.fixture
def policy():
    return DataPolicy(
        min_sample_size=1000,
        min_stratum_battles=200,
        min_strata_coverage=0.80,
    )


@pytest.fixture
def oracle(dataset, reference, policy):
    return BrawlOracle(dataset, reference, policy)


@pytest.fixture
def window():
    """The four windows before the fixture's balance patch."""
    return ObservationWindow(
        start=datetime(2026, 8, 3, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
    )


@pytest.fixture
def published_at(window):
    return window.start - timedelta(days=1)


def make_row(
    *,
    brawler: str = "SPIKE",
    mode: str = "gemGrab",
    map_id: str = "HardRockMine",
    bucket: str = "mid",
    battles: int,
    wins: int,
    draws: int = 0,
    slots: int | None = None,
    week: int = 0,
    source_id: str = "test",
) -> AggregateRow:
    """Build one aggregate row with sane defaults, for hand-built scenarios."""
    start = datetime(2026, 8, 3, tzinfo=UTC) + timedelta(weeks=week)
    end = start + timedelta(weeks=1)
    return AggregateRow(
        observed_at=end + timedelta(days=2),
        window_start=start,
        window_end=end,
        brawler_id=brawler,
        mode_id=mode,
        map_id=map_id,
        trophy_bucket=bucket,
        brawler_battles=battles,
        brawler_wins=wins,
        brawler_draws=draws,
        stratum_battles=max(battles, 1),
        stratum_slots=slots if slots is not None else max(battles * 20, 1),
        source_id=source_id,
    )
