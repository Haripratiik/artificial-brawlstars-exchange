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

from decimal import Decimal

import pytest

from arena.agents.fundamental import FundamentalTrader
from arena.market.instrument import Instrument
from arena.agents.market_maker import MarketMaker
from arena.agents.noise import NoiseTrader
from arena.exchange.events import Submit
from arena.exchange.types import AgentId, OrderType, Quantity, Side, TimeInForce
from arena.market.live import HUMAN_ID
from arena.portfolio.money import from_money
from arena.market.venue import SymbolCommand
from arena.sim.kernel import Kernel
from arena.sim.latency import PairwiseLatency
from arena.sim.time import Duration, Timestamp, micros, millis, seconds

from dashboard.build_market import build, instruments as build_instruments

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


def test_a_large_order_moves_the_price():
    """Size has to have consequences, or the book is decorative.

    A sweep should walk the book, pay progressively worse prices, and leave the
    touch higher than it found it -- then decay back as the maker refills and
    the fundamental agents push against it. Temporary impact and permanent
    impact are different things, and a market that shows neither is not a
    market.

    Sized against the depth that is standing rather than against a round
    number. This test used to sweep 5,000 lots, which happened to be more than
    the whole offer side: it cleared the book, left nothing at the touch, and
    then read the mark off whichever small print landed next. That measures
    luck, not impact -- and it duly broke the first time an unrelated change
    moved the market's state at the sixty-second mark.
    """
    # Funded explicitly. A person's default account is deliberately small
    # enough to read a profit against, and this test is about what a *large*
    # order does to a book -- so it asks for the capital it needs rather than
    # inheriting whatever the product happens to hand a new user.
    m = build(seed=7, human_cash=40_000_000)
    m.kernel.start()
    m.kernel.advance(until=seconds(60))

    instrument = m.venue.registry.require(SYMBOL)
    book = m.venue.engine(SYMBOL).book.snapshot(levels=10_000)
    offered = sum(int(quantity) for _, quantity in book.asks)
    assert offered > 200, f"nothing to sweep: only {offered} offered"

    before = float(m.venue.mark_price(SYMBOL))
    best_ask = float(instrument.from_ticks(book.best_ask))
    sweep = offered * 3 // 5

    m.human.enqueue(
        SymbolCommand(
            SYMBOL,
            Submit(HUMAN_ID, Side.BUY, Quantity(sweep), None, OrderType.MARKET, TimeInForce.IOC),
        )
    )
    m.kernel.advance(until=seconds(61))
    impact = float(m.venue.mark_price(SYMBOL))

    position = m.venue.account(HUMAN_ID).positions[SYMBOL]
    assert position.quantity > 0, "the sweep did not fill at all"
    # It walked the book, so it paid worse than the touch on average.
    assert float(position.average_price) > best_ask
    # And it moved the market it traded through: the cheapest offer left is
    # dearer than the cheapest offer it started with.
    after = m.venue.engine(SYMBOL).book.snapshot()
    assert after.best_ask is None or float(instrument.from_ticks(after.best_ask)) > best_ask
    assert impact > before, f"mark did not move: {before} -> {impact}"

    # And the market does *not* repair itself, which is a defect this test
    # pins rather than a property it wants.
    #
    # One maker supplies essentially the whole other side: measured at the time
    # of writing it absorbs ~89% of the sweep and is left short about two
    # thousand lots, which is far past the point where its collateral lets it
    # quote again. So the offer it was run over at never comes back, the spread
    # stays ten times wider than it started, and a minute later the maker has
    # worked none of it off. A real book repairs in milliseconds because the
    # maker that got run over is one of many. Asserted as it is so that the day
    # replenishment arrives, this test fails and says so.
    #
    # The bound is well below the measured figure on purpose. The claim is "one
    # maker is the whole other side", not "89%" -- and the exact fraction moves
    # whenever an unrelated contract is listed, because the seeded market takes
    # a different path. Pinning it tight made this fail twice for reasons that
    # had nothing to do with what it is testing.
    maker = m.venue.account("mm-1").positions[SYMBOL]
    assert int(maker.quantity) < 0
    absorbed = -int(maker.quantity) / int(position.quantity)
    assert absorbed > 0.75, f"the maker took only {absorbed:.0%} of the sweep"

    before_spread = float(instrument.from_ticks(book.best_ask)) - float(
        instrument.from_ticks(book.best_bid)
    )
    m.kernel.advance(until=seconds(120))
    stuck = m.venue.engine(SYMBOL).book.snapshot()
    assert stuck.best_ask is not None and stuck.best_bid is not None
    after_spread = float(instrument.from_ticks(stuck.best_ask)) - float(
        instrument.from_ticks(stuck.best_bid)
    )
    assert after_spread > 3 * before_spread, (
        f"the spread repaired from {before_spread} to {after_spread}; if that is "
        "real, replenishment now works and this assertion should become the "
        "recovery test it replaced"
    )


def test_the_mark_never_sits_outside_the_touch():
    """A print is about the past; a resting order is about now.

    When they disagree the resting order wins, because it is a price someone
    will actually deal at. Without the clamp a single lot trading on the bid
    can drag the mark away from a touch that a thousand lots are standing at,
    and every open position is then valued at a number nobody is offering.
    """
    m = build(seed=7)
    m.kernel.start()
    m.kernel.advance(until=seconds(45))

    for symbol in m.venue.registry.symbols:
        instrument = m.venue.registry.require(symbol)
        book = m.venue.engine(symbol).book.snapshot()
        mark = float(m.venue.mark_price(symbol))
        if book.best_bid is not None:
            assert mark >= float(instrument.from_ticks(book.best_bid)) - 1e-9, symbol
        if book.best_ask is not None:
            assert mark <= float(instrument.from_ticks(book.best_ask)) + 1e-9, symbol


def test_a_market_order_is_collateralised_against_the_book_not_the_range():
    """A market order can only trade against resting liquidity.

    Reserving against the far end of the settlement range instead -- 10,000 on a
    contract quoted near 4,700 -- rejects orders that could never have cost
    anything like that much, for a price they were structurally incapable of
    paying. The symptom was a large order silently vanishing.

    Funded explicitly: the point is that collateral is measured against the
    *book*, so the account has to be able to cover what the book actually
    holds. Inheriting a deliberately small default account would make this
    test pass or fail for the wrong reason.
    """
    m = build(seed=7, human_cash=40_000_000)
    m.kernel.start()
    m.kernel.advance(until=seconds(60))

    m.human.enqueue(
        SymbolCommand(
            SYMBOL,
            Submit(HUMAN_ID, Side.BUY, Quantity(5000), None, OrderType.MARKET, TimeInForce.IOC),
        )
    )
    m.kernel.advance(until=seconds(62))

    rejects = [e for e in m.human.log if e["type"] == "reject"]
    assert not rejects, f"order was rejected: {rejects[-1]['reason']}"
    assert m.venue.account(HUMAN_ID).positions[SYMBOL].quantity > 0


def test_an_order_beyond_all_liquidity_and_capital_is_still_refused():
    """The check must still bite when it should.

    Bounding a market order by the book must not become a way to take on
    exposure the account cannot cover: a resting limit order still reserves
    against its own price.
    """
    m = build(seed=7)
    m.kernel.start()
    m.kernel.advance(until=seconds(60))

    instrument = m.venue.registry.require(SYMBOL)
    m.human.enqueue(
        SymbolCommand(
            SYMBOL,
            Submit(
                HUMAN_ID,
                Side.BUY,
                Quantity(500_000),
                instrument.to_ticks(Decimal("4700")),
                OrderType.LIMIT,
                TimeInForce.GTC,
            ),
        )
    )
    m.kernel.advance(until=seconds(62))

    reasons = [e["reason"] for e in m.human.log if e["type"] == "reject"]
    assert "insufficient_collateral" in reasons


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
    # By what an agent *is*, not by what it is called. The class-name version
    # of this broke the day a specialised maker was wired in, which is a fact
    # about the test rather than about the population.
    assert sum(isinstance(a, MarketMaker) for a in market.agents) == 1
    kinds = [type(a).__name__ for a in market.agents]
    assert kinds.count("FundamentalTrader") == 2
    assert kinds.count("NoiseTrader") >= 10


# --------------------------------------------------------------------------
# The arbitrageur: consistency *between* books
# --------------------------------------------------------------------------


def test_relations_are_read_out_of_the_listed_contracts():
    """Nothing is configured by hand -- the algebra comes from the contracts.

    This is what makes the agent connective tissue rather than a strategy: list
    a new spread and it becomes arbitrageable with no code change. If these
    relations were a hand-written table, the test would be checking that I can
    copy a list, which is not a property of the market.
    """
    from arena.agents.arbitrageur import derive_relations

    listed = {i.symbol: i for i in build_instruments()}
    relations = {r.name: r for r in derive_relations(listed)}

    spread = relations["spread:SPIKE_CROW"]
    assert spread.target == "SPIKE_CROW"
    assert dict(spread.legs) == {"SPIKE_WR_FUT": 1.0, "CROW_WR_FUT": -1.0}
    assert spread.constant == 0.0

    # Put-call parity, C = P + F - K, with the strike as the constant.
    parity = relations["parity:SPIKE_C4700"]
    assert parity.target == "SPIKE_C4700"
    assert dict(parity.legs) == {"SPIKE_P4700": 1.0, "SPIKE_WR_FUT": 1.0}
    assert parity.constant == -4_700.0


def test_a_relation_missing_a_leg_is_not_formed():
    """Take a leg away and the index relation goes; put it back and it returns.

    A relation traded against a proxy is a bet, not an arbitrage, so the agent
    has to decline it -- and then form it the moment the leg is listed, with no
    code change, or the derivation is not really reading the contracts.

    Written by *removing* a listed leg rather than by relying on one being
    absent. The first version leaned on PIPER having no future, which was true
    until PIPER was listed precisely so the index would have all three of them;
    a test whose premise is an accident of the listing stops testing anything
    the day the listing improves.
    """
    from arena.agents.arbitrageur import derive_relations

    listed = {i.symbol: i for i in build_instruments()}
    assert "ASSASSIN_IDX" in listed
    piper = listed.pop("PIPER_WR_FUT")
    assert not any(r.target == "ASSASSIN_IDX" for r in derive_relations(listed))

    listed[piper.symbol] = piper
    index = next(r for r in derive_relations(listed) if r.target == "ASSASSIN_IDX")
    assert dict(index.legs) == {
        "SPIKE_WR_FUT": 0.5,
        "CROW_WR_FUT": 0.3,
        "PIPER_WR_FUT": 0.2,
    }


@pytest.fixture(scope="module")
def arb_market():
    """A market with the arbitrageur switched on.

    It is off by default. Across four paired seeds it improved spread
    consistency on three and worsened it on the fourth, and on one seed it took
    visible ask depth from 877 lots to 69 -- consistency bought with liquidity.
    So the tests below assert what it reliably *does* (derives the right
    identities, trades them, conserves value, respects its limits) and not the
    price convergence it does not reliably deliver. docs/GAPS.md carries the
    numbers.
    """
    m = build(seed=41, arbitrageur=True)
    m.kernel.start()
    m.kernel.advance(until=seconds(300))
    return m


def test_the_arbitrageur_actually_traded(arb_market):
    """Otherwise every assertion below is about an idle agent."""
    from arena.agents.arbitrageur import Arbitrageur

    arb = next(a for a in arb_market.agents if isinstance(a, Arbitrageur))
    assert arb.attempts > 0


def test_the_arbitrageur_leaves_conservation_exact(arb_market):
    """It adds volume, so it is the obvious place for the invariant to break."""
    assert arb_market.venue.conservation_check() == 0


def test_the_arbitrageur_never_legs_past_its_position_limit(arb_market):
    """A half-entered relation is a directional bet, so the limit is per leg."""
    from arena.agents.arbitrageur import Arbitrageur

    arb = next(a for a in arb_market.agents if isinstance(a, Arbitrageur))
    for symbol, quantity in arb.position.items():
        assert abs(quantity) <= arb.position_limit + arb.base_size * 2, symbol


def test_the_arbitrageur_sizes_to_the_liquidity_it_can_see(arb_market):
    """The participation cap is what stops it stripping the book bare.

    Without it this agent fired IOC orders on three legs every 400ms and took
    ask depth from 207 resting lots to 26 -- and a book that thin cannot absorb
    anyone else's order, so the damage was not confined to its own P&L.
    """
    from arena.agents.arbitrageur import Arbitrageur

    arb = next(a for a in arb_market.agents if isinstance(a, Arbitrageur))
    assert 0.0 < arb.max_participation <= 1.0
    for symbol in arb.instruments:
        for side in (Side.BUY, Side.SELL):
            book = arb.books[symbol]
            resting = book.ask_size if side is Side.BUY else book.bid_size
            assert arb._takeable(symbol, side) <= max(0, resting)


# --------------------------------------------------------------------------
# You can see who took the other side
# --------------------------------------------------------------------------


def test_your_counterparty_is_named_and_is_one_of_the_bots():
    """"Is anything actually on the other side of this?" deserves a real answer.

    A roster of agents does not answer it; naming the participant on the fill
    does. The population here is a market maker, two fundamental traders and
    fourteen noise traders, and a person's order has to end up against one of
    them rather than against nothing.
    """
    market = build(seed=7, human_cash=250_000)
    market.start()
    market.kernel.advance(until=seconds(45))
    market.submit(SYMBOL, "buy", 12, None)
    market.kernel.advance(until=seconds(50))

    fills = market.venue.counterparties_for(HUMAN_ID)
    assert fills, "the order never traded against anyone"

    population = {str(a.agent_id) for a in market.agents}
    for fill in fills:
        assert fill["counterparty"] in population, fill["counterparty"]
        assert fill["counterparty"] != str(HUMAN_ID), "you cannot fill yourself"
        assert fill["symbol"] == SYMBOL
        assert fill["quantity"] > 0


def test_your_fills_survive_the_bots_flooding_the_tape():
    """The regression that made the feature useless without failing anything.

    The first version kept one rolling window of *every* fill on the venue. The
    bots print thousands a minute, so a person's single trade was evicted from
    it within seconds -- the panel was empty in exactly the situation it existed
    for, and nothing anywhere reported a problem. Logs are per participant now.
    """
    market = build(seed=7, human_cash=250_000)
    market.start()
    market.kernel.advance(until=seconds(45))
    market.submit(SYMBOL, "buy", 12, None)
    market.kernel.advance(until=seconds(50))

    immediately = market.venue.counterparties_for(HUMAN_ID)
    assert immediately

    # Two more minutes of the population trading among themselves.
    market.kernel.advance(until=seconds(180))
    assert market.venue.counterparties_for(HUMAN_ID) == immediately


def test_a_person_starts_with_an_account_they_can_read():
    """A gain of a hundred against forty million teaches nothing.

    The bots keep a large balance because a maker quoting seven books at once
    genuinely needs one; the person does not, and their opening balance is
    deliberately a figure a profit is visible against.
    """
    from dashboard.build_market import HUMAN_STARTING_CASH

    market = build(seed=7)
    human = market.venue.account(HUMAN_ID)
    maker = next(a for a in market.agents if isinstance(a, MarketMaker))

    assert float(from_money(human.starting_cash)) == float(HUMAN_STARTING_CASH)
    assert human.starting_cash < market.venue.account(maker.agent_id).starting_cash
