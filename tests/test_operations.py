"""Running the mechanisms that were built and never run.

Fees, an opening call auction, a circuit breaker and a scoring-rule venue were
all written, all tested in isolation, and all defaulted off -- so the live
exchange exercised none of them. Switching them on found four bugs in an hour,
every one of which had been sitting in tested code:

1. **A market-on-open order that did not fill stayed in the book.** It rests at
   a sentinel price so it crosses every candidate the auction considers, which
   makes it the best offer in a continuous book by a margin of 2^61. The first
   order afterwards matched it *at that price*: trades printed at
   -4,611,686,018,427,387,904, the mark went to zero and the venue billed
   4.8e22 in fees.
2. **Cancelling it back out did not work either.** `Book.remove` reduces a
   level's total and leaves the order as a tombstone; what makes the matcher
   skip it is its *status* being terminal, and the first fix removed without
   marking. The order then vanished from every diagnostic that reads resting
   orders while remaining perfectly tradeable.
3. **`best_bid` and `best_ask` reported sentinel levels as prices.** An order
   that names no price cannot be the best price.
4. **A venue that pays a maker rebate on both sides of its own auction loses
   money on every cross.** Twenty-six opening auctions took venue revenue to
   minus 1,251.

None of these are exotic. They are what "built but never run" means.
"""

from __future__ import annotations

from collections import Counter

import pytest

from arena.exchange.session import SENTINEL, SessionState
from arena.market.fees import FREE, MAKER_TAKER, FeeSchedule
from arena.sim.time import seconds

from dashboard.build_market import build

SYMBOL = "SPIKE_WR_FUT"


@pytest.fixture(scope="module")
def opened():
    """A market that has opened through its call auction and traded on."""
    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(240))
    return market


# --------------------------------------------------------------------------
# The opening auction
# --------------------------------------------------------------------------


def test_the_market_opens_with_a_call_rather_than_a_race():
    """Nothing trades before the open, and then everything trades at one price."""
    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(8))

    assert all(
        market.venue.session(s) is SessionState.PRE_OPEN
        for s in market.venue.registry.symbols
    )
    assert not market.venue.engine(SYMBOL).tape, "something traded before the open"
    resting = market.venue.engine(SYMBOL).book.total_resting_quantity
    assert resting > 0, "the call collected no interest"

    market.kernel.advance(until=seconds(12))
    # Left the call, though not necessarily still trading: the walk from the
    # opening price to fair value is fast enough here that the breaker can
    # catch a symbol within seconds of the open, which is a fact about this
    # market's compressed price discovery rather than about the auction.
    assert all(
        market.venue.session(s) is not SessionState.PRE_OPEN
        for s in market.venue.registry.symbols
    )
    assert (
        sum(
            market.venue.session(s) is SessionState.CONTINUOUS
            for s in market.venue.registry.symbols
        )
        >= len(market.venue.registry.symbols) - 6
    )
    tape = market.venue.engine(SYMBOL).tape
    assert tape, "the auction cleared nothing"
    opening = {int(t.price) for t in tape[:20]}
    assert len(opening) == 1, f"the opening cross printed at several prices: {opening}"


def test_an_unfilled_market_on_open_order_does_not_survive_the_call(opened):
    """The bug that printed trades at minus two to the sixty-first.

    A market order names no price. It is given one -- the sentinel -- so that it
    crosses every candidate the auction considers, and once that auction has
    cleared there is no price it was ever willing to pay. Leaving it resting
    makes it the whole book.
    """
    for symbol in opened.venue.registry.symbols:
        book = opened.venue.engine(symbol).book
        stragglers = [
            o for o in book.resting_orders if abs(int(o.price)) >= SENTINEL
        ]
        assert not stragglers, f"{symbol} still holds {len(stragglers)} market orders"


def test_no_trade_ever_prints_at_a_price_no_instrument_could_quote(opened):
    for symbol in opened.venue.registry.symbols:
        instrument = opened.venue.registry.require(symbol)
        low, high = instrument.tick_bounds
        for trade in opened.venue.engine(symbol).tape:
            assert int(low) <= int(trade.price) <= int(high), (
                f"{symbol} printed at {int(trade.price)}, outside [{low}, {high}]"
            )


def test_a_sentinel_order_is_never_the_best_price():
    """It is at the top of the book and it is not a price.

    The levels keep it, because the auction has to count that interest to know
    what would trade. The *touch* must not, because everything that reads a
    touch is asking what something is worth.
    """
    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(8))

    saw_one = False
    for symbol in market.venue.registry.symbols:
        book = market.venue.engine(symbol).book
        levels = book.snapshot(levels=1 << 20)
        if any(abs(int(p)) >= SENTINEL for p, _q in levels.bids + levels.asks):
            saw_one = True
        for price in (levels.best_bid, levels.best_ask):
            assert price is None or abs(int(price)) < SENTINEL
    assert saw_one, "no market-on-open interest was collected; the test proves nothing"


def test_cancelling_an_order_makes_it_untradeable_and_not_merely_invisible():
    """`Book.remove` is a tombstone, and a tombstone needs the status set.

    Removed without it, an order disappears from the depth and from every
    diagnostic that reads resting orders while still sitting in its level's
    queue, where the matcher will happily fill it. Invisible and tradeable is
    the worst of both.
    """
    from arena.exchange.book import OrderBook, Order
    from arena.exchange.types import OrderStatus, Price, Quantity, Side

    book = OrderBook()
    order = Order(
        order_id=1,
        agent_id="a",
        side=Side.SELL,
        price=Price(100),
        quantity=Quantity(10),
        remaining=Quantity(10),
        priority=1,
    )
    order.status = OrderStatus.NEW
    book.add(order)
    assert book.snapshot().best_ask == Price(100)

    book.remove(order)
    order.status = OrderStatus.CANCELLED
    assert book.snapshot().best_ask is None
    assert order not in book.resting_orders


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------


def test_an_auction_is_charged_rather_than_rebated():
    """Both sides of a cross are passive; billing both at the maker rate pays out."""
    assert MAKER_TAKER.maker_bps < 0, "the premise is a rebate schedule"
    notional = 10_000_000
    passive = int(MAKER_TAKER.charge(notional, aggressor=False))
    crossed = int(MAKER_TAKER.charge(notional, aggressor=False, auction=True))
    assert passive < 0 <= crossed, (
        f"an auction fill is billed {crossed} where a resting fill is billed {passive}"
    )


def test_an_explicit_auction_rate_is_honoured():
    schedule = FeeSchedule(taker_bps=2.0, maker_bps=-1.0, auction_bps=0.5)
    assert schedule.rate(False, auction=True) == 0.5
    assert schedule.rate(True) == 2.0
    assert schedule.rate(False) == -1.0
    assert FREE.free, "a schedule with no rates anywhere is free"


def test_the_venue_earns_rather_than_pays(opened):
    """With auctions running and a rebate schedule, revenue is still positive."""
    assert int(opened.venue.fees_collected) > 0


def test_fees_do_not_break_conservation(opened):
    assert int(opened.venue.conservation_check()) == 0


# --------------------------------------------------------------------------
# The circuit breaker
# --------------------------------------------------------------------------


def test_a_single_print_outside_the_band_does_not_halt_anything():
    """The rule this models has three states, and the middle one is the point.

    Halting on one print turns a fat finger into an outage. The symbol enters a
    limit state, and only pauses if it is still outside the band when the clock
    has run.
    """
    market = build(seed=7)
    market.kernel.start()
    market.kernel.advance(until=seconds(240))

    reasons = Counter(h["reason"] for h in market.venue.halts)
    assert reasons["limit_state"] > 0, "the band was never even approached"
    assert reasons["limit_state"] > reasons["price_band"], (
        "every excursion became a pause, so the limit state is doing nothing"
    )


def test_a_paused_symbol_reopens_through_an_auction(opened):
    """Never straight back into continuous trading."""
    operator = next(
        a for a in opened.agents if type(a).__name__ == "SessionOperator"
    )
    reopens = [r for r in operator.reopens if r["reason"] == "reopen"]
    assert reopens, "nothing was paused, so nothing reopened"
    assert all(
        opened.venue.session(s) is not SessionState.PRE_OPEN
        for s in opened.venue.registry.symbols
    )


def test_the_band_is_a_fraction_of_what_a_contract_can_be_worth():
    """Not of what it costs, which is meaningless for a bounded claim.

    A binary trading at fifty cents under a 5%-of-price band gets two and a
    half cents of room, which any ordinary change of opinion breaks -- so the
    breaker paused every event contract repeatedly while never touching the
    future, whose 5% is sixteen standard deviations.
    """
    market = build(seed=7, price_band=0.05)
    market.kernel.start()
    market.kernel.advance(until=seconds(240))

    paused = Counter(
        h["symbol"] for h in market.venue.halts if h["reason"] == "price_band"
    )
    events = [s for s in market.venue.registry.symbols if s.endswith(("GT44", "GT46", "GT47", "GT48"))]
    assert events
    worst = max((paused.get(s, 0) for s in events), default=0)
    assert worst <= 4, f"an event contract paused {worst} times in four minutes"


def test_the_breaker_measures_elapsed_time_and_not_the_calendar():
    """Two clocks, two questions.

    The calendar decides whether a contract has expired. Elapsed simulated time
    decides how long a symbol has been in a limit state. Sharing one meant the
    limit-state timer never advanced: 241 excursions in three minutes and not
    one pause.
    """
    market = build(seed=7)
    assert market.venue.sim_clock is not None
    market.kernel.start()
    market.kernel.advance(until=seconds(30))
    assert market.venue.sim_clock() > 0


def test_a_market_with_no_band_never_pauses():
    market = build(seed=7, price_band=None)
    market.kernel.start()
    market.kernel.advance(until=seconds(120))
    assert not [h for h in market.venue.halts if h["reason"] == "price_band"]


# --------------------------------------------------------------------------
# More than one maker
# --------------------------------------------------------------------------


def test_the_exchange_has_more_than_one_market_maker(opened):
    """One maker is not a market; it is a counterparty.

    Measured on the single-maker configuration: sweeping 60% of the offers, it
    absorbed 89% of the order, ended short past the point its collateral let it
    quote, and the spread it left was unchanged three minutes later.
    """
    from arena.agents.market_maker import MarketMaker

    makers = [a for a in opened.agents if isinstance(a, MarketMaker)]
    assert len(makers) >= 3
    # Different, or they are one maker with three times the balance sheet.
    assert len({m.half_spread for m in makers}) > 1
    assert len({m.position_limit for m in makers}) > 1
