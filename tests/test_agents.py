"""Trading agents, and the market they make together.

Two of these are regression tests for bugs that only appeared once a real market
was run, and both are the dangerous kind -- they produce plausible numbers rather
than crashes.

``test_messages_on_a_link_never_overtake`` guards the kernel: jittered latency
was allowed to reorder messages on the same link, so a fill could arrive before
the acknowledgement of the order that produced it.

``test_agent_position_matches_the_venue`` guards the consequence: an agent that
saw a fill for an order it had not been told about dropped the position update
entirely, and the late acknowledgement then left a phantom order working
forever. The agent believed it held +7 while the venue held 0.
"""

from __future__ import annotations

import pytest

from arena.agents.fundamental import FundamentalTrader
from arena.agents.market_maker import MarketMaker
from arena.agents.noise import NoiseTrader
from arena.exchange.events import Submit
from arena.exchange.types import AgentId, OrderType, Quantity, Side, TimeInForce
from arena.market.live import HUMAN_ID
from arena.market.venue import SymbolCommand
from arena.sim.kernel import Kernel
from arena.sim.latency import PairwiseLatency
from arena.sim.time import Duration, micros, millis, seconds

from dashboard.build_market import build

SYMBOL = "SPIKE_WR_FUT"


@pytest.fixture(scope="module")
def market():
    m = build(seed=17)
    m.kernel.start()
    m.kernel.advance(until=seconds(90))
    return m


# --------------------------------------------------------------------------
# Kernel: per-link ordering
# --------------------------------------------------------------------------


def test_messages_on_a_link_never_overtake():
    """An exchange session is a stream; a fill cannot precede its own ack.

    Independent per-message jitter models a datagram network, which is the
    wrong model for order entry and produced a real, silent divergence between
    an agent's position and the venue's.
    """
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class Sink:
        agent_id: AgentId = AgentId("sink")
        arrivals: list[tuple[int, int]] = field(default_factory=list)

        def on_start(self, ctx): pass
        def on_wakeup(self, ctx): pass
        def on_finish(self, ctx): pass
        def on_message(self, ctx, sender, message):
            self.arrivals.append((int(ctx.now), message))

    @dataclass
    class Spammer:
        agent_id: AgentId = AgentId("spammer")

        def on_start(self, ctx):
            for i in range(200):
                ctx.send(AgentId("sink"), i)

        def on_wakeup(self, ctx): pass
        def on_finish(self, ctx): pass
        def on_message(self, ctx, sender, message): pass

    kernel = Kernel(
        seed=5,
        latency=PairwiseLatency(default=millis(10), jitter_fraction=0.9, seed=5),
    )
    sink = Sink()
    kernel.add_all([sink, Spammer()])
    kernel.run(until=seconds(5))

    payloads = [m for _t, m in sink.arrivals]
    assert payloads == sorted(payloads), "messages overtook each other on one link"
    stamps = [t for t, _m in sink.arrivals]
    assert stamps == sorted(stamps)


def test_jitter_still_varies_delivery():
    """Ordering is preserved, but latency must not become constant."""
    from dataclasses import dataclass, field

    @dataclass
    class Sink:
        agent_id: AgentId = AgentId("sink")
        stamps: list[int] = field(default_factory=list)

        def on_start(self, ctx): pass
        def on_wakeup(self, ctx): pass
        def on_finish(self, ctx): pass
        def on_message(self, ctx, sender, message): self.stamps.append(int(ctx.now))

    @dataclass
    class Pinger:
        agent_id: AgentId = AgentId("pinger")
        sent: int = 0

        def on_start(self, ctx): ctx.request_wakeup(millis(100))
        def on_wakeup(self, ctx):
            self.sent += 1
            ctx.send(AgentId("sink"), self.sent)
            if self.sent < 30:
                ctx.request_wakeup(millis(100))
        def on_finish(self, ctx): pass
        def on_message(self, ctx, sender, message): pass

    kernel = Kernel(
        seed=5,
        latency=PairwiseLatency(default=millis(10), jitter_fraction=0.5, seed=5),
    )
    sink = Sink()
    kernel.add_all([sink, Pinger()])
    kernel.run(until=seconds(10))

    gaps = {sink.stamps[i + 1] - sink.stamps[i] for i in range(len(sink.stamps) - 1)}
    assert len(gaps) > 1, "jitter was flattened out entirely"


# --------------------------------------------------------------------------
# Agents keep an honest book
# --------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [3, 11, 29])
def test_agent_position_matches_the_venue(seed):
    """An agent's belief about its own position must equal the truth.

    The failure this guards was silent: the agent reported +7 where the venue
    held 0, and nothing about either number looked wrong on its own.
    """
    m = build(seed=seed)
    m.kernel.start()
    m.kernel.advance(until=seconds(20))

    for i in range(10):
        m.human.enqueue(
            SymbolCommand(
                SYMBOL,
                Submit(
                    HUMAN_ID,
                    Side.BUY if i % 2 == 0 else Side.SELL,
                    Quantity(3),
                    None,
                    OrderType.MARKET,
                    TimeInForce.IOC,
                ),
            )
        )
        m.kernel.advance(until=seconds(20 + (i + 1) * 2))

    venue_quantity = m.venue.account(HUMAN_ID).positions[SYMBOL].quantity
    assert m.human.position[SYMBOL] == venue_quantity


def test_market_orders_leave_nothing_working():
    """An IOC never rests, so no phantom order may survive it."""
    m = build(seed=3)
    m.kernel.start()
    m.kernel.advance(until=seconds(20))
    for _ in range(6):
        m.human.enqueue(
            SymbolCommand(
                SYMBOL,
                Submit(HUMAN_ID, Side.BUY, Quantity(2), None, OrderType.MARKET, TimeInForce.IOC),
            )
        )
    m.kernel.advance(until=seconds(40))
    assert m.human.live_orders == {}


# --------------------------------------------------------------------------
# The market behaves like a market
# --------------------------------------------------------------------------


def test_the_market_trades(market):
    total = sum(len(market.venue.engine(s).tape) for s in market.venue.registry.symbols)
    assert total > 200, f"only {total} trades in 90 seconds"


def test_every_instrument_is_quoted_and_never_crossed(market):
    """Every book is live, and none of them cross.

    Not every book is two-sided, and that is correct rather than a defect. A
    maker at its position limit stops adding to the side that would breach it,
    so a contract the market has decided is worthless can end up offered-only.
    That is adverse selection visible in the book, which is a phenomenon to
    observe rather than to configure away.
    """
    for symbol in market.venue.registry.symbols:
        book = market.venue.engine(symbol).book.snapshot()
        assert book.bids or book.asks, f"{symbol} has no quotes at all"
        if book.best_bid is not None and book.best_ask is not None:
            assert book.best_bid < book.best_ask, f"{symbol} is crossed"


def test_the_futures_stay_two_sided(market):
    """The instruments carrying the research should keep a real market."""
    for symbol in ("SPIKE_WR_FUT", "CROW_WR_FUT"):
        book = market.venue.engine(symbol).book.snapshot()
        assert book.best_bid is not None and book.best_ask is not None


def test_no_account_is_over_committed(market):
    """Free cash below zero means the venue allowed a trade it could not back."""
    for agent_id, account in market.venue.accounts.items():
        assert int(account.free_cash) >= 0, f"{agent_id} is over-committed"


def test_value_is_conserved_in_a_live_market(market):
    assert market.venue.conservation_check() == 0


@pytest.mark.parametrize("symbol,tolerance", [("SPIKE_WR_FUT", 0.10), ("CROW_WR_FUT", 0.10)])
def test_price_discovers_the_settlement_value(symbol, tolerance):
    """The market must move toward what the contract will actually pay.

    Opened deliberately at the midpoint of each contract's range rather than at
    the answer, so any convergence has to be produced by the fundamental agents
    trading against the maker. Without this the market would be liquid and
    volatile but anchored to nothing.
    """
    from dashboard.build_market import instruments, true_values

    m = build(seed=17)
    m.kernel.start()
    truth = true_values(instruments())
    instrument = m.venue.registry.require(symbol)

    # Compared in price space, not ticks: a mark is a midpoint and can fall
    # between ticks, which to_ticks rightly refuses to round.
    low, high = instrument.settlement_bounds
    opening = float(low + high) / 2
    target = float(instrument.from_ticks(int(truth[symbol])))
    m.kernel.advance(until=seconds(120))

    final = float(m.venue.mark_price(symbol))
    span = abs(float(high) - float(low))

    assert abs(final - target) < abs(opening - target), (
        f"{symbol} moved away from settlement: opened {opening}, "
        f"ended {final}, truth {target}"
    )
    assert abs(final - target) / span < tolerance


def test_the_maker_respects_its_position_limit(market):
    """A position limit is soft, and the reason is worth stating.

    The maker stops quoting the side that would breach its limit, but its
    cancels are in flight while fills are still landing, so it can overshoot by
    roughly one quote. Any real market maker has the same exposure -- an order
    already resting cannot be unsent -- so the test allows the overshoot and
    checks it stays small rather than pretending it cannot happen.
    """
    maker = next(a for a in market.agents if isinstance(a, MarketMaker))
    slack = maker.quote_size * 2
    for symbol, quantity in maker.position.items():
        assert abs(quantity) <= maker.position_limit + slack, (
            f"{symbol} overshot its limit by more than one quote"
        )


def test_agent_populations_are_present(market):
    kinds = [type(a).__name__ for a in market.agents]
    assert kinds.count("MarketMaker") == 1
    assert kinds.count("FundamentalTrader") == 2
    assert kinds.count("NoiseTrader") >= 10
