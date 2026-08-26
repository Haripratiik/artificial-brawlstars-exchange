"""Maker-taker fees, and the order type that exists because of them.

Two things could go wrong here and only one of them is loud. The loud one is
charging the wrong amount. The quiet one is charging the right amount and
putting it nowhere -- the ledger would still balance to within a rounding error,
every test that checks "roughly conserved" would pass, and the venue's central
invariant would have become an approximation without anyone noticing.

So the conservation tests here are exact-equality tests, and there is a separate
one asserting that the venue's own account holds precisely what the participants
paid.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arena.contracts.payoff import Linear
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Single
from arena.exchange.events import Filled, Rejected, Submit
from arena.exchange.types import (
    AgentId,
    OrderType,
    Price,
    Quantity,
    RejectReason,
    Side,
    TimeInForce,
)
from arena.market.fees import FREE, MAKER_TAKER, FeeSchedule
from arena.market.instrument import Instrument
from arena.market.venue import FEE_ACCOUNT_ID, Venue
from arena.portfolio.money import Money
from arena.worlds.brawl.metrics import metric_ref

UTC = timezone.utc
START = datetime(2026, 8, 31, tzinfo=UTC)
MAKER = AgentId("maker")
TAKER = AgentId("taker")


def _instrument() -> Instrument:
    spec = ContractSpec(
        contract_id="F",
        underlying=Single(metric_ref("adjusted_win_rate", "SUBJECT")),
        payoff=Linear(10_000.0),
        window=ObservationWindow(START, START + timedelta(days=28)),
        policy=DataPolicy(
            min_sample_size=1_000, min_stratum_battles=200, min_strata_coverage=0.80
        ),
        reference_id="ref-2026S09-v1",
        published_at=START - timedelta(days=1),
        tick_size="0.25",
    )
    return Instrument("F", spec)


def _venue(fees: FeeSchedule = MAKER_TAKER) -> Venue:
    venue = Venue("arena", starting_cash=50_000_000, fees=fees)
    venue.list_instrument(_instrument())
    return venue


def _rest(venue: Venue, side: Side, price: int, quantity: int, who: AgentId = MAKER):
    return venue.submit(
        who,
        "F",
        Submit(who, side, Quantity(quantity), Price(price), OrderType.LIMIT,
               TimeInForce.GTC),
    )


def _cross(venue: Venue, side: Side, quantity: int, who: AgentId = TAKER):
    return venue.submit(
        who,
        "F",
        Submit(who, side, Quantity(quantity), None, OrderType.MARKET, TimeInForce.IOC),
    )


# --------------------------------------------------------------------------
# The schedule itself
# --------------------------------------------------------------------------


def test_a_free_schedule_charges_nothing():
    assert FREE.free
    assert int(FREE.charge(10**9, aggressor=True)) == 0
    assert int(FREE.charge(10**9, aggressor=False)) == 0


def test_the_taker_pays_and_the_maker_is_paid():
    assert int(MAKER_TAKER.charge(1_000_000, aggressor=True)) > 0
    assert int(MAKER_TAKER.charge(1_000_000, aggressor=False)) < 0


@pytest.mark.parametrize("notional", [1, 7, 999, 333_333, 10**9])
def test_rounding_always_favours_the_venue(notional):
    """A charge rounds up and a rebate rounds down in magnitude.

    The other way round, a strategy of many tiny fills would extract a fraction
    of a unit per trade -- precisely the leak the integer ledger exists to make
    impossible.
    """
    taker = int(MAKER_TAKER.charge(notional, aggressor=True))
    maker = int(MAKER_TAKER.charge(notional, aggressor=False))
    assert taker >= notional * MAKER_TAKER.taker_bps / 10_000
    assert maker >= notional * MAKER_TAKER.maker_bps / 10_000
    assert taker + maker >= 0, "the venue paid out more than it took in"


def test_a_schedule_that_pays_more_than_it_takes_is_representable():
    """Venues really do this, and really do lose money doing it.

    Refusing to model it would hide a genuine failure mode behind a validation
    error, so it is allowed and simply shows up as a negative venue balance.
    """
    generous = FeeSchedule(taker_bps=1.0, maker_bps=-3.0)
    total = int(generous.charge(1_000_000, True)) + int(
        generous.charge(1_000_000, False)
    )
    assert total < 0


# --------------------------------------------------------------------------
# Fees through the ledger
# --------------------------------------------------------------------------


def test_value_is_conserved_exactly_with_fees_on():
    """Zero, not nearly zero. A fee is a transfer, not an evaporation."""
    venue = _venue()
    rng = random.Random(4)
    for _ in range(120):
        _rest(venue, Side.BUY, rng.randint(18_000, 18_500), rng.randint(1, 40))
        _rest(venue, Side.SELL, rng.randint(18_600, 19_000), rng.randint(1, 40))
        _cross(venue, rng.choice([Side.BUY, Side.SELL]), rng.randint(1, 30))
        assert venue.conservation_check() == 0


def test_the_venue_account_holds_exactly_what_was_charged():
    """The quiet failure: right amount charged, wrong amount banked."""
    venue = _venue()
    _rest(venue, Side.SELL, 18_800, 500)
    _cross(venue, Side.BUY, 300)
    treasury = venue.account(FEE_ACCOUNT_ID)
    banked = int(treasury.cash) - int(treasury.starting_cash)
    assert banked == int(venue.fees_collected)
    assert banked > 0


def test_the_taker_is_charged_and_the_maker_credited_on_a_real_fill():
    venue = _venue()
    _rest(venue, Side.SELL, 18_800, 200)
    before_maker = int(venue.account(MAKER).cash)
    before_taker = int(venue.account(TAKER).cash)
    _cross(venue, Side.BUY, 100)
    assert int(venue.account(MAKER).cash) > before_maker
    assert int(venue.account(TAKER).cash) < before_taker


def test_fees_scale_with_the_notional_traded():
    small, large = _venue(), _venue()
    _rest(small, Side.SELL, 18_800, 500)
    _rest(large, Side.SELL, 18_800, 500)
    _cross(small, Side.BUY, 10)
    _cross(large, Side.BUY, 400)
    assert int(large.fees_collected) > int(small.fees_collected)


def test_a_free_venue_behaves_exactly_as_before():
    """Fees are off by default, so every existing measurement keeps its meaning."""
    venue = _venue(FREE)
    _rest(venue, Side.SELL, 18_800, 300)
    _cross(venue, Side.BUY, 200)
    assert int(venue.fees_collected) == 0
    assert FEE_ACCOUNT_ID not in venue.accounts
    assert venue.conservation_check() == 0


# --------------------------------------------------------------------------
# Post-only
# --------------------------------------------------------------------------


def _post_only(venue: Venue, side: Side, price: int, quantity: int = 50):
    return venue.submit(
        MAKER,
        "F",
        Submit(MAKER, side, Quantity(quantity), Price(price), OrderType.LIMIT,
               TimeInForce.POST_ONLY),
    )


def test_a_post_only_order_that_would_cross_is_rejected():
    venue = _venue()
    _rest(venue, Side.SELL, 18_800, 100, who=TAKER)
    events = _post_only(venue, Side.BUY, 18_900)
    rejected = next(e for e in events if isinstance(e, Rejected))
    assert rejected.reason is RejectReason.POST_ONLY_WOULD_CROSS
    assert not any(isinstance(e, Filled) for e in events)


def test_a_post_only_order_that_does_not_cross_rests_normally():
    venue = _venue()
    _rest(venue, Side.SELL, 18_800, 100, who=TAKER)
    _post_only(venue, Side.BUY, 18_700)
    book = venue.engine("F").book
    assert int(book.best_price(Side.BUY)) == 18_700


def test_a_post_only_market_order_is_a_contradiction_and_is_refused():
    """A market order is defined by being willing to cross."""
    venue = _venue()
    events = venue.submit(
        MAKER,
        "F",
        Submit(MAKER, Side.BUY, Quantity(10), None, OrderType.MARKET,
               TimeInForce.POST_ONLY),
    )
    rejected = next(e for e in events if isinstance(e, Rejected))
    assert rejected.reason is RejectReason.MARKET_ORDER_MUST_BE_IOC


def test_post_only_guarantees_the_rebate_rather_than_the_fee():
    """The reason the order type exists, measured end to end.

    A maker that crosses by accident pays the taker fee instead of earning the
    rebate, which can turn a profitable quote into a losing one. Post-only makes
    that outcome unreachable.
    """
    venue = _venue()
    _rest(venue, Side.SELL, 18_800, 300, who=TAKER)
    opening = int(venue.account(MAKER).cash)

    # Refused, so no fee either way.
    _post_only(venue, Side.BUY, 18_900)
    assert int(venue.account(MAKER).cash) == opening

    # Rests, gets hit, earns the rebate.
    _post_only(venue, Side.BUY, 18_700)
    venue.submit(
        TAKER,
        "F",
        Submit(TAKER, Side.SELL, Quantity(50), None, OrderType.MARKET, TimeInForce.IOC),
    )
    assert int(venue.account(MAKER).cash) > opening
