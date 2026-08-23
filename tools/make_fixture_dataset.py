"""Generate the deterministic fixture dataset used by the test suite.

The fixture is synthetic and says so. It exists so that settlement can be
tested -- exhaustively, offline, with no API key and no waiting on a crawl --
and so that the shape of the canonical table is pinned by something executable
rather than described in a docstring.

It is built to exercise the parts of the metric that are easy to get wrong:

  * strata with wildly different sample sizes, so shrinkage has work to do;
  * a stratum that is thin in every window, so coverage thresholds can bite;
  * a stratum carrying no reference weight, so out-of-universe cells are
    provably ignored;
  * a mid-series balance patch, so there is a real information shock to
    replay later.

Regenerate with:

    python tools/make_fixture_dataset.py

Output is committed, so a change to this script that moves the data will show
up as a diff rather than as a silently different test baseline.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260823
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "brawl_aggregates.csv"
SOURCE_ID = "fixture-synthetic-v1"

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
    "stratum_battles",
    "stratum_slots",
    "source_id",
)

# Slots offered per battle. A 3v3 battle puts six brawlers on the field; a
# showdown puts ten. This is the use-rate denominator and getting it wrong
# rescales every use rate in the mode.
SLOTS_PER_BATTLE = {"gemGrab": 6, "brawlBall": 6, "showdown": 10}

MAPS = {
    "gemGrab": ("HardRockMine", "DoubleSwoosh", "UndermineGG"),
    "brawlBall": ("BackyardBowl", "PinholePunt", "SneakySneak"),
    "showdown": ("SkullCreek", "FeastOrFamine", "RockwallBrawl"),
}

TROPHY_BUCKETS = ("low", "mid", "high")

# Relative battle volume. Mid-trophy is where the population actually is, and
# the high bucket being thin is exactly why per-stratum shrinkage matters.
BUCKET_VOLUME = {"low": 5200, "mid": 9000, "high": 1400}

BRAWLERS = ("SPIKE", "CROW", "ELPRIMO", "PIPER")

# True win rate before any patch, by brawler and mode. Showdown numbers sit
# near 0.4 because a "win" there is a top-four finish out of ten.
BASE_WIN_RATE = {
    ("SPIKE", "gemGrab"): 0.558,
    ("SPIKE", "brawlBall"): 0.531,
    ("SPIKE", "showdown"): 0.421,
    ("CROW", "gemGrab"): 0.512,
    ("CROW", "brawlBall"): 0.487,
    ("CROW", "showdown"): 0.446,
    ("ELPRIMO", "gemGrab"): 0.494,
    ("ELPRIMO", "brawlBall"): 0.547,
    ("ELPRIMO", "showdown"): 0.388,
    ("PIPER", "gemGrab"): 0.523,
    ("PIPER", "brawlBall"): 0.502,
    ("PIPER", "showdown"): 0.412,
}

# Per-map deviation, so standardization weights actually change the answer.
MAP_EFFECT = {
    "HardRockMine": 0.011,
    "DoubleSwoosh": -0.008,
    "UndermineGG": 0.003,
    "BackyardBowl": -0.006,
    "PinholePunt": 0.014,
    "SneakySneak": -0.009,
    "SkullCreek": 0.007,
    "FeastOrFamine": -0.011,
    "RockwallBrawl": 0.004,
}

# Skill gradient. A brawler with a high skill ceiling performs better in the
# high bucket, which is precisely the confound standardization removes when the
# crawl's trophy composition drifts.
BUCKET_EFFECT = {
    "SPIKE": {"low": -0.018, "mid": 0.0, "high": 0.021},
    "CROW": {"low": -0.012, "mid": 0.0, "high": 0.015},
    "ELPRIMO": {"low": 0.014, "mid": 0.0, "high": -0.017},
    "PIPER": {"low": -0.005, "mid": 0.0, "high": 0.008},
}

USE_RATE = {
    "SPIKE": 0.061,
    "CROW": 0.048,
    "ELPRIMO": 0.055,
    "PIPER": 0.043,
}

# The series splits into two halves that serve different purposes.
#
#   weeks 0-7    ESTIMATION history. Exists so a reference snapshot can be
#                derived from data strictly before any contract window opens.
#                Without it, every snapshot would be lookahead by construction.
#   weeks 8-15   TRADING period. Contracts are written and settled here.
#
# The trading period starts 2026-08-03, so a snapshot dated 2026-08-03 sees
# exactly the estimation history and nothing else.
ESTIMATION_WEEKS = 8
TRADING_WEEKS = 8
WEEKS = ESTIMATION_WEEKS + TRADING_WEEKS

TRADING_START = datetime(2026, 8, 3, tzinfo=timezone.utc)
FIRST_WINDOW_START = TRADING_START - timedelta(weeks=ESTIMATION_WEEKS)
COLLECTION_LAG = timedelta(days=2)

# The information shock. From this window onward Spike is nerfed in team modes.
# Placed four weeks into the trading period so half the tradeable series sits
# either side of it, which is what makes the fixture usable for an event study
# rather than only for settlement tests.
PATCH_WEEK = ESTIMATION_WEEKS + 4
PATCH_EFFECT = {"gemGrab": -0.032, "brawlBall": -0.024, "showdown": -0.006}

# A stratum kept deliberately starved so coverage and per-stratum thresholds
# have something real to exclude.
STARVED = ("showdown", "RockwallBrawl", "high")

# A map that enters rotation only once the trading period starts, so it does not
# exist during the estimation window and therefore carries no weight in any
# snapshot frozen beforehand. This is the realistic version of "out of
# universe": not a synthetic exclusion, but a genuine content addition arriving
# after the settlement rule was fixed.
#
# It exercises two things at once -- that a heavily-played unweighted stratum
# cannot move the adjusted metric, and that a stratum with no prior of its own
# falls back to its mode's prior rather than failing.
NEW_MAP = ("gemGrab", "NewRotationMap", "mid")
NEW_MAP_ARRIVES_WEEK = ESTIMATION_WEEKS


def true_win_rate(brawler: str, mode: str, map_id: str, bucket: str, week: int) -> float:
    rate = BASE_WIN_RATE[(brawler, mode)]
    rate += MAP_EFFECT.get(map_id, 0.0)
    rate += BUCKET_EFFECT[brawler][bucket]
    if brawler == "SPIKE" and week >= PATCH_WEEK:
        rate += PATCH_EFFECT[mode]
    return min(max(rate, 0.01), 0.99)


def main() -> None:
    rng = random.Random(SEED)
    rows: list[dict[str, object]] = []

    strata = [
        (mode, map_id, bucket)
        for mode in sorted(MAPS)
        for map_id in MAPS[mode]
        for bucket in TROPHY_BUCKETS
    ]
    strata.append(NEW_MAP)

    for week in range(WEEKS):
        window_start = FIRST_WINDOW_START + timedelta(weeks=week)
        window_end = window_start + timedelta(weeks=1)
        observed_at = window_end + COLLECTION_LAG

        for mode, map_id, bucket in strata:
            slots_per_battle = SLOTS_PER_BATTLE[mode]

            if (mode, map_id, bucket) == NEW_MAP:
                if week < NEW_MAP_ARRIVES_WEEK:
                    continue
                # Large on purpose: if the metric ever weighted a stratum that
                # postdates the snapshot, the effect would be impossible to miss.
                stratum_battles = rng.randint(14000, 18000)
            elif (mode, map_id, bucket) == STARVED:
                stratum_battles = rng.randint(30, 70)
            else:
                base = BUCKET_VOLUME[bucket]
                stratum_battles = int(base * rng.uniform(0.82, 1.18))

            stratum_slots = stratum_battles * slots_per_battle

            for brawler in BRAWLERS:
                appearances = int(stratum_slots * USE_RATE[brawler] * rng.uniform(0.88, 1.12))
                if appearances == 0:
                    continue
                rate = true_win_rate(brawler, mode, map_id, bucket, week)
                wins = rng.binomialvariate(appearances, rate)
                rows.append(
                    {
                        "observed_at": _fmt(observed_at),
                        "window_start": _fmt(window_start),
                        "window_end": _fmt(window_end),
                        "brawler_id": brawler,
                        "mode_id": mode,
                        "map_id": map_id,
                        "trophy_bucket": bucket,
                        "brawler_battles": appearances,
                        "brawler_wins": wins,
                        "stratum_battles": stratum_battles,
                        "stratum_slots": stratum_slots,
                        "source_id": SOURCE_ID,
                    }
                )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # newline="" plus an explicit \n terminator keeps the committed file
    # byte-identical on Windows and Linux, which matters because the dataset
    # digest is computed over these exact bytes.
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows to {OUTPUT}")


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    main()
