"""The option chain, and the two reasons it was not one.

A set of call prices at a single maturity is free of static arbitrage exactly
when it is decreasing and convex in strike with slope in [-1, 0] (Davis and
Hobson 2007; Carr and Madan 2005). The live market satisfied none of those:
`SPIKE_C4700` marked at 72.7 while `SPIKE_C4600` marked at 59.1, which is a
riskless trade anyone could take, and put-call parity was out by 35 ticks.

Two causes, and the smaller one was the market maker.

The larger was that **every agent held a separate view of the same Brawler for
every contract written on it**. `FundamentalTrader` drew its estimate and its
Monte Carlo sample per symbol, so SPIKE_C4600 and SPIKE_C4650 were valued from
independent draws of the same posterior; the error between them was
independent, so the ladder was not monotone and the agent traded on the
difference. Measured before the fix: `fund-vague` valued the 4,650 call at
119.03 and the strictly more valuable 4,600 call at 36.67.

These tests pin both, and they pin the property rather than the numbers: what
must hold is that a chain priced off one distribution cannot be arbitraged, and
that an agent has one opinion per thing rather than one per contract.
"""

from __future__ import annotations

import math

import pytest

from arena.agents.arbitrageur import Relation, derive_relations
from arena.agents.surface import call_delta, derive_chains, option_value
from arena.sim.time import seconds

from dashboard.build_market import build, instruments

SCALE = 10_000.0
FORWARD = 4_669.0
LADDER = (4_500, 4_600, 4_650, 4_700, 4_750, 4_800)


# --------------------------------------------------------------------------
# The pricing function itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("concentration", [40.0, 900.0, 20_000.0, 500_000.0])
def test_a_chain_priced_off_one_distribution_cannot_be_arbitraged(concentration):
    """Monotone, convex, and slope in [-1, 0], at every width.

    All three follow from the prices being ``E[(F - K)+]`` under some law, and
    none of them survive pricing each strike separately. Checked across four
    concentrations because the point is that it holds for *any* belief, not
    that one belief happens to produce a tidy ladder.
    """
    calls = [option_value(FORWARD, k, SCALE, concentration, True) for k in LADDER]

    for lower, higher in zip(calls, calls[1:]):
        assert lower >= higher, "a call struck higher costs more"
    for a, b, c in zip(calls, calls[1:], calls[2:]):
        assert a - 2 * b + c >= -1e-9, "the middle strike is above the line"
    for (k1, c1), (k2, c2) in zip(zip(LADDER, calls), zip(LADDER[1:], calls[1:])):
        slope = (c2 - c1) / (k2 - k1)
        assert -1.0 - 1e-9 <= slope <= 1e-9, f"slope {slope} outside [-1, 0]"


def test_put_call_parity_is_exact_by_construction():
    """Not approximately: the put is defined as the call's parity partner.

    Integrating the put separately would leave the two agreeing to within
    floating point, and the entire purpose of quoting from one distribution is
    that they agree exactly.
    """
    for strike in LADDER:
        call = option_value(FORWARD, strike, SCALE, 900.0, True)
        put = option_value(FORWARD, strike, SCALE, 900.0, False)
        assert call - put - (FORWARD - strike) == pytest.approx(0.0, abs=1e-12)


def test_a_call_is_worth_more_than_intrinsic_and_less_than_the_forward():
    """The bounds every option satisfies, whatever the model."""
    for strike in LADDER:
        call = option_value(FORWARD, strike, SCALE, 900.0, True)
        assert call >= max(0.0, FORWARD - strike) - 1e-9
        assert call <= FORWARD + 1e-9


def test_a_tighter_belief_is_worth_less_time_value():
    """Concentration is the width, so more of it means cheaper options.

    This is the property that makes the estimate worth estimating: when the
    underlying stops moving the chain has to converge on intrinsic value, or
    the maker is selling insurance against a risk that has gone away.
    """
    strike = 4_700
    values = [
        option_value(FORWARD, strike, SCALE, k, True)
        for k in (100.0, 1_000.0, 10_000.0, 1_000_000.0)
    ]
    assert values == sorted(values, reverse=True)
    assert values[-1] == pytest.approx(max(0.0, FORWARD - strike), abs=0.5)


def test_delta_is_the_derivative_of_the_price():
    """``dC/dK = -P(F > K)``, which is what makes delta hedging arithmetic."""
    strike, step = 4_650.0, 0.01
    up = option_value(FORWARD, strike + step, SCALE, 900.0, True)
    down = option_value(FORWARD, strike - step, SCALE, 900.0, True)
    numeric = -(up - down) / (2 * step)
    assert numeric == pytest.approx(call_delta(FORWARD, strike, SCALE, 900.0), abs=1e-3)


def test_delta_falls_from_one_to_zero_across_the_ladder():
    deltas = [call_delta(FORWARD, k, SCALE, 900.0) for k in LADDER]
    assert deltas == sorted(deltas, reverse=True)
    assert all(0.0 <= d <= 1.0 for d in deltas)


# --------------------------------------------------------------------------
# One view per underlying
# --------------------------------------------------------------------------


def test_an_agent_holds_one_view_per_underlying_not_per_contract():
    """Three strikes on SPIKE are three contracts and one opinion.

    Independent draws per contract made the agent's own ladder non-monotone --
    measured at 119.03 for the 4,650 call against 36.67 for the 4,600 -- and it
    then traded on a difference that was entirely its own Monte Carlo error.
    """
    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(90))

    calls = ["SPIKE_C4600", "SPIKE_C4650", "SPIKE_C4700"]
    checked = 0
    for agent in market.kernel._agents.values():
        view = getattr(agent, "_estimate", None) or getattr(agent, "_value", None)
        if not view or any(view.get(s) is None for s in calls):
            continue
        values = [view[s] for s in calls]
        assert values == sorted(values, reverse=True), (
            f"{agent.agent_id} values {dict(zip(calls, values))}, which is not a "
            "ladder any single view of SPIKE could produce"
        )
        assert values[0] - 2 * values[1] + values[2] >= -1e-6, (
            f"{agent.agent_id}'s own ladder is not convex"
        )
        checked += 1
    assert checked >= 2, "no informed agent had a view; the test proves nothing"


def test_two_contracts_on_one_brawler_share_a_posterior():
    """The sample is battles involving a Brawler, not battles involving a bet."""
    from arena.agents.bayesian import BayesianFundamental
    from arena.exchange.types import AgentId
    from arena.market.live import VENUE_ID

    listed = {i.symbol: i for i in instruments()}
    levels = {s: 0.47 for s in listed}
    agent = BayesianFundamental(
        AgentId("probe"), VENUE_ID, listed, levels, battles=500
    )

    market = build(seed=7)
    market.kernel.add(agent)
    market.kernel.start()

    class _Ctx:
        rng = market.kernel.rng_for(AgentId("probe"))

    ctx = _Ctx()
    first = agent.posterior(ctx, "SPIKE_C4600")
    second = agent.posterior(ctx, "SPIKE_C4700")
    third = agent.posterior(ctx, "SPIKE_WR_FUT")
    other = agent.posterior(ctx, "CROW_WR_FUT")
    assert first == second == third, "one Brawler, three posteriors"
    assert other != first, "two Brawlers collapsed into one posterior"


# --------------------------------------------------------------------------
# Relations: bands, not only identities
# --------------------------------------------------------------------------


def test_a_band_relation_is_only_breached_outside_the_band():
    relation = Relation("v", "A", (("B", 1.0),), lower=0.0, upper=50.0)
    assert relation.excess(120.0, {"B": 100.0}) == 0.0
    assert relation.excess(150.0, {"B": 100.0}) == 0.0
    assert relation.excess(163.0, {"B": 100.0}) == pytest.approx(13.0)
    assert relation.excess(94.0, {"B": 100.0}) == pytest.approx(-6.0)


def test_an_identity_is_a_band_of_zero_width():
    """So every relation that existed before still behaves exactly as it did."""
    relation = Relation("p", "A", (("B", 1.0), ("C", 1.0)), constant=-10.0)
    assert relation.excess(95.0, {"B": 50.0, "C": 55.0}) == pytest.approx(0.0)
    assert relation.excess(99.0, {"B": 50.0, "C": 55.0}) == pytest.approx(4.0)


def test_the_option_chain_produces_vertical_and_butterfly_relations():
    relations = {r.name: r for r in derive_relations({i.symbol: i for i in instruments()})}
    verticals = [r for name, r in relations.items() if name.startswith("vertical:")]
    butterflies = [r for name, r in relations.items() if name.startswith("butterfly:")]
    assert verticals and butterflies

    for relation in verticals:
        assert relation.lower == 0.0 and relation.upper > 0.0
    for relation in butterflies:
        assert relation.upper == 0.0 and relation.lower == -math.inf


def test_a_share_relates_to_the_weeks_it_pays():
    """And exactly, because both sides resolve the same metric over the same weeks."""
    from dashboard.build_market import true_values

    listed = instruments()
    relations = {r.name: r for r in derive_relations({i.symbol: i for i in listed})}
    strip = relations.get("strip:SPIKE_EQ")
    assert strip is not None, "the share has no replicating package"

    values = true_values(listed)
    replicated = sum(values[leg] for leg, _weight in strip.legs)
    assert replicated == values["SPIKE_EQ"], (
        f"the strip settles at {replicated} and the share at {values['SPIKE_EQ']}; "
        "if these differ the relation is not an identity and must not be traded"
    )


def test_two_contracts_differing_only_by_window_are_not_confused():
    """The lookup key includes the period, and it has to.

    Without it a relation can be formed against the wrong week -- an identity
    between two things that are not the same thing, traded as though it were
    free money. Nothing was mispriced by it while no composite referenced a
    weekly contract; listing weekly futures is what would have made it wrong.
    """
    from arena.agents.arbitrageur import _underlying_key

    listed = {i.symbol: i for i in instruments()}
    weekly = [s for s in listed if s.startswith("SPIKE_WR_W")]
    assert len(weekly) >= 2
    keys = {_underlying_key(listed[s]) for s in weekly}
    assert len(keys) == len(weekly), "two delivery weeks share one key"
    assert _underlying_key(listed["SPIKE_WR_FUT"]) not in keys


# --------------------------------------------------------------------------
# The market, end to end
# --------------------------------------------------------------------------


def test_the_live_chain_is_free_of_static_arbitrage():
    """The measurement that motivated all of it, as a test.

    Scored only when the books involved are two-sided, because a book with one
    side has no price -- and that is not a technicality here: under the plain
    maker `SPIKE_C4700` is two-sided 0% of the time, so its apparent
    consistency was three quotable books out of five rather than a surface.
    """
    market = build(seed=7, surface=True)
    market.kernel.start()

    strikes = [(4_600, "SPIKE_C4600"), (4_650, "SPIKE_C4650"), (4_700, "SPIKE_C4700")]
    scored = 0
    for t in range(60, 241, 20):
        market.kernel.advance(until=seconds(t))
        books = {
            symbol: market.venue.engine(symbol).book.snapshot()
            for _k, symbol in strikes
        }
        if any(b.best_bid is None or b.best_ask is None for b in books.values()):
            continue
        marks = [float(market.venue.mark_price(s)) for _k, s in strikes]
        assert marks[0] >= marks[1] >= marks[2], f"not monotone at t={t}: {marks}"
        assert marks[0] - 2 * marks[1] + marks[2] >= -1e-9, f"not convex at t={t}"
        for i in (0, 1):
            width = strikes[i + 1][0] - strikes[i][0]
            assert marks[i] - marks[i + 1] <= width + 1e-9, (
                f"vertical too wide at t={t}: {marks[i]} - {marks[i + 1]} > {width}"
            )
        scored += 1
    assert scored >= 5, f"only {scored} moments had a quotable chain"


def test_every_strike_stays_quotable():
    """A chain with no price on half its strikes is not a chain."""
    market = build(seed=7, surface=True)
    market.kernel.start()

    chain = derive_chains({i.symbol: i for i in instruments()})
    assert chain, "nothing was matched to an underlying"

    quotable = {symbol: 0 for symbol in chain}
    moments = list(range(60, 241, 20))
    for t in moments:
        market.kernel.advance(until=seconds(t))
        for symbol in chain:
            book = market.venue.engine(symbol).book.snapshot()
            if book.best_bid is not None and book.best_ask is not None:
                quotable[symbol] += 1

    for symbol, count in quotable.items():
        assert count == len(moments), (
            f"{symbol} was two-sided {count}/{len(moments)} of the time"
        )
