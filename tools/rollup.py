"""Roll raw battle shards up into canonical aggregates, then prune the raw.

    python tools/rollup.py --data-dir data/raw --out data/derived
    python tools/rollup.py --data-dir data/raw --out data/derived --prune-after-days 7

This is what keeps the corpus affordable on a laptop. Measured on realistic
battle records, a raw battle costs ~227 bytes gzipped and its normalized
contribution ~35 -- so rolling up and pruning is roughly a 6x reduction:

    500k battles/day, raw only        3.4 GB/month     41 GB after a year
    500k battles/day, rolled + pruned 0.5 GB/month      6 GB after a year
                                      + a fixed ~0.8 GB raw buffer

The aggregates are the artifact settlement actually reads, and they are tiny --
a few thousand rows per window regardless of how many battles produced them.

**Pruning is opt-in and never deletes a shard it has not just rolled up.**
Raw battles are evidence, and a settlement six months from now may need to name
the bytes it came from. Keeping a buffer of recent days means a normalization
bug is recoverable; pruning beyond that is a deliberate trade of auditability
for disk, which is the user's call rather than a default.
"""

from __future__ import annotations

import argparse
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena.worlds.brawl.modes import MECHANICS, mechanical_baseline
from arena.worlds.brawl.schema import FIELDS

from collectors.brawl_api.normalize import aggregate, read_shard

SHARD = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data/derived"))
    parser.add_argument(
        "--prune-after-days",
        type=int,
        default=0,
        help=(
            "delete raw shards older than N days, but only ones this run rolled "
            "up successfully. 0 (default) never deletes anything"
        ),
    )
    parser.add_argument("--collection-lag-hours", type=float, default=6.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shard_dir = args.data_dir / "battlelogs"
    if not shard_dir.is_dir():
        print(f"no shards at {shard_dir}; has the collector run?")
        return 1

    shards = sorted(p for p in shard_dir.iterdir() if SHARD.match(p.name))
    if not shards:
        print(f"no shards at {shard_dir}")
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    rolled: list[Path] = []
    all_rows = []

    for shard in shards:
        day = datetime.strptime(SHARD.match(shard.name).group(1), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        window_start, window_end = day, day + timedelta(days=1)
        rows = aggregate(
            read_shard(shard),
            window_start=window_start,
            window_end=window_end,
            # A shard is knowable once its day has closed and collection has
            # caught up. Claiming it was knowable at window_end would be a
            # lookahead of exactly the collection lag.
            observed_at=window_end + timedelta(hours=args.collection_lag_hours),
            source_id=f"crawl:{shard.name}",
        )
        if not rows:
            print(f"{shard.name:<24} no usable battles")
            continue

        out_path = args.out / f"aggregates-{day:%Y-%m-%d}.csv"
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(row.to_csv() for row in rows)

        battles = sum(row.brawler_battles for row in rows)
        print(
            f"{shard.name:<24} {len(rows):>6} rows  "
            f"{battles:>9,} scored appearances  -> {out_path.name}"
        )
        rolled.append(shard)
        all_rows.extend(rows)

    if all_rows:
        _report_mechanical_check(all_rows)

    if args.prune_after_days > 0:
        _prune(rolled, args.prune_after_days)

    return 0


def _report_mechanical_check(rows) -> None:
    """Compare observed pooled rates against what each mode's rules force.

    The sharpest correctness check available, and it costs nothing. For team
    modes we observe every slot, so the pooled rate over all brawlers must be
    0.500; a material gap means participants are being dropped, battles double
    counted, or the log owner's team perspective inverted.

    Showdown is excluded from the verdict on purpose: the API reports a rank for
    the log owner only, so we score one slot in ten and that one belongs to a
    player the crawler chose to visit. Its pooled rate reflects crawled-player
    skill, not the game's baseline.
    """
    scored: dict[str, list[float]] = {}
    for row in rows:
        totals = scored.setdefault(row.mode_id, [0.0, 0.0])
        totals[0] += row.brawler_wins + 0.5 * row.brawler_draws
        totals[1] += row.brawler_battles

    print("\nmechanical baseline check:")
    for mode, (points, battles) in sorted(scored.items()):
        if battles <= 0:
            continue
        observed = points / battles
        expected = mechanical_baseline(mode)
        mechanics = MECHANICS.get(mode)
        fully_observed = mechanics is not None and mechanics.slots == 6

        if expected is None:
            print(f"  {mode:<16} {observed:.4f}   (mode not characterized)")
        elif not fully_observed:
            print(
                f"  {mode:<16} {observed:.4f}   vs {expected:.4f} -- not comparable, "
                "only the log owner's slot is scoreable"
            )
        else:
            gap = observed - expected
            flag = "   <-- CHECK PIPELINE" if abs(gap) > 0.01 else "   ok"
            print(f"  {mode:<16} {observed:.4f}   vs {expected:.4f}  gap {gap:+.4f}{flag}")


def _prune(rolled: list[Path], keep_days: int) -> None:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=keep_days)
    removed = 0
    freed = 0
    for shard in rolled:
        day = datetime.strptime(SHARD.match(shard.name).group(1), "%Y-%m-%d").date()
        if day >= cutoff:
            continue
        freed += shard.stat().st_size
        shard.unlink()
        removed += 1
    if removed:
        print(f"\npruned {removed} rolled-up shards older than {keep_days}d, freed {freed/1e6:.1f} MB")
    else:
        print(f"\nnothing to prune (keeping {keep_days}d of raw)")


if __name__ == "__main__":
    raise SystemExit(main())
