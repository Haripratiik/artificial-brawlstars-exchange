"""Collateral on a portfolio, and why it is still arithmetic.

Charging every contract its own worst case assumes the world can be
simultaneously terrible for all of them. For contracts on the same underlying
it cannot: they are functions of the same number. An account long a future,
long a put and short a call at the same strike holds a package that put-call
parity says cannot lose anything at all, and it was posting collateral three
times over.

The usual objection to portfolio margining is that it means a risk *model*, a
model is an estimate, and an estimate is exactly what this project's collateral
is not. That objection does not apply here, for the same reason single-contract
collateral is exact: every instrument settles as a known function of a bounded
scalar, so the portfolio's worst case is the minimum of a piecewise-linear
function of one bounded variable. Its minimum sits at an endpoint or a kink,
every kink is known in advance, and there are a handful of them. Evaluating
each is not an approximation of the answer -- it is the answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from arena.contracts.payoff import Binary, Call, Linear, Payoff
from arena.contracts.spec import (
    ContractSpec,
    DataPolicy,
    DistributionSchedule,
    ObservationWindow,
)
from arena.contracts.underlying import MetricRef, Single
from arena.determinism import quantize_to_tick
from arena.portfolio.netting import kinks_of, netting_benefit, worst_case
from arena.sim.time import seconds

from dashboard.build_market import build, instruments

UTC = timezone.utc
WINDOW = ObservationWindow(
    datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 29, tzinfo=UTC)
)


@pytest.fixture(scope="module")
def listed():
    return {i.symbol: i for i in instruments()}


def _holdings(listed, *rows):
    return [(listed[symbol].spec, quantity, D(price)) for symbol, quantity, price in rows]


def _spec(contract_id, payoff, tick="0.25", underlying=None, distribution=None):
    """A hand-built contract, for shapes the live catalogue does not list yet.

    The catalogue only lists `>` binaries with positive thresholds and only
    linear distribution schedules, so three of the faults below are unreachable
    from `instruments()` -- which is exactly why they survived.
    """
    return ContractSpec(
        contract_id=contract_id,
        underlying=underlying or Single(MetricRef("adjusted_win_rate", "SPIKE")),
        payoff=payoff,
        window=WINDOW,
        policy=DataPolicy(min_sample_size=1),
        reference_id="ref-1",
        published_at=WINDOW.start - timedelta(days=1),
        tick_size=tick,
        distribution=distribution,
    )


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


def test_a_riskless_package_needs_no_collateral(listed):
    """A conversion cannot lose at any level, and should be charged for none.

    Long the future, long the put and short the call at one strike is the
    definition of put-call parity. Charged per contract it posts against all
    three worst cases at once -- a hundred thousand on ten lots -- for a package
    whose value does not depend on the outcome at all.
    """
    gross, net = netting_benefit(
        _holdings(
            listed,
            ("SPIKE_WR_FUT", 10, "4670"),
            ("SPIKE_P4700", 10, "31"),
            ("SPIKE_C4700", -10, "1"),
        )
    )
    assert gross > 0
    assert net == 0


def test_a_share_against_its_weekly_legs_needs_no_collateral(listed):
    """The strip is an identity, so the package is riskless and costs nothing."""
    rows = [("SPIKE_EQ", -10, "1869")] + [
        (f"SPIKE_WR_W{n}", 10, "467") for n in (1, 2, 3, 4)
    ]
    gross, net = netting_benefit(_holdings(listed, *rows))
    assert gross > 0
    assert net == 0


def test_a_vertical_spread_is_charged_only_the_gap_between_its_strikes(listed):
    """Which is the most it can lose, and is nothing like the sum of its legs.

    Ten spreads between 4,600 and 4,650 can lose at most fifty points a lot.
    Charged per contract it posts 54,000 for a risk of 500.
    """
    gross, net = netting_benefit(
        _holdings(listed, ("SPIKE_C4600", 10, "70"), ("SPIKE_C4650", -10, "20"))
    )
    assert net == pytest.approx(D(500), abs=1)
    assert gross > 50 * net


def test_a_lone_position_gets_no_discount(listed):
    """There is nothing to net against, and netting must not invent a benefit."""
    gross, net = netting_benefit(_holdings(listed, ("SPIKE_WR_FUT", 10, "4670")))
    assert net == gross


def test_netting_never_asks_for_more_than_the_gross(listed):
    """The gross is a sum of worst cases; the net is the worst case of the sum.

    A netted figure larger than the gross would be an arithmetic error, not a
    conservative choice, so this is a check on the implementation rather than a
    property of markets.

    Asserted exactly. This carried a tolerance of one unit, and the tolerance
    was covering two real faults rather than a rounding artifact: one portfolio
    in the list held a dispersion contract against a win-rate future, which are
    two different scalars and no longer net at all, and `collateral_for` could
    return a *negative* gross for a position opened outside the range its claim
    can settle in. Neither needed slack once fixed.
    """
    portfolios = [
        (("SPIKE_WR_FUT", 7, "4670"),),
        (("SPIKE_C4600", -3, "70"), ("SPIKE_C4700", 5, "10")),
        (("SPIKE_GT47", 40, "0.50"), ("SPIKE_WR_FUT", -2, "4670")),
        (("SPIKE_WR_W1", 5, "467"), ("SPIKE_WR_FUT", -4, "4670")),
        (("SPIKE_EQ", 3, "1869"), ("SPIKE_P4700", -2, "31")),
    ]
    for rows in portfolios:
        gross, net = netting_benefit(_holdings(listed, *rows))
        assert net <= gross, rows
        assert net >= 0, rows


# --------------------------------------------------------------------------
# The line between arithmetic and modelling
# --------------------------------------------------------------------------


def test_two_underlyings_are_refused_rather_than_netted(listed):
    """Netting them would be a correlation, and a correlation is an estimate.

    The rule was a sentence in a docstring and nothing else, and breaking it was
    silent. Measured before the check: a long of four SPIKE_WR_FUT at 4,670
    against a short of four CROW_WR_FUT at the same price netted to **zero**
    against a gross of 40,000, because both are Linear(10000) and the arithmetic
    treated two Brawlers as one number. That is not conservative and it is not
    exact -- it is a perfect-correlation assumption, which is precisely the risk
    model this collateral exists to avoid.
    """
    with pytest.raises(ValueError, match="not written on the same underlying"):
        worst_case(
            _holdings(
                listed, ("SPIKE_WR_FUT", 4, "4670"), ("CROW_WR_FUT", -4, "4670")
            )
        )


def test_the_same_subject_is_not_the_same_underlying(listed):
    """A win rate and a dispersion are different numbers about one Brawler.

    They can be adverse at once -- a Brawler can be losing everywhere *and*
    losing unevenly -- so nothing about sharing a subject makes them net.
    Measured before the check: long four SPIKE_WR_FUT against short five
    SPIKE_DISP charged 26,030 against a gross of 41,030.
    """
    with pytest.raises(ValueError, match="not written on the same underlying"):
        worst_case(
            _holdings(listed, ("SPIKE_WR_FUT", 4, "4670"), ("SPIKE_DISP", -5, "530"))
        )


def test_the_venue_groups_by_exactly_the_rule_netting_enforces(listed):
    """Whatever the venue calls one underlying, `worst_case` must accept.

    The two live in different modules and could drift apart, and drifting apart
    means either the venue nets things the arithmetic refuses -- a crash on an
    ordinary order -- or the arithmetic accepts things the venue would have
    split, which is the silent direction.
    """
    from collections import defaultdict

    from arena.determinism import canonical_json

    groups = defaultdict(list)
    for symbol, instrument in listed.items():
        groups[canonical_json(instrument.spec.underlying.to_dict())].append(symbol)

    for members in groups.values():
        rows = [(symbol, 1, "1") for symbol in sorted(members)]
        assert worst_case(_holdings(listed, *rows)) >= 0


def test_an_option_kinks_at_its_strike(listed):
    """Miss the kink and the minimum is evaluated everywhere except where it is."""
    call = listed["SPIKE_C4600"].spec
    kinks = kinks_of(call.payoff, call.underlying.bounds())
    assert kinks == pytest.approx([0.46])


def test_a_linear_payoff_has_no_kink(listed):
    future = listed["SPIKE_WR_FUT"].spec
    assert kinks_of(future.payoff, future.underlying.bounds()) == []


def test_a_binary_offers_both_sides_of_its_threshold(listed):
    """A step is not a kink: the value differs either side and is never between.

    Which side is adverse depends on the sign of the position, and the caller
    should not have to know -- so both are offered and both are evaluated.
    """
    binary = listed["SPIKE_GT47"].spec
    kinks = kinks_of(binary.payoff, binary.underlying.bounds())
    assert len(kinks) == 2
    assert min(kinks) == pytest.approx(0.47)
    assert max(kinks) > 0.47


def test_an_empty_portfolio_can_lose_nothing():
    assert worst_case([]) == 0


# --------------------------------------------------------------------------
# Steps, and the two ways the old candidates missed one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("comparison", [">", ">=", "<", "<="])
def test_a_binary_offers_both_branches_whichever_way_it_compares(comparison):
    """A step has two values and the candidates must show both of them.

    `>` and `<=` leave the threshold on the low branch, so ``{threshold, just
    above}`` happened to span both. `>=` and `<` put it on the high branch, so
    both candidates sat inside one branch and the other was never evaluated at
    all -- a binary whose entire payout the minimisation could not see.
    """
    payoff = Binary(comparison, 0.5, payout=1.0)
    branches = {payoff.apply(level) for level in kinks_of(payoff, (0.0, 1.0))}
    assert branches == {0.0, 1.0}


def test_a_short_across_a_step_is_charged_for_the_payout():
    """The `<` case end to end, in money rather than in candidate levels.

    Short a Linear(1000) at 467 and short a binary paying 1,000 below 0.5. The
    package is worst just below the threshold, where it owes the payout and the
    linear leg has run half its range. Measured before the fix: 33 charged
    against a loss of 533.
    """
    linear = _spec("F1K", Linear(1_000.0))
    step = _spec("LT50", Binary("<", 0.5, payout=1_000.0), tick="0.01")
    charged = worst_case([(linear, -1, D("467")), (step, -1, D("500"))])
    assert charged == pytest.approx(533, abs=0.001)


def test_a_step_below_minus_one_still_finds_its_far_side():
    """The old epsilon was multiplicative, so it inverted on negative levels.

    ``threshold * (1 + 1e-12) + 1e-12`` lands above a negative threshold only
    while its magnitude is under one. A spread bounded by [-2, 2] with a step at
    -1.5 got a "far side" candidate of -1.5000000000005, on the *same* side, so
    the payout branch went unevaluated: 1,500 charged against a loss of 2,000.
    """
    ref = MetricRef("adjusted_win_rate_lift", "SPIKE", bounds=(-2.0, 2.0))
    linear = _spec("LIFT", Linear(1_000.0), underlying=Single(ref))
    step = _spec(
        "GT_NEG", Binary(">", -1.5, payout=1_000.0), tick="0.01", underlying=Single(ref)
    )
    candidates = kinks_of(step.payoff, (-2.0, 2.0))
    assert min(candidates) == -1.5
    assert max(candidates) > -1.5

    charged = worst_case([(linear, 1, D("0")), (step, -1, D("500"))])
    assert charged == pytest.approx(2_000, abs=0.001)


def test_a_schedules_kink_is_a_kink_of_the_claim():
    """A share is worth its settlement plus everything it pays on the way.

    So a schedule paying an option-shaped amount puts a kink in the portfolio's
    value that the settlement payoff knows nothing about. Short one Call(4600)
    against two shares each paying Call(4700) once: the only kink enumerated was
    0.46, the minimum sits at 0.47, and the package was charged nothing for a
    loss of 100.
    """
    week = ObservationWindow(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC)
    )
    call = _spec("C4600", Call(4_600.0, 10_000.0))
    share = _spec(
        "SHARE",
        Linear(0.0),
        distribution=DistributionSchedule(windows=(week,), payoff=Call(4_700.0, 10_000.0)),
    )
    assert worst_case([(call, -1, D("0")), (share, 2, D("0"))]) == pytest.approx(100)


def test_an_unknown_payoff_shape_is_refused_rather_than_sampled():
    """Sampling was not conservative, it was wrong by an unbounded amount.

    The old fallback took 63 evenly spaced levels and called the smallest of
    them the answer. Against a payoff that dips to -1,000 inside a window 0.004
    wide, all 63 samples missed the dip and the portfolio was charged nothing
    for a position that loses 1,000. There is no sample count that fixes that,
    so a shape whose kinks are unknown is refused.
    """

    class Spike(Payoff):
        def apply(self, level):
            return -1_000.0 if abs(level - 0.5031) <= 0.002 else 0.0

        def bounds(self, level_bounds):
            return (-1_000.0, 0.0)

        def to_dict(self):
            return {"kind": "spike"}

    with pytest.raises(TypeError, match="no declared kinks"):
        kinks_of(Spike(), (0.0, 1.0))
    with pytest.raises(TypeError, match="no declared kinks"):
        worst_case([(_spec("SPIKEY", Spike()), 1, D("0"))])


# --------------------------------------------------------------------------
# The one gap that is measured rather than closed
# --------------------------------------------------------------------------


def test_the_tick_grid_is_where_exactness_stops(listed):
    """What settles is the quantized payoff, and staircases do not cancel.

    Legs sharing a scale and a tick cancel exactly -- half-even rounding is odd,
    so ``quantize(x) + quantize(K - x) = K`` for ``K`` on the grid, which is why
    put-call parity and the four weekly legs of a share still net to zero here.
    Legs of *different* scale do not. This package is riskless before
    quantization and loses 1.25 after it, at a level of 0.00013.

    Recorded rather than corrected: the tight answer means enumerating every
    level where any leg crosses a half-tick boundary -- 40,000 of them for one
    scale-10,000 contract on a 0.25 grid -- which an order-entry check cannot
    afford, and the half-tick allowance that would bound it cheaply is not the
    exact number either. The loss is bounded by ``sum |quantity| * tick / 2``,
    and this asserts that bound so a future change cannot quietly widen it.
    """
    future = listed["SPIKE_WR_FUT"].spec
    weekly = listed["SPIKE_WR_W1"].spec
    holdings = [(future, 1, D("4670")), (weekly, -10, D("467"))]
    assert worst_case(holdings) == 0

    worst = D(0)
    for step in range(2_001):
        # A tenth of a basis point apart, over the bottom two percent of the
        # range. The worst point sits at 0.00013, which a coarser sweep steps
        # straight over -- the staircase is 40,000 steps wide and its shape is
        # not visible at any resolution a test can afford, which is the whole
        # reason this is bounded rather than solved.
        level = step * 0.02 / 2_000
        realised = sum(
            D(quantity)
            * (quantize_to_tick(spec.payoff.apply(level), spec.tick_size) - price)
            for spec, quantity, price in holdings
        )
        worst = min(worst, realised)

    allowance = sum(abs(q) * D(s.tick_size) for s, q, _p in holdings) / 2
    assert -worst == D("1.25")
    assert -worst <= allowance == D("1.375")


# --------------------------------------------------------------------------
# On the venue
# --------------------------------------------------------------------------


def test_the_venue_charges_gross_unless_asked_otherwise():
    """Off by default, so every published measurement keeps meaning what it did."""
    market = build(seed=7)
    assert market.venue.netting is False


# Two full 300-second market simulations, one gross and one netted, because the
# claim is a DIFFERENCE between two trajectories and neither side can be
# shortened without changing the number the docstring records. 678 seconds, and
# it is the single reason the suite cannot be run on every push.
@pytest.mark.slow
def test_netting_frees_the_arbitrageur_when_collateral_binds():
    """Its whole business is holding packages that offset.

    This claim got considerably smaller when the measurement got better, and
    the shrinkage is the useful part. It used to read 944 attempts and 374
    fundings missed against 1,094 and 281 -- a large, clean win for netting.
    Most of that win was two defects in the branch it was being compared
    against. The *gross* check was counting working orders that were no longer
    on the book, and was charging `resulting quantity * incoming price` rather
    than the cost basis the fill would actually create. Both inflated gross
    collateral, and an inflated comparison makes anything look good.

    Re-measured over five minutes with those fixed, `starved` being the
    occasions it saw a mispricing and could not fund the trade:

        seed   gross att / starved    netted att / starved
          41        1045 / 341             1030 / 248
           7         916 / 354              916 / 354
          13         946 / 225              946 / 225
          29         929 / 344              929 / 344
          55         720 / 337              720 / 337

    On four of five seeds netting changes *nothing at all*, down to the event.
    That is not netting failing, it is netting not binding: with the gross
    check no longer overcharging, the arbitrageur rarely runs out of capital,
    and a discount on a bill you were never struggling to pay buys you nothing.
    It is also a strong statement in its own right -- an identical trajectory
    proves netting is inert when it should be inert, rather than quietly
    permitting trades gross margining would refuse.

    Where capital genuinely binds it still does its job, and that is seed 41,
    which is the one asserted here. Note the direction of the two numbers:
    netting is starved 27% less often and *attempts slightly fewer* trades.
    That is not a contradiction. Funding more of what it sees means acting on
    the mispricings sooner, which removes them, so there are fewer left to see.
    `starved` is the direct measure of the constraint and `attempts` is a
    second-order consequence of a diverging trajectory, so only the first is
    asserted.
    """
    attempts = {}
    starved = {}
    for netting in (False, True):
        market = build(seed=41, arbitrageur=True)
        market.venue.netting = netting
        market.kernel.start()
        market.kernel.advance(until=seconds(300))
        agent = next(a for a in market.agents if a.agent_id == "arb-1")
        attempts[netting] = agent.attempts
        starved[netting] = agent.starved
        assert int(market.venue.conservation_check()) == 0

    assert starved[True] < starved[False], (
        f"netting left the arbitrageur starved {starved[True]} times against "
        f"{starved[False]} charged gross, so it bought nothing where capital binds"
    )
