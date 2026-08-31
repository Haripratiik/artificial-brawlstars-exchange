"""The boundary between a strategy and the exchange.

Most of what is worth testing here is a refusal. A strategy that can see the
venue can see the settlement level and the other participants' positions, and a
backtest of such a thing measures nothing, so the tests that matter are the
ones that check the view contains what it should and stops there.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arena.agents.strategy_agent import MARKOUT_HORIZON, StrategyAgent
from arena.exchange.types import Side
from arena.market.live import VENUE_ID
from arena.sim.time import millis, seconds
from arena.strategies.base import MarketView, Quote, Take, TwoSided, snap

from dashboard.build_market import build


class Fixed:
    """A constant two-sided quote around whatever the view calls a reference."""

    def __init__(self, width=Decimal("0.03"), size=5, skew=Decimal(2000)):
        self.width, self.size, self.skew = width, size, skew
        self.calls = 0

    def symbols(self, view):
        return list(view.symbols)

    def quote(self, view, symbol):
        self.calls += 1
        v = view[symbol]
        reference = v.reference
        if reference is None:
            return TwoSided()
        low, high = v.bounds
        half = self.width * (high - low) / 2
        centre = reference - (high - low) * Decimal(v.position) / self.skew
        return TwoSided(
            bid=Quote(centre - half, self.size), ask=Quote(centre + half, self.size)
        )


class Silent:
    def symbols(self, view):
        return []

    def quote(self, view, symbol):
        return TwoSided()


def _market(strategy=None, taker=None, symbols=8, seed=7, cash=20_000_000):
    market = build(seed=seed)
    by_symbol = {
        s: market.venue.registry.require(s)
        for s in list(market.venue.registry.symbols)[:symbols]
    }
    market.venue.open_account("strat-1", Decimal(cash))
    agent = StrategyAgent(
        "strat-1",
        VENUE_ID,
        by_symbol,
        millis(320),
        maker=strategy,
        taker=taker,
        starting_cash=Decimal(cash),
    )
    market.kernel.add(agent)
    market.agents.append(agent)
    return market, agent


class _Ctx:
    """Enough of a context to assemble a view. There is deliberately no more."""

    def __init__(self, now=seconds(3)):
        self.now = now
        self.rng = None


# --------------------------------------------------------------------------
# Prices leaving a strategy
# --------------------------------------------------------------------------


def test_a_price_is_rounded_away_from_the_touch():
    """A bid rounds down and an ask rounds up, never the other way.

    Rounding a bid up posts it a tick better than the strategy asked for, which
    is a real order at a real price nobody chose. On a book whose tick is a
    meaningful fraction of the spread that tick is most of the edge.
    """
    market, _ = _market(Silent())
    instrument = next(iter(market.venue.registry.symbols))
    listed = market.venue.registry.require(instrument)
    tick = listed.tick_size
    off_grid = listed.from_ticks(10) + tick / 3

    assert snap(listed, Side.BUY, off_grid) == listed.from_ticks(10)
    assert snap(listed, Side.SELL, off_grid) == listed.from_ticks(11)


def test_snapping_a_price_already_on_the_grid_changes_nothing():
    """Idempotent, or a quote drifts a tick every time it is repeated."""
    market, _ = _market(Silent())
    listed = market.venue.registry.require(next(iter(market.venue.registry.symbols)))
    on_grid = listed.from_ticks(40)
    for side in (Side.BUY, Side.SELL):
        assert snap(listed, side, on_grid) == on_grid
        assert snap(listed, side, snap(listed, side, on_grid)) == on_grid


def test_a_price_outside_the_settlement_range_is_pinned_to_it():
    """The contract cannot pay more than its bounds, so no order may ask it to."""
    market, _ = _market(Silent())
    listed = market.venue.registry.require(next(iter(market.venue.registry.symbols)))
    low, high = listed.tick_bounds
    assert snap(listed, Side.BUY, listed.from_ticks(low) - 10) == listed.from_ticks(low)
    assert (
        snap(listed, Side.SELL, listed.from_ticks(high) + 10)
        == listed.from_ticks(high)
    )


def test_a_quote_for_no_lots_is_refused():
    """Distinct from declining to quote, which is `None` and is legitimate."""
    with pytest.raises(ValueError, match="not a quote"):
        Quote(Decimal(1), 0)
    with pytest.raises(ValueError, match="not a take"):
        Take("X", Side.BUY, 0)
    assert TwoSided().is_empty


# --------------------------------------------------------------------------
# What a strategy may see
# --------------------------------------------------------------------------


def test_a_strategy_runs_with_no_kernel_and_no_venue():
    """The property that makes a strategy testable at all.

    A view is a value. If assembling one required a running market then every
    unit test of a strategy would be an integration test, and the arithmetic
    of a quoting rule would only ever be checkable by running six hundred
    simulated seconds and squinting at the result.
    """
    _market_, agent = _market(Fixed())
    view = agent.view(_Ctx())
    assert isinstance(view, MarketView)
    quote = agent.maker.quote(view, view.symbols[0])
    assert quote.bid is not None and quote.ask is not None
    assert quote.bid.price < quote.ask.price


def test_the_view_offers_no_route_to_the_venue_or_to_anyone_else():
    """Structural, and the whole validity argument for a result from here.

    Checked by attribute rather than by inspection because the failure mode is
    somebody adding a convenience accessor later, not somebody writing
    `self.venue` today.
    """
    _market_, agent = _market(Fixed())
    view = agent.view(_Ctx())
    for forbidden in ("venue", "kernel", "agents", "oracle", "settlement", "accounts"):
        assert not hasattr(view, forbidden), forbidden
    one = view[view.symbols[0]]
    for forbidden in ("venue", "settlement_value", "true_value", "level"):
        assert not hasattr(one, forbidden), forbidden


def test_the_view_reports_the_settlement_range_because_it_is_public():
    """It is written in the contract and it is what collateral is charged on.

    A strategy that could not see it would quote prices the contract cannot
    pay, which is not realism, it is a missing input.
    """
    _market_, agent = _market(Fixed())
    view = agent.view(_Ctx())
    for symbol in view.symbols:
        low, high = view[symbol].bounds
        assert low < high
        assert view[symbol].bounds == agent.instruments[symbol].spec.value_bounds


def test_the_book_a_strategy_reads_is_the_stale_one_it_would_really_have():
    """Latency is the phenomenon, not an inconvenience to be smoothed away.

    The view is built from the agent's own local books, which lag the venue by
    that agent's latency. Serving it the venue's current book instead is the
    single most common way a backtest lies.
    """
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(30))
    view = agent.view(_Ctx(seconds(30)))
    for symbol in view.symbols:
        local = agent.books[symbol]
        seen = view[symbol]
        expected = (
            None if local.bid is None else agent.instruments[symbol].from_ticks(local.bid)
        )
        assert seen.best_bid == expected


# --------------------------------------------------------------------------
# The shadow ledger and the markout
# --------------------------------------------------------------------------


def test_the_agent_books_its_own_fills():
    """It is never told its cash, so it adds its fills up, like a real desk.

    This regressed once and silently: a `Filled` does not carry its symbol, so
    a handler that read `event.symbol` saw `None` and booked nothing. Measured
    at the time, 386 fills moved the ledger by exactly zero, and nothing raised.
    """
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(120))

    assert agent.fills > 0
    assert agent.cash != agent.starting_cash

    venue_positions = market.venue.account("strat-1").positions
    for symbol in agent.instruments:
        theirs = venue_positions.get(symbol)
        assert agent.position.get(symbol, 0) == (0 if theirs is None else theirs.quantity)


def test_a_strategy_is_asked_again_the_moment_it_is_filled():
    """Being lifted is news, and news that waits for a timer is a free second hit.

    Glosten-Milgrom's ask is the expectation conditional on the next order
    being a buy, so a quote that does not move when it is taken can be taken
    again at the same price. Measured on the incumbent makers, which requote on
    a schedule only, 17% of passive fills were a repeat at the same price
    within 500ms.
    """
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(60))

    wakes = int(seconds(60)) // int(millis(320))
    assert agent.fills > 0
    # More quote calls than the schedule alone could have produced.
    assert agent.maker.calls > wakes


def test_a_strategy_that_does_not_requote_on_fill_is_asked_less_often():
    """The same run with the behaviour switched off, as a control."""
    market, agent = _market(Fixed())
    agent.requote_on_fill = False
    market.kernel.start()
    market.kernel.advance(until=seconds(60))
    quiet = agent.maker.calls

    market2, agent2 = _market(Fixed())
    market2.kernel.start()
    market2.kernel.advance(until=seconds(60))

    assert agent2.maker.calls > quiet


def test_markout_is_signed_so_that_negative_always_means_picked_off():
    """One convention, in both directions, or the two sides cannot be added.

    A maker that bought and then saw the mid fall has been picked off; so has
    one that sold and saw it rise. Both are negative here. Without that the
    bid and ask markouts have opposite signs for the same event and their sum
    means nothing.
    """
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(180))

    assert agent._markout, "no fill matured, so the test measured nothing"
    for (symbol, side), value in agent._markout.items():
        assert symbol in agent.instruments
        assert side in (Side.BUY, Side.SELL)
        assert isinstance(value, float)

    view = agent.view(_Ctx(seconds(180)))
    for symbol in view.symbols:
        markout = view[symbol].markout
        assert set(markout) == {Side.BUY, Side.SELL}


def test_a_markout_is_not_reported_before_its_horizon_has_passed():
    """There is no answer yet, and inventing one would report a fill as good."""
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=MARKOUT_HORIZON // 2)
    assert agent._markout == {}


# --------------------------------------------------------------------------
# Running as a participant
# --------------------------------------------------------------------------


def test_a_strategy_trades_and_conservation_is_still_exactly_zero():
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(180))
    assert agent.fills > 0
    assert market.venue.conservation_check() == 0


def test_a_strategy_that_declines_to_quote_sends_nothing():
    market, agent = _market(Silent())
    market.kernel.start()
    market.kernel.advance(until=seconds(60))
    assert agent.fills == 0
    assert not agent.live_orders


def test_an_agent_needs_a_strategy_of_some_kind():
    market = build(seed=3)
    by_symbol = {s: market.venue.registry.require(s) for s in list(market.venue.registry.symbols)[:2]}
    with pytest.raises(ValueError, match="needs a maker, a taker, or both"):
        StrategyAgent("nobody", VENUE_ID, by_symbol, millis(300))


def test_an_unchanged_quote_keeps_its_place_in_the_queue():
    """Cancelling and reposting a price that has not moved is the worst thing
    an agent can do with a message: it gives up priority and pays for it.

    Measured here once already, when every agent reposted unconditionally on
    every wakeup: 1.6 million events per simulated minute, against 317 thousand
    after. The strategy adapter must not reintroduce it, so a strategy that
    returns the same numbers twice must produce no second order.
    """
    market, agent = _market(Fixed())
    market.kernel.start()
    market.kernel.advance(until=seconds(45))

    before = dict(agent._intent)
    symbol = next(iter(agent.instruments))
    working = {k: v for k, v in agent.live_orders.items() if v == symbol}

    ctx = _Ctx(seconds(45))
    view = agent.view(ctx)
    same = agent.maker.quote(view, symbol)
    agent._apply_quote(_LiveCtx(market, agent, seconds(45)), symbol, same)

    assert agent._intent == before
    assert {k: v for k, v in agent.live_orders.items() if v == symbol} == working


def test_a_maker_can_refuse_to_cross():
    """Post-only, which the engine has always had and the strategy layer had not.

    A maker whose fair value is through the touch takes liquidity instead of
    providing it, and until a quote could carry a time-in-force there was no
    way to say otherwise: every quote went out good-till-cancelled. Measured
    over 60 simulated seconds with a strategy that deliberately quotes through
    the touch on both sides, fills fall from 10,818 to 99.

    It is not free and so it is not the default. The orders that do not cross
    are refused rather than repriced, and a maker that cannot cross also
    cannot exit its inventory, so this is a choice per quote.
    """

    class Crosser:
        def __init__(self, post_only):
            self.post_only = post_only

        def symbols(self, view):
            return list(view.symbols)

        def quote(self, view, symbol):
            v = view[symbol]
            reference = v.reference
            if reference is None:
                return TwoSided()
            low, high = v.bounds
            edge = (high - low) * Decimal("0.01")
            # Bid above and ask below the reference, so both sides cross.
            return TwoSided(
                bid=Quote(reference + edge, 5, post_only=self.post_only),
                ask=Quote(reference - edge, 5, post_only=self.post_only),
            )

    crossing, _ = _market(Crosser(False))
    crossing.kernel.start()
    crossing.kernel.advance(until=seconds(60))
    freely = next(a for a in crossing.agents if a.agent_id == "strat-1").fills

    refusing, _ = _market(Crosser(True))
    refusing.kernel.start()
    refusing.kernel.advance(until=seconds(60))
    refused = next(a for a in refusing.agents if a.agent_id == "strat-1").fills

    assert freely > 10 * refused
    assert crossing.venue.conservation_check() == 0
    assert refusing.venue.conservation_check() == 0


def test_a_quote_is_good_till_cancelled_unless_it_says_otherwise():
    """The default has to stay what every existing strategy already assumed."""
    assert Quote(Decimal(1), 1).post_only is False


class _LiveCtx:
    """A context that records sends instead of making them."""

    def __init__(self, market, agent, now):
        self.now = now
        self.rng = None
        self.sent = []

    def send(self, recipient, message):
        self.sent.append((recipient, message))

    def request_wakeup(self, delay):
        pass
