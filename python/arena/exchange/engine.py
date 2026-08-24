"""The matching engine.

Deterministic by construction: no clock, no randomness, no I/O. Feed it the same
commands in the same order and it emits byte-identical events, every time, on
any machine. That property is not a nicety -- it is the acceptance test for the
C++ port, which must produce an identical event stream from an identical command
stream, and it is what makes a seeded experiment reproducible months later.

Matching rules, in the order they are applied:

  1. **Price priority.** The best price trades first.
  2. **Time priority.** Within a price, the order that arrived first trades
     first, by arrival sequence rather than by timestamp.
  3. **Trades print at the resting order's price.** The passive side set the
     terms; the aggressor accepted them. This is what makes price improvement
     accrue to the taker and what makes effective spread measurable.
  4. **Partial fills are normal.** An order walks as many levels as it needs.
"""

from __future__ import annotations

from collections.abc import Iterable

from arena.exchange.book import Order, OrderBook
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
from arena.exchange.types import (
    OrderId,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    RejectReason,
    SelfTradePrevention,
    SequenceNumber,
    Side,
    TimeInForce,
)

__all__ = ["MatchingEngine"]


class MatchingEngine:
    """A single-instrument exchange."""

    def __init__(
        self,
        instrument: str = "DEFAULT",
        self_trade_prevention: SelfTradePrevention = SelfTradePrevention.CANCEL_OLDEST,
    ) -> None:
        self.instrument = instrument
        self.self_trade_prevention = self_trade_prevention
        self.book = OrderBook()
        self._sequence = 0
        self._next_order_id = 0
        self._arrival = 0
        self._tape: list[Traded] = []

    # -- identity ----------------------------------------------------------

    def _seq(self) -> SequenceNumber:
        self._sequence += 1
        return SequenceNumber(self._sequence)

    def _order_id(self) -> OrderId:
        self._next_order_id += 1
        return OrderId(self._next_order_id)

    @property
    def tape(self) -> tuple[Traded, ...]:
        """Every trade printed, in order. The public record."""
        return tuple(self._tape)

    # -- dispatch ----------------------------------------------------------

    def apply(self, command: Command) -> list[Event]:
        """Process one command, returning the events it caused."""
        if isinstance(command, Submit):
            return self._submit(command)
        if isinstance(command, Cancel):
            return self._cancel(command)
        if isinstance(command, Replace):
            return self._replace(command)
        raise TypeError(f"unknown command type {type(command).__name__}")

    def apply_all(self, commands: Iterable[Command]) -> list[Event]:
        events: list[Event] = []
        for command in commands:
            events.extend(self.apply(command))
        return events

    # -- submit ------------------------------------------------------------

    def _submit(self, command: Submit) -> list[Event]:
        reason = _validate(command)
        if reason is not None:
            return [Rejected(self._seq(), command.agent_id, reason)]

        order_id = self._order_id()
        self._arrival += 1
        # A market order is priced at the extreme so it crosses everything; the
        # limit-price comparison then needs no special case for it.
        limit = command.price if command.price is not None else _unbounded(command.side)
        order = Order(
            order_id=order_id,
            agent_id=command.agent_id,
            side=command.side,
            price=limit,
            quantity=command.quantity,
            remaining=command.quantity,
            priority=self._arrival,
        )

        events: list[Event] = [
            Acknowledged(
                self._seq(),
                command.agent_id,
                order_id,
                command.side,
                command.quantity,
                command.price,
            )
        ]

        # Fill-or-kill is decided before anything trades, so a partial walk is
        # never left half-done and then unwound. Checking first is simpler and
        # leaves no intermediate state an observer could see.
        if command.time_in_force is TimeInForce.FOK and not self._fillable(
            command.side, limit, command.quantity
        ):
            order.status = OrderStatus.CANCELLED
            self.book.track(order)
            events.append(
                Rejected(
                    self._seq(), command.agent_id, RejectReason.FOK_NOT_FILLABLE, order_id
                )
            )
            return events

        events.extend(self._match(order))

        if order.remaining > 0:
            if command.time_in_force is TimeInForce.GTC:
                order.status = (
                    OrderStatus.PARTIALLY_FILLED
                    if order.remaining < order.quantity
                    else OrderStatus.NEW
                )
                self.book.add(order)
            else:
                order.status = OrderStatus.CANCELLED
                self.book.track(order)
                events.append(
                    Cancelled(self._seq(), command.agent_id, order_id, order.remaining)
                )
        else:
            order.status = OrderStatus.FILLED
            self.book.track(order)

        return events

    def _fillable(self, side: Side, limit: Price, quantity: Quantity) -> bool:
        """Whether ``quantity`` could be filled immediately at ``limit`` or better."""
        available = 0
        for price, depth in self._crossable_levels(side, limit):
            available += depth
            if available >= quantity:
                return True
        return False

    def _crossable_levels(
        self, side: Side, limit: Price
    ) -> list[tuple[Price, int]]:
        """Opposite-side levels this order could reach, best first."""
        opposite = side.opposite
        snapshot = self.book.snapshot(levels=1 << 20)
        levels = snapshot.asks if opposite is Side.SELL else snapshot.bids
        return [(p, int(q)) for p, q in levels if side.crosses(p, limit)]

    def _prevent_self_trade(
        self, incoming: Order, resting: Order, level
    ) -> list[Event]:
        """Resolve a would-be wash trade according to the configured policy.

        Removing the resting order also pops it off the level, so the matching
        loop advances rather than meeting the same order forever -- which would
        be an infinite loop rather than a wrong price.
        """
        events: list[Event] = []
        policy = self.self_trade_prevention

        if policy in (SelfTradePrevention.CANCEL_OLDEST, SelfTradePrevention.CANCEL_BOTH):
            remaining = resting.remaining
            self.book.remove(resting)
            resting.status = OrderStatus.CANCELLED
            level.popleft()
            events.append(
                Cancelled(self._seq(), resting.agent_id, resting.order_id, remaining)
            )

        if policy in (SelfTradePrevention.CANCEL_NEWEST, SelfTradePrevention.CANCEL_BOTH):
            events.append(
                Cancelled(
                    self._seq(), incoming.agent_id, incoming.order_id, incoming.remaining
                )
            )
            incoming.remaining = Quantity(0)
            incoming.status = OrderStatus.CANCELLED

        return events

    def _match(self, incoming: Order) -> list[Event]:
        """Walk the opposite side until filled or out of crossable price."""
        events: list[Event] = []

        while incoming.remaining > 0:
            level = self.book.best_level(incoming.side.opposite)
            if level is None:
                break
            resting_price = level.price
            if not incoming.side.crosses(resting_price, incoming.price):
                break

            level.prune()
            if level.empty:
                # Level emptied by pruning; loop and let best_level move on.
                continue

            resting = level.peek()

            if (
                resting.agent_id == incoming.agent_id
                and self.self_trade_prevention is not SelfTradePrevention.ALLOW
            ):
                events.extend(self._prevent_self_trade(incoming, resting, level))
                if incoming.status is OrderStatus.CANCELLED:
                    break
                continue

            traded = Quantity(min(incoming.remaining, resting.remaining))

            self.book.consume(resting, traded)
            incoming.remaining = Quantity(incoming.remaining - traded)

            buy_id, sell_id = (
                (incoming.order_id, resting.order_id)
                if incoming.side is Side.BUY
                else (resting.order_id, incoming.order_id)
            )

            # Trades print at the RESTING price: the passive side set the terms.
            events.append(
                Filled(
                    self._seq(),
                    resting.agent_id,
                    resting.order_id,
                    resting.side,
                    traded,
                    resting_price,
                    aggressor=False,
                    remaining=resting.remaining,
                )
            )
            events.append(
                Filled(
                    self._seq(),
                    incoming.agent_id,
                    incoming.order_id,
                    incoming.side,
                    traded,
                    resting_price,
                    aggressor=True,
                    remaining=incoming.remaining,
                )
            )
            trade = Traded(
                self._seq(),
                traded,
                resting_price,
                aggressor_side=incoming.side,
                buy_order_id=buy_id,
                sell_order_id=sell_id,
            )
            events.append(trade)
            self._tape.append(trade)

            if resting.remaining <= 0:
                level.popleft()

        return events

    # -- cancel ------------------------------------------------------------

    def _cancel(self, command: Cancel) -> list[Event]:
        order = self.book.get(command.order_id)
        if order is None:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.UNKNOWN_ORDER,
                    command.order_id,
                )
            ]
        if order.agent_id != command.agent_id:
            # Reported as not-owner rather than unknown. Leaking "this id exists"
            # is harmless here -- ids are engine-assigned and sequential, so an
            # agent could enumerate them anyway -- and the honest error is far
            # easier to debug than a misleading one.
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.NOT_ORDER_OWNER,
                    command.order_id,
                )
            ]
        if order.status.terminal:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.ALREADY_TERMINAL,
                    command.order_id,
                )
            ]

        remaining = order.remaining
        self.book.remove(order)
        order.status = OrderStatus.CANCELLED
        return [Cancelled(self._seq(), command.agent_id, command.order_id, remaining)]

    # -- replace -----------------------------------------------------------

    def _replace(self, command: Replace) -> list[Event]:
        order = self.book.get(command.order_id)
        if order is None:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.UNKNOWN_ORDER,
                    command.order_id,
                )
            ]
        if order.agent_id != command.agent_id:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.NOT_ORDER_OWNER,
                    command.order_id,
                )
            ]
        if order.status.terminal:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.ALREADY_TERMINAL,
                    command.order_id,
                )
            ]
        if command.new_quantity <= 0:
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.INVALID_QUANTITY,
                    command.order_id,
                )
            ]

        new_price = command.new_price if command.new_price is not None else order.price
        # Priority survives only a strict reduction at an unchanged price. Any
        # price change or size increase is a new claim on the queue.
        keeps_priority = new_price == order.price and command.new_quantity < order.remaining

        if keeps_priority:
            shrink = Quantity(order.remaining - command.new_quantity)
            self.book.consume(order, shrink)
            # consume() marks a shrunk order as partially filled, which is wrong
            # here: nothing traded. Restore the status the order actually has.
            order.status = (
                OrderStatus.PARTIALLY_FILLED
                if order.filled > 0
                else OrderStatus.NEW
            )
            return [
                Replaced(
                    self._seq(),
                    command.agent_id,
                    command.order_id,
                    command.new_quantity,
                    new_price,
                    kept_priority=True,
                )
            ]

        # Otherwise: pull the old order and resubmit at the back of the queue,
        # re-running the match in case the new price now crosses.
        self.book.remove(order)
        order.status = OrderStatus.CANCELLED

        self._arrival += 1
        replacement = Order(
            order_id=command.order_id,
            agent_id=command.agent_id,
            side=order.side,
            price=new_price,
            quantity=command.new_quantity,
            remaining=command.new_quantity,
            priority=self._arrival,
        )
        events: list[Event] = [
            Replaced(
                self._seq(),
                command.agent_id,
                command.order_id,
                command.new_quantity,
                new_price,
                kept_priority=False,
            )
        ]
        events.extend(self._match(replacement))
        if replacement.remaining > 0:
            replacement.status = (
                OrderStatus.PARTIALLY_FILLED
                if replacement.remaining < replacement.quantity
                else OrderStatus.NEW
            )
            self.book.add(replacement)
        else:
            replacement.status = OrderStatus.FILLED
            self.book.track(replacement)
        return events


def _validate(command: Submit) -> RejectReason | None:
    if command.quantity <= 0:
        return RejectReason.INVALID_QUANTITY
    if command.order_type is OrderType.MARKET:
        if command.price is not None:
            return RejectReason.INVALID_PRICE
        if command.time_in_force is TimeInForce.GTC:
            # An unpriced resting order would match anything forever.
            return RejectReason.MARKET_ORDER_MUST_BE_IOC
        return None
    if command.price is None:
        return RejectReason.LIMIT_ORDER_REQUIRES_PRICE
    return None


def _unbounded(side: Side) -> Price:
    """The price a market order behaves as if it had."""
    return Price(1 << 62) if side is Side.BUY else Price(-(1 << 62))
