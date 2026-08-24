"""Settle a small book of contracts across the fixture's balance patch.

Run:  python examples/settle_demo.py

Shows the three instrument families settling through one mechanism, the metric
diagnostics that make a settlement auditable, and the patch moving the
underlying between two adjacent windows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from arena.contracts.payoff import Binary, Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Difference, Single
from arena.settlement.engine import settle
from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.metrics import metric_ref
from arena.worlds.brawl.oracle import BrawlOracle
from arena.worlds.brawl.reference import load_reference

UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]

REFERENCE_ID = "ref-2026S09-v1"
POLICY = DataPolicy(min_sample_size=1_000, min_stratum_battles=200, min_strata_coverage=0.80)

SPIKE = Single(metric_ref("adjusted_win_rate", "SPIKE"))
CROW = Single(metric_ref("adjusted_win_rate", "CROW"))
# Performance relative to each stratum's mechanically-pinned baseline. Zero means
# "exactly average wherever it was played", under any weighting and any rotation.
#
# Built through metric_ref rather than by hand because its range is [-1, 1], not
# the [0, 1] a rate defaults to -- and declaring that wrongly would size a short
# position's collateral against the wrong worst case.
LIFT = Single(metric_ref("adjusted_win_rate_lift", "SPIKE"))


def spec(contract_id, underlying, payoff, window) -> ContractSpec:
    return ContractSpec(
        contract_id=contract_id,
        underlying=underlying,
        payoff=payoff,
        window=window,
        policy=POLICY,
        reference_id=REFERENCE_ID,
        published_at=window.start - timedelta(days=1),
        tick_size="0.25",
    )


def main() -> None:
    dataset = CanonicalDataset.from_csv(REPO / "data" / "fixtures" / "brawl_aggregates.csv")
    reference = load_reference(REPO / "data" / "reference" / f"{REFERENCE_ID}.json")
    oracle = BrawlOracle(dataset, reference, POLICY)

    estimation = dict(reference.estimation)
    print(f"dataset   {len(dataset)} rows, digest {dataset.source_digest[7:23]}")
    print(
        f"reference {reference.reference_id}, {len(reference.weights)} strata, "
        f"kappa {reference.prior_strength:.1f} (estimated, {estimation.get('method')})"
    )
    print(
        f"          as_of {reference.as_of.date()}, "
        f"{estimation.get('lookback_days', 0):.0f}d lookback, "
        f"weights by {estimation.get('weight_basis')}"
    )
    print("          mode priors: " + ", ".join(
        f"{mode}={prior:.4f}" for mode, prior in sorted(reference.mode_priors)
    ))

    windows = {
        "pre-patch ": ObservationWindow(
            datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC)
        ),
        "post-patch": ObservationWindow(
            datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 28, tzinfo=UTC)
        ),
    }

    print()
    for label, window in windows.items():
        future = settle(spec("SPIKE_WR_FUT", SPIKE, Linear(scale=10_000.0), window), oracle)
        binary = settle(spec("SPIKE_WR_GT_54", SPIKE, Binary(">", 0.54), window), oracle)
        spread = settle(
            spec("SPIKE_CROW_SPREAD", Difference(SPIKE, CROW), Linear(scale=10_000.0), window),
            oracle,
        )
        diagnostics = dict(future.resolutions[0].diagnostics)

        lift = settle(
            spec("SPIKE_WR_LIFT", LIFT, Linear(scale=10_000.0), window), oracle
        )
        print(
            f"{label}  future {future.settlement_value:>9}"
            f"   binary(>0.54) {binary.settlement_value:>4}"
            f"   spread {spread.settlement_value:>9}"
            f"   lift {lift.settlement_value:>8}"
        )
        print(
            f"            level {future.underlying_level:.6f}"
            f"   n {future.resolutions[0].sample_size:>9,}"
            f"   coverage {diagnostics['coverage']:.3f}"
            f"   strata {diagnostics['strata_evidenced']} evidenced"
            f" / {diagnostics['strata_imputed']} imputed"
        )

    window = windows["pre-patch "]
    once = settle(spec("SPIKE_WR_FUT", SPIKE, Linear(scale=10_000.0), window), oracle)
    twice = settle(spec("SPIKE_WR_FUT", SPIKE, Linear(scale=10_000.0), window), oracle)
    print(f"\ndeterministic: {once.result_digest == twice.result_digest}")
    print(f"result digest: {once.result_digest}")
    print(f"spec digest:   {once.spec_digest}")


if __name__ == "__main__":
    main()
