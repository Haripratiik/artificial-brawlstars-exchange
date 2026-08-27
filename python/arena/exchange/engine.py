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
from dataclasses import dataclass

from arena.exchange.book import Order, OrderBook
from arena.exchange.session import SENTINEL, SessionState, indicative_auction
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
    AgentId,
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


@dataclass(slots=True)
class _PendingStop:
    """A stop order waiting for its trigger.

    Not an :class:`Order`, because it is not one yet: it has no place in a
    queue, no price priority, and nothing anyone can trade against. It becomes
    an order when the market reaches it.
    """

    order_id: OrderId
    agent_id: AgentId
    side: Side
    quantity: Quantity
    stop_price: Price
    limit_price: Price | None
    time_in_force: TimeInForce
    display_size: int
    arrival: int

    def as_submit(self) -> Submit:
        """The order this becomes once it is triggered.

        A plain stop becomes a *market* order, and a market order is
        immediate-or-cancel by construction here -- an unpriced order that
        rested would match anything forever. Carrying the stop's own
        time-in-force through would hand the engine a GTC market order, which
        it refuses, and the stop would vanish on being triggered: parked,
        released, rejected, gone, with nothing in the tape to say so.
        """
        if self.limit_price is None:
            return Submit(
                self.agent_id,
                self.side,
                self.quantity,
                None,
                OrderType.MARKET,
                TimeInForce.IOC,
            )
        return Submit(
            self.agent_id,
            self.side,
            self.quantity,
            self.limit_price,
            OrderType.LIMIT,
            self.time_in_force,
            self.display_size,
        )


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
        # Continuous by default, so an engine used on its own behaves exactly
        # as it always has and every existing test keeps its meaning.
        self.phase = SessionState.CONTINUOUS
        # Prices a trade may print at, or ``None`` for no limit.
        #
        # The rule this models does not only pause a runaway after the fact: it
        # *prevents trades outside the bands*, and that is the half that
        # protects anyone. Without it a market order with no price protection
        # walks a thin book to the floor -- measured here, a resting bid at
        # **0.25** was filled on a contract worth 4,700, and the breaker then
        # dutifully halted a symbol whose damage was already done.
        #
        # Set by the venue before each command, because the band moves with the
        # reference price and only the venue tracks that.
        self.execution_band: tuple[int, int] | None = None
        # Stop orders waiting for their trigger. Off the book on purpose.
        self._stops: list[_PendingStop] = []
        # How many rounds each cascade of stops ran for, oldest first. A
        # measurement rather than a control: a stop that fills moves the price,
        # which triggers more stops, and how often that chains is exactly the
        # thing worth knowing.
        self.cascade_depth: list[int] = []
        # A cascade that never ends is a bug in the model, not an event in the
        # market. High enough that a real chain is never cut short.
        self._max_cascade = 24
        # True while a cascade is being worked through, so a triggered stop's
        # own trades do not start a nested release inside the loop that is
        # already handling them. Without it the chain recurses instead of
        # iterating: each level records a depth of one, the measurement says
        # nothing, and the bound above guards a loop the cascade is not using.
        self._releasing = False
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
            display_size=command.display_size,
        )

        # A stop acknowledges at the price it is contingent on: its limit if it
        # has one, otherwise its trigger. The venue reserves collateral from
        # this, and a stop that acknowledged no price at all would be reserved
        # against by nothing -- an agent could park a hundred of them, each
        # individually affordable and collectively not.
        acknowledged_at = command.price
        if command.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            acknowledged_at = command.price or command.stop_price

        events: list[Event] = [
            Acknowledged(
                self._seq(),
                command.agent_id,
                order_id,
                command.side,
                command.quantity,
                acknowledged_at,
            )
        ]

        if command.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return events + self._park_stop(command, order_id)

        if not self.phase.matches_continuously:
            return events + self._accumulate(command, order)

        # Post-only is decided before anything trades, for the same reason a
        # maker uses it: the whole point is that no part of it may ever take.
        if command.time_in_force is TimeInForce.POST_ONLY and self._crossable_levels(
            command.side, limit
        ):
            order.status = OrderStatus.REJECTED
            self.book.track(order)
            events.append(
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.POST_ONLY_WOULD_CROSS,
                    order_id,
                )
            )
            return events

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

        events.extend(
            self._match(
                order,
                self.execution_band
                if command.order_type is OrderType.MARKET
                else None,
            )
        )
        # A print can bring stop orders to life, and one of those can print
        # again. Released after this order has finished matching rather than
        # inside the loop, so a cascade is a sequence of complete orders rather
        # than an interleaving of half-filled ones.
        last = next(
            (e.price for e in reversed(events) if isinstance(e, Traded)), None
        )
        if last is not None and self._stops and not self._releasing:
            events.extend(self._release_stops(last))

        if order.remaining > 0:
            if command.time_in_force in (TimeInForce.GTC, TimeInForce.POST_ONLY):
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

    def _accumulate(self, command: Submit, order: Order) -> list[Event]:
        """Rest an order during a call phase, where nothing matches yet.

        A *limit* order marked immediate-or-cancel or fill-or-kill is refused
        rather than silently rested: both are instructions about what to do
        *right now*, and during a call phase there is no right now. Accepting
        them would quietly convert a "do not leave this working" order into one
        that works until the uncross.

        A **market** order is the exception, and not an inconsistency. It is
        required to be IOC in continuous trading only because an unpriced
        resting order would match anything forever -- during a call phase nothing
        matches until the uncross, so the danger does not exist. What it becomes
        is a market-on-open order: willing to trade at whatever price the
        auction clears at, which is exactly what such an order means.
        """
        if command.order_type is not OrderType.MARKET and command.time_in_force in (
            TimeInForce.IOC,
            TimeInForce.FOK,
        ):
            order.status = OrderStatus.CANCELLED
            self.book.track(order)
            return [
                Rejected(
                    self._seq(),
                    command.agent_id,
                    RejectReason.NOT_ACCEPTED_IN_AUCTION,
                    order.order_id,
                )
            ]
        order.status = OrderStatus.NEW
        self.book.add(order)
        return []

    def uncross(self, reference: Price | None = None) -> list[Event]:
        """Clear the accumulated book at a single price.

        Everything trades at the auction price, including orders that were
        willing to pay more -- the price improvement is the reward for having
        been in the auction, and it is why the clearing price is trustworthy in
        a way a first-arrival price is not.

        Nobody is the aggressor here, so every fill is booked as passive. Under
        a maker-taker schedule that means auction fills earn the maker rate on
        both sides, which is what venues that run auctions actually charge.
        """
        result = indicative_auction(self.book, reference)
        if result is None or result.volume <= 0:
            # Even a call that clears nothing has to take the market orders
            # back out. An auction with no crossing interest is the *most*
            # likely place to leave one behind, and one left behind is a
            # sentinel-priced order sitting at the touch of a continuous book.
            return self._cancel_unfilled_market_orders()

        limit = int(result.price)
        buys = sorted(
            (o for o in self.book.resting_orders
             if o.side is Side.BUY and int(o.price) >= limit),
            key=lambda o: (-int(o.price), o.priority),
        )
        sells = sorted(
            (o for o in self.book.resting_orders
             if o.side is Side.SELL and int(o.price) <= limit),
            key=lambda o: (int(o.price), o.priority),
        )

        events: list[Event] = []
        i = j = 0
        while i < len(buys) and j < len(sells):
            buy, sell = buys[i], sells[j]
            if buy.remaining <= 0:
                i += 1
                continue
            if sell.remaining <= 0:
                j += 1
                continue
            if buy.agent_id == sell.agent_id:
                # A wash print is worse in an auction than in continuous
                # trading: it would be struck at the official price and could
                # move a settlement. Drop the older side and carry on, which is
                # the CANCEL_OLDEST policy applied to a two-sided book.
                stale = buy if buy.priority <= sell.priority else sell
                events.append(
                    Cancelled(self._seq(), stale.agent_id, stale.order_id, stale.remaining)
                )
                self.book.remove(stale)
                i, j = (i + 1, j) if stale is buy else (i, j + 1)
                continue

            quantity = Quantity(min(int(buy.remaining), int(sell.remaining)))
            price = Price(limit)
            for order in (buy, sell):
                self.book.consume(order, quantity)
                events.append(
                    Filled(
                        self._seq(),
                        order.agent_id,
                        order.order_id,
                        order.side,
                        quantity,
                        price,
                        False,
                        order.remaining,
                    )
                )
            trade = Traded(
                self._seq(),
                quantity,
                price,
                # An auction has no aggressor. The surplus side is the closest
                # honest analogue, so order-flow statistics stay meaningful.
                result.surplus_side or Side.BUY,
                buy.order_id,
                sell.order_id,
            )
            self._tape.append(trade)
            events.append(trade)

        events.extend(self._cancel_unfilled_market_orders())
        return events

    def _cancel_unfilled_market_orders(self) -> list[Event]:
        """Take market-on-open orders that did not trade back out of the book.

        They rest at the sentinel price so that they cross every candidate in
        the auction, which is the whole point of them -- and it is why leaving
        one behind is catastrophic rather than untidy. An unfilled market sell
        sits at minus 2^61, which is the best offer in the book by a margin of
        2^61, so the first continuous buy order matches it *at that price*. Run
        for the first time, this printed trades at -4,611,686,018,427,387,904,
        marked the book at zero and billed 4.8e22 in fees before anything else
        went wrong.

        A market order is an instruction about the auction it was entered for.
        Once that auction has cleared there is no price it was willing to pay,
        because it never named one, so cancelling is the only honest outcome --
        and it is what venues do with unexecuted market-on-open interest.
        """
        events: list[Event] = []
        for order in list(self.book.resting_orders):
            if abs(int(order.price)) < SENTINEL:
                continue
            events.append(
                Cancelled(self._seq(), order.agent_id, order.order_id, order.remaining)
            )
            self.book.remove(order)
            # The status, and not only the level bookkeeping. `Book.remove`
            # reduces the level's total and leaves the order in the queue as a
            # tombstone; what makes the matcher skip it on the way past is its
            # status being terminal. Removing without marking left an order the
            # depth no longer counted but the matcher would still fill -- so it
            # was invisible to every diagnostic that reads resting orders while
            # remaining perfectly tradeable, which is why the sentinel prints
            # survived two attempts at fixing them.
            order.status = OrderStatus.CANCELLED
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

    def _tradeable(self, price: Price) -> bool:
        """Whether a trade may print at this price."""
        if self.execution_band is None:
            return True
        low, high = self.execution_band
        return low <= int(price) <= high


    # -- stops -------------------------------------------------------------

    def _park_stop(self, command: Submit, order_id: OrderId) -> list[Event]:
        """Hold a stop off the book until its trigger is reached.

        Off the book, not on it: a resting stop is not liquidity and must not
        appear as any. Publishing one would tell everybody exactly where the
        market has to go to set off a cascade, which is the single piece of
        information a stop order's owner most wants kept quiet.
        """
        self._stops.append(
            _PendingStop(
                order_id=order_id,
                agent_id=command.agent_id,
                side=command.side,
                quantity=command.quantity,
                stop_price=command.stop_price,
                limit_price=command.price,
                time_in_force=command.time_in_force,
                display_size=command.display_size,
                arrival=self._arrival,
            )
        )
        return []

    def _triggered_by(self, price: Price) -> list["_PendingStop"]:
        """Stops this print sets off, in a deterministic order.

        A buy stop triggers when the market trades at or above its price, a
        sell stop at or below. Ordered by how far through the trigger the print
        went and then by arrival, so a single print that sets off several stops
        releases them in the order the market reached them rather than in the
        order they happened to be entered.
        """
        hit = [
            stop
            for stop in self._stops
            if (stop.side is Side.BUY and int(price) >= int(stop.stop_price))
            or (stop.side is Side.SELL and int(price) <= int(stop.stop_price))
        ]
        if not hit:
            return []
        self._stops = [stop for stop in self._stops if stop not in hit]
        hit.sort(
            key=lambda s: (
                -int(s.stop_price) if s.side is Side.BUY else int(s.stop_price),
                s.arrival,
            )
        )
        return hit

    def _release_stops(self, price: Price) -> list[Event]:
        """Fire everything this print triggered, and everything that triggers.

        Iterative rather than recursive, and bounded. A stop that fills moves
        the price, which can trigger more stops -- that is a cascade, it is
        real, and this does not prevent it. What it does prevent is a cascade
        that never terminates, which would be a bug in the model rather than an
        event in the market. `cascade_depth` records how far each one went.
        """
        events: list[Event] = []
        pending = self._triggered_by(price)
        depth = 0
        self._releasing = True
        try:
            depth = self._work_cascade(pending, events)
        finally:
            self._releasing = False
        if depth:
            self.cascade_depth.append(depth)
        return events

    def _work_cascade(self, pending: list["_PendingStop"], events: list[Event]) -> int:
        depth = 0
        while pending and depth < self._max_cascade:
            depth += 1
            following: list[_PendingStop] = []
            for stop in pending:
                released = self._submit(stop.as_submit())
                events.extend(released)
                for event in released:
                    if isinstance(event, Traded):
                        following.extend(self._triggered_by(event.price))
            pending = following
        return depth

    def _match(
        self, incoming: Order, collar: tuple[int, int] | None = None
    ) -> list[Event]:
        """Walk the opposite side until filled, out of crossable price, or out
        of collar.

        The collar applies to **market orders only**, and that distinction is
        the whole of it. A market order names no price, so it needs protecting
        from the book: without a collar one walked a thin book to the floor and
        filled a resting bid at **0.25** on a contract worth 4,700. A limit
        order names a price and is entitled to it; collaring one too was tried
        and was much worse than the disease. Orders slid to a band edge, the
        band later moved away from them, and the book locked -- bid above offer,
        neither allowed to trade, nothing in continuous trading able to clear
        it. Measured on that version: 2,492 limit states in five minutes and a
        future marking at 9,267 against a settlement of 4,669.
        """
        events: list[Event] = []

        while incoming.remaining > 0:
            level = self.book.best_level(incoming.side.opposite)
            if level is None:
                break
            resting_price = level.price
            if not incoming.side.crosses(resting_price, incoming.price):
                break
            if collar is not None:
                low, high = collar
                if not low <= int(resting_price) <= high:
                    # Past the edge of the collar. The order stops here rather
                    # than printing beyond it, and whatever is left is
                    # cancelled -- a market order was never willing to rest.
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

            # At most the slice an iceberg is showing. Taking its reserve
            # in one go would make the reserve pointless: the aggressor would
            # get the whole order at one price and nobody else at that level
            # would ever get a turn, which is precisely what a hidden order is
            # not entitled to.
            available = resting.shown if resting.is_iceberg else resting.remaining
            traded = Quantity(min(int(incoming.remaining), int(available)))
            if traded <= 0:
                level.prune()
                continue

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
    if command.display_size < 0:
        return RejectReason.INVALID_QUANTITY
    stopping = command.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    if stopping and command.stop_price is None:
        return RejectReason.INVALID_STOP_PRICE
    if not stopping and command.stop_price is not None:
        return RejectReason.INVALID_STOP_PRICE
    if command.order_type is OrderType.STOP and command.price is not None:
        return RejectReason.INVALID_PRICE
    if command.order_type is OrderType.STOP_LIMIT and command.price is None:
        return RejectReason.LIMIT_ORDER_REQUIRES_PRICE
    if stopping and command.time_in_force in (TimeInForce.IOC, TimeInForce.FOK):
        # "Do this now" and "do this later" are contradictory instructions.
        return RejectReason.MARKET_ORDER_MUST_BE_IOC
    if stopping:
        # Everything below is about an order that exists now. A stop does not:
        # its price rules were checked above, against what it will become.
        return None
    if command.display_size and command.order_type not in (
        OrderType.LIMIT,
        OrderType.STOP_LIMIT,
    ):
        # An order with no price cannot hide anything: it never rests, so
        # there is no queue for a reserve to wait in.
        return RejectReason.INVALID_QUANTITY
    if command.order_type is OrderType.MARKET:
        if command.price is not None:
            return RejectReason.INVALID_PRICE
        if command.time_in_force in (TimeInForce.GTC, TimeInForce.POST_ONLY):
            # An unpriced resting order would match anything forever -- and a
            # post-only market order is a contradiction in terms, since a market
            # order is defined by being willing to cross.
            return RejectReason.MARKET_ORDER_MUST_BE_IOC
        return None
    if command.price is None:
        return RejectReason.LIMIT_ORDER_REQUIRES_PRICE
    return None


def _unbounded(side: Side) -> Price:
    """The price a market order behaves as if it had."""
    return Price(1 << 62) if side is Side.BUY else Price(-(1 << 62))
