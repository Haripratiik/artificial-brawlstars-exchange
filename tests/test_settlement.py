"""End-to-end settlement tests.

Milestone 0's deliverable is one sentence: given a contract and a historical
dataset, settlement is deterministic. These tests are what that sentence means
operationally -- not just that the number is stable, but that it is provably
tied to the exact spec, dataset bytes, and reference snapshot that produced it,
and that it refuses to appear at all when the evidence is too thin.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from arena.contracts.payoff import Binary, Linear
from arena.contracts.spec import (
    ContractSpec,
    DataPolicy,
    DistributionSchedule,
    ObservationWindow,
)
from arena.contracts.underlying import Basket, Difference, MetricRef, Single
from arena.settlement.engine import (
    ReferenceLookahead,
    ReferenceMismatch,
    SettlementOutOfBounds,
    distributions,
    settle,
)
from arena.settlement.oracle import MetricResolution
from arena.settlement.result import SettlementStatus
from arena.worlds.brawl.oracle import BrawlOracle

SPIKE_WR = MetricRef(metric="adjusted_win_rate", subject="SPIKE")
CROW_WR = MetricRef(metric="adjusted_win_rate", subject="CROW")
ELPRIMO_WR = MetricRef(metric="adjusted_win_rate", subject="ELPRIMO")


def make_spec(window, published_at, **overrides):
    defaults = dict(
        contract_id="SPIKE_WR_FUT_2026W31",
        underlying=Single(SPIKE_WR),
        payoff=Linear(scale=10_000.0),
        window=window,
        policy=DataPolicy(
            min_sample_size=1_000,
            min_stratum_battles=200,
            min_strata_coverage=0.80,
        ),
        reference_id="ref-2026S09-v1",
        published_at=published_at,
        tick_size="0.25",
    )
    defaults.update(overrides)
    return ContractSpec(**defaults)


# --------------------------------------------------------------------------
# The core deliverable
# --------------------------------------------------------------------------


def test_settlement_is_deterministic(oracle, window, published_at):
    spec = make_spec(window, published_at)
    first = settle(spec, oracle)
    second = settle(spec, oracle)

    assert first.status == SettlementStatus.SETTLED
    assert first.result_digest == second.result_digest
    assert first.to_dict() == second.to_dict()


def test_settlement_lands_on_the_tick_grid(oracle, window, published_at):
    spec = make_spec(window, published_at, tick_size="0.25")
    result = settle(spec, oracle)
    assert result.settlement_value is not None
    assert result.settlement_value % Decimal("0.25") == 0


def test_settlement_value_tracks_the_underlying(oracle, window, published_at):
    """A 10000x linear future on a win rate near 0.55 settles near 5500."""
    result = settle(make_spec(window, published_at), oracle)
    assert result.underlying_level is not None
    assert 0.45 < result.underlying_level < 0.65
    assert result.settlement_value == pytest.approx(
        Decimal(str(round(result.underlying_level * 10_000, 2))), abs=Decimal("0.5")
    )


def test_result_carries_full_provenance(oracle, window, published_at):
    result = settle(make_spec(window, published_at), oracle)
    (resolution,) = result.resolutions
    source_ids = {source.source_id for source in resolution.sources}

    assert any(name.startswith("reference:") for name in source_ids)
    assert all(source.digest.startswith("sha256:") for source in resolution.sources)
    assert dict(resolution.diagnostics)["reference_id"] == "ref-2026S09-v1"
    assert dict(resolution.diagnostics)["standardized"] is True


# --------------------------------------------------------------------------
# Digest sensitivity: the record must notice when anything material changed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("payoff", Linear(scale=10_000.0, offset=1.0)),
        ("tick_size", "0.5"),
        ("policy", DataPolicy(min_sample_size=2_000)),
        ("contract_id", "SOMETHING_ELSE"),
    ],
)
def test_changing_the_spec_changes_its_digest(window, published_at, field, value):
    base = make_spec(window, published_at)
    mutated = make_spec(window, published_at, **{field: value})
    assert base.spec_digest != mutated.spec_digest


def test_changing_the_dataset_changes_the_result_digest(
    dataset, reference, policy, window, published_at
):
    """Settling the same spec against different evidence must be visible."""
    spec = make_spec(window, published_at)
    full = settle(spec, BrawlOracle(dataset, reference, policy))

    trimmed = dataset.visible_at(window.end + timedelta(days=3))
    partial = settle(spec, BrawlOracle(trimmed, reference, policy))

    assert full.result_digest != partial.result_digest


def test_reference_snapshot_is_covered_by_the_spec_digest(window, published_at):
    base = make_spec(window, published_at)
    other = make_spec(window, published_at, reference_id="ref-2026S09-v2")
    assert base.spec_digest != other.spec_digest


def test_oracle_pinned_to_a_different_reference_is_rejected(
    dataset, reference, policy, window, published_at
):
    """Wiring the experiment up wrong raises; it does not quietly void."""
    spec = make_spec(window, published_at, reference_id="ref-SOMETHING-ELSE")
    with pytest.raises(ReferenceMismatch, match="pinned to reference"):
        settle(spec, BrawlOracle(dataset, reference, policy))


def test_snapshot_dated_after_the_window_opens_is_rejected(
    dataset, reference, policy, published_at
):
    """The most dangerous lookahead channel, because it improves results.

    A snapshot estimated from data inside its own observation window has seen
    the outcome, so its weights and priors encode the answer. The settlement it
    produces would look *better*, not broken -- which is exactly why this has to
    be a hard error rather than something a reviewer is expected to notice.
    """
    # A window that opens the day before the snapshot was estimated.
    late = ObservationWindow(
        start=reference.as_of - timedelta(days=1),
        end=reference.as_of + timedelta(days=28),
    )
    spec = make_spec(late, late.start - timedelta(days=1))

    with pytest.raises(ReferenceLookahead, match="after the window had already begun"):
        settle(spec, BrawlOracle(dataset, reference, policy))


def test_snapshot_dated_exactly_at_the_window_open_is_allowed(
    dataset, reference, policy
):
    """The boundary is inclusive: estimated *at* the open has seen nothing of it."""
    boundary = ObservationWindow(
        start=reference.as_of, end=reference.as_of + timedelta(days=28)
    )
    spec = make_spec(boundary, boundary.start - timedelta(days=1))
    assert settle(spec, BrawlOracle(dataset, reference, policy)).settled


# --------------------------------------------------------------------------
# Voiding: refusing to settle is a feature
# --------------------------------------------------------------------------


def test_voids_when_sample_size_is_below_the_bar(oracle, window, published_at):
    spec = make_spec(
        window,
        published_at,
        policy=DataPolicy(min_sample_size=10**12, min_strata_coverage=0.0),
    )
    result = settle(spec, oracle)

    assert result.status == SettlementStatus.VOID
    assert result.settlement_value is None
    assert "sample size" in result.void_reason


def test_voids_when_no_observations_exist_in_window(
    dataset, reference, policy, published_at
):
    empty = ObservationWindow(
        start=published_at.replace(year=2030),
        end=published_at.replace(year=2030) + timedelta(days=28),
    )
    spec = make_spec(empty, empty.start - timedelta(days=1))
    result = settle(spec, BrawlOracle(dataset, reference, policy))

    assert result.status == SettlementStatus.VOID
    assert "no observations" in result.void_reason


def test_voids_on_unknown_subject(oracle, window, published_at):
    spec = make_spec(
        window,
        published_at,
        underlying=Single(MetricRef(metric="adjusted_win_rate", subject="NOBODY")),
    )
    assert settle(spec, oracle).status == SettlementStatus.VOID


def test_voids_on_unknown_metric(oracle, window, published_at):
    spec = make_spec(
        window,
        published_at,
        underlying=Single(MetricRef(metric="vibes", subject="SPIKE")),
    )
    result = settle(spec, oracle)
    assert result.status == SettlementStatus.VOID
    assert "unknown metric" in result.void_reason


def test_void_record_keeps_the_evidence_it_did_gather(oracle, window, published_at):
    """A spread whose second leg fails still records the first leg's resolution.

    The missing subject is named to sort *after* SPIKE. Atoms resolve in
    canonical order, so a name like "NOBODY" would fail before SPIKE was ever
    reached and the test would pass vacuously with zero resolutions.
    """
    spec = make_spec(
        window,
        published_at,
        underlying=Difference(
            left=Single(SPIKE_WR),
            right=Single(MetricRef(metric="adjusted_win_rate", subject="ZZ_MISSING")),
        ),
    )
    result = settle(spec, oracle)

    assert result.status == SettlementStatus.VOID
    assert len(result.resolutions) == 1
    assert result.resolutions[0].ref.subject == "SPIKE"


# --------------------------------------------------------------------------
# The underlying algebra: one mechanism, three instrument families
# --------------------------------------------------------------------------


def test_binary_contract_settles_to_payout_or_zero(oracle, window, published_at):
    low = make_spec(
        window, published_at, payoff=Binary(">", threshold=0.30), tick_size="0.01"
    )
    high = make_spec(
        window, published_at, payoff=Binary(">", threshold=0.99), tick_size="0.01"
    )
    assert settle(low, oracle).settlement_value == Decimal("1")
    assert settle(high, oracle).settlement_value == Decimal("0")


def test_spread_equals_the_difference_of_its_legs(oracle, window, published_at):
    spike = settle(make_spec(window, published_at, underlying=Single(SPIKE_WR)), oracle)
    crow = settle(make_spec(window, published_at, underlying=Single(CROW_WR)), oracle)
    spread = settle(
        make_spec(
            window,
            published_at,
            underlying=Difference(Single(SPIKE_WR), Single(CROW_WR)),
        ),
        oracle,
    )
    assert spread.underlying_level == pytest.approx(
        spike.underlying_level - crow.underlying_level
    )


def test_basket_equals_its_weighted_components(oracle, window, published_at):
    legs = ((Single(SPIKE_WR), 0.6), (Single(CROW_WR), 0.4))
    index = settle(make_spec(window, published_at, underlying=Basket(legs)), oracle)

    spike = settle(make_spec(window, published_at, underlying=Single(SPIKE_WR)), oracle)
    crow = settle(make_spec(window, published_at, underlying=Single(CROW_WR)), oracle)

    assert index.underlying_level == pytest.approx(
        0.6 * spike.underlying_level + 0.4 * crow.underlying_level
    )


def test_basket_leg_order_does_not_change_the_settlement_value(
    oracle, window, published_at
):
    """Float addition is not associative, so this is a real risk, not a ritual."""
    forward = ((Single(SPIKE_WR), 0.5), (Single(CROW_WR), 0.3), (Single(ELPRIMO_WR), 0.2))
    reverse = tuple(reversed(forward))

    a = settle(make_spec(window, published_at, underlying=Basket(forward)), oracle)
    b = settle(make_spec(window, published_at, underlying=Basket(reverse)), oracle)

    assert a.underlying_level == b.underlying_level
    assert a.settlement_value == b.settlement_value


# --------------------------------------------------------------------------
# Payments, which cannot be walked back
# --------------------------------------------------------------------------


class _FixedOracle:
    """Returns one level for every reference and window, whatever was asked.

    Enough to drive the distribution path past the point where a real oracle
    would have refused, which is the only way to reach the guard below: the
    fixture never produces an out-of-range rate, and a guard that has never
    fired is a guard nobody has checked.
    """

    def __init__(self, value, as_of, reference_id="ref-2026S09-v1"):
        self._value = value
        self._as_of = as_of
        self._reference_id = reference_id

    @property
    def reference_id(self):
        return self._reference_id

    @property
    def reference_as_of(self):
        return self._as_of

    def resolve(self, ref, window, policy_overrides=None):
        return MetricResolution(
            ref=ref, value=self._value, sample_size=10_000, sources=()
        )


def _share(window, published_at, periods=4):
    span = (window.end - window.start) / periods
    return make_spec(
        window,
        published_at,
        contract_id="SPIKE_EQ",
        payoff=Linear(scale=0.0),
        distribution=DistributionSchedule(
            windows=tuple(
                ObservationWindow(
                    window.start + span * n, window.start + span * (n + 1)
                )
                for n in range(periods)
            ),
            payoff=Linear(scale=1_000.0),
        ),
    )


def test_each_period_is_measured_on_its_own_evidence(oracle, window, published_at):
    """A share is worth the stream, and the stream is only interesting if it moves."""
    paid = distributions(_share(window, published_at), oracle)
    assert len(paid) == 4
    assert len(set(paid)) > 1
    assert all(Decimal(0) <= amount <= Decimal(1_000) for amount in paid)


def test_a_payment_outside_the_schedules_range_is_a_hard_error(window, published_at):
    """The one guard a share never had, on the one contract whose cash moves early.

    A share's terminal payoff is Linear(0), so its settlement bounds are [0, 0]
    and the out-of-range check in `settle` can never fire on it -- yet the
    payments happen *before* settlement and `Venue.distribute` lowers the range
    every short is collateralised against by whatever was paid. So a payment
    computed from a level the contract never contemplated would silently move
    the bounds that back the whole contract, and nothing downstream would
    notice. A rate of 1.5 against a declared [0, 1] pays 1,500 on a schedule
    whose range is [0, 1000].
    """
    spec = _share(window, published_at)
    fabulist = _FixedOracle(1.5, published_at)
    with pytest.raises(SettlementOutOfBounds, match="outside the range"):
        distributions(spec, fabulist)

    # And the honest end of the same range still passes, so the guard is not
    # simply refusing everything.
    assert distributions(spec, _FixedOracle(1.0, published_at)) == (Decimal(1_000),) * 4


def test_repeated_leg_is_resolved_once(oracle, window, published_at):
    """Atom deduplication keeps oracle work proportional to distinct metrics."""
    spec = make_spec(
        window,
        published_at,
        underlying=Basket(((Single(SPIKE_WR), 0.5), (Single(SPIKE_WR), 0.5))),
    )
    result = settle(spec, oracle)
    assert len(result.resolutions) == 1
    assert result.underlying_level == pytest.approx(result.resolutions[0].value)
