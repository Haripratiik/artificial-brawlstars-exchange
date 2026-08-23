"""Tests for deriving a reference snapshot from data.

The point of this module is that nothing in a settlement rule is typed in by
hand. These tests check that the derivation is correct, that it recovers known
quantities from data where the answer is knowable, and above all that it cannot
see the future.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arena.worlds.brawl.estimation import (
    KAPPA_MAX,
    build_snapshot,
    estimate_priors,
    estimate_prior_strength,
    estimate_weights,
    sweep_prior_strength,
)
from arena.worlds.brawl.schema import StratumKey
from tests.conftest import make_row

UTC = timezone.utc
AS_OF = datetime(2026, 8, 3, tzinfo=UTC)
LOOKBACK = timedelta(days=56)


# --------------------------------------------------------------------------
# No lookahead. The reason this module exists.
# --------------------------------------------------------------------------


def test_snapshot_only_sees_rows_knowable_at_as_of(dataset):
    """A snapshot fitted on its own settlement window would encode the outcome."""
    snapshot, _report = build_snapshot(
        dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK
    )
    # The fixture's trading period starts exactly at AS_OF and introduces a map
    # that did not exist before. If the estimator had peeked past AS_OF, that
    # map would have picked up a weight.
    assert snapshot.weight_for(StratumKey("gemGrab", "NewRotationMap", "mid")) == 0.0


def test_uncollected_rows_are_invisible_even_if_their_window_closed(dataset):
    """Filtering is on observed_at, not on window bounds.

    A window that closed before the snapshot date but was still being collected
    on that date must not contribute -- the data did not exist yet.
    """
    lagged = [row for row in dataset if row.window_end <= AS_OF < row.observed_at]
    assert lagged, "fixture should contain closed-but-uncollected windows at this date"

    snapshot, report = build_snapshot(
        dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK
    )
    visible = [
        row
        for row in dataset
        if row.observed_at <= AS_OF
        and row.window_end <= AS_OF
        and row.window_start >= AS_OF - LOOKBACK
    ]
    assert report.rows_used == len(visible)
    assert snapshot.as_of == AS_OF


def test_empty_estimation_window_raises(dataset):
    with pytest.raises(ValueError, match="cannot estimate"):
        build_snapshot(
            dataset,
            reference_id="t",
            as_of=datetime(2020, 1, 1, tzinfo=UTC),
            lookback=LOOKBACK,
        )


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------


def test_weights_are_shares_of_observed_play_volume():
    rows = [
        make_row(map_id="HardRockMine", bucket="mid", battles=10, slots=6_000, wins=5),
        make_row(map_id="DoubleSwoosh", bucket="mid", battles=10, slots=2_000, wins=5),
    ]
    weights = estimate_weights(rows)
    assert weights["gemGrab/HardRockMine/mid"] == pytest.approx(0.75)
    assert weights["gemGrab/DoubleSwoosh/mid"] == pytest.approx(0.25)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_stratum_slots_are_not_double_counted_across_brawlers():
    """``stratum_slots`` describes the stratum, and repeats on every brawler row.

    Summing it naively would scale each stratum's weight by how many brawlers
    the crawl happened to observe there -- making the weights a function of
    coverage, which is the exact thing standardization exists to remove.
    """
    shared_slots = 6_000
    one_brawler = [make_row(brawler="SPIKE", battles=10, slots=shared_slots, wins=5)]
    four_brawlers = [
        make_row(brawler=name, battles=10, slots=shared_slots, wins=5)
        for name in ("SPIKE", "CROW", "PIPER", "ELPRIMO")
    ]
    assert estimate_weights(one_brawler) == estimate_weights(four_brawlers)


def test_weights_need_slots():
    with pytest.raises(ValueError, match="no slots observed"):
        estimate_weights([make_row(battles=0, wins=0, slots=0)])


# --------------------------------------------------------------------------
# Priors
# --------------------------------------------------------------------------


def test_mode_prior_recovers_the_modes_own_baseline(dataset):
    """Nobody tells the estimator that Showdown scores 'top four of ten'.

    The fixture generates Showdown around 0.4 and team modes around 0.5. The
    estimator has to find that from the data, which is the whole reason priors
    are estimated rather than asserted.
    """
    rows = [row for row in dataset if row.observed_at <= AS_OF]
    _stratum_priors, mode_priors = estimate_priors(rows)

    assert mode_priors["showdown"] == pytest.approx(0.41, abs=0.03)
    assert mode_priors["gemGrab"] == pytest.approx(0.52, abs=0.03)
    assert mode_priors["showdown"] < mode_priors["gemGrab"] - 0.05


def test_stratum_priors_are_pooled_toward_their_mode(dataset):
    """A stratum borrows strength from its mode instead of inventing a baseline."""
    rows = [row for row in dataset if row.observed_at <= AS_OF]
    # Pooling strength is in battles, and the fixture's strata hold hundreds of
    # thousands, so overwhelming them takes a genuinely large number.
    stratum_priors, mode_priors = estimate_priors(rows, pooling_strength=10**12)

    # With overwhelming pooling every stratum collapses onto its mode.
    for key, prior in stratum_priors.items():
        assert prior == pytest.approx(mode_priors[key.split("/", 1)[0]], abs=1e-6)


def test_stratum_priors_track_their_own_data_when_pooling_is_off(dataset):
    rows = [row for row in dataset if row.observed_at <= AS_OF]
    pooled, _ = estimate_priors(rows, pooling_strength=5_000.0)
    unpooled, _ = estimate_priors(rows, pooling_strength=0.0)
    # Pooling must actually move the estimates, or it is not doing anything.
    assert any(
        abs(pooled[key] - unpooled[key]) > 1e-6 for key in pooled if key in unpooled
    )


# --------------------------------------------------------------------------
# Prior strength (kappa)
# --------------------------------------------------------------------------


def test_kappa_diverges_when_all_spread_is_binomial_noise():
    """If cells differ only by sampling noise, trust none of the variation.

    Every cell here has exactly its prior's rate, so the observed second moment
    is below what binomial noise alone predicts and there is no between-cell
    variance to find. The estimator must shrink maximally rather than report a
    small kappa.
    """
    priors = {"gemGrab/HardRockMine/mid": 0.5}
    rows = [
        make_row(brawler=name, battles=1_000, wins=500)
        for name in ("A", "B", "C", "D", "E")
    ]
    kappa, diagnostics = estimate_prior_strength(rows, priors, {"gemGrab": 0.5})
    assert kappa == KAPPA_MAX
    assert diagnostics["between_component"] == 0.0


def test_kappa_is_finite_when_cells_genuinely_differ():
    """Real between-cell variation should produce a usable, finite kappa."""
    priors = {"gemGrab/HardRockMine/mid": 0.5}
    spread = (0.35, 0.42, 0.50, 0.58, 0.65)
    rows = [
        make_row(brawler=f"B{i}", battles=20_000, wins=round(20_000 * rate))
        for i, rate in enumerate(spread)
    ]
    kappa, diagnostics = estimate_prior_strength(rows, priors, {"gemGrab": 0.5})

    assert 1.0 < kappa < KAPPA_MAX
    assert diagnostics["between_component"] > 0.0
    # Wider true spread means less shrinkage.
    tighter = [
        make_row(brawler=f"B{i}", battles=20_000, wins=round(20_000 * rate))
        for i, rate in enumerate((0.48, 0.49, 0.50, 0.51, 0.52))
    ]
    tighter_kappa, _ = estimate_prior_strength(tighter, priors, {"gemGrab": 0.5})
    assert tighter_kappa > kappa


def test_kappa_needs_at_least_two_cells():
    priors = {"gemGrab/HardRockMine/mid": 0.5}
    kappa, _ = estimate_prior_strength(
        [make_row(battles=10_000, wins=6_000)], priors, {"gemGrab": 0.5}
    )
    assert kappa == KAPPA_MAX


def test_thin_cells_are_excluded_from_the_kappa_fit():
    """Method of moments is unweighted, so tiny cells would let noise dominate."""
    priors = {"gemGrab/HardRockMine/mid": 0.5}
    rows = [
        make_row(brawler=f"B{i}", battles=20_000, wins=round(20_000 * rate))
        for i, rate in enumerate((0.40, 0.45, 0.55, 0.60))
    ] + [make_row(brawler=f"T{i}", battles=3, wins=3) for i in range(50)]

    with_thin, _ = estimate_prior_strength(rows, priors, {"gemGrab": 0.5}, min_cell_battles=1)
    without_thin, diagnostics = estimate_prior_strength(
        rows, priors, {"gemGrab": 0.5}, min_cell_battles=100
    )
    assert diagnostics["cells_used"] == 4
    assert with_thin != without_thin


def test_out_of_sample_sweep_agrees_with_the_closed_form(dataset):
    """Method of moments assumes a beta prior. This checks the assumption holds.

    If the closed form's kappa sat far from the predictive optimum, the beta
    model would be misspecified and the number should not be trusted. They
    should land within a factor of a few on well-behaved data.
    """
    rows = [
        row
        for row in dataset
        if row.observed_at <= AS_OF and row.window_start >= AS_OF - LOOKBACK
    ]
    stratum_priors, mode_priors = estimate_priors(rows)
    kappa, _ = estimate_prior_strength(rows, stratum_priors, mode_priors)

    curve = sweep_prior_strength(
        rows,
        stratum_priors,
        mode_priors,
        candidates=[10.0, 50.0, 250.0, 1_000.0, 5_000.0],
        split_at=AS_OF - LOOKBACK / 2,
    )
    assert curve, "sweep produced no scored candidates"
    best_kappa = min(curve, key=lambda pair: pair[1])[0]

    assert 0.2 < kappa / best_kappa < 5.0, (
        f"method-of-moments kappa {kappa:.0f} is far from the out-of-sample "
        f"optimum {best_kappa:.0f}; the beta prior may be misspecified"
    )


# --------------------------------------------------------------------------
# The snapshot as a whole
# --------------------------------------------------------------------------


def test_snapshot_records_how_it_was_derived(dataset):
    """Provenance is what separates an estimate from an assertion."""
    snapshot, _ = build_snapshot(dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK)
    estimation = dict(snapshot.estimation)

    assert estimation["method"] == "moments/beta-binomial"
    assert estimation["lookback_days"] == 56.0
    assert estimation["rows_used"] > 0
    assert snapshot.source_digest is not None


def test_estimation_provenance_is_covered_by_the_digest(dataset):
    """Two snapshots fitted differently are different snapshots."""
    a, _ = build_snapshot(dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK)
    b, _ = build_snapshot(
        dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK, prior_pooling=1.0
    )
    assert a.snapshot_digest != b.snapshot_digest


def test_estimation_is_deterministic(dataset):
    a, _ = build_snapshot(dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK)
    b, _ = build_snapshot(dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK)
    assert a.snapshot_digest == b.snapshot_digest


def test_saving_over_an_existing_snapshot_is_refused(dataset, tmp_path):
    """Contracts pin snapshots by id and settle against them years later."""
    from arena.worlds.brawl.reference import load_reference, save_reference

    snapshot, _ = build_snapshot(dataset, reference_id="t", as_of=AS_OF, lookback=LOOKBACK)
    path = tmp_path / "t.json"
    save_reference(snapshot, path)

    with pytest.raises(FileExistsError, match="immutable"):
        save_reference(snapshot, path)

    # A round-trip preserves everything the digest covers.
    reloaded = load_reference(path)
    assert reloaded.to_dict() == snapshot.to_dict()
    assert reloaded.snapshot_digest == snapshot.snapshot_digest
    # The digest of the file on disk is a separate fact, recorded but not part
    # of the snapshot's content.
    assert reloaded.file_digest is not None
    assert reloaded.file_digest != reloaded.source_digest
