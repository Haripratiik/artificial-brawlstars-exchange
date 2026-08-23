"""Tests for the canonical metric definitions.

The centrepiece is ``test_standardization_cancels_composition_drift``. It is
the test that justifies the project's data strategy: the collector cannot draw
a representative sample, so the metric must be insensitive to *how* the sample
was composed. If that test ever fails, contracts are settling on the crawler's
reach rather than on the game, and every downstream result is worthless.
"""

from __future__ import annotations

import pytest

from arena.worlds.brawl.metrics import (
    InsufficientEvidence,
    MissingStrata,
    adjusted_win_rate,
    raw_win_rate,
    use_rate,
)
from arena.worlds.brawl.schema import StratumKey
from tests.conftest import make_row

# The stratum conftest.make_row defaults to.
DEFAULT_STRATUM = StratumKey("gemGrab", "HardRockMine", "mid")

# Isolating one stratum requires suppressing imputation, since by default every
# stratum in the snapshot participates.
ONLY_OBSERVED = {"missing_strata": MissingStrata.DROP_AND_RENORMALIZE}


def test_standardization_cancels_composition_drift(reference):
    """Same per-stratum truth, wildly different mix -> same adjusted rate.

    Two crawls observe the identical underlying game. The first happens to
    reach mostly low-trophy players, the second mostly high-trophy ones. Spike
    genuinely performs better at high trophies, so the *raw* rates must differ.
    The adjusted rates must not, because both standardize onto the same pinned
    weights.
    """
    truth = {
        ("gemGrab", "HardRockMine", "low"): 0.52,
        ("gemGrab", "HardRockMine", "mid"): 0.55,
        ("gemGrab", "HardRockMine", "high"): 0.60,
    }
    low_heavy = {"low": 40_000, "mid": 6_000, "high": 1_000}
    high_heavy = {"low": 1_000, "mid": 6_000, "high": 40_000}

    def build(volumes):
        return [
            make_row(
                mode=mode,
                map_id=map_id,
                bucket=bucket,
                battles=volumes[bucket],
                # Exact expected wins: this test is about weighting, so
                # sampling noise is removed deliberately.
                wins=round(volumes[bucket] * rate),
            )
            for (mode, map_id, bucket), rate in truth.items()
        ]

    low_rows, high_rows = build(low_heavy), build(high_heavy)

    raw_low = raw_win_rate(low_rows, reference).value
    raw_high = raw_win_rate(high_rows, reference).value
    adj_low = adjusted_win_rate(low_rows, reference, **ONLY_OBSERVED).value
    adj_high = adjusted_win_rate(high_rows, reference, **ONLY_OBSERVED).value

    assert abs(raw_low - raw_high) > 0.05, (
        f"fixture is not exercising composition drift: raw {raw_low} vs {raw_high}"
    )
    # Standardization removes almost all of it. The residual is shrinkage: the
    # thin bucket is pulled harder toward its prior, and it is a *different*
    # bucket in each crawl, so exact equality is not expected.
    assert abs(adj_low - adj_high) < 0.02
    assert abs(adj_low - adj_high) < abs(raw_low - raw_high) / 5


# --------------------------------------------------------------------------
# Strata outside the snapshot
# --------------------------------------------------------------------------


def test_map_added_after_the_snapshot_cannot_move_the_metric(reference, dataset, window):
    """A map that entered rotation after the snapshot was frozen carries no weight.

    The fixture adds ``NewRotationMap`` only once the trading period begins, so
    no snapshot estimated beforehand knows about it. It is heavily played on
    purpose: if the metric ever weighted a stratum that postdates its own
    settlement rule, the effect would be impossible to miss.
    """
    rows = [
        row
        for row in dataset
        if row.brawler_id == "SPIKE"
        and window.start <= row.window_start
        and row.window_end <= window.end
    ]
    without_new_map = [row for row in rows if row.map_id != "NewRotationMap"]

    assert len(rows) > len(without_new_map), "fixture should contain the new map"
    assert reference.weight_for(StratumKey("gemGrab", "NewRotationMap", "mid")) == 0.0
    assert (
        adjusted_win_rate(rows, reference).value
        == adjusted_win_rate(without_new_map, reference).value
    )


def test_stratum_without_its_own_prior_falls_back_to_its_mode(reference):
    """A brand-new map has no stratum prior, but its mode does."""
    new_map = StratumKey("gemGrab", "NewRotationMap", "mid")
    assert new_map.key not in dict(reference.stratum_priors)
    assert reference.prior_for(new_map) == dict(reference.mode_priors)["gemGrab"]


def test_unknown_mode_raises_rather_than_guessing(reference):
    with pytest.raises(KeyError, match="no prior"):
        reference.prior_for(StratumKey("chessMode", "SomeMap", "mid"))


# --------------------------------------------------------------------------
# Shrinkage
# --------------------------------------------------------------------------


def test_shrinkage_pulls_thin_strata_toward_the_prior(reference):
    """Nine battles and six wins is not evidence of a 67% win rate."""
    outcome = adjusted_win_rate([make_row(battles=9, wins=6)], reference, **ONLY_OBSERVED)
    prior = reference.prior_for(DEFAULT_STRATUM)
    kappa = reference.prior_strength

    assert outcome.value == pytest.approx((6 + kappa * prior) / (9 + kappa))
    assert abs(outcome.value - prior) < abs(outcome.value - 6 / 9)


def test_shrinkage_fades_as_evidence_accumulates(reference):
    """With plenty of battles the prior stops mattering."""
    rate = 0.58
    thick = [make_row(battles=500_000, wins=round(500_000 * rate))]
    outcome = adjusted_win_rate(thick, reference, **ONLY_OBSERVED)
    assert outcome.value == pytest.approx(rate, abs=1e-3)


def test_thin_strata_are_excluded_below_threshold(reference):
    """A stratum under min_stratum_battles contributes no evidence."""
    rows = [
        make_row(map_id="HardRockMine", bucket="mid", battles=5_000, wins=2_900),
        make_row(map_id="DoubleSwoosh", bucket="mid", battles=12, wins=12),
    ]
    kept = adjusted_win_rate(rows, reference, min_stratum_battles=200, **ONLY_OBSERVED)

    assert dict(kept.diagnostics)["strata_evidenced"] == 1
    kappa, prior = reference.prior_strength, reference.prior_for(DEFAULT_STRATUM)
    assert kept.value == pytest.approx((2_900 + kappa * prior) / (5_000 + kappa))


# --------------------------------------------------------------------------
# Missing strata
# --------------------------------------------------------------------------


def test_missing_strata_are_imputed_from_their_prior_by_default(reference):
    """Unobserved strata shrink fully to their prior instead of being dropped.

    Dropping and renormalizing assumes the missing strata resemble the observed
    ones. They do not -- strata go missing because they are unpopular, rare, or
    high-trophy. Partial pooling is the standard poststratification answer to
    empty cells, and full shrinkage to the prior is exactly that.
    """
    rows = [make_row(battles=100_000, wins=90_000)]  # an absurd 90% in one stratum

    imputed = adjusted_win_rate(rows, reference)
    dropped = adjusted_win_rate(rows, reference, **ONLY_OBSERVED)
    diagnostics = dict(imputed.diagnostics)

    # Dropping lets one stratum speak for the whole universe.
    assert dropped.value > 0.85
    # Imputing lets it speak only for its own weight; the rest is prior.
    assert imputed.value < 0.55
    assert diagnostics["strata_evidenced"] == 1
    assert diagnostics["strata_imputed"] == len(reference.weights) - 1


def test_coverage_measures_the_share_backed_by_evidence(reference):
    rows = [make_row(battles=100_000, wins=55_000)]
    coverage = dict(adjusted_win_rate(rows, reference).diagnostics)["coverage"]
    expected = reference.weight_for(DEFAULT_STRATUM) / reference.total_weight
    assert coverage == pytest.approx(expected)


def test_coverage_below_threshold_raises_under_either_policy(reference):
    """A metric that is mostly prior is not a measurement, however well-formed."""
    rows = [make_row(battles=100_000, wins=55_000)]
    adjusted_win_rate(rows, reference, min_coverage=0.0)
    for policy in ({}, ONLY_OBSERVED):
        with pytest.raises(InsufficientEvidence, match="coverage"):
            adjusted_win_rate(rows, reference, min_coverage=0.80, **policy)


def test_unknown_missing_strata_policy_is_rejected(reference):
    with pytest.raises(ValueError, match="missing_strata must be one of"):
        adjusted_win_rate([make_row(battles=10, wins=5)], reference, missing_strata="WING_IT")


# --------------------------------------------------------------------------
# Sample size accounting
# --------------------------------------------------------------------------


def test_sample_size_counts_only_evidence_that_was_actually_used(reference):
    """The evidential bar must not be clearable with excluded data.

    ``min_sample_size`` exists to guarantee a settlement rests on enough
    evidence. If the reported sample counted strata the metric threw away --
    a map outside the snapshot, or a cell too thin to trust -- a contract could
    clear the bar on data that never entered the number.
    """
    rows = [
        make_row(map_id="HardRockMine", bucket="mid", battles=5_000, wins=2_750),
        make_row(map_id="DoubleSwoosh", bucket="mid", battles=50, wins=25),
        make_row(map_id="NewRotationMap", bucket="mid", battles=900_000, wins=450_000),
    ]
    outcome = adjusted_win_rate(rows, reference, min_stratum_battles=200)
    diagnostics = dict(outcome.diagnostics)

    assert outcome.sample_size == 5_000
    assert diagnostics["battles_used"] == 5_000
    assert diagnostics["battles_observed"] == 905_050


def test_use_rate_sample_size_counts_only_used_slots(reference):
    rows = [
        make_row(map_id="HardRockMine", bucket="mid", battles=1_000, slots=6_000, wins=500),
        make_row(map_id="NewRotationMap", bucket="mid", battles=1_000, slots=600_000, wins=500),
    ]
    outcome = use_rate(rows, reference)
    assert outcome.sample_size == 6_000
    assert dict(outcome.diagnostics)["slots_observed"] == 606_000


# --------------------------------------------------------------------------
# Pooling and denominators
# --------------------------------------------------------------------------


def test_sub_windows_are_pooled_by_count_not_by_averaging_ratios(reference):
    """Every battle carries equal weight regardless of which week it fell in.

    Averaging the two weekly ratios would give 0.75; pooling the counts gives
    the correct 0.545. This is the classic trap and it is worth a named test.
    """
    rows = [
        make_row(battles=1_000, wins=500, week=0),
        make_row(battles=100, wins=100, week=1),
    ]
    outcome = raw_win_rate(rows, reference)
    assert outcome.value == pytest.approx(600 / 1100)
    assert outcome.sample_size == 1100


def test_use_rate_is_denominated_in_slots(reference):
    """A brawler in every battle of a 3v3 mode has use rate 1/6, not 1."""
    assert use_rate([make_row(battles=1_000, wins=500, slots=6_000)], reference).value == (
        pytest.approx(1 / 6)
    )


def test_use_rate_does_not_shrink_zeros(reference):
    """Nobody picking a brawler is a measurement, not noise."""
    assert use_rate([make_row(battles=0, wins=0, slots=60_000)], reference).value == 0.0


def test_empty_input_raises_rather_than_returning_zero(reference):
    with pytest.raises(InsufficientEvidence):
        adjusted_win_rate([], reference)
    with pytest.raises(InsufficientEvidence):
        raw_win_rate([], reference)


def test_metric_is_order_independent(reference, dataset, window):
    """Row order must not perturb the last bit of the result."""
    rows = [
        row
        for row in dataset
        if row.brawler_id == "SPIKE"
        and window.start <= row.window_start
        and row.window_end <= window.end
    ]
    assert (
        adjusted_win_rate(rows, reference).value
        == adjusted_win_rate(list(reversed(rows)), reference).value
    )
