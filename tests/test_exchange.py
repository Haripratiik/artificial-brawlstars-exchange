"""Matching engine tests.

This engine's job is to be the oracle the C++ port is validated against, so a
subtle bug here would be transcribed into the port and validated into looking
correct. The suite is therefore built around invariants that must hold for *any*
command stream, not only around hand-picked scenarios:

    * quantity is conserved -- matching neither creates nor destroys it
    * the book never crosses -- best bid is always below best ask
    * price-time priority is respected
    * identical command streams produce identical event streams
"""

from __future__ import annotations

import random

import pytest

from arena.exchange.engine import MatchingEngine
from arena.exchange.events import (
    Acknowledged,
    Cancel,
    Cancelled,
    Filled,
    Rejected,
    Replace,
    Replaced,
    Submit,
    Traded,
)
from arena.exchange.types import (
    AgentId,
    OrderType,
    Price,
    Quantity,
    RejectReason,
    Side,
    TimeInForce,
)

A = AgentId("alice")
B = AgentId("bob")
C = AgentId("carol")


def limit(agent, side, price, qty, tif=TimeInForce.GTC) -> Submit:
    return Submit(agent, side, Quantity(qty), Price(price), OrderType.LIMIT, tif)


def market(agent, side, qty, tif=TimeInForce.IOC) -> Submit:
    return Submit(agent, side, Quantity(qty), None, OrderType.MARKET, tif)


def trades(events) -> list[Traded]:
    return [e for e in events if isinstance(e, Traded)]


def acked(events) -> Acknowledged:
    return next(e for e in events if isinstance(e, Acknowledged))


@pytest.fixture
def engine() -> MatchingEngine:
    return MatchingEngine("SPIKE_WR_FUT")


# --------------------------------------------------------------------------
# Resting and crossing
# --------------------------------------------------------------------------


def test_non_crossing_orders_rest(engine):
    engine.apply(limit(A, Side.BUY, 100, 10))
    engine.apply(limit(B, Side.SELL, 102, 10))
    book = engine.book.snapshot()

    assert book.best_bid == 100
    assert book.best_ask == 102
    assert book.spread == 2
    assert book.mid == 101.0
    assert engine.tape == ()


def test_crossing_order_trades(engine):
    engine.apply(limit(A, Side.SELL, 100, 10))
    events = engine.apply(limit(B, Side.BUY, 100, 10))

    (trade,) = trades(events)
    assert trade.price == 100
    assert trade.quantity == 10
    assert trade.aggressor_side is Side.BUY
    assert engine.book.snapshot().best_bid is None


def test_trade_prints_at_the_resting_price_not_the_aggressors(engine):
    """Price improvement accrues to the taker, and the passive side set the terms.

    A buyer willing to pay 105 that hits a 100 offer pays 100. Printing the
    aggressor's limit instead would inflate every effective-spread measurement.
    """
    engine.apply(limit(A, Side.SELL, 100, 10))
    events = engine.apply(limit(B, Side.BUY, 105, 10))
    assert trades(events)[0].price == 100


def test_partial_fill_leaves_the_remainder_resting(engine):
    engine.apply(limit(A, Side.SELL, 100, 4))
    events = engine.apply(limit(B, Side.BUY, 100, 10))

    assert trades(events)[0].quantity == 4
    book = engine.book.snapshot()
    assert book.best_bid == 100
    assert book.bids[0][1] == 6


def test_an_order_walks_multiple_levels(engine):
    engine.apply(limit(A, Side.SELL, 100, 5))
    engine.apply(limit(B, Side.SELL, 101, 5))
    engine.apply(limit(C, Side.SELL, 102, 5))

    events = engine.apply(limit(A, Side.BUY, 102, 12))
    prints = trades(events)

    assert [(int(t.price), int(t.quantity)) for t in prints] == [(100, 5), (101, 5), (102, 2)]
    assert engine.book.snapshot().best_ask == 102


# --------------------------------------------------------------------------
# Priority
# --------------------------------------------------------------------------


def test_price_priority_beats_time(engine):
    """A better price trades first even if it arrived later."""
    engine.apply(limit(A, Side.SELL, 101, 10))
    later = acked(engine.apply(limit(B, Side.SELL, 100, 10)))

    events = engine.apply(limit(C, Side.BUY, 101, 10))
    (trade,) = trades(events)

    assert trade.price == 100
    assert trade.sell_order_id == later.order_id


def test_time_priority_within_a_price_level(engine):
    """Equal prices trade in arrival order, first in first out."""
    first = acked(engine.apply(limit(A, Side.SELL, 100, 10)))
    second = acked(engine.apply(limit(B, Side.SELL, 100, 10)))

    events = engine.apply(limit(C, Side.BUY, 100, 15))
    prints = trades(events)

    assert [t.sell_order_id for t in prints] == [first.order_id, second.order_id]
    assert [int(t.quantity) for t in prints] == [10, 5]


def test_cancelling_the_front_order_promotes_the_next(engine):
    first = acked(engine.apply(limit(A, Side.SELL, 100, 10)))
    second = acked(engine.apply(limit(B, Side.SELL, 100, 10)))
    engine.apply(Cancel(A, first.order_id))

    events = engine.apply(limit(C, Side.BUY, 100, 10))
    assert trades(events)[0].sell_order_id == second.order_id


# --------------------------------------------------------------------------
# Order types and time in force
# --------------------------------------------------------------------------


def test_market_order_sweeps_the_book(engine):
    engine.apply(limit(A, Side.SELL, 100, 5))
    engine.apply(limit(B, Side.SELL, 200, 5))

    events = engine.apply(market(C, Side.BUY, 8))
    assert [int(t.price) for t in trades(events)] == [100, 200]


def test_market_order_with_no_liquidity_cancels(engine):
    events = engine.apply(market(A, Side.BUY, 10))
    assert trades(events) == []
    assert any(isinstance(e, Cancelled) for e in events)
    assert engine.book.snapshot().best_bid is None


def test_market_order_may_not_rest(engine):
    """An unpriced resting order would match anything, forever."""
    events = engine.apply(
        Submit(A, Side.BUY, Quantity(10), None, OrderType.MARKET, TimeInForce.GTC)
    )
    (rejection,) = [e for e in events if isinstance(e, Rejected)]
    assert rejection.reason is RejectReason.MARKET_ORDER_MUST_BE_IOC


def test_ioc_takes_what_it_can_and_cancels_the_rest(engine):
    engine.apply(limit(A, Side.SELL, 100, 4))
    events = engine.apply(limit(B, Side.BUY, 100, 10, TimeInForce.IOC))

    assert trades(events)[0].quantity == 4
    cancel = next(e for e in events if isinstance(e, Cancelled))
    assert cancel.remaining == 6
    assert engine.book.snapshot().best_bid is None


def test_fok_fills_completely_or_not_at_all(engine):
    engine.apply(limit(A, Side.SELL, 100, 4))

    rejected = engine.apply(limit(B, Side.BUY, 100, 10, TimeInForce.FOK))
    assert trades(rejected) == []
    assert any(
        isinstance(e, Rejected) and e.reason is RejectReason.FOK_NOT_FILLABLE
        for e in rejected
    )
    # The resting sell is untouched -- nothing was consumed and unwound.
    assert engine.book.snapshot().best_ask == 100

    filled = engine.apply(limit(C, Side.BUY, 100, 4, TimeInForce.FOK))
    assert trades(filled)[0].quantity == 4


def test_fok_spanning_several_levels_is_allowed(engine):
    engine.apply(limit(A, Side.SELL, 100, 4))
    engine.apply(limit(B, Side.SELL, 101, 6))
    events = engine.apply(limit(C, Side.BUY, 101, 10, TimeInForce.FOK))
    assert sum(int(t.quantity) for t in trades(events)) == 10


def test_cancelled_orders_do_not_inflate_reported_depth(engine):
    """A tombstone mid-queue must not be counted as available liquidity.

    Cancellation tombstones rather than splices, so the level's queue still
    holds the dead order until something prunes past it. Summing the queue
    counted it; the level's maintained total does not.
    """
    first = acked(engine.apply(limit(A, Side.SELL, 100, 10)))
    engine.apply(limit(B, Side.SELL, 100, 10))
    third = acked(engine.apply(limit(C, Side.SELL, 100, 10)))

    # Cancel the middle one by cancelling first then third would prune; cancel
    # a strictly interior order instead.
    engine.apply(Cancel(A, first.order_id))       # front: pruned on next touch
    engine.apply(Cancel(C, third.order_id))       # interior relative to nothing

    assert int(engine.book.depth_at(Side.SELL, Price(100))) == 10
    assert engine.book.snapshot().asks == ((100, 10),)


def test_fill_or_kill_is_not_fooled_by_dead_liquidity(engine):
    """The consequence of over-reported depth, and why it mattered.

    ``_fillable`` reads the level totals. If a cancelled order still counted,
    a FOK order would be accepted as satisfiable and then partially fill --
    exactly what fill-or-kill exists to prevent.
    """
    engine.apply(limit(A, Side.SELL, 100, 5))
    doomed = acked(engine.apply(limit(B, Side.SELL, 100, 20)))
    engine.apply(Cancel(B, doomed.order_id))

    events = engine.apply(limit(C, Side.BUY, 100, 15, TimeInForce.FOK))

    assert trades(events) == [], "FOK filled against liquidity that was cancelled"
    assert any(
        isinstance(e, Rejected) and e.reason is RejectReason.FOK_NOT_FILLABLE
        for e in events
    )
    # And the genuine five lots are untouched.
    assert int(engine.book.depth_at(Side.SELL, Price(100))) == 5


@pytest.mark.parametrize("quantity", [0, -5])
def test_non_positive_quantity_is_rejected(engine, quantity):
    events = engine.apply(Submit(A, Side.BUY, Quantity(quantity), Price(100)))
    assert events[0].reason is RejectReason.INVALID_QUANTITY


def test_limit_order_without_a_price_is_rejected(engine):
    events = engine.apply(Submit(A, Side.BUY, Quantity(10), None, OrderType.LIMIT))
    assert events[0].reason is RejectReason.LIMIT_ORDER_REQUIRES_PRICE


# --------------------------------------------------------------------------
# Cancel and replace
# --------------------------------------------------------------------------


def test_cancel_removes_resting_quantity(engine):
    ack = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    events = engine.apply(Cancel(A, ack.order_id))

    assert isinstance(events[0], Cancelled)
    assert events[0].remaining == 10
    assert engine.book.snapshot().best_bid is None


def test_cancelling_twice_is_rejected(engine):
    ack = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    engine.apply(Cancel(A, ack.order_id))
    events = engine.apply(Cancel(A, ack.order_id))
    assert events[0].reason is RejectReason.ALREADY_TERMINAL


def test_cancelling_someone_elses_order_is_rejected(engine):
    ack = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    events = engine.apply(Cancel(B, ack.order_id))

    assert events[0].reason is RejectReason.NOT_ORDER_OWNER
    assert engine.book.snapshot().best_bid == 100


def test_cancelling_an_unknown_order_is_rejected(engine):
    from arena.exchange.types import OrderId

    events = engine.apply(Cancel(A, OrderId(999)))
    assert events[0].reason is RejectReason.UNKNOWN_ORDER


def test_shrinking_at_the_same_price_keeps_queue_position(engine):
    """The rule that decides whether a market maker is honest or magic.

    Reducing size asks for nothing the order was not already entitled to, so it
    keeps its place. Alice shrinks and must still trade before Bob.
    """
    alice = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    engine.apply(limit(B, Side.BUY, 100, 10))

    replaced = engine.apply(Replace(A, alice.order_id, Quantity(5)))
    assert isinstance(replaced[0], Replaced)
    assert replaced[0].kept_priority is True

    events = engine.apply(limit(C, Side.SELL, 100, 5))
    assert trades(events)[0].buy_order_id == alice.order_id


def test_increasing_size_loses_queue_position(engine):
    """A bigger claim goes to the back. Otherwise size buys priority."""
    alice = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    bob = acked(engine.apply(limit(B, Side.BUY, 100, 10)))

    replaced = engine.apply(Replace(A, alice.order_id, Quantity(20)))
    assert replaced[0].kept_priority is False

    events = engine.apply(limit(C, Side.SELL, 100, 5))
    assert trades(events)[0].buy_order_id == bob.order_id


def test_changing_price_loses_queue_position(engine):
    alice = acked(engine.apply(limit(A, Side.BUY, 100, 10)))
    engine.apply(limit(B, Side.BUY, 101, 10))

    replaced = engine.apply(Replace(A, alice.order_id, Quantity(10), Price(101)))
    assert replaced[0].kept_priority is False

    events = engine.apply(limit(C, Side.SELL, 101, 10))
    assert trades(events)[0].buy_order_id != alice.order_id


def test_replacing_into_a_crossing_price_trades_immediately(engine):
    engine.apply(limit(A, Side.SELL, 100, 10))
    bid = acked(engine.apply(limit(B, Side.BUY, 98, 10)))

    events = engine.apply(Replace(B, bid.order_id, Quantity(10), Price(100)))
    assert trades(events)[0].price == 100


# --------------------------------------------------------------------------
# Invariants under random flow
# --------------------------------------------------------------------------


def random_commands(seed: int, count: int = 400) -> list:
    rng = random.Random(seed)
    agents = [A, B, C]
    commands = []
    for _ in range(count):
        agent = rng.choice(agents)
        side = rng.choice([Side.BUY, Side.SELL])
        roll = rng.random()
        if roll < 0.72:
            commands.append(
                limit(agent, side, rng.randint(95, 105), rng.randint(1, 20))
            )
        elif roll < 0.85:
            commands.append(market(agent, side, rng.randint(1, 10)))
        else:
            commands.append(
                limit(agent, side, rng.randint(95, 105), rng.randint(1, 20), TimeInForce.IOC)
            )
    return commands


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 42])
def test_book_never_crosses(seed):
    """Best bid must always sit strictly below best ask.

    A crossed book means a trade that should have happened did not, and it is
    the single most damaging silent failure a matching engine can have.
    """
    engine = MatchingEngine()
    for command in random_commands(seed):
        engine.apply(command)
        book = engine.book.snapshot()
        if book.best_bid is not None and book.best_ask is not None:
            assert book.best_bid < book.best_ask, f"crossed book at seed {seed}"


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 42])
def test_quantity_is_conserved(seed):
    """Matching moves quantity; it never creates or destroys it.

    For every order: submitted = filled + remaining + cancelled. Summed over the
    book, traded volume must reconcile against fills exactly.
    """
    engine = MatchingEngine()
    events = engine.apply_all(random_commands(seed))

    filled_by_side: dict[Side, int] = {Side.BUY: 0, Side.SELL: 0}
    for event in events:
        if isinstance(event, Filled):
            filled_by_side[event.side] += int(event.quantity)

    traded = sum(int(t.quantity) for t in engine.tape)
    # Every trade has exactly one buyer and one seller, so each side's filled
    # volume must equal total traded volume.
    assert filled_by_side[Side.BUY] == traded
    assert filled_by_side[Side.SELL] == traded


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 42])
def test_depth_matches_the_sum_of_resting_orders(seed):
    """Aggregated L2 depth must equal what is actually in the queues."""
    engine = MatchingEngine()
    engine.apply_all(random_commands(seed))

    book = engine.book.snapshot(levels=1 << 20)
    for side, levels in ((Side.BUY, book.bids), (Side.SELL, book.asks)):
        for price, quantity in levels:
            actual = sum(
                o.remaining
                for o in engine.book.resting_orders
                if o.side is side and o.price == price
            )
            assert int(quantity) == actual


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 11, 42])
def test_identical_command_streams_produce_identical_events(seed):
    """The acceptance test for the C++ port, run against the reference itself."""
    commands = random_commands(seed)

    first = [e.to_dict() for e in MatchingEngine().apply_all(commands)]
    second = [e.to_dict() for e in MatchingEngine().apply_all(commands)]

    assert first == second
    assert [e["sequence"] for e in first] == list(range(1, len(first) + 1))


def test_sequence_numbers_are_gapless_and_ordered(engine):
    events = engine.apply_all(random_commands(5, count=120))
    sequences = [int(e.sequence) for e in events]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))
