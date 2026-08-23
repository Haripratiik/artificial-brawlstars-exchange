"""Estimate a reference snapshot from data and write it to disk.

    python tools/build_reference.py --as-of 2026-08-03 --lookback-days 56 \
        --reference-id ref-2026S09-v1

Everything in the snapshot -- standardization weights, hierarchical priors, and
the shrinkage strength -- is derived from rows that were knowable at ``--as-of``.
Nothing is typed in by hand.

The output file is refused if it already exists. Snapshots are immutable: a
contract pins one by id and settles against it long afterwards, so a new
estimate gets a new id, never an edit to an old one.

``--validate`` additionally runs the out-of-sample sweep, fitting on the first
half of the estimation window and scoring predictions on the second. Method of
moments assumes cell rates really are beta-distributed around their stratum
prior; the sweep tests that where it matters. If the closed-form kappa lands
near the minimum of the curve, the assumption is doing its job.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.estimation import (
    build_snapshot,
    estimate_priors,
    sweep_prior_strength,
)
from arena.worlds.brawl.reference import save_reference

REPO = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=REPO / "data" / "fixtures" / "brawl_aggregates.csv"
    )
    parser.add_argument("--reference-id", required=True)
    parser.add_argument(
        "--as-of",
        required=True,
        help="snapshot date, YYYY-MM-DD. Must be on or before any window it settles",
    )
    parser.add_argument("--lookback-days", type=int, default=56)
    parser.add_argument("--min-cell-battles", type=int, default=30)
    parser.add_argument("--prior-pooling", type=float, default=5_000.0)
    parser.add_argument(
        "--weight-basis",
        choices=("slots", "battles"),
        default="slots",
        help=(
            "what play volume means. 'slots' asks what a brawler would score at a "
            "random appearance; 'battles' at a random battle. Showdown offers ten "
            "slots per battle against a team mode's six, so this visibly moves the "
            "settlement value"
        ),
    )
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "reference")
    parser.add_argument("--validate", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    lookback = timedelta(days=args.lookback_days)

    dataset = CanonicalDataset.from_csv(args.dataset)
    snapshot, report = build_snapshot(
        dataset,
        reference_id=args.reference_id,
        as_of=as_of,
        lookback=lookback,
        min_cell_battles=args.min_cell_battles,
        prior_pooling=args.prior_pooling,
        weight_basis=args.weight_basis,
    )

    print(f"dataset        {args.dataset.name}  ({len(dataset)} rows)")
    print(f"estimated from {(as_of - lookback).date()} to {as_of.date()}")
    print(f"weight basis   {args.weight_basis}")
    print(f"rows used      {report.rows_used}")
    print(f"strata         {report.strata}")
    print(f"total slots    {report.total_slots:,}")
    print()
    print(f"kappa          {report.kappa:.1f}{'  (CLAMPED)' if report.kappa_clamped else ''}")
    print(f"  second moment      {report.second_moment:.6e}")
    print(f"  binomial component {report.binomial_component:.6e}")
    print(f"  between component  {report.between_component:.6e}")
    print(f"  cells used         {report.cells_used}")
    print()

    modes = dict(snapshot.mode_priors)
    print("mode priors (estimated, not asserted):")
    for mode, prior in sorted(modes.items()):
        print(f"  {mode:<14} {prior:.4f}")
    print()

    weights = sorted(snapshot.weights, key=lambda kv: -kv[1])
    print("heaviest strata:")
    for key, weight in weights[:5]:
        prior = dict(snapshot.stratum_priors).get(key, float("nan"))
        print(f"  {key:<38} w={weight:.4f}  prior={prior:.4f}")
    print()

    if args.validate:
        rows = [
            row
            for row in dataset
            if row.observed_at <= as_of and row.window_start >= as_of - lookback
        ]
        stratum_priors, mode_priors = estimate_priors(rows, args.prior_pooling)
        split = as_of - lookback / 2
        candidates = [10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1_000.0, 2_500.0, 5_000.0]
        curve = sweep_prior_strength(
            rows,
            stratum_priors,
            mode_priors,
            candidates,
            split_at=split,
            min_cell_battles=args.min_cell_battles,
        )
        if curve:
            best = min(curve, key=lambda pair: pair[1])
            print(f"out-of-sample validation (train < {split.date()}, test after):")
            for kappa, mse in curve:
                marker = "  <-- empirical optimum" if kappa == best[0] else ""
                print(f"  kappa {kappa:>8.0f}   weighted MSE {mse:.6e}{marker}")
            print(f"\n  method-of-moments kappa = {report.kappa:.1f}")
            print(f"  empirical optimum       = {best[0]:.0f}")
            print()
        else:
            print("out-of-sample validation: not enough overlapping cells to score\n")

    path = args.out_dir / f"{args.reference_id}.json"
    save_reference(snapshot, path)
    print(f"wrote {path}")
    print(f"digest {snapshot.snapshot_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
