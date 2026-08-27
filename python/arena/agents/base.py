"""What every trading agent shares.

An agent's view of the world is exactly what has reached its mailbox. It does
not read the venue, the book, or another agent's state -- if it wants to know
the best bid it must have subscribed, and what it knows is what arrived, at the
time it arrived. That restriction is the whole point: an agent that could peek
at the book would make every latency and information-asymmetry result
meaningless, and the peek would be invisible in the output.

So this base class maintains a *local* view, updated only by messages, and
offers the small vocabulary an agent needs: quote, cancel, and know its own
position. Anything domain-specific -- what a brawler's win rate will be, whether
a patch matters -- belongs to the subclass and arrives through its own feed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from arena.exchange.events import Acknowledged, Cancel, Filled, Rejected, Submit
from arena.exchange.types import (
    AgentId,
    OrderId,
    OrderType,
    Price,
    Quantity,
    Side,
    TimeInForce,
)
from arena.market.instrument import Instrument
from arena.market.venue import SymbolCommand
from arena.sim.kernel import SimulationContext
from arena.sim.messages import Feed, PrivateEvent, Subscribe, TopOfBook, TradePrint
from arena.sim.time import Duration, Timestamp

__all__ = ["TradingAgent", "LocalBook"]


@dataclass(slots=True)
class LocalBook:
    """An agent's own picture of one symbol, as of the last message it received.

    Deliberately allowed to be stale. A market maker quoting off a view that is
    100ms old *is* the adverse-selection problem -- modelling it away would
    delete the phenomenon the experiments are about.
    """

    symbol: str
    bid: Price | None = None
    ask: Price | None = None
    bid_size: int = 0
    ask_size: int = 0
    last: Price | None = None
    updated_at: int = 0

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return float(self.last) if self.last is not None else None
        return (int(self.bid) + int(self.ask)) / 2.0

    @property
    def spread(self) -> int | None:
        if self.bid is None or self.ask is None:
            return None
        return int(self.ask) - int(self.bid)


class TradingAgent:
    """Base for agents that trade on a venue through the kernel."""

    def __init__(
        self,
        agent_id: AgentId,
        venue_id: AgentId,
        instruments: dict[str, Instrument],
        wake_interval: Duration,
    ) -> None:
        self.agent_id = agent_id
        self.venue_id = venue_id
        self.instruments = instruments
        self.wake_interval = wake_interval
        self.books: dict[str, LocalBook] = {
            symbol: LocalBook(symbol) for symbol in instruments
        }
        # Orders this agent believes are live. Believes, not knows: an
        # acknowledgement may still be in flight, and a fill may not have
        # arrived yet. Reconciling that belief with reality is a real trading
        # problem and is left visible rather than assumed away.
        # Working orders, keyed by **(symbol, order id)**.
        #
        # Order ids come from the matching engine, and there is one engine per
        # symbol, so id 5 exists on every book at once. Keyed by id alone -- as
        # this was -- an agent working orders on twenty-six contracts loses
        # track of almost all of them: a new acknowledgement overwrites the
        # entry for a different symbol's order, and completing one puts its id
        # in ``_completed``, after which the acknowledgement for *another*
        # book's order with the same id is discarded as a late duplicate and
        # that order is never cancelled again.
        #
        # Measured before the fix, after two minutes: `mm-3` believed it had 8
        # working orders across the whole exchange and had **123 resting in one
        # book**. The stale ones formed a wall the price could not move
        # through, the maker's own position drifted from the venue's (it
        # believed +807 where the ledger said -63), and the cancel-to-trade
        # ratio -- the thing docs/GAPS.md recorded as "nothing requotes fast
        # enough" -- was low because most quotes were never cancelled at all.
        self.live_orders: dict[tuple[str, OrderId], str] = {}
        # Which side each working order is on, so one side can be
        # replaced without disturbing the other.
        self.order_side: dict[tuple[str, OrderId], Side] = {}
        # The quote this agent believes it is working, per (symbol, side).
        self._intent: dict[tuple[str, Side], tuple[int, int]] = {}
        # Orders known to be finished, so a late message cannot revive one.
        self._completed: dict[tuple[str, OrderId], None] = {}
        self.position: dict[str, int] = dict.fromkeys(instruments, 0)
        self.fills = 0
        self.rejects = 0

    # -- lifecycle ---------------------------------------------------------

    def on_start(self, ctx: SimulationContext) -> None:
        # Quotes conflated to this agent's own decision cadence; trades not.
        #
        # An agent cannot act on a book update that arrives between two of its
        # wakeups -- by the time it looks, the update has been superseded -- so
        # sending every one is work nobody uses. Real feeds conflate for exactly
        # this reason, and unconflated ones are a product you pay extra for.
        #
        # Measured: with twenty agents each subscribed to twenty-six books with
        # no conflation, market data alone was most of **887,000 events per
        # simulated minute**.
        #
        # Trades are not conflated. The tape is the record of what happened,
        # and the maker's anchor is an average over it -- dropping prints would
        # not slow the market down, it would change what the market believes.
        throttle = int(self.wake_interval) // 2
        for symbol in sorted(self.instruments):
            ctx.send(self.venue_id, Subscribe(Feed.TOP_OF_BOOK, symbol, throttle))
            ctx.send(self.venue_id, Subscribe(Feed.TRADES, symbol))
        self.schedule_next(ctx)

    def on_finish(self, ctx: SimulationContext) -> None:
        pass

    def schedule_next(self, ctx: SimulationContext) -> None:
        """Wake on a jittered interval.

        Jittered because a population of agents on an identical cadence would
        synchronise into artificial waves of order flow that no real market
        produces, and those waves would then show up in every microstructure
        measurement as if they were real.
        """
        jitter = ctx.rng.uniform(0.7, 1.3)
        ctx.request_wakeup(Duration(max(1, int(int(self.wake_interval) * jitter))))

    def on_wakeup(self, ctx: SimulationContext) -> None:
        self.act(ctx)
        self.schedule_next(ctx)

    def act(self, ctx: SimulationContext) -> None:
        """What the agent does when it wakes. Subclasses implement this."""

    # -- messages ----------------------------------------------------------

    def on_message(self, ctx: SimulationContext, sender: AgentId, message: Any) -> None:
        if isinstance(message, TopOfBook):
            book = self.books.get(message.symbol)
            if book is not None:
                book.bid, book.ask = message.bid, message.ask
                book.bid_size = int(message.bid_size)
                book.ask_size = int(message.ask_size)
                book.updated_at = int(ctx.now)
            self.on_quote(ctx, message)
        elif isinstance(message, TradePrint):
            book = self.books.get(message.symbol)
            if book is not None:
                book.last = message.price
            self.on_print(ctx, message)
        elif isinstance(message, PrivateEvent):
            self._on_private(ctx, message.event, message.symbol)

    def _on_private(self, ctx: SimulationContext, event: Any, symbol: str) -> None:
        order_id = getattr(event, "order_id", None)
        key = None if order_id is None else (symbol, order_id)

        if isinstance(event, Acknowledged):
            # A late acknowledgement must not resurrect an order that has
            # already finished. The kernel now preserves per-link ordering so
            # this should not arise, but an agent whose book of working orders
            # depends on message ordering being perfect is an agent that will
            # eventually be wrong about its own risk.
            if key not in self._completed:
                self.live_orders[key] = symbol
                self.order_side[key] = event.side
                # What actually rested, which is not always what was asked
                # for: an order beyond the price band slides to its edge. The
                # intent has to record the *result*, or the agent believes it
                # is quoting where it wanted, sees no reason to requote, and
                # leaves a slid order sitting at the band for the rest of the
                # session.
                if event.price is not None:
                    self._intent[(symbol, event.side)] = (
                        int(event.price),
                        int(event.quantity),
                    )
        elif isinstance(event, Filled):
            self.fills += 1
            # The symbol comes from the event, not from a lookup in
            # live_orders. Looking it up would silently drop the fill if the
            # acknowledgement had not arrived yet -- and a dropped fill means
            # the agent's position diverges from the venue's, which is the
            # worst class of bug this system can have.
            signed = int(event.quantity) * (1 if event.side is Side.BUY else -1)
            self.position[symbol] = self.position.get(symbol, 0) + signed
            if int(event.remaining) == 0:
                self._complete(key)
        elif isinstance(event, Rejected):
            self.rejects += 1
            self._complete(key)
        else:
            self._complete(key)
        self.on_private(ctx, event)

    def _complete(self, key: Any) -> None:
        if key is None:
            return
        self.live_orders.pop(key, None)
        self.order_side.pop(key, None)
        # A dict rather than a set, purely for its insertion order. Trimming a
        # set to "the last 2048" is a sentence with no meaning -- ``list(set)``
        # yields hash order -- so the old bound discarded an arbitrary half of
        # the record and could resurrect a finished order at any time.
        self._completed.pop(key, None)
        self._completed[key] = None
        if len(self._completed) > 4096:
            # Bounded: a long session would otherwise grow this without limit,
            # and only recent ids can plausibly be re-acknowledged.
            for stale in list(self._completed)[:2048]:
                del self._completed[stale]

    def on_quote(self, ctx: SimulationContext, quote: TopOfBook) -> None:
        """A quote update arrived. Override to react."""

    def on_print(self, ctx: SimulationContext, print_: TradePrint) -> None:
        """A trade printed. Override to react."""

    def on_private(self, ctx: SimulationContext, event: Any) -> None:
        """A private event arrived. Override to react."""

    # -- actions -----------------------------------------------------------

    def quote(
        self,
        ctx: SimulationContext,
        symbol: str,
        side: Side,
        price: Price,
        quantity: int,
        tif: TimeInForce = TimeInForce.GTC,
    ) -> None:
        """Send a limit order, clamped to the instrument's settlement range.

        Clamping rather than rejecting: an agent quoting outside the range is
        asking for something the contract cannot pay, and the sensible venue
        behaviour is to refuse it. But an agent that computed a price slightly
        past the boundary is usually right about direction and wrong about
        magnitude, so pinning it to the boundary keeps it in the market instead
        of silently dropping its order.
        """
        instrument = self.instruments[symbol]
        low, high = instrument.tick_bounds
        bounded = Price(max(int(low), min(int(high), int(price))))
        ctx.send(
            self.venue_id,
            SymbolCommand(
                symbol,
                Submit(
                    self.agent_id,
                    side,
                    Quantity(max(1, quantity)),
                    bounded,
                    OrderType.LIMIT,
                    tif,
                ),
            ),
        )

    def post(
        self,
        ctx: SimulationContext,
        symbol: str,
        side: Side,
        price: Price,
        size: int,
        tif: TimeInForce = TimeInForce.GTC,
    ) -> None:
        """Work a quote, replacing what is there only if it has moved.

        Cancelling and replacing an order at a price it is already at is the
        worst thing an agent can do with a message: it gives up queue priority
        for nothing and pays for the privilege. No real participant does it.

        Doing it here cost more than realism. Every agent cancelled and
        reposted on all twenty-six contracts on every wakeup, which produced
        **1.6 million events per simulated minute** -- the market spending its
        time on paperwork rather than on trading. It was invisible until agents
        could track their own orders, because before that they lost most of
        them and most of the cancels were never sent: the bug was quietly
        acting as a rate limiter.
        """
        wanted = (int(price), int(size))
        working = any(
            order_symbol == symbol and self.order_side.get(key) is side
            for key, order_symbol in self.live_orders.items()
        )
        if working and self._intent.get((symbol, side)) == wanted:
            return
        self.cancel_side(ctx, symbol, side)
        self._intent[(symbol, side)] = wanted
        self.quote(ctx, symbol, side, price, size, tif)

    def withdraw(self, ctx: SimulationContext, symbol: str, side: Side) -> None:
        """Pull this agent's quote on one side, if it has one."""
        if self.cancel_side(ctx, symbol, side):
            self._intent.pop((symbol, side), None)

    def cancel_side(self, ctx: SimulationContext, symbol: str, side: Side) -> bool:
        """Pull this agent's working orders on one side of one book.

        Separate from :meth:`cancel_all` because replacing a quote should not
        disturb the other side of it: an agent that cancels both sides to move
        one gives up its queue position on an order it was perfectly happy
        with, and pays for it twice -- once in priority and once in messages.
        """
        pulled = False
        for key, order_symbol in list(self.live_orders.items()):
            if order_symbol != symbol or self.order_side.get(key) is not side:
                continue
            ctx.send(
                self.venue_id,
                SymbolCommand(order_symbol, Cancel(self.agent_id, key[1])),
            )
            self.live_orders.pop(key, None)
            self.order_side.pop(key, None)
            pulled = True
        return pulled

    def take(
        self, ctx: SimulationContext, symbol: str, side: Side, quantity: int
    ) -> None:
        """Cross the spread immediately, taking whatever is available."""
        ctx.send(
            self.venue_id,
            SymbolCommand(
                symbol,
                Submit(
                    self.agent_id,
                    side,
                    Quantity(max(1, quantity)),
                    None,
                    OrderType.MARKET,
                    TimeInForce.IOC,
                ),
            ),
        )

    def cancel_all(self, ctx: SimulationContext, symbol: str | None = None) -> None:
        for key, order_symbol in list(self.live_orders.items()):
            if symbol is not None and order_symbol != symbol:
                continue
            ctx.send(
                self.venue_id,
                SymbolCommand(order_symbol, Cancel(self.agent_id, key[1])),
            )
            self.live_orders.pop(key, None)
