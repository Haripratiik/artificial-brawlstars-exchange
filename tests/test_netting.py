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

from decimal import Decimal as D

import pytest

from arena.portfolio.netting import kinks_of, netting_benefit, worst_case
from arena.sim.time import seconds

from dashboard.build_market import build, instruments


@pytest.fixture(scope="module")
def listed():
    return {i.symbol: i for i in instruments()}


def _holdings(listed, *rows):
    return [(listed[symbol].spec, quantity, D(price)) for symbol, quantity, price in rows]


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
    """
    portfolios = [
        (("SPIKE_WR_FUT", 7, "4670"),),
        (("SPIKE_C4600", -3, "70"), ("SPIKE_C4700", 5, "10")),
        (("SPIKE_GT47", 40, "0.50"), ("SPIKE_WR_FUT", -2, "4670")),
        (("SPIKE_DISP", 5, "530"), ("SPIKE_WR_FUT", 4, "4670")),
    ]
    for rows in portfolios:
        gross, net = netting_benefit(_holdings(listed, *rows))
        assert net <= gross + 1, rows
        assert net >= 0, rows


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
# On the venue
# --------------------------------------------------------------------------


def test_the_venue_charges_gross_unless_asked_otherwise():
    """Off by default, so every published measurement keeps meaning what it did."""
    market = build(seed=7)
    assert market.venue.netting is False


def test_netting_lets_the_arbitrageur_act_more_often():
    """Its whole business is holding packages that offset.

    Measured over five minutes on seed 41: 944 attempts and 374 occasions where
    it saw a mispricing and could not fund the trade, against 1,094 and 281
    with the portfolio charged for what it can actually lose.
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

    assert attempts[True] > attempts[False]
    assert starved[True] < starved[False]
