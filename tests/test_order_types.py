"""Orders that hide, and orders that wait.

The exchange offered limit and market, with GTC, IOC, FOK and post-only. A
production venue offers twenty or more, and two of the missing ones are missing
in a way that changes what the market *is* rather than what it can express:

**Iceberg orders** trade visibility for queue priority. Size is information --
an order for ten thousand lots announces what you are doing before you have
done any of it -- so it is worked in slices, and each refreshed slice goes to
the back of its level behind everything that arrived while the last one worked.
docs/GAPS.md recorded them as "absent, and they change queue dynamics
materially", which is exactly right and is why they are worth having.

**Stop orders** wait for a price before they exist. They are the classic risk
tool and the classic accelerant: a stop sells into a fall, which pushes the
price down, which triggers more stops. Nothing here prevents that cascade and
nothing should -- being able to *measure* one is most of the reason to model
stops at all.
"""

from __future__ import annotations

import pytest

from arena.exchange.engine import MatchingEngine
from arena.exchange.events import Submit, Traded
from arena.exchange.types import (
    AgentId,
    OrderType,
    Price,
    Quantity,
    RejectReason,
    Side,
    TimeInForce,
)


def _limit(engine, who, side, quantity, price, **kwargs):
    return engine.apply(
        Submit(AgentId(who), side, Quantity(quantity), Price(price), **kwargs)
    )


def _market(engine, who, side, quantity):
    return engine.apply(
        Submit(
            AgentId(who),
            side,
            Quantity(quantity),
            None,
            OrderType.MARKET,
            TimeInForce.IOC,
        )
    )


def _stop(engine, who, side, quantity, trigger, limit=None):
    return engine.apply(
        Submit(
            AgentId(who),
            side,
            Quantity(quantity),
            None if limit is None else Price(limit),
            OrderType.STOP if limit is None else OrderType.STOP_LIMIT,
            TimeInForce.GTC,
            stop_price=Price(trigger),
        )
    )


def _prints(events):
    return [(int(e.quantity), int(e.price)) for e in events if isinstance(e, Traded)]


# --------------------------------------------------------------------------
# Iceberg
# --------------------------------------------------------------------------


def test_an_iceberg_shows_only_its_slice():
    engine = MatchingEngine()
    _limit(engine, "berg", Side.SELL, 100, 50, display_size=10)
    assert engine.book.snapshot().asks == ((Price(50), Quantity(10)),)


def test_a_refreshed_slice_goes_to_the_back_of_the_queue():
    """The price of hiding, and the whole reason it is not simply better.

    A venue that refreshed in place would let one participant hold the front of
    a queue indefinitely while showing a single lot.
    """
    engine = MatchingEngine()
    _limit(engine, "berg", Side.SELL, 100, 50, display_size=10)
    _limit(engine, "plain", Side.SELL, 20, 50)

    events = _market(engine, "buyer", Side.BUY, 25)
    assert _prints(events) == [(10, 50), (15, 50)]

    resting = {o.agent_id: (int(o.remaining), int(o.shown)) for o in engine.book.resting_orders}
    assert resting["berg"] == (90, 10), "the iceberg refreshed"
    assert resting["plain"] == (5, 5), "the order behind it got its turn"


def test_an_iceberg_hides_its_reserve_from_the_depth():
    engine = MatchingEngine()
    _limit(engine, "berg", Side.SELL, 100, 50, display_size=10)
    _market(engine, "buyer", Side.BUY, 10)
    # Ninety still to sell, ten of it visible. Both numbers are true and they
    # are different questions: the depth is what the market can see, and the
    # resting quantity is what is really there.
    assert engine.book.snapshot().asks == ((Price(50), Quantity(10)),)
    assert engine.book.total_resting_quantity == 90


def test_an_iceberg_fills_completely_if_you_keep_taking():
    engine = MatchingEngine()
    _limit(engine, "berg", Side.SELL, 100, 50, display_size=10)
    taken = 0
    for _ in range(10):
        taken += sum(q for q, _p in _prints(_market(engine, "buyer", Side.BUY, 10)))
    assert taken == 100
    assert not engine.book.resting_orders


def test_an_order_with_no_price_cannot_hide():
    """It never rests, so there is no queue for a reserve to wait in."""
    engine = MatchingEngine()
    events = engine.apply(
        Submit(
            AgentId("a"),
            Side.SELL,
            Quantity(50),
            None,
            OrderType.MARKET,
            TimeInForce.IOC,
            display_size=10,
        )
    )
    assert any(
        getattr(e, "reason", None) is RejectReason.INVALID_QUANTITY for e in events
    )


# --------------------------------------------------------------------------
# Stops
# --------------------------------------------------------------------------


def test_a_stop_is_not_liquidity_and_does_not_appear_as_any():
    """Publishing one would say exactly where the market must go to set off a
    cascade, which is the thing its owner most wants kept quiet."""
    engine = MatchingEngine()
    _limit(engine, "mm", Side.BUY, 20, 100)
    _stop(engine, "a", Side.SELL, 30, trigger=99)

    book = engine.book.snapshot()
    assert book.asks == ()
    assert book.bids == ((Price(100), Quantity(20)),)


def test_a_stop_stays_asleep_until_the_market_reaches_it():
    engine = MatchingEngine()
    _limit(engine, "mm", Side.BUY, 20, 100)
    _stop(engine, "a", Side.SELL, 30, trigger=90)

    _market(engine, "seller", Side.SELL, 5)
    assert len(engine._stops) == 1, "a print at 100 woke a stop set at 90"


def test_a_triggered_stop_trades():
    engine = MatchingEngine()
    for price, quantity in ((100, 20), (99, 20), (98, 20)):
        _limit(engine, "mm", Side.BUY, quantity, price)
    _stop(engine, "a", Side.SELL, 30, trigger=99)

    events = _market(engine, "seller", Side.SELL, 25)
    prints = _prints(events)
    assert prints[:2] == [(20, 100), (5, 99)], "the order that triggered it"
    assert sum(q for q, _p in prints[2:]) == 30, "the stop's own thirty lots"
    assert not engine._stops


def test_one_stop_sets_off_another():
    """A cascade, which is a real thing markets do and this one does not prevent."""
    engine = MatchingEngine()
    for price, quantity in ((100, 20), (99, 20), (98, 20), (97, 20), (96, 60)):
        _limit(engine, "mm", Side.BUY, quantity, price)
    _stop(engine, "a", Side.SELL, 30, trigger=99)
    _stop(engine, "b", Side.SELL, 30, trigger=98)

    events = _market(engine, "seller", Side.SELL, 25)
    assert not engine._stops, "both stops should have gone off"
    assert engine.cascade_depth, "nothing recorded a cascade"
    assert engine.cascade_depth[-1] >= 2, (
        f"the second stop was not set off by the first: {engine.cascade_depth}"
    )
    # Everything that was sold: the original order plus both stops.
    assert sum(q for q, _p in _prints(events)) == 25 + 30 + 30


def test_a_cascade_cannot_run_forever():
    """A chain that never ends is a bug in the model, not an event in a market."""
    engine = MatchingEngine()
    assert engine._max_cascade > 0
    for price in range(200, 100, -1):
        _limit(engine, "mm", Side.BUY, 5, price)
        _stop(engine, f"s{price}", Side.SELL, 5, trigger=price)
    _market(engine, "seller", Side.SELL, 5)
    assert engine.cascade_depth[-1] <= engine._max_cascade


def test_a_stop_limit_will_not_fill_below_its_limit():
    """Which is the trade every stop user actually faces: protection against a
    bad fill, paid for with the risk of no fill at all."""
    engine = MatchingEngine()
    _limit(engine, "mm", Side.BUY, 20, 100)
    _limit(engine, "mm", Side.BUY, 20, 90)
    _stop(engine, "a", Side.SELL, 30, trigger=99, limit=95)

    _market(engine, "seller", Side.SELL, 25)
    prints = _prints(engine.apply(Submit(AgentId("noop"), Side.BUY, Quantity(1), Price(1))))
    resting = [o for o in engine.book.resting_orders if o.agent_id == "a"]
    assert resting, "the stop-limit did not rest"
    assert all(int(o.price) == 95 for o in resting)
    assert engine.book.snapshot().bids[0][0] == Price(90), (
        "it should not have sold into the 90 bid"
    )


def test_a_stop_needs_a_trigger_and_nothing_else_may_have_one():
    engine = MatchingEngine()
    missing = engine.apply(
        Submit(AgentId("a"), Side.SELL, Quantity(5), None, OrderType.STOP, TimeInForce.GTC)
    )
    assert any(
        getattr(e, "reason", None) is RejectReason.INVALID_STOP_PRICE for e in missing
    )

    spurious = engine.apply(
        Submit(
            AgentId("a"),
            Side.SELL,
            Quantity(5),
            Price(100),
            OrderType.LIMIT,
            TimeInForce.GTC,
            stop_price=Price(99),
        )
    )
    assert any(
        getattr(e, "reason", None) is RejectReason.INVALID_STOP_PRICE for e in spurious
    )


def test_a_stop_cannot_be_immediate():
    """"Do this now" and "do this later" are contradictory instructions."""
    engine = MatchingEngine()
    events = engine.apply(
        Submit(
            AgentId("a"),
            Side.SELL,
            Quantity(5),
            None,
            OrderType.STOP,
            TimeInForce.IOC,
            stop_price=Price(99),
        )
    )
    assert any(getattr(e, "reason", None) is not None for e in events)


def test_the_venue_reserves_for_a_stop_the_moment_it_is_parked():
    """The engine releases a triggered stop inside its own matching, which never
    passes back through the collateral check. An unreserved stop would create a
    position the account was never asked to cover."""
    from arena.exchange.session import SessionState
    from arena.market.live import HUMAN_ID
    from arena.market.venue import SymbolCommand
    from arena.sim.time import Timestamp, seconds
    from dashboard.build_market import build

    symbol = "SPIKE_WR_FUT"
    market = build(seed=7, human_cash=4_000_000)
    market.kernel.start()
    market.kernel.advance(until=seconds(180))
    for moment in range(185, 400, 5):
        market.kernel.advance(until=seconds(moment))
        book = market.venue.engine(symbol).book.snapshot()
        if (
            market.venue.session(symbol) is SessionState.CONTINUOUS
            and book.best_bid is not None
        ):
            break

    instrument = market.venue.registry.require(symbol)
    trigger = float(instrument.from_ticks(book.best_bid)) - 400.0
    market.submit(symbol, "sell", 10, None, stop=f"{trigger:.2f}", trader=None)
    market.kernel.advance(until=Timestamp(int(market.kernel.now) + int(seconds(1))))

    working = market.venue._working.get((HUMAN_ID, symbol), {})
    assert working, "a parked stop reserved nothing"
    assert int(market.venue.conservation_check()) == 0
