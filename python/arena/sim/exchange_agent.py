"""The exchange, as a participant in the simulation.

Wraps a :class:`~arena.exchange.engine.MatchingEngine` and gives it a mailbox.
The engine stays pure -- no clock, no agents, no messages -- and everything
about *when* things happen lives here, where it can be reasoned about
separately.

The split matters for a specific reason. The matching engine is the component
that gets ported to C++ and validated by differential testing, so it must be a
deterministic function of its command stream and nothing else. Latency,
subscriptions, and throttling are simulation concerns; folding them into the
engine would make the port's acceptance test far harder to state.

Two ordering guarantees this agent provides:

**Commands are processed in arrival order**, which the kernel already sequences.
Two agents whose orders arrive in the same nanosecond are separated by the
kernel's insertion counter, so priority is total and reproducible.

**Private events go out before public ones.** An agent learns its own fill
before the market learns a trade happened -- which is true of real exchanges, and
which matters because the opposite would let a subscriber react to a print before
its counterparty knew it had traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from arena.exchange.engine import MatchingEngine
from arena.exchange.events import (
    Acknowledged,
    Cancel,
    Cancelled,
    Command,
    Event,
    Filled,
    Rejected,
    Replace,
    Replaced,
    Submit,
    Traded,
)
from arena.exchange.types import AgentId, Quantity, RejectReason, SequenceNumber, Side
from arena.sim.kernel import SimulationContext
from arena.sim.messages import (
    DepthUpdate,
    Feed,
    PrivateEvent,
    Subscribe,
    TopOfBook,
    TradePrint,
    Unsubscribe,
)
from arena.sim.time import Timestamp

__all__ = ["ExchangeAgent"]


@dataclass(slots=True)
class _Subscription:
    feed: Feed
    throttle: int
    last_sent: int = -1


class ExchangeAgent:
    """A single-instrument venue with a mailbox and market-data feeds."""

    def __init__(
        self,
        agent_id: AgentId,
        instrument: str = "DEFAULT",
        depth_levels: int = 5,
    ) -> None:
        self.agent_id = agent_id
        self.instrument = instrument
        self.engine = MatchingEngine(instrument)
        self.depth_levels = depth_levels
        self._subscriptions: dict[AgentId, dict[Feed, _Subscription]] = {}
        self._open = False
        # Every public event, in order, for post-hoc analysis. The research
        # harness reads this; agents never do.
        self.public_log: list[tuple[Timestamp, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def on_start(self, ctx: SimulationContext) -> None:
        self._open = True

    def on_finish(self, ctx: SimulationContext) -> None:
        self._open = False

    def on_wakeup(self, ctx: SimulationContext) -> None:
        """The exchange does not act on its own schedule."""

    # -- mailbox -----------------------------------------------------------

    def on_message(self, ctx: SimulationContext, sender: AgentId, message: Any) -> None:
        if isinstance(message, Subscribe):
            self._subscriptions.setdefault(sender, {})[message.feed] = _Subscription(
                feed=message.feed, throttle=message.throttle
            )
            # Send an immediate snapshot so a new subscriber is not blind until
            # something happens to move the book.
            self._send_snapshot(ctx, sender, message.feed)
            return

        if isinstance(message, Unsubscribe):
            self._subscriptions.get(sender, {}).pop(message.feed, None)
            return

        if isinstance(message, (Submit, Cancel, Replace)):
            self._handle_command(ctx, sender, message)
            return

    def _handle_command(
        self, ctx: SimulationContext, sender: AgentId, command: Command
    ) -> None:
        if command.agent_id != sender:
            # An agent may only act for itself. Trusting the field would let one
            # agent cancel another's orders by writing a different id.
            ctx.send(sender, PrivateEvent(_forged(command)))
            return

        events = self.engine.apply(command)

        # Private first: the owner learns of its own fill before the tape does.
        for event in events:
            recipient = _owner(event)
            if recipient is not None:
                ctx.send(recipient, PrivateEvent(event))

        for event in events:
            if isinstance(event, Traded):
                print_ = TradePrint(
                    timestamp=ctx.now,
                    sequence=event.sequence,
                    price=event.price,
                    quantity=event.quantity,
                    aggressor_side=event.aggressor_side,
                )
                self.public_log.append((ctx.now, print_))
                self._broadcast(ctx, Feed.TRADES, print_)

        if events:
            self._publish_quotes(ctx)

    # -- market data -------------------------------------------------------

    def _publish_quotes(self, ctx: SimulationContext) -> None:
        top = self._top_of_book(ctx.now)
        self.public_log.append((ctx.now, top))
        self._broadcast(ctx, Feed.TOP_OF_BOOK, top)
        self._broadcast(
            ctx,
            Feed.DEPTH,
            DepthUpdate(ctx.now, self.engine.book.snapshot(self.depth_levels)),
        )

    def _top_of_book(self, now: Timestamp) -> TopOfBook:
        book = self.engine.book
        bid = book.best_price(_BUY)
        ask = book.best_price(_SELL)
        return TopOfBook(
            timestamp=now,
            bid=bid,
            bid_size=book.depth_at(_BUY, bid) if bid is not None else Quantity(0),
            ask=ask,
            ask_size=book.depth_at(_SELL, ask) if ask is not None else Quantity(0),
        )

    def _broadcast(self, ctx: SimulationContext, feed: Feed, message: Any) -> None:
        """Send to every subscriber, each after their own latency.

        Subscribers are iterated in sorted order so the sequence of sends -- and
        therefore the kernel sequence numbers that break timestamp ties -- does
        not depend on subscription order.
        """
        for recipient in sorted(self._subscriptions):
            subscription = self._subscriptions[recipient].get(feed)
            if subscription is None:
                continue
            if subscription.throttle > 0:
                if int(ctx.now) - subscription.last_sent < subscription.throttle:
                    continue
            subscription.last_sent = int(ctx.now)
            ctx.send(recipient, message)

    def _send_snapshot(
        self, ctx: SimulationContext, recipient: AgentId, feed: Feed
    ) -> None:
        if feed is Feed.TOP_OF_BOOK:
            ctx.send(recipient, self._top_of_book(ctx.now))
        elif feed is Feed.DEPTH:
            ctx.send(
                recipient,
                DepthUpdate(ctx.now, self.engine.book.snapshot(self.depth_levels)),
            )

    # -- convenience for the research harness ------------------------------

    @property
    def tape(self):
        return self.engine.tape

    def snapshot(self, levels: int = 5):
        return self.engine.book.snapshot(levels)


_BUY = Side.BUY
_SELL = Side.SELL


def _owner(event: Event) -> AgentId | None:
    """Which agent, if any, this event is private to."""
    if isinstance(event, (Acknowledged, Rejected, Filled, Cancelled, Replaced)):
        return event.agent_id
    return None


def _forged(command: Command) -> Rejected:
    order_id = getattr(command, "order_id", None)
    return Rejected(
        SequenceNumber(0), command.agent_id, RejectReason.NOT_ORDER_OWNER, order_id
    )
