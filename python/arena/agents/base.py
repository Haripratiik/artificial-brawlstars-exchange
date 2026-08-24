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
        self.live_orders: dict[OrderId, str] = {}
        # Orders known to be finished, so a late message cannot revive one.
        self._completed: set[OrderId] = set()
        self.position: dict[str, int] = dict.fromkeys(instruments, 0)
        self.fills = 0
        self.rejects = 0

    # -- lifecycle ---------------------------------------------------------

    def on_start(self, ctx: SimulationContext) -> None:
        for symbol in sorted(self.instruments):
            ctx.send(self.venue_id, Subscribe(Feed.TOP_OF_BOOK, symbol))
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

        if isinstance(event, Acknowledged):
            # A late acknowledgement must not resurrect an order that has
            # already finished. The kernel now preserves per-link ordering so
            # this should not arise, but an agent whose book of working orders
            # depends on message ordering being perfect is an agent that will
            # eventually be wrong about its own risk.
            if order_id not in self._completed:
                self.live_orders[order_id] = symbol
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
                self._complete(order_id)
        elif isinstance(event, Rejected):
            self.rejects += 1
            self._complete(order_id)
        else:
            self._complete(order_id)
        self.on_private(ctx, event)

    def _complete(self, order_id: Any) -> None:
        if order_id is None:
            return
        self.live_orders.pop(order_id, None)
        self._completed.add(order_id)
        if len(self._completed) > 4096:
            # Bounded: a long session would otherwise grow this without limit,
            # and only recent ids can plausibly be re-acknowledged.
            self._completed = set(list(self._completed)[-2048:])

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
        for order_id, order_symbol in list(self.live_orders.items()):
            if symbol is not None and order_symbol != symbol:
                continue
            ctx.send(
                self.venue_id,
                SymbolCommand(order_symbol, Cancel(self.agent_id, order_id)),
            )
            self.live_orders.pop(order_id, None)
