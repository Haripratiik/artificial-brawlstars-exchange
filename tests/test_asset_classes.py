"""Every asset class, and the venue lifecycle around it.

The four canonical instruments -- future, event contract, spread, index -- are
only the combinations someone thought to try. The underlying algebra composes
freely, so this exercises the combinations nobody designed for: a binary written
on a spread, a spread of spreads, an index of spreads, a long/short basket, an
inverse future. Each one has to price, trade, mark, collateralise and settle
without special-casing, or the algebra is not really an algebra.

The lifecycle tests cover the second half of the question: what happens around
the edges of a contract's life. Three real bugs were found here -- a replace that
was never collateral-checked, a replace that left the venue reserving against
stale values, and working-order state that survived settlement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from arena.contracts.payoff import Binary, Call, Linear, Put
from arena.contracts.spec import ContractSpec, DataPolicy, ObservationWindow
from arena.contracts.underlying import Basket, Difference, MetricRef, Single
from arena.exchange.events import Replace, Submit
from arena.exchange.types import (
    AgentId,
    OrderType,
    Quantity,
    RejectReason,
    Side,
    TimeInForce,
)
from arena.market.instrument import Instrument, InstrumentClass
from arena.market.venue import Venue
from arena.settlement.result import SettlementResult, SettlementStatus

UTC = timezone.utc
WINDOW = ObservationWindow(
    datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
)
A, B = AgentId("a"), AgentId("b")
SYM = "SYM"


def wr(subject: str, bounds=(0.0, 1.0)) -> Single:
    return Single(MetricRef("adjusted_win_rate", subject, bounds=bounds))


def make(underlying, payoff, tick="0.25") -> Instrument:
    spec = ContractSpec(
        contract_id=SYM,
        underlying=underlying,
        payoff=payoff,
        window=WINDOW,
        policy=DataPolicy(min_sample_size=1),
        reference_id="ref-1",
        published_at=WINDOW.start - timedelta(days=1),
        tick_size=tick,
    )
    return Instrument(SYM, spec)


def venue_with(instrument: Instrument, cash: int = 100_000_000) -> Venue:
    venue = Venue(starting_cash=cash)
    venue.list_instrument(instrument)
    return venue


def order(agent, side, ticks, quantity) -> Submit:
    return Submit(agent, side, Quantity(quantity), ticks, OrderType.LIMIT, TimeInForce.GTC)


# --------------------------------------------------------------------------
# The algebra composes
# --------------------------------------------------------------------------

COMBINATIONS = {
    "future": (wr("A"), Linear(10_000.0), "0.25", (D("0"), D("10000"))),
    "event": (wr("A"), Binary(">", 0.5), "0.01", (D("0"), D("1"))),
    "spread": (
        Difference(wr("A"), wr("B")),
        Linear(10_000.0),
        "0.25",
        (D("-10000"), D("10000")),
    ),
    "index": (
        Basket(((wr("A"), 0.5), (wr("B"), 0.5))),
        Linear(10_000.0),
        "0.25",
        (D("0"), D("10000")),
    ),
    "event on a spread": (
        Difference(wr("A"), wr("B")),
        Binary(">", 0.0),
        "0.01",
        (D("0"), D("1")),
    ),
    "spread of spreads": (
        Difference(Difference(wr("A"), wr("B")), Difference(wr("C"), wr("D"))),
        Linear(10_000.0),
        "0.25",
        (D("-20000"), D("20000")),
    ),
    "index of spreads": (
        Basket(
            ((Difference(wr("A"), wr("B")), 0.5), (Difference(wr("C"), wr("D")), 0.5))
        ),
        Linear(10_000.0),
        "0.25",
        (D("-10000"), D("10000")),
    ),
    "long/short basket": (
        Basket(((wr("A"), 1.0), (wr("B"), -1.0))),
        Linear(10_000.0),
        "0.25",
        (D("-10000"), D("10000")),
    ),
    "inverse future": (wr("A"), Linear(-10_000.0), "0.25", (D("-10000"), D("0"))),
    "call option": (
        wr("A"),
        Call(5_000.0, 10_000.0),
        "0.25",
        (D("0"), D("5000")),
    ),
    "put option": (
        wr("A"),
        Put(5_000.0, 10_000.0),
        "0.25",
        (D("0"), D("5000")),
    ),
    "future with offset": (
        wr("A"),
        Linear(10_000.0, offset=5_000.0),
        "0.25",
        (D("5000"), D("15000")),
    ),
}


@pytest.mark.parametrize("name", sorted(COMBINATIONS))
def test_bounds_propagate_through_the_algebra(name):
    """Interval arithmetic has to survive nesting and sign changes.

    The two traps: a difference inverts its right operand, and a negative weight
    or scale flips its interval. Either mistake understates the range, and an
    understated range under-collateralises every short in the instrument.
    """
    underlying, payoff, tick, expected = COMBINATIONS[name]
    assert make(underlying, payoff, tick).settlement_bounds == expected


@pytest.mark.parametrize("name", sorted(COMBINATIONS))
def test_every_class_trades_marks_and_settles(name):
    """One mechanism, no special cases, all the way through to cash."""
    underlying, payoff, tick, (low, high) = COMBINATIONS[name]
    instrument = make(underlying, payoff, tick)
    venue = venue_with(instrument)

    # Trade at a price a quarter of the way up the range, snapped to the grid.
    raw = low + (high - low) / 4
    step = instrument.tick_size
    price = (raw / step).to_integral_value() * step
    ticks = instrument.to_ticks(price)

    venue.submit(A, SYM, order(A, Side.SELL, ticks, 10))
    events = venue.submit(B, SYM, order(B, Side.BUY, ticks, 10))

    assert any(type(e).__name__ == "Traded" for e in events), f"{name} did not trade"
    assert venue.mark_price(SYM) == price
    assert venue.account(B).positions[SYM].quantity == 10
    assert venue.account(A).positions[SYM].quantity == -10

    settlement = (high / step).to_integral_value() * step
    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, settlement, 0.0, ()
    )
    realised = venue.settle(SYM, result)

    # The long gains exactly what the short loses, and nothing leaks.
    assert int(realised[A]) == -int(realised[B])
    assert venue.conservation_check() == 0
    assert all(int(a.posted_collateral) == 0 for a in venue.accounts.values())


OPTION_STRIKE = 5_000.0
OPTION_SCALE = 10_000.0


def test_options_are_classified_and_bounded():
    call = make(wr("A"), Call(OPTION_STRIKE, OPTION_SCALE))
    put = make(wr("A"), Put(OPTION_STRIKE, OPTION_SCALE))

    assert call.instrument_class == InstrumentClass.CALL
    assert put.instrument_class == InstrumentClass.PUT
    # Floored at zero by structure, capped by the best the underlying can do.
    assert call.settlement_bounds == (D("0"), D("5000"))
    assert put.settlement_bounds == (D("0"), D("5000"))


@pytest.mark.parametrize("level", [0.0, 0.1, 0.3, 0.4669, 0.5, 0.5001, 0.75, 1.0])
def test_put_call_parity_holds_exactly(level):
    """``C - P = F - K``, with no discount factor and no approximation.

    Both legs settle from the same metric at the same instant, so parity is an
    identity here rather than a no-arbitrage relationship that holds to within
    financing costs. If it ever fails, one of the two payoffs is wrong.
    """
    call = Call(OPTION_STRIKE, OPTION_SCALE).apply(level)
    put = Put(OPTION_STRIKE, OPTION_SCALE).apply(level)
    future = Linear(OPTION_SCALE).apply(level)

    assert call - put == pytest.approx(future - OPTION_STRIKE)


def test_an_option_never_settles_negative():
    """The whole point of buying one: downside bounded by structure."""
    for level in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert Call(OPTION_STRIKE, OPTION_SCALE).apply(level) >= 0
        assert Put(OPTION_STRIKE, OPTION_SCALE).apply(level) >= 0


@pytest.mark.parametrize("payoff_name", ["call", "put"])
def test_options_trade_and_settle(payoff_name):
    payoff = (
        Call(OPTION_STRIKE, OPTION_SCALE)
        if payoff_name == "call"
        else Put(OPTION_STRIKE, OPTION_SCALE)
    )
    instrument = make(wr("A"), payoff)
    venue = venue_with(instrument)
    ticks = instrument.to_ticks(D("400"))  # a plausible premium

    venue.submit(A, SYM, order(A, Side.SELL, ticks, 10))
    events = venue.submit(B, SYM, order(B, Side.BUY, ticks, 10))
    assert any(type(e).__name__ == "Traded" for e in events)

    # A long option's worst case is exactly the premium paid -- it expires
    # worthless, and cannot do worse than that.
    assert venue.account(B).collateral[SYM] == int(D("400") * 10 * 1_000_000)

    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("0"), 0.0, ()
    )
    venue.settle(SYM, result)
    assert venue.conservation_check() == 0


def test_an_option_on_a_spread_is_an_option():
    """The payoff decides the class, not the shape of what it is written on."""
    instrument = make(
        Difference(wr("A"), wr("B")), Call(0.0, OPTION_SCALE)
    )
    assert instrument.instrument_class == InstrumentClass.CALL
    # A spread ranges over [-1, 1], so a call struck at zero can pay up to 10000.
    assert instrument.settlement_bounds == (D("0"), D("10000"))


def test_negative_prices_trade_correctly():
    """A spread is genuinely negative much of the time.

    Books, marks, collateral and settlement all have to work below zero, which
    is not true of an equity venue and is easy to leave untested.
    """
    instrument = make(Difference(wr("A"), wr("B")), Linear(10_000.0))
    venue = venue_with(instrument)
    ticks = instrument.to_ticks(D("-250"))

    venue.submit(A, SYM, order(A, Side.SELL, ticks, 10))
    venue.submit(B, SYM, order(B, Side.BUY, ticks, 10))

    assert venue.mark_price(SYM) == D("-250")
    # A long at -250 on a floor of -10000 risks only the distance to the floor.
    assert venue.account(B).collateral[SYM] == int(
        D("9750") * 10 * 1_000_000
    )

    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("-57"), 0.0, ()
    )
    realised = venue.settle(SYM, result)
    assert int(realised[B]) > 0  # bought at -250, settled at -57
    assert venue.conservation_check() == 0


# --------------------------------------------------------------------------
# Venue lifecycle
# --------------------------------------------------------------------------


def test_replace_is_collateral_checked():
    """A modification is a request for risk, exactly like an order.

    Guarding only new orders left a hole: work ten lots, then replace them with
    five hundred at a worse price, and take on exposure the account could never
    cover.
    """
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument, cash=100_000)
    ack = venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("4800")), 10))[0]

    events = venue.submit(
        A, SYM, Replace(A, ack.order_id, Quantity(5000), instrument.to_ticks(D("4900")))
    )
    assert events[0].reason is RejectReason.INSUFFICIENT_COLLATERAL
    # And the original order is untouched by the refusal.
    assert list(venue._working[(A, SYM)].values())[0][1] == 10


def test_replace_updates_the_reserved_amount():
    """Stale working-order state reserves against an order that no longer exists."""
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    ack = venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("4800")), 10))[0]

    venue.submit(
        A, SYM, Replace(A, ack.order_id, Quantity(50), instrument.to_ticks(D("4900")))
    )
    side, quantity, price = list(venue._working[(A, SYM)].values())[0]
    assert quantity == 50
    assert price == int(D("4900") * 1_000_000)


def test_a_shrinking_replace_is_still_allowed():
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    ack = venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("4800")), 10))[0]
    events = venue.submit(
        A, SYM, Replace(A, ack.order_id, Quantity(8), instrument.to_ticks(D("4800")))
    )
    assert any(type(e).__name__ == "Replaced" for e in events)


def test_settlement_clears_working_orders():
    """Nothing can be working on a settled contract."""
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("4000")), 10))

    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("5000"), 0.5, ()
    )
    venue.settle(SYM, result)

    assert not venue._working.get((A, SYM))
    assert int(venue.account(A).posted_collateral) == 0
    assert venue.conservation_check() == 0


def test_a_void_returns_everyone_to_where_they_started():
    """Nobody wins or loses when the world produced no evidence."""
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    ticks = instrument.to_ticks(D("4000"))
    venue.submit(A, SYM, order(A, Side.BUY, ticks, 10))
    venue.submit(B, SYM, order(B, Side.SELL, ticks, 10))
    venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("3000")), 5))

    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.VOID, None, None, (), "no data"
    )
    venue.settle(SYM, result)

    for account in venue.accounts.values():
        assert int(account.cash) == int(account.starting_cash)
        assert int(account.posted_collateral) == 0
    assert venue.conservation_check() == 0


def test_a_settled_symbol_cannot_be_traded():
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("5000"), 0.5, ()
    )
    venue.settle(SYM, result)

    events = venue.submit(A, SYM, order(A, Side.BUY, instrument.to_ticks(D("4000")), 1))
    assert events[0].reason is RejectReason.ALREADY_TERMINAL


def test_settling_twice_is_refused():
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("5000"), 0.5, ()
    )
    venue.settle(SYM, result)
    with pytest.raises(ValueError, match="already settled"):
        venue.settle(SYM, result)


def test_an_account_that_never_traded_is_unaffected_by_settlement():
    instrument = make(wr("A"), Linear(10_000.0))
    venue = venue_with(instrument)
    venue.account(B)
    result = SettlementResult(
        SYM, instrument.spec.spec_digest, SettlementStatus.SETTLED, D("5000"), 0.5, ()
    )
    venue.settle(SYM, result)
    assert int(venue.account(B).cash) == int(venue.account(B).starting_cash)


# --------------------------------------------------------------------------
# Commodities: an amount delivered, not a proportion
# --------------------------------------------------------------------------


def test_a_quantity_metric_makes_a_commodity_not_a_future():
    """The class comes from what the contract measures, not from its name.

    A linear claim is a future when it is written on a rate and a commodity
    when it is written on an amount delivered. The metric declares which, so
    the layer doing the classifying never has to know what a Brawler is.
    """
    from dashboard.build_market import instruments

    listed = {i.symbol: i for i in instruments()}
    assert listed["SPIKE_VOL_W1"].instrument_class == "commodity"
    assert listed["SPIKE_WR_FUT"].instrument_class == "future"


def test_a_commodity_has_a_term_structure():
    """Consecutive delivery weeks, differing in nothing else.

    That is what separates a commodity from a future on the same subject: the
    amount delivered in one week is a different thing from the amount delivered
    in the next, so the four of them form a curve rather than four copies.
    """
    from dashboard.build_market import instruments

    rungs = [i for i in instruments() if i.symbol.startswith("SPIKE_VOL_")]
    assert len(rungs) >= 3

    windows = [(i.spec.window.start, i.spec.window.end) for i in rungs]
    assert len(set(windows)) == len(windows), "two rungs measure the same week"
    # Consecutive and non-overlapping: each week begins where the last ended.
    for (_, end), (start, _) in zip(sorted(windows), sorted(windows)[1:]):
        assert end == start, "the delivery weeks leave a gap or overlap"

    # Everything except the window is identical, or they are not one curve.
    payoffs = {i.spec.payoff.to_dict()["kind"] for i in rungs}
    subjects = {i.spec.underlying.ref.subject for i in rungs}
    assert payoffs == {"linear"} and len(subjects) == 1


def test_the_delivery_weeks_settle_at_different_amounts():
    """A curve that is flat by construction would prove nothing."""
    from dashboard.build_market import instruments, true_values

    values = true_values(instruments())
    curve = [values[f"SPIKE_VOL_W{n}"] for n in (1, 2, 3, 4)]
    assert len(set(curve)) > 1, f"the term structure is flat: {curve}"


def test_a_metric_must_declare_whether_it_is_a_rate_or_a_quantity():
    """The distinction decides the asset class, so a typo must not pass."""
    from arena.contracts.underlying import MetricRef

    with pytest.raises(ValueError, match="must be 'rate' or 'quantity'"):
        MetricRef(metric="battle_volume", subject="SPIKE", kind="amount")


def test_volume_is_not_standardized_and_says_so():
    """Reweighting a count onto reference proportions counts nothing.

    Standardization exists to remove the crawler's composition from a *rate*.
    Applied to an amount it produces a number nobody could deliver, so the
    metric declines to do it -- and records that it declined, because the
    consequence is that this measures the corpus rather than the game.
    """
    from arena.worlds.brawl.metrics import METRIC_KINDS, METRICS

    assert METRIC_KINDS["battle_volume"] == "quantity"
    assert "battle_volume" in METRICS
    assert METRICS["battle_volume"].__doc__ is not None
