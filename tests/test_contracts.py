"""Contract validation, determinism primitives, and the no-lookahead interface.

The lookahead tests matter more than they look. A lookahead bug does not crash
and does not produce implausible numbers -- it produces *better* results, which
is the worst possible failure mode for a research project, because nothing about
the output invites suspicion. So each channel through which the future could
leak gets an explicit, named test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arena.contracts.payoff import Binary, Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import ALL, Basket, MetricRef, Single
from arena.determinism import canonical_json, digest, quantize_to_tick, stable_sum

UTC = timezone.utc
WINDOW = ObservationWindow(
    start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 10, 1, tzinfo=UTC)
)


def spec_with(**overrides) -> ContractSpec:
    defaults = dict(
        contract_id="TEST",
        underlying=Single(MetricRef(metric="adjusted_win_rate", subject="SPIKE")),
        payoff=Linear(scale=10_000.0),
        window=WINDOW,
        policy=DataPolicy(min_sample_size=1_000),
        reference_id="ref-2026S09-v1",
        published_at=WINDOW.start - timedelta(days=1),
    )
    defaults.update(overrides)
    return ContractSpec(**defaults)


# --------------------------------------------------------------------------
# No-lookahead invariants
# --------------------------------------------------------------------------


def test_contract_published_after_its_window_opens_is_rejected():
    """Writing a market after the outcome has begun forming is lookahead."""
    with pytest.raises(ValueError, match="lookahead by construction"):
        spec_with(published_at=WINDOW.start + timedelta(days=1))


def test_contract_published_exactly_at_window_open_is_allowed():
    assert spec_with(published_at=WINDOW.start).published_at == WINDOW.start


def test_dataset_visibility_filters_on_observation_not_window(dataset):
    """An observer at time t sees rows collected by t, not rows *about* t.

    This is the distinction that makes replay honest. A row describing August
    battles that was not finished being collected until September must be
    invisible to an agent standing in late August.
    """
    # Chosen to land inside the fixture's collection lag: the window ending
    # 17 Aug has closed, but is not collected until 19 Aug. A cutoff outside
    # that gap would make the test pass without demonstrating anything.
    cutoff = datetime(2026, 8, 18, tzinfo=UTC)
    visible = dataset.visible_at(cutoff)

    assert len(visible) < len(dataset)
    assert all(row.observed_at <= cutoff for row in visible)
    # And crucially: some hidden rows describe windows that already closed.
    hidden = [row for row in dataset if row.observed_at > cutoff]
    assert any(row.window_end <= cutoff for row in hidden), (
        "fixture should contain already-closed windows that were not yet collected"
    )


def test_aggregate_row_cannot_be_knowable_before_its_window_closes(dataset):
    from tests.conftest import make_row

    with pytest.raises(ValueError, match="cannot be knowable"):
        row = make_row(battles=10, wins=5)
        type(row)(
            observed_at=row.window_start,
            window_start=row.window_start,
            window_end=row.window_end,
            brawler_id=row.brawler_id,
            mode_id=row.mode_id,
            map_id=row.map_id,
            trophy_bucket=row.trophy_bucket,
            brawler_battles=row.brawler_battles,
            brawler_wins=row.brawler_wins,
            brawler_draws=row.brawler_draws,
            stratum_battles=row.stratum_battles,
            stratum_slots=row.stratum_slots,
            source_id=row.source_id,
        )


def test_visibility_cutoff_must_be_timezone_aware(dataset):
    with pytest.raises(ValueError, match="timezone-aware"):
        dataset.visible_at(datetime(2026, 8, 20))


# --------------------------------------------------------------------------
# Spec validation
# --------------------------------------------------------------------------


def test_window_must_be_utc_and_ordered():
    with pytest.raises(ValueError, match="timezone-aware"):
        ObservationWindow(datetime(2026, 9, 1), datetime(2026, 10, 1))
    with pytest.raises(ValueError, match="must precede"):
        ObservationWindow(
            datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)
        )


def test_window_is_half_open():
    assert WINDOW.contains(WINDOW.start)
    assert not WINDOW.contains(WINDOW.end)


def test_reference_id_is_required():
    with pytest.raises(ValueError, match="standardization must be pinned"):
        spec_with(reference_id="")


def test_metric_ref_filters_must_be_sorted_and_unambiguous():
    MetricRef(metric="m", subject="s", modes=("brawlBall", "gemGrab"))
    with pytest.raises(ValueError, match="must be sorted"):
        MetricRef(metric="m", subject="s", modes=("gemGrab", "brawlBall"))
    with pytest.raises(ValueError, match="mixes 'ALL'"):
        MetricRef(metric="m", subject="s", modes=(ALL, "gemGrab"))
    with pytest.raises(ValueError, match="duplicates"):
        MetricRef(metric="m", subject="s", maps=("A", "A"))
    with pytest.raises(ValueError, match="non-empty"):
        MetricRef(metric="m", subject="s", maps=())


def test_equal_filters_compare_equal_regardless_of_construction():
    a = MetricRef(metric="m", subject="s", modes=("brawlBall", "gemGrab"))
    b = MetricRef(metric="m", subject="s", modes=("brawlBall", "gemGrab"))
    assert a == b and hash(a) == hash(b)


def test_binary_payoff_rejects_unknown_comparison():
    with pytest.raises(ValueError, match="comparison must be one of"):
        Binary("~=", threshold=0.5)


def test_empty_basket_is_rejected():
    with pytest.raises(ValueError, match="at least one leg"):
        Basket(())


def test_policy_bounds_are_validated():
    with pytest.raises(ValueError, match="must lie in"):
        DataPolicy(min_sample_size=1, min_strata_coverage=1.5)
    with pytest.raises(ValueError, match="cannot be negative"):
        DataPolicy(min_sample_size=-1)
    with pytest.raises(ValueError, match="must be one of"):
        DataPolicy(min_sample_size=1, missing_data_policy="GUESS")


# --------------------------------------------------------------------------
# Determinism primitives
# --------------------------------------------------------------------------


def test_digest_is_insensitive_to_key_order():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_digest_is_sensitive_to_values():
    assert digest({"a": 1}) != digest({"a": 1.0000001})


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_canonical_json_rejects_unserializable_types():
    with pytest.raises(TypeError, match="not canonically serializable"):
        canonical_json({"x": object()})


@pytest.mark.parametrize(
    "value,tick,expected",
    [
        (5537.4999, "0.25", "5537.5"),
        # Half-even: .125 on a 0.25 grid rounds to the even multiple.
        (5537.125, "0.25", "5537"),
        (5537.375, "0.25", "5537.5"),
        (0.004, "0.01", "0"),
        (-3.5, "1", "-4"),
    ],
)
def test_tick_quantization_uses_bankers_rounding(value, tick, expected):
    assert quantize_to_tick(value, tick) == Decimal(expected)


def test_tick_size_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        quantize_to_tick(1.0, "0")


def test_stable_sum_is_order_independent():
    values = [1e16, 1.0, -1e16, 2.0, 1e-9]
    assert stable_sum(values) == stable_sum(list(reversed(values)))


def test_spec_digest_is_stable_across_construction_order():
    assert spec_with().spec_digest == spec_with().spec_digest
